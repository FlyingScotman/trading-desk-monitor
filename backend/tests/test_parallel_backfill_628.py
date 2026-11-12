"""TDM#628, the history of runs in several processes: the dates of a pass computed at once in a pool, published in the
order of the dates, the runs those of the dates run one after the other, to the last bit. The date's own error comes
back as it would alone, a process that dies costs no date, the budget cuts what has not started, and the carried
spreads (the one thing a date reads of the earlier runs) are read once the dates before it are published. Synthetic
data only (the fixture's market shifted to five business days); the pool is real (``spawn``, as on Windows)."""

from __future__ import annotations

import json
import os
import re
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
from desk.settings import Settings, check_settings_doc
from desk.settings import load as load_settings
from desk.store import MIGRATIONS, migrate

# The tests start process pools of their own and measure how long a stuck or dead process takes to be seen: they run on their
# own, after the parallel run (pyproject.toml, marker serial).
pytestmark = pytest.mark.serial


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
    shared.used = 4
    events: list[str] = []
    gates = [parallel.Gate(shared, i) for i in range(4)]

    def date(i: int, work: float) -> None:
        time.sleep(work)
        events.append(f"{i} computed")
        gates[i].before_publish()
        events.append(f"{i} published")
        gates[i].finish()

    # the later dates compute faster: they wait for their turn
    ts = [threading.Thread(target=date, args=(i, w)) for i, w in ((0, 0.6), (1, 0.3), (2, 0.1), (3, 0.0))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(10)
    published = [e for e in events if e.endswith("published")]
    assert published == ["0 published", "1 published", "2 published", "3 published"]
    assert events.index("3 computed") < events.index("0 published")
    assert shared.done_count() == 4


def test_a_date_that_failed_holds_the_turn_until_the_starting_process_hands_it_on():
    """TDM#628 second review: a date that fails does not finish by itself. The sequential run goes on to the next date
    without it, so the next date reads the spreads and publishes without it only when the caller says it goes on
    (``Pipeline.release``); when the caller stops there it closes the pipeline instead, and the next date does not publish."""
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    shared.used = 3
    gates = [parallel.Gate(shared, i) for i in range(3)]
    published: list[int] = []
    errors: list[BaseException] = []

    def date(i: int) -> None:
        try:
            gates[i].before_publish()
            published.append(i)
            gates[i].finish()
        except parallel.Aborted as e:
            errors.append(e)

    gates[0].before_publish()
    gates[0].finish()
    # date 1 failed (it never finishes); dates 2 and 3 wait
    t2 = threading.Thread(target=date, args=(2,), daemon=True)
    t2.start()
    time.sleep(0.5)
    assert t2.is_alive() and published == [] and shared.done_count() == 1
    shared.fin[1] = 1  # the caller goes on to the next date: ``Pipeline.release``
    t2.join(10)
    assert published == [2] and shared.done_count() == 3
    # the other ending: the caller stops, the date whose turn comes later does not publish
    shared.reset()
    shared.used = 3
    g = parallel.Gate(shared, 1)
    t = threading.Thread(target=lambda: errors.append(_raises(g.before_publish)), daemon=True)
    t.start()
    time.sleep(0.3)
    shared.abort.value = 1
    t.join(10)
    assert not t.is_alive() and isinstance(errors[-1], parallel.Aborted)


def _raises(fn) -> BaseException | None:
    try:
        fn()
    except BaseException as e:  # noqa: BLE001 - the test looks at which
        return e
    return None


def test_a_closed_pipeline_stops_a_date_whose_turn_has_come_before_it_publishes():
    """TDM#628 second review: the abort flag was looked at only while a date waited for its turn; a date whose turn had
    come published after the pass was stopped."""
    import multiprocessing

    shared = parallel.Shared(multiprocessing.get_context("spawn"))
    g = parallel.Gate(shared, 0)
    g.wait_turn()  # the turn is there (the carried spreads were read)
    shared.abort.value = 1
    with pytest.raises(parallel.Aborted):
        g.before_publish()
    with pytest.raises(parallel.Aborted):
        parallel.Gate(shared, 0).before_publish()


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
    g1.finish()  # (``run_date`` does it for a date that found the flag) lets the next one through
    g2.before_publish()
    g2.finish()
    assert shared.done_count() == 3
    shared.reset()  # the next pass on the same processes starts clean
    assert shared.done_count() == 0 and not parallel.Gate(shared, 1).skipped()


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
    from multiprocessing.connection import wait as mp_wait

    ctx = multiprocessing.get_context("spawn")
    shared = parallel.Shared(ctx)
    pool = ProcessPoolExecutor(max_workers=3, mp_context=ctx, initializer=_gate_init, initargs=(shared,))
    for i in range(3):
        pool.submit(_gate_job, i)
    # the death is read from the sentinels, as ``Pipeline.result`` does: the executor itself may not notice a process
    # (CPython 3.11, ``spawn``), and a test that waits for its BrokenProcessPool fails in one run of four
    assert mp_wait([p.sentinel for p in pool._processes.values()], timeout=60), "the process of the first date lives"
    closed = threading.Event()

    def close() -> None:
        shared.abort.value = 1
        closed.set()

    threading.Thread(target=close, daemon=True).start()
    assert closed.wait(10), "closing the pipeline hung on the sleepers of a dead pool"
    pool.shutdown(wait=False, cancel_futures=True)


def test_a_dead_process_is_seen_from_its_sentinel_when_the_executor_does_not_notice(monkeypatch):
    """TDM#628 second review: with ``spawn`` CPython 3.11's executor may not watch the process started last, and the
    answer of a date never came (the call waited ``RESULT_TIMEOUT_S``, half an hour). ``Pipeline.result`` looks at the
    processes every ``CHECK_S``. Here the future never answers at all, and a process of the executor is gone."""
    import multiprocessing
    import types
    from concurrent.futures import Future

    proc = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(0.3,))
    proc.start()
    pipe = parallel.Pipeline.__new__(parallel.Pipeline)
    pipe._executor = types.SimpleNamespace(_processes={proc.pid: proc})
    pipe._futures, pipe._seqs, pipe._taken = {"2026-10-12": Future()}, {"2026-10-12": 0}, {}
    pipe.timeout_s, pipe.broken = 120.0, False
    closed: list[bool] = []
    pipe.close = lambda kill=False: closed.append(kill)
    monkeypatch.setattr(parallel, "CHECK_S", 0.2)
    t0 = time.monotonic()
    try:
        with pytest.raises(parallel.PoolBroken, match="died"):
            pipe.result("2026-10-12")
    finally:
        proc.join(10)
    assert time.monotonic() - t0 < 30 and pipe.broken and closed == [True]  # the pool is ended at once


def test_a_live_pool_is_not_taken_for_dead_while_a_date_takes_long(monkeypatch):
    """The look at the processes every ``CHECK_S`` does not cut a date that is only slow; the timeout still ends one that
    does not answer."""
    import multiprocessing
    import types
    from concurrent.futures import Future

    proc = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(60,))
    proc.start()
    try:
        pipe = parallel.Pipeline.__new__(parallel.Pipeline)
        pipe._executor = types.SimpleNamespace(_processes={proc.pid: proc})
        fut: Future = Future()
        pipe._futures, pipe._seqs, pipe._taken = {"d": fut}, {"d": 0}, {}
        pipe.timeout_s, pipe.broken, pipe.peak_mb = 600.0, False, 0.0
        pipe.close = lambda kill=False: None
        monkeypatch.setattr(parallel, "CHECK_S", 0.1)
        threading.Timer(1.0, lambda: fut.set_result({"rid": 7, "lines": []})).start()
        assert pipe.result("d")["rid"] == 7 and not pipe.broken
        pipe._futures, pipe._seqs, pipe.timeout_s = {"d2": Future()}, {"d2": 1}, 0.5
        with pytest.raises(parallel.PoolBroken, match="no answer"):
            pipe.result("d2")
    finally:
        proc.kill()
        proc.join(10)


PARENT = """
import sys, time
from desk import parallel
if __name__ == "__main__":
    pool = parallel.Pool(2)
    ex, _ = pool.acquire()
    for f in [ex.submit(time.sleep, 1.0), ex.submit(time.sleep, 1.0)]:  # both processes are started (on demand)
        f.result(120)
    print(*(p.pid for p in ex._processes.values()), flush=True)
    time.sleep(600)
"""


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"  # a zombie is gone: not reaped
    except OSError:
        return False


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads the processes from /proc")
def test_the_processes_of_the_pool_end_when_the_process_that_started_them_is_killed():
    """TDM#628 second review: after a kill of the starting process (``kill -9``, the end of the task, a crash) the pool's
    processes waited for work for ever, each holding hundreds of MB. Each watches its parent now (``_init``)."""
    import signal
    import subprocess
    import sys

    parent = subprocess.Popen([sys.executable, "-B", "-c", PARENT], stdout=subprocess.PIPE, text=True)
    try:
        pids = [int(x) for x in parent.stdout.readline().split()]
        assert len(pids) == 2 and all(_alive(p) for p in pids)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait(30)
        until = time.monotonic() + 30
        while any(_alive(p) for p in pids) and time.monotonic() < until:
            time.sleep(0.2)
        left = [p for p in pids if _alive(p)]
        for p in left:
            os.kill(p, signal.SIGKILL)  # (do not leave them behind when the test fails)
        assert not left, f"the pool's processes {left} live on after their parent was killed"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(30)


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


def _renumbered(x: Any, ids: dict[int, int]) -> Any:
    """``x`` with the run ids (``run_id``, ``supersedes_run_id``, ``previous_run_id`` and the like, the «(run N)» of a
    carried spread's source text) replaced by the run's place in the store (``ids``)."""
    if isinstance(x, dict):
        return {k: ids.get(v, v) if (k == "run_id" or k.endswith("_run_id")) and isinstance(v, int) else _renumbered(v, ids)
                for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_renumbered(v, ids) for v in x]
    if isinstance(x, str):
        return re.sub(r"\(run (\d+)\)", lambda m: f"(run {ids.get(int(m[1]), int(m[1]))})", x)
    return x


def dump(url: str) -> list[dict[str, Any]]:
    """Every run of the store, run after run: header, every row, model spreads (with their source text), references,
    core result hashes and the log lines. A run is known by its place in the store, not by its id (TDM#697): a process of
    the pool killed in the middle of its publication takes the id it had drawn with it (a rolled-back transaction does not
    give a sequence value back), so the runs after it have other ids than in the store of the sequential run, and are the
    same runs."""
    out = []
    with store.connect(url) as con:
        cur = con.cursor(row_factory=dict_row)
        runs = cur.execute("SELECT * FROM run ORDER BY run_id").fetchall()
        ids = {r["run_id"]: i for i, r in enumerate(runs, 1)}
        for r in runs:
            rid = r["run_id"]
            hdr = {k: v for k, v in r.items() if k not in ("started_at", "completed_at", "positions_known_at",
                                                           "backend_git_sha")}
            hdr["lineage"] = json.dumps(_strip(dict(hdr.pop("lineage"))), sort_keys=True, default=str)
            out.append(_renumbered({
                "hdr": json.dumps(_renumbered(hdr, ids), sort_keys=True, default=str),
                "rows": cur.execute("SELECT * FROM run_position WHERE run_id = %s ORDER BY ord", (rid,)).fetchall(),
                "ms": cur.execute("SELECT * FROM model_spread WHERE run_id = %s ORDER BY secid", (rid,)).fetchall(),
                "ref": cur.execute("SELECT kind, data FROM run_reference WHERE run_id = %s ORDER BY kind",
                                   (rid,)).fetchall(),
                "cr": cur.execute("SELECT result_sha256, label FROM run_core_result WHERE run_id = %s ORDER BY 1",
                                  (rid,)).fetchall(),
                "log": [_logline(m) for (m,) in con.execute(
                    "SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))]}, ids))
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
    assert par_order == seq_order  # a date in flight after the failed one (15.10) is not published: its turn did not come


# ---- failures at random: what is published, and what each run carries, is what the sequential run makes

CARRY_DATES = ["2026-07-24", "2026-09-01", *DATES]
CARRY_LOADED = "2026-07-24"


@pytest.fixture
def cache_carry(tmp_path) -> Path:
    """``NOQ`` is priced on the first two dates only (07.24 and 09.01, with different spreads), so on every later date its
    model price carries the spread of the latest run before the date: the run of 09.01, when it is published."""
    c = tmp_path / "cache_carry"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(CARRY_DATES):
        doc = shifted(d, len(CARRY_DATES) - i, light=False)
        if d not in CARRY_DATES[:2]:
            doc["data"]["bond_quotes"] = [q for q in doc["data"]["bond_quotes"] if q["secid"] != NOQ]
        write(c, doc)
    return c


def make_injected(path: str, real: Any):
    """``dayrun.eod_run`` that fails by the plan in the JSON file ``path``: {cycle, events: [[kind, date, pass, cycle]]}.
    Deterministic by (cycle, pass, date), not once: a date tried again in the same cycle fails again, whichever process
    tries it. ``kind``: ``transient`` (the store drops the connection), ``error`` (the date's own error), ``death`` (the
    process ends at once; only a pool's process dies, this one never)."""
    import multiprocessing

    def run(settings, d, **kw):
        plan_ = json.loads(Path(path).read_text(encoding="utf-8"))
        ph = 1 if kw.get("no_gamma") else 2
        for kind, dd, p, c in plan_["events"]:
            if dd == d and p == ph and c == plan_["cycle"]:
                if kind == "transient":
                    raise psycopg.OperationalError("injected: the store dropped the connection")
                if kind == "error":
                    raise ValueError("injected: the date's own error")
                if kind == "death" and multiprocessing.parent_process() is not None:
                    os._exit(3)
        return real(settings, d, **kw)

    return run


def inject(path: str) -> None:
    """A pool process's hook: the failures of ``make_injected``."""
    dayrun.eod_run = make_injected(path, dayrun.eod_run)


def two_cycles(pg_server, tmp_path: Path, cache_dir: Path, workers: int, events: list, monkeypatch):
    """Two cycles (the first pass of the history, then the second) of a store with one book (07.24) and a failure plan
    (``make_injected``) that holds in the first cycle only: the runs of the store (``dump``), the order they were
    published in after each cycle, and what each pass reported."""
    url = second_store(pg_server)
    load_noq(url, tmp_path, CARRY_LOADED, 10)
    s = Settings(home=tmp_path / f"home_{uuid.uuid4().hex[:6]}", snapshot_cache=cache_dir, store_url=url,
                 backfill_workers=workers)
    inj = tmp_path / f"inj_{uuid.uuid4().hex[:6]}.json"

    def plan_() -> backfill.Plan:
        with store.connect(url) as con:
            return backfill.plan(con, s, date(2026, 7, 20), date(2026, 10, 15))

    pool = parallel.Pool(workers, (inject, (str(inj),))) if workers > 1 else None
    orders: list[list[str]] = []
    said: list[Any] = []
    try:
        with monkeypatch.context() as m:
            m.setattr(dayrun, "eod_run", make_injected(str(inj), dayrun.eod_run))
            for cycle, hour in ((1, 9), (2, 12)):
                inj.write_text(json.dumps({"cycle": cycle, "events": events}))
                now = datetime(2026, 10, 20, hour, tzinfo=UTC)
                r1 = backfill.run(s, plan_(), store_url=url, out=lambda line: None, workers=workers, pool=pool, now=now)
                r2 = backfill.run_second(s, plan_(), store_url=url, out=lambda line: None, workers=workers, pool=pool)
                orders.append(run_order(url))
                said.append((r1["done"], [d for d, _ in r1["failed"]], r1["unconverged"], r2["done"],
                             [d for d, _ in r2["failed"]], r2["moved"]))
    finally:
        if pool is not None:
            pool.close()
    return dump(url), orders, said


def test_a_date_after_a_store_error_does_not_publish_with_the_spread_of_an_older_run_and_stays_so(
        tmp_path, cache_carry, pg_server, monkeypatch):
    """TDM#628 second review: 09.01 fails once on a store error. The sequential run stops there; the pool published the
    dates after it all the same (the failed date handed its turn on), with the spread of the run before 09.01 instead
    of 09.01's, and the plan does not call such a date stale: the wrong spread stayed on every date after it, for
    ever. Now the failed date holds the turn, the dates after it are not published, and two cycles are the sequential
    run's to the last bit."""
    events = [["transient", "2026-09-01", 1, 1]]
    seq, seq_orders, seq_said = two_cycles(pg_server, tmp_path, cache_carry, 1, events, monkeypatch)
    par, par_orders, par_said = two_cycles(pg_server, tmp_path, cache_carry, 3, events, monkeypatch)
    assert set(seq_orders[0]) == {"2026-07-24"}, "the scenario: the sequential run stops at the failed date"
    assert len(seq_orders[1]) > 10 and "2026-09-01" in seq_orders[1]
    assert par_orders[0] == seq_orders[0]  # nothing is published after the failed date
    assert par_orders == seq_orders and par_said == seq_said
    assert not differences(seq, par), differences(seq, par)[:5]
    def z(quality: str) -> set[float]:
        return {round(float(row["fact"]["spread_bp"]), 2) for r in par for row in r["rows"]
                if row["secid"] == NOQ and row["quality"] == quality}

    # the scenario: the dates 07.24 and 09.01 have two spreads of their own, and the model prices carry the later one
    assert len(z("assumed")) == 2 and len(z("model_price")) == 1 and z("model_price") < z("assumed")


def test_a_date_that_fails_with_its_own_error_lets_the_next_date_publish_as_the_sequential_run_does(
        tmp_path, cache_carry, pg_server, monkeypatch):
    """The other ending: the date's own error is passed over (the sequential run goes on): the next date publishes
    without it (the spread of the run before), in the pool as one after the other, in both cycles."""
    events = [["error", "2026-09-01", 1, 1]]
    seq, seq_orders, seq_said = two_cycles(pg_server, tmp_path, cache_carry, 1, events, monkeypatch)
    par, par_orders, par_said = two_cycles(pg_server, tmp_path, cache_carry, 3, events, monkeypatch)
    assert "2026-09-01" not in seq_orders[0] and "2026-10-09" in seq_orders[0], "the scenario: the run goes on"
    assert par_orders == seq_orders and par_said == seq_said
    assert not differences(seq, par), differences(seq, par)[:5]


def random_events(seed: int) -> list:
    """One or two failures of the first cycle: kind, date, pass (the first pass of the history or the second), at random."""
    import random

    rnd = random.Random(seed)
    out: list = []
    for _ in range(rnd.choice((1, 1, 2))):
        out.append([rnd.choice(("transient", "error", "death")), rnd.choice(CARRY_DATES), rnd.choice((1, 1, 2)), 1])
    return out


# The default run takes six seeds that between them hold every kind of failure (a store error, the date's own error, a
# process that dies) in each pass, and one or two of them at once; the other 18 (the same property, 15-18 s each: 5 minutes
# of the Linux job) are marked ``full_property`` and run by the separate job «parallel-property» of ci.yml
# (``pytest -m full_property``) and by the release workflow.
DEFAULT_SEEDS = (0, 5, 10, 12, 15, 17)
SEEDS = [s if s in DEFAULT_SEEDS else pytest.param(s, marks=pytest.mark.full_property) for s in range(24)]


@pytest.mark.parametrize("seed", SEEDS)
def test_failures_at_random_publish_the_same_dates_with_the_same_numbers_as_the_sequential_run(
        seed, tmp_path, cache_carry, pg_server, monkeypatch):
    """The invariant of the pool, whatever fails and wherever: the dates published and the contents of every run are what
    the sequential run publishes at the same failures (a store error, the date's own error), two cycles on. A process that
    dies is no failure of the date (the dates it did not publish run one after the other): the sequential run it is
    compared with does not have it. Fixed seeds, so a run that fails can be run again."""
    events = random_events(seed)
    seq, seq_orders, seq_said = two_cycles(pg_server, tmp_path, cache_carry, 1, events, monkeypatch)
    par, par_orders, par_said = in_thread(
        lambda: two_cycles(pg_server, tmp_path, cache_carry, 3, events, monkeypatch), 900)
    assert par_orders == seq_orders, events
    assert par_said == seq_said, events
    assert not differences(seq, par), (events, differences(seq, par)[:5])


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

def test_the_settings_of_the_processes(tmp_path):
    """On by default since 11.11.2026: 0 leaves the number of processes to the program (``parallel.workers_for``), 1 is the
    old sequential run, a number from 2 up is used as it is."""
    assert Settings().backfill_workers == 0
    env = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")}
    assert load_settings(env).backfill_workers == 0
    assert load_settings({**env, "DESK_BACKFILL_WORKERS": "3"}).backfill_workers == 3
    assert load_settings({**env, "DESK_BACKFILL_WORKERS": "1"}).backfill_workers == 1
    assert not check_settings_doc({"backfill_workers": 2}) and not check_settings_doc({"backfill_workers": 0})
    assert check_settings_doc({"backfill_workers": -1}) == ["backfill_workers: must be a whole number >= 0"]


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
