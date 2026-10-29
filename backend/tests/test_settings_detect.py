"""What the settings page finds out by itself (#449, ``desk.settings_detect``, ``GET /api/v1/settings/detect``): the
system proxy and its source, the trust store, Python interpreters offered for ``acquisition_python`` (none is run),
report folders to offer from the adapter's config files and from files the inbox accepted. Synthetic paths only."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk import settings_detect as sd
from desk.api import create_app
from desk.inbox import scan
from desk.settings import Settings


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


def client(url: str = "postgresql://unused") -> TestClient:
    return TestClient(create_app(url), base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))


def test_registry_proxy_reads_the_internet_options() -> None:
    assert sd.registry_proxy({"ProxyEnable": 1, "ProxyServer": "proxy.example:3128"}) == "http://proxy.example:3128"
    assert sd.registry_proxy({"ProxyEnable": 1, "ProxyServer": "http=a:1;https=b.example:8080"}) == "http://b.example:8080"
    assert sd.registry_proxy({"ProxyEnable": 0, "ProxyServer": "proxy.example:3128"}) is None  # switched off
    assert sd.registry_proxy({"ProxyEnable": 1, "ProxyServer": "ftp=a:1"}) is None
    assert sd.registry_proxy({}) is None


def test_proxy_source_order_and_masking() -> None:
    reg = {"ProxyEnable": 1, "ProxyServer": "win.example:3128", "AutoConfigURL": "http://pac.example/p.pac"}
    p = sd.proxy({}, {}, {}, reg)
    assert (p["system"], p["source"], p["effective_source"], p["pac"]) == ("http://win.example:3128", "windows", "windows", True)
    env = {"HTTPS_PROXY": "http://u:secret@env.example:8080"}
    p = sd.proxy(env, {"https_proxy": "http://s.example:1"}, {}, reg)
    assert p["system"] == "http://u:***@env.example:8080" and p["source"] == "environment"
    assert p["effective_source"] == "environment" and "secret" not in json.dumps(p)  # the real environment wins
    # the variable load() exported from settings.json is not the system's
    p = sd.proxy({"HTTPS_PROXY": "http://s.example:1"}, {"https_proxy": "http://s.example:1"},
                 {"HTTPS_PROXY": "http://s.example:1"}, {})
    assert p["environment"] is None and p["system"] is None and p["settings"] == "http://s.example:1"
    assert p["effective_source"] == "settings"


def test_pythons_are_found_not_run(tmp_path: Path) -> None:
    exe = tmp_path / "Python311" / "python.exe"
    exe.parent.mkdir()
    exe.write_text("never run")
    stub = tmp_path / "WindowsApps" / "python.exe"
    stub.parent.mkdir()
    stub.write_text("store stub")
    own = tmp_path / "own" / "python"
    own.parent.mkdir()
    own.write_text("this program")
    found = sd.pythons({"PATH": ""}, registry=lambda: [(str(exe), "3.11"), (str(stub), "3.12"), (str(exe), "3.11"),
                                                       ("\\\\server\\share\\python.exe", "3.10")], own=str(own))
    assert [(f["path"], f["source"], f["version"], f["own"]) for f in found] == [
        (str(exe), "registry", "3.11", False), (str(own), "own", found[1]["version"], True)]
    assert sd._version(r"C:\Users\u\AppData\Local\Programs\Python\Python312\python.exe") == "3.12"


def test_covered_folders() -> None:
    dirs = [(r"C:\Reports", True), (r"D:\Pos", False)]
    assert sd.covered(r"c:\reports\sub\x", dirs) and sd.covered("D:\\Pos\\", dirs) and sd.covered(r"D:\Pos\**", dirs)
    assert not sd.covered(r"D:\Pos\sub", dirs) and not sd.covered(r"C:\ReportsOld", dirs)
    assert sd.covered("/r/a", [("/r", True)]) and not sd.covered("/r/a", [("/r", False)])


def test_folders_named_in_the_adapter_config_are_offered(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (a := tmp_path / "a").mkdir()
    (b := tmp_path / "b").mkdir()
    (out := tmp_path / "out").mkdir()
    (cfg / "options.json").write_text(json.dumps({
        "report_dirs": [str(a), str(b) + "/**", str(tmp_path / "missing")], "output_dir": str(out),
        "note": "not a path", "nested": {"folder": "\\\\server\\share\\reports"}}), encoding="utf-8")
    (cfg / "broken.json").write_text("{", encoding="utf-8")
    found = sd.config_folders(cfg, [(str(a), False)])
    assert [(f["path"], f["recursive"], f["key"], f["exists"]) for f in found] == [
        (str(b), True, "report_dirs[1]", True), ("\\\\server\\share\\reports", False, "nested.folder", None)]
    assert all(f["file"] == "options.json" for f in found)
    assert sd.config_folders(None, []) == [] and sd.config_folders(tmp_path / "none", []) == []


def test_api_detect(home: Path, tmp_path: Path, monkeypatch) -> None:
    for v in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    reports = tmp_path / "reports"
    reports.mkdir()
    (home / "adapter-config").mkdir()
    (home / "adapter-config" / "options.json").write_text(json.dumps({"report_dirs": [str(reports)]}), encoding="utf-8")
    c = client()
    j = c.get("/api/v1/settings/detect").json()
    assert j["proxy"]["source"] in (None, "windows") and j["trust"]["kind"] in ("system", "python")
    assert j["defaults"] == {"snapshot_cache": str(home / "snapshots"), "inbox_archive": str(home / "inbox-archive"),
                             "private_adapter_config": str(home / "adapter-config")}
    assert j["adapter"]["config_default_exists"] is True and j["adapter"]["config_files"] == 1
    assert [f["path"] for f in j["inbox"]["from_adapter_config"]] == [str(reports)]
    assert j["inbox"]["from_accepted"] == [] and "store unavailable" in j["inbox"]["accepted_error"]
    assert any(p["own"] for p in j["python"])
    c.put("/api/v1/settings", json={"inbox_dirs": [{"path": str(reports), "label": "", "recursive": False}],
                                    "acquisition_python": str(tmp_path / "nope.exe")})
    j = c.get("/api/v1/settings/detect").json()
    assert j["inbox"]["from_adapter_config"] == []  # the monitor scans it now
    assert j["acquisition_python"] == {"path": str(tmp_path / "nope.exe"), "exists": False}
    assert not (home / "settings.json").read_text(encoding="utf-8").count("snapshot_cache")  # nothing written
    remote = TestClient(create_app("postgresql://unused"), base_url="http://127.0.0.1:8000", client=("10.1.2.3", 1))
    assert remote.get("/api/v1/settings/detect").status_code == 403


def test_folders_of_accepted_files(home: Path, tmp_path: Path, store_url: str) -> None:
    old, cur = tmp_path / "old", tmp_path / "cur"
    old.mkdir()
    cur.mkdir()
    st = Settings(home=home)
    with psycopg.connect(store_url, autocommit=True) as con:
        for i, (folder, status, day) in enumerate([(old, "accepted", "2026-10-20"), (old, "partially_accepted", "2026-10-21"),
                                                   (old, "rejected", "2026-10-22"), (cur, "accepted", "2026-10-22"),
                                                   (st.adapter_output_dir / "act", "accepted", "2026-10-22")]):
            sha = f"{i:064x}"
            con.execute("INSERT INTO inbox_file (sha256, path, name, size_bytes, mtime) VALUES (%s, %s, %s, 1, now())",
                        (sha, str(folder / f"f{i}.csv"), f"f{i}.csv"))
            con.execute("INSERT INTO inbox_file_status (sha256, status, adapter_fingerprint, path, report_date) "
                        "VALUES (%s, %s, 'fp', %s, %s)", (sha, status, str(folder / f"f{i}.csv"), day))
        rows = con.execute(sd.ACCEPTED_FOLDERS).fetchall()
    cols = ["folder", "files", "last_report"]
    found = sd.accepted_folders([dict(zip(cols, r, strict=True)) for r in rows], st, [(str(cur), False)])
    assert found == [{"path": str(old), "files": 2, "last_report": "2026-10-21", "exists": True}]
    (home / "settings.json").write_text(json.dumps({"inbox_dirs": [{"path": str(cur)}]}), encoding="utf-8")
    j = client(store_url).get("/api/v1/settings/detect").json()
    assert j["inbox"]["accepted_error"] is None
    assert [(f["path"], f["files"], f["last_report"]) for f in j["inbox"]["from_accepted"]] == [
        (str(old), 2, date(2026, 10, 21).isoformat())]
