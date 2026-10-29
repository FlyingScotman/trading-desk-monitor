"""R23, the history of runs: ``desk backfill`` and the autopilot's backfill step compute the default run of every past
business date with positions (carried forward from the last report), oldest first, skipping the dates whose inputs
and logic are unchanged, reusing each date's prepared market for the next, a failed date not retried on the same
inputs, within a time budget per cycle; the older reports of the inbox read once from the start date. The fixture's
market shifted to five business days. Synthetic data only."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from test_inbox_r22 import CONFIG, GOOD, put
from test_market_history import FIX, OFZ, STATIC, USD_FACE, shifted, write

from desk import backfill, cli, store
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.settings import Settings, check_settings_doc

DATES = ["2026-10-09", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15"]


@pytest.fixture
def cache(tmp_path) -> Path:
    import shutil

    c = tmp_path / "cache"
    c.mkdir()
    shutil.copy(FIX / f"{STATIC}.json.gz", c)
    for i, d in enumerate(DATES):
        write(c, shifted(d, len(DATES) - i, light=False))
    return c


def settings(tmp_path: Path, cache: Path, store_url: str, **kw) -> Settings:
    return Settings(home=tmp_path / "home", snapshot_cache=cache, store_url=store_url, **kw)


def load(store_url: str, tmp_path: Path, d: str, qty: int) -> None:
    f = tmp_path / f"book{d}.csv"
    f.write_text(f"position_id,book_id,secid,quantity_bonds\nP1,OFZ,{OFZ},{qty}\nP2,CORP,{USD_FACE},3\n",
                 encoding="utf-8")
    with store.connect(store_url) as con:
        cli._load(con, f, d, out=lambda s: None)


def default_dates(store_url: str) -> list[str]:
    with store.connect(store_url) as con:
        return sorted(d.isoformat() for d in store.default_runs(con, date(2026, 1, 1)))


def test_plan_run_oldest_first_carry_forward_and_skip_unchanged(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    assert [(d.date, d.status) for d in p.days] == [("2026-10-09", "no_positions")] + [(d, "todo") for d in DATES[1:]]
    assert all(d.why == "no default run" and d.cached for d in p.todo) and p.todo[0].prev_date == "2026-10-09"
    prepared = []
    real = cli.prepare
    monkeypatch.setattr(cli, "prepare", lambda sd, d, *a, **kw: prepared.append(d) or real(sd, d, *a, **kw))
    ran = []
    real_run = cli.eod_run
    monkeypatch.setattr(cli, "eod_run", lambda *a, **kw: ran.append(a[1]) or real_run(*a, **kw))
    res = backfill.run(s, p, store_url=store_url, out=lambda line: None)
    assert res["done"] == DATES[1:] and ran == DATES[1:] and not res["failed"] and res["left"] == 0
    assert sorted(prepared) == DATES  # each market prepared once: as its own date, then as the next one's previous
    assert default_dates(store_url) == DATES[1:]
    with store.connect(store_url) as con:
        sets = {r[0]: r[1] for r in con.execute("SELECT business_date::text, position_set_ids FROM run")}
        assert len({tuple(v) for v in sets.values()}) == 1  # 13.10-15.10: the 12.10 report carried forward
        assert con.execute("SELECT count(*) FROM run_summary").fetchone()[0] == 4  # the pages' sums written
        assert con.execute("SELECT count(*) FROM md_static_summary").fetchone()[0] == 0  # the read model: latest only
        again = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    assert again.count("done") == 4 and not again.todo
    load(store_url, tmp_path, "2026-10-14", 12)  # a new report: that date and the next change
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    assert [(d.date, d.why) for d in p.todo] == [("2026-10-14", "positions changed"), ("2026-10-15", "positions changed")]


def test_logic_change_but_not_a_new_build_recomputes(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        backfill.run(s, backfill.plan(con, s, date(2026, 10, 14), date(2026, 10, 15)), store_url=store_url,
                     out=lambda line: None)
        lv = backfill.logic_version()
        monkeypatch.setattr(backfill, "logic_version", lambda: lv + "+build20261101")
        assert not backfill.plan(con, s, date(2026, 10, 14), date(2026, 10, 15)).todo
        monkeypatch.setattr(backfill, "logic_version", lambda: lv.replace("+v", "+v9"))
        assert [d.why for d in backfill.plan(con, s, date(2026, 10, 14), date(2026, 10, 15)).todo] == ["program updated"] * 2
    assert backfill.same_logic("0.1.0+v7+core1+build1", "0.1.0+v7+core1") and not backfill.same_logic("a+v6", "a+v7")


def test_a_release_without_logic_changes_does_not_recompute_the_history(tmp_path, cache, store_url, monkeypatch):
    """Issue #24: a new backend version (a release) with the same VALUATION_VERSION and core leaves the history alone;
    the dates stamped by the older release count as current."""
    import desk

    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    monkeypatch.delenv("DESK_BUNDLE_VERSION", raising=False)
    with store.connect(store_url) as con:
        backfill.run(s, backfill.plan(con, s, date(2026, 10, 14), date(2026, 10, 15)), store_url=store_url,
                     out=lambda line: None)
        monkeypatch.setattr(desk, "__version__", "9.9.9")  # the next release, same valuation and core
        monkeypatch.setenv("DESK_BUNDLE_VERSION", "9.9.9-20261101-abc")
        assert not backfill.plan(con, s, date(2026, 10, 14), date(2026, 10, 15)).todo
    lv = desk.logic_version()
    assert backfill.same_logic(lv, lv.replace("9.9.9", "0.2.3", 1).split("+build")[0] + "+build0.2.3-x")
    assert not backfill.same_logic(lv, lv.replace(f"+v{desk.VALUATION_VERSION}", f"+v{desk.VALUATION_VERSION + 1}"))
    assert not backfill.same_logic(lv, lv.replace("+core", "+core9", 1))
    assert backfill.same_logic("0.2.3+v11+core0.35.0", "0.2.4+v11+core0.35.0+build0.2.4-20261022")
    assert not backfill.same_logic("0.2.4+v11+core0.35.0", "0.2.4+v11+core0.36.0")
    assert not backfill.same_logic("0.2.4+v11+core0.35.0", None) and backfill.same_logic(None, None)


def test_a_failed_date_is_not_retried_on_the_same_inputs(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    real = cli.eod_run

    def flaky(settings, d, **kw):
        if d == "2026-10-14":
            raise ValueError("bad bundle")
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)
    with store.connect(store_url) as con:
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                           out=lambda line: None)
        assert res["failed"] == [("2026-10-14", "ValueError: bad bundle")] and res["done"] == ["2026-10-15"]
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
        assert [(d.date, d.status) for d in p.days if d.status != "no_positions"] == [
            ("2026-10-14", "failed"), ("2026-10-15", "done")]
        st = backfill.status(con, s, today=date(2026, 10, 15))
    assert st["failed"] == 1 and st["failed_dates"] == ["2026-10-14"] and st["oldest_run"] == "2026-10-15"


def test_a_failed_date_is_retried_once_after_an_upgrade(tmp_path, cache, store_url, monkeypatch):
    """#155: a date remembered as failed (``backfill.json``) on one program version is tried again once on the next
    (another bundle VERSION: another ``logic_version()``), and stays failed within a version."""
    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    real = cli.eod_run
    tried: list[str] = []

    def flaky(settings, d, **kw):
        tried.append(d)
        if d == "2026-10-14":
            raise ValueError("bad bundle")
        return real(settings, d, **kw)

    def status(con) -> dict[str, str]:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
        return {d.date: d.status for d in p.days if d.status != "no_positions"}

    monkeypatch.setattr(cli, "eod_run", flaky)
    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.4-20261022-aaaaaaa")
    with store.connect(store_url) as con:
        backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                     out=lambda line: None)
        assert status(con) == {"2026-10-14": "failed", "2026-10-15": "done"}
        monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.5-20261023-bbbbbbb")  # the fixed program installed
        assert status(con) == {"2026-10-14": "todo", "2026-10-15": "done"}  # the same build-less logic: 15.10 kept
        monkeypatch.setattr(cli, "eod_run", lambda settings, d, **kw: (tried.append(d), real(settings, d, **kw))[1])
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                           out=lambda line: None)
        assert res["done"] == ["2026-10-14"] and not res["failed"]
        assert status(con) == {"2026-10-14": "done", "2026-10-15": "done"}
    assert tried == ["2026-10-14", "2026-10-15", "2026-10-14"]


@pytest.mark.parametrize("error", [
    pytest.param(lambda: __import__("psycopg").OperationalError("connection to server on socket failed"), id="store"),
    pytest.param(lambda: __import__("desk.marketdata.hub", fromlist=["HubError"]).HubError("hub unreachable"), id="hub"),
])
def test_a_store_or_network_error_is_not_a_failed_date(tmp_path, cache, store_url, monkeypatch, error):
    """E2E 21.10.2026: the store went away mid-backfill and every later date was remembered as failed, so they were
    never computed again. Such an error stops the run and is not remembered: the next run computes the dates."""
    load(store_url, tmp_path, "2026-10-13", 10)
    s = settings(tmp_path, cache, store_url)
    real = cli.eod_run
    down = {"on": True}

    def flaky(settings, d, **kw):
        if down["on"] and d == "2026-10-14":
            raise error()
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)
    with store.connect(store_url) as con:
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                           out=lambda line: None)
        assert res["done"] == ["2026-10-13"] and [d for d, _ in res["failed"]] == ["2026-10-14"]
        assert "tried again next time" in res["failed"][0][1] and res["left"] == 1  # 15.10 not even tried
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
        assert [(d.date, d.status) for d in p.days if d.status != "no_positions"] == [
            ("2026-10-13", "done"), ("2026-10-14", "todo"), ("2026-10-15", "todo")]
        down["on"] = False
        res = backfill.run(s, p, store_url=store_url, out=lambda line: None)
    assert res["done"] == ["2026-10-14", "2026-10-15"] and not res["failed"]


def test_budget_runs_at_least_one_date(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    res = backfill.run(s, p, store_url=store_url, budget_s=0.0, out=lambda line: None)
    assert res["done"] == ["2026-10-12"] and res["left"] == 3


def test_a_date_not_cached_without_a_hub_is_skipped(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 16),
                          hub=[("2026-10-16", "a" * 64), *[(d, "") for d in reversed(DATES)]])
    assert p.hub and p.todo[-1].date == "2026-10-16" and not p.todo[-1].cached and p.to_downloads() == 1
    res = backfill.run(s, p, store_url=store_url, out=lambda line: None)
    assert res["skipped"] == ["2026-10-16"] and res["done"] == ["2026-10-14", "2026-10-15"]


def test_autopilot_step_after_the_eod_step_within_its_budget(tmp_path, cache, store_url):
    load(store_url, tmp_path, "2026-10-12", 10)
    load(store_url, tmp_path, "2026-10-15", 11)
    s = settings(tmp_path, cache, store_url, backfill_budget_s=0.001, backfill_from="2026-10-01")
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 11, 30))
    st = ap.run_cycle(hub=False)
    steps = {x["name"]: x for x in st["steps"]}
    assert steps["eod"]["detail"].startswith("2026-10-15 run")  # the latest date in the eod step
    assert steps["backfill"]["status"] == "ok" and "ran 1 dates (2026-10-12..2026-10-12)" in steps["backfill"]["detail"]
    assert "2 dates in the next cycles" in steps["backfill"]["detail"]
    ap.run_cycle(hub=False)
    ap.run_cycle(hub=False)
    st = ap.run_cycle(hub=False)
    b = next(x for x in st["steps"] if x["name"] == "backfill")
    assert b["detail"].startswith("up to date (2026-10-01..2026-11-30: 4 of 4 dates")  # 15.10 by the eod step (#23)
    assert default_dates(store_url) == DATES[1:]
    off = settings(tmp_path, cache, store_url, backfill=False)
    st = Autopilot(settings_loader=lambda: off, store_url=store_url, today=lambda: date(2026, 11, 30)).run_cycle(hub=False)
    assert "backfill" not in {x["name"] for x in st["steps"]}


def test_inbox_reads_the_older_reports_once(tmp_path, store_url):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261017.csv")
    now = datetime(2026, 10, 27, 9, tzinfo=UTC)
    copied = (now - timedelta(hours=1)).timestamp()  # before the fixed now, whatever the wall clock says
    for f in inbox.iterdir():
        os.utime(f, (copied, copied))
    s = Settings(home=tmp_path / "home", snapshot_cache=FIX, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                 private_adapter=GOOD, private_adapter_config=CONFIG, inbox_poll_seconds=0, inbox_lookback_days=1)
    with store.connect(store_url) as con:
        r = scan.scan(con, s, now=now)
        assert r.files == {"ignored_too_old": 2}, r.text()
        line = backfill.inbox(con, s, date(2026, 10, 1))
        assert line and "accepted" in line
        assert con.execute("SELECT count(DISTINCT valid_date) FROM position_set").fetchone()[0] == 2
        assert backfill.inbox(con, s, date(2026, 10, 1)) is None  # done for this start and adapter
        assert backfill.inbox(con, s, date(2026, 10, 1), force=True) is not None
        assert backfill.inbox(con, s, date(2026, 9, 1)) is not None  # another start date reads again


def test_cli_dry_run_and_run(tmp_path, cache, store_url, monkeypatch, capsys):
    load(store_url, tmp_path, "2026-10-13", 10)
    monkeypatch.setenv("DESK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DESK_SNAPSHOT_CACHE", str(cache))
    monkeypatch.setenv("DESK_STORE_URL", store_url)
    monkeypatch.setenv("DESK_HUB_URL", "")
    assert cli.main(["backfill", "--from", "2026-10-01", "--to", "2026-10-15", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "backfill 2026-10-01..2026-10-15 (cache only): 5 business dates, 0 up to date, 3 to run, 2 without" in out
    assert "2026-10-13  todo    no default run" in out and default_dates(store_url) == []
    assert cli.main(["backfill", "--from", "2026-10-01", "--to", "2026-10-15", "--no-inbox"]) == 0
    out = capsys.readouterr().out
    assert "[3/3] 2026-10-15 done" in out and "backfill: 3 dates run, 0 failed" in out
    assert default_dates(store_url) == DATES[2:]
    assert cli.main(["backfill", "--from", "2026-10-16", "--to", "2026-10-15"]) == 2


def test_settings_keys():
    assert not check_settings_doc({"backfill": True, "backfill_from": "2026-01-05", "backfill_budget_s": 300})
    assert check_settings_doc({"backfill_from": "05.01.2026"}) == ["backfill_from: must be a date YYYY-MM-DD"]
    assert Settings().backfill and Settings().backfill_from == "2026-01-05"


def test_status_api_and_diagnostics(tmp_path, cache, store_url, monkeypatch):
    from fastapi.testclient import TestClient

    from desk import diagnostics
    from desk.api import create_app

    load(store_url, tmp_path, "2026-10-14", 10)
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url, "DESK_HOME": str(tmp_path / "h"),
                 "DESK_SNAPSHOT_CACHE": str(cache), "DESK_BACKFILL_FROM": "2026-10-01"}.items():
        monkeypatch.setenv(k, v)
    client = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    st = client.get("/api/v1/backfill/status").json()
    assert st["from"] == "2026-10-01" and st["todo"] == 2 and st["no_positions"] == 3 and st["enabled"]
    assert st["oldest_run"] is None and st["first_positions"] == "2026-10-14"
    doc = diagnostics.build(store_url=store_url, hub=False)
    assert doc["backfill"]["todo"] == 2 and doc["backfill"]["run_summaries"] == {"written": 0, "complete_runs": 0}


def test_autopilot_recomputes_stale_dates_inside_the_eod_window(tmp_path, cache, store_url, monkeypatch):
    """#23: after an upgrade the eod step recomputes only the latest positions date, and the backfill used to stop at
    the eod step's first date, so the other dates of the eod window stayed stale for good («194 of 195» while
    ``desk backfill --dry-run`` listed them). Every date the backfill's plan calls stale is now run by some step."""
    import desk

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url, backfill_from="2026-10-01")
    monkeypatch.delenv("DESK_BUNDLE_VERSION", raising=False)
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 10, 20))
    ap.run_cycle(hub=False)
    assert default_dates(store_url) == DATES[1:]  # 12.10-15.10: all inside the eod window (from 06.10)
    monkeypatch.setattr(desk, "VALUATION_VERSION", desk.VALUATION_VERSION + 1)  # an upgrade
    for _ in range(2):
        ap.run_cycle(hub=False)
    with store.connect(store_url) as con:
        runs = store.default_runs(con, date(2026, 10, 1))
        assert {d.isoformat(): r["logic_version"] for d, r in runs.items()} == {d: desk.logic_version() for d in DATES[1:]}
        assert not backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 20)).todo
    b = next(x for x in ap.status()["steps"] if x["name"] == "backfill")
    assert b["detail"].startswith("up to date (2026-10-01..2026-10-20: 4 of 4 dates"), b["detail"]


def test_a_date_the_eod_step_failed_on_other_inputs_is_the_backfills(tmp_path, cache, store_url, monkeypatch):
    """#23 (review of #209): the backfill leaves out only the dates the eod step ran, failed or skipped as failed
    before this cycle. A date the eod step failed once on other inputs (its key stays in ``_failed`` until a restart)
    and does not take this cycle is the backfill's: it used to be left out for good, the backfill saying «up to date»
    while the plan kept it stale."""
    import desk

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url, backfill_from="2026-10-01")
    monkeypatch.delenv("DESK_BUNDLE_VERSION", raising=False)
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, today=lambda: date(2026, 10, 20))
    ap.run_cycle(hub=False)
    ap._failed["2026-10-13"] = ((), "an older snapshot", None, "an older logic")  # failed once, on other inputs
    monkeypatch.setattr(desk, "VALUATION_VERSION", desk.VALUATION_VERSION + 1)  # an upgrade: 12.10 the eod step's
    for _ in range(2):
        ap.run_cycle(hub=False)
    with store.connect(store_url) as con:
        run = store.default_runs(con, date(2026, 10, 1))[date(2026, 10, 13)]
        assert run["logic_version"] == desk.logic_version()
        assert not backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 20)).todo


def _stale_after_run(monkeypatch, bad: str) -> list[str]:
    """``cli.eod_run`` whose run of ``bad`` is stamped with an old logic version: the date never becomes current."""
    from desk.publish import desk_run

    real, stamp = cli.eod_run, desk_run.logic_version
    tried: list[str] = []

    def run(settings, d, **kw):
        tried.append(d)
        monkeypatch.setattr(desk_run, "logic_version", (lambda: "0.0.1+v1+core0") if d == bad else stamp)
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", run)
    return tried


def test_a_date_that_does_not_converge_is_a_problem_date_after_two_runs(tmp_path, cache, store_url, monkeypatch,
                                                                        caplog):
    """#23: a date still stale after its run is checked against the plan again, the differing field logged; after two
    runs it is a problem date with its reason (not tried again on the same inputs); the queue goes on meanwhile."""
    load(store_url, tmp_path, "2026-10-13", 10)
    s = settings(tmp_path, cache, store_url)
    tried = _stale_after_run(monkeypatch, "2026-10-13")

    def plan():
        with store.connect(store_url) as con:
            return backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))

    caplog.set_level("WARNING", logger="desk.backfill")
    res = backfill.run(s, plan(), store_url=store_url, out=lambda line: None)
    assert res["done"] == ["2026-10-14", "2026-10-15"] and not res["failed"] and res["unconverged"] == ["2026-10-13"]
    assert "2026-10-13" in caplog.text and "program updated" in caplog.text and "logic_version" in caplog.text
    assert [(d.date, d.status) for d in plan().days if d.status != "no_positions"] == [
        ("2026-10-13", "todo"), ("2026-10-14", "done"), ("2026-10-15", "done")]
    res = backfill.run(s, plan(), store_url=store_url, out=lambda line: None)
    assert [d for d, _ in res["failed"]] == ["2026-10-13"] and "program updated" in res["failed"][0][1]
    assert [(d.date, d.status) for d in plan().days if d.status != "no_positions"] == [
        ("2026-10-13", "failed"), ("2026-10-14", "done"), ("2026-10-15", "done")]
    res = backfill.run(s, plan(), store_url=store_url, out=lambda line: None)
    assert not res["done"] and not res["failed"] and tried == ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-13"]
    with store.connect(store_url) as con:
        st = backfill.status(con, s, today=date(2026, 10, 15))
    assert st["total"] == 3 and st["done"] == 2 and st["failed"] == 1
    assert [(p["date"], p["why"], p["attempts"]) for p in st["problem_dates"]] == [("2026-10-13", "program updated", 2)]
    assert "does not converge" in st["problem_dates"][0]["error"]


def test_a_run_that_created_nothing_is_a_failed_date(tmp_path, cache, store_url, monkeypatch):
    load(store_url, tmp_path, "2026-10-14", 10)
    s = settings(tmp_path, cache, store_url)
    real = cli.eod_run
    monkeypatch.setattr(cli, "eod_run", lambda settings, d, **kw: None if d == "2026-10-14" else real(settings, d, **kw))
    with store.connect(store_url) as con:
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                           out=lambda line: None)
        assert [d for d, _ in res["failed"]] == ["2026-10-14"] and "no run" in res["failed"][0][1]
        assert res["done"] == ["2026-10-15"] and res["left"] == 0
        st = backfill.status(con, s, today=date(2026, 10, 15))
    assert st["failed_dates"] == ["2026-10-14"] and st["problem_dates"][0]["date"] == "2026-10-14"


def test_a_date_that_keeps_failing_on_the_network_stops_holding_the_queue(tmp_path, cache, store_url, monkeypatch):
    """#23: a store or network error stops the run (the next dates would hit it too) and is not remembered as a
    failed date; when one date keeps hitting it run after run, the queue goes past it, the date a problem date."""
    from desk.marketdata.hub import HubError

    load(store_url, tmp_path, "2026-10-13", 10)
    s = settings(tmp_path, cache, store_url)
    real = cli.eod_run

    def flaky(settings, d, **kw):
        if d == "2026-10-14":
            raise HubError("bundle: 502 bad gateway", status=502)
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)

    def run():
        with store.connect(store_url) as con:
            return backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15)), store_url=store_url,
                                out=lambda line: None)

    assert run()["done"] == ["2026-10-13"]  # 15.10 waits: the hub may be down as a whole
    res = run()
    assert res["done"] == ["2026-10-15"] and [d for d, _ in res["failed"]] == ["2026-10-14"]
    with store.connect(store_url) as con:
        st = backfill.status(con, s, today=date(2026, 10, 15))
    assert st["todo"] == 1 and [(p["date"], p["attempts"]) for p in st["problem_dates"]] == [("2026-10-14", 2)]
    monkeypatch.setattr(cli, "eod_run", real)
    assert run()["done"] == ["2026-10-14"]
    with store.connect(store_url) as con:
        assert backfill.status(con, s, today=date(2026, 10, 15))["problem_dates"] == []


def test_backfill_status_route_lists_the_hub_problem_dates(tmp_path, cache, store_url, monkeypatch):
    """#163: the hub dates the sync step cannot load (``problem_dates``) come with the backfill's status, so the
    header's indicator counts them among the dates that do not converge."""
    from fastapi.testclient import TestClient

    from desk.api import create_app

    load(store_url, tmp_path, "2026-10-14", 10)
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url, "DESK_HOME": str(tmp_path / "h"),
                 "DESK_SNAPSHOT_CACHE": str(cache), "DESK_BACKFILL_FROM": "2026-10-01"}.items():
        monkeypatch.setenv(k, v)
    prob = {"date": "2026-10-16", "error": "SnapshotError: digest", "attempts": 3, "next_attempt": "2026-10-20T10:00:00+00:00"}

    class Thread:
        def status(self):
            return {"steps": [{"name": "sync", "status": "ok", "detail": "", "problem_dates": [prob], "at": ""}]}

    app = create_app(store_url)
    client = TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    assert client.get("/api/v1/backfill/status").json()["hub_problem_dates"] == []
    app.state.autopilot = Thread()
    assert client.get("/api/v1/backfill/status").json()["hub_problem_dates"] == [prob]


def _near(iso: str, want: datetime, slack_s: float = 5) -> bool:
    return abs((datetime.fromisoformat(iso) - want).total_seconds()) <= slack_s


def _cycle(s: Settings, store_url: str, t: datetime, **kw) -> tuple[dict, dict]:
    with store.connect(store_url) as con:
        res = backfill.run(s, backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15), kw.pop("rows", None)),
                           store_url=store_url, out=lambda line: None, now=t, **kw)
        return res, backfill.status(con, s, today=date(2026, 10, 15))


def test_the_same_network_error_on_every_date_is_a_stall_not_problem_dates(tmp_path, cache, store_url, monkeypatch):
    """TDM#447: the network down for every date used to add one problem date a cycle («48 of M · K do not converge»,
    K growing, nothing done). The same transient error on two dates in a row is the environment's: the dates' attempts
    are not counted, the status says the backfill stands (cause, error, since, next attempt), a run tries the oldest
    date and one not in the stall yet; once the network is back the dates run again."""
    from desk.marketdata.hub import TransportError

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    real, tried, down = cli.eod_run, [], {"on": True}

    def flaky(settings, d, **kw):
        tried.append(d)
        if down["on"]:
            raise TransportError(f"bundle {d}: timed out after 30 s")
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    res, st = _cycle(s, store_url, t0)
    assert tried == ["2026-10-12"] and res["stall"] is None and st["stall"] is None  # one date: maybe its own (#23)
    probes = [["2026-10-12", "2026-10-13"], ["2026-10-12", "2026-10-14"], ["2026-10-12", "2026-10-15"],
              ["2026-10-12"], ["2026-10-12"]]  # all four in the stall: only the oldest is tried
    for i, want in enumerate(probes, 1):
        tried.clear()
        res, st = _cycle(s, store_url, t0 + timedelta(hours=i))
        assert tried == want, (i, tried)
        assert st["problem_dates"] == [] and st["failed"] == 0 and st["todo"] == 4 and st["done"] == 0, (i, st)
        stall = st["stall"]
        assert stall["cause"] == "network" and stall["phase"] == "run" and stall["error"] == \
            f"TransportError: bundle {want[-1]}: timed out after 30 s"
        assert _near(stall["since"], t0) and stall["dates"] == min(i + 1, 4) and stall["stops"] == i
        wait = backfill.STALL_BACKOFF_S[min(i, len(backfill.STALL_BACKOFF_S)) - 1]
        assert _near(stall["next_attempt"], t0 + timedelta(hours=i, seconds=wait))
        assert res["stall"]["next_attempt"] == stall["next_attempt"] and not res["done"]
    down["on"] = False
    res, st = _cycle(s, store_url, t0 + timedelta(hours=6))
    assert res["done"] == ["2026-10-12", *DATES[2:]] and res["stall"] is None
    assert st["stall"] is None and st["problem_dates"] == [] and st["done"] == 4 and st["todo"] == 0


def test_two_dates_failing_on_their_own_do_not_hold_the_queue(tmp_path, cache, store_url, monkeypatch):
    """Review of TDM#447: 12.10 and 13.10 time out each on its own (big bundles through a slow proxy, say), 14.10 and
    15.10 run. Their two errors in a row look like a stall; the next run tries a date outside it, which runs: the
    stall is over, 12.10 failed on its own. The two become problem dates (``transient``), the queue goes on."""
    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    real, tried = cli.eod_run, []

    def flaky(settings, d, **kw):
        tried.append(d)
        if d in ("2026-10-12", "2026-10-13"):
            raise TimeoutError(f"bundle {d}: timed out")
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    _cycle(s, store_url, t0)
    res, st = _cycle(s, store_url, t0 + timedelta(hours=1))
    assert st["stall"] is not None and st["stall"]["dates"] == 2  # as far as it can tell: the environment
    tried.clear()
    res, st = _cycle(s, store_url, t0 + timedelta(hours=2))
    assert tried == ["2026-10-12", "2026-10-14", "2026-10-15"] and res["done"] == ["2026-10-14", "2026-10-15"]
    assert st["stall"] is None
    for i in range(3, 9):
        tried.clear()
        res, st = _cycle(s, store_url, t0 + timedelta(hours=i))
        assert st["stall"] is None and res["stall"] is None, (i, st["stall"])
        assert set(tried) == {"2026-10-12", "2026-10-13"}, (i, tried)
    assert st["done"] == 2 and st["todo"] == 2
    assert [(p["date"], p["kind"]) for p in st["problem_dates"]] == [("2026-10-12", "transient"),
                                                                       ("2026-10-13", "transient")]


def test_a_download_outage_is_not_ended_by_a_date_run_from_the_cache(tmp_path, cache, store_url, monkeypatch):
    """TDM#447: the hub down (``HubUnavailable`` after its retries, the 502 of its last attempt) for the bundles to
    download; a date already cached runs meanwhile, which says nothing of the downloads: the stall stands, the dates
    that failed to download are not taken for their own trouble. Once the hub answers they download and run."""
    import shutil

    from desk.marketdata.bundle import SnapshotDir
    from desk.marketdata.hub import HubUnavailable

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    rows = [(d, SnapshotDir(cache).market_id(d)) for d in reversed(DATES)]
    ids, down, synced, hub = dict(rows), {"on": True}, [], tmp_path / "hub"
    with store.connect(store_url) as con:  # 13.10 run before: 14.10 (from the cache) has its previous close cached
        backfill.run(s, backfill.plan(con, s, date(2026, 10, 13), date(2026, 10, 13), rows), store_url=store_url,
                     out=lambda line: None)
    hub.mkdir()
    for d in ("2026-10-12", "2026-10-15"):  # to download
        shutil.move(cache / f"{ids[d]}.json.gz", hub / f"{ids[d]}.json.gz")

    def sync(client, cache_dir, want):
        synced.append(list(want))
        if down["on"]:
            raise HubUnavailable("hub.test unreachable after 3 attempts: 502 bad gateway", 502)
        for d in want:
            shutil.copy(hub / f"{ids[d]}.json.gz", cache_dir)

    monkeypatch.setattr(backfill.cache, "sync", sync)
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    res, st = _cycle(s, store_url, t0, rows=rows, client=object())
    assert not res["done"] and [d for d, _ in res["failed"]] == ["2026-10-12"] and synced == [["2026-10-12"]]
    res, st = _cycle(s, store_url, t0 + timedelta(hours=1), rows=rows, client=object())
    # 12.10 a second time, 14.10 from the cache runs (not the downloads), 15.10 fails to download: it stands
    assert res["done"] == ["2026-10-14"] and res["stall"] is not None and [d for d, _ in res["failed"]] == [
        "2026-10-12", "2026-10-15"]
    assert st["stall"]["cause"] == "hub" and st["stall"]["status"] == 502 and st["stall"]["phase"] == "bundle"
    assert st["problem_dates"] == [] and backfill.state(s)["problems"].keys() == {"2026-10-15"}  # 12.10 not its own
    down["on"] = False
    res, st = _cycle(s, store_url, t0 + timedelta(hours=2), rows=rows, client=object())
    assert res["done"] == ["2026-10-12", "2026-10-15"] and st["stall"] is None and st["todo"] == 0


def test_the_older_transient_problem_dates_go_and_an_old_stall_is_forgotten(tmp_path, cache, store_url, monkeypatch):
    """TDM#447 after the upgrade: the transient problem dates the old program piled up go once the backfill stands
    (none shown while it does); a stall on disk whose dates are no longer to run, or whose last error is old, is
    forgotten: a new outage starts a new one (its since, its first wait)."""
    from desk.marketdata.hub import TransportError

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url)
    monkeypatch.setattr(cli, "eod_run", lambda settings, d, **kw: (_ for _ in ()).throw(TransportError(f"{d}: reset")))
    with store.connect(store_url) as con:
        p = backfill.plan(con, s, date(2026, 10, 1), date(2026, 10, 15))
    st = backfill.state(s)
    st["problems"] = {d.date: {"kind": "transient", "why": d.why, "error": "TransportError: x", "attempts": 5,
                               "sig": d.sig} for d in p.todo}
    st["stall"] = {"key": "run:network:TransportError", "phase": "run", "dates": ["2026-09-01", "2026-09-02"],
                   "since": "2026-09-01T00:00:00+00:00", "stops": 7, "cause": "network", "error": "old",
                   "at": "2026-09-02T00:00:00+00:00", "next_attempt": "2026-09-02T01:00:00+00:00"}
    backfill.save_state(s, st)
    with store.connect(store_url) as con:
        assert len(backfill.status(con, s, today=date(2026, 10, 15))["problem_dates"]) == 4
        assert backfill.status(con, s, today=date(2026, 10, 15))["stall"] is None  # none of its dates to run
    t0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    res, st = _cycle(s, store_url, t0)
    assert st["stall"]["stops"] == 1 and _near(st["stall"]["since"], t0) and st["problem_dates"] == []
    assert backfill.state(s)["problems"].keys() == {"2026-10-13"}  # the last one tried
    state = backfill.state(s)
    state["stall"]["at"] = (t0 - timedelta(days=2)).isoformat()
    backfill.save_state(s, state)
    res, st = _cycle(s, store_url, t0 + timedelta(hours=1))
    assert res["stall"] is None and st["stall"] is None  # old: forgotten, 12.10 starts a new streak


def test_stall_causes_and_keys():
    import psycopg

    from desk.marketdata.hub import HubError, HubUnavailable, TransportError

    cases = [(psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), "store_slow"),
             (psycopg.OperationalError("connection to server failed"), "store"),
             (HubUnavailable("hub.test unreachable after 3 attempts: 502", 502), "hub"),
             (HubUnavailable("hub.test unreachable after 3 attempts: timed out"), "hub"),
             (HubError("hub token not configured; set DESK_HUB_TOKEN_FILE (or DESK_HUB_TOKEN)"), "access"),
             (HubError("token rejected by hub.test (401)", 401), "access"),
             (HubError("hub address not configured; set DESK_HUB_URL"), "setup"),
             (HubError("bundle: 407", 407), "proxy"), (TransportError("tunnel refused", 407), "proxy"),
             (TransportError("timed out"), "network"), (TimeoutError(), "network"), (OSError("disk full"), "disk")]
    for e, cause in cases:
        assert backfill.transient(e) and backfill.stall_cause(e) == cause, (e, backfill.stall_cause(e))
    assert backfill.stall_key(HubUnavailable("a", 502)) == backfill.stall_key(HubUnavailable("b", 502)) == \
        "hub:HubUnavailable:502"
    assert backfill.stall_key(HubUnavailable("a", 502)) != backfill.stall_key(HubUnavailable("a", 503))


def test_autopilot_waits_while_the_backfill_stands(tmp_path, cache, store_url, monkeypatch):
    """TDM#447: while the backfill stands the autopilot's step does not try the dates again before the stall's next
    attempt and says why (status ``error``), unless the cycle is asked for (``trigger``); after the wait, the network
    back, the dates run."""
    import desk
    from desk.inbox.timesem import MSK
    from desk.marketdata.hub import TransportError

    load(store_url, tmp_path, "2026-10-12", 10)
    s = settings(tmp_path, cache, store_url, backfill_from="2026-10-01")
    monkeypatch.delenv("DESK_BUNDLE_VERSION", raising=False)
    clock = [datetime(2026, 10, 20, 12, 0, tzinfo=MSK)]
    ap = Autopilot(settings_loader=lambda: s, store_url=store_url, now=lambda: clock[0])
    ap.run_cycle(hub=False)
    assert default_dates(store_url) == DATES[1:]
    real, tried, down = cli.eod_run, [], {"on": True}

    def flaky(settings, d, **kw):
        if d != "2026-10-12":  # the latest positions date: the eod step's after an upgrade, it runs
            tried.append(d)
            if down["on"]:
                raise TransportError("proxy: connection reset")
        return real(settings, d, **kw)

    monkeypatch.setattr(cli, "eod_run", flaky)
    monkeypatch.setattr(desk, "VALUATION_VERSION", desk.VALUATION_VERSION + 1)  # an upgrade: the past dates the backfill's

    def step() -> dict:
        return next(x for x in ap.run_cycle(hub=False)["steps"] if x["name"] == "backfill")

    step()
    assert tried == ["2026-10-13"], tried
    b = step()  # 13.10 a second time, then 14.10: the same error, the environment's
    assert b["status"] == "error" and "stands since" in b["detail"], b["detail"]
    assert "next attempt 2026-10-20T12:10:0" in b["detail"], b["detail"]
    tried.clear()
    b = step()
    assert tried == [] and b["status"] == "error" and "stands since" in b["detail"]  # waits
    ap.trigger()  # a cycle asked for: no wait
    b = step()
    assert tried == ["2026-10-13", "2026-10-15"] and "stands since" in b["detail"], tried
    clock[0] += timedelta(minutes=31)
    down["on"] = False
    b = step()
    assert b["status"] == "ok" and "ran 3 dates (2026-10-13..2026-10-15)" in b["detail"], b["detail"]
    assert default_dates(store_url) == DATES[1:]
