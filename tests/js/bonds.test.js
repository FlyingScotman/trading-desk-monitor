// Облигации в мониторе (prototypes/desk-util.js): спред к выбранной кривой, срок до оферты, агрегаты по выпуску,
// согласованность демо-истории с данными дня.   node tests/js/bonds.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

const ctx = { window: {}, URLSearchParams };
vm.createContext(ctx);
vm.runInContext(P('data.js'), ctx);
vm.runInContext(P('desk-util.js'), ctx);
const D = ctx.window.DESK, U = D.util;
const by = n => D.positions.find(p => p.name === n);
const near = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg}: ${a} vs ${b}`);
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

t('базовая кривая валюты', () => {
  assert.strictEqual(U.baseCurve('RUB'), 'RUB OFZ');      // гос. кривая
  assert.strictEqual(U.baseCurve('USD'), 'USD SOFR');     // гос. кривой нет — первая кривая валюты
  assert.strictEqual(U.baseCurve('JPY'), null);
});

t('спред к кривой = доходность − ставка на сроке', () => {
  const p = by('Роснефть 002Р-13');
  near(U.spreadTo(p, 'RUB OFZ'), (p.ytm - U.curveRate('RUB OFZ', p.tenorY)) * 100, 1e-9, 'OFZ');
  near(U.spreadTo(p, 'base'), U.spreadTo(p, 'RUB OFZ'), 1e-12, 'base = OFZ');
  near(U.spreadTo(p, 'RUB RUONIA'), p.spreadBp, 0.06, 'демо: расчётный спред RUB-корпората — к RUONIA');
  assert.strictEqual(p.spreadCurve, 'RUB RUONIA');
  assert.strictEqual(U.spreadTo(p, 'own'), p.spreadBp);
  assert.ok(U.spreadTo(p, 'RUB RUONIA') !== U.spreadTo(p, 'RUB OFZ'));
});

t('оферта: срок и спред — к оферте', () => {
  const p = by('МТС 002Р-08');
  assert.strictEqual(p.yieldDate, '2027-11-24');
  near(U.yieldTenor(p), 1.1143, 1e-3, 'срок до оферты');
  assert.ok(p.tenorY > 3);
  near(U.spreadTo(p, 'RUB OFZ'), (p.ytm - U.curveRate('RUB OFZ', U.yieldTenor(p))) * 100, 1e-9, 'спред к оферте');
});

t('флоатер, другая валюта, не облигация → null', () => {
  assert.strictEqual(U.spreadTo(by('ОФЗ 29010'), 'RUB OFZ'), null);
  assert.strictEqual(U.spreadTo(by('ОФЗ 29010'), 'own'), by('ОФЗ 29010').spreadBp);   // DM
  assert.strictEqual(U.spreadTo(by('Газпром ЗО27-1-Д'), 'RUB OFZ'), null);
  assert.ok(U.spreadTo(by('Газпром ЗО27-1-Д'), 'base') != null);
  assert.strictEqual(U.spreadTo(D.positions.find(p => p.instrType === 'IRS'), 'RUB OFZ'), null);
});

t('вчера: Δ спреда к кривой согласован с изменениями дня', () => {
  const p = by('Сегежа 003Р-02R');   // SPREAD_MOVES['Сегежа'] = 38: доходность выросла на Δ RUONIA + 38 б.п.
  near(U.spreadTo(p, 'RUB RUONIA') - U.spreadTo(p, 'RUB RUONIA', true), 38, 0.05, 'Δ спреда к своей кривой (RUONIA)');
  near(p.quote.zSpread - p.quote.prevZSpread, 38, 1e-9, 'Δ расч. спреда');
});

t('агрегаты по выпуску и средние по |MV|', () => {
  const a = by('ОФЗ 26244'), b = Object.assign({}, a, { id: 'dup', book: 'RATES', notional: -a.notional / 2, mvRub: -a.mvRub / 2, dv01: -a.dv01 / 2, cs01: 0 });
  const rows = U.bondRows([a, b, by('ОФЗ 26245')]);
  assert.strictEqual(rows.length, 2);
  const r = rows.find(x => x.p.isin === a.isin);
  assert.deepStrictEqual(Array.from(r.books).sort(), ['OFZ', 'RATES']);
  near(r.mvRub, a.mvRub / 2, 1e-6, 'MV нетто');
  assert.strictEqual(U.bondRows([a, b], p => p.book).length, 2);         // группировка по книге — строка на (книга, выпуск)
  near(U.wavg([{ v: 10, mvRub: -1 }, { v: 20, mvRub: 3 }, { v: null, mvRub: 5 }], x => x.v), 17.5, 1e-12, 'wavg');
  assert.strictEqual(U.wavg([], x => x.v), null);
});

t('демо-история: последняя точка = данные дня, предпоследняя = quote.prev*', async () => {
  for (const p of D.positions.filter(x => x.instrType === 'BOND')) {
    const h = D.demo.bondHistory(p, p.spreadCurve), s = h.series, L = s[s.length - 1], Y = s[s.length - 2];
    assert.strictEqual(s.length, D.demo.histDates.length);
    assert.strictEqual(L.date, D.asOf);
    near(L.ytm, p.ytm, 1e-4, p.name + ' ytm');
    near(L.clean_price, p.price, 1e-4, p.name + ' price');
    assert.strictEqual(L.face_amount, p.notional);
    near(Y.ytm, p.quote.prevYtm, 1e-12, p.name + ' prevYtm');
    if (p.couponType !== 'float') near(L.spread_to_curve_bp, U.spreadTo(p, p.spreadCurve), 0.01, p.name + ' spread');
  }
});

t('normHistory: столбцы и ошибки', () => {
  const h = U.normHistory({ instrument_id: 'X', curve: 'RUB OFZ', series: [{ date: '2026-10-13', clean_price: 99, ytm: 14, spread_to_curve_bp: 50, face_amount: null }] });
  assert.deepStrictEqual(Array.from(h.dates), ['2026-10-13']);
  assert.deepStrictEqual(Array.from(h.curveSpread), [50]);
  assert.deepStrictEqual(Array.from(h.face), [null]);
  assert.strictEqual(U.normHistory({ series: [], error: 'HTTP 400' }).error, 'HTTP 400');
});

console.log(`bonds: ${n} тестов OK`);
