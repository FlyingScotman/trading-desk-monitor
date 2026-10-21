"""Synthetic benchmark of the concept pages' data at the owner's size (R23): a year of runs of a big book.

    backend/.venv/bin/python backend/tools/bench_concepts.py [--positions 600] [--books 60] [--runs 200]
                                                             [--bonds 3000] [--static 4500] [--serve PORT]

Dev only (needs the ``dev`` extra: pgserver, an embedded PostgreSQL). Builds in a temporary folder a store with:

* one real EOD run of the fixture's bonds (``cli.eod_run``), the template: its facts, PnL and references;
* ``--runs`` default EOD runs on consecutive weekdays up to 2026-10-15, each ``--positions`` positions in
  ``--books`` books, cloned from the template's facts (face, PV and risk drifting by date), each position on a
  bond of its own (``SYN0000``...);
* market history (``md_kbd``, ``md_bond_eod``, ``md_instrument``) of ``--bonds`` bonds over the runs' dates and 60
  dates before (the position bonds among them), the fixture's KBD;
* when the code has them (R23): the static facts of a synthetic static snapshot of ``--static`` bonds with a year
  of coupons, amortizations and offers (``static_facts.load_static``) and the fixture's CBR meetings.

Then it serves the API on 127.0.0.1 and times each data request twice (cold: the first after start; warm: the next,
ETag-less) with its size, raw and gzip, and prints them as JSON. ``--serve PORT`` keeps serving on PORT afterwards
(for ``prototypes/concepts/tools/tti.js``); Ctrl+C stops it. Run it on an older checkout (``PYTHONPATH`` to that
checkout's ``backend/src``) for the numbers before a change. Synthetic data only.
"""

from __future__ import annotations

import argparse
import copy
import gzip
import inspect
import json
import random
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(1, str(BACKEND / "tests"))  # after any PYTHONPATH: an older checkout's desk wins

import psycopg
from psycopg.types.json import Jsonb

from desk import cli, store
from desk.api.app import create_app
from desk.marketdata.bundle import SnapshotDir, read_bundle
from desk.orchestrator import prepare
from desk.settings import Settings

FIX = BACKEND / "tests" / "fixtures" / "mdhub_v1"
DATE = "2026-10-15"


def weekdays(end: str, n: int) -> list[date]:
    """``n`` weekdays ending on ``end``, oldest first."""
    out, d = [], date.fromisoformat(end)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def template(tmp: Path, url: str) -> int:
    import shutil

    cache = tmp / "cache"
    cache.mkdir()
    for p in FIX.glob("*.json.gz"):
        shutil.copy(p, cache / p.name)
    secids = sorted(prepare(SnapshotDir(cache), DATE).static.bonds)
    book = tmp / "book.csv"
    book.write_text("position_id,book_id,secid,quantity_bonds\n" + "".join(
        f"T{i:02d},TPL,{s},{1000 + 250 * i}\n" for i, s in enumerate(secids)), encoding="utf-8")
    kw = {"read_model": False} if "read_model" in inspect.signature(cli.eod_run).parameters else {}
    rid = cli.eod_run(Settings(home=tmp / "home", snapshot_cache=cache), DATE, store_url=url, book=str(book),
                      out=lambda s: None, **kw)
    assert rid
    return rid


def scale_fact(f: dict, k: float, pid: str, book: str, secid: str) -> dict:
    f = copy.deepcopy(f)
    f["position_id"], f["book_id"], f["instrument_id"] = pid, book, secid
    for key in ("notional", "notional_rub", "mv", "mv_rub", "dv01", "cs01", "ofz_spr01", "accrued"):
        if isinstance(f.get(key), (int, float)):
            f[key] *= k
    for c, bk in (f.get("risk") or {}).items():
        f["risk"][c] = {b: v * k for b, v in bk.items()}
    if f.get("extra", {}).get("cashflows"):
        f["extra"]["cashflows"] = [[d, kind, a * k, e] for d, kind, a, e in f["extra"]["cashflows"]]
    return f


def runs(con: psycopg.Connection, tpl: int, n_runs: int, n_pos: int, n_books: int) -> list[date]:
    try:
        from desk.publish import summary
    except ImportError:
        summary = None
    run = con.execute("SELECT * FROM run WHERE run_id = %s", (tpl,)).fetchone()
    cols = [d.name for d in con.execute("SELECT * FROM run LIMIT 0").description]
    rows = con.execute("SELECT * FROM run_position WHERE run_id = %s AND fact IS NOT NULL ORDER BY ord", (tpl,)).fetchall()
    pcols = [d.name for d in con.execute("SELECT * FROM run_position LIMIT 0").description]
    refs = con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s", (tpl,)).fetchall()
    rec = dict(zip(cols, run))
    tf = [dict(zip(pcols, r)) for r in rows]
    dates = weekdays(DATE, n_runs)
    rnd = random.Random(7)
    base = [(f"P{i:04d}", f"B{i % n_books:02d}", f"SYN{i:04d}", tf[i % len(tf)], 0.5 + rnd.random() * 3) for i in range(n_pos)]
    skip = {"run_id", "status", "completed_at", "started_at", "coverage", "totals", "error"}
    for j, d in enumerate(dates):
        vals = {k: v for k, v in rec.items() if k not in skip}
        vals.update(business_date=d, prev_date=dates[j - 1] if j else None,
                    market_snapshot_id=f"{j:064x}", prev_market_snapshot_id=f"{j - 1:064x}" if j else None)
        vals = {k: Jsonb(v) if isinstance(v, (dict, list)) and k != "position_set_ids" else v for k, v in vals.items()}
        rid = con.execute(f"INSERT INTO run ({', '.join(vals)}) VALUES ({', '.join(['%s'] * len(vals))}) RETURNING run_id",
                          list(vals.values())).fetchone()[0]
        drift = 1 + 0.3 * (j / n_runs) - 0.15
        with con.cursor() as cur, cur.copy("COPY run_position (run_id, position_id, ord, book_id, secid, quantity, quality,"
                                           " reason, pv_rub, dv01_rub, cs01_rub, fact, pnl, no_pnl) FROM STDIN") as cp:
            for i, (pid, book, secid, t, k0) in enumerate(base):
                k = k0 * drift * (1.25 if (i + j) % 37 == 0 else 1.0)  # an occasional trade
                f = scale_fact(t["fact"], k, pid, book, secid)
                pnl = {c: 1e3 * k * (rnd.random() - 0.4) for c in ("carry", "rolldown", "rates", "spread", "other")} \
                    if j else None  # the first date has no previous close
                cp.write_row((rid, pid, i, book, secid, (t["quantity"] or 0) * k, t["quality"], t["reason"],
                              f.get("mv_rub"), f.get("dv01"), f.get("cs01"), json.dumps(f),
                              json.dumps(pnl) if pnl else None, None))
        for kind, data in refs:
            con.execute("INSERT INTO run_reference (run_id, kind, data) VALUES (%s, %s, %s)", (rid, kind, Jsonb(data)))
        con.execute("UPDATE run SET status = 'complete', completed_at = now() WHERE run_id = %s", (rid,))
        if summary is not None:  # as desk eod does right after the publication (R23)
            summary.ensure(con, [rid])
    return dates


def market(con: psycopg.Connection, dates: list[date], n_bonds: int) -> None:
    mk = read_bundle(next(p for p in FIX.glob("*.json.gz") if read_bundle(p).kind == "market"))
    z = mk.data["zcyc_params"][0]
    allw = weekdays(dates[-1].isoformat(), len(dates) + 60)
    rnd = random.Random(11)
    with con.transaction():
        for t in ("md_bond_eod", "md_kbd", "md_instrument"):  # the template run's own date
            con.execute("DELETE FROM " + t)
        for d in allw:
            con.execute("INSERT INTO md_kbd (business_date, snapshot_id, known_at, trade_date, params) VALUES"
                        " (%s, %s, now(), %s, %s)", (d, f"m{d}", d, Jsonb(z["params"])))
        with con.cursor() as cur, cur.copy("COPY md_instrument (secid, isin, name, short_name, bond_class, coupon_type,"
                                           " face_unit, maturity_date, is_perpetual, business_date, snapshot_id)"
                                           " FROM STDIN") as cp:
            for i in range(n_bonds):
                cp.write_row((f"SYN{i:04d}", f"RU000SYN{i:04d}", f"Синтетика {i}", f"Синт{i}",
                              "ofz_pd" if i % 10 == 0 else "corporate", "floating" if i % 4 == 1 else "fixed",
                              "USD" if i % 25 == 3 else "RUB", date(2027 + i % 12, 1 + i % 12, 15), False,
                              dates[-1], "static"))
        with con.cursor() as cur, cur.copy("COPY md_bond_eod (business_date, secid, board, clean_pct, price_basis,"
                                           " yield_pct, yield_date, duration_days, gspread_bp, zspread_bp, value,"
                                           " currency_id, num_trades) FROM STDIN") as cp:
            for i in range(n_bonds):
                px, y = 95 + rnd.random() * 10, 12 + rnd.random() * 8
                for d in allw:
                    px += (rnd.random() - 0.5) * 0.3
                    y += (rnd.random() - 0.5) * 0.05
                    cp.write_row((d, f"SYN{i:04d}", "TQCB", px, "legal_close", y, None, 400 + i % 900,
                                  100 + rnd.random() * 200, 120 + rnd.random() * 200, rnd.random() * 1e8, "SUR", 10))


def static(con: psycopg.Connection, n: int, as_of: date) -> str | None:
    try:
        from desk.marketdata import static_facts
        from desk.marketdata.bundle import Snapshot
    except ImportError:
        return None
    st = read_bundle(next(p for p in FIX.glob("*.json.gz") if read_bundle(p).kind == "static"))
    mk = read_bundle(next(p for p in FIX.glob("*.json.gz") if read_bundle(p).kind == "market"))
    rnd = random.Random(3)
    sectors = list(static_facts.SECTOR_LABELS)
    issuers = [{"issuer_id": k, "name": f"ПАО Эмитент {k}", "short_name": f"Эмитент {k}", "inn": None,
                "sector": sectors[k % len(sectors)], "group_name": f"Группа {k // 4}" if k % 3 else None,
                "group_id": f"g{k // 4}", "is_sovereign": k == 0, "country": "RU"} for k in range(600)]
    inst, cpns, reds, offers = [], [], [], []
    for i in range(n):
        fx = i % 25 == 3
        mat = as_of + timedelta(days=60 + int(rnd.random() * 3650))
        cls = "ofz_pd" if i % 10 == 0 else "corporate"
        inst.append({"secid": f"SYN{i:04d}", "isin": f"RU000SYN{i:04d}", "name": f"Синтетика {i}", "short_name": f"Синт{i}",
                     "lat_name": f"Synth {i}", "bond_class": cls, "coupon_type": "floating" if i % 4 == 1 else "fixed",
                     "face_unit": ["USD", "CNY", "EUR"][i % 3] if fx else "RUB", "currency_id": "SUR",
                     "face_value": "1000", "issue_size": str(int(1e5 + rnd.random() * 3e7)), "status": "active",
                     "issuer_id": 0 if cls.startswith("ofz") else i % 600, "maturity_date": mat.isoformat(),
                     "coupon_frequency": 2 if cls.startswith("ofz") else 4, "is_perpetual": False})
        step = 182 if cls.startswith("ofz") else 91
        d = mat
        no = 60
        while d > as_of - timedelta(days=200):
            cpns.append({"secid": f"SYN{i:04d}", "coupon_no": no, "start_date": (d - timedelta(days=step)).isoformat(),
                         "pay_date": d.isoformat(), "amount_per_bond": None if (i % 4 == 1 and d > as_of + timedelta(days=90))
                         else f"{20 + i % 30}.5", "rate_pct": "12.5", "face_value": "1000",
                         "status": "known" if d <= as_of + timedelta(days=90) else "unknown"})
            d -= timedelta(days=step)
            no -= 1
        reds.append({"secid": f"SYN{i:04d}", "date": mat.isoformat(), "kind": "maturity", "value_per_bond": "1000"})
        if i % 7 == 0:
            offers.append({"secid": f"SYN{i:04d}", "offer_date": (as_of + timedelta(days=30 + i % 300)).isoformat(),
                           "type": "put", "status": "scheduled"})
    doc = {"schema_version": "market_snapshot/1.1", "kind": "static", "cut_type": "daily", "business_date": as_of.isoformat(),
           "data": {**st.data, "instruments": inst, "coupon_periods": cpns, "redemptions": reds, "offers": offers,
                    "issuers": issuers}}
    snap = Snapshot("s" * 64, "static", "daily", as_of.isoformat(), None, doc)
    t0 = time.perf_counter()
    static_facts.load_static(con, snap)
    static_facts.load_cbr(con, mk)
    return f"{time.perf_counter() - t0:.1f} s"


def fetch(url: str, gz: bool = True) -> tuple[float, int, int, int]:
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"} if gz else {})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=600) as r:
        body = r.read()
        enc = r.headers.get("Content-Encoding")
        st = r.status
    dt = time.perf_counter() - t0
    raw = gzip.decompress(body) if enc == "gzip" else body
    return dt, len(raw), len(body), st


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--positions", type=int, default=600)
    ap.add_argument("--books", type=int, default=60)
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--bonds", type=int, default=3000)
    ap.add_argument("--static", type=int, default=4500)
    ap.add_argument("--serve", type=int, default=0)
    ap.add_argument("--profile", action="store_true", help="profile the history and extras builds, then stop")
    a = ap.parse_args()
    import pgserver
    import uvicorn

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        srv = pgserver.get_server(tmp / "pg", cleanup_mode="stop")
        srv.psql("CREATE DATABASE bench;")
        url = srv.get_uri("bench")
        store.migrate(url)
        t0 = time.perf_counter()
        tpl = template(tmp, url)
        with store.connect(url) as con:
            dates = runs(con, tpl, a.runs, a.positions, a.books)
            market(con, dates, max(a.bonds, a.positions))
            st = static(con, a.static, dates[-1])
            con.execute("ANALYZE")
            size = con.execute("SELECT pg_size_pretty(pg_database_size(current_database()))").fetchone()[0]
        build_s = time.perf_counter() - t0
        if a.profile:
            import cProfile
            import pstats

            from psycopg.rows import dict_row

            from desk.api import compat, concepts
            with psycopg.connect(url, row_factory=dict_row, autocommit=True) as con:
                run = compat.resolve_run(con)
                for fn in (lambda: concepts.positions_history(con, run), lambda: concepts.extras(con, run)):
                    pr = cProfile.Profile()
                    pr.enable()
                    fn()
                    pr.disable()
                    pstats.Stats(pr).sort_stats("cumulative").print_stats(18)
            return 0
        port = a.serve or 8197
        app = create_app(url)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        while not server.started:
            time.sleep(0.1)
        base = f"http://127.0.0.1:{port}"
        out = {"positions": a.positions, "books": a.books, "runs": len(dates), "bonds": a.bonds, "static_bonds": a.static,
               "build_s": round(build_s, 1), "store": size, "static_facts": st, "requests": {}}
        for name, path in (("desk-data.js", "/desk-data.js"), ("concepts-data.js", "/concepts-data.js"),
                           ("history/positions", "/api/v1/history/positions"),
                           ("calendar/market", "/api/v1/calendar/market"),
                           ("calendar drill", "/api/v1/calendar/market?market=rub&kind=red")):
            res = []
            for _ in range(2):
                try:
                    dt, raw, wire, status = fetch(base + path)
                except Exception as e:  # noqa: BLE001 - an endpoint the older code lacks
                    res.append({"error": str(e)[:80]})
                    break
                res.append({"s": round(dt, 3), "kb": raw // 1024, "gzip_kb": wire // 1024, "status": status})
            out["requests"][name] = {"cold": res[0], "warm": res[1] if len(res) > 1 else None}
        try:  # R23: serve builds the newest run's bodies ahead (a new run invalidates the cache)
            from desk.api.app import BodyCache, warm_bodies

            app.state.body_cache = BodyCache()
            t1 = time.perf_counter()
            built = warm_bodies(app)
            out["warm_up"] = {"s": round(time.perf_counter() - t1, 2), "built": built}
            for name, path in (("concepts-data.js", "/concepts-data.js"), ("history/positions", "/api/v1/history/positions")):
                dt, raw, wire, status = fetch(base + path)
                out["requests"][name]["after_warm_up"] = {"s": round(dt, 3), "status": status}
        except ImportError:
            pass
        print(json.dumps(out, indent=1, ensure_ascii=False), flush=True)
        if a.serve:
            print(f"serving on {base}/concepts/ (Ctrl+C stops)", flush=True)
            try:
                th.join()
            except KeyboardInterrupt:
                pass
        server.should_exit = True
    return 0


if __name__ == "__main__":
    sys.exit(main())
