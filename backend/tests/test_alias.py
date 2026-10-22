"""Instrument aliases (batch 1, item 3): migration 0014 (append-only revisions, the view of those in force), the
routes of api.alias_routes (loopback-only writes, who and when) and the alias on a run's positions, old runs too."""

from __future__ import annotations

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_api import book, run  # noqa: F401  (book: the module fixture of the golden runs)

from desk.api import create_app

ISIN = "RU000A1038V6"
OTHER = "XS0767472458"
LOCAL = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}


@pytest.fixture
def local(store_url) -> TestClient:
    return TestClient(create_app(store_url), **LOCAL)


def put(c: TestClient, isin: str, alias: str | None, **kw):
    return c.put("/api/v1/instruments/alias", json={"isin": isin, "alias": alias}, **kw)


def test_revisions_are_append_only_and_the_view_shows_the_latest(store_url):
    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("INSERT INTO instrument_alias_rev (isin, alias) VALUES (%s, 'A'), (%s, 'B'), (%s, NULL),"
                    " (%s, 'C')", (ISIN, ISIN, OTHER, OTHER))
        con.execute("INSERT INTO instrument_alias_rev (isin, alias) VALUES (%s, NULL)", (ISIN,))
        assert con.execute("SELECT isin, alias, changed_by FROM instrument_alias").fetchall() == [(OTHER, "C", "local")]
        for sql in ("UPDATE instrument_alias_rev SET alias = 'X'", "DELETE FROM instrument_alias_rev",
                    "TRUNCATE instrument_alias_rev"):
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
                con.execute(sql)
        for bad in (("not-an-isin", "A"), (ISIN, " padded "), (ISIN, ""), (ISIN, "x" * 121)):
            with pytest.raises(psycopg.errors.CheckViolation):
                con.execute("INSERT INTO instrument_alias_rev (isin, alias) VALUES (%s, %s)", bad)


def test_set_change_clear_with_history(local):
    assert local.get("/api/v1/instruments/aliases").json() == {"aliases": []}
    r = put(local, " ru000a1038v6 ", "  ОФЗ-н   $  2028 ")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["isin"] == ISIN and s["alias"] == "ОФЗ-н $ 2028" and s["changed"] and s["changed_by"] == "local"
    assert s["changed_at"]
    again = put(local, ISIN, "ОФЗ-н $ 2028").json()
    assert not again["changed"] and again["changed_at"] == s["changed_at"]  # no revision for the same alias
    put(local, ISIN, "USD OFZ 2028")
    put(local, OTHER, "Russia 2042")
    got = local.get("/api/v1/instruments/aliases").json()["aliases"]
    assert [(a["isin"], a["alias"], a["changed_by"]) for a in got] == [(ISIN, "USD OFZ 2028", "local"),
                                                                         (OTHER, "Russia 2042", "local")]
    cleared = put(local, ISIN, "   ").json()
    assert cleared["alias"] is None and cleared["changed"]
    assert not put(local, ISIN, None).json()["changed"]  # already cleared
    assert [a["isin"] for a in local.get("/api/v1/instruments/aliases").json()["aliases"]] == [OTHER]
    h = local.get(f"/api/v1/instruments/aliases/{ISIN.lower()}/history").json()
    assert h["isin"] == ISIN and [x["alias"] for x in h["revisions"]] == [None, "USD OFZ 2028", "ОФЗ-н $ 2028"]
    assert all(x["changed_by"] == "local" for x in h["revisions"])
    never = put(local, "RU000A0JX0J2", None).json()
    assert never == {"isin": "RU000A0JX0J2", "alias": None, "changed_by": None, "changed_at": None,
                     "changed": False}


def test_bad_input(local):
    for isin, alias in (("RU000A1038V", "x"), ("12000A1038V6", "x"), ("", "x")):
        r = put(local, isin, alias)
        assert r.status_code == 422 and r.json()["code"] == "invalid_parameter", (isin, r.text)
    assert put(local, ISIN, "x" * 121).status_code == 422
    assert put(local, ISIN, "a\x00b").status_code == 422
    assert put(local, ISIN, "x" * 120).json()["changed"]
    assert local.put("/api/v1/instruments/alias", json={"alias": "x"}).status_code == 422  # no isin
    assert local.get("/api/v1/instruments/aliases/nope/history").status_code == 422


def test_writes_are_local_only(store_url):
    app = create_app(store_url)
    remote = TestClient(app, base_url="http://127.0.0.1:8100", client=("10.1.2.3", 50000))
    assert put(remote, ISIN, "x").status_code == 403
    assert remote.get("/api/v1/instruments/aliases").status_code == 200  # reads are open, like the rest of the API
    rebound = TestClient(app, base_url="http://evil.example:8100", client=("127.0.0.1", 50000))
    assert put(rebound, ISIN, "x").status_code == 403
    local = TestClient(app, **LOCAL)
    assert put(local, ISIN, "x", headers={"Origin": "http://evil.example"}).status_code == 403
    form = local.put("/api/v1/instruments/alias", content="isin=x", headers={"Content-Type": "text/plain"})
    assert form.status_code == 415
    assert local.get("/api/v1/instruments/aliases").json() == {"aliases": []}


def test_positions_carry_the_alias_in_force(store_url, book, local):  # noqa: F811
    rid = run(store_url, book)
    before = local.get(f"/api/v1/runs/{rid}/positions")
    ps = before.json()["positions"]
    assert all(p["alias"] is None for p in ps)
    target = next(p for p in ps if p["fact"] and p["fact"].get("isin"))
    isin, name = target["fact"]["isin"], target["fact"]["name"]
    assert put(local, isin, "Мой алиас").json()["changed"]  # after the run: applies to it all the same
    after = local.get(f"/api/v1/runs/{rid}/positions")
    assert after.headers["ETag"] != before.headers["ETag"]
    by_id = {p["position_id"]: p for p in after.json()["positions"]}
    same = [p for p in by_id.values() if p["fact"] and p["fact"].get("isin") == isin]
    assert same and all(p["alias"] == "Мой алиас" and p["fact"]["name"] == name for p in same)
    assert all(p["alias"] is None for p in by_id.values() if p not in same)
    unchanged = {k: v for k, v in by_id[target["position_id"]].items() if k != "alias"}
    assert unchanged == {k: v for k, v in target.items() if k != "alias"}  # the run's own fields as they were
    ofz = local.get(f"/api/v1/runs/{rid}/positions?book={target['book_id']}").json()["positions"]
    assert any(p["alias"] == "Мой алиас" for p in ofz)
    put(local, isin, "")
    assert all(p["alias"] is None for p in local.get(f"/api/v1/runs/{rid}/positions").json()["positions"])
