"""HTTP client of the hub's snapshot API (slice S3); the only module that knows the hub's paths.

* ``GET /health/ready``                              version and migrations (no token)
* ``GET /v1/snapshots?kind&cut_type&business_date&latest``  list, newest first, ``meta.next_offset``
* ``GET /v1/snapshots/{id}/manifest``                envelope
* ``GET /v1/snapshots/{id}/bundle``                  gzipped canonical JSON, ``X-Snapshot-Sha256``

Transport is the standard library: ``urllib`` honours ``HTTPS_PROXY``/``NO_PROXY``. TLS trust is the operating
system's verifier through ``truststore`` when it is installed (on Windows: the certificate store with the automatic
root download and the corporate roots a policy pushes, where a TLS-inspecting proxy's CA usually is), else the
``ssl`` default; ``DESK_HUB_CA_BUNDLE`` replaces either (``tls_context``). The token goes only into the ``Authorization`` header and never into
a message, a URL or a log line.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
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


class HubClient:
    retries = 3
    backoff_s = 1.0

    def __init__(self, settings: Settings) -> None:
        if not settings.hub_url:
            raise HubError("hub address not configured; set DESK_HUB_URL (environment or backend/.env)")
        self.url = settings.hub_url
        self.host = urllib.parse.urlsplit(self.url).hostname or ""
        self._token = settings.hub_token
        self._timeout = settings.hub_timeout_s
        ctx = tls_context(settings.hub_ca_bundle)
        self._opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))

    # -- transport -------------------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None, *, auth: bool = True) -> tuple[bytes, Any]:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        req = urllib.request.Request(self.url + path + (f"?{query}" if query else ""),
                                     headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
        if auth:
            if not self._token:
                raise HubError("hub token not configured; set DESK_HUB_TOKEN_FILE (or DESK_HUB_TOKEN)")
            req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        last = ""
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff_s * 2 ** (attempt - 1))
            try:
                with self._opener.open(req, timeout=self._timeout) as r:
                    return r.read(), r.headers
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise HubError(f"token rejected by {self.host} (401)", 401) from None
                if e.code == 403:
                    raise HubError(f"token lacks scope 'snapshots' on {self.host} (403)", 403) from None
                if e.code < 500:
                    raise HubError(f"{path}: {e.code} {_message(e)}", e.code) from None
                last = f"{e.code} {_message(e)}"
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = str(getattr(e, "reason", e))
        raise HubError(f"{self.host} unreachable after {self.retries + 1} attempts: {last}")

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


def _message(e: urllib.error.HTTPError) -> str:
    try:
        return json.loads(e.read())["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return e.reason or ""


def hub_check(settings: Settings, cut: str = "eod_final", retries: int | None = None) -> dict[str, Any]:
    """``desk hub-check`` as data (the CLI and the settings page): ``{"ok", "host", "health": {status, version,
    migrations} | None, "token_accepted", "latest": {cut, business_date, snapshot_id, published_at, age_hours} |
    None, "error": str | None}``. Never raises; the token is never part of the result."""
    out: dict[str, Any] = {"ok": False, "host": None, "health": None, "token_accepted": False, "latest": None,
                           "error": None}
    try:
        client = HubClient(settings)
        if retries is not None:
            client.retries = retries
        out["host"] = client.host
        h = client.health()
        out["health"] = {k: h.get(k) for k in ("status", "version", "migrations")}
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
    return out
