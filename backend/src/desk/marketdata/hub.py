"""HTTP client of the hub's snapshot API (slice S3); the only module that knows the hub's paths.

* ``GET /health/ready``                              version and migrations (no token)
* ``GET /v1/snapshots?kind&cut_type&business_date&latest``  list, newest first, ``meta.next_offset``
* ``GET /v1/snapshots/{id}/manifest``                envelope
* ``GET /v1/snapshots/{id}/bundle``                  gzipped canonical JSON, ``X-Snapshot-Sha256``
* ``GET /v1/history/index|bonds|curves/{ds}|zcyc|fixings|fx``  hub 0.2.0 (R24): columnar pages (``history_page``),
                                                     ``next`` cursor, gzip, weak ETag -> 304, numbers as ``Decimal``;
                                                     scope ``read`` (403 -> ``токен без права read``), a hub before
                                                     0.2.0 answers 404 (``hub без API истории (версия < 0.2.0)``)

Two transports behind one client (``hub_transport``, settings): ``python``, the standard library (``urllib``
honours ``HTTPS_PROXY``/``NO_PROXY`` and, on Windows, the Internet Options proxy; a proxy login only as Basic,
from ``proxy_user`` and its password), and ``curl``, the system ``curl`` (on Windows 10/11
``System32\\curl.exe``: Schannel TLS with the Windows certificate store and SSPI, so ``--proxy-anyauth`` with
``--proxy-user :`` logs in to an NTLM/Negotiate proxy as the current Windows user). ``auto`` (the default) starts
with Python and switches to curl for the rest of the process when the proxy refuses the tunnel (407 and the like).
A 407 names the schemes the proxy asks for (``Proxy-Authenticate``) in the error.

TLS trust of the Python transport is the operating system's verifier through ``truststore`` when it is installed
(on Windows: the certificate store with the automatic root download and the corporate roots a policy pushes, where
a TLS-inspecting proxy's CA usually is), else the ``ssl`` default; ``DESK_HUB_CA_BUNDLE`` replaces either
(``tls_context``; curl gets it as ``--cacert``). The token goes only into the ``Authorization`` header (for curl:
a private config file in ``<home>``, deleted after the request, never the command line) and never into a message,
a URL or a log line; so does the proxy password.
"""

from __future__ import annotations

import gzip
import http.client
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from pathlib import Path
from typing import Any

from ..settings import Settings

USER_AGENT = "desk-backend"


def tls_context(ca_bundle: Any = None) -> ssl.SSLContext:
    """The hub's TLS context: the given CA bundle alone; without one the OS trust store (``truststore``), falling
    back silently to ``ssl.create_default_context()`` when truststore is missing or cannot be used."""
    if ca_bundle:
        return ssl.create_default_context(cafile=str(ca_bundle))
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 - not installed, or an OS it does not support: the stdlib default
        return ssl.create_default_context()


class HubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class HubUnavailable(HubError):
    """No answer or a 5xx from the hub after the retries: the hub is down, not one request's fault (#147).
    ``last_status``: the last attempt's 5xx, None when it got no HTTP answer at all."""

    def __init__(self, message: str, last_status: int | None = None) -> None:
        super().__init__(message)
        self.last_status = last_status


@dataclass
class Response:
    status: int
    headers: Message
    body: bytes


HISTORY_PREFIX = "/v1/history"
NO_HISTORY_API = "hub без API истории (версия < 0.2.0)"
NO_READ_SCOPE = "токен без права read"


class HistoryUnavailable(HubError):
    """The hub's history API cannot be used: ``code`` ``no_api`` (404: a hub before 0.2.0), ``no_read`` (403: the
    token has no scope ``read``) or ``unauthorized`` (401). The desk then falls back to the snapshots."""

    def __init__(self, message: str, status: int, code: str) -> None:
        super().__init__(message, status)
        self.code = code


@dataclass
class HistoryPage:
    """A page of the columnar envelope: ``rows[i][j]`` is the value of ``columns[j]`` (numbers ``Decimal``)."""

    dataset: str | None
    columns: list[str]
    rows: list[list[Any]]
    next: str | None
    data_version: str | None
    as_of: str | None
    meta: dict[str, Any]
    etag: str | None
    not_modified: bool = False
    wire_bytes: int = 0

    def records(self) -> Iterator[dict[str, Any]]:
        cols = self.columns
        return (dict(zip(cols, r, strict=True)) for r in self.rows)


class TransportError(Exception):
    """No HTTP answer from the hub. ``proxy_status``: the proxy refused the tunnel (or a plain request) with this
    code; ``schemes``: its ``Proxy-Authenticate`` schemes on a 407."""

    def __init__(self, message: str, proxy_status: int | None = None, schemes: list[str] | None = None) -> None:
        super().__init__(message)
        self.proxy_status = proxy_status
        self.schemes = schemes or []


def hub_proxy(url: str) -> str | None:
    """The proxy urllib would use for ``url`` (environment, else on Windows the Internet Options), None when the
    request goes direct (no proxy, or ``NO_PROXY``)."""
    u = urllib.parse.urlsplit(url)
    proxy = urllib.request.getproxies().get(u.scheme)
    if not proxy or urllib.request.proxy_bypass(u.hostname or ""):
        return None
    return proxy if "://" in proxy else "http://" + proxy


def split_proxy(proxy: str) -> tuple[str, str | None, str | None]:
    """``(proxy URL without user:password, user, password)``; user and password percent-decoded."""
    u = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    host = u.hostname or ""
    host = f"[{host}]" if ":" in host else host
    bare = f"{u.scheme}://{host}" + (f":{u.port}" if u.port else "")
    unq = urllib.parse.unquote
    return bare, (unq(u.username) if u.username is not None else None), \
        (unq(u.password) if u.password is not None else None)


def with_credentials(proxy: str, user: str, password: str | None) -> str:
    """The proxy URL carrying ``user:password`` (percent-encoded: ``DOMAIN\\user`` works) for urllib's Basic."""
    bare, _, _ = split_proxy(proxy)
    scheme, rest = bare.split("://", 1)
    q = lambda v: urllib.parse.quote(v, safe="")
    return f"{scheme}://{q(user)}:{q(password or '')}@{rest}"


def auth_schemes(values: list[str] | None) -> list[str]:
    """Scheme names of ``Proxy-Authenticate`` header values (``Negotiate``, ``NTLM``, ``Basic realm="x"``)."""
    out: list[str] = []
    for v in values or []:
        for part in v.split(","):
            word = part.strip().split(" ", 1)[0]
            if word and "=" not in word and word not in out:
                out.append(word)
    return out


def probe_proxy_schemes(proxy: str, url: str, timeout: float) -> list[str]:
    """The ``Proxy-Authenticate`` schemes of a bare ``CONNECT`` (no credentials) to the hub through ``proxy``;
    Python 3.11's urllib drops the headers of a refused tunnel. Empty on any failure."""
    u, p = urllib.parse.urlsplit(url), urllib.parse.urlsplit(split_proxy(proxy)[0])
    target = f"{u.hostname}:{u.port or (443 if u.scheme == 'https' else 80)}"
    try:
        conn = http.client.HTTPConnection(p.hostname or "", p.port or 80, timeout=min(timeout, 15))
        try:
            conn.request("CONNECT", target, headers={"Host": target, "User-Agent": USER_AGENT})
            r = conn.getresponse()
            return auth_schemes(r.msg.get_all("Proxy-Authenticate")) if r.status == 407 else []
        finally:
            conn.close()
    except (OSError, http.client.HTTPException):
        return []


class PythonTransport:
    """urllib; the proxy of ``hub_proxy`` with ``proxy_user``/password as Basic when they are set."""

    name = "python"

    def __init__(self, settings: Settings, url: str) -> None:
        self.timeout = settings.hub_timeout_s
        self.proxy = hub_proxy(url)
        handlers: list[Any] = [urllib.request.HTTPSHandler(context=tls_context(settings.hub_ca_bundle))]
        if self.proxy and settings.proxy_user:
            scheme = urllib.parse.urlsplit(url).scheme
            handlers.append(urllib.request.ProxyHandler(
                {scheme: with_credentials(self.proxy, settings.proxy_user, settings.proxy_password)}))
        self._opener = urllib.request.build_opener(*handlers)

    def get(self, url: str, token: str | None, headers: dict[str, str] | None = None) -> Response:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity",
                                                   **(headers or {})})
        if token:
            req.add_unredirected_header("Authorization", f"Bearer {token}")
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return Response(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except (OSError, http.client.HTTPException):
                body = b""
            return Response(e.code, e.headers, body)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
            # OSError: a TLS drop (ssl.SSLError) or a reset while reading the body, HTTPException: a cut-off body
            reason = str(getattr(e, "reason", e))
            m = re.search(r"Tunnel connection failed: (\d{3})", reason)
            if m and self.proxy:
                code = int(m.group(1))
                schemes = probe_proxy_schemes(self.proxy, url, self.timeout) if code == 407 else []
                raise TransportError(reason, code, schemes) from None
            raise TransportError(reason) from None


def find_curl() -> str | None:
    """``%SystemRoot%\\System32\\curl.exe`` on Windows (Schannel + SSPI), else ``curl`` on PATH."""
    if os.name == "nt":
        exe = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "curl.exe"
        if exe.is_file():
            return str(exe)
    return shutil.which("curl")


_curl_versions: dict[str, tuple[int, ...]] = {}


def curl_version(curl: str) -> tuple[int, ...]:
    if curl not in _curl_versions:
        try:
            out = subprocess.run([curl, "--version"], capture_output=True, timeout=15, check=False,
                                 creationflags=_NO_WINDOW).stdout.decode("ascii", "replace")
            m = re.match(r"curl (\d+)\.(\d+)\.(\d+)", out)
            _curl_versions[curl] = tuple(map(int, m.groups())) if m else (0,)
        except (OSError, subprocess.SubprocessError):
            _curl_versions[curl] = (0,)
    return _curl_versions[curl]


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _q(value: str) -> str:
    """A curl config file value: double quotes, ``\\`` and ``"`` escaped; one line only."""
    if "\n" in value or "\r" in value:
        raise HubError("a value for curl holds a line break")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class CurlTransport:
    """The system curl, one process per request. Secrets (token, proxy password) go into a config file (``-K``)
    in ``<home>``, private and deleted afterwards; the body streams to a file there."""

    name = "curl"

    def __init__(self, settings: Settings, url: str, curl: str | None = None) -> None:
        self.curl = curl or find_curl()
        if not self.curl:
            raise HubError("hub_transport curl: no curl found (Windows 10/11: C:\\Windows\\System32\\curl.exe)")
        self.timeout = settings.hub_timeout_s
        self.home = settings.home
        self.ca_bundle = settings.hub_ca_bundle
        self.proxy = hub_proxy(url)
        self.proxy_user, self.proxy_password = settings.proxy_user, settings.proxy_password
        self.windows = os.name == "nt"

    def config(self, token: str | None, headers: dict[str, str] | None = None) -> str:
        extra = {"Accept-Encoding": "identity", **(headers or {})}  # no --compressed: the client gunzips (as Python)
        lines = [f"header = {_q('User-Agent: ' + USER_AGENT)}"] + [f"header = {_q(f'{k}: {v}')}"
                                                                   for k, v in extra.items()]
        if token:
            lines.append(f"header = {_q('Authorization: Bearer ' + token)}")
        if self.proxy:
            bare, user, password = split_proxy(self.proxy)
            if self.proxy_user:
                user, password = self.proxy_user, self.proxy_password
            login = f"{user}:{password or ''}" if user else ":"  # ':' = the current Windows logon (SSPI)
            lines += [f"proxy = {_q(bare)}", "proxy-anyauth", f"proxy-user = {_q(login)}",
                      'noproxy = ""']  # NO_PROXY was urllib's to apply (hub_proxy), not curl's again
        else:
            lines.append(f"noproxy = {_q('*')}")  # urllib's verdict (NO_PROXY, no proxy) holds for curl too
        if self.windows and curl_version(self.curl) >= (7, 70):
            lines.append("ssl-revoke-best-effort")  # Schannel's revocation check fails behind many proxies
        if self.ca_bundle:
            lines.append(f"cacert = {_q(str(self.ca_bundle))}")
        return "\n".join(lines) + "\n"

    def command(self, url: str, body: Path, heads: Path, cfg: Path) -> list[str]:
        t = f"{self.timeout:g}"
        return [self.curl, "-q", "-sS", "--location", "--connect-timeout", t, "--speed-limit", "1", "--speed-time", t,
                "--max-time", f"{max(self.timeout * 30, 600):g}", "-o", str(body), "-D", str(heads),
                "-w", "%{http_code} %{http_connect}", "-K", str(cfg), url]

    def get(self, url: str, token: str | None, headers: dict[str, str] | None = None) -> Response:
        self.home.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        try:
            for suffix in (".cfg", ".body", ".head"):
                fd, name = tempfile.mkstemp(prefix=".curl-", suffix=suffix, dir=self.home)
                os.close(fd)  # mkstemp: mode 0600 on POSIX; on Windows <home> is the user's
                paths.append(Path(name))
            cfg, body, heads = paths
            cfg.write_text(self.config(token, headers), encoding="utf-8")
            try:
                r = subprocess.run(self.command(url, body, heads, cfg), capture_output=True, check=False,
                                   timeout=max(self.timeout * 30, 600) + 30, creationflags=_NO_WINDOW)
            except subprocess.TimeoutExpired:
                raise TransportError(f"curl: no answer in {max(self.timeout * 30, 600):g} s") from None
            codes = r.stdout.decode("ascii", "replace").split()
            status = int(codes[0]) if codes and codes[0].isdigit() else 0
            connect = int(codes[1]) if len(codes) > 1 and codes[1].isdigit() else 0
            blocks = _header_blocks(heads.read_bytes())
            if r.returncode:
                msg = r.stderr.decode("utf-8", "replace").strip().splitlines()
                text = msg[-1] if msg else f"curl exit code {r.returncode}"
                if connect and connect != 200:
                    refused = [m for c, m in blocks if c == connect]
                    schemes = auth_schemes(refused[-1].get_all("Proxy-Authenticate")) if refused else []
                    raise TransportError(text, connect, schemes)
                raise TransportError(text)
            final = blocks[-1][1] if blocks else Message()
            return Response(status, final, body.read_bytes())
        finally:
            for p in paths:
                p.unlink(missing_ok=True)


def _header_blocks(raw: bytes) -> list[tuple[int, Message]]:
    """The response header blocks curl's ``-D`` wrote (a proxy's CONNECT answer, redirects, the final one)."""
    out = []
    for block in re.split(rb"\r?\n\r?\n", raw):
        lines = block.lstrip(b"\r\n").split(b"\n", 1)
        m = re.match(rb"HTTP/[\d.]+ (\d{3})", lines[0])
        if m:
            out.append((int(m.group(1)), http.client.parse_headers(io.BytesIO(lines[1] + b"\r\n\r\n"
                                                                           if len(lines) > 1 else b"\r\n"))))
    return out


_auto_curl = False  # hub_transport auto: the proxy refused Python's tunnel once, curl serves this process


class HubClient:
    retries = 3
    backoff_s = 1.0

    def __init__(self, settings: Settings) -> None:
        if not settings.hub_url:
            raise HubError("hub address not configured; set DESK_HUB_URL (environment or backend/.env)")
        self.url = settings.hub_url
        self.host = urllib.parse.urlsplit(self.url).hostname or ""
        self._token = settings.hub_token
        self._settings = settings
        self.mode = settings.hub_transport
        if self.mode == "curl" or (self.mode == "auto" and _auto_curl and find_curl()):
            self.transport: PythonTransport | CurlTransport = CurlTransport(settings, self.url)
        else:
            self.transport = PythonTransport(settings, self.url)

    def _switch_to_curl(self) -> bool:
        """auto: the proxy refused Python; use curl from now on (this process) if there is one."""
        global _auto_curl
        if self.mode != "auto" or isinstance(self.transport, CurlTransport) or not (curl := find_curl()):
            return False
        self.transport = CurlTransport(self._settings, self.url, curl)
        _auto_curl = True
        return True

    def _proxy_refused(self, status: int, schemes: list[str]) -> HubError:
        proxy = split_proxy(self.transport.proxy)[0].split("://", 1)[-1] if self.transport.proxy else "?"
        asks = f"прокси требует: {', '.join(schemes)}" if schemes else "прокси не назвал способ входа"
        if status != 407:
            return HubError(f"{self.host} unreachable: proxy {proxy} refused the connection ({status})", status)
        if isinstance(self.transport, CurlTransport):
            hint = ("; curl could not log in " + ("with the proxy login and password set" if self.proxy_creds()
                                                 else "as the current Windows user: set the proxy login "
                                                      "(DOMAIN\\user) and password"))
        else:
            hint = ("; the python transport only does Basic" + (" and the proxy refused the login set"
                                                                 if self.proxy_creds() else
                                                                 ": set the proxy login and password, or the "
                                                                 "connection method curl (Windows logon)")
                    + (" (no curl found)" if self.mode == "auto" else ""))
        return HubError(f"{self.host} unreachable: proxy {proxy} answers 407 Proxy Authentication Required "
                        f"({asks}) [{self.transport.name}]{hint}", 407)

    def proxy_creds(self) -> bool:
        return bool(self._settings.proxy_user or (self.transport.proxy and split_proxy(self.transport.proxy)[1]))

    # -- transport -------------------------------------------------------------------------
    def _fetch(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True,
               headers: dict[str, str] | None = None, scope: str = "snapshots") -> Response:
        """One GET with the retries: a 2xx (or a 304 when ``headers`` revalidate) answer; 401, 403, other 4xx raise
        at once (``scope`` names the token scope in the 403 message), 5xx and no answer are retried."""
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = self.url + path + (f"?{query}" if query else "")
        if auth and not self._token:
            raise HubError("hub token not configured; set DESK_HUB_TOKEN_FILE (or DESK_HUB_TOKEN)")
        last, last_status = "", None
        attempt = 0
        while attempt <= self.retries:
            if attempt:
                time.sleep(self.backoff_s * 2 ** (attempt - 1))
            attempt += 1
            try:
                r = self.transport.get(url, self._token if auth else None, headers)
            except TransportError as e:
                if e.proxy_status:
                    if self._switch_to_curl():
                        attempt -= 1  # the same attempt again, through curl
                        continue
                    if e.proxy_status == 407:
                        raise self._proxy_refused(407, e.schemes) from None
                last, last_status = str(e), None
                continue
            if r.status == 407:  # a plain-HTTP proxy's answer
                if self._switch_to_curl():
                    attempt -= 1
                    continue
                raise self._proxy_refused(407, auth_schemes(r.headers.get_all("Proxy-Authenticate")))
            if r.status == 401:
                raise HubError(f"token rejected by {self.host} (401)", 401)
            if r.status == 403:
                raise HubError(f"token lacks scope '{scope}' on {self.host} (403)", 403)
            if 200 <= r.status < 300 or (r.status == 304 and headers and "If-None-Match" in headers):
                return r
            if r.status < 500:
                raise HubError(f"{path}: {r.status} {_message(r)}", r.status)
            last, last_status = f"{r.status} {_message(r)}", r.status
        raise HubUnavailable(f"{self.host} unreachable after {self.retries + 1} attempts: {last}", last_status)

    def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> tuple[bytes, Any]:
        r = self._fetch(path, params, auth=auth)
        return r.body, r.headers

    def _json(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> dict[str, Any]:
        body, _ = self._get(path, params, auth=auth)
        return json.loads(body)

    # -- API -------------------------------------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._json("/health/ready", auth=False)

    def list_snapshots(self, kind: str | None = None, cut_type: str | None = None,
                       business_date: str | None = None, latest: bool = False) -> Iterator[dict[str, Any]]:
        """Rows newest first; follows ``meta.next_offset`` lazily."""
        params: dict[str, Any] = {"kind": kind, "cut_type": cut_type, "business_date": business_date,
                                  "latest": "true" if latest else None}
        offset: int | None = 0
        while offset is not None:
            try:
                page = self._json("/v1/snapshots", {**params, "offset": offset or None})
            except HubError as e:
                if e.status == 404:
                    raise HubError(f"hub {self.host} (version {self.health().get('version')}) has no snapshot "
                                   "layer: /v1/snapshots answers 404") from None
                raise
            yield from page["data"]
            offset = page["meta"].get("next_offset")

    def manifest(self, snapshot_id: str) -> dict[str, Any]:
        return self._json(f"/v1/snapshots/{snapshot_id}/manifest")

    def bundle(self, snapshot_id: str) -> bytes:
        """The gzipped body; the caller verifies it (``desk.marketdata.cache``)."""
        body, headers = self._get(f"/v1/snapshots/{snapshot_id}/bundle")
        if (headers.get("X-Snapshot-Sha256") or snapshot_id) != snapshot_id:
            raise HubError(f"bundle {snapshot_id[:12]}: X-Snapshot-Sha256 names another snapshot")
        return body


    # -- history API (hub 0.2.0, WEB-15: docs/HISTORY_API.md of the hub) ------------------------------------------
    def history_page(self, path: str, params: dict[str, Any], *, cursor: str | None = None,
                     etag: str | None = None) -> HistoryPage:
        """One page of ``/v1/history{path}``: gzip asked for, numbers as ``Decimal``; ``etag`` revalidates (a 304 is
        a page with ``not_modified`` and no rows). A hub without the API (404 on ``/index``) or a token without
        scope ``read`` (403) raise ``HistoryUnavailable`` with the reason the desk shows."""
        headers = {"Accept-Encoding": "gzip", "Accept": "application/json"}
        if etag:
            headers["If-None-Match"] = etag
        try:
            r = self._fetch(HISTORY_PREFIX + path, {**params, "cursor": cursor}, headers=headers, scope="read")
        except HubError as e:
            if e.status == 404 and path == "/index":
                raise HistoryUnavailable(NO_HISTORY_API, 404, "no_api") from None
            if e.status == 403:
                raise HistoryUnavailable(NO_READ_SCOPE, 403, "no_read") from None
            if e.status == 401:
                raise HistoryUnavailable(f"токен не принят хабом {self.host} (401)", 401, "unauthorized") from None
            raise
        tag = r.headers.get("ETag")
        if r.status == 304:
            return HistoryPage(None, [], [], None, None, None, {}, tag or etag, True, 0)
        raw = r.body
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except (OSError, EOFError) as e:
                raise HubError(f"{path}: not a complete gzip body ({e})") from None
        try:
            doc = json.loads(raw, parse_float=Decimal)
            return HistoryPage(doc.get("dataset"), list(doc["columns"]), doc["rows"], doc.get("next"),
                               doc.get("data_version"), doc.get("as_of"), doc.get("meta") or {}, tag, False,
                               len(r.body))
        except (ValueError, KeyError, TypeError) as e:
            raise HubError(f"{path}: not a history envelope ({type(e).__name__}: {e})") from None

    def history_pages(self, path: str, params: dict[str, Any]) -> Iterator[HistoryPage]:
        """Every page of a history query, following ``next`` with the same parameters."""
        page = self.history_page(path, params)
        yield page
        while page.next:
            page = self.history_page(path, params, cursor=page.next)
            yield page

    def history_index(self) -> list[dict[str, Any]]:
        """``/v1/history/index``: one dict per dataset / series (``dataset``, ``series``, ``source``, ``unit``,
        ``date_from``, ``date_to``, ``rows``, ``days``, ``series_count``, ``endpoint``)."""
        return [r for p in self.history_pages("/index", {}) for r in p.records()]


def _message(r: Response) -> str:
    try:
        return json.loads(r.body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return http.client.responses.get(r.status, "")


def hub_check(settings: Settings, cut: str = "eod_final", retries: int | None = None) -> dict[str, Any]:
    """``desk hub-check`` as data (the CLI and the settings page): ``{"ok", "host", "health": {status, version,
    migrations} | None, "token_accepted", "latest": {cut, business_date, snapshot_id, published_at, age_hours} |
    None, "error": str | None, "history_api": history_probe() | None}``. Never raises; the token is never part of
    the result. ``history_api`` (the hub reached): whether the history API serves this token; ``ok`` does not
    depend on it (without it the desk loads the history from the snapshots)."""
    out: dict[str, Any] = {"ok": False, "host": None, "health": None, "token_accepted": False, "latest": None,
                           "error": None, "transport": None, "history_api": None}
    client = None
    try:
        client = HubClient(settings)
        if retries is not None:
            client.retries = retries
        out["host"] = client.host
        h = client.health()
        out["health"] = {k: h.get(k) for k in ("status", "version", "migrations")}
        out["history_api"] = history_probe(client)
        latest = next(client.list_snapshots(kind="market", cut_type=cut, latest=True), None)
        out["token_accepted"] = True
        if latest is None:
            out["error"] = f"the hub has published no {cut} market snapshot yet"
            return out
        age = None
        try:
            age = round((datetime.now(UTC) - datetime.fromisoformat(latest["published_at"])).total_seconds() / 3600, 1)
        except (TypeError, ValueError, KeyError):
            pass
        out["latest"] = {"cut": cut, **{k: latest.get(k) for k in ("business_date", "snapshot_id", "published_at")},
                         "age_hours": age}
        out["ok"] = h.get("status") == "ok"
        if not out["ok"]:
            out["error"] = f"hub status {h.get('status')!r}"
    except HubError as e:
        out["error"] = str(e)
    except (OSError, ValueError) as e:  # a missing CA bundle, TLS (ssl.SSLError is an OSError), a body not JSON
        out["error"] = f"{type(e).__name__}: {e}"
    if client is not None:
        out["transport"] = client.transport.name
    return out


def history_probe(client: HubClient) -> dict[str, Any]:
    """``{"available", "code", "reason", "datasets", "series"}`` of the hub's history API for this token: one call of
    ``/v1/history/index``. ``code``: ``ok``, ``no_api`` (a hub before 0.2.0), ``no_read`` (the token lacks scope
    ``read``), ``unauthorized`` or ``error``; ``reason`` is the text the desk shows (Russian for the first two)."""
    try:
        idx = client.history_index()
    except HistoryUnavailable as e:
        return {"available": False, "code": e.code, "reason": str(e), "datasets": None, "series": None}
    except (HubError, OSError, ValueError) as e:
        return {"available": False, "code": "error", "reason": f"{type(e).__name__}: {e}", "datasets": None,
                "series": None}
    return {"available": True, "code": "ok", "reason": None, "datasets": sorted({r.get("dataset") for r in idx
                                                                                 if r.get("dataset")}),
            "series": len(idx)}
