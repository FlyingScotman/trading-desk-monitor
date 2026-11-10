"""The calculation of a day (``eod_run``) and what it shares: the code path of every EOD, intraday and morning run
(TDM#644; it lived in ``desk.cli``). ``desk eod``, the autopilot's eod, intraday and morning steps, the backfill, the
recomputation after an upgrade, and the acquisition of terms all call it; ``desk.cli`` only parses the command line.

* ``eod_run``: values the books of the store on a cached market of a date and publishes the run;
* ``ContextCache``: prepared market dates kept between the runs of consecutive dates (the backfill);
* ``pnl_basis``: what the flash PnL of a run is measured against;
* ``stored_fixings``: the stored fixings history a run of a date fills its cut's window from;
* ``age_label``: «3.2 h old» for a timestamp.
"""

from __future__ import annotations

import dataclasses
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from . import bookfile, store
from . import trades as trades_mod
from .curves import fx as fxcurves
from .marketdata import fixing_history, history, reference, static_facts
from .marketdata.adapter import MarketView
from .marketdata.bundle import SnapshotDir, previous_close
from .orchestrator import EodContext, attach_pnl, prepare, value
from .pnl import direct as direct_pnl
from .pnl import new_trades as new_trades_pnl
from .publish import desk_run
from .publish import summary as run_summary
from .report import validation
from .settings import Settings
from .valuation import offreport
from .valuation import positions as pv1


class ContextCache:
    """The ``size`` prepared market dates used last (``orchestrator.prepare``), keyed by the snapshot folder, date, cut and
    snapshot id: ``desk backfill`` values consecutive dates, and each date's market is the next date's previous one.
    A prepared context is read only by the valuation (its curve cache only grows), so sharing it changes nothing."""

    def __init__(self, size: int = 3) -> None:  # a date's own market, its previous one, the next date's
        self.size, self._items = size, {}

    def prepare(self, sd: SnapshotDir, business_date: str, cut_type: str = "eod_final",
                market_snapshot_id: str | None = None, *, fx_options: fxcurves.FxOptions | None = None,
                fixings: fixing_history.FixingStore | None = None) -> EodContext:
        key = (str(sd.root), business_date, cut_type, market_snapshot_id or sd.market_id(business_date, cut_type),
               fx_options, fixings.stamp if fixings is not None else None)  # another stored history: prepared again
        ctx = self._items.pop(key, None)
        if ctx is None:
            ctx = prepare(sd, business_date, cut_type, market_snapshot_id=market_snapshot_id, fx_options=fx_options,
                          fixings=fixings)
        self._items[key] = ctx
        while len(self._items) > self.size:
            self._items.pop(next(iter(self._items)))
        return ctx


def age_label(ts: str | None) -> str:
    if not ts:
        return "age unknown"
    h = (datetime.now(UTC) - datetime.fromisoformat(ts)).total_seconds() / 3600
    return f"{h:.1f} h old" if h < 48 else f"{h / 24:.1f} days old"


def pnl_basis(ctx: EodContext, prev: tuple[str, str] | None) -> dict[str, Any]:
    """What the flash PnL of ``ctx`` is measured against (R11; lineage ``pnl_basis``): ``prev_date``,
    ``prev_cut_type`` (``bundle.previous_close``), ``expected_date`` (the previous business day of the
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


def stored_fixings(url: str, d: date, out: Callable[[str], None] = print) -> fixing_history.FixingStore | None:
    """The stored fixings history for a run of ``d`` (``marketdata.fixing_history``, TDM#590): back to
    ``fixing_history.DEPTH_YEARS`` before it; None when the store cannot be read (said, the run goes on without)."""
    try:
        with store.connect(url) as con:
            return fixing_history.for_date(con, d)
    except psycopg.Error as e:
        out(f"WARN stored fixings history not read: {type(e).__name__}: {e}")
        return None


def eod_run(settings: Settings, d: str, *, snapshots: str | Path | None = None, store_url: str | None = None,
            book: str | None = None, books: list[str] | None = None, run_type: str = "EOD",
            report_dir: str | Path | None = None, no_gamma: bool = False, no_pnl: bool = False,
            out: Callable[[str], None] = print,
            cut_type: str = "eod_final", market_snapshot_id: str | None = None,
            contexts: ContextCache | None = None, read_model: bool = True,
            on_close: tuple[str, str] | None = None) -> int | None:
    """The EOD run of date ``d`` (``desk eod`` without its hub pull; the autopilot calls it too): values the
    books of the store on the cached snapshot of ``d`` and publishes the run. Returns the desk run id, None when
    the store has no positions for the date. Raises on a missing snapshot or a failed publication.

    ``cut_type`` / ``market_snapshot_id``: another market cut of ``d`` than its ``eod_final`` (R11: the autopilot's
    intraday step values today's book on an ``intraday`` cut, ``run_type`` INTRADAY, or on ``eod_preliminary``).
    The flash PnL is measured against the newest earlier ``eod_final`` of the cache, else an ``eod_preliminary``
    (``bundle.previous_close`` of the cache: the backfill's plan and the autopilot's steps pick it by the same rule,
    #203); when that is not the previous business day's final, the run says so (``pnl_basis``).

    ``contexts`` (``desk backfill``, R23): prepared dates kept between calls, so that a run of consecutive dates
    prepares each date's market once (as the run's own date, then as the next date's previous one).
    ``read_model``: also put the run's static snapshot's facts (the market calendar, issuer groups, sectors) and
    the CBR meetings into the store's read model (``desk.marketdata.static_facts``); the backfill leaves them to the
    latest date. After the publication the run's summary for the concept pages is written (``publish.summary``).

    ``on_close`` (the autopilot's morning run, #21): (date, cut) of an earlier close: the book of ``d`` valued on
    that close, published as a ``run_type`` run of ``d`` with cut ``prev_close`` (lineage ``market_close``) and
    without a flash PnL (the day's first intraday cut brings it)."""
    snapshots = snapshots or settings.snapshot_cache
    sd = SnapshotDir(Path(snapshots))
    t0 = time.perf_counter()
    prep = contexts.prepare if contexts is not None else prepare
    fx_options = fxcurves.options_from(settings)  # the provisional FX choices (curves.fx.FxOptions)
    run_date = date.fromisoformat(d)
    fixings = stored_fixings(store_url or store.resolve_url(settings), run_date, out)
    ctx = prep(sd, *(on_close or (d, cut_type)), market_snapshot_id=market_snapshot_id, fx_options=fx_options,
               fixings=fixings)
    if fh := (ctx.rates or {}).get("fixings_history"):
        if fh.get("codes"):
            out("fixings from the stored history before the cut's window: " + ", ".join(
                f"{c} {i['from']}..{i['to']} ({i['rows']})" for c, i in sorted(fh["codes"].items())))
        if hole := ((((ctx.rates or {}).get("fixings") or {}).get("RUONIA") or {}).get("calendar_from_fixings")
                    or {}).get("hole"):
            out(f"WARN stored RUONIA history has a hole {hole['from']}..{hole['to']}: only the fixings after it are "
                "used, a period over it is not valued")
        for c, e in sorted((fh.get("errors") or {}).items()):
            out(f"WARN stored fixings of {c} not used: {e}")
        if fh.get("dropped"):
            out(f"WARN stored fixings history not used: {fh['dropped']}")
    # issuer names, groups, sectors and ratings: the newest static of the cache, not the market's own (R25)
    ctx = dataclasses.replace(ctx, reference=reference.facts(sd, ctx.static),
                              proxy_curves=bool(getattr(settings, "proxy_curves_for_risk", False)),
                              quote_curve_risk=bool(getattr(settings, "quote_curve_risk", False)))
    pc = None if on_close else previous_close(sd, d)
    prev_cut = pc[:2] if pc else None
    prev_date = prev_cut[0] if prev_cut else None
    prev = MarketView(sd.market(*prev_cut)) if prev_cut else None
    basis = pnl_basis(ctx, prev_cut)
    if on_close:
        basis["label"] = (f"нет PnL: позиции {run_date:%d.%m} на закрытии {ctx.business_date:%d.%m}, "
                          "PnL дня — с первого внутридневного среза")
    prov = sd.provenance(ctx.market.snapshot.id)
    origin = (f"hub {prov['hub']}, published {prov['published_at']} ({age_label(prov['published_at'])})" if prov
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
                if ctx.reference_facts.snapshot_id != ctx.static.snapshot.id:  # the newest static's facts (R25)
                    static_facts.load_static(con, sd.get(ctx.reference_facts.snapshot_id))
                static_facts.load_cbr(con, ctx.market.snapshot)
            except psycopg.Error as e:
                out(f"WARN static facts not updated: {type(e).__name__}: {e}")
        if book:
            bookfile.load_book(con, Path(book), d, out=out)
        inputs = store.positions_for(con, run_date, books=books)
        start_inputs = None
        # the day's bond trades in the book of any run of the date (TDM#557); the start of the day for their PnL
        inputs, intraday_trades = trades_mod.intraday_inputs(con, run_date, settings, inputs)
        if intraday_trades and not intraday_trades["set_ids"]:
            intraday_trades = None   # no trades of the date: nothing to say in the lineage
        if intraday_trades:
            start_inputs = intraday_trades.get("start_inputs")
        if intraday_trades and (intraday_trades["applied"] or intraday_trades["skipped"]):
            out(f"trades of {run_date}: {intraday_trades['applied']} in the book "
                f"({intraday_trades['new_positions']} new positions), left out "
                + (", ".join(f"{k} {v}" for k, v in intraday_trades["skipped"].items()) or "none")
                + (f"; {intraday_trades['history']} trades of earlier days (no report since)"
                   if intraday_trades.get("history") else ""))
            for b, c in (intraday_trades.get("history_cut") or {}).items():
                out(f"WARN trades: {c['dropped']} trade(s) of {b} older than {trades_mod.MAX_HISTORY_DAYS} days since its "
                    f"last report ({c['report']}) are not counted in the start of the day")
            if intraday_trades.get("mixed_units"):
                out(f"WARN trades: {len(intraday_trades['mixed_units'])} bond(s) with lots in different units "
                    "(quantity / face amount): their trades are not in the PnL: "
                    + ", ".join(intraday_trades["mixed_units"][:10]))
            if intraday_trades.get("gaps"):
                out(f"WARN trades: {len(intraday_trades['gaps'])} position(s) where the report of {run_date} differs "
                    "from the start of the day plus the trades (x.trades_report_gap, the report's quantity kept): "
                    + ", ".join(f"{p} {g:+g}" for p, g in list(intraday_trades["gaps"].items())[:10]))
        if not inputs.rows:
            out(f"no positions in the store for {d}" + (f" (books {','.join(books)})" if books else ""))
            out(f"done in {time.perf_counter() - t0:.1f} s")
            return None
        for b, vd in sorted(inputs.valid_dates.items()):
            src = inputs.sources.get(b) or {}
            age = (run_date - vd).days
            out(f"  {b}: {src.get('source')} {src.get('file_name')}"
                + (f", report {src['report_date']}" if src.get("report_date") else "")
                + f", valid {vd}" + (f", carried {age} days" if age else ""))
        carried = store.carried_spreads(con, before=run_date)
        bp = pv1.plan(ctx, inputs, second_order=not no_gamma)
        orp = offreport.summary(bp.off_report, bp.off_report_matured, "include" if fx_options.off_report else "exclude",
                                sum(1 for pid in bp.added if bp.direct[pid].valuation is not None))
        if fx_options.off_report:
            if orp["trades"]:
                out(f"trade_terms: derivatives outside the positions report included: {orp['included']}, valued "
                    f"{orp['valued']}, unreadable terms {orp['invalid']}")
        elif orp["excluded"]:
            out(f"WARN trade_terms: {orp['excluded']} derivative(s) outside the positions report excluded by the setting "
                "(off_report_derivatives): " + ", ".join(t.key for t in bp.off_report if t.state == "excluded")[:300])
        if orp["suspect"]:
            out(f"WARN trade_terms: possibly already in the positions report, not included: {orp['suspect']}: "
                + ", ".join(t.key for t in bp.off_report if t.state == "suspect")[:300])
        # the swaps and REPOs the switch values are no longer «not valued»
        taken = {t.key for t in bp.off_report if t.state == "included"}
        rates_left = [k for k in bp.unlinked_rates if k not in taken]
        if bp.ambiguous_terms:
            out(f"WARN trade_terms: {len(bp.ambiguous_terms)} position row(s) share a trade's legs with another row "
                "(one deal_id on several rows), valued in none: " + "; ".join(bp.ambiguous_terms[:10]))
        outcomes = value(ctx, bp.requests, second_order=not no_gamma, position_ids=bp.position_ids,
                         carried_spreads=carried)
        outside_total = None   # the day's trades whose PnL is not in the run's total (the lineage's ``outside_total``)
        if prev_cut and not no_pnl:
            prev_ctx = prep(sd, *prev_cut, fx_options=fx_options, fixings=fixings)  # the same history (TDM#590)
            attach_pnl(prev_ctx, value(prev_ctx, bp.requests, position_ids=bp.position_ids, carried_spreads=carried),
                       ctx, outcomes)
            # fx rows, the report-valued rows (otc, unmapped bonds): by the rate move and by the reports of both
            # dates (pnl.direct); the reason in run_position.no_pnl when there is none, never a bare null
            prev_inputs = store.positions_for(con, prev_ctx.business_date, books=books)
            direct_pnl.attach(prev_ctx, ctx, bp.direct, inputs, pv1.plan(prev_ctx, prev_inputs, second_order=False).direct,
                              prev_inputs.valid_dates, prev_inputs)
            if intraday_trades and intraday_trades["applied"] and start_inputs is not None:   # start of day + trades
                split = new_trades_pnl.attach(prev_ctx, ctx, start_inputs, bp, outcomes, intraday_trades, carried)
                texts = {"mixed_valuation": "lots valued in different ways (core and the report): the PnL of the bond "
                                            "(the start of the day and the trades) is whole on one lot",
                         "lost": "valued at the report's figure, no lot with terms: the trades are inside the reports' "
                                 "difference, the day's PnL of the bond is not shown"}
                for kind, text in texts.items():
                    if split[kind]:
                        out(f"WARN trades: {len(split[kind])} bond(s), {text}: " + ", ".join(split[kind][:10]))
                        intraday_trades[kind] = split[kind]
                for pid_, flag_ in split["rows"].items():   # the rows say so (the facts are built from the inputs)
                    if pid_ in inputs.extra:
                        x_ = inputs.extra[pid_]
                        inputs.extra[pid_] = {**x_, "x": {**(x_.get("x") or {}), flag_: True}}
                out(f"PnL of the day's trades: {split['split']} positions split, {split['opened']} opened today, "
                    f"{split['assumed']} on an assumption (start of the day not valued), {split['no_pnl']} without PnL")
                if split["flagged"]:
                    out(f"WARN trades: {split['flagged']} trade(s) without a PnL (no price, other currency, no "
                        "valuation or rate): the PnL of the trades is incomplete")
                    intraday_trades["unpriced"] = split["flagged"]
                outside_total = new_trades_pnl.outside_total(intraday_trades["applied"], split)
            flash = [o.pnl for o in outcomes if o.pnl]
            out(f"flash PnL {prev_date} -> {d}: {len(flash)}/{sum(o.valuation is not None for o in outcomes)} "
                f"priced positions, total {sum(p.total for p in flash):,.0f} RUB, "
                f"max identity error {max((abs(p.identity_error) for p in flash), default=0):.2e} RUB")
            dp = [o.pnl for o in bp.direct.values() if o.pnl]
            out(f"PnL without core: {len(dp)}/{sum(o.valuation is not None for o in bp.direct.values())} positions "
                f"(by the rate move or the reports), total {sum(p.total for p in dp):,.0f} RUB")
        if intraday_trades and intraday_trades["applied"]:
            # no previous close (or no PnL asked): none of the trades is in a total, said in the lineage
            intraday_trades["outside_total"] = outside_total or new_trades_pnl.outside_total(
                intraday_trades["applied"], no_prev_close=not prev_cut, no_pnl_run=bool(no_pnl))
        if on_close:
            out(f"no flash PnL: the book of {d} on the close of {ctx.business_date} (morning run)")
        elif not prev_date:
            out(f"WARN no flash PnL: no eod_final or eod_preliminary market snapshot before {d} in {snapshots}")
        if intraday_trades:   # the transient figures for the PnL of the trades are not lineage
            intraday_trades = {k: v for k, v in intraday_trades.items() if k not in ("trades", "rows", "start_inputs")}
        positions = pv1.merge(bp, outcomes)
        ok = sum(o.valuation is not None for _, _, o in positions)
        by_q = Counter(o.valuation.quality if o.valuation else "not_valued" for _, _, o in positions)
        out(f"book: {ok}/{len(positions)} positions valued (" + ", ".join(f"{q} {n}" for q, n in by_q.most_common())
            + f"), {len(carried)} carried spreads")
        run_id, status, warnings = desk_run.publish(con, ctx, positions, prev=prev, run_type=run_type,
                                                    inputs=inputs, carried_from=carried, pnl_basis=basis,
                                                    core_results=[bp.core_result] if bp.core_result else None,
                                                    unlinked_rates=rates_left, off_report=orp,
                                                    intraday_trades=intraday_trades,
                                                    **({"business_date": run_date, "cut_type": "prev_close"}
                                                       if on_close else {}))
        out(f"desk run {run_id}: {status}, {len(warnings)} warnings")
        for (m,) in con.execute("SELECT message FROM run_log WHERE run_id = %s AND message LIKE %s",
                                (run_id, "справочник эмитентов%")):
            out(m)  # where the issuer facts and ratings come from and how many bonds have one (R25)
        try:
            run_summary.ensure(con, [run_id])
        except psycopg.Error as e:  # the pages build it on first read
            out(f"WARN run summary not written: {type(e).__name__}: {e}")
    out(f"done in {time.perf_counter() - t0:.1f} s")
    return run_id
