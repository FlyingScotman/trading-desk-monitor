import { describe, expect, it } from 'vitest';
import type { RiskPayload, RiskPosition } from '../api/types';
import { NL_KEY, fxShock } from './fxshock';
import { normalize, runTaylor } from './taylor';

const B = ['1M', '3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];
const pos = (over: Partial<RiskPosition>): RiskPosition => ({
  position_id: 'P', book_id: 'B1', secid: 'S', name: null, quality: 'exact', issuer: null, sector: null,
  rating: null, spread_kind: null, spread_bp: null, pv_rub: 1e6, delta: [], gamma: [], ...over,
});
// factors: 0 = IR 1Y, 1 = FX|USD. Rate 90: a position's FX delta is RUB per 1 % = amount x 0.9.
const payload: RiskPayload = {
  run_id: 1, business_date: '2026-11-03', buckets: B, curves: ['RUB OFZ'], factors: ['IR|RUB OFZ|1Y', 'FX|USD'],
  positions: [
    pos({ position_id: 'bond', delta: [[0, -10], [1, 90 * 100 / 100]] }),
    pos({ position_id: 'opt', book_id: 'B2', delta: [[1, 0.9 * 30]], gamma: [[1, 1, 0.5]] }),
  ],
  ocp: {
    available: true, reason: null, as_of: '2026-11-03', basis: 'report_base_plus_day', method_note: null,
    by_ccy: { USD: { amount: 150, nonlinear: 30, linear: 120, rate: 90 } },
    by_book: [{ book: 'B1', books: ['B1'], entity: null, ccy: 'USD', amount: 120, nonlinear: 0, linear: 120 },
      { book: 'B2', books: ['B2'], entity: null, ccy: 'USD', amount: 30, nonlinear: 30, linear: 0 }],
    nonlinear_ids: ['opt'], approx_ids: [], missing_rate: [],
  },
};

describe('fxShock from the OCP', () => {
  const norm = normalize({ fx: { USD: 10 } });
  const t = runTaylor(payload, norm);
  it('linear part is the OCP without the option x the shock', () => {
    const s = fxShock(payload, norm, t);
    expect(s.mode).toBe('ocp');
    expect(s.linearTotal).toBeCloseTo(120 * 90 * 10 / 100);  // 1080, not the positions' FX
    expect(s.taylor.totals._total.fx).toBeCloseTo(1080 + 295);  // linear + the non-linear row
    expect(s.taylor.totals.B1.fx).toBeCloseTo(1080);
    expect(s.taylor.totals.B2.fx).toBeCloseTo(0);
  });
  it('the option is apart, delta-gamma, and out of the linear column', () => {
    const s = fxShock(payload, norm, t);
    expect(s.nonlinear.n).toBe(1);
    expect(s.nonlinear.taylor).toBeCloseTo(0.9 * 30 * 10 + 0.5 * 0.5 * 100);  // 270 + 25
    expect(s.taylor.totals.B2.total).toBeCloseTo(0);  // its FX and FX gamma are not in the book's columns
    expect(s.taylor.totals[NL_KEY].total).toBeCloseTo(295);  // a row of its own, summed into the total
    expect(s.taylor.totals._total.fx).toBeCloseTo(1080 + 295);
  });
  it('no published OCP: the positions\' FX deltas, said', () => {
    const s = fxShock({ ...payload, ocp: { ...payload.ocp!, available: false, reason: 'в прогоне нет публикуемой ОВП' } }, norm, t);
    expect(s.mode).toBe('positions');
    expect(s.taylor).toBe(t);
    expect(s.linearTotal).toBeCloseTo(t.totals._total.fx);
    expect(s.reason).toContain('нет публикуемой ОВП');
  });
  it('no non-linear derivatives', () => {
    const p2 = { ...payload, ocp: { ...payload.ocp!, nonlinear_ids: [] } };
    expect(fxShock(p2, norm, runTaylor(p2, norm)).nonlinear.n).toBe(0);
  });
});
