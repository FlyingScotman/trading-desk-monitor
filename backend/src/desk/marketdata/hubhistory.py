"""Deep market history from the hub's history API (hub 0.2.0, ``/v1/history/*``; R24): bond EOD, the KBD, the curve
quote sets (RUONIA OIS, key-rate swaps, CNY curves: whatever ``/v1/history/index`` lists), fixings and FX rates of
past dates in a few columnar calls, into the store's ``md_hub_*`` tables (migration 0011). The snapshot path
(``desk.marketdata.history``: one bundle per date) stays the fallback: a hub before 0.2.0 answers 404 on the index
(``hub без API истории (версия < 0.2.0)``), a token without scope ``read`` 403 (``токен без права read``); the reason
is kept in ``md_hub_state`` and shown by the settings page, ``desk hub-check``, ``desk history status`` and the
diagnostics, and the API is asked again after ``UNAVAILABLE_TTL_S``.

What is fetched (``plan``), each as one *job*, a history query with fixed parameters (``md_hub_job``):

* ``tail``: from the first day of the month ``REFETCH_DAYS`` ago to today, per dataset; asked again every
  ``TAIL_POLL_S`` with the ETag of its first page: a 304 costs one request, a change (a new day, a correction of
  the last weeks) fetches it again. Its rows replace the stored ones of its range.
* chunks: every closed month (bonds) or year (the rest) before the tail, back to the configured depth
  (``history_bonds_from``, default 2024-01-03 where the hub's bond EOD starts; ``history_rates_from``, default
  2010-01-01, bounded by the first date the index lists), newest first; once complete, revalidated every
  ``REVALIDATE_S`` by one conditional request of its first page (the hub's ETag covers the whole selection, so a
  revision of any row of the chunk, also of a past day, makes it 200) and fetched again when changed.
* ``held``: the desk's bonds (the default runs of the latest dates) not fetched yet, their whole depth at once
  (500 secids per call), before the whole market's months: a new position gets its history in the next cycle.

Paging follows ``next`` with identical parameters; a job that is still paging when the cycle's budget
(``BUDGET_S``) runs out keeps its cursor and goes on in the next cycle (``status partial``). Every fetch writes its
rows with a new fetch id (``md_hub_fetch_seq``); when its last page is in, the rows of its range and selection from
older fetches are deleted (the hub no longer has them), rows a newer fetch of an overlapping query wrote stay.
Numbers arrive as ``Decimal`` (the hub sends the stored digits); rates and fixings are stored as ``numeric``, bond
fields as ``double precision`` like the snapshot rows they sit beside (views ``md_bond_day``, ``md_kbd_day``).

Sizes (synthetic, ``tests/test_hub_history.py``; the hub's own test: 125 000 bond rows in 3 calls in ~3 s): a bond
row of the fields asked for is ~250 bytes of JSON, ~35 bytes gzipped; the whole market (~3 000 bonds) from
2024-01-03 is ~2.1 M rows, ~45 pages of 50 000, ~75 MB on the wire once, then the tail (2 pages) when the hub
changes it and ~50 conditional requests a day.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from ..inbox.timesem import MSK
from ..store import DEFAULT_RUNS
from .hub import HistoryPage, HistoryUnavailable, HubClient, HubError

log = logging.getLogger("desk.marketdata.hubhistory")

BONDS_FROM = date(2024, 1, 3)
"""Default depth of the bond history: the hub's bond EOD starts on this date."""
RATES_FROM = date(2010, 1, 1)
"""Default depth of the KBD, curves, fixings and FX: as far as the hub has them (its index bounds it)."""
REFETCH_DAYS = 14        # the tail starts on the first day of the month this many days ago: corrections land there
TAIL_POLL_S = 600.0      # the tail is revalidated at most this often
REVALIDATE_S = 24 * 3600.0  # a complete chunk is revalidated (one conditional request) this often
INDEX_TTL_S = 3600.0     # /v1/history/index is read again after this
UNAVAILABLE_TTL_S = 3600.0  # a hub without the API (or a token without read) is asked again after this
BUDGET_S = 40.0          # seconds of history fetching per autopilot cycle; the rest goes on in the next cycle
PAGE_LIMIT = 50000       # the hub's maximum
MAX_SECIDS = 500         # per bonds call (the hub's maximum)
HELD_RUNS = 10           # the default runs whose bonds are "held" (fetched first)
FX_KINDS = ("cbr_official", "moex_eod")

BOND_FIELDS = ("legal_close_pct", "close_pct", "waprice_pct", "accrued_per_bond", "face_value", "face_unit",
               "currency_id", "volume_bonds", "value_ccy", "num_trades", "yield_close_pct", "effective_yield_pct",
               "yield_to_offer_pct", "yield_date", "yield_date_type", "duration_days", "zspread_bp", "gspread_bp",
               "icpi", "ir", "bei", "cbr")
BOND_PRICES = (("legal_close", "legal_close_pct"), ("close", "close_pct"), ("waprice", "waprice_pct"))
"""The runs' price basis order (``valuation.bonds.PRICE_BASIS_ORDER`` without ``last``, an intraday basis)."""
ZCYC_PARAMS = ("B1", "B2", "B3", "T1", *(f"G{i}" for i in range(1, 10)))
CURVE_FIELDS = ("fetched_at", "quote_basis", "labels", "tenor_months", "tenor_days", "bid_pct", "mid_pct", "ask_pct")
FIXING_FIELDS = ("value", "unit", "effective_date", "published_on", "hub_ingested_at", "labels")
FX_FIELDS = ("rate", "tenor", "close", "waprice", "close_per_unit", "waprice_per_unit", "nominal")


def _plain(v: Any) -> Any:
    """JSON-able: Decimals as their digits (strings), recursively."""
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def _js(v: Any) -> Jsonb | None:
    return None if v is None else Jsonb(_plain(v))


# ---- datasets ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spec:
    """A dataset of the history API and its table. ``scope``: the deletion's extra condition (SQL with
    ``%(…)s`` parameters from ``scope_args``)."""

    name: str
    path: str
    params: tuple[tuple[str, str], ...]
    chunk: str            # month | year
    start: date
    table: str
    date_col: str
    cols: tuple[str, ...]
    keys: tuple[str, ...]
    convert: Callable[[dict[str, Any]], tuple | None]
    scope: str = "TRUE"
    scope_args: tuple[tuple[str, Any], ...] = ()


def _bond(r: dict[str, Any]) -> tuple | None:
    px = next(((b, r[f]) for b, f in BOND_PRICES if r.get(f) is not None), None)
    y = r.get("effective_yield_pct")
    if px is None and y is None:
        return None  # no price and no yield: not a quoted day (as the snapshot rows)
    return (r["trade_date"], r["secid"], r["board"], px and px[1], px and px[0], r.get("close_pct"),
            r.get("waprice_pct"), y, r.get("yield_close_pct"), r.get("yield_to_offer_pct"), r.get("yield_date"),
            r.get("yield_date_type"), r.get("duration_days"), r.get("gspread_bp"), r.get("zspread_bp"),
            r.get("accrued_per_bond"), r.get("face_value"), r.get("face_unit"), r.get("currency_id"),
            r.get("volume_bonds"), r.get("value_ccy"), r.get("num_trades"), r.get("icpi"), r.get("ir"), r.get("bei"),
            r.get("cbr"))


BOND_COLS = ("trade_date", "secid", "board", "clean_pct", "price_basis", "close_pct", "waprice_pct", "yield_pct",
             "yield_close_pct", "yield_to_offer_pct", "yield_date", "yield_date_type", "duration_days", "gspread_bp",
             "zspread_bp", "accrued_per_bond", "face_value", "face_unit", "currency_id", "volume_bonds", "value",
             "num_trades", "icpi", "ir", "bei", "cbr")


def _zcyc(r: dict[str, Any]) -> tuple | None:
    params = {k: _plain(r.get(k)) for k in ZCYC_PARAMS}
    if any(v is None for v in params.values()):
        return None  # an incomplete publication gives no curve rate (md_kbd keeps such a day with params null)
    return r["trade_date"], r.get("trade_time"), r.get("is_eod"), Jsonb(params), _js(r.get("standard_yields"))


def _curve(dataset: str) -> Callable[[dict[str, Any]], tuple | None]:
    def conv(r: dict[str, Any]) -> tuple | None:
        return (dataset, r["snapshot_date"], r["source"], r["tenor"], r.get("quote_set_id"), r.get("position"),
                r.get("tenor_months"), r.get("tenor_days"), r.get("bid_pct"), r.get("mid_pct"), r.get("ask_pct"),
                r.get("quote_basis"), r.get("fetched_at"), _js(r.get("labels")))
    return conv


def _fixing(r: dict[str, Any]) -> tuple | None:
    return (r["index_code"], r["source"], r["fixing_date"], r.get("value"), r.get("unit"), r.get("effective_date"),
            r.get("published_on"), r.get("hub_ingested_at"), _js(r.get("labels")))


def _fx(r: dict[str, Any]) -> tuple | None:
    per_unit = r.get("rate") if r["kind"] == "cbr_official" else (r.get("close_per_unit")
                                                                  or r.get("waprice_per_unit"))
    return (r["kind"], r["pair"], r["date"], r.get("secid") or "", r.get("board") or "", r.get("rate"),
            r.get("close"), r.get("waprice"), per_unit, r.get("tenor"), r.get("nominal"))


def specs(index: list[dict[str, Any]], bonds_from: date = BONDS_FROM, rates_from: date = RATES_FROM) -> list[Spec]:
    """The datasets the desk fetches, from what the index lists: rates first (small), the bonds last."""
    first: dict[str, date] = {}
    curves: dict[str, date] = {}
    for r in index:
        if not r.get("date_from"):
            continue
        d = date.fromisoformat(str(r["date_from"]))
        ds = r.get("dataset")
        if ds == "curves" and r.get("series"):
            curves[r["series"]] = min(curves.get(r["series"], d), d)
        elif ds in ("bonds", "zcyc", "fixings", "fx"):
            first[ds] = min(first.get(ds, d), d)
    lim = (("limit", str(PAGE_LIMIT)),)
    out: list[Spec] = []
    if "zcyc" in first:
        out.append(Spec("zcyc", "/zcyc", (("rule", "eod"), ("fields", ",".join(("trade_time", "is_eod",
                                                                                 *ZCYC_PARAMS, "standard_yields"))),
                                          *lim), "year", max(rates_from, first["zcyc"]), "md_hub_zcyc", "trade_date",
                        ("trade_date", "trade_time", "is_eod", "params", "standard_yields"), ("trade_date",), _zcyc))
    if "fixings" in first:
        out.append(Spec("fixings", "/fixings", (("fields", ",".join(FIXING_FIELDS)), *lim), "year",
                        max(rates_from, first["fixings"]), "md_hub_fixing", "fixing_date",
                        ("index_code", "source", "fixing_date", "value", "unit", "effective_date", "published_on",
                         "hub_ingested_at", "labels"), ("index_code", "source", "fixing_date"), _fixing))
    if "fx" in first:
        out.append(Spec("fx", "/fx", (("kind", ",".join(FX_KINDS)), ("fields", ",".join(FX_FIELDS)), *lim), "year",
                        max(rates_from, first["fx"]), "md_hub_fx", "date",
                        ("kind", "pair", "date", "secid", "board", "rate", "close", "waprice", "per_unit", "tenor",
                         "nominal"), ("kind", "pair", "date", "secid", "board"), _fx,
                        "kind = ANY(%(kinds)s)", (("kinds", list(FX_KINDS)),)))
    for ds, d in sorted(curves.items()):
        out.append(Spec(f"curves/{ds}", f"/curves/{ds}", (("rule", "eod"), ("fields", ",".join(CURVE_FIELDS)), *lim),
                        "year", max(rates_from, d), "md_hub_curve", "snapshot_date",
                        ("dataset", "snapshot_date", "source", "tenor", "quote_set_id", "position", "tenor_months",
                         "tenor_days", "bid_pct", "mid_pct", "ask_pct", "quote_basis", "fetched_at", "labels"),
                        ("dataset", "snapshot_date", "source", "tenor"), _curve(ds), "dataset = %(ds)s",
                        (("ds", ds),)))
    if "bonds" in first:
        out.append(Spec("bonds", "/bonds", (("board", "primary"), ("fields", ",".join(BOND_FIELDS)), *lim), "month",
                        max(bonds_from, first["bonds"]), "md_hub_bond", "trade_date", BOND_COLS,
                        ("trade_date", "secid"), _bond))
    return out


# ---- jobs --------------------------------------------------------------------------------------------------------


@dataclass
class Job:
    spec: Spec
    key: str
    period_from: date
    period_to: date
    secids: tuple[str, ...] | None = None

    @property
    def dataset(self) -> str:
        return "held" if self.key.startswith("held") else self.spec.name

    def params(self) -> dict[str, Any]:
        p = dict(self.spec.params)
        p.update({"from": self.period_from.isoformat(), "to": self.period_to.isoformat()})
        if self.secids:
            p["secid"] = ",".join(self.secids)
        return p


def tail_from(today: date) -> date:
    """The first day of the tail: the first of the month ``REFETCH_DAYS`` before today."""
    return (today - timedelta(days=REFETCH_DAYS)).replace(day=1)


def chunks(spec: Spec, today: date) -> list[Job]:
    """The closed chunks of a dataset before the tail, newest first."""
    end = tail_from(today) - timedelta(days=1)
    out = []
    d = spec.start
    while d <= end:
        if spec.chunk == "month":
            nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
            key = d.strftime("%Y-%m")
        else:
            nxt = date(d.year + 1, 1, 1)
            key = str(d.year)
        out.append(Job(spec, key, d, min(nxt - timedelta(days=1), end)))
        d = nxt
    return out[::-1]


def held_secids(con: psycopg.Connection) -> list[str]:
    """The bonds of the default runs of the ``HELD_RUNS`` latest dates (the MOEX secid: the fact's instrument id)."""
    rows = _q(con)(
        f"SELECT DISTINCT COALESCE(p.fact->>'instrument_id', p.secid) FROM run_position p WHERE p.run_id IN"
        f" (SELECT run_id FROM run WHERE run_id IN ({DEFAULT_RUNS}) ORDER BY business_date DESC LIMIT {HELD_RUNS})"
        " AND p.fact->>'instr_type' = 'BOND'").fetchall()
    return sorted(s for (s,) in rows if s)


# ---- the state of the API -----------------------------------------------------------------------------------------

_MEMO: dict[str, tuple[float, str, str | None, list[dict[str, Any]] | None]] = {}
"""hub URL -> (monotonic time, code, reason, index): the API's last answer in this process."""


def record_state(con: psycopg.Connection, code: str, reason: str | None, index: list[dict] | None = None,
                 version: str | None = None) -> None:
    con.execute("INSERT INTO md_hub_state (id, code, reason, hub_version, index, checked_at)"
                " VALUES (true, %s, %s, %s, %s, clock_timestamp()) ON CONFLICT (id) DO UPDATE SET code = EXCLUDED.code,"
                " reason = EXCLUDED.reason, hub_version = COALESCE(EXCLUDED.hub_version, md_hub_state.hub_version),"
                " index = COALESCE(EXCLUDED.index, md_hub_state.index), checked_at = EXCLUDED.checked_at",
                (code, reason, version, _js(index)))


def forget() -> None:
    """Drop the in-process memory of the API's answers (tests; a new token from the settings page)."""
    _MEMO.clear()


# ---- one pass -----------------------------------------------------------------------------------------------------


@dataclass
class Report:
    available: bool
    code: str = "ok"
    reason: str | None = None
    requests: int = 0
    not_modified: int = 0
    rows: int = 0
    wire_bytes: int = 0
    done: list[str] = field(default_factory=list)       # jobs completed this pass
    partial: list[str] = field(default_factory=list)    # jobs left mid-way (budget)
    errors: list[str] = field(default_factory=list)
    left: int = 0                                       # jobs still due after this pass
    seconds: float = 0.0

    def text(self) -> str:
        if not self.available:
            return f"history API: {self.reason}"
        parts = [(f"history API: {self.requests} requests ({self.not_modified} unchanged), {self.rows} rows,"
                 f" {self.wire_bytes / 1e6:.1f} MB in {self.seconds:.1f} s")]
        if self.done:
            shown = self.done[:6] + ([f"+{len(self.done) - 6}"] if len(self.done) > 6 else [])
            parts.append("fetched " + ", ".join(shown))
        if self.partial:
            parts.append("continues " + ", ".join(self.partial))
        if self.errors:
            parts.append("errors " + "; ".join(self.errors[:3]))
        parts.append(f"{self.left} queries in the next cycles" if self.left else "up to date")
        return "; ".join(parts)


class Sync:
    """One pass over the jobs within ``budget_s``. ``today``: the Moscow date (the tail's last day)."""

    def __init__(self, con: psycopg.Connection, client: HubClient, *, today: date | None = None,
                 bonds_from: date = BONDS_FROM, rates_from: date = RATES_FROM, held: Iterable[str] = (),
                 budget_s: float = BUDGET_S, progress: Callable[[int, int | None, str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.con, self.client = con, client
        self.today = today or datetime.now(MSK).date()
        self.bonds_from, self.rates_from = bonds_from, rates_from
        self.held = sorted(set(held))
        self.budget_s, self.progress, self.clock = budget_s, progress, clock
        self.deadline = clock() + budget_s
        self.report = Report(True)

    # -- the index
    def index(self) -> list[dict[str, Any]] | None:
        host = getattr(self.client, "url", None) or self.client.host  # the hub (scheme, host, port)
        now = self.clock()
        memo = _MEMO.get(host)
        if memo is not None:
            at, code, reason, idx = memo
            if code != "ok" and now - at < UNAVAILABLE_TTL_S:
                self.report = Report(False, code, reason)
                return None
            if code == "ok" and now - at < INDEX_TTL_S and idx is not None:
                return idx
        try:
            idx = self.client.history_index()
            self.report.requests += 1
        except HistoryUnavailable as e:
            _MEMO[host] = (now, e.code, str(e), None)
            record_state(self.con, e.code, str(e))
            self.report = Report(False, e.code, str(e))
            return None
        _MEMO[host] = (now, "ok", None, idx)
        record_state(self.con, "ok", None, idx)
        return idx

    # -- the plan
    def plan(self, idx: list[dict[str, Any]]) -> list[tuple[str, Job]]:
        """(kind, job) in the order they run: the tails, the held bonds, the chunks not complete (rates first, then
        the bonds' months, newest first), the complete chunks due for revalidation (least recently checked first).
        Every job's row is created or, when its parameters changed, reset."""
        sp = specs(idx, self.bonds_from, self.rates_from)
        jobs: list[tuple[str, Job]] = []
        tf = tail_from(self.today)
        for s in sp:
            jobs.append(("tail", Job(s, "tail", max(tf, s.start) if s.start <= self.today else tf, self.today)))
        bonds = next((s for s in sp if s.name == "bonds"), None)
        if bonds is not None and self.held:
            have = {s for (s,) in _q(self.con)("SELECT secid FROM md_hub_held WHERE from_date <= %s",
                                                   (bonds.start,))}
            new = [s for s in self.held if s not in have]
            if new:
                jobs.append(("held", Job(bonds, "held", bonds.start, self.today, tuple(new[:MAX_SECIDS]))))
        for s in sp:
            jobs += [("chunk", j) for j in chunks(s, self.today)]
        rows = self._sync_rows([j for _, j in jobs])
        now = datetime.now(UTC)
        out, recheck = [], []
        for kind, j in jobs:
            st = rows[(j.dataset, j.key)]
            if kind == "tail":
                if st["status"] != "done" or st["cursor"] or not _fresh(st["checked_at"], now, TAIL_POLL_S):
                    out.append((kind, j))
            elif st["status"] == "error":
                if not _fresh(st["checked_at"], now, TAIL_POLL_S):  # a query the hub refused: not every cycle
                    out.append((kind, j))
            elif kind == "held" or st["status"] != "done":
                out.append((kind, j))
            elif not _fresh(st["checked_at"], now, REVALIDATE_S):
                recheck.append((st["checked_at"] or datetime.min.replace(tzinfo=UTC), kind, j))
        out += [(k, j) for _, k, j in sorted(recheck, key=lambda x: x[0])]
        return out

    def _sync_rows(self, jobs: list[Job]) -> dict[tuple[str, str], dict[str, Any]]:
        """Create the jobs' rows; reset a row whose parameters changed (the tail's dates, a chunk's end)."""
        sql = "SELECT dataset, key, params, status, cursor, checked_at FROM md_hub_job"
        cur = {(r[0], r[1]): r[2] for r in _q(self.con)(sql)}
        with self.con.transaction():
            for j in jobs:
                p = j.params()
                if cur.get((j.dataset, j.key)) == p:
                    continue
                # a new query: no cursor, no ETag; a tail that was complete stays "done" (it is due anyway)
                self.con.execute(
                    "INSERT INTO md_hub_job (dataset, key, params, period_from, period_to) VALUES (%s, %s, %s, %s, %s)"
                    " ON CONFLICT (dataset, key) DO UPDATE SET params = EXCLUDED.params, period_from ="
                    " EXCLUDED.period_from, period_to = EXCLUDED.period_to, cursor = NULL, pending_etag = NULL,"
                    " etag = NULL, status = CASE WHEN md_hub_job.key = 'tail' AND md_hub_job.status = 'done'"
                    " THEN 'done' ELSE 'new' END, checked_at = NULL",
                    (j.dataset, j.key, Jsonb(p), j.period_from, j.period_to))
        return {(r[0], r[1]): {"status": r[3], "cursor": r[4], "checked_at": r[5]} for r in _q(self.con)(sql)}

    # -- running
    def run(self) -> Report:
        t0 = self.clock()
        idx = self.index()
        if idx is None:
            return self.report
        todo = self.plan(idx)
        left = 0
        try:
            for i, (kind, job) in enumerate(todo):
                if self.clock() >= self.deadline:
                    left = len(todo) - i
                    break
                if self.progress is not None:
                    self.progress(i, len(todo), f"{job.dataset} {job.key}")
                self._job(kind, job)
        except HistoryUnavailable as e:  # the token lost its scope between two calls
            _MEMO[getattr(self.client, "url", None) or self.client.host] = (self.clock(), e.code, str(e), None)
            record_state(self.con, e.code, str(e))
            self.report.available, self.report.code, self.report.reason = False, e.code, str(e)
        self.report.left = left + len(self.report.partial)
        self.report.seconds = round(self.clock() - t0, 2)
        return self.report

    def _job(self, kind: str, job: Job) -> None:
        name = f"{job.dataset} {job.key}"
        try:
            outcome = self._fetch(job)
        except HubError as e:
            if e.status is None or e.status >= 500:
                raise  # the hub is not answering: the cycle's step error, the rest waits
            self.con.execute("UPDATE md_hub_job SET status = 'error', error = %s, cursor = NULL, pending_etag = NULL,"
                             " checked_at = clock_timestamp() WHERE dataset = %s AND key = %s",
                             (str(e)[:500], job.dataset, job.key))
            self.report.errors.append(f"{name}: {e}")
            log.warning("history API: %s: %s", name, e)
            return
        if outcome == "done":
            self.report.done.append(name)
        elif outcome == "partial":
            self.report.partial.append(name)

    def _fetch(self, job: Job) -> str:
        """``unchanged`` (304), ``done`` or ``partial`` (the budget ran out between two pages)."""
        con, spec, params = self.con, job.spec, job.params()
        row = _q(con)("SELECT status, etag, cursor, pending_etag, fetch_id, data_version, rows, pages,"
                          " wire_bytes FROM md_hub_job WHERE dataset = %s AND key = %s",
                          (job.dataset, job.key)).fetchone()
        status, etag, cursor, pending, fetch_id, dv, nrows, pages, nbytes = row
        changed = False
        if cursor is None:
            page = self.client.history_page(spec.path, params, etag=etag if status == "done" else None)
            self.report.requests += 1
            if page.not_modified:
                self.report.not_modified += 1
                con.execute("UPDATE md_hub_job SET checked_at = clock_timestamp(), error = NULL"
                            " WHERE dataset = %s AND key = %s", (job.dataset, job.key))
                return "unchanged"
            fetch_id = _q(con)("SELECT nextval('md_hub_fetch_seq')").fetchone()[0]
            pending, dv, nrows, pages, nbytes = page.etag, page.data_version, 0, 0, 0
            cursor = self._write(job, page, fetch_id, pending, dv, nrows, pages, nbytes)
            nrows, pages, nbytes = nrows + len(page.rows), pages + 1, nbytes + page.wire_bytes
        while cursor is not None and self.clock() < self.deadline:
            page = self.client.history_page(spec.path, params, cursor=cursor)
            self.report.requests += 1
            if page.data_version != dv:
                changed = True  # the hub wrote meanwhile: this fetch is refetched at its next check
            cursor = self._write(job, page, fetch_id, pending, dv, nrows, pages, nbytes)
            nrows, pages, nbytes = nrows + len(page.rows), pages + 1, nbytes + page.wire_bytes
        if cursor is not None:
            return "partial"
        with con.transaction():
            scope = dict(spec.scope_args)
            where = f"{spec.date_col} BETWEEN %(f)s AND %(t)s AND fetch_id < %(id)s AND {spec.scope}"
            if job.secids:
                where += " AND secid = ANY(%(secids)s)"
            con.execute(f"DELETE FROM {spec.table} WHERE {where}",
                        {**scope, "f": job.period_from, "t": job.period_to, "id": fetch_id,
                         "secids": list(job.secids or ())})
            con.execute("UPDATE md_hub_job SET status = 'done', etag = %s, cursor = NULL, pending_etag = NULL,"
                        " fetched_at = clock_timestamp(), checked_at = CASE WHEN %s THEN NULL ELSE clock_timestamp()"
                        " END, error = NULL WHERE dataset = %s AND key = %s",
                        (None if changed else pending, changed, job.dataset, job.key))
            if job.secids:
                con.execute("INSERT INTO md_hub_held (secid, from_date) SELECT unnest(%s::text[]), %s"
                            " ON CONFLICT (secid) DO UPDATE SET from_date = EXCLUDED.from_date,"
                            " fetched_at = clock_timestamp()", (list(job.secids), job.period_from))
        return "done"

    def _write(self, job: Job, page: HistoryPage, fetch_id: int, pending: str | None, dv: str | None, nrows: int,
               pages: int, nbytes: int) -> str | None:
        """A page's rows into the table and the job's cursor, in one transaction; returns ``page.next``."""
        spec = job.spec
        rows = [t for t in (spec.convert(r) for r in page.records()) if t is not None]
        self.report.rows += len(rows)
        self.report.wire_bytes += page.wire_bytes
        with self.con.transaction():
            if rows:
                upsert(self.con, spec.table, spec.cols, spec.keys, rows, fetch_id)
            self.con.execute(
                "UPDATE md_hub_job SET status = 'partial', cursor = %s, pending_etag = %s, fetch_id = %s,"
                " data_version = %s, rows = %s, pages = %s, wire_bytes = %s WHERE dataset = %s AND key = %s",
                (page.next, pending, fetch_id, dv, nrows + len(page.rows), pages + 1, nbytes + page.wire_bytes,
                 job.dataset, job.key))
        return page.next


def _q(con: psycopg.Connection) -> Callable[..., psycopg.Cursor]:
    """``execute`` of a cursor with tuple rows, whatever the connection's row factory (the API's are dicts)."""
    return con.cursor(row_factory=tuple_row).execute


def _fresh(ts: datetime | None, now: datetime, ttl: float) -> bool:
    return ts is not None and (now - ts).total_seconds() < ttl


def upsert(con: psycopg.Connection, table: str, cols: tuple[str, ...], keys: tuple[str, ...], rows: list[tuple],
           fetch_id: int) -> None:
    """Rows into ``table`` with ``fetch_id``: COPY into a temporary table, then one INSERT … ON CONFLICT (the last
    row of a key wins)."""
    tmp = f"_load_{table}"
    allc = (*cols, "fetch_id")
    con.execute(f"CREATE TEMP TABLE IF NOT EXISTS {tmp} (LIKE {table}) ON COMMIT DELETE ROWS")
    with con.cursor() as c, c.copy(f"COPY {tmp} ({', '.join(allc)}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row((*r, fetch_id))
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in allc if c not in keys)
    k = ", ".join(keys)
    con.execute(f"INSERT INTO {table} ({', '.join(allc)}) SELECT DISTINCT ON ({k}) {', '.join(allc)} FROM {tmp}"
                f" ORDER BY {k} ON CONFLICT ({k}) DO UPDATE SET {upd}")


def sync(con: psycopg.Connection, client: HubClient, **kw: Any) -> Report:
    """One pass (``Sync``); the autopilot's history step and ``desk history sync``."""
    return Sync(con, client, **kw).run()


# ---- status -------------------------------------------------------------------------------------------------------

TABLES = (("bonds", "md_hub_bond", "trade_date"), ("zcyc", "md_hub_zcyc", "trade_date"),
          ("curves", "md_hub_curve", "snapshot_date"), ("fixings", "md_hub_fixing", "fixing_date"),
          ("fx", "md_hub_fx", "date"))


def status(con: psycopg.Connection) -> dict[str, Any]:
    """The API's last answer and, per dataset, the stored range and rows and the jobs: ``{"api": {code, reason,
    checked_at, hub_version} | None, "datasets": {name: {from, to, rows, jobs, done, pending, errors, fetched_at,
    checked_at, wire_bytes}}}``. Dates, counts and codes only."""
    q = _q(con)
    st = q("SELECT code, reason, checked_at, hub_version FROM md_hub_state").fetchone()
    api = None if st is None else {"code": st[0], "reason": st[1], "checked_at": st[2] and st[2].isoformat(),
                                   "hub_version": st[3], "available": st[0] == "ok"}
    out: dict[str, dict[str, Any]] = {}
    for name, table, col in TABLES:
        r = q(f"SELECT min({col}), max({col}), count(*) FROM {table}").fetchone()
        out[name] = {"from": r[0] and r[0].isoformat(), "to": r[1] and r[1].isoformat(), "rows": r[2]}
    if (n := q("SELECT count(*), count(DISTINCT secid) FROM md_hub_bond").fetchone()) is not None:
        out["bonds"]["bonds"] = n[1]
    for ds, jobs, done, pend, errs, fetched, checked, nbytes in q(
            "SELECT split_part(dataset, '/', 1), count(*), count(*) FILTER (WHERE status = 'done'),"
            " count(*) FILTER (WHERE status IN ('new', 'partial')), count(*) FILTER (WHERE status = 'error'),"
            " max(fetched_at), max(checked_at), sum(wire_bytes) FROM md_hub_job GROUP BY 1"):
        d = out.setdefault(ds if ds != "held" else "held", {})
        d.update({"jobs": jobs, "done": done, "pending": pend, "errors": errs,
                  "fetched_at": fetched and fetched.isoformat(), "checked_at": checked and checked.isoformat(),
                  "wire_bytes": int(nbytes or 0)})
    errors = [f"{d} {k}: {e}" for d, k, e in q(
        "SELECT dataset, key, error FROM md_hub_job WHERE status = 'error' ORDER BY dataset, key LIMIT 10")]
    return {"api": api, "datasets": out, "errors": errors}


def status_text(st: dict[str, Any]) -> list[str]:
    """``desk history status`` lines."""
    api = st["api"]
    lines = ["history API: " + ("never asked" if api is None else
                                "available" if api["available"] else f"not used: {api['reason']}")
             + (f" (checked {api['checked_at'][:19]})" if api else "")]
    for name, d in st["datasets"].items():
        if name == "held":
            lines.append(f"  held      {d.get('done', 0)} of {d.get('jobs', 0)} batches fetched")
            continue
        rng = f"{d['from']}..{d['to']}" if d.get("from") else "empty"
        extra = f", {d['bonds']} bonds" if d.get("bonds") else ""
        jobs = (f"; queries {d.get('done', 0)}/{d.get('jobs', 0)} complete" + (f", {d['pending']} to fetch"
                                                                                  if d.get("pending") else "")
                + (f", {d['errors']} failed" if d.get("errors") else "")) if d.get("jobs") else ""
        lines.append(f"  {name:<9} {rng}, {d['rows']} rows{extra}{jobs}")
    lines += [f"  error: {e}" for e in st["errors"]]
    return lines


def to_json(st: dict[str, Any]) -> str:
    return json.dumps(st, ensure_ascii=False, indent=1)
