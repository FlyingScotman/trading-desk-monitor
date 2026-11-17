// TDM#750, #751, #752 без браузера: общий столбец «Сделки за день» позиций (K.posCols.tradeDay, K.tradeNet) и его место в
// наборах колонок C03 и таблицах позиций; цена оценки сделки (K.tradeMarkTitle). Вымышленные данные.
//   node tests/js/trades_run_date_750_751_752.test.js
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
const SYN = { buy: { n: 2, counted: 2, priced: 2, bonds: 4, face: 4e6, avg: 99.25 }, sell: { n: 1, counted: 1, priced: 1, bonds: 2, face: 2e6, avg: 101.5 },
  net: { bonds: 2, face: 2e6 }, n: 3, qty: 2e6 };

t('нетто за день — то же число, что «Нетто» блока карточки; подсказка: куплено, продано, сделок', () => {
  const { K } = load('real');
  const p = { id: 'X', ccy: 'RUB', tradeToday: SYN };
  const net = K.tradeNet(p);
  assert.strictEqual(net.face, SYN.net.face);
  assert.ok(/Нетто: куплено 2 шт\. 2\.0 mm RUB/.test(strip(K.dayTradesBlock(p))), 'в карточке то же нетто');
  assert.ok(/Куплено: 4 шт\. \(4\.0 mm RUB\); продано: 2 шт\. \(2\.0 mm RUB\)\. Сделок: 3/.test(net.tip), net.tip);
  const col = K.posCols.tradeDay;
  assert.strictEqual(col.label, 'Сделки за день');
  assert.strictEqual(col.val(p), 2e6);
  assert.ok(/^\+2\.00/.test(strip(col.fmt(col.val(p), p))) && /mm RUB/.test(strip(col.fmt(col.val(p), p))), strip(col.fmt(col.val(p), p)));
});

t('продажа — со знаком минус, без сделок — пусто и null (сортировка), продано в ноль — штуки', () => {
  const { K } = load('real');
  const col = K.posCols.tradeDay;
  const sold = { id: 'X', ccy: 'USD', tradeToday: { sell: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3e6, avg: 98 }, net: { bonds: -3, face: -3e6 }, n: 1 } };
  assert.strictEqual(col.val(sold), -3e6);
  K.withNegFmt('minus', () => assert.ok(/^−3\.00/.test(strip(col.fmt(col.val(sold), sold))), strip(col.fmt(col.val(sold), sold))));
  assert.strictEqual(col.val({ id: 'X', ccy: 'RUB', tradeToday: null }), null);
  assert.strictEqual(col.fmt(null, { id: 'X', ccy: 'RUB', tradeToday: null }), '');
  assert.strictEqual(col.fmt(null, { id: 'X', ccy: 'RUB' }), '');
  const zero = { id: 'X', ccy: 'RUB', tradeToday: { sell: { n: 1, counted: 1, priced: 1, bonds: 9, face: null, avg: 98 }, net: { bonds: -9, face: null }, n: 1 } };
  assert.strictEqual(col.val(zero), null);
  assert.ok(/\(9\) шт\./.test(strip(col.fmt(null, zero))), strip(col.fmt(null, zero)));
  const flat = { id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 99 }, sell: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 101 }, net: { bonds: 0, face: 0 }, n: 2 } };
  assert.strictEqual(col.val(flat), 0);   // купил и продал одно и то же: нетто ноль, а не «нет сделок»
  assert.ok(/0\.00/.test(strip(col.fmt(0, flat))));
});

t('прогон без разбивки по сторонам: изменение номинала p.tradeToday.qty; сделок без количества — прочерк', () => {
  const { K } = load('real');
  const col = K.posCols.tradeDay;
  assert.strictEqual(col.val({ id: 'X', ccy: 'RUB', tradeToday: { qty: 5e6, price: 99, n: 2 } }), 5e6);
  const noQty = { id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 2, counted: 0, priced: 0, bonds: 0, face: 0, avg: null }, net: { bonds: 0, face: 0 }, n: 2 } };
  assert.strictEqual(col.val(noQty), null);
  assert.ok(/—/.test(strip(col.fmt(null, noQty))), 'нетто неизвестно, а не ноль');
});

t('CSV: число в mm валюты бумаги; без сделок и без номинала (продано в ноль) — пустая ячейка', () => {
  const { K } = load('real');
  const col = K.posCols.tradeDay;
  assert.strictEqual(col.unit, 'mm в вал.');
  assert.strictEqual(col.csvFmt(2e6, { id: 'X', ccy: 'RUB', tradeToday: SYN }), '2.00');
  assert.strictEqual(col.csvFmt(null, { id: 'X', ccy: 'RUB' }), '');
  const zero = { id: 'X', ccy: 'RUB', tradeToday: { sell: { n: 1, counted: 1, priced: 1, bonds: 9, face: null, avg: 98 }, net: { bonds: -9, face: null }, n: 1 } };
  assert.strictEqual(col.csvFmt(null, zero), '', 'числовой столбец: штуки в него не пишем');
  assert.ok(/9/.test(strip(col.fmt(null, zero))) && /шт\./.test(strip(col.fmt(null, zero))), 'на экране штуки остались');
  assert.ok(/9 шт\./.test(K.tradeNet(zero).tip), 'и в подсказке');
});

t('строка «Сделки дня» (C03, C05, C14): день без сделок — «Сделок за ДД.ММ пока нет; последние — за ДД.ММ →» со ссылкой на тот день', () => {
  const { K } = load('real');
  const h = K.dayTradesLine({ date: '2026-11-16', trades: 0, applied: 0, cancelled: 0, earlier: { date: '2026-11-13', trades: 7 } });
  assert.ok(/^Сделок за 16\.11 пока нет; последние — за 13\.11 →$/.test(strip(h)), strip(h));
  assert.ok(/href="c20-trades\.html\?date=2026-11-13"/.test(h), h);
  // сделки есть — прежняя строка; сделок нет и прошлых нет — тоже прежняя («Сделки дня: 0»)
  assert.ok(/Сделки дня: 3 ?, в позициях 2/.test(strip(K.dayTradesLine({ date: '2026-11-16', trades: 3, applied: 2, cancelled: 0, earlier: null }))));
  assert.ok(/Сделки дня: 0/.test(strip(K.dayTradesLine({ date: '2026-11-16', trades: 0, applied: 0, cancelled: 0, earlier: null }))));
});

t('столбец в C03 (наборы «Кэрри и PnL» и «Всё») и в таблицах позиций C01, C06, C07, C12, C13', () => {
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/tradeDay: Object\.assign\(K\.tradeDayCol\(\)/.test(c03), 'описание колонки C03 — общее');
  for (const key of ['pnl', 'all']) {
    const m = new RegExp("\\['" + key + "', '[^']+', \\[([^\\]]*)\\]\\]").exec(c03);
    assert.ok(m && /'tradeDay'/.test(m[1]), 'набор ' + key);
  }
  for (const f of ['c01-cockpit', 'c06-launchpad', 'c07-portfolio-map', 'c12-terminal', 'c13-issuers'])
    assert.ok(/K\.cols\(\[[^\]]*'tradeDay'/.test(P('concepts/' + f + '.html')), f);
});

t('«Цена оценки»: подсказка называет цену и откуда она; источники — слова сервера (модель, рынок, отчёт, номинал)', () => {
  const { K } = load('real');
  assert.ok(/чистая цена/.test(K.tradeMarkTitle()) && !/Откуда/.test(K.tradeMarkTitle()));
  for (const [o, w] of [['model', 'модель'], ['intraday', 'внутридневной срез'], ['close', 'закрытие'], ['report', 'отчёт'], ['par', 'номинал']])
    assert.ok(new RegExp('Откуда: ' + w).test(K.tradeMarkTitle(o)), o);
  assert.ok(/Откуда: последняя цена от 10\.11/.test(K.tradeMarkTitle('stale', '2026-11-10')));
  assert.ok(/Откуда: устаревшая цена$/.test(K.tradeMarkTitle('stale')), 'даты нет — без неё');
  const PY = fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'pnl', 'new_trades.py'), 'utf8');
  const origins = new Set([...(/MARK_ORIGINS = \{([^}]*)\}/.exec(PY)[1]).matchAll(/: "([a-z]+)"/g)].map(x => x[1]).concat('stale'));
  for (const o of origins) assert.ok(K.TRADE_MARK_ORIGIN[o], o);
});

t('C20: без даты страница даты не просит; пустой день — со ссылкой на последний день со сделками', () => {
  const c20 = P('concepts/c20-trades.html');
  assert.ok(/d\.earlier \? 'Сделок за ' \+ esc\(dm\(d\.date\)\) \+ ' пока нет; последние — за /.test(c20));
  assert.ok(/'api\/v1\/trades\?days=30' \+ \(on \? '&date=' \+ on : ''\)/.test(c20), 'дата — только если задана');
  assert.ok(/<th class="n">Цена<\/th><th class="n" title="' \+ esc\(K\.tradeMarkTitle\(\)\) \+ '">Цена оценки<\/th>/.test(c20));
});

console.log(n + ' tests');
