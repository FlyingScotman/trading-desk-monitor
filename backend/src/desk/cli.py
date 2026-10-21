"""``desk`` command line (slices S1-S7a).

    desk hub-check                       hub version, token, latest eod_final and its age
    desk sync --last 5                   pull verified snapshots into the cache (also --date, --from/--to)
    desk eod --date 2026-10-15 --book examples/test_book.csv --report-dir ../.devdata/reports
    desk eod --date 2026-10-15 --sync    pull the date and the one before it first
    desk import-drop                     verified copy from DESK_SNAPSHOT_DROP (hub unreachable)
    desk db-migrate                      create or update the desk store (DESK_STORE_URL, else embedded)
    desk load-positions examples/test_book.csv --date 2026-10-15 [--books OFZ,CORP]
    desk runs --date 2026-10-15          the store's runs: status, positions, qualities, PnL
    desk inbox scan                      bank reports of DESK_INBOX_DIRS through the adapter into the store
    desk inbox status [--date D] [--json]          files, units, issues per source and date; book ages
    desk inbox reconcile [--date D] [--json]       per book: report rows -> accepted -> in the run; why rows were lost
    desk inbox retry [--status failed,rejected]    offer files in these statuses again
    desk inbox replay --source S --from D1 --to D2 read archived accepted files again (new revisions if changed)
    desk adapter check [--adapter SPEC] [--dir D] [--stub-ctx] [--json]   the conformance kit (08 §8)
    desk serve                           desk API + Desk Monitor on http://127.0.0.1:8100, scenario worker, autopilot
    desk cycle [--date D] [--no-hub]     one autopilot cycle: hub sync -> inbox scan -> EOD of changed dates
    desk backfill [--from D] [--to D] [--dry-run]  default runs of the past dates with positions (R23)
    desk openapi --out ../frontend/openapi.json   the API's OpenAPI document (stdout without --out)
    desk diagnostics [--out FILE] [--no-hub]       how the desk works here, without private data (R13)

Defaults for the snapshot folder, the run store and the report folder come from
:mod:`desk.settings` (``DESK_*`` variables or ``backend/.env``); ``desk config`` prints them.

``desk eod`` values the books of the desk store for the date (positions carried from the latest
earlier set, from ``desk inbox scan`` or ``desk load-positions``; ``--book FILE`` loads the file for the date
first, an identical file adds nothing; each book's source and age is printed),
attaches the day-over-day flash PnL when the folder has a snapshot of an earlier date (``--no-pnl``
turns it off) and publishes the run into the store; ``--tdm`` also into the tdm SQLite store.
``--books A,B`` values only those books, as a TEST run (never the default run of the date). A load
covers the books of its file (``load-positions --books``: these); a covered book without rows is closed.
    desk demo-book --snapshots .devdata/snapshots --date 2026-10-15 --out examples/demo_book.csv
    desk instruments export --out instruments.xlsx [--date D] [--active] [--value]
                                         the bond universe of a cached static snapshot, no positions (R9)
"""

from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import json
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from itertools import islice
from pathlib import Path
from typing import Any

import psycopg

from . import store
from .marketdata import cache, history, static_facts
from .marketdata.adapter import MarketView
from .marketdata.bundle import SnapshotDir, SnapshotError, previous_market
from .marketdata.hub import HubClient, HubError, hub_check
from .orchestrator import EodContext, attach_pnl, prepare, value
from .pnl import direct as direct_pnl
from .publish import desk_run, tdm_run
from .publish import summary as run_summary
from .report import validation
from .settings import Settings, SettingsError, adapter_config_dir, load, mask_url
from .valuation import positions as pv1


class ContextCache:
    """The ``size`` prepared market dates used last (``orchestrator.prepare``), keyed by the snapshot folder, date, cut and
    snapshot id: ``desk backfill`` values consecutive dates, and each date's market is the next date's previous one.
    A prepared context is read only by the valuation (its curve cache only grows), so sharing it changes nothing."""

    def __init__(self, size: int = 3) -> None:  # a date's own market, its previous one, the next date's
        self.size, self._items = size, {}

    def prepare(self, sd: SnapshotDir, business_date: str, cut_type: str = "eod_final",
                market_snapshot_id: str | None = None) -> EodContext:
        key = (str(sd.root), business_date, cut_type, market_snapshot_id or sd.market_id(business_date, cut_type))
        ctx = self._items.pop(key, None)
        if ctx is None:
            ctx = prepare(sd, business_date, cut_type, market_snapshot_id=market_snapshot_id)
        self._items[key] = ctx
        while len(self._items) > self.size:
            self._items.pop(next(iter(self._items)))
        return ctx


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


def _store(a: argparse.Namespace) -> psycopg.Connection:
    return store.connect(store.resolve_url(a.settings))


def _load(con: psycopg.Connection, path: Path, d: str, books: list[str] | None = None,
          out: Callable[[str], None] = print) -> None:
    rows = read_book(path)
    loaded = store.load_positions(con, rows, valid_date=date.fromisoformat(d), source_name=path.name,
                                  source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                                  recorded_by=getpass.getuser(), books=books)
    out(f"positions {path.name} for {d}: " + ", ".join(
        f"{x.book_id} rev {x.revision}" + ("" if x.changed else " unchanged") for x in loaded))


def cmd_eod(a: argparse.Namespace) -> int:
    if a.books and a.run_type == "EOD":
        print("desk eod: --books values part of the desk, a TEST run; it cannot be the EOD run of the date",
              file=sys.stderr)
        return 2
    run_type = a.run_type or ("TEST" if a.books else "EOD")
    if a.sync:
        client = HubClient(a.settings)
        dates = [a.date] + list(islice((d for d in cache.hub_dates(client, a.cut) if d < a.date), 1))
        _print_synced(cache.sync(client, Path(a.snapshots), dates, a.cut))
    eod_run(a.settings, a.date, snapshots=a.snapshots, book=a.book, books=a.books, run_type=run_type,
            report_dir=a.report_dir, tdm=a.tdm, db=a.db, no_gamma=a.no_gamma, no_pnl=a.no_pnl)
    return 0


def pnl_basis(ctx: EodContext, prev: tuple[str, str] | None) -> dict[str, Any]:
    """What the flash PnL of ``ctx`` is measured against (R11; lineage ``pnl_basis``): ``prev_date``,
    ``prev_cut_type`` (``bundle.previous_market``), ``expected_date`` (the previous business day of the
    settlement calendar) and ``label``: None when it is the expected day's ``eod_final``, else what the pages show
    («PnL от 15.10 (закрытие): нет закрытия 16.10»)."""
    expected: date | None = None
    try:  # core steps forward only: walk back to the previous business day
        d = ctx.business_date
        for _ in range(31):
            d -= timedelta(days=1)
            if ctx.calendar.is_business_day(d):
                expected = d
                break
    except Exception:  # noqa: BLE001 - a date outside the calendar: no expectation, the basis is still named
        expected = None
    out: dict[str, Any] = {"prev_date": prev[0] if prev else None, "prev_cut_type": prev[1] if prev else None,
                           "expected_date": expected.isoformat() if expected else None, "label": None}
    exp = f"{expected:%d.%m}" if expected else "предыдущего рабочего дня"
    if prev is None:
        out["label"] = f"нет PnL: нет закрытия {exp} и более ранних"
    elif prev[1] != "eod_final" or prev[0] != out["expected_date"]:
        pd = date.fromisoformat(prev[0])
        missing = (f"нет закрытия {exp}" if prev[0] != out["expected_date"]
                   else f"нет окончательного закрытия {exp}")
        out["label"] = f"PnL от {pd:%d.%m} ({store.CUT_LABELS[prev[1]]}): {missing}"
    return out


def eod_run(settings: Settings, d: str, *, snapshots: str | Path | None = None, store_url: str | None = None,
            book: str | None = None, books: list[str] | None = None, run_type: str = "EOD",
            report_dir: str | Path | None = None, tdm: bool = False, db: str | Path | None = None,
            no_gamma: bool = False, no_pnl: bool = False, out: Callable[[str], None] = print,
            cut_type: str = "eod_final", market_snapshot_id: str | None = None,
            contexts: ContextCache | None = None, read_model: bool = True) -> int | None:
    """The EOD run of date ``d`` (``desk eod`` without its hub pull; the autopilot calls it too): values the
    books of the store on the cached snapshot of ``d`` and publishes the run. Returns the desk run id, None when
    the store has no positions for the date. Raises on a missing snapshot or a failed publication.

    ``cut_type`` / ``market_snapshot_id``: another market cut of ``d`` than its ``eod_final`` (R11: the autopilot's
    intraday step values today's book on an ``intraday`` cut, ``run_type`` INTRADAY, or on ``eod_preliminary``).
    The flash PnL is measured against the newest earlier ``eod_final`` of the cache, else an ``eod_preliminary``
    (``bundle.previous_market``); when that is not the previous business day's final, the run says so
    (``pnl_basis``).

    ``contexts`` (``desk backfill``, R23): prepared dates kept between calls, so that a run of consecutive dates
    prepares each date's market once (as the run's own date, then as the next date's previous one).
    ``read_model``: also put the run's static snapshot's facts (the market calendar, issuer groups, sectors) and
    the CBR meetings into the store's read model (``desk.marketdata.static_facts``); the backfill leaves them to the
    latest date. After the publication the run's summary for the concept pages is written (``publish.summary``)."""
    snapshots = snapshots or settings.snapshot_cache
    sd = SnapshotDir(Path(snapshots))
    t0 = time.perf_counter()
    prep = contexts.prepare if contexts is not None else prepare
    ctx = prep(sd, d, cut_type, market_snapshot_id=market_snapshot_id)
    prev_cut = previous_market(sd, d)
    prev_date = prev_cut[0] if prev_cut else None
    prev = MarketView(sd.market(*prev_cut)) if prev_cut else None
    basis = pnl_basis(ctx, prev_cut)
    prov = sd.provenance(ctx.market.snapshot.id)
    origin = (f"hub {prov['hub']}, published {prov['published_at']} ({_age(prov['published_at'])})" if prov
              else f"local file, known_at {ctx.market.snapshot.doc.get('known_at')}")
    cut = "" if ctx.market.snapshot.cut_type == "eod_final" else \
        f" [{ctx.market.snapshot.cut_type} {ctx.market.snapshot.doc.get('cut_ts')}]"
    out(f"market {ctx.market.snapshot.id[:12]}{cut} ({origin})  static {ctx.static.snapshot.id[:12]}  "
        f"KBD check {ctx.kbd_check_bp:.4f} bp  previous {prev_date}"
        + (f" ({prev_cut[1]})" if prev_cut and prev_cut[1] != "eod_final" else ""))
    if basis["label"] and prev_cut:
        out(f"WARN flash PnL basis: {basis['label']}")
    if ctx.point_in_time_warning:
        out(f"WARN {ctx.point_in_time_warning}")

    if report_dir:
        universe = [(s, 1.0) for s, t in ctx.static.bonds.items() if t.status == "active"]
        rows, summary = validation.build(ctx, value(ctx, universe))
        path = validation.write(Path(report_dir), ctx, rows, summary)
        by_q = Counter(r["quality"] for r in rows)
        out(f"universe: {len(rows)} instruments; " + ", ".join(f"{q} {n}" for q, n in by_q.most_common())
            + f"; report {path}")
        for cls, c in list(summary["by_class"].items())[:8]:
            out(f"  {cls:<18} priced {c['priced']:>5}/{c['instruments']:<5} ok {c['ok']:>4} warn {c['warn']:>4} "
                f"mismatch {c['mismatch']:>4}  median |dy| {c['median_abs_d_y_bp']} bp")

    with store.connect(store_url or store.resolve_url(settings)) as con:
        for rid in store.reap_orphans(con):
            out(f"WARN run {rid} was still loading: marked failed")
        try:  # the run's snapshots in the market history (the bond charts); one query each once loaded
            for snap in (ctx.market.snapshot, ctx.static.snapshot, *((prev.snapshot,) if prev else ())):
                history.load_snapshot(con, snap)
        except psycopg.Error as e:
            out(f"WARN market history not updated: {type(e).__name__}: {e}")
        if read_model:
            try:  # the concept pages' market calendar, issuer groups and sectors, CBR meetings (R23)
                static_facts.load_static(con, ctx.static.snapshot)
                static_facts.load_cbr(con, ctx.market.snapshot)
            except psycopg.Error as e:
                out(f"WARN static facts not updated: {type(e).__name__}: {e}")
        if book:
            _load(con, Path(book), d, out=out)
        inputs = store.positions_for(con, date.fromisoformat(d), books=books)
        if not inputs.rows:
            out(f"no positions in the store for {d}" + (f" (books {','.join(books)})" if books else ""))
            out(f"done in {time.perf_counter() - t0:.1f} s")
            return None
        for b, vd in sorted(inputs.valid_dates.items()):
            src = inputs.sources.get(b) or {}
            age = (ctx.business_date - vd).days
            out(f"  {b}: {src.get('source')} {src.get('file_name')}"
                + (f", report {src['report_date']}" if src.get("report_date") else "")
                + f", valid {vd}" + (f", carried {age} days" if age else ""))
        carried = store.carried_spreads(con, before=ctx.business_date)
        bp = pv1.plan(ctx, inputs)
        outcomes = value(ctx, bp.requests, second_order=not no_gamma, position_ids=bp.position_ids,
                         carried_spreads=carried)
        if prev_cut and not no_pnl:
            prev_ctx = prep(sd, *prev_cut)
            attach_pnl(prev_ctx, value(prev_ctx, bp.requests, position_ids=bp.position_ids, carried_spreads=carried),
                       ctx, outcomes)
            # fx rows, the report-valued rows (otc, unmapped bonds): by the rate move and by the reports of both
            # dates (pnl.direct); the reason in run_position.no_pnl when there is none, never a bare null
            prev_inputs = store.positions_for(con, prev_ctx.business_date, books=books)
            direct_pnl.attach(prev_ctx, ctx, bp.direct, inputs, pv1.plan(prev_ctx, prev_inputs).direct)
            flash = [o.pnl for o in outcomes if o.pnl]
            out(f"flash PnL {prev_date} -> {d}: {len(flash)}/{sum(o.valuation is not None for o in outcomes)} "
                f"priced positions, total {sum(p.total for p in flash):,.0f} RUB, "
                f"max identity error {max((abs(p.identity_error) for p in flash), default=0):.2e} RUB")
            dp = [o.pnl for o in bp.direct.values() if o.pnl]
            out(f"PnL without core: {len(dp)}/{sum(o.valuation is not None for o in bp.direct.values())} positions "
                f"(by the rate move or the reports), total {sum(p.total for p in dp):,.0f} RUB")
        if not prev_date:
            out(f"WARN no flash PnL: no eod_final or eod_preliminary market snapshot before {d} in {snapshots}")
        positions = pv1.merge(bp, outcomes)
        ok = sum(o.valuation is not None for _, _, o in positions)
        by_q = Counter(o.valuation.quality if o.valuation else "not_valued" for _, _, o in positions)
        out(f"book: {ok}/{len(positions)} positions valued (" + ", ".join(f"{q} {n}" for q, n in by_q.most_common())
            + f"), {len(carried)} carried spreads")
        run_id, status, warnings = desk_run.publish(con, ctx, positions, prev=prev, run_type=run_type,
                                                    inputs=inputs, carried_from=carried, pnl_basis=basis)
        out(f"desk run {run_id}: {status}, {len(warnings)} warnings")
        try:
            run_summary.ensure(con, [run_id])
        except psycopg.Error as e:  # the pages build it on first read
            out(f"WARN run summary not written: {type(e).__name__}: {e}")
    if tdm:
        tdm_id, status, warnings = tdm_run.publish(db or settings.db, ctx, positions, prev=prev, inputs=inputs)
        out(f"tdm run {tdm_id}: {status}, {len(warnings)} warnings")
    out(f"done in {time.perf_counter() - t0:.1f} s")
    return run_id


def cmd_inbox(a: argparse.Namespace) -> int:
    from .inbox import scan

    with _store(a) as con:
        if a.inbox_cmd == "status":
            st = scan.status(con, a.settings, date.fromisoformat(a.date) if a.date else None)
            print(json.dumps(st, indent=2) if a.json else scan.status_text(st))
            return 0 if st["adapter"].startswith("ok") else 1
        if a.inbox_cmd == "reconcile":
            rc = scan.reconcile(con, a.settings, date.fromisoformat(a.date) if a.date else None)
            print(json.dumps(rc, indent=2, ensure_ascii=False) if a.json else scan.reconcile_text(rc))
            return 0
        if a.inbox_cmd == "scan":
            res = scan.scan(con, a.settings)
        elif a.inbox_cmd == "retry":
            res = scan.retry(con, a.settings, tuple(a.status) if a.status else (*scan.REOFFER, "waiting_calendar"))
        else:
            res = scan.replay(con, a.settings, a.source, date.fromisoformat(a.date_from),
                              date.fromisoformat(a.date_to))
    print(res.text())
    return 0 if res.ok else 1


def cmd_adapter_check(a: argparse.Namespace) -> int:
    from .inbox import conformance

    st = a.settings
    rep = conformance.check(
        a.adapter or st.private_adapter, a.dir or [str(d) for d in st.inbox_dirs], stub_ctx=a.stub_ctx,
        snapshot_dir=str(st.snapshot_cache), config_dir=str(cfg) if (cfg := adapter_config_dir(st)) else None, expected_sha256=None if a.adapter else st.private_adapter_sha256,
        match_timeout_s=st.adapter_match_timeout_s, read_timeout_s=st.adapter_read_timeout_s)
    print(json.dumps(rep.to_json(), indent=2, ensure_ascii=False) if a.json else rep.text())
    return 0 if rep.ok else 1


def cmd_load_positions(a: argparse.Namespace) -> int:
    with _store(a) as con:
        _load(con, Path(a.file), a.date, a.books)
    return 0


def cmd_db_migrate(a: argparse.Namespace) -> int:
    url = store.resolve_url(a.settings)
    applied = store.migrate(url)
    print(f"store {mask_url(url)}: " + (f"applied {', '.join(applied)}" if applied else "up to date"))
    return 0


def cmd_runs(a: argparse.Namespace) -> int:
    with _store(a) as con:
        rows = con.execute("SELECT r.run_id, r.run_type, r.business_date, r.cut_type, r.status, r.coverage, r.totals,"
                           " (SELECT count(*) FROM run_position p WHERE p.run_id = r.run_id), r.error, r.prev_date FROM run r"
                           " WHERE %(d)s::date IS NULL OR r.business_date = %(d)s ORDER BY r.run_id",
                           {"d": a.date}).fetchall()
    for rid, typ, d, cut, status, cov, tot, n, err, prev in rows:
        q = ", ".join(f"{k} {v}" for k, v in (cov or {}).get("_total", {}).items() if v)
        pnl = ((tot or {}).get("_total") or {}).get("pnl", {}).get("total") if prev else None
        print(f"{rid:>5}  {typ:<8} {d}  {cut:<16} {status:<8} {n:>4} pos  {q or '-'}"
              + (f"  PnL {pnl:,.0f} vs {prev}" if pnl is not None else "") + (f"  error: {err}" if err else ""))
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


def cmd_instruments_export(a: argparse.Namespace) -> int:
    from .report import instruments

    ex, summ = instruments.export(Path(a.out), Path(a.snapshots), on=a.date, static_file=a.static_file,
                                  market_file=a.market_file, active_only=a.active, with_value=a.value)
    m = ex.market
    print(f"static {ex.static.id[:12]} of {ex.static.business_date} (known_at {ex.static.doc.get('known_at')}), "
          f"market {m.id[:12] if m else '-'}: {len(ex.rows)} instruments -> {a.out}")
    print(instruments.summary_text(summ))
    return 0


def cmd_hub_check(a: argparse.Namespace) -> int:
    r = hub_check(a.settings, a.cut)
    if h := r["health"]:
        m = h.get("migrations") or {}
        print(f"hub      {r['host']}: {h.get('status')}, version {h.get('version')}, "
              f"migrations {m.get('applied')}/{m.get('known')} applied, {m.get('pending')} pending")
    if r["token_accepted"]:
        print("token    accepted")
    if latest := r["latest"]:
        print(f"latest   {a.cut} {latest['business_date']}  {(latest['snapshot_id'] or '')[:12]}  "
              f"published {latest['published_at']} ({_age(latest['published_at'])})")
    if r["error"]:
        print(f"desk: {r['error']}", file=sys.stderr)
    return 0 if r["ok"] else 1


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
    _load_history(a.settings, Path(a.snapshots))
    return 0 if rows and all(s.market_id for s in rows) else 1


def cmd_import_drop(a: argparse.Namespace) -> int:
    if not a.drop:
        print("desk import-drop: set DESK_SNAPSHOT_DROP or pass --drop", file=sys.stderr)
        return 2
    imported, rejected = cache.import_drop(Path(a.drop), Path(a.snapshots))
    print(f"imported {len(imported)} bundles from {a.drop}")
    for name, reason in rejected:
        print(f"  rejected {name}: {reason}")
    _load_history(a.settings, Path(a.snapshots))
    return 1 if rejected else 0


def _load_history(settings: Settings, folder: Path) -> None:
    """The new bundles of the cache into the store's market history (the bond charts) when a store is set
    (``DESK_STORE_URL``): the embedded one is not started for it. ``desk eod``, ``desk cycle`` and the autopilot load
    them anyway, so a store that cannot be reached is only a note."""
    if not settings.store_url:
        return
    try:
        with store.connect(settings.store_url) as con:
            line = history.load_cache(con, folder).text()
    except psycopg.Error as e:
        print(f"market history not updated ({type(e).__name__}: {e}); the next desk cycle loads it")
        return
    if line:
        print(line)


def cmd_serve(a: argparse.Namespace) -> int:
    from .api import serve

    url = store.resolve_url(a.settings)
    store.migrate(url)
    print(f"desk API on http://{a.host}:{a.port}/desk-monitor.html, store {mask_url(url)}")
    from .autopilot import Autopilot
    from .inbox import scan

    # default: the thread runs and follows the settings' autopilot flag every cycle; --autopilot runs cycles
    # whatever the settings say; --no-autopilot starts no thread
    ap = None if a.autopilot is False else Autopilot(store_url=url, force=bool(a.autopilot))
    if ap is not None:
        on = a.autopilot or a.settings.autopilot
        print(f"autopilot: {'on' if on else 'off in the settings'}, every {a.settings.inbox_poll_seconds:g} s")
    # not 0 when the settings page installs an update: the launcher (start.bat) restarts it (desk.update)
    return serve(url, host=a.host, port=a.port, snapshots=None if a.no_worker else a.settings.snapshot_cache,
                 adapter_status=lambda: scan.adapter_line(scan.adapter(a.settings)), autopilot=ap)


def cmd_cycle(a: argparse.Namespace) -> int:
    from .autopilot import Autopilot

    url = store.resolve_url(a.settings)
    store.migrate(url)
    st = Autopilot(store_url=url, force=True, out=print).run_cycle(a.date or (), hub=not a.no_hub)
    lr = st["latest_run"]
    print(f"{st['message']}; latest run: "
          + (f"{lr['run_id']} of {lr['as_of']} ({lr.get('label')}), {lr['created_at']}" if lr else "none"))
    return 1 if st["state"] == "error" else 0


def cmd_backfill(a: argparse.Namespace) -> int:
    """``desk backfill``: the default runs of the past dates (``desk.backfill``). The runs of one date at a time hold
    the cycle lock (``<home>/cycle.lock``), so a ``desk serve`` running alongside keeps its cycles between them."""
    from . import backfill
    from .autopilot import CycleLock

    st = a.settings
    url = store.resolve_url(st)
    store.migrate(url)
    d_from = backfill.start_date(st, date.fromisoformat(a.date_from) if a.date_from else None)
    d_to = date.fromisoformat(a.date_to) if a.date_to else date.today()
    if d_from > d_to:
        print(f"desk backfill: --from {d_from} is after --to {d_to}", file=sys.stderr)
        return 2
    client = None
    rows = None
    if not a.no_hub and st.hub_url:
        client = HubClient(st)
        rows = backfill.hub_rows(client, d_from)
        print(f"hub {client.host}: {len(rows)} eod_final dates from {rows[-1][0] if rows else '-'}")
    with _store(a) as con:
        if not a.no_inbox and not a.dry_run:
            line = backfill.inbox(con, st, d_from, force=a.rescan)
            print(line or "inbox: reports from the start date read before (--rescan reads them again)")
        p = backfill.plan(con, st, d_from, d_to, rows)
    print(backfill.text(p, dry_run=a.dry_run))
    if a.dry_run or not p.todo:
        return 0
    lock = CycleLock(st.home)
    contexts = ContextCache()
    done = failed = 0
    t0 = time.perf_counter()
    for i, day in enumerate(p.todo, 1):
        waited = False
        while not lock.acquire():
            if not waited:
                print("  waiting for the autopilot's cycle (desk serve) to end ...")
                waited = True
            time.sleep(5)
        try:
            one = backfill.Plan(p.date_from, p.date_to, [day], p.hub)
            res = backfill.run(st, one, store_url=url, client=client, out=print, contexts=contexts)
        finally:
            lock.release()
        done += len(res["done"])
        failed += len(res["failed"])
        for d, err in res["failed"]:
            print(f"  {d} FAILED: {err}")
        for d in res["skipped"]:
            print(f"  {d} skipped: its bundle is not cached and the hub is off")
        el = time.perf_counter() - t0
        print(f"[{i}/{len(p.todo)}] {day.date} done; {el:.0f} s, about {el / i * (len(p.todo) - i):.0f} s left")
    print(f"backfill: {done} dates run, {failed} failed, in {time.perf_counter() - t0:.0f} s")
    return 1 if failed else 0


def cmd_openapi(a: argparse.Namespace) -> int:
    from .api import create_app

    text = json.dumps(create_app("postgresql://unused").openapi(), indent=2, ensure_ascii=False) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


def cmd_diagnostics(a: argparse.Namespace) -> int:
    from . import diagnostics

    doc = diagnostics.build(a.settings, hub=not a.no_hub)
    text = diagnostics.dumps(doc)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"diagnostics written to {a.out} ({len(text.encode()) // 1024 + 1} KB): no positions, books, ISINs "
              "or amounts; send this file to the agent")
    else:
        sys.stdout.write(text)
    return 0


def cmd_config(a: argparse.Namespace) -> int:
    for key, shown in a.settings.describe():
        print(f"{key:<24} {shown}")
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
    e.add_argument("--book", help="positions CSV (position_id, book_id, secid, quantity_bonds), loaded into the "
                   "store for the date first; the load covers the file's books only, other books are kept")
    e.add_argument("--books", type=_list, help="value only these books (A,B): a TEST run")
    e.add_argument("--run-type", choices=("EOD", "TEST"), help="default EOD, TEST with --books")
    e.add_argument("--tdm", action="store_true", help="also publish into the tdm SQLite store (DESK_DB)")
    e.add_argument("--db", default=str(settings.db), help="tdm SQLite store (default: DESK_DB)")
    e.add_argument("--report-dir", default=settings.report_dir and str(settings.report_dir),
                   help="write the validation report of the whole universe here (default: DESK_REPORT_DIR)")
    e.add_argument("--no-gamma", action="store_true", help="skip the Hessian")
    e.add_argument("--no-pnl", action="store_true", help="skip the flash PnL against the previous date")
    e.add_argument("--sync", action="store_true", help="pull the date and the business date before it from the hub")
    e.add_argument("--cut", default="eod_final", help=argparse.SUPPRESS)
    e.set_defaults(fn=cmd_eod)
    lp = sub.add_parser("load-positions", help="load a positions CSV into the store for a date")
    lp.add_argument("file")
    lp.add_argument("--date", required=True, type=_iso)
    lp.add_argument("--books", type=_list, help="the books the file covers (A,B; default: the file's books); "
                    "a covered book without rows in the file is closed for the date")
    lp.set_defaults(fn=cmd_load_positions)
    ib = sub.add_parser("inbox", help="bank reports through the private adapter (08 §13)")
    isub = ib.add_subparsers(dest="inbox_cmd", required=True)
    isub.add_parser("scan", help="take in the new stable files of DESK_INBOX_DIRS").set_defaults(fn=cmd_inbox)
    ist = isub.add_parser("status", help="files, units and issues per source and business date; book ages")
    ist.add_argument("--date", type=_iso, help="business date (default: today's positions)")
    ist.add_argument("--json", action="store_true")
    ist.set_defaults(fn=cmd_inbox)
    irc = isub.add_parser("reconcile", help="per book: rows of the report, accepted, in the default run; the losses")
    irc.add_argument("--date", type=_iso, help="business date (default: the latest default run's)")
    irc.add_argument("--json", action="store_true")
    irc.set_defaults(fn=cmd_inbox)
    irt = isub.add_parser("retry", help="offer files in these statuses again, whatever the adapter fingerprint")
    irt.add_argument("--status", type=_list, help="statuses (default ignored,match_failed,failed,timed_out,"
                     "rejected,waiting_calendar)")
    irt.set_defaults(fn=cmd_inbox)
    irp = isub.add_parser("replay", help="read the archived accepted files of a source again")
    irp.add_argument("--source", required=True)
    irp.add_argument("--from", dest="date_from", required=True, type=_iso, help="first business date")
    irp.add_argument("--to", dest="date_to", required=True, type=_iso, help="last business date")
    irp.set_defaults(fn=cmd_inbox)
    ad = sub.add_parser("adapter", help="the private adapter")
    asub = ad.add_subparsers(dest="adapter_cmd", required=True)
    ack = asub.add_parser("check", help="the conformance kit over report folders, without the store (08 §8)")
    ack.add_argument("--adapter", help="package directory or module (default: DESK_PRIVATE_ADAPTER)")
    ack.add_argument("--dir", action="append", default=[], help="a report folder (repeatable; default: DESK_INBOX_DIRS)")
    ack.add_argument("--stub-ctx", action="store_true", help="every ctx.instruments lookup returns None")
    ack.add_argument("--json", action="store_true")
    ack.set_defaults(fn=cmd_adapter_check)
    m = sub.add_parser("db-migrate", help="apply the desk store migrations")
    m.set_defaults(fn=cmd_db_migrate)
    r = sub.add_parser("runs", help="list the runs of the desk store")
    r.add_argument("--date", type=_iso)
    r.set_defaults(fn=cmd_runs)
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
    ins = sub.add_parser("instruments", help="the instruments of the cached static snapshots (public data)")
    insub = ins.add_subparsers(dest="instruments_cmd", required=True)
    ie = insub.add_parser("export", help="every bond of a static snapshot as the desk sees it, with data-quality "
                          "flags and core eligibility; no positions (CSV UTF-8 with BOM, or .xlsx)")
    ie.add_argument("--out", required=True, help="file to write: .csv or .xlsx")
    ie.add_argument("--date", type=_iso, help="the static snapshot desk eod would use for this date "
                    "(default: the newest cached static snapshot)")
    ie.add_argument("--snapshots", default=str(settings.snapshot_cache), help="cache folder (default: DESK_SNAPSHOT_CACHE)")
    ie.add_argument("--static-file", type=Path, help="export this static bundle instead of the cache's")
    ie.add_argument("--market-file", type=Path, help="with --static-file: the market bundle for the quote columns")
    ie.add_argument("--active", action="store_true", help="active instruments only (default: also recently matured)")
    ie.add_argument("--value", action="store_true", help="also value every active bond through core with the "
                    "date's quote (slow) and add the reason it is not exact")
    ie.set_defaults(fn=cmd_instruments_export)
    d = sub.add_parser("demo-book", help="generate the synthetic demo book")
    d.add_argument("--snapshots", default=str(settings.snapshot_cache))
    d.add_argument("--date", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--profile", choices=("demo", "test"), default="demo",
                   help="demo: the S1 demo book; test: the S3 test book (three books, coverage cases)")
    d.set_defaults(fn=cmd_demo_book)
    sv = sub.add_parser("serve", help="the desk API and the Desk Monitor prototype on the store")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8100)
    sv.add_argument("--no-worker", action="store_true",
                    help="no exact scenario worker (jobs stay queued); it reprices from DESK_SNAPSHOT_CACHE")
    sv.add_argument("--autopilot", action=argparse.BooleanOptionalAction, default=None,
                    help="the autopilot thread (sync, inbox scan, EOD; desk cycle): default on, following the "
                    "settings' autopilot flag; --autopilot ignores the flag, --no-autopilot starts no thread")
    sv.set_defaults(fn=cmd_serve)
    cy = sub.add_parser("cycle", help="one autopilot cycle in the foreground: hub sync, inbox scan, EOD of the "
                        "dates whose inputs changed")
    cy.add_argument("--date", action="append", type=_iso, help="also sync and keep current this business date "
                    "(repeatable)")
    cy.add_argument("--no-hub", action="store_true", help="no hub sync (the drop folder is still imported)")
    cy.set_defaults(fn=cmd_cycle)
    bf = sub.add_parser("backfill", help="the default runs of past business dates with positions (history of "
                        "risk and PnL): older reports in, hub closes pulled, oldest date first")
    bf.add_argument("--from", dest="date_from", type=_iso, help="first business date (default: the setting "
                    "backfill_from, 2026-01-05: the hub's first eod_final close)")
    bf.add_argument("--to", dest="date_to", type=_iso, help="last business date (default: today)")
    bf.add_argument("--dry-run", action="store_true", help="only say which dates would run and why")
    bf.add_argument("--no-hub", action="store_true", help="the cached closes only, nothing downloaded")
    bf.add_argument("--no-inbox", action="store_true", help="do not read the older reports of the inbox first")
    bf.add_argument("--rescan", action="store_true", help="read the inbox from the start date again even if done "
                    "before with the same adapter")
    bf.set_defaults(fn=cmd_backfill)
    oa = sub.add_parser("openapi", help="print or write the desk API's OpenAPI document")
    oa.add_argument("--out", help="write it to this file")
    oa.set_defaults(fn=cmd_openapi)
    dg = sub.add_parser("diagnostics", help="how the desk works here as JSON, without private data (positions, "
                        "books, ISINs, amounts, paths, tokens): for the agent")
    dg.add_argument("--out", help="write it to this file (stdout without --out)")
    dg.add_argument("--no-hub", action="store_true", help="do not ask the hub (no live check, no snapshot list)")
    dg.set_defaults(fn=cmd_diagnostics)
    c = sub.add_parser("config", help="print the effective settings (token masked)")
    c.set_defaults(fn=cmd_config)
    a = ap.parse_args(argv)
    try:
        return int(a.fn(a))
    except psycopg.errors.UndefinedTable as e:
        print(f"desk: the desk store is not migrated (desk db-migrate): {e}", file=sys.stderr)
        return 1
    except (HubError, SnapshotError, SettingsError, store.InputError) as e:
        print(f"desk: {e}", file=sys.stderr)
        return 1


def _iso(s: str) -> str:
    return date.fromisoformat(s).isoformat()


def _list(s: str) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


if __name__ == "__main__":
    sys.exit(main())
