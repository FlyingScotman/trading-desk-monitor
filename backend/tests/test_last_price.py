"""TDM#383: a bond without a price of its own today (an intraday cut before its first trade, a day it did not trade)
is valued and carried by core at the fallback ladder's price (``fallback.at_last_price``).

Before: a floater on a priced rung had the ladder's duration estimate, rates risk only to its next coupon on the
discount curve, and no carry (``orchestrator.horizon`` had no ladder). РЖД 001Б-02 on the intraday cut of 29.10.2026
(bid and offer, no trade yet) showed no KBD risk, no OFZ basis and no carry, though the close of 28.10 valued it in
full."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest
from test_perpetuals_never_called import ALFA, KEY, ctx, key_rate_perpetual, reset_perpetual  # noqa: F401

import desk.orchestrator as orch
from desk.orchestrator import Outcome
from desk.valuation.fallback import FallbackContext, at_last_price

RZD = "RU000A0JXQ44"  # a fixed-coupon corporate of the fixture
Q = 1000.0


@pytest.fixture
def untraded(ctx, monkeypatch):  # noqa: F811
    """``untraded(secid, prices, days)``: today's quote of ``secid`` with only ``prices`` (no price basis of the
    exact path; None: no quote today) and its quote of ``days`` days before, with its prices, as the latest earlier
    close."""
    quote = ctx.market.quote

    def make(secid, prices, days=1):
        t = ctx.static.bonds[secid]
        today = None if prices is None else dataclasses.replace(quote(t), prices_pct=prices)
        monkeypatch.setattr(ctx.market, "quote", lambda terms: today if terms.secid == secid else quote(terms))
        earlier = dataclasses.replace(quote(t), trade_date=ctx.business_date - timedelta(days))
        monkeypatch.setattr(FallbackContext, "past_quotes", lambda self, terms: iter([earlier]))
        return quote(t)

    return make


def _exact(ctx, secid):  # noqa: F811
    (o,) = orch.value(ctx, [(secid, Q)])
    assert o.reason is None and o.core_result is not None
    return o.valuation


def test_untraded_kbd_reset_perpetual_is_valued_by_core_at_its_last_price(ctx, untraded, monkeypatch):  # noqa: F811
    monkeypatch.setitem(ctx.static.bonds, ALFA, reset_perpetual(ctx.static.bonds[ALFA]))
    e = _exact(ctx, ALFA)
    close = untraded(ALFA, {"bid": Decimal(90), "offer": Decimal(91)})
    (o,) = orch.value(ctx, [(ALFA, Q)])
    v = o.valuation
    assert o.reason == "no_price" and o.core_result is not None
    assert v.quality == "stale_price" and v.price_basis in close.prices_pct and v.floater.tail is not None
    assert v.assumptions[1:3] == ("last price 2026-10-14, 1 days old (legal_close)", "valued by core at this price")
    assert "perpetual: no call assumed" in v.assumptions
    # the same price and accrued as the close: the exact valuation's value and its risk on both curves
    assert v.pv_rub == pytest.approx(e.pv_rub, rel=1e-9)
    for curve in ("RUB OFZ", "RUB RUONIA"):
        assert v.risk()[curve] == pytest.approx(e.risk()[curve], rel=1e-6, abs=1e-9)
    kbd = v.risk()["RUB OFZ"]
    assert kbd["3Y"] < 0 < kbd["10Y"] and sum(kbd.values()) > 0  # was none: rates only to the next coupon on RUONIA

    (c,) = orch.horizon(ctx, [(ALFA, Q)], ctx.business_date + timedelta(days=30))[0]
    assert c.reason is None and c.carry.method == "core" and c.carry.coupon > 0  # was no_price: no carry
    assert c.detail == "last price 2026-10-14, 1 days old (legal_close); valued by core at this price"


def test_key_rate_floater_with_todays_fallback_price(ctx, monkeypatch):  # noqa: F811
    """A price of today of the ladder's bases only (``market_price2``): the ``market_value`` rung, by core."""
    monkeypatch.setitem(ctx.static.bonds, KEY, key_rate_perpetual(ctx.static.bonds[KEY]))
    e = _exact(ctx, KEY)
    close = ctx.market.quote(ctx.static.bonds[KEY])
    today = dataclasses.replace(close, prices_pct={"market_price2": close.prices_pct["legal_close"]})
    monkeypatch.setattr(ctx.market, "quote", lambda terms: today)
    (o,) = orch.value(ctx, [(KEY, Q)])
    v = o.valuation
    assert v.quality == "market_value" and v.price_basis == "market_price2" and o.core_result is not None
    assert v.assumptions[1:3] == ("price basis market_price2", "valued by core at this price")
    assert v.pv_rub == pytest.approx(e.pv_rub, rel=1e-9)
    assert sum(v.risk()["RUB KEY"].values()) == pytest.approx(sum(e.risk()["RUB KEY"].values()), rel=1e-6)


@pytest.mark.parametrize("today", [{"bid": Decimal(99)}, None], ids=["no_price", "no_quote"])
def test_fixed_bond_is_valued_and_carried_from_cores_value(ctx, untraded, today):  # noqa: F811
    """A fixed coupon had core's risk on its schedule on the ladder already, but the ladder's own accrued: its value was
    not the one core carries from. Now both are core's at the same price. Without a quote today at all its face and
    accrued are the static's (core's)."""
    e = _exact(ctx, RZD)
    untraded(RZD, today, days=3)
    (o,) = orch.value(ctx, [(RZD, Q)])
    v = o.valuation
    assert o.reason == ("no_quote" if today is None else "no_price") and o.core_result is not None
    assert v.quality == "stale_price" and "valued by core at this price" in v.assumptions
    assert v.pv_rub == pytest.approx(e.pv_rub, rel=1e-6) and v.dv01_rub == pytest.approx(e.dv01_rub, rel=1e-6)
    (c,) = orch.horizon(ctx, [(RZD, Q)], ctx.business_date + timedelta(days=30))[0]
    assert c.reason is None and c.carry.method == "core" and c.carry.coupon > 0
    assert c.carry.pv_start == pytest.approx(v.pv_rub, abs=1e-6)  # the forward carry's base check (scenario.carry)


@pytest.mark.parametrize("reason", ["face_mismatch", "floater_formula_incomplete", None])
def test_only_a_missing_price_is_priced_by_the_ladder(ctx, reason):  # noqa: F811
    t = ctx.static.bonds[KEY]
    o = Outcome(KEY, t, ctx.market.quote(t), reason=reason)
    assert at_last_price(ctx, "p", o, Q, FallbackContext.build(ctx)) is None
