'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { prepare } = require('../../prototypes/concepts/lib/c02-sim');
const parity = require('../../backend/tests/fixtures/taylor_parity.json');

const M = { buckets: ['3M', '20Y+'], pillars: [0.25, 20], riskCurveCcy: id => id.split(' ')[0] };
const sim = { scope: 'ALL', par: 0, twist: 0, basis: 0, credit: 0 };
const position = (id, ccy) => ({ id, ccy, cs01b: [0, 0], risk: { 'RUB OFZ': [0, 0] } });
const parts = ({ r, b, c, cvx, t }) => [r, b, c, cvx, t];
const factors = ['IR|RUB RUONIA|3M', 'IR|RUB RUONIA|20Y+', 'IR|RUB OFZ|3M', 'IR|USD SOFR|20Y+', 'CS', 'FX|USD'];
const credit = {
  position_id: 'credit', spread_kind: 'credit', delta: [[0, -10], [1, -20], [3, -7], [4, -30], [5, 100]],
  gamma: [[0, 0, 2], [0, 1, 3], [0, 4, 5], [1, 1, 4], [3, 3, 2], [4, 4, 6], [4, 5, 100], [5, 5, 100]],
};
const p = { ...position('credit', 'USD'), cs01b: [-7.5, -22.5] };
const runCredit = prepare(M, [], { factors, positions: [credit] });

test('параллельный сдвиг и наклон: диагональ, кросс IR и разбивка по бакетам', () => {
  const parallel = runCredit([p], { ...sim, par: 10 }, true).rows[0];
  assert.deepEqual(parts(parallel), [-370, 0, 0, 700, 330]);
  assert.deepEqual(parallel.byBucket.cvx, [250, 450, 0]);
  const { rows: [twist], shifts } = runCredit([p], { ...sim, twist: 20 }, true);
  assert.deepEqual(shifts, [-10, 10]);
  assert.deepEqual(parts(twist), [-170, 0, 0, 100, -70]);
  assert.deepEqual(twist.byBucket.cvx, [-50, 150, 0]);
});

test('кредит, смешанный IR × CS и выбор валюты по кривой и позиции', () => {
  const all = runCredit([p], { ...sim, par: 10, credit: 5 }, true).rows[0];
  assert.deepEqual(parts(all), [-370, 0, -150, 1025, 505]);
  assert.deepEqual(all.byBucket.c, [-37.5, -112.5, 0]);
  assert.deepEqual(all.byBucket.cvx, [518.75, 506.25, 0]);
  const scoped = scope => runCredit([p], { ...sim, scope, par: 10, credit: 5 }, true).rows[0];
  assert.deepEqual(parts(scoped('RUB')), [-300, 0, 0, 600, 300]);
  assert.deepEqual(parts(scoped('USD')), [-70, 0, -150, 175, -45]);
  const noWeights = runCredit([{ ...p, cs01b: [-1, 1] }], { ...sim, credit: 5 }, true).rows[0];
  assert.deepEqual(noWeights.byBucket.cvx, [0, 0, 75]);
  assert.equal(noWeights.cvx, 75);
});

test('базис ОФЗ шокирует уровень КБД и спред ОФЗ-ПК, собственный спред фиксированной ОФЗ сохраняется', () => {
  // ofz_levels: dRuo = dRuo|basis − dBasis, xKBD = xRuo + xBasis.
  const fixed = { position_id: 'fix', spread_kind: 'ofz', delta: [[0, 30], [2, -60], [4, -100]],
    gamma: [[0, 0, 2], [0, 2, -3], [2, 2, 6], [2, 4, 50], [4, 4, 100]] };
  const pk = { position_id: 'pk', spread_kind: 'ofz', delta: [[0, -10], [4, -20]], gamma: [[0, 0, 2], [0, 4, 4], [4, 4, 8]] };
  const facts = [
    { id: 'fix', risk: { 'RUB RUONIA': [30, 0], 'RUB OFZ': [-60, 0] }, ofzSpr01: -100, extra: { ofz_basis01: -60 } },
    { id: 'pk', risk: { 'RUB RUONIA': [-10, 0] }, ofzSpr01: -20 },
  ];
  for (const ofzView of ['basis', 'kbd']) {
    const model = { ...M, ofzView };
    const weights = [-5, -15];
    const positions = [position('fix', 'RUB'), { ...position('pk', 'RUB'), group: 'GOV', kind: 'PK',
      risk: { 'RUB OFZ': ofzView === 'basis' ? weights : [0, 0] }, ofzSprb: ofzView === 'kbd' ? weights : [0, 0] }];
    const calculate = prepare(model, facts, { factors, positions: [fixed, pk] });
    const scenario = { ...sim, par: 10, basis: 20, credit: 50 };
    const { rows: [a, b] } = calculate(positions, scenario, true);
    assert.deepEqual(parts(a), [-300, -1200, 0, 1900, 400], ofzView);
    assert.deepEqual(a.byBucket.cvx, [1900, 0, 0]);
    assert.deepEqual(parts(b), [-100, -400, 0, 2500, 2000], ofzView);
    assert.deepEqual(b.byBucket.b, [-100, -300, 0]);
    assert.deepEqual(b.byBucket.cvx, [1300, 1200, 0]);
    const onlyBasis = calculate(positions, { ...sim, basis: 20 }, true).rows[0];
    assert.deepEqual(parts(onlyBasis), [0, -1200, 0, 1200, 0]);
    assert.deepEqual(calculate(positions, { ...scenario, scope: 'USD' }, true).rows.map(parts), [[0, 0, 0, 0, 0], [0, 0, 0, 0, 0]]);
    assert.deepEqual(prepare(model, facts)(positions, scenario, false).rows.map(parts), calculate(positions, scenario, false).rows.map(parts));
    const proxy = { ...positions[0], curveProxy: 'КБД' };
    assert.deepEqual(parts(calculate([proxy], { ...sim, basis: 20 }, true).rows[0]), [0, 0, 0, 0, 0]);
    const ofzIn = { ...positions[0], group: 'GOV', kind: 'IN', quality: 'assumed' };
    assert.deepEqual(parts(calculate([ofzIn], { ...sim, basis: 20 }, true).rows[0]), [0, 0, 0, 0, 0]);
    const legacy = prepare(model, [{ ...facts[0], extra: {} }], { factors, positions: [fixed] });
    assert.deepEqual(parts(legacy(positions.slice(0, 1), { ...sim, basis: 20 }, true).rows[0]), [0, -2000, 0, 20_000, 18_000]);
  }
});

test('линейный режим и second_order: false дают только первый порядок', () => {
  const scenario = { ...sim, par: 10, credit: 5 };
  const pairs = [[runCredit, false], [prepare(M, [], { second_order: false, factors, positions: [credit] }), true]];
  for (const [calculate, enabled] of pairs) {
    const r = calculate([p], scenario, enabled).rows[0];
    assert.deepEqual(parts(r), [-370, 0, -150, null, -520]);
    assert.deepEqual(r.byBucket.cvx, [0, 0, 0]);
  }
});

test('сырые факты и гессиан переводятся в валюту отчёта, готовые суммы демо уже в ней', () => {
  const model = { ...M, repr: 'USD', fx: { USD: 4 }, curves: [{ id: 'USD SOFR', ccy: 'USD', kind: 'rate' }] };
  const facts = [{ id: p.id, cs01: -30, risk: { 'RUB RUONIA': [-10, -20], 'USD SOFR': [0, -7] } }];
  const scenario = { ...sim, par: 10, credit: 5 };
  const live = prepare(model, facts, { factors, positions: [credit] })([p], scenario, true).rows[0];
  assert.deepEqual(parts(live), [-92.5, 0, -37.5, 256.25, 126.25]);
  assert.deepEqual(live.byBucket.cvx, [129.6875, 126.5625, 0]);
  assert.deepEqual(parts(prepare(model, facts)([p], scenario, false).rows[0]), [-92.5, 0, -37.5, null, -130]);
  const demo = { ...p, risk: { ...p.risk, 'USD SOFR': [0, -7 / 4] }, cs01b: p.cs01b.map(v => v / 4) };
  assert.deepEqual(parts(prepare(model, [])([demo], scenario, false).rows[0]), [-17.5, 0, -37.5, null, -55]);
});

test('позиция без гаммы учитывается в покрытии, только если её фактор сдвинулся', () => {
  const calculate = prepare(M, [], { factors, positions: [{ ...credit, gamma: [], delta: [[3, -7], [5, 100]] }] });
  const active = calculate([p], { ...sim, par: 10 }, true);
  assert.equal(active.linearOnly, 1);
  assert.deepEqual(parts(active.rows[0]), [-70, 0, 0, null, -70]);
  assert.equal(calculate([p], { ...sim, scope: 'RUB', par: 10 }, true).linearOnly, 0);
  assert.equal(calculate([p], { ...sim, par: 10 }, false).linearOnly, 0);
});

test('параллельный RUB +50 совпадает с run_taylor по эталонной фикстуре Python', () => {
  const { payload, expected } = parity.cases.find(c => c.name === 'rub+50');
  const model = { ...M, buckets: payload.buckets, pillars: [1 / 12, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20] };
  const L = payload.positions.map(p => ({ id: p.position_id, cs01b: Array(model.buckets.length).fill(0) }));
  const { rows } = prepare(model, [], payload)(L, { ...sim, scope: 'RUB', par: 50 }, true);
  const near = (a, b) => assert.ok(Math.abs(a - b) <= parity.tolerance_rel * Math.max(1, Math.abs(b)), `${a} ≠ ${b}`);
  for (const [i, r] of rows.entries()) {
    const e = expected.positions[i];
    assert.equal(r.p.id, e.position_id);
    near(r.r, e.rates);
    near(r.cvx, e.convexity);
    near(r.t, e.total);
    near(r.byBucket.cvx.reduce((a, b) => a + b, 0), e.convexity);
  }
  near(rows.reduce((a, r) => a + r.t, 0), expected.totals._total.total);
});
