"""Day status (S7b, issue #25; ``desk.daystatus``): the monitors' «Статус дня» screen, and the live feed (#28).

* ``GET /api/v1/inbox?on=YYYY-MM-DD``: the date's inputs, closes, files, rejections with their reasons, runs and what
  superseded what, per book where the positions went (``inbox.scan.reconcile``), the date's events.
* ``GET /api/v1/inbox/days?to=&n=``: a line per business date for the screen's strip.
* ``GET /api/v1/inbox/datasets/{dataset}?on=``: why a dataset is not in force on a date: the files of its sources,
  the latest with its status and codes, the files never read (C16 «отчёта ОВП нет», TDM#445).
* ``GET /api/v1/events?after=&before=&on=&type=&limit=``: the event feed (JSON pages by ``event_id``).
* ``GET /api/v1/events/stream?after=&type=``: the same feed as Server-Sent Events (#28): ``id:`` = ``event_id``,
  ``event:`` = the type, ``data:`` = the event as in the JSON pages (one line). It opens with ``event: hello``
  (no ``id:``, ``data: {"after": <cursor>}``): a client that gets no hello knows a proxy holds the response back and
  polls instead (``prototypes/concepts/lib/live.js``). It resumes after ``Last-Event-ID``
  (what a browser's EventSource sends when it reconnects), else after ``?after=``, else from now. One reader per
  process (``Feed``) reads the feed every ``poll_s`` with ``daystatus.events``, the query a poll makes, on a short
  connection, and wakes every open stream: the stream and the poll see the same events in the same order, and an
  open stream holds no store connection. A store that is down when the stream opens gives a 503. It writes a ``: keep-alive`` comment when it has nothing to say, and ends the
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
import logging
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Annotated, Any

import anyio
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .. import daystatus, store
from ..settings import SettingsError, load
from . import auth

log = logging.getLogger(__name__)
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


@public.get("/api/v1/inbox/datasets/{dataset}")
def get_dataset_files(request: Request, dataset: str, on: str | None = None) -> JSONResponse:
    """Why ``dataset`` is or is not in force on ``on`` (default today): its sources' files and the latest of them,
    the files never read, what is in force now (TDM#445)."""
    if not dataset.replace("_", "").isalnum() or len(dataset) > 64:
        raise HTTPException(400, "dataset must be a dataset name")
    day = _date(on, "on")
    settings, url = _url(request)
    with store.connect(url) as con:
        doc = daystatus.dataset_files(con, settings, dataset, day)
    try:  # the settings page answers this user (an admin, or this computer with sign-in off): the page may link it
        auth.require_admin(request, ())
        doc["settings_link"] = True
    except Exception:  # noqa: BLE001 - ApiError 401/403: a viewer, or another computer
        doc["settings_link"] = False
    return _json(doc)


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
    keep: int = 2000  # the reader's window of recent events; a stream further behind reads its gap itself


class Feed:
    """One reader of the feed per process (per event loop), shared by every open stream: while any stream is open it
    reads the events after the newest it has seen every ``poll_s`` (``daystatus.events``) on a short connection of
    its own, keeps the recent ones and wakes the streams. A stream holds no connection between reads; one that
    resumes from further back than the window (``since`` gives None) reads its gap itself, on a short connection,
    page by page. So the store sees one read per ``poll_s`` whatever the number of streams (review of #191: a
    connection per stream for 10 minutes would use up ``max_connections`` with a few dozen tabs)."""

    def __init__(self, url: str, cfg: StreamConfig):
        self.url, self.cfg = url, cfg
        self.loop = asyncio.get_running_loop()
        self.last: int | None = None  # the newest event id read
        self.base = 0  # every event after base that the reader has read is in recent
        self.recent: deque[dict[str, Any]] = deque(maxlen=cfg.keep)
        self.cond = asyncio.Condition()
        self.starting = asyncio.Lock()
        self.users = 0
        self.task: asyncio.Task | None = None

    def read(self, after: int | None, limit: int) -> dict[str, Any]:
        """A page of the feed on a connection opened and closed for it (a worker thread)."""
        with store.connect(self.url) as con:
            return daystatus.events(con, after=after, limit=limit)

    async def start(self) -> None:
        """Where the window starts: the newest event now, read again whenever the reader is not running (after
        an idle spell ``last`` is old: a stream «from now» would replay what came meanwhile). Raises the store's
        error before the response starts (a 503 ``store_unavailable``, not a broken 200)."""
        async with self.starting:  # one read for streams that open together (the first one starts the reader)
            if self.last is None or self.task is None:
                last = (await anyio.to_thread.run_sync(self.read, None, 1))["last_id"] or 0
                if self.task is None:  # the window restarts here: a stream from further back reads its gap itself
                    self.recent.clear()
                    self.last = self.base = last

    def since(self, pos: int) -> list[dict[str, Any]] | None:
        """The window's events after ``pos``; None when ``pos`` is older than the window."""
        if pos < self.base:
            return None
        return [e for e in self.recent if e["id"] > pos]

    async def _run(self) -> None:
        try:
            while self.users > 0:
                try:
                    page = await anyio.to_thread.run_sync(self.read, self.last, self.cfg.page)
                except psycopg.Error as e:  # the store went away: the streams keep their keep-alives, the reader retries
                    log.warning("event stream: the feed not read (%s: %s)", type(e).__name__, e)
                    await asyncio.sleep(self.cfg.poll_s)
                    continue
                except Exception:  # anything else: logged, and the reader goes on (it must not stop silently)
                    log.exception("event stream: the feed not read")
                    await asyncio.sleep(self.cfg.poll_s)
                    continue
                if page["events"]:
                    self.recent.extend(page["events"])
                    if len(self.recent) == self.cfg.keep:  # the oldest dropped out: a stream behind them reads its gap
                        self.base = max(self.base, self.recent[0]["id"] - 1)
                    self.last = page["events"][-1]["id"]
                    async with self.cond:
                        self.cond.notify_all()
                    if page["has_more"]:
                        continue
                await asyncio.sleep(self.cfg.poll_s)
        finally:
            self.task = None

    def join(self) -> None:
        self.users += 1
        if self.task is None:
            self.task = self.loop.create_task(self._run())

    def leave(self) -> None:
        self.users -= 1

    async def wait(self, timeout: float) -> None:
        """Until the reader has read something new, at most ``timeout`` seconds."""
        try:
            async with self.cond:
                await asyncio.wait_for(self.cond.wait(), timeout)
        except TimeoutError:
            pass


def feed_of(state: Any, url: str, cfg: StreamConfig) -> Feed:
    """The process's reader (``app.state.events_feed``), one per event loop (a test client runs a loop per request)."""
    feed = getattr(state, "events_feed", None)
    if feed is None or feed.loop is not asyncio.get_running_loop() or feed.cfg != cfg:
        feed = state.events_feed = Feed(url, cfg)
    return feed


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


def _wanted(e: dict[str, Any], types: list[str] | None) -> bool:
    """``daystatus.events``' type filter: each prefix matched as the start of the type."""
    return not types or any(e["type"].startswith(t) for t in types)


async def stream(feed: Feed, cursor: int | None, types: list[str] | None, gone: Callable[[], Awaitable[bool]],
                 stopping: Callable[[], bool]) -> AsyncIterator[str]:
    """The SSE body (module doc) over the process's reader (``Feed``). ``gone``: has the client left
    (``request.is_disconnected``); ``stopping``: the server is shutting down."""
    cfg = feed.cfg
    await feed.start()
    pos = feed.last if cursor is None else cursor
    feed.join()
    try:
        yield f"retry: {cfg.retry_ms}\nevent: hello\ndata: {json.dumps({'after': pos})}\n\n"
        start = quiet = time.monotonic()
        while True:
            if stopping() or await gone():
                return
            evs, more = feed.since(pos), False
            if evs is None:  # behind the window (a reconnect after a long gap): its own short read
                page = await anyio.to_thread.run_sync(feed.read, pos, cfg.page)
                evs, more = page["events"], page["has_more"]
                if not more:  # all after pos is read: the window covers the rest
                    pos = max(pos, feed.base)
            if evs:
                pos = max(pos, evs[-1]["id"])
                out = "".join(sse(e) for e in evs if _wanted(e, types))
                if out:
                    yield out
                    quiet = time.monotonic()
                if more:
                    continue
            now = time.monotonic()
            if now - start >= cfg.lifetime_s:
                return
            if now - quiet >= cfg.keepalive_s:
                yield ": keep-alive\n\n"
                quiet = now
            if feed.since(pos) == []:
                await feed.wait(cfg.poll_s)
    finally:
        feed.leave()


@public.get("/api/v1/events/stream")
async def get_events_stream(request: Request, after: int | None = None, type: str | None = None) -> StreamingResponse:
    """The feed as Server-Sent Events (module doc): what follows ``Last-Event-ID`` or ``after``, then what comes."""
    cursor = cursor_of(request.headers.get("last-event-id"), after)
    state = request.app.state
    cfg: StreamConfig = getattr(state, "events_stream", None) or StreamConfig()
    stopping: Callable[[], bool] = getattr(state, "stopping", None) or (lambda: False)
    feed = feed_of(state, state.store_url, cfg)
    await feed.start()  # the store's error here: a 503, before the response starts
    body = stream(feed, cursor, _types(type), request.is_disconnected, stopping)
    # no-transform and X-Accel-Buffering: a reverse proxy passes the events on as they come (05 §2)
    return StreamingResponse(body, media_type="text/event-stream; charset=utf-8",
                             headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"})
