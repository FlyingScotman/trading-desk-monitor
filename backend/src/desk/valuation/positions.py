"""Positions of the private contract v1 (slice S7a; 08 §6.1, §10 ``positions``) into a valuation plan and back.

``plan`` splits a run's ``PositionInputs``: a bond with a known instrument and a number of bonds becomes a
request for ``orchestrator.value`` (core, then the fallback ladder); everything else is valued here, without
core (``direct``). ``merge`` puts the two back in the inputs' order for ``desk_run.publish``.

- Instrument: the row's secid, else re-resolved from ``moex_secid``, ``isin``, ``regnum`` against the static
  snapshot of the run (08 §6.1: a matured bond is not in a later static); a key that names two bonds is not used.
- Number of bonds: ``quantity``, else ``face_amount_ccy`` / initial face, else ``face_outstanding_ccy`` / the face
  outstanding on the business date (``bonds.face_on``). Two figures more than half a bond apart: the quantity wins,
  with a ``quantity_face_mismatch`` note in the published assumptions.
- Unmapped bond (no instrument, or no number of bonds): rung ``reported_value``, the bank's own figure: ``mv_rub``,
  else ``mark_price_pct`` x face (+ ``accrued_ccy``) at the CBR rate; the report's risk (``reported_risk``) when it
  gives one, else none (DV01 and CS01 0). Nothing at all: not valued, reason ``unmapped_instrument``.
- FX or OTC row with its trade's terms (private/1.2 ``trade_terms``: an FX forward, NDF, FX swap or cross; since 1.4
  an IRS / OIS on RUONIA or the key rate, a REPO), or a FORTS FX future named by ticker (setting
  ``futures_by_ticker``): valued by core ``value_portfolio`` v3 (``valuation.derivatives``): PV, rates risk per curve
  (``RUB RUONIA``, the foreign curve, ``RUB KEY``), FX delta per currency (core's spot equivalent), basis, fixing
  status. When core cannot (no curve, no spot, a fixing missing, a CCS), the row keeps the rule below with the reason
  first in its assumptions.
- FX row without terms: its FX delta is the amount in currency (``tdm_run.direct_row``), no rates risk. Spot or cash
  (no ``value_date``, or one not after the business date): PV = ``amount_ccy`` x the rate of the business date
  (``fallback.fx_rate``; RUB at 1), quality ``market_value``. A forward (``value_date`` after the business date: an
  NDF, an FX future, an unsettled leg): PV = the report's ``mv_rub`` (``reported_value``), else 0 (``assumed``, said
  in the assumptions), flagged ``no_trade_terms`` (the notional as FX delta, undiscounted); its amount counts in the
  open currency position, never in MV.
- OTC and other rows (and any asset class this module does not know): rung ``reported_value`` at the report's
  ``mv_rub``, with its risk (``reported_risk``): ``risk_dv01_rub`` in the rates bucket of ``RISK_BUCKETS`` whose pillar
  is nearest ``maturity_date`` (no maturity, or one already past: the shortest bucket, said in the assumptions), on
  the curve of the row's currency (``RUB RUONIA`` for RUB, else ``USD`` and so on), ``risk_cs01_rub`` as CS01.
  Without ``mv_rub``: not valued, reason ``no_reported_value`` (in coverage, no fact).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from rub_fixed_income import RISK_BUCKETS, RISK_PILLARS

from ..inbox.schema import load_schema, norm_ccy
from ..orchestrator import EodContext, Outcome
from ..store import PositionInputs
from . import derivatives as dv
from .bonds import BondValuation, ccy_curve, face_on
from .fallback import fx_rate

MISMATCH_BONDS = 0.5
RESOLVE_KEYS = (("moex_secid", "secid"), ("isin", "isin"), ("regnum", "regnumber"))
"""(field of the row, field of the static instrument), in the order tried."""


@dataclass
class BookPlan:
    requests: list[tuple[str, float]]
    """(secid, number of bonds) for ``orchestrator.value``."""
    position_ids: list[str]
    """Aligned with ``requests``."""
    direct: dict[str, Outcome]
    """position_id -> outcome of a row valued without core (fx, unmapped bonds, otc/other)."""
    order: list[tuple[str, str]]
    """(position_id, book_id) of every input row, in ``inputs.rows`` order."""
    notes: dict[str, list[str]] = field(default_factory=dict)
    """position_id -> notes of a request (resolution, face, mismatch), added to its assumptions by ``merge``."""
    core_result: Any = None
    """The core result (v3) of the derivatives valued from their terms (``valuation.derivatives``), None without."""
    unlinked_terms: list[str] = field(default_factory=list)
    """``book|trade_id`` of the trade_terms trades no positions row names (not valued unless ``fx_unlinked_terms``)."""
    added: list[str] = field(default_factory=list)
    """Position IDs of the trades added with ``fx_unlinked_terms`` (rows of their own, in ``order`` and ``direct``)."""


def _index(ctx: EodContext) -> dict[str, dict[str, str | None]]:
    """Per resolve key, value -> secid (None when two instruments share the value)."""
    out: dict[str, dict[str, str | None]] = defaultdict(dict)
    for i in ctx.static.snapshot.data["instruments"]:
        for _, k in RESOLVE_KEYS:
            if v := i.get(k):
                out[k][v] = i["secid"] if out[k].get(v, i["secid"]) == i["secid"] else None
    return out


def _resolve(ctx: EodContext, index: dict, x: dict[str, Any]) -> tuple[str, str] | None:
    for f, k in RESOLVE_KEYS:
        if (v := x.get(f)) and (secid := index[k].get(v)):
            return secid, f"instrument resolved by {f} {v} on {ctx.static.snapshot.business_date}"
    return None


def _bonds(ctx: EodContext, secid: str, qty: float | None, x: dict[str, Any]) -> tuple[float | None, list[str]]:
    """The number of bonds of the row and the notes on how it was derived."""
    t = ctx.static.bonds[secid]
    figures: list[tuple[str, float]] = [("quantity", qty)] if qty is not None else []
    notes = []
    unit = t.face_unit or t.currency or "RUB"
    if x.get("ccy") and x["ccy"] != unit and (x.get("face_amount_ccy") is not None
                                              or x.get("face_outstanding_ccy") is not None):
        notes.append(f"face amounts in {x['ccy']}, the bond's face is in {unit}: not used")
    else:
        initial, when_initial = t.initial_face_value, "initial"
        if initial is None and x.get("face_amount_ccy") is not None:
            # Without the static field: today's face plus the redemptions, indexed for an OFZ-IN, so said.
            initial, when_initial = face_on(t, date.min), "initial (from the redemptions, no initial_face_value)"
        for f, face, when in (("face_amount_ccy", initial, when_initial),
                              ("face_outstanding_ccy", face_on(t, ctx.business_date), f"on {ctx.business_date}")):
            if x.get(f) is not None and face:
                figures.append((f, x[f] / float(face)))
                if qty is None and len(figures) == 1:
                    notes.append(f"number of bonds {figures[0][1]:g} = {f} {x[f]:g} / {when} face {face}")
    if not figures:
        return None, notes
    n = figures[0][1]
    notes += [f"quantity_face_mismatch: {figures[0][0]} gives {n:g} bonds, {f} {m:g}; {figures[0][0]} kept"
              for f, m in figures[1:] if abs(m - n) > MISMATCH_BONDS]
    return n, notes


def reported_risk(ctx: EodContext, x: dict[str, Any]) -> tuple[dict[str, dict[str, float]], float, list[str]]:
    """The report's own risk of a row valued without core: (bucket DV01 by curve, CS01, notes). ``risk_dv01_rub``
    goes whole into the bucket whose pillar is nearest the years to ``maturity_date`` (no maturity or a past one:
    the shortest bucket), on the curve of the row's currency (``bonds.ccy_curve``: RUONIA for RUB, else ``USD``
    and so on). Both are the bank's figures (approximate), so said in the notes."""
    buckets, notes = dict.fromkeys(RISK_BUCKETS, 0.0), []
    dv01, cs01 = x.get("risk_dv01_rub"), x.get("risk_cs01_rub")
    curve = ccy_curve(x.get("ccy") or "RUB")
    if dv01:
        mat = x.get("maturity_date")
        t = (mat - ctx.business_date).days / 365.25 if mat else None
        i = 0 if t is None or t <= 0 else min(range(len(RISK_PILLARS)), key=lambda k: abs(RISK_PILLARS[k] - t))
        buckets[RISK_BUCKETS[i]] = dv01
        where = (f"maturity {mat}" if t is not None and t > 0 else
                 f"maturity {mat} not after {ctx.business_date}: shortest bucket" if mat else
                 "no maturity_date: shortest bucket")
        notes.append(f"reported_risk: DV01 {dv01:g} RUB/bp of the report in bucket {RISK_BUCKETS[i]} of {curve} "
                     f"({where}), approximate")
    if cs01:
        notes.append(f"reported_risk: CS01 {cs01:g} RUB/bp of the report")
    return ({curve: buckets} if dv01 else {}), cs01 or 0.0, notes


def _valuation(ctx: EodContext, kind: str, qty: float, pv: float, quality: str, why: list[str], *,
               price: float = math.nan, accrued: float = 0.0, face: float = math.nan,
               horizon: date | None = None, risk: dict[str, dict[str, float]] | None = None,
               cs01: float = 0.0) -> BondValuation:
    """A valuation without core: PV, and only the risk given (``reported_risk``, bucket DV01 by curve)."""
    bd = ctx.business_date
    buckets = {b: math.fsum(bs[b] for bs in (risk or {}).values()) for b in RISK_BUCKETS}
    return BondValuation(
        secid="", quantity=qty, valuation_date=bd, settlement=bd, horizon_date=horizon or bd,
        horizon_kind="value_date" if horizon else "unknown", price_basis=kind, clean_pct=price, accrued_rub=accrued,
        face_rub=face, current_coupon_pct=None, dirty_rub=pv / qty if qty else math.nan, pv_rub=pv, ytm_pct=math.nan,
        macaulay_years=math.nan, modified_duration=math.nan, z_spread_bp=math.nan,
        spread_kind="credit" if cs01 else kind, dv01_rub=math.fsum(buckets.values()), buckets=buckets,
        spread01_rub=cs01, calculation_id=f"desk-{kind}:{bd}", curve_buckets=dict(risk or {}),
        quality=quality, assumptions=tuple(why))


def _source_id(x: dict[str, Any]) -> str:
    return next((str(x[k]) for k in ("source_instrument_id", "isin", "regnum", "moex_secid", "source_instrument_name")
                 if x.get(k)), "?")


def _reported(ctx: EodContext, qty: float | None, x: dict[str, Any], reason: str, why: list[str]) -> Outcome:
    """Rung ``reported_value``: the bank's figure for a bond we cannot value ourselves."""
    o = Outcome(None, None, None, reason=reason)  # type: ignore[arg-type]
    q = qty if qty is not None else 1.0
    by_curve, cs01, risk = reported_risk(ctx, x)
    if x.get("mv_rub") is not None:
        o.valuation = _valuation(ctx, "reported", q, x["mv_rub"], "reported_value",
                                 [*why, "reported_value: PV = the report's mv_rub" + ("" if risk else ", no risk"),
                                  *risk], price=x.get("mark_price_pct") or math.nan, risk=by_curve, cs01=cs01)
        return o
    f = next((f for f in ("face_outstanding_ccy", "face_amount_ccy") if x.get(f) is not None), None)
    ccy = x.get("ccy") or "RUB"
    if x.get("mark_price_pct") is not None and f is not None:
        if (fx := fx_rate(ctx, ccy)) is None:
            o.reason, o.detail = "no_fx_rate", f"{ccy} on {ctx.business_date}"
            return o
        face = x[f]
        if f == "face_amount_ccy":
            why.append("face_amount_ccy is the initial face: an amortization is not known")
        acc = x.get("accrued_ccy")
        why += [f"reported_value: PV = mark {x['mark_price_pct']:g}% x face {face:g} {ccy}"
                + (f" + accrued {acc:g}" if acc is not None else ", accrued unknown: 0")
                + (f", x {fx[1]} {fx[0]:g}" if ccy != "RUB" else "") + ("" if risk else ", no risk"), *risk]
        pv = (x["mark_price_pct"] / 100 * face + (acc or 0.0)) * fx[0]
        o.valuation = _valuation(ctx, "reported", q, pv, "reported_value", why, price=x["mark_price_pct"],
                                 accrued=(acc or 0.0) * fx[0], face=face * fx[0], risk=by_curve, cs01=cs01)
        return o
    o.reason, o.detail = reason.split(":")[0], f"{_source_id(x)}: no mv_rub, no mark price with a face"
    return o


def _fx(ctx: EodContext, x: dict[str, Any]) -> Outcome:
    o = Outcome(None, None, None)  # type: ignore[arg-type]
    ccy, amount, vd = (x.get("ccy") or "").upper(), x.get("amount_ccy"), x.get("value_date")
    if not ccy or amount is None:
        o.reason = "fx_row_without_ccy_or_amount"
        return o
    if (fx := fx_rate(ctx, ccy)) is None:
        o.reason, o.detail = "no_fx_rate", f"{ccy} on {ctx.business_date}"
        return o
    leg = f"{amount:g} {ccy}" + (f" x {fx[1]} {fx[0]:g}" if ccy != "RUB" else "")
    if vd is not None and vd > ctx.business_date:
        # A forward (NDF, FX future, an unsettled leg): the amount is an exposure, not a value. PV is the report's
        # mv_rub, else 0; the amount goes only into the FX delta (OCP) through ``tdm_run.direct_row``.
        if x.get("mv_rub") is not None:
            pv, quality = x["mv_rub"], "reported_value"
            why = [f"forward to {vd}: PV = the report's mv_rub; the amount {leg} is FX exposure only"]
        else:
            pv, quality = 0.0, "assumed"
            why = [f"forward to {vd}: no mv_rub in the report, PV assumed 0;"
                   + f" the amount {leg} is FX exposure only, not in MV"]
    else:
        pv, quality = amount * fx[0], "market_value"
        why = [leg] if ccy != "RUB" else []
    o.valuation = _valuation(ctx, "fx", amount, pv, quality, why, face=fx[0], horizon=vd)
    return o


def _otc(ctx: EodContext, qty: float | None, x: dict[str, Any], kind: str) -> Outcome:
    """An otc/other row: the report's mv_rub and risk (rung ``reported_value``), else not valued."""
    if x.get("mv_rub") is None:
        return Outcome(None, None, None, reason="no_reported_value",  # type: ignore[arg-type]
                       detail=f"{kind} {_source_id(x)}: no mv_rub")
    o = Outcome(None, None, None)  # type: ignore[arg-type]
    by_curve, cs01, risk = reported_risk(ctx, x)
    o.valuation = _valuation(ctx, "reported", qty if qty is not None else 1.0, x["mv_rub"], "reported_value",
                             [f"{kind}: PV = the report's mv_rub" + ("" if risk else ", no risk reported"), *risk],
                             risk=by_curve, cs01=cs01)
    return o


_PROPS = load_schema("positions")["properties"]  # type: ignore[index]
NUMERIC = tuple(k for k, v in _PROPS.items() if v.get("$ref") == "#/$defs/amount")
DATES = tuple(k for k, v in _PROPS.items() if v.get("$ref") == "#/$defs/date")


def normalised(x: dict[str, Any]) -> dict[str, Any]:
    """The row's fields as this module computes with them: amounts as float (a Decimal or a decimal string),
    dates as date, the currency upper case (``RUR`` is ``RUB``). Idempotent."""
    out = dict(x)
    for k in NUMERIC:
        if out.get(k) is not None:
            out[k] = float(out[k])
    for k in DATES:
        if isinstance(out.get(k), str):
            out[k] = date.fromisoformat(out[k])
    if isinstance(out.get("ccy"), str):
        out["ccy"] = norm_ccy(out["ccy"])
    return out


NO_TERMS = "no_trade_terms"
"""Reason of an FX forward row without its trade's terms: the report's value (or 0) and the notional as FX delta."""


def _derivatives(ctx: EodContext, inputs: PositionInputs, p: BookPlan, second_order: bool
                 ) -> dict[str, BondValuation | dv.NotValued]:
    """The FX and OTC rows valued by core (``valuation.derivatives``): those with trade_terms, a FORTS FX future by
    ticker (setting ``futures_by_ticker``), and with ``fx_unlinked_terms`` the trades no row names (added to the
    plan as rows of their own). Returns per position ID the valuation or why core did not value it."""
    opts = ctx.fx_options
    rows = []
    for pid, book, _, _ in inputs.rows:
        x = inputs.extra.get(pid)
        if x is not None and (x.get("asset_class") or "bond").lower() != "bond":
            rows.append((pid, book, normalised(x)))
    linked, unlinked = dv.link(rows, inputs.trade_terms)
    p.unlinked_terms = sorted({f"{t.get('book_id')}|{t.get('trade_id')}" for t in unlinked})
    reqs, out = [], {}
    qty = {pid: q for pid, _, _, q in inputs.rows}
    for pid, book, x in rows:
        if pid in linked:
            try:
                reqs.append(dv.request_of(pid, book, x, linked[pid]))
            except dv.NotValued as e:
                out[pid] = e
        elif opts.futures_by_ticker and (f := _future_row(ctx, x)) is not None:
            n = qty.get(pid) if qty.get(pid) is not None else x.get("amount_ccy")
            if n is None:
                out[pid] = dv.NotValued("no_quantity", f"future {f['secid']}: no number of contracts")
            else:
                reqs.append(dv.Request(pid, book, x, future=f, contracts=float(n)))
    if opts.unlinked_terms and unlinked:
        trades: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for t in unlinked:
            trades[(str(t.get("book_id")), str(t.get("trade_id")))].append(t)
        for (book, trade), ts in sorted(trades.items()):
            pid = f"{book}|{trade}"
            try:
                legs = [dv.TermsLeg.of(t) for t in ts]
            except dv.NotValued as e:
                p.unlinked_terms.append(f"{pid}: {e}")
                continue
            ccy = next((c for t in legs for c in (t.base, t.quote) if c != "RUB"), "RUB")
            x = {"asset_class": "fx", "ccy": ccy, "deal_id": trade, "source_instrument_id": trade,
                 "source_instrument_name": f"{legs[0].base}/{legs[0].quote} {legs[0].product} {trade}",
                 "value_date": max(t.value_date for t in legs)}
            inputs.extra[pid] = x
            p.order.append((pid, book))
            reqs.append(dv.Request(pid, book, x, legs))
            p.added.append(pid)
    valued, result = dv.value(ctx, reqs, options=opts, second_order=second_order)
    p.core_result = result
    return {**out, **valued}


def _future_row(ctx: EodContext, x: dict[str, Any]) -> dict[str, Any] | None:
    """The FORTS FX future a non-bond row names by ticker (``moex_secid``, else ``source_instrument_id``)."""
    for k in ("moex_secid", "source_instrument_id"):
        if x.get(k) and (f := ctx.market.future(str(x[k]))) is not None and f.get("is_fx"):
            return f
    return None


def _core_outcome(v: BondValuation, result: Any) -> Outcome:
    o = Outcome(None, None, None, valuation=v)  # type: ignore[arg-type]
    o.core_result = result
    return o


def _not_by_core(o: Outcome, e: dv.NotValued) -> Outcome:
    """A row core did not value from its terms: its earlier rule, the reason said."""
    note = f"trade terms not valued by core ({e.reason}{': ' + e.detail if e.detail else ''}): the report's rule"
    if o.valuation is not None:
        o.valuation = replace(o.valuation, assumptions=(note, *o.valuation.assumptions))
    o.reason = o.reason or e.reason
    o.detail = o.detail or e.detail
    o.core_not_valued = (e.reason, e.detail)
    return o


def plan(ctx: EodContext, inputs: PositionInputs, *, second_order: bool = True) -> BookPlan:
    """Requests for core and the outcomes of the rows valued here, in ``inputs.rows`` order (then the trades added
    with ``fx_unlinked_terms``). The FX and OTC rows with trade terms (or an FX future by ticker) are valued by core
    (``valuation.derivatives``); the others, and those core rejects, keep the rule of the module docstring."""
    p = BookPlan([], [], {}, [(pid, book) for pid, book, _, _ in inputs.rows])
    index = None
    by_core = _derivatives(ctx, inputs, p, second_order)
    for pid in p.added:
        v = by_core.get(pid)
        if isinstance(v, BondValuation):
            p.direct[pid] = _core_outcome(v, p.core_result)
        else:
            p.direct[pid] = Outcome(None, None, None, reason=v.reason if v else "not_valued",  # type: ignore[arg-type]
                                    detail=v.detail if v else "",
                                    core_not_valued=(v.reason, v.detail) if v else ("not_valued", ""))
    for pid, _, secid, qty in inputs.rows:
        x = inputs.extra.get(pid)
        if x is None:  # a legacy row: a bond with secid and quantity
            p.requests.append((secid, qty))  # type: ignore[arg-type]
            p.position_ids.append(pid)
            continue
        x = normalised(x)
        qty = None if qty is None else float(qty)
        kind = (x.get("asset_class") or "bond").lower()
        core = by_core.get(pid)
        if isinstance(core, BondValuation):
            p.direct[pid] = _core_outcome(core, p.core_result)
            continue
        if kind == "fx":
            o = _fx(ctx, x)
            if core is not None:
                o = _not_by_core(o, core)
            elif x.get("value_date") is not None and x["value_date"] > ctx.business_date and o.valuation is not None:
                o = _not_by_core(o, dv.NotValued(NO_TERMS, "no trade_terms row names this position: FX delta = "
                                                           "the notional, undiscounted"))
            p.direct[pid] = o
            continue
        if kind != "bond":
            o = _otc(ctx, qty, x, kind)
            if core is None and kind == "otc":  # the report's value only: listed by publish.derivatives_parallel
                o.core_not_valued = (NO_TERMS, "no trade_terms row names this position")
            p.direct[pid] = _not_by_core(o, core) if core is not None else o
            continue
        notes = []
        if secid is None or secid not in ctx.static.bonds:
            index = index or _index(ctx)
            hit = _resolve(ctx, index, x)
            if hit is None:
                why = [f"secid {secid} not in the static snapshot"] if secid else []
                p.direct[pid] = _reported(ctx, qty, x, f"unmapped_instrument: {_source_id(x)}", why)
                continue
            secid, note = hit
            notes.append(note)
        n, more = _bonds(ctx, secid, qty, x)
        if n is None:
            p.direct[pid] = _reported(ctx, None, x, f"no_quantity: {_source_id(x)} ({secid})", notes + more)
            continue
        p.requests.append((secid, n))
        p.position_ids.append(pid)
        if notes + more:
            p.notes[pid] = notes + more
    return p


def merge(plan: BookPlan, outcomes: list[Outcome]) -> list[tuple[str, str, Outcome]]:
    """(position_id, book_id, outcome) in ``plan.order``; ``outcomes`` are ``value``'s, in ``plan.requests``
    order. A request's notes go first in its assumptions, or in the detail of a bond that is not valued."""
    if len(outcomes) != len(plan.requests):
        raise ValueError("one outcome per request")
    by_pid = dict(zip(plan.position_ids, outcomes))
    for pid, notes in plan.notes.items():
        o = by_pid[pid]
        if o.valuation is not None:
            o.valuation = replace(o.valuation, assumptions=(*notes, *o.valuation.assumptions))
        else:
            o.detail = "; ".join([*notes, *([o.detail] if o.detail else [])])
    return [(pid, book, plan.direct[pid] if pid in plan.direct else by_pid[pid]) for pid, book in plan.order]
