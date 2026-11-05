"""TDM#576: a PnL that cannot be told exactly is shown on an assumption, said in ``pnl.extra.assumption``, instead of
«нет PnL». One case per refusal of the bond path (``orchestrator.attach_pnl``); the cases without a safe assumption keep
their ``no_pnl`` (a coupon without an amount: ``received_unknown``). The direct rows (reports, FX, derivatives) are in
test_pnl_r10 and test_off_report_468, the start of the day of a traded position in test_pnl_new_trades."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from test_fallback import FRN, RZD, Q, _outcome, ctx, exact  # noqa: F401 - fixtures
from test_slice_s2 import shifted_prev

from desk.orchestrator import Outcome, attach_pnl
from desk.pnl.reasons import ASSUMPTION_TEXT, NO_PNL_TEXT, assumption_text


@pytest.fixture(scope="module")
def prev(ctx):  # noqa: F811
    return shifted_prev(ctx, ctx.business_date - timedelta(1))


def _run(prev, ctx, o, prev_valuation, cur_valuation=None):  # noqa: F811
    p, c = _outcome(o, "x"), _outcome(o, "x")
    p.valuation, c.valuation = prev_valuation, cur_valuation or o.valuation
    attach_pnl(prev, [p], ctx, [c])
    return c


def _no_prev(prev, ctx, o):   # noqa: F811
    p = Outcome(o.secid, o.terms, o.quote, reason="not_in_static_snapshot", detail="synthetic")
    c = _outcome(o, "x")
    c.valuation = o.valuation
    attach_pnl(prev, [p], ctx, [c])
    return c


def test_no_valuation_on_the_previous_day_is_valued_on_the_market_of_that_day(prev, ctx, exact):  # noqa: F811
    """TDM#576: today's terms on the market of D-1 (the ladder: its quote, a stale one with its date), not today's price."""
    o = exact[RZD]
    c = _no_prev(prev, ctx, o)
    assert c.no_pnl is None and c.pnl.assumption["code"] == "start_price_carried"
    assert c.pnl.assumption["date"] <= prev.business_date.isoformat()
    assert "последняя известная котировка от " in assumption_text(c.pnl.assumption)
    assert c.pnl.other_reason["code"] == "fallback_price"


def test_nothing_on_the_ladder_carries_the_clean_price_with_the_accrual_of_each_date(prev, ctx, exact, monkeypatch):  # noqa: F811
    """No quote, no model: the clean price of D stands for D-1, the accrued interest of its own date. A normal day has the
    accrual of the day in the PnL; a coupon day the coupon once (the accrual drop and the payment cancel)."""
    import desk.orchestrator as orch
    import desk.valuation.fallback as fb
    monkeypatch.setattr(orch, "approximate", lambda *a, **k: None)
    o = exact[RZD]
    v = o.valuation
    acc_prev = v.accrued_rub - 2.0   # an accrual of 2 RUB a bond in the day
    monkeypatch.setattr(fb, "_accrued", lambda t, d, face: (acc_prev, ""))   # only D-1 is asked (the fixture's prev has the date of D)
    monkeypatch.setattr(fb, "received_per_bond", lambda *a: 0.0)
    c = _no_prev(prev, ctx, o)
    assert c.no_pnl is None and c.pnl.assumption == {"code": "start_price_today"}
    assert c.pnl.total == pytest.approx(Q * 2.0, rel=1e-6)   # the accrued income is there
    # a coupon day: the accrued falls by the coupon 30 and 30 is received: the same 2, not 32 and not -28
    monkeypatch.setattr(fb, "_accrued", lambda t, d, face: (v.accrued_rub + 28.0, ""))
    monkeypatch.setattr(fb, "received_per_bond", lambda *a: 30.0)
    c = _no_prev(prev, ctx, o)
    assert c.pnl.total == pytest.approx(Q * 2.0, rel=1e-6) and c.pnl.received_rub == pytest.approx(Q * 30.0)


def test_quantity_change_without_trades_is_measured_on_the_quantity_of_the_start(prev, ctx, exact):  # noqa: F811
    v = exact[RZD].valuation
    d0 = ctx.business_date - timedelta(1)
    c = _run(prev, ctx, exact[RZD], replace(v, valuation_date=d0, dirty_rub=v.dirty_rub - 1.0),
             replace(v, quantity=v.quantity + 200))
    assert c.no_pnl is None and c.pnl.quantity == v.quantity   # the start of the day's quantity, not the changed one
    assert c.pnl.total == pytest.approx(v.quantity * 1.0 + c.pnl.received_rub)
    assert c.pnl.assumption["code"] == "quantity_changed" and "→" in c.pnl.assumption["detail"]


def test_a_quote_not_updated_since_the_previous_day_carries_its_clean_price(prev, ctx, exact):  # noqa: F811
    v = exact[RZD].valuation
    c = _run(prev, ctx, exact[RZD], replace(v, dirty_rub=v.dirty_rub - 0.5))   # the same valuation date on both days
    assert c.no_pnl is None and c.pnl.assumption == {"code": "price_carried", "date": v.valuation_date.isoformat()}
    # the clean price is the same on both days: what is left is the accrual of the day and the payments of (D-1, D]
    assert c.pnl.v0 != pytest.approx(Q * (v.dirty_rub - 0.5))   # not the stale dirty price: the clean price, the accrual of D-1
    assert c.pnl.v0 == pytest.approx(Q * v.dirty_rub, rel=0.01)
    assert c.pnl.total == pytest.approx(Q * v.dirty_rub - c.pnl.v0 + c.pnl.received_rub)


def test_other_refusals_of_the_exact_steps_give_the_simple_pnl_on_an_assumption(prev, ctx, exact):  # noqa: F811
    v = exact[RZD].valuation
    d0 = ctx.business_date - timedelta(1)
    c = _run(prev, ctx, exact[RZD], replace(v, valuation_date=d0, dirty_rub=v.dirty_rub - 1.0, spread_kind="other"))
    assert c.no_pnl is None and c.pnl.method == "desk-flash-simple-v1"
    assert c.pnl.assumption == {"code": "simple_fallback", "detail": NO_PNL_TEXT["spread_kind_changed"]}
    assert c.pnl.total == pytest.approx(Q * 1.0 + c.pnl.received_rub)


def test_a_coupon_without_an_amount_has_no_assumption_and_keeps_its_reason(prev, ctx, exact, monkeypatch):  # noqa: F811
    import desk.valuation.fallback as fb
    monkeypatch.setattr(fb, "received_per_bond", lambda *a: None)
    v = exact[FRN].valuation
    d0 = ctx.business_date - timedelta(1)
    c = _run(prev, ctx, exact[FRN], replace(v, valuation_date=d0, spread_kind="other"))
    assert c.pnl is None and c.no_pnl.reason == "received_unknown"


def test_every_assumption_code_has_a_text_and_unknown_ones_are_refused():
    from desk.pnl.reasons import assumption
    assert all(assumption_text({"code": k, "detail": "x", "date": "2026-10-14"}) for k in ASSUMPTION_TEXT)
    assert assumption_text(None) is None and assumption_text({"code": "gone"}) is None
    with pytest.raises(ValueError):
        assumption("gone")
