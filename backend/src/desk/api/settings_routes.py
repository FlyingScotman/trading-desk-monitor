"""Settings API (I5) behind the settings page ``/settings.html``: ``<home>/settings.json``, the hub token file,
a hub check, and the private adapter's status and declared config files (08 §4 ``config_files``).

Every route re-reads the settings (``desk.settings.load``), so an edit applies to the running server. The adapter
is only ever called through the child-process runner (``desk.inbox.loader.load_adapter``), never imported here.

shortcut: no authentication, like the rest of the API (05 §6). These routes therefore answer loopback clients
only (``desk serve`` binds 127.0.0.1), with a loopback ``Host`` (no DNS rebinding) and, if the browser sends one,
a loopback ``Origin``; a write needs ``Content-Type: application/json``, which a cross-site form cannot send.
The token is accepted, stored and reported as set or not, never returned.
"""

from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Request

from ..inbox import runner, scan
from ..inbox.loader import CONFIG_NAME_RE, check_config_files, load_adapter
from ..marketdata import hubhistory
from ..marketdata.hub import hub_check
from ..settings import (
    READ_ONLY_KEYS,
    Settings,
    SettingsError,
    adapter_config_dir,
    atomic_write_text,
    desk_home,
    env_overrides,
    load,
    mask_proxy,
    mask_url,
    read_settings_json,
    update_settings_json,
    write_hub_token,
    write_proxy_password,
)
from .app import ApiError

MAX_CONFIG_BYTES = 2 * 1024 * 1024
JsonBody = Annotated[dict[str, Any], Body()]
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.strip("[]").lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def local_only(request: Request) -> None:
    """Refuse anything but a same-machine browser or tool (see the module doc)."""
    check_local(request)


def check_local(request: Request, write_types: tuple[str, ...] = ("application/json",)) -> None:
    """``local_only`` with the content types a write may send (none of them one a cross-site form can send)."""
    client = request.client.host if request.client else None
    if not _loopback(client):
        raise ApiError(403, "invalid_parameter", "settings are available from this computer only (127.0.0.1)")
    if not _loopback(request.url.hostname):
        raise ApiError(403, "invalid_parameter", "open the settings page as http://127.0.0.1:<port>/settings.html")
    origin = request.headers.get("origin")
    if origin and origin != "null" and not _loopback(urlsplit(origin).hostname):
        raise ApiError(403, "invalid_parameter", "cross-site request refused")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in write_types:
            raise ApiError(415, "invalid_parameter", f"send Content-Type: {' or '.join(write_types)}")


router = APIRouter(dependencies=[Depends(local_only)])


# Error codes stay those of ErrorOut (the OpenAPI contract); the HTTP status tells 403, 409, 415 and 422 apart.


# ---- settings ------------------------------------------------------------------------------------------------


def _effective(st: Settings) -> dict[str, Any]:
    """Resolved values for display (read-only): paths as strings, the store password masked, no token."""
    return {
        "home": str(st.home), "settings_file": str(st.settings_file), "token_file": str(st.token_file),
        "hub_url": st.hub_url, "hub_token_source": st.hub_token_source,
        "hub_ca_bundle": str(st.hub_ca_bundle) if st.hub_ca_bundle else None, "hub_timeout_s": st.hub_timeout_s,
        "https_proxy": mask_proxy(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None),
        "no_proxy": os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or None,
        "hub_transport": st.hub_transport, "proxy_user": st.proxy_user, "proxy_password_set": bool(st.proxy_password),
        "snapshot_cache": str(st.snapshot_cache), "snapshot_drop": str(st.snapshot_drop) if st.snapshot_drop else None,
        "store": mask_url(st.store_url) if st.store_url else f"embedded: {st.pg_dir}",
        "pg_dir": str(st.pg_dir), "db": str(st.db), "report_dir": str(st.report_dir) if st.report_dir else None,
        "private_adapter": st.private_adapter or runner.REFERENCE_ADAPTER,
        "private_adapter_default": st.private_adapter_default,
        "private_adapter_config": str(st.private_adapter_config) if st.private_adapter_config else None,
        "private_adapter_sha256": st.private_adapter_sha256,
        "inbox_dirs": [str(d) for d in st.inbox_dirs], "inbox_archive": str(st.inbox_archive),
        "inbox_poll_seconds": st.inbox_poll_seconds, "autopilot": st.autopilot,
        "autopilot_sync_last": st.autopilot_sync_last, "intraday": st.intraday,
        "inbox_lookback_days": st.inbox_lookback_days,
        "history_days": st.history_days,
        "history_bonds_from": st.history_bonds_from.isoformat(), "history_rates_from": st.history_rates_from.isoformat(),
        "backfill": st.backfill, "backfill_from": st.backfill_from, "backfill_budget_s": st.backfill_budget_s,
        "logs_dir": str(st.logs_dir), "update_dir": str(st.update_dir) if st.update_dir else None,
    }


def settings_payload() -> dict[str, Any]:
    """``GET /api/v1/settings``: the stored settings.json (I1 shape, unknown keys too) plus ``hub_token_set``,
    ``home``, ``effective``, ``env_overrides`` (keys the process environment overrides) and ``error``."""
    home = desk_home()
    error = None
    try:
        doc = read_settings_json(home)
    except SettingsError as e:
        doc, error = {}, str(e)
    try:
        st: Settings | None = load()
    except SettingsError as e:
        st, error = None, error or str(e)
    stored = {k: v for k, v in doc.items() if k not in READ_ONLY_KEYS}
    if isinstance(stored.get("https_proxy"), str):  # its password, like the token's, is never sent back
        stored["https_proxy"] = mask_proxy(stored["https_proxy"])
    return {**stored, "hub_token_set": bool(st and st.hub_token), "proxy_password_set": bool(st and st.proxy_password),
            "home": str(home),
            "effective": _effective(st) if st else None, "env_overrides": env_overrides(), "error": error}


@router.get("/api/v1/settings")
def get_settings() -> dict[str, Any]:
    return settings_payload()


@router.put("/api/v1/settings")
def put_settings(body: JsonBody) -> dict[str, Any]:
    """Merge into settings.json: a key with null is removed, absent keys and unknown keys are kept."""
    if "hub_token" in body:
        raise ApiError(422, "invalid_parameter", "the token is not a setting: PUT /api/v1/settings/hub-token")
    if "proxy_password" in body:
        raise ApiError(422, "invalid_parameter", "the proxy password is not a setting: PUT /api/v1/settings/proxy-password")
    try:
        stored = read_settings_json(desk_home()).get("https_proxy")
        if isinstance(stored, str) and body.get("https_proxy") == mask_proxy(stored) != stored:
            del body["https_proxy"]  # the masked value of GET sent back unchanged: keep the stored password
        update_settings_json(body, desk_home())
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    scan.forget_adapters()
    return settings_payload()


@router.put("/api/v1/settings/hub-token")
def put_hub_token(body: JsonBody) -> dict[str, Any]:
    """Store the token in ``<home>/hub_token``; an empty token removes the file. The token is never returned."""
    token = body.get("token")
    if token is not None and not isinstance(token, str):
        raise ApiError(422, "invalid_parameter", "token must be a string")
    try:
        write_hub_token(token, desk_home())
        st = load()
        hubhistory.forget()  # a new token: the history API is asked again in the next cycle
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    return {"hub_token_set": bool(st.hub_token), "hub_token_source": st.hub_token_source}


@router.put("/api/v1/settings/proxy-password")
def put_proxy_password(body: JsonBody) -> dict[str, Any]:
    """Store the proxy password in ``<home>/proxy_password``; empty removes the file. Never returned."""
    password = body.get("password")
    if password is not None and not isinstance(password, str):
        raise ApiError(422, "invalid_parameter", "password must be a string")
    try:
        write_proxy_password(password, desk_home())
        st = load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    return {"proxy_password_set": bool(st.proxy_password)}


@router.post("/api/v1/settings/test-hub")
def test_hub() -> dict[str, Any]:
    """``desk hub-check`` with the current settings, one retry."""
    try:
        st = load()
    except SettingsError as e:
        return {"ok": False, "host": None, "health": None, "token_accepted": False, "latest": None, "error": str(e),
                "transport": None}
    return hub_check(st, retries=1)


# ---- adapter -------------------------------------------------------------------------------------------------


def _settings() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None


def _handle(st: Settings):
    cfg = adapter_config_dir(st)
    return load_adapter(st.private_adapter, str(cfg) if cfg else None, st.private_adapter_sha256,
                        inbox_dirs=[str(d) for d in st.inbox_dirs]), cfg


@router.get("/api/v1/adapter")
def get_adapter() -> dict[str, Any]:
    """The adapter of the settings, loaded afresh through the child-process runner (describe only)."""
    st = _settings()
    h, cfg = _handle(st)
    files, warnings = check_config_files(h.describe)
    for f in files:
        f["exists"] = bool(cfg) and (cfg / f["name"]).is_file()
    return {"loaded": h.ok, "path": st.private_adapter or runner.REFERENCE_ADAPTER,
            "reference": st.private_adapter is None, "default": st.private_adapter_default, "name": h.name, "version": h.version,
            "fingerprint": h.fingerprint, "config_dir": str(cfg) if cfg else None,
            "describe": h.describe or None, "config_files": files, "warnings": warnings, "error": h.error}


def _config_path(st: Settings, name: str) -> Path:
    """The file ``name`` in the adapter's config folder: a plain file name, ``*.json`` or declared by the
    adapter's ``config_files``; never a path out of the folder."""
    if not CONFIG_NAME_RE.fullmatch(name):
        raise ApiError(422, "invalid_parameter", "a plain file name ([A-Za-z0-9_.-], no folder) is required")
    cfg = adapter_config_dir(st)
    if cfg is None:
        raise ApiError(409, "invalid_parameter", "no adapter config folder is set")
    if not name.lower().endswith(".json"):
        declared = {f["name"] for f in check_config_files(_handle(st)[0].describe)[0]}
        if name not in declared:
            raise ApiError(422, "invalid_parameter", f"{name}: only *.json or files the adapter declares")
    path = cfg / name
    if path.resolve().parent != cfg.resolve():
        raise ApiError(422, "invalid_parameter", f"{name}: outside the config folder")
    return path


def _config_payload(name: str, path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8-sig") if path.is_file() else None
    return {"name": name, "path": str(path), "exists": text is not None, "text": text}


@router.get("/api/v1/adapter/config/{name}")
def get_adapter_config(name: str) -> dict[str, Any]:
    st = _settings()
    return _config_payload(name, _config_path(st, name))


@router.put("/api/v1/adapter/config/{name}")
def put_adapter_config(name: str, body: JsonBody) -> dict[str, Any]:
    """Body ``{"text": "..."}``, stored as is (UTF-8); a ``*.json`` file must parse as JSON."""
    st = _settings()
    path = _config_path(st, name)
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(422, "invalid_parameter", "text must be a string")
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ApiError(422, "invalid_parameter", f"larger than {MAX_CONFIG_BYTES // 1024 // 1024} MB")
    if name.lower().endswith(".json"):
        try:
            json.loads(text)
        except ValueError as e:
            details = {"line": getattr(e, "lineno", None), "column": getattr(e, "colno", None)}
            raise ApiError(422, "invalid_parameter", f"not valid JSON: {getattr(e, 'msg', e)}", details) from None
    atomic_write_text(path, text if text.endswith("\n") else text + "\n")
    scan.forget_adapters()  # the fingerprint covers the config folder
    return _config_payload(name, path)
