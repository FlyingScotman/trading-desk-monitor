"""The run content of ``publish.tdm_run.run_content`` checked as a whole, without a store.

Until #294 the slice tests published through the legacy ``tdm`` SQLite writer and read its ``status``: the writer
refused a run whose rows did not hang together. ``consistency`` keeps those checks on the content itself (the desk
store, ``desk_run.publish``, stores the same content): an issuer for every bond, a rate for every currency, each
curve's points on every bucket, gamma factors on the run's curves and rates, the known spread types and book types,
risk curves among the run's curves and a dv01 equal to the sum of its risk.
"""

from __future__ import annotations

import re
from typing import Any

from rub_fixed_income import RISK_BUCKETS

from desk.publish import tdm_run

SPREAD_TYPES = ("Z", "G", "I", "DM", "OAS", "ASW")
BOOK_TYPES = ("TRADING", "HTM", "DERIVS")
GAMMA_FACTOR = re.compile(r"^(IR\|[^|]+\|[^|]+|CS|FX\|[A-Z]{3})$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def content(ctx, positions, prev=None, inputs=None, lineage: dict[str, Any] | None = None) -> tdm_run.RunContent:
    return tdm_run.run_content(ctx, positions, prev, lineage or {}, inputs)


def warnings(c: tdm_run.RunContent) -> list[str]:
    """The run's WARN lines, as ``desk_run.publish`` returns them."""
    return [m for level, m in c.log if level == "WARN"]


def consistency(c: tdm_run.RunContent) -> list[str]:
    """What does not hang together in the run content; empty when all does."""
    err: list[str] = []
    rows, ref = c.rows, c.reference
    if not rows:
        err.append("no position")
    issuers = {i["issuer"] for i in ref["issuers"]}
    for r in rows:
        if r.get("issuer") is not None and r["issuer"] not in issuers:
            err.append(f"issuer {r['issuer']!r} not in the reference")
        if r["instr_type"] in ("BOND", "CDS") and r.get("issuer") is None:
            err.append(f"{r['position_id']}: a bond without an issuer")
    have = {"RUB", *(x["ccy"] for x in ref["fx"])}
    used = {r.get("ccy") for r in rows} | {c for r in rows for c in r.get("fx_delta") or {}} \
        | {x.get("ccy") for x in ref["curves"]}
    err += [f"no rate for {ccy}" for ccy in sorted(used - have - {None})]
    curves = {x["curve_id"] for x in ref["curves"]}
    for x in ref["curves"]:
        if x["points"] and sorted(p["bucket"] for p in x["points"]) != sorted(RISK_BUCKETS):
            err.append(f"curve {x['curve_id']}: {len(x['points'])} points, not one per bucket")
    for r in rows:
        pid = r["position_id"]
        for a, b, _ in r.get("gamma") or []:
            for f in (a, b):
                if not GAMMA_FACTOR.match(f):
                    err.append(f"{pid}: gamma factor {f!r} unknown")
                elif f.startswith("IR|") and (f.split("|")[1] not in curves or f.split("|")[2] not in RISK_BUCKETS):
                    err.append(f"{pid}: gamma factor {f!r} not on a run curve and bucket")
                elif f.startswith("FX|") and f[3:] not in have:
                    err.append(f"{pid}: gamma factor {f!r} without a rate")
        if r.get("spread_type") is not None and r["spread_type"] not in SPREAD_TYPES:
            err.append(f"{pid}: spread_type {r['spread_type']!r}")
        if r.get("yield_date") is not None and not DATE.match(str(r["yield_date"])):
            err.append(f"{pid}: yield_date {r['yield_date']!r}")
        if r.get("spread_curve") is not None and r["spread_curve"] not in curves:
            err.append(f"{pid}: spread_curve {r['spread_curve']!r} not a run curve")
        risk = r.get("risk") or {}
        err += [f"{pid}: risk on {c!r}, not a run curve" for c in risk if c not in curves]
        total = sum(v for bs in risk.values() for v in bs.values())
        if abs((r.get("dv01") or 0.0) - total) > max(100.0, 1e-3 * abs(r.get("dv01") or 0.0)):
            err.append(f"{pid}: dv01 {r.get('dv01')} but its risk sums to {total}")
    books = {b["book_id"]: b for b in ref["books"]}
    for b, x in books.items():
        if x.get("book_type") is not None and x["book_type"] not in BOOK_TYPES:
            err.append(f"book {b}: book_type {x['book_type']!r}")
        if x.get("ocp_to") is not None and x["ocp_to"] not in books:
            err.append(f"book {b}: ocp_to {x['ocp_to']!r} not a run book")
    return err
