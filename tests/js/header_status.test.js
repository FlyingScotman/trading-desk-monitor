// #22: шапка мониторов (prototypes/concepts/lib/kit.js) — дата и тип данных прогона, «следующее обновление» и
// предупреждение, если в рабочий день после 10:15 МСК нет сегодняшнего внутридневного среза, с причиной из шага
// intraday автопилота (GET /api/v1/autopilot/status, steps[].reason). Чистые функции K.dataLabel и K.headerStatus.
//   node tests/js/header_status.test.js
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
const msk = s => new Date(s + '+03:00');  // момент по московскому времени
// статус автопилота: шаг intraday с причиной, последний показываемый прогон
const st = (reason, o) => Object.assign({
  enabled: true, state: 'idle', last_cycle_at: '2026-10-23T08:10:00+00:00', message: '',
  steps: reason === undefined ? [] : [{ name: 'sync', status: 'ok', detail: '', at: '' }, { name: 'intraday', status: 'skipped', detail: 'd', reason, at: '' }],
  latest_run: { run_id: '7', as_of: '2026-10-22', run_type: 'EOD', cut_type: 'eod_final', cut_ts: null, label: '22.10 · закрытие' }
}, o || {});

t('тип данных: окончательное и предварительное закрытие, внутридневной срез со временем по МСК', () => {
  assert.strictEqual(K.dataLabel({ asOf: '2026-10-22', cutType: 'eod_final' }).text, '22.10 · окончательное закрытие');
  assert.strictEqual(K.dataLabel({ asOf: '2026-10-23', cutType: 'eod_preliminary' }).text, '23.10 · предварительное закрытие');
  assert.strictEqual(K.dataLabel({ asOf: '2026-10-23', cutType: 'intraday', cutTs: '2026-10-23T08:30:00+00:00' }).text, '23.10 · внутридневной срез 11:30');
  assert.strictEqual(K.dataLabel({ asOf: '2026-10-23', cutType: 'intraday' }).text, '23.10 · внутридневной срез');
});

t('рынок старше позиций (утренний прогон на закрытии D−1): видны обе даты', () => {
  const L = K.dataLabel({ asOf: '2026-10-23', cutType: 'eod_final', marketDate: '2026-10-22' });
  assert.strictEqual(L.text, 'позиции 23.10 · рынок 22.10 · окончательное закрытие');
  assert.strictEqual(L.date, '23.10'); assert.strictEqual(L.market, '22.10');
  assert.strictEqual(K.dataLabel({ asOf: '2026-10-22', cutType: 'eod_final', marketDate: '2026-10-22' }).market, null);
});

t('утренний прогон (cutType prev_close, #21): тип закрытия из marketCutType, без сырого кода', () => {
  const L = o => K.dataLabel(Object.assign({ asOf: '2026-10-23', cutType: 'prev_close', marketDate: '2026-10-22' }, o)).text;
  assert.strictEqual(L({ marketCutType: 'eod_final' }), 'позиции 23.10 · рынок 22.10 · окончательное закрытие');
  assert.strictEqual(L({ marketCutType: 'eod_preliminary' }), 'позиции 23.10 · рынок 22.10 · предварительное закрытие');
  assert.strictEqual(L({}), 'позиции 23.10 · рынок 22.10 · закрытие прошлого дня');
  assert.ok(!L({}).includes('prev_close'));
});

t('предупреждение: рабочий день после 10:15 без сегодняшнего среза — причина из шага intraday', () => {
  const now = msk('2026-10-23T10:20:00');  // пятница
  const W = r => K.headerStatus(st(r), now).warn;
  assert.ok(W('hub_error').text.startsWith('нет внутридневного среза за 23.10'), W('hub_error').text);
  const cases = { hub_error: 'хаб не ответил', no_cut_yet: 'на хабе ещё нет среза', run_failed: 'расчёт упал',
    failed_before: 'расчёт упал', off: 'внутридневной расчёт выключен', no_positions: 'нет позиций', error: 'ошибка автопилота' };
  Object.entries(cases).forEach(([r, txt]) => assert.ok(W(r).text.includes(txt), r + ': ' + W(r).text));
  assert.ok(K.headerStatus(st('hub_error', { enabled: false }), now).warn.text.includes('автопилот не работает'));
  assert.ok(K.headerStatus(null, now).warn.text.includes('сервер'), 'статус не получен');
  assert.ok(K.headerStatus(st(undefined, { steps: [{ name: 'store', status: 'error', detail: 'x', at: '' }] }), now).warn.text.includes('баз'));
  assert.ok(K.headerStatus(st(undefined, { steps: [{ name: 'sync', status: 'ok', detail: '', at: '' }] }), now).warn.text.includes('хаб не настроен'));
  // цикл идёт (шагов ещё нет): причина прошлого цикла
  assert.ok(K.headerStatus(st(undefined, { state: 'running' }), now, { lastReason: 'no_cut_yet' }).warn.text.includes('на хабе ещё нет среза'));
  // давно не было цикла
  assert.ok(K.headerStatus(st('no_cut_yet', { last_cycle_at: '2026-10-23T05:00:00+00:00' }), now).warn.text.includes('автопилот не запускался с 08:00'));
});

t('предупреждения нет: до 10:15, в выходной, в нерабочий день хаба и при сегодняшнем срезе', () => {
  assert.strictEqual(K.headerStatus(st('no_cut_yet'), msk('2026-10-23T10:10:00')).warn, null);
  assert.strictEqual(K.headerStatus(st('no_cut_yet'), msk('2026-10-24T12:00:00')).warn, null);  // суббота
  assert.strictEqual(K.headerStatus(st('not_business_day'), msk('2026-10-23T12:00:00')).warn, null);
  const today = { run_id: '8', as_of: '2026-10-23', run_type: 'INTRADAY', cut_type: 'intraday', cut_ts: '2026-10-23T08:30:00+00:00' };
  assert.strictEqual(K.headerStatus(st('up_to_date', { latest_run: today }), msk('2026-10-23T12:00:00')).warn, null);
  const prelim = Object.assign({}, today, { run_type: 'EOD', cut_type: 'eod_preliminary' });
  assert.strictEqual(K.headerStatus(st('up_to_date', { latest_run: prelim }), msk('2026-10-23T20:30:00')).warn, null);
  // утренний прогон на закрытии вчера (as_of сегодня, срез eod_final) — не внутридневной: после 10:15 предупреждение
  const morning = Object.assign({}, today, { run_type: 'EOD', cut_type: 'eod_final', cut_ts: null });
  assert.ok(K.headerStatus(st('no_cut_yet', { latest_run: morning }), msk('2026-10-23T10:30:00')).warn);
});

t('следующее обновление: ≈ 10:05 утром, следующий 10-минутный слот днём, завтра после предварительного закрытия', () => {
  const N = (s, now, o) => K.headerStatus(s, msk(now), o).next;
  assert.strictEqual(N(st('before_open'), '2026-10-23T07:40:00'), 'следующее обновление ≈ 10:05');
  assert.strictEqual(N(st('no_cut_yet'), '2026-10-23T11:31:00'), 'следующее обновление ≈ 11:35');
  assert.strictEqual(N(st('no_cut_yet'), '2026-10-23T11:35:00'), 'следующее обновление ≈ 11:45');
  assert.strictEqual(N(st('no_cut_yet'), '2026-10-23T19:40:00'), 'следующее обновление ≈ 19:45');
  const prelim = { run_id: '9', as_of: '2026-10-23', run_type: 'EOD', cut_type: 'eod_preliminary', cut_ts: null };
  assert.strictEqual(N(st('up_to_date', { latest_run: prelim }), '2026-10-23T20:10:00'), 'следующее обновление ≈ пн 26.10 10:05');
  assert.strictEqual(N(st('not_business_day'), '2026-10-24T12:00:00'), 'следующее обновление ≈ пн 26.10 10:05');
  assert.strictEqual(N(st('no_cut_yet'), '2026-10-22T21:00:00'), 'следующее обновление ≈ 21:05');  // до среза предв. закрытия
  // расписание с сервера (status.schedule) важнее значений по умолчанию
  const sch = { schedule: { intraday_from: '10:00', prelim_from: '19:45', poll_slot_s: 1800, poll_lag_s: 300, warn_after: '10:15' } };
  assert.strictEqual(N(st('no_cut_yet', sch), '2026-10-23T11:31:00'), 'следующее обновление ≈ 11:35');
  assert.strictEqual(N(st('no_cut_yet', sch), '2026-10-23T11:36:00'), 'следующее обновление ≈ 12:05');
  assert.strictEqual(N(st('off'), '2026-10-23T11:31:00'), 'внутридневное обновление выключено');
  assert.strictEqual(N(st('hub_error', { enabled: false }), '2026-10-23T11:31:00'), 'автообновление выключено');
  assert.strictEqual(K.headerStatus(null, msk('2026-10-23T11:31:00')).next, null);
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
console.log(failed ? failed + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
