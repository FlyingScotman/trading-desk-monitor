"""Desk store (slice S5; ADR-006, 04 §2-§3): PostgreSQL through psycopg 3, plain SQL.

Positions are bitemporal and append-only: a load adds a ``position_set`` per book and valid date
(a correction is the next revision), and ``positions_for`` reads "positions of D as known at T". Since S7a the
sets also come from the inbox (``desk.inbox.scan``, ``store_inbox_set``), ordered by their report key (08 §6.6),
and books from the adapter's ``reference()`` (``store_books``, ``current_books``).
Runs are immutable once complete or failed (``desk.publish.desk_run`` writes them); the triggers of
``migrations/0001_init.sql`` reject any update or delete. Every function here runs its own
transactions: pass a connection from ``connect`` (autocommit).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

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
        conflict = con.execute(
            "SELECT DISTINCT book_id, source FROM position_set WHERE valid_date = %s AND book_id = ANY(%s)"
            " AND source <> %s AND (priority IS NULL OR priority = 0) ORDER BY 1, 2",
            (valid_date, list(by_book), LEGACY_SOURCE)).fetchall()
        if conflict:  # 08 §6.6: load-positions is a source of priority 0; one source per unit
            raise InputError(f"{source_name}: scope_conflict for {valid_date}: "
                             + ", ".join(f"{b} (from {s})" for b, s in conflict))
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

    rows: list[tuple[str, str, str | None, float | None]]
    """(position_id, book_id, secid, quantity), by book then position."""
    set_ids: list[int]
    valid_dates: dict[str, date]
    """Valid date of each book's set: earlier than the run's date when the positions are carried forward."""
    known_at: datetime
    private_inputs_hash: str
    """sha256 over the content of the sets (books and positions, the v1 fields of inbox rows without ``src`` and
    ``x``), not their ids or load times."""
    extra: dict[str, dict] = field(default_factory=dict)
    """S7a: per position_id the private contract v1 fields beyond ``rows`` (08 §10 ``positions``) that the row has:
    asset_class, ccy, face_amount_ccy, face_outstanding_ccy, amount_ccy, value_date, deal_id, source_instrument_id,
    source_instrument_name, isin, regnum, moex_secid, mark_price_pct, accrued_ccy, mv_rub, book_value_rub,
    purchase_*, contract_rate_pct, maturity_date, risk_dv01_rub, risk_cs01_rub, src, x (amounts as float, dates as
    date). A position loaded by
    ``desk load-positions`` has no entry (a bond with secid and quantity). In ``rows`` secid and quantity are None
    when the inbox row has no resolved instrument or gives only a face amount."""
    sources: dict[str, dict] = field(default_factory=dict)
    """S7a: per book the set's origin for lineage: {source, report_date, inbox_file_sha256, file_name}
    (``load-positions`` sets: no report date and no inbox file)."""
    books: dict[str, dict] = field(default_factory=dict)
    """Per book of the sets that has one, its ``book_rev`` in force on the valid date as known at ``known_at``
    (``current_books``: name, desk, book_type, entity, ...): what the run publishes as the book's reference."""


LEGACY_SOURCE = "load-positions"
V1_COLUMNS = ("asset_class", "ccy", "face_amount_ccy", "face_outstanding_ccy", "amount_ccy", "value_date", "deal_id",
              "source_instrument_id", "source_instrument_name", "isin", "regnum", "moex_secid", "mark_price_pct",
              "accrued_ccy", "mv_rub", "book_value_rub", "purchase_date", "purchase_price_pct", "purchase_yield_pct",
              "contract_rate_pct", "maturity_date", "risk_dv01_rub", "risk_cs01_rub")
"""position_input columns of the private contract v1 beyond secid (instrument_id) and quantity."""
CURRENT_SET = ("valid_date DESC, coalesce(priority, 0) DESC, report_date DESC NULLS LAST, report_ts DESC NULLS LAST,"
               " seq DESC NULLS LAST, generated_at DESC NULLS LAST, file_mtime DESC NULLS LAST, recorded_at DESC,"
               " revision DESC")
"""Order of the sets of a book (08 §6.6): the latest valid date, then the higher source priority, then the
greatest report key; a missing part sorts below a present one. Legacy sets have no report key: among themselves
the latest recorded, as before S7a."""


def positions_for(con: psycopg.Connection, valid_date: date, *, known_at: datetime | None = None,
                  books: list[str] | None = None) -> PositionInputs:
    """Per book the set with the latest valid date <= ``valid_date``, of those recorded at or before ``known_at``
    (default now) the current one by ``CURRENT_SET``. A closed book (an empty set) has no rows but its set id
    is listed. known_at is a horizon, not an exact replay (a load committing after it can carry an earlier
    recorded_at): a run's ``position_set_ids`` are the source of truth for its inputs."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    sets = con.execute(
        "SELECT DISTINCT ON (book_id) s.set_id, s.book_id, s.valid_date, s.source, s.report_date,"
        " s.inbox_file_sha256, s.source_name FROM position_set s"
        " WHERE valid_date <= %s AND recorded_at <= %s AND (%s::text[] IS NULL OR book_id = ANY(%s))"
        f" ORDER BY book_id, {CURRENT_SET}", (valid_date, known_at, books, books)).fetchall()
    ids = [s[0] for s in sets]
    cols = ", ".join(f"i.{c}" for c in V1_COLUMNS)
    rows, extra, canon_extra = [], {}, {}
    for pid, book, secid, qty, inbox, *v1, src, x in con.execute(
            f"SELECT i.position_id, s.book_id, i.secid, i.quantity, s.inbox_file_sha256 IS NOT NULL, {cols},"
            " i.src, i.x FROM position_input i JOIN position_set s USING (set_id)"
            " WHERE set_id = ANY(%s) ORDER BY s.book_id, i.position_id", (ids,)):
        rows.append((pid, book, secid, qty))
        if inbox:
            fields = {k: v for k, v in zip(V1_COLUMNS, v1) if v is not None}
            canon_extra[pid] = {k: str(v) for k, v in fields.items()}
            extra[pid] = {k: float(v) if isinstance(v, Decimal) else v for k, v in fields.items()}
            extra[pid].update({k: v for k, v in (("src", src), ("x", x)) if v is not None})
    # legacy inputs hash as before S7a; inbox rows add their canonical v1 fields (not src, not x: 08 §6.6)
    canon = json.dumps([rows, canon_extra] if canon_extra else rows, separators=(",", ":"), ensure_ascii=False,
                       sort_keys=True)
    used = {b for _, b, *_ in sets}
    return PositionInputs(rows, ids, {b: d for _, b, d, *_ in sets}, known_at,  # type: ignore[arg-type]
                          hashlib.sha256(canon.encode()).hexdigest(), extra,
                          {b: {"source": src, "report_date": rd, "inbox_file_sha256": sha, "file_name": name}
                           for _, b, _, src, rd, sha, name in sets},
                          {b["book_id"]: b for b in current_books(con, valid_date, known_at) if b["book_id"] in used})


def store_inbox_set(con: psycopg.Connection, *, book_id: str, valid_date: date, rows: list[dict], source: str,
                    priority: int | None, report_date: date, report_ts: datetime | None, seq: int | None,
                    generated_at: datetime | None, file_mtime: datetime, file_name: str, sha256: str,
                    adapter_fingerprint: str, unit_hash: str) -> int:
    """One accepted positions unit of an inbox file as a position set (revision = the previous one of
    (book, valid_date) + 1). ``rows``: validated contract v1 rows (``desk.inbox.validate``: amounts as decimal
    strings, ``_src``, ``x_*``). Returns the set id. The caller (``desk.inbox.scan``) decides that the unit is a
    new revision, in its own transaction."""
    set_id = con.execute(
        "INSERT INTO position_set (book_id, valid_date, revision, recorded_by, source_name, source_file_sha256,"
        " source, priority, report_date, report_ts, seq, generated_at, file_mtime, inbox_file_sha256,"
        " adapter_fingerprint, unit_hash) VALUES (%(b)s, %(d)s, (SELECT coalesce(max(revision), 0) + 1"
        " FROM position_set WHERE book_id = %(b)s AND valid_date = %(d)s), 'inbox', %(name)s, %(sha)s, %(src)s,"
        " %(prio)s, %(rd)s, %(ts)s, %(seq)s, %(gen)s, %(mtime)s, %(sha)s, %(fp)s, %(uh)s) RETURNING set_id",
        {"b": book_id, "d": valid_date, "name": file_name, "sha": sha256, "src": source, "prio": priority,
         "rd": report_date, "ts": report_ts, "seq": seq, "gen": generated_at, "mtime": file_mtime,
         "fp": adapter_fingerprint, "uh": unit_hash}).fetchone()[0]

    def val(col: str, v):
        if v is None:
            return None
        return date.fromisoformat(v) if col.endswith("_date") else Decimal(v) if col in _NUMERIC else v

    with con.cursor() as cur:
        cur.executemany(
            f"INSERT INTO position_input (set_id, position_id, secid, quantity, {', '.join(V1_COLUMNS)}, src, x)"
            f" VALUES ({', '.join(['%s'] * (len(V1_COLUMNS) + 6))})",
            [(set_id, r["position_id"], r.get("instrument_id"),
              float(r["quantity"]) if r.get("quantity") is not None else None,
              *(val(c, r.get(c)) for c in V1_COLUMNS), r.get("_src"),
              Jsonb(x) if (x := {k: v for k, v in r.items() if k.startswith("x_")}) else None)
             for r in sorted(rows, key=lambda r: r["position_id"])])
    return set_id


_NUMERIC = {"face_amount_ccy", "face_outstanding_ccy", "amount_ccy", "mark_price_pct", "accrued_ccy", "mv_rub",
            "book_value_rub", "purchase_price_pct", "purchase_yield_pct", "contract_rate_pct", "risk_dv01_rub",
            "risk_cs01_rub"}


# --- books (08 §7, §10 books) --------------------------------------------------------------------------

BOOK_FIELDS = ("book_id", "name", "desk", "book_type", "entity", "ocp_to", "trader", "active", "valid_from")


def store_books(con: psycopg.Connection, books: list[dict], adapter_fingerprint: str) -> list[str]:
    """Validated ``reference()["books"]`` rows: a new ``book_rev`` for each book whose content changed.
    Returns the book ids revised. shortcut: a book that reference() stops listing keeps its last revision;
    upgrade when the adapter needs to retire books by omission rather than by ``active = false``."""
    changed = []
    with con.transaction():
        for b in books:
            content = {k: b.get(k) for k in BOOK_FIELDS}
            sha = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
            last = con.execute("SELECT content_sha256 FROM book_rev WHERE book_id = %s ORDER BY rev_id DESC LIMIT 1",
                               (b["book_id"],)).fetchone()
            if last and last[0] == sha:
                continue
            con.execute(f"INSERT INTO book_rev ({', '.join(BOOK_FIELDS)}, adapter_fingerprint, content_sha256)"
                        f" VALUES ({', '.join(['%s'] * (len(BOOK_FIELDS) + 2))})",
                        (*content.values(), adapter_fingerprint, sha))
            changed.append(b["book_id"])
    return changed


def current_books(con: psycopg.Connection, on: date, known_at: datetime | None = None) -> list[dict]:
    """The books in force on ``on`` as known at ``known_at`` (default now), by book id: per book the latest
    revision recorded by then whose ``valid_from`` (else the Moscow day it was recorded) is <= ``on``. A book's
    first revision is in force from the beginning, so a book is never missing for an earlier date."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    cols = ", ".join(BOOK_FIELDS)
    rows = con.execute(
        f"SELECT DISTINCT ON (book_id) {cols} FROM (SELECT *, coalesce(valid_from, CASE WHEN rev_id ="
        " min(rev_id) OVER (PARTITION BY book_id) THEN '-infinity'::date"
        " ELSE (recorded_at AT TIME ZONE 'Europe/Moscow')::date END) AS eff FROM book_rev WHERE recorded_at <= %s) b"
        " WHERE eff <= %s ORDER BY book_id, eff DESC, rev_id DESC", (known_at, on)).fetchall()
    return [{k: v.isoformat() if isinstance(v, date) else v for k, v in zip(BOOK_FIELDS, r) if v is not None}
            for r in rows]


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
