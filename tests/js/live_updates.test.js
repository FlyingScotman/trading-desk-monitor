// #28: живые обновления мониторов (prototypes/concepts/lib/live.js) — одно соединение на браузер (вкладка-лидер по
// Web Locks держит поток SSE и раздаёт события через BroadcastChannel), откат на опрос, курсор без повторов; реакция
// страницы прогона на run.published (одна точка выбора: переключение — решение владельца 25.10 — или полоса; прогон
// берётся у сервера, latest_run); список типов ленты сверяется с бэкендом.
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
  }
  close() { this.closed = true; this.readyState = 2; }
}
FakeES.all = [];

// браузер: общие для его вкладок BroadcastChannel и Web Locks (замок — очередь запросов, как navigator.locks)
function browser() {
  const chans = new Set();
  class BC {
    constructor(name) { this.name = name; chans.add(this); }
    postMessage(d) {
      const data = JSON.parse(JSON.stringify(d));
      for (const c of chans) if (c !== this && c.name === this.name) setTimeout(() => c.onmessage && chans.has(c) && c.onmessage({ data }), 0);
    }
    close() { chans.delete(this); }
  }
  const waiting = [];
  let held = false;
  const locks = {
    request(name, o, cb) {
      return new Promise((res, rej) => {
        const w = () => {
          held = true;
          Promise.resolve(cb()).then(() => { held = false; res(); const n = waiting.shift(); if (n) n(); });
        };
        if (o.signal) o.signal.addEventListener('abort', () => { const i = waiting.indexOf(w); if (i >= 0) { waiting.splice(i, 1); rej(new Error('AbortError')); } });
        if (!held) w(); else waiting.push(w);
      });
    }
  };
  return { BroadcastChannel: BC, locks };
}

// вкладка: fetch отвечает из respond (по пути запроса); br — браузер (null: без Web Locks и BroadcastChannel)
function load(opt) {
  opt = opt || {};
  const calls = [];
  const ctx = {
    console, URL, setTimeout, clearTimeout, JSON, AbortController,
    fetch: p => {
      calls.push(p);
      const r = opt.respond ? opt.respond(p) : { events: [], has_more: false, last_id: 0 };
      if (r && r.status) return Promise.resolve({ ok: false, status: r.status, json: () => Promise.resolve({}) });
      return Promise.resolve({ ok: true, json: () => Promise.resolve(r) });
    },
    document: opt.document || { hidden: !!opt.hidden },
    localStorage: { getItem: k => (opt.store || {})[k] ?? null, setItem() {}, removeItem() {} },
  };
  if (opt.location) ctx.location = opt.location;
  if (opt.es !== false) ctx.EventSource = FakeES;
  const br = opt.br === undefined ? browser() : opt.br;
  if (br) { ctx.BroadcastChannel = br.BroadcastChannel; ctx.navigator = { locks: br.locks }; }
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return { L: ctx.DESK_LIVE, calls, ctx, br };
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

test('реакция: одна точка выбора, по умолчанию автопереключение (владелец, 25.10); переопределение этого браузера', () => {
  const { L } = load();
  assert.strictEqual(L.REACTION, 'switch');
  assert.strictEqual(L.reaction({ getItem: () => null }), 'switch');
  assert.strictEqual(L.reaction({ getItem: () => 'banner' }), 'banner');
  assert.strictEqual(L.reaction({ getItem: () => 'всё сразу' }), 'switch');
  assert.strictEqual(L.reaction({ getItem: () => { throw new Error('blocked'); } }), 'switch');
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

test('решение: полоса только в режиме banner или по закреплённому прогону; переключение только без закреплённого прогона и один раз на прогон', () => {
  const { L } = load();
  const e = { run_id: 43 };
  assert.strictEqual(L.decide('banner', { pinned: false }, e, null), 'banner');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, null), 'switch');
  assert.strictEqual(L.decide('switch', { pinned: true }, e, null), 'banner', 'закреплённый выбор сам не меняется');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, '43'), 'banner', 'уже переключались на этот прогон');
  assert.strictEqual(L.decide('switch', { pinned: false }, e, '41'), 'switch');
});

test('прогон, на который переходить, говорит сервер (latest_run): опубликован не показываемый — ничего (R11)', () => {
  const { L: L0 } = load();
  const L = { target: (...a) => JSON.parse(JSON.stringify(L0.target(...a))) };  // объекты другого контекста vm
  const ctx = { source: 'real', runId: '42' };
  const e = { run_id: 43, text: 'Прогон внутри дня #43' };
  assert.deepStrictEqual(L.target(e, { run_id: '42', label: '20.10 · закрытие' }, ctx), null, 'показываемый не сменился');
  assert.deepStrictEqual(L.target(e, { run_id: '43', label: '21.10 · закрытие' }, ctx), { run_id: '43', text: '21.10 · закрытие · #43' });
  assert.deepStrictEqual(L.target(e, null, ctx), null, 'прогонов нет');
  assert.deepStrictEqual(L.target(e, undefined, ctx), { run_id: 43, text: 'Прогон внутри дня #43' }, 'статус не получен: по событию');
  assert.deepStrictEqual(L.target(e, { run_id: '43' }, { source: 'empty' }), { run_id: '43', text: '#43' });
});

test('типы вкладки: префиксы через запятую, как ?type= ленты', () => {
  const { L } = load();
  assert.ok(L.want({ type: 'run.published' }, 'run.'));
  assert.ok(L.want({ type: 'run.published' }, 'input.rejected, run'));
  assert.ok(!L.want({ type: 'input.accepted' }, 'run.published'));
  assert.ok(L.want({ type: 'input.accepted' }, ''));
});

test('лидер: поток без фильтра, курсор в адресе, hello — режим sse, свои типы по порядку и без повторов', async () => {
  FakeES.all = [];
  const { L } = load();
  const got = [], modes = [];
  const sub = L.subscribe({ after: 10, types: 'run.', onEvents: l => got.push(...l.map(e => e.id)), onMode: m => modes.push(m) });
  await tick(5);
  assert.ok(sub.leader());
  const es = FakeES.all[0];
  assert.strictEqual(es.url, '../api/v1/events/stream?after=10');  // все типы: поток один на все вкладки
  es.emit('hello', { after: 10 });
  assert.deepStrictEqual(modes, ['sse']);
  es.emit('run.published', ev(11, 'run.published'), 11);
  es.emit('run.published', ev(11, 'run.published'), 11);  // повтор — не доставляется
  es.emit('input.rejected', ev(12, 'input.rejected'), 12);  // не свой тип: курсор идёт, onEvents не зовётся
  es.emit('run.failed', ev(13, 'run.failed'), 13);
  assert.deepStrictEqual(got, [11, 13]);
  assert.strictEqual(sub.cursor(), 13);
  sub.stop();
  assert.ok(es.closed);
});

test('семь вкладок одного браузера — один поток; закрылся лидер — поток у следующей вкладки с её курсора', async () => {
  FakeES.all = [];
  const br = browser();
  const tabs = [];
  for (let i = 0; i < 7; i++) {
    const { L, calls } = load({ br });
    const got = [], modes = [];
    tabs.push({ sub: L.subscribe({ after: 20, onEvents: l => got.push(...l.map(e => e.id)), onMode: m => modes.push(m) }), got, modes, calls });
  }
  await tick(5);
  assert.strictEqual(FakeES.all.length, 1, 'одно соединение на браузер');
  assert.deepStrictEqual(tabs.map(t => t.sub.leader()), [true, false, false, false, false, false, false]);
  const es = FakeES.all[0];
  es.emit('hello', { after: 20 });
  es.emit('run.published', ev(21, 'run.published'), 21);
  await tick(5);
  for (const t of tabs) assert.deepStrictEqual(t.got, [21]);
  assert.ok(tabs.slice(1).every(t => t.modes[0] === 'sse'), 'режим лидера виден всем вкладкам');
  tabs[0].sub.stop();  // вкладка-лидер закрылась
  await tick(5);
  assert.ok(es.closed);
  assert.strictEqual(FakeES.all.length, 2);
  assert.ok(tabs[1].sub.leader());
  assert.strictEqual(FakeES.all[1].url, '../api/v1/events/stream?after=21');  // без пропусков
  FakeES.all[1].emit('hello', { after: 21 });
  FakeES.all[1].emit('run.failed', ev(22, 'run.failed'), 22);
  await tick(5);
  for (const t of tabs.slice(1)) assert.deepStrictEqual(t.got, [21, 22]);
  tabs.forEach(t => t.sub.stop());
});

test('новая вкладка-слушатель один раз дочитывает ленту от своего курсора, рассылка не дублирует', async () => {
  FakeES.all = [];
  const br = browser();
  const a = load({ br });
  const leader = a.L.subscribe({ after: 30, onEvents() {} });
  await tick(5);
  const b = load({ br, respond: p => /after=29&limit=500/.test(p) ? { events: [ev(30), ev(31)], has_more: false, last_id: 31 } : { events: [], has_more: false, last_id: 31 } });
  const got = [];
  const sub = b.L.subscribe({ after: 29, onEvents: l => got.push(...l.map(e => e.id)) });
  FakeES.all[0].emit('input.accepted', ev(31), 31);  // приходит, пока слушатель дочитывает: ждёт очереди
  await tick(10);
  assert.deepStrictEqual(got, [30, 31]);
  assert.deepStrictEqual(b.calls, ['../api/v1/events?after=29&limit=500']);
  sub.stop(); leader.stop();
});

test('откат на опрос: поток закрыт (401/404/не event-stream) — лидер опрашивает с курсора, has_more подряд', async () => {
  FakeES.all = [];
  const pages = { 12: { events: [ev(13), ev(14)], has_more: true, last_id: 15 }, 14: { events: [ev(15)], has_more: false, last_id: 15 } };
  const { L, calls } = load({ respond: p => /limit=500/.test(p) ? { events: [], has_more: false, last_id: 15 } : pages[+/after=(\d+)/.exec(p)[1]] || { events: [], has_more: false, last_id: 15 } });
  const got = [], modes = [];
  const sub = L.subscribe({ after: 12, onEvents: l => got.push(...l.map(e => e.id)), onMode: m => modes.push(m), pollMs: 10 });
  await tick(5);
  const es = FakeES.all[0];
  es.readyState = 2; es.onerror();
  await tick(5);
  assert.deepStrictEqual(modes, ['poll']);
  assert.ok(es.closed);
  assert.deepStrictEqual(got, [13, 14, 15]);
  assert.ok(calls.includes('../api/v1/events?after=12&limit=200') && calls.includes('../api/v1/events?after=14&limit=200'), calls.join(' '));
  await tick(30);
  assert.ok(calls.includes('../api/v1/events?after=15&limit=200'), 'дальше опрос с последнего id: ' + calls.join(' '));
  sub.stop();
});

test('откат на опрос: три обрыва подряд без hello, или hello не пришёл (прокси копит ответ)', async () => {
  FakeES.all = [];
  let { L } = load();
  const modes = [];
  let sub = L.subscribe({ after: 1, onEvents() {}, onMode: m => modes.push(m), pollMs: 1e4 });
  await tick(5);
  let es = FakeES.all[0];
  es.emit('hello', { after: 1 });
  es.onerror(); es.emit('hello', { after: 1 });  // сервер закрыл ответ по сроку, браузер переподключился: не обрыв
  es.onerror(); es.onerror();
  assert.deepStrictEqual(modes, ['sse']);
  es.onerror();
  assert.deepStrictEqual(modes, ['sse', 'poll']);
  sub.stop();

  FakeES.all = [];
  ({ L } = load());
  L.HELLO_MS = 5;
  const m2 = [];
  sub = L.subscribe({ after: 1, onEvents() {}, onMode: m => m2.push(m), pollMs: 1e4 });
  await tick(30);
  assert.deepStrictEqual(m2, ['poll']);
  assert.ok(FakeES.all[0].closed);
  sub.stop();
});

test('истекла сессия: опрос получает 401 — режим signed-out, после входа снова poll', async () => {
  FakeES.all = [];
  let status = 401;
  const { L } = load({ es: false, respond: p => /limit=200/.test(p) && status ? { status } : { events: [], has_more: false, last_id: 5 } });
  const modes = [];
  const sub = L.subscribe({ after: 5, onEvents() {}, onMode: m => modes.push(m), pollMs: 10 });
  await tick(5);
  assert.deepStrictEqual(modes, ['poll', 'signed-out']);
  status = 0;
  await tick(30);
  assert.deepStrictEqual(modes.slice(0, 3), ['poll', 'signed-out', 'poll']);
  sub.stop();
});

test('без Web Locks и BroadcastChannel — потока нет совсем: опрашивает только видимая вкладка', async () => {
  FakeES.all = [];
  const hidden = load({ br: null, hidden: true });
  const s1 = hidden.L.subscribe({ after: 3, types: 'run.', onEvents() {}, pollMs: 1e4 });
  const shown = load({ br: null });
  const s2 = shown.L.subscribe({ after: 3, types: 'run.', onEvents() {}, pollMs: 1e4 });
  await tick(5);
  assert.strictEqual(FakeES.all.length, 0, 'EventSource есть, но поток в каждой вкладке занял бы соединения');
  assert.deepStrictEqual(hidden.calls, []);
  assert.deepStrictEqual(shown.calls, ['../api/v1/events?after=3&type=run.&limit=200']);
  s1.stop(); s2.stop();
});

test('без курсора — сначала last_id ленты, старое не приходит', async () => {
  FakeES.all = [];
  const { L, calls } = load({ es: false, br: null, respond: p => /limit=1$/.test(p) ? { events: [ev(30)], has_more: true, last_id: 30 } : { events: [], has_more: false, last_id: 30 } });
  const got = [], modes = [];
  const sub = L.subscribe({ onEvents: l => got.push(...l), onMode: m => modes.push(m), pollMs: 1e4 });
  await tick(5);
  assert.strictEqual(calls[0], '../api/v1/events?limit=1');
  assert.strictEqual(calls[1], '../api/v1/events?after=30&limit=200');
  assert.deepStrictEqual(modes, ['poll']);
  assert.deepStrictEqual(got, []);
  sub.stop();
});

test('реакция по шагам (L.react): переключение, полоса, сверка после загрузки; ошибка звена не останавливает следующие', async () => {
  const { L } = load();
  const calls = [];
  const deps = (lr, sw) => ({
    latest: () => Promise.resolve(lr), storage: { getItem: () => null },
    reload: () => calls.push('reload'), banner: t => calls.push('banner ' + t.run_id),
    switched: id => { if (id === undefined) return sw; calls.push('switched ' + id); }
  });
  const real = { source: 'real', runId: '42', pinned: false };
  const e = { type: 'run.published', run_id: 43, text: 't' };
  assert.strictEqual(await L.react(e, real, deps({ run_id: '43' }, null)), 'switch');
  assert.deepStrictEqual(calls.splice(0), ['switched 43', 'reload']);
  assert.strictEqual(await L.react(e, real, deps({ run_id: '43' }, '43')), 'banner', 'уже переключались: без петли');
  assert.strictEqual(await L.react(e, { ...real, pinned: true }, deps({ run_id: '43' }, null)), 'banner');
  assert.strictEqual(await L.react(e, real, deps({ run_id: '42' }, null)), 'none', 'R11: показываемый не сменился');
  calls.splice(0);
  // сверка после загрузки (e = null): прогон, опубликованный, пока страница грузилась
  assert.strictEqual(await L.react(null, real, deps({ run_id: '44' }, null)), 'switch');
  assert.strictEqual(await L.react(null, real, deps({ run_id: '42' }, null)), 'none');
  assert.strictEqual(await L.react(null, real, deps(undefined, null)), 'none', 'статус не получен — ничего');
  assert.strictEqual(await L.react(null, real, deps({ run_id: '44' }, '44')), 'none', 'сверка полосу не показывает');
  // пустая страница (#319): первый прогон вышел, пока она грузилась, — переход на него; прогонов всё ещё нет — ничего;
  // сверка — у обоих источников, если прогон не выбран
  const empty = { source: 'empty', pinned: false };
  calls.splice(0);
  assert.strictEqual(await L.react(null, empty, deps({ run_id: '1' }, null)), 'switch');
  assert.deepStrictEqual(calls.splice(0), ['switched 1', 'reload']);
  assert.strictEqual(await L.react(null, empty, deps(null, null)), 'none');
  assert.strictEqual(await L.react(null, empty, deps({ run_id: '1' }, '1')), 'none', 'уже переключались: без петли');
  assert.match(SRC, /if \(!ctx\.pinned\) queue\(null\);/);
  // ошибка в звене (banner упал): промис отклонён; watchRuns ловит его в .catch и ждёт следующего прогона
  const bad = Object.assign(deps({ run_id: '45' }, '45'), { banner: () => { throw new Error('boom'); } });
  await assert.rejects(L.react(e, real, bad), /boom/);
  assert.match(SRC, /busy = busy\.then\(\(\) => mine\.cancelled \? 'none' : L\.react\(e, ctx, Object\.assign\(\{\}, d, \{ prepare:[^\n]*\n\s*\.catch\(/);
});

// #727: новый прогон не опустошает экран — цифры остаются, страница ждёт данные нового прогона и переходит на тёплый кэш
test('реакция (#727): переключение — только когда данные нового прогона готовы; нет — полоса, перезагрузки нет', async () => {
  const { L } = load();
  const calls = [];
  const deps = (res) => ({
    latest: () => Promise.resolve({ run_id: '43' }), storage: { getItem: () => null },
    reload: () => calls.push('reload'), banner: t => calls.push('banner ' + t.run_id),
    switched: id => { if (id === undefined) return null; calls.push('switched ' + id); },
    prepare: (t, ctx) => { calls.push('prepare ' + t.run_id); return Promise.resolve(res); }
  });
  const real = { source: 'real', runId: '42', pinned: false };
  const e = { type: 'run.published', run_id: 43, text: 't' };
  assert.strictEqual(await L.react(e, real, deps('ready')), 'switch');
  assert.deepStrictEqual(calls.splice(0), ['prepare 43', 'switched 43', 'reload'], 'прогон помечен прямо перед перезагрузкой: петли нет');
  for (const res of ['timeout', 'error']) {
    assert.strictEqual(await L.react(e, real, deps(res)), 'banner', res);
    assert.deepStrictEqual(calls.splice(0), ['prepare 43', 'banner 43'], res + ': страница не перезагружается');
  }
  assert.strictEqual(await L.react(e, real, deps('cancelled')), 'none', 'пришёл прогон новее — реакция выходит молча');
  assert.deepStrictEqual(calls.splice(0), ['prepare 43'], 'отменённое ожидание прогон не помечает: следующая реакция на него переключит');
  // отмена, пока статус всё ещё называет этот прогон: прогон не помечен, и следующая реакция переключает, а не даёт полосу
  let marked = null, waits = 0;
  const twice = Object.assign(deps('ready'), {
    switched: id => { if (id === undefined) return marked; marked = String(id); },
    prepare: () => Promise.resolve(waits++ ? 'ready' : 'cancelled')
  });
  assert.strictEqual(await L.react(e, real, twice), 'none');
  assert.strictEqual(marked, null, 'после отмены вкладка не помечена');
  assert.strictEqual(await L.react(e, real, twice), 'switch', 'повторная реакция на тот же прогон — переключение, не полоса');
  assert.strictEqual(marked, '43');
  calls.splice(0);
  // закреплённый прогон — как раньше: полоса, ожидания нет
  assert.strictEqual(await L.react(e, { ...real, pinned: true }, deps('ready')), 'banner');
  assert.deepStrictEqual(calls.splice(0), ['banner 43']);
  // без ожидания (d.prepare нет) — сразу, как раньше
  const bare = deps('ready'); delete bare.prepare;
  assert.strictEqual(await L.react(e, real, bare), 'switch');
});

test('готовность (#727): опрос /api/v1/runs/{id}/ready; сбой сети и 5xx — ждём дальше; 404 — ошибка; таймаут; отмена', async () => {
  const { L } = load();
  const run = (answers, o) => {
    const urls = [];
    let t = 0, i = 0;
    const fetch = (u) => {
      urls.push(u);
      const a = answers[Math.min(i++, answers.length - 1)];
      if (a === 'net') return Promise.reject(new Error('net'));
      return Promise.resolve({ ok: typeof a === 'object' || (a >= 200 && a < 300), status: typeof a === 'number' ? a : 200,
        json: () => Promise.resolve(typeof a === 'object' ? a : {}) });
    };
    return L.waitReady(43, Object.assign({ fetch, now: () => t, sleep: ms => { t += ms; return Promise.resolve(); },
      timeoutMs: 6000, pollMs: 1500 }, o)).then(r => ({ r, urls, t }));
  };
  let x = await run([{ ready: false }, 500, 'net', { ready: true }], { pages: 'concepts' });
  assert.strictEqual(x.r, 'ready');
  assert.strictEqual(x.urls.length, 4);
  assert.ok(x.urls.every(u => u === '../api/v1/runs/43/ready?pages=concepts'), x.urls.join());
  assert.strictEqual((await run([{ ready: true }])).r, 'ready');
  assert.strictEqual((await run([404])).r, 'error', 'прогона нет (заменён и удалён)');
  assert.strictEqual((await run([401])).r, 'error');
  x = await run([{ ready: false }]);
  assert.strictEqual(x.r, 'timeout');
  assert.ok(x.t <= 6000 && x.urls.length >= 4, 'опрашивает до таймаута и не дольше: ' + x.urls.length + ' запросов за ' + x.t + ' мс');
  const task = { cancelled: false };
  x = await run([{ ready: false }, { ready: false }], { task, sleep: () => { task.cancelled = true; return Promise.resolve(); } });
  assert.strictEqual(x.r, 'cancelled');
  assert.strictEqual(L.READY_MS, 60000, 'ждём не дольше минуты');
});

test('плашка (#727): текст с временем показанного прогона по Москве; без времени — без него', () => {
  const { L } = load();
  assert.strictEqual(L.noteText('2026-11-13T11:32:00+00:00'), 'Пересчитан новый прогон, загружаю… · данные на 14:32 МСК');
  assert.strictEqual(L.noteText('2026-11-13T14:05:00+03:00'), 'Пересчитан новый прогон, загружаю… · данные на 14:05 МСК');
  assert.strictEqual(L.noteText(null), 'Пересчитан новый прогон, загружаю…');
  assert.strictEqual(L.noteText('не дата'), 'Пересчитан новый прогон, загружаю…');
});

// страница с минимальным DOM: плашка живёт в .k-live под шапкой
function dom(hidden) {
  const bars = [];
  const mk = () => ({
    className: '', innerHTML: '', isConnected: true, attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    after(n) { bars.push(n); }, remove() { this.isConnected = false; },
    querySelector(sel) { return sel === '[data-a=show]' ? (this.show = this.show || {}) : sel === '[data-a=close]' ? (this.close = this.close || {}) : null; }
  });
  const doc = {
    hidden: !!hidden, listeners: {}, head: { appendChild() {} }, body: { prepend: n => bars.push(n) },
    getElementById: () => null, createElement: () => mk(), querySelector: () => null,
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    removeEventListener(t, f) { this.listeners[t] = (this.listeners[t] || []).filter(g => g !== f); }
  };
  return { doc, bars };
}

async function pageWithNewRun(opt) {
  FakeES.all = [];
  const { doc, bars } = dom(opt.hidden);
  const reloads = [], asked = [];
  let n = 0, status = 0;
  const { L, ctx } = load({
    document: doc, location: { reload: () => reloads.push(1), href: 'http://x/concepts/c01.html' },
    respond: p => {
      asked.push(p);
      // сверка при загрузке: нового прогона нет (42); потом, по событию, — 43
      if (p.startsWith('../api/v1/autopilot/status')) return { latest_run: { run_id: status++ ? 43 : 42, label: '13.11 · внутри дня' } };
      if (p.includes('/api/v1/runs/43/ready')) return opt.ready(++n);
      return { events: [], has_more: false, last_id: 5 };
    }
  });
  Object.assign(L, { READY_POLL_MS: 5, READY_MS: opt.timeout || 200 });
  const shown = '2026-11-13T11:32:00+00:00';
  L.watchRuns({ source: 'real', runId: '42', asOf: '2026-11-13', pinned: false, shownAt: shown });
  await tick(20);
  FakeES.all[0].emit('hello', { after: 5 });
  FakeES.all[0].emit('run.published', ev(6, 'run.published', { run_id: 43, valid_date: '2026-11-13', text: 'e6' }), 6);
  return { L, bars, doc, reloads, asked, ctx };
}

test('страница (#727): новый прогон — плашка с временем, цифры на месте, после готовности — перезагрузка', async () => {
  const p = await pageWithNewRun({ ready: n => ({ ready: n >= 8, missing: [] }) });
  await tick(12);
  assert.strictEqual(p.reloads.length, 0, 'пока данные не готовы — страница не перезагружается');
  assert.match(p.bars[0].innerHTML, /Пересчитан новый прогон, загружаю… · данные на 14:32 МСК/);
  assert.ok(!/k-btn/.test(p.bars[0].innerHTML), 'плашка без кнопок: не мешает работать');
  await tick(100);
  assert.strictEqual(p.reloads.length, 1, 'готово — одна перезагрузка');
  assert.ok(p.asked.filter(a => a.includes("/runs/43/ready")).length >= 8);
});

test('страница (#727): данные не дождались — полоса «Доступен новый прогон», страница не перезагружается', async () => {
  const p = await pageWithNewRun({ ready: () => ({ ready: false }), timeout: 40 });
  await tick(150);
  assert.strictEqual(p.reloads.length, 0);
  assert.match(p.bars[0].innerHTML, /Доступен новый прогон: 13\.11 · внутри дня · #43/);
  assert.match(p.bars[0].innerHTML, /data-a="show">Показать</);
});

test('страница (#727): скрытая вкладка не ждёт; когда её покажут — плашка, готовность, перезагрузка', async () => {
  const p = await pageWithNewRun({ hidden: true, ready: () => ({ ready: true }) });
  await tick(40);
  assert.strictEqual(p.reloads.length, 0);
  assert.strictEqual(p.asked.filter(a => a.includes('/ready')).length, 0, 'скрытая вкладка сервер не опрашивает');
  p.doc.hidden = false;
  (p.doc.listeners.visibilitychange || []).forEach(f => f());
  await tick(40);
  assert.strictEqual(p.reloads.length, 1);
  assert.ok(p.asked.some(a => a.includes('/runs/43/ready')));
});

test('страница (#727): прогон новее отменяет ожидание старого и начинает своё', async () => {
  FakeES.all = [];
  const { doc } = dom(false);
  const reloads = [], asked = [];
  let status = 0;
  const { L } = load({
    document: doc, location: { reload: () => reloads.push(1), href: 'http://x/c.html' },
    respond: p => {
      asked.push(p);
      if (p.startsWith('../api/v1/autopilot/status')) return { latest_run: { run_id: [42, 43, 44][Math.min(status++, 2)] } };  // сверка при загрузке, событие 43, событие 44
      if (p.includes('/runs/43/ready')) return { ready: false };
      if (p.includes('/runs/44/ready')) return { ready: true };
      return { events: [], has_more: false, last_id: 5 };
    }
  });
  Object.assign(L, { READY_POLL_MS: 5, READY_MS: 500 });
  L.watchRuns({ source: 'real', runId: '42', asOf: '2026-11-13', pinned: false });
  await tick(20);
  FakeES.all[0].emit('hello', { after: 5 });
  FakeES.all[0].emit('run.published', ev(6, 'run.published', { run_id: 43, valid_date: '2026-11-13' }), 6);
  await tick(30);
  FakeES.all[0].emit('run.published', ev(7, 'run.published', { run_id: 44, valid_date: '2026-11-13' }), 7);
  await tick(100);
  assert.strictEqual(reloads.length, 1, 'одна перезагрузка — на прогон 44');
  const last43 = asked.map((a, i) => a.includes('/runs/43/ready') ? i : -1).filter(i => i >= 0).pop();
  const first44 = asked.findIndex(a => a.includes('/runs/44/ready'));
  assert.ok(first44 > 0 && last43 < first44, 'опрос прогона 43 прекратился до опроса прогона 44');
});

