"""Чтение рыночных рядов и справочников из слоя входных данных (md_*, ref_*)."""
import statistics
from datetime import date, timedelta

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


# ---------------------------------------------------------------------- календарь (контракт 1.3)
def _shift(con, d, step):
    """Соседний рабочий день: по ref_calendar, если он покрывает дату, иначе пн–пт."""
    op, agg = ("<", "MAX") if step < 0 else (">", "MIN")
    covered = con.execute("SELECT 1 FROM ref_calendar WHERE date " + (">=" if step < 0 else "<=") + " ? LIMIT 1", (d,)).fetchone()
    if covered:
        r = con.execute(f"SELECT {agg}(date) FROM ref_calendar WHERE is_business_day=1 AND date {op} ?", (d,)).fetchone()[0]
        if r:
            return r
    x = date.fromisoformat(d) + timedelta(days=step)
    while x.weekday() >= 5:
        x += timedelta(days=step)
    return x.isoformat()


def prev_business_day(con, d):
    return _shift(con, d, -1)


def next_business_day(con, d):
    return _shift(con, d, 1)


def calendar_days(d0, d1):
    """Календарные дни периода начисления (d0, d1]: в понедельник — сб, вс, пн."""
    a, b = date.fromisoformat(d0), date.fromisoformat(d1)
    return [(a + timedelta(days=i)).isoformat() for i in range(1, (b - a).days + 1)]


# ---------------------------------------------------------------------- курсы ЦБ и фиксинги по дням (контракт 1.3)
def fx_cbr_on(con, d):
    """{ccy: курс ЦБ, действующий на d}: последняя запись md_fx_cbr с датой действия не позже d."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT f.ccy, f.rate FROM md_fx_cbr f JOIN (SELECT ccy, MAX(date) AS d FROM md_fx_cbr WHERE date <= ? GROUP BY ccy) m"
        " ON m.ccy=f.ccy AND m.d=f.date", (d,))}


def fx_cbr_next(con, d):
    """{ccy: курс ЦБ на следующий день}: первая запись с датой действия после d (установлен в d)."""
    return {r[0]: r[1] for r in con.execute(
        "SELECT f.ccy, f.rate FROM md_fx_cbr f JOIN (SELECT ccy, MIN(date) AS d FROM md_fx_cbr WHERE date > ? GROUP BY ccy) m"
        " ON m.ccy=f.ccy AND m.d=f.date", (d,))}


def fx_cbr_move(con, d, prev):
    """{ccy: изменение курса ЦБ, %}: действующий на d к действующему на prev (прошлый рабочий день)."""
    cur, old = fx_cbr_on(con, d), fx_cbr_on(con, prev) if prev else {}
    return {k: (v / old[k] - 1) * 100 if old.get(k) else 0.0 for k, v in cur.items()}


def fixing_on(con, index_name, d):
    """Значение индекса, действующее в день d (последняя запись не позже d; КС — по дате вступления в силу)."""
    r = con.execute("SELECT value FROM md_fixing WHERE index_name=? AND date <= ? ORDER BY date DESC LIMIT 1", (index_name, d)).fetchone()
    return r[0] if r else None


def fixings_by_day(con, index_name, d0, d1):
    """[(день, значение)] по календарным дням (d0, d1]: в каждый день — действующее значение (None, если записей нет)."""
    days = calendar_days(d0, d1)
    if not days:
        return []
    rows = con.execute("SELECT date, value FROM md_fixing WHERE index_name=? AND date <= ? ORDER BY date", (index_name, d1)).fetchall()
    out, i, cur = [], 0, None
    for day in days:
        while i < len(rows) and rows[i][0] <= day:
            cur = rows[i][1]
            i += 1
        out.append((day, cur))
    return out


def cbr_meetings(con, after=None):
    """[{date, effective}] — заседания ЦБ; effective по умолчанию — следующий рабочий день после решения."""
    sql, args = "SELECT date, effective_date FROM ref_cbr_meeting", []
    out = []
    for d, eff in con.execute(sql + " ORDER BY date", args):
        eff = eff or next_business_day(con, d)
        if after is None or eff > after:
            out.append({"date": d, "effective": eff})
    return out


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
