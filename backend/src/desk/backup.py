"""Backups of the desk store and the restore drill (ADR07: RPO 24 h, RTO 2 h; 06 §10).

A backup is ``pg_dump --format=custom`` of the store's database into the backup folder (setting ``backup_dir``,
default ``<home>/backups``; put it on another disk or a network share: the settings page warns while it shares the
disk with the store). Files are named ``desk-YYYYMMDD-HHMMSS-<kind>.dump`` (local time; kinds ``nightly``,
``manual``, ``pre-restore``), written as ``.partial`` and renamed once ``pg_restore --list`` reads them back, so a
half-written dump never counts. Beside each a ``.json`` holds what the drill checks against: the exact row count of
every table, counted in the very snapshot ``pg_dump`` dumped (``pg_export_snapshot`` and ``pg_dump --snapshot``),
the schema migrations, the size and the SHA-256 of the file.

* **Schedule** (no Windows service, no Task Scheduler, no administrator rights): ``Scheduler``, a thread of
  ``desk serve`` (the ``start.bat`` window), checks every minute; a backup is due when none was made since the most
  recent ``backup_time`` (local, default 20:30), so a computer that was off at that time backs up as soon as the
  server starts next. ``desk cycle`` (``update_daily.bat``) makes the due backup too. A failure is tried again
  after ``RETRY_S``. One backup at a time per home folder, across processes (``<home>/backup.lock``).
* **Retention**: the newest ``backup_keep`` (default 14) nightly and manual dumps stay, older ones are deleted
  after each new backup; the newest ``KEEP_PRE_RESTORE`` pre-restore dumps stay apart from them.
* **Restore drill** (``restore_check``; ``desk restore``, the settings page's «Проверить восстановление»): the
  dump is restored into a scratch database ``<db>_restore_check`` next to the live one, never into it; every
  table's row count must equal the count recorded at backup time, the schema must not be newer than this program,
  and this program's migrations must apply to it. The scratch database is dropped afterwards.
* **Restore** (``restore_replace``; ``desk restore FILE --replace``, from the command line only, with the server
  stopped): the drill first; then the explicit confirmation; then a ``pre-restore`` dump of the live database
  (no dump, no restore); then the live database is renamed ``<db>_before_restore`` (the previous one of that name
  dropped) and the checked scratch database takes its name. Nothing is overwritten in place.

``pg_dump`` and ``pg_restore`` come from the embedded PostgreSQL (``pgserver``'s ``pginstall/bin``, the same
version as the server), else from ``PATH`` (a ``DESK_STORE_URL`` server).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import psycopg
from psycopg import sql

from . import store
from .settings import Settings, SettingsError, atomic_write_text, load, mask_url

log = logging.getLogger("desk.backup")

KINDS = ("nightly", "manual", "pre-restore")
NAME_RE = re.compile(r"desk-(\d{8})-(\d{6})-(nightly|manual|pre-restore)\.dump")
SCRATCH_SUFFIX = "_restore_check"
PREVIOUS_SUFFIX = "_before_restore"
KEEP_PRE_RESTORE = 3
CHECK_S = 60.0           # the scheduler looks this often whether a backup is due
RETRY_S = 1800.0         # after a failed scheduled backup, the next try
STATE_FILE = "backup-state.json"
LOCK_FILE = "backup.lock"
SERVE_LOCK = "serve.lock"  # held by desk serve while it runs (hold_serve_lock); a restore needs it free
DUMP_TIMEOUT_S = 3 * 3600
CONFIRM_WORD = "restore"


class BackupError(Exception):
    """A backup or a restore that did not happen; the message says why (for the console and the page)."""


@dataclass(frozen=True)
class Backup:
    path: Path
    kind: str
    created: datetime  # local time, from the name
    size: int
    meta: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def name(self) -> str:
        return self.path.name

    def to_json(self) -> dict[str, Any]:
        tables = self.meta.get("tables") or {}
        return {"name": self.name, "kind": self.kind, "created": self.created.isoformat(timespec="seconds"),
                "size": self.size, "rows": sum(tables.values()) if tables else None, "tables": len(tables) or None,
                "runs": tables.get("run"), "schema": self.meta.get("schema"), "version": self.meta.get("version"),
                "checked": bool(self.meta)}


# ---- where and what ---------------------------------------------------------------------------------------------


def backup_dir(settings: Settings) -> Path:
    return Path(settings.backup_dir) if settings.backup_dir else Path(settings.home) / "backups"


def tool(name: str) -> Path:
    """``pg_dump`` / ``pg_restore``: the embedded PostgreSQL's (pgserver), else the one on ``PATH``."""
    exe = name + (".exe" if os.name == "nt" else "")
    try:
        import pgserver

        p = Path(pgserver.__file__).parent / "pginstall" / "bin" / exe
        if p.is_file():
            return p
    except ImportError:
        pass
    found = shutil.which(name)
    if found:
        return Path(found)
    raise BackupError(f"{name} not found: neither the embedded PostgreSQL (pgserver) nor PATH has it")


def db_name(url: str) -> str:
    q = dict(parse_qsl(urlsplit(url).query))
    return q.get("dbname") or urlsplit(url).path.lstrip("/") or "postgres"


def with_db(url: str, name: str) -> str:
    """``url`` naming another database of the same server."""
    u = urlsplit(url)
    q = parse_qsl(u.query, keep_blank_values=True)
    if any(k == "dbname" for k, _ in q):
        q = [(k, name if k == "dbname" else v) for k, v in q]
        return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q, safe="/:"), u.fragment))
    return urlunsplit((u.scheme, u.netloc, "/" + name, u.query, u.fragment))


def parse_name(name: str) -> tuple[datetime, str] | None:
    m = NAME_RE.fullmatch(name)
    if not m:
        return None
    return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S"), m.group(3)


def _meta_path(dump: Path) -> Path:
    return dump.with_suffix(".json")


def read_meta(dump: Path) -> dict[str, Any]:
    try:
        doc = json.loads(_meta_path(dump).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def list_backups(folder: Path) -> list[Backup]:
    """The backups of ``folder``, newest first (files named as this module names them; others are ignored)."""
    out = []
    if not folder.is_dir():
        return out
    for p in folder.glob("desk-*.dump"):
        parsed = parse_name(p.name)
        if parsed is None or not p.is_file():
            continue
        out.append(Backup(p, parsed[1], parsed[0], p.stat().st_size, read_meta(p)))
    return sorted(out, key=lambda b: (b.created, b.name), reverse=True)


def find(settings: Settings, which: str | None) -> Path:
    """``which``: a path, a file name of the backup folder, or None / ``latest`` (the newest backup)."""
    folder = backup_dir(settings)
    if which in (None, "", "latest"):
        backups = [b for b in list_backups(folder) if b.kind != "pre-restore"] or list_backups(folder)
        if not backups:
            raise BackupError(f"no backups in {folder}")
        return backups[0].path
    p = Path(which)
    if not p.is_file() and (folder / which).is_file():
        p = folder / which
    if not p.is_file():
        raise BackupError(f"backup file not found: {which}")
    return p


def same_disk(a: Path, b: Path) -> bool | None:
    """Whether two folders are on one disk (None: unknown). A network share (``\\\\server\\share``) is another disk."""
    def existing(p: Path) -> Path | None:
        p = Path(os.path.abspath(p))
        while not p.exists():
            if p.parent == p:
                return None
            p = p.parent
        return p

    if os.name == "nt":
        da, db = (os.path.splitdrive(os.path.abspath(x))[0].lower() for x in (a, b))
        return bool(da) and da == db
    ea, eb = existing(a), existing(b)
    if ea is None or eb is None:
        return None
    return ea.stat().st_dev == eb.stat().st_dev


# ---- running the PostgreSQL tools ---------------------------------------------------------------------------------


def _run(cmd: list[str], *, timeout: float = DUMP_TIMEOUT_S) -> subprocess.CompletedProcess:
    """A PostgreSQL client tool, without a console window on Windows; the connection URI is never logged."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False, creationflags=flags, env=env)
    except subprocess.TimeoutExpired:
        raise BackupError(f"{Path(cmd[0]).name} did not finish in {timeout:.0f} s") from None
    except OSError as e:
        raise BackupError(f"{Path(cmd[0]).name} could not start: {e}") from None


def _err(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or b"").decode("utf-8", "replace").strip() or (proc.stdout or b"").decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()][-5:]
    return mask_url(" | ".join(lines))[:800] or f"exit code {proc.returncode}"


def archive_entries(dump: Path) -> int:
    """The number of entries ``pg_restore --list`` reads from ``dump`` (BackupError when it cannot read it)."""
    proc = _run([str(tool("pg_restore")), "--list", str(dump)], timeout=600)
    if proc.returncode != 0:
        raise BackupError(f"pg_restore cannot read {dump.name}: {_err(proc)}")
    n = sum(1 for ln in proc.stdout.decode("utf-8", "replace").splitlines() if ln.strip() and not ln.startswith(";"))
    if n == 0:
        raise BackupError(f"{dump.name}: the archive is empty")
    return n


def _tables(con: psycopg.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition ORDER BY 1")]


def table_counts(con: psycopg.Connection) -> dict[str, int]:
    return {t: con.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[0]
            for t in _tables(con)}


def _schema(con: psycopg.Connection) -> list[str]:
    if con.execute("SELECT to_regclass('schema_migration')").fetchone()[0] is None:
        return []
    return sorted(r[0] for r in con.execute("SELECT version FROM schema_migration"))


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _version() -> str:
    from . import __version__

    return __version__


# ---- state (for the settings page) --------------------------------------------------------------------------------


def read_state(home: Path) -> dict[str, Any]:
    try:
        doc = json.loads((Path(home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _record(home: Path, key: str, entry: dict[str, Any]) -> None:
    try:
        doc = read_state(home)
        doc[key] = entry
        atomic_write_text(Path(home) / STATE_FILE, json.dumps(doc, ensure_ascii=False, indent=1, default=str) + "\n")
    except OSError:
        log.warning("backup state not written", exc_info=True)


def _lock(home: Path, name: str = LOCK_FILE):
    from .autopilot import CycleLock

    return CycleLock(home, name)


_serve_lock: Any = None


def hold_serve_lock(home: Path) -> bool:
    """``desk serve``: hold ``<home>/serve.lock`` for the life of the process (the OS releases it on exit or a crash),
    so a restore knows the server runs even between its connections. False when another server of this home holds
    it."""
    global _serve_lock
    lock = _lock(home, SERVE_LOCK)
    try:
        if not lock.acquire():
            return False
    except OSError:
        return False
    _serve_lock = lock
    return True


# ---- backup -------------------------------------------------------------------------------------------------------


def create(settings: Settings, url: str | None = None, kind: str = "manual", *, now: datetime | None = None,
           folder: Path | None = None, prune_after: bool = True) -> Backup:
    """One backup of the store's database into ``folder`` (default ``backup_dir``); retention after it.
    BackupError when another backup runs or any step fails (nothing half-written is left)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    url = url or store.resolve_url(settings)
    folder = Path(folder or backup_dir(settings))
    lock = _lock(settings.home)
    if not lock.acquire():
        raise BackupError("another backup is running")
    t0 = time.monotonic()
    try:
        b = _dump(url, folder, kind, now or datetime.now())
    except BackupError as e:
        _record(settings.home, "last", {"at": datetime.now().isoformat(timespec="seconds"), "ok": False, "kind": kind,
                                        "error": str(e)})
        raise
    finally:
        lock.release()
    removed = prune(folder, settings.backup_keep) if prune_after else []
    _record(settings.home, "last", {"at": datetime.now().isoformat(timespec="seconds"), "ok": True, "kind": kind,
                                    "file": b.name, "size": b.size, "seconds": round(time.monotonic() - t0, 1),
                                    "pruned": [p.name for p in removed]})
    log.info("backup %s: %s, %d bytes", kind, b.name, b.size)
    return b


def _dump(url: str, folder: Path, kind: str, now: datetime) -> Backup:
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"cannot create the backup folder {folder}: {e}") from None
    name = f"desk-{now:%Y%m%d-%H%M%S}-{kind}.dump"
    final = folder / name
    n = 1
    while final.exists():  # two in one second
        final = folder / f"desk-{(now + timedelta(seconds=n)):%Y%m%d-%H%M%S}-{kind}.dump"
        n += 1
    partial = final.with_name(final.name + ".partial")
    try:
        with psycopg.connect(url) as con:  # a transaction held open: the snapshot pg_dump dumps
            con.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            snapshot = con.execute("SELECT pg_export_snapshot()").fetchone()[0]
            counts = table_counts(con)
            schema = _schema(con)
            proc = _run([str(tool("pg_dump")), "--format=custom", f"--snapshot={snapshot}", f"--file={partial}",
                         f"--dbname={url}"])
            con.rollback()
    except psycopg.Error as e:
        partial.unlink(missing_ok=True)
        raise BackupError(f"the store cannot be read: {mask_url(str(e)).strip()}") from None
    except BackupError:
        partial.unlink(missing_ok=True)
        raise
    if proc.returncode != 0:
        partial.unlink(missing_ok=True)
        raise BackupError(f"pg_dump failed: {_err(proc)}")
    try:
        entries = archive_entries(partial)
        meta = {"format": 1, "kind": kind, "created": now.isoformat(timespec="seconds"), "database": db_name(url),
                "version": _version(), "schema": schema[-1] if schema else None, "migrations": schema,
                "tables": counts, "entries": entries, "size": partial.stat().st_size, "sha256": _sha256(partial)}
        atomic_write_text(_meta_path(final), json.dumps(meta, ensure_ascii=False, indent=1) + "\n")
        os.replace(partial, final)
    except (BackupError, OSError) as e:
        partial.unlink(missing_ok=True)
        _meta_path(final).unlink(missing_ok=True)
        raise BackupError(str(e) if isinstance(e, BackupError) else f"cannot write {final}: {e}") from None
    return Backup(final, kind, now.replace(microsecond=0), final.stat().st_size, meta)


def prune(folder: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` nightly and manual backups and the newest ``KEEP_PRE_RESTORE``
    pre-restore ones (each with its ``.json``); returns the deleted dumps. Leftover ``.partial`` files go too."""
    backups = list_backups(folder)
    regular = [b for b in backups if b.kind != "pre-restore"]
    pre = [b for b in backups if b.kind == "pre-restore"]
    removed = []
    for b in regular[max(keep, 1):] + pre[KEEP_PRE_RESTORE:]:
        try:
            b.path.unlink()
            _meta_path(b.path).unlink(missing_ok=True)
            removed.append(b.path)
        except OSError:
            log.warning("old backup not deleted: %s", b.path, exc_info=True)
    for p in folder.glob("desk-*.dump.partial") if folder.is_dir() else ():
        if time.time() - p.stat().st_mtime > DUMP_TIMEOUT_S:  # never one being written now
            p.unlink(missing_ok=True)
    return removed


# ---- schedule -----------------------------------------------------------------------------------------------------


def parse_time(hhmm: str) -> tuple[int, int]:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", (hhmm or "").strip())
    if not m:
        raise SettingsError(f"backup_time must be HH:MM (00:00..23:59), not {hhmm!r}")
    return int(m.group(1)), int(m.group(2))


def last_moment(now: datetime, hhmm: str) -> datetime:
    """The most recent scheduled time at or before ``now``."""
    h, m = parse_time(hhmm)
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return t if t <= now else t - timedelta(days=1)


def due(settings: Settings, now: datetime | None = None, backups: list[Backup] | None = None) -> bool:
    """A backup is due when backups are on and none (of any kind but pre-restore) was made since the most recent
    ``backup_time``."""
    if not settings.backup:
        return False
    now = now or datetime.now()
    backups = list_backups(backup_dir(settings)) if backups is None else backups
    newest = next((b for b in backups if b.kind != "pre-restore"), None)
    return newest is None or newest.created < last_moment(now, settings.backup_time)


def next_at(settings: Settings, now: datetime | None = None) -> datetime | None:
    if not settings.backup:
        return None
    now = now or datetime.now()
    if due(settings, now):
        return now.replace(microsecond=0)
    return last_moment(now, settings.backup_time) + timedelta(days=1)


def run_due(settings: Settings, url: str | None = None, *, now: datetime | None = None,
            out: Callable[[str], None] | None = None) -> Backup | None:
    """The scheduled backup when due (``desk cycle``, the scheduler); None when not due. Errors are reported through
    ``out`` and the state file, not raised."""
    if not due(settings, now):
        return None
    try:
        b = create(settings, url, "nightly", now=now)
    except BackupError as e:
        log.warning("scheduled backup failed: %s", e)
        if out:
            out(f"backup: FAILED - {e}")
        return None
    if out:
        out(f"backup: {b.name} ({b.size / 1e6:.1f} MB)")
    return b


class Scheduler:
    """The scheduled backup inside ``desk serve``: a daemon thread, settings read again on every look."""

    def __init__(self, *, settings_loader: Callable[[], Settings] = load, store_url: str | None = None,
                 clock: Callable[[], datetime] = datetime.now, check_s: float = CHECK_S) -> None:
        self._load, self._url, self._clock, self._check_s = settings_loader, store_url, clock, check_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._retry_after = 0.0  # monotonic: no scheduled try before this after a failure

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="desk-backup", daemon=True)
            self._thread.start()

    def stop(self, timeout: float | None = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def tick(self) -> Backup | None:
        """One look: the due backup, if any (also the tests' entry)."""
        if time.monotonic() < self._retry_after:
            return None
        try:
            settings = self._load()
        except SettingsError:
            return None
        now = self._clock()
        if not due(settings, now):
            return None
        try:
            return create(settings, self._url, "nightly", now=now)
        except BackupError as e:
            if str(e) != "another backup is running":
                log.warning("scheduled backup failed (next try in %.0f min): %s", RETRY_S / 60, e)
                self._retry_after = time.monotonic() + RETRY_S
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # the last guard of the thread
                log.exception("backup scheduler")
            self._stop.wait(self._check_s)


# ---- restore ------------------------------------------------------------------------------------------------------


def _admin(url: str) -> psycopg.Connection:
    """A connection to the server's maintenance database (``postgres``): databases are created, dropped and renamed
    from there."""
    return psycopg.connect(with_db(url, "postgres"), autocommit=True)


def _drop(con: psycopg.Connection, name: str) -> None:
    con.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _exists(con: psycopg.Connection, name: str) -> bool:
    return con.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone() is not None


def other_sessions(url: str) -> int:
    """Connections to the live database other than this one (a running ``desk serve``, a ``desk cycle``)."""
    with _admin(url) as con:
        return con.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                           (db_name(url),)).fetchone()[0]


def restore_check(settings: Settings, url: str | None, dump: Path, *, keep_scratch: bool = False,
                  out: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The restore drill: ``dump`` restored into ``<db>_restore_check`` and checked (see the module doc). Returns a
    report; ``ok`` False lists the ``problems``. The scratch database is dropped unless ``keep_scratch``.
    BackupError when the drill cannot run at all (no tools, the server unreachable, another drill running)."""
    say = out or (lambda _line: None)
    url = url or store.resolve_url(settings)
    dump = Path(dump)
    scratch = db_name(url) + SCRATCH_SUFFIX
    scratch_url = with_db(url, scratch)
    meta = read_meta(dump)
    report: dict[str, Any] = {"file": dump.name, "size": dump.stat().st_size if dump.is_file() else None,
                              "scratch": scratch, "started": datetime.now().isoformat(timespec="seconds"),
                              "problems": [], "warnings": [], "tables": {}, "meta": bool(meta)}
    problems, warnings = report["problems"], report["warnings"]
    lock = _lock(settings.home)
    if not lock.acquire():
        raise BackupError("a backup or another restore check is running")
    t0 = time.monotonic()
    if not dump.is_file():
        lock.release()
        raise BackupError(f"backup file not found: {dump}")
    try:
        if meta.get("sha256"):
            say("checksum ...")
            if meta.get("size") != dump.stat().st_size:
                problems.append(f"the file has {dump.stat().st_size} bytes, {meta.get('size')} at backup time "
                                "(damaged or cut short)")
            elif _sha256(dump) != meta["sha256"]:
                problems.append("the file's SHA-256 differs from the one recorded at backup time (damaged copy)")
        elif not meta:
            warnings.append(f"no {_meta_path(dump).name} beside the dump: row counts cannot be compared")
        say("reading the archive (pg_restore --list) ...")
        report["entries"] = archive_entries(dump)
        with _admin(url) as con:
            _drop(con, scratch)
            con.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(sql.Identifier(scratch)))
        say(f"restoring into the scratch database {scratch} ...")
        proc = _run([str(tool("pg_restore")), "--no-owner", "--no-privileges", "--exit-on-error",
                     f"--dbname={scratch_url}", str(dump)])
        if proc.returncode != 0:
            problems.append(f"pg_restore failed: {_err(proc)}")
        else:
            say("comparing row counts ...")
            with store.connect(scratch_url) as con:
                counts = table_counts(con)
                schema = _schema(con)
                report["runs"] = con.execute("SELECT count(*), max(business_date)::text FROM run").fetchone() \
                    if "run" in counts else None
            report["tables"] = counts
            report["rows"] = sum(counts.values())
            report["schema"] = schema[-1] if schema else None
            if not schema:
                problems.append("no schema_migration table: this is not a desk store backup")
            known = {f.stem for f in store.MIGRATIONS.glob("*.sql")}
            if newer := sorted(set(schema) - known):
                problems.append("the backup comes from a newer program version (migrations "
                                + ", ".join(newer) + "): install that version first")
            if meta.get("tables"):
                want = meta["tables"]
                diff = [f"{t}: {want.get(t)} at backup, {counts.get(t)} restored"
                        for t in sorted(set(want) | set(counts)) if want.get(t) != counts.get(t)]
                if diff:
                    problems.append("row counts differ: " + "; ".join(diff[:10])
                                    + (f" (+{len(diff) - 10} more)" if len(diff) > 10 else ""))
            if not problems:
                say("applying this program's migrations to the restored copy ...")
                try:
                    report["migrated"] = store.migrate(scratch_url)
                except psycopg.Error as e:
                    problems.append(f"this program's migrations fail on the restored copy: {e}".strip())
    except BackupError as e:  # a damaged archive, a failed step: the drill's finding, not a crash
        problems.append(str(e))
    finally:
        lock.release()
        if not keep_scratch:
            try:
                with _admin(url) as con:
                    _drop(con, scratch)
            except psycopg.Error:
                log.warning("scratch database %s not dropped", scratch, exc_info=True)
    report["ok"] = not problems
    report["seconds"] = round(time.monotonic() - t0, 1)
    if meta.get("created"):
        report["backup_created"] = meta["created"]
    _record(settings.home, "last_check", {k: report.get(k) for k in ("started", "file", "ok", "problems", "warnings",
                                                                     "rows", "seconds", "schema")}
            | {"tables": len(report["tables"])})
    return report


def restore_replace(settings: Settings, url: str | None, dump: Path, *, confirm: Callable[[dict[str, Any]], bool],
                    actor: str = "cli", out: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Replace the live database with ``dump`` (see the module doc). ``confirm(report)`` is asked after the drill
    passed and before anything changes; False stops with nothing changed. BackupError on any refusal."""
    url = url or store.resolve_url(settings)
    serving, cycle = _lock(settings.home, SERVE_LOCK), _lock(settings.home, "cycle.lock")
    try:
        free = serving.acquire()
    except OSError:
        free = True  # no lock file possible (read-only home): the connection check below still applies
    if not free:
        raise BackupError("Desk Monitor is running (the start.bat window): stop the server first (stop.bat), "
                          "then run this again")
    try:
        if not cycle.acquire():
            raise BackupError("an autopilot cycle is running (desk cycle, update_daily.bat): wait for it to end")
        try:
            return _replace(settings, url, dump, confirm=confirm, actor=actor, out=out)
        finally:
            cycle.release()
    finally:
        serving.release()


def _replace(settings: Settings, url: str, dump: Path, *, confirm: Callable[[dict[str, Any]], bool], actor: str,
             out: Callable[[str], None] | None) -> dict[str, Any]:
    say = out or (lambda _line: None)
    live = db_name(url)
    scratch, previous = live + SCRATCH_SUFFIX, live + PREVIOUS_SUFFIX
    if n := other_sessions(url):
        raise BackupError(f"{n} other connection(s) to the database {live}: stop the server first (stop.bat), "
                          "then run this again")
    report = restore_check(settings, url, dump, keep_scratch=True, out=out)
    try:
        if not report["ok"]:
            raise BackupError("the restore check failed, the live data is unchanged: " + "; ".join(report["problems"]))
        try:
            with store.connect(url) as con:
                report["live_tables"] = table_counts(con)
        except psycopg.Error:
            report["live_tables"] = {}
        if not confirm(report):
            raise BackupError("not confirmed: the live data is unchanged")
        say("pre-restore backup of the live database ...")
        pre = create(settings, url, "pre-restore", prune_after=False)
        report["pre_restore"] = str(pre.path)
        if n := other_sessions(url):
            raise BackupError(f"{n} connection(s) to {live} opened meanwhile: stop the server (stop.bat) and run "
                              "this again; the live data is unchanged")
        say(f"switching databases: {live} -> {previous}, {scratch} -> {live} ...")
        with _admin(url) as con:
            _drop(con, previous)
            con.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(live), sql.Identifier(previous)))
            try:
                con.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(scratch),
                                                                             sql.Identifier(live)))
            except psycopg.Error:
                con.execute(sql.SQL("ALTER DATABASE {} RENAME TO {}").format(sql.Identifier(previous),
                                                                             sql.Identifier(live)))
                raise
        report["previous"] = previous
    except BaseException:
        try:
            with _admin(url) as con:
                if _exists(con, scratch):
                    _drop(con, scratch)
        except psycopg.Error:
            log.warning("scratch database %s not dropped", scratch, exc_info=True)
        raise
    prune(backup_dir(settings), settings.backup_keep)
    try:
        from .api.auth import audit

        with store.connect(url) as con:
            audit(con, actor, "backup_restore", Path(dump).name,
                  detail={"pre_restore": Path(report["pre_restore"]).name, "rows": report.get("rows")})
    except Exception:
        log.warning("restore not recorded in the audit log", exc_info=True)
    _record(settings.home, "last_restore", {"at": datetime.now().isoformat(timespec="seconds"), "file": Path(dump).name,
                                            "pre_restore": Path(report["pre_restore"]).name, "by": actor})
    return report


# ---- the settings page and diagnostics ----------------------------------------------------------------------------


def status(settings: Settings, job: dict[str, Any] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """``GET /api/v1/backup/status``: the settings, the folder, its disk, the backups, the last attempt and drill."""
    now = now or datetime.now()
    folder = backup_dir(settings)
    backups = list_backups(folder)
    state = read_state(settings.home)
    regular = [b for b in backups if b.kind != "pre-restore"]
    newest = regular[0] if regular else None
    age_h = round((now - newest.created).total_seconds() / 3600, 1) if newest else None
    disk = None if settings.store_url else same_disk(folder, settings.pg_dir)
    try:
        tools_ok, tools_error = bool(tool("pg_dump") and tool("pg_restore")), None
    except BackupError as e:
        tools_ok, tools_error = False, str(e)
    return {"enabled": settings.backup, "time": settings.backup_time, "keep": settings.backup_keep,
            "dir": str(folder), "dir_set": settings.backup_dir is not None, "same_disk": disk,
            "store": "external" if settings.store_url else "embedded", "tools": tools_ok, "tools_error": tools_error,
            "next_at": (n.isoformat(timespec="seconds") if (n := next_at(settings, now)) else None),
            "newest_age_hours": age_h, "stale": settings.backup and (age_h is None or age_h > 26),
            "backups": [b.to_json() for b in backups], "total_size": sum(b.size for b in backups),
            "last": state.get("last"), "last_check": state.get("last_check"), "last_restore": state.get("last_restore"),
            "job": job}


def diagnostics(settings: Settings, now: datetime | None = None) -> dict[str, Any]:
    """The diagnostics section: counts, ages and outcomes, no paths or file names."""
    st = status(settings, now=now)
    last, check = st["last"] or {}, st["last_check"] or {}
    return {"enabled": st["enabled"], "time": st["time"], "keep": st["keep"], "dir_set": st["dir_set"],
            "same_disk": st["same_disk"], "tools": st["tools"], "count": len(st["backups"]),
            "total_mb": round(st["total_size"] / 1e6, 1), "newest_age_hours": st["newest_age_hours"],
            "stale": st["stale"], "last_ok": last.get("ok"), "last_at": last.get("at"),
            "last_error": (last.get("error") or "")[:200] or None, "last_check_ok": check.get("ok"),
            "last_check_at": check.get("started")}
