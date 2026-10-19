"""Slice S2 (day-over-day flash PnL) on the hub's golden fixtures.

The fixtures hold one date (2026-10-15). The previous date is synthetic: the same instruments a
day earlier, on a KBD whose level is 10 bp lower (B1 - 10, a parallel move of the zero curve),
with slightly different prices. Real two-date runs use the local snapshots (VERTICAL_SLICES S2).
"""

from __future__ import annotations

import dataclasses
import math
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from rub_fixed_income import CurveEntry, ShockedCurveCache, kbd_discount_curve

from desk.marketdata.adapter import KBD_CURVE_ID, KBD_SPEC, MarketView
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import EodContext, Outcome, attach_pnl, prepare, value
from desk.pnl.flash import FlashCurves, explain
from desk.publish import tdm_run
from desk.valuation.bonds import BondValuation, bond_request, value_bonds

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
    # The exact repricing method: bonds valued without assumptions. The synthetic previous date keeps
    # the fixture's fixings, which core refuses for floaters on another date; the fallback rungs get
    # the simple PnL (test_fallback).
    return [o for o in value(ctx, [(s, QTY) for s in secids], fallback=False)
            if o.valuation and o.valuation.quality == "exact"]


def shifted_prev(ctx: EodContext, prev_date: date, shift_bp: float = SHIFT_BP) -> EodContext:
    params = dataclasses.replace(ctx.kbd, trade_date=prev_date, b1=ctx.kbd.b1 - shift_bp)
    market = dataclasses.replace(ctx.core_market, business_date=prev_date,
                                 curves=(CurveEntry(KBD_CURVE_ID, KBD_SPEC, params),))
    return dataclasses.replace(ctx, kbd=params, curve=kbd_discount_curve(params), core_market=market,
                               cache=ShockedCurveCache())


def prev_outcome(prev: EodContext, o: Outcome, price_move_pct: float = 0.05) -> Outcome:
    """The same position valued on ``prev``: trade date moved back, every price a bit higher, and
    cum-coupon for the coupons paid since (their amount added to the accrued)."""
    if o.valuation is None:
        return o
    assert o.terms is not None and o.quote is not None and o.quote.accrued_per_bond is not None
    d = prev.kbd.trade_date  # the market view stays the fixture's; only the curve and quote move
    paid = sum((c.amount_per_bond or Decimal(0) for c in o.terms.coupons if d < c.pay_date <= o.quote.trade_date),
               Decimal(0))
    q = dataclasses.replace(o.quote, trade_date=d, accrued_per_bond=o.quote.accrued_per_bond + paid, prices_pct={
        k: v + Decimal(str(price_move_pct)) for k, v in o.quote.prices_pct.items()})
    valued, _ = value_bonds(prev.core_market, [bond_request("p", o.terms, q, d, o.valuation.quantity)])
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
        f = explain(p.valuation, o.valuation, FlashCurves.build(prev.kbd, ctx.kbd))
        dz = o.valuation.z_spread_bp - p.valuation.z_spread_bp
        assert abs(f.rates) < 1e-6  # the same yield per tenor: no rate move
        assert f.spread * dz <= 0  # wider spread, lower value for a long position
        assert abs(f.spread - o.valuation.spread01_rub * dz) < 0.05 * abs(o.valuation.spread01_rub * dz) + 1e-6


def test_coupon_received_in_the_window(ctx, outcomes):
    o = next(x for x in outcomes if x.secid == "SU26238RMFS4")
    last = max((c for c in o.terms.coupons if c.pay_date <= ctx.business_date), key=lambda c: c.pay_date)
    prev = shifted_prev(ctx, last.pay_date - timedelta(days=1), shift_bp=0.0)
    p = prev_outcome(prev, o, price_move_pct=0.0)
    f = explain(p.valuation, o.valuation, FlashCurves.build(prev.kbd, ctx.kbd))
    assert f.received_rub == pytest.approx(float(last.amount_per_bond) * QTY)
    assert abs(f.explained - f.total) < 1e-4
    assert f.carry > f.received_rub * 0.5  # the coupon is carry, not a price drop


def test_not_priced_previous_day_gets_no_pnl_and_a_warning(ctx, outcomes, tmp_path):
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    prevs = [prev_outcome(prev, o) for o in outcomes]
    gone = next(i for i, o in enumerate(outcomes) if o.valuation)
    prevs[gone] = Outcome(prevs[gone].secid, prevs[gone].terms, None, reason="no_quote")
    cur = [dataclasses.replace(o) for o in outcomes]
    attach_pnl(prev, prevs, ctx, cur)
    assert cur[gone].pnl is None and cur[gone].no_pnl.reason == "not_priced_prev"

    db = tmp_path / "tdm.sqlite"
    positions = [(f"P{i}", "OFZ" if o.secid.startswith("SU") else "CORP", o) for i, o in enumerate(cur)]
    prev_view = MarketView(ctx.market.snapshot)
    prev_view.business_date = prev.kbd.trade_date
    run_id, status, warnings = tdm_run.publish(str(db), ctx, positions, prev=prev_view)
    assert status == "complete"
    con = sqlite3.connect(db)
    n_pnl = con.execute("SELECT COUNT(*) FROM position_pnl WHERE run_id=?", (run_id,)).fetchone()[0]
    assert n_pnl == sum(o.pnl is not None for o in cur)
    carry, rates = con.execute("SELECT SUM(carry), SUM(rates) FROM position_pnl WHERE run_id=?", (run_id,)).fetchone()
    assert carry == pytest.approx(sum(o.pnl.carry for o in cur if o.pnl))
    assert rates == pytest.approx(sum(o.pnl.rates for o in cur if o.pnl))
    assert any(f"P{gone} " in w and "not_priced_prev" in w for w in warnings)
    assert any("1 позиций без PnL" in w for w in warnings)
