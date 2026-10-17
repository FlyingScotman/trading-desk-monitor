"""Validation report: our analytics vs MOEX analytics carried in the same snapshot.

For every active instrument of the static snapshot it records whether the backend priced it
or why not. For priced bonds it recomputes the effective yield at the MOEX weighted-average
and close prices and compares it with ``source_analytics``:
- yields (MOEX ``effective_yield_waprice_pct`` has 4 decimals, ``yield_close_pct`` 2);
- Macaulay duration in days;
- the yield horizon date;
- G-spread to the KBD at the duration;
- Z-spread to the KBD (annual and continuous).

Differences point at a convention gap or a data problem in the hub, the adapter or core.

MOEX convention (found in S1): MOEX effective yields and durations are measured from the
**trade date**, not from the T+1 settlement date, with the same dirty price. The comparison
therefore recomputes yield and duration from the trade date; the desk valuation itself stays
at settlement.
"""

from __future__ import annotations

import csv
import html
import json
import statistics
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from rub_fixed_income import __version__ as CORE_VERSION
from rub_fixed_income import (
    bond_duration,
    solve_continuous_z_spread,
    solve_nominal_annual_yield,
    solve_z_spread,
)

from ..orchestrator import EodContext, Outcome
from ..valuation.bonds import SPREAD_CONVENTION

SHORT_DAYS = 30
OK_BP, WARN_BP = 1.0, 10.0


def _f(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


def compare(ctx: EodContext, o: Outcome) -> dict[str, Any]:
    t, q, v = o.terms, o.quote, o.valuation
    row: dict[str, Any] = {
        "secid": o.secid,
        "name": t.short_name if t else None,
        "class": t.bond_class if t else None,
        "coupon_type": t.coupon_type if t else None,
        "status": "rejected" if v is None else "priced",
        "reason": o.reason,
        "detail": o.detail,
    }
    if v is None or q is None:
        return row
    sa = q.source_analytics
    ydate = q.trade_date  # MOEX measures yields from the trade date (see module docstring)
    flows = [(c.payment_date, c.amount) for c in v.cashflows if c.payment_date > ydate]
    days = (v.horizon_date - v.settlement).days
    row.update(
        horizon=v.horizon_date.isoformat(), horizon_kind=v.horizon_kind, horizon_days=days,
        moex_horizon=sa.get("yield_date"), moex_horizon_type=sa.get("yield_date_type"),
        price_basis=v.price_basis, clean_pct=v.clean_pct, ytm_pct=round(v.ytm_pct, 4),
        z_spread_bp=round(v.z_spread_bp, 2), dv01_rub=v.dv01_rub,
    )

    def y_at(basis: str) -> float | None:
        if basis not in q.prices_pct:
            return None
        dirty = float(q.prices_pct[basis]) * v.face_rub / 100.0 + v.accrued_rub
        try:
            return solve_nominal_annual_yield(dirty_price=dirty, settlement_date=ydate, cashflows=flows) * 100
        except ValueError:
            return None

    y_wap, y_close = y_at("waprice"), y_at("close")
    m_wap, m_close = _f(sa.get("effective_yield_waprice_pct")), _f(sa.get("yield_close_pct"))
    row["y_wap"], row["moex_y_wap"] = y_wap, m_wap
    row["d_y_wap_bp"] = (y_wap - m_wap) * 100 if y_wap is not None and m_wap is not None else None
    row["d_y_close_bp"] = (y_close - m_close) * 100 if y_close is not None and m_close is not None else None
    m_dur = _f(sa.get("duration_days"))
    mac = bond_duration(annual_yield=y_wap / 100, settlement_date=ydate, cashflows=flows).macaulay_years \
        if y_wap is not None else v.macaulay_years
    row["mac_days"], row["moex_dur_days"] = round(mac * 365, 1), m_dur
    row["d_dur_days"] = mac * 365 - m_dur if m_dur is not None else None
    row["horizon_match"] = sa.get("yield_date") == v.horizon_date.isoformat() if sa.get("yield_date") else None
    if y_wap is not None:
        row["g_spread_bp"] = (y_wap - ctx.kbd.yield_pct(mac)) * 100
        row["moex_g_spread_bp"] = _f(sa.get("gspread_bp"))
        dirty_wap = float(q.prices_pct["waprice"]) * v.face_rub / 100.0 + v.accrued_rub
        cf = [(c.payment_date, c.amount) for c in v.cashflows]
        try:
            row["z_annual_wap_bp"] = solve_z_spread(cf, dirty_wap, ctx.curve, settlement_date=v.settlement,
                                                    convention=SPREAD_CONVENTION) * 1e4
            row["z_cont_wap_bp"] = solve_continuous_z_spread(cf, dirty_wap, ctx.curve,
                                                             settlement_date=v.settlement) * 1e4
        except (ValueError, RuntimeError):
            pass
        row["moex_z_spread_bp"] = _f(sa.get("zspread_bp"))
    d = row.get("d_y_wap_bp")
    if days < SHORT_DAYS:
        row["check"] = "short"
    elif d is None:
        row["check"] = "no_moex"
    elif abs(d) <= OK_BP and row["horizon_match"] is not False:
        row["check"] = "ok"
    elif abs(d) <= WARN_BP:
        row["check"] = "warn"
    else:
        row["check"] = "mismatch"
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_class: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r["class"] or "?"].append(r)
    for cls, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        priced = [r for r in rs if r["status"] == "priced"]
        comparable = [r for r in priced if r.get("check") in ("ok", "warn", "mismatch")]
        dys = [r["d_y_wap_bp"] for r in comparable]
        by_class[cls] = {
            "instruments": len(rs),
            "priced": len(priced),
            "comparable": len(comparable),
            "ok": sum(r["check"] == "ok" for r in comparable),
            "warn": sum(r["check"] == "warn" for r in comparable),
            "mismatch": sum(r["check"] == "mismatch" for r in comparable),
            "median_d_y_bp": round(statistics.median(dys), 2) if dys else None,
            "median_abs_d_y_bp": round(statistics.median(abs(x) for x in dys), 2) if dys else None,
        }
    reasons = Counter(r["reason"] for r in rows if r["status"] == "rejected")
    return {"by_class": by_class, "reasons": reasons.most_common()}


def build(ctx: EodContext, outcomes: list[Outcome]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = [compare(ctx, o) for o in outcomes]
    return rows, summarize(rows)


def write(out_dir: Path, ctx: EodContext, rows: list[dict[str, Any]], summary: dict[str, Any]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"validation_{ctx.business_date.isoformat()}"
    cols = sorted({k for r in rows for k in r}, key=lambda k: list(rows[0]).index(k) if k in rows[0] else 99)
    with (out_dir / f"{stem}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    meta = {"business_date": ctx.business_date.isoformat(), "generated": date.today().isoformat(),
            **ctx.lineage(), "core_version": CORE_VERSION}
    page = _TEMPLATE.replace("__DATA__", json.dumps({"rows": rows, "summary": summary, "meta": meta},
                                                     ensure_ascii=False, default=str).replace("</", "<\\/"))
    page = page.replace("__DATE__", html.escape(ctx.business_date.isoformat()))
    path = out_dir / f"{stem}.html"
    path.write_text(page, encoding="utf-8")
    return path


_TEMPLATE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сверка с MOEX</title>
<style>
:root{--bg:#fbfaf7;--fg:#1d1f22;--muted:#6b6f76;--line:#e3e0d8;--card:#fff;--ok:#2f7d4f;--warn:#a86b00;--bad:#b3261e;--accent:#2b5c8a}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#16181b;--fg:#e6e4df;--muted:#9a9ea5;--line:#2c3036;--card:#1e2125;--ok:#5cbf83;--warn:#e0a53a;--bad:#ef7a72;--accent:#7fb0de}}
:root[data-theme="dark"]{--bg:#16181b;--fg:#e6e4df;--muted:#9a9ea5;--line:#2c3036;--card:#1e2125;--ok:#5cbf83;--warn:#e0a53a;--bad:#ef7a72;--accent:#7fb0de}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1200px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)}.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{position:sticky;top:0;background:var(--card);cursor:pointer;font-weight:600;font-size:12px;color:var(--muted)}
td.l,th.l{text-align:left}
.ok{color:var(--ok)}.warn{color:var(--warn)}.mismatch{color:var(--bad)}.short,.no_moex{color:var(--muted)}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}
select,input{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:5px 8px;font:inherit}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:8px;margin-top:16px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.kpi b{display:block;font-size:20px}
</style></head><body><main>
<h1>Сверка расчёта бэкенда с аналитикой MOEX — __DATE__</h1>
<div class="muted" id="meta"></div>
<div class="kpis" id="kpis"></div>
<h2>По классам бумаг</h2><div class="wrap"><table id="cls"></table></div>
<p class="muted">Δy — наша эффективная доходность по средневзвешенной цене минус MOEX <span class="mono">effective_yield_waprice_pct</span>, обе от даты сделки (так считает MOEX; оценка позиций — от даты расчётов T+1).
ok: |Δy| ≤ 1 б.п. и тот же горизонт; warn: ≤ 10 б.п.; mismatch: больше. Горизонт короче 30 дней — short (не сравнивается).</p>
<h2>Почему не посчитано</h2><div class="wrap"><table id="rej"></table></div>
<h2>Бумаги</h2>
<div class="bar"><select id="fcls"></select><select id="fchk"></select><input id="fq" placeholder="поиск: secid / имя"></div>
<div class="wrap" style="max-height:70vh"><table id="det"></table></div>
</main>
<script>
const D=__DATA__;
const $=s=>document.querySelector(s), fmt=(v,d=2)=>v==null||v===''?'':(typeof v==='number'?v.toFixed(d):v);
const m=D.meta; $('#meta').textContent=`market ${m.market_snapshot_id.slice(0,12)} · static ${m.static_snapshot_id.slice(0,12)} · кривая ${m.curve_spec} (сверка с опубликованными точками ${m.kbd_check_bp} б.п.) · ${m.calendar} · core ${m.core_version}`;
const rows=D.rows, pr=rows.filter(r=>r.status==='priced'), cmp=pr.filter(r=>['ok','warn','mismatch'].includes(r.check));
const kp=[['Инструментов',rows.length],['Посчитано',pr.length],['Сравнимо с MOEX',cmp.length],['ok',cmp.filter(r=>r.check==='ok').length],['warn',cmp.filter(r=>r.check==='warn').length],['mismatch',cmp.filter(r=>r.check==='mismatch').length]];
$('#kpis').innerHTML=kp.map(([k,v])=>`<div class="kpi"><span class="muted">${k}</span><b>${v}</b></div>`).join('');
const C=D.summary.by_class;
$('#cls').innerHTML='<tr><th class="l">класс</th><th>всего</th><th>посчитано</th><th>сравнимо</th><th>ok</th><th>warn</th><th>mismatch</th><th>медиана Δy</th><th>медиана |Δy|</th></tr>'+
 Object.entries(C).map(([k,c])=>`<tr><td class="l">${k}</td><td>${c.instruments}</td><td>${c.priced}</td><td>${c.comparable}</td><td class="ok">${c.ok}</td><td class="warn">${c.warn}</td><td class="mismatch">${c.mismatch}</td><td>${fmt(c.median_d_y_bp)}</td><td>${fmt(c.median_abs_d_y_bp)}</td></tr>`).join('');
$('#rej').innerHTML='<tr><th class="l">причина</th><th>бумаг</th></tr>'+D.summary.reasons.map(([r,n])=>`<tr><td class="l mono">${r}</td><td>${n}</td></tr>`).join('');
const cols=[['secid','secid','l'],['name','имя','l'],['class','класс','l'],['check','сверка','l'],['reason','причина','l'],['horizon','горизонт','l'],['moex_horizon','горизонт MOEX','l'],['ytm_pct','YTM (legal close)',4],['d_y_wap_bp','Δy wap, б.п.',2],['d_y_close_bp','Δy close, б.п.',2],['mac_days','дюр., дн',0],['d_dur_days','Δдюр., дн',1],['g_spread_bp','G, б.п.',1],['moex_g_spread_bp','G MOEX',0],['z_annual_wap_bp','Z годовой',1],['z_cont_wap_bp','Z непр.',1],['moex_z_spread_bp','Z MOEX',0],['detail','детали','l']];
const fc=$('#fcls'), fk=$('#fchk');
fc.innerHTML='<option value="">все классы</option>'+Object.keys(C).map(k=>`<option>${k}</option>`).join('');
fk.innerHTML='<option value="">все статусы</option>'+['ok','warn','mismatch','short','no_moex','rejected'].map(k=>`<option>${k}</option>`).join('');
let sortK='d_y_wap_bp', sortDir=-1;
function render(){const c=fc.value,k=fk.value,q=$('#fq').value.toLowerCase();
 let rs=rows.filter(r=>(!c||r.class===c)&&(!k||(k==='rejected'?r.status==='rejected':r.check===k))&&(!q||(r.secid+' '+(r.name||'')).toLowerCase().includes(q)));
 rs.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='number'||typeof y==='number'){x=Math.abs(x??-1e9);y=Math.abs(y??-1e9)}return (x>y?1:x<y?-1:0)*sortDir});
 $('#det').innerHTML='<tr>'+cols.map(([k,t,d])=>`<th class="${d==='l'?'l':''}" data-k="${k}">${t}</th>`).join('')+'</tr>'+
  rs.slice(0,1500).map(r=>'<tr>'+cols.map(([k,t,d])=>`<td class="${d==='l'?'l ':''}${k==='check'?r.check:''}">${d==='l'?(r[k]??''):fmt(r[k],d)}</td>`).join('')+'</tr>').join('');
 document.querySelectorAll('#det th').forEach(th=>th.onclick=()=>{sortDir=sortK===th.dataset.k?-sortDir:-1;sortK=th.dataset.k;render()});}
[fc,fk].forEach(e=>e.onchange=render); $('#fq').oninput=render; render();
</script></body></html>
"""
