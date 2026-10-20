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
- FX row: its FX delta is always the amount in currency (``tdm_run.direct_row``), no rates risk. Spot or cash (no
  ``value_date``, or one not after the business date): PV = ``amount_ccy`` x the rate of the business date
  (``fallback.fx_rate``; RUB at 1), quality ``market_value``. A forward (``value_date`` after the business date: an
  NDF, an FX future, an unsettled leg): PV = the report's ``mv_rub`` (``reported_value``), else 0 (``assumed``, said
  in the assumptions); its amount counts in the open currency position, never in MV.
- OTC and other rows (and any asset class this module does not know): rung ``reported_value`` at the report's
  ``mv_rub``, with its risk (``reported_risk``): ``risk_dv01_rub`` in the rates bucket of ``RISK_BUCKETS`` whose pillar
  is nearest ``maturity_date`` (no maturity, or one already past: the shortest bucket, said in the assumptions), on
  the curve of the row's currency (``RUB OFZ`` for RUB, else ``USD`` and so on), ``risk_cs01_rub`` as CS01.
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
    the shortest bucket), on the curve of the row's currency (``bonds.ccy_curve``: the KBD for RUB, else ``USD``
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


def plan(ctx: EodContext, inputs: PositionInputs) -> BookPlan:
    """Requests for core and the outcomes of the rows valued here, in ``inputs.rows`` order."""
    p = BookPlan([], [], {}, [(pid, book) for pid, book, _, _ in inputs.rows])
    index = None
    for pid, _, secid, qty in inputs.rows:
        x = inputs.extra.get(pid)
        if x is None:  # a legacy row: a bond with secid and quantity
            p.requests.append((secid, qty))  # type: ignore[arg-type]
            p.position_ids.append(pid)
            continue
        x = normalised(x)
        qty = None if qty is None else float(qty)
        kind = (x.get("asset_class") or "bond").lower()
        if kind == "fx":
            p.direct[pid] = _fx(ctx, x)
            continue
        if kind != "bond":
            p.direct[pid] = _otc(ctx, qty, x, kind)
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
