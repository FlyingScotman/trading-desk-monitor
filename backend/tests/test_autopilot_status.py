"""#156: the autopilot's overall status is not «Данные актуальны» while a run of the day fails every cycle (skipped as
failed before on the same inputs) or a business day after 10:15 MSK has no run of today. The clock is fixed in every
test. Synthetic data only (the stub hub of slice S3, the canonical inbox fixtures)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest
import test_slice_s3
from test_autopilot import SNAPS, cfg, put, steps
from test_intraday import FRI, MON, at, publish

from desk import cli
from desk.autopilot import Autopilot
from desk.inbox import scan

server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures
SAT = date(2026, 10, 17)


@pytest.fixture(autouse=True)
def _fresh_process():
    scan._handles.clear()
    scan._last_seen.clear()
    yield
    scan._handles.clear()
    scan._last_seen.clear()


def cached(tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)


def test_eod_failed_before_is_an_error_with_its_text(tmp_path, store_url, monkeypatch):
    cached(tmp_path)
    put(tmp_path, "positions_20261016.csv")

    def broken(*a, **kw):
        raise ValueError("time zone not recognized")

    monkeypatch.setattr(cli, "eod_run", broken)
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), now=lambda: at(SAT, 9, 0))
    assert steps(ap.run_cycle())["eod"][0] == "error"
    st = ap.run_cycle()
    assert steps(st)["eod"] == ("skipped", "2026-10-15 failed before on the same inputs, not retried")
    assert "актуальны" not in st["message"] and st["state"] == "error", st["message"]
    assert st["message"] == "Ошибка: прогон 15.10 · закрытие падает (ValueError: time zone not recognized)"


def test_intraday_failed_before_is_an_error_with_its_text(stub, tmp_path, store_url, monkeypatch):
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")
    real = cli.eod_run

    def broken(*a, **kw):  # only the run on today's intraday cut fails
        if kw.get("cut_type") == "intraday":
            raise ValueError("boom")
        return real(*a, **kw)

    monkeypatch.setattr(cli, "eod_run", broken)
    clock = {"t": at(FRI, 9, 0)}
    ap = Autopilot(settings_loader=lambda: s, now=lambda: clock["t"])
    ap.run_cycle()  # the close of 15.10 and the morning run of 16.10
    publish(stub, FRI, "intraday", "2026-10-16T07:00:00Z", 1)
    clock["t"] = at(FRI, 10, 6)
    assert steps(ap.run_cycle())["intraday"][0] == "error"
    clock["t"] = at(FRI, 10, 16)
    st = ap.run_cycle()
    intraday = next(x for x in st["steps"] if x["name"] == "intraday")
    assert intraday["status"] == "skipped" and intraday["reason"] == "failed_before", intraday
    assert "актуальны" not in st["message"] and st["state"] == "error", st["message"]
    assert st["message"] == "Ошибка: прогон 16.10 10:00 МСК · внутри дня падает (ValueError: boom)"


def test_no_run_of_today_after_10_15_on_a_business_day_is_not_current(tmp_path, store_url):
    """No hub: the close of 15.10 runs, but nothing of Monday 19.10 (no close of 16.10 for its morning run)."""
    cached(tmp_path)
    put(tmp_path, "positions_20261016.csv")
    clock = {"t": at(MON, 9, 0)}
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), now=lambda: clock["t"])
    assert ap.run_cycle()["message"] == "Пересчитано: 2026-10-15"
    clock["t"] = at(MON, 10, 14)  # before 10:15: as before
    assert ap.run_cycle()["message"] == "Данные актуальны (не настроено: маркет-дата сервер)"
    clock["t"] = at(MON, 10, 15)
    st = ap.run_cycle()
    assert "актуальны" not in st["message"], st["message"]
    assert st["message"] == ("Данные устарели: нет прогона за 19.10"
                             " (утренний прогон: no close of 2026-10-16 in the cache)")
    assert st["state"] == "idle"  # a warning, not a failed step
    clock["t"] = at(SAT, 12, 0)  # not a business day: nothing expected
    assert ap.run_cycle()["message"] == "Данные актуальны (не настроено: маркет-дата сервер)"
