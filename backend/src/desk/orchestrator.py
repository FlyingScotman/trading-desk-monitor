"""EOD valuation of one business date from hub snapshots (slices S1, S2, S4).

``prepare`` loads the market snapshot of the date and the static snapshot it names, the core
market snapshot (core's KBD curve, the RUONIA OIS and key-rate projection curves and the fixings
the floaters need, built once per date), and the settlement calendar. ``value`` prices a list of
(secid, quantity) requests through core ``value_portfolio`` and returns one ``Outcome`` per
request: a valuation or a rejection with its reason; ``horizon`` carries the same requests to a later date through
core ``horizon_portfolio`` (the forward carry, ``scenario.carry``). ``attach_pnl`` adds the day-over-day flash
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
    PortfolioHorizonResult,
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
from .pnl.flash import FlashCurves, FlashPnl, NoPnl, explain, price_discovery
from .pnl.reasons import (
    CORE_REFUSED,
    DISCOUNT_CURVE_SWITCH,
    FALLBACK_PRICE,
    NO_CURVE_ON_DATE,
    PRICE_SOURCE_SWITCH,
    other_reason,
)
from .valuation.bonds import (
    OFZ_SCHEME,
    OFZ_SCHEME_P,
    BondCarry,
    BondRequest,
    BondValuation,
    Rejected,
    bond_request,
    discount_curve,
    horizon_bonds,
    settlement_date,
    value_bonds,
)
from .valuation.fallback import (
    MODEL_Z_TOLERANCE_BP,
    RUNGS,
    CarriedSpread,
    FallbackContext,
    approximate,
    at_last_price,
    at_model_price,
    by_core,
    fx_rate_cbr,
    market_fx,
    model_notes,
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
    swap_curves: tuple[CurveEntry, ...] = ()
    """Curves only a derivative reads, kept out of ``core_market`` so the bonds' inputs stay as they are: the key-rate
    forward curve (``rates.KEY_RATE_SWAP_CURVE_ID``, ``rub_key_rate_swap_v1``) a key-rate swap projects on."""
    quote_curve_risk: bool = False
    """Setting ``quote_curve_risk`` (default off; core 0.38 ``ValuationPolicy.quote_curve_risk``, issue #171): the risk of
    the foreign curves core can take in their quotes (``FxMarket.quote_entries``) is in those quotes, as RUONIA's: the
    USD / EUR par-yield proxies (``USD UST``, ``EUR AAA``) for every position on them (``risk_market``), the CNY XCCY
    par quotes for the FX products whose foreign curve it is (``fx_risk_market``; a CNY bond stays on the built curve,
    core 0.38 takes XCCY quotes for FX products only). PV and carry are unchanged; the buckets change unit
    (``curve_units``). The key-rate curve stays on its zero rates: core takes KRS quotes for key-rate swaps and
    KEYRATE futures (``rub_key_rate_swap_v1``), not as the projection of a key-rate floater
    (``rub_key_rate_projection_v1``); the desk's key-rate swaps (#45) project on the built forward curve
    (``swap_curves``), so their key-rate risk is in its forward segments, not in KRS quotes, with the setting too."""

    ofz_basis: bool = True
    """An OFZ on the KBD valued in RUONIA + basis (core 0.39 ``ValuationPolicy.ofz_basis``, TDM#213: its RUONIA and basis
    risk both computed; ``valuation.bonds.OFZ_SCHEME``) when the RUONIA OIS curve is in its quotes (``quote_curves``).
    Off: the KBD alone, the basis view a copy of its par risk (``OFZ_SCHEME_P``, 0.2.6): what the exact scenario of a
    0.2.6 run reprices on (``scenario.exact._context``)."""

    @property
    def ofz_scheme(self) -> str:
        """The run's lineage ``ofz_scheme`` (``valuation.bonds.OFZ_SCHEME``): RUONIA + basis when its OFZ can be valued
        so (``ofz_basis`` and RUONIA OIS quotes), else the KBD alone."""
        quoted = any(c.curve_id == rates.RUONIA_OIS_CURVE_ID for c in self.quote_curves)
        return OFZ_SCHEME if self.ofz_basis and quoted else OFZ_SCHEME_P

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
            "ofz_scheme": self.ofz_scheme,
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


def with_rates(market: MarketView, static: StaticView, core: MarketSnapshot,
               kbd_history: dict[date, ZcycParameters] | None = None
               ) -> tuple[MarketSnapshot, dict[str, Any], tuple[CurveEntry, ...], tuple[CurveEntry, ...]]:
    """``core`` plus the RUONIA OIS curve (built once), the key-rate projection off it and the RUONIA and
    key-rate fixings. An input that cannot be built is left out with its error in the lineage: the floaters
    that need it are then rejected by core with the reason, the rest of the book is unaffected.

    The lineage says what is missing and why (R14, R15): ``status`` per input (``ok``; ``missing``: the snapshot has
    no quote set of the dataset dated the business date, a coverage gap of the hub; ``error``: the curve could not be
    built; ``no_discount``: the key-rate curve needs the RUONIA OIS curve), ``codes`` for the run log and the pages
    (``rates.input_notes``) and ``coverage``, the hub's own list of the present and missing quote-set series.
    ``curve_error`` keeps the first error message (the RUONIA one, else the key-rate one). The third element: the
    RUONIA OIS curve as its bootstrap request (``EodContext.quote_curves``), empty without it; the fourth: the key-rate
    forward curve for key-rate swaps (``EodContext.swap_curves``), empty without the key-rate curve. ``kbd_history``:
    the KBD of earlier days for the KBD floaters' past fixings (``rates.kbd_history``, TDM#33)."""
    curves, lineage, fixings, swap_curves = list(core.curves), {}, (), ()
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
            swap_curves = (kr.swap_entry,) if kr.swap_entry is not None else ()
            lineage["key_rate"] = {k: v for k, v in kr.lineage.items() if k != "segments"}
            status["key_rate_swap"] = "ok"
        except (rates.RatesInputError, SnapshotError, CoreRejection, ValueError) as e:
            lineage.setdefault("curve_error", str(e))
            lineage["key_rate_error"] = str(e)
            status["key_rate_swap"] = "missing" if isinstance(e, rates.QuoteSetMissing) else "error"
    try:
        fixings, lineage["fixings"] = rates.fixing_entries(market.snapshot, static.snapshot, kbd_history=kbd_history)
        status["fixings"] = "ok"
    except (SnapshotError, CoreRejection, ValueError) as e:
        lineage["fixings_error"] = str(e)
        status["fixings"] = "error"
    lineage["status"] = status
    lineage["codes"] = rates.input_codes(status)
    return (replace(core, curves=tuple(curves), fixings=fixings), lineage, (ois.entry,) if ois else (),
            swap_curves)


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
    core_market, rates_lineage, quote_curves, swap_curves = with_rates(
        market, static, market.core_market(static), rates.kbd_history(snapshots, business_date, static.snapshot))
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
                      quote_curves=quote_curves, swap_curves=swap_curves)


def value(ctx: EodContext, requests: list[tuple[str, float]], *, second_order: bool = False,
          position_ids: list[str] | None = None, fallback: bool = True,
          carried_spreads: dict[str, CarriedSpread] | None = None, scenarios: tuple = ()) -> list[Outcome]:
    """``position_ids`` (the book's, unique) name the positions in the core results; default the request index.

    With ``fallback`` (the owner's rule: a bond is valued somehow rather than not at all) every outcome
    core cannot value gets a ``valuation.fallback`` rung (market value, stale or model price, par proxy)
    with its quality flag; ``reason``/``detail`` keep why it is not exact. Only a bond with no data at
    all stays without a valuation. ``carried_spreads`` (from the desk store) are the model spreads of
    the latest earlier run: a model price reuses its bond's spread instead of the day's median. A RUB bond without a
    price of its own is valued by core at the ladder's price (``fallback.at_last_price``, TDM#383) or at its model price
    (``fallback.at_model_price``, TDM#441), its rung's quality kept; the ladder's own estimate stays for the bonds core
    refuses there. ``scenarios`` (core ``PortfolioScenario``) are repriced by core: ``core_result.scenarios``."""
    pids = _pids(requests, position_ids)
    index = {p: i for i, p in enumerate(pids)}
    out, todo = bond_requests(ctx, requests, pids)
    valued, results = value_bonds(ctx.risk_market, todo, second_order=second_order, cache=ctx.cache,
                                  scenarios=scenarios, foreign=foreign_bond_curves(ctx),
                                  quote_curve_risk=ctx.quote_curve_risk, ofz_basis=ctx.ofz_basis)
    by_pid = {p.position_id: r for r in results for p in r.positions}
    for pid, v in valued.items():
        o = out[index[pid]]
        if isinstance(v, Rejected):
            o.reason, o.detail = v.reason, v.detail
        else:
            o.valuation, o.core_result = v, by_pid[pid]
    if fallback:
        fb = FallbackContext.build(ctx, carried_spreads)
        # a bond without a price of its own today: core at the ladder's price, its risk and model with it (TDM#383)
        at_price = {pid: (o, r) for pid, o, (_, qty) in zip(pids, out, requests) if o.valuation is None
                    and (r := at_last_price(ctx, pid, o, qty, fb)) is not None}
        if at_price:
            valued, by_pid = _core_at(ctx, [r[0] for _, r in at_price.values()], second_order, scenarios)
            for pid, v in valued.items():
                o, (_, quality, notes) = at_price[pid]
                if isinstance(v, BondValuation):
                    o.valuation, o.core_result = replace(v, quality=quality, assumptions=notes + v.assumptions), by_pid[pid]
                else:  # the ladder's own rung below, saying core was tried
                    o.detail = (f"{o.detail}; " if o.detail else "") + f"core at the last price refused: {v.reason}" \
                        + (f" ({v.detail})" if v.detail else "")
        for o, (_, qty) in zip(out, requests):
            if o.valuation is None and o.terms is not None:
                try:
                    o.valuation = approximate(ctx, o, qty, fb)
                except Exception as e:  # one bond's bad data must not stop the run: the last rung, flagged
                    log.warning("fallback ladder failed for %s, valued at par proxy", o.secid, exc_info=True)
                    o.valuation = par_proxy(ctx, o, qty, f"fallback ladder failed: {type(e).__name__}: {e}")
        # a model price: core at the model's price, its flows, risk and carry with it (TDM#441)
        at_model = {pid: (o, r) for pid, o, (_, qty) in zip(pids, out, requests) if (m := o.valuation) is not None
                    and m.quality == "model_price" and (r := at_model_price(ctx, pid, o, qty, m.clean_pct)) is not None}
        if at_model:
            valued, by_pid = _core_at(ctx, [r for _, r in at_model.values()], second_order, scenarios)
            for pid, v in valued.items():
                o, m = at_model[pid][0], at_model[pid][0].valuation
                assert m is not None
                if isinstance(v, BondValuation) and abs(v.z_spread_bp - m.z_spread_bp) <= MODEL_Z_TOLERANCE_BP:
                    o.valuation = replace(v, quality="model_price", model_spread_source=m.model_spread_source,
                                          assumptions=model_notes(m) + v.assumptions)
                    o.core_result = by_pid[pid]
                else:  # the ladder's own model price, saying core was tried
                    why = (f"core at the model price refused: {v.reason}" + (f" ({v.detail})" if v.detail else "")
                           if isinstance(v, Rejected) else f"core at the model price: Z {v.z_spread_bp:.4f} bp, not "
                           f"the model's {m.z_spread_bp:.4f} bp: the ladder's model price")
                    o.valuation = replace(m, assumptions=(*m.assumptions, why))
    return out


def _core_at(ctx: EodContext, reqs: list[BondRequest], second_order: bool, scenarios: tuple
             ) -> tuple[dict[str, BondValuation | Rejected], dict[str, PortfolioRiskResult]]:
    """``value_bonds`` of fallback rungs' requests at their price (``at_last_price``, ``at_model_price``) on the run's
    inputs, and the core result of each position."""
    valued, results = value_bonds(ctx.risk_market, reqs, second_order=second_order, cache=ctx.cache, scenarios=scenarios,
                                  foreign=foreign_bond_curves(ctx), quote_curve_risk=ctx.quote_curve_risk,
                                  ofz_basis=ctx.ofz_basis)
    return valued, {p.position_id: r for r in results for p in r.positions}


@dataclass
class CarryOutcome:
    """One request of ``horizon``: its carry to the horizon by core, or why there is none."""

    secid: str
    carry: BondCarry | None = None
    reason: str | None = None
    detail: str = ""


def bond_requests(ctx: EodContext, requests: list[tuple[str, float]], pids: list[str]
                  ) -> tuple[list[Outcome], list[BondRequest]]:
    """One ``Outcome`` per (secid, quantity) request (the bond's terms and quote, or the refusal before core) and the
    ``BondRequest`` of each one the backend's checks pass (``value``, ``horizon``)."""
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
    return out, todo


def _pids(requests: list, position_ids: list[str] | None) -> list[str]:
    pids = [str(i) for i in range(len(requests))] if position_ids is None else position_ids
    if len(pids) != len(requests) or len(set(pids)) != len(pids):
        raise ValueError("position_ids must be unique, one per request")
    return pids


def horizon(ctx: EodContext, requests: list[tuple[str, float]], horizon_date: date, *,
            position_ids: list[str] | None = None, model_prices: dict[str, float] | None = None
            ) -> tuple[list[CarryOutcome], PortfolioHorizonResult | None]:
    """Carry and roll-down of (secid, quantity) ``requests`` from the context's date to ``horizon_date`` through core
    ``horizon_portfolio`` (``valuation.bonds.horizon_bonds``) on the inputs ``value`` values them on: the same
    backend checks, instruments, quotes, curves (``risk_market``) and policy. A RUB bond without a price of its own
    today is carried at the ladder's price (``fallback.at_last_price``, TDM#383: an intraday cut before its first
    trade, a day it did not trade; ``detail`` says which), else, without a price in the ladder's window, at the clean
    price of ``model_prices`` (position ID -> clean %: its run's model price, ``fallback.at_model_price``, TDM#441);
    otherwise no fallback ladder: a bond core cannot value has no carry, with its reason. ``position_ids`` as in
    ``value``. Returns one ``CarryOutcome`` per request and the core result."""
    pids = _pids(requests, position_ids)
    pre, todo = bond_requests(ctx, requests, pids)
    out = [CarryOutcome(o.secid, reason=o.reason, detail=o.detail) for o in pre]
    fb, at_price, models = FallbackContext.build(ctx), {}, model_prices or {}
    for pid, o, (_, qty) in zip(pids, pre, requests):  # without a price today: at the ladder's price (TDM#383)
        if (r := at_last_price(ctx, pid, o, qty, fb)) is not None:
            todo.append(r[0])
            at_price[pid] = r
        elif pid in models and (m := at_model_price(ctx, pid, o, qty, models[pid])) is not None:  # TDM#441
            todo.append(m)
            at_price[pid] = (m, "model_price", ("", "model price of the run", "valued by core at this price"))
    carried, result = horizon_bonds(ctx.risk_market, todo, horizon_date, cache=ctx.cache, foreign=foreign_bond_curves(ctx),
                                    quote_curve_risk=ctx.quote_curve_risk, ofz_basis=ctx.ofz_basis)
    index = {p: i for i, p in enumerate(pids)}
    for pid, v in carried.items():
        o = out[index[pid]]
        if isinstance(v, Rejected):
            o.reason, o.detail = v.reason, v.detail
        else:
            o.carry = v
            if pid in at_price:
                o.reason, o.detail = None, "; ".join(at_price[pid][2][1:])
    return out, result


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
    """Set ``pnl`` or ``no_pnl`` on every outcome of ``ctx`` priced today. Both lists are in request order. A bond valued
    by core on both dates (``fallback.by_core``: exact, or a rung at its price) takes the exact flash steps (a rung keeps
    the simple PnL when they refuse, ``EXACT_REFUSALS``); one on a
    fallback rung off core on either date, or discounted on another curve on each, the simple PnL; each with why its PnL
    is (partly) in ``other`` (``pnl.extra.other_reason``, ``ladder_reason``, ``curve_switch``; ``pnl.flash``)."""
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
        pv, ov = p.valuation, o.valuation
        try:
            if "par_proxy" in (pv.quality, ov.quality):  # a placeholder value: no PnL is better
                d = prev.business_date if pv.quality == "par_proxy" else ctx.business_date
                raise NoPnl("par_proxy", f"valued at par on {d}, no price")
            # no cashflows to reprice: the dirty price move (a foreign bond's carry at its yield, ``yield_pnl``); a
            # rung core valued at its price on both dates has them (TDM#383, TDM#441)
            if (ov.quality in RUNGS or pv.quality in RUNGS) and not (by_core(pv) and by_core(ov)):
                assert o.terms is not None
                pnl = yield_pnl(pv, ov, o.terms, prev, ctx)
                o.pnl = split_fx(pnl if pnl.other_reason else replace(pnl, other_reason=ladder_reason(prev, p, ctx, o)),
                                 prev, ctx, o.terms)
                continue
            if pv.discount_curve_id != ov.discount_curve_id:  # a KBD (or foreign) fallback on one date
                assert o.terms is not None
                o.pnl = split_fx(replace(simple_pnl(pv, ov, o.terms, ctx), note=(
                    f"simple PnL: discounted on {pv.spread_curve} on {prev.business_date}, on "
                    f"{ov.spread_curve} on {ctx.business_date}: the spreads are to different curves, "
                    "q x (dirty D - dirty D-1) + received, all in other"), other_reason=curve_switch(prev, pv, ctx, ov)),
                                 prev, ctx, o.terms)
                continue
            # a foreign bond (core 0.34) in its currency on its own curve, the rate move in fx_gross; a model price on
            # one date: its spread step is price discovery, in other (TDM#441)
            try:
                o.pnl = with_cbr(price_discovery(explain(pv, ov, curves, cache), pv, ov), prev, ctx)
            except NoPnl as e:
                # a rung core valued on both dates had the simple PnL before the exact steps (TDM#441): when they
                # refuse, it keeps it, with why in other_reason
                if e.reason not in EXACT_REFUSALS or not (ov.quality in RUNGS or pv.quality in RUNGS):
                    raise
                assert o.terms is not None
                why = f"exact flash refused: {e.reason}{': ' + e.detail if e.detail else ''}"
                pnl, lr = yield_pnl(pv, ov, o.terms, prev, ctx), ladder_reason(prev, p, ctx, o)
                reason = pnl.other_reason or (lr if lr["code"] == PRICE_SOURCE_SWITCH else other_reason(
                    CORE_REFUSED, why, core_code=e.reason, rung=ov.quality if ov.quality in RUNGS else pv.quality,
                    date=ov.valuation_date.isoformat()))
                o.pnl = split_fx(replace(pnl, note=f"{pnl.note}; {why}", other_reason=reason), prev, ctx, o.terms)
        except NoPnl as e:
            o.no_pnl = e


EXACT_REFUSALS = ("curve_missing", "core_error", "identity_failed")
"""Refusals of the exact steps after which a rung core valued on both dates keeps the simple PnL (``attach_pnl``)."""


def missing_curve(ctx: EodContext) -> str | None:
    """The first published curve the date's market lacks (``rates.input_notes`` of its rates lineage), or None."""
    return next((n["curve"] for n in rates.input_notes(ctx.rates, ctx.business_date) if n.get("curve")), None)


def curve_switch(prev: EodContext, pv: BondValuation, ctx: EodContext, ov: BondValuation) -> dict[str, Any]:
    """``discount_curve_switch`` (``pnl.reasons``): the curves of D-1 and D, and the one a date lacks if known (``date``:
    the valuation date)."""
    gone = next(((c, v.valuation_date) for d, v in ((ctx, ov), (prev, pv)) if (c := missing_curve(d))), (None, None))
    return other_reason(DISCOUNT_CURVE_SWITCH, f"{pv.spread_curve} on {pv.valuation_date}, {ov.spread_curve} on "
                        f"{ov.valuation_date}" + (f": no {gone[0]} on {gone[1]}" if gone[0] else ""),
                        curves=[pv.spread_curve, ov.spread_curve], curve=gone[0],
                        date=gone[1].isoformat() if gone[1] else None)


def ladder_reason(prev: EodContext, p: Outcome, ctx: EodContext, o: Outcome) -> dict[str, Any]:
    """Why the simple PnL of a bond on a fallback rung off core on either date is all in ``other`` (``pnl.reasons``): a
    model price against a market one (``price_source_switch``), else the rung of D (of D-1 when D is on core) and why
    the exact path refused it: a curve the date lacks (``no_curve_on_date``), another refusal of core
    (``core_refused``), anything else (``fallback_price``: no price, terms core does not take). ``date``: the rung's
    valuation date."""
    assert p.valuation is not None and o.valuation is not None
    qs = (p.valuation.quality, o.valuation.quality)
    if "model_price" in qs and qs[0] != qs[1]:
        way = "model -> market" if qs[0] == "model_price" else "market -> model"
        return other_reason(PRICE_SOURCE_SWITCH, way, rung=qs[1] if qs[0] == "model_price" else qs[0])
    side, d = (o, ctx) if not by_core(o.valuation) else (p, prev)
    v = side.valuation
    assert v is not None
    why, on = side.reason or "", v.valuation_date
    if why == "core_missing_market_data" and (curve := missing_curve(d)) is not None:
        return other_reason(NO_CURVE_ON_DATE, f"{curve} not on {on}: {v.quality}", curve=curve, date=on.isoformat(),
                            rung=v.quality, core_code=why)
    if why.startswith("core_"):
        return other_reason(CORE_REFUSED, f"{why}{': ' + side.detail if side.detail else ''}", core_code=why,
                            rung=v.quality, date=on.isoformat())
    return other_reason(FALLBACK_PRICE, f"{v.quality} on {on}" + (f" ({why})" if why else ""), rung=v.quality,
                        why=why or None, date=on.isoformat())
