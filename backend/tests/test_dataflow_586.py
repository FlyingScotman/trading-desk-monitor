"""The data flow of a date (TDM#586): ``desk.dataflow`` and ``GET /api/v1/dataflow`` behind the monitors' screen C21
«Схема данных». Every block has a status in words, what it got and left out, a link; the edges say where incomplete
data went on (red on the page); the queue of background work ahead. Built from what the store, the cache and the
autopilot already have: no hub, no services. Synthetic data only (the fixtures of tests/fixtures/private/v1)."""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import cli, dataflow, store
from desk.api import auth, create_app
from desk.inbox import scan
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures"
PRIV, CONFIG, SNAPS = FIX / "private" / "v1", FIX / "private" / "v1" / "config", FIX / "mdhub_v1"
D = date.fromisoformat
SOURCES = {"positions", "ocp_report", "cash", "pnl_official", "trades", "terms", "past_trades", "market",
           "hub_history", "reference"}
PROCESS = {"read", "store", "intraday", "value", "ocp", "pnl", "backfill", "history_load"}
RESULTS = {"r_positions", "r_trades", "r_ocp", "r_pnl", "r_history"}


@pytest.fixture(autouse=True)
def _fresh_process():
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()
    yield
    for memo in (scan._handles, scan._last_seen, scan._static_seen):
        memo.clear()


def _env(monkeypatch, tmp_path: Path, store_url: str, inbox: Path) -> None:
    for k, v in {"DESK_ENV_FILE": str(tmp_path / "none.env"), "DESK_STORE_URL": store_url,
                 "DESK_INBOX_DIRS": str(inbox), "DESK_INBOX_ARCHIVE": str(tmp_path / "arch"),
                 "DESK_SNAPSHOT_CACHE": str(SNAPS), "DESK_PRIVATE_ADAPTER_CONFIG": str(CONFIG),
                 "DESK_INBOX_POLL_SECONDS": "0", "DESK_INBOX_LOOKBACK_DAYS": "0", "DESK_HOME": str(tmp_path / "h"),
                 "DESK_BACKFILL": "0"}.items():
        monkeypatch.setenv(k, v)


def _check_shape(doc: dict) -> dict[str, dict]:
    by = {b["id"]: b for b in doc["blocks"]}
    assert set(by) == SOURCES | PROCESS | RESULTS
    assert {b["column"] for b in doc["blocks"]} == {"source", "process", "result"}
    assert [c["id"] for c in doc["columns"]] == ["source", "process", "result"]
    for b in doc["blocks"]:
        assert b["status"] in dataflow.STATUS and b["status_text"] == dataflow.STATUS[b["status"]], b
        assert b["link"] and b["link"]["href"] and b["link"]["text"], b["id"]
        for ln in b["got"] + b["dropped"]:
            assert set(ln) == {"t", "at", "tone"} and ln["t"], (b["id"], ln)
    for e in doc["edges"]:
        assert e["from"] in by and e["to"] in by, e
        assert e["incomplete"] == bool(e["why"]), e
    assert doc["incomplete"] == sum(e["incomplete"] for e in doc["edges"])
    return by


def test_an_empty_store_waits_for_everything_and_never_fails(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    _env(monkeypatch, tmp_path, store_url, inbox)
    c = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    r = c.get("/api/v1/dataflow", params={"on": "2026-10-15"})
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    doc = r.json()
    by = _check_shape(doc)
    assert doc["date"] == "2026-10-15" and doc["run"] is None
    assert by["positions"]["status"] == "waiting" and by["positions"]["waits"] == "отчёт о позициях за 15.10"
    for k in ("value", "ocp", "pnl", "r_positions", "r_pnl"):
        assert by[k]["status"] == "waiting" and by[k]["waits"], k
    assert by["backfill"]["status"] == "off"  # DESK_BACKFILL=0: the setting turns it off
    assert by["positions"]["fix"]["href"] == "../settings.html#adapter/folders"  # this computer: the settings link
    assert any(a["id"] == "wait:positions" for a in doc["ahead"])
    assert c.get("/api/v1/dataflow", params={"on": "15.10.2026"}).status_code == 400
    assert c.get("/concepts/c21-data-flow.html").status_code == 200


def test_a_date_with_a_run_shows_what_went_where_and_what_was_incomplete(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    for n in ("positions_20261015.csv", "positions_20261016.csv"):
        shutil.copy(PRIV / n, inbox / n)
    _env(monkeypatch, tmp_path, store_url, inbox)
    assert cli.main(["inbox", "scan"]) == 0
    assert cli.main(["eod", "--date", "2026-10-15", "--snapshots", str(SNAPS)]) == 0
    c = TestClient(create_app(store_url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))
    doc = c.get("/api/v1/dataflow", params={"on": "2026-10-15"}).json()
    by = _check_shape(doc)
    assert doc["run"]["run_id"] == 1 and doc["run"]["is_default"] and doc["run"]["label"] == "15.10 · закрытие"
    assert by["positions"]["status"] == "ok" and by["positions"]["got"][0]["t"].startswith("за 15.10: книг 3 из 3")
    assert any(ln["t"].startswith("positions_20261016.csv") and ln["at"] for ln in by["positions"]["got"])
    assert by["read"]["status"] == "ok" and by["read"]["got"][0]["t"].startswith("файлов за 15.10: 1")
    assert by["value"]["status"] == "ok" and by["value"]["got"][0]["t"].startswith("прогон #1 · 15.10 · закрытие")
    assert by["market"]["status"] == "ok" and by["market"]["got"][0]["t"].startswith("окончательное закрытие")
    edges = {(e["from"], e["to"]): e for e in doc["edges"]}
    # no OCP report and no balances in the store: the OCP went on without them, and the arrow says so
    ocp_in = edges[("ocp_report", "ocp")]
    assert ocp_in["incomplete"] and "отчёт" in ocp_in["why"]
    assert by["ocp_report"]["status"] in ("waiting", "off")
    assert edges[("ocp", "r_ocp")]["incomplete"]
    assert edges[("positions", "read")]["incomplete"] is False
    # the OCP block receives incomplete data and says so among what it left out
    assert any("на входе неполные данные" in ln["t"] for ln in by["ocp"]["dropped"])
    # trades: none for this date in the fixtures
    assert by["trades"]["status"] in ("waiting", "off") and by["r_trades"]["status"] == "waiting"
    # no private values: no PnL figures, no folder paths
    text = json.dumps(doc, ensure_ascii=False)
    assert str(inbox) not in text and str(tmp_path) not in text


def test_a_viewer_reads_the_flow_without_the_settings_links(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    _env(monkeypatch, tmp_path, store_url, inbox)
    with store.connect(store_url) as con:
        auth.add_user(con, "vic", "viewer", "viewer-pass-1", actor="test")
    remote = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True)), base_url="http://10.0.0.7:8000",
                        client=("10.0.0.7", 50000))
    assert remote.get("/api/v1/dataflow").status_code == 401
    assert remote.post("/api/v1/auth/login", json={"username": "vic", "password": "viewer-pass-1"}).status_code == 200
    doc = remote.get("/api/v1/dataflow", params={"on": "2026-10-15"}).json()
    by = _check_shape(doc)
    assert all(b["fix"] is None for b in doc["blocks"])
    assert by["positions"]["fix_note"] == "настройка «Папки с отчётами» — у администратора"


def test_the_running_step_and_the_queue_ahead(store_url, tmp_path, monkeypatch):
    inbox = tmp_path / "in"
    inbox.mkdir()
    _env(monkeypatch, tmp_path, store_url, inbox)
    s = Settings(snapshot_cache=SNAPS, inbox_dirs=(inbox,), inbox_archive=tmp_path / "archive", private_adapter=None,
                 private_adapter_config=CONFIG, inbox_poll_seconds=0, inbox_lookback_days=0, home=tmp_path / "h")
    ap = {"enabled": True, "state": "running", "message": "Пересчёт истории: 3 из 10",
          "progress": {"step": "backfill", "done": 3, "total": 10, "detail": "2026-10-14"}, "steps": []}

    class Services:
        def history(self) -> dict:
            return {"available": True, "enabled": True, "from": "2026-01-01", "dates": 20, "limit_per_day": 40,
                    "counts": {"fetched": 5, "no_trades": 2, "no_books": 1, "todo": 12, "unavailable": 1,
                               "unmapped": 0, "partial": 0, "covered": 0}, "waiting": None, "current": None,
                    "next_at": None, "last": None}

    with store.connect(store_url) as con:
        doc = dataflow.build(con, s, D("2026-10-15"), autopilot=ap, services=Services(), admin=False)
    by = _check_shape(doc)
    assert by["backfill"]["status"] == "running" and by["backfill"]["progress"] == {"done": 3, "total": 10,
                                                                                     "text": "2026-10-14"}
    assert doc["ahead"][0] == {"id": "now", "title": "Сейчас", "text": "Пересчёт истории: 3 из 10", "at": None,
                               "eta_s": None, "progress": by["backfill"]["progress"]}
    pt = by["past_trades"]
    assert pt["status"] == "waiting" and pt["progress"] == {"done": 8, "total": 20, "text": "дат обработано"}
    assert pt["waits"] == "очередь: 12 дат, не больше 40 запросов в день"
    assert any(a["id"] == "past_trades" and "12" in a["text"] for a in doc["ahead"])
    edges = {(e["from"], e["to"]): e for e in doc["edges"]}
    assert edges[("past_trades", "backfill")]["why"] == "сделки 1 прошлых дат не получены"
    assert doc["autopilot"] == {"state": "running", "message": "Пересчёт истории: 3 из 10", "enabled": True}
