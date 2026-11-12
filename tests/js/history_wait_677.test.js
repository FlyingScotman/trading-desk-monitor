// TDM#677: догрузка сделок прошлых дат ждёт, пока «Сделки дня» не принесут хоть одну сделку за последние 5 рабочих дней: блок прошлых дат говорит почему
// (gate no_trades_week в GET /api/v1/adapter-services/history, слова — desk-backfill-text.js, T.history), а даты, помеченные
// «пусто по выбранным портфелям», названы в диагностике с кодом filtered_empty.
//   node tests/js/history_wait_677.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-backfill-text.js'), 'utf8');
const ctx = {window: {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = ctx.window.DESK_BACKFILL_TEXT;
const HIST = {available: true, enabled: true, paused: false, waiting: null, gate: null, next_at: null, current: null, pending: false,
  dates: 10, counts: {fetched: 0, no_trades: 0, unmapped: 0, unavailable: 0, partial: 0, todo: 10}, problems: [], applied: null};

const tests = [];
const t = (name, f) => tests.push([name, f]);

t('the wait for the trades of the week is said in words, with no time to come back at', () => {
  const w = T.history({...HIST, gate: 'no_trades_week'});
  assert.strictEqual(w.next, 'ждёт сделок: «Сделки дня» не принесли ни одной сделки за последние 5 рабочих дней (пока их нет, прошлые даты не запрашиваются)');
  assert.ok(!/дальше в/.test(w.next));
});

t('without the gate the block goes on as before', () => {
  assert.strictEqual(T.history({...HIST}).next, 'Дальше: сейчас');
  assert.strictEqual(T.history({...HIST, enabled: false, gate: 'no_trades_week'}).next, 'выключено');
  assert.strictEqual(T.history({...HIST, current: '2026-10-14', gate: 'no_trades_week'}).next, 'Сейчас: 14.10');
});

t('a date empty by the chosen portfolios is a problem with its own words', () => {
  const h = {...HIST, counts: {...HIST.counts, unmapped: 1, todo: 9},
    problems: [{date: '2026-10-14', state: 'unmapped', code: 'filtered_empty', attempts: 0, next_at: null}]};
  assert.deepStrictEqual(T.history(h).problems, ['14.10 — не сопоставлено: пусто по выбранным портфелям; повтор после новой версии адаптера']);
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
console.log(failed ? failed + ' of ' + tests.length + ' failed' : 'all ' + tests.length + ' passed');
process.exit(failed ? 1 : 0);
