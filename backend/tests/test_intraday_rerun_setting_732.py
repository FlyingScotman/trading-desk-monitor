"""TDM#732, the pause between two intraday runs for changed trades on the settings screen: saved and checked by the server
(a whole number from 0, 0 - no pause), shown as the effective value, read again by every cycle (no restart)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk.api import create_app
from desk.inbox import scan


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


def test_the_server_takes_zero_and_whole_numbers_and_refuses_the_rest(client: TestClient) -> None:
    assert client.get("/api/v1/settings").json()["effective"]["intraday_rerun_min_s"] == 300     # the default
    for n in (0, 1, 45, 300, 3600):
        j = client.put("/api/v1/settings", json={"intraday_rerun_min_s": n}).json()
        assert j["intraday_rerun_min_s"] == n and j["effective"]["intraday_rerun_min_s"] == n, n
    for bad in (-1, 2.5, "30", True):
        r = client.put("/api/v1/settings", json={"intraday_rerun_min_s": bad})
        assert r.status_code == 422 and "intraday_rerun_min_s" in r.json()["message"], (bad, r.json())
    assert client.get("/api/v1/settings").json()["intraday_rerun_min_s"] == 3600, "a refusal changes nothing"
    j = client.put("/api/v1/settings", json={"intraday_rerun_min_s": None}).json()   # an empty field: the default
    assert "intraday_rerun_min_s" not in j and j["effective"]["intraday_rerun_min_s"] == 300
