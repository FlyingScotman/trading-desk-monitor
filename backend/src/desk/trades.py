"""The day's trades from the private adapter (S13a, TDM#525; 08 §10 ``trades``, private/1.6): what the monitors' screen
«Сделки дня» shows. Acceptance only: no valuation of the trades and no new-trade PnL (the rest of S13, TDM#29).

``trades`` is a flow: a date's trades are those of its own files, never carried forward (08 §6.7). The current row of
a ``trade_id`` is its highest ``trade_version`` over the date's cumulative exports and deltas (``store.dataset_rows``,
``store.versioned_rows``); a cancelled trade stays with its status, a late booking (``trade_date`` before the date)
is a trade of the date and is marked ``late``.

``view(con, on, book_id, days)`` is ``GET /api/v1/trades``: the date's current trades (optionally of one book), the
counts per book, the files read, and the last ``days`` dates that have trades for the screen's strip.
"""

from __future__ import annotations

from collections import Counter
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


def _row(r: dict[str, Any], on: date) -> dict[str, Any]:
    out: dict[str, Any] = {f: r.get(f) for f in FIELDS}
    for f in AMOUNTS:
        if out[f] is not None:
            out[f] = float(Decimal(str(out[f])))
    out["late"] = out["trade_date"] is not None and out["trade_date"] < on.isoformat()
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
    }
