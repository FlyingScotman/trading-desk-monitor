"""The slow requests of this server run (TDM#727): the last ``KEEP`` requests that took ``threshold_s`` or longer, for the
settings page's «Диагностика» → «Медленные запросы» and the diagnostics file, so that the owner can tell which request
is slow on the work machine.

A row holds no data values: the route as declared (``/api/v1/runs/{run_id}/positions``, not the run id or a position
id in it), the *names* of the query parameters (never their values), the method, the status, the time in ms, whether the
answer came from a cache (``hit``) or a body was built for it (``miss``; None: the route has no such cache) and the size
of the answer in bytes. The same line goes to the server log (``desk.api``, WARNING)."""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("desk.api")

ENV_S = "DESK_SLOW_REQUEST_S"
DEFAULT_S = 2.0
KEEP = 100
SKIP = ("/api/v1/events/stream",)
"""Long by design (the response is open for minutes): never listed."""


def threshold() -> float:
    """Seconds from ``DESK_SLOW_REQUEST_S`` (default ``DEFAULT_S``; a bad value: the default; 0 lists every request)."""
    raw = os.environ.get(ENV_S, "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_S
    except ValueError:
        return DEFAULT_S


def route_of(request: Any) -> str:
    """The route's template (``/api/v1/runs/{run_id}/risk``): no id of a run, position or instrument. A path no route
    matches (a page of the static folder, a 404) as it is, cut at 120 characters."""
    template = getattr(request.scope.get("route"), "path", None)
    return template or request.url.path[:120]


class SlowLog:
    def __init__(self, threshold_s: float | None = None, keep: int = KEEP) -> None:
        self.threshold_s = threshold() if threshold_s is None else threshold_s
        self._rows: deque[dict[str, Any]] = deque(maxlen=keep)
        self._lock = threading.Lock()
        self.total = 0
        """How many slow requests there were since the start (the list keeps the last ``KEEP``)."""

    def add(self, request: Any, status: int, seconds: float, size: int | None = None) -> dict[str, Any] | None:
        """Note a finished request when it was slow; the row, or None."""
        route = route_of(request)
        if seconds < self.threshold_s or route in SKIP:
            return None
        cache = getattr(request.state, "cache", None)
        if size is None:
            size = getattr(request.state, "body_bytes", None)
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "method": request.method, "route": route,
               "query": sorted(request.query_params.keys()), "status": status, "ms": round(seconds * 1e3),
               "cache": cache, "bytes": size}
        with self._lock:
            self._rows.append(row)
            self.total += 1
        log.warning("slow request %s %s?%s %s %d ms, cache %s, %s bytes", row["method"], route,
                    ",".join(row["query"]), status, row["ms"], cache or "-", size if size is not None else "-")
        return row

    def rows(self) -> list[dict[str, Any]]:
        """The last slow requests, the newest first."""
        with self._lock:
            return list(reversed(self._rows))

    def snapshot(self) -> dict[str, Any]:
        return {"threshold_s": self.threshold_s, "total": self.total, "keep": KEEP, "requests": self.rows()}
