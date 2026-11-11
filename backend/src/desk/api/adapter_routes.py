"""The settings page's adapter tools (TDM#421, ``desk.adapter_jobs``): «Проверка адаптера» (the conformance kit as a
background job), «Состояние» (the adapter's optional ``status(ctx)``) and «Действия» (its optional
``describe()["actions"]`` and ``run_action``, 08 §3, §4), so the owner never runs ``desk adapter check`` or edits a file
by hand.

* ``GET /api/v1/adapter/check``: the last kit run (``job``: state, request, ``report`` = the kit's JSON, ``text`` =
  the CLI's summary), whether one runs, the folders offered (``options``) and ``timeout_s``.
* ``POST /api/v1/adapter/check`` ``{"dirs": [...]?, "stub_ctx": false}``: starts a run (202); default folders: the
  inbox folders and the adapter's output folder that exist. 409 while one runs, 422 for a folder that is not one.
* ``GET /api/v1/adapter/status``: ``status(ctx)`` now, under ``adapter_status_timeout_s`` (``available`` false: no such
  function, the page hides the panel).
* ``GET /api/v1/adapter/actions``: the declared actions, each with its last job.
* ``POST /api/v1/adapter/actions/{id}/run`` ``{"params": {...}}``: the params checked against the action's schema
  (422 with ``details.issues``, as a settings form, except that a key the schema closes out with
  ``additionalProperties: false`` is an error, not a warning), then the run (202); 404 for an undeclared action, 409
  while an action runs.
* ``GET /api/v1/adapter/actions/{id}``: its last job; the files written carry their inbox outcome (``inbox``) from
  the store.
* ``GET /api/v1/adapter/choices/{kind}`` (TDM#554): the screen of a ``choices`` kind (``desk.inbox.tools.CHOICE_KINDS``):
  the action that declares it, its last job's ``choices`` (rows, options, what to read again), whether the settings
  file changed since the action built them (``stale``: the page runs the action again), the replay job and, for a kind
  in ``CHOICE_SIGNALS`` (TDM#565), ``signal``: ``{code, count, files, latest}`` — the issue codes the last reading of
  the trade terms files of the last ``SIGNAL_DAYS`` days carries (codes and counts only, no value), ``null`` when none
  or no store. ``state`` (TDM#651, ``desk.index_choices``): why there is no table from the adapter (``code``, ``level``,
  ``text`` in the owner's words with what to press); without the adapter's table the ``trade_index_codes`` kind gets one
  built from the names in the loaded trade terms (``choices_source`` ``store``, ``read_only`` when the adapter has no tab
  to save into). 404 (no adapter, it does not load, no such action) carries the same ``state`` in ``details``.
* ``POST /api/v1/adapter/choices/{kind}`` ``{"values": {row key: option value | null}, "replay": true,
  "base_sha256"?}``: writes the values into the settings tab's object the choices name (``save``: a value sets the
  row's key, null removes it, and the row's ``replaces`` spellings go), as a form save of that tab (its schema checked,
  a revision ``via`` form), then, with ``replay``, starts reading the source again (``desk.inbox_replay``, the period
  the choices give). 409: no choices yet, or the file changed since ``base_sha256``; 422: a key or a value the choices
  do not have, or the tab's schema refuses the file.

The guard is the settings routes' (``settings_routes.local_only``: this computer while sign-in is off, else an admin;
a write needs JSON). Nothing here imports adapter code: every call goes through ``desk.inbox.runner``.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Body, Depends, Request

from .. import adapter_jobs, inbox_replay, index_choices, settings_forms
from ..inbox import scan, tools
from ..settings import Settings, SettingsError, load
from .app import ApiError
from .settings_routes import _blocking, _config_payload, _read, _sha, _write, local_only

router = APIRouter(dependencies=[Depends(local_only)])
# settings_forms' codes of a key outside ``additionalProperties: false``: warnings in a form, errors for an action
UNKNOWN_PARAM_CODES = ("unknown_config_key", "unknown_field")
CHOICE_SIGNALS = {"trade_index_codes": ("unmapped_index",)}
"""The inbox issue codes a choices screen is warned by before it is opened (the settings page's dot and summary line)."""
SIGNAL_DAYS = 10
JsonBody = Annotated[dict[str, Any], Body()]


def _settings() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None


# ---- the kit -----------------------------------------------------------------------------------------------------


def _check_payload(st: Settings) -> dict[str, Any]:
    return {"job": adapter_jobs.job(st.home, "check"), "running": adapter_jobs.running("check") is not None,
            "options": adapter_jobs.check_options(st), "timeout_s": st.adapter_kit_timeout_s}


@router.get("/api/v1/adapter/check")
def get_check() -> dict[str, Any]:
    return _check_payload(_settings())


@router.post("/api/v1/adapter/check", status_code=202)
def post_check(body: JsonBody) -> dict[str, Any]:
    st = _settings()
    dirs = body.get("dirs")
    if dirs is None:
        dirs = [o["path"] for o in adapter_jobs.check_options(st) if o["exists"]]
        if not dirs:
            raise ApiError(422, "invalid_parameter", "no folder to check: add an inbox folder or give one")
    try:
        dirs = adapter_jobs.check_dirs(dirs)
    except ValueError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None
    try:
        adapter_jobs.start_check(st, dirs, body.get("stub_ctx") is True)
    except adapter_jobs.Busy:
        raise ApiError(409, "invalid_parameter", "a check is running: wait for its result") from None
    return _check_payload(st)


# ---- status(ctx) -------------------------------------------------------------------------------------------------


@router.get("/api/v1/adapter/status")
def get_status() -> dict[str, Any]:
    return adapter_jobs.status(_settings())


# ---- actions -----------------------------------------------------------------------------------------------------


def _decl(st: Settings, action_id: str) -> dict:
    h = scan.adapter(st)
    decl = next((a for a in h.actions if a["id"] == action_id), None)
    if decl is None:
        raise ApiError(404, "not_found", f"the adapter declares no action {action_id}")
    return decl


def _with_outcomes(request: Request, j: dict | None) -> dict | None:
    if not j or not j.get("files"):
        return j
    got, error = adapter_jobs.outcomes(getattr(request.app.state, "store_url", None), [f["path"] for f in j["files"]])
    return {**j, "files": [{**f, "inbox": got.get(f["path"])} for f in j["files"]], "inbox_error": error}


@router.get("/api/v1/adapter/actions")
def get_actions() -> dict[str, Any]:
    st = _settings()
    h = scan.adapter(st)
    return {"loaded": h.ok, "error": h.error, "running": adapter_jobs.running("action"),
            "output_root": str(st.adapter_output_dir), "timeout_s": st.adapter_action_timeout_s,
            "actions": [{**a, "job": adapter_jobs.job(st.home, f"action:{a['id']}")} for a in h.actions]}


@router.get("/api/v1/adapter/actions/{action_id}")
def get_action(action_id: str, request: Request) -> dict[str, Any]:
    st = _settings()
    decl = _decl(st, action_id)
    key = f"action:{action_id}"
    return {"action": decl, "running": adapter_jobs.running("action") == key,
            "job": _with_outcomes(request, adapter_jobs.job(st.home, key))}


@router.post("/api/v1/adapter/actions/{action_id}/run", status_code=202)
def run_action(action_id: str, body: JsonBody, request: Request) -> dict[str, Any]:
    st = _settings()
    decl = _decl(st, action_id)
    params = body.get("params") if body.get("params") is not None else {}
    if not isinstance(params, dict):
        raise ApiError(422, "invalid_parameter", "params must be an object")
    # a key the schema closes out (additionalProperties: false) is an error here: a settings form ignores it and keeps
    # the file, an action would run with a parameter it does not know
    issues = [{**i, "severity": "error"} if i["code"] in UNKNOWN_PARAM_CODES else i
              for i in settings_forms.check_schema(decl["params"], params)]
    if any(i["severity"] == "error" for i in issues):
        raise ApiError(422, "invalid_parameter", f"{action_id}: the parameters have errors", {"issues": issues})
    try:
        adapter_jobs.start_action(st, decl, params, getattr(request.app.state, "store_url", None),
                                  getattr(request.app.state, "autopilot", None))
    except adapter_jobs.Busy as b:
        raise ApiError(409, "invalid_parameter", f"an action is running ({str(b).removeprefix('action:')}): "
                                                 "wait for its result") from None
    return get_action(action_id, request)


# ---- choices (TDM#554): a screen of dropdowns over an action's result, saved into a settings tab ----------------------


def _choice_action(st: Settings, kind: str) -> tuple[Any, dict]:
    h = scan.adapter(st)
    decl = next((a for a in h.actions if a.get("choices") == kind), None)
    if decl is None:
        # the owner's words in details.state: why there is no screen (no adapter, it does not load, no such action)
        raise ApiError(404, "not_found", f"the adapter has no action for {kind}: update the adapter",
                       {"state": index_choices.adapter_state(h, kind)})
    return h, decl


def _choices(st: Settings, decl: dict) -> tuple[dict | None, dict | None]:
    j = adapter_jobs.job(st.home, f"action:{decl['id']}")
    res = (j or {}).get("result") or {}
    return j, res.get("choices")


def _screen_choices(st: Settings, h: Any, decl: dict, store_url: str | None) -> tuple[dict | None, dict | None, str | None]:
    """(the action's last job, the screen's choices, where they come from): the adapter's own table, else for the
    ``trade_index_codes`` kind the table the monitor builds from the names in the loaded trade terms (TDM#651:
    ``desk.index_choices``), else None."""
    j, ch = _choices(st, decl)
    if ch:
        return j, ch, "adapter"
    if decl.get("choices") != index_choices.KIND:
        return j, None, None
    rep = {x["source"] for x in inbox_replay.sources(None, st)}
    sc = index_choices.stored_choices(st, h, store_url, rep)
    if sc is None:
        return j, None, None
    sc["file_sha256"] = adapter_jobs.choices_file_sha(st, h, sc)
    return j, sc, "store"


def _signal(store_url: str | None, kind: str) -> dict[str, Any] | None:
    """Codes and counts only: the ``CHOICE_SIGNALS`` codes in the issues of the newest reading of each trade terms file
    (``inbox_file_status`` latest per file) of the newest report date per source within ``SIGNAL_DAYS`` days, one file
    per source and day (the last by report time, sequence, reading: a file the next one replaced is not counted twice).
    "One file per source and day" assumes the day's file is a full snapshot of the books.
    The adapter warns once per trade, so ``count`` is trades. A file read again after the owner's choice no longer
    carries them, and an older day does not keep the sign on."""
    codes = CHOICE_SIGNALS.get(kind)
    if not codes or not store_url:
        return None
    sql = (
        "WITH l AS (SELECT DISTINCT ON (sha256) status_id, source, report_ts, seq, generated_at,"
        "  COALESCE(report_date, at::date) AS day FROM inbox_file_status ORDER BY sha256, status_id DESC),"
        " d AS (SELECT DISTINCT ON (source, day) * FROM l"
        "  WHERE day >= current_date - %(days)s AND EXISTS (SELECT 1 FROM inbox_unit u"
        "   WHERE u.status_id = l.status_id AND u.dataset = 'trade_terms')"
        "  ORDER BY source, day, report_ts DESC NULLS LAST, seq DESC NULLS LAST, generated_at DESC NULLS LAST,"
        "  status_id DESC),"
        " w AS (SELECT d.*, max(day) OVER (PARTITION BY source) AS newest FROM d)"
        " SELECT i.code, sum(i.count), count(DISTINCT w.status_id), max(w.day) FROM w"
        " JOIN inbox_issue i ON i.status_id = w.status_id WHERE w.day = w.newest AND i.code = ANY(%(codes)s)"
        " GROUP BY i.code")
    try:
        with psycopg.connect(store_url, autocommit=True, connect_timeout=3) as con:
            rows = con.execute(sql, {"days": SIGNAL_DAYS, "codes": list(codes)}).fetchall()
    except Exception:  # noqa: BLE001 - no store or an older schema: no sign, never an error of the screen
        return None
    if not rows:
        return None
    return {"code": rows[0][0] if len(rows) == 1 else ",".join(sorted(r[0] for r in rows)),
            "count": int(sum(r[1] for r in rows)), "files": max(int(r[2]) for r in rows),
            "latest": max(r[3] for r in rows).isoformat()}


@router.get("/api/v1/adapter/choices/{kind}")
def get_choices(kind: str, request: Request) -> dict[str, Any]:
    st = _settings()
    h, decl = _choice_action(st, kind)
    store_url = getattr(request.app.state, "store_url", None)
    j, ch, source = _screen_choices(st, h, decl, store_url)
    running = adapter_jobs.running("action") == f"action:{decl['id']}"
    now = adapter_jobs.choices_file_sha(st, h, ch) if ch else None
    return {"action": decl, "running": running,
            "job": {k: v for k, v in (j or {}).items() if k != "result"} if j else None,
            "ok": ((j or {}).get("result") or {}).get("ok"),
            "summary": ((j or {}).get("result") or {}).get("summary") or [],
            "choices": ch, "choices_source": source, "file_sha256": now,
            "stale": source == "adapter" and now != ch.get("file_sha256"),
            "state": index_choices.state(j, running, ch, source, kind),
            "replay": inbox_replay.job(st), "replay_running": inbox_replay.running(),
            "signal": _signal(store_url, kind)}


def _start_replay(st: Any, request: Request, r: Any, reason: str) -> dict[str, Any]:
    """The replay the action asks for after the save: its period and source come from the adapter, so they are checked
    as ``POST /api/v1/inbox/replay`` checks the owner's (period, declared source). The file is already saved, so a bad
    request is a result (``started: false`` and why), not an error."""
    src = r.get("source") if isinstance(r, dict) else None
    out: dict[str, Any] = {"started": False, "source": src, "from": r.get("from") if isinstance(r, dict) else None,
                           "to": r.get("to") if isinstance(r, dict) else None}
    try:
        lo, hi = date.fromisoformat(str(out["from"])), date.fromisoformat(str(out["to"]))
    except ValueError:
        return {**out, "error": "the adapter's replay period is not two dates"}
    if (why := inbox_replay.check_period(lo, hi)) is not None:
        return {**out, "error": f"the adapter's replay period is refused: {why}"}
    if not isinstance(src, str) or src not in {x["source"] for x in inbox_replay.sources(None, st)}:
        return {**out, "error": "the adapter declares no such source to read again"}
    try:
        inbox_replay.start(st, src, lo, hi, getattr(request.app.state, "store_url", None),
                           getattr(request.app.state, "autopilot", None), reason=reason)
    except adapter_jobs.Busy:
        return {**out, "error": "a replay is running: start it again when it ends"}
    return {**out, "started": True}


@router.post("/api/v1/adapter/choices/{kind}")
def save_choices(kind: str, body: JsonBody, request: Request) -> dict[str, Any]:
    st = _settings()
    h, decl = _choice_action(st, kind)
    _j, ch, _source = _screen_choices(st, h, decl, getattr(request.app.state, "store_url", None))
    if not ch:
        raise ApiError(409, "invalid_parameter", f"run «{decl['title']}» first: there is nothing to save yet")
    values = body.get("values")
    if not isinstance(values, dict):
        raise ApiError(422, "invalid_parameter", "values must be an object row key -> option value")
    if ch.get("read_only"):
        raise ApiError(409, "invalid_parameter", "the adapter's tab for the index codes is not readable here: "
                                                 "update the adapter")
    rows = {r["key"] for r in ch["rows"]}
    opts = [o["value"] for o in ch["options"]]
    bad = [k for k, v in values.items() if k not in rows or v not in opts]
    if bad:
        raise ApiError(422, "invalid_parameter", f"{len(bad)} values are not rows or options of the choices",
                       {"keys": bad[:20]})
    tab, path = adapter_jobs.choices_file(st, h, ch)
    if tab is None:
        raise ApiError(409, "invalid_parameter", f"the adapter declares no settings tab {ch['save']['tab']} "
                                                 "or no config folder is set")
    text = _read(path)
    base = body.get("base_sha256", ch.get("file_sha256"))
    if base is not None and base != _sha(text):
        raise ApiError(409, "invalid_parameter", f"{path.name} was changed meanwhile: the table is built again, "
                                                 "repeat the choice")
    try:
        data = json.loads(text) if text is not None else {}
    except ValueError:
        raise ApiError(409, "invalid_parameter", f"{path.name} is not valid JSON: fix it on its tab first") from None
    if not isinstance(data, dict):
        raise ApiError(409, "invalid_parameter", f"{path.name} does not hold an object: fix it on its tab first")
    data, changed = tools.merge_choices(data, ch, values)
    issues = settings_forms.check_schema(tab["schema"], data)
    if any(_blocking(i) for i in issues):
        raise ApiError(422, "invalid_parameter", f"{tab['file']}: the form has errors", {"issues": issues})
    saved = _write(request, path, settings_forms.file_text(data), "form", tab["id"]) if changed else \
        _config_payload(path.name, path)
    replay: dict[str, Any] | None = None
    if body.get("replay") is True and ch.get("replay"):
        r = ch["replay"]
        replay = _start_replay(st, request, r, f"сохранено: {decl['title']}")
    return {"saved": saved, "changed": changed, "schema": issues, "replay": replay,
            "replay_job": inbox_replay.job(st)}
