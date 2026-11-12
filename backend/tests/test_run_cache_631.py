"""The finished bodies of the per-run API (TDM#631, ``desk.run_cache``): a repeat is a memory read, a conditional request a
304, the gzip is made once, the cache is bounded and forgets the runs that are deleted, and what a body also depends on
besides the run (the aliases of the positions) makes it a new body."""

from __future__ import annotations

import gzip
import threading
import time
from datetime import date, timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_api import book, run  # noqa: F401  (book: the module fixture of the golden runs)

from desk import run_cache, store
from desk.api import create_app
from desk.run_cache import RunBodyCache

LOCAL = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
ROUTES = ("positions", "risk", "pnl", "risk-table", "aggregates?dims=book,instr_kind", "funding", "ocp", "derivatives")


# ---- the cache itself ----------------------------------------------------------------------------------------------------


def test_a_body_is_built_once_per_key_and_token_and_its_gzip_and_etag_are_made_with_it() -> None:
    c, n = RunBodyCache(max_bytes=1 << 20), []

    def build() -> bytes:
        n.append(1)
        return b'{"x": 1}' * 100

    e = c.get_or_build(("positions", 7, None), ("complete", "t1", None), build)
    assert c.get_or_build(("positions", 7, None), ("complete", "t1", None), build) is e and len(n) == 1
    assert gzip.decompress(e.gz) == e.body and e.etag.startswith('"') and len(e.etag) == 34
    assert c.get(("positions", 7, "other"), ("complete", "t1", None)) is None  # another parameter: another key
    c.get_or_build(("positions", 7, None), ("complete", "t1", 5), build)  # the token changed: built again
    assert len(n) == 2 and c.stats()["items"] == 1  # replaced, not added


def test_the_cache_is_bounded_by_bytes_and_items_the_least_recently_used_goes() -> None:
    c = RunBodyCache(max_bytes=10_000, max_items=3)
    body = lambda: bytes(range(256)) * 4
    for i in range(3):
        c.get_or_build(("r", i), None, body)
    c.get(("r", 0), None)  # 0 is now the most recent
    c.get_or_build(("r", 3), None, body)  # a fourth: 1 (the least recently used) goes
    assert c.get(("r", 1), None) is None and all(c.get(("r", i), None) for i in (0, 2, 3))
    big = RunBodyCache(max_bytes=3_000)
    import os

    for i in range(10):
        big.get_or_build(("r", i), None, lambda: os.urandom(1000))  # incompressible: ~2 KB a body with its gzip
    assert big.bytes <= 3_000 and big.stats()["items"] <= 1
    off = RunBodyCache(max_bytes=0)  # off: built each time, nothing kept
    calls = []
    for _ in range(2):
        off.get_or_build(("r", 1), None, lambda: calls.append(1) or b"x")
    assert len(calls) == 2 and off.stats()["items"] == 0
    tiny = RunBodyCache(max_bytes=100)  # a body that alone does not fit is answered, not kept
    assert tiny.get_or_build(("r", 1), None, lambda: os.urandom(500)).body and tiny.stats()["items"] == 0


def test_runs_that_are_deleted_are_forgotten_in_every_cache_of_the_process() -> None:
    a, b = RunBodyCache(), RunBodyCache()
    for c in (a, b):
        for rid in (1, 2, 3):
            c.get_or_build(("positions", rid, None), None, lambda: b"x" * 100)
            c.get_or_build(("risk", rid), None, lambda: b"y" * 100)
    assert a.stats()["items"] == b.stats()["items"] == 6
    run_cache.forget_runs([2])  # (what it returns counts every cache of the process, other tests' too: not this test's)
    assert a.stats()["items"] == 4 and b.stats()["items"] == 4  # the two bodies of run 2 gone from each of the two caches
    assert a.get(("positions", 2, None), None) is None and a.get(("positions", 1, None), None)
    assert a.retain({1}) == 2 and {k[1] for k in a._items} == {1}  # a run deleted by another process
    assert a.bytes == sum(e.size for e in a._items.values())  # the byte count follows every drop
    a.clear()
    assert a.stats()["items"] == 0 and a.bytes == 0
    assert run_cache.forget_runs([]) == 0


def test_concurrent_requests_for_one_key_build_it_once() -> None:
    c, n = RunBodyCache(), []

    def build() -> bytes:
        n.append(1)
        time.sleep(0.3)
        return b"body"

    out = []
    ts = [threading.Thread(target=lambda: out.append(c.get_or_build(("risk", 1), None, build))) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(n) == 1 and len({id(e) for e in out}) == 1 and c._building == {}


def test_a_failed_build_leaves_no_gate_and_no_entry() -> None:
    c = RunBodyCache()

    def boom() -> bytes:
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        c.get_or_build(("risk", 1), None, boom)
    assert c._building == {} and c.stats()["items"] == 0
    assert c.get_or_build(("risk", 1), None, lambda: b"ok").body == b"ok"


def test_the_size_comes_from_the_environment(monkeypatch) -> None:
    monkeypatch.delenv(run_cache.ENV_MB, raising=False)
    assert run_cache.cache_bytes() == run_cache.DEFAULT_MB << 20
    monkeypatch.setenv(run_cache.ENV_MB, "8")
    assert run_cache.cache_bytes() == 8 << 20
    monkeypatch.setenv(run_cache.ENV_MB, "0")
    assert run_cache.cache_bytes() == 0
    monkeypatch.setenv(run_cache.ENV_MB, "lots")
    assert run_cache.cache_bytes() == run_cache.DEFAULT_MB << 20


# ---- the API --------------------------------------------------------------------------------------------------------------


@pytest.fixture
def client(store_url) -> TestClient:
    return TestClient(create_app(store_url), **LOCAL)


@pytest.mark.parametrize("route", ROUTES)
def test_the_second_answer_comes_from_the_cache_with_the_same_etag_and_a_304_for_it(store_url, book, client, route) -> None:  # noqa: F811
    rid = run(store_url, book)
    url = f"/api/v1/runs/{rid}/{route}"
    cache = client.app.state.run_cache
    first = client.get(url)
    assert first.status_code == 200 and first.headers["ETag"] and cache.stats()["misses"] == 1
    again = client.get(url)
    assert again.status_code == 200 and again.content == first.content and again.headers["ETag"] == first.headers["ETag"]
    assert cache.stats()["misses"] == 1 and cache.stats()["hits"] == 1  # the repeat built nothing
    cond = client.get(url, headers={"If-None-Match": first.headers["ETag"]})
    assert cond.status_code == 304 and cond.content == b"" and cond.headers["ETag"] == first.headers["ETag"]
    assert client.get(url, headers={"If-None-Match": '"other"'}).status_code == 200
    assert cache.stats()["misses"] == 1


def test_gzip_is_made_once_and_served_to_a_client_that_accepts_it(store_url, book, client) -> None:  # noqa: F811
    rid = run(store_url, book)
    url = f"/api/v1/runs/{rid}/positions"
    plain = client.get(url, headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in plain.headers and "Accept-Encoding" in plain.headers["Vary"]
    packed = client.get(url, headers={"Accept-Encoding": "gzip"})
    assert packed.headers["Content-Encoding"] == "gzip"
    # two representations, two ETags; a 304 answers the ETag of the representation the client asks for
    assert packed.headers["ETag"] != plain.headers["ETag"] and packed.headers["ETag"].startswith(plain.headers["ETag"][:-1])
    assert client.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": packed.headers["ETag"]}).status_code == 304
    assert client.get(url, headers={"Accept-Encoding": "gzip", "If-None-Match": plain.headers["ETag"]}).status_code == 200
    assert client.get(url, headers={"Accept-Encoding": "identity", "If-None-Match": plain.headers["ETag"]}).status_code == 304
    entry = next(iter(client.app.state.run_cache._items.values()))
    assert packed.content == plain.content  # the client unpacks it
    assert int(packed.headers["content-length"]) == len(entry.gz) and gzip.decompress(entry.gz) == plain.content  # as stored


def test_parameters_make_their_own_bodies(store_url, book, client) -> None:  # noqa: F811
    rid = run(store_url, book)
    cache = client.app.state.run_cache
    every = client.get(f"/api/v1/runs/{rid}/positions").json()["positions"]
    one_book = client.get(f"/api/v1/runs/{rid}/positions", params={"book": every[0]["book_id"]}).json()["positions"]
    assert 0 < len(one_book) < len(every) or {p["book_id"] for p in every} == {every[0]["book_id"]}
    assert cache.stats()["items"] == 2
    a = client.get(f"/api/v1/runs/{rid}/aggregates?dims=book").json()
    b = client.get(f"/api/v1/runs/{rid}/aggregates?dims=instr_kind").json()
    assert a["dims"] == ["book"] and b["dims"] == ["instr_kind"] and cache.stats()["items"] == 4
    for attribution in ("position", "legs"):  # two keys even when this book gives equal tables
        assert client.get(f"/api/v1/runs/{rid}/risk-table?attribution={attribution}").status_code == 200
    assert cache.stats()["items"] == 6
    assert client.get(f"/api/v1/runs/{rid}/aggregates?dims=nope").status_code == 422  # a bad request is never cached
    assert client.get("/api/v1/runs/99999/positions").status_code == 404 and cache.stats()["items"] == 6


def test_a_new_alias_makes_a_new_positions_body_and_leaves_the_others(store_url, book, client) -> None:  # noqa: F811
    rid = run(store_url, book)
    cache = client.app.state.run_cache
    before = client.get(f"/api/v1/runs/{rid}/positions")
    risk = client.get(f"/api/v1/runs/{rid}/risk")
    isin = next(p["fact"]["isin"] for p in before.json()["positions"] if p["fact"] and p["fact"].get("isin"))
    assert client.put("/api/v1/instruments/alias", json={"isin": isin, "alias": "Мой алиас"}).json()["changed"]
    after = client.get(f"/api/v1/runs/{rid}/positions")
    assert after.headers["ETag"] != before.headers["ETag"]
    assert any(p["alias"] == "Мой алиас" for p in after.json()["positions"])
    misses = cache.stats()["misses"]
    assert client.get(f"/api/v1/runs/{rid}/risk").headers["ETag"] == risk.headers["ETag"]  # the risk has no alias
    assert cache.stats()["misses"] == misses
    client.put("/api/v1/instruments/alias", json={"isin": isin, "alias": ""})
    assert client.get(f"/api/v1/runs/{rid}/positions").content == before.content  # the alias cleared: the first body


def test_a_run_that_is_not_complete_is_never_cached(store_url, book, client) -> None:  # noqa: F811
    rid = run(store_url, book)
    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")
        con.execute("UPDATE run SET status = 'loading' WHERE run_id = %s", (rid,))
        con.execute("ALTER TABLE run ENABLE TRIGGER USER")
    cache = client.app.state.run_cache
    for _ in range(2):
        assert client.get(f"/api/v1/runs/{rid}/positions").status_code == 200
    assert cache.stats() == {**cache.stats(), "items": 0, "misses": 0, "hits": 0}


def test_pruning_runs_drops_their_bodies_and_the_others_stay(store_url, book, client) -> None:  # noqa: F811
    keep, drop = run(store_url, book, run_type="INTRADAY"), run(store_url, book, run_type="INTRADAY")
    cache = client.app.state.run_cache
    for rid in (keep, drop):
        for route in ("positions", "risk"):
            assert client.get(f"/api/v1/runs/{rid}/{route}").status_code == 200
    assert cache.stats()["items"] == 4
    with store.connect(store_url) as con:
        day = con.execute("SELECT business_date FROM run WHERE run_id = %s", (keep,)).fetchone()[0]
        deleted = store.prune_intraday(con, day, keep, grace=timedelta(0))  # no grace: the delete itself
    assert deleted == [drop] and isinstance(day, date)
    assert {k[1] for k in cache._items} == {keep}  # the deleted run's bodies went with it
    assert client.get(f"/api/v1/runs/{drop}/positions").status_code == 404
    assert client.get(f"/api/v1/runs/{keep}/positions").status_code == 200


def test_a_run_deleted_by_another_process_is_dropped_by_retain(store_url, book, client) -> None:  # noqa: F811
    rid = run(store_url, book)
    cache = client.app.state.run_cache
    client.get(f"/api/v1/runs/{rid}/positions")
    with psycopg.connect(store_url, autocommit=True) as con:
        ids = {r[0] for r in con.execute("SELECT run_id FROM run")}
    assert cache.retain(ids) == 0 and cache.retain(set()) == 1 and cache.stats()["items"] == 0


def test_the_body_of_the_cache_is_the_body_without_it(store_url, book) -> None:  # noqa: F811
    """Cached or not (the cache off), the answer is the same bytes: the cache changes when it is built, never what."""
    rid = run(store_url, book)
    on = TestClient(create_app(store_url), **LOCAL)
    off = TestClient(create_app(store_url), **LOCAL)
    off.app.state.run_cache = RunBodyCache(max_bytes=0)
    for route in ROUTES:
        url = f"/api/v1/runs/{rid}/{route}"
        assert on.get(url).content == off.get(url).content == on.get(url).content


def test_the_serve_loop_drops_the_bodies_of_runs_that_are_gone(store_url, book, client) -> None:  # noqa: F811
    from desk.api.app import warm_bodies

    rid = run(store_url, book)
    cache = client.app.state.run_cache
    client.get(f"/api/v1/runs/{rid}/positions")
    cache.get_or_build(("positions", 424242, None), None, lambda: b"x")  # a run another process deleted
    warm_bodies(client.app)
    assert {k[1] for k in cache._items} == {rid}
