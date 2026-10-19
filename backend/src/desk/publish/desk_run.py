"""Write an EOD run into the desk store (slice S5; 04 §3, ADR-006).

The rows are ``tdm_run.run_content``, one format for both stores: ``run_position.fact`` is
``tdm_run.position_row`` (non-finite floats as null, which jsonb cannot hold), the run log has the
same lines. The run is inserted as ``loading`` and its rows, the hash-verified core results, the
model spreads, coverage and totals are written in one transaction that ends with ``complete``; on
any error the rows roll back and the run is marked ``failed`` with the error. A total PnL is null, not 0,
when no position of the book has a flash PnL. The run's ``position_set_ids`` (and lineage ``positions``)
are the source of truth to reproduce its inputs; ``positions_known_at`` is only the horizon they were read at.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from functools import partial
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from rub_fixed_income import __version__ as CORE_VERSION
from rub_fixed_income import portfolio_result_to_json

from .. import __version__ as BACKEND_VERSION
from ..marketdata.adapter import MarketView
from ..orchestrator import EodContext, Outcome
from ..store import PositionInputs
from ..valuation.fallback import CarriedSpread, used_model_spreads
from .tdm_run import REFERENCE_KINDS, RunContent, git_sha, run_content

PNL_KEYS = ("carry", "rolldown", "rates", "spread", "other", "total")


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
            carried_from: dict[str, CarriedSpread] | None = None) -> tuple[int, str, list[str]]:
    """positions: (position_id, book_id, outcome) of ``inputs`` (``store.positions_for``); ``carried_from``:
    the carried spreads the valuation got. Returns (run_id, status, warnings)."""
    outcomes = [o for _, _, o in positions]
    results = {o.core_result.result_sha256: o.core_result for o in outcomes if o.core_result}
    bodies = {sha: portfolio_result_to_json(r).decode() for sha, r in results.items()}  # raises unless verified
    spreads = used_model_spreads(outcomes)
    carried = sum(1 for s in spreads if (c := (carried_from or {}).get(s.secid)) and c.z_spread_bp == s.z_spread_bp)
    bd = ctx.business_date
    lineage = {
        **ctx.lineage(), "backend_version": BACKEND_VERSION, "backend_git_sha": git_sha(),
        "core_version": CORE_VERSION, "prev_market_snapshot_id": prev.snapshot.id if prev else None,
        "positions": {"set_ids": inputs.set_ids, "valid_dates": {b: d.isoformat() for b, d in inputs.valid_dates.items()},
                      "carried_from": {b: d.isoformat() for b, d in inputs.valid_dates.items() if d < bd},
                      "known_at": inputs.known_at.isoformat(), "private_inputs_hash": inputs.private_inputs_hash,
                      "sources": inputs.sources},
        "core_results": [{"result_sha256": sha, "schema": r.schema_version, "label": r.run_id.rsplit("-", 1)[-1]}
                         for sha, r in results.items()],
        "model_spreads": {"carried": carried, "new": len(spreads) - carried},
    }
    content = run_content(ctx, positions, prev, lineage, inputs)
    run_id = con.execute(
        "INSERT INTO run (run_type, business_date, prev_date, cut_type, supersedes_run_id, market_snapshot_id,"
        " static_snapshot_id, prev_market_snapshot_id, private_inputs_hash, position_set_ids, positions_known_at,"
        " core_version, backend_version, backend_git_sha, lineage) VALUES (%(type)s, %(bd)s, %(prev)s, %(cut)s,"
        " (SELECT max(run_id) FROM run WHERE business_date = %(bd)s AND run_type = %(type)s"
        "  AND (cut_type = %(cut)s OR %(cut)s = 'eod_final')"  # a final run supersedes the preliminary one (04 §3)
        "  AND status = 'complete'), %(m)s, %(s)s, %(pm)s, %(h)s, %(ids)s, %(known)s, %(core)s, %(be)s, %(sha)s,"
        " %(lineage)s) RETURNING run_id",
        {"type": run_type, "bd": bd, "prev": prev.business_date if prev else None,
         "cut": ctx.market.snapshot.cut_type, "m": ctx.market.snapshot.id, "s": ctx.static.snapshot.id,
         "pm": lineage["prev_market_snapshot_id"], "h": inputs.private_inputs_hash, "ids": inputs.set_ids,
         "known": inputs.known_at, "core": CORE_VERSION, "be": BACKEND_VERSION, "sha": lineage["backend_git_sha"],
         "lineage": _jsonb(lineage)}).fetchone()[0]
    try:
        with con.transaction(), con.cursor() as cur:
            _write(cur, run_id, positions, inputs, content, bodies, results, spreads)
    except Exception as e:
        con.execute("UPDATE run SET status = 'failed', error = %s WHERE run_id = %s",
                    (f"{type(e).__name__}: {e}"[:4000], run_id))
        raise
    return run_id, "complete", [m for level, m in content.log if level == "WARN"]


def _write(cur: psycopg.Cursor, run_id: int, positions: list[tuple[str, str, Outcome]], inputs: PositionInputs,
           content: RunContent, bodies: dict[str, str], results: dict[str, Any], spreads: list[CarriedSpread]) -> None:
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
        pnl = {**o.pnl.as_tdm(), "extra": o.pnl.extra()} if o.pnl else None
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
                    t["pnl"][k] = (t["pnl"][k] or 0.0) + getattr(o.pnl, k)
    cur.executemany("INSERT INTO run_position (run_id, position_id, ord, book_id, secid, quantity, quality, reason, pv_rub,"
                    " dv01_rub, cs01_rub, fact, pnl, no_pnl) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows)
    cur.executemany("INSERT INTO run_reference (run_id, kind, data) VALUES (%s, %s, %s)",
                    [(run_id, k, _jsonb(content.reference[k])) for k in REFERENCE_KINDS])
    cur.executemany("INSERT INTO run_log (run_id, seq, level, message) VALUES (%s, %s, %s, %s)",
                    [(run_id, i, level, m) for i, (level, m) in enumerate(content.log, 1)])
    cur.executemany("INSERT INTO run_core_result (run_id, result_sha256, label, body) VALUES (%s, %s, %s, %s)",
                    [(run_id, sha, r.run_id.rsplit("-", 1)[-1], bodies[sha]) for sha, r in results.items()])
    cur.executemany("INSERT INTO model_spread (run_id, secid, business_date, z_spread_bp, source) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    [(run_id, s.secid, s.business_date, s.z_spread_bp, s.source) for s in spreads])
    cur.execute("UPDATE run SET status = 'complete', completed_at = now(), coverage = %s, totals = %s "
                "WHERE run_id = %s", (_jsonb(coverage), _jsonb(totals), run_id))
