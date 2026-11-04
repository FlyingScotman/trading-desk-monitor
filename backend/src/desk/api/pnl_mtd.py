"""PnL month and year to date of the concept pages' position lists (TDM#568, audit D14/D15).

The same sums as the PnL report of the history (``/api/v1/history/pnl/period``): the period's runs by
``pnl_history.period_runs`` (default end-of-day runs before the run, then the run itself; an intraday run whose PnL
interval overlaps a default run's is reported apart and is *not* in the sums, as in the report), every position row of
those runs and the net total by ``pnl_history.amounts``, summed with the same compensated ``Sum``. A position's
``pnlMtd`` / ``pnlYtd`` are therefore the report's for that position, and the sum over all positions of the period
(the run's and ``closed``) is the report's total and the sum of the days of the history.

A position sold earlier in the period is not in the run: ``closed`` lists them (their dimensions and sums). The pages'
headers add ``closed`` (``pnlClosed``) to the run's positions through the same header filter (book, entity, desk,
currency, portfolio), so a header's MTD / YTD equals the report's total under the same filter.
The month's components by the pages' factors (``FACTORS``) are for the explain page.

The scan reads only the components and the method of ``pnl`` (not ``pnl.extra``'s inputs of D-1, a year of them is
megabytes) and no dimensions; the dimensions are read for the closed positions only, from their last run.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.rows import dict_row

from .pnl_history import Sum, amounts, period_runs, rows_query
from .runs import PNL_KEYS

FACTORS = {
    "carry": ("carry", "rolldown"),
    "funding": ("funding",),
    "rates": ("rates", "convexity"),
    "spread": ("spread",),
    "fx": ("fx_gross", "fx_funding"),
    "trading": ("new_trades",),
    "other": ("other", "reported"),
}
"""The pages' PnL factors from the report's components (as ``publish.summary.PNL``, the reported PV with ``other``)."""
DIMENSIONS = (
    "book",
    "bookName",
    "positionId",
    "name",
    "instrument",
    "ccy",
    "assetClass",
    "issuer",
    "portfolio",
    "entity",
    "desk",
)


NARROW_PNL = (
    "CASE WHEN p.pnl IS NULL THEN NULL ELSE jsonb_build_object("
    + ", ".join(f"'{k}', p.pnl->'{k}'" for k in PNL_KEYS)
    + ", 'extra', jsonb_build_object('method', p.pnl->'extra'->'method')) END"
)
"""The row's ``pnl`` as ``pnl_history.amounts`` reads it: the components and ``extra.method``."""


class Acc:
    """One position's sums: the total of the year and of the month, the month's factors."""

    def __init__(self) -> None:
        self.ytd, self.mtd = Sum(), Sum()
        self.parts = {k: Sum() for k in FACTORS}
        self.row: dict[str, Any] = {}
        self.open = False

    def merge(self, other: Acc) -> None:
        self.ytd.merge(other.ytd)
        self.mtd.merge(other.mtd)
        for k in FACTORS:
            self.parts[k].merge(other.parts[k])

    def result(self) -> dict[str, Any]:
        return {
            "mtd": self.mtd.value(),
            "ytd": self.ytd.value(),
            "mtdParts": {k: s.value() for k, s in self.parts.items()},
        }


def totals(con, run: dict[str, Any], as_of: date) -> dict[str, Any]:
    """``positions``: ``{(book, position id): Acc}`` over the year to ``as_of`` (the run's date); ``separate``: whether
    the run is reported apart from the month's / the year's sums."""
    year, month = as_of.replace(day=1, month=1), as_of.replace(day=1)
    sy, sm = period_runs(con, run, year, as_of), period_runs(con, run, month, as_of)
    skip = {"y": run["run_id"] if sy["separate"] else None, "m": run["run_id"] if sm["separate"] else None}
    in_month = set(sm["ids"])
    sql = (
        f'SELECT p.run_id AS "runId", p.book_id AS book, p.position_id AS "positionId", {NARROW_PNL} AS pnl,'
        " r.business_date AS date FROM run_position p JOIN run r USING (run_id) WHERE p.run_id = ANY(%s)"
    )
    acc: dict[tuple[str, str], Acc] = {}
    last: dict[tuple[str, str], tuple[Any, int]] = {}
    with con.transaction(), con.cursor(name="pnl_mtd_rows", row_factory=dict_row) as cursor:
        cursor.execute(sql + " ORDER BY p.run_id", (sy["ids"],))
        for r in cursor:
            key = (r["book"], r["positionId"])
            x = acc.setdefault(key, Acc())
            last[key] = max(
                last.get(key, (r["date"], r["runId"])), (r["date"], r["runId"])
            )  # newest date, then run
            if r["runId"] == run["run_id"]:
                x.open = True
            a = amounts(r["pnl"])
            if r["runId"] != skip["y"]:
                x.ytd.add(a["total"])
            if r["runId"] in in_month and r["runId"] != skip["m"]:
                x.mtd.add(a["total"])
                for k, comps in FACTORS.items():
                    for c in comps:
                        x.parts[k].add(a["components"].get(c))
    # the dimensions of the closed positions, from the last run that had them
    gone = closed_keys(acc)
    if gone:
        dsql, dargs = rows_query({}, None, "1 AS one")
        dargs["ids"] = sorted({last[k][1] for k in gone})
        for r in con.execute(dsql, dargs):
            key = (r["book"], r["positionId"])
            if key in gone and last[key][1] == r["runId"]:
                acc[key].row = r
    return {"positions": acc, "separate": {"mtd": sm["separate"], "ytd": sy["separate"]}}


def open_ids(acc: dict[tuple[str, str], Acc]) -> set[str]:
    return {pid for (_book, pid), x in acc.items() if x.open}


def closed_keys(acc: dict[tuple[str, str], Acc]) -> set[tuple[str, str]]:
    """The (book, position id) of the period whose position id the run does not have in any book, with a PnL."""
    now = open_ids(acc)
    return {key for key, x in acc.items() if key[1] not in now and (x.mtd.n or x.ytd.n)}


def by_position(acc: dict[tuple[str, str], Acc]) -> dict[str, dict[str, Any]]:
    """The run's positions' sums by position id (the pages key positions by it): all the period's rows of the id, also
    those of another book it was in earlier."""
    now = open_ids(acc)
    merged: dict[str, Acc] = {}
    for (_book, pid), x in acc.items():
        if pid in now:
            merged.setdefault(pid, Acc()).merge(x)
    return {pid: x.result() for pid, x in merged.items()}


def closed(acc: dict[tuple[str, str], Acc]) -> list[dict[str, Any]]:
    """The positions of the period that the run does not have (sold or redeemed earlier), with their sums."""
    return [
        {**{k: acc[key].row.get(k) for k in DIMENSIONS}, **acc[key].result()}
        for key in sorted(closed_keys(acc))
    ]
