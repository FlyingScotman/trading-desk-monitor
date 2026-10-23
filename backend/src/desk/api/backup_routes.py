"""Backup routes behind the settings page's «Резервные копии» (``desk.backup``, ``prototypes/desk-backup.js``).

* ``GET /api/v1/backup/status``: the settings, the folder and whether it shares the store's disk, the backups, the
  last attempt, the last restore drill and the job running now, if any.
* ``POST /api/v1/backup/run``: a backup now (kind ``manual``), in a background thread; 202, then the status shows it.
* ``POST /api/v1/backup/check`` ``{"file": NAME}`` (default the newest): the restore drill into a scratch database,
  in a background thread. The live data is never touched here: replacing it needs the server stopped
  (``desk restore FILE --replace``, START_HERE section 7а), which a page served by that server cannot do.

Access as the other settings routes (``local_only``: this computer while sign-in is off, else an admin). Each start
is recorded in the audit log (``backup_run``, ``backup_check``) with its outcome.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request

from .. import backup, store
from ..settings import SettingsError, load
from .app import ApiError
from .auth import _audit_quietly, actor, client_ip
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])

_lock = threading.Lock()
_job: dict[str, Any] | None = None  # the running or the last finished job of this server


def _settings():
    try:
        return load()
    except SettingsError as e:
        raise ApiError(500, "internal", f"settings: {e}") from None


def current_job() -> dict[str, Any] | None:
    with _lock:
        return dict(_job) if _job else None


def _start(kind: str, fn, store_url: str, who: str, ip: str | None, target: str) -> dict[str, Any]:
    global _job
    with _lock:
        if _job and _job["state"] == "running":
            raise ApiError(409, "invalid_parameter", f"already running: {_job['kind']}")
        _job = {"kind": kind, "state": "running", "file": target, "started": datetime.now().isoformat(timespec="seconds"),
                "step": "", "result": None, "error": None}
        job = _job

    def step(line: str) -> None:
        with _lock:
            job["step"] = line

    def run() -> None:
        ok, detail = False, {}
        try:
            result = fn(step)
            with _lock:
                job.update(state="done", result=result)
            ok = bool(result.get("ok", True))
            detail = {k: result.get(k) for k in ("file", "size", "rows", "problems") if result.get(k) is not None}
        except Exception as e:  # noqa: BLE001 - shown on the page
            with _lock:
                job.update(state="error", error=f"{e}" if isinstance(e, backup.BackupError) else f"{type(e).__name__}: {e}")
            detail = {"error": job["error"][:300]}
        finally:
            with _lock:
                job["finished"] = datetime.now().isoformat(timespec="seconds")
            _audit_quietly(store_url, who, f"backup_{kind}", target, ok, ip, detail)

    threading.Thread(target=run, name=f"desk-backup-{kind}", daemon=True).start()
    return dict(job)


@router.get("/api/v1/backup/status")
def backup_status() -> dict[str, Any]:
    return backup.status(_settings(), current_job())


@router.post("/api/v1/backup/run", status_code=202)
def backup_run(request: Request) -> dict[str, Any]:
    st = _settings()
    url = request.app.state.store_url or store.resolve_url(st)

    def make(_step) -> dict[str, Any]:
        _step("pg_dump ...")
        b = backup.create(st, url, "manual")
        return {"ok": True, "file": b.name, "size": b.size, "rows": sum((b.meta.get("tables") or {}).values())}

    return _start("run", make, url, actor(request), client_ip(request), "manual")


@router.post("/api/v1/backup/check", status_code=202)
def backup_check(request: Request, body: Annotated[dict[str, Any] | None, Body()] = None) -> dict[str, Any]:
    st = _settings()
    url = request.app.state.store_url or store.resolve_url(st)
    name = (body or {}).get("file")
    if name is not None and (not isinstance(name, str) or backup.parse_name(name) is None):
        raise ApiError(422, "invalid_parameter", "file must be a backup file name of the backup folder")
    try:
        dump = backup.find(st, name)
    except backup.BackupError as e:
        raise ApiError(404, "not_found", str(e)) from None

    def check(step) -> dict[str, Any]:
        return backup.restore_check(st, url, dump, out=step)

    return _start("check", check, url, actor(request), client_ip(request), dump.name)
