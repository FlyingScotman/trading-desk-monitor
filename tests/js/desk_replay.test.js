// «Перечитать отчёты» (prototypes/desk-replay.js, TDM#553), чистая часть: подпись источника, оценка объёма, проверка
// периода, итог задания (ожидание скана, ошибки, ревизии).
//   node tests/js/desk_replay.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-replay.js'), 'utf8'), ctx);
const R = ctx.window.DeskReplay._test;
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

t('подпись источника и склонение «файл»', () => {
  assert.strictEqual(R.label({ source: 'ocp_cash', names: ['ОВП', 'Остатки на счетах'], latest: '2026-11-02' }), 'ОВП, Остатки на счетах · ocp_cash · последний отчёт 02.11.2026');
  assert.strictEqual(R.label({ source: 's', names: [], latest: null }), 's · s · отчётов ещё нет');
  assert.deepStrictEqual([1, 2, 5, 11, 21, 22, 112].map(R.files), ['1 файл', '2 файла', '5 файлов', '11 файлов', '21 файл', '22 файла', '112 файлов']);
});

t('оценка и период', () => {
  assert.strictEqual(R.estimate({ files: 1, dates: ['2026-11-02'] }), 'Будет перечитано: 1 файл (за 02.11.2026).');
  assert.strictEqual(R.estimate({ files: 3, dates: ['2026-10-30', '2026-11-02'] }), 'Будет перечитано: 3 файла (с 30.10.2026 по 02.11.2026).');
  assert.ok(/нечего/.test(R.estimate({ files: 0, dates: [] })));
  assert.strictEqual(R.period('2026-11-02', '2026-11-02', 400), null);
  assert.ok(/позже/.test(R.period('2026-11-03', '2026-11-02', 400)));
  assert.ok(/400/.test(R.period('2025-01-01', '2026-11-02', 400)));
  assert.ok(R.period('', '2026-11-02', 400));
});

t('итог задания', () => {
  const req = { source: 'ocp_cash', from: '2026-11-02', to: '2026-11-02' };
  assert.ok(/читает папки — перечитаю ocp_cash за 02.11.2026 сразу после него/.test(R.result({ state: 'running', waiting: true, request: req }).text));
  const ok = R.result({ state: 'done', request: req, result: { files: { accepted: 1 }, revisions: 1 } });
  assert.strictEqual(ok.cls, 'ok');
  assert.strictEqual(ok.text, 'Перечитано ocp_cash за 02.11.2026: 1 файл, новых ревизий — 1. Затронутые расчёты пересчитает автообновление.');
  const same = R.result({ state: 'done', request: { ...req, from: '2026-10-30' }, result: { files: { accepted: 2, rejected: 1 }, revisions: 0 } });
  assert.ok(same.cls === 'warn' && /30.10.2026–02.11.2026: 3 файла/.test(same.text) && /Не принято файлов: 1/.test(same.text));
  assert.ok(/долго был занят/.test(R.result({ state: 'failed', error_code: 'busy', request: req }).text));
  assert.strictEqual(R.result(null), null);
});

console.log(n + ' tests ok');
