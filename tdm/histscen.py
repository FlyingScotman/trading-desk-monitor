"""Исторические сценарии из рыночных рядов (уровень 1 сценарной логики, docs/SCENARIOS.md).

Для окна длиной h рабочих дней, заканчивающегося датой t, шок =
  кривые:  ставка(t) − ставка(t−h) на каждом из 11 пилларов, б.п.;
  курсы:   курс(t) / курс(t−h) − 1, %;
  спреды:  медиана изменения спреда эмитентов каждой рейтинговой корзины, б.п. (без суверена).
Окна ранжируются по прокси-PnL текущего портфеля (чувствительности последнего опубликованного
прогона) до 2-го порядка: Σ key-rate DV01 × шок + Σ FX-дельта × курс × % + Σ CS01 по рейтингу × спред-шок
+ квадратичная форма вторых производных (гамма, выпуклость, ставки×спред, кросс FX) — как в мониторе.
Отбираются top худших непересекающихся окон; они пишутся в scenario_def (kind='historical').

    python -m tdm build-hist-scenarios --horizon 1 10 --top 5
"""
import json

from . import market as M
from .db import BUCKETS, init_db, now_iso

B = [b for b, _ in BUCKETS]


def _quadratic(con, rid):
    """Второй порядок портфеля как квадратичная форма по агрегированным факторам:
    {(fa, fb): коэффициент} с PnL2 = Σ коэф × Δa × Δb (½ для диагонали уже учтена).
    Факторы: 'IR|кривая|бакет', 'CS|рейтинг' (свой спред позиции → корзина рейтинга, суверен исключён), 'FX|валюта'.
    Позиции без матрицы гамм — из выпуклости (ранг 1 по весам key-rate DV01) и кросс FX из DV01/CS01 (как в мониторе)."""
    curve_ccy = {r[0]: r[1] for r in con.execute("SELECT curve_id, ccy FROM run_curve WHERE run_id=?", (rid,))}
    pos = {r["position_id"]: r for r in con.execute(
        "SELECT p.position_id, p.ccy, p.mv_rub, p.convexity, p.dv01, p.cs01, i.rating, COALESCE(i.is_gov, 0) AS is_gov FROM position p"
        " LEFT JOIN run_issuer i ON i.run_id=p.run_id AND i.issuer=p.issuer WHERE p.run_id=?", (rid,))}
    risk = {}
    for r in con.execute("SELECT position_id, curve_id, bucket, dv01 FROM position_risk WHERE run_id=?", (rid,)):
        risk.setdefault(r[0], []).append((f"IR|{r[1]}|{r[2]}", r[1], r[3]))
    gam = {}
    for r in con.execute("SELECT position_id, factor_1, factor_2, gamma FROM position_gamma WHERE run_id=?", (rid,)):
        gam.setdefault(r[0], []).append((r[1], r[2], r[3]))
    q = {}

    def add(a, b, v):
        if a is None or b is None or not v:
            return
        k = (a, b) if a <= b else (b, a)
        q[k] = q.get(k, 0.0) + v

    for pid, p in pos.items():
        cs_f = f"CS|{p['rating']}" if p["rating"] and not p["is_gov"] else None
        g = gam.get(pid, [])
        has_ir = any(not a.startswith("FX|") and not b.startswith("FX|") for a, b, _ in g)
        has_fx = any(a.startswith("FX|") or b.startswith("FX|") for a, b, _ in g)
        m = lambda f: cs_f if f == "CS" else f  # noqa: E731
        for a, b, v in g:
            add(m(a), m(b), (0.5 if a == b else 1.0) * v)
        rk = risk.get(pid, [])
        if not has_ir and p["convexity"] and p["mv_rub"] and p["dv01"]:
            G = p["mv_rub"] * p["convexity"] * 1e-8
            w = [(f, dv / p["dv01"]) for f, _, dv in rk]
            for i, (fa, wa) in enumerate(w):
                for fb, wb in w[i:]:
                    add(fa, fb, (0.5 if fa == fb else 1.0) * G * wa * wb)
                if p["cs01"] and cs_f:
                    add(fa, cs_f, G * wa)
            if p["cs01"] and cs_f:
                add(cs_f, cs_f, 0.5 * G)
        if not has_fx:
            for f, c, dv in rk:
                ccy = curve_ccy.get(c)
                if ccy and ccy != "RUB":
                    add(f"FX|{ccy}", f, dv / 100)
            if p["ccy"] != "RUB" and p["cs01"] and cs_f:
                add(f"FX|{p['ccy']}", cs_f, p["cs01"] / 100)
    return q


def portfolio_sensitivities(con):
    """Чувствительности последнего опубликованного прогона: (dv{curve:[11]}, fx{ccy:delta}, fxrate, cs01{rating}, run, quad)."""
    run = con.execute("SELECT * FROM calc_run WHERE status='complete' ORDER BY as_of DESC, published_at DESC, run_id DESC LIMIT 1").fetchone()
    if not run:
        return None
    rid = run["run_id"]
    dv = {}
    for r in con.execute("SELECT r.curve_id, b.ord, SUM(r.dv01) FROM position_risk r JOIN ref_bucket b ON b.bucket=r.bucket"
                         " WHERE r.run_id=? GROUP BY r.curve_id, b.ord", (rid,)):
        dv.setdefault(r[0], [0.0] * len(B))[r[1]] = r[2]
    fx = {}
    for r in con.execute("SELECT ccy, SUM(delta) FROM position_fx WHERE run_id=? GROUP BY ccy", (rid,)):
        fx[r[0]] = fx.get(r[0], 0) + r[1]
    for r in con.execute("SELECT ccy, SUM(amount) FROM funding_deal WHERE run_id=? AND ccy<>'RUB' GROUP BY ccy", (rid,)):
        fx[r[0]] = fx.get(r[0], 0) + r[1]
    rates = {r[0]: r[1] for r in con.execute("SELECT ccy, rate FROM mkt_fx WHERE run_id=?", (rid,))}
    cs = {}
    for r in con.execute("SELECT i.rating, SUM(p.cs01) FROM position p JOIN run_issuer i ON i.run_id=p.run_id AND i.issuer=p.issuer"
                         " WHERE p.run_id=? AND i.is_gov=0 GROUP BY i.rating", (rid,)):
        cs[r[0]] = r[1]
    return dv, fx, rates, cs, run, _quadratic(con, rid)


def window_shock(con, d0, d1):
    c0, c1 = M.curves_on(con, d0), M.curves_on(con, d1)
    curves = {c: [round((a - b) * 100, 2) for a, b in zip(c1[c], c0[c])] for c in c1 if c in c0}
    f0, f1 = M.fx_on(con, d0), M.fx_on(con, d1)
    fx = {c: round((f1[c] / f0[c] - 1) * 100, 3) for c in f1 if c in f0 and c != "RUB" and f0[c]}
    spreads = M.rating_spread_moves(con, d0, d1)
    return {"curves": curves, "fx": fx, "spreads": dict(spreads, includeGov=False)}


def proxy_pnl(shock, sens):
    if not sens:  # без портфеля: величина движения RUB-кривых
        return -sum(abs(x) for c in ("RUB OFZ", "RUB RUONIA", "RUB KEY") for x in shock["curves"].get(c, []))
    dv, fx, rates, cs, _, quad = sens
    pnl = sum(d * s for c, v in dv.items() for d, s in zip(v, shock["curves"].get(c, [0] * len(B))))
    pnl += sum(fx[c] * rates.get(c, 0) * shock["fx"].get(c, 0) / 100 for c in fx)
    pnl += sum(cs01 * shock["spreads"].get(r, 0) for r, cs01 in cs.items() if r)
    return pnl + sum(v * factor_shock(a, shock) * factor_shock(b, shock) for (a, b), v in quad.items())


def factor_shock(f, shock):
    t = f.split("|")
    if t[0] == "IR":
        v = shock["curves"].get(t[1])
        return v[B.index(t[2])] if v and t[2] in B else 0.0
    if t[0] == "CS":
        return shock["spreads"].get(t[1], 0.0)
    if t[0] == "FX":
        return shock["fx"].get(t[1], 0.0)
    return 0.0


def build(db_path, horizons=(1, 10), top=5, date_from=None, date_to=None):
    """Пересобрать исторические сценарии. Возвращает список созданных {scenario_id, name, proxy_pnl}."""
    con = init_db(db_path)
    try:
        dates = [d for d in M.market_dates(con, date_from, date_to)
                 if con.execute("SELECT 1 FROM md_curve_point WHERE date=? LIMIT 1", (d,)).fetchone()]
        sens = portfolio_sensitivities(con)
        created = []
        with con:
            con.execute("DELETE FROM scenario_def WHERE kind='historical' AND scenario_id NOT IN (SELECT scenario_id FROM scenario_result)"
                        " AND scenario_id NOT IN (SELECT scenario_id FROM scenario_funding_result)")
            con.execute("UPDATE scenario_def SET active=0 WHERE kind='historical'")
            base_ord = (con.execute("SELECT COALESCE(MAX(ord), 0) FROM scenario_def WHERE kind='preset'").fetchone()[0] or 0) + 1
            for h in horizons:
                cands = []
                for i in range(h, len(dates)):
                    d0, d1 = dates[i - h], dates[i]
                    sh = window_shock(con, d0, d1)
                    if not sh["curves"]:
                        continue
                    cands.append((proxy_pnl(sh, sens), i, d0, d1, sh))
                cands.sort(key=lambda x: x[0])
                chosen = []
                for c in cands:
                    if len(chosen) >= top:
                        break
                    if all(abs(c[1] - x[1]) >= h for x in chosen):   # окна не пересекаются
                        chosen.append(c)
                for k, (pnl, _, d0, d1, sh) in enumerate(chosen):
                    sid = f"hist{h}d-{d1}"
                    name = f"История {h}Д: {_ru(d0)} → {_ru(d1)}"
                    desc = (f"Фактические изменения рынка за {h} раб. дн. ({d0} → {d1}). "
                            + (f"Прокси-PnL портфеля прогона {sens[4]['run_id']}: {pnl / 1e6:,.1f} млн ₽." if sens else "Портфеля нет: ранжировано по движению RUB-кривых."))
                    con.execute("INSERT INTO scenario_def (scenario_id, name, ord, definition_json, active, updated_at, kind, description)"
                                " VALUES (?,?,?,?,1,?, 'historical', ?) ON CONFLICT(scenario_id) DO UPDATE SET name=excluded.name, ord=excluded.ord,"
                                " definition_json=excluded.definition_json, active=1, updated_at=excluded.updated_at, kind='historical',"
                                " description=excluded.description",
                                (sid, name, base_ord + len(created), json.dumps(sh, ensure_ascii=False), now_iso(), desc))
                    created.append({"scenario_id": sid, "name": name, "proxy_pnl": pnl, "horizon": h})
        return created
    finally:
        con.close()


def _ru(d):
    return f"{d[8:10]}.{d[5:7]}.{d[0:4]}"
