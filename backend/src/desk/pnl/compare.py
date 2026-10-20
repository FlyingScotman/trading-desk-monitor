"""The flash PnL both ways: ``desk.pnl.flash`` vs core ``explain_horizon_pnl`` (slice S4 step 4).

Core is run per bond on the D-1 instrument (the backend's ``core_instrument`` after the D-1
trade date), from the D-1 discount curve to the D one (``EodContext.discount``: RUONIA OIS, the KBD on a date
without it), with both spreads calibrated by core to the two published dirty prices; its per-bond amounts are
scaled by the quantity here. The table says "KBD" for that curve; on RUONIA both sides roll it by ``roll_curve``.

Definitions side by side (``q`` and ``received`` omitted; ``z0``/``z1`` flash's calibrated
spreads, ``s0``/``s1`` core's; rolled = D-1 curve with its shape in calendar days moved to D):

=========  ===========================================  ===========================================
Component  flash                                        core
=========  ===========================================  ===========================================
carry      V(D-1 KBD, D, z0) + recv - V0                V(D-1 KBD, D, s0) + recv - pv_start: same
rolldown   V(D-1 ZCYC params dated D, z0) - V_carry     V(roll_curve(D-1), s0) - pv_forward: same
           (``kbd_discount_curve(prev, D)``: the D-1    (``roll_curve`` shifts the D-1 nodes by the
           node DFs on nodes shifted by D - (D-1))      same calendar days: identical nodes)
rates      V(D KBD, z0) - V_roll                        V(D KBD, s0) - V(rolled, s0): same
spread     V(D KBD, z1) - V(D KBD, z0)                  V(rolled, s1) - V(rolled, s0)
other /    q * dirty_D - V(D KBD, z1): D-1 flows vs     total - the four above: the cross term
residual   the D schedule (0 if unchanged)              [V(end, s1) - V(end, s0)] - spread_core
=========  ===========================================  ===========================================

So ``spread_flash == spread_core + residual_core`` when the schedule did not change (then
``s1 == z1`` and ``other == 0``); core's ``s1`` is calibrated on the D-1 flows, so a changed
schedule moves into its spread and residual instead of an ``other``.

Horizons: core's explain takes no exercise assumption and never assumes a call
(``Instrument.horizon``), so a bond the backend values to a call is given to core with that call
as a confirmed put: the same flows, priced to the same date.
"""

from __future__ import annotations

from dataclasses import dataclass

from rub_fixed_income import CoreRejection, explain_horizon_pnl

from ..marketdata.adapter import core_instrument
from ..orchestrator import EodContext, Outcome
from ..valuation.bonds import SPREAD_CONVENTION, Rejected, fixed_schedule

COMPONENTS = ("carry", "rolldown", "rates", "spread", "other")


@dataclass(frozen=True)
class Row:
    secid: str
    quantity: float
    received: float
    identity_total: float
    """q * (dirty_D - dirty_D-1) + received, from the two published valuations."""
    flash: dict[str, float] | None
    """carry, rolldown, rates, spread, other (RUB, position)."""
    core: dict[str, float] | None
    """carry, rolldown, rates, spread, other (= core residual), total (RUB, position)."""
    z: tuple[float, float, float, float] | None = None
    """z0, z1 (flash) and s0, s1 (core), bp."""
    reason: str = ""

    @property
    def delta(self) -> dict[str, float]:
        """core - flash per component, plus spread + other (equal by construction, see above)."""
        if self.flash is None or self.core is None:
            return {}
        d = {c: self.core[c] - self.flash[c] for c in COMPONENTS}
        d["spread+other"] = d["spread"] + d["other"]
        return d

    def identity_error(self, side: str) -> float:
        parts = getattr(self, side)
        return sum(parts[c] for c in COMPONENTS) - self.identity_total


def compare(prev_ctx: EodContext, prev_outcomes: list[Outcome], ctx: EodContext,
            outcomes: list[Outcome]) -> list[Row]:
    """One row per position valued by core (exact or assumed) on both dates; ``outcomes`` must carry ``pnl``
    (``attach_pnl``). Core is given the D-1 schedule the valuation used (coupon gaps filled); a floater, whose
    projected coupons need the market, is left out."""
    rows = []
    for p, o in zip(prev_outcomes, outcomes, strict=True):
        pv, v = p.valuation, o.valuation
        if pv is None or v is None or {pv.quality, v.quality} - {"exact", "assumed"}:
            continue
        assert p.terms is not None and p.quote is not None
        try:
            s = fixed_schedule(p.terms, pv.valuation_date)
        except Rejected:
            continue
        if s.floater is not None:
            continue
        q, f = pv.quantity, o.pnl
        recv = q * sum(cf.amount for cf in pv.cashflows if pv.valuation_date < cf.payment_date <= v.valuation_date)
        total = q * (v.dirty_rub - pv.dirty_rub) + recv
        flash = None if f is None else {c: getattr(f, c) for c in COMPONENTS}
        # shortcut: the call -> confirmed put mapping is untested (no call horizon in the fixture or the
        # dev books; core rejects a call without reset terms); test it once a priced call bond exists
        offer = s.offer if pv.horizon_kind != "maturity" else None
        try:
            inst = core_instrument(p.terms, pv.valuation_date, pv.horizon_date,
                                   None if offer is None else (offer[0], offer[1], "offer"), coupons=s.coupons)
            x = explain_horizon_pnl(inst, prev_ctx.discount[1], ctx.discount[1], prev_ctx.calendar,
                                    start_dirty_price_rub=pv.dirty_rub, end_dirty_price_rub=v.dirty_rub,
                                    spread_convention=SPREAD_CONVENTION,
                                    redemption_horizon="maturity" if offer is None else "confirmed_put",
                                    curve_cache=ctx.cache)
        except (CoreRejection, ValueError) as e:
            rows.append(Row(o.secid, q, recv, total, flash, None, reason=f"core: {e}"))
            continue
        core = {"carry": q * x.carry_rub, "rolldown": q * x.rolldown_rub, "rates": q * x.rates_rub,
                "spread": q * x.spread_rub, "other": q * x.residual_rub, "total": q * x.total_rub}
        z = None if f is None else (f.z0_bp, f.z1_bp, x.horizon.spread_bp, x.end_spread_bp)
        rows.append(Row(o.secid, q, recv, total, flash, core, z,
                        reason="" if f else f"flash: {o.no_pnl.reason if o.no_pnl else 'no pnl'}"))
    return rows
