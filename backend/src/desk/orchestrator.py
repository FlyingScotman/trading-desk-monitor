"""EOD valuation of one business date from hub snapshots (slices S1, S2, S4).

``prepare`` loads the market snapshot of the date and the static snapshot it names, the core
market snapshot (core's KBD curve, the RUONIA OIS and key-rate projection curves and the fixings
the floaters need, built once per date), and the settlement calendar. ``value`` prices a list of
(secid, quantity) requests through core ``value_portfolio`` and returns one ``Outcome`` per
request: a valuation or a rejection with its reason. ``attach_pnl`` adds the day-over-day flash
PnL against the valuations of the previous date (``desk.pnl.flash``; a foreign-face bond's FX revaluation in
``fx_gross``, ``desk.pnl.direct.split_fx``). RUB bonds are discounted on RUONIA OIS (``valuation.bonds.discount_curve``;
the KBD, flagged, on a date whose market has no RUONIA curve).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from rub_fixed_income import (
    KBD_MODEL_VERSION,
    BusinessCalendar,
    CoreRejection,
    CurveEntry,
    DiscountCurve,
    MarketSnapshot,
    PortfolioRiskResult,
    ShockedCurveCache,
    ZcycParameters,
    kbd_discount_curve,
    kbd_effective_yield,
)

from .curves import rates
from .marketdata.adapter import KBD_CURVE_ID, KBD_SPEC, BondQuote, BondTerms, MarketView, StaticView
from .marketdata.bundle import SnapshotDir, SnapshotError
from .pnl.direct import split_fx
from .pnl.flash import FlashCurves, FlashPnl, NoPnl, explain
from .valuation.bonds import (
    BondRequest,
    BondValuation,
    Rejected,
    bond_request,
    discount_curve,
    settlement_date,
    value_bonds,
)
from .valuation.fallback import RUNGS, CarriedSpread, FallbackContext, approximate, par_proxy, simple_pnl

KBD_TOLERANCE_BP = 0.05
log = logging.getLogger(__name__)


@dataclass
class Outcome:
    secid: str
    terms: BondTerms | None
    quote: BondQuote | None
    valuation: BondValuation | None = None
    reason: str | None = None
    detail: str = ""
    pnl: FlashPnl | None = None
    no_pnl: NoPnl | None = None
    core_result: PortfolioRiskResult | None = None
    """The core result this valuation comes from (shared by the positions of one core call)."""


@dataclass
class EodContext:
    snapshots: SnapshotDir
    market: MarketView
    static: StaticView
    kbd: ZcycParameters
    curve: DiscountCurve
    core_market: MarketSnapshot
    calendar: BusinessCalendar
    calendar_window: tuple[date, date]
    kbd_check_bp: float
    cache: ShockedCurveCache
    rates: dict[str, Any] = field(default_factory=dict)
    """Lineage of the rates inputs in ``core_market`` (or the error that left one out)."""

    @property
    def business_date(self) -> date:
        return self.market.business_date

    @property
    def discount(self) -> tuple[str, DiscountCurve, str | None]:
        """The curve RUB bonds are discounted on (``valuation.bonds.discount_curve``): core curve ID, the built curve
        and the note of a KBD fallback (None on RUONIA)."""
        cid, note = discount_curve(self.core_market)
        if cid == KBD_CURVE_ID:
            return cid, self.curve, note
        built = next(c.curve for c in self.core_market.curves if c.curve_id == cid)
        assert isinstance(built, DiscountCurve)
        return cid, built, note

    @property
    def point_in_time_warning(self) -> str | None:
        """Set when the static snapshot is dated after the market: its statuses and faces are later than the market."""
        s = self.static.snapshot.business_date
        return (f"static snapshot dated {s} is later than the market {self.business_date}: status and face are as of "
                f"{s}, faces moved back by the redemption schedule" if s > self.business_date.isoformat() else None)

    def lineage(self) -> dict[str, object]:
        prov = self.snapshots.provenance(self.market.snapshot.id) or {}
        return {
            "market_snapshot_id": self.market.snapshot.id,
            "static_snapshot_id": self.static.snapshot.id,
            "static_business_date": self.static.snapshot.business_date,
            "point_in_time_warning": self.point_in_time_warning,
            "cut_type": self.market.snapshot.cut_type,
            "hub": prov.get("hub"),
            "published_at": prov.get("published_at"),
            "curve_spec": KBD_SPEC,
            "curve_model": KBD_MODEL_VERSION,
            "kbd_check_bp": round(self.kbd_check_bp, 6),
            "calendar": self.calendar.version,
            "calendar_provisional_from": self.static.provisional_from(),
            "rates": self.rates,
            "stale_datasets": self.market.stale,
        }


def check_against_standard_yields(params: ZcycParameters, standard_yields: list[dict[str, str]]) -> float:
    """Largest absolute difference in bp between core's KBD and the published standard yields."""
    return max((abs(kbd_effective_yield(params, float(r["tenor_years"])) * 100 - float(r["yield_pct"])) * 100
                for r in standard_yields), default=0.0)


def with_rates(market: MarketView, static: StaticView, core: MarketSnapshot) -> tuple[MarketSnapshot, dict[str, Any]]:
    """``core`` plus the RUONIA OIS curve (built once), the key-rate projection off it and the RUONIA and
    key-rate fixings. An input that cannot be built is left out with its error in the lineage: the floaters
    that need it are then rejected by core with the reason, the rest of the book is unaffected.

    The lineage says what is missing and why (R14, R15): ``status`` per input (``ok``; ``missing``: the snapshot has
    no quote set of the dataset dated the business date, a coverage gap of the hub; ``error``: the curve could not be
    built; ``no_discount``: the key-rate curve needs the RUONIA OIS curve), ``codes`` for the run log and the pages
    (``rates.input_notes``) and ``coverage``, the hub's own list of the present and missing quote-set series.
    ``curve_error`` keeps the first error message (the RUONIA one, else the key-rate one)."""
    curves, lineage, fixings = list(core.curves), {}, ()
    status: dict[str, str] = {}
    lineage["coverage"] = rates.quote_set_coverage(market.snapshot)
    ois = None
    try:
        ois = rates.ruonia_ois(market.snapshot, static.snapshot)
        curves.append(CurveEntry(ois.entry.curve_id, ois.entry.spec_id, ois.curve))
        lineage["ruonia_ois"] = ois.lineage
        status["ruonia_ois"] = "ok"
    except (rates.RatesInputError, SnapshotError, CoreRejection, ValueError) as e:
        lineage["curve_error"] = str(e)
        status["ruonia_ois"] = "missing" if isinstance(e, rates.QuoteSetMissing) else "error"
    if ois is None:
        status["key_rate_swap"] = "no_discount"
    else:
        try:
            kr = rates.key_rate_projection(market.snapshot, static.snapshot, ois.curve)
            curves.append(kr.entry)
            lineage["key_rate"] = {k: v for k, v in kr.lineage.items() if k != "segments"}
            status["key_rate_swap"] = "ok"
        except (rates.RatesInputError, SnapshotError, CoreRejection, ValueError) as e:
            lineage.setdefault("curve_error", str(e))
            lineage["key_rate_error"] = str(e)
            status["key_rate_swap"] = "missing" if isinstance(e, rates.QuoteSetMissing) else "error"
    try:
        fixings, lineage["fixings"] = rates.fixing_entries(market.snapshot, static.snapshot)
        status["fixings"] = "ok"
    except (SnapshotError, CoreRejection, ValueError) as e:
        lineage["fixings_error"] = str(e)
        status["fixings"] = "error"
    lineage["status"] = status
    lineage["codes"] = rates.input_codes(status)
    return replace(core, curves=tuple(curves), fixings=fixings), lineage


def prepare(snapshots: SnapshotDir, business_date: str, cut_type: str = "eod_final",
            market_snapshot_id: str | None = None) -> EodContext:
    """``market_snapshot_id`` (a stored run's own snapshot) instead of the latest of (date, cut): a later
    correction or a relabelled cut must not change what a run is repriced on."""
    if market_snapshot_id is None:
        snap = snapshots.market(business_date, cut_type)
    else:
        if not (snapshots.root / f"{market_snapshot_id}.json.gz").is_file():
            raise SnapshotError(f"market snapshot {market_snapshot_id} is not in {snapshots.root}")
        snap = snapshots.get(market_snapshot_id)
        if (snap.kind, snap.business_date) != ("market", business_date):
            raise SnapshotError(f"snapshot {market_snapshot_id} is {snap.kind} of {snap.business_date}, "
                                f"not the market of {business_date}")
    market = MarketView(snap)
    assert snap.static_snapshot_id
    static = StaticView(snapshots.get(snap.static_snapshot_id))
    core_market, rates_lineage = with_rates(market, static, market.core_market(static))
    params = market.zcyc_parameters()
    check = check_against_standard_yields(params, market.zcyc["standard_yields"])
    if check > KBD_TOLERANCE_BP:
        raise ValueError(f"KBD curve misses the published standard yields by {check:.3f} bp")
    cal, vf, vt = static.business_calendar("MOEX_STOCK")
    return EodContext(snapshots, market, static, params, kbd_discount_curve(params), core_market, cal, (vf, vt),
                      check, ShockedCurveCache(), rates_lineage)


def value(ctx: EodContext, requests: list[tuple[str, float]], *, second_order: bool = False,
          position_ids: list[str] | None = None, fallback: bool = True,
          carried_spreads: dict[str, CarriedSpread] | None = None, scenarios: tuple = ()) -> list[Outcome]:
    """``position_ids`` (the book's, unique) name the positions in the core results; default the request index.

    With ``fallback`` (the owner's rule: a bond is valued somehow rather than not at all) every outcome
    core cannot value gets a ``valuation.fallback`` rung (market value, stale or model price, par proxy)
    with its quality flag; ``reason``/``detail`` keep why it is not exact. Only a bond with no data at
    all stays without a valuation. ``carried_spreads`` (from the desk store) are the model spreads of
    the latest earlier run: a model price reuses its bond's spread instead of the day's median.
    ``scenarios`` (core ``PortfolioScenario``) are repriced by core: ``core_result.scenarios``."""
    pids = [str(i) for i in range(len(requests))] if position_ids is None else position_ids
    if len(pids) != len(requests) or len(set(pids)) != len(pids):
        raise ValueError("position_ids must be unique, one per request")
    index = {p: i for i, p in enumerate(pids)}
    out: list[Outcome] = []
    todo: list[BondRequest] = []
    for pid, (secid, qty) in zip(pids, requests):
        terms = ctx.static.bonds.get(secid)
        if terms is None:
            out.append(Outcome(secid, None, None, reason="not_in_static_snapshot"))
            continue
        quote = ctx.market.quote(terms)
        o = Outcome(secid, terms, quote)
        out.append(o)
        try:
            if quote is None:
                raise Rejected("no_quote")
            settle = settlement_date(quote.trade_date, terms.settle_lag_days, ctx.calendar, *ctx.calendar_window)
            todo.append(bond_request(pid, terms, quote, settle, qty))
        except Rejected as e:
            o.reason, o.detail = e.reason, e.detail
    valued, results = value_bonds(ctx.core_market, todo, second_order=second_order, cache=ctx.cache,
                                  scenarios=scenarios)
    by_pid = {p.position_id: r for r in results for p in r.positions}
    for pid, v in valued.items():
        o = out[index[pid]]
        if isinstance(v, Rejected):
            o.reason, o.detail = v.reason, v.detail
        else:
            o.valuation, o.core_result = v, by_pid[pid]
    if fallback:
        fb = FallbackContext.build(ctx, carried_spreads)
        for o, (_, qty) in zip(out, requests):
            if o.valuation is None and o.terms is not None:
                try:
                    o.valuation = approximate(ctx, o, qty, fb)
                except Exception as e:  # one bond's bad data must not stop the run: the last rung, flagged
                    log.warning("fallback ladder failed for %s, valued at par proxy", o.secid, exc_info=True)
                    o.valuation = par_proxy(ctx, o, qty, f"fallback ladder failed: {type(e).__name__}: {e}")
    return out


def attach_pnl(prev: EodContext, prev_outcomes: list[Outcome], ctx: EodContext, outcomes: list[Outcome]) -> None:
    """Set ``pnl`` or ``no_pnl`` on every outcome of ``ctx`` priced today. Both lists are in request order."""
    if len(prev_outcomes) != len(outcomes):
        raise ValueError("previous and current requests differ")
    curves = FlashCurves.build(prev.kbd, ctx.kbd, prev.curve, ctx.curve, prev_market=prev.core_market,
                               cur_market=ctx.core_market)
    cache = ShockedCurveCache()
    for p, o in zip(prev_outcomes, outcomes):
        if o.valuation is None:
            continue
        if p.secid != o.secid:
            raise ValueError(f"request order differs: {p.secid} vs {o.secid}")
        if p.valuation is None:
            o.no_pnl = NoPnl("not_priced_prev", f"{prev.business_date}: {p.reason}{': ' + p.detail if p.detail else ''}")
            continue
        try:
            if "par_proxy" in (p.valuation.quality, o.valuation.quality):  # a placeholder value: no PnL is better
                d = prev.business_date if p.valuation.quality == "par_proxy" else ctx.business_date
                raise NoPnl("par_proxy", f"valued at par on {d}, no price")
            if o.valuation.quality in RUNGS or p.valuation.quality in RUNGS:  # no cashflows to reprice
                assert o.terms is not None
                o.pnl = split_fx(simple_pnl(p.valuation, o.valuation, o.terms, ctx), prev, ctx, o.terms)
                continue
            if p.valuation.discount_curve_id != o.valuation.discount_curve_id:  # a KBD fallback on one date
                assert o.terms is not None
                o.pnl = replace(simple_pnl(p.valuation, o.valuation, o.terms, ctx), note=(
                    f"simple PnL: discounted on {p.valuation.spread_curve} on {prev.business_date}, on "
                    f"{o.valuation.spread_curve} on {ctx.business_date}: the spreads are to different curves, "
                    "q x (dirty D - dirty D-1) + received, all in other"))
                continue
            o.pnl = explain(p.valuation, o.valuation, curves, cache)
        except NoPnl as e:
            o.no_pnl = e
