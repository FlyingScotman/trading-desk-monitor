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

The guard is the settings routes' (``settings_routes.local_only``: this computer while sign-in is off, else an admin;
a write needs JSON). Nothing here imports adapter code: every call goes through ``desk.inbox.runner``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request

from .. import adapter_jobs, settings_forms
from ..inbox import scan
from ..settings import Settings, SettingsError, load
from .app import ApiError
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])
# settings_forms' codes of a key outside ``additionalProperties: false``: warnings in a form, errors for an action
UNKNOWN_PARAM_CODES = ("unknown_config_key", "unknown_field")
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
