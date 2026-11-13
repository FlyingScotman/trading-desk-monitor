"""TDM#715: the trades of the dates an adapter with the shifted window of the query brought are asked again once, by the
monitor itself, and the new answer replaces the date whole (a book with records in the older set and none in the newer
cut is empty on the date; the older revisions stay). A synthetic helper (``test_adapter_services``); the «older» and the
«right» adapter are the same synthetic one before and after ``WINDOW_FIXED`` names it. Synthetic data only."""
# ruff: noqa: F811 - the fixture `env` of the worker is imported and used by name
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from test_adapter_services import run_feed
from test_r23_backfill import cache, load  # noqa: F401 - the synthetic market fixture
from test_trades_history import (  # noqa: F401 - the fixture of the worker with the synthetic adapter
    D12,
    D13,
    D14,
    D15,
    _fresh,
    at,
    env,
)

from desk import adapter_services as svcs
from desk import backfill, store, trades_history
from desk.inbox import scan

NAME = "synthetic_services"
CORP_ROW = "TR-CORP-DAY;1;live;DAY;DAYT11:00:00;CORP;P2;bond;SU26238RMFS4;buy;500;;RUB"


def right_window(monkeypatch) -> None:
    """From now on the adapter is one with the right window (``WINDOW_FIXED`` names it)."""
    monkeypatch.setitem(trades_history.WINDOW_FIXED, NAME, (1, 5, 0))


def current(con, day: str, book: str) -> tuple[int, int]:
    """(rows, revisions) of the book's current set of the date."""
    rows = con.execute("SELECT row_count FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s"
                       " AND scope->>'book_id' = %s ORDER BY set_id DESC LIMIT 1", (day, book)).fetchone()
    n = con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s"
                    " AND scope->>'book_id' = %s", (day, book)).fetchone()[0]
    return (rows[0] if rows else None), n


def books_of(con, day: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in store.dataset_rows(con, "trades", datetime.fromisoformat(day).date()).rows:
        out[r["book_id"]] = out.get(r["book_id"], 0) + 1
    return out


def old_cut(env, days) -> None:
    """The cut of the older adapter: the trades of the OFZ book and, shifted, one of the CORP book."""
    for d in days:
        env.put("hist_extra_" + d, CORP_ROW)


def test_version_and_the_table():
    assert trades_history.version_tuple("0.9.16") == (0, 9, 16) and trades_history.version_tuple("v1.2") == (1, 2)
    assert trades_history.version_tuple("0.9.16+b3") == (0, 9, 16)
    assert trades_history.version_tuple("dev") is None and trades_history.version_tuple(None) is None
    assert trades_history.window_fixed("tda", "0.9.16") and trades_history.window_fixed("tda", "0.10.0")
    assert not trades_history.window_fixed("tda", "0.9.15") and not trades_history.window_fixed("tda", "unknown")
    assert not trades_history.window_fixed("other", "9.9.9")   # an adapter not in the table is taken for right: no asking


def test_a_date_received_before_the_sight_is_due_again_and_one_received_after_is_not():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    since = now - timedelta(hours=1)
    old = {"state": "fetched", "code": "exported", "at": (since - timedelta(days=9)).isoformat()}
    new = {**old, "at": (since + timedelta(minutes=1)).isoformat()}
    for state in ("fetched", "no_trades"):
        assert trades_history.due({**old, "state": state}, 50, now, "fp", since=since)   # an old date, far from the newest
        assert not trades_history.due({**new, "state": state}, 50, now, "fp", since=since)
    assert not trades_history.due(old, 50, now, "fp")                                      # no sight: as before
    covered = {**old, "code": "covered"}
    assert trades_history.due(covered, 50, now, "fp", since=since)                         # a covered date too
    assert not trades_history.due({**covered, "at": new["at"]}, 50, now, "fp", since=since)
    for state in ("no_books", "unmapped", "unavailable", "partial"):                       # the rest keeps its own rules
        assert not trades_history.is_stale({"state": state, "at": old["at"]}, since)


def test_the_sight_is_noted_once():
    doc = trades_history.blank()
    doc["marks"] = {D13: {"state": "fetched"}, D14: {"state": "no_trades"}, D15: {"state": "unmapped"}}
    now = at(date(2026, 10, 20), 9)
    assert trades_history.note_window(doc, now, 41, NAME, "1.5.0") is True
    assert doc["window"] == {"since": now.isoformat(), "set_id": 41, "adapter": NAME + " 1.5.0", "total": 2}
    assert trades_history.note_window(doc, now + timedelta(days=1), 99, NAME, "1.5.0") is False
    assert doc["window"]["set_id"] == 41


def test_the_older_dates_are_asked_again_once_and_the_date_is_the_answer_whole(env, monkeypatch):
    env.s.history_set(None, D13, None)
    old_cut(env, (D13, D14))
    env.step(2)
    assert env.asked() == [D13, D14] and env.marks() == {D13: "fetched", D14: "fetched"}
    with store.connect(env.url) as con:
        assert books_of(con, D13) == {"OFZ": 1, "CORP": 1} and current(con, D13, "CORP") == (1, 1)
    backfill.run(env.st, env.plan(D13), store_url=env.url, out=lambda line: None)
    assert env.plan(D13).count("todo") == 0

    right_window(monkeypatch)                      # the adapter is replaced by one with the right window
    for d in (D13, D14):
        (svcs.state_dir(env.st) / ("hist_extra_" + d)).unlink()   # the new cut: OFZ only
    env.step(3)                                    # the 13th and the 14th (stale) and the 15th (new: asked first time)
    assert env.asked()[2:] == [D13, D14, D15]
    assert env.doc["window"]["adapter"] == NAME + " 1.5.0" and env.doc["window"]["total"] == 2
    with store.connect(env.url) as con:
        for d in (D13, D14):
            assert books_of(con, d) == {"OFZ": 1}                  # the shifted trade of CORP is gone from the date
            assert current(con, d, "CORP") == (0, 2)               # an empty revision on top of the older one: nothing deleted
            assert current(con, d, "OFZ")[1] in (1, 2)
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades' AND valid_date = %s"
                           " AND scope->>'book_id' = 'CORP' AND row_count = 1", (D13,)).fetchone()[0] == 1   # the history stays
        assert current(con, D15, "CORP") == (None, 0)             # the new date is not touched
    assert env.marks() == {D13: "fetched", D14: "fetched", D15: "fetched"}
    # the PnL of the dates follows by the hash of the inputs
    assert D13 in [str(d.date) for d in env.plan(D13).todo]
    n = len(env.asked())
    assert env.step(3) == [False, False, False] and len(env.asked()) == n   # nothing is asked twice


def test_a_second_start_of_the_monitor_changes_nothing(env, monkeypatch):
    env.s.history_set(None, D13, None)
    old_cut(env, (D13,))
    env.step()
    right_window(monkeypatch)
    env.step(3)
    doc = env.doc
    n_asked = len(env.asked())
    with store.connect(env.url) as con:
        sets = con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0]
    env.s._window_noted = False                    # a restart: the memory is gone, the file stays
    env.clock["t"] += timedelta(minutes=10)
    env.step(3)
    assert env.doc["window"] == doc["window"] and len(env.asked()) == n_asked
    with store.connect(env.url) as con:
        assert con.execute("SELECT count(*) FROM dataset_set WHERE dataset = 'trades'").fetchone()[0] == sets


def test_the_older_dates_are_asked_again_while_the_fetch_of_past_dates_is_off(env, monkeypatch):
    env.s.history_set(None, D13, None)
    old_cut(env, (D13, D14))
    env.step(2)
    env.s.history_set(False, None, None)           # the owner switched «Догружать сделки прошлых дат» off
    right_window(monkeypatch)
    for d in (D13, D14):
        (svcs.state_dir(env.st) / ("hist_extra_" + d)).unlink()
    env.step(4)
    assert env.asked()[2:] == [D13, D14]           # the dates received are asked again, the 15th (new) is not
    assert not env.doc["enabled"] and env.marks() == {D13: "fetched", D14: "fetched"}
    with store.connect(env.url) as con:
        assert books_of(con, D14) == {"OFZ": 1}
    assert env.step() == [False]
    s = env.s.history()
    assert s["enabled"] is False and s["window"] is None   # nothing left to ask again


def test_a_covered_date_received_before_the_sight_is_asked_again(env, monkeypatch):
    env.s.history_set(None, D13, None)
    env.s._hist_edit(env.st, lambda h: trades_history.apply_mark(h, D13, "fetched", "covered", env.clock["t"],
                                                                 fingerprint="fp"))
    env.step()
    assert env.asked() == [D14]                    # the covered date waits for nothing: no sight of the right window yet
    right_window(monkeypatch)
    env.step(2)
    assert D13 in env.asked() and env.doc["marks"][D13]["code"] != "covered"


def test_the_summary_says_how_many_dates_are_left_and_the_rest_of_the_dates_wait_for_their_turn(env, monkeypatch):
    env.s.history_set(None, D13, None)
    env.step(3)
    right_window(monkeypatch)
    env.s._note_window(env.st, env.s.availability()[1])
    assert env.s.history()["window"] == {"left": 3, "total": 3}
    env.step(2)
    assert env.s.history()["window"] == {"left": 1, "total": 3}
    env.step()
    assert env.s.history()["window"] is None


def test_a_book_without_records_in_a_cut_by_portfolios_that_proves_nothing_is_not_emptied(env, monkeypatch, tmp_path):
    """No date of the service has brought records (the only mark is «без сделок»): a cut by the portfolios that answers
    «no records» proves nothing about the books, the older sets stay (the date is «не сопоставлено» as before)."""
    head = ("trade_id;trade_version;status;trade_date;trade_ts;book_id;position_id;asset_class;source_instrument_id;"
            "side;quantity;amount_ccy;ccy")
    f = tmp_path / "trades_20261013_1845.csv"
    f.write_text(head + "\n" + f"TR-1;1;live;{D13};{D13}T10:30:00;OFZ;P1;bond;SU26238RMFS4;buy;5;;RUB\n"
                 + f"TR-2;1;live;{D13};{D13}T10:40:00;CORP;P2;bond;SU26238RMFS4;buy;5;;RUB\n", encoding="utf-8")
    with store.connect(env.url) as con:
        assert scan.ingest_feed(con, env.st, f, source="trades_csv", handle=scan.adapter(env.st)).files == {"accepted": 1}
    env.s.history_set(None, D13, None)
    env.s._hist_edit(env.st, lambda h: trades_history.apply_mark(h, D13, "no_trades", "no_records", env.clock["t"],
                                                                 fingerprint="fp"))
    env.clock["t"] += timedelta(minutes=5)
    right_window(monkeypatch)
    env.put("hist_mode_" + D13, "no_records_filtered")
    env.step()
    assert env.asked() == [D13] and env.marks()[D13] == "unmapped"
    with store.connect(env.url) as con:
        assert books_of(con, D13) == {"OFZ": 1, "CORP": 1}


def test_a_no_records_answer_proven_empties_the_date(env, monkeypatch):
    env.s.history_set(None, D13, None)
    old_cut(env, (D13, D14))
    env.step(2)
    right_window(monkeypatch)
    env.put("hist_mode_" + D13, "no_records")             # whole: the date has no trades in the right window
    env.step(2)
    with store.connect(env.url) as con:
        assert books_of(con, D13) == {}                   # the shifted trades of the older window are gone
        assert current(con, D13, "OFZ") == (0, 2)
    assert env.marks()[D13] == "no_trades"


def test_the_files_waiting_for_the_cycle_lock_replace_the_date_whole_when_they_are_taken_in(env, monkeypatch):
    env.s.history_set(None, D13, None)
    old_cut(env, (D13,))
    env.step()
    right_window(monkeypatch)
    (svcs.state_dir(env.st) / ("hist_extra_" + D13)).unlink()
    real = svcs.AdapterServices._try_cycle_lock
    busy = {"n": 1}

    def maybe(st):
        if busy["n"]:
            busy["n"] -= 1
            return None
        return real(st)

    monkeypatch.setattr(svcs.AdapterServices, "_try_cycle_lock", staticmethod(maybe))
    assert env.step()[0] is True and env.doc["pending"] and env.doc["pending"]["date"] == D13
    env.clock["t"] += timedelta(seconds=10)
    assert env.s._history_step() is True and env.doc["pending"] is None
    with store.connect(env.url) as con:
        assert books_of(con, D13) == {"OFZ": 1}


def test_todays_feed_replaces_the_set_of_the_day_whole_too(env, monkeypatch):
    """The feed of the day (the helper answers the 15th without a date): the morning cuts of the older adapter are not
    left in a book that has no trade in the new cut."""
    old_cut(env, (D15,))
    run = run_feed(env.s, env.st, "tradeshist", env.clock)
    assert run["state"] == "complete"
    with store.connect(env.url) as con:
        assert books_of(con, D15) == {"OFZ": 1, "CORP": 1}
    right_window(monkeypatch)
    (svcs.state_dir(env.st) / ("hist_extra_" + D15)).unlink()
    env.clock["t"] += timedelta(minutes=5)
    run_feed(env.s, env.st, "tradeshist", env.clock)
    with store.connect(env.url) as con:
        assert books_of(con, D15) == {"OFZ": 1} and current(con, D15, "CORP") == (0, 2)
    run_feed(env.s, env.st, "tradeshist", env.clock)      # once more: no new revision, the unit is empty already
    with store.connect(env.url) as con:
        assert current(con, D15, "CORP") == (0, 2)
