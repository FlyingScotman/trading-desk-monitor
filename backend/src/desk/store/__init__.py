"""Desk store (slice S5; ADR-006, 04 §2-§3): PostgreSQL through psycopg 3, plain SQL.

Positions are bitemporal and append-only: a load adds a ``position_set`` per book and valid date
(a correction is the next revision), and ``positions_for`` reads "positions of D as known at T".
Runs are immutable once complete or failed (``desk.publish.desk_run`` writes them); the triggers of
``migrations/0001_init.sql`` reject any update or delete. Every function here runs its own
transactions: pass a connection from ``connect`` (autocommit).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg

from ..settings import REPO_ROOT, Settings, SettingsError
from ..valuation.fallback import CarriedSpread

MIGRATIONS = Path(__file__).parent / "migrations"
DEV_PGDATA = REPO_ROOT / ".devdata" / "desk_pg"  # not .devdata/pg: the hub seed cluster (ADR-006)
_LOCK = 0x6465736B  # "desk": one migrator at a time


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True)


def resolve_url(settings: Settings) -> str:
    """``DESK_STORE_URL``, else the embedded dev server in ``.devdata/desk_pg`` (pgserver), database ``desk``.
    The dev server keeps running after the process exits, so later commands share it."""
    if settings.store_url:
        return settings.store_url
    try:
        import pgserver
    except ImportError:
        raise SettingsError("set DESK_STORE_URL (postgresql://...): the embedded dev store needs pgserver, "
                            "which is not installed (uv sync --extra dev)") from None
    server = pgserver.get_server(DEV_PGDATA, cleanup_mode=None)
    if "1 row" not in server.psql("SELECT 1 FROM pg_database WHERE datname = 'desk';"):
        server.psql("CREATE DATABASE desk ENCODING 'UTF8' TEMPLATE template0;")
    return server.get_uri("desk")


def migrate(url: str) -> list[str]:
    """Apply the migrations not yet in ``schema_migration``, in order, each in its own transaction.
    Returns the versions applied (empty when the store is current)."""
    applied = []
    with connect(url) as con:
        con.execute("SELECT pg_advisory_lock(%s)", (_LOCK,))
        try:
            done = set()
            if con.execute("SELECT to_regclass('schema_migration')").fetchone()[0] is not None:
                done = {r[0] for r in con.execute("SELECT version FROM schema_migration")}
            for f in sorted(MIGRATIONS.glob("*.sql")):
                if f.stem in done:
                    continue
                with con.transaction():
                    con.execute(f.read_text(encoding="utf-8"))
                    con.execute("INSERT INTO schema_migration (version) VALUES (%s)", (f.stem,))
                applied.append(f.stem)
        finally:
            con.execute("SELECT pg_advisory_unlock(%s)", (_LOCK,))
    return applied


# --- positions -----------------------------------------------------------------------------------------

class InputError(ValueError):
    """A positions file the store rejects; nothing was written."""


@dataclass(frozen=True)
class Loaded:
    book_id: str
    set_id: int
    revision: int
    changed: bool
    """False: the latest set of the book and date already held exactly these positions (no new set)."""


def load_positions(con: psycopg.Connection, rows: list[tuple[str, str, str, float]], *, valid_date: date,
                   source_name: str, source_sha256: str, recorded_by: str | None = None,
                   books: list[str] | None = None) -> list[Loaded]:
    """rows: (position_id, book_id, secid, quantity), as ``cli.read_book`` returns them. The load covers
    ``books`` (default: the books of the rows): one position set per book, revision = the previous one of
    (book, valid_date) + 1; a covered book without rows gets an empty set (closed or dropped), unless its
    latest set of the date is empty already. Raises InputError, writing nothing, when a position id is
    twice in the rows, in a book not covered, or in the latest set (valid date <= ``valid_date``) of a book
    the load does not cover."""
    by_book: dict[str, set[tuple[str, str, float]]] = {b: set() for b in books or ()}
    for pid, book, secid, qty in rows:
        by_book.setdefault(book, set()).add((pid, secid, float(qty)))
    if dup := sorted(p for p, n in Counter(r[0] for r in rows).items() if n > 1):
        raise InputError(f"{source_name}: position ids twice in the file: {', '.join(dup)}")
    if books is not None and (extra := sorted(set(by_book) - set(books))):
        raise InputError(f"{source_name}: rows of books the load does not cover ({', '.join(books)}): "
                         f"{', '.join(extra)}")
    out = []
    with con.transaction():
        clash = con.execute(
            "SELECT i.position_id, s.book_id FROM position_input i JOIN (SELECT DISTINCT ON (book_id) set_id,"
            " book_id FROM position_set WHERE valid_date <= %s AND book_id <> ALL(%s)"
            " ORDER BY book_id, valid_date DESC, recorded_at DESC, revision DESC) s USING (set_id)"
            " WHERE i.position_id = ANY(%s) ORDER BY 1",
            (valid_date, list(by_book), [r[0] for r in rows])).fetchall()
        if clash:
            raise InputError(f"{source_name}: position ids already in other books for {valid_date}: "
                             + ", ".join(f"{p} ({b})" for p, b in clash))
        for book, content in sorted(by_book.items()):
            last = con.execute("SELECT set_id, revision FROM position_set WHERE book_id = %s AND valid_date = %s "
                               "ORDER BY revision DESC LIMIT 1", (book, valid_date)).fetchone()
            if last and set(con.execute("SELECT position_id, secid, quantity FROM position_input WHERE set_id = %s",
                                        (last[0],)).fetchall()) == content:
                out.append(Loaded(book, last[0], last[1], False))
                continue
            rev = last[1] + 1 if last else 1
            set_id = con.execute(
                "INSERT INTO position_set (book_id, valid_date, revision, recorded_by, source_name, source_file_sha256) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING set_id",
                (book, valid_date, rev, recorded_by, source_name, source_sha256)).fetchone()[0]
            with con.cursor() as cur:
                cur.executemany("INSERT INTO position_input (set_id, position_id, secid, quantity) VALUES (%s, %s, %s, %s)",
                                [(set_id, *p) for p in sorted(content)])
            out.append(Loaded(book, set_id, rev, True))
    return out


@dataclass(frozen=True)
class PositionInputs:
    """The private inputs of a run: positions of a date as known at a time."""

    rows: list[tuple[str, str, str, float]]
    """(position_id, book_id, secid, quantity), by book then position."""
    set_ids: list[int]
    valid_dates: dict[str, date]
    """Valid date of each book's set: earlier than the run's date when the positions are carried forward."""
    known_at: datetime
    private_inputs_hash: str
    """sha256 over the content of the sets (books and positions), not their ids or load times."""


def positions_for(con: psycopg.Connection, valid_date: date, *, known_at: datetime | None = None,
                  books: list[str] | None = None) -> PositionInputs:
    """Per book the set with the latest valid date <= ``valid_date``, of those the latest recorded at or
    before ``known_at`` (default now). A closed book (an empty set) has no rows but its set id is listed.
    known_at is a horizon, not an exact replay (a load committing after it can carry an earlier
    recorded_at): a run's ``position_set_ids`` are the source of truth for its inputs."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    sets = con.execute(
        "SELECT DISTINCT ON (book_id) set_id, book_id, valid_date FROM position_set "
        "WHERE valid_date <= %s AND recorded_at <= %s AND (%s::text[] IS NULL OR book_id = ANY(%s)) "
        "ORDER BY book_id, valid_date DESC, recorded_at DESC, revision DESC",
        (valid_date, known_at, books, books)).fetchall()
    ids = [s[0] for s in sets]
    rows = [tuple(r) for r in con.execute(
        "SELECT i.position_id, s.book_id, i.secid, i.quantity FROM position_input i JOIN position_set s USING (set_id) "
        "WHERE set_id = ANY(%s) ORDER BY s.book_id, i.position_id", (ids,))]
    canon = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return PositionInputs(rows, ids, {b: d for _, b, d in sets}, known_at,  # type: ignore[arg-type]
                          hashlib.sha256(canon.encode()).hexdigest())


# --- runs ----------------------------------------------------------------------------------------------

# The default run of a date (04 §3): the latest complete EOD run on eod_final, otherwise on eod_preliminary.
DEFAULT_RUNS = ("SELECT DISTINCT ON (business_date) run_id FROM run"
                " WHERE status = 'complete' AND run_type = 'EOD'"
                " AND cut_type IN ('eod_final', 'eod_preliminary')"
                " ORDER BY business_date, cut_type = 'eod_final' DESC, completed_at DESC, run_id DESC")


def carried_spreads(con: psycopg.Connection, before: date) -> dict[str, CarriedSpread]:
    """Per secid the latest Z-spread of the default runs (``DEFAULT_RUNS``) before ``before``: the model
    spread a model price used, or the bond's last market Z (exact or assumed), whichever run is later.
    TEST, INTRADAY, REPLAY and superseded runs never supply one."""
    rows = con.execute(
        "SELECT DISTINCT ON (secid) secid, z, business_date, run_id, source FROM ("
        " SELECT m.secid, m.z_spread_bp AS z, r.business_date, r.run_id, m.source, 1 AS pri"
        " FROM model_spread m JOIN run r USING (run_id)"
        f" WHERE r.run_id IN ({DEFAULT_RUNS}) AND r.business_date < %(d)s"
        " UNION ALL"
        " SELECT p.secid, (p.fact->>'spread_bp')::float8, r.business_date, r.run_id, NULL, 0"
        " FROM run_position p JOIN run r USING (run_id)"
        f" WHERE r.run_id IN ({DEFAULT_RUNS}) AND r.business_date < %(d)s"
        "  AND p.quality IN ('exact', 'assumed') AND jsonb_typeof(p.fact->'spread_bp') = 'number'"
        ") x ORDER BY secid, business_date DESC, run_id DESC, pri", {"d": before}).fetchall()
    return {s: CarriedSpread(s, z, d, src or f"last market Z {z:.0f} bp on {d} (run {rid})")
            for s, z, d, rid, src in rows if math.isfinite(z)}


def reap_orphans(con: psycopg.Connection, older_than: timedelta = timedelta(hours=1)) -> list[int]:
    """Runs still loading after ``older_than`` (a crashed or killed publisher) become failed."""
    return [r[0] for r in con.execute(
        "UPDATE run SET status = 'failed', error = 'orphaned: still loading after ' || %s::interval || ', reaped' "
        "WHERE status = 'loading' AND started_at < now() - %s::interval RETURNING run_id",
        (older_than, older_than))]
