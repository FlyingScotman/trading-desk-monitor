"""TDM#826: a synthetic monthly floater and its KRS + OIS hedge through the monitor."""

from __future__ import annotations

import math
from copy import copy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import numpy as np
import pytest
from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    DayCount,
    KeyRateProjection,
    key_rate_weights,
    resolve_tenor,
)
from rub_fixed_income.dates import add_months
from test_slice_s2 import shifted_prev
from test_swap_pnl_split_804 import move_key_quotes
from test_swaps_repo_tdm45 import BD, FIX, leg

from desk.curves import rates
from desk.marketdata.adapter import BondQuote, BondTerms, Coupon, Redemption
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import attach_pnl, prepare, value
from desk.pnl.flash import FlashCurves
from desk.publish import tdm_run
from desk.store import PositionInputs
from desk.valuation import bonds, derivatives, positions
from desk.valuation.bonds import SPREAD_CONVENTION

SECID = "SYNTHETIC-KS"
QUANTITY = 100_000
NOTIONAL = QUANTITY * 1000
RUB, KEY = "RUB RUONIA", "RUB KEY"


def with_floater(ctx, start, bond_class="corp"):
    face = Decimal(1000)
    dates = [add_months(start, i) for i in range(32)]
    terms = BondTerms(
        secid=SECID, isin=None, name=SECID, short_name=None, bond_class=bond_class, status="active",
        issuer_id=None, currency="RUB", face_unit="RUB", face_value=face, coupon_type="floating",
        coupon_frequency=12, maturity_date=dates[-1], is_perpetual=False, is_structured=False,
        is_face_indexed=False, is_fx_face=False, is_amortizing=False, calendar="MOEX_STOCK", primary_board="TQCB",
        settle_lag_days=0, labels=(),
        coupons=tuple(Coupon(i, a, b, None, None, None, "projected", face)
                      for i, (a, b) in enumerate(pairwise(dates), 1)),
        redemptions=(Redemption(dates[-1], "maturity", face),), offers=(),
        floater_formula={"benchmark": "KEY_RATE", "spread_bp": 165, "status": "parsed", "lag_days": 0,
                         "averaging": "arithmetic"},
        day_count="ACT/365F",
    )
    market, static = copy(ctx.market), copy(ctx.static)
    market.business_date = ctx.core_market.business_date
    quote = BondQuote(SECID, "TQCB", market.business_date, face, Decimal(0), {"legal_close": Decimal(100)}, 1, {})
    market.quotes = {(SECID, "TQCB"): quote}
    static.bonds = {SECID: terms}
    return replace(ctx, market=market, static=static)


@pytest.fixture(scope="module")
def market():
    return prepare(SnapshotDir(FIX), BD.isoformat())


@pytest.fixture(scope="module")
def ctx(market):
    start = market.key_rate_request.build.calendar.add_business_days(BD, 1)
    return with_floater(market, start)


def swap_terms(ctx):
    """Par KRS and OIS at the same tenors and with the same conventions as the market quotes."""
    ois = ctx.risk_market.curve(rates.RUONIA_OIS_CURVE_ID).curve
    krs = ctx.key_rate_request
    return [
        *swap_legs(ctx.business_date, "irs", "KEY_RATE", krs.build.calendar,
                   [(q.tenor, q.mid) for q in krs.build.quotes]),
        *swap_legs(ctx.business_date, "ois", "RUONIA", ois.convention.ois_swap.calendar,
                   [(q.tenor, q.par_rate) for q in ois.quotes]),
    ]


def swap_legs(bd, kind, index, cal, quotes):
    for tenor, rate in quotes:
        dates = resolve_tenor(bd, tenor, cal)
        tid = f"{kind}-{tenor}"
        info = {"kind": kind, "trade_date": bd.isoformat(), "start_date": dates.effective_date.isoformat(),
                "maturity_date": dates.unadjusted_end.isoformat(), "notional_ccy": str(NOTIONAL), "book_id": "TEST"}
        yield leg(tid, "fixed", "pay", fixed_rate_pct=str(rate * 100), **info)
        yield leg(tid, "floating", "receive", index_code=index, **info)


def run_book(ctx, swaps):
    ids = list(dict.fromkeys(t["trade_id"] for t in swaps))
    inputs = PositionInputs([("FRN", "TEST", SECID, QUANTITY), *((tid, "TEST", None, None) for tid in ids)],
                            [], {"TEST": ctx.business_date}, datetime.now(UTC), "0" * 64,
                            {tid: {"asset_class": "otc", "ccy": "RUB", "deal_id": tid} for tid in ids},
                            trade_terms=swaps)
    plan = positions.plan(ctx, inputs, second_order=False)
    outcomes = value(ctx, plan.requests, position_ids=plan.position_ids, fallback=False)
    merged = positions.merge(plan, outcomes)
    assert all(o.valuation is not None for _, _, o in merged), [(pid, o.reason, o.detail) for pid, _, o in merged]
    return plan, outcomes[0], tdm_run.run_content(ctx, merged, None, ctx.lineage(), inputs)


def vector(row):
    return [row["risk"].get(c, {}).get(b, 0.0) for c in (RUB, KEY) for b in RISK_BUCKETS]


def test_run_uses_one_key_rate_request_and_publishes_core_quote_risk(ctx, monkeypatch):
    requests = []

    def observe(valuation):
        def wrapped(**kwargs):
            requests.append(kwargs["snapshot"].curve(rates.KEY_RATE_CURVE_ID).curve)
            return valuation(**kwargs)
        return wrapped

    monkeypatch.setattr(bonds, "value_portfolio", observe(bonds.value_portfolio))
    monkeypatch.setattr(derivatives, "value_portfolio", observe(derivatives.value_portfolio))
    trades = [t for t in swap_terms(ctx) if t["trade_id"] == "irs-3Y"]
    plan, floater, content = run_book(ctx, trades)
    req = ctx.key_rate_request
    assert len(requests) == 2 and all(r is req for r in requests)
    assert ctx.lineage()["curve_units"][KEY] == "key_rate_quote"
    keyref = next(c for c in content.reference["curves"] if c["curve_id"] == KEY)
    assert len(keyref["points"]) == len(RISK_BUCKETS)
    graph_zero = ctx.key_rate_display.zero_rate(BD + timedelta(days=30))
    assert keyref["points"][0]["rate"] == round(100 * graph_zero, 4)
    assert floater.core_result.positions[0].projection_curve_basis == "key_rate_quote"
    assert plan.core_result.swaps[0].projection_curve_basis == "key_rate_quote"

    v = floater.valuation
    leg_model = v.instrument.floating_leg(ctx.risk_market)
    reference = BondRiskEngine(BondRiskRequest(
        cashflows=tuple(BondCashflow(f.payment_date, f.amount) for f in v.instrument.cashflows(after=v.valuation_date)),
        settlement_date=v.valuation_date, curve=ctx.risk_curve(rates.RUONIA_OIS_CURVE_ID, ctx.discount[1]),
        spread_kind="credit", spread_convention=SPREAD_CONVENTION, dirty_price_rub=v.dirty_rub,
        quantity=QUANTITY, floating_leg=leg_model,
        projection_curve=KeyRateProjection(req, leg_model.history.calendar, leg_model.rule.rate_day_count,
                                           leg_model.rule.observation_days),
    )).risk(difference="central")
    bond, swap = content.rows
    discount = math.fsum(bond["risk"][RUB].values())
    projection = math.fsum(bond["risk"][KEY].values())
    net = math.fsum(vector(bond))
    assert bond["risk"][KEY] == pytest.approx({b.bucket: b.dv01_rub for b in reference.projection_buckets})
    assert net == pytest.approx(reference.dv01_rub, abs=1e-5)
    assert 0.03 < net / abs(discount) < 0.05
    assert math.fsum(swap["risk"][KEY].values()) == pytest.approx(projection, rel=0.16)


def test_par_krs_and_ois_hedge_each_bucket_in_the_monitor(ctx):
    trades = swap_terms(ctx)
    _, _, unhedged = run_book(ctx, trades)
    bond, *swaps = unhedged.rows
    assert all(abs(s["mv_rub"]) < 0.01 for s in swaps)
    sizes = np.linalg.lstsq(np.array([vector(s) for s in swaps]).T, vector(bond), rcond=None)[0]
    by_id = {r["position_id"]: size for r, size in zip(swaps, sizes, strict=True)}
    hedge = []
    for t in trades:
        scale = by_id[t["trade_id"]]
        if abs(scale) > 1e-10:
            side = {"pay": "receive", "receive": "pay"}[t["side"]] if scale > 0 else t["side"]
            hedge.append({**t, "notional_ccy": str(abs(scale) * NOTIONAL), "side": side})
    _, _, hedged = run_book(ctx, hedge)
    residual = np.sum([vector(r) for r in hedged.rows], axis=0)
    assert residual == pytest.approx(np.zeros(2 * len(RISK_BUCKETS)), abs=0.01)


def test_key_rate_floater_flash_uses_krs_quotes_on_both_days(ctx, market):
    start = ctx.static.bonds[SECID].coupons[0].start
    prev = with_floater(shifted_prev(market, BD - timedelta(days=1), 0.0), start)
    old, = value(prev, [(SECID, QUANTITY)], fallback=False)
    assert old.valuation is not None, old.detail
    flat, = value(ctx, [(SECID, QUANTITY)], fallback=False)
    moved = move_key_quotes(ctx, 2.0)
    bumped, = value(moved, [(SECID, QUANTITY)], fallback=False)
    attach_pnl(prev, [old], ctx, [flat])
    attach_pnl(prev, [old], moved, [bumped])
    assert flat.pnl and bumped.pnl, (flat.no_pnl, bumped.no_pnl)
    linear = bumped.pnl.rates_linear - flat.pnl.rates_linear
    exact = bumped.pnl.rates - flat.pnl.rates
    assert abs(exact) > 1_000.0
    assert linear == pytest.approx(exact, rel=0.02)
    assert bumped.pnl.curve_moves_bp[KEY] == pytest.approx(dict.fromkeys(RISK_BUCKETS, 2.0))


def test_even_a_key_rate_ofz_uses_linked_ruonia(market, ctx):
    start = ctx.static.bonds[SECID].coupons[0].start
    (outcome,) = value(with_floater(market, start, "ofz_pd"), [(SECID, QUANTITY)], fallback=False)
    assert outcome.valuation is not None, outcome.detail
    assert outcome.valuation.discount_curve_id == rates.RUONIA_OIS_CURVE_ID
    assert set(outcome.valuation.risk()) == {RUB, KEY}


def test_nonparallel_moves_reproduce_matched_krs_quotes_at_the_risk_maturities(market):
    prev = shifted_prev(market, BD - timedelta(days=1), 0.0)
    request = prev.key_rate_request
    target = np.linspace(-2.0, 3.0, len(RISK_BUCKETS))
    weights = np.array([
        key_rate_weights(DayCount.ACT_ACT_ISDA.year_fraction(
            prev.kbd.trade_date, resolve_tenor(prev.kbd.trade_date, q.tenor, request.build.calendar).end_date))
        for q in request.build.quotes
    ])
    moves = weights @ target
    entry = market.risk_market.curve(rates.KEY_RATE_CURVE_ID)
    quotes = tuple(replace(q, mid=q.mid + bp / 1e4) for q, bp in zip(entry.curve.build.quotes, moves, strict=True))
    shifted = replace(entry, curve=replace(entry.curve, build=replace(entry.curve.build, quotes=quotes)))
    cur = replace(market, quote_curves=tuple(shifted if c == entry else c for c in market.quote_curves))
    got = FlashCurves.from_contexts(prev, cur).curve_move_bp(rates.KEY_RATE_CURVE_ID)
    assert weights @ np.array(list(got.values())) == pytest.approx(moves, abs=1e-10)
