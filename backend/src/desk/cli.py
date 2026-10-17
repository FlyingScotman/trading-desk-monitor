"""``desk`` command line (slices S1, S2).

    desk eod --snapshots .devdata/snapshots --date 2026-10-15 --book examples/demo_book.csv \\
             --db ../.devdata/tdm.sqlite --report-dir ../.devdata/reports

``desk eod`` attaches the day-over-day flash PnL when the folder has a snapshot of an earlier
date (``--no-pnl`` turns it off).
    desk demo-book --snapshots .devdata/snapshots --date 2026-10-15 --out examples/demo_book.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from .marketdata.adapter import MarketView
from .marketdata.bundle import SnapshotDir
from .orchestrator import attach_pnl, prepare, value
from .publish import tdm_run
from .report import validation


def read_book(path: Path) -> list[tuple[str, str, str, float]]:
    with path.open(encoding="utf-8") as f:
        return [(r["position_id"], r["book_id"], r["secid"], float(r["quantity_bonds"]))
                for r in csv.DictReader(f) if not r["position_id"].startswith("#")]


def cmd_eod(a: argparse.Namespace) -> int:
    sd = SnapshotDir(Path(a.snapshots))
    t0 = time.perf_counter()
    ctx = prepare(sd, a.date)
    prev_date = sd.previous_market_date(a.date)
    prev = MarketView(sd.market(prev_date)) if prev_date else None
    print(f"market {ctx.market.snapshot.id[:12]}  static {ctx.static.snapshot.id[:12]}  "
          f"KBD check {ctx.kbd_check_bp:.4f} bp  previous {prev_date}")

    if a.report_dir:
        universe = [(s, 1.0) for s, t in ctx.static.bonds.items() if t.status == "active"]
        rows, summary = validation.build(ctx, value(ctx, universe))
        path = validation.write(Path(a.report_dir), ctx, rows, summary)
        priced = sum(r["status"] == "priced" for r in rows)
        print(f"universe: {len(rows)} instruments, {priced} priced; report {path}")
        for cls, c in list(summary["by_class"].items())[:8]:
            print(f"  {cls:<18} priced {c['priced']:>5}/{c['instruments']:<5} ok {c['ok']:>4} warn {c['warn']:>4} "
                  f"mismatch {c['mismatch']:>4}  median |dy| {c['median_abs_d_y_bp']} bp")

    if a.book:
        book = read_book(Path(a.book))
        requests = [(secid, qty) for _, _, secid, qty in book]
        outcomes = value(ctx, requests, second_order=not a.no_gamma)
        if prev_date and not a.no_pnl:
            prev_ctx = prepare(sd, prev_date)
            attach_pnl(prev_ctx, value(prev_ctx, requests), ctx, outcomes)
            flash = [o.pnl for o in outcomes if o.pnl]
            print(f"flash PnL {prev_date} -> {a.date}: {len(flash)}/{sum(o.valuation is not None for o in outcomes)} "
                  f"priced positions, total {sum(p.total for p in flash):,.0f} RUB, "
                  f"max identity error {max((abs(p.identity_error) for p in flash), default=0):.2e} RUB")
        positions = [(pid, bk, o) for (pid, bk, _, _), o in zip(book, outcomes)]
        run_id, status, warnings = tdm_run.publish(a.db, ctx, positions, prev=prev)
        ok = sum(o.valuation is not None for o in outcomes)
        print(f"book: {ok}/{len(book)} positions priced; tdm run {run_id}: {status}, {len(warnings)} warnings")
    print(f"done in {time.perf_counter() - t0:.1f} s")
    return 0


def cmd_demo_book(a: argparse.Namespace) -> int:
    """Synthetic book over real instruments: the most traded OFZ-PD and fixed corporates of the date,
    plus a few instruments S1 cannot price yet (to show coverage). Quantities are synthetic."""
    sd = SnapshotDir(Path(a.snapshots))
    ctx = prepare(sd, a.date)
    act = [t for t in ctx.static.bonds.values() if t.status == "active"]

    def traded(cls: str, coupon: tuple[str, ...]) -> list[str]:
        cands = []
        for t in act:
            q = ctx.market.quote(t)
            if t.bond_class == cls and t.coupon_type in coupon and q and q.source_analytics:
                y, px = q.source_analytics.get("yield_close_pct"), q.prices_pct.get("waprice")
                if cls == "corporate" and (y is None or float(y) > 35 or px is None or float(px) < 80):
                    continue  # keep distressed names out of the demo credit book
                cands.append((float(q.prices_pct.get("waprice", 0)) * (q.num_trades or 0), t.secid))
        return [s for _, s in sorted(cands, reverse=True)]

    ofz = traded("ofz_pd", ("fixed",))[:12]
    corp_all = traded("corporate", ("fixed", "variable_known"))
    corp = [s for s, o in zip(corp_all, value(ctx, [(s, 1.0) for s in corp_all])) if o.valuation][:16]
    extra = traded("ofz_pk", ("floating",))[:2] + traded("ofz_in", ("indexed_face", "fixed"))[:1] \
        + traded("corporate", ("floating",))[:2]
    rows = []
    for i, s in enumerate(ofz):
        qty = (1_500_000 + 250_000 * (i % 5)) * (-1 if i in (3, 8) else 1)
        rows.append((f"OFZ-{i + 1:02d}", "OFZ", s, qty))
    for i, s in enumerate(corp):
        rows.append((f"CRP-{i + 1:02d}", "CORP", s, 150_000 + 50_000 * (i % 4)))
    for i, s in enumerate(extra):
        rows.append((f"NXT-{i + 1:02d}", "OFZ" if s.startswith("SU") else "CORP", s, 300_000))
    with Path(a.out).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["position_id", "book_id", "secid", "quantity_bonds"])
        w.writerow([f"# synthetic demo book over public instruments of {a.date}; quantities are invented", "", "", "0"])
        w.writerows(rows)
    print(f"{len(rows)} positions -> {a.out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eod", help="EOD run of one date from hub snapshot files")
    e.add_argument("--snapshots", required=True, help="folder with <snapshot_id>.json.gz bundles")
    e.add_argument("--date", required=True)
    e.add_argument("--book", help="positions CSV (position_id, book_id, secid, quantity_bonds)")
    e.add_argument("--db", default=str(Path(__file__).resolve().parents[3] / "data" / "tdm.sqlite"))
    e.add_argument("--report-dir", help="write the validation report of the whole universe here")
    e.add_argument("--no-gamma", action="store_true", help="skip the Hessian")
    e.add_argument("--no-pnl", action="store_true", help="skip the flash PnL against the previous date")
    e.set_defaults(fn=cmd_eod)
    d = sub.add_parser("demo-book", help="generate the synthetic demo book")
    d.add_argument("--snapshots", required=True)
    d.add_argument("--date", required=True)
    d.add_argument("--out", required=True)
    d.set_defaults(fn=cmd_demo_book)
    a = ap.parse_args(argv)
    return int(a.fn(a))


if __name__ == "__main__":
    sys.exit(main())
