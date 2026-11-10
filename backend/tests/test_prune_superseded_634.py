"""TDM#634, superseded runs are deleted: after a date's two passes (or a correction, a new program version) the run
that replaced the older ones is the only one the screens read, so the older ones go, on purpose and narrowly: a
transaction that says so (migration 0024's triggers), never a date's newest run, never a run a scenario job was
made on, not before the replacement is ``keep_days`` old, in batches, again and again with the same result. The space
goes back to the tables (VACUUM) and to the disk on the owner's request (VACUUM FULL). Synthetic data only."""

# ruff: noqa: F811, F401  (the ``cache`` fixture of test_two_pass_627 is imported to be used as a fixture)
from __future__ import annotations

from datetime import date

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_r23_backfill import load, settings
from test_two_pass_627 import RUN_DATES, cache, first_pass, plan, runs, second_pass

from desk import backfill, daystatus, store
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.marketdata.bundle import SnapshotDir
from desk.scenario import jobs


def all_runs(url: str) -> list[tuple[int, str, str]]:
    with store.connect(url) as con:
        return [(r, str(d), c) for r, d, c in con.execute(
            "SELECT run_id, business_date, cut_type FROM run ORDER BY run_id")]


def counts(url: str) -> dict[str, int]:
    with store.connect(url) as con:
        return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("run", *store.RUN_ROW_TABLES)}


def age(url: str, days: dict[int, int]) -> None:
    """Make the runs complete so many days earlier (run id -> days): what a test cannot wait for. A run is immutable
    in service; the trigger is switched off for this one statement."""
    with store.connect(url) as con:
        con.execute("ALTER TABLE run DISABLE TRIGGER run_frozen")
        for rid, n in days.items():
            con.execute("UPDATE run SET completed_at = completed_at - make_interval(days => %s) WHERE run_id = %s",
                        (n, rid))
        con.execute("ALTER TABLE run ENABLE TRIGGER run_frozen")


def age_all(url: str, days: int, only: set[str] | None = None) -> None:
    """The default runs (of the dates ``only``) ``days`` days old, the runs they replaced one day older: the order
    of the runs' ages is what it was (a default run is the newest completed)."""
    default = {r["run_id"]: d for d, r in runs(url).items()}
    with store.connect(url) as con:
        rows = con.execute("SELECT run_id, business_date::text FROM run").fetchall()
    age(url, {r: days + (0 if r in default else 1) for r, d in rows if only is None or d in only})


class SpyConnection(psycopg.Connection):
    """A connection that records every statement with the state of its transaction at that moment."""

    log: list[tuple[str, bool]]

    def execute(self, query, params=None, **kw):
        self.log.append((str(query), self.info.transaction_status == psycopg.pq.TransactionStatus.INTRANS))
        return super().execute(query, params, **kw)


def two_passes(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    second_pass(s, store_url)
    return s


def test_the_two_passes_leave_a_replaced_run_per_date_and_only_those_are_pruned(tmp_path, cache, store_url):
    s = two_passes(tmp_path, cache, store_url)
    before = all_runs(store_url)
    assert len(before) == 8
    default = {d: r["run_id"] for d, r in runs(store_url).items()}
    replaced = [r for r, _, _ in before if r not in default.values()]
    assert len(replaced) == 4
    with store.connect(store_url) as con:
        assert store.prune_superseded(con, 7)["runs"] == []  # the replacement is minutes old: kept for 7 days
        assert store.superseded_runs(con, 0) == [] and store.superseded_runs(con, 1) == []  # 0 is read as 1
    age_all(store_url, 2)  # the replacement is two days old
    with store.connect(store_url) as con:
        assert store.superseded_runs(con, 7) == []
        assert [r for r, _, _ in store.superseded_runs(con, 2)] == replaced and store.superseded_runs(con, 3) == []
        res = store.prune_superseded(con, 2, dry_run=True)
        assert res["runs"] == replaced and res["bytes"] > 0 and res["dates"] == [RUN_DATES[0], RUN_DATES[-1]]
        assert counts(store_url)["run"] == 8  # a dry run deletes nothing
        res = store.prune_superseded(con, 2)
    assert res["runs"] == replaced and res["busy"] is False and res["left"] == 0 and res["bytes"] > 0
    assert [r for r, _, _ in all_runs(store_url)] == sorted(default.values())  # the newest run of every date stays
    after = counts(store_url)
    assert after["run"] == 4 and after["run_position"] == 4 * len(runs(store_url)[RUN_DATES[0]]["rows"])
    # nothing the screens read changed: the same default runs, the plan has nothing to do, the numbers are as before
    p = plan(store_url, s)
    assert not p.todo and not p.second_todo and p.summary()["second"]["done"] == 4
    assert {d: r["run_id"] for d, r in runs(store_url).items()} == default
    with store.connect(store_url) as con:
        assert store.prune_superseded(con, 2)["runs"] == []  # idempotent: nothing left to delete
        spreads = store.carried_spreads(con, date(2026, 10, 16))
    assert spreads  # the default runs still supply the model prices' carried spreads
    client = TestClient(create_app(store_url))
    assert client.get(f"/api/v1/runs/{default[RUN_DATES[-1]]}").status_code == 200
    assert client.get(f"/api/v1/runs/{replaced[0]}").status_code == 404
    day = client.get("/api/v1/inbox", params={"on": RUN_DATES[-1]}).json()
    assert [r["run_id"] for r in day["runs"]] == [default[RUN_DATES[-1]]]  # the day page lists the one that is left
    assert day["runs"][0]["changed"] == []  # what it replaced is gone: nothing to compare with
    # ... and the page is told so, to say it rather than «the inputs are the same» (daystatus.SUPERSEDES_PRUNED)
    assert day["runs"][0]["supersedes_pruned"] is True
    assert day["runs"][0]["supersedes_pruned_text"] == "заменённый расчёт удалён (хранится только актуальный)"


def test_the_replaced_run_is_deleted_by_its_age_not_the_dates_and_the_newest_is_never_deleted(tmp_path, cache, store_url):
    s = two_passes(tmp_path, cache, store_url)
    default = {d: r["run_id"] for d, r in runs(store_url).items()}
    rows = all_runs(store_url)
    by_date = {d: [r for r, dd, _ in rows if dd == d] for d in RUN_DATES}
    age_all(store_url, 10, set(RUN_DATES[:2]))  # only the two older dates' replacement is old
    with store.connect(store_url) as con:
        got = store.prune_superseded(con, 7)
    assert got["runs"] == [by_date[RUN_DATES[0]][0], by_date[RUN_DATES[1]][0]]
    assert {d: [r for r, dd, _ in all_runs(store_url) if dd == d] for d in RUN_DATES} == {
        RUN_DATES[0]: [default[RUN_DATES[0]]], RUN_DATES[1]: [default[RUN_DATES[1]]],
        RUN_DATES[2]: by_date[RUN_DATES[2]], RUN_DATES[3]: by_date[RUN_DATES[3]]}
    assert not plan(store_url, s).second_todo


def test_a_run_with_a_scenario_job_and_a_run_a_date_shows_are_kept(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    first = {d: r["run_id"] for d, r in runs(store_url).items()}
    with store.connect(store_url) as con:  # a scenario on the first pass's run of the 13th
        job, _ = jobs.submit(con, first["2026-10-13"], {"ofzSpread": 10})
        assert jobs.run_once(con, SnapshotDir(cache)) == job["job_id"]
    second_pass(s, store_url)
    default = {d: r["run_id"] for d, r in runs(store_url).items()}
    age_all(store_url, 30)
    with store.connect(store_url) as con:
        got = store.prune_superseded(con, 7)
    assert sorted(got["runs"]) == sorted(r for d, r in first.items() if d != "2026-10-13")
    left = {r for r, _, _ in all_runs(store_url)}
    assert first["2026-10-13"] in left and default["2026-10-13"] in left and len(left) == 5
    with store.connect(store_url) as con:  # its result is still there, and the job reads the run it was made on
        assert con.execute("SELECT count(*) FROM scenario_result WHERE base_run_id = %s",
                           (first["2026-10-13"],)).fetchone()[0] == 1
        assert store.superseded_runs(con, 7) == []
    # the final run replaces a preliminary one; the preliminary run made after the final one is nobody's replacement
    with store.connect(store_url) as con:
        assert con.execute("SELECT count(*) FROM run_position WHERE run_id = %s",
                           (first["2026-10-13"],)).fetchone()[0] > 0


def test_the_triggers_forbid_every_other_delete_and_allow_only_the_superseded_kind(tmp_path, cache, store_url):
    two_passes(tmp_path, cache, store_url)
    default = {d: r["run_id"] for d, r in runs(store_url).items()}
    replaced = [r for r, _, _ in all_runs(store_url) if r not in default.values()]
    with store.connect(store_url) as con:
        # without the word, a replaced run is as immutable as ever
        for sql in ("DELETE FROM run WHERE run_id = %(r)s", "DELETE FROM run_position WHERE run_id = %(r)s",
                    "DELETE FROM run_log WHERE run_id = %(r)s", "UPDATE run SET error = 'x' WHERE run_id = %(r)s",
                    "UPDATE run_position SET quality = 'x' WHERE run_id = %(r)s"):
            with pytest.raises(psycopg.errors.RaiseException, match="cannot change|frozen"):
                con.execute(sql, {"r": replaced[0]})
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            con.execute("TRUNCATE run_position")
        # with it, the newest run of a date (its default run) still cannot go, nor can any update
        with con.transaction():
            con.execute("SET LOCAL desk.prune_superseded = 'on'")
            for sql in ("DELETE FROM run WHERE run_id = %(r)s", "DELETE FROM run_position WHERE run_id = %(r)s"):
                with pytest.raises(psycopg.errors.RaiseException, match="cannot change|frozen"), con.transaction():
                    con.execute(sql, {"r": default[RUN_DATES[0]]})
            with pytest.raises(psycopg.errors.RaiseException, match="cannot change"), con.transaction():
                con.execute("UPDATE run SET error = 'x' WHERE run_id = %s", (replaced[0],))
            with pytest.raises(psycopg.errors.RaiseException, match="append-only"), con.transaction():
                con.execute("TRUNCATE run_position")
        # the word is for one transaction only (SET LOCAL)
        with pytest.raises(psycopg.errors.RaiseException):
            con.execute("DELETE FROM run_position WHERE run_id = %s", (replaced[0],))
        assert counts(store_url)["run"] == 8


def test_a_final_run_replaces_a_preliminary_one_and_a_lone_run_is_not_replaced(tmp_path, cache, store_url):
    """The kinds of replacement: a run replaced by a newer one of its cut, a preliminary run by a final one; a
    preliminary run made after the final one replaces nothing and is not replaced (the date keeps it)."""
    s, _ = first_pass(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        ids = {d: r for r, d in con.execute("SELECT run_id, business_date::text FROM run")}
        # make the run of the 12th a preliminary one, that of the 13th a preliminary run made after a final one
        con.execute("ALTER TABLE run DISABLE TRIGGER run_frozen")
        con.execute("UPDATE run SET cut_type = 'eod_preliminary' WHERE run_id = %s", (ids["2026-10-12"],))
        con.execute("ALTER TABLE run ENABLE TRIGGER run_frozen")
    second_pass(s, store_url, budget_s=0)  # the newest date (15.10) gets its replacement
    with store.connect(store_url) as con:
        rows = con.execute("SELECT run_id, business_date::text, cut_type FROM run ORDER BY run_id").fetchall()
    assert [r for r in rows if r[1] == "2026-10-15"] and len([r for r in rows if r[1] == "2026-10-15"]) == 2
    age(store_url, {r[0]: 30 for r in rows})
    with store.connect(store_url) as con:
        got = store.prune_superseded(con, 7)
    assert got["runs"] == [next(x[0] for x in rows if x[1] == "2026-10-15")]  # only the 15th's first pass
    # a final run of the 12th made now replaces the preliminary one: its run is prunable once the final one is old
    second_pass(s, store_url)
    with store.connect(store_url) as con:
        now = con.execute("SELECT run_id, business_date::text, cut_type FROM run ORDER BY run_id").fetchall()
    assert [x for x in now if x[1] == "2026-10-12" and x[2] == "eod_final"]
    age(store_url, {x[0]: 30 for x in now})
    with store.connect(store_url) as con:
        got = store.prune_superseded(con, 7)
    assert ids["2026-10-12"] in got["runs"]  # the preliminary run is gone: a final one replaced it
    left = [x for x in all_runs(store_url) if x[1] == "2026-10-12"]
    assert len(left) == 1 and left[0][2] == "eod_final"


def test_a_prune_is_cut_by_the_limit_and_by_the_budget_and_resumes_where_it_stopped(tmp_path, cache, store_url,
                                                                                    monkeypatch):
    two_passes(tmp_path, cache, store_url)
    age_all(store_url, 30)
    monkeypatch.setattr(store, "PRUNE_BATCH", 1)
    with store.connect(store_url) as con:
        first = store.prune_superseded(con, 7, limit=1)
        assert len(first["runs"]) == 1 and first["left"] == 3
        second = store.prune_superseded(con, 7, budget_s=0)  # at least one batch, then the budget says stop
        assert len(second["runs"]) == 1 and second["left"] == 2
        rest = store.prune_superseded(con, 7)
        assert len(rest["runs"]) == 2 and rest["left"] == 0
    assert counts(store_url)["run"] == 4


def test_an_interrupted_prune_keeps_whole_runs_and_finishes_next_time(tmp_path, cache, store_url, monkeypatch):
    two_passes(tmp_path, cache, store_url)
    age_all(store_url, 30)
    monkeypatch.setattr(store, "PRUNE_BATCH", 2)
    deletes = 1 + len(store.RUN_ROW_TABLES)  # statements of one batch

    class Dies(SpyConnection):
        def execute(self, query, params=None, **kw):
            if str(query).startswith("DELETE FROM") and sum(
                    q.startswith("DELETE FROM") for q, _ in self.log) == deletes + 2:
                raise KeyboardInterrupt  # the process is stopped between two statements of the second batch
            return super().execute(query, params, **kw)

    con = Dies.connect(store_url, autocommit=True)
    con.log = []
    with con, pytest.raises(KeyboardInterrupt):
        store.prune_superseded(con, 7)
    monkeypatch.undo()
    c = counts(store_url)
    assert c["run"] == 6  # the first batch of two is committed, the second left whole
    with store.connect(store_url) as con:
        # no run is left half deleted: every run has its rows, or neither the run nor its rows are there
        orphan = con.execute("SELECT count(*) FROM run_position p WHERE NOT EXISTS (SELECT 1 FROM run r"
                             " WHERE r.run_id = p.run_id)").fetchone()[0]
        bare = con.execute("SELECT count(*) FROM run r WHERE r.status = 'complete' AND NOT EXISTS (SELECT 1"
                           " FROM run_position p WHERE p.run_id = r.run_id)").fetchone()[0]
        assert orphan == 0 and bare == 0
        assert len(store.prune_superseded(con, 7)["runs"]) == 2
    assert counts(store_url)["run"] == 4


def test_the_space_goes_back_to_the_tables_and_the_disk_on_request(tmp_path, cache, store_url):
    two_passes(tmp_path, cache, store_url)
    default = {d: r["run_id"] for d, r in runs(store_url).items()}
    age_all(store_url, 30)

    def size(con) -> int:
        return sum(con.execute("SELECT pg_total_relation_size(%s::regclass)", (t,)).fetchone()[0]
                   for t in ("run", *store.RUN_ROW_TABLES))

    with store.connect(store_url) as con:
        full = size(con)
        res = store.prune_superseded(con, 7)
        assert store.vacuum_runs(con) == [*store.RUN_ROW_TABLES, "run"]
        assert res["bytes"] > 0
        # about half the bytes were the replaced runs': the estimate is of that order
        assert 0.25 * full < res["bytes"] < 0.75 * full
        freed = store.compact_runs(con)
        assert size(con) < full and sum(freed.values()) > 0  # the files shrank: the disk has the space back
        assert abs(sum(freed.values()) - res["bytes"]) < 0.3 * res["bytes"] + 200_000  # what was estimated is what came back
    assert counts(store_url)["run"] == 4 and counts(store_url)["run_position"] > 0
    assert runs(store_url).keys() == default.keys()  # the runs that are left read as before


def test_a_run_that_replaced_nothing_or_whose_replaced_run_is_there_is_not_marked_pruned(tmp_path, cache, store_url):
    two_passes(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        r1, r2 = daystatus._runs(con, date(2026, 10, 14))
    assert r1["supersedes_pruned"] is False and r1["supersedes_pruned_text"] is None
    assert r2["supersedes_pruned"] is False and r2["supersedes_pruned_text"] is None and r2["changed"]
    age_all(store_url, 30)
    with store.connect(store_url) as con:
        store.prune_superseded(con, 7)
        (r,) = daystatus._runs(con, date(2026, 10, 14))
    assert r["supersedes_run_id"] == r2["supersedes_run_id"] and r["supersedes_pruned"] is True


def test_the_size_of_what_is_deleted_is_estimated_once_and_never_inside_a_delete_transaction(
        tmp_path, cache, store_url, monkeypatch):
    """A count(*) of every run table per batch, inside the transaction that deletes, was a full scan of seven tables
    (millions of rows each) a batch: one estimate a call, before the first transaction."""
    two_passes(tmp_path, cache, store_url)
    age_all(store_url, 30)

    def prune(batch: int) -> tuple[dict, list[tuple[str, bool]]]:
        monkeypatch.setattr(store, "PRUNE_BATCH", batch)
        con = SpyConnection.connect(store_url, autocommit=True)
        con.log = []
        try:
            res = store.prune_superseded(con, 7, dry_run=True)
            assert res["bytes"] > 0
            return res, con.log
        finally:
            con.close()

    _, one = prune(5)
    _, many = prune(1)
    assert [q for q, _ in one] == [q for q, _ in many]  # a dry run: the list and the estimate, however it is batched
    con = SpyConnection.connect(store_url, autocommit=True)
    con.log = []
    monkeypatch.setattr(store, "PRUNE_BATCH", 1)  # four batches
    try:
        res = store.prune_superseded(con, 7)
    finally:
        con.close()
    assert len(res["runs"]) == 4 and res["bytes"] > 0
    counts_ = [(q, tx) for q, tx in con.log if "count(*)" in q]
    assert counts_ and not [q for q, tx in counts_ if tx]  # no count inside a delete's transaction
    assert len([q for q, _ in counts_ if q.startswith("SELECT count(*) FROM run_position")]) <= 2  # not one per batch
    assert len([q for q, _ in con.log if q.startswith("SELECT pg_total_relation_size")]) <= 7  # a table's size, once


def test_a_big_table_is_taken_at_the_planners_count_not_scanned(store_url, monkeypatch):
    seen: list[str] = []
    with store.connect(store_url) as con:
        real = psycopg.Connection.execute

        def spy(self, query, *a, **kw):
            seen.append(str(query))
            return real(self, query, *a, **kw)

        monkeypatch.setattr(psycopg.Connection, "execute", spy)
        monkeypatch.setattr(store, "EXACT_COUNT_ROWS", 0)  # every table counts as big
        con.execute("ANALYZE run")
        n = store._table_rows(con, "run")
        assert n >= 0 and not [q for q in seen if q.startswith("SELECT count(*)")]  # reltuples only
        monkeypatch.setattr(store, "EXACT_COUNT_ROWS", 10**12)  # none does: counted
        store._table_rows(con, "run")
        assert [q for q in seen if q.startswith("SELECT count(*) FROM run")]


def test_prune_wants_a_connection_in_autocommit_outside_a_transaction(tmp_path, cache, store_url):
    two_passes(tmp_path, cache, store_url)
    age_all(store_url, 30)
    # no autocommit: the whole prune would be one transaction
    with psycopg.connect(store_url) as con, pytest.raises(ValueError, match="autocommit"):
        store.prune_superseded(con, 7)
    with store.connect(store_url) as con, con.transaction(), pytest.raises(ValueError, match="outside a transaction"):
        store.prune_superseded(con, 7, dry_run=True)
    assert counts(store_url)["run"] == 8  # nothing was deleted
    with store.connect(store_url) as con:
        assert len(store.prune_superseded(con, 7)["runs"]) == 4


# ---- the autopilot's step, the settings, the command line

def test_the_autopilot_prunes_in_a_quiet_cycle_and_says_how_much(tmp_path, cache, store_url, caplog):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_budget_s=600, backfill_from="2026-10-01")
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    st = ap.run_cycle(hub=False)  # runs: the first and the second pass of every date
    step = {x["name"]: x for x in st["steps"]}["prune"]
    assert step["status"] == "skipped" and "quiet" in step["detail"]
    assert len(all_runs(store_url)) == 4 + 3  # the 15th has one run (the eod step), the three before it have two
    age_all(store_url, 30)
    st = ap.run_cycle(hub=False)
    step = {x["name"]: x for x in st["steps"]}["prune"]
    assert step["status"] == "ok" and step["detail"].startswith("удалено 3 заменённых прогонов (2026-10-12..2026-10-14), освобождено ~")
    assert "МБ" in step["detail"]
    assert len(all_runs(store_url)) == 4
    assert {x["name"]: x for x in ap.run_cycle(hub=False)["steps"]}["prune"]["detail"] == "no replaced runs to delete"
    assert not plan(store_url, s).todo and not plan(store_url, s).second_todo  # the dates are as they were


def test_the_prune_step_is_off_with_the_setting_and_waits_for_the_retention(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    ap = Autopilot(settings_loader=lambda: settings(tmp_path, cache, store_url, prune_superseded=False,
                                                    backfill_from="2026-10-01"), store_url=store_url,
                   today=lambda: date(2026, 10, 15))
    ap.run_cycle(hub=False)
    names = [x["name"] for x in ap.run_cycle(hub=False)["steps"]]
    assert "prune" not in names
    ap2 = Autopilot(settings_loader=lambda: settings(tmp_path, cache, store_url, backfill_from="2026-10-01"),
                    store_url=store_url, today=lambda: date(2026, 10, 15))
    step = {x["name"]: x for x in ap2.run_cycle(hub=False)["steps"]}["prune"]
    assert step["status"] == "ok" and step["detail"] == "no replaced runs to delete"  # the replacement is minutes old


def test_the_prune_settings(tmp_path):
    from desk.settings import Settings, check_settings_doc, load

    assert Settings().prune_superseded is True and Settings().prune_superseded_days == 7
    assert not check_settings_doc({"prune_superseded": False, "prune_superseded_days": 14})
    assert check_settings_doc({"prune_superseded_days": 0}) == ["prune_superseded_days: must be a whole number >= 1"]
    assert check_settings_doc({"prune_superseded": "no"}) == ["prune_superseded: must be true or false"]
    st = load({"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env"),
               "DESK_PRUNE_SUPERSEDED": "0", "DESK_PRUNE_SUPERSEDED_DAYS": "3"})
    assert st.prune_superseded is False and st.prune_superseded_days == 3


def test_the_cli_prunes_and_compacts(tmp_path, cache, store_url, monkeypatch, capsys):
    from desk import cli

    two_passes(tmp_path, cache, store_url)
    age_all(store_url, 30)
    for k, v in {"DESK_HOME": str(tmp_path / "home"), "DESK_STORE_URL": store_url, "DESK_ENV_FILE": str(tmp_path / "none")}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["prune", "--dry-run"]) == 0
    assert "would delete 4 replaced runs (2026-10-12..2026-10-15)" in capsys.readouterr().out
    assert counts(store_url)["run"] == 8
    assert cli.main(["prune", "--compact"]) == 0
    out = capsys.readouterr().out
    assert "deleted 4 replaced runs" in out and "VACUUM FULL" in out and "the files shrank by" in out
    assert counts(store_url)["run"] == 4
    assert cli.main(["prune"]) == 0 and "no runs replaced 7 days ago or longer" in capsys.readouterr().out


def test_a_pruned_history_is_recomputed_the_same_to_the_last_bit(tmp_path, cache, store_url):
    """Pruning takes away the replaced runs only: a date made again after it reads what a date reads (the default runs
    of the dates before it), so its numbers are what they would have been with the replaced runs still there."""
    s = two_passes(tmp_path, cache, store_url)
    kept = runs(store_url)
    age_all(store_url, 30)
    with store.connect(store_url) as con:
        store.prune_superseded(con, 7)
    load(store_url, tmp_path, "2026-10-14", 12)  # a report: the 14th and the 15th are made again
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    assert res["done"] == ["2026-10-14", "2026-10-15"] and not res["failed"]
    second_pass(s, store_url)
    now = runs(store_url)
    for d in ("2026-10-12", "2026-10-13"):
        assert now[d]["run_id"] == kept[d]["run_id"]
    assert now["2026-10-14"]["run_id"] > kept["2026-10-14"]["run_id"]
    assert now["2026-10-14"]["second_order"] and now["2026-10-15"]["second_order"]
