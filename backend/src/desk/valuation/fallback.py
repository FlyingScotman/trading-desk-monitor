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
model_price     no price, a usable fixed schedule: priced on the KBD at the Z-spread carried from
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

Risk of the priced rungs: with a usable fixed schedule, core ``BondRiskEngine`` calibrated to the
dirty price (bucket DV01 and spread risk on the KBD, as for exact bonds). Otherwise a duration
estimate: MOEX ``duration_days``, else the time to the horizon; DV01 = -Macaulay * PV * 1e-4
(the buckets bump continuous zero rates) split linearly between the two nearest pillars; CS01 =
DV01 for credit, 0 for OFZ. A floater's rates duration is the time to its next reset (its CS01
keeps the full duration). A bond with a foreign face is valued in RUB at the CBR rate (MOEX TOM
when CBR is missing); core has no curve in its currency, so its rate risk is always the duration
estimate (DV01 in RUB per bp), booked on the currency's curve (``bonds.ccy_curve``: ``USD``, ``CNY``...)
instead of the KBD. MOEX publishes the accrued in the settlement currency (RUB for most replacement
bonds) and the price in % of the foreign face.

Flash PnL of a position not exact on either date: ``simple_pnl``, q * (dirty_D - dirty_D-1) +
received, all in ``other``. No decomposition is faked.
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from itertools import chain
from typing import TYPE_CHECKING, Any

from rub_fixed_income import (
    RISK_BUCKETS,
    RISK_PILLARS,
    BondCashflow,
    BondRiskEngine,
    BondRiskRequest,
    CoreRejection,
    kbd_effective_yield,
)

from ..marketdata.adapter import OFZ_CLASSES, BondQuote, BondTerms, MarketView, core_instrument
from ..pnl.flash import FlashPnl, NoPnl
from .bonds import (
    PRICE_BASIS_ORDER,
    SPREAD_CONVENTION,
    BondValuation,
    Rejected,
    bond_request,
    ccy_curve,
    face_on,
    fixed_schedule,
    next_offer,
    value_bonds,
)

if TYPE_CHECKING:
    from ..orchestrator import EodContext, Outcome

FALLBACK_BASES = (*PRICE_BASIS_ORDER, "market_price2", "market_price3", "admitted_quote")
LOOKBACK_DAYS = 30
MIN_BONDS = {"issuer": 3, "class": 3, "kind": 3}
Z_BAND_BP = (-100.0, 2000.0)
"""Z-spreads outside are distressed names or bad prints, not a spread level for a model price."""
MIN_HORIZON_DAYS = 30
"""Bonds closer to their horizon stay out of the medians: there a kopeck of price is many bp of Z."""
METHOD = "desk-flash-simple-v1"
QUALITIES = ("exact", "assumed", "market_value", "stale_price", "model_price", "par_proxy", "reported_value")
RUNGS = QUALITIES[2:]
"""The qualities without cashflows on the valuation: flash PnL by ``simple_pnl`` (``reported_value`` has none)."""


@dataclass(frozen=True)
class CarriedSpread:
    """The Z-spread a model price used in the run of ``business_date`` (desk store ``model_spread``,
    slice S5). A later run carries it, so the model price moves with the curve, not the day's median."""

    secid: str
    z_spread_bp: float
    business_date: date
    source: str


def used_model_spreads(outcomes: list[Outcome]) -> list[CarriedSpread]:
    """The spreads the model-price valuations of ``outcomes`` used, one per secid, for the store."""
    out = {o.secid: CarriedSpread(o.secid, v.z_spread_bp, v.valuation_date, v.model_spread_source)
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
    carried: dict[str, CarriedSpread] = field(default_factory=dict)
    """Model spreads of the latest earlier run, by secid (empty without a desk store)."""

    @classmethod
    def build(cls, ctx: EodContext, carried: dict[str, CarriedSpread] | None = None) -> FallbackContext:
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

    def median_z(self, terms: BondTerms, kind: str) -> tuple[float, str] | None:
        if self.medians is None:
            self.medians = _medians(self.ctx)
        for key, label in ((("issuer", terms.issuer_id), "issuer"),
                           (("class", terms.bond_class), terms.bond_class), (("kind", kind), kind)):
            if key in self.medians:
                z, n = self.medians[key]
                return z, f"{label} median Z {z:.0f} bp ({n} bonds)"
        return None


def _medians(ctx: EodContext) -> dict[tuple[str, Any], tuple[float, int]]:
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
    groups: dict[tuple[str, Any], list[float]] = defaultdict(list)
    for r in reqs:
        v = valued.get(r.position_id)
        if isinstance(v, BondValuation) and v.quality == "exact" and math.isfinite(v.z_spread_bp) \
                and Z_BAND_BP[0] <= v.z_spread_bp <= Z_BAND_BP[1] \
                and (v.horizon_date - ctx.business_date).days >= MIN_HORIZON_DAYS:
            for k in (("issuer", r.terms.issuer_id), ("class", r.terms.bond_class), ("kind", v.spread_kind)):
                if k[1] is not None:
                    groups[k].append(v.z_spread_bp)
    return {k: (statistics.median(zs), len(zs)) for k, zs in groups.items() if len(zs) >= MIN_BONDS[k[0]]}


def fx_rate(ctx: EodContext, ccy: str) -> tuple[float, str] | None:
    """RUB per unit: CBR official in force on the business date, else the MOEX TOM price."""
    if ccy == "RUB":
        return 1.0, ""
    fx = ctx.market.fx(ccy)
    if fx["cbr"]:
        return fx["cbr"], "CBR"
    return (fx["moex_tom"], "MOEX TOM") if fx["moex_tom"] else None


def _price(q: BondQuote) -> tuple[str, float] | None:
    return next(((b, float(q.prices_pct[b])) for b in FALLBACK_BASES if q.prices_pct.get(b, 0) > 0), None)


def _current_coupon(t: BondTerms, d: date) -> tuple[Any, date | None]:
    """The coupon period running on ``d`` and its start (None when unknown)."""
    for i, c in enumerate(t.coupons):
        if c.pay_date > d:
            return c, c.start or (t.coupons[i - 1].pay_date if i else None)
    return None, None


def _accrued(t: BondTerms, d: date, face: float) -> tuple[float, str] | None:
    """Accrued per bond in face currency from the current coupon, ACT/365 on the rate."""
    c, start = _current_coupon(t, d)
    if c is None or start is None or start > d:
        return None
    days = (d - start).days
    if c.rate_pct is not None:
        return float(c.rate_pct) / 100 * face * days / 365, f"accrued computed: {c.rate_pct}% x {days}/365"
    if c.amount_per_bond is not None and c.pay_date > start:
        return (float(c.amount_per_bond) * days / (c.pay_date - start).days,
                f"accrued computed: coupon {c.amount_per_bond} x {days}/{(c.pay_date - start).days}")
    return None


def _schedule(t: BondTerms, d: date) -> tuple[Any, tuple[BondCashflow, ...], Any] | None:
    """Fixed-coupon schedule to the horizon (as ``valuation.bonds`` builds it), its flows and the
    core instrument, or None. A call redeems at the horizon."""
    try:
        s = fixed_schedule(t, d)
        if s.floater is not None:
            return None
        call = s.horizon_kind == "call"
        inst = core_instrument(t, d, s.horizon_date, None if call else s.offer, coupons=s.coupons,
                               redeem_at_horizon=call)
        flows = inst.cashflows(after=d, offer=inst.offers[0] if inst.offers else None)
    except (Rejected, CoreRejection):
        return None
    return s, tuple(BondCashflow(f.payment_date, f.amount) for f in flows), inst


def _engine(ctx: EodContext, flows: tuple[BondCashflow, ...], kind: str, q: float, *,
            spread_bp: float | None = None, dirty: float | None = None) -> tuple[BondRiskEngine, Any]:
    e = BondRiskEngine(BondRiskRequest(cashflows=flows, settlement_date=ctx.business_date, curve=ctx.curve,
                                       spread_convention=SPREAD_CONVENTION,
                                       spread_kind=kind,  # type: ignore[arg-type]
                                       spread_bp=spread_bp, dirty_price_rub=dirty, quantity=q),
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


def _f(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


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
    face_ccy = t.face_unit or t.currency or "RUB"
    fx_face, fx_acc = fx_rate(ctx, face_ccy), fx_rate(ctx, t.currency or face_ccy)
    if fx_face is None or fx_acc is None:
        return None
    is_fx = face_ccy != "RUB"
    face_bd = face_on(t, bd)
    matured = not t.is_perpetual and (t.maturity_date is not None and t.maturity_date <= bd
                                      or face_bd is not None and face_bd <= 0)
    curve = ccy_curve(face_ccy)
    if is_fx:
        why.append(f"FX bond ({face_ccy}): market value at {fx_face[1]} {fx_face[0]:g} RUB, no core curve in "
                   f"{face_ccy}: rate risk a duration proxy on curve {curve}")

    found = None
    # today's quote first; the history is loaded only when it has no usable price
    for cand in chain([outcome.quote] if outcome.quote else [], fb.past_quotes(t)):
        if (bd - cand.trade_date).days > LOOKBACK_DAYS:
            break
        if (p := _price(cand)) is not None:
            found = cand, *p
            break
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

    sched = None if is_fx or matured else _schedule(t, bd)
    if sched is not None:
        why.extend(sched[0].assumptions)
    offer = next_offer(t, bd)
    horizon, hkind = ((offer[0], offer[2]) if offer else (t.maturity_date, "maturity") if t.maturity_date
                      else (bd, "unknown"))
    coupon, _ = _current_coupon(t, bd)
    coupon_pct = float(coupon.rate_pct) if coupon is not None and coupon.rate_pct is not None else None
    sa = ref.source_analytics if ref else {}
    y = _yield(sa)
    ytm = y if y is not None else math.nan
    zero = dict.fromkeys(RISK_BUCKETS, 0.0)

    def make(quality: str, **kw: Any) -> BondValuation:
        if matured:  # redeemed: the redemption is booked as received, the position is gone
            why.append(f"matured {t.maturity_date}: redeemed, valued 0" if t.maturity_date and t.maturity_date <= bd
                       else f"face 0 on {bd}: fully redeemed, valued 0")
            kw.update(clean_pct=0.0, accrued_rub=0.0, face_rub=0.0, dirty_rub=0.0, pv_rub=0.0, ytm_pct=math.nan,
                      macaulay_years=0.0, modified_duration=0.0, z_spread_bp=math.nan, dv01_rub=0.0, buckets=zero,
                      curve_buckets={}, spread01_rub=0.0)
        return BondValuation(secid=t.secid, quantity=q, valuation_date=bd, settlement=bd,
                             horizon_date=horizon, horizon_kind=hkind, current_coupon_pct=coupon_pct,
                             spread_kind=kind, calculation_id=f"desk-fallback:{quality}:{t.secid}:{bd}",
                             quality=quality,
                             assumptions=tuple(why), **kw)

    if found:
        quote, basis, clean = found
        quality = "market_value" if quote.trade_date == bd else "stale_price"
        if quality == "stale_price":
            why.append(f"last price {quote.trade_date}, {(bd - quote.trade_date).days} days old ({basis})")
        elif basis not in PRICE_BASIS_ORDER:
            why.append(f"price basis {basis}")
        acc = accrued_rub(quote)
        dirty = clean / 100 * face * fx_face[0] + acc
        pv = q * dirty
        if sched is not None:
            try:
                _, r = _engine(ctx, sched[1], kind, q, dirty=dirty)
                why.append("risk: KBD, Z calibrated to this price on the fixed schedule")
                mdur = -r.dv01_rub / pv * 1e4 if pv else math.nan
                return make(quality, price_basis=basis, clean_pct=clean, accrued_rub=acc, face_rub=face,
                            dirty_rub=dirty, pv_rub=pv, ytm_pct=ytm,
                            macaulay_years=mdur * (1 + (y or 0) / 100), modified_duration=mdur,
                            z_spread_bp=r.spread_bp,
                            buckets={b.bucket: b.dv01_rub for b in r.buckets}, dv01_rub=r.dv01_rub,
                            spread01_rub=r.cs01_rub if kind == "credit" else r.ofz_spr01_rub)
            except (ValueError, RuntimeError):
                pass  # no Z reproduces this price: fall to the duration estimate
        days = _f(sa.get("duration_days"))
        if days:
            mac, note = days / 365, f"duration: MOEX {days:.0f} days"
        elif hkind != "unknown":
            mac, note = max((horizon - bd).days, 0) / 365, f"duration ~ time to {hkind} {horizon}"
        else:
            mac, note = 0.0, "no duration"
        rate_t = mac
        if t.coupon_type == "floating" and coupon is not None:  # the coupon resets: rates risk only to then
            rate_t = (coupon.pay_date - bd).days / 365
            note += f"; rates duration ~ time to next reset {coupon.pay_date}"
        if t.is_face_indexed:  # OFZ-IN (R8 phase 1): no real curve or inflation factor yet, see ORG_CONVENTIONS §4.1
            note += "; indexed face: a nominal-rate proxy, no real-rate and inflation split"
        # bucket DV01 bumps continuously compounded zero rates: dPV ~ -Macaulay * PV, not modified duration
        dv01, s01 = -rate_t * pv * 1e-4 + 0.0, -mac * pv * 1e-4 + 0.0
        why.append(note + (f": DV01 split between the nearest pillars of {curve}" if rate_t else ": DV01 0"))
        yy = y / 100 if y is not None else 0.0 if is_fx else kbd_effective_yield(ctx.kbd, max(mac, 1 / 365))
        buckets = _split(rate_t, dv01) if rate_t else zero
        return make(quality, price_basis=basis, clean_pct=clean, accrued_rub=acc, face_rub=face * fx_face[0],
                    dirty_rub=dirty, pv_rub=pv, ytm_pct=ytm, macaulay_years=mac,
                    modified_duration=mac / (1 + yy), z_spread_bp=math.nan, dv01_rub=dv01,
                    buckets=buckets, curve_buckets={curve: buckets},
                    spread01_rub=s01 if kind == "credit" else 0.0)

    priced = None  # ((engine, risk), Z, model_spread_source, assumption text)
    if sched is not None and (c := fb.carried.get(t.secid)) is not None and math.isfinite(c.z_spread_bp):
        # used even outside Z_BAND_BP: the band keeps outliers out of a group median, but the bond's own wide Z
        # is evidence (a distressed name), and a median would price it near par
        # shortcut: a carried spread never refreshes from better information (new issuer peers, a market-wide
        # spread move) until the bond trades again, upgrade by moving it with its group's median change
        try:
            priced = (_engine(ctx, sched[1], kind, q, spread_bp=c.z_spread_bp), c.z_spread_bp, c.source,
                      f"Z {c.z_spread_bp:.0f} bp carried from {c.business_date} ({c.source})")
        except (ValueError, RuntimeError) as err:
            why.append(f"carried Z {c.z_spread_bp:.0f} bp from {c.business_date} failed ({err}): the day's median")
    if sched is not None and priced is None and (med := fb.median_z(t, kind)) is not None:
        try:
            priced = _engine(ctx, sched[1], kind, q, spread_bp=med[0]), med[0], f"{med[1]} on {bd}", med[1]
        except (ValueError, RuntimeError):
            pass
    if sched is not None and priced is not None:
        (e, r), z, source, label = priced
        dirty = e.base_price_rub
        try:
            acc = sched[2].accrued(bd)
        except (ValueError, CoreRejection):
            acc = accrued_rub(None)
        why.append(f"model price: KBD + {label}, no price in {LOOKBACK_DAYS} days")
        mdur = -r.dv01_rub / r.pv_rub * 1e4 if r.pv_rub else math.nan
        return make("model_price", price_basis="model", clean_pct=(dirty - acc) / face * 100,
                    accrued_rub=acc, face_rub=face, dirty_rub=dirty, pv_rub=q * dirty, ytm_pct=math.nan,
                    macaulay_years=mdur, modified_duration=mdur, z_spread_bp=z, model_spread_source=source,
                    buckets={b.bucket: b.dv01_rub for b in r.buckets}, dv01_rub=r.dv01_rub,
                    spread01_rub=r.cs01_rub if kind == "credit" else r.ofz_spr01_rub)

    acc = accrued_rub(outcome.quote)
    why.append(f"par proxy: no price in {LOOKBACK_DAYS} days, "
               + ("no median Z" if sched is not None else "no usable fixed schedule"))
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
    fx_face = fx_rate(ctx, t.face_unit or t.currency or "RUB")
    fx_acc = fx_rate(ctx, t.currency or t.face_unit or "RUB")
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
        accrued_rub=acc, face_rub=float(face) * fx_face[0], current_coupon_pct=None, dirty_rub=dirty,
        pv_rub=quantity * dirty, ytm_pct=math.nan, macaulay_years=math.nan, modified_duration=math.nan,
        z_spread_bp=math.nan, spread_kind="ofz" if t.bond_class in OFZ_CLASSES else "credit", dv01_rub=0.0,
        buckets=dict.fromkeys(RISK_BUCKETS, 0.0), spread01_rub=0.0,
        calculation_id=f"desk-fallback:par_proxy:{t.secid}:{bd}", quality="par_proxy",
        assumptions=(note, "par proxy: face" + (" + MOEX accrued" if acc else ", accrued 0") + ", no risk"))


@dataclass(frozen=True)
class SimplePnl(FlashPnl):
    """Flash PnL without decomposition: everything in ``other``."""

    note: str = ""
    flag: str = ""
    """Set when a model price is on either side: the PnL is then partly price discovery, not a market move."""

    def extra(self) -> dict[str, Any]:
        return {"method": METHOD, "prev_date": self.prev_date.isoformat(), "received_rub": self.received_rub,
                "total": self.total, "note": self.note, **({"flag": self.flag} if self.flag else {})}


def received_per_bond(t: BondTerms, d0: date, d1: date) -> float | None:
    """Coupons and redemptions paid in (d0, d1] per bond, face currency; None if an amount is missing."""
    amounts = [c.amount_per_bond for c in t.coupons if d0 < c.pay_date <= d1]
    amounts += [r.value_per_bond for r in t.redemptions if d0 < r.date <= d1]
    return None if any(a is None for a in amounts) else float(sum(amounts))  # type: ignore[arg-type]


def simple_pnl(prev: BondValuation, cur: BondValuation, terms: BondTerms, ctx: EodContext) -> SimplePnl:
    """q * (dirty_D - dirty_D-1) + received, all in ``other``; for a position not exact on either date."""
    if abs(prev.quantity - cur.quantity) > 1e-9:
        raise NoPnl("quantity_changed",
                    f"{prev.quantity:g} -> {cur.quantity:g}: trades of the day are not in S2")
    d0, d1 = prev.valuation_date, cur.valuation_date
    per_bond = received_per_bond(terms, d0, d1)
    if per_bond is None:
        raise NoPnl("received_unknown", f"a coupon or redemption in ({d0}, {d1}] has no amount")
    fx = fx_rate(ctx, terms.face_unit or terms.currency or "RUB")
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
