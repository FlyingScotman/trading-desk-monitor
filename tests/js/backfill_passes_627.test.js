// TDM#627: два прохода пересчёта истории словами (GET /api/v1/backfill/status: pass first / second / done, second {done, todo,
// failed, total, next, problem_dates, eta_s}): чип в шапке мониторов (kit.js, K.backfillStatus) и тексты Настроек
// (desk-backfill-text.js, T.passes). «Готово» — только когда pass == done; проблемные даты второго прохода названы; ответ без
// pass (прежний сервер) показывается как раньше.
//   node tests/js/backfill_passes_627.test.js
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
const NOW = Date.parse('2026-10-22T08:30:00Z');
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1', protocol: 'file:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext('Date.now = () => ' + NOW + ';', ctx);  // «сейчас» теста: 22.10 11:30 МСК
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('desk-backfill-text.js'), ctx);  // подключает lib/boot.js раньше kit.js
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return ctx.window.KIT;
}
const K = load();
const T = vm.runInNewContext('var window = {}; ' + P('desk-backfill-text.js') + '; window.DESK_BACKFILL_TEXT');
// статус: первый проход идёт (190 из 199 дат с вторыми производными ещё нет)
const st = o => Object.assign({ enabled: true, from: '2026-01-05', to: '2026-10-23', total: 199, done: 190, todo: 9, failed: 0, next: ['2026-10-09', '2026-10-12'],
  eta_s: 540, problem_dates: [], hub_problem_dates: [], pass: 'first',
  second: { done: 190, todo: 0, failed: 0, total: 190, next: [], problem_dates: [], eta_s: null } }, o || {});
const second = o => st(Object.assign({ todo: 0, done: 199, pass: 'second', eta_s: null,
  second: { done: 100, todo: 99, failed: 0, total: 199, next: ['2026-10-21', '2026-10-20'], problem_dates: [], eta_s: 1500 } }, o || {}));
const done = o => st(Object.assign({ todo: 0, done: 199, pass: 'done', eta_s: null,
  second: { done: 199, todo: 0, failed: 0, total: 199, next: [], problem_dates: [], eta_s: null } }, o || {}));

t('первый проход: «проход 1 из 2: N из M дат», второй ещё не начался, не «готово»', () => {
  const s = K.backfillStatus(st());
  assert.strictEqual(s.text, '⟳ догрузка истории, проход 1 из 2: 190 из 199 дат');
  assert.strictEqual(s.grey, false);
  ['Проход 1 из 2: стоимость, риск и PnL — 190 из 199 дат.', 'Проход 2 из 2: вторые производные (выпуклость, гамма) — начнётся, когда первый проход посчитает все даты.', 'Следующие: 09.10.2026, 12.10.2026.']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.ok(!/готов/.test(s.text) && !/готов/.test(T.passes(st()).second), 'пока идёт первый проход, «готово» нет');
});

t('второй проход: чип остаётся, пока даты досчитываются (todo первого прохода уже 0)', () => {
  const s = K.backfillStatus(second());
  assert.ok(s, 'чип не скрыт при todo == 0, пока идёт второй проход');
  assert.strictEqual(s.text, '⟳ догрузка истории, проход 2 из 2 (вторые производные): 100 из 199 дат');
  assert.strictEqual(s.grey, false);
  ['Проход 1 из 2: стоимость, риск и PnL — 199 из 199 дат, готово.', 'Проход 2 из 2: вторые производные (выпуклость, гамма) — 100 из 199 дат, осталось ≈ 25 мин.',
    'Следующие: 21.10.2026, 20.10.2026.', 'оценка сценариев на этой дате линейная']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  assert.ok(!/готов/.test(s.text));
});

t('готово — только при pass == done: индикатора нет; при дате без вторых производных — серый, дата и причина названы', () => {
  assert.strictEqual(K.backfillStatus(done()), null);
  const bad = done({ second: { done: 197, todo: 0, failed: 2, total: 199, next: [], eta_s: null,
    problem_dates: [{ date: '2026-10-02', error: 'RuntimeError: no run created', attempts: 3 }, { date: '2026-10-05', error: null, attempts: 1 }] } });
  const s = K.backfillStatus(bad);
  assert.ok(s && s.grey, 'остались только проблемные даты');
  assert.strictEqual(s.text, '⟳ догрузка истории 199 из 199 дат · 2 даты без вторых производных');
  ['Вторые производные не посчитались (2):', '02.10.2026 — RuntimeError: no run created, попыток: 3', '05.10.2026 — расчёт упал, попыток: 1', 'Остались только проблемные даты']
    .forEach(x => assert.ok(s.title.includes(x), x + ' нет в «' + s.title + '»'));
  // выключенная догрузка — без чипа, как раньше
  assert.strictEqual(K.backfillStatus(second({ enabled: false })), null);
});

t('проблемные даты первого прохода и второй проход вместе: оба счётчика в чипе', () => {
  const s = K.backfillStatus(second({ failed: 1, problem_dates: [{ date: '2026-10-13', why: 'program updated', error: 'does not converge: x', attempts: 2 }],
    second: { done: 100, todo: 98, failed: 1, total: 199, next: ['2026-10-21'], eta_s: 1500, problem_dates: [{ date: '2026-10-02', error: 'boom', attempts: 2 }] } }));
  assert.strictEqual(s.text, '⟳ догрузка истории, проход 2 из 2 (вторые производные): 100 из 199 дат · 1 не сходится · 1 дата без вторых производных');
  assert.ok(s.title.includes('Не сходятся (1)') && s.title.includes('02.10.2026 — boom, попыток: 2'), s.title);
});

t('ответ без pass (прежний сервер) — тексты как раньше', () => {
  const old = { enabled: true, from: '2026-01-05', total: 199, done: 190, todo: 9, failed: 0, next: ['2026-10-09'], eta_s: 540 };
  assert.strictEqual(T.passes(old), null);
  assert.strictEqual(K.backfillStatus(old).text, '⟳ догрузка истории 190 из 199 дат');
});

t('T.passes: строки Настроек — оба прохода, готово при done, число дат во множественном числе', () => {
  assert.strictEqual(T.passes(done()).first, 'Проход 1 из 2: стоимость, риск и PnL — 199 из 199 дат, готово');
  assert.strictEqual(T.passes(done()).second, 'Проход 2 из 2: вторые производные (выпуклость, гамма) — 199 из 199 дат, готово');
  assert.strictEqual(T.passes(done()).done, true);
  assert.strictEqual(T.passes(second()).done, false);
  assert.strictEqual(T.passes(second()).eta, '25 мин');
  assert.deepStrictEqual([1, 2, 5, 11, 21, 22].map(n => T.plural(n, 'дата', 'даты', 'дат')), ['1 дата', '2 даты', '5 дат', '11 дат', '21 дата', '22 даты']);
  assert.strictEqual(T.eta(5400 + 600), '2 ч');
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
console.log(failed ? failed + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
