"""Live updates (#28): ``GET /api/v1/events/stream``, the event feed as Server-Sent Events.

The stream reads the feed with ``daystatus.events`` (the query of a poll), so what it sends is what ``/api/v1/events``
pages give, event for event; it resumes after ``Last-Event-ID`` or ``?after=``, keeps the connection alive with
comments, and ends when the client leaves, the server stops or its lifetime is over. Synthetic events only."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import auth, create_app
from desk.api import daystatus_routes as routes

FAST = routes.StreamConfig(poll_s=0.02, keepalive_s=0.1, lifetime_s=0.5, retry_ms=1500)


CONNECT = store.connect  # the test's own writes: not counted by a Spy on ``store.connect`` (#338)


def add(url: str, type_: str = "input.accepted", **kw) -> int:
    cols = {"type": type_, "source": "demo_source", "dataset": "positions", "valid_date": "2026-10-20", **kw}
    if "detail" in cols:
        cols["detail"] = json.dumps(cols["detail"])
    with CONNECT(url) as con:
        names = ", ".join(cols)
        marks = ", ".join("%s" for _ in cols)
        return con.execute(f"INSERT INTO event ({names}) VALUES ({marks}) RETURNING event_id",
                           list(cols.values())).fetchone()[0]


def parse(body: str) -> tuple[list[dict], list[str]]:
    """The feed's events (id, event, data parsed) of an SSE body, and the rest: comments, ``retry``, ``hello``."""
    events, other = [], []
    for block in body.split("\n\n"):
        e: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):
                other.append(line[1:].strip())
            elif line:
                k, _, v = line.partition(": ")
                e[k] = v
        if "retry" in e:
            other.append("retry " + e["retry"])
        if e.get("event") == "hello":
            assert "id" not in e  # the greeting moves no cursor (Last-Event-ID stays the last event's)
            other.append(f"hello after {json.loads(e['data'])['after']}")
        elif "data" in e:
            e["data"] = json.loads(e["data"])
            events.append(e)
    return events, other


@pytest.fixture
def client(store_url):
    app = create_app(store_url)
    app.state.events_stream = FAST
    return TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_the_stream_sends_what_the_feed_pages_give_and_resumes_after_last_event_id(store_url, client):
    ids = [add(store_url), add(store_url, "input.rejected"),
           add(store_url, "run.published", run_id=7, dataset=None, source=None,
               detail={"run_type": "EOD", "cut_type": "eod_final", "supersedes_run_id": 5})]
    # #338: the client reads until the keep-alive has come, not for the stream's lifetime (a backlog sent late on a
    # slow runner left less than keepalive_s of it); the lifetime only bounds a stream that never sends one
    client.app.state.events_stream = routes.StreamConfig(poll_s=0.02, keepalive_s=0.05, lifetime_s=30, retry_ms=1500)
    body = ""
    with client.stream("GET", "/api/v1/events/stream", params={"after": ids[0]}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "no-store" in r.headers["cache-control"] and r.headers["x-accel-buffering"] == "no"
        assert "content-encoding" not in r.headers  # never gzipped: the events go out as they come
        for chunk in r.iter_text():  # one chunk per message the server sends
            body += chunk
            if chunk.startswith(": keep-alive"):
                break
    events, comments = parse(body)
    assert [int(e["id"]) for e in events] == ids[1:]
    assert [e["event"] for e in events] == ["input.rejected", "run.published"]
    page = client.get("/api/v1/events", params={"after": ids[0]}).json()["events"]
    assert [e["data"] for e in events] == page  # the same events as a poll, field for field
    assert events[1]["data"]["text"].startswith("Прогон закрытия #7") and events[0]["data"]["level"] == "error"
    assert comments[:2] == ["retry 1500", f"hello after {ids[0]}"]
    assert "keep-alive" in comments  # quiet after the backlog: a comment, not silence

    # the streams below are read to their end: their events are the backlog, sent before the lifetime is checked
    client.app.state.events_stream = FAST
    # a reconnect: Last-Event-ID (what EventSource sends) wins over the URL's after
    with client.stream("GET", "/api/v1/events/stream", params={"after": 0},
                       headers={"Last-Event-ID": str(ids[1])}) as r:
        assert [int(e["id"]) for e in parse(r.read().decode())[0]] == ids[2:]
    # the type prefixes of the feed
    with client.stream("GET", "/api/v1/events/stream", params={"after": 0, "type": "run."}) as r:
        assert [e["event"] for e in parse(r.read().decode())[0]] == ["run.published"]
    assert client.get("/api/v1/events/stream", headers={"Last-Event-ID": "x"}).status_code == 400


def test_from_now_a_new_run_arrives_while_the_stream_is_open(store_url, client):
    old = add(store_url)
    # #282: the run is published once the greeting has come (the cursor is set), not after a fixed delay; the
    # lifetime only bounds a stream that never gets it, the client leaves as soon as it has
    client.app.state.events_stream = routes.StreamConfig(poll_s=0.02, keepalive_s=0.1, lifetime_s=30)
    later, body = None, ""
    with client.stream("GET", "/api/v1/events/stream") as r:  # no cursor: from now, the old event is not resent
        for chunk in r.iter_text():  # one chunk per message the server sends
            body += chunk
            if later is None and "event: hello" in body:
                later = add(store_url, "run.published", run_id=9, dataset=None, source=None,
                            detail={"run_type": "INTRADAY", "cut_type": "intraday"})
            elif later is not None and "event: run.published" in chunk:
                break
    events, comments = parse(body)
    assert f"hello after {old}" in comments
    assert [(int(e["id"]), e["event"]) for e in events] == [(later, "run.published")]
    assert events[0]["data"]["run_id"] == 9


def test_a_backlog_longer_than_a_page_comes_whole_and_in_order(store_url, client):
    ids = [add(store_url) for _ in range(7)]
    client.app.state.events_stream = routes.StreamConfig(poll_s=0.02, keepalive_s=1, lifetime_s=0.2, page=3)
    with client.stream("GET", "/api/v1/events/stream", params={"after": 0}) as r:
        assert [int(e["id"]) for e in parse(r.read().decode())[0]] == ids




def test_a_stream_far_behind_the_shared_window_reads_its_gap_itself(store_url, client):
    ids = [add(store_url) for _ in range(9)]
    client.app.state.events_stream = routes.StreamConfig(poll_s=0.02, keepalive_s=1, lifetime_s=0.3, page=2, keep=3)
    with client.stream("GET", "/api/v1/events/stream") as r:  # the reader starts at the newest event
        assert parse(r.read().decode())[0] == []
    with client.stream("GET", "/api/v1/events/stream", params={"after": ids[1]}) as r:
        assert [int(e["id"]) for e in parse(r.read().decode())[0]] == ids[2:]


class Spy:
    """``store.connect`` that remembers its connections, and how many were open at once (counted as each one opens,
    #338: not sampled in a window of time). A connection is listed before it is made: two that open together see
    each other. A connect that fails leaves no entry (#350): its error is raised as it is, not read later as a
    connection left open."""

    def __init__(self, connect=None):
        self.connect, self.cons, self.most = connect or store.connect, [], 0

    def __call__(self, url: str):
        opening = SimpleNamespace(closed=False)  # its own placeholder: found by identity, whatever opens beside it
        self.cons.append(opening)
        self.most = max(self.most, self.open())
        try:
            c = self.connect(url)
        except BaseException:
            del self.cons[self._slot(opening)]
            raise
        self.cons[self._slot(opening)] = c
        return c

    def _slot(self, entry) -> int:
        return next(i for i, c in enumerate(self.cons) if c is entry)

    def open(self) -> int:
        return sum(not c.closed for c in self.cons)


def test_a_failed_connect_leaves_no_open_connection_in_the_spy():
    """#350: the error of connect comes out as it is, and the spy holds no placeholder that reads as a leak."""
    class Down(Exception):
        pass

    opened = SimpleNamespace(closed=False)
    calls = iter([opened, Down("store is down")])

    def connect(url):
        x = next(calls)
        if isinstance(x, Exception):
            raise x
        return x

    spy = Spy(connect)
    assert spy("u") is opened
    with pytest.raises(Down, match="store is down"):
        spy("u")
    assert spy.cons == [opened] and spy.cons[0] is opened and spy.open() == 1 and spy.most == 2
    opened.closed = True
    assert spy.open() == 0


async def until(cond, timeout: float = 5) -> None:
    """Wait for ``cond()`` (#282: the tests wait for what they need, not for a fixed time)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        assert loop.time() < end, "timed out"
        await asyncio.sleep(0.01)


async def stopped(feed) -> None:
    """Until the feed's reader has stopped (it does after its next read once no stream is open)."""
    if feed.task is not None:
        await asyncio.wait_for(asyncio.shield(feed.task), 5)
    assert feed.task is None


async def here() -> bool:
    return False


async def left() -> bool:
    return True


def test_open_streams_share_one_reader_and_hold_no_connection_between_reads(store_url, monkeypatch):
    spy = Spy()
    monkeypatch.setattr(routes.store, "connect", spy)
    cfg = routes.StreamConfig(poll_s=0.05, keepalive_s=60, lifetime_s=60)
    sleep, naps, reader = asyncio.sleep, [], []

    async def nap(delay, *a, **kw):  # the reader's sleep between reads: how many connections are open then (#338)
        if reader and asyncio.current_task() is reader[0].task:
            naps.append(spy.open())
        return await sleep(delay, *a, **kw)

    monkeypatch.setattr(routes.asyncio, "sleep", nap)

    async def go():
        feed = routes.Feed(store_url, cfg)
        reader.append(feed)
        await feed.start()
        streams = [routes.stream(feed, None, None, here, lambda: False) for _ in range(5)]
        got: list[list[str]] = [[] for _ in streams]

        async def read(i, s):
            async for chunk in s:
                got[i].append(chunk)

        t0 = time.monotonic()
        tasks = [asyncio.create_task(read(i, s)) for i, s in enumerate(streams)]
        await until(lambda: all(got))  # every stream has its greeting: its cursor is set before the run comes
        n = len(spy.cons)
        await until(lambda: len(spy.cons) >= n + 4)  # quiet: the reader reads and sleeps (#338: not a window)
        await asyncio.to_thread(add, store_url, "run.published", run_id=3, dataset=None, source=None,
                                detail={"run_type": "EOD"})
        await until(lambda: all(any("run.published" in c for c in g) for g in got))
        for t in tasks:
            t.cancel()  # the clients leave: the bodies are cancelled
        await asyncio.gather(*tasks, return_exceptions=True)
        await stopped(feed)
        return feed, got, time.monotonic() - t0

    feed, got, took = asyncio.run(go())
    assert all(any(c.startswith("id: ") and "run.published" in c for c in g) for g in got)  # every stream got it
    assert spy.most <= 1  # one reader: one short connection at a time, whatever the number of streams
    assert len(naps) >= 3 and set(naps) == {0}  # while the reader sleeps no connection is open: none held
    assert len(spy.cons) <= 6 + took / 0.05 + 2  # reads of one reader (and each start), not five readers
    assert all(c.closed for c in spy.cons)
    assert feed.users == 0 and feed.task is None  # the last stream gone, the reader stops


async def first(agen, n: int) -> list[str]:
    """The first ``n`` chunks of a stream, then the client leaves."""
    out = []
    async for chunk in agen:
        out.append(chunk)
        if len(out) >= n:
            break
    await agen.aclose()
    return out


def hello_after(chunk: str) -> int:
    return json.loads(chunk.split("data: ", 1)[1])["after"]


def test_from_now_after_the_reader_was_idle_starts_at_the_newest_event(store_url):
    # review of #191: «from now» after an idle spell gave the old cursor and replayed what came meanwhile
    cfg = routes.StreamConfig(poll_s=0.02, keepalive_s=60, lifetime_s=60)
    old = add(store_url)

    async def go():
        feed = routes.Feed(store_url, cfg)
        (hello,) = await first(routes.stream(feed, None, None, here, lambda: False), 1)
        assert hello_after(hello) == old
        await stopped(feed)  # no streams: the reader stops
        new = [await asyncio.to_thread(add, store_url) for _ in range(2)]
        # from now, not from before the idle spell: the greeting, then the next event, not 6 and 7 again
        agen = routes.stream(feed, None, None, here, lambda: False)
        hello = await asyncio.wait_for(anext(agen), 5)
        assert hello_after(hello) == new[-1]
        later = await asyncio.to_thread(add, store_url)  # after the greeting (#282: not after a fixed delay)
        nxt = await asyncio.wait_for(anext(agen), 5)
        await agen.aclose()
        assert nxt.startswith(f"id: {later}\n")

    asyncio.run(go())


def test_the_reader_logs_an_unexpected_error_and_goes_on(store_url, caplog):
    cfg = routes.StreamConfig(poll_s=0.02, keepalive_s=60, lifetime_s=60)

    async def go():
        feed = routes.Feed(store_url, cfg)
        await feed.start()
        read, fails = feed.read, [2]

        def flaky(after, limit):
            if after is not None and fails[0]:  # the reader's reads, not start()
                fails[0] -= 1
                raise RuntimeError("not the store")
            return read(after, limit)

        feed.read = flaky
        agen = routes.stream(feed, None, None, here, lambda: False)
        got = [await asyncio.wait_for(anext(agen), 5)]  # the greeting: the stream's cursor is set (#282)
        eid = await asyncio.to_thread(add, store_url, "run.published", run_id=8, dataset=None, source=None,
                                      detail={"run_type": "EOD"})
        got.append(await asyncio.wait_for(anext(agen), 5))  # only a read after both failures gets it
        await agen.aclose()
        return got, eid, fails[0]

    got, eid, left = asyncio.run(go())
    assert left == 0 and got[1].startswith(f"id: {eid}\n")  # the event came after the errors: the reader lives
    assert sum("the feed not read" in r.message for r in caplog.records) == 2


def test_the_stream_ends_when_the_client_leaves_or_the_server_stops(store_url):
    add(store_url)
    cfg = routes.StreamConfig(poll_s=0.01, keepalive_s=60, lifetime_s=60)

    async def go():
        feed = routes.Feed(store_url, cfg)
        out = [c async for c in routes.stream(feed, 0, None, left, lambda: False)]
        assert len(out) == 1 and out[0].startswith("retry:")  # the client has gone: the greeting, then the end
        stop = [False]
        got = []
        async for chunk in routes.stream(feed, 0, None, here, lambda: stop[0]):  # serve's app.state.stopping
            got.append(chunk)
            stop[0] = chunk.startswith("id: ")
        assert len(got) == 2 and got[1].startswith("id: ")
        assert feed.users == 0

    asyncio.run(go())


def test_a_store_that_is_down_gives_503_before_the_stream_starts(store_url, monkeypatch):
    app = create_app(store_url)
    app.state.events_stream = FAST

    def down(url):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(routes.store, "connect", down)
    r = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000)).get("/api/v1/events/stream")
    assert r.status_code == 503 and r.json()["code"] == "store_unavailable"


def test_signed_in_users_only_when_sign_in_is_on(store_url):
    with store.connect(store_url) as con:
        auth.add_user(con, "vic", "viewer", "viewer-pass-1", actor="test")
    app = create_app(store_url, auth=auth.AuthConfig(enabled=True))
    app.state.events_stream = FAST
    remote = TestClient(app, base_url="http://10.0.0.7:8000", client=("10.0.0.7", 50000))
    assert remote.get("/api/v1/events/stream").status_code == 401
    assert remote.post("/api/v1/auth/login", json={"username": "vic", "password": "viewer-pass-1"}).status_code == 200
    with remote.stream("GET", "/api/v1/events/stream") as r:  # a viewer too (#25: the feed is open to every user)
        assert r.status_code == 200 and r.read().decode().startswith("retry:")
