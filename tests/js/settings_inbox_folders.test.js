// Что найдено в папках с отчётами (TDM#445, prototypes/settings-inbox-folders.js), чистая часть: папка страницы
// находит папку сервера (без «\**», регистра и разделителя; относительная — от папки данных), сводка папки — найдено,
// узнано, не узнано, старше окна, отклонено, по источникам с наборами, последний прочитанный отчёт набора; папка, которую
// сервер не видит или ещё не просматривал.
//   node tests/js/settings_inbox_folders.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { console, window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'settings-inbox-folders.js'), 'utf8'), ctx);
const F = ctx.window.DeskInboxFolders._test;
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };
const text = (f, doc) => F.lines(f, doc).map(l => l.html.replace(/<[^>]+>/g, '')).join('\n');

const IN = { dir: 'C:\\Отчёты\\ОВП', path: 'C:\\Отчёты\\ОВП', recursive: false, output: false, exists: true, files: 6, matched: 4, ignored: 2, too_old: 0, lost: 1,
  by_status: { accepted: 3, rejected: 1 },
  sources: [{ source: 'ocp_src', datasets: ['ocp'], files: 3, by_status: { accepted: 2, rejected: 1 } }, { source: 'cash_src', datasets: ['cash_balances'], files: 1, by_status: { accepted: 1 } }],
  datasets: [{ dataset: 'cash_balances', name: 'Остатки на счетах', latest_valid_date: '2026-10-20', files: 1 }, { dataset: 'ocp', name: 'ОВП', latest_valid_date: '2026-10-21', files: 2 }],
  last_seen: '2026-10-21T08:00:00Z' };
const DOC = { folders: [IN, { path: '/srv/desk/adapter-output', recursive: true, output: true, exists: true, files: 0 },
  { path: '/home/desk/data/reports', recursive: true, output: false, exists: false, files: 0, matched: 0, sources: [], datasets: [] }],
  status_text: { accepted: 'принят', rejected: 'отклонён' }, lookback_days: 35 };

t('папка страницы находит папку сервера', () => {
  assert.strictEqual(F.find(DOC, 'c:/отчёты/овп/', null), IN);
  assert.strictEqual(F.find(DOC, ' C:\\Отчёты\\ОВП\\** ', null), IN);
  assert.strictEqual(F.find(DOC, 'reports', '/home/desk/data/').path, '/home/desk/data/reports', 'относительная — от home');
  assert.strictEqual(F.find(DOC, 'C:\\Другое', null), null);
  assert.strictEqual(F.find(DOC, '/srv/desk/adapter-output', null), null, 'папка результатов адаптера — не папка отчётов');
  assert.strictEqual(F.find(DOC, '', null), null);
  assert.strictEqual(F.find(null, 'C:\\Отчёты\\ОВП', null), null);
});

t('сводка папки: найдено, по источникам с наборами, последний прочитанный отчёт набора', () => {
  const s = text(IN, DOC);
  assert.ok(s.includes('найдено файлов: 6 · адаптер узнал: 4 · не узнал: 2 · отклонено или с ошибкой: 1'), s);
  assert.ok(s.includes('ocp_src (ocp): принят 2, отклонён 1') && s.includes('cash_src (cash_balances): принят 1'), s);
  assert.ok(s.includes('последний прочитанный отчёт: Остатки на счетах — 20.10.2026, ОВП — 21.10.2026'), s);
  assert.ok(s.includes('последний новый файл замечен'), s);
  assert.ok(F.html(IN, DOC).includes('<span class="bad">отклонён 1</span>'));
  assert.ok(text({ ...IN, too_old: 3 }, DOC).includes('старше окна загрузки (35 дн.): 3'));
});

t('папку не видно, файлов нет, ещё не просматривалась, ничего не прочитано', () => {
  assert.ok(text(DOC.folders[2], DOC).startsWith('Сервер не видит эту папку'));
  const empty = F.lines({ ...IN, files: 0, matched: 0, ignored: 0, lost: 0, sources: [], datasets: [] }, DOC);
  assert.deepStrictEqual(Array.from(empty, l => l.cls), ['warn']);
  assert.ok(empty[0].html.includes('Файлов отчётов не найдено (подпапки не просматриваются)'));
  assert.ok(text(null, DOC).startsWith('Сервер ещё не просматривал эту папку'));
  const unread = F.lines({ ...IN, files: 2, matched: 0, ignored: 2, lost: 0, sources: [], datasets: [] }, DOC);
  assert.strictEqual(unread[0].cls, 'warn', 'ни одного файла не узнал — предупреждение');
  const none = text({ ...IN, datasets: [] }, DOC);
  assert.ok(none.includes('ни один отчёт из этой папки не прочитан'), none);
});

t('пути и коды экранируются', () => {
  const h = F.html({ ...IN, sources: [{ source: '<x>', datasets: ['<y>'], files: 1, by_status: { '<z>': 1 } }] }, DOC);
  assert.ok(!h.includes('<x>') && h.includes('&lt;x&gt;') && h.includes('&lt;z&gt; 1'), h);
});

console.log(n + ' tests passed');
