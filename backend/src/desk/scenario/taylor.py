"""The T1 scenario estimate: the run's stored risk as a payload, and the Taylor sum over it (S6 §3).

``risk_payload`` turns the published facts (``tdm_run.position_row``) of a run's valued positions into
indexed factors. ``curves``: the curves a scenario may shock, the run's published curves, those of the risk and
``RUB_CURVES`` (sorted). Factor order: ``IR|<curve>|<bucket>`` by curve (sorted) x ``RISK_BUCKETS``, then ``CS``,
then ``FX|<ccy>`` (sorted); only factors that occur are listed. Per position ``delta`` holds the nonzero
first order in factor order (IR RUB/bp from ``risk``, CS RUB/bp from ``cs01`` or ``ofz_spr01``, FX RUB per
1 % = ``fx_delta x fx_rate / 100``), ``gamma`` the upper-triangle Hessian as ``[i, j, RUB/(unit_i x unit_j)]``
with i <= j, sorted.

``run_taylor`` is the reference the browser's TS port (``frontend/src/taylor``) matches to 1e-9 relative
(``tests/fixtures/taylor_parity.json``): plain left-to-right sums in payload order, no ``fsum``; a gamma
term is ``c * v * x_i * x_j`` evaluated left to right, c = 0.5 on the diagonal, else 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rub_fixed_income import RISK_BUCKETS

from ..marketdata import ratings
from .definition import RUB_CURVES, own_spread_shock

PARTS = ("rates", "spread", "fx", "convexity", "total")


def risk_payload(run: Mapping[str, Any], rows: list[Mapping[str, Any]], reference: Mapping[str, list]
                 ) -> dict[str, Any]:
    """``run``: ``run_id`` and ``business_date``; ``rows``: ``run_position`` dicts in order (a row without
    ``fact`` is not valued and left out); ``reference``: ``run_reference`` data by kind."""
    valued = [r for r in rows if r.get("fact")]
    issuers = {i["issuer"]: i for i in reference.get("issuers") or []}
    curves = sorted({c["curve_id"] for c in reference.get("curves") or []}
                    | {c for r in valued for c in (r["fact"].get("risk") or {})} | set(RUB_CURVES))
    firsts, seen = [], set()
    for r in valued:
        f = r["fact"]
        d = {f"IR|{c}|{b}": x for c, bs in (f.get("risk") or {}).items() for b, x in bs.items()}
        d["CS"] = f.get("cs01") or f.get("ofz_spr01") or 0.0
        rate = (f.get("extra") or {}).get("fx_rate")
        d.update({f"FX|{c}": x * rate / 100 for c, x in (f.get("fx_delta") or {}).items() if x and rate})
        d = {k: x for k, x in d.items() if x}
        firsts.append(d)
        seen |= set(d) | {k for a, b, _ in f.get("gamma") or [] for k in (a, b)}
    factors = [k for k in (*(f"IR|{c}|{b}" for c in curves for b in RISK_BUCKETS), "CS",
                           *sorted(k for k in seen if k.startswith("FX|"))) if k in seen]
    idx = {k: i for i, k in enumerate(factors)}
    positions = []
    for r, d in zip(valued, firsts):
        f = r["fact"]
        iss = issuers.get(f.get("issuer")) or {}
        gamma = sorted([*sorted((idx[a], idx[b])), g] for a, b, g in f.get("gamma") or [] if g)
        positions.append({
            "position_id": r["position_id"], "book_id": r["book_id"], "secid": r["secid"], "name": f.get("name"),
            "quality": r.get("quality"), "issuer": f.get("issuer"), "sector": iss.get("sector"),
            "rating": ratings.position_rating(f, iss),  # the bond's own desk rating, else its issuer's (R25)
            "spread_kind": "ofz" if f.get("ofz_spr01") else "credit" if f.get("cs01") else None,
            "spread_bp": f.get("spread_bp"), "pv_rub": r["pv_rub"],
            "delta": sorted([idx[k], x] for k, x in d.items()), "gamma": gamma,
        })
    return {"run_id": run["run_id"], "business_date": str(run["business_date"]), "buckets": list(RISK_BUCKETS),
            "curves": curves, "factors": factors, "positions": positions}


def run_taylor(payload: Mapping[str, Any], norm: Mapping[str, Any]) -> dict[str, Any]:
    """Per position ``rates``, ``spread``, ``fx``, ``convexity``, ``total`` (RUB), ``second_order`` (has
    gamma) and ``max_move_bp`` (largest |x| over the IR factors and the own spread shock); ``totals`` by
    ``book_id`` and ``_total``, each part summed over the positions in order. Shock vector: x[IR|c|b] =
    curves[c][b] (0 without the curve), x[CS] = ``own_spread_shock``, x[FX|c] = fx[c] (0 when absent)."""
    curves, fx = norm.get("curves", {}), norm.get("fx", {})
    base = []
    for k in payload["factors"]:
        kind, _, rest = k.partition("|")
        if kind == "IR":
            c, _, b = rest.rpartition("|")
            base.append(curves[c][RISK_BUCKETS.index(b)] if c in curves else 0.0)
        else:
            base.append(fx.get(rest, 0.0) if kind == "FX" else 0.0)
    kinds = [k.partition("|")[0] for k in payload["factors"]]
    ir_max = 0.0
    for kind, x in zip(kinds, base):
        if kind == "IR" and abs(x) > ir_max:
            ir_max = abs(x)
    out, totals = [], {"_total": dict.fromkeys(PARTS, 0.0)}
    for p in payload["positions"]:
        ds = own_spread_shock(norm, p)
        x = [ds if kind == "CS" else v for kind, v in zip(kinds, base)]
        rates = spread = fxp = conv = 0.0
        for i, d in p["delta"]:
            if kinds[i] == "IR":
                rates += d * x[i]
            elif kinds[i] == "CS":
                spread += d * x[i]
            else:
                fxp += d * x[i]
        for i, j, v in p["gamma"]:
            conv += (0.5 if i == j else 1.0) * v * x[i] * x[j]
        row = {"rates": rates, "spread": spread, "fx": fxp, "convexity": conv,
               "total": rates + spread + fxp + conv}
        out.append({"position_id": p["position_id"], **row, "second_order": bool(p["gamma"]),
                    "max_move_bp": max(ir_max, abs(ds))})
        for b in (p["book_id"], "_total"):
            t = totals.setdefault(b, dict.fromkeys(PARTS, 0.0))
            for k in PARTS:
                t[k] += row[k]
    return {"positions": out, "totals": totals}
