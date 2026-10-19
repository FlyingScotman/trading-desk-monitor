"""The inbox scanner (08 §6, §13): report files of ``DESK_INBOX_DIRS`` through the private adapter into the desk
store. ``scan`` takes in new files, ``retry`` and ``replay`` offer known ones again, ``status`` sums the ledger
up for ``desk inbox status``. Unlike the rest of ``desk.inbox`` this module uses the store (psycopg).

One scan at a time: a Postgres advisory lock; a second scan finds it taken and does nothing. The adapter is
loaded once per process; when it fails to load, ingestion is off with the reason and nothing is touched.

A file of the folders (temp, hidden and oversized files skipped) is **offered** once it is stable: its size and
modification time unchanged since the previous scan of this process (the watcher), or its modification time
older than ``DESK_INBOX_POLL_SECONDS`` (so a one-shot ``desk inbox scan`` takes files copied earlier). A path
with the size and time already taken in is not hashed again. Then, per file:

* known content under a new path: ``duplicate`` (same source and report date: nothing) or ``stale_copy`` (another
  report date, event ``input.stale``), recorded on the sighting; the content's own status is unchanged;
* ``match`` (one batch per scan): None → ``ignored``; an exception or a bad hit → ``match_failed``;
* a matched file is archived under ``<archive>/<sha256>/<name>`` (the name kept: adapters match by name);
* the business date (``timesem.valid_date`` on the calendar of the cached static snapshots); no calendar →
  ``waiting_calendar``, offered again by every scan;
* ``read``: raises or times out → ``failed`` / ``timed_out``; else ``validate.check_read``, then per unit
  (book, valid date): rejected, or ``superseded`` (sorts below the current revision by report key, or another
  source of higher priority has it), or a ``scope_conflict`` (another source, no priorities), or accepted, a
  new position set unless its ``unit_hash`` equals the current revision's;
* the file: ``accepted``, ``partially_accepted``, ``rejected`` or ``superseded`` (all its units).

Every status change is a new ``inbox_file_status`` row and an event. When the adapter fingerprint differs from
the one that set it, a file in ``ignored``, ``match_failed``, ``failed``, ``timed_out`` or ``rejected`` is offered
again (from the archive when its path is gone). Logs and results carry codes, counts and row numbers, never
row values or exception messages.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .. import store
from ..settings import Settings
from . import runner
from .conformance import list_files
from .loader import AdapterHandle, load_adapter
from .spi import DATASETS_WITH_BOOK, AdapterFailure, source_datasets
from .static_index import CalendarUnavailable, build_index, business_calendar, covering, static_snapshots
from .timesem import MSK, parse_ts, report_date_suspicious, revision_key, valid_date
from .validate import check_hit, check_read, check_reference, unit_hash

log = logging.getLogger(__name__)

SCAN_LOCK = 0x6465736C  # one scan at a time; the store migrator holds 0x6465736B
REOFFER = ("ignored", "match_failed", "failed", "timed_out", "rejected")
READ_DONE = ("accepted", "partially_accepted", "superseded")
MATCH_BATCH = 50
EVENTS = {"stale_copy": "input.stale", "timed_out": "input.failed"}

_handles: dict[tuple, AdapterHandle] = {}
_last_seen: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) at this process's previous scan


def adapter(settings: Settings) -> AdapterHandle:
    """The adapter of the settings, loaded once per process (08 §2); never raises."""
    cfg = settings.private_adapter_config
    key = (settings.private_adapter, cfg, settings.private_adapter_sha256)
    if key not in _handles:
        _handles[key] = load_adapter(settings.private_adapter, str(cfg) if cfg else None,
                                     settings.private_adapter_sha256)
    return _handles[key]


def adapter_line(h: AdapterHandle) -> str:
    """``ok <name> <version> <fingerprint[:12]>`` or ``error <reason>`` (``/health/ready``, ``desk inbox``)."""
    return f"ok {h.name} {h.version} {h.fingerprint[:12]}" if h.ok else f"error {h.error}"


@dataclass
class ScanResult:
    adapter: str
    busy: bool = False
    files: Counter = field(default_factory=Counter)     # status of each file processed
    copies: Counter = field(default_factory=Counter)    # duplicate, stale_copy
    skipped: Counter = field(default_factory=Counter)   # waiting (not stable yet), too_large, gone
    units: Counter = field(default_factory=Counter)
    revisions: int = 0
    books_revised: int = 0
    notes: Counter = field(default_factory=Counter)     # reference() problems by code

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
        return "\n".join(lines + ([f"notes: {c(self.notes)}"] if self.notes else []))


@dataclass(frozen=True)
class Offer:
    sha256: str
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


def _key(report_date: date, report_ts: datetime | None, seq: int | None, generated_at: datetime | None,
         mtime: datetime | None) -> tuple:
    return revision_key(report_date, report_ts.isoformat() if report_ts else None, seq,
                        generated_at.isoformat() if generated_at else None, mtime.timestamp() if mtime else None)


class _Scan:
    def __init__(self, con: psycopg.Connection, settings: Settings, h: AdapterHandle, res: ScanResult,
                 now: datetime) -> None:
        self.con, self.settings, self.h, self.res, self.now = con, settings, h, res, now
        self.cache = settings.snapshot_cache
        self.archive = Path(settings.inbox_archive)
        self.archive.mkdir(parents=True, exist_ok=True)
        self.index = self.archive / "instruments.json"
        build_index(self.cache, self.index)
        self.calendars: dict[str, Any] = {}
        self.books = self._books()

    # ---- books and calendar

    def _books(self) -> list[dict] | None:
        """``reference()`` once per scan: a new ``book_rev`` where a book changed. The books it returns are the
        ones validation uses; when it fails, those in force from earlier scans."""
        if not any(d in DATASETS_WITH_BOOK for s in self.h.describe["sources"] for d in source_datasets(s)):
            return None
        try:
            books, issues = check_reference(runner.call_reference(self.h, self.index,
                                                                  self.settings.adapter_read_timeout_s))
        except AdapterFailure as f:
            books, issues = None, [{"code": f.kind}]
        self.res.notes.update(f"reference_{i['code']}" for i in issues)
        if books is None:
            log.warning("inbox: reference() unusable (%s): the stored books are used", ", ".join(self.res.notes))
            return store.current_books(self.con, self.now.astimezone(MSK).date())
        revised = store.store_books(self.con, books, self.h.fingerprint)  # type: ignore[arg-type]
        self.res.books_revised = len(revised)
        if revised:
            self._event("books.changed", counts={"books": len(revised)})
        return books

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
        out, limit = [], self.settings.inbox_max_file_mb * 2**20
        for p in list_files([str(d) for d in self.settings.inbox_dirs]):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size > limit:
                self.res.skipped["too_large"] += 1
                continue
            stable = (_last_seen.get(p) == (st.st_size, st.st_mtime_ns)
                      or self.now.timestamp() - st.st_mtime >= self.settings.inbox_poll_seconds)
            _last_seen[p] = (st.st_size, st.st_mtime_ns)
            if not stable:
                self.res.skipped["waiting"] += 1
                continue
            mtime = datetime.fromtimestamp(st.st_mtime, UTC)
            if self.con.execute("SELECT 1 FROM inbox_sighting WHERE path = %s AND size_bytes = %s AND mtime = %s",
                                (p, st.st_size, mtime)).fetchone():
                continue
            out.append(Offer(_sha256(p), p, os.path.basename(p), st.st_size, mtime, True))
        return out

    def known(self, want: Callable[[str, str], bool], source: str | None = None,
              between: tuple[date, date] | None = None) -> list[Offer]:
        """Files whose current status and adapter fingerprint pass ``want``: from the archive, else from the
        path they were read at when it still holds the same content."""
        out = []
        for sha, status, fp, path, arch, src, vd, name, size, mtime in self.con.execute(
                "SELECT DISTINCT ON (s.sha256) s.sha256, s.status, s.adapter_fingerprint, s.path, s.archive_path,"
                " s.source, s.valid_date, f.name, f.size_bytes, f.mtime FROM inbox_file_status s"
                " JOIN inbox_file f USING (sha256) ORDER BY s.sha256, s.status_id DESC").fetchall():
            if not want(status, fp) or (source is not None and src != source) \
                    or (between and not (vd and between[0] <= vd <= between[1])):
                continue
            p = arch if arch and os.path.isfile(arch) else \
                path if os.path.isfile(path) and _sha256(path) == sha else None
            if p is None:
                self.res.skipped["gone"] += 1
                continue
            out.append(Offer(sha, p, name, size, mtime, False))
        return out

    # ---- processing

    def run(self, offers: list[Offer]) -> None:
        offers = list({o.path: o for o in reversed(offers)}.values())[::-1]  # one offer per path, first wins
        if not offers:
            return
        hits: dict[str, Any] = {}
        for i in range(0, len(offers), MATCH_BATCH):  # one timeout per batch, not per folder
            hits.update(runner.call_match(self.h, [o.path for o in offers[i:i + MATCH_BATCH]], self.index,
                                          self.settings.adapter_match_timeout_s))
        dates = sorted({h["report_date"] for h in hits.values()
                        if isinstance(h, dict) and isinstance(h.get("report_date"), str)})
        refs = static_snapshots(self.cache)
        if refs and {covering(refs, d).id for d in dates} - {refs[-1].id}:  # lookups on older business dates
            build_index(self.cache, self.index, dates)
        for o in offers:
            with self.con.transaction():
                self.process(o, hits.get(o.path))

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
            raw = runner.call_read(self.h, o.path, hit, self.index, self.settings.adapter_read_timeout_s)
        except AdapterFailure as f:
            log.warning("inbox %s: read %s (%s)", o.sha256[:12], f.kind, f.type)
            self._status(o, f.kind, decl, error_type=f.type, error_frames=f.frames, **cols)
            return
        c = check_read(decl, hit, raw, books=self.books, contract=self.h.describe["contract"])
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
        # shortcut: every unit gets the file's business date; per-unit dates (_report_date, multi-date pnl_official and
        # trades) come with those datasets (S8, S13)
        outcomes = [self._unit(o, decl, hit, cols, parse_ts(gen), key, vd, u, issues) for u in c.units]
        states = Counter(st for st, _, _ in outcomes)
        if c.file_error:
            status = "rejected"
        elif states["rejected"]:
            status = "partially_accepted" if states["accepted"] + states["superseded"] else "rejected"
        else:
            status = "superseded" if states["superseded"] and not states["accepted"] else "accepted"
        sid = self._status(o, status, decl, generated_at=parse_ts(gen), error_code=c.file_error, counts={
            "units": dict(states), "rows": sum(len(u["rows"]) for u in c.units),
            "revisions": sum(1 for st, s, n in outcomes if n), "issues": dict(Counter(i["severity"] for i in issues))},
            **cols)
        unit_ids = []
        for u, (st, set_id, new) in zip(c.units, outcomes):
            self.res.units[st] += 1
            unit_ids.append(con.execute(
                "INSERT INTO inbox_unit (status_id, sha256, dataset, valid_date, report_date, scope, status, unit_hash,"
                " row_count, set_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING unit_id",
                (sid, o.sha256, u["dataset"], vd, u["report_date"], Jsonb(u["scope"]), st, unit_hash(u["rows"]),
                 len(u["rows"]), set_id)).fetchone()[0])
        for i in issues:
            scope = {k: v for k, v in (i["scope"] or {}).items() if not k.startswith("_")}
            uid = next((uid for u, uid in zip(c.units, unit_ids) if scope and u["scope"] == scope), None)
            con.execute("INSERT INTO inbox_issue (status_id, sha256, unit_id, severity, code, src, scope, message,"
                        " count, origin) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (sid, o.sha256, uid, i["severity"], i["code"], i["src"],
                         Jsonb(i["scope"]) if i["scope"] else None, i["message"], i["count"], i["origin"]))

    def _unit(self, o: Offer, decl: dict, hit: dict, cols: dict, gen: datetime | None, key: tuple, vd: date,
              u: dict, issues: list[dict]) -> tuple[str, int | None, bool]:
        """(status, set_id, new revision) of one unit (08 §6.6)."""
        if u["status"] == "rejected":
            return "rejected", None, False
        scope = u["scope"]
        if u["dataset"] != "positions" or "book_id" not in scope:
            # shortcut: S7a stores positions units scoped by book; other datasets and scopes come with their slices
            issues.append(_issue("error", "scope_not_supported", scope, "S7a stores positions per book_id"))
            return "rejected", None, False
        uh, src, prio = unit_hash(u["rows"]), hit["source"], decl.get("priority")
        sets = self.con.execute(
            "SELECT set_id, source, priority, report_date, report_ts, seq, generated_at, file_mtime, unit_hash"
            f" FROM position_set WHERE book_id = %s AND valid_date = %s ORDER BY {store.CURRENT_SET}",
            (scope["book_id"], vd)).fetchall()
        others = [s[2] for s in sets if s[1] != src]
        if any(prio is None or p is None or p == prio for p in others):
            issues.append(_issue("error", "scope_conflict", scope, "another source has this unit (08 §6.6)"))
            return "rejected", None, False
        if any(p > prio for p in others):
            return "superseded", None, False
        own = [s for s in sets if s[1] == src]
        if own:
            if key < _key(*own[0][3:8]):
                return "superseded", None, False
            if own[0][8] == uh:
                return "accepted", own[0][0], False
        set_id = store.store_inbox_set(
            self.con, book_id=scope["book_id"], valid_date=vd, rows=u["rows"], source=src, priority=prio,
            report_date=date.fromisoformat(hit["report_date"]), report_ts=cols["report_ts"], seq=hit.get("seq"),
            generated_at=gen, file_mtime=o.mtime, file_name=o.name, sha256=o.sha256,
            adapter_fingerprint=self.h.fingerprint, unit_hash=uh)  # type: ignore[arg-type]
        self.res.revisions += 1
        return "accepted", set_id, True

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
             offers: Callable[[_Scan], list[Offer]]) -> ScanResult:
    h = adapter(settings)
    res = ScanResult(adapter_line(h))
    if not h.ok:
        log.warning("inbox: adapter error %s: ingestion is off", h.error)
        return res
    if not con.execute("SELECT pg_try_advisory_lock(%s)", (SCAN_LOCK,)).fetchone()[0]:
        res.busy = True
        return res
    try:
        s = _Scan(con, settings, h, res, now or datetime.now(UTC))
        s.run(offers(s))
    finally:
        con.execute("SELECT pg_advisory_unlock(%s)", (SCAN_LOCK,))
    return res


def scan(con: psycopg.Connection, settings: Settings, *, now: datetime | None = None) -> ScanResult:
    """New stable files of the folders, files to re-offer after a fingerprint change, and those waiting for a
    calendar."""
    return _session(con, settings, now, lambda s: s.new_files() + s.known(
        lambda st, fp: (st in REOFFER and fp != s.h.fingerprint) or st == "waiting_calendar"))


def retry(con: psycopg.Connection, settings: Settings, statuses: tuple[str, ...] = (*REOFFER, "waiting_calendar"),
          *, now: datetime | None = None) -> ScanResult:
    """``desk inbox retry``: files in these statuses again, whatever the fingerprint."""
    return _session(con, settings, now, lambda s: s.known(lambda st, fp: st in statuses))


def replay(con: psycopg.Connection, settings: Settings, source: str, date_from: date, date_to: date, *,
           now: datetime | None = None) -> ScanResult:
    """``desk inbox replay``: read the accepted files of a source with business dates in the range again; a new
    revision only where a unit's canonical rows changed (08 §6.6)."""
    return _session(con, settings, now, lambda s: s.known(lambda st, fp: st in READ_DONE, source,
                                                          (date_from, date_to)))


def status(con: psycopg.Connection, settings: Settings, on: date | None = None) -> dict:
    """``desk inbox status``: per source and business date the current status of the files, their units and
    issues (of their latest attempt) and the copies; per active book the valid date of its positions for ``on``
    (default: today, Moscow) and its age. Codes and counts only."""
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
            "expected_valid_date": expect.isoformat() if expect else None, "groups": out_groups, "books": books}


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
    return "\n".join(lines)


