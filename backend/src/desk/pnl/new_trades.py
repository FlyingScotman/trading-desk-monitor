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
never zeros in place of a missing PnL). Only bonds valued by core or its fallback ladder (an ``Outcome`` with terms) carry
the PnL of the trades. A bond is measured and split inside its book and portfolio (``trades.apply_intraday``'s lot key), so a
lot sold out in one portfolio keeps its PnL there. A row valued at the report's figure has the trades inside the difference
of the reports' values, at their whole value (a purchase is not a gain): the bond's day is then not shown
(``no_pnl`` ``trades_in_report_value``, the flag ``x.trades_not_in_pnl``), and where some lots of the bond are valued with terms
the bond's whole PnL is on one of them and the others have none (``mixed_valuation``), so that it is counted once.
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
from .direct import REPORTED_METHOD, DirectPnl
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


SCALED_FIELDS = ("v0", "v_carry", "v_roll", "v_rates", "v_spread", "v1", "received_rub", "carry", "rolldown", "rates",
                 "spread", "other", "total", "identity_error", "rates_linear", "fx_gross", "fx_gross_cbr", "fx_funding",
                 "fx_funding_cbr", "fx_delta_ccy", "carry_coupon", "received_coupon_rub", "new_trades")
"""The amounts of a ``FlashPnl`` that are linear in the quantity: split between the lots by their share."""
PREV_LINEAR = ("pv_rub", "quantity", "dv01_rub", "cs01_rub", "ofz_spr01_rub", "risk", "ofz_ruonia_risk")
"""The keys of ``prev_inputs`` (``extra.prev``) that are position sums and split with the PnL; the per-bond ones
(prices, yield, accrued per bond) and ``curve_moves_bp`` (market moves) are the same for every lot."""


def _parts(v: Any, ws: list[float]) -> list[Any]:
    """``v`` (a number, or a nested dict / tuple of them) in parts by the shares ``ws``; the first part is the rest, so
    that the parts add up to ``v`` exactly."""
    if v is None or isinstance(v, bool | str):
        return [v] * len(ws)
    if isinstance(v, dict):
        per = {k: _parts(x, ws) for k, x in v.items()}
        return [{k: p[i] for k, p in per.items()} for i in range(len(ws))]
    if isinstance(v, tuple | list):
        per_t = [_parts(x, ws) for x in v]
        return [type(v)(p[i] for p in per_t) for i in range(len(ws))]
    if not isinstance(v, int | float):
        return [v] * len(ws)
    rest = [v * w for w in ws[1:]]
    return [v - math.fsum(rest), *rest]


def split_pnl(pnl: Any, ws: list[float]) -> list[Any]:
    """The PnL of a bond in parts for its lots by their shares ``ws`` (the first takes the rounding rest and keeps the
    trades and ``start_quantity``; the others point to it by ``pnl_on_row``): the amounts and the numeric breakdowns
    (``rates_linear_curves``, the position sums of ``prev``) times the share; what is not an amount of the position
    (rates, bp, prices per bond, the market's moves) is the same for each. The sum of the parts is the bond's PnL."""
    cols = {f: _parts(getattr(pnl, f), ws) for f in SCALED_FIELDS if getattr(pnl, f, None) is not None}
    cols["rates_linear_curves"] = _parts(pnl.rates_linear_curves, ws)
    cols["rates_linear_ofz"] = _parts(pnl.rates_linear_ofz, ws)
    prev = pnl.prev_inputs
    prev_parts = ([prev] * len(ws) if not prev else
                  [{**prev, **{k: p[i] for k, p in ((k, _parts(prev[k], ws)) for k in PREV_LINEAR if k in prev)}}
                   for i in range(len(ws))])
    return [replace(pnl, **{f: c[i] for f, c in cols.items()}, prev_inputs=prev_parts[i])
            for i in range(len(ws))]


def _core(o: Outcome | None) -> bool:
    """A row valued with terms (by core or its ladder): the rows whose PnL the trades split; not a row at the report's
    figure."""
    return o is not None and o.valuation is not None and o.terms is not None


def _covered(prev: EodContext, ctx: EodContext, o: Outcome, main_pid: str) -> SimplePnl:
    """A lot of a bond whose PnL is on another lot (``pnl_on_row``) without a split: none of its own."""
    return replace(_opened_today(prev, ctx, o, 0.0, []), start_quantity=None, trades=None, pnl_on_row=main_pid,
                   note=f"the bond was traded today: its PnL is on the lot {main_pid}")


def _cover(prev: EodContext, ctx: EodContext, o: Outcome, main_pid: str) -> DirectPnl:
    """The PnL of a lot at the report's figure of a bond whose whole PnL (the start of the day of all its lots and the
    trades) is on the lot ``main_pid``: a zero with the reason in the note, so that nothing is counted twice."""
    note = f"the bond was traded today: its PnL is on the lot {main_pid}"
    if isinstance(o.pnl, DirectPnl):
        return replace(o.pnl, total=0.0, other=0.0, fx_gross=0.0, fx_gross_cbr=None, fx_funding=0.0, fx_funding_cbr=None,
                       carry=0.0, rolldown=0.0, rates=0.0, spread=0.0, identity_error=0.0, flag="", other_reason=None,
                       report_from=None, new_trades=0.0, received_rub=None, note=note)
    return DirectPnl(REPORTED_METHOD, prev.business_date, ctx.business_date, 0.0, note=note)


NOT_IN_PNL = "trades_in_report_value"
"""``no_pnl.reason`` of a traded bond valued at the report's figure: its PnL is the difference of the reports' values, and
a purchase (or a sale) of the day is inside it at its whole value, not as a gain: the day's PnL of the bond is not
shown (``pnl.reasons.NO_PNL_TEXT``). Core cannot value the bond from its terms, so the trades' cost (a percent of a face
it does not know) cannot be taken off the difference reliably."""


def outside_total(applied: int, split: dict[str, Any] | None = None, *, no_prev_close: bool = False,
                  no_pnl_run: bool = False) -> dict[str, Any]:
    """The lineage's ``intraday_trades.outside_total``: what the day's trades did not put in the run's total, always the
    same keys. ``applied``: the trades in the book; ``no_prev_close`` / ``no_pnl_run``: no previous close, or the run
    asked for no PnL (then ``split`` is None and no trade is in a total); ``no_pnl``: positions whose start of the day has
    no PnL (their trades' PnL is beside it: ``pnl_rub``) and their trades, counted with the positions the bond's other
    lots without PnL; ``lost``: bonds at the report's figure and their trades (no PnL of the day); ``unpriced``: trades
    without a price, a rate or a valuation (0 in the total); ``unvalued``: trades of bonds not valued at all."""
    n = split or {}
    return {"no_prev_close": no_prev_close, "no_pnl_run": no_pnl_run, "trades": applied,
            "no_pnl": {"positions": n.get("no_pnl", 0), "trades": n.get("no_pnl_trades", 0),
                       "pnl_rub": n.get("no_pnl_rub", 0.0)},
            "lost": {"bonds": len(n.get("lost", ())), "trades": n.get("lost_trades", 0)},
            "unpriced": {"trades": n.get("flagged", 0)},
            "unvalued": {"trades": n.get("unvalued_trades", 0)}}


def attach(prev: EodContext, ctx: EodContext, start: PositionInputs, bp: Any, outcomes: list[Outcome],
           summary: dict[str, Any], carried: Any) -> dict[str, Any]:
    """Split the PnL of the rows the day's trades changed (module doc), in place on ``outcomes`` (``bp``'s, after
    ``attach_pnl``): ``summary`` is ``trades.apply_intraday``'s. Returns the counts ``split`` / ``opened`` / ``no_pnl`` /
    ``flagged`` (and, for the lineage's ``outside_total``, ``no_pnl_trades`` / ``no_pnl_rub`` / ``lost_trades``)
    for the run's log and the anomalies: ``mixed_valuation`` (``book|bond`` whose lots are valued some by
    core and some at the report's figure: the PnL, the start of the day of all the lots and the trades, goes whole on one
    lot valued with terms; the others have none), ``lost`` (no lot valued with terms: the bond has no PnL, the trades are
    inside the reports' difference) and ``rows`` (their lots with the flag to put on each, ``x.trades_mixed_valuation`` / ``x.trades_not_in_pnl``)."""
    by_row = _by_row(summary.get("trades") or [])
    info = summary.get("rows") or {}
    main = dict(zip(bp.position_ids, outcomes))
    started = {p for p in by_row if (info.get(p) or {}).get("start")}
    begun = portfolio_pnl(prev, ctx, start, started, carried) if started else {}
    direct = getattr(bp, "direct", None) or {}
    n: dict[str, Any] = {"split": 0, "opened": 0, "no_pnl": 0, "flagged": 0, "mixed_valuation": [], "lost": [],
                         "rows": {}, "no_pnl_trades": 0, "no_pnl_rub": 0.0, "lost_trades": 0,
                         "unvalued_trades": 0}

    def outcome(m: str) -> Outcome | None:
        return main.get(m) or direct.get(m)

    def not_in_pnl(label: str, lots: list[str], count: int, start: dict[str, float] | None) -> None:
        """The lots of a traded bond valued at the report's figure: no PnL (the trades are inside the reports' difference
        at their whole value), said on the lots and in the run. Only the lots the trades changed (a different quantity
        than at the start of the day, new, or ``start`` unknown): a lot that is as it was has its reports' difference
        right. A lot that already has no PnL keeps its reason."""
        valued = [m for m in lots if (outcome(m) is not None and outcome(m).valuation is not None)]   # type: ignore[union-attr]
        if not valued:
            n["unvalued_trades"] += count   # the bond is not valued at all: its trades are in no PnL either
            return
        hit = [m for m in valued if start is None or start.get(m) != mem_q[m]]
        if not hit:
            return
        why = NoPnl(NOT_IN_PNL, "the bond was traded today and is valued at the report's figure: the trades are inside "
                                "the difference of the reports' values")
        for m in hit:
            o_ = outcome(m)
            assert o_ is not None
            if o_.no_pnl is None or o_.pnl is not None:
                o_.pnl, o_.no_pnl = None, why
        n["lost"].append(label)
        n["lost_trades"] += count
        n["rows"].update(dict.fromkeys(hit, "trades_not_in_pnl"))
    for pid, ts in by_row.items():
        mem = (info.get(pid) or {}).get("members") or {}
        mem_q = mem
        start_q = (info.get(pid) or {}).get("members_start")
        label = (f"{(info.get(pid) or {}).get('book')}|{(info.get(pid) or {}).get('bond')}"
                 + (f"|{(info.get(pid) or {})['portfolio']}" if (info.get(pid) or {}).get("portfolio") else ""))
        target = pid
        if mem:   # a book with its report: the lot that carries the bond's PnL is one valued with terms
            ok = [m for m in sorted(mem) if _core(main.get(m))]
            if len(ok) < len(mem) and ok:
                n["mixed_valuation"].append(label)
                n["rows"].update(dict.fromkeys(mem, "trades_mixed_valuation"))
            if not ok:
                not_in_pnl(label, sorted(mem), len(ts), start_q)
                continue
            target = pid if pid in ok else ok[0]
        o = main.get(target)
        if not _core(o):   # a row valued at the report's figure: the trades are inside its reports (D23)
            not_in_pnl(label, [pid], len(ts), None)
            continue
        assert o is not None and o.valuation is not None
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
            n["no_pnl_trades"] += len(ts)
            n["no_pnl_rub"] += nt
        if target not in mem:
            continue
        # the lots at the report's figure of a bond valued in different ways: the bond's whole PnL (the start of the
        # day of all its lots and the trades) is on the lot valued with terms, theirs would count it twice (D21)
        plain = [m for m in sorted(mem) if m != target and not _core(main.get(m)) and outcome(m) is not None
                 and outcome(m).valuation is not None]   # type: ignore[union-attr]
        for m in plain:
            o_ = outcome(m)
            assert o_ is not None
            o_.pnl, o_.no_pnl = ((_cover(prev, ctx, o_, target), None) if o.pnl is not None else (None, o.no_pnl))
        if o.pnl is None:   # these lots have no PnL either: counted with the bond's
            n["no_pnl"] += len(plain)
        lots = [m for m in mem if m != target and _core(main.get(m))]
        if not lots:
            continue
        if o.pnl is None:   # no PnL of the bond: none on its other lots either
            for m in lots:
                main[m].pnl, main[m].no_pnl = None, o.no_pnl
            continue
        if label in n["mixed_valuation"]:   # valued in different ways: not split, the whole on one lot
            for m in lots:
                main[m].pnl, main[m].no_pnl = _covered(prev, ctx, main[m], target), None
            continue
        order = [target, *sorted(lots)]   # the lot that keeps the trades first
        tot = math.fsum(mem[m] for m in order)
        if tot == 0:
            continue
        parts = split_pnl(o.pnl, [mem[m] / tot for m in order])
        for m, part in zip(order, parts):
            lot = main[m]
            lot.no_pnl = None
            lot.pnl = replace(part, quantity=lot.valuation.quantity) if m == target else replace(
                part, quantity=lot.valuation.quantity, trades=None, start_quantity=None, pnl_on_row=target)
    return n
