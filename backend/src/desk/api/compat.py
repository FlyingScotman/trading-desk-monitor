"""Compatibility adapter (05 §4): ``window.DESK`` from the desk store, in the shape ``tdm.assemble.assemble``
produces. A port of that function onto the store's rows (run_position.fact/pnl, run_reference); the tdm
writer's defaults (zero cs01/jtd, nonzero risk/FX/gamma only, RUB at 1) are applied here. What the store
does not hold yet (funding, OCP, scenario results, official PnL, CBR meetings, bond quotes) is rendered as the tdm
assembler renders a run without it. ``history`` is the default runs of the earlier dates, where tdm's
desk_history holds the latest published EOD run of each date (any cut). A position the run did not value (no
fact: no data at all, an OTC row without the report's value) is listed after the valued ones with ``notValued``,
no PV (``mvRub`` null), zero risk and PnL and its reason in ``extra``, so the positions list shows it and no total
moves; it has no segment and no instrument type (the one addition beyond tdm's assembler, which drops such a
position). compat, removed with window.DESK (M7); never extended otherwise."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from rub_fixed_income import RISK_BUCKETS, RISK_PILLARS

from ..store import DEFAULT_RUNS

PNL_KEYS = ("carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "funding", "convexity",
            "new_trades", "other")
CARRY_HORIZONS = [["1M", 1], ["3M", 3], ["6M", 6], ["1Y", 12]]
DEFAULT_CARRY = [{"id": "flat", "name": "КС без изменений", "ks": {"mode": "flat"}}]
RATINGS = ["AAA", "AA+", "AA", "AA-", "A+", "A", "A-", "BBB+", "BBB", "BBB-", "BB+", "BB", "BB-", "B+", "B",
           "B-", "CCC", "D"]  # tdm ref_rating seed order
RUB = {"ccy": "RUB", "rate": 1.0, "move_pct": 0.0, "rate_cbr": 1.0, "move_pct_cbr": 0.0, "rate_cbr_next": 1.0}
CONTRACT_VERSION = "1.3"
HISTORY_DAYS = 22  # tdm.assemble.HISTORY_DAYS

def resolve_run(con, as_of: date | None = None, run_id: int | None = None) -> dict[str, Any] | None:
    """As tdm.assemble.resolve_run: the complete run run_id, else the default run of as_of, else the latest
    default run."""
    if run_id:
        return con.execute("SELECT * FROM run WHERE run_id = %s AND status = 'complete'",
                           (run_id,)).fetchone()
    if as_of:
        return con.execute(f"SELECT * FROM run WHERE run_id IN ({DEFAULT_RUNS}) AND business_date = %s",
                           (as_of,)).fetchone()
    return con.execute(f"SELECT * FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " ORDER BY business_date DESC LIMIT 1").fetchone()


def _years(d0: str, d1: str | None) -> float | None:
    try:
        return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _zero(v: float | None) -> float:
    return 0.0 if v is None else v


def _prev_weekday(d: date) -> date:
    # shortcut: Mon-Fri as tdm without ref_calendar; a run without prev_date is an S1-style run
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def gamma_summary(rows, mv_rub, convexity, cs01) -> dict[str, Any]:
    """As tdm.assemble.gamma_summary: RUB per bp^2 to parallel IR, to the spread and mixed."""
    ir = cs = ircs = 0.0
    if rows:
        for a, b, v in rows:
            k = 1 if a == b else 2
            ta, tb = a.split("|")[0], b.split("|")[0]
            if ta == "IR" and tb == "IR":
                ir += k * v
            elif ta == "CS" and tb == "CS":
                cs += v
            elif {ta, tb} == {"IR", "CS"}:
                ircs += v
        src = "matrix"
    elif convexity and mv_rub:
        ir = mv_rub * convexity * 1e-8
        if cs01:
            cs = ircs = ir
        src = "convexity"
    else:
        src = None
    return {"gammaIr": ir, "gammaCs": cs, "gammaIrCs": ircs, "gammaSource": src}


def _gamma(rows: list[list[Any]] | None) -> list[list[Any]]:
    """The writer's position_gamma: nonzero pairs, factors sorted, ordered by factor."""
    out: dict[tuple[str, str], float] = {}
    for a, b, v in rows or []:
        if v:
            out[min(a, b), max(a, b)] = v
    return [[a, b, v] for (a, b), v in sorted(out.items())]


def run_totals(rows) -> tuple[float, float, float, float]:
    """As tdm.writer.run_totals on rows (fact, pnl) of a run without funding deals: (official, explained,
    dv01, cs01). A position without official PnL counts its explained PnL plus the FX basis (CBR rates),
    one without PnL nothing."""
    off, exp = [], []
    for r in rows:
        if q := r["pnl"]:
            e = sum(q.get(k) or 0.0 for k in PNL_KEYS)  # tdm EXPLAINED_SQL, the same order
            g, f = q.get("fx_gross") or 0.0, q.get("fx_funding") or 0.0
            gc = q["fx_gross_cbr"] if q.get("fx_gross_cbr") is not None else g
            fc = q["fx_funding_cbr"] if q.get("fx_funding_cbr") is not None else f
            off.append(q["official"] if q.get("official") is not None else e + (gc + fc - g - f))
            exp.append(e)
    facts = [r["fact"] for r in rows]  # SQLite SUM is compensated too
    return (math.fsum(off), math.fsum(exp), math.fsum(p.get("dv01") or 0.0 for p in facts),
            math.fsum(p.get("cs01") or 0.0 for p in facts))


def _not_valued(r: dict[str, Any], books: dict[str, dict]) -> dict[str, Any]:
    """A run_position without fact as a Desk Monitor position: visible, PV empty, nothing in any total."""
    zero = dict.fromkeys(("carry", "rolldown", "rates", "spread", "fx", "funding", "convexity", "newTrades", "other",
                          "fxGross", "fxFunding", "fxGrossCbr", "fxFundingCbr", "fxCbr", "explained", "explainedCbr",
                          "fxBasis", "unexplained"), 0.0)
    return {
        "id": r["position_id"], "book": r["book_id"], "assetClass": None, "segment": None, "instrType": None,
        "name": r["secid"] or r["position_id"], "isin": None, "issuer": None, "issuerGroup": None, "sector": None,
        "rating": None, "ccy": None, "maturity": None, "tenorY": None, "side": None, "notional": None,
        "notionalRub": None, "price": None, "mv": None, "mvRub": None, "dv01": 0.0, "cs01": 0.0, "ofzSpr01": 0.0,
        "risk": {}, "fxDelta": {}, "jtd": 0.0, "gamma": [], **gamma_summary([], None, None, 0.0),
        "entity": books.get(r["book_id"], {}).get("entity", "—"), "newToday": False, "notValued": True,
        "pnl": {**zero, "official": None, "breakNote": None},
        "extra": {"quality": "not_valued", "valuation": "not_valued", "reason": r["reason"],
                  "quantity": r["quantity"]},
    }


def assemble(con, run: dict[str, Any]) -> dict[str, Any]:
    rid, as_of = run["run_id"], run["business_date"].isoformat()
    ref = {r["kind"]: r["data"]
           for r in con.execute("SELECT kind, data FROM run_reference WHERE run_id = %s", (rid,))}
    B = list(RISK_BUCKETS)
    bidx = {b: i for i, b in enumerate(B)}

    curves, moves = {}, {}
    for c in ref.get("curves", []):
        pts, mv = [None] * len(B), [0.0] * len(B)
        for p in c["points"]:
            pts[bidx[p["bucket"]]], mv[bidx[p["bucket"]]] = p["rate"], p.get("move_bp") or 0
        curves[c["curve_id"]] = {"name": c["name"], "ccy": c["ccy"], "kind": c["kind"], "pts": pts}
        moves[c["curve_id"]] = mv

    fxr = list({"RUB": RUB, **{r["ccy"]: r for r in ref.get("fx", [])}}.values())  # RUB first, as in tdm
    fx = {r["ccy"]: r["rate"] for r in fxr}
    fx_move = {r["ccy"]: r.get("move_pct") or 0 for r in fxr}
    fx_cbr = {r["ccy"]: r["rate_cbr"] if r.get("rate_cbr") is not None else r["rate"] for r in fxr}
    fx_cbr_move = {r["ccy"]: r["move_pct_cbr"] if r.get("move_pct_cbr") is not None
                   else (fx_move[r["ccy"]] if r.get("rate_cbr") is None else 0.0) for r in fxr}
    fx_cbr_next = {r["ccy"]: r["rate_cbr_next"] if r.get("rate_cbr_next") is not None else fx_cbr[r["ccy"]]
                   for r in fxr}
    indices = {r["index_name"]: r["value"] for r in ref.get("indices", [])}

    prows = con.execute("SELECT position_id, fact, pnl FROM run_position WHERE run_id = %s"
                        " AND fact IS NOT NULL ORDER BY ord", (rid,)).fetchall()
    has_bonds = {r["fact"]["book_id"] for r in prows if r["fact"].get("instr_type") == "BOND"}
    books = {}
    for r in ref.get("books", []):
        b = {"name": r["name"], "desk": r["desk"], "trader": r.get("trader"),
             "bookType": r.get("book_type") or ("TRADING" if r["book_id"] in has_bonds else "DERIVS"),
             "entity": r.get("entity") or "—"}
        if r.get("ocp_to"):
            b["ocpTo"] = r["ocp_to"]
        books[r["book_id"]] = b
    issuers, disp = {}, {}
    for r in ref.get("issuers", []):
        disp[r["issuer"]] = r.get("name") or r["issuer"]
        x = {"group": r["issuer_group"], "sector": r.get("sector"), "rating": r.get("rating")}
        if r.get("is_gov"):
            x["gov"] = True
        if r.get("is_quasi"):
            x["quasi"] = True
        issuers[disp[r["issuer"]]] = x
    used = list(dict.fromkeys(x["rating"] for x in issuers.values() if x["rating"]))
    rating_order = sorted(used, key=lambda r:
                          RATINGS.index(r) if r in RATINGS else len(RATINGS) + used.index(r))

    positions = []
    for row in prows:
        p, q = row["fact"], row["pnl"]
        pid = row["position_id"]
        iname = disp.get(p["issuer"], p["issuer"]) if p.get("issuer") else None
        iss = issuers.get(iname) if iname else None
        comp = {k: (q.get(k) or 0.0) if q else 0.0 for k in PNL_KEYS}
        explained = sum(comp.values())
        gross_cbr = q["fx_gross_cbr"] if q and q.get("fx_gross_cbr") is not None else comp["fx_gross"]
        fund_cbr = q["fx_funding_cbr"] if q and q.get("fx_funding_cbr") is not None else comp["fx_funding"]
        fx_basis = (gross_cbr + fund_cbr) - (comp["fx_gross"] + comp["fx_funding"])
        explained_cbr = explained + fx_basis
        official = q.get("official") if q else None
        cs01 = _zero(p.get("cs01"))
        gamma = _gamma(p.get("gamma"))
        risk = {}
        for curve, bk in (p.get("risk") or {}).items():
            for b, dv in bk.items():
                if dv:
                    risk.setdefault(curve, [0.0] * len(B))[bidx[b]] = dv
        pos = {
            "id": pid, "book": p["book_id"], "assetClass": p.get("asset_class"), "segment": p.get("segment"),
            "instrType": p.get("instr_type"), "name": p.get("name"), "isin": p.get("isin"), "issuer": iname,
            "issuerGroup": iss["group"] if iss else None, "sector": iss["sector"] if iss else None,
            "rating": iss["rating"] if iss else None, "ccy": p.get("ccy"), "maturity": p.get("maturity"),
            "tenorY": p["tenor_y"] if p.get("tenor_y") is not None else _years(as_of, p.get("maturity")),
            "couponType": p.get("coupon_type"), "coupon": p.get("coupon"), "index": p.get("index_name"),
            "margin": p.get("margin"), "side": p.get("side"),
            "notional": p.get("notional"), "notionalRub": p.get("notional_rub"), "price": p.get("price"),
            "mv": p.get("mv"), "mvRub": p.get("mv_rub"), "ytm": p.get("ytm"), "spreadBp": p.get("spread_bp"),
            "mdur": p.get("mdur"), "sprDur": p.get("spr_dur"), "convexity": p.get("convexity"),
            "dv01": p.get("dv01"), "cs01": cs01, "ofzSpr01": p.get("ofz_spr01") or 0.0, "risk": risk,
            "fxDelta": {c: d for c, d in (p.get("fx_delta") or {}).items() if d}, "jtd": _zero(p.get("jtd")),
            "wal": p.get("wal"), "cpr": p.get("cpr"), "counterparty": p.get("counterparty"),
            "newToday": bool(p.get("new_today")),
            "gamma": gamma, **gamma_summary(gamma, p.get("mv_rub"), p.get("convexity"), cs01),
            "entity": books.get(p["book_id"], {}).get("entity", "—"),
            "pnl": {
                "carry": comp["carry"], "rolldown": comp["rolldown"], "rates": comp["rates"],
                "spread": comp["spread"],
                "fx": comp["fx_gross"] + comp["fx_funding"], "funding": comp["funding"],
                "convexity": comp["convexity"], "newTrades": comp["new_trades"], "other": comp["other"],
                "fxGross": comp["fx_gross"], "fxFunding": comp["fx_funding"], "fxGrossCbr": gross_cbr,
                "fxFundingCbr": fund_cbr, "fxCbr": gross_cbr + fund_cbr, "explained": explained,
                "explainedCbr": explained_cbr, "fxBasis": fx_basis,
                "unexplained": 0.0 if official is None else official - explained_cbr, "official": official,
                "breakNote": q.get("break_note") if q else None,
            },
        }
        if p.get("accrual_only"):
            pos.update({"accrualOnly": True, "purchaseDate": p.get("purchase_date"),
                        "purchasePrice": p.get("purchase_price"), "purchaseYield": p.get("purchase_yield"),
                        "contractRate": p.get("contract_rate")})
        if p.get("ocp_transfer") or (p.get("extra") or {}).get("ocp_transfer"):
            pos["ocpTransfer"] = True
        if p.get("instr_type") == "BOND":
            # shortcut: no bond quotes in the store (tdm md_bond_quote); the market proxy is a later slice
            pos.update({"instrumentId": p.get("instrument_id") or p.get("isin"), "accrued": p.get("accrued"),
                        "couponFreq": p.get("coupon_freq"), "yieldDate": p.get("yield_date"),
                        "spreadType": p.get("spread_type"), "spreadCurve": p.get("spread_curve"),
                        "quote": None})
        if p.get("extra") is not None:
            pos["extra"] = p["extra"]
        positions.append(pos)
    for r in con.execute("SELECT position_id, book_id, secid, quantity, reason FROM run_position WHERE run_id = %s"
                         " AND fact IS NULL ORDER BY ord", (rid,)):
        positions.append(_not_valued(r, books))

    scenarios = [{"id": s["scenario_id"], "name": s["name"], "kind": s["kind"], "description": s["description"],
                  **s["definition"]} for s in con.execute(
        "SELECT * FROM scenario_def WHERE active ORDER BY ord, scenario_id")]
    hist = con.execute(f"SELECT run_id, business_date FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " AND business_date < %s ORDER BY business_date DESC LIMIT %s",
                       (run["business_date"], HISTORY_DAYS - 1)).fetchall()[::-1]
    history, cum = [], 0.0
    for d, rows in [(h["business_date"].isoformat(), con.execute(
            "SELECT jsonb_build_object('dv01', fact->'dv01', 'cs01', fact->'cs01') AS fact, pnl FROM run_position"
            " WHERE run_id = %s AND fact IS NOT NULL", (h["run_id"],)).fetchall()) for h in hist] + [(as_of, prows)]:
        off, exp, dv01, cs01 = run_totals(rows)
        cum += off
        history.append({"date": d, "pnlOfficial": off, "pnlExplained": exp, "pnlCum": cum, "dv01": dv01,
                        "cs01": cs01})
    prev = run["prev_date"] or _prev_weekday(run["business_date"])
    zero_book = {"carry": 0.0, "fx": 0.0, "fxCbr": 0.0, "fxAllocated": 0.0, "inPositions": 0.0}
    completed = run["completed_at"]
    return {
        "asOf": as_of, "prevDate": prev.isoformat(),
        "accrualDays": max(1, (run["business_date"] - prev).days), "reportingCcy": "RUB",
        "BUCKETS": B, "PILLARS": list(RISK_PILLARS), "CURVES": curves, "CURVE_MOVES": moves,
        "FX": fx, "FX_MOVE_PCT": fx_move, "FX_CBR": fx_cbr, "FX_CBR_MOVE_PCT": fx_cbr_move,
        "FX_CBR_NEXT": fx_cbr_next,
        "INDICES": indices, "ISSUERS": issuers, "RATING_ORDER": rating_order,
        "BOOKS": books, "SCENARIOS": scenarios, "scenarioResults": {},
        "JTD_RECOVERY": 0.0, "FUNDING_RULES": [], "OCP_TRANSFERS": [],
        "CBR_MEETINGS": [], "CARRY_SCENARIOS": DEFAULT_CARRY, "CARRY_HORIZONS": CARRY_HORIZONS,
        "official": {"asOf": as_of, "received": any(p["pnl"]["official"] is not None for p in positions),
                     "basis": "CBR", "receivedAt": None},
        "positions": positions, "funding": [], "fundingPnl": {"carry": 0.0, "fx": 0.0, "fxCbr": 0.0},
        "fundingPnlByBook": {b: dict(zero_book) for b in books}, "history": history,
        "source": {"kind": "db", "runId": rid, "runType": run["run_type"],
                   "publishedAt": completed.isoformat() if completed else None,
                   "libVersion": f"rub-fixed-income-core {run['core_version']}",
                   "marketSnapshot": run["market_snapshot_id"], "contractVersion": CONTRACT_VERSION},
    }
