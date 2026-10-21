"""Data of the concept pages (``prototypes/concepts/``) beyond ``window.DESK``: ``/concepts-data.js`` sets
``window.DESK_EXTRAS`` for the run the page shows, ``/api/v1/history/positions`` gives the positions' series for the
charts (``lib/real.js`` builds the pages' data from both and ``window.DESK``).

``extras``: the desk's series over the default runs up to the run (risk by curve, CS01, the OFZ basis, DV01 by book,
market value by instrument type and currency, PnL by factor), per position of the run its PnL month and year to date
over those runs, the previous run's price and spread and what changed in its holding (a trade, a new position), its
flows of the next 18 months (``extra.cashflows`` of runs made from ``VALUATION_VERSION`` 6), and the market of the
latest cached close not after the run: every quoted bond (``universeRub``, ``universeFx``) and the day's movers. What
the store does not have is null with a note in ``notes``: ratings, sectors and outstanding amounts of the market's
bonds (the hub does not publish them yet), CBR meetings, months before the first default run.

``positions_history``: per position of the run, aligned on one date axis: MOEX's clean price, yield (none for a
floater), G- and Z-spread and a linker's ICPI / IR / BEI from ``md_bond_day`` (the hub's history API, from
2024-01-03; else the snapshots, ``history_days`` deep), the KBD at the bond's term (a RUB bond with known coupons),
the face and DV01 of the default runs, carried forward between runs (null before the first), and a floater's
discount margin on the days of a run.

The classification (``kind_of``) and the curve ids (``CURVE_MAP``) are the pages' own and ``lib/real.js`` mirrors
them: a change here is a change there."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from ..marketdata.history import RUB_SETTLEMENT, curve_rate, kbd, latest_market_day
from ..store import DEFAULT_RUNS

HIST_RUNS = 260
"""At most this many default runs make the desk series (about a year of business days)."""
HISTORY_DAYS = 365
"""Default depth of ``positions_history`` (``from`` asks for more: the card of a position asks for its whole history)."""
FX_CCYS = ("USD", "EUR", "CNY", "CHF", "GBP", "HKD", "AED", "INR", "KZT", "BYN", "TRY", "JPY")

CURVE_MAP = {"RUB OFZ": "RUB RUONIA", "USD": "USD SOFR", "EUR": "EUR ESTR", "CNY": "CNY XCCY"}
"""A run's risk curve -> the concept pages' curve. ``RUB OFZ`` (the KBD) carries a bond's rates risk only on a date
without RUONIA OIS (``riskNotes``), so it is added to RUONIA there; the OFZ basis of the pages is ``ofz_spr01``.
A currency's curve without points gets the name of the currency's curve the pages know."""

PNL = {"carry": ("carry", "rolldown"), "rates": ("rates", "convexity"), "spread": ("spread",),
       "fx": ("fx_gross", "fx_funding"), "trading": ("new_trades",), "other": ("funding", "other")}
"""The pages' PnL factors from the run's components (``compat.PNL_KEYS``)."""

GOV_CLASSES = {"ОФЗ-ПД": "PD", "ОФЗ-АД": "PD", "ОФЗ-ПК": "PK", "ОФЗ-ИН": "IN"}


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


def _pnl(q: dict[str, Any] | None) -> dict[str, float] | None:
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


def _runs(con, run: dict[str, Any]) -> list[dict[str, Any]]:
    """The default runs up to the run's date (the run itself last, also when it is an intraday run), oldest first."""
    rows = con.execute(f"SELECT run_id, business_date FROM run WHERE run_id IN ({DEFAULT_RUNS})"
                       " AND business_date < %s ORDER BY business_date DESC LIMIT %s",
                       (run["business_date"], HIST_RUNS - 1)).fetchall()[::-1]
    return rows + [{"run_id": run["run_id"], "business_date": run["business_date"]}]


RISK_SQL = ("SELECT position_id, book_id, quantity, pv_rub, fact->'risk' AS risk, (fact->>'cs01')::float8 AS cs01,"
            " (fact->>'ofz_spr01')::float8 AS ofz, fact->>'instr_type' AS instr_type, fact->>'segment' AS segment,"
            " fact->>'asset_class' AS asset_class, fact->>'coupon_type' AS coupon_type, fact->>'ccy' AS ccy,"
            " (fact->>'notional')::float8 AS notional, (fact->>'notional_rub')::float8 AS notional_rub,"
            " (fact->>'price')::float8 AS price, (fact->>'spread_bp')::float8 AS spread_bp,"
            " (fact->>'dv01')::float8 AS dv01, pnl FROM run_position WHERE run_id = %s AND fact IS NOT NULL")


def extras(con, run: dict[str, Any]) -> dict[str, Any]:
    runs = _runs(con, run)
    dates = [r["business_date"].isoformat() for r in runs]
    n = len(runs)
    dv01: dict[str, list[float]] = {}
    book: dict[str, list[float]] = {}
    mv_type: dict[str, list[float]] = {}
    mv_ccy: dict[str, list[float]] = {}
    cs01, basis = [0.0] * n, [0.0] * n
    pnl = {k: [None] * n for k in (*PNL, "total")}
    pos_pnl: dict[str, dict[str, float]] = {}
    prev_rows: dict[str, dict[str, Any]] = {}
    cur_rows: dict[str, dict[str, Any]] = {}
    as_of = run["business_date"]

    def add(series: dict[str, list[float]], key: str, i: int, v: float) -> None:
        series.setdefault(key, [0.0] * n)[i] += v

    for i, r in enumerate(runs):
        rows = con.execute(RISK_SQL, (r["run_id"],)).fetchall()
        for x in rows:
            f = dict(x)
            for curve, bk in (x["risk"] or {}).items():
                add(dv01, CURVE_MAP.get(curve, curve), i, sum(v or 0.0 for v in bk.values()))
            cs01[i] += x["cs01"] or 0.0
            basis[i] += x["ofz"] or 0.0
            add(book, x["book_id"], i, x["dv01"] or 0.0)
            g, k = kind_of(f)
            add(mv_type, f"{g}.{k}", i, _mv(f, x["pv_rub"]))
            if x["instr_type"] == "BOND":
                add(mv_ccy, x["ccy"] or "RUB", i, x["pv_rub"] or 0.0)
            p = _pnl(x["pnl"])
            if p:
                for kk, v in p.items():
                    pnl[kk][i] = (pnl[kk][i] or 0.0) + v
                d = r["business_date"]
                acc = pos_pnl.setdefault(x["position_id"], {"mtd": 0.0, "ytd": 0.0})
                if d.year == as_of.year:
                    acc["ytd"] += p["total"]
                    if d.month == as_of.month:
                        acc["mtd"] += p["total"]
            if i == n - 2:
                prev_rows[x["position_id"]] = f
            if i == n - 1:
                cur_rows[x["position_id"]] = f
    first = runs[0]["business_date"]
    positions: dict[str, dict[str, Any]] = {}
    for pid, f in cur_rows.items():
        p0 = prev_rows.get(pid)
        out: dict[str, Any] = {}
        if pid in pos_pnl:
            out["pnlMtd"], out["pnlYtd"] = pos_pnl[pid]["mtd"], pos_pnl[pid]["ytd"]
        if p0 is not None:
            out["prevPrice"] = p0["price"]
            if f["spread_bp"] is not None and p0["spread_bp"] is not None:
                out["spreadChgBp"] = f["spread_bp"] - p0["spread_bp"]
            dq = (f["notional"] or 0.0) - (p0["notional"] or 0.0)
            if abs(dq) > 1e-6 * max(1.0, abs(f["notional"] or 0.0)):
                out["trade"] = {"qty": dq, "price": f["price"]}
        elif n > 1:
            out["trade"] = {"qty": f["notional"], "price": f["price"], "new": True}
        positions[pid] = out
    notes = []
    if first.replace(day=1) > as_of.replace(day=1, month=1):
        notes.append(f"история прогонов с {first.isoformat()}: PnL с начала года — с этой даты")
    flows = []
    for x in con.execute("SELECT position_id, fact->>'ccy' AS ccy, fact->'extra'->'cashflows' AS cf FROM run_position"
                         " WHERE run_id = %s AND fact IS NOT NULL", (run["run_id"],)):
        for d, kind, amount, est in x["cf"] or []:
            flows.append({"date": d, "pos": x["position_id"], "kind": kind, "ccy": x["ccy"] or "RUB",
                          "amount": amount, "est": bool(est)})
    if not flows and cur_rows:
        notes.append("потоков позиций в прогоне нет: прогон посчитан до версии с потоками (пересчитается последняя дата)")
    flows.sort(key=lambda c: c["date"])
    market = universe(con, as_of, {f.get("instrument_id") for f in _facts(con, run["run_id"])})
    notes += market.pop("notes")
    return {"runId": run["run_id"], "asOf": as_of.isoformat(), "histDates": dates,
            "hist": {"dv01": dv01, "cs01": cs01, "basis01": basis, "dv01Book": book, "mvType": mv_type,
                     "mvCcy": mv_ccy, "pnl": pnl},
            "positions": positions, "cashflows": flows, **market,
            "cbr": {"next": None, "meetings": []}, "curveMap": CURVE_MAP, "notes": notes}


def _facts(con, run_id: int) -> list[dict[str, Any]]:
    return [r["f"] for r in con.execute("SELECT jsonb_build_object('instrument_id', fact->>'instrument_id') AS f"
                                        " FROM run_position WHERE run_id = %s AND fact IS NOT NULL", (run_id,))]


MARKET_SQL = ("SELECT b.*, i.isin, i.name, i.short_name, i.bond_class, i.coupon_type, i.face_unit, i.maturity_date,"
              " i.is_perpetual FROM md_bond_day b LEFT JOIN md_instrument i USING (secid) WHERE b.business_date = %s")
"""A day's market: ``md_bond_day``, the hub's history API rows, else the snapshots' (R24)."""


def universe(con, as_of: date, held: set[str | None]) -> dict[str, Any]:
    """The market of the latest cached close not after ``as_of``: every quoted RUB bond (``universeRub``), every quoted
    bond with a foreign face (``universeFx``) and the 60 RUB bonds with the most turnover with their yield change since
    the close before (``marketMovers``). Ratings, sectors, outstanding amounts and coupons are not in the store."""
    d = latest_market_day(con, as_of)
    if d is None:
        return {"marketDate": None, "universeRub": [], "universeFx": [], "marketMovers": [],
                "notes": ["рыночной истории (md_bond_eod) нет: карта рынка и календарь рынка пусты"]}
    prev = latest_market_day(con, d - timedelta(days=1))
    old = {r["secid"]: r["yield_pct"] for r in con.execute(
        "SELECT secid, yield_pct FROM md_bond_day WHERE business_date = %s", (prev,))} if prev else {}
    rub, fx = [], []
    for r in con.execute(MARKET_SQL, (d,)).fetchall():
        mat, yd = r["maturity_date"], r["yield_date"]
        ev = yd if yd and (mat is None or yd < mat) else mat
        tenor = (ev - d).days / 365.25 if ev and ev > d else None
        flt = r["coupon_type"] == "floating"
        y = r["yield_pct"]
        chg = (y - old[r["secid"]]) * 100 if y is not None and old.get(r["secid"]) is not None else None
        b = {"id": r["secid"], "isin": r["isin"], "name": r["short_name"] or r["name"] or r["secid"],
             "maturity": mat.isoformat() if mat else None,
             "put": yd.isoformat() if yd and mat and yd < mat else None, "tenor": tenor,
             "ytm": None if flt else y, "ytmMoex": y, "dur": (r["duration_days"] or 0) / 365.25 or None,
             "spread": r["gspread_bp"], "zspread": r["zspread_bp"], "chgBp": chg, "value": r["value"],
             "flt": flt, "held": r["secid"] in held, "rating": None, "sector": None, "size": None, "cpn": None,
             "freq": None, **{k: r[k] for k in ("icpi", "ir", "bei") if r[k] is not None}}
        unit = r["face_unit"] or ("RUB" if r["currency_id"] in RUB_SETTLEMENT else None)
        if unit == "RUB" or unit is None:
            b["isOfz"] = (r["bond_class"] or "").startswith("ofz")
            rub.append(b)
        else:
            b.update({"ccy": unit, "origCcy": unit, "issuer": None, "issuerName": b["name"].split(" ")[0],
                      "outstanding": None, "coupons": []})
            fx.append(b)
    movers = sorted((b for b in rub if b["chgBp"] is not None and b["value"]), key=lambda b: -b["value"])[:60]
    return {"marketDate": d.isoformat(), "universeRub": rub, "universeFx": fx,
            "marketMovers": [{"name": b["name"], "rating": None, "sector": None, "ytm": b["ytmMoex"],
                              "chgBp": b["chgBp"], "vol": b["value"]} for b in movers],
            "notes": [(f"рынок на {d.isoformat()} (последнее закрытие в истории): у выпусков нет рейтинга, сектора, "
                       "объёма в обращении и купона — их нет в снимках хаба")]}


def positions_history(con, run: dict[str, Any], ids: list[str] | None = None, from_: date | None = None
                      ) -> dict[str, Any]:
    from_ = from_ or run["business_date"] - timedelta(days=HISTORY_DAYS)
    cur = con.execute("SELECT position_id, secid, fact FROM run_position WHERE run_id = %s AND fact IS NOT NULL"
                      + (" AND position_id = ANY(%s)" if ids else ""),
                      (run["run_id"], ids) if ids else (run["run_id"],)).fetchall()
    pos = {r["position_id"]: r for r in cur}
    if not pos:
        return {"runId": run["run_id"], "dates": [], "series": {}}
    runs = [r for r in _runs(con, run) if r["business_date"] >= from_]
    by_run: dict[str, dict[date, tuple[float | None, float | None]]] = {}
    for r in runs:
        for x in con.execute("SELECT position_id, (fact->>'notional')::float8 AS n, (fact->>'dv01')::float8 AS dv,"
                             " (fact->>'spread_bp')::float8 AS spr FROM run_position WHERE run_id = %s"
                             " AND fact IS NOT NULL AND position_id = ANY(%s)", (r["run_id"], list(pos))):
            by_run.setdefault(x["position_id"], {})[r["business_date"]] = (x["n"], x["dv"], x["spr"])
    secid = {pid: (r["fact"].get("instrument_id") or r["secid"]) for pid, r in pos.items()
             if r["fact"].get("instr_type") == "BOND"}
    quotes: dict[str, dict[date, dict[str, Any]]] = {}
    for q in con.execute("SELECT business_date, secid, clean_pct, yield_pct, gspread_bp, zspread_bp, yield_date,"
                         " icpi, ir, bei FROM md_bond_day"
                         " WHERE secid = ANY(%s) AND business_date >= %s AND business_date <= %s",
                         (list(set(secid.values())), from_, run["business_date"])):
        quotes.setdefault(q["secid"], {})[q["business_date"]] = q
    dates = sorted({d for s in quotes.values() for d in s} | {r["business_date"] for r in runs})
    curves = {r["business_date"]: kbd(r["trade_date"], r["params"]) for r in con.execute(
        "SELECT business_date, trade_date, params FROM md_kbd_day WHERE business_date = ANY(%s)", (dates,))}
    series = {}
    for pid, r in pos.items():
        f = r["fact"]
        flt = f.get("coupon_type") == "float"
        rub_fix = f.get("ccy") in (None, "RUB") and f.get("coupon_type") == "fix"
        mat = date.fromisoformat(f["maturity"]) if f.get("maturity") else None
        q = quotes.get(secid.get(pid) or "", {})
        held = by_run.get(pid, {})
        s = {k: [] for k in ("price", "ytm", "spr", "notional", "dv01", "bench", *OPTIONAL)}
        last: tuple[float | None, float | None, float | None] | None = None
        for d in dates:
            x = q.get(d)
            if d in held:
                last = held[d]
            s["zspr"].append(x["zspread_bp"] if x else None)
            s["dm"].append(held[d][2] if flt and d in held else None)
            for k in ("icpi", "ir", "bei"):
                s[k].append(x[k] if x else None)
            s["price"].append(x["clean_pct"] if x else (f.get("price") if d == run["business_date"] else None))
            s["ytm"].append(None if flt or not x else x["yield_pct"])
            s["spr"].append(x["gspread_bp"] if x else None)
            s["notional"].append(last[0] if last else None)
            s["dv01"].append(last[1] if last else None)
            yd = x["yield_date"] if x and x["yield_date"] else mat
            t = (yd - d).days / 365.25 if yd and yd > d else None
            s["bench"].append(curve_rate(curves.get(d), t) if rub_fix else None)
        for k in OPTIONAL:  # only when the bond has them: most series would be nulls
            if all(v is None for v in s[k]):
                del s[k]
        series[pid] = s
    return {"runId": run["run_id"], "dates": [d.isoformat() for d in dates], "series": series,
            "note": ("цена, доходность, G- и Z-спред (к КБД), у линкеров ICPI, IR, BEI — MOEX (история закрытий хаба);"
                     " номинал, DV01 и дисконт-маржа флоатера — по прогонам деска")}


OPTIONAL = ("zspr", "dm", "icpi", "ir", "bei")
"""Series of ``positions_history`` given only when the position has a value: MOEX's Z-spread to the KBD, the desk's
discount margin of a floater (its run spread), MOEX's linker analytics (contract 1.2)."""


def script(data: dict[str, Any]) -> str:
    return "window.DESK_EXTRAS = " + json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str) + ";\n"
