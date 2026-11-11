"""TDM#628, the default: the history is computed in several processes unless the setting says one. The automatic number
is up to three, one core always left to the PC and as many as the free memory holds; the setting still decides (1 is the
sequential run); the pool is not started for a machine that would not gain."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from desk import parallel
from desk.settings import Settings, SettingsError, load

REAL_AUTO = parallel.auto_workers  # (the suite's fixture holds ``auto_workers`` at 1 once the tests run)


@pytest.mark.parametrize("cpus,mb,expected", [
    (1, None, 1), (2, None, 1), (3, None, 2), (4, None, 3), (8, None, 3), (64, None, 3),  # the cores
    (8, 16_000, 3), (8, 4_200, 2), (8, 3_200, 1), (8, 2_500, 1), (8, 100, 1), (8, 0, 1),  # the memory left over 2 GB
    (2, 64_000, 1), (4, 5_200, 3), (4, 5_100, 2),  # the lower of the two
])
def test_the_automatic_number_is_the_lower_of_the_cores_and_the_memory(cpus, mb, expected) -> None:
    assert parallel.choose_workers(cpus, mb) == expected


def test_the_automatic_number_follows_the_figures_it_is_given() -> None:
    assert parallel.choose_workers(8, 10_000, per_worker_mb=2_000, reserve_mb=1_000) == 3
    assert parallel.choose_workers(8, 10_000, per_worker_mb=4_000, reserve_mb=1_000) == 2
    assert parallel.choose_workers(8, 10_000, limit=2) == 2


def test_the_memory_is_told_on_this_machine_or_not_at_all() -> None:
    mb = parallel.available_memory_mb()
    assert mb is None or mb > 0
    if sys.platform != "win32" and mb is None:
        pytest.fail("/proc/meminfo is there")


def test_the_setting_decides_and_zero_leaves_it_to_the_program(monkeypatch) -> None:
    monkeypatch.setattr(parallel, "auto_workers", lambda: 3)
    assert parallel.workers_for(SimpleNamespace(backfill_workers=0)) == 3
    assert parallel.workers_for(SimpleNamespace()) == 3
    assert parallel.workers_for(SimpleNamespace(backfill_workers=1)) == 1  # one date after the other, as before
    assert parallel.workers_for(SimpleNamespace(backfill_workers=2)) == 2
    assert parallel.workers_for(SimpleNamespace(backfill_workers=6)) == 6  # asked for: more than the automatic ones


def test_the_default_setting_is_automatic_and_one_is_the_old_behaviour(tmp_path, monkeypatch) -> None:
    for k in ("DESK_BACKFILL_WORKERS",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    assert Settings().backfill_workers == 0 and load().backfill_workers == 0
    for v, n in (("1", 1), ("2", 2), ("0", 0)):
        monkeypatch.setenv("DESK_BACKFILL_WORKERS", v)
        assert load().backfill_workers == n
    monkeypatch.setenv("DESK_BACKFILL_WORKERS", "-1")
    with pytest.raises(SettingsError):
        load()
    monkeypatch.setenv("DESK_BACKFILL_WORKERS", "x")
    with pytest.raises(SettingsError):
        load()


def test_the_settings_summary_says_automatic(tmp_path) -> None:
    text = dict(Settings(home=tmp_path).describe())["backfill"]
    assert "automatic" in text and "process(es)" not in text
    assert "2 process(es)" in dict(Settings(home=tmp_path, backfill_workers=2).describe())["backfill"]


def test_the_machine_decides_the_automatic_number_and_windows_only_when_the_switch_is_on(monkeypatch) -> None:
    monkeypatch.setattr(parallel, "available_memory_mb", lambda: 16_000.0)
    monkeypatch.setattr(parallel.os, "sched_getaffinity", lambda n: set(range(8)), raising=False)
    assert REAL_AUTO() == 3
    monkeypatch.setattr(parallel.os, "sched_getaffinity", lambda n: {0, 1}, raising=False)
    assert REAL_AUTO() == 1  # two cores: one date after the other
    monkeypatch.setattr(parallel.os, "sched_getaffinity", lambda n: set(range(8)), raising=False)
    monkeypatch.setattr(parallel.sys, "platform", "win32")
    assert REAL_AUTO() == (3 if parallel.AUTO_ON_WINDOWS else 1)
    monkeypatch.setattr(parallel, "AUTO_ON_WINDOWS", False)
    assert REAL_AUTO() == 1


def test_the_pool_check_starts_processes_and_each_answers(tmp_path) -> None:
    """``desk pool-check``: the processes of the history start by ``spawn`` and each answers once (the bundle's CI runs it on
    Windows through the program's own launcher)."""
    res = parallel.check(2)
    assert res["ok"] is True and res["processes"] == 2 and res["error"] is None, res
    from desk import cli

    assert cli.main(["pool-check", "--workers", "1"]) == 0


def test_the_backfill_status_says_how_many_processes_compute_the_history(tmp_path, store_url, monkeypatch) -> None:
    """The settings screen shows it beside the passes («Процессов пересчёта»): the number, and whether the program chose it."""
    from desk import backfill, store

    monkeypatch.setattr(parallel, "auto_workers", lambda: 3)
    with store.connect(store_url) as con:
        auto = backfill.status(con, Settings(home=tmp_path, snapshot_cache=tmp_path / "cache", store_url=store_url))
        two = backfill.status(con, Settings(home=tmp_path, snapshot_cache=tmp_path / "cache", store_url=store_url,
                                            backfill_workers=2))
        one = backfill.status(con, Settings(home=tmp_path, snapshot_cache=tmp_path / "cache", store_url=store_url,
                                            backfill_workers=1))
    assert (auto["workers"], auto["workers_auto"]) == (3, True)
    assert (two["workers"], two["workers_auto"]) == (2, False)
    assert (one["workers"], one["workers_auto"]) == (1, False)
