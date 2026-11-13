"""TDM#727, opening a screen without a blank wait: the bodies of a run stay built while other runs are published
(``concepts.run_fingerprint``), the newest run's are built right after its publication (``Warmer``,
``warm_bodies``), a page asks whether they are built (``GET /api/v1/runs/{id}/ready``), and the slow requests are
listed without data values (``desk.api.slowlog``). Synthetic data only: the two runs of ``test_r23_concepts``."""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_r23_concepts import DATE, PREV, desk  # noqa: F401 - the fixture

from desk import diagnostics, store
from desk.api import compat, concepts, concepts_run
from desk.api.app import BodyCache, Warmer, missing_bodies, warm_bodies
from desk.api.runs import resolve_run
from desk.api.slowlog import SlowLog, threshold
from desk.publish import summary
from desk.settings import Settings

ALL = ["run", "run-js", "extras", "history", "calendar", "desk"]


def clone_run(store_url: str, src: int, *, intraday: bool = False) -> int:
    """A complete run made of ``src``'s rows, published as the pipeline does (the event in the completion
    transaction): an INTRADAY run of the next weekday (today's book on a later cut), or an EOD run of ``src``'s own date
    (a recompute of a past date: it replaces ``src`` as the date's default run)."""
    with store.connect(store_url) as con:
        cols = [d.name for d in con.execute("SELECT * FROM run LIMIT 0").description]
        row = dict(zip(cols, con.execute("SELECT * FROM run WHERE run_id = %s", (src,)).fetchone()))
        skip = {"run_id", "status", "completed_at", "started_at", "coverage", "totals", "error"}
        vals = {k: v for k, v in row.items() if k not in skip}
        if intraday:
            d = row["business_date"] + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            vals.update(run_type="INTRADAY", cut_type="intraday", business_date=d, prev_date=row["business_date"])
        vals = {k: Jsonb(v) if isinstance(v, dict | list) and k != "position_set_ids" else v
                for k, v in vals.items()}
        with con.transaction():
            rid = con.execute(f"INSERT INTO run ({', '.join(vals)}) VALUES ({', '.join(['%s'] * len(vals))})"
                              " RETURNING run_id", list(vals.values())).fetchone()[0]
            pc = [d.name for d in con.execute("SELECT * FROM run_position LIMIT 0").description if d.name != "run_id"]
            con.execute(f"INSERT INTO run_position (run_id, {', '.join(pc)}) SELECT %s, {', '.join(pc)}"
                        " FROM run_position WHERE run_id = %s", (rid, src))
            con.execute("INSERT INTO run_reference (run_id, kind, data) SELECT %s, kind, data FROM run_reference"
                        " WHERE run_id = %s", (rid, src))
            con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
            store.run_event(con, "run.published", [rid])
        summary.ensure(con, [rid])
    return rid


def newest(store_url: str) -> dict:
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        return resolve_run(con)


def missing(app, store_url: str, run_id: int, pages: str = "concepts") -> list[str]:
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        return missing_bodies(app.state.body_cache, con, resolve_run(con, None, run_id), pages)


# ---- the bodies of a run stay built while other runs come ------------------------------------------------------------


def test_a_run_published_later_leaves_the_bodies_of_the_older_runs_built(desk, store_url, monkeypatch):  # noqa: F811
    app = desk.app
    old = newest(store_url)["run_id"]
    assert warm_bodies(app) == ALL
    assert missing(app, store_url, old) == [] and missing(app, store_url, old, "desk") == []
    new = clone_run(store_url, old, intraday=True)
    assert newest(store_url)["run_id"] == new
    # the older run is as built as it was: a new intraday cut changes nothing it reads
    assert missing(app, store_url, old) == [] and missing(app, store_url, old, "desk") == []
    assert missing(app, store_url, new) == ["run-js", "extras", "history", "calendar"]  # the new run: nothing yet
    assert warm_bodies(app) == ALL  # and now it is built, the older run's bodies untouched
    assert missing(app, store_url, new) == [] and missing(app, store_url, old) == []
    calls = []
    monkeypatch.setattr(concepts, "extras", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(concepts_run, "assemble", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(compat, "assemble", lambda *a, **kw: calls.append(1))
    for q in (f"?run_id={old}", f"?run_id={new}", ""):  # the pinned older run, the new one, the newest shown
        for path in ("/concepts-data.js", "/concepts-run.js", "/desk-data.js"):
            assert desk.get(path + q).status_code == 200
    assert not calls  # every one answered from the cache


def test_a_recomputed_past_date_rebuilds_only_what_reads_the_history(desk, store_url):  # noqa: F811
    app = desk.app
    run = newest(store_url)
    assert warm_bodies(app) == ALL
    with store.connect(store_url) as con:
        prev = con.execute("SELECT run_id FROM run WHERE business_date = %s", (date.fromisoformat(PREV),)).fetchone()[0]
    clone_run(store_url, prev)  # the earlier date is calculated again: the history of the run changes
    assert missing(app, store_url, run["run_id"]) == ["extras", "history"]  # run, run-js and calendar read the run only
    assert missing(app, store_url, run["run_id"], "desk") == ["desk"]
    assert warm_bodies(app) == ["extras", "history", "desk"]
    assert missing(app, store_url, run["run_id"]) == []


def test_the_first_request_after_a_publish_and_warm_is_served_from_the_cache(desk, store_url, monkeypatch):  # noqa: F811
    """The measure of the issue: before the warm-up the first request of the new run's body builds it (``miss``, the
    slow list says so), after it the same request is a memory read (``hit``) that builds nothing."""
    app = desk.app
    app.state.slow.threshold_s = 0.0  # every request is listed
    assert warm_bodies(app) == ALL
    clone_run(store_url, newest(store_url)["run_id"], intraday=True)
    cold = desk.get("/concepts-run.js")
    assert cold.status_code == 200 and app.state.slow.rows()[0]["cache"] == "miss"
    clone_run(store_url, newest(store_url)["run_id"], intraday=True)
    assert warm_bodies(app) == ALL
    monkeypatch.setattr(concepts_run, "assemble", lambda *a, **kw: pytest.fail("built after the warm-up"))
    monkeypatch.setattr(concepts, "extras", lambda *a, **kw: pytest.fail("built after the warm-up"))
    for path in ("/concepts-run.js", "/concepts-data.js", "/api/v1/history/positions", "/api/v1/calendar/market"):
        assert desk.get(path).status_code == 200
        row = app.state.slow.rows()[0]
        assert row["cache"] == "hit" and row["bytes"] > 0, (path, row)


def test_concurrent_requests_for_one_body_build_it_once():
    c, n, gate = BodyCache(), [], threading.Event()

    def build() -> bytes:
        n.append(1)
        gate.wait(5)
        return b"body"

    out = []
    ts = [threading.Thread(target=lambda: out.append(c.get_or_build(("run", 1), "fp", build))) for _ in range(5)]
    for t in ts:
        t.start()
    time.sleep(0.3)  # all five are waiting for the one build
    gate.set()
    for t in ts:
        t.join(5)
    assert len(n) == 1 and sorted(was for _, was in out) == [False, True, True, True, True]
    assert c.get_or_build(("run", 1), "other", lambda: b"new")[1] is False  # another fingerprint: built again
    assert c.stats()["misses"] == 2


def test_the_body_cache_is_bounded_by_bytes_least_recently_used_first(monkeypatch):
    import os

    c = BodyCache(max_bytes=5000)  # a 1000-byte random body and its gzip are about 2000
    for rid in (1, 2):
        c.get_or_build(("run", rid), "fp", lambda: os.urandom(1000))
    assert c.stats()["items"] == 2 and 3500 < c.bytes <= 5000
    assert c.get(("run", 1), "fp") is not None  # run 1 used last
    c.get_or_build(("run", 3), "fp", lambda: os.urandom(1000))
    assert c.get(("run", 2), "fp") is None and c.get(("run", 1), "fp") and c.get(("run", 3), "fp")  # 2 went
    assert c.bytes <= 5000 and c.stats()["items"] == 2
    c.get_or_build(("run", 3), "other", lambda: os.urandom(1000))  # rebuilt for another state: counted once
    assert c.stats()["items"] == 2 and c.bytes <= 5000
    assert c.retain([1]) == 1 and c.stats()["items"] == 1 and 1500 < c.bytes < 2500
    huge = BodyCache(max_bytes=100)  # a body larger than the cache is answered, not kept
    val, was = huge.get_or_build(("run", 1), "fp", lambda: os.urandom(1000))
    assert was is False and len(val[0]) == 1000 and huge.stats()["items"] == 0 and huge.bytes == 0
    monkeypatch.setenv("DESK_BODY_CACHE_MB", "2")
    assert BodyCache().max_bytes == 2 << 20
    monkeypatch.setenv("DESK_BODY_CACHE_MB", "many")
    assert BodyCache().max_bytes == BodyCache.DEFAULT_MB << 20


def test_opened_position_cards_do_not_push_the_newest_runs_page_bodies_out():
    c = BodyCache(max_bytes=64 << 20)
    assert c.size >= 96  # the page bodies of a dozen runs (six keys each) fit
    page = [(name, 200) for name in ("run", "run-js", "extras", "calendar", "desk")] + [("history", 200, (), None)]
    for rid in range(150, 200):  # fifty older runs' bodies stay valid: more than the cache holds
        for name in ("run", "run-js", "extras", "calendar", "desk"):
            c.get_or_build((name, rid), "fp", lambda: b"x" * 100)
        c.get_or_build(("history", rid, (), None), "fp", lambda: b"x" * 100)
    for key in page:
        c.get_or_build(key, "fp", lambda: b"x" * 100)
    for n in range(300):  # a day of position cards of the newest run, and one from a date
        c.get_or_build(("history", 200, (f"p{n}",), None), "fp", lambda: b"x" * 100)
        c.get_or_build(("history", 200, (), date(2026, 1, 1) + timedelta(days=n)), "fp", lambda: b"x" * 100)
    assert len(c._items) <= c.size
    for key in page:
        assert c.get(key, "fp") is not None, f"{key} was evicted by position cards"
    assert c.get(("history", 200, (), date(2026, 1, 1) + timedelta(days=299)), "fp") is not None  # the last asked is kept
    assert c.get(("history", 200, ("p0",), None), "fp") is None  # the older cards went first
    # without side bodies the oldest run's page bodies go as before (least recently used)
    only = BodyCache(size=4, max_bytes=64 << 20)
    for rid in range(6):
        only.get_or_build(("run", rid), "fp", lambda: b"x")
    assert [only.get(("run", rid), "fp") is not None for rid in range(6)] == [False, False, True, True, True, True]
    assert BodyCache.side(("history", 1, ("a",), None)) and BodyCache.side(("history", 1, (), date(2026, 1, 1)))
    assert not BodyCache.side(("history", 1, (), None)) and not BodyCache.side(("run", 1))


def test_the_bodies_of_a_deleted_run_are_dropped():
    c = BodyCache()
    for rid in (1, 2, 3):
        c.get_or_build(("run", rid), "fp", lambda: b"x")
    c.get_or_build(("history", 2, ("a",), None), "fp", lambda: b"x")
    assert c.retain([1, 3]) == 2 and c.stats()["items"] == 2


# ---- the warm thread wakes when a run is published ---------------------------------------------------------------------


def test_the_warm_thread_wakes_for_a_new_run_a_page_and_the_timer(desk, store_url):  # noqa: F811
    w = Warmer(store_url)
    w.mark()
    fast = {"watch": 0.02, "quiet": 0.1, "step": 0.01}
    assert w.wait(lambda: False, every=0.3, **fast) == "timer"
    assert w.wait(lambda: True, every=5, **fast) == "stop"
    rid = clone_run(store_url, newest(store_url)["run_id"], intraday=True)
    t0 = time.monotonic()
    assert w.wait(lambda: False, every=5, **fast) == "run"  # seen, then quiet for `quiet`
    assert time.monotonic() - t0 < 2
    w.mark()
    assert w.wait(lambda: False, every=0.3, **fast) == "timer"  # the same newest: nothing new
    w.request(rid)
    w.request(rid)
    assert w.wait(lambda: False, every=5, **fast) == "requested" and w.take() == [rid] and w.take() == []


def test_runs_published_one_after_another_are_built_for_once(desk, store_url):  # noqa: F811
    w = Warmer(store_url)
    w.mark()
    seen = []
    t = threading.Thread(target=lambda: seen.append(w.wait(lambda: False, every=10, watch=0.02, quiet=0.5, step=0.01)))
    t.start()
    for _ in range(3):  # a recompute: a run every 0.2 s, closer than `quiet`
        clone_run(store_url, newest(store_url)["run_id"], intraday=True)
        time.sleep(0.2)
    assert t.is_alive()  # still waiting for the last one to be quiet
    t.join(5)
    assert seen == ["run"]


# ---- the page asks whether the run is built --------------------------------------------------------------------------


def test_ready_says_what_is_missing_and_asks_the_warm_thread(desk, store_url):  # noqa: F811
    app = desk.app
    rid = newest(store_url)["run_id"]
    r = desk.get(f"/api/v1/runs/{rid}/ready")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == {"run_id": rid, "ready": False, "missing": ["run-js", "extras", "history", "calendar"],
                        "warming": False}
    assert desk.get(f"/api/v1/runs/{rid}/ready?pages=desk").json()["missing"] == ["desk"]
    app.state.warmer = Warmer(store_url)  # serve's: the page's question wakes it for the run
    desk.get(f"/api/v1/runs/{rid}/ready")
    assert app.state.warmer.take() == [rid]
    warm_bodies(app)
    assert desk.get(f"/api/v1/runs/{rid}/ready").json() == {"run_id": rid, "ready": True, "missing": [],
                                                           "warming": False}
    assert desk.get(f"/api/v1/runs/{rid}/ready?pages=desk").json()["ready"] is True
    assert app.state.warmer.take() == []  # nothing asked for: it is built
    r = desk.get("/api/v1/runs/99999/ready")
    assert r.status_code == 404 and r.json()["details"]["reason"] == "run_not_found"
    assert desk.get(f"/api/v1/runs/{rid}/ready?pages=x").status_code == 422


def test_an_older_run_a_page_asks_for_is_built_on_request(desk, store_url):  # noqa: F811
    app = desk.app
    app.state.warmer = Warmer(store_url)
    old = newest(store_url)["run_id"]
    new = clone_run(store_url, old, intraday=True)
    for rid in (old, new):
        assert desk.get(f"/api/v1/runs/{rid}/ready").json()["ready"] is False
    assert app.state.warmer.take() == [old, new]
    assert warm_bodies(app, run_id=old) == ALL and warm_bodies(app) == ALL
    assert all(desk.get(f"/api/v1/runs/{rid}/ready").json()["ready"] for rid in (old, new))


def test_the_summaries_still_being_rebuilt_keep_the_run_not_ready(desk, store_url, monkeypatch):  # noqa: F811
    app = desk.app
    monkeypatch.setattr(summary, "VERSION", summary.VERSION + 1)  # an upgrade: the series wait for the summaries
    rid = newest(store_url)["run_id"]
    warm_bodies(app, summaries=False)
    assert desk.get(f"/api/v1/runs/{rid}/ready").json()["missing"] == ["extras", "history"]


# ---- the slow requests --------------------------------------------------------------------------------------------------


def test_slow_requests_are_listed_without_values_and_say_whether_a_body_was_built(desk, store_url):  # noqa: F811
    app = desk.app
    rid = newest(store_url)["run_id"]
    desk.get(f"/api/v1/runs/{rid}/positions")
    assert app.state.slow.rows() == []  # a fast one is not listed (2 s)
    app.state.slow.threshold_s = 0.0
    for _ in range(2):
        assert desk.get(f"/api/v1/runs/{rid}/positions?book=OFZ&quality=exact").status_code == 200
    first, second = app.state.slow.rows()[1], app.state.slow.rows()[0]
    assert first["route"] == second["route"] == "/api/v1/runs/{run_id}/positions"  # the route, not the run id
    assert first["query"] == ["book", "quality"] and "OFZ" not in str(first) and str(rid) not in first["route"]
    assert (first["cache"], second["cache"]) == ("miss", "hit") and first["status"] == 200
    assert first["ms"] >= 0 and first["bytes"] > 0 and first["method"] == "GET" and first["at"]
    assert desk.get("/concepts/c01-cockpit.html").status_code in (200, 404)
    assert app.state.slow.rows()[0]["cache"] is None  # a page: no body cache
    snap = app.state.slow.snapshot()
    assert snap["threshold_s"] == 0.0 and snap["total"] == len(snap["requests"]) >= 3


def test_the_slow_list_is_bounded_and_the_threshold_is_an_env_setting(monkeypatch):
    log = SlowLog(0.0, keep=3)

    Req = SimpleNamespace(method="GET", query_params={}, scope={"route": SimpleNamespace(path="/api/v1/x/{id}")},
                          state=SimpleNamespace(), url=SimpleNamespace(path="/api/v1/x/42"))

    for _ in range(5):
        log.add(Req, 200, 0.5)
    assert len(log.rows()) == 3 and log.total == 5 and log.rows()[0]["route"] == "/api/v1/x/{id}"
    monkeypatch.setenv("DESK_SLOW_REQUEST_S", "0.5")
    assert threshold() == 0.5 and SlowLog().threshold_s == 0.5
    monkeypatch.setenv("DESK_SLOW_REQUEST_S", "soon")
    assert threshold() == 2.0
    monkeypatch.delenv("DESK_SLOW_REQUEST_S")
    assert threshold() == 2.0
    assert SlowLog(5.0).add(Req, 200, 4.9) is None and SlowLog(5.0).add(Req, 200, 5.0) is not None


def test_the_slow_list_is_in_the_diagnostics_file_and_the_settings_route(desk, store_url, tmp_path):  # noqa: F811
    app = desk.app
    app.state.slow.threshold_s = 0.0
    rid = newest(store_url)["run_id"]
    desk.get(f"/api/v1/runs/{rid}/risk")
    local = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    j = local.get("/api/v1/diagnostics/slow-requests").json()
    assert j["threshold_s"] == 0.0 and j["requests"][-1]["route"] == "/api/v1/runs/{run_id}/risk"
    doc = diagnostics.build(Settings(home=tmp_path / "home"), store_url=store_url, hub=False,
                            slow_requests=app.state.slow.snapshot())
    rows = doc["slow_requests"]["requests"]
    assert any(r["route"] == "/api/v1/runs/{run_id}/risk" and r["cache"] == "miss" for r in rows)
    assert "slow_requests" not in diagnostics.build(Settings(home=tmp_path / "home2"), store_url=store_url, hub=False)
