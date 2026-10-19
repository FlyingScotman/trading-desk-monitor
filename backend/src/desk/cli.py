"""``desk`` command line (slices S1-S3).

    desk hub-check                       hub version, token, latest eod_final and its age
    desk sync --last 5                   pull verified snapshots into the cache (also --date, --from/--to)
    desk eod --date 2026-10-15 --book examples/test_book.csv --report-dir ../.devdata/reports
    desk eod --date 2026-10-15 --sync    pull the date and the one before it first
    desk import-drop                     verified copy from DESK_SNAPSHOT_DROP (hub unreachable)

Defaults for the snapshot folder, the run store and the report folder come from
:mod:`desk.settings` (``DESK_*`` variables or ``backend/.env``); ``desk config`` prints them.

``desk eod`` attaches the day-over-day flash PnL when the folder has a snapshot of an earlier
date (``--no-pnl`` turns it off).
    desk demo-book --snapshots .devdata/snapshots --date 2026-10-15 --out examples/demo_book.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections import Counter
from datetime import UTC, date, datetime
from itertools import islice
from pathlib import Path

from .marketdata import cache
from .marketdata.adapter import MarketView
from .marketdata.bundle import SnapshotDir, SnapshotError
from .marketdata.hub import HubClient, HubError
from .orchestrator import attach_pnl, prepare, value
from .publish import tdm_run
from .report import validation
from .settings import Settings, SettingsError, load


def read_book(path: Path) -> list[tuple[str, str, str, float]]:
    with path.open(encoding="utf-8") as f:
        return [(r["position_id"], r["book_id"], r["secid"], float(r["quantity_bonds"]))
                for r in csv.DictReader(f) if not r["position_id"].startswith("#")]


def _age(ts: str | None) -> str:
    if not ts:
        return "age unknown"
    h = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 3600
    return f"{h:.1f} h old" if h < 48 else f"{h / 24:.1f} days old"


def _print_synced(rows: list[cache.Synced]) -> None:
    for s in rows:
        if s.market_id is None:
            print(f"  {s.business_date}  no market snapshot on the hub")
        else:
            print(f"  {s.business_date}  market {s.market_id[:12]}  static {(s.static_id or '-')[:12]}  "
                  f"published {s.published_at} ({_age(s.published_at)})  "
                  f"{len(s.downloaded)} downloaded, {2 - len(s.downloaded)} cached")


def cmd_eod(a: argparse.Namespace) -> int:
    if a.sync:
        client = HubClient(a.settings)
        dates = [a.date] + list(islice((d for d in cache.hub_dates(client, a.cut) if d < a.date), 1))
        _print_synced(cache.sync(client, Path(a.snapshots), dates, a.cut))
    sd = SnapshotDir(Path(a.snapshots))
    t0 = time.perf_counter()
    ctx = prepare(sd, a.date)
    prev_date = sd.previous_market_date(a.date)
    prev = MarketView(sd.market(prev_date)) if prev_date else None
    prov = sd.provenance(ctx.market.snapshot.id)
    origin = (f"hub {prov['hub']}, published {prov['published_at']} ({_age(prov['published_at'])})" if prov
              else f"local file, known_at {ctx.market.snapshot.doc.get('known_at')}")
    print(f"market {ctx.market.snapshot.id[:12]} ({origin})  static {ctx.static.snapshot.id[:12]}  "
          f"KBD check {ctx.kbd_check_bp:.4f} bp  previous {prev_date}")
    if ctx.point_in_time_warning:
        print(f"WARN {ctx.point_in_time_warning}")

    if a.report_dir:
        universe = [(s, 1.0) for s, t in ctx.static.bonds.items() if t.status == "active"]
        rows, summary = validation.build(ctx, value(ctx, universe))
        path = validation.write(Path(a.report_dir), ctx, rows, summary)
        by_q = Counter(r["quality"] for r in rows)
        print(f"universe: {len(rows)} instruments; " + ", ".join(f"{q} {n}" for q, n in by_q.most_common())
              + f"; report {path}")
        for cls, c in list(summary["by_class"].items())[:8]:
            print(f"  {cls:<18} priced {c['priced']:>5}/{c['instruments']:<5} ok {c['ok']:>4} warn {c['warn']:>4} "
                  f"mismatch {c['mismatch']:>4}  median |dy| {c['median_abs_d_y_bp']} bp")

    if a.book:
        book = read_book(Path(a.book))
        requests = [(secid, qty) for _, _, secid, qty in book]
        pids = [pid for pid, *_ in book]
        outcomes = value(ctx, requests, second_order=not a.no_gamma, position_ids=pids)
        if prev_date and not a.no_pnl:
            prev_ctx = prepare(sd, prev_date)
            attach_pnl(prev_ctx, value(prev_ctx, requests, position_ids=pids), ctx, outcomes)
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

    if a.profile == "test":
        rows = _test_rows(sd, ctx)
        note = (f"# synthetic test book (slice S3) over public instruments of {', '.join(_window(sd, a.date))}; "
                "quantities are invented")
        return _write_book(Path(a.out), note, rows)
    ofz = traded("ofz_pd", ("fixed",))[:12]
    corp_all = traded("corporate", ("fixed", "variable_known"))
    corp = [s for s, o in zip(corp_all, value(ctx, [(s, 1.0) for s in corp_all], fallback=False)) if o.valuation][:16]
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
    return _write_book(Path(a.out), f"# synthetic demo book over public instruments of {a.date}; "
                       "quantities are invented", rows)


def _write_book(out: Path, note: str, rows: list[tuple[str, str, str, float]]) -> int:
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["position_id", "book_id", "secid", "quantity_bonds"])
        w.writerow([note, "", "", "0"])
        w.writerows(rows)
    print(f"{len(rows)} positions -> {out}")
    return 0


def _window(sd: SnapshotDir, d: str, n: int = 5) -> list[str]:
    return [x for x in sd.market_dates() if x <= d][-n:]


def _spread(items: list, n: int) -> list:
    """n items evenly spaced over a sorted list (all of them when there are fewer)."""
    if len(items) <= n:
        return items
    return [items[round(i * (len(items) - 1) / (n - 1))] for i in range(n)]


def _test_rows(sd: SnapshotDir, ctx) -> list[tuple[str, str, str, float]]:
    """Test book of slice S3 (VERTICAL_SLICES B5): books OFZ, CORP, MIXED over the last five cached
    dates. Priced cases: OFZ-PD across the curve, corporates across spreads, amortizers, a put within
    six months, coupons paid inside the window. Coverage cases (listed with a reason, not priced):
    OFZ-PK, a key-rate floater, OFZ-IN, a perpetual, a bond with no quote on one date."""
    d = ctx.business_date
    window = _window(sd, d.isoformat())
    views = [MarketView(sd.market(x)) for x in window]
    act = [t for t in ctx.static.bonds.values() if t.status == "active" and t.currency in (None, "RUB")]
    outcome = {o.secid: o for o in value(ctx, [(t.secid, 1.0) for t in act], fallback=False)}

    def turnover(t) -> float:
        q = ctx.market.quote(t)
        return float(q.prices_pct.get("waprice", 0)) * (q.num_trades or 0) if q else 0.0

    def quoted_all(t) -> bool:
        return all(v.quote(t) is not None for v in views)

    def liquid(pred, n: int = 10**9) -> list:
        """Most traded first, quoted on every date of the window."""
        return sorted((t for t in act if pred(t) and turnover(t) > 0 and quoted_all(t)), key=turnover, reverse=True)[:n]

    def priced(t) -> bool:
        o = outcome[t.secid]
        return (o.valuation is not None and o.valuation.quality == "exact"
                and (t.bond_class != "corporate" or 0 < o.valuation.z_spread_bp < 1500))

    used: set[str] = set()

    def take(ts: list, n: int) -> list:
        out = [t for t in ts if t.secid not in used][:n]
        used.update(t.secid for t in out)
        return out

    ofz = take(_spread(sorted(liquid(lambda t: t.bond_class == "ofz_pd" and priced(t)),
                              key=lambda t: t.maturity_date), 12), 12)
    corp_pool = liquid(lambda t: t.bond_class == "corporate" and not t.offers and not t.is_amortizing and priced(t), 80)
    corp = take(_spread(sorted(corp_pool, key=lambda t: outcome[t.secid].valuation.z_spread_bp), 14), 14)
    ofz_ad = take(liquid(lambda t: t.bond_class == "ofz_ad" and priced(t)), 1)
    amort = take(liquid(lambda t: t.bond_class == "corporate" and t.is_amortizing and priced(t)), 1)
    put = take(liquid(lambda t: priced(t) and any(
        o.type == "put" and o.status == "scheduled" and o.date and 0 < (o.date - d).days <= 183 for o in t.offers)), 2)
    first = date.fromisoformat(window[0])
    coupon = take(liquid(lambda t: priced(t) and t.bond_class in ("ofz_pd", "corporate")
                         and any(first < c.pay_date <= d for c in t.coupons)), 2)
    ofz_pk = take(liquid(lambda t: t.bond_class == "ofz_pk"), 1)
    ofz_in = take(liquid(lambda t: t.bond_class == "ofz_in"), 1)
    key_rate = take(liquid(lambda t: t.bond_class == "corporate"
                           and (t.floater_formula or {}).get("benchmark") == "KEY_RATE"), 1)
    perp = take(liquid(lambda t: t.is_perpetual), 1)
    gap = take(sorted((t for t in act if priced(t) and t.bond_class == "corporate" and turnover(t) > 0
                       and not quoted_all(t)), key=turnover, reverse=True), 1)
    hedge = take(liquid(lambda t: t.bond_class == "ofz_pd" and priced(t)), 3)

    rows: list[tuple[str, str, str, float]] = []

    def add(prefix: str, book: str, ts: list, qty) -> None:
        for t in ts:
            n = sum(r[0].startswith(prefix) for r in rows) + 1
            rows.append((f"{prefix}-{n:02d}", book, t.secid, qty(n)))

    add("OFZ", "OFZ", ofz, lambda i: (1_000_000 + 250_000 * (i % 4)) * (-1 if i in (4, 9) else 1))
    add("OFZ", "OFZ", ofz_ad + ofz_pk + ofz_in, lambda i: 500_000)
    add("CRP", "CORP", corp, lambda i: (100_000 + 50_000 * (i % 4)) * (-1 if i == 7 else 1))
    add("MIX", "MIXED", amort + put + coupon + key_rate + perp + gap, lambda i: 200_000)
    add("MIX", "MIXED", hedge, lambda i: -400_000)
    return rows


def cmd_hub_check(a: argparse.Namespace) -> int:
    client = HubClient(a.settings)
    h = client.health()
    m = h.get("migrations", {})
    print(f"hub      {client.host}: {h.get('status')}, version {h.get('version')}, "
          f"migrations {m.get('applied')}/{m.get('known')} applied, {m.get('pending')} pending")
    latest = next(client.list_snapshots(kind="market", cut_type=a.cut, latest=True), None)
    print("token    accepted")
    if latest is None:
        print(f"desk: the hub has published no {a.cut} market snapshot yet", file=sys.stderr)
        return 1
    print(f"latest   {a.cut} {latest['business_date']}  {latest['snapshot_id'][:12]}  "
          f"published {latest['published_at']} ({_age(latest['published_at'])})")
    return 0 if h.get("status") == "ok" else 1


def cmd_sync(a: argparse.Namespace) -> int:
    client = HubClient(a.settings)
    if a.date:
        dates = [a.date]
    elif a.last:
        dates = list(islice(cache.hub_dates(client, a.cut), a.last))
    else:
        if not (a.date_from and a.date_to):
            print("desk sync: give --date, --last N, or both --from and --to", file=sys.stderr)
            return 2
        dates = []
        for d in cache.hub_dates(client, a.cut):
            if d < a.date_from:
                break
            if d <= a.date_to:
                dates.append(d)
    rows = cache.sync(client, Path(a.snapshots), sorted(dates), a.cut)
    print(f"sync {a.cut} from {client.host} into {a.snapshots}: {len(rows)} dates")
    _print_synced(rows)
    return 0 if rows and all(s.market_id for s in rows) else 1


def cmd_import_drop(a: argparse.Namespace) -> int:
    if not a.drop:
        print("desk import-drop: set DESK_SNAPSHOT_DROP or pass --drop", file=sys.stderr)
        return 2
    imported, rejected = cache.import_drop(Path(a.drop), Path(a.snapshots))
    print(f"imported {len(imported)} bundles from {a.drop}")
    for name, reason in rejected:
        print(f"  rejected {name}: {reason}")
    return 1 if rejected else 0


def cmd_config(a: argparse.Namespace) -> int:
    for key, shown in a.settings.describe():
        print(f"{key:<15} {shown}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        settings: Settings = load()
    except SettingsError as e:
        print(f"desk: {e}", file=sys.stderr)
        return 2
    ap = argparse.ArgumentParser(prog="desk")
    ap.set_defaults(settings=settings)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("eod", help="EOD run of one date from hub snapshot files")
    e.add_argument("--snapshots", default=str(settings.snapshot_cache),
                   help="folder with <snapshot_id>.json.gz bundles (default: DESK_SNAPSHOT_CACHE)")
    e.add_argument("--date", required=True, type=_iso)
    e.add_argument("--book", help="positions CSV (position_id, book_id, secid, quantity_bonds)")
    e.add_argument("--db", default=str(settings.db), help="tdm SQLite store (default: DESK_DB)")
    e.add_argument("--report-dir", default=settings.report_dir and str(settings.report_dir),
                   help="write the validation report of the whole universe here (default: DESK_REPORT_DIR)")
    e.add_argument("--no-gamma", action="store_true", help="skip the Hessian")
    e.add_argument("--no-pnl", action="store_true", help="skip the flash PnL against the previous date")
    e.add_argument("--sync", action="store_true", help="pull the date and the business date before it from the hub")
    e.add_argument("--cut", default="eod_final", help=argparse.SUPPRESS)
    e.set_defaults(fn=cmd_eod)
    h = sub.add_parser("hub-check", help="hub version, token, latest snapshot and its age")
    h.add_argument("--cut", default="eod_final")
    h.set_defaults(fn=cmd_hub_check)
    s = sub.add_parser("sync", help="pull verified snapshots from the hub into the cache")
    s.add_argument("--snapshots", default=str(settings.snapshot_cache), help="cache folder (default: DESK_SNAPSHOT_CACHE)")
    s.add_argument("--date", type=_iso)
    s.add_argument("--from", dest="date_from", type=_iso)
    s.add_argument("--to", dest="date_to", type=_iso)
    s.add_argument("--last", type=int, help="the N latest business dates the hub has published")
    s.add_argument("--cut", default="eod_final", help="cut type (default eod_final)")
    s.set_defaults(fn=cmd_sync)
    i = sub.add_parser("import-drop", help="copy hand-delivered bundles into the cache, verified")
    i.add_argument("--drop", default=settings.snapshot_drop and str(settings.snapshot_drop),
                   help="drop folder (default: DESK_SNAPSHOT_DROP)")
    i.add_argument("--snapshots", default=str(settings.snapshot_cache))
    i.set_defaults(fn=cmd_import_drop)
    d = sub.add_parser("demo-book", help="generate the synthetic demo book")
    d.add_argument("--snapshots", default=str(settings.snapshot_cache))
    d.add_argument("--date", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--profile", choices=("demo", "test"), default="demo",
                   help="demo: the S1 demo book; test: the S3 test book (three books, coverage cases)")
    d.set_defaults(fn=cmd_demo_book)
    c = sub.add_parser("config", help="print the effective settings (token masked)")
    c.set_defaults(fn=cmd_config)
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a))
    except (HubError, SnapshotError) as e:
        print(f"desk: {e}", file=sys.stderr)
        return 1


def _iso(s: str) -> str:
    return date.fromisoformat(s).isoformat()


if __name__ == "__main__":
    sys.exit(main())
