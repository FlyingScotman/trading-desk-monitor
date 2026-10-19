"""The adapter's child process (08 §2) and the parent-side calls into it.

The backend never imports adapter code itself: every call (``describe``, ``reference``, ``match``, ``read``)
runs in ``<python> -I`` with an environment stripped to ``DESK_PRIVATE_ADAPTER*``, ``PATH``, ``SYSTEMROOT``,
``TEMP`` and ``TMP``, so a hung or crashing adapter cannot stop the backend and never sees the store URL or the
hub token. ``-I`` also drops ``PYTHONPATH``; the folder holding ``desk`` is put on the child's ``sys.path`` by
the bootstrap code instead. One JSON request goes in on stdin, one JSON response comes out on stdout (the
adapter's own prints are sent to stderr), and the JSON encoding enforces "plain data only".

Exceptions cross as their type and (file, line, function) frames, never the message, which can hold a cell
value (08 §6.8). With ``DESK_PRIVATE_ADAPTER_DEBUG=1`` the child prints the full traceback to stderr and the
parent lets stderr through, for local debugging only.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import traceback
from dataclasses import dataclass
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


def _import(spec: str | None) -> Any:
    spec = spec or REFERENCE_ADAPTER
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
    ctx = Ctx(SPI_VERSION, req.get("config_dir"), Instruments.from_file(req.get("instruments_index")))
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


def run(request: dict, timeout_s: float) -> dict:
    """One child process, one request; raises ``AdapterFailure`` on timeout, crash or garbage output. Never
    hangs: on timeout the process tree is killed and the wait for it is bounded too."""
    boot = f"import sys; sys.path[:0] = [{_DESK_ROOT!r}]; from desk.inbox.runner import main; main()"
    debug = os.environ.get(DEBUG_ENV) == "1"
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen([sys.executable, "-I", "-c", boot], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=None if debug else subprocess.DEVNULL, env=child_env(), **group)
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


def _request(handle: Any, op: str, instruments_index: str | Path | None, **extra: Any) -> dict:
    if handle.error:
        raise AdapterFailure("failed", "adapter_not_loaded")
    return {"op": op, "adapter": handle.spec, "config_dir": handle.config_dir,
            "instruments_index": str(instruments_index) if instruments_index else None, **extra}


def _result(resp: dict) -> Any:
    if resp.get("ok"):
        return resp["result"]
    err = resp.get("error") or {"type": resp.get("load_error"), "frames": resp.get("frames", [])}
    raise AdapterFailure("failed", err["type"] or "error", err.get("frames", []))


def call_describe(spec: str | None, config_dir: str | None, timeout_s: float) -> dict:
    """The child's raw describe response: ``{"ok": True, "result": {describe, spi_version, has_reference}}`` or
    ``{"ok": False, "load_error": code, "type": ..., "frames": [...]}``."""
    return run({"op": "describe", "adapter": spec, "config_dir": config_dir}, timeout_s)


def call_match(handle: Any, paths: list[str], instruments_index: str | Path | None,
               timeout_s: float) -> dict[str, Any]:
    """``match`` for files, ``MATCH_CHUNK`` per child call and ``timeout_s`` per call: per path the hit, None
    (not the adapter's) or ``{"_error": {"kind": "failed"|"timed_out", "type": ..., "frames": [...]}}``
    (status ``match_failed``). A timeout or crash of a call marks every path of its chunk."""
    out: dict[str, Any] = {}
    paths = list(paths)
    for i in range(0, len(paths), MATCH_CHUNK):
        chunk = paths[i:i + MATCH_CHUNK]
        try:
            out.update(_result(run(_request(handle, "match", instruments_index, paths=chunk), timeout_s)))
        except AdapterFailure as f:
            out.update({p: {"_error": {"kind": f.kind, "type": f.type, "frames": f.frames}} for p in chunk})
    return out


def call_read(handle: Any, path: str, hit: dict, instruments_index: str | Path | None, timeout_s: float) -> Any:
    """The raw ``read`` result (checked by ``validate.check_read``); raises ``AdapterFailure``."""
    return _result(run(_request(handle, "read", instruments_index, path=path, hit=hit), timeout_s))


def call_reference(handle: Any, instruments_index: str | Path | None, timeout_s: float) -> Any:
    """The raw ``reference(ctx)`` result; raises ``AdapterFailure``."""
    return _result(run(_request(handle, "reference", instruments_index), timeout_s))


if __name__ == "__main__":
    main()
