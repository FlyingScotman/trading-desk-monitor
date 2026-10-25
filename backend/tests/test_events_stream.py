"""Live updates (#28): ``GET /api/v1/events/stream``, the event feed as Server-Sent Events.

The stream reads the feed with ``daystatus.events`` (the query of a poll), so what it sends is what ``/api/v1/events``
pages give, event for event; it resumes after ``Last-Event-ID`` or ``?after=``, keeps the connection alive with
comments, and ends when the client leaves, the server stops or its lifetime is over. Synthetic events only."""

from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from desk import store
from desk.api import auth, create_app
from desk.api import daystatus_routes as routes

FAST = routes.StreamConfig(poll_s=0.02, keepalive_s=0.1, lifetime_s=0.5, retry_ms=1500)


def add(url: str, type_: str = "input.accepted", **kw) -> int:
    cols = {"type": type_, "source": "demo_source", "dataset": "positions", "valid_date": "2026-10-20", **kw}
    if "detail" in cols:
        cols["detail"] = json.dumps(cols["detail"])
    with store.connect(url) as con:
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
    with client.stream("GET", "/api/v1/events/stream", params={"after": ids[0]}) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "no-store" in r.headers["cache-control"] and r.headers["x-accel-buffering"] == "no"
        assert "content-encoding" not in r.headers  # never gzipped: the events go out as they come
        events, comments = parse(r.read().decode())
    assert [int(e["id"]) for e in events] == ids[1:]
    assert [e["event"] for e in events] == ["input.rejected", "run.published"]
    page = client.get("/api/v1/events", params={"after": ids[0]}).json()["events"]
    assert [e["data"] for e in events] == page  # the same events as a poll, field for field
    assert events[1]["data"]["text"].startswith("Прогон закрытия #7") and events[0]["data"]["level"] == "error"
    assert comments[:2] == ["retry 1500", f"hello after {ids[0]}"]
    assert "keep-alive" in comments  # quiet after the backlog: a comment, not silence

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
    later: list[int] = []

    def publish() -> None:
        time.sleep(0.15)
        later.append(add(store_url, "run.published", run_id=9, dataset=None, source=None,
                         detail={"run_type": "INTRADAY", "cut_type": "intraday"}))

    t = threading.Thread(target=publish)
    t.start()
    with client.stream("GET", "/api/v1/events/stream") as r:  # no cursor: from now, the old event is not resent
        events, comments = parse(r.read().decode())
    t.join()
    assert f"hello after {old}" in comments
    assert [(int(e["id"]), e["event"]) for e in events] == [(later[0], "run.published")]
    assert events[0]["data"]["run_id"] == 9


def test_a_backlog_longer_than_a_page_comes_whole_and_in_order(store_url, client):
    ids = [add(store_url) for _ in range(7)]
    client.app.state.events_stream = routes.StreamConfig(poll_s=0.02, keepalive_s=1, lifetime_s=0.2, page=3)
    with client.stream("GET", "/api/v1/events/stream", params={"after": 0}) as r:
        assert [int(e["id"]) for e in parse(r.read().decode())[0]] == ids


class Spy:
    """``store.connect`` that remembers its connections: the stream must close its one connection."""

    def __init__(self, url: str):
        self.connect, self.cons = store.connect, []

    def __call__(self, url: str):
        c = self.connect(url)
        self.cons.append(c)
        return c


def run(agen, n: int | None = None) -> list[str]:
    async def go():
        out = []
        async for chunk in agen:
            out.append(chunk)
            if n is not None and len(out) >= n:
                await agen.aclose()  # what a client that leaves does to the response's body
                break
        return out
    return asyncio.run(go())


def test_the_stream_ends_and_closes_its_connection_when_the_client_leaves_or_the_server_stops(store_url, monkeypatch):
    add(store_url)
    spy = Spy(store_url)
    monkeypatch.setattr(routes.store, "connect", spy)
    slow = routes.StreamConfig(poll_s=0.01, keepalive_s=60, lifetime_s=60)

    async def here() -> bool:
        return False

    async def left() -> bool:
        return True

    # the client has gone: only the greeting, then the end
    out = run(routes.stream(store_url, 0, None, slow, left, lambda: False))
    assert len(out) == 1 and out[0].startswith("retry:")
    # the server is shutting down (serve's app.state.stopping)
    stop = [False]
    agen = routes.stream(store_url, 0, None, slow, here, lambda: stop[0])

    async def until_stopped():
        got = []
        async for chunk in agen:
            got.append(chunk)
            stop[0] = chunk.startswith("id: ")  # after the first event: the next turn sees the server stopping
        return got
    got = asyncio.run(until_stopped())
    assert len(got) == 2 and got[1].startswith("id: ")
    # the body cancelled mid-stream (the client's socket closed): finally still closes the connection
    run(routes.stream(store_url, 0, None, slow, here, lambda: False), n=2)
    assert len(spy.cons) == 3 and all(c.closed for c in spy.cons)


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
