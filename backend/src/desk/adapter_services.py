"""The adapter's «services» capability, version 1 (08 §2.2; TDM#548): the connection to the organisation's services
is set up in the monitor's settings, then the monitor fetches their data by itself, in the background.

The monitor knows nothing about the services. A compatible private package declares ``describe()["services"] =
{"version": 1, "entry": "<relative .py path>"}``; its helper (``desk.helper_proc``: contained entry, ``-I -B``,
restricted environment, one form-encoded request, one JSON object back) answers four operations:

* ``describe`` and ``save`` (``f.<key>`` values): the settings panel (PANEL: sections of typed fields and actions)
  and the feeds (FEED: id, title, dataset, the adapter source that reads its files, enabled by default, ready,
  schedule, ``after_positions``). The monitor renders the panel generically and caches both under
  ``<home>/adapter-services/state.json`` with the adapter fingerprint they belong to.
* ``action`` (``action``, the panel's ``f.<key>`` as typed, optional ``arg``): lines, an optional table, a refreshed
  panel, an optional pick list (``choices`` + ``choose_action``; without ``choose_action`` no pick list). Every action
  (and a save whose answer has no feeds) is followed by a describe, so the panel and the feeds' ``ready`` are current.
* ``feed`` (``feed``, optional ``date``, a fresh empty ``output_dir``): files written into ``output_dir``, the
  business dates they carry; or no files (a feed that keeps its data locally). A ``trade_terms`` feed run without a
  date (by its schedule or by hand) gets the latest positions business date of the store. Each file goes through the
  ordinary scanner/contract/store path as the feed's declared source only (``inbox.scan.ingest_feed``: another source
  is rejected before it is read; files of a feed whose source the adapter does not declare are refused), under the
  cycle lock; the runs the accepted units affect are then made again (``desk.recompute.after_inputs``: the EOD run of each business
  date of the accepted units, today's current runs when they read those inputs).

**Process.** One helper process at a time, in one worker thread: the owner's operations first (``save``, ``action``,
``describe``), then feeds asked for with «Запустить сейчас», then (``start()``, ``desk serve`` only) the scheduled
feeds. Every operation holds ``helper_proc.HELPER_LOCK``, which an acquisition job holds too: they never overlap.
Timeouts: ``describe``/``save`` 60 s, ``action`` 15 min (a browser login waits), ``feed`` 30 min. ``cancel`` and
shutdown kill the process tree. Secrets (``f.<key>`` of a ``secret`` field) are only in the request body: never in
a job status, the state file, a log line or an error.

The owner's operation waits for no queued background work (TDM#615): a feed that first runs the feed of its list runs
the owner's operation in between (``_owner_first``), and a feed whose files wait for the cycle lock runs it while it
waits (no helper process runs then). An owner's operation run in that wait may change the adapter's config folder
(a connect, a save): the files then were fetched under the old configuration, so the run ends ``cancelled``/
``adapter_reconfigured`` (not a failure: no retry step, its files stay pending) and the same feed is queued again at
once, to run under the new one; a change with no owner's operation in the wait stays ``error``/``adapter_changed``.
While it waits for a running feed or history request, GET says which one and
since when (``job.waiting_for``); «Остановить загрузку» (``cancel("feed")``) kills that helper: the run is
``cancelled`` (not a failure: no retry step is counted), its files not yet taken in stay pending, and the feed runs
again by its schedule. An action's answer is published as soon as the helper returns (``job.state``, its lines); the
describe that follows it runs as a ``describe`` (the next operation of the owner may be queued meanwhile) and sets
``job.refreshed`` when it is done. ``job.section``: the panel section of the action (a pick list's action: the
section that offered it), ``job.login``: an action that may open a login window (the action's ``login`` flag, else
the ids ``LOGIN_ACTIONS``). The action ``CONNECT_ACTION`` (the whole connection to the trades service in one press; its
``table`` is the checklist of its steps) that ends ``connected``, ``connected_partially`` or ``partial`` (only its
first load fell short) queues the feed ``CONNECT_FEED`` as «Запустить сейчас» does, after its describe: the adapter
hands its first load over to that run (``job.feed_queued``).

**Scheduling** (``next_due``). Local wall clock of the work machine. ``{"every_min": N, "from": "HH:MM", "to":
"HH:MM", "weekdays_only": bool}``: inside the window, N minutes after the last start (at once when never run).
``{"daily_at": ["HH:MM", ...], "weekdays_only": bool}``: the first slot after the last start; a feed never run
catches up today's slot already passed, once; missed slots of days the monitor was off are one run, not many.
``books`` (TDM#557): the feed gets the owner's books as a repeated request field ``book`` (``owner_books``: active,
with a current position set; at most ``MAX_BOOKS``); none: not called, ``blocked``/``no_books``.
``trades_file`` (TDM#561): before such a feed (``trade_terms``) the monitor compares the stored ``trade_list`` of the
owner's books with the terms it holds (``desk.terms_plan``) and, with a plan, writes the live trades of the list as
``<state_dir>/requests/<run id>.json`` (``terms-request`` v1) and passes its path as the one field ``trades_file``;
the adapter then asks for the terms of the new and changed trades only. The last ``KEEP_REQUESTS`` files are kept for
diagnosis. The outcome gets a line of the counts. Without a plan the feed runs as before.
``after_positions``: also once when a positions set for a newer business date than the feed last saw is in the
store (the first sight only records it), with that date as ``date``. Only enabled (the monitor's switch, default the
feed's own ``enabled``) and ready feeds run by themselves. ``blocked``/``login_required`` pauses the feed («нужен
вход») until a describe after a successful save/action/describe says it is ready, or a manual run succeeds. Errors
(``error``, another ``blocked``, a bad answer, an ingestion failure) retry after 5, 15, then every 60 minutes; a
cancelled retry waits for its next step too. Files a run produced but could not take in (the cycle lock stayed busy,
the store failed, a cancel) stay ``pending`` and are taken in at the feed's next run, before the helper is asked
again: the adapter may answer ``no_change`` then and never produce them again. Feed
state (last start/end, outcome, failures, next retry, the pause, the positions date seen) is persisted, so a restart
does not fetch again at once.

**Trades of past dates** (TDM#569, ``desk.trades_history``; on by default since 04.11.2026, silent while the feed is off in the monitor or the adapter does not declare ``history``). A feed with the dataset
``trades``, ``books`` and the new ``history: true`` (and, when the source knows it, ``history_from``: the earliest date
it keeps) also answers for a past ``date``. When nothing else is due (the owner's operations, «Запустить сейчас», the
scheduled feeds), the worker asks it for one past date at a time (``_history_step``): newest block of dates first, the
limits and the hours of ``trades_history``. It does not touch the feed's own state (``runs``): the schedule, the retries
and the login pause of today's feed are not affected by it. Unlike a feed run, a history request (a) takes its files in
with one non-blocking try of the cycle lock: busy, they stay ``pending`` in ``trades_history.json`` and are taken in at
the next step, before another date is asked, with ``HELPER_LOCK`` free meanwhile; (b) makes no run after the ingestion:
the eod and backfill steps do, by the changed hash of the date's inputs (the eod step leaves such dates to the backfill in
the trading hours). A feed due within the time of the last request is waited for. The answer's date must be the asked one.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any

from . import backfill, helper_proc, recompute, store, terms_plan, trades_history
from .autopilot import CycleLock
from .inbox import scan
from .inbox.loader import AdapterHandle, check_services, fingerprint, load_adapter
from .inbox.timesem import MSK
from .settings import Settings

log = logging.getLogger("desk.adapter_services")

VERSION = 1
TIMEOUTS = {"describe": 60.0, "save": 60.0, "action": 900.0, "feed": 1800.0}
BACKOFF_MIN = (5, 15, 60)
TICK_S = 30.0
NEWEST_CONNECT_S = 5  # seconds the feeds' read of the newest positions waits for the store
RECHECK_S = 600.0          # the scheduler loads the adapter again this often: a new fingerprint is described again
AVAILABILITY_S = 15.0      # GET's availability is reused this long (the settings page polls while a job runs)
LOCK_WAIT_S = 1200.0       # a feed's files wait this long for the cycle lock (an autopilot cycle), then retry later
KEEP_REQUESTS = 5          # the request files (``trades_file``) kept for diagnosis
MAX_REQUEST_TRADES = 50000  # the adapter refuses a longer list (08 §2.2); the monitor then sends none
KEEP_OUT = 20              # the output folders of the last feed runs kept (the archive keeps accepted files anyway)
STATE_FILE = "state.json"
LOGIN_ACTIONS = ("login", "v2_login", "v2_connect")  # a login window may open: the action declares no ``login`` flag
CONNECT_ACTION, CONNECT_FEED = "v2_connect", "trades_intraday"  # a connect that got in runs that feed at once
CONNECTED = ("connected", "connected_partially")
RECONFIGURED = "adapter_reconfigured"  # the owner changed the adapter's config while a feed waited (``_ingest``)

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")  # dotted keys too (``services.positions.base_url``)
ID_RE = _ID  # a feed, field or action id (the routes check the feed id of their path with it)
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_HHMM = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_HEX = re.compile(r"[0-9a-f]{64}")
_RID = re.compile(r"[0-9a-f]{32}")  # a feed run's id: its output folder under ``out``
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
FIELD_TYPES = ("text", "url", "bool", "select", "secret", "int")
ACTION_STATES = ("complete", "partial", "blocked", "error")
FEED_STATES = ("complete", "partial", "skipped", "blocked", "error")
OK_FILE = ("accepted", "partially_accepted", "superseded", "duplicate")


class ServiceError(Exception):
    def __init__(self, code: str, state: str = "blocked"):
        super().__init__(code)
        self.code, self.state = code, state


class Invalid(Exception):
    """A helper answer that does not follow the protocol."""


@dataclass(frozen=True)
class Helper:
    handle: AdapterHandle
    root: Path
    entry: Path


def helper(st: Settings) -> Helper:
    """The services helper of the current adapter, or ServiceError with why not (codes as acquisition's)."""
    h = load_adapter(st.private_adapter, str(st.private_adapter_config) if st.private_adapter_config else None,
                     st.private_adapter_sha256, inbox_dirs=[str(p) for p in st.inbox_dirs])
    if not h.ok:
        raise ServiceError("adapter_unavailable")
    if "services" not in h.describe:
        raise ServiceError("services_unavailable")
    cap = check_services(h.describe)[0]
    if cap is None:
        raise ServiceError("capability_invalid")
    if not h.package_dir:
        raise ServiceError("helper_unavailable")
    root = Path(h.package_dir).resolve()
    entry = helper_proc.contained(root, cap["entry"])
    if entry is None:
        raise ServiceError("helper_unavailable")
    if fingerprint(str(root), h.config_dir) != h.fingerprint:
        raise ServiceError("adapter_changed")
    if st.acquisition_python is None or not st.acquisition_python.is_file():
        raise ServiceError("runtime_unavailable")
    return Helper(h, root, entry)


# ---- the helper's answers, checked ---------------------------------------------------------------------------------

def _str(v: Any, limit: int, *, empty: bool = True) -> str:
    if not isinstance(v, str) or len(v) > limit or (not empty and not v):
        raise Invalid
    return v


def _opt(d: dict, key: str, limit: int) -> str:
    return _str(d[key], limit) if d.get(key) is not None else ""


def _lines(v: Any, n: int = 200) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or len(v) > n:
        raise Invalid
    return [_str(x, 2000) for x in v]


def clean_panel(p: Any) -> dict:
    """PANEL as the page renders it; secret fields never carry a value (``value`` "", ``set`` bool)."""
    if not isinstance(p, dict) or not isinstance(p.get("sections"), list) or len(p["sections"]) > 30:
        raise Invalid
    sections, keys, actions = [], set(), set()
    for s in p["sections"]:
        if not isinstance(s, dict) or not isinstance(s.get("fields", []), list) or len(s.get("fields", [])) > 60 \
                or not isinstance(s.get("actions", []), list) or len(s.get("actions", [])) > 30:
            raise Invalid
        fields = []
        for f in s.get("fields", []):
            if not isinstance(f, dict) or not isinstance(f.get("key"), str) or not _ID.fullmatch(f["key"]) \
                    or f.get("type") not in FIELD_TYPES or f["key"] in keys:
                raise Invalid
            keys.add(f["key"])
            out = {"key": f["key"], "label": _str(f.get("label", f["key"]), 200), "type": f["type"],
                   "help": _opt(f, "help", 2000), "placeholder": _opt(f, "placeholder", 200)}
            v = f.get("value")
            if f["type"] == "secret":
                out["value"], out["set"] = "", f.get("set") is True
            elif f["type"] == "bool":
                out["value"] = v is True
            elif f["type"] == "int":
                if v is not None and (type(v) is not int or abs(v) > 10**12):
                    raise Invalid
                out["value"] = v
            else:
                out["value"] = "" if v is None else _str(v, 4096)
            if f["type"] == "select":
                opts = f.get("options")
                if not isinstance(opts, list) or len(opts) > 200 or not all(isinstance(o, dict) for o in opts):
                    raise Invalid
                out["options"] = [{"value": _str(o.get("value"), 4096), "label": _str(o.get("label", o.get("value")), 200)}
                                  for o in opts]
            fields.append(out)
        acts = []
        for a in s.get("actions", []):
            if not isinstance(a, dict) or not isinstance(a.get("id"), str) or not _ID.fullmatch(a["id"]):
                raise Invalid
            actions.add(a["id"])
            act = {"id": a["id"], "label": _str(a.get("label", a["id"]), 200),
                   "confirm": _opt(a, "confirm", 1000), "long": a.get("long") is True}
            if isinstance(a.get("login"), bool):  # declared: a login window may open (else by the id, LOGIN_ACTIONS)
                act["login"] = a["login"]
            acts.append(act)
        sections.append({"id": _str(s.get("id", ""), 64), "title": _opt(s, "title", 200), "help": _opt(s, "help", 4000),
                         "fields": fields, "actions": acts})
    return {"title": _opt(p, "title", 200), "status": _lines(p.get("status"), 50), "sections": sections}


def panel_keys(panel: dict | None) -> dict[str, str]:
    """{field key: type} of a cleaned panel."""
    return {f["key"]: f["type"] for s in (panel or {}).get("sections", []) for f in s["fields"]}


def panel_actions(panel: dict | None) -> set[str]:
    return {a["id"] for s in (panel or {}).get("sections", []) for a in s["actions"]}


def clean_schedule(s: Any) -> dict | None:
    """A schedule of the protocol, or None (no schedule: manual and ``after_positions`` runs only)."""
    if not isinstance(s, dict):
        return None
    wk = s.get("weekdays_only") is True
    if "every_min" in s:
        n, fr, to = s.get("every_min"), s.get("from", "00:00"), s.get("to", "23:59")
        if type(n) is not int or not 1 <= n <= 7 * 24 * 60 or not isinstance(fr, str) or not isinstance(to, str) \
                or not _HHMM.fullmatch(fr) or not _HHMM.fullmatch(to) or fr > to:
            return None
        return {"every_min": n, "from": fr, "to": to, "weekdays_only": wk}
    slots = s.get("daily_at")
    if isinstance(slots, list) and 1 <= len(slots) <= 48 and all(isinstance(x, str) and _HHMM.fullmatch(x)
                                                                  for x in slots):
        return {"daily_at": sorted(set(slots)), "weekdays_only": wk}
    return None


def _day(v: Any) -> str | None:
    """A date ``YYYY-MM-DD`` or None (anything else is dropped: an optional key)."""
    try:
        return v if isinstance(v, str) and date.fromisoformat(v).isoformat() == v else None
    except ValueError:
        return None


def clean_feeds(v: Any, h: AdapterHandle | None) -> list[dict]:
    """FEEDs. ``source_declared``: the adapter declares ``source`` for ``dataset`` (its ``sources``). A feed whose
    source it does not declare may still run (one that archives locally and returns no files); only its files are
    refused (``feed_source_invalid``), since no adapter source would read them."""
    if not isinstance(v, list) or len(v) > 50:
        raise Invalid
    out, seen = [], set()
    for f in v:
        if not isinstance(f, dict) or not isinstance(f.get("id"), str) or not _ID.fullmatch(f["id"]) \
                or f["id"] in seen or not isinstance(f.get("enabled"), bool) or not isinstance(f.get("ready"), bool):
            raise Invalid
        seen.add(f["id"])
        feed = {"id": f["id"], "title": _str(f.get("title", f["id"]), 200), "dataset": _str(f.get("dataset"), 64),
                "source": _str(f.get("source"), 100, empty=False), "enabled": f["enabled"], "ready": f["ready"],
                "why_not_ready": _opt(f, "why_not_ready", 1000), "schedule": clean_schedule(f.get("schedule")),
                "after_positions": f.get("after_positions") is True, "books": f.get("books") is True,
                "trades_file": f.get("trades_file") is True, "history": f.get("history") is True,
                "history_from": _day(f.get("history_from"))}
        decl = h.source(feed["source"]) if h is not None else None
        feed["source_declared"] = h is None or (decl is not None and decl.get("dataset") == feed["dataset"])
        out.append(feed)
    return out


MAX_BOOKS = 200  # the request's ``book`` values (08 §2.2)


def owner_books(con: Any, on: date) -> tuple[list[str], list[str]]:
    """The owner's books for a feed with ``books`` (08 §2.2): the active books in force on ``on`` whose current
    position set has rows, sorted; and the notes for the owner when some were left out (a book id over 200 characters,
    more than ``MAX_BOOKS`` books: never silently). Empty books: the feed is not called."""
    held = {r[1] for r in store.positions_for(con, on).rows}
    active = {b["book_id"] for b in store.current_books(con, on) if b.get("active", True) is not False}
    books, notes = sorted(held & active), []
    long_ = [b for b in books if len(b) > 200]
    if long_:
        books = [b for b in books if len(b) <= 200]
        notes.append(f"Книг с идентификатором длиннее 200 символов пропущено: {len(long_)}.")
    if len(books) > MAX_BOOKS:
        notes.append(f"Книг с позициями {len(books)}, в запрос вошли первые {MAX_BOOKS}.")
        books = books[:MAX_BOOKS]
    return books, notes


def clean_action(r: dict) -> dict:
    if r.get("state") not in ACTION_STATES or not isinstance(r.get("code"), str) or not _CODE.fullmatch(r["code"]):
        raise Invalid
    out: dict[str, Any] = {"state": r["state"], "code": r["code"], "lines": _lines(r.get("lines"))}
    t = r.get("table")
    if t is not None:
        if not isinstance(t, dict) or not isinstance(t.get("columns"), list) or not isinstance(t.get("rows"), list) \
                or len(t["columns"]) > 30 or len(t["rows"]) > 500:
            raise Invalid
        cols = [_str(c, 200) for c in t["columns"]]
        rows = []
        for row in t["rows"]:
            if not isinstance(row, list) or len(row) > len(cols):
                raise Invalid
            rows.append([c if c is None or isinstance(c, (int, float)) and not isinstance(c, bool) else _str(c, 2000)
                         for c in row])
        out["table"] = {"columns": cols, "rows": rows}
    if r.get("panel") is not None:
        out["panel"] = clean_panel(r["panel"])
    ca = r.get("choose_action")
    if r.get("choices") is not None and ca is not None:  # no choose_action: no pick list
        ch = r["choices"]
        if not isinstance(ch, list) or len(ch) > 200 or not all(isinstance(c, dict) for c in ch) \
                or not isinstance(ca, str) or not _ID.fullmatch(ca):
            raise Invalid
        out["choices"] = [{"id": _str(c.get("id"), 4096, empty=False), "label": _str(c.get("label", c.get("id")), 500)}
                          for c in ch]
        out["choose_action"] = ca
    if r.get("field") is not None:
        out["field"] = _str(r["field"], 100)
    return out


def panel_action(panel: dict | None, action_id: str | None) -> tuple[str | None, dict | None]:
    """(the section id, the declaration) of a panel action; (None, None) when the panel has no such action."""
    for s in (panel or {}).get("sections", []):
        for a in s["actions"]:
            if a["id"] == action_id:
                return s["id"], a
    return None, None


def clean_feed_result(r: dict) -> dict:
    if r.get("state") not in FEED_STATES or not isinstance(r.get("code"), str) or not _CODE.fullmatch(r["code"]):
        raise Invalid
    files = r.get("files") or []
    dates = r.get("dates") or []
    if not isinstance(files, list) or len(files) > 100 or not isinstance(dates, list) or len(dates) > 400:
        raise Invalid
    clean = []
    for f in files:
        if not isinstance(f, dict) or not isinstance(f.get("file"), str) or not _NAME.fullmatch(f["file"]) \
                or not isinstance(f.get("sha256"), str) or not _HEX.fullmatch(f["sha256"]):
            raise Invalid
        clean.append({"file": f["file"], "sha256": f["sha256"]})
    for d in dates:
        try:
            if not isinstance(d, str) or date.fromisoformat(d).isoformat() != d:
                raise ValueError
        except ValueError:
            raise Invalid from None
    return {"state": r["state"], "code": r["code"], "files": clean, "dates": sorted(set(dates)),
            "lines": _lines(r.get("lines"))}


# ---- scheduling ---------------------------------------------------------------------------------------------------

def _hm(s: str) -> dtime:
    return dtime(int(s[:2]), int(s[3:]))


def _at(d: date, t: dtime, like: datetime) -> datetime:
    return datetime.combine(d, t, like.tzinfo)


def next_allowed(sched: dict, t: datetime) -> datetime:
    """The first moment from ``t`` on that the schedule allows a run (its window, weekdays)."""
    for i in range(9):
        d = t.date() + timedelta(days=i)
        if sched.get("weekdays_only") and d.weekday() >= 5:
            continue
        if "every_min" in sched:
            start, end = _at(d, _hm(sched["from"]), t), _at(d, _hm(sched["to"]), t)
            c = max(t, start) if i == 0 else start
            if c <= end:
                return c
        else:
            return t if i == 0 else _at(d, dtime(0), t)
    return t + timedelta(days=9)


def next_slot(sched: dict, after: datetime) -> datetime:
    """``daily_at``: the first slot strictly after ``after``."""
    for i in range(9):
        d = after.date() + timedelta(days=i)
        if sched.get("weekdays_only") and d.weekday() >= 5:
            continue
        for s in sched["daily_at"]:
            c = _at(d, _hm(s), after)
            if c > after:
                return c
    return after + timedelta(days=9)


def _ts(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def next_due(feed: dict, run: dict, now: datetime) -> datetime | None:
    """When the schedule (or a retry) runs the feed next; at or before ``now``: due. None: not by the clock (no
    schedule and no retry, or paused for a login). See the module doc."""
    if run.get("needs_login"):
        return None
    sched = feed.get("schedule")
    retry = _ts(run.get("retry_at")) if run.get("failures") else None
    if retry is not None:
        return next_allowed(sched, retry) if sched else retry
    if sched is None:
        return None
    last = _ts(run.get("last_started"))
    if "every_min" in sched:
        return next_allowed(sched, now if last is None else last + timedelta(minutes=sched["every_min"]))
    return next_slot(sched, last if last is not None else _at(now.date(), dtime(0), now))


def backoff(failures: int) -> timedelta:
    return timedelta(minutes=BACKOFF_MIN[min(max(failures, 1), len(BACKOFF_MIN)) - 1])


def plan_line(c: dict) -> str:
    """The owner's line about a terms plan (``terms_plan.plan`` counts)."""
    return (f"Список: сделок {c['live']}, книг {c['books']}; запрошено {c['requested']} (новых {c['new']}, "
            f"изменённых {c['changed']}), уже были {c['have']}, закрыто {c['closed']}.")


def write_request(st: Settings, rid: str, plan: dict) -> Path:
    """The terms request file of a plan (08 §2.2 ``trades_file``), atomically, UTF-8, under ``state_dir/requests``."""
    d = state_dir(st) / "requests"
    d.mkdir(parents=True, exist_ok=True)
    doc = {"format": "terms-request", "version": 1, "date": plan["date"], "books": plan["books"],
           "trades": plan["trades"]}
    tmp = d / f"{rid}.json.tmp"
    tmp.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    final = d / f"{rid}.json"
    os.replace(tmp, final)
    return final


# ---- persisted state -------------------------------------------------------------------------------------------------

def state_dir(st: Settings) -> Path:
    return Path(st.home) / "adapter-services"


def read_state(st: Settings) -> dict:
    try:
        doc = json.loads((state_dir(st) / STATE_FILE).read_text(encoding="utf-8"))
        if isinstance(doc, dict) and doc.get("version") == VERSION:
            return doc
    except (OSError, ValueError):
        pass
    return {"version": VERSION, "fingerprint": None, "described_at": None, "panel": None, "feeds": [],
            "enabled": {}, "runs": {}}


def write_state(st: Settings, doc: dict) -> None:
    d = state_dir(st)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, d / STATE_FILE)


def _enabled(doc: dict, feed: dict) -> bool:
    v = doc.get("enabled", {}).get(feed["id"])
    return feed["enabled"] if v is None else bool(v)


def _local_now() -> datetime:
    return datetime.now().astimezone()


def day_feeds(st: Settings, now: datetime | None = None) -> list[dict]:
    """«Статус дня» (``daystatus.day``): per feed its last run and state, «нужен вход», the next run. No lines (they
    are the owner's), no secrets. Empty without the capability's state."""
    doc = read_state(st)
    now = now or _local_now()
    out = []
    for f in doc.get("feeds") or []:
        run = doc.get("runs", {}).get(f["id"], {})
        on = _enabled(doc, f)
        nd = next_due(f, run, now) if on and f.get("ready") else None
        out.append({"id": f["id"], "title": f["title"], "dataset": f["dataset"], "enabled": on, "ready": f.get("ready"),
                    "state": run.get("state"), "code": run.get("code"), "last_started": run.get("last_started"),
                    "last_finished": run.get("last_finished"), "last_ok": run.get("last_ok"),
                    "needs_login": bool(run.get("needs_login")), "failures": run.get("failures", 0),
                    "next_run": nd.isoformat() if nd else None})
    return out


# ---- the service ---------------------------------------------------------------------------------------------------

def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


class AdapterServices:
    """The worker and the state behind ``/api/v1/adapter-services`` (module doc). ``now``: the local clock of the
    schedules; ``today``: the Moscow business date of the recomputation (both for tests)."""

    def __init__(self, settings_loader: Callable[[], Settings], store_url: str, *,
                 now: Callable[[], datetime] | None = None, today: Callable[[], date] | None = None,
                 tick_s: float = TICK_S, timeouts: dict[str, float] | None = None) -> None:
        self._load, self._url = settings_loader, store_url
        self._now = now or _local_now
        self._today = today or (lambda: datetime.now(MSK).date())
        self._tick = tick_s
        self._timeouts = {**TIMEOUTS, **(timeouts or {})}
        self._lock = threading.Lock()
        self._wake, self._stop, self._cancel = threading.Event(), threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self._scheduling = False
        self._pending: dict | None = None          # the owner's operation waiting for the worker (holds f.* values)
        self._job: dict = {"state": "idle", "code": "idle"}  # the owner's last operation, as GET shows it
        self._manual: deque[tuple[str, str | None]] = deque()  # feeds asked for with «Запустить сейчас»
        self._running: dict | None = None            # what the worker runs now: {"kind", "op"|"feed", "since"}
        self._describe_wanted = False
        self._choose: set[str] = set()                # choose_action ids the last action offered
        self._choose_section: str | None = None       # ... and the panel section of the action that offered them
        self._avail: tuple[float, dict, Helper | None] | None = None
        self._checked = float("-inf")
        self._describe_failed: tuple[str | None, str, float] | None = None  # (fingerprint, code, monotonic time)
        self._newest_failing = False  # the last read of the newest positions failed (one warning per streak)
        self._owner_runs = 0  # the owner's operations run inside background work (``_owner_first``; worker thread)

    # ---- thread

    def start(self) -> None:
        """The scheduler (``desk serve``): feeds run by themselves from now on."""
        self._scheduling = True
        self._ensure_thread()
        self._wake.set()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="desk-adapter-services", daemon=True)
                self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._cancel.set()
        self._wake.set()
        t = self._thread
        if t is not None:
            t.join(timeout=6)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                busy = self._step()
            except Exception:
                log.exception("adapter services: worker step failed")
                busy = False
            if not busy:
                self._wake.wait(self._tick)

    def _step(self) -> bool:
        with self._lock:
            job, self._pending = self._pending, None
            manual = self._manual.popleft() if self._manual and job is None else None
        if job is not None:
            self._run_job(job)
            return True
        if manual is not None:
            self._feed_by_id(*manual, trigger="manual")
            return True
        if self._describe_wanted or (self._scheduling and time.monotonic() - self._checked >= RECHECK_S):
            self._checked = time.monotonic()
            self._describe_wanted = False
            self._background_describe()
            return False
        if self._scheduling:
            return self._scheduled() or self._history_step()
        return False

    # ---- availability and GET

    def _helper(self, st: Settings) -> Helper:
        h = helper(st)
        a = {"available": True, "code": "available", "name": h.handle.name, "version": h.handle.version,
             "fingerprint": h.handle.fingerprint}
        with self._lock:
            self._avail = (time.monotonic(), a, h)
        return h

    def availability(self) -> tuple[dict, Helper | None]:
        """{available, code, name, version, fingerprint} of the current adapter's helper, reused ``AVAILABILITY_S``."""
        with self._lock:
            if self._avail is not None and time.monotonic() - self._avail[0] < AVAILABILITY_S:
                return dict(self._avail[1]), self._avail[2]
        try:
            h = self._helper(self._load())
            with self._lock:
                return dict(self._avail[1]), h  # type: ignore[index]
        except ServiceError as e:
            a: dict = {"available": False, "code": e.code}
        except Exception:  # noqa: BLE001 - settings/plugin errors never expose external diagnostics
            a = {"available": False, "code": "settings_invalid"}
        with self._lock:
            self._avail = (time.monotonic(), a, None)
        return dict(a), None

    def overview(self) -> dict:
        """``GET /api/v1/adapter-services``: availability, the cached panel and feeds with their state, the owner's
        current job and what the worker runs. A missing or older-fingerprint cache asks the worker for a describe."""
        a, _ = self.availability()
        st = self._load()
        with self._lock:
            doc = read_state(st)
            job = json.loads(json.dumps(self._job))
            running = dict(self._running) if self._running else None
            queued = [f for f, _ in self._manual]
        if job.get("state") == "queued":  # what it waits for, as it is now (TDM#615)
            w = self._waiting_for(running, doc)
            job["code"] = "waiting_feed" if w else "queued"
            if w:
                job["waiting_for"] = w
        out = {**a, "panel": None, "feeds": [], "job": job, "running": running, "scheduler": self._scheduling}
        if not a["available"]:
            return out
        stale = doc.get("fingerprint") != a.get("fingerprint")
        failed = self._describe_failed
        failed = failed if failed is not None and failed[0] == a.get("fingerprint") else None
        if (doc.get("panel") is None or stale) and not running \
                and (failed is None or time.monotonic() - failed[2] >= TIMEOUTS["describe"]):
            self._describe_wanted = True  # once a minute at most while it fails
            self._ensure_thread()
            self._wake.set()
        now = self._now()
        out.update(panel=doc.get("panel"), described_at=doc.get("described_at"), stale=stale,
                   describe_error=failed[1] if failed else None,
                   feeds=[self._feed_view(doc, f, now, running, queued) for f in doc.get("feeds") or []])
        return out

    def _feed_view(self, doc: dict, f: dict, now: datetime, running: dict | None, queued: list[str]) -> dict:
        run = doc.get("runs", {}).get(f["id"], {})
        on = _enabled(doc, f)
        nd = next_due(f, run, now) if on and f.get("ready") else None
        last = {k: run.get(k) for k in ("last_started", "last_finished", "last_ok", "state", "code", "lines",
                                        "trigger", "date", "files", "dates", "recomputed", "recompute_failed",
                                        "failures", "retry_at", "needs_login")}
        return {**f, "enabled": on, "enabled_default": f["enabled"], "last": last,
                "next_run": _iso(nd), "running": bool(running and running.get("feed") == f["id"]),
                "waits_history": bool(running and running.get("kind") == "history" and f.get("history")),
                "queued": f["id"] in queued}

    # ---- the owner's requests

    def submit(self, op: str, fields: dict[str, list[str]]) -> dict:
        """``save``/``action``/``describe`` for the worker: checked against the cached panel, then queued (one at a
        time). ``fields`` is cleared (it may hold secrets)."""
        try:
            a, _ = self.availability()
            if not a["available"]:
                raise ServiceError(a["code"])
            with self._lock:
                doc = read_state(self._load())
                params = self._params(op, fields, doc.get("panel"))
                if self._pending is not None or (self._running or {}).get("kind") == "op":
                    params.clear()
                    raise ServiceError("services_busy")
                wait = self._waiting_for(self._running, doc)
                section, login = self._of_action(op, params, doc)
                self._job = {"id": uuid.uuid4().hex, "op": op, "action": params.get("action"), "state": "queued",
                             "code": "waiting_feed" if wait else "queued", "long": self._long(op, params, doc),
                             "login": login, "section": section, "queued_at": _iso(self._now())}
                self._pending = {"op": op, "params": params, "id": self._job["id"], "section": section}
                job = {**self._job, **({"waiting_for": wait} if wait else {})}  # GET says it as it is then
        finally:
            fields.clear()
        self._ensure_thread()
        self._wake.set()
        return job

    @staticmethod
    def _waiting_for(running: dict | None, doc: dict) -> dict | None:
        """The background work an owner's operation waits for: a feed run or a history request (its feed, title, start,
        phase, the date of a history request); None for none (or a describe: short)."""
        r = running or {}
        if r.get("kind") not in ("feed", "history"):
            return None
        fid = r.get("feed") or r.get("history_feed")
        title = next((f["title"] for f in doc.get("feeds") or [] if f["id"] == fid), fid)
        return {"kind": r["kind"], "feed": fid, "title": title, "since": r.get("since"), "phase": r.get("phase"),
                **({"date": r["date"]} if r.get("date") else {})}

    def _of_action(self, op: str, params: dict, doc: dict) -> tuple[str | None, bool]:
        """(the panel section of an action, whether it may open a login window). A pick list's action: the section of
        the action that offered it; the login: the action's ``login`` flag, else its id in ``LOGIN_ACTIONS``."""
        if op != "action":
            return None, False
        aid = params.get("action")
        section, decl = panel_action(doc.get("panel"), aid)
        if decl is None:
            return (self._choose_section if aid in self._choose else None), False
        return section, decl["login"] if isinstance(decl.get("login"), bool) else aid in LOGIN_ACTIONS

    @staticmethod
    def _long(op: str, params: dict, doc: dict) -> bool:
        if op != "action":
            return False
        return any(a["id"] == params.get("action") and a["long"] for s in (doc.get("panel") or {}).get("sections", [])
                   for a in s["actions"]) or params.get("action") not in panel_actions(doc.get("panel"))

    def _params(self, op: str, fields: dict[str, list[str]], panel: dict | None) -> dict[str, str]:
        if op not in ("save", "action", "describe"):
            raise ServiceError("invalid_request")
        if any(len(v) != 1 for v in fields.values()):
            raise ServiceError("invalid_request")
        keys = panel_keys(panel)
        p: dict[str, str] = {}
        for k, (v,) in fields.items():
            if k.startswith("f.") and op != "describe":
                key = k[2:]
                if key not in keys:
                    raise ServiceError("field_unknown")
                if keys[key] == "secret" and v == "":
                    continue  # an empty secret is unchanged: never sent
                p[k] = v
            elif k == "action" and op == "action":
                if not _ID.fullmatch(v) or (v not in panel_actions(panel) and v not in self._choose):
                    raise ServiceError("action_unknown")
                p[k] = v
            elif k == "arg" and op == "action":
                p[k] = v
            else:
                raise ServiceError("invalid_request")
        if op == "action" and "action" not in p:
            raise ServiceError("action_unknown")
        if op != "describe" and panel is None:
            raise ServiceError("panel_missing")
        if any(len(v) > helper_proc.MAX_VALUE for v in p.values()):
            raise ServiceError("invalid_request")
        return p

    def run_feed(self, feed_id: str, day: str | None = None) -> dict:
        """«Запустить сейчас»: the feed queued for the worker, whatever its switch and pause."""
        if day is not None:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError
            except (ValueError, TypeError):
                raise ServiceError("invalid_request") from None
        a, _ = self.availability()
        if not a["available"]:
            raise ServiceError(a["code"])
        with self._lock:
            doc = read_state(self._load())
            if not any(f["id"] == feed_id for f in doc.get("feeds") or []):
                raise ServiceError("feed_unknown")
            if feed_id in [f for f, _ in self._manual] or (self._running or {}).get("feed") == feed_id:
                raise ServiceError("feed_busy")
            self._manual.append((feed_id, day))
        self._ensure_thread()
        self._wake.set()
        return {"state": "queued", "code": "queued", "feed": feed_id}

    def _queue_feed(self, st: Settings, feed_id: str, day: str | None = None) -> str | None:
        """The feed queued as «Запустить сейчас» does, by the worker itself (a connect that got in, a run whose wait
        saw the owner change the adapter's config); its id, or None when the adapter describes no such feed. Not
        queued a second time while it already waits in the queue or is the work running now; once that run is over,
        it may be queued again."""
        with self._lock:
            if not any(f["id"] == feed_id for f in read_state(st).get("feeds") or []):
                return None
            if feed_id not in [f for f, _ in self._manual] and (self._running or {}).get("feed") != feed_id:
                self._manual.append((feed_id, day))
        self._wake.set()
        return feed_id

    def set_enabled(self, feed_id: str, on: bool) -> dict:
        st = self._load()
        with self._lock:
            doc = read_state(st)
            feed = next((f for f in doc.get("feeds") or [] if f["id"] == feed_id), None)
            if feed is None:
                raise ServiceError("feed_unknown")
            doc.setdefault("enabled", {})[feed_id] = bool(on)
            write_state(st, doc)
        self._wake.set()
        return {"state": "complete", "code": "saved", "feed": feed_id, "enabled": bool(on)}

    def cancel(self, target: str = "job") -> dict:
        """``job``: the owner's operation (dropped while queued, its process killed while running); ``feed``: the
        running feed's process (before its files are taken in)."""
        with self._lock:
            if target == "job" and self._pending is not None:
                self._pending["params"].clear()
                self._pending = None
                self._job.update(state="cancelled", code="cancelled", finished_at=_iso(self._now()))
                return dict(self._job)
            r = self._running or {}
            if (target == "job" and r.get("kind") == "op") or (target == "feed" and r.get("kind") in ("feed", "history")):
                if r.get("phase") == "ingesting":
                    raise ServiceError("cancel_after_commit_unavailable")
                self._cancel.set()
            return dict(self._job)

    # ---- running an operation

    def _acquire(self, on_wait: Callable[[], None]) -> bool:
        """``HELPER_LOCK``, waiting while an acquisition job holds it; False on shutdown or cancellation."""
        waited = False
        while not helper_proc.HELPER_LOCK.acquire(timeout=0.25):
            if not waited:
                on_wait()
                waited = True
            if self._stop.is_set() or self._cancel.is_set():
                return False
        return True

    def _call(self, st: Settings, hp: Helper, op: str, extra: dict[str, str | list[str]], *,
              out: Path | None = None) -> dict:
        fields: dict[str, str | list[str]] = {"op": op, **extra, "adapter_root": str(hp.root),
                                              "config_dir": hp.handle.config_dir or "",
                                              "inbox_dir": [str(d) for d in st.inbox_dirs],
                                              "state_dir": str(state_dir(st))}
        extra.clear()
        if out is not None:
            fields["output_dir"] = str(out)
        state_dir(st).mkdir(parents=True, exist_ok=True)
        try:
            result, rc = helper_proc.run(st.acquisition_python, hp.entry, fields, timeout=self._timeouts[op],
                                         cancel=self._cancel)
        except helper_proc.HelperFailure as f:
            raise ServiceError({"cancelled": "cancelled", "timeout": "helper_timeout", "request_invalid":
                                "invalid_request", "runtime_unavailable": "runtime_unavailable"}.get(
                f.code, "helper_response_invalid"), "cancelled" if f.code == "cancelled" else "error") from None
        finally:
            fields.clear()
        if rc != 0 and result.get("state") in ("complete", "partial"):
            raise ServiceError("helper_failed", "error")
        return result

    def _described(self, st: Settings, hp: Helper, result: dict, *, unpause: bool) -> dict:
        """A describe/save answer into the cache (the fingerprint read after the call: the helper may have written
        the adapter's config). ``unpause``: feeds it says are ready leave their login pause."""
        if result.get("state") != "complete" or not isinstance(result.get("code"), str):
            raise Invalid
        panel, feeds = clean_panel(result.get("panel")), clean_feeds(result.get("feeds"), hp.handle)
        try:
            fp = fingerprint(str(hp.root), hp.handle.config_dir)
        except Exception:  # noqa: BLE001
            fp = hp.handle.fingerprint
        if fp != hp.handle.fingerprint:
            scan.forget_adapters()  # the inbox and the autopilot load the adapter with its new config
        with self._lock:
            doc = read_state(st)
            doc.update(fingerprint=fp, described_at=_iso(self._now()), panel=panel, feeds=feeds)
            if unpause:
                for f in feeds:
                    run = doc.setdefault("runs", {}).get(f["id"])
                    if f["ready"] and run and run.get("needs_login"):
                        run["needs_login"] = False
                if any(f["ready"] and f.get("history") for f in feeds):
                    self._history_unwait(st)
            write_state(st, doc)
            self._avail = None  # the fingerprint may have changed
        return panel

    def _background_describe(self) -> None:
        try:
            st = self._load()
            hp = self._helper(st)
        except Exception:  # noqa: BLE001 - no capability: nothing to describe
            return
        doc = read_state(st)
        if doc.get("panel") is not None and doc.get("fingerprint") == hp.handle.fingerprint:
            return
        self._cancel.clear()
        if not self._acquire(lambda: None):
            return
        try:
            with self._lock:
                self._running = {"kind": "describe", "op": "describe", "since": _iso(self._now())}
            result = self._call(st, hp, "describe", {})
            if result.get("state") != "complete":
                raise ServiceError(result.get("code") if isinstance(result.get("code"), str)
                                   and _CODE.fullmatch(result["code"]) else "helper_response_invalid")
            self._described(st, hp, result, unpause=False)
            self._describe_failed = None
        except (ServiceError, Invalid) as e:
            code = getattr(e, "code", "helper_response_invalid")
            self._describe_failed = (hp.handle.fingerprint, code, time.monotonic())
            log.warning("adapter services: describe failed (%s)", code)
        finally:
            with self._lock:
                self._running = None
            helper_proc.HELPER_LOCK.release()

    def _job_update(self, jid: str, **kw: Any) -> None:
        with self._lock:
            if self._job.get("id") == jid:
                self._job.update(kw)

    def _owner_first(self, *, held: bool = False) -> bool:
        """The owner's queued operation now, from inside background work that runs no helper process at this point
        (TDM#615); True when one ran. ``held``: the caller holds ``HELPER_LOCK`` and keeps it. The caller's
        ``_running`` is restored, and a cancel of the owner's operation does not cancel the background work."""
        with self._lock:
            job, self._pending = self._pending, None
            saved = self._running
        if job is None:
            return False
        self._owner_runs += 1
        try:
            self._run_job(job, held=held)
        finally:
            with self._lock:
                self._running = saved
            if not self._stop.is_set():
                self._cancel.clear()
        return True

    def _run_job(self, job: dict, *, held: bool = False) -> None:
        """The owner's operation. An action's answer is published at once; the describe after it runs as a
        ``describe`` and only sets ``refreshed`` (module doc). ``held``: ``HELPER_LOCK`` is the caller's
        (``_owner_first``): neither acquired nor released here."""
        op, params, jid = job["op"], job["params"], job["id"]
        aid = params.get("action")  # ``params`` is cleared by the call
        self._cancel.clear()
        with self._lock:
            self._running = {"kind": "op", "op": op, "since": _iso(self._now()), "phase": "waiting"}
        try:
            st = self._load()
            hp = self._helper(st)
            if not held and not self._acquire(lambda: self._job_update(jid, state="waiting", code="waiting_acquisition")):
                raise ServiceError("cancelled", "cancelled")
        except Exception as e:  # noqa: BLE001
            params.clear()
            err = e if isinstance(e, ServiceError) else ServiceError("settings_invalid", "error")
            with self._lock:
                self._running = None
            self._job_update(jid, state=err.state, code=err.code, finished_at=_iso(self._now()))
            return
        try:
            with self._lock:
                self._running = {"kind": "op", "op": op, "since": _iso(self._now()), "phase": "running"}
            self._job_update(jid, state="running", code="running", started_at=_iso(self._now()))
            result = self._call(st, hp, op, params)
            if op in ("describe", "save"):
                if result.get("state") in ("blocked", "error"):
                    code, field = result.get("code"), result.get("field")
                    self._job_update(jid, state=result["state"],
                                     code=code if isinstance(code, str) and _CODE.fullmatch(code)
                                     else "helper_response_invalid", lines=_lines(result.get("lines")),
                                     field=field if isinstance(field, str) and _ID.fullmatch(field) else None)
                else:
                    if op == "save" and not isinstance(result.get("feeds"), list):
                        result = self._call(st, self._helper(st), "describe", {})  # a save without the feeds
                    self._described(st, hp, result, unpause=True)
                    self._job_update(jid, state="complete", code="saved" if op == "save" else "described")
            else:
                r = clean_action(result)
                with self._lock:
                    self._choose = {r["choose_action"]} if r.get("choose_action") else set()
                    self._choose_section = job.get("section") if r.get("choose_action") else None
                    if r.get("panel") is not None:
                        doc = read_state(st)
                        doc["panel"] = r["panel"]
                        write_state(st, doc)
                    # the answer at once (TDM#615): the describe below neither delays it nor keeps the job running;
                    # the owner's next operation may be queued meanwhile
                    self._running = {"kind": "describe", "op": "describe", "since": _iso(self._now()),
                                     "phase": "refreshing", "after": jid}
                    if self._job.get("id") == jid:
                        self._job.update({k: v for k, v in r.items() if k != "panel"}, refreshed=None,
                                         finished_at=_iso(self._now()))
                # action results carry no feeds, and their readiness may have changed (a login): describe again
                try:
                    self._described(st, self._helper(st), self._call(st, hp, "describe", {}), unpause=True)
                    refreshed = True
                except (ServiceError, Invalid):
                    refreshed = False
                except Exception:
                    log.exception("adapter services: the describe after an action failed")
                    refreshed = False
                connected = aid == CONNECT_ACTION and (r["code"] in CONNECTED or r["state"] == "partial")
                self._job_update(jid, refreshed=refreshed, feed_queued=self._queue_feed(st, CONNECT_FEED) if connected
                                 else None)
        except ServiceError as e:
            self._job_update(jid, state=e.state, code=e.code)
        except Invalid:
            self._job_update(jid, state="error", code="helper_response_invalid")
        except Exception:
            log.exception("adapter services: %s failed", op)
            self._job_update(jid, state="error", code="operation_failed")
        finally:
            params.clear()
            with self._lock:
                self._running = None
                if self._job.get("id") == jid and not self._job.get("finished_at"):
                    self._job["finished_at"] = _iso(self._now())
            if not held:
                helper_proc.HELPER_LOCK.release()

    # ---- feeds

    def _newest_positions(self) -> date | None:
        # A bounded connect: with no store reachable this answers "none" and the feed goes on (and can be stopped). It is
        # the worker's thread, not a screen (the sign of adapter_routes keeps 3 s): a store that is only slow, as in the
        # minute after a start, must not read as "no positions", so the wait is the 5 s of the store's other readers.
        try:
            with store.connect(self._url, connect_timeout=NEWEST_CONNECT_S) as con:
                newest = con.execute("SELECT max(valid_date) FROM position_set").fetchone()[0]
        except Exception as e:  # noqa: BLE001 - no store: no after_positions trigger this tick
            if not self._newest_failing:
                self._newest_failing = True
                log.warning("adapter services: the newest positions could not be read (%s); the feeds that wait for "
                            "new positions do not start until the store answers", type(e).__name__)
            return None
        self._newest_failing = False
        return newest

    def _scheduled(self) -> bool:
        st = self._load()
        now = self._now()
        doc = read_state(st)
        feeds = [f for f in doc.get("feeds") or [] if _enabled(doc, f) and f.get("ready")]
        if not feeds:
            return False
        newest = self._newest_positions() if any(f["after_positions"] for f in feeds) else None
        due: list[tuple[datetime, dict, str, str | None]] = []
        changed: list[str] = []
        for f in feeds:
            run = doc.setdefault("runs", {}).setdefault(f["id"], {})
            if f["after_positions"] and newest is not None and not run.get("needs_login"):
                seen = run.get("positions_seen")
                if seen is None:
                    changed.append(f["id"])  # the first sight only records it
                elif newest.isoformat() > seen:
                    due.append((now, f, "after_positions", newest.isoformat()))
                    continue
            nd = next_due(f, run, now)
            if nd is not None and nd <= now:
                retry = bool(run.get("failures"))
                due.append((nd, f, "retry" if retry else "schedule", run.get("date") if retry else None))
        for fid in changed:
            self._record(st, fid, positions_seen=newest.isoformat())  # type: ignore[union-attr]
        if not due:
            return False
        due.sort(key=lambda x: x[0])
        _, f, trigger, day = due[0]
        self._feed(st, f, trigger, day, newest if trigger == "after_positions" else None)
        return True

    def _feed_by_id(self, feed_id: str, day: str | None, *, trigger: str) -> None:
        st = self._load()
        f = next((x for x in read_state(st).get("feeds") or [] if x["id"] == feed_id), None)
        if f is not None:
            self._feed(st, f, trigger, day, None)

    def _record(self, st: Settings, feed_id: str, **kw: Any) -> dict:
        with self._lock:
            doc = read_state(st)
            run = doc.setdefault("runs", {}).setdefault(feed_id, {})
            run.update(kw)
            write_state(st, doc)
            return dict(run)

    def _feed(self, st: Settings, f: dict, trigger: str, day: str | None, newest: date | None) -> None:
        asked = day  # the date asked for: a run queued again (``RECONFIGURED``) asks the same
        if f.get("trades_file"):
            self._ensure_list(st, f, day)
        self._owner_first()  # the owner's operation asked for meanwhile (the list's feed ran first) goes first
        rid = uuid.uuid4().hex
        started = self._now()
        seen = {"positions_seen": newest.isoformat()} if newest is not None else {}
        # recorded before the call: a restart in the middle does not fetch again at once
        prev = self._record(st, f["id"], last_started=_iso(started), trigger=trigger, date=day)
        outcome: dict[str, Any]
        books: list[str] = []
        notes: list[str] = []
        if f.get("books"):  # a feed of the owner's books (TDM#557): none to ask for, nothing to call
            try:
                with store.connect(self._url) as con:
                    books, notes = owner_books(con, date.fromisoformat(day) if day else self._today())
            except Exception:
                log.exception("adapter services: books of feed %s not read", f["id"])
                self._finish(st, f, prev, {"state": "error", "code": "operation_failed", "lines": []}, started)
                return
            if not books:
                if seen:  # seen all the same: a feed after the positions is not due again at every step
                    self._record(st, f["id"], **seen)
                self._finish(st, f, prev, {"state": "blocked", "code": "no_books", "lines": [
                    "Нет книг владельца с позициями: загрузите позиции и отметьте книги активными."]}, started)
                return
        if seen:
            self._record(st, f["id"], **seen)
        self._cancel.clear()
        with self._lock:
            self._running = {"kind": "feed", "feed": f["id"], "since": _iso(started), "phase": "waiting"}
        try:
            hp = self._helper(st)
            if not self._acquire(lambda: None):
                raise ServiceError("cancelled", "cancelled")
        except Exception as e:  # noqa: BLE001
            err = e if isinstance(e, ServiceError) else ServiceError("settings_invalid", "error")
            with self._lock:
                self._running = None
            self._finish(st, f, prev, {"state": err.state, "code": err.code, "lines": []}, started)
            return
        try:
            with self._lock:
                self._running = {"kind": "feed", "feed": f["id"], "since": _iso(started), "phase": "fetching"}
            out = state_dir(st) / "out" / rid
            out.mkdir(parents=True)
            if day is None and f["dataset"] in ("trade_terms", "trade_list"):
                # the business date of the latest positions: the adapter has no holiday calendar of its own
                newest_pos = self._newest_positions()
                day = newest_pos.isoformat() if newest_pos else None
                if day:
                    self._record(st, f["id"], date=day)
            drained = self._drain_pending(st, hp, f, prev.get("pending"))
            extra: dict[str, str | list[str]] = {"feed": f["id"], **({"date": day} if day else {}),
                                                 **({"book": books} if books else {})}
            plan, note = self._terms_plan(st, f, rid, day) if f.get("trades_file") else (None, None)
            if plan is not None:
                extra["trades_file"] = str(plan[0])
            if books:  # the whole request within the protocol's limits before the call, the books cut with a note
                self._fit_books(st, hp, out, extra, books, notes)
            r = clean_feed_result(self._call(st, hp, "feed", extra, out=out))
            outcome = {k: r[k] for k in ("state", "code", "lines", "dates")}
            if notes:
                outcome["lines"] = [*notes, *outcome["lines"]]
            if plan is not None:
                outcome["lines"] = [plan_line(plan[1]), *outcome["lines"]]
            elif note:
                outcome["lines"] = [note, *outcome["lines"]]
            if r["state"] in ("complete", "partial") and r["files"]:
                if not f.get("source_declared", True):
                    raise ServiceError("feed_source_invalid", "error")  # files no declared source would read
                try:
                    outcome.update(self._ingest(st, hp, f, out, r))
                except Invalid:
                    raise
                except BaseException:
                    # not taken in (the cycle lock, a store failure, a cancel): kept for the next run, since the
                    # adapter may answer ``no_change`` then and never produce these files again
                    self._record(st, f["id"], pending={"dir": rid, "files": r["files"], "state": r["state"],
                                                       "code": r["code"]})
                    raise
            elif drained is not None:
                outcome.update({k: drained[k] for k in ("files", "accepted_units", "recomputed", "recompute_failed")})
        except ServiceError as e:
            outcome = {"state": e.state, "code": e.code, "lines": []}
        except Invalid:
            outcome = {"state": "error", "code": "helper_response_invalid", "lines": []}
        except Exception:
            log.exception("adapter services: feed %s failed", f["id"])
            outcome = {"state": "error", "code": "operation_failed", "lines": []}
        finally:
            with self._lock:
                self._running = None
            helper_proc.HELPER_LOCK.release()
            self._prune(st)
        if outcome["code"] == RECONFIGURED:  # queued before the outcome is published: GET never shows it as over
            self._queue_feed(st, f["id"], asked)
        self._finish(st, f, prev, outcome, started)

    @staticmethod
    def _fit_books(st: Settings, hp: Helper, out: Path, extra: dict, books: list[str], notes: list[str]) -> None:
        """``extra["book"]``: the books within the protocol's limits of one request, cut with a note in ``notes``."""
        asked = len(books)
        base = len(st.inbox_dirs) + 8
        books = books[:max(0, helper_proc.MAX_FIELDS - base)]
        while books:
            try:
                helper_proc.encode({"op": "feed", **extra, "book": books, "adapter_root": str(hp.root),
                                    "config_dir": hp.handle.config_dir or "", "state_dir": str(state_dir(st)),
                                    "output_dir": str(out), "inbox_dir": [str(d) for d in st.inbox_dirs]})
                break
            except helper_proc.HelperFailure:
                books = books[:-max(1, len(books) // 10)]
        extra["book"] = books
        if len(books) < asked:
            notes.append(f"В запрос вошло книг: {len(books)} из {asked} (предел размера запроса).")
        if not books:
            raise ServiceError("invalid_request", "error")

    def _plan_of(self, day: str | None) -> tuple[date | None, dict | None]:
        on = date.fromisoformat(day) if day else self._newest_positions()
        if on is None:
            return None, None
        with store.connect(self._url) as con:
            books, _ = owner_books(con, on)
            return on, terms_plan.plan(con, on, books) if books else None

    def _ensure_list(self, st: Settings, f: dict, day: str | None) -> None:
        """A ``trades_file`` feed needs the list of its date: when the store has none of that day, the enabled and
        ready feed of the dataset ``trade_list`` runs first, in the same turn (module doc)."""
        try:
            on, plan = self._plan_of(day)
            if on is None or (plan is not None and not plan.get("stale")):
                return
            doc = read_state(st)
            tl = next((x for x in doc.get("feeds") or [] if x.get("dataset") == "trade_list" and x["id"] != f["id"]
                       and x.get("ready") and _enabled(doc, x)), None)
            if tl is not None:
                self._feed(st, tl, "dependency", on.isoformat(), None)
        except Exception:
            log.exception("adapter services: list for feed %s not ensured", f["id"])

    def _terms_plan(self, st: Settings, f: dict, rid: str, day: str | None
                    ) -> tuple[tuple[Path, dict] | None, str | None]:
        """(the request file of a ``trades_file`` feed and the plan's counts or None, a line for the outcome or None).
        No file (no list of the date, too long a list, any failure): the feed runs as before, the whole report, and the
        line says why. The counts are kept in the feed's state."""
        try:
            on, plan = self._plan_of(day)
            if on is None:
                return None, None
            if plan is None or plan.get("stale"):
                return None, (f"Список сделок за {on:%d.%m.%Y} не получен — условия запрошены полным проходом.")
            if len(plan["trades"]) > MAX_REQUEST_TRADES:
                return None, f"Заявка не построена: в списке больше {MAX_REQUEST_TRADES} сделок, полный проход."
            path = write_request(st, rid, plan)
        except Exception:
            log.exception("adapter services: terms plan of feed %s not made", f["id"])
            return None, "Заявка не построена: ошибка при сравнении со списком, полный проход."
        self._record(st, f["id"], plan=plan["counts"])
        return (path, plan["counts"]), None

    def _finish(self, st: Settings, f: dict, prev: dict, outcome: dict, started: datetime) -> None:
        now = self._now()
        state, code = outcome["state"], outcome["code"]
        rec: dict[str, Any] = {"last_finished": _iso(now), "state": state, "code": code,
                               "lines": outcome.get("lines", [])[:50], "dates": outcome.get("dates", []),
                               "files": outcome.get("files", []), "recomputed": outcome.get("recomputed", []),
                               "recompute_failed": outcome.get("recompute_failed", [])}
        if state == "blocked" and code == "login_required":
            rec.update(needs_login=True, failures=0, retry_at=None)
        elif state == "blocked" and code == "no_books":
            rec.update(needs_login=False, failures=0, retry_at=None)  # nothing to ask for yet: the next step tries again
        elif state in ("complete", "partial", "skipped"):
            rec.update(needs_login=False, failures=0, retry_at=None, last_ok=_iso(now))
            if f.get("history"):
                with self._lock:   # a feed run got in: the history of its trades need not wait for a login
                    self._history_unwait(st)
        elif state == "cancelled" and code == RECONFIGURED:
            pass  # not a failure and no retry step: the same feed is queued again at once (``_feed``)
        elif state == "cancelled":
            n = int(prev.get("failures") or 0)  # a cancelled retry waits for its next step, never again at once
            rec.update(failures=n, retry_at=_iso(now + backoff(n)) if n else None)
        else:
            n = int(prev.get("failures") or 0) + 1
            rec.update(failures=n, retry_at=_iso(now + backoff(n)))
        self._record(st, f["id"], **rec)
        log.info("adapter services: feed %s %s/%s", f["id"], state, code)

    def _wait_cycle_lock(self, st: Settings) -> CycleLock | None:
        """The cycle lock, waited for ``LOCK_WAIT_S`` (None then, or on shutdown). Meanwhile no helper process runs: the
        owner's operation asked for goes first (``HELPER_LOCK`` kept, its time not counted, ``_owner_runs`` counts
        it), and «Остановить» stops the wait (``cancelled``: the files stay pending)."""
        lock = CycleLock(st.home)
        until = time.monotonic() + LOCK_WAIT_S
        while True:
            if lock.acquire():
                return lock
            if self._cancel.is_set():
                raise ServiceError("cancelled", "cancelled")
            t0 = time.monotonic()
            if self._owner_first(held=True):
                until += time.monotonic() - t0
                continue
            if time.monotonic() >= until or self._stop.wait(1.0):
                return None

    @staticmethod
    def _try_cycle_lock(st: Settings) -> CycleLock | None:
        """One try of the cycle lock, no waiting (the history of trades: TDM#569)."""
        lock = CycleLock(st.home)
        return lock if lock.acquire() else None

    def _ingest(self, st: Settings, hp: Helper, f: dict, out: Path, r: dict, *, recompute_runs: bool = True,
                wait: bool = True) -> dict:
        """The produced files through the scanner as the feed's source, then the affected runs (module doc).
        ``recompute_runs`` False and ``wait`` False: the history of trades (one try of the cycle lock, no runs after it)."""
        root = out.resolve()
        paths = []
        for x in r["files"]:
            p = (out / x["file"]).resolve()
            if p.parent != root or not p.is_file() or scan._sha256(str(p)) != x["sha256"]:
                raise Invalid
            paths.append(p)
        if self._cancel.is_set():
            raise ServiceError("cancelled", "cancelled")
        runs = self._owner_runs
        lock = self._wait_cycle_lock(st) if wait else self._try_cycle_lock(st)
        owner = self._owner_runs != runs  # the owner's operation ran in the wait (it may have changed the config)
        if lock is None:
            raise ServiceError("calculation_busy", "error")
        try:
            with self._lock:
                if self._running:
                    self._running["phase"] = "ingesting"
            if fingerprint(str(hp.root), hp.handle.config_dir) != hp.handle.fingerprint:
                # the owner's operation in the wait changed the config (a connect, a save): expected, run again
                raise ServiceError(RECONFIGURED, "cancelled") if owner else ServiceError("adapter_changed", "error")
            files, dates, accepted = [], set(), 0
            with store.connect(self._url) as con:
                for p in paths:
                    res = scan.ingest_feed(con, st, p, source=f["source"], handle=hp.handle)
                    if res.busy:
                        raise ServiceError("ingestion_busy", "error")
                    sha = scan._sha256(str(p))
                    row = con.execute("SELECT status_id, status, source FROM inbox_file_status WHERE sha256 = %s"
                                      " ORDER BY status_id DESC LIMIT 1", (sha,)).fetchone()
                    status = "duplicate" if res.copies.get("duplicate") else row[1] if row else "unknown"
                    if row is not None and row[2] == f["source"] and row[1] in OK_FILE:
                        units = con.execute("SELECT valid_date FROM inbox_unit WHERE status_id = %s"
                                            " AND status = 'accepted'", (row[0],)).fetchall()
                        dates |= {d for (d,) in units if d is not None}
                        accepted += len(units) if status != "duplicate" else 0
                    files.append({"file": p.name, "status": status})
            rec = recompute.after_inputs(st, self._url, dates, self._today()) if dates and recompute_runs \
                else {"runs": [], "failed": []}
        finally:
            lock.release()
        bad = [x for x in files if x["status"] not in OK_FILE]
        res_state = r["state"] if not bad else "partial" if len(bad) < len(files) else "error"
        return {"state": res_state, "code": r["code"] if res_state != "error" else "files_not_accepted",
                "files": files, "accepted_units": accepted, "recomputed": rec["runs"],
                "recompute_failed": rec["failed"]}

    def _drain_pending(self, st: Settings, hp: Helper, f: dict, pending: Any) -> dict | None:
        """The files an earlier run produced but could not take in (``_feed``), taken in now, before the helper is
        asked again; None when there are none. Gone or changed files are dropped; a failure again keeps them."""
        if not isinstance(pending, dict) or not isinstance(pending.get("dir"), str) \
                or not _RID.fullmatch(pending["dir"]) or not isinstance(pending.get("files"), list):
            if pending is not None:
                self._record(st, f["id"], pending=None)
            return None
        folder = state_dir(st) / "out" / pending["dir"]
        try:
            drained = self._ingest(st, hp, f, folder, {"files": pending["files"], "state": pending.get("state"),
                                                       "code": pending.get("code")}) if folder.is_dir() else None
        except Invalid:
            drained = None
        self._record(st, f["id"], pending=None)
        with self._lock:
            if self._running:
                self._running["phase"] = "fetching"
        return drained

    # ---- the trades of past dates (TDM#569, ``desk.trades_history``)

    @staticmethod
    def _history_feed(doc: dict) -> dict | None:
        """The feed that answers for past dates: the dataset ``trades``, the owner's books, ``history``."""
        return next((f for f in doc.get("feeds") or [] if f.get("history") and f.get("books")
                     and f.get("dataset") == "trades"), None)

    def _hist_edit(self, st: Settings, fn: Callable[[dict], Any]) -> Any:
        """The history file changed under the service's lock, atomically (the worker thread and the routes write)."""
        with self._lock:
            doc = trades_history.read(st)
            r = fn(doc)
            trades_history.write(st, doc)
            return r

    def _history_unwait(self, st: Settings) -> None:
        """The caller holds ``self._lock``."""
        doc = trades_history.read(st)
        if doc.get("wait"):
            doc["wait"] = None
            trades_history.write(st, doc)

    def _feed_due_soon(self, doc: dict, now: datetime, spent: float | None) -> bool:
        """A feed is (or will be) due within the time the last request of the history took (at least 30 s): it would wait
        for the whole request, so the history waits for it."""
        need = timedelta(seconds=max(float(spent or 0.0), 30.0))
        for f in doc.get("feeds") or []:
            if _enabled(doc, f) and f.get("ready"):
                nd = next_due(f, doc.get("runs", {}).get(f["id"], {}), now)
                if nd is not None and nd - now < need:
                    return True
        return False

    def _history_step(self) -> bool:
        """One step of the worker of the trades of past dates (module doc): the files waiting for the cycle lock first,
        else one date, if the limits and the hours allow. True: it did something that asks for the next step at once."""
        st = self._load()
        sdoc = read_state(st)
        f = self._history_feed(sdoc)
        h = trades_history.read(st)
        if f is None or not h["enabled"] or not _enabled(sdoc, f) or not f.get("ready") or sdoc.get("runs", {}).get(f["id"], {}).get("needs_login"):
            return False
        now = self._now()
        if h.get("pending"):
            return self._history_drain(st, f, h)
        if trades_history.gate(h, now) is not None or self._feed_due_soon(sdoc, now, h.get("last_duration_s")):
            return False
        fp = sdoc.get("fingerprint")
        pick: tuple[str, list[str], list[str]] | None = None
        no_books: dict[str, str] = {}
        try:
            with store.connect(self._url) as con:
                q = trades_history.queue(con, st, h, now, f.get("history_from"), fp)
                for d in q["order"][:20]:
                    books, notes = owner_books(con, date.fromisoformat(d))
                    if books:
                        pick = (d, books, notes)
                        break
                    no_books[d] = trades_history.sets_signature(con, d)
        except Exception:
            log.exception("adapter services: the history of trades not planned")
            return False

        def marks(doc: dict) -> None:
            for d in q["covered"]:
                trades_history.apply_mark(doc, d, "fetched", "covered", now, fingerprint=fp)
            for d, sig in no_books.items():
                trades_history.apply_mark(doc, d, "no_books", "no_books", now, fingerprint=fp, sets=sig)
            doc["priority"] = [d for d in doc.get("priority") or [] if d not in doc["marks"]]

        if q["covered"] or no_books:
            self._hist_edit(st, marks)
        return self._history_fetch(st, f, *pick, fp) if pick else False

    def _history_fetch(self, st: Settings, f: dict, day: str, books: list[str], notes: list[str],
                       fp: str | None) -> bool:
        """The feed asked for one past date: the answer checked (the date is the asked one), its files taken in with one
        non-blocking try of the cycle lock, no run made after it, the date marked (``_history_finish``)."""
        try:
            hp = self._helper(st)
        except Exception as e:  # noqa: BLE001 - no helper now: the next step tries again
            log.info("adapter services: history waits (%s)", getattr(e, "code", type(e).__name__))
            return False
        self._cancel.clear()
        if not helper_proc.HELPER_LOCK.acquire(timeout=0.25):   # an acquisition job or an operation: not waited for
            return False
        started = self._now()
        rid = uuid.uuid4().hex
        self._hist_edit(st, lambda h: trades_history.count_call(h, started))   # counted before the call: a restart too
        with self._lock:
            self._running = {"kind": "history", "history_feed": f["id"], "date": day, "since": _iso(started),
                             "phase": "fetching"}
        r: dict = {"state": "error", "code": "operation_failed", "files": [], "dates": []}
        ing: dict | None = None
        pending: dict | None = None
        try:
            out = state_dir(st) / "out" / rid
            out.mkdir(parents=True)
            extra: dict[str, str | list[str]] = {"feed": f["id"], "date": day}
            self._fit_books(st, hp, out, extra, books, notes)
            r = clean_feed_result(self._call(st, hp, "feed", extra, out=out))
            if r["dates"] and r["dates"] != [day]:
                raise Invalid   # the answer is for another date: not marked
            if r["files"] and r["state"] in ("complete", "partial"):
                if not f.get("source_declared", True):
                    raise ServiceError("feed_source_invalid", "error")
                try:
                    ing = self._ingest(st, hp, f, out, r, recompute_runs=False, wait=False)
                except Invalid:
                    raise
                except BaseException:
                    pending = {"dir": rid, "files": r["files"], "state": r["state"], "code": r["code"], "date": day}
                    raise
        except ServiceError as e:
            r = {**r, "code": e.code} if pending is not None else {"state": e.state, "code": e.code, "files": [],
                                                                    "dates": []}
        except Invalid:
            r = {"state": "error", "code": "helper_response_invalid", "files": [], "dates": []}
        except Exception:
            log.exception("adapter services: history of %s failed", day)
            r = {"state": "error", "code": "operation_failed", "files": [], "dates": []}
        finally:
            with self._lock:
                self._running = None
            helper_proc.HELPER_LOCK.release()
            self._prune(st)
        self._history_finish(st, day, r, ing, fp, started, pending=pending)
        return True

    def _history_drain(self, st: Settings, f: dict, h: dict) -> bool:
        """The files an earlier step could not take in (the cycle lock was busy), tried again with one non-blocking try;
        no other date is asked before. Gone or changed files: dropped, the date is asked again."""
        p = h["pending"]
        if not isinstance(p, dict) or not _RID.fullmatch(str(p.get("dir"))) or not isinstance(p.get("files"), list) \
                or not isinstance(p.get("date"), str) or not (state_dir(st) / "out" / p["dir"]).is_dir():
            self._hist_edit(st, lambda d: d.update(pending=None))
            return False
        folder = state_dir(st) / "out" / p["dir"]
        try:
            hp = self._helper(st)
        except Exception:  # noqa: BLE001
            return False
        day, started = p["date"], self._now()
        self._cancel.clear()   # a cancel that came after the request must not hold the files for ever
        if not helper_proc.HELPER_LOCK.acquire(timeout=0.25):   # as every operation; busy: the next step
            return False
        with self._lock:
            self._running = {"kind": "history", "history_feed": f["id"], "date": day, "since": _iso(started),
                             "phase": "ingesting"}
        r = {"state": p.get("state"), "code": p.get("code"), "files": p["files"], "dates": [day]}
        try:
            ing = self._ingest(st, hp, f, folder, r, recompute_runs=False, wait=False)
        except ServiceError:
            return False   # still busy (or a cancel): kept
        except Invalid:
            self._hist_edit(st, lambda d: d.update(pending=None))
            return False
        except Exception:
            log.exception("adapter services: pending history files of %s not taken in", day)
            return False
        finally:
            with self._lock:
                self._running = None
            helper_proc.HELPER_LOCK.release()
        self._history_finish(st, day, r, ing, read_state(st).get("fingerprint"), started, drained=True)
        return True

    def _history_finish(self, st: Settings, day: str, r: dict, ing: dict | None, fp: str | None, started: datetime, *,
                        pending: dict | None = None, drained: bool = False) -> None:
        """The answer into the date's mark or, with no mark, into the worker's wait / retry (``trades_history`` doc)."""
        now = self._now()
        sha = {x.get("file"): x.get("sha256") for x in r.get("files") or [] if isinstance(x, dict)}
        # the mark keeps the sha256 of each file taken in (the helper's own, checked by _ingest): the screens tell the
        # files of this fetch from the feed of the day by it (trades.origin_of, TDM#585)
        files = [{**x, "sha256": x.get("sha256") or sha.get(x.get("file"))} for x in (ing or {}).get("files") or []]
        bad = [x for x in files if x["status"] not in OK_FILE]
        state, code = str(r.get("state")), str(r.get("code"))
        units = int((ing or {}).get("accepted_units") or 0)
        mark, mcode = trades_history.mark_for(
            state, code, files_ok=not bad, units=units, duplicate=any(x["status"] == "duplicate" for x in files),
            had_files=bool(r.get("files")))
        if pending is not None:
            mark, mcode = None, code   # the files wait for the cycle lock: nothing is known of the date yet

        def edit(h: dict) -> None:
            h["last_duration_s"] = round((now - started).total_seconds(), 1)
            if drained:
                h["pending"] = None
            if pending is not None:
                h["pending"] = pending
                h["last"] = {"date": day, "state": "pending", "code": mcode, "at": now.isoformat()}
                return
            if mark is not None:
                trades_history.apply_mark(h, day, mark, mcode, now, fingerprint=fp, files=files, units=units)
                h.update(failures=0, retry_at=None, wait=None,
                         last={"date": day, "state": h["marks"][day]["state"], "code": mcode, "at": now.isoformat()})
                h["priority"] = [d for d in h.get("priority") or [] if d != day]
            elif state == "blocked" and code in trades_history.WAITS:
                h["wait"] = {"code": code, "at": now.isoformat()}
                h["last"] = {"date": day, "state": "blocked", "code": code, "at": now.isoformat()}
            else:
                n = int(h.get("failures") or 0) + (0 if state == "cancelled" else 1)
                h.update(failures=n, retry_at=_iso(now + backoff(n)) if n else None,
                         last={"date": day, "state": state, "code": code, "at": now.isoformat()})

        self._hist_edit(st, edit)
        log.info("adapter services: history %s %s/%s", day, mark or state, mcode)

    # ---- the history of trades for the routes

    def history(self) -> dict:
        """``GET /api/v1/adapter-services/history`` (``trades_history.summary``)."""
        a, _ = self.availability()
        st = self._load()
        with self._lock:
            sdoc, h = read_state(st), trades_history.read(st)
            running = dict(self._running) if self._running else None
        f = self._history_feed(sdoc) if a["available"] else None
        fs = sdoc.get("runs", {}).get(f["id"]) if f else None
        now = self._now()
        try:
            with store.connect(self._url) as con:
                out = trades_history.summary(con, st, h, now=now, feed=f, fingerprint=sdoc.get("fingerprint"),
                                             running=running, feed_state=fs)
        except Exception:
            log.exception("adapter services: the history summary without the store")
            out = trades_history.summary(None, st, h, now=now, feed=f, fingerprint=sdoc.get("fingerprint"),
                                         running=running, feed_state=fs)
            out["store"] = False
        if not a["available"]:
            out["code"] = a["code"]
        return out

    def history_set(self, enabled: bool | None, from_: str | None, when: str | None) -> dict:
        """Turn the worker on or off, set the first date (never before ``backfill_from``) and the hours."""
        st = self._load()
        today = self._now().date()
        d: date | None = None
        if from_ is not None:
            try:
                d = date.fromisoformat(from_)
                if d.isoformat() != from_ or d >= today:
                    raise ValueError
            except ValueError:
                raise ServiceError("invalid_request") from None
        if when is not None and when not in trades_history.WHEN:
            raise ServiceError("invalid_request")

        def edit(h: dict) -> None:
            if when is not None:
                h["when"] = when
            if d is not None:
                h["from"] = max(d, backfill.start_date(st)).isoformat()
            if enabled is not None:
                h["enabled"] = enabled
                if enabled:
                    h.update(failures=0, retry_at=None)
                    h["from"] = h.get("from") or trades_history.default_from(st, today).isoformat()

        self._hist_edit(st, edit)
        self._wake.set()
        return {"state": "complete", "code": "saved"}

    def history_refetch(self, from_: str, to: str, confirm: bool) -> dict:
        """«Перезапросить период»: the marks of the dates of the period (but «no books») dropped and the dates put first.
        Without ``confirm`` only the number of dates is told."""
        st = self._load()
        today = self._now().date()
        try:
            lo, hi = date.fromisoformat(from_), date.fromisoformat(to)
            if lo.isoformat() != from_ or hi.isoformat() != to or lo > hi or hi >= today:
                raise ValueError
        except ValueError:
            raise ServiceError("invalid_request") from None
        with self._lock:
            sdoc, h = read_state(st), trades_history.read(st)
        f = self._history_feed(sdoc)
        if f is None:
            raise ServiceError("history_unsupported")
        try:
            with store.connect(self._url) as con:
                cands = trades_history.candidates(con, st, h, self._now(), f.get("history_from"))
        except Exception:
            log.exception("adapter services: dates to ask again not read")
            raise ServiceError("operation_failed", "error") from None
        dates = trades_history.refetch_dates(st, cands, h, lo, hi)
        if not confirm:
            return {"state": "confirm", "code": "confirm", "dates": len(dates)}

        def edit(doc: dict) -> None:
            for x in dates:
                doc["marks"].pop(x, None)
            doc["priority"] = sorted({*dates, *(doc.get("priority") or [])}, reverse=True)

        self._hist_edit(st, edit)
        self._wake.set()
        return {"state": "complete", "code": "queued", "dates": len(dates)}

    @staticmethod
    def _prune(st: Settings) -> None:
        base = state_dir(st) / "out"
        keep = {r.get("pending", {}).get("dir") for r in read_state(st).get("runs", {}).values()
                if isinstance(r, dict) and isinstance(r.get("pending"), dict)}
        hp_ = trades_history.read(st).get("pending")
        if isinstance(hp_, dict):   # the files of the history of trades waiting for the cycle lock (TDM#569)
            keep.add(hp_.get("dir"))
        try:
            dirs = sorted((d for d in base.iterdir() if d.is_dir() and d.name not in keep),
                          key=lambda d: d.stat().st_mtime)
        except OSError:
            return
        for d in dirs[:-KEEP_OUT] if len(dirs) > KEEP_OUT else []:
            shutil.rmtree(d, ignore_errors=True)
        try:
            reqs = sorted((p for p in (state_dir(st) / "requests").iterdir() if p.is_file()),
                          key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for p in reqs[:-KEEP_REQUESTS]:
            p.unlink(missing_ok=True)
