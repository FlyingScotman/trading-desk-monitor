"""Read a source's reports again from the interface (TDM#553, ``desk.inbox_replay``): the button «Перечитать» on C14
«Статус дня» and on the settings page, never ``desk.bat inbox replay``.

* ``GET /api/v1/inbox/replay?source=&from=&to=``: the sources (``inbox_replay.sources``), the default period (the
  previous business day), for a source and a period the estimate (how many accepted files it reads again), the last
  job and whether one runs.
* ``POST /api/v1/inbox/replay`` ``{"source", "from", "to"}``: starts the replay (202, the job); 404 for a source the
  adapter does not declare, 422 for a bad period, 409 while one runs.

The guard is the settings routes' (``settings_routes.local_only``: this computer while sign-in is off, else an admin;
a write needs JSON): a replay rewrites the store's revisions of a source. C14 is a read page for every user: it asks
this route and offers the button only where the answer is not a 403.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Body, Depends, Query, Request

from .. import adapter_jobs, inbox_replay
from ..settings import Settings, SettingsError, load
from .app import ApiError
from .settings_routes import local_only

router = APIRouter(dependencies=[Depends(local_only)])
JsonBody = Annotated[dict[str, Any], Body()]


def _settings() -> Settings:
    try:
        return load()
    except SettingsError as e:
        raise ApiError(422, "invalid_parameter", str(e)) from None


def _day(v: Any, name: str) -> date | None:
    if v is None or v == "":
        return None
    try:
        if not isinstance(v, str) or len(v) != 10:
            raise ValueError
        return date.fromisoformat(v)
    except ValueError:
        raise ApiError(422, "invalid_parameter", f"{name}: a date YYYY-MM-DD") from None


def _connect(request: Request) -> psycopg.Connection | None:
    url = getattr(request.app.state, "store_url", None)
    if not url:
        return None
    try:
        return psycopg.connect(url, autocommit=True, connect_timeout=3,
                               options="-c default_transaction_read_only=on")
    except psycopg.Error:
        return None


def payload(request: Request, st: Settings, source: str | None = None, date_from: date | None = None,
            date_to: date | None = None) -> dict[str, Any]:
    lo, hi = inbox_replay.default_period(st)
    con = _connect(request)
    try:
        srcs = inbox_replay.sources(con, st)
        est = None
        if source and con is not None:
            est = inbox_replay.estimate(con, source, date_from or lo, date_to or hi)
    finally:
        if con is not None:
            con.close()
    return {"sources": srcs, "default": {"from": lo.isoformat(), "to": hi.isoformat()},
            "store": con is not None, "estimate": est, "job": inbox_replay.job(st), "running": inbox_replay.running(),
            "max_days": inbox_replay.MAX_DAYS}


@router.get("/api/v1/inbox/replay")
def get_replay(request: Request, source: str | None = None,
               from_: Annotated[str | None, Query(alias="from")] = None, to: str | None = None) -> dict[str, Any]:
    st = _settings()
    return payload(request, st, source, _day(from_, "from"), _day(to, "to"))


@router.post("/api/v1/inbox/replay", status_code=202)
def post_replay(body: JsonBody, request: Request) -> dict[str, Any]:
    st = _settings()
    source = body.get("source")
    if not isinstance(source, str) or not source:
        raise ApiError(422, "invalid_parameter", "source is required")
    lo, hi = _day(body.get("from"), "from"), _day(body.get("to"), "to")
    if lo is None or hi is None:
        d0, d1 = inbox_replay.default_period(st)
        lo, hi = lo or d0, hi or d1
    if (why := inbox_replay.check_period(lo, hi)) is not None:
        raise ApiError(422, "invalid_parameter", why)
    if source not in {s["source"] for s in inbox_replay.sources(None, st)}:
        raise ApiError(404, "not_found", f"the adapter declares no source {source}")
    try:
        inbox_replay.start(st, source, lo, hi, getattr(request.app.state, "store_url", None),
                           getattr(request.app.state, "autopilot", None), reason=body.get("reason")
                           if isinstance(body.get("reason"), str) else None)
    except adapter_jobs.Busy:
        raise ApiError(409, "invalid_parameter", "a replay is running: wait for its result") from None
    return payload(request, st, source, lo, hi)
