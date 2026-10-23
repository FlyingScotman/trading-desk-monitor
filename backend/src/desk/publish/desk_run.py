"""Write an EOD run into the desk store (slice S5; 04 §3, ADR-006).

The rows are ``tdm_run.run_content``, one format for both stores: ``run_position.fact`` is
``tdm_run.position_row`` (non-finite floats as null, which jsonb cannot hold), the run log has the
same lines. The run is inserted as ``loading`` and its rows, the hash-verified core results, the
model spreads, coverage and totals are written in one transaction that ends with ``complete``; on
any error the rows roll back and the run is marked ``failed`` with the error. A total PnL is null, not 0,
when no position of the book has a flash PnL. The run's ``position_set_ids`` (and lineage ``positions``)
are the source of truth to reproduce its inputs; ``positions_known_at`` is only the horizon they were read at.

Funding (B1, ``funding_run``): computed on the run's content before the run row is written; its block is the
``run_reference`` kind ``funding``, its summary lineage ``funding``, its lines in the run log, a position's
``fact.extra.funding`` and the ``funding`` component of its PnL (absent, not 0, when funding is not configured or the
position has a problem). The book totals' ``pnl.funding`` sums it and ``pnl.total`` is net of it.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import date
from functools import partial
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from rub_fixed_income import __version__ as CORE_VERSION
from rub_fixed_income import portfolio_result_to_json

from .. import __version__ as BACKEND_VERSION
from .. import logic_version
from ..marketdata.adapter import MarketView
from ..orchestrator import EodContext, Outcome
from ..store import PositionInputs
from ..valuation.fallback import CarriedSpread, used_model_spreads
from . import derivatives_parallel, funding_run
from .tdm_run import RunContent, git_sha, run_content

PNL_KEYS = ("carry", "rolldown", "rates", "spread", "other", "fx_gross", "funding", "total")
"""Run totals per book: the components sum to ``total`` (a foreign-face bond's FX revaluation is in ``fx_gross``, the
position's funding in ``funding``: ``total`` is net of funding)."""


def _finite(x: Any) -> Any:
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_finite(v) for v in x]
    return x


def _jsonb(x: Any) -> Jsonb:
    return Jsonb(_finite(x), dumps=partial(json.dumps, ensure_ascii=False, allow_nan=False, default=str))


def publish(con: psycopg.Connection, ctx: EodContext, positions: list[tuple[str, str, Outcome]], *,
            prev: MarketView | None, run_type: str = "EOD", inputs: PositionInputs,
            carried_from: dict[str, CarriedSpread] | None = None,
            pnl_basis: dict[str, Any] | None = None, core_results: list[Any] | None = None,
            business_date: date | None = None, cut_type: str | None = None) -> tuple[int, str, list[str]]:
    """positions: (position_id, book_id, outcome) of ``inputs`` (``store.positions_for``); ``carried_from``:
    the carried spreads the valuation got; ``pnl_basis``: what the flash PnL is measured against
    (``desk.cli.pnl_basis``, lineage ``pnl_basis``); ``core_results``: core results of the run besides those its
    outcomes carry (``BookPlan.core_result``, the v3 result of the derivatives: stored next to the bonds' v2 results
    even when core valued none of its positions). Returns (run_id, status, warnings). An INTRADAY run supersedes
    nothing: the older intraday runs of its date are deleted once it is complete (``store.prune_intraday``).
    ``business_date`` / ``cut_type``: the morning run (#21), the book of ``business_date`` on the earlier close of
    ``ctx`` as cut ``prev_close``; lineage ``market_close`` names that close."""
    outcomes = [o for _, _, o in positions]
    results = {r.result_sha256: r for r in [*(o.core_result for o in outcomes), *(core_results or ())] if r}
    bodies = {sha: portfolio_result_to_json(r).decode() for sha, r in results.items()}  # raises unless verified
    spreads = used_model_spreads(outcomes)
    carried = sum(1 for s in spreads if (c := (carried_from or {}).get(s.secid)) and c.z_spread_bp == s.z_spread_bp)
    bd = business_date or ctx.business_date
    lineage = {
        **ctx.lineage(), "backend_version": BACKEND_VERSION, "backend_git_sha": git_sha(),
        "core_version": CORE_VERSION, "logic_version": logic_version(),
        "prev_market_snapshot_id": prev.snapshot.id if prev else None,
        "positions": {"set_ids": inputs.set_ids, "valid_dates": {b: d.isoformat() for b, d in inputs.valid_dates.items()},
                      "carried_from": {b: d.isoformat() for b, d in inputs.valid_dates.items() if d < bd},
                      "known_at": inputs.known_at.isoformat(), "private_inputs_hash": inputs.private_inputs_hash,
                      "sources": inputs.sources,
                      **({"trade_terms": inputs.trade_terms_units} if inputs.trade_terms_units else {})},
        "core_results": [{"result_sha256": sha, "schema": r.schema_version, "label": r.run_id.rsplit("-", 1)[-1]}
                         for sha, r in results.items()],
        "model_spreads": {"carried": carried, "new": len(spreads) - carried},
        "cut_ts": ctx.market.snapshot.doc.get("cut_ts"),
        "market_business_date": ctx.market.snapshot.business_date,  # the header shows it when older (#22)
        "pnl_basis": pnl_basis,
    }
    if bd != ctx.business_date:  # the morning run (#21): the close the book of ``bd`` is valued on
        lineage["market_close"] = {"date": ctx.business_date.isoformat(), "cut_type": ctx.market.snapshot.cut_type}
    content = run_content(ctx, positions, prev, lineage, inputs)
    funding = funding_run.compute(con, bd, prev, ctx.market, content.rows, content.reference["books"], inputs,
                                  {pid: o.terms for pid, _, o in positions if o.terms is not None})
    funding.apply(content.rows, content.reference, content.log)
    try:  # a diagnostic: its failure is said in the run log, the run goes on without the block
        parallel = derivatives_parallel.compare(positions, inputs.extra, inputs.trade_terms)
        line = derivatives_parallel.log_line(parallel)
    except Exception as e:  # noqa: BLE001
        why = f"деривативы, ядро и отчёт параллельно: сравнение не построено ({type(e).__name__}: {e})"
        content.log.append(("WARN", why))
    else:
        content.reference[derivatives_parallel.KIND] = parallel
        if line:
            content.log.append(line)
    lineage["funding"] = funding.lineage()
    run_id = con.execute(
        "INSERT INTO run (run_type, business_date, prev_date, cut_type, supersedes_run_id, market_snapshot_id,"
        " static_snapshot_id, prev_market_snapshot_id, private_inputs_hash, position_set_ids, positions_known_at,"
        " core_version, backend_version, backend_git_sha, lineage, cut_ts, prev_cut_type)"
        " VALUES (%(type)s, %(bd)s, %(prev)s, %(cut)s,"
        " (SELECT max(run_id) FROM run WHERE business_date = %(bd)s AND run_type = %(type)s AND %(type)s <> 'INTRADAY'"
        "  AND (cut_type = %(cut)s OR %(cut)s = 'eod_final')"  # a final run supersedes the preliminary one (04 §3)
        "  AND status = 'complete'), %(m)s, %(s)s, %(pm)s, %(h)s, %(ids)s, %(known)s, %(core)s, %(be)s, %(sha)s,"
        " %(lineage)s, %(cut_ts)s, %(pcut)s) RETURNING run_id",
        {"type": run_type, "bd": bd, "prev": prev.business_date if prev else None,
         "cut_ts": lineage["cut_ts"], "pcut": prev.snapshot.cut_type if prev else None,
         "cut": cut_type or ctx.market.snapshot.cut_type, "m": ctx.market.snapshot.id, "s": ctx.static.snapshot.id,
         "pm": lineage["prev_market_snapshot_id"], "h": inputs.private_inputs_hash, "ids": inputs.set_ids,
         "known": inputs.known_at, "core": CORE_VERSION, "be": BACKEND_VERSION, "sha": lineage["backend_git_sha"],
         "lineage": _jsonb(lineage)}).fetchone()[0]
    try:
        with con.transaction(), con.cursor() as cur:
            _write(cur, run_id, positions, inputs, content, bodies, results, spreads, funding.pnl_funding)
    except Exception as e:
        con.execute("UPDATE run SET status = 'failed', error = %s WHERE run_id = %s",
                    (f"{type(e).__name__}: {e}"[:4000], run_id))
        raise
    return run_id, "complete", [m for level, m in content.log if level == "WARN"]


def _write(cur: psycopg.Cursor, run_id: int, positions: list[tuple[str, str, Outcome]], inputs: PositionInputs,
           content: RunContent, bodies: dict[str, str], results: dict[str, Any], spreads: list[CarriedSpread],
           pnl_funding: dict[str, float]) -> None:
    qty = {pid: q for pid, _, _, q in inputs.rows}
    facts = {r["position_id"]: _finite(r) for r in content.rows}
    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"pv_rub": 0.0, "dv01_rub": 0.0, "cs01_rub": 0.0,
                                                             "pnl": dict.fromkeys(PNL_KEYS)})
    rows = []
    for i, (pid, book, o) in enumerate(positions):
        v, f = o.valuation, facts.get(pid)
        why = f"{o.reason}{': ' + o.detail if o.detail else ''}" if o.reason else None
        pv, dv01, cs01 = (f["mv_rub"], f["dv01"], f["cs01"]) if f else (None, None, None)
        fund = pnl_funding.get(pid) if o.pnl else None
        pnl = {**o.pnl.as_tdm(), **({"funding": fund} if fund is not None else {}), "extra": o.pnl.extra()} \
            if o.pnl else None
        q = v.quantity if v and o.terms else qty[pid]  # a bond given by face: the number of bonds valued
        rows.append((run_id, pid, i, book, o.secid, q, v.quality if v else None, why, pv, dv01, cs01,
                     _jsonb(f) if f else None, _jsonb(pnl) if pnl else None,
                     _jsonb({"reason": o.no_pnl.reason, "detail": o.no_pnl.detail}) if o.no_pnl else None))
        for b in (book, "_total"):
            coverage[b][v.quality if v else "not_valued"] += 1
            coverage[b]["not_valued"] += 0
            t = totals[b]
            for k, x in (("pv_rub", pv), ("dv01_rub", dv01), ("cs01_rub", cs01)):
                t[k] += x or 0.0
            if o.pnl:
                for k in PNL_KEYS:
                    if k == "funding":
                        if fund is not None:
                            t["pnl"][k] = (t["pnl"][k] or 0.0) + fund
                        continue
                    net = (fund or 0.0) if k == "total" else 0.0  # the total net of the position's funding
                    t["pnl"][k] = (t["pnl"][k] or 0.0) + getattr(o.pnl, k) + net
    cur.executemany("INSERT INTO run_position (run_id, position_id, ord, book_id, secid, quantity, quality, reason, pv_rub,"
                    " dv01_rub, cs01_rub, fact, pnl, no_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows)
    cur.executemany("INSERT INTO run_reference (run_id, kind, data) VALUES (%s, %s, %s)",
                    [(run_id, k, _jsonb(v)) for k, v in content.reference.items()])  # REFERENCE_KINDS + funding
    cur.executemany("INSERT INTO run_log (run_id, seq, level, message) VALUES (%s, %s, %s, %s)",
                    [(run_id, i, level, m) for i, (level, m) in enumerate(content.log, 1)])
    cur.executemany("INSERT INTO run_core_result (run_id, result_sha256, label, body) VALUES (%s, %s, %s, %s)",
                    [(run_id, sha, r.run_id.rsplit("-", 1)[-1], bodies[sha]) for sha, r in results.items()])
    cur.executemany("INSERT INTO model_spread (run_id, secid, business_date, z_spread_bp, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(run_id, s.secid, s.business_date, s.z_spread_bp, s.source) for s in spreads])
    cur.execute("UPDATE run SET status = 'complete', completed_at = now(), coverage = %s, totals = %s "
                "WHERE run_id = %s", (_jsonb(coverage), _jsonb(totals), run_id))
