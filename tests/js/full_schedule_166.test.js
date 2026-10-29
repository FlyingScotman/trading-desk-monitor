// #166: карточка облигации — весь график выпуска. M.posFlows (lib/real.js) запоминает только график: сбой сервера,
// нет связи, 404 и ответ без графика следующее открытие карточки спрашивает снова; график из потоков прогона
// (source 'run', нет файла статики в кэше) — с пометкой, откуда он. Резервный список потоков прогона карточка
// подписывает прямо: «Расписание до <дата>, полное недоступно: <причина>». Прогон — фикстура concepts-run.json.
//   node tests/js/full_schedule_166.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const T = [];
const t = (name, f) => T.push([name, f]);

function el() {
  const kids = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', textContent: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(s) { return kids[s] || (kids[s] = el()); }, querySelectorAll() { return []; },
    addEventListener() {}, setAttribute() {}, remove() {}, getBoundingClientRect: () => ({ width: 80 }) };
}
// answers: очередь ответов fetch — {status, body} или Error (нет связи); calls — сколько раз спросили
function load(answers) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open() {} send() { this.status = 404; } }
  const net = { calls: 0 };
  const fetch = () => {
    net.calls++;
    const a = answers.shift();
    if (a instanceof Error) return Promise.reject(a);
    return Promise.resolve({ status: a.status, ok: a.status >= 200 && a.status < 300, statusText: '', json: () => Promise.resolve(a.body) });
  };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR, fetch };
  ctx.window = ctx;
  vm.createContext(ctx);
  loadRun(ctx);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, net };
}
const FLOWS = [['2027-03-15', 'cpn', 1000, false], ['2030-03-15', 'mat', 1e5, false]];

t('сбой, нет связи, 404 и ответ без графика не запоминаются: следующее открытие спрашивает снова', async () => {
  const { M, net } = load([{ status: 500 }, new Error('offline'), { status: 404 }, { status: 200, body: { flows: [], reason: 'no_static' } },
    { status: 200, body: { source: 'static', ccy: 'RUB', flows: FLOWS } }]);
  const a = await M.posFlows('PX');
  assert.ok(a.flows === null && /^ошибка сервера 500/.test(a.error));
  const b = await M.posFlows('PX');
  assert.ok(b.flows === null && /^нет связи с сервером: offline/.test(b.error));
  const c = await M.posFlows('PX');
  assert.ok(c.flows === null && c.reason);
  const d = await M.posFlows('PX');
  assert.strictEqual(d.reason, 'нет статики хаба за дату прогона');
  const e = await M.posFlows('PX');
  assert.strictEqual(e.flows.length, 2);
  assert.strictEqual(e.note, undefined);
  assert.strictEqual(net.calls, 5);
  const again = await M.posFlows('PX');  // график — из памяти
  assert.strictEqual(again.flows.length, 2);
  assert.strictEqual(net.calls, 5);
});

t('график из потоков прогона (нет файла статики в кэше) — весь, с пометкой, откуда он', async () => {
  const { M, K } = load([{ status: 200, body: { source: 'run', reason: 'no_static', ccy: 'RUB', flows: FLOWS } }]);
  const x = await M.posFlows('PX');
  assert.strictEqual(x.flows.length, 2);
  assert.ok(x.note.startsWith('график по условиям бумаги в прогоне (нет статики хаба за дату прогона)'), x.note);
  const p = { id: 'PX', isin: 'RU000TEST001', name: 'Тест', ccy: 'RUB' };
  const h = K.flowsTable(p, x.flows, false, null, x.note);
  assert.ok(h.includes('график по условиям бумаги в прогоне') && h.includes('2 потоков до 15.03.30') && !h.includes('полное недоступно'));
});

t('резервный список прогона — «Расписание до <дата>, полное недоступно: <причина>»', async () => {
  const { M, K } = load([{ status: 200, body: { flows: [], reason: 'no_schedule' } }]);
  const p = { id: 'PX', isin: 'RU000TEST001', name: 'Тест', ccy: 'RUB' };
  M.cashflows = [{ date: '2027-03-15', pos: 'PX', kind: 'cpn', amount: 1000, amountRub: 1e6 },
    { date: '2027-09-15', pos: 'PX', kind: 'put', amount: 1e5, amountRub: 1e8 }];
  const box = el(); box.querySelector = () => null; K.flows(box, p); await new Promise(r => setTimeout(r, 0));
  assert.ok(box.innerHTML.includes('Расписание до 15.09.27, полное недоступно: в статике хаба нет графика этой бумаги. Показаны потоки прогона.'), box.innerHTML);
});

// #464: dataset в браузере хранит строки — числовой id позиции не должен делать ответ сервера «устаревшим»
t('числовой id позиции: карточка показывает весь график, а не «загрузка потоков…»', async () => {
  const { M, K } = load([{ status: 200, body: { source: 'static', ccy: 'RUB', flows: FLOWS } }]);
  const p = { id: 7, isin: 'RU000TEST007', name: 'Тест', ccy: 'RUB' };
  M.cashflows = [];
  const box = el(); box.querySelector = () => null;
  box.dataset = new Proxy({}, { set(o, k, v) { o[k] = String(v); return true; } });  // как DOMStringMap
  K.flows(box, p);
  assert.ok(box.innerHTML.includes('загрузка потоков…'), box.innerHTML);
  await new Promise(r => setTimeout(r, 0));
  assert.ok(box.innerHTML.includes('2 потоков до 15.03.30') && !box.innerHTML.includes('загрузка'), box.innerHTML);
});

(async () => {
  for (const [name, f] of T) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
