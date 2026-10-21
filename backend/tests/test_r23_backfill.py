"""R23, the history of runs: ``desk backfill`` and the autopilot's backfill step compute the default run of every past
business date with positions (carried forward from the last report), oldest first, skipping the dates whose inputs
and logic are unchanged, reusing each date's prepared market for the next, a failed date not retried on the same
inputs, within a time budget per cycle; the older reports of the inbox read once from the start date. The fixture's
market shifted to five business days. Synthetic data only."""

from __future__ import annotations

from datetime import UTC, date, datetime
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
    assert b["detail"].startswith("up to date (2026-10-01..2026-10-14: 3 of 3 dates")  # 15.10: the eod step's
    assert default_dates(store_url) == DATES[1:]
    off = settings(tmp_path, cache, store_url, backfill=False)
    st = Autopilot(settings_loader=lambda: off, store_url=store_url, today=lambda: date(2026, 11, 30)).run_cycle(hub=False)
    assert "backfill" not in {x["name"] for x in st["steps"]}


def test_inbox_reads_the_older_reports_once(tmp_path, store_url):
    inbox = tmp_path / "in"
    put(inbox, "positions_20261015.csv", "positions_20261017.csv")
    s = Settings(home=tmp_path / "home", snapshot_cache=FIX, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive",
                 private_adapter=GOOD, private_adapter_config=CONFIG, inbox_poll_seconds=0, inbox_lookback_days=1)
    with store.connect(store_url) as con:
        r = scan.scan(con, s, now=datetime(2026, 10, 27, 9, tzinfo=UTC))
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
