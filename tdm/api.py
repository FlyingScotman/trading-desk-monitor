"""Эндпойнты слоя входных данных (только чтение): справочники, срезы, рынок, покрытие.
Описание — docs/SERVICE_DATA_MODEL.md, раздел «API»."""
import json
import re
from datetime import date

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
    if route == "funding-rules":   # 1.3: справочник ставок фондирования ('' в ключе = «любое значение»)
        out = _rows(con, "SELECT * FROM ref_funding_rate ORDER BY book_type, desk, book_id, entity, instrument_id, ccy")
        for x in out:
            x.update({k: None for k in ("book_type", "desk", "book_id", "entity", "instrument_id") if x[k] == ""})
        return {"rules": out}
    if route == "cbr-meetings":    # 1.3: заседания ЦБ; effective — дата действия новой КС
        f = _d(q, "from")
        return {"meetings": [m for m in M.cbr_meetings(con) if not f or m["date"] >= f]}
    if route == "carry-scenarios":
        out = []
        for r in con.execute("SELECT * FROM carry_scenario_def ORDER BY ord, scenario_id"):
            x = dict(r)
            x["definition"] = json.loads(x.pop("definition_json"))
            out.append(x)
        return {"scenarios": out}
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
    if route == "cash-balances":   # 1.3: остатки ДС книг деривативов
        d = _latest(con, "cash_snapshot", "as_of", q)
        sql, args = "SELECT * FROM cash_snapshot WHERE as_of=?", [d]
        if q.get("book"):
            sql += " AND book_id=?"; args.append(q["book"])
        return {"as_of": d, "balances": _rows(con, sql + " ORDER BY book_id, ccy", args)}
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
    if route == "market/fx-cbr":   # 1.3: курсы ЦБ по дате действия
        if q.get("ccy"):
            return {"ccy": q["ccy"], "series": _series(con, "md_fx_cbr", "ccy", q["ccy"], ["rate", "set_date"], q)}
        d = _latest(con, "md_fx_cbr", "date", q)
        if not d:
            return {"as_of": None, "prev": None, "fx": {}}
        prev = M.prev_business_day(con, d)
        cur, nxt, mv = M.fx_cbr_on(con, d), M.fx_cbr_next(con, d), M.fx_cbr_move(con, d, prev)
        return {"as_of": d, "prev": prev, "fx": {k: {"rate": v, "move_pct": mv.get(k), "next": nxt.get(k)} for k, v in cur.items()}}
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
    if route == "bonds/history":
        return bond_history(con, q)
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


def _years(d0, d1):
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25


def bond_history(con, q):
    """История выпуска: котировки md_bond_quote + ставка кривой `curve` на сроке доходности в каждую дату
    + спред к ней + номинал позиции по снэпшотам. Срок: до оферты (ref_instrument.offer_date, пока она впереди),
    иначе до погашения; для ИЦБ — wal (текущий WAL на дату `to`) + прошедшее время."""
    iid, isin = q.get("instrument_id"), q.get("isin")
    inst = None
    if iid:
        inst = con.execute("SELECT * FROM ref_instrument WHERE instrument_id=?", (iid,)).fetchone()
    elif isin:
        inst = con.execute("SELECT * FROM ref_instrument WHERE isin=?", (isin,)).fetchone()
        iid = inst["instrument_id"] if inst else isin
    if not iid:
        raise ValueError("нужен instrument_id или isin")
    f, t = _d(q, "from"), _d(q, "to")
    sql, args = "SELECT * FROM md_bond_quote WHERE instrument_id=?", [iid]
    if f:
        sql += " AND date >= ?"; args.append(f)
    if t:
        sql += " AND date <= ?"; args.append(t)
    quotes = _rows(con, sql + " ORDER BY date", args)
    curve = q.get("curve") or None
    try:
        wal = float(q["wal"]) if q.get("wal") else None
    except ValueError:
        raise ValueError("wal: ожидалось число лет")
    ccy = inst["ccy"] if inst else None
    c_ccy = None
    if curve:
        r = con.execute("SELECT ccy FROM ref_curve WHERE curve_id=?", (curve,)).fetchone() or \
            con.execute("SELECT ccy FROM run_curve WHERE curve_id=? ORDER BY run_id DESC LIMIT 1", (curve,)).fetchone()
        c_ccy = r[0] if r else None
    is_float = bool(inst and inst["coupon_type"] == "float")
    note = None
    if not inst:
        note = "выпуска нет в ref_instrument: срок доходности неизвестен"
    elif is_float:
        note = "флоатер: спред к индексу (DM), не к кривой"
    elif curve and c_ccy and ccy and c_ccy != ccy:
        note = "валюта кривой не совпадает с валютой выпуска"
    elif curve and not c_ccy:
        note = f"кривая {curve} не найдена"
    use_curve = bool(curve and inst and not note)
    pts = {}
    if use_curve and quotes:
        for d, ten, rate in con.execute("SELECT date, tenor_years, rate FROM md_curve_point WHERE curve_id=? AND date BETWEEN ? AND ?"
                                        " ORDER BY date, tenor_years", (curve, quotes[0]["date"], quotes[-1]["date"])):
            pts.setdefault(d, ([], []))
            pts[d][0].append(ten)
            pts[d][1].append(rate)
    face = {r[0]: r[1] for r in con.execute("SELECT as_of, SUM(COALESCE(face_amount, quantity)) FROM pos_snapshot WHERE instrument_id=? GROUP BY as_of", (iid,))}
    if quotes:   # снэпшот на дату есть, а выпуска в нём нет — позиция 0; снэпшота нет — неизвестно (null)
        snap_days = {r[0] for r in con.execute("SELECT DISTINCT as_of FROM pos_snapshot WHERE as_of BETWEEN ? AND ?", (quotes[0]["date"], quotes[-1]["date"]))}
        face.update({d: 0 for d in snap_days if d not in face})
    end = t or (quotes[-1]["date"] if quotes else None)
    series = []
    for x in quotes:
        d = x["date"]
        if wal is not None and end:
            ten = wal + _years(d, end)
        elif inst:
            target = inst["offer_date"] if inst["offer_date"] and inst["offer_date"] > d else inst["maturity"]
            ten = _years(d, target)
        else:
            ten = None
        cr = M.interp(pts[d][0], pts[d][1], ten) if use_curve and ten is not None and d in pts else None
        row = {k: x[k] for k in ("date", "clean_price", "ytm", "z_spread_bp", "g_spread_bp", "accrued", "volume_rub", "source")}
        row.update(face_amount=face.get(d), tenor_y=None if ten is None else round(ten, 4), curve_rate=None if cr is None else round(cr, 4),
                   spread_to_curve_bp=None if cr is None or x["ytm"] is None else round((x["ytm"] - cr) * 100, 2))
        series.append(row)
    return {"instrument_id": iid, "isin": inst["isin"] if inst else isin, "name": inst["name"] if inst else None, "ccy": ccy,
            "curve": curve, "spread_to_curve_note": note, "series": series}


COVER = [(d.name, d.table, d.date_col) for d in DATASETS if d.date_col and d.name != "issuer_ratings"]
NO_DAYS = {"md_fx_cbr"}   # ряд по дате действия (есть и выходные): дни покрытия не добавляет


def coverage(con, date_from=None, date_to=None):
    """По каждому рабочему дню — сколько строк в каждом датированном наборе и есть ли EOD-прогон."""
    dates = set(M.market_dates(con, date_from, date_to))
    for name, table, col in COVER:   # плюс даты, на которые есть хоть какие-то срезы
        if name in NO_DAYS:
            continue
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
