// «ОВП: типы строк отчёта» (prototypes/settings-ocp-types.js, TDM#554), чистая часть: начальный выбор (сохранённый вид,
// иначе подсказка у типа с будущими строками), что запишет кнопка, предупреждения о двойном счёте и о подсказке,
// порядок и фильтры, когда запускать действие самому, ход «Сохранить и пересчитать». Метки вымышленные.
//   node tests/js/settings_ocp_types.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-ocp-types.js'), 'utf8'), ctx);
const T = ctx.window.DeskOcpTypes._test;
const plain = v => JSON.parse(JSON.stringify(v));
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

const CH = {
  options: [{ value: null, title: 'не дериватив (оставить в отчёте)' }, { value: 'fx_forward', title: 'валютный форвард' },
    { value: 'fx_swap', title: 'FX-своп' }, { value: 'fx_future', title: 'валютный фьючерс' }],
  risky_unset: ['fx_future'],
  rows: [
    { key: 'CASH', value: null, suggested: null, future: 0, cells: { lines: 50 } },
    { key: 'SWAP A', value: 'fx_swap', suggested: 'fx_swap', future: 9, cells: { lines: 20 } },
    { key: 'FUT', value: null, suggested: 'fx_future', future: 4, hint: 'по названию типа', cells: { lines: 6 } },
    { key: 'SWAP B', value: null, suggested: 'fx_swap', future: 3, hint: 'по подтипам', cells: { lines: 5 }, new: true },
    { key: 'OLD', value: 'fx_forward', suggested: null, future: 0, cells: { lines: 2 } }]
};

t('начальный выбор: сохранённое, иначе подсказка у типа с будущими строками', () => {
  assert.deepStrictEqual(plain(T.initial(CH)), { CASH: null, 'SWAP A': 'fx_swap', FUT: 'fx_future', 'SWAP B': 'fx_swap', OLD: 'fx_forward' });
  assert.deepStrictEqual(plain(T.saved(CH)), { CASH: null, 'SWAP A': 'fx_swap', FUT: null, 'SWAP B': null, OLD: 'fx_forward' });
});

t('кнопка записывает только отличия от сохранённого, в том числе снятие вида', () => {
  const sel = T.initial(CH);
  assert.deepStrictEqual(plain(T.changes(CH, sel)), { FUT: 'fx_future', 'SWAP B': 'fx_swap' });
  sel.OLD = null; sel.FUT = null;
  assert.deepStrictEqual(plain(T.changes(CH, sel)), { 'SWAP B': 'fx_swap', OLD: null });
  assert.deepStrictEqual(plain(T.changes(CH, T.saved(CH))), {});
});

t('двойной счёт: вид вне отчёта или назначенный другому типу; подсказка — «проверьте»', () => {
  const sel = T.saved(CH);
  const fut = T.warnings(CH, sel, CH.rows[2]);
  assert.strictEqual(fut.length, 1);
  assert.strictEqual(fut[0].level, 'bad');
  assert.ok(/Риск двойного счёта: похоже на «валютный фьючерс»/.test(fut[0].text) && /4 с датой позже/.test(fut[0].text));
  const b = T.warnings(CH, sel, CH.rows[3]);
  assert.ok(b[0].level === 'bad' && /назначен другому типу/.test(b[0].text));  // SWAP A has fx_swap
  sel['SWAP A'] = null;
  assert.deepStrictEqual(plain(T.warnings(CH, sel, CH.rows[3])), []);  // nobody has fx_swap now: no risk to name
  const init = T.initial(CH);
  assert.deepStrictEqual(plain(T.warnings(CH, init, CH.rows[2])), [{ level: 'warn', text: 'Предложено по подсказке (по названию типа) — проверьте.' }]);
  assert.deepStrictEqual(plain(T.warnings(CH, init, CH.rows[0])), []);
  assert.strictEqual(T.warnings(CH, init, CH.rows[4])[0].level, '');  // a kind with no future line: said, not a warning
});

t('порядок и фильтры: сначала типы с будущими строками; «требуют внимания» — предупреждения и новые', () => {
  assert.deepStrictEqual(T.order(CH.rows).map(r => r.key), ['SWAP A', 'FUT', 'SWAP B', 'CASH', 'OLD']);
  assert.deepStrictEqual(T.filter(CH, T.saved(CH), CH.rows, 'future').map(r => r.key), ['SWAP A', 'FUT', 'SWAP B']);
  assert.deepStrictEqual(T.filter(CH, T.saved(CH), CH.rows, 'attention').map(r => r.key), ['FUT', 'SWAP B']);
  assert.deepStrictEqual(T.filter(CH, T.initial(CH), CH.rows, 'attention').map(r => r.key), ['FUT', 'SWAP B']);
  assert.strictEqual(T.filter(CH, {}, CH.rows, 'all').length, 5);
});

t('действие запускается само: нет результата, файл менялся, результат старый; не во время работы', () => {
  const now = Date.parse('2026-11-03T12:00:00Z');
  assert.strictEqual(T.needsRun({ choices: null }, now), true);
  assert.strictEqual(T.needsRun({ choices: {}, stale: true, job: { finished_at: '2026-11-03T11:59:00Z' } }, now), true);
  assert.strictEqual(T.needsRun({ choices: {}, stale: false, job: { finished_at: '2026-11-03T11:00:00Z' } }, now), false);
  assert.strictEqual(T.needsRun({ choices: {}, stale: false, job: { finished_at: '2026-11-03T05:00:00Z' } }, now), true);
  assert.strictEqual(T.needsRun({ choices: null, running: true }, now), false);
});

t('ход «Сохранить и пересчитать»: ожидание скана, перечитывание, пересчёт, итог', () => {
  const saved = Date.parse('2026-11-03T12:00:00Z');
  assert.ok(/сразу после него/.test(T.progress({ state: 'running', waiting: true, request: { to: '2026-11-02' } }, null, saved).text));
  assert.strictEqual(T.progress({ state: 'running', request: { to: '2026-11-02' } }, null, saved).text, 'Перечитываю отчёт за 02.11.2026…');
  const done = { state: 'done', finished_at: '2026-11-03T12:00:05Z', result: { revisions: 1 } };
  assert.strictEqual(T.progress(done, null, saved).state, 'recalc');
  assert.strictEqual(T.progress(done, { enabled: true, state: 'running', latest_run: { created_at: '2026-11-03T11:00:00Z' } }, saved).state, 'recalc');
  assert.deepStrictEqual(plain(T.progress(done, { enabled: true, latest_run: { created_at: '2026-11-03T12:01:00Z', label: '03.11 15:00 МСК · внутри дня' } }, saved)),
    { state: 'done', text: 'Готово, ОВП пересчитана (расчёт 03.11 15:00 МСК · внутри дня).' });
  assert.ok(/выключено/.test(T.progress(done, { enabled: false }, saved).text));
  assert.ok(/не изменились/.test(T.progress({ state: 'done', result: { revisions: 0 } }, null, saved).text));
  assert.strictEqual(T.progress({ state: 'failed', error: 'x' }, null, saved).state, 'fail');
});

t('числа и доли', () => {
  assert.strictEqual(T.pct(0.25), '25%');
  assert.strictEqual(T.pct(0.004), '<1%');
  assert.strictEqual(T.pct(0), '0%');
  assert.strictEqual(T.pct(null), '—');
  assert.strictEqual(T.dm('2026-11-02'), '02.11.2026');
  assert.deepStrictEqual([1, 2, 5, 11, 21, 24].map(T.types), ['1 тип', '2 типа', '5 типов', '11 типов', '21 тип', '24 типа']);
});

console.log(n + ' tests ok');
