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
   another previous close, read its inputs before an ``ocp`` report or ``cash_balances`` in force on the date were
   recorded (``late_why``, TDM#181), or was
   made by another valuation or core version (``same_logic``: ``VALUATION_VERSION`` and the core version of
   ``logic_version()`` recompute the history; a new release or Windows bundle of the same computation does not). Otherwise it is skipped: nothing
   is computed twice.
3. **runs**, oldest first: the date's bundle and the previous date's (flash PnL) from the hub when not cached
   (``cache.sync``), then ``cli.eod_run``, the code path of every EOD run. The prepared market of a date is kept for
   the next one (its previous date: ``cli.ContextCache``), so each date's static snapshot and curves are built once.
   One date at a time: a model price carries its bond's spread from the latest earlier run
   (``store.carried_spreads``), so a date's run depends on the runs before it; dates in parallel would compute
   other numbers. A date that fails is not tried again on the same inputs (``STATE_FILE``). A run that created
   nothing is the date's failure too. After its run a date is planned again (the same ``plan``): still stale (a
   reason that does not go away, e.g. a previous close the run picks differently from the plan), it is logged with
   the differing field and tried again next time; after ``MAX_ATTEMPTS`` runs it is a problem date, not tried again
   on the same inputs (#23). A saved failed signature from before private input hashes were included is stale
   once: after one retry the new signature suppresses unchanged failures again.
   An error of the store or the network stops the run (the next dates would hit it too)
   and is not remembered as the date's failure; a date that hits one ``MAX_ATTEMPTS`` runs in a row no longer stops
   the queue: the run goes on past it, the date tried again every time.

The bundles of the backfilled dates stay in the snapshot cache (each date's market and static snapshot, several MB a
date: ``--dry-run`` says how many are to be downloaded).

``status(con, settings)`` (the settings page, ``/api/v1/backfill/status``, the header's indicator, the diagnostics):
the range, how many dates have their run, are still to run, have no positions, failed; the oldest default run; the
problem dates with their reason, error and attempts (``problem_dates``) and the estimated time left (``eta_s``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import psycopg

from . import logic_version, store
from .marketdata import cache
from .marketdata.bundle import SnapshotDir, previous_market
from .marketdata.hub import HubError, TransportError
from .settings import Settings

log = logging.getLogger("desk.backfill")

DEFAULT_FROM = date(2026, 1, 5)
"""The first ``eod_final`` close on the hub: the default start (setting ``backfill_from``)."""
STATE_FILE = "backfill.json"
MAX_ATTEMPTS = 2
"""Runs of a date still stale after its run, or stopped on it by the store or the network, before it is a problem
date (#23)."""


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
    """The close before the date (the hub's, else the cache's): the flash PnL's basis."""


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
    all_dates = sorted(set(hub_ids) | cached)
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
        before = [x for x in all_dates if x < d]
        pd = before[-1] if before else None
        if hub is not None and before:
            prev = hub_ids.get(before[-1]) or (sd.market_id(before[-1]) if before[-1] in cached else None)
        else:
            pc = previous_market(sd, d)
            prev = sd.market_id(*pc) if pc else None
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
            days.append(Day(d, "done", market_id=snap, cached=is_cached, prev_date=pd))
        elif failed.get(d) == sig:
            days.append(Day(d, "failed", why, sig, snap, is_cached, pd))
        else:
            days.append(Day(d, "todo", why, sig, snap, is_cached, pd))
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


def transient(e: BaseException) -> bool:
    """An error of the store connection, the network, the disk or the hub as a whole (unreachable, 5xx, token or
    proxy refused), not of the date's data: such a date is not marked failed (a failed date is not tried again on the
    same inputs), and the run stops there, since the next dates would hit it too."""
    if isinstance(e, HubError):
        return e.status is None or e.status >= 500 or e.status in (401, 403, 407)
    return isinstance(e, (psycopg.OperationalError, TransportError, OSError))


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
        exclude: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """Step 3 (module doc) over the plan's ``todo`` dates, oldest first, until ``budget_s`` seconds have passed
    (None: all), but ``exclude`` (the dates the autopilot's eod step ran or failed this cycle). ``client``: a
    ``HubClient`` for the bundles not cached (None: only the cached dates run). Returns {done: [dates], failed:
    [(date, error)], unconverged: [dates still stale after their run, tried again next time], skipped: [dates
    without a bundle], left: n, seconds}."""
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
    try:
        for i, day in enumerate(todo):
            if budget_s is not None and done and time.monotonic() - t0 >= budget_s:
                break
            if progress is not None:
                progress(i, len(todo), day.date)
            still = None
            try:
                have = set(SnapshotDir(cache_dir).market_dates())
                want = [x for x in (day.date, day.prev_date) if x and x not in have]
                if want and client is not None:
                    cache.sync(client, cache_dir, want)
                elif day.date in want:
                    skipped.append(day.date)
                    continue
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
                    n = _attempt(problems, day, "transient", f"{type(e).__name__}: {e}")
                    log.exception("backfill %s interrupted (%d in a row)", day.date, n)
                    failed.append((day.date, f"{type(e).__name__}: {e} (not the date's data: tried again next time)"))
                    if n < MAX_ATTEMPTS:
                        break  # the rest waits for next time
                    continue  # this date hits it run after run: it no longer holds the queue (#23)
                log.exception("backfill %s failed", day.date)
                fails[day.date] = day.sig
                _attempt(problems, day, "failed", f"{type(e).__name__}: {e}")
                failed.append((day.date, f"{type(e).__name__}: {e}"))
                continue
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
        save_state(settings, st)
    left = len(todo) - len(done) - len(failed) - len(unconverged) - len(skipped)
    return {"done": done, "failed": failed, "unconverged": unconverged, "skipped": skipped, "left": left,
            "seconds": round(time.monotonic() - t0, 1)}


def status(con: psycopg.Connection, settings: Settings, today: date | None = None) -> dict[str, Any]:
    """The backfill's state on the cache and the store (no hub call): ``Plan.summary`` plus the oldest default run
    and the first positions date; ``enabled`` (setting ``backfill``)."""
    today = today or date.today()
    p = plan(con, settings, start_date(settings), today)
    oldest = con.execute(f"SELECT min(business_date) FROM run WHERE run_id IN ({store.DEFAULT_RUNS})").fetchone()[0]
    first = con.execute("SELECT min(valid_date) FROM position_set").fetchone()[0]
    st = state(settings)
    problems = st.get("problems", {})
    probs = []  # the failed dates and those a store or network error keeps stopping on (#23), oldest first
    for d in p.days:
        r = problems.get(d.date) or {}
        if d.status == "failed" or (d.status == "todo" and r.get("kind") == "transient"
                                    and r.get("attempts", 0) >= MAX_ATTEMPTS):
            probs.append({"date": d.date, "why": d.why, "error": r.get("error"), "attempts": r.get("attempts")})
    sec = st.get("seconds_per_date")
    s = p.summary()
    return {**s, "total": s["done"] + s["todo"] + s["failed"], "enabled": bool(getattr(settings, "backfill", True)),
            "oldest_run": oldest.isoformat() if oldest else None,
            "first_positions": first.isoformat() if first else None,
            "inbox_scanned_from": (st.get("inbox") or {}).get("from"),
            "failed_dates": sorted(st.get("failed", {}))[:20],
            "problem_dates": probs[:50],
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

