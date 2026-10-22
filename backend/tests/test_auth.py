"""Users, sign-in and network access (batch 1, item 9; desk.api.auth, migration 0015): sign-in off keeps the old
loopback behaviour; on, the access matrix by role, CSRF, the lockout, session expiry and revocation, the first-admin
setup, the audit log, the refusal to listen on the network without sign-in, the CLI and the launcher's helpers."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import socket
import ssl
from datetime import timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from desk import cli, store
from desk.api import auth, create_app
from desk.api.app import listen_sockets

REPO = Path(__file__).resolve().parents[2]
PW = {"boss": "admin-pass-1", "tom": "trader-pass-1", "vic": "viewer-pass-1"}
ROLE = {"boss": "admin", "tom": "trader", "vic": "viewer"}
ISIN = "RU000A1038V6"
LAN = {"base_url": "http://desk.example:8100", "client": ("10.1.2.3", 50000)}
LOOP = {"base_url": "http://127.0.0.1:8100", "client": ("127.0.0.1", 50000)}
ON = auth.AuthConfig(enabled=True, cache_s=0)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    return h


def add_users(url: str, *names: str) -> None:
    with psycopg.connect(url, autocommit=True) as con:
        for n in names or tuple(PW):
            auth.add_user(con, n, ROLE[n], PW[n], actor="test")


def sql(url: str, q: str, args=()) -> list:
    with psycopg.connect(url, autocommit=True) as con:
        cur = con.execute(q, args or None)
        return cur.fetchall() if cur.description else []


def login(c: TestClient, name: str, password: str | None = None, **kw):
    return c.post("/api/v1/auth/login", json={"username": name, "password": password or PW[name]}, **kw)


def signed_in(url: str, name: str, cfg: auth.AuthConfig = ON, where: dict = LAN) -> TestClient:
    c = TestClient(create_app(url, auth=cfg), **where)
    r = login(c, name)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["username"] == name and c.cookies.get("desk_session") and c.cookies.get("desk_csrf")
    return c


def csrf(c: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": c.cookies["desk_csrf"]}


def alias(c: TestClient, value: str, headers: dict | None = None):
    return c.put("/api/v1/instruments/alias", json={"isin": ISIN, "alias": value}, headers=headers)


# ---- passwords, settings, binding --------------------------------------------------------------------------------


def test_passwords_are_argon2id_hashes_and_checked():
    h = auth.hash_password("correct horse")
    assert h.startswith("$argon2id$") and "correct horse" not in h
    assert auth.verify_password(h, "correct horse") and not auth.verify_password(h, "wrong")
    assert not auth.verify_password("not a hash", "x")
    for bad in ("short", "ivanov", "x" * 300):
        with pytest.raises(auth.AuthError):
            auth.check_password("ivanov", bad)
    for bad in ("", "Иванов", "a b", "-x", "x" * 65):
        with pytest.raises(auth.AuthError):
            auth.normalize_username(bad)
    assert auth.normalize_username(" I.Ivanov ") == "i.ivanov"


def test_settings_keys_and_defaults(home: Path):
    from desk.settings import Settings, SettingsError, load, update_settings_json

    st = load()
    assert (st.auth, st.bind_host, st.port, st.tls_cert, st.allow_plain_http, st.admin_from_network) == \
        (False, "127.0.0.1", 8100, None, False, True)
    assert (st.session_idle_minutes, st.session_max_hours, st.login_max_failures, st.login_lockout_minutes) == \
        (480, 24, 5, 15)
    cfg = auth.AuthConfig.from_settings(Settings(auth=True, bind_host="0.0.0.0"))
    assert cfg.enabled and cfg.plain_http_lan and not cfg.secure_cookies and cfg.lockout == timedelta(minutes=15)
    update_settings_json({"auth": True, "bind_host": "0.0.0.0", "port": 8443, "tls_cert": "tls/a.crt",
                          "tls_key": "tls/a.key"}, home)
    st = load()
    assert st.auth and st.port == 8443 and st.tls_cert == home / "tls" / "a.crt"
    assert not auth.AuthConfig.from_settings(st).plain_http_lan and auth.AuthConfig.from_settings(st).secure_cookies
    for bad in ({"port": 70000}, {"tls_key": None}, {"bind_host": "a b"}, {"auth": "yes"}):
        with pytest.raises(SettingsError):
            update_settings_json(bad, home)


def test_listening_on_the_network_needs_sign_in_and_https_or_an_explicit_http():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert auth.check_bind(host, auth=False, tls=False, allow_plain_http=False) == []
    for host in ("0.0.0.0", "10.1.2.3", "desk-pc"):
        with pytest.raises(auth.BindError, match="вход по паролю выключен"):
            auth.check_bind(host, auth=False, tls=True, allow_plain_http=True)
    with pytest.raises(auth.BindError, match="HTTPS"):
        auth.check_bind("0.0.0.0", auth=True, tls=False, allow_plain_http=False)
    assert auth.check_bind("0.0.0.0", auth=True, tls=True, allow_plain_http=False) == []
    assert "без шифрования" in auth.check_bind("0.0.0.0", auth=True, tls=False, allow_plain_http=True)[0]


def test_desk_serve_refuses_a_network_address_without_sign_in(home: Path, monkeypatch, capsys):
    def no_store(*_a, **_k):
        raise AssertionError("the store must not be touched")

    monkeypatch.setattr(store, "resolve_url", no_store)
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 2
    assert "вход по паролю выключен" in capsys.readouterr().err
    (home / "settings.json").write_text(json.dumps({"bind_host": "0.0.0.0"}), encoding="utf-8")
    assert cli.main(["serve"]) == 2  # the setting, not only the flag
    (home / "settings.json").write_text(json.dumps({"bind_host": "0.0.0.0", "auth": True}), encoding="utf-8")
    assert cli.main(["serve"]) == 2 and "HTTPS" in capsys.readouterr().err  # no TLS, no allow_plain_http


def test_listen_sockets_bind_the_address():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    socks = listen_sockets("127.0.0.1", port)
    try:
        assert [x.getsockname() for x in socks] == [("127.0.0.1", port)]
        socks[0].listen()  # as the server does
        with pytest.raises(OSError):
            listen_sockets("127.0.0.1", port)  # taken
    finally:
        for x in socks:
            x.close()


def test_self_signed_certificate_loads(tmp_path: Path):
    crt, key = auth.self_signed_cert(tmp_path / "tls", ["desk-pc", "10.1.2.3"])
    ssl.create_default_context(ssl.Purpose.CLIENT_AUTH).load_cert_chain(crt, key)
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(crt.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "desk-pc" in san.get_values_for_type(x509.DNSName)
    assert {str(i) for i in san.get_values_for_type(x509.IPAddress)} == {"10.1.2.3", "127.0.0.1"}
    if os.name != "nt":
        assert key.stat().st_mode & 0o077 == 0


# ---- sign-in off: as before ---------------------------------------------------------------------------------------


def test_sign_in_off_keeps_this_computer_only(store_url, home: Path):
    app = create_app(store_url)
    local, remote = TestClient(app, **LOOP), TestClient(app, **LAN)
    s = local.get("/api/v1/auth/state").json()
    assert not s["enabled"] and s["user"]["username"] == "local" and s["setup_needed"] and s["setup_allowed"]
    assert remote.get("/api/v1/auth/state").json()["user"] is None
    assert remote.get("/api/v1/runs").status_code == 200  # reads open, as before
    assert remote.get("/api/v1/settings").status_code == 403 and remote.get("/api/v1/users").status_code == 403
    assert alias(remote, "x").status_code == 403
    assert TestClient(app).post("/api/v1/scenario-jobs", json={}).status_code == 422  # open, as before
    assert local.post("/api/v1/auth/login", json={"username": "a", "password": "b"}).status_code == 409
    # users are managed from this computer before sign-in is turned on
    r = local.post("/api/v1/users", json={"username": "boss", "role": "admin", "password": PW["boss"]})
    assert r.status_code == 201 and r.json()["must_change_password"]
    assert local.put("/api/v1/settings", json={"autopilot": False}).status_code == 200
    assert alias(local, "Мой").json()["changed_by"] == "local"
    rows = sql(store_url, "SELECT actor, action, target, detail FROM audit_log ORDER BY audit_id")
    assert [(a, b, c) for a, b, c, _ in rows] == [("local", "user_add", "boss"),
                                                  ("local", "settings", "/api/v1/settings"),
                                                  ("local", "alias", ISIN)]
    assert rows[1][3] == {"method": "PUT", "status": 200, "keys": ["autopilot"]}  # key names, no values
    assert local.get("/login.html").status_code == 200


# ---- sign-in on ---------------------------------------------------------------------------------------------------


def test_without_a_session_only_the_login_is_open(store_url):
    c = TestClient(create_app(store_url, auth=ON), follow_redirects=False, **LAN)
    assert c.get("/health/live").json() == {"status": "ok"}
    assert c.get("/login.html").status_code == 200
    s = c.get("/api/v1/auth/state").json()
    assert s["enabled"] and s["user"] is None and s["setup_needed"] and not s["setup_allowed"]
    for path in ("/api/v1/runs", "/health/ready", "/desk-data.js", "/concepts-data.js", "/api/v1/settings",
                 "/concepts/index.html", "/app/", "/desk-nav.js", "/openapi.json"):
        r = c.get(path)
        assert r.status_code == 401 and r.json()["code"] == "unauthorized", path
    page = c.get("/concepts/c01-cockpit.html?run_id=3", headers={"Accept": "text/html,*/*"})
    assert page.status_code == 303 and page.headers["location"] == "/login.html?next=/concepts/c01-cockpit.html%3Frun_id%3D3"
    assert c.post("/api/v1/scenario-jobs", json={}).status_code == 401
    assert alias(c, "x").status_code == 401
    assert c.post("/api/v1/auth/logout").status_code == 401


def test_access_by_role(store_url, home: Path):
    add_users(store_url)
    who = {n: signed_in(store_url, n) for n in PW}
    for n, c in who.items():
        assert c.get("/api/v1/runs").status_code == 200, n
        assert c.get("/api/v1/auth/state").json()["user"] == {"username": n, "role": ROLE[n], "display_name": None,
                                                                 "must_change_password": False}
    expect = {  # path -> the roles that pass
        ("GET", "/api/v1/settings"): {"boss"}, ("GET", "/api/v1/users"): {"boss"}, ("GET", "/api/v1/audit"): {"boss"},
        ("GET", "/api/v1/update"): {"boss"}, ("GET", "/api/v1/backfill/status"): {"boss"},
        ("PUT", "/api/v1/instruments/alias"): {"boss", "tom"}, ("POST", "/api/v1/scenario-jobs"): {"boss", "tom"},
        ("POST", "/api/v1/autopilot/run"): {"boss", "tom"},
    }
    body = {("PUT", "/api/v1/instruments/alias"): {"isin": ISIN, "alias": "A"}}
    for (method, path), allowed in expect.items():
        for n, c in who.items():
            r = c.request(method, path, json=body.get((method, path), {}), headers=csrf(c))
            if n in allowed:  # through the guard: the route's own answer (409: no autopilot thread, 422: empty job)
                assert r.status_code in (200, 409, 422), (n, path, r.text)
            else:
                assert r.status_code == 403 and r.json()["code"] == "forbidden", (n, path, r.text)
    assert sql(store_url, "SELECT changed_by FROM instrument_alias_rev ORDER BY rev_id") == [("boss",)]  # then same


def test_admin_pages_from_the_network_can_be_turned_off(store_url, home: Path):
    add_users(store_url, "boss")
    cfg = auth.AuthConfig(enabled=True, cache_s=0, admin_from_network=False)
    assert signed_in(store_url, "boss", cfg).get("/api/v1/settings").status_code == 403
    assert signed_in(store_url, "boss", cfg, LOOP).get("/api/v1/settings").status_code == 200
    assert signed_in(store_url, "boss", cfg).get("/api/v1/runs").status_code == 200


def test_writes_need_the_csrf_token_and_this_site(store_url):
    add_users(store_url, "tom")
    c = signed_in(store_url, "tom")
    for headers in (None, {"X-CSRF-Token": "forged"}, {**csrf(c), "Origin": "https://evil.example"},
                    {**csrf(c), "Origin": "null"}, {**csrf(c), "Referer": "http://evil.example/page"}):
        r = alias(c, "x", headers)
        assert r.status_code == 403 and r.json()["code"] == "forbidden", headers
    assert alias(c, "x", {**csrf(c), "Origin": "http://desk.example:8100"}).status_code == 200
    assert alias(c, "y", csrf(c)).status_code == 200  # no Origin: not a browser
    form = c.put("/api/v1/instruments/alias", content="isin=x", headers={**csrf(c), "Content-Type": "text/plain"})
    assert form.status_code == 415
    fresh = TestClient(create_app(store_url, auth=ON), **LAN)
    assert login(fresh, "tom", headers={"Origin": "https://evil.example"}).status_code == 403  # login CSRF
    assert fresh.post("/api/v1/auth/login", content=json.dumps({"username": "tom", "password": PW["tom"]}),
                      headers={"Content-Type": "text/plain"}).status_code == 415
    cookie = c.cookies.jar
    attrs = {k.name: k for k in cookie}
    assert attrs["desk_session"].has_nonstandard_attr("HttpOnly") and not attrs["desk_csrf"].has_nonstandard_attr("HttpOnly")
    raw = login(TestClient(create_app(store_url, auth=ON), **LAN), "tom").headers.get_list("set-cookie")
    assert all("samesite=strict" in h.lower() and "secure" not in h.lower() for h in raw)
    https = TestClient(create_app(store_url, auth=auth.AuthConfig(enabled=True, secure_cookies=True)),
                       base_url="https://desk.example", client=("10.1.2.3", 1))
    assert all("secure" in h.lower() for h in login(https, "tom").headers.get_list("set-cookie"))


def test_lockout_after_wrong_passwords(store_url):
    add_users(store_url, "tom")
    app = create_app(store_url, auth=auth.AuthConfig(enabled=True, cache_s=0, max_failures=3, ip_max_failures=50))
    c = TestClient(app, **LAN)
    for _ in range(2):
        r = login(c, "tom", "wrong-password")
        assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    r = login(c, "tom", "wrong-password")
    assert r.status_code == 429 and r.json()["code"] == "locked" and "заблокирован" in r.json()["message"]
    assert login(c, "tom").status_code == 429  # the right password too, while locked
    assert login(c, "nobody", "whatever-1").status_code == 401  # the same answer as a wrong password
    sql(store_url, "UPDATE app_user SET locked_until = now() - interval '1 second'")
    assert login(c, "tom").status_code == 200
    assert sql(store_url, "SELECT failed_logins, locked_until FROM app_user") == [(0, None)]
    acts = [r[0] for r in sql(store_url, "SELECT action FROM audit_log WHERE action LIKE 'login%' ORDER BY audit_id")]
    assert acts == ["login_failed"] * 5 + ["login"]
    # one address guessing many names: refused before any password check
    app = create_app(store_url, auth=auth.AuthConfig(enabled=True, ip_max_failures=3))
    c = TestClient(app, **LAN)
    for i in range(3):
        assert login(c, f"user{i}", "wrong-password").status_code == 401
    assert login(c, "tom").status_code == 429
    assert login(TestClient(app, base_url=LAN["base_url"], client=("10.9.9.9", 1)), "tom").status_code == 200


def test_sessions_expire_and_are_revoked(store_url):
    add_users(store_url, "boss", "tom")
    c = signed_in(store_url, "tom")
    sql(store_url, "UPDATE app_session SET last_seen_at = now() - interval '9 hours'")  # idle 8 h
    assert c.get("/api/v1/runs").status_code == 401
    c = signed_in(store_url, "tom")
    assert c.get("/api/v1/runs").status_code == 200
    sql(store_url, "UPDATE app_session SET expires_at = now() - interval '1 second' WHERE revoked_at IS NULL")
    assert c.get("/api/v1/runs").status_code == 401  # absolute limit
    c = signed_in(store_url, "tom")
    assert c.post("/api/v1/auth/logout", headers=csrf(c)).status_code == 204
    assert c.get("/api/v1/runs").status_code == 401 and not c.cookies.get("desk_session")
    # an admin disables a user: the user's open sessions end at once
    c, boss = signed_in(store_url, "tom"), signed_in(store_url, "boss")
    assert boss.patch("/api/v1/users/tom", json={"active": False}, headers=csrf(boss)).status_code == 200
    assert c.get("/api/v1/runs").status_code == 401
    assert login(TestClient(create_app(store_url, auth=ON), **LAN), "tom").status_code == 403  # disabled
    # the in-memory cache of a live server forgets a revoked session too
    cached = auth.AuthConfig(enabled=True, cache_s=3600)
    boss2 = signed_in(store_url, "boss", cached)
    assert boss2.get("/api/v1/runs").status_code == 200
    assert boss2.post("/api/v1/auth/logout", headers=csrf(boss2)).status_code == 204
    boss2.cookies.clear()
    assert boss2.get("/api/v1/runs").status_code == 401


def test_password_reset_forces_a_change(store_url):
    add_users(store_url, "boss", "vic")
    boss, vic = signed_in(store_url, "boss"), signed_in(store_url, "vic")
    r = boss.post("/api/v1/users/vic/password", json={"password": "temporary-1"}, headers=csrf(boss))
    assert r.status_code == 204 and vic.get("/api/v1/runs").status_code == 401  # sessions revoked
    vic = TestClient(create_app(store_url, auth=ON), follow_redirects=False, **LAN)
    assert login(vic, "vic", "temporary-1").json()["user"]["must_change_password"]
    r = vic.get("/api/v1/runs")
    assert r.status_code == 403 and "временный пароль" in r.json()["message"]
    page = vic.get("/settings.html", headers={"Accept": "text/html"})
    assert page.status_code == 303 and page.headers["location"].startswith("/login.html?change=1&next=")
    bad = vic.post("/api/v1/auth/password", json={"current_password": "nope", "new_password": "brand-new-1"},
                   headers=csrf(vic))
    assert bad.status_code == 403
    ok = vic.post("/api/v1/auth/password", json={"current_password": "temporary-1", "new_password": "brand-new-1"},
                  headers=csrf(vic))
    assert ok.status_code == 204 and vic.get("/api/v1/runs").status_code == 200  # this session kept
    assert login(TestClient(create_app(store_url, auth=ON), **LAN), "vic", "brand-new-1").status_code == 200
    acts = [r[0] for r in sql(store_url, "SELECT action FROM audit_log WHERE target = 'vic' ORDER BY audit_id")]
    assert "password_reset" in acts and "password_change" in acts


def test_users_admin_api(store_url):
    add_users(store_url, "boss")
    boss = signed_in(store_url, "boss")
    h = csrf(boss)
    r = boss.post("/api/v1/users", json={"username": "Petrov", "role": "trader", "password": "petrov-pass-1",
                                         "display_name": "Петров П."}, headers=h)
    assert r.status_code == 201 and r.json()["username"] == "petrov" and r.json()["created_by"] == "boss"
    assert boss.post("/api/v1/users", json={"username": "petrov", "role": "viewer", "password": "x" * 10},
                     headers=h).status_code == 409
    assert boss.post("/api/v1/users", json={"username": "w", "role": "viewer", "password": "short"},
                     headers=h).status_code == 422
    assert boss.post("/api/v1/users", json={"username": "w", "role": "owner", "password": "x" * 10},
                     headers=h).status_code == 422
    assert boss.patch("/api/v1/users/petrov", json={"role": "viewer"}, headers=h).json()["role"] == "viewer"
    assert boss.patch("/api/v1/users/boss", json={"role": "viewer"}, headers=h).status_code == 409  # last admin
    assert boss.patch("/api/v1/users/boss", json={"active": False}, headers=h).status_code == 409
    assert boss.patch("/api/v1/users/ghost", json={"active": False}, headers=h).status_code == 404
    names = [u["username"] for u in boss.get("/api/v1/users").json()["users"]]
    assert names == ["boss", "petrov"]
    log = boss.get("/api/v1/audit?limit=5").json()["entries"]
    assert log[0]["action"] == "user_change" and log[0]["detail"] == {"role": "viewer"} and log[0]["actor"] == "boss"
    hashes = sql(store_url, "SELECT password_hash FROM app_user")
    assert all(h[0].startswith("$argon2id$") for h in hashes)
    with psycopg.connect(store_url, autocommit=True) as con, pytest.raises(psycopg.errors.RaiseException):
        con.execute("DELETE FROM audit_log")  # append-only


def test_first_admin_from_this_computer_only(store_url):
    app = create_app(store_url, auth=ON)
    remote = TestClient(app, **LAN)
    body = {"username": "boss", "password": PW["boss"], "display_name": "Шеф"}
    assert remote.post("/api/v1/auth/setup", json=body).status_code == 403
    rebound = TestClient(app, base_url="http://evil.example:8100", client=("127.0.0.1", 1))
    assert rebound.post("/api/v1/auth/setup", json=body).status_code == 403  # DNS rebinding
    local = TestClient(app, **LOOP)
    r = local.post("/api/v1/auth/setup", json=body)
    assert r.status_code == 200 and r.json()["user"]["username"] == "boss" and not r.json()["setup_needed"]
    assert local.get("/api/v1/users").status_code == 200  # signed in at once
    assert TestClient(app, **LOOP).post("/api/v1/auth/setup", json={**body, "username": "two"}).status_code == 409


def test_alias_edits_carry_the_user(store_url):
    add_users(store_url, "tom")
    c = signed_in(store_url, "tom")
    r = alias(c, "ОФЗ-н", csrf(c))
    assert r.status_code == 200 and r.json()["changed_by"] == "tom"
    assert sql(store_url, "SELECT actor, target, detail FROM audit_log WHERE action = 'alias'") == \
        [("tom", ISIN, {"alias": "ОФЗ-н", "was": None})]


# ---- the CLI and the launcher -------------------------------------------------------------------------------------


def test_cli_users_and_lan(store_url, home: Path, monkeypatch, capsys):
    monkeypatch.setenv("DESK_STORE_URL", store_url)

    def run(*argv: str, stdin: str = "") -> int:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return cli.main(list(argv))

    assert run("lan", "enable", "--self-signed") == 1 and "first admin" in capsys.readouterr().err
    assert run("user", "add", "Boss", "--role", "admin", "--password-stdin", stdin="admin-pass-1\n") == 0
    assert run("user", "add", "boss", "--role", "admin", "--password-stdin", stdin="admin-pass-1\n") == 1
    assert run("user", "add", "eve", "--role", "viewer", "--password-stdin", stdin="short\n") == 1
    assert run("user", "add", "tom", "--role", "trader", "--temporary", "--password-stdin", stdin="trader-pass\n") == 0
    capsys.readouterr()
    assert run("user", "list") == 0
    out = capsys.readouterr().out
    assert "boss" in out and "trader" in out and "must change password" in out
    assert run("user", "role", "tom", "admin") == 0 and run("user", "disable", "tom") == 0
    assert run("user", "passwd", "boss", "--password-stdin", stdin="new-admin-pass\n") == 0
    with psycopg.connect(store_url, autocommit=True) as con:
        assert auth.authenticate(con, "boss", "new-admin-pass", ON).role == "admin"
    assert run("lan", "enable") == 2  # HTTPS or --plain-http must be chosen
    assert run("lan", "enable", "--self-signed", "--port", "8443", "--name", "desk.corp") == 0
    out = capsys.readouterr().out
    assert "desk.bat firewall add" in out and "netsh advfirewall firewall add rule" in out
    doc = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert doc["auth"] is True and doc["bind_host"] == "0.0.0.0" and doc["port"] == 8443
    assert Path(doc["tls_cert"]).is_file() and Path(doc["tls_key"]).is_file()
    assert run("lan", "status") == 0 and "sign-in (auth): on" in capsys.readouterr().out
    assert run("lan", "enable", "--plain-http") == 0
    doc = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert doc["allow_plain_http"] is True and "tls_cert" not in doc
    assert run("lan", "disable", "--auth-off") == 0
    doc = json.loads((home / "settings.json").read_text(encoding="utf-8"))
    assert doc["bind_host"] == "127.0.0.1" and doc["auth"] is False
    actors = {r[0] for r in sql(store_url, "SELECT actor FROM audit_log WHERE action LIKE 'user%'")}
    assert len(actors) == 1 and next(iter(actors)).startswith("cli")


def _deskctl():
    spec = importlib.util.spec_from_file_location("deskctl_auth", REPO / "backend/windows/deskctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_launcher_reads_the_address_and_builds_the_firewall_rule(tmp_path: Path):
    d = _deskctl()
    assert d.serve_settings({}, tmp_path) == {"host": "127.0.0.1", "port": 8100, "tls": False, "auth": False}
    (tmp_path / "settings.json").write_text(json.dumps({"bind_host": "0.0.0.0", "port": 8443, "auth": True,
                                                        "tls_cert": "c", "tls_key": "k"}), encoding="utf-8")
    assert d.serve_settings({}, tmp_path) == {"host": "0.0.0.0", "port": 8443, "tls": True, "auth": True}
    assert d.serve_settings({"DESK_PORT": "9000"}, tmp_path)["port"] == 9000
    add = d.firewall_commands("add", 8443)[0]
    assert add[:5] == ["netsh", "advfirewall", "firewall", "add", "rule"]
    assert "name=Desk Monitor (TCP 8443)" in add and "localport=8443" in add and "dir=in" in add
    assert "profile=domain,private" in add and "protocol=TCP" in add and "action=allow" in add
    assert d.firewall_commands("add", 8100, "localsubnet")[0][-1] == "remoteip=localsubnet"
    assert d.firewall_commands("remove", 8100)[0][3:5] == ["delete", "rule"]
    assert cli.firewall_command(8443) == d._shown(add).replace('"name=Desk Monitor (TCP 8443)"',
                                                              'name="Desk Monitor (TCP 8443)"')
