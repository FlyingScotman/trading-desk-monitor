"""FX market inputs for core (WP-M2; core 0.34 foreign bond curves, 0.35 FX forwards for any currency).

``with_fx`` adds to the run's core market snapshot, once per date, next to ``orchestrator.with_rates``:

- **FX rates and spots.** ``FxRate`` per currency (bonds convert at it) and ``FxSpot`` (FX forwards and crosses value
  from it) at the backend's market rate (``fallback.FX_MARKET``: MOEX 19:00, else 14:00, else CETS close, else CBR;
  setting ``fx_spot_source`` ``cets_tom`` takes the CETS TOM close first). The spot's value date is the hub's TOM
  ``settle_date`` (contract 1.4), else TOM on the pair's settlement calendar; that calendar is the joint calendar of
  the hub's ``settlement_calendars`` (``FX:<ccy>``: MOEX_FX plus the currency's own, CN_INTERBANK, US_FED, TARGET,
  CH_ZURICH), each present in the static snapshot.
- **FX-implied curves** (``<CCY> FX``, spec ``<ccy>_fx_implied_v1``): the CETS swap points of the currency (``fx_swaps``,
  1.4) chained from the spot, plus the dated FORTS FX futures past the last swap (setting ``fx_implied_futures``, on)
  as outrights at their delivery date. Core builds the curve in ``value_portfolio`` (``FxImpliedCurveRequest`` on the
  RUONIA OIS curve); this module builds it once more for the lineage, the run's curve points and to leave out a
  currency whose quotes do not build (no TOD/TOM anchor, a gap): its lineage says why.
- **CNY XCCY** (``CNY XCCY``, ``cny_ruonia_xccy_v1``): the hub's ``cny_ruonia_xccy`` par quotes (percent, divided by
  100) bootstrapped by core ``bootstrap_xccy_curve`` on RUONIA OIS (MOEX XCS CNYRUB terms; the joint SPFI, MOSCOW,
  BEIJING and RUONIA calendar; the notional rate is the spot, provisional). It discounts CNY bonds.
- **Government par-yield proxies** (``USD UST``, ``EUR AAA``; ``usd_ust_par_proxy_v1``, ``eur_ecb_aaa_par_proxy_v1``):
  ``bootstrap_par_yield_curve`` of the hub's ``usd_ust_par`` / ``eur_aaa_par`` sets (percent / 100), dated the business
  date (the hub's set may be a day or two older: said in the lineage). Bonds use them only with the setting
  ``proxy_curves_for_risk`` (core BACKLOG #132); they are never an FX forward's curve (a proxy needs a calibrated basis).
- **NDF fixings**: the CBR official rates of the snapshot as ``FxFixing(ccy, "CBR", set date, rate)``.
- **Quote curves** (``FxMarket.quote_entries``, core 0.38, issue #171): the CNY XCCY par quotes
  (``XccyCurveBootstrapRequest``) and the proxies' par yields (``ParYieldCurveRequest``) the curves above are built from,
  under the same curve IDs. ``core_market`` keeps the built curves; with the setting ``quote_curve_risk``
  (``orchestrator.EodContext.quote_curve_risk``) core takes the risk of those curves in their quotes: the proxies for
  every position on them, the XCCY quotes for the FX products only (a CNY bond needs the built curve in core 0.38). The
  XCCY quotes are offered only on the FX spot's value date (core checks it); otherwise the lineage says why.

Every core curve ID of a foreign curve is its published name (``IR|CNY FX|1Y``), so its risk needs no renaming. An input
that cannot be built is left out with its error in the lineage; the positions that need it are then valued on their
fallback (a bond's ladder, an FX forward's report value), the rest of the book is unaffected.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from rub_fixed_income import (
    BusinessCalendar,
    CalendarEntry,
    CoreRejection,
    CurveEntry,
    DiscountCurve,
    FxBasis,
    FxFixing,
    FxImpliedCurveRequest,
    FxOutrightQuote,
    FxRate,
    FxSpot,
    FxSwapPointsQuote,
    MarketSnapshot,
    ParYieldCurveRequest,
    ParYieldQuote,
    XccyCurveBootstrapRequest,
    XccyParQuote,
    bootstrap_par_yield_curve,
    bootstrap_xccy_curve,
    fx_implied_spec_id,
    fx_value_date,
    joint_calendar,
    xcs_cnyrub_convention,
)

from ..marketdata import adapter_rates as ar
from ..marketdata.bundle import SnapshotError
from . import proxy as proxies
from . import rates

FX_CURVE_KINDS = ("fx_implied", "xccy")
"""The curve an FX forward's foreign leg can be on (setting ``fx_foreign_curve``; the other is its fallback)."""
PROXY_SPECS = {"usd_ust_par": ("USD", "USD UST", "usd_ust_par_proxy_v1"),
               "eur_aaa_par": ("EUR", "EUR AAA", "eur_ecb_aaa_par_proxy_v1")}
"""Hub proxy quote set -> (currency, curve ID, core spec). ``eur_bund_par`` has no core spec (core BACKLOG #130)."""
XCCY_CURVE_ID, XCCY_SPEC = "CNY XCCY", "cny_ruonia_xccy_v1"
CBR_SOURCE = "CBR"
SPOT_SOURCES = ("market", "cets_tom")
SWAP_LAG = {"TOD": 0, "TOM": 1, "SPT": 2}
MAIN_CCYS = ("USD", "EUR", "CNY")
"""Currencies whose missing FX-implied curve the lineage reports (the others only when they have quotes)."""


def implied_curve_id(ccy: str) -> str:
    return f"{ccy} FX"


def fx_calendar_id(ccy: str) -> str:
    return f"FX:{ccy}"


@dataclass(frozen=True)
class FxOptions:
    """The run's provisional FX choices (settings, docs/program/DECISIONS.md open questions)."""

    foreign_curve: str = "fx_implied"
    """``fx_foreign_curve``: the curve of an FX forward's foreign leg, ``fx_implied`` (from swap points) or ``xccy``
    (CNY only); the other one is its fallback when absent."""
    implied_futures: bool = True
    """``fx_implied_futures``: FORTS FX futures past the last swap point extend the implied curve."""
    spot_source: str = "market"
    """``fx_spot_source``: ``market`` (the FX_MARKET chain, as bonds and the OCP) or ``cets_tom`` (CETS TOM close)."""
    unlinked_terms: bool = False
    """``fx_unlinked_terms``: value a trade_terms trade that no positions row names as a position of its own."""
    fixing_sources: tuple[tuple[str, str], ...] = (("CBR", "CBR"), ("ЦБ", "CBR"), ("ЦБ РФ", "CBR"))
    """``ndf_fixing_sources``: the report's fixing source -> the hub source (``CBR``: the CBR official rate)."""
    futures_by_ticker: bool = False
    """``futures_by_ticker``: a position row naming a FORTS FX future by ticker is valued by core (P3)."""

    def fixing_source(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        m = {k.upper(): v for k, v in self.fixing_sources}
        return m.get(raw.strip().upper(), raw.strip())


def parse_fixing_sources(text: str | None) -> tuple[tuple[str, str], ...]:
    """``"CBR=CBR,ЦБ=CBR"`` -> pairs; an entry without ``=`` maps to itself."""
    out = []
    for part in (text or "").split(","):
        if part.strip():
            k, _, v = part.partition("=")
            out.append((k.strip(), (v or k).strip()))
    return tuple(out)


@dataclass(frozen=True)
class ForeignCurve:
    """A foreign curve of the run: core ID = published curve (``CNY FX``, ``CNY XCCY``, ``USD UST``)."""

    curve_id: str
    ccy: str
    kind: str
    """``fx_implied`` | ``xccy`` | ``proxy``."""
    spec_id: str
    name: str
    """The run's curve reference name (Russian)."""
    curve: DiscountCurve
    """Built here: the points, the scenarios' view."""

    @property
    def proxy(self) -> bool:
        return self.kind == "proxy"


@dataclass
class FxMarket:
    curves: dict[str, ForeignCurve] = field(default_factory=dict)
    """By curve ID."""
    spots: dict[str, FxSpot] = field(default_factory=dict)
    rates: dict[str, tuple[float, str]] = field(default_factory=dict)
    """ccy -> (RUB per unit, source) of the spot."""
    lineage: dict[str, Any] = field(default_factory=dict)
    quote_entries: dict[str, CurveEntry] = field(default_factory=dict)
    """By curve ID: the quotes a curve of ``curves`` is built from (``XCCY_CURVE_ID``: ``XccyCurveBootstrapRequest``; a
    proxy: ``ParYieldCurveRequest``), for core's risk in quotes (module docstring, "Quote curves")."""

    def curve(self, ccy: str, kind: str) -> ForeignCurve | None:
        return next((c for c in self.curves.values() if c.ccy == ccy and c.kind == kind), None)

    def fx_curve_ids(self, ccy: str, options: FxOptions) -> tuple[str, tuple[str, ...]]:
        """(curve ID, fallback IDs) of an FX forward's ``ccy`` leg: the chosen kind, else the other one."""
        order = [options.foreign_curve, *(k for k in FX_CURVE_KINDS if k != options.foreign_curve)]
        ids = [implied_curve_id(ccy) if k == "fx_implied" else XCCY_CURVE_ID for k in order
               if k == "fx_implied" or ccy == "CNY"]
        return ids[0], tuple(ids[1:])


def _calendar(static: Any, names: tuple[str, ...]) -> tuple[BusinessCalendar, list[str]]:
    """The joint calendar of the hub calendars ``names`` present in ``static`` and the names missing."""
    have, missing = [], []
    for n in names:
        try:
            have.append(ar.calendar(static.snapshot, n)[0])
        except SnapshotError:
            missing.append(n)
    if not have:
        have.append(ar.calendar(static.snapshot, "MOEX_FX")[0])
    cal = have[0] if len(have) == 1 else joint_calendar(have, version="+".join(c.version for c in have))
    return cal, missing


def _spot_rate(market: Any, ccy: str, rates_in: dict[str, tuple[float, str] | None],
               options: FxOptions) -> tuple[float, str] | None:
    if options.spot_source == "cets_tom":
        tom = market.fx(ccy).get("cets_close")
        if tom:
            return tom, "CETS TOM close"
    return rates_in.get(ccy)


def _swap_quotes(market: Any, ccy: str, cal: BusinessCalendar, bd: date) -> tuple[list, dict[str, str], date | None]:
    """FxSwapPointsQuote of the currency's CETS swaps (one per far date), the dropped ones with the reason and the
    last far date."""
    quotes, dropped, far_dates = [], {}, {}
    for r in market.fx_swap_quotes(ccy):
        near_code, far_code = r["near_leg"].upper(), r["far_leg"].upper()
        label = f"{r['secid']}:{near_code}/{far_code}"
        try:
            near = date.fromisoformat(r["near_settle_date"]) if r.get("near_settle_date") else \
                fx_value_date(bd, near_code, cal)
            if r.get("far_settle_date"):
                far = date.fromisoformat(r["far_settle_date"])
            elif far_code in SWAP_LAG:
                far = fx_value_date(bd, far_code, cal)
            else:
                far = fx_value_date(bd, far_code, cal, spot_lag_days=SWAP_LAG.get(near_code, 1))
        except (CoreRejection, ValueError) as e:
            dropped[label] = f"value dates: {e}"
            continue
        if far <= near:
            dropped[label] = f"far {far} not after near {near}"
            continue
        if far in far_dates:  # one quote per far date: a TOM-near swap (chained from the spot) first
            dropped[label] = f"far date {far} already quoted by {far_dates[far]}"
            continue
        far_dates[far] = label
        quotes.append(FxSwapPointsQuote(label, near, far, r["points"]))
    return quotes, dropped, max(far_dates, default=None)


def _implied(market: Any, ccy: str, spot: FxSpot, cal: BusinessCalendar, rub: DiscountCurve, bd: date,
             options: FxOptions) -> tuple[FxImpliedCurveRequest, DiscountCurve, dict[str, Any]]:
    quotes, dropped, last = _swap_quotes(market, ccy, cal, bd)
    futures = []
    if options.implied_futures:
        for f in market.fx_futures(ccy):
            d = date.fromisoformat(f["last_del_date"])
            if d <= spot.value_date or (last is not None and d <= last):
                dropped[f["secid"]] = "future before the last swap point" if last and d <= last else \
                    "future on or before the spot date"
                continue
            futures.append(FxOutrightQuote(f["secid"], d, float(f["settle_rub_per_unit"])))
    if not quotes and not futures:
        raise rates.RatesInputError(f"{bd}: no {ccy} swap point or future in the snapshot")
    anchor = None
    if spot.value_date > bd and not any(q.near_date == bd for q in quotes):
        # No TOD/TOM swap (USD, EUR: futures only): the foreign rate from the business date to the spot date is
        # taken as 0, so the TOD/TOM points are S x (1 - DF_rub(spot)) (provisional, DECISIONS: FX-implied anchor).
        pts = spot.rub_per_unit * (1.0 - rub.discount_factor(spot.value_date))
        quotes.insert(0, FxSwapPointsQuote("anchor:TOD/TOM", bd, spot.value_date, pts))
        anchor = "assumed: no TOD/TOM swap, foreign rate 0 from the business date to the spot date"
    request = FxImpliedCurveRequest(ccy, bd, rates.RUONIA_OIS_CURVE_ID, tuple(quotes + futures))
    curve = request.build(rub, spot.rub_per_unit, spot.value_date)
    return request, curve, {
        "status": "ok", "spec_id": fx_implied_spec_id(ccy), "swaps": [q.label for q in quotes],
        "futures": [q.label for q in futures], "dropped": dropped, "last_node": curve.nodes[-1][0].isoformat(),
        "rub_curve": rates.RUONIA_OIS_CURVE_ID, **({"anchor": anchor} if anchor else {})}


def _xccy(market: Any, static: Any, spot: FxSpot, rub: DiscountCurve, bd: date
          ) -> tuple[DiscountCurve, XccyCurveBootstrapRequest, dict[str, Any]]:
    qs, lineage = rates.select_quote_set(market.snapshot, "cny_ruonia_xccy")
    parts = [ar.calendar(static.snapshot, n) for n in ("MOEX_FORTS", "RU_PRODUCTION", "CN_INTERBANK")]
    parts.append(rates.ruonia_calendar(static.snapshot, bd))
    cal = joint_calendar([c for c, _ in parts], version="+".join(c.version for c, _ in parts))
    quotes, dropped = [], {}
    for q in qs.quotes:
        if q.mid is None:
            dropped[q.tenor] = "no mid"
            continue
        try:
            quotes.append(XccyParQuote(q.tenor, q.mid))
        except (CoreRejection, ValueError) as e:
            dropped[q.tenor] = str(e)
    if not quotes:
        raise rates.RatesInputError(f"{bd}: no usable cny_ruonia_xccy quote in set {qs.quote_set_id}: {dropped}")
    spot_date = cal.add_business_days(bd, 1)
    request = XccyCurveBootstrapRequest(bd, "CNY", spot.rub_per_unit, spot_date, spot.rub_per_unit, tuple(quotes),
                                        xcs_cnyrub_convention(cal))
    curve = bootstrap_xccy_curve(request, rub_discount_curve=rub)
    lineage.update(status="ok", spec_id=XCCY_SPEC, tenors_used=[q.tenor for q in quotes], tenors_dropped=dropped,
                   notional_fx_rate="spot (provisional)", last_node=curve.nodes[-1][0].isoformat(),
                   calendar=cal.version)
    return curve, request, lineage


def _tenor(q: dict[str, Any]) -> str | None:
    t = str(q.get("tenor") or "").strip().upper()
    if t[:-1].isdigit() and t[-1:] in ("D", "W", "M", "Y"):
        return t
    if q.get("tenor_months") is not None:
        return f"{int(q['tenor_months'])}M"
    if q.get("tenor_days") is not None:
        return f"{int(q['tenor_days'])}D"
    return None


def _proxy(market: Any, dataset: str, spec: str, bd: date
           ) -> tuple[DiscountCurve, ParYieldCurveRequest, dict[str, Any]] | None:
    from decimal import Decimal

    sets = [s for s in market.curve_quote_sets if s.get("dataset") == dataset and s.get("quotes")]
    if not sets:
        return None
    s = max(sets, key=lambda s: (s.get("snapshot_date") or "", s.get("fetched_at") or ""))
    quotes, dropped = {}, {}
    for q in s["quotes"]:
        t, m = _tenor(q), q.get("mid_pct")
        if m is None and q.get("bid_pct") is not None and q.get("ask_pct") is not None:
            m = (Decimal(str(q["bid_pct"])) + Decimal(str(q["ask_pct"]))) / 2
        if t is None or m is None:
            dropped[str(q.get("tenor"))] = "no tenor or mid"
            continue
        quotes[t] = ParYieldQuote(t, float(Decimal(str(m)) / 100))
    request = ParYieldCurveRequest(spec, bd, tuple(quotes.values()))
    curve = bootstrap_par_yield_curve(request)
    return curve, request, {"status": "ok", "spec_id": spec, "dataset": dataset, "source": s.get("source"),
                   "quote_date": s.get("snapshot_date"), "dated": bd.isoformat(), "tenors_used": list(quotes),
                   "tenors_dropped": dropped, "proxy": True,
                   **({"stale": f"quotes of {s.get('snapshot_date')} dated {bd}"}
                      if s.get("snapshot_date") and s["snapshot_date"] != bd.isoformat() else {})}


def with_fx(market: Any, static: Any, core: MarketSnapshot, fx_rates: dict[str, tuple[float, str] | None],
            options: FxOptions | None = None) -> tuple[MarketSnapshot, FxMarket]:
    """``core`` (``with_rates``' snapshot) plus the FX inputs of the module docstring. ``fx_rates``: ccy -> the market
    rate and its source (``fallback.market_fx``), None without one."""
    options = options or FxOptions()
    bd = market.business_date
    out = FxMarket()
    lin: dict[str, Any] = {"options": {"foreign_curve": options.foreign_curve, "spot_source": options.spot_source,
                                       "implied_futures": options.implied_futures}, "spots": {}, "curves": {}}
    rub = next((c.curve for c in core.curves if c.curve_id == rates.RUONIA_OIS_CURVE_ID), None)
    rub_curve = rub if isinstance(rub, DiscountCurve) else None
    curves, calendars, spots, bases = list(core.curves), list(core.calendars), [], []
    fx = [FxRate(c, r[0]) for c, r in sorted(fx_rates.items()) if r and c != "RUB"]
    for ccy in sorted(c for c, r in fx_rates.items() if r and c != "RUB"):
        rate = _spot_rate(market, ccy, fx_rates, options)
        if rate is None:
            continue
        settle, names = market.fx_settlement(ccy)
        try:
            cal, missing = _calendar(static, names)
            vd = settle or cal.add_business_days(bd, 1)
            spot = FxSpot(ccy, rate[0], vd, fx_calendar_id(ccy))
        except (CoreRejection, ValueError, SnapshotError) as e:
            lin["spots"][ccy] = {"status": "error", "error": str(e)}
            continue
        calendars.append(CalendarEntry(fx_calendar_id(ccy), cal))
        spots.append(spot)
        out.spots[ccy], out.rates[ccy] = spot, rate
        lin["spots"][ccy] = (f"{rate[0]:g} {rate[1]}, value {vd.isoformat()} "
                             f"({'hub settle_date' if settle else 'TOM'}) on {'+'.join(names)}"
                             + (f", calendars missing {missing}" if missing else ""))
        cid = implied_curve_id(ccy)
        if rub_curve is None:
            lin["curves"][cid] = {"status": "no_rub_curve"}
        else:
            try:
                request, built, info = _implied(market, ccy, spot, cal, rub_curve, bd, options)
                curves.append(CurveEntry(cid, fx_implied_spec_id(ccy), request))
                out.curves[cid] = ForeignCurve(cid, ccy, "fx_implied", fx_implied_spec_id(ccy),
                                               f"{ccy}: из своп-пойнтов (FX implied)", built)
                lin["curves"][cid] = info
            except rates.RatesInputError as e:
                if ccy in MAIN_CCYS:  # the other currencies without quotes are not worth a line
                    lin["curves"][cid] = {"status": "missing", "error": str(e)}
            except (CoreRejection, ValueError, SnapshotError) as e:
                lin["curves"][cid] = {"status": "error", "error": str(e)}
    if "CNY" in out.spots and rub_curve is not None:
        try:
            built, request, info = _xccy(market, static, out.spots["CNY"], rub_curve, bd)
            curves.append(CurveEntry(XCCY_CURVE_ID, XCCY_SPEC, built))
            spot = out.spots["CNY"]
            if (request.spot_rate, request.spot_value_date) == (spot.rub_per_unit, spot.value_date):
                out.quote_entries[XCCY_CURVE_ID] = CurveEntry(XCCY_CURVE_ID, XCCY_SPEC, request)
            else:  # core takes XCCY quotes for an FX product only on the snapshot spot (its value date)
                info["quote_curve"] = (f"no: the quotes' spot date {request.spot_value_date} is not the FX spot's "
                                       f"{spot.value_date}")
            bases.append(FxBasis("CNY", XCCY_CURVE_ID, 0.0))  # an FX forward on XCCY: zero basis (provisional)
            out.curves[XCCY_CURVE_ID] = ForeignCurve(XCCY_CURVE_ID, "CNY", "xccy", XCCY_SPEC,
                                                     "CNY: XCCY к RUONIA (MOEX XCS CNYRUB)", built)
            lin["curves"][XCCY_CURVE_ID] = info
        except rates.QuoteSetMissing as e:
            lin["curves"][XCCY_CURVE_ID] = {"status": "missing", "error": str(e)}
        except (rates.RatesInputError, CoreRejection, ValueError, SnapshotError) as e:
            lin["curves"][XCCY_CURVE_ID] = {"status": "error", "error": str(e)}
    for dataset, (ccy, cid, spec) in PROXY_SPECS.items():
        try:
            got = _proxy(market, dataset, spec, bd)
        except (CoreRejection, ValueError) as e:
            lin["curves"][cid] = {"status": "error", "error": str(e)}
            continue
        if got is None:
            continue
        curves.append(CurveEntry(cid, spec, got[0]))
        out.quote_entries[cid] = CurveEntry(cid, spec, got[1])
        out.curves[cid] = ForeignCurve(cid, ccy, "proxy", spec, f"{ccy}: {proxies.label(dataset)}", got[0])
        lin["curves"][cid] = got[2]
    fixings = {}
    for f in market.cbr_fixings():
        fixings[(f["ccy"], f["fixing_date"])] = FxFixing(f["ccy"], CBR_SOURCE, f["fixing_date"], f["rate"])
    lin["fixings"] = sorted(f"{c} {d.isoformat()}" for c, d in fixings)
    lin["codes"] = sorted(f"fx_curve_{v['status']}:{k}" for k, v in lin["curves"].items() if v.get("status") != "ok")
    out.lineage = lin
    snap = replace(core, curves=tuple(curves), calendars=tuple(calendars), fx=tuple(fx), fx_spots=tuple(spots),
                   fx_bases=tuple(bases), fx_fixings=tuple(fixings[k] for k in sorted(fixings)))
    return snap, out


def options_from(settings: Any) -> FxOptions:
    """The run's ``FxOptions`` from the backend settings (``desk.settings.Settings``; defaults for what it lacks)."""
    d = FxOptions()
    return FxOptions(
        foreign_curve=getattr(settings, "fx_foreign_curve", d.foreign_curve),
        implied_futures=bool(getattr(settings, "fx_implied_futures", d.implied_futures)),
        spot_source=getattr(settings, "fx_spot_source", d.spot_source),
        unlinked_terms=bool(getattr(settings, "fx_unlinked_terms", d.unlinked_terms)),
        fixing_sources=parse_fixing_sources(getattr(settings, "ndf_fixing_sources", None)) or d.fixing_sources,
        futures_by_ticker=bool(getattr(settings, "futures_by_ticker", d.futures_by_ticker)))
