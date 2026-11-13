// TDM#732: файлы сделок прошлых дат, которые ждут блокировки расчёта, видны в строке «Дальше» раньше «Дальше: сейчас»,
// с причиной (GET /api/v1/adapter-services/history, поля pending и pending_why; слова — desk-backfill-text.js, T.history).
//   node tests/js/history_pending_732.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-backfill-text.js'), 'utf8');
const ctx = {window: {}};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx);
const T = ctx.window.DESK_BACKFILL_TEXT;
const HIST = {available: true, enabled: true, paused: false, waiting: null, gate: null, next_at: null, current: null, pending: 0,
  pending_why: null, dates: 10, counts: {fetched: 6, no_trades: 0, unmapped: 0, unavailable: 0, partial: 0, todo: 4}, problems: [],
  applied: null};

const tests = [];
const t = (name, f) => tests.push([name, f]);

t('files that wait are shown before «Дальше: сейчас», with the reason', () => {
  const w = T.history({...HIST, pending: 1, pending_why: 'calculation_busy'});
  assert.strictEqual(w.next, 'Файлы ждут окончания расчёта: ждёт блокировки расчёта, файлы принимаются между датами пересчёта истории');
  assert.ok(T.history({...HIST, window: {left: 7, total: 10}, pending: 2}).next.startsWith('Файлы ждут окончания расчёта (2 даты)'));
});

t('without waiting files it is «Дальше: сейчас» as before; a reason is not invented', () => {
  assert.strictEqual(T.history({...HIST}).next, 'Дальше: сейчас');
  assert.strictEqual(T.history({...HIST, pending: 3}).next, 'Файлы ждут окончания расчёта (3 даты)');
  assert.strictEqual(T.history({...HIST, counts: {...HIST.counts, todo: 0}, pending: 5, pending_why: 'x'}).next, 'Файлы ждут окончания расчёта (5 дат)');
});

t('the answer of the older server (pending is a flag) reads as one date', () => {
  assert.strictEqual(T.history({...HIST, pending: true}).next, 'Файлы ждут окончания расчёта');
});

t('a request in progress, a pause or a wait for a login still come first', () => {
  assert.strictEqual(T.history({...HIST, pending: 1, current: '2026-10-15'}).next, 'Сейчас: 15.10');
  assert.ok(/пауза между запросами/.test(T.history({...HIST, pending: 1, gate: 'gap', next_at: '2026-10-16T08:00:00Z'}).next));
  assert.strictEqual(T.history({...HIST, pending: 1, paused: true, waiting: 'login_required'}).next, T.HIST_WAIT.login_required);
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
process.exit(failed ? 1 : 0);
