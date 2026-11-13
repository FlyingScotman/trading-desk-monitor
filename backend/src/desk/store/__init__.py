"""Desk store (slice S5; ADR-006, 04 §2-§3): PostgreSQL through psycopg 3, plain SQL.

Positions are bitemporal and append-only: a load adds a ``position_set`` per book and valid date
(a correction is the next revision), and ``positions_for`` reads "positions of D as known at T". Since S7a the
sets also come from the inbox (``desk.inbox.scan``, ``store_inbox_set``), ordered by their report key (08 §6.6),
and books from the adapter's ``reference()`` (``store_books``, ``current_books``).
Runs are immutable once complete or failed (``desk.publish.desk_run`` writes them); the triggers of
``migrations/0001_init.sql`` reject any update or delete, except (0010, R11) the delete of an INTRADAY run that is
no longer the latest of its date (an hour after its replacement), with its rows (``prune_intraday``). Every function here runs its own
transactions: pass a connection from ``connect`` (autocommit).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from ..inbox.schema import alias_position, alias_row, ccy_fields
from ..inbox.spi import (  # stdlib only, no cycle
    DATASET_KEYS,
    REFERENCE_KEYS,
    STOCK_DATASETS,
    VERSIONED_DATASETS,
)
from ..run_cache import forget_runs  # the per-run API's finished bodies go with their runs (TDM#631)
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


def connect(url: str, connect_timeout: float | None = None) -> psycopg.Connection:
    """A connection (autocommit). ``connect_timeout`` (seconds): for a caller that treats "no store" as an answer and must
    not stand in a connect that never ends (Windows keeps a connection to a port nobody listens on for seconds)."""
    if connect_timeout is None:
        return psycopg.connect(url, autocommit=True)
    return psycopg.connect(url, autocommit=True, connect_timeout=max(1, round(connect_timeout)))


def resolve_url(settings: Settings) -> str:
    """``DESK_STORE_URL``, else the embedded server in ``settings.pg_dir`` (pgserver; ``<home>/pg``, in the dev
    checkout ``.devdata/desk_pg``), database ``desk``. It keeps running after the process exits, so later
    commands share it. A server that was stopped in an emergency recovers first, which takes minutes: this waits for
    it, saying so on the console, and a start that cannot succeed raises ``embedded.StoreStartError`` (a
    ``SettingsError``) with the tail of the server's log (``desk.store.embedded``)."""
    if settings.store_url:
        return settings.store_url
    try:
        import pgserver  # noqa: F401 - embedded.get_server imports it again; this is the message for a missing one
    except ImportError:
        raise SettingsError("set DESK_STORE_URL (postgresql://...): the embedded dev store needs pgserver, "
                            "which is not installed (uv sync --extra dev)") from None
    settings.pg_dir.parent.mkdir(parents=True, exist_ok=True)
    from . import embedded  # pgserver's start with the recovery of a crashed cluster (TDM#518)

    server = embedded.get_server(settings.pg_dir)
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
    """rows: (position_id, book_id, secid, quantity), as ``bookfile.read_book`` returns them. The load covers
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
    other_book_terms: list[dict] = field(default_factory=list)
    """TDM#468: the ``trade_terms`` rows of the books that have no positions set but are in the books reference and not
    switched off (``active``). Always loaded, never in ``private_inputs_hash``: only the setting
    ``off_report_derivatives`` = ``include`` values them (``valuation.offreport``), and then the hash says so
    (``offreport.stamp``)."""
    other_book_units: list[dict] = field(default_factory=list)
    """Where those rows came from, as ``trade_terms_units``."""
    other_books: dict[str, dict] = field(default_factory=dict)
    """The books reference (``current_books``) of the books of ``other_book_terms``."""


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
            alias_position(fields)  # TDM#601: CNH is CNY; the report's code stays as ccy_reported
            canon_extra[pid] = {k: str(v) for k, v in fields.items()}
            extra[pid] = {k: float(v) if isinstance(v, Decimal) else v for k, v in fields.items()}
            extra[pid].update({k: v for k, v in (("src", src), ("x", x)) if v is not None})
    used = {b for _, b, *_ in sets}
    # the FX trades of the books (private/1.2 trade_terms, a stock), valued from their terms (valuation.derivatives)
    terms = dataset_rows(con, "trade_terms", valid_date, known_at=known_at)
    trade_terms = [r for r in terms.rows if str(r.get("book_id")) in used]
    ref = {b["book_id"]: b for b in current_books(con, valid_date, known_at)}
    # TDM#468: the books with trade terms but no positions set, that the books reference lists and has not switched off
    other = {str(r.get("book_id")) for r in terms.rows} - used
    other = {b for b in other if b in ref and ref[b].get("active") is not False and (books is None or b in books)}
    other_terms = [r for r in terms.rows if str(r.get("book_id")) in other]
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
                          {b: r for b, r in ref.items() if b in used},
                          trade_terms,
                          [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in u.items()}
                           for u in terms.units if str((u["scope"] or {}).get("book_id")) in used],
                          other_terms,
                          [{k: (v.isoformat() if isinstance(v, date) else v) for k, v in u.items()}
                           for u in terms.units if str((u["scope"] or {}).get("book_id")) in other],
                          {b: r for b, r in ref.items() if b in other})


def current_sets(con: psycopg.Connection, valid_date: date) -> tuple[list[int], int]:
    """The set ids ``positions_for(valid_date)`` reads now (sorted) and their number of rows, without the rows:
    the autopilot compares them with a run's ``position_set_ids`` every cycle."""
    ids, n = con.execute(
        "WITH s AS (SELECT DISTINCT ON (book_id) set_id FROM position_set WHERE valid_date <= %s"
        f" ORDER BY book_id, {CURRENT_SET}) SELECT coalesce(array_agg(set_id ORDER BY set_id), '{{}}'),"
        " (SELECT count(*) FROM position_input WHERE set_id IN (SELECT set_id FROM s)) FROM s",
        (valid_date,)).fetchone()
    return list(ids), n


LATE_DATASETS = {"ocp": "ОВП", "cash_balances": "остатки"}
"""Stocks a run reads besides positions and trade terms (the OCP report, the cash balances: ``publish.ocp_run``,
``publish.funding_run``) that usually arrive in the evening, after the run (TDM#181) -> their name in the day status."""


def dataset_set_ids(con: psycopg.Connection, valid_date: date, datasets: Iterable[str] = tuple(LATE_DATASETS), *,
                    known_at: datetime | None = None) -> dict[str, list[int]]:
    """Per dataset (a stock) the set ids ``dataset_rows(dataset, valid_date, known_at=known_at)`` reads (sorted),
    without the rows; a dataset with none is absent."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    return {ds: list(ids) for ds, ids in con.execute(
        "SELECT dataset, array_agg(set_id ORDER BY set_id) FROM (SELECT DISTINCT ON (dataset, scope_key) dataset,"
        " set_id FROM dataset_set WHERE dataset = ANY(%s) AND valid_date <= %s AND recorded_at <= %s"
        f" ORDER BY dataset, scope_key, {CURRENT_SET}) s GROUP BY dataset",
        (list(datasets), valid_date, known_at))}


def late_inputs(con: psycopg.Connection, valid_date: date, known_at: datetime | None,
                revs: ReferenceRevs | None = None) -> list[str]:
    """The ``LATE_DATASETS`` whose sets in force on ``valid_date`` now differ from those a run that read its inputs at
    ``known_at`` (its ``positions_known_at``) saw: an OCP report or balances recorded (or corrected) after the run.
    The autopilot and the backfill run the date again, the day status names them (TDM#181)."""
    if known_at is None:
        return []
    now, then = dataset_set_ids(con, valid_date), dataset_set_ids(con, valid_date, known_at=known_at)
    return [ds for ds in LATE_DATASETS if now.get(ds, []) != then.get(ds, [])] + late_reference(con, known_at, revs)


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
                      sha256: str, adapter_fingerprint: str, unit_hash: str, completeness: str | None = None) -> int:
    """One accepted unit of a dataset other than positions as a ``dataset_set`` (revision = the previous one of the
    unit + 1) with its rows. ``rows``: validated contract rows (``desk.inbox.validate``); ``_src`` and ``x_*`` are
    kept beside the canonical fields. ``completeness``: the source's (08 §4), what a read of a ``VERSIONED_DATASETS``
    dataset needs (an ``increment`` set adds, a ``full_snapshot`` one replaces; None reads as a full snapshot).
    Returns the set id. The caller decides that the unit is a new revision."""
    sk = scope_key(scope)
    set_id = con.execute(
        "INSERT INTO dataset_set (dataset, scope, scope_key, valid_date, revision, source, priority, report_date,"
        " report_ts, seq, generated_at, file_mtime, inbox_file_sha256, source_name, adapter_fingerprint, unit_hash,"
        " row_count, completeness) VALUES (%(ds)s, %(scope)s, %(sk)s, %(d)s, (SELECT coalesce(max(revision), 0) + 1"
        " FROM dataset_set WHERE dataset = %(ds)s AND scope_key = %(sk)s AND valid_date = %(d)s), %(src)s, %(prio)s,"
        " %(rd)s, %(ts)s, %(seq)s, %(gen)s, %(mtime)s, %(sha)s, %(name)s, %(fp)s, %(uh)s, %(n)s, %(cpl)s)"
        " RETURNING set_id",
        {"ds": dataset, "scope": Jsonb(scope), "sk": sk, "d": valid_date, "src": source, "prio": priority,
         "rd": report_date, "ts": report_ts, "seq": seq, "gen": generated_at, "mtime": file_mtime, "sha": sha256,
         "name": file_name, "fp": adapter_fingerprint, "uh": unit_hash, "n": len(rows),
         "cpl": completeness}).fetchone()[0]
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


def retire_old_trades(con: psycopg.Connection, *, valid_date: date, source: str, up_to_set_id: int,
                      keep: Iterable[str], adapter_fingerprint: str) -> int:
    """The trades of ``valid_date`` replaced whole by an answer (TDM#715): every unit (book) of the date whose current set
    is of ``source``, is not newer than ``up_to_set_id`` (a set of the adapter whose query had the window of the day
    before) and has no records in the answer (its ``scope_key`` is not in ``keep``) gets a new revision with no rows, a full
    snapshot, with the report key of the set it replaces and a later ``recorded_at``: so it is the unit's current set, the
    date holds what the answer holds, and the older revisions stay (nothing is deleted). A unit already empty, one of
    another source, one recorded after ``up_to_set_id`` is left alone, so the call is made again at no cost. Returns the
    number of units emptied. The caller holds the cycle lock."""
    from ..inbox.validate import (
        unit_hash,  # at call time: validate imports the inbox schema, which imports the store
    )

    keep = set(keep)
    rows = con.execute(
        "SELECT DISTINCT ON (scope_key) set_id, scope, scope_key, source, priority, report_date, report_ts, seq,"
        " generated_at, file_mtime, inbox_file_sha256, source_name, row_count, coalesce(completeness, 'full_snapshot')"
        f" FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s ORDER BY scope_key, {CURRENT_SET}",
        (valid_date,)).fetchall()
    n = 0
    for sid, scope, sk, src, prio, rd, ts, seq, gen, mtime, sha, name, cnt, cpl in rows:
        if sk in keep or src != source or sid > up_to_set_id or (cnt == 0 and cpl == "full_snapshot"):
            continue
        store_dataset_set(con, dataset="trades", scope=scope, valid_date=valid_date, rows=[], source=src, priority=prio,
                          report_date=rd, report_ts=ts, seq=seq, generated_at=gen, file_mtime=mtime, file_name=name,
                          sha256=sha, adapter_fingerprint=adapter_fingerprint, unit_hash=unit_hash([]),
                          completeness="full_snapshot")
        n += 1
    return n


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
    date <= ``on``; a flow (pnl_official, trades) only units of valid date ``on``, never carried forward. ``scope``:
    only units whose scope contains these key/values (``{"book_id": "B1"}``); for ``trades`` (``versioned_rows``)
    only rows with these values."""
    if dataset in VERSIONED_DATASETS:
        return versioned_rows(con, dataset, on, known_at=known_at, where=scope)
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
        alias_row(data, ccy_fields(dataset))  # TDM#601: CNH is CNY; the report's codes in _ccy_reported
        rows.append({**data, "_src": src, **({"x": x} if x else {}), "_valid_date": by_set[sid]["valid_date"],
                     "_set_id": sid})
    return DatasetInputs(dataset, on, rows, units)


def versioned_rows(con: psycopg.Connection, dataset: str, on: date, *, known_at: datetime | None = None,
                   where: dict | None = None) -> DatasetInputs:
    """The rows in force on ``on`` of a dataset of ``spi.VERSIONED_DATASETS`` (``trades``, private/1.6, 08 §10): a flow,
    valid date ``on`` only, as known at ``known_at`` (default now).

    Per unit (scope unit, date) the sets of the source of its current set (``CURRENT_SET``: a higher priority, then the
    later report key) are read from the newest back to its newest full snapshot (a cumulative export replaces the
    unit's earlier sets), every ``increment`` set newer than that snapshot added. Then, across the date's units (a
    trade amended into another book moves), each record key keeps the row of its highest integer version; an equal or
    absent version: the row of the newer set. A cancelled trade stays, with its status; a late booking (``trade_date``
    before the date) is a trade of the date like any other. ``where``: only the current rows with these field values
    (``{"book_id": "B1"}``), applied after the merge. ``units`` lists the sets read, with ``completeness``."""
    key_field, version_field = VERSIONED_DATASETS[dataset]
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    sets = con.execute(  # newest first within a unit; age: the set's place in the date's CURRENT_SET order
        "SELECT set_id, scope_key, scope, valid_date, source, report_date, report_ts, inbox_file_sha256, source_name,"
        f" row_count, coalesce(completeness, 'full_snapshot'), row_number() OVER (ORDER BY {CURRENT_SET}) AS age"
        f" FROM dataset_set WHERE dataset = %s AND valid_date = %s AND recorded_at <= %s"
        f" ORDER BY scope_key, {CURRENT_SET}", (dataset, on, known_at)).fetchall()
    read: list[tuple] = []
    unit, unit_source, done = None, None, False
    for sid, sk, sc, vd, src, rd, ts, sha, name, cnt, cpl, age in sets:
        if sk != unit:
            unit, unit_source, done = sk, src, False
        if done or src != unit_source:
            continue
        read.append((sid, sc, vd, src, rd, ts, sha, name, cnt, cpl, age))
        done = cpl != "increment"
    units = [{"scope": sc, "valid_date": vd, "set_id": sid, "source": src, "report_date": rd, "report_ts": ts,
              "inbox_file_sha256": sha, "file_name": name, "row_count": cnt, "completeness": cpl}
             for sid, sc, vd, src, rd, ts, sha, name, cnt, cpl, _ in read]
    by_set, age_of = {u["set_id"]: u for u in units}, {r[0]: r[-1] for r in read}
    current: dict[str, tuple[tuple, dict]] = {}
    for sid, data, src, x in con.execute(
            "SELECT set_id, data, src, x FROM dataset_row WHERE set_id = ANY(%s) ORDER BY set_id, row_no",
            (list(by_set),)):
        v = data.get(version_field)
        rank = (-1 if v is None else int(v), -age_of[sid])
        k = str(data.get(key_field))
        if k not in current or rank > current[k][0]:
            alias_row(data, ccy_fields(dataset))  # TDM#601, as dataset_rows
            current[k] = (rank, {**data, "_src": src, **({"x": x} if x else {}), "_valid_date": by_set[sid]["valid_date"],
                                 "_set_id": sid})
    rows = [r for _, r in current.values() if not where or all(r.get(f) == v for f, v in where.items())]
    rows.sort(key=lambda r: (str(r.get("book_id")), str(r.get("trade_ts") or ""), str(r.get(key_field))))
    return DatasetInputs(dataset, on, rows, units)


# --- reference tables beyond books (08 §7, §10: entities, funding_rules) ---------------------------------

LATE_REFERENCE = {"ocp_policy": "политика ОВП"}
"""Policy tables a run reads for every date, whose change recomputes all dates -> their name (TDM#567). They are
stored as a snapshot of the adapter's delivery (``store_reference``)."""


def store_reference(con: psycopg.Connection, kind: str, rows: list[dict], adapter_fingerprint: str) -> list[str]:
    """Validated ``reference()[kind]`` rows (``validate.check_reference_tables``): a new ``reference_rev`` for each
    (key, valid_from) whose content changed. Returns the keys revised. shortcut: as for books, a row reference()
    stops listing keeps its last revision; retire it with ``active = false``.

    A ``LATE_REFERENCE`` kind is a **snapshot**: the rows of one call are one delivery. When the set of (key,
    valid_from, content) of the delivery equals that of the last recorded delivery, nothing is written; otherwise
    every row is written, all with one ``clock_timestamp()`` taken before the inserts as ``recorded_at`` (the
    delivery's identity), and the read takes the keys of the last delivery, so a row the adapter no longer lists
    stops being in force and a return to an older content is a new delivery. A row of a validated delivery that the
    validation left out (an error) is not in the snapshot: its key stops being in force. Two calls for one kind in
    one transaction get different ``clock_timestamp()`` values, so they stay two deliveries. An empty list is no delivery (the adapter lists nothing: the stored policy is
    kept; the owner switches a row off with ``active = false``)."""
    changed = []
    with con.transaction():
        snapshot = kind in LATE_REFERENCE
        if snapshot and not rows:
            return []
        items = []
        for r in rows:
            content = {k: v for k, v in r.items() if not k.startswith("_")}
            key = "|".join(str(r[k]) for k in REFERENCE_KEYS[kind])
            sha = hashlib.sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()
            items.append((r, content, key, sha))
        if snapshot:
            last = {(k, None if v is None else str(v), h) for k, v, h in con.execute(
                "SELECT ref_key, valid_from, content_sha256 FROM reference_rev WHERE kind = %(k)s AND recorded_at ="
                " (SELECT max(recorded_at) FROM reference_rev WHERE kind = %(k)s)", {"k": kind})}
            if last == {(key, None if r.get("valid_from") is None else str(r["valid_from"]), sha)
                        for r, _, key, sha in items}:
                return []
            delivered_at = con.execute("SELECT clock_timestamp()").fetchone()[0]
        for r, content, key, sha in items:
            if not snapshot:
                last_sha = con.execute(
                    "SELECT content_sha256 FROM reference_rev WHERE kind = %s AND ref_key = %s"
                    " AND valid_from IS NOT DISTINCT FROM %s ORDER BY rev_id DESC LIMIT 1",
                    (kind, key, r.get("valid_from"))).fetchone()
                if last_sha and last_sha[0] == sha:
                    continue
            con.execute("INSERT INTO reference_rev (kind, ref_key, valid_from, active, content, adapter_fingerprint,"
                        " content_sha256, recorded_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                        if snapshot else
                        "INSERT INTO reference_rev (kind, ref_key, valid_from, active, content, adapter_fingerprint,"
                        " content_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (kind, key, r.get("valid_from"), r.get("active", True), Jsonb(content), adapter_fingerprint,
                         sha, *((delivered_at,) if snapshot else ())))
            changed.append(key)
    return changed


def _reference_revs(con: psycopg.Connection, kind: str, on: date, known_at: datetime | None) -> list[tuple[int, dict]]:
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    if kind in LATE_REFERENCE:  # the keys of the last delivery by ``known_at``; per key the newest delivery with a row <= ``on``
        return con.execute(
            "SELECT DISTINCT ON (ref_key) rev_id, content FROM reference_rev WHERE kind = %(k)s AND recorded_at <= %(at)s"
            " AND coalesce(valid_from, '-infinity'::date) <= %(on)s AND ref_key IN (SELECT ref_key FROM reference_rev"
            " WHERE kind = %(k)s AND recorded_at = (SELECT max(recorded_at) FROM reference_rev WHERE kind = %(k)s"
            " AND recorded_at <= %(at)s)) ORDER BY ref_key, recorded_at DESC,"
            " coalesce(valid_from, '-infinity'::date) DESC, rev_id DESC",
            {"k": kind, "at": known_at, "on": on}).fetchall()
    return con.execute(
        "SELECT DISTINCT ON (ref_key) rev_id, content FROM (SELECT *, coalesce(valid_from, CASE WHEN rev_id ="
        " min(rev_id) OVER (PARTITION BY ref_key) THEN '-infinity'::date"
        f" ELSE {MSK_DAY} END) AS eff FROM reference_rev"
        " WHERE kind = %s AND recorded_at <= %s) r WHERE eff <= %s ORDER BY ref_key, eff DESC, rev_id DESC",
        (kind, known_at, on)).fetchall()


def current_reference(con: psycopg.Connection, kind: str, on: date, known_at: datetime | None = None) -> list[dict]:
    """The rows of a reference table in force on ``on`` as known at ``known_at`` (default now), by key: per key the
    revision recorded by then with the greatest effective date <= ``on`` (its ``valid_from``, else the Moscow day it
    was recorded; the first revision of a key: always), the latest on a tie. Retired rows (``active`` false) are
    returned too, as ``current_books`` does. A ``LATE_REFERENCE`` kind is a snapshot: only the keys of the last
    delivery recorded by ``known_at`` count; per key the row of the newest delivery that has one with a ``valid_from``
    (none: always) <= ``on``, the greatest ``valid_from`` within it, so a row dated for later leaves the previous
    delivery's row in force until its day."""
    return [c for _, c in _reference_revs(con, kind, on, known_at)]


def moscow_day(at: datetime) -> date:
    """The Moscow calendar day (fixed +03:00) of a moment."""
    return at.astimezone(timezone(timedelta(hours=3))).date()


def current_policy(con: psycopg.Connection, kind: str, known_at: datetime | None = None) -> list[dict]:
    """The rows of a policy table that holds for EVERY date: the ones in force on the Moscow day of ``known_at``
    (default: today) as known at ``known_at``, whatever the date being computed (owner, 04.11.2026, TDM#567: one
    methodology for the whole history; a changed setting recomputes all dates). A ``valid_from`` in the future only
    postpones the change to its day."""
    known_at = known_at or con.execute("SELECT now()").fetchone()[0]
    return current_reference(con, kind, moscow_day(known_at), known_at)


class ReferenceRevs:
    """The revisions of the ``LATE_REFERENCE`` tables loaded once (a few dozen rows), so that a plan over hundreds of
    dates compares them in Python, not with queries a date (TDM#567). Same choice as ``current_policy``: the keys of
    the last delivery recorded by the horizon; per key the row of the newest delivery that has one with a
    ``valid_from`` (none: always) <= the horizon's Moscow day, the greatest ``valid_from`` within it."""

    def __init__(self, con: psycopg.Connection) -> None:
        self.now = con.execute("SELECT now()").fetchone()[0]
        self.rows = list(con.execute(
            "SELECT kind, ref_key, rev_id, recorded_at, valid_from, content_sha256 FROM reference_rev WHERE kind = ANY(%s)",
            (list(LATE_REFERENCE),)))

    def ids(self, known_at: datetime | None = None) -> dict[str, list[str]]:
        """Per kind the rows ``current_policy`` picks as known at ``known_at`` (default now), each as ``key:content
        hash``: a delivery that changes nothing in force (a row added for a later day) is no change."""
        horizon = known_at or self.now
        day = moscow_day(horizon)
        out = {}
        for kind in LATE_REFERENCE:
            known = [r for r in self.rows if r[0] == kind and r[3] <= horizon]
            last = max((r[3] for r in known), default=None)
            keys = {r[1] for r in known if r[3] == last}
            best: dict[str, tuple[Any, date, int, str]] = {}
            for _, key, rev_id, rec, valid_from, sha in known:
                eff = valid_from or date.min
                rank = (rec, eff, rev_id, sha)
                if key in keys and eff <= day and (key not in best or rank[:3] > best[key][:3]):
                    best[key] = rank
            out[kind] = sorted(f"{key}:{r[3]}" for key, r in best.items())
        return out


def reference_rev_ids(con: psycopg.Connection, known_at: datetime | None = None,
                      revs: ReferenceRevs | None = None) -> dict[str, list[str]]:
    """Per ``LATE_REFERENCE`` kind its rows (key and content hash) in force today as known at ``known_at`` (default now),
    the same for every date; ``revs``: the revisions loaded once for many dates."""
    return (revs or ReferenceRevs(con)).ids(known_at)


def late_reference(con: psycopg.Connection, known_at: datetime | None,
                   revs: ReferenceRevs | None = None) -> list[str]:
    """The ``LATE_REFERENCE`` kinds whose revisions in force today now differ from those a run that read its inputs at
    ``known_at`` saw (compared by key and content); the same answer for every date."""
    if known_at is None:
        return []
    revs = revs or ReferenceRevs(con)
    now, then = revs.ids(), revs.ids(known_at)
    return [k for k in LATE_REFERENCE if now.get(k) != then.get(k)]


# --- runs ----------------------------------------------------------------------------------------------

PNL_COMPONENTS = ("carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "funding", "convexity",
                  "new_trades", "other")
"""The PnL components of a run position (``run_position.pnl``), in the order the explained PnL sums them; the run
totals per book (``publish.desk_run``) add ``total`` (TDM#568: one list, so that a book's components sum to its total)."""

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


INTRADAY_GRACE = timedelta(minutes=60)
"""How long a replaced INTRADAY run stays after the run that replaced it was made (``prune_intraday``). A page keeps the
``run_id`` it was opened on and reads the rest of its data lazily (a position's sources, flows, history, the calendar);
the intraday cuts come every half hour, so a run deleted at once left the page it had just given to the owner with
«run not found» (TDM#675). An hour is two cuts: the page opened on a run still reads it after the next cut and after a
recompute done by hand; a page left open longer is told to be reloaded. Only a few small runs more stay on the date."""


def _drop_runs(con: psycopg.Connection, ids: list[int]) -> None:
    """Delete INTRADAY runs ``ids`` with their rows (the triggers allow it, 0010), in the caller's transaction."""
    if ids:
        for table in RUN_ROW_TABLES:
            con.execute("DELETE FROM " + table + " WHERE run_id = ANY(%s)", (ids,))
        con.execute("DELETE FROM run WHERE run_id = ANY(%s)", (ids,))


def prune_intraday(con: psycopg.Connection, business_date: date, keep_run_id: int,
                   grace: timedelta | None = None) -> list[int]:
    """Delete the INTRADAY runs of ``business_date`` that a newer one replaced, with their rows: the run shown stays
    (R11). A replaced run stays until the run that replaced it is ``grace`` old (``INTRADAY_GRACE``), so every replaced
    run younger than that is kept, not only one: the page that was opened on it keeps reading it (TDM#675). A run that
    failed is deleted at once, a run a scenario job was submitted on is kept, ``keep_run_id`` is never deleted. The runs
    still held are taken by a later call (the next cut or recompute of the date, or ``prune_intraday_stale``). Returns
    the ids deleted."""
    grace = INTRADAY_GRACE if grace is None else grace
    with con.transaction():
        ids = [r for (r,) in con.execute(
            "SELECT run_id FROM run r WHERE run_type = 'INTRADAY' AND business_date = %(d)s AND run_id <> %(keep)s"
            " AND status <> 'loading' AND NOT EXISTS (SELECT 1 FROM scenario_job j WHERE j.base_run_id = r.run_id)"
            " AND (r.status <> 'complete' OR EXISTS (SELECT 1 FROM run n WHERE n.business_date = r.business_date"
            "  AND n.run_type = 'INTRADAY' AND n.status = 'complete' AND (n.run_id > r.run_id OR n.run_id = %(keep)s)"
            "  AND n.completed_at < now() - %(grace)s::interval))"
            " ORDER BY run_id", {"d": business_date, "keep": keep_run_id, "grace": grace})]
        _drop_runs(con, ids)
    forget_runs(ids)
    return ids


def prune_intraday_stale(con: psycopg.Connection, grace: timedelta | None = None) -> list[int]:
    """Delete the complete INTRADAY runs of any date that are not the newest complete INTRADAY run of their date and
    were replaced at least ``grace`` ago (``INTRADAY_GRACE``), without a scenario job. ``prune_intraday`` is called only
    when a new run of a date is made and holds the runs replaced within the grace; after the day's last cut nothing
    makes a run of the date, so the autopilot calls this each cycle and the date keeps one INTRADAY run (R11, TDM#675).
    Returns the ids deleted; the run cache forgets them."""
    grace = INTRADAY_GRACE if grace is None else grace
    with con.transaction():
        ids = [r for (r,) in con.execute(
            "SELECT run_id FROM run r WHERE run_type = 'INTRADAY' AND status = 'complete'"
            " AND NOT EXISTS (SELECT 1 FROM scenario_job j WHERE j.base_run_id = r.run_id)"
            " AND EXISTS (SELECT 1 FROM run n WHERE n.business_date = r.business_date AND n.run_type = 'INTRADAY'"
            "  AND n.status = 'complete' AND n.run_id > r.run_id AND n.completed_at < now() - %(grace)s::interval)"
            " ORDER BY run_id", {"grace": grace})]
        _drop_runs(con, ids)
    forget_runs(ids)
    return ids


PRUNE_LOCK = 0x7DE5C0DE
"""The advisory lock of ``prune_superseded`` (a second pruner, the CLI beside the autopilot, finds the first busy)."""
PRUNE_BATCH = 5
"""Runs one transaction of ``prune_superseded`` deletes: an interrupted prune loses at most this batch."""
PRUNE_MIN_DAYS = 1
"""The least ``keep_days`` ``prune_superseded`` accepts: a run replaced today can still be what a page shows."""

_PRUNABLE = (
    "SELECT r.run_id, r.business_date, r.cut_type FROM run r"
    " WHERE r.run_type = 'EOD' AND r.status = 'complete' AND r.cut_type IN ('eod_final', 'eod_preliminary')"
    f" AND r.run_id NOT IN ({CURRENT_RUNS})"  # the run a date shows is never it (nor, so, the default run)
    # replaced by a newer complete EOD run of the date (a final one replaces any, a preliminary one a preliminary),
    # and that at least ``days`` days ago: the replaced run stays that long for a look back at the change
    " AND EXISTS (SELECT 1 FROM run n WHERE n.business_date = r.business_date AND n.run_type = 'EOD'"
    "  AND n.status = 'complete' AND n.run_id > r.run_id AND n.cut_type IN ('eod_final', 'eod_preliminary')"
    "  AND (n.cut_type = 'eod_final' OR r.cut_type = 'eod_preliminary')"
    "  AND n.completed_at < now() - make_interval(days => %(days)s))"
    # a scenario job on the run (its result is kept for ever) holds it
    " AND NOT EXISTS (SELECT 1 FROM scenario_job j WHERE j.base_run_id = r.run_id)"
    " ORDER BY r.business_date, r.run_id")


def superseded_runs(con: psycopg.Connection, keep_days: int, limit: int | None = None) -> list[tuple[int, date, str]]:
    """The runs ``prune_superseded`` may delete, oldest date first: (run_id, business_date, cut_type). Each was
    replaced by a newer complete EOD run of its date at least ``keep_days`` days ago, is not the run its date shows
    (``CURRENT_RUNS``) and has no scenario job."""
    return [(r, d, c) for r, d, c in con.execute(
        _PRUNABLE + (" LIMIT %(limit)s" if limit else ""), {"days": max(keep_days, PRUNE_MIN_DAYS), "limit": limit})]


EXACT_COUNT_ROWS = 100_000
"""A run table with fewer rows than this (by the planner's count) is counted exactly for ``_share_bytes``; a bigger one
is taken at the planner's count, not scanned."""


def _table_rows(con: psycopg.Connection, table: str) -> int:
    """How many rows ``table`` has: the planner's count (``pg_class.reltuples``, kept by autovacuum) when it says the table
    is big, else counted (a small or never analyzed table costs nothing; a full scan of a big one is what to avoid)."""
    est = con.execute("SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass", (table,)).fetchone()[0]
    if est is not None and est >= EXACT_COUNT_ROWS:
        return int(est)
    return con.execute("SELECT count(*) FROM " + table).fetchone()[0]


def _share_bytes(con: psycopg.Connection, ids: list[int]) -> int:
    """About how many bytes the rows of runs ``ids`` take on disk: per table (with its indexes and TOAST) the table's
    size by the share of its rows that are the runs' (counted by ``run_id``, an index range; the table's own rows are
    ``_table_rows``). One estimate for a whole prune, made before its first transaction: not a pass over the tables for
    every batch, and none inside the delete's transaction."""
    total = 0
    for table in ("run", *RUN_ROW_TABLES):
        n = con.execute("SELECT count(*) FROM " + table + " WHERE run_id = ANY(%s)", (ids,)).fetchone()[0]
        if n and (n_all := max(_table_rows(con, table), n)):
            total += con.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0] * n // n_all
    return total


def autovacuum_on(con: psycopg.Connection) -> bool:
    """The server's autovacuum is working (the embedded server's default): it gives the space of deleted runs back to the
    tables, so no explicit ``VACUUM`` of ours is needed. It works only with ``autovacuum`` and ``track_counts`` both on
    (the server counts the dead rows for it); with the counts off the setting alone would say it does."""
    on, counts = con.execute("SELECT current_setting('autovacuum'), current_setting('track_counts')").fetchone()
    return on == "on" and counts == "on"


def prune_superseded(con: psycopg.Connection, keep_days: int, *, limit: int | None = None,
                     budget_s: float | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Delete the superseded EOD runs (``superseded_runs``) with their rows (TDM#634): the history of replaced
    calculations is not read, and a date's two passes leave a run each. The one way the runs, immutable otherwise
    (ADR-006), are deleted besides ``prune_intraday``: the transaction says ``desk.prune_superseded`` and migration
    0024's triggers allow only this kind of run (never the newest of a date, never one with a scenario job).
    ``limit``: at most this many runs; ``budget_s``: stop before the next batch once this many seconds passed;
    ``dry_run``: only the list. A batch of ``PRUNE_BATCH`` runs is one transaction: an interrupted prune keeps whole
    runs, and runs again where it stopped (nothing is deleted twice; a second call finds nothing). Returns
    {runs: [ids], dates: [first, last] or None, bytes: about what the rows took, busy: another prune runs,
    left: runs still prunable after this call}. The space is the tables' to reuse once ``vacuum_runs`` ran; the
    files shrink with ``compact_runs``."""
    if not con.autocommit or con.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        # a batch is a transaction of its own (an interrupted prune keeps whole runs): in a transaction already, or
        # without autocommit, the whole prune would be one, and the batches nothing but savepoints
        raise ValueError("prune_superseded needs a connection in autocommit, outside a transaction (store.connect)")
    t0 = time.monotonic()
    out: dict[str, Any] = {"runs": [], "dates": None, "bytes": 0, "busy": False, "left": 0}
    todo = superseded_runs(con, keep_days, limit)
    ids_all = [r for r, _, _ in todo]
    total_bytes = _share_bytes(con, ids_all) if ids_all else 0  # one estimate, before anything is deleted
    if dry_run:
        return {**out, "runs": ids_all, "dates": [str(todo[0][1]), str(todo[-1][1])] if todo else None,
                "bytes": total_bytes}
    dates: list[date] = []
    for i in range(0, len(todo), PRUNE_BATCH):
        if budget_s is not None and out["runs"] and time.monotonic() - t0 >= budget_s:
            break
        batch = todo[i:i + PRUNE_BATCH]
        with con.transaction():
            if not con.execute("SELECT pg_try_advisory_xact_lock(%s)", (PRUNE_LOCK,)).fetchone()[0]:
                out["busy"] = True
                break
            con.execute("SET LOCAL desk.prune_superseded = 'on'")
            ids = [r for r, _, _ in batch]
            for table in RUN_ROW_TABLES:
                con.execute("DELETE FROM " + table + " WHERE run_id = ANY(%s)", (ids,))
            con.execute("DELETE FROM run WHERE run_id = ANY(%s)", (ids,))
        forget_runs(ids)
        out["runs"] += ids
        dates += [d for _, d, _ in batch]
    out["dates"] = [str(min(dates)), str(max(dates))] if dates else None
    out["bytes"] = total_bytes * len(out["runs"]) // len(todo) if todo else 0  # what was deleted of what was estimated
    out["left"] = len(superseded_runs(con, keep_days)) if todo else 0
    return out


def vacuum_runs(con: psycopg.Connection) -> list[str]:
    """``VACUUM`` of the run tables: the space the deleted runs took goes back to the tables for the next runs (the
    files do not shrink: ``compact_runs``). Takes no lock that stops reads or writes; needs a connection in
    autocommit (``connect``). Returns the tables."""
    tables = [*RUN_ROW_TABLES, "run"]
    for table in tables:
        con.execute("VACUUM " + table)
    return tables


def compact_runs(con: psycopg.Connection) -> dict[str, int]:
    """``VACUUM FULL`` of the run tables: the files shrink to what the runs left take and the disk gets the rest
    back. Rewrites each table under an exclusive lock, so the monitor cannot read the runs meanwhile (minutes with a
    year of runs), and needs free disk for the largest table's copy: run by the owner's choice, never by the
    autopilot. Needs a connection in autocommit. Returns the bytes each table freed (with its indexes and TOAST)."""
    freed = {}
    for table in ("run_position", "run_core_result", "run_reference", "run_log", "model_spread", "run_summary", "run"):
        before = con.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
        con.execute("VACUUM FULL " + table)
        freed[table] = before - con.execute("SELECT pg_total_relation_size(%s::regclass)", (table,)).fetchone()[0]
    return freed


def default_runs(con: psycopg.Connection, since: date, until: date | None = None) -> dict[date, dict]:
    """The default run of each business date from ``since`` on (to ``until``, when given: a plan of one date does not
    read the history after it, TDM#660): run_id, its snapshots, position sets (sorted),
    completed_at, private inputs hash, the horizon it read its inputs at (``positions_known_at``: ``late_inputs``), the
    ``logic_version`` of the code that made it (lineage; None for a run made before it was recorded) and
    ``second_order``: False for a run made without the second derivatives (lineage ``second_order``, TDM#627: the first
    pass of the history); True for a run with them and for one made before it was recorded. ``spread_sources``: the
    carried spreads the run's model prices used, ``{secid: {"date", "z_bp"}}`` (lineage ``carried_spread_sources``,
    TDM#660; ``{}``: none were carried), None for a run made before it was recorded; ``spreads_carried``: how many
    spreads the run says it carried (lineage ``model_spreads.carried``, older than ``spread_sources``)."""
    return {r[0]: {"run_id": r[1], "market_snapshot_id": r[2], "prev_market_snapshot_id": r[3],
                   "position_set_ids": sorted(r[4]), "completed_at": r[5], "logic_version": r[6],
                   "private_inputs_hash": r[7], "positions_known_at": r[8], "second_order": r[9] != "false",
                   "spread_sources": r[10], "spreads_carried": int(r[11]) if r[11] is not None else None}
            for r in con.execute(
                "SELECT business_date, run_id, market_snapshot_id, prev_market_snapshot_id, position_set_ids,"
                " completed_at, lineage->>'logic_version', private_inputs_hash, positions_known_at,"
                " lineage->>'second_order', lineage->'carried_spread_sources',"
                " lineage->'model_spreads'->>'carried' FROM run"
                f" WHERE run_id IN ({DEFAULT_RUNS}) AND business_date >= %s AND business_date <= %s",
                (since, until or date.max))}


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


def spread_values(con: psycopg.Connection, pairs: list[tuple[int, str]]) -> dict[tuple[int, str], float]:
    """The Z-spread each (run_id, secid) of ``pairs`` holds, as ``carried_spreads`` reads it: the model spread a model
    price used, or the bond's market Z (exact or assumed) in that run; a pair the run holds no spread for is absent. What
    the plan (``backfill.plan``, TDM#660) compares a run's carried spreads with."""
    if not pairs:
        return {}
    # (``run_id = ANY`` beside the join with the pairs: the planner reads the rows of these runs by the key of
    # ``run_position`` instead of the whole table, which every plan of a date would otherwise read: TDM#660)
    rows = con.execute(
        "WITH w AS (SELECT * FROM unnest(%(r)s::bigint[], %(s)s::text[]) AS t(run_id, secid))"
        " SELECT run_id, secid, z FROM ("
        " SELECT m.run_id, m.secid, m.z_spread_bp AS z, 1 AS pri FROM model_spread m JOIN w USING (run_id, secid)"
        "  WHERE m.run_id = ANY(%(ids)s::bigint[])"
        " UNION ALL"
        " SELECT p.run_id, p.secid, (p.fact->>'spread_bp')::float8, 0 FROM run_position p JOIN w USING (run_id, secid)"
        "  WHERE p.run_id = ANY(%(ids)s::bigint[]) AND p.quality IN ('exact', 'assumed')"
        "  AND jsonb_typeof(p.fact->'spread_bp') = 'number'"
        ") x ORDER BY run_id, secid, pri",
        {"r": [r for r, _ in pairs], "s": [s for _, s in pairs], "ids": sorted({r for r, _ in pairs})},
        prepare=False).fetchall()  # no server-side prepared plan: after five runs on one connection psycopg switches
    # to the generic plan, which reads the whole table for these arrays (30 times slower, TDM#660)
    out: dict[tuple[int, str], float] = {}
    seen: set[tuple[int, str]] = set()
    for rid, secid, z in rows:
        if (rid, secid) not in seen:  # the first of a pair, as ``carried_spreads`` takes it; a non-finite one is none
            seen.add((rid, secid))
            if z is not None and math.isfinite(z):
                out[(rid, secid)] = z
    return out


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
