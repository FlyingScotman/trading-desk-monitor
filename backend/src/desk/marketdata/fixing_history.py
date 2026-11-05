"""The stored history of the index fixings under a run's market cut (TDM#590; owner, 05.11.2026).

A market cut of the hub carries the fixings of a window only: an intraday cut ``intraday_fixing_lookback_days`` (200)
days, a close 400. A swap whose accrual period started before the window (an IRS on RUONIA with a one-year period)
needs the compounding from the start of the period: core refuses it (``core_missing_market_data``: no RUONIA fixing on
or before the period's start). The monitor keeps the fixings of every index the hub has (RUONIA, the key rate, the
RUSFAR family, the foreign overnight indices, ...) from ``history_rates_from`` (default 2010) in ``md_hub_fixing``:
the autopilot's history step loads them from the hub's history API (``/v1/history/fixings``, paged with a cursor,
``desk.marketdata.hubhistory``), the last weeks again every ``TAIL_POLL_S`` (corrections of the sources), each closed
year again once a day when the hub changed it.

A run takes the cut's fixings and fills in, per index the cut has, the stored rows dated **before the first fixing of
the cut's window** and no older than ``DEPTH_YEARS`` (``extend``): the cut's own rows are never replaced, a past
fixing is never changed. Only the sources the cut has for that index are taken (an index read from several sources
by a preference keeps its choice; one read from one source never gets a second row of a date). The filled cut is a
copy: the snapshot file is not changed, its id stays the run's market snapshot id. The lineage of the run says, per
index, how many rows came from the store and from which dates (``rates.fixings_history``), and core's fixing history
of an index filled so is named ``<market snapshot id>+md_hub_fixing`` (``fixing_snapshot_id`` of a swap's valuation).
Where the stored RUONIA reaches before the hub's calendar (``valid_from`` of the static's calendars), its publication
calendar there is the days with a fixing (``rates.published_before``), so those rows are not dropped.

``status`` is the card «История прогонов»: per index code the stored range and rows, and the state of the loading.
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from .adapter_rates import FIXING_SOURCE_ORDER
from .bundle import Snapshot

log = logging.getLogger("desk.marketdata.fixing_history")
_LOCK = threading.Lock()

DEPTH_YEARS = 10
"""A run takes stored fixings back to this many years before its business date (the owner: «на 10 лет»)."""
SOURCE_ID = "md_hub_fixing"
"""The table, and the suffix of core's fixing snapshot id of an index filled from it."""
KEY_CODES = ("RUONIA", "KEY_RATE", "RUSFAR", "RUSFARCNY", "SOFR", "ESTR")
"""The indices the card names first (core's: RUONIA and the key rate for swaps and REPO, RUSFAR, the foreign
overnight indices of the FX and CCS curves); the others are counted."""


@dataclass(frozen=True)
class FixingStore:
    """The stored fixings with a value, per index code, as snapshot rows (``index_code``, ``source``,
    ``fixing_date``, ``value`` as decimal text, ``unit``, ``effective_date``, ``published_on``, ``published_at``
    None, ``hub_ingested_at``, ``labels``, ``origin``), oldest first; ``stamp`` changes whenever the stored rows do."""

    rows: dict[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    stamp: tuple = ()


_MEMO: OrderedDict[tuple[str, date | None], FixingStore] = OrderedDict()
"""The ``load`` results last used per (database, since), at most ``MEMO_SIZE`` (an LRU): a backfill runs hundreds of
dates on one stored history, and a date of another year (its own depth) does not evict the autopilot's today."""
MEMO_SIZE = 4


def _stamp(con: psycopg.Connection) -> tuple:
    return tuple(str(x) for x in con.execute(
        "SELECT count(*), max(fetch_id), max(fixing_date), current_database() FROM md_hub_fixing").fetchone())


def load(con: psycopg.Connection, since: date | None = None) -> FixingStore:
    """``md_hub_fixing`` rows with a value from ``since`` on (default: all); the rows read last are reused while the
    table's stamp (rows, newest fetch, newest date) is the same."""
    stamp = _stamp(con)
    key = (stamp[-1], since)
    with _LOCK:
        if (hit := _MEMO.get(key)) is not None and hit.stamp == stamp:
            _MEMO.move_to_end(key)
            return hit
    by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for code, source, d, value, unit, eff, pub, ing, labels in con.execute(
            "SELECT index_code, source, fixing_date, value, unit, effective_date, published_on, hub_ingested_at, labels"
            " FROM md_hub_fixing WHERE value IS NOT NULL AND (%s::date IS NULL OR fixing_date >= %s)"
            " ORDER BY index_code, fixing_date, source", (since, since)):
        by[code].append({"index_code": code, "source": source, "fixing_date": d.isoformat(),
                         "value": str(value), "unit": unit,
                         "effective_date": eff and eff.isoformat(), "published_on": pub and pub.isoformat(),
                         "published_at": None, "hub_ingested_at": ing and ing.isoformat(),
                         "labels": labels if isinstance(labels, list) else [], "origin": SOURCE_ID})
    out = FixingStore({k: tuple(v) for k, v in by.items()}, stamp)
    with _LOCK:
        _MEMO[key] = out
        _MEMO.move_to_end(key)
        while len(_MEMO) > MEMO_SIZE:
            _MEMO.popitem(last=False)
    return out


def extend(snap: Snapshot, store: FixingStore, depth_years: int = DEPTH_YEARS) -> tuple[Snapshot, dict[str, Any]]:
    """The market cut with the stored fixings of its indices before its window (module doc) and the lineage
    ``{"source", "depth_from", "codes": {code: {"rows", "from", "to", "window_from", "sources"}}}`` (only the codes
    filled). Nothing to fill: the cut itself."""
    if snap.kind != "market" or not store.rows:
        return snap, {"source": SOURCE_ID, "codes": {}}
    bd = date.fromisoformat(snap.business_date)
    try:
        depth = bd.replace(year=bd.year - depth_years).isoformat()
    except ValueError:  # 29 February
        depth = bd.replace(year=bd.year - depth_years, day=28).isoformat()
    own: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in snap.data.get("fixings") or ():
        own[f["index_code"]].append(f)
    added: list[dict[str, Any]] = []
    codes: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for code, rows in sorted(own.items()):
        stored = store.rows.get(code)
        if not stored:
            continue
        try:
            fill = _fill(code, rows, stored, depth)
        except (ArithmeticError, KeyError, TypeError, ValueError) as e:  # one index's bad rows: that index unfilled
            errors[code] = f"{type(e).__name__}: {e}"
            continue
        if not fill:
            continue
        added += fill
        codes[code] = {"rows": len(fill), "from": fill[0]["fixing_date"], "to": fill[-1]["fixing_date"],
                       "window_from": min(f["fixing_date"] for f in rows),
                       "sources": sorted({f["source"] for f in fill})}
    lineage = {"source": SOURCE_ID, "depth_from": depth, "codes": codes, **({"errors": errors} if errors else {})}
    if not added:
        return snap, lineage
    data = {**snap.data, "fixings": [*added, *snap.data["fixings"]]}
    return replace(snap, doc={**snap.doc, "data": data, "fixings_history": codes}), lineage


def _fill(code: str, rows: list[dict[str, Any]], stored: tuple[dict[str, Any], ...], depth: str
          ) -> list[dict[str, Any]]:
    """The stored rows of one index that go under the cut's ``rows``: dated in [``depth``, the cut's first date), of a
    source the cut has and of the cut's unit (a row without a unit or in another one is never taken: the run reads
    percents, ``adapter_rates.fixings`` refuses the others), one per date (the source order of
    ``adapter_rates.FIXING_SOURCE_ORDER``, else the cut's first row's source first), every value a number."""
    start = min(f["fixing_date"] for f in rows)
    sources = {f["source"] for f in rows}
    units = {f.get("unit") for f in rows}
    if len(units) != 1 or None in units:
        raise ValueError(f"{code}: the cut's rows are in {sorted(map(str, units))}, not one unit")
    unit = next(iter(units))
    first = min(rows, key=lambda f: f["fixing_date"])["source"]
    order = FIXING_SOURCE_ORDER.get(code) or (first,)
    rank = {s: i for i, s in enumerate(order)}
    best: dict[str, dict[str, Any]] = {}
    for f in stored:
        if not (depth <= f["fixing_date"] < start) or f["source"] not in sources or f.get("unit") != unit:
            continue
        Decimal(str(f["value"]))  # raises on a value that is no number
        date.fromisoformat(f["fixing_date"])
        old = best.get(f["fixing_date"])
        key = (rank.get(f["source"], len(rank)), f["source"])
        if old is None or key < (rank.get(old["source"], len(rank)), old["source"]):
            best[f["fixing_date"]] = f
    return [best[d] for d in sorted(best)]


def filled(snap: Snapshot, code: str) -> dict[str, Any] | None:
    """The lineage of ``extend`` for one index of a filled cut, None when it was not filled."""
    return ((getattr(snap, "doc", None) or {}).get("fixings_history") or {}).get(code)


def for_date(con: psycopg.Connection, d: date) -> FixingStore:
    """``load`` for a run of ``d``: the stored rows from 1 January ``DEPTH_YEARS`` years before it."""
    return load(con, date(d.year - DEPTH_YEARS, 1, 1))


def try_for_date(con: psycopg.Connection, d: date) -> FixingStore | None:
    """``for_date`` in a transaction of its own; None, logged, when the store cannot read it (a scenario or a carry is
    then computed on the cut alone, not failed)."""
    try:
        with con.transaction():
            return for_date(con, d)
    except psycopg.Error as e:
        log.warning("stored fixings history not read: %s: %s", type(e).__name__, e)
        return None


def status(con: psycopg.Connection) -> dict[str, Any]:
    """The card «История прогонов» (TDM#590): ``{"indices": n, "rows", "from", "to", "codes": [{code, from, to, rows}]
    (the ``KEY_CODES`` present, then the other codes' count), "others": n, "jobs": {done, pending, errors, fetched_at,
    checked_at}, "api": {available, reason}}``. Dates, counts and codes only."""
    rows = con.execute("SELECT index_code, min(fixing_date), max(fixing_date), count(*) FROM md_hub_fixing"
                       " WHERE value IS NOT NULL GROUP BY 1 ORDER BY 1").fetchall()
    by = {r[0]: {"code": r[0], "from": r[1].isoformat(), "to": r[2].isoformat(), "rows": r[3]} for r in rows}
    jobs = con.execute("SELECT count(*) FILTER (WHERE status = 'done'), count(*) FILTER (WHERE status IN ('new',"
                       " 'partial')), count(*) FILTER (WHERE status = 'error'), max(fetched_at), max(checked_at)"
                       " FROM md_hub_job WHERE dataset = 'fixings'").fetchone()
    api = con.execute("SELECT code, reason FROM md_hub_state").fetchone()
    return {"indices": len(by), "rows": sum(x["rows"] for x in by.values()),
            "from": min((x["from"] for x in by.values()), default=None),
            "to": max((x["to"] for x in by.values()), default=None),
            "codes": [by[c] for c in KEY_CODES if c in by], "others": len(set(by) - set(KEY_CODES)),
            "jobs": {"done": jobs[0], "pending": jobs[1], "errors": jobs[2],
                     "fetched_at": jobs[3] and jobs[3].isoformat(), "checked_at": jobs[4] and jobs[4].isoformat()},
            "api": None if api is None else {"available": api[0] == "ok", "reason": api[1]}}
