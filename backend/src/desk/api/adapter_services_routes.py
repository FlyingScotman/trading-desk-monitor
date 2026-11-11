"""The adapter's services (08 §2.2, TDM#548): local administrative routes with the acquisition's rules (admin and an
actual loopback peer/Host, no forwarded peer headers, same-origin form POSTs). Panel values, secrets included, only
in form bodies; never in a URL, a response, a log line or the audit."""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ..acquisition import FlowError
from ..adapter_services import ID_RE as FEED_ID
from ..adapter_services import ServiceError
from .acquisition_routes import form, guard

router = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}
FORBIDDEN = ("local_only", "origin_invalid")
CONFLICT = ("services_busy", "feed_busy", "services_unavailable", "cancel_after_commit_unavailable",
            "state_unavailable")


def fail(code: str, state: str = "blocked") -> JSONResponse:
    status = 403 if code in FORBIDDEN else 404 if code == "feed_unknown" else 409 if code in CONFLICT else 422
    return JSONResponse({"state": state, "code": code}, status_code=status, headers=NO_STORE)


def _service(request: Request):
    return request.app.state.adapter_services


@router.get("/api/v1/adapter-services")
async def overview(request: Request):
    try:
        guard(request)
        return JSONResponse(await run_in_threadpool(_service(request).overview), headers=NO_STORE)
    except (FlowError, ServiceError) as e:  # ServiceError: the state file is in use (state_unavailable)
        return fail(e.code, e.state)


async def _op(request: Request, op: str):
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        job = await run_in_threadpool(_service(request).submit, op, fields)
        return JSONResponse(job, status_code=202, headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()


@router.post("/api/v1/adapter-services/save")
async def save(request: Request):
    return await _op(request, "save")


@router.post("/api/v1/adapter-services/action")
async def action(request: Request):
    return await _op(request, "action")


@router.post("/api/v1/adapter-services/describe")
async def describe(request: Request):
    return await _op(request, "describe")


@router.post("/api/v1/adapter-services/cancel")
async def cancel(request: Request):
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        if set(fields) - {"target"} or [len(v) for v in fields.values()] not in ([], [1]) \
                or fields.get("target", ["job"])[0] not in ("job", "feed"):
            raise ServiceError("invalid_request")
        return JSONResponse(_service(request).cancel(fields.get("target", ["job"])[0]), headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()


@router.post("/api/v1/adapter-services/feeds/{feed_id}/run")
async def run_feed(feed_id: str, request: Request):
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        if not FEED_ID.fullmatch(feed_id) or set(fields) - {"date"} or any(len(v) != 1 for v in fields.values()):
            raise ServiceError("invalid_request")
        day = fields["date"][0] if fields.get("date") and fields["date"][0] else None
        result = await run_in_threadpool(_service(request).run_feed, feed_id, day)
        return JSONResponse(result, status_code=202, headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()


@router.post("/api/v1/adapter-services/feeds/{feed_id}/enabled")
async def set_enabled(feed_id: str, request: Request):
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        if not FEED_ID.fullmatch(feed_id) or set(fields) != {"enabled"} or len(fields["enabled"]) != 1 \
                or fields["enabled"][0] not in ("true", "false"):
            raise ServiceError("invalid_request")
        result = await run_in_threadpool(_service(request).set_enabled, feed_id, fields["enabled"][0] == "true")
        return JSONResponse(result, headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()


@router.get("/api/v1/adapter-services/history")
async def history(request: Request):
    """The trades of past dates (TDM#569): the switch, the first date, the counts by mark, how far the trades are in the
    PnL, the problems. No value of a trade."""
    try:
        guard(request)
        return JSONResponse(await run_in_threadpool(_service(request).history), headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)


def _bool(v: str) -> bool:
    if v not in ("true", "false"):
        raise ServiceError("invalid_request")
    return v == "true"


@router.post("/api/v1/adapter-services/history")
async def history_set(request: Request):
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        if not fields or set(fields) - {"enabled", "from", "when"} or any(len(v) != 1 for v in fields.values()):
            raise ServiceError("invalid_request")
        one = {k: v[0] for k, v in fields.items()}
        result = await run_in_threadpool(_service(request).history_set,
                                         _bool(one["enabled"]) if "enabled" in one else None, one.get("from"),
                                         one.get("when"))
        return JSONResponse(result, headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()


@router.post("/api/v1/adapter-services/history/refetch")
async def history_refetch(request: Request):
    """Ask the dates of a period again. Without ``confirm=true`` only the number of dates is returned."""
    fields: dict[str, list[str]] = {}
    try:
        guard(request)
        fields = await form(request)
        if set(fields) - {"from", "to", "confirm"} or {"from", "to"} - set(fields) \
                or any(len(v) != 1 for v in fields.values()):
            raise ServiceError("invalid_request")
        one = {k: v[0] for k, v in fields.items()}
        result = await run_in_threadpool(_service(request).history_refetch, one["from"], one["to"],
                                         _bool(one["confirm"]) if "confirm" in one else False)
        return JSONResponse(result, headers=NO_STORE)
    except (FlowError, ServiceError) as e:
        return fail(e.code, e.state)
    finally:
        fields.clear()
