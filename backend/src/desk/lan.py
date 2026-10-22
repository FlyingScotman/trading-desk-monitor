"""Network access self-check (``desk lan status``, the start of ``desk serve``, the settings page's «Диагностика»).

``probe`` opens a raw connection to one address of this computer (no proxy: plain sockets), does the TLS handshake
when the server speaks HTTPS, asks ``GET /health/live`` and sorts the outcome into one ``STATUSES`` kind, each with a
Russian hint. The server marks its answers with ``X-Desk-Instance`` (a random id per process, ``INSTANCE_HEADER``):
an answer from another process on that address (an old Desk Monitor, another program) is told apart from this one.
A probe from this computer to its own network address does not cross the Windows firewall (that traffic stays on the
machine), so ``ok`` here means the server answers on that address; colleagues may still need the firewall rule and
their browser's proxy to leave the address alone.

``status`` gathers what ``desk lan status`` prints: the address and port, HTTPS, the certificate's names (and this
computer's addresses it lacks), the firewall rule (``netsh ... show rule``, no administrator rights needed), the URLs
for colleagues and the probes. No cell of any report, no password; addresses and host names only.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import subprocess
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INSTANCE_HEADER = "X-Desk-Instance"
PROBE_TIMEOUT_S = 3.0
FIREWALL_RULE = "Desk Monitor (TCP {port})"
WILDCARD = ("0.0.0.0", "::")

# kind: (short Russian label, hint); {port} is filled in
STATUSES: dict[str, tuple[str, str]] = {
    "ok": ("отвечает", ("Сервер отвечает на этом адресе. Если коллеги всё равно не открывают: правило брандмауэра "
                       "(«Доступ из сети» -> «Брандмауэр Windows» -> «Скопировать для ИТ», или desk.bat firewall add от "
                       "имени администратора) и прокси в их браузере (адрес - в исключения прокси). Проверка с этого же "
                       "компьютера брандмауэр не проходит.")),
    "other_server": ("отвечает другой сервер",
                     ("На этом адресе отвечает не этот Desk Monitor, а другой процесс (старый экземпляр или другая "
                     "программа). netstat -ano | findstr :{port} - все строки LISTENING; PID из последней колонки - в "
                     "Диспетчере задач. Закройте лишний процесс или смените порт («Доступ из сети» -> «Порт», или "
                     "desk.bat lan enable --port 8443).")),
    "no_response": ("TCP есть, ответа нет",
                    ("Соединение установлено, но ответа нет: порт на этом адресе держит процесс, который не отвечает "
                    "(старый экземпляр, другая программа: netstat -ano | findstr :{port}), либо соединения перехватывает "
                    "антивирус или защита рабочей станции. Попробуйте другой порт или свой адрес вместо 0.0.0.0 («Доступ "
                    "из сети» -> «Порт», «Адрес»; или desk.bat lan enable --self-signed --port 8443 --host АДРЕС); "
                    "serve.log покажет, дошёл ли запрос.")),
    "tls_error": ("ошибка HTTPS",
                  ("TCP есть, но HTTPS не договорился: на этом адресе сервер без HTTPS или чужой сертификат "
                  "(другая программа на порту {port}, перехват HTTPS антивирусом). netstat -ano | findstr :{port}.")),
    "http_error": ("ответ с ошибкой", "Сервер ответил, но не 200 на /health/live: подробности в serve.log."),
    "refused": ("отказ в соединении",
                ("Порт {port} на этом адресе никто не слушает: Desk Monitor не запущен или слушает другой адрес "
                "(«Проверка доступа» или desk.bat lan status: строка «listens on»; новые настройки доступа действуют "
                "после перезапуска).")),
    "timeout": ("нет TCP-соединения",
                "Соединение не устанавливается за отведённое время: пакеты отбрасываются (брандмауэр, VPN, сеть)."),
    "error": ("ошибка", "Проверка не удалась: см. подробность."),
}


def label(kind: str) -> str:
    return STATUSES.get(kind, STATUSES["error"])[0]


def hint(kind: str, port: int) -> str:
    return STATUSES.get(kind, STATUSES["error"])[1].format(port=port)


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def local_addresses() -> list[str]:
    """This computer's IPv4 addresses other than 127.x (``getaddrinfo`` of its name, then the address the default
    route leaves from: a UDP ``connect`` sends nothing)."""
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.append(str(info[4][0]))
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: never reached, only routed
            out.append(s.getsockname()[0])
    except OSError:
        pass
    return [a for i, a in enumerate(out) if a not in out[:i] and not a.startswith(("127.", "0.", "169.254."))]


def local_names() -> list[str]:
    """This computer's host names (short and full) and IPv4 addresses: for the certificate and the colleagues' URLs.
    ``localhost`` is left out (a colleague's localhost is his own computer)."""
    out: list[str] = []
    for get in (socket.gethostname, socket.getfqdn):
        try:
            n = get()
        except OSError:
            continue
        if n and n not in out and not is_loopback(n) and not n.lower().startswith("localhost"):
            out.append(n)
    return out + [a for a in local_addresses() if a not in out]


def lan_addresses(bind_host: str) -> list[str]:
    """The network addresses to probe: every local address for 0.0.0.0 / ::, the address itself when specific,
    none for a loopback one."""
    if is_loopback(bind_host):
        return []
    if bind_host in WILDCARD:
        return local_addresses()
    return [bind_host.strip("[]")]


def lan_urls(bind_host: str, port: int, tls: bool) -> list[str]:
    scheme = "https" if tls else "http"
    names = local_names() if bind_host in WILDCARD else [bind_host]
    return [f"{scheme}://{f'[{n}]' if ':' in n else n}:{port}/" for n in names]


def _read_head(sock: socket.socket, deadline: float) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf and len(buf) < 16384:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        sock.settimeout(left)
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def probe(host: str, port: int, tls: bool, *, instance: str | None = None,
          timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """One address: ``{"address", "url", "status", "label", "ms", "detail", "instance", "hint"}``; ``status`` is a
    key of ``STATUSES``. ``instance``: the id this server answers with; a different one (or none) is
    ``other_server``. Never raises; never uses a proxy (raw sockets)."""
    scheme = "https" if tls else "http"
    shown = f"[{host}]" if ":" in host else host
    out: dict[str, Any] = {"address": host, "url": f"{scheme}://{shown}:{port}/", "status": "error", "ms": None,
                           "detail": None, "instance": None}
    t0 = time.monotonic()
    deadline = t0 + timeout
    stage = "connect"
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            sock: socket.socket = raw
            if tls:
                stage = "tls"
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE  # our own server, often a self-signed certificate
                raw.settimeout(max(0.05, deadline - time.monotonic()))
                sock = ctx.wrap_socket(raw, server_hostname=None if _is_ip(host) else host)
            stage = "http"
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            sock.sendall(f"GET /health/live HTTP/1.1\r\nHost: {shown}:{port}\r\nAccept: application/json\r\n"
                         f"User-Agent: desk-lan-probe\r\nConnection: close\r\n\r\n".encode("ascii"))
            head = _read_head(sock, deadline)
            if tls:
                sock.close()
        out["ms"] = round((time.monotonic() - t0) * 1000)
        if not head:
            out.update(status="no_response", detail="соединение закрыто без ответа")
            return _done(out, port)
        lines = head.split(b"\r\n")
        parts = lines[0].split(b" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        headers = {k.strip().lower(): v.strip() for k, _, v in
                   (ln.decode("latin-1").partition(":") for ln in lines[1:] if b":" in ln)}
        out["instance"] = headers.get(INSTANCE_HEADER.lower())
        if not lines[0].startswith(b"HTTP/"):
            out.update(status="tls_error" if not tls else "error", detail="ответ не HTTP")
        elif instance is not None and out["instance"] != instance:
            out.update(status="other_server", detail=f"HTTP {code}, другой экземпляр" if out["instance"]
                       else f"HTTP {code} без {INSTANCE_HEADER}")
        elif code == 200:
            out.update(status="ok", detail="HTTP 200")
        else:
            out.update(status="http_error", detail=f"HTTP {code}")
    except ConnectionRefusedError:
        out.update(status="refused", detail="ECONNREFUSED")
    except ssl.SSLError as e:
        out.update(status="tls_error", detail=f"{e.reason or type(e).__name__}"[:160])
    except TimeoutError:
        out.update(status="timeout" if stage == "connect" else "no_response",
                   detail={"connect": "TCP не установлено", "tls": "TCP есть, TLS-ответа нет",
                           "http": "TCP есть, HTTP-ответа нет"}[stage] + f" за {timeout:g} с")
    except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
        out.update(status="no_response" if stage != "connect" else "error",
                   detail=f"{'TCP есть, ' if stage != 'connect' else ''}соединение сброшено ({type(e).__name__})")
    except OSError as e:
        out.update(status="error", detail=f"{type(e).__name__}: {e.strerror or e}"[:160])
    if out["ms"] is None:
        out["ms"] = round((time.monotonic() - t0) * 1000)
    return _done(out, port)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _done(out: dict[str, Any], port: int) -> dict[str, Any]:
    out["label"] = label(out["status"])
    out["hint"] = hint(out["status"], port)
    return out


def probe_all(bind_host: str, port: int, tls: bool, *, instance: str | None = None,
              timeout: float = PROBE_TIMEOUT_S, addresses: Iterable[str] | None = None) -> dict[str, Any]:
    """127.0.0.1 and each network address (``lan_addresses``). Without ``instance`` (``desk lan status``, another
    process) the loopback answer's id stands for this server."""
    loop = probe("127.0.0.1", port, tls, instance=instance, timeout=timeout)
    expected = instance if instance is not None else loop.get("instance")
    lan = [probe(a, port, tls, instance=expected, timeout=timeout)
           for a in (lan_addresses(bind_host) if addresses is None else addresses)]
    return {"at": datetime.now(UTC).isoformat(timespec="seconds"), "loopback": loop, "lan": lan,
            "summary": summary(loop, lan)}


def summary(loop: dict[str, Any], lan: list[dict[str, Any]]) -> str:
    """One Russian line for the console, the log and the page."""
    if loop["status"] != "ok":
        return f"127.0.0.1: {loop['label']} ({loop['detail']})"
    if not lan:
        return "127.0.0.1: отвечает; сетевых адресов для проверки нет"
    bad = [r for r in lan if r["status"] != "ok"]
    if not bad:
        return "127.0.0.1 и " + ", ".join(r["address"] for r in lan) + ": отвечает"
    return "; ".join(f"{r['address']}: {r['label']} ({r['detail']})" for r in bad)


# ---- certificate and firewall ----------------------------------------------------------------------------------


def cert_info(crt: Path | None, wanted: Iterable[str] = ()) -> dict[str, Any] | None:
    """The certificate's names (SAN), validity and which of ``wanted`` (this computer's names and addresses) it
    lacks; None without a certificate."""
    if crt is None:
        return None
    info: dict[str, Any] = {"file": str(crt), "exists": Path(crt).is_file()}
    if not info["exists"]:
        return info
    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(Path(crt).read_bytes())
        try:
            san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            names = [str(n).lower() for n in san.get_values_for_type(x509.DNSName)]
            names += [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
        except x509.ExtensionNotFound:
            names = []
        info.update(sans=names, not_after=cert.not_valid_after_utc.date().isoformat(),
                    missing=[w for w in wanted if w.lower() not in names])
    except (OSError, ValueError, TypeError) as e:  # a broken or foreign file: the rest still prints
        info["error"] = f"{type(e).__name__}: {e}"[:160]
    return info


def firewall_rule_command(port: int) -> list[str]:
    return ["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE.format(port=port)}"]


def firewall_rule(port: int, runner: Any = None) -> bool | None:
    """Whether the inbound rule ``Desk Monitor (TCP <port>)`` exists (``netsh ... show rule``: exit code 0 when
    found; no administrator rights needed). None off Windows or when netsh fails to run."""
    if runner is None and os.name != "nt":
        return None
    try:
        r = (runner or subprocess.run)(firewall_rule_command(port), capture_output=True, check=False, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.returncode == 0


def status(settings: Any, *, host: str | None = None, port: int | None = None, tls: bool | None = None,
           instance: str | None = None, run_probe: bool = True,
           timeout: float = PROBE_TIMEOUT_S, firewall_runner: Any = None) -> dict[str, Any]:
    """Everything ``desk lan status`` prints, as a dict (the settings page shows the same)."""
    port = port or settings.port
    host = host or settings.bind_host
    tls = bool(settings.tls_cert and settings.tls_key) if tls is None else tls
    loop = is_loopback(host)
    doc: dict[str, Any] = {
        "bind_host": host, "port": port, "network": not loop, "auth": bool(settings.auth), "tls": tls,
        "allow_plain_http": bool(settings.allow_plain_http), "scheme": "https" if tls else "http",
        "local_url": f"{'https' if tls else 'http'}://127.0.0.1:{port}/",
        "urls": [] if loop else lan_urls(host, port, tls),
        "addresses": [] if loop else lan_addresses(host),
        "cert": cert_info(settings.tls_cert, local_names()) if tls else None,
        "firewall": {"rule": FIREWALL_RULE.format(port=port), "present": firewall_rule(port, firewall_runner),
                     "show": " ".join(f'"{a}"' if " " in a else a for a in firewall_rule_command(port)),
                     "add": firewall_add_command(port)},
    }
    doc["probe"] = probe_all(host, port, tls, instance=instance, timeout=timeout) if run_probe else None
    return doc


def status_lines(doc: dict[str, Any]) -> list[str]:
    """``status`` for the console."""
    out = [f"sign-in (auth): {'on' if doc['auth'] else 'off'}",
           f"listens on: {doc['bind_host']}:{doc['port']}" + ("" if doc["network"] else "  (this computer only)")
           + ("  (every address of this computer, 127.0.0.1 too)" if doc["bind_host"] in WILDCARD else ""),
           f"HTTPS: {'on' if doc['tls'] else 'off'}"
           + ("; plain HTTP allowed (allow_plain_http)" if doc["allow_plain_http"] and not doc["tls"] else "")]
    c = doc.get("cert")
    if c:
        if not c.get("exists"):
            out.append(f"certificate: {c['file']} NOT FOUND")
        elif c.get("error"):
            out.append(f"certificate: {c['file']}: {c['error']}")
        else:
            out.append(f"certificate: {c['file']}, valid to {c['not_after']}, names: {', '.join(c['sans'])}")
            if c["missing"]:
                out.append(f"  not in the certificate (browsers warn on these): {', '.join(c['missing'])}; "
                           f"a new one: desk.bat lan enable --self-signed")
    if doc["network"]:
        fw = doc["firewall"]
        state = {True: "present", False: "ABSENT (desk.bat firewall add, as administrator)",
                 None: "unknown (not Windows)"}[fw["present"]]
        out.append(f"firewall rule «{fw['rule']}»: {state}")
        out.append("colleagues open: " + (", ".join(doc["urls"]) or "-"))
    p = doc.get("probe")
    if p:
        out.append("self-check (from this computer, no proxy):")
        for r in [p["loopback"], *p["lan"]]:
            out.append(f"  {r['url']}  {r['status']}: {r['label']} ({r['detail']}, {r['ms']} ms)")
            if r["status"] != "ok":
                out.append(f"    {r['hint']}")
        if doc["network"] and not p["lan"]:
            out.append("  no network address found for this computer")
    return out


# ---- changing the access (desk lan enable / disable, the settings page's «Доступ из сети») ---------------------

NETWORK_KEYS = ("auth", "bind_host", "port", "tls_cert", "tls_key", "allow_plain_http", "admin_from_network")
"""The settings.json keys a change of the network access writes (all read when desk serve starts)."""
TLS_MODES = ("self_signed", "cert", "plain_http")


class LanError(ValueError):
    """A refused change of the network access: ``ru`` for the settings page, ``en`` and ``exit`` for the CLI."""

    def __init__(self, ru: str, en: str, exit: int = 2) -> None:
        super().__init__(ru)
        self.ru, self.en, self.exit = ru, en, exit


def firewall_add_command(port: int) -> str:
    """The Windows firewall rule letting colleagues reach the port, as one line for cmd (``deskctl firewall add``
    runs the same; it needs administrator rights)."""
    return (f'netsh advfirewall firewall add rule name="{FIREWALL_RULE.format(port=port)}" dir=in action=allow '
            f"protocol=TCP localport={port} profile=domain,private")


def enable_changes(settings: Any, *, has_admin: bool, host: str = "0.0.0.0", port: int | None = None,
                   tls: str | None = None, cert: str | None = None, key: str | None = None,
                   names: Iterable[str] = (), admin_from_network: bool | None = None) -> tuple[dict[str, Any], dict]:
    """The settings.json changes of ``desk lan enable``: sign-in on, listen on ``host``, HTTPS or plain HTTP.
    ``tls``: ``self_signed`` (a new certificate in ``<home>/tls`` for this computer's names and ``names``), ``cert``
    (``cert`` and ``key``: PEM files, e.g. from the corporate CA), ``plain_http``, or None: keep what the settings
    have (refused when they have neither). Refuses (``LanError``) without an active admin: after the restart nobody
    could sign in. Returns (changes, info): ``info["cert"]`` and ``info["names"]`` when a certificate was made."""
    if tls not in (None, *TLS_MODES):
        raise LanError(f"режим {tls!r}: один из {', '.join(TLS_MODES)}", f"tls mode {tls!r}")
    if tls == "cert" and not (cert and key):
        raise LanError("сертификат и ключ указываются вместе", "--cert and --key go together")
    if tls is None and not ((settings.tls_cert and settings.tls_key) or settings.allow_plain_http):
        raise LanError("выберите HTTPS или HTTP без шифрования",
                       "choose HTTPS (--self-signed, or --cert F --key F) or --plain-http")
    if port is not None and not 1 <= int(port) <= 65535:
        raise LanError("порт: число от 1 до 65535", "the port is a number from 1 to 65535")
    if not has_admin:
        raise LanError("сначала заведите администратора (раздел «Пользователи»): без него после перезапуска никто "
                       "не сможет войти", "create the first admin first: desk user add NAME --role admin", 1)
    changes: dict[str, Any] = {"auth": True, "bind_host": host}
    info: dict[str, Any] = {}
    if port:
        changes["port"] = int(port)
    if tls == "self_signed":
        from .api.auth import self_signed_cert

        wanted = list(dict.fromkeys(n for n in [*names, *local_names()] if n))
        crt, pem = self_signed_cert(settings.home / "tls", wanted)
        changes.update(tls_cert=str(crt), tls_key=str(pem), allow_plain_http=None)
        info.update(cert=crt, names=wanted)
    elif tls == "cert":
        for f in (cert, key):
            if not Path(str(f)).is_file():
                raise LanError(f"файл {f} не найден", f"{f} not found")
        changes.update(tls_cert=str(Path(str(cert)).resolve()), tls_key=str(Path(str(key)).resolve()),
                       allow_plain_http=None)
    elif tls == "plain_http":
        changes.update(allow_plain_http=True, tls_cert=None, tls_key=None)
    if admin_from_network is not None:
        changes["admin_from_network"] = bool(admin_from_network)
    return changes, info


def disable_changes(auth_off: bool = False) -> dict[str, Any]:
    """``desk lan disable``: 127.0.0.1 again (this computer only); sign-in stays on unless ``auth_off``."""
    return {"bind_host": "127.0.0.1", **({"auth": False} if auth_off else {})}


def listen_problem(host: str, port: int, *, current: dict[str, Any] | None = None) -> str | None:
    """Why ``desk serve`` could not listen on ``host:port`` (and on 127.0.0.1 beside a specific address), in
    Russian; None when it can. ``current``: what this server listens on (``app.state.listen``): on its own port
    only the address is checked (the port is this server's). A check before the settings change, not a promise:
    the launcher still rolls back a change the new server does not start with (``deskctl``)."""
    hosts = [host] + ([] if is_loopback(host) or host in WILDCARD else ["127.0.0.1"])
    same_port = bool(current) and int((current or {}).get("port") or 0) == int(port)
    for h in hosts:
        family = socket.AF_INET6 if ":" in h else socket.AF_INET
        for p in ((0,) if same_port else (0, int(port))):
            with socket.socket(family, socket.SOCK_STREAM) as s:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    s.bind((h.strip("[]"), p))
                except OSError as e:
                    if p == 0:
                        return (f"адреса {h} нет у этого компьютера ({e.strerror or e}); выберите «все адреса» "
                                "или адрес из списка")
                    return (f"порт {port} на адресе {h} занят другой программой или закрыт Windows "
                            f"({e.strerror or e}); выберите другой, например 8443")
    return None
