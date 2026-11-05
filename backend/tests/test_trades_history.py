"""The trades of past dates (TDM#569, ``desk.trades_history`` and the worker in ``desk.adapter_services``): the
candidates and their order, the marks of the answers, the limits, the files waiting for the cycle lock, no run made after
the ingestion (the eod and backfill steps make them), the routes. A synthetic helper (``test_adapter_services``). Synthetic
data only."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_adapter_services import (
    FORM,
    LOOP,
    make,
    mode,
    requests,
    run_feed,
    service,
    submit,
)
from test_r23_backfill import cache, load  # noqa: F401 - the synthetic market fixture

from desk import adapter_services as svcs
from desk import backfill, helper_proc, recompute, store, trades_history
from desk.api import create_app
from desk.autopilot import CycleLock, in_trading_hours
from desk.inbox import scan
from desk.inbox.timesem import MSK
from desk.settings import Settings

BOOKS = [{"book_id": b, "name": b, "desk": "Test", "book_type": "TRADING", "entity": "TEST", "active": True}
         for b in ("OFZ", "CORP")]
D12, D13, D14, D15, FRI = "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", date(2026, 10, 16)


@pytest.fixture(autouse=True)
def _fresh():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    assert not helper_proc.HELPER_LOCK.locked()


def at(d: date, h: int, m: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=MSK)


class Env:
    def __init__(self, st: Settings, s: svcs.AdapterServices, clock: dict, url: str):
        self.st, self.s, self.clock, self.url = st, s, clock, url

    def step(self, n: int = 1, dt: int = 130) -> list[bool]:
        out = []
        for _ in range(n):
            out.append(self.s._history_step())
            self.clock["t"] += timedelta(seconds=dt)
        return out

    @property
    def doc(self) -> dict:
        return trades_history.read(self.st)

    def marks(self) -> dict[str, str]:
        return {d: m["state"] for d, m in sorted(self.doc["marks"].items())}

    def asked(self) -> list[str]:
        return [r["date"] for r in requests(self.st) if r["op"] == "feed" and r["feed"] == "tradeshist"]

    def put(self, name: str, text: str) -> None:
        (svcs.state_dir(self.st) / name).write_text(text)

    def plan(self, lo: str = D12) -> backfill.Plan:
        with store.connect(self.url) as con:
            return backfill.plan(con, self.st, date.fromisoformat(lo), FRI)


@pytest.fixture
def env(tmp_path, store_url, cache, request):  # noqa: F811
    st = make(tmp_path, snapshot_cache=cache, store_url=store_url)
    load(store_url, tmp_path, D12, 10)
    with store.connect(store_url) as con:
        store.store_books(con, BOOKS, "fp")
        backfill.run(st, backfill.plan(con, st, date(2026, 10, 12), date(2026, 10, 15)), store_url=store_url,
                     out=lambda line: None)
    mode(st, "history_feed")
    clock = {"t": at(FRI, 21, 0)}
    s = service(st, store_url, clock)
    e = Env(st, s, clock, store_url)
    try:
        submit(s, "describe")
        submit(s, "action", action="login")
        s.history_set(True, D12, None)
        yield e
    finally:
        s.close()


# ---- the candidates, the order, the hours --------------------------------------------------------------------------

def test_the_blocks_go_from_the_newest_block_to_the_older_ones_and_from_old_to_new_inside(monkeypatch):
    cands = [f"2026-10-{d}" for d in range(30, 9, -1)]   # 21 dates, the newest first
    monkeypatch.setattr(trades_history, "BLOCK", 10)
    got = trades_history.order(cands, set(cands), [])
    assert got[:10] == sorted(cands[:10]) and got[10:20] == sorted(cands[10:20]) and got[20:] == cands[20:]
    # a block is finished before the next one starts, whatever of it is still to ask
    todo = set(cands) - {got[0], got[1]}
    assert trades_history.order(cands, todo, [])[0] == got[2]
    # what the owner asked again goes first
    assert trades_history.order(cands, set(cands), ["2026-10-12"])[0] == "2026-10-12"


def test_candidates_today_never_yesterday_after_seven_the_adapters_first_date_and_backfill_from(env):
    with store.connect(env.url) as con:
        def cands(now, hf=None, doc=None):
            return trades_history.candidates(con, env.st, doc or env.doc, now, hf)
        assert cands(at(FRI, 20)) == [D15, D14, D13, D12]
        assert cands(at(FRI, 6, 59)) == [D14, D13, D12]               # yesterday only from 07:00
        assert cands(at(FRI, 7, 0)) == [D15, D14, D13, D12]
        assert cands(at(date(2026, 10, 15), 20)) == [D14, D13, D12]   # that day's own run is not a candidate
        assert cands(at(FRI, 20), hf=D14) == [D15, D14]                # the adapter's first date
        before = {**env.doc, "from": "2026-01-02"}                     # never before the setting backfill_from
        assert trades_history.lower_bound(Settings(backfill_from="2026-10-13"), before, None, FRI) == date(2026, 10, 13)
        assert trades_history.lower_bound(env.st, {**env.doc, "from": None}, None, FRI) == date(2026, 1, 5)
    assert trades_history.default_from(env.st, FRI) == date(2026, 1, 5)
    assert trades_history.default_from(Settings(backfill_from="2025-03-01"), FRI) == date(2026, 1, 1)


def test_a_saturday_with_a_hub_close_is_a_candidate(monkeypatch, env):
    monkeypatch.setattr(store, "default_runs", lambda con, since: {date(2026, 10, 17): {}, date(2026, 10, 16): {}})
    with store.connect(env.url) as con:
        got = trades_history.candidates(con, env.st, env.doc, at(date(2026, 10, 19), 9), None)
    assert got == ["2026-10-17", "2026-10-16"]   # the feed of the day skips a Saturday (weekdays_only); this one asks


def test_a_set_made_after_the_end_of_the_date_covers_it_without_a_request(env, tmp_path):
    head = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;source_instrument_id;"
            "side;quantity;amount_ccy;ccy")
    row = "TR-M;1;live;{d};{d}T10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;5;;RUB"
    drop = tmp_path / "drop"
    drop.mkdir()
    same = drop / "trades_20261014_2300.csv"   # cut at 23:00 of the 14th itself: the end of the day is not known
    same.write_text(head + "\n" + row.format(d=D14) + "\n", encoding="utf-8")
    late = drop / "trades_20261016_0100.csv"   # a manual export of the 16th, 01:00, holding the 15th
    late.write_text(head + ";_report_date\n" + row.format(d=D15) + ";" + D15 + "\n", encoding="utf-8")
    with store.connect(env.url) as con:
        for f in (same, late):
            assert scan.ingest_feed(con, env.st, f, source="trades_csv", handle=scan.adapter(env.st)).files == {"accepted": 1}
        assert trades_history.covered(con, [D15, D14], env.doc) == [D15]
    assert env.step() == [True] and env.asked() == [D12]   # the 12th is first; 15th has its mark, no request
    assert env.doc["marks"][D15]["code"] == "covered" and env.marks()[D15] == "fetched"
    env.clock["t"] = at(date(2026, 10, 17), 20)
    env.step(4)
    assert D15 not in env.asked()                            # not even in the window of the newest dates


# ---- a date with trades --------------------------------------------------------------------------------------------

def test_trades_are_taken_in_without_a_run_and_the_backfill_makes_the_dates_again(env, monkeypatch):
    env.s.history_set(None, D13, None)
    env.put("hist_priced_" + D13, "")   # the trade has its price: the whole PnL of it is in
    env.put("hist_mode_" + D14, "no_records")
    env.put("hist_mode_" + D15, "no_records")
    calls = []
    monkeypatch.setattr(recompute, "after_inputs", lambda *a, **kw: calls.append(a) or {"runs": [], "failed": []})
    assert env.plan(D13).count("todo") == 0
    assert env.step(3) == [True, True, True]
    assert env.asked() == [D13, D14, D15] and calls == []   # no run after the ingestion
    assert env.marks() == {D13: "fetched", D14: "no_trades", D15: "no_trades"}
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == 1
    p = env.plan(D13)
    # the date of the trades and, the book having no report since, the days after it (they start with the position)
    assert [(d.date, d.why) for d in p.todo] == [(D13, "private inputs changed"), (D14, "private inputs changed"),
                                                (D15, "private inputs changed")]
    res = backfill.run(env.st, p, store_url=env.url, out=lambda line: None)
    assert res["done"] == [D13, D14, D15]
    with store.connect(env.url) as con:
        rid = store.default_runs(con, date(2026, 10, 13))[date(2026, 10, 13)]["run_id"]
        lin = con.execute("SELECT lineage->'intraday_trades' FROM run WHERE run_id = %s", (rid,)).fetchone()[0]
        assert lin["applied"] == 1 and lin["set_ids"]
        summary = trades_history.applied(con, env.st, env.doc)
    assert (summary["full"], summary["partial"], summary["reasons"], summary["waiting_recompute"]) == (1, 0, {}, 0)
    assert summary["in_pnl"] == 1
    assert env.plan(D13).count("todo") == 0


def test_the_summary_says_waiting_for_the_run_and_no_effect(env):
    env.s.history_set(None, D15, None)
    env.step()
    with store.connect(env.url) as con:
        a = trades_history.applied(con, env.st, env.doc)
    assert (a["waiting_recompute"], a["in_pnl"], a["no_effect"]) == (1, 0, 0)
    env.put("hist_mode_" + D14, "ok")
    # trades of a kind that does not enter the inputs: the hash stays, the run is not made again (no effect)
    env.put("hist_extra", "")
    h = env.s.history()
    assert h["available"] and h["counts"]["fetched"] == 1 and h["applied"]["waiting_recompute"] == 1
    assert h["enabled"] and h["from"] == D15 and h["problems"] == []


def test_an_empty_answer_is_a_mark_by_its_code_and_the_files_status_is_not_a_failure(env):
    env.s.history_set(None, D14, None)
    env.put("hist_mode_" + D14, "no_records_file")   # a file with the header only
    env.put("hist_mode_" + D15, "no_records")
    env.step(2)
    assert env.marks() == {D14: "no_trades", D15: "no_trades"}
    assert env.doc["failures"] == 0 and env.doc["last"]["code"] == "no_records"
    again = env.step(2)   # the same day: not asked again
    assert again == [False, False] and env.asked() == [D14, D15]


def test_unmapped_stays_until_the_adapter_changes(env):
    env.s.history_set(None, D15, None)
    env.put("hist_mode", "unmapped")
    env.step()
    assert env.marks() == {D15: "unmapped"} and env.doc["marks"][D15]["code"] == "books_unmapped"
    env.clock["t"] += timedelta(days=1)
    assert env.step() == [False]
    books = Path(env.st.private_adapter_config) / "books.csv"
    books.write_text(books.read_text() + "NEW;New;Test;TRADING;TEST;1\n")   # the adapter's settings changed
    submit(env.s, "describe")
    env.put("hist_mode", "ok")
    assert env.step() == [True] and env.marks() == {D15: "fetched"} and env.asked() == [D15, D15]


def test_an_empty_answer_with_no_reason_is_unmapped_and_a_wrong_date_is_not_marked(env):
    env.s.history_set(None, D15, None)
    env.put("hist_mode", "empty")
    env.step()
    m = env.doc["marks"][D15]
    assert (m["state"], m["code"]) == ("unmapped", "empty")
    env.s.history_refetch(D15, D15, True)
    env.put("hist_mode", "wrongdate")
    env.step()
    assert env.doc["marks"].get(D15) is None and env.doc["last"]["code"] == "helper_response_invalid"
    assert env.doc["failures"] == 1 and env.doc["retry_at"]


def test_an_unavailable_date_is_asked_again_after_a_day_and_the_next_date_goes_on(env):
    env.put("hist_mode_" + D13, "unavailable")
    env.put("hist_mode", "no_records")
    env.step(4)
    assert env.marks() == {D12: "no_trades", D13: "unavailable", D14: "no_trades", D15: "no_trades"}
    m = env.doc["marks"][D13]
    assert (m["code"], m["attempts"]) == ("date_unavailable", 1)
    assert _ts(m["next_at"]) - _ts(m["at"]) == timedelta(days=1)
    env.clock["t"] = _ts(m["next_at"]) + timedelta(minutes=1)
    assert D13 in env.asked() and env.step() == [True] and env.asked()[-1] == D13   # 13 is the first due; 15, 14 too
    assert env.doc["marks"][D13]["attempts"] == 2 and _ts(env.doc["marks"][D13]["next_at"]) - env.clock["t"] > timedelta(days=2)


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v)


def test_an_incomplete_export_is_asked_again_by_the_backoff_and_then_unavailable(env):
    env.s.history_set(None, D15, None)
    env.put("hist_mode", "partial")
    gaps = []
    for _ in range(3):
        env.step()
        m = env.doc["marks"][D15]
        gaps.append((m["state"], m["attempts"]))
        env.clock["t"] = _ts(m["next_at"]) + timedelta(seconds=1) if m["next_at"] else env.clock["t"]
    assert gaps == [("partial", 1), ("partial", 2), ("unavailable", 1)]
    assert env.marks() == {D15: "unavailable"} and env.asked() == [D15] * 3


def test_a_login_is_waited_for_and_today_feed_state_is_not_touched(env):
    env.s.history_set(None, D15, None)
    (svcs.state_dir(env.st) / "logged_in").unlink()
    run_before = svcs.read_state(env.st)["runs"].get("tradeshist")
    env.step()
    h = env.doc
    assert h["wait"]["code"] == "login_required" and h["marks"] == {} and h["failures"] == 0
    assert svcs.read_state(env.st)["runs"].get("tradeshist") == run_before   # the schedule, retries, pause: its own
    assert env.step(2) == [False, False] and len(env.asked()) == 1
    assert env.s.history()["waiting"] == "login_required"
    submit(env.s, "action", action="login")   # a describe after the login: the wait ends
    assert env.doc["wait"] is None
    env.step()
    assert env.marks() == {D15: "fetched"}


def test_a_network_error_marks_nothing_and_holds_the_worker_back(env):
    env.s.history_set(None, D14, None)
    env.put("hist_mode", "error")
    env.step()
    h = env.doc
    assert h["marks"] == {} and h["failures"] == 1 and _ts(h["retry_at"]) == env.clock["t"] - timedelta(seconds=130) + timedelta(minutes=5)
    assert env.step() == [False]          # the retry time has not come
    env.clock["t"] = _ts(h["retry_at"]) + timedelta(seconds=1)
    env.put("hist_mode", "ok")
    assert env.step() == [True] and env.doc["failures"] == 0 and env.doc["retry_at"] is None
    assert env.asked() == [D14, D14]


def test_a_new_version_of_the_trades_is_a_new_revision_and_the_same_content_is_not(env):
    env.s.history_set(None, D15, None)
    env.step()
    backfill.run(env.st, env.plan(D15), store_url=env.url, out=lambda line: None)
    assert env.plan(D15).count("todo") == 0
    env.clock["t"] += timedelta(days=1)       # the next day the date is in the window again: asked once
    with store.connect(env.url) as con:
        sets = con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0]
    env.step()
    with store.connect(env.url) as con:    # the same content: no revision (a duplicate file), the date is current
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == sets
    assert env.plan(D15).count("todo") == 0
    env.clock["t"] += timedelta(days=1)
    env.put("hist_hhmm", "1930")
    env.put("hist_extra", "TR-DAY;2;cancelled;DAY;DAYT10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;1000;;RUB")
    env.step()
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == sets + 1
    assert [(d.date, d.why) for d in env.plan(D15).todo] == [(D15, "private inputs changed")]


def test_the_newest_dates_are_asked_again_once_a_day_the_older_ones_not_and_the_period_button(env):
    env.put("hist_mode", "no_records")
    env.step(4)
    assert env.asked() == [D12, D13, D14, D15]
    env.clock["t"] = at(date(2026, 10, 17), 20)   # the next day: the newest REFRESH_BD candidates only
    env.step(4)
    assert env.asked()[4:] == [D13, D14, D15]
    env.clock["t"] += timedelta(hours=1)
    assert env.step() == [False]                    # once a calendar day
    with pytest.raises(svcs.ServiceError, match="invalid_request"):
        env.s.history_refetch(D12, "2026-10-17", True)   # not today or later
    assert env.s.history_refetch(D12, D13, False) == {"state": "confirm", "code": "confirm", "dates": 2}
    assert env.marks()[D12] == "no_trades"
    assert env.s.history_refetch(D12, D13, True)["dates"] == 2
    assert env.doc["priority"] == [D13, D12] and D12 not in env.doc["marks"]
    env.step(2)
    assert env.asked()[-2:] == [D12, D13] and env.doc["priority"] == []


def test_no_books_is_marked_and_asked_again_with_new_positions(env, tmp_path):
    with store.connect(env.url) as con:
        store.store_books(con, [{**b, "active": False, "valid_from": "2026-01-01"} for b in BOOKS], "fp2")
    env.s.history_set(None, D15, None)
    assert env.step() == [False]
    assert env.marks() == {D15: "no_books"} and env.asked() == []
    assert env.step() == [False]
    with store.connect(env.url) as con:
        store.store_books(con, [{**b, "valid_from": "2026-01-02"} for b in BOOKS], "fp3")
    load(env.url, tmp_path, D15, 11)                # new positions of the date
    assert env.step() == [True] and env.marks() == {D15: "fetched"}


# ---- the queue of the service: hours, limits, the scheduled feed ---------------------------------------------------

def test_off_hours_waits_for_the_end_of_the_trading_hours_and_any_does_not(env):
    assert env.doc["when"] == "any" and env.doc["enabled"] is True  # the defaults (owner, 04.11.2026)
    env.s.history_set(None, D15, "off_hours")
    env.clock["t"] = at(FRI, 11, 0)
    assert in_trading_hours(env.clock["t"]) and env.step() == [False] and env.asked() == []
    g = trades_history.gate(env.doc, env.clock["t"])
    assert g["code"] == "trading_hours" and _ts(g["until"]) == at(FRI, 19, 45)
    assert env.s.history()["next_at"] == g["until"] and env.s.history()["gate"] == "trading_hours"
    env.clock["t"] = at(date(2026, 10, 17), 11, 0)    # a Saturday is not trading hours
    assert not in_trading_hours(env.clock["t"])
    env.clock["t"] = at(FRI, 11, 0)
    env.s.set_enabled("terms", False)       # (the scheduled feed would be due at 11:00: it goes first)
    env.s.history_set(None, None, "any")
    assert env.step() == [True] and env.marks() == {D15: "fetched"}
    with pytest.raises(svcs.ServiceError, match="invalid_request"):
        env.s.history_set(None, None, "always")


def test_one_date_in_two_minutes_and_a_limit_a_day_and_the_answer_is_for_the_asked_date(env, monkeypatch):
    env.put("hist_mode", "no_records")
    assert env.s._history_step() is True
    assert env.s._history_step() is False                      # inside the gap
    assert env.s.history()["gate"] == "gap"
    env.clock["t"] += timedelta(seconds=121)
    assert env.s._history_step() is True
    monkeypatch.setattr(trades_history, "HISTORY_PER_DAY", 2)
    env.clock["t"] += timedelta(seconds=121)
    assert env.s._history_step() is False and env.s.history()["gate"] == "daily_limit"
    assert env.doc["calls"] == {"day": FRI.isoformat(), "n": 2}
    assert env.asked() == [D12, D13]
    env.clock["t"] = at(date(2026, 10, 17), 9)
    assert env.s._history_step() is True                       # the next day


def test_a_scheduled_feed_due_soon_or_asked_for_goes_first(env):
    env.s.history_set(None, D15, None)
    sdoc = svcs.read_state(env.st)
    env.clock["t"] = at(FRI, 19, 59, ) + timedelta(seconds=50)   # the terms feed (every 30 min to 20:00) is due
    assert env.step() == [False] and env.asked() == []
    env.clock["t"] = at(FRI, 21, 0)
    env.step()
    assert env.asked() == [D15]
    # the time of the last request counts: a feed due in less than that waits for nothing
    env.s.history_refetch(D15, D15, True)
    h = env.doc
    h["last_duration_s"] = 600.0
    svcs.write_state(env.st, sdoc)
    trades_history.write(env.st, h)
    env.clock["t"] = at(FRI, 21, 0)
    assert env.s._feed_due_soon(sdoc, at(date(2026, 10, 19), 7, 55), 600.0) is True    # 08:00 Monday is within 10 min
    assert env.s._feed_due_soon(sdoc, at(date(2026, 10, 19), 7, 0), 600.0) is False


def test_a_history_request_does_not_touch_the_feeds_run_or_wait_for_the_login_pause(env):
    env.s.history_set(None, D15, None)
    before = svcs.read_state(env.st)["runs"]
    env.step()
    assert svcs.read_state(env.st)["runs"] == before and env.s._running is None
    run = run_feed(env.s, env.st, "tradeshist", env.clock)   # today's feed keeps its own state afterwards
    assert (run["state"], run["code"]) == ("complete", "exported")
    assert env.doc["marks"][D15]["state"] == "fetched"


def test_a_feed_without_the_flag_is_not_used_and_the_screen_says_so(env):
    mode(env.st, "history_plain")
    submit(env.s, "describe")
    assert env.step() == [False] and env.asked() == []
    h = env.s.history()
    assert h["available"] is False and h["code"] == "history_unsupported" and h["feed"] is None
    with pytest.raises(svcs.ServiceError, match="history_unsupported"):
        env.s.history_refetch(D12, D13, False)


def test_the_first_date_is_clamped_to_backfill_from_and_the_summary_says_backfill_is_off(env):
    env.s.history_set(None, "2026-01-02", None)
    assert env.doc["from"] == "2026-01-05"
    assert env.s.history()["from"] == "2026-01-05" and env.s.history()["from_min"] == "2026-01-05"
    env.s.history_set(None, D13, None)
    h = env.s.history()
    assert h["from"] == D13 and h["dates"] == 3 and h["backfill_enabled"] is True
    env.st = Settings(**{**env.st.__dict__, "backfill": False})
    env.s._load = lambda: env.st
    assert env.s.history()["backfill_enabled"] is False
    for bad in ("16.10.2026", "2099-01-01", "2026-10-16"):
        with pytest.raises(svcs.ServiceError, match="invalid_request"):
            env.s.history_set(None, bad, None)


# ---- the files that wait for the cycle lock ------------------------------------------------------------------------

def test_a_busy_cycle_lock_keeps_the_files_pending_and_frees_the_helper(env, monkeypatch):
    env.s.history_set(None, D15, None)
    real = svcs.AdapterServices._try_cycle_lock
    busy = {"n": 1}

    def maybe(st):
        if busy["n"]:
            busy["n"] -= 1
            return None
        return real(st)

    monkeypatch.setattr(svcs.AdapterServices, "_try_cycle_lock", staticmethod(maybe))
    assert env.s._history_step() is True
    h = env.doc
    assert h["marks"] == {} and h["pending"]["date"] == D15 and h["pending"]["files"][0]["file"].endswith(".csv")
    assert not helper_proc.HELPER_LOCK.locked()                       # free for today's feed and the buttons
    assert (svcs.state_dir(env.st) / "out" / h["pending"]["dir"]).is_dir()
    for _ in range(svcs.KEEP_OUT + 2):                                # the pending folder survives the pruning
        (svcs.state_dir(env.st) / "out" / os.urandom(16).hex()).mkdir()
    svcs.AdapterServices._prune(env.st)
    assert (svcs.state_dir(env.st) / "out" / h["pending"]["dir"]).is_dir()
    env.clock["t"] += timedelta(seconds=10)    # the gap does not hold the files back; no other date is asked
    assert env.s._history_step() is True
    assert env.asked() == [D15] and env.marks() == {D15: "fetched"} and env.doc["pending"] is None
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == 1


def test_a_cycle_that_holds_the_lock_does_not_hold_the_scheduled_feed_back(env, monkeypatch):
    env.s.history_set(None, D15, None)
    monkeypatch.setattr(env.s, "_wait_cycle_lock", lambda st: None)   # today's feed would wait 20 minutes; not the test
    lock = CycleLock(env.st.home)
    assert lock.acquire()
    try:
        t0 = time.monotonic()
        env.s._history_step()
        assert time.monotonic() - t0 < 30 < svcs.LOCK_WAIT_S      # one try, not the wait of a feed's files
        assert env.doc["pending"] and not helper_proc.HELPER_LOCK.locked()
        assert env.s._history_step() is False and len(env.asked()) == 1   # still busy: kept, nothing asked
        run = run_feed(env.s, env.st, "tradeshist", {"t": at(FRI, 21, 0)})   # today's feed has the helper at once
        assert (run["state"], run["code"]) == ("error", "calculation_busy")   # it got the helper at once
    finally:
        lock.release()
    env.clock["t"] += timedelta(seconds=10)
    assert env.s._history_step() is True and env.marks() == {D15: "fetched"}


def test_a_restart_in_the_middle_of_a_request_asks_the_date_again_and_keeps_the_count(env):
    env.s.history_set(None, D15, None)
    h = env.doc
    trades_history.count_call(h, env.clock["t"])        # the process died after the call was counted
    trades_history.write(env.st, h)
    s2 = service(env.st, env.url, env.clock)
    try:
        assert s2._history_step() is False                 # the gap holds across the restart
        env.clock["t"] += timedelta(seconds=130)
        assert s2._history_step() is True and env.marks() == {D15: "fetched"}
        assert env.doc["calls"]["n"] == 2
    finally:
        s2.close()


def test_a_pending_folder_that_is_gone_is_dropped_and_the_date_asked_again(env):
    env.s.history_set(None, D15, None)
    h = env.doc
    h["pending"] = {"dir": os.urandom(16).hex(), "files": [{"file": "x.csv", "sha256": "0" * 64}], "state": "complete",
                    "code": "exported", "date": D15}
    trades_history.write(env.st, h)
    assert env.s._history_step() is False and env.doc["pending"] is None
    env.clock["t"] += timedelta(seconds=130)
    assert env.s._history_step() is True and env.marks() == {D15: "fetched"}


def test_the_threaded_worker_asks_for_the_history_when_nothing_else_is_due(env):
    env.s.history_set(None, D15, None)
    env.s.start()
    until = time.monotonic() + 40
    while D15 not in env.doc["marks"] and time.monotonic() < until:
        env.s._wake.set()   # (the thread idles for the tick, an hour here, after a step that did nothing: wake it)
        time.sleep(0.1)
    assert env.marks() == {D15: "fetched"}


# ---- the routes ----------------------------------------------------------------------------------------------------

def test_routes_summary_switch_and_the_period_button(env):
    app = create_app(env.url)
    app.state.adapter_services.close()
    app.state.adapter_services = env.s
    with TestClient(app, **LOOP) as c:
        r = c.get("/api/v1/adapter-services/history")
        j = r.json()
        assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
        assert j["available"] and j["enabled"] and j["counts"]["todo"] == 4 and j["from"] == D12 and j["when"] == "any"
        assert c.get("/api/v1/adapter-services/history", headers={"X-Forwarded-For": "10.0.0.1"}).status_code == 403
        post = lambda path, body, **h: c.post("/api/v1/adapter-services" + path, content=body, headers={**FORM, **h})
        assert post("/history", "enabled=false").status_code == 200 and env.doc["enabled"] is False
        assert post("/history", "enabled=true&from=2026-10-14&when=any").status_code == 200
        assert (env.doc["from"], env.doc["when"], env.doc["enabled"]) == (D14, "any", True)
        for bad in ("", "enabled=1", "from=14.10.2026", "when=never", "x=1", "enabled=true&enabled=false", "from=2099-01-01"):
            assert post("/history", bad).status_code == 422, bad
        assert post("/history", "enabled=true", Origin="https://evil.example").status_code == 403
        r = post("/history/refetch", "from=2026-10-14&to=2026-10-15")
        assert r.status_code == 200 and r.json() == {"state": "confirm", "code": "confirm", "dates": 2}
        assert post("/history/refetch", "from=2026-10-14&to=2026-10-15&confirm=true").json()["dates"] == 2
        assert env.doc["priority"] == [D15, D14]
        for bad in ("from=2026-10-14", "from=2026-10-15&to=2026-10-14", "from=a&to=b", "from=2026-10-14&to=2026-10-16",
                    "from=2026-10-14&to=2026-10-15&confirm=yes&x=1"):
            assert post("/history/refetch", bad).status_code == 422, bad
        text = json.dumps(c.get("/api/v1/adapter-services/history").json())
        assert "TR-" not in text and "SU26238" not in text      # no value of a trade
    c = TestClient(app, base_url="http://evil.example:8100", client=("127.0.0.1", 1))
    assert c.get("/api/v1/adapter-services/history").status_code == 403


def test_the_summary_without_the_adapter_or_the_history_file(tmp_path, store_url):
    st = make(tmp_path, store_url=store_url)
    s = service(st, store_url)
    try:
        h = s.history()
        assert h["available"] is False and h["enabled"] is False and h["counts"]["todo"] == 0 and h["applied"]["in_pnl"] == 0
    finally:
        s.close()


def test_defaults_are_on_any_time_and_silent_without_a_history_feed(tmp_path, store_url):
    """Owner, 04.11.2026: on by default, any time, from the start of the year; with no adapter feed that declares
    ``history`` the worker asks nothing and writes nothing."""
    st = make(tmp_path, store_url=store_url)
    d = trades_history.blank()
    assert (d["enabled"], d["when"], d["from"]) == (True, "any", None)
    assert trades_history.lower_bound(st, d, None, FRI) == trades_history.default_from(st, FRI)
    assert trades_history.lower_bound(st, d, "2026-03-02", FRI) == date(2026, 3, 2)  # not before the adapter's start
    s = service(st, store_url)
    try:
        assert s._history_step() is False and not trades_history.path(st).exists()
    finally:
        s.close()


def test_the_feed_switched_off_in_the_monitor_is_not_asked_for_past_dates(env):
    """The owner who switched the day's trades feed off («Фоновая загрузка») gets no requests for past dates either."""
    f = env.s._history_feed(svcs.read_state(env.st))
    env.s.set_enabled(f["id"], False)
    assert env.step() == [False] and env.asked() == []
    env.s.set_enabled(f["id"], True)
    assert env.step() == [True] and env.asked() != []


# ---- how far the trades are in the PnL: read from the lineage ----------------------------------------------------------

@pytest.mark.parametrize("it,want", [
    ({"applied": 3}, ("unknown", None)),
    ({"applied": 3, "no_pnl": 0, "lost": [], "unpriced": 0, "no_prev_close": 0}, ("full", None)),
    ({"applied": 3, "no_pnl": {"n": 2, "sum": 5.0}, "lost": [], "unpriced": 1}, ("partial", "no_pnl")),
    ({"applied": 3, "lost": ["A"], "unpriced": []}, ("partial", "lost")),
    ({"applied": 3, "unpriced": 2}, ("partial", "unpriced")),
    ({"applied": 3, "no_prev_close": {"n": 3, "sum": 1.0}, "no_pnl": 0}, ("outside", "no_prev_close")),
    ({"applied": 3, "no_pnl": True}, ("unknown", None)),
])
def test_completeness_from_the_lineage_keys(it, want):
    assert trades_history.completeness(it) == want


def _ot(**kw):
    base = {"no_prev_close": False, "no_pnl_run": False, "trades": 4, "no_pnl": {"positions": 0, "trades": 0, "pnl_rub": 0.0},
            "lost": {"bonds": 0, "trades": 0}, "unpriced": {"trades": 0}, "unvalued": {"trades": 0}}
    return {"applied": 4, "outside_total": {**base, **kw}}


@pytest.mark.parametrize("it,want", [
    (_ot(), ("full", None)),
    (_ot(no_prev_close=True), ("outside", "no_prev_close")),
    (_ot(no_pnl_run=True), ("outside", "no_pnl_run")),
    (_ot(no_pnl={"positions": 1, "trades": 1, "pnl_rub": 5.0}), ("partial", "no_pnl")),
    (_ot(lost={"bonds": 1, "trades": 2}, unpriced={"trades": 1}), ("partial", "lost")),
    (_ot(unpriced={"trades": 4}), ("outside", "unpriced")),
    (_ot(unvalued={"trades": 1}), ("partial", "unvalued")),
    ({**_ot(), "no_pnl": 3}, ("full", None)),   # outside_total wins over the plain keys
])
def test_completeness_from_outside_total(it, want):
    assert trades_history.completeness(it) == want


def test_every_past_date_refused_says_to_check_the_settings(env):
    env.put("hist_mode", "unavailable")
    env.step(4)
    assert set(env.marks().values()) == {"unavailable"}
    assert env.s.history()["hint"] == "all_unavailable"
    env.put("hist_mode_" + D12, "no_records")
    env.s.history_refetch(D12, D12, True)
    env.step()
    assert env.s.history()["hint"] is None


def test_the_two_no_records_cases_are_both_no_trades(env):
    """The adapter's two cases (no records at all: a file without units; records of other books only: empty units of the
    requested books): the mark is by the code, the date is quiet either way."""
    env.s.history_set(None, D14, None)
    env.put("hist_mode_" + D14, "no_records")        # no file
    env.put("hist_mode_" + D15, "no_records_file")   # a file with the header only
    env.step(2)
    assert env.marks() == {D14: "no_trades", D15: "no_trades"} and env.doc["failures"] == 0


def test_the_period_button_asks_the_service_again_for_a_date_it_already_has(env):
    """A real export is dated by the moment it was made (after the end of the date): the date counts as covered only when
    nobody asked for it. A date the owner asked again goes to the service all the same."""
    env.s.history_set(None, D15, None)
    env.put("hist_late", "2026-10-16")
    assert env.step() == [True] and env.asked() == [D15] and env.marks() == {D15: "fetched"}
    assert env.doc["marks"][D15]["code"] != "covered"
    env.clock["t"] += timedelta(days=2)               # (the window of the newest dates would ask on the next day anyway)
    with store.connect(env.url) as con:
        assert trades_history.covered(con, [D15], {"marks": {}}) == [D15]   # the set is later than the date
    env.s.history_refetch(D15, D15, True)
    env.clock["t"] += timedelta(seconds=130)
    env.put("hist_extra", "TR-DAY;2;cancelled;DAY;DAYT10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;1000;;RUB")
    assert env.step() == [True] and env.asked() == [D15, D15]
    assert env.doc["priority"] == [] and env.marks() == {D15: "fetched"}


def test_pending_files_are_taken_in_after_a_cancel_and_the_helper_lock_is_taken(env, monkeypatch):
    env.s.history_set(None, D15, None)
    real = svcs.AdapterServices._try_cycle_lock
    busy = {"n": 1}
    monkeypatch.setattr(svcs.AdapterServices, "_try_cycle_lock",
                        staticmethod(lambda st: (busy.update(n=0) or None) if busy["n"] else real(st)))
    env.s._history_step()
    assert env.doc["pending"]
    env.s._cancel.set()                                  # a cancel that came late must not hold the files for ever
    assert helper_proc.HELPER_LOCK.acquire(timeout=1)    # an operation holds the helper: the files wait one step
    try:
        assert env.s._history_step() is False and env.doc["pending"]
    finally:
        helper_proc.HELPER_LOCK.release()
    assert env.s._history_step() is True
    assert env.doc["pending"] is None and env.marks() == {D15: "fetched"} and not env.s._cancel.is_set()


def test_in_pnl_counts_only_the_dates_whose_total_has_the_trades(env):
    env.s.history_set(None, D13, None)
    env.put("hist_priced_" + D13, "")                       # the 13th: one trade, priced: full
    env.put("hist_priced_" + D14, "")                       # the 14th: two trades, one without a price: partial
    env.put("hist_extra_" + D14, "TR2-DAY;1;live;DAY;DAYT10:40:00;OFZ;P1;bond;SU26238RMFS4;buy;500;;RUB")
    env.put("hist_mode_" + D15, "no_records")
    env.step(3)
    backfill.run(env.st, env.plan(D13), store_url=env.url, out=lambda line: None)
    with store.connect(env.url) as con:
        a = trades_history.applied(con, env.st, env.doc)
    # a date that only carries the trades of the days before (no trade of its own in the book) is not counted anywhere
    assert a["dates"][D13] == "full" and a["dates"][D14] == "partial", a
    assert (a["full"], a["partial"], a["outside"], a["unknown"], a["in_pnl"]) == (1, 1, 0, 0, 2)
    assert a["reasons"] == {"unpriced": 1}


def test_a_date_whose_only_trade_has_no_price_is_outside_the_total(env):
    env.s.history_set(None, D13, None)
    env.put("hist_mode_" + D14, "no_records")
    env.put("hist_mode_" + D15, "no_records")
    env.step(3)
    backfill.run(env.st, env.plan(D13), store_url=env.url, out=lambda line: None)
    with store.connect(env.url) as con:
        a = trades_history.applied(con, env.st, env.doc)
    assert a["dates"][D13] == "outside" and a["reasons"] == {"unpriced": 1}
    assert (a["full"], a["partial"], a["outside"], a["in_pnl"]) == (0, 0, 1, 0)


def test_the_hash_of_a_date_follows_the_off_report_switch_without_a_restart(env, monkeypatch):
    """The memo of the plan is keyed by what ``trades.intraday_inputs`` reads, also the switch ``off_report_derivatives``
    (it puts the off-report trades into the hash): switching it in the same process gives the date its new hash."""
    from dataclasses import replace

    from desk import trades as trades_mod
    from desk.valuation import offreport
    real = offreport.stamp

    def stamp(inputs, on, mode, *a, **kw):
        out = real(inputs, on, mode, *a, **kw)
        return replace(out, private_inputs_hash=out.private_inputs_hash + "+" + mode) if mode == "include" else out

    monkeypatch.setattr(offreport, "stamp", stamp)
    monkeypatch.setattr(trades_mod.offreport, "stamp", stamp, raising=False)
    inc = replace(env.st, off_report_derivatives="include")
    with store.connect(env.url) as con:
        a = backfill.inputs_hash(con, date.fromisoformat(D13), env.st)
        b = backfill.inputs_hash(con, date.fromisoformat(D13), inc)
        assert backfill.inputs_stamp(con, env.st) != backfill.inputs_stamp(con, inc)
        assert b == a + "+include" and backfill.inputs_hash(con, date.fromisoformat(D13), env.st) == a


def test_the_end_of_the_date_is_the_moscow_midnight_whatever_the_session_time_zone(env, tmp_path):
    """The Moscow day is UTC+03:00 (no named zone: the Windows bundle has no time zone data). A report made at 23:59 of
    the date does not cover it; one made at 00:00 of the next day does, also with another session time zone."""
    head = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;source_instrument_id;"
            "side;quantity;amount_ccy;ccy;_report_date\n")
    row = "TR-{n};1;live;{d};{d}T10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;5;;RUB;{d}\n"
    drop = tmp_path / "drop"
    drop.mkdir()
    for name, d in (("trades_20261014_2359.csv", D14), ("trades_20261016_0000.csv", D15)):
        f = drop / name
        f.write_text(head + row.format(n=name[7:15], d=d), encoding="utf-8")
        with store.connect(env.url) as con:
            assert scan.ingest_feed(con, env.st, f, source="trades_csv", handle=scan.adapter(env.st)).files == {"accepted": 1}
    with store.connect(env.url) as con:
        for tz in ("UTC", "-05:00", "+09:00"):
            con.execute(f"SET TIME ZONE '{tz}'")
            assert trades_history.covered(con, [D15, D14], env.doc) == [D15], tz
