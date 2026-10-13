"""Эндпойнты слоя входных данных (только чтение): справочники, срезы, рынок, покрытие.
Описание — docs/SERVICE_DATA_MODEL.md, раздел «API»."""
import json
import re

from . import market as M
from .datasets import DATASETS

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_ROWS = 100000


def _d(q, k, required=False):
    v = q.get(k)
    if v is None or v == "":
        if required:
            raise ValueError(f"нужен параметр {k}=YYYY-MM-DD")
        return None
    if not _DATE.match(v):
        raise ValueError(f"{k}: ожидалась дата YYYY-MM-DD")
    return v


def _rows(con, sql, args=()):
    return [dict(r) for r in con.execute(sql + f" LIMIT {MAX_ROWS}", args)]


def _latest(con, table, col, q, key="as_of"):
    """Дата из запроса или последняя доступная."""
    d = _d(q, key)
    return d or con.execute(f"SELECT MAX({col}) FROM {table}").fetchone()[0]


def _series(con, table, keycol, keyval, valcols, q):
    f, t = _d(q, "from"), _d(q, "to")
    sql, args = f"SELECT date, {', '.join(valcols)} FROM {table} WHERE {keycol}=?", [keyval]
    if f:
        sql += " AND date >= ?"; args.append(f)
    if t:
        sql += " AND date <= ?"; args.append(t)
    return _rows(con, sql + " ORDER BY date", args)


def handle(con, route, q):
    """Вернуть объект ответа или None, если маршрут не отсюда. ValueError → 400."""
    if route == "books":
        return {"books": _rows(con, "SELECT * FROM ref_book ORDER BY ord, book_id")}
    if route == "issuers":
        d = _d(q, "as_of") or "9999-12-31"
        rt = M.ratings_on(con, d)
        out = _rows(con, "SELECT * FROM ref_issuer ORDER BY name")
        for x in out:
            x["rating"] = rt.get(x["issuer_id"])
        return {"as_of": None if d.startswith("9999") else d, "issuers": out}
    if route == "issuers/ratings":
        sql, args = "SELECT * FROM ref_issuer_rating", []
        if q.get("issuer_id"):
            sql += " WHERE issuer_id=?"; args.append(q["issuer_id"])
        return {"ratings": _rows(con, sql + " ORDER BY issuer_id, date", args)}
    if route == "counterparties":
        return {"counterparties": _rows(con, "SELECT * FROM ref_counterparty ORDER BY name")}
    if route == "curves":
        return {"curves": _rows(con, "SELECT * FROM ref_curve ORDER BY ord, curve_id")}
    if route == "instruments":
        sql, args, w = "SELECT * FROM ref_instrument", [], []
        if _d(q, "as_of"):
            w.append("maturity >= ?"); args.append(q["as_of"])
        if q.get("issuer_id"):
            w.append("issuer_id = ?"); args.append(q["issuer_id"])
        if q.get("q"):
            w.append("(name LIKE ? OR isin LIKE ? OR instrument_id LIKE ?)"); args += [f"%{q['q']}%"] * 3
        return {"instruments": _rows(con, sql + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY name", args)}
    if route.startswith("instruments/"):
        iid = route[len("instruments/"):]
        r = con.execute("SELECT * FROM ref_instrument WHERE instrument_id=?", (iid,)).fetchone()
        if not r:
            return {"_status": 404, "error": "not found"}
        cf = _rows(con, "SELECT date, type, rate, amount FROM ref_cashflow WHERE instrument_id=? ORDER BY date", (iid,))
        last = con.execute("SELECT * FROM md_bond_quote WHERE instrument_id=? ORDER BY date DESC LIMIT 1", (iid,)).fetchone()
        return {"instrument": dict(r), "cashflows": cf, "last_quote": dict(last) if last else None}
    if route == "otc-deals":
        sql, args, w = "SELECT * FROM ref_otc_deal", [], []
        if _d(q, "as_of"):
            w.append("maturity >= ? AND trade_date <= ?"); args += [q["as_of"], q["as_of"]]
        if q.get("book"):
            w.append("book_id=?"); args.append(q["book"])
        return {"deals": _rows(con, sql + (" WHERE " + " AND ".join(w) if w else "") + " ORDER BY deal_id", args)}

    # ---- срезы
    if route == "positions":
        d = _latest(con, "pos_snapshot", "as_of", q)
        sql = ("SELECT s.*, COALESCE(i.name, o.instr_type || ' ' || o.deal_id) AS name, COALESCE(i.ccy, o.ccy) AS ccy,"
               " CASE WHEN s.instrument_id IS NOT NULL THEN 'BOND' ELSE o.instr_type END AS instr_type,"
               " i.isin, i.issuer_id, i.asset_class, i.segment, COALESCE(i.maturity, o.maturity) AS maturity,"
               " o.side, o.counterparty_id"
               " FROM pos_snapshot s LEFT JOIN ref_instrument i ON i.instrument_id=s.instrument_id"
               " LEFT JOIN ref_otc_deal o ON o.deal_id=s.deal_id WHERE s.as_of=?")
        args = [d]
        if q.get("book"):
            sql += " AND s.book_id=?"; args.append(q["book"])
        rows = _rows(con, sql + " ORDER BY s.book_id, s.position_id", args)
        prev = con.execute("SELECT MAX(as_of) FROM pos_snapshot WHERE as_of < ?", (d,)).fetchone()[0] if d else None
        if prev:
            old = {r[0] for r in con.execute("SELECT position_id FROM pos_snapshot WHERE as_of=?", (prev,))}
            for r in rows:
                r["new_today"] = r["position_id"] not in old
        return {"as_of": d, "prev_as_of": prev, "positions": rows}
    if route == "funding":
        d = _latest(con, "fund_snapshot", "as_of", q)
        sql, args = "SELECT * FROM fund_snapshot WHERE as_of=?", [d]
        if q.get("book"):
            sql += " AND book_id=?"; args.append(q["book"])
        return {"as_of": d, "funding": _rows(con, sql + " ORDER BY book_id, maturity", args)}
    if route == "trades":
        d = _latest(con, "trade_log", "trade_date", q, key="date")
        return {"date": d, "trades": _rows(con, "SELECT * FROM trade_log WHERE trade_date=? ORDER BY trade_id", (d,))}
    if route == "pnl/official":
        f, t = _d(q, "from"), _d(q, "to")
        if f or t:
            # итог по книге: строка книги, а если её нет — сумма позиций
            args = []
            sql = ("SELECT as_of, book_id, COALESCE(MAX(CASE WHEN position_id='' THEN pnl_rub END), SUM(CASE WHEN position_id<>'' THEN pnl_rub END)) AS pnl_rub"
                   " FROM pnl_official WHERE 1=1")
            if f:
                sql += " AND as_of >= ?"; args.append(f)
            if t:
                sql += " AND as_of <= ?"; args.append(t)
            return {"pnl": _rows(con, sql + " GROUP BY as_of, book_id ORDER BY as_of, book_id", args)}
        d = _latest(con, "pnl_official", "as_of", q)
        return {"as_of": d, "pnl": _rows(con, "SELECT * FROM pnl_official WHERE as_of=? ORDER BY book_id, position_id", (d,))}

    # ---- рынок
    if route == "market/curves":
        if q.get("curve"):
            c = q["curve"]
            dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM md_curve_point WHERE curve_id=?"
                                               + (" AND date >= ?" if _d(q, "from") else "") + (" AND date <= ?" if _d(q, "to") else "")
                                               + " ORDER BY date", [c] + [x for x in (q.get("from"), q.get("to")) if x])]
            return {"curve_id": c, "buckets": [b for b, _ in M.BUCKETS],
                    "series": [{"date": d, "rates": M.curves_on(con, d).get(c)} for d in dates[-MAX_ROWS:]]}
        d = _latest(con, "md_curve_point", "date", q)
        prev = M.prev_date(con, d) if d else None
        cur, pc = M.curves_on(con, d) if d else {}, M.curves_on(con, prev) if prev else {}
        raw = _rows(con, "SELECT curve_id, tenor_years, rate, source FROM md_curve_point WHERE date=? ORDER BY curve_id, tenor_years", (d,))
        return {"as_of": d, "prev": prev, "pillars": M.PILLARS,
                "curves": {c: {"rates": v, "move_bp": [round((a - b) * 100, 4) for a, b in zip(v, pc.get(c, v))]} for c, v in cur.items()},
                "raw_points": raw}
    if route == "market/fx":
        if q.get("ccy"):
            return {"ccy": q["ccy"], "series": _series(con, "md_fx", "ccy", q["ccy"], ["rate", "source"], q)}
        d = _latest(con, "md_fx", "date", q)
        prev = M.prev_date(con, d, "md_fx") if d else None
        cur, pv = M.fx_on(con, d) if d else {}, M.fx_on(con, prev) if prev else {}
        return {"as_of": d, "prev": prev, "fx": {k: {"rate": v, "move_pct": (v / pv[k] - 1) * 100 if pv.get(k) else None} for k, v in cur.items()}}
    if route == "market/fixings":
        if q.get("index"):
            return {"index_name": q["index"], "series": _series(con, "md_fixing", "index_name", q["index"], ["value"], q)}
        d = _latest(con, "md_fixing", "date", q)
        return {"as_of": d, "fixings": M.fixings_on(con, d) if d else {}}
    if route == "market/bonds":
        if q.get("instrument_id"):
            return {"instrument_id": q["instrument_id"], "series": _series(con, "md_bond_quote", "instrument_id", q["instrument_id"],
                                                                            ["clean_price", "ytm", "z_spread_bp", "g_spread_bp", "accrued", "volume_rub", "source"], q)}
        d = _latest(con, "md_bond_quote", "date", q)
        return {"as_of": d, "quotes": _rows(con, "SELECT * FROM md_bond_quote WHERE date=? ORDER BY instrument_id", (d,))}
    if route == "market/issuer-spreads":
        d = _latest(con, "md_bond_quote", "date", q)
        prev = M.prev_date(con, d, "md_bond_quote") if d else None
        s, p = M.issuer_spreads(con, d) if d else {}, M.issuer_spreads(con, prev) if prev else {}
        return {"as_of": d, "prev": prev, "issuers": {k: {"spread_bp": v, "move_bp": v - p[k] if k in p else None} for k, v in s.items()},
                "rating_moves_bp": M.rating_spread_moves(con, prev, d) if prev else {}}
    if route == "market/cds":
        d = _latest(con, "md_cds_quote", "date", q)
        return {"as_of": d, "quotes": _rows(con, "SELECT * FROM md_cds_quote WHERE date=? ORDER BY issuer_id, tenor_years", (d,))}

    # ---- сценарии, покрытие, журнал
    if route == "scenarios":
        out = []
        for r in con.execute("SELECT * FROM scenario_def ORDER BY kind DESC, ord, scenario_id"):
            x = dict(r)
            x["definition"] = json.loads(x.pop("definition_json"))
            if q.get("kind") and x["kind"] != q["kind"]:
                continue
            out.append(x)
        return {"scenarios": out}
    if route == "coverage":
        return coverage(con, _d(q, "from"), _d(q, "to"))
    if route == "loads":
        return {"loads": _rows(con, "SELECT * FROM data_load ORDER BY load_id DESC LIMIT 200")}
    return None


COVER = [(d.name, d.table, d.date_col) for d in DATASETS if d.date_col and d.name != "issuer_ratings"]


def coverage(con, date_from=None, date_to=None):
    """По каждому рабочему дню — сколько строк в каждом датированном наборе и есть ли EOD-прогон."""
    dates = set(M.market_dates(con, date_from, date_to))
    for _, table, col in COVER:   # плюс даты, на которые есть хоть какие-то срезы
        sql, args = f"SELECT DISTINCT {col} FROM {table} WHERE 1=1", []
        if date_from:
            sql += f" AND {col} >= ?"; args.append(date_from)
        if date_to:
            sql += f" AND {col} <= ?"; args.append(date_to)
        dates |= {r[0] for r in con.execute(sql, args)}
    dates = sorted(dates)
    counts = {}
    for name, table, col in COVER:
        counts[name] = {r[0]: r[1] for r in con.execute(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")}
    runs = {r[0]: r[1] for r in con.execute("SELECT as_of, MAX(run_id) FROM calc_run WHERE status='complete' AND run_type='EOD' GROUP BY as_of")}
    days = [dict({"date": d, "eod_run": runs.get(d)}, **{n: counts[n].get(d, 0) for n, _, _ in COVER}) for d in dates]
    required = [d.name for d in DATASETS if d.priority == 1 and d.date_col and d.name != "issuer_ratings"]
    gaps = [x["date"] for x in days if any(x[n] == 0 for n in required)]
    return {"datasets": [n for n, _, _ in COVER], "required": required, "days": days, "days_with_gaps": gaps}
