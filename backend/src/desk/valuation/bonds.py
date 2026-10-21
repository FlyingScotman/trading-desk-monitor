"""RUB bonds through core's portfolio path, ``value_portfolio`` (slices S1, S4).

Steps:

1. ``bond_request``: decide whether the bond is in scope (``eligibility``), its horizon (the
   next scheduled offer whose window is still open, rule F2, which core 0.33 applies itself: the puts go to core with
   their exercise windows and the policy's ``open_put`` horizon chooses; a perpetual's assumed call), its coupons up to
   it, and the price basis. A refusal is a ``Rejected`` with a stable reason code.
2. ``value_bonds``: one ``value_portfolio`` call for every request of the run, the spread kind per bond in the
   policy (``spread_kinds``: OFZ spread for OFZ, credit spread for the rest). Core calibrates the Z-spread (annual,
   ACT/365F) to the dirty price, and computes bucket DV01, the CS01 or OFZ spread risk and its split by tenor bucket
   (``spread_buckets``), the Hessian, the effective yield and duration, the G-spread to the KBD whatever the discount
   curve (``g_spread_curve_id``, MOEX's ``GSPREADBP`` definition) and a floater's discount margin over its projection
   curve (``discount_margin``). Bucket DV01 and the Hessian stay per curve, under the
   published curve IDs (``RISK_CURVES``): a floater's projection curve (the key-rate projection) is a
   separate factor from its discount curve, so their basis risk is not netted away. Its rejections become
   ``Rejected("core_<code>")``. The hash-verified result (``rub-portfolio-risk-result.v2``) is returned for storage.

**Discount curve** (owner's decision of 20.10, ORG_CONVENTIONS §4, DATA_CONTRACT §0.3): every RUB bond, OFZ and
corporate, fixed and floater, is discounted on the RUONIA OIS curve (``RUONIA_OIS_CURVE_ID``, published ``RUB
RUONIA``) plus its Z-spread calibrated to the market price: the OFZ spread to RUONIA (``ofz_spr01``) or the credit
spread to RUONIA (CS01). So a fixed bond's rate DV01 is on ``RUB RUONIA``; a RUONIA floater projects and discounts
on the same curve (one factor); a key-rate floater has ``RUB KEY`` (projection) and ``RUB RUONIA`` (discount). The
KBD (``RUB OFZ``) stays the market curve of the G-spread, the curve chart and the bond history ("спред к КБД"):
no bond carries DV01 on it, except on a date without a RUONIA OIS curve in the market (``discount_curve``): then
the bonds are discounted on the KBD, their risk is on ``RUB OFZ`` and every valuation says so in ``assumptions``
(quality ``assumed``), and the run goes on.

**A bond is valued somehow rather than not at all** (the owner's rule). Below an exact valuation
``BondValuation.quality`` is ``assumed`` and ``assumptions`` says why, one short reason each:

- a coupon MOEX lists as known without an amount: rate x face x days / 365 (RATE_ACT_365); with
  an amount but no rate (a corporate): the rate from the amount;
- a coupon not known yet (``unknown``, or ``projected_by_moex`` on a fixed bond): at the last
  known rate, or MOEX's projection when no rate is known;
- a perpetual: called on an assumed date (``perpetual_call``), coupons MOEX does not list at the
  current rate;
- a floater on an ``inferred`` formula: its family's default convention (``floater_rule``);
- no MOEX accrued: computed by core (``Instrument.accrued``: a fixed leg, and since core 0.33 a floater's: a coupon
  fixed in advance pro rata on the amount for OFZ-PK or on the rate for corporates, one set in arrears by the formula
  over the elapsed days with the realised fixings);
- our accrued differs from MOEX's by more than ``ACCRUED_TOLERANCE_RUB``: kept and flagged.

Valuation date (finding F9, DEC entry pending): the MOEX price and the MOEX accrued are both
measured at the **trade date**, and the bond trades cum-coupon through the record date. So the
cashflows are those paid after the trade date, and core settles on it (instrument settlement
lag 0, see ``marketdata.adapter.core_instrument``). The T+1 settlement date is kept on the
valuation for information only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    BondCashflow,
    BusinessCalendar,
    CoreRejection,
    CouponRateBounds,
    DayCount,
    ExerciseAssumption,
    FixedCouponLeg,
    FloatingCouponLeg,
    FloatingCouponRule,
    Instrument,
    MarketSnapshot,
    ObservationShift,
    ObservationWindow,
    Offer,
    PortfolioRiskResult,
    Position,
    PositionValuation,
    RateAccrualCouponLeg,
    ShockedCurveCache,
    ValuationPolicy,
    ZSpreadConvention,
    value_portfolio,
)
from rub_fixed_income import __version__ as CORE_VERSION
from rub_fixed_income.dates import add_months

from ..curves.rates import KEY_RATE_CURVE_ID, RUONIA_OIS_CURVE_ID
from ..marketdata.adapter import (
    KBD_CURVE_ID,
    OFZ_CLASSES,
    BondQuote,
    BondTerms,
    Coupon,
    core_instrument,
    price_observation,
)

PRICE_BASIS_ORDER = ("legal_close", "close", "last", "waprice")  # last: intraday cuts only (R11)
"""Provisional valuation price basis (to be fixed by a DEC entry): MOEX legal close, then close,
then weighted average."""

SUPPORTED_COUPON_TYPES = ("fixed", "variable_known", "unknown", "zero", "floating")
SPREAD_CONVENTION = ZSpreadConvention()  # annual, ACT/365F: the desk default (core CONVENTIONS)
ACCRUED_TOLERANCE_RUB = 0.01
CAP_FLOOR_GUARD_BP = 25.0
"""Floors and caps are priced at intrinsic value (core has no volatility) only this far from the strike."""
PERPETUAL_CALL_YEARS = 5

OFZ_CURVE, RUONIA_CURVE = "RUB OFZ", "RUB RUONIA"
RISK_CURVES = {KBD_CURVE_ID: OFZ_CURVE, RUONIA_OIS_CURVE_ID: RUONIA_CURVE, KEY_RATE_CURVE_ID: "RUB KEY"}
"""Core curve ID -> the published curve its bucket DV01 goes on (the run's ``curves`` reference, R8): a key-rate
floater has RUONIA OIS (discounting) and the key-rate projection, each with its own buckets, so its basis risk
shows."""
DISCOUNT_CURVE_ID = RUONIA_OIS_CURVE_ID
"""The discount curve of RUB bonds (the owner's decision of 20.10); ``KBD_CURVE_ID`` when the market has none."""


def ccy_curve(ccy: str) -> str:
    """The published curve of rate risk without a core curve (a duration proxy, a report's DV01): RUONIA for RUB
    (the curve RUB bonds are discounted on, DATA_CONTRACT §0.3), else a curve named by the currency (``USD``),
    published without points until the hub has its curve."""
    return RUONIA_CURVE if ccy == "RUB" else ccy


def discount_curve(market: MarketSnapshot) -> tuple[str, str | None]:
    """The core curve ID RUB bonds are discounted on in ``market`` and, when it is not RUONIA OIS, the note
    every valuation on it carries: RUONIA OIS, else the KBD (a market without the RUONIA curve: the hub
    published no OIS quotes for the date or the curve could not be built, ``orchestrator.with_rates``)."""
    if any(c.curve_id == DISCOUNT_CURVE_ID for c in market.curves):
        return DISCOUNT_CURVE_ID, None
    return KBD_CURVE_ID, (f"discounted on the KBD: no RUONIA OIS curve in the market of {market.business_date}, "
                          "risk on RUB OFZ, spread to the KBD")


FLOATER_BENCHMARKS = ("KEY_RATE", "RUONIA", "RUONIA_3M", "RUONIA_INDEX")
OLD_OFZ_PK = ("SU29007RMFS0", "SU29008RMFS8", "SU29009RMFS6", "SU29010RMFS4")
"""Minfin's in-advance OFZ-PK: RUONIA mean over 6 months to the 2nd business day before the coupon start."""


class Rejected(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class FixedSchedule:
    horizon_date: date
    horizon_kind: str  # maturity | offer | call
    offer: tuple[date, Decimal, str] | None
    current_face: float
    current_coupon_pct: float | None
    coupons: tuple[Coupon, ...] = ()
    """MOEX's coupons with the gaps filled (status ``assumed`` for those the backend assumed)."""
    assumptions: tuple[str, ...] = ()
    floater: tuple[FloatingCouponRule, str, str] | None = None
    """Coupon rule, fixing ID and projection curve ID of a floater."""


def eligibility(terms: BondTerms) -> None:
    if terms.currency != "RUB" or terms.face_unit != "RUB":
        raise Rejected("not_rub", f"currency {terms.currency}, face {terms.face_unit}")
    if terms.is_structured or terms.bond_class == "structured":
        raise Rejected("structured")
    if terms.is_face_indexed:
        raise Rejected("face_indexed")
    if terms.is_fx_face:
        raise Rejected("fx_face")
    if terms.coupon_type not in SUPPORTED_COUPON_TYPES:
        raise Rejected(f"coupon_{terms.coupon_type}")
    if terms.bond_class in ("mbs", "securitization"):
        raise Rejected("securitized", "amortization is prepayment-driven")
    if terms.coupon_type == "floating":
        f = terms.floater_formula
        if f is None:
            raise Rejected("floater_formula_missing")
        if f["benchmark"] not in FLOATER_BENCHMARKS:
            raise Rejected("floater_benchmark_unsupported", f"benchmark {f['benchmark']} ({f.get('benchmark_raw')})")
        if f.get("spread_bp") is None:
            raise Rejected("floater_formula_incomplete", f"{f['status']} formula without a spread")



def _pct(x: float | None) -> str:
    """A floater's floor or cap in its description; «—» when the formula has none (a cap without a floor: 4 floaters
    of the hub's static of 21.10.2026 failed here)."""
    return "—" if x is None else f"{x:.2%}"

def floater_rule(terms: BondTerms) -> tuple[FloatingCouponRule, str, str, str]:
    """Coupon rule, fixing ID, projection curve ID and a description of a floater's formula.

    The hub gives the benchmark and the spread; the observation convention is the family's,
    chosen as the one that reproduces most MOEX coupons published in the dev snapshots
    (09-15.10.2026, coupons since 20.09.2025) and in core's ``test_moex_floater_goldens``:

    - OFZ-PK on RUONIA: arithmetic over the period shifted 6 calendar days, ACT/ACT ISDA, the amount
      accruing over the shifted window (Minfin, 29013+; 39 of 44 coupons). The hub's curated
      ``lag_days`` 7 of SU29015 reproduces 4 of 44, so OFZ-PK ignore ``lag_days``;
    - OFZ-PK 29007-29010 (``OLD_OFZ_PK``): in advance, rate rounded to 0.01 %, ACT/365F; MOEX's
      figure for the running coupon is its fixed rate, so it is used as published;
    - term RUONIA 3M (OFZ-PK 29026+; 14 of 14) and the RUONIA index (VEB.RF; 19 of 20): compounded,
      shifted 7 calendar days;
    - corporates on RUONIA (247 of 380) or the key rate (2277 of 3755; the next best, a single fixing
      5 business days before the start, 1803): arithmetic, shifted 6 calendar days (the formula's
      ``lag_days`` when given), ACT/365F.

    The margin is ``spread_bp``; ``averaging``, ``multiplier`` and ``cap_pct``/``floor_pct`` (on the
    all-in rate) come from the formula when given.
    """
    f = terms.floater_formula
    assert f is not None
    bench, ofz = f["benchmark"], terms.bond_class == "ofz_pk"
    term = bench in ("RUONIA_3M", "RUONIA_INDEX")
    shift = 7 if term else int(f["lag_days"]) if f.get("lag_days") is not None and not ofz else 6
    averaging = f.get("averaging") or ("compounded" if term else "arithmetic")
    window = ObservationWindow(2, "business_days", 6) if terms.secid in OLD_OFZ_PK else None
    dc = DayCount.ACT_ACT_ISDA if ofz and not term and not window else DayCount.ACT_365_FIXED
    floor, cap = (None if f.get(k) is None else float(f[k]) / 100 for k in ("floor_pct", "cap_pct"))
    margin = float(f["spread_bp"]) / 1e4
    rule = FloatingCouponRule(
        "key_rate" if bench == "KEY_RATE" else "ruonia", averaging, margin, dc, dc,
        rate_rounding_decimals=2 if window else None, amount_rounding_decimals=2,
        multiplier=float(f["multiplier"]) if f.get("multiplier") is not None else 1.0,
        bounds=CouponRateBounds("coupon_rate", floor, cap) if floor is not None or cap is not None else None,
        observation_shift=ObservationShift(shift, "calendar_days", "observation") if shift and not window else None,
        observation_window=window)
    obs = "6 months in advance" if window else f"{shift} calendar days back" if shift else "no lag"
    desc = (f"{bench} {'+' if margin >= 0 else '-'} {abs(margin) * 1e4:g} bp, {averaging}, {obs}, "
            f"{'ACT/ACT' if dc is DayCount.ACT_ACT_ISDA else 'ACT/365F'}"
            + (f", floor {_pct(floor)} cap {_pct(cap)}" if rule.bounds else ""))
    if bench == "KEY_RATE":
        return rule, "KEY_RATE", KEY_RATE_CURVE_ID, desc
    return rule, "RUONIA", RUONIA_OIS_CURVE_ID, desc


INDEX_NAMES = {"KEY_RATE": "КС", "RUONIA": "RUONIA", "RUONIA_3M": "RUONIA 3M", "RUONIA_6M": "RUONIA 6M",
               "RUONIA_INDEX": "RUONIA (индекс)", "CPI": "ИПЦ", "ZCYC": "КБД"}
"""Display name of a floater's benchmark; КС and RUONIA are the Desk Monitor's index names (``tdm_run.INDICES``)."""


@dataclass(frozen=True)
class CouponView:
    """The coupon a position shows (D4), for every bond, also one core does not value: from the static terms."""

    pct: float | None
    """% a year; None when nothing is known."""
    basis: str | None
    """``rate`` (MOEX's rate of the running period), ``amount`` (its published amount as a rate:
    amount x 365 / (days x face)), ``assumed`` (no figure for the period: the last known rate), ``projected`` (a
    floater's index + margin at the latest fixing, not a published coupon), ``zero``; None with ``pct``."""
    index_name: str | None = None
    """A floater's benchmark (``INDEX_NAMES``)."""
    margin_pct: float | None = None
    """A floater's margin over the benchmark, %."""


def current_period(terms: BondTerms, d: date) -> tuple[Coupon | None, date | None]:
    """The coupon period running on ``d`` (the first one paid after it) and its start (None when unknown)."""
    for i, c in enumerate(terms.coupons):
        if c.pay_date > d:
            return c, c.start or (terms.coupons[i - 1].pay_date if i else None)
    return None, None


def coupon_view(terms: BondTerms, d: date, index_value: Callable[[str], float | None] | None = None) -> CouponView:
    """The running coupon of a bond on ``d``: MOEX's rate, else its amount as a rate, else for a floater its
    projection ``index + margin`` (``index_value(benchmark)``: the latest fixing, %; floor, cap and multiplier
    applied), else the last known rate. A floater always carries its index and margin when the formula has them."""
    f = terms.floater_formula if terms.coupon_type == "floating" else None
    bench = f.get("benchmark") if f else None
    index = (INDEX_NAMES.get(bench) or f.get("benchmark_raw") or bench) if f and bench else None
    margin = float(f["spread_bp"]) / 100 if f and f.get("spread_bp") is not None else None
    if terms.coupon_type == "zero":
        return CouponView(0.0, "zero")
    c, start = current_period(terms, d)
    if c is not None and c.rate_pct is not None:
        return CouponView(float(c.rate_pct), "rate", index, margin)
    face = (c.face or terms.face_value) if c is not None else None
    if c is not None and c.amount_per_bond and start is not None and c.pay_date > start and face:
        rate = float(c.amount_per_bond) * 365 * 100 / (float(face) * (c.pay_date - start).days)
        return CouponView(round(rate, 4), "amount", index, margin)
    if f is not None:
        fixing = index_value(bench) if index_value and bench and margin is not None else None
        if fixing is None:
            return CouponView(None, None, index, margin)
        rate = fixing * float(f.get("multiplier") or 1) + margin  # type: ignore[operator]
        if f.get("floor_pct") is not None:
            rate = max(rate, float(f["floor_pct"]))
        if f.get("cap_pct") is not None:
            rate = min(rate, float(f["cap_pct"]))
        return CouponView(round(rate, 4), "projected", index, margin)
    last = next((x.rate_pct for x in reversed(terms.coupons) if x.pay_date <= d and x.rate_pct is not None), None)
    return CouponView(float(last), "assumed") if last is not None else CouponView(None, None)


def implied_frequency(terms: BondTerms) -> int | None:
    """Coupons a year from the schedule (365 / the median period), for display when MOEX gives no frequency (D8:
    MOEX omits COUPONFREQUENCY when the first period is irregular)."""
    days, prev = [], None
    for c in terms.coupons:
        start, prev = c.start or prev, c.pay_date
        if start is not None and c.pay_date > start:
            days.append((c.pay_date - start).days)
    if not days:
        return None
    days.sort()
    n = len(days)
    med = days[n // 2] if n % 2 else (days[n // 2 - 1] + days[n // 2]) / 2
    return max(1, round(365 / med))


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


def perpetual_call(terms: BondTerms, after: date) -> tuple[date, tuple[Coupon, ...]]:
    """The assumed call of a perpetual without a scheduled call, and its coupons extended to it.

    DEC10 leaves exercise assumptions open and the snapshot has no call dates, so the default is
    explicit: the first coupon date at least ``PERPETUAL_CALL_YEARS`` after ``after``, MOEX's listed
    date or, past its schedule, the last listed one stepped by the coupon frequency. A call within
    months would price a deep-discount perpetual (most trade at 60-90 %) at yields of thousands of
    percent; five years matches the usual call cycle of Russian AT1 issues.
    """
    if not terms.coupons:
        raise Rejected("perpetual_no_schedule", "no coupon listed")
    target = add_months(after, 12 * PERPETUAL_CALL_YEARS)
    listed = next((c.pay_date for c in terms.coupons if c.pay_date >= target), None)
    if listed is not None:
        return listed, terms.coupons
    last, extra = terms.coupons[-1], []
    step = max(1, 12 // terms.coupon_frequency) if terms.coupon_frequency else 6
    while (prev := extra[-1] if extra else last).pay_date < target:
        extra.append(Coupon(prev.no + 1, prev.pay_date, add_months(prev.pay_date, step), None, None, None, "unknown",
                            last.face))
    return extra[-1].pay_date, terms.coupons + tuple(extra)


def _amount(rate_pct: Decimal, face: Decimal, days: int) -> Decimal:
    return (rate_pct / 100 * face * days / 365).quantize(Decimal("0.01"), ROUND_HALF_UP)


def _fill_coupons(terms: BondTerms, coupons: tuple[Coupon, ...], after: date, horizon: date,
                  notes: list[str]) -> tuple[Coupon, ...]:
    """Coupons paid in ``(after, horizon]`` with the gaps core cannot value filled (see the module
    docstring). A floater's are left as listed: core projects the unpublished ones."""
    ofz, floating = terms.bond_class in OFZ_CLASSES, terms.coupon_type == "floating"
    out: list[Coupon] = []
    last_rate, prev_pay = None, None
    computed, derived, moex, assumed = 0, 0, 0, set()
    for c in coupons:
        start, prev_pay = c.start or prev_pay, c.pay_date
        known = c.status in ("known", "current_published")
        if c.pay_date <= after or floating:
            if after < c.pay_date <= horizon:
                in_advance = terms.secid in OLD_OFZ_PK and c.start is not None and c.start <= after
                out.append(replace(c, status="current_published") if in_advance and c.amount_per_bond else c)
            elif known and c.rate_pct:
                last_rate = c.rate_pct
            continue
        if c.pay_date > horizon:
            break
        face = c.face or terms.face_value
        days = (c.pay_date - start).days if start else 0
        if known and c.amount_per_bond is not None:
            pass
        elif known and c.rate_pct is not None and face:
            c = replace(c, amount_per_bond=_amount(c.rate_pct, face, days))
            computed += 1
        elif last_rate is not None and face:
            c = replace(c, status="assumed", rate_pct=last_rate, amount_per_bond=_amount(last_rate, face, days))
            assumed.add(last_rate)
        elif c.status == "projected_by_moex" and c.amount_per_bond is not None:
            moex += 1
        else:
            raise Rejected("coupon_not_known", f"coupon {c.no} on {c.pay_date}: {c.status}, no known rate before it")
        if c.rate_pct is None and not ofz and days > 0 and face:  # a corporate accrues on the rate
            c = replace(c, rate_pct=round(c.amount_per_bond * 365 * 100 / (face * days), 6))  # type: ignore[operator]
            derived += 1
        if c.status != "assumed" and c.rate_pct:
            last_rate = c.rate_pct
        out.append(replace(c, start=start))
    if computed:
        notes.append(f"coupon amount from rate x face x days/365: {computed} coupon(s)")
    if derived:
        notes.append(f"coupon rate from amount: {derived} coupon(s)")
    if assumed:
        notes.append(f"future coupons assumed at {', '.join(f'{r.normalize():f} %' for r in sorted(assumed))}")
    if moex:
        notes.append(f"MOEX-projected coupons used, no known rate: {moex} coupon(s)")
    return tuple(out)


def face_on(terms: BondTerms, d: date) -> Decimal | None:
    """Face outstanding on ``d`` after the redemptions paid on or before it, from the static face moved by the
    redemptions between the static snapshot's date and ``d`` (a static snapshot later or earlier than the market).

    The static face is the face before the redemptions dated on its own date (seen on the dev snapshots: an
    amortization on 17.10 is still in the face of the 17.10 static, one on 16.10 is out), and before the final
    redemption on a bond matured by then (MOEX keeps that face)."""
    if terms.face_value is None or terms.face_date is None:
        return terms.face_value
    s = terms.face_date
    if terms.maturity_date is not None and not terms.is_perpetual:
        s = min(s, terms.maturity_date)
    reds = [r.value_per_bond or Decimal(0) for r in terms.redemptions if (d < r.date < s if d < s else s <= r.date <= d)]
    return terms.face_value + (1 if d < s else -1) * sum(reds, Decimal(0))


def fixed_schedule(terms: BondTerms, settlement: date) -> FixedSchedule:
    """The horizon, the coupons up to it and the checks core does not make (redemptions complete)."""
    eligibility(terms)
    if terms.face_value is None or terms.face_value <= 0:
        raise Rejected("no_face_value")
    notes: list[str] = []
    offer, coupons = next_offer(terms, settlement), terms.coupons
    if terms.is_perpetual:
        if offer is None:
            call, coupons = perpetual_call(terms, settlement)
            offer = (call, Decimal(100), "call")
            notes.append(f"perpetual: call assumed {call.isoformat()}")
        horizon, kind = offer[0], offer[2]
    else:
        if terms.maturity_date is None or terms.maturity_date <= settlement:
            raise Rejected("matured")
        horizon, kind = (offer[0], offer[2]) if offer else (terms.maturity_date, "maturity")
        future_reds = [r for r in terms.redemptions if r.date > settlement]
        if any(r.value_per_bond is None for r in future_reds):
            raise Rejected("redemption_amount_missing")
        total = sum((r.value_per_bond for r in future_reds), Decimal(0))  # type: ignore[misc]
        if abs(total - (face := face_on(terms, settlement))) > Decimal("0.01"):  # type: ignore[operator]
            raise Rejected("redemption_incomplete", f"future redemptions {total} vs face {face} on {settlement}")
    floater = None
    if terms.coupon_type == "floating":
        rule, fixing_id, curve_id, desc = floater_rule(terms)
        floater = (rule, fixing_id, curve_id)
        if terms.floater_formula["status"] != "curated":  # type: ignore[index]
            notes.append(f"formula {terms.floater_formula['status']}: {desc}")  # type: ignore[index]
        if kind == "call":  # core has no callable floaters (callables.py)
            notes.append(f"call {horizon.isoformat()} valued as the maturity")
    filled = _fill_coupons(terms, coupons, settlement, horizon, notes)
    current = next((float(c.rate_pct) for c in filled if c.rate_pct is not None), None)
    return FixedSchedule(horizon, kind, offer, float(face_on(terms, settlement)), current, filled,  # type: ignore[arg-type]
                         tuple(notes), floater)


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


@dataclass(frozen=True)
class BondRequest:
    """One position that passed the backend's checks, ready for core."""

    position_id: str
    terms: BondTerms
    quote: BondQuote
    schedule: FixedSchedule
    quantity: float
    settlement: date
    price_basis: str
    clean_pct: float


def bond_request(position_id: str, terms: BondTerms, quote: BondQuote, settlement: date,
                 quantity: float) -> BondRequest:
    schedule = fixed_schedule(terms, quote.trade_date)  # valuation date: see the module docstring
    basis, clean = select_price(quote)
    face = float(quote.face_value) if quote.face_value is not None else schedule.current_face
    if abs(face - schedule.current_face) > 0.01:
        raise Rejected("face_mismatch", f"quote face {face} vs static {schedule.current_face}")
    return BondRequest(position_id, terms, quote, schedule, quantity, settlement, basis, clean)


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
    """Sum of the bucket DV01 (central differences, 1 bp)."""
    buckets: dict[str, float]
    """Per bucket, summed over the curves (``curve_buckets``): every curve moved together (the flash PnL's
    linear rates term)."""
    spread01_rub: float
    """CS01 for credit, OFZ spread risk for OFZ: core factor ``CS``."""
    calculation_id: str
    curve_buckets: dict[str, dict[str, float]] = field(default_factory=dict)
    """Bucket DV01 per published curve (``RISK_CURVES``, ``ccy_curve``); empty: all of ``buckets`` on the KBD.
    Read it through ``risk``."""
    curve_convexity: float | None = None
    hessian: list[tuple[str, str, float]] = field(default_factory=list)
    """Upper triangle, factor names ``IR|<published curve>|<bucket>`` and ``CS``: a floater's discount and
    projection curves are separate factors, with their cross terms."""
    cashflows: tuple[BondCashflow, ...] = ()
    """To the horizon; a floater's projected on the base curves."""
    core_version: str = CORE_VERSION
    quality: str = "exact"
    """exact | assumed | market_value | stale_price | model_price | par_proxy (the owner's rule: a bond is
    valued on the best rung available, never left out; every rung below exact says why in ``assumptions``)."""
    assumptions: tuple[str, ...] = ()
    model_spread_source: str | None = None
    """Set on a ``model_price``: where its ``z_spread_bp`` comes from (stored and carried by the desk store)."""
    discount_curve_id: str | None = None
    """Core ID of the curve the bond is discounted on and its Z-spread is to (``DISCOUNT_CURVE_ID``, the KBD on a
    date without RUONIA, ``discount_curve``); None for a value without a curve (a price or a report)."""
    floater: FloaterModel | None = field(default=None, repr=False, compare=False)
    """A floater's coupon model, for the flash PnL to reproject it on other curves (``pnl.flash``)."""
    g_spread_bp: float | None = None
    """Core's G-spread to the KBD (a RUB fixed-coupon bond; effective yield minus the KBD at the Macaulay duration,
    MOEX's ``GSPREADBP`` definition), whatever the discount curve; None for a floater and off core."""
    discount_margin_bp: float | None = None
    """A floater's discount margin: the spread over its projection curve that reprices its projected flows to the
    dirty price (core ``discount_margin_bp``; the Z-spread on a single curve); None for a fixed bond and off core."""
    spread_buckets: dict[str, float] = field(default_factory=dict)
    """``spread01_rub`` split by ``RISK_BUCKETS`` (core ``spread_buckets_rub``, in proportion to the discount-only
    key-rate risk): CS01 by tenor for credit, the OFZ–RUONIA basis by tenor for OFZ (R18); empty off core."""

    @property
    def spread_curve(self) -> str | None:
        """The published curve of ``z_spread_bp`` (``RISK_CURVES``)."""
        return None if self.discount_curve_id is None else RISK_CURVES.get(self.discount_curve_id)

    def risk(self) -> dict[str, dict[str, float]]:
        """Bucket DV01 by published curve: ``curve_buckets``, else ``buckets`` on the discount curve (RUONIA)."""
        return self.curve_buckets or {self.spread_curve or RUONIA_CURVE: self.buckets}


@dataclass(frozen=True)
class FloaterModel:
    """What core priced a floater with: its instrument, the offer it is redeemed at (None: maturity) and the
    cap/floor guard of the policy. ``pnl.flash`` builds its coupons on any dated market from these."""

    instrument: Instrument
    offer: Offer | None
    horizon_date: date
    guard_bp: float | None


@dataclass
class _Prepared:
    request: BondRequest
    instrument: Instrument
    """With every coupon to the horizon in its leg (accrued and cashflows read it)."""
    flows: tuple[BondCashflow, ...]
    notes: list[str]
    accrued: float
    """Core's (``Instrument.accrued``), to check the MOEX accrued."""


def _split_assumed(inst: Instrument, s: FixedSchedule, after: date) -> tuple[Instrument, tuple]:
    """A call's instrument without its trailing assumed coupons, which go to ``ExerciseAssumption.to_call``."""
    leg = inst.coupon_leg
    flows = [c for c in s.coupons if after < c.pay_date <= s.horizon_date]
    n = next((i for i, c in enumerate(flows) if c.status == "assumed"), len(flows))
    if not isinstance(leg, FixedCouponLeg | RateAccrualCouponLeg) or n == 0 or n == len(flows) \
            or any(c.status != "assumed" for c in flows[n:]):
        return inst, ()
    known = FixedCouponLeg(leg.periods[:n]) if isinstance(leg, FixedCouponLeg) \
        else RateAccrualCouponLeg(leg.periods[:n], leg.accrual_basis)  # type: ignore[arg-type]
    return replace(inst, coupon_leg=known), leg.periods[n:]


def _prepare(r: BondRequest, market: MarketSnapshot) -> _Prepared:
    s, after = r.schedule, r.quote.trade_date
    call = s.horizon_kind == "call"
    inst = core_instrument(r.terms, after, s.horizon_date, s.offer, coupons=s.coupons, floater=s.floater,
                           redeem_at_horizon=call and s.floater is not None)
    # Cashflows to the horizon: a call (or a perpetual's assumed one) redeems the face then.
    flow_inst = core_instrument(r.terms, after, s.horizon_date, coupons=s.coupons, floater=s.floater,
                                redeem_at_horizon=True) if call else inst
    offer = None if call or not inst.offers else inst.horizon("open_put", after)[1]
    floating = isinstance(inst.coupon_leg, FloatingCouponLeg)  # not when no coupon is left to the horizon
    flows = flow_inst.cashflows(after=after, offer=offer, market=market if floating else None,
                                strike_guard_bp=CAP_FLOOR_GUARD_BP)
    accrued = inst.accrued(after, market if floating else None)
    notes = list(s.assumptions)
    moex = r.quote.accrued_per_bond
    if moex is None:
        notes.append("accrued computed: no MOEX accrued")
    elif abs(round(accrued - float(moex), 2)) > ACCRUED_TOLERANCE_RUB:
        notes.append(f"accrued mismatch vs MOEX {accrued - float(moex):+.2f} RUB (ours {accrued:.2f})")
    return _Prepared(r, inst, tuple(BondCashflow(f.payment_date, f.amount) for f in flows), notes, accrued)


def value_bonds(market: MarketSnapshot, requests: list[BondRequest], *, second_order: bool = False,
                cache: ShockedCurveCache | None = None, scenarios: tuple = ()
                ) -> tuple[dict[str, BondValuation | Rejected], list[PortfolioRiskResult]]:
    """Value ``requests`` on ``market`` (a core snapshot without quotes, with the curves and fixings
    floaters need), in one core call (the spread kind per bond: ``spread_kinds``). Returns the valuation or the
    rejection per position ID, and the core results (one, or none without a valuable request). ``scenarios`` (core
    ``PortfolioScenario``) go to the core call."""
    out: dict[str, BondValuation | Rejected] = {}
    results = []
    disc, disc_note = discount_curve(market)
    for r in requests:  # core values at the business date; the MOEX accrued is as of the trade date
        if r.quote.trade_date != market.business_date:
            out[r.position_id] = Rejected("stale_quote", f"trade date {r.quote.trade_date}, business date "
                                          f"{market.business_date}")
    reqs = [r for r in requests if r.position_id not in out]
    instruments, quotes, positions, calls, ok, kinds = {}, {}, [], {}, {}, {}
    for r in reqs:
        s = r.schedule
        try:
            prep = ok.get(r.terms.secid) or _prepare(r, market)
            inst = prep.instrument
            if s.horizon_kind == "call" and s.floater is None:
                inst, assumed = _split_assumed(inst, s, r.quote.trade_date)
                calls[inst.instrument_id] = ExerciseAssumption.to_call(s.horizon_date, assumed)
        except (CoreRejection, ValueError) as e:
            code = e.code.lower() if isinstance(e, CoreRejection) else "invalid_terms"
            out[r.position_id] = Rejected(f"core_{code}", str(e))
            continue
        ok[r.terms.secid] = prep
        instruments[inst.instrument_id] = inst
        kinds[inst.instrument_id] = "ofz" if r.terms.bond_class in OFZ_CLASSES else "credit"
        # no MOEX accrued: core's own (a floater's too since core 0.33)
        quotes[inst.instrument_id] = price_observation(inst.instrument_id, r.price_basis, r.clean_pct,
                                                       r.quote.accrued_per_bond)
        positions.append(Position(r.position_id, inst.instrument_id, r.quantity, "desk", AccountingCategory.TRADING,
                                  "RUB"))
    if not positions:
        return out, results
    guard = any(i.coupon_leg.rule.bounds for i in instruments.values()  # type: ignore[union-attr]
                if isinstance(i.coupon_leg, FloatingCouponLeg))
    policy = ValuationPolicy(disc, spread_kind="credit", spread_convention=SPREAD_CONVENTION, horizon="open_put",
                             include_second_order=second_order,
                             cap_floor_guard_bp=CAP_FLOOR_GUARD_BP if guard else None,
                             exercise_assumptions=tuple(calls.items()),
                             spread_kinds=tuple((i, k) for i, k in kinds.items() if k == "ofz"),
                             g_spread_curve_id=KBD_CURVE_ID, discount_margin=True, spread_buckets=True)
    floaters = any(isinstance(i.coupon_leg, FloatingCouponLeg) for i in instruments.values())
    # Without floaters the call needs only the discount curve and the KBD (the G-spread).
    snap = market if floaters else replace(market, fixings=(), curves=tuple(
        c for c in market.curves if c.curve_id in (disc, KBD_CURVE_ID)))
    result = value_portfolio(run_id=f"desk-eod-{market.business_date}-book", as_of=market.known_at,
                             snapshot=replace(snap, quotes=tuple(quotes.values())),
                             instruments=list(instruments.values()), positions=positions, policy=policy,
                             scenarios=scenarios, curve_cache=cache)
    results.append(result)
    for rej in result.coverage.rejections:
        out[rej.position_id] = Rejected(f"core_{rej.code.lower()}", rej.message)
    rows = {v.position_id: v for v in result.positions}
    for p in positions:
        if p.position_id in rows:
            out[p.position_id] = _valuation(ok[p.instrument_id], rows[p.position_id], p.quantity, guard, disc_note)
    return out, results


def _valuation(prep: _Prepared, v: PositionValuation, quantity: float, guard: bool = False,
               disc_note: str | None = None) -> BondValuation:
    r = prep.request
    notes = [*prep.notes, *([disc_note] if disc_note else [])]
    inst = prep.instrument
    floater = None
    if isinstance(inst.coupon_leg, FloatingCouponLeg):
        offer = inst.horizon(v.horizon, v.settlement_date)[1] if v.horizon in ("confirmed_put", "open_put") else None
        floater = FloaterModel(inst, offer, v.horizon_date, CAP_FLOOR_GUARD_BP if guard else None)
    first = v.factor_risk.first_order_by_factor()
    by_curve: dict[str, dict[str, float]] = {}
    for f, x in first.items():
        if f.startswith("IR|"):
            _, c, b = _factor(f).split("|")
            by_curve.setdefault(c, dict.fromkeys(RISK_BUCKETS, 0.0))[b] += x
    buckets = {b: math.fsum(bs[b] for bs in by_curve.values()) for b in RISK_BUCKETS}
    if v.horizon_date != r.schedule.horizon_date:  # core's open_put chose another offer than rule F2 (not expected)
        notes.append(f"horizon: core {v.horizon_date.isoformat()} ({v.horizon}), backend "
                     f"{r.schedule.horizon_date.isoformat()} ({r.schedule.horizon_kind})")
    out = BondValuation(
        secid=r.terms.secid, quantity=quantity, valuation_date=v.settlement_date, settlement=r.settlement,
        horizon_date=v.horizon_date, horizon_kind=r.schedule.horizon_kind, price_basis=r.price_basis,
        clean_pct=v.clean_price_pct, accrued_rub=v.accrued_interest_rub, face_rub=v.current_nominal_rub,
        current_coupon_pct=r.schedule.current_coupon_pct, dirty_rub=v.dirty_price_rub, pv_rub=v.pv_rub,
        ytm_pct=v.effective_yield * 100.0, macaulay_years=v.macaulay_duration_years,
        modified_duration=v.macaulay_duration_years / (1.0 + v.effective_yield),
        z_spread_bp=v.spread_bp, spread_kind=v.spread_kind, dv01_rub=math.fsum(buckets.values()), buckets=buckets,
        spread01_rub=first["CS"], calculation_id=v.calculation_id, curve_buckets=by_curve, cashflows=prep.flows,
        quality="assumed" if notes else "exact", assumptions=tuple(notes), discount_curve_id=v.curve_id,
        floater=floater, g_spread_bp=v.g_spread_bp, discount_margin_bp=v.discount_margin_bp,
        spread_buckets=dict(zip(RISK_BUCKETS, v.spread_buckets_rub)) if v.spread_buckets_rub else {},
    )
    if v.factor_risk.has_second_order:
        # Core's full Hessian over its factors, renamed to the published curves; every curve is its own factor,
        # so a floater keeps the cross terms of its discount and projection curves.
        for (a, b), x in v.factor_risk.hessian_by_factor().items():
            fa, fb = sorted((_factor(a), _factor(b)))
            out.hessian.append((fa, fb, x))
        # every curve moved in parallel together: the convexity of the yield
        rates = math.fsum(x * (1 if a == b else 2) for a, b, x in out.hessian
                          if a.startswith("IR|") and b.startswith("IR|"))
        out.curve_convexity = rates * 1e8 / v.pv_rub if v.pv_rub else None
    return out


def _factor(name: str) -> str:
    """Core ``IR|<curve>|<bucket>`` -> ``IR|<published curve>|<bucket>`` (``RISK_CURVES``)."""
    if not name.startswith("IR|"):
        return name
    _, c, b = name.split("|")
    return f"IR|{RISK_CURVES.get(c, c)}|{b}"
