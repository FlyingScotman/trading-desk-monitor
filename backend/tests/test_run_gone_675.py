"""A page keeps the ``run_id`` it was opened on (TDM#675). The intraday run of the date is replaced by the next cut and
used to be deleted at once: the card's «Откуда данные» then got 404 and said the position was not found. Now a replaced
INTRADAY run stays ``store.INTRADAY_GRACE`` after its replacement, and a request for a run that is gone says so
(``details.reason`` ``run_not_found``), apart from a position the run does not have (``position_not_found``). Synthetic."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from test_api import book, run  # noqa: F401  (book: the module fixture of the golden runs)

from desk import store
from desk.api import create_app

LOCAL = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
GONE = 999_999


@pytest.fixture
def client(store_url) -> TestClient:
    return TestClient(create_app(store_url), **LOCAL)


def _age(store_url: str, rid: int, minutes: int) -> None:
    """The run was completed ``minutes`` ago (the triggers reject an update of a complete run: off for the test)."""
    import psycopg

    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER USER")
        con.execute("UPDATE run SET completed_at = now() - make_interval(mins => %s) WHERE run_id = %s", (minutes, rid))
        con.execute("ALTER TABLE run ENABLE TRIGGER USER")


def _ids(store_url: str) -> list[int]:
    with store.connect(store_url) as con:
        return [r for (r,) in con.execute("SELECT run_id FROM run ORDER BY run_id")]


def _prune(store_url: str, keep: int, **kw) -> list[int]:
    with store.connect(store_url) as con:
        day = con.execute("SELECT business_date FROM run WHERE run_id = %s", (keep,)).fetchone()[0]
        assert isinstance(day, date)
        return store.prune_intraday(con, day, keep, **kw)


# ---- the 404 says which of the two it is -----------------------------------------------------------------------------------


def test_a_deleted_run_is_run_not_found_and_a_live_run_without_the_position_is_position_not_found(store_url, book, client):  # noqa: F811
    rid = run(store_url, book)
    q = {"id": "no-such-position", "run_id": rid}
    r = client.get("/api/v1/positions/provenance", params=q)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert r.json()["details"] == {"reason": "position_not_found", "run_id": rid}
    r = client.get("/api/v1/positions/provenance", params={"id": "any", "run_id": GONE})
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert r.json()["details"] == {"reason": "run_not_found", "run_id": GONE}
    # the same for the other requests a page makes by its run
    gone = {"run_id": GONE}
    for url, params in (("/api/v1/positions/flows", {"id": "x", **gone}), ("/api/v1/history/positions", gone),
                        ("/api/v1/history/market", gone), ("/api/v1/calendar/market", gone),
                        (f"/api/v1/runs/{GONE}/derivatives", {}), (f"/api/v1/runs/{GONE}/positions", {}),
                        (f"/api/v1/runs/{GONE}/scenario-results", {})):
        r = client.get(url, params=params)
        assert r.status_code == 404 and r.json()["details"]["reason"] == "run_not_found", url
    flows = client.get("/api/v1/positions/flows", params=q)
    assert flows.status_code == 404 and flows.json()["details"]["reason"] == "position_not_found"


def test_no_runs_at_all_is_no_runs(client):
    r = client.get("/api/v1/positions/provenance", params={"id": "x"})
    assert r.status_code == 404 and r.json()["details"] == {"reason": "no_runs"}


# ---- a replaced intraday run stays for the grace -----------------------------------------------------------------------------


def test_the_run_a_page_is_on_is_read_after_the_next_cut_and_goes_with_the_one_after(store_url, book, client):  # noqa: F811
    a = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    _age(store_url, a, 90)  # the page was opened on it an hour and a half ago
    b = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    assert _prune(store_url, b) == []  # b has just replaced a: a stays for the page that is on it
    assert _ids(store_url) == [a, b]
    assert client.get(f"/api/v1/history/market?run_id={a}").status_code != 404  # still read (not the 404 of a run gone)
    assert client.get("/api/v1/positions/provenance", params={"id": "nope", "run_id": a}).json()["details"]["reason"] \
        == "position_not_found"
    c = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    _age(store_url, b, 61)
    assert _prune(store_url, c) == [a]  # b was replaced by c a minute ago: a, replaced by b long ago, goes
    assert _ids(store_url) == [b, c]
    r = client.get("/api/v1/positions/provenance", params={"id": "nope", "run_id": a})
    assert r.json()["details"]["reason"] == "run_not_found"  # the page that stayed on a is told so


def test_runs_replaced_within_the_grace_all_stay_and_the_next_call_takes_them(store_url, book):  # noqa: F811
    a = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    b = run(store_url, book, run_type="INTRADAY", cut_type="intraday")  # a recompute at once after the cut
    c = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    assert _prune(store_url, c) == [] and _ids(store_url) == [a, b, c]
    for rid in (b, c):
        _age(store_url, rid, 120)
    d = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    assert _prune(store_url, d) == [a, b] and _ids(store_url) == [c, d]
    assert _prune(store_url, d, grace=timedelta(0)) == [c]  # no grace: as before TDM#675


def test_a_run_with_a_scenario_job_and_the_other_kinds_of_run_stay(store_url, book):  # noqa: F811
    held = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    eod = run(store_url, book, run_type="EOD")  # an EOD run is never touched (its prune is TDM#634)
    test_run = run(store_url, book, run_type="TEST")
    import psycopg

    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("INSERT INTO scenario_job (base_run_id, definition_hash, definition, method, core_version)"
                    " VALUES (%s, 'h', '{}', 'm', 'c')", (held,))
    plain = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    cur = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    for rid in (held, plain):
        _age(store_url, rid, 180)
    _age(store_url, cur, 120)
    new = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    assert _prune(store_url, new) == [plain]
    assert set(_ids(store_url)) == {held, eod, test_run, cur, new}


# ---- after the day's last cut nothing makes a run of the date: the stale clean-up takes the rest ---------------------------


def test_the_last_cut_of_the_day_leaves_one_intraday_run_after_the_grace(store_url, book, client):  # noqa: F811
    a = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    b = run(store_url, book, run_type="INTRADAY", cut_type="intraday")  # the day's last cut
    assert _prune(store_url, b) == []  # a is held: the page may be on it
    with store.connect(store_url) as con:
        assert store.prune_intraday_stale(con) == []  # still within the grace
    _age(store_url, b, 61)  # no more runs of the date, no more calls by date
    cache = client.app.state.run_cache
    assert client.get(f"/api/v1/runs/{a}/positions").status_code == 200 and {k[1] for k in cache._items} == {a}
    with store.connect(store_url) as con:
        assert store.prune_intraday_stale(con) == [a]
        assert store.prune_intraday_stale(con) == []  # one run stays
    assert _ids(store_url) == [b]
    assert {k[1] for k in cache._items} == set()  # the cache of #631 forgot the deleted run
    assert client.get(f"/api/v1/runs/{a}/positions").status_code == 404


def test_the_stale_clean_up_keeps_the_newest_of_each_date_a_scenario_job_and_other_run_types(store_url, book):  # noqa: F811
    import psycopg

    held = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("INSERT INTO scenario_job (base_run_id, definition_hash, definition, method, core_version)"
                    " VALUES (%s, 'h', '{}', 'm', 'c')", (held,))
    eod = run(store_url, book, run_type="EOD")
    test_run = run(store_url, book, run_type="TEST")
    plain = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    last = run(store_url, book, run_type="INTRADAY", cut_type="intraday")
    for rid in (held, plain, last):
        _age(store_url, rid, 600)
    with store.connect(store_url) as con:
        assert store.prune_intraday_stale(con) == [plain]
        assert store.prune_intraday_stale(con, grace=timedelta(0)) == []
    assert set(_ids(store_url)) == {held, eod, test_run, last}
