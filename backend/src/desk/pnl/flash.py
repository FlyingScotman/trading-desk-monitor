"""Day-over-day flash PnL of a bond position by exact repricing with core (slices S2, S4).

The position of ``D-1`` (its cashflows, quantity and calibrated Z-spread ``z0``) is repriced
step by step until it reaches the market of ``D``. Each step changes one thing, so the
components add up to the total by construction:

==========  =================================================================  ==============
Step        Value of the D-1 cashflows                                          Component
==========  =================================================================  ==============
V0          D-1 KBD, D-1 valuation date, ``z0`` (= the D-1 dirty price)
V_carry     D-1 KBD, **D valuation date**, ``z0``, plus cashflows received      ``carry``
V_roll      D-1 ZCYC parameters dated D (same yield per tenor), ``z0``          ``rolldown``
V_rates     **D KBD**, ``z0``                                                    ``rates``
V_spread    D KBD, **z1** (the Z-spread calibrated on D)                        ``spread``
V1          the D dirty price                                                   ``other``
==========  =================================================================  ==============

- ``carry`` is the time value on unchanged forwards and spread: core prices at the valuation
  date (the quote's trade date, see ``valuation.bonds``), so moving it on the D-1 curve
  realizes the curve's forwards. Coupons and redemptions paid in (D-1, D] count here.
- ``rolldown`` is the difference between "yields per tenor unchanged" and "forwards realized".
- ``rates`` and ``spread`` are full repricings, so their second-order effects are inside
  them; ``convexity`` stays 0 on purpose.
- ``other`` is what the D-1 cashflows do not explain at the D market: a changed schedule
  (an offer passed, a new coupon fixed) or a quote whose price and accrued disagree.

Check: ``carry + rolldown + rates + spread + other == q * (dirty_D - dirty_D-1) + received``,
where both dirty prices come from the published valuations, not from the steps.

A position that is not priced on either date gets no PnL and a reason; it is never zeroed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from rub_fixed_income import (
    RISK_BUCKETS,
    RISK_PILLARS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    DiscountCurve,
    ShockedCurveCache,
    ZcycParameters,
    kbd_discount_curve,
    kbd_zero_rate_bp,
)

from ..valuation.bonds import SPREAD_CONVENTION, BondValuation

METHOD = "desk-flash-exact-v1"
IDENTITY_TOLERANCE_RUB = 0.01


@dataclass(frozen=True)
class FlashCurves:
    """The three curves of the steps: D-1, D-1 parameters dated D, and D."""

    prev: ZcycParameters
    cur: ZcycParameters
    prev_curve: DiscountCurve
    roll_curve: DiscountCurve
    cur_curve: DiscountCurve

    @classmethod
    def build(cls, prev: ZcycParameters, cur: ZcycParameters,
              prev_curve: DiscountCurve | None = None, cur_curve: DiscountCurve | None = None) -> FlashCurves:
        """The curves are core's KBD (``kbd_discount_curve``), as in the valuations."""
        if prev.trade_date >= cur.trade_date:
            raise ValueError(f"previous curve {prev.trade_date} is not before {cur.trade_date}")
        return cls(prev, cur, prev_curve or kbd_discount_curve(prev), kbd_discount_curve(prev, cur.trade_date),
                   cur_curve or kbd_discount_curve(cur))

    def zero_move_bp(self) -> dict[str, float]:
        """Continuously compounded zero-rate move per risk pillar at the same tenor."""
        return {b: kbd_zero_rate_bp(self.cur, t) - kbd_zero_rate_bp(self.prev, t)
                for b, t in zip(RISK_BUCKETS, RISK_PILLARS)}


@dataclass(frozen=True)
class FlashPnl:
    secid: str
    quantity: float
    prev_date: date
    as_of: date
    prev_valuation_date: date
    valuation_date: date
    z0_bp: float
    z1_bp: float
    v0: float
    v_carry: float
    v_roll: float
    v_rates: float
    v_spread: float
    v1: float
    received_rub: float
    carry: float
    rolldown: float
    rates: float
    spread: float
    other: float
    total: float
    """q * (dirty_D - dirty_D-1) + received, from the two valuations."""
    identity_error: float
    rates_linear: float
    """Σ D-1 bucket DV01 × zero move per pillar: the first-order estimate of ``rates``."""
    price_basis: tuple[str, str]
    fx_gross: float = 0.0
    """The FX revaluation of a foreign-face bond (``pnl.direct.split_fx``), taken out of ``other``; 0 for a RUB bond."""

    @property
    def explained(self) -> float:
        return self.carry + self.rolldown + self.rates + self.spread + self.other + self.fx_gross

    def as_tdm(self) -> dict[str, float]:
        """The ``pnl`` dict of a ``tdm`` position (writer PNL_COLS)."""
        return {"carry": self.carry, "rolldown": self.rolldown, "rates": self.rates, "spread": self.spread,
                "convexity": 0.0, "other": self.other, "fx_gross": self.fx_gross, "fx_funding": 0.0, "new_trades": 0.0}

    def extra(self) -> dict[str, Any]:
        return {
            "method": METHOD, "prev_date": self.prev_date.isoformat(),
            "prev_valuation_date": self.prev_valuation_date.isoformat(), "z0_bp": round(self.z0_bp, 4),
            "z1_bp": round(self.z1_bp, 4), "received_rub": self.received_rub, "total": self.total,
            "identity_error": self.identity_error, "rates_linear": self.rates_linear,
            "prev_price_basis": self.price_basis[0],
        }


class NoPnl(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _pv(flows: tuple[BondCashflow, ...], settlement: date, curve: DiscountCurve, spread_bp: float,
        spread_kind: str, quantity: float, cache: ShockedCurveCache) -> float:
    """Position PV of the flows after ``settlement``; 0 when none is left (redeemed in between)."""
    if not any(f.payment_date > settlement and f.amount > 0 for f in flows):
        return 0.0
    engine = BondRiskEngine(
        BondRiskRequest(cashflows=flows, settlement_date=settlement, curve=curve,
                        spread_convention=SPREAD_CONVENTION, spread_kind=spread_kind,  # type: ignore[arg-type]
                        spread_bp=spread_bp, quantity=quantity),
        curve_cache=cache,
    )
    return engine.base_price_rub * quantity


def explain(prev: BondValuation, cur: BondValuation, curves: FlashCurves,
            cache: ShockedCurveCache | None = None) -> FlashPnl:
    """Flash PnL from ``prev`` (valued on ``curves.prev_curve``) to ``cur`` (on ``curves.cur_curve``)."""
    if prev.secid != cur.secid:
        raise ValueError(f"{prev.secid} vs {cur.secid}")
    if abs(prev.quantity - cur.quantity) > 1e-9:
        raise NoPnl("quantity_changed", f"{prev.quantity:g} -> {cur.quantity:g}: trades of the day are not in S2")
    if prev.spread_kind != cur.spread_kind:
        raise NoPnl("spread_kind_changed", f"{prev.spread_kind} -> {cur.spread_kind}")
    if cur.valuation_date <= prev.valuation_date:
        raise NoPnl("stale_quote", f"trade date {cur.valuation_date}, previous {prev.valuation_date}")
    cache = cache or ShockedCurveCache()
    q, kind, flows = prev.quantity, prev.spread_kind, prev.cashflows
    s0, s1 = prev.valuation_date, cur.valuation_date
    try:
        v0 = _pv(flows, s0, curves.prev_curve, prev.z_spread_bp, kind, q, cache)
        received = q * sum(f.amount for f in flows if s0 < f.payment_date <= s1)
        v_carry = _pv(flows, s1, curves.prev_curve, prev.z_spread_bp, kind, q, cache) + received
        v_roll = _pv(flows, s1, curves.roll_curve, prev.z_spread_bp, kind, q, cache) + received
        v_rates = _pv(flows, s1, curves.cur_curve, prev.z_spread_bp, kind, q, cache) + received
        v_spread = _pv(flows, s1, curves.cur_curve, cur.z_spread_bp, kind, q, cache) + received
    except (ValueError, RuntimeError) as e:
        raise NoPnl("core_error", str(e)) from e
    v1 = q * cur.dirty_rub + received
    total = q * (cur.dirty_rub - prev.dirty_rub) + received
    out = FlashPnl(
        secid=prev.secid, quantity=q, prev_date=curves.prev.trade_date, as_of=curves.cur.trade_date,
        prev_valuation_date=s0, valuation_date=s1, z0_bp=prev.z_spread_bp, z1_bp=cur.z_spread_bp,
        v0=v0, v_carry=v_carry, v_roll=v_roll, v_rates=v_rates, v_spread=v_spread, v1=v1,
        received_rub=received,
        carry=v_carry - v0, rolldown=v_roll - v_carry, rates=v_rates - v_roll, spread=v_spread - v_rates,
        other=v1 - v_spread, total=total, identity_error=0.0,
        rates_linear=sum(prev.buckets[b] * m for b, m in curves.zero_move_bp().items()),
        price_basis=(prev.price_basis, cur.price_basis),
    )
    err = out.explained - total
    object.__setattr__(out, "identity_error", err)
    if abs(err) > IDENTITY_TOLERANCE_RUB + 1e-9 * abs(prev.pv_rub):
        raise NoPnl("identity_failed", f"components {out.explained:.2f} vs total {total:.2f} (error {err:.2f})")
    return out
