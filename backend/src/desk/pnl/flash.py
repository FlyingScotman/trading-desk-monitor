"""Day-over-day flash PnL of a bond position by exact repricing with core (slices S2, S4).

The position of ``D-1`` (its cashflows or coupon model, quantity and calibrated Z-spread ``z0``) is repriced
step by step until it reaches the market of ``D``. Each step changes one thing, so the components add up to
the total by construction. "Curves" are the curves the bond depends on: its discount curve (RUONIA OIS, the KBD
on a date without RUONIA: ``BondValuation.discount_curve_id``) and, for a floater, its projection curve (RUONIA
OIS, then the same curve, or the key-rate projection):

==========  =================================================================  ==============
Step        Value of the D-1 position                                          Component
==========  =================================================================  ==============
V0          D-1 curves, D-1 valuation date, ``z0`` (= the D-1 dirty price)
V_carry     D-1 curves, **D valuation date**, ``z0``, plus cashflows received   ``carry``
V_roll      D-1 curves rolled to D (same rate per tenor), ``z0``               ``rolldown``
V_rates     **D curves**, ``z0``                                                ``rates``
V_spread    D curves, **z1** (the Z-spread calibrated on D)                     ``spread``
V1          the D dirty price                                                   ``other``
==========  =================================================================  ==============

- A fixed bond reprices its D-1 cashflows. A floater reprojects its coupons on each step's curves (core
  ``BondRiskEngine`` with the D-1 instrument's floating leg, ``BondValuation.floater``: the fixings known on D-1
  for the D-1 steps, those known on D for the D steps), so a projection-curve move is in ``rates`` as its DV01
  says, not in ``other``.
- ``carry`` is the time value on unchanged forwards and spread: core prices at the valuation
  date (the quote's trade date, see ``valuation.bonds``), so moving it on the D-1 curves
  realizes the curves' forwards. Coupons and redemptions paid in (D-1, D] count here.
- ``rolldown`` is the difference between "rates per tenor unchanged" and "forwards realized": the KBD's ZCYC
  parameters dated D, any other curve rolled by core ``roll_curve`` (its nodes moved by the days in between).
  A floater's fixings published between D-1 and D enter here.
- ``rates`` and ``spread`` are full repricings, so their second-order effects are inside
  them; ``convexity`` stays 0 on purpose. ``rates_linear`` (Σ over the curves of the D-1 bucket DV01 x that
  curve's continuous zero-rate move per pillar) is ``rates`` to first order.
- ``other`` is what the D-1 position does not explain at the D market: a changed schedule
  (an offer passed, a new coupon fixed) or a quote whose price and accrued disagree.

Check: ``carry + rolldown + rates + spread + other == q * (dirty_D - dirty_D-1) + received``,
where both dirty prices come from the published valuations, not from the steps.

A position that is not priced on either date gets no PnL and a reason; it is never zeroed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from rub_fixed_income import (
    RISK_BUCKETS,
    RISK_PILLARS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    CoreRejection,
    DiscountCurve,
    MarketSnapshot,
    ShockedCurveCache,
    ZcycParameters,
    kbd_discount_curve,
    kbd_zero_rate_bp,
    roll_curve,
)

from ..marketdata.adapter import KBD_CURVE_ID
from ..valuation.bonds import RISK_CURVES, SPREAD_CONVENTION, BondValuation, FloaterModel

METHOD = "desk-flash-exact-v2"
"""v2: on the bond's own curves (the RUONIA discount curve, a floater's projection curve reprojected); v1 repriced
fixed flows on the KBD only."""
IDENTITY_TOLERANCE_RUB = 0.01
PUBLISHED_TO_CORE = {v: k for k, v in RISK_CURVES.items()}


class NoPnl(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _zero(c: DiscountCurve, t: float) -> float:
    d = c.curve_date + timedelta(days=max(1, round(t * 365.25)))
    return -math.log(c.discount_factor(d)) / c.convention.time_day_count.year_fraction(c.curve_date, d)


@dataclass(frozen=True)
class FlashCurves:
    """The curves of the steps by core curve ID: D-1, D-1 rolled to D, and D. The KBD under ``KBD_CURVE_ID``
    (its roll: the D-1 ZCYC parameters dated D); every other built curve of both markets (RUONIA OIS, the key-rate
    projection) rolled by core ``roll_curve``. ``prev_market``/``cur_market`` give a floater the fixings known on
    each date."""

    prev: ZcycParameters
    cur: ZcycParameters
    prev_curves: dict[str, DiscountCurve]
    roll_curves: dict[str, DiscountCurve]
    cur_curves: dict[str, DiscountCurve]
    prev_market: MarketSnapshot | None = None
    cur_market: MarketSnapshot | None = None
    _moves: dict[str, dict[str, float]] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def build(cls, prev: ZcycParameters, cur: ZcycParameters,
              prev_curve: DiscountCurve | None = None, cur_curve: DiscountCurve | None = None, *,
              prev_market: MarketSnapshot | None = None, cur_market: MarketSnapshot | None = None) -> FlashCurves:
        """The KBD is core's (``kbd_discount_curve``), as in the valuations; the other curves are the markets'."""
        if prev.trade_date >= cur.trade_date:
            raise ValueError(f"previous curve {prev.trade_date} is not before {cur.trade_date}")
        p = {KBD_CURVE_ID: prev_curve or kbd_discount_curve(prev)}
        r = {KBD_CURVE_ID: kbd_discount_curve(prev, cur.trade_date)}
        c = {KBD_CURVE_ID: cur_curve or kbd_discount_curve(cur)}
        if prev_market is not None and cur_market is not None:
            now = {e.curve_id: e.curve for e in cur_market.curves if isinstance(e.curve, DiscountCurve)}
            for e in prev_market.curves:
                if isinstance(e.curve, DiscountCurve) and e.curve_id in now and e.curve_id != KBD_CURVE_ID:
                    p[e.curve_id], c[e.curve_id] = e.curve, now[e.curve_id]
                    r[e.curve_id] = roll_curve(e.curve, now[e.curve_id].curve_date)
        return cls(prev, cur, p, r, c, prev_market, cur_market)

    @property
    def prev_curve(self) -> DiscountCurve:
        return self.prev_curves[KBD_CURVE_ID]

    @property
    def roll_curve(self) -> DiscountCurve:
        return self.roll_curves[KBD_CURVE_ID]

    @property
    def cur_curve(self) -> DiscountCurve:
        return self.cur_curves[KBD_CURVE_ID]

    def zero_move_bp(self, curve_id: str = KBD_CURVE_ID) -> dict[str, float]:
        """Continuously compounded zero-rate move per risk pillar at the same tenor, of ``curve_id`` (empty when the
        curve is not on both dates). The KBD by its parameters; another curve at the pillar's date counted in days
        (``round(t x 365.25)``) on its own time basis, as its bucket shifts read it."""
        if curve_id not in self._moves:
            if curve_id == KBD_CURVE_ID:
                out = {b: kbd_zero_rate_bp(self.cur, t) - kbd_zero_rate_bp(self.prev, t)
                       for b, t in zip(RISK_BUCKETS, RISK_PILLARS)}
            elif curve_id in self.cur_curves:
                out = {b: (_zero(self.cur_curves[curve_id], t) - _zero(self.prev_curves[curve_id], t)) * 1e4
                       for b, t in zip(RISK_BUCKETS, RISK_PILLARS)}
            else:
                out = {}
            self._moves[curve_id] = out
        return self._moves[curve_id]

    def market(self, prev: bool) -> MarketSnapshot:
        m = self.prev_market if prev else self.cur_market
        if m is None:
            raise NoPnl("no_market", "a floater needs the fixings of both dates")
        return m


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
    """Σ over the curves of the D-1 bucket DV01 × that curve's zero move per pillar: the first-order estimate of
    ``rates``."""
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


def _pv_floater(fl: FloaterModel, settlement: date, market: MarketSnapshot, curve: DiscountCurve,
                projection: DiscountCurve | None, spread_bp: float, spread_kind: str, quantity: float,
                cache: ShockedCurveCache) -> float:
    """Position PV of a floater after ``settlement``: its principal, and its coupons projected on ``projection``
    (None: ``curve`` projects and discounts) with the fixings of ``market``, as core values it."""
    inst = fl.instrument
    principal = tuple(BondCashflow(f.payment_date, f.amount) for f in inst.cashflows(
        after=settlement, horizon=fl.horizon_date, offer=fl.offer))
    if not any(f.amount > 0 for f in principal):
        return 0.0
    leg = inst.floating_leg(market, horizon=fl.horizon_date, strike_guard_bp=fl.guard_bp)
    engine = BondRiskEngine(
        BondRiskRequest(cashflows=principal, settlement_date=settlement, curve=curve,
                        spread_convention=SPREAD_CONVENTION, spread_kind=spread_kind,  # type: ignore[arg-type]
                        spread_bp=spread_bp, quantity=quantity, floating_leg=leg, projection_curve=projection),
        curve_cache=cache,
    )
    return engine.base_price_rub * quantity


def explain(prev: BondValuation, cur: BondValuation, curves: FlashCurves,
            cache: ShockedCurveCache | None = None) -> FlashPnl:
    """Flash PnL from ``prev`` (valued on ``curves.prev_curves``) to ``cur`` (on ``curves.cur_curves``), both
    discounted on the same curve (``discount_curve_id``; ``orchestrator.attach_pnl`` gives a change of curve the
    simple PnL)."""
    if prev.secid != cur.secid:
        raise ValueError(f"{prev.secid} vs {cur.secid}")
    if abs(prev.quantity - cur.quantity) > 1e-9:
        raise NoPnl("quantity_changed", f"{prev.quantity:g} -> {cur.quantity:g}: trades of the day are not in S2")
    if prev.spread_kind != cur.spread_kind:
        raise NoPnl("spread_kind_changed", f"{prev.spread_kind} -> {cur.spread_kind}")
    if cur.valuation_date <= prev.valuation_date:
        raise NoPnl("stale_quote", f"trade date {cur.valuation_date}, previous {prev.valuation_date}")
    disc = prev.discount_curve_id or KBD_CURVE_ID
    if (cur.discount_curve_id or KBD_CURVE_ID) != disc:
        raise NoPnl("discount_curve_changed", f"{prev.spread_curve} -> {cur.spread_curve}")
    fl = prev.floater
    proj = getattr(fl.instrument.coupon_leg, "projection_curve_id", None) if fl else None
    two = proj is not None and proj != disc
    if missing := [c for c in (disc, *((proj,) if two else ())) if c not in curves.cur_curves]:
        raise NoPnl("curve_missing", f"{', '.join(missing)}: not built on both dates")
    cache = cache or ShockedCurveCache()
    q, kind, flows = prev.quantity, prev.spread_kind, prev.cashflows
    s0, s1 = prev.valuation_date, cur.valuation_date

    def pv(s: date, step: dict[str, DiscountCurve], z: float, on_d: bool) -> float:
        if fl is None:
            return _pv(flows, s, step[disc], z, kind, q, cache)
        return _pv_floater(fl, s, curves.market(not on_d), step[disc], step[proj] if two else None,  # type: ignore[index]
                           z, kind, q, cache)

    try:
        v0 = pv(s0, curves.prev_curves, prev.z_spread_bp, False)
        received = q * sum(f.amount for f in flows if s0 < f.payment_date <= s1)
        v_carry = pv(s1, curves.prev_curves, prev.z_spread_bp, False) + received
        v_roll = pv(s1, curves.roll_curves, prev.z_spread_bp, True) + received
        v_rates = pv(s1, curves.cur_curves, prev.z_spread_bp, True) + received
        v_spread = pv(s1, curves.cur_curves, cur.z_spread_bp, True) + received
    except (ValueError, RuntimeError, CoreRejection) as e:
        raise NoPnl("core_error", str(e)) from e
    linear = 0.0
    for c, bs in prev.risk().items():
        move = curves.zero_move_bp(PUBLISHED_TO_CORE.get(c, c))
        linear += sum(x * move.get(b, 0.0) for b, x in bs.items())
    v1 = q * cur.dirty_rub + received
    total = q * (cur.dirty_rub - prev.dirty_rub) + received
    out = FlashPnl(
        secid=prev.secid, quantity=q, prev_date=curves.prev.trade_date, as_of=curves.cur.trade_date,
        prev_valuation_date=s0, valuation_date=s1, z0_bp=prev.z_spread_bp, z1_bp=cur.z_spread_bp,
        v0=v0, v_carry=v_carry, v_roll=v_roll, v_rates=v_rates, v_spread=v_spread, v1=v1,
        received_rub=received,
        carry=v_carry - v0, rolldown=v_roll - v_carry, rates=v_rates - v_roll, spread=v_spread - v_rates,
        other=v1 - v_spread, total=total, identity_error=0.0, rates_linear=linear,
        price_basis=(prev.price_basis, cur.price_basis),
    )
    err = out.explained - total
    object.__setattr__(out, "identity_error", err)
    if abs(err) > IDENTITY_TOLERANCE_RUB + 1e-9 * abs(prev.pv_rub):
        raise NoPnl("identity_failed", f"components {out.explained:.2f} vs total {total:.2f} (error {err:.2f})")
    return out
