"""The settings page's adapter tools (TDM#421): the owner never runs a command line, so what ``desk adapter check`` and
the adapter's own tools do runs from the page, in the background, through the same child processes.

* **The kit** (08 §8, ``desk.inbox.conformance``): ``start_check`` runs ``python -m desk.inbox.conformance --json`` over
  the chosen folders in a child process of its own (it starts the adapter's children itself), killed with its tree
  after ``adapter_kit_timeout_s``. The result is the kit's JSON and the summary the CLI prints (codes, counts and
  shares, never cell values), kept until the next run, also across a restart (``<home>/adapter-jobs/check.json``).
* **status(ctx)** (08 §3, optional): ``status`` calls it in a one-shot child under ``adapter_status_timeout_s`` and
  checks the panels (``desk.inbox.tools.check_status``). Synchronous: the API route runs in a worker thread.
* **Actions** (08 §3, §4, optional): ``start_action`` runs ``run_action(id, params, ctx)`` in a one-shot child under
  ``adapter_action_timeout_s``; when it says ``ok``, its files (if any, or none for an action declaring ``output_dir``)
  replace that action's previous ones in ``<home>/adapter-output/<id>/`` (``desk.inbox.tools.write_outputs``), and an inbox scan
  follows at once (the autopilot is woken too, so the new inputs are valued), so the files go through ``match`` /
  ``read`` like any report. ``action`` adds each file's inbox outcome from the store when asked.

One kit run and one action at a time (a second start is refused, ``Busy``); a job is a daemon thread, so no request
waits for it. Every call passes ``ctx.inbox_dirs`` = the settings' inbox folders, never the output folder: the adapter
does not read its own outputs as inputs. ``status`` and ``run_action`` also get ``ctx.adapter_output_dir`` (08 §7),
that folder as an absolute path, so the adapter can tell what an action already wrote. Nothing here returns an exception message (it can hold a cell value): the
adapter's failures are its exception type, ours their class name.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .inbox import conformance, runner, scan, tools
from .inbox.spi import AdapterFailure
from .settings import Settings, adapter_config_dir, atomic_write_text

log = logging.getLogger(__name__)

JOBS_DIR = "adapter-jobs"
MAX_DIRS = 20
_DESK_ROOT = str(Path(__file__).resolve().parents[1])  # the folder holding the desk package
_lock = threading.Lock()
_jobs: dict[str, dict] = {}       # "check", "action:<id>" -> the job, newest of its kind
_running: dict[str, str] = {}     # "check" | "action" -> the job key running


class Busy(Exception):
    """A job of that kind is already running."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _file(home: Path, key: str) -> Path:
    return home / JOBS_DIR / (key.replace(":", "-") + ".json")


def _save(home: Path, key: str, job: dict) -> None:
    try:
        atomic_write_text(_file(home, key), json.dumps(job, ensure_ascii=False, indent=1) + "\n")
    except OSError as e:  # the result stays in memory; only a restart loses it
        log.warning("adapter job %s: not saved (%s)", key, type(e).__name__)


def job(home: Path, key: str) -> dict | None:
    """The newest job ``key``: the running one, else the last result (from memory, else from its file)."""
    with _lock:
        j = _jobs.get(key)
        if j is not None:
            return dict(j)
    try:
        j = json.loads(_file(home, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if j.get("state") == "running":  # the server stopped during the run
        j.update(state="failed", error="the server stopped during the run", error_code="interrupted")
    return j


def _start(home: Path, kind: str, key: str, request: dict, work: Callable[[dict], None]) -> dict:
    with _lock:
        if kind in _running:
            raise Busy(_running[kind])
        j = {"key": key, "state": "running", "started_at": _now(), "finished_at": None, "request": request,
             "error": None, "error_code": None}
        _jobs[key], _running[kind] = j, key

    def body() -> None:
        t0 = time.monotonic()
        try:
            work(j)
        except Exception as e:
            log.exception("adapter job %s failed", key)
            j.update(state="failed", error=f"internal error ({type(e).__name__})", error_code="internal")
        finally:
            with _lock:
                if j["state"] == "running":
                    j["state"] = "done"
                j["finished_at"], j["seconds"] = _now(), round(time.monotonic() - t0, 1)
                _running.pop(kind, None)
                done = dict(j)
            _save(home, key, done)

    threading.Thread(target=body, name=f"desk-adapter-{kind}", daemon=True).start()
    return dict(j)


def running(kind: str) -> str | None:
    with _lock:
        return _running.get(kind)


def reset() -> None:
    """Forget the jobs in memory (tests). Running threads finish on their own."""
    with _lock:
        _jobs.clear()
        _running.clear()


def _index(st: Settings) -> str | None:
    """The scan's instruments index (``ctx.instruments``), when there is one; else every lookup is None."""
    p = Path(st.inbox_archive) / "instruments.json"
    return str(p) if p.is_file() else None


# ---- the kit -----------------------------------------------------------------------------------------------------


def check_options(st: Settings) -> list[dict]:
    """The folders the page offers to check: the inbox folders and the adapter's output folder."""
    out = [{"path": str(d), "label": None, "kind": "inbox", "exists": Path(str(d).removesuffix("**").rstrip("\\/")).is_dir()}
           for d in st.inbox_dirs]
    o = st.adapter_output_dir
    out.append({"path": str(o / "**"), "label": "папка результатов адаптера", "kind": "adapter_output",
                "exists": o.is_dir()})
    return out


def check_dirs(dirs: Any) -> list[str]:
    """The folders of a kit request: existing folders (a trailing ``**``: with subfolders); ValueError otherwise."""
    if not isinstance(dirs, list) or not dirs or len(dirs) > MAX_DIRS:
        raise ValueError(f"dirs: a list of 1 to {MAX_DIRS} folders")
    out = []
    for d in dirs:
        if not isinstance(d, str) or not d.strip() or "\0" in d:
            raise ValueError("dirs: each a folder path")
        d = d.strip()
        if not Path(d.rstrip("\\/").removesuffix("**").rstrip("\\/") or d).expanduser().is_dir():
            raise ValueError(f"not a folder: {d}")
        if d not in out:
            out.append(d)
    return out


def kit_argv(st: Settings, dirs: list[str], stub_ctx: bool) -> list[str]:
    """The kit's command line, as ``desk adapter check`` runs it with these settings. A path goes in one argument,
    ``--dir=<path>``: a folder whose name starts with ``-`` is never read as a flag."""
    cfg = adapter_config_dir(st)
    argv = [f"--dir={d}" for d in dirs] + ["--json", f"--snapshots={st.snapshot_cache}",
                                           "--match-timeout", f"{st.adapter_match_timeout_s:g}",
                                           "--read-timeout", f"{st.adapter_read_timeout_s:g}"]
    argv += [f"--inbox-dir={d}" for d in st.inbox_dirs] or ["--inbox-dir="]
    if st.private_adapter:
        argv.append(f"--adapter={st.private_adapter}")
    if cfg:
        argv.append(f"--config={cfg}")
    if st.private_adapter_sha256:
        argv.append(f"--sha256={st.private_adapter_sha256}")
    if stub_ctx:
        argv.append("--stub-ctx")
    return argv


def _run_kit(argv: list[str], timeout_s: float) -> tuple[dict | None, str | None, str | None]:
    """(the kit's JSON, error, error code) of one child run; the child and its tree are killed on the timeout."""
    boot = (f"import sys; sys.path[:0] = [{_DESK_ROOT!r}]; from desk.inbox.conformance import main; "
            "sys.exit(main(sys.argv[1:]))")
    debug = os.environ.get(runner.DEBUG_ENV) == "1"
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen([sys.executable, "-I", "-c", boot, *argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=None if debug else subprocess.DEVNULL, env=runner.child_env(), **group)
    try:
        out, _ = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        runner._kill_tree(p)
        try:
            p.communicate(timeout=runner.KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            if p.stdout:
                p.stdout.close()
        return None, f"the check took longer than {timeout_s:g} s (adapter_kit_timeout_s)", "timeout"
    try:
        return json.loads(out), None, None
    except ValueError:
        return None, f"the kit ended without a result (exit code {p.returncode})", "failed"


def start_check(st: Settings, dirs: list[str], stub_ctx: bool = False) -> dict:
    """The kit over ``dirs`` in the background; ``Busy`` while one runs."""
    argv = kit_argv(st, dirs, stub_ctx)

    def work(j: dict) -> None:
        rep, err, code = _run_kit(argv, st.adapter_kit_timeout_s)
        if rep is None:
            j.update(state="timeout" if code == "timeout" else "failed", error=err, error_code=code)
            return
        j.update(report=rep, text=conformance.summary_text(rep), ok=rep.get("ok"))

    return _start(st.home, "check", "check", {"dirs": dirs, "stub_ctx": stub_ctx}, work)


# ---- status(ctx) -------------------------------------------------------------------------------------------------


def status(st: Settings) -> dict:
    """The adapter's ``status(ctx)`` for the page: ``available`` false without the function (the panel is hidden)."""
    h = scan.adapter(st)
    out: dict[str, Any] = {"available": h.ok and "status" in h.optional, "loaded": h.ok, "panels": [], "issues": [],
                           "problems": [], "error": None, "error_code": None, "timeout_s": st.adapter_status_timeout_s,
                           "at": _now()}
    if not out["available"]:
        return out
    t0 = time.monotonic()
    try:
        out.update(tools.check_status(runner.call_status(h, _index(st), st.adapter_status_timeout_s,
                                                         st.adapter_output_dir)))
    except AdapterFailure as f:
        out.update(error=f"status() {'did not answer in time' if f.kind == 'timed_out' else 'failed'}: {f.type}",
                   error_code="timeout" if f.kind == "timed_out" else "failed")
    except tools.BadResult as e:
        out.update(error=str(e), error_code="bad_result")
    out["seconds"] = round(time.monotonic() - t0, 2)
    return out


# ---- actions -----------------------------------------------------------------------------------------------------


def start_action(st: Settings, decl: dict, params: dict, store_url: str | None, autopilot: Any = None) -> dict:
    """``run_action`` in the background, then its files and an inbox scan (module doc); ``Busy`` while one runs."""
    h = scan.adapter(st)
    aid = decl["id"]

    def work(j: dict) -> None:
        try:
            raw = runner.call_action(h, aid, params, _index(st), st.adapter_action_timeout_s, st.adapter_output_dir)
        except AdapterFailure as f:
            timed = f.kind == "timed_out"
            j.update(state="timeout" if timed else "failed", error_code="timeout" if timed else "failed",
                     error=f"run_action() {'did not finish in time' if timed else 'failed'}: {f.type}")
            return
        try:
            res = tools.check_action_result(raw, decl)
        except tools.BadResult as e:
            j.update(state="failed", error=str(e), error_code="bad_result")
            return
        files = res.pop("files")
        j.update(result=res, files=[], written=False)
        if not res["ok"] or not (files or decl["output_dir"]):
            return  # nothing to write; ok false keeps the action's previous files
        try:
            paths = tools.write_outputs(st.adapter_output_dir, aid, files)
        except OSError as e:
            j.update(state="failed", error=f"the files were not written ({type(e).__name__})", error_code="write")
            return
        j.update(written=True, output_dir=str(st.adapter_output_dir / aid),
                 files=[{"name": p.name, "path": str(p), "bytes": p.stat().st_size} for p in paths])
        j["scan"] = _scan(st, store_url, autopilot) if paths else None

    return _start(st.home, "action", f"action:{aid}", {"action": aid, "params": params}, work)


def _scan(st: Settings, store_url: str | None, autopilot: Any) -> dict:
    """An inbox scan now (the files are new), then the autopilot woken so the new inputs are valued."""
    out: dict[str, Any] = {"ran": False, "busy": False, "text": None, "error": None}
    if store_url:
        import psycopg

        try:
            with psycopg.connect(store_url, autocommit=True, connect_timeout=5) as con:
                res = scan.scan(con, st)
            out.update(ran=not res.busy, busy=res.busy, text=res.text())
        except Exception as e:  # noqa: BLE001 - the files are written; the autopilot's scan takes them later
            out["error"] = f"the scan did not run ({type(e).__name__}); the next inbox scan reads the files"
    if autopilot is not None:
        try:
            autopilot.trigger()
            out["autopilot"] = True
        except Exception:  # noqa: BLE001
            out["autopilot"] = False
    return out


_OUTCOME = """
SELECT s.path, s.size_bytes, s.mtime, l.status, l.source, l.report_date
FROM inbox_sighting s
JOIN LATERAL (SELECT status, source, report_date FROM inbox_file_status f WHERE f.sha256 = s.sha256
              ORDER BY status_id DESC LIMIT 1) l ON true
WHERE s.path = ANY(%s)
UNION ALL
SELECT path, size_bytes, mtime, status, source, report_date FROM inbox_unread WHERE path = ANY(%s)"""


def outcomes(store_url: str | None, paths: list[str]) -> tuple[dict[str, dict], str | None]:
    """The inbox outcome of each file as it is now on disk (its path, size and modification time): the latest status,
    source and report date of its content (``inbox_unread`` for a file ``match`` declined), or nothing yet; (``{}``,
    why) without the store."""
    if not paths:
        return {}, None
    if not store_url:
        return {}, "no store"
    import psycopg

    now: dict[str, tuple[int, float]] = {}
    for p in paths:
        try:
            st = os.stat(p)
            now[p] = (st.st_size, st.st_mtime)
        except OSError:
            continue
    try:
        with psycopg.connect(store_url, autocommit=True, connect_timeout=3,
                             options="-c default_transaction_read_only=on") as con:
            rows = con.execute(_OUTCOME, (paths, paths)).fetchall()
    except psycopg.Error as e:
        return {}, f"store unavailable ({type(e).__name__})"
    out: dict[str, dict] = {}
    for p, size, mtime, status, source, rd in rows:
        if p in now and now[p][0] == size and abs(now[p][1] - mtime.timestamp()) < 1.0:
            out[p] = {"status": status, "source": source, "report_date": rd.isoformat() if rd else None}
    return out, None
