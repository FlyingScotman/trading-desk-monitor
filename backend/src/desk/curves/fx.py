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
- **CNY RUSFAR** (``CNY RUSFAR``, ``cny_discount_factors_v1``; TDM#565): the hub's ``cny_rusfar_ois`` quotes (OIS on
  RUSFAR CNY, the Moscow Exchange's overnight CNY repo rate; fractions) bootstrapped here into a ``DiscountCurve``
  (``ois-logdf-v1``, ACT/365F) that projects the floating CNY leg of a cross-currency swap on RUSFARCNY
  (``valuation.derivatives``); it never discounts and is never an FX forward's curve. Conventions, **provisional**
  (the exchange's OIS RUSFARCNY terms are not at hand): the swap starts TOM on ``FX:CNY`` (the curve date to the start
  at the latest RUSFARCNY fixing, else the shortest tenor's rate), ends the tenor later (modified following), pays
  once up to 1Y and annually beyond
  (rolled back from the end, adjusted, a short first stub), the fixed leg ACT/365F; the floating leg is single-curve
  (its PV is ``DF(start) - DF(end)``). The nodes are the start and the swap ends; past the last quote the curve is
  extrapolated flat-forward (said in the lineage).
- **Overnight index fixings** (``FxMarket.index_fixings``, core ``IndexFixingEntry``; ``FOREIGN_OVERNIGHT``: ``RUSFARCNY``,
  ``SOFR``, ``ESTR``; ``RUB_OVERNIGHT``: the RUB ``RUSFAR`` of a RUB leg, TDM#565 stage 2, on ``MOEX_RUSFAR`` else
  ``MOEX_STOCK``, no curve of its own): the hub's fixings of the index (percent),
  those published by the snapshot's ``known_at`` and dated before the business date, as core ``IndexFixings`` (fractions,
  dated the business date). Observation calendar: the dates published before the business date (the exchange does not
  publish on weekends, China's holidays and the last trading day of the year: the previous value then holds, the
  ``bridged_days`` of the lineage), the index's calendar (``ForeignOvernight.base_calendar``: ``US_FED`` / ``TARGET`` from the static snapshot, else ``FX:<ccy>``;
  ``FX:CNY`` for RUSFARCNY) from the business date on, before the first and after the last fixing too
  (a business day with no fixing there is missing, which core refuses, never filled).
- **Forecast curves USD SOFR / EUR ESTR** (``USD SOFR``, ``EUR ESTR``; ``usd_discount_factors_v1``,
  ``eur_discount_factors_v1``; TDM#565): the nodes of the proxies below as a ``DiscountCurve`` (``ois-logdf-v1``, ACT/365F),
  so their discount factors equal the proxies'. Only the forecast of a floating USD / EUR leg of a cross-currency swap
  (``valuation.derivatives``): never discounting, never an FX forward's curve, a separate ID from the proxy (a proxy is a
  ``ParYieldCurveRequest`` in the quote-risk slice, which core's ``discount_curve`` does not give a swap). **Provisional:**
  government yields stand for the OIS curve (the spread is some tens of bp); the lineage says so (``proxy``,
  ``provisional``, ``stale``). Without the proxy set: no curve (``missing``). SOFR and ESTR fixings: one date from several
  sources takes the first of ``ForeignOvernight.sources`` known at the cut (``adapter_rates.fixings``).
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

import calendar as _pycal
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any

from rub_fixed_income import (
    BusinessCalendar,
    BusinessDayConvention,
    CalendarEntry,
    CalendarRangeError,
    CoreRejection,
    CurveConvention,
    CurveEntry,
    DayCount,
    DiscountCurve,
    FxBasis,
    FxFixing,
    FxImpliedCurveRequest,
    FxOutrightQuote,
    FxRate,
    FxSpot,
    FxSwapPointsQuote,
    IndexFixingEntry,
    IndexFixings,
    MarketSnapshot,
    ParYieldCurveRequest,
    ParYieldQuote,
    XccyCurveBootstrapRequest,
    XccyParQuote,
    adjust_date,
    bootstrap_par_yield_curve,
    bootstrap_xccy_curve,
    fx_implied_spec_id,
    fx_value_date,
    joint_calendar,
    resolve_tenor,
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
RUSFAR_CNY_CURVE_ID, RUSFAR_CNY_SPEC = "CNY RUSFAR", "cny_discount_factors_v1"
RUSFAR_CNY_DATASET = "cny_rusfar_ois"
RUSFAR_CNY_FIXING = "RUSFARCNY"
RUSFAR_CNY_INDEX = "CNY-RUSFARCNY-COMPOUND"
RUSFAR_RUB_FIXING = "RUSFAR"
RUSFAR_RUB_INDEX = "RUB-RUSFAR-OIS-COMPOUND"
"""The RUB RUSFAR (TDM#565, stage 2): the hub's fixing code and core's compounded index of a RUB leg of a cross-currency
swap on it (``valuation.derivatives.CCS_RUB_INDEX``)."""
"""The name of core's compounded index of a RUSFARCNY leg and of its fixings (``valuation.derivatives.CCS_FOREIGN_INDEX``)."""
RUSFAR_CNY_MAX_UNPUBLISHED_DAYS = 1
"""As RUONIA's (``rates.RUONIA_MAX_UNPUBLISHED_DAYS``): at most one business day just before the business date may lack its
RUSFARCNY fixing (the hub late, the first business day of the year after the exchange's last day without the index): the
previous value is carried and said. More are missing fixings, never filled (``valuation.derivatives``: ``no_fixings``)."""
RUSFAR_CNY_PRELIMINARY = "moex_marketdata_preliminary"
"""The hub's label of a fixing that is a preliminary value of the same day from the exchange's market data, replaced later by
the history's (hub ``HISTORY_API.md``); said in the lineage."""


@dataclass(frozen=True)
class ForeignOvernight:
    """A foreign overnight index a cross-currency swap's floating leg can float on (TDM#565): its fixings and, apart
    from the exchange's own, the projection curve ``curve_id`` (``proxy_of``: the government par-yield proxy it is
    renamed from)."""

    code: str
    """The hub's fixing code and the terms' ``index_code``."""
    ccy: str
    index_name: str
    """Core's compounded index name of the fixings."""
    curve_id: str
    spec_id: str
    base_calendar: str | None
    """The hub calendar of the index's fixing days (``US_FED``, ``TARGET``); None, or absent in the static snapshot:
    ``FX:<ccy>``."""
    sources: tuple[str, ...] | None
    """The hub sources in order of preference when one date comes from several (``adapter_rates.fixings``); None: one
    source only (a duplicate is an error)."""
    max_unpublished: int
    preliminary_label: str | None = None
    proxy_of: str | None = None
    """The proxy quote set (``PROXY_SPECS``) the projection curve is built from; None: its own quotes."""
    label: str = ""
    """The curve's run reference name."""


FOREIGN_OVERNIGHT = {
    RUSFAR_CNY_FIXING: ForeignOvernight(
        RUSFAR_CNY_FIXING, "CNY", RUSFAR_CNY_INDEX, RUSFAR_CNY_CURVE_ID, RUSFAR_CNY_SPEC, None, None,
        RUSFAR_CNY_MAX_UNPUBLISHED_DAYS, RUSFAR_CNY_PRELIMINARY, label="CNY: OIS на RUSFAR CNY"),
    "SOFR": ForeignOvernight(
        "SOFR", "USD", "USD-SOFR-COMPOUND", "USD SOFR", "usd_discount_factors_v1", "US_FED", ("nyfed", "fred"), 1,
        proxy_of="usd_ust_par", label="USD: прогноз SOFR по UST par (прокси, не OIS)"),
    "ESTR": ForeignOvernight(
        "ESTR", "EUR", "EUR-ESTR-COMPOUND", "EUR ESTR", "eur_discount_factors_v1", "TARGET",
        ("ecb_data_portal", "bundesbank", "dbnomics", "fred"), 1,
        proxy_of="eur_aaa_par", label="EUR: прогноз €STR по ECB AAA par (прокси, не OIS)")}
"""Index code -> the foreign overnight indices valued (``valuation.derivatives.CCS_FOREIGN_INDEX``). ``USD SOFR`` /
``EUR ESTR`` are forecast curves only (the proxies of the government par yields renamed, never discounting)."""
RUB_OVERNIGHT = {
    RUSFAR_RUB_FIXING: ForeignOvernight(
        RUSFAR_RUB_FIXING, "RUB", RUSFAR_RUB_INDEX, "", "", "MOEX_RUSFAR", None, 1, label="RUB: RUSFAR")}
"""Index code -> the RUB overnight indices read as core ``IndexFixings`` (``valuation.derivatives.CCS_RUB_INDEX``):
fixings only, no curve (the leg projects on RUONIA). The fixing days' calendar is ``MOEX_RUSFAR`` where the static
snapshot has it (hub contract 1.6), else ``MOEX_STOCK``, then as RUSFARCNY's: the days the exchange published."""
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
    off_report: bool = False
    """``off_report_derivatives == "include"`` (TDM#468): value the trade_terms derivatives no positions row names as
    positions of their own."""
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
    """``fx_implied`` | ``xccy`` | ``proxy`` | ``ois`` (``CNY RUSFAR``, ``USD SOFR``, ``EUR ESTR``: a
    projection curve only)."""
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
    index_fixings: tuple[IndexFixingEntry, ...] = ()
    """The overnight indices' fixings read as ``IndexFixings`` (``FOREIGN_OVERNIGHT``: ``RUSFARCNY``, ``SOFR``, ``ESTR``;
    ``RUB_OVERNIGHT``: the RUB ``RUSFAR``), kept out of
    ``core_market`` so the bonds' inputs stay as they were: ``valuation.derivatives`` adds them to the call that reads
    them."""
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


def _add_months(d: date, months: int) -> date:
    i = d.month - 1 + months
    y, m = d.year + i // 12, i % 12 + 1
    return date(y, m, min(d.day, _pycal.monthrange(y, m)[1]))


RUSFAR_CNY_CONVENTION = (
    "provisional: the swap starts TOM on FX:CNY, ends the tenor later (modified following), pays once up to 1Y and "
    "annually beyond (rolled back from the end, adjusted, short first stub), fixed leg ACT/365F, floating leg "
    "single-curve (DF(start) - DF(end)); DF(start) at the latest RUSFARCNY fixing (else the shortest tenor's rate) "
    "from the curve date; beyond the last "
    "quote flat-forward")
"""The ``CNY RUSFAR`` bootstrap rule, in its lineage."""


def _rusfar_cny(market: Any, cal: BusinessCalendar, bd: date, front: tuple[date, float] | None = None
                ) -> tuple[DiscountCurve, dict[str, Any]]:
    """``CNY RUSFAR``: the hub's ``cny_rusfar_ois`` quotes bootstrapped, tenor by tenor, into discount factors on the
    swap's start and ends (``RUSFAR_CNY_CONVENTION``, provisional). ``front``: the latest RUSFARCNY fixing (date, rate),
    the rate from the curve date to the start; absent, the shortest tenor's. A tenor whose dates fall outside the calendar, or
    that does not start TOM, or ends before the previous one, is dropped with the reason."""
    qs, lineage = rates.select_quote_set(market.snapshot, RUSFAR_CNY_DATASET)
    tau = DayCount.ACT_365_FIXED.year_fraction
    conv = CurveConvention(time_day_count=DayCount.ACT_365_FIXED)
    start = cal.add_business_days(bd, 1)
    swaps, dropped = [], {}
    for q in qs.quotes:
        if q.mid is None:
            dropped[q.tenor] = "no mid"
            continue
        try:
            td = resolve_tenor(bd, q.tenor, cal)
        except (CoreRejection, ValueError, CalendarRangeError) as e:
            dropped[q.tenor] = str(e)
            continue
        if td.effective_date != start:
            dropped[q.tenor] = f"starts {td.effective_date}, not TOM {start}"
            continue
        swaps.append((td, q.mid, q.tenor))
    if not swaps:
        raise rates.RatesInputError(f"{bd}: no usable {RUSFAR_CNY_DATASET} quote in set {qs.quote_set_id}: {dropped}")
    swaps.sort(key=lambda x: x[0].end_date)
    front_rate = front[1] if front else swaps[0][1]
    df_start = 1.0 / (1.0 + front_rate * tau(bd, start)) if start > bd else 1.0
    nodes: list[tuple[date, float]] = [(start, df_start)] if start > bd else []
    used = []
    for td, rate, tenor in swaps:
        end = td.end_date
        if end <= (nodes[-1][0] if nodes else bd):
            dropped[tenor] = f"ends {end}, not after the previous tenor"
            continue
        try:
            pays = [end]
            if td.unadjusted_end > _add_months(start, 12):
                rolls, k = [], 1
                while (roll := _add_months(td.unadjusted_end, -12 * k)) > start:
                    rolls.append(adjust_date(roll, BusinessDayConvention.MODIFIED_FOLLOWING, cal))
                    k += 1
                pays = sorted({a for a in rolls if start < a < end}) + [end]
        except CalendarRangeError as e:
            dropped[tenor] = f"calendar range: {e}"
            continue
        taus = [tau(a, b) for a, b in zip([start, *pays[:-1]], pays, strict=True)]

        def gap(x: float, pays=pays, taus=taus, rate=rate, end=end) -> float:
            c = DiscountCurve(bd, (*nodes, (end, x)), "ois-logdf-v1", convention=conv)
            return sum(rate * t * c.discount_factor(p) for t, p in zip(taus, pays, strict=True)) + x - df_start

        if len(pays) == 1:
            x = df_start / (1.0 + rate * taus[0])
        else:
            lo, hi = 1e-6, 2.0
            if not gap(lo) < 0.0 < gap(hi):
                dropped[tenor] = "no discount factor reprices the quote"
                continue
            for _ in range(100):
                mid = (lo + hi) / 2.0
                lo, hi = (mid, hi) if gap(mid) < 0.0 else (lo, mid)
            x = (lo + hi) / 2.0
        nodes.append((end, x))
        used.append(tenor)
    curve = DiscountCurve(bd, tuple(nodes), "ois-logdf-v1", convention=conv)
    lineage.update(status="ok", spec_id=RUSFAR_CNY_SPEC, tenors_used=used, tenors_dropped=dropped,
                   last_node=nodes[-1][0].isoformat(), calendar=cal.version,
                   front_rate=f"{RUSFAR_CNY_FIXING} {front[0].isoformat()}" if front else "shortest tenor",
                   convention=RUSFAR_CNY_CONVENTION,
                   extrapolation=f"flat-forward beyond {nodes[-1][0].isoformat()} (provisional)")
    return curve, lineage


def _observation_calendar(cal: BusinessCalendar, published: set[date], bd: date | None = None,
                          max_unpublished: int = RUSFAR_CNY_MAX_UNPUBLISHED_DAYS, code: str = RUSFAR_CNY_FIXING
                          ) -> tuple[BusinessCalendar, list[date], list[date]]:
    """``cal`` (the index's settlement calendar) with the days between the first and the last of ``published`` made
    business days exactly where the index was published (a day with no fixing there is bridged by the previous value:
    the exchange does not publish on weekends, China's holidays or the last trading day of the year). With ``bd``, up to
    ``max_unpublished`` business days of ``cal`` between the last fixing and ``bd`` are carried too
    (unpublished); more are left as business days, so a fixing is required on them. Returns the calendar, the
    bridged days (business days of ``cal`` with no fixing between the first and the last fixing) and the unpublished
    ones."""
    holidays, working = set(cal.holidays), set(cal.working_days)
    bridged, unpublished, d, last = [], [], min(published), max(published)

    def drop(day: date) -> None:
        working.discard(day)
        if day.weekday() < 5:
            holidays.add(day)

    while d <= last:
        if d in published:
            holidays.discard(d)
            if d.weekday() >= 5:
                working.add(d)
        else:
            if cal.is_business_day(d):
                bridged.append(d)
            drop(d)
        d += timedelta(days=1)
    if bd is not None:
        tail = []
        d = last + timedelta(days=1)
        while d < bd:
            if cal.covers(d) and cal.is_business_day(d):
                tail.append(d)
            d += timedelta(days=1)
        if len(tail) <= max_unpublished:
            unpublished = tail
            for day in tail:
                drop(day)
    return BusinessCalendar(frozenset(holidays), frozenset(working), f"{cal.version}+{code}",
                            cal.valid_from, cal.valid_to), bridged, unpublished


def index_fixings(snapshot: Any, spec: ForeignOvernight, cal: BusinessCalendar, bd: date,
                  known_at: datetime | None = None) -> tuple[IndexFixingEntry, dict[str, Any]]:
    """The ``spec`` index's fixings of the market snapshot as core's ``IndexFixingEntry`` (module docstring): rows dated
    before ``bd`` and in the calendar ``cal`` published by ``known_at`` (default the snapshot's;
    ``rates.fixing_known``), in fractions, dated ``bd``. One date from several sources: the first of ``spec.sources``
    that is known by the cut (deterministic; ``adapter_rates.fixings``). The lineage: the rows, the first and the latest
    fixing, the rows not yet published at the cut, the ``bridged_days`` (the previous value holds), the
    ``unpublished_business_days`` (at most ``spec.max_unpublished`` just before ``bd``, carried), the ``preliminary_days``
    (rows the hub labels ``spec.preliminary_label``) and the sources read. Raises ``RatesInputError`` without a usable
    row."""
    cut = known_at or ar.known_at(snapshot)
    every = [f for f in ar.fixings(snapshot, spec.code, spec.sources) if f.fixing_date < bd]
    rows = [f for f in ar.fixings(snapshot, spec.code, spec.sources, lambda f: rates.fixing_known(f, cut))
            if f.fixing_date < bd]
    kept_days = {f.fixing_date for f in rows}
    unknown = [f.fixing_date.isoformat() for f in every if f.fixing_date not in kept_days]
    early = [f for f in rows if cal.valid_from is not None and f.fixing_date < cal.valid_from]
    rows = rows[len(early):]
    if not rows:
        raise rates.RatesInputError(f"{bd}: no {spec.code} fixing known at {cut.isoformat()}")
    obs, bridged, unpublished = _observation_calendar(cal, {f.fixing_date for f in rows}, bd, spec.max_unpublished,
                                                      spec.code)
    entry = IndexFixingEntry(spec.code, IndexFixings(
        spec.index_name, bd, tuple((f.fixing_date, f.rate) for f in rows), obs, snapshot.id))
    labels = {(str(r.get("fixing_date")), r.get("source")): r.get("labels") or ()
              for r in snapshot.data.get("fixings") or () if r.get("index_code") == spec.code}
    prelim = sorted(f.fixing_date.isoformat() for f in rows
                    if spec.preliminary_label and spec.preliminary_label in labels.get((f.fixing_date.isoformat(), f.source), ()))
    return entry, {"status": "ok", "rows": len(rows), "first": rows[0].fixing_date.isoformat(),
                   "latest": rows[-1].fixing_date.isoformat(), "known_at": cut.isoformat(),
                   "sources": sorted({f.source for f in rows}),
                   "unpublished_at_cut": unknown, "dropped_before_calendar": len(early),
                   "bridged_days": [d.isoformat() for d in bridged],
                   "unpublished_business_days": [d.isoformat() for d in unpublished],
                   "max_unpublished_business_days": spec.max_unpublished,
                   "preliminary_days": prelim, "calendar": obs.version}


def rusfar_cny_fixings(snapshot: Any, cal: BusinessCalendar, bd: date, known_at: datetime | None = None
                       ) -> tuple[IndexFixingEntry, dict[str, Any]]:
    """The ``RUSFARCNY`` fixings (``index_fixings``) on the calendar ``cal`` (``FX:CNY``)."""
    return index_fixings(snapshot, FOREIGN_OVERNIGHT[RUSFAR_CNY_FIXING], cal, bd, known_at)


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


def _index_calendar(static: Any, ov: ForeignOvernight, spot_cals: dict[str, BusinessCalendar]
                    ) -> BusinessCalendar | None:
    """The fixing days' calendar of ``ov``: its hub calendar from the static snapshot, else ``FX:<ccy>``; None without
    either (the currency has no spot)."""
    for name in ((ov.base_calendar, "MOEX_STOCK") if ov.ccy == "RUB" else (ov.base_calendar,)):
        if name is None:
            continue
        try:
            return ar.calendar(static.snapshot, name)[0]
        except SnapshotError:
            pass
    return spot_cals.get(ov.ccy)


def _proxy_projection(proxy: DiscountCurve, proxy_lineage: dict[str, Any], ov: ForeignOvernight, bd: date
                      ) -> tuple[DiscountCurve, dict[str, Any]]:
    """``USD SOFR`` / ``EUR ESTR``: the nodes of the government par-yield proxy (``USD UST`` / ``EUR AAA``) as a
    ``DiscountCurve`` (``ois-logdf-v1``, ACT/365F) under the spec core reads a currency's discount factors by. The
    interpolation is the proxy's own, so the two curves have the same discount factors; the lineage keeps the proxy's
    quote date and ``stale`` and says what stands for what (provisional)."""
    built = DiscountCurve(bd, proxy.nodes, "ois-logdf-v1", convention=CurveConvention(time_day_count=DayCount.ACT_365_FIXED))
    last = proxy.nodes[-1][0].isoformat()
    info = {"status": "ok", "spec_id": ov.spec_id, "source_curve": PROXY_SPECS[ov.proxy_of or ""][1],
            "dataset": proxy_lineage.get("dataset"), "source": proxy_lineage.get("source"),
            "quote_date": proxy_lineage.get("quote_date"), "dated": bd.isoformat(),
            "tenors_used": proxy_lineage.get("tenors_used"), "last_node": last,
            "extrapolation": f"flat-forward beyond {last} (provisional)", "proxy": True,
            "provisional": "UST/AAA par yields stand for the OIS curve", **({"stale": proxy_lineage["stale"]}
                                                                          if proxy_lineage.get("stale") else {})}
    return built, info


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
    spot_cals: dict[str, BusinessCalendar] = {}
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
        spot_cals[ccy] = cal
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
    entries: list[IndexFixingEntry] = []
    lin["index_fixings"] = {}
    for code, ov in (*FOREIGN_OVERNIGHT.items(), *RUB_OVERNIGHT.items()):  # the overnight indices of a floating leg (TDM#565)
        cal = _index_calendar(static, ov, spot_cals)
        if cal is None:
            continue
        try:
            entry, info = index_fixings(market.snapshot, ov, cal, bd)
            entries.append(entry)
            lin["index_fixings"][code] = info
        except (rates.RatesInputError, CoreRejection, ValueError, SnapshotError) as e:
            lin["index_fixings"][code] = {"status": "missing", "error": str(e)}
    out.index_fixings = tuple(entries)
    if "CNY" in out.spots:  # RUSFAR CNY: the projection curve of a floating CNY leg
        cal = spot_cals["CNY"]
        front = next((e.fixings.fixings[-1] for e in entries if e.fixing_id == RUSFAR_CNY_FIXING), None)
        try:
            built, info = _rusfar_cny(market, cal, bd, front)
            curves.append(CurveEntry(RUSFAR_CNY_CURVE_ID, RUSFAR_CNY_SPEC, built))
            out.curves[RUSFAR_CNY_CURVE_ID] = ForeignCurve(RUSFAR_CNY_CURVE_ID, "CNY", "ois", RUSFAR_CNY_SPEC,
                                                           FOREIGN_OVERNIGHT[RUSFAR_CNY_FIXING].label, built)
            lin["curves"][RUSFAR_CNY_CURVE_ID] = info
        except rates.QuoteSetMissing as e:
            lin["curves"][RUSFAR_CNY_CURVE_ID] = {"status": "missing", "error": str(e)}
        except (rates.RatesInputError, CoreRejection, ValueError, SnapshotError) as e:
            lin["curves"][RUSFAR_CNY_CURVE_ID] = {"status": "error", "error": str(e)}
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
    for ov in FOREIGN_OVERNIGHT.values():  # USD SOFR / EUR ESTR: the proxies renamed, a forecast curve only
        if ov.proxy_of is None:
            continue
        proxy_id = PROXY_SPECS[ov.proxy_of][1]
        if proxy_id not in out.curves:
            src = lin["curves"].get(proxy_id) or {}
            lin["curves"][ov.curve_id] = {
                "status": src.get("status", "missing"),
                "error": (f"{proxy_id} not built for {ov.curve_id}: {src['error']}" if src.get("error")
                          else f"no {ov.proxy_of} quotes in the snapshot for {ov.curve_id}")}
            continue
        built, info = _proxy_projection(out.curves[proxy_id].curve, lin["curves"][proxy_id], ov, bd)
        curves.append(CurveEntry(ov.curve_id, ov.spec_id, built))
        out.curves[ov.curve_id] = ForeignCurve(ov.curve_id, ov.ccy, "ois", ov.spec_id, ov.label, built)
        lin["curves"][ov.curve_id] = info
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
        off_report=getattr(settings, "off_report_derivatives", "exclude") == "include",
        fixing_sources=parse_fixing_sources(getattr(settings, "ndf_fixing_sources", None)) or d.fixing_sources,
        futures_by_ticker=bool(getattr(settings, "futures_by_ticker", d.futures_by_ticker)))
