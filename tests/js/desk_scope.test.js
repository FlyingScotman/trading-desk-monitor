// Фильтр Desk Monitor на остальных страницах (prototypes/desk-scope.js): юрлицо ∩ деск ∩ портфель ∩ книги ∩ сегмент ∩
// валюта из fidm.sel.v1, на демо-наборе (два юрлица, четыре деска, портфели HTM / торговый / ликвидность / деривативы).
//   node tests/js/desk_scope.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

function load(sel) {
  const store = { 'fidm.sel.v1': sel === undefined ? null : JSON.stringify(sel) };
  const ctx = { window: {}, URLSearchParams, localStorage: { getItem: k => store[k] ?? null, removeItem: k => { delete store[k]; } } };
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  vm.runInContext(P('desk-util.js'), ctx);
  vm.runInContext(P('desk-scope.js'), ctx);
  const D = ctx.window.DESK, all = D.positions.slice(), books = Object.assign({}, D.BOOKS);
  return { D, all, books, st: D.scope.apply() };
}
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const E1 = 'Головной банк', E2 = 'Дочерний банк';

t('нет выбора — данные как есть', () => {
  const { D, all, st } = load();
  assert.strictEqual(st.active, false);
  assert.strictEqual(D.positions.length, all.length);
});

t('юрлицо ∩ деск — пересечение, а не последнее нажатое', () => {
  const { D, books, st } = load({ entities: [E2], desks: ['Rates'] });
  assert.ok(st.active);
  assert.deepStrictEqual(Object.keys(D.BOOKS), Object.keys(books).filter(b => books[b].entity === E2 && books[b].desk === 'Rates'));
  assert.ok(D.positions.length && D.positions.every(p => books[p.book].entity === E2 && books[p.book].desk === 'Rates'));
});

t('портфель — по позициям: книга с двумя портфелями отдаёт только выбранный', () => {
  const { D, all } = load({ portfolios: ['Торговый'] });
  assert.ok(D.positions.length && D.positions.every(p => p.portfolio === 'Торговый'));
  assert.ok(all.some(p => p.book === 'OFZ' && p.portfolio === 'Ликвидность'));  // в книге ОФЗ есть оба портфеля
  assert.ok(D.positions.some(p => p.book === 'OFZ') && Object.keys(D.BOOKS).indexOf('HTM') < 0);
  assert.ok(D.BOOKS.HTM, 'книга вне фильтра читается по id (сохранённые настройки страниц)');
});

t('выбранная книга вне юрлица не считается; не осталось книг — фильтр по книгам не действует', () => {
  const { D, books } = load({ entities: [E2], books: ['OFZ'] });  // ОФЗ — книга головного банка
  assert.deepStrictEqual(Object.keys(D.BOOKS).sort(), Object.keys(books).filter(b => books[b].entity === E2).sort());
});

t('сочетание, пустое в прогоне, ослабляется; значения не из прогона пропускаются', () => {
  const { D, all, st } = load({ entities: [E2], desks: ['FX'], portfolios: ['HTM'], ccys: ['XXX'] });  // у дочернего банка нет деска FX
  assert.ok(st.active && D.positions.length && D.positions.length < all.length);
  assert.ok(D.positions.every(p => p.entity === E2));  // ослаблены портфель, затем деск: остаётся юрлицо
});

t('сегмент и валюта; фондирование — по книгам и валюте', () => {
  const { D } = load({ segments: ['Корп'], ccys: ['USD'] });
  assert.ok(D.positions.length && D.positions.every(p => p.segment === 'Корп' && p.ccy === 'USD'));
  assert.ok(D.funding.every(f => f.ccy === 'USD' && D.BOOKS[f.book]));
});

console.log(n + ' tests ok');
