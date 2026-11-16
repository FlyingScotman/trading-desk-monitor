"""TDM#749: an internal error of the database (PostgreSQL class XX: ``InternalError_`` XX000, ``DataCorrupted`` XX001,
``IndexCorrupted`` XX002; e.g. «unexpected data beyond EOF in block N») is the store's trouble, not the date's data.
The backfill stops on it as on a store that does not answer (a stall, cause ``store_broken``) in both passes and does
not mark the date failed; the autopilot does not remember the run as failed on its inputs; the dates a state of
before froze are let go when it is read. Synthetic data only."""

from __future__ import annotations

import errno
import json
import logging
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from test_autopilot import SNAPS, TODAY, cfg, put, steps
from test_market_history import FIX, STATIC, shifted, write
from test_r23_backfill import DATES, load, settings
from test_two_pass_627 import D1, NEWEST_FIRST, RUN_DATES, first_pass, plan, second_pass

from desk import backfill, dayrun, store
from desk.autopilot import Autopilot


@pytest.fixture
def cache(tmp_path) -> Path:
    """The fixture's market shifted to the five business dates of ``DATES`` (as ``test_r23_backfill``'s)."""
    c = tmp_path / "cache"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(DATES):
        write(c, shifted(d, len(DATES) - i, light=False))
    return c

EOF_MSG = "unexpected data beyond EOF in block 9469 of relation base/16384/16512"
ERRORS = [
    pytest.param(lambda: psycopg.errors.InternalError_(EOF_MSG), id="InternalError_"),
    pytest.param(lambda: psycopg.errors.DataCorrupted("invalid page in block 1"), id="DataCorrupted"),
    pytest.param(lambda: psycopg.errors.IndexCorrupted("index is corrupted"), id="IndexCorrupted"),
]


def test_the_classes_are_a_stall_of_the_store_not_of_a_date():
    for make in (e.values[0] for e in ERRORS):
        e = make()
        assert backfill.transient(e) and backfill.stall_cause(e) == "store_broken", e
        assert backfill.store_broken(e) and backfill.store_fault(e)
    disk = psycopg.errors.DiskFull("could not extend file: No space left on device")
    assert isinstance(disk, psycopg.OperationalError)  # a stall already; its cause is the disk's, not «no connection»
    assert backfill.transient(disk) and backfill.stall_cause(disk) == "disk"
    assert backfill.store_fault(disk) and not backfill.store_broken(disk)
    # the store that does not answer and a date's own errors are what they were
    assert backfill.stall_cause(psycopg.OperationalError("connection failed")) == "store"
    assert not backfill.store_fault(psycopg.OperationalError("connection failed"))
    # the other psycopg.InternalError classes (24, 25, 2B, 2D) are what our own statements did, not the store's trouble
    for e in (ValueError("bad"), RuntimeError("InternalError_"), psycopg.errors.UniqueViolation("dup"),
              psycopg.errors.DataError("x"), psycopg.errors.NumericValueOutOfRange("x"),
              psycopg.errors.InFailedSqlTransaction("current transaction is aborted"),
              psycopg.errors.ActiveSqlTransaction("x"), psycopg.errors.NoActiveSqlTransaction("x"),
              psycopg.errors.ReadOnlySqlTransaction("x"), psycopg.errors.DependentObjectsStillExist("x"),
              psycopg.errors.InvalidCursorState("x")):
        assert not backfill.store_fault(e) and not backfill.store_broken(e) and not backfill.transient(e), e
    for e in (OSError(errno.ENOSPC, "no space"), OSError(errno.EDQUOT, "quota")):  # the host's disk: the same as the database's
        assert backfill.store_fault(e) and backfill.transient(e) and backfill.stall_cause(e) == "disk", e
    assert not backfill.store_fault(PermissionError("cache file"))
    assert not backfill.no_wait({"cause": "store_broken", "source": "store"})  # a restart is needed: waits, not at once


@pytest.mark.parametrize("error", ERRORS)
def test_an_internal_error_of_the_store_is_not_a_failed_date(tmp_path, cache, store_url, monkeypatch, error):
    """The first pass stops on it at the date (a stall, ``store_broken``), the date is not failed; the next run, the
    store mended, makes the dates."""
    load(store_url, tmp_path, "2026-10-13", 10)
    s = settings(tmp_path, cache, store_url)
    real = dayrun.eod_run
    broken = {"on": True}

    def flaky(settings, d, **kw):
        if broken["on"] and d == "2026-10-14":
            raise error()
        return real(settings, d, **kw)

    monkeypatch.setattr(dayrun, "eod_run", flaky)
    with store.connect(store_url) as con:
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                           out=lambda line: None)
        assert res["done"] == ["2026-10-13"] and not res["complete"] and res["left"] == 1
        assert [d for d, _ in res["failed"]] == ["2026-10-14"] and "tried again next time" in res["failed"][0][1]
        stall = res["stalled"]
        assert stall["cause"] == "store_broken" and stall["source"] == "store" and stall["date"] == "2026-10-14"
        assert type(error()).__name__ in stall["error"]
        st = backfill.state(s)
        assert st.get("failed", {}) == {} and st["problems"]["2026-10-14"]["kind"] == "transient"
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
        assert [(d.date, d.status) for d in p.days if d.status != "no_positions"] == [
            ("2026-10-13", "done"), ("2026-10-14", "todo"), ("2026-10-15", "todo")]
        status = backfill.status(con, s, today=date(2026, 10, 15))
        assert status["failed"] == 0 and status["failed_dates"] == [] and status["problem_dates"] == []
        assert status["stalled"]["cause"] == "store_broken"
        broken["on"] = False  # the server restarted, the file mended
        res = backfill.run(s, p, store_url=store_url, out=lambda line: None)
    assert res["done"] == ["2026-10-14", "2026-10-15"] and not res["failed"] and not res["stalled"]
    assert "stall" not in backfill.state(s)


def test_a_store_that_breaks_every_date_freezes_none_of_them(tmp_path, cache, store_url, monkeypatch):
    """The owner's case: the same internal error on every date, run after run (the autopilot's cycles). No date is
    failed, none is a problem date, the stall stands with its reason, and when the store is mended they all run."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    real = dayrun.eod_run
    broken = {"on": True}

    def flaky(settings, d, **kw):
        if broken["on"]:
            raise psycopg.errors.InternalError_(EOF_MSG)
        return real(settings, d, **kw)

    monkeypatch.setattr(dayrun, "eod_run", flaky)
    for _ in range(5):
        res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
        assert not res["done"] and res["stalled"]["cause"] == "store_broken"
        assert backfill.state(s).get("failed", {}) == {}
        with store.connect(store_url) as con:
            status = backfill.status(con, s, D1)
        assert status["failed"] == 0 and status["problem_dates"] == [] and status["todo"] == 4
        assert status["stalled"]["cause"] == "store_broken"
    broken["on"] = False
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    assert res["done"] == RUN_DATES and not res["failed"] and not res["stalled"] and res["complete"]


@pytest.mark.parametrize("error", ERRORS)
def test_an_internal_error_of_the_store_stops_the_second_pass_as_a_stall(tmp_path, cache, store_url, monkeypatch, error):
    """The pass stops on the first date, the date is not given up (``second.failed``), and the stop is remembered as the
    pass's stall: the cause, since when, the stops and the wait to the next attempt, shown by ``status`` even though the
    first pass has nothing left to run; a date made ends it."""
    s, _ = first_pass(tmp_path, cache, store_url)
    calls = []

    def broken(settings, d, **kw):
        calls.append(d)
        raise error()

    monkeypatch.setattr(dayrun, "eod_run", broken)
    t0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    runs = backfill.MAX_ATTEMPTS + 1  # more runs than the attempts a date gets before it is given up
    for i in range(1, runs + 1):
        res = second_pass(s, store_url, now=t0 + timedelta(hours=i))
        assert res["done"] == [] and res["failed"] == [] and res["stopped"].startswith(type(error()).__name__ + ":")
        assert res["left"] == 4
        stall = backfill.state(s)["second"]["stall"]
        assert stall["cause"] == "store_broken" and stall["source"] == "store" and stall["phase"] == "second"
        assert stall["date"] == NEWEST_FIRST[0] and stall["stops"] == i and stall["since"].startswith("2026-11-16T13:00")
        wait = backfill.STALL_BACKOFF_S[min(i, len(backfill.STALL_BACKOFF_S)) - 1]
        assert datetime.fromisoformat(stall["next_attempt"]) - datetime.fromisoformat(stall["at"]) == timedelta(seconds=wait)
    assert calls == [NEWEST_FIRST[0]] * runs  # (``run_second`` itself does not wait: the autopilot does, see below)
    assert plan(store_url, s).count_second("failed") == 0
    assert backfill.state(s).get("second", {}).get("failed", {}) == {}
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["todo"] == 0 and st["pass"] == "second"
    assert st["stalled"]["cause"] == "store_broken" and st["stalled"]["phase"] == "second" and st["stalled"]["next_attempt"]
    monkeypatch.undo()
    assert second_pass(s, store_url)["done"] == NEWEST_FIRST
    assert "stall" not in backfill.state(s)["second"]
    with store.connect(store_url) as con:
        assert backfill.status(con, s, D1)["stalled"] is None


def test_a_stall_of_the_second_pass_of_a_day_ago_is_forgotten(tmp_path, cache, store_url, monkeypatch):
    s, _ = first_pass(tmp_path, cache, store_url)
    monkeypatch.setattr(dayrun, "eod_run", lambda *a, **kw: (_ for _ in ()).throw(psycopg.errors.InternalError_(EOF_MSG)))
    t0 = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    second_pass(s, store_url, now=t0)
    second_pass(s, store_url, now=t0 + timedelta(days=2))
    stall = backfill.state(s)["second"]["stall"]
    assert stall["stops"] == 1 and stall["since"].startswith("2026-11-18T12:00")


def test_the_autopilot_waits_for_the_stall_of_the_second_pass(tmp_path, cache, store_url, monkeypatch):
    """The second pass on a broken store is not run every cycle: the step says why and waits (status ``error``), unless
    the owner asks («Обновить»); after the wait it tries again, the stop counted; the store mended, it runs."""
    from desk.inbox.timesem import MSK

    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_budget_s=600, backfill_from="2026-10-01")
    real, calls, broken = dayrun.eod_run, [], {"on": True}

    def flaky(settings, d, **kw):
        if broken["on"] and not kw.get("no_gamma") and d != "2026-10-15":  # the second pass; the eod step's date runs
            calls.append(d)
            raise psycopg.errors.InternalError_(EOF_MSG)
        return real(settings, d, **kw)

    monkeypatch.setattr(dayrun, "eod_run", flaky)
    clock = [datetime(2026, 11, 30, 12, 0, tzinfo=MSK)]
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30),
                   now=lambda: clock[0])

    def step():
        return next(x for x in ap.run_cycle(hub=False)["steps"] if x["name"] == "backfill")

    b = step()
    assert calls == ["2026-10-14"] and b["status"] == "error" and "store_broken" in b["detail"], b
    assert "next attempt 2026-11-30T12:10:" in b["detail"], b["detail"]
    clock[0] += timedelta(minutes=1)
    b = step()
    assert calls == ["2026-10-14"] and b["status"] == "error" and "stands since" in b["detail"], (calls, b)  # waits
    ap.trigger(manual=True)  # the owner's «Обновить»: no wait
    b = step()
    assert calls == ["2026-10-14"] * 2 and "next attempt 2026-11-30T12:31:" in b["detail"], (calls, b)
    clock[0] += timedelta(minutes=5)
    step()
    assert len(calls) == 2  # still waiting (30 min after the second stop)
    broken["on"] = False
    clock[0] += timedelta(minutes=31)
    b = step()
    assert b["status"] == "ok" and "second derivatives of 3 dates" in b["detail"], b
    assert "stall" not in backfill.state(s).get("second", {})


STORE_ERR = "InternalError_: " + EOF_MSG


def test_the_dates_a_store_error_froze_are_planned_again_when_the_state_is_read(tmp_path, cache, store_url, caplog):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    errors = {"2026-10-12": STORE_ERR, "2026-10-13": "DataCorrupted: invalid page in block 3",
              "2026-10-14": "ValueError: bad bundle", "2026-10-15": "IndexCorrupted: index is corrupted"}
    # an ordinary error stays frozen, and so does a date that does not converge
    path = Path(s.home) / backfill.STATE_FILE
    raw = {"failed": {}, "problems": {}}
    p = plan(store_url, s)
    sig = {d.date: d.sig for d in p.days}
    for d, err in errors.items():
        raw["failed"][d] = sig[d]
        raw["problems"][d] = {"kind": "failed", "why": "no default run", "error": err, "attempts": 1, "sig": sig[d]}
    raw["problems"]["2026-10-15"]["kind"] = "unconverged"  # does not converge: not a store's error whatever the text
    raw["stall"] = {"error": "InternalError_: x", "date": "2026-10-12", "dates": ["2026-10-12"], "source": "store"}
    backfill.save_state(s, raw)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="desk.backfill"):
        p = plan(store_url, s)
    assert {d.date: d.status for d in p.days if d.status != "no_positions"} == {
        "2026-10-12": "todo", "2026-10-13": "todo", "2026-10-14": "failed", "2026-10-15": "failed"}
    healed = [r.getMessage() for r in caplog.records if "planned again" in r.getMessage()]
    assert healed == [("backfill: 2 dates of the first pass and 0 of the second were failed on the store's own error "
                       "(not the dates' data): planned again")]
    on_disk = json.loads(path.read_text(encoding="utf-8"))  # written back: once
    assert sorted(on_disk["failed"]) == ["2026-10-14", "2026-10-15"] and sorted(on_disk["problems"]) == [
        "2026-10-14", "2026-10-15"]
    assert on_disk["stall"]["dates"] == ["2026-10-12"]  # a stall's own record is not a frozen date
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="desk.backfill"):
        backfill.state(s)
    assert not [r for r in caplog.records if "planned again" in r.getMessage()]
    with store.connect(store_url) as con:  # the screen: the ordinary failures only
        status = backfill.status(con, s, D1)
    assert sorted(status["failed_dates"]) == ["2026-10-14", "2026-10-15"]
    assert {x["date"] for x in status["problem_dates"]} == {"2026-10-14", "2026-10-15"}


def test_the_dates_the_second_pass_gave_up_on_a_store_error_are_planned_again(tmp_path, cache, store_url, caplog):
    s, _ = first_pass(tmp_path, cache, store_url)
    p = plan(store_url, s)
    sig = {d.date: d.sig for d in p.days}
    run_id = {d.date: d.run_id for d in p.days}
    gave_up = {"2026-10-12": STORE_ERR, "2026-10-13": "DataCorrupted: invalid page in block 3",
               "2026-10-14": "RuntimeError: the new run does not make the date current: positions changed",
               "2026-10-15": "IndexCorrupted: index is corrupted"}
    raw = {"second": {"failed": {d: {"sig": sig[d], "run_id": run_id[d], "attempts": 2, "error": e}
                                 for d, e in gave_up.items()}, "mismatched": {}}}
    path = Path(s.home) / backfill.STATE_FILE
    backfill.save_state(s, raw)
    with caplog.at_level(logging.WARNING, logger="desk.backfill"):
        p = plan(store_url, s)
    assert {d.date: d.second for d in p.days if d.status == "done"} == {
        "2026-10-12": "todo", "2026-10-13": "todo", "2026-10-14": "failed", "2026-10-15": "todo"}
    assert any("0 dates of the first pass and 3 of the second" in r.getMessage() for r in caplog.records)
    assert sorted(json.loads(path.read_text(encoding="utf-8"))["second"]["failed"]) == ["2026-10-14"]
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert [x["date"] for x in st["second"]["problem_dates"]] == ["2026-10-14"]
    assert second_pass(s, store_url)["done"] == ["2026-10-15", "2026-10-13", "2026-10-12"]  # the freed ones, newest first


@pytest.mark.parametrize("text, store_error", [
    ("InternalError_: unexpected data beyond EOF in block 9469 of relation base/1/2", True),
    ("DataCorrupted: x", True), ("IndexCorrupted: x", True), ("DiskFull: x", True), ("InternalError_", True),
    ("InternalError: x", False), ("InFailedSqlTransaction: current transaction is aborted", False),
    ("вторые производные: InternalError_: x", False),  # the screen's wording, not what is stored
    ("ValueError: InternalError_: x", False), ("InternalErrorX: x", False), ("MyInternalError_: x", False),
    ("RuntimeError: psycopg.errors.InternalError_ in the pool", False), ("OperationalError: connection lost", False),
    ("does not converge: positions changed after 2 runs", False), ("", False), (None, False)])
def test_only_the_store_classes_at_the_start_of_the_error_are_released(text, store_error):
    st = {"failed": {"2026-10-13": ["sig"]},
          "problems": {"2026-10-13": {"kind": "failed", "error": text, "attempts": 1}},
          "second": {"failed": {"2026-10-13": {"error": text, "attempts": 2}}}}
    first, second = backfill.release_store_faults(st)
    assert (first, second) == ((1, 1) if store_error else (0, 0))
    assert (st["failed"], st["problems"], st["second"]["failed"]) == (
        ({}, {}, {}) if store_error else ({"2026-10-13": ["sig"]}, {"2026-10-13": st["problems"]["2026-10-13"]},
                                          {"2026-10-13": st["second"]["failed"]["2026-10-13"]}))
    assert backfill.release_store_faults({}) == (0, 0) and backfill.release_store_faults({"second": None}) == (0, 0)


def test_the_autopilot_does_not_remember_a_run_failed_on_a_store_error_but_pauses_it(tmp_path, store_url, monkeypatch):
    """The eod step: a run that failed on the store's own error is not remembered as failed on its inputs (an ordinary
    error is: see ``test_failed_eod_is_not_retried_until_inputs_change``); it waits for a pause, ``STALL_BACKOFF_S``
    by the stops in a row, not asked for every cycle, and is tried again after it; a run that goes through ends it."""
    from desk.inbox.timesem import MSK

    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    calls, broken = [], {"on": True}
    real = dayrun.eod_run

    def flaky(*a, **kw):
        calls.append(a[1])
        if broken["on"]:
            raise psycopg.errors.InternalError_(EOF_MSG)
        return real(*a, **kw)

    monkeypatch.setattr(dayrun, "eod_run", flaky)
    clock = [datetime(2026, 10, 19, 12, 0, tzinfo=MSK)]
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY, now=lambda: clock[0])
    st = ap.run_cycle()
    assert steps(st)["eod"][0] == "error" and "InternalError_" in steps(st)["eod"][1]
    assert ap._failed == {} and ap._fail_errors == {} and ap._store_pause["2026-10-15"][0] == 1
    clock[0] += timedelta(minutes=5)  # inside the first pause (10 min)
    st = ap.run_cycle()
    assert calls == ["2026-10-15"]  # not asked again
    status, detail = steps(st)["eod"]
    assert status == "skipped" and detail.startswith("2026-10-15 failed on an error of the store (InternalError_: unexpected data")
    assert detail.endswith("), next attempt 12:10"), detail
    assert st["state"] == "error" and "InternalError_" in st["message"]  # said, not «актуальны»
    clock[0] += timedelta(minutes=6)
    ap.run_cycle()
    assert calls == ["2026-10-15"] * 2 and ap._store_pause["2026-10-15"][0] == 2  # the second pause is 30 min
    clock[0] += timedelta(minutes=20)
    ap.run_cycle()
    assert len(calls) == 2
    broken["on"] = False
    clock[0] += timedelta(minutes=11)
    st = ap.run_cycle()
    assert len(calls) == 3 and steps(st)["eod"][0] == "ok" and ap._store_pause == {}


def test_the_autopilot_remembers_the_runs_that_failed_on_their_own_error():
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    ap = Autopilot(settings_loader=lambda: None, now=lambda: now)
    ap._fail("a", ("sig",), ValueError("boom"))
    assert ap._failed == {"a": ("sig",)} and ap._fail_errors == {"a": "ValueError: boom"} and ap._store_pause == {}
    for e in (psycopg.errors.InternalError_(EOF_MSG), psycopg.errors.DataCorrupted("x"),
              psycopg.errors.IndexCorrupted("x"), psycopg.errors.DiskFull("x"),
              OSError(errno.ENOSPC, "no space")):  # also forgets a mark made before, and waits instead
        ap._fail("a", ("sig2",), e)
        assert ap._failed == {} and ap._fail_errors == {} and ap._store_pause["a"][1] > now
        assert ap._failed_before("a", ("sig2",), "label") and ap._failing[-1][0] == "label"
        ap._fail("a", ("sig",), ValueError("boom"))  # an ordinary error after it: the pause is gone, the mark is made
        assert ap._store_pause == {} and ap._failed == {"a": ("sig",)}
    # the other errors of the database are not the store's (a failed transaction is what our own statement did)
    ap._fail("b", ("sig",), psycopg.errors.InFailedSqlTransaction("current transaction is aborted"))
    assert "b" in ap._failed and "b" not in ap._store_pause
