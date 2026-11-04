"""The day's trades from the private adapter (S13a, TDM#525; 08 §10 ``trades``, private/1.6): what the monitors' screen
«Сделки дня» shows, and (TDM#557) the day's bond trades in the day's book (``apply_intraday``) and their PnL
(``pnl.new_trades``).

``trades`` is a flow: a date's trades are those of its own files, never carried forward (08 §6.7). The current row of
a ``trade_id`` is its highest ``trade_version`` over the date's cumulative exports and deltas (``store.dataset_rows``,
``store.versioned_rows``); a cancelled trade stays with its status, a late booking (``trade_date`` before the date)
is a trade of the date and is marked ``late``.

**The day's book** (``apply_intraday``, TDM#557): every run of date D (``cli.eod_run``: the autopilot's intraday and
morning steps, the preliminary and the final close, a backfill; setting ``intraday_trades``, on by default) values the
positions of D plus the day's trades. A book whose latest report is older than D (valid before D) is carried: its set
plus the trades of the days after the report (a late report) plus the trades of D. A book with the report of D (valid
D) has the report's positions and risk strictly; the trades of D then give the PnL only (``pnl.new_trades``), and what
the report differs from the start of the day plus the trades by is flagged on the row (``x.trades_report_gap``) and in
the lineage (``gaps``). A trade is every current trade of its date (``dataset_rows``) that is ``live``, a ``bond``, of
trade date D (a late booking or an amendment of an older trade is in the report already, or will be) and has a quantity
(``quantity`` in bonds, else ``face_amount_ccy``): it changes the row of its ``position_id`` in its book (else the row
of its book with its ``instrument_id``): ``buy`` adds, ``sell`` subtracts; the row's own figures of the report (face
amounts, market and book value, accrued, the report's risk) are scaled with the quantity. A trade of a bond the book
does not hold opens a row of its own (``position_id`` of the trade; it needs an ``instrument_id``). Anything else is
left out and counted by reason (``SKIP_REASONS``). The private inputs hash covers the trades applied (of D and of the
days before), so the run is made again when they change or the report of D comes: everything that compares the hash of
a run (the autopilot's steps, ``recompute``, ``backfill``, ``acquisition``) takes it from ``intraday_inputs``; the
run's lineage ``intraday_trades`` says what was applied. The PnL of a changed row is the start-of-day position's plus
the trades' own (``new_trades``, ``pnl.new_trades``): ``apply_intraday`` hands over, besides the lineage summary,
``trades`` (each applied trade of D with the row it hit, its price and accrued), ``rows`` (per changed row: start and
final quantity) and ``start_inputs`` (the positions at the start of D), which ``cli.eod_run`` takes out before the
lineage.

``view(con, on, book_id, days)`` is ``GET /api/v1/trades``: the date's current trades (optionally of one book), the
counts per book, the files read, and the last ``days`` dates that have trades for the screen's strip.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from . import store

FIELDS = ("trade_id", "trade_version", "status", "trade_date", "trade_ts", "book_id", "position_id", "asset_class",
          "source_instrument_id", "source_instrument_name", "isin", "regnum", "moex_secid", "instrument_id", "side",
          "quantity", "face_amount_ccy", "amount_ccy", "price_pct", "fx_rate", "accrued_ccy", "settle_date",
          "counterparty_id", "ccy")
"""The canonical fields of a ``trades`` row (contract/private/v1/trades.schema.json), in the screen's order."""
AMOUNTS = ("quantity", "face_amount_ccy", "amount_ccy", "price_pct", "fx_rate", "accrued_ccy")


def trade_dates(con: psycopg.Connection, on: date, n: int) -> list[date]:
    """The latest ``n`` business dates <= ``on`` with a stored ``trades`` set, newest first."""
    return [d for (d,) in con.execute(
        "SELECT DISTINCT valid_date FROM dataset_set WHERE dataset = 'trades' AND valid_date <= %s"
        " ORDER BY valid_date DESC LIMIT %s", (on, n))]


def _moscow_today() -> date:
    return datetime.now(UTC).astimezone(store.MSK).date()


SKIP_REASONS = ("cancelled", "not_bond", "other_trade_date", "no_quantity", "no_instrument", "units_mismatch",
                "other_book", "position_id_taken", "no_currency", "mixed_units")
"""Why a trade of the day is not in the day's book (``effect``; the screen shows them, the run counts them)."""
SCALED = ("face_amount_ccy", "face_outstanding_ccy", "mv_rub", "accrued_ccy", "amount_ccy",
          "risk_dv01_rub", "risk_cs01_rub")
"""The report's own figures of a position row that follow its quantity when trades change it."""
MAX_LISTED = 20000
"""Trades of the day the run's lineage lists one by one (versions taken in, reasons left out)."""
COST_BASIS = ("book_value_rub", "purchase_accrued_ccy")
"""Follow the quantity only when it falls (the average cost of what stays); a purchase has its own price, which the
row does not take in: the figures stay and the row says so (``x.intraday_cost_basis``)."""


def effect(r: dict[str, Any], on: date) -> str | None:
    """None when the trade ``r`` (a stored row) goes into the intraday book of ``on``, else the reason
    (``SKIP_REASONS``; ``no_instrument`` is decided by ``apply_intraday``, which knows the book's rows)."""
    if r.get("status") != "live":
        return "cancelled"
    if (r.get("asset_class") or "").lower() != "bond":
        return "not_bond"
    if str(r.get("trade_date")) != on.isoformat():
        return "other_trade_date"
    if r.get("quantity") is None and r.get("face_amount_ccy") is None:
        return "no_quantity"
    return None


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(Decimal(str(v)))
    except (ArithmeticError, ValueError):
        return None


def _times(t: dict[str, Any]) -> str:
    v = t.get("trade_ts")
    return v.isoformat() if hasattr(v, "isoformat") else str(v or "")


MAX_HISTORY_DAYS = 7
"""How many days back the trades of a book's days without a report are looked for (``apply_intraday``)."""
MAX_CUT_DAYS = 60
"""How far back the trades older than that window are counted for the lineage's ``history_cut`` (rows of the stored
sets, an upper bound)."""


def _trade_sets(con: psycopg.Connection, lo: date, hi: date) -> list[tuple[date, str, int]]:
    """(date, book, rows) of the stored ``trades`` sets in [lo, hi), oldest first: one aggregate query for the whole
    window, no row read. The rows are those of every stored set and revision (an upper bound of the trades)."""
    return [(d, str(b), int(n)) for d, b, n in con.execute(
        "SELECT valid_date, scope->>'book_id', sum(row_count) FROM dataset_set WHERE dataset = 'trades'"
        " AND valid_date >= %s AND valid_date < %s GROUP BY 1, 2 HAVING sum(row_count) > 0 ORDER BY 1, 2", (lo, hi))]


def _fold(rows: list, extra: dict[str, dict], books: set[str], d: date, trade_rows: list[dict[str, Any]]
          ) -> tuple[list[list], dict[str, dict], dict[str, Any]]:
    """The trades ``trade_rows`` of date ``d`` folded into copies of ``rows`` and ``extra`` (module doc): the trades
    are first summed per position row, then the row's figures are scaled once by final / initial quantity (so the
    order of the trades, a sale to zero and a later purchase, does not matter). Only the books in ``books`` take
    trades (else ``other_book``). A row that changes carries ``x.intraday_start_qty`` (or ``_face``): the quantity
    before the trades. Returns the rows, the extras and ``{"applied": [...], "skipped": Counter, "skip_ids": {...},
    "changed": {pid: {"mode", "start", "final", "new"}}, "new_positions": n}``."""
    rows = [list(r) for r in rows]
    extra = {pid: dict(x) for pid, x in extra.items()}
    taken = {r[0] for r in rows}
    by_pid = {(r[1], r[0]): i for i, r in enumerate(rows)}
    by_secid = {(r[1], r[2]): i for i, r in enumerate(rows) if r[2]}
    ccy_of: dict[str, str] = {}
    for r in rows:
        c = (extra.get(r[0]) or {}).get("ccy")
        if c and r[2]:
            ccy_of.setdefault(r[2], c)
    skipped: Counter = Counter()
    skip_ids: dict[str, str] = {}
    applied: list[dict] = []
    groups: dict[int, dict[str, Any]] = {}   # row index -> {"mode": "qty"|"face", "d": float, "new": bool}

    def skip(t: dict, why: str) -> None:
        skipped[why] += 1
        skip_ids[str(t.get("trade_id"))] = why

    for t in sorted(trade_rows, key=lambda t: (_times(t), str(t.get("trade_id")))):
        why = effect(t, d)
        if why:
            skip(t, why)
            continue
        book, sign = str(t["book_id"]), 1.0 if t.get("side") == "buy" else -1.0
        if book not in books:
            skip(t, "other_book")
            continue
        qty, face = _num(t.get("quantity")), _num(t.get("face_amount_ccy"))
        mode = "qty" if qty is not None else "face"
        amount = qty if qty is not None else face
        if amount is None:
            skip(t, "no_quantity")
            continue
        i = by_pid.get((book, str(t.get("position_id"))))
        if i is None and t.get("instrument_id"):
            i = by_secid.get((book, t["instrument_id"]))
        if i is None:
            if not t.get("instrument_id"):
                skip(t, "no_instrument")
                continue
            pid = str(t["position_id"])
            if pid in taken:
                skip(t, "position_id_taken")   # the id of a row of another book: its figures must not be replaced
                continue
            ccy = ccy_of.get(t["instrument_id"])   # the bond's own currency, as another row of the set has it
            if mode == "face" and (not ccy or t.get("ccy") != ccy):   # a face amount needs it; a quantity does not:
                skip(t, "no_currency" if not ccy else "units_mismatch")  # the core takes it from the reference
                continue
            rows.append([pid, book, t["instrument_id"], 0.0 if mode == "qty" else None])
            x = {"asset_class": "bond", **({"ccy": ccy} if ccy else {}), "x": {"intraday_trade": True}}
            for k in ("isin", "source_instrument_id", "source_instrument_name"):
                if t.get(k) is not None:
                    x[k] = t[k]
            if mode == "face":
                x["face_amount_ccy"] = 0.0
            extra[pid] = x
            taken.add(pid)
            i = by_pid[(book, pid)] = by_secid[(book, t["instrument_id"])] = len(rows) - 1
            groups[i] = {"mode": mode, "d": 0.0, "new": True}
        else:
            x = extra.get(rows[i][0])
            row_mode = "qty" if rows[i][3] is not None else "face"
            if mode != row_mode or (row_mode == "face" and (
                    x is None or x.get("face_amount_ccy") is None or not t.get("ccy") or x.get("ccy") != t.get("ccy"))):
                skip(t, "units_mismatch")
                continue
            groups.setdefault(i, {"mode": mode, "d": 0.0, "new": False})
        groups[i]["d"] += sign * amount
        applied.append({**{k: t.get(k) for k in ("trade_id", "trade_version", "book_id", "position_id", "side",
                                                 "quantity", "face_amount_ccy", "instrument_id", "price_pct",
                                                 "accrued_ccy", "ccy", "settle_date")}, "row": rows[i][0]})
    drop: set[int] = set()
    changed: dict[str, dict[str, Any]] = {}
    new_positions = 0
    for i, g in groups.items():
        pid = rows[i][0]
        x = extra.get(pid)
        qty_mode = g["mode"] == "qty"
        old = float(rows[i][3] or 0.0) if qty_mode else float(x.get("face_amount_ccy") or 0.0)
        final = old + g["d"]
        changed[pid] = {"mode": g["mode"], "start": old, "final": final, "new": g["new"]}
        if g["new"]:
            if abs(final) < 1e-9:
                drop.add(i)
                continue
            new_positions += 1
        elif x is not None and old != 0:
            k = final / old
            x["x"] = {**(x.get("x") or {}), "intraday_trade": True,
                      "intraday_start_" + ("qty" if qty_mode else "face"): old}
            for f in SCALED:
                if x.get(f) is not None:
                    x[f] = float(x[f]) * k
            for f in COST_BASIS:
                if x.get(f) is not None and k <= 1:
                    x[f] = float(x[f]) * k
                elif x.get(f) is not None:
                    x["x"]["intraday_cost_basis"] = "stale"
        elif x is not None:
            x["x"] = {**(x.get("x") or {}), "intraday_trade": True,
                      "intraday_start_" + ("qty" if qty_mode else "face"): 0}
        if qty_mode:
            rows[i][3] = final
        else:
            x["face_amount_ccy"] = final  # type: ignore[index]
    if drop:   # a new row whose trades add up to nothing
        for i in drop:
            extra.pop(rows[i][0], None)
            changed.pop(rows[i][0], None)
        rows = [r for j, r in enumerate(rows) if j not in drop]
    return rows, extra, {"applied": applied, "skipped": skipped, "skip_ids": skip_ids, "changed": changed,
                         "new_positions": new_positions}


def _bond(r: Any, x: dict[str, Any] | None) -> tuple[str, str]:
    """(book, bond) of a row: its instrument, else its ISIN, else its own id; the lots of a bond share it."""
    return r[1], str(r[2] or (x or {}).get("isin") or r[0])


def _row_qty(r: Any, x: dict[str, Any] | None) -> tuple[str, float | None]:
    """The row's quantity as its inputs give it: (``qty``, number of bonds), else (``face``, face amount)."""
    if r[3] is not None:
        return "qty", float(r[3])
    f = None if x is None else x.get("face_amount_ccy")
    return "face", None if f is None else float(f)


def _zeroed(x: dict[str, Any]) -> dict[str, Any]:
    """The figures of a row that the report no longer lists, at zero (a position sold out)."""
    x = dict(x)
    for f in (*SCALED, *COST_BASIS):
        if x.get(f) is not None:
            x[f] = 0.0
    if x.get("face_amount_ccy") is not None:
        x["face_amount_ccy"] = 0.0
    return x


def apply_intraday(con: psycopg.Connection, on: date,
                   inputs: store.PositionInputs) -> tuple[store.PositionInputs, dict[str, Any]]:
    """The positions of ``on`` with the trades applied (module doc) and the summary for the run's lineage
    ``intraday_trades``: ``{"set_ids", "applied", "new_positions", "skipped": {reason: n}, "versions": {trade_id:
    version} of every trade taken in, "skipped_ids": {trade_id: reason} of every one left out, "history": the trades of
    earlier days of books without a report since, "gaps": {position_id: quantity} the report of ``on`` lacks or has over
    the start of the day plus the trades}``; above ``MAX_LISTED`` trades the lists are cut and ``truncated`` is true
    (the screen then says "no information" for the rest). Also the transient ``trades`` (each applied trade of ``on``
    with the row it hit, its price and accrued), ``rows`` (per changed row: start and final quantity) and
    ``start_inputs`` (the positions at the start of ``on``) for the PnL of the trades (``pnl.new_trades``).

    **Carried books** (their latest set valid before ``on``): the start of the day is the set plus the trades of
    the days after it before ``on`` (``history``: a report that is late); the positions of the run are that plus the
    trades of ``on``. **Books with the report of ``on``** (valid ``on``): the positions are the report's, strictly;
    the start of the day is the book's earlier set plus its days' trades, and the trades of ``on`` give the PnL only
    (``new_trades``). The difference of the report and start + trades is flagged on the row
    (``x.trades_report_gap``) and in ``gaps``; a traded row the report no longer lists is kept at zero (a row of
    its own, ``<position id>|closed`` when that id is taken: the history of a position by its id breaks there).
    Bonds whose lots are in different units (quantity and face amount) cannot be compared or measured: their trades
    are left out of the PnL, ``mixed_units`` and ``x.trades_mixed_units`` say so."""
    held = dict(inputs.valid_dates)
    rep = {b for b, v in held.items() if v >= on}
    day = store.dataset_rows(con, "trades", on)
    summary: dict[str, Any] = {"set_ids": sorted({u["set_id"] for u in day.units}), "applied": 0,
                               "new_positions": 0, "skipped": {}, "versions": {}, "skipped_ids": {}, "trades": [],
                               "rows": {}, "history": 0, "history_cut": {}, "mixed_units": [], "gaps": {}, "start_inputs": None}
    start_hash = None
    base_rows: list[list] = [list(r) for r in inputs.rows if r[1] not in rep]
    base_extra = {r[0]: inputs.extra[r[0]] for r in base_rows if r[0] in inputs.extra}
    bvalid = {b: v for b, v in held.items() if b not in rep}
    if rep and any(str(t.get("book_id")) in rep for t in day.rows):   # the start of the day of a book with its report
        # ``positions_for`` takes the latest set of each book valid on or before the date: the day before ``on`` gives
        # the set in force at the start of ``on`` (the report of ``on`` itself is not valid before ``on``)
        prev = store.positions_for(con, on - timedelta(days=1), books=sorted(rep))
        base_rows += [list(r) for r in prev.rows]
        base_extra.update(prev.extra)
        bvalid.update(prev.valid_dates)
        start_hash = prev.private_inputs_hash   # a corrected report of the day before changes the PnL of the trades
    hist: list[dict] = []
    live = {r[1] for r in base_rows}   # the books that hold positions: a closed or silent book sets no window
    lo = min((v for b, v in bvalid.items() if b in live), default=on) + timedelta(days=1)
    first = max(lo, on - timedelta(days=MAX_HISTORY_DAYS))
    sets = _trade_sets(con, max(lo, on - timedelta(days=MAX_CUT_DAYS)), on) if lo < on else []   # one aggregate query
    per_date: dict[date, dict[str, int]] = {}
    for d, b, n_ in sets:
        if b in bvalid and bvalid[b] < d:
            per_date.setdefault(d, {})[b] = n_
    cut: dict[str, dict[str, Any]] = {}
    for d, by_book in sorted(per_date.items()):
        if d < first:   # older than the window: not counted, said (``history_cut``), never silent
            for b, n_ in by_book.items():
                c = cut.setdefault(b, {"report": bvalid[b].isoformat(), "dropped": 0})
                c["dropped"] += n_
            continue
        past = store.dataset_rows(con, "trades", d)   # trades of the days between a report and ``on``: start of day
        if past.rows:
            base_rows, base_extra, res = _fold(base_rows, base_extra, {b for b, v in bvalid.items() if v < d}, d,
                                               past.rows)
            hist += res["applied"]
            summary["set_ids"] = sorted({*summary["set_ids"], *(u["set_id"] for u in past.units)})
    summary["history_cut"] = cut
    summary["history"] = len(hist)
    if not day.rows and not hist:
        return inputs, summary
    rows_f, extra_f, res = _fold(base_rows, base_extra, set(held), on, day.rows)
    applied, changed = res["applied"], res["changed"]
    summary.update(trades=[a for a in applied if a["row"] in changed], new_positions=res["new_positions"],
                   applied=len(applied), skipped=dict(sorted(res["skipped"].items())),
                   versions={str(a["trade_id"]): a["trade_version"] for a in applied[:MAX_LISTED]},
                   skipped_ids=dict(list(res["skip_ids"].items())[:MAX_LISTED]),
                   truncated=len(applied) > MAX_LISTED or len(res["skip_ids"]) > MAX_LISTED)
    if not applied and not hist:
        return inputs, summary
    # the rows of the run: carried books from the fold, books with a report as it is
    by_pid = {r[0]: r for r in rows_f}
    out_rows: list[tuple] = []
    out_extra = dict(inputs.extra)
    listed = {r[0] for r in inputs.rows}
    for r in inputs.rows:
        if r[1] in rep or r[0] not in by_pid:
            out_rows.append(r)
            continue
        out_rows.append(tuple(by_pid[r[0]]))
        if r[0] in extra_f:
            out_extra[r[0]] = extra_f[r[0]]
    for r in rows_f:   # a position the trades opened in a carried book
        if r[0] not in listed and r[1] not in rep:
            out_rows.append(tuple(r))
            out_extra[r[0]] = extra_f[r[0]]
    # a book with its report is compared and measured by bond, not by position id: the source keeps a row per lot, a
    # purchase of a bond held is a new lot of the report and a sale takes the first lot (FIFO), not the one the fold folds on
    start_rows = [list(r) for r in base_rows]
    start_extra = dict(base_extra)
    gaps: dict[str, float] = {}
    mixed: list[str] = []
    mixed_bonds: list[str] = []
    rows_out: dict[str, dict[str, Any]] = {p: c for p, c in changed.items() if by_pid[p][1] not in rep}
    traded = sorted({_bond(r, extra_f.get(r[0])) for r in rows_f if r[1] in rep and r[0] in changed}, key=str)
    for key in traded:
        lots_b = [r for r in base_rows if _bond(r, base_extra.get(r[0])) == key]
        lots_f = [r for r in rows_f if _bond(r, extra_f.get(r[0])) == key]
        lots_r = [r for r in inputs.rows if r[1] in rep and _bond(r, inputs.extra.get(r[0])) == key]
        modes = {_row_qty(r, ex.get(r[0]))[0] for rs, ex in ((lots_b, base_extra), (lots_f, extra_f),
                                                              (lots_r, inputs.extra)) for r in rs}
        if len(modes) != 1:   # lots in quantities and in face amounts: not comparable, so said, never silent
            pids_m = {r[0] for r in lots_f}
            mixed += [str(a["trade_id"]) for a in applied if a["row"] in pids_m]
            mixed_bonds.append(f"{key[0]}|{key[1]}")
            for r in lots_r:
                out_extra[r[0]] = {**out_extra.get(r[0], {}), "x": {**(out_extra.get(r[0], {}).get("x") or {}),
                                                                   "trades_mixed_units": True}}
            for p_ in pids_m:
                rows_out.pop(p_, None)
            continue
        mode = next(iter(modes))

        def total(rs: list, ex: dict) -> float:
            return math.fsum(_row_qty(r, ex.get(r[0]))[1] or 0.0 for r in rs)

        q0, q_fold, q_rep = total(lots_b, base_extra), total(lots_f, extra_f), total(lots_r, inputs.extra)
        book = key[0]
        if lots_r:   # the lot that keeps the trades: a lot of the start of the day, the smallest id, else the smallest
            starts = sorted(r[0] for r in lots_r if r[0] in {b_[0] for b_ in lots_b})
            anchor = starts[0] if starts else min(r[0] for r in lots_r)
        else:        # the report lacks the bond: one row at zero keeps its PnL
            anchor = (lots_b or lots_f)[0][0]
            if anchor in listed:
                anchor = f"{anchor}|closed"
            src = (lots_f or lots_b)[0]
            out_rows.append((anchor, book, src[2], 0.0 if mode == "qty" else None))
            out_extra[anchor] = _zeroed(extra_f.get(src[0]) or base_extra.get(src[0]) or {"asset_class": "bond"})
        gap = q_rep - q_fold
        if abs(gap) > 1e-6:
            gaps[f"{book}|{key[1]}"] = gap
            out_extra[anchor] = {**out_extra.get(anchor, {}), "x": {**(out_extra.get(anchor, {}).get("x") or {}),
                                                                   "trades_report_gap": gap}}
        pids_f = {r[0] for r in lots_f}
        for a in applied:
            if a["row"] in pids_f:
                a["row"] = anchor
        for p in pids_f:
            rows_out.pop(p, None)
        rows_out[anchor] = {"mode": mode, "start": q0, "final": q_rep, "final_fold": q_fold, "new": q0 == 0,
                            "report": True, "bond": str(key[1]), "book": book,
                            "members": {r[0]: _row_qty(r, inputs.extra.get(r[0]))[1] or 0.0 for r in lots_r}}
        # the start of the day of the bond for its PnL: its lots in one row
        start_rows = [r for r in start_rows if r not in lots_b]
        for r in lots_b:
            start_extra.pop(r[0], None)
        if lots_b:
            x0 = dict(base_extra.get(lots_b[0][0]) or {})
            if len(lots_b) > 1:   # the lots in one row: the figures that follow the quantity added up, else dropped
                for f in (*SCALED, *COST_BASIS):
                    vals = [(base_extra.get(r[0]) or {}).get(f) for r in lots_b]
                    if None in vals:
                        x0.pop(f, None)
                    else:
                        x0[f] = math.fsum(float(v) for v in vals)
            start_rows.append([anchor, book, lots_b[0][2], q0 if mode == "qty" else None])
            start_extra[anchor] = x0
    if mixed:   # the trades of a bond in lots of different units: not in the PnL of the trades, said
        drop_ids = set(mixed)
        kept = [a for a in applied if str(a["trade_id"]) not in drop_ids]
        skipped_m = dict(summary["skipped"])
        skipped_m["mixed_units"] = len(drop_ids)
        summary.update(applied=len(kept), skipped=dict(sorted(skipped_m.items())),
                       versions={str(a["trade_id"]): a["trade_version"] for a in kept[:MAX_LISTED]},
                       skipped_ids={**summary["skipped_ids"], **dict.fromkeys(sorted(drop_ids), "mixed_units")})
    summary["mixed_units"] = mixed_bonds
    summary.update(trades=[a for a in applied if a["row"] in rows_out and str(a["trade_id"]) not in set(mixed)],
                   rows=rows_out,
                   gaps=dict(list(gaps.items())[:MAX_LISTED]))
    summary["start_inputs"] = replace(inputs, rows=[tuple(r) for r in start_rows], extra=start_extra, valid_dates=bvalid)
    canon = json.dumps([inputs.private_inputs_hash, applied, hist, start_hash, cut], sort_keys=True, default=str, separators=(",", ":"))
    return replace(inputs, rows=out_rows, extra=out_extra,  # type: ignore[misc]
                   private_inputs_hash=hashlib.sha256(canon.encode()).hexdigest()), summary


def intraday_inputs(con: psycopg.Connection, day: date, settings: Any,
                    inputs: store.PositionInputs | None = None) -> tuple[store.PositionInputs, dict[str, Any] | None]:
    """The positions of ``day`` as an INTRADAY run values them: ``inputs`` (default ``store.positions_for``) with the
    day's trades applied, and the summary for the lineage. Unchanged, with None, when ``settings.intraday_trades`` is
    off or there are no positions. The one place that decides it: the run (``cli.eod_run``) and every comparison of an
    INTRADAY run's ``private_inputs_hash`` (autopilot, ``recompute``) go through it."""
    if inputs is None:
        inputs = store.positions_for(con, day)
    if not inputs.rows or not getattr(settings, "intraday_trades", True):
        return inputs, None
    return apply_intraday(con, day, inputs)


def _intraday_state(r: dict[str, Any], on: date, run: dict[str, Any] | None, enabled: bool) -> str:
    """What the screen says of a trade: ``applied`` only when the latest run of the date (any type, a close too) took
    exactly this version in (its lineage), else the reason it is out (``in_positions``: the book's report is as of the
    end of the day), ``pending`` (no run since it arrived) or ``off`` (the setting)."""
    why = effect(r, on)
    if why:
        return why
    if not enabled:
        return "off"
    if run is None:
        return "pending"
    tid = str(r.get("trade_id"))
    if tid in run["versions"]:
        return "applied" if str(run["versions"][tid]) == str(r.get("trade_version")) else "pending"
    return run["skipped_ids"].get(tid) or ("unknown" if run.get("truncated") else "pending")


def _trade_pnl(con: psycopg.Connection, run: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The PnL of each trade the run took in (``pnl.new_trades``): trade_id -> ``{pnl_rub, flag}``, from the run's
    positions (``pnl.extra.trades``, or ``no_pnl.trades`` for a position whose start of day has no PnL)."""
    if run is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for (ts,) in con.execute("SELECT COALESCE(pnl->'extra'->'trades', no_pnl->'trades') FROM run_position"
                             " WHERE run_id = %s AND (pnl->'extra'->'trades' IS NOT NULL OR no_pnl->'trades' IS NOT NULL)",
                             (run["run_id"],)):
        out.update({str(t["trade_id"]): t for t in ts})
    return out


def _row(r: dict[str, Any], on: date, run: dict[str, Any] | None = None, enabled: bool = True,
         pnl: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {f: r.get(f) for f in FIELDS}
    for f in AMOUNTS:
        if out[f] is not None:
            out[f] = float(Decimal(str(out[f])))
    out["late"] = out["trade_date"] is not None and out["trade_date"] < on.isoformat()
    out["intraday"] = _intraday_state(r, on, run, enabled)  # in the intraday book, or why not (TDM#557)
    p = (pnl or {}).get(str(r.get("trade_id"))) if out["intraday"] == "applied" else None
    out["pnl_rub"] = p["pnl_rub"] if p else None   # the trade's PnL from its price to the run's valuation (TDM#557)
    out["pnl_flag"] = p.get("flag") if p else None   # why it is 0 or assumed (pnl.new_trades.FLAGS)
    out["src"] = r.get("_src")
    if r.get("x"):
        out["x"] = r["x"]
    return out


def view(con: psycopg.Connection, on: date | None = None, book_id: str | None = None,
         days: int = 20, intraday_trades: bool = True) -> dict[str, Any]:
    """The trades screen of ``on`` (default: the latest date with trades, else today, Moscow), of ``book_id`` only when
    given: see the module docstring. Amounts as the contract has them (unsigned; ``side`` gives the direction)."""
    if on is None:
        latest = trade_dates(con, _moscow_today(), 1)
        on = latest[0] if latest else _moscow_today()
    t = store.dataset_rows(con, "trades", on, scope={"book_id": book_id} if book_id else None)
    run = _intraday_run(con, on)
    pnl = _trade_pnl(con, run)
    rows = [_row(r, on, run, intraday_trades, pnl) for r in t.rows]
    books = {b["book_id"]: b for b in store.current_books(con, on)}
    per_book: dict[str, Counter] = {}
    for r in rows:
        c = per_book.setdefault(str(r["book_id"]), Counter())
        c["trades"] += 1
        c[r["status"]] += 1
    strip = []
    for d in trade_dates(con, on, days):  # a small screen's strip: the current trades per date
        day = store.dataset_rows(con, "trades", d)
        strip.append({"date": d.isoformat(), "trades": len(day.rows),
                      "cancelled": sum(1 for r in day.rows if r.get("status") == "cancelled")})
    return {
        "date": on.isoformat(), "book_id": book_id,
        "summary": {"trades": len(rows), "live": sum(1 for r in rows if r["status"] == "live"),
                    "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
                    "late": sum(1 for r in rows if r["late"]), "books": len(per_book),
                    "versions": sum(1 for r in rows if (r["trade_version"] or 0) > 1)},
        "books": [{"book_id": b, "name": (books.get(b) or {}).get("name"), "trades": c["trades"], "live": c["live"],
                   "cancelled": c["cancelled"]} for b, c in sorted(per_book.items())],
        "rows": rows,
        "files": [{"scope": u["scope"], "source": u["source"], "report_date": u["report_date"].isoformat(),
                   "report_ts": u["report_ts"].isoformat() if u["report_ts"] else None,
                   "completeness": u["completeness"], "rows": u["row_count"], "file_name": u["file_name"]}
                  for u in t.units if not book_id or u["scope"].get("book_id") in (None, book_id)],
        "days": strip,
        "intraday": ({k: v for k, v in run.items() if k not in ("versions", "skipped_ids")}
                     | {"enabled": intraday_trades}) if run else None,
    }


def _intraday_run(con: psycopg.Connection, on: date) -> dict[str, Any] | None:
    """The latest complete run of ``on`` (an INTRADAY one, or a close: ``run_type`` ``EOD``) and what of the day's
    trades it took in (its lineage ``intraday_trades``), or None."""
    row = con.execute("SELECT run_id, cut_type, run_type, lineage->'intraday_trades' FROM run WHERE business_date = %s"
                      " AND status = 'complete' ORDER BY run_id DESC LIMIT 1", (on,)).fetchone()
    if not row:
        return None
    t = row[3] or {}
    return {"run_id": row[0], "cut_type": row[1], "run_type": row[2], "applied": t.get("applied", 0),
            "new_positions": t.get("new_positions", 0), "skipped": t.get("skipped", {}),
            "versions": t.get("versions", {}), "skipped_ids": t.get("skipped_ids", {}),
            "truncated": bool(t.get("truncated"))}
