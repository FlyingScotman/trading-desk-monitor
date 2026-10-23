"""The adapter's child process (08 §2) and the parent-side calls into it.

The backend never imports adapter code itself: every call (``describe``, ``reference``, ``match``, ``read``)
runs in a child ``<python> -I`` with an environment stripped to ``DESK_PRIVATE_ADAPTER*``, ``PATH``, ``SYSTEMROOT``,
``TEMP`` and ``TMP``, so a hung or crashing adapter cannot stop the backend and never sees the store URL or the
hub token. ``-I`` also drops ``PYTHONPATH``; the folder holding ``desk`` is put on the child's ``sys.path`` by
the bootstrap code instead. One JSON request goes in on stdin, one JSON response comes out on stdout (the
adapter's own prints are sent to stderr), and the JSON encoding enforces "plain data only".

A call is one child process (``run``), or, inside ``pooled()`` (one inbox scan), a request to a long-lived child
(``Worker``, ``serve``: one JSON line in, one JSON line out) that imports the adapter and loads the instruments
index once instead of per call: on Windows a Python start and the adapter's imports cost far more than a ``match``.
A worker that times out or crashes is killed like a one-shot child, and the next call starts a new one.
``call_warm`` starts every worker of the pool and has it import the adapter and load the index under a budget of
its own (R22), so the start-up of a slow adapter never eats the timeout of the ``match`` calls that follow.

Exceptions cross as their type and (file, line, function) frames, never the message, which can hold a cell
value (08 §6.8). With ``DESK_PRIVATE_ADAPTER_DEBUG=1`` the child prints the full traceback to stderr and the
parent lets stderr through, for local debugging only.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from desk.inbox.spi import SPI_VERSION, AdapterFailure
from desk.inbox.static_index import Instruments

REFERENCE_ADAPTER = "desk.inbox.reference"
DEBUG_ENV = "DESK_PRIVATE_ADAPTER_DEBUG"
_ENV_KEEP = ("PATH", "SYSTEMROOT", "TEMP", "TMP")
MATCH_CHUNK = 50      # paths per match call: a folder of hundreds of files is not one timeout
KILL_WAIT_S = 5.0     # the bounded wait for a killed child
_DESK_ROOT = str(Path(__file__).resolve().parents[2])  # the folder holding the desk package


def is_path_spec(spec: str) -> bool:
    """A package directory rather than a dotted module name (08 §2)."""
    return any(c in spec for c in "/\\") or spec.startswith((".", "~"))


# ---- child side --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ctx:
    """What the adapter gets as ``ctx`` (08 §7); read-only."""

    spi: int
    config_dir: str | None
    instruments: Instruments
    inbox_dirs: list[str] = field(default_factory=list)  # optional member (08 §7, §9): the report folders


class _LoadError(Exception):
    def __init__(self, code: str, exc: BaseException | None = None) -> None:
        super().__init__(code)
        self.code, self.exc = code, exc


def _frames(exc: BaseException) -> list[list]:
    here = os.path.abspath(__file__)
    return [[os.path.basename(f.filename), f.lineno, f.name] for f in traceback.extract_tb(exc.__traceback__)
            if os.path.abspath(f.filename) != here]


def _error(exc: BaseException) -> dict:
    if os.environ.get(DEBUG_ENV) == "1":
        traceback.print_exception(exc, file=sys.stderr)
    return {"type": type(exc).__name__, "frames": _frames(exc)}


_modules: dict[str, Any] = {}  # child side: the adapter imported once per process (a worker serves many calls)
_indexes: dict[tuple, Instruments] = {}  # child side: the instruments index per (path, mtime, size)


def _import(spec: str | None) -> Any:
    spec = spec or REFERENCE_ADAPTER
    if spec not in _modules:
        _modules[spec] = _import_new(spec)
    return _modules[spec]


def _instruments(path: str | None) -> Instruments:
    if not path:
        return Instruments(None)
    st = os.stat(path)
    key = (path, st.st_mtime_ns, st.st_size)
    if key not in _indexes:
        _indexes.clear()  # one index at a time: the scan rewrites it in place
        _indexes[key] = Instruments.from_file(path)
    return _indexes[key]


def _import_new(spec: str) -> Any:
    if not is_path_spec(spec):
        try:
            return importlib.import_module(spec)
        except BaseException as e:  # noqa: BLE001 - any failure of foreign code is an import error
            raise _LoadError("import_error", e) from None
    d = Path(spec).expanduser().resolve()
    name = d.name
    if not (d / "__init__.py").is_file():
        raise _LoadError("not_a_package")
    if not name.isidentifier():
        raise _LoadError("bad_package_name")
    if name in sys.modules or importlib.util.find_spec(name) is not None:
        raise _LoadError("name_collision")  # no shadowing either way (08 §2)
    mspec = importlib.util.spec_from_file_location(name, d / "__init__.py", submodule_search_locations=[str(d)])
    assert mspec is not None and mspec.loader is not None
    module = importlib.util.module_from_spec(mspec)
    sys.modules[name] = module
    try:
        mspec.loader.exec_module(module)
    except BaseException as e:  # noqa: BLE001
        del sys.modules[name]
        raise _LoadError("import_error", e) from None
    return module


def _plain(value: Any) -> Any:
    """``value`` if it survives strict JSON, else raises: plain data only (08 §3)."""
    json.dumps(value, allow_nan=False)
    return value


def _handle(req: dict) -> dict:
    try:
        mod = _import(req.get("adapter"))
    except _LoadError as e:
        return {"ok": False, "load_error": e.code, **(_error(e.exc) if e.exc else {"type": None, "frames": []})}
    op = req["op"]
    if op == "warm":  # the import above, and the index: paid once per worker, not inside a match timeout
        return {"ok": True, "result": {"snapshot_id": _instruments(req.get("instruments_index")).snapshot_id}}
    if op == "describe":
        missing = [f for f in ("describe", "match", "read") if not callable(getattr(mod, f, None))]
        if missing:
            return {"ok": False, "load_error": "missing_function", "type": None, "frames": [], "missing": missing}
        try:
            desc = _plain(mod.describe())
        except BaseException as e:  # noqa: BLE001
            return {"ok": False, "load_error": "describe_failed", **_error(e)}
        location = {"file": getattr(mod, "__file__", None), "path": list(getattr(mod, "__path__", None) or [])}
        return {"ok": True, "result": {"describe": desc, "spi_version": getattr(mod, "SPI_VERSION", None),
                                       "has_reference": callable(getattr(mod, "reference", None)),
                                       "location": location}}
    ctx = Ctx(SPI_VERSION, req.get("config_dir"), _instruments(req.get("instruments_index")),
              [str(d) for d in req.get("inbox_dirs") or []])
    if op == "match":
        out: dict[str, Any] = {}
        for path in req["paths"]:
            try:
                out[path] = _plain(mod.match(path, ctx))
            except BaseException as e:  # noqa: BLE001 - an adapter bug: match_failed, the scan goes on
                out[path] = {"_error": {"kind": "failed", **_error(e)}}
        return {"ok": True, "result": out}
    try:
        if op == "read":
            result = mod.read(req["path"], req["hit"], ctx)
        elif op == "reference":
            result = mod.reference(ctx)
        else:
            return {"ok": False, "error": {"type": "unknown_op", "frames": []}}
        return {"ok": True, "result": _plain(result)}
    except BaseException as e:  # noqa: BLE001
        return {"ok": False, "error": _error(e)}


def main() -> None:
    # The reply gets its own copy of fd 1; fd 1 itself becomes stderr, so neither prints nor os.write(1, ...),
    # C extensions or subprocesses of the adapter can corrupt the response.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        resp = _handle(json.loads(sys.stdin.buffer.read()))
    except BaseException as e:  # noqa: BLE001
        resp = {"ok": False, "error": _error(e)}
    out.write(json.dumps(resp, allow_nan=False).encode("ascii"))
    out.flush()


def serve() -> None:
    """The worker's loop (``Worker``): one JSON request per line on stdin, one JSON response per line on stdout,
    until stdin closes. stdout is protected as in ``main``."""
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    for line in sys.stdin.buffer:
        try:
            resp = _handle(json.loads(line))
            body = json.dumps(resp, allow_nan=False)
        except BaseException as e:  # noqa: BLE001
            body = json.dumps({"ok": False, "error": _error(e)})
        out.write(body.encode("ascii") + b"\n")
        out.flush()


# ---- parent side -------------------------------------------------------------------------------------------


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if k.startswith("DESK_PRIVATE_ADAPTER") or k.upper() in _ENV_KEEP}


def _kill_tree(p: subprocess.Popen) -> None:
    """The child and everything it started: its own process group (POSIX) or ``taskkill /T`` (Windows)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False, timeout=KILL_WAIT_S)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    p.kill()


def _spawn(entry: str) -> subprocess.Popen:
    boot = f"import sys; sys.path[:0] = [{_DESK_ROOT!r}]; from desk.inbox.runner import {entry}; {entry}()"
    debug = os.environ.get(DEBUG_ENV) == "1"
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    return subprocess.Popen([sys.executable, "-I", "-c", boot], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=None if debug else subprocess.DEVNULL, env=child_env(), **group)


def run(request: dict, timeout_s: float) -> dict:
    """One child process, one request; raises ``AdapterFailure`` on timeout, crash or garbage output. Never
    hangs: on timeout the process tree is killed and the wait for it is bounded too."""
    p = _spawn("main")
    try:
        stdout, _ = p.communicate(json.dumps(request).encode("utf-8"), timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(p)
        try:
            p.communicate(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:  # something still holds the pipe: leave it, never hang
            if p.stdout:
                p.stdout.close()
        raise AdapterFailure("timed_out", "timeout") from None
    if p.returncode != 0:
        raise AdapterFailure("failed", "runner_exit")
    try:
        resp = json.loads(stdout)
    except ValueError:
        raise AdapterFailure("failed", "bad_runner_output") from None
    return resp


class Worker:
    """A long-lived child (``serve``) answering one request at a time. Started on the first call; a timeout, a
    crash or garbage output kills it (``AdapterFailure`` as ``run`` raises) and the next call starts a new one."""

    def __init__(self) -> None:
        self._p: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()

    def _start(self) -> subprocess.Popen:
        p = _spawn("serve")
        lines: queue.Queue = queue.Queue()

        def pump() -> None:  # the reader thread: a blocking readline cannot time out by itself
            try:
                for line in p.stdout:  # type: ignore[union-attr]
                    lines.put(line)
            except (OSError, ValueError):
                pass
            lines.put(None)

        threading.Thread(target=pump, name="desk-adapter-pump", daemon=True).start()
        self._p, self._lines = p, lines
        return p

    def call(self, request: dict, timeout_s: float) -> dict:
        p = self._p if self._p is not None and self._p.poll() is None else self._start()
        try:
            p.stdin.write(json.dumps(request).encode("utf-8") + b"\n")  # type: ignore[union-attr]
            p.stdin.flush()  # type: ignore[union-attr]
        except OSError:
            self.close()
            raise AdapterFailure("failed", "runner_exit") from None
        try:
            line = self._lines.get(timeout=timeout_s)
        except queue.Empty:
            self.close()
            raise AdapterFailure("timed_out", "timeout") from None
        if line is None:
            self.close()
            raise AdapterFailure("failed", "runner_exit")
        try:
            return json.loads(line)
        except ValueError:
            self.close()
            raise AdapterFailure("failed", "bad_runner_output") from None

    def close(self) -> None:
        """Ends the child: stdin closed (its loop returns), killed with its tree if it does not exit at once."""
        p, self._p = self._p, None
        if p is None:
            return
        try:
            p.stdin.close()  # type: ignore[union-attr]
        except OSError:
            pass
        try:
            p.wait(timeout=0.5 if p.poll() is None else 0)
        except subprocess.TimeoutExpired:
            _kill_tree(p)
            try:
                p.wait(timeout=KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
        if p.stdout:
            p.stdout.close()


class Pool:
    """``size`` workers handed out one call at a time (``read`` calls run in parallel threads)."""

    def __init__(self, size: int) -> None:
        self.size = max(1, int(size))
        self._all = [Worker() for _ in range(self.size)]
        self._idle: queue.LifoQueue = queue.LifoQueue()
        for w in self._all:
            self._idle.put(w)

    def call(self, request: dict, timeout_s: float) -> dict:
        w = self._idle.get()
        try:
            return w.call(request, timeout_s)
        finally:
            self._idle.put(w)

    def each(self, request: dict, timeout_s: float) -> list[dict | AdapterFailure]:
        """``request`` on every worker at once (each started if needed), waiting for all: the responses or the
        failures, one per worker. The workers are taken from the idle queue, so no other call interleaves."""
        workers = [self._idle.get() for _ in range(self.size)]
        out: list[dict | AdapterFailure] = [AdapterFailure("failed", "not_called")] * len(workers)

        def one(i: int) -> None:
            try:
                out[i] = workers[i].call(request, timeout_s)
            except AdapterFailure as f:
                out[i] = f

        threads = [threading.Thread(target=one, args=(i,), name="desk-adapter-warm", daemon=True)
                   for i in range(len(workers))]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            for w in workers:
                self._idle.put(w)
        return out

    def close(self) -> None:
        for w in self._all:
            w.close()


_pool: Pool | None = None


@contextmanager
def pooled(size: int = 1) -> Iterator[Pool]:
    """Within the block every ``call_*`` but ``call_describe`` goes to ``size`` long-lived workers, which are
    ended on leaving it (so a changed adapter is imported afresh by the next block)."""
    global _pool
    prev, pool = _pool, Pool(size)
    _pool = pool
    try:
        yield pool
    finally:
        _pool = prev
        pool.close()


def _call(request: dict, timeout_s: float) -> dict:
    pool = _pool
    return pool.call(request, timeout_s) if pool is not None else run(request, timeout_s)


def _request(handle: Any, op: str, instruments_index: str | Path | None, **extra: Any) -> dict:
    if handle.error:
        raise AdapterFailure("failed", "adapter_not_loaded")
    return {"op": op, "adapter": handle.spec, "config_dir": handle.config_dir,
            "inbox_dirs": list(getattr(handle, "inbox_dirs", ())),
            "instruments_index": str(instruments_index) if instruments_index else None, **extra}


def _result(resp: dict) -> Any:
    if resp.get("ok"):
        return resp["result"]
    err = resp.get("error") or {"type": resp.get("load_error"), "frames": resp.get("frames", [])}
    raise AdapterFailure("failed", err["type"] or "error", err.get("frames", []))


def call_describe(spec: str | None, config_dir: str | None, timeout_s: float,
                  inbox_dirs: list[str] | tuple[str, ...] = ()) -> dict:
    """The child's raw describe response: ``{"ok": True, "result": {describe, spi_version, has_reference}}`` or
    ``{"ok": False, "load_error": code, "type": ..., "frames": [...]}``."""
    return run({"op": "describe", "adapter": spec, "config_dir": config_dir, "inbox_dirs": list(inbox_dirs)},
               timeout_s)


def call_warm(handle: Any, instruments_index: str | Path | None, timeout_s: float) -> dict[str, int]:
    """Inside ``pooled()``: every worker started, the adapter imported and the instruments index loaded, all at once
    under ``timeout_s`` (R22). Counts ``{"ok": n, "<failure type>": n}``; a worker that fails is started again by
    its next call, as before. Outside a pool (one child per call) there is nothing to warm."""
    pool = _pool
    if pool is None or handle.error:
        return {}
    counts: dict[str, int] = {}
    for r in pool.each(_request(handle, "warm", instruments_index), timeout_s):
        if isinstance(r, AdapterFailure):
            k = r.type or "error"
        else:
            k = "ok" if r.get("ok") else (r.get("load_error") or (r.get("error") or {}).get("type") or "error")
        counts[k] = counts.get(k, 0) + 1
    return counts


# a call's failure that says nothing about the files it was given: a timeout or a crash of the worker, worth one
# more try with more time and fewer files (R22); an exception of match itself is the adapter's answer, not retried
TRANSIENT = ("timeout", "runner_exit", "bad_runner_output")


def call_match(handle: Any, paths: list[str], instruments_index: str | Path | None,
               timeout_s: float, chunk_size: int = MATCH_CHUNK) -> dict[str, Any]:
    """``match`` for files, ``chunk_size`` per child call and ``timeout_s`` per call: per path the hit, None
    (not the adapter's) or ``{"_error": {"kind": "failed"|"timed_out", "type": ..., "frames": [...]}}``
    (status ``match_failed``). A timeout or crash of a call marks every path of its chunk."""
    out: dict[str, Any] = {}
    paths = list(paths)
    for i in range(0, len(paths), chunk_size):
        chunk = paths[i:i + chunk_size]
        try:
            out.update(_result(_call(_request(handle, "match", instruments_index, paths=chunk), timeout_s)))
        except AdapterFailure as f:
            out.update({p: {"_error": {"kind": f.kind, "type": f.type, "frames": f.frames}} for p in chunk})
    return out


def call_read(handle: Any, path: str, hit: dict, instruments_index: str | Path | None, timeout_s: float) -> Any:
    """The raw ``read`` result (checked by ``validate.check_read``); raises ``AdapterFailure``."""
    return _result(_call(_request(handle, "read", instruments_index, path=path, hit=hit), timeout_s))


def call_reference(handle: Any, instruments_index: str | Path | None, timeout_s: float,
                   worker: Worker | None = None) -> Any:
    """The raw ``reference(ctx)`` result; raises ``AdapterFailure``. With ``worker``, the call goes to that long-lived
    child (the settings forms keep one warm between checks) instead of the pool or a one-shot child."""
    req = _request(handle, "reference", instruments_index)
    return _result(worker.call(req, timeout_s) if worker is not None else _call(req, timeout_s))


if __name__ == "__main__":
    main()
