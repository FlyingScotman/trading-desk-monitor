"""EOD valuation of one business date from hub snapshots (slices S1, S2, S4).

``prepare`` loads the market snapshot of the date and the static snapshot it names, the core
market snapshot (core's KBD curve, the RUONIA OIS and key-rate projection curves and the fixings
the floaters need, built once per date), and the settlement calendar. ``value`` prices a list of
(secid, quantity) requests through core ``value_portfolio`` and returns one ``Outcome`` per
request: a valuation or a rejection with its reason. ``attach_pnl`` adds the day-over-day flash
PnL against the valuations of the previous date (``desk.pnl.flash``; a foreign bond core values on its own curve is
explained in its currency with the rate move in ``fx_gross``, one on the fallback ladder gets its carry at its yield,
``valuation.fallback.yield_pnl``, and its FX revaluation in ``fx_gross``, ``desk.pnl.direct.split_fx``). RUB bonds are discounted on RUONIA OIS (``valuation.bonds.discount_curve``;
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

from .curves import fx as fxcurves
from .curves import rates
from .marketdata.adapter import KBD_CURVE_ID, KBD_SPEC, BondQuote, BondTerms, MarketView, StaticView
from .marketdata.bundle import SnapshotDir, SnapshotError
from .marketdata.reference import Reference
from .marketdata.reference import facts as reference_of
from .pnl.direct import split_fx
from .pnl.flash import FlashCurves, FlashPnl, NoPnl, explain
from .valuation.bonds import (
    OFZ_SCHEME,
    BondRequest,
    BondValuation,
    Rejected,
    bond_request,
    discount_curve,
    settlement_date,
    value_bonds,
)
from .valuation.fallback import (
    RUNGS,
    CarriedSpread,
    FallbackContext,
    approximate,
    fx_rate_cbr,
    market_fx,
    par_proxy,
    simple_pnl,
    yield_pnl,
)

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
    core_not_valued: tuple[str, str] | None = None
    """(reason, detail) of a derivative row that is not valued by core from trade terms (none, or core could not):
    its value is the report's (``valuation.positions``); ``publish.derivatives_parallel`` lists it."""


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
    reference: Reference | None = None
    """Issuer and rating facts of the newest static of the cache (``marketdata.reference``, R25); None: the run's
    own static's (``reference_facts``)."""
    proxy_curves: bool = False
    """Setting ``proxy_curves_for_risk`` (default off): show the hub's government par-yield proxies (UST par, ECB AAA,
    Bund; contract 1.3) as the USD/EUR curves of the run, and discount USD / EUR bonds on them through core (``USD UST``,
    ``EUR AAA``; core BACKLOG #132). Off: those curves stay without points and such bonds on their fallback rung."""
    fx: fxcurves.FxMarket = field(default_factory=fxcurves.FxMarket)
    """The run's FX inputs in ``core_market`` (``curves.fx.with_fx``): foreign curves, spots, their lineage."""
    fx_options: fxcurves.FxOptions = field(default_factory=fxcurves.FxOptions)
    """The provisional FX choices of the settings (``curves.fx.FxOptions``)."""
    quote_curves: tuple[CurveEntry, ...] = ()
    """Curves of ``core_market`` that core values risk on by their quotes (the owner's decision of 23.10: every curve's
    risk in par quotes): the RUONIA OIS curve as its ``CurveBootstrapRequest``, so a bucket moves its OIS par quotes
    and the curve is rebootstrapped, the unit of the KBD's par buckets (``rub_ofz_kbd_v3``). ``core_market`` keeps the
    built curves (the curve chart, the flash PnL, the FX curves); ``risk_market`` swaps these in for the core calls
    that publish risk. Until 0.2.6 RUONIA's buckets moved its continuous zero rate (R30), about 14 % more DV01."""
    quote_curve_risk: bool = False
    """Setting ``quote_curve_risk`` (default off; core 0.38 ``ValuationPolicy.quote_curve_risk``, issue #171): the risk of
    the foreign curves core can take in their quotes (``FxMarket.quote_entries``) is in those quotes, as RUONIA's: the
    USD / EUR par-yield proxies (``USD UST``, ``EUR AAA``) for every position on them (``risk_market``), the CNY XCCY
    par quotes for the FX products whose foreign curve it is (``fx_risk_market``; a CNY bond stays on the built curve,
    core 0.38 takes XCCY quotes for FX products only). PV and carry are unchanged; the buckets change unit
    (``curve_units``). The key-rate curve stays on its zero rates: core takes KRS quotes for key-rate swaps and
    KEYRATE futures (``rub_key_rate_swap_v1``), which the desk does not value, not as the projection of a key-rate
    floater (``rub_key_rate_projection_v1``)."""

    @property
    def reference_facts(self) -> Reference:
        return self.reference if self.reference is not None else reference_of(None, self.static)

    @property
    def business_date(self) -> date:
        return self.market.business_date

    @property
    def risk_market(self) -> MarketSnapshot:
        """``core_market`` with ``quote_curves`` in place of their built curves (and, with ``quote_curve_risk``, the
        proxies' par yields): what risk is valued on."""
        q = {c.curve_id: c for c in self.quote_curves}
        if self.quote_curve_risk:
            q.update((i, e) for i, e in self.fx.quote_entries.items() if i != fxcurves.XCCY_CURVE_ID)
        return self._with(q)

    @property
    def fx_risk_market(self) -> MarketSnapshot:
        """``risk_market`` and, with ``quote_curve_risk``, the CNY XCCY par quotes in place of the built curve: what the
        FX products are valued on (``valuation.derivatives``; core rebuilds the quotes on every shock)."""
        q = {c.curve_id: c for c in self.quote_curves}
        if self.quote_curve_risk:
            q.update(self.fx.quote_entries)
        return self._with(q)

    def _with(self, q: dict[str, CurveEntry]) -> MarketSnapshot:
        if not q:
            return self.core_market
        return replace(self.core_market, curves=tuple(q.get(c.curve_id, c) for c in self.core_market.curves))

    def curve_units(self) -> dict[str, str]:
        """Published curve -> the unit of its risk buckets in this run (the run's lineage ``curve_units``; the pages'
        tips, the history's mark of a change of unit, issue #171): ``kbd_par`` (the KBD's par yields, 1Y-20Y+),
        ``ois_quote`` (RUONIA's OIS par quotes), ``par_yield_quote`` (a proxy's par yields), ``xccy_quote_fx`` (the
        FX products' CNY buckets in the XCCY par quotes, a CNY bond's in the built curve's zero rates), ``zero`` (the
        continuous zero rate of the built curve)."""
        quoted = {c.curve_id for c in self.quote_curves}
        out = {"RUB OFZ": "kbd_par", "RUB KEY": "zero",
               "RUB RUONIA": "ois_quote" if rates.RUONIA_OIS_CURVE_ID in quoted else "zero"}
        for cid, c in sorted(self.fx.curves.items()):
            q = self.quote_curve_risk and cid in self.fx.quote_entries
            out[cid] = ("par_yield_quote" if c.proxy else "xccy_quote_fx") if q else "zero"
        return out

    def risk_curve(self, curve_id: str, built: DiscountCurve) -> Any:
        """The source core's risk engine takes for ``curve_id``: its bootstrap request when it is a quote curve,
        else ``built``."""
        return next((c.curve for c in self.quote_curves if c.curve_id == curve_id), built)

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
            "reference_snapshot_id": self.reference_facts.snapshot_id,
            "reference_business_date": self.reference_facts.business_date,
            "point_in_time_warning": self.point_in_time_warning,
            "cut_type": self.market.snapshot.cut_type,
            "hub": prov.get("hub"),
            "published_at": prov.get("published_at"),
            "curve_spec": KBD_SPEC,
            "ofz_scheme": OFZ_SCHEME,
            "curve_model": KBD_MODEL_VERSION,
            "kbd_check_bp": round(self.kbd_check_bp, 6),
            "calendar": self.calendar.version,
            "calendar_provisional_from": self.static.provisional_from(),
            "rates": self.rates,
            "fx": self.fx.lineage,
            "quote_curve_risk": self.quote_curve_risk,
            "curve_units": self.curve_units(),
            "stale_datasets": self.market.stale,
        }


def check_against_standard_yields(params: ZcycParameters, standard_yields: list[dict[str, str]]) -> float:
    """Largest absolute difference in bp between core's KBD and the published standard yields."""
    return max((abs(kbd_effective_yield(params, float(r["tenor_years"])) * 100 - float(r["yield_pct"])) * 100
                for r in standard_yields), default=0.0)


def new_cache() -> ShockedCurveCache:
    """A run's cache of shocked curves (core ``ShockedCurveCache``: by curve content and shift, bounded, never changes a
    number). With RUONIA in its OIS quotes (``EodContext.quote_curves``) a run rebootstraps the curve once per bucket
    shift: about 25 times for the first order and 245 with the Hessian, whatever the number of positions: ~0.08 s each
    on core 0.37, ~5-10 ms on core 0.38 (the same curves to the bit; a Hessian run of the live fixture 21.8 s -> 2.9 s).
    The test session shares one (``tests/conftest.py``): its contexts repeat the same curves."""
    return ShockedCurveCache()


def with_rates(market: MarketView, static: StaticView, core: MarketSnapshot
               ) -> tuple[MarketSnapshot, dict[str, Any], tuple[CurveEntry, ...]]:
    """``core`` plus the RUONIA OIS curve (built once), the key-rate projection off it and the RUONIA and
    key-rate fixings. An input that cannot be built is left out with its error in the lineage: the floaters
    that need it are then rejected by core with the reason, the rest of the book is unaffected.

    The lineage says what is missing and why (R14, R15): ``status`` per input (``ok``; ``missing``: the snapshot has
    no quote set of the dataset dated the business date, a coverage gap of the hub; ``error``: the curve could not be
    built; ``no_discount``: the key-rate curve needs the RUONIA OIS curve), ``codes`` for the run log and the pages
    (``rates.input_notes``) and ``coverage``, the hub's own list of the present and missing quote-set series.
    ``curve_error`` keeps the first error message (the RUONIA one, else the key-rate one). The third element: the
    RUONIA OIS curve as its bootstrap request (``EodContext.quote_curves``), empty without it."""
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
    return replace(core, curves=tuple(curves), fixings=fixings), lineage, (ois.entry,) if ois else ()


def market_rates(market: MarketView) -> dict[str, tuple[float, str] | None]:
    """ccy -> the market rate (``fallback.FX_MARKET``) and its source, for every currency of the snapshot's rates."""
    return {c: market_fx(market.fx(c)) for c in sorted({r["ccy"] for r in market.fx_rates if r.get("ccy")} - {"RUB"})}


def prepare(snapshots: SnapshotDir, business_date: str, cut_type: str = "eod_final",
            market_snapshot_id: str | None = None, *, fx_options: fxcurves.FxOptions | None = None) -> EodContext:
    """``market_snapshot_id`` (a stored run's own snapshot) instead of the latest of (date, cut): a later
    correction or a relabelled cut must not change what a run is repriced on. ``fx_options``: the provisional FX
    settings (``curves.fx.FxOptions``; default its defaults)."""
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
    core_market, rates_lineage, quote_curves = with_rates(market, static, market.core_market(static))
    fx_options = fx_options or fxcurves.FxOptions()
    core_market, fx_market = fxcurves.with_fx(market, static, core_market, market_rates(market), fx_options)
    params = market.zcyc_parameters()
    check = check_against_standard_yields(params, market.zcyc["standard_yields"])
    if check > KBD_TOLERANCE_BP:
        raise ValueError(f"KBD curve misses the published standard yields by {check:.3f} bp")
    cal, vf, vt = static.business_calendar("MOEX_STOCK")
    # par buckets of the KBD (rub_ofz_kbd_v3), as core's: the risk of an OFZ on the fallback ladder sums with an exact one's
    kbd = kbd_discount_curve(params, zero_shock="annual_par")
    return EodContext(snapshots, market, static, params, kbd, core_market, cal, (vf, vt),
                      check, new_cache(), rates_lineage, fx=fx_market, fx_options=fx_options,
                      quote_curves=quote_curves)


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
    valued, results = value_bonds(ctx.risk_market, todo, second_order=second_order, cache=ctx.cache,
                                  scenarios=scenarios, foreign=foreign_bond_curves(ctx),
                                  quote_curve_risk=ctx.quote_curve_risk)
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


def foreign_bond_curves(ctx: EodContext) -> dict[str, tuple[str, ...]]:
    """ccy -> the curves a foreign bond is discounted on, in order (core ``foreign_curves`` then its
    ``foreign_curve_fallbacks``; core BACKLOG #132): CNY on the XCCY curve; USD and EUR on the government par-yield
    proxies only with the setting ``proxy_curves_for_risk``. A currency without one stays on the fallback ladder."""
    out: dict[str, tuple[str, ...]] = {"CNY": (fxcurves.XCCY_CURVE_ID,)}
    if ctx.proxy_curves:
        out.update({ccy: (cid,) for ccy, cid, _ in fxcurves.PROXY_SPECS.values()})
    return {c: ids for c, ids in out.items() if any(i in ctx.fx.curves for i in ids)}


def with_cbr(pnl: FlashPnl, prev: EodContext, ctx: EodContext) -> FlashPnl:
    """A foreign bond's flash PnL with ``fx_gross_cbr``: its FX delta of D-1 x the move of the CBR rates in force (the
    official basis, as ``pnl.direct.split_fx``); as is for a RUB bond or without a CBR rate on either date."""
    if pnl.fx_rates is None:
        return pnl
    c0, c1 = fx_rate_cbr(prev, pnl.ccy), fx_rate_cbr(ctx, pnl.ccy)
    return replace(pnl, fx_gross_cbr=pnl.fx_delta_ccy * (c1[0] - c0[0])) if c0 and c1 else pnl


def attach_pnl(prev: EodContext, prev_outcomes: list[Outcome], ctx: EodContext, outcomes: list[Outcome]) -> None:
    """Set ``pnl`` or ``no_pnl`` on every outcome of ``ctx`` priced today. Both lists are in request order."""
    if len(prev_outcomes) != len(outcomes):
        raise ValueError("previous and current requests differ")
    curves = FlashCurves.build(prev.kbd, ctx.kbd, prev.curve, ctx.curve, prev_market=prev.core_market,
                               cur_market=ctx.core_market)
    cache = new_cache()
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
            # no cashflows to reprice: the dirty price move (a foreign bond's carry at its yield, ``yield_pnl``)
            if o.valuation.quality in RUNGS or p.valuation.quality in RUNGS:
                assert o.terms is not None
                o.pnl = split_fx(yield_pnl(p.valuation, o.valuation, o.terms, prev, ctx), prev, ctx, o.terms)
                continue
            if p.valuation.discount_curve_id != o.valuation.discount_curve_id:  # a KBD (or foreign) fallback on one date
                assert o.terms is not None
                o.pnl = split_fx(replace(simple_pnl(p.valuation, o.valuation, o.terms, ctx), note=(
                    f"simple PnL: discounted on {p.valuation.spread_curve} on {prev.business_date}, on "
                    f"{o.valuation.spread_curve} on {ctx.business_date}: the spreads are to different curves, "
                    "q x (dirty D - dirty D-1) + received, all in other")), prev, ctx, o.terms)
                continue
            # a foreign bond (core 0.34) in its currency on its own curve, the rate move in fx_gross
            o.pnl = with_cbr(explain(p.valuation, o.valuation, curves, cache), prev, ctx)
        except NoPnl as e:
            o.no_pnl = e
