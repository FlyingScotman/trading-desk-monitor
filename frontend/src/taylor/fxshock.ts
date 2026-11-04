// FX shock of the scenario screen from the published OCP (TDM#577, D12). The linear part is the OCP of the run by
// currency (the lines the OCP screen reads) without the non-linear derivatives, x the shock; the non-linear derivatives
// (an option, a position with an FX gamma) are apart, by delta-gamma. No published OCP: the positions' FX deltas, as before.
import type { RiskPayload, ScenarioResult } from '../api/types';
import type { NormDef, TaylorRow, TaylorRun } from './taylor';

export interface FxCcyRow { ccy: string; amount: number; nonlinear: number; linear: number; rate: number | null; pct: number; rub: number }
export interface FxNonlinear {
  n: number;
  /** delta + gamma over the FX factors of those positions, RUB (≈) */
  taylor: number;
  /** their whole exact repricing (all factors) when the exact job covers them, RUB; null otherwise */
  exact: number | null;
  nExact: number;
  /** the same positions' Taylor over all factors, to set against `exact` */
  taylorAll: number;
}
export interface FxShock {
  mode: 'ocp' | 'positions';
  /** why there is no OCP (mode positions) */
  reason: string | null;
  asOf: string | null;
  methodNote: string | null;
  /** how the published OCP was made (`derived`: from positions, no report figure) */
  basis: string | null;
  /** the table to show: in mode ocp the FX column is the OCP's linear part, the non-linear derivatives' FX are out of it */
  taylor: TaylorRun;
  ccys: FxCcyRow[];
  linearTotal: number;
  missingRate: string[];
  /** non-linear positions the OCP took at their amount, not at the FX delta: their share is approximate */
  approxIds: string[];
  nonlinear: FxNonlinear;
}

/** The table's row for the non-linear derivatives' FX (delta-gamma), so the table still sums to the scenario's total. */
export const NL_KEY = 'Нелинейные деривативы (FX, ≈)';

const fxIndex = (payload: RiskPayload) => payload.factors.map((f, i) => (f.startsWith('FX|') ? i : -1));

/** FX part of a position (delta x shock + gamma between FX factors), RUB. */
function fxPart(payload: RiskPayload, norm: NormDef, p: RiskPayload['positions'][number]): number {
  const fx = fxIndex(payload);
  const x = (i: number) => norm.fx?.[payload.factors[i].slice(3)] ?? 0;
  let s = 0;
  for (const [i, d] of p.delta) if (fx[i] >= 0) s += d * x(i);
  for (const [i, j, g] of p.gamma) if (fx[i] >= 0 && fx[j] >= 0) s += (i === j ? 0.5 : 1) * g * x(i) * x(j);
  return s;
}

export function fxShock(payload: RiskPayload, norm: NormDef, taylor: TaylorRun, result: ScenarioResult | null = null): FxShock {
  const ocp = payload.ocp;
  const none: FxNonlinear = { n: 0, taylor: 0, exact: null, nExact: 0, taylorAll: 0 };
  if (!ocp || !ocp.available) {
    return { mode: 'positions', reason: ocp?.reason ?? 'в прогоне нет публикуемой ОВП', asOf: null, methodNote: null, basis: null, taylor,
      ccys: [], linearTotal: taylor.totals._total?.fx ?? 0, missingRate: [], approxIds: [], nonlinear: none };
  }
  const nl = new Set(ocp.nonlinear_ids);
  const fx = fxIndex(payload);
  // positions: the FX part leaves the column (the linear part comes from the OCP, the non-linear one is apart)
  const nlr: FxNonlinear = { ...none };
  const byId = new Map(result?.positions.map(r => [r.position_id, r]));
  const rows: TaylorRow[] = taylor.positions.map((r, k) => {
    const p = payload.positions[k];
    const part = fxPart(payload, norm, p);
    if (nl.has(p.position_id)) {
      nlr.n += 1; nlr.taylor += part; nlr.taylorAll += r.total;
      const e = byId.get(p.position_id);
      if (e?.exact != null) { nlr.exact = (nlr.exact ?? 0) + e.exact; nlr.nExact += 1; }
    }
    // the FX terms of this position leave `fx` and, for gamma between FX factors, `convexity`
    let gFx = 0;
    for (const [i, j, g] of p.gamma) if (fx[i] >= 0 && fx[j] >= 0) gFx += (i === j ? 0.5 : 1) * g * (norm.fx?.[payload.factors[i].slice(3)] ?? 0) * (norm.fx?.[payload.factors[j].slice(3)] ?? 0);
    const convexity = r.convexity - gFx;
    return { ...r, fx: 0, convexity, total: r.rates + r.spread + convexity };
  });
  // nonlinear.exact counts only when every non-linear position is covered, else the figure would mix
  if (nlr.exact != null && nlr.nExact < nlr.n) nlr.exact = null;

  const zero = () => ({ rates: 0, spread: 0, fx: 0, convexity: 0, total: 0 });
  const totals: TaylorRun['totals'] = {};
  const add = (key: string, d: { rates?: number; spread?: number; fx?: number; convexity?: number; total?: number }) => {
    const t = (totals[key] ??= zero());
    for (const k of ['rates', 'spread', 'fx', 'convexity', 'total'] as const) t[k] += d[k] ?? 0;
  };
  for (const r of rows) { add(r.book_id, r); add('_total', r); }

  const ccys: FxCcyRow[] = [];
  const rate = (c: string) => ocp.by_ccy[c]?.rate ?? null;
  for (const [ccy, v] of Object.entries(ocp.by_ccy)) {
    const pct = norm.fx?.[ccy] ?? 0, r = v.rate ?? null;
    ccys.push({ ccy, amount: v.amount, nonlinear: v.nonlinear, linear: v.linear, rate: r, pct, rub: r == null ? 0 : v.linear * r * pct / 100 });
  }
  let linearTotal = 0;
  for (const l of ocp.by_book) {
    const r = rate(l.ccy);
    if (r == null) continue;
    const rub = l.linear * r * (norm.fx?.[l.ccy] ?? 0) / 100;
    const key = l.book ?? `${l.entity ?? 'юрлицо'}: ${l.books.length ? l.books.join(', ') : 'остаток юрлица без книг'}`;
    add(key, { fx: rub, total: rub });
    add('_total', { fx: rub, total: rub });
    linearTotal += rub;
  }
  if (nlr.n) { add(NL_KEY, { fx: nlr.taylor, total: nlr.taylor }); add('_total', { fx: nlr.taylor, total: nlr.taylor }); }
  return { mode: 'ocp', reason: null, asOf: ocp.as_of ?? null, methodNote: ocp.method_note ?? null, basis: ocp.basis ?? null,
    taylor: { positions: rows, totals }, ccys, linearTotal, missingRate: ocp.missing_rate, approxIds: ocp.approx_ids ?? [], nonlinear: nlr };
}
