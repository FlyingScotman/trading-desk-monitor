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

``status`` is the card «История прогонов»: per index code the stored range and rows, and the state of the loading.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import psycopg

from .bundle import Snapshot

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


_MEMO: dict[tuple[str, date | None], FixingStore] = {}
"""The last ``load`` per (database, since): a backfill runs hundreds of dates on one stored history."""


def _stamp(con: psycopg.Connection) -> tuple:
    return tuple(str(x) for x in con.execute(
        "SELECT count(*), max(fetch_id), max(fixing_date), current_database() FROM md_hub_fixing").fetchone())


def load(con: psycopg.Connection, since: date | None = None) -> FixingStore:
    """``md_hub_fixing`` rows with a value from ``since`` on (default: all); the rows read last are reused while the
    table's stamp (rows, newest fetch, newest date) is the same."""
    stamp = _stamp(con)
    key = (stamp[-1], since)
    if (hit := _MEMO.get(key)) is not None and hit.stamp == stamp:
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
    _MEMO.clear()  # one history in memory at a time
    _MEMO[key] = out
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
    for code, rows in sorted(own.items()):
        stored = store.rows.get(code)
        if not stored:
            continue
        start = min(f["fixing_date"] for f in rows)
        sources = {f["source"] for f in rows}
        fill = [f for f in stored if depth <= f["fixing_date"] < start and f["source"] in sources]
        if not fill:
            continue
        added += fill
        codes[code] = {"rows": len(fill), "from": fill[0]["fixing_date"], "to": fill[-1]["fixing_date"],
                       "window_from": start, "sources": sorted({f["source"] for f in fill})}
    lineage = {"source": SOURCE_ID, "depth_from": depth, "codes": codes}
    if not added:
        return snap, lineage
    data = {**snap.data, "fixings": [*added, *snap.data["fixings"]]}
    return replace(snap, doc={**snap.doc, "data": data, "fixings_history": codes}), lineage


def filled(snap: Snapshot, code: str) -> dict[str, Any] | None:
    """The lineage of ``extend`` for one index of a filled cut, None when it was not filled."""
    return (snap.doc.get("fixings_history") or {}).get(code)


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
