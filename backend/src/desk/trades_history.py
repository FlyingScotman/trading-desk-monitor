"""Trades of past dates (TDM#569): what the background worker of ``desk.adapter_services`` fetches, in which order, how
each answer is marked, and the summary the settings page shows. The PnL of the trades of a past date (``new_trades``,
TDM#557) needs the trades of that date in the store; the feed of the trades of the day asks the service for today only.
The worker asks the same feed for one past date at a time (``date``), the files go through the ordinary path, and the
existing steps of the autopilot (eod for the latest dates, backfill for the older ones) make the runs again by the
changed ``private_inputs_hash``. This module has no thread and never calls the helper: the service does, and writes
the file (``<home>/adapter-services/trades_history.json``, under its lock, atomically); no value of a trade is in it.

**Switch.** On by default (owner, 04.11.2026): ``enabled``, ``from`` (the first date; by default the 1st of January of
the current year but not before the start of the history, ``default_from``; never before the setting ``backfill_from``,
since a date the backfill does not reach is not made again by anyone, nor the adapter's ``history_from``), ``when``:
``any`` (default) or ``off_hours`` (not in the trading hours, ``autopilot.in_trading_hours``). A feed that does not
declare ``history`` (an adapter without it, a source off) gives the worker nothing to ask: it stays silent, and no
request, file or load comes of the default.

**Dates.** The candidates are the dates of the default runs in [from, yesterday] (and not before the adapter's
``history_from``), yesterday from ``HISTORY_DAY_FROM`` on. Today is never asked: the feed of the day has it. The order
is by blocks of ``BLOCK`` candidates, from the newest block to the older ones, inside a block from the oldest date to
the newest: the trades of a book without a report enter the start of the next days (``trades.MAX_HISTORY_DAYS``), so
the older date goes first and the newer ones are made again once, not up to eight times. The blocks are fixed by the
candidates (not by what is still to ask), so a block is finished before the next one starts. A date whose trades set
has a report time after its own end on the Moscow clock is covered (``fetched``/``covered``): no request.

**Marks** (by date; ``mark_for``) and when a date is asked again (``due``):

=============  ==========================================================  ==========================================
``fetched``    the files are taken in, the answer had records               the newest ``REFRESH_BD`` candidates once
                                                                            a calendar day, the older ones never
``no_trades``  ``complete``/``no_records``                                  as ``fetched``
``no_books``   no book of the owner has positions on the date               when the position sets of the date change
``unmapped``   ``partial``/``books_unmapped``, or no unit taken in at all,   a new adapter fingerprint
               or ``complete``/``no_records`` of a cut the adapter marked
               ``filtered`` while no other date has brought records
               (``filtered_empty``: asked once more when some date has)
``unavailable````error``/``date_unavailable`` (or ``partial`` too often)    after 1, 3, 7 days, then a new fingerprint
``partial``    ``partial``/``export_incomplete``, files not all taken in     after 5 and 15 minutes; the third failure:
                                                                            ``unavailable``
=============  ==========================================================  ==========================================

No mark: a login or a setting is wanted (``wait``: the whole worker waits for the feed's login or a describe), an error
of the network, the helper or the store, ``skipped``, the cycle lock busy (the files wait as ``pending``, a list, and are taken
in at the next step, or, while the autopilot's backfill holds the lock, by it between two of its dates; another date is asked
meanwhile until ``PENDING_MAX`` files wait): ``failures``/``retry_at`` hold the whole worker back by ``BACKOFF_MIN``.
A cut by the chosen portfolios (``filtered``, 08 §2.2) that answers «no records» proves nothing about the books: with
no date of the service holding records yet (``service_proven``) it is ``unmapped``/``filtered_empty``, asked again only
by a new adapter fingerprint (never in a loop); once any date has records the same answer is ``no_trades``.

**Trades first** (``today_gate``). The worker asks no past date while the feed's source (``feed["source"]``) has brought no
record in the store for any of the last ``WORK_DAYS`` working days, today included (``work_days``; a Saturday or a
Sunday are skipped, the holidays are not known): a service that answered nothing for the whole week answers nothing for
the past, and every request occupies the adapter. A night, a holiday or a quiet day do not close the gate, the records
of an earlier day of the week keep it open. While it is closed only the dates the owner asked again (``priority``,
those that are candidates) are asked. The gate is ``no_trades_week``.

Limits: one date in ``HISTORY_GAP_S`` seconds, ``HISTORY_PER_DAY`` requests a day, none while a feed is due within the
time of the last request (``Service._feed_due_soon``), none while ``PENDING_MAX`` files wait for the cycle lock. The pace is
the gap's, not the autopilot's cycle's: the files are taken in between the dates of the backfill (``adapter_services``,
``take_in_history``), so a date asked again does not wait for the end of a cycle that holds the lock for ten minutes.

**The window of the query (TDM#715).** An adapter before ``WINDOW_FIXED`` (the private adapter 0.9.16) asked the service
for the trades of a date with the window shifted a day back (the setting «местное время со смещением»): the set of the
date D held the trades of D-1, and a book without records in a later cut kept them (a cut by the chosen portfolios
replaces only the books it has records for). The monitor mends it by itself, once, with no action of the owner and also
while the fetch of past dates is switched off: the first time it sees an adapter that is not before ``WINDOW_FIXED``
(``note_window``) it writes ``window`` into the file: the time (``since``), the newest ``dataset_set`` id (``set_id``:
the sets up to it are of the older adapter) and the number of the dates received (``total``). Then (a) a mark ``fetched``
(``covered`` too) or ``no_trades`` made before ``since`` is a date to ask again (``stale``, ``due``): the asking is as
slow as the fetch of the past (one date in ``HISTORY_GAP_S``), the newest block first; a date whose asking again failed
(``partial``, ``unavailable``, ``unmapped``) is still owed (``redo``) and retried by the usual rules; (b) the complete
answer of such a date, and of today's feed, puts an empty revision (``store.retire_old_trades``,
``adapter_services._ingest``) on every unit (book) of the date that was asked for, has no records in the answer and
whose current set is of the older adapter: for these books the date is the answer whole, the older revisions stay in the
history, a book that was not asked for is not touched. «Exported» counts only when the date is marked received; «no
records» only once the adapter with the right window has brought records for some date (``window_proven``): until then the
mark is held (``hold``) and the date asked again then. Files of the older adapter that wait for the cycle lock
(``pending``) are dropped by the sight. A mark made after ``since`` is never stale: nothing is
asked twice, and a second start does nothing. The PnL of a date follows by itself: the stamp of the inputs
(``backfill.inputs_stamp``, per date: the trades sets of the 60 days up to it, the trade terms in force) says the inputs of
the date changed, the eod and backfill steps make it again, those whose trades the answer changed first
(``backfill.run``).

``summary`` is what ``GET /api/v1/adapter-services/history`` returns: the counts by mark, what the runs say of the dates
(``applied``: how far the trades are in the PnL, read from the lineage of the date's default run), the problems.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

import psycopg

from . import backfill, fsretry, store
from .autopilot import PRELIM_FROM, in_trading_hours
from .inbox.timesem import MSK
from .settings import Settings

VERSION = 1
FILE = "trades_history.json"
HISTORY_GAP_S = 120.0
PENDING_MAX = 4
"""The files of dates asked that wait for the cycle lock (``pending``): no more are asked while so many wait, up to that
the worker goes on with the next date, the lock being busy for ten minutes at a time while the autopilot backfills."""
HISTORY_PER_DAY = 300
REFRESH_BD = 3
BLOCK = 10
BACKOFF_MIN = (5, 15, 60)
MAX_ATTEMPTS = 3
UNAVAILABLE_DAYS = (1, 3, 7)
HISTORY_DAY_FROM = dtime(7, 0)
WHEN = ("off_hours", "any")
MARKS = ("fetched", "no_trades", "no_books", "unmapped", "unavailable", "partial")
WAITS = ("login_required", "config_invalid", "source_disabled")
WORK_DAYS = 5
WINDOW_FIXED: dict[str, tuple[int, ...]] = {"tda": (0, 9, 16)}
"""The first version of an adapter (by its ``describe()["name"]``) whose query of the trades of a date has the right
window; before it the sets of a date hold the trades of the day before (the module doc). An adapter not in the table, or a
version that is not read, is taken for right: nothing is asked again for it."""
COMPLETENESS = ("no_pnl", "lost", "unpriced", "no_prev_close")
"""The keys of the run's lineage ``intraday_trades`` that say how much of the trades is outside the PnL (a count or a
list or ``{"n": count, "sum": amount}``); without any of them the screen says that it has no information."""


# ---- the file ------------------------------------------------------------------------------------------------------

def path(st: Settings) -> Path:
    return Path(st.home) / "adapter-services" / FILE


def blank() -> dict:
    return {"version": VERSION, "enabled": True, "from": None, "when": "any", "marks": {}, "priority": [],
            "pending": [], "window": None, "calls": {"day": None, "n": 0}, "last_call": None, "last_duration_s": None,
            "wait": None, "failures": 0, "retry_at": None, "last": None}


def read(st: Settings) -> dict:
    try:
        p = path(st)
        doc = json.loads(fsretry.retry(lambda: p.read_text(encoding="utf-8"), fsretry.REPLACE_WAIT_S))
        if isinstance(doc, dict) and doc.get("version") == VERSION and isinstance(doc.get("marks"), dict):
            out = {**blank(), **doc}
            out["pending"] = pending_of(out)
            return out
    except (OSError, ValueError):
        pass
    return blank()


def pending_of(doc: dict) -> list[dict]:
    """The files that wait for the cycle lock, the oldest first: a list (a file written by the version that held one
    waiting date has a dict, ``None`` or no key)."""
    p = doc.get("pending")
    return [x for x in (p if isinstance(p, list) else [p]) if isinstance(x, dict)]


def write(st: Settings, doc: dict) -> None:
    """Atomic; the caller holds the service's lock (the worker thread and the routes both write)."""
    p = path(st)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
        fsretry.replace(tmp, p)  # again while a reader holds the file (Windows), up to 2 s
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def default_from(settings: Settings, today: date) -> date:
    """The start of the year, but not before the start of the history (the setting ``backfill_from``)."""
    return max(date(today.year, 1, 1), backfill.start_date(settings))


def lower_bound(settings: Settings, doc: dict, history_from: str | None, today: date) -> date:
    """The first date asked: the owner's ``from`` (default ``default_from``), never before the setting
    ``backfill_from`` nor the adapter's ``history_from``."""
    d = date.fromisoformat(doc["from"]) if doc.get("from") else default_from(settings, today)
    d = max(d, backfill.start_date(settings))
    return max(d, date.fromisoformat(history_from)) if history_from else d


# ---- the window of the query (TDM#715) -----------------------------------------------------------------------------

def version_tuple(v: Any) -> tuple[int, ...] | None:
    """``(0, 9, 16)`` of ``"0.9.16"`` (a suffix after the digits is ignored), None for what is not a version."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", v) if isinstance(v, str) else None
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def window_fixed(name: Any, version: Any) -> bool:
    """Is the adapter one whose query of the trades has the right window (``WINDOW_FIXED``)?"""
    need, have = WINDOW_FIXED.get(name) if isinstance(name, str) else None, version_tuple(version)
    return need is not None and have is not None and have >= need


def window_since(doc: dict) -> datetime | None:
    w = doc.get("window")
    return _ts(w.get("since")) if isinstance(w, dict) else None


def is_stale(mark: dict | None, since: datetime | None) -> bool:
    """A received date (``fetched``, ``covered`` too, or ``no_trades``) whose mark is older than the first sight of the
    adapter with the right window: asked again once."""
    if since is None or not mark or mark.get("state") not in ("fetched", "no_trades"):
        return False
    at = _ts(mark.get("at"))
    return at is None or at < since


def window_proven(doc: dict) -> bool:
    """Has the adapter with the right window brought records for a date (a mark ``fetched``, not ``covered``, made after
    the first sight of it by such an adapter)? The older adapter's marks do not count: an answer «no records» of a date
    empties the date only then (TDM#715)."""
    since = window_since(doc)
    if since is None:
        return False
    for m in doc.get("marks", {}).values():
        at = _ts(m.get("at"))
        name, _, version = str(m.get("adapter") or "").partition(" ")
        if m.get("state") == "fetched" and m.get("code") != "covered" and at is not None and at >= since \
                and window_fixed(name, version):
            return True
    return False


def owed(mark: dict | None, since: datetime | None) -> bool:
    """A date not yet asked again after the sight of the adapter with the right window: its mark is older (``is_stale``),
    held (an answer «no records» that could not empty the date yet) or the last asking ended in a failure (``redo``)."""
    return bool(mark) and (is_stale(mark, since) or bool(mark.get("hold")) or bool(mark.get("redo")))


def stale_dates(doc: dict) -> list[str]:
    """The dates the worker has to ask again also while the fetch of past dates is off: the older marks, the held ones
    once the adapter has proven the service, and those whose asking failed (retried by the usual rules)."""
    since, proven = window_since(doc), window_proven(doc)
    return sorted(d for d, m in doc.get("marks", {}).items()
                  if is_stale(m, since) or m.get("redo") or (m.get("hold") and proven))


def note_window(doc: dict, now: datetime, set_id: int, name: str, version: str) -> bool:
    """The first sight of an adapter with the right window written into ``doc`` (the module doc); False when it is
    written already."""
    if isinstance(doc.get("window"), dict) and doc["window"].get("since"):
        return False
    doc["window"] = {"since": now.isoformat(), "set_id": int(set_id), "adapter": f"{name} {version}",
                     "total": sum(1 for m in doc["marks"].values() if m.get("state") in ("fetched", "no_trades"))}
    doc["pending"] = []   # files of the older adapter that wait for the cycle lock are dropped: the date is asked again
    return True


# ---- marks ---------------------------------------------------------------------------------------------------------

def mark_for(state: str, code: str, *, files_ok: bool = True, units: int = 0, duplicate: bool = False,
             had_files: bool = True, filtered: bool = False, proven: bool = True) -> tuple[str | None, str]:
    """The mark an answer gives a date, and the code to show: ``(None, code)``: no mark (the module doc). ``files_ok``:
    every file the answer produced was taken in; ``units``: units taken in; ``duplicate``: a file was a copy of one
    taken in before; ``filtered``: the adapter says the cut was made by the chosen portfolios; ``proven``: some date has
    brought records already (``service_proven``)."""
    if state == "complete" and code == "no_records":
        if filtered and not proven:
            return "unmapped", "filtered_empty"   # nothing proves the filter is right: asked again by a new fingerprint
        return "no_trades", code   # by the code, not by the files: a file without units is not «not taken in»
    if state == "partial" and code == "books_unmapped":
        return "unmapped", code
    if state == "error" and code == "date_unavailable":
        return "unavailable", code
    if state == "partial" and code == "export_incomplete":
        return "partial", code
    if state in ("complete", "partial"):
        if not had_files:
            return "unmapped", "empty"
        if not files_ok:
            return "partial", "files_not_accepted"
        if units > 0 or duplicate:
            return ("fetched" if state == "complete" else "partial"), code
        return "unmapped", "empty"   # the adapter did not say why there is nothing: «пусто, причина не указана»
    return None, code


def service_proven(doc: dict) -> bool:
    """Has the service brought records for any date (a mark ``fetched`` of its answer, not a covering manual export)?"""
    return any(m.get("state") == "fetched" and m.get("code") != "covered" for m in doc.get("marks", {}).values())


def apply_mark(doc: dict, day: str, state: str, code: str, now: datetime, *, fingerprint: str | None,
               files: list[dict] | None = None, units: int = 0, sets: str | None = None, adapter: str | None = None,
               hold: bool = False) -> dict:
    """The mark of ``day`` written into ``doc`` with its attempts and next time (the module doc); the mark. ``adapter``:
    ``"<name> <version>"`` of the adapter that answered (``window_proven``). ``hold``: the answer «no records» of a date
    received with the older window could not empty it yet (the date is asked again once the service is proven)."""
    prev = doc["marks"].get(day) or {}
    since = window_since(doc)
    owes = owed(prev, since)
    attempts, next_at = 0, None
    if state == "partial":
        attempts = int(prev.get("attempts") or 0) + 1 if prev.get("state") == "partial" else 1
        if attempts >= MAX_ATTEMPTS:
            state, attempts = "unavailable", 0
        else:
            next_at = (now + timedelta(minutes=BACKOFF_MIN[attempts - 1])).isoformat()
    if state == "unavailable":
        attempts += int(prev.get("attempts") or 0) if prev.get("state") == "unavailable" else 0
        attempts += 1
        next_at = (now + timedelta(days=UNAVAILABLE_DAYS[attempts - 1])).isoformat() \
            if attempts <= len(UNAVAILABLE_DAYS) else None
    m = {"state": state, "code": code, "at": now.isoformat(), "attempts": attempts, "next_at": next_at,
         "fingerprint": fingerprint, "files": [{"file": x["file"], "sha256": x.get("sha256"), "status": x.get("status")}
                                               for x in (files or [])][:200], "units": units}
    if sets is not None:
        m["sets"] = sets
    if adapter:
        m["adapter"] = adapter
    if hold and state in ("fetched", "no_trades"):
        m["hold"] = True
    elif owes and state in ("partial", "unavailable", "unmapped"):
        m["redo"] = True   # a date received with the older window whose asking again failed is still owed (TDM#715)
    doc["marks"][day] = m
    return m


def due(mark: dict | None, idx: int, now: datetime, fingerprint: str | None, sets: str | None = None,
        proven: bool = False, stale: bool = False) -> bool:
    """Is the date to be asked now (the module doc)? ``idx``: the date's place among the candidates, the newest 0;
    ``proven``: some date has brought records (``service_proven``): a ``filtered_empty`` date is asked once more then
    (it cannot be ``filtered_empty`` again, so there is no loop); ``stale``: a date received with the older window
    (``is_stale``, or held and the service proven now): asked again, a covered one too (TDM#715)."""
    if mark is None:
        return True
    state = mark.get("state")
    if state in ("fetched", "no_trades"):
        if stale:
            return True
        if mark.get("code") == "covered":
            return False
        at = _ts(mark.get("at"))
        return idx < REFRESH_BD and (at is None or at.astimezone(now.tzinfo).date() < now.date())
    if state == "no_books":
        return mark.get("sets") != sets
    if state == "unmapped":
        return mark.get("fingerprint") != fingerprint or (proven and mark.get("code") == "filtered_empty")
    if state in ("unavailable", "partial"):
        if state == "unavailable" and mark.get("fingerprint") != fingerprint:
            return True
        na = _ts(mark.get("next_at"))
        return na is not None and now >= na
    return True


# ---- the candidates and the order ------------------------------------------------------------------------------------

def candidates(con: psycopg.Connection, settings: Settings, doc: dict, now: datetime, history_from: str | None
               ) -> list[str]:
    """The dates to bring up to date, the newest first: the default runs in [from, yesterday]; today never,
    yesterday from ``HISTORY_DAY_FROM`` on."""
    today = now.date()
    lo = lower_bound(settings, doc, history_from, today)
    last = today - timedelta(days=1 if now.time() >= HISTORY_DAY_FROM else 2)
    if lo > last:
        return []
    return sorted((d.isoformat() for d in store.default_runs(con, lo) if lo <= d <= last), reverse=True)


def sets_signature(con: psycopg.Connection, day: str) -> str:
    return ",".join(str(i) for i in store.current_sets(con, date.fromisoformat(day))[0])


def covered(con: psycopg.Connection, cands: list[str], doc: dict) -> list[str]:
    """Dates without a mark whose trades are in the store from a report made after the end of the date (its report time
    on the Moscow clock is a later date): a manual export; no request (the monitor does not know the adapter's end of
    day). The Moscow day is the fixed offset UTC+03:00 (as ``store.MSK_DAY``): the Windows bundle's database has no time
    zone data."""
    todo = [d for d in cands if d not in doc["marks"]]
    if not todo:
        return []
    rows = con.execute("SELECT DISTINCT valid_date FROM dataset_set WHERE dataset = 'trades' AND valid_date = ANY(%s)"
                       " AND (report_ts AT TIME ZONE INTERVAL '+03:00')::date > valid_date",
                       ([date.fromisoformat(d) for d in todo],)).fetchall()
    return sorted({r[0].isoformat() for r in rows})


def order(cands: list[str], todo: set[str], priority: list[str]) -> list[str]:
    """The dates to ask, in the order of the module doc: the blocks of ``BLOCK`` candidates, the newest block first,
    inside a block the oldest date first. The dates the owner asked again (``priority``) go before the rest."""
    pool = todo & set(priority) or todo
    out: list[str] = []
    for i in range(0, len(cands), BLOCK):
        out += sorted(d for d in cands[i:i + BLOCK] if d in pool)
    return out


def end_of_trading_hours(now: datetime) -> datetime:
    """The first moment from ``now`` on that is not in the trading hours."""
    t = now.astimezone(MSK)
    if not in_trading_hours(t):
        return now
    return datetime.combine(t.date(), PRELIM_FROM, MSK)


def day_start(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), dtime(0), now.tzinfo)


def gate(doc: dict, now: datetime, *, stale_only: bool = False) -> dict | None:
    """Why the worker does not ask now: ``{"code", "until"}`` (``until``: ISO time or None), or None (it may).
    ``stale_only``: the fetch is switched off, only the dates received with the older window are asked (TDM#715)."""
    if not doc.get("enabled") and not stale_only:
        return {"code": "off", "until": None}
    w = doc.get("wait")
    if isinstance(w, dict) and w.get("code") in WAITS:
        return {"code": w["code"], "until": None}
    retry = _ts(doc.get("retry_at"))
    if retry is not None and retry > now:
        return {"code": "backoff", "until": retry.isoformat()}
    if doc.get("when", "any") == "off_hours" and in_trading_hours(now):
        return {"code": "trading_hours", "until": end_of_trading_hours(now).isoformat()}
    calls = doc.get("calls") or {}
    if calls.get("day") == now.date().isoformat() and int(calls.get("n") or 0) >= HISTORY_PER_DAY:
        return {"code": "daily_limit", "until": day_start(now).isoformat()}
    last = _ts(doc.get("last_call"))
    if last is not None and (now - last).total_seconds() < HISTORY_GAP_S:
        return {"code": "gap", "until": (last + timedelta(seconds=HISTORY_GAP_S)).isoformat()}
    return None


def work_days(now: datetime, n: int = WORK_DAYS) -> list[date]:
    """The last ``n`` working days (Monday to Friday) up to ``now``, today included when it is one."""
    out, d = [], now.date()
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def recent_records(con: psycopg.Connection, source: str | None, days: list[date]) -> int:
    """The records of the source's current sets of the trades on ``days`` (per unit the latest set; a unit whose last
    export has no row adds nothing). The latest set of a unit is the whole truth of it only for a ``full_snapshot``
    source (every source of the trades is one now): for ``completeness = increment`` the sets add up and this is wrong."""
    return int(con.execute(
        "SELECT coalesce(sum(row_count), 0) FROM (SELECT DISTINCT ON (valid_date, scope_key) row_count FROM dataset_set"
        " WHERE dataset = 'trades' AND source = %s AND valid_date = ANY(%s)"
        f" ORDER BY valid_date, scope_key, {store.CURRENT_SET}) s", (source, days)).fetchone()[0])


def today_gate(con: psycopg.Connection, source: str | None, now: datetime) -> dict | None:
    """``{"code": "no_trades_week", "until": None}`` while the source has no record for the last ``WORK_DAYS`` working
    days (the module doc), else None."""
    return None if recent_records(con, source, work_days(now)) > 0 else {"code": "no_trades_week", "until": None}


def count_call(doc: dict, now: datetime) -> None:
    calls = doc.get("calls") or {}
    day = now.date().isoformat()
    doc["calls"] = {"day": day, "n": (int(calls.get("n") or 0) if calls.get("day") == day else 0) + 1}
    doc["last_call"] = now.isoformat()


def queue(con: psycopg.Connection, settings: Settings, doc: dict, now: datetime, history_from: str | None,
          fingerprint: str | None, *, stale_only: bool = False) -> dict:
    """What to ask: ``{"candidates", "covered", "order"}``. The dates ``covered`` get their mark from the caller.
    ``stale_only``: only the dates received with the older window (the fetch is off, TDM#715)."""
    cands = candidates(con, settings, doc, now, history_from)
    asked = set(doc.get("priority") or [])   # asked again by the owner: a covering set is no answer to that
    cov = [] if stale_only else covered(con, [d for d in cands if d not in asked], doc)
    todo: set[str] = set()
    proven = service_proven(doc)
    since, wproven = window_since(doc), window_proven(doc)
    for i, d in enumerate(cands):
        if d in cov:
            continue
        m = doc["marks"].get(d)
        stale = is_stale(m, since) or bool(m and m.get("hold") and wproven)
        if stale_only and not (stale or (m and m.get("redo"))):
            continue
        if due(m, i, now, fingerprint, sets_signature(con, d) if m and m.get("state") == "no_books" else None, proven,
               stale):
            todo.add(d)
    return {"candidates": cands, "covered": cov, "order": order(cands, todo, doc.get("priority") or [])}


def refetch_dates(settings: Settings, cands: list[str], doc: dict, lo: date, hi: date) -> list[str]:
    """The candidates in [lo, hi] that «Перезапросить период» would ask again: all but those with no books."""
    return [d for d in cands if lo.isoformat() <= d <= hi.isoformat()
            and (doc["marks"].get(d) or {}).get("state") != "no_books"]


# ---- what the runs say ---------------------------------------------------------------------------------------------

def count_of(v: Any) -> int | None:
    """A count from a lineage value: a number, a list, ``{"n"|"count": number, ...}`` or a dict (its size); else None."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, (list, tuple)):
        return len(v)
    if isinstance(v, dict):
        for k in ("n", "count"):
            if isinstance(v.get(k), (int, float)) and not isinstance(v.get(k), bool):
                return int(v[k])
        return len(v)
    return None


def completeness(it: dict) -> tuple[str, str | None]:
    """How far the trades of a date are in its PnL, from the lineage ``intraday_trades``: ``("full", None)``,
    ``("partial", reason)`` (some trades are outside the total), ``("outside", reason)`` (none is in: no previous close,
    no PnL asked, or every trade outside), ``("unknown", None)`` (the run says nothing of it). The run's
    ``outside_total`` has all the keys always: ``no_prev_close`` / ``no_pnl_run`` (flags), ``trades`` (the day's trades),
    ``no_pnl`` / ``lost`` / ``unpriced`` / ``unvalued`` (each with ``trades``). Without it (a run of an older program) the
    plain keys of ``COMPLETENESS`` are read, counts or lists."""
    ot = it.get("outside_total")
    if isinstance(ot, dict):
        if ot.get("no_prev_close"):
            return "outside", "no_prev_close"
        if ot.get("no_pnl_run"):
            return "outside", "no_pnl_run"
        out = {k: count_of((ot.get(k) or {}).get("trades") if isinstance(ot.get(k), dict) else ot.get(k)) or 0
               for k in ("no_pnl", "lost", "unpriced", "unvalued")}
        if not any(out.values()):
            return "full", None
        why = max(out, key=lambda k: out[k])
        return ("outside" if sum(out.values()) >= (count_of(ot.get("trades")) or 0) > 0 else "partial"), why
    vals = {k: count_of(it.get(k)) for k in COMPLETENESS}
    if all(v is None for v in vals.values()):
        return "unknown", None
    if (vals["no_prev_close"] or 0) > 0:
        return "outside", "no_prev_close"
    out = {k: vals[k] or 0 for k in ("no_pnl", "lost", "unpriced")}
    if not any(out.values()):
        return "full", None
    return "partial", max(out, key=lambda k: out[k])


def applied(con: psycopg.Connection, settings: Settings, doc: dict) -> dict:
    """The dates with trades taken in (marks ``fetched``), by what their default run says: ``full``, ``partial``,
    ``outside`` (none of the trades is in the total, with the reasons), ``unknown`` (the run has no completeness figures),
    ``waiting_recompute`` (the run read other inputs: the eod or backfill step makes it again), ``no_effect`` (the trades
    do not change the inputs: not bonds, all left out). ``in_pnl`` is ``full`` + ``partial``: dates where the PnL of
    the trades is in the total, whole or in part; ``outside`` and ``unknown`` are not counted in it."""
    days = sorted(d for d, m in doc["marks"].items() if m.get("state") == "fetched")
    out: dict[str, Any] = {"full": 0, "partial": 0, "outside": 0, "unknown": 0, "waiting_recompute": 0,
                           "no_effect": 0, "reasons": {}, "dates": {}}
    if days:
        dts = [date.fromisoformat(d) for d in days]
        runs = store.default_runs(con, min(dts))
        current: dict[date, list[int]] = {}   # the sets in force of each date's trades units (CURRENT_SET)
        for d_, sid in con.execute("SELECT DISTINCT ON (valid_date, scope_key) valid_date, set_id FROM dataset_set"
                                   " WHERE dataset = 'trades' AND valid_date = ANY(%s)"
                                   f" ORDER BY valid_date, scope_key, {store.CURRENT_SET}", (dts,)):
            current.setdefault(d_, []).append(sid)
        lin = {}
        for d_, it in con.execute(
                "SELECT business_date, jsonb_build_object('set_ids', lineage #> '{intraday_trades,set_ids}',"
                " 'applied', lineage #> '{intraday_trades,applied}', 'no_pnl', lineage #> '{intraday_trades,no_pnl}',"
                " 'lost', lineage #> '{intraday_trades,lost}', 'unpriced', lineage #> '{intraday_trades,unpriced}',"
                " 'no_prev_close', lineage #> '{intraday_trades,no_prev_close}',"
                " 'outside_total', lineage #> '{intraday_trades,outside_total}', 'has', lineage ? 'intraday_trades')"
                f" FROM run WHERE run_id IN ({store.DEFAULT_RUNS}) AND business_date = ANY(%s)", (dts,)):
            lin[d_] = it
        stamp = backfill.inputs_stamp(con, settings)
        for d in dts:
            run, it = runs.get(d), lin.get(d) or {}
            key = d.isoformat()
            if run is None:
                kind = "waiting_recompute"
            elif it.get("has") and current.get(d) and set(current[d]) <= set(it.get("set_ids") or []):
                if count_of(it.get("applied")) == 0:
                    kind, why = "no_effect", None   # nothing of the date's trades entered the book (not bonds, left out)
                else:
                    kind, why = completeness(it)
                if why:
                    out["reasons"][why] = out["reasons"].get(why, 0) + 1
            elif backfill.inputs_hash(con, d, settings, stamp) == run["private_inputs_hash"]:
                kind = "no_effect"
            else:
                kind = "waiting_recompute"
            out[kind] += 1
            out["dates"][key] = kind
    out["in_pnl"] = out["full"] + out["partial"]
    return out


def summary(con: psycopg.Connection | None, settings: Settings, doc: dict, *, now: datetime, feed: dict | None,
            fingerprint: str | None, running: dict | None, feed_state: dict | None = None) -> dict:
    """``GET /api/v1/adapter-services/history`` (the module doc). ``feed``: the history feed of the adapter or None;
    ``feed_state``: its run state in the service (the pause for a login)."""
    hist_from = (feed or {}).get("history_from")
    today = now.date()
    out: dict[str, Any] = {
        "available": feed is not None, "code": "available" if feed else "history_unsupported",
        "feed": feed["id"] if feed else None, "enabled": bool(doc.get("enabled")) and feed is not None, "when": doc.get("when", "any"),
        "from": lower_bound(settings, doc, hist_from, today).isoformat(), "history_from": hist_from,
        "from_min": max(backfill.start_date(settings), date.fromisoformat(hist_from) if hist_from else date.min).isoformat(),
        "from_default": default_from(settings, today).isoformat(),
        "backfill_enabled": bool(getattr(settings, "backfill", True)), "paused": False, "waiting": None,
        "counts": {k: 0 for k in (*MARKS, "todo", "covered")}, "dates": 0, "applied": None, "current": None,
        "last": doc.get("last"), "window": None, "hint": None, "next_at": None, "gate": None, "problems": [], "pending": len(pending_of(doc)),
        "pending_why": next((p["why"] for p in pending_of(doc) if isinstance(p.get("why"), str)), None),
        "calls_today": int((doc.get("calls") or {}).get("n") or 0) if (doc.get("calls") or {}).get("day") == today.isoformat() else 0,
        "limit_per_day": HISTORY_PER_DAY}
    w = doc.get("wait")
    out["waiting"] = w.get("code") if isinstance(w, dict) else None
    if feed is not None and not feed.get("ready"):
        out["waiting"] = out["waiting"] or "not_ready"
    if feed_state and feed_state.get("needs_login"):
        out["waiting"] = out["waiting"] or "login_required"
    out["paused"] = out["waiting"] is not None
    if running and running.get("kind") == "history":
        out["current"] = running.get("date")
    g = gate(doc, now, stale_only=feed is not None and bool(stale_dates(doc)))
    out["gate"] = g["code"] if g else None
    out["next_at"] = g["until"] if g else None
    if con is None:
        return out
    cands = candidates(con, settings, doc, now, hist_from)
    out["dates"] = len(cands)
    if g is None and out["enabled"] and not out["paused"] and feed is not None \
            and not (set(doc.get("priority") or []) & set(cands)):   # what the owner asked again goes anyway
        g = today_gate(con, feed.get("source"), now)
        out["gate"] = g["code"] if g else None
    for i, d in enumerate(cands):
        m = doc["marks"].get(d)
        if m is None:
            out["counts"]["todo"] += 1
            continue
        out["counts"][m["state"]] += 1
        if m.get("code") == "covered":
            out["counts"]["covered"] += 1
        if m["state"] in ("unavailable", "unmapped", "partial"):
            out["problems"].append({"date": d, "state": m["state"], "code": m.get("code"),
                                    "attempts": m.get("attempts"), "next_at": m.get("next_at")})
    out["problems"] = out["problems"][:50]
    c = out["counts"]   # every past date refused: more likely the settings than the dates (the adapter says so, 08 §2.2)
    out["hint"] = "all_unavailable" if c["unavailable"] >= 3 and not (c["fetched"] or c["no_trades"] or c["unmapped"]) \
        else None
    since = window_since(doc)
    left = sum(1 for d in cands if owed(doc["marks"].get(d), since))
    if left:   # the dates received with the older window not asked again yet (TDM#715)
        out["window"] = {"left": left, "total": max(left, int((doc.get("window") or {}).get("total") or 0))}
    out["applied"] = applied(con, settings, doc)
    out["applied"].pop("dates", None)
    return out
