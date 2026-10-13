"""Сборка объекта window.DESK (форма — как prototypes/data.js) из опубликованного прогона."""
import json
from datetime import date

PNL_KEYS = ("carry", "rolldown", "rates", "spread", "fx_gross", "fx_funding", "convexity", "new_trades", "other")
HISTORY_DAYS = 22


def resolve_run(con, as_of=None, run_id=None):
    """Опубликованный прогон: по run_id, иначе последний на as_of, иначе самый последний."""
    if run_id:
        return con.execute("SELECT * FROM calc_run WHERE run_id=? AND status='complete'", (run_id,)).fetchone()
    if as_of:
        return con.execute("SELECT * FROM calc_run WHERE as_of=? AND status='complete' ORDER BY published_at DESC, run_id DESC LIMIT 1", (as_of,)).fetchone()
    return con.execute("SELECT * FROM calc_run WHERE status='complete' ORDER BY as_of DESC, published_at DESC, run_id DESC LIMIT 1").fetchone()


def _years(d0, d1):
    try:
        return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
    except (TypeError, ValueError):
        return None


def gamma_summary(rows, mv_rub, convexity, cs01):
    """Сводные вторые производные позиции, RUB на (б.п.)^2:
    gammaIr — к параллельному сдвигу всех кривых (сумма всех IR×IR, вне диагонали дважды),
    gammaCs — к спреду, gammaIrCs — смешанная ставки×спред.
    Без явной матрицы — из выпуклости: MV × C × 1e-8 (доходность = ставка + спред)."""
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


def bond_quotes(con, as_of):
    """Котировки облигаций на дату среза и на предыдущую дату с котировками: {instrument_id: (cur, prev)}.
    Если на as_of котировок нет — берётся последняя дата до неё (quote.date это покажет)."""
    d = con.execute("SELECT MAX(date) FROM md_bond_quote WHERE date <= ?", (as_of,)).fetchone()[0]
    if not d:
        return {}
    p = con.execute("SELECT MAX(date) FROM md_bond_quote WHERE date < ?", (d,)).fetchone()[0]
    cur = {r["instrument_id"]: r for r in con.execute("SELECT * FROM md_bond_quote WHERE date=?", (d,))}
    prev = {r["instrument_id"]: r for r in con.execute("SELECT * FROM md_bond_quote WHERE date=?", (p,))} if p else {}
    return {k: (v, prev.get(k)) for k, v in cur.items()}


def _quote(q):
    if not q:
        return None
    c, p = q
    out = {"date": c["date"], "price": c["clean_price"], "ytm": c["ytm"], "zSpread": c["z_spread_bp"], "gSpread": c["g_spread_bp"], "volume": c["volume_rub"],
           "prevDate": None, "prevPrice": None, "prevYtm": None, "prevZSpread": None, "prevGSpread": None}
    if p:
        out.update(prevDate=p["date"], prevPrice=p["clean_price"], prevYtm=p["ytm"], prevZSpread=p["z_spread_bp"], prevGSpread=p["g_spread_bp"])
    return out


def assemble(con, run):
    rid, as_of = run["run_id"], run["as_of"]
    rows = lambda sql, *a: con.execute(sql, (rid,) + a).fetchall()  # noqa: E731

    buckets = con.execute("SELECT bucket, pillar_years FROM ref_bucket ORDER BY ord").fetchall()
    B = [b["bucket"] for b in buckets]
    bidx = {b: i for i, b in enumerate(B)}

    curves, moves = {}, {}
    for c in rows("SELECT * FROM run_curve WHERE run_id=? ORDER BY ord"):
        curves[c["curve_id"]] = {"name": c["name"], "ccy": c["ccy"], "kind": c["kind"], "pts": [None] * len(B)}
        moves[c["curve_id"]] = [0.0] * len(B)
    for p in rows("SELECT * FROM mkt_curve_point WHERE run_id=?"):
        curves[p["curve_id"]]["pts"][bidx[p["bucket"]]] = p["rate"]
        moves[p["curve_id"]][bidx[p["bucket"]]] = p["move_bp"]

    fx = {r["ccy"]: r["rate"] for r in rows("SELECT * FROM mkt_fx WHERE run_id=? ORDER BY CASE ccy WHEN 'RUB' THEN 0 ELSE 1 END, rowid")}
    fx_move = {r["ccy"]: r["move_pct"] for r in rows("SELECT * FROM mkt_fx WHERE run_id=?")}
    indices = {r["index_name"]: r["value"] for r in rows("SELECT * FROM mkt_index WHERE run_id=?")}
    books = {r["book_id"]: {"name": r["name"], "desk": r["desk"], "trader": r["trader"]} for r in rows("SELECT * FROM run_book WHERE run_id=? ORDER BY ord")}
    issuers, disp = {}, {}
    for r in rows("SELECT * FROM run_issuer WHERE run_id=?"):
        disp[r["issuer"]] = r["name"] or r["issuer"]   # монитор показывает имя; ключ может быть кодом
        x = {"group": r["issuer_group"], "sector": r["sector"], "rating": r["rating"]}
        if r["is_gov"]:
            x["gov"] = True
        if r["is_quasi"]:
            x["quasi"] = True
        issuers[disp[r["issuer"]]] = x
    used_ratings = {x["rating"] for x in issuers.values() if x["rating"]}
    rating_order = [r["rating"] for r in con.execute("SELECT rating FROM ref_rating ORDER BY ord") if r["rating"] in used_ratings]

    risk, fxd = {}, {}
    for r in rows("SELECT position_id, curve_id, bucket, dv01 FROM position_risk WHERE run_id=?"):
        risk.setdefault(r[0], {}).setdefault(r[1], [0.0] * len(B))[bidx[r[2]]] = r[3]
    for r in rows("SELECT position_id, ccy, delta FROM position_fx WHERE run_id=?"):
        fxd.setdefault(r[0], {})[r[1]] = r[2]
    pnl = {r["position_id"]: r for r in rows("SELECT * FROM position_pnl WHERE run_id=?")}
    gamma = {}
    for r in rows("SELECT position_id, factor_1, factor_2, gamma FROM position_gamma WHERE run_id=? ORDER BY position_id, factor_1, factor_2"):
        gamma.setdefault(r[0], []).append([r[1], r[2], r[3]])

    quotes = bond_quotes(con, as_of)
    iid_by_isin = {r[0]: r[1] for r in con.execute("SELECT isin, instrument_id FROM ref_instrument WHERE isin IS NOT NULL")}

    positions = []
    for p in rows("SELECT * FROM position WHERE run_id=? ORDER BY rowid"):
        pid, iname = p["position_id"], disp.get(p["issuer"], p["issuer"]) if p["issuer"] else None
        iss = issuers.get(iname) if iname else None
        q = pnl.get(pid)
        comp = {k: (q[k] if q else 0.0) for k in PNL_KEYS}
        explained = sum(comp.values())
        official = q["official"] if q and q["official"] is not None else explained
        pos = {
            "id": pid, "book": p["book_id"], "assetClass": p["asset_class"], "segment": p["segment"], "instrType": p["instr_type"],
            "name": p["name"], "isin": p["isin"], "issuer": iname,
            "issuerGroup": iss["group"] if iss else None, "sector": iss["sector"] if iss else None, "rating": iss["rating"] if iss else None,
            "ccy": p["ccy"], "maturity": p["maturity"],
            "tenorY": p["tenor_y"] if p["tenor_y"] is not None else _years(as_of, p["maturity"]),
            "couponType": p["coupon_type"], "coupon": p["coupon"], "index": p["index_name"], "margin": p["margin"], "side": p["side"],
            "notional": p["notional"], "notionalRub": p["notional_rub"], "price": p["price"], "mv": p["mv"], "mvRub": p["mv_rub"],
            "ytm": p["ytm"], "spreadBp": p["spread_bp"], "mdur": p["mdur"], "sprDur": p["spr_dur"], "convexity": p["convexity"],
            "dv01": p["dv01"], "cs01": p["cs01"], "risk": risk.get(pid, {}), "fxDelta": fxd.get(pid, {}), "jtd": p["jtd"],
            "wal": p["wal"], "cpr": p["cpr"], "counterparty": p["counterparty"], "newToday": bool(p["new_today"]),
            "gamma": gamma.get(pid, []), **gamma_summary(gamma.get(pid), p["mv_rub"], p["convexity"], p["cs01"]),
            "pnl": {
                "carry": comp["carry"], "rolldown": comp["rolldown"], "rates": comp["rates"], "spread": comp["spread"],
                "fx": comp["fx_gross"] + comp["fx_funding"], "fxGross": comp["fx_gross"], "fxFunding": comp["fx_funding"],
                "convexity": comp["convexity"], "newTrades": comp["new_trades"], "other": comp["other"],
                "explained": explained, "unexplained": official - explained, "official": official,
                "breakNote": q["break_note"] if q else None,
            },
        }
        if p["instr_type"] == "BOND":
            iid = p["instrument_id"] or iid_by_isin.get(p["isin"]) or p["isin"]
            pos.update({"instrumentId": iid, "accrued": p["accrued"], "couponFreq": p["coupon_freq"], "yieldDate": p["yield_date"],
                        "spreadType": p["spread_type"], "spreadCurve": p["spread_curve"], "quote": _quote(quotes.get(iid))})
        if p["extra_json"]:
            pos["extra"] = json.loads(p["extra_json"])
        positions.append(pos)

    funding, fpnl, fbook = [], {"carry": 0.0, "fx": 0.0}, {b: {"carry": 0.0, "fx": 0.0, "fxAllocated": 0.0} for b in books}
    for f in rows("SELECT d.*, p.carry, p.fx_gross, p.fx_allocated FROM funding_deal d LEFT JOIN funding_pnl p"
                  " ON p.run_id=d.run_id AND p.deal_id=d.deal_id WHERE d.run_id=? ORDER BY d.rowid"):
        carry, gross, alloc = f["carry"] or 0.0, f["fx_gross"] or 0.0, f["fx_allocated"] or 0.0
        funding.append({
            "id": f["deal_id"], "book": f["book_id"], "type": f["type"], "ccy": f["ccy"], "amount": f["amount"],
            "amountRub": f["amount"] * fx.get(f["ccy"], 1.0), "rateExpr": f["rate_expr"], "rate": f["rate"],
            "start": f["start_date"], "maturity": f["maturity"], "tenorY": max(1 / 365, _years(as_of, f["maturity"]) or 0),
            "counterparty": f["counterparty"], "collateral": f["collateral"],
            "fxDelta": {f["ccy"]: f["amount"]} if f["ccy"] != "RUB" else {},
            "pnl": {"carry": carry, "fx": gross - alloc, "fxGross": gross, "fxAllocated": alloc},
        })
        fpnl["carry"] += carry
        fpnl["fx"] += gross - alloc
        b = fbook.setdefault(f["book_id"], {"carry": 0.0, "fx": 0.0, "fxAllocated": 0.0})
        b["carry"] += carry
        b["fx"] += gross - alloc
        b["fxAllocated"] += alloc

    scenarios = []
    for s in con.execute("SELECT * FROM scenario_def WHERE active=1 ORDER BY ord, scenario_id"):
        d = json.loads(s["definition_json"])
        scenarios.append(dict({"id": s["scenario_id"], "name": s["name"], "kind": s["kind"], "description": s["description"]}, **d))
    scen_res = {}
    for r in rows("SELECT * FROM scenario_result WHERE run_id=?"):
        scen_res.setdefault(r["scenario_id"], {"byPosition": {}, "fundingFxByBook": {}})["byPosition"][r["position_id"]] = {
            "rates": r["rates"], "spread": r["spread"], "fxGross": r["fx_gross"], "fxFunding": r["fx_funding"],
            "convexity": r["convexity"], "fxCross": r["fx_cross"], "total": r["total"]}
    for r in rows("SELECT * FROM scenario_funding_result WHERE run_id=?"):
        scen_res.setdefault(r["scenario_id"], {"byPosition": {}, "fundingFxByBook": {}})["fundingFxByBook"][r["book_id"]] = r["fx_residual"]

    from .writer import run_totals
    off, exp, dv, cs = run_totals(con, rid)
    hist = con.execute("SELECT * FROM desk_history WHERE as_of < ? ORDER BY as_of DESC LIMIT ?", (as_of, HISTORY_DAYS - 1)).fetchall()[::-1]
    history, cum = [], 0.0
    for h in [dict(as_of=h["as_of"], pnl_official=h["pnl_official"], pnl_explained=h["pnl_explained"], dv01=h["dv01"], cs01=h["cs01"]) for h in hist] + \
            [dict(as_of=as_of, pnl_official=off, pnl_explained=exp, dv01=dv, cs01=cs)]:
        cum += h["pnl_official"]
        history.append({"date": h["as_of"], "pnlOfficial": h["pnl_official"], "pnlExplained": h["pnl_explained"],
                        "pnlCum": cum, "dv01": h["dv01"], "cs01": h["cs01"]})

    return {
        "asOf": as_of, "reportingCcy": "RUB",
        "BUCKETS": B, "PILLARS": [b["pillar_years"] for b in buckets], "CURVES": curves, "CURVE_MOVES": moves,
        "FX": fx, "FX_MOVE_PCT": fx_move, "INDICES": indices, "ISSUERS": issuers, "RATING_ORDER": rating_order,
        "BOOKS": books, "SCENARIOS": scenarios, "scenarioResults": scen_res,
        "positions": positions, "funding": funding, "fundingPnl": fpnl, "fundingPnlByBook": fbook, "history": history,
        "source": {"kind": "db", "runId": rid, "runType": run["run_type"], "publishedAt": run["published_at"],
                   "libVersion": run["lib_version"], "marketSnapshot": run["market_snapshot"], "contractVersion": run["contract_version"]},
    }
