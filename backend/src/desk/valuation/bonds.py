"""Fixed-coupon RUB bonds on a discount curve with core 0.8.1 (slice S1).

Steps per instrument:

1. ``eligibility`` and ``fixed_schedule``: decide whether core can price it as fixed cashflows,
   and build those cashflows from the snapshot's facts: ``known`` coupons, redemptions, and the
   next scheduled offer as horizon. A refusal is a ``Rejected`` with a stable reason code; it is
   never priced on guessed numbers.
2. ``value_bond``: dirty price from the selected price basis plus MOEX accrued; core
   ``BondRiskEngine`` calibrates the Z-spread to it (annual, ACT/365F) and computes bucket DV01,
   KRD, CS01 or OFZ spread risk and, optionally, the full Hessian; core ``compatibility`` gives
   the effective yield and duration.

Valuation date (found in S2, to be fixed by a DEC entry): the MOEX price and the MOEX accrued
are both measured at the **trade date**, and the bond trades cum-coupon through the record
date. So the cashflows are those paid after the trade date, and core values them at the trade
date. The T+1 settlement date is kept on the valuation for information only. Valuing at T+1
dropped a coupon paid on the settlement date while the price still contained its accrued.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from rub_fixed_income import (
    RISK_BUCKETS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    BusinessCalendar,
    DiscountCurve,
    ShockedCurveCache,
    ZSpreadConvention,
    bond_duration,
    solve_nominal_annual_yield,
)
from rub_fixed_income import __version__ as CORE_VERSION

from ..marketdata.adapter import BondQuote, BondTerms

PRICE_BASIS_ORDER = ("legal_close", "close", "waprice")
"""Provisional valuation price basis (to be fixed by a DEC entry): MOEX legal close, then close,
then weighted average."""

OFZ_CLASSES = ("ofz_pd", "ofz_ad", "ofz_pk", "ofz_in")
SUPPORTED_COUPON_TYPES = ("fixed", "variable_known")
SPREAD_CONVENTION = ZSpreadConvention()  # annual, ACT/365F: the desk default (core CONVENTIONS)


class Rejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FixedSchedule:
    cashflows: tuple[BondCashflow, ...]
    horizon_date: date
    horizon_kind: str  # maturity | offer | call
    current_face: float
    current_coupon_pct: float | None


def eligibility(terms: BondTerms) -> None:
    if terms.currency != "RUB" or terms.face_unit != "RUB":
        raise Rejected("not_rub", f"currency {terms.currency}, face {terms.face_unit}")
    if terms.is_perpetual:
        raise Rejected("perpetual")
    if terms.is_structured or terms.bond_class == "structured":
        raise Rejected("structured")
    if terms.is_face_indexed:
        raise Rejected("face_indexed")
    if terms.is_fx_face:
        raise Rejected("fx_face")
    if terms.coupon_type not in SUPPORTED_COUPON_TYPES:
        raise Rejected(f"coupon_{terms.coupon_type}", "floaters arrive in slice S3")
    if terms.bond_class in ("mbs", "securitization"):
        raise Rejected("securitized", "amortization is prepayment-driven")


def next_offer(terms: BondTerms, settlement: date) -> tuple[date, Decimal, str] | None:
    """Earliest scheduled offer after settlement and before maturity whose exercise window is
    still open: a holder put (or an untyped offer) or an issuer call. MOEX analytics price to
    the same date (``yield_date_type = OFFER``)."""
    cands = [o for o in terms.offers
             if o.status == "scheduled" and o.type in ("put", "call", "unknown") and o.date is not None
             and o.date > settlement and (terms.maturity_date is None or o.date < terms.maturity_date)
             and (o.window_end is None or o.window_end >= settlement)]
    if not cands:
        return None
    o = min(cands, key=lambda o: o.date)  # type: ignore[arg-type,return-value]
    price = o.price_pct if o.price_pct is not None else Decimal(100)
    return o.date, price, ("call" if o.type == "call" else "offer")  # type: ignore[return-value]


def fixed_schedule(terms: BondTerms, settlement: date) -> FixedSchedule:
    eligibility(terms)
    if terms.maturity_date is None or terms.maturity_date <= settlement:
        raise Rejected("matured")
    if terms.face_value is None or terms.face_value <= 0:
        raise Rejected("no_face_value")
    offer = next_offer(terms, settlement)
    horizon, kind = (offer[0], offer[2]) if offer else (terms.maturity_date, "maturity")

    future_reds = [r for r in terms.redemptions if r.date > settlement]
    if any(r.value_per_bond is None for r in future_reds):
        raise Rejected("redemption_amount_missing")
    total = sum((r.value_per_bond for r in future_reds), Decimal(0))  # type: ignore[misc]
    if abs(total - terms.face_value) > Decimal("0.01"):
        raise Rejected("redemption_incomplete", f"future redemptions {total} vs face {terms.face_value}")

    flows: dict[date, Decimal] = {}
    current_coupon = None
    for c in terms.coupons:
        if c.pay_date <= settlement:
            continue
        if current_coupon is None:
            current_coupon = float(c.rate_pct) if c.rate_pct is not None else None
        if c.pay_date > horizon:
            break
        if c.status != "known" or c.amount_per_bond is None:
            raise Rejected("coupon_not_known", f"coupon {c.no} on {c.pay_date}: {c.status}")
        flows[c.pay_date] = flows.get(c.pay_date, Decimal(0)) + c.amount_per_bond
    outstanding = terms.face_value
    for r in future_reds:
        if r.date > horizon:
            break
        flows[r.date] = flows.get(r.date, Decimal(0)) + r.value_per_bond  # type: ignore[operator]
        outstanding -= r.value_per_bond  # type: ignore[operator]
    if offer is not None and outstanding > 0:
        assert offer is not None
        flows[horizon] = flows.get(horizon, Decimal(0)) + outstanding * offer[1] / Decimal(100)
    if not flows:
        raise Rejected("no_future_cashflows")
    return FixedSchedule(
        tuple(BondCashflow(d, float(a)) for d, a in sorted(flows.items())),
        horizon, kind, float(terms.face_value), current_coupon,
    )


def select_price(quote: BondQuote) -> tuple[str, float]:
    for basis in PRICE_BASIS_ORDER:
        if basis in quote.prices_pct:
            return basis, float(quote.prices_pct[basis])
    raise Rejected("no_price", f"bases present: {sorted(quote.prices_pct) or 'none'}")


def settlement_date(trade_date: date, lag_days: int | None, calendar: BusinessCalendar,
                    valid_from: date, valid_to: date) -> date:
    lag = 1 if lag_days is None else lag_days
    d = calendar.add_business_days(trade_date, lag)
    if not valid_from <= d <= valid_to:
        raise Rejected("calendar_unknown", f"{d} outside {valid_from}..{valid_to}")
    return d


@dataclass
class BondValuation:
    secid: str
    quantity: float
    valuation_date: date
    """The trade date of the quote: cashflows after it, PV and yield as of it."""
    settlement: date
    horizon_date: date
    horizon_kind: str
    price_basis: str
    clean_pct: float
    accrued_rub: float
    face_rub: float
    current_coupon_pct: float | None
    dirty_rub: float
    pv_rub: float
    ytm_pct: float
    macaulay_years: float
    modified_duration: float
    z_spread_bp: float
    spread_kind: str
    dv01_rub: float
    parallel_dv01_rub: float
    buckets: dict[str, float]
    krd_years: dict[str, float]
    spread01_rub: float
    base_id: str
    curve_convexity: float | None = None
    gradient: dict[str, float] = field(default_factory=dict)
    hessian: list[tuple[str, str, float]] = field(default_factory=list)
    cashflows: tuple[BondCashflow, ...] = ()
    core_version: str = CORE_VERSION


def value_bond(
    terms: BondTerms,
    quote: BondQuote,
    schedule: FixedSchedule,
    valuation_date: date,
    curve: DiscountCurve,
    *,
    settlement: date | None = None,
    quantity: float = 1.0,
    second_order: bool = False,
    cache: ShockedCurveCache | None = None,
) -> BondValuation:
    basis, clean = select_price(quote)
    if quote.accrued_per_bond is None:
        raise Rejected("no_accrued")
    face = float(quote.face_value) if quote.face_value is not None else schedule.current_face
    if abs(face - schedule.current_face) > 0.01:
        raise Rejected("face_mismatch", f"quote face {face} vs static {schedule.current_face}")
    accrued = float(quote.accrued_per_bond)
    dirty = clean * face / 100.0 + accrued
    spread_kind = "ofz" if terms.bond_class in OFZ_CLASSES else "credit"
    flows = tuple((cf.payment_date, cf.amount) for cf in schedule.cashflows)
    try:
        ytm = solve_nominal_annual_yield(dirty_price=dirty, settlement_date=valuation_date, cashflows=flows)
        dur = bond_duration(annual_yield=ytm, settlement_date=valuation_date, cashflows=flows)
        engine = BondRiskEngine(
            BondRiskRequest(
                cashflows=schedule.cashflows, settlement_date=valuation_date, curve=curve,
                spread_convention=SPREAD_CONVENTION, spread_kind=spread_kind,  # type: ignore[arg-type]
                dirty_price_rub=dirty, quantity=quantity,
            ),
            curve_cache=cache or ShockedCurveCache(),
        )
        risk = engine.risk()
    except (ValueError, RuntimeError) as e:
        raise Rejected("core_error", str(e)) from e
    out = BondValuation(
        secid=terms.secid, quantity=quantity, valuation_date=valuation_date,
        settlement=settlement or valuation_date,
        horizon_date=schedule.horizon_date, horizon_kind=schedule.horizon_kind,
        price_basis=basis, clean_pct=clean, accrued_rub=accrued, face_rub=face,
        current_coupon_pct=schedule.current_coupon_pct, dirty_rub=dirty, pv_rub=risk.pv_rub,
        ytm_pct=ytm * 100.0, macaulay_years=dur.macaulay_years, modified_duration=dur.modified_years,
        z_spread_bp=risk.spread_bp, spread_kind=spread_kind, dv01_rub=risk.dv01_rub,
        parallel_dv01_rub=risk.parallel_dv01_rub,
        buckets={b.bucket: b.dv01_rub for b in risk.buckets},
        krd_years={b.bucket: b.krd_years for b in risk.buckets},
        spread01_rub=risk.cs01_rub if spread_kind == "credit" else risk.ofz_spr01_rub,
        base_id=risk.base_id, cashflows=schedule.cashflows,
    )
    if second_order:
        so = engine.second_order()
        names = [f"IR|{b}" for b in RISK_BUCKETS] + ["CS"]
        out.gradient = dict(zip(names, so.gradient_rub_per_bp))
        h = so.hessian_rub_per_bp2
        out.hessian = [(names[i], names[j], h[i][j]) for i in range(len(names)) for j in range(i, len(names))
                       if h[i][j] != 0.0]
        rates = sum(h[i][j] for i in range(len(RISK_BUCKETS)) for j in range(len(RISK_BUCKETS)))
        out.curve_convexity = rates * 1e8 / risk.pv_rub if risk.pv_rub else None
    return out
