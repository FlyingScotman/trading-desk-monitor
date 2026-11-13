// Блок «Сделки за день» в карточке позиции (TDM#726), без браузера: куплено и продано — количество, номинал, средняя цена
// и число сделок, нетто, ссылка на сделки позиции на C20; данные — p.tradeToday.buy / .sell / .net из сервера
// (backend desk/api/concepts.py trade_of), нового запроса нет; нет разбивки по сторонам — блока нет. Вымышленные данные.
//   node tests/js/day_trades_block_726.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const P = f => fs.readFileSync(path.join(ROOT, 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { const r = f(); n++; console.log('ok', name); return r; };

function el() {
  const q = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true, parentNode: {}, q,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(sel) { return q[sel] || (q[sel] = el()); }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, remove() {}, insertAdjacentHTML() {} };
}
function load(source, calls) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c03-blotter.html', pathname: '/concepts/c03-blotter.html', search: '', hostname: 'x', protocol: 'http:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, Promise,
    getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} },
    fetch: (...a) => { (calls || []).push(a[0]); return new Promise(() => {}); } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  const M = ctx.window.MOCK;
  M.asOf = '2026-10-20'; M.run = { id: 42 };
  ctx.window.CONCEPTS_SOURCE = source;
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M, K: ctx.window.KIT };
}
const strip = h => h.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();

// две покупки по разным ценам и одна продажа: 3 бумаги по 99 и 1 по 100; 2 бумаги по 101,5; в позиции десять бумаг по 1000
const SYN = { buy: { n: 2, counted: 2, priced: 2, bonds: 4, face: 4e6, avg: 99.25 }, sell: { n: 1, counted: 1, priced: 1, bonds: 2, face: 2e6, avg: 101.5 },
  net: { bonds: 2, face: 2e6 }, n: 3, qty: 2e6 };

t('блок: куплено и продано — количество, номинал, средняя цена, число сделок; нетто; ссылка на сделки позиции', () => {
  const calls = [], { K } = load('real', calls);
  const h = K.dayTradesBlock({ id: 'B1|SEC 1', ccy: 'RUB', tradeToday: SYN });
  const text = strip(h);
  assert.ok(text.includes('Сделки за день'), text);
  assert.ok(/Куплено 4 шт\. 4\.0 mm RUB 99\.2500 % 2/.test(text), text);
  assert.ok(/Продано 2 шт\. 2\.0 mm RUB 101\.5000 % 1/.test(text), text);
  assert.ok(/Нетто: куплено 2 шт\. 2\.0 mm RUB — 3/.test(text), text);
  assert.ok(/>Количество<\/th>/.test(h) && /Цена, % ном\./.test(h) && /<th class="n">Сделок<\/th>/.test(h), 'столбцы');
  // ссылка — на «Сделки дня» с датой страницы, позицией и прогоном страницы (как у пометки «сд N»)
  assert.ok(/href="c20-trades\.html\?date=2026-10-20&pos=B1%7CSEC%201&run_id=42"/.test(h) && /сделки позиции на экране «Сделки дня» →/.test(h), h);
  assert.deepStrictEqual(calls, [], 'блок читает то, что уже есть на странице: запросов нет');
});

t('нетто: продано — со словом и числом без знака; ноль — без направления; без одной из сторон — «нет»', () => {
  const { K } = load('real');
  const sold = strip(K.dayTradesBlock({ id: 'X', ccy: 'USD', tradeToday: { sell: { n: 2, counted: 2, priced: 2, bonds: 9, face: 9e6, avg: 98 }, net: { bonds: -9, face: -9e6 } } }));
  assert.ok(/Куплено нет/.test(sold) && /Продано 9 шт\. 9\.0 mm USD 98\.0000 % 2/.test(sold) && /Нетто: продано 9 шт\. 9\.0 mm USD — 2/.test(sold), sold);
  const flat = strip(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 99 }, sell: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 101 }, net: { bonds: 0, face: 0 } } }));
  assert.ok(/Нетто 0 шт\./.test(flat) && !/Нетто: (куплено|продано)/.test(flat), flat);
  assert.ok(/Куплено 3 шт\. 3k RUB 99\.0000 %/.test(flat), flat);
});

t('номинал неизвестен (продано в ноль) и цены нет — прочерк, а не ноль; сделки без цены или количества названы', () => {
  const { K } = load('real');
  const h = K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: { sell: { n: 3, counted: 2, priced: 1, bonds: 8, face: null, avg: 98 }, net: { bonds: -8, face: null } } });
  const text = strip(h);
  assert.ok(/Продано 8 шт\. — 98\.0000 % 3/.test(text), text);
  assert.ok(/Сделок без количества: 1 — в суммах их нет\./.test(text) && /Сделок без цены: 1 — в средней цене их нет\./.test(text), text);
  assert.ok(!/без цены|без количества/.test(strip(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: SYN }))), 'всё есть — замечания нет');
});

t('ни у одной сделки нет количества — прочерки, нетто не «0 шт.»', () => {
  const { K } = load('real');
  const text = strip(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 2, counted: 0, priced: 0, bonds: 0, face: 0, avg: null }, net: { bonds: 0, face: 0 } } }));
  assert.ok(/Куплено — — 2/.test(text) && /Нетто — — 2/.test(text) && !/0 шт\./.test(text) && /Сделок без количества: 2/.test(text), text);
});

t('нет сделок, старый прогон (только qty), позиция не облигация — блока нет', () => {
  const { K } = load('real');
  assert.strictEqual(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: null }), '');
  assert.strictEqual(K.dayTradesBlock({ id: 'X', ccy: 'RUB' }), '');
  assert.strictEqual(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: { qty: 5e6, price: 99 } }), '');
  assert.strictEqual(K.dayTradesBlock({ id: 'X', ccy: 'RUB', tradeToday: { qty: 5e6, price: 99, n: 2, new: true } }), '');
});

t('пометка «сд N» ведёт туда же, куда ссылка блока (одна функция K.tradesHref)', () => {
  const { K } = load('real');
  const tag = K.tradesTag({ id: 'B1|SEC 1', tradeToday: { n: 3 } });
  assert.ok(tag.includes('href="' + K.tradesHref('B1|SEC 1') + '"') && /href="c20-trades\.html\?date=2026-10-20&pos=B1%7CSEC%201&run_id=42"/.test(tag), tag);
});

t('демо: у облигации со сделками сегодня есть куплено, продано, нетто; цена и число сделок — вымышленные', () => {
  const { M, K } = load('demo');
  const p = M.positions.find(x => x.cpn && x.tradeToday && x.tradeToday.buy);
  assert.ok(p, 'в демо-наборе есть облигация со сделками сегодня');
  const h = K.dayTradesBlock(p), text = strip(h);
  assert.ok(/Куплено \d[\d,]* шт\./.test(text) && /Продано \d[\d,]* шт\./.test(text) && /Нетто/.test(text), text);
  assert.strictEqual(p.tradeToday.net.bonds, p.tradeToday.buy.bonds - p.tradeToday.sell.bonds);
  assert.ok(M.positions.filter(x => x.tradeToday && !x.cpn).every(x => !x.tradeToday.buy), 'у не облигации разбивки нет');
});

t('карточки: полная (K.card) и боковая C03 ставят блок между параметрами и PnL', () => {
  const kit = P('concepts/lib/kit.js'), c03 = P('concepts/c03-blotter.html');
  assert.ok(/K\.legsHtml\(p\) : ''\) \+\n\s+K\.dayTradesBlock\(p\) \+/.test(kit), 'полная карточка: после «Ног сделки», до «PnL»');
  assert.ok(/K\.legsHtml\(p\) : ''\) \+ K\.dayTradesBlock\(p\);/.test(c03), 'карточка справа в C03');
});

console.log(n + ' tests');
