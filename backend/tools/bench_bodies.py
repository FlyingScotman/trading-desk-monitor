"""Timing of the bodies of a new intraday run at the owner's size (TDM#736): a year of default runs of a big book.

    PYTHONPATH=backend/src backend/.venv/bin/python backend/tools/bench_bodies.py --home DIR
        [--runs 260] [--positions 2000] [--books 60] [--bonds 3000] [--static 4500] [--json] [--keep]

Dev only (the ``dev`` extra: pgserver). Builds, in ``--home`` (a folder of your own, deleted at the end unless ``--keep``;
the store of 260 runs of 2000 positions is about 2 GB), the same synthetic store as ``bench_concepts.py`` (its functions),
then plays what ``serve`` does when runs are published and prints the seconds each body takes (``run``, ``extras``,
``history``, ``desk``, ``calendar`` and the whole ``warm_bodies``) as JSON:

* ``cold``: the newest run's bodies, nothing built before (the start of the server, or after an update);
* ``intraday 1`` and ``intraday 2``: a new intraday run of the next weekday is published (its rows are the newest run's),
  then another one of the same date: what a new run costs while the server is up;
* ``recompute``: an earlier date (a month back) is recomputed (a new end-of-day run of it), then a new intraday run.

With ``--check`` each step's bodies are also built from scratch (the caches off) and compared byte for byte. The
before/after of TDM#736 is this script on the checkout before and after the change. Synthetic data only."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import re
import shutil
import sys
import time
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
BACKEND = HERE.parent
sys.path.insert(1, str(BACKEND / "tests"))

import bench_concepts as bc
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from desk import store
from desk.api import app as appmod
from desk.api.app import create_app, warm_bodies
from desk.api.runs import resolve_run
from desk.publish import summary

BUILT = re.compile(r"built (\S+) in (\d+) ms, (\d+) KB")


def build_store(home: Path, a: argparse.Namespace) -> str:
    import pgserver

    srv = pgserver.get_server(home / "pg", cleanup_mode="stop")
    srv.psql("CREATE DATABASE bench;")
    url = srv.get_uri("bench")
    store.migrate(url)
    t0 = time.perf_counter()
    tpl = bc.template(home, url)
    with store.connect(url) as con:
        dates = bc.runs(con, tpl, a.runs, a.positions, a.books)
        bc.market(con, dates, max(a.bonds, a.positions))
        bc.static(con, a.static, dates[-1])
        con.execute("ANALYZE")
    print(f"store built in {time.perf_counter() - t0:.0f} s", file=sys.stderr, flush=True)
    return url


def clone_run(url: str, src: int, *, intraday: bool, day: date | None = None) -> int:
    """A complete run made of ``src``'s rows: an INTRADAY run of ``day`` (the next weekday of ``src``'s by default), or
    an EOD run of ``src``'s own date (a recompute of a past date)."""
    with store.connect(url) as con:
        cols = [d.name for d in con.execute("SELECT * FROM run LIMIT 0").description]
        row = dict(zip(cols, con.execute("SELECT * FROM run WHERE run_id = %s", (src,)).fetchone()))
        skip = {"run_id", "status", "completed_at", "started_at", "coverage", "totals", "error"}
        vals = {k: v for k, v in row.items() if k not in skip}
        if intraday:
            d = day or row["business_date"] + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            vals.update(run_type="INTRADAY", cut_type="intraday", business_date=d,
                        prev_date=row["prev_date"] if d == row["business_date"] else row["business_date"])
        vals = {k: Jsonb(v) if isinstance(v, dict | list) and k != "position_set_ids" else v for k, v in vals.items()}
        with con.transaction():
            rid = con.execute(f"INSERT INTO run ({', '.join(vals)}) VALUES ({', '.join(['%s'] * len(vals))})"
                              " RETURNING run_id", list(vals.values())).fetchone()[0]
            pc = [d.name for d in con.execute("SELECT * FROM run_position LIMIT 0").description if d.name != "run_id"]
            con.execute(f"INSERT INTO run_position (run_id, {', '.join(pc)}) SELECT %s, {', '.join(pc)}"
                        " FROM run_position WHERE run_id = %s", (rid, src))
            con.execute("INSERT INTO run_reference (run_id, kind, data) SELECT %s, kind, data FROM run_reference"
                        " WHERE run_id = %s", (rid, src))
            con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
        summary.ensure(con, [rid])
    return rid


class Catch(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.built: dict[str, tuple[float, int]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        m = BUILT.match(record.getMessage())
        if m:
            self.built[m.group(1)] = (int(m.group(2)) / 1000, int(m.group(3)))


def step(app, url: str, title: str, catch: Catch, check: bool) -> dict:
    """``warm_bodies`` for the newest run: the seconds of each body it built (the log's), and the whole."""
    catch.built.clear()
    t0 = time.perf_counter()
    built = warm_bodies(app)
    out = {"run": None, "total_s": round(time.perf_counter() - t0, 2),
           "bodies_s": {k: v[0] for k, v in catch.built.items()}, "kb": {k: v[1] for k, v in catch.built.items()},
           "built": built}
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as con:
        run = resolve_run(con, None, None)
        out["run"] = {"id": run["run_id"], "date": run["business_date"].isoformat(), "type": run["run_type"]}
        if check:
            out["identical"] = same_as_scratch(app, con, run)
    print(title, json.dumps(out["bodies_s"]), out["total_s"], file=sys.stderr, flush=True)
    return out


def same_as_scratch(app, con, run) -> dict[str, bool]:
    """Each warmed body of ``run`` as the cache holds it against the body built from scratch (the caches off)."""
    scratch = {"run": appmod.run_body(con, run), "extras": appmod.extras_body(con, run),
               "history": appmod.history_body(con, run), "desk": appmod.desk_body(con, run)}
    res = {}
    for name, body in scratch.items():
        key = appmod._warm_key(name, run["run_id"])
        fp = appmod.body_fingerprint(con, key, run, appmod.DESK_FP_SQL if name == "desk" else None)
        hit = app.state.body_cache.get(key, fp)
        res[name] = hit is not None and hit[0] == body
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True, help="a folder for the store (created; deleted at the end unless --keep)")
    ap.add_argument("--positions", type=int, default=2000)
    ap.add_argument("--books", type=int, default=60)
    ap.add_argument("--runs", type=int, default=260)
    ap.add_argument("--bonds", type=int, default=3000)
    ap.add_argument("--static", type=int, default=4500)
    ap.add_argument("--check", action="store_true", help="compare each step's bodies with ones built from scratch")
    ap.add_argument("--keep", action="store_true", help="keep the folder (the next run uses the store in it)")
    ap.add_argument("--json", action="store_true", help="only the JSON on stdout")
    a = ap.parse_args()
    home = Path(a.home)
    fresh = not (home / "pg").exists()
    home.mkdir(parents=True, exist_ok=True)
    try:
        import pgserver

        if fresh:
            url = build_store(home, a)
        else:
            url = pgserver.get_server(home / "pg", cleanup_mode="stop").get_uri("bench")
        catch = Catch()
        logging.getLogger("desk.api").addHandler(catch)
        logging.getLogger("desk.api").setLevel(logging.INFO)
        app = create_app(url)
        out: dict = {"positions": a.positions, "runs": a.runs}
        out["cold"] = step(app, url, "cold", catch, a.check)
        with store.connect(url) as con:
            newest = con.execute("SELECT max(run_id) FROM run WHERE status = 'complete'").fetchone()[0]
        r1 = clone_run(url, newest, intraday=True)  # the next weekday's first intraday run
        out["intraday 1"] = step(app, url, "intraday 1", catch, a.check)
        with store.connect(url) as con:
            day = con.execute("SELECT business_date FROM run WHERE run_id = %s", (r1,)).fetchone()[0]
        clone_run(url, r1, intraday=True, day=day)  # a later cut of the same date
        out["intraday 2"] = step(app, url, "intraday 2", catch, a.check)
        with store.connect(url) as con:  # an earlier date recomputed: its new end-of-day run
            old = con.execute("SELECT run_id FROM run WHERE run_type = 'EOD' AND status = 'complete'"
                              " ORDER BY business_date DESC OFFSET 20 LIMIT 1").fetchone()[0]
        clone_run(url, old, intraday=False)
        out["recompute"] = step(app, url, "recompute", catch, a.check)
        with psycopg.connect(url, row_factory=dict_row, autocommit=True) as con:
            shown = resolve_run(con, None, None)
        clone_run(url, shown["run_id"], intraday=True, day=shown["business_date"])  # and a new intraday run after it
        out["intraday after recompute"] = step(app, url, "intraday after recompute", catch, a.check)
        print(json.dumps(out, indent=1, ensure_ascii=False))
    finally:
        if not a.keep:
            with contextlib.suppress(Exception):
                import pgserver

                pgserver.get_server(home / "pg", cleanup_mode="stop").cleanup()
            shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
