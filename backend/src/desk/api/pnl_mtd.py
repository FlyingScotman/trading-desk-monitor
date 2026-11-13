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

FACTORS = {"carry": ("carry", "rolldown"), "funding": ("funding",), "rates": ("rates", "convexity"),
           "spread": ("spread",), "fx": ("fx_gross", "fx_funding"), "trading": ("new_trades",),
           "other": ("other", "reported")}
"""The pages' PnL factors from the report's components (as ``publish.summary.PNL``, the reported PV with ``other``)."""
DIMENSIONS = ("book", "bookName", "positionId", "name", "instrument", "ccy", "assetClass", "issuer", "portfolio",
              "entity", "desk")


NARROW_PNL = ("CASE WHEN p.pnl IS NULL THEN NULL ELSE jsonb_build_object("
              + ", ".join(f"'{k}', p.pnl->'{k}'" for k in PNL_KEYS)
              + ", 'extra', jsonb_build_object('method', p.pnl->'extra'->'method')) END")
"""The row's ``pnl`` as ``pnl_history.amounts`` reads it: the components and ``extra.method``."""


class Acc:
    """One position's sums: the total of the year and of the month, the month's factors."""

    def __init__(self) -> None:
        self.ytd, self.mtd = Sum(), Sum()
        self.parts = {k: Sum() for k in FACTORS}
        self.row: dict[str, Any] = {}
        self.open = False
        self.last: tuple[Any, int] | None = None
        """The newest (date, run id) of the year's rows of the position (``totals`` reads its dimensions from there)."""

    def copy(self) -> Acc:
        out = Acc()
        out.ytd, out.mtd = self.ytd.copy(), self.mtd.copy()
        out.parts = {k: s.copy() for k, s in self.parts.items()}
        out.row, out.open, out.last = self.row, self.open, self.last
        return out

    def merge(self, other: Acc) -> None:
        self.ytd.merge(other.ytd)
        self.mtd.merge(other.mtd)
        for k in FACTORS:
            self.parts[k].merge(other.parts[k])

    def result(self) -> dict[str, Any]:
        return {"mtd": self.mtd.value(), "ytd": self.ytd.value(),
                "mtdParts": {k: s.value() for k, s in self.parts.items()}}


class PeriodCache:
    """The sums of the period's runs *before* the run shown, kept between builds (TDM#736).

    ``totals`` read every position row of the year (400 thousand rows and more at the owner's size, about 11 s) for each
    new run, though a new intraday run of a date changes the sums only by its own rows: the runs of the earlier dates are
    complete and never change. So the running sums over those runs (the year's and the month's) are kept as they stood
    after the last of them, and a build goes on from there with the run's own rows. The same ``Sum`` (compensated) adds
    the same values in the same order (run id, as the scan's ``ORDER BY``), so the result is the one a scan of the whole
    year gives, bit for bit.

    A state is valid for exactly the runs it was built over: the key is the pass, the period's start and the runs' ids
    with their completion times in order. A date recomputed since (a new run in the place of its old one) or an earlier
    date added is another key; of the kept states the one over the longest start of the wanted runs is gone on from (the
    next day's close adds one run to the end), else the rows are read again. A run that is not the newest of the period
    (asked for by its id, with a later run id among the period's) has the later runs' rows read after its own, as the
    scan did. A run that is deleted, or a store replaced, is no run of the key any more. Thread safe."""

    SIZE = 4
    """States kept, least recently used dropped: the year's and the month's of the run shown and of the one before (about
    16 MB each at 2000 positions)."""

    def __init__(self, size: int = SIZE) -> None:
        import threading

        self.size = size
        self._items: dict[tuple, dict[tuple[str, str], Acc]] = {}
        self._lock = threading.Lock()
        self.hits = self.extended = self.misses = 0

    def find(self, slot: str, start: date, runs: tuple) -> tuple[dict[tuple[str, str], Acc], int]:
        """A copy of the state over the longest start of ``runs`` that is kept, to go on from, and how many runs it
        covers (an empty state and 0 when none is)."""
        with self._lock:
            best, n = None, 0
            for key in self._items:
                if key[0] == slot and key[1] == start and n < len(key[2]) <= len(runs) and runs[:len(key[2])] == key[2]:
                    best, n = key, len(key[2])
            if best is None:
                self.misses += 1
                return {}, 0
            if n == len(runs):
                self.hits += 1
            else:
                self.extended += 1
            self._items[best] = self._items.pop(best)  # the most recently used
            return copy_state(self._items[best]), n

    def put(self, slot: str, start: date, runs: tuple, state: dict[tuple[str, str], Acc]) -> None:
        """Keep a copy of ``state`` (over ``runs``)."""
        key = (slot, start, runs)
        kept = copy_state(state)
        with self._lock:
            self._items.pop(key, None)
            self._items[key] = kept
            while len(self._items) > self.size:
                del self._items[next(iter(self._items))]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"items": len(self._items), "hits": self.hits, "extended": self.extended, "misses": self.misses}


def copy_state(state: dict[tuple[str, str], Acc]) -> dict[tuple[str, str], Acc]:
    return {k: x.copy() for k, x in state.items()}


def _scan(con, ids: list[int], year: bool, run_id: int, skip: int | None, acc: dict[tuple[str, str], Acc]) -> None:
    """Add the rows of the runs ``ids`` (read in run id order) to ``acc``: the year's pass (``ytd``, ``last``: the
    newest (date, run) of each position, ``open``: the position is in run ``run_id``) or the month's (``mtd`` and the
    factors). The run ``skip`` is reported apart: not in the sums."""
    if not ids:
        return
    sql = (f'SELECT p.run_id AS "runId", p.book_id AS book, p.position_id AS "positionId", {NARROW_PNL} AS pnl,'
           " r.business_date AS date FROM run_position p JOIN run r USING (run_id) WHERE p.run_id = ANY(%s)")
    with con.transaction(), con.cursor(name="pnl_mtd_rows", row_factory=dict_row) as cursor:
        cursor.execute(sql + " ORDER BY p.run_id", (ids,))
        for r in cursor:
            key = (r["book"], r["positionId"])
            x = acc.get(key)
            if x is None:
                x = acc[key] = Acc()
            if year:
                x.last = max(x.last or (r["date"], r["runId"]), (r["date"], r["runId"]))  # newest date, then run
                if r["runId"] == run_id:
                    x.open = True
            if r["runId"] == skip:
                continue
            a = amounts(r["pnl"])
            if year:
                x.ytd.add(a["total"])
            else:
                x.mtd.add(a["total"])
                for k, comps in FACTORS.items():
                    for c in comps:
                        x.parts[k].add(a["components"].get(c))


def _period(con, cache: PeriodCache | None, slot: str, start: date, scope: dict[str, Any], run: dict[str, Any],
            skip: int | None) -> dict[tuple[str, str], Acc]:
    """The sums of one pass (``slot``: ``"y"`` the year's, ``"m"`` the month's) over the period's runs (``scope``:
    ``pnl_history.period_runs``): the runs before the run shown from ``cache`` (read, and kept there, when it has no
    state for them), then the run's own rows and those of the runs after it in id order, read."""
    rid, year = run["run_id"], slot == "y"
    ordered = sorted(scope["runs"], key=lambda r: r["run_id"])
    acc: dict[tuple[str, str], Acc] = {}
    if cache is None:
        _scan(con, [r["run_id"] for r in ordered], year, rid, skip, acc)
        return acc
    before = [r for r in ordered if r["run_id"] < rid]
    ident = tuple((r["run_id"], r["completed_at"]) for r in before)
    acc, n = cache.find(slot, start, ident)
    if n < len(before):
        _scan(con, [r["run_id"] for r in before[n:]], year, rid, None, acc)
        cache.put(slot, start, ident, acc)
    _scan(con, [r["run_id"] for r in ordered if r["run_id"] >= rid], year, rid, skip, acc)
    return acc


def totals(con, run: dict[str, Any], as_of: date, cache: PeriodCache | None = None) -> dict[str, Any]:
    """``positions``: ``{(book, position id): Acc}`` over the year to ``as_of`` (the run's date); ``separate``: whether
    the run is reported apart from the month's / the year's sums. ``cache``: the sums of the runs before the run kept
    from the last build (``PeriodCache``, TDM#736); the same numbers without it."""
    year, month = as_of.replace(day=1, month=1), as_of.replace(day=1)
    sy, sm = period_runs(con, run, year, as_of), period_runs(con, run, month, as_of)
    skip = {"y": run["run_id"] if sy["separate"] else None, "m": run["run_id"] if sm["separate"] else None}
    acc = _period(con, cache, "y", year, sy, run, skip["y"])
    for key, m in _period(con, cache, "m", month, sm, run, skip["m"]).items():  # every month's run is a year's
        acc[key].mtd, acc[key].parts = m.mtd, m.parts
    # the dimensions of the closed positions, from the last run that had them
    gone = closed_keys(acc)
    if gone:
        dsql, dargs = rows_query({}, None, "1 AS one")
        dargs["ids"] = sorted({acc[k].last[1] for k in gone})  # type: ignore[index]
        for r in con.execute(dsql, dargs):
            key = (r["book"], r["positionId"])
            if key in gone and acc[key].last[1] == r["runId"]:  # type: ignore[index]
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
    return [{**{k: acc[key].row.get(k) for k in DIMENSIONS}, **acc[key].result()} for key in sorted(closed_keys(acc))]
