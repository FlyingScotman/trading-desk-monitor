// Одно правило «книга / позиция под фильтром шапки» (TDM#566, #496): тот же набор позиций и фильтров, что у сервера
// (backend/tests/test_scope_566.py, tests/js/fixtures/scope_cases.json) — клиентский предикат K.scope даёт те же позиции,
// портфель берётся из поля позиции, строки без позиции и ОВП идут по тому же правилу книги.
//   node tests/js/scope_566.test.js
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
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} },
    getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, fetch: () => new Promise(() => {}) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  const M = ctx.window.MOCK;
  // позиции и книги случая вместо демо-набора — до загрузки kit.js (список портфелей он строит при загрузке)
  M.book = Object.fromEntries(Object.entries(CASES.books).map(([id, b]) => [id, { id, name: id, entity: b.entity, desk: b.desk, type: b.type, portfolio: b.type === 'HTM' ? 'HTM' : null }]));  // portfolio — как BOOKS сервера
  M.positions = CASES.positions.map(p => ({ id: p.id, book: p.book, ccy: p.ccy, portfolio: p.portfolio, entity: CASES.books[p.book].entity, desk: CASES.books[p.book].desk, pnl: { total: p.pnl } }));
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M, K: ctx.window.KIT };
}
const RESET = { entity: null, desk: null, books: null, ccy: null, portsOff: null };

t('те же позиции под каждым фильтром, что и на сервере; итог = сумма списка', () => {
  const { K } = load();
  for (const c of CASES.filters) {
    K.setFilter({ ...RESET, ...c.filter });
    const L = K.list();
    eq(L.map(p => p.id).sort(), [...c.expect].sort(), c.name);
    const want = CASES.positions.filter(p => c.expect.includes(p.id)).reduce((s, p) => s + p.pnl, 0);
    assert.strictEqual(L.reduce((s, p) => s + p.pnl.total, 0), want, c.name);
  }
});

t('строки без позиции — те же ожидания, что в общей фикстуре: книга HTM даёт HTM, у остальных книг фильтр портфелей не действует', () => {
  const { K } = load();
  for (const c of CASES.filters) {
    K.setFilter({ ...RESET, ...c.filter });
    eq(CASES.rows.filter(r => K.rowIn(r)).map(r => r.id).sort(), [...c.expectRows].sort(), c.name);
  }
});

t('портфель позиции — поле сервера, клиент его не пересчитывает', () => {
  const { M, K } = load();
  for (const p of M.positions) assert.strictEqual(K.scope.portOf(p), CASES.positions.find(x => x.id === p.id).portfolio || '—');
  // поле важнее чего-либо ещё в позиции: у позиции HTM-книги с «чужим» полем отчёта клиент берёт то, что пришло
  assert.strictEqual(K.scope.portOf({ portfolio: 'HTM', book: 'B2' }), 'HTM');
  assert.strictEqual(K.scope.portOf({}), '—');
});

t('строка без позиции (остатки, строки отчёта): правило книги шапки, без портфеля — «—»', () => {
  const { K } = load();
  K.setFilter({ ...RESET, portsOff: ['HTM'] });
  assert.ok(K.rowIn({ book: 'B2', ccy: 'RUB' }));
  K.setFilter({ ...RESET, portsOff: ['—'] });  // книга не HTM: портфеля у строки нет, фильтр к ней не применяется
  assert.ok(K.rowIn({ book: 'B2', ccy: 'RUB' }));
  K.setFilter({ ...RESET, portsOff: ['Торговый'] });
  assert.ok(!K.rowIn({ book: 'B2', ccy: 'RUB', portfolio: 'Торговый' }) && K.rowIn({ book: 'B2', ccy: 'RUB', portfolio: 'Другой' }));
  K.setFilter({ ...RESET, entity: 'E2' });
  assert.ok(K.rowIn({ book: 'B3', ccy: 'RUB' }) && !K.rowIn({ book: 'B2', ccy: 'RUB' }));
  K.setFilter({ ...RESET, desk: 'D1', books: ['B1'] });
  assert.ok(K.rowIn({ book: 'B1', ccy: 'RUB' }) && !K.rowIn({ book: 'B2', ccy: 'RUB' }));
  K.setFilter({ ...RESET, ccy: 'USD' });
  assert.ok(K.rowIn({ book: 'B1', ccy: 'USD' }) && !K.rowIn({ book: 'B1', ccy: 'RUB' }));
  // явные юрлицо и деск строки главнее справочника
  K.setFilter({ ...RESET, entity: 'E2' });
  assert.ok(K.rowIn({ book: 'B1', entity: 'E2', ccy: 'RUB' }));
});

t('ОВП: строка книги и часть отчёта идут по тому же правилу книги; валюта — валюта строки ОВП', () => {
  const { M, K } = load();
  M.ocp = { basis: 'derived', byBook: [
    { entity: 'E1', book: 'B1', books: ['B1'], ccy: 'USD', amount: 1, source: 'derived' },
    { entity: 'E1', book: 'B2', books: ['B2'], ccy: 'CNY', amount: 2, source: 'derived' },
    { entity: 'E2', book: 'B3', books: ['B3'], ccy: 'USD', amount: 4, source: 'derived' }], notes: [], gaps: [], books: {}, sourceNames: {} };
  const lines = f => { K.setFilter({ ...RESET, ...f }); return K.ocpScope().lines.map(x => x.book).sort(); };
  eq(lines({}), ['B1', 'B2', 'B3']);
  eq(lines({ entity: 'E1' }), ['B1', 'B2']);
  eq(lines({ books: ['B3'] }), ['B3']);
  eq(lines({ ccy: 'USD' }), ['B1', 'B3']);  // валюта строки ОВП, не позиции
  // те же книги, что у позиций под тем же фильтром
  K.setFilter({ ...RESET, entity: 'E1', desk: 'D1' });
  eq([...new Set(K.list().map(p => p.book))].sort(), K.ocpScope().lines.map(x => x.book).sort());
  K.setFilter({ ...RESET, ccy: 'USD' });
  assert.ok(/валюты/.test(K.ocpScope().ccyNote) && !K.ocpScope().notes.some(x => /валюты/.test(x)), 'смысл валюты — не потеря ОВП');
  assert.ok(!/не вся/.test(K.ocpNote()) && /валюта/.test(K.ocpNote()));
});

t('«Сбросить фильтр книг» сбрасывает и снятые портфели; K.top({ filter: false }) подписан', () => {
  const kit = P('concepts/lib/kit.js');
  assert.ok(/label: 'Сбросить фильтр книг', run: \(\) => K\.setFilter\(\{[^}]*portsOff: null/.test(kit));
  assert.ok(/opt\.filter === false/.test(kit));
  for (const f of ['c14-day-status', 'c15-cash', 'c20-trades']) assert.ok(/K\.top\(\{[^)]*filter: false/.test(P('concepts/' + f + '.html')), f);
  assert.ok(!/C\.keep/.test(P('concepts/lib/carry.js') + P('concepts/c17-forward-carry.html')));
});

console.log(n + ' tests');
