"""HTTP client of the hub's snapshot API (slice S3); the only module that knows the hub's paths.

* ``GET /health/ready``                              version and migrations (no token)
* ``GET /v1/snapshots?kind&cut_type&business_date&latest``  list, newest first, ``meta.next_offset``
* ``GET /v1/snapshots/{id}/manifest``                envelope
* ``GET /v1/snapshots/{id}/bundle``                  gzipped canonical JSON, ``X-Snapshot-Sha256``

Transport is the standard library: ``urllib`` honours ``HTTPS_PROXY``/``NO_PROXY`` and, on Windows,
the system certificate store (a TLS-inspecting proxy's CA is usually there); ``DESK_HUB_CA_BUNDLE``
replaces the default trust. The token goes only into the ``Authorization`` header and never into
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
from typing import Any

from ..settings import Settings

USER_AGENT = "desk-backend"


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
        ctx = ssl.create_default_context(cafile=str(settings.hub_ca_bundle) if settings.hub_ca_bundle else None)
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
