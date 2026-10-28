"""Validation report: our analytics vs MOEX analytics carried in the same snapshot.

For every active instrument of the static snapshot it records the quality of its valuation
(exact, assumed, a fallback rung of ``valuation.fallback``, or not valued) and why it is not
exact; the counts per quality and class are in the summary. For exact and assumed bonds it
recomputes the effective yield at the MOEX weighted-average and close prices and compares it
with ``source_analytics``:
- yields (MOEX ``effective_yield_waprice_pct`` has 4 decimals, ``yield_close_pct`` 2);
- Macaulay duration in days;
- the yield horizon date;
- G-spread to the KBD at the duration (and core's own G-spread of the valuation, at its price basis);
- Z-spread to the KBD (annual and continuous).

All priced bonds are recomputed in one core ``bond_metrics_batch`` per price basis (core 0.33): the scalar solvers'
definitions, solved with numpy (the recompute of 1,800 bonds: 1.9 s -> 0.6 s on 20.10.2026).

Differences point at a convention gap or a data problem in the hub, the adapter or core.

MOEX convention (R26, live EOD of 20.10.2026, 594 fixed bonds, as core 0.33's goldens): MOEX yields are measured at
the **T+1 settlement date** with the accrued interest of that date: MOEX ``ACCINT`` (as of the trade date) plus our
accrual of the days in between. ``EFFECTIVEYIELDWAPRICE`` then agrees to 0.003 bp (median), 495 of 594 within 0.5 bp;
from the trade date with the trade-date dirty price (the S1 reading) it was 1.2 bp, 220 of 594. MOEX ``DURATION`` is
the Macaulay duration at its published yield (core: within a day). The desk valuation itself stays at the trade date
(``valuation.bonds``, F9), so its ``ytm_pct`` is not MOEX's to the bp. The hub's EOD ``gspread_bp`` is on an intraday
KBD, not the closing one of the snapshot (11.8 bp median from MOEX's own yield and duration on the closing KBD): it is
shown, not compared bp for bp.
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

from rub_fixed_income import (
    CONTINUOUS,
    BondMetricsInput,
    CoreRejection,
    InvalidTerms,
    ZSpreadConvention,
    bond_metrics_batch,
)
from rub_fixed_income import __version__ as CORE_VERSION

from ..marketdata.adapter import core_instrument
from ..orchestrator import EodContext, Outcome
from ..valuation.bonds import SPREAD_CONVENTION, Rejected, fixed_schedule
from ..valuation.fallback import QUALITIES

Z_CONTINUOUS = ZSpreadConvention(compounding=CONTINUOUS)

COMPARED = ("exact", "assumed")
"""Qualities with a full schedule, compared with MOEX; the fallback rungs are only counted."""
SHORT_DAYS = 30
OK_BP, WARN_BP = 1.0, 10.0


def _f(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


def _row(o: Outcome) -> dict[str, Any]:
    """The comparison row without the recomputed analytics (``_metrics`` adds them)."""
    t, q, v = o.terms, o.quote, o.valuation
    row: dict[str, Any] = {
        "secid": o.secid,
        "name": t.short_name if t else None,
        "class": t.bond_class if t else None,
        "coupon_type": t.coupon_type if t else None,
        "status": "rejected" if v is None else "priced" if v.quality in COMPARED else "approximated",
        "quality": v.quality if v else "not_valued",
        "reason": o.reason,
        "detail": o.detail,
    }
    if v is not None and row["status"] == "approximated":
        row.update(price_basis=v.price_basis, clean_pct=v.clean_pct, pv_per_bond_rub=v.dirty_rub,
                   dv01_rub=v.dv01_rub, assumptions="; ".join(v.assumptions))
    if row["status"] != "priced" or q is None:
        return row
    sa = q.source_analytics
    days = (v.horizon_date - v.valuation_date).days
    row.update(
        horizon=v.horizon_date.isoformat(), horizon_kind=v.horizon_kind, horizon_days=days,
        moex_horizon=sa.get("yield_date"), moex_horizon_type=sa.get("yield_date_type"),
        price_basis=v.price_basis, clean_pct=v.clean_pct, ytm_pct=round(v.ytm_pct, 4),
        z_spread_bp=round(v.z_spread_bp, 2), dv01_rub=v.dv01_rub,
        **({"core_g_spread_bp": round(v.g_spread_bp, 2)} if v.g_spread_bp is not None else {}),
        **({"core_dm_bp": round(v.discount_margin_bp, 2)} if v.discount_margin_bp is not None else {}),
    )
    return row


def _accrual_to_settlement(ctx: EodContext, o: Outcome) -> float | None:
    """Our accrued interest at the T+1 settlement date minus at the trade date (core ``Instrument.accrued`` on the
    valuation's terms), None when it cannot be computed."""
    v, t = o.valuation, o.terms
    assert v is not None and t is not None
    try:
        if v.floater is not None:
            inst = v.floater.instrument
            return inst.accrued(v.settlement, ctx.core_market) - inst.accrued(v.valuation_date, ctx.core_market)
        sch = fixed_schedule(t, v.valuation_date)
        inst = core_instrument(t, v.valuation_date, sch.horizon_date, sch.offer, coupons=sch.coupons)
        return inst.accrued(v.settlement) - inst.accrued(v.valuation_date)
    except (CoreRejection, Rejected, ValueError):
        return None


def _inputs(o: Outcome, basis: str, accrual: float | None) -> BondMetricsInput | None:
    """The bond at the MOEX price ``basis`` (``waprice``, ``close``) on its valuation's flows, settled T+1 with the
    MOEX accrued moved to that date by ``accrual``, as MOEX measures yields (see the module docstring)."""
    q, v = o.quote, o.valuation
    if q is None or v is None or basis not in q.prices_pct or accrual is None or v.perpetual_tail is not None:
        return None  # a never-called perpetual: its flows are the listed coupons without the tail (MOEX: to a call)
    dirty = float(q.prices_pct[basis]) * v.face_rub / 100.0 + v.accrued_rub + accrual
    flows = tuple(c for c in v.cashflows if c.payment_date > v.settlement)
    try:
        return BondMetricsInput(v.settlement, dirty, flows, o.secid)
    except (ValueError, InvalidTerms):
        return None


def _metrics(ctx: EodContext, priced: list[tuple[dict[str, Any], Outcome]]) -> None:
    """Our yield, duration and spreads at the MOEX prices of every priced bond, vs MOEX's: core
    ``bond_metrics_batch`` (core 0.33) for all bonds at once instead of the scalar solvers per bond, the same
    definitions (``solve_nominal_annual_yield``, ``bond_duration``, ``g_spread_bp``, ``solve_z_spread``) to ~1e-12."""
    if not priced:
        return
    accrual = {id(o): _accrual_to_settlement(ctx, o) for _, o in priced}
    wap = [(row, o, i) for row, o in priced if (i := _inputs(o, "waprice", accrual[id(o)])) is not None]
    close = [(row, i) for row, o in priced if (i := _inputs(o, "close", accrual[id(o)])) is not None]
    if wap:
        ins = [i for _, _, i in wap]
        annual = bond_metrics_batch(ins, kbd=ctx.kbd, curve=ctx.curve, spread_convention=SPREAD_CONVENTION)
        cont = bond_metrics_batch(ins, curve=ctx.curve, spread_convention=Z_CONTINUOUS)
        for (row, o, _), m, c in zip(wap, annual, cont):
            if m.effective_yield is not None:
                row["y_wap"] = m.effective_yield * 100
                row["mac_days"] = round(m.macaulay_years * 365, 1)
                row["g_spread_bp"] = m.g_spread_bp
                row["moex_g_spread_bp"] = _f(o.quote.source_analytics.get("gspread_bp"))
            if m.z_spread_bp is not None:
                row["z_annual_wap_bp"] = m.z_spread_bp
            if c.z_spread_bp is not None:
                row["z_cont_wap_bp"] = c.z_spread_bp
            row["moex_z_spread_bp"] = _f(o.quote.source_analytics.get("zspread_bp"))
    for (row, _), m in zip(close, bond_metrics_batch([i for _, i in close]) if close else []):
        if m.effective_yield is not None:
            row["y_close"] = m.effective_yield * 100


def _check(row: dict[str, Any], o: Outcome) -> None:
    sa = o.quote.source_analytics  # type: ignore[union-attr]
    v = o.valuation
    assert v is not None
    y_wap, y_close = row.pop("y_wap", None), row.pop("y_close", None)
    m_wap, m_close = _f(sa.get("effective_yield_waprice_pct")), _f(sa.get("yield_close_pct"))
    row["y_wap"], row["moex_y_wap"] = y_wap, m_wap
    row["d_y_wap_bp"] = (y_wap - m_wap) * 100 if y_wap is not None and m_wap is not None else None
    row["d_y_close_bp"] = (y_close - m_close) * 100 if y_close is not None and m_close is not None else None
    m_dur = _f(sa.get("duration_days"))
    row.setdefault("mac_days", round(v.macaulay_years * 365, 1))
    row["moex_dur_days"] = m_dur
    row["d_dur_days"] = row["mac_days"] - m_dur if m_dur is not None else None
    row["horizon_match"] = sa.get("yield_date") == v.horizon_date.isoformat() if sa.get("yield_date") else None
    d = row.get("d_y_wap_bp")
    if row["horizon_days"] < SHORT_DAYS:
        row["check"] = "short"
    elif d is None:
        row["check"] = "no_moex"
    elif abs(d) <= OK_BP and row["horizon_match"] is not False:
        row["check"] = "ok"
    elif abs(d) <= WARN_BP:
        row["check"] = "warn"
    else:
        row["check"] = "mismatch"


def compare_all(ctx: EodContext, outcomes: list[Outcome]) -> list[dict[str, Any]]:
    """One row per outcome (``compare``), the analytics of all priced bonds in one batch."""
    rows = [_row(o) for o in outcomes]
    priced = [(r, o) for r, o in zip(rows, outcomes) if r["status"] == "priced" and o.quote is not None]
    _metrics(ctx, priced)
    for r, o in priced:
        _check(r, o)
    return rows


def compare(ctx: EodContext, o: Outcome) -> dict[str, Any]:
    return compare_all(ctx, [o])[0]


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
            "quality": dict(Counter(r["quality"] for r in rs)),
            "comparable": len(comparable),
            "ok": sum(r["check"] == "ok" for r in comparable),
            "warn": sum(r["check"] == "warn" for r in comparable),
            "mismatch": sum(r["check"] == "mismatch" for r in comparable),
            "median_d_y_bp": round(statistics.median(dys), 2) if dys else None,
            "median_abs_d_y_bp": round(statistics.median(abs(x) for x in dys), 2) if dys else None,
        }
    reasons = Counter(r["reason"] for r in rows if r["status"] != "priced")
    return {"by_class": by_class, "reasons": reasons.most_common(), "qualities": [*QUALITIES, "not_valued"]}


def build(ctx: EodContext, outcomes: list[Outcome]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = compare_all(ctx, outcomes)
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
<h2>Качество оценки по классам</h2><div class="wrap"><table id="qual"></table></div>
<p class="muted">exact / assumed сравниваются с MOEX; market_value, stale_price, model_price, par_proxy — запасные ступени (цена без полного расписания, прошлая цена, модельная цена по медианному Z, номинал), not_valued — данных нет совсем.</p>
<h2>Почему не точно</h2><div class="wrap"><table id="rej"></table></div>
<h2>Бумаги</h2>
<div class="bar"><select id="fcls"></select><select id="fchk"></select><input id="fq" placeholder="поиск: secid / имя"></div>
<div class="wrap" style="max-height:70vh"><table id="det"></table></div>
</main>
<script>
const D=__DATA__;
const $=s=>document.querySelector(s), fmt=(v,d=2)=>v==null||v===''?'':(typeof v==='number'?v.toFixed(d):v);
const m=D.meta; $('#meta').textContent=`market ${m.market_snapshot_id.slice(0,12)} · static ${m.static_snapshot_id.slice(0,12)} · кривая ${m.curve_spec} (сверка с опубликованными точками ${m.kbd_check_bp} б.п.) · ${m.calendar} · core ${m.core_version}`;
const rows=D.rows, pr=rows.filter(r=>r.status==='priced'), cmp=pr.filter(r=>['ok','warn','mismatch'].includes(r.check));
const kp=[['Инструментов',rows.length],['Посчитано',pr.length],['Приближённо',rows.filter(r=>r.status==='approximated').length],['Не оценено',rows.filter(r=>r.status==='rejected').length],['Сравнимо с MOEX',cmp.length],['ok',cmp.filter(r=>r.check==='ok').length],['warn',cmp.filter(r=>r.check==='warn').length],['mismatch',cmp.filter(r=>r.check==='mismatch').length]];
$('#kpis').innerHTML=kp.map(([k,v])=>`<div class="kpi"><span class="muted">${k}</span><b>${v}</b></div>`).join('');
const C=D.summary.by_class;
$('#cls').innerHTML='<tr><th class="l">класс</th><th>всего</th><th>посчитано</th><th>сравнимо</th><th>ok</th><th>warn</th><th>mismatch</th><th>медиана Δy</th><th>медиана |Δy|</th></tr>'+
 Object.entries(C).map(([k,c])=>`<tr><td class="l">${k}</td><td>${c.instruments}</td><td>${c.priced}</td><td>${c.comparable}</td><td class="ok">${c.ok}</td><td class="warn">${c.warn}</td><td class="mismatch">${c.mismatch}</td><td>${fmt(c.median_d_y_bp)}</td><td>${fmt(c.median_abs_d_y_bp)}</td></tr>`).join('');
const Q=D.summary.qualities;
$('#qual').innerHTML='<tr><th class="l">класс</th>'+Q.map(q=>`<th>${q}</th>`).join('')+'</tr>'+Object.entries(C).map(([k,c])=>`<tr><td class="l">${k}</td>`+Q.map(q=>`<td>${c.quality[q]||''}</td>`).join('')+'</tr>').join('');
$('#rej').innerHTML='<tr><th class="l">причина</th><th>бумаг</th></tr>'+D.summary.reasons.map(([r,n])=>`<tr><td class="l mono">${r}</td><td>${n}</td></tr>`).join('');
const cols=[['secid','secid','l'],['name','имя','l'],['class','класс','l'],['check','сверка','l'],['quality','качество','l'],['reason','причина','l'],['horizon','горизонт','l'],['moex_horizon','горизонт MOEX','l'],['ytm_pct','YTM (legal close)',4],['d_y_wap_bp','Δy wap, б.п.',2],['d_y_close_bp','Δy close, б.п.',2],['mac_days','дюр., дн',0],['d_dur_days','Δдюр., дн',1],['g_spread_bp','G, б.п.',1],['moex_g_spread_bp','G MOEX',0],['z_annual_wap_bp','Z годовой',1],['z_cont_wap_bp','Z непр.',1],['moex_z_spread_bp','Z MOEX',0],['detail','детали','l'],['assumptions','допущения','l']];
const fc=$('#fcls'), fk=$('#fchk');
fc.innerHTML='<option value="">все классы</option>'+Object.keys(C).map(k=>`<option>${k}</option>`).join('');
fk.innerHTML='<option value="">все статусы</option>'+['ok','warn','mismatch','short','no_moex','approximated','rejected'].map(k=>`<option>${k}</option>`).join('');
let sortK='d_y_wap_bp', sortDir=-1;
function render(){const c=fc.value,k=fk.value,q=$('#fq').value.toLowerCase();
 let rs=rows.filter(r=>(!c||r.class===c)&&(!k||(['rejected','approximated'].includes(k)?r.status===k:r.check===k))&&(!q||(r.secid+' '+(r.name||'')).toLowerCase().includes(q)));
 rs.sort((a,b)=>{let x=a[sortK],y=b[sortK];if(typeof x==='number'||typeof y==='number'){x=Math.abs(x??-1e9);y=Math.abs(y??-1e9)}return (x>y?1:x<y?-1:0)*sortDir});
 $('#det').innerHTML='<tr>'+cols.map(([k,t,d])=>`<th class="${d==='l'?'l':''}" data-k="${k}">${t}</th>`).join('')+'</tr>'+
  rs.slice(0,1500).map(r=>'<tr>'+cols.map(([k,t,d])=>`<td class="${d==='l'?'l ':''}${k==='check'?r.check:''}">${d==='l'?(r[k]??''):fmt(r[k],d)}</td>`).join('')+'</tr>').join('');
 document.querySelectorAll('#det th').forEach(th=>th.onclick=()=>{sortDir=sortK===th.dataset.k?-sortDir:-1;sortK=th.dataset.k;render()});}
[fc,fk].forEach(e=>e.onchange=render); $('#fq').oninput=render; render();
</script></body></html>
"""
