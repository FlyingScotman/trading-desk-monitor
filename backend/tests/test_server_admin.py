"""The server from the settings page («Доступ из сети», desk.api.server_routes, desk.restart): the state, a restart,
the network access through the same changes as ``desk lan enable`` / ``disable``, user deletion, who may do it,
the audit, and the guards against locking the owner out; the launcher's side (backend/windows/deskctl.py): the
rollback of a network change that does not start, and end to end on a fake bundle with the real launcher and API."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk import cli, restart
from desk.api import auth, create_app

REPO = Path(__file__).resolve().parents[2]
PW = {"boss": "admin-pass-1", "tom": "trader-pass-1", "ann": "admin-pass-2"}
ROLE = {"boss": "admin", "tom": "trader", "ann": "admin"}
LOOP = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
LAN = {"base_url": "http://desk.example:8100", "client": ("10.1.2.3", 50000)}
ON = auth.AuthConfig(enabled=True, cache_s=0)


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


deskctl = _load("deskctl_r", REPO / "backend/windows/deskctl.py")


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    return h


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def add_users(url: str, *names: str) -> None:
    with psycopg.connect(url, autocommit=True) as con:
        for n in names:
            auth.add_user(con, n, ROLE[n], PW[n], actor="test")


def audit_actions(url: str) -> list[tuple]:
    with psycopg.connect(url, autocommit=True) as con:
        return con.execute("SELECT action, actor, ok, detail FROM audit_log ORDER BY audit_id").fetchall()


def settings_doc(home: Path) -> dict:
    p = home / "settings.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def served(url: str, cfg: auth.AuthConfig | None = None, *, port: int = 8100, under_launcher: bool = True,
           where: dict = LOOP) -> tuple[TestClient, list]:
    """A client of an app as ``desk serve`` sets it up: what it listens on and, under the launcher, a restart."""
    app = create_app(url, auth=cfg)
    app.state.listen = {"host": "127.0.0.1", "port": port, "tls": False}
    asked: list = []
    if under_launcher:
        app.state.request_restart = lambda: asked.append(True)
    return TestClient(app, **where), asked


def test_network_access_from_the_page(store_url, home: Path, monkeypatch):
    c, asked = served(store_url)
    s = c.get("/api/v1/server").json()
    assert s["can_restart"] and s["configured"]["lan"] is False and s["running"]["port"] == 8100
    assert s["restart_needed"] == [] and s["admins"] == [] and s["client"]["may_change_network"]
    assert s["firewall"] is None and s["local_url"] == "http://127.0.0.1:8100/"

    # no admin yet: sign-in would go on with nobody to sign in, so nothing is written
    port = free_port()
    r = c.post("/api/v1/server/network", json={"lan": True, "https": True, "port": port})
    assert r.status_code == 409 and "администратор" in r.json()["message"]
    assert settings_doc(home) == {} and restart.pending(home) is None and asked == []

    add_users(store_url, "boss")
    r = c.post("/api/v1/server/network", json={"lan": True, "https": True, "port": port})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["restarting"] and asked == [True] and j["sign_in_needed"] and j["configured"]["https"]
    assert j["local_url"] == f"https://127.0.0.1:{port}/" and j["urls"]
    doc = settings_doc(home)
    assert doc["auth"] is True and doc["bind_host"] == "0.0.0.0" and doc["port"] == port
    assert Path(doc["tls_cert"]).is_file() and Path(doc["tls_key"]).is_file()
    staged = restart.pending(home)  # the way back for the launcher
    assert staged["previous"] == {"auth": None, "bind_host": None, "port": None, "tls_cert": None, "tls_key": None,
                                  "allow_plain_http": None}
    assert staged["expect"] == {"host": "0.0.0.0", "port": port, "https": True}
    s = c.get("/api/v1/server").json()
    assert set(s["restart_needed"]) == {"bind_host", "port", "https", "auth"} and s["admins"] == ["boss"]
    assert str(port) in s["firewall"]["add"] and "netsh advfirewall firewall add rule" in s["firewall"]["add"]
    assert s["cert"]["exists"] and s["pending"]
    acts = audit_actions(store_url)
    net = [a for a in acts if a[0] == "network_change"]
    assert len(net) == 1 and net[0][1] == "local" and net[0][2] and net[0][3]["port"] == port

    # applied again before the restart: the way back stays the original settings
    r = c.post("/api/v1/server/network", json={"lan": True, "https": True, "port": port})
    assert r.status_code == 200 and restart.pending(home)["previous"]["bind_host"] is None
    restart.drop_network_change(home)  # as the launcher does once the new server answers

    # the same certificate unless asked for a new one; a new one backs up the old files for the launcher
    before = Path(doc["tls_cert"]).read_bytes()
    r = c.post("/api/v1/server/network", json={"lan": True, "https": True, "port": port})
    assert r.status_code == 200 and Path(doc["tls_cert"]).read_bytes() == before
    restart.drop_network_change(home)
    r = c.post("/api/v1/server/network", json={"lan": True, "https": True, "port": port, "regenerate_cert": True})
    assert r.status_code == 200 and Path(doc["tls_cert"]).read_bytes() != before
    files = restart.pending(home)["files"]
    assert {Path(f["target"]).name for f in files} == {"desk.crt", "desk.key"}
    assert Path(next(f for f in files if f["target"].endswith("desk.crt"))["backup"]).read_bytes() == before

    # plain HTTP, a port another program holds, a key the environment sets, a non-JSON write
    r = c.post("/api/v1/server/network", json={"lan": True, "https": False, "port": port})
    assert r.status_code == 200 and settings_doc(home)["allow_plain_http"] is True and "tls_cert" not in settings_doc(home)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        r = c.post("/api/v1/server/network", json={"lan": True, "https": False, "port": taken})
        assert r.status_code == 409 and "занят" in r.json()["message"] and settings_doc(home)["port"] == port
    r = c.post("/api/v1/server/network", json={"lan": True, "host": "203.0.113.77", "https": False})
    assert r.status_code == 409 and "адреса" in r.json()["message"]
    r = c.post("/api/v1/server/network", json={"lan": True, "host": "127.0.0.1"})
    assert r.status_code == 422
    monkeypatch.setenv("DESK_PORT", str(port))
    r = c.post("/api/v1/server/network", json={"lan": True, "https": False, "port": free_port()})
    assert r.status_code == 409 and "port" in r.json()["message"]
    assert "port" in c.get("/api/v1/server").json()["locked"]
    monkeypatch.delenv("DESK_PORT")
    assert c.post("/api/v1/server/network", content=b"lan=1",
                  headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415

    # back to this computer only, sign-in off too
    restart.drop_network_change(home)
    r = c.post("/api/v1/server/network", json={"lan": False, "auth_off": True})
    assert r.status_code == 200 and settings_doc(home)["bind_host"] == "127.0.0.1" and settings_doc(home)["auth"] is False
    assert restart.pending(home)["previous"]["bind_host"] == "0.0.0.0"


def test_restart_and_what_runs_without_the_launcher(store_url, home: Path):
    c, asked = served(store_url)
    r = c.post("/api/v1/server/restart", json={})
    assert r.status_code == 200 and r.json()["restarting"] and asked == [True]
    assert [a[0] for a in audit_actions(store_url)] == ["server_restart"]
    assert c.post("/api/v1/server/restart", content=b"", headers={"Content-Type": "text/plain"}).status_code == 415

    c, asked = served(store_url, under_launcher=False)  # a checkout or desk serve by hand: nobody starts it again
    s = c.get("/api/v1/server").json()
    assert not s["can_restart"] and "start.bat" in s["unavailable"]
    assert c.post("/api/v1/server/restart", json={}).status_code == 409
    port = free_port()
    r = c.post("/api/v1/server/network", json={"lan": False, "port": port})
    assert r.status_code == 200 and not r.json()["restarting"] and "перезапуска" in r.json()["message"]
    assert settings_doc(home)["port"] == port and restart.pending(home) is None  # no launcher to confirm it
    assert c.get("/api/v1/server").json()["restart_needed"] == ["port"]

    (home / "restart").mkdir(exist_ok=True)
    (home / "restart/last.json").write_text(json.dumps({"kind": "network", "ok": False, "message": "порт занят"}))
    assert c.get("/api/v1/server").json()["last"]["message"] == "порт занят"


def test_who_may_restart_or_change_the_network(store_url, home: Path):
    add_users(store_url, "boss", "tom")
    off_lan, asked = served(store_url, where=LAN)  # sign-in off: this computer only
    assert off_lan.get("/api/v1/server").status_code == 403
    assert off_lan.post("/api/v1/server/restart", json={}).status_code == 403 and asked == []

    def signed(name: str, cfg: auth.AuthConfig, where: dict) -> tuple[TestClient, list]:
        c, asked = served(store_url, cfg, where=where)
        assert c.post("/api/v1/auth/login", json={"username": name, "password": PW[name]}).status_code == 200
        return c, asked

    tom, asked = signed("tom", ON, LOOP)
    csrf = {"X-CSRF-Token": tom.cookies["desk_csrf"]}
    assert tom.get("/api/v1/server").status_code == 403
    assert tom.post("/api/v1/server/restart", json={}, headers=csrf).status_code == 403 and asked == []

    boss, asked = signed("boss", ON, LAN)  # admin_from_network on (the default): from another computer too
    csrf = {"X-CSRF-Token": boss.cookies["desk_csrf"]}
    assert boss.post("/api/v1/server/restart", json={}).status_code == 403  # no CSRF token
    assert boss.post("/api/v1/server/restart", json={}, headers=csrf).status_code == 200 and asked == [True]
    s = boss.get("/api/v1/server").json()
    assert s["client"] == {"loopback": False, "may_change_network": True}

    strict = auth.AuthConfig(enabled=True, cache_s=0, admin_from_network=False)
    boss, asked = signed("boss", strict, LAN)
    csrf = {"X-CSRF-Token": boss.cookies["desk_csrf"]}
    assert boss.post("/api/v1/server/network", json={"lan": False}, headers=csrf).status_code == 403
    assert boss.post("/api/v1/server/restart", json={}, headers=csrf).status_code == 403 and asked == []
    boss, asked = signed("boss", strict, LOOP)  # on the computer itself it works
    csrf = {"X-CSRF-Token": boss.cookies["desk_csrf"]}
    assert boss.post("/api/v1/server/restart", json={}, headers=csrf).status_code == 200 and asked == [True]
    assert [a[1] for a in audit_actions(store_url) if a[0] == "server_restart"] == ["boss", "boss"]


def test_delete_users_but_never_the_last_admin_or_oneself(store_url, home: Path, monkeypatch, capsys):
    add_users(store_url, "boss", "tom")
    c, _ = served(store_url, ON)
    assert c.post("/api/v1/auth/login", json={"username": "boss", "password": PW["boss"]}).status_code == 200
    h = {"X-CSRF-Token": c.cookies["desk_csrf"], "Content-Type": "application/json"}
    assert c.delete("/api/v1/users/boss", headers=h).status_code == 409  # oneself
    assert c.delete("/api/v1/users/nobody", headers=h).status_code == 404
    assert c.delete("/api/v1/users/tom", headers={"Content-Type": "application/json"}).status_code == 403  # CSRF
    assert c.delete("/api/v1/users/tom", headers=h).status_code == 204
    assert [u["username"] for u in c.get("/api/v1/users").json()["users"]] == ["boss"]
    acts = audit_actions(store_url)
    assert ("user_delete", "boss", True) == acts[-1][:3]
    with psycopg.connect(store_url, autocommit=True) as con, pytest.raises(auth.AuthError, match="последний"):
        auth.delete_user(con, "boss", actor="someone")
    monkeypatch.setenv("DESK_STORE_URL", store_url)
    monkeypatch.setattr("sys.stdin", io.StringIO("admin-pass-2\n"))
    assert cli.main(["user", "add", "ann", "--role", "admin", "--password-stdin"]) == 0
    assert cli.main(["user", "delete", "boss"]) == 0 and "deleted" in capsys.readouterr().out
    assert cli.main(["user", "delete", "ann"]) == 1 and "последний" in capsys.readouterr().err


def test_cli_and_page_share_the_changes(home: Path):
    from desk import lan
    from desk.settings import load

    st = load()
    with pytest.raises(lan.LanError) as e:
        lan.enable_changes(st, has_admin=False, tls="plain_http")
    assert e.value.exit == 1 and "first admin" in e.value.en and "администратор" in e.value.ru
    with pytest.raises(lan.LanError) as e:
        lan.enable_changes(st, has_admin=True)  # neither HTTPS nor HTTP chosen, none set
    assert e.value.exit == 2
    changes, _ = lan.enable_changes(st, has_admin=True, tls="plain_http", port=8443, admin_from_network=False)
    assert changes == {"auth": True, "bind_host": "0.0.0.0", "port": 8443, "allow_plain_http": True, "tls_cert": None,
                       "tls_key": None, "admin_from_network": False}
    assert lan.disable_changes() == {"bind_host": "127.0.0.1"}
    assert lan.disable_changes(True) == {"bind_host": "127.0.0.1", "auth": False}
    assert cli.firewall_command(8443) == lan.firewall_add_command(8443)
    assert lan.listen_problem("127.0.0.1", free_port()) is None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        p = s.getsockname()[1]
        assert "занят" in lan.listen_problem("127.0.0.1", p)
        assert lan.listen_problem("127.0.0.1", p, current={"port": p}) is None  # this server's own port


# ---- the launcher ------------------------------------------------------------------------------------------------


def test_launcher_rolls_back_a_network_change_that_does_not_start(tmp_path: Path):
    home = tmp_path / "home"
    (home / "tls").mkdir(parents=True)
    env = {"DESK_HOME": str(home)}
    crt, key = home / "tls/desk.crt", home / "tls/desk.key"
    crt.write_text("old cert")
    key.write_text("old key")
    (home / "settings.json").write_text(json.dumps({"hub_url": "https://hub", "port": 8100}))
    assert not deskctl.rollback_network(env, "x")  # nothing staged, nothing known to be good

    files = restart.backup_files(home, [crt, key, home / "tls/none.pem"])
    restart.stage_network_change(home, {"port": 8100, "bind_host": None, "auth": None}, files=files,
                                 expect={"host": "0.0.0.0", "port": 8443, "https": True})
    (home / "settings.json").write_text(json.dumps({"hub_url": "https://hub", "port": 8443, "bind_host": "0.0.0.0",
                                                    "auth": True}))
    crt.write_text("new cert")
    assert deskctl.network_pending(env)["expect"]["port"] == 8443
    assert deskctl.rollback_network(env, "сервер не запустился, код 3")
    assert json.loads((home / "settings.json").read_text()) == {"hub_url": "https://hub", "port": 8100}
    assert crt.read_text() == "old cert" and key.read_text() == "old key"
    last = restart.last(home)
    assert last["kind"] == "network" and not last["ok"] and "код 3" in last["message"] and "прежние" in last["message"]
    assert deskctl.network_pending(env) is None and restart.pending(home) is None

    # a good start is remembered; a plain restart that then fails goes back to it
    deskctl.save_good(env)
    (home / "settings.json").write_text(json.dumps({"hub_url": "https://hub", "port": 9999}))
    assert deskctl.rollback_network(env, "сервер не ответил")
    assert json.loads((home / "settings.json").read_text())["port"] == 8100
    assert not deskctl.rollback_network(env, "again")  # already the good one: the caller reports the failure

    restart.stage_network_change(home, {"port": 8100}, expect={"host": "127.0.0.1", "port": 8200, "https": False})
    deskctl.confirm_network(env, "http://127.0.0.1:8200/settings.html")
    assert restart.pending(home) is None and restart.last(home)["ok"] and "127.0.0.1:8200" in restart.last(home)["message"]


@pytest.mark.parametrize("ok", [True, False])
def test_the_launcher_drops_the_staged_change_before_it_records_the_outcome(tmp_path: Path, monkeypatch, ok: bool):
    """TDM#451: the page reads the outcome, then the staged change (server_state); the launcher drops the change
    first, so a confirmed or rolled back change is never shown as still pending."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"DESK_HOME": str(home)}
    (home / "settings.json").write_text(json.dumps({"port": 8200}))
    restart.stage_network_change(home, {"port": 8100}, expect={"host": "127.0.0.1", "port": 8200, "https": False})
    seen: list[bool] = []
    write = deskctl._write_json

    def spy(path: Path, doc: dict) -> None:
        if path.name == "last.json":
            seen.append(restart.pending(home) is None)
        write(path, doc)

    monkeypatch.setattr(deskctl, "_write_json", spy)
    if ok:
        deskctl.confirm_network(env, "http://127.0.0.1:8200/settings.html")
    else:
        assert deskctl.rollback_network(env, "сервер не запустился, код 3")
    assert seen == [True] and restart.last(home)["ok"] is ok and restart.pending(home) is None


STUB = """#!{python}
import sys
if sys.argv[1] == "db-migrate":
    sys.exit(0)
import json, os
from pathlib import Path
from desk.api.app import serve
doc = json.loads((Path(os.environ["DESK_HOME"]) / "settings.json").read_text(encoding="utf-8"))
sys.exit(serve("postgresql://unused", host=doc.get("bind_host") or "127.0.0.1",
               port=int(sys.argv[sys.argv.index("--port") + 1])))
"""


@pytest.mark.skipif(os.name == "nt", reason="the launcher's process checks are tested on Linux")
def test_restart_from_the_page_end_to_end(tmp_path: Path):
    """start.bat's launcher on a fake bundle whose venv runs the real API without a store: «Перезапустить сервер»
    starts a new server; a new port from «Доступ из сети» is taken up; a port that turns out taken is rolled back."""
    version = "0.1.0-20261022-ddddddd"
    root = tmp_path / "DeskMonitor"
    (root / "app").mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n")
    (root / "app/deskctl.py").write_bytes((REPO / "backend/windows/deskctl.py").read_bytes())
    scripts = root / "env" / "bin"
    scripts.mkdir(parents=True)
    (scripts / "desk").write_text(STUB.format(python=sys.executable))
    (scripts / "desk").chmod(0o755)
    (root / "env/desk-bundle.json").write_text(json.dumps({"version": version, "root": str(root.resolve())}))
    home, port = tmp_path / "home", free_port()
    home.mkdir()
    (home / "settings.json").write_text(json.dumps({"port": port}))
    env = {k: v for k, v in os.environ.items() if not k.startswith("DESK_")}
    env.update(DESK_HOME=str(home), DESK_ENV_FILE=str(tmp_path / "none.env"), BROWSER="true")
    with open(tmp_path / "launcher.log", "wb") as log:
        proc = subprocess.Popen([sys.executable, "-I", str(root / "app/deskctl.py"), "start"], env=env, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(p: int, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{p}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=30) as r:
            return json.loads(r.read())

    def until(p: int, cond, what: str, timeout: float = 90.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                j = call(p, "GET", "/api/v1/server")
                if cond(j):
                    return j
            except (OSError, ValueError):
                pass
            assert proc.poll() is None, (tmp_path / "launcher.log").read_text(errors="replace")
            time.sleep(0.5)
        raise AssertionError(f"{what}:\n" + (tmp_path / "launcher.log").read_text(errors="replace"))

    try:
        first = until(port, lambda j: j["can_restart"], "the server did not start")
        r = call(port, "POST", "/api/v1/server/restart", {})
        assert r["restarting"]
        j = until(port, lambda j: j["instance"] != first["instance"] and j["last"], "no restart")
        assert j["last"]["kind"] == "restart" and j["last"]["ok"]

        new = free_port()
        r = call(port, "POST", "/api/v1/server/network", {"lan": False, "port": new})
        assert r["restarting"] and r["local_url"] == f"http://127.0.0.1:{new}/"
        j = until(new, lambda j: j["running"]["port"] == new and j["last"]["kind"] == "network",
                  "the new port was not taken up")  # the launcher confirms once it sees the server answer
        assert j["last"]["kind"] == "network" and j["last"]["ok"] and not j["pending"] and j["restart_needed"] == []

        third = free_port()
        r = call(new, "POST", "/api/v1/server/network", {"lan": False, "port": third})
        assert r["restarting"]
        with socket.socket() as squatter:  # another program takes the port meanwhile: the new server cannot start
            squatter.bind(("127.0.0.1", third))
            squatter.listen()
            j = until(new, lambda j: j["last"]["kind"] == "network" and not j["last"]["ok"], "no rollback", 120)
        assert j["running"]["port"] == new and not j["pending"] and "прежние" in j["last"]["message"]
        assert json.loads((home / "settings.json").read_text())["port"] == new
    finally:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
