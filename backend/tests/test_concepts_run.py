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
    r = c.get(f"/api/v1/concepts/run?run={rid}")
    assert r.status_code == 200, r.text
    got = r.json()
    assert set(got) == TOP and set(got["source"]) <= SOURCE
    assert all(set(p) <= POSITION and set(p["pnl"]) == PNL for p in got["positions"])
    for k in TOP:
        assert got[k] == want[k], k
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
    assert client.get("/api/v1/concepts/run?run=5").json()["details"] == {"reason": "not_found"}
    assert client.get("/api/v1/concepts/run?run=x").status_code == 422

    rid = run(store_url, book)
    app = create_app(store_url)
    assert "run" in warm_bodies(app)  # built before the first page load, as /desk-data.js
    assert "run" not in warm_bodies(app)
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
