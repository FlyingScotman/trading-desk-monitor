// R18: кривая ОФЗ = RUONIA + базис ОФЗ–RUONIA. Прототипный движок сценариев (desk-util.js runScenario) двигает фактор
// базиса как desk.scenario.definition.curve_moves: сдвиг одной RUONIA двигает и кривую ОФЗ (базис на месте), сдвиг
// RUB OFZ — это базис; параллельный сдвиг всех кривых ставок даёт ОФЗ тот же PnL, что прежний сдвиг кривой ОФЗ.
//   node tests/js/ofz_basis_r18.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const same = (a, b) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b));  // массивы из vm: другой realm
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const B = 'RUB OFZ-RUONIA', v = x => new Array(11).fill(x);

// демо + одна ОФЗ с риском прогона R18: DV01 по RUONIA и тот же по базису, гамма по обоим факторам
function load() {
  const ctx = { window: {}, URLSearchParams, console };
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  const D = ctx.window.DESK;
  D.CURVES[B] = { name: 'Базис ОФЗ–RUONIA', ccy: 'RUB', kind: 'basis', pts: v(0.5) };
  D.CURVE_MOVES[B] = v(2);
  const dv = [0, 0, 0, 0, -100, -300, 0, 0, 0, 0, 0];
  D.positions = [{ id: 'OFZ1', book: Object.keys(D.BOOKS)[0], instrType: 'BOND', ccy: 'RUB', mvRub: 1e6, dv01: -400, cs01: 0,
    ofzSpr01: -390, risk: { 'RUB RUONIA': dv.slice(), [B]: dv.slice() }, fxDelta: {},
    gamma: [['IR|RUB RUONIA|3Y', 'IR|RUB RUONIA|3Y', 0.02], [`IR|${B}|3Y`, `IR|${B}|3Y`, 0.02], ['IR|RUB RUONIA|3Y', `IR|${B}|3Y`, 0.02]] }];
  D.funding = []; D.scenarioResults = {};
  vm.runInContext(P('desk-util.js'), ctx);
  return ctx.window.DESK;
}
const D = load(), U = D.util;
const run = curves => U.runScenario({ id: 'x', curves }).byPosition[0];

t('кривые ставок без базиса', () => {
  same(U.rateCurves([B, 'RUB RUONIA']), ['RUB RUONIA']);
  assert.ok(U.isBasis(B) && !U.isBasis('RUB OFZ'));
});
t('curveMoves: RUONIA двигает кривую ОФЗ, RUB OFZ — базис, базис важнее RUB OFZ', () => {
  let m = U.curveMoves({ curves: { 'RUB RUONIA': 10 } });
  same([m['RUB OFZ'], m[B]], [v(10), v(0)]);
  m = U.curveMoves({ curves: { 'RUB OFZ': 30, 'RUB RUONIA': 10 } });
  same([m['RUB OFZ'], m[B]], [v(30), v(20)]);
  m = U.curveMoves({ curves: { [B]: 5, 'RUB OFZ': 99 } });
  same([m['RUB OFZ'], m[B]], [v(5), v(5)]);
});
t('ОФЗ: RUONIA + базис вместе = прежний сдвиг кривой ОФЗ (с гаммой)', () => {
  const a = run({ 'RUB RUONIA': 50, 'RUB OFZ': 50, 'RUB KEY': 50 });  // параллельный: базис 0
  assert.strictEqual(a.rates, -400 * 50);
  const b = run({ 'RUB OFZ': 30, 'RUB RUONIA': 10 });  // кривая ОФЗ +30 = RUONIA +10 и базис +20
  assert.ok(Math.abs(b.rates - (-400 * 30)) < 1e-9, b.rates);
  // гамма прогона — подстановка k = r + s в γ_kk (split_hessian): γ_rr = γ_ss = γ_rs = γ_kk, итог 0.5·γ_kk·Δk²
  assert.ok(Math.abs(b.convexity - 0.5 * 0.02 * 30 * 30) < 1e-9, b.convexity);
  assert.strictEqual(run({ [B]: 25 }).rates, -400 * 25);  // один базис
  assert.strictEqual(run({ 'RUB OFZ': 25 }).rates, -400 * 25);  // одна кривая ОФЗ — тоже базис
});
t('сводная гамма к параллельному сдвигу ставок без базиса', () => {
  assert.ok(Math.abs(D.positions[0].gammaIr - 0.02) < 1e-12, D.positions[0].gammaIr);
});
console.log(`${n} tests passed`);
