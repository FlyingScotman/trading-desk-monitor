"""TDM#657, the number of processes of the history on the settings screen: saved and checked by the server (0, the program
chooses, to ``BACKFILL_WORKERS_MAX``), read again by every cycle of the autopilot (no restart), and shown as the number the
pool of the last cycle started with, not as a figure worked out again when the page is opened."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk import backfill, parallel
from desk.api import create_app
from desk.autopilot import Autopilot
from desk.inbox import scan
from desk.settings import BACKFILL_WORKERS_MAX, Settings, SettingsError, check_settings_doc, load


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    scan.forget_adapters()
    return h


@pytest.fixture
def client(home: Path) -> TestClient:
    return TestClient(create_app("postgresql://unused"), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_the_server_takes_zero_to_the_limit_and_refuses_the_rest(client: TestClient, home: Path) -> None:
    for n in (0, 1, 3, BACKFILL_WORKERS_MAX):
        j = client.put("/api/v1/settings", json={"backfill_workers": n}).json()
        assert j["backfill_workers"] == n and j["effective"]["backfill_workers"] == n, n
    for bad in (-1, BACKFILL_WORKERS_MAX + 1, 2.5, "3", True):
        r = client.put("/api/v1/settings", json={"backfill_workers": bad})
        assert r.status_code == 422 and "backfill_workers" in r.json()["message"], (bad, r.json())
    assert client.get("/api/v1/settings").json()["backfill_workers"] == BACKFILL_WORKERS_MAX, "a refusal changes nothing"
    j = client.put("/api/v1/settings", json={"backfill_workers": None}).json()  # an empty field: the default
    assert "backfill_workers" not in j and j["effective"]["backfill_workers"] == 0


def test_the_limit_is_the_same_in_the_file_and_in_the_environment(home: Path) -> None:
    assert check_settings_doc({"backfill_workers": BACKFILL_WORKERS_MAX}) == []
    assert "from 0 to" in check_settings_doc({"backfill_workers": BACKFILL_WORKERS_MAX + 1})[0]
    env = {"DESK_HOME": str(home), "DESK_ENV_FILE": str(home / "none.env")}
    assert load({**env, "DESK_BACKFILL_WORKERS": str(BACKFILL_WORKERS_MAX)}).backfill_workers == BACKFILL_WORKERS_MAX
    with pytest.raises(SettingsError, match="DESK_BACKFILL_WORKERS"):
        load({**env, "DESK_BACKFILL_WORKERS": str(BACKFILL_WORKERS_MAX + 1)})


def test_every_cycle_reads_the_number_again_and_the_screen_shows_the_pool_it_started(home: Path, monkeypatch) -> None:
    """The settings file is read by each cycle (``Autopilot``'s loader): a number saved on the screen is the next cycle's, no
    restart. ``started`` is what the pool had; the free memory of the moment the page is opened does not change it."""
    sizes: list[int] = []

    class FakePool:
        def __init__(self, workers: int, init=None) -> None:
            sizes.append(workers)
            self.workers = workers

        def close(self) -> None:
            pass

    monkeypatch.setattr(parallel, "Pool", FakePool)
    monkeypatch.setattr(parallel, "auto_workers", lambda: 3)
    ap = Autopilot(settings_loader=load, store_url="postgresql://unused", today=lambda: date(2026, 11, 30))
    monkeypatch.setattr(ap, "_backfill_passes", lambda settings, *a: ("ok", "stub"))

    def run_cycle() -> Settings:
        settings = load()
        ap._backfill(settings, None, None, False, [])
        return settings

    def write(doc: dict) -> None:
        (home / "settings.json").write_text(json.dumps(doc), encoding="utf-8")

    write({})
    s0 = run_cycle()
    write({"backfill_workers": 2})
    s2 = run_cycle()
    write({"backfill_workers": 1})
    s1 = run_cycle()
    write({"backfill_workers": 0})
    s_auto = run_cycle()
    assert sizes == [3, 2, 3], sizes  # (one process is no pool: the dates run in the cycle's own)
    assert s0.backfill_workers == 0 and s2.backfill_workers == 2 and s1.backfill_workers == 1 and s_auto.backfill_workers == 0
    # the screen: a number the owner set is that number; the program's own choice is the one the pool started with
    assert backfill._workers(s2) == {"workers": 2, "workers_auto": False}
    assert backfill._workers(s1) == {"workers": 1, "workers_auto": False}
    monkeypatch.setattr(parallel, "auto_workers", lambda: 1)  # the memory is short at the moment the page is opened
    assert backfill._workers(s_auto) == {"workers": 3, "workers_auto": True}


def test_before_any_cycle_the_screen_shows_what_the_program_would_choose_now(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(parallel, "auto_workers", lambda: 2)
    assert backfill._workers(Settings(home=home)) == {"workers": 2, "workers_auto": True}


def test_a_number_the_program_chose_is_not_kept_after_the_owner_sets_one(home: Path, monkeypatch) -> None:
    monkeypatch.setattr(parallel, "auto_workers", lambda: 3)
    backfill.note_workers(Settings(home=home), 3)
    assert backfill._workers(Settings(home=home)) == {"workers": 3, "workers_auto": True}
    assert backfill._workers(Settings(home=home, backfill_workers=1)) == {"workers": 1, "workers_auto": False}
    backfill.note_workers(Settings(home=home, backfill_workers=1), 1)
    monkeypatch.setattr(parallel, "auto_workers", lambda: 2)
    assert backfill._workers(Settings(home=home)) == {"workers": 2, "workers_auto": True}, \
        "back to the program's choice: the number it chose before the owner's one is not shown as a pool's"
