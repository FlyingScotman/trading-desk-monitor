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
   the run picks once its bundle is synced, #203), read its inputs before an ``ocp`` report or ``cash_balances`` in
   force on the date were recorded (``late_why``, TDM#181), or was
   made by another valuation or core version (``same_logic``: ``VALUATION_VERSION`` and the core version of
   ``logic_version()`` recompute the history; a new release or Windows bundle of the same computation does not). Otherwise it is skipped: nothing
   is computed twice.
3. **runs**, oldest first: the date's bundle and the previous date's (flash PnL) from the hub when not cached, or
   cached under another snapshot than the hub's of the plan (a correction of an older date, #439) (``cache.sync``), then ``cli.eod_run``, the code path of every EOD run. The prepared market of a date is kept for
   the next one (its previous date: ``cli.ContextCache``), so each date's static snapshot and curves are built once.
   One date at a time: a model price carries its bond's spread from the latest earlier run
   (``store.carried_spreads``), so a date's run depends on the runs before it; dates in parallel would compute
   other numbers. A date that fails is not tried again on the same inputs (``STATE_FILE``). A run that created
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

import errno
import http.client
import json
import logging
import re
import socket
import ssl
import time
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from . import logic_version, store
from .marketdata import cache
from .marketdata.bundle import SnapshotDir, previous_close
from .marketdata.hub import HubError, TransportError
from .settings import Settings

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
    market_id: str | None = None
    cached: bool = False
    prev_date: str | None = None
    """The close before the date (``bundle.previous_close``, the run's too, #203): the flash PnL's basis."""
    prev_cut: str | None = None
    """Its cut: ``eod_final``, else a cached ``eod_preliminary`` (not synced from the hub)."""


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

    def to_downloads(self) -> int:
        return sum(1 for d in self.todo if not d.cached)

    def summary(self) -> dict[str, Any]:
        return {"from": self.date_from, "to": self.date_to, "dates": len(self.days), "done": self.count("done"),
                "todo": self.count("todo"), "no_positions": self.count("no_positions"),
                "no_market": self.count("no_market"), "failed": self.count("failed"), "hub": self.hub,
                "to_download": self.to_downloads(), "next": [d.date for d in self.todo[:5]]}


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


LATE_WHY = {"ocp": "ocp report", "cash_balances": "cash balances"}
"""``store.LATE_DATASETS`` as a reason to run a date again (TDM#181)."""


def late_why(con: psycopg.Connection, d: date, run: dict | None) -> str | None:
    """"ocp report changed after the run" (and / or the cash balances) when an ``ocp`` or ``cash_balances`` set in
    force on ``d`` was recorded after the default run ``run`` read its inputs (``store.late_inputs``); else None."""
    late = store.late_inputs(con, d, run.get("positions_known_at")) if run else []
    return " and ".join(LATE_WHY[x] for x in late) + " changed after the run" if late else None


def plan(con: psycopg.Connection, settings: Settings, date_from: date, date_to: date,
         hub: list[tuple[str, str]] | None = None) -> Plan:
    """What the backfill would do (module doc, step 2). ``hub``: the hub's (date, snapshot id) rows (``hub_rows``);
    None plans on the cache alone."""
    sd = SnapshotDir(Path(settings.snapshot_cache))
    lo, hi = date_from.isoformat(), date_to.isoformat()
    cached = set(sd.market_dates())
    hub_ids = dict(hub or [])
    dates = sorted({d for d in (hub_ids if hub is not None else cached) if lo <= d <= hi})
    runs = store.default_runs(con, date_from)
    logic = logic_version()
    failed = state(settings).get("failed", {})
    first_pos = con.execute("SELECT min(valid_date) FROM position_set").fetchone()[0]
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
        inputs_hash = store.positions_for(con, date.fromisoformat(d)).private_inputs_hash
        late = late_why(con, date.fromisoformat(d), run)
        why = ("no default run" if run is None else "positions changed" if run["position_set_ids"] != ids
               else "market snapshot changed" if run["market_snapshot_id"] != snap
               else "previous snapshot changed" if prev is not None and run["prev_market_snapshot_id"] != prev
               else "private inputs changed" if run["private_inputs_hash"] != inputs_hash
               else late
               or ("program updated" if not same_logic(run.get("logic_version"), logic) else None))
        sig = [ids, snap, prev, logic, inputs_hash, store.dataset_set_ids(con, date.fromisoformat(d))]
        if why is None:
            days.append(Day(d, "done", market_id=snap, cached=is_cached, prev_date=pd, prev_cut=pcut))
        elif failed.get(d) == sig:
            days.append(Day(d, "failed", why, sig, snap, is_cached, pd, pcut))
        else:
            days.append(Day(d, "todo", why, sig, snap, is_cached, pd, pcut))
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
    return "; ".join(f"{k}: run {a}, plan {b}" for k, (a, b) in have.items() if a != b) or "nothing"


def run(settings: Settings, p: Plan, *, store_url: str | None = None, client: Any = None,
        budget_s: float | None = None, out: Callable[[str], None] = print,
        progress: Callable[[int, int, str], None] | None = None, contexts: Any = None,
        exclude: set[str] | frozenset[str] = frozenset(), now: datetime | None = None,
        hub_answered: bool = False) -> dict[str, Any]:
    """Step 3 (module doc) over the plan's ``todo`` dates, oldest first, until ``budget_s`` seconds have passed
    (None: all), but ``exclude`` (the dates the autopilot's eod step ran or failed this cycle). ``client``: a
    ``HubClient`` for the bundles not cached (None: only the cached dates run). ``now``: the run's start, for the
    stall's times (default: the wall clock). ``hub_answered``: the hub answered this cycle (the autopilot's sync step),
    so a date passed over on its download, no later date to tell, does not load on its own. Returns {done: [dates], failed: [(date, error)], unconverged: [dates
    still stale after their run, tried again next time], skipped: [dates without a bundle], left: n, stalled: the
    stall the run stopped on or None, seconds}."""
    from .cli import ContextCache, eod_run

    t0 = time.monotonic()
    contexts = contexts if contexts is not None else ContextCache()
    todo = [d for d in p.todo if d.date not in exclude]
    done: list[str] = []
    failed: list[tuple[str, str]] = []
    unconverged: list[str] = []
    skipped: list[str] = []
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
        for i, day in enumerate(todo):
            if budget_s is not None and tried and time.monotonic() - t0 >= budget_s:
                break  # the budget counts every date tried, failed or not, but at least one is tried (#447)
            if (passed and live is not None and day.date == live.get("probe") and i < len(todo) - 1
                    and (problems.get(day.date) or {}).get("attempts", 0) >= MAX_ATTEMPTS):
                waiting.append(day.date)  # stopped on it run after run as well: the next date tells (#447)
                continue
            tried += 1
            if progress is not None:
                progress(i, len(todo), day.date)
            still, phase, downloaded = None, "bundle", False
            try:
                sd = SnapshotDir(cache_dir)
                have = set(sd.market_dates())
                # not cached, or cached under another snapshot than the hub's of the plan (the hub re-published the
                # date or the previous close, #439): its bundle again; an unchanged cached date is not downloaded.
                # The previous close is the plan's, the one the run then picks (``bundle.previous_close``, #203);
                # a cached eod_preliminary one is not the hub's to sync
                planned = ((day.date, day.market_id),
                           (day.prev_date if day.prev_cut == "eod_final" else None, day.sig[2] if day.sig else None))
                want = [x for x, sid in planned
                        if x and (x not in have or (p.hub and sid and sd.market_id(x) != sid))]
                if want and client is not None:
                    cache.sync(client, cache_dir, want)
                    downloaded = True
                elif day.date not in have:
                    skipped.append(day.date)
                    tried -= 1  # nothing was tried: no bundle, no time spent
                    continue
                phase = "run"
                out(f"backfill {day.date}: {day.why}")
                t1 = time.monotonic()
                rid = eod_run(settings, day.date, snapshots=cache_dir, store_url=store_url, report_dir=None,
                              out=lambda s: out("  " + s), contexts=contexts, read_model=False)
                if rid is not None:
                    st["seconds_per_date"] = round(time.monotonic() - t1, 1)
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
                        continue  # its own error (later dates loaded meanwhile): it no longer holds the queue (#23)
                    if n >= MAX_ATTEMPTS and not passed:
                        passed.append((day.date, phase))  # stopped on it before: its own error or the network's? the
                        continue  # next date tells
                    stall = {**_stall(problems[day.date], err, (passed or [(day.date, phase)])[0][0],
                                      [x for x, _ in passed] + [day.date]), "probe": day.date}
                    break  # the rest waits for next time, the dates not problem dates (#447)
                # the date's own error: it got past the store and the network (the hub answered its download)
                loads({"bundle"} if phase == "bundle" else {"run", "bundle"} if downloaded else {"run"})
                log.exception("backfill %s failed", day.date)
                fails[day.date] = day.sig
                _attempt(problems, day, "failed", f"{type(e).__name__}: {e}")
                failed.append((day.date, f"{type(e).__name__}: {e}"))
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
    left = len(todo) - len(done) - len(failed) - len(unconverged) - len(skipped)
    return {"done": done, "failed": failed, "unconverged": unconverged, "skipped": skipped, "left": left,
            "stalled": stall and st["stall"], "seconds": round(time.monotonic() - t0, 1)}


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
    return {**s, "total": s["done"] + s["todo"] + s["failed"], "enabled": bool(getattr(settings, "backfill", True)),
            "oldest_run": oldest.isoformat() if oldest else None,
            "first_positions": first.isoformat() if first else None,
            "inbox_scanned_from": (st.get("inbox") or {}).get("from"),
            "failed_dates": sorted(st.get("failed", {}))[:20],
            "problem_dates": probs[:50], "stalled": stall,
            "eta_s": round(s["todo"] * sec) if isinstance(sec, (int, float)) and s["todo"] else None}


def text(p: Plan, *, dry_run: bool = False) -> str:
    s = p.summary()
    lines = [f"backfill {s['from']}..{s['to']} ({'hub' if p.hub else 'cache only'}): {s['dates']} business dates, "
             f"{s['done']} up to date, {s['todo']} to run, {s['no_positions']} without positions"
             + (f", {s['no_market']} without a close" if s['no_market'] else "")
             + (f", {s['failed']} failed before (same inputs)" if s['failed'] else "")]
    if dry_run:
        for d in p.days:
            if d.status in ("todo", "failed"):
                lines.append(f"  {d.date}  {d.status:<7} {d.why}" + ("" if d.cached else "  (bundle to download)"))
        if s["to_download"]:
            lines.append(f"  {s['to_download']} dates to download from the hub (market and static bundle each)")
    return "\n".join(lines)

