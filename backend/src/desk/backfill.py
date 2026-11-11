"""History of runs (R23): the default EOD run of every past business date with positions, so that the risk, MV and
PnL series, PnL month and year to date, and the history pages reach back as far as the reports and the hub's
``eod_final`` closes do (the hub has them from 2026-01-05).

Why the runs reached only about two weeks back: the autopilot's eod step keeps the runs current for
``WINDOW_DAYS`` (14 calendar days, at most ``MAX_EOD_DATES`` dates); its sync pulls the hub's bundles only for the
dates with positions in that window; the inbox reads reports at most ``inbox_lookback_days`` (35) old; and an
upgrade recomputes only the latest date. Nothing ever computed an older date.

``desk backfill [--from D] [--to D] [--dry-run]`` and the autopilot's ``backfill`` step (after every other step,
``backfill_budget_s`` seconds a cycle, so the latest date is never held up) do it in three parts:

1. **reports** (``inbox``): the inbox scan once more with its lookback starting at ``--from`` (``inbox.scan.scan``,
   ``since``): the reports older than the lookback, which the regular scans leave out (``ignored_too_old``), are read
   into position sets of their dates. The autopilot does this once per start date and adapter (``STATE_FILE``).
2. **plan**: the business dates from ``--from`` to ``--to`` the hub (else the cache) has an ``eod_final`` close for.
   A date's positions are ``store.positions_for`` of the date: per book the newest set valid on or before it, so a
   book whose report did not come that day is carried forward from its last report, as every run does; a date
   before the first report of every book has no positions and no run. A date needs a run when it has none, or its
   default run read other position sets or canonical private inputs, another market snapshot (a hub correction),
   another previous close (``bundle.previous_close``: the newest close of the hub and the cache together, the one
   the run picks once its bundle is synced, #203), read its inputs before an ``ocp`` report, ``cash_balances`` or a revision of the OCP policy in
   force on the date were recorded (``late_why``, TDM#181, TDM#567), or was
   made by another valuation or core version (``same_logic``: ``VALUATION_VERSION`` and the core version of
   ``logic_version()`` recompute the history; a new release or Windows bundle of the same computation does not). Otherwise it is skipped: nothing
   is computed twice.
3. **runs**, oldest first: the date's bundle and the previous date's (flash PnL) from the hub when not cached, or
   cached under another snapshot than the hub's of the plan (a correction of an older date, #439) (``cache.sync``), then ``dayrun.eod_run``, the code path of every EOD run. The prepared market of a date is kept for
   the next one (its previous date: ``dayrun.ContextCache``), so each date's static snapshot and curves are built once.
   A model price carries its bond's spread from the latest earlier run (``store.carried_spreads``), so a date's run
   depends on the runs before it. One date at a time, or, with ``workers`` > 1 (setting ``backfill_workers``, TDM#628),
   several in processes of a pool (``desk.parallel``) that keep that dependence: the spreads are read when the
   valuation first looks one up, once the dates before are published, and the runs are published in the order of the
   dates, so the runs are those of one process, to the last bit. A date that fails is not tried again on the same inputs (``STATE_FILE``). A run that created
   nothing is the date's failure too. After its run a date is planned again (the same ``plan``): still stale (a
   reason that does not go away, e.g. a hub date whose bundle does not come), it is logged with
   the differing field and tried again next time; after ``MAX_ATTEMPTS`` runs it is a problem date, not tried again
   on the same inputs (#23). A saved failed signature from before private input hashes were included is stale
   once: after one retry the new signature suppresses unchanged failures again.
   An error of the store or the network stops the run (the next dates would hit it too)
   and is not remembered as the date's failure: the backfill stalls (``STATE_FILE`` ``stall``: the error, since when,
   the last attempt) and the dates are not problem dates (#447: a network down for hours added one «does not
   converge» date a cycle). A date that hits one ``MAX_ATTEMPTS`` runs in a row is passed over once: when a later date
   of the same run gets past the store and the network, the error is the date's own (``alone``: the date «does not
   load», #23, #217) and it no longer holds the queue, tried again every time; when the later date hits one too, it
   is the network, and the run stops there (at most two dates a cycle hit it, #208). The date the last run stopped
   on after it (``stall["probe"]``) is passed over too while it stalls, by the same rule (``MAX_ATTEMPTS`` runs
   stopped on it in a row) and when a later date is left: the next date tells, so two neighbouring dates that fail
   each on its own do not hold the queue for ever; the dates run oldest first but for these two. When the stall ends, the date that waited is not taken for its
   own trouble yet (``recheck``): only when it fails again on its own after ``MAX_ATTEMPTS`` runs (#447).
   A date run from the cache says nothing of the downloads (``phase``: ``bundle``, the download, or ``run``): it
   ends no stall of the downloads, nor tells a date that failed to download from the hub. A date passed over on its
   download with no later date to download is told by the hub's other answers of the cycle (``hub_answered``: the
   autopilot's sync step read the hub this cycle): the hub answers, the bundle does not come, it is the date's own
   («does not load», as #217); without them (``desk backfill``, the hub off) it stays a stall. The autopilot waits
   ``STALL_BACKOFF_S`` between its runs on a stall (none at its start, in a cycle the owner asked for, nor when the
   store is unreachable); a stall whose dates are no longer to run, or whose last error is older than
   ``STALL_STALE``, is forgotten.

**Two passes** (TDM#627). The second derivatives (the Hessian: the positions' ``gamma``, the bonds' ``convexity``) are
2.5 times the work of a date (42 s against 17 s with 2000 positions), and only the Taylor estimate of the scenario
screen reads them (the PnL explain reprices in full, the monitors read the first derivatives). So step 3 is two passes
over the window: the first makes the runs without them (``eod_run(no_gamma=True)``: the run's lineage says
``second_order`` false, its log says so, its ``gamma`` is empty, not zero, ``RunHeader.second_order`` and
``RiskPayload.second_order`` tell the screens), so the whole window has its value, risk and PnL as soon as possible;
when the first pass has gone through every date (``run``: ``complete``) the second (``run_second``) makes each date
again in full, the newest first, from the cache: the same inputs give the same value, first derivatives and PnL
to the last bit, and the new run supersedes the first pass's with the gamma added. Both passes are cut by the same
budget and resume where they stopped: a date's state is its default run (``Day.second``). The current date (the
autopilot's eod and intraday steps) and every other run are full. ``status``: ``pass`` (``first``, ``second``, ``done``)
and ``second`` (the dates ``done``, ``todo``, ``failed``, ``next``, ``problem_dates``, ``eta_s``, ``mismatched`` and
``mismatch_dates``). The second pass checks the new run's totals per book against the replaced run's and warns of a
difference; a date it cannot make current is given up under its inputs, not under a run (``run_second``).

The bundles of the backfilled dates stay in the snapshot cache (each date's market and static snapshot, several MB a
date: ``--dry-run`` says how many are to be downloaded).

``status(con, settings)`` (the settings page, ``/api/v1/backfill/status``, the header's indicator, the diagnostics):
the range, how many dates have their run, are still to run, have no positions, failed; the oldest default run; the
problem dates with their kind (``failed``, ``unconverged``: do not converge; ``unreachable``: do not load), reason,
error and attempts (``problem_dates``); the stall (``stalled``: the store's or the network's error the backfill
stopped on, what is down (``cause``, ``stall_cause``), since when, the next attempt of the autopilot; None when it goes
on) and the estimated time left (``eta_s``).
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import logging
import re
import socket
import ssl
import threading
import time
import urllib.error
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import psycopg

from . import logic_version, store, trades
from .marketdata import cache
from .marketdata.bundle import SnapshotDir, previous_close
from .marketdata.hub import HubError, TransportError
from .settings import Settings
from .valuation.fallback import SPREAD_EPS_BP

log = logging.getLogger("desk.backfill")

DEFAULT_FROM = date(2026, 1, 5)
"""The first ``eod_final`` close on the hub: the default start (setting ``backfill_from``)."""
STATE_FILE = "backfill.json"
MAX_ATTEMPTS = 2
"""Runs of a date still stale after its run before it is a problem date (#23); runs stopped on a date by the store or
the network before the run passes over it to tell its own error from the network's (#447)."""
STALL_BACKOFF_S = (600, 1800, 3600)
"""The autopilot's waits before it runs the backfill again while it stalls (#447), by the runs stopped on the stall,
the last one onwards: 10 min, 30 min, then hourly (``desk backfill`` does not wait). The store unreachable
(``cause`` ``store``) is not waited for: the plan of the next cycle reads the store first, so a run gets as far only
once it answers. A slow store (``store_slow``, a query cancelled by the statement timeout) is waited for: the same
heavy query again at once would time out again."""
STALL_STALE = timedelta(days=1)
"""A stall whose last error is older than this is forgotten: the next error starts a new one (its since, its waits)."""


@dataclass
class Day:
    date: str
    status: str
    """``done`` (its default run is current), ``todo``, ``no_positions``, ``no_market`` (no eod_final close on the
    hub or in the cache), ``failed`` (failed before on the same inputs)."""
    why: str | None = None
    sig: list = field(default_factory=list)
    """The inputs the plan was made on (position sets, market, previous close, logic, private inputs, late sets): a
    failed date is remembered under it; a ``done`` date has it too (the second pass's give-up is remembered under it)."""
    market_id: str | None = None
    cached: bool = False
    prev_date: str | None = None
    """The close before the date (``bundle.previous_close``, the run's too, #203): the flash PnL's basis."""
    prev_cut: str | None = None
    """Its cut: ``eod_final``, else a cached ``eod_preliminary`` (not synced from the hub)."""
    second: str | None = None
    """The second pass (TDM#627) of a ``done`` date, else None: ``done`` (its run has the second derivatives), ``todo``
    (its run was made without them: the first pass), ``failed`` (the second pass gave up on that run: ``MAX_ATTEMPTS``
    errors)."""
    run_id: int | None = None
    """The date's default run (a ``done`` date)."""
    carry: str | None = None
    """What the plan knows of the spreads the date's model prices carried (TDM#660), for a ``done`` date: ``unknown``
    (its run was made before the run recorded where they came from, and says it carried some: not told stale, not told
    current), else None."""
    chain: bool = False
    """A ``todo`` date listed with ``CARRY_WHY`` only because a date it carried from is to run (the chain, TDM#660), not
    because its own run holds a spread its source holds otherwise now: ``run`` asks the store again before it makes such
    a date, once the dates before it are done, and leaves it when its source came out with the same spread."""


@dataclass
class Plan:
    date_from: str
    date_to: str
    days: list[Day]
    hub: bool
    """True: the dates are the hub's; False: the cache's only."""
    rows: list[tuple[str, str]] | None = None
    """The hub's rows the plan was made on (``hub_rows``): a date is planned again on them after its run."""

    def count(self, status: str) -> int:
        return sum(d.status == status for d in self.days)

    @property
    def todo(self) -> list[Day]:
        return [d for d in self.days if d.status == "todo"]

    @property
    def second_todo(self) -> list[Day]:
        """The dates whose default run lacks the second derivatives (``Day.second``), the newest first: the order of
        the second pass."""
        return [d for d in reversed(self.days) if d.second == "todo"]

    def count_second(self, status: str) -> int:
        return sum(d.second == status for d in self.days)

    def to_downloads(self) -> int:
        return sum(1 for d in self.todo if not d.cached)

    def summary(self) -> dict[str, Any]:
        by: dict[str, int] = {}
        for d in self.todo:
            k = reason_key(d.why)
            by[k] = by.get(k, 0) + 1
        return {"from": self.date_from, "to": self.date_to, "dates": len(self.days), "done": self.count("done"),
                "todo": self.count("todo"), "no_positions": self.count("no_positions"),
                "no_market": self.count("no_market"), "failed": self.count("failed"), "hub": self.hub,
                "to_download": self.to_downloads(), "next": [d.date for d in self.todo[:5]],
                "carry_unknown": sum(d.carry == "unknown" for d in self.days),
                "second": {"done": self.count_second("done"), "todo": self.count_second("todo"),
                           "failed": self.count_second("failed"), "next": [d.date for d in self.second_todo[:5]]},
                "todo_by_reason": [{"key": k, "text": REASON_TEXT[k], "n": n}
                                   for k, n in sorted(by.items(), key=lambda kv: (-kv[1], kv[0]))]}


REASON_TEXT = {"program": "новая версия расчёта", "no_run": "ещё не считалась", "positions": "изменились позиции",
               "market": "хаб поправил срез", "previous": "хаб поправил прошлое закрытие",
               "private_inputs": "изменились сделки или условия сделок", "late": "поздний отчёт ОВП или остатков",
               "carry": "перенесённый спред устарел", "other": "другое"}
"""TDM#587: why a date is to run (``plan``'s ``why``) -> the words of «Осталось» on «История прогонов»."""


def reason_key(why: str | None) -> str:
    """The ``REASON_TEXT`` key of a date's ``why`` (``plan``)."""
    w = why or ""
    return ("no_run" if w == "no default run" else "positions" if w == "positions changed"
            else "market" if w == "market snapshot changed" else "previous" if w == "previous snapshot changed"
            else "private_inputs" if w == "private inputs changed" else "program" if w == "program updated"
            else "carry" if w == CARRY_WHY
            else "late" if w.endswith("changed after the run") else "other")


def state(settings: Settings) -> dict[str, Any]:
    try:
        return json.loads((Path(settings.home) / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(settings: Settings, st: dict[str, Any]) -> None:
    path = Path(settings.home) / STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.warning("backfill: state not saved: %s", e)


def start_date(settings: Settings, date_from: date | None = None) -> date:
    if date_from is not None:
        return date_from
    v = getattr(settings, "backfill_from", None)
    return date.fromisoformat(v) if isinstance(v, str) and v else (v if isinstance(v, date) else DEFAULT_FROM)


_HASH_MEMO: OrderedDict[tuple, str] = OrderedDict()
HASH_MEMO_SIZE = 4096
_HASH_LOCK = threading.Lock()   # the autopilot's thread and the API's threads share the memo


def inputs_stamp(con: psycopg.Connection, settings: Settings) -> tuple:
    """What decides every date's ``trades.intraday_inputs`` hash, cheaply (two aggregate queries): the database, the
    position sets, every stored dataset set (the trades of the date and of the 60 days before it, the trade terms), the
    settings that ``trades.intraday_inputs`` reads (``intraday_trades``, ``off_report_derivatives``: the latter puts
    the off-report trades into the hash), the revisions of the books reference and the reference tables (TDM#587). Sets are never changed, a new revision is a new row, so a changed count
    or newest id says that some date's inputs changed (TDM#569)."""
    pos = con.execute("SELECT max(set_id), count(*) FROM position_set").fetchone()
    ds = con.execute("SELECT max(set_id), count(*), (SELECT oid FROM pg_database WHERE datname = current_database())"
                     " FROM dataset_set").fetchone()
    # TDM#587: the books reference and the other reference tables (a book's type or entity read by the day's trades)
    refs = con.execute("SELECT (SELECT max(rev_id) FROM book_rev), (SELECT count(*) FROM book_rev),"
                       " (SELECT max(rev_id) FROM reference_rev), (SELECT count(*) FROM reference_rev)").fetchone()
    return (tuple(pos), tuple(ds), tuple(refs), bool(getattr(settings, "intraday_trades", True)),
            str(getattr(settings, "off_report_derivatives", "exclude")))


def inputs_hash(con: psycopg.Connection, d: date, settings: Settings, stamp: tuple | None = None) -> str:
    """``trades.intraday_inputs(...)`` ``private_inputs_hash`` of the date, remembered in this process (an LRU) under
    ``inputs_stamp``: the plan reads it for every date of the range at each poll of the status, and with the trades of
    hundreds of dates the work of ``apply_intraday`` (the day's trades, the start of the day, the folds) would be done
    again and again (TDM#569). A changed stamp is a different key: the work is done once for the new inputs."""
    key = (stamp if stamp is not None else inputs_stamp(con, settings), d.isoformat())
    with _HASH_LOCK:
        hit = _HASH_MEMO.get(key)
        if hit is not None:
            _HASH_MEMO.move_to_end(key)
            return hit
    h = trades.intraday_inputs(con, d, settings)[0].private_inputs_hash   # (not under the lock: it reads the store)
    with _HASH_LOCK:
        _HASH_MEMO[key] = h
        while len(_HASH_MEMO) > HASH_MEMO_SIZE:
            _HASH_MEMO.popitem(last=False)
    return h


def hub_rows(client: Any, date_from: date) -> list[tuple[str, str]]:
    """(business date, current eod_final snapshot id) of the hub from the date before ``date_from`` on, newest
    first (``cache.hub_latest``)."""
    out = []
    for d, sid in cache.hub_latest(client):
        out.append((d, sid))
        if d < date_from.isoformat():
            break
    return out


def logic_key(v: str | None) -> str | None:
    """What of a ``logic_version()`` value decides the numbers: ``v<VALUATION_VERSION>+core<core version>``, without
    the backend version and the bundle build (a release without changes to the computation, issue #24). The values
    stamped by older releases have the same parts, so they need no migration. A value without them is kept whole
    (less its build)."""
    if not v:
        return v
    v = v.split("+build", 1)[0]
    val = re.search(r"(?:^|\+)v(\d+)(?=\+|$)", v)
    core = re.search(r"\+core(.+)$", v)
    return f"v{val.group(1)}+core{core.group(1)}" if val and core else v


def same_logic(a: str | None, b: str | None) -> bool:
    """Two ``logic_version()`` values compute the same numbers: the same ``logic_key`` (VALUATION_VERSION and the
    core version)."""
    return logic_key(a) == logic_key(b)


LATE_WHY = {"ocp": "ocp report", "cash_balances": "cash balances", "ocp_policy": "ocp policy"}
"""``store.LATE_DATASETS`` and ``store.LATE_REFERENCE`` as a reason to run a date again (TDM#181, TDM#567)."""


def late_why(con: psycopg.Connection, d: date, run: dict | None, revs: store.ReferenceRevs | None = None) -> str | None:
    """"ocp report changed after the run" (and / or the cash balances) when an ``ocp`` or ``cash_balances`` set in
    force on ``d`` was recorded after the default run ``run`` read its inputs (``store.late_inputs``); else None."""
    late = store.late_inputs(con, d, run.get("positions_known_at"), revs) if run else []
    return " and ".join(LATE_WHY[x] for x in late) + " changed after the run" if late else None


CARRY_WHY = "carried spread stale"
"""``plan``'s ``why`` of a date whose run carried a spread that is not what its source date holds now (TDM#660)."""
def stale_spreads(con: psycopg.Connection, runs: dict[date, dict], date_from: date) -> dict[date, list[tuple[str, str, float, float | None]]]:
    """Per date of ``runs`` (``store.default_runs``) the spreads its run carried that its source date no longer holds:
    (secid, source date, the spread carried, the spread the source date's default run holds now, None: none). The
    source is a date, not a run: a run replaced by the same date's run of the second pass (TDM#627) or of the same
    calculation holds the same spread and is no change, while a run that was deleted (``store.prune_superseded``,
    TDM#634) with nothing in its place, or a date recalculated to another spread, is. A run made before the sources were
    recorded (``spread_sources`` None) has none to compare: it is not listed (``Day.carry`` says it is unknown)."""
    want = {d: r["spread_sources"] for d, r in runs.items() if r.get("spread_sources")}
    if not want:
        return {}
    sources = {date.fromisoformat(v["date"]) for srcs in want.values() for v in srcs.values()}
    by_date = {d: r["run_id"] for d, r in runs.items()}
    early = min(sources)
    if early < date_from:  # a source before the plan's first date: its default run is read apart (the dates up to it)
        by_date.update({d: r["run_id"] for d, r in store.default_runs(con, early, date_from - timedelta(days=1)).items()})
    pairs = {(by_date[date.fromisoformat(v["date"])], secid) for srcs in want.values() for secid, v in srcs.items()
             if date.fromisoformat(v["date"]) in by_date}
    now = store.spread_values(con, sorted(pairs)) if pairs else {}
    out: dict[date, list[tuple[str, str, float, float | None]]] = {}
    for d, srcs in want.items():
        for secid, v in sorted(srcs.items()):
            src = date.fromisoformat(v["date"])
            rid = by_date.get(src)
            held = now.get((rid, secid)) if rid is not None else None
            if held is None or abs(held - v["z_bp"]) > SPREAD_EPS_BP:
                out.setdefault(d, []).append((secid, v["date"], v["z_bp"], held))
    return out


def plan(con: psycopg.Connection, settings: Settings, date_from: date, date_to: date,
         hub: list[tuple[str, str]] | None = None) -> Plan:
    """What the backfill would do (module doc, step 2). ``hub``: the hub's (date, snapshot id) rows (``hub_rows``);
    None plans on the cache alone."""
    sd = SnapshotDir(Path(settings.snapshot_cache))
    lo, hi = date_from.isoformat(), date_to.isoformat()
    cached = set(sd.market_dates())
    hub_ids = dict(hub or [])
    dates = sorted({d for d in (hub_ids if hub is not None else cached) if lo <= d <= hi})
    runs = store.default_runs(con, date_from, date_to)  # (no later date is planned: none of them is read)
    stale = stale_spreads(con, runs, date_from)  # TDM#660: the carried spreads their source dates no longer hold
    todo_dates: set[date] = set()  # the dates of the plan that will be made again: a date that carried from one is stale
    logic = logic_version()
    st = state(settings)
    failed = st.get("failed", {})
    second_failed = (st.get("second") or {}).get("failed", {})
    first_pos = con.execute("SELECT min(valid_date) FROM position_set").fetchone()[0]
    stamp = inputs_stamp(con, settings)
    revs = store.ReferenceRevs(con)  # once for every date (TDM#567)
    days = []
    for d in dates:
        if first_pos is None or d < first_pos.isoformat():
            days.append(Day(d, "no_positions"))
            continue
        ids, n = store.current_sets(con, date.fromisoformat(d))
        if not n:
            days.append(Day(d, "no_positions"))
            continue
        is_cached = d in cached
        snap = hub_ids.get(d) or (sd.market_id(d) if is_cached else None)
        if snap is None:
            days.append(Day(d, "no_market"))
            continue
        pc = previous_close(sd, d, hub_ids)  # the run's previous close once the run synced it (#203)
        pd, pcut, prev = pc if pc else (None, None, None)
        run = runs.get(date.fromisoformat(d))
        ih = inputs_hash(con, date.fromisoformat(d), settings, stamp)  # TDM#557, #569: remembered
        late = late_why(con, date.fromisoformat(d), run, revs)
        why = ("no default run" if run is None else "positions changed" if run["position_set_ids"] != ids
               else "market snapshot changed" if run["market_snapshot_id"] != snap
               else "previous snapshot changed" if prev is not None and run["prev_market_snapshot_id"] != prev
               else "private inputs changed" if run["private_inputs_hash"] != ih
               else late
               or ("program updated" if not same_logic(run.get("logic_version"), logic) else None))
        carry, chain = None, False
        if why is None:  # the run is current by its inputs; are the spreads its model prices carried?
            srcs = run["spread_sources"]
            if srcs is None:  # a run from before the sources were recorded: unknown, never told stale for that
                carry = "unknown" if (run.get("spreads_carried") or 0) > 0 else None
            elif date.fromisoformat(d) in stale:
                why = CARRY_WHY  # its source date holds another spread now
            elif any(date.fromisoformat(v["date"]) in todo_dates for v in srcs.values()):
                why, chain = CARRY_WHY, True  # its source date is made again, so its spread may be another: ``run`` looks
        sig = [ids, snap, prev, logic, ih, store.dataset_set_ids(con, date.fromisoformat(d)),
               store.reference_rev_ids(con, revs=revs)]
        if why is None:
            giveup = second_failed.get(d) or {}  # the give-up is of the date's inputs, not of a run (TDM#627)
            second = ("done" if run["second_order"] else
                      "failed" if giveup.get("sig") == sig and giveup.get("attempts", 0) >= MAX_ATTEMPTS
                      else "todo")
            days.append(Day(d, "done", sig=sig, market_id=snap, cached=is_cached, prev_date=pd, prev_cut=pcut,
                            second=second, run_id=run["run_id"], carry=carry))
        elif failed.get(d) == sig:
            days.append(Day(d, "failed", why, sig, snap, is_cached, pd, pcut))
        else:
            days.append(Day(d, "todo", why, sig, snap, is_cached, pd, pcut, chain=chain))
            todo_dates.add(date.fromisoformat(d))
    return Plan(lo, hi, days, hub is not None, hub)


def inbox(con: psycopg.Connection, settings: Settings, date_from: date, *, force: bool = False,
          progress: Callable[[int, int | None, str], None] | None = None) -> str | None:
    """Step 1 (module doc): the reports from ``date_from`` on, once per start date and adapter fingerprint unless
    ``force``. Returns the scan's summary line, None when skipped (done before, or no inbox folders)."""
    from .inbox import scan

    if not settings.scan_dirs:
        return None
    h = scan.adapter(settings)
    st = state(settings)
    mark = {"from": date_from.isoformat(), "fingerprint": h.fingerprint}
    if not force and st.get("inbox") == mark:
        return None
    res = scan.scan(con, settings, since=date_from, progress=progress)
    if res.busy:
        return "inbox: another scan is running; next time"
    if res.ok:
        st = state(settings)
        st["inbox"] = mark
        save_state(settings, st)
    return "inbox from " + date_from.isoformat() + ": " + "; ".join(res.text().splitlines())


def _disk_full(e: BaseException) -> bool:
    """No space left (``ENOSPC``) or over the quota (``EDQUOT``, where the platform has it): the host's trouble, which
    ends when the disk is freed, not the date's data."""
    return isinstance(e, OSError) and e.errno in {getattr(errno, n) for n in ("ENOSPC", "EDQUOT") if hasattr(errno, n)}


def transient(e: BaseException) -> bool:
    """An error of the store connection, the network or the hub as a whole (unreachable, 429, 5xx, token or proxy
    refused), not of the date's data: such a date is not marked failed (a failed date is not tried again on the
    same inputs), and the run stops there, since the next dates would hit it too (``run``: the stall, #447).
    Network: ``TransportError`` (the hub client's, urllib or curl), ``TimeoutError``, ``ConnectionError``, DNS, TLS
    (``ssl.SSLError``) and urllib/http.client errors. Store: ``psycopg.OperationalError``.
    A full disk or an exceeded quota (``ENOSPC``, ``EDQUOT``) is a stall too (``disk``). Any other ``OSError`` (a missing or locked cache file,
    permission) is the date's own error, not a stall."""
    if isinstance(e, HubError):
        return e.status is None or e.status >= 500 or e.status in (401, 403, 407, 429)
    return _disk_full(e) or isinstance(e, (
        psycopg.OperationalError, TransportError, TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError,
        urllib.error.URLError, http.client.HTTPException))


def stall_cause(e: BaseException) -> str:
    """What a ``transient`` error says is down: ``store`` (no connection), ``store_slow`` (a query cancelled by the
    statement timeout), ``proxy`` (a 407 or a refused tunnel), ``access`` (the hub refused the token: 401, 403, or no
    token), ``setup`` (no hub address, no curl), ``hub`` (no answer or a 5xx, ``HubUnavailable``; another hub error
    without a status), ``network`` (no connection, timeout, TLS, a cut-off body),
    ``disk`` (no space or quota: ``ENOSPC``, ``EDQUOT``)."""
    if isinstance(e, psycopg.errors.QueryCanceled):
        return "store_slow"
    if isinstance(e, psycopg.OperationalError):
        return "store"
    if isinstance(e, TransportError):
        return "proxy" if e.proxy_status else "network"
    if isinstance(e, HubError):
        if e.status == 407:
            return "proxy"
        if e.status in (401, 403) or (e.status is None and "token" in str(e)):
            return "access"
        return "setup" if e.status is None and ("not configured" in str(e) or "no curl" in str(e)) else "hub"
    if isinstance(e, (TimeoutError, ConnectionError, socket.gaierror, ssl.SSLError, urllib.error.URLError,
                      http.client.HTTPException)):
        return "network"
    return "disk"


def _code(e: BaseException) -> int | None:
    """The HTTP status of a hub or proxy error (``HubUnavailable``: its last attempt's 5xx), else None."""
    return getattr(e, "status", None) or getattr(e, "last_status", None) or getattr(e, "proxy_status", None)


def no_wait(stall: dict[str, Any]) -> bool:
    """A stall the autopilot runs again without waiting (``STALL_BACKOFF_S``): the store unreachable (a state of
    before ``cause``: its ``source``)."""
    return (stall.get("cause") or stall.get("source")) == "store"


def _when(iso: str | None, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(iso) if iso else default
    except (TypeError, ValueError):
        return default


def next_attempt(stall: dict[str, Any], settings: Settings) -> str | None:
    """When the autopilot tries a stalled backfill again (#447): its wait after the stall's last run
    (``STALL_BACKOFF_S``, none for the store), at the earliest the next cycle; None without the autopilot."""
    if not getattr(settings, "autopilot", True):
        return None
    try:
        at = datetime.fromisoformat(stall["at"])
        poll = timedelta(seconds=max(float(getattr(settings, "inbox_poll_seconds", 0) or 0), 1))
        wait = datetime.fromisoformat(stall["next_attempt"]) if stall.get("next_attempt") else at
    except (KeyError, TypeError, ValueError):
        return None
    return max(wait, at + poll).isoformat(timespec="seconds")


def drop_stall(settings: Settings, *, hub: bool = True) -> None:
    """Forget the stall (nothing left to run): a later outage is a new one (#447). ``hub``: the plan with nothing left
    was the hub's; one of the cache alone keeps a stall of the downloads (its dates are the hub's)."""
    st = state(settings)
    if (st.get("stall") or {}).get("phase") == "bundle" and not hub:
        return
    if st.pop("stall", None) is not None:
        save_state(settings, st)


def _attempt(problems: dict[str, Any], day: Day, kind: str, error: str) -> int:
    """A date's ``kind`` of trouble once more (``state["problems"]``, the reason shown with a problem date); its
    attempts in a row on the same inputs."""
    old = problems.get(day.date) or {}
    n = old.get("attempts", 0) + 1 if old.get("kind") == kind and old.get("sig") == day.sig else 1
    problems[day.date] = {"kind": kind, "why": day.why, "error": error, "attempts": n, "sig": day.sig}
    return n


def _differs(con: psycopg.Connection, day: Day) -> str:
    """What of the date's default run differs from its plan (``Day.sig``: position sets, market snapshot, previous
    snapshot, logic version, private inputs hash, the ``ocp`` / ``cash_balances`` sets in force), for the log of a
    date that does not converge."""
    run = store.default_runs(con, date.fromisoformat(day.date)).get(date.fromisoformat(day.date))
    if run is None:
        return "no default run"
    ids, snap, prev, logic, inputs_hash, *late = day.sig
    have = {"position_set_ids": (run["position_set_ids"], ids), "market_snapshot_id": (run["market_snapshot_id"], snap),
            "prev_market_snapshot_id": (run["prev_market_snapshot_id"], prev),
            "logic_version": (logic_key(run.get("logic_version")), logic_key(logic)),
            "private_inputs_hash": (run["private_inputs_hash"], inputs_hash),
            "late_inputs": (store.dataset_set_ids(con, date.fromisoformat(day.date), known_at=run["positions_known_at"]),
                            late[0] if late else None)}
    if len(late) > 1:  # a signature from before the policy revisions counted (TDM#567) lacks it: stale once
        have["late_reference"] = (store.reference_rev_ids(con, run["positions_known_at"]), late[1])
    out = [f"{k}: run {a}, plan {b}" for k, (a, b) in have.items() if a != b]
    d = date.fromisoformat(day.date)
    out += [f"carried spread of {secid}: run {z:g} bp from {src}, {'none' if held is None else f'{held:g} bp'} there now"
            for secid, src, z, held in stale_spreads(con, {d: run}, d).get(d, [])[:5]]
    return "; ".join(out) or "nothing"


SKIP = object()
"""``bundle_step``: the date's bundle is not cached and there is no hub to download it from."""
PIPE_BROKEN = object()
"""``_eod``: a process of the pool died."""


def _submit(pipe: Any, date: str) -> bool:
    """Give ``date`` to the pool. False when the pool cannot take it (its processes do not start, or one died): the
    date and those after it run in this process, one after the other."""
    try:
        pipe.submit(date)
    except Exception as e:  # noqa: BLE001 - spawn refused, a broken pool: the sequential run is the answer
        pipe.broken = True
        log.warning("backfill: the processes cannot take %s (%s: %s): the dates run one after the other",
                    date, type(e).__name__, e)
        return False
    return True


def _open_pipeline(out: Callable[[str], None], settings: Settings, workers: int, **kw: Any) -> Any:
    """The pool for a pass, or None when it cannot be made (no semaphores, no process, no connection for the orphans):
    the pass then runs the dates one after the other in this process, said (``WARN``)."""
    from . import parallel

    try:
        return parallel.Pipeline(settings, workers, **kw)
    except Exception as e:  # noqa: BLE001 - the sequential run is the answer, whatever the pool says
        msg = (f"backfill: the processes cannot be started ({type(e).__name__}: {e}): "
               "the dates run one after the other")
        log.warning("%s", msg)
        out("WARN " + msg)
        return None


def _published(con: psycopg.Connection, settings: Settings, p: Plan, day: Day, *, second: bool = False) -> int | None:
    """After the pool broke: the run a process of it published for ``day`` before it died (the date is current now,
    but was not in the plan; ``second``: it has a newer run than the plan's, with the second derivatives), else None.
    Such a date is not made again: the sequential run would have made it once."""
    d = date.fromisoformat(day.date)
    now_ = next((x for x in plan(con, settings, d, d, p.rows if p.hub or not second else None).days
                 if x.date == day.date), None)
    if now_ is None or now_.status != "done":
        return None
    if second and (now_.run_id == day.run_id or now_.second == "todo"):
        return None
    return now_.run_id


def _chain_current(con: psycopg.Connection, settings: Settings, p: Plan, day: Day) -> bool:
    """A date of the chain (``Day.chain``, TDM#660), asked once the dates before it are done: is it current now, with
    the spreads its source dates hold now? The plan of the date alone has no queue to chain on, only the stored runs:
    a source that was made again with another spread makes the date stale (``stale_spreads``), one that came out with
    the same spread, or was not made at all (its own trouble, the date stays queued), leaves it as it is. Current by the
    same inputs (``Day.sig``): nothing else of the date moved either."""
    d = date.fromisoformat(day.date)
    now_ = next((x for x in plan(con, settings, d, d, p.rows).days if x.date == day.date), None)
    return now_ is not None and now_.status == "done" and now_.sig == day.sig


def _go_on(pipe: Any, date: str) -> None:
    """The pass goes on to the next date after ``date`` failed (the sequential run does, and the next date then runs
    without it): its turn in the pool. A pass that stops on it closes the pipeline instead and does not call this."""
    if pipe is not None:
        pipe.release(date)


def _eod(pipe: Any, date: str, alone: Callable[[], int | None], out: Callable[[str], None]) -> Any:
    """The run of ``date``: its answer from the pool when it was given to it (the lines it printed first, as
    ``dayrun.eod_run`` prints them), else ``alone()``. ``PIPE_BROKEN`` when a process of the pool died."""
    from . import parallel

    if pipe is None or not pipe.has(date):
        return alone()
    try:
        return pipe.result(date, lambda line: out("  " + line))["rid"]
    except parallel.PoolBroken:
        return PIPE_BROKEN


def run(settings: Settings, p: Plan, *, store_url: str | None = None, client: Any = None,
        budget_s: float | None = None, out: Callable[[str], None] = print,
        progress: Callable[[int, int, str], None] | None = None, contexts: Any = None,
        exclude: set[str] | frozenset[str] = frozenset(), now: datetime | None = None,
        hub_answered: bool = False, workers: int = 1, pool_init: Any = None, pool: Any = None) -> dict[str, Any]:
    """Step 3 (module doc) over the plan's ``todo`` dates, oldest first, until ``budget_s`` seconds have passed
    (None: all), but ``exclude`` (the dates the autopilot's eod step ran or failed this cycle). ``client``: a
    ``HubClient`` for the bundles not cached (None: only the cached dates run). ``now``: the run's start, for the
    stall's times (default: the wall clock). ``hub_answered``: the hub answered this cycle (the autopilot's sync step),
    so a date passed over on its download, no later date to tell, does not load on its own. Returns {done: [dates], failed: [(date, error)], unconverged: [dates
    still stale after their run, tried again next time], skipped: [dates without a bundle], unchanged: [dates of the
    chain (``Day.chain``, TDM#660) whose sources came out with the same spread: not made again], left: n, stalled: the
    stall the run stopped on or None, seconds}. ``progress(x, m, date)``, before each date (TDM#587): ``x`` the dates of
    the plan already current (``done``) plus those this run computed, ``m`` the dates with positions and a close
    (done, to run, failed), ``date`` the one computed now: «посчитано x из m, сейчас ДД.ММ», from the first date.

    The first pass of the history (TDM#627): the runs are made without the second derivatives (``eod_run`` with
    ``no_gamma``), 40 % of the time (17 s against 42 s a date of 2000 positions); the dates are current, but their runs say they have no ``gamma`` (lineage
    ``second_order``) until ``run_second`` makes them again. ``complete`` in the result: the run went through all its
    dates (not stopped by the budget or a stall), so the second pass may follow.

    A date of the chain (``Day.chain``: listed only because a date it carries a spread from is to run, TDM#660) is
    made again only if that spread came out different: when the run reaches it, the dates before it are done, and the
    store is asked (``_chain_current``). A source that came out with the same spread, or one that never comes out (its
    own error every time, passed over), leaves the dates after it as they are: they are not made again every cycle
    (``unchanged``). The plan keeps listing them while the source is queued (they may be stale); with the pool the date
    may already be computing ahead: it is dropped when the run reaches it, or, if it got to publish first, a run of the
    same numbers is the whole cost.

    ``workers`` > 1 (TDM#628, ``desk.parallel``): the dates are computed in that many processes at once, each date in
    one, published in the order of the dates; this process downloads the bundles ahead and takes the dates' answers in
    order, so everything below (the stall, the problem dates, the budget) works on them as on dates run one after the
    other. The budget cuts what has not started: the dates already submitted (at most ``workers`` + 1) are finished.
    The runs are the sequential run's, to the last bit (``test_parallel_backfill_628``). ``pool_init``: (function, args)
    each process of the pool calls when it starts (a test's hook; the function of an importable module). ``pool``: a
    ``parallel.Pool`` of the caller's that serves the passes of a cycle in turn (its processes start once and keep their
    prepared markets); none: this run starts its own and ends it. If the processes cannot start, or one dies, the dates
    run one after the other here (said), and a date a process published before it died is not made again."""
    from . import parallel
    from .dayrun import ContextCache, eod_run

    t0 = time.monotonic()
    contexts = contexts if contexts is not None else ContextCache()

    def indented(line: str) -> None:
        out("  " + line)

    todo = [d for d in p.todo if d.date not in exclude]
    cut = False  # the run stopped before the end of ``todo``: the budget, or a stall of the store or the network
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    unconverged: list[str] = []
    skipped: list[str] = []
    unchanged: list[str] = []  # chain dates (``Day.chain``) whose sources came out with the same spread: not made again
    cache_dir = Path(settings.snapshot_cache)
    st = state(settings)
    fails = st.setdefault("failed", {})
    problems = st.setdefault("problems", {})
    con: psycopg.Connection | None = None  # the date planned again after its run
    now = now or datetime.now(UTC)
    live = st.get("stall")  # the stall standing (#447), until a date of this run gets past its phase
    if live and ((not set(live.get("dates") or [live.get("date")]) & {d.date for d in p.todo}
                  and (p.hub or live.get("phase") != "bundle"))  # a plan of the cache alone has no download dates
                 or now - _when(live.get("at"), now) > STALL_STALE):
        st.pop("stall")  # none of its dates is left to run, or it is old: forgotten, a new outage is a new stall
        live = None
    passed: list[tuple[str, str]] = []  # (date, phase) passed over this run on a store or network error they hit
    # run after run (#23)
    waiting: list[str] = []  # the date the last run stopped on, passed over this run: the next date tells (#447)
    stall: dict[str, Any] | None = None  # the store's or the network's error the run stopped on (#447)
    tried = 0  # dates attempted this run (done or failed), for the time budget
    through = False  # the stall standing at the start is over: a date got past its phase
    pipe = None
    broke = False  # the pool broke: a date may have been published by a process that was in flight
    prefetched: dict[str, Any] = {}  # date -> downloaded (bool), SKIP or the error its bundle step raised (workers > 1)
    blocked: list[int] = []  # the index of a date whose bundle step failed ahead: the dates after it wait for the loop

    def bundle_step(day: Day) -> bool | object:
        """The date's bundle: downloaded when it is not cached, or cached under another snapshot than the plan's
        (the hub re-published the date or the previous close, #439); an unchanged cached date is not. Returns whether it
        was downloaded, ``SKIP`` when it is not cached and there is no hub."""
        sd = SnapshotDir(cache_dir)
        have = set(sd.market_dates())
        # The previous close is the plan's, the one the run then picks (``bundle.previous_close``, #203);
        # a cached eod_preliminary one is not the hub's to sync
        planned = ((day.date, day.market_id),
                   (day.prev_date if day.prev_cut == "eod_final" else None, day.sig[2] if day.sig else None))
        want = [x for x, sid in planned if x and (x not in have or (p.hub and sid and sd.market_id(x) != sid))]
        if want and client is not None:
            cache.sync(client, cache_dir, want)
            return True
        return False if day.date in have else SKIP

    def fill(i: int) -> None:
        """Workers > 1: the dates from ``i`` on are given to the pool, up to ``workers`` + ``LOOKAHEAD`` in flight, their
        bundles downloaded here first (while the processes compute the dates before them). A bundle that fails stops
        the filling there: the loop reaches that date, raises the error where a date run alone raises it, and goes on."""
        if pipe.broken:
            return
        for j in range(i, min(len(todo), i + workers + parallel.LOOKAHEAD)):
            d = todo[j]
            if d.date in prefetched or pipe.has(d.date):
                continue
            if blocked and j > blocked[0]:
                return
            if budget_s is not None and (j > i or tried) and time.monotonic() - t0 >= budget_s:
                return  # nothing new starts after the budget (the first date of the run always does)
            try:
                r = bundle_step(d)
            except Exception as e:  # noqa: BLE001 - raised again at its date
                prefetched[d.date] = e
                blocked[:] = [j]
                return
            prefetched[d.date] = r
            if r is not SKIP and not _submit(pipe, d.date):
                return

    def current(day: Day) -> bool:
        """The chain date (``Day.chain``) is current after its sources were made (``_chain_current``). An error of the
        store asking is no answer: the date runs, and meets it there."""
        nonlocal con
        try:
            con = con or store.connect(store_url or store.resolve_url(settings))
            return _chain_current(con, settings, p, day)
        except Exception as e:  # noqa: BLE001 - the date itself says what is wrong with the store
            log.warning("backfill %s: the chain check failed (%s: %s): the date runs", day.date, type(e).__name__, e)
            if con is not None:
                with contextlib.suppress(Exception):
                    con.close()
                con = None
            return False

    def loads(phases: set[str]) -> None:  # a date got past the store and the network in ``phases`` (a date run from
        # the cache, ``run`` only, says nothing of the downloads): the passed dates' errors there are their own (#217)
        nonlocal through, live
        for x, ph in list(passed):
            if ph in phases:
                problems[x]["alone"] = True
                passed.remove((x, ph))
        if live is not None and live.get("phase", "run") not in phases:
            return
        for x in waiting:  # not tried since: its own trouble only if it fails again on its own (#23)
            if (problems.get(x) or {}).get("kind") == "transient":
                problems[x]["recheck"] = True
        waiting.clear()
        through, live = through or live is not None, None

    try:
        if workers > 1 and len(todo) > 1:
            pipe = _open_pipeline(out, settings, workers, store_url=store_url or store.resolve_url(settings),
                                  snapshots=str(cache_dir), no_gamma=True, wait_carried=True, init=pool_init,
                                  pool=pool)
        for i, day in enumerate(todo):
            if day.chain and current(day):  # (before the pool is asked for it: a date it already took is dropped)
                if pipe is not None:
                    pipe.discard(day.date)
                if isinstance(prefetched.pop(day.date, None), Exception):
                    blocked.clear()
                unchanged.append(day.date)
                continue
            if pipe is not None:
                fill(i)
            submitted = pipe is not None and pipe.has(day.date)  # a date in the pool is taken whatever the budget says
            if budget_s is not None and tried and not submitted and time.monotonic() - t0 >= budget_s:
                cut = True
                break  # the budget counts every date tried, failed or not, but at least one is tried (#447)
            if (passed and live is not None and day.date == live.get("probe") and i < len(todo) - 1
                    and (problems.get(day.date) or {}).get("attempts", 0) >= MAX_ATTEMPTS):
                waiting.append(day.date)  # stopped on it run after run as well: the next date tells (#447)
                if submitted:
                    pipe.discard(day.date)
                continue
            tried += 1
            if progress is not None:  # cumulative, not this run's index: «0 из N» for hours read as stopped (TDM#587)
                progress(p.count("done") + len(done) + len(unchanged),
                         p.count("done") + p.count("todo") + p.count("failed"), day.date)
            still, phase, downloaded = None, "bundle", False
            try:
                step = prefetched.pop(day.date) if day.date in prefetched else bundle_step(day)
                if isinstance(step, Exception):
                    blocked.clear()  # the loop is at the date whose bundle failed: the dates after it go on
                    raise step  # the bundle step of a date given to the pool ahead failed: as it fails here
                if step is SKIP:
                    skipped.append(day.date)
                    tried -= 1  # nothing was tried: no bundle, no time spent
                    continue
                downloaded = step
                phase = "run"
                out(f"backfill {day.date}: {day.why}")
                t1 = time.monotonic()
                alone = partial(eod_run, settings, day.date, snapshots=cache_dir, store_url=store_url, report_dir=None,
                                out=indented, contexts=contexts, read_model=False, no_gamma=True)
                if broke:  # a date the processes published before the pool broke is not made again
                    con = con or store.connect(store_url or store.resolve_url(settings))
                    rid = _published(con, settings, p, day) or alone()
                else:
                    rid = _eod(pipe, day.date, alone, out)
                if rid is PIPE_BROKEN:  # a process of the pool died: this date and the rest one after the other
                    pipe.close()
                    pipe, broke = None, True
                    prefetched.clear()
                    con = con or store.connect(store_url or store.resolve_url(settings))
                    rid = _published(con, settings, p, day) or alone()
                if rid is not None:
                    # one after the other: this date's time; in the pool: the pass's time a date, what the ETA wants
                    st["seconds_per_date"] = round((time.monotonic() - (t0 if workers > 1 else t1))
                                                   / (tried if workers > 1 else 1), 1)
                    con = con or store.connect(store_url or store.resolve_url(settings))
                    d = date.fromisoformat(day.date)
                    again = next((x for x in plan(con, settings, d, d, p.rows).days if x.date == day.date), None)
                    if again is not None and again.status != "done":
                        still = (again.why or again.status, _differs(con, day))
            except Exception as e:  # one date's bad data must not stop the others
                if transient(e):  # the store or the network, not the date: not remembered as failed
                    err = f"{type(e).__name__}: {e}"
                    old = problems.get(day.date) or {}
                    same = old.get("kind") == "transient" and old.get("sig") == day.sig
                    n = _attempt(problems, day, "transient", err)
                    problems[day.date].update(source="store" if isinstance(e, psycopg.OperationalError) else "network",
                                              cause=stall_cause(e), status=_code(e), phase=phase)
                    # its own error before, or it waited while the stall ended and fails again on its own (#447)
                    alone = same and (old.get("alone") or (old.get("recheck") and n >= MAX_ATTEMPTS))
                    if alone:
                        problems[day.date]["alone"] = True
                    elif same and old.get("recheck"):
                        problems[day.date]["recheck"] = True
                    log.exception("backfill %s interrupted (%d in a row)", day.date, n)
                    failed.append((day.date, f"{err} (not the date's data: tried again next time)"))
                    if alone and not passed:
                        _go_on(pipe, day.date)
                        continue  # its own error (later dates loaded meanwhile): it no longer holds the queue (#23)
                    if n >= MAX_ATTEMPTS and not passed:
                        passed.append((day.date, phase))  # stopped on it before: its own error or the network's? the
                        _go_on(pipe, day.date)
                        continue  # next date tells
                    stall = {**_stall(problems[day.date], err, (passed or [(day.date, phase)])[0][0],
                                      [x for x, _ in passed] + [day.date]), "probe": day.date}
                    cut = True
                    break  # the rest waits for next time, the dates not problem dates (#447)
                # the date's own error: it got past the store and the network (the hub answered its download)
                loads({"bundle"} if phase == "bundle" else {"run", "bundle"} if downloaded else {"run"})
                log.exception("backfill %s failed", day.date)
                fails[day.date] = day.sig
                _attempt(problems, day, "failed", f"{type(e).__name__}: {e}")
                failed.append((day.date, f"{type(e).__name__}: {e}"))
                _go_on(pipe, day.date)
                continue
            loads({"run", "bundle"} if downloaded else {"run"})
            if rid is None:  # a run that created nothing (no positions for the date after all): the date's failure
                log.warning("backfill %s: no run created", day.date)
                fails[day.date] = day.sig
                _attempt(problems, day, "failed", "no run created (no positions for the date)")
                failed.append((day.date, "no run created (no positions for the date)"))
                continue
            if still is not None:  # the run did not make the date current: the plan's reason did not go away
                why, diff = still
                n = _attempt(problems, day, "unconverged", "")
                problems[day.date]["error"] = f"does not converge: {why} after {n} runs"
                log.warning("backfill %s still stale after its run (%d of %d): %s; %s", day.date, n, MAX_ATTEMPTS,
                            why, diff)
                if n >= MAX_ATTEMPTS:  # a problem date: not tried again on the same inputs
                    fails[day.date] = day.sig
                    failed.append((day.date, problems[day.date]["error"]))
                else:
                    unconverged.append(day.date)
                continue
            fails.pop(day.date, None)
            problems.pop(day.date, None)
            done.append(day.date)
    finally:
        if pipe is not None:
            pipe.close()
        if con is not None:
            con.close()
        if stall is None and hub_answered and any(ph == "bundle" for _, ph in passed):
            loads({"bundle"})  # the hub answered this cycle, the date's bundle does not come: its own (#217, #447)
        if stall is None and passed:  # passed over, and no later date to tell: as stopped on it
            p0 = problems[passed[0][0]]
            stall = _stall(p0, p0["error"], passed[0][0], [x for x, _ in passed])
        if stall is not None:  # since its first stop; the autopilot's next attempt later after each stop
            old = live or {}
            at = now + timedelta(seconds=time.monotonic() - t0)
            stops = old.get("stops", 0) + 1
            wait = 0 if no_wait(stall) else STALL_BACKOFF_S[min(stops, len(STALL_BACKOFF_S)) - 1]
            dates = list(dict.fromkeys([*(old.get("dates") or []), *stall["dates"]]))
            st["stall"] = {**stall, "dates": dates, "since": old.get("since") or at.isoformat(timespec="seconds"),
                           "at": at.isoformat(timespec="seconds"), "stops": stops,
                           "next_attempt": (at + timedelta(seconds=wait)).isoformat(timespec="seconds")}
        elif through:
            st.pop("stall", None)
        save_state(settings, st)
    left = len(todo) - len(done) - len(failed) - len(unconverged) - len(skipped) - len(unchanged)
    return {"done": done, "failed": failed, "unconverged": unconverged, "skipped": skipped, "unchanged": unchanged,
            "left": left,
            "stalled": stall and st["stall"], "seconds": round(time.monotonic() - t0, 1),
            "complete": not cut and stall is None}


def totals_diff(old: dict[str, Any] | None, new: dict[str, Any] | None) -> list[str]:
    """How the totals per book of two runs (``run.totals``: PV, DV01, CS01 and the PnL components) differ, one entry
    each, ``[]`` when they are equal to the last bit; the book missing from one of the runs is one entry."""
    old, new = old or {}, new or {}
    out = []
    for book in sorted(set(old) | set(new)):
        a, b = old.get(book), new.get(book)
        if a is None or b is None:
            out.append(f"{book}: only in the {'new' if a is None else 'replaced'} run")
            continue
        for k in sorted(set(a) | set(b)):
            x, y = a.get(k), b.get(k)
            if isinstance(x, dict) or isinstance(y, dict):
                x, y = x or {}, y or {}
                out += [f"{book} {k}.{c}: {x.get(c)} -> {y.get(c)}" for c in sorted(set(x) | set(y)) if x.get(c) != y.get(c)]
            elif x != y:
                out.append(f"{book} {k}: {x} -> {y}")
    return out


MISMATCH_SHOWN = 3
"""How many of the differing totals a warning of the second pass names."""


def run_second(settings: Settings, p: Plan, *, store_url: str | None = None, budget_s: float | None = None,
               out: Callable[[str], None] = print, progress: Callable[[int, int, str], None] | None = None,
               contexts: Any = None, exclude: set[str] | frozenset[str] = frozenset(),
               workers: int = 1, pool_init: Any = None, pool: Any = None) -> dict[str, Any]:
    """The second pass of the history (TDM#627, module doc): the dates of ``p`` whose default run was made without
    the second derivatives (``Day.second`` ``todo``), the newest first, until ``budget_s`` seconds have passed (None:
    all), but ``exclude``. A date's run is made again in full from the cache (``eod_run``; no download: a date whose
    bundle is not cached is left): the inputs are the first pass's, so its value, first derivatives and PnL come out
    the same to the last bit, and the new run supersedes the first pass's with the ``gamma`` and the bonds' ``convexity``
    added (a run is never changed, ADR-006). The new run's totals per book are checked against the replaced run's
    (``totals_diff``): a difference is a warning (the log, ``mismatch`` here, ``status`` ``second.mismatch_dates``), the
    new run stays: the inputs are not frozen on it, the screens show it as any default run.
    A date is never made current by this pass. When its inputs changed since the plan (a report, a hub correction), its
    new run read the new ones and its totals are not compared; when the run read the old ones and the plan now wants
    the date again, the date is ``moved`` (not a failure: the next plan says what is to do, the first pass runs it
    again). When the inputs are the plan's and the date is still not current (a reason that comes with the
    full run), that is the date's failure, remembered under the date's inputs (``state["second"]``, not under the run:
    every pass makes a new one), so the cycles do not run the date through both passes for ever: after ``MAX_ATTEMPTS``
    on the same inputs it is left (``Day.second`` ``failed``).
    ``progress(x, m, date)`` as ``run``'s: ``x`` the dates with the second derivatives, ``m`` all that have a run.
    An error of the store or the network stops the pass (tried again next cycle); another error is the date's, counted
    the same way. ``workers`` > 1: the dates in that many processes at once, published newest first as one after the
    other (``run``, ``desk.parallel``; ``pool``: the same). Returns {done: [dates], failed: [(date, error)], moved: [dates], skipped: [dates
    not cached], mismatch: [(date, what differs)], left: n, stopped: the transient error or None, seconds}."""
    from . import parallel
    from .dayrun import ContextCache, eod_run

    t0 = time.monotonic()
    contexts = contexts if contexts is not None else ContextCache()

    def indented(line: str) -> None:
        out("  " + line)

    todo = [d for d in p.second_todo if d.date not in exclude]
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    moved: list[str] = []
    skipped: list[str] = []
    mismatch: list[tuple[str, str]] = []
    stopped: str | None = None
    cache_dir = Path(settings.snapshot_cache)
    st = state(settings)
    st2 = st.setdefault("second", {})
    fails = st2.setdefault("failed", {})
    odd = st2.setdefault("mismatched", {})
    total = p.count_second("done") + p.count_second("todo") + p.count_second("failed")
    con: psycopg.Connection | None = None
    tried = 0
    pipe = None
    broke = False  # the pool broke: a run may have been published by a process that was in flight
    have = set(SnapshotDir(cache_dir).market_dates())

    def fill(i: int) -> None:
        if pipe.broken:
            return
        for j in range(i, min(len(todo), i + workers + parallel.LOOKAHEAD)):
            d = todo[j]
            if pipe.has(d.date) or not d.cached or d.date not in have:
                continue
            if budget_s is not None and (j > i or tried) and time.monotonic() - t0 >= budget_s:
                return
            if not _submit(pipe, d.date):
                return

    try:
        if workers > 1 and len(todo) > 1:
            # newest first, each date reads the dates before it, which this pass publishes later: no read waits
            pipe = _open_pipeline(out, settings, workers, store_url=store_url or store.resolve_url(settings),
                                  snapshots=str(cache_dir), no_gamma=False, wait_carried=False, init=pool_init,
                                  pool=pool)
        for i, day in enumerate(todo):
            if pipe is not None:
                fill(i)
            if budget_s is not None and tried and not (pipe is not None and pipe.has(day.date)) \
                    and time.monotonic() - t0 >= budget_s:
                break
            if not day.cached or day.date not in have:
                skipped.append(day.date)  # nothing to value on; the first pass downloaded it, a prune took it
                continue
            tried += 1
            if progress is not None:
                progress(p.count_second("done") + len(done), total, day.date)
            try:
                out(f"backfill {day.date}: second derivatives (the run {day.run_id} has none)")
                t1 = time.monotonic()
                alone = partial(eod_run, settings, day.date, snapshots=cache_dir, store_url=store_url, report_dir=None,
                                out=indented, contexts=contexts, read_model=False)
                if broke:  # a run the processes published before the pool broke is not made again
                    con = con or store.connect(store_url or store.resolve_url(settings))
                    rid = _published(con, settings, p, day, second=True) or alone()
                else:
                    rid = _eod(pipe, day.date, alone, out)
                if rid is PIPE_BROKEN:  # a process of the pool died: this date and the rest one after the other
                    pipe.close()
                    pipe, broke = None, True
                    con = con or store.connect(store_url or store.resolve_url(settings))
                    rid = _published(con, settings, p, day, second=True) or alone()
                if rid is None:
                    raise RuntimeError("no run created (no positions for the date)")
                st["seconds_per_date_second"] = round((time.monotonic() - (t0 if workers > 1 else t1))
                                                      / (tried if workers > 1 else 1), 1)
                con = con or store.connect(store_url or store.resolve_url(settings))
                d = date.fromisoformat(day.date)
                again = next((x for x in plan(con, settings, d, d, p.rows if p.hub else None).days
                              if x.date == day.date), None)
                if again is not None and again.status != "done" and again.sig != day.sig:
                    log.info("backfill second pass %s: its inputs changed meanwhile (%s); the first pass runs it again",
                             day.date, again.why or again.status)
                    moved.append(day.date)
                    continue
                if again is None or again.status != "done" or again.run_id == day.run_id or again.second == "todo":
                    raise RuntimeError(f"the new run does not make the date current: "
                                       f"{again.why if again is not None and again.why else again and again.status}")
                diff = []
                if again.sig == day.sig:  # the same inputs: the same totals (else the new run read newer ones)
                    diff = totals_diff(*(con.execute("SELECT totals FROM run WHERE run_id = %s", (r,)).fetchone()[0]
                                         for r in (day.run_id, again.run_id)))
                else:
                    log.info("backfill second pass %s: its inputs changed meanwhile, the new run read the new ones: "
                             "the totals are not compared", day.date)
            except Exception as e:  # one date's error must not stop the others
                if transient(e):
                    stopped = f"{type(e).__name__}: {e}"
                    log.exception("backfill second pass %s interrupted", day.date)
                    break
                err = f"{type(e).__name__}: {e}"
                old = fails.get(day.date) or {}
                n = old.get("attempts", 0) + 1 if old.get("sig") == day.sig else 1
                fails[day.date] = {"sig": day.sig, "run_id": day.run_id, "attempts": n, "error": err}
                log.exception("backfill second pass %s failed (%d of %d)", day.date, n, MAX_ATTEMPTS)
                failed.append((day.date, err))
                _go_on(pipe, day.date)
                continue
            fails.pop(day.date, None)
            done.append(day.date)
            if diff:  # the full run does not reproduce the first pass's totals: the date's numbers moved under the owner
                more = f" (and {len(diff) - MISMATCH_SHOWN} more)" if len(diff) > MISMATCH_SHOWN else ""
                what = "; ".join(diff[:MISMATCH_SHOWN]) + more
                msg = (f"backfill second pass {day.date}: the totals of the new run {again.run_id} differ from the "
                       f"replaced run {day.run_id}: {what}")
                log.warning("%s", msg)
                out("WARN " + msg)
                mismatch.append((day.date, what))
                odd[day.date] = {"run_id": again.run_id, "replaced": day.run_id, "diff": what}
            else:
                odd.pop(day.date, None)
    finally:
        if pipe is not None:
            pipe.close()
        if con is not None:
            con.close()
        # only the dates of this plan (``desk backfill`` passes one date at a time): the others' memory stays
        for x in [x for x in fails if x in {d.date for d in p.days if d.second not in ("todo", "failed")}]:
            fails.pop(x)  # the date has its second derivatives (or no run): nothing to remember
        for x in [x for x in odd if x in {d.date for d in p.days if d.status != "done"}]:
            odd.pop(x)  # the date has no run in force
        save_state(settings, st)
    return {"done": done, "failed": failed, "moved": moved, "skipped": skipped, "mismatch": mismatch,
            "left": len(todo) - len(done) - len(failed) - len(skipped) - len(moved),
            "stopped": stopped, "seconds": round(time.monotonic() - t0, 1)}


def _stall(problem: dict[str, Any], error: str, first: str, dates: list[str]) -> dict[str, Any]:
    """The stall a run stops on (#447): the error, the date it stands on (the oldest passed over), what is down
    (``source``: store or network; ``cause``, ``status``: ``stall_cause``, the HTTP code), the phase, the dates,
    ``probe``: the date it stopped on after passing over the oldest (None: no later date to tell)."""
    return {"error": error, "date": first, "dates": dates, "probe": None,
            **{k: problem.get(k) for k in ("source", "cause", "status", "phase")}}


def status(con: psycopg.Connection, settings: Settings, today: date | None = None) -> dict[str, Any]:
    """The backfill's state on the cache and the store (no hub call): ``Plan.summary`` plus the oldest default run
    and the first positions date; ``enabled`` (setting ``backfill``)."""
    today = today or date.today()
    p = plan(con, settings, start_date(settings), today)
    oldest = con.execute(f"SELECT min(business_date) FROM run WHERE run_id IN ({store.DEFAULT_RUNS})").fetchone()[0]
    first = con.execute("SELECT min(valid_date) FROM position_set").fetchone()[0]
    st = state(settings)
    problems = st.get("problems", {})
    probs = []  # the failed dates and those whose own store or network error keeps failing (#23, #217), oldest first
    for d in p.days:
        r = problems.get(d.date) or {}
        if d.status == "failed":
            probs.append({"date": d.date, "kind": "unconverged" if r.get("kind") == "unconverged" else "failed",
                          "why": d.why, "error": r.get("error"), "attempts": r.get("attempts")})
        elif d.status == "todo" and r.get("kind") == "transient" and r.get("alone") and r.get("sig") == d.sig:
            probs.append({"date": d.date, "kind": "unreachable", "why": d.why, "error": r.get("error"),
                          "attempts": r.get("attempts")})
    sec = st.get("seconds_per_date")
    s = p.summary()
    # the store's or the network's error the backfill stopped on (#447), while its dates are to run (those of a
    # stall of the downloads are the hub's, not in this plan of the cache)
    stall = st.get("stall") if s["todo"] else None
    if stall and stall.get("phase") != "bundle" and not set(stall.get("dates") or [stall.get("date")]) & {
            d.date for d in p.todo}:
        stall = None
    if stall:
        stall = {**stall, "next_attempt": next_attempt(stall, settings)}
    from .marketdata import fixing_history

    try:  # TDM#590: the stored fixings history the runs fill the cuts' windows from
        fixings = {**fixing_history.status(con),
                   "loading": bool(getattr(settings, "hub_url", None)) and int(getattr(settings, "history_days", 0) or 0) > 0,
                   "since": str(getattr(settings, "history_rates_from", "") or "") or None}
    except psycopg.Error as e:
        con.rollback()
        fixings = {"error": f"{type(e).__name__}: {e}"}
    # TDM#627: the second pass (the second derivatives of the dates the first pass made without them), the dates it gave up on
    sec2 = st.get("seconds_per_date_second")
    gave_up = (st.get("second") or {}).get("failed", {})
    odd = (st.get("second") or {}).get("mismatched", {})
    mismatch = [{"date": d.date, "run_id": d.run_id, "diff": odd[d.date].get("diff")} for d in p.days
                if d.status == "done" and (odd.get(d.date) or {}).get("run_id") == d.run_id]  # of the run in force
    second = {**s["second"], "total": s["second"]["done"] + s["second"]["todo"] + s["second"]["failed"],
              "mismatched": len(mismatch), "mismatch_dates": mismatch[:50],
              "problem_dates": [{"date": d.date, "error": (gave_up.get(d.date) or {}).get("error"),
                                 "attempts": (gave_up.get(d.date) or {}).get("attempts")}
                                for d in p.days if d.second == "failed"][:50],
              "eta_s": round(s["second"]["todo"] * sec2) if isinstance(sec2, (int, float)) and s["second"]["todo"]
              else None}
    return {**s, "second": second, "pass": "first" if s["todo"] else "second" if second["todo"] else "done",
            "total": s["done"] + s["todo"] + s["failed"], "enabled": bool(getattr(settings, "backfill", True)),
            "fixings_history": fixings, **_workers(settings),
            "oldest_run": oldest.isoformat() if oldest else None,
            "first_positions": first.isoformat() if first else None,
            "inbox_scanned_from": (st.get("inbox") or {}).get("from"),
            "failed_dates": sorted(st.get("failed", {}))[:20],
            "problem_dates": probs[:50], "stalled": stall,
            "eta_s": round(s["todo"] * sec) if isinstance(sec, (int, float)) and s["todo"] else None}


def note_workers(settings: Settings, n: int) -> None:
    """The autopilot's cycle starts its pool with ``n`` processes (``parallel.workers_for``, read from the settings and the
    free memory of this moment): remembered, so that the screen shows what the pool started with, not a figure worked out
    again when the page is opened (the free memory is another by then). Written when it changes."""
    st = state(settings)
    auto = not int(getattr(settings, "backfill_workers", 0) or 0)
    if st.get("workers") != {"n": n, "auto": auto}:
        st["workers"] = {"n": n, "auto": auto}
        save_state(settings, st)


def _workers(settings: Settings) -> dict[str, Any]:
    """The processes the history is computed in (``workers``) and whether the setting leaves the number to the program
    (``workers_auto``, TDM#628): what the screen shows beside the passes. A number set by the owner is what the next cycle
    uses. The program's own choice depends on the free memory of the moment, so the screen shows the one the last cycle
    started its pool with (``note_workers``), and, before the first cycle, what it would choose now."""
    from . import parallel

    asked = int(getattr(settings, "backfill_workers", 0) or 0)
    seen = state(settings).get("workers")
    started = seen["n"] if not asked and isinstance(seen, dict) and seen.get("auto") and isinstance(seen.get("n"), int) \
        and seen["n"] >= 1 else None
    return {"workers": asked or started or parallel.auto_workers(), "workers_auto": not asked}


def text(p: Plan, *, dry_run: bool = False) -> str:
    s = p.summary()
    lines = [f"backfill {s['from']}..{s['to']} ({'hub' if p.hub else 'cache only'}): {s['dates']} business dates, "
             f"{s['done']} up to date, {s['todo']} to run, {s['no_positions']} without positions"
             + (f", {s['no_market']} without a close" if s['no_market'] else "")
             + (f", {s['failed']} failed before (same inputs)" if s['failed'] else "")]
    if s["second"]["todo"] or s["second"]["failed"]:  # TDM#627
        lines.append(f"  second derivatives (gamma): {s['second']['todo']} dates to make again, "
                     f"{s['second']['done']} have them" + (f", {s['second']['failed']} given up" if s["second"]["failed"] else ""))
    if dry_run:
        for d in p.days:
            if d.status in ("todo", "failed"):
                lines.append(f"  {d.date}  {d.status:<7} {d.why}" + ("" if d.cached else "  (bundle to download)"))
        if s["to_download"]:
            lines.append(f"  {s['to_download']} dates to download from the hub (market and static bundle each)")
    return "\n".join(lines)

