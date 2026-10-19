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
  else ``mark_price_pct`` x face (+ ``accrued_ccy``) at the CBR rate; no risk (DV01 and CS01 0). Nothing at all:
  not valued, reason ``unmapped_instrument``.
- FX row: ``amount_ccy`` x the rate of the business date (``fallback.fx_rate``; RUB at 1); its FX delta is the amount
  in currency, as a foreign-face bond's (``position_row``), no rates risk. ``value_date`` is shown, not discounted
  (S7a): quality ``market_value`` up to spot (T+2 business days), ``assumed`` for a forward.
- OTC and other rows: not valued, reason ``asset_class_not_supported_s7a``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

from rub_fixed_income import RISK_BUCKETS

from ..inbox.schema import load_schema, norm_ccy
from ..orchestrator import EodContext, Outcome
from ..store import PositionInputs
from .bonds import BondValuation, face_on
from .fallback import fx_rate

MISMATCH_BONDS = 0.5
SPOT_DAYS = 2
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


def _valuation(ctx: EodContext, kind: str, qty: float, pv: float, quality: str, why: list[str], *,
               price: float = math.nan, accrued: float = 0.0, face: float = math.nan,
               horizon: date | None = None) -> BondValuation:
    """A valuation without core: PV only, no risk."""
    bd = ctx.business_date
    return BondValuation(
        secid="", quantity=qty, valuation_date=bd, settlement=bd, horizon_date=horizon or bd,
        horizon_kind="value_date" if horizon else "unknown", price_basis=kind, clean_pct=price, accrued_rub=accrued,
        face_rub=face, current_coupon_pct=None, dirty_rub=pv / qty if qty else math.nan, pv_rub=pv, ytm_pct=math.nan,
        macaulay_years=math.nan, modified_duration=math.nan, z_spread_bp=math.nan, spread_kind=kind, dv01_rub=0.0,
        buckets=dict.fromkeys(RISK_BUCKETS, 0.0), spread01_rub=0.0, calculation_id=f"desk-{kind}:{bd}",
        quality=quality, assumptions=tuple(why))


def _source_id(x: dict[str, Any]) -> str:
    return next((str(x[k]) for k in ("source_instrument_id", "isin", "regnum", "moex_secid", "source_instrument_name")
                 if x.get(k)), "?")


def _reported(ctx: EodContext, qty: float | None, x: dict[str, Any], reason: str, why: list[str]) -> Outcome:
    """Rung ``reported_value``: the bank's figure for a bond we cannot value ourselves."""
    o = Outcome(None, None, None, reason=reason)  # type: ignore[arg-type]
    q = qty if qty is not None else 1.0
    if x.get("mv_rub") is not None:
        o.valuation = _valuation(ctx, "reported", q, x["mv_rub"], "reported_value",
                                 [*why, "reported_value: PV = the report's mv_rub, no risk"],
                                 price=x.get("mark_price_pct") or math.nan)
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
                + (f", x {fx[1]} {fx[0]:g}" if ccy != "RUB" else "") + ", no risk"]
        pv = (x["mark_price_pct"] / 100 * face + (acc or 0.0)) * fx[0]
        o.valuation = _valuation(ctx, "reported", q, pv, "reported_value", why, price=x["mark_price_pct"],
                                 accrued=(acc or 0.0) * fx[0], face=face * fx[0])
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
    forward = vd is not None and vd > ctx.calendar.add_business_days(ctx.business_date, SPOT_DAYS)
    why = [f"{amount:g} {ccy} x {fx[1]} {fx[0]:g}"] if ccy != "RUB" else []
    if forward:
        why.append(f"forward to {vd}: today's rate, not discounted (S7a)")
    o.valuation = _valuation(ctx, "fx", amount, amount * fx[0], "assumed" if forward else "market_value", why,
                             face=fx[0], horizon=vd)
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
            p.direct[pid] = Outcome(None, None, None, reason="asset_class_not_supported_s7a",  # type: ignore[arg-type]
                                    detail=kind)
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
