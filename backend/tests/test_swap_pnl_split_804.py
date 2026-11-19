"""FlyingScotman/trading-desk-monitor#804: the day's PnL of a swap or REPO valued by core is split into carry, rolldown and
rates, not all in ``other``.

Until then the PnL of a derivative valued by core was the change of its PV plus the cash it paid, the FX delta's part
in ``fx_gross``, everything else in ``other`` (``fx_core_rest``): a RUB swap had nothing but ``other``. Now the D-1 plan
carries core's horizon to D (``extra.horizon``), and ``pnl.direct._rates_split`` takes its carry and rolldown, the D-1
bucket DV01 x the curve's move as ``rates``, and leaves the rest. On the contract 1.4 golden snapshot of 15.10.2026 (D)
and the same market one day earlier with the RUONIA curve moved by an invented number of basis points (D-1), a synthetic
swap and REPO (invented notionals and rates)."""

from __future__ import annotations

import copy
import dataclasses

import pytest
from test_deriv_pnl_flows_466 import (
    BD,
    FIX,
    PREV,
    _with_fixings_from_september,
    inputs,
    repo,
    swap,
)
from test_slice_s2 import shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.pnl import direct
from desk.pnl.reasons import OTHER_REASON_TEXT, RATES_CORE_METHOD, RATES_CORE_REST
from desk.valuation import positions as pv1

MOVE_BP = 25.0
"""The RUONIA OIS curve of D is this much above D-1's (``shifted_prev`` moves D-1 down)."""


@pytest.fixture(scope="module")
def ctx():
    return _with_fixings_from_september(prepare(SnapshotDir(FIX), BD.isoformat()))


def _prev(ctx, bp: float):
    p = shifted_prev(ctx, PREV, bp)
    m = copy.copy(ctx.market)
    m.business_date = PREV
    m.fx = ctx.market.fx
    return dataclasses.replace(p, market=m)


@pytest.fixture(scope="module")
def flat(ctx):
    """D-1 with the same curves as D: the day's PnL is time and flows."""
    return _prev(ctx, 0.0)


@pytest.fixture(scope="module")
def moved(ctx):
    return _prev(ctx, MOVE_BP)


def day(prev, ctx, terms, pid, *, horizon=True):
    """(outcome on D with the PnL attached by ``pnl.direct``, outcome on D-1); ``horizon``: as ``dayrun`` plans D-1."""
    inp, prev_inp = inputs(ctx.business_date, terms, pid), inputs(prev.business_date, terms, pid)
    bp = pv1.plan(ctx, inp)
    pbp = pv1.plan(prev, prev_inp, horizon=ctx.business_date if horizon else None)
    direct.attach(prev, ctx, bp.direct, inp, pbp.direct, prev_inp.valid_dates)
    return bp.direct[pid], pbp.direct[pid]


def test_a_swap_on_an_unmoved_market_is_carry_and_a_small_rest(ctx, flat):
    """The monthly coupon paid on D and a day of accrual, no move of the curves: all of it is the carry, the rates are 0
    and what is left is the day's fixing against the forward, small beside the carry."""
    o, p = day(flat, ctx, swap("S1"), "S1")
    pnl = o.pnl
    assert o.no_pnl is None and pnl.method == RATES_CORE_METHOD
    h = p.valuation.extra["horizon"]
    assert h["date"] == BD.isoformat() and h["received_rub"] == pytest.approx(pnl.received_rub)
    assert pnl.carry == pytest.approx(h["carry_rub"]) and pnl.rolldown == pytest.approx(h["rolldown_rub"])
    assert pnl.fx_gross == 0.0
    # the curves of the two dates are two builds, not one: their tenor pillars differ by hundredths of a bp, which the
    # rolled curve (rolldown) and the move (rates) count with opposite signs; both are small beside a real move (below)
    assert pnl.carry != 0.0 and abs(pnl.rates) < 5e4 and abs(pnl.rolldown) < 5e4 and abs(pnl.other) < 2e4
    assert pnl.explained == pytest.approx(pnl.total) and pnl.as_tdm()["carry"] == pnl.carry
    assert pnl.extra()["other_reason"]["code"] == RATES_CORE_REST and RATES_CORE_REST in OTHER_REASON_TEXT


def test_a_move_of_the_curve_is_rates_by_the_d1_risk(ctx, moved):
    """RUONIA 25 bp up: the swap's rates are its D-1 bucket DV01 x the move per bucket, and carry, rolldown and rates
    leave a rest of a few percent of the rates (the second order, the day's fixing)."""
    o, p = day(moved, ctx, swap("S1"), "S1")
    pnl = o.pnl
    assert o.no_pnl is None and pnl.method == RATES_CORE_METHOD
    risk = p.valuation.curve_buckets["RUB RUONIA"]
    moves = pnl.curve_moves_bp["RUB RUONIA"]
    assert all(moves[b] == pytest.approx(MOVE_BP, abs=3.0) for b in moves)  # the move is the curve's: about 25 bp
    assert pnl.rates == pytest.approx(sum(risk[b] * moves[b] for b in moves)) and abs(pnl.rates) > 1e6
    assert sum(pnl.rates_linear_curves["RUB RUONIA"].values()) == pytest.approx(pnl.rates)
    assert abs(pnl.other) < 0.02 * abs(pnl.rates), (pnl.other, pnl.rates)
    assert abs(pnl.carry) < 0.1 * abs(pnl.rates)  # a day of carry beside a 25 bp move
    assert pnl.explained == pytest.approx(pnl.total) and abs(pnl.total) > 1e6
    ex = pnl.extra()
    assert ex["rates_linear"] == pnl.rates and ex["curve_moves_bp"] == pnl.curve_moves_bp


def test_without_the_horizon_the_row_keeps_everything_in_other(ctx, moved):
    """A D-1 plan asked without the horizon (as before) is the old split: the PV change and the cash, the rest in
    ``other``, said as ``fx_core_rest``."""
    o, _ = day(moved, ctx, swap("S1"), "S1", horizon=False)
    pnl = o.pnl
    assert pnl.method != RATES_CORE_METHOD and pnl.carry == pnl.rates == pnl.rolldown == 0.0
    assert pnl.other == pytest.approx(pnl.total) and pnl.extra()["other_reason"]["code"] == "fx_core_rest"


def test_a_horizon_whose_cash_is_not_the_flows_is_not_used(ctx, flat):
    """Core's cash to D and the flows' cash are one number; when they are not, the split is not made."""
    inp, prev_inp = inputs(BD, swap("S1"), "S1"), inputs(PREV, swap("S1"), "S1")
    bp, pbp = pv1.plan(ctx, inp), pv1.plan(flat, prev_inp, horizon=BD)
    p = pbp.direct["S1"]
    p.valuation.extra["horizon"] = {**p.valuation.extra["horizon"], "received_rub": 5e6}
    direct.attach(flat, ctx, bp.direct, inp, pbp.direct, prev_inp.valid_dates)
    assert bp.direct["S1"].pnl.method != RATES_CORE_METHOD


def test_the_last_day_of_a_swap_has_no_rates_part(ctx, moved):
    """A swap that matured on D is gone: the curve's move did not touch it, rates 0, the change of PV and its last cash
    are carry (core's horizon to D has nothing left to value)."""
    o, p = day(moved, ctx, swap("S1", "2026-10-14"), "S1")
    pnl = o.pnl
    assert o.valuation.extra["core"]["model_flags"] == ["settled"]
    assert pnl.rates == 0.0 and pnl.explained == pytest.approx(pnl.total)
    assert pnl.total == pytest.approx(-p.valuation.pv_rub + pnl.received_rub)


def test_a_repo_is_split_too(ctx, flat):
    """A reverse REPO of the day: the carry is its day of interest, the move 0."""
    terms = [repo("R1", "reverse_repo", "2026-10-09", "2026-10-30", fixed_rate_pct="16.1", day_count="ACT/365")]
    o, _ = day(flat, ctx, terms, "R1")
    pnl = o.pnl
    assert o.no_pnl is None and pnl.method == RATES_CORE_METHOD
    assert pnl.carry > 0 and abs(pnl.other) < 0.05 * abs(pnl.carry) + 1e3
    assert pnl.explained == pytest.approx(pnl.total)


def test_an_fx_row_keeps_the_old_split(ctx, flat):
    """A row with an FX delta is not a swap or REPO: it has no horizon and keeps ``fx_core_rest``."""
    from test_deriv_pnl_flows_466 import fx_leg
    terms = [fx_leg("F1", "near", "buy", 1_000_000, 12_500_000, "2026-10-20"),
             fx_leg("F1", "far", "sell", 1_000_000, 12_600_000, "2027-01-20")]
    o, p = day(flat, ctx, terms, "F1")
    assert "horizon" not in p.valuation.extra and o.pnl.method != RATES_CORE_METHOD
    assert o.pnl.carry == 0.0 and o.pnl.extra()["other_reason"]["code"] == "fx_core_rest"


def test_the_valuation_version_says_the_numbers_changed():
    from desk import VALUATION_VERSION
    assert VALUATION_VERSION >= 39  # the split came with 39; a later version keeps it
