"""TDM#732: the re-fetch of the trades of past dates is not held back by the autopilot's long cycle. The files of a date
wait for the cycle lock, which the autopilot's backfill holds for its whole budget: they are taken in between two of its
dates (``AdapterServices.take_in_history``, ``backfill.run``'s ``between``), up to ``PENDING_MAX`` dates wait while the
worker goes on asking, today's feed waits for the lock without the helper lock, the dates whose trades were asked again
run first, and the stamp of the inputs is of the date, so a set of another dataset (or a date out of reach) does not make
every hash again. A synthetic helper (``test_adapter_services``). Synthetic data only."""
# ruff: noqa: F811 - the fixture `env` of the worker is imported and used by name
from __future__ import annotations

import threading
import time
from datetime import date, timedelta

import pytest
from psycopg.types.json import Jsonb
from test_r23_backfill import cache, load, settings  # noqa: F401 - the synthetic market fixture
from test_trades_history import (  # noqa: F401 - the fixture of the worker with the synthetic adapter
    D12,
    D13,
    D14,
    D15,
    FRI,
    _fresh,
    env,
)

from desk import adapter_services as svcs
from desk import backfill, dayrun, helper_proc, store, trades, trades_history
from desk.autopilot import Autopilot, CycleLock


def hold_the_cycle_lock(env) -> CycleLock:
    """What the autopilot does for the whole of a cycle."""
    lock = CycleLock(env.st.home)
    assert lock.acquire()
    return lock


# ---- the files wait up to ``PENDING_MAX`` dates, the worker goes on asking ----------------------------------------

def test_the_cap_is_three_to_five():
    assert 3 <= trades_history.PENDING_MAX <= 5


def test_the_worker_asks_on_while_the_cycle_holds_the_lock_and_stops_at_the_cap(env, monkeypatch):
    monkeypatch.setattr(trades_history, "PENDING_MAX", 2)
    env.s.history_set(None, D12, None)
    lock = hold_the_cycle_lock(env)
    try:
        assert env.step(3) == [True, True, False]       # a date a step, the lock never free; the third waits: the cap
        assert env.asked() == [D12, D13]                  # not one date for the whole cycle (a block: oldest first)
        h = env.doc
        assert [p["date"] for p in h["pending"]] == [D12, D13] and h["marks"] == {}
        assert all(p["why"] == "calculation_busy" for p in h["pending"])
        assert not helper_proc.HELPER_LOCK.locked()
        s = env.s.history()
        assert s["pending"] == 2 and s["pending_why"] == "calculation_busy"
    finally:
        lock.release()
    assert env.step() == [True]                           # the lock is free: both taken in, nothing new asked yet
    assert env.doc["pending"] == [] and env.marks() == {D12: "fetched", D13: "fetched"} and env.asked() == [D12, D13]
    assert env.s.history()["pending"] == 0
    assert env.step() == [True] and env.asked() == [D12, D13, D14]
    with store.connect(env.url) as con:   # the files of the three dates are in the store
        assert con.execute("SELECT count(DISTINCT valid_date) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == 3


def test_a_date_whose_files_wait_is_not_asked_again(env):
    env.s.history_set(None, D14, None)
    lock = hold_the_cycle_lock(env)
    try:
        env.step(3)
    finally:
        lock.release()
    assert env.asked() == [D14, D15] and sorted(p["date"] for p in env.doc["pending"]) == [D14, D15]


def test_a_pending_file_of_the_older_version_is_a_list_of_one(tmp_path):
    assert trades_history.pending_of({"pending": None}) == [] and trades_history.pending_of({}) == []
    assert trades_history.pending_of({"pending": {"dir": "x"}}) == [{"dir": "x"}]
    assert trades_history.pending_of({"pending": [{"dir": "a"}, "junk", {"dir": "b"}]}) == [{"dir": "a"}, {"dir": "b"}]


# ---- taken in between the dates of the backfill --------------------------------------------------------------------

def test_files_are_taken_in_between_the_dates_of_a_long_backfill(env, tmp_path, monkeypatch):
    """The autopilot's backfill holds the cycle lock for every date it computes; the worker asks a date meanwhile (the
    lock is busy: its files wait) and the backfill takes it in before its next date, under the lock it holds."""
    env.s.history_set(None, D13, None)
    load(env.url, tmp_path, D12, 11)               # other positions: the four dates are stale, the backfill has work
    events: list[str] = []
    ran = []
    real_run = dayrun.eod_run

    def eod_run(settings_, day, **kw):
        events.append("run " + day)
        ran.append(day)
        if len(ran) == 1:                          # the worker thread asks the next date while the backfill computes
            env.clock["t"] += timedelta(seconds=130)
            env.s._history_step()
            events.append(f"worker asked, {len(env.doc['pending'])} waiting")
        return real_run(settings_, day, **kw)

    monkeypatch.setattr(dayrun, "eod_run", eod_run)

    def between() -> int:
        n = env.s.take_in_history()
        events.append(f"between {n}")
        return n

    lock = hold_the_cycle_lock(env)
    try:
        assert env.plan(D12).count("todo") == 4
        res = backfill.run(env.st, env.plan(D12), store_url=env.url, out=lambda line: None, between=between)
    finally:
        lock.release()
    assert res["done"] == [D12, D13, D14, D15]
    assert events[:4] == ["between 0", f"run {D12}", "worker asked, 1 waiting", "between 1"], events
    assert events.index("between 1") < events.index(f"run {D13}")      # in between two dates, not after the run
    assert env.doc["pending"] == [] and env.marks()[D13] == "fetched"
    assert env.asked()[0] == D13
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s",
                           (D13,)).fetchone()[0] == 1
    assert not helper_proc.HELPER_LOCK.locked()


def test_between_does_nothing_without_files_or_while_the_helper_is_busy(env):
    env.s.history_set(None, D15, None)
    assert env.s.take_in_history() == 0                       # no files wait
    lock = hold_the_cycle_lock(env)
    try:
        env.s._history_step()
        assert env.doc["pending"]
        assert helper_proc.HELPER_LOCK.acquire(timeout=1)     # a feed run, a job: not waited for
        try:
            t0 = time.monotonic()
            assert env.s.take_in_history() == 0 and time.monotonic() - t0 < 5
        finally:
            helper_proc.HELPER_LOCK.release()
        env.s._cancel.set()                                   # «Остановить»: the files stay for the worker's step
        assert env.s.take_in_history() == 0 and env.doc["pending"]
        env.s._cancel.clear()
        before = env.s._running
        assert env.s.take_in_history() == 1 and env.s._running is before     # what the worker showed is put back
    finally:
        lock.release()
    assert env.doc["pending"] == [] and env.marks() == {D15: "fetched"} and not helper_proc.HELPER_LOCK.locked()


def test_between_leaves_what_the_worker_shows_alone(env, monkeypatch):
    """``_running`` is the worker's: a feed that waits for the cycle lock, the owner's operation run in that wait
    (``_owner_first`` replaces it and puts the feed's back). The autopilot's thread neither writes it nor restores a copy."""
    env.s.history_set(None, D15, None)
    lock = hold_the_cycle_lock(env)
    try:
        env.s._history_step()
        feed = {"kind": "feed", "feed": "tradeshist", "since": "x", "phase": "ingesting"}
        owner = {"kind": "op", "op": "action", "since": "y", "phase": "running"}
        env.s._running = feed
        seen = []
        real = env.s._ingest

        def ingest(*a, **kw):
            seen.append(env.s._running)
            env.s._running = owner          # the worker thread changed it while the ingestion ran
            return real(*a, **kw)

        monkeypatch.setattr(env.s, "_ingest", ingest)
        assert env.s.take_in_history() == 1
        assert seen == [feed] and env.s._running is owner      # not shown over the feed, not put back over the owner's
    finally:
        env.s._running = None
        lock.release()
    assert env.doc["pending"] == [] and not helper_proc.HELPER_LOCK.locked()


def test_a_file_the_autopilot_took_in_is_not_taken_in_again_by_the_worker(env, monkeypatch):
    """The worker's step read the list, then waited for the helper lock; the autopilot took the file in meanwhile. The
    worker reads the list again once the helper lock is its own."""
    env.s.history_set(None, D15, None)
    lock = hold_the_cycle_lock(env)
    try:
        env.s._history_step()
        stale = env.doc
        assert [p["date"] for p in stale["pending"]] == [D15]
        assert env.s.take_in_history() == 1
    finally:
        lock.release()
    calls = []
    real = env.s._ingest
    monkeypatch.setattr(env.s, "_ingest", lambda *a, **kw: calls.append(1) or real(*a, **kw))
    f = env.s._history_feed(svcs.read_state(env.st))
    assert env.s._history_drain(env.st, f, stale) is None and calls == []      # nothing left to take in: not again
    assert not helper_proc.HELPER_LOCK.locked() and env.marks() == {D15: "fetched"}
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s",
                           (D15,)).fetchone()[0] == 1


def test_between_runs_before_each_date_and_its_errors_do_not_stop_the_run(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    seen: list[str] = []

    def between() -> None:
        seen.append("b")
        raise RuntimeError("the hook failed")

    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    res = backfill.run(s, p, store_url=store_url, out=lambda line: None, between=between)
    assert res["done"] == ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"] and seen == ["b"] * 4


def test_the_autopilot_calls_the_hook_between_dates_and_ignores_its_errors(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_from="2026-10-01")
    calls = []

    def hook() -> int:
        calls.append(1)
        return 0 if len(calls) < 2 else 1 // 0

    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    assert ap.between_dates is None
    ap.between_dates = hook
    st = ap.run_cycle(hub=False)
    step = next(x for x in st["steps"] if x["name"] == "backfill")
    assert step["status"] == "ok" and "ran 3 dates" in step["detail"], step         # the 12.-14.10 (15.10: the eod step)
    assert len(calls) >= 3, calls                                                    # before each date, the error ignored


# ---- today's feed waits for the lock without the helper lock ------------------------------------------------------

def test_a_feed_waiting_for_the_cycle_lock_gives_the_helper_lock_up_and_takes_it_again(env):
    lock = hold_the_cycle_lock(env)
    got: dict = {}
    assert helper_proc.HELPER_LOCK.acquire(timeout=1)         # the feed's run holds it, as ``_feed`` does

    def feed_wait() -> None:
        try:
            got["lock"] = env.s._wait_cycle_lock(env.st)
            got["held"] = helper_proc.HELPER_LOCK.locked()
        except BaseException as e:  # noqa: BLE001
            got["error"] = e

    t = threading.Thread(target=feed_wait)
    t.start()
    try:
        until = time.monotonic() + 10
        while helper_proc.HELPER_LOCK.locked() and time.monotonic() < until:
            time.sleep(0.01)
        assert not helper_proc.HELPER_LOCK.locked()           # free while it waits: the history files can go in
        env.s.history_set(None, D15, None)
        env.s._history_step()                                 # the history worker's helper work is not held off by it
        assert env.doc["pending"]                             # (the lock is busy: the file waits)
        assert env.s.take_in_history() == 1                   # taken in with the cycle lock that is held elsewhere
    finally:
        lock.release()                                        # the cycle ends: the feed gets the lock and the helper
    t.join(10)
    assert not t.is_alive() and "error" not in got and got["lock"] is not None and got["held"] is True
    got["lock"].release()
    assert helper_proc.HELPER_LOCK.locked()                   # the feed's end releases it, as before
    helper_proc.HELPER_LOCK.release()


def test_a_feed_that_gave_the_wait_up_still_holds_the_helper_lock(env, monkeypatch):
    monkeypatch.setattr(svcs, "LOCK_WAIT_S", 0.5)
    lock = hold_the_cycle_lock(env)
    assert helper_proc.HELPER_LOCK.acquire(timeout=1)
    try:
        assert env.s._wait_cycle_lock(env.st) is None         # a timeout of the wait
        assert helper_proc.HELPER_LOCK.locked()               # held again: the caller's end releases it
    finally:
        helper_proc.HELPER_LOCK.release()
        lock.release()
    env.s._cancel.set()                                       # and a cancel
    lock = hold_the_cycle_lock(env)
    assert helper_proc.HELPER_LOCK.acquire(timeout=1)
    try:
        with pytest.raises(svcs.ServiceError, match="cancelled"):
            env.s._wait_cycle_lock(env.st)
        assert helper_proc.HELPER_LOCK.locked()
    finally:
        helper_proc.HELPER_LOCK.release()
        lock.release()
        env.s._cancel.clear()


def test_a_feed_does_not_wait_with_the_cycle_lock_in_hand_for_a_busy_helper(env, monkeypatch):
    """The cycle lock is free but a job took the helper lock in the wait: the cycle lock is let go at once (the autopilot
    must not wait for a login window), the wait goes on."""
    monkeypatch.setattr(svcs, "LOCK_WAIT_S", 3.0)
    assert helper_proc.HELPER_LOCK.acquire(timeout=1)         # the feed's run
    got: dict = {}

    def feed_wait() -> None:
        got["lock"] = env.s._wait_cycle_lock(env.st)

    t = threading.Thread(target=feed_wait)
    other = threading.Event()

    def job() -> None:   # an acquisition job: takes the helper lock as soon as the feed let it go
        while not helper_proc.HELPER_LOCK.acquire(timeout=0.01):
            pass
        other.set()
        time.sleep(0.6)
        probe = CycleLock(env.st.home)
        got["free_while_job"] = probe.acquire()               # the cycle lock was not kept by the waiting feed
        probe.release()
        helper_proc.HELPER_LOCK.release()

    j = threading.Thread(target=job)
    lock = hold_the_cycle_lock(env)
    t.start()
    j.start()
    time.sleep(0.2)
    assert other.wait(5)
    lock.release()                                            # the cycle is over; the job still has the helper
    t.join(15)
    j.join(5)
    assert got["free_while_job"] is True and got["lock"] is not None   # then the feed got both
    got["lock"].release()
    helper_proc.HELPER_LOCK.release()


# ---- the order of the dates --------------------------------------------------------------------------------------

def test_the_dates_whose_trades_were_asked_again_run_first_each_group_oldest_first():
    days = [backfill.Day("2026-10-12", "todo", "no default run"), backfill.Day("2026-10-13", "todo", backfill.INPUTS_WHY),
            backfill.Day("2026-10-14", "done"), backfill.Day("2026-10-15", "todo", "positions changed"),
            backfill.Day("2026-10-16", "todo", backfill.INPUTS_WHY), backfill.Day("2026-10-19", "failed", "x")]
    p = backfill.Plan("2026-10-12", "2026-10-19", days, True)
    assert [d.date for d in p.todo] == ["2026-10-13", "2026-10-16", "2026-10-12", "2026-10-15"]
    assert p.summary()["next"] == ["2026-10-13", "2026-10-16", "2026-10-12", "2026-10-15"]


def test_a_date_with_new_trades_runs_before_older_stale_dates_and_every_date_comes_out_current(tmp_path, cache, store_url,
                                                                                               monkeypatch):
    """The order is only the order: after the run every date is current, the older ones too (the chain of the spreads is
    the plan's: a date run before an older one is made again when the older one comes out with another spread)."""
    from desk.inbox import scan

    root, cfg = tmp_path / "adapter", tmp_path / "config"
    root.mkdir()
    cfg.mkdir()
    (root / "__init__.py").write_text("from desk.inbox.reference import SPI_VERSION, match, read, reference, describe\n")
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity;active\nOFZ;Synthetic;Test;TRADING;TEST;1\n"
                                   "CORP;Other;Test;TRADING;TEST;1\n")
    s = settings(tmp_path, cache, store_url, private_adapter=str(root), private_adapter_config=cfg)
    load(store_url, tmp_path, D12, 10)
    with store.connect(store_url) as con:   # the 15th has its run; the 12th to the 14th have none
        backfill.run(s, backfill.plan(con, s, date(2026, 10, 15), date(2026, 10, 15)), store_url=store_url,
                     out=lambda line: None)
    from test_market_history import OFZ

    f = tmp_path / "trades_20261016_1845.csv"
    f.write_text("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;"
                 "source_instrument_id;side;quantity;amount_ccy;ccy;_report_date\n"
                 f"TR-1;1;live;{D15};{D15}T10:30:00;OFZ;P1;bond;{OFZ};buy;5;;RUB;{D15}\n", encoding="utf-8")
    with store.connect(store_url) as con:
        assert scan.ingest_feed(con, s, f, source="trades_csv", handle=scan.adapter(s)).files == {"accepted": 1}
        p = backfill.plan(con, s, date(2026, 10, 12), date(2026, 10, 15))
    assert [(d.date, d.why) for d in p.todo] == [(D15, backfill.INPUTS_WHY), (D12, "no default run"),
                                                  (D13, "no default run"), (D14, "no default run")]
    ran = []
    real_run = dayrun.eod_run
    monkeypatch.setattr(dayrun, "eod_run", lambda settings_, day, **kw: ran.append(day) or real_run(settings_, day, **kw))
    res = backfill.run(s, p, store_url=store_url, out=lambda line: None)
    assert ran[:4] == [D15, D12, D13, D14] and not res["failed"], ran
    with store.connect(store_url) as con:
        again = backfill.plan(con, s, date(2026, 10, 12), date(2026, 10, 15))
    # nothing is left but what the chain of the spreads asks (the 15th ran before its older dates)
    assert {d.why for d in again.todo} <= {backfill.CARRY_WHY}, [(d.date, d.why) for d in again.todo]
    for _ in range(3):
        if not again.todo:
            break
        with store.connect(store_url) as con:
            backfill.run(s, again, store_url=store_url, out=lambda line: None)
            again = backfill.plan(con, s, date(2026, 10, 12), date(2026, 10, 15))
    assert not again.todo and again.count("done") == 4


# ---- the stamp of the inputs is of the date --------------------------------------------------------------------

def _set(con, dataset: str, valid: date, book: str = "OFZ") -> None:
    import hashlib
    import json

    scope = {"book_id": book}
    n = con.execute("SELECT count(*) FROM dataset_set").fetchone()[0]
    sha = hashlib.sha256(f"{dataset}{valid}{n}".encode()).hexdigest()
    con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, 'x', 'synthetic.csv', 1, now())",
                (sha,))
    con.execute("INSERT INTO dataset_set (dataset, scope, scope_key, valid_date, revision, source, report_date,"
                " inbox_file_sha256, adapter_fingerprint, unit_hash, row_count, source_name)"
                " VALUES (%s, %s, %s, %s, (SELECT coalesce(max(revision), 0) + 1 FROM dataset_set WHERE dataset = %s"
                " AND valid_date = %s), 'synthetic', %s, %s, 'fp', 'uh', 0, 'synthetic.csv')",
                (dataset, Jsonb(scope), json.dumps(scope, sort_keys=True), valid, dataset, valid, valid, sha))


def test_the_memo_of_the_hashes_survives_a_set_that_cannot_reach_them(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    early = [date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)]
    late = date(2027, 1, 20)                                       # more than 60 days after the early ones
    calls: list[date] = []
    real = trades.intraday_inputs
    monkeypatch.setattr(trades, "intraday_inputs", lambda con, d, *a, **kw: calls.append(d) or real(con, d, *a, **kw))
    backfill._HASH_MEMO.clear()

    def hashes(days) -> list[str]:
        calls.clear()
        with store.connect(store_url) as con:
            return [backfill.inputs_hash(con, d, s) for d in days]

    base = hashes([*early, late])
    assert len(calls) == 4 and hashes([*early, late]) == base and calls == []     # a memo
    with store.connect(store_url) as con:
        _set(con, "ocp", date(2026, 10, 13))                       # another dataset: the hash does not read it
        _set(con, "cash_balances", date(2026, 10, 14))
    assert hashes([*early, late]) == base and calls == []
    with store.connect(store_url) as con:
        _set(con, "trades", late)                                  # a set of a date out of reach of the early ones
    hashes([*early, late])
    assert calls == [late]
    with store.connect(store_url) as con:
        _set(con, "trades", date(2026, 10, 13))                    # in reach: the date and the dates after it
    hashes([*early, late])
    assert calls == [date(2026, 10, 13), date(2026, 10, 14)]       # (the 12th is before it; the late one is 90 days on)
    with store.connect(store_url) as con:
        _set(con, "trade_terms", date(2026, 10, 14))               # a stock: in force from its date on, the later ones too
    hashes([*early, late])
    assert calls == [date(2026, 10, 14), late]
    for d in (*early, late):                                       # a hit is never a stale hash
        with store.connect(store_url) as con:
            assert backfill.inputs_hash(con, d, s) == real(con, d, s)[0].private_inputs_hash, d


def test_the_stamp_still_changes_with_every_other_input(tmp_path, cache, store_url):
    """The tuple of the stamp is what is the same for every date: a position set, the books reference, the settings."""
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        a = backfill.inputs_stamp(con, s)
        assert backfill.inputs_stamp(con, s) == a and a.of(date(2026, 10, 14)) == ((0, 0), (0, 0))
    load(store_url, tmp_path, "2026-10-12", 10)
    with store.connect(store_url) as con:
        assert backfill.inputs_stamp(con, s) != a
        _set(con, "trades", date(2026, 10, 14))
        b = backfill.inputs_stamp(con, s)
        assert tuple(b) == tuple(backfill.inputs_stamp(con, s))
        assert b.of(date(2026, 10, 14))[0] != (0, 0) and b.of(date(2026, 10, 13)) == ((0, 0), (0, 0))
        assert b.of(date(2026, 10, 14) + timedelta(days=trades.MAX_CUT_DAYS))[0] != (0, 0)
        assert b.of(date(2026, 10, 14) + timedelta(days=trades.MAX_CUT_DAYS + 1))[0] == (0, 0)
