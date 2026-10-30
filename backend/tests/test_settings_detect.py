"""What the settings page finds out by itself (#449, ``desk.settings_detect``, ``GET /api/v1/settings/detect``): the
system proxy and its source, the trust store, Python interpreters offered for ``acquisition_python`` (none is run),
report folders to offer from the adapter's config files and from files the inbox accepted. Synthetic paths only."""

from __future__ import annotations

import json
import os
import urllib.request
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


def winreg_like(server: str, override: str = ""):
    """What urllib's ``getproxies_registry`` / ``proxy_bypass_registry`` give for these Internet Options (their
    Windows-only code; the override match is urllib's own ``_proxy_bypass_winreg_override``)."""
    proxies: dict[str, str] = {}
    if "=" not in server:
        server = f"http={server};https={server};ftp={server}"
    for part in server.split(";"):
        scheme, addr = part.split("=", 1)
        proxies[scheme] = addr if "://" in addr else ("socks://" if scheme == "socks" else "http://") + addr
    if proxies.get("socks"):
        proxies.setdefault("https", proxies["socks"].replace("socks://", "socks4://"))
    return lambda: (proxies, lambda host: bool(override) and urllib.request._proxy_bypass_winreg_override(host, override))


HUB = "https://hub.example"


def test_proxy_is_what_hub_proxy_takes() -> None:
    p = sd.proxy(HUB, {}, {}, winreg_like("win.example:3128"), pac=True)
    assert p == {"system": "http://win.example:3128", "source": "windows", "bypassed": False, "socks": False, "pac": True}
    # http only: urllib has no https proxy, the hub goes direct
    p = sd.proxy(HUB, {}, {}, winreg_like("http=a.example:1"))
    assert p["system"] is None and p["source"] is None and not p["bypassed"]
    # the hub in the exceptions (by name, or <local> for a host without dots): direct
    assert sd.proxy(HUB, {}, {}, winreg_like("win.example:3128", "*.example;10.*"))["bypassed"] is True
    p = sd.proxy("https://hub", {}, {}, winreg_like("win.example:3128", "<local>"))
    assert p["bypassed"] and p["system"] is None and p["source"] == "windows"
    assert sd.proxy(HUB, {}, {}, winreg_like("win.example:3128", "<local>"))["bypassed"] is False
    # SOCKS only: urllib takes it for https too, the monitor cannot use it
    p = sd.proxy(HUB, {}, {}, winreg_like("socks=s.example:1080"))
    assert p["system"] == "socks4://s.example:1080" and p["socks"] is True


def test_proxy_from_the_environment_masked_and_not_the_exported_one() -> None:
    env = {"HTTPS_PROXY": "http://u:secret@env.example:8080", "NO_PROXY": "other.example"}
    p = sd.proxy(HUB, env, {}, winreg_like("win.example:3128"))
    assert p["system"] == "http://u:***@env.example:8080" and p["source"] == "environment"
    assert "secret" not in json.dumps(p)
    assert sd.proxy(HUB, {**env, "NO_PROXY": "hub.example"}, {}, winreg_like("win.example:3128"))["bypassed"] is True
    # the variable load() exported from settings.json is not the system's: the Internet Options are
    p = sd.proxy(HUB, {"HTTPS_PROXY": "http://s.example:1"}, {"HTTPS_PROXY": "http://s.example:1"},
                 winreg_like("w.example:2"))
    assert p["system"] == "http://w.example:2" and p["source"] == "windows"
    both = {"https_proxy": "http://low:1", "HTTPS_PROXY": "http://up:1"}
    assert sd.environment_proxies(both, {}) == {"https": "http://low:1"}


def test_trust_follows_tls_context(monkeypatch) -> None:
    import ssl

    from desk.marketdata import hub

    assert sd.trust(Settings(hub_ca_bundle=Path("/x/ca.pem"))) == {"kind": "bundle"}
    monkeypatch.setattr(hub, "tls_context", lambda ca=None: ssl.create_default_context())  # truststore unusable
    assert sd.trust(Settings()) == {"kind": "python"}


def test_disk_never_touches_network_paths_and_has_a_budget(tmp_path: Path) -> None:
    d = sd.Disk(budget=2, remote=lambda drive: drive == "Z:")
    assert d.network("\\\\server\\share\\x") and d.network("Z:\\Reports") and not d.network("C:\\x")
    assert d.is_dir("\\\\server\\share") is None and d.is_dir("Z:\\Reports") is None and d.budget == 2
    assert d.is_dir(str(tmp_path)) is True and d.is_dir(str(tmp_path / "none")) is False
    assert d.is_dir(str(tmp_path / "none")) is False and d.budget == 0  # remembered, not asked again
    assert d.is_dir(str(tmp_path / "other")) is None  # out of checks


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
    found = sd.pythons({"PATH": ""}, sd.Disk(), registry=lambda: [(str(exe), "3.11"), (str(stub), "3.12"), (str(exe), "3.11"),
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
    found = sd.config_folders(cfg, [(str(a), False)], sd.Disk())
    assert [(f["path"], f["recursive"], f["key"], f["exists"]) for f in found] == [
        (str(b), True, "report_dirs[1]", True), ("\\\\server\\share\\reports", False, "nested.folder", None)]
    assert all(f["file"] == "options.json" for f in found)
    assert sd.config_folders(None, [], sd.Disk()) == []
    assert sd.config_folders(tmp_path / "none", [], sd.Disk()) == []


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
                             "private_adapter_config": str(home / "adapter-config"), "private_adapter": None,
                             "private_adapter_source": "reference"}
    assert [f["path"] for f in j["inbox"]["from_adapter_config"]] == [str(reports)]
    assert j["inbox"]["from_accepted"] == []  # no store: nothing to offer
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
    found = sd.accepted_folders([dict(zip(cols, r, strict=True)) for r in rows], st, [(str(cur), False)], sd.Disk())
    assert found == [{"path": str(old), "files": 2, "last_report": "2026-10-21", "exists": True}]
    (home / "settings.json").write_text(json.dumps({"inbox_dirs": [{"path": str(cur)}]}), encoding="utf-8")
    j = client(store_url).get("/api/v1/settings/detect").json()
    assert [(f["path"], f["files"], f["last_report"]) for f in j["inbox"]["from_accepted"]] == [
        (str(old), 2, date(2026, 10, 21).isoformat())]
