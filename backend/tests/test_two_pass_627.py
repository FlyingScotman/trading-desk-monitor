"""TDM#627, the history of runs in two passes: the first makes every date's run without the second derivatives (the
Hessian: ``gamma`` and the bonds' ``convexity``), the second makes the dates again with them, the newest first. The
second pass's value, first derivatives and PnL are the first pass's to the last bit, the two passes together give the
runs of one full pass, both resume where they stopped, and the runs, the risk payload and the scenario result say that
the second derivatives are not computed yet (not zero). Synthetic data only (the fixture's market shifted to five
business days)."""

from __future__ import annotations

import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row
from test_market_history import FIX, STATIC, shifted, write
from test_r23_backfill import DATES, load, settings

from desk import backfill, dayrun, store
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.marketdata.bundle import SnapshotDir
from desk.publish.desk_run import SECOND_ORDER_OFF
from desk.scenario import jobs, taylor
from desk.store import MIGRATIONS, migrate


@pytest.fixture
def cache(tmp_path) -> Path:
    """The fixture's market shifted to the five business dates of ``DATES`` (as ``test_r23_backfill``'s)."""
    c = tmp_path / "cache"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(DATES):
        write(c, shifted(d, len(DATES) - i, light=False))
    return c


D0 = date(2026, 10, 1)
D1 = date(2026, 10, 15)
RUN_DATES = DATES[1:]  # 12.10 - 15.10: the first report is of the 12th
NEWEST_FIRST = RUN_DATES[::-1]


def plan(store_url: str, s) -> backfill.Plan:
    with store.connect(store_url) as con:
        return backfill.plan(con, s, D0, D1)


def runs(store_url: str) -> dict[str, dict[str, Any]]:
    """Per date its default run: the id, whether it has the second derivatives, its lineage, coverage, totals, log, core
    result hashes and positions (position_id -> columns)."""
    out = {}
    with store.connect(store_url) as con:
        cur = con.cursor(row_factory=dict_row)
        for d, r in store.default_runs(con, D0).items():
            rid = r["run_id"]
            rows = cur.execute("SELECT position_id, pv_rub, dv01_rub, cs01_rub, fact, pnl, no_pnl, quality, reason, quantity"
                               " FROM run_position WHERE run_id = %s ORDER BY ord", (rid,)).fetchall()
            hdr = cur.execute("SELECT lineage, coverage, totals, prev_date FROM run WHERE run_id = %s", (rid,)).fetchone()
            out[d.isoformat()] = {
                "run_id": rid, "second_order": r["second_order"], **hdr, "rows": {x["position_id"]: x for x in rows},
                "log": [m for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s ORDER BY seq", (rid,))],
                "sha": [x for (x,) in con.execute("SELECT result_sha256 FROM run_core_result WHERE run_id = %s ORDER BY 1",
                                                  (rid,))]}
    return out


def first_pass(tmp_path, cache, store_url, **kw):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url, **kw)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    return s, res


def second_pass(s, store_url: str, **kw) -> dict[str, Any]:
    return backfill.run_second(s, plan(store_url, s), store_url=store_url, out=lambda line: None, **kw)


def first_order(fact: dict[str, Any]) -> dict[str, Any]:
    """A fact without what the Hessian brings: ``gamma``, the bonds' ``convexity``, and core's ``calculation_id`` (it
    hashes the call, whose policy says ``include_second_order``)."""
    return {**{k: v for k, v in fact.items() if k not in ("gamma", "convexity")},
            "extra": {k: v for k, v in fact["extra"].items() if k != "calculation_id"}}


def has_gamma(fact: dict[str, Any]) -> bool:
    return bool(fact["gamma"])


def test_the_first_pass_makes_runs_without_the_second_derivatives_and_says_so(tmp_path, cache, store_url):
    s, res = first_pass(tmp_path, cache, store_url)
    assert res["done"] == RUN_DATES and res["complete"] and not res["failed"]
    r = runs(store_url)
    assert sorted(r) == RUN_DATES
    for run in r.values():
        assert run["second_order"] is False and run["lineage"]["second_order"] is False
        assert SECOND_ORDER_OFF in run["log"]
        bond = run["rows"]["P1"]["fact"]
        assert bond["gamma"] == [] and bond["convexity"] is None  # not computed, and not shown as a zero
        assert bond["mv_rub"] and bond["dv01"] and bond["risk"]  # the value and the first derivatives are there
        assert not any(has_gamma(x["fact"]) for x in run["rows"].values() if x["fact"])
    p = plan(store_url, s)
    assert [(d.date, d.status, d.second) for d in p.days if d.status != "no_positions"] == [(d, "done", "todo") for d in RUN_DATES]
    assert [d.date for d in p.second_todo] == NEWEST_FIRST and not p.todo
    assert p.summary()["second"] == {"done": 0, "todo": 4, "failed": 0, "next": NEWEST_FIRST}


def test_the_second_pass_adds_the_gamma_and_changes_nothing_else_to_the_last_bit(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    before = runs(store_url)
    res = second_pass(s, store_url)
    assert res["done"] == NEWEST_FIRST and not res["failed"] and not res["stopped"] and res["left"] == 0
    after = runs(store_url)
    assert sorted(after) == RUN_DATES
    for d in RUN_DATES:
        a, b = before[d], after[d]
        assert b["run_id"] > a["run_id"] and b["second_order"] is True and b["lineage"]["second_order"] is True
        assert SECOND_ORDER_OFF not in b["log"] and set(a["rows"]) == set(b["rows"])
        for pid, x in a["rows"].items():
            y = b["rows"][pid]
            for col in ("pv_rub", "dv01_rub", "cs01_rub", "pnl", "no_pnl", "quality", "reason", "quantity"):
                assert x[col] == y[col], (d, pid, col)  # ==, not approx: the same floats
            if x["fact"]:
                assert first_order(x["fact"]) == first_order(y["fact"]), (d, pid)
        assert a["coverage"] == b["coverage"] and a["totals"] == b["totals"] and a["prev_date"] == b["prev_date"]
        assert has_gamma(b["rows"]["P1"]["fact"]) and b["rows"]["P1"]["fact"]["convexity"]  # the Hessian is there now
        assert a["sha"] != b["sha"]  # core's result of the call with the Hessian is another document
    p = plan(store_url, s)
    assert not p.todo and not p.second_todo and p.summary()["second"] == {"done": 4, "todo": 0, "failed": 0, "next": []}
    with store.connect(store_url) as con:  # the first pass's runs are kept, superseded (a run is never changed)
        assert con.execute("SELECT count(*) FROM run").fetchone()[0] == 8
        assert con.execute("SELECT count(*) FROM run WHERE supersedes_run_id IS NOT NULL").fetchone()[0] == 4


def test_two_passes_give_the_runs_of_one_full_pass(tmp_path, cache, store_url, pg_server):
    s, _ = first_pass(tmp_path, cache, store_url)
    second_pass(s, store_url)
    name = "desk_" + uuid.uuid4().hex[:12]
    pg_server.psql(f"CREATE DATABASE {name};")
    full_url = pg_server.get_uri(name)
    assert migrate(full_url) == [f.stem for f in sorted(MIGRATIONS.glob("*.sql"))]
    load(full_url, tmp_path, "2026-10-12", 10)
    s2 = settings(tmp_path, cache, full_url)
    for d in RUN_DATES:  # one full pass, the oldest first: what the backfill did before
        assert dayrun.eod_run(s2, d, snapshots=cache, store_url=full_url, report_dir=None, out=lambda x: None,
                              read_model=False)
    two, one = runs(store_url), runs(full_url)
    assert sorted(two) == sorted(one) == RUN_DATES
    for d in RUN_DATES:
        a, b = two[d], one[d]
        assert a["second_order"] is b["second_order"] is True
        assert a["rows"] == b["rows"] and a["sha"] == b["sha"] and a["totals"] == b["totals"] and a["coverage"] == b["coverage"]
        assert [x for x in a["log"] if not x.startswith("lineage ")] == [x for x in b["log"] if not x.startswith("lineage ")]
        assert {k: v for k, v in a["lineage"].items() if k != "positions"} == {
            k: v for k, v in b["lineage"].items() if k != "positions"}  # (``positions.known_at`` is the read's time)


def test_the_first_pass_is_cut_by_the_budget_and_resumes_and_the_second_follows_only_after_it(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None, budget_s=0)
    assert res["done"] == RUN_DATES[:1] and res["complete"] is False and res["left"] == 3  # at least one date is tried
    res = backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    assert res["done"] == RUN_DATES[1:] and res["complete"] is True
    assert [d.date for d in plan(store_url, s).second_todo] == NEWEST_FIRST


def test_the_second_pass_is_cut_by_the_budget_newest_first_and_resumes(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    seen: list[str] = []
    for want in NEWEST_FIRST[:-1]:
        res = second_pass(s, store_url, budget_s=0)  # at least one date a run
        assert res["done"] == [want] and res["left"] == len(NEWEST_FIRST) - len(seen) - 1
        seen.append(want)
        p = plan(store_url, s)
        assert [d.date for d in p.second_todo] == [d for d in NEWEST_FIRST if d not in seen]
        assert p.count_second("done") == len(seen)
    assert second_pass(s, store_url)["done"] == NEWEST_FIRST[-1:]
    assert not plan(store_url, s).second_todo
    assert second_pass(s, store_url) == {"done": [], "failed": [], "skipped": [], "left": 0, "stopped": None,
                                         "seconds": pytest.approx(0, abs=1)}  # nothing left: nothing made again
    assert {r["second_order"] for r in runs(store_url).values()} == {True}


def test_a_second_pass_date_that_fails_does_not_hold_the_others_and_is_left_after_two_runs(tmp_path, cache, store_url,
                                                                                         monkeypatch):
    s, _ = first_pass(tmp_path, cache, store_url)
    real, tried = dayrun.eod_run, []

    def flaky(settings, d, **kw):
        tried.append(d)
        if d == "2026-10-14":
            raise ValueError("bad terms")
        return real(settings, d, **kw)

    monkeypatch.setattr(dayrun, "eod_run", flaky)
    res = second_pass(s, store_url)
    assert res["done"] == ["2026-10-15", "2026-10-13", "2026-10-12"] and res["failed"] == [("2026-10-14", "ValueError: bad terms")]
    assert plan(store_url, s).count_second("todo") == 1  # tried once: again next time
    res = second_pass(s, store_url)
    assert res["done"] == [] and [d for d, _ in res["failed"]] == ["2026-10-14"] and tried.count("2026-10-14") == 2
    p = plan(store_url, s)
    assert not p.second_todo and p.count_second("failed") == 1  # given up on that run
    assert second_pass(s, store_url)["done"] == [] and tried.count("2026-10-14") == 2
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["pass"] == "done" and st["second"]["failed"] == 1
    assert st["second"]["problem_dates"] == [{"date": "2026-10-14", "error": "ValueError: bad terms", "attempts": 2}]
    # a new run of the date (its inputs changed: the first pass again) forgets the give-up
    load(store_url, tmp_path, "2026-10-14", 12)
    assert [d.date for d in plan(store_url, s).todo] == ["2026-10-14", "2026-10-15"]


def test_a_store_or_network_error_stops_the_second_pass_without_marking_the_date(tmp_path, cache, store_url, monkeypatch):
    s, _ = first_pass(tmp_path, cache, store_url)

    def down(settings, d, **kw):
        raise psycopg.OperationalError("connection lost")

    monkeypatch.setattr(dayrun, "eod_run", down)
    res = second_pass(s, store_url)
    assert res["done"] == [] and res["failed"] == [] and res["stopped"].startswith("OperationalError")
    assert res["left"] == 4 and plan(store_url, s).count_second("failed") == 0
    monkeypatch.undo()
    assert second_pass(s, store_url)["done"] == NEWEST_FIRST


def test_a_date_whose_bundle_is_not_cached_is_left_to_the_next_time(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    p = plan(store_url, s)
    p.days[-1].cached = False  # the plan says so (a pruned cache, the hub only)
    res = backfill.run_second(s, p, store_url=store_url, out=lambda line: None)
    assert res["skipped"] == ["2026-10-15"] and res["done"] == NEWEST_FIRST[1:]
    assert [d.date for d in plan(store_url, s).second_todo] == ["2026-10-15"]


def test_status_says_which_pass_the_history_is_in(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["pass"] == "first" and st["todo"] == 4 and st["second"]["total"] == 0
    backfill.run(s, plan(store_url, s), store_url=store_url, out=lambda line: None)
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["pass"] == "second" and st["todo"] == 0 and st["done"] == 4
    assert st["second"] == {"done": 0, "todo": 4, "failed": 0, "next": NEWEST_FIRST, "total": 4, "problem_dates": [],
                            "eta_s": None}
    second_pass(s, store_url, budget_s=0)
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["second"]["done"] == 1 and st["second"]["todo"] == 3 and st["second"]["eta_s"] is not None
    assert "second derivatives (gamma): 3 dates to make again, 1 have them" in backfill.text(plan(store_url, s))
    second_pass(s, store_url)
    with store.connect(store_url) as con:
        st = backfill.status(con, s, D1)
    assert st["pass"] == "done" and st["second"]["done"] == 4 and st["second"]["next"] == []


def test_the_autopilot_runs_the_second_pass_only_after_the_first_has_gone_through_every_date(tmp_path, cache, store_url,
                                                                                            monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)  # the eod step makes the latest date, the backfill the dates before it
    s = settings(tmp_path, cache, store_url, backfill_budget_s=0.001, backfill_from="2026-10-01")
    real, calls = dayrun.eod_run, []

    def spy(settings, d, **kw):
        calls.append((d, bool(kw.get("no_gamma"))))
        return real(settings, d, **kw)

    monkeypatch.setattr(dayrun, "eod_run", spy)
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    details = []
    for _ in range(10):  # one date a cycle on that budget
        st = ap.run_cycle(hub=False)
        details.append(next(x for x in st["steps"] if x["name"] == "backfill")["detail"])
        if details[-1].startswith("up to date"):
            break
    assert calls == [("2026-10-15", False)] + [(d, True) for d in RUN_DATES[:3]] + [(d, False) for d in NEWEST_FIRST[1:]]
    assert "ran 1 dates (2026-10-12..2026-10-12)" in details[0]
    assert not any("second derivatives of" in x for x in details[:3])  # the first pass is not over yet
    assert any("second derivatives of 1 dates (2026-10-14..2026-10-14)" in x for x in details[3:])
    assert details[-1].startswith("up to date (2026-10-01..2026-11-30: 4 of 4 dates"), details[-1]
    assert {r["second_order"] for r in runs(store_url).values()} == {True}


def test_a_cycle_with_budget_left_runs_both_passes(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_budget_s=600, backfill_from="2026-10-01")
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    st = ap.run_cycle(hub=False)
    b = next(x for x in st["steps"] if x["name"] == "backfill")
    assert b["status"] == "ok" and "ran 3 dates (2026-10-12..2026-10-14)" in b["detail"], b["detail"]
    assert "second derivatives of 3 dates (2026-10-14..2026-10-12)" in b["detail"]
    assert "гамма 3 дат" in st["message"], st["message"]
    assert {r["second_order"] for r in runs(store_url).values()} == {True}


def test_the_cli_runs_both_passes(tmp_path, cache, store_url, monkeypatch, capsys):
    from desk import cli

    load(store_url, tmp_path, "2026-10-13", 10)
    for k, v in {"DESK_HOME": str(tmp_path / "home"), "DESK_SNAPSHOT_CACHE": str(cache), "DESK_STORE_URL": store_url,
                 "DESK_HUB_URL": ""}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["backfill", "--from", "2026-10-01", "--to", "2026-10-15", "--no-inbox"]) == 0
    out = capsys.readouterr().out
    assert "backfill: 3 dates run, 0 failed" in out and "[second 3/3] 2026-10-13 done" in out
    assert "backfill: second derivatives of 3 dates, 0 failed in all" in out
    assert {r["second_order"] for r in runs(store_url).values()} == {True}
    assert cli.main(["backfill", "--from", "2026-10-01", "--to", "2026-10-15", "--no-inbox", "--dry-run"]) == 0
    assert "second derivatives" not in capsys.readouterr().out  # nothing left in either pass


def test_the_daily_and_the_intraday_runs_stay_full(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    assert dayrun.eod_run(s, "2026-10-15", snapshots=cache, store_url=store_url, report_dir=None, out=lambda x: None)
    assert dayrun.eod_run(s, "2026-10-14", snapshots=cache, store_url=store_url, report_dir=None, out=lambda x: None,
                          run_type="TEST", books=["OFZ"])
    with store.connect(store_url) as con:
        lin = [x for (x,) in con.execute("SELECT lineage->'second_order' FROM run ORDER BY run_id")]
    assert lin == [True, True]
    assert has_gamma(runs(store_url)["2026-10-15"]["rows"]["P1"]["fact"])


def test_a_run_valued_by_hand_without_the_gamma_is_made_whole_by_the_backfill(tmp_path, cache, store_url):
    """``desk eod --no-gamma`` has always been possible; its run is now also «not computed yet», and the history's second
    pass completes it."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    assert dayrun.eod_run(s, "2026-10-12", snapshots=cache, store_url=store_url, report_dir=None, out=lambda x: None,
                          no_gamma=True)
    assert runs(store_url)["2026-10-12"]["second_order"] is False
    assert [d.date for d in plan(store_url, s).second_todo] == ["2026-10-12"]
    assert second_pass(s, store_url)["done"] == ["2026-10-12"]
    assert runs(store_url)["2026-10-12"]["second_order"] is True


# ---- what the screens read

def test_the_run_header_the_risk_payload_and_the_scenario_result_say_the_gamma_is_not_there_yet(tmp_path, cache, store_url):
    s, _ = first_pass(tmp_path, cache, store_url)
    client = TestClient(create_app(store_url))
    rid = runs(store_url)["2026-10-15"]["run_id"]
    head = client.get(f"/api/v1/runs/{rid}").json()
    assert head["second_order"] is False and head["lineage"]["second_order"] is False
    assert client.get("/api/v1/runs").json()["runs"][0]["second_order"] is False
    risk = client.get(f"/api/v1/runs/{rid}/risk").json()
    assert risk["second_order"] is False and all(p["gamma"] == [] for p in risk["positions"])
    with store.connect(store_url) as con:
        job, _ = jobs.submit(con, rid, {"ofzSpread": 10})
        assert jobs.run_once(con, SnapshotDir(cache)) == job["job_id"]
    res = client.get(f"/api/v1/scenario-jobs/{job['job_id']}").json()["result"]
    assert res["taylor_second_order"] is False
    assert all(p["taylor"]["convexity"] == 0.0 for p in res["positions"])  # linear: the estimate has no second term
    # after the second pass the new default run has them
    second_pass(s, store_url)
    rid2 = runs(store_url)["2026-10-15"]["run_id"]
    assert rid2 > rid
    assert client.get(f"/api/v1/runs/{rid2}").json()["second_order"] is True
    risk2 = client.get(f"/api/v1/runs/{rid2}/risk").json()
    assert risk2["second_order"] is True and any(p["gamma"] for p in risk2["positions"])
    with store.connect(store_url) as con:
        job2, _ = jobs.submit(con, rid2, {"ofzSpread": 10})
        assert jobs.run_once(con, SnapshotDir(cache)) == job2["job_id"]
    res2 = client.get(f"/api/v1/scenario-jobs/{job2['job_id']}").json()["result"]
    assert res2.get("taylor_second_order", True) is True  # (unset when True: the route leaves unset fields out)


def test_the_risk_payload_marks_only_a_run_without_the_second_derivatives():
    rows = [{"position_id": "P", "book_id": "B", "secid": "S", "quality": "exact", "pv_rub": 1.0,
             "fact": {"risk": {"RUB OFZ": {"1Y": 2.0}}, "cs01": 0.0, "gamma": []}}]
    base = {"run_id": 1, "business_date": date(2026, 10, 15)}
    assert "second_order" not in taylor.risk_payload(base, rows, {})  # a run of before the flag: unchanged bytes
    assert "second_order" not in taylor.risk_payload({**base, "lineage": {"second_order": True}}, rows, {})
    assert taylor.risk_payload({**base, "lineage": {"second_order": False}}, rows, {})["second_order"] is False
    assert taylor.risk_payload({**base, "second_order": False}, rows, {})["second_order"] is False
    assert taylor.has_second_order({**base, "second_order": None}) and taylor.has_second_order({"lineage": {}})
