"""Desk store (slice S5): migrations, bitemporal append-only positions, immutable runs, carried spreads."""

from __future__ import annotations

from datetime import date, timedelta

import psycopg
import pytest
from psycopg.types.json import Jsonb

from desk import store

D1, D2, D3 = date(2026, 10, 13), date(2026, 10, 14), date(2026, 10, 15)
BOOK = [("P1", "OFZ", "SU26238RMFS4", 1000.0), ("P2", "OFZ", "SU26240RMFS0", -500.0),
        ("C1", "CORP", "RU000A0JXQ44", 200.0)]


@pytest.fixture
def con(store_url):
    with store.connect(store_url) as c:
        yield c


def _load(con, rows, d=D1, name="book.csv"):
    return store.load_positions(con, rows, valid_date=d, source_name=name, source_sha256="0" * 64)


def test_migrate_is_idempotent(store_url):
    assert store.migrate(store_url) == []


def test_positions_are_bitemporal(con):
    first = _load(con, BOOK)
    assert [(x.book_id, x.revision, x.changed) for x in first] == [("CORP", 1, True), ("OFZ", 1, True)]
    between = con.execute("SELECT now()").fetchone()[0]
    assert [x.changed for x in _load(con, BOOK)] == [False, False]  # the same file again: nothing new
    fixed = [r if r[0] != "P2" else ("P2", "OFZ", "SU26240RMFS0", -600.0) for r in BOOK]
    second = _load(con, fixed, name="book_fixed.csv")
    assert [(x.book_id, x.revision, x.changed) for x in second] == [("CORP", 1, False), ("OFZ", 2, True)]

    now = store.positions_for(con, D1)
    assert {p: q for p, _, _, q in now.rows}["P2"] == -600.0
    assert set(now.set_ids) == {first[0].set_id, second[1].set_id}
    then = store.positions_for(con, D1, known_at=between)
    assert {p: q for p, _, _, q in then.rows}["P2"] == -500.0
    assert then.private_inputs_hash != now.private_inputs_hash
    assert store.positions_for(con, D1 - timedelta(days=1)).rows == []

    later = store.positions_for(con, D3)  # carried forward: the latest earlier set of each book
    assert later.rows == now.rows and later.valid_dates == {"CORP": D1, "OFZ": D1}
    assert later.private_inputs_hash == now.private_inputs_hash
    assert store.positions_for(con, D3, books=["CORP"]).rows == [r for r in now.rows if r[1] == "CORP"]


def test_a_load_closes_the_books_it_covers_without_rows(con):
    first = _load(con, BOOK)
    between = con.execute("SELECT now()").fetchone()[0]
    ofz = [r for r in BOOK if r[1] == "OFZ"]
    assert [(x.book_id, x.revision, x.changed) for x in _load(con, ofz)] == [("OFZ", 1, False)]  # CORP kept
    assert len(store.positions_for(con, D1).rows) == 3
    closed = store.load_positions(con, ofz, valid_date=D1, source_name="ofz.csv", source_sha256="1" * 64,
                                  books=["OFZ", "CORP"])
    assert [(x.book_id, x.revision, x.changed) for x in closed] == [("CORP", 2, True), ("OFZ", 1, False)]
    assert con.execute("SELECT count(*) FROM position_input WHERE set_id = %s", (closed[0].set_id,)).fetchone()[0] == 0
    again = store.load_positions(con, ofz, valid_date=D1, source_name="ofz.csv", source_sha256="1" * 64,
                                 books=["OFZ", "CORP"])
    assert [x.changed for x in again] == [False, False]  # already empty: no third revision
    now, later = store.positions_for(con, D1), store.positions_for(con, D3)
    assert now.rows == later.rows == ofz and later.valid_dates == {"CORP": D1, "OFZ": D1}  # carried forward empty
    assert closed[0].set_id in now.set_ids and closed[0].set_id in later.set_ids  # the lineage lists it
    then = store.positions_for(con, D1, known_at=between)
    assert then.rows == store.positions_for(con, D3, known_at=between).rows == sorted(BOOK, key=lambda r: (r[1], r[0]))
    assert first[0].set_id in then.set_ids  # an earlier known_at still sees CORP
    with pytest.raises(store.InputError, match="books the load does not cover.*CORP"):
        store.load_positions(con, BOOK, valid_date=D2, source_name="x", source_sha256="-", books=["OFZ"])


def test_duplicate_position_ids_are_rejected(con):
    with pytest.raises(store.InputError, match="twice in the file: P1"):
        _load(con, BOOK + [BOOK[0]])  # an identical line too: a set would collapse it
    assert con.execute("SELECT count(*) FROM position_set").fetchone()[0] == 0  # nothing written
    _load(con, BOOK)
    moved = [("P1", "CORP", "SU26238RMFS4", 1000.0)]
    with pytest.raises(store.InputError, match=r"other books for 2026-10-14: P1 \(OFZ\)"):
        _load(con, moved, d=D2)  # OFZ of D1, carried to D2, holds P1
    assert con.execute("SELECT count(*) FROM position_set").fetchone()[0] == 2
    assert [x.book_id for x in store.load_positions(con, moved + BOOK[1:2], valid_date=D2, source_name="m",
                                                    source_sha256="-", books=["OFZ", "CORP"])] == ["CORP", "OFZ"]


def test_recorded_at_is_the_insert_time(con):
    with con.transaction():  # a load inside a longer transaction: recorded_at is not its start
        t0 = con.execute("SELECT now()").fetchone()[0]
        con.execute("SELECT pg_sleep(0.01)")
        _load(con, BOOK)
    assert all(t > t0 for (t,) in con.execute("SELECT recorded_at FROM position_set"))


def test_hash_depends_on_content_only(con):
    _load(con, BOOK, d=D1)
    _load(con, BOOK, d=D2, name="other.csv")  # same content, another date and file: new sets, same hash
    a, b = store.positions_for(con, D1), store.positions_for(con, D2)
    assert set(a.set_ids).isdisjoint(b.set_ids) and a.private_inputs_hash == b.private_inputs_hash
    _load(con, BOOK[:2] + [("C1", "CORP", "RU000A0JXQ44", 201.0)], d=D3)
    assert store.positions_for(con, D3).private_inputs_hash != a.private_inputs_hash


def test_inputs_cannot_change(con):
    _load(con, BOOK)
    for sql in ("UPDATE position_input SET quantity = 0", "DELETE FROM position_input",
                "UPDATE position_set SET revision = 9", "DELETE FROM position_set", "TRUNCATE position_input",
                "TRUNCATE position_set CASCADE"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            con.execute(sql)


def _run(con, d, *, run_type="EOD", status="complete", market=(), model=(), cut="eod_final"):
    """A minimal run: market = [(secid, quality, spread_bp)], model = [(secid, z, source)]."""
    rid = con.execute(
        "INSERT INTO run (run_type, business_date, cut_type, market_snapshot_id, static_snapshot_id,"
        " private_inputs_hash, position_set_ids, positions_known_at, core_version, backend_version, lineage)"
        " VALUES (%s, %s, %s, 'm', 's', 'h', '{}', now(), 'c', 'b', '{}') RETURNING run_id",
        (run_type, d, cut)).fetchone()[0]
    for i, (secid, q, z) in enumerate(market):
        con.execute("INSERT INTO run_position (run_id, position_id, ord, book_id, secid, quantity, quality, fact)"
                    " VALUES (%s, %s, %s, 'B', %s, 1, %s, %s)", (rid, f"P{i}", i, secid, q, Jsonb({"spread_bp": z})))
    for secid, z, src in model:
        con.execute("INSERT INTO model_spread VALUES (%s, %s, %s, %s, %s)", (rid, secid, d, z, src))
    if status != "loading":
        con.execute("UPDATE run SET status = %s WHERE run_id = %s", (status, rid))
    return rid


def test_complete_run_is_frozen(con):
    rid = _run(con, D1, market=[("X", "exact", 100.0)])
    for sql in ("UPDATE run SET status = 'failed' WHERE run_id = %s", "DELETE FROM run WHERE run_id = %s",
                "UPDATE run_position SET pv_rub = 1 WHERE run_id = %s", "DELETE FROM run_position WHERE run_id = %s"):
        with pytest.raises(psycopg.errors.RaiseException, match="desk store"):
            con.execute(sql, (rid,))
    with pytest.raises(psycopg.errors.RaiseException, match="frozen"):
        con.execute("INSERT INTO run_log VALUES (%s, 1, 'INFO', 'late')", (rid,))
    for t in ("run_position", "run_log", "run_reference", "run_core_result", "model_spread", "run CASCADE"):
        with pytest.raises(psycopg.errors.RaiseException, match="TRUNCATE .* not allowed"):
            con.execute(f"TRUNCATE {t}")


def test_carried_spreads_precedence(con):
    _run(con, D1, model=[("A", 250.0, "issuer median Z 250 bp (4 bonds)")], market=[("B", "exact", 90.0)])
    r2 = _run(con, D2, market=[("A", "exact", 266.0), ("C", "market_value", 500.0), ("D", "assumed", None)])
    _run(con, D2, run_type="TEST", model=[("B", 1.0, "test")])
    _run(con, D2, status="failed", model=[("B", 2.0, "failed")])
    _run(con, D2, status="loading", model=[("B", 3.0, "loading")])
    _run(con, D2, run_type="INTRADAY", cut="intraday", model=[("B", 4.0, "intraday")])
    _run(con, D2, cut="eod_preliminary", model=[("B", 5.0, "re-run preliminary")])  # not the default run
    _run(con, D2, run_type="TEST", market=[("A", "exact", 7.0)])
    _run(con, D3, model=[("A", 999.0, "same date")])  # not before the date asked for

    got = store.carried_spreads(con, before=D3)
    assert set(got) == {"A", "B"}  # C is a fallback rung, D has no finite Z
    a = got["A"]  # the later market Z wins over the earlier model spread
    assert (a.z_spread_bp, a.business_date, a.source) == (266.0, D2, f"last market Z 266 bp on {D2} (run {r2})")
    assert (got["B"].z_spread_bp, got["B"].business_date) == (90.0, D1)
    early = store.carried_spreads(con, before=D2)["A"]
    assert (early.z_spread_bp, early.source) == (250.0, "issuer median Z 250 bp (4 bonds)")


def test_reap_orphans(con):
    rid = _run(con, D1, status="loading")
    assert store.reap_orphans(con) == []
    assert store.reap_orphans(con, older_than=timedelta(0)) == [rid]
    status, error = con.execute("SELECT status, error FROM run WHERE run_id = %s", (rid,)).fetchone()
    assert status == "failed" and "orphaned" in error
