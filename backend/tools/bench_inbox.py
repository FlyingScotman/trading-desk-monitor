"""Synthetic benchmark of the inbox scan (R22): a folder like the owner's, a slow-to-import adapter, a big static.

    backend/.venv/bin/python backend/tools/bench_inbox.py [--files 2500] [--lookback 10] [--import-s 1.5]
                                                          [--instruments 20000] [--keep DIR]

Dev only (needs the ``dev`` extra: pgserver, an embedded PostgreSQL). Builds in a temporary folder:

* a snapshot cache with a static snapshot per business day of the last ~6 weeks, each with ``--instruments``
  synthetic bonds plus the fixture's (the instruments index the adapter reads grows with them);
* an adapter package that sleeps ``--import-s`` on import (heavy imports on Windows) and opens each file in
  ``match`` to sniff its first line, like a real one telling report formats apart;
* ``--files`` report files: old-format reports over two years (declined by ``match``), position reports of past
  dates (behind the lookback), other reports of recent days (declined) and one position report per recent business
  day (40 books x 25 rows), each with the modification time of its report date.

Then two scans (cold: every file new; idle: nothing new) on a fresh store, and prints their times and the file
statuses as JSON. Synthetic data only.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import random
import shutil
import sys
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND / "src"))

from desk import store
from desk.inbox import scan
from desk.inbox.timesem import MSK
from desk.settings import Settings

FIX = BACKEND / "tests" / "fixtures"
STATIC = FIX / "mdhub_v1" / "4f173275220b492c558acad982351bac01fe73baea6d50dcb0fcc38400953c4a.json.gz"
BOOKS, ROWS = 40, 25

ADAPTER = '''
import csv, os, re, time
time.sleep({import_s})  # heavy imports (pandas, openpyxl, ...) on a work PC
MATCH_S = {match_s}
SPI_VERSION = 1
NAME = re.compile(r"^positions_([0-9]{{4}})([0-9]{{2}})([0-9]{{2}})(?:_([0-9]+))?\\.csv$")

def describe():
    return {{"name": "bench_adapter", "version": "0.0.1", "spi": 1, "contract": "private/1.0",
            "sources": [{{"source": "positions_daily", "dataset": "positions", "time_basis": "start_of_day",
                         "completeness": "full_snapshot", "scope": ["book_id"], "covers": "all_active_books"}}]}}

def _records(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f, delimiter=";")
        head = next(r, [])
        for values in r:
            rec = {{k: v for k, v in zip(head, values) if v}}
            if rec:
                yield r.line_num, rec

def match(path, ctx):
    with open(path, "rb") as f:  # sniff the format, as a real adapter does
        first = f.read(256)
    if MATCH_S:
        time.sleep(MATCH_S)  # a network share, a workbook opened to read its header
    if first.startswith(b"OLDFMT"):
        return None
    m = NAME.match(os.path.basename(path))
    if not m:
        return None
    return {{"source": "positions_daily", "report_date": f"{{m[1]}}-{{m[2]}}-{{m[3]}}", "date_from": "name",
            "report_ts": None, "seq": int(m[4]) if m[4] else None}}

def read(path, h, ctx):
    rows, books = [], {{}}
    for line, rec in _records(path):
        books[rec["book_id"]] = None
        found = ctx.instruments.lookup("isin", rec["isin"], h["report_date"])
        if found:
            rec["instrument_id"] = found["instrument_id"]
        rows.append({{**rec, "_src": f"R{{line}}"}})
    return {{"rows": rows, "scopes": [{{"book_id": b}} for b in books], "issues": [], "controls": {{}},
            "meta": {{"generated_at": f"{{h['report_date']}}T06:30:00+03:00"}}}}

def reference(ctx):
    return {{"books": [{{**rec, "active": True, "_src": f"R{{line}}"}}
                      for line, rec in _records(os.path.join(ctx.config_dir, "books.csv"))]}}
'''


def business_days(end: date, n: int) -> list[date]:
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def make_cache(folder: Path, days: list[date], n_instr: int) -> list[str]:
    """One static snapshot per day: the fixture's plus ``n_instr`` synthetic bonds (a few change per day)."""
    folder.mkdir(parents=True)
    with gzip.open(STATIC) as f:
        base = json.load(f)
    rnd = random.Random(7)
    synth = [{"secid": f"RU000S{i:06d}", "isin": f"RU000S{i:06d}", "regnumber": f"4B02-{i:05d}-00000-B",
              "name": f"Synthetic bond {i}", "currency_id": "RUB", "bond_class": "corporate",
              "initial_face_value": "1000"} for i in range(n_instr)]
    isins = []
    for k, d in enumerate(days):
        doc = json.loads(json.dumps(base))
        doc["business_date"] = d.isoformat()
        day = synth[:n_instr - len(days) + k]  # a few new issues each day
        if k:
            day = day[:]
            for j in rnd.sample(range(len(day)), 10):  # renamed issues: the index records differ per day
                day[j] = {**day[j], "name": day[j]["name"] + f" v{k}"}
        doc["data"]["instruments"] = base["data"]["instruments"] + day
        raw = json.dumps(doc, ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(raw).hexdigest()
        (folder / f"{sha}.json.gz").write_bytes(gzip.compress(raw, 1))
        isins = [i["isin"] for i in day]
    return isins


def make_inbox(folder: Path, n_files: int, recent: list[date], isins: list[str]) -> dict[str, int]:
    folder.mkdir(parents=True)
    rnd = random.Random(11)
    counts = {"position_recent": 0, "position_old": 0, "other_recent": 0, "old_format": 0}

    def write(name: str, text: str, d: date) -> None:
        p = folder / name
        p.write_text(text, encoding="utf-8")
        t = datetime.combine(d, datetime.min.time(), MSK).timestamp() + 19 * 3600
        os.utime(p, (t, t))

    head = "book_id;position_id;asset_class;source_instrument_id;isin;ccy;quantity\n"
    for d in recent:
        body = "".join(f"B{b:02d};B{b:02d}|{r};bond;S{r};{isins[(b * ROWS + r) % len(isins)]};RUB;{1000 * (r + 1) + d.day}\n"
                       for b in range(BOOKS) for r in range(ROWS))
        write(f"positions_{d:%Y%m%d}.csv", head + body, d)
        counts["position_recent"] += 1
    left = n_files - len(recent)
    n_other, n_pos_old = left // 9, left // 4
    n_old = left - n_other - n_pos_old
    for i in range(n_other):
        d = recent[-1 - i % len(recent)]
        write(f"trades_{d:%Y%m%d}_{i}.csv", "deal;qty\n1;2\n", d)
        counts["other_recent"] += 1
    oldest = recent[0] - timedelta(days=30)
    for i in range(n_pos_old):
        d = oldest - timedelta(days=i % 700)
        write(f"positions_{d:%Y%m%d}_{i // 700 + 1}.csv", head + "B01;B01|1;bond;S1;RU000S000001;RUB;1\n", d)
        counts["position_old"] += 1
    for i in range(n_old):
        d = recent[-1] - timedelta(days=rnd.randrange(730))
        write(f"report_{d:%Y%m%d}_{i}.csv", "OLDFMT;v1\n" + "x;" * 200 + "\n", d)
        counts["old_format"] += 1
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=2500)
    ap.add_argument("--lookback", type=int, default=10)
    ap.add_argument("--import-s", type=float, default=1.5)
    ap.add_argument("--match-ms", type=float, default=0.0, help="extra time match spends per file")
    ap.add_argument("--instruments", type=int, default=20000)
    ap.add_argument("--recent-days", type=int, default=20)
    ap.add_argument("--keep", type=Path)
    a = ap.parse_args()
    import pgserver

    tmp = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="desk-bench-"))
    if a.keep and tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).astimezone(MSK).date()
    recent = business_days(today - timedelta(days=1), a.recent_days)
    t0 = time.perf_counter()
    isins = make_cache(tmp / "cache", business_days(today, 30), a.instruments)
    files = make_inbox(tmp / "inbox", a.files, recent, isins)
    adapter = tmp / "bench_adapter"
    adapter.mkdir()
    (adapter / "__init__.py").write_text(ADAPTER.format(import_s=a.import_s, match_s=a.match_ms / 1000), encoding="utf-8")
    cfg = tmp / "config"
    cfg.mkdir()
    (cfg / "books.csv").write_text("book_id;name;desk;book_type;entity\n"
                                   + "".join(f"B{b:02d};Book {b};D;TRADING;E1\n" for b in range(BOOKS)),
                                   encoding="utf-8")
    setup_s = time.perf_counter() - t0

    srv = pgserver.get_server(tmp / "pg", cleanup_mode="stop")
    try:
        srv.psql("CREATE DATABASE bench;")
        url = srv.get_uri("bench")
        store.migrate(url)
        s = Settings(snapshot_cache=tmp / "cache", inbox_dirs=(tmp / "inbox",), inbox_archive=tmp / "archive",
                     private_adapter=str(adapter), private_adapter_config=cfg, inbox_poll_seconds=0,
                     inbox_lookback_days=a.lookback)
        out: dict = {"files": files, "setup_s": round(setup_s, 1), "lookback_days": a.lookback,
                     "import_s": a.import_s, "match_ms": a.match_ms, "instruments": a.instruments, "workers": scan.read_workers()}
        with store.connect(url) as con:
            for name in ("cold", "idle"):
                t = time.perf_counter()
                r = scan.scan(con, s)
                out[name] = {"seconds": round(time.perf_counter() - t, 1), "files": dict(r.files),
                             "copies": dict(r.copies), "notes": dict(r.notes),
                             "skipped": dict(r.skipped), "revisions": r.revisions}
            out["ledger"] = dict(con.execute(
                "SELECT status, count(*) FROM (SELECT DISTINCT ON (sha256) status FROM inbox_file_status"
                " ORDER BY sha256, status_id DESC) l GROUP BY 1").fetchall())
            out["unread"] = dict(con.execute("SELECT status, count(*) FROM inbox_unread GROUP BY 1").fetchall())
            idx = tmp / "archive" / "instruments.json"
            out["index_mb"] = round(idx.stat().st_size / 2**20, 1) if idx.is_file() else None
            out["position_sets"] = con.execute("SELECT count(*) FROM position_set").fetchone()[0]
            out["issues"] = dict(con.execute("SELECT code, sum(count) FROM inbox_issue GROUP BY 1").fetchall())
            out["file_errors"] = [list(r) for r in con.execute(
                "SELECT status, error_code, error_type, count(*) FROM (SELECT DISTINCT ON (sha256) * FROM"
                " inbox_file_status ORDER BY sha256, status_id DESC) l WHERE error_code IS NOT NULL"
                " OR error_type IS NOT NULL GROUP BY 1, 2, 3").fetchall()]
        print(json.dumps(out, ensure_ascii=False, indent=1))
    finally:
        srv.cleanup()
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
