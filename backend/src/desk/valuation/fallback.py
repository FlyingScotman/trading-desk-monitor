"""Fallback ladder for bonds core could not value (slice S4).

Owner's rule: a bond is never left unvalued; better valued somehow than not at all. Each rung
below exact sets ``quality`` and says why in ``assumptions``; ``approximate`` returns None only
when there is no data at all (no terms, no face, no FX rate for a foreign face).

==============  ==============================================================================
Rung            Value
==============  ==============================================================================
market_value    a price today: q * (clean % * face / 100 + accrued). Accrued from the quote,
                else from the current coupon (rate * face * days / 365), else 0
stale_price     the same with the last price of the previous ``LOOKBACK_DAYS`` calendar days
model_price     no price, a usable fixed schedule: priced on the discount curve (RUONIA OIS, the KBD on a
                date without it, ``EodContext.discount``) at the Z-spread to that curve carried from
                the latest earlier run (its model spread, or the bond's last market Z if newer;
                desk store, S5), so the price moves with the curve only; without one at the
                median Z of today's traded exact-priced bonds of the issuer, else of the bond
                class, else of the kind (computed only when a bond needs it)
par_proxy       nothing else: q * (current face + accrued if known), no risk
reported_value  an instrument the static snapshot does not know (an unmapped bank-report row,
                ``valuation.positions``): the report's own mv_rub or mark price x face, no risk.
                Last: every rung above is our valuation of a bond we know; this is a figure we
                cannot check against terms or the market
==============  ==============================================================================

A bond refused only for want of a price of its own today (``no_price``, ``no_quote``; for a foreign face ``stale_quote``,
a quote of an earlier day, too) is valued by core's exact path at the
price of ``market_value`` / ``stale_price`` instead, with that quality (``at_last_price``, TDM#383, TDM#619): a floater's
projection, OFZ basis and a perpetual's tail, which no duration estimate has, core's accrued, and ``orchestrator.horizon``
carries it from the same value. The rungs' own risk below is for the bonds core refuses.

A bond with a foreign face has its currency's curve in the run (CNY on XCCY, USD / EUR on the par-yield proxies with
``proxy_curves_for_risk``: ``orchestrator.foreign_bond_curves``) and core values it on it at its last price (TDM#619): its
yield, its Z-spread to the curve (the same ``solve_z_spread`` and ``SPREAD_CONVENTION`` as at today's price) and its risk
on the curve, the price's age in the assumptions. Where core cannot (no curve of the currency, or a refusal of its own) the
ladder values it below: the yield is the bond's own from the last price and the flows (``own_from_flows``), never the
source's (MOEX's stays in the quote for a comparison), the spread is empty with its reason
(``extra.spread_missing``: ``no_foreign_curve``, ``core_refused``), and a bond with no price in ``LOOKBACK_DAYS`` has
neither, each with its reason (``extra.yield_missing``, ``extra.spread_missing``: ``no_price``). The ladder's own
engine is for a RUB bond (its flows, its curve, its RUB price); a foreign bond's flows have no curve in it.
The date rule of the Z-spread at an old price (TDM#619): the clean price is the last one, the curve and the accrued
interest are those of the settlement date (the business date). A foreign bond the ladder values has no yield of its own to
modify its duration by only when it has no schedule to compute one from: then the rate of its currency's curve at the
duration, else zero, said (``extra.risk_assumption``, ``RISK_ASSUMPTION``).
The reasons of an empty yield or spread (``extra.yield_missing``, ``extra.spread_missing``) are written for a foreign bond
at every rung and for a rouble bond on ``model_price``, ``par_proxy`` and a ladder failure; a rouble bond on
``market_value`` / ``stale_price`` without a Z (a floater or a refused schedule) has none yet: the coverage of the reasons
is partial for rouble bonds.

A model price is a question of the price's source, not of its attribution (the owner's decision of 29.10, TDM#441): a
RUB bond on ``model_price`` for the same want of a price of its own is valued by core at the model's price in the same
way (``at_model_price``): core's flows, risk and accrued, quality kept, the spread's source in the assumptions, carried
by ``orchestrator.horizon`` at that price. The model is not circular: core's Z at the model's price must give back the
Z the price was built from (``MODEL_Z_TOLERANCE_BP``; the price is the same engine's on the same curve, schedule and
accrued), so a carried spread (``CarriedSpread``) is carried unchanged; else the ladder's own model price stays, said.

Risk of the priced rungs: with a usable fixed schedule, core ``BondRiskEngine`` calibrated to the
dirty price (bucket DV01 and spread risk on the discount curve, as for exact bonds: the KBD for ``OFZ_KBD_CLASSES``,
``bond_discount``). Otherwise a duration estimate: MOEX ``duration_days``, else the time to the horizon; DV01 =
-Macaulay / (1 + y) * PV * 1e-4 (the modified duration: core's buckets bump par quotes and the Z-spread is annual;
until 0.2.5 -Macaulay * PV, about 15 % more, #36) split linearly between the two nearest pillars; the spread risk
(CS01 for credit, the spread to the KBD for an OFZ) the same at the full duration. A floater's rates duration is the time to its next reset (its CS01
keeps the full duration); a key-rate floater books it on two curves as core does an exact one
(``key_rate_floater_proxy``, R15): the full duration on the discount curve, the projection on ``RUB KEY``. A bond with a foreign face is valued in RUB at the CBR rate (MOEX TOM
when CBR is missing); a bond core did not take has no curve in the ladder, so its rate risk is the duration
estimate (DV01 in RUB per bp, modified by the bond's own yield, else said at a zero rate; the duration is its own from the
flows and the price, TDM#619, else the source's or the time to the horizon), booked on the currency's curve (``bonds.ccy_curve``: ``USD``, ``CNY``...)
instead of RUONIA. MOEX publishes the accrued in the settlement currency (RUB for most replacement
bonds) and the price in % of the foreign face.

Yield and duration of a bond whose MOEX fields cannot be trusted (TDM#764, TDM#768). MOEX's effective yield and duration
are read for the yield shown, the duration estimate and the carry. Not when the hub says they were computed on a wrong
redemption schedule (``BondQuote.source_analytics_unreliable``, ``BondTerms.source_analytics_unreliable_reasons``,
contract 1.10: ``unreliable_source``): then, if the bond has a complete fixed schedule (``_schedule``: ``fixed_schedule``
accepted it, no label of a broken one, known coupons, not a floater) and its price is on the schedule's face, the
effective annual yield and the Macaulay duration are computed here from the flows after the date and the dirty price with
core's ``solve_effective_annual_yield`` and ``bond_duration`` in the bond's yield convention (``own_analytics``; no curve
is needed, so a bond of a currency without one has them), the DV01 and CS01 follow from them as for any duration estimate
and the assumptions say so (``OWN_ANALYTICS_TEXT``). Without them (a broken schedule, a floater, a price that no yield
reproduces) the bond has no yield, duration or risk, with its reason (``BROKEN_SCHEDULE_TEXT``): no figure of MOEX's, no
close yield, no time to the horizon in their place.

Flash PnL of a position not valued by core on either date (``by_core``; a rung core valued at its price takes the exact
steps, ``pnl.flash``): ``simple_pnl``, q * (dirty_D - dirty_D-1) + received, all in ``other``, its reason in
``other_reason`` (``orchestrator.attach_pnl``, ``pnl.reasons``). No decomposition is faked: only a foreign-face bond
priced on both dates gets its carry at its own yield (``yield_pnl``, batch 1 item 5), and its FX revaluation goes to
``fx_gross`` (``pnl.direct``).
"""

from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from itertools import chain
from typing import TYPE_CHECKING, Any, Self

from rub_fixed_income import (
    RISK_BUCKETS,
    RISK_PILLARS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    CoreRejection,
    PerpetualTail,
    YieldRequest,
    bond_duration,
    kbd_effective_yield,
    perpetual_tail,
    perpetual_yield,
    solve_effective_annual_yield,
)

from ..curves.rates import KEY_RATE_CURVE_ID
from ..marketdata.adapter import (
    KBD_CURVE_ID,
    OFZ_CLASSES,
    BondQuote,
    BondTerms,
    MarketView,
    bond_currency,
    core_instrument,
    settlement_currency,
)
from ..pnl.flash import FlashPnl, NoPnl
from ..pnl.reasons import YIELD_NO_CURVE, other_reason
from .bonds import (
    OFZ_KBD_CLASSES,
    PRICE_BASIS_ORDER,
    REDEMPTIONS_LABEL,
    RISK_CURVES,
    ROUNDED_LABEL,
    SPREAD_CONVENTION,
    BondRequest,
    BondValuation,
    Rejected,
    bond_request,
    ccy_curve,
    coupon_view,
    current_period,
    face_on,
    fixed_schedule,
    next_offer,
    settlement_date,
    value_bonds,
)

if TYPE_CHECKING:
    from ..orchestrator import EodContext, Outcome

FALLBACK_BASES = (*PRICE_BASIS_ORDER, "market_price2", "market_price3", "admitted_quote")
KEY_CURVE = RISK_CURVES[KEY_RATE_CURVE_ID]
"""The key-rate projection curve (``RUB KEY``): a key-rate floater on a fallback rung books its projection here."""
LOOKBACK_DAYS = 30
KBD_SHORT = ("1M", "3M", "6M")
"""Buckets of the KBD that carry no risk (``rub_ofz_kbd_v3``: no OFZ there, merged into 1Y): a duration proxy on the KBD
moves them into 1Y, as core's par buckets do."""
MIN_BONDS = {"issuer": 3, "class": 3, "kind": 3}
Z_BAND_BP = (-100.0, 2000.0)
"""Z-spreads outside are distressed names or bad prints, not a spread level for a model price."""
MIN_HORIZON_DAYS = 30
"""Bonds closer to their horizon stay out of the medians: there a kopeck of price is many bp of Z."""
METHOD = "desk-flash-simple-v1"
QUALITIES = ("exact", "assumed", "market_value", "stale_price", "model_price", "par_proxy", "reported_value")
RUNGS = QUALITIES[2:]
"""The fallback rungs: flash PnL by ``simple_pnl`` (``reported_value`` has none) unless valued by core on both dates
(``by_core``: at a price rung's or the model's price, ``at_last_price``, ``at_model_price``)."""
STALE_NOTE = "last price {date}, {days} days old ({basis})"
"""The assumption of a valuation at an earlier day's price (quality ``stale_price``); ``stale_note`` writes it and
``STALE_NOTE_DATE`` reads the date back (``pnl.new_trades.mark_of``: the date of a trade's valuation price)."""
STALE_NOTE_DATE = re.compile(re.escape(STALE_NOTE).replace(r"\{date\}", r"(\d{4}-\d{2}-\d{2})").split(r"\{days\}")[0])


def stale_note(price_date: date, bd: date, basis: str) -> str:
    """``STALE_NOTE`` of the price of ``price_date`` used on ``bd``."""
    return STALE_NOTE.format(date=price_date, days=(bd - price_date).days, basis=basis)


MODEL_BASIS = "model"
"""The price basis of a model price (``BondValuation.price_basis``; core's price observation at it)."""
MODEL_Z_TOLERANCE_BP = 1e-3
"""Core's Z at a model's price against the Z the price was built from (``at_model_price``): beyond it the ladder's own
model price stays (the price would not be the spread's)."""


def by_core(v: BondValuation) -> bool:
    """Valued by core (its instrument and flows on the valuation): the exact path, or a fallback rung at its price."""
    return v.instrument is not None and bool(v.cashflows)


@dataclass(frozen=True)
class CarriedSpread:
    """The Z-spread a model price used in the run of ``business_date`` (desk store ``model_spread``,
    slice S5). A later run carries it, so the model price moves with the curve, not the day's median."""

    secid: str
    z_spread_bp: float
    business_date: date
    source: str
    curve: str | None = None
    """The published curve the spread is to (``BondValuation.spread_curve``, a run's ``spread_curve``): a model price
    reuses only a spread to its own discount curve (a run before the RUONIA switch had spreads to the KBD)."""


SPREAD_EPS_BP = 1e-6
"""Two Z-spreads this close (a millionth of a basis point) are one: a model price valued by core at a carried spread gives
back the spread it was priced at to the last digits but one, not always to the last bit."""


def used_model_spreads(outcomes: list[Outcome]) -> list[CarriedSpread]:
    """The spreads the model-price valuations of ``outcomes`` used, one per secid, for the store."""
    out = {o.secid: CarriedSpread(o.secid, v.z_spread_bp, v.valuation_date, v.model_spread_source, v.spread_curve)
           for o in outcomes if (v := o.valuation) and v.quality == "model_price" and v.model_spread_source
           and math.isfinite(v.z_spread_bp)}
    return list(out.values())


@dataclass
class FallbackContext:
    """Per-run inputs of the ladder, computed on first need: earlier market snapshots and the
    median Z-spreads of the exact-priced universe."""

    ctx: EodContext
    history: list[MarketView] | None = None
    """Market snapshots of the previous ``LOOKBACK_DAYS`` calendar days, newest first."""
    medians: dict[tuple[str, Any], tuple[float, int]] | None = None
    carried: Mapping[str, CarriedSpread] = field(default_factory=dict)
    """Model spreads of the latest earlier run, by secid (empty without a desk store)."""

    @classmethod
    def build(cls, ctx: EodContext, carried: Mapping[str, CarriedSpread] | None = None) -> FallbackContext:
        # a lazy mapping (``desk.parallel.LazyCarried``: read from the store at its first lookup, after the earlier
        # dates are published) is kept as it is: a copy would read it at once
        if carried is not None and not isinstance(carried, dict):
            return cls(ctx, carried=carried)
        return cls(ctx, carried=dict(carried or {}))

    def past_quotes(self, terms: BondTerms) -> Iterator[BondQuote]:
        if self.history is None:
            # the closes of the earlier dates, also for a run on an intraday or preliminary cut (R11)
            bd, cut = self.ctx.business_date, "eod_final"
            dates = [d for d in self.ctx.snapshots.market_dates(cut)
                     if bd - timedelta(LOOKBACK_DAYS) <= date.fromisoformat(d) < bd]
            self.history = [MarketView(self.ctx.snapshots.market(d, cut)) for d in reversed(dates)]
        for m in self.history:
            if (q := m.quote(terms)) is not None:
                yield q

    def median_z(self, terms: BondTerms, kind: str, curve: str) -> tuple[float, str] | None:
        """The day's median Z of the bond's issuer, else class, else spread kind, over the exact bonds discounted on the
        same core curve ``curve`` (an OFZ-PD's Z is to the KBD, an OFZ-PK's to RUONIA: not one level)."""
        if self.medians is None:
            self.medians = _medians(self.ctx)
        for key, label in ((("issuer", terms.issuer_id, curve), "issuer"),
                           (("class", terms.bond_class, curve), terms.bond_class), (("kind", kind, curve), kind)):
            if key in self.medians:
                z, n = self.medians[key]
                return z, f"{label} median Z {z:.0f} bp ({n} bonds)"
        return None


def _medians(ctx: EodContext) -> dict[tuple[str, Any, Any], tuple[float, int]]:
    reqs = []
    for t in ctx.static.bonds.values():
        q = ctx.market.quote(t)
        if q is None or q.trade_date != ctx.business_date or not q.num_trades:
            continue  # an untraded bond's legal close is an old price: its Z is no evidence of today
        try:
            reqs.append(bond_request(t.secid, t, q, q.trade_date, 1.0))
        except Rejected:
            continue
    valued, _ = value_bonds(ctx.core_market, reqs, cache=ctx.cache)
    groups: dict[tuple[str, Any, Any], list[float]] = defaultdict(list)
    for r in reqs:
        v = valued.get(r.position_id)
        if isinstance(v, BondValuation) and v.quality == "exact" and math.isfinite(v.z_spread_bp) \
                and Z_BAND_BP[0] <= v.z_spread_bp <= Z_BAND_BP[1] \
                and (v.horizon_date - ctx.business_date).days >= MIN_HORIZON_DAYS:
            c = v.discount_curve_id
            for k in (("issuer", r.terms.issuer_id, c), ("class", r.terms.bond_class, c), ("kind", v.spread_kind, c)):
                if k[1] is not None:
                    groups[k].append(v.z_spread_bp)
    return {k: (statistics.median(zs), len(zs)) for k, zs in groups.items() if len(zs) >= MIN_BONDS[k[0]]}


FX_MARKET = (("moex_19", "MOEX 19:00"), ("moex_14", "MOEX 14:00"), ("cets_close", "CETS close"), ("cbr", "CBR"))
"""The market rate of a currency (owner's decision of 21.10, R26; ORG_CONVENTIONS §2.2, DATA_CONTRACT §0.2), first
present: the MOEX indicative rate at 19:00 MSK of the business date, else at 14:00, else the CETS TOM close, else the
CBR rate in force. Its label is the rate's ``source`` shown next to it."""


def fx_rate(ctx: EodContext, ccy: str) -> tuple[float, str] | None:
    """RUB per unit at the market rate (``FX_MARKET``) and its source: what foreign-currency bonds and FX rows are
    valued at and their flash PnL is measured in (the CBR rate in force stays beside it: ``fx_rate_cbr``)."""
    if ccy == "RUB":
        return 1.0, ""
    return market_fx(ctx.market.fx(ccy))


def market_fx(fx: dict[str, float | None]) -> tuple[float, str] | None:
    """The first rate of ``FX_MARKET`` in ``MarketView.fx`` and its source label."""
    return next(((fx[k], label) for k, label in FX_MARKET if fx.get(k)), None)  # type: ignore[misc]


def fx_rate_cbr(ctx: EodContext, ccy: str) -> tuple[float, str] | None:
    """RUB per unit at the CBR rate in force on the business date (set the business day before): the official PnL's
    rate (``fx_gross_cbr``); None without one."""
    if ccy == "RUB":
        return 1.0, ""
    r = ctx.market.fx(ccy)["cbr"]
    return (r, "CBR") if r else None


def _price(q: BondQuote) -> tuple[str, float] | None:
    return next(((b, float(q.prices_pct[b])) for b in FALLBACK_BASES if q.prices_pct.get(b, 0) > 0), None)


OWN_PRICE_MISSING = ("no_price", "no_quote")
"""The exact path's refusals ``at_last_price`` takes: no price basis of its own today, or no quote today at all."""
FOREIGN_PRICE_MISSING = (*OWN_PRICE_MISSING, "stale_quote")
"""The same for a bond with a foreign face (TDM#619): a quote of an earlier day is the same want of a price of its own
today, and the MOEX rows of a replacement bond that did not trade carry the last trade's date."""


def price_missing(terms: BondTerms) -> tuple[str, ...]:
    """The exact path's refusals ``at_last_price`` takes for ``terms``' bond."""
    return OWN_PRICE_MISSING if bond_currency(terms) == "RUB" else FOREIGN_PRICE_MISSING


def last_price(outcome: Outcome, fb: FallbackContext) -> tuple[BondQuote, str, float] | None:
    """The ladder's price of ``outcome``'s bond: today's quote with a price of ``FALLBACK_BASES``, else the latest
    earlier close of the previous ``LOOKBACK_DAYS`` calendar days (the history is loaded only when today's quote has
    none): (its quote, basis, clean %), or None."""
    bd = fb.ctx.business_date
    for cand in chain([outcome.quote] if outcome.quote else [], fb.past_quotes(outcome.terms)):  # type: ignore[arg-type]
        if (bd - cand.trade_date).days > LOOKBACK_DAYS:
            break
        if (p := _price(cand)) is not None:
            return cand, *p
    return None


def at_last_price(ctx: EodContext, position_id: str, outcome: Outcome, quantity: float, fb: FallbackContext
                  ) -> tuple[BondRequest, str, tuple[str, ...]] | None:
    """A bond the exact path refused only for want of a price of its own today (``price_missing``: an intraday
    cut before its first trade, a day it did not trade, for a foreign face an earlier day's quote too) as a request core
    values exactly at the ladder's price
    (``last_price``): today's quote (its face and accrued) with that price, else the last close's quote as of today
    (face and accrued from the static). Returns the request, its rung (``market_value``: a price of today,
    ``stale_price``: an earlier close) and the notes, or None (another refusal, no price).

    Why (TDM#383): a floater has no fixed schedule, so the ladder's own risk is a duration estimate, rates only to its
    next coupon on the discount curve: a KBD floater lost its KBD risk and the OFZ basis, a perpetual its tail, and
    the carry (core's, ``orchestrator.horizon``) was none. The same bond at the same price through core has them all.
    A fixed coupon had core's risk on its schedule already, but the ladder's own accrued (rate x days / 365), so its
    value was not the one core carries from (``scenario.carry``: ``base_mismatch``)."""
    t = outcome.terms
    if t is None or outcome.reason not in price_missing(t) or (found := last_price(outcome, fb)) is None:
        return None
    cand, basis, clean = found
    bd = ctx.business_date
    quote = (outcome.quote if outcome.quote is not None and outcome.quote.trade_date == bd
             else replace(cand, trade_date=bd, face_value=None, accrued_per_bond=None, num_trades=None,
                          source_analytics={}, volume_bonds=None, value=None))
    quality = "market_value" if cand.trade_date == bd else "stale_price"
    notes = [f"not exact: {outcome.reason}{': ' + outcome.detail if outcome.detail else ''}",
             stale_note(cand.trade_date, bd, basis)
             if quality == "stale_price" else f"price basis {basis}",
             "valued by core at this price"]
    try:
        settle = settlement_date(bd, t.settle_lag_days, ctx.calendar, *ctx.calendar_window)
        req = bond_request(position_id, t, quote, settle, quantity, price=(basis, clean))
    except Rejected:
        return None
    return req, quality, tuple(notes)


def at_model_price(ctx: EodContext, position_id: str, outcome: Outcome, quantity: float, clean_pct: float
                   ) -> BondRequest | None:
    """A RUB bond the exact path refused only for want of a price of its own (``OWN_PRICE_MISSING``) as a request core
    values exactly at the model's clean price ``clean_pct`` (``approximate``'s ``model_price``, or a stored run's,
    ``orchestrator.horizon``), TDM#441: today's quote if any, else a quote of no price dated today, with no MOEX
    accrued either way: the model's dirty price is core's PV on the bond's schedule, so its clean is that less core's
    own accrued, which core then adds back. None: another refusal, a foreign face, terms core's request refuses.

    Why: the model price had core's risk on its fixed schedule already, but no flows on the valuation, so its flash
    PnL was the dirty price's move, all in ``other`` (``simple_pnl``), and it had no carry; the same bond at the same
    price through core has its exact flash steps (``pnl.flash``) and core's carry."""
    t = outcome.terms
    if t is None or outcome.reason not in OWN_PRICE_MISSING or bond_currency(t) != "RUB":
        return None
    bd, q = ctx.business_date, outcome.quote
    quote = (replace(q, accrued_per_bond=None) if q is not None and q.trade_date == bd
             else BondQuote(t.secid, t.primary_board or "", bd, None, None, {}, None, {}))
    try:
        settle = settlement_date(bd, t.settle_lag_days, ctx.calendar, *ctx.calendar_window)
        return bond_request(position_id, t, quote, settle, quantity, price=(MODEL_BASIS, clean_pct))
    except Rejected:
        return None


def model_notes(model: BondValuation) -> tuple[str, ...]:
    """The assumptions of a model price valued by core (``at_model_price``): why it is not exact and the model's spread
    and its source, as the ladder said them, then ``valued by core at this price``."""
    why = tuple(a for a in model.assumptions if a.startswith(("not exact: ", "model price: ")))
    return (*why, "valued by core at this price")


def _accrued(t: BondTerms, d: date, face: float) -> tuple[float, str] | None:
    """Accrued per bond in face currency from the current coupon, ACT/365 on the rate."""
    c, start = current_period(t, d)
    if c is None or start is None or start > d:
        return None
    days = (d - start).days
    if c.rate_pct is not None:
        return float(c.rate_pct) / 100 * face * days / 365, f"accrued computed: {c.rate_pct}% x {days}/365"
    if c.amount_per_bond is not None and c.pay_date > start:
        return (float(c.amount_per_bond) * days / (c.pay_date - start).days,
                f"accrued computed: coupon {c.amount_per_bond} x {days}/{(c.pay_date - start).days}")
    return None


def carry_price(src: BondValuation, terms: BondTerms, ctx: EodContext, d: date, quantity: float | None = None
                ) -> BondValuation | None:
    """``src`` (a valuation of another date) carried to ``d``: its CLEAN price stands, the face and the accrued interest
    are those of ``d`` (``face_on``, ``_accrued`` at the rates of ``ctx``), so that ``simple_pnl`` over the carried and
    the real valuation is the accrual of the day plus the payments of (D-1, D], never a coupon twice (TDM#576). None
    without a clean price, a face, a rate or an accrual on ``d``."""
    if not math.isfinite(src.clean_pct):
        return None
    face = face_on(terms, d)
    fx_f = fx_rate(ctx, bond_currency(terms))
    if face is None or float(face) <= 0 or fx_f is None:
        return None
    acc = _accrued(terms, d, float(face))
    if acc is None:
        return None
    face_rub, acc_rub = float(face) * fx_f[0], acc[0] * fx_f[0]
    q = src.quantity if quantity is None else quantity
    dirty = src.clean_pct / 100 * face_rub + acc_rub
    return replace(src, quantity=q, valuation_date=d, settlement=d, face_rub=face_rub, accrued_rub=acc_rub,
                   dirty_rub=dirty, pv_rub=dirty * q)


def _schedule(t: BondTerms, d: date) -> tuple[Any, tuple[BondCashflow, ...], Any, PerpetualTail | None] | None:
    """Fixed-coupon schedule to the horizon (as ``valuation.bonds`` builds it), its flows, the core instrument and a
    never-called perpetual's tail (core ``perpetual_tail``, TDM#35; None for a dated bond), or None."""
    try:
        s = fixed_schedule(t, d)
        if s.floater is not None:
            return None
        inst = core_instrument(t, d, s.horizon_date, s.offer, coupons=s.coupons)
        flows = inst.cashflows(after=d, offer=inst.horizon("open_put", d)[1] if inst.offers else None)
        tail = perpetual_tail(inst.coupon_leg.periods) if s.horizon_kind == "perpetual" else None  # type: ignore[union-attr]
    except (Rejected, CoreRejection):
        return None
    return s, tuple(BondCashflow(f.payment_date, f.amount) for f in flows), inst, tail


def bond_discount(ctx: EodContext, terms: BondTerms) -> tuple[str, Any, str | None]:
    """The core curve a RUB bond of the ladder is discounted on, as on the exact path (``bonds.value_bonds``): the KBD
    for ``OFZ_KBD_CLASSES``, else the run's (``EodContext.discount``: RUONIA OIS, the KBD on a date without it); what
    core's risk engine takes for it (the KBD built with par buckets, RUONIA as its OIS bootstrap request:
    ``EodContext.risk_curve``) and the note of a KBD fallback."""
    if terms.bond_class in OFZ_KBD_CLASSES:
        return KBD_CURVE_ID, ctx.curve, None
    cid, built, note = ctx.discount
    return cid, ctx.risk_curve(cid, built), note  # RUONIA by its OIS quotes, as on the exact path


def _engine(ctx: EodContext, curve: Any, flows: tuple[BondCashflow, ...], kind: str, q: float, *,
            spread_bp: float | None = None, dirty: float | None = None,
            tail: PerpetualTail | None = None) -> tuple[BondRiskEngine, Any]:
    e = BondRiskEngine(BondRiskRequest(cashflows=flows, settlement_date=ctx.business_date, curve=curve,
                                       spread_convention=SPREAD_CONVENTION,
                                       spread_kind=kind,  # type: ignore[arg-type]
                                       spread_bp=spread_bp, dirty_price_rub=dirty, quantity=q, perpetual_tail=tail),
                       curve_cache=ctx.cache)
    return e, e.risk(difference="central")


def _split(t: float, dv01: float) -> dict[str, float]:
    """DV01 at tenor ``t`` years, split linearly between the two nearest risk pillars."""
    out = dict.fromkeys(RISK_BUCKETS, 0.0)
    i = bisect_left(RISK_PILLARS, t)
    if i == 0 or i == len(RISK_PILLARS):
        out[RISK_BUCKETS[min(i, len(RISK_BUCKETS) - 1)]] = dv01
        return out
    w = (RISK_PILLARS[i] - t) / (RISK_PILLARS[i] - RISK_PILLARS[i - 1])
    out[RISK_BUCKETS[i - 1]], out[RISK_BUCKETS[i]] = dv01 * w, dv01 * (1 - w)
    return out


def key_rate_floater_proxy(disc: str, mac: float, reset: float, pv: float) -> dict[str, dict[str, float]]:
    """The duration proxy of a key-rate floater by curve, as core books an exact one (R15): every cashflow is
    discounted on ``disc`` (-Macaulay x PV at the full duration) and the coupons after the reset are projected on
    the key-rate curve (``KEY_CURVE``): +duration at the horizon, -time to the reset at the reset (the forwards
    between them). Summed over the curves the buckets are the single-curve proxy (-time to the reset x PV at the
    reset), so the total DV01 and every parallel shift of all curves are unchanged; only a move of the key rate
    against RUONIA (the basis) now moves the floater."""
    proj = {b: x + y for (b, x), y in zip(_split(mac, mac * pv * 1e-4).items(),
                                         _split(reset, -reset * pv * 1e-4).values())}
    return {disc: {b: x + 0.0 for b, x in _split(mac, -mac * pv * 1e-4).items()},
            KEY_CURVE: {b: x + 0.0 for b, x in proj.items()}}


def _f(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


SCHEDULE_REJECTIONS = ("redemption_incomplete",)
"""Reasons of the exact path (``Outcome.reason``) that say the same without the label: the schedule does not add up (or the
hub says so: ``bonds.fixed_schedule`` refuses a bond with its label as this)."""
BROKEN_SCHEDULE_TEXT = {
    "bond_redemptions_label": ("график погашений в данных источника не сходится с номиналом (метка хаба "
                               + REDEMPTIONS_LABEL + "): доходность, дюрация и DV01 не считаются"),
    "bond_redemptions_rounded": ("погашения в данных источника округлены, график не сходится с номиналом (метка хаба "
                                 + ROUNDED_LABEL + "): доходность, дюрация и DV01 не считаются"),
    "bond_redemptions_incomplete": ("график погашений не сходится с номиналом (расчёт нашёл погашения неполными, "
                                    "redemption_incomplete): доходность, дюрация и DV01 не считаются"),
    "bond_source_analytics_unreliable": ("доходность и дюрация источника посчитаны по искажённому графику погашений: "
                                         "не используются, DV01 не считается"),
}
"""Why a bond with a broken redemption schedule has no yield, duration or risk (TDM#764, the owner's rule: no
substitutes, a data problem is shown). MOEX's own effective yield and duration are computed on the same wrong schedule
(RUS-30: 53.8 % at a close yield of 6.8 %, 505 days against about 635), and neither the close yield nor the time to the
horizon stands in for them. Published as ``extra.risk_missing`` (``valuation.positions.RISK_MISSING`` takes these codes:
the risk table and the pages' «риска нет» say it), and in the assumptions. The value at the price stays.
``bond_source_analytics_unreliable``: the hub says (``BondQuote.source_analytics_unreliable``, contract 1.10) that the
yield and duration fields the ladder reads were computed on a wrong schedule, whatever the schedule now is."""
ANALYTICS_READ = ("effective_yield_pct", "yield_close_pct", "duration_days")
"""The ``source_analytics`` fields of MOEX the ladder reads (``_yield``, the duration estimate)."""
SCHEDULE_LABEL_CODES = {ROUNDED_LABEL: "bond_redemptions_rounded", REDEMPTIONS_LABEL: "bond_redemptions_label"}
"""The more specific label first: the hub's contract 1.10 puts ``ROUNDED_LABEL`` on a bond together with ``REDEMPTIONS_LABEL``."""


def broken_schedule(t: BondTerms, reason: str | None) -> str | None:
    """The ``BROKEN_SCHEDULE_TEXT`` code of a bond whose redemption schedule does not add up to its face: the hub's
    label (``BROKEN_REDEMPTION_LABELS``), else the exact path's rejection; None for any other bond. A manual override of
    the hub (``MANUAL_OVERRIDE_LABEL``) is not one."""
    code = next((c for lab, c in SCHEDULE_LABEL_CODES.items() if lab in t.labels), None)
    return code or ("bond_redemptions_incomplete" if reason in SCHEDULE_REJECTIONS else None)


def unreliable_source(quote: BondQuote | None, terms: BondTerms | None = None) -> str | None:
    """``bond_source_analytics_unreliable`` when the hub says MOEX's yield and duration of the bond cannot be trusted: a
    field of ``ANALYTICS_READ`` is among the quote's ``source_analytics_unreliable``, or the bond's own static lists
    ``source_analytics_unreliable_reasons`` (contract 1.10; a quote of an earlier snapshot, the last price of a stale
    rung, carries no flag, the static does). None otherwise, also for a hub that does not say."""
    u = quote.source_analytics_unreliable if quote else None
    if u and any(f in u["fields"] for f in ANALYTICS_READ):
        return "bond_source_analytics_unreliable"
    return "bond_source_analytics_unreliable" if terms is not None and terms.source_analytics_unreliable_reasons else None


OWN_ANALYTICS_TEXT = "доходность и дюрация посчитаны монитором по графику потоков и цене (кривой валюты нет)"
"""The assumption of a bond whose yield and duration are the monitor's own (``own_analytics``), a bond with a foreign
face: the ladder has no curve in its currency."""
OWN_ANALYTICS_TEXT_CURVE = ("доходность и дюрация посчитаны монитором по графику потоков и цене "
                            "(поля источника недостоверны, не используются)")
"""The same for a bond with a curve of its currency (a rouble bond): it is the source's fields that are unusable."""
OWN_ANALYTICS_TEXT_FX = ("доходность и дюрация посчитаны монитором по графику потоков и последней цене "
                         "(цифры источника — только для сверки)")
"""The same for a bond with a foreign face the ladder values (TDM#619): the rule of every bond, not a case of
untrustworthy source fields: the yield is the monitor's own whether the source published one or not."""


class OwnFailure(str):
    """Why ``own_analytics`` / ``own_from_flows`` have no numbers: the reason in Russian (the text) and its machine ``code``
    (``YIELD_MISSING``), a ``str`` so that what shows it as a text takes it as it was."""

    code: str

    def __new__(cls, text: str, code: str) -> Self:
        self = super().__new__(cls, text)
        self.code = code
        return self


@dataclass(frozen=True)
class OwnAnalytics:
    """Yield and duration of a bond computed from its flows and its price, ``own_analytics``."""

    yield_pct: float
    macaulay_years: float
    modified_years: float
    notes: tuple[str, ...] = ()
    """The notes of the schedule the flows come from (``FixedSchedule.assumptions``: coupons computed from a rate)."""


def own_analytics(sched: tuple[Any, tuple[BondCashflow, ...], Any, PerpetualTail | None], settlement: date,
                  dirty_face: float, face: float) -> OwnAnalytics | OwnFailure:
    """The effective annual yield and the Macaulay and modified durations of a bond from its remaining flows
    (``_schedule``) and its dirty price in its face currency (``dirty_face``), with core's own functions, in the bond's yield
    convention (``Instrument.yield_convention``, annual compounding, ACT/365F unless the issue says otherwise), as the exact
    path's ``ytm_pct`` is: no curve is needed (TDM#768, a bond of a currency without one). A perpetual never called: the yield
    of its endless stream (core ``perpetual_yield``). ``face``, the face the price is a percentage of, must be the one the
    flows are on. A text (why it could not be computed) instead of the numbers when there is none: a face that is not the
    schedule's, a price no yield reproduces."""
    fixed, flows, inst, tail = sched
    if not math.isclose(face, fixed.current_face, rel_tol=1e-6, abs_tol=1e-9):
        return OwnFailure(f"цена дана к номиналу {face:g}, потоки — на номинале {fixed.current_face:g}", "face_mismatch")
    conv = inst.yield_convention
    pairs = tuple((f.payment_date, f.amount) for f in flows)
    try:
        if tail is not None:
            r = perpetual_yield(settlement, dirty_face, pairs, tail, conv)
            return OwnAnalytics(r.effective_yield * 100, r.macaulay_duration_years, r.modified_duration_years,
                                fixed.assumptions)
        eff = solve_effective_annual_yield(YieldRequest(settlement, dirty_face, flows, conv)).effective_annual_yield
        mac = bond_duration(annual_yield=eff, settlement_date=settlement, cashflows=pairs, convention=conv).macaulay_years
    except (ValueError, RuntimeError, ArithmeticError, CoreRejection) as err:
        return OwnFailure(f"никакая доходность не воспроизводит цену на этих потоках ({err})", "no_solution")
    return OwnAnalytics(eff * 100, mac, mac / (1 + eff), fixed.assumptions)


OWN_NOT_COMPUTED = ". Собственный расчёт монитора не вышел: {}."
"""Appended to ``BROKEN_SCHEDULE_TEXT["bond_source_analytics_unreliable"]`` when the monitor's own yield and duration of a
bond with unreliable source fields could not be computed: its reason (``own_from_flows``)."""


def own_from_flows(t: BondTerms, d: date, dirty_face: float, face: float, *, allow_assumed: bool = False
                   ) -> OwnAnalytics | OwnFailure:
    """``own_analytics`` of bond ``t`` on ``d`` from ``_schedule``: the schedule the exact path accepts (``fixed_schedule``:
    the redemptions add up to the face, no label of a broken one) with the coupons it fills (``bonds._fill_coupons``: a coupon
    of a rate and no amount from the face of the period by the schedule and the static's day count). A text (the reason, in
    Russian: it is shown with the risk that is missing) instead of the numbers for a bond without a complete fixed schedule
    (a floater, a refused schedule), a coupon in the window that is not known (``assumed``,
    ``projected_by_moex``, ``unknown``: a coupon of a rate and no amount without the static's day count is ``assumed``), and
    for each case of ``own_analytics``. ``allow_assumed`` (a foreign bond on the ladder, TDM#619): the coupons the
    exact path fills in (``assumed``: at the last known rate, ``projected_by_moex``) are taken, as it takes them, and
    said in the notes; ``unknown`` ones, with no rate to take, still refuse."""
    sched = _schedule(t, d)
    if sched is None:
        try:
            fixed = fixed_schedule(t, d)
        except Rejected as err:
            return OwnFailure(f"график бумаги отклонён расчётом ({err.reason})", "schedule_rejected")
        return (OwnFailure("флоатер: купоны будущих периодов не известны", "floater") if fixed.floater is not None
                else OwnFailure("потоки графика не построены", "no_flows"))
    unknown = [c for c in sched[0].coupons if c.status == "unknown"]
    assumed = [c for c in sched[0].coupons if c.status == "assumed"]
    projected = [c for c in sched[0].coupons if c.status == "projected_by_moex"]
    if unknown or (not allow_assumed and (assumed or projected)):
        parts = [f"{len(assumed) + len(unknown)} подставлено" if assumed or unknown else "",
                 f"{len(projected)} спрогнозировано" if projected else ""]
        return OwnFailure("купоны не известны: " + ", ".join(x for x in parts if x), "coupons_unknown")
    return own_analytics(sched, d, dirty_face, face)


YIELD_MISSING = {
    "no_price": "доходности нет: у бумаги нет цены за последние " + str(LOOKBACK_DAYS) + " дней",
    "model_price": "доходности нет: цена бумаги модельная (рыночной цены нет), доходность по ней не считается",
    "schedule_rejected": "доходности нет: график потоков бумаги отклонён расчётом, по нему доходность не считается",
    "floater": "доходности нет: у флоатера купоны будущих периодов не известны",
    "no_flows": "доходности нет: график потоков бумаги не построен",
    "coupons_unknown": "доходности нет: купоны будущих периодов не известны",
    "face_mismatch": "доходности нет: цена дана к номиналу, которого нет в графике потоков",
    "no_solution": "доходности нет: никакая доходность не воспроизводит цену на потоках бумаги",
    "schedule_broken": "доходности нет: график погашений бумаги не сходится с номиналом (см. причину отсутствия риска)",
    "ladder_failed": "доходности нет: оценка бумаги не вышла, бумага взята по номиналу",
}
"""TDM#619: why a bond the ladder values has no yield of its own (``BondValuation.extra['yield_missing']``, published in the
fact of the position as ``extra.yield_missing`` ``{reason, text}``, ``missing_reason``): the machine code and the text for
the card. The source's yield is never in its place (TDM#764, #619): a figure is the monitor's own from the last price and
the flows, or it is empty with its reason."""
SPREAD_MISSING = {
    "no_foreign_curve": "спреда нет: в прогоне нет кривой {ccy} (CNY — кривая XCCY, USD и EUR — прокси по par yield "
                        "государственных облигаций, он включается настройкой или в данных нет его котировок)",
    "core_refused": "спреда нет: расчёт по кривой {ccy} отказал ({why})",
    "no_price": "спреда нет: у бумаги нет цены за последние " + str(LOOKBACK_DAYS) + " дней",
    "schedule_broken": "спреда нет: график погашений бумаги не сходится с номиналом (см. причину отсутствия риска)",
    "ladder_failed": "спреда нет: оценка бумаги не вышла, бумага взята по номиналу",
}
"""TDM#619: the same for the spread to the curve of the bond's currency (``extra.spread_missing``
``{reason, ccy, text}``): a bond with a price and a curve of its currency has the Z-spread (core's, at its last price,
``at_last_price``); without either, its reason."""


def missing_reason(table: dict[str, str], code: str, ccy: str | None = None, why: str = "") -> dict[str, Any]:
    """``{reason, ccy, text}`` of ``code`` in ``table`` (``YIELD_MISSING``, ``SPREAD_MISSING``): the shape of
    ``valuation.positions.risk_missing``."""
    return {"reason": code, **({"ccy": ccy} if ccy else {}), "text": table[code].format(ccy=ccy or "", why=why)}


RISK_ASSUMPTION = {
    "zero_rate": ("DV01 при нулевой ставке: у бумаги нет доходности и в прогоне нет кривой {ccy}, чтобы пересчитать "
                  "дюрацию Маколея в модифицированную (DV01 = дюрация Маколея × PV; дюрация — {duration})"),
    "zero_rate_floater": ("DV01 при нулевой ставке: у флоатера нет доходности и в прогоне нет кривой {ccy}; процентная часть "
                          "риска — до ближайшего пересмотра купона, дюрация Маколея × PV без поправки на ставку "
                          "(дюрация — {duration})"),
}
"""TDM#619: the one assumption of a foreign bond's DV01 the ladder cannot take from a rate: it has no yield of its own
and the run has no curve of its currency to take a rate from (``foreign_curve_rate``). Published as
``extra.risk_assumption`` ``{reason, ccy, text}``; ``{duration}`` says whose the duration is, honestly (MOEX's figure
not checked against the flows, or the time to the horizon)."""


def foreign_curve_rate(ctx: EodContext, ccy: str, years: float) -> tuple[float, str] | None:
    """The annual effective zero rate of the run's curve of ``ccy`` (``orchestrator.foreign_bond_curves``: the first of its
    curves the run holds) at ``years`` ahead of the business date (ACT/365F, the curve's own convention), and the curve's
    ID; None without one. What a foreign bond with no yield of its own modifies its duration by (the ladder's
    counterpart of ``kbd_effective_yield`` for a rouble bond)."""
    from ..orchestrator import foreign_bond_curves  # not at the top: the orchestrator imports this module

    cid = next((i for i in foreign_bond_curves(ctx).get(ccy, ()) if i in ctx.fx.curves), None)
    if cid is None:
        return None
    curve, bd, t = ctx.fx.curves[cid].curve, ctx.business_date, max(years, 1 / 365)
    d = bd + timedelta(days=max(round(t * 365), 1))
    try:
        rate = (curve.discount_factor(bd) / curve.discount_factor(d)) ** (365 / (d - bd).days) - 1
    except (ValueError, ArithmeticError, CoreRejection):
        return None
    return (rate, cid) if math.isfinite(rate) else None


def fx_spread_missing(ctx: EodContext, outcome: Outcome, ccy: str) -> dict[str, Any]:
    """``extra.spread_missing`` of a foreign-face bond the ladder values at a price (TDM#619): the run has no curve of its
    currency (``orchestrator.foreign_bond_curves``: CNY on XCCY, USD / EUR on the par-yield proxies with the setting), or it
    has one and core, tried at the bond's last price (``at_last_price``) or today's, refused it (``outcome``'s reason
    and detail: its words)."""
    from ..orchestrator import foreign_bond_curves  # not at the top: the orchestrator imports this module

    if not foreign_bond_curves(ctx).get(ccy):
        return missing_reason(SPREAD_MISSING, "no_foreign_curve", ccy)
    why = "; ".join(x for x in (outcome.reason, outcome.detail) if x) or "нет причины"
    return missing_reason(SPREAD_MISSING, "core_refused", ccy, why)


def _yield(sa: dict[str, Any]) -> float | None:
    """MOEX effective yield, else the close yield, in %; a print at or below -100 % is no yield."""
    return next((y for k in ("effective_yield_pct", "yield_close_pct") if (y := _f(sa.get(k))) is not None
                 and y > -100), None)


def approximate(ctx: EodContext, outcome: Outcome, quantity: float,
                fb: FallbackContext | None = None) -> BondValuation | None:
    """The best rung for a bond the exact path rejected (``outcome.reason``); None only without
    any data. Pass one ``fb`` per run so that history and medians are loaded once."""
    t = outcome.terms
    if t is None:
        return None
    fb = fb or FallbackContext.build(ctx)
    bd, q = ctx.business_date, quantity
    kind = "ofz" if t.bond_class in OFZ_CLASSES else "credit"
    why = [f"not exact: {outcome.reason}{': ' + outcome.detail if outcome.detail else ''}"
           ] if outcome.reason else []
    face_ccy = bond_currency(t)
    fx_face, fx_acc = fx_rate(ctx, face_ccy), fx_rate(ctx, settlement_currency(t))
    if fx_face is None or fx_acc is None:
        return None
    is_fx = face_ccy != "RUB"
    face_bd = face_on(t, bd)
    matured = not t.is_perpetual and (t.maturity_date is not None and t.maturity_date <= bd
                                      or face_bd is not None and face_bd <= 0)
    disc_id, disc_curve, disc_note = bond_discount(ctx, t) if face_ccy == "RUB" else ctx.discount
    disc = RISK_CURVES[disc_id]
    curve = disc if face_ccy == "RUB" else ccy_curve(face_ccy)
    if is_fx:
        from ..orchestrator import foreign_bond_curves  # not at the top: the orchestrator imports this module

        core_why = (f"no {face_ccy} curve in the run" if not foreign_bond_curves(ctx).get(face_ccy)
                    else "core refused it: " + "; ".join(x for x in (outcome.reason, outcome.detail) if x))
        why.append(f"FX bond ({face_ccy}): market value at {fx_face[1]} {fx_face[0]:g} RUB, not valued by core "
                   f"({core_why}): rate risk a duration proxy on curve {curve}")

    found = last_price(outcome, fb)
    ref = found[0] if found else outcome.quote
    # today's MOEX face, else the face outstanding today by the static schedule (not an old quote's: an
    # amortization since then would be valued again and booked as received)
    today = outcome.quote if outcome.quote and outcome.quote.trade_date == bd and outcome.quote.face_value else None
    face = (float(today.face_value) if today else float(face_bd) if face_bd is not None  # type: ignore[union-attr]
            else float(ref.face_value) if ref and ref.face_value else None)
    if ref and ref.face_value and not today and abs(float(ref.face_value) - (face or 0)) > 0.005:
        why.append(f"face {face:g} outstanding on {bd} by the redemption schedule "
                   f"(quote of {ref.trade_date}: {ref.face_value})")
    if matured:
        face = face or 0.0  # valued 0 below whatever the face
    elif face is None or face <= 0:
        return None

    def accrued_rub(today_quote: BondQuote | None) -> float:
        if matured:
            return 0.0
        if today_quote and today_quote.trade_date == bd and today_quote.accrued_per_bond is not None:
            return float(today_quote.accrued_per_bond) * fx_acc[0]
        if (a := _accrued(t, bd, face)) is not None:
            why.append(a[1])
            return a[0] * fx_face[0]
        why.append("accrued unknown: 0")
        return 0.0

    # the ladder's engine is a RUB one (RUB flows, a RUB curve, a RUB dirty price): a foreign bond's flows are in its currency
    # and have no curve here, so no engine (a foreign bond core takes is valued at its price by ``at_last_price``, TDM#619);
    # its yield is from ``own_from_flows``
    sched = None if is_fx or matured else _schedule(t, bd)
    if sched is not None:
        why.extend(sched[0].assumptions)
    offer = next_offer(t, bd)
    listed = [c.pay_date for c in t.coupons if c.pay_date > bd]
    horizon, hkind = ((offer[0], offer[2]) if offer else (t.maturity_date, "maturity") if t.maturity_date
                      else (sched[0].horizon_date if sched else listed[-1], "perpetual") if t.is_perpetual
                      and (sched or listed) else (bd, "unknown"))  # a perpetual is never called (TDM#35)
    coupon, _ = current_period(t, bd)
    coupon_pct = coupon_view(t, bd).pct  # every rung shows the static coupon (D4): rate, amount, last known rate
    sa = ref.source_analytics if ref else {}
    broken = broken_schedule(t, outcome.reason)  # TDM#764: no yield, duration or risk from a schedule that is wrong
    unreliable = None if broken else unreliable_source(ref, t)  # TDM#764, #768: the source's fields are not used
    no_source = broken or unreliable
    # TDM#619: the yield of a foreign-face bond is its own, from the last price and the flows, never the source's (that one
    # stays in the quote's extra for a comparison): the same rule as core's at the last price (``at_last_price``)
    y = None if no_source or is_fx else _yield(sa)
    ytm = y if y is not None else math.nan
    zero = dict.fromkeys(RISK_BUCKETS, 0.0)
    missing: dict[str, Any] = {}  # extra.yield_missing / extra.spread_missing of the valuation (``missing_reason``)

    def make(quality: str, **kw: Any) -> BondValuation:
        if matured:  # redeemed: the redemption is booked as received, the position is gone
            why.append(f"matured {t.maturity_date}: redeemed, valued 0" if t.maturity_date and t.maturity_date <= bd
                       else f"face 0 on {bd}: fully redeemed, valued 0")
            kw.update(clean_pct=0.0, accrued_rub=0.0, face_rub=0.0, dirty_rub=0.0, pv_rub=0.0, ytm_pct=math.nan,
                      macaulay_years=0.0, modified_duration=0.0, z_spread_bp=math.nan, dv01_rub=0.0, buckets=zero,
                      curve_buckets={}, spread01_rub=0.0)
        detail = kw.pop("missing_detail", None)  # TDM#768: why the monitor's own yield and duration were not computed
        if missing and not matured:  # TDM#619: why the yield or the spread is empty
            kw["extra"] = {**kw.get("extra", {}), **missing}
        if (code := kw.pop("missing", broken)) and not matured:
            text = BROKEN_SCHEDULE_TEXT[code] + (OWN_NOT_COMPUTED.format(detail) if detail else "")
            why.append(text)
            kw["extra"] = {**kw.get("extra", {}), "risk_missing": {"reason": code, "text": text}}
        return BondValuation(secid=t.secid, quantity=q, valuation_date=bd, settlement=bd,
                             horizon_date=horizon, horizon_kind=hkind, current_coupon_pct=coupon_pct,
                             spread_kind=kind, calculation_id=f"desk-fallback:{quality}:{t.secid}:{bd}",
                             quality=quality,
                             assumptions=tuple(why), **kw)

    if found:
        quote, basis, clean = found
        quality = "market_value" if quote.trade_date == bd else "stale_price"
        if quality == "stale_price":
            why.append(stale_note(quote.trade_date, bd, basis))
        elif basis not in PRICE_BASIS_ORDER:
            why.append(f"price basis {basis}")
        acc = accrued_rub(quote)
        dirty = clean / 100 * face * fx_face[0] + acc
        pv = q * dirty
        own: OwnAnalytics | None = None
        detail = None
        # TDM#768: the source's yield and duration are wrong, ours come from the flows; TDM#619: a foreign-face bond's
        # always are (the same rule as core's at the last price), the coupons the exact path assumes taken as it takes them
        if (unreliable or is_fx and not broken) and not matured:
            got = own_from_flows(t, bd, dirty / fx_face[0], face, allow_assumed=is_fx and not unreliable)
            if isinstance(got, OwnAnalytics):
                own, y = got, got.yield_pct
                ytm = y
                why.append((OWN_ANALYTICS_TEXT if unreliable else OWN_ANALYTICS_TEXT_FX) if is_fx
                           else OWN_ANALYTICS_TEXT_CURVE)
                if sched is None:  # the schedule's notes are in the assumptions already for a bond with a curve
                    why.extend(got.notes)
            else:
                detail = got
                if not no_source:  # a foreign bond with no yield of its own: its reason, the source's figure not in its place
                    missing["yield_missing"] = missing_reason(YIELD_MISSING, got.code)
                    why.append(f"{missing['yield_missing']['text']} ({got})")
        if is_fx and not matured:  # TDM#619: core would have valued it with a curve of its currency
            missing["spread_missing"] = (missing_reason(SPREAD_MISSING, "schedule_broken", face_ccy) if broken
                                         else fx_spread_missing(ctx, outcome, face_ccy))
            why.append(missing["spread_missing"]["text"])
            if broken:  # no yield of its own from a schedule that does not add up (TDM#764): the code, not only the risk's
                missing["yield_missing"] = missing_reason(YIELD_MISSING, "schedule_broken")
        if no_source and own is None:  # the value is the price's; no yield, duration or risk from a schedule that does not add up
            return make(quality, missing=no_source, missing_detail=detail, price_basis=basis, clean_pct=clean, accrued_rub=acc, face_rub=face * fx_face[0],
                        dirty_rub=dirty, pv_rub=pv, ytm_pct=math.nan, macaulay_years=math.nan,
                        modified_duration=math.nan, z_spread_bp=math.nan, dv01_rub=0.0, buckets=zero,
                        spread01_rub=0.0)
        if sched is not None:
            try:
                _, r = _engine(ctx, disc_curve, sched[1], kind, q, dirty=dirty, tail=sched[3])
                why.append(f"risk: {disc}, Z calibrated to this price on the fixed schedule"
                           + (f" ({disc_note})" if disc_note else ""))
                bs = {b.bucket: b.dv01_rub for b in r.buckets}
                mdur = -r.dv01_rub / pv * 1e4 if pv else math.nan
                return make(quality, price_basis=basis, clean_pct=clean, accrued_rub=acc, face_rub=face,
                            dirty_rub=dirty, pv_rub=pv, ytm_pct=ytm,
                            # the monitor's own yield and duration come as a pair (modified = Macaulay / (1 + y), as the
                            # exact path's); the DV01 below is the engine's (Z calibrated to the price on the discount curve)
                            macaulay_years=own.macaulay_years if own else mdur * (1 + (y or 0) / 100),
                            modified_duration=own.modified_years if own else mdur,
                            z_spread_bp=r.spread_bp, discount_curve_id=disc_id,
                            buckets=bs, curve_buckets={disc: bs}, dv01_rub=r.dv01_rub,
                            spread01_rub=r.cs01_rub if kind == "credit" else r.ofz_spr01_rub)
            except (ValueError, RuntimeError):
                pass  # no Z reproduces this price: fall to the duration estimate
        days = _f(sa.get("duration_days"))
        if own:  # TDM#768: the monitor's own, from the flows and the price (MOEX's fields are not used)
            mac, note = own.macaulay_years, f"duration: Macaulay {own.macaulay_years:.3f} y from the flows and the price"
        elif days:
            mac, note = days / 365, f"duration: MOEX {days:.0f} days"
        elif hkind != "unknown":
            mac, note = max((horizon - bd).days, 0) / 365, f"duration ~ time to {hkind} {horizon}"
        else:
            mac, note = 0.0, "no duration"
        if is_fx and own is None:  # TDM#619: said what it is: the source's figure, not checked against the flows
            whose = ("цифра источника, не проверена по потокам: своей дюрации нет" if days
                     else "срок до даты погашения или оферты, а не дюрация: своей дюрации нет" if hkind != "unknown"
                     else "нет")
        rate_t = mac
        if t.coupon_type == "floating" and coupon is not None:  # the coupon resets: rates risk only to then
            rate_t = (coupon.pay_date - bd).days / 365
            note += f"; rates duration ~ time to next reset {coupon.pay_date}"
        if t.is_face_indexed:  # OFZ-IN (R8 phase 1): no real curve or inflation factor yet, see ORG_CONVENTIONS §4.1
            note += "; indexed face: a nominal-rate proxy, no real-rate and inflation split"
        if y is not None:
            yy = y / 100
        elif is_fx:  # TDM#619: no yield of its own: the rate of its currency's curve at the duration, else zero, and said
            cr = foreign_curve_rate(ctx, face_ccy, mac)
            yy = cr[0] if cr else 0.0
            if cr:
                why.append(f"DV01 modified by the {cr[1]} rate at the duration, {cr[0] * 100:.2f} % (no yield of the bond); "
                           f"the duration is {whose}")
            else:
                code = "zero_rate_floater" if t.coupon_type == "floating" else "zero_rate"
                text = RISK_ASSUMPTION[code].format(ccy=face_ccy, duration=whose)
                missing["risk_assumption"] = {"reason": code, "ccy": face_ccy, "text": text}
                why.append(text)
        else:
            yy = kbd_effective_yield(ctx.kbd, max(mac, 1 / 365))
        # core's buckets bump par (RUONIA OIS, the KBD) and the Z-spread is annual: dPV ~ -Macaulay / (1 + y) * PV,
        # the modified duration (-Macaulay * PV, a continuous-zero DV01, was about 15 % larger: #36)
        dv01, s01 = -rate_t / (1 + yy) * pv * 1e-4 + 0.0, -mac / (1 + yy) * pv * 1e-4 + 0.0
        why.append(note + (f": DV01 split between the nearest pillars of {curve}" if rate_t else ": DV01 0"))
        buckets = _split(rate_t, dv01) if rate_t else zero
        by_curve = {curve: buckets}
        if not is_fx and t.coupon_type == "floating" and coupon is not None and mac > rate_t \
                and (t.floater_formula or {}).get("benchmark") == "KEY_RATE":
            by_curve = key_rate_floater_proxy(curve, mac, rate_t, pv / (1 + yy))
            why.append(f"key-rate floater (R15): discounting {-mac:.2f} y on {curve}, projection "
                       f"+{mac:.2f} y / {-rate_t:.2f} y on {KEY_CURVE}; their sum is the DV01 to the reset")
        if (kb := by_curve.get(RISK_CURVES[KBD_CURVE_ID])) is not None:  # the KBD has no risk below 1Y (v3: in 1Y)
            by_curve[RISK_CURVES[KBD_CURVE_ID]] = {
                b: (math.fsum(kb[k] for k in KBD_SHORT) + kb["1Y"] if b == "1Y" else 0.0 if b in KBD_SHORT else x)
                for b, x in kb.items()}
            buckets = {b: math.fsum(bs[b] for bs in by_curve.values()) for b in RISK_BUCKETS}
        return make(quality, price_basis=basis, clean_pct=clean, accrued_rub=acc, face_rub=face * fx_face[0],
                    dirty_rub=dirty, pv_rub=pv, ytm_pct=ytm, macaulay_years=mac,
                    modified_duration=mac / (1 + yy), z_spread_bp=math.nan, dv01_rub=dv01,
                    buckets=buckets, curve_buckets=by_curve, spread01_rub=s01)

    priced = None  # ((engine, risk), Z, model_spread_source, assumption text)
    if sched is not None and (c := fb.carried.get(t.secid)) is not None and c.curve not in (None, disc):
        why.append(f"carried Z {c.z_spread_bp:.0f} bp from {c.business_date} is to {c.curve}, not {disc}: not used")
    elif sched is not None and c is not None and math.isfinite(c.z_spread_bp):
        # used even outside Z_BAND_BP: the band keeps outliers out of a group median, but the bond's own wide Z
        # is evidence (a distressed name), and a median would price it near par
        # shortcut: a carried spread never refreshes from better information (new issuer peers, a market-wide
        # spread move) until the bond trades again, upgrade by moving it with its group's median change
        try:
            priced = (_engine(ctx, disc_curve, sched[1], kind, q, spread_bp=c.z_spread_bp, tail=sched[3]), c.z_spread_bp, c.source,
                      f"Z {c.z_spread_bp:.0f} bp carried from {c.business_date} ({c.source})")
        except (ValueError, RuntimeError) as err:
            why.append(f"carried Z {c.z_spread_bp:.0f} bp from {c.business_date} failed ({err}): the day's median")
    if sched is not None and priced is None and (med := fb.median_z(t, kind, disc_id)) is not None:
        try:
            priced = _engine(ctx, disc_curve, sched[1], kind, q, spread_bp=med[0], tail=sched[3]), med[0], f"{med[1]} on {bd}", med[1]
        except (ValueError, RuntimeError):
            pass
    if sched is not None and priced is not None:
        (e, r), z, source, label = priced
        dirty = e.base_price_rub
        try:
            acc = sched[2].accrued(bd)
        except (ValueError, CoreRejection):
            acc = accrued_rub(None)
        why.append(f"model price: {disc} + {label}, no price in {LOOKBACK_DAYS} days"
                   + (f" ({disc_note})" if disc_note else ""))
        missing["yield_missing"] = missing_reason(YIELD_MISSING, "model_price")
        bs = {b.bucket: b.dv01_rub for b in r.buckets}
        mdur = -r.dv01_rub / r.pv_rub * 1e4 if r.pv_rub else math.nan
        return make("model_price", price_basis=MODEL_BASIS, clean_pct=(dirty - acc) / face * 100,
                    accrued_rub=acc, face_rub=face, dirty_rub=dirty, pv_rub=q * dirty, ytm_pct=math.nan,
                    macaulay_years=mdur, modified_duration=mdur, z_spread_bp=z, model_spread_source=source,
                    discount_curve_id=disc_id, buckets=bs, curve_buckets={disc: bs}, dv01_rub=r.dv01_rub,
                    spread01_rub=r.cs01_rub if kind == "credit" else r.ofz_spr01_rub)

    acc = accrued_rub(outcome.quote)
    why.append(f"par proxy: no price in {LOOKBACK_DAYS} days, "
               + ("no median Z" if sched is not None else "no usable fixed schedule"))
    missing["yield_missing"] = missing_reason(YIELD_MISSING, "no_price")  # TDM#619
    missing["spread_missing"] = missing_reason(SPREAD_MISSING, "no_price", face_ccy)
    dirty = face * fx_face[0] + acc
    return make("par_proxy", price_basis="par", clean_pct=100.0, accrued_rub=acc, face_rub=face * fx_face[0],
                dirty_rub=dirty, pv_rub=q * dirty, ytm_pct=math.nan, macaulay_years=math.nan,
                modified_duration=math.nan, z_spread_bp=math.nan, dv01_rub=0.0, buckets=zero,
                spread01_rub=0.0)


def par_proxy(ctx: EodContext, outcome: Outcome, quantity: float, note: str) -> BondValuation | None:
    """The last rung on its own for a bond whose ladder failed (``note`` says how): today's face (MOEX, else the
    redemption schedule) + today's MOEX accrued if any, no risk; None without a face or an FX rate."""
    t, quote, bd = outcome.terms, outcome.quote, ctx.business_date
    assert t is not None
    today = quote if quote and quote.trade_date == bd else None
    face = today.face_value if today and today.face_value else face_on(t, bd)
    fx_face = fx_rate(ctx, bond_currency(t))
    fx_acc = fx_rate(ctx, settlement_currency(t))
    matured = not t.is_perpetual and (t.maturity_date is not None and t.maturity_date <= bd
                                      or face is not None and face <= 0)
    if matured:
        face, note = Decimal(0), note + f"; redeemed by {bd} (maturity {t.maturity_date}), valued 0"
    elif face is None or face <= 0:
        return None
    if fx_face is None or fx_acc is None:
        return None
    acc = float(today.accrued_per_bond) * fx_acc[0] if today and today.accrued_per_bond is not None \
        and not matured else 0.0
    dirty = float(face) * fx_face[0] + acc
    return BondValuation(
        secid=t.secid, quantity=quantity, valuation_date=bd, settlement=bd, horizon_date=t.maturity_date or bd,
        horizon_kind="maturity" if t.maturity_date else "unknown", price_basis="par", clean_pct=100.0,
        accrued_rub=acc, face_rub=float(face) * fx_face[0], current_coupon_pct=coupon_view(t, bd).pct, dirty_rub=dirty,
        pv_rub=quantity * dirty, ytm_pct=math.nan, macaulay_years=math.nan, modified_duration=math.nan,
        z_spread_bp=math.nan, spread_kind="ofz" if t.bond_class in OFZ_CLASSES else "credit", dv01_rub=0.0,
        buckets=dict.fromkeys(RISK_BUCKETS, 0.0), spread01_rub=0.0,
        calculation_id=f"desk-fallback:par_proxy:{t.secid}:{bd}", quality="par_proxy",
        assumptions=(note, "par proxy: face" + (" + MOEX accrued" if acc else ", accrued 0") + ", no risk"),
        extra={} if matured else {"yield_missing": missing_reason(YIELD_MISSING, "ladder_failed"),
                                  "spread_missing": missing_reason(SPREAD_MISSING, "ladder_failed",
                                                                   bond_currency(t))})


@dataclass(frozen=True)
class SimplePnl(FlashPnl):
    """Flash PnL without decomposition: everything in ``other`` (``flag``: a model price on either side)."""

    note: str = ""
    method: str = METHOD
    """``METHOD``, or ``YIELD_METHOD`` with a foreign bond's carry at its yield (``yield_pnl``)."""

    def extra(self) -> dict[str, Any]:
        return {"method": self.method, "prev_date": self.prev_date.isoformat(), "received_rub": self.received_rub,
                "total": self.total, "note": self.note, **self.tags()}


def received_per_bond(t: BondTerms, d0: date, d1: date) -> float | None:
    """Coupons and redemptions paid in (d0, d1] per bond, face currency; None if an amount is missing."""
    amounts = [c.amount_per_bond for c in t.coupons if d0 < c.pay_date <= d1]
    amounts += [r.value_per_bond for r in t.redemptions if d0 < r.date <= d1]
    return None if any(a is None for a in amounts) else float(sum(amounts))  # type: ignore[arg-type]


YIELD_METHOD = "desk-flash-yield-v1"
YIELD_QUALITIES = ("market_value", "stale_price")
"""The rungs with a price of the bond's own (today's or its last), so a yield to carry it at."""


def yield_pnl(prev: BondValuation, cur: BondValuation, terms: BondTerms, prev_ctx: EodContext,
              ctx: EodContext) -> SimplePnl:
    """``simple_pnl``, with the carry of a foreign-face bond priced on both dates without a curve in its currency
    (USD / EUR without ``proxy_curves_for_risk``, CNY on a date without the XCCY curve; batch 1, item 5): the D-1 PV in
    its currency x ((1 + y)^(days / 365) - 1) at its D-1 yield (``ytm_pct``: the monitor's own from the price and the
    flows, ``own_analytics``, TDM#619, never MOEX's; the coupon income and the pull to the price at an unchanged yield), at
    today's market rate, goes from ``other`` to ``carry``. A bond core valued at its last price (``at_last_price``) has the
    exact steps, not this. A bond
    without a yield (a broken redemption schedule, TDM#764: ``ytm_pct`` is NaN) has no carry: ``simple_pnl`` as is, all
    in ``other``. Without a curve
    the yield move is not split between rates and spread: it stays in ``other`` with the prints, and
    ``pnl.direct.split_fx`` then takes the FX revaluation out of ``other``. A RUB bond, a model price or a par proxy:
    ``simple_pnl`` as is."""
    pnl = simple_pnl(prev, cur, terms, ctx)
    ccy = bond_currency(terms)
    y = prev.ytm_pct
    if ccy == "RUB" or prev.quality not in YIELD_QUALITIES or cur.quality not in YIELD_QUALITIES \
            or y is None or not math.isfinite(y) or y <= -100:
        return pnl
    r0, r1 = fx_rate(prev_ctx, ccy), fx_rate(ctx, ccy)
    if r0 is None or r1 is None:
        return pnl
    days = (cur.valuation_date - prev.valuation_date).days
    carry = pnl.v0 / r0[0] * ((1 + y / 100) ** (days / 365) - 1) * r1[0]
    return replace(pnl, carry=carry, other=pnl.other - carry, v_carry=pnl.v0 + carry, method=YIELD_METHOD,
                   other_reason=other_reason(YIELD_NO_CURVE, f"no {ccy} curve: the yield move stays in other", ccy=ccy),
                   note=f"{pnl.note}; carry = PV D-1 {pnl.v0 / r0[0]:,.2f} {ccy} x ((1 + {y:g} %)^({days}/365) - 1) "
                        f"x {r1[0]:g} (no {ccy} curve: the yield move stays in other)")


def simple_pnl(prev: BondValuation, cur: BondValuation, terms: BondTerms, ctx: EodContext) -> SimplePnl:
    """q * (dirty_D - dirty_D-1) + received, all in ``other``; for a position not exact on either date."""
    if abs(prev.quantity - cur.quantity) > 1e-9:
        raise NoPnl("quantity_changed",
                    f"{prev.quantity:g} -> {cur.quantity:g}: trades of the day are not in S2")
    d0, d1 = prev.valuation_date, cur.valuation_date
    per_bond = received_per_bond(terms, d0, d1)
    if per_bond is None:
        raise NoPnl("received_unknown", f"a coupon or redemption in ({d0}, {d1}] has no amount")
    fx = fx_rate(ctx, bond_currency(terms))
    q = cur.quantity
    received = q * per_bond * (fx[0] if fx else 1.0)
    total = q * (cur.dirty_rub - prev.dirty_rub) + received
    v0 = q * prev.dirty_rub
    qs = (prev.quality, cur.quality)
    flag = ("" if "model_price" not in qs else "model price on both dates" if qs[0] == qs[1]
            else f"price discovery: {'model -> market' if qs[0] == 'model_price' else 'market -> model'}")
    return SimplePnl(
        secid=cur.secid, quantity=q, prev_date=d0, as_of=d1, prev_valuation_date=d0, valuation_date=d1,
        z0_bp=prev.z_spread_bp, z1_bp=cur.z_spread_bp, v0=v0, v_carry=v0, v_roll=v0, v_rates=v0, v_spread=v0,
        v1=q * cur.dirty_rub + received, received_rub=received, carry=0.0, rolldown=0.0, rates=0.0,
        spread=0.0, other=total, total=total, identity_error=0.0, rates_linear=0.0,
        price_basis=(prev.price_basis, cur.price_basis),
        note=f"simple PnL ({prev.quality} -> {cur.quality}): q x (dirty D - dirty D-1) + received, "
             "all in other", flag=flag)
