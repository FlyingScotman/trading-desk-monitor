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
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

from rub_fixed_income import (
    RISK_BUCKETS,
    AccountingCategory,
    CalendarEntry,
    CoreRejection,
    FuturesQuote,
    FxCrossForward,
    FxCrossInstrument,
    FxForward,
    FxForwardInstrument,
    FxForwardLeg,
    FxFuturesContract,
    FxFuturesInstrument,
    Position,
    ValuationPolicy,
    joint_calendar,
    value_portfolio,
)
from rub_fixed_income import __version__ as CORE_VERSION

from ..curves.fx import FxOptions
from ..curves.rates import RUONIA_OIS_CURVE_ID
from .bonds import RISK_CURVES, BondValuation

PUBLISHED_FACTORS = ("IR|", "FX|")
"""Factors of the published Hessian (the Taylor's vocabulary, ``scenario.taylor``); the basis goes to ``basis01``."""
RATE_TOLERANCE = 1e-4
"""Relative difference of ``contract_rate`` and the amounts' rate said in the assumptions."""


class NotValued(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason, self.detail = reason, detail


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


@dataclass
class Request:
    """A position row valued here: its trades' legs, or a FORTS future."""

    position_id: str
    book_id: str
    row: dict[str, Any]
    """The positions row (``positions.normalised``)."""
    legs: list[TermsLeg] = field(default_factory=list)
    future: dict[str, Any] | None = None
    """The FORTS row of a future (``MarketView.future``) and ``contracts`` the signed number of contracts."""
    contracts: float | None = None


def link(rows: list[tuple[str, str, dict[str, Any]]], terms: list[dict[str, Any]]
         ) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """position_id -> its trade_terms rows (``position_id`` of the terms, else ``trade_id`` = the row's ``deal_id`` or
    ``position_id``, same book), for ``rows`` = (position_id, book_id, positions row) of the FX and OTC rows; and the
    terms rows no row names (``fx_unlinked_terms``)."""
    by_pid: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_trade: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in terms:
        if t.get("product_kind") not in ("fx_forward", "fx_swap"):
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
    unlinked = [t for ts in (*by_pid.values(), *by_trade.values()) for t in ts if id(t) not in used]
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


def _published(name: str) -> str:
    if name.startswith("IR|"):
        _, c, b = name.split("|")
        return f"IR|{RISK_CURVES.get(c, c)}|{b}"
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
        why = NotValued("no_rub_curve", f"no RUONIA OIS curve on {bd}: FX forwards keep the report's value")
        return {r.position_id: why for r in requests}, None
    curve_of, fallbacks = {}, {}
    for ccy in ctx.fx.spots:
        primary, alts = ctx.fx.fx_curve_ids(ccy, options)
        have = [i for i in (primary, *alts) if i in present]
        if have:
            curve_of[ccy] = primary
            fallbacks.update({(primary, a): None for a in alts if a in present})
    instruments, positions, quotes, owner, calendars = [], [], [], {}, {}
    terms_of: dict[str, TermsLeg | None] = {}
    notes: dict[str, list[str]] = defaultdict(list)
    for r in requests:
        built = []
        try:
            if r.future is not None:
                iid = f"{r.position_id}#fut"
                inst, ccy, q = _future(ctx, r, iid, curve_of)
                built.append((inst, ccy, None, r.contracts or 0.0))
                quotes.append(q)
            else:
                live = [t for t in r.legs if t.value_date >= bd]
                gone = [t for t in r.legs if t.value_date < bd]
                if gone:
                    notes[r.position_id].append("settled legs left out: " + ", ".join(t.label for t in gone))
                if not live:
                    raise NotValued("settled", "every leg settled before the business date")
                for n, t in enumerate(live):
                    iid = f"{r.position_id}#{n}"
                    inst, ccy, more = core_legs(ctx, t, iid, options, curve_of)
                    notes[r.position_id] += more
                    if isinstance(inst, FxCrossInstrument) and inst.calendar_id not in calendars:
                        calendars[inst.calendar_id] = _calendar_entry(
                            ctx, inst.calendar_id, (f"FX:{t.base}", f"FX:{t.quote}"))
                    built.append((inst, ccy, t, 1.0))
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
    used = {RUONIA_OIS_CURVE_ID, *curve_of.values(), *(a for _, a in fallbacks)}
    # risk on RUONIA in its OIS par quotes, as the bonds' (EodContext.quote_curves)
    snap = replace(ctx.risk_market, quotes=(), fixings=(), curves=tuple(c for c in ctx.risk_market.curves
                                                                         if c.curve_id in used),
                   calendars=(*ctx.core_market.calendars, *calendars.values()),
                   futures_quotes=tuple(quotes))
    policy = ValuationPolicy(RUONIA_OIS_CURVE_ID, include_second_order=second_order,
                             fx_curve_fallbacks=tuple(fallbacks))
    result = value_portfolio(run_id=f"desk-eod-{bd}-derivatives", as_of=ctx.core_market.known_at, snapshot=snap,
                             instruments=instruments, positions=positions, policy=policy, scenarios=scenarios)
    acc: dict[str, _Acc] = defaultdict(_Acc)
    for rej in result.coverage.rejections:
        acc[owner[rej.position_id].position_id].rejected.append((rej.code, rej.message))
    flags = {m.position_id: m.flags for m in result.position_models}
    for row in (*result.fx_forwards, *result.fx_crosses, *result.fx_futures):
        r, t = owner[row.position_id], terms_of[row.position_id]
        a = acc[r.position_id]
        a.pv += row.pv_rub
        a.calc_ids.append(row.calculation_id)
        for f, x in row.factor_risk.first_order_by_factor().items():
            a.first[_published(f)] += x
        for (fi, fj), x in row.factor_risk.hessian_by_factor().items():
            a.hessian[tuple(sorted((_published(fi), _published(fj))))] += x  # type: ignore[index]
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
        else:
            out[r.position_id] = _valuation(ctx, r, a, notes[r.position_id], result)
    return out, result


def _valuation(ctx: Any, r: Request, a: _Acc, notes: list[str], result: Any) -> BondValuation:
    bd, x = ctx.business_date, r.row
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
    # the bank's PV: the positions row's mv_rub, else the sum of its trades' pv_rub (trade_terms)
    reported = x.get("mv_rub")
    if reported is None and any(t.pv_rub is not None for t in r.legs):
        reported = math.fsum(t.pv_rub for t in r.legs if t.pv_rub is not None)  # type: ignore[misc]
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
    }
    hessian = [(i, j, v) for (i, j), v in sorted(a.hessian.items())
               if v and i.startswith(PUBLISHED_FACTORS) and j.startswith(PUBLISHED_FACTORS)]
    return BondValuation(
        secid="", quantity=amount, valuation_date=bd, settlement=bd, horizon_date=horizon or bd,
        horizon_kind="value_date", price_basis="core", clean_pct=math.nan, accrued_rub=0.0, face_rub=spot,
        current_coupon_pct=None, dirty_rub=a.pv / amount if amount else math.nan, pv_rub=a.pv, ytm_pct=math.nan,
        macaulay_years=math.nan, modified_duration=math.nan, z_spread_bp=math.nan, spread_kind=product,
        dv01_rub=math.fsum(buckets.values()), buckets=buckets, spread01_rub=0.0,
        calculation_id=a.calc_ids[0] if len(a.calc_ids) == 1 else f"desk-derivatives:{bd}:{r.position_id}",
        curve_buckets=by_curve, hessian=hessian, quality="assumed" if why else "exact", assumptions=tuple(why),
        fx_delta={c: v for c, v in a.fx_delta.items() if v}, extra=extra)
