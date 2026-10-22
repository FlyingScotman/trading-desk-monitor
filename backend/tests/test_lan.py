"""desk.lan (network access self-check) and the serving sockets of desk.api.app.

The Windows report behind it (0.2.3): ``0.0.0.0:8100 LISTENING``, 127.0.0.1 answers, the computer's own network
address connects (TCP) but nothing answers there. ``test_probe_tells_a_dead_listener_from_this_server`` builds the
same situation here: this server on 127.0.0.1 and a socket on another address of the same port that listens and
never accepts; the self-check must call the first ``ok`` and the second ``no_response``.
"""

from __future__ import annotations

import http.server
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from desk import lan
from desk.api import app as app_module
from desk.api import auth


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def cert(tmp_path_factory) -> tuple[Path, Path]:
    return auth.self_signed_cert(tmp_path_factory.mktemp("tls"), ["desk-pc", "10.1.2.3"])


@contextmanager
def http_server(port: int, *, code: int = 200, instance: str | None = None, tls: tuple[Path, Path] | None = None,
                host: str = "127.0.0.1"):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"status":"ok"}'
            self.send_response(code)
            if instance:
                self.send_header(lan.INSTANCE_HEADER, instance)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):
            pass

    srv = http.server.ThreadingHTTPServer((host, port), Handler)
    if tls:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*tls)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@contextmanager
def dead_listener(host: str, port: int):
    """Bound and listening, never accepted: the kernel completes the TCP handshake, nobody answers."""
    s = socket.socket()
    s.bind((host, port))
    s.listen(8)
    try:
        yield s
    finally:
        s.close()


def test_probe_ok_and_other_server(cert):
    port = free_port()
    with http_server(port, instance="abc", tls=cert):
        r = lan.probe("127.0.0.1", port, True, instance="abc", timeout=2)
        assert r["status"] == "ok" and r["instance"] == "abc" and r["url"] == f"https://127.0.0.1:{port}/"
        assert r["label"] == "отвечает" and "брандмауэр" in r["hint"]
        assert lan.probe("127.0.0.1", port, True, instance="zzz", timeout=2)["status"] == "other_server"
        assert lan.probe("127.0.0.1", port, True, timeout=2)["status"] == "ok"  # no id to compare: any answer
    with http_server(port, tls=cert):
        r = lan.probe("127.0.0.1", port, True, instance="abc", timeout=2)
        assert r["status"] == "other_server" and lan.INSTANCE_HEADER in r["detail"]
        assert f"findstr :{port}" in r["hint"]
    with http_server(port, code=500, instance="abc"):
        assert lan.probe("127.0.0.1", port, False, instance="abc", timeout=2)["status"] == "http_error"


def test_probe_no_response_tls_error_refused_timeout(monkeypatch):
    port = free_port()
    with dead_listener("127.0.0.1", port):
        t0 = time.monotonic()
        r = lan.probe("127.0.0.1", port, True, timeout=0.5)
        assert r["status"] == "no_response" and "TLS" in r["detail"] and time.monotonic() - t0 < 3
        assert "антивирус" in r["hint"] and "netstat -ano | findstr" in r["hint"]
        assert lan.probe("127.0.0.1", port, False, timeout=0.5)["status"] == "no_response"
    with http_server(port):  # plain HTTP where the client speaks TLS
        r = lan.probe("127.0.0.1", port, True, timeout=2)
        assert r["status"] == "tls_error", r
    r = lan.probe("127.0.0.1", port, True, timeout=1)
    assert r["status"] == "refused" and r["label"] == "отказ в соединении"

    def slow(*_a, **_k):
        raise TimeoutError("timed out")

    monkeypatch.setattr(lan.socket, "create_connection", slow)
    r = lan.probe("10.9.9.9", port, True, timeout=1)
    assert r["status"] == "timeout" and "брандмауэр" in r["hint"]


def test_probe_tells_a_dead_listener_from_this_server(cert):
    """The report: this server answers on 127.0.0.1; on the other address of the same port a socket that is bound
    and listening but never accepted (a stale process, another program) takes the connections."""
    port = free_port()
    try:
        with dead_listener("127.0.0.2", port):
            pass
    except OSError:
        pytest.skip("no 127.0.0.2 here")
    with http_server(port, instance="me", tls=cert), dead_listener("127.0.0.2", port):
        res = lan.probe_all("0.0.0.0", port, True, timeout=0.5, addresses=["127.0.0.2"])
        assert res["loopback"]["status"] == "ok" and res["loopback"]["instance"] == "me"
        assert [r["status"] for r in res["lan"]] == ["no_response"]
        assert res["summary"].startswith("127.0.0.2: TCP есть, ответа нет")
    with http_server(port, instance="me", tls=cert), http_server(port, instance="old", tls=cert, host="127.0.0.2"):
        res = lan.probe_all("0.0.0.0", port, True, timeout=2, addresses=["127.0.0.2"])
        assert [r["status"] for r in res["lan"]] == ["other_server"]  # another Desk Monitor answers there


def test_addresses_names_and_urls(monkeypatch):
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "desk-pc")
    monkeypatch.setattr(lan.socket, "getfqdn", lambda *a: "desk-pc.corp.local")
    monkeypatch.setattr(lan, "local_addresses", lambda: ["10.1.2.3", "172.16.0.5"])
    assert lan.local_names() == ["desk-pc", "desk-pc.corp.local", "10.1.2.3", "172.16.0.5"]
    monkeypatch.setattr(lan.socket, "getfqdn", lambda *a: "localhost")  # Linux without DNS: not for colleagues
    assert "localhost" not in lan.local_names()
    assert lan.lan_addresses("0.0.0.0") == ["10.1.2.3", "172.16.0.5"] and lan.lan_addresses("127.0.0.1") == []
    assert lan.lan_addresses("10.1.2.3") == ["10.1.2.3"]
    assert lan.lan_urls("10.1.2.3", 8100, True) == ["https://10.1.2.3:8100/"]
    assert lan.lan_urls("fd00::5", 8100, False) == ["http://[fd00::5]:8100/"]
    assert lan.lan_urls("0.0.0.0", 8443, True)[0] == "https://desk-pc:8443/"
    real = lan.socket.getaddrinfo

    def addrinfo(host, *a, **k):
        if host == "desk-pc":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ("127.0.1.1", "10.1.2.3",
                                                                                     "169.254.7.7", "10.1.2.3")]
        return real(host, *a, **k)

    monkeypatch.undo()
    monkeypatch.setattr(lan.socket, "gethostname", lambda: "desk-pc")
    monkeypatch.setattr(lan.socket, "getaddrinfo", addrinfo)
    got = lan.local_addresses()
    assert got[0] == "10.1.2.3" and got.count("10.1.2.3") == 1
    assert not any(a.startswith(("127.", "169.254.")) for a in got)


def test_self_signed_certificate_names_this_computer(tmp_path, monkeypatch):
    monkeypatch.setattr(lan, "local_names", lambda: ["desk-pc", "desk-pc.corp.local", "10.1.2.3"])
    crt, _ = auth.self_signed_cert(tmp_path, auth.local_names())
    info = lan.cert_info(crt, ["desk-pc", "DESK-PC.corp.local", "10.1.2.3", "10.9.9.9"])
    assert info["sans"] == ["desk-pc", "desk-pc.corp.local", "localhost", "10.1.2.3", "127.0.0.1"]
    assert info["missing"] == ["10.9.9.9"] and info["not_after"]
    assert lan.cert_info(tmp_path / "none.crt") == {"file": str(tmp_path / "none.crt"), "exists": False}
    (tmp_path / "bad.crt").write_text("not a certificate")
    assert "error" in lan.cert_info(tmp_path / "bad.crt")
    assert lan.cert_info(None) is None


def test_firewall_rule_presence():
    class R:
        def __init__(self, rc):
            self.returncode = rc

    seen = []

    def runner(rc):
        def run(cmd, **_k):
            seen.append(cmd)
            return R(rc)
        return run

    assert lan.firewall_rule(8100, runner(0)) is True
    assert seen[0] == ["netsh", "advfirewall", "firewall", "show", "rule", "name=Desk Monitor (TCP 8100)"]
    assert lan.firewall_rule(8100, runner(1)) is False  # «No rules match the specified criteria»

    def broken(*_a, **_k):
        raise OSError("no netsh")

    assert lan.firewall_rule(8100, broken) is None
    if lan.os.name != "nt":
        assert lan.firewall_rule(8100) is None


def test_status_lines(tmp_path, monkeypatch, cert):
    from desk.settings import Settings

    monkeypatch.setattr(lan, "local_addresses", lambda: ["10.1.2.3"])
    st = Settings(home=tmp_path, bind_host="0.0.0.0", port=free_port(), auth=True, tls_cert=cert[0], tls_key=cert[1])
    doc = lan.status(st, timeout=0.3, firewall_runner=lambda *a, **k: type("R", (), {"returncode": 1})())
    text = "\n".join(lan.status_lines(doc))
    assert doc["network"] and doc["tls"] and doc["firewall"]["present"] is False
    assert "listens on: 0.0.0.0" in text and "ABSENT" in text and "names: desk-pc, localhost, 10.1.2.3, 127.0.0.1" in text
    assert "refused" in text and f"https://10.1.2.3:{st.port}/" in text  # nothing runs there


# ---- the serving sockets ---------------------------------------------------------------------------------------


def test_listen_sockets_one_for_every_address_none_inheritable():
    port = free_port()
    socks = app_module.listen_sockets("0.0.0.0", port)
    try:
        assert [s.getsockname() for s in socks] == [("0.0.0.0", port)]  # 0.0.0.0 covers 127.0.0.1: no second one
        assert not any(s.get_inheritable() for s in socks)
    finally:
        for s in socks:
            s.close()
    port = free_port()
    socks = app_module.listen_sockets("127.0.0.1", port)
    try:
        assert [s.getsockname() for s in socks] == [("127.0.0.1", port)]
    finally:
        for s in socks:
            s.close()


def test_listen_sockets_a_specific_address_gets_127_beside_it():
    addrs = lan.local_addresses()
    if not addrs:
        pytest.skip("no network address here")
    port = free_port()
    socks = app_module.listen_sockets(addrs[0], port)
    try:
        assert [s.getsockname() for s in socks] == [(addrs[0], port), ("127.0.0.1", port)]
        assert not any(s.get_inheritable() for s in socks)
    finally:
        for s in socks:
            s.close()


def test_server_loop_is_the_selector_on_windows(monkeypatch):
    import asyncio

    from uvicorn.config import Config

    assert app_module.server_loop() == ("asyncio:SelectorEventLoop" if app_module.os.name == "nt" else "auto")
    monkeypatch.setattr(app_module.os, "name", "nt")
    loop = app_module.server_loop()
    monkeypatch.undo()
    assert loop == "asyncio:SelectorEventLoop"
    assert Config(app=None, loop=loop).get_loop_factory() is asyncio.SelectorEventLoop  # uvicorn imports it


def test_one_server_answers_loopback_and_the_network_address_with_tls(cert):
    """serve's setup: listen_sockets on 0.0.0.0, one uvicorn server with TLS on that socket; the self-check (as
    serve runs it) finds this server on 127.0.0.1 and on each network address."""
    port = free_port()
    app = Starlette(routes=[Route("/health/live", lambda r: JSONResponse({"status": "ok"},
                                                                         headers={lan.INSTANCE_HEADER: "srv1"}))])
    app.state.instance = "srv1"
    app.state.lan_probe = None
    socks = app_module.listen_sockets("0.0.0.0", port)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning",
                                           loop=app_module.server_loop(), ssl_certfile=str(cert[0]),
                                           ssl_keyfile=str(cert[1])))
    t = threading.Thread(target=server.run, kwargs={"sockets": socks}, daemon=True)
    t.start()
    try:
        app_module.self_check(app, server, "0.0.0.0", port, True, wait_s=20)
        res = app.state.lan_probe
        assert res is not None and res["loopback"]["status"] == "ok"
        assert all(r["status"] == "ok" for r in res["lan"]), res
        assert [r["address"] for r in res["lan"]] == lan.local_addresses()
    finally:
        server.should_exit = True
        t.join(10)
        for s in socks:
            s.close()
