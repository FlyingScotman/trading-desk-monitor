"""A run's sums for the concept pages' series (R23; table ``run_summary``, migration 0011).

The desk's series over time (``desk.api.concepts``) sum every default run's positions: risk by curve, CS01, OFZ
basis, DV01 by book, MV by instrument type and currency, PnL by factor, and per position its face, DV01, CS01,
basis, MV, risk by curve, PnL, price and spread. Read from ``run_position`` on every page load that is a query of
hundreds of thousands of rows at a year of runs; a run never changes, so its sums are computed once
(``ensure``: by ``desk eod`` right after the publication, by the API for a run of an older version on first read)
and the pages read one row per run.

Per position (``pos``): ``[face, dv01, cs01, basis, mv, pnl, price, spread_bp, {curve: dv01}, dm_bp, ofz_spr, ofz_ruo]``
with
``face`` the fact's ``notional`` (face currency), ``dv01`` the sum of its risk by curve (RUB per bp, the same sum the
pages' DV01 is), ``basis`` the OFZ basis to RUONIA (``basis_of``), ``mv`` as the type series count it (a bond's PV in
RUB, a derivative's or FX row's notional in RUB), ``pnl`` the day's total over the pages' factors (null without a flash
PnL), ``dm_bp`` a floater's discount margin (core 0.33; null for a fixed bond and in a run before it), ``ofz_spr`` an
OFZ's spread risk to the KBD (0.2.6, ``basis_of``; null in a run before it), ``ofz_ruo`` an OFZ's RUONIA risk computed
apart from its basis (``extra.ofz_ruonia01``, TDM#213; null without it). Curves are the pages' ids (``CURVE_MAP``): the
«RUONIA + базис» view, an OFZ's RUONIA risk on RUONIA (its row's ``RUB RUONIA`` plus ``RUB OFZ``: the RUONIA risk; a
row without ``ofz_ruonia01`` has its KBD risk there, the basis again: variant P). The KBD view is RUONIA minus the
OFZ's RUONIA risk (``ofzRuo``: ``ofz_ruo``, else the basis) with the basis on the KBD.
A position's ``basis`` is ``basis_of`` as it is, an OFZ-IN's nominal proxy included (the pages tell it by the position,
``ofz_in_proxy``); the run's sum ``basis`` leaves the proxies out, ``basisIn`` is their sum (TDM#173); ``ofzRuo`` sums the
RUONIA risk of the OFZ whose basis is in ``basis`` (an OFZ-PK's basis is its own spread, it has none); it is null in a
run with no OFZ on the KBD (before 0.2.6), whose KBD view takes the basis off RUONIA as before."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

VERSION = 6  # 2: a floater's discount margin (pos[9]); 3: the OFZ basis of the KBD scheme, the spread to the KBD;
# 4: an OFZ-IN's nominal proxy out of the run's basis (basisIn, TDM#173); 5: an OFZ's RUONIA risk apart from its basis
# (ofzRuo, pos[11]; TDM#213); 6: the funding its own factor, the carry without it (TDM#168)
CURVE_MAP = {"RUB OFZ": "RUB RUONIA", "USD": "USD SOFR", "EUR": "EUR ESTR", "CNY": "CNY XCCY"}
"""A run's risk curve -> the concept pages' curve (``desk.api.concepts`` re-exports it; ``lib/real.js`` mirrors it).
``RUB OFZ`` (the KBD: a fixed-coupon OFZ's rates risk since 0.2.6, its basis in RUONIA + basis since 0.2.7, every bond's
on a date without RUONIA OIS) is added to RUONIA: the pages' default «RUONIA + базис» view, where moving RUONIA moves the
KBD with it (a row in RUONIA + basis: its ``RUB RUONIA`` plus ``RUB OFZ`` is its RUONIA risk, ``valuation.bonds.ofz_levels``)."""
PNL = {"carry": ("carry", "rolldown"), "funding": ("funding",), "rates": ("rates", "convexity"), "spread": ("spread",),
       "fx": ("fx_gross", "fx_funding"), "trading": ("new_trades",), "other": ("other",)}
"""The pages' PnL factors from the run's components (``compat.PNL_KEYS``): the carry is the coupon income and the
roll-down without the funding, the position's funding (B1) is its own factor (TDM#168: 0 in a run before B1). The
factors sum to the day's total as before."""
GOV_CLASSES = {"ОФЗ-ПД": "PD", "ОФЗ-АД": "PD", "ОФЗ-ПК": "PK", "ОФЗ-ИН": "IN"}

ROWS_SQL = ("SELECT position_id, book_id, pv_rub, fact->'risk' AS risk, (fact->>'cs01')::float8 AS cs01,"
            " (fact->>'ofz_spr01')::float8 AS ofz, fact->>'instr_type' AS instr_type, fact->>'segment' AS segment,"
            " fact->>'asset_class' AS asset_class, fact->>'coupon_type' AS coupon_type, fact->>'ccy' AS ccy,"
            " (fact->>'notional')::float8 AS notional, (fact->>'notional_rub')::float8 AS notional_rub,"
            " (fact->>'price')::float8 AS price, (fact->>'spread_bp')::float8 AS spread_bp,"
            " (fact->>'dv01')::float8 AS dv01, (fact->'extra'->>'dm_bp')::float8 AS dm_bp, pnl,"
            " (fact->'extra'->>'ofz_basis01')::float8 AS ofz_basis, fact->'extra'->>'quality' AS quality,"
            " (fact->'extra'->>'ofz_ruonia01')::float8 AS ofz_ruo"
            " FROM run_position WHERE run_id = %s AND fact IS NOT NULL")


MINFIN_INN = "7710168360"
"""The INN of the Ministry of Finance of the Russian Federation (hub static ``issuers.inn``; the hub's own
``is_sovereign`` flag and its bond classes ``ofz_*`` / ``sovereign_fx`` are derived from it)."""
MINFIN_CLASSES = frozenset({"sovereign_fx"})
"""Hub bond classes of the Minfin's foreign-currency bonds (Eurobonds and the replacement / FX bonds). ``tdm_run``
has no label for them, so the fact's ``asset_class`` is the class itself (its segment ``Корп``)."""
NOT_MINFIN_CLASSES = frozenset({"foreign_sovereign"})
"""A foreign state's bonds (e.g. Belarus on MOEX): a sovereign, but never the Russian Minfin."""


def is_minfin(f: dict[str, Any], gov_issuers: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a bond's fact is the Russian Minfin's (R39): identified by the issuer, not by an ISIN list. The issuer
    is the Minfin when the run's issuer reference flags it (``gov_issuers``: the issuer keys with ``is_gov``, i.e. the
    hub's ``is_sovereign``, which the hub sets for INN ``MINFIN_INN`` only -- the Amur region's Minfin is not one) or
    the hub's bond class says so (OFZ: segment ``Гос`` / ``GOV_CLASSES``; ``MINFIN_CLASSES``), never for a foreign
    sovereign (``NOT_MINFIN_CLASSES``)."""
    ac = f.get("asset_class")
    if ac in NOT_MINFIN_CLASSES:
        return False
    return (f.get("segment") == "Гос" or ac in GOV_CLASSES or ac in MINFIN_CLASSES
            or (f.get("issuer") is not None and f.get("issuer") in gov_issuers))


def kind_of(f: dict[str, Any], gov_issuers: frozenset[str] | set[str] = frozenset()) -> tuple[str, str]:
    """(group, kind) of a position's fact: GOV PD / PK / IN / SOVFX, SUB FIX / FLT, CORP FIX / FLT, DER OTC / FX,
    OTH OTH. A floater is a bond whose coupon type is ``float``. The Minfin's bonds (``is_minfin``) are GOV: in
    roubles an OFZ by its class, in a foreign currency SOVFX (the Minfin's Eurobonds and replacement bonds)."""
    it = f.get("instr_type")
    if it == "FX":
        return "DER", "FX"
    if it == "OTC":
        return "DER", "OTC"
    if it not in ("BOND", None):
        return "OTH", "OTH"
    seg, ac = f.get("segment"), f.get("asset_class")
    flt = "FLT" if f.get("coupon_type") == "float" else "FIX"
    if is_minfin(f, gov_issuers):
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


def basis_of(x: dict[str, Any]) -> tuple[float, float | None]:
    """(the OFZ basis to RUONIA, the spread risk to the KBD) of a ``ROWS_SQL`` row, RUB per bp: in the KBD scheme
    (0.2.6, ``extra.ofz_basis01`` present: ``valuation.bonds.OFZ_SCHEME``) the basis is the OFZ's par DV01 on the KBD
    and ``ofz_spr01`` its spread to the KBD; before, ``ofz_spr01`` was the spread to RUONIA, i.e. the basis, and there
    was no spread to the KBD (None)."""
    if x.get("ofz_basis") is not None:
        return x["ofz_basis"], x["ofz"] or 0.0
    return x["ofz"] or 0.0, None


def ofz_in_proxy(is_in: bool, ofz_basis: float | None, quality: str | None) -> bool:
    """Whether an OFZ's par-par basis (``basis_of``) is an OFZ-IN's nominal proxy (TDM#173): an OFZ-IN (``is_in``: kind
    ``("GOV", "IN")``, a linker) of the KBD scheme (``ofz_basis`` present) that core did not value (``quality`` not
    ``exact``). Core has no real-rate curve or inflation factor yet (TDM#38), so the fallback books the linker's nominal
    duration on the KBD: its KBD DV01 is a nominal-rate proxy, not a basis to RUONIA. It is left out of the OFZ basis
    and shown apart (the run's ``basisIn``, the risk table's ``dv01_ofz_in_proxy_rub``, the pages' ``ofzIn01``); its rate
    risk stays where the view puts the KBD (RUONIA in «RUONIA + базис», ``RUB OFZ`` in «КБД»). ``lib/real.js``
    mirrors it (``isInProxy``)."""
    return is_in and ofz_basis is not None and quality != "exact"


def _r(v: float | None, nd: int) -> float | None:
    return None if v is None or not math.isfinite(v) else round(v, nd)


def build(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """The summary of a run from its ``ROWS_SQL`` rows (module doc)."""
    dv01: dict[str, float] = {}
    book: dict[str, float] = {}
    mv_type: dict[str, float] = {}
    mv_ccy: dict[str, float] = {}
    cs01 = basis = basis_pk = basis_in = ofz_spr = ofz_ruo = 0.0
    kbd_ofz = False  # a run before 0.2.6 has no OFZ on the KBD: its ofzRuo is null, not 0 (the pages read null)
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
        b, spr = basis_of(x)
        ofz_spr += spr or 0.0
        book[x["book_id"]] = book.get(x["book_id"], 0.0) + (x["dv01"] or 0.0)
        g, k = kind_of(f)
        if ofz_in_proxy((g, k) == ("GOV", "IN"), x.get("ofz_basis"), x.get("quality")):
            basis_in += b  # an OFZ-IN's nominal proxy: not a basis (TDM#173)
        else:
            basis += b
            if x.get("ofz_basis") is not None:  # an OFZ on the KBD: its RUONIA risk, the basis again without one (P)
                kbd_ofz = True
                ofz_ruo += x["ofz_ruo"] if x.get("ofz_ruo") is not None else b
        if (g, k) == ("GOV", "PK"):  # an OFZ-PK's basis is its own spread to RUONIA, no KBD risk (the KBD view)
            basis_pk += b
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
        pos[x["position_id"]] = [_r(x["notional"], 2), _r(sum(curves.values()), 2), _r(x["cs01"], 2), _r(b, 2),
                                 _r(m, 0), _r(p["total"], 0) if p else None, _r(x["price"], 4),
                                 _r(x["spread_bp"], 2), {c: round(v, 2) for c, v in curves.items() if v},
                                 _r(x.get("dm_bp"), 2), _r(spr, 2), _r(x.get("ofz_ruo"), 2)]
    return {"dv01": dv01, "cs01": cs01, "basis": basis, "basisPk": basis_pk, "basisIn": basis_in, "ofzSpr": ofz_spr,
            "ofzRuo": ofz_ruo if kbd_ofz else None,
            "dv01Book": book,
            "mvType": mv_type,
            "mvCcy": mv_ccy, "pnl": pnl, "pos": pos}


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
