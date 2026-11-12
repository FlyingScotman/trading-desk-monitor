// TDM#675: страница держит номер прогона, с которым открыта. Если прогон заменили новым и удалили, ленивые запросы по нему
// (карточка «Откуда данные», потоки, история, календарь, рынок, отчёт по деривативам) получают 404 с details.reason
// 'run_not_found'. Это не «нет позиции» и не «нет данных»: страница говорит, что расчёт заменён, и даёт обновить её.
// 404 без этого признака (нет позиции, старый сервер) остаётся как был.
//   node tests/js/run_gone_675.test.js
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
    const status = typeof a === 'number' ? a : a && a.__status ? a.__status : 200;  // {__status, body}: ответ с кодом и телом
    const body = typeof a === 'number' ? null : a && a.__status ? a.body : a;
    return Promise.resolve({ status, ok: status === 200, statusText: '', json: () => Promise.resolve(body) });
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
const J = x => JSON.parse(JSON.stringify(x));

const GONE = { __status: 404, body: { code: 'not_found', message: 'run 5 not found', details: { reason: 'run_not_found', run_id: 5 } } };
const NOPOS = { __status: 404, body: { code: 'not_found', message: 'no position x in run 5', details: { reason: 'position_not_found', run_id: 5 } } };
const SAY = 'Расчёт, который показывает страница, заменён новым — обновите страницу';

t('карточка «Откуда данные»: прогона нет — страница заменена, позиции нет — как раньше, сбой не запоминается', async () => {
  const gone = load({ 'positions/provenance': GONE }), id = 'x';
  const r = await gone.M.posProvenance(id);
  assert.deepStrictEqual(J(r), { error: SAY, gone: true });
  assert.strictEqual(gone.M.RUN_GONE, SAY);
  await gone.M.posProvenance(id);
  assert.strictEqual(gone.net.urls.filter(u => /provenance/.test(u)).length, 2, 'ответ «прогона нет» не запоминается: спросит снова');
  const nopos = load({ 'positions/provenance': NOPOS });
  assert.deepStrictEqual(J(await nopos.M.posProvenance(id)), { error: 'сервер не нашёл позицию в прогоне' });
  const old = load({ 'positions/provenance': 404 });  // сервер до TDM#675: тела с признаком нет
  assert.deepStrictEqual(J(await old.M.posProvenance(id)), { error: 'сервер не нашёл позицию в прогоне' });
  const srv = load({ 'positions/provenance': 500 });
  assert.ok(/ошибка сервера 500/.test((await srv.M.posProvenance(id)).error));
});

t('потоки позиции: прогона нет — сообщение о замене (error), позиции нет — причина как раньше', async () => {
  const gone = load({ 'positions/flows': GONE });
  assert.deepStrictEqual(J(await gone.M.posFlows('x')), { flows: null, error: SAY, gone: true });
  const nopos = load({ 'positions/flows': NOPOS });
  assert.deepStrictEqual(J(await nopos.M.posFlows('x')), { flows: null, reason: 'сервер не нашёл позицию или её график выпуска' });
});

t('история, рынок, календарь, отчёт по деривативам: прогона нет — одна строка о замене и «Обновить страницу», не «нет данных»', async () => {
  const { M, events } = load({ 'history/positions': GONE, 'history/market': GONE, 'calendar/market': GONE, '/derivatives': GONE },
    { mutate: r => { r.source = Object.assign({}, r.source, { runId: 5 }); } });
  const id = M.positions[0].id;
  const errs = await M.loadHistory([id], '2026-07-01');
  assert.ok(errs.length === 1 && errs[0].gone === true, 'история: сбой с признаком gone');
  assert.strictEqual(await M.loadMarket().then(x => x, () => 'rej'), null);
  assert.ok(M.netErrors.some(x => x.what === 'история рынка' && x.text === SAY && x.status === 404), JSON.stringify(M.netErrors));
  assert.strictEqual(await M.loadCal(), null);
  assert.strictEqual(M.calStatus().state, 'failed'); assert.strictEqual(M.calStatus().error, SAY, 'календарь: не «статика не разобрана»');
  const par = await M.loadDeriv();
  assert.strictEqual(par.state, 'unavailable'); assert.strictEqual(par.gone, true);
  const ev = events.filter(e => e && e.error);
  assert.ok(ev.length >= 4 && ev.every(e => e.error === SAY && e.gone === true), JSON.stringify(ev));
  const d = await M.loadDeep(id);
  assert.strictEqual(d, null);
});

t('404 без признака «прогона нет» — как раньше: данных нет, не сбой', async () => {
  const { M, events } = load({ 'history/market': NOPOS, 'calendar/market': 404 });
  await M.loadMarket();
  assert.strictEqual(await M.loadCal(), null);
  assert.strictEqual(M.calStatus().state, 'missing');
  assert.strictEqual(M.netErrors.length, 0); assert.ok(!events.some(e => e && e.error));
});

t('K.reloadLink: действие «Обновить страницу»', () => {
  const { K } = load({});
  const h = K.reloadLink();
  assert.ok(h.includes('Обновить страницу') && h.includes('data-a="reload-page"'), h);
});

(async () => {
  for (const [name, f] of T) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
