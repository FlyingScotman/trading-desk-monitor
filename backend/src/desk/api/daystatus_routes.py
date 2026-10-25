"""Day status (S7b, issue #25; ``desk.daystatus``): the monitors' «Статус дня» screen, and the live feed (#28).

* ``GET /api/v1/inbox?on=YYYY-MM-DD``: the date's inputs, closes, files, rejections with their reasons, runs and what
  superseded what, per book where the positions went (``inbox.scan.reconcile``), the date's events.
* ``GET /api/v1/inbox/days?to=&n=``: a line per business date for the screen's strip.
* ``GET /api/v1/events?after=&before=&on=&type=&limit=``: the event feed (JSON pages by ``event_id``).
* ``GET /api/v1/events/stream?after=&type=``: the same feed as Server-Sent Events (#28): ``id:`` = ``event_id``,
  ``event:`` = the type, ``data:`` = the event as in the JSON pages (one line). It opens with ``event: hello``
  (no ``id:``, ``data: {"after": <cursor>}``): a client that gets no hello knows a proxy holds the response back and
  polls instead (``prototypes/concepts/lib/live.js``). It resumes after ``Last-Event-ID``
  (what a browser's EventSource sends when it reconnects), else after ``?after=``, else from now. The server reads
  the feed every ``poll_s`` with ``daystatus.events``, the query a poll makes, so the stream and the poll see the
  same events in the same order. It writes a ``: keep-alive`` comment when it has nothing to say, and ends the
  response after ``lifetime_s`` (the browser reconnects by itself after ``retry:``, and the reconnect checks the
  session again), when the client goes away, or when the server is shutting down (``app.state.stopping``: an endless
  response would hold a restart).

All are read routes like the runs: every signed-in user, the ``viewer`` role included (owner's decision in #25:
nothing of the day status is hidden from viewers, book ids, file names and sources included); with sign-in off, this
computer, as every other page.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

import anyio
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

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


def _types(type: str | None) -> list[str] | None:
    return [t.strip() for t in (type or "").split(",") if t.strip()] or None


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
    with store.connect(request.app.state.store_url) as con:
        return _json(daystatus.events(con, after=after, before=before, on=day, types=_types(type), limit=limit))


# ---- the SSE stream (#28) ---------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamConfig:
    """The stream's timing (``app.state.events_stream``; the tests shorten it)."""

    poll_s: float = 2.0  # how often the feed is read
    keepalive_s: float = 15.0  # a comment after this long without an event (proxies drop idle connections)
    lifetime_s: float = 600.0  # one response at most this long; the browser reconnects with Last-Event-ID
    retry_ms: int = 3000  # the browser's reconnect delay (``retry:``)
    page: int = 200  # events per read; a longer backlog is read page after page without waiting


def sse(e: dict[str, Any]) -> str:
    """One event in the SSE wire format: ``id:`` = ``event_id``, ``event:`` = the type, ``data:`` = one JSON line."""
    data = json.dumps(e, ensure_ascii=False, separators=(",", ":"))
    return f"id: {e['id']}\nevent: {e['type']}\ndata: {data}\n\n"


def cursor_of(last_event_id: str | None, after: int | None) -> int | None:
    """Where the stream starts: ``Last-Event-ID`` (a reconnect) over ``?after=``; None: from now."""
    if last_event_id and last_event_id.strip():
        try:
            return int(last_event_id.strip())
        except ValueError:
            raise HTTPException(400, "Last-Event-ID must be an event id") from None
    return after


async def stream(url: str, cursor: int | None, types: list[str] | None, cfg: StreamConfig,
                 gone: Callable[[], Awaitable[bool]], stopping: Callable[[], bool]) -> AsyncIterator[str]:
    """The SSE body (module doc). ``gone``: has the client left (``request.is_disconnected``); ``stopping``: the
    server is shutting down. The store is read in a worker thread, on one connection per response, closed however
    the response ends (a client that leaves cancels the body; ``finally`` still runs)."""
    con = await anyio.to_thread.run_sync(store.connect, url)
    try:
        if cursor is None:  # from now: the newest event in the store, whatever the filter
            cursor = (await anyio.to_thread.run_sync(lambda: daystatus.events(con, limit=1)))["last_id"] or 0
        yield f"retry: {cfg.retry_ms}\nevent: hello\ndata: {json.dumps({'after': cursor})}\n\n"
        start = quiet = time.monotonic()
        while True:
            if stopping() or await gone():
                return
            page = await anyio.to_thread.run_sync(
                lambda c=cursor: daystatus.events(con, after=c, types=types, limit=cfg.page))
            if page["events"]:
                cursor = page["events"][-1]["id"]
                yield "".join(sse(e) for e in page["events"])
                quiet = time.monotonic()
                if page["has_more"]:
                    continue
            now = time.monotonic()
            if now - start >= cfg.lifetime_s:
                return
            if now - quiet >= cfg.keepalive_s:
                yield ": keep-alive\n\n"
                quiet = now
            await asyncio.sleep(cfg.poll_s)
    finally:
        await anyio.to_thread.run_sync(con.close)


@public.get("/api/v1/events/stream")
async def get_events_stream(request: Request, after: int | None = None, type: str | None = None) -> StreamingResponse:
    """The feed as Server-Sent Events (module doc): what follows ``Last-Event-ID`` or ``after``, then what comes."""
    cursor = cursor_of(request.headers.get("last-event-id"), after)
    state = request.app.state
    cfg: StreamConfig = getattr(state, "events_stream", None) or StreamConfig()
    stopping: Callable[[], bool] = getattr(state, "stopping", None) or (lambda: False)
    body = stream(state.store_url, cursor, _types(type), cfg, request.is_disconnected, stopping)
    # no-transform and X-Accel-Buffering: a reverse proxy passes the events on as they come (05 §2)
    return StreamingResponse(body, media_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
