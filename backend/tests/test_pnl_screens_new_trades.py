"""TDM#568: the PnL of the day's trades (``new_trades``, the factor «trading») is in every PnL and the totals agree.

Synthetic history of ``test_pnl_period`` (a position closed earlier in the month, one bought and sold in a day, one
opened today with a trade): the month and year to date of the concept pages' data (``/concepts-data.js``, summed over
the run's positions and the positions the run no longer has) equal the PnL report's (``/api/v1/history/pnl/period``)
and the sum of its days, and all of them contain the trades' PnL; the trades screen reads the PnL of the run it is
asked for (default: the date's main run)."""
from __future__ import annotations

import json
import math

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_concepts import desk  # noqa: F401 - fixture
from test_pnl_period import DAYS, fact, seed_period

from desk import store, trades
from desk.api import concepts, create_app
from desk.store import PNL_COMPONENTS

LAST = DAYS[-1]
TRADES = [{"trade_id": "tr-1", "side": "buy", "quantity": 3.0, "price_pct": 99.0, "pnl_rub": 100.0, "flag": None},
          {"trade_id": "tr-2", "side": "sell", "quantity": 3.0, "price_pct": 101.0, "pnl_rub": 50.0, "flag": None}]
OPENED = [{"trade_id": "tr-3", "side": "buy", "quantity": 5.0, "price_pct": 99.0, "pnl_rub": 777.0, "flag": None}]


def more(con, rid, day):
    if day != LAST:
        return
    # A1: bought and sold three bonds in a day (the quantity does not change), the trades' PnL 150
    con.execute("UPDATE run_position SET pnl = jsonb_set(jsonb_set(pnl, '{new_trades}', '150'::jsonb),"
                " '{extra}', (pnl->'extra') || %s::jsonb) WHERE run_id = %s AND position_id = 'A1'",
                (Jsonb({"start_quantity": 15.0, "trades": TRADES}), rid))
    # T1: opened today, only the trades' PnL
    pnl = {k: 0.0 for k in ("carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "convexity", "other")}
    pnl.update(new_trades=777.0, extra={"method": "desk-flash-v1", "total": 777.0, "prev_date": "2026-10-15",
                                        "start_quantity": 0.0, "trades": OPENED})
    con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,pv_rub,dv01_rub,"
                "cs01_rub,fact,pnl) VALUES (%s,'T1',99,'DEMO-A','SYN-1',5,'exact',1e6,-400,-10,%s,%s)",
                (rid, Jsonb(fact("A1", 11, day)), Jsonb(pnl)))
    # N2: the start of the day has no PnL, its trade is kept in no_pnl
    con.execute("INSERT INTO run_position(run_id,position_id,ord,book_id,secid,quantity,quality,pv_rub,dv01_rub,"
                "cs01_rub,fact,no_pnl) VALUES (%s,'N2',98,'DEMO-A','SYN-1',5,'exact',1e6,-400,-10,%s,%s)",
                (rid, Jsonb(fact("A1", 11, day)),
                 Jsonb({"reason": "no_prev_report", "detail": "synthetic", "start_quantity": 2.0,
                        "trades": [{"trade_id": "tr-4", "side": "buy", "quantity": 3.0, "pnl_rub": 5.0}]})))
    # A1 moves to the other book on the last day: one position id, two books
    con.execute("UPDATE run_position SET book_id = 'DEMO-B' WHERE run_id = %s AND position_id = 'A1'", (rid,))


@pytest.fixture
def screens(store_url):
    ids = seed_period(store_url, more=more)
    return TestClient(create_app(store_url)), ids, store_url


def period(client, start):
    r = client.get("/api/v1/history/pnl/period", params={"from": start, "to": LAST, "gran": "month"})
    assert r.status_code == 200, r.text
    return r.json()


def extras(client, run_id):
    js = client.get(f"/concepts-data.js?run_id={run_id}")
    assert js.status_code == 200, js.text
    return json.loads(js.text[len("window.DESK_EXTRAS = "):-2])


def test_mtd_ytd_of_the_header_are_the_reports_and_the_sum_of_the_history(screens):
    client, ids, _url = screens
    x = extras(client, ids[-1])
    closed = x["pnlClosed"]
    rep0 = period(client, "2026-10-01")
    both = math.fsum(q["total"] for q in rep0["positions"] if q["positionId"] == "A1")  # two books, one id
    assert x["positions"]["A1"]["pnlMtd"] == pytest.approx(both)
    assert {c["positionId"] for c in closed} == {"C1"}  # sold on 2026-10-08, in no later run
    for key, start in (("pnlMtd", "2026-10-01"), ("pnlYtd", "2026-01-01")):
        short = key[3:].lower()
        header = math.fsum([p[key] for p in x["positions"].values() if key in p] + [c[short] for c in closed])
        rep = period(client, start)
        assert header == pytest.approx(rep["summary"]["total"])
        assert header == pytest.approx(math.fsum(d["total"] for d in rep["days"] if d["total"] is not None))
    # the trades' PnL (the factor «trading») is in all of them
    rep = period(client, "2026-10-01")
    assert rep["summary"]["c"]["new_trades"] == pytest.approx(150.0 + 777.0)
    assert math.fsum(p["pnlMtdParts"]["trading"] for p in x["positions"].values()
                     if (p.get("pnlMtdParts") or {}).get("trading") is not None) == pytest.approx(150.0 + 777.0)
    assert x["positions"]["T1"]["pnlMtd"] == pytest.approx(777.0)  # a position of the day: only its trades
    # the components of the month sum to the total, position by position
    for p in x["positions"].values():
        if "pnlMtd" not in p:
            continue
        parts = [v for v in p["pnlMtdParts"].values() if v is not None]
        assert math.fsum(parts) == pytest.approx(p["pnlMtd"])


def test_a_day_trade_is_from_the_recorded_trades_not_the_change_of_the_quantity(screens):
    client, ids, _url = screens
    p = extras(client, ids[-1])["positions"]
    assert p["A1"]["trade"]["n"] == 2  # bought and sold: the quantity is the same
    assert p["T1"]["trade"]["n"] == 1 and p["T1"]["trade"]["new"]
    assert p["N2"]["trade"]["n"] == 1  # the start of the day has no PnL: the trade is in no_pnl.trades
    assert "trade" not in p["B1"]


def test_the_trades_screen_reads_the_asked_run_else_the_main_run_of_the_date(screens):
    client, ids, url = screens
    with store.connect(url) as con:
        main = ids[-1]
        late = con.execute(
            "INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,"
            "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage,status)"
            " SELECT 'INTRADAY',business_date,prev_date,'intraday',market_snapshot_id,static_snapshot_id,"
            "private_inputs_hash,position_set_ids,positions_known_at,core_version,backend_version,lineage,'complete'"
            " FROM run WHERE run_id = %s RETURNING run_id", (main,)).fetchone()[0]
        test = con.execute("INSERT INTO run(run_type,business_date,prev_date,cut_type,market_snapshot_id,"
                           "static_snapshot_id,private_inputs_hash,position_set_ids,positions_known_at,core_version,"
                           "backend_version,lineage,status) SELECT 'TEST',business_date,prev_date,'eod_final',"
                           "market_snapshot_id,static_snapshot_id,private_inputs_hash,position_set_ids,"
                           "positions_known_at,core_version,backend_version,lineage,'complete' FROM run"
                           " WHERE run_id = %s RETURNING run_id", (main,)).fetchone()[0]
        assert late > main and test > late
        day = con.execute("SELECT business_date FROM run WHERE run_id = %s", (main,)).fetchone()[0]
        assert trades._intraday_run(con, day)["run_id"] == main  # the main run, not the later intraday or test run
        assert trades._intraday_run(con, day, late)["run_id"] == late  # the run asked for
        assert trades._intraday_run(con, day, 10**9)["run_id"] == main  # no such run: the main run
        assert set(trades._trade_pnl(con, trades._intraday_run(con, day))) == {"tr-1", "tr-2", "tr-3", "tr-4"}
        assert trades._trade_pnl(con, trades._intraday_run(con, day, late)) == {}
    r = client.get("/api/v1/trades", params={"run_id": late})  # no date: the run's date
    assert r.status_code == 200 and r.json()["date"] == LAST and r.json()["intraday"]["run_id"] == late
    assert client.get("/api/v1/trades", params={"date": LAST}).json()["intraday"]["run_id"] == main


def test_an_overlapping_intraday_run_is_apart_in_the_header_as_in_the_report(store_url):
    ids = seed_period(store_url, more=more, intraday_anchor_prev="2026-10-14")
    client = TestClient(create_app(store_url))
    x = extras(client, ids[-1])
    assert x["pnlPeriods"]["mtd"]["intradayApart"] and any("внутридневной прогон" in n for n in x["notes"])
    header = math.fsum([p["pnlMtd"] for p in x["positions"].values() if "pnlMtd" in p]
                       + [c["mtd"] for c in x["pnlClosed"]])
    rep = period(client, "2026-10-01")
    assert rep["separateAnchor"]["overlappingDates"]
    assert header == pytest.approx(rep["summary"]["total"])
    assert header == pytest.approx(math.fsum(d["total"] for d in rep["days"] if d["total"] is not None))
    year = math.fsum([p["pnlYtd"] for p in x["positions"].values() if "pnlYtd" in p] + [c["ytd"] for c in x["pnlClosed"]])
    assert x["pnlPeriods"]["ytd"]["intradayApart"]
    assert year == pytest.approx(period(client, "2026-01-01")["summary"]["total"])


def test_trade_of_a_position_sold_to_zero_and_the_face_traded():
    pos = [10000.0, 0, 0, 0, 0, 0, 99.0]  # notional of 10 bonds
    two = [{"side": "buy", "quantity": 3.0}, {"side": "sell", "quantity": 1.0}]
    t = concepts.trade_of({"trades": two, "quantity": 10.0, "start": 9.0}, pos)
    assert t["qty"] == pytest.approx(2 * 1000.0) and t["n"] == 2 and "new" not in t
    zero = concepts.trade_of({"trades": [{"side": "sell", "quantity": 9.0}], "quantity": 0.0, "start": 9.0},
                             [0.0, 0, 0, 0, 0, 0, 99.0])
    assert zero["qty"] is None and zero["n"] == 1  # sold to zero: the face of a bond is not known
    assert concepts.trade_of({"trades": [{"side": "buy"}], "quantity": 5.0, "start": 0.0}, pos)["qty"] is None


def test_the_change_of_the_quantity_is_not_a_trade_when_the_run_records_trades(screens):
    client, ids, _url = screens
    p = extras(client, ids[-1])["positions"]
    assert "trade" not in p["B1"] and "trade" not in p["F1"]  # their holdings are the same as the day before


def test_cash_screen_opens_on_the_date_of_the_run(screens):
    client, ids, _url = screens
    r = client.get("/api/v1/cash", params={"run_id": ids[-3]})
    assert r.status_code == 200 and r.json()["date"] == DAYS[-3]


def test_a_date_with_only_intraday_runs_takes_the_latest_of_them_not_a_test_run(screens):
    _client, ids, url = screens
    cols = ("business_date,prev_date,cut_type,market_snapshot_id,static_snapshot_id,private_inputs_hash,"
            "position_set_ids,positions_known_at,core_version,backend_version,lineage,status")
    with store.connect(url) as con:
        got = []
        for kind, cut in (("INTRADAY", "intraday"), ("INTRADAY", "intraday"), ("TEST", "eod_final")):
            got.append(con.execute(
                f"INSERT INTO run(run_type,{cols}) SELECT %s,'2026-10-20',prev_date,%s,market_snapshot_id,"
                "static_snapshot_id,private_inputs_hash,position_set_ids,positions_known_at,core_version,"
                "backend_version,lineage,'complete' FROM run WHERE run_id = %s RETURNING run_id",
                (kind, cut, ids[-1])).fetchone()[0])
        day = con.execute("SELECT business_date FROM run WHERE run_id = %s", (got[0],)).fetchone()[0]
        assert trades._intraday_run(con, day)["run_id"] == got[1]  # the later intraday, not the test run
        assert trades._intraday_run(con, day, got[2])["run_id"] == got[2]  # asked for explicitly


def test_book_totals_components_sum_to_total(desk, store_url):  # noqa: F811
    with store.connect(store_url) as con:
        totals = con.execute("SELECT totals FROM run WHERE status = 'complete'").fetchall()
    checked = 0
    for (t,) in totals:
        for book in (t or {}).values():
            pnl = book["pnl"]
            if pnl.get("total") is None:
                continue
            assert "convexity" in pnl
            assert math.fsum(pnl[k] or 0.0 for k in PNL_COMPONENTS) == pytest.approx(pnl["total"])
            checked += 1
    assert checked
