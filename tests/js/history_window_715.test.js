// TDM#715: после исправления адаптера монитор сам заново запрашивает уже полученные даты; сводка «Сделки прошлых дат» говорит,
// сколько дат осталось (GET /api/v1/adapter-services/history, поле window; слова — desk-backfill-text.js, T.history).
//   node tests/js/history_window_715.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-backfill-text.js'), 'utf8');
const ctx = {window: {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = ctx.window.DESK_BACKFILL_TEXT;
const HIST = {available: true, enabled: true, paused: false, waiting: null, gate: null, next_at: null, current: null, pending: false,
  dates: 10, counts: {fetched: 10, no_trades: 0, unmapped: 0, unavailable: 0, partial: 0, todo: 0}, problems: [], applied: null};

const tests = [];
const t = (name, f) => tests.push([name, f]);

t('the summary says how many received dates are still to be asked again', () => {
  const w = T.history({...HIST, window: {left: 7, total: 10}});
  assert.ok(w.summary.endsWith('. Исправление адаптера: сделки уже полученных дат запрашиваются заново, осталось 7 из 10'), w.summary);
  assert.strictEqual(w.next, 'Дальше: сейчас');
});

t('without the field, or with nothing left, the summary is as before', () => {
  assert.ok(!/Исправление адаптера/.test(T.history({...HIST}).summary));
  assert.ok(!/Исправление адаптера/.test(T.history({...HIST, window: null}).summary));
  assert.strictEqual(T.history({...HIST}).next, 'Все даты получены');
});

t('with the fetch of new dates switched off the dates received are still asked again, and the block says so', () => {
  const w = T.history({...HIST, enabled: false, window: {left: 3, total: 3}});
  assert.strictEqual(w.next, 'догрузка новых дат выключена; уже полученные даты запрашиваются заново');
  assert.strictEqual(T.history({...HIST, enabled: false}).next, 'выключено');
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
process.exit(failed ? 1 : 0);
