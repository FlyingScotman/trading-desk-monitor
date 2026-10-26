"""Slice S2 (day-over-day flash PnL) on the hub's golden fixtures.

The fixtures hold one date (2026-10-15). The previous date is synthetic: the same instruments a
day earlier, on curves 10 bp lower (the KBD's B1 - 10; RUONIA OIS and the key-rate projection in parallel), the
fixings known then, with slightly different prices. Real two-date runs use the local snapshots (VERTICAL_SLICES S2).
"""

from __future__ import annotations

import dataclasses
import math
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from rub_fixed_income import (
    CurveEntry,
    DiscountCurve,
    FixingEntry,
    bootstrap_discount_curve,
    kbd_discount_curve,
    resolve_tenor,
)
from run_checks import consistency, content, warnings

import desk.orchestrator as orch
from desk.marketdata.adapter import KBD_CURVE_ID, KBD_SPEC, MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import EodContext, Outcome, attach_pnl, prepare, value
from desk.pnl.flash import FlashCurves, explain
from desk.valuation.bonds import OFZ_KBD_CLASSES, BondValuation, bond_request, value_bonds

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"
QTY = 1000.0
SHIFT_BP = 10.0


@pytest.fixture(scope="module")
def ctx() -> EodContext:
    return prepare(SnapshotDir(FIX), DATE)


@pytest.fixture(scope="module")
def secids(ctx) -> list[str]:
    return sorted(ctx.static.bonds)


@pytest.fixture(scope="module")
def outcomes(ctx, secids) -> list[Outcome]:
    # The exact repricing method: bonds valued without assumptions (the floaters, ``assumed``, are in
    # test_floater_rates_on_both_curves); the fallback rungs get the simple PnL (test_fallback).
    return [o for o in value(ctx, [(s, QTY) for s in secids], fallback=False)
            if o.valuation and o.valuation.quality == "exact"]


def shifted_curve(c: DiscountCurve, prev_date: date, shift_bp: float) -> DiscountCurve:
    """``c`` dated ``prev_date`` with its shape in days kept (``roll_curve`` of it is ``c``) and every continuous
    zero rate ``shift_bp`` lower."""
    days = c.curve_date - prev_date
    return dataclasses.replace(c, curve_date=prev_date, nodes=tuple(
        (n - days, df * math.exp(shift_bp * 1e-4 * c.convention.time_day_count.year_fraction(c.curve_date, n)))
        for n, df in c.nodes))


def shifted_prev(ctx: EodContext, prev_date: date, shift_bp: float = SHIFT_BP, *, kbd_only: bool = False
                 ) -> EodContext:
    """The fixture's market on ``prev_date``: the KBD's level ``shift_bp`` lower (B1), the RUONIA OIS quotes
    ``shift_bp`` lower and the curve rebootstrapped (its risk is in those quotes: ``EodContext.quote_curves``), the
    key-rate projection ``shift_bp`` lower in parallel, the fixings known before ``prev_date``. ``kbd_only``: a market
    without the rates curves (the KBD fallback)."""
    params = dataclasses.replace(ctx.kbd, trade_date=prev_date, b1=ctx.kbd.b1 - shift_bp)
    curves = [CurveEntry(KBD_CURVE_ID, KBD_SPEC, params)]
    quoted = {e.curve_id: e for e in ctx.quote_curves}
    quote_curves = []
    if not kbd_only:
        for e in ctx.core_market.curves:
            if not isinstance(e.curve, DiscountCurve):
                continue
            if e.curve_id in quoted:
                req = quoted[e.curve_id].curve
                cal = req.convention.ois_swap.calendar  # the tenors resolved from prev_date, as rates.ruonia_ois does
                req = dataclasses.replace(req, curve_date=prev_date, quotes=tuple(
                    dataclasses.replace(q, maturity=resolve_tenor(prev_date, q.tenor, cal).end_date,
                                        par_rate=q.par_rate - shift_bp * 1e-4) for q in req.quotes))
                quote_curves.append(dataclasses.replace(quoted[e.curve_id], curve=req))
                curves.append(dataclasses.replace(e, curve=bootstrap_discount_curve(req)))
            else:
                curves.append(dataclasses.replace(e, curve=shifted_curve(e.curve, prev_date, shift_bp)))
    fixings = tuple(FixingEntry(f.fixing_id, dataclasses.replace(
        f.history, valuation_date=prev_date, fixings=tuple(r for r in f.history.fixings if r[0] < prev_date),
        published_at=())) for f in ctx.core_market.fixings)
    market = dataclasses.replace(ctx.core_market, business_date=prev_date, curves=tuple(curves), fixings=fixings,
                                 fx_fixings=tuple(f for f in ctx.core_market.fx_fixings if f.fixing_date <= prev_date))
    return dataclasses.replace(ctx, kbd=params, curve=kbd_discount_curve(params, zero_shock="annual_par"),
                               core_market=market, cache=orch.new_cache(), quote_curves=tuple(quote_curves))


def flash_curves(prev: EodContext, ctx: EodContext) -> FlashCurves:
    return FlashCurves.build(prev.kbd, ctx.kbd, prev.curve, ctx.curve, prev_market=prev.core_market,
                             cur_market=ctx.core_market)


def prev_outcome(prev: EodContext, o: Outcome, price_move_pct: float = 0.05) -> Outcome:
    """The same position valued on ``prev``: trade date moved back, every price a bit higher, and
    cum-coupon for the coupons paid since (their amount added to the accrued)."""
    if o.valuation is None:
        return o
    assert o.terms is not None and o.quote is not None
    d = prev.kbd.trade_date  # the market view stays the fixture's; only the curve and quote move
    paid = sum((c.amount_per_bond or Decimal(0) for c in o.terms.coupons if d < c.pay_date <= o.quote.trade_date),
               Decimal(0))
    acc = None if o.quote.accrued_per_bond is None else o.quote.accrued_per_bond + paid
    q = dataclasses.replace(o.quote, trade_date=d, accrued_per_bond=acc, prices_pct={
        k: v + Decimal(str(price_move_pct)) for k, v in o.quote.prices_pct.items()})
    valued, _ = value_bonds(prev.risk_market, [bond_request("p", o.terms, q, d, o.valuation.quantity)])
    assert isinstance(valued["p"], BondValuation), valued["p"]
    return Outcome(o.secid, o.terms, q, valuation=valued["p"])


def test_valuation_date_is_the_trade_date(outcomes):
    for o in outcomes:
        if o.valuation:
            assert o.valuation.valuation_date == o.quote.trade_date
            assert o.valuation.settlement > o.valuation.valuation_date  # T+1, kept for information
            assert all(cf.payment_date > o.quote.trade_date for cf in o.valuation.cashflows)


def test_flash_components_add_up(ctx, outcomes):
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) for o in outcomes]
    cur = [dataclasses.replace(o) for o in outcomes]
    attach_pnl(prev, prevs, ctx, cur)
    priced = [o for o in cur if o.valuation]
    assert len(priced) >= 3
    for o, p in zip(cur, prevs):
        if o.valuation is None:
            assert o.pnl is None and o.no_pnl is None  # not written at all; S1 logs its reason
            continue
        f = o.pnl
        assert f is not None, o.no_pnl
        # the identity: components == q * (dirty_D - dirty_D-1) + cashflows received
        total = QTY * (o.valuation.dirty_rub - p.valuation.dirty_rub) + f.received_rub
        assert abs(f.explained - total) < 1e-4 and abs(f.identity_error) < 1e-4
        assert abs(f.v0 - p.valuation.pv_rub) < 1e-4
        # one day of carry and rolldown is about the dirty value times the continuous yield
        accrual = p.valuation.dirty_rub * QTY * math.log1p(p.valuation.ytm_pct / 100) / 365
        assert 0.6 * accrual < f.carry + f.rolldown < 1.4 * accrual, (o.secid, f.carry, f.rolldown, accrual)
        # the curve rose 10 bp in parallel: a long position loses, as the bucket DV01 predicts
        assert f.rates < 0
        assert abs(f.rates - f.rates_linear) < 0.02 * abs(f.rates_linear), (o.secid, f.rates, f.rates_linear)
        assert f.received_rub == pytest.approx(QTY * sum(
            cf.amount for cf in p.valuation.cashflows if cf.payment_date <= o.valuation.valuation_date))
        assert f.as_tdm()["convexity"] == 0.0


def test_spread_is_the_z_change(ctx, outcomes):
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1), shift_bp=0.0)
    for o in outcomes:
        if o.valuation is None:
            continue
        p = prev_outcome(prev, o)
        f = explain(p.valuation, o.valuation, flash_curves(prev, ctx))
        dz = o.valuation.z_spread_bp - p.valuation.z_spread_bp
        # the same yield per tenor: no rate move; on RUONIA, rebuilt from the same OIS quotes resolved from the day
        # before, its quotes' calendar moves a few RUB on a million (0.2.6, EodContext.quote_curves); the KBD none
        on_kbd = o.valuation.discount_curve_id == KBD_CURVE_ID
        assert abs(f.rates) < (1e-6 if on_kbd else 5e-5 * abs(p.valuation.pv_rub)), (o.secid, f.rates)
        assert f.spread * dz <= 0  # wider spread, lower value for a long position
        assert abs(f.spread - o.valuation.spread01_rub * dz) < 0.05 * abs(o.valuation.spread01_rub * dz) + 1e-6


def test_coupon_received_in_the_window(ctx, outcomes):
    o = next(x for x in outcomes if x.secid == "SU26238RMFS4")
    last = max((c for c in o.terms.coupons if c.pay_date <= ctx.business_date), key=lambda c: c.pay_date)
    prev = shifted_prev(ctx, last.pay_date - timedelta(days=1), shift_bp=0.0)
    p = prev_outcome(prev, o, price_move_pct=0.0)
    f = explain(p.valuation, o.valuation, flash_curves(prev, ctx))
    assert f.received_rub == pytest.approx(float(last.amount_per_bond) * QTY)
    assert abs(f.explained - f.total) < 1e-4
    assert f.carry > f.received_rub * 0.5  # the coupon is carry, not a price drop


def test_ofz_on_the_kbd_the_rest_on_ruonia(outcomes):
    """The owner's decisions of 20.10 and 23.10 (#32): a fixed-coupon OFZ has its Z-spread to the KBD and its rate
    DV01 on RUB OFZ in par buckets 1Y-20Y+ (rub_ofz_kbd_v3: 1M-6M are 0); any other fixed RUB bond its Z-spread to
    RUONIA OIS and its DV01 on RUB RUONIA. Neither is an assumption."""
    kinds = Counter(o.terms.bond_class in OFZ_KBD_CLASSES for o in outcomes)
    assert kinds[True] and kinds[False], kinds
    for o in outcomes:
        v = o.valuation
        ofz = o.terms.bond_class in OFZ_KBD_CLASSES
        want = ("RUB_KBD", "RUB OFZ") if ofz else ("RUB_RUONIA_OIS", "RUB RUONIA")
        assert (v.discount_curve_id, v.spread_curve) == want, o.secid
        assert set(v.risk()) == ({want[1], "RUB RUONIA"} if ofz else {want[1]}) and v.dv01_rub < 0 and v.spread01_rub < 0
        assert v.quality == "exact" and not any("KBD" in a for a in v.assumptions)
        if ofz:  # RUONIA + basis (#213): the basis (RUB OFZ) has no 1M-6M risk, RUONIA does; dv01 = the RUONIA risk
            assert all(v.risk()["RUB OFZ"][b] == 0.0 for b in ("1M", "3M", "6M")) and v.spread_kind == "ofz"
            assert v.ofz_ruonia and v.dv01_rub == pytest.approx(math.fsum(v.ofz_ruonia.values()))


@pytest.mark.parametrize("secid,curves", [("SU29008RMFS8", {"RUB RUONIA"}),
                                          ("RU000A109XR1", {"RUB RUONIA", "RUB KEY"})])
def test_floater_rates_on_both_curves(ctx, secid, curves):
    """A floater's ``rates`` reprices its discount and projection curves: it matches the linear explain (DV01 per
    curve x that curve's move) and leaves nothing in ``other`` when nothing else changed."""
    o = value(ctx, [(secid, QTY)], fallback=False)[0]
    assert o.valuation is not None and o.valuation.floater is not None, o.reason
    assert {c for c, bs in o.valuation.risk().items() if any(bs.values())} == curves
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    p = prev_outcome(prev, o)
    f = explain(p.valuation, o.valuation, flash_curves(prev, ctx))
    assert abs(f.rates - f.rates_linear) < 0.02 * abs(f.rates_linear) + 1.0, (f.rates, f.rates_linear)
    assert abs(f.other) < 1.0, f.other
    assert abs(f.explained - f.total) < 1e-4


def test_kbd_fallback_without_ruonia(ctx, outcomes):
    """No RUONIA OIS curve in the market: the bonds are discounted on the KBD, say so (``assumed``) and carry their
    DV01 on RUB OFZ; a PnL across the change of curve is the simple one (all in other), the run goes on."""
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1), kbd_only=True)
    prevs = [prev_outcome(prev, o) for o in outcomes]
    for p in prevs:
        v = p.valuation
        assert (v.discount_curve_id, v.quality, set(v.risk())) == ("RUB_KBD", "assumed", {"RUB OFZ"})
        assert any("discounted on the KBD: no RUONIA OIS curve" in a for a in v.assumptions)
    cur = [dataclasses.replace(o) for o in outcomes]
    attach_pnl(prev, prevs, ctx, cur)
    for o in cur:
        assert o.pnl is not None
        if o.terms.bond_class in OFZ_KBD_CLASSES:  # on the KBD on both dates: the exact explain
            assert o.pnl.rates < 0 and abs(o.pnl.rates - o.pnl.rates_linear) < 0.02 * abs(o.pnl.rates_linear)
            continue
        assert o.pnl.rates == 0.0 and o.pnl.other == pytest.approx(o.pnl.total)
        assert "discounted on RUB OFZ" in o.pnl.note
    # both dates on the KBD: the exact explain on it, as before the switch
    prev2 = shifted_prev(ctx, ctx.business_date - timedelta(days=2), 2 * SHIFT_BP, kbd_only=True)
    p2 = prev_outcome(prev2, prevs[0])
    f = explain(p2.valuation, prevs[0].valuation, flash_curves(prev2, prev))
    assert abs(f.rates - f.rates_linear) < 0.02 * abs(f.rates_linear) and abs(f.other) < 1e-6


def test_not_priced_previous_day_gets_no_pnl_and_a_warning(ctx, outcomes):
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) for o in outcomes]
    gone = next(i for i, o in enumerate(outcomes) if o.valuation)
    prevs[gone] = Outcome(prevs[gone].secid, prevs[gone].terms, None, reason="no_quote")
    cur = [dataclasses.replace(o) for o in outcomes]
    attach_pnl(prev, prevs, ctx, cur)
    assert cur[gone].pnl is None and cur[gone].no_pnl.reason == "not_priced_prev"

    positions = [(f"P{i}", "OFZ" if o.secid.startswith("SU") else "CORP", o) for i, o in enumerate(cur)]
    prev_view = MarketView(ctx.market.snapshot)
    prev_view.business_date = prev.kbd.trade_date
    c = content(ctx, positions, prev=prev_view)
    assert consistency(c) == []
    pnl = [r["pnl"] for r in c.rows if r.get("pnl") is not None]
    assert len(pnl) == sum(o.pnl is not None for o in cur)
    assert sum(p["carry"] for p in pnl) == pytest.approx(sum(o.pnl.carry for o in cur if o.pnl))
    assert sum(p["rates"] for p in pnl) == pytest.approx(sum(o.pnl.rates for o in cur if o.pnl))
    assert any(f"P{gone} " in w and "not_priced_prev" in w for w in warnings(c))
    # one published row without PnL: the position not priced the day before (no PnL, never 0)
    assert [r["position_id"] for r in c.rows if r.get("pnl") is None] == [f"P{gone}"]
