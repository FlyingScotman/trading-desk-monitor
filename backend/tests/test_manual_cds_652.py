"""Manually entered CDS curves (TDM#652): migration 0028 (append-only revisions, checks, the view of the entries), the
curve in force on a date (``desk.marketdata.manual_cds``), and the routes of ``api.manual_md_routes`` (rights, who and
when, audit, copying, the CDS names of a run)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_auth import ON, add_users, csrf, home, signed_in  # noqa: F401  (home: the module's fixture)

from desk import store
from desk.api import create_app
from desk.marketdata import manual_cds as M

LOCAL = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
BASE = "/api/v1/market-data/manual/cds"
D0, D1, D2 = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
CURVE = {"3M": 90, "6M": 110, "1Y": 140.5, "2Y": 190, "3Y": 240, "5Y": 300, "7Y": 330, "10Y": 350}


@pytest.fixture
def local(store_url) -> TestClient:
    return TestClient(create_app(store_url), **LOCAL)


@pytest.fixture
def con(store_url):
    with psycopg.connect(store_url, autocommit=True) as c:
        yield c


def put(c: TestClient, name: str, on: date, spreads: dict | None = None, *, recovery=None, ccy=None):
    body = {"name": name, "date": on.isoformat(), "spreads_bp": CURVE if spreads is None else spreads}
    return c.put(BASE + "/curve", json=body | ({"recovery": recovery} if recovery is not None else {})
                 | ({"ccy": ccy} if ccy else {}))


def rows(url: str, sql: str, args=()):
    with psycopg.connect(url, autocommit=True) as c:
        cur = c.execute(sql, args or None)
        return cur.fetchall() if cur.description else []


# ---- the store ---------------------------------------------------------------------------------------------------


def test_revisions_are_append_only_and_the_view_shows_the_latest_of_each_date(con):
    ins = ("INSERT INTO manual_cds_rev (name_key, name, valid_from, spread_5y, recovery, ccy)"
           " VALUES ('a', 'A', %s, %s, 0.25, 'USD')")
    con.execute(ins, (D0, 100))
    con.execute(ins, (D0, 110))
    con.execute(ins, (D1, 120))
    con.execute("INSERT INTO manual_cds_rev (name_key, name, valid_from, cleared) VALUES ('a', 'A', %s, true)", (D2,))
    got = con.execute("SELECT valid_from, spread_5y, cleared FROM manual_cds_curve ORDER BY valid_from").fetchall()
    assert [(d, None if s is None else float(s), c) for d, s, c in got] == [(D0, 110.0, False), (D1, 120.0, False),
                                                                         (D2, None, True)]
    for sql in ("UPDATE manual_cds_rev SET spread_5y = 1", "DELETE FROM manual_cds_rev", "TRUNCATE manual_cds_rev"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            con.execute(sql)


def test_the_store_refuses_what_is_no_curve(con):
    base = {"name_key": "a", "name": "A", "valid_from": D0, "spread_5y": 100, "recovery": 0.25, "ccy": "USD", "cleared": False}
    ok = "INSERT INTO manual_cds_rev ({}) VALUES ({})"

    def ins(**kw):
        d = base | kw
        con.execute(ok.format(", ".join(d), ", ".join(["%s"] * len(d))), list(d.values()))

    ins()
    for bad in ({"spread_5y": -0.0001}, {"spread_5y": 10000.01}, {"spread_3m": -1}, {"recovery": 1.01}, {"recovery": 1},
                {"recovery": -0.1}, {"ccy": "usd"}, {"ccy": "US"}, {"ccy": None}, {"recovery": None},
                {"spread_5y": None},  # no point at all
                {"name": " A"}, {"name": ""}, {"name": "x" * 121}, {"name_key": ""}, {"name_key": "x" * 121},
                {"valid_from": date(1999, 1, 1)},
                {"cleared": True},  # a withdrawal holds no curve
                ):
        with pytest.raises(psycopg.errors.CheckViolation):
            ins(**bad)
    ins(spread_5y=0, recovery=0, ccy="EUR")
    ins(spread_5y=10000, recovery=0.9999)
    ins(cleared=True, spread_5y=None, recovery=None, ccy=None)


# ---- the curve in force on a date --------------------------------------------------------------------------------


def test_name_key_is_the_identity_of_a_name():
    assert M.name_key("  Эмитент   А ") == M.name_key("ЭМИТЕНТ А") == "эмитент а"
    assert M.name_key("Ｅｍｉｔｅｎｔ A") == "emitent a"  # width variants (NFKC)
    assert M.name_key(M.name_key("Straße  Ltd")) == M.name_key("Straße  Ltd") == "strasse ltd"
    assert M.TENORS == ("3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y")


def test_curve_on_is_the_latest_entry_on_or_before_the_date(con):
    assert M.curve_on(con, D1, "Эмитент А") is None
    c0, changed = M.put_curve(con, D0, "Эмитент А", {"5Y": 300, "3Y": 240}, who="tom")
    assert changed and c0.valid_from == D0 and c0.recovery == 0.25 and c0.ccy == "USD" and c0.changed_by == "tom"
    assert c0.spreads_bp == {**dict.fromkeys(M.TENORS), "3Y": 240.0, "5Y": 300.0}
    assert c0.points() == [("3Y", 3.0, 240.0), ("5Y", 5.0, 300.0)]
    assert M.curve_on(con, D0 - timedelta(days=1), "Эмитент А") is None  # before the first entry
    assert M.curve_on(con, D0, "эмитент   а").rev_id == c0.rev_id  # any spelling of the name
    assert M.curve_on(con, D2, "Эмитент А").rev_id == c0.rev_id  # carried forward
    c1, _ = M.put_curve(con, D1, "Эмитент А", {"5Y": 320}, recovery=0.4, ccy="eur")
    assert (c1.recovery, c1.ccy) == (0.4, "EUR")
    assert M.curve_on(con, D0, "Эмитент А").rev_id == c0.rev_id  # a later entry leaves the earlier date alone
    assert M.curve_on(con, D1, "Эмитент А").rev_id == c1.rev_id and M.curve_on(con, D2, "Эмитент А").rev_id == c1.rev_id
    # a correction of the past date: the latest revision of the same valid_from wins, later dates keep their own entry
    c0b, changed = M.put_curve(con, D0, "Эмитент А", {"5Y": 310})
    assert changed and M.curve_on(con, D0, "Эмитент А").rev_id == c0b.rev_id
    assert M.curve_on(con, D1, "Эмитент А").rev_id == c1.rev_id
    # the same request again: nothing to add
    same, changed = M.put_curve(con, D0, "Эмитент А", {"5Y": 310.0})
    assert not changed and same.rev_id == c0b.rev_id
    assert len(M.history(con, "ЭМИТЕНТ А")) == 3 and [c.rev_id for c in M.history(con, "эмитент а")] == [c0b.rev_id, c1.rev_id, c0.rev_id]


def test_a_withdrawal_ends_the_curve_until_a_later_entry(con):
    M.put_curve(con, D0, "Эмитент А", CURVE)
    assert M.put_curve(con, D1, "Эмитент А", None) == (None, True)
    assert M.put_curve(con, D2, "Эмитент А", None) == (None, False)  # nothing in force: nothing to withdraw
    assert M.curve_on(con, D0, "Эмитент А") and M.curve_on(con, D1, "Эмитент А") is None
    assert M.curve_on(con, D2, "Эмитент А") is None and M.curves_on(con, D2) == []
    again, _ = M.put_curve(con, D2, "Эмитент А", {"5Y": 280})
    assert M.curve_on(con, D2, "Эмитент А").rev_id == again.rev_id
    assert [c.cleared for c in M.history(con, "Эмитент А")] == [False, True, False]


def test_known_at_shows_what_an_earlier_run_saw(con):
    M.put_curve(con, D0, "Эмитент А", {"5Y": 300})
    seen = con.execute("SELECT clock_timestamp()").fetchone()[0]
    assert not M.curves_changed_since(con, D1, seen) and not M.curves_changed_since(con, D1, None)
    M.put_curve(con, D1, "Эмитент А", {"5Y": 310})
    assert M.curve_on(con, D1, "Эмитент А", known_at=seen).spreads_bp["5Y"] == 300.0
    assert M.curve_on(con, D1, "Эмитент А").spreads_bp["5Y"] == 310.0
    assert M.curves_changed_since(con, D1, seen)  # a date the new entry reaches
    assert not M.curves_changed_since(con, D0, seen)  # a date it does not reach
    M.put_curve(con, D2, "Эмитент Б", {"5Y": 500})
    assert M.curves_changed_since(con, D2, seen) and not M.curves_changed_since(con, D0, seen)


def test_input_that_is_no_curve_is_refused(con):
    bad = [{"2.5Y": 100}, {"5Y": -1}, {"5Y": 10001}, {"5Y": float("nan")}, {"5Y": float("inf")}, {"5Y": "abc"},
           {"5Y": True}, {}, {"5Y": None}]
    for spreads in bad:
        with pytest.raises(M.CurveError):
            M.put_curve(con, D0, "A", spreads)
    for kw in ({"recovery": 1.5}, {"recovery": -0.01}, {"recovery": "x"}, {"recovery": float("nan")},
               {"ccy": "US"}, {"ccy": "U$D"}):
        with pytest.raises(M.CurveError):
            M.put_curve(con, D0, "A", {"5Y": 1}, **kw)
    for kw in ({"recovery": 1}, {"recovery": 1.0}, {"recovery": 0.99996}):  # 100 % is no recovery rate (nor its rounding)
        with pytest.raises(M.CurveError):
            M.put_curve(con, D0, "A", {"5Y": 1}, **kw)
    assert M.put_curve(con, D0, "Z", {"5Y": 1}, recovery=0.9999)[0].recovery == 0.9999
    for d in (date(1999, 12, 31), date(2101, 1, 1)):  # the table's dates: a refusal, not a database error
        with pytest.raises(M.CurveError):
            M.put_curve(con, d, "A", {"5Y": 1})
        with pytest.raises(M.CurveError):
            M.put_curve(con, d, "A", None)
        with pytest.raises(M.CurveError):
            M.copy_curves(con, d)
    with pytest.raises(M.CurveError):
        M.copy_curves(con, D1, from_date=date(1999, 1, 1))
    assert M.put_curve(con, date(2000, 1, 1), "Z", {"5Y": 1})[1] and M.put_curve(con, date(2100, 12, 31), "Z", {"5Y": 2})[1]
    for name in ("", "   ", "a\x00b", "x" * 121, "A\u200bB", "A\u200eB", "A\u202eB", "\ufeffA", "A\u00adB"):
        with pytest.raises(M.CurveError):
            M.put_curve(con, D0, name, {"5Y": 1})
    assert M.put_curve(con, D0, "A", {"5Y": "12,5", "3Y": ""})[0].spreads_bp["5Y"] == 12.5
    assert M.put_curve(con, D0, "B", {"5Y": 0, "10Y": 10000}, recovery=0, ccy=None)[0].recovery == 0
    assert con.execute("SELECT count(*) FROM manual_cds_rev").fetchone()[0] == 2 + 3  # the refused ones left nothing


def test_copying_enters_the_curves_in_force_before_the_date(con):
    M.put_curve(con, D0, "Эмитент А", {"5Y": 300}, recovery=0.4, ccy="EUR", who="tom")
    M.put_curve(con, D0, "Эмитент Б", {"3Y": 200})
    M.put_curve(con, D0, "Эмитент В", {"3Y": 210})
    M.put_curve(con, D1, "Эмитент В", None)  # withdrawn: nothing to copy from D2 on
    r = M.copy_curves(con, D2, who="bob")
    assert [c.name for c in r.copied] == ["Эмитент А", "Эмитент Б"] and not r.skipped
    a = M.curve_on(con, D2, "Эмитент А")
    assert a.valid_from == D2 and a.changed_by == "bob" and (a.recovery, a.ccy) == (0.4, "EUR")
    assert a.spreads_bp == M.curve_on(con, D0, "Эмитент А").spreads_bp
    assert M.curve_on(con, D2, "Эмитент В") is None
    # a second copy changes nothing: the names have their own entry now; a name asked for with no curve is told
    r = M.copy_curves(con, D2, names=["эмитент   а", "Эмитент Г", "Эмитент В"])
    assert not r.copied and {(k, w) for k, _, w in r.skipped} == {
        ("эмитент а", "на эту дату кривая уже введена"), ("эмитент г", "нет кривой на дату, с которой копируем"),
        ("эмитент в", "нет кривой на дату, с которой копируем")}
    # only the names chosen; the source date given: what was in force on it, not what came after
    M.put_curve(con, D1, "Эмитент Б", {"3Y": 250})
    r = M.copy_curves(con, date(2026, 11, 19), from_date=D0, names=["Эмитент Б"])
    assert r.copied[0].spreads_bp["3Y"] == 200.0 and M.curve_on(con, date(2026, 11, 19), "Эмитент А").valid_from == D2
    with pytest.raises(M.CurveError):
        M.copy_curves(con, D1, from_date=D1)
    with pytest.raises(M.CurveError):
        M.copy_curves(con, D1, names=[""])
    # a copy is one step: a name the store refuses leaves no new entries
    n = con.execute("SELECT count(*) FROM manual_cds_rev").fetchone()[0]
    with pytest.raises(M.CurveError):
        M.copy_curves(con, date(2026, 11, 20), names=["Эмитент А", "x" * 121])
    assert con.execute("SELECT count(*) FROM manual_cds_rev").fetchone()[0] == n


def test_copying_everything_never_undoes_a_withdrawal(con):
    M.put_curve(con, D0, "Эмитент А", {"5Y": 300})
    M.put_curve(con, D0, "Эмитент Б", {"5Y": 400})
    M.put_curve(con, D1, "Эмитент А", None)  # the owner withdrew the name on D1
    r = M.copy_curves(con, D1)  # a copy of everything: Б has none of its own on D1, А was withdrawn there
    assert [c.name for c in r.copied] == ["Эмитент Б"]
    assert r.skipped == [("эмитент а", "Эмитент А", "на эту дату имя снято")]
    assert M.curve_on(con, D1, "Эмитент А") is None
    # only a name asked for is brought back
    r = M.copy_curves(con, D1, names=["Эмитент А"])
    assert [c.name for c in r.copied] == ["Эмитент А"] and M.curve_on(con, D1, "Эмитент А").spreads_bp["5Y"] == 300.0


def test_copy_reads_the_source_after_the_locks_of_the_names(store_url):
    """A change of a name that holds its lock (a put in another transaction) is seen by a copy that waited for it: the
    source is read after the lock, not before."""
    import threading
    with psycopg.connect(store_url, autocommit=True) as a, psycopg.connect(store_url, autocommit=True) as b:
        key = M.name_key("Эмитент А")
        out: dict = {}

        def copy():
            out["r"] = M.copy_curves(b, D1, names=["Эмитент А"])

        with a.transaction():
            a.execute("SELECT pg_advisory_xact_lock(hashtext('manual_cds:' || %s))", (key,))
            a.execute("INSERT INTO manual_cds_rev (name_key, name, valid_from, spread_5y, recovery, ccy)"
                      " VALUES (%s, 'Эмитент А', %s, 300, 0.25, 'USD')", (key, D0))
            t = threading.Thread(target=copy)
            t.start()
            t.join(0.7)
            assert t.is_alive(), "the copy has to wait for the lock of the name"
        t.join(10)
        assert not t.is_alive()
        assert [c.name for c in out["r"].copied] == ["Эмитент А"] and not out["r"].skipped


# ---- the routes --------------------------------------------------------------------------------------------------


def test_set_list_get_history_through_the_api(local, store_url):
    r = put(local, "  Эмитент   А ", D0, recovery=0.4)
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["changed"] and j["curve"]["name"] == "Эмитент А" and j["curve"]["name_key"] == "эмитент а"
    assert j["curve"]["changed_by"] == "local" and j["curve"]["entered_on_date"] and j["curve"]["manual"]
    assert j["curve"]["spreads_bp"] == {t: float(v) for t, v in CURVE.items()} and j["curve"]["recovery"] == 0.4
    assert not put(local, " Эмитент  А", D0, recovery=0.4).json()["changed"]  # no change, no revision
    # the day after: carried, not entered on it
    lst = local.get(BASE + f"/curves?date={D1}").json()
    assert lst["tenors"] == list(CURVE) and lst["default_recovery"] == 0.25 and lst["default_ccy"] == "USD"
    assert [(c["name"], c["valid_from"], c["entered_on_date"]) for c in lst["curves"]] == [("Эмитент А", "2026-11-16",
                                                                                          False)]
    assert local.get(BASE + f"/curves?date={D0 - timedelta(days=1)}").json()["curves"] == []
    got = local.get(BASE + "/curve", params={"name": "ЭМИТЕНТ А", "date": D1.isoformat()})
    assert got.status_code == 200 and got.json()["entered_on_date"] is False
    assert local.get(BASE + "/curve", params={"name": "Нет такого", "date": D1.isoformat()}).status_code == 404
    put(local, "Эмитент А", D0, {"5Y": 305})  # the past date corrected
    put(local, "Эмитент А", D1, {"5Y": 320})
    h = local.get(BASE + "/history", params={"name": "эмитент а"}).json()
    assert h["name_key"] == "эмитент а" and [(x["valid_from"], x["spreads_bp"]["5Y"]) for x in h["revisions"]] == [
        ("2026-11-17", 320.0), ("2026-11-16", 305.0), ("2026-11-16", 300.0)]
    assert all(x["changed_by"] == "local" and x["changed_at"] for x in h["revisions"])
    assert local.get(BASE + "/history", params={"name": "никто"}).json()["revisions"] == []
    # a withdrawal, then the name is gone from the list of the date and back in the history
    w = local.put(BASE + "/curve", json={"name": "Эмитент А", "date": D2.isoformat(), "clear": True}).json()
    assert w == {"changed": True, "curve": None}
    assert local.get(BASE + f"/curves?date={D2}").json()["curves"] == []
    assert local.get(BASE + f"/curves?date={D1}").json()["curves"][0]["spreads_bp"]["5Y"] == 320.0
    assert local.get(BASE + "/history", params={"name": "эмитент а"}).json()["revisions"][0]["cleared"] is True
    assert not local.put(BASE + "/curve", json={"name": "Эмитент А", "date": D2.isoformat(), "clear": True}
                         ).json()["changed"]
    # every change is in the audit log, with its detail; the refused and the unchanged are not
    audit = rows(store_url, "SELECT actor, action, target, detail FROM audit_log WHERE action = 'manual_cds'"
                            " ORDER BY audit_id")
    assert [(a, t, d["date"], d["clear"]) for a, _, t, d in audit] == [
        ("local", "эмитент а", "2026-11-16", False), ("local", "эмитент а", "2026-11-16", False),
        ("local", "эмитент а", "2026-11-17", False), ("local", "эмитент а", "2026-11-18", True)]
    assert audit[0][3]["spreads_bp"]["5Y"] == 300.0 and audit[0][3]["recovery"] == 0.4 and audit[0][3]["ccy"] == "USD"


def test_bad_input_through_the_api(local, store_url):
    for body in ({"name": "A", "date": "2026-11-16", "spreads_bp": {"2.5Y": 1}},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": -5}},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": 1e9}},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {}},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": 1}, "recovery": 1.2},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": 1}, "recovery": 1},
                 {"name": "A", "date": "1999-12-31", "spreads_bp": {"5Y": 1}},
                 {"name": "A", "date": "2101-01-01", "clear": True},
                 {"name": "A\u200bB", "date": "2026-11-16", "spreads_bp": {"5Y": 1}},
                 {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": 1}, "ccy": "RUBL"},
                 {"name": "  ", "date": "2026-11-16", "spreads_bp": {"5Y": 1}},
                 {"name": "x" * 121, "date": "2026-11-16", "spreads_bp": {"5Y": 1}}):
        r = local.put(BASE + "/curve", json=body)
        assert r.status_code == 422 and r.json()["code"] == "invalid_parameter", (body, r.text)
    for body in ({"name": "A", "spreads_bp": {"5Y": 1}}, {"name": "A", "date": "2026-13-01", "spreads_bp": {"5Y": 1}},
                 {"date": "2026-11-16", "spreads_bp": {"5Y": 1}}):
        assert local.put(BASE + "/curve", json=body).status_code == 422, body
    assert local.get(BASE + "/curves").status_code == 422 and local.get(BASE + "/curves?date=x").status_code == 422
    assert local.post(BASE + "/copy", json={"to_date": "2026-11-16", "from_date": "2026-11-16"}).status_code == 422
    assert local.post(BASE + "/copy", json={"to_date": "1999-12-31"}).status_code == 422
    assert rows(store_url, "SELECT count(*) FROM manual_cds_rev") == [(0,)]
    assert rows(store_url, "SELECT count(*) FROM audit_log WHERE action = 'manual_cds'") == [(0,)]


def test_writes_are_local_only_and_json_only(store_url):
    app = create_app(store_url)
    remote = TestClient(app, base_url="http://127.0.0.1:8100", client=("10.1.2.3", 50000))
    body = {"name": "A", "date": "2026-11-16", "spreads_bp": {"5Y": 1}}
    assert remote.put(BASE + "/curve", json=body).status_code == 403
    assert remote.post(BASE + "/copy", json={"to_date": "2026-11-17"}).status_code == 403
    assert remote.get(BASE + "/curves?date=2026-11-16").status_code == 200  # reads are open, like the rest of the API
    rebound = TestClient(app, base_url="http://evil.example:8100", client=("127.0.0.1", 50000))
    assert rebound.put(BASE + "/curve", json=body).status_code == 403
    local = TestClient(app, **LOCAL)
    assert local.put(BASE + "/curve", json=body, headers={"Origin": "http://evil.example"}).status_code == 403
    form = local.put(BASE + "/curve", content="name=A", headers={"Content-Type": "text/plain"})
    assert form.status_code == 415
    assert local.post(BASE + "/copy", content="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert rows(store_url, "SELECT count(*) FROM manual_cds_rev") == [(0,)]


def test_copy_through_the_api(local, store_url):
    put(local, "Эмитент А", D0)
    put(local, "Эмитент Б", D0, {"3Y": 200})
    r = local.post(BASE + "/copy", json={"to_date": D1.isoformat(), "names": ["эмитент б"]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["to_date"] == "2026-11-17" and [c["name"] for c in j["copied"]] == ["Эмитент Б"] and j["skipped"] == []
    assert j["copied"][0]["entered_on_date"] and j["copied"][0]["valid_from"] == "2026-11-17"
    j = local.post(BASE + "/copy", json={"to_date": D1.isoformat()}).json()  # the rest
    assert [c["name"] for c in j["copied"]] == ["Эмитент А"]
    assert [(s["name_key"], s["why"]) for s in j["skipped"]] == [("эмитент б", "на эту дату кривая уже введена")]
    j = local.post(BASE + "/copy", json={"to_date": D1.isoformat(), "names": ["Эмитент А", "Эмитент Я"]}).json()
    assert j["copied"] == [] and {(s["name_key"], s["why"]) for s in j["skipped"]} == {
        ("эмитент а", "на эту дату кривая уже введена"), ("эмитент я", "нет кривой на дату, с которой копируем")}
    assert all(c["entered_on_date"] for c in local.get(BASE + f"/curves?date={D1}").json()["curves"])
    audit = rows(store_url, "SELECT target, detail FROM audit_log WHERE action = 'manual_cds' AND target = 'copy'"
                            " ORDER BY audit_id")
    assert [d["names"] for _, d in audit] == [["эмитент б"], ["эмитент а"]]  # the empty copy is not logged


def _run_with_cds(url: str, on: date, rows_: list[dict]) -> int:
    with store.connect(url) as con:
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'p.csv', 'p.csv', 1,"
                    " now())", ("a" * 64,))
        sid = store.store_inbox_set(
            con, book_id="B1", valid_date=on, source="positions_csv", priority=None, report_date=on, report_ts=None,
            seq=None, generated_at=None, file_mtime=datetime.now(UTC), file_name="p.csv", sha256="a" * 64,
            adapter_fingerprint="fp", unit_hash="u", rows=[{"_src": f"L{i}", "book_id": "B1", **r}
                                                         for i, r in enumerate(rows_)])
        return con.execute(
            "INSERT INTO run (run_type, business_date, cut_type, status, completed_at, market_snapshot_id,"
            " static_snapshot_id, private_inputs_hash, position_set_ids, positions_known_at, core_version,"
            " backend_version, lineage) VALUES ('EOD', %s, 'eod_final', 'complete', now(), 'm', 's', 'h', %s, now(),"
            " 'c', 'b', '{}') RETURNING run_id", (on, [sid])).fetchone()[0]


def test_the_list_names_the_cds_rows_of_the_dates_run(local, store_url):
    cds = {"asset_class": "otc", "ccy": "USD", "amount_ccy": "1000000", "x_product_group": "cds"}
    rid = _run_with_cds(store_url, D1, [
        {"position_id": "P1", "source_instrument_name": "CDS Эмитент А 5Y", "x_issuer": " Эмитент  А ", **cds},
        {"position_id": "P2", "source_instrument_name": "CDS Эмитент А 3Y", "x_issuer": "эмитент а", **cds},
        {"position_id": "P3", "source_instrument_name": "CDS-Б", **cds},  # no issuer: the deal's name
        {"position_id": "P4", "source_instrument_id": "CDS-В", **cds},  # no name either: the id
        {"position_id": "P5", "source_instrument_name": "Своп", "asset_class": "otc", "ccy": "RUB",
         "x_product_group": "irs", "x_issuer": "Эмитент Г"},
        {"position_id": "P6", "source_instrument_name": "Облигация", "asset_class": "bond", "ccy": "RUB",
         "x_issuer": "Эмитент Д"}])
    put(local, "Эмитент А", D0)
    lst = local.get(BASE + f"/curves?date={D1}").json()
    assert lst["run_id"] == rid
    assert lst["positions"] == [
        {"name": "CDS-Б", "name_key": "cds-б", "positions": 1, "deals": ["CDS-Б"], "has_curve": False},
        {"name": "CDS-В", "name_key": "cds-в", "positions": 1, "deals": [], "has_curve": False},
        {"name": "Эмитент А", "name_key": "эмитент а", "positions": 2, "deals": ["CDS Эмитент А 3Y",
                                                                                 "CDS Эмитент А 5Y"], "has_curve": True}]
    other = local.get(BASE + f"/curves?date={D2}").json()  # a date without a run: no names, the curves as usual
    assert other["run_id"] is None and other["positions"] == [] and len(other["curves"]) == 1


def test_who_may_change_with_sign_in_on(store_url, home: Path):  # noqa: F811
    add_users(store_url)
    body = {"name": "Эмитент А", "date": "2026-11-16", "spreads_bp": {"5Y": 300}}
    for name, status in (("boss", 200), ("tom", 200), ("vic", 403)):
        c = signed_in(store_url, name)
        r = c.put(BASE + "/curve", json=body | {"spreads_bp": {"5Y": 300 + len(name)}}, headers=csrf(c))
        assert r.status_code == status, (name, r.text)
        assert c.get(BASE + "/curves?date=2026-11-16", headers=csrf(c)).status_code == 200  # every role reads
        copy = c.post(BASE + "/copy", json={"to_date": "2026-11-17"}, headers=csrf(c))
        assert copy.status_code == (200 if status == 200 else 403), (name, copy.text)
    anon = TestClient(create_app(store_url, auth=ON), base_url="http://desk.example:8100",
                      client=("10.1.2.3", 50000))
    assert anon.put(BASE + "/curve", json=body).status_code == 401
    assert rows(store_url, "SELECT changed_by FROM manual_cds_rev WHERE valid_from = '2026-11-16' ORDER BY rev_id") == [
        ("boss",), ("tom",)]
    assert {a for (a,) in rows(store_url, "SELECT actor FROM audit_log WHERE action = 'manual_cds'")} == {"boss", "tom"}
