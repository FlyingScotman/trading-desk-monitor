// TDM#629: мониторы не ждут сеть главным потоком. lib/real.js грузит данные сверх прогона только асинхронно (fetch):
//  - в real.js нет синхронных запросов (ни XMLHttpRequest, ни async=false), в страницах — тоже;
//  - один адрес — один запрос в пределах страницы; история позиций просит только нужные бумаги и период, уже загруженное
//    с той же или более ранней даты не просит снова, длинный список — частями, «все» — без ids;
//  - читалки (M.posHistory, M.marketHistory, M.marketCal, M.derivParallel) синхронны и отдают то, что пришло;
//  - сбой запроса не молчит (M.netErrors, событие concepts-data), «данных нет» (404) — не сбой, повтор после сбоя возможен;
//  - KIT.whenData: каркас и «загрузка» сразу, страница — по ответу; в демо — сразу; сбой — полоса «Повторить»;
//  - страницы C05 / C09 / C11 ждут данные через KIT.whenData, C13 / C03 / C08 — своими запросами по нужным бумагам и периоду.
//   node tests/js/async_load_629.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const T = [];
const t = (name, f) => T.push([name, f]);

function el() {
  const e = { style: {}, dataset: {}, children: [], innerHTML: '', textContent: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    insertBefore(c) { this.children.unshift(c); return c; }, insertAdjacentHTML() {},
    querySelector() { return el(); }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {},
    remove() { this.isConnected = false; }, getBoundingClientRect: () => ({ width: 80 }) };
  return e;
}
// routes: { подстрока адреса: ответ (тело JSON), число (код), Error (нет связи) | функция (url) → то же }; net.urls — все запросы
function load(routes, opts) {
  opts = opts || {};
  const handlers = {}, events = [];
  const page = el();
  const document = { addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); }, dispatchEvent(e) { events.push(e.detail); (handlers[e.type] || []).forEach(f => f(e)); },
    createElement: el, documentElement: el(), body: el(), querySelector: s => (s === '.k-page' ? page : null), querySelectorAll() { return []; }, getElementById: () => el() };
  const net = { urls: [] };
  class XHR { constructor() { throw new Error('синхронный запрос запрещён (TDM#629)'); } }
  const fetch = u => {
    net.urls.push(u);
    const key = Object.keys(routes || {}).find(k => u.includes(k));
    let a = key ? routes[key] : 404;
    if (typeof a === 'function') a = a(u);
    if (a instanceof Error) return Promise.reject(a);
    const status = typeof a === 'number' ? a : 200;
    return Promise.resolve({ status, ok: status === 200, statusText: '', json: () => Promise.resolve(typeof a === 'number' ? null : a) });
  };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR, fetch, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.demo) vm.runInContext(P('concepts/lib/mock.js'), ctx);
  else { loadRun(ctx); if (opts.mutate) opts.mutate(ctx.window.CONCEPTS_RUN); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  ctx.CONCEPTS_SOURCE = opts.demo ? 'demo' : 'real';
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, net, events, page, ctx };
}
const J = x => JSON.parse(JSON.stringify(x));  // объекты vm-контекста — другого «мира»: сравнение по значению
const hist = (id, dates) => ({ dates, runAt: dates.map(() => null), series: { [id]: { price: dates.map((_, i) => 100 + i), notional: dates.map(() => 1e6), dv01: dates.map(() => -5000) } } });
const histUrls = net => net.urls.filter(u => /history\/positions/.test(u));

t('в real.js и в страницах нет синхронных запросов', () => {
  const real = P('concepts/lib/real.js');
  assert.ok(!/new XMLHttpRequest|XMLHttpRequest\(/.test(real), 'real.js: XMLHttpRequest');
  assert.ok(!/\.open\([^)]*,\s*false\s*\)/.test(real), 'real.js: open(…, false)');
  for (const f of fs.readdirSync(path.join(__dirname, '..', '..', 'prototypes', 'concepts')).filter(x => /\.html$/.test(x)))
    assert.ok(!/new XMLHttpRequest/.test(P('concepts/' + f)), f);
  assert.ok(!/new XMLHttpRequest/.test(P('concepts/lib/kit.js')) && !/new XMLHttpRequest/.test(P('concepts/lib/live.js')));
});

t('история позиций: только нужные бумаги и период, один адрес — один запрос, уже загруженное не просим снова', async () => {
  const { M, net } = load({ 'history/positions': u => hist(decodeURIComponent((u.match(/ids=([^&]*)/) || [])[1] || '').split(',')[0] || 'x', ['2026-10-14', '2026-10-15']) });
  const [a, b] = [M.positions[0].id, M.positions[1].id];
  const before = M.posHistory(a);
  assert.ok(before.fromRuns && before.dates.length === 1, 'до загрузки — единственная точка прогона, без сети');
  assert.strictEqual(histUrls(net).length, 0, 'чтение не ходит в сеть');
  await Promise.all([M.loadHistory([a], '2026-07-01'), M.loadHistory([a], '2026-07-01')]);
  assert.strictEqual(histUrls(net).length, 1, 'одинаковые запросы не дублируются');
  const u = histUrls(net)[0];
  assert.ok(u.includes('ids=' + encodeURIComponent(a)) && u.includes('from=2026-07-01') && u.includes('run_id='), u);
  assert.deepStrictEqual(decodeURIComponent(u.match(/ids=([^&]*)/)[1]).split(','), [String(a)], 'только запрошенные бумаги'); void b;
  assert.strictEqual(M.posHistory(a).dates.length, 2);
  await M.loadHistory([a], '2026-08-01');  // уже есть с более ранней даты
  assert.strictEqual(histUrls(net).length, 1);
  await M.loadHistory([a], '2026-06-01');  // нужна более ранняя
  assert.strictEqual(histUrls(net).length, 2);
  assert.ok(histUrls(net)[1].includes('from=2026-06-01'));
  await M.loadHistory([a], null);  // по умолчанию: сервер отдаёт год до даты прогона
  assert.strictEqual(histUrls(net).length, 3);
  assert.ok(!/from=/.test(histUrls(net)[2]));
  await M.loadHistory([a], '2000-01-01'); await M.loadHistory([a], null);
  assert.strictEqual(histUrls(net).length, 4, 'год по умолчанию — не вся история: с 2000 года просим отдельно');
  assert.ok(histUrls(net)[3].includes('from=2000-01-01'));
  await M.loadHistory([a], '2001-05-05');
  assert.strictEqual(histUrls(net).length, 4, 'загруженное с 2000 года покрывает любой более поздний период');
});

t('запрос без from — это год до даты прогона, а не «вся история»: более ранний период просится отдельно (ревью TDM#629)', async () => {
  const { M, net } = load({ 'history/positions': u => hist(decodeURIComponent((u.match(/ids=([^&]*)/) || [])[1] || '').split(',')[0] || 'x', ['2026-10-14', '2026-10-15']) });
  const a = M.positions[0].id, asOf = M.asOf;
  assert.strictEqual(M.histLoaded(a), false);
  await M.loadHistory([a], null);
  assert.strictEqual(histUrls(net).length, 1); assert.ok(!/from=/.test(histUrls(net)[0]), 'адрес по умолчанию — как греет сервер');
  assert.strictEqual(M.histLoaded(a), true, 'за год загружено');
  await M.loadHistory([a], M.util.addDays(asOf, -300));  // внутри года — уже есть
  assert.strictEqual(histUrls(net).length, 1);
  await M.loadHistory([a], M.util.addDays(asOf, -400));  // раньше года — запрос с датой
  assert.strictEqual(histUrls(net).length, 2); assert.ok(histUrls(net)[1].includes('from=' + M.util.addDays(asOf, -400)));
  // граница, с которой ряд загружен, — дата: короткое окно (60 дней) год не покрывает, карточке нужен запрос за год
  const b = M.positions[1].id;
  await M.loadHistory([b], M.util.addDays(asOf, -60));
  assert.strictEqual(M.histLoaded(b), false);
  await M.loadHistory([b], null);
  assert.strictEqual(M.histLoaded(b), true); assert.strictEqual(histUrls(net).length, 4);
});

t('нет fetch: сбой не остаётся в кэше запросов навсегда', async () => {
  const { M, ctx, events } = load({});
  ctx.fetch = undefined;
  assert.strictEqual(await M.loadMarket(), null);
  assert.ok(/нет fetch/.test(M.netErrors[0].text), JSON.stringify(M.netErrors));
  assert.ok(events.some(e => e && e.error));
  ctx.fetch = u =>  // связь появилась
    Promise.resolve({ status: 200, ok: true, statusText: '', json: () => Promise.resolve({ marketDate: '2026-10-15', indices: {}, curves: {}, notes: [] }) });
  const m = await M.loadMarket();
  assert.ok(m && m.marketDate === '2026-10-15', 'следующий вызов спрашивает снова');
});

t('история всех позиций — одним запросом без ids; длинный список — частями; бумага без истории не запрашивается снова', async () => {
  const { M, net } = load({ 'history/positions': { dates: ['2026-10-15'], runAt: [null], series: {} } });
  await M.loadHistory(null, '2026-09-01');
  assert.strictEqual(histUrls(net).length, 1);
  assert.ok(!/ids=/.test(histUrls(net)[0]) && histUrls(net)[0].includes('from=2026-09-01'), histUrls(net)[0]);
  await M.loadHistory(null, '2026-09-01');
  assert.strictEqual(histUrls(net).length, 1, 'ответ без ряда бумаги — «истории нет», а не повод спросить снова');
});

t('длинный список бумаг — частями (адрес не растёт без предела); почти все — одним запросом без ids', async () => {
  const many = D => { const b = D.positions[0]; for (let i = 0; i < 400; i++) D.positions.push(Object.assign({}, b, { id: 'SYN' + i, isin: null })); };
  const { M, net } = load({ 'history/positions': { dates: ['2026-10-15'], runAt: [null], series: {} } }, { mutate: many });
  const ids = M.positions.map(p => p.id);
  await M.loadHistory(ids.slice(0, 301), '2026-09-01');  // явный список: 301 бумага → 3 запроса по 150
  assert.strictEqual(histUrls(net).length, 3);
  assert.ok(histUrls(net).every(u => u.length < 3000), 'адрес короткий');
  await M.loadHistory(null, '2026-09-01');  // осталось меньше половины — по спискам, без повторов уже загруженных
  const rest = histUrls(net).slice(3);
  assert.ok(rest.length >= 1 && rest.every(u => /ids=/.test(u)));
  const fresh = load({ 'history/positions': { dates: ['2026-10-15'], runAt: [null], series: {} } }, { mutate: many });
  await fresh.M.loadHistory(null, null);
  assert.strictEqual(histUrls(fresh.net).length, 1); assert.ok(!/ids=/.test(histUrls(fresh.net)[0]));
});

t('сбой запроса не молчит и не запоминается; 404 — «данных нет», не сбой', async () => {
  let down = true;
  const { M, net, events } = load({ 'history/market': () => (down ? new Error('offline') : { marketDate: '2026-10-15', indices: {}, curves: {}, notes: ['заметка рынка'] }) });
  assert.strictEqual(M.marketHistory(), null, 'до ответа — null');
  assert.strictEqual(await M.loadMarket(), null);
  assert.strictEqual(M.netErrors.length, 1);
  assert.ok(/история рынка: нет связи с сервером: offline/.test(M.netErrors[0].text), M.netErrors[0].text);
  assert.ok(events.some(e => e && e.error && /история рынка/.test(e.error)), 'событие concepts-data с ошибкой');
  down = false;
  const m = await M.loadMarket();  // сбой не запомнен: спрашивает снова
  assert.strictEqual(m.marketDate, '2026-10-15'); assert.strictEqual(M.marketHistory(), m);
  assert.ok(M.notes.includes('заметка рынка') && M.crosses, 'заметки и кросс-курсы — по приходу');
  assert.strictEqual(await M.loadMarket(), m); assert.strictEqual(net.urls.filter(u => /history\/market/.test(u)).length, 2, 'после ответа — один запрос');
  const nf = load({ 'history/market': 404 });
  assert.strictEqual(await nf.M.loadMarket(), null);
  assert.strictEqual(nf.M.netErrors.length, 0, '404 — рынка в данных нет, не сбой');
  const bad = load({ 'history/market': 500 });
  await bad.M.loadMarket();
  assert.ok(/ошибка сервера 500/.test(bad.M.netErrors[0].text));
});

t('календарь рынка: null до ответа, потом объект; строки за ячейкой — обещание; отчёт по деривативам — «загрузка», потом ответ', async () => {
  const cal = { staticDate: '2026-10-15', asOf: '2026-10-15', horizonEnd: '2027-10-15', bigRub: 1e10, held: [], fields: { rub: ['date', 'type', 'kind', 'amount', 'count', 'bigAmount', 'bigCount', 'estAmount'], rubSector: [], rubBig: [], fx: [], fxIssuers: [], fxEvents: [] },
    types: ['PD'], typeLabels: { PD: 'ОФЗ-ПД' }, ccys: [], rub: [['2026-11-01', 'PD', 'cpn', 5e9, 3, 0, 0, 0]], rubSector: [], rubBig: [], fx: [], fxIssuers: [], fxEvents: [] };
  const flows = { count: 1, amount: 5e9, rows: [{ date: '2026-11-01', amount: 5e9, secid: 'X', name: 'Бумага', kind: 'cpn', type: 'PD' }] };
  const { M, net } = load({ 'calendar/market?run_id=': u => (/market=rub/.test(u) ? flows : cal), 'derivatives': { state: 'computed', rows: [], tolerance: null } }, { mutate: D => { D.source.runId = 42; } });
  assert.strictEqual(M.marketCal, null); assert.strictEqual(M.calLoading(), false);
  M.toRepr.RUB = 2;  // валюта отчёта: суммы рублёвого рынка пересчитываются
  const p = M.loadCal();
  assert.strictEqual(M.calLoading(), true);
  const c = await p;
  assert.strictEqual(M.calLoading(), false); assert.strictEqual(M.marketCal, c);
  assert.strictEqual(c.rub[0].count, 3);
  const j = c.flows({ market: 'rub', from: '2026-10-16', to: '2026-12-31' });
  assert.ok(typeof j.then === 'function', 'flows — обещание');
  const rows = await j;
  assert.strictEqual(rows.count, 1); assert.strictEqual(rows.rows[0].amount, 1e10);
  const again = await c.flows({ market: 'rub', from: '2026-10-16', to: '2026-12-31' });  // тот же адрес: ответ общий, но сумма не пересчитывается второй раз
  assert.strictEqual(again.rows[0].amount, 1e10); assert.strictEqual(again.amount, 1e10);
  await M.loadCal(); assert.strictEqual(net.urls.filter(u => /calendar\/market\?run_id=[^&]*$/.test(u)).length, 1, 'календарь — один запрос');
  assert.strictEqual(M.derivParallel().state, 'loading');
  await M.loadDeriv();
  assert.strictEqual(M.derivParallel().state, 'computed');
});

t('календарь: сбой запроса, 404 и «статика не разобрана» — разные состояния; сбой не запоминается', async () => {
  let mode = 'down';
  const cal = { staticDate: '2026-10-15', asOf: '2026-10-15', horizonEnd: '2027-10-15', bigRub: 1e10, held: [], fields: { rub: [], rubSector: [], rubBig: [], fx: [], fxIssuers: [], fxEvents: [] },
    types: [], typeLabels: {}, ccys: [], rub: [], rubSector: [], rubBig: [], fx: [], fxIssuers: [], fxEvents: [] };
  const { M, events } = load({ 'calendar/market': () => mode === 'down' ? new Error('offline') : mode === '500' ? 500 : mode === '404' ? 404 : mode === 'missing' ? { missing: true } : mode === 'broken' ? { staticDate: 'x' } : cal });
  assert.strictEqual(M.calStatus().state, null); assert.strictEqual(M.calStatus().error, null);
  const p = M.loadCal();
  assert.strictEqual(M.calStatus().state, 'loading'); assert.strictEqual(M.calLoading(), true);
  assert.strictEqual(await p, null);
  assert.strictEqual(M.calStatus().state, 'failed'); assert.ok(/нет связи с сервером: offline/.test(M.calStatus().error));
  assert.strictEqual(M.calLoading(), false);
  assert.ok(events.some(e => e && e.error && /календарь рынка/.test(e.error)), 'сбой — событием (полоса «Повторить»)');
  mode = '500'; await M.loadCal();
  assert.strictEqual(M.calStatus().state, 'failed'); assert.ok(/ошибка сервера 500/.test(M.calStatus().error), 'после сбоя спрашивает снова');
  mode = 'ok'; const c = await M.loadCal();
  assert.ok(c && M.calStatus().state === 'ready' && M.marketCal === c, 'после сбоя — ответ');
  const nf = load({ 'calendar/market': 404 });
  assert.strictEqual(await nf.M.loadCal(), null);
  assert.strictEqual(nf.M.calStatus().state, 'missing', '404 — календаря нет, не сбой'); assert.strictEqual(nf.M.calStatus().error, null);
  assert.strictEqual(nf.M.netErrors.length, 0);
  const miss = load({ 'calendar/market': { missing: true } });
  await miss.M.loadCal(); assert.strictEqual(miss.M.calStatus().state, 'missing');
  const broken = load({ 'calendar/market': { staticDate: 'x' } });  // ответ без полей: сборка падает — не «загрузка» навсегда
  assert.strictEqual(await broken.M.loadCal(), null);
  assert.strictEqual(broken.M.calStatus().state, 'failed'); assert.strictEqual(broken.M.calLoading(), false);
  assert.ok(/ответ не разобран/.test(broken.M.calStatus().error));
});

t('алиасы по приходу: событие alias и точечная замена имён, без события filter; календарь, пришедший раньше алиасов, переименовывается', async () => {
  const cal = { staticDate: '2026-10-15', asOf: '2026-10-15', horizonEnd: '2027-10-15', bigRub: 1e10, held: [], fields: { rub: [], rubSector: [], rubBig: ['date', 'kind', 'secid', 'name', 'amount'], fx: [], fxIssuers: [], fxEvents: [] },
    types: [], typeLabels: {}, ccys: [], rub: [], rubSector: [], rubBig: [['2026-11-01', 'mat', 'SEC1', 'Имя хаба', 5e9]], fx: [], fxIssuers: [], fxEvents: [] };
  let release;
  const gate = new Promise(r => { release = r; });
  const withIsin = D => { D.positions[0].instrumentId = 'SEC1'; D.positions[0].isin = 'SU000TESTISIN'; };
  const { M, K, ctx } = load({ 'instruments/aliases': () => gate.then(() => ({ aliases: [{ isin: 'SU000TESTISIN', alias: 'Мой алиас', changed_by: 'x', changed_at: '2026-10-15T10:00:00' }] })),
    'calendar/market': cal }, { mutate: withIsin });
  const p = M.positions.find(x => x.isin === 'SU000TESTISIN');
  assert.ok(p && p.name === p.exchName, 'до ответа — имя биржи');
  const c = await M.loadCal();  // календарь пришёл раньше алиасов
  assert.strictEqual(c.rubBig[0].pos, p.id); assert.strictEqual(c.rubBig[0].name, 'Имя хаба');
  // ячейка таблицы, нарисованная до алиасов, помечена data-pn
  const html = K.nameCell(p, p.name);
  assert.ok(html.includes('data-pn="' + p.id + '"'), html);
  const cellA = { dataset: { pn: String(p.id) }, textContent: p.name, title: '', removeAttribute(k) { this[k] = ''; } };
  ctx.document.querySelectorAll = s => (s === '[data-pn]' ? [cellA] : []);
  let nFilter = 0, nAlias = 0; K.onFilter(() => nFilter++); K.onAlias(() => nAlias++);
  release(); await M.aliasesReady; await new Promise(r => setImmediate(r));
  assert.strictEqual(nAlias, 1, 'страницы с именами подписаны на alias');
  assert.strictEqual(nFilter, 0, 'filter по приходу алиасов не рассылается: он перерисовывает тяжёлые страницы целиком');
  assert.strictEqual(p.name, 'Мой алиас');
  assert.strictEqual(cellA.textContent, 'Мой алиас'); assert.ok(/биржевое имя: /.test(cellA.title));
  assert.strictEqual(M.marketCal.rubBig[0].name, 'Мой алиас', 'календарь, пришедший раньше алиасов, показывает алиас');
  // правка человеком (карточка): как раньше — и alias, и filter
  K.setAlias('SU000TESTISIN', null);
  assert.strictEqual(nFilter, 1); assert.strictEqual(nAlias, 2);
  assert.strictEqual(p.name, p.exchName);
});

t('ряды истории разных запросов: оси разные — сводятся по дате (KIT.histAxis / KIT.histOn), одна ось — без пересчёта', () => {
  const { K } = load({});
  // «истина» — один запрос на обе бумаги: общая ось, цена пустая там, где бумага не торговалась, номинал — последний прогона
  const T = ['2026-10-01', '2026-10-02', '2026-10-03', '2026-10-05'];
  const truthA = { price: [100, 101, null, 102], notional: [10, 10, 10, 20] }, truthB = { price: [50, null, 51, 52], notional: [null, null, 5, 5] };
  // два запроса: ось каждого — даты своих котировок и прогонов (10-01 и 10-05 — прогоны, в обеих осях)
  const A = { dates: ['2026-10-01', '2026-10-02', '2026-10-05'], price: [100, 101, 102], notional: [10, 10, 20], curves: { 'RUB RUONIA': [1, 1, 2] }, pt: { 2: ['method'] }, runAt: [{ id: 1 }, null, { id: 3 }], fromRuns: true };
  const B = { dates: ['2026-10-01', '2026-10-03', '2026-10-05'], price: [50, 51, 52], notional: [null, 5, 5], fromRuns: true };
  const axis = K.histAxis([A, B]);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(axis)), T);
  const a = K.histOn(A, axis), b = K.histOn(B, axis);
  assert.deepStrictEqual(J(a.price), truthA.price); assert.deepStrictEqual(J(a.notional), truthA.notional);
  assert.deepStrictEqual(J(b.price), truthB.price); assert.deepStrictEqual(J(b.notional), truthB.notional);
  assert.deepStrictEqual(J(a.curves['RUB RUONIA']), [1, 1, 1, 2], 'ряды «между прогонами — последний» — значение последней своей даты');
  assert.deepStrictEqual(J(a.pt), { 3: ['method'] }, 'номера точек пометок — на новой оси'); assert.strictEqual(a.fromRuns, true);
  assert.deepStrictEqual(J(a.runAt), [{ id: 1 }, null, null, { id: 3 }]);
  assert.strictEqual(a.dates, axis);
  // одна ось (все ряды одного запроса): сама ось, ряды — те же объекты
  const shared = ['2026-10-01', '2026-10-02'], X = { dates: shared, price: [1, 2] }, Y = { dates: shared, price: [3, 4] };
  assert.strictEqual(K.histAxis([X, Y]), shared); assert.strictEqual(K.histOn(Y, shared), Y);
  // дата до первой своей даты — null (нет ни цены, ни номинала), а не чужая
  const early = K.histOn({ dates: ['2026-10-05'], price: [7], notional: [9] }, T);
  assert.deepStrictEqual(J(early.price), [null, null, null, 7]); assert.deepStrictEqual(J(early.notional), [null, null, null, 9]);
  assert.strictEqual(K.histAxis([]).length, 0);
});

t('карточка: вся история бумаги — отдельный запрос, ответ запоминается; бумага без неё — null, без повторов', async () => {
  const id = () => (ctx2.M.positions.find(p => p.cpn) || ctx2.M.positions[0]).id;
  const ctx2 = load({ 'history/positions': u => ({ dates: ['2024-01-03', '2026-10-15'], runAt: [null, null], series: { [decodeURIComponent(u.match(/ids=([^&]*)/)[1])]: { price: [90, 99], notional: [1, 1], dv01: [-1, -2], zspr: [10, 11] } } }) });
  const { M, net } = ctx2, a = id();
  assert.strictEqual(M.hasDeep(a), false);
  const d = await M.loadDeep(a);
  assert.ok(d && d.dates.length === 2 && M.hasDeep(a));
  assert.ok(histUrls(net)[0].includes('from=2000-01-01'));
  const h = M.posHistory(a, { deep: true });
  assert.ok(h.deep && h.zspr[1] === 11, 'вся история — с рядом Z-спреда');
  await M.loadDeep(a); assert.strictEqual(histUrls(net).length, 1);
  const none = load({ 'history/positions': { dates: ['2026-10-15'], runAt: [null], series: {} } });
  assert.strictEqual(await none.M.loadDeep(none.M.positions[0].id), null);
  assert.strictEqual(none.M.hasDeep(none.M.positions[0].id), true);
});

t('KIT.whenData: «загрузка» и каркас сразу, страница — по ответу; сбой — полоса с «Повторить», страница всё равно рисуется', async () => {
  const { K, page, ctx } = load({});
  let ran = 0, resolve;
  K.whenData(() => new Promise(r => { resolve = r; }), () => { ran++; });
  assert.strictEqual(ran, 0, 'страница не ждёт данные синхронно');
  assert.ok(page.children[0] && /Загрузка данных/.test(page.children[0].innerHTML), 'строка «Загрузка данных…» уже в странице');
  resolve(); await new Promise(r => setImmediate(r));
  assert.strictEqual(ran, 1); assert.strictEqual(page.children[0].isConnected, false, 'строка «загрузка» убрана');
  assert.strictEqual(ctx.document.documentElement.dataset.ready, '1');
  // сбой загрузчика
  let ran2 = 0;
  K.whenData(() => Promise.reject(new Error('нет связи')), () => { ran2++; });
  await new Promise(r => setImmediate(r));
  assert.strictEqual(ran2, 1, 'без этих данных, как раньше');
  assert.ok(page.children.some(c => /k-load-err/.test(c.className || '') && /нет связи/.test(c.innerHTML) && /Повторить/.test(c.innerHTML)), 'полоса сбоя с «Повторить»');
});

t('KIT.whenData в демо — сразу, без «загрузки»', () => {
  const { K, page } = load({}, { demo: true });
  let ran = 0;
  K.whenData(() => Promise.resolve(), () => { ran++; });
  assert.strictEqual(ran, 1); assert.strictEqual(page.children.length, 0);
});

t('страницы: C05 и C09 ждут историю рынка, C11 — историю позиций за период; C13, C03, C08 — свои запросы', () => {
  for (const f of ['c05-morning-brief', 'c09-market']) {
    const s = P('concepts/' + f + '.html');
    assert.ok(/K\.whenData\(\(\) => M\.loadMarket && M\.loadMarket\(\)/.test(s), f);
    assert.ok(!/const MH = M\.marketHistory\(\)/.test(s), f + ': MH не читается до ответа');
  }
  const c11 = P('concepts/c11-history.html');
  assert.ok(/K\.whenData\(\(\) => M\.loadHistory\(null, WIN\.from\)/.test(c11) && /location\.reload\(\)/.test(c11), 'C11: окно периода, шире — заново');
  assert.ok(/cmpDate/.test(c11), 'C11: дата сравнения хранится датой');
  const c13 = P('concepts/c13-issuers.html');
  assert.ok(/M\.loadHistory\(L\.map\(p => p\.id\), U\.addDays\(M\.asOf, -366\)\)/.test(c13), 'C13: бумаги эмитента и год');
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/M\.loadHistory\(null, M\.util\.addDays\(M\.asOf, -60\)\)/.test(c03) && /M\.loadHistory\(\[p\.id\], null\)/.test(c03), 'C03: спарклайн — 60 дней, карточка — выбранная бумага');
  const c08 = P('concepts/c08-calendar.html');
  assert.ok(/M\.loadCal\(\)/.test(c08) && /whenFlows\(C\.flows/.test(c08), 'C08: календарь и строки — по ответу');
  assert.ok(/M\.calStatus/.test(c08) && /K\.onAlias\(render\)/.test(c08), 'C08: сбой отличается от «статика не разобрана»; алиасы подставляются и после календаря');
  assert.ok(/K\.histAxis/.test(c13) && /K\.histOn/.test(c13), 'C13: ряды разных запросов — по дате');
  assert.ok(/K\.histOn/.test(c11), 'C11: ряды на общей оси по дате');
});

(async () => {
  for (const [name, f] of T) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
