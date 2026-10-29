// #23 / #163 / #447: индикатор догрузки истории в шапке мониторов (prototypes/concepts/lib/kit.js, K.backfillStatus по
// GET /api/v1/backfill/status): «догрузка истории N из M дат · K не сходятся · L не загружаются», подсказка с датами и
// причинами (не сходятся — свои проблемы дат; не загружаются — дата раз за разом упирается в сеть или базу и даты хаба:
// ошибка, попытки, следующая попытка), серый, когда остались только проблемные даты; без проблемных дат — без них.
// Застой (stall: одна и та же ошибка сети или базы на датах подряд) — «⚠ догрузка стоит: <причина>, повтор в ЧЧ:ММ», в подсказке
// полный текст ошибки и следующая попытка; сетевые ошибки «не сходятся» не называются.
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
  vm.runInContext(P('desk-backfill-text.js'), ctx);  // подключает lib/boot.js раньше kit.js
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

t('идёт догрузка, проблемных дат нет: «догрузка истории N из M дат», без «не сходятся», не серый', () => {
  const s = K.backfillStatus(bf({ failed: 0, total: 199 }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 199 дат');
  assert.ok(!s.text.includes('не сходятся') && !s.title.includes('Не сходятся'));
  assert.strictEqual(s.grey, false);
  assert.ok(s.title.includes('09.10.2026') && s.title.includes('≈ 9 мин'), s.title);  // следующие даты и ETA
});

t('с проблемными датами бэкфилла и хаба: «· K не сходятся · L не загружается», в подсказке даты, причины, попытки, следующая попытка', () => {
  const s = K.backfillStatus(bf({ problem_dates: prob, hub_problem_dates: hub }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 204 дат · 2 не сходятся · 1 не загружается');
  assert.ok(s.title.includes('Не сходятся (2)') && s.title.includes('Не загружаются (1)'), s.title);
  ['08.10.2026', 'ValueError: bad bundle', '13.10.2026', 'не сходится', 'попыток: 2',
    '16.10.2026', 'с хаба не загружается', 'SnapshotError: digest mismatch', 'попыток: 3', 'следующая попытка 23.10 12:30']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.strictEqual(s.grey, false);
  // одна и та же дата в обоих списках считается один раз
  assert.ok(K.backfillStatus(bf({ problem_dates: prob, hub_problem_dates: [Object.assign({}, hub[0], { date: '2026-10-13' })] })).text.endsWith('· 2 не сходятся'));
});

t('остались только проблемные даты — серый бейдж; ничего не осталось — индикатора нет', () => {
  const s = K.backfillStatus(bf({ todo: 0, done: 202, failed: 2, problem_dates: prob }));
  assert.strictEqual(s.text, '⟳ догрузка истории 202 из 204 дат · 2 не сходятся');
  assert.strictEqual(s.grey, true);
  assert.ok(s.title.includes('Остались только проблемные даты') && s.title.includes('когда изменятся данные или программа'), s.title);
  assert.ok(!s.title.includes('по расписанию'), s.title);
  assert.strictEqual(K.backfillStatus(bf({ todo: 0, failed: 0, done: 204 })), null);
  assert.strictEqual(K.backfillStatus(null), null);
  // только даты хаба (бэкфилл выключен или досчитан) — индикатор есть, серый
  const h = K.backfillStatus(bf({ enabled: false, todo: 0, failed: 0, done: 204, hub_problem_dates: hub }));
  assert.ok(h && h.grey && h.text.endsWith('дат · 1 не загружается') && h.title.includes('16.10.2026'), h && h.text);
  assert.ok(h.title.includes('пробует снова сам, по расписанию') && !h.title.includes('изменятся данные'), h.title);  // у них повтор по расписанию
  assert.strictEqual(K.backfillStatus(bf({ enabled: false, problem_dates: prob })), null);  // бэкфилл выключен
});

t('#447: дата, которая раз за разом упирается в сеть (kind transient), — «не загружается», не «не сходится»', () => {
  const s = K.backfillStatus(bf({ problem_dates: [prob[0], { date: '2026-10-09', why: 'no default run', kind: 'transient',
    error: 'TransportError: timed out', attempts: 3 }] }));
  assert.strictEqual(s.text, '⟳ догрузка истории 190 из 204 дат · 1 не сходится · 1 не загружается');
  assert.ok(s.title.includes('09.10.2026 — не загружается (сеть или база): TransportError: timed out, попыток: 3'), s.title);
  assert.strictEqual(s.stall, false);
});

t('#447: застой — коротко «⚠ догрузка стоит: <причина>, повтор в ЧЧ:ММ · N из M дат», в подсказке с какого времени, ошибка, проблемные даты', () => {
  const day = new Date(Date.now() + 3 * 3600e3).toISOString().slice(0, 10);  // сегодня по МСК: время без даты
  const stall = { cause: 'hub', status: 502, phase: 'bundle', error: 'HubUnavailable: hub.test unreachable after 3 attempts: 502 bad gateway', dates: 2, stops: 3,
    since: day + 'T06:00:00+00:00', at: day + 'T08:00:00+00:00', next_attempt: day + 'T09:00:00+00:00' };
  const s = K.backfillStatus(bf({ done: 48, total: 206, todo: 156, failed: 0, stall, problem_dates: prob.slice(1), hub_problem_dates: hub }));
  assert.strictEqual(s.text, '⚠ догрузка стоит: хаб не отвечает, повтор в 12:00 · 48 из 206 дат');  // счётчики — в подсказке
  assert.ok(s.title.startsWith('Стоит с 09:00: хаб не отвечает. Следующая попытка 12:00. Ошибка: HubUnavailable: hub.test unreachable after 3 attempts: 502 bad gateway\n'), s.title);
  ['Готово 48 из 206 дат', 'Не сходятся (1)', '13.10.2026', 'Не загружаются (1)', '16.10.2026'].forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.ok(!s.title.includes('(код') && !s.title.includes('подряд'), s.title);
  assert.strictEqual(s.grey, false);
  assert.strictEqual(s.stall, true);
  // код ответа, которого нет в тексте ошибки, — в подсказке, не в чипе; другие причины
  const c = K.backfillStatus(bf({ stall: Object.assign({}, stall, { error: 'HubUnavailable: hub.test unreachable after 3 attempts' }) }));
  assert.ok(c.title.includes('(код 502)') && !c.text.includes('502'), c.text);
  assert.ok(K.backfillStatus(bf({ stall: Object.assign({}, stall, { cause: 'store_slow' }) })).text.startsWith('⚠ догрузка стоит: база не успевает, повтор в 12:00'));
  assert.ok(K.backfillStatus(bf({ stall: Object.assign({}, stall, { cause: 'access' }) })).text.startsWith('⚠ догрузка стоит: нет доступа к хабу (токен)'));
  // не сегодня — с датой; дат к расчёту в кэше нет, а застой загрузки есть — чип есть
  const o = K.backfillStatus(bf({ todo: 0, failed: 0, done: 204, stall: Object.assign({}, stall, { next_attempt: '2026-10-23T09:00:00+00:00' }) }));
  assert.ok(o && o.text.startsWith('⚠ догрузка стоит: хаб не отвечает, повтор в 23.10 12:00') && !o.grey, o && o.text);
  assert.strictEqual(K.backfillStatus(bf({ enabled: false, stall })), null);  // бэкфилл выключен
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
console.log(failed ? failed + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
