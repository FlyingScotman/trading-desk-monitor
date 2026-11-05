"""TDM#565: a CCS whose CNY leg floats on RUSFARCNY (compounded) against a RUB leg fixed or on RUONIA.

On the hub's contract 1.4 golden snapshot of 15.10.2026 (``fixtures/mdhub_v1_4``: ``cny_rusfar_ois`` 1W-5Y, RUSFARCNY
fixings of 8-14.10) and synthetic trades (invented amounts and rates). The conventions of the curve, the index and its
calendar are provisional (``curves.fx``, ``valuation.derivatives.CCS_FOREIGN_INDEX``)."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date

import pytest
from test_swaps_repo_tdm45 import BD, FIX, ccs, ccs_plan

from desk.curves import fx as fxcurves
from desk.curves.rates import RUONIA_OIS_CURVE_ID
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.publish import tdm_run
from desk.rules import ocp as O
from desk.valuation import derivatives as dv


@pytest.fixture(scope="module")
def ctx():
    return prepare(SnapshotDir(FIX), BD.isoformat())


def rus(tid="R1", rub_side="pay", spread="0", start="2026-10-08", end="2028-10-08", index="RUSFARCNY", **kw):
    """A CCS of two rows: RUB (``kw``: fixed or RUONIA) against CNY floating on ``index`` (+ ``spread`` bp), 100m CNY."""
    t = ccs(tid, rub_side, 1_250_000_000, 100_000_000, start=start, end=end, **kw)
    t[1] = {k: v for k, v in {**t[1], "fixed_rate_pct": None, "index_code": index, "spread_bp": spread}.items()
            if v is not None}
    return t


FIXED = {"fixed_rate_pct": "15"}
RUONIA = {"index_code": "RUONIA"}


def without_curve(ctx, curve_id):
    return replace(ctx, core_market=replace(ctx.core_market, curves=tuple(
        c for c in ctx.core_market.curves if c.curve_id != curve_id)))


def value(ctx, terms, pid="R1"):
    bp, inp = ccs_plan(ctx, terms, pid)
    return bp.direct[pid], inp


# --- the run's inputs --------------------------------------------------------------------------------------------


def test_the_run_has_the_cny_rusfar_curve_and_the_fixings(ctx):
    c = ctx.fx.curves["CNY RUSFAR"]
    assert (c.ccy, c.kind, c.spec_id) == ("CNY", "ois", "cny_discount_factors_v1")
    assert "CNY RUSFAR" in {e.curve_id for e in ctx.core_market.curves}
    lin = ctx.fx.lineage["curves"]["CNY RUSFAR"]
    assert lin["status"] == "ok" and lin["tenors_used"][0] == "1W" and lin["tenors_used"][-1] == "5Y"
    assert lin["last_node"] == c.curve.nodes[-1][0].isoformat() and "flat-forward" in lin["extrapolation"]
    assert "provisional" in lin["convention"]
    assert "CNY RUSFAR" not in ctx.fx.fx_curve_ids("CNY", ctx.fx_options)[1]  # never an FX forward's curve
    # the fixings are the run's own, not the bonds' market
    (entry,) = [e for e in ctx.fx.index_fixings if e.fixing_id == "RUSFARCNY"]
    assert entry.fixing_id == "RUSFARCNY" and entry.fixings.index == "CNY-RUSFARCNY-COMPOUND"
    assert entry.fixings.valuation_date == BD and entry.fixings.fixings[0] == (date(2026, 10, 8), 0.0063)
    assert [d for d, _ in entry.fixings.fixings][-1] == date(2026, 10, 14)  # dated before the business date
    assert ctx.core_market.index_fixings == ()
    info = ctx.fx.lineage["index_fixings"]["RUSFARCNY"]
    assert info["status"] == "ok" and info["rows"] == 5 and info["bridged_days"] == []


def test_the_curve_reprices_its_quotes(ctx):
    """The one-period 1Y quote and the annual 2Y quote (ACT/365F fixed leg, TOM start) are the curve's par rates."""
    curve = ctx.fx.curves["CNY RUSFAR"].curve
    nodes = dict(curve.nodes)
    start = date(2026, 10, 16)  # TOM on FX:CNY
    df0 = nodes[start]
    end1 = date(2027, 10, 18)
    assert (df0 - nodes[end1]) / (nodes[end1] * (end1 - start).days / 365.0) == pytest.approx(0.0785, abs=1e-12)
    end2, mid = date(2028, 10, 16), date(2027, 10, 18)
    pv = 0.08 * ((mid - start).days / 365.0 * nodes[mid] + (end2 - mid).days / 365.0 * nodes[end2]) + nodes[end2]
    assert pv == pytest.approx(df0, abs=1e-12)
    assert curve.discount_factor(date(2026, 10, 15)) == 1.0 and 0 < df0 < 1.0


# --- valued by core ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rub", [FIXED, RUONIA], ids=["rub_fixed", "rub_ruonia"])
def test_a_floating_cny_leg_on_rusfarcny_is_valued_with_risk_on_the_three_curves(ctx, rub):
    o, inp = value(ctx, rus(**rub))
    v = o.valuation
    assert o.core_not_valued is None and v.extra["core"]["product"] == "ccs", (o.reason, o.detail)
    want = {"CNY RUSFAR", "CNY XCCY"} | {"RUB RUONIA"}
    assert set(v.curve_buckets) == want
    assert v.curve_buckets["CNY RUSFAR"]["2Y"] != 0 and v.curve_buckets["CNY XCCY"]["2Y"] != 0
    (fact,) = v.extra["swaps"]
    assert (fact["product"], fact["foreign_ccy"], fact["foreign_index"], fact["foreign_spread_bp"]) == (
        "ccs", "CNY", "RUSFARCNY", 0.0)
    assert fact["foreign_projection_curve"] == "CNY RUSFAR" and fact["foreign_fixing_id"] == "RUSFARCNY"
    assert fact["foreign_curve"] == "CNY XCCY" and fact["par_rate_pct"] is None
    assert fact["par_spread_bp"] is not None and abs(fact["par_spread_bp"]) < 1e4
    assert v.fx_delta["CNY"] == pytest.approx(fact["foreign_leg_pv"]) and v.extra["fx_rates"] == {"CNY": 12.5141}
    assert v.pv_rub == pytest.approx(fact["rub_leg_pv_rub"] + fact["foreign_leg_pv_rub"], rel=1e-9)
    assert v.extra["basis01_xccy"]["CNY"] != 0
    fx = v.extra["index_fixings"]["RUSFARCNY"]
    assert (fx["first"], fx["latest"], fx["missing_business_days"], fx["bridged_days"]) == (
        "2026-10-08", "2026-10-14", [], [])
    assert fact["side"] == ("receiver" if rub is RUONIA else "payer")  # paying the fixed RUB leg: payer
    assert any("RUSFARCNY compounded" in a and "provisional" in a for a in v.assumptions)
    # a 2Y swap ends inside the quotes (to 5Y): not marked as extrapolated
    assert any("CNY RUSFAR quotes to 2031-10-16" in a and "flat-forward" not in a for a in v.assumptions)
    assert "RUSFARCNY" in v.extra["core"]["curves"] or "CNY RUSFAR" in v.extra["core"]["curves"]
    row = tdm_run.direct_row(ctx, "IRB", "R1", o, inp.extra["R1"], None)
    assert row["instr_type"] != "FX" and row["fx_delta"] == {"CNY": v.fx_delta["CNY"]}


def test_the_spread_moves_the_pv_by_the_leg_annuity(ctx):
    base = value(ctx, rus(**FIXED))[0].valuation
    wide = value(ctx, rus(spread="100", **FIXED))[0].valuation
    fact = wide.extra["swaps"][0]
    assert fact["foreign_spread_bp"] == 100.0
    # receiving CNY at +100 bp: worth more; the par spread (the spread that sets the swap's PV to zero) is the same
    assert wide.pv_rub > base.pv_rub
    par = base.extra["swaps"][0]["par_spread_bp"]
    assert wide.extra["swaps"][0]["par_spread_bp"] == pytest.approx(par, abs=1e-6)
    at_par = value(ctx, rus(spread=repr(par), **FIXED))[0].valuation
    assert abs(at_par.pv_rub) < 1.0


def test_a_new_swap_foreign_leg_is_the_forward_compounding_of_the_curve(ctx):
    """Zero spread and zero basis (the XCCY curve's, provisional): a single-period swap starting ahead, no exchanges,
    receives ``N x (DF(a) / DF(b) - 1)`` of the CNY RUSFAR curve at the period's end, discounted on CNY XCCY (the year
    has no leap day: the index's 365 / 366 basis is the day count)."""
    t = rus(start="2026-10-20", end="2027-10-20", exchange="none", **FIXED)
    for r in t:
        r["day_count"] = "ACT/365"
    inst, _, _ = dv._ccs_instrument(ctx, dv.CcsTerms.of(t), "x", {})
    (p,) = inst.swap.foreign_leg.periods
    proj = ctx.fx.curves["CNY RUSFAR"].curve
    disc = ctx.fx.curve("CNY", "xccy").curve
    coupon = 1e8 * (proj.discount_factor(p.accrual_start) / proj.discount_factor(p.accrual_end) - 1.0)
    o, _ = value(ctx, t)
    fact = o.valuation.extra["swaps"][0]
    assert fact["foreign_leg_pv"] == pytest.approx(coupon * disc.discount_factor(p.payment_date), rel=1e-6)


# --- the fixings -------------------------------------------------------------------------------------------------


def test_a_started_swap_without_the_fixings_is_not_valued(ctx):
    bare = replace(ctx, fx=replace(ctx.fx, index_fixings=()))
    o, _ = value(bare, rus(**FIXED))
    assert o.core_not_valued[0] == "no_fixings" and "RUSFARCNY" in o.core_not_valued[1]
    o, _ = value(bare, rus(start="2026-10-20", **FIXED))  # starting ahead: no fixing is read
    assert o.core_not_valued is None


def test_a_swap_started_before_the_first_fixing_is_not_valued_with_the_date(ctx):
    o, _ = value(ctx, rus(start="2026-09-28", end="2027-09-28", **FIXED))
    assert o.core_not_valued[0] == "no_fixings"
    assert o.core_not_valued[1].startswith("no RUSFARCNY fixing for 2026-09-28 in the snapshot")


def _with_rows(ctx, drop):
    """``ctx`` whose RUSFARCNY history lacks the dates ``drop`` (rebuilt as the run builds it)."""
    snap = copy.deepcopy(ctx.market.snapshot)
    snap.data["fixings"] = [f for f in snap.data["fixings"]
                         if not (f["index_code"] == "RUSFARCNY" and f["fixing_date"] in drop)]
    cal = next(c.calendar for c in ctx.core_market.calendars if c.calendar_id == "FX:CNY")
    entry, info = fxcurves.rusfar_cny_fixings(snap, cal, BD)
    lineage = {**ctx.fx.lineage, "index_fixings": {"RUSFARCNY": info}}
    return replace(ctx, fx=replace(ctx.fx, index_fixings=(entry,), lineage=lineage)), info


def test_a_day_the_index_was_not_published_inside_the_history_is_bridged_and_said(ctx):
    full = value(ctx, rus(**FIXED))[0].valuation
    gap, info = _with_rows(ctx, {"2026-10-13"})
    assert info["bridged_days"] == ["2026-10-13"]
    o, _ = value(gap, rus(**FIXED))
    v = o.valuation
    assert o.core_not_valued is None, o.core_not_valued
    assert v.extra["index_fixings"]["RUSFARCNY"]["bridged_days"] == ["2026-10-13"]
    assert any("not published on 2026-10-13" in a and "previous fixing holds" in a for a in v.assumptions)
    assert v.pv_rub != full.pv_rub  # the previous value (0.68 %) in place of 0.54 % over a day


def test_one_unpublished_last_business_day_is_carried_and_said(ctx):
    late, info = _with_rows(ctx, {"2026-10-14"})
    assert info["bridged_days"] == [] and info["latest"] == "2026-10-13"
    assert info["unpublished_business_days"] == ["2026-10-14"] and info["max_unpublished_business_days"] == 1
    o, _ = value(late, rus(**FIXED))
    v = o.valuation
    assert o.core_not_valued is None, o.core_not_valued
    fx = v.extra["index_fixings"]["RUSFARCNY"]
    assert fx["unpublished_business_days"] == ["2026-10-14"] and fx["missing_business_days"] == []
    assert any("RUSFARCNY not yet published for 2026-10-14" in a and "2026-10-13" in a for a in v.assumptions)


def test_two_unpublished_last_business_days_are_missing_fixings_not_core_refusals(ctx):
    late, info = _with_rows(ctx, {"2026-10-13", "2026-10-14"})
    assert info["unpublished_business_days"] == []
    o, _ = value(late, rus(**FIXED))
    assert o.core_not_valued[0] == "no_fixings" and o.core_not_valued[1].startswith("no RUSFARCNY fixing for 2026-10-13 in the snapshot")


def test_a_preliminary_fixing_is_said(ctx):
    snap = copy.deepcopy(ctx.market.snapshot)
    for f in snap.data["fixings"]:
        if f["index_code"] == "RUSFARCNY" and f["fixing_date"] == "2026-10-14":
            f["labels"] = ["moex_marketdata_preliminary"]
    cal = next(c.calendar for c in ctx.core_market.calendars if c.calendar_id == "FX:CNY")
    entry, info = fxcurves.rusfar_cny_fixings(snap, cal, BD)
    assert info["preliminary_days"] == ["2026-10-14"]
    pre = replace(ctx, fx=replace(ctx.fx, index_fixings=(entry,),
                                  lineage={**ctx.fx.lineage, "index_fixings": {"RUSFARCNY": info}}))
    v = value(pre, rus(**FIXED))[0].valuation
    assert v.extra["index_fixings"]["RUSFARCNY"]["preliminary_days"] == ["2026-10-14"]
    assert any("preliminary value" in a and "2026-10-14" in a for a in v.assumptions)


def test_the_curve_starts_from_the_latest_fixing(ctx):
    assert ctx.fx.lineage["curves"]["CNY RUSFAR"]["front_rate"] == "RUSFARCNY 2026-10-14"
    nodes = ctx.fx.curves["CNY RUSFAR"].curve.nodes
    assert nodes[0] == (date(2026, 10, 16), pytest.approx(1.0 / (1.0 + 0.0043 / 365.0)))


def test_the_observation_calendar_follows_the_publication():
    from rub_fixed_income import BusinessCalendar

    base = BusinessCalendar(holidays=frozenset({date(2026, 10, 7)}), working_days=frozenset({date(2026, 10, 10)}),
                            version="T", valid_from=date(2026, 1, 1), valid_to=date(2030, 12, 31))
    published = {date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 9), date(2026, 10, 12)}
    cal, bridged, unpublished = fxcurves._observation_calendar(base, published)
    # 7.10 is a holiday and 8.10 a weekday with no fixing (bridged); 10.10 a working Saturday with none (bridged)
    assert bridged == [date(2026, 10, 8), date(2026, 10, 10)]
    assert [d for d in (date(2026, 10, n) for n in range(5, 13)) if cal.is_business_day(d)] == sorted(published)
    assert cal.is_business_day(date(2026, 10, 13))  # after the last fixing: the settlement calendar
    assert unpublished == []  # no business date given
    cal, _, unpublished = fxcurves._observation_calendar(base, published, date(2026, 10, 14))
    assert unpublished == [date(2026, 10, 13)] and not cal.is_business_day(date(2026, 10, 13))
    _, _, unpublished = fxcurves._observation_calendar(base, published, date(2026, 10, 15))
    assert unpublished == []  # two business days in front of the business date: required


# --- what is not valued ------------------------------------------------------------------------------------------


def test_without_the_projection_curve_the_swap_is_not_valued(ctx):
    o, _ = value(without_curve(ctx, "CNY RUSFAR"), rus(**FIXED))
    assert o.core_not_valued[0] == "no_foreign_projection_curve" and "CNY RUSFAR" in o.core_not_valued[1]


def test_without_the_xccy_curve_the_fx_curve_discounts_and_it_is_said(ctx):
    o, _ = value(without_curve(ctx, "CNY XCCY"), rus(**FIXED))
    v = o.valuation
    assert o.core_not_valued is None and set(v.curve_buckets) == {"RUB RUONIA", "CNY FX", "CNY RUSFAR"}
    assert any("no CNY XCCY curve in the run" in a for a in v.assumptions)


def test_other_indices_keep_their_reason(ctx):
    for index in ("SHIBOR", "SOFR"):
        o, _ = value(ctx, rus(index=index, **FIXED))
        assert o.core_not_valued[0] == "ccs_foreign_floating_unsupported", index
        assert index in o.core_not_valued[1] and "not supported" in o.core_not_valued[1]
    t = rus(**FIXED)  # RUSFARCNY on a leg that is not in CNY
    t[1]["ccy"] = "USD"
    with pytest.raises(dv.NotValued) as e:
        dv._ccs_build(ctx, dv.CcsTerms.of(t), "x", {})
    assert e.value.reason == "ccs_foreign_floating_unsupported"


def test_an_arithmetically_averaged_index_is_not_valued(ctx):
    t = rus(**FIXED)
    t[1]["x_overnight_arithmetic"] = True
    o, _ = value(ctx, t)
    assert o.core_not_valued[0] == "ccs_index_averaging_unsupported"
    t[1]["x_overnight_arithmetic"] = False
    assert value(ctx, t)[0].core_not_valued is None


def test_a_floating_rub_leg_on_another_index_still_is_not_valued(ctx):
    o, _ = value(ctx, rus(index_code="SHIBOR"))
    assert o.core_not_valued[0] == "swap_index_unsupported" and "RUONIA, KEY_RATE or RUSFAR" in o.core_not_valued[1]


# --- OCP, flows --------------------------------------------------------------------------------------------------


def _ocp(row, terms, basis):
    return O.classify(row, terms, {}, BD, replace(O.PROVISIONAL_POLICY, amount_basis=basis))


def test_the_fx_delta_and_the_ocp_amount_are_the_same_in_the_risk_row_and_the_ocp(ctx):
    t = rus(**RUONIA)  # receives 100m CNY (floating), pays RUONIA
    o, inp = value(ctx, t)
    v = o.valuation
    row = tdm_run.direct_row(ctx, "IRB", "R1", o, inp.extra["R1"], None)
    assert row["extra"]["ocp_amount"] == {"CNY": 1e8} and v.extra["ocp_amount"] == {"CNY": 1e8}
    comp, amounts, basis, _ = _ocp(row, t, O.AmountBasis.NOMINAL)
    assert (comp, amounts, basis) == ("fx_legs", {"CNY": 1e8}, "nominal")
    comp, amounts, basis, _ = _ocp(row, t, O.AmountBasis.SPOT_EQUIVALENT)
    assert comp == "fx_legs" and amounts == pytest.approx({"CNY": v.fx_delta["CNY"]})
    assert row["fx_delta"] == {"CNY": v.fx_delta["CNY"]}
    pay = rus(rub_side="receive", **RUONIA)  # pays the floating CNY leg
    o2, _ = value(ctx, pay)
    assert o2.valuation.extra["ocp_amount"] == {"CNY": -1e8} and o2.valuation.fx_delta["CNY"] < 0
    assert o2.valuation.extra["swaps"][0]["side"] == "payer"  # both legs float: the foreign leg's side counts


def test_the_flows_in_the_window_are_the_exchange_in_cny_and_the_coupons_in_rub(ctx):
    """A two-week swap (started 8.10, ends 22.10): its final exchange of 100m CNY is a CNY flow; the RUB leg's coupon and
    notional and the floating CNY coupon (core's RUB cash, at the spot it holds) are RUB."""
    t = rus(end="2026-10-22", **FIXED)
    o, _ = value(ctx, t)
    assert o.core_not_valued is None, o.core_not_valued
    flows = [f for f in o.valuation.extra["flows"] if f["amount"] is not None]
    by_ccy = {c: sum(f["amount"] for f in flows if f["ccy"] == c) for c in {f["ccy"] for f in flows}}
    assert set(by_ccy) == {"CNY", "RUB"}
    assert by_ccy["CNY"] == pytest.approx(1e8)  # the exchange only: the floating coupon is not in CNY
    rub_leg = -1.25e9 * (1.0 + 0.15 * 14 / 365.0)
    coupon_rub = by_ccy["RUB"] - rub_leg  # the floating CNY coupon, converted at the spot
    assert 0 < coupon_rub < 2e6  # ~100m CNY x 0.5 % x 14 / 365 x 12.5 RUB


def test_flows_of_a_fixed_cny_leg_are_unchanged(ctx):
    t = ccs("R2", "pay", 1_250_000_000, 100_000_000, start="2026-10-08", end="2026-10-22", index_code="RUONIA")
    o, _ = value(ctx, t, "R2")
    cny = sum(f["amount"] for f in o.valuation.extra["flows"] if f["ccy"] == "CNY")
    assert cny == pytest.approx(1e8 * (1 + 0.03 * 14 / 365.0))  # the fixed coupon and the exchange in CNY


def test_inputs_survive_a_date_with_no_cny_spot_or_no_hub_data(ctx):
    """Without the CNY spot there is no curve and no fixing; the swap is then not valued, not a crash."""
    spots = {k: v for k, v in ctx.fx.spots.items() if k != "CNY"}
    o, _ = value(replace(ctx, fx=replace(ctx.fx, spots=spots)), rus(**FIXED))
    assert o.core_not_valued[0] == "no_fx_spot"


# --- the run's curve reference, risk in quotes ---------------------------------------------------------------------


def test_the_projection_curve_is_in_the_reference_only_with_risk_on_it(ctx):
    names = lambda rows: {c["curve_id"] for c in tdm_run.curves_reference(ctx, ctx.market, rows, [{"ccy": "CNY"}])}
    assert "CNY RUSFAR" not in names([]) and "CNY XCCY" in names([])
    got = tdm_run.curves_reference(ctx, ctx.market, [{"risk": {"CNY RUSFAR": {"2Y": 1.0}}}], [{"ccy": "CNY"}])
    (c,) = [c for c in got if c["curve_id"] == "CNY RUSFAR"]
    assert c["ccy"] == "CNY" and c["kind"] == "swap" and c["points"] and all(p["move_bp"] == 0 for p in c["points"])


def test_the_previous_days_projection_curve_gives_the_move(ctx):
    ois = next(e.curve for e in ctx.core_market.curves if e.curve_id == RUONIA_OIS_CURVE_ID)
    old = tdm_run._prev_fx(ctx, ctx.market, ois)
    assert "CNY RUSFAR" in old and old["CNY RUSFAR"].nodes == ctx.fx.curves["CNY RUSFAR"].curve.nodes


def test_risk_in_quotes_keeps_the_pv_and_the_projection_curve_built(ctx):
    base = value(ctx, rus(**FIXED))[0].valuation
    on = value(replace(ctx, quote_curve_risk=True), rus(**FIXED))[0].valuation
    assert on.pv_rub == pytest.approx(base.pv_rub, rel=1e-9)
    assert set(on.curve_buckets) == set(base.curve_buckets) and on.curve_buckets["CNY RUSFAR"]["2Y"] != 0
    assert replace(ctx, quote_curve_risk=True).curve_units()["CNY RUSFAR"] == "zero"
