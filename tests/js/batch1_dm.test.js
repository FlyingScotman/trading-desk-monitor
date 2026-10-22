// Батч 1, п. 6 (R35): DM флоатера в истории — ядро по прогонам (dm) и оценка по цене MOEX за всю историю цены
// (dmPx, пунктир), K.dmSeries для карточки и C11; в демо у флоатера оба ряда.
//   node tests/js/batch1_dm.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}

t('без dmPx — как раньше: DM прогонов, иначе спред', () => {
  const { K } = load();
  const S = K.dmSeries({ dm: [null, 10], spr: [1, 2] });
  assert.strictEqual(S.length, 1); assert.deepStrictEqual(S[0].data, [null, 10]); assert.strictEqual(S[0].name, 'DM, б.п.');
  assert.deepStrictEqual(K.dmSeries({ spr: [1, 2] })[0].data, [1, 2]);
});

t('с dmPx — два ряда: ядро по прогонам и оценка по цене пунктиром, срез и цвета', () => {
  const { K } = load();
  const S = K.dmSeries({ dm: [null, null, 12], dmPx: [15, 13, 12] }, a => a.slice(1), ['#111', '#222']);
  assert.strictEqual(S.length, 2);
  assert.deepStrictEqual(S[0].data, [null, 12]); assert.deepStrictEqual(S[1].data, [13, 12]);
  assert.ok(/прогоны/.test(S[0].name) && /по цене \(оценка\)/.test(S[1].name));
  assert.strictEqual(S[1].lineStyle.type, 'dashed'); assert.strictEqual(S[0].color, '#111'); assert.strictEqual(S[1].color, '#222');
  // DM ядра нет ни в один день: пустой ряд той же длины, оценка видна
  assert.deepStrictEqual(K.dmSeries({ dmPx: [1, 2] })[0].data, [null, null]);
});

t('демо: у флоатера DM прогонов только в последние дни, оценка по цене — за всю историю', () => {
  const { M } = load();
  const flt = Object.values(M.pos).find(p => p.cpn && p.cpn.type === 'float' && p.group !== 'DER');
  const fix = Object.values(M.pos).find(p => p.cpn && p.cpn.type === 'fix' && p.group !== 'DER');
  const H = M.posHistory(flt.id);
  assert.strictEqual(H.dmPx.length, H.dates.length); assert.ok(H.dmPx[0] != null);
  assert.strictEqual(H.dm[0], null); assert.strictEqual(H.dm[H.dm.length - 1], H.dmPx[H.dmPx.length - 1]);
  assert.ok(!('dmPx' in M.posHistory(fix.id)));
});

console.log(n + ' tests passed');
