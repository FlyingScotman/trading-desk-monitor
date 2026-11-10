"""Past dates computed in several processes (TDM#628): the backfill's runs of consecutive dates, one date in a process,
published in the order of the dates, the numbers those of the same dates run one after the other.

**What a date reads of the others.** Almost nothing: its market and its previous close come from the snapshot cache, its
positions and trades from the store's inputs. One thing is another run's: the model spreads of the runs before it
(``store.carried_spreads``), which a bond without a price of its own is valued with (its spread, and the date and the
run it comes from, are in the run). So the run of a date is not independent of the runs of the dates before it, and the
processes keep the order where it counts:

* every date's run, from the first to the last statement of the calculation, is one process's, as ``dayrun.eod_run``;
* the carried spreads are read when the valuation first looks one up (``LazyCarried``), not at the start, and only
  once the dates before this one are published (``Gate``), so they are those of the sequential run; a date that values
  no model price does not wait at all, and the dates ahead of it in the order go on meanwhile;
* a run is published in its turn, in the order of the dates, as the runs of the sequential backfill are (a later run's
  funding and OCP read the earlier ones, ``run.supersedes_run_id`` and the default run follow the order);
* the second pass of the history goes newest first and each date reads the dates *before* it, which that pass publishes
  later: its reads need no wait (``wait_carried`` False), its publications keep the order.

Only light things cross the process boundary, all picklable: the settings, the date, the store address, flags; the
processes answer with the run id, the lines the run printed and the error, if any. Nothing of core or of the prepared
markets is passed; each process prepares what it needs and keeps it for its next dates (``dayrun.ContextCache``). The
processes are started with ``spawn``, the only way on the target machine (Windows), and on every platform the same.
Each has its own connections to the store.

A date that fails in its process fails as it would alone (the error comes back, the original class when it can be
pickled). A process that dies takes the pipeline with it: the dates not yet published are run one after the other, in
this process, as before, and the run that was loading is reaped as any orphan.
"""

from __future__ import annotations

import logging
import multiprocessing
import pickle
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

from . import store
from .settings import Settings, auto_workers  # noqa: F401 (the default of ``backfill_workers``)

log = logging.getLogger("desk.parallel")

LOOKAHEAD = 1
"""Dates in flight beyond the number of processes: a process finishing a date has the next one waiting."""
POLL_S = 0.5


class Aborted(RuntimeError):
    """The pipeline is closed (the run stopped, or a process died): a date waiting for its turn does not publish."""


class PoolBroken(RuntimeError):
    """A process of the pool died; the dates not published are the caller's to run one after the other."""


class Shared:
    """What the starting process creates and every process of the pool is given (``initargs``): the number of dates
    published (in the order they were submitted) and the flag that closes the pipeline."""

    def __init__(self, mp_context: Any) -> None:
        self.cond = mp_context.Condition()
        self.done = mp_context.Value("q", 0, lock=False)
        self.abort = mp_context.Value("b", 0, lock=False)


class LazyCarried(Mapping):
    """The carried spreads of a date (``store.carried_spreads``), read at the first lookup, once. Behaves as the dict
    ``dayrun.eod_run`` reads at its start when it runs alone: a lookup of a bond, its length, iteration."""

    def __init__(self, load: Callable[[], dict[str, Any]]) -> None:
        self._load, self._data = load, None

    @property
    def loaded(self) -> bool:
        return self._data is not None

    def _get(self) -> dict[str, Any]:
        if self._data is None:
            self._data = self._load()
        return self._data

    def __getitem__(self, key: str) -> Any:
        return self._get()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._get())

    def __len__(self) -> int:
        return len(self._get())


class Gate:
    """One date's place in the order (``seq``: the number it was submitted under). ``dayrun.eod_run`` asks it for the
    carried spreads (``carried``), waits for its turn before it publishes (``before_publish``) and says it is done
    (``finish``). The turn is when ``seq`` dates before it are finished: published, failed or left."""

    def __init__(self, shared: Shared, seq: int, wait_carried: bool = True) -> None:
        self.shared, self.seq, self.wait_carried = shared, seq, wait_carried
        self._turn = self._finished = False

    def wait_turn(self) -> None:
        if self._turn:
            return
        s = self.shared
        with s.cond:
            while s.done.value < self.seq:
                if s.abort.value:
                    raise Aborted(f"date {self.seq} of the pipeline: closed before its turn")
                s.cond.wait(POLL_S)
        self._turn = True

    def carried(self, con: Any, run_date: Any) -> LazyCarried:
        def load() -> dict[str, Any]:
            if self.wait_carried:
                self.wait_turn()
            return store.carried_spreads(con, before=run_date)

        return LazyCarried(load)

    def before_publish(self) -> None:
        self.wait_turn()

    def finish(self) -> None:
        """This date is done, whatever came of it: the next one's turn. A date that failed first waits for its own
        turn, so that the dates finish in the order."""
        if self._finished:
            return
        try:
            self.wait_turn()
        except Aborted:
            return
        s = self.shared
        with s.cond:
            s.done.value = max(s.done.value, self.seq + 1)
            s.cond.notify_all()
        self._finished = True


# ---- the processes of the pool

_SHARED: Shared | None = None
_CONTEXTS: Any = None


def _init(shared: Shared, init: tuple[Callable[..., None], tuple] | None) -> None:
    """A process of the pool starts: the shared state, a cache of prepared markets of its own."""
    global _SHARED, _CONTEXTS
    from .dayrun import ContextCache

    _SHARED, _CONTEXTS = shared, ContextCache()
    if init is not None:  # a test's hook (a function of an importable module)
        init[0](*init[1])


def _portable(e: BaseException) -> BaseException:
    """``e`` as it can be sent back: itself when it survives a pickle round trip, else a ``RuntimeError`` with its text
    (the class and the message: the caller reports it as the date's own error)."""
    try:
        pickle.loads(pickle.dumps(e))
        return e
    except Exception:  # noqa: BLE001 - whatever makes it unpicklable
        return RuntimeError(f"{type(e).__module__}.{type(e).__name__}: {e}")


def run_date(spec: dict[str, Any]) -> dict[str, Any]:
    """One date's run in a process of the pool (``dayrun.eod_run`` with its gate). ``spec``: ``settings``, ``date``,
    ``seq``, ``wait_carried``, ``snapshots``, ``store_url``, ``no_gamma``. Returns {rid, lines, seconds} or {error,
    lines, trace}: the lines the run printed, in order."""
    from . import dayrun

    gate = Gate(_SHARED, spec["seq"], spec["wait_carried"])
    lines: list[str] = []
    t0 = time.monotonic()
    try:
        rid = dayrun.eod_run(spec["settings"], spec["date"], snapshots=spec["snapshots"], store_url=spec["store_url"],
                             report_dir=None, out=lines.append, contexts=_CONTEXTS, read_model=False,
                             no_gamma=spec["no_gamma"], gate=gate)
        return {"rid": rid, "lines": lines, "seconds": round(time.monotonic() - t0, 1)}
    except Aborted as e:
        return {"error": e, "lines": lines, "trace": "", "aborted": True}
    except Exception as e:  # noqa: BLE001 - the date's error goes back to the caller, who classifies it
        return {"error": _portable(e), "lines": lines, "trace": traceback.format_exc()}
    finally:
        gate.finish()


class Pipeline:
    """The dates of one backfill pass in a pool of ``workers`` processes (module doc). ``submit(date)`` in the order of
    the dates, ``result(date)`` in the same order; ``close()`` when the pass is over, however it ends."""

    def __init__(self, settings: Settings, workers: int, *, store_url: str, snapshots: str, no_gamma: bool,
                 wait_carried: bool, init: tuple[Callable[..., None], tuple] | None = None) -> None:
        self.settings, self.workers = settings, workers
        self.store_url, self.snapshots, self.no_gamma, self.wait_carried = store_url, str(snapshots), no_gamma, wait_carried
        self._futures: dict[str, Future] = {}
        self._seq = 0
        self.broken = False
        # runs still loading before the pool starts are the orphans of an earlier crash; in the pool no process reaps
        with store.connect(store_url) as con:
            store.reap_orphans(con)
        ctx = multiprocessing.get_context("spawn")
        self.shared = Shared(ctx)
        self._pool = ProcessPoolExecutor(max_workers=workers, mp_context=ctx, initializer=_init,
                                         initargs=(self.shared, init))

    def has(self, date: str) -> bool:
        return date in self._futures

    @property
    def in_flight(self) -> int:
        return sum(1 for f in self._futures.values() if not f.done())

    @property
    def pending(self) -> int:
        """Dates submitted and not yet taken (``result`` / ``discard``)."""
        return len(self._futures)

    def submit(self, date: str) -> None:
        spec = {"settings": self.settings, "date": date, "seq": self._seq, "wait_carried": self.wait_carried,
                "snapshots": self.snapshots, "store_url": self.store_url, "no_gamma": self.no_gamma}
        self._seq += 1
        self._futures[date] = self._pool.submit(run_date, spec)

    def result(self, date: str, out: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
        """The date's answer ({rid, lines, seconds}), its lines first given to ``out``; raises the date's own error
        (the original exception when it could be sent back) and ``PoolBroken`` when a process died."""
        fut = self._futures.pop(date)
        try:
            ans = fut.result()
        except BrokenProcessPool as e:
            self.broken = True
            self.close()
            raise PoolBroken(str(e)) from e
        for line in ans["lines"]:
            out(line)
        if "error" in ans:
            if ans.get("aborted"):
                raise PoolBroken("the pipeline was closed")
            if ans.get("trace"):
                log.debug("date %s failed in its process:\n%s", date, ans["trace"])
            raise ans["error"]
        return ans

    def discard(self, date: str) -> None:
        """A date submitted and no longer wanted (the run passed it over): its answer is dropped."""
        fut = self._futures.pop(date, None)
        if fut is not None:
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001 - nobody's date any more
                log.debug("date %s dropped: %s", date, e)

    def close(self) -> None:
        """Close the pass: the dates not yet in their turn do not publish (a date already publishing finishes); the
        processes end."""
        s = self.shared
        with s.cond:
            s.abort.value = 1
            s.cond.notify_all()
        self._pool.shutdown(wait=True, cancel_futures=True)
        self._futures.clear()
