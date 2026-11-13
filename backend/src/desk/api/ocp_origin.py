"""Where a derived figure of the published OCP comes from (TDM#713): per line published from positions (``derived``,
``derived_stale_report``) every item its amount sums, down to the first source.

A derived line is the sum of the run's OCP lines (``rules.ocp.derive``) of its books in its currency: the run's
positions (``position_lines``), the cash balances (``cash_lines``) and the funding liabilities of bonds
(``liability_lines``), whatever their component. The run stores those lines (the block's ``lines``) but not their
sources, so this module reads the sources for the run **at request time** from what the store has fixed for it: the
position sets of the run (``run.position_set_ids``: the book's set, its valid date, its file), the run's facts of the
positions (``run_position``: name and kind) and the cash balances in force on the run's date as known at its
``positions_known_at`` (``store.dataset_rows``, the same call the run made). A run computed before this module has the
breakdown too, nothing is recomputed or stored.

An item of the run's lines is a position, a cash balance of a book and currency or a funding liability; with it its
set: ``valid_date``, ``carried`` (the set is older than the run's date: the book's positions or balances were carried
forward from it) and the file. A cash balance has its accounts as the reading finds them; when the reading does not
give the stored amount back (the dataset was read otherwise since: ``restored`` false), the accounts are left out
and the stored amount stays. The items sum to the line's amount (``sum``; ``matches``): at most ``MAX_ITEMS`` largest
by size, the rest in ``rest``.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date
from typing import Any

from psycopg.rows import dict_row, tuple_row

from .. import cash as cashmod
from .. import store
from ..rules import ocp as O

MAX_ITEMS = 40
"""Per line at most this many items (the largest by size), the rest summed in ``rest``."""
KINDS = {"CASH": "cash", "FUND": "liability"}
"""The first part of an OCP line's key -> its kind; any other key is a position's id."""


def _q(con, sql: str, params: tuple) -> list[dict[str, Any]]:
    """Rows as dicts, whatever the row factory of the caller's connection."""
    with con.cursor(row_factory=dict_row) as cur:
        return cur.execute(sql, params).fetchall()


def _day(x: Any) -> date | None:
    return x if isinstance(x, date) else None


def _set(valid: date | None, on: date, **kw: Any) -> dict[str, Any]:
    """The origin of a set: its valid date, whether it is carried forward (older than the run's date) and its age."""
    return {"valid_date": valid, "carried": bool(valid and valid < on), "age_days": (on - valid).days if valid else None,
            **kw}


def _kind(line: dict[str, Any]) -> str:
    return KINDS.get(str(line.get("key") or "").split("|", 1)[0], "position")


def _cash(con, on: date, known_at: Any) -> tuple[dict[tuple[str, str], list[dict[str, Any]]], dict[Any, dict]]:
    """((book, currency) -> the cash rows in force, set id -> its unit), as the run read them."""
    was = con.row_factory
    con.row_factory = tuple_row  # the store reads its rows by position; the request's connection gives dicts
    try:
        ds = store.dataset_rows(con, "cash_balances", on, known_at=known_at)
    finally:
        con.row_factory = was
    by: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in ds.rows:
        by[(str(r.get("book_id")), str(r.get("ccy")))].append(r)
    return by, {u["set_id"]: u for u in ds.units}


def _cash_item(item: dict[str, Any], rows: list[dict[str, Any]], units: dict[Any, dict], on: date) -> None:
    """The accounts and sets of a cash line of the run, when the reading gives its amount back."""
    got = [b.known for b in cashmod.compare(rows)] if rows else []
    known = float(got[0]) if len(got) == 1 and got[0] is not None else None
    item["restored"] = known is not None and abs(known - item["amount"]) <= max(0.01, 1e-9 * abs(item["amount"]))
    if not item["restored"]:
        return
    item["accounts"] = [{"account_id": r.get("account_id"), "account_kind": r.get("account_kind"),
                         "ledger": r.get("ledger") or None, "balance_ccy": float(r["balance_ccy"])} for r in rows]
    sets = {}
    for r in rows:
        u = units.get(r.get("_set_id")) or {}
        sets[r.get("_set_id")] = _set(_day(r.get("_valid_date")), on, set_id=r.get("_set_id"),
                                      source=u.get("source"), file_name=u.get("file_name"),
                                      report_date=_day(u.get("report_date")))
    item["sets"] = sorted(sets.values(), key=lambda s: (str(s["valid_date"]), s["set_id"] or 0))


def origins(con, run_id: int, block: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The breakdown of every published line of the run's block that is derived from positions (the module
    docstring); ``[]`` for a block without such a line or without the run's lines."""
    pub = ((block or {}).get("published") or {}).get("by_book") or []
    todo = [x for x in pub if x.get("source") in (O.DERIVED, O.DERIVED_STALE)]
    stored = (block or {}).get("lines") or []
    if not todo or not stored:
        return []
    runs = _q(con, "SELECT business_date, position_set_ids, positions_known_at FROM run WHERE run_id = %s", (run_id,))
    if not runs:
        return []
    run = runs[0]
    on, known_at = run["business_date"], run["positions_known_at"]
    books = {b for x in todo for b in (x.get("books") or [x.get("book_id")]) if b}
    sets = {r["book_id"]: _set(r["valid_date"], on, set_id=r["set_id"], source=r["source"], file_name=r["source_name"],
                               report_date=_day(r["report_date"]))
            for r in _q(con, "SELECT set_id, book_id, valid_date, source, source_name, report_date"
                             " FROM position_set WHERE set_id = ANY(%s)", (run["position_set_ids"],))
            if r["book_id"] in books}
    pids = sorted({x["position_id"] for x in stored if x.get("position_id") and x.get("book_id") in books})
    facts = {r["position_id"]: r for r in _q(
        con, "SELECT position_id, secid, fact->>'name' AS name, fact->>'instr_type' AS instr_type,"
        " fact->>'asset_class' AS asset_class, fact->>'maturity' AS maturity FROM run_position"
        " WHERE run_id = %s AND position_id = ANY(%s)", (run_id, pids))} if pids else {}
    cash_rows, cash_units = _cash(con, on, known_at) if any(_kind(x) == "cash" for x in stored) else ({}, {})
    by_line: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for x in stored:
        by_line[(str(x.get("book_id")), str(x.get("ccy")))].append(x)
    out = []
    for line in todo:
        held = sorted({b for b in (line.get("books") or [line.get("book_id")]) if b})
        items = []
        for b in held:
            for x in by_line.get((b, line["ccy"]), ()):
                kind = _kind(x)
                item = {"kind": kind, "key": x["key"], "position_id": x.get("position_id"), "book_id": b,
                        "component": x["component"], "basis": x["basis"], "amount": x["amount"], "pv": x.get("pv"),
                        "note": x.get("note") or ""}
                if kind in ("position", "liability") and x.get("position_id") in facts:
                    f = facts[x["position_id"]]
                    item.update(name=f["name"], instr_type=f["instr_type"], asset_class=f["asset_class"],
                                maturity=f["maturity"], secid=f["secid"] or None)
                if kind == "position":
                    item["set"] = sets.get(b)
                elif kind == "cash":
                    _cash_item(item, cash_rows.get((b, line["ccy"]), []), cash_units, on)
                items.append(item)
        items.sort(key=lambda i: (-abs(i["amount"]), i["key"]))
        head, rest = items[:MAX_ITEMS], items[MAX_ITEMS:]
        total = math.fsum(i["amount"] for i in items)
        out.append({"entity": line["entity"], "book_id": line.get("book_id"), "books": held, "ccy": line["ccy"],
                    "source": line["source"], "amount": line["amount"], "sum": total,
                    "matches": abs(total - line["amount"]) <= max(0.01, 1e-9 * abs(line["amount"])),
                    "items": head,
                    "rest": {"count": len(rest), "amount": math.fsum(i["amount"] for i in rest)} if rest else None,
                    "sets": [{"book_id": b, "set": sets.get(b)} for b in held]})
    return out
