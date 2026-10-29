// Почему набора нет (TDM#445, prototypes/concepts/lib/inbox-why.js): объяснение C16 «отчёта ОВП нет» по ответу
// /api/v1/inbox/datasets/ocp (desk/daystatus.dataset_files) — отчёт уже в базе после прогона, адаптер без источника,
// файлов нет (пропущенные — честно без источника) с подсказкой про папки монитора, файлы есть — по статусам и последний
// с причиной и кодами, файлы старше окна загрузки; ссылки на C14 и настройки.
//   node tests/js/inbox_why.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'lib', 'inbox-why.js'), 'utf8'), ctx);
const W = ctx.window.INBOX_WHY;
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const base = over => Object.assign({ dataset: 'ocp', name: 'ОВП', on: '2026-10-20', adapter: 'ok demo 1 abc', sources: ['ocp_csv'], in_force: null,
  files: { total: 0, by_status: {}, status_text: {} }, latest: null, unread: { ignored: 0, too_old: 0 }, lookback_days: null,
  folders: { count: 2, missing: 0 }, last_scan: { at: '2026-10-20T07:00:00Z', files: {} } }, over);
const text = (x, opt) => W.lines(x, opt).map(l => l.html.replace(/<[^>]+>/g, '')).join('\n');

t('файлов ОВП нет и пропущенных нет: подсказка про папки монитора, а не параметры адаптера', () => {
  const s = text(base());
  assert.ok(s.includes('нет ни одного файла, который адаптер признал отчётом ОВП'), s);
  assert.ok(s.includes('Настройки → «Папки с отчётами» (папки монитора), а не только в параметры адаптера') && s.includes('Папок с отчётами: 2.'), s);
  assert.ok(s.includes('C14 «Состояние дня»') && s.includes('Последний просмотр папок'), s);
  const html = W.html(base(), { listClass: 'c16-list' });
  assert.ok(html.startsWith('<ul class="c16-list" data-why="ocp">') && html.includes('href="c14-day-status.html?on=2026-10-20"') && html.includes('href="../settings.html#adapter"'), html);
  assert.ok(text(base({ folders: { count: 0, missing: 0 } })).includes('Папки с отчётами не заданы.'));
  assert.ok(text(base({ folders: { count: 2, missing: 1 } })).includes('из них сервер не видит: 1'));
  assert.ok(text(base({ last_scan: null })).includes('папки ещё не просматривались'));
});

t('у пропущенного файла источник неизвестен: «признал отчётом ОВП — нет; пропущено M»', () => {
  const s = text(base({ unread: { ignored: 3, too_old: 0 } }));
  assert.ok(s.includes('Файлов, которые адаптер признал отчётом ОВП, нет; пропущено 3 файла (адаптер их не узнал).'), s);
  assert.ok(text(base({ unread: { ignored: 1, too_old: 0 } })).includes('пропущено 1 файл '));
  assert.ok(text(base({ unread: { ignored: 11, too_old: 0 } })).includes('пропущено 11 файлов '));
});

t('файлы есть: по статусам, последний — имя, даты, статус, причина, коды адаптера', () => {
  const x = base({ files: { total: 3, by_status: { rejected: 2, accepted: 1 }, status_text: { rejected: 'отклонён', accepted: 'принят' } },
    latest: { name: 'ocp_20261020.csv', status: 'rejected', status_text: 'отклонён', report_date: '2026-10-20', valid_date: '2026-10-20', after: false,
      why: 'файл отклонён целиком при проверке', issues: [{ severity: 'error', code: 'unknown_book', count: 2, text: 'книги нет в справочнике' }] } });
  const L = W.lines(x);
  const s = text(x);
  assert.ok(s.includes('Файлов отчёта ОВП во входящих: 3 — принят 1, отклонён 2.'), s);
  assert.ok(s.includes('Последний: ocp_20261020.csv · отчёт за 20.10.2026 · отклонён: файл отклонён целиком при проверке · коды адаптера: unknown_book ×2 (книги нет в справочнике).'), s);
  assert.strictEqual(L[0].cls, 'warn'); assert.strictEqual(L[1].cls, 'bad');
  assert.ok(!s.includes('Папки с отчётами»'), 'файлы есть — подсказки про папки нет');
  assert.ok(text({ ...x, unread: { ignored: 2, too_old: 0 } }).includes('отклонён 2; в папках ещё 2 файла, которые адаптер не узнал.'));
  const later = text(base({ files: { total: 1, by_status: { accepted: 1 }, status_text: { accepted: 'принят' } },
    latest: { name: 'a.csv', status: 'accepted', status_text: 'принят', report_date: '2026-10-21', valid_date: '2026-10-22', after: true, why: null, issues: [] } }));
  assert.ok(later.includes('отчёт за 21.10.2026 (рабочий день 22.10.2026) · принят — позже даты прогона 20.10.2026'), later);
});

t('только старше окна загрузки: сколько и где окно', () => {
  const s = text(base({ unread: { ignored: 0, too_old: 4 }, lookback_days: 35 }));
  assert.ok(s.includes('Файлы отчёта ОВП есть, но все старше окна загрузки (35 дн.): 4 файла — не читались.'), s);
  const both = text(base({ unread: { ignored: 0, too_old: 1 }, files: { total: 2, by_status: { accepted: 2 }, status_text: { accepted: 'принят' } } }));
  assert.ok(both.includes('во входящих: 2 — принят 2; ещё 1 файл старше окна загрузки — не читались.') && !both.includes('«Отчёты не старше'), both);
  assert.ok(s.includes('«Отчёты не старше, дней»'), s);
});

t('отчёт уже в базе, прогон посчитан до него; адаптер не загружен или без источника', () => {
  const s = text(base({ in_force: { units: 1, valid_date: '2026-10-19', recorded_at: '2026-10-20T09:00:00Z' },
    files: { total: 1, by_status: { accepted: 1 }, status_text: { accepted: 'принят' } } }));
  assert.ok(s.startsWith('В базе уже есть отчёт ОВП на 20.10.2026 (от 19.10.2026,') && s.includes('пересчитайте прогон'), s);
  assert.ok(text(base({ adapter: 'error import_failed' })).includes('Адаптер не загружен (import_failed)'));
  assert.ok(text(base({ sources: [] })).includes('Адаптер не объявляет источник набора «ОВП»'));
});

t('имена файлов и коды экранируются', () => {
  const html = W.html(base({ files: { total: 1, by_status: { rejected: 1 }, status_text: {} },
    latest: { name: '<img src=x>.csv', status: 'rejected', report_date: '2026-10-20', issues: [{ code: '<b>', count: 1 }] } }));
  assert.ok(!html.includes('<img') && html.includes('&lt;img src=x&gt;.csv') && html.includes('&lt;b&gt;'), html);
});

console.log(n + ' tests passed');
