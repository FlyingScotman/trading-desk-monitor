"""Routes of the GitHub updater (``desk.updater.service``) behind «Обновление программы» and the navigation bar's
«новая версия» badge. Admin only, like the other update routes (``settings_routes.check_local``).

* ``GET  /api/v1/update/remote``                 the state: installed and latest version, what would be installed,
                                                 the job, the diagnostics; starts a background check when due
* ``POST /api/v1/update/remote/check``           ask GitHub now through the server's layers
* ``POST /api/v1/update/remote/browser-release`` the page's own GitHub API answer (``{"release": ...}``,
                                                 ``{"none": true}`` or ``{"error": "..."}``)
* ``POST /api/v1/update/remote/install``         ``{"allow_downgrade": bool, "via": "browser"?}``: start the job
* ``POST /api/v1/update/remote/cancel``          stop the job (a browser download it waits for, say)
* ``POST /api/v1/update/remote/diagnose``        every layer against GitHub's hosts
* ``POST /api/v1/update/remote/browser-diag``    what the page reached from the browser
* ``GET  /api/v1/update/remote/downloads``       the DeskMonitor zips of the downloads folder, verified or not
* ``POST /api/v1/update/remote/install-found``   ``{"file": "<name in the downloads folder>", "sha256": "..."}``
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request

from .. import update
from ..settings import Settings, SettingsError, load
from ..updater import downloads, service
from ..updater.release import ReleaseError
from .app import ApiError
from .settings_routes import local_only

JsonBody = Annotated[dict[str, Any], Body()]
router = APIRouter(dependencies=[Depends(local_only)])


def _st() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None


def _restart(request: Request):
    return getattr(request.app.state, "request_restart", None)


def _unavailable(request: Request) -> str | None:
    if _restart(request) is None:
        return update.unavailable() or "Этот сервер не умеет перезапускаться для обновления."
    return update.unavailable()


def _status(st: Settings, request: Request) -> dict[str, Any]:
    why = _unavailable(request)
    return service.status(st, why is None, why)


@router.get("/api/v1/update/remote")
def remote(request: Request) -> dict[str, Any]:
    st = _st()
    service.maybe_auto_check(st)
    return _status(st, request)


@router.post("/api/v1/update/remote/check")
def remote_check(request: Request) -> dict[str, Any]:
    st = _st()
    service.check(st)
    return _status(st, request)


@router.post("/api/v1/update/remote/browser-release")
def browser_release(body: JsonBody, request: Request) -> dict[str, Any]:
    st = _st()
    try:
        service.take_browser_release(st, body)
    except ReleaseError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    return _status(st, request)


@router.post("/api/v1/update/remote/install", status_code=202)
def remote_install(body: JsonBody, request: Request) -> dict[str, Any]:
    st = _st()
    via = body.get("via")
    if via not in (None, "browser", "server"):
        raise ApiError(422, "invalid_parameter", "via: browser или server")
    try:
        job = service.start_install(st, allow_older=body.get("allow_downgrade") is True, via=via,
                                    restart=_restart(request), unavailable=_unavailable(request))
    except update.UpdateError as e:
        raise ApiError(409, "invalid_parameter", str(e)) from None
    return {"job": job}


@router.post("/api/v1/update/remote/cancel")
def remote_cancel(request: Request) -> dict[str, Any]:
    service.cancel()
    return _status(_st(), request)


@router.post("/api/v1/update/remote/diagnose")
def remote_diagnose(request: Request) -> dict[str, Any]:
    st = _st()
    if service.diagnose(st)["working"] and st.update_transport != "browser":
        service.check(st)  # a layer gets through now: the version from the server, the download by it too
    return _status(st, request)


@router.post("/api/v1/update/remote/browser-diag")
def browser_diag(body: JsonBody, request: Request) -> dict[str, Any]:
    st = _st()
    service.take_browser_diag(st, body)
    return _status(st, request)


@router.get("/api/v1/update/remote/downloads")
def remote_downloads() -> dict[str, Any]:
    return service.scan(_st())


@router.post("/api/v1/update/remote/install-found", status_code=202)
def install_found(body: JsonBody, request: Request) -> dict[str, Any]:
    st = _st()
    name, sha = body.get("file"), body.get("sha256")
    if not isinstance(name, str) or Path(name).name != name or "\\" in name or not name.lower().endswith(".zip"):
        raise ApiError(422, "invalid_parameter", "file: имя zip-файла в папке загрузок")
    if not isinstance(sha, str):
        raise ApiError(422, "invalid_parameter", "sha256: контрольная сумма из списка «Найти в Загрузках»")
    path = downloads.downloads_dir(st.update_downloads_dir) / name
    if not path.is_file():
        raise ApiError(422, "invalid_parameter", f"Файла {name} нет в папке загрузок.")
    item = next((i for i in downloads.scan_folder(path.parent, service.known_sums(st)) if i["file"] == name), None)
    if item is None or not item["verified"] or item["sha256"] != sha:
        raise ApiError(422, "invalid_parameter", f"{name}: контрольная сумма не подтверждена (нет SHA256SUMS?).")
    if update.compare(item["version"], update.current_version()) == "older" and body.get("allow_downgrade") is not True:
        raise ApiError(409, "invalid_parameter", "Эта версия старее установленной: подтвердите (allow_downgrade).")
    try:
        return service.install_found(st, path, sha, _restart(request), _unavailable(request))
    except update.UpdateError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
