"""The autopilot (I4): hub snapshots, the inbox and the EOD runs kept current without an operator.

One cycle (``Autopilot.run_cycle``; ``desk cycle`` runs one in the foreground, ``desk serve`` a thread that runs one
every ``inbox_poll_seconds`` and on ``POST /api/v1/autopilot/run``):

1. **sync**: from the hub (when its address and token are set) the ``autopilot_sync_last`` latest business dates,
   each business date with positions in the last ``WINDOW_DAYS`` (and the latest one before) with the hub date
   before it (flash PnL), and for each inbox file waiting for a calendar the newest hub date on or before its
   report date; dates already cached are skipped, but rechecked once per ``RECHECK_S`` (a hub correction). The
   snapshot drop folder, when set, is imported too. A hub error is the step's error: the cycle goes on with the
   cache.
2. **scan**: ``desk inbox scan`` (when inbox folders are set). Sync comes first: a ``start_of_day`` report needs a
   cached calendar covering its date and the day before, otherwise it waits (``waiting_calendar``).
3. **eod**: for each cached ``eod_final`` business date from ``min(today - WINDOW_DAYS, latest positions date)``
   on (at most ``MAX_EOD_DATES``, the latest positions date always) that has positions in the store, the EOD run of
   ``desk eod`` when the date has no default run, or its default run read other position sets (a new revision of a
   book), another market snapshot (a correction) or another previous snapshot (the flash PnL). Oldest first. A
   date that failed is not retried until its inputs change.
4. **status** (``status()``, the shape of ``GET /api/v1/autopilot/status``).

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
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import psycopg

from . import store
from .inbox.timesem import MSK
from .marketdata import cache
from .marketdata.bundle import SnapshotDir, SnapshotError
from .marketdata.hub import HubClient, HubError
from .settings import Settings, load

log = logging.getLogger("desk.autopilot")

WINDOW_DAYS = 14        # calendar days back from today that the EOD runs are kept current for (~10 business days)
MAX_EOD_DATES = 10
RECHECK_S = 3600.0      # cached dates are asked of the hub again this often (corrections)
MAX_HUB_DATES = 400     # how far back the hub's date list is read at most
MIN_SLEEP_S = 1.0
LOCK_FILE = "cycle.lock"

LATEST_RUN = (f"SELECT run_id, business_date, completed_at FROM run WHERE run_id IN ({store.DEFAULT_RUNS})"
              " ORDER BY business_date DESC LIMIT 1")


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat(timespec="seconds") if ts else None


def latest_run(con: psycopg.Connection) -> dict[str, str] | None:
    """The default run of the latest business date: {run_id, as_of, created_at} (I4), None without one."""
    row = con.execute(LATEST_RUN).fetchone()
    if row is None:
        return None
    rid, d, done = row
    return {"run_id": str(rid), "as_of": d.isoformat(), "created_at": _iso(done)}


class CycleLock:
    """The inter-process cycle lock ``<home>/cycle.lock``, non-blocking. ``msvcrt.locking`` on Windows,
    ``fcntl.flock`` elsewhere; the operating system releases it when the process dies, so a crashed cycle leaves
    no stale lock. Two opens of the file exclude each other within one process too."""

    def __init__(self, home: Path) -> None:
        self.path = Path(home) / LOCK_FILE
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
            "steps": [], "latest_run": None}


class Autopilot:
    """``force``: run cycles even when the settings say ``autopilot: false`` (``desk cycle``,
    ``desk serve --autopilot``). ``out``: a callback for progress lines (``desk cycle`` prints them)."""

    def __init__(self, *, settings_loader: Callable[[], Settings] = load, store_url: str | None = None,
                 force: bool = False, today: Callable[[], date] | None = None,
                 out: Callable[[str], None] | None = None) -> None:
        self._load, self._store_url, self.force, self._out = settings_loader, store_url, force, out
        self._today = today or (lambda: datetime.now(MSK).date())
        self._cycle = threading.Lock()
        self._lock = threading.Lock()
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._failed: dict[str, tuple] = {}
        self._rechecked = float("-inf")
        self._interval = 60.0
        self._on = True
        self._log_handler: RotatingFileHandler | None = None
        self._status = idle_status(True, "Ожидает первого цикла")

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
        """Run a cycle now (after the running one, if any)."""
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
        st["enabled"] = self.running and (self.force or self._on)
        return st

    # ---- one cycle

    def run_cycle(self, extra_dates: Iterable[str] = (), *, hub: bool = True) -> dict[str, Any]:
        """One cycle, synchronous; returns the status. ``extra_dates``: business dates to sync and keep current
        besides the automatic ones (``desk cycle --date``); ``hub=False`` skips the hub (the drop is imported)."""
        with self._cycle:
            started = datetime.now(UTC)
            self._update(state="running", message="Цикл выполняется", steps=[])
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
                    self._status.update(state="idle", message="Автопилот выключен в настройках", steps=[])
                return self.status()
            lock = CycleLock(settings.home)
            try:
                held = lock.acquire()
            except OSError as e:  # no lock file (the home folder is not writable): no cycle
                steps.append(self._step("lock", "error", f"{type(e).__name__}: {e}"))
                return self._finish(started, steps, [])
            if not held:
                steps.append(self._step("cycle", "skipped", "another cycle running"))
                self._update(state="idle", last_cycle_at=_iso(started),
                             message="Пропущено: выполняется другой цикл (desk cycle или desk serve)")
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
        try:
            steps.append(self._guard("sync", lambda: self._sync(settings, con, extra, hub)))
            if con is not None:
                steps.append(self._guard("scan", lambda: self._scan(settings, con)))
                steps.append(self._guard("eod", lambda: self._eod(settings, con, url, extra, ran)))
                try:
                    latest = latest_run(con)
                except psycopg.Error:
                    latest = None
                with self._lock:
                    self._status["latest_run"] = latest
        finally:
            if con is not None:
                con.close()
        return self._finish(started, steps, ran)

    def _finish(self, started: datetime, steps: list[dict], ran: list[str]) -> dict[str, Any]:
        errors = [s["name"] for s in steps if s["status"] == "error"]
        if errors:
            msg = "Ошибки: " + ", ".join(errors)
        elif ran:
            msg = "Пересчитано: " + ", ".join(ran)
        else:
            msg = "Данные актуальны"
        upd: dict[str, Any] = {"state": "error" if errors else "idle", "last_cycle_at": _iso(started),
                               "message": msg, "steps": steps}
        if not errors:
            upd["last_ok_at"] = _iso(started)
        self._update(**upd)
        log.info("autopilot: cycle done: %s", msg)
        return self.status()

    def _update(self, **kw: Any) -> None:
        with self._lock:
            self._status.update(kw)

    def _step(self, name: str, status: str, detail: str) -> dict[str, Any]:
        s = {"name": name, "status": status, "detail": detail, "at": _iso(datetime.now(UTC))}
        (log.warning if status == "error" else log.info)("autopilot: %s %s: %s", name, status, detail)
        self._emit(f"{name}: {status}" + (f" - {detail}" if detail else ""))
        with self._lock:
            self._status["steps"] = [*self._status["steps"], s]
        return s

    def _guard(self, name: str, fn: Callable[[], tuple[str, str]]) -> dict[str, Any]:
        try:
            status, detail = fn()
        except Exception as e:
            log.exception("autopilot: step %s failed", name)
            status, detail = "error", f"{type(e).__name__}: {e}"
        return self._step(name, status, detail)

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

    # ---- steps

    def _sync(self, settings: Settings, con: psycopg.Connection | None, extra: list[str],
              hub: bool) -> tuple[str, str]:
        parts, status = [], "ok"
        if hub and settings.hub_url:
            try:
                parts.append(self._hub_sync(settings, con, extra))
            except (HubError, SnapshotError, OSError, ValueError) as e:
                status = "error"
                parts.append(f"hub: {e}; the cached snapshots are used")
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
        if not parts:
            return "skipped", "hub not configured (hub_url)" if hub else "hub off (--no-hub), no drop folder"
        return status, "; ".join(parts)

    def _hub_sync(self, settings: Settings, con: psycopg.Connection | None, extra: list[str]) -> str:
        client = HubClient(settings)  # raises HubError without a token on the first request
        n = int(getattr(settings, "autopilot_sync_last", 5) or 0)
        today = self._today()
        pos, cover = list(extra), []
        if con is not None:
            pos += [d.isoformat() for d in store.position_dates(con, today - timedelta(days=WINDOW_DAYS))]
            cover = [d.isoformat() for (d,) in con.execute(
                "SELECT DISTINCT report_date FROM (SELECT DISTINCT ON (sha256) status, report_date"
                " FROM inbox_file_status ORDER BY sha256, status_id DESC) s"
                " WHERE status = 'waiting_calendar' AND report_date IS NOT NULL")]
        floor = min(pos + cover, default=None)
        hub_dates: list[str] = []  # newest first
        for d in cache.hub_dates(client):
            hub_dates.append(d)
            if (len(hub_dates) >= n and (floor is None or d < floor)) or len(hub_dates) >= MAX_HUB_DATES:
                break
        wanted = set(hub_dates[:n])
        for d in pos:  # the date and the hub date before it (flash PnL)
            wanted.add(d)
            wanted.add(next((x for x in hub_dates if x < d), d))
        for r in cover:  # a cached calendar covering the report date (an older snapshot when none is earlier)
            wanted.add(next((x for x in hub_dates if x <= r), hub_dates[-1] if hub_dates else r))
        wanted &= set(hub_dates)
        recheck = time.monotonic() - self._rechecked >= RECHECK_S
        cached = set(SnapshotDir(Path(settings.snapshot_cache)).market_dates())
        todo = sorted(d for d in wanted if recheck or d not in cached)
        rows = cache.sync(client, Path(settings.snapshot_cache), todo)
        if recheck:
            self._rechecked = time.monotonic()
        pulled = [s.business_date for s in rows if s.downloaded]
        latest = hub_dates[0] if hub_dates else "none"
        return (f"hub {client.host}: latest {latest}; " + (f"pulled {', '.join(pulled)}" if pulled else "up to date")
                + f" ({len(wanted)} dates wanted, {len(todo)} asked)")

    def _scan(self, settings: Settings, con: psycopg.Connection) -> tuple[str, str]:
        from .inbox import scan

        if not settings.inbox_dirs:
            return "skipped", "no inbox folders (inbox_dirs)"
        res = scan.scan(con, settings)
        detail = "; ".join(res.text().splitlines())
        if res.busy:
            return "skipped", detail
        return ("ok" if res.ok else "error"), detail

    def _eod(self, settings: Settings, con: psycopg.Connection, url: str | None, extra: list[str],
             ran: list[str]) -> tuple[str, str]:
        from .cli import eod_run

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
        done, failed, notes = [], [], []
        for d in dates:
            ids, n = store.current_sets(con, date.fromisoformat(d))
            if not n:
                continue
            snap = sd.market(d).id
            prev_d = sd.previous_market_date(d)
            prev = sd.market(prev_d).id if prev_d else None
            run = runs.get(date.fromisoformat(d))
            why = ("no default run" if run is None else "positions changed" if run["position_set_ids"] != ids
                   else "market snapshot changed" if run["market_snapshot_id"] != snap
                   else "previous snapshot changed" if run["prev_market_snapshot_id"] != prev else None)
            if why is None:
                continue
            sig = (tuple(ids), snap, prev)
            if self._failed.get(d) == sig:
                notes.append(f"{d} failed before on the same inputs, not retried")
                continue
            self._emit(f"eod {d}: {why}")
            log.info("autopilot: eod %s (%s)", d, why)
            try:
                rid = eod_run(settings, d, snapshots=settings.snapshot_cache, store_url=url, report_dir=None,
                              out=self._eod_line)
            except Exception as e:
                log.exception("autopilot: eod %s failed", d)
                self._failed[d] = sig
                failed.append(f"{d} failed ({type(e).__name__}: {e})")
                continue
            self._failed.pop(d, None)
            if rid is not None:
                done.append(f"{d} run {rid} ({why})")
                ran.append(d)
        parts = done + failed + notes
        if failed:
            return "error", "; ".join(parts)
        return ("ok" if done else "skipped"), "; ".join(parts) or f"up to date ({dates[0]}..{dates[-1]})"

    def _eod_line(self, line: str) -> None:
        log.info("eod: %s", line)
        self._emit("  " + line)
