// #23 / #163: индикатор догрузки истории в шапке мониторов (prototypes/concepts/lib/kit.js, K.backfillStatus по
// GET /api/v1/backfill/status): «догрузка истории N из M · K не сходятся», подсказка с датами и причинами (даты
// бэкфилла и даты хаба, которые не загружаются: ошибка, попытки, следующая попытка), серый, когда остались только
// проблемные даты; без проблемных дат — без «не сходятся». #447: даты, которые не загружаются, — «не загружаются»,
// а остановка догрузки на ошибке сети или базы — «стоит (сеть)» с причиной и следующей попыткой, не «не сходятся».
//   node tests/js/backfill_indicator.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const tests = [];
const t = (name, f) => tests.push([name, f]);

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1', protocol: 'file:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return ctx.window.KIT;
}
const K = load();
// статус бэкфилла (desk.backfill.status + hub_problem_dates шага sync)
const bf = o => Object.assign({ enabled: true, from: '2026-01-05', to: '2026-10-23', total: 204, done: 190, todo: 9, failed: 5,
  next: ['2026-10-09', '2026-10-12'], eta_s: 540, problem_dates: [], hub_problem_dates: [] }, o || {});
const prob = [{ date: '2026-10-08', why: 'no default run', error: 'ValueError: bad bundle', attempts: 1 },
  { date: '2026-10-13', why: 'program updated', error: 'does not converge: program updated after 2 runs', attempts: 2 }];
const hub = [{ date: '2026-10-16', error: 'SnapshotError: digest mismatch', attempts: 3, next_attempt: '2026-10-23T09:30:00+00:00' }];

t('идёт догрузка, проблемных дат нет: «догрузка истории N из M», без «не сходятся», не серый', () => {
  const s = K.backfillStatus(bf({ failed: 0, total: 199 }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 199');
  assert.ok(!s.text.includes('не сходятся') && !s.title.includes('Не сходятся'));
  assert.strictEqual(s.grey, false);
  assert.ok(s.title.includes('09.10.2026') && s.title.includes('≈ 9 мин'), s.title);  // следующие даты и ETA
});

t('с проблемными датами бэкфилла и хаба: «· K не сходятся», в подсказке даты, причины, попытки, следующая попытка', () => {
  const s = K.backfillStatus(bf({ problem_dates: prob, hub_problem_dates: hub }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 204 · 2 не сходятся · 1 не загружается');
  ['08.10.2026', 'ValueError: bad bundle', '13.10.2026', 'не сходится', 'попыток: 2',
    '16.10.2026', 'с хаба не загружается', 'SnapshotError: digest mismatch', 'попыток: 3', 'следующая попытка 23.10 12:30']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.strictEqual(s.grey, false);
  // одна и та же дата в обоих списках считается один раз
  assert.ok(K.backfillStatus(bf({ problem_dates: prob, hub_problem_dates: [Object.assign({}, hub[0], { date: '2026-10-13' })] })).text.endsWith('· 2 не сходятся'));
});

t('остались только проблемные даты — серый бейдж; ничего не осталось — индикатора нет', () => {
  const s = K.backfillStatus(bf({ todo: 0, done: 202, failed: 2, problem_dates: prob }));
  assert.strictEqual(s.text, '⟳ догрузка истории 202 из 204 · 2 не сходятся');
  assert.strictEqual(s.grey, true);
  assert.ok(s.title.includes('Остались только даты, которые не сходятся'), s.title);
  assert.strictEqual(K.backfillStatus(bf({ todo: 0, failed: 0, done: 204 })), null);
  assert.strictEqual(K.backfillStatus(null), null);
  // только даты хаба (бэкфилл выключен или досчитан) — индикатор есть, серый
  const h = K.backfillStatus(bf({ enabled: false, todo: 0, failed: 0, done: 204, hub_problem_dates: hub }));
  assert.ok(h && h.grey && h.text.endsWith('· 1 не загружается') && h.title.includes('16.10.2026'), h && h.text);
  assert.strictEqual(K.backfillStatus(bf({ enabled: false, problem_dates: prob })), null);  // бэкфилл выключен
});

t('#447: догрузка стоит на сети — «стоит (сеть)», причина и следующая попытка в подсказке, сетевые ошибки не «не сходятся»', () => {
  const stalled = { error: 'TimeoutError: timed out', date: '2026-10-09', source: 'network', since: '2026-10-23T06:00:00+00:00',
    at: '2026-10-23T09:00:00+00:00', next_attempt: '2026-10-23T09:01:00+00:00' };
  const s = K.backfillStatus(bf({ failed: 0, total: 199, stalled }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 199 · стоит (сеть)');
  assert.ok(!s.text.includes('не сходя') && !s.title.includes('Не сходятся'), s.title);
  ['Догрузка стоит: TimeoutError: timed out', 'сети или хаба', 'с 23.10 09:00', 'Следующая попытка ≈ 23.10 12:01']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.strictEqual(s.grey, false);
  // база
  assert.ok(K.backfillStatus(bf({ failed: 0, stalled: Object.assign({}, stalled, { source: 'store', next_attempt: null }) })).text.endsWith('· стоит (база)'));
  // дата, которая не загружается сама по себе (другие грузятся), — «не загружается», а не «не сходится»
  const own = { date: '2026-10-14', kind: 'unreachable', why: 'no default run', error: 'HubError: bundle: 502 bad gateway', attempts: 2 };
  const u = K.backfillStatus(bf({ problem_dates: [prob[1], own], hub_problem_dates: hub }));
  assert.strictEqual(u.text, '⟳ догрузка истории 190 из 204 · 1 не сходится · 2 не загружаются');
  assert.ok(/Не сходятся \(1\):\n13\.10\.2026/.test(u.title) && /Не загружаются \(2\):\n14\.10\.2026 — не загружается: HubError: bundle: 502 bad gateway, попыток: 2\n16\.10\.2026 — с хаба не загружается/.test(u.title), u.title);
  // остановка без дат к расчёту не показывается
  assert.strictEqual(K.backfillStatus(bf({ todo: 0, failed: 0, done: 204, stalled })), null);
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
console.log(failed ? failed + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
