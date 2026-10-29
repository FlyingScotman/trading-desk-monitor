// «Обновление адаптера» (prototypes/desk-adapter-updater.js): какие кнопки при каком состоянии, строка «Используется»,
// строка проверки GitHub; файл не ходит во внешнюю сеть сам (только по кнопке — обычное скачивание браузером).
//   node tests/js/desk_adapter_updater.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-adapter-updater.js'), 'utf8');

function load() {
  const ctx = {
    window: {}, Node: function () {}, setTimeout, clearTimeout, console,
    document: { currentScript: { dataset: {} }, readyState: 'complete', getElementById: () => null, addEventListener: () => {} },
    fetch: () => { throw new Error('no network in tests'); }
  };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return ctx.window.DESK_ADAPTER_UPDATER._test;
}
const T = load();
const ids = st => JSON.parse(JSON.stringify(T.actions(st).map(a => a[0])));
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

t('новый выпуск: «Установить <версия>», браузер, поиск в «Загрузках»', () => {
  const st = { release: { version: '0.5.0' }, update_available: true, compare: 'newer', token_set: true };
  assert.deepStrictEqual(ids(st), ['check', 'install', 'browser', 'scan']);
  assert.strictEqual(T.actions(st)[1][1], 'Установить 0.5.0');
});
t('не новее — только явно; закреплённый отпечаток — без установки', () => {
  assert.deepStrictEqual(ids({ release: { version: '0.4.0' }, compare: 'older' }), ['check', 'install-older', 'browser', 'scan']);
  assert.deepStrictEqual(ids({ release: { version: '0.5.0' }, compare: 'same' }), ['check', 'browser', 'scan']);
  assert.deepStrictEqual(ids({ pinned: true, release: { version: '0.6.0' }, update_available: true }), ['check', 'scan']);
});
t('откат к предыдущему и к адаптеру из комплекта', () => {
  const st = { installed: { version: '0.5.1', previous: '0.5.0' }, previous_available: true, bundle: { version: '0.4.0' } };
  const a = T.actions(st);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(a.map(x => x[0]))), ['check', 'browser', 'scan', 'rollback', 'bundle']);
  assert.strictEqual(a[3][1], 'Вернуть 0.5.0');
  assert.strictEqual(a[4][1], 'Использовать адаптер из комплекта');
  assert.ok(!ids({ installed: { version: '0.5.1', previous: '0.5.0' }, previous_available: false }).includes('rollback'));
});
t('пока идёт установка — только «Отменить»', () => {
  assert.deepStrictEqual(ids({ job: { state: 'waiting_browser' }, release: { version: '1' }, update_available: true }), ['cancel']);
  assert.ok(ids({ job: { state: 'done' } }).includes('check'));
});
t('строка «Используется» и проверка GitHub', () => {
  assert.strictEqual(T.inUse({ source: 'installed', in_use: { version: '0.5.0' } }), 'версия 0.5.0, установлен обновлением');
  assert.strictEqual(T.inUse({ source: 'reference', in_use: {} }), 'эталонный адаптер (CSV)');
  assert.strictEqual(T.checkLine({}), 'GitHub ещё не проверялся');
  const bad = T.checkLine({ check: { ok: false, at: '2026-10-29T10:00:00', error: { code: 'token_rejected', what: 'GitHub не принял токен (401)' } } });
  assert.ok(/GitHub не принял токен \(401\)$/.test(bad), bad);
  const ok = T.checkLine({ check: { ok: true, at: '2026-10-29T10:00:00', layer: 'curl' }, release: { version: '0.5.0' } });
  assert.ok(/\(curl\.exe\): последний выпуск 0\.5\.0$/.test(ok), ok);
});
t('без внешних запросов при загрузке страницы', () => {
  assert.ok(!/fetch\(\s*['"`]https?:/.test(SRC));
  assert.ok(!/github_pat_[A-Za-z0-9]{10}/.test(SRC), 'в коде нет токенов');
});
console.log(n + ' tests passed');
