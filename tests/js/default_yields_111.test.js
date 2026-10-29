// #111: дефолтные и дистрессные выпуски (KIT.distress, prototypes/concepts/lib/kit.js) — доходность MOEX вида
// «29 574 %» не показывается числом и не входит в медианы и шкалы. Только синтетика.
//   node --test tests/js/default_yields_111.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { document, URLSearchParams, URL, console, setTimeout, location: { href: 'http://x/', search: '', hostname: '127.0.0.1', protocol: 'file:' },
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return ctx;
}
const CTX = load(), K = CTX.KIT;

test('#111: доходность MOEX от 50 %, G-спред шире 2000 б.п. и рейтинг дефолта — пометка', () => {
  assert.equal(K.distress({ ytmMoex: 29574, ytm: null, spread: null }).kind, 'distress');
  assert.equal(K.distress({ ytm: 50 }).kind, 'distress');
  assert.equal(K.distress({ ytm: 49.9, spread: 2100 }).kind, 'distress');
  assert.equal(K.distress({ ytm: 18, gspread: 2500 }).kind, 'distress');  // позиция: G-спред в gspread
  for (const r of ['RD', 'SD', 'D']) assert.equal(K.distress({ ytm: 15, rating: r }).kind, 'default');
});

test('#111: обычный выпуск, нет данных и граница 2000 б.п. — без пометки', () => {
  for (const b of [{ ytm: 16.5, spread: 250, rating: 'A' }, { ytm: 49.99, spread: 2000 }, { ytm: null, spread: null }, {}, null, { ytm: -3, spread: -150 }])
    assert.equal(K.distress(b), null, JSON.stringify(b));
});

test('#111: пометка — текстом, без доходности числом; подсказка — почему', () => {
  const d = K.distress({ ytmMoex: 29574 });
  const tag = K.distressTag(d);
  assert.match(tag, />дистресс</);
  assert.match(tag, /не входит в медианы и шкалы/);
  assert.match(d.why, /доходность MOEX/);
  assert.doesNotMatch(tag.replace(/title="[^"]*"/, ''), /29/);
});

test('#111: демо-набор рынка правилом не задет (медианы и движения демо прежние)', () => {
  const M = CTX.MOCK;
  assert.ok(M.universeRub.length > 0 && M.marketMovers.length > 0);
  assert.deepEqual([...M.universeRub.filter(b => K.distress(b)).map(b => b.name)], []);
  assert.deepEqual([...M.marketMovers.filter(b => K.distress(b)).map(b => b.name)], []);
});
