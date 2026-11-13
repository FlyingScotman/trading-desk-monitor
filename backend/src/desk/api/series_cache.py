"""What the bodies of a new run take from the runs of the earlier dates, kept between builds (TDM#736).

A complete run never changes (its rows, its summary), and a new intraday run of a date leaves the runs of the earlier dates
as they were. Yet the pages' bodies (``/desk-data.js``, ``/concepts-data.js``, the positions' history) took the series of
a year of them from the store again for every new run: about 33 seconds at the owner's size, a few minutes apart, on the
machine the desk works on. The pieces that are a function of a complete run alone are kept here, in memory, by the run
(its id and its completion time, which another run of the date, a recompute, never shares); the pieces that depend on
the set of runs (the month's and the year's sums) are kept by that set (``pnl_mtd.PeriodCache``). Nothing here changes a
number: each piece is what the same code gave from the same rows, and a body built with the cache is the body built
without it (``tests/test_series_cache_736.py``).

``SeriesCache`` is the app's (``app.state.series_cache``); the builders take it as an optional argument and build from
the store alone without it."""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from . import pnl_mtd


class Memo:
    """A bounded, thread safe map, least recently used first out: what one complete run gives, by ``(run id, completion
    time)``."""

    def __init__(self, size: int = 1024) -> None:
        self.size = size
        self._items: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key: Any) -> Any | None:
        with self._lock:
            val = self._items.get(key)
            if val is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return val

    def put(self, key: Any, val: Any) -> Any:
        with self._lock:
            self._items[key] = val
            self._items.move_to_end(key)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return val

    def __len__(self) -> int:
        return len(self._items)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"items": len(self._items), "hits": self.hits, "misses": self.misses}


class SeriesCache:
    """The kept pieces, one object for the app: ``period`` (the year's and the month's sums of the runs before the run
    shown, ``pnl_mtd.PeriodCache``) and ``days`` (a past run's line of ``window.DESK.history``: its official and explained
    PnL, DV01, CS01 and the state of the official PnL, ``compat.assemble``)."""

    def __init__(self) -> None:
        self.period = pnl_mtd.PeriodCache()
        self.days = Memo()

    def stats(self) -> dict[str, dict[str, int]]:
        return {"period": self.period.stats(), "days": self.days.stats()}
