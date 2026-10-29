"""The autopilot (I4): hub sync -> inbox scan -> EOD of the dates whose inputs changed, its status and API, against
the stub hub of slice S3, the reference adapter and the canonical inbox fixtures. Synthetic data only."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_slice_s3 import TOKEN

from desk import autopilot, cli, store
from desk.api import create_app
from desk.autopilot import LOCK_FILE, Autopilot, CycleLock, progress_text
from desk.inbox import scan
from desk.inbox.timesem import MSK
from desk.marketdata import cache
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1"
TODAY = date(2026, 10, 19)
STEP_KEYS = {"name", "status", "detail", "at"}
OPTIONAL_STEP_KEYS = {"reason", "failed_dates", "problem_dates"}  # intraday reason; sync failed, problem dates
server, stub = test_slice_s3.server, test_slice_s3.stub  # the stub hub's fixtures


@pytest.fixture(autouse=True)
def _fresh_process():
    scan._handles.clear()
    scan._last_seen.clear()
    yield
    scan._handles.clear()
    scan._last_seen.clear()


def cfg(tmp_path: Path, store_url: str, hub=None, **kw) -> Settings:
    hub_kw = {"hub_url": f"http://127.0.0.1:{hub.server_address[1]}", "hub_token": TOKEN, "hub_timeout_s": 5} \
        if hub is not None else {}
    return Settings(**hub_kw, home=tmp_path / "home", snapshot_cache=tmp_path / "cache", store_url=store_url,
                    inbox_dirs=(tmp_path / "in",), inbox_archive=tmp_path / "archive", private_adapter_config=CONFIG,
                    inbox_poll_seconds=0, **{"inbox_lookback_days": 0, **kw})  # fixed fixture dates: no lookback


def put(tmp_path: Path, *names: str) -> None:
    (tmp_path / "in").mkdir(exist_ok=True)
    for n in names:
        shutil.copy(PRIV / n, tmp_path / "in" / n)


def steps(st: dict) -> dict[str, tuple[str, str]]:
    return {s["name"]: (s["status"], s["detail"]) for s in st["steps"]}


def runs(store_url: str) -> list[tuple]:
    with store.connect(store_url) as con:
        return con.execute("SELECT run_id, business_date, run_type, status FROM run ORDER BY run_id").fetchall()


def test_cycle_syncs_scans_and_runs_eod_once_then_on_a_new_revision(stub, tmp_path, store_url):
    s = cfg(tmp_path, store_url, stub)
    put(tmp_path, "positions_20261016.csv")  # start_of_day 16.10 -> positions of 15.10
    lines: list[str] = []
    ap = Autopilot(settings_loader=lambda: s, today=lambda: TODAY, out=lines.append)
    st = ap.run_cycle()
    by = steps(st)
    assert st["state"] == "idle" and st["last_ok_at"] == st["last_cycle_at"], by
    assert by["sync"][0] == "ok" and "pulled 2026-10-15" in by["sync"][1]
    assert by["scan"][0] == "ok" and "files: accepted 1" in by["scan"][1]
    assert by["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert runs(store_url) == [(1, date(2026, 10, 15), "EOD", "complete")]
    assert st["latest_run"]["run_id"] == "1" and st["latest_run"]["as_of"] == "2026-10-15"
    assert st["message"] == "Пересчитано: 2026-10-15"
    assert any(x.startswith("  desk run 1: complete") for x in lines)

    stub.requests.clear()
    st = ap.run_cycle()  # nothing changed: no download, no run
    by = steps(st)
    assert by["sync"][0] == "ok" and "up to date" in by["sync"][1], by
    assert not any("/bundle" in r for r in stub.requests)
    assert by["eod"] == ("skipped", "up to date (2026-10-15..2026-10-15)")
    assert len(runs(store_url)) == 1 and st["message"] == "Данные актуальны"

    put(tmp_path, "positions_20261016_2.csv")  # a correction of the same report: a new revision of B1
    st = ap.run_cycle()
    assert steps(st)["eod"] == ("ok", "2026-10-15 run 2 (positions changed)")
    assert [r[0] for r in runs(store_url)] == [1, 2]
    with store.connect(store_url) as con:
        assert store.default_runs(con, date(2026, 10, 1))[date(2026, 10, 15)]["run_id"] == 2
    assert steps(ap.run_cycle())["eod"][0] == "skipped"


def test_hub_down_still_scans_and_runs_on_the_cache(stub, tmp_path, store_url, monkeypatch, capsys):
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    stub.fail = 100  # 503 on every request
    for k, v in {"DESK_HOME": str(tmp_path / "home"), "DESK_ENV_FILE": str(tmp_path / "none.env"),
                 "DESK_STORE_URL": store_url,
                 "DESK_HUB_URL": f"http://127.0.0.1:{stub.server_address[1]}", "DESK_HUB_TOKEN": TOKEN,
                 "DESK_SNAPSHOT_CACHE": str(tmp_path / "cache"), "DESK_INBOX_DIRS": str(tmp_path / "in"),
                 "DESK_INBOX_ARCHIVE": str(tmp_path / "archive"), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0"}.items():
        monkeypatch.setenv(k, v)
    init = Autopilot.__init__  # desk cycle's clock fixed in business hours, whenever the test runs (#147)
    monkeypatch.setattr(Autopilot, "__init__", lambda self, **kw: init(
        self, **{"now": lambda: datetime.combine(TODAY, dtime(12, 0), MSK), **kw}))
    assert cli.main(["cycle"]) == 1  # desk cycle: the step error is the exit code
    out = capsys.readouterr().out
    assert "sync: error - hub: 127.0.0.1 unreachable after 4 attempts" in out and "cached snapshots are used" in out
    assert "scan: ok" in out and "eod: ok - 2026-10-15 run 1 (no default run)" in out
    # the sync found the hub down: the intraday step does not ask it again this cycle (#147)
    assert "intraday: skipped - hub down this cycle" in out
    assert "Ошибки: sync; latest run: 1 of 2026-10-15 (15.10 · закрытие)" in out, out
    assert TOKEN not in out
    assert runs(store_url) == [(1, date(2026, 10, 15), "EOD", "complete")]
    assert cli.main(["cycle", "--no-hub"]) == 0  # the hub skipped: nothing else to do
    out = capsys.readouterr().out
    assert "sync: skipped" in out and "eod: skipped" in out
    # the fixed clock: a business day after 10:15 without a run of today is not current (#156)
    assert "Данные устарели: нет прогона за 19.10" in out, out


def test_failed_eod_is_not_retried_until_inputs_change(tmp_path, store_url, monkeypatch):
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    calls = []

    def broken(*a, **kw):
        calls.append(a[1])
        raise ValueError("boom")

    monkeypatch.setattr(cli, "eod_run", broken)
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY)
    st = ap.run_cycle()
    assert st["state"] == "error" and steps(st)["eod"] == ("error", "2026-10-15 failed (ValueError: boom)")
    assert steps(st)["sync"][0] == "skipped" and st["last_ok_at"] is None
    st = ap.run_cycle()
    assert steps(st)["eod"] == ("skipped", "2026-10-15 failed before on the same inputs, not retried")
    put(tmp_path, "positions_20261016_2.csv")
    ap.run_cycle()
    assert calls == ["2026-10-15", "2026-10-15"]


def test_failed_eod_is_retried_once_after_an_upgrade(tmp_path, store_url, monkeypatch):
    """#155: a date that failed on one program version is tried again once on the next (another bundle VERSION, so
    another ``logic_version()``), not again within that version. Here without a restart (a restart forgets the failed
    inputs anyway)."""
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    calls = []
    real = cli.eod_run

    def broken(*a, **kw):
        calls.append(a[1])
        raise ValueError("time zone not recognized")

    monkeypatch.setattr(cli, "eod_run", broken)
    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.4-20261022-aaaaaaa")
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY)
    assert steps(ap.run_cycle())["eod"][0] == "error"
    assert steps(ap.run_cycle())["eod"] == ("skipped", "2026-10-15 failed before on the same inputs, not retried")
    assert calls == ["2026-10-15"]

    monkeypatch.setattr(cli, "eod_run", lambda *a, **kw: (calls.append(a[1]), real(*a, **kw))[1])
    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.5-20261023-bbbbbbb")  # the fixed program installed
    assert steps(ap.run_cycle())["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    assert steps(ap.run_cycle())["eod"][0] == "skipped" and calls == ["2026-10-15", "2026-10-15"]


def test_failed_eod_on_a_new_version_is_not_retried_within_it(tmp_path, store_url, monkeypatch):
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    calls = []

    def broken(*a, **kw):
        calls.append(a[1])
        raise ValueError("boom")

    monkeypatch.setattr(cli, "eod_run", broken)
    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.4-20261022-aaaaaaa")
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY)
    ap.run_cycle()
    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.2.5-20261023-bbbbbbb")
    assert steps(ap.run_cycle())["eod"][0] == "error"  # tried once on the new version
    for _ in range(2):
        assert steps(ap.run_cycle())["eod"][0] == "skipped"
    assert calls == ["2026-10-15", "2026-10-15"]


def test_upgrade_recomputes_the_latest_positions_date_once(tmp_path, store_url, monkeypatch):
    """Runs record ``desk.logic_version()``; after an upgrade (a bumped VALUATION_VERSION) the latest positions date is
    recomputed once; another bundle build of the same code (``+build``) recomputes nothing (``backfill.same_logic``)."""
    import desk

    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    monkeypatch.delenv("DESK_BUNDLE_VERSION", raising=False)
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY)
    assert steps(ap.run_cycle())["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    with store.connect(store_url) as con:
        lin = con.execute("SELECT lineage FROM run WHERE run_id = 1").fetchone()[0]
    assert lin["logic_version"] == desk.logic_version() and "+build" not in lin["logic_version"]
    assert steps(ap.run_cycle())["eod"][0] == "skipped"

    monkeypatch.setenv("DESK_BUNDLE_VERSION", "0.1.0-20261020-abc")  # a new bundle (deskctl sets it)
    assert desk.logic_version().endswith("+build0.1.0-20261020-abc")
    assert steps(ap.run_cycle())["eod"][0] == "skipped"  # a new build of the same code: no recompute (backfill.same_logic)
    monkeypatch.setattr(desk, "VALUATION_VERSION", desk.VALUATION_VERSION + 1)  # a hand-bumped logic version
    assert steps(ap.run_cycle())["eod"] == ("ok", "2026-10-15 run 2 (program updated)")
    assert steps(ap.run_cycle())["eod"][0] == "skipped"
    with store.connect(store_url) as con:
        assert store.default_runs(con, date(2026, 10, 1))[date(2026, 10, 15)]["logic_version"] == desk.logic_version()


def test_upgrade_leaves_older_dates_alone(tmp_path, monkeypatch):
    """Only the latest positions date is recomputed for a logic version change (also for a run of a version that
    recorded none); an older date only when its inputs change. The store and the snapshot cache are stubs here (the
    fixtures hold one market date; runs are immutable)."""
    import desk.autopilot as ap_mod

    d14, d15 = date(2026, 10, 14), date(2026, 10, 15)

    class Snap:
        def __init__(self, d: str) -> None:
            self.id = "m" + d

    class Dir:
        def __init__(self, *_: object) -> None:
            pass

        def market_dates(self) -> list[str]:
            return ["2026-10-14", "2026-10-15"]

        def market(self, d: str, cut: str = "eod_final") -> Snap:
            return Snap(d)

        def market_id(self, d: str, cut: str = "eod_final") -> str:
            return Snap(d).id

        def previous_market_date(self, d: str, cut: str = "eod_final") -> str | None:
            return "2026-10-14" if d == "2026-10-15" and cut == "eod_final" else None

    old = "0.0.9+v0+core0"
    default = {d14: {"run_id": 1, "market_snapshot_id": "m2026-10-14", "prev_market_snapshot_id": None,
                     "position_set_ids": [1], "logic_version": old, "private_inputs_hash": "unchanged"},
               d15: {"run_id": 2, "market_snapshot_id": "m2026-10-15", "prev_market_snapshot_id": "m2026-10-14",
                     "position_set_ids": [2], "logic_version": None, "private_inputs_hash": "unchanged"}}
    monkeypatch.setattr(ap_mod, "SnapshotDir", Dir)
    monkeypatch.setattr(ap_mod.store, "position_dates", lambda con, since: [d14, d15])
    monkeypatch.setattr(ap_mod.store, "current_sets", lambda con, d: ([1] if d == d14 else [2], 1))
    monkeypatch.setattr(ap_mod.store, "positions_for", lambda con, d:
                        store.PositionInputs([], [], {}, datetime.now(UTC), "unchanged"))
    monkeypatch.setattr(ap_mod.store, "default_runs", lambda con, since: default)
    monkeypatch.setattr(ap_mod.store, "dataset_set_ids", lambda con, d, *a, **kw: {})  # no OCP report, no balances
    calls: list[str] = []
    monkeypatch.setattr(cli, "eod_run", lambda settings, d, **kw: calls.append(d) or len(calls) + 2)
    ap = Autopilot(settings_loader=lambda: Settings(home=tmp_path), today=lambda: TODAY)
    ran: list[str] = []
    st = ap._eod(Settings(home=tmp_path, snapshot_cache=tmp_path), None, None, [], ran)
    assert st == ("ok", "2026-10-15 run 3 (program updated)") and calls == ["2026-10-15"] == ran


def test_settings_switch_and_errors_in_status(tmp_path, store_url):
    def early() -> datetime:  # a business day before WARN_AFTER: «актуальны» is about the steps, whenever it runs (#240)
        return datetime.combine(TODAY, dtime(9, 0), MSK)

    off = Settings(home=tmp_path / "home", store_url=store_url, snapshot_cache=tmp_path / "cache", autopilot=False)
    st = Autopilot(settings_loader=lambda: off, now=early).run_cycle()  # a thread following the flag: nothing while off
    assert st["message"] == "Автопилот выключен в настройках" and not st["steps"]
    assert Autopilot(settings_loader=lambda: off, force=True, now=early).run_cycle()["steps"]  # desk cycle: runs anyway

    def bad():
        raise RuntimeError("settings.json: bad JSON")

    st = Autopilot(settings_loader=bad, now=early).run_cycle()
    assert st["state"] == "error" and steps(st) == {"settings": ("error", "RuntimeError: settings.json: bad JSON")}
    st = Autopilot(settings_loader=lambda: Settings(home=tmp_path / "home", store_url="postgresql://127.0.0.1:1/none",
                                                    snapshot_cache=tmp_path / "cache"), now=early).run_cycle()
    assert st["state"] == "error" and steps(st)["store"][0] == "error" and steps(st)["sync"][0] == "skipped"
    assert set(st) == {"enabled", "state", "last_cycle_at", "last_ok_at", "message", "steps", "latest_run", "progress"}
    assert st["progress"] is None  # only while a cycle runs
    assert all(STEP_KEYS <= set(x) <= STEP_KEYS | OPTIONAL_STEP_KEYS for x in st["steps"])
    (tmp_path / "drop").mkdir()
    half = Settings(home=tmp_path / "home", store_url=store_url, snapshot_cache=tmp_path / "cache",
                    snapshot_drop=tmp_path / "drop")  # market data from a drop folder, no report folders
    assert Autopilot(settings_loader=lambda: half, now=early).run_cycle()["message"] == (
        "Данные актуальны (не настроено: папки с отчётами)")


def test_api_status_and_run(tmp_path, store_url):
    app = create_app(store_url)
    c = TestClient(app)
    st = c.get("/api/v1/autopilot/status").json()
    now = datetime.fromisoformat(st.pop("now"))  # #22: the server's clock and the intraday schedule, for the header
    assert now.tzinfo is not None and abs((now - datetime.now(UTC)).total_seconds()) < 60
    assert st == {"enabled": False, "state": "idle", "last_cycle_at": None, "last_ok_at": None,
                  "message": "Автопилот не запущен", "steps": [], "latest_run": None, "progress": None,
                  "schedule": {"intraday_from": "10:00", "prelim_from": "19:45", "poll_slot_s": 600,
                               "poll_lag_s": 300, "warn_after": "10:15"}}
    r = c.post("/api/v1/autopilot/run")
    assert r.status_code == 409 and r.json()["code"] == "invalid_parameter"

    loads = []

    def settings() -> Settings:
        loads.append(1)
        return Settings(home=tmp_path / "home", store_url=store_url, snapshot_cache=tmp_path / "cache",
                        inbox_poll_seconds=3600)

    ap = Autopilot(settings_loader=settings, store_url=store_url)
    app.state.autopilot = ap
    ap.start()
    try:
        _wait(lambda: len(loads) == 1 and c.get("/api/v1/autopilot/status").json()["last_cycle_at"])
        st = c.get("/api/v1/autopilot/status").json()
        assert st["enabled"] is True and st["state"] == "idle"
        assert st["message"] == "Не настроено: укажите маркет-дата сервер и папки с отчётами в Настройках"
        assert [(x["name"], x["status"]) for x in st["steps"]] == [("sync", "skipped"), ("scan", "skipped"),
                                                                    ("eod", "skipped"), ("morning", "skipped"),
                                                                    ("scenarios", "skipped"), ("backfill", "skipped")]
        assert st["steps"][2]["detail"] == "no positions in the store"
        r = c.post("/api/v1/autopilot/run")
        assert r.status_code == 202 and r.json() == {"queued": True}
        _wait(lambda: len(loads) == 2)  # woken long before the 3600 s sleep ends
    finally:
        ap.stop()
    assert not ap.running and c.get("/api/v1/autopilot/status").json()["enabled"] is False
    assert "/api/v1/autopilot/status" in c.get("/openapi.json").json()["paths"]


def test_status_shows_live_progress_of_a_running_cycle(tmp_path, store_url, monkeypatch):
    app = create_app(store_url)
    c = TestClient(app)
    seen: list[dict] = []

    def fake_scan(con, settings, *, progress=None, now=None, since=None):
        progress(0, None, "просмотр папок")
        seen.append(c.get("/api/v1/autopilot/status").json())
        progress(37, 412, "positions_20261016.csv")
        seen.append(c.get("/api/v1/autopilot/status").json())
        return scan.ScanResult("ok fake 1 abc")

    monkeypatch.setattr(scan, "scan", fake_scan)
    ap = Autopilot(settings_loader=lambda: cfg(tmp_path, store_url), today=lambda: TODAY)
    app.state.autopilot = ap
    st = ap.run_cycle()
    assert seen[0]["state"] == "running" and seen[0]["message"] == "Скан отчётов: просмотрено файлов 0"
    assert seen[1]["progress"] == {"step": "scan", "done": 37, "total": 412, "detail": "positions_20261016.csv"}
    assert seen[1]["message"] == "Скан отчётов: 37 из 412" and seen[1]["state"] == "running"
    assert [x["name"] for x in seen[1]["steps"]] == ["sync"]  # the steps done so far
    assert st["progress"] is None and st["state"] == "idle"  # cleared when the cycle ends
    assert c.get("/api/v1/autopilot/status").json()["progress"] is None
    assert progress_text({"step": "sync", "done": 2, "total": 5, "detail": ""}) == "Загрузка снимков: 2 из 5"
    assert progress_text({"step": "eod", "done": 1, "total": 3, "detail": "2026-10-16"}) == "EOD 16.10: 1 из 3"


def test_hub_sync_stays_within_the_lookback_caps_and_remembers_missing_dates(tmp_path, store_url, monkeypatch):
    days = []  # the hub's history: every weekday back to 2024
    d = TODAY
    while d >= date(2024, 1, 1):
        if d.weekday() < 5:
            days.append(d.isoformat())
        d -= timedelta(days=1)
    listed, asked, cached = [], [], set()

    def hub_dates(client):
        for x in days:
            listed.append(x)
            yield x

    def sync(client, folder, dates):
        (x,) = dates
        asked.append(x)
        if x == "2026-10-12":
            return [cache.Synced(x)]  # no snapshot for this date on the hub
        cached.add(x)
        return [cache.Synced(x, market_id="m" + x, downloaded=["m" + x])]

    class Client:
        host = "hub.test"

        def __init__(self, settings):
            pass

    class Dir:
        def __init__(self, folder):
            pass

        def market_dates(self):
            return sorted(cached)

    monkeypatch.setattr(autopilot, "HubClient", Client)
    monkeypatch.setattr(autopilot, "SnapshotDir", Dir)
    monkeypatch.setattr(autopilot.cache, "hub_dates", hub_dates)
    monkeypatch.setattr(autopilot.cache, "sync", sync)
    with store.connect(store_url) as con:  # files waiting for a calendar: years old, and of last week
        for i, rd in enumerate(("2019-05-06", "2023-01-10", "2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15")):
            sha = f"{i:064x}"
            con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, %s, %s, 1, now())",
                        (sha, f"/in/{i}", f"{i}.csv"))
            con.execute("INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path, report_date)"
                        " VALUES (%s, 'waiting_calendar', 'fp', %s, %s)", (sha, f"/in/{i}", rd))
        s = cfg(tmp_path, store_url, hub_url="https://hub.test", hub_token=TOKEN, autopilot_sync_last=2,
                inbox_lookback_days=10)
        ap = Autopilot(settings_loader=lambda: s, today=lambda: TODAY)
        detail = ap._hub_sync(s, con, [])
        assert asked == ["2026-10-19", "2026-10-16", "2026-10-15", "2026-10-14", "2026-10-13"], asked  # 2 + 3
        assert "1 next cycle" in detail and len(listed) < 20  # the hub's list read only down to the lookback
        detail = ap._hub_sync(s, con, [])
        assert asked[5:] == ["2026-10-12"] and "no snapshot for 2026-10-12" in detail
        ap._hub_sync(s, con, [])
        assert len(asked) == 6  # the missing date is remembered, the others are cached
    assert min(asked) >= "2026-10-09"  # never a date behind the lookback (today - 10 days)


HOLDER = """
import sys
from pathlib import Path
from desk.autopilot import CycleLock
lock = CycleLock(Path(sys.argv[1]))
print("held" if lock.acquire() else "busy", flush=True)
sys.stdin.readline()
"""


def test_cycle_lock_across_processes(tmp_path, store_url):
    """A desk cycle while another process (desk serve's thread, another update_daily.bat) is in a cycle does
    nothing: no second EOD of the same date."""
    (tmp_path / "cache").mkdir()
    for p in SNAPS.glob("*.json.gz"):
        shutil.copy(p, tmp_path / "cache" / p.name)
    put(tmp_path, "positions_20261016.csv")
    s = cfg(tmp_path, store_url)
    home = s.home
    holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(home)], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert (home / LOCK_FILE).is_file()
        with CycleLock(home) as held:  # another open of the file: taken
            assert held is False
        ap = Autopilot(settings_loader=lambda: s, today=lambda: TODAY)
        st = ap.run_cycle()
        assert st["state"] == "idle" and steps(st) == {"cycle": ("skipped", "another cycle running")}, st
        assert st["message"].startswith("Пропущено") and st["last_ok_at"] is None and st["last_cycle_at"]
        assert runs(store_url) == []
    finally:
        holder.communicate("\n", timeout=30)
    st = ap.run_cycle()  # released when the holder exits
    assert steps(st)["eod"] == ("ok", "2026-10-15 run 1 (no default run)")
    with CycleLock(home) as held:  # and released by the cycle
        assert held is True
        fresh = Autopilot(settings_loader=lambda: s, today=lambda: TODAY)  # a desk cycle process: knows no run yet
        st = fresh.run_cycle()
        assert steps(st) == {"cycle": ("skipped", "another cycle running")}
        assert st["latest_run"]["run_id"] == "1"  # the store's latest default run, not "none"
    assert runs(store_url) == [(1, date(2026, 10, 15), "EOD", "complete")]
    assert (s.logs_dir / "autopilot.log").is_file()  # the log goes to <home>/logs, created on demand


def _wait(cond, timeout: float = 30.0) -> None:
    t = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < t, "timed out"
        time.sleep(0.05)
