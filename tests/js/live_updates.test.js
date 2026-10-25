// #28: живые обновления мониторов (prototypes/concepts/lib/live.js) — подписка на ленту /api/v1/events по SSE с
// откатом на опрос, курсор без повторов; реакция страницы прогона на run.published (одна точка выбора: полоса или
// переключение, по умолчанию полоса); список типов ленты сверяется с бэкендом.
//   node --test tests/js/live_updates.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const SRC = fs.readFileSync(path.join(ROOT, 'prototypes', 'concepts', 'lib', 'live.js'), 'utf8');

class FakeES {
  constructor(url) { this.url = url; this.readyState = 0; this.on = {}; this.closed = false; FakeES.all.push(this); }
  addEventListener(t, f) { (this.on[t] = this.on[t] || []).push(f); }
  emit(t, data, id) {
    const m = { data: JSON.stringify(data), lastEventId: id == null ? '' : String(id) };
    (this.on[t] || []).forEach(f => f(m));
    if (!this.on[t] && this.onmessage && t === 'message') this.onmessage(m);
  }
  close() { this.closed = true; this.readyState = 2; }
}

// контекст страницы: fetch отвечает из pages (по пути запроса), EventSource — FakeES (или нет)
function load(opt) {
  opt = opt || {};
  FakeES.all = [];
  const calls = [];
  const ctx = {
    console, URL, setTimeout, clearTimeout, JSON,
    fetch: p => {
      calls.push(p);
      const r = opt.respond ? opt.respond(p) : { events: [], has_more: false, last_id: 0 };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(r) });
    },
    document: { hidden: false },
    localStorage: { getItem: k => (opt.store || {})[k] ?? null, setItem() {}, removeItem() {} },
  };
  if (opt.es !== false) ctx.EventSource = FakeES;
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { L: ctx.DESK_LIVE, calls, ctx };
}
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
const ev = (id, type, extra) => Object.assign({ id, type: type || 'input.accepted', text: 'e' + id }, extra || {});

test('типы ленты: все, что пишет бэкенд (EventSource без подписки на тип событие не отдаёт)', () => {
  const { L } = load();
  const py = fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'daystatus.py'), 'utf8');
  const block = name => py.slice(py.indexOf(name + ' = {'), py.indexOf('}', py.indexOf(name + ' = {')));
  const keys = s => [...s.matchAll(/"([a-z_]+\.[a-z_]+)":/g)].map(m => m[1]);
  const scan = fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'inbox', 'scan.py'), 'utf8');
  const fromScan = [...scan.matchAll(/_event\("([a-z_.]+)"/g)].map(m => m[1]);
  const written = new Set([...keys(block('EVENT_TEXT')), ...keys(block('LEVELS')), ...fromScan, 'run.published', 'run.failed']);
  assert.ok(written.size >= 14, [...written].join(','));
  for (const t of written) assert.ok(L.TYPES.includes(t), 'нет в DESK_LIVE.TYPES: ' + t);
});

test('реакция: одна точка выбора, по умолчанию полоса; переопределение этого браузера', () => {
  const { L } = load();
  assert.strictEqual(L.REACTION, 'banner');
  assert.strictEqual(L.reaction({ getItem: () => null }), 'banner');
  assert.strictEqual(L.reaction({ getItem: () => 'switch' }), 'switch');
  assert.strictEqual(L.reaction({ getItem: () => 'всё сразу' }), 'banner');
  assert.strictEqual(L.reaction({ getItem: () => { throw new Error('blocked'); } }), 'banner');
});

test('новый прогон: не тот, что на экране, не тестовый, не досчёт прошлой даты', () => {
  const { L } = load();
  const ctx = { source: 'real', runId: 42, asOf: '2026-10-20' };
  const pub = (run_id, valid_date, run_type) => ({ type: 'run.published', run_id, valid_date, detail: { run_type } });
  assert.ok(L.newRun(pub(43, '2026-10-20', 'EOD'), ctx));
  assert.ok(L.newRun(pub(44, '2026-10-21', 'INTRADAY'), ctx));
  assert.ok(!L.newRun(pub(42, '2026-10-20', 'EOD'), ctx), 'тот же прогон');
  assert.ok(!L.newRun(pub(45, '2026-10-01', 'EOD'), ctx), 'история за прошлую дату');
  assert.ok(!L.newRun(pub(46, '2026-10-21', 'TEST'), ctx));
  assert.ok(!L.newRun({ type: 'run.failed', run_id: 47, valid_date: '2026-10-21' }, ctx));
  assert.ok(L.newRun(pub(1, '2026-10-01', 'EOD'), { source: 'empty' }), 'прогонов не было: любой новый');
});

test('решение: полоса по умолчанию; переключение только без закреплённого прогона и один раз на прогон', () => {
  const { L } = load();
  const e = { run_id: 43 };
  assert.strictEqual(L.decide('banner', { pinned: false }, e, null), 'banner');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, null), 'switch');
  assert.strictEqual(L.decide('switch', { pinned: true }, e, null), 'banner', 'закреплённый выбор сам не меняется');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, '43'), 'banner', 'уже переключались на этот прогон');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, '41'), 'switch');
});

test('SSE: курсор в адресе, hello — режим sse, события по порядку и без повторов', async () => {
  const { L, calls } = load();
  const got = [], modes = [];
  const sub = L.subscribe({ after: 10, types: 'run.', onEvents: l => got.push(...l.map(e => e.id)), onMode: m => modes.push(m) });
  const es = FakeES.all[0];
  assert.strictEqual(es.url, '../api/v1/events/stream?after=10&type=run.');
  es.emit('hello', { after: 10 });
  assert.deepStrictEqual(modes, ['sse']);
  es.emit('run.published', ev(11, 'run.published'), 11);
  es.emit('run.published', ev(11, 'run.published'), 11);  // повтор — не доставляется
  es.emit('input.rejected', ev(12, 'input.rejected'), 12);
  assert.deepStrictEqual(got, [11, 12]);
  assert.strictEqual(sub.cursor(), 12);
  assert.strictEqual(calls.length, 0, 'с курсором и SSE опроса нет');
  sub.stop();
  assert.ok(es.closed);
});

test('откат на опрос: поток закрыт (401/404/не event-stream) — опрос с курсора, страницы has_more подряд', async () => {
  const pages = { 12: { events: [ev(13), ev(14)], has_more: true, last_id: 15 }, 14: { events: [ev(15)], has_more: false, last_id: 15 } };
  const { L, calls } = load({ respond: p => pages[+/after=(\d+)/.exec(p)[1]] || { events: [], has_more: false, last_id: 15 } });
  const got = [], modes = [];
  const sub = L.subscribe({ after: 12, onEvents: l => got.push(...l.map(e => e.id)), onMode: m => modes.push(m), pollMs: 10 });
  const es = FakeES.all[0];
  es.readyState = 2; es.onerror();
  await tick(5);
  assert.deepStrictEqual(modes, ['poll']);
  assert.ok(es.closed);
  assert.deepStrictEqual(got, [13, 14, 15]);
  assert.deepStrictEqual(calls.slice(0, 2), ['../api/v1/events?after=12&limit=200', '../api/v1/events?after=14&limit=200']);
  await tick(30);
  assert.ok(calls.some(c => c === '../api/v1/events?after=15&limit=200'), 'дальше опрос с последнего id: ' + calls.join(' '));
  sub.stop();
});

test('откат на опрос: три обрыва подряд без hello, или hello не пришёл (прокси копит ответ)', async () => {
  let { L } = load();
  const modes = [];
  let sub = L.subscribe({ after: 1, onEvents() {}, onMode: m => modes.push(m), pollMs: 1e4 });
  let es = FakeES.all[0];
  es.emit('hello', { after: 1 });
  es.onerror(); es.emit('hello', { after: 1 });  // сервер закрыл ответ по сроку, браузер переподключился: не обрыв
  es.onerror(); es.onerror();
  assert.deepStrictEqual(modes, ['sse']);
  es.onerror();
  assert.deepStrictEqual(modes, ['sse', 'poll']);
  sub.stop();

  ({ L } = load());
  L.HELLO_MS = 5;
  const m2 = [];
  sub = L.subscribe({ after: 1, onEvents() {}, onMode: m => m2.push(m), pollMs: 1e4 });
  await tick(20);
  assert.deepStrictEqual(m2, ['poll']);
  assert.ok(FakeES.all[0].closed);
  sub.stop();
});

test('без EventSource — сразу опрос; без курсора — сначала last_id ленты, старое не приходит', async () => {
  const { L, calls } = load({ es: false, respond: p => /limit=1$/.test(p) ? { events: [ev(30)], has_more: true, last_id: 30 } : { events: [], has_more: false, last_id: 30 } });
  const got = [], modes = [];
  const sub = L.subscribe({ onEvents: l => got.push(...l), onMode: m => modes.push(m), pollMs: 1e4 });
  await tick(5);
  assert.strictEqual(calls[0], '../api/v1/events?limit=1');
  assert.strictEqual(calls[1], '../api/v1/events?after=30&limit=200');
  assert.deepStrictEqual(modes, ['poll']);
  assert.deepStrictEqual(got, []);
  sub.stop();
});
