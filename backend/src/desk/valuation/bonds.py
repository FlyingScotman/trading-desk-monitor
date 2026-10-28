"""RUB bonds through core's portfolio path, ``value_portfolio`` (slices S1, S4).

Steps:

1. ``bond_request``: decide whether the bond is in scope (``eligibility``), its horizon (the
   next scheduled put offer whose window is still open, rule F2, which core 0.33 applies itself: the puts go to core
   with their exercise windows and the policy's ``open_put`` horizon chooses; no call is ever assumed exercised:
   a perpetual without a put is valued as never called, TDM#35), its coupons up to it, and the price basis. A refusal is a ``Rejected`` with a stable reason code.
2. ``value_bonds``: one ``value_portfolio`` call for every request of the run, the spread kind per bond in the
   policy (``spread_kinds``: OFZ spread for OFZ, credit spread for the rest). Core calibrates the Z-spread (annual,
   ACT/365F) to the dirty price, and computes bucket DV01, the CS01 or OFZ spread risk and its split by tenor bucket
   (``spread_buckets``), the Hessian, the effective yield and duration, the G-spread to the KBD whatever the discount
   curve (``g_spread_curve_id``, MOEX's ``GSPREADBP`` definition) and a floater's discount margin over its projection
   curve (``discount_margin``). Bucket DV01 and the Hessian stay per curve, under the
   published curve IDs (``RISK_CURVES``): a floater's projection curve (the key-rate projection) is a
   separate factor from its discount curve, so their basis risk is not netted away. Its rejections become
   ``Rejected("core_<code>")``. The hash-verified result (``rub-portfolio-risk-result.v2``) is returned for storage.
3. ``horizon_bonds`` (the forward carry, TDM#66): core ``horizon_portfolio`` on the same inputs (``core_call``, shared
   with ``value_bonds`` so a carry is computed on exactly what was valued): each spread calibrated to its quote and
   held, the curves static; per position the carry, the roll-down and the coupon income (``BondCarry``).

**Discount curve** (owner's decisions of 20.10, 22.10 and 23.10, ORG_CONVENTIONS §4, DATA_CONTRACT §0.3, core
``docs/proposals/ofz-curve-risk-factors.md``): a fixed-coupon OFZ (``OFZ_KBD_CLASSES``: PD, AD, IN) is discounted on
the KBD (``KBD_CURVE_ID``, published ``RUB OFZ``, spec ``rub_ofz_kbd_v3``) plus its own Z-spread to it
(``ofz_spr01``: the spread to the KBD). Its rate risk is two computed risks (core 0.39, ``OFZ_BASIS_IDS``, TDM#213):
to RUONIA's OIS quotes with the basis held (``BondValuation.ofz_ruonia``) and to the basis, the KBD's par yields
1Y-20Y+ with RUONIA held (``ofz_basis01``; equal to the KBD's par risk to the bit). The row publishes them in the
scenarios' coordinates (``ofz_levels``): ``RUB OFZ`` the basis risk (a move of the KBD's par yields), ``RUB RUONIA``
the RUONIA risk minus it, so its sum over the curves is the RUONIA risk and a scenario on ``RUB OFZ`` and ``RUB
RUONIA`` reprices as core does; the pages show it as RUONIA plus the basis or as the KBD. Every other RUB bond (corporates, OFZ-PK, floaters) is discounted on the RUONIA OIS curve
(``RUONIA_OIS_CURVE_ID``, published ``RUB RUONIA``) plus its Z-spread calibrated to the market price (CS01 for credit,
``ofz_spr01`` for an OFZ-PK); a RUONIA floater projects and discounts on the same curve (one factor); a key-rate
floater has ``RUB KEY`` (projection) and ``RUB RUONIA`` (discount). The KBD stays the market curve of the G-spread,
the curve chart and the bond history ("спред к КБД"). On a date without a RUONIA OIS curve in the market
(``discount_curve``) every bond is discounted on the KBD, its risk is on ``RUB OFZ`` and every valuation says so in
``assumptions`` (quality ``assumed``), and the run goes on. Until 0.2.5 every OFZ was on RUONIA and ``ofz_spr01`` was
its spread to RUONIA (``OFZ_SCHEME``).

**A bond is valued somehow rather than not at all** (the owner's rule). Below an exact valuation
``BondValuation.quality`` is ``assumed`` and ``assumptions`` says why, one short reason each:

- a coupon MOEX lists as known without an amount: rate x face x days / 365 (RATE_ACT_365); with
  an amount but no rate (a corporate): the rate from the amount;
- a coupon not known yet (``unknown``, or ``projected_by_moex`` on a fixed bond): at the last
  known rate, or MOEX's projection when no rate is known;
- a perpetual: never called (TDM#35, the owner's rule ORG_CONVENTIONS §4.1 «не отзовут», core BACKLOG #125-#128):
  its listed coupons and then the last one for ever (core ``ExerciseAssumption.never_called``, ``PerpetualTail``),
  ``perpetual: no call assumed``; a listed coupon not known yet at the last known rate;
- a floater on an ``inferred`` formula: its family's default convention (``floater_rule``);
- no MOEX accrued (an intraday cut, a gap): computed by core (``Instrument.accrued``) and flagged only where core's
  accrual is not exact: a coupon period the backend assumed or MOEX projected, or a floater's coupon not published yet
  (accrued on the projected rate). A fixed leg with its published coupon, and a floater coupon fixed in advance (pro
  rata on the amount for OFZ-PK, on the rate for corporates, core 0.33), are exact: no note, the quality stays;
- a foreign bond (CNY, USD, EUR face; core 0.34): valued in its currency on its own curve (``value_bonds``,
  ``foreign``), converted at the run's market rate; MOEX's accrued in RUB (``currency_id`` SUR) is converted to the
  face currency at that rate. A government par-yield proxy curve (USD UST, EUR ECB AAA: not OIS) or a same-currency
  fallback curve is said in the assumptions;
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
from itertools import pairwise
from typing import Any

from rub_fixed_income import (
    OFZ_BASIS_CURVE_BASIS,
    RISK_BUCKETS,
    AccountingCategory,
    BondCashflow,
    BusinessCalendar,
    CoreRejection,
    CouponRateBounds,
    CurveBootstrapRequest,
    DayCount,
    ExerciseAssumption,
    FixedCouponLeg,
    FixingDateRule,
    FloatingCouponLeg,
    FloatingCouponRule,
    Instrument,
    MarketSnapshot,
    ObservationShift,
    ObservationWindow,
    Offer,
    OfzBasisIds,
    ParYieldCurveRequest,
    PerpetualTail,
    PortfolioHorizonResult,
    PortfolioRiskResult,
    Position,
    PositionHorizon,
    PositionValuation,
    RateAccrualCouponLeg,
    ShockedCurveCache,
    ValuationPolicy,
    ZSpreadConvention,
    clean_price_pct_to_dirty_rub,
    floating_perpetual_tail,
    horizon_portfolio,
    perpetual_tail,
    value_portfolio,
)
from rub_fixed_income import __version__ as CORE_VERSION
from rub_fixed_income.dates import add_months

from ..curves.rates import KEY_RATE_CURVE_ID, RUONIA_OIS_CURVE_ID, kbd_fixing_id
from ..marketdata.adapter import (
    FORMULA_FLOATER_LABEL,
    KBD_CURVE_ID,
    OFZ_CLASSES,
    BondQuote,
    BondTerms,
    Coupon,
    bond_currency,
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

OFZ_CURVE, RUONIA_CURVE = "RUB OFZ", "RUB RUONIA"
RISK_CURVES = {KBD_CURVE_ID: OFZ_CURVE, RUONIA_OIS_CURVE_ID: RUONIA_CURVE, KEY_RATE_CURVE_ID: "RUB KEY"}
"""Core curve ID -> the published curve its bucket DV01 goes on (the run's ``curves`` reference, R8): a key-rate
floater has RUONIA OIS (discounting) and the key-rate projection, each with its own buckets, so its basis risk
shows."""
DISCOUNT_CURVE_ID = RUONIA_OIS_CURVE_ID
"""The discount curve of RUB bonds other than ``OFZ_KBD_CLASSES`` (the owner's decision of 20.10); ``KBD_CURVE_ID`` when
the market has none."""
OFZ_KBD_CLASSES = ("ofz_pd", "ofz_ad", "ofz_in")
"""OFZ discounted on the KBD (option C, core 0.34+; par buckets, core 0.37.0): the fixed-coupon and indexed ones. An
OFZ-PK (a RUONIA floater) stays on RUONIA OIS."""
OFZ_SCHEME, OFZ_SCHEME_P = "ruonia_basis", "kbd_par"
"""How the run values OFZ (the run's lineage ``ofz_scheme``, the pages' basis view): ``ruonia_basis`` -- on the KBD, the
risk two computed risks, to RUONIA's OIS quotes and to the basis (core 0.39, TDM#213; a row in it has
``extra.ofz_ruonia01``); ``kbd_par`` (``OFZ_SCHEME_P``, 0.2.6, and a run of 0.2.7 without RUONIA OIS quotes) -- on the
KBD in par buckets, ``ofz_basis01`` the par risk on the KBD and the pages' RUONIA the same risk again (core
``basis_view``, variant P: RUONIA ≡ basis bucket by bucket), ``ofz_spr01`` the spread to the KBD; a run without it --
on RUONIA, ``ofz_spr01`` the basis to RUONIA (until 0.2.5)."""
OFZ_BASIS_ID = "RUB_OFZ_BASIS"
"""Core's name of the OFZ basis factor (``IR|RUB_OFZ_BASIS|<bucket>``, core ``OfzBasisIds.basis_id``): not a snapshot
curve. It never reaches a row (``ofz_levels`` takes it to ``RUB OFZ``); a scenario names it (``scenario.exact``)."""
OFZ_BASIS_IDS = OfzBasisIds(KBD_CURVE_ID, RUONIA_OIS_CURVE_ID, OFZ_BASIS_ID)
"""Core ``ValuationPolicy.ofz_basis`` of a core call that holds the RUONIA OIS curve as quotes (``value_bonds``)."""


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


FLOATER_BENCHMARKS = ("KEY_RATE", "RUONIA", "RUONIA_3M", "RUONIA_INDEX", "ZCYC")
KBD_RESET_FIXING_OFFSET = 4
"""ZCYC publication days before the reset a KBD-reset perpetual's coupon is fixed on when the hub's formula has no
``fixing_offset`` (TDM#383): the curated value of the issuer's per-period KBD floaters (РЖД 001Б-04, 4 publication
days); the offset only moves the forward's start by days."""
KBD_FIXING_UNITS = {"zcyc_publication_days": "business_days", "business_days": "business_days",
                    "calendar_days": "calendar_days"}
"""A KBD floater's fixing offset unit (contract 1.4 ``fixing_offset_unit``) -> core's (``FixingDateRule.unit``): a day
with an end-of-day ZCYC publication is a business day of the KBD's fixing calendar (``curves.rates.kbd_fixing_entries``,
MOEX_STOCK), so the hub's ``zcyc_publication_days`` are core's business days."""
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
    horizon_kind: str  # maturity | offer | perpetual (never called: the last listed coupon, then core's tail)
    offer: tuple[date, Decimal, str] | None
    current_face: float
    current_coupon_pct: float | None
    coupons: tuple[Coupon, ...] = ()
    """MOEX's coupons with the gaps filled (status ``assumed`` for those the backend assumed)."""
    assumptions: tuple[str, ...] = ()
    floater: tuple[FloatingCouponRule, str, str] | None = None
    """Coupon rule, fixing ID and projection curve ID of a floater."""
    resets: tuple[date, ...] = ()
    """A KBD-reset perpetual's reset dates (``kbd_reset``): each coupon is fixed at the last one on or before it."""


FOREIGN_BOND_CCYS = ("CNY", "USD", "EUR")
"""Face currencies core values a bond in besides RUB (core ``SUPPORTED_CURRENCIES``, 0.34: each on its own curve)."""


def eligibility(terms: BondTerms) -> None:
    """A foreign-face bond (CNY, USD, EUR; MOEX's ``is_fx_face``, settled in RUB or in its currency) is in scope since
    core 0.36 (BACKLOG #132): core discounts it on its currency's curve (``value_bonds``, ``foreign``). Another face
    currency stays ``not_rub``."""
    ccy = bond_currency(terms)
    if ccy != "RUB" and ccy not in FOREIGN_BOND_CCYS:
        raise Rejected("not_rub", f"currency {terms.currency}, face {terms.face_unit}")
    if terms.is_structured or terms.bond_class == "structured":
        raise Rejected("structured")
    if terms.is_face_indexed:
        raise Rejected("face_indexed")
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
        if f["benchmark"] == "ZCYC" and kbd_reset(terms) is None and (
                f.get("benchmark_tenor_years") is None or f.get("fixing_offset") is None
                or (f.get("fixing_offset_unit") or "zcyc_publication_days") not in KBD_FIXING_UNITS):
            raise Rejected("floater_formula_incomplete",
                           f"KBD floater without its tenor or fixing rule (contract 1.4): tenor "
                           f"{f.get('benchmark_tenor_years')}, offset {f.get('fixing_offset')} {f.get('fixing_offset_unit')}")



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
    if bench == "ZCYC":
        if (reset := kbd_reset(terms)) is not None:
            years, resets = reset
            rule, fixing_id, curve_id, desc = kbd_floater_rule(
                {**f, "benchmark_tenor_years": years, "fixing_offset": KBD_RESET_FIXING_OFFSET,
                 "fixing_offset_unit": "zcyc_publication_days"})
            return rule, fixing_id, curve_id, (f"{desc}, reset every {years} years at the calls (last "
                                               f"{resets[-1].isoformat()}; tenor and fixing assumed)")
        return kbd_floater_rule(f)
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


def is_kbd_floater(terms: BondTerms) -> bool:
    """A floater on the KBD (MOEX ``ZR_YLD_CRV``, the hub's benchmark ``ZCYC``): its coupons project on ``RUB OFZ``."""
    f = terms.floater_formula
    return terms.coupon_type == "floating" and f is not None and f.get("benchmark") == "ZCYC"


def kbd_reset(terms: BondTerms) -> tuple[int, tuple[date, ...]] | None:
    """A KBD-reset perpetual (TDM#383, core BACKLOG #127): its coupon is fixed for N years at each call date to the
    N-year KBD plus the margin (РЖД 001Б-01/-02, Газпром капитал 001Б-02/-03: 6.55 % for five years, then
    17.26 % = KBD 5Y of June 2025 + 2.05 %). The hub's formula is ``ZCYC`` without a tenor or fixing (its fit per coupon
    reproduces few coupons: the rate does not move between resets). Read from the terms: a perpetual with call dates
    (past and scheduled) a whole number N of years apart, and one rate for every coupon listed from the last past call
    to the next. Returns (N, the reset dates up to the next scheduled one), or None (a formula with its own tenor is
    a floater fixed per period)."""
    f = terms.floater_formula
    if not terms.is_perpetual or f is None or f.get("benchmark") != "ZCYC" or f.get("benchmark_tenor_years") is not None:
        return None
    calls = sorted({o.date for o in terms.offers if o.type == "call" and o.date is not None
                    and o.status in ("past", "scheduled")})
    past = [d for d in calls if any(o.date == d and o.status == "past" for o in terms.offers)]
    nxt = [d for d in calls if d not in past]
    if not past or not nxt or len(calls) < 2:
        return None
    gaps = {round((b - a).days / 365.25) for a, b in pairwise(calls)}
    if len(gaps) != 1 or (years := gaps.pop()) < 1 or any(abs((b - a).days - years * 365.25) > 31
                                                         for a, b in pairwise(calls)):
        return None
    cycle = [c for c in terms.coupons if c.start is not None and past[-1] <= c.start and c.pay_date <= nxt[0]]
    rates = {c.rate_pct for c in cycle}
    if not cycle or len(rates) != 1 or None in rates:
        return None
    return years, tuple(d for d in calls if d <= nxt[0])


def kbd_floater_rule(f: dict[str, Any]) -> tuple[FloatingCouponRule, str, str, str]:
    """A KBD floater (MOEX ``ZR_YLD_CRV``, TDM#33) as the hub's contract 1.4 states it (``verify/zcycfloat`` of the hub):
    rate = round(Y(T; d_fix), ``rate_rounding_decimals``) + margin, ``Y(T; d)`` the KBD's effective annual zero yield
    at the constant maturity ``benchmark_tenor_years`` of day ``d``'s end-of-day publication, ``d_fix`` the
    ``fixing_offset``-th publication day before the period start (``fixing_anchor`` ``accrual_start``): core's KBD index
    (0.34, ``index="kbd"``, one fixing in advance on ACT/365F). The margin is the current ``spread_bp`` (the hub's
    ``spread_schedule`` steps only at a put offer, the horizon of an open put). It projects on the KBD
    (``KBD_CURVE_ID``, published ``RUB OFZ``: its par buckets) and is discounted like every corporate (RUONIA OIS +
    Z): its coupon risk is the OFZ basis, its discount risk RUONIA's."""
    tenor = float(f["benchmark_tenor_years"])
    unit = KBD_FIXING_UNITS[f.get("fixing_offset_unit") or "zcyc_publication_days"]
    margin = float(f["spread_bp"]) / 1e4
    dec = f.get("rate_rounding_decimals")
    floor, cap = (None if f.get(k) is None else float(f[k]) / 100 for k in ("floor_pct", "cap_pct"))
    rule = FloatingCouponRule(
        "kbd", "single_fixing", margin, DayCount.ACT_365_FIXED, DayCount.ACT_365_FIXED,
        rate_rounding_decimals=2 if dec is None else int(dec), amount_rounding_decimals=2,
        multiplier=float(f["multiplier"]) if f.get("multiplier") is not None else 1.0,
        bounds=CouponRateBounds("coupon_rate", floor, cap) if floor is not None or cap is not None else None,
        fixing_date=FixingDateRule("accrual_start", int(f["fixing_offset"]), unit),  # type: ignore[arg-type]
        index_tenor_years=tenor)
    desc = (f"KBD {tenor:g}Y {'+' if margin >= 0 else '-'} {abs(margin) * 1e4:g} bp, fixed {f['fixing_offset']} "
            f"{unit.replace('_', ' ')} before the period, ACT/365F"
            + (f", floor {_pct(floor)} cap {_pct(cap)}" if rule.bounds else ""))
    return rule, kbd_fixing_id(tenor), KBD_CURVE_ID, desc


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
    still open: a holder put (or an untyped offer). MOEX analytics price to the same date
    (``yield_date_type = OFFER``). An issuer call is never the horizon (TDM#35, ORG_CONVENTIONS §4: a call is almost
    never exercised): a callable runs to its maturity, a perpetual is never called."""
    cands = [o for o in terms.offers
             if o.status == "scheduled" and o.type in ("put", "unknown") and o.date is not None
             and o.date > settlement and (terms.maturity_date is None or o.date < terms.maturity_date)
             and (o.window_end is None or o.window_end >= settlement)]
    if not cands:
        return None
    o = min(cands, key=lambda o: o.date)  # type: ignore[arg-type,return-value]
    price = o.price_pct if o.price_pct is not None else Decimal(100)
    return o.date, price, "offer"  # type: ignore[return-value]


def perpetual_coupons(terms: BondTerms, after: date) -> tuple[Coupon, ...]:
    """The coupons of a never-called perpetual: MOEX's listed ones, with at least two paid after ``after`` (core's
    ``perpetual_tail`` continues the last at the step of the last two): past the listed schedule one stepped by the
    coupon frequency, ``unknown`` (``_fill_coupons`` assumes it at the last known rate)."""
    if not terms.coupons:
        raise Rejected("perpetual_no_schedule", "no coupon listed")
    out = list(terms.coupons)
    step = max(1, 12 // terms.coupon_frequency) if terms.coupon_frequency else 6
    while sum(c.pay_date > after for c in out) < 2:
        prev = out[-1]
        out.append(Coupon(prev.no + 1, prev.pay_date, add_months(prev.pay_date, step), None, None, None, "unknown",
                          prev.face))
    return tuple(out)


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
        if offer is None:  # never called (TDM#35): the listed coupons, then core's tail of the last one
            coupons = perpetual_coupons(terms, settlement)
            horizon, kind = coupons[-1].pay_date, "perpetual"
            notes.append("perpetual: no call assumed")
        else:
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
    floater, resets = None, ()
    if terms.coupon_type == "floating":
        rule, fixing_id, curve_id, desc = floater_rule(terms)
        floater = (rule, fixing_id, curve_id)
        if (reset := kbd_reset(terms)) is not None:
            resets = reset[1]
            # the running cycle's coupons are set at its reset (MOEX lists them projected): cash, not a projection
            cycle = [c for c in coupons if c.status == "projected_by_moex" and c.start is not None
                     and resets[-2] <= c.start and c.pay_date <= resets[-1]]
            coupons = tuple(replace(c, status="known") if c in cycle else c for c in coupons)
            if coupons[-1].pay_date <= resets[-1]:  # the next cycle's first coupon, for core to continue cycle by cycle
                last = coupons[-1]
                step = max(1, 12 // terms.coupon_frequency) if terms.coupon_frequency else 6
                coupons += (Coupon(last.no + 1, last.pay_date, add_months(last.pay_date, step), None, None, None,
                                   "unknown", last.face),)
            if cycle:
                notes.append(f"coupons to the reset {resets[-1].isoformat()} at {cycle[0].rate_pct:f} % "
                             "(set at the last reset, MOEX)")
        if FORMULA_FLOATER_LABEL in terms.labels:  # TDM#401
            notes.append("floater by its formula: MOEX lists known coupons, the coming ones are not known")
        if terms.floater_formula["status"] != "curated":  # type: ignore[index]
            notes.append(f"formula {terms.floater_formula['status']}: {desc}")  # type: ignore[index]
    filled = _fill_coupons(terms, coupons, settlement, horizon, notes)
    current = next((float(c.rate_pct) for c in filled if c.rate_pct is not None), None)
    return FixedSchedule(horizon, kind, offer, float(face_on(terms, settlement)), current, filled,  # type: ignore[arg-type]
                         tuple(notes), floater, resets)


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
    perpetual_tail: PerpetualTail | None = field(default=None, repr=False, compare=False)
    """A never-called fixed-coupon perpetual (TDM#35): its coupons after ``cashflows``, for the flash PnL to reprice
    with them (core ``BondRiskRequest.perpetual_tail``)."""
    g_spread_bp: float | None = None
    """Core's G-spread to the KBD (a RUB fixed-coupon bond; effective yield minus the KBD at the Macaulay duration,
    MOEX's ``GSPREADBP`` definition), whatever the discount curve; None for a floater and off core."""
    discount_margin_bp: float | None = None
    """A floater's discount margin: the spread over its projection curve that reprices its projected flows to the
    dirty price (core ``discount_margin_bp``; the Z-spread on a single curve); None for a fixed bond and off core."""
    spread_buckets: dict[str, float] = field(default_factory=dict)
    """``spread01_rub`` split by ``RISK_BUCKETS`` (core ``spread_buckets_rub``, in proportion to the discount-only
    key-rate risk): CS01 by tenor for credit, the OFZ–RUONIA basis by tenor for OFZ (R18); empty off core."""
    fx_delta: dict[str, float] | None = None
    """A derivative valued by core (an FX forward, NDF, swap, cross, FX future: ``valuation.derivatives``): its spot
    equivalent per currency (core ``fx_delta_foreign``, a cross's base and quote); None: the row's own rule."""
    extra: dict[str, Any] = field(default_factory=dict)
    """A derivative valued by core: what the run publishes in the row's ``extra`` beside the common fields (legs,
    OCP amounts, basis risk, fixing status, the core model flags, the reconciliation with the report)."""
    fx_rate: tuple[str, float] | None = None
    """A foreign bond valued by core: its currency and the rate its ``_rub`` amounts are converted at (core
    ``CurrencyAmounts``, the run's market rate), so the flash PnL reprices it in its currency and books the rate move
    in ``fx_gross`` (``pnl.flash.explain``); None for a RUB bond and off core."""
    ofz_ruonia: dict[str, float] = field(default_factory=dict)
    """An OFZ on the KBD valued in RUONIA + basis (``OFZ_BASIS_IDS``, TDM#213): its RUONIA risk by bucket, to RUONIA's
    OIS quotes with the basis held (core ``IR|RUB_RUONIA_OIS|*``); its basis risk is ``curve_buckets["RUB OFZ"]`` and
    ``curve_buckets["RUB RUONIA"]`` is the difference (``ofz_levels``). Empty for every other bond."""

    @property
    def spread_curve(self) -> str | None:
        """The published curve of ``z_spread_bp`` (``RISK_CURVES``; a foreign curve's ID is its published name)."""
        return None if self.discount_curve_id is None else RISK_CURVES.get(self.discount_curve_id,
                                                                           self.discount_curve_id)

    @property
    def foreign(self) -> bool:
        """Discounted by core on a foreign curve (a CNY / USD / EUR bond, core 0.34): no RUB curve to explain on."""
        return self.discount_curve_id is not None and self.discount_curve_id not in RISK_CURVES

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
    tail: PerpetualTail | None = None
    """A never-called floating perpetual (TDM#35, core 0.41): ``instrument`` holds its leg continued as core values it
    (``floating_perpetual_tail``, about 35 years) and this the projected tail after it."""


@dataclass
class _Prepared:
    request: BondRequest
    instrument: Instrument
    """With every coupon to the horizon in its leg (accrued and cashflows read it)."""
    flows: tuple[BondCashflow, ...]
    notes: list[str]
    accrued: float
    """Core's (``Instrument.accrued``), to check the MOEX accrued."""
    coupons: tuple[tuple[date, float], ...] = ()
    """The coupon part of ``flows`` by payment date (``horizon_bonds``: the coupon income of a bond redeemed before
    the horizon)."""
    tail: PerpetualTail | None = None
    """A never-called fixed-coupon perpetual's coupons after ``flows`` (core ``perpetual_tail`` of its leg, as core
    values it), for the flash PnL."""


def _prepare(r: BondRequest, market: MarketSnapshot) -> _Prepared:
    s, after = r.schedule, r.quote.trade_date
    inst = core_instrument(r.terms, after, s.horizon_date, s.offer, coupons=s.coupons, floater=s.floater,
                           resets=s.resets)
    offer = None if not inst.offers else inst.horizon("open_put", after)[1]
    floating = isinstance(inst.coupon_leg, FloatingCouponLeg)  # not when no coupon is left to the horizon
    # cashflows to the horizon; a never-called perpetual's listed coupons, without its tail
    flows = inst.cashflows(after=after, offer=offer, market=market if floating else None,
                           strike_guard_bp=CAP_FLOOR_GUARD_BP)
    tail = perpetual_tail(inst.coupon_leg.periods) \
        if s.horizon_kind == "perpetual" and isinstance(inst.coupon_leg, FixedCouponLeg | RateAccrualCouponLeg) else None
    accrued = inst.accrued(after, market if floating else None)
    notes = list(s.assumptions)
    moex = quote_accrued(r, market)
    ccy = bond_currency(r.terms)
    if moex is None:
        why = inexact_accrual(r, inst, after)
        if why:
            notes.append(f"accrued computed: no MOEX accrued, {why}")
    elif abs(round(accrued - float(moex), 2)) > ACCRUED_TOLERANCE_RUB:
        notes.append(f"accrued mismatch vs MOEX {accrued - float(moex):+.2f} {ccy} (ours {accrued:.2f})")
    return _Prepared(r, inst, tuple(BondCashflow(f.payment_date, f.amount) for f in flows), notes, accrued,
                     tuple((f.payment_date, f.coupon) for f in flows), tail)


def quote_accrued(r: BondRequest, market: MarketSnapshot) -> Decimal | None:
    """MOEX's accrued per bond in the bond's currency: as published, converted from RUB at the run's rate for a
    foreign-face bond settled in RUB (``currency_id`` SUR/RUB; core's ``FxRate`` of the market). None without one."""
    moex, ccy = r.quote.accrued_per_bond, bond_currency(r.terms)
    if moex is None or ccy == "RUB":
        return moex
    settle = (r.quote.currency_id or r.terms.currency or ccy).upper()
    if settle not in ("SUR", "RUB", "RUR"):
        return moex
    rate = next((x.rub_per_unit for x in market.fx if x.currency == ccy), None)
    return None if not rate else (moex / Decimal(str(rate))).quantize(Decimal("0.01"), ROUND_HALF_UP)


def inexact_accrual(r: BondRequest, inst: Instrument, after: date) -> str | None:
    """Why core's own accrued of the running period is not exact (None: it is): the period's coupon was assumed by
    the backend or projected by MOEX, or a floater's coupon is not published yet (accrued on the projected rate)."""
    running = next((c for c in r.schedule.coupons if c.pay_date > after), None)
    if running is None:
        return None
    if running.status in ("assumed", "projected_by_moex"):
        return f"running coupon {running.status}"
    if isinstance(inst.coupon_leg, FloatingCouponLeg):
        published = running.status in ("known", "current_published") and bool(running.amount_per_bond)
        if not published:
            return "floater coupon not published, accrued on the projected rate"
    return None


def value_bonds(market: MarketSnapshot, requests: list[BondRequest], *, second_order: bool = False,
                cache: ShockedCurveCache | None = None, scenarios: tuple = (),
                foreign: dict[str, tuple[str, ...]] | None = None, quote_curve_risk: bool = False,
                ofz_basis: bool = True) -> tuple[dict[str, BondValuation | Rejected], list[PortfolioRiskResult]]:
    """Value ``requests`` on ``market`` (a core snapshot without quotes, with the curves and fixings
    floaters need), in one core call (the spread kind per bond: ``spread_kinds``). Returns the valuation or the
    rejection per position ID, and the core results (one, or none without a valuable request). ``scenarios`` (core
    ``PortfolioScenario``) go to the core call. ``foreign``: ccy -> the curve IDs a foreign bond of that currency is
    discounted on, in order (core ``foreign_curves``, then ``foreign_curve_fallbacks``;
    ``orchestrator.foreign_bond_curves``); a foreign bond of a currency without one is ``Rejected("no_foreign_curve")``
    (the fallback ladder values it). ``quote_curve_risk`` (``EodContext.quote_curve_risk``): core's
    ``ValuationPolicy.quote_curve_risk`` when the call holds a curve as par-yield quotes (a proxy of ``risk_market``),
    so its risk is in those quotes; a call without one keeps its policy and its bytes. ``ofz_basis``
    (``EodContext.ofz_basis``): an OFZ on the KBD in RUONIA + basis (core ``ValuationPolicy.ofz_basis``, ``OFZ_BASIS_IDS``)
    when ``market`` holds the RUONIA OIS curve as its quotes (``EodContext.risk_market``); off, or on built curves (the
    fallback ladder's medians), on the KBD alone (variant P, ``OFZ_SCHEME_P``). Fixed flows take core's closed form
    (``risk_method`` and ``second_order_method`` ``analytic``): equal to the bumps up to their O(bump²) error, and the
    RUONIA + basis Hessian by bumps is about 1 060 repricings per bond. The core inputs are ``core_call``'s, which
    ``horizon_bonds`` shares."""
    call = core_call(market, requests, second_order=second_order, foreign=foreign,
                     quote_curve_risk=quote_curve_risk, ofz_basis=ofz_basis)
    out: dict[str, BondValuation | Rejected] = dict(call.out)
    results = []
    if call.policy is None:
        return out, results
    result = value_portfolio(run_id=f"desk-eod-{market.business_date}-book", as_of=market.known_at,
                             snapshot=call.snapshot, instruments=list(call.instruments.values()),
                             positions=call.positions, policy=call.policy, scenarios=scenarios, curve_cache=cache)
    results.append(result)
    for rej in result.coverage.rejections:
        out[rej.position_id] = Rejected(f"core_{rej.code.lower()}", rej.message)
    rows = {v.position_id: v for v in result.positions}
    for p in call.positions:
        if p.position_id in rows:
            v = rows[p.position_id]
            note = call.disc_note if v.curve_id == call.disc else foreign_curve_note(v, v.curve_id in call.proxies)
            out[p.position_id] = _valuation(call.prepared[p.instrument_id], v, p.quantity, call.guard, note)
    return out, results


@dataclass
class CoreCall:
    """The inputs of one core portfolio call over a run's bond requests (``core_call``): what ``value_bonds`` gives
    ``value_portfolio`` and ``horizon_bonds`` gives ``horizon_portfolio``, so a carry is computed on exactly the
    instruments, quotes, curves and policy the valuation was."""

    out: dict[str, Rejected]
    """The requests refused before core (a stale quote, a foreign bond without its curve or rate, invalid terms)."""
    prepared: dict[str, _Prepared]
    """By instrument ID (the secid): the core instrument, its flows and notes."""
    instruments: dict[str, Instrument]
    positions: list[Position]
    snapshot: MarketSnapshot
    """``market`` with the quotes and only the inputs the call needs."""
    policy: ValuationPolicy | None
    """None: no request reaches core (``positions`` empty)."""
    guard: bool
    disc: str
    disc_note: str | None
    proxies: set[str]


def core_call(market: MarketSnapshot, requests: list[BondRequest], *, second_order: bool = False,
              foreign: dict[str, tuple[str, ...]] | None = None, quote_curve_risk: bool = False,
              ofz_basis: bool = True) -> CoreCall:
    """The core inputs of ``requests`` on ``market`` (see ``value_bonds`` for the parameters): the refusals before
    core, the instruments (a perpetual's ``ExerciseAssumption``: never called), the quotes, one position
    per request and the policy (the spread kind per bond, the KBD for ``OFZ_KBD_CLASSES``, the foreign curves, the
    RUONIA + basis scheme when ``market`` holds the RUONIA OIS quotes)."""
    foreign = foreign or {}
    out: dict[str, Rejected] = {}
    disc, disc_note = discount_curve(market)
    for r in requests:  # core values at the business date; the MOEX accrued is as of the trade date
        ccy = bond_currency(r.terms)
        if r.quote.trade_date != market.business_date:
            out[r.position_id] = Rejected("stale_quote", f"trade date {r.quote.trade_date}, business date "
                                          f"{market.business_date}")
        elif ccy != "RUB" and not foreign.get(ccy):
            out[r.position_id] = Rejected("no_foreign_curve", f"no {ccy} curve in the run (CNY: the XCCY curve; "
                                          "USD/EUR: the par-yield proxies with proxy_curves_for_risk)")
        elif ccy != "RUB" and not any(x.currency == ccy for x in market.fx):
            out[r.position_id] = Rejected("no_fx_rate", f"{ccy} on {market.business_date}")
    reqs = [r for r in requests if r.position_id not in out]
    instruments, quotes, positions, calls, ok, kinds = {}, {}, [], {}, {}, {}  # calls: the exercise assumptions
    on_kbd: set[str] = set()
    for r in reqs:
        s = r.schedule
        try:
            prep = ok.get(r.terms.secid) or _prepare(r, market)
            inst = prep.instrument
            if s.horizon_kind == "perpetual":  # TDM#35: no call is exercised, the coupons run for ever
                calls[inst.instrument_id] = ExerciseAssumption.never_called()
        except (CoreRejection, ValueError) as e:
            code = e.code.lower() if isinstance(e, CoreRejection) else "invalid_terms"
            out[r.position_id] = Rejected(f"core_{code}", str(e))
            continue
        ok[r.terms.secid] = prep
        instruments[inst.instrument_id] = inst
        kinds[inst.instrument_id] = "ofz" if r.terms.bond_class in OFZ_CLASSES else "credit"
        if r.terms.bond_class in OFZ_KBD_CLASSES and bond_currency(r.terms) == "RUB":
            on_kbd.add(inst.instrument_id)
        # no MOEX accrued: core's own (a floater's too since core 0.33); a foreign bond's in its currency
        quotes[inst.instrument_id] = price_observation(inst.instrument_id, r.price_basis, r.clean_pct,
                                                       quote_accrued(r, market))
        positions.append(Position(r.position_id, inst.instrument_id, r.quantity, "desk", AccountingCategory.TRADING,
                                  inst.currency))
    if not positions:
        return CoreCall(out, ok, instruments, positions, market, None, False, disc, disc_note, set())
    guard = any(i.coupon_leg.rule.bounds for i in instruments.values()  # type: ignore[union-attr]
                if isinstance(i.coupon_leg, FloatingCouponLeg))
    ccys = sorted({i.currency for i in instruments.values()} - {"RUB"})
    policy = ValuationPolicy(disc, spread_kind="credit", spread_convention=SPREAD_CONVENTION, horizon="open_put",
                             include_second_order=second_order,
                             cap_floor_guard_bp=CAP_FLOOR_GUARD_BP if guard else None,
                             exercise_assumptions=tuple(calls.items()),
                             spread_kinds=tuple((i, k) for i, k in kinds.items() if k == "ofz"),
                             curve_overrides=tuple((i, KBD_CURVE_ID) for i in sorted(on_kbd)) if disc != KBD_CURVE_ID else (),
                             g_spread_curve_id=KBD_CURVE_ID, discount_margin=True, spread_buckets=True,
                             foreign_curves=tuple((c, foreign[c][0]) for c in ccys),
                             foreign_curve_fallbacks=tuple((c, i) for c in ccys for i in foreign[c][1:]))
    floaters = any(isinstance(i.coupon_leg, FloatingCouponLeg) for i in instruments.values())
    # The call needs the discount curve, the KBD (the G-spread), a floater's projection curves and the foreign curves
    # of its bonds, and no FX forward inputs: a RUB-only book keeps its bytes.
    keep = {disc, KBD_CURVE_ID, *(i for c in ccys for i in foreign[c])}
    snap = replace(market, fixings=market.fixings if floaters else (),
                   curves=tuple(c for c in market.curves if floaters and not _foreign_id(c.curve_id)
                                or c.curve_id in keep),
                   fx=tuple(x for x in market.fx if x.currency in ccys), fx_spots=(), fx_bases=(), fx_fixings=())
    proxies = {c.curve_id for c in market.curves if c.spec_id.endswith("_par_proxy_v1")}
    if quote_curve_risk and any(isinstance(c.curve, ParYieldCurveRequest) for c in snap.curves):
        policy = replace(policy, quote_curve_risk=True)
    # the view covers an OFZ on the KBD and a KBD floater on RUONIA OIS (core#90: projection on the KBD read as RUONIA
    # + basis, TDM#33)
    kbd_floaters = any(isinstance(i.coupon_leg, FloatingCouponLeg) and i.coupon_leg.projection_curve_id == KBD_CURVE_ID
                       for i in instruments.values())
    if ofz_basis and (on_kbd or kbd_floaters) and disc == RUONIA_OIS_CURVE_ID and any(
            c.curve_id == RUONIA_OIS_CURVE_ID and isinstance(c.curve, CurveBootstrapRequest) for c in snap.curves):
        policy = replace(policy, ofz_basis=OFZ_BASIS_IDS)
    policy = replace(policy, risk_method="analytic", second_order_method="analytic")
    return CoreCall(out, ok, instruments, positions, replace(snap, quotes=tuple(quotes.values())), policy, guard,
                    disc, disc_note, proxies)


@dataclass(frozen=True)
class BondCarry:
    """One position's carry to a horizon by core ``horizon_portfolio`` (RUB, signed by quantity), split so that
    ``total = coupon + pull + rolldown``.

    Core holds the Z-spread calibrated to the quote and the curves static: ``carry`` (core ``carry_rub``) is
    ``pv_forward + received - pv_start``, the return at today's forwards, and ``rolldown`` (core ``rolldown_rub``) is
    ``pv_rolled - pv_forward``, the gain of the curve keeping its shape in calendar days. Both are dirty values, so
    the accrued interest is inside them; ``coupon`` takes it out as the coupon income of the horizon: the coupons
    received (core ``coupons_received_rub``) plus the change of the accrued coupon from the start settlement to the
    horizon settlement (core ``accrued_end_rub - accrued_start_rub``; a floater's from ``Instrument.accrued`` on the
    run's market, which core leaves to the caller). ``pull`` is the rest of core's carry (``carry - coupon``): the
    clean price's drift at the held spread on today's forwards, the pull to par included."""

    position_id: str
    total: float
    """``carry + rolldown`` (core ``horizon_pnl_rub``): the bond's return to the horizon before funding."""
    coupon: float
    pull: float
    rolldown: float
    carry: float
    """Core's ``carry_rub`` (``coupon + pull``)."""
    received: float
    """Coupons and principal paid to the horizon settlement (core ``received_rub``)."""
    pv_start: float
    horizon_settlement: date
    redemption_date: date
    spread_bp: float | None
    """Core's calibrated spread; None for ``method`` ``redeemed`` (no spread is needed)."""
    method: str = "core"
    """``core`` (``horizon_portfolio``) or ``redeemed``: the bond pays its last flow by the horizon, which core does
    not carry (it needs flows left after the horizon), so the backend takes it as cash: ``total = received - pv_start``
    (every coupon and redemption paid in (start, horizon], not reinvested), ``coupon`` = the coupons received minus the
    accrued bought at the start, ``pull`` the rest (the redemption against the clean price), ``rolldown`` 0."""


def horizon_bonds(market: MarketSnapshot, requests: list[BondRequest], horizon_date: date, *,
                  cache: ShockedCurveCache | None = None, foreign: dict[str, tuple[str, ...]] | None = None,
                  quote_curve_risk: bool = False, ofz_basis: bool = True
                  ) -> tuple[dict[str, BondCarry | Rejected], PortfolioHorizonResult | None]:
    """Carry and roll-down of ``requests`` from ``market``'s business date to ``horizon_date``: one core
    ``horizon_portfolio`` call on the inputs ``value_bonds`` values them on (``core_call``), each spread calibrated
    to its quote and held, the curves static (``BondCarry``). A bond whose last flow is paid by the horizon, which core
    refuses (it carries a bond only while it has flows left after the horizon), is carried as cash by the backend
    (``_redeemed``, ``BondCarry.method`` ``redeemed``). Any other refusal before core or by core is a ``Rejected``
    (``core_<code>``). Returns the core result too (None without a request core takes)."""
    call = core_call(market, requests, foreign=foreign, quote_curve_risk=quote_curve_risk, ofz_basis=ofz_basis)
    out: dict[str, BondCarry | Rejected] = dict(call.out)
    if call.policy is None:
        return out, None
    result = horizon_portfolio(snapshot=call.snapshot, instruments=list(call.instruments.values()),
                               positions=call.positions, policy=call.policy, horizon_date=horizon_date,
                               curve_cache=cache)
    positions = {p.position_id: p for p in call.positions}
    for rej in result.coverage.rejections:
        if "redeemed before the horizon" in rej.message:  # all its flows by the horizon: carried as cash
            p = positions[rej.position_id]
            try:
                out[rej.position_id] = _redeemed(call.prepared[p.instrument_id], p, call.snapshot, horizon_date)
                continue
            except (CoreRejection, ValueError) as e:
                rej = replace(rej, message=f"{rej.message}; as cash: {e}")
        out[rej.position_id] = Rejected(f"core_{rej.code.lower()}", rej.message)
    for h in result.positions:
        try:
            out[h.position_id] = _carry(call.prepared[h.instrument_id], h, call.snapshot)
        except (CoreRejection, ValueError) as e:  # a floater's accrued at the horizon (its projection)
            out[h.position_id] = Rejected("core_accrued_failed", str(e))
    return out, result


def _redeemed(prep: _Prepared, p: Position, market: MarketSnapshot, horizon_date: date) -> BondCarry:
    """``BondCarry`` of a bond whose last flow is paid by the horizon (``method`` ``redeemed``): its flows to the horizon
    (``_Prepared.flows``, the redemption at its offer or call when it is priced to one; a floater's projected on the
    run's curves) received as cash against its dirty value at the start, as core prices it (the quote's clean price on
    the outstanding face plus the quote's accrued, else core's own)."""
    start = market.business_date  # core settles a bond on the trade date (settlement lag 0, module docstring)
    quote = market.quote(p.instrument_id)
    accrued = quote.accrued_per_bond if quote.accrued_per_bond is not None else prep.accrued
    dirty = clean_price_pct_to_dirty_rub(quote.clean_price_pct, prep.instrument.outstanding_face(start), accrued)
    flows = [f for f in prep.flows if start < f.payment_date <= horizon_date]
    if not flows or any(f.payment_date > horizon_date for f in prep.flows):
        raise ValueError("flows left after the horizon")
    q = p.quantity
    received = math.fsum(f.amount for f in flows) * q
    coupon = (math.fsum(c for d, c in prep.coupons if start < d <= horizon_date) - accrued) * q
    total = received - dirty * q
    last = max(f.payment_date for f in flows)
    return BondCarry(p.position_id, total, coupon, total - coupon, 0.0, total, received, dirty * q, last, last, None,
                     "redeemed")


def _carry(prep: _Prepared, h: PositionHorizon, market: MarketSnapshot) -> BondCarry:
    b, q = h.per_bond, h.quantity
    start, end = b.accrued_start_rub, b.accrued_end_rub
    if start is None or end is None:  # a floater: core leaves its accrued to the caller
        start = prep.instrument.accrued(b.settlement_date, market)
        end = prep.instrument.accrued(b.horizon_settlement_date, market)
    coupon = (b.coupons_received_rub + end - start) * q
    return BondCarry(h.position_id, h.horizon_pnl_rub, coupon, h.carry_rub - coupon, h.rolldown_rub, h.carry_rub,
                     h.received_rub, b.pv_start_rub * q, b.horizon_settlement_date, b.redemption_date, b.spread_bp)


def _foreign_id(curve_id: str) -> bool:
    """A foreign curve of ``curves.fx`` (its ID is its published name, ``CNY FX``): not a RUB bond's input."""
    return " " in curve_id


def foreign_curve_note(v: PositionValuation, proxy: bool) -> str | None:
    """The assumption of a foreign bond on a proxy or a fallback curve (core ``CurveUse.proxy``,
    ``curve_fallback_from``); None on its own curve (CNY on XCCY)."""
    parts = []
    if v.curve_fallback_from:
        parts.append(f"curve {v.curve_fallback_from} absent: discounted on the fallback {v.curve_id}")
    if proxy:
        parts.append(f"discounted on {v.curve_id}: a government par-yield proxy, not OIS (proxy_curves_for_risk)")
    return "; ".join(parts) or None


def _valuation(prep: _Prepared, v: PositionValuation, quantity: float, guard: bool = False,
               disc_note: str | None = None) -> BondValuation:
    r = prep.request
    notes = [*prep.notes, *([disc_note] if disc_note else [])]
    inst = prep.instrument
    floater = None
    if isinstance(inst.coupon_leg, FloatingCouponLeg):
        offer = inst.horizon(v.horizon, v.settlement_date)[1] if v.horizon in ("confirmed_put", "open_put") else None
        tail = None
        if v.horizon == "perpetual":
            leg, tail = floating_perpetual_tail(inst.coupon_leg, v.settlement_date)
            inst = replace(inst, coupon_leg=leg)
        floater = FloaterModel(inst, offer, v.horizon_date, CAP_FLOOR_GUARD_BP if guard else None, tail)
    first = v.factor_risk.first_order_by_factor()
    hess = v.factor_risk.hessian_by_factor() if v.factor_risk.has_second_order else {}
    ruonia: dict[str, float] = {}
    if v.curve_basis == OFZ_BASIS_CURVE_BASIS:
        ruonia = {b: first.get(f"IR|{RUONIA_OIS_CURVE_ID}|{b}", 0.0) for b in RISK_BUCKETS}
        first, hess = ofz_levels(first, hess)
    by_curve: dict[str, dict[str, float]] = {}
    for f, x in first.items():
        if f.startswith("IR|"):
            _, c, b = _factor(f).split("|")
            by_curve.setdefault(c, dict.fromkeys(RISK_BUCKETS, 0.0))[b] += x
    buckets = {b: math.fsum(bs[b] for bs in by_curve.values()) for b in RISK_BUCKETS}
    if v.horizon_date != r.schedule.horizon_date and v.horizon != "perpetual":  # core's open_put chose another offer
        # than rule F2 (not expected); a floating perpetual's is its continued leg's end
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
        floater=floater, perpetual_tail=prep.tail, g_spread_bp=v.g_spread_bp, discount_margin_bp=v.discount_margin_bp,
        spread_buckets=dict(zip(RISK_BUCKETS, v.spread_buckets_rub)) if v.spread_buckets_rub else {},
        fx_rate=(v.currency_amounts.currency, v.currency_amounts.rub_per_unit) if v.currency_amounts else None,
        ofz_ruonia=ruonia,
    )
    if v.factor_risk.has_second_order:
        # Core's full Hessian over its factors, renamed to the published curves; every curve is its own factor,
        # so a floater keeps the cross terms of its discount and projection curves.
        for (a, b), x in hess.items():
            fa, fb = sorted((_factor(a), _factor(b)))
            out.hessian.append((fa, fb, x))
        # every curve moved in parallel together: the convexity of the yield
        rates = math.fsum(x * (1 if a == b else 2) for a, b, x in out.hessian
                          if a.startswith("IR|") and b.startswith("IR|"))
        out.curve_convexity = rates * 1e8 / v.pv_rub if v.pv_rub else None
    return out


def ofz_levels(first: dict[str, float], hessian: dict[tuple[str, str], float]
               ) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """An OFZ's risk in RUONIA + basis (core factors ``r_b = IR|RUB_RUONIA_OIS|b``, ``q_b = IR|RUB_OFZ_BASIS|b``, ``CS``)
    in the scenarios' coordinates: RUONIA ``r`` and the KBD's par level ``o = r + q`` (``IR|RUB_KBD|b``, published
    ``RUB OFZ``). The change of variables ``q = o - r`` is linear, so the first order and the Hessian are exact in both:
    ``dV/do = dV/dq`` (the basis risk, the KBD's par risk), ``dV/dr|o = dV/dr - dV/dq``, ``H' = M^T H M``.

    Why (TDM#213): a scenario and the Taylor move the published curves (``scenario.definition``), where ``RUB OFZ`` is
    the KBD's par yields and a move of all rates shifts ``RUB OFZ`` and ``RUB RUONIA`` alike; in these coordinates the
    move is RUONIA by ``r`` and the basis by ``o - r`` (``scenario.exact.core_scenario``), and the sum of a row's risk
    over the curves is its RUONIA risk, what a move of all rates does (no factor counted twice). ``hessian``: core's
    ``hessian_by_factor`` (each pair once); the result likewise (the Hessian's zeros left out)."""
    kbd = f"IR|{KBD_CURVE_ID}|"

    def parts(f: str) -> tuple[tuple[str, float], ...]:  # a coordinate y as the core factors z: z = M y
        if f.startswith(f"IR|{RUONIA_OIS_CURVE_ID}|"):
            b = f.rsplit("|", 1)[1]
            return (f, 1.0), (f"IR|{OFZ_BASIS_ID}|{b}", -1.0)
        if f.startswith(kbd):
            return ((f"IR|{OFZ_BASIS_ID}|{f.rsplit('|', 1)[1]}", 1.0),)
        return ((f, 1.0),)

    def y_of(f: str) -> str:
        return kbd + f.rsplit("|", 1)[1] if f.startswith(f"IR|{OFZ_BASIS_ID}|") else f

    ys = sorted({y_of(f) for f in first} | {y_of(f) for ab in hessian for f in ab}
                | {f"IR|{RUONIA_OIS_CURVE_ID}|{f.rsplit('|', 1)[1]}" for f in first if f.startswith(f"IR|{OFZ_BASIS_ID}|")})
    sym: dict[tuple[str, str], float] = {}
    for (a, b), x in hessian.items():
        sym[a, b] = sym[b, a] = x
    g = {y: math.fsum(c * first.get(z, 0.0) for z, c in parts(y)) for y in ys}
    h = {}
    for i, a in enumerate(ys):
        for b in ys[i:]:
            x = math.fsum(ca * cb * sym.get((za, zb), 0.0) for za, ca in parts(a) for zb, cb in parts(b))
            if x:
                h[a, b] = x
    return g, h


def _factor(name: str) -> str:
    """Core ``IR|<curve>|<bucket>`` -> ``IR|<published curve>|<bucket>`` (``RISK_CURVES``)."""
    if not name.startswith("IR|"):
        return name
    _, c, b = name.split("|")
    return f"IR|{RISK_CURVES.get(c, c)}|{b}"
