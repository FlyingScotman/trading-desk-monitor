"""EOD valuation of one business date from hub snapshots (slice S1).

``prepare`` loads the market snapshot of the date and the static snapshot it names, builds the
KBD curve and the settlement calendar. ``value`` prices a list of (secid, quantity) requests and
returns one ``Outcome`` per request: a valuation or a rejection with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from rub_fixed_income import BusinessCalendar, DiscountCurve, ShockedCurveCache

from .curves import kbd
from .marketdata.adapter import BondQuote, BondTerms, MarketView, StaticView
from .marketdata.bundle import SnapshotDir
from .valuation.bonds import BondValuation, Rejected, fixed_schedule, settlement_date, value_bond

KBD_TOLERANCE_BP = 0.05


@dataclass
class Outcome:
    secid: str
    terms: BondTerms | None
    quote: BondQuote | None
    valuation: BondValuation | None = None
    reason: str | None = None
    detail: str = ""


@dataclass
class EodContext:
    snapshots: SnapshotDir
    market: MarketView
    static: StaticView
    kbd: kbd.KbdCurve
    curve: DiscountCurve
    calendar: BusinessCalendar
    calendar_window: tuple[date, date]
    kbd_check_bp: float
    cache: ShockedCurveCache

    @property
    def business_date(self) -> date:
        return self.market.business_date

    def lineage(self) -> dict[str, object]:
        return {
            "market_snapshot_id": self.market.snapshot.id,
            "static_snapshot_id": self.static.snapshot.id,
            "curve_spec": kbd.SPEC_ID,
            "curve_model": kbd.MODEL_VERSION,
            "kbd_check_bp": round(self.kbd_check_bp, 6),
            "calendar": self.calendar.version,
            "stale_datasets": self.market.stale,
        }


def prepare(snapshots: SnapshotDir, business_date: str, cut_type: str = "eod_final") -> EodContext:
    snap = snapshots.market(business_date, cut_type)
    market = MarketView(snap)
    assert snap.static_snapshot_id
    static = StaticView(snapshots.get(snap.static_snapshot_id))
    curve_model = kbd.from_zcyc(market.business_date, market.zcyc_params())
    check = kbd.check_against_standard_yields(curve_model, market.zcyc["standard_yields"])
    if check > KBD_TOLERANCE_BP:
        raise ValueError(f"KBD curve misses the published standard yields by {check:.3f} bp")
    cal, vf, vt = static.business_calendar("MOEX_STOCK")
    return EodContext(snapshots, market, static, curve_model, curve_model.discount_curve(), cal, (vf, vt),
                      check, ShockedCurveCache())


def value(ctx: EodContext, requests: list[tuple[str, float]], *, second_order: bool = False) -> list[Outcome]:
    out = []
    for secid, qty in requests:
        terms = ctx.static.bonds.get(secid)
        if terms is None:
            out.append(Outcome(secid, None, None, reason="not_in_static_snapshot"))
            continue
        quote = ctx.market.quote(terms)
        o = Outcome(secid, terms, quote)
        try:
            if quote is None:
                raise Rejected("no_quote")
            settle = settlement_date(quote.trade_date, terms.settle_lag_days, ctx.calendar, *ctx.calendar_window)
            sched = fixed_schedule(terms, settle)
            o.valuation = value_bond(terms, quote, sched, settle, ctx.curve, quantity=qty,
                                     second_order=second_order, cache=ctx.cache)
        except Rejected as e:
            o.reason, o.detail = e.reason, e.detail
        out.append(o)
    return out
