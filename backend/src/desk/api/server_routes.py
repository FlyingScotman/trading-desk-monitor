"""The server from the settings page (section «Доступ из сети»): its state, a restart, and the network access.

* ``GET  /api/v1/server``          what runs (address, port, HTTPS, sign-in) and what the settings say, whether a
                                   restart is needed and possible, the addresses for colleagues, the firewall rule
                                   to give IT, the admins (who can sign in after sign-in is turned on), the outcome
                                   of the last restart (``desk.restart.last``)
* ``POST /api/v1/server/restart``  ``{}``: restart now (``desk.restart``: the launcher starts it again)
* ``POST /api/v1/server/network``  ``{"lan", "https", "port", "host", "regenerate_cert", "admin_from_network",
                                   "auth_off", "restart"}``: the same settings changes as ``desk lan enable`` /
                                   ``desk lan disable`` (``desk.lan.enable_changes``, ``disable_changes``), staged so
                                   that the launcher rolls them back when the new server does not start, then a
                                   restart

Access: admins only (``require_admin``: sign-in off, this computer only; on, an admin, from another computer only
with ``admin_from_network``), JSON writes, the CSRF token with sign-in on (the auth middleware), each action in the
audit log (``server_restart``, ``network_change``). Nothing here can lock the owner out: no sign-in without an
active admin, the port and address are tried before they are written, 127.0.0.1 keeps answering whatever the
address (``desk.api.app.listen_sockets``), and a server that does not start with the new settings is started
again with the old ones by the launcher. The Windows firewall rule needs administrator rights and stays a command
(the page shows it with «Скопировать для ИТ»).
"""

from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from .. import lan, restart, update
from ..settings import Settings, SettingsError, env_overrides, load, read_settings_json, update_settings_json
from . import auth as authn
from .app import ApiError
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])
HOST_RE = re.compile(r"[A-Za-z0-9.:_-]{1,253}|\[[0-9A-Fa-f:.]+\]")  # as desk.settings checks bind_host


def network_guard(request: Request) -> authn.User:
    """Changes of the network and of sign-in: an admin (``require_admin``) on this computer, or on another one only
    while ``admin_from_network`` is on (with sign-in off ``require_admin`` already allows this computer only)."""
    user = authn.require_admin(request)
    cfg: authn.AuthConfig = request.app.state.auth
    if cfg.enabled and not cfg.admin_from_network and not authn.is_loopback(authn.client_ip(request)):
        raise ApiError(403, "forbidden", "доступ из сети меняют только на компьютере, где работает Desk Monitor "
                                         "(admin_from_network выключен)")
    return user


def _st() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", f"ошибка в настройках: {e}") from None


def unavailable(request: Request) -> str | None:
    """Why this server cannot restart itself; None when it can (it runs under the launcher, start.bat)."""
    if getattr(request.app.state, "request_restart", None) is None:
        return ("Сервер запущен не через start.bat: перезапустить его отсюда нельзя. Остановите его и запустите "
                "start.bat (или перезапустите тем же способом, каким запускали).")
    return None


def _scheme(https: bool) -> str:
    return "https" if https else "http"


def configured(st: Settings) -> dict[str, Any]:
    """What the settings say (``desk serve`` reads it when it starts)."""
    tls = bool(st.tls_cert and st.tls_key)
    own = bool(tls and st.tls_cert and Path(st.tls_cert).parent == st.home / "tls")
    return {"lan": not lan.is_loopback(st.bind_host), "bind_host": st.bind_host, "port": st.port, "https": tls,
            "plain_http": bool(st.allow_plain_http) and not tls, "auth": bool(st.auth),
            "admin_from_network": bool(st.admin_from_network), "self_signed": own,
            "tls_cert": str(st.tls_cert) if st.tls_cert else None}


def running(request: Request) -> dict[str, Any] | None:
    """What this server listens on and how it guards (read once at its start); None outside ``desk serve``."""
    listen = getattr(request.app.state, "listen", None)
    if not listen:
        return None
    cfg: authn.AuthConfig = request.app.state.auth
    return {"lan": not lan.is_loopback(listen["host"]), "bind_host": listen["host"], "port": listen["port"],
            "https": bool(listen["tls"]), "auth": cfg.enabled, "admin_from_network": cfg.admin_from_network}


def restart_needed(run: dict[str, Any] | None, conf: dict[str, Any]) -> list[str]:
    """The settings that differ from what runs (they apply at the next start)."""
    if run is None:
        return []
    return [k for k in ("bind_host", "port", "https", "auth", "admin_from_network") if run[k] != conf[k]]


def _admins(request: Request) -> list[str] | None:
    try:
        with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=3) as con:
            return [r["username"] for r in authn.list_users(con) if r["role"] == "admin" and r["active"]]
    except psycopg.Error:
        return None


def _urls(conf: dict[str, Any]) -> tuple[str, list[str]]:
    scheme = _scheme(conf["https"])
    return (f"{scheme}://127.0.0.1:{conf['port']}/",
            lan.lan_urls(conf["bind_host"], conf["port"], conf["https"]) if conf["lan"] else [])


@router.get("/api/v1/server")
def server_state(request: Request) -> dict[str, Any]:
    st = _st()
    conf, run = configured(st), running(request)
    local_url, urls = _urls(conf)
    ip = authn.client_ip(request)
    cfg: authn.AuthConfig = request.app.state.auth
    why = unavailable(request)
    # the outcome before the staged change: the launcher drops network.json, then writes last.json, so an outcome
    # read here is never paired with the marker it already dropped (TDM#451)
    last = restart.last(st.home)
    pending = restart.pending(st.home) is not None
    return {
        "version": update.current_version(), "instance": request.app.state.instance,
        "started_at": request.app.state.started_at.isoformat(timespec="seconds"),
        "can_restart": why is None, "unavailable": why,
        "running": run, "configured": conf, "restart_needed": restart_needed(run, conf),
        "locked": [k for k in env_overrides() if k in lan.NETWORK_KEYS],
        "client": {"loopback": authn.is_loopback(ip),
                   "may_change_network": authn.is_loopback(ip) or (cfg.enabled and cfg.admin_from_network)},
        "admins": _admins(request), "addresses": lan.local_addresses(), "computer": socket.gethostname(),
        "local_url": local_url, "urls": urls,
        "cert": lan.cert_info(st.tls_cert, lan.local_names()) if conf["https"] else None,
        "firewall": {"rule": lan.FIREWALL_RULE.format(port=st.port), "add": lan.firewall_add_command(st.port),
                     "present": lan.firewall_rule(st.port)} if conf["lan"] else None,
        "pending": pending, "last": last,
    }


class RestartIn(BaseModel):
    reason: str | None = Field(None, max_length=200)


@router.post("/api/v1/server/restart")
def restart_now(request: Request, body: RestartIn) -> dict[str, Any]:
    why = unavailable(request)
    who, ip = authn.actor(request), authn.client_ip(request)
    if why:
        raise ApiError(409, "invalid_parameter", why)
    authn._audit_quietly(request.app.state.store_url, who, "server_restart", "desk serve", True, ip, {})
    request.app.state.request_restart()
    return {"restarting": True, "instance": request.app.state.instance}


class NetworkIn(BaseModel):
    lan: bool = Field(description="Colleagues may open the monitor from their computers (sign-in on)")
    https: bool = Field(True, description="HTTPS (a self-signed certificate made here); false: plain HTTP")
    port: int | None = Field(None, ge=1, le=65535, description="null: keep the setting")
    host: str | None = Field(None, max_length=255, description="The address to listen on (lan); default 0.0.0.0")
    regenerate_cert: bool = Field(False, description="A new self-signed certificate even if one is set")
    admin_from_network: bool | None = Field(None, description="Admins may use the settings from other computers")
    auth_off: bool = Field(False, description="With lan false: sign-in off too (this computer only, no accounts)")
    restart: bool = Field(True, description="Restart at once (when the server can)")


NetworkAdmin = Annotated[authn.User, Depends(network_guard)]


def _refuse(message: str, status: int = 422) -> ApiError:
    return ApiError(status, "invalid_parameter" if status != 403 else "forbidden", message)


@router.post("/api/v1/server/network")
def change_network(request: Request, body: NetworkIn, admin: NetworkAdmin) -> dict[str, Any]:
    st = _st()
    home = st.home
    who, ip = authn.actor(request), authn.client_ip(request)
    cfg: authn.AuthConfig = request.app.state.auth
    run = running(request)
    have_tls = bool(st.tls_cert and st.tls_key)
    host = (body.host or "0.0.0.0").strip() if body.lan else "127.0.0.1"
    port = body.port or st.port
    if body.lan and (not HOST_RE.fullmatch(host) or lan.is_loopback(host)):
        raise _refuse("адрес для доступа из сети: «все адреса» (0.0.0.0) или сетевой адрес этого компьютера")
    mode = (("self_signed" if body.regenerate_cert or not have_tls else None) if body.https else "plain_http") \
        if body.lan else None
    # what the change writes, before anything is written: a key the environment sets cannot be changed here
    planned = {"bind_host": host, **({"port": port} if port != st.port else {})}
    if body.lan or body.auth_off:
        planned["auth"] = bool(body.lan)
    if mode:
        planned["tls_cert"] = mode
    if body.admin_from_network is not None and body.admin_from_network != st.admin_from_network:
        planned["admin_from_network"] = body.admin_from_network
    locked = [k for k in env_overrides() if k in planned or (k in ("tls_key", "allow_plain_http") and mode)]
    if locked:
        raise _refuse("эти настройки заданы переменными окружения (или при запуске start.bat --port) и меняются "
                      f"только там: {', '.join(locked)}", 409)
    problem = lan.listen_problem(host, port, current={"port": run["port"]} if run else None)
    if problem:
        raise _refuse(problem, 409)
    has_admin = False
    if body.lan:  # sign-in goes on: somebody must be able to sign in after the restart
        try:
            with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=5) as con:
                has_admin = authn.has_admin(con)
        except psycopg.Error:
            raise ApiError(503, "store_unavailable", "база недоступна: не проверить, есть ли администратор") from None
    files: list[dict[str, str | None]] = []
    if mode == "self_signed":  # the files the new certificate replaces: restored when the new server fails
        files = restart.backup_files(home, [home / "tls" / "desk.crt", home / "tls" / "desk.key"])
    try:
        if body.lan:
            changes, _info = lan.enable_changes(st, has_admin=has_admin, host=host, port=body.port, tls=mode,
                                                admin_from_network=body.admin_from_network)
        else:
            changes = lan.disable_changes(body.auth_off)
            if body.port:
                changes["port"] = body.port
            if body.admin_from_network is not None:
                changes["admin_from_network"] = body.admin_from_network
    except lan.LanError as e:
        raise _refuse(e.ru, 409 if e.exit == 1 else 422) from None
    try:
        stored = read_settings_json(home)
    except SettingsError:
        stored = {}
    previous = {k: stored.get(k) for k in changes}
    why = unavailable(request)
    now = body.restart and why is None
    earlier = restart.pending(home)  # applied again before the restart: the way back stays the original one
    if now and earlier is not None:
        previous = {**previous, **(earlier.get("previous") or {})}
        targets = {f.get("target") for f in earlier.get("files") or []}
        files = [*(earlier.get("files") or []), *(f for f in files if f.get("target") not in targets)]
    if now:  # before the write: a crash in between still leaves the launcher the way back
        https = have_tls if mode is None else mode == "self_signed"
        restart.stage_network_change(home, previous, files=files, by=who,
                                     expect={"host": host, "port": port, "https": https})
    try:
        update_settings_json(changes, home)
    except SettingsError as e:
        restart.drop_network_change(home)
        _restore(files)
        authn._audit_quietly(request.app.state.store_url, who, "network_change", "settings.json", False, ip,
                             {"keys": sorted(changes), "error": str(e)[:200]})
        raise _refuse(f"настройки не сохранены: {e}") from None
    conf = configured(_st())
    local_url, urls = _urls(conf)
    authn._audit_quietly(request.app.state.store_url, who, "network_change", "settings.json", True, ip,
                         {"keys": sorted(changes), "lan": conf["lan"], "https": conf["https"], "port": conf["port"],
                          "bind_host": conf["bind_host"], "auth": conf["auth"], "new_certificate": bool(files),
                          "restart": now})
    if now:
        request.app.state.request_restart()
    sign_in = conf["auth"] and not cfg.enabled
    msg = ("Сервер перезапускается с новыми настройками." if now else
           "Сохранено. Новые настройки заработают после перезапуска сервера"
           + (f" ({why.rstrip('.')})" if why and body.restart else "") + ".")
    if sign_in:
        msg += " После перезапуска нужен вход: имя и пароль администратора."
    return {"restarting": now, "changes": sorted(changes), "configured": conf, "local_url": local_url, "urls": urls,
            "sign_in_needed": sign_in, "message": msg, "instance": request.app.state.instance}


def _restore(files: list[dict[str, str | None]]) -> None:
    import shutil

    for f in files:
        if f.get("backup") and f.get("target"):
            try:
                shutil.copy2(str(f["backup"]), str(f["target"]))
            except OSError:
                pass
