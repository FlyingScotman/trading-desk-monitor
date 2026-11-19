"""TDM#711: the day's trades in the PnL of their own date, and the reason when one is not in.

(a) a trade that is in the store before the run of its date gives the PnL of that date; (b) a trade that comes after the
run (the final close of the date is made, the date is a past one) makes the date again by the inputs hash and gives the
PnL; (c) a trade the book cannot take (a book without positions, a bond the book does not hold and the trade does not
name) changes no input hash, so no run follows it: the screens say the reason (``other_book``, ``no_instrument``)
instead of «ждёт прогона» for ever. Synthetic data only (the fixtures of the intraday tests)."""
from __future__ import annotations

from datetime import date

import pytest
import test_slice_s3
from test_autopilot import steps
from test_intraday import FRI, MON, at, publish
from test_pnl_new_trades import HEAD, Day, lineage, trade_line

from desk import backfill, dataflow, store, trades
from desk.inbox import scan

server, stub = test_slice_s3.server, test_slice_s3.stub
TUE = date(2026, 10, 20)


@pytest.fixture
def day(stub, tmp_path, store_url):
    return Day(stub, tmp_path, store_url)


def states(day_, on=FRI):
    with store.connect(day_.url) as con:
        return {r["trade_id"]: r["intraday"] for r in trades.view(con, on)["rows"]}


def close_final(day_):
    publish(day_.hub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)
    day_.cycle(19, 50)
    publish(day_.hub, FRI, "eod_final", "2026-10-19T05:00:00Z", 3)
    day_.clock["t"] = at(MON, 8, 30)
    by = steps(day_.ap.run_cycle())
    assert by["eod"][0] == "ok", by["eod"]
    return day_.latest_id("EOD")


def late_file(day_, *lines, name="trades_20261016_2100.csv"):
    (day_.tmp / "in" / name).write_text(HEAD + "".join(lines), encoding="utf-8")


def test_a_trade_in_the_store_before_the_run_gives_the_pnl_of_its_date(day):
    """(a): the file is there when the date's close runs: the close holds the trade and its PnL."""
    late_file(day, trade_line("S1", "sell", 1000, 62.0, 5_000.0), name="trades_20261016_1100.csv")
    publish(day.hub, FRI, "eod_preliminary", "2026-10-16T16:45:00Z", 3)
    by = day.cycle(19, 50)
    assert by["intraday"][0] == "ok", by["intraday"]
    rid = day.latest_id("EOD")
    r = day.row(rid=rid)
    assert r["q"] == 11000 and r["pnl"]["new_trades"] != 0
    assert r["pnl"]["extra"]["total"] != day.base["pnl"]["extra"]["total"]
    assert states(day) == {"S1": "applied"}
    assert lineage(day.url, rid)["applied"] == 1


def test_a_trade_after_the_final_close_makes_the_date_again_and_gives_the_pnl(day):
    """(b): the date is done (final close), the trade comes later: the plan names the date, the next cycle makes it.
    FRI is the latest date with positions, so ``_eod`` of the autopilot makes it; a date older than the autopilot's window
    is made by the backfill on the same hash (``backfill.plan`` says the same ``INPUTS_WHY``), see
    test_trades_history.py::test_trades_are_taken_in_without_a_run_and_the_backfill_makes_the_dates_again."""
    final = close_final(day)
    assert day.row(rid=final)["pnl"]["new_trades"] == 0.0
    late_file(day, trade_line("S1", "sell", 1000, 62.0, 5_000.0))
    with store.connect(day.url) as con:
        scan.scan(con, day.s)
        assert states(day) == {"S1": "pending"}   # it will be taken in: a wait, not a reason
        plan = backfill.plan(con, day.s, FRI, FRI)
    [d] = plan.days
    assert d.status == "todo" and d.why == backfill.INPUTS_WHY, d
    day.clock["t"] = at(TUE, 3, 0)
    by = steps(day.ap.run_cycle())
    assert by["eod"][0] == "ok" and "private inputs changed" in by["eod"][1], by["eod"]
    again = day.latest_id("EOD")
    assert again > final
    r = day.row(rid=again)
    assert r["q"] == 11000 and r["pnl"]["new_trades"] != 0
    assert r["pnl"]["extra"]["trades"][0]["trade_id"] == "S1"
    assert states(day) == {"S1": "applied"}
    day.clock["t"] = at(TUE, 3, 10)
    assert steps(day.ap.run_cycle())["eod"][1].startswith("up to date") and day.latest_id("EOD") == again


def test_a_trade_the_book_cannot_take_says_why_and_no_run_waits_for_it(day):
    """(c): after the run, a trade of a book without positions and one of a bond nobody holds that names no instrument:
    their reasons, not «ждёт прогона» for ever; the plan has nothing to make again."""
    rid0 = day.latest_id()
    other = trade_line("X1", "sell", 1000, 62.0, 5_000.0).replace(";B1;", ";B9;")
    nobody = trade_line("X2", "buy", 1000, 62.0, 5_000.0, pid="B1|NEW", instr="SEC-99999", iid="")
    by = day.drop(other, nobody)
    assert by["intraday"][1].startswith("up to date") and day.latest_id() == rid0   # no new input: no run
    assert states(day) == {"X1": "other_book", "X2": "no_instrument"}
    with store.connect(day.url) as con:
        b = trades.brief(con, FRI)
        v = trades.view(con, FRI)
        flow = dataflow.build(con, day.s, FRI)
    assert b["problems"] == 2 and {s["state"] for s in b["states"]} == {"other_book", "no_instrument"}
    assert v["counts"]["problems"] == 2 and v["counts"]["applied"] == 0
    blocks = {x["id"]: x for x in flow["blocks"]}
    assert blocks["intraday"]["status"] == "ok", blocks["intraday"]   # not «пересчёт: 2 сделок пришли после прогона»
    edge = next(e for e in flow["edges"] if (e["from"], e["to"]) == ("trades", "intraday"))
    assert "не вошли в позиции дня сделок: 2" in (edge.get("why") or ""), edge


def test_a_trade_that_will_be_taken_waits_and_one_that_will_not_has_its_reason(day):
    """The two come together after the run: the first is ``pending`` until the cycle makes the run, the second has its reason
    at once; the run then says both (the lineage)."""
    ok = trade_line("S1", "sell", 1000, 62.0, 5_000.0)
    other = trade_line("X1", "sell", 1000, 62.0, 5_000.0).replace(";B1;", ";B9;")
    late_file(day, ok, other, name="trades_20261016_1100.csv")
    with store.connect(day.url) as con:
        scan.scan(con, day.s)
    assert states(day) == {"S1": "pending", "X1": "other_book"}
    by = day.cycle(10, 20)
    assert by["intraday"][0] == "ok" and "private inputs changed" in by["intraday"][1], by["intraday"]
    assert states(day) == {"S1": "applied", "X1": "other_book"}
    lin = lineage(day.url, day.latest_id())
    assert lin["applied"] == 1 and lin["skipped_ids"] == {"X1": "other_book"}
    assert day.row()["pnl"]["new_trades"] != 0


def test_without_a_run_the_trade_waits_and_the_setting_off_says_off(day):
    """No run of the date: ``pending`` (nothing to say yet); the lineage and the setting still decide the rest."""
    with store.connect(day.url) as con:
        run = trades._intraday_run(con, FRI)
    row = {"status": "live", "asset_class": "bond", "trade_date": FRI.isoformat(), "quantity": "5", "trade_id": "Z"}
    assert trades._intraday_state(row, FRI, None, True) == "pending"
    assert trades._intraday_state(row, FRI, run, False) == "off"
    ahead = {"skipped_ids": {"Z": "no_instrument"}}
    assert trades._intraday_state(row, FRI, run, True, ahead) == "no_instrument"
    assert trades._intraday_state(row, FRI, None, True, ahead) == "pending"


def after_run_x1(day_):
    """A trade of a book without positions that came after the run (``other_book`` when asked, else ``pending``)."""
    late_file(day_, trade_line("X1", "sell", 1000, 62.0, 5_000.0).replace(";B1;", ";B9;"),
              name="trades_20261016_1100.csv")
    with store.connect(day_.url) as con:
        scan.scan(con, day_.s)


def test_a_failing_question_does_not_take_the_screens_down(day, monkeypatch):
    """The reason is a hint: when ``apply_intraday`` fails the trades screen and the summary answer, the trade waits."""
    from fastapi.testclient import TestClient

    from desk.api import create_app

    after_run_x1(day)

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(trades, "apply_intraday", boom)
    c = TestClient(create_app(day.url))
    r = c.get("/api/v1/trades", params={"date": "2026-10-16"})
    assert r.status_code == 200, r.text
    assert {x["trade_id"]: x["intraday"] for x in r.json()["rows"]} == {"X1": "pending"}
    r = c.get("/api/v1/trades/summary", params={"date": "2026-10-16"})
    assert r.status_code == 200, r.text
    assert [s["state"] for s in r.json()["states"]] == ["pending"]


def test_the_reason_is_asked_once_for_the_same_inputs(day, monkeypatch):
    """A screen asked again (view, summary, a position's panel) does not read the positions and fold the trades again, until
    the inputs of the date change."""
    after_run_x1(day)
    calls = []
    real = trades.apply_intraday

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(trades, "apply_intraday", counted)
    for _ in range(3):
        assert states(day) == {"X1": "other_book"}
    with store.connect(day.url) as con:
        assert trades.brief(con, FRI)["problems"] == 1
    assert len(calls) == 1
    late_file(day, trade_line("X1", "sell", 1000, 62.0, 5_000.0).replace(";B1;", ";B9;"),
              trade_line("X2", "sell", 10, 62.0, 5_000.0).replace(";B1;", ";B9;"), name="trades_20261016_1105.csv")
    with store.connect(day.url) as con:
        scan.scan(con, day.s)
    assert states(day) == {"X1": "other_book", "X2": "other_book"}
    assert len(calls) == 2   # a new trades set: new inputs, asked again


def test_a_trades_screen_of_a_settled_date_asks_nothing(day, monkeypatch):
    """Every trade settled by the run's lineage (applied): no question at all."""
    late_file(day, trade_line("S1", "sell", 1000, 62.0, 5_000.0), name="trades_20261016_1100.csv")
    day.cycle(10, 20)
    monkeypatch.setattr(trades, "apply_intraday", lambda *a, **k: pytest.fail("asked"))
    assert states(day) == {"S1": "applied"}


def test_the_positions_panel_says_the_reason_too(day):
    """The panel of a position (``/api/v1/positions/provenance``) takes a trade of the run's sets that the lineage does not
    explain (a run whose lineage lacks the reasons) as the trades screen does: its reason, not a wait."""
    # a trade given by its face for a position held in bonds: units_mismatch
    late_file(day, trade_line("F1", "sell", "", 62.0, 5_000.0, face="1000000"),
              trade_line("S1", "sell", 1000, 62.0, 5_000.0, hhmm="10:40"), name="trades_20261016_1100.csv")
    day.cycle(10, 20)   # S1 is taken in (the run reads the set), F1 is left out
    rid = day.latest_id()
    assert lineage(day.url, rid)["skipped_ids"] == {"F1": "units_mismatch"}
    import copy

    import psycopg
    from psycopg.rows import dict_row

    from desk.api import provenance

    with psycopg.connect(day.url, row_factory=dict_row, autocommit=True) as con:   # the API's connection: dict rows
        run = con.execute("SELECT * FROM run WHERE run_id = %s", (rid,)).fetchone()
        run = {**run, "lineage": copy.deepcopy(run["lineage"])}
        run["lineage"]["intraday_trades"]["skipped_ids"] = {}   # a run is never changed: the lineage as an older one has it
        out = provenance.provenance(con, run, "B1|SEC-26238")
        items = {i["trade_id"]: i for i in next(s for s in out["sources"] if s["key"] == "trades")["items"]}
        assert (items["F1"]["state"], items["S1"]["state"]) == ("units_mismatch", "applied")
        assert items["F1"]["state_text"] == trades.STATE_TEXT["units_mismatch"]
        assert con.row_factory is dict_row   # the connection is given back as it was
