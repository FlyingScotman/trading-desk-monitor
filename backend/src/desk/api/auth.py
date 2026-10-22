"""Users, sign-in and access (batch 1, item 9; 05 §6; DECISIONS DEC13): the monitor's own accounts, so that colleagues
can open it from their computers.

**Off** (the setting ``auth``, the default): as before. ``desk serve`` listens on 127.0.0.1 only (``check_bind``
refuses any other address); the admin routes (settings, adapter, updates, diagnostics, users) answer a loopback client
with a loopback ``Host`` and ``Origin`` (``admin_guard``), the rest is open to this computer.

**On**: every page and API needs a signed-in user, except ``PUBLIC`` (``/health/live``, the login page, the login
API). A page without a session is redirected to ``/login.html``; an API call answers 401. Roles (05 §6, ``ROLES``):

* ``viewer``: reads everything but the admin routes;
* ``trader``: also submits scenario jobs, runs the autopilot cycle now, edits aliases (``trader_guard``,
  ``alias_guard``);
* ``admin``: also settings, adapter, updates, diagnostics and users (``admin_guard``); from other computers only
  with the setting ``admin_from_network`` (default on).

Mechanics, all from maintained libraries (ADR04: no custom cryptography):

* passwords: Argon2id hashes (argon2-cffi's ``PasswordHasher``, its RFC 9106 defaults), rehashed on login when the
  parameters change;
* sessions: server-side rows (``app_session``, migration 0015) keyed by the SHA-256 of a random token
  (``secrets.token_urlsafe``) that lives in an ``httpOnly; SameSite=Strict`` cookie (``Secure`` over HTTPS); an idle
  limit (``session_idle_minutes``) and an absolute one (``session_max_hours``);
* CSRF: a state-changing request needs the session's token in ``X-CSRF-Token`` (the pages read it from the
  ``desk_csrf`` cookie: ``prototypes/desk-auth.js``) and, when the browser sends one, an ``Origin`` of this site;
* login throttling: ``login_max_failures`` wrong passwords lock the account for ``login_lockout_minutes``; an address
  with too many failures in that window gets 429 without a password check;
* audit (``audit_log``): logins, logouts, user changes, settings / adapter / update writes, scenario jobs, alias edits.

The first admin: ``desk user add NAME --role admin`` (the installer's guide), or the setup form of the login page,
which only a loopback client may use while no active admin exists (``POST /api/v1/auth/setup``).
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlsplit

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import APIRouter, Depends, FastAPI, Query, Request, Response
from psycopg.rows import dict_row
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse

log = logging.getLogger("desk.auth")

Role = Literal["viewer", "trader", "admin"]
ROLES: tuple[str, ...] = ("viewer", "trader", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
ROLE_NAMES = {"viewer": "просмотр", "trader": "трейдер", "admin": "администратор"}
SESSION_COOKIE = "desk_session"
CSRF_COOKIE = "desk_csrf"
CSRF_HEADER = "x-csrf-token"
LOCAL = "local"
"""The actor of a change from this computer while auth is off."""
MIN_PASSWORD = 8
MAX_PASSWORD = 256
USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
LOGIN_PAGE = "/login.html"
PUBLIC = frozenset({"/health/live", LOGIN_PAGE, "/favicon.ico", "/api/v1/auth/state", "/api/v1/auth/login",
                    "/api/v1/auth/setup"})
"""Paths open without a session when auth is on."""
CSRF_EXEMPT = frozenset({"/api/v1/auth/login", "/api/v1/auth/setup"})
"""No session yet, so no token: these need JSON and a same-site ``Origin`` instead."""
AUDITED = (("/api/v1/settings", "settings"), ("/api/v1/adapter", "adapter"), ("/api/v1/update", "update"),
           ("/api/v1/scenario-jobs", "scenario_job"), ("/api/v1/autopilot/run", "autopilot_run"))
"""State-changing requests the middleware records (path prefix, action); users, logins and aliases record
themselves, with their details."""
_LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("desk: a password nobody has")  # an unknown user costs a verify too (no timing hint)


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.strip("[]").lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _api_error(status: int, code: str, message: str, details: Any = None) -> Exception:
    from .app import ApiError  # app imports this module

    return ApiError(status, code, message, details)


# ---- configuration ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthConfig:
    """How ``create_app`` guards the API; ``from_settings`` for ``desk serve``. Read once at start: a change of the
    settings takes effect after a restart."""

    enabled: bool = False
    admin_from_network: bool = True
    secure_cookies: bool = False  # served over HTTPS
    plain_http_lan: bool = False  # on, on a network address, without TLS: the pages show a warning
    session_idle: timedelta = timedelta(minutes=480)
    session_max: timedelta = timedelta(hours=24)
    max_failures: int = 5
    lockout: timedelta = timedelta(minutes=15)
    ip_max_failures: int = 30  # wrong passwords from one address per lockout window before 429
    cache_s: float = 10.0  # how long a checked session is trusted in memory (a revoke here clears it at once)

    @classmethod
    def from_settings(cls, st: Any, host: str | None = None) -> AuthConfig:
        host = host or st.bind_host
        tls = bool(st.tls_cert and st.tls_key)
        return cls(enabled=st.auth, admin_from_network=st.admin_from_network, secure_cookies=tls,
                   plain_http_lan=st.auth and not is_loopback(host) and not tls,
                   session_idle=timedelta(minutes=st.session_idle_minutes),
                   session_max=timedelta(hours=st.session_max_hours), max_failures=st.login_max_failures,
                   lockout=timedelta(minutes=st.login_lockout_minutes))


class BindError(ValueError):
    """``desk serve`` must not listen on this address with these settings."""


def check_bind(host: str, *, auth: bool, tls: bool, allow_plain_http: bool) -> list[str]:
    """Refuse (``BindError``) a network address without auth, or without TLS unless ``allow_plain_http``; returns
    the warnings to print (plain HTTP on the network)."""
    if is_loopback(host):
        return []
    if not auth:
        raise BindError(
            f"desk serve: адрес {host} доступен из сети, а вход по паролю выключен. Сначала создайте администратора "
            f"(desk user add ИМЯ --role admin) и включите вход: desk lan enable (или settings.json \"auth\": true). "
            f"Либо слушайте только этот компьютер: bind_host 127.0.0.1. / Refusing to listen on {host} without "
            f"auth.")
    if not tls and not allow_plain_http:
        raise BindError(
            f"desk serve: адрес {host} без HTTPS: пароли шли бы по сети открытым текстом. Включите HTTPS "
            f"(desk lan enable --self-signed, или tls_cert и tls_key) либо явно разрешите HTTP "
            f"(desk lan enable --plain-http, settings.json \"allow_plain_http\": true). / Refusing plain HTTP on "
            f"{host}.")
    return [] if tls else [(f"ВНИМАНИЕ: {host} по HTTP без шифрования (allow_plain_http): пароли и данные идут "
                            "по сети открытым текстом; страницы показывают предупреждение.")]


# ---- users -----------------------------------------------------------------------------------------------------


class AuthError(ValueError):
    """A refused user change (bad name, weak password, the last admin); the message is for the person."""

    def __init__(self, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class User:
    username: str
    role: str
    display_name: str | None = None
    user_id: int | None = None
    must_change_password: bool = False

    def can(self, role: str) -> bool:
        return RANK[self.role] >= RANK[role]


LOCAL_ADMIN = User(LOCAL, "admin", "этот компьютер")
"""Who a loopback client is while auth is off."""


def normalize_username(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not USERNAME_RE.fullmatch(name):
        raise AuthError("имя пользователя: латинские буквы, цифры, точка, дефис, подчёркивание; до 64 знаков "
                        "(например ivanov или i.ivanov)")
    return name


def check_password(username: str, password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f"пароль не короче {MIN_PASSWORD} знаков")
    if len(password) > MAX_PASSWORD:
        raise AuthError(f"пароль длиннее {MAX_PASSWORD} знаков")
    if password.strip().lower() == username.lower():
        raise AuthError("пароль не должен совпадать с именем пользователя")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str, password: str) -> bool:
    try:
        return _hasher.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _role(role: str) -> str:
    if role not in ROLES:
        raise AuthError(f"роль: одна из {', '.join(ROLES)}")
    return role


def _cur(con: psycopg.Connection):
    return con.cursor(row_factory=dict_row)


USER_COLS = ("username, display_name, role, active, must_change_password, created_at, created_by, updated_at,"
             " password_changed_at, last_login_at, (locked_until IS NOT NULL AND locked_until > now()) AS locked")


def list_users(con: psycopg.Connection) -> list[dict[str, Any]]:
    return _cur(con).execute(f"SELECT {USER_COLS} FROM app_user ORDER BY username").fetchall()


def get_user(con: psycopg.Connection, username: str) -> dict[str, Any] | None:
    return _cur(con).execute(f"SELECT user_id, {USER_COLS} FROM app_user WHERE username = %s",
                             (username,)).fetchone()


def has_admin(con: psycopg.Connection) -> bool:
    return con.execute("SELECT EXISTS (SELECT 1 FROM app_user WHERE role = 'admin' AND active)").fetchone()[0]


def audit(con: psycopg.Connection, actor: str, action: str, target: str | None = None, *, ok: bool = True,
          ip: str | None = None, detail: dict[str, Any] | None = None) -> None:
    con.execute("INSERT INTO audit_log (actor, action, target, ok, ip, detail) VALUES (%s, %s, %s, %s, %s, %s)",
                (actor, action, target, ok, ip, json.dumps(detail, ensure_ascii=False, default=str)
                 if detail is not None else None))


def list_audit(con: psycopg.Connection, limit: int = 200, action: str | None = None) -> list[dict[str, Any]]:
    sql, args = "SELECT audit_id, at, actor, action, target, ok, ip, detail FROM audit_log", []
    if action:
        sql += " WHERE action = %s"
        args.append(action)
    return _cur(con).execute(sql + " ORDER BY audit_id DESC LIMIT %s", (*args, limit)).fetchall()


def add_user(con: psycopg.Connection, username: str, role: str, password: str, *, actor: str,
             display_name: str | None = None, must_change: bool = False, ip: str | None = None) -> dict[str, Any]:
    name = normalize_username(username)
    _role(role)
    check_password(name, password)
    display = (display_name or "").strip() or None
    if display and len(display) > 120:
        raise AuthError("отображаемое имя длиннее 120 знаков")
    with con.transaction():
        if con.execute("SELECT 1 FROM app_user WHERE username = %s", (name,)).fetchone():
            raise AuthError(f"пользователь {name} уже есть", 409)
        con.execute("INSERT INTO app_user (username, display_name, role, password_hash, must_change_password,"
                    " created_by) VALUES (%s, %s, %s, %s, %s, %s)",
                    (name, display, role, hash_password(password), must_change, actor))
        audit(con, actor, "user_add", name, ip=ip, detail={"role": role, "must_change_password": must_change})
    return get_user(con, name)  # type: ignore[return-value]


def _last_admin(con: psycopg.Connection, name: str) -> bool:
    return not con.execute("SELECT EXISTS (SELECT 1 FROM app_user WHERE role = 'admin' AND active AND username <> %s)",
                           (name,)).fetchone()[0]


def update_user(con: psycopg.Connection, username: str, *, actor: str, role: str | None = None,
                active: bool | None = None, display_name: str | None = None, unlock: bool = False,
                ip: str | None = None) -> dict[str, Any]:
    """Role, enabled or not, display name, unlock. Disabling revokes the user's sessions; the last active admin
    can neither be disabled nor lose the role."""
    name = normalize_username(username)
    with con.transaction():
        row = _cur(con).execute("SELECT * FROM app_user WHERE username = %s FOR UPDATE", (name,)).fetchone()
        if row is None:
            raise AuthError(f"пользователя {name} нет", 404)
        changes: dict[str, Any] = {}
        if role is not None and _role(role) != row["role"]:
            changes["role"] = role
        if active is not None and active != row["active"]:
            changes["active"] = active
        if display_name is not None and ((display_name.strip() or None) != row["display_name"]):
            if len(display_name.strip()) > 120:
                raise AuthError("отображаемое имя длиннее 120 знаков")
            changes["display_name"] = display_name.strip() or None
        if unlock and (row["locked_until"] or row["failed_logins"]):
            changes.update(locked_until=None, failed_logins=0)
        demoted = row["role"] == "admin" and row["active"] and (
            changes.get("role", "admin") != "admin" or changes.get("active") is False)
        if demoted and _last_admin(con, name):
            raise AuthError("это последний активный администратор: сначала назначьте другого", 409)
        if changes.get("active") is False and name == actor:
            raise AuthError("нельзя отключить самого себя", 409)
        if changes:
            sets = ", ".join(f"{k} = %s" for k in changes)
            con.execute(f"UPDATE app_user SET {sets}, updated_at = clock_timestamp() WHERE user_id = %s",
                        (*changes.values(), row["user_id"]))
            if changes.get("active") is False or "role" in changes:
                revoke_user_sessions(con, row["user_id"])
            audit(con, actor, "user_change", name, ip=ip,
                  detail={k: v for k, v in changes.items() if k != "failed_logins"})
    return get_user(con, name)  # type: ignore[return-value]


def set_password(con: psycopg.Connection, username: str, password: str, *, actor: str, must_change: bool = False,
                 ip: str | None = None, keep_session: int | None = None) -> None:
    """A new password (an admin's reset, or the user's own change): unlocks the account and revokes its sessions
    but ``keep_session`` (the user's own, on a change)."""
    name = normalize_username(username)
    check_password(name, password)
    with con.transaction():
        row = con.execute("SELECT user_id FROM app_user WHERE username = %s FOR UPDATE", (name,)).fetchone()
        if row is None:
            raise AuthError(f"пользователя {name} нет", 404)
        con.execute("UPDATE app_user SET password_hash = %s, must_change_password = %s, failed_logins = 0,"
                    " locked_until = NULL, password_changed_at = clock_timestamp(), updated_at = clock_timestamp()"
                    " WHERE user_id = %s", (hash_password(password), must_change, row[0]))
        revoke_user_sessions(con, row[0], keep=keep_session)
        audit(con, actor, "password_change" if actor == name else "password_reset", name, ip=ip,
              detail={"must_change_password": must_change})


def revoke_user_sessions(con: psycopg.Connection, user_id: int, keep: int | None = None) -> None:
    con.execute("UPDATE app_session SET revoked_at = clock_timestamp() WHERE user_id = %s AND revoked_at IS NULL"
                " AND session_id IS DISTINCT FROM %s", (user_id, keep))
    _registry_forget(user_id=user_id)


class LoginFailed(Exception):
    def __init__(self, message: str, status: int = 401, code: str = "unauthorized") -> None:
        super().__init__(message)
        self.status, self.code = status, code


BAD_LOGIN = "неверное имя пользователя или пароль"


def authenticate(con: psycopg.Connection, username: str, password: str, cfg: AuthConfig,
                 ip: str | None = None) -> User:
    """The user of a name and password, or ``LoginFailed``; the attempt is in the audit log either way."""
    try:
        name: str | None = normalize_username(username)
    except AuthError:
        name = None
    now = datetime.now(UTC)
    err: LoginFailed | None = None
    with con.transaction():  # the failure count and the audit row are kept: the error is raised after it
        row = _cur(con).execute("SELECT * FROM app_user WHERE username = %s FOR UPDATE", (name,)).fetchone() \
            if name else None
        if row is None:
            verify_password(_DUMMY_HASH, password or "")
            audit(con, (name or "?")[:64], "login_failed", name, ok=False, ip=ip, detail={"reason": "unknown"})
            err = LoginFailed(BAD_LOGIN)
        elif row["locked_until"] and row["locked_until"] > now:
            audit(con, name, "login_failed", name, ok=False, ip=ip, detail={"reason": "locked"})  # type: ignore[arg-type]
            err = LoginFailed(_locked_message(row["locked_until"]), 429, "locked")
        elif not verify_password(row["password_hash"], password or ""):
            failed = row["failed_logins"] + 1
            locked = failed >= cfg.max_failures
            con.execute("UPDATE app_user SET failed_logins = %s, locked_until = %s WHERE user_id = %s",
                        (0 if locked else failed, now + cfg.lockout if locked else None, row["user_id"]))
            audit(con, name, "login_failed", name, ok=False, ip=ip,  # type: ignore[arg-type]
                  detail={"reason": "password", "failures": failed, "locked": locked})
            err = LoginFailed(_locked_message(now + cfg.lockout), 429, "locked") if locked else LoginFailed(BAD_LOGIN)
        elif not row["active"]:  # said only to someone who knows the password
            audit(con, name, "login_failed", name, ok=False, ip=ip, detail={"reason": "disabled"})  # type: ignore[arg-type]
            err = LoginFailed("учётная запись отключена: обратитесь к администратору", 403, "forbidden")
        else:
            rehash = _hasher.check_needs_rehash(row["password_hash"])
            con.execute("UPDATE app_user SET failed_logins = 0, locked_until = NULL, last_login_at = %s"
                        + (", password_hash = %s" if rehash else "") + " WHERE user_id = %s",
                        (now, *([hash_password(password)] if rehash else []), row["user_id"]))
            audit(con, name, "login", name, ip=ip)  # type: ignore[arg-type]
    if err is not None:
        raise err
    assert row is not None and name is not None
    return User(name, row["role"], row["display_name"], row["user_id"], row["must_change_password"])


def _locked_message(until: datetime) -> str:
    msk = (until + timedelta(hours=3)).strftime("%H:%M")
    return f"слишком много неверных паролей: вход заблокирован до {msk} МСК (или администратор снимет блокировку)"


# ---- sessions --------------------------------------------------------------------------------------------------


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Session:
    session_id: int
    user: User
    csrf: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    checked: float = field(default=0.0)  # time.monotonic() of the last check against the store


_registries: list[Sessions] = []  # every app's cache in this process: a revoke anywhere clears them


def _registry_forget(user_id: int | None = None, token_sha: str | None = None) -> None:
    for reg in _registries:
        reg.forget(user_id=user_id, token_sha=token_sha)


class Sessions:
    """Session lookups for the middleware, through a short in-memory cache (``AuthConfig.cache_s``)."""

    def __init__(self, store_url: str, cfg: AuthConfig) -> None:
        self.store_url, self.cfg = store_url, cfg
        self._items: dict[str, Session] = {}
        self._lock = threading.Lock()
        _registries.append(self)

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.store_url, autocommit=True, connect_timeout=5)

    def forget(self, user_id: int | None = None, token_sha: str | None = None) -> None:
        with self._lock:
            for k in [k for k, s in self._items.items() if (user_id is None and token_sha is None)
                      or s.user.user_id == user_id or k == token_sha]:
                del self._items[k]

    def create(self, user: User, ip: str | None, user_agent: str | None) -> tuple[str, str]:
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.connect() as con:
            con.execute("INSERT INTO app_session (token_sha256, user_id, csrf_token, expires_at, ip, user_agent)"
                        " VALUES (%s, %s, %s, clock_timestamp() + %s, %s, %s)",
                        (_token_hash(token), user.user_id, csrf, self.cfg.session_max, ip, (user_agent or "")[:300]))
        return token, csrf

    def revoke(self, token: str) -> None:
        sha = _token_hash(token)
        with self.connect() as con:
            con.execute("UPDATE app_session SET revoked_at = clock_timestamp() WHERE token_sha256 = %s"
                        " AND revoked_at IS NULL", (sha,))
        _registry_forget(token_sha=sha)

    def lookup(self, token: str) -> Session | None:
        """The live session of a cookie's token: not revoked, not past its absolute or idle limit, its user
        active. The store's ``last_seen_at`` moves at most once a minute."""
        sha = _token_hash(token)
        now_m = time.monotonic()
        with self._lock:
            hit = self._items.get(sha)
        if hit is not None and now_m - hit.checked < self.cfg.cache_s:
            return hit
        with self.connect() as con:
            row = _cur(con).execute(
                "SELECT s.session_id, s.csrf_token, s.created_at, s.last_seen_at, s.expires_at, clock_timestamp() AS now,"
                " u.user_id, u.username, u.role, u.display_name, u.must_change_password FROM app_session s"
                " JOIN app_user u USING (user_id) WHERE s.token_sha256 = %s AND s.revoked_at IS NULL AND u.active",
                (sha,)).fetchone()
            if row is None or row["now"] >= row["expires_at"] or row["now"] - row["last_seen_at"] >= self.cfg.session_idle:
                with self._lock:
                    self._items.pop(sha, None)
                return None
            if row["now"] - row["last_seen_at"] >= timedelta(minutes=1):
                con.execute("UPDATE app_session SET last_seen_at = clock_timestamp() WHERE session_id = %s",
                            (row["session_id"],))
        s = Session(row["session_id"], User(row["username"], row["role"], row["display_name"], row["user_id"],
                                            row["must_change_password"]),
                    row["csrf_token"], row["created_at"], row["last_seen_at"], row["expires_at"], now_m)
        with self._lock:
            self._items[sha] = s
        return s


class LoginThrottle:
    """Wrong passwords per client address in the lockout window (in memory: a restart forgets them)."""

    def __init__(self, cfg: AuthConfig) -> None:
        self.cfg = cfg
        self._fails: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, ip: str) -> collections.deque[float]:
        q = self._fails.setdefault(ip, collections.deque())
        cut = time.monotonic() - self.cfg.lockout.total_seconds()
        while q and q[0] < cut:
            q.popleft()
        return q

    def blocked(self, ip: str) -> bool:
        with self._lock:
            return len(self._recent(ip)) >= self.cfg.ip_max_failures

    def failed(self, ip: str) -> None:
        with self._lock:
            self._recent(ip).append(time.monotonic())


# ---- request helpers -------------------------------------------------------------------------------------------


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_user(request: Request) -> User | None:
    """The signed-in user (auth on), ``LOCAL_ADMIN`` for a loopback client while auth is off, else None."""
    cfg: AuthConfig = request.app.state.auth
    if cfg.enabled:
        s = getattr(request.state, "session", None)
        return s.user if s else None
    return LOCAL_ADMIN if is_loopback(client_ip(request)) else None


def actor(request: Request) -> str:
    """Who to record for a change: the user's name, or ``local`` while auth is off."""
    u = current_user(request)
    return u.username if u and request.app.state.auth.enabled else LOCAL


def same_site(request: Request) -> bool:
    """The browser's ``Origin`` (else ``Referer``) is this site; True without either (not a browser)."""
    ref = request.headers.get("origin") or request.headers.get("referer")
    if not ref:
        return True
    if ref == "null":
        return False
    u = urlsplit(ref)
    return (u.scheme, u.netloc.lower()) == (request.url.scheme, request.headers.get("host", "").lower())


def _check_write_type(request: Request, write_types: tuple[str, ...]) -> None:
    if request.method not in SAFE_METHODS:
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype not in write_types:
            raise _api_error(415, "invalid_parameter", f"send Content-Type: {' or '.join(write_types)}")


def check_local(request: Request, write_types: tuple[str, ...] = ("application/json",)) -> None:
    """A same-machine browser or tool: a loopback client with a loopback ``Host`` (no DNS rebinding) and, if the
    browser sends one, a loopback ``Origin``; a write needs one of ``write_types`` (none a cross-site form can send).
    """
    if not is_loopback(client_ip(request)):
        raise _api_error(403, "invalid_parameter", "settings are available from this computer only (127.0.0.1)")
    if not is_loopback(request.url.hostname):
        raise _api_error(403, "invalid_parameter", "open the settings page as http://127.0.0.1:<port>/settings.html")
    origin = request.headers.get("origin")
    if origin and origin != "null" and not is_loopback(urlsplit(origin).hostname):
        raise _api_error(403, "invalid_parameter", "cross-site request refused")
    _check_write_type(request, write_types)


def _signed_in(request: Request, role: str) -> User:
    user = current_user(request)
    if user is None:
        raise _api_error(401, "unauthorized", "войдите в Desk Monitor")
    if not user.can(role):
        raise _api_error(403, "forbidden", f"нужна роль «{ROLE_NAMES[role]}» (у вас «{ROLE_NAMES[user.role]}»)")
    return user


def admin_guard(request: Request) -> User:
    """The dependency of the admin routes: ``require_admin`` with JSON writes."""
    return require_admin(request)


def require_admin(request: Request, write_types: tuple[str, ...] = ("application/json",)) -> User:
    """Settings, adapter, updates, diagnostics, users. Auth off: ``check_local``. On: an admin, from another
    computer only with ``admin_from_network``; a write still needs one of ``write_types``."""
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        check_local(request, write_types)
        return LOCAL_ADMIN
    user = _signed_in(request, "admin")
    if not cfg.admin_from_network and not is_loopback(client_ip(request)):
        raise _api_error(403, "forbidden", "администрирование только с компьютера, где работает Desk Monitor "
                                           "(admin_from_network выключен)")
    _check_write_type(request, write_types)
    return user


def trader_guard(request: Request) -> User | None:
    """Scenario jobs and the autopilot's «Обновить». Auth off: open, as before (127.0.0.1 only)."""
    if not request.app.state.auth.enabled:
        return current_user(request)
    return _signed_in(request, "trader")


def alias_guard(request: Request) -> User:
    """Alias edits: a trader or admin; auth off: this computer only (``check_local``), as before."""
    if not request.app.state.auth.enabled:
        check_local(request)
        return LOCAL_ADMIN
    user = _signed_in(request, "trader")
    _check_write_type(request, ("application/json",))
    return user


# ---- the middleware --------------------------------------------------------------------------------------------


def _wants_page(request: Request) -> bool:
    return request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")


def _login_redirect(request: Request, extra: str = "") -> Response:
    target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"{LOGIN_PAGE}?{extra}next={quote(target, safe='/')}", status_code=303,
                            headers={"Cache-Control": "no-store"})


def _audited(request: Request) -> str | None:
    if request.method in SAFE_METHODS:
        return None
    path = request.url.path
    for prefix, action in AUDITED:
        if path == prefix or path.startswith(prefix + "/"):
            return action
    return None


def install(app: FastAPI, error: Callable[..., Response]) -> None:
    """The auth middleware and routes on ``app``; ``app.state.auth`` (an ``AuthConfig``) is set by ``create_app``.
    ``error(request, status, code, message)`` builds the API's ErrorOut responses."""
    cfg: AuthConfig = app.state.auth
    app.state.sessions = Sessions(app.state.store_url, cfg)
    app.state.login_throttle = LoginThrottle(cfg)

    @app.middleware("http")
    async def authenticate_request(request: Request, call_next):
        request.state.session = None
        path = request.url.path
        if cfg.enabled:
            token = request.cookies.get(SESSION_COOKIE)
            if token:
                try:
                    request.state.session = await run_in_threadpool(app.state.sessions.lookup, token)
                except psycopg.OperationalError:
                    log.error("sessions: the store is unavailable")
                    return error(request, 503, "store_unavailable", "desk store unavailable")
            session = request.state.session
            public = path in PUBLIC
            if session is None and not public:
                if _wants_page(request):
                    return _login_redirect(request)
                return error(request, 401, "unauthorized", "войдите в Desk Monitor: " + LOGIN_PAGE)
            if session and session.user.must_change_password and not public and \
                    not path.startswith("/api/v1/auth/"):
                if _wants_page(request):
                    return _login_redirect(request, "change=1&")
                return error(request, 403, "forbidden", "смените временный пароль: " + LOGIN_PAGE + "?change=1")
            if request.method not in SAFE_METHODS:
                if not same_site(request):
                    return error(request, 403, "forbidden", "cross-site request refused")
                if path not in CSRF_EXEMPT and not (
                        session and hmac.compare_digest(request.headers.get(CSRF_HEADER, ""), session.csrf)):
                    return error(request, 403, "forbidden", "CSRF token missing or wrong (reload the page)")
        action = _audited(request)
        keys = None
        if action == "settings" and request.method == "PUT" and path == "/api/v1/settings" and \
                int(request.headers.get("content-length") or 0) <= 1 << 20:
            try:  # the names of the keys changed, never their values
                body = json.loads(await request.body() or b"{}")
                keys = sorted(body) if isinstance(body, dict) else None
            except ValueError:
                keys = None
        response = await call_next(request)
        if action is not None and response.status_code not in (401, 403, 415, 422):  # refused before acting
            detail = {"method": request.method, "status": response.status_code, **({"keys": keys} if keys else {})}
            await run_in_threadpool(_audit_quietly, app.state.store_url, actor(request), action, path,
                                    response.status_code < 400, client_ip(request), detail)
        return response

    app.include_router(router)


def _audit_quietly(store_url: str, who: str, action: str, target: str, ok: bool, ip: str | None,
                   detail: dict[str, Any]) -> None:
    try:
        with psycopg.connect(store_url, autocommit=True, connect_timeout=3) as con:
            audit(con, who, action, target, ok=ok, ip=ip, detail=detail)
    except psycopg.Error as e:
        log.warning("audit not recorded (%s %s): %s", action, target, e)


# ---- routes ----------------------------------------------------------------------------------------------------

router = APIRouter(tags=["auth"])


def _rw(request: Request):
    with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=5) as con:
        yield con


RW = Annotated[psycopg.Connection, Depends(_rw)]
Admin = Annotated[User, Depends(admin_guard)]


class UserOut(BaseModel):
    username: str
    role: Role
    display_name: str | None = None
    must_change_password: bool = False


class AuthState(BaseModel):
    enabled: bool = Field(description="Sign-in is on (the setting auth); off: this computer only, no accounts")
    user: UserOut | None = Field(description="Who this is: the signed-in user; auth off: 'local' (admin) for this "
                                             "computer; null: not signed in")
    setup_needed: bool | None = Field(description="No active admin yet (null: the store is unavailable)")
    setup_allowed: bool = Field(description="This client may create the first admin (POST /api/v1/auth/setup): "
                                            "a loopback client while setup_needed")
    loopback: bool = Field(description="The client is this computer")
    secure: bool = Field(description="Served over HTTPS")
    plain_http_lan: bool = Field(description="Sign-in over plain HTTP on a network address: show a warning")
    admin_from_network: bool
    roles: dict[str, str] = Field(description="Role -> its Russian name")


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=MAX_PASSWORD * 4)


class SetupIn(LoginIn):
    display_name: str | None = Field(None, max_length=120)


class PasswordIn(BaseModel):
    current_password: str = Field(max_length=MAX_PASSWORD * 4)
    new_password: str = Field(max_length=MAX_PASSWORD * 4)


class UserRow(BaseModel):
    username: str
    display_name: str | None
    role: Role
    active: bool
    must_change_password: bool
    locked: bool
    created_at: datetime
    created_by: str
    updated_at: datetime
    password_changed_at: datetime
    last_login_at: datetime | None


class UserList(BaseModel):
    users: list[UserRow]
    roles: dict[str, str]


class UserIn(BaseModel):
    username: str = Field(max_length=200)
    role: Role
    password: str = Field(max_length=MAX_PASSWORD * 4)
    display_name: str | None = Field(None, max_length=120)
    must_change_password: bool = Field(True, description="The user sets their own password at the first sign-in")


class UserPatch(BaseModel):
    role: Role | None = None
    active: bool | None = None
    display_name: str | None = Field(None, max_length=120)
    unlock: bool = Field(False, description="Clear a lockout after wrong passwords")


class PasswordReset(BaseModel):
    password: str = Field(max_length=MAX_PASSWORD * 4)
    must_change_password: bool = Field(True, description="A temporary password: changed at the next sign-in")


class AuditRow(BaseModel):
    audit_id: int
    at: datetime
    actor: str
    action: str
    target: str | None
    ok: bool
    ip: str | None
    detail: Any = None


class AuditList(BaseModel):
    entries: list[AuditRow]


def _user_out(u: User | None) -> dict[str, Any] | None:
    return u and {"username": u.username, "role": u.role, "display_name": u.display_name,
                  "must_change_password": u.must_change_password}


def _setup_needed(request: Request) -> bool | None:
    try:
        with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=3) as con:
            return not has_admin(con)
    except psycopg.Error:
        return None


def _state(request: Request, user: User | None = None) -> dict[str, Any]:
    cfg: AuthConfig = request.app.state.auth
    loop = is_loopback(client_ip(request)) and is_loopback(request.url.hostname)
    needed = _setup_needed(request)
    return {"enabled": cfg.enabled, "user": _user_out(user or current_user(request)), "setup_needed": needed,
            "setup_allowed": bool(needed) and loop, "loopback": loop, "secure": request.url.scheme == "https",
            "plain_http_lan": cfg.plain_http_lan, "admin_from_network": cfg.admin_from_network,
            "roles": ROLE_NAMES}


def _set_cookies(request: Request, response: Response, token: str, csrf: str) -> None:
    cfg: AuthConfig = request.app.state.auth
    age = int(cfg.session_max.total_seconds())
    for name, value, http_only in ((SESSION_COOKIE, token, True), (CSRF_COOKIE, csrf, False)):
        response.set_cookie(name, value, max_age=age, path="/", httponly=http_only, samesite="strict",
                            secure=cfg.secure_cookies)


def _clear_cookies(request: Request, response: Response) -> None:
    secure = request.app.state.auth.secure_cookies
    for name, http_only in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(name, path="/", httponly=http_only, samesite="strict", secure=secure)


def _err(e: AuthError) -> Exception:
    code = {404: "not_found", 409: "invalid_parameter"}.get(e.status, "invalid_parameter")
    return _api_error(e.status, code, str(e))


def _json_only(request: Request) -> None:
    _check_write_type(request, ("application/json",))


@router.get("/api/v1/auth/state", response_model=AuthState)
def auth_state(request: Request, response: Response):
    """Sign-in on or off, who this is, whether the first admin may be created here. Open without a session."""
    response.headers["Cache-Control"] = "no-store"
    return _state(request)


@router.post("/api/v1/auth/login", response_model=AuthState, dependencies=[Depends(_json_only)],
             responses={401: {"description": "wrong name or password"}, 429: {"description": "locked out"}})
def login(request: Request, body: LoginIn, response: Response):
    cfg: AuthConfig = request.app.state.auth
    if not cfg.enabled:
        raise _api_error(409, "invalid_parameter", "вход по паролю выключен: Desk Monitor открыт только на этом "
                                                   "компьютере")
    ip = client_ip(request) or "?"
    throttle: LoginThrottle = request.app.state.login_throttle
    if throttle.blocked(ip):
        raise _api_error(429, "locked", "слишком много неверных паролей с этого адреса: подождите "
                                        f"{int(cfg.lockout.total_seconds() // 60)} минут")
    try:
        with psycopg.connect(request.app.state.store_url, autocommit=True, connect_timeout=5) as con:
            user = authenticate(con, body.username, body.password, cfg, ip)
    except LoginFailed as e:
        if e.status in (401, 429):
            throttle.failed(ip)
        raise _api_error(e.status, e.code, str(e)) from None
    token, csrf = request.app.state.sessions.create(user, ip, request.headers.get("user-agent"))
    _set_cookies(request, response, token, csrf)
    response.headers["Cache-Control"] = "no-store"
    return _state(request, user)


@router.post("/api/v1/auth/logout", status_code=204)
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    response = Response(status_code=204)
    if request.app.state.auth.enabled and token:
        user = current_user(request)
        request.app.state.sessions.revoke(token)
        if user:
            _audit_quietly(request.app.state.store_url, user.username, "logout", user.username, True,
                           client_ip(request), {})
    _clear_cookies(request, response)
    return response


@router.post("/api/v1/auth/setup", response_model=AuthState, dependencies=[Depends(_json_only)],
             description="Create the first admin: only while there is no active admin, only from this computer")
def setup(request: Request, body: SetupIn, response: Response, con: RW):
    if not (is_loopback(client_ip(request)) and is_loopback(request.url.hostname)):
        raise _api_error(403, "forbidden", "первого администратора создают на самом компьютере Desk Monitor: "
                                           "http://127.0.0.1:<порт>/login.html или desk user add ИМЯ --role admin")
    with con.transaction():
        con.execute("SELECT pg_advisory_xact_lock(hashtext('desk:auth-setup'))")
        if has_admin(con):
            raise _api_error(409, "invalid_parameter", "администратор уже есть: войдите под ним")
        try:
            add_user(con, body.username, "admin", body.password, actor=LOCAL, display_name=body.display_name,
                     ip=client_ip(request))
        except AuthError as e:
            raise _err(e) from None
    cfg: AuthConfig = request.app.state.auth
    user = None
    if cfg.enabled:  # signed in at once
        row = get_user(con, normalize_username(body.username))
        user = User(row["username"], "admin", row["display_name"], row["user_id"])
        token, csrf = request.app.state.sessions.create(user, client_ip(request), request.headers.get("user-agent"))
        _set_cookies(request, response, token, csrf)
    return _state(request, user)


@router.post("/api/v1/auth/password", status_code=204, dependencies=[Depends(_json_only)])
def change_password(request: Request, body: PasswordIn, con: RW):
    """The signed-in user's own password (also the forced change after a reset); other sessions are revoked."""
    cfg: AuthConfig = request.app.state.auth
    session = getattr(request.state, "session", None)
    if not cfg.enabled or session is None:
        raise _api_error(409, "invalid_parameter", "вход по паролю выключен" if not cfg.enabled else "войдите")
    row = con.execute("SELECT password_hash FROM app_user WHERE user_id = %s", (session.user.user_id,)).fetchone()
    if row is None or not verify_password(row[0], body.current_password):
        raise _api_error(403, "forbidden", "текущий пароль неверен")
    if body.new_password == body.current_password:
        raise _api_error(422, "invalid_parameter", "новый пароль совпадает с текущим")
    try:
        set_password(con, session.user.username, body.new_password, actor=session.user.username,
                     ip=client_ip(request), keep_session=session.session_id)
    except AuthError as e:
        raise _err(e) from None
    request.app.state.sessions.forget(user_id=session.user.user_id)
    return Response(status_code=204)


# ---- users (admins) ------------------------------------------------------------------------------------------


@router.get("/api/v1/users", response_model=UserList, dependencies=[Depends(admin_guard)])
def users(con: RW):
    return {"users": list_users(con), "roles": ROLE_NAMES}


@router.post("/api/v1/users", response_model=UserRow, status_code=201)
def create_user(request: Request, body: UserIn, admin: Admin, con: RW):
    try:
        return add_user(con, body.username, body.role, body.password, actor=actor(request),
                        display_name=body.display_name, must_change=body.must_change_password,
                        ip=client_ip(request))
    except AuthError as e:
        raise _err(e) from None


@router.patch("/api/v1/users/{username}", response_model=UserRow)
def change_user(request: Request, username: str, body: UserPatch, admin: Admin,
                con: RW):
    try:
        return update_user(con, username, actor=actor(request), role=body.role, active=body.active,
                           display_name=body.display_name, unlock=body.unlock, ip=client_ip(request))
    except AuthError as e:
        raise _err(e) from None


@router.post("/api/v1/users/{username}/password", status_code=204)
def reset_password(request: Request, username: str, body: PasswordReset, admin: Admin,
                   con: RW):
    try:
        set_password(con, username, body.password, actor=actor(request), must_change=body.must_change_password,
                     ip=client_ip(request))
    except AuthError as e:
        raise _err(e) from None
    return Response(status_code=204)


@router.get("/api/v1/audit", response_model=AuditList, dependencies=[Depends(admin_guard)])
def audit_entries(con: RW, limit: Annotated[int, Query(ge=1, le=2000)] = 200, action: str | None = None):
    return {"entries": list_audit(con, limit, action)}


# ---- TLS -------------------------------------------------------------------------------------------------------


def self_signed_cert(folder: Path, names: list[str], days: int = 825) -> tuple[Path, Path]:
    """A self-signed certificate and its key (``desk.crt``, ``desk.key``) for ``names`` (host names and IP
    addresses; localhost and 127.0.0.1 are added), via ``cryptography``. Browsers warn about it until each
    colleague accepts it (or IT issues a certificate from the corporate CA: tls_cert / tls_key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    from ..settings import atomic_write_text

    key = ec.generate_private_key(ec.SECP256R1())
    alt: list[x509.GeneralName] = []
    seen: set[str] = set()
    for n in [*names, "localhost", "127.0.0.1"]:
        n = n.strip().lower()
        if not n or n in seen:
            continue
        seen.add(n)
        try:
            alt.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            alt.append(x509.DNSName(n))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (names[0] if names else "localhost")[:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Desk Monitor (self-signed)")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    crt, pem = folder / "desk.crt", folder / "desk.key"
    atomic_write_text(pem, key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()).decode(), private=True)
    atomic_write_text(crt, cert.public_bytes(serialization.Encoding.PEM).decode())
    return crt, pem


def local_names() -> list[str]:
    """This computer's host names and IPv4 addresses (for the certificate and the URL to give colleagues)."""
    import socket

    out: list[str] = []
    for n in (socket.gethostname(), socket.getfqdn()):
        if n and n not in out:
            out.append(n)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = str(info[4][0])
            if ip not in out and not ip.startswith("127."):
                out.append(ip)
    except OSError:
        pass
    return out

