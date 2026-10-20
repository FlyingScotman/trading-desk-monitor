"""Hub transports (``hub_transport``): the urllib path's 407 diagnostics, the curl transport against the stub hub,
proxy logins for both, and ``auto`` switching to curl when the proxy refuses Python's tunnel. Local only: an
in-process stub hub (``test_slice_s3``) and an in-process fake proxy."""

from __future__ import annotations

import base64
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_slice_s3 import MARKET, STATIC, TOKEN, Handler, Stub

from desk.marketdata import hub
from desk.marketdata.hub import CurlTransport, HubClient, HubError, PythonTransport, hub_check
from desk.settings import Settings

FIX = Path(__file__).parent / "fixtures" / "mdhub_v1"
needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="needs the curl command")
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy", "ALL_PROXY",
              "all_proxy")


class FakeProxy(socketserver.ThreadingTCPServer):
    """CONNECT tunnels and absolute-URI forwarding; ``accept``: Proxy-Authorization values let through (None:
    everyone), ``challenge``: Proxy-Authenticate values of its 407."""

    daemon_threads = True
    allow_reuse_address = True

    def reset(self, accept: set[str] | None = None, challenge: tuple[str, ...] = ("Basic realm=\"corp\"",)) -> None:
        self.accept, self.challenge, self.seen = accept, challenge, []


def _relay(a: socket.socket, b: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t = threading.Thread(target=pump, args=(b, a), daemon=True)
    t.start()
    pump(a, b)
    t.join(10)


class ProxyHandler(socketserver.BaseRequestHandler):
    server: FakeProxy

    def handle(self) -> None:
        conn = self.request
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = conn.recv(65536)
            if not chunk:
                return
            head += chunk
        head, rest = head.split(b"\r\n\r\n", 1)
        line, *hdrs = head.decode("latin-1").split("\r\n")
        method, target, _ = line.split(" ", 2)
        headers = [h.split(":", 1) for h in hdrs if ":" in h]
        auth = next((v.strip() for k, v in headers if k.lower() == "proxy-authorization"), None)
        self.server.seen.append((method, auth))
        if self.server.accept is not None and auth not in self.server.accept:
            out = "HTTP/1.1 407 Proxy Authentication Required\r\n"
            out += "".join(f"Proxy-Authenticate: {c}\r\n" for c in self.server.challenge)
            conn.sendall((out + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            return
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            up = socket.create_connection((host, int(port)))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                up.sendall(rest)
        else:
            u = urlsplit(target)
            up = socket.create_connection((u.hostname, u.port or 80))
            keep = "".join(f"{k}:{v}\r\n" for k, v in headers
                           if not k.lower().startswith("proxy-") and k.lower() != "connection")
            up.sendall(f"{method} {u.path}{'?' + u.query if u.query else ''} HTTP/1.1\r\n{keep}"
                       f"Connection: close\r\n\r\n".encode("latin-1") + rest)
        _relay(conn, up)


@pytest.fixture(scope="module")
def proxy_server():
    srv = FakeProxy(("127.0.0.1", 0), ProxyHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def proxy(proxy_server, monkeypatch):
    proxy_server.reset()
    for v in PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    url = f"http://127.0.0.1:{proxy_server.server_address[1]}"
    monkeypatch.setenv("HTTPS_PROXY", url)
    monkeypatch.setenv("HTTP_PROXY", url)
    return proxy_server


@pytest.fixture(scope="module")
def hub_server():
    srv = Stub(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


@pytest.fixture
def stub(hub_server, monkeypatch):
    hub_server.reset()
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    monkeypatch.setattr(hub, "_auto_curl", False)
    return hub_server


@pytest.fixture(scope="module")
def tls_hub(tmp_path_factory):
    """The stub hub over TLS with a self-signed certificate (``hub_ca_bundle``), for CONNECT tunnels."""
    if shutil.which("openssl") is None:
        pytest.skip("needs the openssl command for a test certificate")
    d = tmp_path_factory.mktemp("tls")
    cert, key = d / "hub.pem", d / "hub.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
                    "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out",
                    str(cert)], check=True, capture_output=True)
    srv = Stub(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, cert
    srv.shutdown()
    srv.server_close()


def cfg(url: str, home: Path, **kw) -> Settings:
    return Settings(**{"hub_url": url, "hub_token": TOKEN, "hub_timeout_s": 5, "home": home, **kw})


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


# ---- python: 407 diagnostics --------------------------------------------------------------------------------


def test_python_407_names_the_proxy_schemes(proxy, tmp_path, monkeypatch):
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    proxy.reset(accept=set(), challenge=("Negotiate", "NTLM", 'Basic realm="corp", charset="UTF-8"'))
    c = HubClient(cfg("https://127.0.0.1:9", tmp_path, hub_transport="python"))
    with pytest.raises(HubError) as e:
        c.health()
    assert e.value.status == 407 and "прокси требует: Negotiate, NTLM, Basic" in str(e.value)
    assert [m for m, _ in proxy.seen] == ["CONNECT", "CONNECT"]  # the tunnel and the probe; 407 is not retried
    r = hub_check(cfg("https://127.0.0.1:9", tmp_path, hub_transport="python", proxy_user="u", proxy_password="pw"))
    assert "Negotiate" in r["error"] and r["transport"] == "python" and "pw" not in r["error"]
    assert TOKEN not in r["error"]


# ---- curl against the stub hub ------------------------------------------------------------------------------


@needs_curl
def test_curl_transport_success_errors_and_secrets(stub, tmp_path, monkeypatch):
    for v in PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    calls: list[tuple[list[str], str]] = []
    real = subprocess.run

    def spy(cmd, **kw):
        if "-K" in cmd:
            calls.append((list(cmd), Path(cmd[cmd.index("-K") + 1]).read_text(encoding="utf-8")))
        return real(cmd, **kw)

    monkeypatch.setattr(hub.subprocess, "run", spy)
    url = f"http://127.0.0.1:{stub.server_address[1]}"
    c = HubClient(cfg(url, tmp_path, hub_transport="curl"))
    assert isinstance(c.transport, CurlTransport)
    assert c.health()["status"] == "ok"
    assert [r["snapshot_id"] for r in c.list_snapshots(kind="market", latest=True)] == [MARKET]
    assert c.bundle(STATIC) == (FIX / f"{STATIC}.json.gz").read_bytes()
    stub.fail = 2  # 503 twice, then success: retried
    assert c.manifest(MARKET)["meta"]["superseded_by"] is None
    with pytest.raises(HubError, match="token rejected .*401"):
        HubClient(cfg(url, tmp_path, hub_transport="curl", hub_token="wrong")).manifest(MARKET)
    stub.snapshots = False
    with pytest.raises(HubError, match="no snapshot layer"):
        list(c.list_snapshots())
    assert calls and all(TOKEN not in " ".join(argv) for argv, _ in calls)  # never on the command line
    assert all("Authorization: Bearer" in text and 'noproxy = "*"' in text for argv, text in calls[1:3])
    assert not list(tmp_path.glob(".curl-*"))  # config, body and header files removed


@needs_curl
def test_curl_unreachable_is_retried_then_hub_error(tmp_path, monkeypatch):
    for v in PROXY_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    c = HubClient(cfg(f"http://127.0.0.1:{port}", tmp_path, hub_transport="curl"))
    c.retries = 1
    with pytest.raises(HubError, match="unreachable after 2 attempts"):
        c.health()


# ---- proxy logins -------------------------------------------------------------------------------------------

USER, PASSWORD = "CORP\\ivanov", 'p@ss w"rd:1'


@pytest.mark.parametrize("transport", ["python", pytest.param("curl", marks=needs_curl)])
def test_basic_proxy_login_plain_http(proxy, stub, tmp_path, transport):
    proxy.reset(accept={basic(USER, PASSWORD)})
    url = f"http://127.0.0.1:{stub.server_address[1]}"
    c = HubClient(cfg(url, tmp_path, hub_transport=transport, proxy_user=USER, proxy_password=PASSWORD))
    assert [r["snapshot_id"] for r in c.list_snapshots(kind="market", latest=True)] == [MARKET]
    assert proxy.seen and all(m == "GET" for m, _ in proxy.seen)
    with pytest.raises(HubError) as e:  # no login: the proxy's 407, its scheme named
        HubClient(cfg(url, tmp_path, hub_transport=transport)).health()
    assert e.value.status == 407 and "прокси требует: Basic" in str(e.value)


@pytest.mark.parametrize("transport", ["python", pytest.param("curl", marks=needs_curl)])
def test_basic_proxy_login_connect_tunnel(proxy, tls_hub, tmp_path, monkeypatch, transport):
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    srv, cert = tls_hub
    srv.reset()
    proxy.reset(accept={basic(USER, PASSWORD)})
    url = f"https://127.0.0.1:{srv.server_address[1]}"
    c = HubClient(cfg(url, tmp_path, hub_transport=transport, proxy_user=USER, proxy_password=PASSWORD,
                      hub_ca_bundle=cert))
    assert c.bundle(MARKET) == (FIX / f"{MARKET}.json.gz").read_bytes()
    assert {m for m, _ in proxy.seen} == {"CONNECT"}


@needs_curl
def test_auto_switches_to_curl_on_407(proxy, tls_hub, tmp_path, monkeypatch):
    """Python gets the 407; auto takes curl, which logs in as the current user (``proxy-user ":"``: on Windows
    SSPI; this fake proxy takes the empty Basic login Linux curl sends instead) and keeps it."""
    monkeypatch.setattr(HubClient, "backoff_s", 0.0)
    monkeypatch.setattr(hub, "_auto_curl", False)
    srv, cert = tls_hub
    srv.reset()
    proxy.reset(accept={basic("", "")})  # Basic only: Linux curl would answer Negotiate with Kerberos (no ticket)
    url = f"https://127.0.0.1:{srv.server_address[1]}"
    c = HubClient(cfg(url, tmp_path, hub_ca_bundle=cert))
    assert isinstance(c.transport, PythonTransport)
    assert c.health()["status"] == "ok"
    assert isinstance(c.transport, CurlTransport) and hub._auto_curl
    assert isinstance(HubClient(cfg(url, tmp_path, hub_ca_bundle=cert)).transport, CurlTransport)  # remembered
    r = hub_check(cfg(url, tmp_path, hub_ca_bundle=cert))
    assert r["ok"] and r["transport"] == "curl"
    monkeypatch.setattr(hub, "_auto_curl", False)
    monkeypatch.setattr(hub, "find_curl", lambda: None)  # no curl: the 407 with its schemes
    with pytest.raises(HubError, match="прокси требует: Basic"):
        HubClient(cfg(url, tmp_path, hub_ca_bundle=cert)).health()
