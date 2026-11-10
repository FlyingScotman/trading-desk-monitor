"""The day's trades from the private adapter (S13a, TDM#525; 08 §10 ``trades``, private/1.6): what the monitors' screen
«Сделки дня» shows, and (TDM#557) the day's bond trades in the day's book (``apply_intraday``) and their PnL
(``pnl.new_trades``).

``trades`` is a flow: a date's trades are those of its own files, never carried forward (08 §6.7). The current row of
a ``trade_id`` is its highest ``trade_version`` over the date's cumulative exports and deltas (``store.dataset_rows``,
``store.versioned_rows``); a cancelled trade stays with its status, a late booking (``trade_date`` before the date)
is a trade of the date and is marked ``late``.

**The day's book** (``apply_intraday``, TDM#557): every run of date D (``dayrun.eod_run``: the autopilot's intraday and
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
final quantity) and ``start_inputs`` (the positions at the start of D), which ``dayrun.eod_run`` takes out before the
lineage.

``view(con, on, book_id, days)`` is ``GET /api/v1/trades``: the date's current trades (optionally of one book), the
counts per book, the files read, and the last ``days`` dates that have trades for the screen's strip. Each trade says
where it came from (``origin``, TDM#585: ``file`` a report folder, ``feed`` the adapter services' feed of the day,
``history`` the fetch of past dates, ``request`` a managed acquisition; ``origin_of``) and when the monitor received it
(``received_at``: the stored set's ``recorded_at``), with the book's legal entity, desk and portfolio for the header's
filter (``api.scope_rule``). ``brief(con, on, books, ...)`` is ``GET /api/v1/trades/summary``: the same trades counted
for the general screens (C01, C03, C05, C14) under the books of the header, without the rows.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from . import store
from .valuation import offreport

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

STATE_TEXT = {"applied": "в позициях", "cancelled": "отменена", "not_bond": "не облигация — в позиции дня пока не входит",
              "other_trade_date": "дата сделки другая — войдёт с отчётом позиций", "no_quantity": "нет количества",
              "no_instrument": "бумага не найдена", "units_mismatch": "количество и номинал не сопоставить",
              "other_book": "книги нет среди книг с позициями", "position_id_taken": "номер позиции занят другой книгой",
              "no_currency": "валюта бумаги неизвестна",
              "mixed_units": "лоты бумаги в разных единицах (штуки и номинал) — сделка не в PnL",
              "pending": "ждёт прогона", "unknown": "нет сведений о прогоне", "off": "расчёт по сделкам дня выключен"}
"""What the screens say of a trade's ``intraday`` state (``_intraday_state``): in the day's book, or why not. The
monitors keep the same words in ``prototypes/concepts/lib/kit.js`` (``K.TRADE_STATE``, for the demo); a test keeps them
equal."""
PROBLEMS = ("no_quantity", "no_instrument", "units_mismatch", "other_book", "position_id_taken", "no_currency",
            "mixed_units")
"""States of a live bond trade of the date that should have gone into the day's book and did not: the data flow marks
the arrow «сделки → позиции дня» incomplete (TDM#586)."""
ORIGINS = ("file", "feed", "history", "request")
ORIGIN_TEXT = {"file": "из папки с отчётами", "feed": "фид сервисов адаптера", "history": "догрузка прошлых дат",
               "request": "загрузка по запросу"}
"""Where a trade's file came from (``origin_of``, TDM#585)."""
_FEED_DIR = re.compile(r"[\\/]adapter-services[\\/]out[\\/][0-9a-f]{32}[\\/][^\\/]+$")
_REQUEST_DIR = re.compile(r"[\\/]acquisition[\\/][0-9A-Za-z_-]{8,64}[\\/][^\\/]+$")


def origin_of(path: str | None, recorded_at: datetime | None, valid_date: date | None, sha256: str | None = None,
              history: dict[str, set[str]] | None = None) -> str:
    """``file``, ``feed``, ``history`` or ``request`` (``ORIGINS``) of a stored set by the path its file was first seen
    at (``inbox_file.path``): a services feed writes into ``<home>/adapter-services/out/<run id>/`` (the feed of the day
    and the fetch of past dates alike), a managed acquisition into ``<home>/acquisition/<id>/``; anything else is a
    file of a report folder. Which of the feed's files the fetch of past dates brought is known exactly from its marks
    (``history``: per date the sha256 of the files it took in, ``history_files``): a file of a date with a mark is
    ``history`` when the mark lists it, else the feed of the day. Only a date without a mark (no marks file, a mark
    gone) falls back to the clock: a file of the feed recorded after its own business date (Moscow) is ``history``."""
    p = str(path or "")
    if _FEED_DIR.search(p):
        known = (history or {}).get(valid_date.isoformat()) if valid_date is not None else None
        if known is not None:
            return "history" if sha256 and sha256 in known else "feed"
        late = recorded_at is not None and valid_date is not None and recorded_at.astimezone(store.MSK).date() > valid_date
        return "history" if late else "feed"
    return "request" if _REQUEST_DIR.search(p) else "file"


def history_files(settings: Any) -> dict[str, set[str]] | None:
    """Per date the sha256 of the files the fetch of past dates took in (``trades_history`` marks, TDM#569), for
    ``origin_of``; None when the marks are not readable (then the clock decides)."""
    if settings is None:
        return None
    try:
        from . import trades_history  # at call time: it imports the autopilot

        marks = trades_history.read(settings).get("marks") or {}
    except Exception:  # noqa: BLE001 - no marks: the fallback of origin_of
        return None
    out: dict[str, set[str]] = {}
    for d, m in marks.items():
        files = (m.get("files") or []) if isinstance(m, dict) else []
        shas = {str(f["sha256"]) for f in files if isinstance(f, dict) and f.get("sha256")}
        if shas:   # a mark written before 0.2.20 lists its files without sha256: the clock decides for its date
            out[str(d)] = shas
    return out


def set_origins(con: psycopg.Connection, set_ids: list[int],
                history: dict[str, set[str]] | None = None) -> dict[int, dict[str, Any]]:
    """Per stored set: its ``origin`` (``origin_of``; ``history``: ``history_files``) and ``received_at`` (when the
    monitor stored it)."""
    out: dict[int, dict[str, Any]] = {}
    if not set_ids:
        return out
    for sid, at, vd, path, sha in con.execute(
            "SELECT s.set_id, s.recorded_at, s.valid_date, f.path, f.sha256 FROM dataset_set s"
            " JOIN inbox_file f ON f.sha256 = s.inbox_file_sha256 WHERE s.set_id = ANY(%s)", (sorted(set(set_ids)),)):
        out[sid] = {"origin": origin_of(path, at, vd, sha, history), "received_at": at.isoformat() if at else None}
    return out
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


class _AnyPortfolio:
    """The portfolio of a lot key that takes the bond whole, in every portfolio of the book: a sentinel that no real
    portfolio name equals."""

    def __repr__(self) -> str:
        return "<any portfolio>"


ANY = _AnyPortfolio()


def _portfolio(x: dict[str, Any] | None) -> str | None:
    """The report's Portfolio column (``x_portfolio``) of a row's extra, None for a row without it."""
    v = ((x or {}).get("x") or {}).get("x_portfolio")
    return None if v is None else str(v)


def _fold(rows: list, extra: dict[str, dict], books: set[str], d: date, trade_rows: list[dict[str, Any]],
          keep_flat: bool = False) -> tuple[list[list], dict[str, dict], dict[str, Any]]:
    """The trades ``trade_rows`` of date ``d`` folded into copies of ``rows`` and ``extra`` (module doc): the trades
    are first summed per position row, then the row's figures are scaled once by final / initial quantity (so the
    order of the trades, a sale to zero and a later purchase, does not matter). Only the books in ``books`` take
    trades (else ``other_book``). A row that changes carries ``x.intraday_start_qty`` (or ``_face``): the quantity
    before the trades. Returns the rows, the extras and ``{"applied": [...], "skipped": Counter, "skip_ids": {...},
    "changed": {pid: {"mode", "start", "final", "new"}}, "new_positions": n}``. A position the trades open
    carries the portfolio of its book (the trade's own ``x_portfolio``, else the book's when all its rows share one).
    ``keep_flat``: a position new today whose trades add up to nothing stays as a row at zero (the day's own trades,
    which carry a PnL); else it is dropped (the trades of earlier days: only the start of the day)."""
    rows = [list(r) for r in rows]
    extra = {pid: dict(x) for pid, x in extra.items()}
    taken = {r[0] for r in rows}
    by_pid = {(r[1], r[0]): i for i, r in enumerate(rows)}
    by_secid = {(r[1], r[2]): i for i, r in enumerate(rows) if r[2]}
    by_secid_pf = {(r[1], r[2], _portfolio(extra.get(r[0]))): i for i, r in enumerate(rows) if r[2]}
    inexact: set[str] = set()   # rows a trade hit by the bond alone, not knowing the lot's portfolio (see apply_intraday)
    ccy_of: dict[str, str] = {}
    for r in rows:
        c = (extra.get(r[0]) or {}).get("ccy")
        if c and r[2]:
            ccy_of.setdefault(r[2], c)
    book_pf: dict[str, set[str | None]] = {}
    for r in rows:   # the bonds' portfolios: an FX or OTC row of the book says nothing of where a bond belongs
        if ((extra.get(r[0]) or {}).get("asset_class") or "bond").lower() == "bond":
            book_pf.setdefault(r[1], set()).add(_portfolio(extra.get(r[0])))
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
            tpf = t.get("x_portfolio") or (t.get("x") or {}).get("x_portfolio")
            if tpf is not None:   # the trade says where it belongs: the last lot of the bond in that portfolio
                i = by_secid_pf.get((book, t["instrument_id"], str(tpf)))
            if i is None:
                i = by_secid.get((book, t["instrument_id"]))
                if i is not None:
                    inexact.add(rows[i][0])
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
            pf = t.get("x_portfolio") or (t.get("x") or {}).get("x_portfolio")
            if pf is None and len(book_pf.get(book, ())) == 1:
                pf = next(iter(book_pf[book]))
            if pf is not None:   # the portfolio filter must not lose a position the trades opened
                x["x"]["x_portfolio"] = str(pf)
            for k in ("isin", "source_instrument_id", "source_instrument_name"):
                if t.get(k) is not None:
                    x[k] = t[k]
            if mode == "face":
                x["face_amount_ccy"] = 0.0
            extra[pid] = x
            taken.add(pid)
            i = by_pid[(book, pid)] = by_secid[(book, t["instrument_id"])] = len(rows) - 1
            by_secid_pf[(book, t["instrument_id"], _portfolio(x))] = i
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
                if not keep_flat:
                    drop.add(i)
                    continue
            else:
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
                         "new_positions": new_positions, "inexact": inexact}


def _bond(r: Any, x: dict[str, Any] | None) -> tuple[str, str]:
    """(book, bond) of a row: its instrument, else its ISIN, else its own id; the lots of a bond share it."""
    return r[1], str(r[2] or (x or {}).get("isin") or r[0])


def _lot_key(r: Any, x: dict[str, Any] | None) -> tuple[str, str, str | None]:
    """(book, bond, portfolio) of a row: the lots of a bond in one portfolio share it. The PnL of a traded bond is
    measured and split inside this key, so that a lot sold out in one portfolio keeps its PnL there."""
    return (*_bond(r, x), _portfolio(x))


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
    The lots of a traded bond are taken by book, bond **and portfolio** (``x_portfolio``): the PnL of the trades and of
    the start of the day stays in the portfolio of the lot the trade hit, a lot sold out in one portfolio is a row at zero
    there. A bond bought and sold within the day (no row at the start, none in the report) is kept as a row at zero too,
    its PnL is the trades'.
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
    rows_f, extra_f, res = _fold(base_rows, base_extra, set(held), on, day.rows, keep_flat=True)
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
    # the lots of a traded bond are taken by portfolio. A trade that does not say its portfolio and hit a bond the book
    # holds in several portfolios (or in a portfolio of the report that is not the start's) is not known to be in the
    # row the fold chose: the bond is then taken whole, as ``ANY`` portfolio (book, bond), as it was before portfolios
    inexact = res["inexact"]
    by_bond: dict[tuple[str, str], list] = {}
    for r in rows_f:
        if r[1] in rep and r[0] in changed:
            by_bond.setdefault(_bond(r, extra_f.get(r[0])), []).append(r)
    keys: set[tuple[str, str, Any]] = set()
    for bk, rs in by_bond.items():
        pfs = ({_portfolio(base_extra.get(r[0])) for r in base_rows if _bond(r, base_extra.get(r[0])) == bk}
               | {_portfolio(inputs.extra.get(r[0])) for r in inputs.rows if r[1] in rep
                  and _bond(r, inputs.extra.get(r[0])) == bk})
        if len(pfs) > 1 and any(r[0] in inexact for r in rs):
            keys.add((*bk, ANY))
        else:
            keys |= {_lot_key(r, extra_f.get(r[0])) for r in rs}

    def of(key: tuple, r: Any, ex: dict) -> bool:
        return _bond(r, ex.get(r[0])) == key[:2] if key[2] is ANY else _lot_key(r, ex.get(r[0])) == key

    for key in sorted(keys, key=str):
        pf = None if key[2] is ANY else key[2]
        lots_b = [r for r in base_rows if of(key, r, base_extra)]
        lots_f = [r for r in rows_f if of(key, r, extra_f)]
        lots_r = [r for r in inputs.rows if r[1] in rep and of(key, r, inputs.extra)]
        if not lots_r and not any(_bond(r, base_extra.get(r[0])) == key[:2] for r in base_rows):
            # a bond that is new today: the report may put it in another portfolio than the book's
            lots_r = [r for r in inputs.rows if r[1] in rep and _bond(r, inputs.extra.get(r[0])) == key[:2]]
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
            if pf is not None:   # the row that keeps the PnL of a sold-out lot is in its portfolio
                zx = out_extra[anchor]
                out_extra[anchor] = {**zx, "x": {**(zx.get("x") or {}), "x_portfolio": pf}}
        gap = q_rep - q_fold
        if abs(gap) > 1e-6:
            gaps[f"{book}|{key[1]}" + (f"|{pf}" if pf is not None else "")] = gap
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
                            **({"portfolio": pf} if pf is not None else {}),
                            "members": {r[0]: _row_qty(r, inputs.extra.get(r[0]))[1] or 0.0 for r in lots_r},
                            "members_start": {r[0]: _row_qty(r, base_extra.get(r[0]))[1] for r in lots_b}}
        # the start of the day of the bond for its PnL: its lots in one row
        start_rows = [r for r in start_rows if r not in lots_b]
        for r in lots_b:
            start_extra.pop(r[0], None)
        if lots_b:
            # the figures of a lot with a quantity: the start row must be valued like the lots that have one
            src_b = next((r for r in lots_b if _row_qty(r, base_extra.get(r[0]))[1] is not None), lots_b[0])
            x0 = dict(base_extra.get(src_b[0]) or {})
            if len(lots_b) > 1:   # the lots in one row: the figures that follow the quantity added up, else dropped
                for f in (*SCALED, *COST_BASIS):
                    vals = [(base_extra.get(r[0]) or {}).get(f) for r in lots_b]
                    if None in vals:
                        x0.pop(f, None)
                    else:
                        x0[f] = math.fsum(float(v) for v in vals)
            start_rows.append([anchor, book, src_b[2], q0 if mode == "qty" else None])
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
    off or there are no positions. The one place that decides it: the run (``dayrun.eod_run``) and every comparison of an
    INTRADAY run's ``private_inputs_hash`` (autopilot, ``recompute``) go through it."""
    if inputs is None:
        inputs = store.positions_for(con, day)
    if not inputs.rows or not getattr(settings, "intraday_trades", True):
        out, summary = inputs, None
    else:
        out, summary = apply_intraday(con, day, inputs)
    # TDM#468: with the switch ``include`` the hash also says which trades off the report are in, today and on the day
    # before (the PnL of today is measured against it); every path here
    mode = getattr(settings, "off_report_derivatives", "exclude")
    prev, prev_day = None, None
    if mode == "include" and out.rows:
        prev_day = day - timedelta(days=1)
        while prev_day.weekday() > 4:  # the previous weekday: the run's previous market date, holidays apart
            prev_day -= timedelta(days=1)
        prev = store.positions_for(con, prev_day)
    return offreport.stamp(out, day, mode, prev, prev_day), summary


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
    for pid, ts in con.execute("SELECT position_id, COALESCE(pnl->'extra'->'trades', no_pnl->'trades') FROM run_position"
                               " WHERE run_id = %s AND (pnl->'extra'->'trades' IS NOT NULL OR no_pnl->'trades' IS NOT NULL)",
                               (run["run_id"],)):
        # the position the run put the trade in (by its position id, else by its bond in the book: ``_fold``)
        out.update({str(t["trade_id"]): {**t, "run_position_id": pid} for t in ts if isinstance(t, dict)})
    return out


def _row(r: dict[str, Any], on: date, run: dict[str, Any] | None = None, enabled: bool = True,
         pnl: dict[str, dict[str, Any]] | None = None, origins: dict[int, dict[str, Any]] | None = None,
         books: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    from .api.scope_rule import effective_portfolio  # at call time: desk.api imports the app

    out: dict[str, Any] = {f: r.get(f) for f in FIELDS}
    for f in AMOUNTS:
        if out[f] is not None:
            out[f] = float(Decimal(str(out[f])))
    out["late"] = out["trade_date"] is not None and out["trade_date"] < on.isoformat()
    out["intraday"] = _intraday_state(r, on, run, enabled)  # in the intraday book, or why not (TDM#557)
    p = (pnl or {}).get(str(r.get("trade_id"))) if out["intraday"] == "applied" else None
    out["pnl_rub"] = p["pnl_rub"] if p else None   # the trade's PnL from its price to the run's valuation (TDM#557)
    out["pnl_flag"] = p.get("flag") if p else None   # why it is 0 or assumed (pnl.new_trades.FLAGS)
    # the run's position the trade went into: its own position_id, or another row of its bond in the book (``_fold``
    # takes it by the bond when the ids differ); C03's «сд N» opens C20 on the trades of this position (TDM#585)
    out["run_position_id"] = p.get("run_position_id") if p else None
    out["src"] = r.get("_src")
    o = (origins or {}).get(r.get("_set_id")) or {}
    out["origin"] = o.get("origin")             # where the file came from (TDM#585, ``origin_of``)
    out["received_at"] = o.get("received_at")   # when the monitor stored it
    b = (books or {}).get(str(r.get("book_id"))) or {}
    out["entity"], out["desk"] = b.get("entity"), b.get("desk")   # for the header's filter (TDM#585)
    out["portfolio"] = effective_portfolio(b.get("book_type"), (r.get("x") or {}).get("x_portfolio"))
    if r.get("x"):
        out["x"] = r["x"]
    return out


def _day(con: psycopg.Connection, on: date, intraday_trades: bool, run_id: int | None,
         book_id: str | None = None, history: dict[str, set[str]] | None = None) -> tuple[store.DatasetInputs, dict[str, Any] | None, list[dict[str, Any]],
                                               dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """The trades of ``on`` as the screens show them: the stored rows, the run read, the rows of the screen, the origin
    of each set read and the books in force."""
    t = store.dataset_rows(con, "trades", on, scope={"book_id": book_id} if book_id else None)
    run = _intraday_run(con, on, run_id)
    pnl = _trade_pnl(con, run)
    origins = set_origins(con, [u["set_id"] for u in t.units], history)
    books = {b["book_id"]: b for b in store.current_books(con, on)}
    rows = [_row(r, on, run, intraday_trades, pnl, origins, books) for r in t.rows]
    return t, run, rows, origins, books


def _sources(rows: list[dict[str, Any]], units: list[dict[str, Any]], origins: dict[int, dict[str, Any]]
             ) -> list[dict[str, Any]]:
    """Per origin (``ORIGINS`` order): the trades whose current row came from it and when it last delivered a set of
    the date (any set read, also one whose trades all moved on to a newer version)."""
    out = []
    for k in ORIGINS:
        ats = [o["received_at"] for u in units if (o := origins.get(u["set_id"])) and o["origin"] == k
               and o["received_at"]]
        n = sum(1 for r in rows if r["origin"] == k)
        if n or ats:
            out.append({"origin": k, "text": ORIGIN_TEXT[k], "trades": n, "last_at": max(ats) if ats else None})
    return out


def _counts(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """The day's trades counted: live, cancelled, in the day's positions, by state (``STATE_TEXT``), the problems
    (``PROBLEMS``) and the time of the last live trade."""
    states = Counter(r["intraday"] for r in rows if r["intraday"])
    ts = [str(r["trade_ts"]) for r in rows if r.get("trade_ts") and r["status"] == "live"]
    return {"trades": len(rows), "live": sum(1 for r in rows if r["status"] == "live"),
            "cancelled": sum(1 for r in rows if r["status"] == "cancelled"),
            "applied": states.get("applied", 0),
            "states": [{"state": k, "text": STATE_TEXT.get(k, k), "n": n} for k, n in states.most_common()],
            "problems": sum(n for k, n in states.items() if k in PROBLEMS),
            "last_trade_ts": max(ts) if ts else None}


def view(con: psycopg.Connection, on: date | None = None, book_id: str | None = None,
         days: int = 20, intraday_trades: bool = True, run_id: int | None = None,
         history: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """The trades screen of ``on`` (default: the latest date with trades, else today, Moscow), of ``book_id`` only when
    given: see the module docstring. Amounts as the contract has them (unsigned; ``side`` gives the direction).
    ``history``: the files of the fetch of past dates (``history_files``, for ``origin_of``)."""
    if on is None:
        latest = trade_dates(con, _moscow_today(), 1)
        on = latest[0] if latest else _moscow_today()
    t, run, rows, origins, books = _day(con, on, intraday_trades, run_id, book_id, history)
    per_book: dict[str, Counter] = {}
    for r in rows:
        c = per_book.setdefault(str(r["book_id"]), Counter())
        c["trades"] += 1
        c[r["status"]] += 1
        c["applied"] += r["intraday"] == "applied"
    strip = []
    for d in trade_dates(con, on, days):  # a small screen's strip: the current trades per date
        day = store.dataset_rows(con, "trades", d)
        strip.append({"date": d.isoformat(), "trades": len(day.rows),
                      "cancelled": sum(1 for r in day.rows if r.get("status") == "cancelled")})
    counts = _counts(rows)
    return {
        "date": on.isoformat(), "book_id": book_id,
        "summary": {"trades": len(rows), "live": counts["live"], "cancelled": counts["cancelled"],
                    "late": sum(1 for r in rows if r["late"]), "books": len(per_book),
                    "versions": sum(1 for r in rows if (r["trade_version"] or 0) > 1)},
        "counts": counts,   # TDM#585: in the day's positions, left out by reason, the last trade
        "sources": _sources(rows, t.units, origins),   # TDM#585: per origin the trades and the last delivery
        "state_text": STATE_TEXT, "origin_text": ORIGIN_TEXT,
        "books": [{"book_id": b, "name": (books.get(b) or {}).get("name"), "entity": (books.get(b) or {}).get("entity"),
                   "desk": (books.get(b) or {}).get("desk"), "trades": c["trades"], "live": c["live"],
                   "cancelled": c["cancelled"], "applied": c["applied"]} for b, c in sorted(per_book.items())],
        "rows": rows,
        "files": [{"scope": u["scope"], "source": u["source"], "report_date": u["report_date"].isoformat(),
                   "report_ts": u["report_ts"].isoformat() if u["report_ts"] else None,
                   "completeness": u["completeness"], "rows": u["row_count"], "file_name": u["file_name"],
                   **{k: (origins.get(u["set_id"]) or {}).get(k) for k in ("origin", "received_at")}}
                  for u in t.units if not book_id or u["scope"].get("book_id") in (None, book_id)],
        "days": strip,
        "intraday": ({k: v for k, v in run.items() if k not in ("versions", "skipped_ids")}
                     | {"enabled": intraday_trades}) if run else None,
    }


TOP = 5
"""The largest trades of the day the general screens list (``brief``)."""
BRIEF_FIELDS = ("trade_id", "book_id", "position_id", "run_position_id", "trade_ts", "side", "asset_class",
                "source_instrument_id", "source_instrument_name", "quantity", "face_amount_ccy", "amount_ccy", "ccy",
                "price_pct", "intraday", "pnl_rub", "pnl_flag", "origin", "received_at")


def header_filter(entity: str | None = None, desk: str | None = None, books: list[str] | None = None,
                  ccy: str | None = None, ports_off: list[str] | None = None) -> dict[str, Any] | None:
    """The header's filter of the monitors as ``brief`` applies it to the trades (None: no filter): the same rule as
    the pages' ``K.scope.rowIn`` that C20 applies to each trade (TDM#566, #585): the book's legal entity and desk (of
    the books in force, as each trade row carries them), the books chosen, the trade's currency and its portfolio
    (``portfolio`` of the row: an HTM book's is HTM, else the trade's Portfolio column; a trade without a portfolio is
    not filtered by it). A trade of a book without positions (``other_book``) is counted by its own book's entity and
    desk, as C20 shows it."""
    f = {k: v for k, v in (("entity", entity), ("desk", desk), ("books", sorted(set(books)) if books else None),
                           ("ccy", ccy), ("ports_off", sorted(set(ports_off)) if ports_off else None)) if v}
    return f or None


def _in_filter(f: dict[str, Any] | None, r: dict[str, Any]) -> bool:
    if not f:
        return True
    return ((not f.get("entity") or r.get("entity") == f["entity"])
            and (not f.get("desk") or r.get("desk") == f["desk"])
            and (not f.get("books") or str(r.get("book_id")) in f["books"])
            and (not f.get("ccy") or r.get("ccy") == f["ccy"])
            and (not f.get("ports_off") or not r.get("portfolio") or r["portfolio"] not in f["ports_off"]))


def _unit_in_filter(f: dict[str, Any] | None, u: dict[str, Any], books: dict[str, dict[str, Any]]) -> bool:
    """A stored set under the filter's books (entity, desk, books; a set of no one book: always)."""
    bid = (u["scope"] or {}).get("book_id")
    if not f or bid is None:
        return True
    b = books.get(str(bid)) or {}
    return ((not f.get("entity") or b.get("entity") == f["entity"]) and (not f.get("desk") or b.get("desk") == f["desk"])
            and (not f.get("books") or str(bid) in f["books"]))


def brief(con: psycopg.Connection, on: date | None = None, books: list[str] | None = None,
          intraday_trades: bool = True, run_id: int | None = None, *, scope: dict[str, Any] | None = None,
          history: dict[str, set[str]] | None = None) -> dict[str, Any]:
    """``GET /api/v1/trades/summary`` (TDM#585): the day's trades for the general screens under the header's filter
    (``scope``: ``header_filter``; ``books`` alone: those books), without the rows: the counts (``_counts``), per
    origin the trades and the last delivery, per book its counts and last trade, and the ``TOP`` live trades of the
    largest PnL (by its size; then the latest without a PnL). The same filter gives the same trades as C20 shows.
    ``on``: default the date of ``run_id``, else the latest date with trades, else today (Moscow)."""
    if on is None and run_id is not None:
        r = con.execute("SELECT business_date FROM run WHERE run_id = %s AND status = 'complete'", (run_id,)).fetchone()
        on = r[0] if r else None
    if on is None:
        latest = trade_dates(con, _moscow_today(), 1)
        on = latest[0] if latest else _moscow_today()
    f = scope if scope is not None else header_filter(books=books)
    t, run, rows, origins, in_force = _day(con, on, intraday_trades, run_id, history=history)
    rows = [r for r in rows if _in_filter(f, r)]
    units = [u for u in t.units if _unit_in_filter(f, u, in_force)]
    per_book: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = per_book.setdefault(str(r["book_id"]), {"trades": 0, "applied": 0, "last_trade_ts": None})
        b["trades"] += 1
        b["applied"] += r["intraday"] == "applied"
        if r["status"] == "live" and r.get("trade_ts") and str(r["trade_ts"]) > str(b["last_trade_ts"] or ""):
            b["last_trade_ts"] = str(r["trade_ts"])
    live = [r for r in rows if r["status"] == "live"]
    top = sorted((r for r in live if r["pnl_rub"] is not None), key=lambda r: -abs(r["pnl_rub"]))[:TOP]
    top += sorted((r for r in live if r["pnl_rub"] is None), key=lambda r: str(r.get("trade_ts") or ""),
                  reverse=True)[:TOP - len(top)]
    return {"date": on.isoformat(), "books": (f or {}).get("books"), "filter": f,
            "run_id": run["run_id"] if run else None, **_counts(rows),
            "sources": _sources(rows, units, origins), "state_text": STATE_TEXT,
            "by_book": dict(sorted(per_book.items())),
            "top": [{k: r.get(k) for k in BRIEF_FIELDS} for r in top],
            "enabled": intraday_trades}


def _intraday_run(con: psycopg.Connection, on: date, run_id: int | None = None) -> dict[str, Any] | None:
    """The run the trades screen reads of ``on`` and what of the day's trades it took in (its lineage
    ``intraday_trades``), or None (TDM#568): the run asked for (``run_id``: the page's run of the navigation) when it is
    a complete run of ``on``; else the date's default run (the close, ``store.DEFAULT_RUNS``), as the other screens
    show it; a date with only INTRADAY runs: the latest of them. A test run is never taken by default."""
    cols = "SELECT run_id, cut_type, run_type, lineage->'intraday_trades' FROM run WHERE business_date = %s" \
           " AND status = 'complete'"
    row = None
    if run_id is not None:
        row = con.execute(cols + " AND run_id = %s", (on, run_id)).fetchone()
    if row is None:
        row = con.execute(cols + f" AND run_id IN ({store.DEFAULT_RUNS})", (on,)).fetchone() \
            or con.execute(cols + " AND run_type <> 'TEST' ORDER BY run_id DESC LIMIT 1", (on,)).fetchone()
    if not row:
        return None
    t = row[3] or {}
    return {"run_id": row[0], "cut_type": row[1], "run_type": row[2], "applied": t.get("applied", 0),
            "new_positions": t.get("new_positions", 0), "skipped": t.get("skipped", {}),
            "versions": t.get("versions", {}), "skipped_ids": t.get("skipped_ids", {}),
            "truncated": bool(t.get("truncated"))}
