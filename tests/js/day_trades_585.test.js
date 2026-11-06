// Сделки дня на общих экранах (TDM#585) и экран «Схема данных» (TDM#586), без браузера: слова состояний и источников
// сделки в kit.js — те же, что у сервера (backend desk/trades.py); сводка сделок дня (K.dayTrades) спрашивает сервер о
// дате и прогоне страницы и о книгах фильтра шапки; строка «Сделки дня: N, в позициях M, последняя в ЧЧ:ММ» ведёт на
// C20; C20 следует фильтру шапки; C01, C03, C05, C14 показывают сделки дня; C20 и C21 — в навигации и галерее.
//   node tests/js/day_trades_585.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const P = f => fs.readFileSync(path.join(ROOT, 'prototypes', f), 'utf8');
const PY = fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'trades.py'), 'utf8');
let n = 0; const t = (name, f) => { const r = f(); n++; console.log('ok', name); return r; };
const at = async (name, f) => { await f(); n++; console.log('ok', name); };

function pyDict(name) {
  const m = new RegExp('^' + name + ' = \\{([\\s\\S]*?)\\}\\n', 'm').exec(PY);
  assert.ok(m, name);
  return Object.fromEntries([...m[1].matchAll(/"([a-z_]+)": "([^"]*)"/g)].map(x => [x[1], x[2]]));
}
function pyTuple(name) {
  const m = new RegExp('^' + name + ' = \\(([\\s\\S]*?)\\)\\n', 'm').exec(PY);
  return [...m[1].matchAll(/"([a-z_]+)"/g)].map(x => x[1]);
}

function el() {
  const q = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true, parentNode: {}, q,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(sel) { return q[sel] || (q[sel] = el()); }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, remove() {}, insertAdjacentHTML() {} };
}
function load(source, fetchImpl) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c01-cockpit.html', pathname: '/concepts/c01-cockpit.html', search: '', hostname: 'x', protocol: 'http:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, Promise,
    getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, fetch: fetchImpl || (() => new Promise(() => {})) };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  const M = ctx.window.MOCK;
  M.books = [{ id: 'B1', name: 'B1', entity: 'E1', desk: 'D1' }, { id: 'B2', name: 'B2', entity: 'E1', desk: 'D2' }, { id: 'B3', name: 'B3', entity: 'E2', desk: 'D1' }];
  M.book = Object.fromEntries(M.books.map(b => [b.id, b]));
  M.asOf = '2026-10-20'; M.run = { id: 42 };
  ctx.window.CONCEPTS_SOURCE = source;
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M, K: ctx.window.KIT };
}
const plain = x => JSON.parse(JSON.stringify(x));
const RESET = { entity: null, desk: null, books: null, ccy: null, portsOff: null };

t('слова состояний, источников и «проблем» сделки — те же, что у сервера', () => {
  const { K } = load('demo');
  assert.deepStrictEqual(plain(K.TRADE_STATE), pyDict('STATE_TEXT'));
  assert.deepStrictEqual(plain(K.TRADE_ORIGIN), pyDict('ORIGIN_TEXT'));
  assert.deepStrictEqual(plain(K.TRADE_PROBLEMS), pyTuple('PROBLEMS'));
});

t('книги шапки: без фильтра — все (null), юрлицо, деск и выбор книг — по одному правилу K.scope.bookIn', () => {
  const { K } = load('demo');
  K.setFilter(RESET);
  assert.strictEqual(K.headerBooks(), null);
  K.setFilter({ ...RESET, entity: 'E1' });
  assert.deepStrictEqual([...K.headerBooks()], ['B1', 'B2']);
  K.setFilter({ ...RESET, entity: 'E1', desk: 'D2' });
  assert.deepStrictEqual([...K.headerBooks()], ['B2']);
  K.setFilter({ ...RESET, books: ['B3'] });
  assert.deepStrictEqual([...K.headerBooks()], ['B3']);
  K.setFilter({ ...RESET, ccy: 'USD' });  // валюта и портфель книг не выбирают: сводка — по книгам
  assert.strictEqual(K.headerBooks(), null);
});

(async () => {
  await at('сводка: дата и прогон страницы, книги шапки; одна на фильтр; пустой выбор книг — без запроса', async () => {
    const urls = [];
    const body = { date: '2026-10-20', books: ['B1', 'B2'], trades: 7, live: 6, cancelled: 1, applied: 4, problems: 1, last_trade_ts: '2026-10-20T16:41:00',
      states: [{ state: 'applied', text: 'в позициях', n: 4 }, { state: 'no_instrument', text: 'бумага не найдена', n: 1 }],
      sources: [{ origin: 'feed', text: 'фид сервисов адаптера', trades: 6, last_at: '2026-10-20T13:42:00+00:00' }], top: [], by_book: {} };
    const { K } = load('real', u => { urls.push(u); return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }); });
    K.setFilter({ ...RESET, entity: 'E1' });
    let got = null;
    assert.strictEqual(K.dayTrades(d => { got = d; }), null);
    await new Promise(r => setTimeout(r, 0));
    assert.strictEqual(got, body);
    assert.strictEqual(urls.length, 1);
    const u = new URL(urls[0], 'http://x/concepts/');
    assert.strictEqual(u.pathname, '/api/v1/trades/summary');
    assert.strictEqual(u.searchParams.get('date'), '2026-10-20');
    assert.strictEqual(u.searchParams.get('run_id'), '42');
    assert.deepStrictEqual(u.searchParams.getAll('books'), ['B1', 'B2']);
    assert.strictEqual(K.dayTrades(), body, 'та же страница и фильтр — без второго запроса');
    assert.strictEqual(urls.length, 1);
    // своя дата экрана (C14) — без прогона страницы; по всем книгам
    K.dayTrades(null, { all: true, date: '2026-10-19' });
    const v = new URL(urls[1], 'http://x/concepts/');
    assert.strictEqual(v.searchParams.get('date'), '2026-10-19');
    assert.strictEqual(v.searchParams.get('run_id'), null);
    assert.deepStrictEqual(v.searchParams.getAll('books'), []);
    // фильтр не оставил книг — ноль без запроса
    K.setFilter({ ...RESET, entity: 'E9' });
    const z = K.dayTrades();
    assert.strictEqual(z.trades, 0);
    assert.strictEqual(urls.length, 2);
    // строка экранов: число, в позициях, последняя, ошибки учёта, ссылка на C20 с датой
    const line = K.dayTradesLine(body);
    assert.ok(/Сделки дня: <b>7<\/b>, в позициях <b>4<\/b>, последняя в <b>16:41<\/b>/.test(line), line);
    assert.ok(/не учтено с ошибкой: 1/.test(line) && /href="c20-trades\.html\?date=2026-10-20"/.test(line), line);
    assert.ok(/по книгам шапки: 2/.test(line));
    assert.ok(/загрузка/.test(K.dayTradesLine(null)) && /не загрузились/.test(K.dayTradesLine({ error: 'ответ 500' })));
    // пометка позиции «сд N» ведёт на её сделки
    const tag = K.tradesTag({ id: 'B1|SEC 1', tradeToday: { n: 3 } });
    assert.ok(/href="c20-trades\.html\?date=2026-10-20&pos=B1%7CSEC%201"/.test(tag) && />сд 3</.test(tag), tag);
    assert.strictEqual(K.dayTradesTop({ top: [] }), '');
    const top = K.dayTradesTop({ date: '2026-10-20', top: [{ trade_id: 'T1', position_id: 'B1|S', source_instrument_name: 'Бумага', pnl_rub: -1500, side: 'sell' }] });
    assert.ok(/Бумага/.test(top) && /&pos=B1%7CS/.test(top) && /class="neg"/.test(top), top);
  });

  t('демо: сводка из позиций демо-набора, под фильтром шапки', () => {
    const { K } = load('demo');
    K.setFilter(RESET);
    const d = K.dayTrades();
    assert.ok(d && d.demo && d.trades === d.applied && d.trades >= 0);
  });

  t('C20 следует фильтру шапки, показывает источник и время получения, сделки позиции по ?pos=', () => {
    const c20 = P('concepts/c20-trades.html');
    assert.ok(!/filter: false/.test(c20), 'C20 больше не «фильтр шапки здесь не действует»');
    assert.ok(/K\.rowIn\(\{ book: r\.book_id, ccy: r\.ccy, entity: r\.entity, desk: r\.desk/.test(c20));
    assert.ok(/K\.onFilter\(/.test(c20) && /searchParams\.get\('pos'\)/.test(c20));
    assert.ok(/Источник · получена/.test(c20) && /Откуда пришли/.test(c20) && /Не в позициях — почему/.test(c20));
  });

  t('C01, C03, C05, C14 показывают сделки дня со ссылкой на C20', () => {
    assert.ok(/K\.dayTradesTile\(document\.getElementById\('trades'\)\)/.test(P('concepts/c01-cockpit.html')));
    const c03 = P('concepts/c03-blotter.html');
    assert.ok(/K\.dayTradesLine\(/.test(c03) && /K\.tradesTag\(r\)/.test(c03));
    const c05 = P('concepts/c05-morning-brief.html');
    assert.ok(/K\.dayTradesTop\(d\)/.test(c05) && /K\.dayTradesLine\(d\)/.test(c05));
    const c14 = P('concepts/c14-day-status.html');
    assert.ok(/K\.dayTradesTile\(dtEl, \{ all: true, date: d\.date/.test(c14) && /panel\('Сделки дня'/.test(c14));
  });

  t('C20 и C21 — в строке навигации, в шапке мониторов и в галерее', () => {
    const nav = P('desk-nav.js');
    assert.ok(nav.includes("['concepts/c20-trades.html', 'Сделки дня'") && nav.includes("['concepts/c21-data-flow.html', 'Схема данных'"));
    const kit = P('concepts/lib/kit.js');
    assert.ok(kit.includes("['c20-trades.html', 'сделки дня'") && kit.includes("['c21-data-flow.html', 'схема данных'"));
    const cat = P('concepts/catalog.js');
    assert.ok(/\['c21-data-flow', 'C21', 'Схема данных'/.test(cat));
    assert.ok(fs.existsSync(path.join(ROOT, 'prototypes', 'concepts', 'thumbs', 'c21-data-flow.jpg')));
  });

  t('C21: один маршрут сервера, статусы словами, красная стрелка — неполные данные, узкое окно — столбцом', () => {
    const c21 = P('concepts/c21-data-flow.html');
    assert.ok(/data-run="optional"/.test(c21));
    assert.ok(/getJSON\('api\/v1\/dataflow'/.test(c21));
    const apis = [...c21.matchAll(/getJSON\('api\/v1\/([a-z/-]+)/g)].map(m => m[1]).sort();
    assert.deepStrictEqual(apis, ['dataflow', 'inbox/days'], 'хаб и сервисы экран не спрашивает');
    for (const w of ['готово', 'идёт', 'ждёт', 'ошибка', 'выключено']) assert.ok(c21.includes(w), w);
    assert.ok(/\.c21-svg path\.bad \{ stroke: var\(--neg\)/.test(c21));
    assert.ok(/@media \(max-width: 900px\) \{\s*\.c21-flow \{ grid-template-columns: minmax\(0, 1fr\)/.test(c21));
    assert.ok(/Что впереди/.test(c21) && /Где данные неполные/.test(c21));
  });

  console.log(n + ' tests');
})().catch(e => { console.error(e); process.exit(1); });
