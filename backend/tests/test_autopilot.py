"""The autopilot (I4): hub sync -> inbox scan -> EOD of the dates whose inputs changed, its status and API, against
the stub hub of slice S3, the reference adapter and the canonical inbox fixtures. Synthetic data only."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest
import test_slice_s3
from fastapi.testclient import TestClient
from test_slice_s3 import TOKEN

from desk import cli, store
from desk.api import create_app
from desk.autopilot import LOCK_FILE, Autopilot, CycleLock
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1"
TODAY = date(2026, 10, 19)
STEP_KEYS = {"name", "status", "detail", "at"}
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
                    inbox_poll_seconds=0, **kw)


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
                 "DESK_INBOX_POLL_SECONDS": "0"}.items():
        monkeypatch.setenv(k, v)
    assert cli.main(["cycle"]) == 1  # desk cycle: the step error is the exit code
    out = capsys.readouterr().out
    assert "sync: error - hub: 127.0.0.1 unreachable after 4 attempts" in out and "cached snapshots are used" in out
    assert "scan: ok" in out and "eod: ok - 2026-10-15 run 1 (no default run)" in out
    assert "Ошибки: sync; latest default run: 1 of 2026-10-15" in out
    assert TOKEN not in out
    assert runs(store_url) == [(1, date(2026, 10, 15), "EOD", "complete")]
    assert cli.main(["cycle", "--no-hub"]) == 0  # the hub skipped: nothing else to do
    out = capsys.readouterr().out
    assert "sync: skipped" in out and "eod: skipped" in out and "Данные актуальны" in out


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


def test_settings_switch_and_errors_in_status(tmp_path, store_url):
    off = Settings(home=tmp_path / "home", store_url=store_url, snapshot_cache=tmp_path / "cache", autopilot=False)
    st = Autopilot(settings_loader=lambda: off).run_cycle()  # a thread that follows the flag does nothing while off
    assert st["message"] == "Автопилот выключен в настройках" and not st["steps"]
    assert Autopilot(settings_loader=lambda: off, force=True).run_cycle()["steps"]  # desk cycle: runs anyway

    def bad():
        raise RuntimeError("settings.json: bad JSON")

    st = Autopilot(settings_loader=bad).run_cycle()
    assert st["state"] == "error" and steps(st) == {"settings": ("error", "RuntimeError: settings.json: bad JSON")}
    st = Autopilot(settings_loader=lambda: Settings(home=tmp_path / "home", store_url="postgresql://127.0.0.1:1/none",
                                                    snapshot_cache=tmp_path / "cache")).run_cycle()
    assert st["state"] == "error" and steps(st)["store"][0] == "error" and steps(st)["sync"][0] == "skipped"
    assert set(st) == {"enabled", "state", "last_cycle_at", "last_ok_at", "message", "steps", "latest_run"}
    assert all(set(x) == STEP_KEYS for x in st["steps"])


def test_api_status_and_run(tmp_path, store_url):
    app = create_app(store_url)
    c = TestClient(app)
    st = c.get("/api/v1/autopilot/status").json()
    assert st == {"enabled": False, "state": "idle", "last_cycle_at": None, "last_ok_at": None,
                  "message": "Автопилот не запущен", "steps": [], "latest_run": None}
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
        assert st["enabled"] is True and st["state"] == "idle" and st["message"] == "Данные актуальны"
        assert [(x["name"], x["status"]) for x in st["steps"]] == [("sync", "skipped"), ("scan", "skipped"),
                                                                    ("eod", "skipped")]
        assert st["steps"][2]["detail"] == "no positions in the store"
        r = c.post("/api/v1/autopilot/run")
        assert r.status_code == 202 and r.json() == {"queued": True}
        _wait(lambda: len(loads) == 2)  # woken long before the 3600 s sleep ends
    finally:
        ap.stop()
    assert not ap.running and c.get("/api/v1/autopilot/status").json()["enabled"] is False
    assert "/api/v1/autopilot/status" in c.get("/openapi.json").json()["paths"]


HOLDER = """
import sys
from pathlib import Path
from desk.autopilot import CycleLock
lock = CycleLock(Path(sys.argv[1]))
print("held" if lock.acquire() else "busy", flush=True)
sys.stdin.readline()
"""


def test_cycle_lock_across_processes(tmp_path, store_url):
    """A desk cycle while another process (desk serve's thread, another daily_backend.bat) is in a cycle does
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
        assert steps(ap.run_cycle()) == {"cycle": ("skipped", "another cycle running")}
    assert runs(store_url) == [(1, date(2026, 10, 15), "EOD", "complete")]
    assert (s.logs_dir / "autopilot.log").is_file()  # the log goes to <home>/logs, created on demand


def _wait(cond, timeout: float = 30.0) -> None:
    t = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < t, "timed out"
        time.sleep(0.05)
