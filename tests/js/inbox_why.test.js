// Почему набора нет (TDM#445, prototypes/concepts/lib/inbox-why.js): причина C16 «отчёта ОВП нет» одной фразой по
// ответу /api/v1/inbox/datasets/ocp (desk/daystatus.dataset_files) — отчёт уже в базе, адаптер, последний файл отклонён
// (код — в подсказке), отчёт только позже, старше окна, папки не просматривались, папку не видно, файла нет (подсказка
// про «Папки с отчётами»); второй строкой — имя файла, сколько не распознано, ссылки (настройки — только админу).
//   node tests/js/inbox_why.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ctx = { window: {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'lib', 'inbox-why.js'), 'utf8'), ctx);
const W = ctx.window.INBOX_WHY;
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const base = over => Object.assign({ dataset: 'ocp', name: 'ОВП', on: '2026-10-20', adapter: 'ok demo 1 abc', sources: ['ocp_csv'], in_force: null,
  files: { total: 0, by_status: {}, status_text: {}, later: { total: 0, by_status: {}, first_date: null } }, latest: null, unread: { ignored: 0, too_old: 0 },
  lookback_days: null, folders: { count: 2, scanned: 2, missing: 0 }, last_scan: { at: '2026-10-20T07:00:00Z', files: {}, skipped: {} }, settings_link: false }, over);
const strip = s => s.replace(/<[^>]+>/g, '');
const main = (x, opt) => strip(W.view(x, opt).html);
const sub = (x, opt) => strip(W.view(x, opt).sub.join(' · '));
const REJ = { name: 'ocp_20261020.csv', status: 'rejected', status_text: 'отклонён', report_date: '2026-10-20', valid_date: '2026-10-20', after: false,
  reason: 'книги нет в справочнике', issues: [{ severity: 'error', code: 'unknown_book', count: 2, text: 'книги нет в справочнике' }] };

t('файла нет: одна фраза с подсказкой про «Папки с отчётами»; ссылка на настройки — только админу', () => {
  assert.strictEqual(main(base()), 'Файла отчёта ОВП в папках нет — добавьте папку в Настройки → Папки с отчётами · C14');
  assert.strictEqual(sub(base()), '', 'второй строке нечего сказать — ссылка в конце первой');
  assert.ok(!W.html(base()).includes('class="mut3"'), 'одна строка');
  const admin = W.view(base({ settings_link: true }));
  assert.ok(admin.html.includes('<a href="../settings.html#dirs">Настройки → Папки с отчётами</a>'));
  assert.ok(!admin.sub.join('').includes('Настройки'), 'ссылка уже во фразе — второй раз не нужна');
  assert.ok(!W.view(base()).html.includes('settings.html'), 'зрителю — без ссылки (настройки ответят ему 403)');
  const html = W.html(base(), {});
  assert.ok(html.includes('data-why="ocp"') && html.includes('href="c14-day-status.html?on=2026-10-20"'), html);
});

t('последний файл отклонён: причина словами, без даты прогона; код — в подсказке; имя и «ещё N не распознано» мелко', () => {
  const x = base({ files: { total: 1, by_status: { rejected: 1 }, status_text: {}, later: { total: 0 } }, latest: REJ, unread: { ignored: 3, too_old: 0 }, settings_link: true });
  const v = W.view(x);
  assert.strictEqual(strip(v.html), 'Отчёт ОВП отклонён: книги нет в справочнике');
  assert.strictEqual(v.cls, 'bad'); assert.strictEqual(v.title, 'unknown_book ×2');
  assert.strictEqual(sub(x), 'ocp_20261020.csv · ещё 3 файла не распознано · C14 · Настройки');
  assert.strictEqual(main(base({ latest: { ...REJ, report_date: '2026-10-17', valid_date: '2026-10-17' } })), 'Отчёт ОВП за 17.10 отклонён: книги нет в справочнике');
  assert.ok(sub(base({ latest: REJ, unread: { ignored: 1 } })).includes('ещё 1 файл не распознан'));
  assert.ok(sub(base({ unread: { ignored: 11 } })).includes('в папках 11 файлов не распознано'));
  const failed = W.view(base({ latest: { ...REJ, status: 'failed', reason: 'ValueError', issues: [] } }));
  assert.strictEqual(strip(failed.html), 'Отчёт ОВП не прочитан'); assert.strictEqual(failed.title, 'ValueError');
});

t('отчёт в базе после прогона, только позже, старше окна, папки не просматривались, папку не видно', () => {
  assert.strictEqual(main(base({ in_force: { units: 1, valid_date: '2026-10-19', recorded_at: 'x' } })), 'Отчёт ОВП уже в базе за 19.10 — прогон посчитан раньше, пересчитайте его · C14');
  assert.strictEqual(main(base({ latest: { ...REJ, status: 'accepted', after: true, valid_date: '2026-10-21' } })), 'Отчёт ОВП есть только позже даты прогона — за 21.10');
  assert.strictEqual(main(base({ unread: { ignored: 0, too_old: 4 }, lookback_days: 35 })), 'Отчёты ОВП в папках старше окна загрузки (35 дн.) — не читались · C14');
  assert.strictEqual(main(base({ last_scan: null })), 'Папки ещё не просматривались · C14');
  // папку только что добавили, скан до неё не дошёл: не «добавьте папку»
  assert.strictEqual(main(base({ folders: { count: 2, scanned: 1, missing: 0 } })), 'Папка ещё не просматривалась · C14');
  assert.strictEqual(main(base({ folders: { count: 1, scanned: 0, missing: 0 }, autopilot: false })), 'Папка ещё не просматривалась — автообновление выключено · C14');
  assert.strictEqual(main(base({ last_scan: { at: 'x', files: {}, skipped: { too_old: 3, too_large: 1 } } })), 'Файлы в папках пропущены: старые по дате изменения и слишком большие · C14');
  const part = W.view(base({ latest: { ...REJ, status: 'partially_accepted', status_text: 'принят частично', units: { accepted: 1, rejected: 1 } } }));
  assert.strictEqual(strip(part.html), 'Отчёт ОВП принят частично: книги нет в справочнике'); assert.strictEqual(part.cls, 'bad');
  assert.strictEqual(main(base({ folders: { count: 2, scanned: 2, missing: 1 } })), 'Сервер не видит папку с отчётами (1 из 2) · C14');
  assert.strictEqual(main(base({ adapter: 'error import_failed' })), 'Адаптер не загружен — отчёты не читаются · C14');
  assert.strictEqual(main(base({ sources: [] })), 'Адаптер не знает отчёта ОВП · C14');
});

t('имена файлов и коды экранируются', () => {
  const html = W.html(base({ latest: { ...REJ, name: '<img src=x>.csv', reason: '<b>', issues: [{ severity: 'error', code: '"x', count: 1 }] } }));
  assert.ok(!html.includes('<img') && !html.includes('<b>') && html.includes('&lt;img src=x&gt;.csv') && html.includes('title="&quot;x ×1"'), html);
});

console.log(n + ' tests passed');
