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

**Nothing here can wait for ever.** The order is kept with shared counters that the dates *poll* (``POLL_S``), never with
a lock or a condition: a process killed while the others wait for their turn (an out-of-memory kill) leaves nothing
held, and the pipeline is closed by two stores into shared memory (the abort flag, the done counter), which no dead
process can block. The starting process waits for an answer at most ``RESULT_TIMEOUT_S`` for one date and for the
processes to end at most ``CLOSE_GRACE_S`` (then they are terminated); a date waiting for its turn gives up when the
starting process is gone or after ``TURN_TIMEOUT_S``. Each of these ends as a pool that is broken, which the caller runs
one after the other.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import pickle
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from multiprocessing.connection import wait as mp_wait
from typing import Any

from . import store
from .settings import Settings

log = logging.getLogger("desk.parallel")

LOOKAHEAD = 1
"""Dates in flight beyond the number of processes: a process finishing a date has the next one waiting."""
POLL_S = 0.1
"""How often a date waiting for its turn looks at the shared counters (the turns are tens of seconds apart)."""
RESULT_TIMEOUT_S = 1800.0
"""The longest the starting process waits for the answer of the date it takes next (the oldest of those in flight: its
own calculation and publication, a date takes 10 to 60 s); then the pool is broken and the date runs here."""
CLOSE_GRACE_S = 120.0
"""How long ``close`` lets the processes finish a date that is publishing (the others stop at the abort flag) before
they are terminated."""
TURN_TIMEOUT_S = 7200.0
"""A date waiting for its turn gives up after this long (the dates before it did not finish: a stuck process)."""
SKIP_CAPACITY = 16384
"""Dates of one pass that ``Pipeline.discard`` can still stop (the date's number is its place in the shared flags)."""
WORKER_NICE = 5


class Aborted(RuntimeError):
    """The pipeline is closed (the run stopped, or a process died): a date waiting for its turn does not publish."""


class Skipped(RuntimeError):
    """The date was dropped by the starting process (``Pipeline.discard``) before it was published: it does not publish."""


class PoolBroken(RuntimeError):
    """A process of the pool died or did not answer; the dates not published are the caller's to run one after the
    other."""


class Shared:
    """What the starting process creates and every process of the pool is given (``initargs``): the number of dates
    published (in the order they were submitted), the flag that closes the pipeline and a flag per date that drops it.
    Plain shared memory, no lock and no condition: a process that dies holds nothing the others wait on."""

    def __init__(self, mp_context: Any) -> None:
        self.done = mp_context.Value("q", 0, lock=False)
        self.abort = mp_context.Value("b", 0, lock=False)
        self.skip = mp_context.Array("b", SKIP_CAPACITY, lock=False)
        self.used = 0  # the starting process's: how many flags of ``skip`` a pass set (``reset`` clears those)

    def reset(self) -> None:
        """A new pass on the same processes (nothing of the earlier one is in flight)."""
        self.done.value = 0
        self.abort.value = 0
        for i in range(self.used):
            self.skip[i] = 0
        self.used = 0


class LazyCarried(Mapping):
    """The carried spreads of a date (``store.carried_spreads``), read at the first lookup, once. Behaves as the dict
    ``dayrun.eod_run`` reads at its start when it runs alone: a lookup of a bond, its length, iteration.

    When the read fails (the store, ``Aborted`` or ``Skipped`` from the gate) the error is remembered and every later
    lookup raises it again: the run that reads them at its start fails with it, and so does this one, wherever the first
    lookup happened (``orchestrator.value`` lets ``error`` through instead of valuing the bond at par)."""

    def __init__(self, load: Callable[[], dict[str, Any]]) -> None:
        self._load, self._data, self._error = load, None, None

    @property
    def loaded(self) -> bool:
        return self._data is not None

    @property
    def error(self) -> BaseException | None:
        """The error of the read, None while it did not fail."""
        return self._error

    def _get(self) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        if self._data is None:
            try:
                self._data = self._load()
            except BaseException as e:  # the same error for every later lookup
                self._error = e
                raise
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

    def skipped(self) -> bool:
        """The starting process dropped this date (``Pipeline.discard``): it is not to publish."""
        return self.seq < SKIP_CAPACITY and bool(self.shared.skip[self.seq])

    def wait_turn(self, *, honour_skip: bool = True) -> None:
        """Poll until the dates before this one are finished. ``Aborted`` when the pipeline is closed, the starting
        process is gone or ``TURN_TIMEOUT_S`` passed; ``Skipped`` when the date was dropped."""
        if self._turn:
            return
        s = self.shared
        parent = multiprocessing.parent_process()  # None in the starting process itself (the tests' threads)
        t0 = time.monotonic()
        while s.done.value < self.seq:
            if s.abort.value:
                raise Aborted(f"date {self.seq} of the pipeline: closed before its turn")
            if honour_skip and self.skipped():
                raise Skipped(f"date {self.seq} of the pipeline: dropped before its turn")
            if parent is not None and not parent.is_alive():
                raise Aborted(f"date {self.seq} of the pipeline: the process that started it is gone")
            if time.monotonic() - t0 > TURN_TIMEOUT_S:
                raise Aborted(f"date {self.seq} of the pipeline: its turn did not come in {TURN_TIMEOUT_S:.0f} s "
                              f"(done {s.done.value})")
            time.sleep(POLL_S)
        self._turn = True

    def carried(self, con: Any, run_date: Any) -> LazyCarried:
        def load() -> dict[str, Any]:
            if self.wait_carried:
                self.wait_turn()
            return store.carried_spreads(con, before=run_date)

        return LazyCarried(load)

    def before_publish(self) -> None:
        if self.skipped():
            raise Skipped(f"date {self.seq} of the pipeline: dropped before it published")
        self.wait_turn()

    def finish(self) -> None:
        """This date is done, whatever came of it: the next one's turn. A date that failed (or was dropped) first waits
        for its own turn, so that the dates finish in the order."""
        if self._finished:
            return
        try:
            self.wait_turn(honour_skip=False)
        except Aborted:
            return
        s = self.shared
        s.done.value = max(s.done.value, self.seq + 1)  # only the date whose turn it is writes
        self._finished = True


# ---- the processes of the pool

_SHARED: Shared | None = None
_CONTEXTS: Any = None


def _lower_priority() -> None:
    """The pool's processes work below the owner's programs (the desk's PC stays responsive): below normal on Windows,
    a few steps nicer elsewhere. Not fatal when it does not work."""
    try:
        if sys.platform == "win32":
            import ctypes

            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            k32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(WORKER_NICE)
    except Exception as e:  # noqa: BLE001 - priority is a courtesy
        log.debug("the pool's process keeps its priority: %s: %s", type(e).__name__, e)


def peak_memory_mb() -> float | None:
    """The most this process has held in memory (resident set / working set), MB; None where it cannot be told."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            c = Counters()
            c.cb = ctypes.sizeof(c)
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.GetCurrentProcess.restype = ctypes.c_void_p
            psapi = ctypes.windll.psapi  # type: ignore[attr-defined]
            psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), wintypes.DWORD]
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb):
                return None
            return round(c.PeakWorkingSetSize / 1_048_576, 1)
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KB on Linux, bytes on macOS
        return round(peak / (1_048_576 if sys.platform == "darwin" else 1024), 1)
    except Exception:  # noqa: BLE001 - a figure for the log
        return None


def _init(shared: Shared, init: tuple[Callable[..., None], tuple] | None) -> None:
    """A process of the pool starts: its priority, the shared state, a cache of prepared markets of its own."""
    global _SHARED, _CONTEXTS
    from .dayrun import ContextCache

    _lower_priority()
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
    ``seq``, ``wait_carried``, ``snapshots``, ``store_url``, ``no_gamma``. Returns {rid, lines, seconds, peak_mb} or
    {error, lines, trace}: the lines the run printed, in order."""
    from . import dayrun

    gate = Gate(_SHARED, spec["seq"], spec["wait_carried"])
    lines: list[str] = []
    t0 = time.monotonic()
    try:
        if gate.skipped():  # dropped while it was waiting for a process: nothing is computed
            raise Skipped(f"date {spec['seq']} of the pipeline: dropped before it started")
        rid = dayrun.eod_run(spec["settings"], spec["date"], snapshots=spec["snapshots"], store_url=spec["store_url"],
                             report_dir=None, out=lines.append, contexts=_CONTEXTS, read_model=False,
                             no_gamma=spec["no_gamma"], gate=gate)
        return {"rid": rid, "lines": lines, "seconds": round(time.monotonic() - t0, 1), "peak_mb": peak_memory_mb()}
    except Aborted as e:
        return {"error": e, "lines": lines, "trace": "", "aborted": True}
    except Skipped as e:
        return {"error": e, "lines": lines, "trace": "", "skipped": True}
    except Exception as e:  # noqa: BLE001 - the date's error goes back to the caller, who classifies it
        return {"error": _portable(e), "lines": lines, "trace": traceback.format_exc()}
    finally:
        gate.finish()


def _stop(executor: ProcessPoolExecutor, *, kill: bool) -> None:
    """End the processes of ``executor``: they get the end of work, and after ``CLOSE_GRACE_S`` (at once with
    ``kill``) they are terminated, so that this returns in bounded time whatever the processes do. Whether a process
    lives is read from its sentinel, which does not reap it (the executor's own thread joins them: ``Process.is_alive``
    from here would race with it)."""
    procs = list((getattr(executor, "_processes", None) or {}).values())  # private, but the only handle on them

    def alive() -> list[Any]:
        return [p for p in procs if not mp_wait([p.sentinel], 0)]

    def end(by: float) -> None:
        while (left := alive()) and time.monotonic() < by:
            mp_wait([p.sentinel for p in left], max(0.0, min(0.25, by - time.monotonic())))

    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception as e:  # noqa: BLE001 - a pool already broken
        log.debug("pool shutdown: %s: %s", type(e).__name__, e)
    if not kill:
        end(time.monotonic() + CLOSE_GRACE_S)
    for p in alive():
        try:
            p.terminate()
        except Exception as e:  # noqa: BLE001 - gone meanwhile
            log.debug("pool process %s: %s: %s", p.pid, type(e).__name__, e)
    end(time.monotonic() + 10.0)
    for p in alive():
        log.warning("pool process %s did not end when terminated: killed", p.pid)
        try:
            p.kill()
        except Exception as e:  # noqa: BLE001 - gone meanwhile
            log.debug("pool process %s: %s: %s", p.pid, type(e).__name__, e)


class Pool:
    """The processes of the pool and what they share. A ``Pipeline`` (one pass of dates) borrows it; the caller that
    makes several passes in a row (the autopilot's cycle: the first pass and the second, ``desk backfill``'s batches)
    makes one ``Pool`` for them all, so the processes are started once and keep their prepared markets. Started on the
    first pass, and again after a pass that broke it; ``close()`` ends it."""

    def __init__(self, workers: int, init: tuple[Callable[..., None], tuple] | None = None) -> None:
        self.workers, self.init = workers, init
        self.executor: ProcessPoolExecutor | None = None
        self.shared: Shared | None = None

    def acquire(self) -> tuple[ProcessPoolExecutor, Shared]:
        """The processes for a new pass, the shared counters at zero (the caller says nothing of an earlier pass is in
        flight). Raises what the start raises (no semaphores, no process): the caller runs one after the other."""
        if self.executor is None or self.shared is None:
            ctx = multiprocessing.get_context("spawn")
            shared = Shared(ctx)
            self.executor = ProcessPoolExecutor(max_workers=self.workers, mp_context=ctx, initializer=_init,
                                                initargs=(shared, self.init))
            self.shared = shared
        else:
            self.shared.reset()
        return self.executor, self.shared

    def stop(self, *, kill: bool = False) -> None:
        """End the processes (the next pass starts new ones)."""
        ex, self.executor, self.shared = self.executor, None, None
        if ex is not None:
            _stop(ex, kill=kill)

    close = stop


class Pipeline:
    """The dates of one backfill pass in a pool of ``workers`` processes (module doc). ``submit(date)`` in the order of
    the dates, ``result(date)`` in the same order; ``close()`` when the pass is over, however it ends. ``pool``: the
    caller's ``Pool`` that serves several passes (it is not ended here, unless this pass broke it); none: the pipeline
    starts its own and ends it."""

    def __init__(self, settings: Settings, workers: int, *, store_url: str, snapshots: str, no_gamma: bool,
                 wait_carried: bool, init: tuple[Callable[..., None], tuple] | None = None, pool: Pool | None = None,
                 timeout_s: float | None = None) -> None:
        self.settings, self.workers = settings, workers
        self.store_url, self.snapshots, self.no_gamma, self.wait_carried = store_url, str(snapshots), no_gamma, wait_carried
        self.timeout_s = RESULT_TIMEOUT_S if timeout_s is None else timeout_s
        self._futures: dict[str, Future] = {}
        self._seqs: dict[str, int] = {}
        self._dropped: list[Future] = []
        self._seq = 0
        self._closed = False
        self.broken = False
        self.peak_mb = 0.0
        # runs still loading before the pool starts are the orphans of an earlier crash; in the pool no process reaps
        with store.connect(store_url) as con:
            store.reap_orphans(con)
        self._owns = pool is None
        self.pool = pool if pool is not None else Pool(workers, init)
        try:
            self._executor, self.shared = self.pool.acquire()
        except BaseException:
            self.pool.stop(kill=True)
            raise

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
        if self._closed or self.broken:
            raise PoolBroken("the pipeline is closed or broken")
        spec = {"settings": self.settings, "date": date, "seq": self._seq, "wait_carried": self.wait_carried,
                "snapshots": self.snapshots, "store_url": self.store_url, "no_gamma": self.no_gamma}
        try:
            self._futures[date] = self._executor.submit(run_date, spec)
        except Exception:
            self.broken = True
            raise
        self._seqs[date] = self._seq
        self._seq += 1
        self.shared.used = min(self._seq, SKIP_CAPACITY)

    def _break(self, why: str) -> PoolBroken:
        """The pool is of no use any more: its processes are ended, the dates not published are the caller's."""
        self.broken = True
        log.warning("the processes of the pool are stopped: %s", why)
        self.close(kill=True)
        return PoolBroken(why)

    def result(self, date: str, out: Callable[[str], None] = lambda _: None) -> dict[str, Any]:
        """The date's answer ({rid, lines, seconds}), its lines first given to ``out``; raises the date's own error
        (the original exception when it could be sent back) and ``PoolBroken`` when a process died, did not answer in
        ``timeout_s`` or the pipeline was closed."""
        fut = self._futures.pop(date)
        self._seqs.pop(date, None)
        try:
            ans = fut.result(timeout=self.timeout_s)
        except BrokenProcessPool as e:
            raise self._break(f"a process of the pool died ({e})") from e
        except TimeoutError as e:
            raise self._break(f"no answer for {date} from the pool in {self.timeout_s:.0f} s") from e
        for line in ans["lines"]:
            out(line)
        if ans.get("peak_mb"):
            self.peak_mb = max(self.peak_mb, ans["peak_mb"])
        if "error" in ans:
            if ans.get("aborted") or ans.get("skipped"):
                raise PoolBroken("the pipeline was closed")
            if ans.get("trace"):
                log.debug("date %s failed in its process:\n%s", date, ans["trace"])
            raise ans["error"]
        return ans

    def discard(self, date: str) -> None:
        """A date submitted and no longer wanted (the run passed it over, as the sequential run passes over a date it
        does not run): its answer is dropped, and it is not published. A date that has not started is not computed; one
        that is computing is not published (its process finds the flag at its turn). Does not wait."""
        fut = self._futures.pop(date, None)
        seq = self._seqs.pop(date, None)
        if fut is None:
            return
        if seq is not None and seq < SKIP_CAPACITY:
            self.shared.skip[seq] = 1
        self._dropped.append(fut)  # not cancelled: the date still has its place in the order (the gate counts it)

    def close(self, *, kill: bool = False) -> None:
        """Close the pass: the dates not yet in their turn do not publish (a date already publishing finishes, up to
        ``CLOSE_GRACE_S``), the processes end (a pool of the caller's stays, when it can: all finished in time).
        Does not hang: after the grace the processes are terminated."""
        if self._closed:
            return
        self._closed = True
        self.shared.abort.value = 1
        left = [*self._futures.values(), *self._dropped]
        clean = False
        if not kill:
            _, not_done = wait(left, timeout=CLOSE_GRACE_S)
            clean = not not_done
        self._futures.clear()
        self._dropped.clear()
        if self._owns or kill or not clean or self.broken:
            self.pool.stop(kill=kill or not clean)
        if self.peak_mb:
            log.info("pool: the most one process held in memory, %.0f MB", self.peak_mb)
