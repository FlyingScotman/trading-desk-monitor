"""The reference adapter: SPI v1 over canonical private-contract CSV (``contract/private/v1/``).

It is what the backend uses when ``DESK_PRIVATE_ADAPTER`` is unset, and an example of a correct adapter. Like
any adapter it imports only the standard library (08 §2); it is loaded by its module name.

Files, all ``;``-separated UTF-8 with BOM, header = canonical field names (``x_*`` columns pass through):

* ``positions_YYYYMMDD.csv``: source ``positions_csv``, dataset ``positions``, ``start_of_day`` (the date in
  the name is the cut: positions at 00:00 of that day), ``full_snapshot`` per ``book_id``, ``covers=listed``.
  A correction of the same date is ``positions_YYYYMMDD_N.csv``; ``N`` is the hit's ``seq``, so it outranks
  the file without a suffix (08 §6.6). The other datasets (private/1.2) work the same way, all ``end_of_day``
  (the date in the name is the business date) and ``covers=listed``:
  ``ocp_YYYYMMDD.csv`` (source ``ocp_csv``, scope ``entity``, ``book_id``), ``cash_balances_YYYYMMDD.csv``
  (``cash_balances_csv``, ``entity``, ``book_id``), ``pnl_official_YYYYMMDD.csv`` (``pnl_official_csv``,
  ``entity``, ``book_id``) and ``trade_terms_YYYYMMDD.csv`` (``trade_terms_csv``, ``book_id``; FX trades since
  private/1.2, swap legs, REPO and HTM purchases since 1.4). A
  ``cash_balances`` or ``pnl_official`` file may hold several dates (a history): a ``_report_date`` column gives
  each row's date (08 §6.1); an empty cell is the date in the name.
* ``books.csv`` in ``ctx.config_dir``: ``reference()["books"]``, its unreadable lines in ``["issues"]``. Without a
  config folder there are no books, so the backend rejects every unit with ``unknown_book``: positions are never
  accepted for undefined books. ``entities.csv`` and ``funding_rules.csv`` there, when present, are
  ``reference()["entities"]`` and ``["funding_rules"]`` (private/1.2); ``funding_policy.csv`` is
  ``["funding_policy"]`` (private/1.3), ``ocp_policy.csv`` ``["ocp_policy"]`` (private/1.5).

``scopes`` of a file are the distinct scope units of its rows, plus every **marker**: a row whose only non-empty
fields are the scope keys (and ``_report_date``), meaning "this unit is covered and has no rows" (how a book that
sold out is closed under ``covers=listed``). A file with no rows and no markers touches nothing and says
``empty_report``. Empty cells are absent fields; ``1``/``true``/``yes`` and ``0``/``false``/``no`` in a boolean
column (``active``, ``ndf``, ``include_ndf``, ``cash_from_balances``) are booleans. ``_src`` is ``L<line>`` (the header is line 1).
``instrument_id`` (``positions``, ``pnl_official``) is kept when the file has it, else looked up by ``isin``,
``regnum`` or ``moex_secid``.
"""

from __future__ import annotations

import csv
import os
import re
from datetime import datetime

SPI_VERSION = 1
SOURCE = "positions_csv"
_FILE = re.compile(r"^(positions|ocp|cash_balances|pnl_official|trade_terms)_([0-9]{8})(?:_([0-9]+))?\.csv$")
_DATASETS = {  # file prefix = dataset -> (source, time basis, scope)
    "positions": (SOURCE, "start_of_day", ["book_id"]),
    "ocp": ("ocp_csv", "end_of_day", ["entity", "book_id"]),
    "cash_balances": ("cash_balances_csv", "end_of_day", ["entity", "book_id"]),
    "pnl_official": ("pnl_official_csv", "end_of_day", ["entity", "book_id"]),
    "trade_terms": ("trade_terms_csv", "end_of_day", ["book_id"]),
}
_BY_SOURCE = {src: ds for ds, (src, _, _) in _DATASETS.items()}
_MULTI_DATE = {"cash_balances", "pnl_official"}
_WITH_INSTRUMENT = {"positions", "pnl_official"}
_BOOLEANS = {"active", "ndf", "include_ndf", "cash_from_balances"}
_REFERENCE = (("books", "books.csv"), ("entities", "entities.csv"), ("funding_rules", "funding_rules.csv"),
              ("funding_policy", "funding_policy.csv"), ("ocp_policy", "ocp_policy.csv"))
_TRUE, _FALSE = {"1", "true", "yes"}, {"0", "false", "no"}


def describe() -> dict:
    return {
        "name": "desk_reference", "version": "1.3.0", "spi": 1, "contract": "private/1.5",
        "sources": [{"source": src, "dataset": ds, "time_basis": tb, "completeness": "full_snapshot",
                     "scope": scope, "covers": "listed"} for ds, (src, tb, scope) in _DATASETS.items()],
    }


def match(path: str, ctx) -> dict | None:
    m = _FILE.match(os.path.basename(path))
    if not m:
        return None
    try:
        day = datetime.strptime(m.group(2), "%Y%m%d").date()
    except ValueError:
        return None
    return {"source": _DATASETS[m.group(1)][0], "report_date": day.isoformat(), "date_from": "name",
            "report_ts": None, "seq": int(m.group(3)) if m.group(3) else None}


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
            rec = {k: v.strip() for k, v in zip(header, values) if v.strip()}
            for k in _BOOLEANS & set(rec):
                flag = rec[k].lower()
                if flag in _TRUE or flag in _FALSE:
                    rec[k] = flag in _TRUE
            out.append((reader.line_num, rec))
    return out, issues


def read(path: str, hit: dict, ctx) -> dict:
    dataset = _BY_SOURCE[hit["source"]]
    scope_keys = _DATASETS[dataset][2]
    records, issues = _records(path)
    rows, units = [], {}
    for line, rec in records:
        if dataset not in _MULTI_DATE:
            rec.pop("_report_date", None)  # only multi-date datasets may carry it; dropped, not passed on
        if all(k in rec for k in scope_keys):
            unit = {k: rec[k] for k in (*scope_keys, "_report_date") if k in rec}
            units.setdefault(tuple(sorted(unit.items())), unit)
        if set(rec) <= {*scope_keys, "_report_date"}:
            continue  # a marker: covered, no rows
        if dataset in _WITH_INSTRUMENT and "instrument_id" not in rec:
            for kind in ("isin", "regnum", "moex_secid"):
                found = rec.get(kind) and ctx.instruments.lookup(kind, rec[kind], rec.get("_report_date",
                                                                                          hit["report_date"]))
                if found:
                    rec["instrument_id"] = found["instrument_id"]
                    break
        rows.append({**rec, "_src": f"L{line}"})
    if not records:
        issues.append({"severity": "warning", "code": "empty_report", "message": "no rows and no markers"})
    return {"rows": rows, "scopes": list(units.values()), "issues": issues, "controls": {}, "meta": {}}


def reference(ctx) -> dict:
    out: dict = {"books": []}
    issues: list[dict] = []
    for kind, name in _REFERENCE:
        if not ctx.config_dir or not os.path.isfile(p := os.path.join(ctx.config_dir, name)):
            continue
        records, bad = _records(p)
        out[kind] = [{**rec, "_src": f"L{line}"} for line, rec in records]
        out["issues"] = issues = issues + bad  # a line that cannot be read is said, not dropped silently
    return out
