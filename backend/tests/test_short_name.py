"""Short names of securities (TDM#776): migration 0026 (append-only revisions, the view of those in force), the routes of
api.short_name_routes (who and when, 40 characters, the audit log) and the short name on a run's positions, old runs too.
It is a second name apart from the alias: neither touches the other."""

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


def put(c: TestClient, isin: str, name: str | None, **kw):
    return c.put("/api/v1/instruments/short-name", json={"isin": isin, "short_name": name}, **kw)


def test_revisions_are_append_only_and_the_view_shows_the_latest(store_url):
    with psycopg.connect(store_url, autocommit=True) as con:
        con.execute("INSERT INTO instrument_short_name_rev (isin, short_name) VALUES (%s, 'A'), (%s, 'B'),"
                    " (%s, NULL), (%s, 'C')", (ISIN, ISIN, OTHER, OTHER))
        con.execute("INSERT INTO instrument_short_name_rev (isin, short_name) VALUES (%s, NULL)", (ISIN,))
        assert con.execute("SELECT isin, short_name, changed_by FROM instrument_short_name").fetchall() == [
            (OTHER, "C", "local")]
        for sql in ("UPDATE instrument_short_name_rev SET short_name = 'X'", "DELETE FROM instrument_short_name_rev",
                    "TRUNCATE instrument_short_name_rev"):
            with pytest.raises(psycopg.errors.RaiseException):
                con.execute(sql)
        for bad in (("RU000A1038V", "x"), (ISIN, ""), (ISIN, " x"), (ISIN, "x" * 41)):
            with pytest.raises(psycopg.errors.CheckViolation):
                con.execute("INSERT INTO instrument_short_name_rev (isin, short_name) VALUES (%s, %s)", bad)
        con.execute("INSERT INTO instrument_short_name_rev (isin, short_name) VALUES (%s, %s)", (ISIN, "x" * 40))


def test_set_change_clear_with_history(local):
    assert local.get("/api/v1/instruments/short-names").json() == {"short_names": []}
    r = put(local, " ru000a1038v6 ", "  ОФЗ-н   28  ")
    assert r.status_code == 200, r.text
    s = r.json()
    assert s["isin"] == ISIN and s["short_name"] == "ОФЗ-н 28" and s["changed"] and s["changed_by"] == "local"
    assert s["changed_at"]
    again = put(local, ISIN, "ОФЗ-н 28").json()
    assert not again["changed"] and again["changed_at"] == s["changed_at"]  # no revision for the same name
    put(local, ISIN, "ОФЗ28 USD")
    put(local, OTHER, "RU-42")
    got = local.get("/api/v1/instruments/short-names").json()["short_names"]
    assert [(a["isin"], a["short_name"], a["changed_by"]) for a in got] == [(ISIN, "ОФЗ28 USD", "local"),
                                                                              (OTHER, "RU-42", "local")]
    cleared = put(local, ISIN, "   ").json()
    assert cleared["short_name"] is None and cleared["changed"]
    assert not put(local, ISIN, None).json()["changed"]  # already cleared
    assert [a["isin"] for a in local.get("/api/v1/instruments/short-names").json()["short_names"]] == [OTHER]
    h = local.get(f"/api/v1/instruments/short-names/{ISIN.lower()}/history").json()
    assert h["isin"] == ISIN and [x["short_name"] for x in h["revisions"]] == [None, "ОФЗ28 USD", "ОФЗ-н 28"]
    assert all(x["changed_by"] == "local" and x["changed_at"] and x["rev_id"] for x in h["revisions"])
    never = put(local, "RU000A0JX0J2", None).json()
    assert never == {"isin": "RU000A0JX0J2", "short_name": None, "changed_by": None, "changed_at": None,
                     "changed": False}


def test_bad_input(local):
    for isin, name in (("RU000A1038V", "x"), ("12000A1038V6", "x"), ("", "x")):
        r = put(local, isin, name)
        assert r.status_code == 422 and r.json()["code"] == "invalid_parameter", (isin, r.text)
    long = put(local, ISIN, "x" * 41)
    assert long.status_code == 422 and "short name is longer than 40" in long.json()["message"]
    assert put(local, ISIN, "a\x00b").status_code == 422
    assert put(local, ISIN, "x" * 40).json()["changed"]
    assert local.put("/api/v1/instruments/short-name", json={"short_name": "x"}).status_code == 422  # no isin
    assert local.get("/api/v1/instruments/short-names/nope/history").status_code == 422


def test_writes_are_local_only_while_sign_in_is_off(store_url):
    app = create_app(store_url)
    remote = TestClient(app, base_url="http://127.0.0.1:8100", client=("10.1.2.3", 50000))
    assert put(remote, ISIN, "x").status_code == 403
    assert remote.get("/api/v1/instruments/short-names").status_code == 200  # reads are open, like the rest of the API
    local = TestClient(app, **LOCAL)
    assert put(local, ISIN, "x", headers={"Origin": "http://evil.example"}).status_code == 403
    form = local.put("/api/v1/instruments/short-name", content="isin=x", headers={"Content-Type": "text/plain"})
    assert form.status_code == 415
    assert local.get("/api/v1/instruments/short-names").json() == {"short_names": []}


def test_a_change_is_in_the_audit_log(store_url, local):
    put(local, ISIN, "First")
    put(local, ISIN, "Second")
    put(local, ISIN, "Second")  # no change, no entry
    with psycopg.connect(store_url) as con:
        rows = con.execute("SELECT actor, target, detail FROM audit_log WHERE action = 'short_name'"
                           " ORDER BY audit_id").fetchall()
    assert [(a, t, d["short_name"], d["was"]) for a, t, d in rows] == [("local", ISIN, "First", None),
                                                                       ("local", ISIN, "Second", "First")]


def test_it_is_apart_from_the_alias(store_url, local):
    local.put("/api/v1/instruments/alias", json={"isin": ISIN, "alias": "Full name"})
    put(local, ISIN, "Short")
    assert local.get("/api/v1/instruments/aliases").json()["aliases"][0]["alias"] == "Full name"
    put(local, ISIN, None)
    assert local.get("/api/v1/instruments/aliases").json()["aliases"][0]["alias"] == "Full name"
    assert local.get("/api/v1/instruments/short-names").json() == {"short_names": []}


def test_positions_carry_the_short_name_in_force(store_url, book, local):  # noqa: F811
    rid = run(store_url, book)
    before = local.get(f"/api/v1/runs/{rid}/positions")
    ps = before.json()["positions"]
    assert all(p["short_name"] is None for p in ps)
    target = next(p for p in ps if p["fact"] and p["fact"].get("isin"))
    isin = target["fact"]["isin"]
    assert put(local, isin, "Мой кор").json()["changed"]  # after the run: applies to it all the same
    after = local.get(f"/api/v1/runs/{rid}/positions")
    assert after.headers["ETag"] != before.headers["ETag"]
    by_id = {p["position_id"]: p for p in after.json()["positions"]}
    same = [p for p in by_id.values() if p["fact"] and p["fact"].get("isin") == isin]
    assert same and all(p["short_name"] == "Мой кор" and p["alias"] is None for p in same)
    assert all(p["short_name"] is None for p in by_id.values() if p not in same)
    unchanged = {k: v for k, v in by_id[target["position_id"]].items() if k != "short_name"}
    assert unchanged == {k: v for k, v in target.items() if k != "short_name"}  # the run's own fields as they were
    # the exchange's short name stays in the run's row, apart from the user's
    assert "short_name" in (target["fact"].get("extra") or {})
    ofz = local.get(f"/api/v1/runs/{rid}/positions?book={target['book_id']}").json()["positions"]
    assert any(p["short_name"] == "Мой кор" for p in ofz)
    put(local, isin, "")
    assert all(p["short_name"] is None for p in local.get(f"/api/v1/runs/{rid}/positions").json()["positions"])
