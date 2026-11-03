"""The day's trades from the private adapter (S13a, TDM#525; 08 §10 ``trades``, private/1.6): what the monitors' screen
«Сделки дня» shows, and (TDM#557) the day's bond trades in the intraday book (``apply_intraday``). No new-trade PnL
(the rest of S13, TDM#29).

``trades`` is a flow: a date's trades are those of its own files, never carried forward (08 §6.7). The current row of
a ``trade_id`` is its highest ``trade_version`` over the date's cumulative exports and deltas (``store.dataset_rows``,
``store.versioned_rows``); a cancelled trade stays with its status, a late booking (``trade_date`` before the date)
is a trade of the date and is marked ``late``.

**The intraday book** (``apply_intraday``, TDM#557): an INTRADAY run of date D (``cli.eod_run``; the autopilot's
intraday and morning steps, setting ``intraday_trades``, on by default) values the positions of D (the start-of-day
report, carried forward) plus the day's trades: every current trade of D (``dataset_rows``) that is ``live``, a
``bond``, of trade date D (a late booking or an amendment of an older trade is in the report already, or will be) and
has a quantity (``quantity`` in bonds, else ``face_amount_ccy``) changes the row of its ``position_id`` in its book
(else the row of its book with its ``instrument_id``): ``buy`` adds, ``sell`` subtracts; the row's own figures of the
report (face amounts, market and book value, accrued, the report's risk) are scaled with the quantity. A trade of a
bond the book does not hold opens a row of its own (``position_id`` of the trade; it needs an ``instrument_id``).
Anything else is left out and counted by reason (``SKIP_REASONS``). The private inputs hash covers the trades
applied, so the run is made again when they change: everything that compares the hash of an INTRADAY run (the
autopilot's intraday and morning steps, ``recompute.current_day``) takes it from ``intraday_inputs``; the run's lineage
``intraday_trades`` says what was applied. The flash PnL of such a row
is still measured against the previous close (the new trade's own PnL from its price is TDM#29).

``view(con, on, book_id, days)`` is ``GET /api/v1/trades``: the date's current trades (optionally of one book), the
counts per book, the files read, and the last ``days`` dates that have trades for the screen's strip.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime
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


SKIP_REASONS = ("cancelled", "not_bond", "other_trade_date", "no_quantity", "no_instrument", "units_mismatch")
"""Why a trade of the day is not in the intraday book (``effect``; the screen shows them, the run counts them)."""
SCALED = ("face_amount_ccy", "face_outstanding_ccy", "mv_rub", "book_value_rub", "accrued_ccy", "amount_ccy",
          "risk_dv01_rub", "risk_cs01_rub")
"""The report's own figures of a position row that follow its quantity when a trade changes it."""


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


def apply_intraday(con: psycopg.Connection, on: date,
                   inputs: store.PositionInputs) -> tuple[store.PositionInputs, dict[str, Any]]:
    """The positions of ``on`` with the day's trades applied (module doc) and the summary for the run's lineage
    ``intraday_trades``: ``{"set_ids", "applied", "new_positions", "skipped": {reason: n}, "trade_ids"}``."""
    day = store.dataset_rows(con, "trades", on)
    summary: dict[str, Any] = {"set_ids": sorted({u["set_id"] for u in day.units}), "applied": 0,
                               "new_positions": 0, "skipped": {}, "trade_ids": []}
    if not day.rows:
        return inputs, summary
    rows = [list(r) for r in inputs.rows]
    extra = {pid: dict(x) for pid, x in inputs.extra.items()}
    by_pid = {(r[1], r[0]): i for i, r in enumerate(rows)}
    by_secid = {(r[1], r[2]): i for i, r in enumerate(rows) if r[2]}
    skipped: Counter = Counter()
    applied: list[dict] = []
    for t in sorted(day.rows, key=lambda t: (str(t.get("trade_ts") or ""), str(t.get("trade_id")))):
        why = effect(t, on)
        if why:
            skipped[why] += 1
            continue
        book, sign = str(t["book_id"]), 1.0 if t.get("side") == "buy" else -1.0
        qty, face = _num(t.get("quantity")), _num(t.get("face_amount_ccy"))
        i = by_pid.get((book, str(t.get("position_id"))))
        if i is None and t.get("instrument_id"):
            i = by_secid.get((book, t["instrument_id"]))
        if i is None:
            if not t.get("instrument_id"):
                skipped["no_instrument"] += 1
                continue
            pid = str(t["position_id"])
            rows.append([pid, book, t["instrument_id"], sign * qty if qty is not None else None])
            x = {"asset_class": "bond", "x": {"intraday_trade": True}}
            for k in ("isin", "source_instrument_id", "source_instrument_name", "ccy"):
                if t.get(k) is not None:
                    x[k] = t[k]
            if qty is None:
                x["face_amount_ccy"] = sign * face  # type: ignore[operator]
            extra[pid] = x
            by_pid[(book, pid)] = by_secid[(book, t["instrument_id"])] = len(rows) - 1
            summary["new_positions"] += 1
        else:
            pid, _, _, old = rows[i]
            x = extra.get(pid)
            if qty is not None and old is not None:
                new = float(old) + sign * qty
                if x is not None and float(old) != 0:
                    k = new / float(old)
                    for f in SCALED:
                        if x.get(f) is not None:
                            x[f] = float(x[f]) * k
                rows[i][3] = new
            elif qty is None and x is not None and x.get("face_amount_ccy") is not None and old is None:
                x["face_amount_ccy"] = float(x["face_amount_ccy"]) + sign * face  # type: ignore[operator]
            else:
                skipped["units_mismatch"] += 1
                continue
        applied.append({k: t.get(k) for k in ("trade_id", "trade_version", "book_id", "position_id", "side",
                                              "quantity", "face_amount_ccy", "instrument_id")})
    summary.update(applied=len(applied), skipped=dict(sorted(skipped.items())),
                   trade_ids=[str(a["trade_id"]) for a in applied][:500])
    if not applied:
        return inputs, summary
    canon = json.dumps([inputs.private_inputs_hash, applied], sort_keys=True, default=str, separators=(",", ":"))
    return replace(inputs, rows=[tuple(r) for r in rows], extra=extra,  # type: ignore[misc]
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


def _row(r: dict[str, Any], on: date) -> dict[str, Any]:
    out: dict[str, Any] = {f: r.get(f) for f in FIELDS}
    for f in AMOUNTS:
        if out[f] is not None:
            out[f] = float(Decimal(str(out[f])))
    out["late"] = out["trade_date"] is not None and out["trade_date"] < on.isoformat()
    out["intraday"] = effect(r, on) or "applied"  # in the intraday book, or why not (TDM#557)
    out["src"] = r.get("_src")
    if r.get("x"):
        out["x"] = r["x"]
    return out


def view(con: psycopg.Connection, on: date | None = None, book_id: str | None = None,
         days: int = 20) -> dict[str, Any]:
    """The trades screen of ``on`` (default: the latest date with trades, else today, Moscow), of ``book_id`` only when
    given: see the module docstring. Amounts as the contract has them (unsigned; ``side`` gives the direction)."""
    if on is None:
        latest = trade_dates(con, _moscow_today(), 1)
        on = latest[0] if latest else _moscow_today()
    t = store.dataset_rows(con, "trades", on, scope={"book_id": book_id} if book_id else None)
    rows = [_row(r, on) for r in t.rows]
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
        "intraday": _intraday_run(con, on),
    }


def _intraday_run(con: psycopg.Connection, on: date) -> dict[str, Any] | None:
    """The latest complete INTRADAY run of ``on`` and what of the day's trades it took in (its lineage
    ``intraday_trades``), or None."""
    row = con.execute("SELECT run_id, cut_type, lineage->'intraday_trades' FROM run WHERE business_date = %s"
                      " AND run_type = 'INTRADAY' AND status = 'complete' ORDER BY run_id DESC LIMIT 1",
                      (on,)).fetchone()
    if not row:
        return None
    t = row[2] or {}
    return {"run_id": row[0], "cut_type": row[1], "applied": t.get("applied", 0),
            "new_positions": t.get("new_positions", 0), "skipped": t.get("skipped", {}),
            "trade_ids": t.get("trade_ids", [])}
