"""The monitors' run (TDM#289): ``/concepts-run.js`` (``window.CONCEPTS_RUN``) and ``/api/v1/concepts/run``.

Temporary parity with window.DESK (``api.compat``) while the monitors move off it (TDM#107): on golden and fixture
runs the new endpoint equals compat on every key ``prototypes/concepts/lib/real.js`` (and ``kit.js``) reads, listed
here explicitly, and has no other key. Drop the parity tests together with compat (TDM#107, step 4)."""

from __future__ import annotations

import copy
import dataclasses
import json
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from store_rows import insert_run
from test_api import DATE as API_DATE
from test_api import book, client, run  # noqa: F401 - fixtures
from test_funding_run import DATE, SNAPS, _fresh_process, _home, _runs, _scan  # noqa: F401 - fixtures

from desk import store
from desk.api import create_app
from desk.api.app import warm_bodies
from desk.api.concepts import cbr
from desk.marketdata import static_facts
from desk.marketdata.adapter import MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import attach_pnl, prepare, value

LIVE = Path(__file__).parent / "fixtures" / "hub_live_2026-10-20"

# What lib/real.js reads of window.DESK (kit.js: a position's id and extra). The parity is checked on these, and
# the new endpoint has exactly these keys.
TOP = {"asOf", "prevDate", "pnlAvailable", "pnlReasonText", "accrualDays", "BUCKETS", "PILLARS", "CURVES",
       "CURVE_MOVES", "FX", "FX_MOVE_PCT", "FX_CBR", "FX_CBR_MOVE_PCT", "FX_SOURCE", "INDICES", "ISSUERS", "BOOKS",
       "riskNotes", "fundingStatus", "source", "positions"}
SOURCE = {"runId", "runType", "slice", "cutType", "cutTs", "publishedAt", "pnlBasis", "marketDate", "marketCutType",
          "ofzScheme", "curveUnits"}
POSITION = {"id", "book", "assetClass", "segment", "instrType", "name", "isin", "issuer", "issuerGroup", "sector",
            "rating", "ratingInfo", "ccy", "maturity", "tenorY", "couponType", "coupon", "index", "margin", "notional",
            "notionalRub", "price", "mv", "mvRub", "ytm", "spreadBp", "mdur", "sprDur", "cs01", "ofzSpr01", "risk",
            "fxDelta", "counterparty", "newToday", "entity", "portfolio", "pnl", "accrualOnly", "ocpTransfer", "extra",
            "notValued",
            # a bond's
            "instrumentId", "accrued", "couponFreq", "yieldDate", "spreadCurve"}
PNL = {"available", "reasonText", "carry", "rolldown", "rates", "convexity", "spread", "fx", "funding", "newTrades",
       "other", "reported", "explained", "method", "methodText"}


MOVES = {"CURVE_MOVES", "FX_MOVE_PCT", "FX_CBR_MOVE_PCT"}
"""The day's moves: null where the run has none (TDM#334, B1), 0 in compat; equal to compat with null as 0."""


def zeroed(v):
    """``v`` with every null (at any depth) as compat's 0."""
    if isinstance(v, dict):
        return {k: zeroed(x) for k, x in v.items()}
    if isinstance(v, list):
        return [zeroed(x) for x in v]
    return 0.0 if v is None else v


def assert_moves(got: dict) -> None:
    """A run without a previous market has no move but the rouble's own 0: every curve's and rate's move is null."""
    if got["prevDate"] is None:
        assert all(v is None for mv in got["CURVE_MOVES"].values() for v in mv)
        for k in ("FX_MOVE_PCT", "FX_CBR_MOVE_PCT"):
            assert got[k]["RUB"] == 0 and all(v is None for c, v in got[k].items() if c != "RUB"), k
    nb = len(got["BUCKETS"])
    assert all(len(mv) == nb for mv in got["CURVE_MOVES"].values())


def read(desk: dict) -> dict:
    """window.DESK as lib/real.js sees it: the keys above, with their presence."""
    out = {k: v for k, v in desk.items() if k in TOP}
    out["source"] = {k: v for k, v in desk["source"].items() if k in SOURCE}
    out["positions"] = [{**{k: v for k, v in p.items() if k in POSITION},
                         "pnl": {k: v for k, v in p["pnl"].items() if k in PNL}} for p in desk["positions"]]
    return out


def assert_parity(c: TestClient, rid: int) -> dict:
    """``/api/v1/concepts/run`` of the run equals compat on what the pages read, has nothing else, and
    ``/concepts-run.js`` is the same document."""
    want = read(c.get(f"/api/v1/compat/desk?run_id={rid}").json())
    r = c.get(f"/api/v1/concepts/run?run_id={rid}")
    assert r.status_code == 200, r.text
    got = r.json()
    assert set(got) == TOP and set(got["source"]) <= SOURCE
    assert all(set(p) <= POSITION and set(p["pnl"]) == PNL for p in got["positions"])
    for k in TOP:
        assert (zeroed(got[k]) if k in MOVES else got[k]) == want[k], k
    assert_moves(got)
    js = c.get(f"/concepts-run.js?run_id={rid}").text
    assert js.startswith("window.CONCEPTS_RUN = ") and js.endswith(";\n")
    assert json.loads(js[len("window.CONCEPTS_RUN = "):-2]) == got
    return got


def test_parity_on_the_golden_runs(store_url, book, client):  # noqa: F811
    """The S2 pair of test_api: the 14th without a previous market (no PnL, R10), the 15th with the flash PnL of the
    exact bonds and a bond the run could not value (listed with ``notValued``)."""
    ctx, positions, view = book
    market = copy.copy(ctx.market)
    market.business_date = view.business_date
    early = insert_run(store_url, dataclasses.replace(ctx, market=market), positions, None)
    rid = run(store_url, book, cut_type="eod_preliminary")
    got = assert_parity(client, rid)
    assert got["pnlAvailable"] and any(p["pnl"]["available"] for p in got["positions"])
    nv = [p for p in got["positions"] if p.get("notValued")]
    assert len(nv) == 1 and nv[0]["mvRub"] is None and nv[0]["pnl"]["available"] is False
    assert_parity(client, early)
    # the run shown, by date and by default, is the one /desk-data.js shows
    shown = client.get("/api/v1/compat/desk").json()["source"]["runId"]
    assert client.get("/api/v1/concepts/run").json()["source"]["runId"] == shown
    assert client.get(f"/api/v1/concepts/run?as_of={API_DATE}").json()["source"]["runId"] == \
        client.get(f"/api/v1/compat/desk?as_of={API_DATE}").json()["source"]["runId"]


def test_moves_null_when_the_run_has_none(store_url, book):  # noqa: F811
    """TDM#334: a move the run does not have is null, not 0, so that the monitors say «сдвигов нет в прогоне»: every
    move of a run without a previous market (the publisher stores 0 there), a curve point without ``move_bp``, a
    rate without a previous one (no ``prev_rate``); the rouble's own move stays 0, a move the run has stays."""
    ctx, positions, view = book
    market = copy.copy(ctx.market)
    market.business_date = view.business_date
    early = insert_run(store_url, dataclasses.replace(ctx, market=market), positions, None)
    rid = run(store_url, book)
    # the golden book has no currency: the rates as the publisher stores them (``tdm_run._fx_rows``) -- USD with a
    # previous rate, CNY without one (its move stored as 0), the CBR's move unknown for CNY, EUR unmoved (a real 0)
    usd = {"ccy": "USD", "rate": 90.0, "rate_source": "MOEX 19:00", "move_pct": 0.5, "prev_rate": 89.55,
           "prev_rate_source": "MOEX 19:00", "rate_cbr": 90.1, "move_pct_cbr": 0.4, "rate_cbr_next": 90.2}
    cny = {"ccy": "CNY", "rate": 12.5, "rate_source": "MOEX 19:00", "move_pct": 0.0, "rate_cbr": 12.4,
           "move_pct_cbr": None, "rate_cbr_next": 12.4}
    eur = {"ccy": "EUR", "rate": 100.0, "rate_source": "MOEX 19:00", "move_pct": 0.0, "prev_rate": 100.0,
           "prev_rate_source": "MOEX 19:00", "rate_cbr": 100.1, "move_pct_cbr": 0.0, "rate_cbr_next": 100.1}
    with psycopg.connect(store_url, row_factory=dict_row) as con:
        stored = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'", (early,)).fetchone()
        assert any(p.get("move_bp") == 0 for c in stored["data"] for p in c["points"])  # stored as 0
        curves = con.execute("SELECT data FROM run_reference WHERE run_id = %s AND kind = 'curves'", (rid,)).fetchone()
        con.execute("ALTER TABLE run_reference DISABLE TRIGGER USER")  # a published run's rows are frozen
        for r in (early, rid):
            con.execute("DELETE FROM run_reference WHERE run_id = %s AND kind = 'fx'", (r,))
            con.execute("INSERT INTO run_reference (run_id, kind, data) VALUES (%s, 'fx', %s)",
                        (r, json.dumps([usd, cny, eur])))
    c = TestClient(create_app(store_url))
    no_prev = c.get(f"/api/v1/concepts/run?run_id={early}").json()
    assert no_prev["prevDate"] is None and no_prev["CURVE_MOVES"]
    assert all(v is None for mv in no_prev["CURVE_MOVES"].values() for v in mv)
    assert no_prev["FX_MOVE_PCT"] == no_prev["FX_CBR_MOVE_PCT"] == {"RUB": 0, "USD": None, "CNY": None, "EUR": None}

    got = c.get(f"/api/v1/concepts/run?run_id={rid}").json()
    assert got["prevDate"] is not None
    ofz = got["CURVE_MOVES"]["RUB OFZ"]
    assert all(v is not None for v in ofz), ofz  # the KBD's move, on the previous market's KBD
    # a real 0 stays 0, not null (a regression «x or None» fails here)
    assert got["FX_MOVE_PCT"] == {"RUB": 0, "USD": 0.5, "CNY": None, "EUR": 0}
    assert got["FX_CBR_MOVE_PCT"] == {"RUB": 0, "USD": 0.4, "CNY": None, "EUR": 0}
    assert got["FX_MOVE_PCT"]["EUR"] is not None and got["FX_CBR_MOVE_PCT"]["EUR"] is not None
    # the stored reference: one point without its move, the next one unmoved (move_bp 0); a position in a currency the
    # reference lacks, its rate from ``extra.fx_rate`` (its move unknown), with risk on a curve the reference lacks
    x = next(x for x in curves["data"] if len(x["points"]) > 1)
    del x["points"][0]["move_bp"]
    x["points"][1]["move_bp"] = 0.0
    gone, zero = x["points"][0]["bucket"], x["points"][1]["bucket"]
    with psycopg.connect(store_url, row_factory=dict_row) as con:
        con.execute("ALTER TABLE run_reference DISABLE TRIGGER USER")
        con.execute("ALTER TABLE run_position DISABLE TRIGGER USER")
        con.execute("UPDATE run_reference SET data = %s WHERE run_id = %s AND kind = 'curves'",
                    (json.dumps(curves["data"]), rid))
        pos = con.execute("SELECT position_id, fact FROM run_position WHERE run_id = %s AND fact IS NOT NULL"
                          " ORDER BY ord LIMIT 1", (rid,)).fetchone()
        fact = pos["fact"]
        bk = next(iter(next(iter(fact["risk"].values()))))  # a bucket name of the fact's risk
        fact.update(ccy="CHF", extra={**(fact.get("extra") or {}), "fx_rate": 95.5, "fx_source": "ЦБ"},
                    risk={**fact["risk"], "GBP": {bk: 12.0}})
        con.execute("UPDATE run_position SET fact = %s WHERE run_id = %s AND position_id = %s",
                    (json.dumps(fact), rid, pos["position_id"]))
    got = TestClient(create_app(store_url)).get(f"/api/v1/concepts/run?run_id={rid}").json()
    nb, mv = len(got["BUCKETS"]), got["CURVE_MOVES"][x["curve_id"]]
    assert mv[got["BUCKETS"].index(gone)] is None
    assert mv[got["BUCKETS"].index(zero)] == 0 and mv[got["BUCKETS"].index(zero)] is not None
    assert got["FX"]["CHF"] == 95.5 and got["FX_MOVE_PCT"]["CHF"] is None  # the extra.fx_rate branch
    assert got["CURVES"]["GBP"]["pts"] == [None] * nb and got["CURVE_MOVES"]["GBP"] == [None] * nb


def test_parity_on_the_inbox_runs_with_funding(store_url, tmp_path):
    """test_funding_run's book from the inbox (books, entities, funding rules, cash, an HTM lot, OTC rows, the
    report's portfolio): funding computed, a run without a previous market and one with."""
    ctx = prepare(SnapshotDir(SNAPS), DATE.isoformat())
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        rid1, rid2, _ = _runs(con, ctx)
    c = TestClient(create_app(store_url))
    first = assert_parity(c, rid1)
    assert first["prevDate"] is None and first["pnlAvailable"] is False and first["pnlReasonText"]
    got = assert_parity(c, rid2)
    assert got["fundingStatus"]["state"] == "computed" and got["BOOKS"]
    assert any(p["pnl"]["funding"] is not None for p in got["positions"])


def test_parity_on_the_live_golden_pair(store_url):
    """The hub's eod_final of 19.10 and 20.10.2026 (test_golden_live_2026_10_20): floaters, OFZ, FX bonds with their
    PnL at the market rate and the CBR rate beside it."""
    snaps = SnapshotDir(LIVE)
    bonds = json.loads((LIVE / "golden_index.json").read_text())["bonds"]
    prev, ctx = prepare(snaps, "2026-10-19"), prepare(snaps, "2026-10-20")
    req = [(s, 1000.0) for s in bonds]
    old, cur = value(prev, req), value(ctx, req)
    attach_pnl(prev, old, ctx, cur)
    def rows(outcomes):
        return [(f"P{i:02d}", "OFZ" if o.secid.startswith("SU") else "CORP", o, 1000.0)
                for i, o in enumerate(outcomes)]

    rid0 = insert_run(store_url, prev, rows(old), None)
    rid = insert_run(store_url, ctx, rows(cur), MarketView(prev.market.snapshot))
    c = TestClient(create_app(store_url))
    got = assert_parity(c, rid)
    assert {p["ccy"] for p in got["positions"]} > {"RUB"} and set(got["FX_CBR"]) > {"RUB"}
    assert assert_parity(c, rid0)["prevDate"] is None


def test_empty_state_demo_cache_and_warming(store_url, book, client):  # noqa: F811
    r = client.get("/concepts-run.js")
    assert r.status_code == 200 and "window.CONCEPTS_RUN =" not in r.text
    assert 'window.CONCEPTS_RUN_EMPTY = {"reason": "no_runs", "message": "нет опубликованных прогонов"};' in r.text
    assert '"reason": "not_found"' in client.get("/concepts-run.js?run_id=5").text
    assert "CONCEPTS_RUN" not in client.get("/concepts-run.js?demo=1").text
    e = client.get("/api/v1/concepts/run").json()
    assert e["code"] == "not_found" and e["details"] == {"reason": "no_runs"}
    assert client.get("/api/v1/concepts/run?run_id=5").json()["details"] == {"reason": "not_found"}
    assert client.get("/api/v1/concepts/run?run_id=x").status_code == 422

    rid = run(store_url, book)
    app = create_app(store_url)
    assert {"run", "run-js"} <= set(warm_bodies(app))  # built before the first page load, as /desk-data.js
    assert warm_bodies(app) == []
    c = TestClient(app)
    plain, gz = c.get("/concepts-run.js"), c.get("/concepts-run.js", headers={"Accept-Encoding": "gzip"})
    assert plain.headers["cache-control"] == "no-store" and gz.headers.get("content-encoding") == "gzip"
    assert plain.text == gz.text and f'"runId":{rid}' in plain.text
    j = c.get("/api/v1/concepts/run")
    assert j.status_code == 200 and c.get("/api/v1/concepts/run",
                                          headers={"If-None-Match": j.headers["ETag"]}).status_code == 304


def test_cbr_schedule_is_compats_cbr_meetings(store_url, tmp_path):
    """``concepts.cbr``'s ``schedule``: every meeting that takes effect after the run date, with its effective date
    and whether the date is estimated, as window.DESK's ``CBR_MEETINGS`` (the key-rate path's)."""
    ctx = prepare(SnapshotDir(SNAPS), DATE.isoformat())
    with store.connect(store_url) as con:
        _scan(con, tmp_path)
        _, rid, _ = _runs(con, ctx)
        static_facts.load_cbr(con, ctx.market.snapshot)
    c = TestClient(create_app(store_url))
    want = c.get(f"/api/v1/compat/desk?run_id={rid}").json()["CBR_MEETINGS"]
    with psycopg.connect(store_url, row_factory=dict_row) as con:
        got = cbr(con, DATE)
    assert want and got["schedule"] == want
    assert all(set(m) == {"date", "effective", "estimated"} for m in got["schedule"])
