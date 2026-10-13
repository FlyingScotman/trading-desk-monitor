// Точность сценарного движка (prototypes/desk-util.js) против точной переоценки облигации.
//   node tests/js/scenario_engine.test.js        (запускается и из python -m unittest, если есть node)
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');

const BUCKETS = ['1M', '3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];
const PILLARS = [1 / 12, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20];
const I5 = BUCKETS.indexOf('5Y');

// Точная цена: 5-летняя облигация, купон 9% s.a., дисконт по доходности y (десятичная), цена на 100.
function price(y) { let p = 0; for (let k = 1; k <= 10; k++) p += (k === 10 ? 104.5 : 4.5) * Math.pow(1 + y / 2, -k); return p; }

function load(positions, fx) {
  const ctx = { window: { DESK: {
    BUCKETS, PILLARS, FX: fx, positions, funding: [], SCENARIOS: [],
    CURVES: { 'RUB OFZ': { ccy: 'RUB', pts: [] }, 'USD SOFR': { ccy: 'USD', pts: [] } },
  } } };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-util.js'), 'utf8'), ctx);
  return ctx.window.DESK;
}

// позиция облигации: чувствительности считаем численно от точной цены
function bondPos(id, ccy, curve, notional, fxRate, y0, withGamma) {
  const P = price(y0), mv = notional * P / 100, mvRub = mv * fxRate;
  const h = 1e-4;
  const dP = (price(y0 + h) - price(y0 - h)) / 2;                 // на 1 б.п.
  const d2P = price(y0 + h) + price(y0 - h) - 2 * P;               // на (1 б.п.)²
  const dv01 = notional / 100 * dP * fxRate;
  const convexity = d2P / (h * h) / P;                             // в годах²
  const risk = {}; risk[curve] = BUCKETS.map((_, i) => i === I5 ? dv01 : 0);
  const p = { id, book: 'B', instrType: 'BOND', segment: 'Корп', ccy, mvRub, dv01, cs01: dv01, convexity, risk,
    fxDelta: ccy === 'RUB' ? {} : { [ccy]: mv }, spreadBp: 200 };
  if (withGamma) {
    const G = notional / 100 * d2P * fxRate, f = `IR|${curve}|5Y`;
    p.gamma = [['CS', 'CS', G], ['CS', f, G], [f, f, G]];
    if (ccy !== 'RUB') p.gamma.push(['CS', `FX|${ccy}`, dv01 / 100], [`FX|${ccy}`, f, dv01 / 100]);
  }
  return { p, exact: (dr, ds, dfx) => notional * price(y0 + (dr + ds) * 1e-4) / 100 * fxRate * (1 + dfx / 100) - mvRub };
}

let failures = 0;
function check(name, fn) { try { fn(); console.log('ok  ', name); } catch (e) { failures++; console.log('FAIL', name, '\n     ', e.message); } }

const cases = [
  ['RUB-облигация, ставки +300, спреды +200', 'RUB', 'RUB OFZ', 1, 300, 200, 0],
  ['RUB-облигация, ставки −250', 'RUB', 'RUB OFZ', 1, -250, 0, 0],
  ['USD-облигация, ставки +150, спреды +300, рубль −30%', 'USD', 'USD SOFR', 80, 150, 300, 30],
  ['ставки +400, спреды +100, рубль −20% (граница точности)', 'USD', 'USD SOFR', 80, 400, 100, 20],
];
for (const withGamma of [false, true]) {
  for (const [name, ccy, curve, fxRate, dr, ds, dfx] of cases) {
    check(`${name} — ${withGamma ? 'матрица гамм' : 'из выпуклости'}`, () => {
      const b = bondPos('X', ccy, curve, 1e9, fxRate, 0.14, withGamma);
      const D = load([b.p], { RUB: 1, USD: fxRate });
      const sc = { curves: { [curve]: dr }, spreads: { _all: ds, includeGov: true }, fx: dfx ? { USD: dfx } : undefined };
      const r = D.util.runScenario(sc).byPosition[0];
      const exact = b.exact(dr, ds, dfx);
      const first = r.rates + r.spread + r.fx;
      const err1 = Math.abs(first - exact), err2 = Math.abs(r.total - exact);
      console.log(`      точно ${(exact / 1e6).toFixed(1)} млн; 1-й порядок ${(first / 1e6).toFixed(1)} (ошибка ${(100 * err1 / b.p.mvRub).toFixed(2)}% MV);`
        + ` 2-й порядок ${(r.total / 1e6).toFixed(1)} (ошибка ${(100 * err2 / b.p.mvRub).toFixed(2)}% MV)`);
      assert(err2 < 0.2 * err1, `второй порядок должен убрать ≥80% ошибки: ${err2} vs ${err1}`);
      assert(err2 / b.p.mvRub < 0.012, `ошибка ${(100 * err2 / b.p.mvRub).toFixed(2)}% MV слишком велика`);
      assert.strictEqual(r.secondOrderSrc, withGamma ? 'gamma' : 'convexity');
    });
  }
}

check('«2022» (+1300 б.п. по доходности): вне области точности — сценарий помечен, нужна полная переоценка', () => {
  const b = bondPos('X', 'USD', 'USD SOFR', 1e9, 80, 0.14, true);
  const D = load([b.p], { RUB: 1, USD: 80 });
  const r = D.util.runScenario({ curves: { 'USD SOFR': 900 }, spreads: { _all: 400, includeGov: true }, fx: { USD: 35 } });
  const exact = b.exact(900, 400, 35), x = r.byPosition[0];
  console.log(`      точно ${(exact / 1e6).toFixed(1)} млн; 2-й порядок ${(x.total / 1e6).toFixed(1)}`
    + ` (ошибка ${(100 * Math.abs(x.total - exact) / b.p.mvRub).toFixed(2)}% MV); сдвиг доходности ${x.dyEff.toFixed(0)} б.п.`);
  assert(r.accuracyWarning && r.accuracyWarning.positions === 1, 'должно быть предупреждение о точности');
  assert(Math.abs(r.maxDyBp - 1300) < 1);
  // в пределах области точности предупреждения нет
  assert.strictEqual(D.util.runScenario({ curves: { 'USD SOFR': 200 }, fx: { USD: 35 } }).accuracyWarning, null);
});

check('матрица гамм и выпуклость дают одно и то же на согласованных данных', () => {
  const a = bondPos('A', 'USD', 'USD SOFR', 1e9, 80, 0.14, false), g = bondPos('G', 'USD', 'USD SOFR', 1e9, 80, 0.14, true);
  const D = load([a.p, g.p], { RUB: 1, USD: 80 });
  const r = D.util.runScenario({ curves: { 'USD SOFR': 120 }, spreads: { _all: 80, includeGov: true }, fx: { USD: 12 } }).byPosition;
  assert(Math.abs(r[0].convexity - r[1].convexity) < 1e-6 * Math.abs(r[0].convexity) + 1, `${r[0].convexity} vs ${r[1].convexity}`);
  assert(Math.abs(r[0].fxCross - r[1].fxCross) < 1e-6 * Math.abs(r[0].fxCross) + 1, `${r[0].fxCross} vs ${r[1].fxCross}`);
});

check('ключевые ставки: шок только на чужом пилларе не даёт гаммы', () => {
  const b = bondPos('X', 'RUB', 'RUB OFZ', 1e9, 1, 0.14, true);
  const D = load([b.p], { RUB: 1 });
  const curve = BUCKETS.map((_, i) => i === I5 ? 0 : 500);
  const r = D.util.runScenario({ curves: { 'RUB OFZ': curve } }).byPosition[0];
  assert.strictEqual(r.convexity, 0);
  assert.strictEqual(r.total, 0);
});

check('предупреждение о точности не срабатывает на позициях с ногами разного знака (XCCY, FX-форвард)', () => {
  const p = { id: 'XC', book: 'B', instrType: 'XCCY', segment: 'Деривативы', ccy: 'USD', mvRub: 1e6, dv01: 10, cs01: 0,
    risk: { 'USD SOFR': BUCKETS.map((_, i) => i === I5 ? -50000 : 0), 'RUB OFZ': BUCKETS.map((_, i) => i === I5 ? 50010 : 0) }, fxDelta: {} };
  const D = load([p], { RUB: 1, USD: 80 });
  const r = D.util.runScenario({ curves: { 'RUB OFZ': 200 } });
  assert.strictEqual(r.accuracyWarning, null, `ложное предупреждение: сдвиг ${r.maxDyBp}`);
  assert(r.maxDyBp <= 200 + 1e-9);
});

check('демо-набор: умеренные пресеты без предупреждения, «2022» — с предупреждением', () => {
  const ctx = { window: {} }; vm.createContext(ctx);
  for (const f of ['data.js', 'desk-util.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8'), ctx);
  const D = ctx.window.DESK, run = id => D.util.runScenario(D.SCENARIOS.find(s => s.id === id));
  for (const id of ['par+100', 'par-100', 'keyhike', 'bullsteep', 'bearflat', 'rubdep']) assert.strictEqual(run(id).accuracyWarning, null, id + ': ' + run(id).maxDyBp);
  assert(run('stress22').accuracyWarning, 'stress22 должен быть помечен');
});

check('FX-позиция линейна: кросс-эффекта нет, итог точный', () => {
  const p = { id: 'F', book: 'B', instrType: 'FX', segment: 'FX', ccy: 'USD', mvRub: 0, dv01: 0, cs01: 0, risk: {}, fxDelta: { USD: 1e6 } };
  const D = load([p], { RUB: 1, USD: 80 });
  const r = D.util.runScenario({ fx: { USD: 25 } }).byPosition[0];
  assert.strictEqual(r.fxCross, 0);
  assert(Math.abs(r.total - 1e6 * 80 * 0.25) < 1e-6);
});

check('демо-набор: сценарии считаются, гаммы есть, итог = сумма компонент', () => {
  const ctx = { window: {} }; vm.createContext(ctx);
  for (const f of ['data.js', 'desk-util.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8'), ctx);
  const D = ctx.window.DESK;
  assert(D.positions.filter(p => p.gamma && p.gamma.length).length > 50);
  for (const sc of D.SCENARIOS) {
    const r = D.util.runScenario(sc);
    r.byPosition.forEach(x => assert(Math.abs(x.rates + x.spread + x.fx + x.convexity + x.fxCross - x.total) < 1e-3));
    assert(Number.isFinite(r.total));
  }
});

if (failures) { console.log(`\n${failures} провал(ов)`); process.exit(1); }
console.log('\nвсе проверки пройдены');
