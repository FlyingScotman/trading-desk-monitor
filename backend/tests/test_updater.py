"""The GitHub updater (desk.updater): what a release holds and which zip fits, checksums, the server's layers and
their error classes, the choice of layer, the downloads-folder watcher, the jobs end to end through the routes (the
network faked), the rollback from the launcher's backup and the release workflow's tool."""

from __future__ import annotations

import hashlib
import json
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
from pathlib import Path

import pytest
import test_update as tu
from test_update import NEW, OLD, client_for, deskctl, full_zip, stage_dir, tree

from desk import update
from desk.api import create_app
from desk.settings import load
from desk.updater import downloads, net, release, service

REPO_NAME = "FlyingScotman/trading-desk-monitor"
TAG = "v0.1.0"
NEWER = "0.1.1-20261022-ddddddd"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def gh(name: str, tag: str = TAG) -> str:
    return f"https://github.com/{REPO_NAME}/releases/download/{tag}/{name}"


def api_doc(files: dict[str, bytes], tag: str = TAG, digest: bool = True, body: str = "") -> dict:
    """A GitHub API release document for ``files`` (name -> bytes)."""
    return {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-10-22T08:00:00Z", "body": body,
            "assets": [{"name": n, "size": len(b), "browser_download_url": gh(n, tag),
                        **({"digest": "sha256:" + sha(b)} if digest else {})} for n, b in files.items()]}


# ---- versions and the choice of zip ----------------------------------------------------------------------------


def _rel(version: str, assets: list[release.Asset], adapter: bool = False) -> release.Release:
    return release.Release(REPO_NAME, TAG, version, None, "", assets, "api", adapter_included=adapter)


def _a(name: str, sha256: str | None = "a" * 64, size: int | None = 10) -> release.Asset:
    kind, v, base = release.classify_name(name)
    return release.Asset(name, kind, v, base, size, sha256, gh(name))


def test_names_and_the_choice_of_zip():
    assert release.classify_name(f"DeskMonitor-{NEW}.zip") == ("full", NEW, None)
    assert release.classify_name(f"DeskMonitor-{NEW}-with-adapter.zip") == ("full", NEW, None)
    assert release.classify_name(f"DeskMonitor-patch-{OLD}-to-{NEW}.zip") == ("patch", NEW, OLD)
    assert release.classify_name("SHA256SUMS") is None and release.classify_name("release.json") is None
    full, patch, other = _a(f"DeskMonitor-{NEW}.zip"), _a(f"DeskMonitor-patch-{OLD}-to-{NEW}.zip"), \
        _a(f"DeskMonitor-patch-{NEWER}-to-{NEW}.zip")
    rel = _rel(NEW, [full, patch, other])
    c = release.choose(rel, OLD)
    assert c.compare == "newer" and c.asset is patch and not c.needs_confirm  # the patch made from exactly OLD
    assert release.choose(_rel(NEW, [full, other]), OLD).asset is full  # no patch from OLD: the full zip
    same = release.choose(rel, NEW)
    assert same.compare == "same" and same.asset is None and "последняя" in same.reason
    older = release.choose(rel, NEWER)  # GitHub has an older version than the installed one
    assert older.compare == "older" and older.asset is None and older.needs_confirm and "явно" in older.reason
    forced = release.choose(rel, NEWER, allow_older=True)
    assert forced.asset is full and forced.needs_confirm  # a downgrade: only the full zip, only when asked
    assert release.choose(_rel(NEW, [_a(f"DeskMonitor-{NEW}.zip", sha256=None)]), OLD).asset is None  # no checksum
    assert release.choose(_rel(NEW, [full], adapter=True), OLD).asset is None  # never a public adapter


def test_checksums_from_every_source_must_agree():
    z = b"zip bytes"
    name = f"DeskMonitor-{NEW}.zip"
    assert release.parse_sums(f"{sha(z)}  {name}\n{sha(b'x')} *other.zip\njunk\n") == {name: sha(z), "other.zip": sha(b"x")}
    notes = f"text\n```text\n{sha(z)}  {name}\n```\n"
    rel = release.from_api(api_doc({name: z}, body=notes), REPO_NAME)
    assert rel.version == NEW and rel.assets[0].sha256 == sha(z) and rel.assets[0].size == len(z)
    assert release.from_api(api_doc({name: z}, digest=False, body=notes), REPO_NAME).assets[0].sha256 == sha(z)
    assert release.from_api(api_doc({name: z}, digest=False), REPO_NAME).assets[0].sha256 is None
    with pytest.raises(release.ReleaseError, match="не совпадают"):
        release.from_api(api_doc({name: z}, body=f"{sha(b'other')}  {name}\n"), REPO_NAME)
    with pytest.raises(release.ReleaseError, match="не совпадают"):
        release.from_api(api_doc({name: z}), REPO_NAME, sums_text=f"{sha(b'other')}  {name}\n")


def test_a_release_document_cannot_point_the_server_elsewhere():
    name = f"DeskMonitor-{NEW}.zip"
    doc = api_doc({name: b"z"})
    for url in ("https://evil.example/x.zip", f"http://github.com/{REPO_NAME}/releases/download/{TAG}/{name}",
                f"https://github.com/someone/else/releases/download/{TAG}/{name}",
                f"https://github.com/{REPO_NAME}/releases/download/{TAG}/../../x"):
        doc["assets"][0]["browser_download_url"] = url
        with pytest.raises(release.ReleaseError):
            release.from_api(doc, REPO_NAME)
    with pytest.raises(release.ReleaseError, match="нет архива"):
        release.from_api(api_doc({"notes.txt": b"x"}), REPO_NAME)
    with pytest.raises(release.ReleaseError, match="черновик"):
        release.from_api({**api_doc({name: b"z"}), "prerelease": True}, REPO_NAME)


def test_release_json_round_trip():
    files = [(f"DeskMonitor-{NEW}.zip", 100, "b" * 64), (f"DeskMonitor-patch-{OLD}-to-{NEW}.zip", 10, "c" * 64)]
    doc = json.loads(json.dumps(release.release_doc(TAG, NEW, files, commit="abc", core="core==1")))
    rel = release.from_release_json(doc, REPO_NAME)
    assert rel.version == NEW and rel.core == "core==1" and [a.kind for a in rel.assets] == ["full", "patch"]
    assert rel.assets[1].base == OLD and rel.assets[0].url == gh(files[0][0]) and rel.source == "release.json"
    assert release.from_public(rel.public()) == rel
    assert release.parse_sums(release.sums_text(files)) == {n: s for n, _, s in files}
    with pytest.raises(release.ReleaseError, match="адаптер"):
        release.release_doc(TAG, NEW, [(f"DeskMonitor-{NEW}-with-adapter.zip", 1, "d" * 64)])
    with pytest.raises(release.ReleaseError, match="другой версии"):
        release.from_release_json({**doc, "schema": 99}, REPO_NAME)


def test_verify_is_mandatory_and_checks_size_and_sha(tmp_path):
    p = tmp_path / "z.zip"
    p.write_bytes(b"0123456789")
    ok = _a(f"DeskMonitor-{NEW}.zip", sha256=sha(b"0123456789"), size=10)
    service.verify(p, ok)
    for bad, match in ((_a(ok.name, sha256=sha(b"0123456789"), size=11), "размер"),
                       (_a(ok.name, sha256="0" * 64, size=10), "SHA-256"),
                       (_a(ok.name, sha256=None), "нет опубликованной")):
        with pytest.raises(service.VerifyError, match=match):
            service.verify(p, bad)


# ---- error classes of the layers ---------------------------------------------------------------------------------


def test_python_errors_are_classified():
    cert = ssl.SSLCertVerificationError(1, "certificate verify failed")
    cert.verify_message = "unable to get local issuer certificate"
    assert net._classify_os("python", urllib.error.URLError(cert)).code == "tls_untrusted"
    assert net._classify_os("python", urllib.error.URLError(ssl.SSLError(1, "wrong version"))).code == "tls"
    assert net._classify_os("python", urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))).code == "dns"
    assert net._classify_os("python", urllib.error.URLError(TimeoutError("timed out"))).code == "timeout"
    assert net._classify_os("python", urllib.error.URLError(ConnectionRefusedError(10061, "refused"))).code == "connect"
    e = net._classify_os("python", urllib.error.URLError(OSError("Tunnel connection failed: 407 Proxy Authentication Required")))
    assert e.code == "proxy_auth" and e.status == 407 and e.public()["it_hint"]
    assert net._classify_os("python", OSError("Tunnel connection failed: 403 Forbidden")).code == "proxy_denied"
    assert net.http_error("python", "https://github.com/x", 403, {}).code == "proxy_denied"  # not GitHub's own answer
    assert net.http_error("python", "https://github.com/x", 403, {"x-github-request-id": "1"}).code == "http"
    assert net.http_error("python", "https://api.github.com/x", 403,
                          {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}).code == "rate_limited"
    assert net.http_error("python", "https://github.com/x", 404, {}).code == "not_found"


def _ctx(tmp_path: Path, runner=None) -> net.Context:
    return net.Context(home=tmp_path / "home", timeout_s=5, runner=runner, proxy_for=lambda url: None)


def test_curl_errors_and_the_proxy_login(tmp_path, monkeypatch):
    seen = {}

    def runner(cmd, **kw):
        cfg = Path(cmd[cmd.index("-K") + 1]).read_text()
        seen["cfg"] = cfg
        heads = Path(cmd[cmd.index("-D") + 1])
        if seen["mode"] == "407":
            heads.write_bytes(b"HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Negotiate\r\n"
                              b"Proxy-Authenticate: NTLM\r\n\r\n")
            return subprocess.CompletedProcess(cmd, 56, b"000 407", b"curl: (56) CONNECT tunnel failed, response 407")
        if seen["mode"] == "tls":
            return subprocess.CompletedProcess(cmd, 60, b"000 200", b"curl: (60) SSL certificate problem")
        if seen["mode"] == "schannel":
            return subprocess.CompletedProcess(cmd, 35, b"000 200", b"schannel: SEC_E_UNTRUSTED_ROOT (0x80090325)")
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"{}")
        heads.write_bytes(b"HTTP/1.1 302 Found\r\nLocation: x\r\n\r\nHTTP/1.1 200 OK\r\nX-GitHub-Request-Id: 1\r\n\r\n")
        return subprocess.CompletedProcess(cmd, 0, b"200 000", b"")

    ctx = net.Context(home=tmp_path / "home", timeout_s=5, runner=runner, proxy_for=lambda url: "http://proxy:3128")
    for mode, code in (("407", "proxy_auth"), ("tls", "tls_untrusted"), ("schannel", "tls_untrusted")):
        seen["mode"] = mode
        with pytest.raises(net.NetError) as ei:
            net.fetch_curl(ctx, "https://github.com/x", tmp_path / "out", 1 << 20, curl="curl")
        assert ei.value.code == code
        if mode == "407":
            assert ei.value.schemes == ["Negotiate", "NTLM"]
    assert 'proxy-user = ":"' in seen["cfg"] and "proxy-anyauth" in seen["cfg"]  # SSPI: the Windows logon
    seen["mode"] = "ok"
    r = net.fetch_curl(ctx, "https://github.com/x", tmp_path / "out", 1 << 20, curl="curl")
    assert r.status == 200 and r.headers["x-github-request-id"] == "1" and r.proxy == "http://proxy:3128"
    assert not list((tmp_path / "home/updates/downloads").glob(".dl-*"))  # the config and header files are gone


def test_powershell_results_are_classified(tmp_path, monkeypatch):
    monkeypatch.setattr(net, "find_powershell", lambda: "powershell.exe")
    answers = {}

    def runner(cmd, **kw):
        assert cmd[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"] and '"' not in cmd[4]
        doc = answers["doc"]
        if doc.get("ok"):
            Path(kw["env"]["DESK_UPD_OUT"]).write_bytes(b"data")
        return subprocess.CompletedProcess(cmd, 0, (json.dumps(doc) + "\n").encode(), b"")

    ctx = _ctx(tmp_path, runner)
    for doc, code in (({"ok": False, "web": "TrustFailure", "message": "trust relationship"}, "tls_untrusted"),
                      ({"ok": False, "web": "NameResolutionFailure", "message": "x"}, "dns"),
                      ({"ok": False, "status": 407, "message": "(407) Proxy Authentication Required"}, "proxy_auth"),
                      ({"ok": False, "status": 403, "message": "Forbidden"}, "proxy_denied"),
                      ({"ok": False, "web": "ConnectFailure", "message": "x"}, "connect")):
        answers["doc"] = doc
        with pytest.raises(net.NetError) as ei:
            net.fetch_powershell(ctx, "https://github.com/x", tmp_path / "o", 1 << 20)
        assert ei.value.code == code, doc
    answers["doc"] = {"ok": True, "status": 200, "proxy": "http://pac-proxy:8080/", "github": "abc"}
    r = net.fetch_powershell(ctx, "https://github.com/x", tmp_path / "o", 1 << 20)
    assert r.read() == b"data" and r.proxy == "http://pac-proxy:8080"
    answers["doc"] = {"proxy": "http://pac-proxy:8080/"}  # the PAC answer of Windows for python and curl
    monkeypatch.setattr(net, "windows", lambda: True)
    monkeypatch.setattr(net, "hub_proxy", lambda url: None)
    net._pac.clear()
    assert net.system_proxy("https://github.com/", runner) == "http://pac-proxy:8080"


def test_probe_counts_any_answer_of_the_site(tmp_path, monkeypatch):
    def fake(ctx, url, dest, max_bytes, accept=None, progress=None):
        if "api" in url:
            raise net.NetError("not_found", "404", "python", 404)
        raise net.NetError("tls_untrusted", "self-signed certificate in certificate chain", "python")

    monkeypatch.setitem(net.FETCHERS, "python", fake)
    assert net.probe("python", _ctx(tmp_path), "https://api.github.com/")["ok"]
    r = net.probe("python", _ctx(tmp_path), "https://github.com/")
    assert not r["ok"] and r["code"] == "tls_untrusted" and "корневой" in r["it_hint"]


# ---- the server's layers, chosen -----------------------------------------------------------------------------------


class FakeNet:
    """``net.FETCHERS`` stand-ins: a layer either fails with a NetError code or serves ``files`` by URL."""

    def __init__(self, monkeypatch, files: dict[str, bytes], broken: dict[str, str]):
        self.files, self.broken, self.calls = files, broken, []
        for layer in net.LAYERS:
            monkeypatch.setitem(net.FETCHERS, layer, self.make(layer))
        monkeypatch.setattr(net, "available", lambda layer: None)

    def make(self, layer):
        def fetch(ctx, url, dest, max_bytes, accept=None, progress=None):
            self.calls.append((layer, url))
            if layer in self.broken:
                raise net.NetError(self.broken[layer], f"{layer} broken", layer)
            if url not in self.files:
                raise net.NetError("not_found", f"404 {url}", layer, 404)
            dest.write_bytes(self.files[url])
            if progress:
                progress(len(self.files[url]))
            return net.Result(200, {}, dest, len(self.files[url]))
        return fetch


@pytest.fixture
def bundle_env(tu_home, old_new, monkeypatch):
    """The installed old bundle under the launcher, the new one's full zip and the release that publishes it."""
    root, new, _ = old_new
    monkeypatch.setenv("DESK_BUNDLE_ROOT", str(root))
    monkeypatch.setenv("DESK_BUNDLE_VERSION", OLD)
    monkeypatch.setenv("DESK_RESTART_CODE", "75")
    monkeypatch.setenv("DESK_UPDATE_DOWNLOADS_DIR", str(tu_home.parent / "Downloads"))
    monkeypatch.setenv("DESK_UPDATE_AUTO_CHECK", "0")  # a test asks itself; the auto check has its own test
    (tu_home.parent / "Downloads").mkdir()
    z = full_zip(new, tu_home.parent / "built.zip").read_bytes()
    name = f"DeskMonitor-{NEW}.zip"
    doc = release.release_doc(TAG, NEW, [(name, len(z), sha(z))])
    files = {release.latest_url(REPO_NAME, release.RELEASE_JSON): json.dumps(doc).encode(), gh(name): z}
    return {"root": root, "zip": z, "name": name, "files": files, "home": tu_home}


@pytest.fixture
def tu_home(tmp_path, monkeypatch):
    return _home(tmp_path, monkeypatch)


def _home(tmp_path, monkeypatch):
    import os

    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    return h


@pytest.fixture
def old_new(tmp_path):
    """As test_update's: (the installed old bundle, the new bundle's stage, the old bundle's full zip)."""
    old = stage_dir(tmp_path / "old", tu.bundle(OLD))
    new = stage_dir(tmp_path / "new", tu.bundle(NEW, {"app/prototypes/settings.html": b"<p>2</p>",
                                                      "app/prototypes/new.js": b"new", "app/prototypes/old.js": None}))
    root = tmp_path / "DeskMonitor"
    shutil.copytree(old, root)
    return root, new, full_zip(old, tmp_path / f"DeskMonitor-{OLD}.zip")


def _app():
    app = create_app("postgresql://unused")
    asked: list[bool] = []
    app.state.request_restart = lambda: asked.append(True)
    return app, asked


def _wait(c, states=("restarting", "ready", "failed", "cancelled", "waiting_browser"), timeout=20.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = c.get("/api/v1/update/remote").json()["job"]
        if j and j["state"] in states:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {states}: {j}")


def test_check_picks_the_first_layer_that_works_and_remembers_it(bundle_env, monkeypatch):
    fake = FakeNet(monkeypatch, bundle_env["files"], {"python": "tls_untrusted"})
    st = load()
    chk = service.check(st)
    assert chk["ok"] and chk["layer"] == "curl" and [a["ok"] for a in chk["attempts"]] == [False, True]
    assert chk["attempts"][0]["code"] == "tls_untrusted" and chk["attempts"][0]["it_hint"]
    assert service.layers(st, st.home)[0] == "curl"  # the one that worked goes first next time
    fake.calls.clear()
    service.check(st)
    assert fake.calls[0][0] == "curl"
    s = service.status(st, True, None)
    assert s["update_available"] and s["choice"]["asset"]["name"] == bundle_env["name"] and not s["browser_check_due"]
    monkeypatch.setenv("DESK_UPDATE_TRANSPORT", "powershell")  # a layer named in the settings: only that one
    assert service.layers(load(), st.home) == ["powershell"]
    monkeypatch.setenv("DESK_UPDATE_TRANSPORT", "browser")
    assert service.layers(load(), st.home) == []


def test_check_falls_back_to_the_api_and_reports_no_release(bundle_env, monkeypatch):
    z, name = bundle_env["zip"], bundle_env["name"]
    files = {release.api_latest_url(REPO_NAME): json.dumps(api_doc({name: z, "SHA256SUMS": b"s"})).encode(),
             release.latest_url(REPO_NAME, "SHA256SUMS"): f"{sha(z)}  {name}\n".encode()}
    FakeNet(monkeypatch, files, {})
    st = load()
    assert service.check(st)["ok"]
    assert service.status(st, True, None)["release"]["source"] == "api"
    FakeNet(monkeypatch, {}, {})  # nothing on GitHub: no release yet, the network works
    assert service.check(st)["ok"] and service.status(st, True, None)["no_release"]


def test_when_no_layer_works_the_browser_checks(bundle_env, monkeypatch):
    FakeNet(monkeypatch, {}, {"python": "proxy_auth", "curl": "proxy_auth", "powershell": "dns"})
    st = load()
    chk = service.check(st)
    assert not chk["ok"] and [a["code"] for a in chk["attempts"]] == ["proxy_auth", "proxy_auth", "dns"]
    c = client_for(_app()[0])
    s = c.get("/api/v1/update/remote").json()
    assert s["browser_check_due"] and not s["update_available"]
    z, name = bundle_env["zip"], bundle_env["name"]
    r = c.post("/api/v1/update/remote/browser-release", json={"release": api_doc({name: z})})
    assert r.status_code == 200 and r.json()["update_available"] and not r.json()["browser_check_due"]
    assert r.json()["release"]["source"] == "browser"
    bad = api_doc({name: z})
    bad["assets"][0]["browser_download_url"] = "https://evil.example/x.zip"
    assert c.post("/api/v1/update/remote/browser-release", json={"release": bad}).status_code == 422
    assert c.post("/api/v1/update/remote/browser-release", json={"release": {}},
                  headers={"Origin": "https://evil.example"}).status_code == 403


def test_the_auto_check_runs_when_due_in_the_bundle_only(bundle_env, monkeypatch):
    fake = FakeNet(monkeypatch, bundle_env["files"], {})
    monkeypatch.setenv("DESK_UPDATE_AUTO_CHECK", "1")
    st = load()
    assert service.maybe_auto_check(st)
    for _ in range(200):
        if not service._checking.is_set():
            break
        time.sleep(0.01)
    assert fake.calls and service.read_state(st.home)["check"]["ok"]
    assert not service.maybe_auto_check(st)  # checked just now
    monkeypatch.setattr(service, "CHECK_EVERY_S", -1)
    monkeypatch.delenv("DESK_BUNDLE_ROOT")  # a checkout: never on its own
    assert not service.maybe_auto_check(load())
    monkeypatch.setenv("DESK_BUNDLE_ROOT", str(bundle_env["root"]))
    monkeypatch.setenv("DESK_UPDATE_AUTO_CHECK", "0")
    assert not service.maybe_auto_check(load())


def test_browser_reports_no_release_or_its_error(bundle_env):
    c = client_for(_app()[0])
    s = c.post("/api/v1/update/remote/browser-release", json={"none": True}).json()
    assert s["no_release"] and s["browser"]["ok"] and not s["browser_check_due"]
    s = c.post("/api/v1/update/remote/browser-release", json={"error": "TypeError: Failed to fetch"}).json()
    assert s["browser"]["error"] == "TypeError: Failed to fetch"
    s = c.post("/api/v1/update/remote/browser-diag", json={"api": {"ok": True, "ms": 120}, "github": {"ok": False,
               "ms": 30, "error": "Failed to fetch"}}).json()
    assert s["diagnose"]["browser"]["api"]["ok"] and not s["diagnose"]["browser"]["github"]["ok"]


# ---- install jobs ---------------------------------------------------------------------------------------------------


def test_server_download_installs_through_the_pipeline(bundle_env, monkeypatch):
    FakeNet(monkeypatch, bundle_env["files"], {"python": "connect"})
    app, asked = _app()
    c = client_for(app)
    assert c.post("/api/v1/update/remote/check", json={}).json()["update_available"]
    r = c.post("/api/v1/update/remote/install", json={})
    assert r.status_code == 202 and r.json()["job"]["via"] == "server"
    j = _wait(c, ("restarting", "failed"))
    assert j["state"] == "restarting" and j["layer"] == "curl" and asked == [True], j
    assert [(a["ok"], a["layer"]) for a in j["attempts"]] == [(True, "curl")]  # the layer the check found first
    pending = c.get("/api/v1/update").json()["pending"]
    assert pending["version"] == NEW and pending["from_version"] == OLD
    assert (bundle_env["home"] / "updates/uploads" / bundle_env["name"]).is_file()


def test_a_damaged_download_is_not_installed(bundle_env, monkeypatch):
    files = dict(bundle_env["files"])
    files[gh(bundle_env["name"])] = b"PK" + bundle_env["zip"][2:-1] + b"X"  # the proxy changed a byte
    FakeNet(monkeypatch, files, {"powershell": "unavailable"})
    app, asked = _app()
    c = client_for(app)
    c.post("/api/v1/update/remote/check", json={})
    c.post("/api/v1/update/remote/install", json={})
    j = _wait(c)
    assert j["state"] == "waiting_browser" and asked == []  # every server copy failed the checksum: the browser now
    assert {a["code"] for a in j["attempts"] if not a["ok"]} == {"checksum", "unavailable"}
    c.post("/api/v1/update/remote/cancel", json={})
    assert _wait(c, ("cancelled",))["state"] == "cancelled"
    assert not list((bundle_env["home"] / "updates/uploads").glob("*.zip"))


def test_browser_download_is_watched_verified_and_installed(bundle_env, monkeypatch):
    FakeNet(monkeypatch, {}, {"python": "proxy_denied", "curl": "proxy_denied", "powershell": "proxy_denied"})
    monkeypatch.setattr(service, "WATCH_POLL_S", 0.05)
    app, asked = _app()
    c = client_for(app)
    c.post("/api/v1/update/remote/check", json={})
    z, name = bundle_env["zip"], bundle_env["name"]
    c.post("/api/v1/update/remote/browser-release", json={"release": api_doc({name: z})})
    r = c.post("/api/v1/update/remote/install", json={})
    job = r.json()["job"]
    assert job["via"] == "browser" and job["browser_url"] == gh(name)  # the page starts the download at once
    dl = bundle_env["home"].parent / "Downloads"
    (dl / f"{name}.crdownload").write_bytes(z[:100])
    assert _wait(c, ("waiting_browser",))["found"]["state"] in ("downloading", "waiting")
    (dl / name).write_bytes(b"an older file of that name")  # already there: the browser saves "name (1).zip"
    (dl / f"{name}.crdownload").unlink()
    (dl / name.replace(".zip", " (1).zip")).write_bytes(z)
    j = _wait(c, ("restarting", "failed"))
    assert j["state"] == "restarting" and asked == [True], j
    assert c.get("/api/v1/update").json()["pending"]["version"] == NEW
    assert (dl / name.replace(".zip", " (1).zip")).is_file()  # the owner's file stays where the browser put it


def test_downgrade_only_when_asked(bundle_env, monkeypatch):
    FakeNet(monkeypatch, bundle_env["files"], {})
    monkeypatch.setenv("DESK_BUNDLE_VERSION", NEWER)  # pretend a newer version is installed
    app, _ = _app()
    c = client_for(app)
    s = c.post("/api/v1/update/remote/check", json={}).json()
    assert not s["update_available"] and s["choice"]["compare"] == "older" and s["choice"]["needs_confirm"]
    r = c.post("/api/v1/update/remote/install", json={})
    assert r.status_code == 409 and "явно" in r.json()["message"]
    assert c.post("/api/v1/update/remote/install", json={"allow_downgrade": True}).status_code == 202


def test_the_settings_page_install_refuses_an_older_zip_unless_asked(bundle_env, monkeypatch, tmp_path):
    root = bundle_env["root"]
    monkeypatch.setenv("DESK_BUNDLE_VERSION", NEWER)
    up = bundle_env["home"] / "updates/uploads"
    up.mkdir(parents=True)
    (up / bundle_env["name"]).write_bytes(bundle_env["zip"])
    (root / "VERSION").write_text(NEWER + "\n")  # the installed bundle says so too
    app, asked = _app()
    c = client_for(app)
    body = {"source": "upload", "name": bundle_env["name"]}
    r = c.post("/api/v1/update/install", json=body)
    assert r.status_code == 409 and "старее" in r.json()["message"] and asked == []


def test_find_in_downloads_with_a_saved_sha256sums(bundle_env, monkeypatch):
    app, asked = _app()
    c = client_for(app)
    dl = bundle_env["home"].parent / "Downloads"
    z, name = bundle_env["zip"], bundle_env["name"]
    (dl / name).write_bytes(z)
    j = c.get("/api/v1/update/remote/downloads").json()
    assert j["items"][0]["verified"] is False and "SHA256SUMS" in j["items"][0]["reason"]
    (dl / "SHA256SUMS").write_text(f"{sha(z)}  {name}\n")
    it = c.get("/api/v1/update/remote/downloads").json()["items"][0]
    assert it["verified"] and it["compare"] == "newer"
    assert c.post("/api/v1/update/remote/install-found", json={"file": "../x.zip", "sha256": it["sha256"]}).status_code == 422
    assert c.post("/api/v1/update/remote/install-found", json={"file": name, "sha256": "0" * 64}).status_code == 422
    r = c.post("/api/v1/update/remote/install-found", json={"file": name, "sha256": it["sha256"]})
    assert r.status_code == 202 and r.json()["restarting"] and asked == [True]


# ---- the downloads folder -------------------------------------------------------------------------------------------


def test_the_watcher_finds_only_the_published_bytes(tmp_path):
    name, z = f"DeskMonitor-{NEW}.zip", b"zipdata" * 10
    assert downloads.find_expected(tmp_path / "missing", name, len(z), sha(z)).state == "waiting"
    assert downloads.find_expected(tmp_path, name, len(z), sha(z)).state == "waiting"
    (tmp_path / f"{name}.crdownload").write_bytes(z[:5])
    f = downloads.find_expected(tmp_path, name, len(z), sha(z))
    assert f.state == "downloading" and f.size == 5
    (tmp_path / f"{name}.crdownload").unlink()
    (tmp_path / name).write_bytes(z[:-1] + b"!")  # same size, other bytes
    f = downloads.find_expected(tmp_path, name, len(z), sha(z))
    assert f.state == "mismatch" and "контрольная сумма" in f.message
    (tmp_path / name.replace(".zip", "(2).zip")).write_bytes(z)  # Firefox's numbering
    f = downloads.find_expected(tmp_path, name, len(z), sha(z))
    assert f.state == "found" and f.path.name == name.replace(".zip", "(2).zip")
    (tmp_path / f"x{name}").write_bytes(z)  # another name is never taken
    assert [p.name for p in downloads.copies(tmp_path, name)] == [name.replace(".zip", "(2).zip"), name]


def test_downloads_dir_setting_wins(tmp_path):
    assert downloads.downloads_dir(tmp_path) == tmp_path
    assert downloads.downloads_dir(None).name in ("Downloads", "Загрузки") or downloads.windows_downloads()


# ---- rollback from the launcher's backup -----------------------------------------------------------------------------


def test_rollback_restores_the_previous_version(old_new, tmp_path, monkeypatch):
    root, new, _ = old_new
    for name, value in (("ROOT", root), ("APP", root / "app"), ("WHEELHOUSE", root / "wheelhouse"),
                        ("ADAPTER", root / "adapter"), ("ENV", root / "env"), ("SCRIPTS", root / "env/bin"),
                        ("MARKER", root / "env/desk-bundle.json")):
        monkeypatch.setattr(deskctl, name, value)
    monkeypatch.setattr(deskctl, "say", lambda msg="": None)
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    env = {"DESK_HOME": str(home)}
    old_tree = tree(root)
    assert update.rollback_point(root, home) is None
    with pytest.raises(update.UpdateError, match="Нет резервной копии"):
        update.stage_rollback(root, home)
    doc = update.stage(full_zip(new, tmp_path / "n.zip"), root, home)
    deskctl.apply_update(env, Path(doc["staged"]))
    deskctl.confirm_update(env)
    assert deskctl.version() == NEW
    point = update.rollback_point(root, home)
    assert point and point["version"] == OLD
    staged = update.stage_rollback(root, home)
    assert staged["version"] == OLD and staged["from_version"] == NEW and staged["kind"] == "rollback"
    assert staged["delete"] == ["app/prototypes/new.js"]
    deskctl.apply_update(env, Path(staged["staged"]))
    deskctl.confirm_update(env)
    assert tree(root) == old_tree and deskctl.version() == OLD
    assert update.rollback_point(root, home)["version"] == NEW  # and forward again, from the rollback's backup


def test_rollback_route(old_new, tmp_path, monkeypatch):
    root, _, _ = old_new
    h = _home(tmp_path, monkeypatch)
    monkeypatch.setenv("DESK_BUNDLE_ROOT", str(root))
    monkeypatch.setenv("DESK_RESTART_CODE", "75")
    monkeypatch.setenv("DESK_BUNDLE_VERSION", OLD)
    app, asked = _app()
    c = client_for(app)
    assert c.get("/api/v1/update").json()["rollback"] is None
    assert c.post("/api/v1/update/rollback", json={}).status_code == 422
    backup = h / "updates/backup" / "0.0.9-20261001-0000000"
    (backup / "files").mkdir(parents=True)
    (backup / "files/VERSION").write_text("0.0.9-20261001-0000000\n")
    (backup / "backup.json").write_text(json.dumps({"version": "0.0.9-20261001-0000000", "to_version": OLD,
                                                    "replaced": ["VERSION"], "created": []}))
    assert c.get("/api/v1/update").json()["rollback"] == {"version": "0.0.9-20261001-0000000"}
    r = c.post("/api/v1/update/rollback", json={})
    assert r.status_code == 202 and r.json()["version"] == "0.0.9-20261001-0000000" and asked == [True]


# ---- the release workflow's tool -------------------------------------------------------------------------------------

release_assets = tu._load("release_assets", tu.REPO / "backend/tools/release_assets.py")


def test_release_assets_checks_the_zips_and_writes_the_manifests(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    version = release_assets.backend_version()
    bundle_version = f"{version}-20261022-eeeeeee"
    stage = stage_dir(tmp_path / "rel", tu.bundle(bundle_version))
    full_zip(stage, dist / f"DeskMonitor-{bundle_version}.zip")
    with pytest.raises(SystemExit, match="must be"):
        release_assets.check_tag("v9.9.9")
    doc = release_assets.write(dist, f"v{version}", REPO_NAME, "abc")
    assert doc["version"] == bundle_version and doc["assets"][0]["kind"] == "full" and not doc["adapter_included"]
    sums = (dist / "SHA256SUMS").read_text()
    assert release.parse_sums(sums)[f"DeskMonitor-{bundle_version}.zip"] == doc["assets"][0]["sha256"]
    assert sums.strip() in (dist / "NOTES.md").read_text()
    rel = release.from_release_json(json.loads((dist / "release.json").read_text()), REPO_NAME)
    assert rel.version == bundle_version
    # the notes alone (what the browser reads through the API) carry the checksums
    api = api_doc({f"DeskMonitor-{bundle_version}.zip": (dist / f"DeskMonitor-{bundle_version}.zip").read_bytes()},
                  tag=f"v{version}", digest=False, body=(dist / "NOTES.md").read_text())
    assert release.from_api(api, REPO_NAME).assets[0].sha256 == doc["assets"][0]["sha256"]
    with_adapter = stage_dir(tmp_path / "ad", tu.bundle(bundle_version, {"adapter/tda/__init__.py": b""}))
    full_zip(with_adapter, dist / f"DeskMonitor-{bundle_version}.zip")
    with pytest.raises(SystemExit, match="adapter"):
        release_assets.write(dist, f"v{version}", REPO_NAME, "abc")
    full_zip(stage, dist / f"DeskMonitor-{bundle_version}.zip")
    with pytest.raises(SystemExit, match="tag"):
        release_assets.collect(dist, "v0.0.1")


def test_release_notes_start_with_the_versions_changelog_section(tmp_path):
    log = tmp_path / "CHANGELOG.md"
    log.write_text("# Что нового\n\nintro\n\n## 1.2.10\n\n- новое\n\n## 1.2.1\n\n- старое\n", encoding="utf-8")
    assert release_assets.changelog("1.2.10", log) == "- новое"
    assert release_assets.changelog("1.2.1", log) == "- старое"
    assert release_assets.changelog("1.2", log) == "" and release_assets.changelog("1.2.1", tmp_path / "no") == ""
    version = release_assets.backend_version()
    section = release_assets.changelog(version)
    assert section, f"CHANGELOG.md has no section ## {version} for the release notes"
    dist = tmp_path / "dist"
    dist.mkdir()
    bundle_version = f"{version}-20261022-eeeeeee"
    full_zip(stage_dir(tmp_path / "rel", tu.bundle(bundle_version)), dist / f"DeskMonitor-{bundle_version}.zip")
    release_assets.write(dist, f"v{version}", REPO_NAME, "abc")
    notes = (dist / "NOTES.md").read_text(encoding="utf-8")
    assert notes.startswith(f"Desk Monitor {bundle_version}\n\n{section}\n\n---\n\n")
    assert release.parse_sums(notes) == release.parse_sums((dist / "SHA256SUMS").read_text())
