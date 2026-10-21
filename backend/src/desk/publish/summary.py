"""A run's sums for the concept pages' series (R23; table ``run_summary``, migration 0011).

The desk's series over time (``desk.api.concepts``) sum every default run's positions: risk by curve, CS01, OFZ
basis, DV01 by book, MV by instrument type and currency, PnL by factor, and per position its face, DV01, CS01,
basis, MV, risk by curve, PnL, price and spread. Read from ``run_position`` on every page load that is a query of
hundreds of thousands of rows at a year of runs; a run never changes, so its sums are computed once
(``ensure``: by ``desk eod`` right after the publication, by the API for a run of an older version on first read)
and the pages read one row per run.

Per position (``pos``): ``[face, dv01, cs01, basis, mv, pnl, price, spread_bp, {curve: dv01}]`` with
``face`` the fact's ``notional`` (face currency), ``dv01`` the sum of its risk by curve (RUB per bp, the same sum the
pages' DV01 is), ``basis`` its ``ofz_spr01``, ``mv`` as the type series count it (a bond's PV in RUB, a derivative's
or FX row's notional in RUB), ``pnl`` the day's total over the pages' factors (null without a flash PnL). Curves are
the pages' ids (``CURVE_MAP``)."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

VERSION = 1
CURVE_MAP = {"RUB OFZ": "RUB RUONIA", "USD": "USD SOFR", "EUR": "EUR ESTR", "CNY": "CNY XCCY"}
"""A run's risk curve -> the concept pages' curve (``desk.api.concepts`` re-exports it; ``lib/real.js`` mirrors it).
``RUB OFZ`` (the KBD) carries a bond's rates risk only on a date without RUONIA OIS, so it is added to RUONIA."""
PNL = {"carry": ("carry", "rolldown"), "rates": ("rates", "convexity"), "spread": ("spread",),
       "fx": ("fx_gross", "fx_funding"), "trading": ("new_trades",), "other": ("funding", "other")}
"""The pages' PnL factors from the run's components (``compat.PNL_KEYS``)."""
GOV_CLASSES = {"ОФЗ-ПД": "PD", "ОФЗ-АД": "PD", "ОФЗ-ПК": "PK", "ОФЗ-ИН": "IN"}

ROWS_SQL = ("SELECT position_id, book_id, pv_rub, fact->'risk' AS risk, (fact->>'cs01')::float8 AS cs01,"
            " (fact->>'ofz_spr01')::float8 AS ofz, fact->>'instr_type' AS instr_type, fact->>'segment' AS segment,"
            " fact->>'asset_class' AS asset_class, fact->>'coupon_type' AS coupon_type, fact->>'ccy' AS ccy,"
            " (fact->>'notional')::float8 AS notional, (fact->>'notional_rub')::float8 AS notional_rub,"
            " (fact->>'price')::float8 AS price, (fact->>'spread_bp')::float8 AS spread_bp,"
            " (fact->>'dv01')::float8 AS dv01, pnl FROM run_position WHERE run_id = %s AND fact IS NOT NULL")


def kind_of(f: dict[str, Any]) -> tuple[str, str]:
    """(group, kind) of a position's fact: GOV PD / PK / IN / SOVFX, SUB FIX / FLT, CORP FIX / FLT, DER OTC / FX,
    OTH OTH. A floater is a bond whose coupon type is ``float``; a government bond in a foreign currency is SOVFX."""
    it = f.get("instr_type")
    if it == "FX":
        return "DER", "FX"
    if it == "OTC":
        return "DER", "OTC"
    if it not in ("BOND", None):
        return "OTH", "OTH"
    seg, ac = f.get("segment"), f.get("asset_class")
    flt = "FLT" if f.get("coupon_type") == "float" else "FIX"
    if seg == "Гос" or ac in GOV_CLASSES:
        if (f.get("ccy") or "RUB") != "RUB":
            return "GOV", "SOVFX"
        return "GOV", GOV_CLASSES.get(ac, "PD")
    if seg == "Суб":
        return "SUB", flt
    return "CORP", flt


def pnl_factors(q: dict[str, Any] | None) -> dict[str, float] | None:
    if not q:
        return None
    out = {k: sum(q.get(c) or 0.0 for c in cs) for k, cs in PNL.items()}
    out["total"] = sum(out.values())
    return out


def _mv(f: dict[str, Any], pv: float | None) -> float:
    """Market value for the type and currency series: a bond's PV, a derivative's or FX row's notional (RUB)."""
    if f.get("instr_type") == "BOND":
        return pv or 0.0
    return abs(f.get("notional_rub") or 0.0)


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def build(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The summary of a run from its ``ROWS_SQL`` rows (module doc)."""
    dv01: dict[str, float] = {}
    book: dict[str, float] = {}
    mv_type: dict[str, float] = {}
    mv_ccy: dict[str, float] = {}
    cs01 = basis = 0.0
    pnl: dict[str, float] | None = None
    pos: dict[str, list] = {}
    for x in rows:
        f = dict(x)
        curves: dict[str, float] = {}
        for curve, bk in (x["risk"] or {}).items():
            c = CURVE_MAP.get(curve, curve)
            curves[c] = curves.get(c, 0.0) + sum(v or 0.0 for v in bk.values())
        curves = {c: v for c, v in curves.items() if math.isfinite(v)}
        for c, v in curves.items():
            dv01[c] = dv01.get(c, 0.0) + v
        cs01 += x["cs01"] or 0.0
        basis += x["ofz"] or 0.0
        book[x["book_id"]] = book.get(x["book_id"], 0.0) + (x["dv01"] or 0.0)
        g, k = kind_of(f)
        m = _mv(f, x["pv_rub"])
        mv_type[f"{g}.{k}"] = mv_type.get(f"{g}.{k}", 0.0) + m
        if x["instr_type"] == "BOND":
            c = x["ccy"] or "RUB"
            mv_ccy[c] = mv_ccy.get(c, 0.0) + (x["pv_rub"] or 0.0)
        p = pnl_factors(x["pnl"])
        if p:
            pnl = pnl or dict.fromkeys(p, 0.0)
            for kk, v in p.items():
                pnl[kk] += v
        pos[x["position_id"]] = [_r(x["notional"], 2), _r(sum(curves.values()), 2), _r(x["cs01"], 2), _r(x["ofz"], 2),
                                 _r(m, 0), _r(p["total"], 0) if p else None, _r(x["price"], 4),
                                 _r(x["spread_bp"], 2), {c: round(v, 2) for c, v in curves.items() if v}]
    return {"dv01": dv01, "cs01": cs01, "basis": basis, "dv01Book": book, "mvType": mv_type, "mvCcy": mv_ccy,
            "pnl": pnl, "pos": pos}


def ensure(con: psycopg.Connection, run_ids: Iterable[int]) -> int:
    """Write the missing (or older-version) summaries of these complete runs; returns how many were written. ``con``
    must be writable."""
    ids = list(run_ids)
    if not ids:
        return 0
    have = {r[0] for r in con.execute("SELECT run_id FROM run_summary WHERE run_id = ANY(%s) AND version = %s",
                                      (ids, VERSION))}
    todo = [r for (r,) in con.execute("SELECT run_id FROM run WHERE run_id = ANY(%s) AND status = 'complete'",
                                      (ids,)) if r not in have]
    for rid in todo:
        with con.cursor(row_factory=psycopg.rows.dict_row) as cur:
            data = build(cur.execute(ROWS_SQL, (rid,)).fetchall())
        con.execute("INSERT INTO run_summary (run_id, version, data) VALUES (%s, %s, %s) ON CONFLICT (run_id) DO UPDATE"
                    " SET version = EXCLUDED.version, data = EXCLUDED.data, built_at = clock_timestamp()",
                    (rid, VERSION, Jsonb(data, dumps=lambda o: json.dumps(o, ensure_ascii=False, allow_nan=False))))
    return len(todo)
