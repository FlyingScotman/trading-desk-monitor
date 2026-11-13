"""TDM#736, the bodies of a new intraday run built from what the earlier dates already gave: the year's and the month's
PnL sums (``pnl_mtd.PeriodCache``), the lines of the desk's history (``series_cache.Memo``), the summaries read without
the positions of the dates that do not need them. The one rule: a body built with the caches is the body built without
them, byte for byte (JSON equality of the pages' data), whatever came since the caches were filled: a new intraday run
of the date, the next date's close, an earlier date recomputed (a new run in its place), a run that is not the newest
asked for by its id, a run reported apart from the sums. Synthetic data only: the two runs of ``test_r23_concepts``
cloned onto other dates with the PnL scaled by odd factors, so that the sums are not round."""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from test_r23_concepts import DATE, PREV, desk  # noqa: F401 - the fixture

from desk import store
from desk.api import app as appmod
from desk.api import pnl_mtd
from desk.api.app import warm_bodies
from desk.api.pnl_history import Sum, amounts, period_runs, rows_query
from desk.api.runs import resolve_run
from desk.api.series_cache import Memo, SeriesCache
from desk.publish import summary

BIG = ["run", "extras", "history", "desk"]


def scaled(o, k: float):
    """The numbers of a PnL document times ``k`` (strings, flags and integers stay)."""
    if isinstance(o, dict):
        return {key: scaled(v, k) for key, v in o.items()}
    if isinstance(o, list):
        return [scaled(v, k) for v in o]
    return o * k if isinstance(o, float) else o


def add_run(store_url: str, src: int, *, day: str | None = None, k: float = 1.0, intraday: bool = False,
            prev: str | None = None) -> int:
    """A complete run made of ``src``'s rows on ``day`` (its own date when None), the PnL times ``k``, published as the
    pipeline does. ``intraday``: an INTRADAY run (``prev``: its previous close, the day before ``day`` by default)."""
    with store.connect(store_url) as con:
        cols = [d.name for d in con.execute("SELECT * FROM run LIMIT 0").description]
        row = dict(zip(cols, con.execute("SELECT * FROM run WHERE run_id = %s", (src,)).fetchone()))
        skip = {"run_id", "status", "completed_at", "started_at", "coverage", "totals", "error"}
        vals = {c: v for c, v in row.items() if c not in skip}
        if day:
            d = date.fromisoformat(day)
            vals.update(business_date=d, prev_date=d - timedelta(days=1))
        if prev:
            vals["prev_date"] = date.fromisoformat(prev)
        if intraday:
            vals.update(run_type="INTRADAY", cut_type="intraday")
        vals = {c: Jsonb(v) if isinstance(v, dict | list) and c != "position_set_ids" else v for c, v in vals.items()}
        with con.transaction():
            rid = con.execute(f"INSERT INTO run ({', '.join(vals)}) VALUES ({', '.join(['%s'] * len(vals))})"
                              " RETURNING run_id", list(vals.values())).fetchone()[0]
            pc = [d.name for d in con.execute("SELECT * FROM run_position LIMIT 0").description
                  if d.name not in ("run_id", "pnl")]
            con.execute(f"INSERT INTO run_position (run_id, pnl, {', '.join(pc)}) SELECT %s, NULL, {', '.join(pc)}"
                        " FROM run_position WHERE run_id = %s", (rid, src))
            for position_id, pnl in con.execute("SELECT position_id, pnl FROM run_position WHERE run_id = %s",
                                                (src,)).fetchall():
                if pnl is not None:
                    con.execute("UPDATE run_position SET pnl = %s WHERE run_id = %s AND position_id = %s",
                                (Jsonb(scaled(pnl, k)), rid, position_id))
            con.execute("INSERT INTO run_reference (run_id, kind, data) SELECT %s, kind, data FROM run_reference"
                        " WHERE run_id = %s", (rid, src))
            con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
            store.run_event(con, "run.published", [rid])
        summary.ensure(con, [rid])
    return rid


def history(store_url: str, days: list[str], base: int) -> dict[str, int]:
    """Runs on ``days`` (clones of ``base``, in the order given, so run ids do not follow the dates), the PnL times a
    different odd factor each."""
    rnd = random.Random(736)
    return {d: add_run(store_url, base, day=d, k=0.5 + rnd.random() * 1.7) for d in days}


EARLIER = ["2026-09-30", "2026-01-12", "2026-10-01", "2026-03-04", "2026-09-16", "2026-02-16", "2026-10-02",
           "2026-10-05", "2026-06-03", "2026-10-06", "2026-09-01", "2026-10-07", "2026-10-08", "2026-10-09",
           "2026-10-12", "2026-07-01", "2026-10-13"]
"""Business dates before the fixture's two runs, in a scrambled order: two months, the year's start."""


def newest(store_url: str, run_id: int | None = None) -> dict:
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        return resolve_run(con, None, run_id)


def scratch(store_url: str, run: dict) -> dict[str, bytes]:
    """The bodies of ``run`` built from the store alone, no cache."""
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        return {"run": appmod.run_body(con, run), "extras": appmod.extras_body(con, run),
                "history": appmod.history_body(con, run), "desk": appmod.desk_body(con, run)}


def cached(app, store_url: str, run: dict) -> dict[str, bytes]:
    """The bodies of ``run`` as ``warm_bodies`` left them in the app's cache (None: not there)."""
    out = {}
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        for name in BIG:
            key = appmod._warm_key(name, run["run_id"])
            fp = appmod.body_fingerprint(con, key, run, appmod.DESK_FP_SQL if name == "desk" else None)
            hit = app.state.body_cache.get(key, fp)
            out[name] = hit[0] if hit else None
    return out


def same(app, store_url: str, run_id: int | None = None) -> None:
    """Build the run's bodies with the caches, then from scratch: the same bytes."""
    run = newest(store_url, run_id)
    warm_bodies(app, run_id=run_id)
    have, want = cached(app, store_url, run), scratch(store_url, run)
    for name in BIG:
        assert have[name] is not None, f"{name} was not built"
        assert have[name] == want[name], f"{name} differs from the body built from scratch"


# ---- the bodies -------------------------------------------------------------------------------------------------------


def test_bodies_with_the_caches_equal_bodies_from_scratch_through_a_session(desk, store_url):  # noqa: F811
    """The session of ``serve``: the first build, new intraday runs of the date, one reported apart, the date's close, an
    earlier date recomputed, the next date's intraday runs, a run that is not the newest asked for by its id. After each
    step the caches' counters say what was kept (``hits``: the whole state, ``extended``: the start of it, ``misses``: read
    from the store) and the bodies are the ones built from scratch."""
    app = desk.app
    base = newest(store_url)["run_id"]
    old = history(store_url, EARLIER, base)  # run ids of the earlier dates: below the run shown, as they are in a store
    cur = add_run(store_url, base, day=DATE, k=1.0)  # the date's run: the default of 2026-10-15, newer than the fixture's
    assert newest(store_url)["run_id"] == cur
    series: SeriesCache = app.state.series_cache

    def moved(**delta: int) -> None:
        """The counters went up by ``delta`` (the year's and the month's state each)."""
        nonlocal seen
        now = series.period.stats()
        assert {k: now[k] - seen[k] for k in ("hits", "extended", "misses")} == \
            {"hits": 0, "extended": 0, "misses": 0, **delta}
        seen = now

    seen = series.period.stats()
    same(app, store_url)  # nothing kept yet: read from the store
    moved(misses=2)
    assert len(series.days) > 0

    r1 = add_run(store_url, base, day="2026-10-16", intraday=True, k=1.013, prev=DATE)
    same(app, store_url)  # a new intraday run of the next date: the kept sums plus the one run before it
    moved(extended=2)
    days = series.days.stats()
    r2 = add_run(store_url, r1, k=0.987)  # a later cut of the same date
    same(app, store_url)
    moved(hits=2)
    assert series.days.stats()["misses"] == days["misses"]  # the desk's history lines of the earlier runs: all kept

    # an intraday run whose interval overlaps a close's (its previous close is two days before): reported apart, not in the
    # sums
    r3 = add_run(store_url, r2, k=1.001, prev=PREV)
    run3 = newest(store_url)
    assert run3["run_id"] == r3
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        assert period_runs(con, run3, date(2026, 1, 1), run3["business_date"])["separate"]
    same(app, store_url)
    moved(hits=2)

    # the date's close: its own run is not among the runs before it, the state is the same
    eod = add_run(store_url, base, day="2026-10-16", k=1.05, prev=DATE)
    assert newest(store_url)["run_id"] == eod
    same(app, store_url)
    moved(hits=2)

    # an earlier date is calculated again: its new run takes the place of the old one among the defaults, and has the
    # newest run id: no state is made of these runs, and the run shown reads the new one's rows after its own
    again = add_run(store_url, base, day="2026-10-02", k=1.37)
    assert again > eod and newest(store_url)["run_id"] == eod
    same(app, store_url)
    moved(misses=2)
    r4 = add_run(store_url, eod, day="2026-10-19", intraday=True, k=0.93, prev="2026-10-16")
    same(app, store_url)  # the next date: the start of the state of the run before is gone on from, the new date's close on it
    moved(extended=2)
    r5 = add_run(store_url, r4, k=0.95)
    same(app, store_url)
    moved(hits=2)

    # a run that is not the newest, asked for by its id (a page pinned on it): built beside, the same bytes
    for pinned in (r1, eod, r5):
        same(app, store_url, run_id=pinned)
    assert old and r2 < r3 < eod < again < r4 < r5
    assert series.period.stats()["items"] <= series.period.size


def test_the_period_sums_of_the_cache_are_the_scans_sums_bit_for_bit(desk, store_url):  # noqa: F811
    """``pnl_mtd.totals`` with and without the cache against the scan as it was written before the cache: every position's
    running sums (value, compensation, count), the factors, the dimensions of the closed positions, ``open`` and ``last``."""
    base = newest(store_url)["run_id"]
    history(store_url, EARLIER, base)
    cache = pnl_mtd.PeriodCache()
    for step in range(4):
        if step == 1:
            add_run(store_url, base, day="2026-10-16", intraday=True, k=1.1, prev=DATE)
        if step == 2:
            add_run(store_url, base, day="2026-10-02", k=1.21)  # a recomputed date
        if step == 3:
            add_run(store_url, base, day="2026-11-02", k=0.8)  # the month's first day: the month starts again
        run = newest(store_url)
        with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
            want = reference_totals(con, run, run["business_date"])
            for c in (None, cache, cache):  # no cache, a state to make, the state to use
                got = pnl_mtd.totals(con, run, run["business_date"], c)
                assert got["separate"] == want["separate"]
                assert set(got["positions"]) == set(want["positions"])
                for key, w in want["positions"].items():
                    g = got["positions"][key]
                    assert state(g) == state(w), (step, key)
                    assert g.row == w.row and g.open == w.open and g.last == w.last
                assert pnl_mtd.closed(got["positions"]) == pnl_mtd.closed(want["positions"])
                assert pnl_mtd.by_position(got["positions"]) == pnl_mtd.by_position(want["positions"])
    stats = cache.stats()
    assert stats["hits"] >= 2 and stats["items"] <= cache.size


def state(x: pnl_mtd.Acc) -> tuple:
    """Everything a running sum holds, bits included (``Sum`` keeps the sum, its compensation and the count)."""
    return tuple((s.s, s.c, s.n) for s in (x.ytd, x.mtd, *x.parts.values()))


def reference_totals(con, run: dict, as_of: date) -> dict:
    """``pnl_mtd.totals`` as it was before the cache: one scan of every row of the year in run id order."""
    year, month = as_of.replace(day=1, month=1), as_of.replace(day=1)
    sy, sm = period_runs(con, run, year, as_of), period_runs(con, run, month, as_of)
    skip = {"y": run["run_id"] if sy["separate"] else None, "m": run["run_id"] if sm["separate"] else None}
    in_month = set(sm["ids"])
    sql = (f'SELECT p.run_id AS "runId", p.book_id AS book, p.position_id AS "positionId", {pnl_mtd.NARROW_PNL} AS pnl,'
           " r.business_date AS date FROM run_position p JOIN run r USING (run_id) WHERE p.run_id = ANY(%s)")
    acc: dict = {}
    last: dict = {}
    for r in con.execute(sql + " ORDER BY p.run_id", (sy["ids"],)):
        key = (r["book"], r["positionId"])
        x = acc.setdefault(key, pnl_mtd.Acc())
        last[key] = max(last.get(key, (r["date"], r["runId"])), (r["date"], r["runId"]))
        if r["runId"] == run["run_id"]:
            x.open = True
        a = amounts(r["pnl"])
        if r["runId"] != skip["y"]:
            x.ytd.add(a["total"])
        if r["runId"] in in_month and r["runId"] != skip["m"]:
            x.mtd.add(a["total"])
            for k, comps in pnl_mtd.FACTORS.items():
                for c in comps:
                    x.parts[k].add(a["components"].get(c))
    gone = pnl_mtd.closed_keys(acc)
    if gone:
        dsql, dargs = rows_query({}, None, "1 AS one")
        dargs["ids"] = sorted({last[k][1] for k in gone})
        for r in con.execute(dsql, dargs):
            key = (r["book"], r["positionId"])
            if key in gone and last[key][1] == r["runId"]:
                acc[key].row = r
    for key, x in acc.items():
        x.last = last[key]
    return {"positions": acc, "separate": {"mtd": sm["separate"], "ytd": sy["separate"]}}


def test_a_state_is_only_used_for_the_runs_it_was_made_of(desk, store_url):  # noqa: F811
    """The key is the runs' ids and completion times: a date recomputed, a date added, a run gone is another key; the
    longest start of the wanted runs is gone on from, and what is handed out is a copy (the next build adds to its own)."""
    cache = pnl_mtd.PeriodCache(size=3)
    day = date(2026, 10, 1)
    t = [date(2026, 10, 15) + timedelta(days=i) for i in range(4)]  # stand-ins for completion times
    runs = tuple((i, t[0]) for i in range(1, 5))
    acc = {("b", "p"): pnl_mtd.Acc()}
    acc[("b", "p")].ytd.add(1.5)
    cache.put("y", day, runs, acc)
    got, n = cache.find("y", day, runs)
    assert n == 4 and got[("b", "p")].ytd.value() == 1.5
    got[("b", "p")].ytd.add(10.0)  # a build adds to its copy: the kept state is not changed
    assert cache.find("y", day, runs)[0][("b", "p")].ytd.value() == 1.5
    assert cache.find("y", day, (*runs, (9, t[1])))[1] == 4  # one more run after them: the state is the start of it
    assert cache.find("y", day, runs[:3])[1] == 0  # a run less: the state covers a run it must not have
    assert cache.find("y", day, ((1, t[0]), (2, t[1]), (3, t[0]), (4, t[0])))[1] == 0  # the same id, another completion
    assert cache.find("y", day, ((1, t[0]), (5, t[0]), (3, t[0]), (4, t[0])))[1] == 0  # a date recomputed: its new id
    assert cache.find("m", day, runs)[1] == 0 and cache.find("y", day + timedelta(days=31), runs)[1] == 0
    for i in range(5, 9):  # bounded
        cache.put("y", day, ((i, t[0]),), acc)
    assert cache.stats()["items"] == 3


def test_a_sum_copy_goes_on_apart_and_adds_the_same():
    xs = [0.1, 1e16, -1e16, 0.2, 3.3, 1e-9, None, 7.7]
    a = Sum()
    for v in xs[:4]:
        a.add(v)
    b = a.copy()
    for v in xs[4:]:
        a.add(v)
        b.add(v)
    whole = Sum()
    for v in xs:
        whole.add(v)
    assert (a.s, a.c, a.n) == (whole.s, whole.c, whole.n) == (b.s, b.c, b.n)
    assert math.isfinite(whole.value())


def test_the_summaries_come_without_the_positions_of_the_dates_that_need_none(desk, store_url):  # noqa: F811
    from desk.api import concepts

    base = newest(store_url)["run_id"]
    ids = list(history(store_url, EARLIER[:3], base).values()) + [base]
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        full = concepts.summaries(con, ids)
        part = concepts.summaries(con, ids, pos_of=ids[-2:])
    assert all("pos" in full[i] and full[i]["pos"] for i in ids)
    assert all("pos" not in part[i] for i in ids[:-2]) and all(part[i]["pos"] == full[i]["pos"] for i in ids[-2:])
    for i in ids:
        assert {k: v for k, v in part[i].items() if k != "pos"} == {k: v for k, v in full[i].items() if k != "pos"}
    # the positions asked for only (a card's history): their entries of every run, the same as in the whole summary
    some = sorted(full[ids[-1]]["pos"])[:2] + ["no such position"]
    with psycopg.connect(store_url, row_factory=dict_row, autocommit=True) as con:
        picked = concepts.summaries(con, ids, pids=some)
    for i in ids:
        assert picked[i]["pos"] == {k: v for k, v in full[i]["pos"].items() if k in some}


def test_the_memo_is_bounded_and_counts():
    m = Memo(size=2)
    assert m.get("a") is None
    m.put("a", 1)
    m.put("b", 2)
    assert m.get("a") == 1  # a is the latest used
    m.put("c", 3)
    assert m.get("b") is None and m.get("a") == 1 and m.get("c") == 3 and len(m) == 2
    assert m.stats() == {"items": 2, "hits": 3, "misses": 2}
