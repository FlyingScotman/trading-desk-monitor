"""The PnL of a day with trades (TDM#557): the position of the start of the day plus the PnL of each trade of the day.

A position that was bought or sold today is measured in two parts, so that the day's PnL is right whatever was traded:

- **the start-of-day position** (``portfolio_pnl``): the quantity the book opened the day with, valued on the previous
  close and on today's valuation as any position (the exact flash steps, the factors, ``orchestrator.attach_pnl``). What
  was sold keeps the result from the previous close up to its sale (the move before the sale is in these factors, the
  whole day's); a position opened today has none.
- **each trade** (``trade_pnl``, the component ``new_trades``, no factors): a purchase from its price to today's
  valuation, ``n x dirty_now - cost``; a sale from today's valuation to its price, ``cost - n x dirty_now``. The cost is
  ``n x price_pct / 100 x face`` (today's face of one bond, in RUB at today's rate) plus the trade's accrued interest
  (``accrued_ccy``, a sum for the whole trade in its currency; without it ``n x`` today's accrued, flagged
  ``accrued_assumed``). A trade without a price is 0, flagged ``no_trade_price`` (its quantity still changes the
  position); a currency other than the bond's and RUB is 0, ``units_mismatch``. ``price_pct`` is a percent of the face
  whatever the settlement currency, so a replacement bond (a face in USD, settled in RUB) is priced as any: the cost
  is ``n x price_pct / 100 x face_rub``; its accrued in RUB is taken as it is, in the bond's currency at the rate
  implied by the valuation (``face_rub`` / the face in currency), so that no PnL of ``n x accrued x rate move`` appears.
  The trade's own settlement date is not used: its accrued is taken as given and the valuation's ``dirty_rub`` is the
  one of the quote's settlement (T+1), so a T+0 trade carries a day of accrued in its PnL (a known simplification).

``attach`` puts the two together on the outcomes of the run: a changed row's ``pnl`` is its start-of-day PnL with
``new_trades`` added to ``total`` and ``quantity`` the number of bonds after the trades (``extra.start_quantity``,
``extra.trades``). A start-of-day position without PnL keeps its ``no_pnl`` (the trades' PnL in ``no_pnl.new_trades``,
never zeros in place of a missing PnL). Only bonds valued by core or its fallback ladder (an ``Outcome`` with terms) are
split; a row valued at the report's figure has its trades inside the reports (``pnl.direct``).
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

from ..marketdata.adapter import bond_currency, ccy_code
from ..orchestrator import EodContext, Outcome, attach_pnl, value
from ..store import PositionInputs
from ..valuation import positions as pv1
from ..valuation.bonds import BondValuation
from ..valuation.fallback import METHOD as SIMPLE_METHOD
from ..valuation.fallback import SimplePnl, fx_rate
from .flash import NoPnl

FLAGS = ("no_trade_price", "units_mismatch", "no_valuation", "no_fx_rate", "no_quantity", "accrued_assumed")
"""Why a trade's PnL is 0 (the first four and ``no_quantity``) or assumed (``accrued_assumed``): ``trades[].flag``."""


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _finite(*vs: float | None) -> bool:
    return all(v is not None and math.isfinite(v) for v in vs)


def trade_pnl(ctx: EodContext, secid: str, terms: Any, ref: BondValuation | None, t: dict[str, Any]) -> dict[str, Any]:
    """One trade ``t`` (an ``applied`` entry of ``trades.apply_intraday``) of the bond ``secid`` against today's valuation
    ``ref`` (per-bond figures: ``dirty_rub``, ``accrued_rub``, ``face_rub``): its entry of ``extra.trades``."""
    sign = 1.0 if t.get("side") == "buy" else -1.0
    qty, face = _num(t.get("quantity")), _num(t.get("face_amount_ccy"))
    out: dict[str, Any] = {k: t.get(k) for k in ("trade_id", "trade_version", "side", "price_pct", "accrued_ccy", "ccy")}
    for k in ("price_pct", "accrued_ccy"):
        out[k] = _num(out[k])
    unit = bond_currency(terms)
    tc = ccy_code(t["ccy"]) if t.get("ccy") else None
    ccy_ok = tc is None or tc in (unit, "RUB")   # the price is a percent of the face; the accrued is in the trade's
    n = None
    if ccy_ok:
        n, _ = pv1._bonds(ctx, secid, qty, {} if qty is not None else {"ccy": t.get("ccy"), "face_amount_ccy": face})
    out.update(quantity=n, pnl_rub=0.0, flag=None)
    if not ccy_ok:
        out["flag"] = "units_mismatch"
    elif n is None or not math.isfinite(n):
        out["flag"] = "no_quantity"
    elif out["price_pct"] is None:
        out["flag"] = "no_trade_price"
    elif ref is None or not _finite(ref.dirty_rub, ref.face_rub):
        out["flag"] = "no_valuation"
    else:
        acc_ccy = out["accrued_ccy"]
        rate = 1.0 if (tc == "RUB" or unit == "RUB") else _implied_rate(ctx, terms, unit, ref)
        if acc_ccy is not None and rate is None:
            out["flag"] = "no_fx_rate"
        else:
            if acc_ccy is not None:
                acc, flag = acc_ccy * rate, None  # type: ignore[operator]
            else:
                acc = n * (ref.accrued_rub if _finite(ref.accrued_rub) else 0.0)
                flag = "accrued_assumed"
            cost = n * out["price_pct"] / 100.0 * ref.face_rub + acc
            now = n * ref.dirty_rub
            out.update(pnl_rub=sign * (now - cost), cost_rub=cost, value_rub=now, flag=flag)
    return out


def _implied_rate(ctx: EodContext, terms: Any, unit: str, ref: BondValuation) -> float | None:
    """RUB per unit of the bond's currency as its valuation has it (``face_rub`` / the face in currency), else the market
    rate: the accrued of a trade is converted as the face is, so the cost has no PnL of a rate move."""
    try:
        face = float(pv1.face_on(terms, ctx.business_date))
    except Exception:   # noqa: BLE001 - no face: the market rate
        face = 0.0
    if face > 0 and math.isfinite(ref.face_rub):
        return ref.face_rub / face
    r = fx_rate(ctx, unit)
    return None if r is None else r[0]


def _by_row(trades: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        out.setdefault(t["row"], []).append(t)
    return out


def portfolio_pnl(prev: EodContext, ctx: EodContext, start: PositionInputs, pids: set[str],
                  carried: Any) -> dict[str, Outcome]:
    """The rows ``pids`` of ``start`` (the positions before the day's trades) valued on ``ctx`` and ``prev`` and their
    PnL attached (``orchestrator.attach_pnl``, the factors in full), by position id. A row valued without core is not
    in it."""
    rows = [r for r in start.rows if r[0] in pids]
    sub = replace(start, rows=rows, extra={p: x for p, x in start.extra.items() if p in pids},
                  trade_terms=[], trade_terms_units=[])
    bp = pv1.plan(ctx, sub, second_order=False)
    if not bp.requests:
        return {}
    now = value(ctx, bp.requests, position_ids=bp.position_ids, carried_spreads=carried)
    before = value(prev, bp.requests, position_ids=bp.position_ids, carried_spreads=carried)
    attach_pnl(prev, before, ctx, now)
    return dict(zip(bp.position_ids, now))


def _opened_today(prev: EodContext, ctx: EodContext, o: Outcome, nt: float, res: list[dict[str, Any]]) -> SimplePnl:
    """The PnL of a position opened today: no components, the trades' only."""
    v = o.valuation
    assert v is not None
    return SimplePnl(
        secid=o.secid, quantity=v.quantity, prev_date=prev.business_date, as_of=ctx.business_date,
        prev_valuation_date=prev.business_date, valuation_date=v.valuation_date, z0_bp=v.z_spread_bp,
        z1_bp=v.z_spread_bp, v0=0.0, v_carry=0.0, v_roll=0.0, v_rates=0.0, v_spread=0.0, v1=0.0, received_rub=0.0,
        carry=0.0, rolldown=0.0, rates=0.0, spread=0.0, other=0.0, total=nt, identity_error=0.0, rates_linear=0.0,
        price_basis=(v.price_basis, v.price_basis), new_trades=nt, start_quantity=0.0, trades=res,
        note="position opened today: the PnL of its trades only (from the trade price to today's valuation)",
        method=SIMPLE_METHOD)


def attach(prev: EodContext, ctx: EodContext, start: PositionInputs, bp: Any, outcomes: list[Outcome],
           summary: dict[str, Any], carried: Any) -> dict[str, int]:
    """Split the PnL of the rows the day's trades changed (module doc), in place on ``outcomes`` (``bp``'s, after
    ``attach_pnl``): ``summary`` is ``trades.apply_intraday``'s. Returns counts for the run's log."""
    by_row = _by_row(summary.get("trades") or [])
    info = summary.get("rows") or {}
    main = dict(zip(bp.position_ids, outcomes))
    started = {p for p in by_row if (info.get(p) or {}).get("start")}
    begun = portfolio_pnl(prev, ctx, start, started, carried) if started else {}
    n = {"split": 0, "opened": 0, "no_pnl": 0, "flagged": 0}
    for pid, ts in by_row.items():
        o = main.get(pid)
        if o is None or o.valuation is None or o.terms is None:
            continue   # a row valued at the report's figure: the trades are inside its reports
        s = begun.get(pid)
        was_held = bool((info.get(pid) or {}).get("start"))
        ref = s.valuation if s is not None and s.valuation is not None else o.valuation
        res = [trade_pnl(ctx, o.secid, o.terms, ref, t) for t in ts]
        nt = math.fsum(r["pnl_rub"] for r in res)
        n["flagged"] += sum(1 for r in res if r["flag"] and r["flag"] != "accrued_assumed")
        if not was_held:
            o.pnl, o.no_pnl = _opened_today(prev, ctx, o, nt, res), None
            n["opened"] += 1
        elif s is not None and s.pnl is not None:
            o.pnl, o.no_pnl = replace(s.pnl, quantity=o.valuation.quantity, total=s.pnl.total + nt, new_trades=nt,
                                      start_quantity=s.valuation.quantity if s.valuation else None, trades=res), None
            n["split"] += 1
        else:   # the start-of-day position has no PnL: kept so, with the trades' PnL beside it
            o.pnl = None
            o.no_pnl = (s.no_pnl if s is not None and s.no_pnl is not None else o.no_pnl
                        or NoPnl("start_not_valued", "the position of the start of the day is not valued"))
            o.trades = {"new_trades": nt, "start_quantity": s.valuation.quantity if s and s.valuation else None,
                        "trades": res}
            n["no_pnl"] += 1
    return n
