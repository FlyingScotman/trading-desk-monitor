"""Derivatives through core ``value_portfolio`` (result contract v3): FX forwards, NDFs, FX swaps and crosses from the
private contract's ``trade_terms`` (WP-M1, M3, M4), and FORTS FX futures by ticker (P3, setting ``futures_by_ticker``).

``valuation.positions.plan`` links each FX or OTC position row to its trades (``link``): a ``trade_terms`` row whose
``position_id`` is the row's, or whose ``trade_id`` is the row's ``deal_id`` or ``position_id``, in the same book. A row
with trades is valued here, all of them in one core call per run (``value``); a row without (or whose trades core
cannot value) keeps the rule it had before: the report's ``mv_rub``, else 0, with the amount as its FX delta, and the
reason in its assumptions (``not_exact_reason``).

**Mapping** (``core_legs``; one core instrument per exchange, a swap's near and far legs apart, which values and risks
as core's ``FxSwap``):

- X/RUB (or RUB/X, read the other way round): ``FxForward(currency X, quote units 1, FxForwardLeg(value_date, N, K))``,
  N = +``notional_base`` when the desk buys X, - when it sells; K = ``contract_rate`` (RUB per X), else
  ``notional_quote / notional_base``. A leg whose value date is before the business date has settled: left out.
- NDF (``ndf``): cash-settled on ``value_date`` (``payment_date``; the leg's value date is taken as the same day,
  provisional), fixed on ``fixing_date`` by ``fixing_source`` mapped to the hub's (setting ``ndf_fixing_sources``,
  default CBR names -> ``CBR``, the CBR official rate set that day, ``curves.fx``). A published fixing freezes the leg
  (core: RUB risk only); a fixing due today and not yet published is projected (``fixing_pending``); one past without
  a fixing in the snapshot is a core rejection, so the row keeps the report's value. An NDF settled in another currency
  than RUB, or a cross NDF, is not valued (core BACKLOG #135).
- X/Y without RUB: ``FxCrossForward`` (``FxCrossInstrument`` on the joint settlement calendar of X and Y).
- The RUB leg is discounted on RUONIA OIS (``RUB RUONIA``), the foreign leg on the currency's curve
  (``FxMarket.fx_curve_ids``: setting ``fx_foreign_curve``, default the FX-implied curve ``<CCY> FX``; CNY's other
  choice, the XCCY curve, is the fallback when the chosen one is absent, core ``fx_curve_fallbacks``).

**Published** (``tdm_run.direct_row``): PV; ``risk`` per curve (``RUB RUONIA`` and the foreign curve, both currencies);
``fx_delta`` per currency (core's spot equivalent, not the notional); the Hessian over the rate and FX factors;
``extra``: ``legs`` (the shape of the concept pages), ``ocp_amount`` (the nominal per currency for the OCP, beside the
delta), ``basis01`` (``FXBASIS|<ccy>``, RUB per bp), ``fixings`` (fixed / pending / open), ``fx_rates`` (the spots),
``core`` (product, trades, model flags, curves), ``reported_pv_rub`` and ``pv_diff_rub`` (the reconciliation).

**Interest rate swaps and REPO** (private/1.4, TDM#45; ``SwapTerms``, ``RepoTerms``): one core instrument per trade.

- ``irs`` / ``ois``: the two leg rows of a ``trade_id`` (a ``pay`` and a ``receive`` leg, one fixed with
  ``fixed_rate_pct``, one floating on ``index_code``) are one core ``InterestRateSwap``: ``payer`` when the desk pays
  the fixed leg, notional ``notional_ccy`` (RUB only, both legs equal), ``start_date`` the effective date,
  ``maturity_date`` the (unadjusted) termination, ``spread_bp`` the float spread. ``RUONIA``: compounded, the MOEX OIS
  convention (``rub_ruonia_ois_moex_v2``), discounted and projected on ``RUB RUONIA`` (single curve), on the joint
  OIS calendar (``rates.ois_calendar``). ``KEY_RATE``: arithmetic average, MOEX IRS KEYRATE (``rub_key_rate_swap_v1``),
  discounted on ``RUB RUONIA``, projected on the key-rate forward curve (``rates.KEY_RATE_SWAP_CURVE_ID``, published as
  ``RUB KEY``). A leg's ``payment_frequency`` (``<n>M``, ``<n>Y``, ``term``) and ``day_count`` replace the
  convention's for that leg; without them the convention's apply, said in the assumptions (provisional). A swap whose
  period has started reads the run's fixings of its index (``rates.fixing_entries``: point-in-time, RUONIA with one
  declared unpublished trailing business day); ``extra.index_fixings`` says which history it read, its latest fixing,
  the declared unpublished days (carried at the latest fixing) and any business day missing inside it (core rejects
  a swap that reads one). Not valued (the report's rule, with the reason): a ``ccs`` (``ccs_unsupported``: no
  cross-currency or basis swap in the monitor yet), two floating legs (``basis_swap_unsupported``), another index or
  currency, a ``<n>D`` / ``<n>W`` frequency or ``30/360``, a matured swap.
- ``repo``: one core ``RepoTrade`` per row: ``repo`` borrows cash, ``reverse_repo`` lends it, ``notional_ccy`` the first
  leg's cash (RUB only), ``start_date`` / ``maturity_date`` its legs, fixed ``fixed_rate_pct`` or floating
  ``RUONIA`` / ``KEY_RATE`` + ``spread_bp`` (the MOEX observation rule; the key rate projected on
  ``RUB_KEY_RATE_PROJ``), simple interest, amounts rounded to the kopeck, day basis ``day_count`` (absent: ACT/365-366,
  the MOEX rouble basis, provisional), discounted on ``RUB RUONIA``, settled on ``MOEX_STOCK``. An open REPO (no
  ``maturity_date``) is not valued (``repo_open``: its assumed end is an owner's decision), nor a RUSFAR one (no
  RUSFAR fixings in the run). The security is a reference only: the bond is its own position.

**Flows and the last day** (TDM#466): ``extra.flows`` lists what the row's trades pay or settle in the next
``FLOW_DAYS`` days, signed as received (+) or paid (-): a swap's and a REPO's payments are core's projection (the
``received_rub`` of ``horizon_portfolio`` at each payment date, a difference of two horizons); an FX leg's amounts per
currency are its terms' (``out_on``: the first date core no longer counts it). A flow of unknown amount (``amount``
None: an NDF's cash, a date core refused) stops the PnL only when it falls in the window. A coupon is core's projection
on D-1: with a payment lag of a business day or more it rests wholly on the fixings and equals the coupon paid
(core's ``_compounded_known``); a difference is possible only at lag 0 (a day of forward) or when a fixing is revised,
and it is in no day's PnL (the change of PV of D no longer holds the period). ``pnl.direct`` adds those of D-1 falling in (D-1, D] to the change of PV. A row whose trades have all
paid their last flow, the last within ``FLOW_DAYS`` of the date, is valued as settled (``_settled_row``: PV 0, no
risk, ``extra.settled``) instead of ``NotValued("settled")``, so that its last day has a PnL; when only some trades
ended, the others are valued and the ended ones noted.

Published for them: PV, ``risk`` per curve (``RUB RUONIA``, ``RUB KEY``), the Hessian; ``extra``: ``swaps`` / ``repos``
(the terms and core's figures per trade), ``index_fixings``, ``core``, ``reported_pv_rub`` and ``pv_diff_rub``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    CalendarEntry,
    CoreRejection,
    CurveBootstrapRequest,
    DayCount,
    FixedRepoRate,
    FloatingRepoRate,
    FuturesQuote,
    FxCrossForward,
    FxCrossInstrument,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxFuturesContract,
    FxFuturesInstrument,
    InterestRateSwap,
    ParYieldCurveRequest,
    Position,
    RepoDayBasis,
    RepoInstrument,
    RepoTrade,
    ShockedCurveCache,
    SwapInstrument,
    SwapLegSchedule,
    ValuationPolicy,
    XccyCurveBootstrapRequest,
    horizon_portfolio,
    joint_calendar,
    swap_convention,
    swap_schedule,
    value_portfolio,
)
from rub_fixed_income import __version__ as CORE_VERSION

from ..curves import rates
from ..curves.fx import FxOptions
from ..curves.rates import KEY_RATE_CURVE_ID, KEY_RATE_SWAP_CURVE_ID, RUONIA_OIS_CURVE_ID
from .bonds import OIS_TAIL, RISK_CURVES, BondValuation

PUBLISHED_FACTORS = ("IR|", "FX|")
"""Factors of the published Hessian (the Taylor's vocabulary, ``scenario.taylor``); the basis goes to ``basis01``."""
RATE_TOLERANCE = 1e-4
"""Relative difference of ``contract_rate`` and the amounts' rate said in the assumptions."""
PUBLISHED_CURVES = {**RISK_CURVES, KEY_RATE_SWAP_CURVE_ID: RISK_CURVES[KEY_RATE_CURVE_ID]}
"""Core curve ID -> published curve: a key-rate swap's projection is the key rate (``RUB KEY``), as a floater's."""
FX_KINDS = ("fx_forward", "fx_swap")
SWAP_KINDS = ("irs", "ois")
LINKED_KINDS = (*FX_KINDS, *SWAP_KINDS, "ccs", "repo")
"""``product_kind`` of the trade_terms rows ``link`` attaches to a positions row (``bond_purchase`` is funding only)."""
SWAP_INDEX = {"RUONIA": "ruonia", "KEY_RATE": "key_rate"}
"""``index_code`` of a swap's floating leg -> core's swap index."""
SWAP_SPEC = {"ruonia": "rub_ruonia_ois_moex_v2", "key_rate": rates.KEY_RATE_SWAP_SPEC}
"""Core's swap index -> the registered convention a swap takes where its terms state none (provisional)."""
SWAP_CALENDAR = {"ruonia": "OIS:RUONIA", "key_rate": "IRS:KEYRATE"}
"""Core's swap index -> the snapshot calendar its schedule is rolled on (``rates.ois_calendar``, ``krs_calendar``)."""
FIXING_ID = {"ruonia": "RUONIA", "key_rate": "KEY_RATE"}
"""Core's swap index -> the run's fixing history (``rates.fixing_entries``)."""
DAY_COUNTS = {"ACT/365": DayCount.ACT_365_FIXED, "ACT/360": DayCount.ACT_360, "ACT/ACT": DayCount.ACT_ACT_ISDA}
REPO_BASIS = {"ACT/365": RepoDayBasis.ACT_365_FIXED, "ACT/360": RepoDayBasis.ACT_360,
              "ACT/ACT": RepoDayBasis.ACT_365_366, None: RepoDayBasis.ACT_365_366}
"""A REPO's ``day_count`` -> core's day basis: ACT/ACT and an absent one are the MOEX rouble basis (ACT/365-366)."""
REPO_OBSERVATION = {"RUONIA": "previous_settlement_day", "KEY_RATE": "calendar_day"}
REPO_CALENDAR = "MOEX_STOCK"
REPO_ROUNDING = 2
FLOW_DAYS = 16
"""How far ahead of the business date a valuation records the flows its trades pay or settle (``extra.flows``), and how
long ago a trade's last flow may be for the row to be valued as settled (PV 0) rather than keep the report's rule: the
longest gap between two runs (the New Year holidays, 11 days) with a margin. ``pnl.direct`` adds the flows of D-1 that
fall in (D-1, D] to the change of PV."""


class NotValued(Exception):
    def __init__(self, reason: str, detail: str = "", last: date | None = None) -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail
        self.last = last
        """``settled``: the date of the trade's last flow (a swap's last payment date, not its maturity)."""


def _num(v: Any) -> float | None:
    return None if v is None else float(Decimal(str(v)))


def _day(v: Any) -> date | None:
    return v if isinstance(v, date) or v is None else date.fromisoformat(str(v))


def _ccy(v: Any) -> str | None:
    if v is None:
        return None
    c = str(v).strip().upper()
    return "RUB" if c in ("RUR", "SUR") else c


@dataclass(frozen=True)
class TermsLeg:
    """One ``trade_terms`` row (an exchange: an outright, an NDF, one leg of a swap)."""

    trade_id: str
    leg_id: str | None
    product: str
    book_id: str
    base: str
    quote: str
    side: str
    n_base: float
    n_quote: float
    rate: float
    """Quote currency per base: ``contract_rate``, else the amounts."""
    value_date: date
    ndf: bool
    settle_ccy: str | None
    fixing_date: date | None
    fixing_source: str | None
    pv_rub: float | None
    rate_note: str | None = None

    @property
    def sign(self) -> float:
        return 1.0 if self.side == "buy" else -1.0

    @property
    def label(self) -> str:
        return self.trade_id + (f"|{self.leg_id}" if self.leg_id else "")

    @classmethod
    def of(cls, r: dict[str, Any]) -> TermsLeg:
        base, quote = _ccy(r.get("base_ccy")), _ccy(r.get("quote_ccy"))
        nb, nq, vd = _num(r.get("notional_base")), _num(r.get("notional_quote")), _day(r.get("value_date"))
        side = str(r.get("side") or "").lower()
        if not base or not quote or base == quote or nb is None or nq is None or vd is None or side not in (
                "buy", "sell"):
            raise NotValued("terms_incomplete", f"trade {r.get('trade_id')}: pair, side, notionals and value_date")
        if nb <= 0 or nq <= 0:
            raise NotValued("terms_invalid", f"trade {r.get('trade_id')}: notionals must be positive")
        k, note = _num(r.get("contract_rate")), None
        if k is None:
            k = nq / nb
        elif abs(k - nq / nb) > RATE_TOLERANCE * k:
            note = f"contract_rate {k:g} differs from notional_quote / notional_base {nq / nb:g}: contract_rate used"
        return cls(str(r["trade_id"]), r.get("leg_id"), str(r.get("product_kind")), str(r.get("book_id")), base, quote,
                   side, nb, nq, k, vd, bool(r.get("ndf")), _ccy(r.get("settle_ccy")), _day(r.get("fixing_date")),
                   r.get("fixing_source"), _num(r.get("pv_rub")), note)


def _months(start: date, end: date) -> int:
    return (end.year - start.year) * 12 + end.month - start.month


def _leg_schedule(base: SwapLegSchedule, freq: str | None, dc: str | None, start: date, end: date, label: str
                  ) -> tuple[SwapLegSchedule, bool]:
    """The convention's leg schedule with the leg's own ``payment_frequency`` and ``day_count`` where it states them,
    and whether it states both."""
    if freq is None and dc is None:
        return base, False
    months, single, day_count = base.frequency_months, base.single_period_up_to_months, base.day_count
    if freq is not None:
        if freq == "term":
            months, single = max(1, _months(start, end) + 1), None  # one period: the roll steps past the start
        elif freq[-1] in "MY":
            months, single = int(freq[:-1]) * (12 if freq[-1] == "Y" else 1), None
        else:
            raise NotValued("swap_frequency_unsupported", f"{label}: payment_frequency {freq} (months or years only)")
    if dc is not None:
        if dc not in DAY_COUNTS:
            raise NotValued("day_count_unsupported", f"{label}: day_count {dc}")
        day_count = DAY_COUNTS[dc]
    return SwapLegSchedule(months, base.roll, day_count, single), freq is not None and dc is not None


def _leg_field(r: dict[str, Any], k: str) -> str | None:
    v = r.get(k)
    return None if v is None else str(v).strip().upper() if k == "day_count" else str(v).strip()


@dataclass(frozen=True)
class SwapTerms:
    """One ``irs`` / ``ois`` trade from its two leg rows: a fixed leg against a floating one, in RUB."""

    trade_id: str
    product: str
    book_id: str
    index: str
    """Core's index: ``ruonia`` or ``key_rate``."""
    side: str
    """``payer`` (the desk pays fixed) or ``receiver``."""
    notional: float
    fixed_rate_pct: float
    spread_bp: float
    start: date
    maturity: date
    fixed_leg: tuple[str | None, str | None]
    """(payment_frequency, day_count) of the fixed leg as the terms state them; the floating leg's below."""
    float_leg: tuple[str | None, str | None]
    pv_rub: float | None
    notes: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.trade_id

    @property
    def amount(self) -> float:
        return self.notional

    @property
    def end(self) -> date:
        return self.maturity

    @classmethod
    def of(cls, rows: list[dict[str, Any]]) -> SwapTerms:
        tid = str(rows[0].get("trade_id"))
        kinds = {r.get("product_kind") for r in rows}
        if len(kinds) != 1:
            raise NotValued("terms_invalid", f"trade {tid}: legs of different products {sorted(map(str, kinds))}")
        pay = [r for r in rows if str(r.get("side") or "").lower() == "pay"]
        rec = [r for r in rows if str(r.get("side") or "").lower() == "receive"]
        if len(rows) != 2 or len(pay) != 1 or len(rec) != 1:
            raise NotValued("terms_incomplete", f"trade {tid}: a swap needs one pay and one receive leg, "
                                                f"has {len(rows)} rows")
        fixed = [r for r in rows if r.get("fixed_rate_pct") is not None and r.get("index_code") is None]
        floating = [r for r in rows if r.get("index_code") is not None and r.get("fixed_rate_pct") is None]
        if len(floating) == 2:
            raise NotValued("basis_swap_unsupported", f"trade {tid}: two floating legs (a basis swap)")
        if len(fixed) != 1 or len(floating) != 1:
            raise NotValued("terms_incomplete", f"trade {tid}: one fixed (fixed_rate_pct) and one floating "
                                                "(index_code) leg")
        fx, fl = fixed[0], floating[0]
        ccys = {_ccy(r.get("ccy")) for r in rows}
        if ccys != {"RUB"}:
            raise NotValued("swap_ccy_unsupported", f"trade {tid}: legs in {sorted(map(str, ccys))}, RUB swaps only")
        code = str(fl["index_code"]).strip().upper()
        if code not in SWAP_INDEX:
            raise NotValued("swap_index_unsupported", f"trade {tid}: index {code} (RUONIA and KEY_RATE only)")
        n = [_num(r.get("notional_ccy")) for r in (fx, fl)]
        starts, ends = [_day(r.get("start_date")) for r in (fx, fl)], [_day(r.get("maturity_date")) for r in (fx, fl)]
        if None in n or None in starts or None in ends:
            raise NotValued("terms_incomplete", f"trade {tid}: notional_ccy, start_date and maturity_date of both legs")
        if abs(n[0] - n[1]) > 1e-9 * max(abs(n[0]), 1.0):  # type: ignore[operator]
            raise NotValued("swap_notional_mismatch", f"trade {tid}: the legs' notionals differ")
        if starts[0] != starts[1] or ends[0] != ends[1]:
            raise NotValued("swap_dates_mismatch", f"trade {tid}: the legs' start or maturity dates differ")
        if n[0] <= 0:  # type: ignore[operator]
            raise NotValued("terms_invalid", f"trade {tid}: notional must be positive")
        notes = []
        product = str(fx.get("product_kind"))
        if (product == "ois") != (code == "RUONIA"):
            notes.append(f"{tid}: product {product} on {code}: valued as a {SWAP_INDEX[code]} swap")
        pvs = [p for r in rows if (p := _num(r.get("pv_rub"))) is not None]
        return cls(tid, product, str(fx.get("book_id")), SWAP_INDEX[code],
                   "payer" if fx is pay[0] else "receiver", n[0], _num(fx["fixed_rate_pct"]),  # type: ignore[arg-type]
                   _num(fl.get("spread_bp")) or 0.0, starts[0], ends[0],  # type: ignore[arg-type]
                   (_leg_field(fx, "payment_frequency"), _leg_field(fx, "day_count")),
                   (_leg_field(fl, "payment_frequency"), _leg_field(fl, "day_count")),
                   math.fsum(pvs) if pvs else None, tuple(notes))

    def core_swap(self) -> tuple[InterestRateSwap, list[str]]:
        """Core's swap and the notes on its convention; raises ``NotValued``."""
        base = swap_convention(SWAP_SPEC[self.index])
        fixed, s1 = _leg_schedule(base.fixed_leg, *self.fixed_leg, self.start, self.maturity, f"{self.label} fixed")
        floating, s2 = _leg_schedule(base.float_leg, *self.float_leg, self.start, self.maturity,
                                     f"{self.label} float")
        notes = list(self.notes)
        if not (s1 and s2):
            notes.append(f"{self.label}: convention {base.spec_id} where the terms state no payment frequency or "
                         "day count (provisional)")
        conv = replace(base, fixed_leg=fixed, float_leg=floating)
        swap = InterestRateSwap(self.label, self.side, self.notional, self.fixed_rate_pct / 100.0,  # type: ignore[arg-type]
                                self.start, self.maturity, conv, self.spread_bp / 1e4)
        return swap, notes


@dataclass(frozen=True)
class RepoTerms:
    """One ``repo`` row: a REPO (``repo``, the desk borrows cash) or a reverse REPO (``reverse_repo``, lends it)."""

    trade_id: str
    leg_id: str | None
    book_id: str
    side: str
    ccy: str | None
    cash: float
    start: date
    maturity: date | None
    fixed_rate_pct: float | None
    index_code: str | None
    spread_bp: float
    day_count: str | None
    pv_rub: float | None
    security: str | None

    @property
    def label(self) -> str:
        return self.trade_id + (f"|{self.leg_id}" if self.leg_id else "")

    @property
    def amount(self) -> float:
        """Signed cash: + lent (reverse REPO), - borrowed."""
        return self.cash if self.side == "reverse_repo" else -self.cash

    @property
    def end(self) -> date:
        return self.maturity or self.start

    @classmethod
    def of(cls, r: dict[str, Any]) -> RepoTerms:
        tid, side = str(r.get("trade_id")), str(r.get("side") or "").lower()
        cash, start = _num(r.get("notional_ccy")), _day(r.get("start_date"))
        if side not in ("repo", "reverse_repo") or cash is None or start is None:
            raise NotValued("terms_incomplete", f"REPO {tid}: side, notional_ccy and start_date")
        if (r.get("fixed_rate_pct") is None) == (r.get("index_code") is None):
            raise NotValued("terms_incomplete", f"REPO {tid}: exactly one of fixed_rate_pct and index_code")
        if cash <= 0:
            raise NotValued("terms_invalid", f"REPO {tid}: notional_ccy must be positive")
        sec = next((str(r[k]) for k in ("instrument_id", "isin", "moex_secid", "regnum", "source_instrument_id")
                    if r.get(k)), None)
        return cls(tid, r.get("leg_id"), str(r.get("book_id")), side, _ccy(r.get("ccy")), cash, start,
                   _day(r.get("maturity_date")), _num(r.get("fixed_rate_pct")),
                   str(r["index_code"]).strip().upper() if r.get("index_code") is not None else None,
                   _num(r.get("spread_bp")) or 0.0, _leg_field(r, "day_count"), _num(r.get("pv_rub")), sec)


RatesTrade = SwapTerms | RepoTerms


@dataclass
class Request:
    """A position row valued here: its trades' legs, its swaps or REPOs, or a FORTS future."""

    position_id: str
    book_id: str
    row: dict[str, Any]
    """The positions row (``positions.normalised``)."""
    legs: list[TermsLeg] = field(default_factory=list)
    future: dict[str, Any] | None = None
    """The FORTS row of a future (``MarketView.future``) and ``contracts`` the signed number of contracts."""
    contracts: float | None = None
    trades: list[RatesTrade] = field(default_factory=list)
    """The swaps and REPOs of the row (``SwapTerms``, ``RepoTerms``), one core instrument each."""
    partial: bool = False
    """Some trades of the row have settled and are left out: the report's PV covers more than the valued ones."""


def request_of(position_id: str, book_id: str, row: dict[str, Any], terms: list[dict[str, Any]]) -> Request:
    """The request of a positions row from its linked trade_terms rows (``link``); raises ``NotValued`` for terms
    core is not given (a CCS, FX and rates trades under one row, incomplete terms)."""
    kinds = {str(t.get("product_kind")) for t in terms}
    if kinds <= set(FX_KINDS):
        return Request(position_id, book_id, row, [TermsLeg.of(t) for t in terms])
    if "ccs" in kinds:
        raise NotValued("ccs_unsupported", "a cross-currency swap: not valued by core in the monitor yet "
                                           "(no cross-currency or basis swap mapping), the report's PV")
    if kinds & set(FX_KINDS):
        raise NotValued("terms_mixed", f"FX and rates trades under one position: {sorted(kinds)}")
    by_trade: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in terms:
        by_trade[str(t.get("trade_id"))].append(t)
    trades: list[RatesTrade] = []
    for _, ts in sorted(by_trade.items()):
        if ts[0].get("product_kind") == "repo":
            trades += [RepoTerms.of(t) for t in ts]
        else:
            trades.append(SwapTerms.of(ts))
    return Request(position_id, book_id, row, trades=trades)


def link(rows: list[tuple[str, str, dict[str, Any]]], terms: list[dict[str, Any]]
         ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """position_id -> its trade_terms rows (``position_id`` of the terms, else ``trade_id`` = the row's ``deal_id`` or
    ``position_id``, same book), for ``rows`` = (position_id, book_id, positions row) of the FX and OTC rows; and the
    FX terms rows no row names (``fx_unlinked_terms``). Linked: the products of ``LINKED_KINDS``."""
    by_pid: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_trade: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in terms:
        if t.get("product_kind") not in LINKED_KINDS:
            continue
        b = str(t.get("book_id"))
        if t.get("position_id"):
            by_pid[(b, str(t["position_id"]))].append(t)
        else:
            by_trade[(b, str(t["trade_id"]))].append(t)
    out: dict[str, list[dict]] = {}
    used: set[int] = set()
    for pid, book, x in rows:
        got = by_pid.get((book, pid)) or by_trade.get((book, str(x.get("deal_id")))) or by_trade.get((book, pid)) or []
        if got:
            out[pid] = got
            used |= {id(t) for t in got}
    unlinked = [t for ts in (*by_pid.values(), *by_trade.values()) for t in ts
                if id(t) not in used and t.get("product_kind") in FX_KINDS]
    return out, unlinked


def _calendar_entry(ctx: Any, cid: str, ids: tuple[str, ...]) -> CalendarEntry:
    cals = {c.calendar_id: c.calendar for c in ctx.core_market.calendars}
    parts = [cals[i] for i in ids if i in cals]
    if len(parts) != len(ids):
        raise NotValued("no_fx_calendar", f"{', '.join(i for i in ids if i not in cals)} not in the run")
    return CalendarEntry(cid, joint_calendar(parts, version="+".join(p.version for p in parts)))


def core_legs(ctx: Any, leg: TermsLeg, iid: str, options: FxOptions, curve_of: dict[str, str]
              ) -> tuple[Any, str, list[str]]:
    """(core instrument, position currency, notes) of one exchange; raises ``NotValued``."""
    bd, notes = ctx.business_date, [leg.rate_note] if leg.rate_note else []
    if leg.value_date < bd:
        raise NotValued("settled", f"{leg.label} value date {leg.value_date} before {bd}")
    rub = leg.base == "RUB" or leg.quote == "RUB"
    if leg.ndf and (not rub or (leg.settle_ccy or "RUB") != "RUB"):
        raise NotValued("ndf_unsupported", f"{leg.label}: an NDF settled in {leg.settle_ccy} or a cross NDF "
                                           "(core BACKLOG #135)")
    for c in (leg.base, leg.quote):
        if c != "RUB" and c not in ctx.fx.spots:
            raise NotValued("no_fx_spot", f"{c} on {bd}")
        if c != "RUB" and c not in curve_of:
            raise NotValued("no_foreign_curve", f"no {c} curve in the run")
    if not rub:
        trade = FxCrossForward(leg.label, leg.base, leg.quote, 1.0,
                               FxForwardLeg(leg.value_date, leg.sign * leg.n_base, leg.rate))
        cal = f"FX:{leg.base}/{leg.quote}"
        return FxCrossInstrument(iid, trade, RUONIA_OIS_CURVE_ID, curve_of[leg.base], curve_of[leg.quote], cal), \
            leg.base, notes
    if leg.base != "RUB":
        ccy, n, k = leg.base, leg.sign * leg.n_base, leg.rate
    else:  # RUB/X: the desk buys RUB = sells X
        ccy, n, k = leg.quote, -leg.sign * leg.n_quote, 1.0 / leg.rate
    pay = fixing = source = None
    if leg.ndf:
        pay = leg.value_date
        fixing, source = leg.fixing_date, options.fixing_source(leg.fixing_source)
        if fixing is None:
            notes.append(f"{leg.label}: NDF without fixing_date, valued as cash-settled on its value date")
            source = None
    fx_leg = FxForwardLeg(leg.value_date, n, k, pay, fixing_date=fixing, fixing_source=source if fixing else None)
    return FxForwardInstrument(iid, FxForward(leg.label, ccy, 1.0, fx_leg), RUONIA_OIS_CURVE_ID, curve_of[ccy]), \
        ccy, notes


def _future(ctx: Any, r: Request, iid: str, curve_of: dict[str, str]) -> tuple[Any, str, Any]:
    f = r.future or {}
    ccy, price, unit = f.get("base_ccy"), _num(f.get("settle_price")), _num(f.get("settle_rub_per_unit"))
    lot, d = _num(f.get("lot_volume")), _day(f.get("last_del_date"))
    if not ccy or not price or not unit or not lot or d is None:
        raise NotValued("future_terms_incomplete", f"{f.get('secid')}: base, settle price, lot, delivery date")
    if ccy not in curve_of or ccy not in ctx.fx.spots:
        raise NotValued("no_foreign_curve", f"no {ccy} curve or spot in the run")
    contract = FxFuturesContract(f["secid"], ccy, lot, round(price / unit, 6), d)
    return FxFuturesInstrument(iid, contract, RUONIA_OIS_CURVE_ID, curve_of[ccy]), ccy, \
        FuturesQuote(f["secid"], price, price)


def _fixing_ids(ctx: Any) -> set[str]:
    return {f.fixing_id for f in ctx.core_market.fixings}


def _swap_instrument(ctx: Any, s: SwapTerms, iid: str, calendars: dict[str, CalendarEntry]
                     ) -> tuple[SwapInstrument, list[str]]:
    """Core's swap position of one ``SwapTerms`` (the module docstring); adds its calendar to ``calendars``."""
    bd = ctx.business_date
    if s.maturity < bd - timedelta(days=FLOW_DAYS + 7):  # long ago, whatever the payment lag, weekends and holidays
        raise NotValued("settled", f"swap {s.label} matured on {s.maturity}", s.maturity)
    swap, notes = s.core_swap()
    cid = SWAP_CALENDAR[s.index]
    if cid not in calendars:
        build = rates.ois_calendar(ctx.static.snapshot, bd) if s.index == "ruonia" else \
            rates.krs_calendar(ctx.static.snapshot)
        calendars[cid] = CalendarEntry(cid, build[0])
    try:
        sched = swap_schedule(swap, calendars[cid].calendar)
    except (ValueError, CoreRejection):
        if s.maturity < bd:  # its dates are outside the calendar of the run: it ended before it
            raise NotValued("settled", f"swap {s.label} matured on {s.maturity}", s.maturity) from None
        raise
    last = max(p.payment_date for p in (*sched.fixed, *sched.floating))
    if last <= bd:  # core: no flow after the valuation date. By the schedule, not the maturity: the last payment
        # follows the adjusted end by the payment lag, over a weekend it is days after the maturity
        raise NotValued("settled", f"swap {s.label} paid its last flow on {last}", last)
    projection = None
    if s.index == "key_rate":
        if KEY_RATE_SWAP_CURVE_ID not in {c.curve_id for c in ctx.swap_curves}:
            raise NotValued("no_key_rate_curve", f"no key-rate curve on {bd}: key-rate swaps keep the report's value")
        projection = KEY_RATE_SWAP_CURVE_ID
    fixing = None
    if s.start < bd:  # a period has started: its known part comes from the fixings
        fixing = FIXING_ID[s.index]
        if fixing not in _fixing_ids(ctx):
            raise NotValued("no_fixings", f"no {fixing} fixings in the run on {bd} for the started swap {s.label}")
    return SwapInstrument(iid, swap, cid, RUONIA_OIS_CURVE_ID, projection, fixing), notes


def _repo_instrument(ctx: Any, r: RepoTerms, iid: str) -> tuple[RepoInstrument, list[str]]:
    """Core's REPO position of one ``RepoTerms`` (the module docstring)."""
    bd, notes = ctx.business_date, []
    if r.ccy != "RUB":
        raise NotValued("repo_ccy_unsupported", f"REPO {r.label} in {r.ccy}: RUB only")
    if r.maturity is None:
        raise NotValued("repo_open", f"REPO {r.label} has no maturity_date: an open REPO needs an assumed end "
                                     "(not set)")
    if r.maturity <= bd:
        raise NotValued("settled", f"REPO {r.label} ended on {r.maturity}", r.maturity)
    if r.day_count not in REPO_BASIS:
        raise NotValued("day_count_unsupported", f"REPO {r.label}: day_count {r.day_count}")
    basis = REPO_BASIS[r.day_count]
    if r.day_count in (None, "ACT/ACT"):
        notes.append(f"{r.label}: day basis {r.day_count or 'not stated'} taken as ACT/365-366, the MOEX rouble basis "
                     "(provisional)")
    projection = fixing = None
    rate: FixedRepoRate | FloatingRepoRate
    if r.index_code is None:
        rate = FixedRepoRate(r.fixed_rate_pct / 100.0, basis)  # type: ignore[operator]
    else:
        if r.index_code not in REPO_OBSERVATION:
            reason = "no_fixings" if r.index_code == "RUSFAR" else "repo_index_unsupported"
            raise NotValued(reason, f"REPO {r.label} on {r.index_code}: RUONIA and KEY_RATE only (the run has "
                                    "no fixings of other indices)")
        fixing = r.index_code
        if fixing not in _fixing_ids(ctx):
            raise NotValued("no_fixings", f"no {fixing} fixings in the run on {bd} for REPO {r.label}")
        if r.index_code == "KEY_RATE":
            if KEY_RATE_CURVE_ID not in {c.curve_id for c in ctx.core_market.curves}:
                raise NotValued("no_key_rate_curve", f"no key-rate curve on {bd} for REPO {r.label}")
            projection = KEY_RATE_CURVE_ID
        rate = FloatingRepoRate(r.index_code, r.spread_bp / 1e4, basis,  # type: ignore[arg-type]
                                REPO_OBSERVATION[r.index_code], DayCount.ACT_365_FIXED)  # type: ignore[arg-type]
    trade = RepoTrade(r.label, r.side, r.start, r.maturity, r.cash, rate, REPO_ROUNDING)  # type: ignore[arg-type]
    return RepoInstrument(iid, trade, REPO_CALENDAR, RUONIA_OIS_CURVE_ID, projection, fixing), notes


def _published(name: str) -> str:
    if name.startswith("IR|"):
        _, c, b = name.split("|")
        return f"IR|{PUBLISHED_CURVES.get(c, c)}|{b}"
    return name


@dataclass
class _Acc:
    pv: float = 0.0
    first: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    hessian: dict[tuple[str, str], float] = field(default_factory=lambda: defaultdict(float))
    fx_delta: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    legs: list[dict[str, Any]] = field(default_factory=list)
    fixings: list[dict[str, Any]] = field(default_factory=list)
    flags: set[str] = field(default_factory=set)
    curves: set[str] = field(default_factory=set)
    fallback_from: set[str] = field(default_factory=set)
    products: set[str] = field(default_factory=set)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    calc_ids: list[str] = field(default_factory=list)
    swaps: list[dict[str, Any]] = field(default_factory=list)
    repos: list[dict[str, Any]] = field(default_factory=list)
    fixing_ids: set[str] = field(default_factory=set)
    flows: list[dict[str, Any]] | None = field(default_factory=list)
    """``extra.flows`` of the row; None only in tests (flows not recorded: the PnL says so)."""

    def add_risk(self, row: Any) -> None:
        self.pv += row.pv_rub
        self.calc_ids.append(row.calculation_id)
        for f, x in row.factor_risk.first_order_by_factor().items():
            self.first[_published(f)] += x
        for (fi, fj), x in row.factor_risk.hessian_by_factor().items():
            self.hessian[tuple(sorted((_published(fi), _published(fj))))] += x  # type: ignore[index]


def _curve(cid: str | None) -> str | None:
    return None if cid is None else PUBLISHED_CURVES.get(cid, cid)


def _swap_fact(t: SwapTerms, row: Any) -> dict[str, Any]:
    """A swap of ``extra.swaps``: its terms and core's figures (RUB)."""
    return {"trade_id": t.trade_id, "product": t.product, "index": FIXING_ID[t.index], "side": t.side,
            "notional_rub": row.notional_rub, "fixed_rate_pct": t.fixed_rate_pct, "spread_bp": t.spread_bp,
            "start_date": t.start.isoformat(), "maturity_date": t.maturity.isoformat(), "convention": row.convention,
            "pv_rub": row.pv_rub, "clean_pv_rub": row.clean_pv_rub, "accrued_rub": row.accrued_rub,
            "fixed_leg_pv_rub": row.fixed_leg_pv_rub, "float_leg_pv_rub": row.float_leg_pv_rub,
            "par_rate_pct": row.par_rate * 100.0, "annuity": row.annuity,
            "discount_curve": _curve(row.discount_curve_id), "projection_curve": _curve(row.projection_curve_id),
            "fixing_id": row.fixing_id, "fixing_snapshot_id": row.fixing_snapshot_id,
            **({"reported_pv_rub": t.pv_rub} if t.pv_rub is not None else {})}


def _repo_fact(t: RepoTerms, row: Any) -> dict[str, Any]:
    """A REPO of ``extra.repos``: its terms and core's figures (RUB)."""
    return {"trade_id": t.label, "side": t.side, "start_date": t.start.isoformat(),
            "end_date": row.end_date.isoformat(), "fixed_rate_pct": t.fixed_rate_pct, "index": t.index_code,
            "spread_bp": t.spread_bp if t.index_code else None, "start_amount_rub": row.start_amount_rub,
            "end_amount_rub": row.end_amount_rub, "end_amount_source": row.end_amount_source,
            "known_interest_rub": row.known_interest_rub, "accrued_interest_rub": row.accrued_interest_rub,
            "current_repurchase_amount_rub": row.current_repurchase_amount_rub, "pv_rub": row.pv_rub,
            "discount_curve": _curve(row.discount_curve_id), "projection_curve": _curve(row.projection_curve_id),
            "fixing_id": row.fixing_id, "fixing_snapshot_id": row.fixing_snapshot_id, "security": t.security,
            **({"reported_pv_rub": t.pv_rub} if t.pv_rub is not None else {})}


def index_fixings(snapshot: Any, fixing_id: str) -> dict[str, Any]:
    """What a swap or REPO read of the run's fixing history ``fixing_id``: its latest fixing, the declared unpublished
    business days before the business date (carried at the latest fixing, at most ``max_unpublished``) and the
    business days missing inside it (core rejects a position that reads one)."""
    h = next(e.history for e in snapshot.fixings if e.fixing_id == fixing_id)
    have = {d for d, _ in h.fixings}
    first, latest = (h.fixings[0][0], h.latest_fixing_date) if h.fixings else (None, None)
    missing = []
    if first is not None and latest is not None:
        d = first
        while d < latest:
            if h.calendar.is_business_day(d) and d not in have:
                missing.append(d.isoformat())
            d = date.fromordinal(d.toordinal() + 1)
    return {"snapshot_id": h.snapshot_id, "known_at": h.known_at_utc, "first": first and first.isoformat(),
            "latest": latest and latest.isoformat(), "latest_rate_pct": round(h.fixings[-1][1] * 100, 10) if h.fixings else None,
            "unpublished_business_days": [d.isoformat() for d in h.unpublished_business_days],
            "max_unpublished_business_days": h.max_unpublished_business_days,
            "missing_business_days": missing}


def _ui_leg(t: TermsLeg | None, lv: Any, ccy: str, cross: bool = False) -> dict[str, Any]:
    """A leg in the concept pages' shape (``prototypes/concepts/lib/real.js``, ``extra.legs``)."""
    out: dict[str, Any] = {"value_date": lv.value_date.isoformat(), "pv_rub": lv.pv_rub,
                           "df": lv.rub_discount_factor, "fwd_rate": lv.forward_rate, "points": lv.forward_points}
    if t is not None:
        buy, sell = (t.base, t.quote) if t.side == "buy" else (t.quote, t.base)
        out.update(leg=t.leg_id or "single", trade_id=t.trade_id, buy_ccy=buy,
                   buy_amount=t.n_base if t.side == "buy" else t.n_quote, sell_ccy=sell,
                   sell_amount=t.n_quote if t.side == "buy" else t.n_base, contract_rate=t.rate,
                   rate_quote=f"{t.base}/{t.quote}", settlement="cash" if t.ndf else "deliverable")
        if t.ndf:
            out.update(settlement_ccy=t.settle_ccy or "RUB", payment_date=lv.payment_date.isoformat(),
                       fixing_source=t.fixing_source)
            if t.fixing_date is not None:
                out["fixing_date"] = t.fixing_date.isoformat()
    if cross:
        out.update(base_forward_rate=lv.base_forward_rate, quote_forward_rate=lv.quote_forward_rate)
    elif t is not None and t.base == "RUB":  # read the other way round: core's rate is RUB per unit of ``ccy``
        out["core_rate_quote"] = f"{ccy}/RUB"
    if getattr(lv, "fixing_rate", None) is not None:
        out["fixing_rate"] = lv.fixing_rate
    return out


def legs_from_fact(extra: dict[str, Any]) -> list[TermsLeg]:
    """The trade legs of a published row (``extra.legs`` of ``_ui_leg``) back as ``TermsLeg``: what the exact scenario
    reprices a stored run's FX derivative from (``scenario.exact``). Empty for a row without legs (an FX future)."""
    out = []
    for lg in extra.get("legs") or ():
        if "rate_quote" not in lg or "buy_ccy" not in lg:
            return []
        base, quote = lg["rate_quote"].split("/")
        buy = lg["buy_ccy"] == base
        out.append(TermsLeg.of({
            "trade_id": lg.get("trade_id") or "?", "leg_id": None if lg.get("leg") == "single" else lg.get("leg"),
            "product_kind": "fx_swap" if lg.get("leg") in ("near", "far") else "fx_forward", "book_id": "",
            "base_ccy": base, "quote_ccy": quote, "side": "buy" if buy else "sell",
            "notional_base": lg["buy_amount"] if buy else lg["sell_amount"],
            "notional_quote": lg["sell_amount"] if buy else lg["buy_amount"], "contract_rate": lg["contract_rate"],
            "value_date": lg["value_date"], "ndf": lg.get("settlement") == "cash",
            "settle_ccy": lg.get("settlement_ccy"), "fixing_date": lg.get("fixing_date"),
            "fixing_source": lg.get("fixing_source")}))
    return out


def value(ctx: Any, requests: list[Request], *, options: FxOptions, second_order: bool = True,
          scenarios: tuple = ()) -> tuple[dict[str, BondValuation | NotValued], Any]:
    """Value ``requests`` in one core call. Returns per position ID a valuation or why not (``NotValued``: the row then
    keeps its earlier rule), and the core result (None without an instrument). ``scenarios`` (core
    ``PortfolioScenario``) go to the core call; a position's core positions are ``<position_id>#<n>``."""
    bd = ctx.business_date
    out: dict[str, BondValuation | NotValued] = {}
    if not requests:
        return out, None
    present = {c.curve_id for c in ctx.core_market.curves}
    if RUONIA_OIS_CURVE_ID not in present:
        why = NotValued("no_rub_curve", f"no RUONIA OIS curve on {bd}: derivatives keep the report's value")
        return {r.position_id: why for r in requests}, None
    curve_of, fallbacks = {}, {}
    for ccy in ctx.fx.spots:
        primary, alts = ctx.fx.fx_curve_ids(ccy, options)
        have = [i for i in (primary, *alts) if i in present]
        if have:
            curve_of[ccy] = primary
            fallbacks.update({(primary, a): None for a in alts if a in present})
    instruments, positions, quotes, owner, calendars = [], [], [], {}, {}
    terms_of: dict[str, Any] = {}
    notes: dict[str, list[str]] = defaultdict(list)
    flows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    live_of: dict[str, Request] = {}
    pay_dates: dict[str, tuple[date, ...]] = {}
    for r in requests:
        built: list[tuple[Any, str, Any, float]] = []
        try:
            if r.trades:
                gone: list[tuple[Any, NotValued]] = []
                for n, t in enumerate(r.trades):
                    iid = f"{r.position_id}#{n}"
                    try:
                        inst, more = (_swap_instrument(ctx, t, iid, calendars) if isinstance(t, SwapTerms)
                                      else _repo_instrument(ctx, t, iid))
                    except NotValued as e:
                        if e.reason != "settled":
                            raise
                        gone.append((t, e))
                        continue
                    notes[r.position_id] += more
                    built.append((inst, "RUB", t, 1.0))
                    pay_dates[iid] = _pay_dates(inst, t, calendars, bd)
                if gone and not built:
                    out[r.position_id] = _settled_row(ctx, r, [(t, e.last or _last_event(t)) for t, e in gone], gone[0][1],
                                                      notes[r.position_id])
                    continue
                if gone:  # the others are valued, the settled ones' last flows are in the PnL (pnl.direct)
                    notes[r.position_id].append("settled trades left out: " + ", ".join(t.label for t, _ in gone))
                    dead = {id(t) for t, _ in gone}
                    live_of[r.position_id] = replace(r, trades=[t for t in r.trades if id(t) not in dead], partial=True)
            elif r.future is not None:
                iid = f"{r.position_id}#fut"
                inst, ccy, q = _future(ctx, r, iid, curve_of)
                built.append((inst, ccy, None, r.contracts or 0.0))
                quotes.append(q)
            else:
                live = [t for t in r.legs if t.value_date >= bd]
                gone_legs = [t for t in r.legs if t.value_date < bd]
                if gone_legs:
                    notes[r.position_id].append("settled legs left out: " + ", ".join(t.label for t in gone_legs))
                if not live:
                    out[r.position_id] = _settled_row(
                        ctx, r, [(t, t.value_date) for t in gone_legs],
                        NotValued("settled", "every leg settled before the business date"), notes[r.position_id])
                    continue
                for n, t in enumerate(live):
                    iid = f"{r.position_id}#{n}"
                    inst, ccy, more = core_legs(ctx, t, iid, options, curve_of)
                    notes[r.position_id] += more
                    if isinstance(inst, FxCrossInstrument) and inst.calendar_id not in calendars:
                        calendars[inst.calendar_id] = _calendar_entry(
                            ctx, inst.calendar_id, (f"FX:{t.base}", f"FX:{t.quote}"))
                    built.append((inst, ccy, t, 1.0))
                    flows[r.position_id] += _leg_flows(t, bd)
        except (NotValued, CoreRejection, ValueError) as e:
            out[r.position_id] = e if isinstance(e, NotValued) else NotValued("terms_invalid", str(e))
            continue
        for inst, ccy, t, qty in built:
            instruments.append(inst)
            positions.append(Position(inst.instrument_id, inst.instrument_id, qty, r.book_id or "desk",
                                      AccountingCategory.TRADING, ccy))
            owner[inst.instrument_id] = r
            terms_of[inst.instrument_id] = t
    if not instruments:
        return out, None
    market = ctx.fx_risk_market
    # RUONIA past its last quote as the bonds' (bonds.OIS_TAIL, TDM#499): on its quotes, with the KBD it follows
    tail = any(c.curve_id == RUONIA_OIS_CURVE_ID and isinstance(c.curve, CurveBootstrapRequest) for c in market.curves)
    used = {RUONIA_OIS_CURVE_ID, *curve_of.values(), *(a for _, a in fallbacks),
            *((OIS_TAIL.kbd_curve_id,) if tail else ())}
    used |= {c for i in instruments if isinstance(i, (SwapInstrument, RepoInstrument))
             and (c := i.projection_curve_id) is not None}
    fixing_ids = {f for i in instruments if isinstance(i, (SwapInstrument, RepoInstrument))
                  and (f := i.fixing_id) is not None}
    # risk on RUONIA in its OIS par quotes, as the bonds' (EodContext.quote_curves); with the setting quote_curve_risk
    # the CNY XCCY curve as its par quotes too (EodContext.fx_risk_market), rebuilt by core on every shock. The fixings
    # only a swap or a REPO reads (point-in-time, with RUONIA's declared unpublished day), so an FX book's inputs stay
    # as they were; the key-rate forward curve of a key-rate swap from EodContext.swap_curves
    snap = replace(market, quotes=(),
                   fixings=tuple(f for f in ctx.core_market.fixings if f.fixing_id in fixing_ids),
                   curves=(*(c for c in market.curves if c.curve_id in used),
                           *(c for c in ctx.swap_curves if c.curve_id in used)),
                   calendars=(*ctx.core_market.calendars, *calendars.values()),
                   futures_quotes=tuple(quotes))
    # core's quote_curve_risk only when the call holds quotes it takes for them: a call without keeps its job ID
    quoted = any(isinstance(c.curve, XccyCurveBootstrapRequest | ParYieldCurveRequest) for c in snap.curves)
    policy = ValuationPolicy(RUONIA_OIS_CURVE_ID, include_second_order=second_order,
                             fx_curve_fallbacks=tuple(fallbacks),
                             quote_curve_risk=ctx.quote_curve_risk and quoted, ois_tail=OIS_TAIL if tail else None)
    result = value_portfolio(run_id=f"desk-eod-{bd}-derivatives", as_of=ctx.core_market.known_at, snapshot=snap,
                             instruments=instruments, positions=positions, policy=policy, scenarios=scenarios)
    _rates_flows(snap, policy, instruments, positions, pay_dates, owner, terms_of, flows)
    acc: dict[str, _Acc] = defaultdict(_Acc)
    for rej in result.coverage.rejections:
        acc[owner[rej.position_id].position_id].rejected.append((rej.code, rej.message))
    flags = {m.position_id: m.flags for m in result.position_models}
    for row in (*result.swaps, *result.repos):
        r, t = owner[row.position_id], terms_of[row.position_id]
        a = acc[r.position_id]
        a.add_risk(row)
        a.flags |= set(flags.get(row.position_id, ()))
        a.curves |= {c for c in (_curve(row.discount_curve_id), _curve(row.projection_curve_id)) if c}
        if row.fixing_id:
            a.fixing_ids.add(row.fixing_id)
        if isinstance(t, SwapTerms):
            a.products.add(t.product)
            a.swaps.append(_swap_fact(t, row))
        else:
            a.products.add("repo")
            a.repos.append(_repo_fact(t, row))
    for row in (*result.fx_forwards, *result.fx_crosses, *result.fx_futures):
        r, t = owner[row.position_id], terms_of[row.position_id]
        a = acc[r.position_id]
        a.add_risk(row)
        a.flags |= set(flags.get(row.position_id, ()))
        if hasattr(row, "fx_delta_base"):  # a cross
            a.fx_delta[row.base_currency] += row.fx_delta_base
            a.fx_delta[row.quote_currency] += row.fx_delta_quote
            a.curves |= {row.base_curve_id, row.quote_curve_id}
            a.fallback_from |= {c for c in (row.base_curve_fallback_from, row.quote_curve_fallback_from) if c}
            a.products.add("fx_cross")
            a.legs += [_ui_leg(t, lv, row.base_currency, cross=True) for lv in row.legs]
        elif hasattr(row, "contract_id"):  # an FX future
            a.fx_delta[row.currency] += row.fx_delta_foreign
            a.curves.add(row.foreign_curve_id)
            a.products.add("fx_future")
            if row.curve_fallback_from:
                a.fallback_from.add(row.curve_fallback_from)
        else:
            a.fx_delta[row.currency] += row.fx_delta_foreign
            a.curves.add(row.foreign_curve_id)
            if row.curve_fallback_from:
                a.fallback_from.add(row.curve_fallback_from)
            a.products.add("fx_ndf" if t is not None and t.ndf else "fx_swap" if t is not None and t.leg_id
                           else "fx_forward")
            for lv in row.legs:
                a.legs.append(_ui_leg(t, lv, row.currency))
                if t is not None and t.ndf and t.fixing_date is not None:
                    status = ("fixed" if lv.fixing_rate is not None else
                              "pending" if "fixing_pending" in flags.get(row.position_id, ()) else "open")
                    a.fixings.append({"trade_id": t.label, "fixing_date": t.fixing_date.isoformat(),
                                      "source": t.fixing_source, "status": status,
                                      **({"fixing_rate": lv.fixing_rate} if lv.fixing_rate is not None else {})})
    for r in requests:
        if r.position_id in out:
            continue
        a = acc.get(r.position_id)
        if a is None:
            out[r.position_id] = NotValued("not_valued", "core returned no row")
        elif a.rejected:
            code, msg = a.rejected[0]
            out[r.position_id] = NotValued(f"core_{code.lower()}", msg)
        elif r.trades:
            a.flows = flows[r.position_id]
            out[r.position_id] = _rates_valuation(ctx, live_of.get(r.position_id, r), a, notes[r.position_id], result,
                                                  snap)
        else:
            a.flows = flows[r.position_id]
            out[r.position_id] = _valuation(ctx, r, a, notes[r.position_id], result)
    return out, result


def _last_event(t: Any) -> date:
    """The date of a trade's last flow as its terms state it: a leg's value date, a swap's or REPO's end."""
    return t.value_date if isinstance(t, TermsLeg) else t.end


def _side_text(t: Any) -> str:
    """The side as ``tdm_run.rates_side`` reads it: ``PAY`` / ``REC`` for a swap, ``REPO`` / ``REVERSE_REPO``."""
    return t.side.upper() if isinstance(t, RepoTerms) else "PAY" if t.side == "payer" else "REC"


def _settled_row(ctx: Any, r: Request, gone_at: list[tuple[Any, date]], why: NotValued, notes: list[str]
                 ) -> BondValuation | NotValued:
    """A row whose every trade has paid its last flow by the business date, valued as settled (PV 0, no risk) when the
    last flow is within ``FLOW_DAYS``: the day-over-day PnL of its last day is then the flows it paid, less its PV of
    D-1 (``pnl.direct``), not nothing. A row settled earlier keeps ``why`` (the report's rule: nothing to carry)."""
    bd, x = ctx.business_date, r.row
    gone, ends = [t for t, _ in gone_at], {id(t): d for t, d in gone_at}
    last = max(ends.values())
    if last < bd - timedelta(days=FLOW_DAYS):
        return why
    rates = bool(r.trades)
    if not rates and _num(x.get("amount_ccy")) not in (None, 0.0):
        return why  # an FX row with an amount keeps the rule of cash (PV = amount x rate, FX delta, OCP): ``pnl.direct``
        # takes the settled legs' flows beside it
    labels = sorted({t.label for t in gone})
    if rates:
        product = "+".join(sorted({"repo" if isinstance(t, RepoTerms) else t.product for t in gone}))
        amount, ccy = math.fsum(t.amount for t in gone), "RUB"
    else:
        product = "+".join(sorted({"fx_ndf" if t.ndf else "fx_swap" if t.leg_id else "fx_forward" for t in gone}))
        ccy = str(x.get("ccy") or next((c for t in gone for c in (t.base, t.quote) if c != "RUB"), "RUB")).upper()
        amount = float(x["amount_ccy"]) if x.get("amount_ccy") is not None else 0.0
    spot = ctx.fx.rates.get(ccy, (1.0, ""))[0] if ccy != "RUB" else 1.0
    why_text = f"settled on {last}: PV 0, the flows of the last day are in the day's PnL"
    extra = {
        "core": {"product": product, "trades": sorted({t.trade_id for t in gone}), "model_flags": ["settled"],
                 "rub_curve": RISK_CURVES[RUONIA_OIS_CURVE_ID], "core_version": CORE_VERSION},
        "settled": [{"trade_id": t.label, "date": ends[id(t)].isoformat()} for t in gone],
        "flows": [],
        **({"reported_pv_rub": reported} if (reported := _reported_pv(x, [t.pv_rub for t in gone])) is not None
           else {}),
        **({"rates_settled": [{"trade_id": t.label, "side": _side_text(t)} for t in gone]} if rates else
           {"legs": [], "ocp_amount": {}, "fx_rates": {}}),
    }
    return BondValuation(
        secid="", quantity=amount, valuation_date=bd, settlement=bd, horizon_date=bd,
        horizon_kind="maturity" if rates else "value_date", price_basis="core", clean_pct=math.nan, accrued_rub=0.0,
        face_rub=1.0 if rates else spot, current_coupon_pct=None, dirty_rub=math.nan, pv_rub=0.0, ytm_pct=math.nan,
        macaulay_years=math.nan, modified_duration=math.nan, z_spread_bp=math.nan, spread_kind=product, dv01_rub=0.0,
        buckets=dict.fromkeys(RISK_BUCKETS, 0.0), spread01_rub=0.0,
        calculation_id=f"desk-derivatives:{bd}:{r.position_id}:settled", curve_buckets={}, hessian=[],
        quality="assumed", assumptions=(*notes, why_text, f"trades: {', '.join(labels)}"), fx_delta={}, extra=extra)


def _leg_flows(t: TermsLeg, bd: date) -> list[dict[str, Any]]:
    """The cash an FX leg settles on its value date, signed as received (+) or paid (-) per currency; core stops
    counting the leg from the day after (``out_on``). An NDF settles in cash at its fixing: its amount is not known."""
    if (t.value_date - bd).days > FLOW_DAYS:
        return []
    base = {"trade_id": t.label, "kind": "fx", "date": t.value_date.isoformat(),
            "out_on": (t.value_date + timedelta(days=1)).isoformat()}
    if t.ndf:
        return [{**base, "kind": "ndf", "ccy": None, "amount": None}]
    return [{**base, "ccy": t.base, "amount": t.sign * t.n_base}, {**base, "ccy": t.quote, "amount": -t.sign * t.n_quote}]


def _pay_dates(inst: Any, t: Any, calendars: dict[str, CalendarEntry], bd: date) -> tuple[date, ...]:
    """The dates in (bd, bd + ``FLOW_DAYS``] on which a swap or REPO pays: the periods' payment dates, a REPO's
    start (if still ahead) and end."""
    if isinstance(inst, SwapInstrument):
        sched = swap_schedule(inst.swap, calendars[inst.calendar_id].calendar)
        days = {p.payment_date for p in (*sched.fixed, *sched.floating)}
    else:
        days = {t.start, t.end}
    return tuple(sorted(d for d in days if bd < d <= bd + timedelta(days=FLOW_DAYS)))


def _rates_flows(snap: Any, policy: ValuationPolicy, instruments: list[Any], positions: list[Position],
                 pay_dates: dict[str, tuple[date, ...]], owner: dict[str, Request], terms_of: dict[str, Any],
                 flows: dict[str, list[dict[str, Any]]]) -> None:
    """Add to ``flows`` the cash the swaps and REPOs pay in the next ``FLOW_DAYS`` days, as core projects it: the
    ``received_rub`` of core's ``horizon_portfolio`` at each date a trade pays on is cumulative, so a payment is the
    difference between two horizons. A date core refuses (a rejection, an error) is recorded as a flow of unknown
    amount on that date only (and on the next payment of the trade, whose difference would hold it): the PnL
    says so (``received_unknown``) when such a date falls in its window, never 0, and not otherwise."""
    inst_of = {i.instrument_id: i for i in instruments}
    pos_of = {p.position_id: p for p in positions}
    jobs = {iid: ds for iid, ds in pay_dates.items() if ds and iid in inst_of}
    seen: dict[str, float] = {}
    stale: set[str] = set()  # trades whose last horizon failed: their next difference holds an unread payment
    cache = ShockedCurveCache()  # the curves are built once for all the horizons

    def add(iid: str, h: date, amount: float | None) -> None:
        flows[owner[iid].position_id].append({
            "trade_id": terms_of[iid].label, "kind": "swap" if isinstance(inst_of[iid], SwapInstrument) else "repo",
            "date": h.isoformat(), "out_on": h.isoformat(), "ccy": "RUB" if amount is not None else None,
            "amount": amount})

    for h in sorted({d for ds in jobs.values() for d in ds}):
        sel = [iid for iid, ds in jobs.items() if ds[0] <= h]
        try:
            res = horizon_portfolio(snapshot=snap, instruments=[inst_of[i] for i in sel],
                                    positions=[pos_of[i] for i in sel], policy=policy, horizon_date=h,
                                    curve_cache=cache)
            got = {row.position_id: row for row in (*res.swaps, *res.repos)}
        except (CoreRejection, ValueError, LookupError, ArithmeticError):
            got = {}
        for iid in sel:
            row = got.get(iid)
            if row is None:
                if h in jobs[iid]:  # its own payment is unread; a failure on another's date loses nothing of its:
                    stale.add(iid)  # ``seen`` stays, and the next difference holds only its own payments
                    add(iid, h, None)
                continue
            amount, seen[iid] = row.received_rub - seen.get(iid, 0.0), row.received_rub
            if iid in stale:
                stale.discard(iid)
                if h in jobs[iid]:
                    add(iid, h, None)
                continue
            if amount:
                add(iid, h, amount)


def _risk(a: _Acc) -> tuple[dict[str, dict[str, float]], dict[str, float], dict[str, float]]:
    """(bucket DV01 by published curve, the buckets summed, basis01 by currency) of an accumulated position."""
    by_curve: dict[str, dict[str, float]] = {}
    basis = {}
    for f, v in a.first.items():
        if f.startswith("IR|"):
            _, c, b = f.split("|")
            by_curve.setdefault(c, dict.fromkeys(RISK_BUCKETS, 0.0))[b] += v
        elif f.startswith("FXBASIS|") and v:
            basis[f.split("|", 1)[1]] = v
    by_curve = {c: bs for c, bs in by_curve.items() if any(bs.values())}  # a fixed NDF: no foreign curve risk
    buckets = {b: math.fsum(bs[b] for bs in by_curve.values()) for b in RISK_BUCKETS}
    return by_curve, buckets, basis


def _hessian(a: _Acc) -> list[tuple[str, str, float]]:
    return [(i, j, v) for (i, j), v in sorted(a.hessian.items())
            if v and i.startswith(PUBLISHED_FACTORS) and j.startswith(PUBLISHED_FACTORS)]


def _reported_pv(x: dict[str, Any], pvs: list[float | None]) -> float | None:
    """The bank's PV: the positions row's mv_rub, else the sum of its trades' pv_rub (trade_terms)."""
    reported = x.get("mv_rub")
    if reported is None and any(p is not None for p in pvs):
        reported = math.fsum(p for p in pvs if p is not None)
    return None if reported is None else float(reported)


def _rates_valuation(ctx: Any, r: Request, a: _Acc, notes: list[str], result: Any, snap: Any) -> BondValuation:
    """A row of swaps and REPOs valued by core: PV, rates risk per curve, the trades and the fixings read."""
    bd, x = ctx.business_date, r.row
    by_curve, buckets, _ = _risk(a)
    why = list(notes)
    fixings = {}
    for fid in sorted(a.fixing_ids):
        info = fixings[fid] = index_fixings(snap, fid)
        if info["unpublished_business_days"]:
            why.append(f"{fid} not yet published for {', '.join(info['unpublished_business_days'])}: the latest "
                       f"fixing of {info['latest']} carried (declared, at most "
                       f"{info['max_unpublished_business_days']} business day)")
    amount = math.fsum(t.amount for t in r.trades)
    # the report's PV of a row with settled trades covers more than the valued ones: no difference then
    reported = None if r.partial else _reported_pv(x, [t.pv_rub for t in r.trades])
    horizon = max(t.end for t in r.trades)
    product = "+".join(sorted(a.products))
    extra = {
        "core": {"product": product, "trades": sorted({t.trade_id for t in r.trades}),
                 "model_flags": sorted(a.flags), "rub_curve": RISK_CURVES[RUONIA_OIS_CURVE_ID],
                 "curves": sorted(a.curves), "result_schema": result.schema_version, "core_version": CORE_VERSION},
        **({"swaps": a.swaps} if a.swaps else {}), **({"repos": a.repos} if a.repos else {}),
        **({"index_fixings": fixings} if fixings else {}),
        **({"reported_pv_rub": reported, "pv_diff_rub": a.pv - reported} if reported is not None else {}),
        "flows": a.flows,
    }
    return BondValuation(
        secid="", quantity=amount, valuation_date=bd, settlement=bd, horizon_date=horizon, horizon_kind="maturity",
        price_basis="core", clean_pct=math.nan, accrued_rub=0.0, face_rub=1.0, current_coupon_pct=None,
        dirty_rub=a.pv / amount if amount else math.nan, pv_rub=a.pv, ytm_pct=math.nan, macaulay_years=math.nan,
        modified_duration=math.nan, z_spread_bp=math.nan, spread_kind=product, dv01_rub=math.fsum(buckets.values()),
        buckets=buckets, spread01_rub=0.0,
        calculation_id=a.calc_ids[0] if len(a.calc_ids) == 1 else f"desk-derivatives:{bd}:{r.position_id}",
        curve_buckets=by_curve, hessian=_hessian(a), quality="assumed" if why else "exact", assumptions=tuple(why),
        fx_delta={}, extra=extra)


def _valuation(ctx: Any, r: Request, a: _Acc, notes: list[str], result: Any) -> BondValuation:
    bd, x = ctx.business_date, r.row
    by_curve, buckets, basis = _risk(a)
    why = list(notes)
    if a.fallback_from:
        why.append("foreign curve absent: " + ", ".join(sorted(a.fallback_from)) + " replaced by the fallback "
                   + ", ".join(sorted(a.curves)))
    for c in sorted(a.curves & set(by_curve)):  # only a curve the position has risk on (not a fixed NDF's)
        anchor = ((ctx.fx.lineage.get("curves") or {}).get(c) or {}).get("anchor")
        if anchor:
            why.append(f"{c}: {anchor}")
    ccy = (x.get("ccy") or next(iter(a.fx_delta), "RUB")).upper()
    spot = ctx.fx.rates.get(ccy, (1.0, ""))[0] if ccy != "RUB" else 1.0
    amount = x.get("amount_ccy")
    if amount is None:
        amount = math.fsum(t.sign * t.n_base if t.base == ccy else -t.sign * t.n_quote if t.quote == ccy else 0.0
                           for t in r.legs if t.value_date >= bd) if r.future is None else (r.contracts or 0.0)
    ocp: dict[str, float] = defaultdict(float)
    for t in r.legs:
        if t.value_date >= bd:
            ocp[t.base] += t.sign * t.n_base
            ocp[t.quote] -= t.sign * t.n_quote
    if r.future is not None:
        f = r.future
        ocp[f["base_ccy"]] += (r.contracts or 0.0) * float(Decimal(str(f["lot_volume"])))
    reported = _reported_pv(x, [t.pv_rub for t in r.legs])
    horizon = max((t.value_date for t in r.legs if t.value_date >= bd), default=None)
    if r.future is not None:
        horizon = _day(r.future.get("last_del_date"))
    product = "+".join(sorted(a.products))
    extra = {
        "core": {"product": product, "trades": sorted({t.trade_id for t in r.legs}) or None,
                 "model_flags": sorted(a.flags), "rub_curve": RISK_CURVES[RUONIA_OIS_CURVE_ID],
                 "foreign_curves": sorted(a.curves), "result_schema": result.schema_version,
                 "core_version": CORE_VERSION,
                 **({"curve_fallback_from": sorted(a.fallback_from)} if a.fallback_from else {})},
        "legs": a.legs, "ocp_amount": {c: v for c, v in ocp.items() if c != "RUB" and v},
        "fx_rates": {c: ctx.fx.rates[c][0] for c in a.fx_delta if c in ctx.fx.rates},
        **({"basis01": basis} if basis else {}), **({"fixings": a.fixings} if a.fixings else {}),
        **({"reported_pv_rub": reported, "pv_diff_rub": a.pv - reported} if reported is not None else {}),
        "flows": a.flows,
    }
    hessian = _hessian(a)
    return BondValuation(
        secid="", quantity=amount, valuation_date=bd, settlement=bd, horizon_date=horizon or bd,
        horizon_kind="value_date", price_basis="core", clean_pct=math.nan, accrued_rub=0.0, face_rub=spot,
        current_coupon_pct=None, dirty_rub=a.pv / amount if amount else math.nan, pv_rub=a.pv, ytm_pct=math.nan,
        macaulay_years=math.nan, modified_duration=math.nan, z_spread_bp=math.nan, spread_kind=product,
        dv01_rub=math.fsum(buckets.values()), buckets=buckets, spread01_rub=0.0,
        calculation_id=a.calc_ids[0] if len(a.calc_ids) == 1 else f"desk-derivatives:{bd}:{r.position_id}",
        curve_buckets=by_curve, hessian=hessian, quality="assumed" if why else "exact", assumptions=tuple(why),
        fx_delta={c: v for c, v in a.fx_delta.items() if v}, extra=extra)
