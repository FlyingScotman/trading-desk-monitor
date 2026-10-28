"""TDM#35, TDM#383: perpetuals valued never called (core 0.41): a fixed coupon, a key-rate floater and a coupon reset
every five years to the 5-year KBD, their risk, their carry and the flash PnL with the tail.

The golden fixtures of 15.10.2026 hold two fixed-coupon perpetuals (RU000A1034P7, RU000A100GW5) and no floating one,
so the floating ones are those bonds' terms reshaped as the hub lists РЖД 001Б-02 (28.10.2026) and a key-rate one."""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest
from test_slice_s2 import prev_outcome, shifted_prev

from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import Outcome, attach_pnl, prepare
from desk.valuation.bonds import (
    BondValuation,
    Rejected,
    bond_request,
    fixed_schedule,
    horizon_bonds,
    kbd_reset,
    value_bonds,
)

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
ALFA, KEY = "RU000A100GW5", "RU000A109XR1"
NEXT_RESET = date(2029, 9, 13)  # ALFA's last listed coupon


FORMULA = {"secid": ALFA, "benchmark": "ZCYC", "benchmark_raw": "ZR_YLD_CRV", "benchmark_tenor_years": None,
           "fixing_offset": None, "fixing_offset_unit": None, "spread_bp": "200.0", "status": "inferred"}
CALLS = (date(2024, 9, 12), NEXT_RESET, date(2034, 9, 14))


@pytest.fixture(scope="module")
def ctx():
    """The fixture's static with ALFA's calls and a KBD formula without a tenor, as the hub lists РЖД 001Б-02: the run
    then keeps the KBD history at the reset cycle (``rates.kbd_tenors``). ALFA itself stays a fixed coupon."""
    snaps = SnapshotDir(FIX)
    get, cache = snaps.get, {}
    snaps.get = lambda i: cache.setdefault(i, get(i))  # type: ignore[method-assign]  # one static object to edit
    static = snaps.get(snaps.market("2026-10-15", "eod_final").static_snapshot_id)
    static.data["floater_formulas"].append(FORMULA)
    static.data["offers"].extend({"secid": ALFA, "offer_date": d.isoformat(), "type": "call",
                                  "status": "past" if d < date(2026, 10, 15) else "scheduled", "price_pct": "100"}
                                 for d in CALLS)
    return prepare(snaps, "2026-10-15")


def reset_perpetual(t):
    """ALFA as a KBD-reset perpetual: calls five years apart, the coupons from the last call to the next one set at
    15 % (MOEX lists the coming ones ``projected_by_moex``), the hub's ``ZCYC`` formula without a tenor or fixing."""
    coupons = tuple(dataclasses.replace(c, status="projected_by_moex") if c.pay_date > date(2026, 10, 15) else c
                    for c in t.coupons)
    return dataclasses.replace(t, coupon_type="floating", coupons=coupons)


def key_rate_perpetual(t):
    """The fixture's key-rate floater without a maturity or redemptions: a key-rate perpetual."""
    return dataclasses.replace(t, is_perpetual=True, maturity_date=None, redemptions=())


def value_terms(ctx, t, q=1.0) -> Outcome:
    quote = ctx.market.quote(t)
    valued, results = value_bonds(ctx.risk_market, [bond_request("x", t, quote, ctx.business_date, q)],
                                  ofz_basis=ctx.ofz_basis, quote_curve_risk=ctx.quote_curve_risk)
    v = valued["x"]
    assert isinstance(v, BondValuation), v
    return Outcome(t.secid, t, quote, valuation=v, core_result=results[0] if results else None)


def test_kbd_reset_is_read_from_the_calls_and_the_coupons(ctx):
    t = reset_perpetual(ctx.static.bonds[ALFA])
    years, resets = kbd_reset(t)
    assert years == 5 and resets[-1] == NEXT_RESET
    # a formula with its own tenor is a floater fixed per period; varying coupons in the cycle are not a reset
    assert kbd_reset(dataclasses.replace(t, floater_formula={**t.floater_formula, "benchmark_tenor_years": "5"})) is None
    moved = tuple(dataclasses.replace(c, rate_pct=Decimal(16)) if c.no == t.coupons[-2].no else c for c in t.coupons)
    assert kbd_reset(dataclasses.replace(t, coupons=moved)) is None
    assert kbd_reset(dataclasses.replace(t, is_perpetual=False)) is None
    # without a reset the hub's formula stays incomplete, as before
    with pytest.raises(Rejected) as e:
        fixed_schedule(dataclasses.replace(t, coupons=moved), ctx.business_date)
    assert e.value.reason == "floater_formula_incomplete"


def test_kbd_reset_perpetual_has_kbd_rate_risk_beyond_the_reset(ctx):
    """The owner (28.10.2026): a coupon reset on the KBD has rate risk «of interesting shapes», not none. The coupons
    to the next reset are cash; each later cycle is fixed once, at its reset, to the forward 5-year KBD: short the KBD
    at the reset, long beyond it; discounted on RUONIA + Z, the spread risk for ever."""
    o = value_terms(ctx, reset_perpetual(ctx.static.bonds[ALFA]))
    v = o.valuation
    assert v.horizon_kind == "perpetual" and v.perpetual_tail is None and v.floater.tail is not None
    assert v.assumptions[:2] == ("perpetual: no call assumed",
                                 "coupons to the reset 2029-09-13 at 15.0 % (set at the last reset, MOEX)")
    assert "reset every 5 years at the calls" in v.assumptions[2]
    # the leg core continues holds the next cycle's first coupon anchored on its reset (review of #412: without it
    # core continued a floater fixed every period), and every later cycle is anchored once, 5 years apart
    periods = v.floater.instrument.coupon_leg.periods
    anchors = sorted({p.fixing_anchor for p in periods if p.fixing_anchor is not None})
    assert anchors[0] == NEXT_RESET and len(anchors) >= 7
    assert all(abs((b - a).days - 5 * 365.25) < 92 for a, b in pairwise(anchors))
    assert all(p.fixing_anchor is None for p in periods if p.payment_date <= NEXT_RESET)  # the running cycle: cash
    kbd = v.risk()["RUB OFZ"]
    assert kbd["3Y"] < 0 < kbd["10Y"] and sum(kbd.values()) > 0
    assert sum(v.risk()["RUB RUONIA"].values()) < 0
    assert v.modified_duration > 3.5 and -v.spread01_rub / v.pv_rub * 1e4 > 3.5  # CS01 of the endless stream


def test_floating_perpetuals_have_carry(ctx):
    terms = [reset_perpetual(ctx.static.bonds[ALFA]), key_rate_perpetual(ctx.static.bonds[KEY]), ctx.static.bonds[ALFA]]
    reqs = [bond_request(f"p{i}", t, ctx.market.quote(t), ctx.business_date, 1.0) for i, t in enumerate(terms)]
    carried, _ = horizon_bonds(ctx.risk_market, reqs, ctx.business_date + timedelta(days=30), ofz_basis=ctx.ofz_basis,
                               quote_curve_risk=ctx.quote_curve_risk)
    for k, c in carried.items():
        assert not isinstance(c, Rejected), (k, c)
        assert c.coupon > 0 and c.method == "core"  # core 0.41 carries a never-called perpetual (was unsupported)


@pytest.mark.parametrize("secid,make", [(ALFA, lambda t: t), (ALFA, reset_perpetual), (KEY, key_rate_perpetual)],
                         ids=["fixed", "kbd_reset", "key_rate"])
def test_flash_pnl_reprices_the_tail(ctx, secid, make):
    """The flash PnL reprices the perpetual with its tail (the fixed one's ``PerpetualTail``, a floating one's continued
    leg and projected tail, ``FloaterModel.tail``): repriced at the D spread on the D curves it is the D price, so
    nothing is left in ``other``. Without the tail the flows end at the last listed coupon: ``other`` would hold the
    tail's value (a third to a half of the price) and ``rates`` of a fixed one about a third of its DV01's."""
    o = value_terms(ctx, make(ctx.static.bonds[secid]))
    prev = shifted_prev(ctx, ctx.business_date - timedelta(days=1))
    p = prev_outcome(prev, o)
    attach_pnl(prev, [p], ctx, [o])
    f = o.pnl
    assert f is not None, o.no_pnl
    assert f.total == pytest.approx(f.carry + f.rolldown + f.rates + f.spread + f.other)
    assert abs(f.other) < 1e-6 * o.valuation.pv_rub and f.carry > 0
    if make is not reset_perpetual:  # the KBD's par move is not one number on RUONIA + basis: no linear check there
        assert f.rates == pytest.approx(f.rates_linear, rel=0.05)


def test_key_rate_perpetual_projects_on_the_key_rate(ctx):
    o = value_terms(ctx, key_rate_perpetual(ctx.static.bonds[KEY]))
    v = o.valuation
    assert v.horizon_kind == "perpetual" and v.floater.tail is not None
    assert sum(v.risk()["RUB KEY"].values()) > 0 and v.modified_duration > 3
