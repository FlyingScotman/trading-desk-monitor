"""Settings API (I5) behind the settings page ``/settings.html``: ``<home>/settings.json``, the hub token file,
a hub check, and the private adapter's status and declared config files (08 §4 ``config_files``), with forms over
them (08 §4 ``settings_tabs``, FND-4): a check against the tab's schema and a dry run of the adapter's
``reference()`` (``desk.settings_forms``), the write, its revisions in the store (``adapter_config_rev``, migration
0018: who and when) and what the forms offer in their lists (books seen in recent positions, the hub's indices).

Every route re-reads the settings (``desk.settings.load``), so an edit applies to the running server. The adapter
is only ever called through the child-process runner (``desk.inbox.loader.load_adapter``), never imported here.

Access (``desk.api.auth.require_admin``): with sign-in off, loopback clients only (``desk serve`` binds 127.0.0.1),
with a loopback ``Host`` (no DNS rebinding) and, if the browser sends one, a loopback ``Origin``; with it on, an
admin (from other computers only with ``admin_from_network``). A write needs ``Content-Type: application/json``,
which a cross-site form cannot send. The token is accepted, stored and reported as set or not, never returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Body, Depends, Request
from psycopg.rows import dict_row

from .. import funding_settings, settings_forms
from ..inbox import runner, scan
from ..inbox.loader import CONFIG_NAME_RE, check_config_files, check_settings_tabs, load_adapter
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
from .auth import LOCAL, actor, require_admin

MAX_CONFIG_BYTES = 2 * 1024 * 1024
LOCAL_USER = LOCAL
"""``changed_by`` of a change while sign-in is off (this computer; as ``alias_routes``); with it on, the user's name."""
RECENT_DAYS = 35
"""Books seen in positions of the last ``RECENT_DAYS`` days before the newest position date are offered."""
CURRENCIES = ("RUB", "USD", "EUR", "CNY", "CHF", "GBP", "JPY", "HKD", "KZT", "AED", "TRY", "BYN", "INR")
JsonBody = Annotated[dict[str, Any], Body()]


def local_only(request: Request) -> None:
    """The admin routes' guard (see the module doc): this computer while sign-in is off, else an admin."""
    require_admin(request)


def check_local(request: Request, write_types: tuple[str, ...] = ("application/json",)) -> None:
    """``local_only`` with the content types a write may send (none of them one a cross-site form can send)."""
    require_admin(request, write_types)


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
        "inbox_poll_seconds": st.inbox_poll_seconds, "adapter_check_timeout_s": st.adapter_check_timeout_s,
        "autopilot": st.autopilot,
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


# ---- funding (#237) -------------------------------------------------------------------------------------------


def _funding_payload() -> dict[str, Any]:
    """``GET /api/v1/settings/funding``: the stored ``funding`` object (null: not set), the effective one over the
    defaults, the market default rate in force and what the form offers."""
    try:
        stored = read_settings_json(desk_home()).get(funding_settings.KEY)
    except SettingsError:
        stored = None
    fs = funding_settings.from_doc(stored)
    return {"stored": stored, "effective": {"default_rate": fs.default_rate, "default_rate_by_ccy": fs.default_rate_by_ccy,
                                            "rules": [{k: v for k, v in r.items() if k != "source"} for r in fs.rule_rows],
                                            "policy": fs.policy},
            "default": fs.describe(), "error": fs.error, "options": funding_settings.options()}


@router.get("/api/v1/settings/funding")
def get_funding_settings() -> dict[str, Any]:
    return _funding_payload()


@router.put("/api/v1/settings/funding")
def put_funding_settings(body: JsonBody) -> dict[str, Any]:
    """Replace the ``funding`` object of settings.json (checked by ``funding_settings.check``); the next run uses it."""
    try:
        update_settings_json({funding_settings.KEY: body}, desk_home())
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    return _funding_payload()


@router.delete("/api/v1/settings/funding")
def delete_funding_settings() -> dict[str, Any]:
    """Back to the defaults (``funding_settings.DEFAULTS``)."""
    try:
        update_settings_json({funding_settings.KEY: None}, desk_home())
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    return _funding_payload()


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
    tabs = check_settings_tabs(h.describe)[0]  # its warnings are the handle's, in ``warnings`` below
    for f in files:
        f["exists"] = bool(cfg) and (cfg / f["name"]).is_file()
    return {"loaded": h.ok, "path": st.private_adapter or runner.REFERENCE_ADAPTER,
            "reference": st.private_adapter is None, "default": st.private_adapter_default, "name": h.name, "version": h.version,
            "fingerprint": h.fingerprint, "config_dir": str(cfg) if cfg else None,
            "describe": h.describe or None, "config_files": files, "settings_tabs": tabs,
            "warnings": warnings + [w for w in h.warnings if w.startswith("settings_tabs")], "error": h.error}


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


def _sha(text: str | None) -> str | None:
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text is not None else None


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8-sig") if path.is_file() else None


def _config_payload(name: str, path: Path, **more: Any) -> dict[str, Any]:
    text = _read(path)
    return {"name": name, "path": str(path), "exists": text is not None, "text": text, "sha256": _sha(text), **more}


@router.get("/api/v1/adapter/config/{name}")
def get_adapter_config(name: str) -> dict[str, Any]:
    st = _settings()
    return _config_payload(name, _config_path(st, name))


@router.put("/api/v1/adapter/config/{name}")
def put_adapter_config(name: str, body: JsonBody, request: Request) -> dict[str, Any]:
    """Body ``{"text": "...", "base_sha256": "..."}``, stored as is (UTF-8); a ``*.json`` file must parse as
    JSON. ``base_sha256`` (optional): the ``sha256`` the page read; another one on disk is a 409 (changed
    meanwhile). Each write is a revision (``via`` raw), when the store is reachable."""
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
    _check_base(path, body)
    return _write(request, path, text if text.endswith("\n") else text + "\n", "raw", None)


def _check_base(path: Path, body: dict) -> None:
    base = body.get("base_sha256")
    if base is not None and base != _sha(_read(path)):
        raise ApiError(409, "invalid_parameter", f"{path.name} was changed meanwhile: reload it and repeat the change")


def _write(request: Request, path: Path, text: str, via: str, tab_id: str | None) -> dict[str, Any]:
    old = _read(path)
    atomic_write_text(path, text)
    scan.forget_adapters()  # the fingerprint covers the config folder
    rev, error = _record(request, path, old, text, via, tab_id)
    return _config_payload(path.name, path, revision=rev, history_error=error)


# ---- settings tabs (08 §4 settings_tabs): forms over the config files ---------------------------------------------


def _tab(st: Settings, tab_id: str) -> tuple[Any, dict, Path | None, Path]:
    h, cfg = _handle(st)
    tab = next((t for t in check_settings_tabs(h.describe)[0] if t["id"] == tab_id), None)
    if tab is None:
        raise ApiError(404, "not_found", f"the adapter declares no settings tab {tab_id}")
    return h, tab, cfg, _config_path(st, tab["file"])


def _data(body: dict) -> str:
    if "data" not in body:
        raise ApiError(422, "invalid_parameter", "data (the file as a JSON object) is required")
    text = settings_forms.file_text(body["data"])
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ApiError(422, "invalid_parameter", f"larger than {MAX_CONFIG_BYTES // 1024 // 1024} MB")
    return text


def _blocking(i: dict) -> bool:
    return i["severity"] == "error"


@router.post("/api/v1/adapter/settings/{tab_id}/check")
def check_settings_tab(tab_id: str, body: JsonBody) -> dict[str, Any]:
    """Body ``{"data": {...}, "adapter": true}``: the issues of the schema check (``schema``) and, when it has no
    error and ``adapter`` is not false, of the adapter's ``reference()`` over a copy of the config folder with the
    file replaced (``adapter``, each with ``new``: not an issue of the current files), and ``text``, what a save
    writes (for the diff). ``ok``: no schema error and no new adapter error. When the adapter could not check it,
    ``adapter_error`` says why and ``adapter_error_code`` is ``timeout`` (over ``adapter_timeout_s``, the setting
    ``adapter_check_timeout_s``), ``failed`` or ``not_loaded``. A save (PUT) never depends on this check."""
    st = _settings()
    h, tab, cfg, path = _tab(st, tab_id)
    text = _data(body)
    schema = settings_forms.check_schema(tab["schema"], body["data"])
    out: dict[str, Any] = {"tab": tab_id, "file": tab["file"], "text": text,
                           "current": _config_payload(tab["file"], path), "schema": schema, "adapter": [],
                           "adapter_error": None, "adapter_error_code": None, "adapter_checked": False,
                           "adapter_timeout_s": st.adapter_check_timeout_s}
    if body.get("adapter", True) is not False and not any(_blocking(i) for i in schema):
        run = settings_forms.dry_run(h, cfg, tab["file"], text, st.adapter_check_timeout_s)
        out.update(adapter=run.issues, adapter_error=run.error, adapter_error_code=run.code,
                   adapter_checked=run.error is None)
    out["ok"] = not any(_blocking(i) for i in schema) and not any(_blocking(i) and i["new"] for i in out["adapter"])
    return out


@router.put("/api/v1/adapter/settings/{tab_id}")
def put_settings_tab(tab_id: str, body: JsonBody, request: Request) -> dict[str, Any]:
    """Body ``{"data": {...}, "base_sha256": "..."}``: written as two-space JSON when the schema check has no
    error (422 with the issues in ``details.issues`` otherwise); 409 when the file changed since it was read
    (``base_sha256`` null: the page read no file). A revision ``via`` form."""
    st = _settings()
    _h, tab, _cfg, path = _tab(st, tab_id)
    text = _data(body)
    issues = settings_forms.check_schema(tab["schema"], body["data"])
    if any(_blocking(i) for i in issues):
        raise ApiError(422, "invalid_parameter", f"{tab['file']}: the form has errors", {"issues": issues})
    if "base_sha256" in body and body["base_sha256"] != _sha(_read(path)):
        raise ApiError(409, "invalid_parameter", f"{path.name} was changed meanwhile: reload it and repeat the change")
    return {**_write(request, path, text, "form", tab_id), "schema": issues}


# ---- revisions (adapter_config_rev, migration 0018) -------------------------------------------------------------


def _store(request: Request, read_only: bool = True) -> psycopg.Connection:
    url = getattr(request.app.state, "store_url", None)
    if not url:
        raise psycopg.OperationalError("no store")
    return psycopg.connect(url, row_factory=dict_row, autocommit=True, connect_timeout=3,
                           options="-c default_transaction_read_only=on" if read_only else "")


def _who(request: Request) -> str:
    return actor(request)  # the signed-in admin; LOCAL_USER while sign-in is off


def _record(request: Request, path: Path, old: str | None, text: str, via: str,
            tab_id: str | None) -> tuple[dict | None, str | None]:
    """A revision of the write, after one of the previous content when that was changed outside the page (or never
    recorded). Never fails the write: without the store the history misses it, and the answer says so."""
    folder, name = str(path.parent), path.name
    ins = ("INSERT INTO adapter_config_rev (config_dir, file_name, content, content_sha256, via, tab_id, changed_by) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s) "
           "RETURNING rev_id, via, tab_id, changed_by, changed_at, content_sha256 AS sha256")
    try:
        with _store(request, read_only=False) as con, con.transaction():
            last = con.execute("SELECT content_sha256 FROM adapter_config_rev WHERE config_dir = %s AND file_name = %s "
                               "ORDER BY rev_id DESC LIMIT 1", (folder, name)).fetchone()
            if old is not None and (last is None or last["content_sha256"] != _sha(old)):
                con.execute(ins, (folder, name, old, _sha(old), "file", None, "file"))
            rev = con.execute(ins, (folder, name, text, _sha(text), via, tab_id, _who(request))).fetchone()
    except psycopg.Error as e:
        return None, f"the history is not recorded: store unavailable ({type(e).__name__})"
    return {**rev, "changed_at": rev["changed_at"].isoformat()}, None


@router.get("/api/v1/adapter/config/{name}/history")
def get_config_history(name: str, request: Request) -> dict[str, Any]:
    """The revisions of a config file, newest first (100 at most), without their content; ``changed_outside``:
    the file on disk is none of them (edited by hand since)."""
    st = _settings()
    path = _config_path(st, name)
    current = _sha(_read(path))
    try:
        with _store(request) as con:
            rows = con.execute(
                "SELECT rev_id, via, tab_id, changed_by, changed_at, content_sha256 AS sha256, "
                "octet_length(content) AS bytes FROM adapter_config_rev WHERE config_dir = %s AND file_name = %s "
                "ORDER BY rev_id DESC LIMIT 100", (str(path.parent), name)).fetchall()
    except psycopg.Error as e:
        return {"name": name, "available": False, "error": f"store unavailable ({type(e).__name__})",
                "revisions": [], "changed_outside": False}
    revs = [{**r, "changed_at": r["changed_at"].isoformat(), "current": r["sha256"] == current} for r in rows]
    return {"name": name, "available": True, "error": None, "revisions": revs,
            "changed_outside": current is not None and bool(revs) and revs[0]["sha256"] != current}


@router.get("/api/v1/adapter/config/{name}/history/{rev_id}")
def get_config_revision(name: str, rev_id: int, request: Request) -> dict[str, Any]:
    """One revision with its ``text`` (to compare or to load back into the form)."""
    st = _settings()
    path = _config_path(st, name)
    try:
        with _store(request) as con:
            r = con.execute("SELECT rev_id, via, tab_id, changed_by, changed_at, content_sha256 AS sha256, "
                            "content AS text FROM adapter_config_rev "
                            "WHERE config_dir = %s AND file_name = %s AND rev_id = %s",
                            (str(path.parent), name, rev_id)).fetchone()
    except psycopg.Error as e:
        raise ApiError(503, "store_unavailable", f"store unavailable ({type(e).__name__})") from None
    if r is None:
        raise ApiError(404, "not_found", f"{name}: no revision {rev_id}")
    return {**r, "changed_at": r["changed_at"].isoformat()}


# ---- what the forms offer --------------------------------------------------------------------------------------


_RECENT_BOOKS = f"""
WITH newest AS (SELECT max(valid_date) AS d FROM position_set),
sets AS (
    SELECT DISTINCT ON (ps.book_id) ps.book_id, ps.valid_date, ps.set_id, ps.source
    FROM position_set ps, newest WHERE ps.valid_date >= newest.d - {RECENT_DAYS}
    ORDER BY ps.book_id, ps.valid_date DESC, ps.revision DESC, ps.set_id DESC),
books AS (SELECT DISTINCT ON (book_id) * FROM book_rev ORDER BY book_id, rev_id DESC)
SELECT s.book_id, s.valid_date AS last_date, s.source,
       (SELECT count(*) FROM position_input pi WHERE pi.set_id = s.set_id) AS positions,
       b.name, b.desk, b.book_type, b.entity, b.trader
FROM sets s LEFT JOIN books b USING (book_id) ORDER BY s.book_id"""


@router.get("/api/v1/adapter/settings-context")
def get_settings_context(request: Request) -> dict[str, Any]:
    """Lists for the forms, from the store: ``recent_books`` (books with positions in the last weeks; the page marks
    those no books.json entry names «не настроено»), ``indices`` (the hub's fixings) and ``currencies``. Without the
    store: empty lists (the currencies: the usual ones) and ``error``."""
    out: dict[str, Any] = {"recent_books": [], "indices": [], "currencies": list(CURRENCIES), "error": None,
                           "recent_days": RECENT_DAYS}
    try:
        with _store(request) as con:
            out["recent_books"] = [{**r, "last_date": r["last_date"].isoformat()} for r in con.execute(_RECENT_BOOKS)]
            out["indices"] = [{"code": r["index_code"], "last": r["last"].isoformat() if r["last"] else None,
                               "unit": r["unit"]} for r in con.execute(
                "SELECT index_code, max(fixing_date) AS last, max(unit) AS unit FROM md_hub_fixing "
                "GROUP BY index_code ORDER BY index_code")]
            pairs = [r["pair"] for r in con.execute("SELECT DISTINCT pair FROM md_hub_fx")]
    except psycopg.Error as e:
        out["error"] = f"store unavailable ({type(e).__name__})"
        return out
    seen = {c for p in pairs for c in re.findall(r"[A-Z]{3}", p or "")}
    out["currencies"] += sorted(seen - set(CURRENCIES))
    return out
