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
  private/1.2, swap legs, REPO and HTM purchases since 1.4) and ``trade_list_YYYYMMDD.csv`` (``trade_list_csv``,
  ``book_id``; private/1.8: the trades of the books, a stock). An ``ocp`` row's ``derivatives_ccy`` (private/1.7) is
  written ``fx_forward=-1000000,ndf=0`` (``{}``: none); its ``spot_future`` (private/1.9) is a list of lines
  ``value_date,pair,amount_ccy,src`` separated by ``|`` (``2026-12-14,EUR/USD,800000,R5|2026-12-15,,-5000``: ``pair`` and
  ``src`` may be empty; ``[]``: none). A
  ``cash_balances``, ``pnl_official`` or ``trades`` file may hold several dates (a history): a ``_report_date``
  column gives each row's date (08 §6.1); an empty cell is the date in the name.
* ``trades`` (private/1.6), ``intraday``, scope ``book_id``: ``trades_YYYYMMDD_HHMM.csv`` is the day's cumulative
  export cut at HH:MM Moscow time (source ``trades_csv``, ``full_snapshot``, ``covers=listed``: it replaces the trades
  of the books it lists), ``trades_delta_YYYYMMDD_HHMM.csv`` deltas (source ``trades_delta_csv``, ``increment``:
  its rows add to the earlier deltas of the book and date, the highest ``trade_version`` of a ``trade_id``
  current). The two are separate sources, so one book and date takes either cumulative cuts or deltas: a delta
  for a book that already has a cumulative cut that date is a ``scope_conflict`` (08 §6.6). The suffix is the hit's ``report_ts``, not a ``seq``; without it the file is the day's
  final cut (23:59:59), an invalid time is not matched.
* ``books.csv`` in ``ctx.config_dir``: ``reference()["books"]``, its unreadable lines in ``["issues"]``. Without a
  config folder there are no books, so the backend rejects every unit with ``unknown_book``: positions are never
  accepted for undefined books. ``entities.csv`` and ``funding_rules.csv`` there, when present, are
  ``reference()["entities"]`` and ``["funding_rules"]`` (private/1.2); ``funding_policy.csv`` is
  ``["funding_policy"]`` (private/1.3), ``ocp_policy.csv`` ``["ocp_policy"]`` (private/1.5).

``scopes`` of a file are the distinct scope units of its rows, plus every **marker**: a row whose only non-empty
fields are the scope keys (and ``_report_date``), meaning "this unit is covered and has no rows" (how a book that
sold out is closed under ``covers=listed``). A file with no rows and no markers touches nothing and says
``empty_report``. Empty cells are absent fields; ``1``/``true``/``yes`` and ``0``/``false``/``no`` in a boolean
column (``active``, ``ndf``, ``include_ndf``, ``cash_from_balances``, ``in_positions``) are booleans. ``_src`` is ``L<line>`` (the header is line 1).
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
_FILE = re.compile(r"^(positions|ocp|cash_balances|pnl_official|trade_terms|trade_list|trades|trades_delta)_([0-9]{8})"
                   r"(?:_([0-9]+))?\.csv$")
_DATASETS = {  # file prefix -> (source, time basis, scope); the prefix is the dataset, but for trades_delta
    "positions": (SOURCE, "start_of_day", ["book_id"]),
    "ocp": ("ocp_csv", "end_of_day", ["entity", "book_id"]),
    "cash_balances": ("cash_balances_csv", "end_of_day", ["entity", "book_id"]),
    "pnl_official": ("pnl_official_csv", "end_of_day", ["entity", "book_id"]),
    "trade_terms": ("trade_terms_csv", "end_of_day", ["book_id"]),
    "trade_list": ("trade_list_csv", "end_of_day", ["book_id"]),
    "trades": ("trades_csv", "intraday", ["book_id"]),
    "trades_delta": ("trades_delta_csv", "intraday", ["book_id"]),
}
_DATASET_OF = {"trades_delta": "trades"}
_INCREMENT = {"trades_delta"}
_BY_SOURCE = {src: prefix for prefix, (src, _, _) in _DATASETS.items()}
_MULTI_DATE = {"cash_balances", "pnl_official", "trades"}
_WITH_INSTRUMENT = {"positions", "pnl_official", "trades"}
_CUT = re.compile(r"([01][0-9]|2[0-3])([0-5][0-9])")
_BOOLEANS = {"active", "ndf", "include_ndf", "cash_from_balances", "in_positions"}
_OBJECTS = {"derivatives_ccy"}
"""Cells that hold an object (private/1.7 ``ocp.derivatives_ccy``): ``key=amount`` pairs separated by ``,``
(``fx_forward=-1000000,ndf=0``), or ``{}`` for an empty one."""
_LISTS = {"spot_future"}
"""Cells that hold a list of objects (private/1.9 ``ocp.spot_future``): the entries separated by ``|``, the fields of an
entry by ``,`` in the order ``_SPOT_FIELDS`` (``pair`` and ``src`` may be empty), or ``[]`` for an empty list."""
_SPOT_FIELDS = ("value_date", "pair", "amount_ccy", "src")
_REFERENCE = (("books", "books.csv"), ("entities", "entities.csv"), ("funding_rules", "funding_rules.csv"),
              ("funding_policy", "funding_policy.csv"), ("ocp_policy", "ocp_policy.csv"))
_TRUE, _FALSE = {"1", "true", "yes"}, {"0", "false", "no"}


def describe() -> dict:
    return {
        "name": "desk_reference", "version": "1.5.0", "spi": 1, "contract": "private/1.9",
        "sources": [{"source": src, "dataset": _DATASET_OF.get(prefix, prefix), "time_basis": tb, "scope": scope,
                     **({"completeness": "increment"} if prefix in _INCREMENT else
                        {"completeness": "full_snapshot", "covers": "listed"})}
                    for prefix, (src, tb, scope) in _DATASETS.items()],
    }


def match(path: str, ctx) -> dict | None:
    m = _FILE.match(os.path.basename(path))
    if not m:
        return None
    try:
        day = datetime.strptime(m.group(2), "%Y%m%d").date()
    except ValueError:
        return None
    src, basis, _ = _DATASETS[m.group(1)]
    if basis == "intraday":  # trades: the suffix is the cut's time, none the day's final cut
        cut = _CUT.fullmatch(m.group(3) or "2359")
        if cut is None:
            return None
        ts = f"{day.isoformat()}T{cut.group(1)}:{cut.group(2)}:{'59' if m.group(3) is None else '00'}+03:00"
        return {"source": src, "report_date": day.isoformat(), "date_from": "name", "report_ts": ts, "seq": None}
    return {"source": src, "report_date": day.isoformat(), "date_from": "name",
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
            for k in _OBJECTS & set(rec):
                rec[k], twice = _object(rec[k])
                if twice:
                    issues.append({"severity": "warning", "code": "duplicate_object_key", "src": f"L{reader.line_num}",
                                   "message": f"{k} names a key twice: the last value kept"})
            for k in _LISTS & set(rec):
                rec[k] = _spot_list(rec[k])
            out.append((reader.line_num, rec))
    return out, issues


def _spot_list(cell: str) -> list | str:
    """An ``_LISTS`` cell as a list of ``{value_date, pair, amount_ccy, src}`` (empty fields left out); a malformed one
    stays text, so the backend says ``bad_value``."""
    if cell == "[]":
        return []
    out = []
    for part in cell.split("|"):
        cells = [c.strip() for c in part.split(",")]
        if not 3 <= len(cells) <= len(_SPOT_FIELDS):
            return cell
        out.append({k: v for k, v in zip(_SPOT_FIELDS, cells, strict=False) if v})
    return out


def _object(cell: str) -> tuple[dict | str, bool]:
    """(an ``_OBJECTS`` cell as an object, whether a key was given twice: the last value kept, said as a warning); a
    malformed one stays text, so the backend says ``bad_value``."""
    if cell == "{}":
        return {}, False
    out: dict = {}
    twice = False
    for part in cell.split(","):
        k, eq, v = part.partition("=")
        if not eq or not k.strip() or not v.strip():
            return cell, False
        twice = twice or k.strip() in out
        out[k.strip()] = v.strip()
    return out, twice


def read(path: str, hit: dict, ctx) -> dict:
    prefix = _BY_SOURCE[hit["source"]]
    dataset, scope_keys = _DATASET_OF.get(prefix, prefix), _DATASETS[prefix][2]
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
    scopes = [] if prefix in _INCREMENT else list(units.values())  # an increment lists no units: it replaces none
    return {"rows": rows, "scopes": scopes, "issues": issues, "controls": {}, "meta": {}}


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
