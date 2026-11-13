"""The finished bodies of the per-run API (TDM#631): ``/api/v1/runs/{id}/positions``, ``/risk``, ``/pnl`` and the like.

A run that is ``complete`` never changes, yet each request rebuilt its answer (a read of every position, the models, the
JSON: 1.1-1.4 s for 14 MB on a book of 2000 positions). ``RunBodyCache`` keeps the answer: the body, its gzip made once
and its ETag, by ``(route, run id, parameters)``, so a repeat is a memory read and a browser's conditional request a 304.

* Valid while the **token** is the same: what the body also depends on besides the run (the run's status and completion
  time; for the positions the revision of the aliases, which are joined at read time). A new token replaces the entry.
* Bounded: at most ``max_bytes`` (body and gzip together) and ``max_items``, the least recently used dropped; an entry
  larger than the whole cache is not kept. ``DESK_RUN_CACHE_MB`` sets the size (0 turns the cache off).
* Reset when a run goes: ``forget_runs(ids)`` drops the entries of the runs a prune deleted (``desk.store``), in every
  cache of this process; ``retain(ids)`` drops the entries of any run not in ``ids`` (the serve loop, for a run deleted by
  another process). A replaced run keeps its id and its body (it is not touched; the replacing run has an id of its own).
* One build per key at a time: concurrent requests for the same key wait for the first instead of building it again.

Only whole runs are cached: a run that is not ``complete`` is answered fresh each time (its rows are still being written).
"""

from __future__ import annotations

import gzip
import hashlib
import os
import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_MB = 256
DEFAULT_ITEMS = 128
ENV_MB = "DESK_RUN_CACHE_MB"
Key = tuple[Any, ...]
_CACHES: weakref.WeakSet[RunBodyCache] = weakref.WeakSet()


@dataclass(frozen=True)
class Entry:
    body: bytes
    gz: bytes
    etag: str
    token: Any
    built_s: float

    @property
    def size(self) -> int:
        return len(self.body) + len(self.gz)


def cache_bytes() -> int:
    """The size from ``DESK_RUN_CACHE_MB`` (default ``DEFAULT_MB``; 0 or a bad value other than a number: off or default)."""
    raw = os.environ.get(ENV_MB, "").strip()
    try:
        mb = float(raw) if raw else DEFAULT_MB
    except ValueError:
        mb = DEFAULT_MB
    return int(max(0.0, mb) * (1 << 20))


class RunBodyCache:
    def __init__(self, max_bytes: int | None = None, max_items: int = DEFAULT_ITEMS) -> None:
        self.max_bytes = cache_bytes() if max_bytes is None else max_bytes
        self.max_items = max_items
        self._items: OrderedDict[Key, Entry] = OrderedDict()
        self._lock = threading.Lock()
        self._building: dict[Key, threading.Lock] = {}
        self.bytes = 0
        self.hits = self.misses = 0
        _CACHES.add(self)

    # -- lookup ----------------------------------------------------------------------------------------------------------

    def get(self, key: Key, token: Any) -> Entry | None:
        with self._lock:
            e = self._items.get(key)
            if e is None or e.token != token:
                return None
            self._items.move_to_end(key)
            return e

    def get_or_build(self, key: Key, token: Any, build: Callable[[], bytes]) -> Entry:
        """The entry of ``key`` valid for ``token``, built by ``build()`` (once, however many ask at the same time)."""
        return self.lookup(key, token, build)[0]

    def lookup(self, key: Key, token: Any, build: Callable[[], bytes]) -> tuple[Entry, bool]:
        """``get_or_build`` and whether the entry was there (True) or built for this call (False: a slow request that
        built its body says so, ``desk.api.slowlog``)."""
        e = self.get(key, token)
        if e is not None:
            self.hits += 1
            return e, True
        with self._lock:
            gate = self._building.setdefault(key, threading.Lock())
        with gate:
            try:
                e = self.get(key, token)  # built meanwhile by the request that held the gate
                if e is not None:
                    self.hits += 1
                    return e, True
                self.misses += 1
                t0 = time.perf_counter()
                body = build()
                e = Entry(body, gzip.compress(body, compresslevel=6), '"' + hashlib.sha256(body).hexdigest()[:32] + '"',
                          token, time.perf_counter() - t0)
                self._store(key, e)
                return e, False
            finally:
                with self._lock:
                    if self._building.get(key) is gate:
                        del self._building[key]

    def _store(self, key: Key, e: Entry) -> None:
        if e.size > self.max_bytes:  # the cache is off (0) or this body alone does not fit
            return
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self.bytes -= old.size
            self._items[key] = e
            self.bytes += e.size
            while self._items and (self.bytes > self.max_bytes or len(self._items) > self.max_items):
                _, gone = self._items.popitem(last=False)
                self.bytes -= gone.size

    # -- invalidation ----------------------------------------------------------------------------------------------------

    def drop_runs(self, run_ids: Any) -> int:
        """Drop every entry of these runs (key position 1 is the run id); returns how many."""
        ids = set(run_ids)
        with self._lock:
            keys = [k for k in self._items if len(k) > 1 and k[1] in ids]
            for k in keys:
                self.bytes -= self._items.pop(k).size
        return len(keys)

    def retain(self, run_ids: Any) -> int:
        """Drop the entries of every run that is not in ``run_ids`` (a run deleted by another process); how many."""
        keep = set(run_ids)
        with self._lock:
            keys = [k for k in self._items if len(k) > 1 and k[1] not in keep]
            for k in keys:
                self.bytes -= self._items.pop(k).size
        return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self.bytes = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"items": len(self._items), "bytes": self.bytes, "max_bytes": self.max_bytes, "hits": self.hits,
                    "misses": self.misses}


def forget_runs(run_ids: Any) -> int:
    """The runs were deleted (``store.prune_intraday``, ``store.prune_superseded``): drop their bodies from every cache of
    this process. Never raises."""
    ids = list(run_ids)
    n = 0
    if ids:
        for c in list(_CACHES):
            n += c.drop_runs(ids)
    return n
