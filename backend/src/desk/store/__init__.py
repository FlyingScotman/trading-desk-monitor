"""Desk store (slice S5; ADR-006, 04 §2-§3): PostgreSQL through psycopg 3, plain SQL.

Positions are bitemporal and append-only: a load adds a ``position_set`` per book and valid date
(a correction is the next revision), and ``positions_for`` reads "positions of D as known at T". Since S7a the
sets also come from the inbox (``desk.inbox.scan``, ``store_inbox_set``), ordered by their report key (08 §6.6),
and books from the adapter's ``reference()`` (``store_books``, ``current_books``).
Runs are immutable once complete or failed (``desk.publish.desk_run`` writes them); the triggers of
``migrations/0001_init.sql`` reject any update or delete, except (0010, R11) the delete of an INTRADAY run that is
no longer the latest of its date, with its rows (``prune_intraday``). Every function here runs its own
transactions: pass a connection from ``connect`` (autocommit).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..inbox.spi import DATASET_KEYS, REFERENCE_KEYS, STOCK_DATASETS  # standard library only, no cycle
from ..settings import REPO_ROOT, Settings, SettingsError
from ..valuation.fallback import CarriedSpread

MIGRATIONS = Path(__file__).parent / "migrations"
DEV_PGDATA = REPO_ROOT / ".devdata" / "desk_pg"  # the dev checkout's Settings.pg_dir (.devdata/pg: hub seed, ADR-006)
MSK = timezone(timedelta(hours=3), "MSK")
# The Moscow day of recorded_at in SQL: a fixed offset, not the named zone Europe/Moscow, which the embedded
# Postgres of the Windows bundle does not know (no time zone data, #155). Moscow has had no DST since 2014. An
# INTERVAL offset is east of UTC (a POSIX string offset such as '-03' would be the other way round).
MSK_DAY = "(recorded_at AT TIME ZONE INTERVAL '+03:00')::date"
_LOCK = 0x6465736B  # "desk": one migrator at a time


def connect(url: str) -> psycopg.Connection:
    return psycopg.connect(url, autocommit=True)


def resolve_url(settings: Settings) -> str:
    """``DESK_STORE_URL``, else the embedded server in ``settings.pg_dir`` (pgserver; ``<home>/pg``, in the dev
    checkout ``.devdata/desk_pg``), database ``desk``. It keeps running after the process exits, so later
    commands share it."""
    if settings.store_url:
        return settings.store_url
    try:
        import pgserver
    except ImportError:
        raise SettingsError("set DESK_STORE_URL (postgresql://...): the embedded dev store needs pgserver, "
                            "which is not installed (uv sync --extra dev)") from None
    settings.pg_dir.parent.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(settings.pg_dir, cleanup_mode=None)
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
    purchase_*, contract_rate_pct, maturity_date, risk_dv01_rub, risk_cs01_rub, purchase_accrued_ccy (1.3), src, x
    (amounts as float, dates as date). A position loaded by ``desk load-positions`` has no entry (a bond with secid
    and quantity). In ``rows`` secid and quantity are None when the inbox row has no resolved instrument or gives
    only a face amount."""
    sources: dict[str, dict] = field(default_factory=dict)
    """S7a: per book the set's origin for lineage: {source, report_date, inbox_file_sha256, file_name}
    (``load-positions`` sets: no report date and no inbox file)."""
    books: dict[str, dict] = field(default_factory=dict)
    """Per book of the sets that has one, its ``book_rev`` in force on the valid date as known at ``known_at``
    (``current_books``: name, desk, book_type, entity, ...): what the run publishes as the book's reference."""
    trade_terms: list[dict] = field(default_factory=list)
    """private/1.2 ``trade_terms`` rows in force on the date for the books of the sets (``dataset_rows``): the FX
    trades ``valuation.derivatives`` values from their terms."""
    trade_terms_units: list[dict] = field(default_factory=list)
    """Where those rows came from (``DatasetInputs.units``), for the run's lineage."""


LEGACY_SOURCE = "load-positions"
V1_COLUMNS = ("asset_class", "ccy", "face_amount_ccy", "face_outstanding_ccy", "amount_ccy", "value_date", "deal_id",
              "source_instrument_id", "source_instrument_name", "isin", "regnum", "moex_secid", "mark_price_pct",
              "accrued_ccy", "mv_rub", "book_value_rub", "purchase_date", "purchase_price_pct", "purchase_yield_pct",
              "contract_rate_pct", "maturity_date", "risk_dv01_rub", "risk_cs01_rub", "purchase_accrued_ccy")
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
    used = {b for _, b, *_ in sets}
    # the FX trades of the books (private/1.2 trade_terms, a stock), valued from their terms (valuation.derivatives)
    terms = dataset_rows(con, "trade_terms", valid_date, known_at=known_at)
    trade_terms = [r for r in terms.rows if str(r.get("book_id")) in used]
    canon_terms = sorted(json.dumps({k: v for k, v in r.items() if not k.startswith("_") and k != "x"},
                                     sort_keys=True, default=str) for r in trade_terms)
    # legacy inputs hash as before S7a; inbox rows add their canonical v1 fields (not src, not x: 08 §6.6); the
    # trades' terms only when there are any (books without them keep their hash)
    parts: Any = ([rows, canon_extra] if canon_extra else rows) if not canon_terms else [rows, canon_extra, canon_terms]
    canon = json.dumps(parts, separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return PositionInputs(rows, ids, {b: d for _, b, d, *_ in sets}, known_at,  # type: ignore[arg-type]
                          hashlib.sha256(canon.encode()).hexdigest(), extra,
                          {b: {"source": src, "report_date": rd, "inbox_file_sha256": sha, "file_name": name}
                           for _, b, _, src, rd, sha, name in sets},
                          {b["book_id"]: b for b in current_books(con, valid_date, known_at) if b["book_id"] in used},
                          trade_terms,
                          [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in u.items()}
                           for u in terms.units if str((u["scope"] or {}).get("book_id")) in used])


def current_sets(con: psycopg.Connection, valid_date: date) -> tuple[list[int], int]:
    """The set ids ``positions_for(valid_date)`` reads now (sorted) and their number of rows, without the rows:
    the autopilot compares them with a run's ``position_set_ids`` every cycle."""
    ids, n = con.execute(
        "WITH s AS (SELECT DISTINCT ON (book_id) set_id FROM position_set WHERE valid_date <= %s"
        f" ORDER BY book_id, {CURRENT_SET}) SELECT coalesce(array_agg(set_id ORDER BY set_id), '{{}}'),"
        " (SELECT count(*) FROM position_input WHERE set_id IN (SELECT set_id FROM s)) FROM s",
        (valid_date,)).fetchone()
    return list(ids), n


def position_dates(con: psycopg.Connection, since: date) -> list[date]:
    """Valid dates of the position sets from ``since`` on, plus the latest one before it (carried forward)."""
    return [d for (d,) in con.execute(
        "SELECT DISTINCT valid_date FROM position_set WHERE valid_date >= %(d)s UNION"
        " SELECT max(valid_date) FROM position_set WHERE valid_date < %(d)s ORDER BY 1", {"d": since}) if d]


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
            "risk_cs01_rub", "purchase_accrued_ccy"}


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
        f" ELSE {MSK_DAY} END) AS eff FROM book_rev WHERE recorded_at <= %s) b"
        " WHERE eff <= %s ORDER BY book_id, eff DESC, rev_id DESC", (known_at, on)).fetchall()
    return [{k: v.isoformat() if isinstance(v, date) else v for k, v in zip(BOOK_FIELDS, r) if v is not None}
            for r in rows]


# --- datasets beyond positions (08 §10, private/1.2: ocp, cash_balances, pnl_official, trade_terms) ----------

def scope_key(scope: dict) -> str:
    """The canonical text of a scope unit: what the revisions of a dataset unit are grouped by."""
    return json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def dataset_unit_sets(con: psycopg.Connection, dataset: str, scope: dict, valid_date: date) -> list[tuple]:
    """The sets of one unit (dataset, scope unit, valid date), current first (``CURRENT_SET``): (set_id, source,
    priority, report_date, report_ts, seq, generated_at, file_mtime, unit_hash), the shape ``desk.inbox.scan``
    decides revisions on, as for ``position_set``."""
    return con.execute(
        "SELECT set_id, source, priority, report_date, report_ts, seq, generated_at, file_mtime, unit_hash"
        f" FROM dataset_set WHERE dataset = %s AND scope_key = %s AND valid_date = %s ORDER BY {CURRENT_SET}",
        (dataset, scope_key(scope), valid_date)).fetchall()


def store_dataset_set(con: psycopg.Connection, *, dataset: str, scope: dict, valid_date: date, rows: list[dict],
                      source: str, priority: int | None, report_date: date, report_ts: datetime | None,
                      seq: int | None, generated_at: datetime | None, file_mtime: datetime, file_name: str,
                      sha256: str, adapter_fingerprint: str, unit_hash: str) -> int:
    """One accepted unit of a dataset other than positions as a ``dataset_set`` (revision = the previous one of the
    unit + 1) with its rows. ``rows``: validated contract rows (``desk.inbox.validate``); ``_src`` and ``x_*`` are
    kept beside the canonical fields. Returns the set id. The caller decides that the unit is a new revision."""
    sk = scope_key(scope)
    set_id = con.execute(
        "INSERT INTO dataset_set (dataset, scope, scope_key, valid_date, revision, source, priority, report_date,"
        " report_ts, seq, generated_at, file_mtime, inbox_file_sha256, source_name, adapter_fingerprint, unit_hash,"
        " row_count) VALUES (%(ds)s, %(scope)s, %(sk)s, %(d)s, (SELECT coalesce(max(revision), 0) + 1 FROM dataset_set"
        " WHERE dataset = %(ds)s AND scope_key = %(sk)s AND valid_date = %(d)s), %(src)s, %(prio)s, %(rd)s, %(ts)s,"
        " %(seq)s, %(gen)s, %(mtime)s, %(sha)s, %(name)s, %(fp)s, %(uh)s, %(n)s) RETURNING set_id",
        {"ds": dataset, "scope": Jsonb(scope), "sk": sk, "d": valid_date, "src": source, "prio": priority,
         "rd": report_date, "ts": report_ts, "seq": seq, "gen": generated_at, "mtime": file_mtime, "sha": sha256,
         "name": file_name, "fp": adapter_fingerprint, "uh": unit_hash, "n": len(rows)}).fetchone()[0]
    keys = DATASET_KEYS.get(dataset, ())

    def row_key(r: dict) -> str:
        return "|".join("" if r.get(k) is None else str(r[k]) for k in keys)

    ordered = sorted(rows, key=lambda r: (row_key(r), json.dumps(r, sort_keys=True, default=str)))
    with con.cursor() as cur:
        cur.executemany(
            "INSERT INTO dataset_row (set_id, row_no, row_key, data, src, x) VALUES (%s, %s, %s, %s, %s, %s)",
            [(set_id, n, row_key(r), Jsonb({k: v for k, v in r.items() if not k.startswith(("_", "x_"))}),
              r.get("_src"), Jsonb(x) if (x := {k: v for k, v in r.items() if k.startswith("x_")}) else None)
             for n, r in enumerate(ordered, 1)])
    return set_id


@dataclass
class DatasetInputs:
    """``dataset_rows``: the rows of a dataset in force on a date, and per unit where they came from."""

    dataset: str
    on: date
    rows: list[dict]
    """Canonical fields as stored (amounts as decimal strings), plus ``_src``, ``x`` (the x_* fields, if any),
    ``_valid_date`` and ``_set_id`` of the unit the row belongs to."""
    units: list[dict]
    """Per unit read: {scope, valid_date, set_id, source, report_date, inbox_file_sha256, file_name, row_count}; a
    unit whose current set is empty (closed by a full snapshot) is listed with row_count 0."""


def dataset_rows(con: psycopg.Connection, dataset: str, on: date, *, known_at: datetime | None = None,
                 scope: dict | None = None) -> DatasetInputs:
    """The rows of ``dataset`` for business date ``on`` as known at ``known_at`` (default now), 08 §6.7: a stock
    (``spi.STOCK_DATASETS``: ocp, cash_balances, trade_terms) takes per unit the current set with the latest valid
    date <= ``on``; a flow (pnl_official) only units of valid date ``on``, never carried forward. ``scope``: only
    units whose scope contains these key/values (``{"book_id": "B1"}``)."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    when = "valid_date <= %(on)s" if dataset in STOCK_DATASETS else "valid_date = %(on)s"
    sets = con.execute(
        "SELECT DISTINCT ON (scope_key) set_id, scope, valid_date, source, report_date, inbox_file_sha256,"
        f" source_name, row_count FROM dataset_set WHERE dataset = %(ds)s AND {when} AND recorded_at <= %(k)s"
        " AND (%(scope)s::jsonb IS NULL OR scope @> %(scope)s::jsonb)"
        f" ORDER BY scope_key, {CURRENT_SET}",
        {"ds": dataset, "on": on, "k": known_at, "scope": Jsonb(scope) if scope else None}).fetchall()
    units = [{"scope": sc, "valid_date": vd, "set_id": sid, "source": src, "report_date": rd,
              "inbox_file_sha256": sha, "file_name": name, "row_count": n}
             for sid, sc, vd, src, rd, sha, name, n in sets]
    by_set = {u["set_id"]: u for u in units}
    rows = []
    for sid, data, src, x in con.execute(
            "SELECT set_id, data, src, x FROM dataset_row WHERE set_id = ANY(%s) ORDER BY set_id, row_no",
            (list(by_set),)):
        rows.append({**data, "_src": src, **({"x": x} if x else {}), "_valid_date": by_set[sid]["valid_date"],
                     "_set_id": sid})
    return DatasetInputs(dataset, on, rows, units)


# --- reference tables beyond books (08 §7, §10: entities, funding_rules) ---------------------------------

def store_reference(con: psycopg.Connection, kind: str, rows: list[dict], adapter_fingerprint: str) -> list[str]:
    """Validated ``reference()[kind]`` rows (``validate.check_reference_tables``): a new ``reference_rev`` for each
    (key, valid_from) whose content changed. Returns the keys revised. shortcut: as for books, a row reference()
    stops listing keeps its last revision; retire it with ``active = false``."""
    changed = []
    with con.transaction():
        for r in rows:
            content = {k: v for k, v in r.items() if not k.startswith("_")}
            key = "|".join(str(r[k]) for k in REFERENCE_KEYS[kind])
            sha = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
            last = con.execute(
                "SELECT content_sha256 FROM reference_rev WHERE kind = %s AND ref_key = %s"
                " AND valid_from IS NOT DISTINCT FROM %s ORDER BY rev_id DESC LIMIT 1",
                (kind, key, r.get("valid_from"))).fetchone()
            if last and last[0] == sha:
                continue
            con.execute("INSERT INTO reference_rev (kind, ref_key, valid_from, active, content, adapter_fingerprint,"
                        " content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (kind, key, r.get("valid_from"), r.get("active", True), Jsonb(content), adapter_fingerprint,
                         sha))
            changed.append(key)
    return changed


def current_reference(con: psycopg.Connection, kind: str, on: date, known_at: datetime | None = None) -> list[dict]:
    """The rows of a reference table in force on ``on`` as known at ``known_at`` (default now), by key: per key the
    revision recorded by then with the greatest effective date <= ``on`` (its ``valid_from``, else the Moscow day it
    was recorded; the first revision of a key: always), the latest on a tie. Retired rows (``active`` false) are
    returned too, as ``current_books`` does."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    rows = con.execute(
        "SELECT DISTINCT ON (ref_key) content FROM (SELECT *, coalesce(valid_from, CASE WHEN rev_id ="
        " min(rev_id) OVER (PARTITION BY ref_key) THEN '-infinity'::date"
        f" ELSE {MSK_DAY} END) AS eff FROM reference_rev"
        " WHERE kind = %s AND recorded_at <= %s) r WHERE eff <= %s ORDER BY ref_key, eff DESC, rev_id DESC",
        (kind, known_at, on)).fetchall()
    return [c for (c,) in rows]


# --- runs ----------------------------------------------------------------------------------------------

# The default run of a date (04 §3): the latest complete EOD run on eod_final, otherwise on eod_preliminary.
DEFAULT_RUNS = ("SELECT DISTINCT ON (business_date) run_id FROM run"
                " WHERE status = 'complete' AND run_type = 'EOD'"
                " AND cut_type IN ('eod_final', 'eod_preliminary')"
                " ORDER BY business_date, cut_type = 'eod_final' DESC, completed_at DESC, run_id DESC")


# The run shown for a date (R11): its default run, otherwise its latest complete INTRADAY run (today's book on the
# hub's latest intraday cut until the evening's preliminary close). The newest of these is the run pages open on.
CURRENT_RUNS = ("SELECT DISTINCT ON (business_date) run_id FROM run"
                " WHERE status = 'complete' AND (run_type = 'EOD' AND cut_type IN ('eod_final', 'eod_preliminary')"
                " OR run_type = 'INTRADAY')"
                " ORDER BY business_date, run_type = 'EOD' DESC, cut_type = 'eod_final' DESC, completed_at DESC,"
                " run_id DESC")

CUT_LABELS = {"intraday": "внутри дня", "eod_preliminary": "закрытие предв.", "eod_final": "закрытие",
              "prev_close": "закрытие пред. дня"}


def slice_label(business_date: date | str, cut_type: str | None, cut_ts: datetime | str | None = None,
                run_type: str | None = None, market_close: dict[str, str] | None = None) -> str:
    """The run's market slice as the pages name it: «20.10 14:30 МСК · внутри дня», «19.10 · закрытие предв.»,
    «19.10 · закрытие»; a TEST or REPLAY run adds its type. The morning run (cut ``prev_close``, #21: the book of
    the date on the previous close, lineage ``market_close`` {date, cut_type}): «20.10 · закрытие 19.10»."""
    d = date.fromisoformat(business_date) if isinstance(business_date, str) else business_date
    day = f"{d:%d.%m}"
    if cut_type == "prev_close" and market_close and market_close.get("date"):
        md = date.fromisoformat(market_close["date"])
        label = f"{day} · {CUT_LABELS.get(market_close.get('cut_type') or '', 'закрытие')} {md:%d.%m}"
        return label + (f" · {run_type}" if run_type in ("TEST", "REPLAY") else "")
    if cut_type == "intraday" and cut_ts:
        ts = datetime.fromisoformat(cut_ts) if isinstance(cut_ts, str) else cut_ts
        day += f" {ts.astimezone(MSK):%H:%M} МСК"
    label = f"{day} · {CUT_LABELS.get(cut_type or '', cut_type or '?')}"
    return label + (f" · {run_type}" if run_type in ("TEST", "REPLAY") else "")


RUN_ROW_TABLES = ("run_position", "run_reference", "run_log", "run_core_result", "model_spread", "run_summary")


def prune_intraday(con: psycopg.Connection, business_date: date, keep_run_id: int) -> list[int]:
    """Delete the INTRADAY runs of ``business_date`` other than ``keep_run_id`` (complete or failed) with their rows:
    one intraday run per date stays (R11). A run a scenario job was submitted on is kept. Returns the ids deleted."""
    with con.transaction():
        ids = [r for (r,) in con.execute(
            "SELECT run_id FROM run r WHERE run_type = 'INTRADAY' AND business_date = %s AND run_id <> %s"
            " AND status <> 'loading' AND NOT EXISTS (SELECT 1 FROM scenario_job j WHERE j.base_run_id = r.run_id)"
            " ORDER BY run_id", (business_date, keep_run_id))]
        if ids:
            for table in RUN_ROW_TABLES:
                con.execute("DELETE FROM " + table + " WHERE run_id = ANY(%s)", (ids,))
            con.execute("DELETE FROM run WHERE run_id = ANY(%s)", (ids,))
    return ids


def default_runs(con: psycopg.Connection, since: date) -> dict[date, dict]:
    """The default run of each business date from ``since`` on: run_id, its snapshots, position sets (sorted),
    completed_at, private inputs hash and the ``logic_version`` of the code that made it (lineage; None for a run
    made before it was recorded)."""
    return {r[0]: {"run_id": r[1], "market_snapshot_id": r[2], "prev_market_snapshot_id": r[3],
                   "position_set_ids": sorted(r[4]), "completed_at": r[5], "logic_version": r[6],
                   "private_inputs_hash": r[7]}
            for r in con.execute(
                "SELECT business_date, run_id, market_snapshot_id, prev_market_snapshot_id, position_set_ids,"
                " completed_at, lineage->>'logic_version', private_inputs_hash FROM run"
                f" WHERE run_id IN ({DEFAULT_RUNS}) AND business_date >= %s", (since,))}


def carried_spreads(con: psycopg.Connection, before: date) -> dict[str, CarriedSpread]:
    """Per secid the latest Z-spread of the default runs (``DEFAULT_RUNS``) before ``before``: the model
    spread a model price used, or the bond's last market Z (exact or assumed), whichever run is later.
    TEST, INTRADAY, REPLAY and superseded runs never supply one. ``curve``: the curve the spread is to, the
    ``spread_curve`` of the bond's fact in that run (``RUB OFZ``, the KBD, for a run before the RUONIA switch)."""
    curve = "COALESCE(p.fact->>'spread_curve', 'RUB OFZ')"
    rows = con.execute(
        "SELECT DISTINCT ON (secid) secid, z, business_date, run_id, source, curve FROM ("
        " SELECT m.secid, m.z_spread_bp AS z, r.business_date, r.run_id, m.source, 1 AS pri,"
        f"  (SELECT {curve} FROM run_position p WHERE p.run_id = m.run_id AND p.secid = m.secid LIMIT 1) AS curve"
        " FROM model_spread m JOIN run r USING (run_id)"
        f" WHERE r.run_id IN ({DEFAULT_RUNS}) AND r.business_date < %(d)s"
        " UNION ALL"
        f" SELECT p.secid, (p.fact->>'spread_bp')::float8, r.business_date, r.run_id, NULL, 0, {curve}"
        " FROM run_position p JOIN run r USING (run_id)"
        f" WHERE r.run_id IN ({DEFAULT_RUNS}) AND r.business_date < %(d)s"
        "  AND p.quality IN ('exact', 'assumed') AND jsonb_typeof(p.fact->'spread_bp') = 'number'"
        ") x ORDER BY secid, business_date DESC, run_id DESC, pri", {"d": before}).fetchall()
    return {s: CarriedSpread(s, z, d, src or f"last market Z {z:.0f} bp on {d} (run {rid})", c or "RUB OFZ")
            for s, z, d, rid, src, c in rows if math.isfinite(z)}


RUN_EVENT_SQL = ("INSERT INTO event (type, valid_date, run_id, detail)"
                 " SELECT %s, business_date, run_id, jsonb_build_object('run_type', run_type, 'cut_type', cut_type,"
                 " 'supersedes_run_id', supersedes_run_id, 'cut_ts', cut_ts) FROM run WHERE run_id = ANY(%s)")


def run_event(con: psycopg.Connection | psycopg.Cursor, type_: str, run_ids: list[int]) -> None:
    """The feed's event of runs (migration 0019, ``desk.daystatus``): ``run.published`` when a run completes (in its
    transaction), ``run.failed`` when it fails or is reaped; the run's date, type, cut and the run it supersedes."""
    if run_ids:
        con.execute(RUN_EVENT_SQL, (type_, list(run_ids)))


def reap_orphans(con: psycopg.Connection, older_than: timedelta = timedelta(hours=1)) -> list[int]:
    """Runs still loading after ``older_than`` (a crashed or killed publisher) become failed."""
    ids = [r[0] for r in con.execute(
        "UPDATE run SET status = 'failed', error = 'orphaned: still loading after ' || %s::interval || ', reaped' "
        "WHERE status = 'loading' AND started_at < now() - %s::interval RETURNING run_id",
        (older_than, older_than))]
    run_event(con, "run.failed", ids)
    return ids
