"""R10: a missing PnL is said, not shown as 0; FX rows and the FX part of a foreign bond get a PnL from the rate
move; report-valued rows from the reports of both dates. On the golden fixtures with a synthetic previous date
whose USD rate is moved. Synthetic rows only."""

from __future__ import annotations

import copy
import dataclasses
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from store_rows import insert_run
from test_slice_s2 import shifted_prev

from desk import store
from desk.api import compat, create_app
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare, value
from desk.pnl import direct
from desk.pnl.reasons import FX_METHOD, NO_PNL_TEXT, REPORTED_METHOD
from desk.publish import desk_run, tdm_run
from desk.store import PositionInputs
from desk.valuation.fallback import FallbackContext, approximate
from desk.valuation.positions import merge, plan

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
PREV = date(2026, 10, 14)
USD_PREV = 80.0
USD_PREV_MKT = 80.4  # the MOEX 19:00 rate of the previous date (R26: the market rate the flash PnL is in)
USD_BOND = "RU000A10BRM5"
Q = 1000.0


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def prev(ctx):
    """The fixture market one day earlier with USD at 80 (CBR) and 80.4 (MOEX 19:00), no MOEX TOM; the KBD shifted as
    in S2."""
    p = shifted_prev(ctx, PREV)
    m = copy.copy(ctx.market)
    m.business_date = PREV
    m.fx = lambda ccy: ({"cbr": USD_PREV, "cbr_next": None, "moex_tom": None, "moex_19": USD_PREV_MKT} if ccy == "USD"
                        else {"cbr": None, "cbr_next": None, "moex_tom": None})
    return dataclasses.replace(p, market=m)


def inputs(rows, extra, valid: date | str = DATE) -> PositionInputs:
    d = date.fromisoformat(valid) if isinstance(valid, str) else valid
    return PositionInputs(rows, [], {b: d for _, b, *_ in rows}, datetime.now(UTC), "0" * 64, extra)


ROWS = [("FX1", "TSY", None, None), ("FX2", "TSY", None, None), ("FX3", "TSY", None, None),
        ("O1", "CORP", None, None), ("B2", "CORP", None, None), ("L1", "OFZ", "SU26238RMFS4", 10.0)]
EXTRA = {
    "FX1": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 1000.0},
    "FX2": {"asset_class": "fx", "ccy": "USD", "amount_ccy": -400.0, "value_date": date(2027, 1, 15)},
    "FX3": {"asset_class": "fx", "ccy": "RUB", "amount_ccy": 5e6},
    "O1": {"asset_class": "otc", "source_instrument_id": "IRS-1", "ccy": "RUB", "mv_rub": 5e5},
    "B2": {"asset_class": "bond", "ccy": "RUB", "source_instrument_id": "XS000", "mv_rub": 1e6},
}
PREV_EXTRA = {"O1": {**EXTRA["O1"], "mv_rub": 4.2e5}}  # B2 was not in yesterday's report


def test_split_fx_of_a_foreign_bond(ctx, prev):
    """A USD-face bond (fallback rung, simple PnL): the FX delta of D-1 x the market rate move (MOEX 19:00, R26) goes to
    fx_gross; the same delta x the CBR move is fx_gross_cbr (the official basis), beside it."""
    (o,) = value(ctx, [(USD_BOND, Q)])
    cur = approximate(ctx, o, Q, FallbackContext(ctx, history=[]))
    fx = ctx.market.fx("USD")
    mkt, cbr = fx["moex_19"], fx["cbr"]
    p_v = dataclasses.replace(cur, valuation_date=PREV, dirty_rub=cur.dirty_rub * USD_PREV_MKT / mkt - 3.0)
    c = Outcome(o.secid, o.terms, o.quote, valuation=cur)
    attach_pnl(prev, [Outcome(o.secid, o.terms, o.quote, valuation=p_v)], ctx, [c])
    pnl = c.pnl
    delta = Q * p_v.dirty_rub / USD_PREV_MKT
    assert pnl.fx_gross == pytest.approx(delta * (mkt - USD_PREV_MKT))
    assert pnl.fx_gross_cbr == pytest.approx(delta * (cbr - USD_PREV))
    # its carry at its yield (batch 1, item 5: ``fallback.yield_pnl``) is out of other too
    assert pnl.carry > 0 and pnl.extra()["method"] == "desk-flash-yield-v1"
    assert pnl.carry + pnl.other + pnl.fx_gross == pytest.approx(pnl.total) and pnl.explained == pytest.approx(pnl.total)
    t = pnl.as_tdm()
    assert t["fx_gross"] == pnl.fx_gross and t["fx_gross_cbr"] == pnl.fx_gross_cbr and FX_METHOD in pnl.extra()["note"]
    assert "MOEX 19:00" in pnl.extra()["note"] and "at CBR" in pnl.extra()["note"]
    # a RUB bond is left as is
    rub = next(x for x in value(ctx, [("RU000A0JXQ44", Q)]))
    v = approximate(ctx, rub, Q, FallbackContext(ctx, history=[]))
    c2 = Outcome(rub.secid, rub.terms, rub.quote, valuation=v)
    attach_pnl(prev, [Outcome(rub.secid, rub.terms, rub.quote,
                              valuation=dataclasses.replace(v, valuation_date=PREV, dirty_rub=v.dirty_rub - 1))],
               ctx, [c2])
    assert c2.pnl.fx_gross == 0.0 and c2.pnl.other == pytest.approx(c2.pnl.total)


def _direct(ctx, prev, valid: date | str = DATE, prev_valid: date = PREV):
    inp = inputs(ROWS, EXTRA, valid)
    bp = plan(ctx, inp)
    prev_extra = {k: v for k, v in {**EXTRA, **PREV_EXTRA}.items() if k != "B2"}
    prev_inp = inputs(ROWS, prev_extra, prev_valid)
    direct.attach(prev, ctx, bp.direct, inp, plan(prev, prev_inp).direct, prev_inp.valid_dates)
    return inp, bp


def test_fx_rows_by_the_rate_move_and_otc_by_the_reports(ctx, prev):
    _, bp = _direct(ctx, prev)
    d, fx = bp.direct, ctx.market.fx("USD")
    mkt, cbr = fx["moex_19"], fx["cbr"]
    fx1 = d["FX1"].pnl  # at the market rate (R26), the CBR move beside it
    assert (fx1.method, fx1.fx_gross, fx1.total, fx1.other) == (FX_METHOD, pytest.approx(1000 * (mkt - USD_PREV_MKT)),
                                                                pytest.approx(1000 * (mkt - USD_PREV_MKT)), 0.0)
    assert fx1.fx_gross_cbr == pytest.approx(1000 * (cbr - USD_PREV)) and fx1.as_tdm()["fx_gross_cbr"] == fx1.fx_gross_cbr
    assert d["FX2"].pnl.total == pytest.approx(-400 * (mkt - USD_PREV_MKT)) and d["FX2"].pnl.flag == direct.FORWARD_FLAG
    assert d["FX3"].pnl.total == 0.0 and d["FX3"].pnl.method == FX_METHOD  # RUB cash: no FX move
    o1 = d["O1"].pnl
    assert (o1.method, o1.other, o1.fx_gross, o1.flag) == (REPORTED_METHOD, pytest.approx(8e4), 0.0, "по отчёту")
    assert o1.as_tdm()["other"] == pytest.approx(8e4) and o1.extra()["method"] == REPORTED_METHOD
    assert d["B2"].pnl is None and d["B2"].no_pnl.reason == "no_prev_report"
    # D-1's own report (valid on D-1): the note says nothing of a gap, no report_from (TDM#440)
    assert "reports gap" not in o1.note and o1.report_from is None and "report_from" not in o1.extra()
    assert o1.note == "reported PV 500,000.00 - 420,000.00 RUB (trades of the day inside)"


def test_gap_between_reports_names_the_old_report(ctx, prev):
    """TDM#440: D-1 carried a report valid on 05.10, today's report is new: the whole gap between the reports lands on
    D (prev_date stays D-1), the note and extra.report_from name the old report's date."""
    old = date(2026, 10, 5)
    _, bp = _direct(ctx, prev, prev_valid=old)
    o1 = bp.direct["O1"].pnl
    assert (o1.method, o1.other, o1.prev_date, o1.flag) == (REPORTED_METHOD, pytest.approx(8e4), PREV, "по отчёту")
    assert o1.note.endswith("; reports gap since 05.10: the D-1 report is valid from 05.10.2026"), o1.note
    assert all(ord(ch) < 128 for ch in o1.note)  # one language: the tail is English like the rest (TDM#459)
    assert o1.report_from == old and o1.extra()["report_from"] == "2026-10-05"
    assert o1.extra()["prev_date"] == PREV.isoformat()
    # rows not valued by the report are not touched
    assert "report_from" not in bp.direct["FX1"].pnl.extra() and "reports gap" not in bp.direct["FX1"].pnl.note


def test_carried_report_and_missing_rate_give_reasons(ctx, prev):
    _, bp = _direct(ctx, prev, valid=PREV)  # today's report is yesterday's: a difference would be a false 0
    assert bp.direct["O1"].pnl is None and bp.direct["O1"].no_pnl.reason == "report_carried"
    noprev = dataclasses.replace(prev, market=copy.copy(prev.market))
    noprev.market.fx = lambda ccy: {"cbr": None, "cbr_next": None, "moex_tom": None}
    _, bp = _direct(ctx, noprev)
    assert bp.direct["FX1"].pnl is None and bp.direct["FX1"].no_pnl.reason == "no_prev_fx_rate"
    assert NO_PNL_TEXT["report_carried"] and NO_PNL_TEXT["no_prev_fx_rate"]


def test_published_pnl_methods_and_honest_absence(ctx, prev, store_url):
    inp, bp = _direct(ctx, prev)
    outs = value(ctx, bp.requests, position_ids=bp.position_ids)
    positions = merge(bp, outs)
    view = prev.market
    with store.connect(store_url) as con:
        rid, status, warnings = desk_run.publish(con, ctx, positions, prev=view, inputs=inp)
    assert status == "complete" and any("B2" in w and "no_prev_report" in w for w in warnings)
    c = TestClient(create_app(store_url))
    pnl = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/pnl").json()["positions"]}
    assert pnl["FX1"]["components"]["fx_gross"] == pytest.approx(pnl["FX1"]["total"])
    assert pnl["O1"]["extra"]["flag"] == "по отчёту" and pnl["B2"]["no_pnl"]["reason"] == "no_prev_report"
    tot = c.get(f"/api/v1/runs/{rid}").json()["totals"]  # the run totals: the components add up to the total
    for b, t in tot.items():
        if t["pnl"]["total"] is not None:
            parts = sum(t["pnl"][k] or 0.0 for k in ("carry", "rolldown", "rates", "spread", "other", "fx_gross"))
            assert parts == pytest.approx(t["pnl"]["total"]), b
    assert tot["_total"]["pnl"]["fx_gross"] == pytest.approx(pnl["FX1"]["total"] + sum(
        p["components"]["fx_gross"] or 0.0 for k, p in pnl.items() if k != "FX1" and p["components"]))
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["prevDate"] == PREV.isoformat() and desk["pnlAvailable"] and desk["pnlReason"] is None
    ps = {p["id"]: p for p in desk["positions"]}
    assert ps["O1"]["pnl"]["reported"] == pytest.approx(8e4) and ps["O1"]["pnl"]["other"] == 0.0
    assert ps["O1"]["pnl"]["explained"] == pytest.approx(8e4) and "по отчёту" in ps["O1"]["pnl"]["methodText"]
    assert ps["FX1"]["pnl"]["fx"] == pytest.approx(pnl["FX1"]["total"]) and ps["FX1"]["pnl"]["method"] == FX_METHOD
    b2 = ps["B2"]["pnl"]  # valued, but no PnL: null with its reason, never 0
    assert b2["available"] is False and b2["explained"] is None and b2["carry"] is None
    assert b2["reasonText"] == NO_PNL_TEXT["no_prev_report"]
    l1 = ps["L1"]["pnl"]  # the bond had no D-1 valuation in this test: not_priced_prev is not set, the run's reason
    assert l1["available"] is False and l1["reason"] == "not_computed"
    cov = desk["pnlCoverage"]
    assert cov == {"withPnl": sum(p["pnl"]["available"] for p in desk["positions"]), "positions": len(ROWS)}
    # TDM#106: no official PnL arrived (rows with one: test_official_label_106); the history's pnlOfficial is
    # explained plus the FX basis, flagged as such
    day = desk["history"][-1]
    assert day["pnlOfficial"] is not None and day["officialReceived"] is False and desk["official"]["received"] is False
    assert 0 < cov["withPnl"] < cov["positions"]  # partial: the page says «частично: N из M позиций»
    # the run log of both stores carries the INFO lines per method
    log = c.get(f"/api/v1/runs/{rid}").json()["log"]
    assert any("по курсу" in x["message"] and FX_METHOD in x["message"] for x in log)
    assert any("по отчёту" in x["message"] and REPORTED_METHOD in x["message"] for x in log)


def test_partial_official_per_book(ctx, prev, store_url):
    """TDM#489: the official PnL arrived for book TSY only (synthetic, set on the stored rows): the day is
    ``partial``, not official; CORP is named as the book without it, and the unexplained is over TSY only."""
    inp, bp = _direct(ctx, prev)
    positions = merge(bp, value(ctx, bp.requests, position_ids=bp.position_ids))
    with store.connect(store_url) as con:
        rid, _, _ = desk_run.publish(con, ctx, positions, prev=prev.market, inputs=inp)
        con.execute("ALTER TABLE run_position DISABLE TRIGGER USER")  # a published run is frozen
        con.execute("UPDATE run_position SET pnl = pnl || jsonb_build_object('official', 1000.0 + ord)"
                    " WHERE run_id = %s AND book_id = 'TSY' AND pnl IS NOT NULL", (rid,))
        con.execute("ALTER TABLE run_position ENABLE TRIGGER USER")
        rows = [{"fact": f, "pnl": q} for f, q in con.execute(
            "SELECT fact, pnl FROM run_position WHERE run_id = %s AND fact IS NOT NULL", (rid,)).fetchall()]
    tsy = [r for r in rows if r["fact"]["book_id"] == "TSY" and r["pnl"]]
    assert tsy and any(r["fact"]["book_id"] == "CORP" and r["pnl"] for r in rows)
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid}").json()
    day = desk["history"][-1]
    assert day["officialState"] == "partial" and day["officialReceived"] is False
    b = day["officialBooks"]
    assert (b["received"], b["books"], b["missing"]) == (1, 2, ["CORP"])
    assert b["pnlOfficial"] == pytest.approx(sum(r["pnl"]["official"] for r in tsy))
    assert b["pnlExplained"] == pytest.approx(compat.run_totals(tsy)[1])
    assert day["pnlOfficial"] == pytest.approx(compat.run_totals(rows)[0])  # the day's total: TSY official + CORP
    assert desk["official"]["state"] == "partial" and desk["official"]["missingBooks"] == ["CORP"]
    ps = [p for p in desk["positions"] if p["book"] == "CORP" and p["pnl"]["available"]]
    assert ps and all(p["pnl"]["official"] is None and p["pnl"]["unexplained"] == 0.0 for p in ps)


def test_run_without_previous_market(ctx, store_url):
    """No eod_final before the date: prevDate null, pnlAvailable false with its reason, every position's PnL null
    with that reason, history PnL null, and the WARN line in the run's own log."""
    secids = ["SU26238RMFS4", "RU000A0JXQ44"]
    outs = value(ctx, [(s, Q) for s in secids])
    positions = [(f"P{i}", "OFZ", o, Q) for i, o in enumerate(outs)]
    rid = insert_run(store_url, ctx, positions, None)
    c = TestClient(create_app(store_url))
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["prevDate"] is None and desk["pnlAvailable"] is False and desk["pnlReason"] == "no_prev_market"
    assert desk["pnlReasonText"] == "нет финального среза рынка за предыдущий рабочий день"
    assert desk["accrualDays"] >= 1  # the funding accrual keeps its weekday fallback
    for p in desk["positions"]:
        assert p["pnl"]["available"] is False and p["pnl"]["reason"] == "no_prev_market"
        assert all(p["pnl"][k] is None for k in ("explained", "carry", "rates", "other", "reported"))
    assert desk["history"][-1]["pnlOfficial"] is None and desk["history"][-1]["pnlExplained"] is None
    log = [x["message"] for x in c.get(f"/api/v1/runs/{rid}").json()["log"]]
    assert any(m.startswith("PnL не посчитан: нет финального среза рынка") for m in log)
    content = tdm_run.run_content(ctx, [(pid, b, o) for pid, b, o, _ in positions], None, {})
    assert ("WARN", next(m for m in log if m.startswith("PnL не посчитан"))) in content.log


def test_run_with_a_previous_market_but_no_pnl(ctx, prev, store_url):
    outs = value(ctx, [("SU26238RMFS4", Q)])
    rid = insert_run(store_url, ctx, [("P0", "OFZ", outs[0], Q)], prev.market)  # as desk eod --no-pnl
    desk = TestClient(create_app(store_url)).get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["prevDate"] == PREV.isoformat() and desk["pnlAvailable"] is False
    assert desk["pnlReason"] == "no_position_pnl" and desk["pnlCoverage"] == {"withPnl": 0, "positions": 1}
    assert desk["positions"][0]["pnl"]["reason"] == "not_computed"
    assert (date.fromisoformat(desk["asOf"]) - timedelta(1)).isoformat() == desk["prevDate"]
