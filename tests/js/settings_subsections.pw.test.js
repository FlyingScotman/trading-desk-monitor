// Экран настроек: важное на виду, редкое — в меню (TDM#551), результаты действий не висят на странице (TDM#552).
// В браузере (Playwright), сервер — заглушка маршрутов, данные синтетические:
//  - подразделы «Адаптера и источников» по адресу #adapter/<подраздел>: открываются напрямую, переживают обновление,
//    без подраздела — последний открытый; элемент внутри (#dirs, #ad-check, #tab-sources) открывает свой подраздел;
//    стрелки переводят фокус между подразделами;
//  - «Дополнительно» в навигации: свёрнуто, открытость помнится; раздел оттуда, открытый по ссылке, раскрывает группу;
//  - поиск по настройкам: подпись, ключ settings.json; Enter — раздел, подраздел, раскрытый блок, курсор в поле;
//  - итог действия — строкой «Последний запуск», раскрывается по щелчку; таблица на 180 строк — первые 10, «Показать
//    все», поиск, CSV; подробности проверки свёрнуты;
//  - места для блоков других модулей (DeskSettings.slot / status);
//  - телефон (390 px): без горизонтальной прокрутки.
//   node --test tests/js/settings_subsections.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';

const TAB_SCHEMA = { type: 'object', additionalProperties: false, properties: {
  report_dirs: { type: 'array', title: 'Папки отчётов', items: { type: 'string', minLength: 1 } },
  mode: { title: 'Режим чтения', oneOf: [{ const: 'fast', title: 'Быстро' }, { const: 'full', title: 'Полностью' }] } } };
const ROWS = []; for (let t = 1; t <= 12; t++) for (let s = 1; s <= 15; s++) ROWS.push(['Тип ' + String(t).padStart(2, '0'), 'подтип ' + String(s).padStart(2, '0'), t * 100 + s]);
const ACTION = { id: 'row_types', title: 'Типы строк', description: 'Список типов строк', output_dir: false,
  params: { type: 'object', properties: {}, additionalProperties: false } };
const DONE = { state: 'done', started_at: '2026-11-02T09:12:00+03:00', seconds: 3.1, request: { params: {} },
  result: { ok: true, summary: ['типов: 12'], table: { columns: ['Тип', 'Подтип', 'Строк'], rows: ROWS }, issues: [] }, written: false };
const REPORT = { ok: true, backend: '0.2.17', spi: 1, name: 'synthetic', version: '0.9.0', contract: 'private/1.6', fingerprint: 'ab'.repeat(32), load_error: null,
  instruments: 'stub', calendar: 'none', files: { matched: 3, ignored: 0, match_failed: 0 }, problems: {}, notes: {}, reference: {}, reference_issues: [],
  sources: [{ source: 'pos_csv', report_date: '2026-10-20', valid_date: '2026-10-19', files: 3, rows: 40, units: { accepted: 3 }, statuses: { accepted: 3 }, issues: [], controls: [], unmapped: 0 }] };

// «Обновление адаптера» (форма ответа — как в adapter_update.pw.test.js): выпуск 0.5.0 новее используемого 0.4.0
const ADAPTER_UPDATE = { repo: 'FlyingScotman/trades-data-adapter', token_set: true, token_source: 'file', token_file: HOME + '\\github_token',
  source: 'bundle', explicit: false, explicit_note: null, pinned: false, pinned_note: null, in_use: { path: 'C:\\DM\\adapter\\tda', version: '0.4.0', source: 'bundle' },
  installed: null, bundle: { version: '0.4.0', path: 'C:\\DM\\adapter\\tda' }, base_version: '0.4.0', versions: [], previous_available: false,
  checked_at: '2026-11-03T08:00:00', check: { ok: true, at: '2026-11-03T08:00:00', layer: 'curl', attempts: [] },
  release: { tag: 'v0.5.0', version: '0.5.0', published_at: '2026-10-29T08:00:00Z', notes: '', asset: { name: 'tda-adapter.zip', size: 120000, sha256: 'a'.repeat(64), id: 11, browser_url: 'https://example.test/tda-adapter.zip' } },
  no_release: false, compare: 'newer', update_available: true, job: null, downloads_dir: 'C:\\Users\\u\\Downloads', downloads_dir_exists: true,
  browser_url: 'https://example.test/tda-adapter.zip', releases_page: 'https://example.test/releases/latest', layers: {}, codes: {}, it_hints: {} };

function server() {
  const st = { runs: 0 };
  st.settings = { home: HOME, hub_url: 'https://hub.example', inbox_dirs: [{ path: 'C:\\in', label: 'Позиции', recursive: false }], hub_token_set: true,
    proxy_password_set: false, env_overrides: [], error: null, autopilot: true,
    effective: { home: HOME, hub_url: 'https://hub.example', adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: true, autopilot: true } };
  st.handle = (u, method) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return st.settings;
    if (p === '/api/v1/adapter') return { loaded: true, path: 'C:\\desk\\adapter', reference: false, default: true, name: 'synthetic', version: '0.9.0',
      fingerprint: 'ab'.repeat(32), config_dir: HOME + '\\adapter-config', describe: { contract: 'private/1.6', sources: [{ source: 'pos_csv', dataset: 'positions' }] },
      config_files: [{ name: 'options.json', title: 'Параметры', exists: true }], settings_tabs: [{ id: 'sources', title: 'Источники', file: 'options.json', schema: TAB_SCHEMA, help: '' }],
      optional: ['status', 'run_action'], actions: ['row_types'], warnings: [], error: null };
    if (p === '/api/v1/adapter/config/options.json') return { name: 'options.json', exists: true, text: '{"report_dirs": ["C:\\\\in"], "mode": "fast"}\n', sha256: 'x' };
    if (p === '/api/v1/adapter/settings-context') return { recent_books: [], indices: [], currencies: ['RUB'], error: null };
    if (p === '/api/v1/adapter/status') return { available: true, at: '2026-11-03T09:00:00+03:00', timeout_s: 60, error: null, problems: [], issues: [],
      panels: [{ id: 'f', title: 'Папки отчётов', level: 'ok', lines: ['папок: 1'] }] };
    if (p === '/api/v1/adapter/actions') return { loaded: true, running: null, output_root: HOME + '\\adapter-output', timeout_s: 900, actions: [{ ...ACTION, job: DONE }] };
    if (p === '/api/v1/adapter/actions/row_types/run') { st.runs++; return { action: ACTION, running: true, job: { state: 'running', started_at: new Date().toISOString() } }; }
    if (p === '/api/v1/adapter/actions/row_types') return { action: ACTION, running: false, job: Object.assign({}, DONE, { started_at: new Date().toISOString() }) };
    if (p === '/api/v1/adapter/check') return { running: false, timeout_s: 1800, options: [{ path: 'C:\\in', kind: 'inbox', exists: true }],
      job: { state: 'done', started_at: '2026-11-02T10:00:00+03:00', finished_at: '2026-11-02T10:01:00+03:00', seconds: 60, request: { dirs: ['C:\\in'] }, report: REPORT, text: 'check: OK', ok: true } };
    if (p === '/api/v1/update/adapter') return ADAPTER_UPDATE;
    if (p === '/api/v1/adapter-services' && st.services) return { available: true, code: 'available', name: 'synthetic', version: '0.1', running: null, scheduler: true,
      job: { state: 'idle', code: 'idle' }, panel: { title: 'Тестовые сервисы', status: ['Среда: тест'], sections: [{ id: 'c', title: 'Подключение', help: '',
        fields: [{ key: 'url', label: 'Адрес', type: 'url', value: 'https://synthetic.invalid', help: '', placeholder: '' }], actions: [{ id: 'check', label: 'Проверить', confirm: '', long: false }] }] },
      feeds: [{ id: 'eodpos', title: 'Позиции', dataset: 'positions', source: 's2', enabled: true, enabled_default: false, ready: true, why_not_ready: '', schedule: { daily_at: ['08:30'] },
        after_positions: false, running: false, queued: false, next_run: null, last: { last_started: '2026-10-16T08:30:00+03:00', state: 'blocked', code: 'login_required', needs_login: true, failures: 0 } }] };
    if (p === '/api/v1/autopilot/status') return { enabled: true, state: 'idle', steps: [{ name: 'sync', status: 'ok', detail: 'срезов: 1' }] };
    if (p === '/api/v1/backfill/status') return { enabled: true, dates: 10, done: 10, todo: 0, no_positions: 0, no_market: 0 };
    if (p === '/api/v1/acquisition') return { available: false, code: 'runtime_unavailable', job: { state: 'idle', phase: 'idle', code: 'idle' } };
    return null;
  };
  return st;
}

async function open(browser, st, hash, opts) {
  const ctx = (opts && opts.ctx) || await browser.newContext(Object.assign({ acceptDownloads: true }, opts && opts.viewport ? { viewport: opts.viewport } : {}));
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      const out = st.handle(u, req.method());
      return out ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html' + hash);
  await pg.waitForSelector('#st-sum:not([hidden]) a');
  return { pg, ctx, errors };
}
const onSub = pg => pg.evaluate(() => [...document.querySelectorAll('#sec-adapter .sub.on')].map(x => x.dataset.sub));
const waitSub = (pg, name) => pg.waitForFunction(n => { const x = document.querySelector('#sec-adapter .sub.on'); return x && x.dataset.sub === n; }, name);  // hashchange приходит позже щелчка
const onSec = pg => pg.evaluate(() => document.querySelector('section.sec.on').dataset.sec);

test('настройки: подразделы, «Дополнительно», поиск, результаты по щелчку (TDM#551, TDM#552)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('подразделы по адресу: напрямую, после обновления, последний открытый, элемент внутри, стрелки', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter');
      assert.deepStrictEqual(await onSub(pg), ['overview'], 'раздел открывается обзором');
      assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/overview', 'в адресе — подраздел');
      assert.ok(await pg.locator('#ad-res').isVisible() && !(await pg.locator('#dirs').isVisible()) && !(await pg.locator('#ad-check').isVisible()));
      await pg.locator('.subnav a[data-sub-link="folders"]').click();
      await waitSub(pg, 'folders');
      assert.deepStrictEqual(await onSub(pg), ['folders']);
      assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/folders');
      assert.strictEqual(await pg.locator('.subnav a[aria-current="page"]').getAttribute('data-sub-link'), 'folders');
      await pg.reload(); await pg.waitForSelector('#st-sum:not([hidden]) a');
      assert.deepStrictEqual(await onSub(pg), ['folders'], 'переживает обновление страницы');
      await pg.locator('.st-nav a[data-sec="hub"]').click();
      await pg.locator('.st-nav a[data-sec="adapter"]').click();
      assert.deepStrictEqual(await onSub(pg), ['folders'], 'без подраздела — последний открытый');
      await pg.goto(ORIGIN + '/settings.html#ad-check');
      await pg.waitForTimeout(100);
      assert.deepStrictEqual(await onSub(pg), ['check'], 'карточка внутри — её подраздел');
      await pg.goto(ORIGIN + '/settings.html#tab-sources');
      await pg.waitForSelector('#tab-sources .sf-grid');
      assert.deepStrictEqual(await onSub(pg), ['sources']);
      await pg.goto(ORIGIN + '/settings.html#adapter/nope');
      await pg.waitForTimeout(100);
      assert.strictEqual(await onSec(pg), 'adapter');
      assert.strictEqual((await onSub(pg)).length, 1, 'неизвестный подраздел — открыт запомненный');
      // клавиатура: стрелки между подразделами, Enter открывает
      await pg.locator('.subnav a[data-sub-link="overview"]').focus();
      await pg.keyboard.press('ArrowRight');
      assert.strictEqual(await pg.evaluate(() => document.activeElement.dataset.subLink), 'folders', 'скрытый «Сервисы» пропущен');
      await pg.keyboard.press('End');
      assert.strictEqual(await pg.evaluate(() => document.activeElement.dataset.subLink), 'expert');
      await pg.keyboard.press('Enter');
      await pg.waitForTimeout(100);
      assert.deepStrictEqual(await onSub(pg), ['expert']);
      // ошибка в поле другого подраздела при сохранении: открыт его подраздел, блок раскрыт, курсор в поле
      await pg.locator('#adapter_read_timeout_s').evaluate(e => { e.value = '-1'; e.dispatchEvent(new Event('input', { bubbles: true })); });
      await pg.locator('.subnav a[data-sub-link="folders"]').click();
      await pg.locator('#save').click();
      await pg.waitForTimeout(100);
      assert.deepStrictEqual(await onSub(pg), ['expert']);
      assert.strictEqual(await pg.evaluate(() => document.activeElement.id), 'adapter_read_timeout_s');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('«Обновление адаптера» — в «Обновлениях»; в «Обзоре» адаптера ссылка и строка состояния', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter/overview');
      assert.strictEqual(await pg.locator('#sec-adapter #ad-upd-card').count(), 0, 'карточки в разделе адаптера нет');
      const lnk = pg.locator('#ad-card .hd a[href="#ad-upd-card"]');
      assert.ok(await lnk.isVisible(), 'ссылка в шапке карточки «Адаптер»');
      await pg.waitForFunction(() => /есть выпуск 0\.5\.0/.test(document.getElementById('ad-upd-line').textContent));
      assert.ok(/Обновление адаптера: есть выпуск 0\.5\.0/.test(await pg.locator('#ad-upd-line').innerText()), await pg.locator('#ad-upd-line').innerText());
      const item = pg.locator('#st-sum a[href="#ad-upd-card"]');
      assert.ok(/Адаптер:\s*есть обновление 0\.5\.0/.test(await item.innerText()), 'строка в сводке сверху');
      await lnk.click();
      await pg.waitForTimeout(100);
      assert.strictEqual(await onSec(pg), 'up');
      assert.ok(await pg.locator('section.sec[data-sec=up] #ad-upd-card').isVisible());
      assert.ok(/Обновление программы и адаптера/.test(await pg.locator('#h-up').innerText()));
      assert.ok(/Обновления/.test(await pg.locator('.st-nav a[data-sec="up"]').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('«Дополнительно»: свёрнуто, помнится; раздел оттуда по ссылке раскрывает группу', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#hub');
      assert.ok(!(await pg.locator('.st-nav a[data-sec="calc"]').isVisible()) && await pg.locator('.st-nav a[data-sec="up"]').isVisible());
      assert.strictEqual(await pg.locator('#st-more').getAttribute('aria-expanded'), 'false');
      await pg.goto(ORIGIN + '/settings.html#calc');
      await pg.waitForTimeout(100);
      assert.ok(await pg.locator('.st-nav a[data-sec="calc"]').isVisible() && await onSec(pg) === 'calc', 'по ссылке — раскрыта');
      await pg.reload(); await pg.waitForSelector('#st-sum:not([hidden]) a');
      await pg.goto(ORIGIN + '/settings.html#hub'); await pg.reload(); await pg.waitForSelector('#st-sum:not([hidden]) a');
      assert.ok(!(await pg.locator('.st-nav a[data-sec="calc"]').isVisible()), 'раскрытие по ссылке не запоминается');
      await pg.locator('#st-more').click();
      assert.ok(await pg.locator('.st-nav a[data-sec="diag"]').isVisible());
      await pg.reload(); await pg.waitForSelector('#st-sum:not([hidden]) a');
      assert.ok(await pg.locator('.st-nav a[data-sec="diag"]').isVisible(), 'открытость помнит браузер');
      // ссылка из сводки в свёрнутую группу (#autopilot — «Автообновление»)
      await pg.locator('#st-more').click();
      await pg.locator('#st-sum a', { hasText: 'Автообновление' }).click();
      await pg.waitForTimeout(100);
      assert.strictEqual(await onSec(pg), 'auto');
      assert.ok(await pg.locator('.st-nav a[data-sec="auto"]').isVisible());
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('поиск: подпись и ключ settings.json, стрелки, Enter — к полю (подраздел, раскрытый блок, фокус), Esc', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#hub');
      const q = pg.locator('#st-q');
      await q.fill('архив принятых');
      const first = pg.locator('#st-q-res .st-q-it').first();
      await first.waitFor();
      assert.ok(/Архив принятых файлов/.test(await first.innerText()) && /Адаптер и источники › Папки/.test(await first.innerText()), await first.innerText());
      await q.press('Enter');
      await pg.waitForTimeout(150);
      assert.strictEqual(await onSec(pg), 'adapter');
      assert.deepStrictEqual(await onSub(pg), ['folders']);
      assert.ok(await pg.locator('#adv-dirs').evaluate(d => d.open), 'свёрнутый блок раскрыт');
      assert.strictEqual(await pg.evaluate(() => document.activeElement.id), 'inbox_archive');
      assert.ok(await pg.locator('#st-q-res').isHidden());
      // по ключу settings.json и стрелками
      await q.fill('adapter_read_timeout');
      await pg.locator('#st-q-res .st-q-it').first().waitFor();
      assert.strictEqual(await pg.locator('#st-q-res .st-q-it').count(), 1);
      await q.press('ArrowDown'); await q.press('Enter');
      await pg.waitForTimeout(150);
      assert.deepStrictEqual(await onSub(pg), ['expert']);
      assert.strictEqual(await pg.evaluate(() => document.activeElement.id), 'adapter_read_timeout_s');
      // поле формы адаптера и раздел в «Дополнительно»
      await q.fill('режим чтения');
      await pg.locator('#st-q-res .st-q-it').first().waitFor();
      await q.press('Enter'); await pg.waitForTimeout(150);
      assert.deepStrictEqual(await onSub(pg), ['sources']);
      await q.fill('отчёты проверки');
      await pg.locator('#st-q-res .st-q-it').first().waitFor();
      await q.press('Enter'); await pg.waitForTimeout(150);
      assert.strictEqual(await onSec(pg), 'calc');
      assert.strictEqual(await pg.evaluate(() => document.activeElement.id), 'report_dir');
      // ничего не нашлось; Esc закрывает, второй Esc — очищает
      await q.fill('zzzz');
      assert.ok(/Ничего не нашлось/.test(await pg.locator('#st-q-res').innerText()));
      await q.press('Escape');
      assert.ok(await pg.locator('#st-q-res').isHidden());
      await q.press('Escape');
      assert.strictEqual(await q.inputValue(), '');
      // «/» — в поиск
      await pg.locator('section.sec.on h1').click();
      await pg.keyboard.press('/');
      assert.strictEqual(await pg.evaluate(() => document.activeElement.id), 'st-q');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('итог действия — «Последний запуск» по щелчку; 180 строк — первые 10, все, поиск, CSV; проверка свёрнута', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter/actions');
      const card = pg.locator('#ad-action-row_types');
      const last = card.locator('details.at-last');
      await last.waitFor();
      assert.ok(!(await last.evaluate(d => d.open)), 'после загрузки страницы итог свёрнут');
      const sum = await last.locator('summary').innerText();
      assert.ok(/Последний запуск/.test(sum) && /02\.11\.2026/.test(sum) && /выполнено/.test(sum) && /типов: 12/.test(sum) && /180 стр/.test(sum), sum);
      const short = await pg.evaluate(() => document.documentElement.scrollHeight);
      assert.ok(short < 1400, 'страница обычной длины: ' + short);
      await last.locator('summary').click();
      const lt = last.locator('.lt-long');
      assert.strictEqual(await lt.locator('tbody tr').count(), 10, 'первые строки');
      assert.ok(/180 строк, показаны первые 10/.test(await lt.locator('.lt-n').innerText()));
      await lt.locator('.lt-more').click();
      assert.strictEqual(await lt.locator('tbody tr').count(), 180);
      await lt.locator('.lt-more').click();
      assert.strictEqual(await lt.locator('tbody tr').count(), 10, 'свернуть');
      await lt.locator('.lt-q').fill('тип 03');
      assert.ok(/найдено 15 строк из 180/.test(await lt.locator('.lt-n').innerText()), await lt.locator('.lt-n').innerText());
      assert.strictEqual(await lt.locator('tbody tr').count(), 15, 'найденное — целиком');
      assert.ok((await lt.locator('tbody tr').allInnerTexts()).every(r => /Тип 03/.test(r)));
      // имя файла — из атрибута download (Chromium без UTF-8 в локали отдаёт кириллическое имя как «download»)
      await pg.evaluate(() => { const c = HTMLAnchorElement.prototype.click; HTMLAnchorElement.prototype.click = function () { window.__dl = this.getAttribute('download'); return c.call(this); }; });
      const [dl] = await Promise.all([pg.waitForEvent('download'), lt.locator('.lt-csv').click()]);
      assert.strictEqual(await pg.evaluate(() => window.__dl), 'типы_строк.csv');
      const csv = fs.readFileSync(await dl.path(), 'utf8');
      assert.ok(csv.startsWith('\ufeffТип;Подтип;Строк\r\nТип 03;подтип 01;301'), csv.slice(0, 80));
      await last.locator('summary').click();
      assert.ok(!(await last.evaluate(d => d.open)));
      // новый запуск: итог остаётся свёрнутым, а строка «Последний запуск» обновляется сразу (новая дата)
      await card.locator('button', { hasText: 'Запустить' }).click();
      await pg.waitForFunction(() => { const d = document.querySelector('#ad-action-row_types details.at-last'); return d && !/02\.11\.2026/.test(d.querySelector('summary').textContent); }, null, { timeout: 10000 });
      assert.ok(!(await pg.locator('#ad-action-row_types details.at-last').evaluate(d => d.open)), 'после запуска итог свёрнут');
      assert.strictEqual(st.runs, 1);
      // проверка: вердикт на виду, подробности — по щелчку
      await pg.locator('.subnav a[data-sub-link="check"]').click();
      await pg.locator('#ad-check-res .verdict').waitFor();
      assert.ok(/соответствует контракту/.test(await pg.locator('#ad-check-res .verdict').innerText()));
      assert.ok(!(await pg.locator('#ad-check-res details.at-check').evaluate(d => d.open)) && !(await pg.locator('#ad-check-res .k-tbl').isVisible()));
      assert.strictEqual(await pg.locator('.subnav .dot[data-subdot="check"]').getAttribute('class'), 'dot ok');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('места для блоков других модулей: подраздел, раздел, строка сводки', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#ovp');
      assert.strictEqual(await onSec(pg), 'hub', 'место без модуля не открывается');
      assert.ok(!(await pg.locator('.st-nav a[data-sec="ovp"]').isVisible()) && !(await pg.locator('.subnav a[data-sub-link="services"]').isVisible()));
      await pg.evaluate(() => {
        const a = window.DeskSettings.slot('services'); a.append(Object.assign(document.createElement('p'), { textContent: 'Подключение к сервисам' }));
        const b = window.DeskSettings.slot('ovp-types'); b.textContent = 'Типы строк';
        window.DeskSettings.slot('reread').textContent = 'Перечитать';
        window.DeskSettings.slot('books-extra').textContent = 'Только ОВП';
        window.DeskSettings.status('feeds', { cls: 'warn', href: '#adapter/services', k: 'Сервисы', text: 'нужен вход' });
        window.DeskSettings.dot('adapter/services', 'warn', 'нужен вход');
      });
      assert.ok(/Сервисы:\s*нужен вход/.test(await pg.locator('#st-sum').innerText()));
      await pg.locator('#st-sum a', { hasText: 'нужен вход' }).click();
      await pg.waitForTimeout(100);
      assert.deepStrictEqual(await onSub(pg), ['services']);
      assert.ok(await pg.locator('[data-slot="services"]').isVisible());
      assert.strictEqual(await pg.locator('.subnav .dot[data-subdot="services"]').getAttribute('class'), 'dot warn');
      await pg.locator('.st-nav a[data-sec="ovp"]').click();
      await pg.waitForTimeout(100);
      assert.strictEqual(await onSec(pg), 'ovp');
      assert.ok(await pg.locator('[data-slot="ovp-types"]').isVisible());
      await pg.evaluate(() => window.DeskSettings.status('feeds', null));
      assert.ok(!/нужен вход/.test(await pg.locator('#st-sum').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('прямая ссылка на место модуля не теряется до slot(): подраздел и раздел', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter/services');
      assert.deepStrictEqual(await onSub(pg), ['overview'], 'пока модуля нет — открыт запасной');
      assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/services', 'адрес не переписан');
      assert.strictEqual(await pg.evaluate(() => localStorage.getItem('settings.sub.adapter')), null, 'запасной подраздел не запомнен');
      await pg.evaluate(() => setTimeout(() => { window.DeskSettings.slot('services'); }, 200));
      await pg.waitForFunction(() => { const x = document.querySelector('#sec-adapter .sub.on'); return x && x.dataset.sub === 'services'; });
      assert.ok(await pg.locator('[data-slot="services"]').isVisible());
      // раздел-место: #ovp после других адресов
      await pg.goto(ORIGIN + '/settings.html#adapter/folders'); await pg.waitForTimeout(100);
      await pg.goto(ORIGIN + '/settings.html#ovp'); await pg.waitForTimeout(100);
      assert.strictEqual(await onSec(pg), 'adapter', 'запасной раздел — последний открытый');
      assert.strictEqual(await pg.evaluate(() => location.hash), '#ovp');
      assert.strictEqual(await pg.evaluate(() => localStorage.getItem('settings.sec')), 'adapter', 'запасной раздел не запомнен как выбранный');
      await pg.evaluate(() => { window.DeskSettings.slot('ovp-types').textContent = 'Типы строк'; });
      await pg.waitForFunction(() => document.querySelector('section.sec.on').dataset.sec === 'ovp');
      await pg.reload(); await pg.waitForSelector('#st-sum:not([hidden]) a');
      assert.notStrictEqual(await onSec(pg), 'ovp', 'после обновления модуль ещё не загрузился');
      assert.strictEqual(await pg.evaluate(() => location.hash), '#ovp');
      await pg.evaluate(() => { window.DeskSettings.slot('ovp-types'); });
      await pg.waitForFunction(() => document.querySelector('section.sec.on').dataset.sec === 'ovp');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('onShow, unslot, поднятие точки подраздела к пункту раздела', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter/overview');
      await pg.evaluate(() => { window.__ev = []; window.__off = window.DeskSettings.onShow(d => window.__ev.push([d.sec, d.sub, d.hash])); });
      assert.deepStrictEqual(await pg.evaluate(() => window.__ev.map(e => e.slice(0, 2))), [['adapter', 'overview']], 'сразу — текущее состояние');
      await pg.locator('.subnav a[data-sub-link="folders"]').click();
      await waitSub(pg, 'folders');
      assert.deepStrictEqual(await pg.evaluate(() => window.__ev.pop()), ['adapter', 'folders', '#adapter/folders']);
      await pg.evaluate(() => window.DeskSettingsNav.go('adapter/expert'));  // переход из поиска
      assert.deepStrictEqual(await pg.evaluate(() => window.__ev.pop().slice(0, 2)), ['adapter', 'expert']);
      await pg.locator('.st-nav a[data-sec="hub"]').click();
      await pg.waitForFunction(() => document.querySelector('section.sec.on').dataset.sec === 'hub');
      assert.deepStrictEqual(await pg.evaluate(() => window.__ev.pop().slice(0, 2)), ['hub', null]);
      await pg.evaluate(() => window.__off());
      const n = await pg.evaluate(() => window.__ev.length);
      await pg.locator('.st-nav a[data-sec="adapter"]').click();
      await pg.waitForFunction(() => document.querySelector('section.sec.on').dataset.sec === 'adapter');
      assert.strictEqual(await pg.evaluate(() => window.__ev.length), n, 'после отписки событий нет');
      // unslot: открытое место скрывается, открывается запасной подраздел
      await pg.evaluate(() => { window.DeskSettings.slot('services'); });
      await pg.locator('.subnav a[data-sub-link="services"]').click();
      assert.deepStrictEqual(await onSub(pg), ['services']);
      await pg.evaluate(() => window.DeskSettings.unslot('services'));
      assert.deepStrictEqual(await onSub(pg), ['overview']);
      assert.ok(!(await pg.locator('.subnav a[data-sub-link="services"]').isVisible()) && !(await pg.locator('[data-slot="services"]').isVisible()));
      assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/overview');
      // точка подраздела поднимается к пункту раздела; убрали — снова своя
      const navDot = pg.locator('.st-nav .dot[data-nav="adapter"]');
      await pg.waitForFunction(() => document.querySelector('.st-nav .dot[data-nav="adapter"]').className === 'dot ok');
      await pg.evaluate(() => window.DeskSettings.dot('adapter/services', 'bad', 'нет входа'));
      assert.ok(/\bbad\b/.test(await navDot.getAttribute('class')));
      await pg.evaluate(() => window.DeskSettings.dot('adapter/services', ''));
      assert.strictEqual(await navDot.getAttribute('class'), 'dot ok');
      await pg.evaluate(() => window.DeskSettings.dot('adapter/folders', 'warn', 'x'));
      assert.ok(/\bwarn\b/.test(await navDot.getAttribute('class')));
      await pg.evaluate(() => window.DeskSettings.dot('adapter/folders', ''));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('история: переход из поиска — новая запись; повторный щелчок по открытому не плодит записи', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#hub');
      const q = pg.locator('#st-q');
      await q.fill('архив принятых');
      await pg.locator('#st-q-res .st-q-it').first().waitFor();
      await q.press('Enter');
      await pg.waitForTimeout(150);
      assert.strictEqual(await onSec(pg), 'adapter');
      await pg.goBack();
      await pg.waitForTimeout(150);
      assert.strictEqual(await onSec(pg), 'hub', 'назад — на раздел, откуда искали');
      await pg.goto(ORIGIN + '/settings.html#adapter/folders'); await pg.waitForTimeout(100);
      const len = await pg.evaluate(() => history.length);
      await pg.locator('.st-nav a[data-sec="adapter"]').click(); await pg.locator('.st-nav a[data-sec="adapter"]').click();
      await pg.locator('.subnav a[data-sub-link="folders"]').click(); await pg.locator('.subnav a[data-sub-link="folders"]').click();
      assert.strictEqual(await pg.evaluate(() => history.length), len, 'записей истории не прибавилось');
      assert.deepStrictEqual(await onSub(pg), ['folders']);
      assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/folders');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('поиск не находит поля разделов, скрытых от пользователя', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#hub');
      const q = pg.locator('#st-q');
      await q.fill('Выход без действий');
      await pg.locator('#st-q-res .st-q-it').first().waitFor();
      assert.ok(/Выход без действий/.test(await pg.locator('#st-q-res').innerText()), 'администратору поле видно');
      await pg.evaluate(() => document.querySelectorAll('[data-users-nav]').forEach(e => { e.hidden = true; }));
      await q.fill('Выход без действий');
      assert.ok(/Ничего не нашлось/.test(await pg.locator('#st-q-res').innerText()), await pg.locator('#st-q-res').innerText());
      await q.fill('Адрес сервера');
      assert.ok(/Адрес сервера/.test(await pg.locator('#st-q-res .st-q-it').first().innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('итог действия и подробности проверки: итоги всегда свёрнуты после запуска; выбор читателя не сбивается «Обновить статус»', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter/actions');
      const last = pg.locator('#ad-action-row_types details.at-last');
      await last.waitFor();
      await pg.locator('#ad-action-row_types button', { hasText: 'Запустить' }).click();
      await pg.waitForFunction(() => { const d = document.querySelector('#ad-action-row_types details.at-last'); return d && !/02\.11\.2026/.test(d.querySelector('summary').textContent); }, null, { timeout: 10000 });
      assert.ok(!(await pg.locator('#ad-action-row_types details.at-last').evaluate(d => d.open)), 'после запуска свёрнут, строка уже обновлена');
      await pg.locator('#ad-action-row_types details.at-last summary').click();
      await pg.locator('#ad-refresh').click();
      await pg.waitForTimeout(500);
      assert.ok(await pg.locator('#ad-action-row_types details.at-last').evaluate(d => d.open), 'раскрытый остался раскрытым');
      await pg.locator('#ad-action-row_types details.at-last summary').click();
      await pg.locator('#ad-refresh').click();
      await pg.waitForTimeout(500);
      assert.ok(!(await pg.locator('#ad-action-row_types details.at-last').evaluate(d => d.open)), 'свёрнутый остался свёрнутым');
      // проверка адаптера: то же
      await pg.locator('.subnav a[data-sub-link="check"]').click();
      await pg.locator('#ad-check-run').click();
      await pg.locator('#ad-check-res details.at-check').waitFor();
      assert.ok(!(await pg.locator('#ad-check-res details.at-check').evaluate(d => d.open)), 'после проверки подробности свёрнуты');
      assert.ok(/источников: 1, строк по датам: 1/.test(await pg.locator('#ad-check-res details.at-check > summary').innerText()), await pg.locator('#ad-check-res details.at-check > summary').innerText());
      await pg.locator('#ad-check-res details.at-check > summary').click();
      await pg.locator('#ad-refresh').click();
      await pg.waitForTimeout(500);
      assert.ok(await pg.locator('#ad-check-res details.at-check').evaluate(d => d.open), 'проверка: раскрытая осталась раскрытой');
      await pg.locator('#ad-check-res details.at-check > summary').click();
      await pg.locator('#ad-refresh').click();
      await pg.waitForTimeout(500);
      assert.ok(!(await pg.locator('#ad-check-res details.at-check').evaluate(d => d.open)), 'проверка: свёрнутая осталась свёрнутой');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('«Источники»: адаптер не загружен — заметка; точка «изменено» у подраздела', async () => {
      const st = server(), real = st.handle;
      st.handle = (u, m) => (u.pathname === '/api/v1/adapter' ? { loaded: false, error: 'нет адаптера', settings_tabs: [], optional: [], actions: [] } : real(u, m));
      const { pg, ctx, errors } = await open(browser, st, '#adapter/sources');
      const none = pg.locator('#tabs-none');
      await none.waitFor();
      assert.ok(/Адаптер не загружен — формы источников появятся/.test(await none.innerText()), await none.innerText());
      assert.strictEqual(await none.locator('a').getAttribute('href'), '#adapter/overview');
      assert.ok(await pg.locator('[data-sub="sources"] a[href="#advanced"]').isVisible(), 'ссылка на правку JSON');
      await pg.locator('[data-sub="sources"] a[href="#advanced"]').click();
      await pg.waitForTimeout(100);
      assert.deepStrictEqual(await onSub(pg), ['expert']);
      // изменили поле — у подраздела точка «изменено»; «Отменить» снимает
      const dot = pg.locator('.subnav .dot[data-subdot="expert"]');
      await pg.locator('#adapter_read_timeout_s').evaluate(e => { e.value = '12'; e.dispatchEvent(new Event('input', { bubbles: true })); });
      assert.ok(/\bdirty\b/.test(await dot.getAttribute('class')));
      assert.ok(/\bdirty\b/.test(await pg.locator('.st-nav .dot[data-nav="adapter"]').getAttribute('class')), 'и у раздела');
      await pg.locator('#save-undo').click();
      assert.ok(!/\bdirty\b/.test(await dot.getAttribute('class')), 'после «Отменить» снято');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });

    await t.test('телефон 390 px: без горизонтальной прокрутки, выбранный подраздел виден', async () => {
      const st = server(); st.services = true;
      for (const hash of ['#adapter/services', '#adapter/actions', '#adapter/check', '#adapter/expert', '#up', '#hub']) {
        const { pg, ctx, errors } = await open(browser, st, hash, { viewport: { width: 390, height: 800 } });
        await pg.waitForTimeout(300);
        const over = await pg.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        assert.ok(over <= 1, hash + ': лишняя ширина ' + over);
        if (hash === '#adapter/services') assert.ok(await pg.locator('#svc-card').isVisible(), 'блок «Сервисы» виден');
        if (hash.startsWith('#adapter')) {
          const box = await pg.locator('.subnav a.on').boundingBox();
          assert.ok(box && box.x >= 0 && box.x + box.width <= 391, hash + ': подраздел виден ' + JSON.stringify(box));
        }
        assert.ok(await pg.locator('#st-q').isVisible(), 'поиск виден');
        await pg.locator('#st-q').fill('папк');
        await pg.locator('#st-q-res .st-q-it').first().waitFor();
        await pg.locator('#st-q').press('ArrowDown');
        const over2 = await pg.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth + scrollX);
        assert.ok(over2 <= 1, hash + ': с открытым поиском лишняя ширина ' + over2);
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      }
    });
  } finally { await browser.close(); }
});
