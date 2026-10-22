"""Update routes behind the settings page's «Обновление программы» (``desk.update``): the zips of the settings'
``update_dir`` and those uploaded from the browser, a check of one, and its install, which stages the update and
asks ``desk serve`` to exit for the launcher (``app.state.request_restart``, set by ``serve``). In a checkout, or a
desk serve not started by start.bat, the list and the check work and the install answers 409.

Admin only, like the settings routes (``settings_routes.check_local``: this computer while sign-in is off); the upload is a raw zip body
(``Content-Type: application/zip``), which a cross-site form cannot send either.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import update
from ..settings import Settings, SettingsError, desk_home, load
from .app import ApiError
from .settings_routes import check_local, local_only

ZIP_TYPES = ("application/zip", "application/x-zip-compressed", "application/octet-stream")
JsonBody = Annotated[dict[str, Any], Body()]
_installing = update.INSTALL_LOCK


def local_zip(request: Request) -> None:
    check_local(request, ZIP_TYPES)


router = APIRouter()


def _settings() -> tuple[Settings | None, Path, str | None]:
    try:
        st = load()
        return st, st.home, None
    except SettingsError as e:
        return None, desk_home(), str(e)


def _unavailable(request: Request) -> str | None:
    if getattr(request.app.state, "request_restart", None) is None:
        return update.unavailable() or "Этот сервер не умеет перезапускаться для обновления."
    return update.unavailable()


def _bad(e: update.UpdateError) -> ApiError:
    return ApiError(422, "invalid_parameter", str(e))


@router.get("/api/v1/update", dependencies=[Depends(local_only)])
def get_update(request: Request) -> dict[str, Any]:
    """The installed version, whether this server can install, the zips (``source`` ``dir`` or ``upload``), the
    version a rollback returns to (``rollback``) and the launcher's ``pending`` / ``applying`` / ``last``
    (desk.update.state)."""
    st, home, error = _settings()
    root, current = update.bundle_root(), update.current_version()
    items: list[dict[str, Any]] = []
    dir_error = None
    if st is not None and st.update_dir is not None:
        try:
            items += [{**i, "source": "dir"} for i in update.list_zips(st.update_dir, root, current)]
        except update.UpdateError as e:
            dir_error = str(e)
    if update.uploads_dir(home).is_dir():
        items += [{**i, "source": "upload"} for i in update.list_zips(update.uploads_dir(home), root, current)]
    reason = _unavailable(request)
    point = update.rollback_point(root, home) if root is not None else None
    return {"current_version": current, "bundle_root": str(root) if root else None, "can_install": reason is None,
            "rollback": {"version": point["version"]} if point else None,
            "unavailable": reason, "update_dir": str(st.update_dir) if st and st.update_dir else None,
            "update_dir_error": dir_error, "settings_error": error, "items": items,
            "max_zip_mb": update.MAX_ZIP_BYTES >> 20, **update.state(home)}


def _find(body: dict[str, Any]) -> tuple[Path, Path, str]:
    st, home, error = _settings()
    if error:
        raise ApiError(422, "invalid_parameter", error)
    source = body.get("source")
    try:
        return update.find_zip(str(source), body.get("name"), st.update_dir if st else None, home), home, str(source)
    except update.UpdateError as e:
        raise _bad(e) from None


@router.post("/api/v1/update/check", dependencies=[Depends(local_only)])
def check(body: JsonBody) -> dict[str, Any]:
    """Body ``{"source": "dir"|"upload", "name": "<zip>"}``: the zip as the list shows it (``error`` None: fits)."""
    path, _, source = _find(body)
    return {**update.describe(path, update.bundle_root(), update.current_version()), "source": source}


@router.post("/api/v1/update/install", dependencies=[Depends(local_only)], status_code=202)
def install(body: JsonBody, request: Request):
    """Check and stage the zip, then ask the launcher to install it: desk serve exits once the response is sent
    (and the autopilot's cycle, if one runs, is over); the page polls ``GET /api/v1/update`` for the new version.
    A version older than the installed one only with ``"allow_downgrade": true``."""
    reason = _unavailable(request)
    if reason:
        raise ApiError(409, "invalid_parameter", reason)
    path, home, _ = _find(body)
    if body.get("allow_downgrade") is not True and \
            update.describe(path, None, update.current_version())["compare"] == "older":
        raise ApiError(409, "invalid_parameter", "Эта версия старее установленной: подтвердите установку "
                                                 "старой версии (allow_downgrade).")
    if not _installing.acquire(blocking=False):
        raise ApiError(409, "invalid_parameter", "Обновление уже готовится.")
    try:
        if update.state(home)["pending"]:
            raise ApiError(409, "invalid_parameter", "Обновление уже запрошено: дождитесь перезапуска.")
        root = update.bundle_root()
        assert root is not None
        try:
            staged = update.stage(path, root, home)
        except update.UpdateError as e:
            raise _bad(e) from None
        update.request(staged, home)
        request.app.state.request_restart()
    finally:
        _installing.release()
    return JSONResponse({"version": staged["version"], "from_version": staged["from_version"], "kind": staged["kind"],
                         "files": len(staged["files"]), "delete": len(staged["delete"]), "restarting": True},
                        status_code=202)


@router.post("/api/v1/update/rollback", dependencies=[Depends(local_only)], status_code=202)
def rollback(request: Request):
    """Stage the return to the previous version from the launcher's backup (``update.stage_rollback``) and ask
    the launcher to install it, as ``install`` does."""
    reason = _unavailable(request)
    if reason:
        raise ApiError(409, "invalid_parameter", reason)
    _, home, _ = _settings()
    if not _installing.acquire(blocking=False):
        raise ApiError(409, "invalid_parameter", "Обновление уже готовится.")
    try:
        if update.state(home)["pending"]:
            raise ApiError(409, "invalid_parameter", "Обновление уже запрошено: дождитесь перезапуска.")
        root = update.bundle_root()
        assert root is not None
        try:
            staged = update.stage_rollback(root, home)
        except update.UpdateError as e:
            raise _bad(e) from None
        update.request(staged, home)
        request.app.state.request_restart()
    finally:
        _installing.release()
    return JSONResponse({"version": staged["version"], "from_version": staged["from_version"], "kind": "rollback",
                         "files": len(staged["files"]), "delete": len(staged["delete"]), "restarting": True},
                        status_code=202)


@router.post("/api/v1/update/upload", dependencies=[Depends(local_zip)])
async def upload(request: Request, name: str) -> dict[str, Any]:
    """The request body is the zip (``?name=`` its file name); kept in ``<home>/updates/uploads/`` when it is an
    update zip at all (422 and dropped otherwise), with the newest few other uploads."""
    try:
        fname = update.upload_name(name)
    except update.UpdateError as e:
        raise _bad(e) from None
    limit = update.MAX_ZIP_BYTES
    if int(request.headers.get("content-length") or 0) > limit:
        raise ApiError(413, "invalid_parameter", f"Архив больше {limit >> 20} МБ.")
    _, home, _ = _settings()
    folder = update.uploads_dir(home)
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / f".{fname}.part"
    size = 0
    try:
        async with await anyio.open_file(part, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise ApiError(413, "invalid_parameter", f"Архив больше {limit >> 20} МБ.")
                await f.write(chunk)
        path = folder / fname
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)

    def check_it() -> dict[str, Any]:
        try:
            with update.open_zip(path) as z:
                update.read_contents(z)
        except update.UpdateError:
            path.unlink(missing_ok=True)
            raise
        update.prune_uploads(home)
        return update.describe(path, update.bundle_root(), update.current_version())

    try:
        item = await run_in_threadpool(check_it)
    except update.UpdateError as e:
        raise _bad(e) from None
    return {**item, "source": "upload"}
