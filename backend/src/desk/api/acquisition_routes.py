"""Local-only administrative acquisition; credentials are bounded form bytes, never validation models."""
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from ..acquisition import MAX_REQUEST, FlowError, parameters
from .auth import check_local, require_admin

FORM = "application/x-www-form-urlencoded"


def guard(request: Request):
    # Reject proxy attribution entirely: sensitive local credentials never use forwarded peer information.
    if any(k.lower().startswith("x-forwarded-") or k.lower() in ("forwarded", "x-real-ip") for k in request.headers):
        raise FlowError("local_only")
    check_local(request, (FORM,))
    require_admin(request, (FORM,))
    if request.method == "POST":
        # Form POSTs are cross-site sendable: require the actual same origin, including port, also with auth off.
        try:
            origin = urlsplit(request.headers.get("origin", ""))
            actual = request.url
            if origin.username is not None or origin.password is not None or origin.path or origin.query or origin.fragment:
                raise ValueError
            if (origin.scheme, origin.hostname, origin.port or (443 if origin.scheme == "https" else 80)) != \
                    (actual.scheme, actual.hostname, actual.port or (443 if actual.scheme == "https" else 80)):
                raise ValueError
        except ValueError:
            raise FlowError("origin_invalid") from None


def fail(e: FlowError, status=422):
    return JSONResponse({"state": e.state, "code": e.code}, status_code=status,
                        headers={"Cache-Control": "no-store"})


async def form(request: Request) -> dict[str, list[str]]:
    raw = bytearray()
    try:
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > MAX_REQUEST):
            raise ValueError
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_REQUEST:
                raise ValueError
        fields = parse_qs(raw.decode("utf-8"), keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=256)
        return fields
    except (ValueError, UnicodeError):
        raise FlowError("invalid_request") from None
    finally:
        raw.clear()


router = APIRouter()


@router.get("/api/v1/acquisition")
def available(request: Request):
    try:
        guard(request)
    except FlowError as e:
        return fail(e, 403)
    service = request.app.state.acquisition
    return JSONResponse({**service.availability(), "job": service.status()}, headers={"Cache-Control": "no-store"})


@router.post("/api/v1/acquisition/start")
async def start(request: Request):
    fields = {}
    try:
        guard(request)
        fields = await form(request)
        params = parameters(fields)
        result = await run_in_threadpool(request.app.state.acquisition.start, params)
        return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
    except FlowError as e:
        return fail(e, 403 if e.code in ("local_only", "origin_invalid") else 409
                    if e.code in ("acquisition_busy", "acquisition_unavailable") else 422)
    finally:
        fields.clear()


@router.post("/api/v1/acquisition/cancel")
async def cancel(request: Request):
    fields = {}
    try:
        guard(request)
        fields = await form(request)
        if fields:
            raise FlowError("invalid_request")
        return JSONResponse(request.app.state.acquisition.cancel(), headers={"Cache-Control": "no-store"})
    except FlowError as e:
        return fail(e, 403 if e.code in ("local_only", "origin_invalid") else 409)
    finally:
        fields.clear()
