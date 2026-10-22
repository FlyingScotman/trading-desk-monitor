"""Day-over-day PnL of what core does not reprice (R10): FX rows, the FX part of a foreign-face bond, and the
positions valued at the report's figure. Each is flagged with its method (``reasons.METHOD_TEXT``).

- FX row (``valuation.positions._fx``), spot or cash at the market rate, or a forward without a reported value:
  today's amount x (rate D - rate D-1), the rate as the valuation takes it (``fallback.fx_rate``: the market rate,
  MOEX 19:00, else 14:00, else CETS close, else CBR; owner's decision of 21.10, R26), method ``desk-fx-spot-v1``, in
  ``fx_gross``; the same amount x the CBR move in ``fx_gross_cbr`` (the official basis). A forward gets its spot delta
  only (no forward points), said in its flag. RUB cash: 0.
- Foreign-face bond (``split_fx``, on its simple PnL): the FX delta of D-1 (PV D-1 in currency, as ``tdm_run``
  publishes ``fx_delta``) x the market rate move goes from ``other`` to ``fx_gross`` (x the CBR move:
  ``fx_gross_cbr``); the rest is the price move at today's rate plus the received flows. MOEX settles replacement
  bonds in RUB at the CBR rate of the settlement day T+1, set on D near the 19:00 market: valued at the CBR rate in
  force on D (set on D-1) the price and the rate moved a day apart and FX and other cancelled (E2E 20.10: large FX
  and other of opposite signs on a nearly flat market).
- Row valued at the report's figure (rung ``reported_value``: OTC, other, unmapped bonds, a forward with the
  report's mv_rub): today's reported PV - yesterday's for the same position_id, both from the reports (method
  ``desk-reported-v1``, flag «по отчёту»), all in ``other``. Trades of the day are inside it. None when the
  position had no reported value the day before, or the book's report was not renewed since then (its set is
  carried: the difference would be a false 0).

Anything else valued without core keeps ``NoPnl("valued_without_core")``. A missing PnL is a reason, never 0.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING, Any

from ..inbox.schema import norm_ccy
from ..valuation.fallback import SimplePnl, fx_rate, fx_rate_cbr
from .flash import NoPnl
from .reasons import CORE_FX_METHOD, FX_METHOD, REPORTED_METHOD

if TYPE_CHECKING:
    from ..marketdata.adapter import BondTerms
    from ..orchestrator import EodContext, Outcome
    from ..store import PositionInputs

REPORTED_FLAG = "по отчёту"
FORWARD_FLAG = "форвард: по дельте спота, без форвардных пунктов"


@dataclass(frozen=True)
class DirectPnl:
    """PnL of a position valued without core, with the fields of ``FlashPnl`` that the run publishes."""

    method: str
    prev_date: date
    as_of: date
    total: float
    fx_gross: float = 0.0
    other: float = 0.0
    fx_gross_cbr: float | None = None
    note: str = ""
    flag: str = ""
    carry: float = 0.0
    rolldown: float = 0.0
    rates: float = 0.0
    spread: float = 0.0
    identity_error: float = 0.0

    @property
    def explained(self) -> float:
        return self.fx_gross + self.other

    def as_tdm(self) -> dict[str, float]:
        return {"carry": 0.0, "rolldown": 0.0, "rates": 0.0, "spread": 0.0, "convexity": 0.0, "other": self.other,
                "fx_gross": self.fx_gross, "fx_funding": 0.0, "new_trades": 0.0,
                **({"fx_gross_cbr": self.fx_gross_cbr} if self.fx_gross_cbr is not None else {})}

    def extra(self) -> dict[str, Any]:
        return {"method": self.method, "prev_date": self.prev_date.isoformat(), "total": self.total,
                "note": self.note, **({"flag": self.flag} if self.flag else {})}


def split_fx(pnl: SimplePnl, prev: EodContext, ctx: EodContext, terms: BondTerms) -> SimplePnl:
    """``pnl`` with the FX revaluation of a foreign-face bond in ``fx_gross`` (out of ``other``); a RUB bond as is.
    Without a rate on either date the FX part stays in ``other``, said in the note."""
    ccy = terms.face_unit or terms.currency or "RUB"
    if ccy == "RUB":
        return pnl
    r0, r1 = fx_rate(prev, ccy), fx_rate(ctx, ccy)
    if r0 is None or r1 is None:
        return replace(pnl, note=f"{pnl.note}; no {ccy} rate on {prev.business_date if r0 is None else ctx.business_date}"
                       ": the FX revaluation stays in other")
    delta = pnl.v0 / r0[0]  # the FX delta of D-1 in currency (PV / rate, as tdm_run publishes fx_delta)
    fx = delta * (r1[0] - r0[0])
    c0, c1 = fx_rate_cbr(prev, ccy), fx_rate_cbr(ctx, ccy)
    fx_cbr = delta * (c1[0] - c0[0]) if c0 and c1 else None
    return replace(pnl, fx_gross=fx, other=pnl.other - fx, fx_gross_cbr=fx_cbr,
                   note=f"{pnl.note}; fx_gross = FX delta D-1 {delta:,.2f} {ccy} x ({r1[0]:g} {r1[1]} - {r0[0]:g} "
                        f"{r0[1]}) ({FX_METHOD})"
                        + (f"; at CBR {c1[0]:g} - {c0[0]:g}: {fx_cbr:,.2f}" if fx_cbr is not None else ""))


def _fx_row(prev: EodContext, ctx: EodContext, o: Outcome, x: dict[str, Any]) -> DirectPnl:
    v = o.valuation
    assert v is not None
    ccy, amount = norm_ccy(x.get("ccy") or "RUB"), v.quantity
    vd = x.get("value_date")
    vd = date.fromisoformat(vd) if isinstance(vd, str) else vd
    flag = FORWARD_FLAG if vd is not None and vd > ctx.business_date else ""
    if ccy == "RUB":
        return DirectPnl(FX_METHOD, prev.business_date, ctx.business_date, 0.0, note="RUB: no FX move")
    r0 = fx_rate(prev, ccy)
    if r0 is None:
        raise NoPnl("no_prev_fx_rate", f"{ccy} on {prev.business_date}")
    r1 = v.face_rub  # the rate the row was valued at (``positions._fx``)
    fx = amount * (r1 - r0[0])
    c0, c1 = fx_rate_cbr(prev, ccy), fx_rate_cbr(ctx, ccy)
    return DirectPnl(FX_METHOD, prev.business_date, ctx.business_date, fx, fx_gross=fx, flag=flag,
                     fx_gross_cbr=amount * (c1[0] - c0[0]) if c0 and c1 else None,
                     note=f"{amount:,.2f} {ccy} x ({r1:g} - {r0[0]:g} {r0[1]})")


def _reported(prev: EodContext, ctx: EodContext, o: Outcome, p: Outcome | None, carried: date | None) -> DirectPnl:
    v = o.valuation
    assert v is not None
    if carried is not None:
        raise NoPnl("report_carried", f"report valid {carried}, not after {prev.business_date}")
    if p is None or p.valuation is None or p.valuation.quality != "reported_value":
        raise NoPnl("no_prev_report", f"no reported value on {prev.business_date}")
    total = v.pv_rub - p.valuation.pv_rub
    return DirectPnl(REPORTED_METHOD, prev.business_date, ctx.business_date, total, other=total, flag=REPORTED_FLAG,
                     note=f"reported PV {v.pv_rub:,.2f} - {p.valuation.pv_rub:,.2f} RUB (trades of the day inside)")


def _core_fx(prev: EodContext, ctx: EodContext, o: Outcome, p: Outcome | None) -> DirectPnl:
    """A derivative valued by core on both dates: PV D minus PV D-1, of which the FX delta of D-1 per currency x the
    market rate move is ``fx_gross`` (x the CBR move: ``fx_gross_cbr``), the rest (rates, points, time) ``other``."""
    v = o.valuation
    assert v is not None
    pv = p.valuation if p is not None else None
    if pv is None or not pv.extra.get("core"):
        raise NoPnl("valued_differently", f"not valued by core from its terms on {prev.business_date}")
    total = v.pv_rub - pv.pv_rub
    fx, cbr, parts = 0.0, 0.0, []
    for ccy, delta in (pv.fx_delta or {}).items():
        r0, r1 = fx_rate(prev, ccy), fx_rate(ctx, ccy)
        if r0 is None or r1 is None:
            raise NoPnl("no_prev_fx_rate", f"{ccy} on {prev.business_date if r0 is None else ctx.business_date}")
        fx += delta * (r1[0] - r0[0])
        c0, c1 = fx_rate_cbr(prev, ccy), fx_rate_cbr(ctx, ccy)
        cbr = cbr + delta * (c1[0] - c0[0]) if c0 and c1 and cbr is not None else None  # type: ignore[operator]
        parts.append(f"{delta:,.2f} {ccy} x ({r1[0]:g} - {r0[0]:g})")
    return DirectPnl(CORE_FX_METHOD, prev.business_date, ctx.business_date, total, fx_gross=fx, other=total - fx,
                     fx_gross_cbr=cbr if pv.fx_delta else None,
                     note=f"PV {v.pv_rub:,.2f} - {pv.pv_rub:,.2f} RUB; fx_gross = FX delta D-1 "
                          + ("; ".join(parts) or "0"))


def attach(prev: EodContext, ctx: EodContext, direct: dict[str, Outcome], inputs: PositionInputs,
           prev_direct: dict[str, Outcome]) -> None:
    """Set ``pnl`` or ``no_pnl`` on each valued outcome of ``direct`` (``positions.plan(ctx, inputs).direct``).
    ``prev_direct``: the same plan on the previous date's inputs and context (the reported values of D-1)."""
    book = {pid: b for pid, b, _, _ in inputs.rows}
    for pid, o in direct.items():
        v = o.valuation
        if v is None:
            continue
        x = inputs.extra.get(pid) or {}
        kind = (x.get("asset_class") or "bond").lower()
        vd = inputs.valid_dates.get(book.get(pid, ""))
        try:
            if v.extra.get("core"):  # a derivative valued by core from its terms (valuation.derivatives)
                o.pnl = _core_fx(prev, ctx, o, prev_direct.get(pid))  # type: ignore[assignment]
            elif v.quality == "reported_value":
                o.pnl = _reported(prev, ctx, o, prev_direct.get(pid),  # type: ignore[assignment]
                                  vd if vd is not None and vd <= prev.business_date else None)
            elif kind == "fx":
                o.pnl = _fx_row(prev, ctx, o, x)  # type: ignore[assignment]
            else:
                raise NoPnl("valued_without_core", f"{v.quality}: no day-over-day PnL")
        except NoPnl as e:
            o.no_pnl = e
