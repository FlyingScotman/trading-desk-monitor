// Что найдено в папках с отчётами (TDM#445, prototypes/settings-inbox-folders.js), чистая часть: папку страницы находит
// сервер (entries: путь из settings.json → его папка); сводка — одна строка («Прочитано: ОВП за 27.10 · не распознано 3
// · отклонено 1»), разбивка по наборам — в «подробнее», без id источников; папка ещё не просмотрена, не видна, пуста,
// все файлы пропущены; папки из переменной окружения.
//   node tests/js/settings_inbox_folders.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-inbox-folders.js'), 'utf8'), ctx);
const F = ctx.window.DeskInboxFolders._test;
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };
const strip = s => s.replace(/<[^>]+>/g, '');

const IN = { dir: 'C:\\Отчёты\\ОВП', path: 'C:\\Отчёты\\ОВП', recursive: false, output: false, scanned: true, exists: true, listed: 7, skipped: {},
  files: 6, matched: 4, ignored: 2, too_old: 0, lost: 1, by_status: { accepted: 3, rejected: 1 },
  sources: [{ source: 'ocp_src', datasets: ['ocp'], files: 3, by_status: { accepted: 2, rejected: 1 } }, { source: 'cash_src', datasets: ['cash_balances'], files: 1, by_status: { accepted: 1 } }],
  datasets: [{ dataset: 'cash_balances', name: 'Остатки на счетах', latest_valid_date: '2026-10-20', files: 1 }, { dataset: 'ocp', name: 'ОВП', latest_valid_date: '2026-10-21', files: 2 }] };
const fold = over => Object.assign({}, IN, over);
const DOC = { folders: [IN, fold({ path: 'D:\\ovp', scanned: false, exists: null, listed: null })], from_env: false,
  entries: [{ path: 'C:\\Отчёты\\ОВП', folder: 0 }, { path: 'd:\\OVP\\', folder: 1 }, { path: '~/x', folder: null }],
  status_text: { accepted: 'принят', rejected: 'отклонён' }, dataset_names: { ocp: 'ОВП', cash_balances: 'Остатки на счетах' }, lookback_days: 35, poll_seconds: 30 };
const view = (f, doc) => F.view({ entry: {}, folder: f }, doc || DOC);

t('папку страницы находит сервер: по пути из settings.json', () => {
  assert.strictEqual(F.find(DOC, ' C:\\Отчёты\\ОВП ').folder, IN);
  assert.strictEqual(F.find(DOC, 'd:\\OVP\\').folder.path, 'D:\\ovp');
  assert.strictEqual(F.find(DOC, '~/x').folder, null);
  assert.strictEqual(F.find(DOC, 'C:\\новая'), null, 'не сохранена — сводки нет');
  assert.strictEqual(F.find(DOC, ''), null); assert.strictEqual(F.find(null, 'C:\\Отчёты\\ОВП'), null);
});

t('сводка — одна строка без id источников; разбивка по наборам — в «подробнее»', () => {
  const v = view(IN);
  assert.strictEqual(strip(v.html), 'Прочитано: Остатки на счетах за 20.10, ОВП за 21.10 · не распознано 2 · отклонено 1');
  assert.ok(!v.html.includes('ocp_src') && !v.html.includes('cash_src'), v.html);
  assert.ok(strip(v.more).includes('ОВП: принят 2, отклонён 1') && strip(v.more).includes('Остатки на счетах: принят 1'), v.more);
  assert.ok(!v.more.includes('ocp_src'), 'наборы — русскими именами, как в основной строке');
  const h = F.html(v);
  assert.ok(h.includes('<details><summary>подробнее</summary>') && h.includes('href="concepts/c14-day-status.html"'), h);
  assert.ok(strip(view(fold({ too_old: 3 })).html).includes('старше окна загрузки 3'));
});

t('не просмотрена, не видна, пуста, всё пропущено, ничего не прочитано, из переменной окружения', () => {
  const w = F.view(F.find(DOC, 'd:\\OVP\\'), DOC);
  assert.strictEqual(w.html, 'ещё не просматривалась'); assert.strictEqual(w.wait, true);
  assert.strictEqual(F.view(F.find(DOC, 'd:\\OVP\\'), { ...DOC, autopilot: false }).html, 'ещё не просматривалась — автообновление выключено');
  assert.ok(view(fold({ exists: false })).html.startsWith('сервер не видит папку'));
  const empty = fold({ files: 0, matched: 0, ignored: 0, lost: 0, listed: 0, sources: [], datasets: [] });
  assert.strictEqual(strip(view(empty).html), 'Папка пуста'); assert.strictEqual(view(empty).cls, 'warn');
  assert.strictEqual(strip(view(fold({ ...empty, listed: 4, skipped: { too_old: 3, too_large: 1 } })).html),
    'Прочитанных отчётов нет · пропущено 4 (старые по дате изменения, слишком большие)');
  assert.strictEqual(strip(view(fold({ ...empty, files: 2, ignored: 2, listed: 2 })).html), 'Прочитанных отчётов нет · не распознано 2');
  assert.strictEqual(F.view(F.find(DOC, '~/x'), DOC).html, 'не просматривается');
  assert.strictEqual(F.view(F.find(DOC, '~/x'), { ...DOC, from_env: true }).html, 'не просматривается: папки заданы переменной окружения');
});

t('имена и коды экранируются', () => {
  const v = view(fold({ datasets: [{ dataset: '<x>', name: '<y>', latest_valid_date: '2026-10-20' }], sources: [{ source: 's', datasets: ['<z>'], by_status: { '<q>': 1 } }] }));
  const h = F.html(v);
  assert.ok(!/<[xyzq]>/.test(h) && h.includes('&lt;y&gt;') && h.includes('&lt;q&gt; 1'), h);
});

console.log(n + ' tests passed');
