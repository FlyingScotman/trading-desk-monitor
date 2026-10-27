"""Saved PnL report (#67/#386): original components, historical holdings, no summary rounding.

Default EODs before the anchor; the selected run on its date, as concepts._runs, without its 260-run cap.
No reconstruction of trades, absent rows, book-level funding or absent components. One aggregator serves the
report and the selected position; only the latter sends observations to the browser.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from psycopg.rows import dict_row

from ..pnl.reasons import REPORTED_METHOD
from ..store import DEFAULT_RUNS
from .runs import PNL_KEYS, run_slice

KEYS = (*PNL_KEYS, "reported")
GROSS_KEYS = tuple(k for k in PNL_KEYS if k not in ("funding", "fx_funding"))
MAX_PERIOD_DAYS = 1096


def amounts(pnl: dict[str, Any] | None) -> dict[str, Any]:
    """Existing net convention, only PNL_KEYS; missing components null, reported PV separate.

    Metadata, received coupons (already in carry) and alternative CBR FX never enter the sum.
    """
    q = pnl or {}
    comp = {k: q.get(k) for k in PNL_KEYS}
    extra = q.get("extra") or {}
    if extra.get("method") == REPORTED_METHOD:
        comp["reported"], comp["other"] = comp["other"], None
    else:
        comp["reported"] = None
    known = [q[k] for k in PNL_KEYS if q.get(k) is not None]
    gross = extra.get("total")
    gap = (gross - math.fsum(q[k] for k in GROSS_KEYS)
           if gross is not None and all(q.get(k) is not None for k in GROSS_KEYS) else None)
    return {"components": comp, "total": math.fsum(known) if known else None,
            "missing": [k for k in PNL_KEYS if q.get(k) is None],
            "method": extra.get("method"), "note": extra.get("note"), "gross": gross,
            "grossCheck": gap, "prevDate": extra.get("prev_date")}


class Sum:
    """Compensated running sum, bounded memory even over years of position rows; no values means null."""

    def __init__(self) -> None:
        self.s = self.c = 0.0
        self.n = 0

    def add(self, v: float | None) -> None:
        if v is None:
            return
        t = self.s + v
        self.c += (self.s - t) + v if abs(self.s) >= abs(v) else (v - t) + self.s
        self.s, self.n = t, self.n + 1

    def value(self) -> float | None:
        return self.s + self.c if self.n else None

    def merge(self, other: Sum) -> None:
        if other.n:
            n = self.n + other.n
            self.add(other.s)
            self.add(other.c)
            self.n = n


def _selected(a: dict[str, Any], keys: list[str]) -> float | None:
    vals = [a["components"][k] for k in keys if a["components"][k] is not None]
    return math.fsum(vals) if vals else None


class Day:
    """Only amounts used by daily charts; component coverage is accumulated once, per position."""

    def __init__(self) -> None:
        self.total, self.selected = Sum(), Sum()

    def add(self, a: dict[str, Any], selected: float | None) -> None:
        self.total.add(a["total"])
        self.selected.add(selected)

    def result(self) -> dict[str, Any]:
        return {"total": self.total.value(), "selected": self.selected.value()}


class Aggregate:
    def __init__(self) -> None:
        self.total, self.selected, self.gap = Sum(), Sum(), Sum()
        self.components = {k: Sum() for k in KEYS}
        self.missing = dict.fromkeys(KEYS, 0)
        self.count = dict.fromkeys(KEYS, 0)
        self.rows = self.no_total = self.partial = 0

    def add(self, a: dict[str, Any], selected: float | None) -> None:
        self.rows += 1
        self.no_total += a["total"] is None
        self.partial += bool(a["missing"])
        self.total.add(a["total"])
        self.selected.add(selected)
        self.gap.add(a["grossCheck"])
        for k, v in a["components"].items():
            applicable = (a["method"] == REPORTED_METHOD if k == "reported" else
                          a["method"] != REPORTED_METHOD if k == "other" else True)
            self.components[k].add(v)
            self.count[k] += applicable
            self.missing[k] += applicable and v is None

    def merge(self, other: Aggregate) -> None:
        self.total.merge(other.total)
        self.selected.merge(other.selected)
        self.gap.merge(other.gap)
        self.rows += other.rows
        self.no_total += other.no_total
        self.partial += other.partial
        for k in KEYS:
            self.components[k].merge(other.components[k])
            self.count[k] += other.count[k]
            self.missing[k] += other.missing[k]

    def result(self) -> dict[str, Any]:
        return {"total": self.total.value(), "selected": self.selected.value(), "rowCount": self.rows,
                "noTotal": self.no_total, "partialRows": self.partial,
                "grossCheck": self.gap.value(), "grossCount": self.gap.n,
                "components": [{"key": k, "value": s.value(), "count": self.count[k], "missing": self.missing[k]}
                               for k, s in self.components.items()]}


def summarize(rows: Iterable[dict[str, Any]], start: date, end: date, selected: list[str],
              detail: bool = False) -> dict[str, Any]:
    total = Aggregate()
    days: dict[str, Day] = {}
    positions: dict[tuple[str, str], tuple[dict, Aggregate]] = {}
    books: dict[str, Aggregate] = {}
    observations = []
    for r in rows:
        r = dict(r)
        a = amounts(r.pop("pnl"))
        d = str(r["date"])
        key = (r["book"], r["positionId"])
        if key not in positions:
            positions[key] = (r, Aggregate())
        positions[key] = (r, positions[key][1])  # latest metadata, identity always (book, position)
        if d not in days:
            days[d] = Day()
        value = _selected(a, selected)
        positions[key][1].add(a, value)
        days[d].add(a, value)
        if detail:
            observations.append({**r, **a})
    for (book, _pid), (_row, a) in positions.items():
        total.merge(a)
        if book not in books:
            books[book] = Aggregate()
        books[book].merge(a)
    axis, cumulative, selected_cumulative = [], Sum(), Sum()
    d = start
    while d <= end:
        a = days.get(str(d), Day()).result()
        cumulative.add(a["total"])
        selected_cumulative.add(a["selected"])
        axis.append({"date": str(d), **a,
                     "cumulative": cumulative.value() if a["total"] is not None else None,
                     "selectedCumulative": selected_cumulative.value() if a["selected"] is not None else None})
        d += timedelta(days=1)
    return {"summary": total.result(), "days": axis,
            "positions": [{"id": json.dumps(k, separators=(",", ":")), "row": r, **a.result()}
                          for k, (r, a) in positions.items()],
            "books": [{"id": k, **a.result()} for k, a in books.items()], "observations": observations}


# Only the fields used by the report; never fetch full valuation facts (risk, flows, legs, etc.).
DIMENSIONS = """SELECT p.run_id AS "runId", r.business_date AS date, p.position_id AS "positionId",
 p.book_id AS book, b.data->>'name' AS "bookName", COALESCE(b.data->>'entity', '—') AS entity,
 COALESCE(b.data->>'desk', '—') AS desk,
 i.x->>'x_portfolio' AS portfolio, COALESCE(p.fact->>'name', i.source_instrument_name, p.secid, p.position_id) AS name,
 COALESCE(p.fact->>'instrument_id', p.secid) AS instrument, COALESCE(p.fact->>'ccy', i.ccy) AS ccy,
 p.fact->>'asset_class' AS "assetClass", p.fact->>'issuer' AS issuer, p.no_pnl AS "noPnl"
 FROM run_position p JOIN run r USING (run_id)
 LEFT JOIN books b ON b.run_id = p.run_id AND b.data->>'book_id' = p.book_id
 LEFT JOIN LATERAL (SELECT x, ccy, source_instrument_name FROM position_input i
 JOIN position_set s USING (set_id) WHERE i.set_id = ANY(r.position_set_ids)
 AND i.position_id = p.position_id AND s.book_id = p.book_id LIMIT 1) i ON true
 WHERE p.run_id = ANY(%(ids)s)"""


def report(con, anchor: dict[str, Any], start: date, end: date, selected: list[str],
           filters: dict[str, Any], catalogue: bool = True, position: tuple[str, str] | None = None) -> dict[str, Any]:
    runs = con.execute(
        f"SELECT * FROM run WHERE run_id IN ({DEFAULT_RUNS}) AND business_date >= %s"
        " AND business_date <= %s AND business_date < %s ORDER BY business_date",
        (start, end, anchor["business_date"])).fetchall()
    overlapping = [r["business_date"] for r in runs if anchor["run_type"] == "INTRADAY"
                   and anchor["prev_date"] and r["prev_date"]
                   and max(anchor["prev_date"], r["prev_date"]) < min(anchor["business_date"], r["business_date"])]
    separate = bool(overlapping) and start <= anchor["business_date"] <= end
    intraday_only = [r["business_date"] for r in con.execute(
        "SELECT DISTINCT business_date FROM run WHERE status='complete' AND run_type='INTRADAY'"
        " AND business_date BETWEEN %s AND %s AND business_date < %s ORDER BY 1",
        (start, end, anchor["business_date"])) if r["business_date"] not in {r["business_date"] for r in runs}]
    if start <= anchor["business_date"] <= end:
        runs.append(anchor)
    ids = [r["run_id"] for r in runs]
    books_sql = ("WITH books AS MATERIALIZED (SELECT run_id, jsonb_array_elements(data) AS data FROM run_reference"
                 " WHERE run_id = ANY(%(ids)s) AND kind = 'books')")
    cte = books_sql + ", dimensions AS (" + DIMENSIONS + ") "
    args: dict[str, Any] = {"ids": ids}
    where = []
    for k in ("entity", "desk", "ccy", "assetClass", "issuer"):
        if filters.get(k):
            where.append(f'd."{k}" = %({k})s')
            args[k] = filters[k]
    if filters.get("books"):
        where.append("d.book = ANY(%(books)s)")
        args["books"] = filters["books"]
    if filters.get("portsOff"):
        where.append("NOT (COALESCE(d.portfolio, '—') = ANY(%(portsOff)s))")
        args["portsOff"] = filters["portsOff"]
    if filters.get("q"):
        where.append("strpos(lower(concat_ws(' ', d.name, d.instrument, d.\"positionId\", d.book, d.issuer)),"
                     " lower(%(q)s)) > 0")
        args["q"] = filters["q"].strip()
    if position:
        where.extend(['d.book = %(position_book)s', 'd."positionId" = %(position_id)s'])
        args.update(position_book=position[0], position_id=position[1])
    # Project PnL on the same scan as dimensions; a second run_position join repeats work at year scale.
    projected = DIMENSIONS.replace('p.no_pnl AS "noPnl"', 'p.no_pnl AS "noPnl", p.pnl')
    sql = books_sql + ", dimensions AS (" + projected + ") SELECT d.* FROM dimensions d" \
        + (" WHERE " + " AND ".join(where) if where else "")
    # Server cursor bounds backend memory; no raw bulk response and no cache subsystem.
    with con.transaction(), con.cursor(name="pnl_history_rows", row_factory=dict_row) as cursor:
        cursor.execute(sql + ' ORDER BY d.date, d.book, d."positionId"', args)
        anchor_rows = []

        def period_rows():
            for row in cursor:
                if separate and row["runId"] == anchor["run_id"]:
                    anchor_rows.append(row)
                else:
                    yield row

        out = summarize(period_rows(), start, end, selected, detail=position is not None)
    if separate:
        out["separateAnchor"] = {"run": {"date": anchor["business_date"], "prevDate": anchor["prev_date"],
                                        **run_slice(anchor)}, "overlappingDates": overlapping,
                                 **summarize(anchor_rows, anchor["business_date"], anchor["business_date"], selected,
                                             detail=position is not None)}
    if catalogue:
        out["catalogue"] = con.execute(cte + 'SELECT DISTINCT book, "bookName", "positionId", portfolio, entity,'
                                      ' desk, ccy, "assetClass", issuer FROM dimensions', {"ids": ids}) \
            .fetchall()
    market = [r["business_date"] for r in con.execute(
        "SELECT business_date FROM md_kbd_day WHERE business_date BETWEEN %s AND %s ORDER BY 1", (start, end))]
    return {"from": start, "to": end, "anchorRunId": anchor["run_id"], "currency": "RUB",
            "marketDates": market, "intradayOnlyDates": intraday_only,
            "runs": [{"date": r["business_date"], "runId": r["run_id"], "prevDate": r["prev_date"],
                      **run_slice(r)} for r in runs if not separate or r["run_id"] != anchor["run_id"]], **out}
