"""A test adapter loaded from a path: the reference CSV layout, but declaring covers=all_active_books."""

import os

from .parse import hit, records

SPI_VERSION = 1


def describe():
    return {"name": "good_adapter", "version": "0.0.1", "spi": 1, "contract": "private/1.0",
            "sources": [{"source": "positions_daily", "dataset": "positions", "time_basis": "start_of_day",
                         "completeness": "full_snapshot", "scope": ["book_id"], "covers": "all_active_books"}]}


def match(path, ctx):
    return hit(path)


def read(path, h, ctx):
    rows, books = [], {}
    for line, rec in records(path):
        books[rec["book_id"]] = None
        if set(rec) == {"book_id"}:
            continue
        found = rec.get("isin") and ctx.instruments.lookup("isin", rec["isin"], h["report_date"])
        if found:
            rec["instrument_id"] = found["instrument_id"]
        rows.append({**rec, "_src": f"R{line}"})
    return {"rows": rows, "scopes": [{"book_id": b} for b in books], "issues": [], "controls": {},
            "meta": {"generated_at": f"{h['report_date']}T06:30:00+03:00"}}


def reference(ctx):
    books = []
    for line, rec in records(os.path.join(ctx.config_dir, "books.csv")):
        rec["active"] = rec.get("active") != "0"
        books.append({**rec, "_src": f"R{line}"})
    return {"books": books}
