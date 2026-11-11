"""TDM#659: the history in several processes, on Windows too (CI runs this file on Windows with CPython 3.11, the target of
the owner's machine; ``backend/tests/windows_suite.txt``). The pool starts its processes with ``spawn`` there, the only way
Windows has, and nobody had run it: this is that run, small. Four things the other platforms never prove for it: a small
history in the pool is the history one date after the other, to the last bit (both passes, the embedded PostgreSQL the
tests start); the processes are started by ``spawn`` from a test of this process and from a program of its own; a process
that dies costs no date; and the processes end when the process that started them is killed. The rest of the pool's tests
(``test_parallel_backfill_628.py``, the failures at random) run on Linux, where they are fast. Synthetic data only."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from test_parallel_backfill_628 import PARENT, break_date, cache, run_order, same_runs, second_store  # noqa: F401 - the fixture
from test_r23_backfill import load, settings
from test_two_pass_627 import NEWEST_FIRST, RUN_DATES, plan, runs

from desk import backfill, parallel


def _history(url: str, tmp_path: Path, cache_dir: Path, workers: int, init=None):
    load(url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache_dir, url)
    r1 = backfill.run(s, plan(url, s), store_url=url, out=lambda line: None, workers=workers, pool_init=init)
    r2 = backfill.run_second(s, plan(url, s), store_url=url, out=lambda line: None, workers=workers)
    return s, r1, r2


def test_the_pool_makes_both_passes_of_a_small_history_as_one_date_after_the_other_does(
        tmp_path, cache, store_url, pg_server):  # noqa: F811
    s, first, second = _history(store_url, tmp_path, cache, 2)
    assert first["done"] == RUN_DATES and first["complete"] and not first["failed"] and first["left"] == 0, first
    assert second["done"] == NEWEST_FIRST and not second["failed"] and not second["mismatch"], second
    assert run_order(store_url) == RUN_DATES + NEWEST_FIRST  # published in the order of the dates, as the passes go
    p = plan(store_url, s)
    assert not p.todo and not p.second_todo
    other = second_store(pg_server)  # the same, in this process
    _history(other, tmp_path, cache, 1)
    same_runs(runs(store_url), runs(other))


def test_a_process_that_dies_costs_no_date_and_the_runs_are_the_same(
        tmp_path, cache, store_url, pg_server):  # noqa: F811
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, workers=2,
                       pool_init=(break_date, ("2026-10-13", "die")))  # ``os._exit`` in the process that has the date
    assert res["done"] == RUN_DATES and not res["failed"] and res["left"] == 0 and res["complete"], res
    assert run_order(store_url) == RUN_DATES
    other = second_store(pg_server)
    load(other, tmp_path, "2026-10-12", 10)
    s1 = settings(tmp_path, cache, other)
    backfill.run(s1, plan(other, s1), store_url=other, out=lambda line: None)
    same_runs(runs(store_url), runs(other))


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE  # a zombie is gone: not reaped yet
    except psutil.NoSuchProcess:
        return False


def test_the_processes_of_the_pool_end_when_the_process_that_started_them_is_killed() -> None:
    """The pool's processes watch their parent's handle (``parallel._end_with_parent``): after a kill of the program that
    started them (``TerminateProcess`` on Windows, SIGKILL elsewhere) none of them waits for work for ever."""
    parent = subprocess.Popen([sys.executable, "-B", "-c", PARENT], stdout=subprocess.PIPE, text=True)
    pids: list[int] = []
    try:
        pids = [int(x) for x in parent.stdout.readline().split()]
        assert len(pids) == 2 and all(_alive(p) for p in pids)
        parent.kill()
        parent.wait(30)
        until = time.monotonic() + 60
        while any(_alive(p) for p in pids) and time.monotonic() < until:
            time.sleep(0.2)
        left = [p for p in pids if _alive(p)]
        for p in left:
            psutil.Process(p).kill()  # (do not leave them behind when the test fails)
        assert not left, f"the pool's processes {left} live on after their parent was killed"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(30)


def test_the_processes_of_the_pool_are_other_processes_started_by_spawn() -> None:
    pool = parallel.Pool(1)
    try:
        ex, shared = pool.acquire()
        assert shared is not None
        pid = ex.submit(os.getpid).result(120)  # (a process of the pool answers: it started and imported what it needs)
        assert pid != os.getpid() and psutil.Process(pid).ppid() == os.getpid()
        assert ex._mp_context.get_start_method() == "spawn"  # (the pool asks for its own context, whatever the platform's)
    finally:
        pool.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows priority class")
def test_the_pool_works_below_normal_priority_on_windows() -> None:
    pool = parallel.Pool(1)
    try:
        ex, _ = pool.acquire()
        pid = ex.submit(os.getpid).result(120)
        assert psutil.Process(pid).nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS  # type: ignore[attr-defined]
    finally:
        pool.close()
