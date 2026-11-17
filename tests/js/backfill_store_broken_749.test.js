// TDM#749: внутренняя ошибка базы (backfill.stall_cause → store_broken) — отдельная причина застоя догрузки истории:
// «внутренняя ошибка базы» в строке «стоит: …», а в подсказке — что случилось (даты не помечены неудачными) и что делать
// (перезапустить сервер; проверить копию: Настройки → «Резервные копии» → карточка «Копии»). Слова — desk-backfill-text.js.
//   node tests/js/backfill_store_broken_749.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const tests = [];
const t = (name, f) => tests.push([name, f]);

const T = vm.runInNewContext('var window = {}; ' + P('desk-backfill-text.js') + '; window.DESK_BACKFILL_TEXT');
const NOW = Date.parse('2026-11-16T09:00:00Z');
const stall = o => ({ enabled: true, todo: 50, stalled: Object.assign({ cause: 'store_broken', source: 'store', date: '2026-10-14',
  error: 'InternalError_: unexpected data beyond EOF in block 9469 of relation base/1/2', since: '2026-11-16T08:00:00Z',
  next_attempt: '2026-11-16T09:10:00Z' }, o || {}) });

t('the cause is named and the stall says what to do', () => {
  assert.strictEqual(T.cause({ cause: 'store_broken', source: 'store' }), 'внутренняя ошибка базы');
  const s = T.stall(stall(), NOW);
  assert.strictEqual(s.why, 'внутренняя ошибка базы');
  assert.strictEqual(s.text, 'стоит: внутренняя ошибка базы, повтор в 12:10');
  assert.ok(s.hint.indexOf('даты не помечены неудачными') >= 0 && s.hint.indexOf('Перезапустите сервер') >= 0);
  // надписи — как на экране: разделы и карточки в settings.html, кнопки там же и в desk-backup.js
  const html = P('settings.html'), bk = P('desk-backup.js');
  for (const label of ['«Доступ из сети»', 'карточка «Сервер»', '«Перезапустить сервер»', 'её видит администратор', 'stop.bat', 'start.bat',
    '«Диагностика»', 'карточка «Файл диагностики»', '«Собрать диагностику»', '«Резервные копии»', 'карточка «Копии»',
    '«Проверить восстановление»', '«Создать копию сейчас»']) assert.ok(s.hint.indexOf(label) >= 0, label);
  assert.ok(s.hint.indexOf('«Проверить восстановление»') < s.hint.indexOf('«Создать копию сейчас»'));
  // окно start.bat закрывать нельзя (встроенная база остановится аварийно): совета закрыть его нет, есть запрет
  assert.ok(s.hint.indexOf('закройте окно') < 0 && s.hint.indexOf('окно start.bat не закрывайте') > 0);
  assert.ok(s.hint.indexOf('stop.bat') < s.hint.indexOf('затем запустите start.bat'));
  assert.ok(s.hint.indexOf('пока нет ни одной копии') > 0);
  for (const code of ['<h1>Доступ из сети</h1>', '<h2>Сервер</h2>', '>Перезапустить сервер</button>', '<h1>Диагностика</h1>',
    '<h2>Файл диагностики</h2>', '>Собрать диагностику</button>', '<h1>Резервные копии</h1>']) assert.ok(html.indexOf(code) > 0, code);
  for (const code of ['<h2>Копии</h2>', '>Создать копию сейчас</button>', '>Проверить восстановление</button>']) assert.ok(bk.indexOf(code) > 0, code);
  assert.ok(s.title.indexOf(s.hint) > 0 && s.title.indexOf('Ошибка на 14.10.2026: InternalError_') > 0);
});

t('a stall of the second pass shows while the first has nothing to run', () => {
  const j = Object.assign(stall({ phase: 'second' }), { todo: 0, pass: 'second', total: 10, done: 10,
    second: { done: 3, todo: 7, failed: 0, total: 10, next: ['2026-10-14'], problem_dates: [], eta_s: null } });
  assert.strictEqual(T.stall(j, NOW).text, 'стоит: внутренняя ошибка базы, повтор в 12:10');
  assert.ok(T.passes(j).second.indexOf('стоит: внутренняя ошибка базы') > 0);
  assert.strictEqual(T.stall(Object.assign({}, j, { second: { todo: 0 } }), NOW), null);  // nothing left in either pass
  assert.strictEqual(T.passes(Object.assign({}, j, { stalled: null })).second.indexOf('стоит'), -1);
});

t('the other causes have no hint, as before', () => {
  for (const cause of ['store', 'store_slow', 'network', 'hub', 'disk']) {
    const s = T.stall(stall({ cause }), NOW);
    assert.strictEqual(s.hint, '', cause);
    assert.ok(s.title.indexOf('Перезапустите') < 0, cause);
  }
  assert.strictEqual(T.stall(stall({ cause: undefined, source: 'store' }), NOW).why, 'база недоступна');  // состояние до причин
});

let failed = 0;
for (const [name, f] of tests) {
  try { f(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n     ' + (e && e.stack || e)); }
}
process.exit(failed ? 1 : 0);
