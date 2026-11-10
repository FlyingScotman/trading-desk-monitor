"""TDM#628, the history of runs in several processes: the dates of a pass computed at once in a pool, published in the
order of the dates, the runs those of the dates run one after the other, to the last bit. The date's own error comes
back as it would alone, a process that dies costs no date, the budget cuts what has not started, and the carried
spreads (the one thing a date reads of the earlier runs) are read once the dates before it are published. Synthetic
data only (the fixture's market shifted to five business days); the pool is real (``spawn``, as on Windows)."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row
from test_market_history import FIX, STATIC, shifted, write
from test_r23_backfill import DATES, _cycle, load, settings
from test_two_pass_627 import D0, D1, NEWEST_FIRST, RUN_DATES, plan, runs

from desk import backfill, bookfile, dayrun, parallel, store
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

    t = threading.Thread(target=wait, daemon=True)
    t.start()
    time.sleep(0.2)
    shared.abort.value = 1  # all close() does here: no lock, no condition for a dead process to hold
    t.join(5)
    assert len(err) == 1 and not t.is_alive()
    g.finish()  # nothing to wait for, nothing advanced
    assert shared.done.value == 0


def test_a_dropped_date_does_not_publish_and_does_not_hold_the_others_up():
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    g0, g1, g2 = (parallel.Gate(shared, i) for i in range(3))
    assert not g1.skipped()
    shared.skip[1] = 1
    shared.used = 3  # what ``Pipeline.submit`` keeps: the flags the next ``reset`` clears
    assert g1.skipped() and not g0.skipped() and not g2.skipped()
    with pytest.raises(parallel.Skipped):
        g1.before_publish()
    g0.before_publish()
    g0.finish()
    g1.finish()  # waits for its turn, then lets the next one through
    g2.before_publish()
    g2.finish()
    assert shared.done.value == 3
    shared.reset()  # the next pass on the same processes starts clean
    assert shared.done.value == 0 and not parallel.Gate(shared, 1).skipped()


def test_a_date_that_waits_for_its_turn_gives_up_when_its_turn_cannot_come(monkeypatch):
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    monkeypatch.setattr(parallel, "TURN_TIMEOUT_S", 0.3)
    with pytest.raises(parallel.Aborted, match="did not come"):
        parallel.Gate(shared, 2).wait_turn()


def test_a_failed_read_of_the_carried_spreads_is_remembered_and_raised_at_every_lookup():
    calls: list[int] = []

    def load() -> dict[str, Any]:
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("the store hiccuped")
        return {"A": 1}

    lazy = parallel.LazyCarried(load)
    assert lazy.error is None
    with pytest.raises(ValueError, match="hiccuped"):
        lazy.get("A")
    assert isinstance(lazy.error, ValueError) and not lazy.loaded
    with pytest.raises(ValueError, match="hiccuped"):  # not read again: the run is the one that failed
        len(lazy)
    assert len(calls) == 1


# ---- a process that dies while the others wait for their turn: nothing waits for ever

def _gate_job(seq: int) -> int:
    g = parallel.Gate(parallel._SHARED, seq)
    if seq == 0:
        time.sleep(2.0)  # seq 1 and 2 are asleep in wait_turn meanwhile
        os._exit(3)  # the process of the first date dies (kill, out of memory)
    g.wait_turn()
    return seq


def _gate_init(shared: parallel.Shared) -> None:
    parallel._SHARED = shared


def test_a_process_that_dies_while_the_others_wait_for_their_turn_does_not_hang_the_close():
    """TDM#628 review: the order was kept with a ``multiprocessing.Condition``; ``notify_all`` of a closing pipeline waits
    for every sleeper, and the sleepers were killed with the broken pool: the close never returned."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool

    ctx = multiprocessing.get_context("spawn")
    shared = parallel.Shared(ctx)
    pool = ProcessPoolExecutor(max_workers=3, mp_context=ctx, initializer=_gate_init, initargs=(shared,))
    futs = [pool.submit(_gate_job, i) for i in range(3)]
    with pytest.raises(BrokenProcessPool):
        futs[0].result(timeout=60)
    closed = threading.Event()

    def close() -> None:
        shared.abort.value = 1
        closed.set()

    threading.Thread(target=close, daemon=True).start()
    assert closed.wait(10), "closing the pipeline hung on the sleepers of a dead pool"
    pool.shutdown(wait=False, cancel_futures=True)


def die_late(bad: str, after_s: float) -> None:
    """A pool process's hook: the run of ``bad`` dies (``os._exit``, as an out-of-memory kill) ``after_s`` seconds in, the
    later dates meanwhile computed and waiting for their turn."""
    real = dayrun.eod_run

    def run(settings, d, **kw):
        if d == bad:
            time.sleep(after_s)
            os._exit(3)
        return real(settings, d, **kw)

    dayrun.eod_run = run


def in_thread(fn, limit_s: float):
    """``fn()`` in a thread, its result, or a failure of the test when it does not return in ``limit_s``."""
    box: list[Any] = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(limit_s)
    assert not t.is_alive(), f"did not return in {limit_s} s"
    return box[0]


def test_a_process_that_dies_in_the_middle_while_later_dates_wait_costs_no_date_and_the_call_returns(
        tmp_path, cache, store_url, pg_server):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = in_thread(lambda: backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3,
                                         pool_init=(die_late, ("2026-10-13", 12.0))), 300)
    assert res["done"] == RUN_DATES and not res["failed"] and res["left"] == 0 and res["complete"]
    assert run_order(store_url) == RUN_DATES  # once each, in the order of the dates
    other = second_store(pg_server)
    load(other, tmp_path, "2026-10-12", 10)
    s1 = settings(tmp_path, cache, other)
    backfill.run(s1, plan(other, s1), store_url=other, out=lambda line: None)
    same_runs(runs(store_url), runs(other))


def hang_date(bad: str) -> None:
    real = dayrun.eod_run

    def run(settings, d, **kw):
        if d == bad:
            time.sleep(3600)  # a process that does not answer
        return real(settings, d, **kw)

    dayrun.eod_run = run


def test_a_process_that_does_not_answer_is_ended_after_the_timeout_and_the_dates_run_one_after_the_other(
        tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    monkeypatch.setattr(parallel, "RESULT_TIMEOUT_S", 40.0)
    monkeypatch.setattr(parallel, "CLOSE_GRACE_S", 5.0)
    t0 = time.monotonic()
    res = in_thread(lambda: backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                                         pool_init=(hang_date, ("2026-10-13",))), 400)
    assert res["done"] == RUN_DATES and not res["failed"] and res["left"] == 0
    assert run_order(store_url) == RUN_DATES
    assert time.monotonic() - t0 < 300


def late_death(plan_: tuple) -> None:
    """A pool process's hook: ``plan_`` is ((date, seconds, what), ...): after the date's run is published the process
    waits ``seconds`` and then ``exit``s (dies) or ``sleep``s (does not return)."""
    real = dayrun.eod_run
    by_date = {d: (sec, what) for d, sec, what in plan_}

    def run(settings, d, **kw):
        rid = real(settings, d, **kw)
        if d in by_date:
            sec, what = by_date[d]
            time.sleep(sec)
            if what == "exit":
                os._exit(3)
            time.sleep(3600)
        return rid

    dayrun.eod_run = run


def test_a_date_a_process_published_before_the_pool_broke_is_not_made_again(tmp_path, cache, store_url):
    """The process of 13.10 dies after it published, the process of 14.10 published and never answers: both futures are
    lost with the pool, the runs are in the store. Neither date is made a second time."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    hook = (late_death, ((("2026-10-13", 8.0, "exit"), ("2026-10-14", 1.0, "sleep")),))
    res = in_thread(lambda: backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=3,
                                         pool_init=hook), 300)
    assert res["done"] == RUN_DATES and not res["failed"] and res["left"] == 0
    assert run_order(store_url) == RUN_DATES  # not one run twice
    assert not plan(store_url, s).todo


def test_the_second_pass_does_not_make_again_a_run_a_process_published_before_the_pool_broke(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    hook = (late_death, ((("2026-10-14", 8.0, "exit"), ("2026-10-13", 1.0, "sleep")),))
    res = in_thread(lambda: backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None,
                                                workers=3, pool_init=hook), 300)
    assert res["done"] == NEWEST_FIRST and not res["failed"] and not res["moved"]
    assert run_order(store_url) == RUN_DATES + NEWEST_FIRST  # one new run of each date
    assert not plan(store_url, s).second_todo


# ---- a bond with no price of its own: its model price carries the spread of an earlier run

NOQ = "RU000A0JXQ44"  # a corporate fixed-rate RUB bond: its quote is dropped on every date but the first
ALL_NOQ = ["2026-09-01", *DATES]


@pytest.fixture
def cache_noq(tmp_path) -> Path:
    """The market of ``cache`` from 01.09 too, and ``NOQ`` priced on the first date only (38 days before the others: no
    stale price, a carried Z): its model price on the later dates carries the spread of the run before it."""
    c = tmp_path / "cache_noq"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(ALL_NOQ):
        doc = shifted(d, len(ALL_NOQ) - i, light=False)
        if d != ALL_NOQ[0]:
            doc["data"]["bond_quotes"] = [q for q in doc["data"]["bond_quotes"] if q["secid"] != NOQ]
        write(c, doc)
    return c


def load_noq(url: str, tmp_path: Path, d: str, qty: int) -> None:
    f = tmp_path / uuid.uuid4().hex[:8] / f"book{d}.csv"  # the same file name in every store: it is in the lineage
    f.parent.mkdir()
    f.write_text("position_id,book_id,secid,quantity_bonds\n"
                 f"P1,OFZ,SU26238RMFS4,{qty}\nP2,CORP,RU000A10BRM5,3\nP3,CORP,{NOQ},7\nP4,CORP,RU000A100GW5,2\n",
                 encoding="utf-8")
    with store.connect(url) as con:
        bookfile.load_book(con, f, d, out=lambda s: None)


def _strip(x: Any) -> Any:
    """Without the time the positions were read at (``known_at``): the one thing two stores loaded apart differ in."""
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k != "known_at"}
    if isinstance(x, list):
        return [_strip(v) for v in x]
    return x


def _logline(m: str) -> str:
    return "lineage " + json.dumps(_strip(json.loads(m[8:])), sort_keys=True) if m.startswith("lineage ") else m


def dump(url: str) -> list[dict[str, Any]]:
    """Every run of the store, run after run: header, every row, model spreads (with their source text), references,
    core result hashes and the log lines."""
    out = []
    with store.connect(url) as con:
        cur = con.cursor(row_factory=dict_row)
        for r in cur.execute("SELECT * FROM run ORDER BY run_id").fetchall():
            rid = r["run_id"]
            hdr = {k: v for k, v in r.items() if k not in ("started_at", "completed_at", "positions_known_at",
                                                           "backend_git_sha")}
            hdr["lineage"] = json.dumps(_strip(dict(hdr.pop("lineage"))), sort_keys=True, default=str)
            out.append({
                "hdr": json.dumps(hdr, sort_keys=True, default=str),
                "rows": cur.execute("SELECT * FROM run_position WHERE run_id = %s ORDER BY ord", (rid,)).fetchall(),
                "ms": cur.execute("SELECT * FROM model_spread WHERE run_id = %s ORDER BY secid", (rid,)).fetchall(),
                "ref": cur.execute("SELECT kind, data FROM run_reference WHERE run_id = %s ORDER BY kind",
                                   (rid,)).fetchall(),
                "cr": cur.execute("SELECT result_sha256, label FROM run_core_result WHERE run_id = %s ORDER BY 1",
                                  (rid,)).fetchall(),
                "log": [_logline(m) for (m,) in con.execute(
                    "SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))]})
    return out


def differences(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[str]:
    """Where two dumps differ: the run, the part of it and the first thing that is not the same."""
    out = [f"{len(a)} runs against {len(b)}"] if len(a) != len(b) else []
    for i, (x, y) in enumerate(zip(a, b, strict=False)):
        for k in x:
            if x[k] != y[k]:
                if isinstance(x[k], list):
                    first = next(((u, v) for u, v in zip(x[k], y[k], strict=False) if u != v), (len(x[k]), len(y[k])))
                    out.append(f"run {i} {k}: {str(first)[:600]}")
                else:
                    j = next((n for n, (u, v) in enumerate(zip(x[k], y[k], strict=False)) if u != v), 0)
                    out.append(f"run {i} {k}: ...{x[k][max(0, j - 80):j + 120]} / ...{y[k][max(0, j - 80):j + 120]}")
    return out


def no_gate() -> None:
    """A pool process's hook (the control): the carried spreads are read at once, as if no order were kept."""
    def carried(self, con, run_date):
        return store.carried_spreads(con, before=run_date)

    parallel.Gate.carried = carried


def history_in(pg_server, tmp_path: Path, cache_dir: Path, workers: int, init: Any = None) -> list[dict[str, Any]]:
    """Both passes of the history of a store with the books of 01.09 and 14.10 (a second report: the dates after it read
    other positions), ``workers`` processes."""
    url = second_store(pg_server)
    load_noq(url, tmp_path, "2026-09-01", 10)
    load_noq(url, tmp_path, "2026-10-14", 12)
    s = Settings(home=tmp_path / f"home_{uuid.uuid4().hex[:6]}", snapshot_cache=cache_dir, store_url=url,
                 backfill_workers=workers)

    def plan_() -> backfill.Plan:
        with store.connect(url) as con:
            return backfill.plan(con, s, date(2026, 8, 25), date(2026, 10, 15))

    r1 = backfill.run(s, plan_(), store_url=url, out=lambda line: None, workers=workers, pool_init=init)
    r2 = backfill.run_second(s, plan_(), store_url=url, out=lambda line: None, workers=workers, pool_init=init)
    assert not r1["failed"] and not r2["failed"] and not r2["moved"]
    if init is None:  # (the control's second pass reads other spreads than its first: its totals may well differ)
        assert not r2["mismatch"]
    return dump(url)


def test_a_model_price_carries_the_same_spread_text_and_all_in_the_pool_as_one_after_the_other(
        tmp_path, cache_noq, pg_server):
    """Review of TDM#628: a bond whose quote is on the first date only is valued by model on the later dates with the
    spread of the run before it (``model_spread``, ``run_position.fact``, the source text names the date and the run). The
    pool reads it once the dates before are published, so both passes are one calculation to the last bit; with the order
    off (the control) the later dates read an older run's spread and the runs differ."""
    seq = history_in(pg_server, tmp_path, cache_noq, 1)
    model = [(r["hdr"], row["pv_rub"]) for r in seq for row in r["rows"]
             if row["secid"] == NOQ and row["quality"] == "model_price"]
    sources = {m["source"] for r in seq for m in r["ms"] if m["secid"] == NOQ}
    assert len(model) >= 8 and sources and all("carried" in x or "last market Z" in x or "Z" in x for x in sources), \
        "the scenario must make the model price from a carried spread, or the check proves nothing"
    par = history_in(pg_server, tmp_path, cache_noq, 3)
    assert not differences(seq, par), differences(seq, par)[:5]
    off = history_in(pg_server, tmp_path, cache_noq, 3, init=(no_gate, ()))
    assert differences(seq, off), "the control: without the gate the pool must read other spreads, else the test shows nothing"


def make_flaky(bad: str):
    """``store.carried_spreads`` that fails once, for date ``bad`` (the connection dropped), then works."""
    real = store.carried_spreads
    state = {"failed": False}

    def read(con, before):
        if before.isoformat() == bad and not state["failed"]:
            state["failed"] = True
            raise psycopg.OperationalError("the store dropped the connection")
        return real(con, before)

    return read


def flaky_carried(bad: str) -> None:
    store.carried_spreads = make_flaky(bad)


def test_a_store_error_reading_the_carried_spreads_fails_the_date_in_the_pool_as_it_does_one_after_the_other(
        tmp_path, cache_noq, pg_server, monkeypatch):
    """The first lookup of the spreads is inside the fallback ladder, which values a bond whose data fail at par proxy;
    an error of the store there is not the bond's: the date fails (here: a stall of the store), it is not published
    with a par proxy because the next read of the spreads worked."""
    bad = "2026-10-14"

    def one(workers: int, init: Any, patch: bool) -> tuple[dict[str, Any], list[str]]:
        url = second_store(pg_server)
        load_noq(url, tmp_path, "2026-09-01", 10)
        load_noq(url, tmp_path, "2026-10-14", 12)
        s = Settings(home=tmp_path / f"home_{uuid.uuid4().hex[:6]}", snapshot_cache=cache_noq, store_url=url)
        with store.connect(url) as con:
            p = backfill.plan(con, s, date(2026, 8, 25), date(2026, 10, 15))
        if patch:
            monkeypatch.setattr(store, "carried_spreads", make_flaky(bad))
        res = backfill.run(s, p, store_url=url, out=lambda line: None, workers=workers, pool_init=init,
                           now=datetime(2026, 10, 20, 9, 0, tzinfo=UTC))
        monkeypatch.undo()
        return res, run_order(url)

    seq, seq_order = one(1, None, True)
    par, par_order = one(3, (flaky_carried, (bad,)), False)
    assert [d for d, _ in seq["failed"]] == [bad] and "OperationalError" in seq["failed"][0][1]
    assert seq["stalled"] and seq["stalled"]["date"] == bad and bad not in seq_order
    assert [d for d, _ in par["failed"]] == [bad] and "OperationalError" in par["failed"][0][1]
    assert par["stalled"] and par["stalled"]["date"] == bad
    assert bad not in par_order and [d for d in par_order if d < bad] == [d for d in seq_order if d < bad]
    # (a date already in flight after it, 15.10, may be published: its process does not wait for the failed date's)


# ---- the pool that cannot start, the pool that serves several passes, the dropped date

def test_a_pool_that_cannot_start_is_the_sequential_run_with_a_warning(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)

    def no_semaphores(*a, **kw):
        raise OSError(28, "No space left on device (semaphores)")

    monkeypatch.setattr(parallel, "Pipeline", no_semaphores)
    lines: list[str] = []
    first = backfill.run(s, plan(store_url, s), store_url=store_url, out=lines.append, workers=3)
    assert first["done"] == RUN_DATES and not first["failed"] and first["complete"]
    assert any(x.startswith("WARN backfill: the processes cannot be started (OSError") and "one after the other" in x
               for x in lines)
    lines.clear()
    second = backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lines.append, workers=3)
    assert second["done"] == NEWEST_FIRST and not second["failed"]
    assert any(x.startswith("WARN backfill: the processes cannot be started") for x in lines)
    assert run_order(store_url) == RUN_DATES + NEWEST_FIRST


def test_one_set_of_processes_serves_both_passes_and_starts_again_after_a_broken_pass(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    pool = parallel.Pool(2)
    try:
        backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2, pool=pool)
        ex = pool.executor
        assert ex is not None  # kept for the next pass
        pids = {p.pid for p in ex._processes.values()}
        res = backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                                  pool=pool)
        assert res["done"] == NEWEST_FIRST and pool.executor is ex  # the same processes
        assert {p.pid for p in pool.executor._processes.values()} == pids
        assert run_order(store_url) == RUN_DATES + NEWEST_FIRST
    finally:
        pool.close()
    assert pool.executor is None


def test_a_pool_whose_pass_broke_is_replaced_for_the_next_pass(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    pool = parallel.Pool(2, init=(break_date, ("2026-10-13", "die")))
    try:
        res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2, pool=pool)
        assert res["done"] == RUN_DATES and pool.executor is None  # the broken processes are gone
        res = backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                                  pool=pool)  # a new set starts (its hook kills 13.10 again: the rest runs here)
        assert res["done"] == NEWEST_FIRST and not res["failed"] and pool.executor is None
    finally:
        pool.close()


def test_a_dropped_date_is_not_published_and_the_dates_after_it_go_on(tmp_path, cache, store_url):
    """``Pipeline.discard``: the date the run passed over (as the sequential run passes over it) is not computed when it has
    not started and not published when it has; the order goes on without it."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    pipe = parallel.Pipeline(s, 1, store_url=store_url, snapshots=str(cache), no_gamma=True, wait_carried=True)
    try:
        for d in RUN_DATES[:3]:
            pipe.submit(d)
        pipe.discard(RUN_DATES[1])  # one process: it is queued behind the first
        assert not pipe.has(RUN_DATES[1]) and pipe.pending == 2
        assert pipe.result(RUN_DATES[0])["rid"]
        assert pipe.result(RUN_DATES[2])["rid"]  # its turn came although the date before it was dropped
        assert pipe.peak_mb > 0  # the most a process held, from the answers
    finally:
        pipe.close()
    assert run_order(store_url) == [RUN_DATES[0], RUN_DATES[2]]


def nice_to_file(path: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{os.nice(0)}\n")


def test_the_processes_of_the_pool_work_below_the_owners_programs(tmp_path, cache, store_url, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX niceness")
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    mine = os.nice(0)
    out = tmp_path / "nice.log"
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                 pool_init=(nice_to_file, (str(out),)))
    assert {int(x) for x in out.read_text().split()} == {mine + parallel.WORKER_NICE}


def test_the_priority_is_lowered_per_platform_and_a_failure_is_not_fatal(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(parallel.sys, "platform", "linux")
    monkeypatch.setattr(os, "nice", lambda n: calls.append(n))
    parallel._lower_priority()
    assert calls == [parallel.WORKER_NICE]

    def refuse(n):
        raise PermissionError("no")

    monkeypatch.setattr(os, "nice", refuse)
    parallel._lower_priority()  # not fatal
    monkeypatch.setattr(parallel.sys, "platform", "win32")  # no windll here: the failure is swallowed as well
    parallel._lower_priority()


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
