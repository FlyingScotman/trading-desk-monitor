// Инструменты адаптера на странице настроек (prototypes/settings-adapter.js, TDM#421), чистая часть: сводка комплекта
// проверки таблицей (версии, по источнику и дате: единицы, коды замечаний с числом и долей строк; «<10» как есть),
// судьба файлов действия во входящих, причины неудачи простыми словами.
//   node tests/js/settings_adapter.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-adapter.js'), 'utf8'), ctx);
const A = ctx.window.DeskAdapterTools._test;
const plain = v => JSON.parse(JSON.stringify(v));
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

t('доля замечаний: только когда оба числа известны', () => {
  assert.strictEqual(A.share(40, 400), '10% строк');
  assert.strictEqual(A.share(3, 400), '0,8% строк');
  assert.strictEqual(A.share(1, 100000), '<0,1% строк');
  assert.strictEqual(A.share('<10', 400), '');
  assert.strictEqual(A.share(5, '<10'), '');
  assert.strictEqual(A.share(5, 0), '');
});

t('сводка комплекта: шапка, файлы, строки по источнику', () => {
  const rep = { backend: '0.2.10', spi: 1, contract: 'private/1.5', name: 'x', version: '1', fingerprint: 'f'.repeat(64), instruments: 'stub', calendar: 'none',
    files: { matched: 12, ignored: '<10', match_failed: 0 }, match_seconds: 0.4,
    sources: [{ source: 's', report_date: '2026-10-20', valid_date: null, files: 12, rows: 400, units: { accepted: 11, rejected: '<10' }, statuses: {},
      issues: [{ severity: 'warning', code: 'unknown_book', count: 40 }, { severity: 'info', code: 'x', count: '<10' }], controls: [], unmapped: 0,
      unmapped_share_pct: { USD: 1.5 } }] };
  assert.deepStrictEqual(plain(A.kitHead(rep)).map(x => x[0]), ['Бэкенд', 'SPI', 'Контракт', 'Адаптер', 'Отпечаток', 'Справочник инструментов', 'Календарь']);
  assert.ok(A.kitHead(rep)[4][1].endsWith('…') && A.kitHead(rep)[4][1].length === 17);
  assert.strictEqual(A.kitFiles(rep), 'файлов узнано: 12, не узнано: <10, ошибок распознавания: 0 · распознавание 0,4 с');
  const [r] = A.kitRows(rep);
  assert.strictEqual(r.valid_date, 'ждёт календарь');
  assert.strictEqual(r.units, 'принято 11, отклонено <10');
  assert.strictEqual(r.unmapped, '0 (1,5% номинала USD)');
  assert.deepStrictEqual(plain(r.issues), [{ severity: 'warning', code: 'unknown_book', count: '40', share: '10% строк' },
    { severity: 'info', code: 'x', count: '<10', share: '' }]);
});

t('входящие и неудачи — по-русски', () => {
  assert.deepStrictEqual(plain(A.inbox({ status: 'accepted' })), ['ok', 'принят']);
  assert.deepStrictEqual(plain(A.inbox(null)), ['', 'ещё не прочитан']);
  assert.deepStrictEqual(plain(A.inbox({ status: 'new_status' })), ['', 'new_status']);
  assert.strictEqual(A.failText({ error_code: 'timeout', error: 'run_action() did not finish in time: timeout' }),
    'не уложилось в отведённое время (run_action() did not finish in time: timeout)');
  assert.strictEqual(A.failText({ error_code: 'interrupted' }), 'сервер перезапускался во время работы');
  assert.strictEqual(A.kb(2048), '2 КБ');
  assert.strictEqual(A.seconds('2026-10-28T10:00:00Z', Date.parse('2026-10-28T10:00:42Z')), 42);
});

console.log(n + ' tests ok');
