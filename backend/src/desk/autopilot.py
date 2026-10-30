"""The autopilot (I4): hub snapshots, the inbox and the EOD runs kept current without an operator.

One cycle (``Autopilot.run_cycle``; ``desk cycle`` runs one in the foreground, ``desk serve`` a thread that runs one
every ``inbox_poll_seconds`` and on ``POST /api/v1/autopilot/run``):

1. **sync**: from the hub (when its address and token are set) the ``autopilot_sync_last`` latest business dates,
   each business date with positions in the last ``WINDOW_DAYS`` (and the latest one before) with the hub date
   before it (flash PnL), and for each inbox file waiting for a calendar the newest hub date on or before its
   report date; dates already cached are skipped, but rechecked once per ``RECHECK_S`` (a hub correction). Never
   a date older than the inbox lookback (``inbox_lookback_days``, less ``PREV_MARGIN_DAYS`` for the date before
   the first one; ``desk cycle --date`` apart), at most ``autopilot_sync_last + SYNC_EXTRA`` dates per cycle (the
   rest next cycle), and a date the hub has no snapshot for is not asked again for ``MISSING_TTL_S``. The
   snapshot drop folder, when set, is imported too. A hub error is the step's error: the cycle goes on with the
   cache. The hub down (no answer or a 5xx after the client's retries, ``HubUnavailable``): the sync stops there, and
   the later steps of the cycle (intraday, history, backfill) do not ask it again (intraday: reason ``hub_error``)
   until the next cycle, so a cycle spends one retry series on a hub that is down (#147); no answer at all is the
   hub's alone, a 5xx for a date's snapshot is that date's failure too (below: its retry waits). No answer for the
   same date ``NO_ANSWER_CYCLES`` cycles in a row while the date list answers: the date is passed over and the next
   date tells; it answers (or there is none): the date fails alone as below, so it backs off and the later dates
   load; no answer for it either: the hub is down (#217). A date
   whose snapshot cannot be fetched (another hub error than 401/403/404, a body that does not verify)
   fails alone (#20): it is named in the step's detail and ``failed_dates`` and asked again next cycle, the other dates
   are synced and the later steps run; after ``MAX_DATE_FAILURES`` such dates the rest wait for the next cycle. A
   failed date is then not asked again for ``RETRY_BACKOFF_S`` (growing with its attempts; per process, forgotten on
   success) and goes after the other dates when asked again, so a date that stays broken does not take the limit in
   every cycle (#146); the step lists it in ``problem_dates`` with its error, attempts and next attempt (a date of
   ``desk cycle --date`` is asked anyway). Then the bundles of the cache not loaded yet go into the store's market history (``desk.marketdata.history``:
   the bond charts; one query when there is nothing new).
2. **scan**: ``desk inbox scan`` (when inbox folders are set). Sync comes first: a ``start_of_day`` report needs a
   cached calendar covering its date and the day before, otherwise it waits (``waiting_calendar``); a report
   before the hub's oldest date (known once the sync read the hub's whole date list) that still has no calendar
   once the hub's oldest static snapshot is cached stops waiting: ``ignored_too_old``, ``before_hub_history`` (#27).
3. **eod**: for each cached ``eod_final`` business date from ``min(today - WINDOW_DAYS, latest positions date)``
   on (at most ``MAX_EOD_DATES``, the latest positions date always) that has positions in the store, the EOD run of
   ``desk eod`` when the date has no default run, or its default run read other position sets (a new revision of a
   book), another market snapshot (a correction) or another previous snapshot (the flash PnL), or an ``ocp`` report or
   ``cash_balances`` in force on the date were recorded after it read its inputs (``backfill.late_why``, TDM#181: they
   usually arrive in the evening, after the run); the latest positions
   date also when its default run was made by another ``desk.logic_version()`` (an upgrade: the older dates are the
   backfill step's, after the other steps; ``backfill.same_logic``: a new bundle build of the same code recomputes nothing). Oldest first, except that the latest positions date goes first while it has no
   default run at all (a first launch, a new day): the current position shows before the backlog is done (its model
   prices then carry spreads from the runs that existed when it ran). While the inbox is still draining (the scan
   left files waiting for a calendar and the sync left hub dates for the next cycle), only the latest positions
   date runs: the older dates run once the backlog is in, not once per cycle as each book's revision lands (R22).
   A date that failed is not retried until its inputs change. The step's detail also lists the latest positions date's bonds without an instrument: counts by
   reason and the ISINs (``inbox.scan.unmapped``, D6).
4. **intraday** (with the hub and the ``intraday`` setting, R11): on a business day from 10:00 MSK, today's book
   (the latest position sets, carried forward: ``store.positions_for``) valued on the hub's newest ``intraday``
   cut of today, from 19:45 on its ``eod_preliminary`` cut: the EOD code path (``cli.eod_run``) on another market
   snapshot, an INTRADAY run (a preliminary one is an EOD run, the date's default until the final). The step asks
   the hub itself, whatever the sync step's outcome (a failed older date never blocks it, #20; the hub found down
   this cycle does, ``hub_error``, #147): one list call
   (``latest=true``) at most once per 10-minute slot (``POLL_SLOT_S``, starting ``POLL_LAG_S`` after the slot
   times), and for the preliminary cut only until today has a run on it; a run is made
   only for a cut newer than the last run's, when the position sets changed, or when the previous close it is measured
   against changed (the previous day's ``eod_final`` published late or corrected). The flash PnL is measured against
   the previous business day's ``eod_final``, else the newest earlier final or preliminary close, and the run says
   which (``pnl_basis``). The step's status carries a machine-readable ``reason`` (``INTRADAY_REASONS``) next to its
   detail. Only the latest INTRADAY run of a date is kept (``store.prune_intraday``); intraday and
   preliminary bundles of dates older than ``KEEP_CUT_DAYS`` leave the cache (``cache.prune``) unless a shown run
   reads them. The next morning the date's ``eod_final`` EOD run (step 3) supersedes both.
   **morning** (#21, with or without the hub): on a business day from ``MORNING_FROM`` (07:00 MSK; earlier once a
   current position set has report date today, e.g. today's ``start_of_day`` report, valid the day before), until today has a run on its own market (an EOD run, an intraday cut's
   run), today's book (``store.current_sets``) valued on the previous business day's close in the cache (its
   ``eod_final``, else its ``eod_preliminary``): ``cli.eod_run(on_close=...)``, an INTRADAY run of today with
   ``cut_type`` ``prev_close``, lineage ``market_close`` {date, cut_type}, no flash PnL (``pnl_basis`` says why), its
   label «16.10 · закрытие 15.10» (``store.slice_label``). Made again only when the position sets, the close or the
   logic (``backfill.same_logic``) change, the earlier morning run deleted (``store.prune_intraday``); the first
   intraday cut's run deletes it the same way. While the inbox is still draining (as step 3, R22) it waits. No run
   when the cache lacks the previous business day's close, even with an older one: the issue asks for D-1's close;
   the ``intraday`` setting does not turn it off. Its status carries a ``reason`` (``MORNING_REASONS``).
5. **scenarios** (TDM#71): the exact repricing of every active scenario (presets, historical, the desk's own) on
   the newest default run, queued for the scenario worker of ``desk serve`` (``scenario.jobs.submit_all``): one job
   per run and definition, so a cycle without a new default run or scenario queues nothing; a job that failed on the
   run is not queued again (a scenario failing on its inputs runs once, not every cycle; a POST of it retries). The presets' comparison
   reads them (``GET /api/v1/runs/{run_id}/scenario-results``). A scenario the run cannot reprice (a curve it does not
   publish) is named in the detail; it is no error of the cycle.
6. **history** (with the hub and ``history_days`` > 0): market history older than the cache for the bond charts,
   from the hub's history API when it serves the token (``desk.marketdata.hubhistory``: bonds from
   ``history_bonds_from``, KBD, curves, fixings and FX from ``history_rates_from``; ``hubhistory.BUDGET_S`` seconds a
   cycle, resumable), else ``HISTORY_PER_CYCLE`` of the ``history_days`` latest hub dates per cycle, newest first,
   into the store's tables without caching the bundles (the step's detail says why the API was not used); only
   after a sync that reached the hub and left no date for the next cycle, and after the EOD runs, so it never delays
   them. The lookback above does not apply: these dates feed no run.
7. **backfill** (setting ``backfill``, R23; ``desk.backfill``): the default runs of the past business dates with
   positions from ``backfill_from`` on: once per start date and adapter the inbox reads the older reports; then the
   dates whose run is missing or stale run, oldest first, for at most ``backfill_budget_s`` seconds a cycle, up to
   today: every date its plan calls stale, also inside the eod step's window (#23: an upgrade recomputes there only
   the latest positions date, and these dates stayed stale while the backfill stopped at the window), but the dates
   the eod step ran or failed (a date is not tried twice a cycle, nor again on inputs the eod step failed on). Last in the cycle, after the EOD, intraday and history steps, so the latest date never waits for it; not while the inbox
   backlog drains (R22). The hub's date list is read once per ``RECHECK_S``, less the dates the sync step cannot load
   (its ``problem_dates``): no run and no previous close on them until they load. While the backfill stalls (it
   stopped on an error of the store or the network, ``backfill`` ``stall``, #447) the step runs it again only
   after the stall's ``next_attempt`` (``backfill.STALL_BACKOFF_S``) and says so, status ``error``; not in the
   first cycle of the process or a cycle asked for (``trigger``, ``desk cycle``), nor when the store is the cause
   (the plan has just read it) or the downloads are and the hub is off this cycle. Nothing left to run ends it.
8. **status** (``status()``, the shape of ``GET /api/v1/autopilot/status``). While a cycle runs, ``message`` and
   ``progress`` ({"step": "sync"|"scan"|"eod"|"intraday"|"history"|"backfill", "done": int, "total": int|null, "detail": str})
   follow it live: hub dates fetched of those to fetch, report files done of those offered, EOD dates run of those
   to run, history dates loaded of those to load. ``latest_run`` is the newest run shown (``store.CURRENT_RUNS``:
   today's intraday run until there is an EOD run of a later date), with its slice (``run_type``, ``cut_type``,
   ``cut_ts``, ``label``). After a cycle, ``message`` is never «Данные актуальны» while a run is skipped as failed
   before on the same inputs («Ошибка: прогон <срез> падает (<ошибка>)», state ``error``; the error is known when the
   run failed in this process) or while a business day past ``WARN_AFTER`` (10:15 MSK, the header's warning, #22)
   shows no run of today («Данные устарели: нет прогона за DD.MM (<why>)», #156).

Settings are read again at the start of every cycle (``desk.settings.load``), so edits apply without a restart.
Errors are caught per step, logged (``desk.autopilot`` and ``<home>/logs/autopilot.log``) and shown in the status;
they never stop the thread.

One cycle at a time per home folder, across processes: a cycle holds ``<home>/cycle.lock`` (``cycle_lock``), so a
``desk cycle`` (``update_daily.bat``) started while the ``desk serve`` thread is in a cycle, or the other way round,
does nothing (step ``cycle``: ``skipped``, "another cycle running") instead of publishing the same date twice.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from itertools import islice
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psycopg

from . import logic_version, store
from .backfill import late_why, same_logic
from .inbox.timesem import MSK
from .marketdata import cache, history, hubhistory, static_facts
from .marketdata.adapter import StaticView
from .marketdata.bundle import SnapshotDir, SnapshotError, previous_close, previous_market
from .marketdata.hub import HubClient, HubError, HubUnavailable
from .scenario import jobs as scenario_jobs
from .settings import Settings, load

log = logging.getLogger("desk.autopilot")

WINDOW_DAYS = 14        # calendar days back from today that the EOD runs are kept current for (~10 business days)
MAX_EOD_DATES = 10
RECHECK_S = 3600.0      # cached dates are asked of the hub again this often (corrections)
MAX_HUB_DATES = 400     # how far back the hub's date list is read at most
SYNC_EXTRA = 3          # hub dates per cycle beyond autopilot_sync_last; the rest wait for the next cycle
MISSING_TTL_S = 6 * 3600.0  # a date the hub has no snapshot for is not asked again for this long (per process)
PREV_MARGIN_DAYS = 7    # below the lookback: the hub date before its first day (flash PnL, a start-of-day calendar)
MIN_SLEEP_S = 1.0
LOCK_FILE = "cycle.lock"
KEEP_CYCLES = 20        # the last cycles kept in memory with their step durations (desk.diagnostics, R13)
HISTORY_PER_CYCLE = 5   # hub dates of market history fetched per cycle (~0.5 MB each), newest first
MAX_HISTORY_DAYS = 2600  # history_days is capped here (~10 years of business dates)

INTRADAY_FROM = dtime(10, 0)  # MSK: the hub's first intraday cut of the day
PRELIM_FROM = dtime(19, 45)   # MSK: the hub's eod_preliminary cut of the day
POLL_SLOT_S = 600             # the hub is asked for a newer cut of today at most once per 10-minute slot (#20)
POLL_LAG_S = 300              # slots start 5 min after the cut times (:05, :15, ...): the hub publishes after its cut
MAX_DATE_FAILURES = 2         # hub dates whose sync failed in a cycle before the rest wait for the next cycle (#20)
# a hub date whose sync failed is asked again after these waits, by its failed attempts, the last one onwards (#146)
RETRY_BACKOFF_S = (600, 1800, 7200, 21600)
# a hub date whose bundle gets no answer at all this many cycles in a row, the date list answering: the next date
# tells the date from the hub; when it answers (or there is none), the date fails and backs off as above (#217)
NO_ANSWER_CYCLES = 3
# the intraday step's ``reason`` (status ``steps[].reason``; the header shows why there is no run of today, #22)
INTRADAY_REASONS = ("off", "before_open", "not_business_day", "no_positions", "no_cut_yet", "up_to_date", "ran",
                    "failed_before", "run_failed", "hub_error", "error")
WARN_AFTER = dtime(10, 15)    # MSK: a business day with no run of today by then is not «актуальны» (#156; the header, #22)
MORNING_FROM = dtime(7, 0)    # MSK: the morning run of a business day is made from then (#21; earlier on its own book)
# the morning step's ``reason`` (status ``steps[].reason``, #21/#22)
MORNING_REASONS = ("not_business_day", "before_morning", "no_positions", "no_close", "superseded", "up_to_date",
                   "inbox_backlog", "ran", "failed_before", "run_failed", "error")
KEEP_CUT_DAYS = 3             # intraday and preliminary bundles of older dates leave the cache
STATIC_POLL_S = 300.0         # the hub's newest static is asked for at most this often (R25: reference facts, ratings)

LATEST_RUN = ("SELECT run_id, business_date, completed_at, run_type, cut_type, cut_ts,"
              " lineage->'pnl_basis'->>'label', lineage->'market_close' FROM run"
              f" WHERE run_id IN ({store.CURRENT_RUNS}) ORDER BY business_date DESC LIMIT 1")


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def _stall_text(s: dict[str, Any]) -> str:
    """The backfill step's detail while the backfill stalls (``backfill`` ``stall``, #447)."""
    return (f"stands since {s.get('since')}, stopped on {s.get('date')}: {_short(s.get('error') or '')} (the "
            f"{s.get('source')}: {s.get('cause')}, not the dates' data); next attempt {s.get('next_attempt')}")


def _short(text: str, limit: int = 200) -> str:
    """An error for the status line: one line, at most ``limit`` characters."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _why_no_run(steps: list[dict[str, Any]]) -> str:
    """Why there is no run of today, for the status line (#156): the intraday step's detail, else the morning
    step's, when the step did not report a run; empty otherwise."""
    for name in ("intraday", "morning"):
        s = next((x for x in steps if x["name"] == name), None)
        if s is not None and s.get("reason") not in ("ran", "up_to_date", "superseded"):
            return f" ({'внутри дня' if name == 'intraday' else 'утренний прогон'}: {_short(s['detail'])})"
    return ""


def _cached(cache_dir: Path, row: dict[str, Any]) -> bool:
    """The market snapshot of a hub list row and the static snapshot it names are in the cache: ``cache.fetch_row``
    does not ask the hub for them."""
    static = row.get("static_snapshot_id")
    return cache.bundle_path(cache_dir, row["snapshot_id"]).exists() and (
        not static or (cache.bundle_path(cache_dir, static).exists() and (cache_dir / f"{static}.hub.json").exists()))


def latest_run(con: psycopg.Connection) -> dict[str, Any] | None:
    """The run shown for the latest business date (``store.CURRENT_RUNS``: its default run, else its intraday run):
    {run_id, as_of, created_at, run_type, cut_type, cut_ts, label, pnl_basis, market_close} (I4, R11), None without
    one. ``market_close``: the morning run's close ({date, cut_type}, cut ``prev_close``, #21), else None."""
    row = con.execute(LATEST_RUN).fetchone()
    if row is None:
        return None
    rid, d, done, rtype, cut, cut_ts, basis, close = row
    return {"run_id": str(rid), "as_of": d.isoformat(), "created_at": _iso(done), "run_type": rtype,
            "cut_type": cut, "cut_ts": _iso(cut_ts), "label": store.slice_label(d, cut, cut_ts, rtype, close),
            "pnl_basis": basis, "market_close": close}


class CycleLock:
    """The inter-process cycle lock ``<home>/cycle.lock``, non-blocking. ``msvcrt.locking`` on Windows,
    ``fcntl.flock`` elsewhere; the operating system releases it when the process dies, so a crashed cycle leaves
    no stale lock. Two opens of the file exclude each other within one process too. ``name``: another lock file of
    the home folder (``desk.backup``'s ``backup.lock``)."""

    def __init__(self, home: Path, name: str = LOCK_FILE) -> None:
        self.path = Path(home) / name
        self._f: Any = None

    def acquire(self) -> bool:
        """True when taken, False when held elsewhere; OSError when the file cannot be created."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self.path, "a+b")  # noqa: SIM115 - kept open while held
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            return False
        self._f = f
        return True

    def release(self) -> None:
        f, self._f = self._f, None
        if f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc: object) -> None:
        self.release()


def idle_status(enabled: bool = False, message: str = "Автопилот не запущен") -> dict[str, Any]:
    return {"enabled": enabled, "state": "idle", "last_cycle_at": None, "last_ok_at": None, "message": message,
            "steps": [], "latest_run": None, "progress": None}


def progress_text(p: dict[str, Any]) -> str:
    """The status line of a running step (the Desk Monitor badge says the same)."""
    done, total, detail = p["done"], p["total"], p.get("detail") or ""
    if p["step"] == "scan":
        return f"Скан отчётов: {done} из {total}" if total is not None else f"Скан отчётов: просмотрено файлов {done}"
    if p["step"] == "sync":
        return f"Загрузка снимков: {done} из {total}" if total is not None else "Загрузка снимков: список дат"
    if p["step"] == "eod":
        day = f" {detail[8:10]}.{detail[5:7]}" if len(detail) == 10 else ""
        return f"EOD{day}: {done} из {total}"
    if p["step"] == "intraday":
        return f"Внутри дня: {detail}" if detail else "Внутри дня"
    if p["step"] == "history":
        return f"История котировок: {done} из {total}" if total is not None else "История котировок: список дат"
    if p["step"] == "backfill":
        day = f" {detail[8:10]}.{detail[5:7]}.{detail[:4]}" if len(detail) == 10 else (f" {detail}" if detail else "")
        return f"История прогонов{day}: {done} из {total}" if total is not None else f"История прогонов{day}"
    return f"{p['step']}: {done}" + (f" из {total}" if total is not None else "")


class Autopilot:
    """``force``: run cycles even when the settings say ``autopilot: false`` (``desk cycle``,
    ``desk serve --autopilot``). ``out``: a callback for progress lines (``desk cycle`` prints them)."""

    def __init__(self, *, settings_loader: Callable[[], Settings] = load, store_url: str | None = None,
                 force: bool = False, today: Callable[[], date] | None = None,
                 now: Callable[[], datetime] | None = None, out: Callable[[str], None] | None = None) -> None:
        """``now`` (the Moscow clock of the intraday step) and ``today``: the clocks, for tests; with ``today``
        alone the clock stands at the start of that day (the intraday step waits for 10:00)."""
        self._load, self._store_url, self.force, self._out = settings_loader, store_url, force, out
        if now is None and today is not None:
            now = lambda: datetime.combine(today(), dtime(0), MSK)
        self._now = now or (lambda: datetime.now(MSK))
        self._today = today or (lambda: self._now().astimezone(MSK).date())
        self._cycle = threading.Lock()
        self._lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._failed: dict[str, tuple] = {}
        self._fail_errors: dict[str, str] = {}  # the keys of ``_failed`` -> the error of their failed run (#156)
        self._failing: list[tuple[str, str | None]] = []  # this cycle's runs skipped as failed before: (slice, error)
        self._no_run_today: str | None = None  # this cycle: a business day after WARN_AFTER without a run of it
        self._checked: dict[str, float] = {}   # hub date -> monotonic time it was last asked of the hub
        self._missing: dict[str, float] = {}   # hub date -> monotonic time the hub had no snapshot for it
        self._hub_failed = False               # this cycle's hub sync failed as a whole: no history step
        self._hub_down = ""                    # this cycle: the hub down after its retries, not asked again (#147)
        self._sync_failed: dict[str, str] = {}  # hub date -> its error, this cycle's sync (#20; the other dates ran)
        self._sync_later = 0                   # hub dates the sync left for the next cycle: no history step
        # hub date -> {"error", "attempts", "next_attempt" (MSK datetime)}: failed dates waiting for a retry (#146)
        self._sync_retry: dict[str, dict[str, Any]] = {}
        self._hub_first: str | None = None     # the hub's oldest date, when its whole list was read (#27)
        self._no_answer: dict[str, int] = {}   # hub date -> cycles in a row its bundle got no answer at all (#217)
        self._inbox_waiting = 0                # files the last scan left waiting for a calendar (R22)
        self._history_dates: tuple[float, int, list[tuple[str, str]]] | None = None
        self._static_polled = float("-inf")    # monotonic time the hub's newest static was last asked for (R25)
        self._backfill_rows: tuple[float, str, list[tuple[str, str]]] | None = None  # (listed at, from, hub rows)
        self._contexts: Any = None  # the backfill's prepared dates, kept between its runs (cli.ContextCache)
        self._stall_now = True  # the backfill runs at once, its stall's wait left out: a start or a manual cycle (#447)
        self._eod_tried: set[str] = set()  # the dates the eod step ran, failed or left as failed before, this cycle
        # (listed at, depth, [(hub date, its current snapshot id)] newest first)
        self._poll_slot: tuple | None = None   # (date, cut, slot) the hub was last asked for a cut of today in
        self._polled: dict[tuple, dict | None] = {}  # (date, cut) -> the hub's newest row of that cut, when asked
        self._calendar: tuple | None = None    # (static id, BusinessCalendar | None, valid from, valid to)
        self._pruned_on: date | None = None    # the day the cache's old intraday bundles were last pruned
        self._interval = 60.0
        self._on = True
        self._log_handler: RotatingFileHandler | None = None
        self._status = idle_status(True, "Ожидает первого цикла")
        self._cycles: deque[dict[str, Any]] = deque(maxlen=KEEP_CYCLES)  # the last cycles, newest last (R13)
        self._durations: dict[str, float] = {}  # step name -> seconds, of the running cycle

    # ---- thread

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="desk-autopilot", daemon=True)
        self._thread.start()

    def stop(self, timeout: float | None = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def trigger(self) -> None:
        """Run a cycle now (after the running one, if any); the backfill does not wait for its stall's next attempt
        (TDM#447: the proxy fixed, say)."""
        self._stall_now = True
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.run_cycle()
            except Exception:  # run_cycle catches per step; this is the last guard of the thread
                log.exception("autopilot: cycle failed")
            self._wake.wait(max(self._interval, MIN_SLEEP_S))

    def status(self) -> dict[str, Any]:
        with self._lock:
            st = {**self._status, "steps": [dict(s) for s in self._status["steps"]]}
            if st.get("progress") is not None:
                st["progress"] = dict(st["progress"])
        st["enabled"] = self.running and (self.force or self._on)
        return st

    # ---- one cycle

    def run_cycle(self, extra_dates: Iterable[str] = (), *, hub: bool = True) -> dict[str, Any]:
        """One cycle, synchronous; returns the status. ``extra_dates``: business dates to sync and keep current
        besides the automatic ones (``desk cycle --date``); ``hub=False`` skips the hub (the drop is imported)."""
        with self._cycle:
            started = datetime.now(UTC)
            self._durations = {}
            self._update(state="running", message="Цикл выполняется", steps=[], progress=None)
            steps: list[dict[str, Any]] = []
            try:
                settings = self._load()
            except Exception as e:  # noqa: BLE001 - a broken settings file is this cycle's error
                steps.append(self._step("settings", "error", f"{type(e).__name__}: {e}"))
                return self._finish(started, steps, [])
            self._interval = float(settings.inbox_poll_seconds or 0.0)
            self._on = bool(settings.autopilot)
            self._setup_log(settings)
            if not (self.force or self._on):
                with self._lock:
                    self._status.update(state="idle", message="Автопилот выключен в настройках", steps=[],
                                        progress=None)
                return self.status()
            log.info("autopilot: cycle start")
            lock = CycleLock(settings.home)
            try:
                held = lock.acquire()
            except OSError as e:  # no lock file (the home folder is not writable): no cycle
                steps.append(self._step("lock", "error", f"{type(e).__name__}: {e}"))
                return self._finish(started, steps, [])
            if not held:
                steps.append(self._step("cycle", "skipped", "another cycle running"))
                self._update(state="idle", last_cycle_at=_iso(started), latest_run=self._latest(settings),
                             message="Пропущено: выполняется другой цикл (desk cycle или desk serve)", progress=None)
                return self.status()
            try:
                return self._locked_cycle(settings, started, steps, extra_dates, hub)
            finally:
                lock.release()

    def _locked_cycle(self, settings: Settings, started: datetime, steps: list[dict[str, Any]],
                      extra_dates: Iterable[str], hub: bool) -> dict[str, Any]:
        extra = sorted({date.fromisoformat(d).isoformat() for d in extra_dates})
        con, url = None, None
        try:
            url = self._store_url or store.resolve_url(settings)
            con = store.connect(url)
        except Exception as e:  # noqa: BLE001 - no store: the hub sync still runs
            steps.append(self._step("store", "error", f"{type(e).__name__}: {e}"))
        ran: list[str] = []
        self._failing, self._no_run_today = [], None
        try:
            steps.append(self._guard("sync", lambda: self._sync(settings, con, extra, hub)))
            if con is not None:
                steps.append(self._guard("scan", lambda: self._scan(settings, con)))
                steps.append(self._guard("eod", lambda: self._eod(settings, con, url, extra, ran)))
                if hub and settings.hub_url:
                    steps.append(self._guard("intraday", lambda: self._intraday(settings, con, url, ran)))
                steps.append(self._guard("morning", lambda: self._morning(settings, con, url, ran)))
                steps.append(self._guard("scenarios", lambda: self._scenarios(con)))
                if hub and settings.hub_url and int(getattr(settings, "history_days", 0) or 0) > 0:
                    steps.append(self._guard("history", lambda: self._history(settings, con)))
                if getattr(settings, "backfill", True):
                    steps.append(self._guard("backfill", lambda: self._backfill(settings, con, url, hub, ran)))
                try:
                    latest = latest_run(con)
                except psycopg.Error:
                    latest = None
                with self._lock:
                    self._status["latest_run"] = latest
                self._no_run_today = self._stale(settings, latest)
        finally:
            if con is not None:
                con.close()
        return self._finish(started, steps, ran, settings)

    def _stale(self, settings: Settings, latest: dict[str, Any] | None) -> str | None:
        """«DD.MM» of today when it is a business day, the Moscow clock is past ``WARN_AFTER`` and no run of today is
        shown (``latest_run`` of an earlier date or none: the header's warning, #22), else None (#156)."""
        now = self._now().astimezone(MSK)
        today = now.date()
        if now.time() < WARN_AFTER or (latest is not None and latest["as_of"] >= today.isoformat()):
            return None
        try:
            if not self._business_day(settings, today):
                return None
        except Exception as e:  # noqa: BLE001 - only for the status line: weekdays
            log.warning("autopilot: business day of %s not known: %s", today, e)
            if today.weekday() >= 5:
                return None
        return f"{today:%d.%m}"

    def _fail(self, key: str, sig: tuple, e: Exception) -> None:
        """A run failed on ``sig``: not retried until its inputs change; its error kept for the status (#156)."""
        self._failed[key] = sig
        self._fail_errors[key] = f"{type(e).__name__}: {e}"

    def _failed_before(self, key: str, sig: tuple, label: str) -> bool:
        """The run failed before on the same inputs: not retried, and the cycle's status says so (#156)."""
        if self._failed.get(key) != sig:
            return False
        self._failing.append((label, self._fail_errors.get(key)))
        return True

    def _latest(self, settings: Settings) -> dict[str, str] | None:
        """The latest run shown read on a connection of its own (a skipped cycle has none); the last known one
        when the store cannot be read."""
        try:
            with store.connect(self._store_url or store.resolve_url(settings)) as con:
                return latest_run(con)
        except Exception:  # noqa: BLE001 - only for the status line
            return self.status().get("latest_run")

    def _finish(self, started: datetime, steps: list[dict], ran: list[str],
                settings: Settings | None = None) -> dict[str, Any]:
        errors = [s["name"] for s in steps if s["status"] == "error"]
        missing = [] if settings is None else [
            what for what, ok in (("маркет-дата сервер", settings.hub_url or settings.snapshot_drop),
                                  ("папки с отчётами", settings.inbox_dirs)) if not ok]
        # a run skipped as failed before is a problem, not «актуальны» (#156): it fails on these inputs every cycle
        failing = [] if settings is None else self._failing
        stale = None if settings is None else self._no_run_today
        if errors:
            msg = "Ошибки: " + ", ".join(errors)
        elif failing:  # the error of a run that failed in an earlier cycle of this process, when known
            label, err = failing[0]
            msg = f"Ошибка: прогон {label} падает" + (f" ({_short(err)})" if err else "") \
                + (f"; ещё прогонов: {len(failing) - 1}" if len(failing) > 1 else "")
        elif ran:
            msg = "Пересчитано: " + ", ".join(ran)
        elif len(missing) == 2:  # first launch: say what to do, not that all is well
            msg = "Не настроено: укажите маркет-дата сервер и папки с отчётами в Настройках"
        elif stale:  # a business day after 10:15 without a run of today: not current, whatever the steps say (#156)
            msg = f"Данные устарели: нет прогона за {stale}" + _why_no_run(steps)
        elif missing:
            msg = f"Данные актуальны (не настроено: {missing[0]})"
        else:
            msg = "Данные актуальны"
        upd: dict[str, Any] = {"state": "error" if errors or failing else "idle", "last_cycle_at": _iso(started),
                               "message": msg, "steps": steps, "progress": None}
        if not (errors or failing):
            upd["last_ok_at"] = _iso(started)
        self._update(**upd)
        self._cycles.append({"started_at": _iso(started), "duration_s": round((datetime.now(UTC) - started)
                                                                               .total_seconds(), 1),
                             "state": upd["state"], "message": msg,
                             "steps": [{**s, "duration_s": self._durations.get(s["name"])} for s in steps]})
        log.info("autopilot: cycle done: %s", msg)
        return self.status()

    def _update(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)

    def _progress(self, step: str, done: int, total: int | None, detail: str = "") -> None:
        """The live ``progress`` of the running step and its ``message``."""
        p = {"step": step, "done": int(done), "total": None if total is None else int(total), "detail": detail}
        self._update(progress=p, message=progress_text(p))

    def _step(self, name: str, status: str, detail: str, **extra: Any) -> dict[str, Any]:
        s = {"name": name, "status": status, "detail": detail, **extra, "at": _iso(datetime.now(UTC))}
        (log.warning if status == "error" else log.info)("autopilot: %s %s: %s", name, status, detail)
        self._emit(f"{name}: {status}" + (f" - {detail}" if detail else ""))
        with self._lock:
            self._status["steps"] = [*self._status["steps"], s]
        return s

    def _guard(self, name: str, fn: Callable[[], tuple]) -> dict[str, Any]:
        """Runs a step: ``fn`` returns (status, detail) or (status, detail, {extra status fields})."""
        t0 = time.monotonic()
        extra: dict[str, Any] = {}
        try:
            status, detail, *more = fn()
            extra = more[0] if more else {}
        except Exception as e:
            log.exception("autopilot: step %s failed", name)
            status, detail = "error", f"{type(e).__name__}: {e}"
            extra = {"reason": "error"} if name in ("intraday", "morning") else {}
        self._durations[name] = round(time.monotonic() - t0, 2)
        return self._step(name, status, detail, **extra)

    def _emit(self, line: str) -> None:
        if self._out is not None:
            self._out(line)

    def _setup_log(self, settings: Settings) -> None:
        path = Path(settings.logs_dir) / "autopilot.log"
        if self._log_handler is not None and Path(self._log_handler.baseFilename) == path.resolve():
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        except OSError as e:
            log.warning("autopilot: no log file %s: %s", path, e)
            return
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        if self._log_handler is not None:
            log.removeHandler(self._log_handler)
            self._log_handler.close()
        log.addHandler(handler)
        if log.level == logging.NOTSET or log.level > logging.INFO:
            log.setLevel(logging.INFO)
        self._log_handler = handler

    def diagnostics(self) -> dict[str, Any]:
        """The thread's own state for ``desk.diagnostics`` (R13): the last cycles with their step durations, what the
        hub was last asked and answered for the intraday step, the dates it remembers as missing or failed. Dates,
        codes, counts and snapshot id prefixes; the step details as they are (the caller scrubs them)."""
        now = time.monotonic()
        hist = self._history_dates
        slot = self._poll_slot
        return {
            "running": self.running, "force": self.force, "interval_s": self._interval, "on_in_settings": self._on,
            "cycles": [dict(c, steps=[dict(s) for s in c["steps"]]) for c in self._cycles],
            "hub_failed_last_cycle": self._hub_failed, "sync_dates_left_for_next_cycle": self._sync_later,
            "hub_failed_dates": dict(sorted(self._sync_failed.items())),  # date -> error, the last cycle's sync (#20)
            "hub_problem_dates": {p["date"]: {k: v for k, v in p.items() if k != "date"}
                                  for p in self._problem_dates()},  # failed dates waiting for a retry (#146)
            "hub_dates_checked": len(self._checked),
            "hub_missing_dates": {d: round(now - t) for d, t in sorted(self._missing.items())},  # date -> age, s
            "failed_inputs": sorted(self._failed),  # dates (eod) and "<cut> <date>" (intraday) not retried as they are
            "history_dates": None if hist is None else {
                "listed_s_ago": round(now - hist[0]), "depth": hist[1], "count": len(hist[2]),
                "newest": hist[2][0] if hist[2] else None, "oldest": hist[2][-1] if hist[2] else None},
            "intraday_poll_slot": None if slot is None else {
                "date": slot[0].isoformat(), "cut": slot[1],
                "slot_start": _iso(datetime.fromtimestamp(slot[2] * POLL_SLOT_S + POLL_LAG_S, MSK))},
            "intraday_hub_rows": [
                {"date": d.isoformat(), "cut": cut, "found": row is not None,
                 **({"snapshot_id": str(row.get("snapshot_id") or "")[:12], "cut_ts": row.get("cut_ts"),
                     "published_at": row.get("published_at")} if row else {})}
                for (d, cut), row in sorted(self._polled.items(), key=lambda kv: (kv[0][0], kv[0][1]))],
            "cache_pruned_on": self._pruned_on.isoformat() if self._pruned_on else None,
        }

    # ---- steps

    def _sync(self, settings: Settings, con: psycopg.Connection | None, extra: list[str],
              hub: bool) -> tuple[str, str, dict[str, Any]]:
        parts, status = [], "ok"
        self._hub_failed, self._sync_later, self._sync_failed, self._hub_down = False, 0, {}, ""
        if hub and settings.hub_url:
            try:
                parts.append(self._hub_sync(settings, con, extra))
            except (HubError, SnapshotError, OSError, ValueError) as e:
                status, self._hub_failed = "error", True
                if isinstance(e, HubUnavailable):  # the later steps of the cycle do not ask it again (#147)
                    self._hub_down = str(e)
                parts.append(f"hub: {e}; the cached snapshots are used")
            if self._sync_failed:  # dates that failed alone (#20): the step's error, not the cycle's
                status = "error"
        failed = {"failed_dates": sorted(self._sync_failed), "problem_dates": self._problem_dates()}
        if settings.snapshot_drop:
            if Path(settings.snapshot_drop).is_dir():
                imported, rejected = cache.import_drop(Path(settings.snapshot_drop), Path(settings.snapshot_cache))
                parts.append(f"drop: {len(imported)} bundles imported"
                             + (f", rejected {', '.join(n for n, _ in rejected)}" if rejected else ""))
                if rejected:
                    status = "error"
            else:
                status = "error"
                parts.append(f"drop: {settings.snapshot_drop} is not a folder")
        loaded = ""
        if con is not None:  # the new bundles of the cache into the market history (the bond charts)
            try:
                loaded = history.load_cache(con, Path(settings.snapshot_cache),
                                            lambda i, n, sid: self._progress("sync", i, n, "history " + sid)).text()
            except psycopg.Error as e:
                status, loaded = "error", f"history tables: {type(e).__name__}: {e}"
        if not parts:
            return ("error" if status == "error" else "skipped"), "; ".join(
                ["hub not configured (hub_url)" if hub else "hub off (--no-hub), no drop folder"]
                + ([loaded] if loaded else [])), failed
        return status, "; ".join(parts + ([loaded] if loaded else [])), failed

    def _problem_dates(self) -> list[dict[str, Any]]:
        """The hub dates whose sync failed and that wait for a retry (#146), oldest first."""
        return [{"date": d, "error": r["error"], "attempts": r["attempts"], "next_attempt": _iso(r["next_attempt"])}
                for d, r in sorted(self._sync_retry.items())]

    def _date_failed(self, d: str, e: Exception) -> None:
        """A hub date whose sync failed: this cycle's ``failed_dates``, and not asked again before its wait (#146)."""
        self._sync_failed[d] = f"{type(e).__name__}: {e}"
        attempts = self._sync_retry.get(d, {}).get("attempts", 0) + 1
        wait = RETRY_BACKOFF_S[min(attempts, len(RETRY_BACKOFF_S)) - 1]
        self._sync_retry[d] = {"error": self._sync_failed[d], "attempts": attempts,
                               "next_attempt": self._now() + timedelta(seconds=wait)}

    def _hub_sync(self, settings: Settings, con: psycopg.Connection | None, extra: list[str]) -> str:
        client = HubClient(settings)  # raises HubError without a token on the first request
        n = int(getattr(settings, "autopilot_sync_last", 5) or 0)
        today = self._today()
        lookback = int(getattr(settings, "inbox_lookback_days", 0) or 0)
        cutoff = (today - timedelta(days=min(lookback, 36500))).isoformat() if lookback > 0 else None
        oldest = (date.fromisoformat(cutoff) - timedelta(days=PREV_MARGIN_DAYS)).isoformat() if cutoff else ""
        pos, cover = [], []
        if con is not None:
            pos += [d.isoformat() for d in store.position_dates(con, today - timedelta(days=WINDOW_DAYS))]
            cover = [d.isoformat() for (d,) in con.execute(
                "SELECT DISTINCT report_date FROM (SELECT DISTINCT ON (sha256) status, report_date"
                " FROM inbox_file_status ORDER BY sha256, status_id DESC) s"
                " WHERE status = 'waiting_calendar' AND report_date IS NOT NULL")]
        pos = [d for d in pos if cutoff is None or d >= cutoff]  # never older than the lookback
        cover = [d for d in cover if cutoff is None or d >= cutoff]
        floor = min(pos + cover + extra, default=None)
        self._progress("sync", 0, None, "список дат хаба")
        hub_dates: list[str] = []  # newest first
        for d in cache.hub_dates(client):
            if len(hub_dates) >= n and d < oldest:
                break
            hub_dates.append(d)
            if (len(hub_dates) >= n and (floor is None or d < floor)) or len(hub_dates) >= MAX_HUB_DATES:
                break
        else:  # the whole list read: its last date is the hub's oldest; the scan retires reports before it (#27)
            self._hub_first = hub_dates[-1] if hub_dates else None
        wanted = set(hub_dates[:n])
        for d in pos + extra:  # the date and the hub date before it (flash PnL)
            wanted.add(d)
            wanted.add(next((x for x in hub_dates if x < d), d))
        for r in cover:  # a cached calendar covering the report date (an older snapshot when none is earlier)
            wanted.add(next((x for x in hub_dates if x <= r), hub_dates[-1] if hub_dates else r))
        wanted &= set(hub_dates)
        wanted = {d for d in wanted if d >= oldest or d in extra}
        now = time.monotonic()
        for d in [d for d, t in self._missing.items() if now - t >= MISSING_TTL_S]:
            del self._missing[d]
        for d in [d for d in self._sync_retry if d not in wanted]:  # no longer wanted: no longer a problem
            del self._sync_retry[d]
        for d in [d for d in self._no_answer if d not in wanted]:
            del self._no_answer[d]
        wall = self._now()
        waiting = sorted(d for d, r in self._sync_retry.items() if d not in extra and wall < r["next_attempt"])
        cached = set(SnapshotDir(Path(settings.snapshot_cache)).market_dates())
        due = [d for d in wanted if d not in self._missing and d not in waiting
               and (d not in cached or now - self._checked.get(d, float("-inf")) >= RECHECK_S)]
        due.sort(reverse=True)  # newest first, then those not cached yet and those asked for (stable sort),
        due.sort(key=lambda d: (d in self._sync_retry and d not in extra, d in cached, d not in extra))  # failed last
        limit = max(n, 1) + SYNC_EXTRA
        todo, later = due[:limit], len(due) - min(len(due), limit)
        self._sync_later = later
        pulled, missing = [], []
        passed: tuple[str, Exception] | None = None  # a date passed over for its no answer, until the next tells (#217)

        def answered() -> None:  # the hub answered for a date after the one passed over: that one fails alone (#217)
            nonlocal passed
            if passed is not None:
                self._no_answer.pop(passed[0], None)
                self._date_failed(*passed)
                passed = None

        for i, d in enumerate(todo):
            if len(self._sync_failed) >= MAX_DATE_FAILURES:  # the hub fails date after date: the rest next cycle
                self._sync_later = later = later + len(todo) - i
                break
            self._progress("sync", i, len(todo), d)
            try:
                rows = cache.sync(client, Path(settings.snapshot_cache), [d])
            except HubError as e:
                if e.status in (401, 403):  # the token: every date would fail
                    raise
                if isinstance(e, HubUnavailable) and e.last_status is None:
                    # no answer after the retries: the hub is down, not the date (#147), unless the same date gets
                    # none NO_ANSWER_CYCLES cycles in a row while the list answers: the next date tells (#217)
                    n = self._no_answer[d] = self._no_answer.get(d, 0) + 1
                    if passed is not None or n < NO_ANSWER_CYCLES:
                        raise
                    passed = (d, e)
                    continue
                self._no_answer.pop(d, None)
                answered()
                if isinstance(e, HubUnavailable):  # 5xx after the retries: the date fails (#146) and the hub is not
                    self._date_failed(d, e)  # asked again this cycle (#147): one retry series, the rest next cycle
                    self._hub_down = str(e)
                    self._sync_later = later = later + len(todo) - i - 1
                    break
                if e.status != 404:
                    self._date_failed(d, e)
                    continue
                rows = []
            except (SnapshotError, OSError, ValueError) as e:  # this date's body: the other dates go on (#20)
                self._no_answer.pop(d, None)
                answered()
                self._date_failed(d, e)
                continue
            self._no_answer.pop(d, None)
            answered()
            self._sync_retry.pop(d, None)
            self._checked[d] = time.monotonic()
            if not rows or not rows[0].market_id:  # no snapshot for this date on the hub: not asked again soon
                self._missing[d] = time.monotonic()
                missing.append(d)
                continue
            pulled += [rows[0].business_date] if rows[0].downloaded else []
        answered()  # no later date to tell: the list answered this cycle, the date none NO_ANSWER_CYCLES in a row
        if todo:
            self._progress("sync", len(todo), len(todo), "")
        latest = hub_dates[0] if hub_dates else "none"
        static = "" if self._hub_down else self._newest_static(settings, con, client)
        return (f"hub {client.host}: latest {latest}; " + (f"pulled {', '.join(pulled)}" if pulled else "up to date")
                + (f"; {static}" if static else "")
                + (f"; no snapshot for {', '.join(missing)}" if missing else "")
                + "".join(f"; failed {d} ({e})" for d, e in sorted(self._sync_failed.items()))
                + (f"; retry later {', '.join(waiting)}" if waiting else "")
                + f" ({len(wanted)} dates wanted, {len(todo)} asked"
                + (f", {later} next cycle" if later else "") + ")")

    def _newest_static(self, settings: Settings, con: psycopg.Connection | None, client: HubClient) -> str:
        """The hub's newest static snapshot into the cache (``cache.newest_static``, at most every
        ``STATIC_POLL_S``) and its facts into the read model (``static_facts.load_static``: the pages' issuer groups,
        sectors and ratings, R25); the step detail, empty when nothing changed."""
        now = time.monotonic()
        if now - self._static_polled < STATIC_POLL_S:
            return ""
        self._static_polled = now
        try:
            got = cache.newest_static(client, Path(settings.snapshot_cache))
        except Exception as e:  # noqa: BLE001 - the dates are synced; the reference facts wait for the next poll
            if isinstance(e, HubUnavailable):
                self._hub_down = str(e)
            return f"newest static not fetched: {type(e).__name__}: {e}"
        if got is None or not got.static_id:
            return ""
        out = f"static {got.static_id[:12]} of {got.business_date} pulled" if got.downloaded else ""
        if con is not None:
            try:
                res = static_facts.load_static(con, SnapshotDir(Path(settings.snapshot_cache)).get(got.static_id))
                if res == "loaded":
                    out = (out + "; " if out else "") + f"static facts {got.static_id[:12]} loaded"
            except (psycopg.Error, SnapshotError, OSError) as e:
                out = (out + "; " if out else "") + f"static facts not loaded: {type(e).__name__}: {e}"
        return out

    def _scenarios(self, con: psycopg.Connection) -> tuple[str, str]:
        """Queues the exact repricing of the active scenarios on the newest default run (step 5, TDM#71)."""
        latest = scenario_jobs.latest_default_run(con)
        if latest is None:
            return "skipped", "no default run"
        rid, day = latest
        rep = scenario_jobs.submit_all(con, rid)
        detail = f"run {rid} ({day}): {len(rep['queued'])} queued, {rep['reused']} already queued or done"
        if rep["failed"]:  # failed on this run before: not queued again every cycle (the page's POST retries it)
            detail += f"; failed before, not retried: {', '.join(rep['failed'])}"
        if rep["invalid"]:
            detail += "; not repriced: " + "; ".join(f"{k}: {v}" for k, v in rep["invalid"].items())
        return "ok", detail

    def _history(self, settings: Settings, con: psycopg.Connection) -> tuple[str, str]:
        """Market history: from the hub's history API when it serves this token (``desk.marketdata.hubhistory``, R24:
        ``hubhistory.BUDGET_S`` a cycle, resumable), else from the snapshots (``_history_bundles``), with the reason.
        Only after a sync that reached the hub and left nothing for the next cycle: the regular dates come first."""
        if self._hub_failed:
            return "skipped", "the hub sync failed this cycle"
        if self._hub_down:
            return "skipped", f"the hub is down this cycle (hub_error: {self._hub_down})"
        if self._sync_later:
            return "skipped", f"after the sync ({self._sync_later} dates next cycle)"
        client = HubClient(settings)
        try:
            rep = hubhistory.sync(con, client, today=self._today(), bonds_from=settings.history_bonds_from,
                                  rates_from=settings.history_rates_from, held=hubhistory.held_secids(con),
                                  progress=lambda i, n, d: self._progress("history", i, n, d))
            if rep.available:
                return ("error" if rep.errors else "ok"), rep.text()
            status, detail = self._history_bundles(settings, con, client)
        except HubUnavailable as e:  # the backfill does not ask it again this cycle (#147)
            self._hub_down = str(e)
            raise
        return status, f"{detail}; {rep.text()}"

    def _history_bundles(self, settings: Settings, con: psycopg.Connection, client: HubClient) -> tuple[str, str]:
        """Market history for the bond charts beyond the cache when the hub has no history API: the ``history_days``
        latest hub dates, newest first, ``HISTORY_PER_CYCLE`` a cycle, straight into the store's tables
        (``history.fetch_hub``; not cached, see ``desk.marketdata.history``), and again a date whose loaded snapshot
        the hub has since superseded by a correction (``history.superseded_dates``). The hub's date list is read again
        once per ``RECHECK_S``."""
        depth = min(int(settings.history_days), MAX_HISTORY_DAYS)
        now = time.monotonic()
        if (self._history_dates is None or self._history_dates[1] != depth
                or now - self._history_dates[0] >= RECHECK_S):
            self._progress("history", 0, None, "список дат хаба")
            self._history_dates = (now, depth, list(islice(cache.hub_latest(client), depth)))
        latest = self._history_dates[2]
        dates = [d for d, _ in latest]
        if not dates:
            return "skipped", "the hub lists no dates"
        for d in [d for d, t in self._missing.items() if now - t >= MISSING_TTL_S]:
            del self._missing[d]
        have = history.loaded_dates(con) | set(SnapshotDir(Path(settings.snapshot_cache)).market_dates())
        corrected = set(history.superseded_dates(con, latest))  # loaded, then corrected on the hub (WEB-11)
        due = [d for d in dates if (d not in have or d in corrected) and d not in self._missing  # cached: the sync
               and d not in self._sync_retry]  # failed in the sync: its retry is the sync's (#146)
        todo = due[:HISTORY_PER_CYCLE]
        loaded, missing = [], []
        for i, d in enumerate(todo):
            self._progress("history", i, len(todo), d)
            try:
                st = history.fetch_hub(con, client, d)
            except HubError as e:
                if e.status != 404:
                    raise
                st = None
            except SnapshotError as e:  # a body that does not verify
                log.warning("autopilot: history %s: %s", d, e)
                st = "unreadable"
            if st == "loaded":
                loaded.append(d)
            else:  # no snapshot on the hub, or one that cannot be loaded: not asked again soon
                self._missing[d] = time.monotonic()
                missing.append(f"{d} ({st or 'no snapshot'})")
        span = f"history_days {depth}: {len(dates)} on the hub back to {dates[-1]}"
        if not todo:
            return "ok", f"up to date ({span})"
        parts = ["loaded " + ", ".join(f"{d} (correction)" if d in corrected else d for d in loaded)
                 if loaded else "nothing loaded"]
        if missing:
            parts.append(f"not loaded {', '.join(missing)}")
        if len(due) > len(todo):
            parts.append(f"{len(due) - len(todo)} dates in the next cycles")
        return "ok", "; ".join(parts) + f" ({span})"

    def _backfill(self, settings: Settings, con: psycopg.Connection, url: str | None, hub: bool,
                  ran: list[str]) -> tuple[str, str]:
        """The backfill step (module doc, ``desk.backfill``)."""
        from . import backfill
        from .cli import ContextCache

        if self._inbox_waiting and self._sync_later:
            return "skipped", "after the inbox backlog"
        if not settings.inbox_dirs and con.execute("SELECT NOT EXISTS (SELECT 1 FROM position_set)").fetchone()[0]:
            return "skipped", "no positions in the store"
        d_from, today = backfill.start_date(settings), self._today()
        # the dates the eod step ran, failed or skipped as failed before on the same inputs, this cycle: not tried
        # again here (a date out of its window, or whose inputs changed, is the backfill's again)
        exclude = set(self._eod_tried)
        parts = []
        line = backfill.inbox(con, settings, d_from,
                              progress=lambda done, total, detail: self._progress("backfill", done, total, "отчёты"))
        if line:
            parts.append(line)
        client, rows = None, None
        if hub and settings.hub_url and not self._hub_failed and not self._hub_down:
            client = HubClient(settings)
            now = time.monotonic()
            if (self._backfill_rows is None or self._backfill_rows[1] != d_from.isoformat()
                    or now - self._backfill_rows[0] >= RECHECK_S):
                self._progress("backfill", 0, None, "список дат хаба")
                self._backfill_rows = (now, d_from.isoformat(), backfill.hub_rows(client, d_from))
            # the hub dates the sync cannot load (#146) are left out: neither run nor anyone's previous close until
            # they load, as in the eod step's runs on the cache (#163, #447: the header shows them as not loading)
            rows = [r for r in self._backfill_rows[2] if r[0] not in self._sync_retry]
        p = backfill.plan(con, settings, d_from, today, rows)
        s = p.summary()
        span = (f"{s['from']}..{s['to']}: {s['done']} of {s['dates'] - s['no_positions'] - s['no_market']} dates with "
                f"positions have their run" + (f", {s['failed']} failed before" if s["failed"] else ""))
        if not p.todo:
            backfill.drop_stall(settings, hub=p.hub)  # nothing left to run: a later outage is a new one (TDM#447)
        if not [d for d in p.todo if d.date not in exclude]:
            return "ok", "; ".join(parts + [f"up to date ({span})"])
        # stalled on the store or the network (#447): its dates not tried again before its wait, unless the process
        # started or the cycle was asked for, the store is the cause (the plan above just read it), or the downloads
        # are and the hub is off this cycle (only the cached dates would run)
        stall, now_ok, self._stall_now = backfill.state(settings).get("stall"), self._stall_now, False
        if (stall and set(stall.get("dates") or []) & {d.date for d in p.todo} and stall.get("next_attempt")
                and not now_ok and stall.get("cause") != "store"
                and not (stall.get("phase") == "bundle" and client is None)
                and self._now() < datetime.fromisoformat(stall["next_attempt"])):
            return "error", "; ".join(parts + [_stall_text(stall), span])
        if self._contexts is None:
            self._contexts = ContextCache()
        res = backfill.run(settings, p, store_url=url, client=client, budget_s=float(settings.backfill_budget_s),
                           out=self._eod_line, contexts=self._contexts, exclude=exclude, now=self._now(),
                           progress=lambda i, n, d: self._progress("backfill", i, n, d))
        if res["done"]:
            ran.append(f"история {len(res['done'])} дат")
            parts.append(f"ran {len(res['done'])} dates ({res['done'][0]}..{res['done'][-1]}) in {res['seconds']} s")
        if res["stalled"]:  # the store or the network, not the dates (#447)
            parts.append(_stall_text(res["stalled"]))
        parts += [f"{d} failed ({e})" for d, e in res["failed"]
                  if not res["stalled"] or d not in res["stalled"]["dates"]][:5]
        if res["unconverged"]:
            parts.append(f"still stale after the run, tried again next cycle: {', '.join(res['unconverged'][:5])}")
        if res["skipped"]:
            parts.append(f"{len(res['skipped'])} dates not cached and no hub")
        if res["left"]:
            parts.append(f"{res['left']} dates in the next cycles")
        parts.append(span)
        return ("error" if res["failed"] else "ok"), "; ".join(parts)

    def _scan(self, settings: Settings, con: psycopg.Connection) -> tuple[str, str]:
        from .inbox import scan

        self._inbox_waiting = 0
        if not settings.scan_dirs:
            return "skipped", "no inbox folders (inbox_dirs)"
        res = scan.scan(con, settings, progress=lambda done, total, detail: self._progress("scan", done, total, detail),
                        history_from=self._hub_first)
        self._inbox_waiting = res.files.get("waiting_calendar", 0)
        detail = "; ".join(res.text().splitlines())
        if res.busy:
            return "skipped", detail
        return ("ok" if res.ok else "error"), detail

    def _eod(self, settings: Settings, con: psycopg.Connection, url: str | None, extra: list[str],
             ran: list[str]) -> tuple[str, str]:
        from .cli import eod_run

        self._eod_tried = set()

        today = self._today()
        pdates = store.position_dates(con, today - timedelta(days=WINDOW_DAYS))
        if not pdates:
            return "skipped", "no positions in the store"
        sd = SnapshotDir(Path(settings.snapshot_cache))
        latest_valid = max(pdates).isoformat()
        start = min(today - timedelta(days=WINDOW_DAYS), max(pdates)).isoformat()
        first = min(pdates).isoformat()
        cached = sd.market_dates()
        dates = [d for d in cached if max(start, first) <= d <= today.isoformat()][-MAX_EOD_DATES:]
        dates = sorted(set(dates) | {d for d in (latest_valid, *extra) if d in cached and d >= first})
        if not dates:
            return "skipped", f"no cached eod_final snapshot from {max(start, first)} on"
        runs = store.default_runs(con, date.fromisoformat(dates[0]))
        logic = logic_version()
        done, failed, notes = [], [], []
        todo: list[tuple[str, str, tuple]] = []
        for d in dates:
            ids, n = store.current_sets(con, date.fromisoformat(d))
            if not n:
                continue
            snap = sd.market_id(d)
            pc = previous_close(sd, d)  # cli.eod_run's: the newest earlier close, its final preferred (#203)
            prev = pc[2] if pc else None
            run = runs.get(date.fromisoformat(d))
            inputs_hash = store.positions_for(con, date.fromisoformat(d)).private_inputs_hash
            why = ("no default run" if run is None else "positions changed" if run["position_set_ids"] != ids
                   else "market snapshot changed" if run["market_snapshot_id"] != snap
                   else "previous snapshot changed" if run["prev_market_snapshot_id"] != prev
                   else "private inputs changed" if run["private_inputs_hash"] != inputs_hash
                   else late_why(con, date.fromisoformat(d), run)  # an OCP report or balances after the run (TDM#181)
                   or ("program updated" if d == latest_valid and not same_logic(run.get("logic_version"), logic)
                       else None))
            if why is None:
                continue
            sig = (tuple(ids), snap, prev, logic, inputs_hash, store.dataset_set_ids(con, date.fromisoformat(d)))
            if self._failed_before(d, sig, store.slice_label(d, "eod_final")):
                self._eod_tried.add(d)  # not the backfill's either this cycle (#23)
                notes.append(f"{d} failed before on the same inputs, not retried")
                continue
            todo.append((d, why, sig))
        todo.sort(key=lambda t: not (t[0] == latest_valid and t[1] == "no default run"))  # stable: then oldest first
        if self._inbox_waiting and self._sync_later:
            held = [t for t in todo if t[0] != latest_valid and t[0] not in extra]
            if held:  # the backlog drains over the next cycles: these dates run once, after it (R22)
                todo = [t for t in todo if t not in held]
                notes.append(f"{len(held)} dates wait for the inbox backlog ({self._inbox_waiting} files wait for "
                             f"a calendar, {self._sync_later} hub dates next cycle)")
        for i, (d, why, sig) in enumerate(todo):
            self._progress("eod", i + 1, len(todo), d)
            self._eod_tried.add(d)
            self._emit(f"eod {d}: {why}")
            log.info("autopilot: eod %s (%s)", d, why)
            try:
                rid = eod_run(settings, d, snapshots=settings.snapshot_cache, store_url=url, report_dir=None,
                              out=self._eod_line)
            except Exception as e:
                log.exception("autopilot: eod %s failed", d)
                self._fail(d, sig, e)
                failed.append(f"{d} failed ({type(e).__name__}: {e})")
                continue
            self._failed.pop(d, None)
            if rid is not None:
                done.append(f"{d} run {rid} ({why})")
                ran.append(d)
        parts = (done + failed + notes or [f"up to date ({dates[0]}..{dates[-1]})"]) \
            + self._unmapped(settings, con, latest_valid)
        return ("error" if failed else "ok" if done else "skipped"), "; ".join(parts)

    @staticmethod
    def _unmapped(settings: Settings, con: psycopg.Connection, day: str) -> list[str]:
        """The latest positions date's bonds without an instrument, by reason with their ISINs (D6), for the eod
        step's detail; nothing when every bond resolves."""
        from .inbox import scan

        d = date.fromisoformat(day)
        try:
            u = scan.unmapped(settings, store.positions_for(con, d), d)
        except Exception as e:  # noqa: BLE001 - a status line never fails the step
            log.warning("autopilot: unmapped bonds of %s not listed: %s", day, e)
            return []
        return [f"{day} {scan.unmapped_text(u, limit=10)}"] if u.get("count") or u.get("error") else []

    def _business_day(self, settings: Settings, d: date) -> bool:
        """``d`` is a business day of the settlement calendar of the newest cached static snapshot (parsed once per
        static); Monday to Friday when there is none or it does not cover ``d``."""
        sd = SnapshotDir(Path(settings.snapshot_cache))
        statics = sorted(((m.get("business_date") or "", sid) for sid in sd.ids()
                          if (m := sd.manifest(sid) or {}).get("kind") == "static"), reverse=True)
        if statics:
            sid = statics[0][1]
            if self._calendar is None or self._calendar[0] != sid:
                try:
                    self._calendar = (sid, *StaticView(sd.get(sid)).business_calendar("MOEX_STOCK"))
                except Exception as e:  # noqa: BLE001 - a calendar that cannot be read: weekdays
                    log.warning("autopilot: no calendar from static %s: %s", sid[:12], e)
                    self._calendar = (sid, None, None, None)
            _, cal, vf, vt = self._calendar
            if cal is not None and vf <= d <= vt:
                return bool(cal.is_business_day(d))
        return d.weekday() < 5

    def _intraday(self, settings: Settings, con: psycopg.Connection, url: str | None,
                  ran: list[str]) -> tuple[str, str, dict[str, str]]:
        """Today's book on the hub's newest intraday (from 19:45: preliminary) cut of today; see the module doc.
        The status, the detail and {"reason": one of ``INTRADAY_REASONS``}. The step asks the hub itself: the sync
        step's outcome (a failed older date, #20) does not stop it, the hub found down this cycle does (``hub_error``,
        #147; a cut already cached still runs)."""
        status, detail, reason = self._intraday_step(settings, con, url, ran)
        return status, detail, {"reason": reason}

    def _intraday_step(self, settings: Settings, con: psycopg.Connection, url: str | None,
                       ran: list[str]) -> tuple[str, str, str]:
        from .cli import eod_run

        if not getattr(settings, "intraday", True):
            return "skipped", "off in the settings (intraday)", "off"
        now = self._now().astimezone(MSK)
        today, t = now.date(), now.time()
        if t < INTRADAY_FROM:
            return "skipped", f"before {INTRADAY_FROM:%H:%M} MSK", "before_open"
        if not self._business_day(settings, today):
            return "skipped", f"{today} is not a business day", "not_business_day"
        ids, n = store.current_sets(con, today)
        if not n:
            return "skipped", "no positions in the store", "no_positions"
        cut = "eod_preliminary" if t >= PRELIM_FROM else "intraday"
        rtype = "INTRADAY" if cut == "intraday" else "EOD"
        last = con.execute(
            "SELECT run_id, market_snapshot_id, cut_ts, position_set_ids, prev_market_snapshot_id FROM run"
            " WHERE business_date = %s"
            " AND run_type = %s AND cut_type = %s AND status = 'complete' ORDER BY run_id DESC LIMIT 1",
            (today, rtype, cut)).fetchone()
        slot = (today, cut, int((now.timestamp() - POLL_LAG_S) // POLL_SLOT_S))
        asked = False
        if self._poll_slot != slot and not (cut == "eod_preliminary" and last):
            if self._hub_down:  # an earlier step found the hub down: asked again next cycle (#147)
                return "skipped", f"hub down this cycle, {cut} cut of {today} asked next cycle", "hub_error"
            self._progress("intraday", 0, None, "срез хаба")
            self._polled = {k: v for k, v in self._polled.items() if k[0] == today}
            try:  # not remembered as asked on an error: asked again next cycle
                self._polled[(today, cut)] = next(HubClient(settings).list_snapshots(
                    kind="market", cut_type=cut, business_date=today.isoformat(), latest=True), None)
            except (HubError, OSError, ValueError) as e:
                if isinstance(e, HubUnavailable):
                    self._hub_down = str(e)
                return "error", f"hub: {cut} cut of {today} not listed ({e})", "hub_error"
            self._poll_slot, asked = slot, True
        row = self._polled.get((today, cut))
        target: tuple[str, str] | None = None  # (snapshot id, why)
        if row is not None:
            row_ts = datetime.fromisoformat(row["cut_ts"]) if row.get("cut_ts") else None
            newer = last is None or (row["snapshot_id"] != last[1]
                                     and (row_ts is None or last[2] is None or row_ts >= last[2]))
            if newer:
                target = (row["snapshot_id"], "new cut" if last is None or row_ts != last[2] else "cut corrected")
        if target is None and last is not None and sorted(last[3]) != ids:
            target = (last[1], "positions changed")
        sd = SnapshotDir(Path(settings.snapshot_cache))
        pc = previous_close(sd, today.isoformat())  # cli.eod_run's (#203)
        prev_id = pc[2] if pc else None
        if target is None and last is not None and last[4] != prev_id:
            target = (last[1], "previous close changed")  # the previous close arrived (or was corrected) after it
        when = "asked the hub" if asked else "next hub check in the next 10-minute slot"
        if target is None:
            if last is not None:
                return "ok", f"up to date: run {last[0]} on {store.slice_label(today, cut, last[2])} ({when})", \
                    "up_to_date"
            return "skipped", f"no {cut} cut of {today} on the hub yet ({when})", "no_cut_yet"
        sid, why = target
        sig = (cut, sid, tuple(ids), prev_id, logic_version())
        label = store.slice_label(today, cut, row.get("cut_ts") if row and sid == row["snapshot_id"] else last[2])
        if self._failed_before(f"{cut} {today}", sig, label):
            return "skipped", f"{cut} {sid[:12]} failed before on the same inputs, not retried", "failed_before"
        cache_dir = Path(settings.snapshot_cache)
        if row is not None and sid == row["snapshot_id"]:
            if self._hub_down and not _cached(cache_dir, row):  # a cached cut needs no hub: it runs (#147)
                return "skipped", f"hub down this cycle, {cut} {sid[:12]} fetched next cycle", "hub_error"
            self._progress("intraday", 0, 1, "загрузка среза")
            try:
                cache.fetch_row(HubClient(settings), cache_dir, row)
            except (HubError, SnapshotError, OSError, ValueError) as e:
                if isinstance(e, HubUnavailable):
                    self._hub_down = str(e)
                return "error", f"hub: {cut} {sid[:12]} not fetched ({e})", "hub_error"
        self._progress("intraday", 0, 1, label)
        self._emit(f"intraday {label}: {why}")
        log.info("autopilot: intraday %s %s (%s)", cut, sid[:12], why)
        try:
            rid = eod_run(settings, today.isoformat(), snapshots=cache_dir, store_url=url, report_dir=None,
                          out=self._eod_line, cut_type=cut, market_snapshot_id=sid, run_type=rtype)
        except Exception as e:
            log.exception("autopilot: intraday %s failed", label)
            self._fail(f"{cut} {today}", sig, e)
            return "error", f"{label} failed ({type(e).__name__}: {e})", "run_failed"
        self._failed.pop(f"{cut} {today}", None)
        if rid is None:
            return "skipped", f"{label}: no positions for {today}", "no_positions"
        ran.append(label)
        parts = [f"{label}: run {rid} ({why})"]
        basis = con.execute("SELECT lineage->'pnl_basis'->>'label' FROM run WHERE run_id = %s", (rid,)).fetchone()
        if basis and basis[0]:
            parts.append(basis[0])
        if cut == "intraday" and (gone := store.prune_intraday(con, today, rid)):
            parts.append(f"removed intraday run{'s' if len(gone) > 1 else ''} {', '.join(map(str, gone))}")
        if self._pruned_on != today:
            keep = {m for (m,) in con.execute(f"SELECT market_snapshot_id FROM run WHERE run_id IN ({store.CURRENT_RUNS})")}
            pruned = cache.prune(cache_dir, (today - timedelta(days=KEEP_CUT_DAYS)).isoformat(), keep)
            self._pruned_on = today
            if pruned:
                parts.append(f"{len(pruned)} old intraday/preliminary bundles pruned from the cache")
        self._progress("intraday", 1, 1, label)
        return "ok", "; ".join(parts), "ran"

    def _morning(self, settings: Settings, con: psycopg.Connection, url: str | None,
                 ran: list[str]) -> tuple[str, str, dict[str, str]]:
        """The morning run (#21): today's book on the previous business day's close until today has a run of its
        own; see the module doc. The status, the detail and {"reason": one of ``MORNING_REASONS``}."""
        status, detail, reason = self._morning_step(settings, con, url, ran)
        return status, detail, {"reason": reason}

    def _morning_step(self, settings: Settings, con: psycopg.Connection, url: str | None,
                      ran: list[str]) -> tuple[str, str, str]:
        from .cli import eod_run

        now = self._now().astimezone(MSK)
        today = now.date()
        if not self._business_day(settings, today):
            return "skipped", f"{today} is not a business day", "not_business_day"
        if con.execute("SELECT EXISTS (SELECT 1 FROM run WHERE business_date = %s AND status = 'complete'"
                       " AND (run_type = 'EOD' OR run_type = 'INTRADAY' AND cut_type <> 'prev_close'))",
                       (today,)).fetchone()[0]:
            return "skipped", f"{today} has a run on its own market", "superseded"
        ids, n = store.current_sets(con, today)
        if not n:
            return "skipped", "no positions in the store", "no_positions"
        if now.time() < MORNING_FROM and not con.execute(  # a report of today is in: its start_of_day report has
                "SELECT EXISTS (SELECT 1 FROM position_set WHERE set_id = ANY(%s)"  # valid_date D-1, report_date D
                " AND (report_date = %s OR valid_date = %s))", (ids, today, today)).fetchone()[0]:
            return "skipped", f"before {MORNING_FROM:%H:%M} MSK", "before_morning"
        expected = today - timedelta(days=1)
        for _ in range(31):  # the previous business day
            if self._business_day(settings, expected):
                break
            expected -= timedelta(days=1)
        sd = SnapshotDir(Path(settings.snapshot_cache))
        close = previous_market(sd, today.isoformat())  # the newest earlier close, its final preferred
        if close is None or close[0] != expected.isoformat():
            return "skipped", f"no close of {expected} in the cache", "no_close"
        snap = sd.market_id(*close)
        last = con.execute(
            "SELECT run_id, market_snapshot_id, position_set_ids, lineage->>'logic_version' FROM run"
            " WHERE business_date = %s AND run_type = 'INTRADAY' AND cut_type = 'prev_close' AND status = 'complete'"
            " ORDER BY run_id DESC LIMIT 1", (today,)).fetchone()
        logic = logic_version()
        why = (f"no run of {today} yet" if last is None else "positions changed" if sorted(last[2]) != ids
               else "close changed" if last[1] != snap
               else "program updated" if not same_logic(last[3], logic) else None)
        label = store.slice_label(today, "prev_close", None, None, {"date": close[0], "cut_type": close[1]})
        if why is None:
            return "ok", f"up to date: run {last[0]} on {label}", "up_to_date"
        if self._inbox_waiting and self._sync_later:  # as the eod step (R22): not once per revision of the backlog
            return "skipped", (f"{label}: after the inbox backlog ({self._inbox_waiting} files wait for a calendar, "
                               f"{self._sync_later} hub dates next cycle)"), "inbox_backlog"
        sig = (snap, tuple(ids), logic)
        if self._failed_before(f"prev_close {today}", sig, label):
            return "skipped", f"{label} failed before on the same inputs, not retried", "failed_before"
        self._progress("morning", 0, 1, label)
        self._emit(f"morning {label}: {why}")
        log.info("autopilot: morning %s (%s)", label, why)
        try:
            rid = eod_run(settings, today.isoformat(), snapshots=settings.snapshot_cache, store_url=url,
                          report_dir=None, out=self._eod_line, run_type="INTRADAY", on_close=close,
                          market_snapshot_id=snap)
        except Exception as e:
            log.exception("autopilot: morning %s failed", label)
            self._fail(f"prev_close {today}", sig, e)
            return "error", f"{label} failed ({type(e).__name__}: {e})", "run_failed"
        self._failed.pop(f"prev_close {today}", None)
        if rid is None:
            return "skipped", f"{label}: no positions for {today}", "no_positions"
        ran.append(label)
        parts = [f"{label}: run {rid} ({why})"]
        if gone := store.prune_intraday(con, today, rid):  # the earlier morning runs of today
            parts.append(f"removed run{'s' if len(gone) > 1 else ''} {', '.join(map(str, gone))}")
        self._progress("morning", 1, 1, label)
        return "ok", "; ".join(parts), "ran"

    def _eod_line(self, line: str) -> None:
        log.info("eod: %s", line)
        self._emit("  " + line)
