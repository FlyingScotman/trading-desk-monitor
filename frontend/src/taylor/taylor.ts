// T1 Taylor ≈ in the browser: a port of desk.scenario.{definition,taylor} (S6 plan §2–§3).
// Plain left-to-right sums in payload order, never reordered, so parity with Python holds to 1e-9.
import type { RiskPayload, RiskPosition, ScenarioDefinition, SpreadRules, TaylorParts } from '../api/types';

export const N_BUCKETS = 11;
const MAX_BP = 5000;
export const OFZ_CURVE = 'RUB OFZ', RUONIA_CURVE = 'RUB RUONIA', BASIS_CURVE = 'RUB OFZ-RUONIA';
/** The rate curves core reprices (Python RUB_RATE_CURVES): a parallel move of the rates moves these. */
export const RUB_RATE_CURVES: readonly string[] = [OFZ_CURVE, RUONIA_CURVE, 'RUB KEY'];
/** Valid in every run (Python RUB_CURVES): the rate curves and the OFZ–RUONIA basis (R18). */
export const RUB_CURVES: readonly string[] = [...RUB_RATE_CURVES, BASIS_CURVE];

export interface NormSpreads {
  _all?: number;
  _mult?: number;
  sector?: Record<string, number>;
  issuer?: Record<string, number>;
  rating?: Record<string, number>;
}
export interface NormDef {
  curves?: Record<string, number[]>;  // absent when no curve is shocked (as in Python)
  ofzSpread?: number;
  spreads?: NormSpreads;
  fx?: Record<string, number>;
}

export interface TaylorRow extends TaylorParts {
  position_id: string;
  book_id: string;
  second_order: boolean;
  max_move_bp: number;
}
export interface TaylorRun {
  positions: TaylorRow[];
  totals: Record<string, TaylorParts>;
}

function bp(v: unknown, what: string): number {
  if (typeof v !== 'number' || !Number.isFinite(v)) throw new Error(`${what}: не число`);
  if (Math.abs(v) > MAX_BP) throw new Error(`${what}: |${v}| > ${MAX_BP} б.п.`);
  return v;
}

function onlyKeys(o: object, allowed: string[], what: string) {
  const extra = Object.keys(o).filter(k => !allowed.includes(k));
  if (extra.length) throw new Error(`${what}: лишние ключи ${extra.join(', ')}`);
}

function bpMap(m: Record<string, number> | undefined, what: string, check = bp): Record<string, number> | undefined {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(m ?? {})) if (check(v, `${what}.${k}`) !== 0) out[k] = v;
  return Object.keys(out).length ? out : undefined;
}

/** Canonical form (plan §2): curves as 11 floats, zeros and _mult == 1 dropped. Throws on invalid input. */
export function normalize(def: ScenarioDefinition, curveNames?: readonly string[]): NormDef {
  onlyKeys(def, ['curves', 'ofzSpread', 'spreads', 'fx'], 'definition');
  const curves: Record<string, number[]> = {};
  for (const [c, v] of Object.entries(def.curves ?? {})) {
    const arr = Array.isArray(v) ? v : new Array<number>(N_BUCKETS).fill(v);
    if (arr.length !== N_BUCKETS) throw new Error(`кривая ${c}: нужно ${N_BUCKETS} бакетов`);
    const vec = arr.map((x, i) => bp(x, `${c}[${i}]`) + 0);  // -0 -> 0
    if (!vec.some(x => x !== 0)) continue;  // an all-zero curve is dropped, even an unpublished one
    if (curveNames && !curveNames.includes(c) && !RUB_CURVES.includes(c)) throw new Error(`кривая ${c} не публикуется в прогоне`);
    curves[c] = vec;
  }
  const out: NormDef = {};
  if (Object.keys(curves).length) out.curves = curves;
  if (def.ofzSpread != null && bp(def.ofzSpread, 'ofzSpread') !== 0) out.ofzSpread = def.ofzSpread;

  if (def.spreads) {
    const s: SpreadRules = def.spreads;
    onlyKeys(s, ['_all', '_mult', 'sector', 'issuer', 'rating'], 'spreads');
    const ns: NormSpreads = {};
    if (s._all != null && bp(s._all, '_all') !== 0) ns._all = s._all;
    if (s._mult != null) {
      if (typeof s._mult !== 'number' || !Number.isFinite(s._mult) || s._mult <= 0) throw new Error('_mult: нужно > 0');
      if (s._mult !== 1) ns._mult = s._mult;
    }
    for (const g of ['sector', 'issuer', 'rating'] as const) {
      const m = bpMap(s[g], g);
      if (m) ns[g] = m;
    }
    if (Object.keys(ns).length) out.spreads = ns;
  }

  const fx = bpMap(def.fx, 'fx', (v, what) => {
    if (typeof v !== 'number' || !Number.isFinite(v) || v <= -100) throw new Error(`${what}: нужно > −100 %`);
    return v;
  });
  if (fx) out.fx = fx;
  return out;
}

/** Stable JSON (sorted keys) to compare two normalized definitions. */
export function canon(v: unknown): string {
  if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
  if (v && typeof v === 'object') {
    const o = v as Record<string, unknown>;
    return '{' + Object.keys(o).sort().map(k => JSON.stringify(k) + ':' + canon(o[k])).join(',') + '}';
  }
  return JSON.stringify(v);
}

/** Own spread shock ds of a position, bp (plan §2). A null spread_bp adds 0 for _mult. */
export function ownSpreadShock(def: NormDef, p: RiskPosition): number {
  if (p.spread_kind === 'ofz') return def.ofzSpread ?? 0;
  if (p.spread_kind !== 'credit') return 0;
  const s = def.spreads ?? {};
  const pick = (m: Record<string, number> | undefined, k: string | null) => (k != null && m?.[k]) || 0;
  let ds = s._all ?? 0;
  if (s._mult != null && p.spread_bp != null) ds += (s._mult - 1) * p.spread_bp;
  ds += pick(s.sector, p.sector);
  ds += pick(s.issuer, p.issuer);
  ds += pick(s.rating, p.rating);
  return ds;
}

type Factor = { kind: 'IR'; curve: string; bucket: number } | { kind: 'CS' } | { kind: 'FX'; ccy: string };

function parseFactors(payload: RiskPayload): Factor[] {
  return payload.factors.map(f => {
    const [kind, a, b] = f.split('|');
    if (kind === 'IR') return { kind, curve: a, bucket: payload.buckets.indexOf(b) };
    if (kind === 'FX') return { kind, ccy: a };
    return { kind: 'CS' };
  });
}

/** The move of every shocked factor (Python curve_moves, R18): the curves as given, the OFZ curve and the basis set
 *  together: basis = its own vector, else RUB OFZ − RUB RUONIA when RUB OFZ is given, else 0; OFZ curve = RUB OFZ when
 *  given (exactly), else RUB RUONIA + basis. Both are listed only when nonzero. */
export function curveMoves(def: NormDef): Record<string, number[]> {
  const curves: Record<string, number[]> = { ...(def.curves ?? {}) };
  const zero = new Array<number>(N_BUCKETS).fill(0);
  const r = curves[RUONIA_CURVE] ?? zero;
  let basis: number[], ofz: number[];
  if (curves[BASIS_CURVE]) { basis = curves[BASIS_CURVE]; ofz = r.map((x, i) => x + basis[i]); }
  else if (curves[OFZ_CURVE]) { ofz = curves[OFZ_CURVE]; basis = ofz.map((o, i) => o - r[i]); }
  else { basis = zero; ofz = r; }
  for (const [c, v] of [[BASIS_CURVE, basis], [OFZ_CURVE, ofz]] as const) {
    if (v.some(x => x !== 0)) curves[c] = v; else delete curves[c];
  }
  return curves;
}

/** Shock x over payload.factors: IR = the factor's move (curveMoves), CS = ds, FX = %; missing = 0. */
export function shockVector(payload: RiskPayload, def: NormDef, p: RiskPosition, factors = parseFactors(payload),
                            moves = curveMoves(def)): Float64Array {
  const ds = ownSpreadShock(def, p);
  return Float64Array.from(factors, f =>
    f.kind === 'IR' ? moves[f.curve]?.[f.bucket] ?? 0 : f.kind === 'CS' ? ds : def.fx?.[f.ccy] ?? 0);
}

export function positionTaylor(payload: RiskPayload, def: NormDef, p: RiskPosition, factors = parseFactors(payload),
                               moves = curveMoves(def)): TaylorRow {
  const x = shockVector(payload, def, p, factors, moves);
  let rates = 0, spread = 0, fx = 0, convexity = 0, maxMove = Math.abs(ownSpreadShock(def, p));
  for (const [i, d] of p.delta) {
    const k = factors[i].kind;
    if (k === 'IR') rates += d * x[i];
    else if (k === 'CS') spread += d * x[i];
    else fx += d * x[i];
  }
  for (const [i, j, v] of p.gamma) convexity += (i === j ? 0.5 : 1.0) * v * x[i] * x[j];
  factors.forEach((f, i) => { if (f.kind === 'IR') maxMove = Math.max(maxMove, Math.abs(x[i])); });
  return {
    position_id: p.position_id, book_id: p.book_id,
    rates, spread, fx, convexity, total: rates + spread + fx + convexity,
    second_order: p.gamma.length > 0, max_move_bp: maxMove,
  };
}

const PARTS = ['rates', 'spread', 'fx', 'convexity', 'total'] as const;

/** Per-position rows plus totals per book_id and `_total`, summed in position order. */
export function runTaylor(payload: RiskPayload, def: NormDef): TaylorRun {
  const factors = parseFactors(payload);
  const moves = curveMoves(def);
  const positions = payload.positions.map(p => positionTaylor(payload, def, p, factors, moves));
  const totals: Record<string, TaylorParts> = {};
  const zero = (): TaylorParts => ({ rates: 0, spread: 0, fx: 0, convexity: 0, total: 0 });
  for (const r of positions) {
    for (const key of ['_total', r.book_id]) {
      const t = (totals[key] ??= zero());
      for (const k of PARTS) t[k] += r[k];
    }
  }
  return { positions, totals };
}
