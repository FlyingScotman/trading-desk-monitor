"""The private adapter's updater (desk.updater.adapter, TDM#427): the release zip checked strictly by its inner
manifest, the token sent only to api.github.com and never across a redirect, in a curl config file or a PowerShell
child's environment, never in an answer or a record; install, settings resolution (explicit > installed vs bundle),
rollback, loader failures, the pinned fingerprint, the browser fallback and the routes (local only). No network."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from desk import diagnostics
from desk.api import create_app
from desk.inbox import scan
from desk.settings import load
from desk.updater import adapter, adapter_store, net

REPO = "FlyingScotman/trades-data-adapter"
TOKEN = "github_pat_" + "A1b2C3d4E5" * 5
PKG = "tda_upd_t"  # not a real module name: no name collision in the loader's child
DESC = {"name": "tda", "spi": 1, "contract": "private/1.0",
        "sources": [{"source": "positions_csv", "dataset": "positions", "time_basis": "start_of_day",
                     "completeness": "full_snapshot", "scope": ["book_id"], "covers": "listed"}]}


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def pkg_files(version: str = "0.5.0", contract: str = "private/1.0", name: str = PKG) -> dict[str, bytes]:
    desc = {**DESC, "version": version, "contract": contract}
    init = (f'__version__ = "{version}"\nSPI_VERSION = 1\n\ndef describe():\n    return {desc!r}\n\n'
            "def match(path, ctx):\n    return None\n\ndef read(path, hit, ctx):\n    return {}\n\n"
            "def reference(ctx):\n    return {'books': []}\n")
    return {f"src/{name}/__init__.py": init.encode(), f"src/{name}/_acquisition/system2/__init__.py": b"",
            f"src/{name}/_acquisition/system2/run.py": b"print('x')\n",  # a nested package of a plain folder
            "README.md": b"# adapter\n", "README_INSTALL.txt": b"install\n"}


def make_zip(path: Path, files: dict[str, bytes], *, version: str = "0.5.0", package: str = f"src/{PKG}",
             listed: dict[str, str] | None = None, extra: dict[str, bytes] | None = None,
             manifest: dict | None = None) -> Path:
    man = {"format": 1, "name": "tda", "version": version, "spi": 1, "contract": "private/1.0", "commit": "c" * 40,
           "package": package, "files": listed if listed is not None else {n: sha(b) for n, b in files.items()},
           **(manifest or {})}
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in sorted({**files, **(extra or {})}.items()):
            z.writestr(zipfile.ZipInfo(n, (1980, 1, 1, 0, 0, 0)), b)
        z.writestr(zipfile.ZipInfo(adapter.MANIFEST, (1980, 1, 1, 0, 0, 0)), json.dumps(man))
    return path


def zip_bytes(tmp_path: Path, version: str = "0.5.0", **kw) -> bytes:
    return make_zip(tmp_path / f"build-{version}" / adapter.ZIP_NAME, pkg_files(version, **kw), version=version
                    ).read_bytes()


# ---- the zip, strictly by its manifest ---------------------------------------------------------------------------


def test_a_good_zip_verifies(tmp_path):
    z = make_zip(tmp_path / "a.zip", pkg_files())
    info = adapter.verify_zip(z)
    assert info["version"] == "0.5.0" and info["package"] == f"src/{PKG}" and info["name"] == PKG
    assert info["sha256"] == sha(z.read_bytes()) and f"src/{PKG}/_acquisition/system2/run.py" in info["files"]


@pytest.mark.parametrize("case", ["extra", "missing", "wrong_sha", "two_packages", "dotdot", "backslash", "absolute",
                                  "root_init", "no_manifest", "format", "package", "outside", "symlink", "encrypted",
                                  "version"])
def test_a_zip_outside_the_contract_is_refused(tmp_path, case):
    files = pkg_files()
    p = tmp_path / "bad.zip"
    if case == "extra":
        make_zip(p, files, extra={f"src/{PKG}/sneaky.py": b"x"})
    elif case == "missing":
        make_zip(p, files, listed={**{n: sha(b) for n, b in files.items()}, f"src/{PKG}/gone.py": "0" * 64})
    elif case == "wrong_sha":
        make_zip(p, files, listed={**{n: sha(b) for n, b in files.items()}, "README.md": "1" * 64})
    elif case == "two_packages":
        make_zip(p, {**files, "src/other/__init__.py": b""})
    elif case in ("dotdot", "backslash", "absolute"):
        bad = {"dotdot": "src/../evil.py", "backslash": "src\\evil.py", "absolute": "/evil.py"}[case]
        make_zip(p, {**files, bad: b"x"})
    elif case == "root_init":
        make_zip(p, {**files, "__init__.py": b""})
    elif case == "no_manifest":
        with zipfile.ZipFile(p, "w") as z:
            for n, b in files.items():
                z.writestr(n, b)
    elif case == "format":
        make_zip(p, files, manifest={"format": 2})
    elif case == "package":
        make_zip(p, files, package="src/other")
    elif case == "outside":
        make_zip(p, {**files, "tools/build.py": b"x"})
    elif case == "version":
        make_zip(p, files, version="../1")
    elif case in ("symlink", "encrypted"):
        make_zip(p, files)
        with zipfile.ZipFile(p, "a") as z:
            info = zipfile.ZipInfo(f"src/{PKG}/link.py", (1980, 1, 1, 0, 0, 0))
            if case == "symlink":
                info.external_attr = (0o120777 << 16)
            else:
                info.flag_bits |= 0x1
            z.writestr(info, b"target")
    with pytest.raises(adapter.AdapterUpdateError) as ei:
        adapter.verify_zip(p)
    assert str(ei.value), case


def test_versions_compare():
    c = adapter_store.compare
    assert c("0.5.1", "0.5.0") == "newer" and c("0.5.0", "0.10.0") == "older" and c("0.5", "0.5.0") == "same"
    assert c("0.5.0", "0.5.0") == "same" and c("1.0.0-rc1", "1.0.0") == "other" and c("x", "0.1") == "other"
    assert c("0.6.0", None) == "other"


# ---- the network layers and the token ----------------------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: ClassVar[list[tuple[str, str, str | None]]] = []
    target = ""

    def do_GET(self):
        type(self).seen.append((self.headers.get("Host"), self.path, self.headers.get("Authorization")))
        if self.path == "/asset":
            self.send_response(302)
            self.send_header("Location", type(self).target)
            self.end_headers()
            return
        body = b"zip-bytes"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_python_layer_drops_the_token_on_a_cross_host_redirect(tmp_path):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        _Recorder.seen = []
        _Recorder.target = f"http://localhost:{port}/storage"  # another host name: the file storage
        ctx = net.Context(home=tmp_path, timeout_s=5, proxy_for=lambda url: None)
        r = net.fetch_python(ctx, f"http://127.0.0.1:{port}/asset", tmp_path / "out", 1 << 20,
                             accept="application/octet-stream", token=TOKEN)
        assert r.status == 200 and r.read() == b"zip-bytes"
        (_, p1, a1), (h2, p2, a2) = _Recorder.seen
        assert p1 == "/asset" and a1 == f"Bearer {TOKEN}"
        assert p2 == "/storage" and h2.startswith("localhost") and a2 is None  # no token after the redirect
    finally:
        srv.shutdown()


def test_the_redirect_handler_keeps_the_token_only_on_the_api():
    import urllib.request

    h = net._ApiOnlyRedirect(TOKEN)
    req = urllib.request.Request("https://api.github.com/repos/o/r/releases/assets/1")
    req.add_unredirected_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Authorization", "Bearer smuggled")  # even an ordinary header is not carried
    for url, keep in (("https://release-assets.githubusercontent.com/x", False),
                      ("https://api.github.com/repositories/9/releases/assets/1", True),
                      ("http://api.github.com/x", False), ("https://github.com/o/r", False)):
        new = h.redirect_request(req, None, 302, "Found", {}, url)
        got = new.get_header("Authorization") or new.unredirected_hdrs.get("Authorization")
        assert (got == f"Bearer {TOKEN}") is keep and (got is None or got == f"Bearer {TOKEN}"), url


def test_the_token_goes_only_to_the_api(tmp_path, monkeypatch):
    called = []
    monkeypatch.setitem(net.FETCHERS, "python", lambda *a, **kw: called.append(kw) or None)
    ctx = net.Context(home=tmp_path, timeout_s=5, proxy_for=lambda url: None, token=TOKEN)
    for url in ("https://github.com/o/r/releases/latest/download/x", "http://api.github.com/x",
                "https://api.github.com.evil.example/x", "https://user@api.github.com/x"):
        with pytest.raises(net.NetError) as ei:
            net.fetch("python", ctx, url, 1 << 20, auth=True)
        assert ei.value.code == "failed" and TOKEN not in str(ei.value), url
    no = net.Context(home=tmp_path, timeout_s=5, proxy_for=lambda url: None)
    with pytest.raises(net.NetError) as ei:
        net.fetch("python", no, "https://api.github.com/x", 1 << 20, auth=True)
    assert ei.value.code == "no_token" and not called


def test_curl_gets_the_token_in_its_config_file_only(tmp_path, monkeypatch):
    seen = {}

    def runner(cmd, **kw):
        seen["cmd"], seen["cfg"] = cmd, Path(cmd[cmd.index("-K") + 1]).read_text(encoding="utf-8")
        heads = Path(cmd[cmd.index("-D") + 1])
        status = seen["status"]
        heads.write_bytes(f"HTTP/1.1 {status} X\r\nX-GitHub-Request-Id: 1\r\n\r\n".encode())
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"{}")
        return subprocess.CompletedProcess(cmd, 0, f"{status} 000".encode(), f"echo {TOKEN}".encode())

    monkeypatch.setattr(net, "find_curl", lambda: "curl")
    monkeypatch.setattr(net, "curl_version", lambda exe: (8, 4, 0))
    ctx = net.Context(home=tmp_path, timeout_s=5, runner=runner, proxy_for=lambda url: None, token=TOKEN)
    url = "https://api.github.com/repos/o/r/releases/latest"
    seen["status"] = 200
    r = net.fetch("curl", ctx, url, 1 << 20, accept="application/vnd.github+json", auth=True)
    assert r.status == 200
    r.path.unlink()
    assert TOKEN not in " ".join(seen["cmd"]) and f'header = "Authorization: Bearer {TOKEN}"' in seen["cfg"]
    assert not list((tmp_path / "updates/downloads").glob(".dl-*"))  # the config file is gone
    for status, code in ((401, "token_rejected"), (404, "no_access")):
        seen["status"] = status
        with pytest.raises(net.NetError) as ei:
            net.fetch("curl", ctx, url, 1 << 20, auth=True)
        assert ei.value.code == code and TOKEN not in json.dumps(ei.value.public()) and ei.value.public()["it_hint"]
    seen["status"] = 200
    net.fetch("curl", net.Context(home=tmp_path, timeout_s=5, runner=runner, proxy_for=lambda u: None), url, 1 << 20)
    assert "Authorization" not in seen["cfg"]  # no token unless asked
    monkeypatch.setattr(net, "curl_version", lambda exe: (7, 55, 1))  # Windows 10 1803's curl
    with pytest.raises(net.NetError) as ei:
        net.fetch("curl", ctx, url, 1 << 20, auth=True)
    assert ei.value.code == "unavailable" and "7.58" in str(ei.value)


def test_powershell_gets_the_token_in_its_environment_only(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(net, "find_powershell", lambda: "powershell.exe")

    def runner(cmd, **kw):
        seen["cmd"], seen["env"] = cmd, kw["env"]
        Path(kw["env"]["DESK_UPD_OUT"]).write_bytes(b"{}")
        return subprocess.CompletedProcess(cmd, 0, b'{"ok": true, "status": 200}\n', b"")

    ctx = net.Context(home=tmp_path, timeout_s=5, runner=runner, proxy_for=lambda url: None, token=TOKEN)
    net.fetch("powershell", ctx, "https://api.github.com/repos/o/r/releases/assets/7", 1 << 20,
              accept="application/octet-stream", auth=True)
    assert TOKEN not in " ".join(seen["cmd"]) and '"' not in seen["cmd"][4]
    assert seen["env"]["DESK_UPD_TOKEN"] == TOKEN and seen["env"]["DESK_UPD_ACCEPT"] == "application/octet-stream"
    assert "$env:DESK_UPD_TOKEN" in seen["cmd"][4] and "Authorization" in seen["cmd"][4]


# ---- the updater end to end --------------------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("DESK_HOME", str(h))
    monkeypatch.setenv("DESK_ENV_FILE", str(tmp_path / "none.env"))
    for v in [v for v in os.environ if v.startswith("DESK_") and v not in ("DESK_HOME", "DESK_ENV_FILE")]:
        monkeypatch.delenv(v)
    monkeypatch.setenv("DESK_UPDATE_DOWNLOADS_DIR", str(tmp_path / "Downloads"))
    (tmp_path / "Downloads").mkdir()
    monkeypatch.setattr(adapter, "WATCH_POLL_S", 0.05)
    monkeypatch.setattr(net, "available", lambda layer: None)
    with adapter._lock:
        adapter._job = None
    scan.forget_adapters()
    return h


def client(app=None, host: str = "127.0.0.1") -> TestClient:
    return TestClient(app or create_app("postgresql://unused"), base_url="http://127.0.0.1:8000", client=(host, 50000))


class FakeGitHub:
    """``net.FETCHERS`` stand-ins serving a private release by its API addresses, only with the token."""

    def __init__(self, monkeypatch, z: bytes, version: str = "0.5.0", broken: dict[str, str] | None = None):
        tag = f"v{version}"
        doc = json.dumps({"format": 1, "name": "tda", "version": version, "tag": tag, "commit": "c" * 40, "spi": 1,
                          "contract": "private/1.0", "notes": "",
                          "asset": {"name": adapter.ZIP_NAME, "size": len(z), "sha256": sha(z)}}).encode()
        sums = f"{sha(z)}  {adapter.ZIP_NAME}\n{sha(doc)}  {adapter.RELEASE_DOC}\n".encode()
        api = {"tag_name": tag, "draft": False, "prerelease": False, "published_at": "2026-10-29T08:00:00Z",
               "assets": [{"id": 11, "name": adapter.ZIP_NAME, "size": len(z), "digest": "sha256:" + sha(z)},
                          {"id": 12, "name": adapter.RELEASE_DOC, "size": len(doc)},
                          {"id": 13, "name": adapter.SUMS_NAME, "size": len(sums)}]}
        self.urls = {adapter.api_latest_url(REPO): json.dumps(api).encode(),
                     adapter.api_asset_url(REPO, 11): z, adapter.api_asset_url(REPO, 12): doc,
                     adapter.api_asset_url(REPO, 13): sums}
        self.calls: list[tuple[str, str, bool]] = []
        self.broken = broken or {}
        for layer in net.LAYERS:
            monkeypatch.setitem(net.FETCHERS, layer, self.make(layer))

    def make(self, layer):
        def fetch(ctx, url, dest, max_bytes, accept=None, progress=None, token=None):
            assert token is None or url.startswith("https://api.github.com/")
            self.calls.append((layer, url, token == TOKEN))
            if layer in self.broken:
                raise net.NetError(self.broken[layer], f"{layer} broken", layer)
            if token != TOKEN:
                raise net.NetError("not_found", "404", layer, 404)  # a private repository without the token
            if "/assets/" in url:
                assert accept == adapter.ASSET_ACCEPT
            dest.write_bytes(self.urls[url])
            return net.Result(200, {}, dest, len(self.urls[url]))
        return fetch


def _wait(states=("done", "failed", "cancelled"), timeout=30.0) -> dict:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        j = adapter.job()
        if j and j["state"] in states:
            return j
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {states}: {adapter.job()}")


def _no_token_anywhere(home: Path, *texts: str) -> None:
    for t in texts:
        assert TOKEN not in t
    for p in home.rglob("*"):
        if p.is_file() and p.name != "github_token":
            assert TOKEN.encode() not in p.read_bytes(), p


def test_check_and_server_install_switch_the_adapter_without_a_restart(home, tmp_path, monkeypatch):
    z = zip_bytes(tmp_path)
    gh = FakeGitHub(monkeypatch, z, broken={"python": "tls_untrusted"})
    monkeypatch.setenv("DESK_GITHUB_TOKEN", TOKEN)
    c = client()
    st = c.post("/api/v1/update/adapter/check", json={}).json()
    assert st["check"]["ok"] and st["check"]["layer"] == "curl" and st["release"]["version"] == "0.5.0"
    assert st["update_available"] and st["token_set"] and st["source"] == "reference" and st["release"]["sums_checked"]
    r = c.post("/api/v1/update/adapter/install", json={})
    assert r.status_code == 202 and r.json()["job"]["via"] == "server"
    j = _wait()
    assert j["state"] == "done", j
    cur = adapter_store.read_current(home)
    assert cur["version"] == "0.5.0" and cur["package"] == f"0.5.0/{PKG}" and cur["previous"] is None
    assert cur["sha256"] == sha(z) and cur["source"] == "server"
    pkg = home / "adapter-releases/0.5.0" / PKG
    assert (pkg / "_acquisition/system2/run.py").is_file() and (home / "adapter-releases/0.5.0/README.md").is_file()
    assert (home / "adapter-releases/0.5.0" / adapter.MANIFEST).is_file()
    s = load()
    assert s.private_adapter == str(pkg) and s.private_adapter_source == "installed" and not s.private_adapter_default
    h = scan.adapter(s)
    assert h.ok and h.version == "0.5.0"
    page = c.get("/api/v1/update/adapter").json()
    assert page["installed"]["version"] == "0.5.0" and page["in_use"]["version"] == "0.5.0"
    assert page["compare"] == "same" and not page["update_available"]
    assert all(tok for _, url, tok in gh.calls)  # every request was an authenticated API one
    log = (home / "logs/update.log").read_text(encoding="utf-8")
    assert "Адаптер 0.5.0 установлен" in log
    _no_token_anywhere(home, json.dumps(page), json.dumps(j), c.get("/api/v1/settings").text)
    assert c.post("/api/v1/update/adapter/install", json={}).status_code == 409  # the same version: only when asked


def test_without_access_or_with_a_rejected_token(home, tmp_path, monkeypatch):
    FakeGitHub(monkeypatch, zip_bytes(tmp_path))
    c = client()
    st = c.post("/api/v1/update/adapter/check", json={}).json()
    assert not st["check"]["ok"] and st["check"]["error"]["code"] == "no_token"
    monkeypatch.setenv("DESK_GITHUB_TOKEN", "github_pat_" + "Z" * 40)  # a token without access: 404
    st = c.post("/api/v1/update/adapter/check", json={}).json()
    assert st["check"]["ok"] and st["no_release"] and "нет доступа" in st["check"]["note"]

    def rejected(ctx, url, dest, max_bytes, accept=None, progress=None, token=None):
        raise net.http_error("python", url, 401, {})

    for layer in net.LAYERS:
        monkeypatch.setitem(net.FETCHERS, layer, rejected)
    st = c.post("/api/v1/update/adapter/check", json={}).json()
    err = st["check"]["error"]
    assert err["code"] == "token_rejected" and "401" in err["what"] and len(st["check"]["attempts"]) == 1


def _install(c: TestClient, z: bytes, allow_older: bool = False):
    return c.post("/api/v1/update/adapter/upload", params={"allow_older": "1"} if allow_older else None, content=z,
                  headers={"Content-Type": "application/zip"})


def test_upload_rollback_and_the_bundle(home, tmp_path, monkeypatch):
    c = client()
    r = _install(c, zip_bytes(tmp_path, "0.5.0"))
    assert r.status_code == 200 and r.json()["version"] == "0.5.0", r.text
    r = _install(c, zip_bytes(tmp_path, "0.5.1"))
    assert r.json()["previous"] == "0.5.0" and r.json()["state"]["previous_available"]
    assert load().private_adapter.endswith(f"0.5.1{os.sep}{PKG}")
    r = c.post("/api/v1/update/adapter/rollback", json={"to": "previous"})
    assert r.status_code == 200 and r.json()["version"] == "0.5.0"
    cur = adapter_store.read_current(home)
    assert cur["version"] == "0.5.0" and cur["previous"] == "0.5.1" and load().private_adapter.endswith(
        f"0.5.0{os.sep}{PKG}")
    older = _install(c, zip_bytes(tmp_path, "0.4.0"))
    assert older.status_code == 409 and older.json()["details"]["needs_confirm"] and "старее" in older.json()["message"]
    assert _install(c, zip_bytes(tmp_path, "0.4.0"), allow_older=True).json()["version"] == "0.4.0"
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "bundle"}).status_code == 200
    assert not adapter_store.current_path(home).exists() and load().private_adapter_source == "reference"
    assert (home / "adapter-releases/0.5.1").is_dir()  # the folders stay
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "previous"}).status_code == 422
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "x"}).status_code == 422
    assert "Адаптер: возврат" in (home / "logs/update.log").read_text(encoding="utf-8")


def test_settings_resolution_explicit_installed_bundle(home, tmp_path, monkeypatch):
    c = client()
    _install(c, zip_bytes(tmp_path, "0.5.0"))
    installed = str(home / "adapter-releases/0.5.0" / PKG)
    bundle = tmp_path / "DeskMonitor/adapter/tda"
    bundle.mkdir(parents=True)
    monkeypatch.setenv("DESK_DEFAULT_PRIVATE_ADAPTER", str(bundle))
    for text, source in (('__version__ = "0.6.0"\n', "bundle"), ('__version__ = "0.4.9"\n', "installed"),
                         ('__version__ = "0.5.0"\n', "installed"), ("VERSION = 'x'\n", "installed")):
        (bundle / "__init__.py").write_text(text, encoding="utf-8")
        s = load()
        assert s.private_adapter_source == source, text
        assert s.private_adapter == (str(bundle) if source == "bundle" else installed)
        assert s.private_adapter_default is (source == "bundle")
    (bundle / "__init__.py").write_text('__version__ = "0.6.0"\n', encoding="utf-8")
    assert c.get("/api/v1/update/adapter").json()["base_version"] == "0.6.0"  # a release is compared with the bundle
    (home / "settings.json").write_text(json.dumps({"private_adapter": str(tmp_path / "mine")}), encoding="utf-8")
    s = load()
    assert s.private_adapter == str(tmp_path / "mine") and s.private_adapter_source == "settings"
    page = c.get("/api/v1/update/adapter").json()
    assert page["explicit"] and "не используется" in page["explicit_note"]
    monkeypatch.setenv("DESK_PRIVATE_ADAPTER", "/env/tda")
    assert load().private_adapter_source == "env"
    monkeypatch.delenv("DESK_PRIVATE_ADAPTER")
    (home / "settings.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(home / "adapter-releases/0.5.0")  # current.json points to a folder that is gone: the bundle again
    assert load().private_adapter_source == "bundle"


def test_a_loader_failure_or_a_pinned_fingerprint_leaves_the_adapter_in_use(home, tmp_path):
    c = client()
    _install(c, zip_bytes(tmp_path, "0.5.0"))
    before = adapter_store.current_path(home).read_text(encoding="utf-8")
    r = _install(c, make_zip(tmp_path / "n" / adapter.ZIP_NAME, pkg_files("0.6.0", contract="private/9.0"),
                             version="0.6.0").read_bytes())
    assert r.status_code == 422 and "более нового контракта" in r.json()["message"] and "обновите программу" in \
        r.json()["message"]
    assert adapter_store.current_path(home).read_text(encoding="utf-8") == before
    assert not (home / "adapter-releases/0.6.0").exists()
    assert "не установлен" in (home / "logs/update.log").read_text(encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({"private_adapter_sha256": "a" * 64}), encoding="utf-8")
    r = _install(c, zip_bytes(tmp_path, "0.6.1"))
    assert r.status_code == 422 and "Отпечаток адаптера" in r.json()["message"]
    assert c.get("/api/v1/update/adapter").json()["pinned"]
    assert adapter_store.current_path(home).read_text(encoding="utf-8") == before


def test_a_damaged_upload_is_refused(home, tmp_path):
    c = client()
    r = _install(c, b"not a zip")
    assert r.status_code == 422 and "не zip" in r.json()["message"]
    assert not list(adapter.uploads_dir(home).glob("*"))  # the upload is not kept


def test_only_the_newest_three_versions_stay(home, tmp_path):
    c = client()
    for i, v in enumerate(("0.1.0", "0.2.0", "0.3.0", "0.4.0", "0.5.0")):
        assert _install(c, zip_bytes(tmp_path, v)).status_code == 200
        os.utime(home / "adapter-releases" / v, (time.time() - 100 + i, time.time() - 100 + i))
    adapter.prune(home)
    assert sorted(p.name for p in (home / "adapter-releases").iterdir() if p.is_dir()) == ["0.3.0", "0.4.0", "0.5.0"]
    c.post("/api/v1/update/adapter/rollback", json={"to": "previous"})  # 0.4.0 now, 0.5.0 previous
    _install(c, zip_bytes(tmp_path, "0.6.0"))
    kept = sorted(p.name for p in (home / "adapter-releases").iterdir() if p.is_dir())
    assert "0.4.0" in kept and "0.6.0" in kept and len(kept) == 3


def test_the_browser_fallback_watches_the_downloads_folder(home, tmp_path, monkeypatch):
    downloads = tmp_path / "Downloads"
    old = downloads / adapter.ZIP_NAME
    old.write_bytes(zip_bytes(tmp_path, "0.4.0"))
    os.utime(old, (time.time() - 3600, time.time() - 3600))  # saved before the click: not taken
    r = client().post("/api/v1/update/adapter/install", json={"via": "browser"})
    assert r.status_code == 202
    j = _wait(("waiting_browser",))
    assert j["browser_url"] == f"https://github.com/{REPO}/releases/latest/download/tda-adapter.zip"
    time.sleep(0.2)
    assert adapter.job()["state"] == "waiting_browser"  # the old file is not the one
    (downloads / "tda-adapter (1).zip").write_bytes(b"half a download")  # a damaged copy: skipped, said why
    time.sleep(0.2)
    assert adapter.job()["found"]["state"] == "mismatch"
    (downloads / "tda-adapter (2).zip").write_bytes(zip_bytes(tmp_path, "0.5.0"))
    j = _wait()
    assert j["state"] == "done", j
    assert adapter_store.read_current(home)["source"] == "browser"
    assert (downloads / "tda-adapter (2).zip").is_file()  # the owner's file stays where the browser saved it


def test_cancel_stops_the_wait(home):
    client().post("/api/v1/update/adapter/install", json={})
    _wait(("waiting_browser",))
    assert client().post("/api/v1/update/adapter/cancel", json={}).status_code == 200
    assert _wait(("cancelled",))["state"] == "cancelled"


def test_find_in_downloads_and_install_found(home, tmp_path):
    downloads = tmp_path / "Downloads"
    (downloads / adapter.ZIP_NAME).write_bytes(zip_bytes(tmp_path, "0.5.0"))
    (downloads / "tda-adapter(1).zip").write_bytes(b"junk")
    c = client()
    found = c.get("/api/v1/update/adapter/downloads").json()
    items = {i["file"]: i for i in found["items"]}
    assert items[adapter.ZIP_NAME]["ok"] and items[adapter.ZIP_NAME]["version"] == "0.5.0"
    assert not items["tda-adapter(1).zip"]["ok"] and items["tda-adapter(1).zip"]["reason"]
    body = {"file": adapter.ZIP_NAME, "sha256": items[adapter.ZIP_NAME]["sha256"]}
    assert c.post("/api/v1/update/adapter/install-found", json={**body, "sha256": "0" * 64}).status_code == 422
    assert c.post("/api/v1/update/adapter/install-found", json={**body, "file": "../x.zip"}).status_code == 422
    r = c.post("/api/v1/update/adapter/install-found", json=body)
    assert r.status_code == 200 and r.json()["version"] == "0.5.0"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_token_route_never_returns_the_token(home, tmp_path):
    c = client()
    r = c.put("/api/v1/settings/github-token", json={"token": TOKEN})
    assert r.status_code == 200 and r.json() == {"github_token_set": True, "github_token_source": "file"}
    assert (home / "github_token").read_text(encoding="utf-8").strip() == TOKEN
    if os.name != "nt":
        assert (home / "github_token").stat().st_mode & 0o077 == 0
    s = c.get("/api/v1/settings").json()
    assert s["github_token_set"] and s["effective"]["github_token_source"] == "file" and TOKEN not in json.dumps(s)
    st = c.get("/api/v1/update/adapter").json()
    assert st["token_set"] and st["token_source"] == "file" and TOKEN not in json.dumps(st)
    assert c.put("/api/v1/settings/github-token", json={"token": "has space x" * 3}).status_code == 422
    assert c.put("/api/v1/settings", json={"github_token": TOKEN}).status_code == 422
    assert c.put("/api/v1/settings/github-token", json={"token": ""}).json()["github_token_set"] is False
    assert not (home / "github_token").exists()
    st = load()
    assert st.github_token is None and "github_token" not in repr(st).replace("github_token_source", "")


def test_the_token_is_scrubbed_from_diagnostics(home, monkeypatch):
    monkeypatch.setenv("DESK_GITHUB_TOKEN", TOKEN)
    st = load()
    assert st.github_token == TOKEN and st.github_token_source == "DESK_GITHUB_TOKEN"
    assert TOKEN not in diagnostics.scrubber(st, None).text(f"error: {TOKEN}")
    section = diagnostics.settings_section(st, None, diagnostics.scrubber(st, None))
    assert section["github_token"] == "set" and section["github_token_source"] == "env"
    assert TOKEN not in json.dumps(section, default=str) and section["private_adapter_source"] == "reference"
    assert TOKEN not in json.dumps(dict(st.describe()))


def test_routes_are_local_only(home, tmp_path):
    app = create_app("postgresql://unused")
    remote = client(app, host="10.1.2.3")
    for method, path in (("get", "/api/v1/update/adapter"), ("post", "/api/v1/update/adapter/check"),
                         ("post", "/api/v1/update/adapter/install"), ("post", "/api/v1/update/adapter/rollback"),
                         ("get", "/api/v1/update/adapter/downloads"), ("post", "/api/v1/update/adapter/install-found"),
                         ("put", "/api/v1/settings/github-token")):
        assert getattr(remote, method)(path, **({} if method == "get" else {"json": {}})).status_code == 403, path
    r = remote.post("/api/v1/update/adapter/upload", content=b"x", headers={"Content-Type": "application/zip"})
    assert r.status_code == 403
    local = client(app)
    assert local.post("/api/v1/update/adapter/upload", content=b"x",
                      headers={"Content-Type": "text/plain"}).status_code in (403, 415)  # not a zip type
    assert local.post("/api/v1/update/adapter/rollback", json={"to": "bundle"},
                      headers={"Origin": "https://evil.example"}).status_code == 403


# ---- a folder or file in use on Windows (TDM#519), simulated --------------------------------------------------


def win_busy(code: int = 32, filename: str | None = None) -> PermissionError:
    """What Windows raises for a file an antivirus, the indexer or an Explorer window holds."""
    e = PermissionError(13, "The process cannot access the file because it is being used by another process", filename)
    e.winerror = code  # type: ignore[attr-defined]
    return e


def _raise(e: BaseException):
    raise e


@pytest.fixture
def pauses(monkeypatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(adapter, "_sleep", slept.append)
    return slept


def _other_build(tmp_path: Path, version: str = "0.5.0") -> bytes:
    """The same version with other files: another build, or what a failed attempt left."""
    return make_zip(tmp_path / f"other-{version}" / adapter.ZIP_NAME,
                    {**pkg_files(version), f"src/{PKG}/extra.py": b"y = 2\n"}, version=version).read_bytes()


def _folders(home: Path) -> list[str]:
    return sorted(p.name for p in (home / "adapter-releases").iterdir() if p.is_dir())


def test_retry_waits_while_busy_and_gives_up_after_the_budget(pauses):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) <= 3:
            raise win_busy()
        return "ok"

    assert adapter.retry(flaky) == "ok" and len(calls) == 4 and pauses == [0.1, 0.2, 0.4]
    with pytest.raises(FileNotFoundError):  # not "in use": no retry
        adapter.retry(lambda: _raise(FileNotFoundError(2, "gone")))
    pauses.clear()
    with pytest.raises(PermissionError):
        adapter.retry(lambda: _raise(win_busy(5)))
    assert abs(sum(pauses) - adapter.BUSY_WAIT_S) < 1e-6 and max(pauses) <= 2.0
    e = win_busy()
    assert adapter.busy(e) and adapter.os_code(e) == "PermissionError WinError 32"
    assert adapter.busy(OSError(16, "busy")) and not adapter.busy(OSError(2, "gone"))


def test_a_busy_current_json_is_retried_and_the_install_goes_through(home, tmp_path, monkeypatch, pauses):
    real = os.replace
    left = {"n": 3}

    def replace(src, dst, *a, **kw):
        if str(dst).endswith(adapter_store.CURRENT_FILE) and left["n"] > 0:
            left["n"] -= 1
            raise win_busy()  # the settings read current.json at that moment, an antivirus scans it
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(os, "replace", replace)
    r = _install(client(), zip_bytes(tmp_path, "0.5.0"))
    assert r.status_code == 200, r.text
    assert left["n"] == 0 and pauses == [0.1, 0.2, 0.4]
    assert adapter_store.read_current(home)["package"] == f"0.5.0/{PKG}"
    assert not list((home / "adapter-releases").glob(".current.json.*"))  # no temporary file left


def test_when_it_stays_busy_the_page_says_what_and_what_to_do(home, tmp_path, monkeypatch, pauses):
    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    before = adapter_store.current_path(home).read_text(encoding="utf-8")
    real_write = adapter.atomic_write_text
    monkeypatch.setattr(adapter, "atomic_write_text", lambda *a, **kw: _raise(win_busy()))
    r = _install(c, zip_bytes(tmp_path, "0.5.1"))
    assert r.status_code == 422
    msg = r.json()["message"]
    assert "заняты" in msg and "WinError 32" in msg and "Проводника" in msg and "stop.bat" in msg
    assert "start.bat" in msg and "Работает прежний адаптер" in msg
    assert abs(sum(pauses) - adapter.BUSY_WAIT_S) < 1e-6
    assert adapter_store.current_path(home).read_text(encoding="utf-8") == before
    assert load().private_adapter.endswith(f"0.5.0{os.sep}{PKG}")
    log = (home / "logs/update.log").read_text(encoding="utf-8")
    assert "current.json не записан — PermissionError WinError 32" in log and str(home) not in log
    monkeypatch.setattr(adapter, "atomic_write_text", real_write)  # free again: what was unpacked is used
    unpacked = []
    monkeypatch.setattr(adapter, "_unpack", lambda *a: unpacked.append(a))
    assert _install(c, zip_bytes(tmp_path, "0.5.1")).json()["version"] == "0.5.1" and not unpacked
    assert _folders(home) == ["0.5.0", "0.5.1"]


def test_an_unpack_failure_says_what_is_busy_and_the_next_try_works(home, tmp_path, monkeypatch, pauses):
    c = client()
    real = adapter._unpack

    def busy_unpack(path, info, dest):
        (dest / PKG).mkdir(parents=True)
        (dest / PKG / "__init__.py").write_bytes(b"half")
        raise win_busy(filename=str(dest / PKG / "__init__.py"))

    monkeypatch.setattr(adapter, "_unpack", busy_unpack)
    r = _install(c, zip_bytes(tmp_path, "0.5.0"))
    msg = r.json()["message"]
    assert r.status_code == 422 and "заняты" in msg and "распаковать" in msg
    assert f"Занят: {home / 'adapter-releases/0.5.0' / PKG / '__init__.py'}." in msg  # which file
    assert adapter_store.read_current(home) is None and _folders(home) == []  # the half folder went
    assert "распаковка в adapter-releases/0.5.0 — PermissionError WinError 32" in \
        (home / "logs/update.log").read_text(encoding="utf-8")
    monkeypatch.setattr(adapter, "_unpack", real)
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200


def test_a_folder_that_never_frees_is_left_and_the_release_goes_beside_it(home, tmp_path, monkeypatch, pauses):
    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    held = home / "adapter-releases/0.5.0"
    real_rename = Path.rename

    def rename(self, target):
        if Path(self) == held:
            raise win_busy()  # an Explorer window, a scan's child, an antivirus: never let go
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    z = _other_build(tmp_path)
    assert _install(c, z).status_code == 409  # the same version: only when asked
    r = _install(c, z, allow_older=True)
    assert r.status_code == 200, r.text
    folder = f"0.5.0+{sha(z)[:8]}"
    cur = adapter_store.read_current(home)
    assert cur["version"] == "0.5.0" and cur["package"] == f"{folder}/{PKG}"
    assert cur["previous"] == "0.5.0" and cur["previous_package"] == f"0.5.0/{PKG}"
    assert (home / "adapter-releases" / folder / PKG / "extra.py").is_file()
    assert not (held / PKG / "extra.py").exists() and (held / PKG / "__init__.py").is_file()  # untouched
    s = load()
    assert s.private_adapter == str(home / "adapter-releases" / folder / PKG)
    assert s.private_adapter_source == "installed"
    page = c.get("/api/v1/update/adapter").json()
    assert page["previous_available"]
    assert {"version": "0.5.0", "folder": folder} in [{k: v[k] for k in ("version", "folder")} for v in page["versions"]]
    for i, v in enumerate(("0.5.1", "0.5.2", "0.5.3")):  # 0.5.0 is old now, but still held: prune skips it quietly
        assert _install(c, zip_bytes(tmp_path, v)).status_code == 200
        os.utime(home / "adapter-releases" / v, (time.time() + i + 1, time.time() + i + 1))
    assert "0.5.0" in _folders(home) and folder not in _folders(home)
    monkeypatch.setattr(Path, "rename", real_rename)
    adapter.prune(home)
    assert _folders(home) == ["0.5.1", "0.5.2", "0.5.3"]  # freed: gone at the next prune


def test_reinstalling_the_same_release_reuses_its_folder(home, tmp_path, monkeypatch):
    c = client()
    z = zip_bytes(tmp_path, "0.5.0")
    assert _install(c, z).status_code == 200
    assert (home / "adapter-releases/0.5.0" / PKG / "__pycache__").is_dir()  # the installer's bytecode
    (home / "adapter-releases/0.5.0" / PKG / "__pycache__/old.cpython-311.pyc").write_bytes(b"x")  # and a stray one
    unpacked = []
    monkeypatch.setattr(adapter, "_unpack", lambda *a: unpacked.append(a))
    r = _install(c, z, allow_older=True)
    assert r.status_code == 200 and r.json()["version"] == "0.5.0" and not unpacked
    cur = adapter_store.read_current(home)
    assert cur["package"] == f"0.5.0/{PKG}" and cur["previous"] is None and _folders(home) == ["0.5.0"]


def test_an_install_right_after_a_failed_attempt(home, tmp_path):
    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    left = home / "adapter-releases/0.5.1"  # a failed attempt's leftover: a file missing
    shutil.copytree(home / "adapter-releases/0.5.0", left)
    man = json.loads((left / adapter.MANIFEST).read_text(encoding="utf-8"))
    man["version"] = "0.5.1"
    (left / adapter.MANIFEST).write_text(json.dumps(man), encoding="utf-8")
    (left / PKG / "_acquisition/system2/run.py").unlink()
    r = _install(c, zip_bytes(tmp_path, "0.5.1"))
    assert r.status_code == 200, r.text
    pkg = adapter_store.installed(home)[0]
    assert pkg.parent.name.startswith("0.5.1+") and (pkg / "_acquisition/system2/run.py").is_file()
    h = scan.adapter(load())
    assert h.ok and h.version == "0.5.1"
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "previous"}).json()["version"] == "0.5.0"
    cur = adapter_store.read_current(home)
    assert cur["package"] == f"0.5.0/{PKG}" and cur["previous_package"] == f"{pkg.parent.name}/{PKG}"
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "previous"}).json()["version"] == "0.5.1"
    assert adapter_store.installed(home)[0] == pkg


def test_a_current_json_of_the_older_version_still_rolls_back(home, tmp_path):
    c = client()
    _install(c, zip_bytes(tmp_path, "0.5.0"))
    _install(c, zip_bytes(tmp_path, "0.5.1"))
    doc = json.loads(adapter_store.current_path(home).read_text(encoding="utf-8"))
    del doc["previous_package"]  # written by 0.2.13
    adapter_store.current_path(home).write_text(json.dumps(doc), encoding="utf-8")
    assert c.get("/api/v1/update/adapter").json()["previous_available"]
    assert c.post("/api/v1/update/adapter/rollback", json={"to": "previous"}).json()["version"] == "0.5.0"


def test_the_switch_ends_the_warm_child_and_the_next_call_uses_the_new_folder(home, tmp_path):
    from desk import settings_forms
    from desk.inbox import runner

    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    old = scan.adapter(load())
    assert old.ok and old.spec.endswith(f"0.5.0{os.sep}{PKG}")
    w = runner.Worker()  # the settings forms' warm child, on the old adapter
    assert runner.call_reference(old, None, 30, worker=w) == {"books": []}
    settings_forms._warm.update(worker=w, key=("old",), used=time.monotonic())
    assert old.spec in runner.specs_in_use()
    scan_child = runner.Worker()  # an inbox scan still running on the old adapter: never ended by the switch
    runner.call_reference(old, None, 30, worker=scan_child)
    try:
        assert _install(c, zip_bytes(tmp_path, "0.5.1")).status_code == 200
        assert settings_forms._warm["worker"] is None and w._p is None  # ended
        assert scan_child._p is not None and scan_child._p.poll() is None  # still running
        new = scan.adapter(load())
        assert new.ok and new.version == "0.5.1" and new.spec.endswith(f"0.5.1{os.sep}{PKG}")
        assert runner.call_reference(new, None, 30) == {"books": []}
        for i, v in enumerate(("0.5.2", "0.5.3", "0.5.4")):  # 0.5.0: neither current nor previous, but in use
            assert _install(c, zip_bytes(tmp_path, v)).status_code == 200
            os.utime(home / "adapter-releases" / v, (time.time() + i + 1, time.time() + i + 1))
        assert "0.5.0" in _folders(home) and "0.5.1" not in _folders(home)
    finally:
        scan_child.close()
    assert old.spec not in runner.specs_in_use()
    adapter.prune(home)
    assert "0.5.0" not in _folders(home)


def _snapshot(folder: Path) -> dict[str, tuple[int, int]]:
    return {str(p.relative_to(folder)): (p.stat().st_mtime_ns, p.stat().st_size) for p in folder.rglob("*")}


def test_the_installer_precompiles_and_the_children_write_nothing_into_the_folder(home, tmp_path):
    from desk.inbox import loader, runner

    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    folder = home / "adapter-releases/0.5.0"
    pkg = folder / PKG
    sources = sorted(p for p in pkg.rglob("*.py"))
    assert sources and all(list(p.parent.glob(f"__pycache__/{p.stem}.*.pyc")) for p in sources)  # precompiled
    before = _snapshot(folder)
    h = scan.adapter(load())
    runner.call_reference(h, None, 30)  # a one-shot child, and a worker, import the adapter from there
    w = runner.Worker()
    try:
        runner.call_reference(h, None, 30, worker=w)
    finally:
        w.close()
    assert _snapshot(folder) == before  # read, never written
    assert h.fingerprint == loader.fingerprint(str(pkg), None)  # __pycache__ is not the adapter's files
    for d in list(pkg.rglob("__pycache__")):  # a folder without bytecode (an older install, the bundle's adapter):
        shutil.rmtree(d)                       # the children compile in memory and write nothing either
    scan.forget_adapters()
    runner.call_reference(scan.adapter(load()), None, 30)
    assert not list(folder.rglob("__pycache__")) and not list(folder.rglob("*.pyc"))


def test_a_failed_precompile_is_logged_and_the_install_goes_on(home, tmp_path, monkeypatch):
    real = subprocess.run

    def run(cmd, *a, **kw):
        if "compileall" in cmd:
            return subprocess.CompletedProcess(cmd, 1)
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    assert _install(client(), zip_bytes(tmp_path, "0.5.0")).status_code == 200
    assert adapter_store.read_current(home)["version"] == "0.5.0"
    assert "предкомпиляция не удалась (код 1)" in (home / "logs/update.log").read_text(encoding="utf-8")


def test_prune_after_an_install_keeps_the_new_and_previous_folders_whatever_current_json_reads(
        home, tmp_path, monkeypatch):
    c = client()
    assert _install(c, zip_bytes(tmp_path, "0.5.0")).status_code == 200
    assert _install(c, zip_bytes(tmp_path, "0.5.1")).status_code == 200
    for i, v in enumerate(("0.6.0", "0.7.0", "0.8.0")):  # newer by time: the newest three are these
        (home / "adapter-releases" / v).mkdir()
        os.utime(home / "adapter-releases" / v, (time.time() + 100 + i, time.time() + 100 + i))
    real_write, real_read = adapter._write_current, adapter_store.read_current
    gone = {"now": False}

    def write(h, doc):
        real_write(h, doc)
        gone["now"] = True  # from here on current.json does not read (a moment's sharing violation)

    monkeypatch.setattr(adapter, "_write_current", write)
    monkeypatch.setattr(adapter_store, "read_current", lambda h: None if gone["now"] else real_read(h))
    r = _install(c, zip_bytes(tmp_path, "0.5.2"))
    assert r.status_code == 200, r.text
    monkeypatch.undo()
    assert _folders(home) == ["0.5.1", "0.5.2", "0.6.0", "0.7.0", "0.8.0"]  # 0.5.0 pruned, current and previous kept
    cur = adapter_store.read_current(home)
    assert cur["package"] == f"0.5.2/{PKG}" and cur["previous_package"] == f"0.5.1/{PKG}"


def test_a_server_download_held_by_an_antivirus_never_fails_the_install(home, tmp_path, monkeypatch, pauses):
    FakeGitHub(monkeypatch, zip_bytes(tmp_path))
    monkeypatch.setenv("DESK_GITHUB_TOKEN", TOKEN)
    c = client()
    c.post("/api/v1/update/adapter/check", json={})
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self.name.startswith("tda-adapter-"):
            raise win_busy()  # still scanned after the install read it
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert c.post("/api/v1/update/adapter/install", json={}).status_code == 202
    j = _wait()
    assert j["state"] == "done", j
    assert adapter_store.read_current(home)["version"] == "0.5.0"
