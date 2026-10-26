// #192: бейдж прогона в шапке мониторов (prototypes/concepts/lib/kit.js) — сколько прошло с публикации прогона
// (K.runAgo, K.runBadge: подсветка «устарел» и «есть новее»), что вошло в прогон (K.runCoverage), и событие окна
// 'desk:live' из lib/live.js, по которому бейдж узнаёт о новом прогоне без перезагрузки.
//   node --test tests/js/run_badge.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function loadKit(withRun) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1', protocol: 'file:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (withRun) {  // прогон из базы: демо-прогон prototypes/data.js через lib/real.js
    vm.runInContext(P('data.js'), ctx);
    withRun(ctx.window.DESK);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { K: ctx.window.KIT, M: ctx.window.MOCK };
}
const { K } = loadKit();
const msk = s => new Date(s + '+03:00');
const MIN = 60e3;

test('возраст: минуты, часы с минутами, часы, дни', () => {
  assert.strictEqual(K.runAgo(20e3), 'только что');
  assert.strictEqual(K.runAgo(-5e3), 'только что');  // часы браузера чуть отстают от сервера
  assert.strictEqual(K.runAgo(12 * MIN), '12 мин');
  assert.strictEqual(K.runAgo(60 * MIN), '1 ч');
  assert.strictEqual(K.runAgo(135 * MIN), '2 ч 15 мин');
  assert.strictEqual(K.runAgo(9 * 60 * MIN), '9 ч');
  assert.strictEqual(K.runAgo(3 * 24 * 60 * MIN + 5), '3 дн.');
});

test('бейдж: возраст тикает от момента now; без времени публикации — «—»', () => {
  const pub = '2026-10-23T11:00:00+03:00';
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:00:30')).short, 'только что');
  const b = K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:12:00'));
  assert.strictEqual(b.short, '12 мин назад');
  assert.strictEqual(b.text, 'опубликован 12 мин назад');
  assert.strictEqual(b.level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:40:00')).short, '40 мин назад');
  const u = K.runBadge({ publishedAt: null }, msk('2026-10-23T11:12:00'));
  assert.strictEqual(u.level, 'unknown');
  assert.strictEqual(u.short, '—');
});

test('устарел: в рабочие часы старше fresh_min, в любое время старше max_days; ночью и в выходные — нет', () => {
  const pub = '2026-10-23T10:00:00+03:00';  // пятница
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:29:00')).level, 'ok');
  const s = K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:31:00'));
  assert.strictEqual(s.level, 'stale');
  assert.match(s.why, /в рабочие часы прогон старше 90 мин/);
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T19:00:00+03:00' }, msk('2026-10-23T23:30:00')).level, 'ok', 'вечером не подсвечиваем');
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T19:00:00+03:00' }, msk('2026-10-25T14:00:00')).level, 'ok', 'воскресенье');
  const old = K.runBadge({ publishedAt: '2026-10-19T19:00:00+03:00' }, msk('2026-10-25T14:00:00'));
  assert.strictEqual(old.level, 'stale');
  assert.match(old.why, /старше 3 дн/);
});

test('закреплённый прогон и демо устаревшими не считаются', () => {
  const pub = '2026-10-19T10:00:00+03:00', now = msk('2026-10-23T12:00:00');
  assert.strictEqual(K.runBadge({ publishedAt: pub, pinned: true }, now).level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: pub, demo: true }, now).level, 'ok');
});

test('есть прогон новее: подсветка и текст, у закреплённого — с пометкой', () => {
  const pub = '2026-10-23T11:00:00+03:00', now = msk('2026-10-23T11:05:00');
  const n = K.runBadge({ publishedAt: pub, newer: { run_id: 9, text: '23.10 11:30 · #9' } }, now);
  assert.strictEqual(n.level, 'newer');
  assert.strictEqual(n.why, 'есть прогон новее: 23.10 11:30 · #9');
  assert.match(K.runBadge({ publishedAt: pub, pinned: true, newer: { run_id: 9 } }, now).why, /#9 \(этот выбран вручную\)/);
  assert.strictEqual(K.runBadge({ publishedAt: pub, demo: true, newer: { run_id: 9 } }, now).level, 'ok');
});

test('что вошло: демо-набор — позиции, книги, PnL, рынок', () => {
  const c = K.runCoverage();
  assert.match(c.short, /^\d+\/\d+ поз\.$/);
  const k = c.rows.map(r => r.k);
  ['Позиции', 'Книги', 'PnL', 'Рынок'].forEach(x => assert.ok(k.includes(x), x));
});

test('что вошло: прогон из базы — без оценки и без PnL подсвечены, время публикации — из source.publishedAt', () => {
  const { K: K2, M } = loadKit(D => {
    D.source.publishedAt = '2026-10-23T08:12:00+00:00';
    D.positions[0].notValued = true;
  });
  assert.strictEqual(M.run.publishedAt, '2026-10-23T08:12:00+00:00');
  const c = K2.runCoverage(), n = M.positions.length;
  assert.strictEqual(c.short, (n - 1) + '/' + n + ' поз.');
  const pos = c.rows.find(r => r.k === 'Позиции');
  assert.strictEqual(pos.v, (n - 1) + ' из ' + n + ' оценены, 1 без оценки');
  assert.strictEqual(pos.warn, true);
  const books = new Set(M.positions.map(p => p.book)).size;
  assert.strictEqual(c.rows.find(r => r.k === 'Книги').v, String(books));
  assert.ok(c.rows.find(r => r.k === 'Фондирование'));
});

test('lib/live.js: подписка сообщает странице о новых событиях и режиме ленты событием окна desk:live', async () => {
  const got = [];
  const ctx = {
    console, URL, setTimeout, clearTimeout, JSON, AbortController,
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ events: [{ id: 5, type: 'run.published', run_id: 9, text: 'r9' }], has_more: false, last_id: 5 }) }),
    document: { hidden: false },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    CustomEvent: class { constructor(type, o) { this.type = type; this.detail = o.detail; } },
    dispatchEvent(e) { got.push(e); return true; }
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/live.js'), ctx);
  const sub = ctx.DESK_LIVE.subscribe({ after: 4, types: 'run.published', onEvents() {}, noShare: true });
  await new Promise(r => setTimeout(r, 10));
  sub.stop();
  assert.ok(got.every(e => e.type === 'desk:live'));
  assert.deepStrictEqual(got.map(e => e.detail.kind), ['mode', 'events']);
  assert.strictEqual(got[0].detail.mode, 'poll');  // EventSource нет — опрос
  assert.deepStrictEqual(JSON.parse(JSON.stringify(got[1].detail.list)).map(e => e.run_id), [9]);
  assert.strictEqual(ctx.DESK_LIVE.newRun(got[1].detail.list[0], { source: 'real', runId: 7, asOf: '2026-10-23' }), true);
});
