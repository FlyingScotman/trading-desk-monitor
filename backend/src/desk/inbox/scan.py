"""The inbox scanner (08 §6, §13): report files of ``DESK_INBOX_DIRS`` through the private adapter into the desk
store. ``scan`` takes in new files, ``retry`` and ``replay`` offer known ones again, ``status`` sums the ledger
up for ``desk inbox status``. Unlike the rest of ``desk.inbox`` this module uses the store (psycopg).

One scan at a time: a Postgres advisory lock; a second scan finds it taken and does nothing. The adapter is
loaded once per process; when it fails to load, ingestion is off with the reason and nothing is touched.

The folders are ``Settings.scan_dirs``: ``DESK_INBOX_DIRS`` and the adapter's output folder ``<home>/adapter-output``
(08 §3 ``run_action``: the backend writes its files there, one folder per action, replaced by a single rename).
A file of the folders (temp, hidden and oversized files skipped) is **offered** once it is stable: its size and
modification time unchanged since the previous scan of this process (the watcher), or its modification time
older than ``DESK_INBOX_POLL_SECONDS`` (so a one-shot ``desk inbox scan`` takes files copied earlier); a file of
the output folder is stable as soon as it is there. A path
with the size and time already taken in is not hashed again. Then, per file:

* known content under a new path: ``duplicate`` (same source and report date: nothing) or ``stale_copy`` (another
  report date, event ``input.stale``), recorded on the sighting; the content's own status is unchanged;
* ``match`` (one batch per scan): None → ``ignored``; an exception or a bad hit → ``match_failed``;
* a matched file is archived under ``<archive>/<sha256>/<name>`` (the name kept: adapters match by name);
* the business date (``timesem.valid_date`` on the calendar of the cached static snapshots); no calendar →
  ``waiting_calendar``, offered again by every scan;
* ``read``: raises or times out → ``failed`` / ``timed_out``; else ``validate.check_read``, then per unit
  (dataset, scope unit, valid date; a unit of a multi-date file has its row's date): rejected, or ``superseded``
  (sorts below the current revision by report key, or another source of higher priority has it), or a
  ``scope_conflict`` (another source, no priorities), or accepted, a new revision unless its ``unit_hash`` equals
  the current revision's: a position set for ``positions`` (per book), a ``dataset_set`` for every other dataset
  with a schema (``ocp``, ``cash_balances``, ``pnl_official``, ``trade_terms``; private/1.2);
* the file: ``accepted``, ``partially_accepted``, ``rejected`` or ``superseded`` (all its units).

The lookback (``inbox_lookback_days``, ``scan`` only; ``retry`` and ``replay`` are explicit and take any date) is
decided by the report date ``match`` gives (R22): a file whose report date is older is remembered once as
``ignored_too_old`` (terminal, not an error, no event) and never archived or read. Only a file modified more than
``MTIME_SLACK_DAYS`` before the lookback starts is skipped on its modification time alone (``skipped: too_old``,
not even matched): a report is written on or after its report date, so it cannot fall inside the window; a file ``waiting_calendar`` whose report date falls behind gets that status too, so it
stops driving hub syncs. None of them is offered again while still too old, fingerprint change or not; after the
lookback grows they are. ``desk backfill`` (R23) scans with the lookback starting on its own start date
(``scan(since=...)``), so the older reports come in once.

Speed (a folder holds thousands of old reports; a scan with nothing new must cost a folder listing, R22): the
folders are listed once with ``os.scandir`` (on Windows the listing carries the sizes and times: no request per
file on a network share). Every offered file is matched first, by the adapter's ``match`` alone, without hashing
it: the workers are started and warmed first (the adapter imported, the instruments index loaded, under the read
timeout), then the batches of ``MATCH_BATCH`` files go to all of them at once, each under the match timeout. The
files of a call that timed out or crashed are matched once more with ``MATCH_RETRY_FACTOR`` times the time,
``MATCH_RETRY_CHUNK`` per call after a timeout and one per call after a crash (a file that kills its worker then
takes only itself); a file still failing that way is ``match_failed`` and offered again by a
later scan once ``TRANSIENT_RETRY_H`` hours have passed (an exception of ``match`` itself is the adapter's answer and
waits for a new fingerprint, as before). A new file that ``match`` declines
(``ignored``) or dates behind the lookback (``ignored_too_old``) is remembered by path, size and time in
``inbox_unread`` and never hashed, archived or ledgered; it is offered again only when the adapter's fingerprint
changes (ignored) or the lookback reaches its date (too old). The rest is processed newest report date first, so
the latest positions land before the backlog. The adapter runs in ``read_workers()`` long-lived workers
(``runner.pooled``), started only when a scan has files to match; reads run in parallel threads a window ahead of
the processing, which stays in order and in one thread. ``reference()`` (books, and since private/1.2 the optional
``entities`` and ``funding_rules``, stored as ``reference_rev``) runs only in a scan that reads a file, or
once after the adapter's fingerprint (code or config files) changed, in parallel with the reads. Files waiting
for a calendar are matched again only after the cache's static snapshots changed. The instruments index is
rebuilt only when it lacks a snapshot the scan needs (a new day, a report date of an older snapshot); it keeps the
snapshots it already holds for dates inside the lookback, so a scan rebuilds it at most once and the next scans
not at all (``static_index`` format 2: the newest snapshot in full, the older ones as differences).

Files are processed one transaction each: an interrupted scan (``stop.bat``, a crash) keeps every file done so
far (its sighting), and the next scan goes on with the rest. ``progress(done, total, detail)`` is called as it
goes (the autopilot status).

Every status change is a new ``inbox_file_status`` row and an event. When the adapter fingerprint differs from
the one that set it, a file in ``ignored``, ``match_failed``, ``failed``, ``timed_out`` or ``rejected`` is offered
again (from the archive when its path is gone). Logs and results carry codes, counts and row numbers, never
row values or exception messages.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import store
from ..settings import Settings, adapter_config_dir
from . import runner
from .conformance import list_entries
from .loader import AdapterHandle, load_adapter
from .spi import DATASETS_WITH_BOOK, REFERENCE_KINDS, AdapterFailure, source_datasets
from .static_index import (
    UNMAPPED_REASONS,
    CalendarUnavailable,
    build_index,
    business_calendar,
    covering,
    index_refs,
    static_snapshots,
    unmapped_bonds,
)
from .timesem import MSK, parse_ts, report_date_suspicious, revision_key, valid_date
from .validate import check_hit, check_read, check_reference_tables, unit_hash

log = logging.getLogger(__name__)

SCAN_LOCK = 0x6465736C  # one scan at a time; the store migrator holds 0x6465736B
REOFFER = ("ignored", "match_failed", "failed", "timed_out", "rejected")
TOO_OLD = "ignored_too_old"
QUIET = (TOO_OLD,)  # statuses without an event (thousands of old files would flood the event log)
UNREAD = ("ignored", TOO_OLD)  # what a new file never read becomes: a row of inbox_unread, not of the ledger
READ_WORKERS_ENV = "DESK_INBOX_READ_WORKERS"
REFERENCE_MARK = "reference.done"  # in the archive folder: the fingerprint the books were last refreshed with
READ_DONE = ("accepted", "partially_accepted", "superseded")
MATCH_BATCH = 50
MATCH_RETRY_CHUNK = 10    # files per call when the files of a timed-out or crashed call are matched again
MATCH_RETRY_FACTOR = 4    # ... with this many times the match timeout (at least MATCH_RETRY_MIN_S)
MATCH_RETRY_MIN_S = 60.0
TRANSIENT_RETRY_H = 6     # a file match_failed by a timeout or a crash is offered again after this many hours
MTIME_SLACK_DAYS = 31     # a file modified this much before the lookback is skipped without asking match
EVENTS = {"stale_copy": "input.stale", "timed_out": "input.failed"}

_handles: dict[tuple, AdapterHandle] = {}
_last_seen: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) at this process's previous scan
_static_seen: dict[str, tuple[str, ...]] = {}  # snapshot cache -> its static snapshot ids at the previous scan
LIST_TICK = 500  # progress while listing the folders, every this many files
Progress = Callable[[int, int | None, str], None]


def read_workers() -> int:
    """Adapter workers of a scan (parallel reads): ``DESK_INBOX_READ_WORKERS``, else up to 4, a core left free."""
    try:
        n = int(os.environ.get(READ_WORKERS_ENV) or 0)
    except ValueError:
        n = 0
    return n if n > 0 else max(1, min(4, (os.cpu_count() or 2) - 1))


def lookback_cutoff(settings: Settings, today: date) -> date | None:
    """The oldest report date (and file modification date) the inbox scan takes; None: no limit."""
    days = int(getattr(settings, "inbox_lookback_days", 0) or 0)
    return today - timedelta(days=min(days, 36500)) if days > 0 else None


def adapter(settings: Settings) -> AdapterHandle:
    """The adapter of the settings, loaded once per process (08 §2; ``forget_adapters`` after a settings edit);
    never raises."""
    cfg = adapter_config_dir(settings)
    dirs = tuple(str(d) for d in settings.inbox_dirs)
    key = (settings.private_adapter, cfg, settings.private_adapter_sha256, dirs)
    if key not in _handles:
        _handles[key] = load_adapter(settings.private_adapter, str(cfg) if cfg else None,
                                     settings.private_adapter_sha256, inbox_dirs=dirs)
    return _handles[key]


def forget_adapters() -> None:
    """Drop the loaded handles: the next ``adapter()`` loads again (the adapter or its config files changed)."""
    _handles.clear()


def adapter_line(h: AdapterHandle) -> str:
    """``ok <name> <version> <fingerprint[:12]>`` or ``error <reason>`` (``/health/ready``, ``desk inbox``)."""
    return f"ok {h.name} {h.version} {h.fingerprint[:12]}" if h.ok else f"error {h.error}"


@dataclass
class ScanResult:
    adapter: str
    busy: bool = False
    files: Counter = field(default_factory=Counter)     # status of each file processed
    copies: Counter = field(default_factory=Counter)    # duplicate, stale_copy
    skipped: Counter = field(default_factory=Counter)   # waiting (not stable yet), too_large, too_old, gone, unreadable
    units: Counter = field(default_factory=Counter)
    revisions: int = 0
    books_revised: int = 0
    reference_revised: Counter = field(default_factory=Counter)  # entities, funding_rules: rows revised
    notes: Counter = field(default_factory=Counter)     # reference() problems by code
    match: Counter = field(default_factory=Counter)     # retried, recovered, warm_<failure>: the match step (R22)
    timings: dict = field(default_factory=dict)         # list_s, match_s, process_s, index_builds

    @property
    def ok(self) -> bool:
        return self.adapter.startswith("ok") and not self.busy

    def text(self) -> str:
        def c(x: Counter) -> str:
            return ", ".join(f"{k} {v}" for k, v in sorted(x.items())) or "none"
        lines = [f"adapter: {self.adapter}"]
        if not self.adapter.startswith("ok"):
            return "\n".join([*lines, "ingestion is off"])
        if self.busy:
            return "\n".join([*lines, "another scan holds the inbox lock: nothing done"])
        lines += [f"files: {c(self.files)}", f"copies: {c(self.copies)}", f"skipped: {c(self.skipped)}",
                  f"units: {c(self.units)}; new revisions {self.revisions}; books revised {self.books_revised}"]
        if self.reference_revised:
            lines.append(f"reference revised: {c(self.reference_revised)}")
        if self.match:
            lines.append(f"match: {c(self.match)}")
        if self.timings:
            lines.append("time: " + ", ".join(f"{k} {v:g}" for k, v in self.timings.items()))
        return "\n".join(lines + ([f"notes: {c(self.notes)}"] if self.notes else []))


@dataclass(frozen=True)
class Offer:
    sha256: str        # "" for a new file: hashed when its batch comes up
    path: str          # what match and read get: the inbox path, or the archive copy
    name: str          # the file name as delivered
    size: int
    mtime: datetime    # the delivered file's modification time (last part of the revision key)
    sighting: bool     # a new path in the inbox: record it, and look for a copy of known content


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _issue(severity: str, code: str, scope: dict | None, message: str) -> dict:
    return {"severity": severity, "code": code, "src": None, "scope": scope, "message": message, "count": 1,
            "origin": "backend"}


def _transient(hit: Any) -> bool:
    """A match result that is a timeout or a crash of the call, not the adapter's answer about the file."""
    e = hit.get("_error") if isinstance(hit, dict) else None
    return isinstance(e, dict) and e.get("type") in runner.TRANSIENT


def _key(report_date: date, report_ts: datetime | None, seq: int | None, generated_at: datetime | None,
         mtime: datetime | None) -> tuple:
    return revision_key(report_date, report_ts.isoformat() if report_ts else None, seq,
                        generated_at.isoformat() if generated_at else None, mtime.timestamp() if mtime else None)


class _Scan:
    def __init__(self, con: psycopg.Connection, settings: Settings, h: AdapterHandle, res: ScanResult,
                 now: datetime, cutoff: date | None = None, progress: Progress | None = None,
                 expected: tuple[str, str, frozenset[str]] | None = None) -> None:
        self.con, self.settings, self.h, self.res, self.now = con, settings, h, res, now
        self.cutoff, self._progress = cutoff, progress
        self.expected = expected
        self.cache = settings.snapshot_cache
        self.archive = Path(settings.inbox_archive)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.index = self.archive / "instruments.json"
        self.calendars: dict[str, Any] = {}
        self._books: list[dict] | None = None
        self._books_done = False
        self._ref: Future | None = None    # reference() running in parallel with the first reads
        self._ex: ThreadPoolExecutor | None = None
        self._reads: dict[str, Future] = {}  # offer path -> its read, started ahead of the processing
        self._indexed: set[str] = set()   # report dates the instruments index covers beyond the newest snapshot

    def progress(self, done: int, total: int | None, detail: str) -> None:
        if self._progress is not None:
            try:
                self._progress(done, total, detail)
            except Exception:
                log.debug("inbox: progress callback failed", exc_info=True)

    def too_old(self, report_date: str | date | None) -> bool:
        if self.cutoff is None or report_date is None:
            return False
        return (report_date if isinstance(report_date, str) else report_date.isoformat()) < self.cutoff.isoformat()

    # ---- books and calendar

    def _needs_books(self) -> bool:
        return any(d in DATASETS_WITH_BOOK for s in self.h.describe["sources"] for d in source_datasets(s))

    @property
    def books(self) -> list[dict] | None:
        """The books validation uses: ``reference()`` once per scan, on first need (a read to check)."""
        if not self._books_done:
            self._books = self._reference_books()
            self._books_done = True
        return self._books

    def _reference_books(self) -> list[dict] | None:
        """``reference()``: a new ``book_rev`` where a book changed. The books it returns are the ones validation
        uses; when it fails, those in force from earlier scans."""
        if not self._needs_books():
            return None
        try:
            ref = self._ref.result() if self._ref is not None else \
                runner.call_reference(self.h, self.index, self.settings.adapter_read_timeout_s)
            tables, issues = check_reference_tables(ref)
            books = tables["books"]
        except AdapterFailure as f:
            tables, books, issues = {}, None, [{"code": f.kind}]
        self.res.notes.update(f"reference_{i['code']}" for i in issues)
        if books is None:
            log.warning("inbox: reference() unusable (%s): the stored books are used", ", ".join(self.res.notes))
            return store.current_books(self.con, self.now.astimezone(MSK).date())
        revised = store.store_books(self.con, books, self.h.fingerprint)  # type: ignore[arg-type]
        self.res.books_revised = len(revised)
        if revised:
            self._event("books.changed", counts={"books": len(revised)})
        for kind in REFERENCE_KINDS[1:]:  # entities, funding_rules: optional tables (private/1.2)
            if tables.get(kind) is not None:
                changed = store.store_reference(self.con, kind, tables[kind], self.h.fingerprint)  # type: ignore[arg-type]
                if changed:
                    self.res.reference_revised[kind] += len(changed)
                    self._event("reference.changed", counts={kind: len(changed)})
        try:
            (self.archive / REFERENCE_MARK).write_text(self.h.fingerprint or "", encoding="utf-8")
        except OSError:
            pass
        return books

    def refresh_books(self) -> None:
        """At the end of a scan that read nothing: ``reference()`` once after the fingerprint changed (a new adapter
        or config files: renamed or new books), not on every idle scan."""
        if self._books_done or not self._needs_books():
            return
        try:
            done = (self.archive / REFERENCE_MARK).read_text(encoding="utf-8").strip()
        except OSError:
            done = None
        if done != self.h.fingerprint:
            with self.con.transaction():
                _ = self.books

    def calendar(self, report_date: str):
        if report_date not in self.calendars:
            try:
                self.calendars[report_date] = business_calendar(self.cache, date.fromisoformat(report_date))
            except CalendarUnavailable:
                self.calendars[report_date] = None
        if self.calendars[report_date] is None:
            raise CalendarUnavailable(report_date)
        return self.calendars[report_date]

    def valid_date(self, decl: dict, report_date: str) -> date:
        tb = decl["time_basis"]
        return valid_date(report_date, tb, self.calendar(report_date) if tb == "start_of_day" else None)

    # ---- what is offered

    def new_files(self) -> list[Offer]:
        """New stable files, not hashed yet (``run`` hashes them batch by batch). Files modified before the
        lookback are skipped on their ``stat`` alone."""
        out, limit = [], self.settings.inbox_max_file_mb * 2**20
        oldest = datetime.combine(self.cutoff - timedelta(days=MTIME_SLACK_DAYS), datetime.min.time(),
                                  MSK).timestamp() if self.cutoff else None
        t0 = time.perf_counter()
        self.progress(0, None, "просмотр папок")
        seen = set(self.con.execute("SELECT path, size_bytes, mtime FROM inbox_sighting").fetchall())
        unread = {(p, n, m): (st, fp, rd) for p, n, m, st, fp, rd in self.con.execute(
            "SELECT path, size_bytes, mtime, status, adapter_fingerprint, report_date FROM inbox_unread")}
        entries = list_entries([str(d) for d in getattr(self.settings, "scan_dirs", self.settings.inbox_dirs)])
        out_dir = getattr(self.settings, "adapter_output_dir", None)
        written = str(out_dir) + os.sep if out_dir is not None else None  # complete when it appears (one rename)
        self.res.timings["list_s"] = round(time.perf_counter() - t0, 2)
        for n, (p, st) in enumerate(entries, 1):
            if n % LIST_TICK == 0:
                self.progress(n, None, "просмотр папок")
            if oldest is not None and st.st_mtime < oldest:
                self.res.skipped["too_old"] += 1
                continue
            if st.st_size > limit:
                self.res.skipped["too_large"] += 1
                continue
            stable = (_last_seen.get(p) == (st.st_size, st.st_mtime_ns) or bool(written and p.startswith(written))
                      or self.now.timestamp() - st.st_mtime >= self.settings.inbox_poll_seconds)
            _last_seen[p] = (st.st_size, st.st_mtime_ns)
            if not stable:
                self.res.skipped["waiting"] += 1
                continue
            mtime = datetime.fromtimestamp(st.st_mtime, UTC)
            if (p, st.st_size, mtime) in seen:
                continue
            u = unread.get((p, st.st_size, mtime))
            if u and ((u[0] == "ignored" and u[1] == self.h.fingerprint) or (u[0] == TOO_OLD and self.too_old(u[2]))):
                continue
            out.append(Offer("", p, os.path.basename(p), st.st_size, mtime, True))
        return out

    def known(self, want: Callable[[str, str], bool], source: str | None = None,
              between: tuple[date, date] | None = None, *, fresh: bool = False,
              statuses: Iterable[str] | None = None, extra: str = "") -> list[Offer]:
        """Files whose current status and adapter fingerprint pass ``want``: from the archive, else from the
        path they were read at when it still holds the same content. ``fresh``: only those within the lookback
        (by report date, else by modification time). ``statuses``: only files in these (filtered by the query, so
        a scan does not walk the whole ledger); ``extra``: one more SQL condition on the latest status ``l``."""
        out = []
        for sha, status, fp, path, arch, src, vd, rd, name, size, mtime in self.con.execute(
                "SELECT l.sha256, l.status, l.adapter_fingerprint, l.path, l.archive_path, l.source, l.valid_date,"
                " l.report_date, f.name, f.size_bytes, f.mtime FROM (SELECT DISTINCT ON (sha256) * FROM"
                " inbox_file_status ORDER BY sha256, status_id DESC) l JOIN inbox_file f USING (sha256)"
                " WHERE (%s::text[] IS NULL OR l.status = ANY(%s))" + (f" AND ({extra})" if extra else "")
                + " ORDER BY l.sha256", (list(statuses) if statuses is not None else None,
                                         list(statuses) if statuses is not None else None)).fetchall():
            if not want(status, fp) or (source is not None and src != source) \
                    or (between and not (vd and between[0] <= vd <= between[1])):
                continue
            if fresh and self.too_old(rd if rd is not None else mtime.astimezone(MSK).date()):
                continue
            p = arch if arch and os.path.isfile(arch) else \
                path if os.path.isfile(path) and _sha256(path) == sha else None
            if p is None:
                self.res.skipped["gone"] += 1
                continue
            out.append(Offer(sha, p, name, size, mtime, False))
        return out

    def transient(self) -> list[Offer]:
        """Files ``match_failed`` by a timeout or a crash of the adapter's worker (not by an exception of ``match``)
        more than ``TRANSIENT_RETRY_H`` hours ago, within the lookback: offered again whatever the fingerprint."""
        cut = self.now - timedelta(hours=TRANSIENT_RETRY_H)
        types = ", ".join(f"'{t}'" for t in runner.TRANSIENT)
        return self.known(lambda st, fp: True, fresh=True, statuses=("match_failed",),
                          extra=f"l.error_type IN ({types}) AND l.at < '{cut.isoformat()}'::timestamptz")

    def unread(self, statuses: Iterable[str]) -> list[Offer]:
        """New-file offers of the ``inbox_unread`` files in these statuses still at their path unchanged."""
        out = []
        for p, size, mtime in self.con.execute("SELECT path, size_bytes, mtime FROM inbox_unread WHERE status = ANY(%s)",
                                               (list(statuses),)).fetchall():
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size == size and datetime.fromtimestamp(st.st_mtime, UTC) == mtime:
                out.append(Offer("", p, os.path.basename(p), size, mtime, True))
        return out

    def calendars_changed(self) -> bool:
        """The cache's static snapshots (the calendars) differ from those of this process's previous scan."""
        ids = tuple(r.id for r in static_snapshots(self.cache))
        key = str(self.cache)
        changed = _static_seen.get(key) != ids
        _static_seen[key] = ids
        return changed

    def retire_too_old(self) -> None:
        """Files waiting for a calendar whose report date fell behind the lookback: ``ignored_too_old`` (no file
        access, the ledger only)."""
        if self.cutoff is None:
            return
        rows = self.con.execute(
            "SELECT l.sha256, l.path, l.archive_path, l.source, l.report_date, f.name, f.size_bytes, f.mtime"
            " FROM (SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC) l"
            " JOIN inbox_file f USING (sha256) WHERE l.status = 'waiting_calendar' AND l.report_date < %s",
            (self.cutoff,)).fetchall()
        with self.con.transaction():
            for sha, path, arch, src, rd, name, size, mtime in rows:
                self._status(Offer(sha, path, name, size, mtime, False), TOO_OLD, None, source=src,
                             report_date=rd, archive_path=arch)

    # ---- processing

    def run(self, offers: list[Offer]) -> None:
        """Match every offer (unhashed), remember the new files it declines or dates too old (``inbox_unread``), then
        hash and process the rest newest report date first, each file its own transaction, reads started a window
        ahead in parallel."""
        offers = list({o.path: o for o in reversed(offers)}.values())[::-1]  # one offer per path, first wins
        if not offers:
            return
        hits = self._match(offers)
        work: list[tuple[Offer, Any]] = []
        for o in offers:
            hit = hits.get(o.path)
            if not o.sha256:  # a new file: what will not be read is not hashed either
                ok, _, why = self._usable(hit)
                if hit is None or (ok and not why and self.too_old(hit["report_date"])):
                    with self.con.transaction():
                        self._unread(o, "ignored" if hit is None else TOO_OLD, hit)
                    continue
            work.append((o, hit))
        dated = [w for w in work if self._usable(w[1])[0] and not self._usable(w[1])[2]]
        dated.sort(key=lambda w: w[1]["report_date"], reverse=True)  # newest first: the latest positions land first
        work = dated + [w for w in work if not any(w is d for d in dated)]
        self._index_for(h for _, h in work)
        t0 = time.perf_counter()
        total, done = len(work), 0
        self.progress(0, total, "")
        window = max(4, 2 * self.workers)
        self._ex = ThreadPoolExecutor(self.workers, thread_name_prefix="desk-inbox-read")
        try:
            for i in range(0, total, window):
                batch = []
                for o, hit in work[i:i + window]:
                    if not o.sha256:
                        try:
                            o = replace(o, sha256=_sha256(o.path))
                        except OSError:  # gone, or locked by another program: the next scan tries again
                            self.res.skipped["unreadable"] += 1
                            _last_seen.pop(o.path, None)
                            done += 1
                            continue
                    self._prefetch(o, hit)
                    batch.append((o, hit))
                for o, hit in batch:
                    with self.con.transaction():
                        self.process(o, hit)
                    done += 1
                    self.progress(done, total, o.name)
        finally:
            for f in self._reads.values():
                f.cancel()
            self._reads.clear()
            self._ex.shutdown(wait=False, cancel_futures=True)  # a read still running ends with its worker
            self._ex = None
            self.res.timings["process_s"] = round(time.perf_counter() - t0, 2)

    @property
    def workers(self) -> int:
        pool = runner._pool
        return pool.size if pool is not None else 1

    def _usable(self, hit: Any) -> tuple[bool, dict | None, str | None]:
        """(a hit without an adapter error, its source's declaration, why it is unusable or None)."""
        ok = isinstance(hit, dict) and "_error" not in hit
        decl = self.h.source(hit.get("source")) if ok else None
        return ok, decl, check_hit(decl, hit) if ok else None

    def _match(self, offers: list[Offer]) -> dict[str, Any]:
        """``match`` for every offer: the workers warmed first, then the batches on all of them at once; the
        files of a call that timed out or crashed once more, in smaller calls with more time (R22)."""
        self._ensure_index([])
        t0 = time.perf_counter()
        paths = [o.path for o in offers]
        self.progress(0, len(paths), "запуск адаптера")
        warm = runner.call_warm(self.h, self.index, max(self.settings.adapter_read_timeout_s,
                                                        self.settings.adapter_match_timeout_s))
        self.res.match.update({f"warm_{k}": n for k, n in warm.items() if k != "ok"})
        timeout = self.settings.adapter_match_timeout_s
        hits = self._match_all(paths, MATCH_BATCH, timeout, "сопоставление файлов")
        again = [p for p in paths if _transient(hits.get(p))]
        if again:
            self.res.match["retried"] += len(again)
            log.warning("inbox: match timed out or crashed for %d files: matched again", len(again))
            # a timeout: fewer files per call and more time; a crash: one file per call, so it takes only itself
            slow = [p for p in again if hits[p]["_error"].get("type") == "timeout"]
            crashed = [p for p in again if p not in set(slow)]
            budget = max(timeout * MATCH_RETRY_FACTOR, MATCH_RETRY_MIN_S)
            second = {**self._match_all(slow, MATCH_RETRY_CHUNK, budget, "повторное сопоставление"),
                      **self._match_all(crashed, 1, budget, "повторное сопоставление")}
            self.res.match.update(recovered=sum(1 for p in again if not _transient(second.get(p))))
            self.res.match = +self.res.match  # no zero counts
            hits.update(second)
        self.res.timings["match_s"] = round(time.perf_counter() - t0, 2)
        return hits

    def _match_all(self, paths: list[str], chunk: int, timeout: float, label: str) -> dict[str, Any]:
        """The calls of ``chunk`` files each, on all the workers at once."""
        hits: dict[str, Any] = {}
        if not paths:
            return hits
        batches = [paths[i:i + chunk] for i in range(0, len(paths), chunk)]
        done = 0
        with ThreadPoolExecutor(min(self.workers, len(batches)) or 1, thread_name_prefix="desk-inbox-match") as ex:
            futs = {ex.submit(runner.call_match, self.h, b, self.index, timeout, chunk): len(b) for b in batches}
            for f in as_completed(futs):
                hits.update(f.result())
                done += futs[f]
                self.progress(done, len(paths), label)
        return hits

    def _prefetch(self, o: Offer, hit: Any) -> None:
        """Start the read ``process`` will ask for, when it will (the same checks, read-only)."""
        ok, decl, why = self._usable(hit)
        if not ok or why or self.too_old(hit["report_date"]) or self._ex is None:
            return
        if o.sighting and self.con.execute("SELECT 1 FROM inbox_file_status WHERE sha256 = %s AND source IS NOT NULL"
                                           " LIMIT 1", (o.sha256,)).fetchone():
            return  # known content: a copy, never read
        try:
            self.valid_date(decl, hit["report_date"])  # type: ignore[arg-type]
        except CalendarUnavailable:
            return
        timeout = self.settings.adapter_read_timeout_s
        if self._ref is None and not self._books_done and self._needs_books():
            self._ref = self._ex.submit(runner.call_reference, self.h, self.index, timeout)
        self._reads[o.path] = self._ex.submit(runner.call_read, self.h, o.path, hit, self.index, timeout)

    def _ensure_index(self, dates: list[str]) -> None:
        """The instruments index holding the newest static snapshot and those covering ``dates``: written only when
        the one on disk lacks one of them (its snapshots are in ``instruments.ids`` next to it). A rebuild keeps the
        snapshots it held that cover dates inside the lookback, so the next scans find what they need (R22)."""
        refs = static_snapshots(self.cache)
        want = {r.id for r in [covering(refs, None)] + [covering(refs, d) for d in dates] if r}
        ids = self.index.with_suffix(".ids")
        have: set[str] = set()
        try:
            if self.index.is_file():
                have = set(json.loads(ids.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            have = set()
        if want <= have and (want or self.index.is_file()):
            return
        floor = covering(refs, self.cutoff) if self.cutoff else None
        keep = {r.id for r in refs if r.id in have and (floor is None or r.business_date >= floor.business_date)}
        chosen = index_refs(self.cache, dates, keep)
        build_index(self.cache, self.index, dates, keep)
        ids.write_text(json.dumps(sorted(r.id for r in chosen)), encoding="utf-8")
        self.res.timings["index_builds"] = self.res.timings.get("index_builds", 0) + 1

    def _index_for(self, hits: Iterable[Any]) -> None:
        """The instruments index covering the report dates of these hits too (lookups on older business dates)."""
        dates = {h["report_date"] for h in hits if isinstance(h, dict) and isinstance(h.get("report_date"), str)
                 and not self.too_old(h["report_date"])}
        if dates - self._indexed:
            self._indexed |= dates
            self._ensure_index(sorted(self._indexed))

    def _unread(self, o: Offer, status: str, hit: Any = None) -> None:
        src, rd = (hit.get("source"), hit.get("report_date")) if isinstance(hit, dict) else (None, None)
        self.con.execute(
            "INSERT INTO inbox_unread (path, size_bytes, mtime, status, adapter_fingerprint, source, report_date)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (path, size_bytes, mtime) DO UPDATE SET"
            " status = EXCLUDED.status, adapter_fingerprint = EXCLUDED.adapter_fingerprint,"
            " source = EXCLUDED.source, report_date = EXCLUDED.report_date, at = clock_timestamp()",
            (o.path, o.size, o.mtime, status, self.h.fingerprint, src, rd))
        self.res.files[status] += 1

    def process(self, o: Offer, hit: Any) -> None:
        con = self.con
        con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT DO NOTHING", (o.sha256, o.path, o.name, o.size, o.mtime))
        ok = isinstance(hit, dict) and "_error" not in hit
        decl = self.h.source(hit.get("source")) if ok else None
        why = check_hit(decl, hit) if ok else None
        if o.sighting:
            known = con.execute("SELECT source, report_date FROM inbox_file_status WHERE sha256 = %s"
                                " AND source IS NOT NULL ORDER BY status_id DESC LIMIT 1", (o.sha256,)).fetchone()
            kind, src, rd, vd = "new", None, None, None
            if known:
                usable = ok and why is None
                same = not usable or (hit["source"], date.fromisoformat(hit["report_date"])) == tuple(known)
                kind = "duplicate" if same else "stale_copy"
                if not same:
                    src, rd = hit["source"], hit["report_date"]
                    try:
                        vd = self.valid_date(decl, rd)  # type: ignore[arg-type]
                    except CalendarUnavailable:
                        vd = None
            con.execute("INSERT INTO inbox_sighting (path, size_bytes, mtime, sha256, kind, source, report_date,"
                        " valid_date) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (o.path, o.size, o.mtime, o.sha256, kind, src, rd, vd))
            if known:
                self.res.copies[kind] += 1
                if kind == "stale_copy":
                    self._event("input.stale", source=src, dataset=self._dataset(decl), valid_date=vd,
                                sha256=o.sha256, counts={"report_date": rd, "content_report_date": known[1].isoformat()})
                return

        if hit is None:
            self._status(o, "ignored")
            return
        if not isinstance(hit, dict):
            self._status(o, "match_failed", error_code="bad_hit")
            return
        if not ok:
            e = hit["_error"] if isinstance(hit["_error"], dict) else {}
            self._status(o, "match_failed", error_type=e.get("type"), error_frames=e.get("frames"))
            return
        if why:
            self._status(o, "match_failed", error_code="bad_hit")
            return
        assert decl is not None
        if self.too_old(hit["report_date"]):  # behind the lookback: ledgered once, never archived or read
            self._status(o, TOO_OLD, decl, source=hit["source"], report_date=hit["report_date"])
            return
        cols = {"source": hit["source"], "report_date": hit["report_date"], "report_ts": parse_ts(hit.get("report_ts")),
                "seq": hit.get("seq"), "date_from": hit.get("date_from"), "archive_path": self._archive(o)}
        try:
            vd = self.valid_date(decl, hit["report_date"])
        except CalendarUnavailable:
            last = con.execute("SELECT status, adapter_fingerprint FROM inbox_file_status WHERE sha256 = %s"
                               " ORDER BY status_id DESC LIMIT 1", (o.sha256,)).fetchone()
            if last == ("waiting_calendar", self.h.fingerprint):  # still waiting: no new status, no event
                self.res.files["waiting_calendar"] += 1
            else:
                self._status(o, "waiting_calendar", decl, **cols)
            return
        cols["valid_date"] = vd
        try:
            ahead = self._reads.pop(o.path, None)
            raw = ahead.result() if ahead is not None else \
                runner.call_read(self.h, o.path, hit, self.index, self.settings.adapter_read_timeout_s)
        except AdapterFailure as f:
            log.warning("inbox %s: read %s (%s)", o.sha256[:12], f.kind, f.type)
            self._status(o, f.kind, decl, error_type=f.type, error_frames=f.frames, **cols)
            return
        c = check_read(decl, hit, raw, books=self.books, contract=self.h.describe["contract"])
        if self.expected is not None:
            source, day, books = self.expected
            valid = (c.status == "accepted" and hit["source"] == source and hit["report_date"] == day and vd.isoformat() == day
                     and {u["scope"].get("book_id") for u in c.units} == books
                     and all(u["dataset"] == "trade_terms" and set(u["scope"]) == {"book_id"}
                             and u["report_date"] == day for u in c.units))
            if not valid:
                # A managed producer may update only the requested stock scopes; reject before any unit writes.
                self._status(o, "rejected", decl, error_code="acquisition_scope_mismatch", **cols)
                return
        gen = c.meta.get("generated_at")
        issues = list(c.issues)
        if report_date_suspicious(hit["report_date"], decl["time_basis"], gen):
            issues.append(_issue("warning", "report_date_suspicious", None,
                                 "meta.generated_at contradicts the report date (08 §6.5)"))
        if decl["time_basis"] == "start_of_day" and con.execute(
                "SELECT 1 FROM inbox_file_status WHERE source = %s AND valid_date = %s AND report_date <> %s"
                " AND status = ANY(%s) LIMIT 1", (hit["source"], vd, hit["report_date"], list(READ_DONE))).fetchone():
            issues.append(_issue("info", "same_valid_date", None, "another report date maps to this business date"))
        key = revision_key(hit["report_date"], hit.get("report_ts"), hit.get("seq"), gen, o.mtime.timestamp())
        # a unit of a multi-date file (_report_date, 08 §6.1) has its own business date; its revision key stays the
        # file's, so a later file (a restated history) outranks an earlier one for the same unit
        outcomes = [self._unit(o, decl, hit, cols, parse_ts(gen), key, vd, u, issues) for u in c.units]
        states = Counter(st for st, *_ in outcomes)
        if c.file_error:
            status = "rejected"
        elif states["rejected"]:
            status = "partially_accepted" if states["accepted"] + states["superseded"] else "rejected"
        else:
            status = "superseded" if states["superseded"] and not states["accepted"] else "accepted"
        sid = self._status(o, status, decl, generated_at=parse_ts(gen), error_code=c.file_error, counts={
            "units": dict(states), "rows": sum(len(u["rows"]) for u in c.units),
            "revisions": sum(1 for _, _, n, _ in outcomes if n),
            "issues": dict(Counter(i["severity"] for i in issues))}, **cols)
        unit_ids = []
        for u, (st, set_id, new, uvd) in zip(c.units, outcomes):
            self.res.units[st] += 1
            positions = u["dataset"] == "positions"
            unit_ids.append(con.execute(
                "INSERT INTO inbox_unit (status_id, sha256, dataset, valid_date, report_date, scope, status, unit_hash,"
                " row_count, set_id, dataset_set_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " RETURNING unit_id",
                (sid, o.sha256, u["dataset"], uvd, u["report_date"], Jsonb(u["scope"]), st, unit_hash(u["rows"]),
                 len(u["rows"]), set_id if positions else None, None if positions else set_id)).fetchone()[0])
        for i in issues:
            scope = {k: v for k, v in (i["scope"] or {}).items() if not k.startswith("_")}
            uid = next((uid for u, uid in zip(c.units, unit_ids) if scope and u["scope"] == scope), None)
            con.execute("INSERT INTO inbox_issue (status_id, sha256, unit_id, severity, code, src, scope, message,"
                        " count, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (sid, o.sha256, uid, i["severity"], i["code"], i["src"],
                         Jsonb(i["scope"]) if i["scope"] else None, i["message"], i["count"], i["origin"]))

    def _unit(self, o: Offer, decl: dict, hit: dict, cols: dict, gen: datetime | None, key: tuple, vd: date,
              u: dict, issues: list[dict]) -> tuple[str, int | None, bool, date | None]:
        """(status, set_id, new revision, business date) of one unit (08 §6.6). positions units are position sets
        (per book_id), every other dataset with a schema a ``dataset_set`` (per scope unit)."""
        scope = u["scope"]
        uvd: date | None = vd
        if u["report_date"] != hit["report_date"]:  # a unit of a multi-date file
            try:
                uvd = self.valid_date(decl, u["report_date"])
            except CalendarUnavailable:
                if u["status"] != "rejected":
                    issues.append(_issue("error", "waiting_calendar", scope, "no calendar covers the row's report date"))
                return "rejected", None, False, None
        if u["status"] == "rejected":
            return "rejected", None, False, uvd
        positions = u["dataset"] == "positions"
        if positions and "book_id" not in scope:
            # shortcut: positions are stored per book (position_set); a positions scope without book_id is refused
            issues.append(_issue("error", "scope_not_supported", scope, "positions are stored per book_id"))
            return "rejected", None, False, uvd
        assert uvd is not None
        uh, src, prio = unit_hash(u["rows"]), hit["source"], decl.get("priority")
        sets = self.con.execute(
            "SELECT set_id, source, priority, report_date, report_ts, seq, generated_at, file_mtime, unit_hash"
            f" FROM position_set WHERE book_id = %s AND valid_date = %s ORDER BY {store.CURRENT_SET}",
            (scope["book_id"], uvd)).fetchall() if positions else \
            store.dataset_unit_sets(self.con, u["dataset"], scope, uvd)
        others = [s[2] for s in sets if s[1] != src]
        if any(prio is None or p is None or p == prio for p in others):
            issues.append(_issue("error", "scope_conflict", scope, "another source has this unit (08 §6.6)"))
            return "rejected", None, False, uvd
        if any(p > prio for p in others):
            return "superseded", None, False, uvd
        own = [s for s in sets if s[1] == src]
        if own:
            if key < _key(*own[0][3:8]):
                return "superseded", None, False, uvd
            if own[0][8] == uh:
                return "accepted", own[0][0], False, uvd
        common = {"valid_date": uvd, "rows": u["rows"], "source": src, "priority": prio,
                  "report_date": date.fromisoformat(hit["report_date"]), "report_ts": cols["report_ts"],
                  "seq": hit.get("seq"), "generated_at": gen, "file_mtime": o.mtime, "file_name": o.name,
                  "sha256": o.sha256, "adapter_fingerprint": self.h.fingerprint, "unit_hash": uh}
        if positions:
            set_id = store.store_inbox_set(self.con, book_id=scope["book_id"], **common)  # type: ignore[arg-type]
        else:
            set_id = store.store_dataset_set(self.con, dataset=u["dataset"], scope=scope, **common)  # type: ignore[arg-type]
        self.res.revisions += 1
        return "accepted", set_id, True, uvd

    def _archive(self, o: Offer) -> str:
        """``<archive>/<sha256>/<name>``: under its hash, with the name the adapter matches by (08 §13)."""
        dest = self.archive / o.sha256 / o.name
        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            shutil.copyfile(o.path, tmp)
            os.replace(tmp, dest)
        return str(dest)

    @staticmethod
    def _dataset(decl: dict | None) -> str | None:
        return decl.get("dataset") if decl else None

    def _status(self, o: Offer, status: str, decl: dict | None = None, *, counts: dict | None = None,
                error_frames: list | None = None, **cols: Any) -> int:
        sid = self.con.execute(
            "INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path, archive_path, source,"
            " report_date, report_ts, seq, generated_at, date_from, valid_date, error_code, error_type, error_frames)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING status_id",
            (o.sha256, status, self.h.fingerprint, o.path, cols.get("archive_path"), cols.get("source"),
             cols.get("report_date"), cols.get("report_ts"), cols.get("seq"), cols.get("generated_at"),
             cols.get("date_from"), cols.get("valid_date"), cols.get("error_code"), cols.get("error_type"),
             Jsonb(error_frames) if error_frames else None)).fetchone()[0]
        self.res.files[status] += 1
        if status in QUIET:
            log.debug("inbox %s %s %s", o.sha256[:12], cols.get("source") or "-", status)
            return sid
        self._event(EVENTS.get(status, f"input.{status}"), source=cols.get("source"), dataset=self._dataset(decl),
                    valid_date=cols.get("valid_date"), sha256=o.sha256, counts=counts)
        log.info("inbox %s %s %s %s", o.sha256[:12], cols.get("source") or "-", status, counts or "")
        return sid

    def _event(self, type_: str, *, source: str | None = None, dataset: str | None = None,
               valid_date: date | None = None, sha256: str | None = None, counts: dict | None = None) -> None:
        self.con.execute("INSERT INTO event (type, source, dataset, valid_date, sha256, counts)"
                         " VALUES (%s, %s, %s, %s, %s, %s)",
                         (type_, source, dataset, valid_date, sha256, Jsonb(counts) if counts else None))


def _session(con: psycopg.Connection, settings: Settings, now: datetime | None,
             offers: Callable[[_Scan], list[Offer]], *, lookback: bool = False,
             progress: Progress | None = None, since: date | None = None,
             expected: tuple[str, str, frozenset[str]] | None = None,
             handle: AdapterHandle | None = None) -> ScanResult:
    h = handle if handle is not None else adapter(settings)
    res = ScanResult(adapter_line(h))
    if not h.ok:
        log.warning("inbox: adapter error %s: ingestion is off", h.error)
        return res
    if not con.execute("SELECT pg_try_advisory_lock(%s)", (SCAN_LOCK,)).fetchone()[0]:
        res.busy = True
        return res
    try:
        now = now or datetime.now(UTC)
        cutoff = since if since is not None else lookback_cutoff(settings, now.astimezone(MSK).date()) if lookback \
            else None
        s = _Scan(con, settings, h, res, now, cutoff, progress, expected)
        with runner.pooled(read_workers()):  # workers start on the first call: an idle scan starts none
            s.run(offers(s))
            s.refresh_books()
    finally:
        con.execute("SELECT pg_advisory_unlock(%s)", (SCAN_LOCK,))
    return res


def scan(con: psycopg.Connection, settings: Settings, *, now: datetime | None = None,
         progress: Progress | None = None, since: date | None = None) -> ScanResult:
    """New stable files of the folders, files to re-offer after a fingerprint change, and those waiting for a
    calendar; within the lookback only (``ignored_too_old`` files again once the lookback covers them). ``since``
    (``desk backfill``, R23): the lookback starts on this date instead, so the older reports are read too."""
    def offers(s: _Scan) -> list[Offer]:
        s.retire_too_old()
        again = ("waiting_calendar", TOO_OLD) if s.calendars_changed() else (TOO_OLD,)
        return s.new_files() + s.known(lambda st, fp: (st in REOFFER and fp != s.h.fingerprint) or st in again,
                                       fresh=True, statuses=(*REOFFER, *again)) + s.transient()
    res = _session(con, settings, now, offers, lookback=True, progress=progress, since=since)
    global last_scan
    last_scan = {"at": datetime.now(UTC).isoformat(), "files": dict(res.files), "skipped": dict(res.skipped),
                 "copies": dict(res.copies), "match": dict(res.match), "timings": dict(res.timings)}
    return res


last_scan: dict[str, Any] | None = None
"""The counts of this process's last ``scan`` (``reconcile`` shows them: the skipped files are not ledgered)."""


def ingest_produced(con: psycopg.Connection, settings: Settings, path: Path, *, source: str,
                    day: date, books: frozenset[str], handle: AdapterHandle) -> ScanResult:
    """One atomically completed managed acquisition file, through the existing read/validation/store pipeline.

    Unlike a folder scan this offers no unrelated unread/reoffer files. Its source/date/book stock scope is
    checked before writes. The caller owns publication/containment and the ordinary cycle lock.
    """
    st = path.stat()
    offer = Offer("", str(path), path.name, st.st_size, datetime.fromtimestamp(st.st_mtime, UTC), True)
    return _session(con, settings, None, lambda _: [offer], expected=(source, day.isoformat(), books), handle=handle)


def retry(con: psycopg.Connection, settings: Settings, statuses: tuple[str, ...] = (*REOFFER, "waiting_calendar"),
          *, now: datetime | None = None) -> ScanResult:
    """``desk inbox retry``: files in these statuses again, whatever the fingerprint."""
    return _session(con, settings, now, lambda s: s.known(lambda st, fp: st in statuses)
                    + s.unread([x for x in statuses if x in UNREAD]))


def replay(con: psycopg.Connection, settings: Settings, source: str, date_from: date, date_to: date, *,
           now: datetime | None = None) -> ScanResult:
    """``desk inbox replay``: read the accepted files of a source with business dates in the range again; a new
    revision only where a unit's canonical rows changed (08 §6.6)."""
    return _session(con, settings, now, lambda s: s.known(lambda st, fp: st in READ_DONE, source,
                                                          (date_from, date_to)))


def status(con: psycopg.Connection, settings: Settings, on: date | None = None) -> dict:
    """``desk inbox status``: per source and business date the current status of the files, their units and
    issues (of their latest attempt) and the copies; per active book the valid date of its positions for ``on``
    (default: today, Moscow) and its age; the bonds of those positions without an instrument (``unmapped``: counts
    by reason and the ISINs, D6). Codes, counts and ISINs only."""
    latest = "SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC"
    groups: dict[tuple, dict] = {}

    def g(src: str | None, vd: date | None) -> dict:
        return groups.setdefault((src or "-", vd or date.min), {
            "source": src or "-", "valid_date": vd.isoformat() if vd else None, "files": Counter(),
            "copies": Counter(), "units": Counter(), "issues": Counter()})

    for src, vd, st, n in con.execute(f"SELECT source, valid_date, status, count(*) FROM ({latest}) l GROUP BY 1, 2, 3"):
        g(src, vd)["files"][st] += n
    for src, vd, st, n in con.execute(f"SELECT l.source, l.valid_date, u.status, count(*) FROM ({latest}) l"
                                      " JOIN inbox_unit u USING (status_id) GROUP BY 1, 2, 3"):
        g(src, vd)["units"][st] += n
    for src, vd, sev, code, n in con.execute(
            f"SELECT l.source, l.valid_date, i.severity, i.code, sum(i.count) FROM ({latest}) l"
            " JOIN inbox_issue i USING (status_id) GROUP BY 1, 2, 3, 4"):
        g(src, vd)["issues"][(sev, code)] += int(n)
    for src, st, n in con.execute("SELECT source, status, count(*) FROM inbox_unread GROUP BY 1, 2"):
        g(src, None)["files"][st] += n  # never read: no business date
    for src, vd, kind, n in con.execute("SELECT source, valid_date, kind, count(*) FROM inbox_sighting"
                                        " WHERE kind <> 'new' GROUP BY 1, 2, 3"):
        g(src, vd)["copies"][kind] += n
    out_groups = [{**x, "files": dict(x["files"]), "copies": dict(x["copies"]), "units": dict(x["units"]),
                   "issues": [{"severity": s, "code": c, "count": n} for (s, c), n in sorted(x["issues"].items())]}
                  for k, x in sorted(groups.items()) if on is None or k[1] == on]

    today = datetime.now(UTC).astimezone(MSK).date()
    day, expect = on or today, on
    if expect is None:
        try:
            expect = business_calendar(settings.snapshot_cache, today).prev_business_day(today)
        except CalendarUnavailable:
            expect = None
    inputs = store.positions_for(con, day)
    books = []
    for b in store.current_books(con, day):
        if not b.get("active", True):
            continue
        vd = inputs.valid_dates.get(b["book_id"])
        books.append({"book_id": b["book_id"], "valid_date": vd.isoformat() if vd else None,
                      "age_days": (day - vd).days if vd else None,
                      "source": inputs.sources.get(b["book_id"], {}).get("source"),
                      "stale": None if expect is None else (vd is None or vd < expect)})
    return {"adapter": adapter_line(adapter(settings)), "positions_for": day.isoformat(),
            "expected_valid_date": expect.isoformat() if expect else None, "groups": out_groups, "books": books,
            "unmapped": unmapped(settings, inputs, day)}


def unmapped(settings: Settings, inputs: store.PositionInputs, day: date) -> dict[str, Any]:
    """The bonds of ``inputs`` without an instrument in the cached static snapshot of ``day``, by reason, with their
    ISINs (``static_index.unmapped_bonds``, D6); ``error`` when the static snapshot cannot be read."""
    try:
        return unmapped_bonds(settings.snapshot_cache, [(secid, inputs.extra.get(pid))
                                                        for pid, _, secid, _ in inputs.rows], day)
    except (OSError, ValueError) as e:
        return {"count": None, "by_reason": {}, "isins": [], "error": type(e).__name__}


def unmapped_text(u: dict[str, Any], limit: int | None = None) -> str:
    """One line: ``unmapped bonds 3 (not_in_static 2, not_on_moex 1): RU000A0JWRE5 not_in_static, ...``."""
    if u.get("error"):
        return f"unmapped bonds: unknown ({u['error']})"
    if not u.get("count"):
        return "unmapped bonds: 0" if u.get("static_snapshot") else "unmapped bonds: no cached static snapshot"
    isins = u["isins"][:limit] if limit else u["isins"]
    more = len(u["isins"]) - len(isins)
    return (f"unmapped bonds {u['count']} (" + ", ".join(f"{k} {v}" for k, v in sorted(u["by_reason"].items()))
            + ")" + (": " + ", ".join(f"{i['isin']} {i['reason']}" for i in isins) if isins else "")
            + (f" and {more} more" if more else ""))


def status_text(st: dict) -> str:
    def c(x: dict) -> str:
        return ", ".join(f"{k} {v}" for k, v in sorted(x.items())) or "-"
    lines = [f"adapter: {st['adapter']}"]
    for gr in st["groups"]:
        lines.append(f"{gr['source']} {gr['valid_date'] or '-'}: files {c(gr['files'])}; units {c(gr['units'])}; "
                     f"copies {c(gr['copies'])}")
        lines += [f"    {i['severity']} {i['code']} {i['count']}" for i in gr["issues"]]
    lines.append(f"books (positions for {st['positions_for']}, expected valid date "
                 f"{st['expected_valid_date'] or 'unknown: no calendar'}):")
    for b in st["books"]:
        flag = "" if b["stale"] is None else " STALE" if b["stale"] else " ok"
        lines.append(f"  {b['book_id']}: " + (f"valid {b['valid_date']}, {b['age_days']} days, {b['source']}"
                                             if b["valid_date"] else "no positions") + flag)
    if "unmapped" in st:
        lines.append(unmapped_text(st["unmapped"]))
        lines += [f"  {code}: {why}" for code, why in UNMAPPED_REASONS.items() if code in st["unmapped"]["by_reason"]]
    return "\n".join(lines)




# ---- reconcile (R22) ------------------------------------------------------------------------------------------

LOSS_REASONS = {
    "match_failed": "адаптер не смог сопоставить файл (ошибка или таймаут match)",
    "timed_out": "чтение файла не уложилось в таймаут",
    "failed": "чтение файла упало с ошибкой адаптера",
    "rejected": "файл отклонён целиком при проверке",
    "waiting_calendar": "нет календаря на дату отчёта (ждёт снимок справочника с хаба)",
    "ignored": "адаптер не узнал файл (не его формат)",
    "ignored_too_old": "дата отчёта старше окна загрузки (inbox_lookback_days)",
    "unit_rejected": "книга в файле отклонена (ошибки в строках, см. коды)",
    "unit_superseded": "книга пришла в другом, более новом отчёте или от источника с большим приоритетом",
    "scope_conflict": "та же книга и дата от другого источника без приоритетов",
    "book_closed_by_omission": "книги нет в отчёте, который покрывает все книги: позиции закрыты",
    "duplicate_key": "повтор ключа позиции в книге: книга отклонена",
    "unknown_book": "книги нет в справочнике книг адаптера (reference)",
    "missing_required": "в строках нет обязательных полей: книга отклонена",
    "not_in_run": "позиции загружены после расчёта даты: расчёт ещё не пересчитан",
    "rows_differ": "строк в расчёте не столько, сколько загружено",
}
"""What each loss code means, for the owner (the settings page and ``desk inbox reconcile``)."""


def reconcile(con: psycopg.Connection, settings: Settings, on: date | None = None) -> dict[str, Any]:
    """``desk inbox reconcile`` (R22): where each book's rows went, for the business date ``on`` (default: the date
    of the latest default run, else today, Moscow). Per book: the position set in force (valid date, source,
    report date, file), the rows of the report unit that stored it, the rows of the set, the rows of the date's
    default run (and how many of them were valued), and the units of the book rejected or superseded from the
    lookback on with their issue codes. Then over all files: the current statuses (the ledger and the files never
    read), the loss codes with counts and their meaning, and the last scan's counts of this process.
    Book ids, file names, codes and counts: the owner's own view; the diagnostics take only its totals."""
    today = datetime.now(UTC).astimezone(MSK).date()
    run = con.execute(f"SELECT run_id, business_date, position_set_ids FROM run WHERE run_id IN ({store.DEFAULT_RUNS})"
                      " AND business_date <= %s ORDER BY business_date DESC LIMIT 1", (on or today,)).fetchone()
    day = on or (run[1] if run else today)
    if run and run[1] != day:
        run = None
    since = lookback_cutoff(settings, today) or (day - timedelta(days=MTIME_SLACK_DAYS))
    latest = "(SELECT DISTINCT ON (sha256) * FROM inbox_file_status ORDER BY sha256, status_id DESC)"

    sets = {b: (sid, vd, src, rd, name) for sid, b, vd, src, rd, name in con.execute(
        "SELECT DISTINCT ON (book_id) set_id, book_id, valid_date, source, report_date, source_name"
        f" FROM position_set WHERE valid_date <= %s ORDER BY book_id, {store.CURRENT_SET}", (day,))}
    ids = [v[0] for v in sets.values()]
    accepted = dict(con.execute("SELECT set_id, count(*) FROM position_input WHERE set_id = ANY(%s) GROUP BY 1",
                                (ids,)).fetchall())
    in_report = dict(con.execute("SELECT DISTINCT ON (set_id) set_id, row_count FROM inbox_unit"
                                 " WHERE set_id = ANY(%s) ORDER BY set_id, unit_id", (ids,)).fetchall())
    in_run: dict[str, tuple[int, int]] = {}
    if run:
        in_run = {b: (n, v) for b, n, v in con.execute(
            "SELECT book_id, count(*), count(*) FILTER (WHERE quality IS NOT NULL) FROM run_position"
            " WHERE run_id = %s GROUP BY 1", (run[0],))}
    # what was lost counts from the valid date of the set in force on: an older report rejected and then
    # followed by an accepted one lost nothing
    def current(b: str | None, vd: date | None) -> bool:
        return vd is not None and (b not in sets or vd >= sets[b][1])

    lost_units: dict[str, Counter] = {}
    for b, st, vd, n, rows in con.execute(
            f"SELECT u.scope->>'book_id', u.status, u.valid_date, count(*), sum(u.row_count) FROM {latest} l"
            " JOIN inbox_unit u USING (status_id) WHERE u.status <> 'accepted' AND u.valid_date BETWEEN %s AND %s"
            " GROUP BY 1, 2, 3", (since, day)):
        if current(b, vd):
            c = lost_units.setdefault(b or "-", Counter())
            c[f"unit_{st}"] += n
            c[f"unit_{st}_rows"] += int(rows or 0)
    issues: dict[str, Counter] = {}
    for b, vd, code, n in con.execute(
            f"SELECT i.scope->>'book_id', l.valid_date, i.code, sum(i.count) FROM {latest} l"
            " JOIN inbox_issue i USING (status_id) WHERE i.severity <> 'info' AND l.valid_date BETWEEN %s AND %s"
            " GROUP BY 1, 2, 3", (since, day)):
        if current(b, vd):
            issues.setdefault(b or "-", Counter())[code] += int(n)

    books = []
    names = {b["book_id"]: b for b in store.current_books(con, day)}
    for b in sorted(set(sets) | {k for k, v in names.items() if v.get("active", True)}):
        sid, vd, src, rd, name = sets.get(b, (None, None, None, None, None))
        n_acc = accepted.get(sid, 0) if sid else None
        n_run, n_val = in_run.get(b, (0, 0) if sid in (run[2] or []) else (None, None)) if run else (None, None)
        lu, iss = lost_units.get(b, Counter()), issues.get(b, Counter())
        flags = []
        if sid is None:
            flags.append("no_positions")
        if run and sid is not None and sid not in (run[2] or []):
            flags.append("not_in_run")
        elif run and n_acc is not None and (n_run or 0) != n_acc:
            flags.append("rows_differ")
        if lu.get("unit_rejected"):
            flags.append("unit_rejected")
        books.append({
            "book_id": b, "name": (names.get(b) or {}).get("name"), "active": (names.get(b) or {}).get("active", True),
            "valid_date": vd.isoformat() if vd else None, "age_days": (day - vd).days if vd else None,
            "source": src, "report_date": rd.isoformat() if rd else None, "file": name,
            "rows_in_report": in_report.get(sid) if sid else None, "rows_accepted": n_acc,
            "rows_in_run": n_run, "rows_valued": n_val,
            "units_rejected": lu.get("unit_rejected", 0), "rows_rejected": lu.get("unit_rejected_rows", 0),
            "units_superseded": lu.get("unit_superseded", 0), "issues": dict(iss.most_common()), "flags": flags})

    files = Counter(dict(con.execute(f"SELECT status, count(*) FROM {latest} l GROUP BY 1").fetchall()))
    files.update(dict(con.execute("SELECT status, count(*) FROM inbox_unread GROUP BY 1").fetchall()))
    errors = [{"status": st, "error_type": t or c, "files": n} for st, c, t, n in con.execute(
        f"SELECT status, error_code, error_type, count(*) FROM {latest} l WHERE status IN"
        " ('match_failed', 'failed', 'timed_out', 'rejected') GROUP BY 1, 2, 3 ORDER BY 4 DESC")]
    losses = Counter({k: files[k] for k in ("match_failed", "timed_out", "failed", "rejected", "waiting_calendar",
                                            "ignored", "ignored_too_old") if files.get(k)})
    for bk in books:
        losses["unit_rejected"] += bk["units_rejected"]
        losses["unit_superseded"] += bk["units_superseded"]
        for code in ("scope_conflict", "book_closed_by_omission", "duplicate_key", "unknown_book",
                     "missing_required"):
            losses[code] += bk["issues"].get(code, 0)
        for f in ("not_in_run", "rows_differ"):
            losses[f] += f in bk["flags"]
    totals = {k: sum(bk[k] or 0 for bk in books) for k in ("rows_in_report", "rows_accepted", "rows_in_run",
                                                            "rows_valued", "rows_rejected")}
    return {"date": day.isoformat(), "run_id": run[0] if run else None, "since": since.isoformat(),
            "books": books, "totals": {**totals, "books": len(books),
                                       "books_flagged": sum(1 for bk in books if bk["flags"])},
            "files": dict(sorted(files.items())), "file_errors": errors,
            "losses": [{"code": k, "count": n, "what": LOSS_REASONS.get(k, k)}
                       for k, n in losses.most_common() if n],
            "last_scan": last_scan}


def reconcile_text(r: dict[str, Any]) -> str:
    """``desk inbox reconcile`` as text: a table of books, then the files and the losses."""
    def n(x: Any) -> str:
        return "-" if x is None else str(x)
    lines = [f"positions for {r['date']}" + (f", default run {r['run_id']}" if r["run_id"] else ", no default run")
             + f" (losses counted from {r['since']})",
             (f"{'book':<16} {'valid':<10} {'age':>4} {'report':>7} {'accepted':>8} {'in run':>7} {'valued':>7}"
              f" {'rejected':>8}  flags / issues")]
    for b in r["books"]:
        extra = ", ".join(b["flags"] + [f"{k} {v}" for k, v in b["issues"].items()])
        lines.append(f"{b['book_id'][:16]:<16} {n(b['valid_date']):<10} {n(b['age_days']):>4}"
                     f" {n(b['rows_in_report']):>7} {n(b['rows_accepted']):>8} {n(b['rows_in_run']):>7}"
                     f" {n(b['rows_valued']):>7} {b['units_rejected'] or '':>8}  {extra}")
    t = r["totals"]
    lines.append(f"{'total':<16} {'':<10} {'':>4} {t['rows_in_report']:>7} {t['rows_accepted']:>8}"
                 f" {t['rows_in_run']:>7} {t['rows_valued']:>7}  books {t['books']}, flagged {t['books_flagged']}")
    lines.append("files: " + (", ".join(f"{k} {v}" for k, v in r["files"].items()) or "none"))
    lines += [f"  {e['status']} {e['error_type'] or '-'}: {e['files']}" for e in r["file_errors"]]
    if r["losses"]:
        lines.append("losses:")
        lines += [f"  {x['code']} {x['count']}: {x['what']}" for x in r["losses"]]
    if r.get("last_scan"):
        ls = r["last_scan"]
        lines.append(f"last scan {ls['at'][:19]}: skipped " + (", ".join(f"{k} {v}" for k, v in ls["skipped"].items())
                                                             or "none")
                     + ("; match " + ", ".join(f"{k} {v}" for k, v in ls["match"].items()) if ls["match"] else ""))
    return "\n".join(lines)
