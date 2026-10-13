"""Чтение рыночных рядов и справочников из слоя входных данных (md_*, ref_*)."""
import statistics

from .db import BUCKETS

PILLARS = [y for _, y in BUCKETS]


def interp(tenors, rates, t):
    """Линейная интерполяция по сроку, плоская экстраполяция на концах."""
    if t <= tenors[0]:
        return rates[0]
    for i in range(1, len(tenors)):
        if t <= tenors[i]:
            w = (t - tenors[i - 1]) / (tenors[i] - tenors[i - 1])
            return rates[i - 1] * (1 - w) + rates[i] * w
    return rates[-1]


def market_dates(con, date_from=None, date_to=None):
    """Рабочие дни: из ref_calendar, иначе — даты, на которые есть кривые или курсы."""
    where, args = [], []
    if date_from:
        where.append("date >= ?"); args.append(date_from)
    if date_to:
        where.append("date <= ?"); args.append(date_to)
    w = (" AND " + " AND ".join(where)) if where else ""
    cal = [r[0] for r in con.execute(f"SELECT date FROM ref_calendar WHERE is_business_day=1{w} ORDER BY date", args)]
    if cal:
        return cal
    w = (" WHERE " + " AND ".join(where)) if where else ""
    return [r[0] for r in con.execute(f"SELECT date FROM md_curve_point{w} UNION SELECT date FROM md_fx{w} ORDER BY 1", args + args)]


def prev_date(con, d, table="md_curve_point"):
    r = con.execute(f"SELECT MAX(date) FROM {table} WHERE date < ?", (d,)).fetchone()
    return r[0]


def curves_on(con, d):
    """{curve_id: [11 ставок на пилларах]} на дату (только кривые с точками на эту дату)."""
    raw = {}
    for r in con.execute("SELECT curve_id, tenor_years, rate FROM md_curve_point WHERE date=? ORDER BY curve_id, tenor_years", (d,)):
        raw.setdefault(r[0], ([], []))
        raw[r[0]][0].append(r[1])
        raw[r[0]][1].append(r[2])
    return {c: [interp(t, v, p) for p in PILLARS] for c, (t, v) in raw.items()}


def fx_on(con, d):
    return {r[0]: r[1] for r in con.execute("SELECT ccy, rate FROM md_fx WHERE date=?", (d,))}


def fixings_on(con, d):
    """Последнее известное значение каждого индекса на дату (фиксинги бывают не каждый день)."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT f.index_name, f.value FROM md_fixing f JOIN (SELECT index_name, MAX(date) AS d FROM md_fixing WHERE date <= ? GROUP BY index_name) m"
        " ON m.index_name=f.index_name AND m.d=f.date", (d,))}


def ratings_on(con, d, agency="COMPOSITE"):
    """{issuer_id: rating} на дату: последняя запись не позже d; запись агентства `agency` важнее остальных."""
    best = {}
    for iss, ag, dt, rt in con.execute("SELECT issuer_id, agency, date, rating FROM ref_issuer_rating WHERE date <= ?", (d,)):
        key = (ag == agency, dt)
        if iss not in best or key >= best[iss][0]:
            best[iss] = (key, rt)
    return {k: v[1] for k, v in best.items()}


def issuer_spreads(con, d):
    """{issuer_id: средний спред бумаг эмитента, б.п.} (Z-спред, иначе G-спред)."""
    acc = {}
    for r in con.execute("SELECT i.issuer_id, COALESCE(q.z_spread_bp, q.g_spread_bp) FROM md_bond_quote q"
                         " JOIN ref_instrument i ON i.instrument_id=q.instrument_id WHERE q.date=?"
                         " AND COALESCE(q.z_spread_bp, q.g_spread_bp) IS NOT NULL", (d,)):
        acc.setdefault(r[0], []).append(r[1])
    return {k: sum(v) / len(v) for k, v in acc.items()}


def rating_spread_moves(con, d0, d1):
    """Изменение спредов d0→d1 по рейтинговым корзинам (медиана по эмитентам, без суверена), б.п."""
    s0, s1 = issuer_spreads(con, d0), issuer_spreads(con, d1)
    gov = {r[0] for r in con.execute("SELECT issuer_id FROM ref_issuer WHERE is_gov=1")}
    rt = ratings_on(con, d1)
    by = {}
    for iss in set(s0) & set(s1):
        if iss in gov or iss not in rt:
            continue
        by.setdefault(rt[iss], []).append(s1[iss] - s0[iss])
    return {k: round(statistics.median(v), 1) for k, v in by.items() if v}
