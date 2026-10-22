"""DESK_HOME (I1), settings.json and its precedence, the hub token file, adapter config_files (I2) and the
settings API behind /settings.html (I5)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from desk.api import create_app
from desk.inbox import scan
from desk.inbox.loader import check_config_files, check_describe
from desk.settings import (
    DEV_HOME,
    REPO_ROOT,
    Settings,
    SettingsError,
    adapter_config_dir,
    desk_home,
    load,
    read_settings_json,
    update_settings_json,
    write_hub_token,
)

TOKEN = "mdh_settings_secret_0123456789"
DESC = {"name": "cfg_adapter", "version": "0.1.0", "spi": 1, "contract": "private/1.0",
        "sources": [{"source": "positions_csv", "dataset": "positions", "time_basis": "start_of_day",
                     "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}]}


def env(home: Path, **kw: str) -> dict[str, str]:
    return {"DESK_HOME": str(home), "DESK_ENV_FILE": str(home / "none.env"), **kw}


def write_json(home: Path, doc: dict) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(json.dumps(doc), encoding="utf-8")


# ---- desk_home -------------------------------------------------------------------------------------------------


def test_desk_home_resolution(tmp_path: Path, monkeypatch) -> None:
    none = str(tmp_path / "none.env")
    assert desk_home({"DESK_HOME": str(tmp_path / "h"), "DESK_ENV_FILE": none}) == tmp_path / "h"
    assert desk_home({"DESK_HOME": "rel", "DESK_ENV_FILE": none}) == REPO_ROOT / "rel"
    assert desk_home({"DESK_ENV_FILE": none, "LOCALAPPDATA": str(tmp_path / "lad")}, windows=True) \
        == tmp_path / "lad" / "DeskMonitor"
    assert desk_home({"DESK_ENV_FILE": none}, windows=False) == DEV_HOME  # this test runs in a git checkout
    monkeypatch.setattr("desk.settings.in_checkout", lambda: False)
    assert desk_home({"DESK_ENV_FILE": none}, windows=False) == Path.home() / ".local" / "share" / "desk-monitor"
    ef = tmp_path / "desk.env"
    ef.write_text(f"DESK_HOME={tmp_path / 'from-file'}\n", encoding="utf-8")
    assert desk_home({"DESK_ENV_FILE": str(ef)}) == tmp_path / "from-file"
    assert desk_home({"DESK_ENV_FILE": str(ef), "DESK_HOME": str(tmp_path / "env")}) == tmp_path / "env"


def test_layout_under_home(tmp_path: Path) -> None:
    s = load(env(tmp_path))
    assert s.home == tmp_path and s.snapshot_cache == tmp_path / "snapshots" and s.db == tmp_path / "tdm.sqlite"
    assert s.inbox_archive == tmp_path / "inbox-archive" and s.pg_dir == tmp_path / "pg"
    assert s.private_adapter_config == tmp_path / "adapter-config" and s.logs_dir == tmp_path / "logs"
    assert s.report_dir is None and s.autopilot is True and s.autopilot_sync_last == 5
    assert not (tmp_path / "adapter-config").exists()  # created on demand only
    assert adapter_config_dir(s) == tmp_path / "adapter-config" and (tmp_path / "adapter-config").is_dir()
    explicit = Settings(home=tmp_path, private_adapter_config=tmp_path / "typo")
    assert adapter_config_dir(explicit) == tmp_path / "typo" and not (tmp_path / "typo").exists()
    dev = Settings(home=DEV_HOME)  # the dev checkout keeps its folder names
    assert dev.pg_dir == DEV_HOME / "desk_pg" and dev.inbox_archive == DEV_HOME / "inbox"


# ---- settings.json -------------------------------------------------------------------------------------------


def test_settings_json_values_and_precedence(tmp_path: Path) -> None:
    write_json(tmp_path, {
        "hub_url": "https://json.example/", "hub_timeout_s": 12, "hub_ca_bundle": "ca.pem",
        "inbox_dirs": [{"path": str(tmp_path / "in"), "label": "Позиции", "recursive": False},
                       {"path": "sub", "label": "PnL", "recursive": True}],
        "private_adapter": "adapters/tda", "private_adapter_config": str(tmp_path / "cfg"),
        "inbox_poll_seconds": 30, "autopilot": False, "autopilot_sync_last": 3, "snapshot_drop": "drop",
        "unknown_key": {"kept": True}})
    (tmp_path / "file.env").write_text("DESK_HUB_URL=https://file.example\nDESK_HUB_TIMEOUT_S=99\n"
                                       "DESK_INBOX_POLL_SECONDS=7\nDESK_DB=/file/db.sqlite\n", encoding="utf-8")
    e = {"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "file.env")}
    s = load(e)
    assert s.hub_url == "https://json.example" and s.hub_timeout_s == 12  # settings.json > env file
    assert s.db == Path("/file/db.sqlite")  # env file > defaults
    assert s.hub_ca_bundle == tmp_path / "ca.pem" and s.snapshot_drop == tmp_path / "drop"  # relative to home
    assert s.inbox_dirs == (tmp_path / "in", tmp_path / "sub" / "**")
    assert s.private_adapter == str(tmp_path / "adapters" / "tda") and s.private_adapter_config == tmp_path / "cfg"
    assert (s.inbox_poll_seconds, s.autopilot, s.autopilot_sync_last) == (30, False, 3)
    s = load({**e, "DESK_HUB_URL": "https://env.example", "DESK_INBOX_DIRS": "/x;/y/**", "DESK_AUTOPILOT": "on",
              "DESK_HUB_TIMEOUT_S": ""})  # environment > settings.json; empty means unset
    assert s.hub_url == "https://env.example" and s.inbox_dirs == (Path("/x"), Path("/y/**")) and s.autopilot
    assert s.hub_timeout_s == 12
    with pytest.raises(SettingsError, match="DESK_AUTOPILOT"):
        load({**e, "DESK_AUTOPILOT": "maybe"})


def test_settings_json_is_reread_and_checked(tmp_path: Path) -> None:
    assert load(env(tmp_path)).hub_url is None
    write_json(tmp_path, {"hub_url": "https://a.example"})
    assert load(env(tmp_path)).hub_url == "https://a.example"
    write_json(tmp_path, {"hub_timeout_s": "fast", "inbox_dirs": [{"path": ""}]})
    with pytest.raises(SettingsError, match="hub_timeout_s.*inbox_dirs"):
        load(env(tmp_path))
    (tmp_path / "settings.json").write_text("{broken", encoding="utf-8")
    with pytest.raises(SettingsError, match="invalid JSON"):
        load(env(tmp_path))


def test_update_round_trip_keeps_unknown_keys(tmp_path: Path) -> None:
    write_json(tmp_path, {"hub_url": "https://a.example", "custom": [1, 2], "autopilot": True})
    doc = update_settings_json({"hub_url": "https://b.example", "autopilot": None, "hub_token": TOKEN,
                                "inbox_dirs": [{"path": "C:\\Отчёты", "label": "Позиции", "recursive": True}]},
                               tmp_path)
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert TOKEN not in text and "Позиции" in text and text.startswith("{\n  ")  # UTF-8, pretty, no token
    assert read_settings_json(tmp_path) == doc == {"hub_url": "https://b.example", "custom": [1, 2],
                                                   "inbox_dirs": [{"path": "C:\\Отчёты", "label": "Позиции",
                                                                   "recursive": True}]}
    with pytest.raises(SettingsError, match="https"):
        update_settings_json({"hub_url": "http://plain.example"}, tmp_path)
    with pytest.raises(SettingsError, match="autopilot_sync_last"):
        update_settings_json({"autopilot_sync_last": 0}, tmp_path)
    assert read_settings_json(tmp_path) == doc  # nothing written on an error
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    (tmp_path / "settings.json").write_text("{broken", encoding="utf-8")
    assert update_settings_json({"autopilot": False}, tmp_path) == {"autopilot": False}
    assert (tmp_path / "settings.json.broken").read_text(encoding="utf-8") == "{broken"


def test_proxy_exported_from_settings_json(tmp_path: Path, monkeypatch) -> None:
    for v in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    write_json(tmp_path, {"https_proxy": "http://proxy.corp:3128", "no_proxy": "localhost"})
    load()
    assert os.environ["HTTPS_PROXY"] == "http://proxy.corp:3128" and os.environ["NO_PROXY"] == "localhost"
    write_json(tmp_path, {"https_proxy": "http://other:8080"})
    load()
    assert os.environ["HTTPS_PROXY"] == "http://other:8080" and "NO_PROXY" not in os.environ
    load({"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "none.env")})  # a test env: no export
    monkeypatch.setenv("HTTPS_PROXY", "http://real:1")  # the real environment wins
    load()
    assert os.environ["HTTPS_PROXY"] == "http://real:1"
    monkeypatch.delenv("HTTPS_PROXY")
    write_json(tmp_path, {"no_proxy": "localhost"})  # alone it would hide the Windows proxy from urllib: not set
    load()
    assert "NO_PROXY" not in os.environ and "HTTPS_PROXY" not in os.environ
    monkeypatch.setenv("https_proxy", "http://real:2")  # with a proxy of the real environment it is
    load()
    assert os.environ["NO_PROXY"] == "localhost"
    monkeypatch.delenv("https_proxy")
    load()
    assert "NO_PROXY" not in os.environ


def test_proxy_password_is_masked(tmp_path: Path, monkeypatch) -> None:
    from desk.settings import mask_proxy

    assert mask_proxy("http://ivanov:S3cr:et@proxy.corp:3128") == "http://ivanov:***@proxy.corp:3128"
    assert mask_proxy("ivanov:pw@proxy:8080") == "ivanov:***@proxy:8080"
    assert mask_proxy("http://proxy:8080") == "http://proxy:8080" and mask_proxy(None) is None
    for v in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("DESK_HOME", str(tmp_path))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    write_json(tmp_path, {"https_proxy": "http://ivanov:pw123@proxy.corp:3128"})
    assert dict(load().describe())["https_proxy"] == "http://ivanov:***@proxy.corp:3128"  # desk config


def test_bundle_default_adapter_is_the_lowest_layer(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle" / "adapter" / "tda"
    e = env(tmp_path, DESK_DEFAULT_PRIVATE_ADAPTER=str(bundle))
    s = load(e)
    assert s.private_adapter is None and not s.private_adapter_default  # the folder does not exist
    bundle.mkdir(parents=True)
    s = load(e)
    assert s.private_adapter == str(bundle) and s.private_adapter_default
    assert "bundle default" in dict(s.describe())["private_adapter"]
    (tmp_path / "file.env").write_text("DESK_PRIVATE_ADAPTER=/from/file\n", encoding="utf-8")
    s = load({**e, "DESK_ENV_FILE": str(tmp_path / "file.env")})
    assert s.private_adapter == "/from/file" and not s.private_adapter_default
    write_json(tmp_path, {"private_adapter": str(tmp_path / "mine")})
    assert load(e).private_adapter == str(tmp_path / "mine")
    assert load({**e, "DESK_PRIVATE_ADAPTER": "/env/tda"}).private_adapter == "/env/tda"


def test_ctx_inbox_dirs_reach_the_adapter(tmp_path: Path) -> None:
    from desk.inbox import runner
    from desk.inbox.loader import load_adapter

    d = tmp_path / "ctx_dirs_adapter_t"
    d.mkdir()
    desc = {k: v for k, v in DESC.items()}
    (d / "__init__.py").write_text(
        f"SPI_VERSION = 1\n\ndef describe():\n    return {desc!r}\n\n"
        "def match(path, ctx):\n    return {'dirs': list(ctx.inbox_dirs)}\n\n"
        "def read(path, hit, ctx):\n    return {}\n\n"
        "def reference(ctx):\n    return {'books': [], 'dirs': ctx.inbox_dirs}\n", encoding="utf-8")
    dirs = [str(tmp_path / "in"), str(tmp_path / "deep" / "**")]
    h = load_adapter(str(d), None, None, inbox_dirs=dirs)
    assert h.ok and h.inbox_dirs == tuple(dirs)
    assert runner.call_match(h, ["x.csv"], None, 10) == {"x.csv": {"dirs": dirs}}
    assert runner.call_reference(h, None, 10)["dirs"] == dirs
    assert runner.call_reference(load_adapter(str(d), None, None), None, 10)["dirs"] == []
    s = Settings(home=tmp_path, inbox_dirs=(tmp_path / "in",), private_adapter=str(d), private_adapter_config=None)
    assert scan.adapter(s).inbox_dirs == (str(tmp_path / "in"),)
    scan.forget_adapters()


# ---- token -----------------------------------------------------------------------------------------------------


def test_token_file_in_home(tmp_path: Path) -> None:
    path = write_hub_token(f"  {TOKEN}\n", tmp_path)
    assert path == tmp_path / "hub_token" and path.read_text(encoding="utf-8") == TOKEN + "\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    s = load(env(tmp_path))
    assert s.hub_token == TOKEN and s.hub_token_source == f"file {path}" and TOKEN not in repr(s)
    assert load(env(tmp_path, DESK_HUB_TOKEN="mdh_env")).hub_token == "mdh_env"  # the environment wins
    (tmp_path / "file.env").write_text("DESK_HUB_TOKEN=mdh_file\n", encoding="utf-8")
    assert load({"DESK_HOME": str(tmp_path), "DESK_ENV_FILE": str(tmp_path / "file.env")}).hub_token == TOKEN
    with pytest.raises(SettingsError):
        write_hub_token("two words", tmp_path)
    assert write_hub_token("", tmp_path) is None and not path.exists()
    assert load(env(tmp_path)).hub_token is None


# ---- config_files (I2) -------------------------------------------------------------------------------------------


def test_config_files_are_optional_and_never_fatal() -> None:
    assert check_describe(DESC, 1, True) is None and check_config_files(DESC) == ([], [])
    good = {"name": "books.json", "title": "Книги", "help": "h", "required": True, "template": {"books": []}}
    files, warnings = check_config_files({**DESC, "config_files": [good, {"name": "../x.json"}, "junk",
                                                                   {"name": "books.json"}, {"name": "map.yaml",
                                                                                            "required": "yes"}]})
    assert check_describe({**DESC, "config_files": "junk"}, 1, True) is None
    assert [f["name"] for f in files] == ["books.json", "map.yaml"] and files[0] == good
    assert files[1] == {"name": "map.yaml", "title": "map.yaml", "help": "", "required": False, "template": None}
    assert len(warnings) == 4
    assert check_config_files({**DESC, "config_files": {"a": 1}}) == ([], ["config_files: not a list, ignored"])


# ---- API -------------------------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    scan.forget_adapters()
    return h


@pytest.fixture
def client(home: Path) -> TestClient:
    return TestClient(create_app("postgresql://unused"), base_url="http://127.0.0.1:8000",
                      client=("127.0.0.1", 50000))


def adapter_pkg(tmp_path: Path) -> Path:
    d = tmp_path / "cfg_files_adapter_t"
    d.mkdir()
    desc = {**DESC, "config_files": [
        {"name": "books.json", "title": "Книги", "help": "Список книг", "required": True, "template": {"books": []}},
        {"name": "patterns.txt", "title": "Шаблоны", "required": False, "template": None}, {"name": "../bad"}]}
    (d / "__init__.py").write_text(f"SPI_VERSION = 1\n\ndef describe():\n    return {desc!r}\n\n"
                                   "def match(path, ctx):\n    return None\n\n"
                                   "def read(path, hit, ctx):\n    return {}\n\n"
                                   "def reference(ctx):\n    return {'books': []}\n", encoding="utf-8")
    return d


def test_api_settings_round_trip(client: TestClient, home: Path) -> None:
    r = client.get("/api/v1/settings")
    assert r.status_code == 200
    j = r.json()
    assert j["home"] == str(home) and j["hub_token_set"] is False and j["error"] is None
    assert j["effective"]["snapshot_cache"] == str(home / "snapshots") and j["effective"]["autopilot"] is True
    body = {"hub_url": "https://hub.example", "inbox_dirs": [{"path": "/r", "label": "Позиции", "recursive": True}],
            "autopilot_sync_last": 7, "future_key": 1}
    j = client.put("/api/v1/settings", json=body).json()
    assert j["hub_url"] == "https://hub.example" and j["future_key"] == 1
    assert j["effective"]["inbox_dirs"] == [str(Path("/r") / "**")] and j["effective"]["autopilot_sync_last"] == 7
    j = client.put("/api/v1/settings", json={"hub_url": None}).json()
    assert "hub_url" not in j and j["future_key"] == 1 and j["inbox_dirs"][0]["label"] == "Позиции"
    r = client.put("/api/v1/settings", json={"hub_timeout_s": -1})
    assert r.status_code == 422 and "hub_timeout_s" in r.json()["message"]
    assert client.put("/api/v1/settings", json={"hub_token": TOKEN}).status_code == 422
    assert "/api/v1/settings" not in client.get("/openapi.json").json()["paths"]  # local admin routes


def test_api_proxy_password_masked_and_kept(client: TestClient, home: Path, monkeypatch) -> None:
    for v in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(v, raising=False)
    j = client.put("/api/v1/settings", json={"https_proxy": "http://ivanov:pw123@proxy.corp:3128"}).json()
    assert "pw123" not in json.dumps(j) and j["https_proxy"] == "http://ivanov:***@proxy.corp:3128"
    assert j["effective"]["https_proxy"] == "http://ivanov:***@proxy.corp:3128"
    j = client.put("/api/v1/settings", json={**j, "hub_url": "https://hub.example"}).json()  # the page saves all
    stored = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert stored["https_proxy"] == "http://ivanov:pw123@proxy.corp:3128" and stored["hub_url"] == "https://hub.example"
    client.put("/api/v1/settings", json={"https_proxy": "http://ivanov:new@proxy.corp:3128"})  # a new one is saved
    assert json.loads((home / "settings.json").read_text(encoding="utf-8"))["https_proxy"].endswith(":new@proxy.corp:3128")


def test_api_token_never_returned(client: TestClient, home: Path) -> None:
    r = client.put("/api/v1/settings/hub-token", json={"token": TOKEN})
    assert r.status_code == 200 and r.json()["hub_token_set"] is True
    assert (home / "hub_token").read_text(encoding="utf-8").strip() == TOKEN
    for resp in (r, client.get("/api/v1/settings"), client.post("/api/v1/settings/test-hub", json={})):
        assert TOKEN not in resp.text and TOKEN[4:-4] not in resp.text
    assert client.get("/api/v1/settings").json()["hub_token_set"] is True
    j = client.post("/api/v1/settings/test-hub", json={}).json()
    assert j["ok"] is False and "DESK_HUB_URL" in j["error"]  # no hub address: no request made
    assert client.put("/api/v1/settings/hub-token", json={"token": ""}).json()["hub_token_set"] is False
    assert not (home / "hub_token").exists()


def test_api_test_hub_against_stub(client: TestClient, home: Path, monkeypatch) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:
            pass

        def do_GET(self) -> None:
            if self.path.startswith("/health/ready"):
                doc: dict = {"status": "ok", "version": "9.9", "migrations": {"applied": 1, "known": 1}}
            elif self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self.send_response(401)
                self.end_headers()
                return
            else:
                doc = {"data": [{"snapshot_id": "ab" * 32, "business_date": "2026-10-15",
                                 "published_at": "2026-10-16T15:50:00+00:00"}], "meta": {}}
            body = json.dumps(doc).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client.put("/api/v1/settings", json={"hub_url": f"http://127.0.0.1:{srv.server_address[1]}"})
        j = client.post("/api/v1/settings/test-hub", json={}).json()
        assert j["ok"] is False and "token not configured" in j["error"] and j["health"]["version"] == "9.9"
        client.put("/api/v1/settings/hub-token", json={"token": "mdh_wrong"})
        j = client.post("/api/v1/settings/test-hub", json={}).json()
        assert j["ok"] is False and "401" in j["error"] and not j["token_accepted"]
        client.put("/api/v1/settings/hub-token", json={"token": TOKEN})
        j = client.post("/api/v1/settings/test-hub", json={}).json()
        assert j["ok"] and j["token_accepted"] and j["latest"]["business_date"] == "2026-10-15"
        assert j["latest"]["age_hours"] > 0 and TOKEN not in json.dumps(j)
    finally:
        srv.shutdown()


def test_api_adapter_and_config_files(client: TestClient, home: Path, tmp_path: Path) -> None:
    j = client.get("/api/v1/adapter").json()  # the reference adapter: no config_files
    assert j["loaded"] and j["reference"] and j["config_files"] == [] and j["error"] is None
    assert j["config_dir"] == str(home / "adapter-config") and (home / "adapter-config").is_dir()
    client.put("/api/v1/settings", json={"private_adapter": str(adapter_pkg(tmp_path))})
    j = client.get("/api/v1/adapter").json()
    assert j["loaded"] and j["name"] == "cfg_adapter" and j["describe"]["sources"][0]["source"] == "positions_csv"
    assert [(f["name"], f["exists"], f["required"]) for f in j["config_files"]] == \
        [("books.json", False, True), ("patterns.txt", False, False)]
    assert len(j["warnings"]) == 1
    fp = j["fingerprint"]

    assert client.get("/api/v1/adapter/config/books.json").json() == {
        "name": "books.json", "path": str(home / "adapter-config" / "books.json"), "exists": False, "text": None,
        "sha256": None}
    r = client.put("/api/v1/adapter/config/books.json", json={"text": '{"books": [1,'})
    assert r.status_code == 422 and r.json()["details"]["line"] == 1
    assert not (home / "adapter-config" / "books.json").exists()
    r = client.put("/api/v1/adapter/config/books.json", json={"text": '{"books": ["Позиции"]}'})
    assert r.status_code == 200 and r.json()["exists"]
    assert json.loads((home / "adapter-config" / "books.json").read_text(encoding="utf-8")) == {"books": ["Позиции"]}
    assert client.get("/api/v1/adapter").json()["fingerprint"] != fp  # the config folder is fingerprinted
    assert client.put("/api/v1/adapter/config/patterns.txt", json={"text": "a*\n"}).status_code == 200  # declared
    assert client.put("/api/v1/adapter/config/other.json", json={"text": "{}"}).status_code == 200  # simple *.json
    for bad in ("notes.txt", "..json", ".hidden.json", "a b.json", "%2E%2E%2Fescape.json"):
        r = client.put(f"/api/v1/adapter/config/{bad}", json={"text": "{}"})
        assert r.status_code in (404, 405, 422), bad  # 405: the static mount
    assert sorted(p.name for p in (home / "adapter-config").iterdir()) == ["books.json", "other.json", "patterns.txt"]
    assert not list(tmp_path.glob("escape.json")) and not list(home.glob("escape.json"))


def test_api_local_only(home: Path) -> None:
    app = create_app("postgresql://unused")
    remote = TestClient(app, base_url="http://127.0.0.1:8000", client=("10.1.2.3", 50000))
    assert remote.get("/api/v1/settings").status_code == 403
    assert remote.put("/api/v1/settings", json={"autopilot": False}).status_code == 403
    rebound = TestClient(app, base_url="http://evil.example:8000", client=("127.0.0.1", 50000))
    assert rebound.get("/api/v1/settings").status_code == 403
    local = TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 50000))
    assert local.get("/api/v1/settings").status_code == 200
    assert local.put("/api/v1/settings", json={"autopilot": False},
                     headers={"Origin": "https://evil.example"}).status_code == 403
    assert local.put("/api/v1/settings", content=b'{"autopilot": false}',
                     headers={"Content-Type": "text/plain"}).status_code == 415
    assert not (home / "settings.json").exists()


def test_settings_page_is_served(client: TestClient) -> None:
    r = client.get("/settings.html")
    assert r.status_code == 200 and "Настройки" in r.text and "/api/v1/settings" in r.text


def test_static_root_from_environment(home: Path, tmp_path: Path, monkeypatch) -> None:
    """The Windows bundle serves app\\prototypes and app\\frontend\\dist through DESK_STATIC_ROOT (start.bat)."""
    root = tmp_path / "app"
    (root / "prototypes").mkdir(parents=True)
    (root / "prototypes" / "only-here.html").write_text("<p>bundle page</p>", encoding="utf-8")
    (root / "frontend" / "dist").mkdir(parents=True)
    (root / "frontend" / "dist" / "index.html").write_text("<p>bundle app</p>", encoding="utf-8")
    monkeypatch.setenv("DESK_STATIC_ROOT", str(root))
    c = TestClient(create_app("postgresql://unused"))
    assert c.get("/only-here.html").text == "<p>bundle page</p>"
    assert c.get("/app/").text == "<p>bundle app</p>"
    assert c.get("/settings.html").status_code == 404  # not the checkout's prototypes
    monkeypatch.delenv("DESK_STATIC_ROOT")
    assert TestClient(create_app("postgresql://unused")).get("/settings.html").status_code == 200


def test_api_proxy_login_password_and_transport(client: TestClient, home: Path) -> None:
    pw = "Pr0xy-secret pw"
    j = client.put("/api/v1/settings", json={"proxy_user": "CORP\\ivanov", "hub_transport": "curl"}).json()
    assert j["proxy_user"] == "CORP\\ivanov" and j["effective"]["hub_transport"] == "curl"
    assert j["proxy_password_set"] is False
    r = client.put("/api/v1/settings/proxy-password", json={"password": pw})
    assert r.status_code == 200 and r.json() == {"proxy_password_set": True}
    assert (home / "proxy_password").read_text(encoding="utf-8") == pw + "\n"
    j = client.get("/api/v1/settings").json()
    assert j["proxy_password_set"] is True and j["effective"]["proxy_password_set"] is True and pw not in json.dumps(j)
    st = load(env(home))
    assert st.proxy_password == pw and st.proxy_user == "CORP\\ivanov" and pw not in repr(st)
    assert pw not in str(st.describe())
    assert client.put("/api/v1/settings", json={"proxy_password": pw}).status_code == 422
    r = client.put("/api/v1/settings", json={"hub_transport": "wget"})
    assert r.status_code == 422 and "hub_transport" in r.json()["message"]
    assert client.put("/api/v1/settings/proxy-password", json={"password": ""}).json()["proxy_password_set"] is False
    assert not (home / "proxy_password").exists()
    assert load(env(home)).hub_transport == "curl" and Settings().hub_transport == "auto"  # default
