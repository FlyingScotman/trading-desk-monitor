"""Day status (S7b, issue #25; ``desk.daystatus``): the monitors' «Статус дня» screen.

* ``GET /api/v1/inbox?on=YYYY-MM-DD``: the date's inputs, closes, files, rejections with their reasons, runs and what
  superseded what, per book where the positions went (``inbox.scan.reconcile``), the date's events.
* ``GET /api/v1/inbox/days?to=&n=``: a line per business date for the screen's strip.
* ``GET /api/v1/events?after=&before=&on=&type=&limit=``: the event feed (JSON pages by ``event_id``). The SSE
  stream of #28 reuses ``daystatus.events`` with ``after`` = ``Last-Event-ID``.

All three are read routes like the runs: every signed-in user, the ``viewer`` role included (owner's decision in #25:
nothing of the day status is hidden from viewers, book ids, file names and sources included); with sign-in off, this
computer, as every other page.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .. import daystatus, store
from ..settings import SettingsError, load

public = APIRouter()
NO_STORE = {"Cache-Control": "no-store"}


def _date(v: str | None, name: str) -> date | None:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        raise HTTPException(400, f"{name} must be YYYY-MM-DD") from None


def _json(doc: dict) -> JSONResponse:
    return JSONResponse(doc, headers=NO_STORE)


def _url(request: Request):
    try:
        settings = load()
    except SettingsError as e:
        raise HTTPException(500, f"settings: {e}") from None
    return settings, request.app.state.store_url or store.resolve_url(settings)


@public.get("/api/v1/inbox")
def get_day(request: Request, on: str | None = None) -> JSONResponse:
    """The day status of ``on`` (default: the date of the latest default run, else today, Moscow)."""
    day = _date(on, "on")
    settings, url = _url(request)
    with store.connect(url) as con:
        return _json(daystatus.day(con, settings, day))


@public.get("/api/v1/inbox/days")
def get_days(request: Request, to: str | None = None, n: Annotated[int, Query(ge=1, le=400)] = 30) -> JSONResponse:
    """The dates ``n`` calendar days back from ``to`` (default today) with anything on them, newest first."""
    day = _date(to, "to")
    settings, url = _url(request)
    with store.connect(url) as con:
        return _json(daystatus.days(con, settings, day, n))


@public.get("/api/v1/events")
def get_events(request: Request, after: int | None = None, before: int | None = None, on: str | None = None,
               type: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100) -> JSONResponse:
    """A page of the feed (``daystatus.events``): with ``after`` the next events, oldest first (a poll); else the
    newest ``limit`` (before ``before``). ``type``: comma-separated type prefixes (``run.,input.rejected``)."""
    day = _date(on, "on")
    types = [t.strip() for t in (type or "").split(",") if t.strip()] or None
    with store.connect(request.app.state.store_url) as con:
        return _json(daystatus.events(con, after=after, before=before, on=day, types=types, limit=limit))
