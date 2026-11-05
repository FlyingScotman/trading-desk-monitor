// «Индексы ставок» (prototypes/settings-index-codes.js, TDM#565), чистая часть: начальный выбор (сохранённый код, иначе
// подсказка), что запишет кнопка, предупреждения, порядок и фильтры, число ног для точки в меню, когда запускать
// действие самому, ход «Сохранить и пересчитать». Названия индексов вымышленные.
//   node tests/js/settings_index_codes.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-index-codes.js'), 'utf8'), ctx);
const T = ctx.window.DeskIndexCodes._test;
const plain = v => JSON.parse(JSON.stringify(v));
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

const CH = {
  options: [{ value: null, title: 'не выбрано' }, { value: 'KEY_RATE', title: 'Ключевая ставка' }, { value: 'RUSFARCNY', title: 'RUSFAR CNY' }, { value: 'OTHER', title: 'другой индекс' }],
  columns: [{ id: 'legs', title: 'Ног' }, { id: 'ccy', title: 'Валюты' }, { id: 'last', title: 'Последний файл' }],
  rows: [
    { key: 'IDX_FAKE_KEY', label: 'IDX_FAKE_KEY', value: 'KEY_RATE', suggested: 'KEY_RATE', future: 7, cells: { legs: 9, ccy: 'RUB' } },
    { key: 'IDX_FAKE_CNY_ON', label: 'IDX_FAKE_CNY_ON', value: null, suggested: 'RUSFARCNY', hint: 'по названию и валюте ноги', future: 4, new: true, cells: { legs: 4, ccy: 'CNY', last: null } },
    { key: 'IDX_FAKE_UNKNOWN', label: 'IDX_FAKE_UNKNOWN', value: null, suggested: null, future: 2, cells: { legs: 2 } },
    { key: 'IDX_FAKE_OLD', label: 'IDX_FAKE_OLD', value: null, suggested: null, future: 0, cells: { legs: 1 } },
    { key: 'IDX_FAKE_OTHER', label: 'IDX_FAKE_OTHER', value: 'OTHER', suggested: 'OTHER', hint: 'индекс без данных в хабе', future: 3, cells: { legs: 3 } }]
};

t('начальный выбор: сохранённое, иначе подсказка (даже у названия без живых ног)', () => {
  assert.deepStrictEqual(plain(T.initial(CH)), { IDX_FAKE_KEY: 'KEY_RATE', IDX_FAKE_CNY_ON: 'RUSFARCNY', IDX_FAKE_UNKNOWN: null, IDX_FAKE_OLD: null, IDX_FAKE_OTHER: 'OTHER' });
  assert.deepStrictEqual(plain(T.saved(CH)), { IDX_FAKE_KEY: 'KEY_RATE', IDX_FAKE_CNY_ON: null, IDX_FAKE_UNKNOWN: null, IDX_FAKE_OLD: null, IDX_FAKE_OTHER: 'OTHER' });
});

t('кнопка записывает только отличия от сохранённого, в том числе снятие кода', () => {
  const sel = T.initial(CH);
  assert.deepStrictEqual(plain(T.changes(CH, sel)), { IDX_FAKE_CNY_ON: 'RUSFARCNY' });
  sel.IDX_FAKE_KEY = null; sel.IDX_FAKE_CNY_ON = null;
  assert.deepStrictEqual(plain(T.changes(CH, sel)), { IDX_FAKE_KEY: null });
  assert.deepStrictEqual(plain(T.changes(CH, T.saved(CH))), {});
});

t('предупреждения: нет кода, «другой индекс», предложено — проверьте, подсказка адаптера', () => {
  const sel = T.initial(CH);
  assert.deepStrictEqual(plain(T.warnings(CH, sel, CH.rows[0])), []);
  const prop = T.warnings(CH, sel, CH.rows[1]);
  assert.deepStrictEqual(plain(prop), [{ level: 'warn', text: 'Предложено по подсказке (по названию и валюте ноги) — проверьте.' }]);
  const none = T.warnings(CH, sel, CH.rows[2]);
  assert.ok(none[0].level === 'warn' && /«другой индекс», сделка будет «не оценено» \(ног живых сделок: 2\)/.test(none[0].text));
  assert.strictEqual(T.warnings(CH, sel, CH.rows[3])[0].level, '');  // нет живых ног: сказано, но не тревога
  const oth = T.warnings(CH, sel, CH.rows[4]);
  assert.ok(/останется «не оценено»/.test(oth[0].text));
  sel.IDX_FAKE_CNY_ON = 'KEY_RATE';  // выбрано не то, что предложено: подсказка остаётся видна
  assert.ok(T.warnings(CH, sel, CH.rows[1]).some(w => /Подсказка адаптера: по названию/.test(w.text)));
});

t('порядок и фильтры: сначала без кода, затем новые; «требуют внимания» — предупреждения и новые', () => {
  assert.deepStrictEqual(T.order(CH.rows).map(r => r.key), ['IDX_FAKE_CNY_ON', 'IDX_FAKE_UNKNOWN', 'IDX_FAKE_OLD', 'IDX_FAKE_KEY', 'IDX_FAKE_OTHER']);
  const sel = T.initial(CH);
  assert.deepStrictEqual(T.filter(CH, sel, CH.rows, 'open').map(r => r.key), ['IDX_FAKE_CNY_ON', 'IDX_FAKE_UNKNOWN', 'IDX_FAKE_OLD']);
  assert.deepStrictEqual(T.filter(CH, sel, CH.rows, 'attention').map(r => r.key), ['IDX_FAKE_CNY_ON', 'IDX_FAKE_UNKNOWN']);
  assert.strictEqual(T.filter(CH, sel, CH.rows, 'all').length, 5);
});

t('колонки адаптера: только непустые значения', () => {
  assert.deepStrictEqual(plain(T.cells(CH, CH.rows[1])), [{ title: 'Ног', value: '4' }, { title: 'Валюты', value: 'CNY' }]);
});

t('точка в меню: ноги живых сделок у строк без сохранённого кода и сигнал по файлам, что больше', () => {
  assert.strictEqual(T.attention({ choices: CH, signal: null }), 6);  // 4 + 2 + 0
  assert.strictEqual(T.attention({ choices: CH, signal: { count: 11 } }), 11);
  assert.strictEqual(T.attention({ choices: null, signal: { count: 3 } }), 3);
  assert.strictEqual(T.attention({ choices: { rows: [CH.rows[0], CH.rows[4]] }, signal: null }), 0);
  assert.strictEqual(T.attention({ error: 'x' }), 0);
});

t('действие запускается само: нет результата, файл менялся, результат старый; не во время работы', () => {
  const now = Date.parse('2026-11-05T12:00:00Z');
  assert.strictEqual(T.needsRun({ choices: null }, now), true);
  assert.strictEqual(T.needsRun({ choices: {}, stale: true, job: { finished_at: '2026-11-05T11:59:00Z' } }, now), true);
  assert.strictEqual(T.needsRun({ choices: {}, stale: false, job: { finished_at: '2026-11-05T11:00:00Z' } }, now), false);
  assert.strictEqual(T.needsRun({ choices: {}, stale: false, job: { finished_at: '2026-11-05T05:00:00Z' } }, now), true);
  assert.strictEqual(T.needsRun({ choices: null, running: true }, now), false);
});

t('ход «Сохранить и пересчитать»: ожидание скана, перечитывание, пересчёт, итог', () => {
  const saved = Date.parse('2026-11-05T12:00:00Z');
  assert.ok(/сразу после него/.test(T.progress({ state: 'running', waiting: true }, null, saved).text));
  assert.strictEqual(T.progress({ state: 'running', request: { to: '2026-11-04' } }, null, saved).text, 'Перечитываю условия сделок за 04.11.2026…');
  const done = { state: 'done', finished_at: '2026-11-05T12:00:05Z', result: { revisions: 1 } };
  assert.strictEqual(T.progress(done, null, saved).state, 'recalc');
  assert.deepStrictEqual(plain(T.progress(done, { enabled: true, latest_run: { created_at: '2026-11-05T12:01:00Z' } }, saved)),
    { state: 'done', text: 'Готово: условия перечитаны, сделки пересчитаны.' });
  assert.ok(/выключено/.test(T.progress(done, { enabled: false }, saved).text));
  assert.ok(/сделки не изменились/.test(T.progress({ state: 'done', result: { revisions: 0 } }, null, saved).text));
  assert.strictEqual(T.progress({ state: 'failed', error: 'x' }, null, saved).state, 'fail');
});

t('тексты: числа и склонения, без команд терминала', () => {
  assert.deepStrictEqual([1, 2, 5, 11, 21].map(T.legs), ['1 нога', '2 ноги', '5 ног', '11 ног', '21 нога']);
  assert.deepStrictEqual([1, 3, 12].map(T.names), ['1 название', '3 названия', '12 названий']);
  assert.strictEqual(T.dm('2026-11-04'), '04.11.2026');
  const src = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-index-codes.js'), 'utf8');
  assert.ok(!/codegraph|python|pip |\bdesk adapter\b/i.test(src.replace(/\/\*[\s\S]*?\*\//, '')));
});

console.log(n + ' tests ok');
