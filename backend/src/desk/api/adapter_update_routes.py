"""Routes of the private adapter's updater (``desk.updater.adapter``) behind «Обновление адаптера» on the settings
page. Admin only, like the other update routes (``settings_routes.local_only``: this computer while sign-in is off;
the upload is a raw zip body like ``POST /api/v1/update/upload``). The token is set by
``PUT /api/v1/settings/github-token`` and never returned: these answers say only whether it is set.

* ``GET  /api/v1/update/adapter``                the state: the adapter in use and its source, the installed release,
                                                 the latest one on GitHub, the token's state, the job
* ``POST /api/v1/update/adapter/check``          ask GitHub now (with the token)
* ``POST /api/v1/update/adapter/install``        ``{"allow_older": bool, "via": "browser"?}``: start the job (202)
* ``POST /api/v1/update/adapter/cancel``         stop the job
* ``POST /api/v1/update/adapter/rollback``       ``{"to": "previous" | "bundle"}``
* ``POST /api/v1/update/adapter/upload``         the body is a ``tda-adapter.zip`` (``?allow_older=1``): installed at once
* ``GET  /api/v1/update/adapter/downloads``      the ``tda-adapter*.zip`` of the downloads folder, checked
* ``POST /api/v1/update/adapter/install-found``  ``{"file": "<name>", "sha256": "...", "allow_older": bool}``

A refused install answers 409 (``details.needs_confirm`` true: an older or the same version, which the page installs
only after the owner confirms) or 422.
"""

from __future__ import annotations

import os
import uuid
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, Body, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..settings import Settings, SettingsError, load
from ..updater import adapter
from .app import ApiError
from .settings_routes import local_only
from .update_routes import local_zip

JsonBody = Annotated[dict[str, Any], Body()]
router = APIRouter()


def _st() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None


def _refused(e: adapter.AdapterUpdateError) -> ApiError:
    if e.needs_confirm:
        return ApiError(409, "invalid_parameter", str(e),
                        {"needs_confirm": True, "version": e.version, "compare": e.compare})
    return ApiError(422, "invalid_parameter", str(e))


@router.get("/api/v1/update/adapter", dependencies=[Depends(local_only)])
def adapter_state() -> dict[str, Any]:
    return adapter.status(_st())


@router.post("/api/v1/update/adapter/check", dependencies=[Depends(local_only)])
def adapter_check() -> dict[str, Any]:
    st = _st()
    adapter.check(st)
    return adapter.status(st)


@router.post("/api/v1/update/adapter/install", dependencies=[Depends(local_only)], status_code=202)
def adapter_install(body: JsonBody) -> dict[str, Any]:
    via = body.get("via")
    if via not in (None, "browser", "server"):
        raise ApiError(422, "invalid_parameter", "via: browser или server")
    try:
        job = adapter.start_install(_st(), allow_older=body.get("allow_older") is True, via=via)
    except adapter.AdapterUpdateError as e:
        raise _refused(e) from None
    return {"job": job}


@router.post("/api/v1/update/adapter/cancel", dependencies=[Depends(local_only)])
def adapter_cancel() -> dict[str, Any]:
    adapter.cancel()
    return adapter.status(_st())


@router.post("/api/v1/update/adapter/rollback", dependencies=[Depends(local_only)])
def adapter_rollback(body: JsonBody) -> dict[str, Any]:
    to = body.get("to")
    if to not in ("previous", "bundle"):
        raise ApiError(422, "invalid_parameter", "to: previous или bundle")
    st = _st()
    try:
        out = adapter.rollback(st, to)
    except adapter.AdapterUpdateError as e:
        raise _refused(e) from None
    return {**out, "state": adapter.status(_st())}


@router.post("/api/v1/update/adapter/upload", dependencies=[Depends(local_zip)])
async def adapter_upload(request: Request, allow_older: bool = False) -> dict[str, Any]:
    """The request body is the zip; checked and installed at once (the upload is the owner's click)."""
    limit = adapter.MAX_ZIP_BYTES
    if int(request.headers.get("content-length") or 0) > limit:
        raise ApiError(413, "invalid_parameter", f"Архив больше {limit >> 20} МБ.")
    st = _st()
    folder = adapter.uploads_dir(st.home)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"upload-{uuid.uuid4().hex[:12]}-{adapter.ZIP_NAME}"
    size = 0
    try:
        async with await anyio.open_file(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise ApiError(413, "invalid_parameter", f"Архив больше {limit >> 20} МБ.")
                await f.write(chunk)
        try:
            out = await run_in_threadpool(adapter.install_zip, st, path, source="upload", allow_older=allow_older)
        except adapter.AdapterUpdateError as e:
            raise _refused(e) from None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return {**out, "state": adapter.status(_st())}


@router.get("/api/v1/update/adapter/downloads", dependencies=[Depends(local_only)])
def adapter_downloads() -> dict[str, Any]:
    return adapter.scan(_st())


@router.post("/api/v1/update/adapter/install-found", dependencies=[Depends(local_only)])
def adapter_install_found(body: JsonBody) -> dict[str, Any]:
    name, sha = body.get("file"), body.get("sha256")
    if not isinstance(name, str) or not isinstance(sha, str):
        raise ApiError(422, "invalid_parameter", "file и sha256 — из списка «Найти в «Загрузках»»")
    st = _st()
    try:
        out = adapter.install_found(st, name, sha, allow_older=body.get("allow_older") is True)
    except adapter.AdapterUpdateError as e:
        raise _refused(e) from None
    return {**out, "state": adapter.status(_st())}
