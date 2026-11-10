"""TDM#628, the history of runs in several processes: the dates of a pass computed at once in a pool, published in the
order of the dates, the runs those of the dates run one after the other, to the last bit. The date's own error comes
back as it would alone, a process that dies costs no date, the budget cuts what has not started, and the carried
spreads (the one thing a date reads of the earlier runs) are read once the dates before it are published. Synthetic
data only (the fixture's market shifted to five business days); the pool is real (``spawn``, as on Windows)."""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_market_history import FIX, STATIC, shifted, write
from test_r23_backfill import DATES, _cycle, load, settings
from test_two_pass_627 import D0, D1, NEWEST_FIRST, RUN_DATES, plan, runs

from desk import backfill, dayrun, parallel, store
from desk.autopilot import Autopilot
from desk.settings import Settings, auto_workers, check_settings_doc
from desk.settings import load as load_settings
from desk.store import MIGRATIONS, migrate


@pytest.fixture
def cache(tmp_path) -> Path:
    c = tmp_path / "cache"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(DATES):
        write(c, shifted(d, len(DATES) - i, light=False))
    return c


def second_store(pg_server) -> str:
    name = "desk_" + uuid.uuid4().hex[:12]
    pg_server.psql(f"CREATE DATABASE {name};")
    url = pg_server.get_uri(name)
    assert migrate(url) == [f.stem for f in sorted(MIGRATIONS.glob("*.sql"))]
    return url


def same_runs(a: dict[str, Any], b: dict[str, Any]) -> None:
    """The default runs of two stores are one calculation: every row, core result, total, log line and lineage (but the
    time the positions were read at, and the run ids)."""
    assert sorted(a) == sorted(b)
    for d, x in a.items():
        y = b[d]
        assert x["second_order"] is y["second_order"]
        assert x["rows"] == y["rows"] and x["sha"] == y["sha"] and x["totals"] == y["totals"], d
        assert x["coverage"] == y["coverage"] and x["prev_date"] == y["prev_date"], d
        assert [m for m in x["log"] if not m.startswith("lineage ")] == [m for m in y["log"] if not m.startswith("lineage ")]
        assert {k: v for k, v in x["lineage"].items() if k != "positions"} == {
            k: v for k, v in y["lineage"].items() if k != "positions"}


def run_order(store_url: str) -> list[str]:
    """The business dates of the runs in the order they were published."""
    with store.connect(store_url) as con:
        return [d.isoformat() for (d,) in con.execute("SELECT business_date FROM run ORDER BY run_id")]


# ---- the same runs as one after the other

def test_the_pool_makes_the_runs_of_the_dates_one_after_the_other_to_the_last_bit(tmp_path, cache, store_url, pg_server):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    first = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3)
    assert first["done"] == RUN_DATES and first["complete"] and not first["failed"] and first["left"] == 0
    assert run_order(store_url) == RUN_DATES  # published in the order of the dates
    assert {r["second_order"] for r in runs(store_url).values()} == {False}
    second = backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3)
    assert second["done"] == NEWEST_FIRST and not second["failed"] and not second["mismatch"] and not second["moved"]
    assert run_order(store_url) == RUN_DATES + NEWEST_FIRST  # the second pass publishes newest first
    p = plan(store_url, s)
    assert not p.todo and not p.second_todo
    other = second_store(pg_server)  # the same, one after the other
    load(other, tmp_path, "2026-10-12", 10)
    s1 = settings(tmp_path, cache, other)
    backfill.run(s1, plan(other, s1), store_url=other, out=lambda line: None)
    backfill.run_second(s1, plan(other, s1), store_url=other, out=lambda line: None)
    assert run_order(other) == RUN_DATES + NEWEST_FIRST
    same_runs(runs(store_url), runs(other))


def test_the_lines_a_run_prints_come_back_in_the_order_of_the_dates(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    lines: list[str] = []
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lines.append, workers=2)
    heads = [x for x in lines if x.startswith("backfill 2026-")]
    assert [x.split(":")[0].split()[1] for x in heads] == RUN_DATES
    for d in RUN_DATES:  # a date's lines follow its own head line
        i = lines.index(next(x for x in heads if x.startswith(f"backfill {d}")))
        assert lines[i + 1].startswith("  ") and any("desk run " in x for x in lines[i + 1:i + 40])


def test_the_budget_cuts_what_has_not_started_and_the_pool_resumes(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, budget_s=0, workers=3)
    assert res["done"] == RUN_DATES[:1] and res["complete"] is False and res["left"] == 3  # one date, as alone
    assert run_order(store_url) == RUN_DATES[:1]  # nothing beyond it was started
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3)
    assert res["done"] == RUN_DATES[1:] and res["complete"] is True
    assert run_order(store_url) == RUN_DATES


def test_a_single_date_and_one_process_run_in_this_process(tmp_path, cache, store_url, monkeypatch):
    """The pool is for several dates and several processes; a date alone, or one worker, is the sequential run."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)

    def no_pool(*a, **kw):
        raise AssertionError("no pool")

    monkeypatch.setattr(parallel, "Pipeline", no_pool)
    p = plan(store_url, s)
    one = backfill.Plan(p.date_from, p.date_to, p.todo[:1], p.hub, p.rows)
    assert backfill.run(s, one, store_url=store_url, out=lambda line: None, workers=3)["done"] == RUN_DATES[:1]
    assert backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=1)["done"] == RUN_DATES[1:]


def test_a_bundle_that_does_not_come_ahead_of_its_date_stalls_the_run_where_the_sequential_run_stalls(
        tmp_path, cache, store_url, monkeypatch):
    """The bundles are downloaded in this process, ahead of the dates in the pool; a download that fails is raised at
    its own date, where a date run alone raises it: the stall, the problem date, the dates before it published."""
    from desk.marketdata.bundle import SnapshotDir
    from desk.marketdata.hub import HubUnavailable

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    rows = [(d, SnapshotDir(cache).market_id(d)) for d in reversed(DATES)]
    ids, hub = dict(rows), tmp_path / "hub"
    hub.mkdir()
    shutil.move(cache / f"{ids['2026-10-15']}.json.gz", hub / f"{ids['2026-10-15']}.json.gz")

    def sync(client, cache_dir, want):
        raise HubUnavailable("hub.test unreachable after 3 attempts: 502 bad gateway", 502)

    monkeypatch.setattr(backfill.cache, "sync", sync)
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    res, _ = _cycle(s, store_url, t0, rows=rows, client=object(), workers=3)
    assert res["done"] == DATES[1:4] and res["stalled"]["phase"] == "bundle" and res["stalled"]["date"] == "2026-10-15"
    assert run_order(store_url) == DATES[1:4]
    res, _ = _cycle(s, store_url, t0 + timedelta(hours=2), rows=rows, client=object(), hub_answered=True, workers=3)
    assert res["stalled"] is None and backfill.state(s)["problems"]["2026-10-15"]["alone"]
    res, _ = _cycle(s, store_url, t0 + timedelta(hours=3), rows=rows, client=object(), workers=3)
    assert [d for d, _ in res["failed"]] == ["2026-10-15"]  # as one after the other: tried every time


def test_a_date_whose_bundle_failed_ahead_does_not_stop_the_dates_after_it_for_good(tmp_path, cache, store_url,
                                                                                    monkeypatch):
    """A date that fails to download on its own (an error that is not the network's) is the date's failure; the dates
    after it, given to the pool once the loop is past it, run."""
    from desk.marketdata.bundle import SnapshotDir

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    rows = [(d, SnapshotDir(cache).market_id(d)) for d in reversed(DATES)]
    ids, hub = dict(rows), tmp_path / "hub"
    hub.mkdir()
    shutil.move(cache / f"{ids['2026-10-13']}.json.gz", hub / f"{ids['2026-10-13']}.json.gz")

    calls: list[list[str]] = []

    def sync(client, cache_dir, want):  # the first download fails, the next (14.10 needs 13.10 as its previous) comes
        calls.append(list(want))
        if len(calls) == 1:
            raise ValueError("a bundle that does not verify")
        shutil.copy(hub / f"{ids['2026-10-13']}.json.gz", cache_dir)

    monkeypatch.setattr(backfill.cache, "sync", sync)
    res, _ = _cycle(s, store_url, datetime(2026, 10, 20, 9, 0, tzinfo=UTC), rows=rows, client=object(), workers=3)
    assert calls[0] == ["2026-10-13"]
    assert [d for d, _ in res["failed"]] == ["2026-10-13"]
    assert res["done"] == ["2026-10-12", "2026-10-14", "2026-10-15"] and res["left"] == 0
    assert run_order(store_url) == ["2026-10-12", "2026-10-14", "2026-10-15"]


# ---- what goes wrong in a process

def break_date(bad: str, how: str) -> None:
    """A pool process's hook: its run of ``bad`` fails (``how``: ``error``, ``unpicklable``, ``die``)."""
    real = dayrun.eod_run

    class Odd(Exception):  # a class a pickle round trip cannot rebuild from its message
        def __init__(self, a, b):
            super().__init__(f"{a}/{b}")

    def run(settings, d, **kw):
        if d == bad:
            if how == "error":
                raise ValueError("bad terms")
            if how == "unpicklable":
                raise Odd(1, 2)
            os._exit(3)  # the process dies, as a kill or an out-of-memory does
        return real(settings, d, **kw)

    dayrun.eod_run = run


def test_a_date_failing_in_its_process_fails_as_it_would_alone_and_the_others_go_on(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3,
                       pool_init=(break_date, ("2026-10-13", "error")))
    assert res["done"] == ["2026-10-12", "2026-10-14", "2026-10-15"]
    assert res["failed"] == [("2026-10-13", "ValueError: bad terms")] and res["left"] == 0
    assert run_order(store_url) == ["2026-10-12", "2026-10-14", "2026-10-15"]  # the failed date is not in the order
    assert [d.date for d in plan(store_url, s).days if d.status == "failed"] == ["2026-10-13"]  # not tried again as is
    assert backfill.state(s)["problems"]["2026-10-13"]["kind"] == "failed"


def test_an_error_that_cannot_be_sent_back_comes_as_the_dates_own_error(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                       pool_init=(break_date, ("2026-10-14", "unpicklable")))
    assert [d for d, _ in res["failed"]] == ["2026-10-14"] and "Odd: 1/2" in res["failed"][0][1]
    assert res["done"] == ["2026-10-12", "2026-10-13", "2026-10-15"]


def test_a_process_that_dies_costs_no_date(tmp_path, cache, store_url, pg_server):
    """The pool is closed, the date that was in it and the dates after it run one after the other, here: the runs are
    the sequential run's."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                       pool_init=(break_date, ("2026-10-13", "die")))
    assert res["done"] == RUN_DATES and not res["failed"] and res["left"] == 0 and res["complete"]
    assert run_order(store_url) == RUN_DATES
    other = second_store(pg_server)
    load(other, tmp_path, "2026-10-12", 10)
    s1 = settings(tmp_path, cache, other)
    backfill.run(s1, plan(other, s1), store_url=other, out=lambda line: None)
    same_runs(runs(store_url), runs(other))
    assert not plan(store_url, s).todo


def test_the_second_pass_in_the_pool_leaves_a_failing_date_and_a_dead_process_costs_nothing(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    res = backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                              pool_init=(break_date, ("2026-10-14", "error")))
    assert res["done"] == ["2026-10-15", "2026-10-13", "2026-10-12"] and res["failed"] == [("2026-10-14", "ValueError: bad terms")]
    s2 = settings(tmp_path, cache, store_url)
    res = backfill.run_second(s2, plan(store_url, s2), store_url=store_url, out=lambda line: None, workers=2,
                              pool_init=(break_date, ("2026-10-14", "die")))
    assert res["done"] == ["2026-10-14"] and res["failed"] == []  # the process died, the date ran in this one
    assert not plan(store_url, s).second_todo


# ---- the order: the carried spreads and the turns

def spy_carried(path: str) -> None:
    """A pool process's hook: every read of the carried spreads writes (date, how many) to ``path``."""
    real = store.carried_spreads

    def read(con, before):
        out = real(con, before)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{before.isoformat()} {len(out)}\n")
        return out

    store.carried_spreads = read


def test_a_date_reads_the_carried_spreads_of_the_dates_before_it_as_in_the_sequential_run(tmp_path, cache, store_url,
                                                                                         pg_server, monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    log = tmp_path / "carried.log"
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3,
                 pool_init=(spy_carried, (str(log),)))
    parallel_reads = dict(x.split() for x in log.read_text().splitlines())
    other = second_store(pg_server)
    load(other, tmp_path, "2026-10-12", 10)
    s1 = settings(tmp_path, cache, other)
    seq_log = tmp_path / "carried_seq.log"
    real = store.carried_spreads

    def spy(con, before):
        out = real(con, before)
        with open(seq_log, "a", encoding="utf-8") as f:
            f.write(f"{before.isoformat()} {len(out)}\n")
        return out

    monkeypatch.setattr(store, "carried_spreads", spy)
    backfill.run(s1, plan(other, s1), store_url=other, out=lambda line: None)
    seq_reads = dict(x.split() for x in seq_log.read_text().splitlines())
    assert sorted(parallel_reads) == sorted(seq_reads) == RUN_DATES
    assert parallel_reads == seq_reads  # the same spreads: the dates before it were published first
    assert int(seq_reads[RUN_DATES[0]]) == 0 and int(seq_reads[RUN_DATES[-1]]) > 0  # and they are not all empty


def test_the_gate_keeps_the_order_of_the_dates():
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    events: list[str] = []
    gates = [parallel.Gate(shared, i) for i in range(4)]

    def date(i: int, work: float, fail: bool = False) -> None:
        time.sleep(work)
        events.append(f"{i} computed")
        try:
            if fail:
                raise ValueError("boom")
            gates[i].before_publish()
            events.append(f"{i} published")
        except ValueError:
            pass
        finally:
            gates[i].finish()

    # the later dates compute faster: they wait for their turn, a failing date does not hold the others up
    ts = [threading.Thread(target=date, args=(i, w, i == 1)) for i, w in ((0, 0.6), (1, 0.3), (2, 0.1), (3, 0.0))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    published = [e for e in events if e.endswith("published")]
    assert published == ["0 published", "2 published", "3 published"]
    assert events.index("3 computed") < events.index("0 published") < events.index("2 published")
    assert shared.done.value == 4


def test_the_carried_spreads_are_read_at_the_first_lookup_and_after_the_turn():
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    reads: list[str] = []
    g0, g1 = parallel.Gate(shared, 0), parallel.Gate(shared, 1)
    lazy0 = parallel.LazyCarried(lambda: reads.append("0") or {"A": 1})
    assert not lazy0.loaded and reads == []
    assert lazy0.get("A") == 1 and lazy0.get("B") is None and len(lazy0) == 1 and dict(lazy0) == {"A": 1} and reads == ["0"]

    class Con:  # a connection whose carried spreads are read at its date's turn
        pass

    import desk.parallel as mod

    real = mod.store.carried_spreads
    mod.store.carried_spreads = lambda con, before: reads.append(f"read before {before}") or {"X": 1}
    try:
        lazy = g1.carried(Con(), date(2026, 10, 14))
        assert not lazy.loaded
        got: list[Any] = []
        t = threading.Thread(target=lambda: got.append(lazy.get("X")))
        t.start()
        time.sleep(0.3)
        assert t.is_alive() and "read before 2026-10-14" not in reads  # the date before it is not finished
        g0.finish()
        t.join(5)
        assert got == [1] and "read before 2026-10-14" in reads
        free = parallel.Gate(shared, 7, wait_carried=False).carried(Con(), date(2026, 10, 9))
        assert free.get("X") == 1  # the second pass's reads wait for nobody
    finally:
        mod.store.carried_spreads = real


def test_a_closed_pipeline_lets_the_waiting_dates_go():
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    g = parallel.Gate(shared, 5)
    err: list[BaseException] = []

    def wait() -> None:
        try:
            g.wait_turn()
        except parallel.Aborted as e:
            err.append(e)

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.2)
    with shared.cond:
        shared.abort.value = 1
        shared.cond.notify_all()
    t.join(5)
    assert len(err) == 1 and not t.is_alive()
    g.finish()  # nothing to wait for, nothing advanced
    assert shared.done.value == 0


# ---- settings, the autopilot

def test_the_settings_of_the_processes(tmp_path, monkeypatch):
    assert Settings().backfill_workers == 1  # a Settings made by hand is the sequential run
    assert 1 <= auto_workers() <= 3
    monkeypatch.setattr(os, "cpu_count", lambda: 1)
    assert auto_workers() == 1
    monkeypatch.setattr(os, "cpu_count", lambda: 4)
    assert auto_workers() == 3
    monkeypatch.setattr(os, "cpu_count", lambda: 64)
    assert auto_workers() == 3  # the owner's computer stays responsive
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    assert load_settings(env).backfill_workers == 3
    assert load_settings({**env, "DESK_BACKFILL_WORKERS": "2"}).backfill_workers == 2
    assert not check_settings_doc({"backfill_workers": 2})
    assert check_settings_doc({"backfill_workers": 0}) == ["backfill_workers: must be a whole number >= 1"]


def test_the_autopilot_computes_the_history_in_the_pool_and_gets_the_same_runs(tmp_path, cache, store_url, pg_server):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_budget_s=600, backfill_from="2026-10-01", backfill_workers=2)
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    st = ap.run_cycle(hub=False)
    b = next(x for x in st["steps"] if x["name"] == "backfill")
    assert b["status"] == "ok" and "ran 3 dates (2026-10-12..2026-10-14)" in b["detail"], b["detail"]
    assert "second derivatives of 3 dates (2026-10-14..2026-10-12)" in b["detail"]
    other = second_store(pg_server)
    load(other, tmp_path, "2026-10-12", 10)
    load(other, tmp_path, "2026-10-15", 11)
    s1 = settings(tmp_path, cache, other, backfill_budget_s=600, backfill_from="2026-10-01")
    Autopilot(settings_loader=lambda: s1, store_url=other, today=lambda: date(2026, 11, 30)).run_cycle(hub=False)
    same_runs(runs(store_url), runs(other))
    assert D0 < D1
