import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import type { RiskPayload, RiskPosition } from '../api/types';
import { BASIS_CURVE, canon, curveMoves, normalize, ownSpreadShock, runTaylor } from './taylor';

const B = ['1M', '3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];

function pos(over: Partial<RiskPosition>): RiskPosition {
  return {
    position_id: 'P', book_id: 'B1', secid: 'S', name: null, quality: 'exact', issuer: null, sector: null,
    rating: null, spread_kind: null, spread_bp: null, pv_rub: 1e6, delta: [], gamma: [], ...over,
  };
}

// factors: 0 = IR 1Y, 1 = IR 5Y, 2 = CS, 3 = FX|USD
const payload: RiskPayload = {
  run_id: 1, business_date: '2026-10-15', buckets: B, curves: ['RUB OFZ'],
  factors: ['IR|RUB OFZ|1Y', 'IR|RUB OFZ|5Y', 'CS', 'FX|USD'],
  positions: [
    pos({ position_id: 'A', spread_kind: 'credit', spread_bp: 200, sector: 'Девелопмент', issuer: 'X',
      delta: [[0, -10], [1, -20], [2, -30]], gamma: [[0, 0, 2], [0, 1, 1], [2, 2, 4]] }),
    pos({ position_id: 'B', book_id: 'B2', spread_kind: 'ofz', delta: [[1, -5], [2, -7]] }),
    pos({ position_id: 'C', book_id: 'B2', spread_kind: 'credit', spread_bp: null, delta: [[2, -1], [3, 50]] }),
  ],
};

describe('normalize', () => {
  it('expands scalars, drops zeros and _mult 1', () => {
    const n = normalize({ curves: { 'RUB OFZ': 100, Z: 0 }, ofzSpread: 0, spreads: { _mult: 1, _all: 0, sector: { a: 0 } }, fx: { USD: 0 } });
    expect(n).toEqual({ curves: { 'RUB OFZ': new Array(11).fill(100) } });
    expect(normalize({ curves: { Z: 0 }, spreads: null })).toEqual({});
    expect(canon(n)).toBe(canon(normalize({ curves: { 'RUB OFZ': new Array(11).fill(100) } })));
  });
  it('rejects bad input', () => {
    expect(() => normalize({ curves: { 'RUB OFZ': [1, 2] } })).toThrow();
    expect(() => normalize({ curves: { 'RUB OFZ': 5001 } })).toThrow();
    expect(() => normalize({ curves: {}, spreads: { _mult: 0 } })).toThrow();
    expect(() => normalize({ curves: {}, fx: { USD: -100 } })).toThrow();
    expect(() => normalize({ curves: {}, basis: 5 } as never)).toThrow();
    expect(() => normalize({ curves: { USD: 10 } }, ['RUB OFZ'])).toThrow();
    expect(normalize({ curves: { USD: 0 } }, ['RUB OFZ'])).toEqual({});
    // the RUB curves core reprices are valid for every run (as in Python)
    expect(normalize({ curves: { 'RUB KEY': 10 } }, ['RUB OFZ'])).toEqual({ curves: { 'RUB KEY': new Array(11).fill(10) } });
    expect(() => normalize({ curves: {}, ofzSpread: true } as never)).toThrow();
  });
});

describe('ownSpreadShock', () => {
  const def = normalize({ curves: {}, ofzSpread: 10, spreads: { _all: 25, _mult: 2, sector: { 'Девелопмент': 100 }, issuer: { X: 7 } } });
  it('credit: _all + (_mult-1)·spread + sector + issuer', () => expect(ownSpreadShock(def, payload.positions[0])).toBe(25 + 200 + 100 + 7));
  it('ofz: ofzSpread', () => expect(ownSpreadShock(def, payload.positions[1])).toBe(10));
  it('null spread_bp adds 0 for _mult', () => expect(ownSpreadShock(def, payload.positions[2])).toBe(25));
  it('no spread kind: 0', () => expect(ownSpreadShock(def, pos({}))).toBe(0));
});

describe('runTaylor (hand-computed)', () => {
  const curve = new Array(11).fill(0);
  curve[3] = 10; curve[6] = 20;  // 1Y +10, 5Y +20
  const def = normalize({ curves: { 'RUB OFZ': curve }, ofzSpread: 4, spreads: { _all: 5 }, fx: { USD: 2 } });
  const r = runTaylor(payload, def);
  it('position A: rates, spread, convexity', () => {
    // rates -10·10 - 20·20 = -500; spread -30·5 = -150; conv ½·2·100 + 1·10·20 + ½·4·25 = 100 + 200 + 50
    expect(r.positions[0]).toEqual({ position_id: 'A', book_id: 'B1', rates: -500, spread: -150, fx: 0,
      convexity: 350, total: -300, second_order: true, max_move_bp: 20 });
  });
  it('fx and totals by book', () => {
    expect(r.positions[2].fx).toBe(100);
    expect(r.positions[2].second_order).toBe(false);
    // B: -5·20 - 7·4 = -128; C: -1·5 + 50·2 = 95
    expect(r.totals.B2.total).toBe(-128 + 95);
    expect(r.totals._total.total).toBe(-300 - 128 + 95);
  });
});

describe('curveMoves (R18: OFZ curve = RUONIA + basis)', () => {
  const v = (x: number) => new Array<number>(11).fill(x);
  it('RUONIA alone moves the OFZ curve with it, the basis stays', () =>
    expect(curveMoves(normalize({ curves: { 'RUB RUONIA': 10 } }))).toEqual({ 'RUB RUONIA': v(10), 'RUB OFZ': v(10) }));
  it('the OFZ curve alone is a basis move', () =>
    expect(curveMoves(normalize({ curves: { 'RUB OFZ': 30, 'RUB RUONIA': 10 } })))
      .toEqual({ 'RUB RUONIA': v(10), 'RUB OFZ': v(30), [BASIS_CURVE]: v(20) }));
  it('the basis wins over RUB OFZ', () =>
    expect(curveMoves(normalize({ curves: { [BASIS_CURVE]: 5, 'RUB OFZ': 99 } }))).toEqual({ [BASIS_CURVE]: v(5), 'RUB OFZ': v(5) }));
  it('all rate curves alike: no basis', () =>
    expect(curveMoves(normalize({ curves: { 'RUB OFZ': 7, 'RUB RUONIA': 7, 'RUB KEY': 7 } }))[BASIS_CURVE]).toBeUndefined());
});

// Parity with the Python reference (S6 plan §3); the fixture is written by the backend agent.
const FIXTURE = fileURLToPath(new URL('../../../backend/tests/fixtures/taylor_parity.json', import.meta.url));
const haveFixture = existsSync(FIXTURE);

describe('parity with backend/tests/fixtures/taylor_parity.json', () => {
  it('fixture exists', () => expect(haveFixture, `${FIXTURE} missing: DESK_REGEN_FIXTURES=1 pytest tests/test_slice_s6.py -k parity`).toBe(true));
  const fx = haveFixture ? JSON.parse(readFileSync(FIXTURE, 'utf8')) : { cases: [] };
  const close = (got: number, exp: number) => Math.abs(got - exp) <= fx.tolerance_rel * Math.max(1, Math.abs(exp));
  const PARTS = ['rates', 'spread', 'fx', 'convexity', 'total'] as const;

  it('has cases', () => expect(fx.cases.length).toBeGreaterThan(0));
  for (const c of fx.cases) {
    it(c.name, () => {
      expect(canon(normalize(c.definition))).toBe(canon(c.definition));  // same normalized form as Python
      const got = runTaylor(c.payload, normalize(c.definition));
      const bad: string[] = [];
      c.expected.positions.forEach((e: Record<string, number | string | boolean>, i: number) => {
        const g = got.positions[i];
        if (g.position_id !== e.position_id || g.second_order !== e.second_order) bad.push(`${e.position_id}: id/second_order`);
        for (const k of [...PARTS, 'max_move_bp'] as const)
          if (!close(g[k], e[k] as number)) bad.push(`${e.position_id}.${k}: ${g[k]} vs ${e[k]}`);
      });
      expect(Object.keys(got.totals).sort()).toEqual(Object.keys(c.expected.totals).sort());
      for (const [book, e] of Object.entries<Record<string, number>>(c.expected.totals))
        for (const k of PARTS) if (!close(got.totals[book][k], e[k])) bad.push(`totals.${book}.${k}: ${got.totals[book][k]} vs ${e[k]}`);
      expect(got.positions.length).toBe(c.expected.positions.length);
      expect(bad).toEqual([]);
    });
  }
});
