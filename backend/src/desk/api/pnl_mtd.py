"""PnL month and year to date of the concept pages' header and position lists (TDM#568, audit D14/D15).

The same path as the PnL report of the history (``/api/v1/history/pnl/period``): the period's runs by
``pnl_history.period_runs`` (default end-of-day runs before the run, then the run itself; an intraday run whose PnL
interval overlaps a default run's is reported apart and is *not* in the sums, as in the report), every position row of
those runs by ``pnl_history.rows_query`` and the net total by ``pnl_history.amounts``, summed with the same compensated
``Sum``. So the month to date of the header is the report's, and both are the sum of the days of the history.

Unlike a sum over the positions of the run shown, a position sold earlier in the period is in it: ``closed`` lists
them (their dimensions and sums), so that nothing the period earned is lost. The month's components by the pages'
factors (``FACTORS``) are for the explain page.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from psycopg.rows import dict_row

from .pnl_history import Sum, amounts, period_runs, rows_query

FACTORS = {"carry": ("carry", "rolldown"), "funding": ("funding",), "rates": ("rates", "convexity"),
           "spread": ("spread",), "fx": ("fx_gross", "fx_funding"), "trading": ("new_trades",),
           "other": ("other", "reported")}
"""The pages' PnL factors from the report's components (as ``publish.summary.PNL``, the reported PV with ``other``)."""
DIMENSIONS = ("book", "bookName", "positionId", "name", "instrument", "ccy", "assetClass", "issuer", "portfolio",
              "entity", "desk")


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
        return {"mtd": self.mtd.value(), "ytd": self.ytd.value(),
                "mtdParts": {k: s.value() for k, s in self.parts.items()}}


def totals(con, run: dict[str, Any], as_of: date) -> dict[str, Any]:
    """``positions``: ``{(book, position id): Acc}`` over the year to ``as_of`` (the run's date); ``separate``: whether
    the run is reported apart from the month's / the year's sums."""
    year, month = as_of.replace(day=1, month=1), as_of.replace(day=1)
    sy, sm = period_runs(con, run, year, as_of), period_runs(con, run, month, as_of)
    skip = {"y": run["run_id"] if sy["separate"] else None, "m": run["run_id"] if sm["separate"] else None}
    in_month = set(sm["ids"])
    sql, args = rows_query({}, None, "p.pnl")
    args["ids"] = sy["ids"]
    acc: dict[tuple[str, str], Acc] = {}
    with con.transaction(), con.cursor(name="pnl_mtd_rows", row_factory=dict_row) as cursor:
        cursor.execute(sql + ' ORDER BY d.date, d.book, d."positionId"', args)
        for r in cursor:
            key = (r["book"], r["positionId"])
            x = acc.setdefault(key, Acc())
            x.row = r
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
    return {"positions": acc, "separate": {"mtd": sm["separate"], "ytd": sy["separate"]}}


def by_position(acc: dict[tuple[str, str], Acc]) -> dict[str, dict[str, Any]]:
    """The open positions' sums by position id (the pages key positions by it; the books of one id are added)."""
    merged: dict[str, Acc] = {}
    for (_book, pid), x in acc.items():
        if x.open:
            merged.setdefault(pid, Acc()).merge(x)
    return {pid: x.result() for pid, x in merged.items()}


def closed(acc: dict[tuple[str, str], Acc]) -> list[dict[str, Any]]:
    """The positions of the period that the run does not have (sold or redeemed earlier), with their sums."""
    return [{**{k: x.row.get(k) for k in DIMENSIONS}, **x.result()} for _key, x in sorted(acc.items())
            if not x.open and (x.mtd.n or x.ytd.n)]
