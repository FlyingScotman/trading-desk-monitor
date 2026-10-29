// Страница настроек в браузере (Playwright), сервер — заглушка маршрутов (TDM#421): без командной строки и без JSON.
//  - «Состояние адаптера» (status), «Действия адаптера» (форма параметров, «Запустить», итог, файлы и их судьба во
//    входящих, ok: false — обычный итог), «Проверка адаптера» (папки флажками, сводка таблицей, «Скопировать сводку»);
//  - вкладка адаптера с «ключ → список шаблонов» и списком папок правится формой; сырой JSON — только «Для опытных»;
//  - поля settings.json, которых раньше на странице не было (таймауты, допущения расчёта), сохраняются.
//   node --test tests/js/settings_adapter.pw.test.js
// Нужен Playwright (как b1_null_sums.pw.test.js); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';

const TAB_SCHEMA = { type: 'object', additionalProperties: false, patternProperties: { '^_': {} }, properties: {
  report_dirs: { type: 'array', title: 'Папки отчётов', items: { type: 'string', minLength: 1 } },
  patterns: { type: 'object', title: 'Шаблоны имён', additionalProperties: { type: 'array', items: { type: 'string', minLength: 1 } } },
  mode: { title: 'Режим', oneOf: [{ const: 'fast', title: 'Быстро' }, { const: 'full', title: 'Полностью' }] } } };
const ADAPTER = { loaded: true, path: 'C:\\desk\\adapter', reference: false, default: false, name: 'synthetic', version: '0.9.0', fingerprint: 'ab'.repeat(32),
  config_dir: HOME + '\\adapter-config', describe: { contract: 'private/1.5', sources: [{ source: 'pos_csv', dataset: 'positions' }] },
  config_files: [{ name: 'options.json', title: 'Параметры', exists: true }, { name: 'extra.json', title: 'Прочее', exists: false, template: {} }],
  settings_tabs: [{ id: 'sources', title: 'Источники', file: 'options.json', schema: TAB_SCHEMA, help: '' }],
  optional: ['status', 'run_action'], actions: ['export_rows'], warnings: [], error: null };
const ACTION = { id: 'export_rows', title: 'Выгрузка строк', description: 'Пишет файл для входящих', output_dir: true,
  params: { type: 'object', required: ['day'], properties: { day: { type: 'string', format: 'date', title: 'Дата' } }, additionalProperties: false } };
const REPORT = { ok: false, backend: '0.2.10', spi: 1, adapter: 'adapter', name: 'synthetic', version: '0.9.0', contract: 'private/1.5', fingerprint: 'ab'.repeat(32),
  load_error: null, instruments: 'stub', calendar: 'none', files: { matched: 12, ignored: '<10', match_failed: 0 }, match_seconds: 0.4,
  problems: { nondeterministic: '<10' }, notes: {}, reference: { books: 14 }, reference_issues: [],
  weekdays: { report_date: { Mon: 12 }, valid_date: {} },
  sources: [{ source: 'pos_csv', report_date: '2026-10-20', valid_date: '2026-10-19', files: 12, rows: 400, units: { accepted: 11, rejected: '<10' },
    statuses: { accepted: 11, rejected: '<10' }, issues: [{ severity: 'warning', code: 'unknown_book', count: 40 }], controls: ['no_controls'],
    unmapped: 0, quantity_only: 0, unmapped_share_pct: null }] };
const TEXT = 'desk adapter check: backend 0.2.10, SPI 1, contract private/1.5\nadapter synthetic 0.9.0\nresult: FAIL';

function server() {
  const st = { puts: [], configPuts: [], actionRuns: 0, actionPolls: 0, checkPolls: 0, checkRuns: [] };
  st.settings = { home: HOME, hub_url: 'https://hub.example', inbox_dirs: [{ path: 'C:\\in', label: 'Позиции', recursive: false }], hub_token_set: true, proxy_password_set: false,
    effective: { home: HOME, adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: false }, env_overrides: [], error: null };
  st.options = { exists: true, text: JSON.stringify({ _comment: 'keep', report_dirs: ['C:\\in'], patterns: { pos_csv: ['^pos_'] } }, null, 2) + '\n', sha256: 'x' };
  st.handle = (u, method, body) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return st.settings;
    if (p === '/api/v1/settings' && method === 'PUT') { st.puts.push(body); Object.assign(st.settings, body); return st.settings; }
    if (p === '/api/v1/adapter') return ADAPTER;
    if (p === '/api/v1/adapter/settings-context') return { recent_books: [], indices: [], currencies: ['RUB'], error: null };
    if (p === '/api/v1/adapter/config/options.json' && method === 'GET') return { name: 'options.json', path: HOME + '\\adapter-config\\options.json', ...st.options };
    if (p === '/api/v1/adapter/config/extra.json') return { name: 'extra.json', exists: false, text: null, sha256: null };
    if (p === '/api/v1/adapter/status') return { available: true, loaded: true, at: '2026-10-28T10:00:00+00:00', timeout_s: 60, error: null, problems: [],
      panels: [{ id: 'folders', title: 'Папки отчётов', level: 'warning', lines: ['папок: 1', 'нет выгрузки за 27.10'], table: { columns: ['отчёт', 'файлов'], rows: [['pos', 3]] } }],
      issues: [] };
    if (p === '/api/v1/adapter/actions') return { loaded: true, error: null, running: null, output_root: HOME + '\\adapter-output', timeout_s: 900, actions: [{ ...ACTION, job: null }] };
    if (p === '/api/v1/adapter/actions/export_rows/run') { st.actionRuns++; st.actionParams = body.params;
      return { action: ACTION, running: true, job: { state: 'running', started_at: new Date().toISOString(), request: body } }; }
    if (p === '/api/v1/adapter/actions/export_rows') { st.actionPolls++;
      return { action: ACTION, running: false, job: { state: 'done', started_at: '2026-10-28T10:00:00+00:00', seconds: 2.5, request: { params: st.actionParams },
        result: { ok: true, summary: ['строк: 2'], table: null, issues: [], warnings: [] }, written: true, output_dir: HOME + '\\adapter-output\\export_rows',
        files: [{ name: 'pnl_official_20261021.csv', path: HOME + '\\adapter-output\\export_rows\\pnl_official_20261021.csv', bytes: 2048,
                  inbox: { status: 'accepted', source: 'pnl_csv', report_date: '2026-10-21' } }], scan: { ran: true } } }; }
    if (p === '/api/v1/adapter/check' && method === 'POST') { st.checkRuns.push(body); st.checkJob = { state: 'running', started_at: new Date().toISOString(), request: body }; }
    if (p === '/api/v1/adapter/check') {
      if (st.checkJob && st.checkJob.state === 'running' && method === 'GET' && ++st.checkPolls > 1)
        st.checkJob = { state: 'done', started_at: '2026-10-28T10:00:00+00:00', finished_at: '2026-10-28T10:01:00+00:00', seconds: 60, request: st.checkJob.request, report: REPORT, text: TEXT, ok: false };
      return { job: st.checkJob || null, running: !!(st.checkJob && st.checkJob.state === 'running'), timeout_s: 1800,
        options: [{ path: 'C:\\in', label: null, kind: 'inbox', exists: true }, { path: HOME + '\\adapter-output\\**', label: 'папка результатов адаптера', kind: 'adapter_output', exists: false }] };
    }
    return null;
  };
  return st;
}

async function open(browser, st) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: ORIGIN });
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      let body = null; try { body = req.postDataJSON(); } catch (e) { /* GET */ }
      const out = st.handle(u, req.method(), body);
      return out ? route.fulfill({ status: req.method() === 'POST' && /run$|check$/.test(u.pathname) ? 202 : 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#adapter');
  await pg.waitForSelector('#ad-status:not([hidden]) .at-panel');
  return { pg, ctx, errors };
}

test('настройки: инструменты адаптера и формы без JSON (TDM#421)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('состояние, действие, проверка, копирование сводки', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st);
      const status = await pg.locator('#ad-status').innerText();
      assert.ok(/Папки отчётов/.test(status) && /нет выгрузки за 27\.10/.test(status) && /внимание/.test(status), status);
      // действие: дата обязательна — без неё не запускается
      const card = pg.locator('#ad-action-export_rows');
      await card.locator('button', { hasText: 'Запустить' }).click();
      assert.ok(/Исправьте параметры/.test(await card.innerText()) && st.actionRuns === 0);
      await card.locator('input[type=date]').fill('2026-10-21');
      await card.locator('button', { hasText: 'Запустить' }).click();
      await card.locator('text=pnl_official_20261021.csv').waitFor();
      const done = await card.innerText();
      assert.ok(/Выполнено/.test(done) && /строк: 2/.test(done) && /принят/.test(done) && /2 КБ/.test(done), done);
      assert.deepStrictEqual(st.actionParams, { day: '2026-10-21' });
      // проверка адаптера: папка входящих отмечена, папки результатов нет — флажок недоступен
      const boxes = pg.locator('#ad-check-opts input[type=checkbox]');
      assert.strictEqual(await boxes.count(), 2);
      assert.ok(await boxes.nth(0).isChecked() && await boxes.nth(1).isDisabled());
      await pg.locator('#ad-check-dir').fill('D:\\archive\\**');
      await pg.locator('#ad-check-run').click();
      await pg.locator('#ad-check-res .k-tbl').waitFor({ timeout: 10000 });
      assert.deepStrictEqual(st.checkRuns[0], { dirs: ['C:\\in', 'D:\\archive\\**'], stub_ctx: false });
      const res = await pg.locator('#ad-check-res').innerText();
      assert.ok(/не прошёл проверку/.test(res) && /pos_csv/.test(res) && /unknown_book/.test(res) && /40 \(10% строк\)/.test(res) && /nondeterministic/.test(res), res);
      assert.ok(/принято 11/.test(res) && /отклонено <10/.test(res), res);
      assert.ok(!/\bnull\b|undefined|NaN/.test(res + done + status), 'без null / undefined в тексте');
      // http://desk.test — не «безопасный» адрес: буфера обмена нет, копирует запасной путь (127.0.0.1 — с буфером)
      await pg.evaluate(() => { document.execCommand = c => { window.__copied = c === 'copy' && document.querySelector('textarea[readonly]').value; return true; }; });
      await pg.locator('#ad-check-copy').click();
      await pg.waitForTimeout(200);
      assert.strictEqual(await pg.evaluate(() => window.__copied), TEXT);
      assert.ok(/скопирована/.test(await pg.locator('#toast').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('вкладка адаптера — формой (ключ → список, список папок), JSON — только «Для опытных»', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st);
      const form = pg.locator('#tab-sources');
      await form.locator('.sf-map').waitFor();
      assert.strictEqual(await form.locator('.sf-acts label', { hasText: 'JSON' }).count(), 0, 'без «Для опытных» — нет переключателя JSON');
      assert.ok(await pg.locator('#cf-card').isHidden() && /extra\.json/.test(await pg.locator('#cf-hidden').innerText()));
      // ключ «hourly» и шаблон в нём
      await form.locator('.sf-map-add input').fill('hourly');
      await form.locator('.sf-map-add button').click();
      const row = form.locator('.sf-map-row').nth(1);
      await row.locator('button', { hasText: '+ Добавить' }).click();
      await row.locator('.sf-list input').first().fill('^h_');
      assert.ok(/изменено/.test(await form.locator('.sf-head').innerText()));
      // «Для опытных»: JSON формы и файлы без формы
      await pg.locator('#advanced').check();
      assert.ok(await pg.locator('#cf-card').isVisible());
      await form.locator('.sf-acts label', { hasText: 'JSON' }).locator('input').check();
      const raw = await form.locator('textarea').inputValue();
      const doc = JSON.parse(raw);
      assert.deepStrictEqual(doc, { _comment: 'keep', report_dirs: ['C:\\in'], patterns: { pos_csv: ['^pos_'], hourly: ['^h_'] } });
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('поля settings.json, которых не было на странице: таймауты, допущения расчёта', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st);
      await pg.locator('#adapter_kit_timeout_s').evaluate(e => { e.closest('details').open = true; });
      await pg.locator('#adapter_kit_timeout_s').fill('600');
      await pg.goto(ORIGIN + '/settings.html#calc');
      await pg.waitForSelector('#fx_foreign_curve');
      await pg.locator('#fx_foreign_curve').selectOption('xccy');
      await pg.locator('label.tgl', { has: pg.locator('#futures_by_ticker') }).click();
      await pg.locator('#save').click();
      await pg.waitForTimeout(300);
      const put = st.puts[st.puts.length - 1];
      assert.ok(put, 'сохранено');
      assert.strictEqual(put.fx_foreign_curve, 'xccy');
      assert.strictEqual(put.futures_by_ticker, true);
      assert.strictEqual(put.quote_curve_risk, null, 'выключено, как по умолчанию — ключа нет');
      assert.strictEqual(put.fx_implied_futures, null, 'включено, как по умолчанию — ключа нет');
      assert.strictEqual(put.autopilot, true, 'прежние флаги — как раньше, явно');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally { await browser.close(); }
});
