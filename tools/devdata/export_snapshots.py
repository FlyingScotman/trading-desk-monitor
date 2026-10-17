"""Publish market snapshots from a restored hub seed database and export them as bundle files.

    MDHUB_DATABASE_URL=... python tools/devdata/export_snapshots.py OUT_DIR DATE [DATE ...]

For every business DATE (YYYY-MM-DD) it publishes one ``eod_final`` market snapshot (plus the
static snapshot it names) with the hub's own snapshot builder, then writes every published
snapshot as ``OUT_DIR/<snapshot_id>.json.gz`` (the exact bytes the hub serves from
``/v1/snapshots/{id}/bundle``) and ``OUT_DIR/index.json`` (manifest summaries).

Development only. The seed was loaded and backfilled in October 2026, so ``known_at`` is "now"
for every cut: a historical cut built here contains what the hub knows today, not what it knew
on that day. Runs with the interpreter of the dev-data venv, which has the hub installed.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from mdhub.core.timeutil import MSK
from mdhub.db.pool import Database
from mdhub.snapshot.runner import publish_market_snapshot


def main() -> None:
    out = Path(sys.argv[1])
    days = [date.fromisoformat(a) for a in sys.argv[2:]]
    out.mkdir(parents=True, exist_ok=True)
    db = Database(os.environ["MDHUB_DATABASE_URL"])
    known_at = datetime.now(UTC).replace(microsecond=0)
    for d in days:
        cut_ts = datetime.combine(d + timedelta(days=1), time(8, 0), tzinfo=MSK)
        res = publish_market_snapshot(
            db, cut_type="eod_final", business_date=d, cut_ts=cut_ts, known_at=known_at
        )
        print(f"{d}: market {res.market.id[:12]} ({res.market.status}), "
              f"static {res.static.id[:12]} ({res.static.status})", flush=True)
    index = []
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, kind, cut_type, business_date, static_snapshot_id, manifest, payload_gz "
            "FROM ops.published_snapshot ORDER BY kind DESC, business_date, published_at"
        ).fetchall()
    for r in rows:
        (out / f"{r['id']}.json.gz").write_bytes(bytes(r["payload_gz"]))
        index.append({
            "id": r["id"],
            "kind": r["kind"],
            "cut_type": r["cut_type"],
            "business_date": r["business_date"].isoformat(),
            "static_snapshot_id": r["static_snapshot_id"],
            "datasets": {ds["name"]: ds["rows"] for ds in r["manifest"]["datasets"]},
        })
    (out / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(f"exported {len(rows)} snapshots to {out}")


if __name__ == "__main__":
    main()
