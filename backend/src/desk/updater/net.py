"""How the server reaches GitHub: three layers that each behave a little more like the browser, and what failed.

* ``python``     the standard library (``urllib``) with the operating system's certificate verifier
                 (``truststore``: on Windows the certificate store, where a TLS-inspecting proxy's root CA is pushed
                 by policy) and the system proxy (``HTTPS_PROXY``, the Internet Options proxy, else the PAC / WPAD
                 answer of Windows, ``system_proxy``); a proxy login only as Basic (the settings' proxy login)
* ``curl``       Windows' own ``System32\\curl.exe``: Schannel (the Windows certificate store) and SSPI, so it logs
                 in to an NTLM / Kerberos / Negotiate proxy as the current Windows user (``--proxy-anyauth`` with
                 ``--proxy-user :``); the proxy as for ``python``
* ``powershell`` Windows PowerShell's ``Invoke-WebRequest`` (.NET of Windows): the proxy and PAC of the system,
                 the current user's login to it (``DefaultNetworkCredentials``), the Windows certificate store, as
                 the browser (Edge, Chrome) does

The monitor's own releases are public and fetched without a token. The private adapter's releases
(``desk.updater.adapter``) need one: ``fetch(..., auth=True)`` sends ``Authorization: Bearer <Context.token>`` and
only to ``https://api.github.com/`` (anything else is refused before a request is made), and never on to the host a
redirect leads to (an asset is a 302 from the API to GitHub's file storage):

* ``python``     the header is an unredirected one (``Request.add_unredirected_header``): urllib copies only the
                 ordinary headers to a redirected request; ``_ApiOnlyRedirect`` puts it back only when the redirect
                 stays on ``https://api.github.com/`` (a renamed repository)
* ``curl``       the header is a line of the ``-K`` config file (never on the command line, where other users of the
                 computer could read it); curl from 7.58 (Windows 10 1803 ships 7.55, later ones 7.79+) does not send
                 a custom ``Authorization`` to another host after ``--location`` (CVE-2018-1000007); an older curl
                 is ``unavailable`` for an authenticated request
* ``powershell`` the token comes in an environment variable of the child (``DESK_UPD_TOKEN``), never in the script
                 or on its command line; .NET Framework's ``HttpWebRequest`` (Windows PowerShell 5.1) clears the
                 ``Authorization`` header on every automatic redirect, so the file storage never sees it

A 401 is ``token_rejected`` (expired, revoked or mistyped); a 404 of an authenticated request is ``no_access``
(no such repository for this token, or no release yet). The token never appears in a ``NetError``, a ``Result`` or
an attempt record (``fetch`` scrubs it from messages, too). Each failure becomes a ``NetError`` with a ``code``
(``CODES``) that tells a TLS-inspecting proxy (``tls_untrusted``) from a proxy that wants a login (``proxy_auth``),
one that blocks the site (``proxy_denied``), a name that does not resolve (``dns``), a closed connection
(``connect``) or a timeout, and an ``IT_HINTS`` sentence for the owner's IT department.

``fetch`` writes the body to a file (a release zip is ~100 MB) and checks a size limit; the caller hashes it.
"""

from __future__ import annotations

import http.client
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.message import Message
from pathlib import Path
from typing import Any

from ..marketdata.hub import (
    auth_schemes,
    curl_version,
    find_curl,
    hub_proxy,
    probe_proxy_schemes,
    split_proxy,
    tls_context,
    with_credentials,
)

USER_AGENT = "desk-updater"
LAYERS = ("python", "curl", "powershell")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CODES = {
    "dns": "имя сервера не находится (DNS)",
    "connect": "соединение не устанавливается (адрес закрыт или недоступен)",
    "timeout": "нет ответа (таймаут)",
    "tls_untrusted": "сертификат сервера не доверен: похоже на прокси с проверкой TLS (подмена сертификата)",
    "tls": "ошибка TLS-соединения",
    "proxy_auth": "прокси требует вход (407)",
    "proxy_denied": "прокси не пускает на этот адрес",
    "not_found": "нет такого файла или релиза (404)",
    "rate_limited": "лимит запросов к GitHub API исчерпан (60 в час на внешний адрес сети)",
    "token_rejected": "GitHub не принял токен (401): срок истёк, токен отозван или вставлен с ошибкой",
    "no_access": "нет доступа или релиза (404): у токена нет доступа к этому репозиторию, или в нём ещё нет релиза",
    "no_token": "токен GitHub не задан",
    "http": "сервер ответил ошибкой",
    "too_big": "ответ больше допустимого",
    "unavailable": "способ недоступен на этом компьютере",
    "failed": "не удалось",
}
IT_HINTS = {
    "dns": "Компьютер не разрешает внешние имена сам: в интернет выходят только через прокси. Нужен адрес "
           "прокси или PAC-файла (как в браузере) — укажите его в поле «Прокси» или узнайте у ИТ.",
    "connect": "Прямое соединение закрыто. Нужен прокси (как у браузера) или разрешение на выход к github.com, "
               "api.github.com, release-assets.githubusercontent.com, objects.githubusercontent.com (порт 443).",
    "timeout": "Соединение молча обрывается: чаще всего файрвол без прокси. То же, что для «соединение не "
               "устанавливается».",
    "tls_untrusted": "Прокси проверяет TLS и подставляет свой сертификат, а его корневой сертификат не в хранилище "
                     "Windows «Доверенные корневые центры сертификации» этого пользователя/компьютера. Попросите ИТ "
                     "добавить его туда (обычно делается групповой политикой) или исключить github.com, "
                     "*.githubusercontent.com из проверки TLS.",
    "tls": "TLS-соединение рвётся: прокси или антивирус вмешивается в HTTPS. Попросите ИТ исключить github.com и "
           "*.githubusercontent.com из проверки TLS.",
    "proxy_auth": "Прокси требует вход. Способ curl входит под текущим пользователем Windows (NTLM/Kerberos); если и "
                  "он получает 407 — прокси не пускает программы, кроме браузера: попросите ИТ разрешить "
                  "curl.exe/python.exe или адреса github.com, *.githubusercontent.com без входа.",
    "proxy_denied": "Прокси блокирует этот адрес для программ (категория сайта или «только браузер»). Попросите ИТ "
                    "разрешить github.com, api.github.com, release-assets.githubusercontent.com, "
                    "objects.githubusercontent.com.",
    "rate_limited": "GitHub API ограничивает 60 запросов в час на внешний адрес всей сети. Проверка повторится "
                    "позже; скачивание релиза этим лимитом не ограничено.",
    "token_rejected": "Создайте новый токен (GitHub → Settings → Developer settings → Fine-grained tokens, только "
                      "этот репозиторий, Contents: Read-only) и вставьте его в «Обновление адаптера».",
    "no_access": "Проверьте, что токен создан для этого репозитория (Repository access → Only select repositories) "
                 "с правом Contents: Read-only, и что в репозитории опубликован релиз.",
    "no_token": "Вставьте токен GitHub в «Обновление адаптера» (или скачайте архив браузером, где вы вошли в GitHub).",
}

# what diagnose() asks: the API, the release pages (and their redirect), the file storage behind the downloads
TARGETS = (
    ("api", "https://api.github.com/", "GitHub API (проверка версии)"),
    ("github", "https://github.com/", "github.com (страница и ссылки релиза)"),
    ("assets", "https://release-assets.githubusercontent.com/", "хранилище файлов релизов"),
    ("objects", "https://objects.githubusercontent.com/", "хранилище файлов (старый адрес)"),
)
GITHUB_HOSTS = ("github.com", "api.github.com")
API_ORIGIN = "https://api.github.com/"
CURL_AUTH_MIN = (7, 58)  # drops a custom Authorization on a redirect to another host (CVE-2018-1000007)


class NetError(Exception):
    def __init__(self, code: str, message: str, layer: str, status: int | None = None,
                 schemes: list[str] | None = None) -> None:
        super().__init__(message)
        self.code, self.layer, self.status, self.schemes = code, layer, status, schemes or []

    def public(self) -> dict[str, Any]:
        return {"code": self.code, "layer": self.layer, "status": self.status, "message": str(self),
                "what": CODES.get(self.code, self.code), "it_hint": IT_HINTS.get(self.code),
                "schemes": self.schemes}


@dataclass
class Result:
    status: int
    headers: dict[str, str]  # lower-case names, the final response's
    path: Path
    size: int
    proxy: str | None = None  # the proxy used (no login), None: direct
    extra: dict[str, Any] = field(default_factory=dict)

    def read(self) -> bytes:
        return self.path.read_bytes()


def http_error(layer: str, url: str, status: int, headers: dict[str, str]) -> NetError:
    """A non-2xx answer as a NetError (the proxy's own block page through TLS inspection included)."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if status == 407:
        return NetError("proxy_auth", "407 Proxy Authentication Required", layer, 407)
    if status == 401 and host == "api.github.com":
        return NetError("token_rejected", "401 Unauthorized от api.github.com", layer, 401)
    if status == 404:
        return NetError("not_found", f"404 {host}{urllib.parse.urlsplit(url).path}", layer, 404)
    if status in (403, 429) and headers.get("x-ratelimit-remaining") == "0":
        return NetError("rate_limited", f"{status}: лимит GitHub API, сброс в "
                        f"{time.strftime('%H:%M', time.localtime(int(headers.get('x-ratelimit-reset') or 0)))}",
                        layer, status)
    if status in (403, 451) and host in GITHUB_HOSTS and "x-github-request-id" not in headers:
        return NetError("proxy_denied", f"{status} от прокси (ответ не от GitHub)", layer, status)
    return NetError("http", f"HTTP {status} от {host}", layer, status)


# ---- the system proxy, PAC included ----------------------------------------------------------------------------

_pac: dict[str, tuple[float, str | None]] = {}
PAC_TTL_S = 3600.0


def windows() -> bool:
    return os.name == "nt"


def find_powershell() -> str | None:
    """Windows PowerShell 5.1 (``System32\\WindowsPowerShell\\v1.0\\powershell.exe``), never on other systems."""
    if not windows():
        return None
    exe = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32/WindowsPowerShell/v1.0/powershell.exe"
    return str(exe) if exe.is_file() else shutil.which("powershell.exe")


def _ps(script: str, env: dict[str, str], timeout: float, runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    """Run a one-line PowerShell ``script`` (values passed in ``env``, never on the command line); its last stdout
    line is JSON."""
    exe = find_powershell()
    if exe is None:
        raise NetError("unavailable", "нет Windows PowerShell", "powershell")
    run = runner or subprocess.run
    try:
        r = run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, check=False,
                timeout=timeout, env={**os.environ, **env}, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise NetError("timeout", f"PowerShell: нет ответа за {timeout:g} с", "powershell") from None
    except OSError as e:
        raise NetError("unavailable", f"PowerShell не запускается: {e}", "powershell") from None
    lines = [ln for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    try:
        doc = json.loads(lines[-1]) if lines else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        hint = " (режим ограниченного языка PowerShell?)" if any("ConstrainedLanguage" in ln or "language mode" in ln
                                                                for ln in err) else ""
        raise NetError("unavailable", f"PowerShell: {err[-1] if err else 'нет ответа'}{hint}"[:300], "powershell")
    return doc


PS_UTF8 = "try { [Console]::OutputEncoding=[Text.Encoding]::UTF8 } catch {} ; "  # Russian messages, not the OEM page
PS_PROXY = (PS_UTF8 + "$u=[Uri]$env:DESK_UPD_URL; $p=''; try { $x=[Net.WebRequest]::GetSystemWebProxy().GetProxy($u); "
            "if ($x -and $x.AbsoluteUri -ne $u.AbsoluteUri) { $p=$x.AbsoluteUri } } catch {} ; "
            "@{ proxy = $p } | ConvertTo-Json -Compress")


def system_proxy(url: str, runner: Callable[..., Any] | None = None) -> str | None:
    """The proxy for ``url``: ``HTTPS_PROXY`` / the Internet Options proxy (``hub_proxy``), else on Windows what the
    system's own resolver answers (the PAC script or WPAD the browser uses); None: direct. Cached an hour."""
    proxy = hub_proxy(url)
    if proxy or not windows():
        return proxy
    host = urllib.parse.urlsplit(url).hostname or ""
    hit = _pac.get(host)
    if hit and time.monotonic() - hit[0] < PAC_TTL_S:
        return hit[1]
    try:
        p = str(_ps(PS_PROXY, {"DESK_UPD_URL": url}, 30, runner).get("proxy") or "").rstrip("/") or None
    except NetError:
        p = None
    _pac[host] = (time.monotonic(), p)
    return p


# ---- the layers ---------------------------------------------------------------------------------------------------


@dataclass
class Context:
    """What every layer needs: where temporary files go, the timeout, the settings' proxy login."""

    home: Path
    timeout_s: float = 60.0
    proxy_user: str | None = None
    proxy_password: str | None = field(default=None, repr=False)
    ca_bundle: Path | None = None
    runner: Callable[..., Any] | None = None  # subprocess.run, replaced in tests
    proxy_for: Callable[[str], str | None] | None = None  # system_proxy, replaced in tests
    token: str | None = field(default=None, repr=False)  # GitHub token, sent only with fetch(auth=True)

    def proxy(self, url: str) -> str | None:
        return (self.proxy_for or (lambda u: system_proxy(u, self.runner)))(url)

    def tmp(self) -> Path:
        d = self.home / "updates" / "downloads"
        d.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".dl-", suffix=".part", dir=d)
        os.close(fd)
        return Path(name)


def _classify_os(layer: str, e: BaseException) -> NetError:
    """An exception of urllib / ssl / socket as a NetError."""
    reason: Any = e
    while isinstance(reason, urllib.error.URLError) and not isinstance(reason, urllib.error.HTTPError):
        reason = reason.reason
    text = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return NetError("tls_untrusted", f"TLS: {reason.verify_message or text}", layer)
    if isinstance(reason, ssl.SSLError):
        return NetError("tls", f"TLS: {text}", layer)
    if isinstance(reason, socket.gaierror):
        return NetError("dns", f"DNS: {text}", layer)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return NetError("timeout", f"таймаут: {text}", layer)
    m = re.search(r"Tunnel connection failed: (\d{3})", text)
    if m:
        code = int(m.group(1))
        return NetError("proxy_auth" if code == 407 else "proxy_denied", f"прокси: {text}", layer, code)
    if isinstance(reason, OSError):
        return NetError("connect", f"соединение: {text}", layer)
    return NetError("failed", text[:300], layer)


def _headers(msg: Message | None) -> dict[str, str]:
    """Lower-case names; a repeated header (``Proxy-Authenticate``) joined with ``, ``."""
    out: dict[str, str] = {}
    for k, v in msg.items() if msg is not None else []:
        out[k.lower()] = f"{out[k.lower()]}, {v}" if k.lower() in out else v
    return out


def api_url(url: str) -> bool:
    """``url`` is on ``https://api.github.com/`` (the only place a token goes)."""
    u = urllib.parse.urlsplit(url)
    return u.scheme == "https" and u.hostname == "api.github.com" and u.port in (None, 443) and not u.username \
        and url.startswith(API_ORIGIN)


class _ApiOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """urllib's redirect, the token kept only while the redirect stays on the API (it is an unredirected header:
    urllib never copies it itself)."""

    def __init__(self, token: str | None) -> None:
        super().__init__()
        self._token = token

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.headers.pop("Authorization", None)
            new.unredirected_hdrs.pop("Authorization", None)
            if self._token and api_url(new.full_url):
                new.add_unredirected_header("Authorization", f"Bearer {self._token}")
        return new


def fetch_python(ctx: Context, url: str, dest: Path, max_bytes: int, accept: str | None = None,
                 progress: Callable[[int], None] | None = None, token: str | None = None) -> Result:
    layer = "python"
    proxy = ctx.proxy(url)
    handlers: list[Any] = [urllib.request.HTTPSHandler(context=tls_context(ctx.ca_bundle)), _ApiOnlyRedirect(token)]
    scheme = urllib.parse.urlsplit(url).scheme
    if proxy:
        p = with_credentials(proxy, ctx.proxy_user, ctx.proxy_password) if ctx.proxy_user else proxy
        handlers.append(urllib.request.ProxyHandler({scheme: p, "https": p}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))  # direct: what system_proxy found, not urllib again
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                               **({"Accept": accept} if accept else {})})
    if token:
        req.add_unredirected_header("Authorization", f"Bearer {token}")
        req.add_unredirected_header("X-GitHub-Api-Version", "2022-11-28")
    bare = split_proxy(proxy)[0] if proxy else None
    try:
        with opener.open(req, timeout=ctx.timeout_s) as r, open(dest, "wb") as out:
            size = 0
            for chunk in iter(lambda: r.read(1 << 20), b""):
                size += len(chunk)
                if size > max_bytes:
                    raise NetError("too_big", f"ответ больше {max_bytes >> 20} МБ", layer)
                out.write(chunk)
                if progress:
                    progress(size)
            return Result(r.status, _headers(r.headers), dest, size, bare)
    except urllib.error.HTTPError as e:
        raise http_error(layer, url, e.code, _headers(e.headers)) from None
    except NetError:
        raise
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        err = _classify_os(layer, e)
        if err.status == 407 and proxy:
            err.schemes = probe_proxy_schemes(proxy, url, ctx.timeout_s)
            if err.schemes:
                err.args = (f"{err} (прокси просит: {', '.join(err.schemes)}; этот способ умеет только Basic)",)
        raise err from None


def _q(value: str) -> str:
    """A curl config value: one line, double quotes, ``\\`` and ``"`` escaped."""
    if "\n" in value or "\r" in value:
        raise NetError("failed", "значение для curl с переводом строки", "curl")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _blocks(raw: bytes) -> list[tuple[int, dict[str, str]]]:
    """The header blocks of curl's ``-D`` (the proxy's CONNECT answer, redirects, the final one)."""
    out = []
    for block in re.split(rb"\r?\n\r?\n", raw):
        lines = block.lstrip(b"\r\n").split(b"\n", 1)
        m = re.match(rb"HTTP/[\d.]+ (\d{3})", lines[0])
        if m:
            msg = http.client.parse_headers(io.BytesIO((lines[1] if len(lines) > 1 else b"") + b"\r\n\r\n"))
            out.append((int(m.group(1)), _headers(msg)))
    return out


# curl's exit codes (https://curl.se/libcurl/c/libcurl-errors.html)
_CURL = {5: "dns", 6: "dns", 7: "connect", 28: "timeout", 35: "tls", 60: "tls_untrusted", 77: "tls_untrusted",
         83: "tls_untrusted", 90: "tls_untrusted", 91: "tls"}


def fetch_curl(ctx: Context, url: str, dest: Path, max_bytes: int, accept: str | None = None,
               progress: Callable[[int], None] | None = None, curl: str | None = None,
               token: str | None = None) -> Result:
    layer = "curl"
    exe = curl or find_curl()
    if not exe:
        raise NetError("unavailable", "нет curl (в Windows 10/11 это C:\\Windows\\System32\\curl.exe)", layer)
    if token and curl_version(exe) < CURL_AUTH_MIN:
        raise NetError("unavailable", "curl старше 7.58 может переслать токен при перенаправлении: с токеном не "
                       "используется", layer)
    proxy = ctx.proxy(url)
    lines = [f"header = {_q('User-Agent: ' + USER_AGENT)}", f"header = {_q('Accept-Encoding: identity')}"]
    if accept:
        lines.append(f"header = {_q('Accept: ' + accept)}")
    if token:  # in the config file only: never on the command line
        lines += [f"header = {_q('Authorization: Bearer ' + token)}",
                  f"header = {_q('X-GitHub-Api-Version: 2022-11-28')}"]
    if proxy:
        bare, user, password = split_proxy(proxy)
        if ctx.proxy_user:
            user, password = ctx.proxy_user, ctx.proxy_password
        login = f"{user}:{password or ''}" if user else ":"  # ':' = the current Windows logon (SSPI)
        lines += [f"proxy = {_q(bare)}", "proxy-anyauth", f"proxy-user = {_q(login)}", 'noproxy = ""']
    else:
        lines.append(f"noproxy = {_q('*')}")
    if windows() and curl_version(exe) >= (7, 70):
        lines.append("ssl-revoke-best-effort")  # Schannel's revocation check fails behind many proxies
    if ctx.ca_bundle:
        lines.append(f"cacert = {_q(str(ctx.ca_bundle))}")
    lines.append(f"max-filesize = {max_bytes}")
    cfg, heads = ctx.tmp(), ctx.tmp()
    try:
        cfg.write_text("\n".join(lines) + "\n", encoding="utf-8")
        t = f"{ctx.timeout_s:g}"
        cmd = [exe, "-q", "-sS", "--location", "--max-redirs", "5", "--connect-timeout", t, "--speed-limit", "1",
               "--speed-time", t, "-o", str(dest), "-D", str(heads), "-w", "%{http_code} %{http_connect}",
               "-K", str(cfg), url]
        try:
            r = (ctx.runner or subprocess.run)(cmd, capture_output=True, check=False, timeout=3600,
                                               creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise NetError("timeout", "curl: нет ответа за час", layer) from None
        except OSError as e:
            raise NetError("unavailable", f"curl не запускается: {e}", layer) from None
        codes = r.stdout.decode("ascii", "replace").split()
        status = int(codes[0]) if codes and codes[0].isdigit() else 0
        connect = int(codes[1]) if len(codes) > 1 and codes[1].isdigit() else 0
        blocks = _blocks(heads.read_bytes()) if heads.is_file() else []
        bare = split_proxy(proxy)[0] if proxy else None
        if r.returncode:
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            text = f"curl ({r.returncode}): {err[-1] if err else 'ошибка'}"[:400]
            if connect and connect != 200:
                refused = [h for c, h in blocks if c == connect]
                schemes = auth_schemes([refused[-1]["proxy-authenticate"]]) if refused and \
                    "proxy-authenticate" in refused[-1] else []
                raise NetError("proxy_auth" if connect == 407 else "proxy_denied", text, layer, connect, schemes)
            if r.returncode == 63:
                raise NetError("too_big", f"ответ больше {max_bytes >> 20} МБ", layer)
            code = _CURL.get(r.returncode, "failed")
            if code == "tls" and re.search(r"SEC_E_UNTRUSTED_ROOT|CERT_E_UNTRUSTEDROOT|certificate chain|untrusted",
                                           text, re.IGNORECASE):
                code = "tls_untrusted"
            raise NetError(code, text, layer)
        final = blocks[-1][1] if blocks else {}
        if not 200 <= status < 300:
            raise http_error(layer, url, status, final)
        size = dest.stat().st_size if dest.is_file() else 0
        if progress:
            progress(size)
        return Result(status, final, dest, size, bare)
    finally:
        cfg.unlink(missing_ok=True)
        heads.unlink(missing_ok=True)


# one line (no double quotes: it passes the Windows command line unchanged); values come in DESK_UPD_* variables
PS_FETCH = (
    PS_UTF8 + "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
    "try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072 } "
    "catch {} ; "
    "try { [Net.WebRequest]::DefaultWebProxy.Credentials = [Net.CredentialCache]::DefaultNetworkCredentials } "
    "catch {} ; "
    "$u=[Uri]$env:DESK_UPD_URL; $p=''; "
    "try { $x=[Net.WebRequest]::GetSystemWebProxy().GetProxy($u); "
    "if ($x -and $x.AbsoluteUri -ne $u.AbsoluteUri) { $p=$x.AbsoluteUri } } catch {} ; "
    "$a=@{ Uri=$u; OutFile=$env:DESK_UPD_OUT; UseBasicParsing=$true; PassThru=$true; UserAgent='desk-updater'; "
    "TimeoutSec=[int]$env:DESK_UPD_TIMEOUT }; $h=@{}; "
    "if ($env:DESK_UPD_ACCEPT) { $h['Accept']=$env:DESK_UPD_ACCEPT }; "
    "if ($env:DESK_UPD_TOKEN) { $h['Authorization']='Bearer ' + $env:DESK_UPD_TOKEN; "
    "$h['X-GitHub-Api-Version']='2022-11-28' }; if ($h.Count) { $a['Headers']=$h }; "
    "if ($p) { $a['Proxy']=$p; $a['ProxyUseDefaultCredentials']=$true }; "
    "try { $r=Invoke-WebRequest @a; $g=[string]$r.Headers['X-GitHub-Request-Id']; "
    "@{ ok=$true; status=[int]$r.StatusCode; proxy=$p; github=$g; "
    "remaining=[string]$r.Headers['X-RateLimit-Remaining'] } | ConvertTo-Json -Compress } "
    "catch { $e=$_.Exception; $s=0; $w=''; $g=''; $rem=''; "
    "if ($e.Response) { try { $s=[int]$e.Response.StatusCode } catch {} ; "
    "try { $g=[string]$e.Response.Headers['X-GitHub-Request-Id']; "
    "$rem=[string]$e.Response.Headers['X-RateLimit-Remaining'] } catch {} } ; "
    "if ($e.Status) { $w=[string]$e.Status } ; $i=$e; while ($i.InnerException) { $i=$i.InnerException } ; "
    "@{ ok=$false; status=$s; web=$w; message=$e.Message; inner=$i.Message; proxy=$p; github=$g; remaining=$rem } "
    "| ConvertTo-Json -Compress }"
)

# System.Net.WebExceptionStatus names
_WEB = {"NameResolutionFailure": "dns", "ProxyNameResolutionFailure": "dns", "ConnectFailure": "connect",
        "ConnectionClosed": "connect", "Timeout": "timeout", "TrustFailure": "tls_untrusted",
        "SecureChannelFailure": "tls", "ReceiveFailure": "connect", "SendFailure": "connect"}


def fetch_powershell(ctx: Context, url: str, dest: Path, max_bytes: int, accept: str | None = None,
                     progress: Callable[[int], None] | None = None, token: str | None = None) -> Result:
    layer = "powershell"
    env = {"DESK_UPD_URL": url, "DESK_UPD_OUT": str(dest), "DESK_UPD_TIMEOUT": f"{int(ctx.timeout_s)}",
           "DESK_UPD_ACCEPT": accept or "", "DESK_UPD_TOKEN": token or ""}  # values in the child's environment only
    doc = _ps(PS_FETCH, env, 3600, ctx.runner)
    proxy = str(doc.get("proxy") or "").rstrip("/") or None
    if not doc.get("ok"):
        status = int(doc.get("status") or 0)
        msg = " / ".join(str(doc.get(k)) for k in ("message", "inner") if doc.get(k))[:400] or "ошибка"
        if status:
            headers = {"x-github-request-id": str(doc["github"])} if doc.get("github") else {}
            if doc.get("remaining") not in (None, ""):
                headers["x-ratelimit-remaining"] = str(doc["remaining"])
            err = http_error(layer, url, status, headers)
            err.args = (f"{err}: {msg}",)
            raise err
        code = _WEB.get(str(doc.get("web") or ""), "failed")
        if code == "failed" and re.search(r"\(407\)", msg):
            code = "proxy_auth"
        if code in ("tls", "failed") and re.search(r"trust relationship|доверительных отношений|untrusted|"
                                                   r"не является доверенным", msg, re.IGNORECASE):
            code = "tls_untrusted"
        raise NetError(code, msg, layer)
    size = dest.stat().st_size if dest.is_file() else 0
    if size > max_bytes:
        raise NetError("too_big", f"ответ больше {max_bytes >> 20} МБ", layer)
    if progress:
        progress(size)
    headers = {"x-github-request-id": str(doc["github"])} if doc.get("github") else {}
    return Result(int(doc.get("status") or 200), headers, dest, size, proxy)


FETCHERS: dict[str, Callable[..., Result]] = {"python": fetch_python, "curl": fetch_curl,
                                              "powershell": fetch_powershell}


def available(layer: str) -> str | None:
    """Why ``layer`` cannot run here; None when it can."""
    if layer == "curl" and not find_curl():
        return "нет curl.exe"
    if layer == "powershell" and not find_powershell():
        return "только в Windows (Windows PowerShell)"
    return None


def scrub(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token and len(token) >= 4 else text


def fetch(layer: str, ctx: Context, url: str, max_bytes: int, *, dest: Path | None = None,
          accept: str | None = None, progress: Callable[[int], None] | None = None, auth: bool = False) -> Result:
    """GET ``url`` through ``layer`` into ``dest`` (a temporary file in ``<home>/updates/downloads`` when None, the
    caller removes it); NetError on anything but a 2xx answer. ``auth``: with ``ctx.token``, only to the API."""
    token = None
    if auth:
        if not ctx.token:
            raise NetError("no_token", "токен GitHub не задан", layer)
        if not api_url(url):
            raise NetError("failed", "токен передаётся только на https://api.github.com/", layer)
        token = ctx.token
    why = available(layer)
    if why:
        raise NetError("unavailable", why, layer)
    out = dest or ctx.tmp()
    try:
        if token:
            return FETCHERS[layer](ctx, url, out, max_bytes, accept=accept, progress=progress, token=token)
        return FETCHERS[layer](ctx, url, out, max_bytes, accept=accept, progress=progress)
    except NetError as e:
        out.unlink(missing_ok=True)
        if token:
            if e.code == "not_found":
                e.code = "no_access"
            e.args = (scrub(str(e), token),)
        raise
    except BaseException:
        out.unlink(missing_ok=True)
        raise


def get_json(layer: str, ctx: Context, url: str, max_bytes: int = 4 << 20, accept: str | None = None,
             auth: bool = False) -> Any:
    r = fetch(layer, ctx, url, max_bytes, accept=accept, auth=auth)
    try:
        return json.loads(r.read().decode("utf-8-sig"))
    except ValueError:
        raise NetError("http", f"{urllib.parse.urlsplit(url).hostname}: ответ не JSON (страница прокси?)",
                       layer, r.status) from None
    finally:
        r.path.unlink(missing_ok=True)


def get_text(layer: str, ctx: Context, url: str, max_bytes: int = 1 << 20) -> str:
    r = fetch(layer, ctx, url, max_bytes)
    try:
        return r.read().decode("utf-8-sig", "replace")
    finally:
        r.path.unlink(missing_ok=True)


def probe(layer: str, ctx: Context, url: str) -> dict[str, Any]:
    """Does ``url`` answer through ``layer`` at all: any HTTP answer from the site counts (a 404 of a bare host
    too), a proxy's refusal or a network error does not. ``ok``, ``ms``, ``status``, ``proxy`` or the error."""
    t0 = time.monotonic()
    try:
        r = fetch(layer, ctx, url, 1 << 20)
        r.path.unlink(missing_ok=True)
        return {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "status": r.status, "proxy": r.proxy}
    except NetError as e:
        ms = int((time.monotonic() - t0) * 1000)
        try:
            proxy = split_proxy(p)[0] if (p := ctx.proxy(url)) else None
        except Exception:  # noqa: BLE001 - only for display
            proxy = None
        if e.code in ("not_found", "http") and e.status and e.status != 407:
            return {"ok": True, "ms": ms, "status": e.status, "proxy": proxy}
        if e.code == "rate_limited":
            return {"ok": True, "ms": ms, "status": e.status, "proxy": proxy, "note": str(e)}
        return {"ok": False, "ms": ms, "proxy": proxy, **e.public()}
