"""Which trades' terms to fetch (TDM#561): the list of the owner's trades against the terms already held.

The adapter's ``trade_list`` dataset (private/1.8, 08 §10) lists the trades of a book, ``trade_terms`` holds their
terms. ``plan`` compares them, per trade id:

* **new**: live in the list, no terms yet;
* **changed**: live in the list, terms held, and both versions known and different (the version of the terms is the
  highest over the trade's legs); an unknown version on either side is not a change;
* **have**: the other live trades of the list;
* **closed**: trades with terms in a listed book that are not in the list as live (gone, or ``cancelled``).

The monitor hands the live trades of the list, each marked with whether its terms are needed, to the adapter as a
file (08 §2.2 ``trades_file``). A source of ``trade_terms`` replaces a book's terms whole, so the adapter needs the
whole list of the book, not just the new and changed trades.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import store


def _version(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def plan(con: Any, day: date, books: list[str]) -> dict | None:
    """The comparison for ``books`` on ``day``, or None when the store has no ``trade_list`` rows of those books."""
    wanted = set(books)
    listing = store.dataset_rows(con, "trade_list", day)
    rows = [r for r in listing.rows if r.get("book_id") in wanted]
    if not rows:
        return None
    listed_books = {u["scope"].get("book_id") for u in listing.units} & wanted | {r["book_id"] for r in rows}
    live: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: (str(r["book_id"]), str(r["trade_id"]))):
        if r.get("status") != "live":
            continue
        tid, ver = str(r["trade_id"]), _version(r.get("trade_version"))
        old = live.get(tid)
        if old is None or (ver is not None and (old["trade_version"] is None or ver > old["trade_version"])):
            live[tid] = {"trade_id": tid, "book_id": r["book_id"], "trade_version": ver}
    held: dict[str, dict] = {}
    for r in store.dataset_rows(con, "trade_terms", day).rows:
        if r.get("book_id") not in listed_books:
            continue
        h = held.setdefault(str(r["trade_id"]), {"version": None})
        v = _version(r.get("trade_version"))
        if v is not None and (h["version"] is None or v > h["version"]):
            h["version"] = v
    new, changed, have, trades = [], [], [], []
    for tid, t in sorted(live.items(), key=lambda kv: (kv[1]["book_id"], kv[0])):
        h = held.get(tid)
        need = True
        if h is None:
            new.append(tid)
        elif t["trade_version"] is not None and h["version"] is not None and t["trade_version"] != h["version"]:
            changed.append(tid)
        else:
            have.append(tid)
            need = False
        trades.append({**t, "need": need})
    closed = sorted(set(held) - set(live))
    return {"date": day.isoformat(), "books": sorted(listed_books), "live": len(live), "new": new,
            "changed": changed, "have": have, "closed": closed, "trades": trades,
            "counts": {"live": len(live), "books": len(listed_books), "requested": len(new) + len(changed),
                       "new": len(new), "changed": len(changed), "have": len(have), "closed": len(closed)}}
