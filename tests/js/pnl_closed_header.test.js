// Итог MTD / YTD шапки включает позиции, закрытые раньше в периоде (M.pnlClosed), по тому же фильтру шапки (TDM#568, #566):
// книга, юрлицо, деск, валюта, портфель; в книге типа HTM портфель закрытой позиции — HTM. Синтетика scope_cases.json.
//   node tests/js/pnl_closed_header.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const CASES = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'scope_cases.json'), 'utf8'));
const eq = (a, b, m) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), m);
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  const q = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true, q,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(sel) { return q[sel] || (q[sel] = el()); }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, remove() {}, insertAdjacentHTML() {} };
}
function load(repr, closed) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem(k) { return repr && k === 'concepts-repr' ? repr : null; }, setItem() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, fetch: () => new Promise(() => {}) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  const M = ctx.window.MOCK;
  // позиции и книги случая вместо демо-набора — до загрузки kit.js (список портфелей он строит при загрузке)
  M.book = Object.fromEntries(Object.entries(CASES.books).map(([id, b]) => [id, { id, name: id, entity: b.entity, desk: b.desk, type: b.type, portfolio: b.type === 'HTM' ? 'HTM' : null }]));  // portfolio — как BOOKS сервера
  M.positions = CASES.positions.map(p => ({ id: p.id, book: p.book, ccy: p.ccy, portfolio: p.portfolio, entity: CASES.books[p.book].entity, desk: CASES.books[p.book].desk, pnl: { total: p.pnl } }));
  M.positions.forEach(p => { p.pnlMtd = p.pnl.total * 10; p.pnlYtd = p.pnl.total * 100; p.risk = {}; p.dv01ByCurve = {}; });
  M.pnlClosed = JSON.parse(JSON.stringify(closed || []));
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M, K: ctx.window.KIT };
}
const RESET = { entity: null, desk: null, books: null, ccy: null, portsOff: null };
const CLOSED = [
  { positionId: 'C1', book: 'B1', ccy: 'RUB', portfolio: 'Торговый', entity: 'E1', desk: 'D1', issuer: 'I1', mtd: 50, ytd: 500 },  // книга HTM: портфель HTM, чужое поле не в счёт
  { positionId: 'C2', book: 'B2', ccy: 'RUB', portfolio: 'Торговый', entity: 'E1', desk: 'D1', issuer: 'I1', mtd: 20, ytd: 200 },
  { positionId: 'C3', book: 'B3', ccy: 'USD', portfolio: null, entity: 'E2', desk: 'D2', issuer: 'I2', mtd: 7, ytd: 70 },
  { positionId: 'C4', book: 'B2', ccy: 'RUB', portfolio: null, entity: 'E1', desk: 'D1', issuer: 'I1', mtd: 3, ytd: 30 },
];
function setup(repr) {
  const { M, K } = load(repr, CLOSED);
  const sum = (key) => K.pnlPeriodList(K.list()).reduce((s, p) => s + p[key], 0);
  return { M, K, sum };
}
const open = (ids, k) => CASES.positions.filter(p => ids.includes(p.id)).reduce((s, p) => s + p.pnl * k, 0);
const CASE = [
  ['без фильтра', {}, ['P1', 'P2', 'P3', 'P4', 'P5', 'P6', 'P7', 'P8'], ['C1', 'C2', 'C3', 'C4']],
  ['портфель HTM снят: закрытая позиция книги HTM уходит с ним', { portsOff: ['HTM'] }, ['P4', 'P5', 'P6', 'P7', 'P8'], ['C2', 'C3', 'C4']],
  ['снят «Торговый»: закрытая C2 уходит, C1 (книга HTM) остаётся', { portsOff: ['Торговый'] }, ['P1', 'P2', 'P3', 'P5', 'P7', 'P8'], ['C1', 'C3', 'C4']],
  ['снято «—»: закрытая без портфеля уходит', { portsOff: ['—'] }, ['P1', 'P2', 'P3', 'P4', 'P6', 'P8'], ['C1', 'C2']],
  ['книга B2', { books: ['B2'] }, ['P3', 'P4', 'P5'], ['C2', 'C4']],
  ['валюта USD', { ccy: 'USD' }, ['P4', 'P7', 'P8'], ['C3']],
  ['юрлицо E2', { entity: 'E2' }, ['P6', 'P7', 'P8'], ['C3']],
  ['деск D1', { desk: 'D1' }, ['P1', 'P2', 'P3', 'P4', 'P5'], ['C1', 'C2', 'C4']],
  ['ничего не подошло', { entity: 'E1', ccy: 'EUR' }, [], []],
];
t('MTD и YTD = открытые под фильтром + закрытые под тем же фильтром', () => {
  const { K, sum } = setup();
  for (const [name, flt, ids, cids] of CASE) {
    K.setFilter({ ...RESET, ...flt });
    eq(K.closedList().map(c => c.id).sort(), cids, name);
    const cl = CLOSED.filter(c => cids.includes(c.positionId));
    assert.strictEqual(sum('pnlMtd'), open(ids, 10) + cl.reduce((s, c) => s + c.mtd, 0), 'MTD ' + name);
    assert.strictEqual(sum('pnlYtd'), open(ids, 100) + cl.reduce((s, c) => s + c.ytd, 0), 'YTD ' + name);
  }
});
t('портфель, которого нет в прогоне (все позиции закрыты), снят целиком: закрытые под ним скрыты', () => {
  const { K, M } = setup();
  M.pnlClosed = CLOSED.concat([{ positionId: 'C5', book: 'B3', ccy: 'RUB', portfolio: 'Старый2', entity: 'E2', desk: 'D2', issuer: 'I2', mtd: 9, ytd: 90 }]);
  K.setFilter({ ...RESET });
  assert.ok(K.closedList().some(c => c.id === 'C5'));
  K.setFilter({ ...RESET, portsOff: ['Старый2'] });
  assert.ok(!K.closedList().some(c => c.id === 'C5') && K.closedList().length === 4);
});
t('эмитент: закрытые только своего эмитента (C13)', () => {
  const { K } = setup();
  K.setFilter({ ...RESET });
  eq(K.closedList('I2').map(c => c.id), ['C3']);
  eq(K.closedList('I1').map(c => c.id).sort(), ['C1', 'C2', 'C4']);
});
t('закрытая позиция годится для разбора по щелчку: поля есть, карточки нет', () => {
  const { K } = setup();
  const c = K.closedList()[0];
  assert.ok(c.searchText && c.bookName && c.issuerName && c.groupLabel && c.closed);
  assert.strictEqual(K.pnlPeriodList(null).length, K.list().length + 4);
});
t('валюта отчёта USD: закрытые позиции пересчитаны вместе с открытыми, итог не смешивает валюты', () => {
  const rub = setup(), usd = setup('USD');
  assert.ok(usd.M.fx.USD > 1);
  for (const [name, flt] of CASE.map(c => [c[0], c[1]])) {
    rub.K.setFilter({ ...RESET, ...flt }); usd.K.setFilter({ ...RESET, ...flt });
    for (const key of ['pnlMtd', 'pnlYtd']) {
      const a = rub.sum(key) / usd.M.fx.USD, b = usd.sum(key);
      assert.ok(Math.abs(a - b) < 1e-6 * Math.max(1, Math.abs(a)), name + ' ' + key + ': ' + a + ' vs ' + b);
    }
  }
});
console.log(n + ' tests');
