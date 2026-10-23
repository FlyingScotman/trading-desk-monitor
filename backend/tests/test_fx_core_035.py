"""Core 0.35 FX inputs and FX derivatives from trade terms (WP-M1..M4, M6; core BACKLOG #132, #137).

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``, vendored from the hub repository:
CETS swap points of CNY, FORTS futures, settlement dates and calendars) and synthetic trades (invented amounts):

- calendars past the hub's ``valid_to`` are extended by their own rules, never Russian holidays;
- ``curves.fx.with_fx``: spots with the hub's value dates on joint calendars, the CNY and USD FX-implied curves
  (swap points, futures; USD's TOD/TOM anchor assumed), the CNY XCCY curve, CBR fixings;
- ``valuation.derivatives``: deliverable forwards, an NDF before and after its fixing, a swap, a cross, an inverted
  pair through core ``value_portfolio`` v3, equal to core's own engine on the same inputs; what core cannot value keeps
  the report's rule with the reason;
- ``positions.plan`` links the rows to their trades; ``tdm_run.direct_row`` publishes risk on both curves, the FX
  delta per currency, the legs, the OCP amounts; the Taylor payload reads a rate per currency.
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from rub_fixed_income import (
    RISK_BUCKETS,
    FxForward,
    FxForwardLeg,
    FxForwardRiskEngine,
    FxForwardRiskRequest,
    ShockedCurveCache,
)

from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata import adapter
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.scenario.taylor import risk_payload
from desk.store import PositionInputs
from desk.valuation import derivatives as dv
from desk.valuation import positions as pv1

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1_4"
DATE = "2026-10-15"
BD = date(2026, 10, 15)


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), DATE)


def terms(trade_id, base, quote, side, nb, nq, vd, **kw):
    """A trade_terms row as the store gives it (amounts as decimal strings, dates ISO)."""
    return {"trade_id": trade_id, "book_id": "FXB", "product_kind": kw.pop("product_kind", "fx_forward"),
            "trade_date": "2026-10-01", "value_date": vd, "base_ccy": base, "quote_ccy": quote, "side": side,
            "notional_base": str(nb), "notional_quote": str(nq), **{k: str(v) if isinstance(v, float) else v
                                                                  for k, v in kw.items()}}


# --- calendars ------------------------------------------------------------------------------------------------


def test_foreign_calendars_are_not_extended_with_russian_holidays(ctx):
    cn = adapter.core_calendar(ctx.static.calendars["CN_INTERBANK"])[0]
    assert cn.valid_to >= date(2037, 12, 31) and "rules" in cn.version
    assert cn.is_business_day(date(2027, 2, 23)) and cn.is_business_day(date(2027, 11, 4))  # Russian holidays
    assert not cn.is_business_day(date(2027, 10, 1))  # its own fixed day
    us = adapter.core_calendar(ctx.static.calendars["US_FED"])[0]
    assert not us.is_business_day(date(2028, 11, 23)) and not us.is_business_day(date(2028, 7, 4))
    assert us.is_business_day(date(2028, 6, 12))  # Russia Day
    ch = adapter.core_calendar(ctx.static.calendars["CH_ZURICH"])[0]
    assert not ch.is_business_day(date(2028, 8, 1)) and ch.is_business_day(date(2028, 6, 12))
    ru = adapter.core_calendar(ctx.static.calendars["MOEX_FX"])[0]
    assert not ru.is_business_day(date(2028, 6, 12))  # a Russian calendar keeps its rules


# --- FX inputs ------------------------------------------------------------------------------------------------


def test_fx_inputs_from_the_14_snapshot(ctx):
    fx = ctx.fx
    cny = fx.spots["CNY"]
    assert (cny.value_date, cny.calendar_id) == (date(2026, 10, 16), "FX:CNY")  # the hub's TOM settle_date
    assert fx.rates["CNY"] == (12.5141, "MOEX 19:00")
    lin = fx.lineage["curves"]
    assert lin["CNY FX"]["swaps"] == ["CNYRUBTODTOM:TOD/TOM", "CNYRUB_TOM1D:TOM/SPT"]
    assert lin["CNY FX"]["futures"] == ["CRZ6"] and "anchor" not in lin["CNY FX"]
    assert lin["USD FX"]["futures"] == ["SiZ6"] and lin["USD FX"]["anchor"].startswith("assumed")
    assert lin["CNY XCCY"]["status"] == "ok" and lin["EUR FX"]["status"] == "missing"
    # the implied curve reprices the future: the outright at its delivery date is its settlement price
    c = fx.curves["CNY FX"].curve
    rub = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    d = date(2026, 12, 17)
    fwd = cny.rub_per_unit * (c.discount_factor(d) / c.discount_factor(cny.value_date)) / (
        rub.discount_factor(d) / rub.discount_factor(cny.value_date))
    assert fwd == pytest.approx(12.746, abs=1e-9)
    assert {f.fixing_date for f in ctx.core_market.fx_fixings if f.currency == "USD"} == {
        date(2026, 10, 14), BD}
    assert {e.spec_id for e in ctx.core_market.curves if e.curve_id in ("CNY FX", "CNY XCCY")} == {
        "cny_fx_implied_v1", "cny_ruonia_xccy_v1"}


def test_rub_books_keep_their_core_inputs(ctx):
    """A RUB-only book's core call has no FX inputs: its result bytes are those before 0.35 (v2, no fx_rates)."""
    from desk.orchestrator import value

    outs = value(ctx, [("SU26238RMFS4", 10.0)], fallback=False)
    r = outs[0].core_result
    assert r.schema_version.endswith(".v2") and r.fx_rates == ()


# --- FX derivatives from terms --------------------------------------------------------------------------------

BOOK = [
    terms("T1", "CNY", "RUB", "buy", 10e6, 127.46e6, "2026-12-17", contract_rate=12.746),  # at the future's price
    terms("T2", "USD", "RUB", "sell", 1e6, 86e6, "2027-01-20", ndf=True, settle_ccy="RUB", fixing_date="2027-01-18",
          fixing_source="ЦБ"),
    terms("T3", "CNY", "RUB", "buy", 50e6, 625e6, "2026-10-16", product_kind="fx_swap", leg_id="near"),
    terms("T3", "CNY", "RUB", "sell", 50e6, 630e6, "2027-01-19", product_kind="fx_swap", leg_id="far"),
    terms("T4", "USD", "CNY", "buy", 2e6, 14e6, "2026-12-17"),
    terms("T5", "RUB", "CNY", "buy", 125e6, 10e6, "2026-12-17"),
    terms("T6", "USD", "RUB", "buy", 1e6, 83e6, "2026-10-16", ndf=True, fixing_date="2026-10-14", fixing_source="CBR"),
    terms("T7", "USD", "RUB", "buy", 1e6, 83e6, "2026-10-10"),
    terms("T8", "EUR", "RUB", "buy", 1e6, 95e6, "2027-01-20"),
]


def requests(rows=BOOK):
    out = {}
    for r in rows:
        out.setdefault(r["trade_id"], []).append(dv.TermsLeg.of(r))
    return [dv.Request(f"P-{t}", "FXB", {"ccy": legs[0].base if legs[0].base != "RUB" else legs[0].quote},
                       legs) for t, legs in out.items()]


@pytest.fixture(scope="module")
def valued(ctx):
    return dv.value(ctx, requests(), options=fxcurves.FxOptions())


def test_book_through_core(valued):
    out, result = valued
    assert result.schema_version.endswith(".v3")
    ok = {k for k, v in out.items() if not isinstance(v, dv.NotValued)}
    assert ok == {"P-T1", "P-T2", "P-T3", "P-T4", "P-T5", "P-T6"}
    assert out["P-T7"].reason == "settled" and out["P-T8"].reason == "no_foreign_curve"
    t1 = out["P-T1"]
    assert abs(t1.pv_rub) < 1.0  # at the market outright: the implied curve reprices the future
    assert set(t1.curve_buckets) == {"RUB RUONIA", "CNY FX"} and t1.quality == "exact"
    assert 0.98 * 10e6 < t1.fx_delta["CNY"] < 10e6  # spot equivalent: the notional discounted on the CNY curve
    assert t1.extra["ocp_amount"] == {"CNY": 10e6} and t1.extra["fx_rates"] == {"CNY": 12.5141}
    leg = t1.extra["legs"][0]
    assert (leg["leg"], leg["buy_ccy"], leg["buy_amount"], leg["sell_ccy"], leg["settlement"]) == (
        "single", "CNY", 10e6, "RUB", "deliverable")
    assert leg["fwd_rate"] == pytest.approx(12.746, abs=1e-6)
    assert all(a.startswith(("IR|", "FX|")) and b.startswith(("IR|", "FX|")) for a, b, _ in t1.hessian)


def test_forward_equals_the_core_engine(ctx, valued):
    out, _ = valued
    curve = ctx.fx.curves["USD FX"].curve
    rub = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    spot = ctx.fx.spots["USD"]
    trade = FxForward("T2", "USD", 1.0, FxForwardLeg(date(2027, 1, 20), -1e6, 86.0, date(2027, 1, 20),
                                                       fixing_date=date(2027, 1, 18), fixing_source="CBR"))
    engine = FxForwardRiskEngine(FxForwardRiskRequest(trade, spot.rub_per_unit, spot.value_date, rub, curve, 0.0),
                                 ShockedCurveCache())
    t2 = out["P-T2"]
    assert t2.pv_rub == pytest.approx(engine.base_pv_rub, rel=1e-12)
    assert t2.fx_delta["USD"] == pytest.approx(engine.risk(difference="central").fx_delta_foreign, rel=1e-9)
    assert t2.extra["fixings"][0]["status"] == "open" and t2.extra["fixings"][0]["source"] == "ЦБ"
    assert t2.extra["legs"][0]["settlement"] == "cash" and t2.extra["legs"][0]["fixing_date"] == "2027-01-18"
    assert t2.quality == "assumed" and any("anchor" not in a and "USD FX" in a for a in t2.assumptions)


def test_ndf_after_its_fixing_is_frozen(valued):
    t6 = valued[0]["P-T6"]
    assert t6.extra["fixings"][0]["status"] == "fixed" and t6.extra["fixings"][0]["fixing_rate"] == 83.9497
    assert not t6.fx_delta  # RUB risk only
    assert set(t6.curve_buckets) == {"RUB RUONIA"} and t6.quality == "exact"  # no USD curve risk, no USD note
    assert t6.pv_rub == pytest.approx(1e6 * (83.9497 - 83) * t6.extra["legs"][0]["df"], rel=1e-12)


def test_swap_cross_and_inverted_pair(valued):
    out, _ = valued
    t3 = out["P-T3"]
    assert [x["leg"] for x in t3.extra["legs"]] == ["near", "far"] and "fx_swap" in t3.extra["core"]["product"]
    assert abs(t3.fx_delta["CNY"]) < 0.05 * 50e6  # the legs offset
    assert t3.extra["ocp_amount"] == {}  # +50m and -50m CNY
    t4 = out["P-T4"]
    assert set(t4.fx_delta) == {"USD", "CNY"} and t4.fx_delta["USD"] > 0 > t4.fx_delta["CNY"]
    assert {"USD FX", "CNY FX", "RUB RUONIA"} >= set(t4.curve_buckets) >= {"USD FX", "CNY FX"}
    assert t4.extra["core"]["product"] == "fx_cross"
    t5 = out["P-T5"]  # buys RUB for CNY: short CNY
    assert t5.fx_delta["CNY"] < 0 and t5.extra["ocp_amount"] == {"CNY": -10e6}


def test_xccy_choice_and_fallback(ctx):
    rows = [BOOK[0]]
    xccy = fxcurves.FxOptions(foreign_curve="xccy")
    out, _ = dv.value(ctx, requests(rows), options=xccy)
    assert set(out["P-T1"].curve_buckets) == {"RUB RUONIA", "CNY XCCY"}
    # the implied curve absent: the XCCY one replaces it, said in the assumptions
    no_fx = replace(ctx, core_market=replace(ctx.core_market, curves=tuple(
        c for c in ctx.core_market.curves if c.curve_id != "CNY FX")))
    out, _ = dv.value(no_fx, requests(rows), options=fxcurves.FxOptions())
    v = out["P-T1"]
    assert set(v.curve_buckets) == {"RUB RUONIA", "CNY XCCY"} and v.quality == "assumed"
    assert v.extra["core"]["curve_fallback_from"] == ["CNY FX"]


# --- the plan and the published row ---------------------------------------------------------------------------


def inputs(rows, extra, trade_terms):
    return PositionInputs([(pid, "FXB", None, None) for pid in rows], [], {"FXB": BD}, datetime.now(UTC), "0" * 64,
                          extra, trade_terms=trade_terms)


def test_plan_links_rows_to_their_trades_and_flags_the_rest(ctx):
    extra = {
        "FXB|T1": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 10e6, "value_date": date(2026, 12, 17),
                   "deal_id": "T1", "mv_rub": 1000.0},
        "FXB|T8": {"asset_class": "fx", "ccy": "EUR", "amount_ccy": 1e6, "value_date": date(2027, 1, 20),
                   "deal_id": "T8", "mv_rub": 5000.0},
        "FXB|NOTERMS": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e6, "value_date": date(2027, 1, 20)},
    }
    inp = inputs(list(extra), extra, [BOOK[0], BOOK[-1], BOOK[1]])
    bp = pv1.plan(ctx, inp)
    t1, t8, nt = (bp.direct[k] for k in extra)
    assert t1.valuation.extra["core"]["trades"] == ["T1"] and t1.core_result is not None
    assert t1.valuation.extra["reported_pv_rub"] == 1000.0
    assert t8.valuation.quality == "reported_value" and t8.reason == "no_foreign_curve"
    assert t8.valuation.assumptions[0].startswith("trade terms not valued by core (no_foreign_curve")
    assert nt.reason == pv1.NO_TERMS and nt.valuation.quality == "assumed"
    assert bp.unlinked_terms == ["FXB|T2"]
    added = pv1.plan(replace(ctx, fx_options=fxcurves.FxOptions(unlinked_terms=True)), inp)
    assert added.added == ["FXB|T2"] and added.direct["FXB|T2"].valuation.extra["core"]["product"] == "fx_ndf"

    row = tdm_run.direct_row(ctx, "FXB", "FXB|T1", t1, extra["FXB|T1"], None)
    assert row["instr_type"] == "FX" and row["extra"]["valuation"] == "computed"
    assert set(row["risk"]) == {"RUB RUONIA", "CNY FX"} and row["fx_delta"] == {"CNY": t1.valuation.fx_delta["CNY"]}
    assert row["gamma"] and row["extra"]["legs"][0]["value_date"] == "2026-12-17"
    old = tdm_run.direct_row(ctx, "FXB", "FXB|NOTERMS", nt, extra["FXB|NOTERMS"], None)
    assert old["fx_delta"] == {"USD": 2e6} and old["extra"]["fx_delta_basis"].startswith("notional")
    # the Taylor payload: FX per 1 % at each currency's own rate (a cross has two)
    cross = pv1.plan(ctx, inputs(["X"], {"X": {"asset_class": "fx", "ccy": "USD", "deal_id": "T4"}}, [BOOK[4]]))
    xr = tdm_run.direct_row(ctx, "FXB", "X", cross.direct["X"], {"asset_class": "fx", "ccy": "USD"}, None)
    pay = risk_payload({"run_id": 1, "business_date": BD}, [{"position_id": "X", "book_id": "FXB", "secid": None,
                                                            "quality": "exact", "pv_rub": xr["mv_rub"], "fact": xr}],
                       {"curves": []})
    f = dict(zip(pay["factors"], [0.0] * len(pay["factors"])))
    for i, x in pay["positions"][0]["delta"]:
        f[pay["factors"][i]] = x
    rates = ctx.fx.rates
    assert f["FX|CNY"] == pytest.approx(xr["fx_delta"]["CNY"] * rates["CNY"][0] / 100)
    assert f["FX|USD"] == pytest.approx(xr["fx_delta"]["USD"] * rates["USD"][0] / 100)


def test_fx_future_by_ticker_behind_its_setting(ctx):
    extra = {"F1": {"asset_class": "other", "source_instrument_id": "SiZ6"}}
    inp = PositionInputs([("F1", "FUT", None, 5.0)], [], {"FUT": BD}, datetime.now(UTC), "0" * 64, extra)
    off = pv1.plan(ctx, inp).direct["F1"]
    assert off.valuation is None and off.reason == "no_reported_value"  # as before: no mv_rub, not valued
    on = pv1.plan(replace(ctx, fx_options=fxcurves.FxOptions(futures_by_ticker=True)), inp).direct["F1"].valuation
    # margined daily: the delta is N x F / S, not discounted
    assert on.extra["core"]["product"] == "fx_future" and abs(on.pv_rub) < 1e-6  # margined at today's settlement
    assert 5000 < on.fx_delta["USD"] < 5000 * 85.219 / 83.76 + 1 and on.extra["ocp_amount"] == {"USD": 5000.0}
    assert set(on.curve_buckets) == {"RUB RUONIA", "USD FX"}


def test_publish_fx_book_through_the_store_and_the_api(ctx, store_url):
    from fastapi.testclient import TestClient

    from desk import store
    from desk.api import create_app
    from desk.publish import desk_run

    extra = {"FXB|T1": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 10e6, "deal_id": "T1"},
             "FXB|T4": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e6, "deal_id": "T4"},
             "FXB|T2": {"asset_class": "otc", "ccy": "USD", "source_instrument_id": "T2", "deal_id": "T2"}}
    inp = inputs(list(extra), extra, [BOOK[0], BOOK[4], BOOK[1]])
    bp = pv1.plan(ctx, inp)
    positions = pv1.merge(bp, [])
    with store.connect(store_url) as con:
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp)
        (n,) = con.execute("SELECT count(*) FROM run_position WHERE run_id = %s AND quality IS NOT NULL",
                           (rid,)).fetchone()
    assert status == "complete" and n == 3
    c = TestClient(create_app(store_url))
    got = {p["position_id"]: p for p in c.get(f"/api/v1/runs/{rid}/positions").json()["positions"]}
    t4 = got["FXB|T4"]["fact"]
    assert set(t4["fx_delta"]) == {"USD", "CNY"} and t4["instr_type"] == "FX"
    assert got["FXB|T2"]["fact"]["instr_type"] == "FX" and got["FXB|T2"]["fact"]["extra"]["core"]["product"] == "fx_ndf"
    assert c.get(f"/api/v1/runs/{rid}/risk").status_code == 200
    desk = c.get(f"/api/v1/compat/desk?run_id={rid}").json()
    assert desk["CURVES"]["CNY FX"]["ccy"] == "CNY" and desk["CURVES"]["USD FX"]["ccy"] == "USD"
    dp = {p["id"]: p for p in desk["positions"]}
    assert set(dp["FXB|T4"]["fxDelta"]) == {"USD", "CNY"} and dp["FXB|T1"]["extra"]["legs"][0]["buy_ccy"] == "CNY"

    # T2 (M5): the FX derivatives repriced exactly from their published legs, curves by name, FX and the basis
    from psycopg.rows import dict_row

    from desk.scenario.exact import reprice_run

    with store.connect(store_url) as conn:
        con = conn.cursor(row_factory=dict_row)
        run_row = con.execute("SELECT run_id, business_date, cut_type, market_snapshot_id FROM run WHERE run_id = %s",
                              (rid,)).fetchone()
        prow = con.execute("SELECT position_id, book_id, secid, quantity, quality, pv_rub, fact FROM run_position"
                           " WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
        ref = {r["kind"]: r["data"] for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s",
                                                         (rid,))}
    small = reprice_run(SnapshotDir(FIX), run_row, prow, ref, {"curves": {"CNY FX": 5, "RUB RUONIA": 5},
                                                               "fx": {"USD": 1, "CNY": 1}})
    res = {p["position_id"]: p for p in small["positions"]}
    assert small["coverage"]["status"] == "complete", small["coverage"]
    for pid in extra:
        r = res[pid]
        assert abs(r["residual"]) < 0.02 * max(1.0, abs(r["exact"])), (pid, r)  # Taylor to second order
    basis = {p["position_id"]: p for p in reprice_run(SnapshotDir(FIX), run_row, prow, ref,
                                                       {"basis": {"CNY": 25}})["positions"]}
    assert basis["FXB|T1"]["exact"] == pytest.approx(25 * t1_basis(got), rel=0.05)  # basis01 x 25 bp
    assert basis["FXB|T1"]["taylor"]["total"] == 0.0 and basis["FXB|T2"]["exact"] == 0.0  # USD: no CNY basis


def test_derivatives_parallel_and_v3_next_to_v2(ctx, store_url):
    """TDM#49: the derivatives' v3 result is stored with the run (also when no outcome carries it) and reads back
    verified; the run's block lists each derivative computed by core next to the report's PV."""
    from fastapi.testclient import TestClient

    from desk import store
    from desk.api import create_app
    from desk.publish import derivatives_parallel, desk_run

    extra = {"FXB|T1": {"asset_class": "fx", "ccy": "CNY", "amount_ccy": 10e6, "deal_id": "T1", "mv_rub": "1000"},
             "FXB|T8": {"asset_class": "fx", "ccy": "EUR", "amount_ccy": 1e6, "value_date": date(2027, 1, 20),
                        "deal_id": "T8", "mv_rub": 5000.0},
             "FXB|IRS": {"asset_class": "otc", "ccy": "RUB", "source_instrument_id": "IRS", "mv_rub": -20.0},
             "FXB|NOTERMS": {"asset_class": "fx", "ccy": "USD", "amount_ccy": 2e6, "value_date": date(2027, 1, 20)}}
    inp = inputs(list(extra), extra, [BOOK[0], BOOK[-1]])
    bp = pv1.plan(ctx, inp)
    positions = pv1.merge(bp, [])
    block = derivatives_parallel.compare(positions, extra)
    rows = {r["position_id"]: r for r in block["rows"]}
    t1 = rows["FXB|T1"]
    assert t1["status"] == "both" and t1["used"] == "computed" and t1["imported_pv_rub"] == 1000.0
    assert t1["diff_rub"] == pytest.approx(t1["computed_pv_rub"] - 1000.0) and t1["product"] == "fx_forward"
    assert (rows["FXB|T8"]["status"], rows["FXB|T8"]["reason"], rows["FXB|T8"]["used"]) == (
        "imported_only", "no_foreign_curve", "imported")
    assert (rows["FXB|IRS"]["status"], rows["FXB|IRS"]["reason"]) == ("imported_only", pv1.NO_TERMS)
    assert (rows["FXB|NOTERMS"]["status"], rows["FXB|NOTERMS"]["reason"]) == ("neither", pv1.NO_TERMS)
    t = block["totals"]
    assert (t["n"], t["both"], t["imported_only"], t["neither"]) == (4, 1, 2, 1)
    assert t["abs_diff_rub"] == pytest.approx(abs(t1["diff_rub"])) and t["n_over"] == int(t1["over"])
    assert block["by_book"]["FXB"]["n"] == 4 and block["reasons"] == {pv1.NO_TERMS: 2, "no_foreign_curve": 1}
    level, text = derivatives_parallel.log_line(block)
    assert "обе оценки у 1" in text and "только отчёт 2" in text and level in ("INFO", "WARN")
    assert derivatives_parallel.log_line(derivatives_parallel.compare([], {})) is None

    for _, _, o in positions:  # the v3 result is the plan's, stored even when no outcome carries it
        o.core_result = None
    with store.connect(store_url) as con:
        rid, status, _ = desk_run.publish(con, ctx, positions, prev=None, inputs=inp, core_results=[bp.core_result])
        stored = desk_run.read_core_results(con, rid)
        logs = [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s", (rid,))]
    assert status == "complete" and [(label, schema) for label, schema, _ in stored] == [
        ("derivatives", "rub-portfolio-risk-result.v3")]
    assert stored[0][2].result_sha256 == bp.core_result.result_sha256
    assert any(m.startswith("деривативы, ядро и отчёт параллельно") for m in logs)
    c = TestClient(create_app(store_url))
    got = c.get(f"/api/v1/runs/{rid}/derivatives").json()
    assert got["state"] == "computed" and got["totals"]["both"] == 1 and len(got["rows"]) == 4
    assert got["rows"][0]["position_id"] in extra and got["tolerance"]["provisional"] is True
    assert c.get("/api/v1/runs/999999/derivatives").status_code == 404


def t1_basis(got):
    return got["FXB|T1"]["fact"]["extra"]["basis01"]["CNY"]


def test_curves_reference_and_fx_log(ctx, valued):
    out, result = valued
    from desk.orchestrator import Outcome

    rows, positions = [], []
    for pid, v in out.items():
        if isinstance(v, dv.NotValued):
            continue
        o = Outcome(None, None, None, valuation=v)
        o.core_result = result
        positions.append((pid, "FXB", o))
        rows.append(tdm_run.direct_row(ctx, "FXB", pid, o, {"asset_class": "fx", "ccy": "CNY"}, None))
    ref = {c["curve_id"]: c for c in tdm_run.curves_reference(ctx, None, rows, [])}
    assert ref["CNY FX"]["ccy"] == "CNY" and len(ref["CNY FX"]["points"]) == len(RISK_BUCKETS)
    assert ref["USD FX"]["ccy"] == "USD" and ref["CNY XCCY"]["kind"] == "swap"
    ((level, line),) = tdm_run.fx_log(ctx, positions)
    assert level == "INFO"
    assert line.startswith("FX-книга: ядром: ") and "fx_cross 1" in line and "fx_ndf 2" in line
    assert math.isfinite(sum(r["mv_rub"] for r in rows))


# --- foreign bonds on their own curve (core 0.36, BACKLOG #132) --------------------------------------------------

USD_BOND, CNY_BOND = "RU000A10BRM5", "RU000A0CNY01"
UST = (("1M", 4.18), ("3M", 4.10), ("6M", 3.98), ("1Y", 3.81), ("2Y", 3.65), ("5Y", 3.68), ("10Y", 4.01))


def synthetic_dir(tmp: Path) -> SnapshotDir:
    """The golden pair with a synthetic CNY copy of the USD-face bond (amounts as in USD, invented) and a UST par
    set (percent, as the hub publishes), re-hashed into a cache folder."""
    import copy
    import gzip
    import hashlib
    import json

    sd = SnapshotDir(FIX)
    market = sd.market(DATE, "eod_final")
    static = copy.deepcopy(sd.get(market.static_snapshot_id).doc)
    m = copy.deepcopy(market.doc)
    d = static["data"]
    inst = next(i for i in d["instruments"] if i["secid"] == USD_BOND)
    d["instruments"].append({**inst, "secid": CNY_BOND, "isin": CNY_BOND, "face_unit": "CNY",
                             "regnumber": "4B02-99-99999-A-001P", "name": "Синтетика CNY"})
    for k in ("coupon_periods", "redemptions", "offers"):
        d[k] += [{**r, "secid": CNY_BOND, **({"face_unit": "CNY"} if "face_unit" in r else {})}
                 for r in d[k] if r["secid"] == USD_BOND]
    q = next(x for x in m["data"]["bond_quotes"] if x["secid"] == USD_BOND and x["board"] == "TQCB")
    m["data"]["bond_quotes"].append({**q, "secid": CNY_BOND, "accrued_per_bond": "42.548"})  # 3.4 CNY at 12.5141
    qs = copy.deepcopy(m["data"]["curve_quote_sets"][0])
    qs.update(dataset="usd_ust_par", source="us_treasury", quote_set_id=990001, labels=["mid_only", "proxy_ust_par"],
              quotes=[{"tenor": t, "position": i, "mid_pct": str(v), "bid_pct": None, "ask_pct": None,
                       "tenor_days": None, "tenor_months": None, "pillar_date": None, "extra": None}
                      for i, (t, v) in enumerate(UST)])
    m["data"]["curve_quote_sets"].append(qs)

    def put(doc):
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        sid = hashlib.sha256(raw).hexdigest()
        (tmp / f"{sid}.json.gz").write_bytes(gzip.compress(raw))
        return sid

    m["static_snapshot_id"] = put(static)
    put(m)
    return SnapshotDir(tmp)


def test_foreign_bonds_on_their_own_curve(tmp_path):
    from desk.orchestrator import value

    sd = synthetic_dir(tmp_path)
    ctx = prepare(sd, DATE)
    assert ctx.fx.lineage["curves"]["USD UST"]["proxy"] is True
    off = {o.secid: o for o in value(ctx, [(USD_BOND, 10.0), (CNY_BOND, 10.0)])}
    cny, usd = off[CNY_BOND].valuation, off[USD_BOND].valuation
    assert cny.discount_curve_id == "CNY XCCY" and cny.quality == "exact" and set(cny.risk()) == {"CNY XCCY"}
    assert cny.foreign and cny.spread_curve == "CNY XCCY" and cny.accrued_rub == pytest.approx(42.548, abs=0.1)
    assert all(f.startswith(("IR|CNY XCCY|", "CS", "FX|")) for f, _, _ in cny.hessian)
    # USD: the proxy only with proxy_curves_for_risk; without it the fallback ladder as before
    assert off[USD_BOND].reason == "no_foreign_curve" and usd.quality == "market_value"
    on = {o.secid: o for o in value(replace(ctx, proxy_curves=True), [(USD_BOND, 10.0)])}
    usd = on[USD_BOND].valuation
    assert usd.discount_curve_id == "USD UST" and usd.quality == "assumed"
    assert any("par-yield proxy, not OIS" in a for a in usd.assumptions)
    pos = [(USD_BOND, "B", on[USD_BOND]), (CNY_BOND, "B", off[CNY_BOND])]
    rows = [tdm_run.position_row(replace(ctx, proxy_curves=True), "B", s, o, "x") for s, _, o in pos]
    ref = {c["curve_id"]: c for c in tdm_run.curves_reference(replace(ctx, proxy_curves=True), None, rows, [])}
    assert ref["USD UST"]["ccy"] == "USD" and ref["USD UST"]["kind"] == "bond" and ref["CNY XCCY"]["ccy"] == "CNY"
    assert "валютные облигации: CNY XCCY exact 1, USD UST assumed 1" in tdm_run.fx_log(ctx, pos)[0][1]
