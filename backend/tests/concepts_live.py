"""A desk server on fixture data for a browser check of the concept pages on real runs (not a test module):
two default runs (2026-10-14 on the shifted fixture market, 2026-10-15 on the fixture's) of every fixture bond in
three books, 30 earlier dates of market history, an embedded PostgreSQL (pgserver) in a temporary folder.

    cd backend && .venv/bin/python tests/concepts_live.py --port 8199
    node ../prototypes/concepts/tools/smoke.js <out> --base http://127.0.0.1:8199/concepts/

Ctrl+C stops the server and removes the database."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_market_history import DATE, FIX, shifted, weekdays_before, write

from desk import cli, store
from desk.api.app import create_app
from desk.marketdata import history
from desk.marketdata.bundle import SnapshotDir
from desk.orchestrator import prepare
from desk.settings import Settings

PREV = "2026-10-14"


def build(tmp: Path) -> str:
    import shutil

    import pgserver

    server = pgserver.get_server(tmp / "pg", cleanup_mode="stop")
    server.psql("CREATE DATABASE desk_concepts;")
    url = server.get_uri("desk_concepts")
    store.migrate(url)
    cache = tmp / "cache"
    cache.mkdir()
    for p in FIX.glob("*.json.gz"):
        shutil.copy(p, cache / p.name)
    write(cache, shifted(PREV, 1, light=False))
    hist = tmp / "hub"
    for i, d in enumerate(weekdays_before(PREV, 30)):
        write(hist, shifted(d, i + 2, known_at=f"{d}T17:00:00Z"))
    secids = sorted(prepare(SnapshotDir(FIX), DATE).static.bonds)
    settings = Settings(home=tmp / "home", snapshot_cache=cache)
    for d, k in ((PREV, 1.0), (DATE, 1.25)):
        lines = ["position_id,book_id,secid,quantity_bonds"]
        for i, s in enumerate(secids):
            book = "OFZ" if s.startswith("SU") else "CORP" if i % 2 else "CREDIT"
            qty = (1000 + 250 * i) * (k if i % 3 == 0 else 1.0) * (-1 if i == 4 else 1)
            lines.append(f"P{i:02d},{book},{s},{qty:g}")
        book = tmp / f"book{d}.csv"
        book.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out: list[str] = []
        assert cli.eod_run(settings, d, store_url=url, book=str(book), out=out.append), out
    with store.connect(url) as con:
        history.load_cache(con, hist)
    return url


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8199)
    args = ap.parse_args()
    import uvicorn

    with tempfile.TemporaryDirectory() as t:
        url = build(Path(t))
        print(f"serving on http://127.0.0.1:{args.port}/concepts/", flush=True)
        uvicorn.run(create_app(url), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
