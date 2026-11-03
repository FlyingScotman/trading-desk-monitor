"""Read a source's accepted reports again, from the interface (TDM#553): what ``desk inbox replay`` does, as a background
job the owner starts with a button (C14 «Статус дня», the settings page, and «Сохранить и пересчитать» of the OCP row
types, TDM#554), never a command line.

* ``sources(con, st)``: the adapter's sources, each with its datasets (and their names), the newest business date it
  has accepted files for and how many.
* ``estimate(con, source, date_from, date_to)``: how many accepted files of the source the period holds and their
  dates (what a replay reads again).
* ``default_period(st)``: the previous business day (the exchange calendar of the cache, else Monday to Friday) twice.
* ``start(st, source, date_from, date_to, store_url, autopilot)``: the replay in the background (one at a time,
  ``adapter_jobs.Busy``). It takes the inbox scan lock as ``desk inbox replay`` does; while another scan holds it the job
  waits (``waiting``: «идёт скан, запущу после него») and tries again every ``RETRY_S`` for up to ``WAIT_S``, then
  gives up (``busy``). After the replay the autopilot is woken: a new revision of a report in force makes the runs it
  feeds run again (``backfill.late_why``, the intraday step too). The result: files by status, units, new revisions and
  the scan's own summary (codes and counts, never cell values), kept until the next replay, across a restart too
  (``<home>/adapter-jobs/replay.json``).
"""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import psycopg

from . import adapter_jobs
from .daystatus import DATASET_NAMES
from .inbox import scan
from .inbox.spi import source_datasets
from .inbox.static_index import CalendarUnavailable, business_calendar
from .inbox.timesem import MSK
from .settings import Settings

KEY = "replay"
RETRY_S = 5.0
WAIT_S = 1800.0
MAX_DAYS = 400  # the longest period one replay reads (a year and some): a typo of a year would read the whole archive

_FILES = """
SELECT valid_date, count(*) FROM (SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC) l
WHERE l.status = ANY(%(done)s) AND l.source = %(source)s
  AND (%(lo)s::date IS NULL OR l.valid_date >= %(lo)s) AND (%(hi)s::date IS NULL OR l.valid_date <= %(hi)s)
GROUP BY valid_date ORDER BY valid_date"""


def default_period(st: Settings, today: date | None = None) -> tuple[date, date]:
    """The previous business day, twice: the report of the day before, which today's runs take as their base."""
    today = today or datetime.now(UTC).astimezone(MSK).date()
    try:
        d = business_calendar(st.snapshot_cache, today).prev_business_day(today)
    except (CalendarUnavailable, OSError, ValueError):
        d = today - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return d, d


def estimate(con: psycopg.Connection, source: str, date_from: date | None, date_to: date | None) -> dict[str, Any]:
    """``{"files": n, "dates": [ISO date]}``: the accepted files of ``source`` with a business date in the period."""
    rows = con.execute(_FILES, {"done": list(scan.READ_DONE), "source": source, "lo": date_from,
                                "hi": date_to}).fetchall()
    return {"files": sum(n for _, n in rows), "dates": [d.isoformat() for d, _ in rows if d]}


def sources(con: psycopg.Connection | None, st: Settings) -> list[dict[str, Any]]:
    """The adapter's sources (``describe()["sources"]``) as the page offers them: ``{source, datasets, names, latest,
    files}``, ``latest`` the newest business date with an accepted file (None: none yet). Without the store, no dates."""
    h = scan.adapter(st)
    out = []
    for s in (h.describe or {}).get("sources", []) if h.ok else []:
        sid = s.get("source")
        if not isinstance(sid, str):
            continue
        ds = list(source_datasets(s))
        est = estimate(con, sid, None, None) if con is not None else {"files": None, "dates": []}
        out.append({"source": sid, "datasets": ds, "names": [DATASET_NAMES.get(d, d) for d in ds],
                    "latest": est["dates"][-1] if est["dates"] else None, "files": est["files"]})
    return out


def check_period(date_from: date, date_to: date) -> str | None:
    """Why the period cannot be replayed, or None."""
    if date_from > date_to:
        return "the first date is after the last one"
    if (date_to - date_from).days >= MAX_DAYS:
        return f"at most {MAX_DAYS} days at once"
    return None


def job(st: Settings) -> dict | None:
    return adapter_jobs.job(st.home, KEY)


def running() -> bool:
    return adapter_jobs.running(KEY) is not None


def start(st: Settings, source: str, date_from: date, date_to: date, store_url: str | None, autopilot: Any = None, *,
          reason: str | None = None, wait_s: float = WAIT_S, retry_s: float = RETRY_S) -> dict:
    """The replay in the background (module doc); ``adapter_jobs.Busy`` while one runs. ``reason``: what asked for it
    (shown with the result, e.g. the OCP row types saved)."""
    request = {"source": source, "from": date_from.isoformat(), "to": date_to.isoformat(), "reason": reason}

    def work(j: dict) -> None:
        if not store_url:
            j.update(state="failed", error="no store", error_code="no_store")
            return
        deadline = time.monotonic() + wait_s
        while True:
            with psycopg.connect(store_url, autocommit=True, connect_timeout=5) as con:
                if "estimate" not in j:
                    j["estimate"] = estimate(con, source, date_from, date_to)
                res = scan.replay(con, st, source, date_from, date_to)
            if not res.busy:
                break
            if time.monotonic() >= deadline:
                j.update(state="failed", waiting=False, error_code="busy",
                         error=f"another inbox scan held the lock for {wait_s / 60:g} min: try again later")
                return
            j["waiting"] = True  # «идёт скан, запущу после него»
            time.sleep(retry_s)
        j.update(waiting=False, ok=res.ok, adapter_ok=res.adapter.startswith("ok"), text=res.text(),
                 result={"files": dict(res.files), "units": dict(res.units), "revisions": res.revisions,
                         "books_revised": res.books_revised, "skipped": dict(res.skipped)})
        if not res.adapter.startswith("ok"):
            j.update(state="failed", error="the adapter is not loaded: nothing was read", error_code="adapter")
            return
        if autopilot is not None:
            try:
                autopilot.trigger()
                j["autopilot"] = True
            except Exception:  # noqa: BLE001 - the replay is done; the next cycle runs the dates
                j["autopilot"] = False

    return adapter_jobs._start(st.home, KEY, KEY, request, work)
