"""The reference adapter: SPI v1 over canonical private-contract CSV (``contract/private/v1/``).

It is what the backend uses when ``DESK_PRIVATE_ADAPTER`` is unset, and an example of a correct adapter. Like
any adapter it imports only the standard library (08 §2); it is loaded by its module name.

Files, all ``;``-separated UTF-8 with BOM, header = canonical field names (``x_*`` columns pass through):

* ``positions_YYYYMMDD.csv``: source ``positions_csv``, dataset ``positions``, ``start_of_day`` (the date in
  the name is the cut: positions at 00:00 of that day), ``full_snapshot`` per ``book_id``, ``covers=listed``.
  A correction of the same date is ``positions_YYYYMMDD_N.csv``; ``N`` is the hit's ``seq``, so it outranks
  the file without a suffix (08 §6.6).
* ``books.csv`` in ``ctx.config_dir``: ``reference()["books"]``, its unreadable lines in ``["issues"]``. Without a
  config folder there are no books, so the backend rejects every unit with ``unknown_book``: positions are never
  accepted for undefined books.

``scopes`` of a positions file are the distinct ``book_id`` of its rows, plus every **book marker**: a row whose
only non-empty field is ``book_id``, meaning "this book is covered and has no positions" (how a book that sold
out is closed under ``covers=listed``). A file with no rows and no markers touches nothing and says
``empty_report``. Empty cells are absent fields. ``_src`` is ``L<line>`` (the header is line 1).
``instrument_id`` is kept when the file has it, else looked up by ``isin``, ``regnum`` or ``moex_secid``.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

SPI_VERSION = 1
SOURCE = "positions_csv"
_POSITIONS = re.compile(r"^positions_([0-9]{8})(?:_([0-9]+))?\.csv$")
_TRUE, _FALSE = {"1", "true", "yes"}, {"0", "false", "no"}


def describe() -> dict:
    return {
        "name": "desk_reference", "version": "1.0.0", "spi": 1, "contract": "private/1.0",
        "sources": [{"source": SOURCE, "dataset": "positions", "time_basis": "start_of_day",
                     "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}],
    }


def match(path: str, ctx) -> dict | None:
    m = _POSITIONS.match(os.path.basename(path))
    if not m:
        return None
    try:
        day = datetime.strptime(m.group(1), "%Y%m%d").date()
    except ValueError:
        return None
    return {"source": SOURCE, "report_date": day.isoformat(), "date_from": "name", "report_ts": None,
            "seq": int(m.group(2)) if m.group(2) else None}


def _records(path: str) -> tuple[list[tuple[int, dict]], list[dict]]:
    """(line number, non-empty cells) per data line, and layout issues."""
    out, issues = [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = [h.strip() for h in next(reader, [])]
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            if len(values) > len(header):
                issues.append({"severity": "error", "code": "unexpected_layout", "src": f"L{reader.line_num}",
                               "message": "more cells than header columns"})
                continue
            out.append((reader.line_num, {k: v.strip() for k, v in zip(header, values) if v.strip()}))
    return out, issues


def read(path: str, hit: dict, ctx) -> dict:
    records, issues = _records(path)
    rows, books = [], {}
    for line, rec in records:
        if "book_id" in rec:
            books.setdefault(rec["book_id"], None)
        if set(rec) == {"book_id"}:
            continue  # a book marker: covered, no positions
        if "instrument_id" not in rec:
            for kind in ("isin", "regnum", "moex_secid"):
                found = rec.get(kind) and ctx.instruments.lookup(kind, rec[kind], hit["report_date"])
                if found:
                    rec["instrument_id"] = found["instrument_id"]
                    break
        rows.append({**rec, "_src": f"L{line}"})
    if not records:
        issues.append({"severity": "warning", "code": "empty_report", "message": "no rows and no book markers"})
    return {"rows": rows, "scopes": [{"book_id": b} for b in books], "issues": issues, "controls": {}, "meta": {}}


def reference(ctx) -> dict:
    if not ctx.config_dir or not os.path.isfile(p := os.path.join(ctx.config_dir, "books.csv")):
        return {"books": []}
    books = []
    records, issues = _records(p)
    for line, rec in records:
        flag = rec.get("active", "").lower()
        if flag in _TRUE or flag in _FALSE:
            rec["active"] = flag in _TRUE
        books.append({**rec, "_src": f"L{line}"})
    return {"books": books, "issues": issues}  # a line that cannot be read is said, not dropped silently
