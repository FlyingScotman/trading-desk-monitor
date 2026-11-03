// Книги ОВП без позиций в настройках книг (TDM#556), в браузере (Playwright), сервер — заглушка маршрутов:
//  - книга ОВП, которой нет в books.json, — среди «не настроено» с пометкой «только ОВП» и датой ОВП; «+ Добавить»
//    открывает карточку, где юрлицо, тип книги и деск — выпадающие списки;
//  - уже настроенная книга ОВП — пометка «только ОВП» у кода книги в таблице;
//  - экран без горизонтальной прокрутки на 390 и 1024 px. SHOT_DIR=<папка> — снимки для человека (deviceScaleFactor 2).
//   node --test tests/js/settings_ocp_books.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const FIX = path.join(__dirname, '..', '..', 'backend', 'tests', 'fixtures', 'settings_tabs');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';
const TABS = JSON.parse(fs.readFileSync(path.join(FIX, 'tda_settings_tabs.json'), 'utf8')).filter(t => t.id === 'books' || t.id === 'entities');
const BOOKS = { books: [
  { book_id: 'T1', name: 'Synthetic bond book', desk: 'SYN_DESK', book_type: 'TRADING', entity: 'ENT1' },
  { book_id: 'OLD_SET', name: 'Old book, set up', desk: 'LEGACY', book_type: 'HTM', entity: 'ENT2' }] };
const ENTITIES = { entities: [{ entity: 'ENT1', name: 'Entity one', funding_scheme: 'by_book_type' },
  { entity: 'ENT2', name: 'Entity two', funding_scheme: 'by_book_type' }] };
const RECENT = [
  { book_id: 'T1', last_date: '2026-10-20', source: 'pos', positions: 3, name: 'T1', desk: 'SYN_DESK', book_type: 'TRADING', entity: 'ENT1', trader: null, ocp_only: false },
  { book_id: 'OLD_SET', last_date: '2026-10-20', source: 'ocp', positions: null, name: 'OLD_SET', desk: 'LEGACY', book_type: 'HTM', entity: 'ENT2', trader: null, ocp_only: true },
  { book_id: 'OLD_NEW', last_date: '2026-10-19', source: 'ocp', positions: null, name: 'OLD_NEW', desk: 'AUTO', book_type: 'TRADING', entity: 'ENT1', trader: null, ocp_only: true }];

function server() {
  const st = {};
  const file = (name, doc) => ({ name, path: HOME + '\\adapter-config\\' + name, exists: true, text: JSON.stringify(doc, null, 2) + '\n', sha256: 'x' });
  st.handle = (u, method) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return { home: HOME, hub_url: 'https://hub.example', inbox_dirs: [], hub_token_set: true, proxy_password_set: false,
      effective: { home: HOME, adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: false }, env_overrides: [], error: null };
    if (p === '/api/v1/adapter') return { loaded: true, path: 'C:\\desk\\adapter', reference: false, default: false, name: 'synthetic', version: '0.9.0',
      fingerprint: 'ab'.repeat(32), config_dir: HOME + '\\adapter-config', describe: { contract: 'private/1.7', sources: [] },
      config_files: [{ name: 'books.json', title: 'Книги', exists: true }, { name: 'entities.json', title: 'Юрлица', exists: true }],
      settings_tabs: TABS, optional: [], actions: [], warnings: [], error: null };
    if (p === '/api/v1/adapter/settings-context') return { recent_books: RECENT, indices: [], currencies: ['RUB', 'USD'], error: null, recent_days: 35 };
    if (p === '/api/v1/adapter/config/books.json') return file('books.json', BOOKS);
    if (p === '/api/v1/adapter/config/entities.json') return file('entities.json', ENTITIES);
    return null;
  };
  return st;
}

async function open(browser, viewport) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: process.env.SHOT_DIR ? 2 : 1 });
  const pg = await ctx.newPage(), errors = [], st = server();
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
  await pg.goto(ORIGIN + '/settings.html#books');
  await pg.waitForSelector('#sec-books tr.unconf');
  return { pg, ctx, errors };
}

test('настройки книг: книги ОВП без позиций (TDM#556)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const [w, h] of [[1024, 900], [390, 844]]) {
      await t.test('ширина ' + w, async () => {
        const { pg, ctx, errors } = await open(browser, { width: w, height: h });
        const sec = pg.locator('#sec-books');
        const text = await sec.innerText();
        assert.ok(/Есть в отчёте ОВП, не настроено — 1/.test(text), text);
        const row = sec.locator('tr.unconf', { hasText: 'OLD_NEW' });
        const rt = await row.innerText();
        assert.ok(/не настроено/.test(rt) && /только ОВП/.test(rt) && /ОВП на 19\.10\.2026, позиций нет/.test(rt) && /юрлицо из отчёта: ENT1/.test(rt), rt);
        // настроенная книга ОВП — пометка у кода книги; книга с позициями — без неё
        const set = sec.locator('tr.row', { hasText: 'OLD_SET' });
        assert.ok(/только ОВП/.test(await set.innerText()));
        assert.ok(!/только ОВП/.test(await sec.locator('tr.row', { hasText: 'Synthetic bond book' }).innerText()));
        // карточка «Книги только из отчёта ОВП» в месте раздела «Книги» (DeskSettings.slot('books-extra'))
        const card0 = pg.locator('[data-slot="books-extra"] #ocp-books-card');
        await card0.waitFor();
        const ct = await card0.innerText();
        assert.ok(/Книги только из отчёта ОВП/i.test(ct) && /2 · не настроено 1/.test(ct), ct);
        assert.ok(/настроено/.test(await card0.locator('tr[data-ocp-book="OLD_SET"]').innerText()) && /HTM/.test(await card0.locator('tr[data-ocp-book="OLD_SET"]').innerText()));
        assert.strictEqual(await card0.locator('tr[data-ocp-book="T1"]').count(), 0, 'книга с позициями — не здесь');
        const noScroll = await pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
        assert.ok(noScroll, 'без горизонтальной прокрутки страницы');
        if (process.env.SHOT_DIR) await sec.screenshot({ path: path.join(process.env.SHOT_DIR, 'books-' + w + '.png') });
        // «+ Добавить»: карточка книги — юрлицо, тип, деск списками; деск — из десков книг и справочника, без AUTO
        await row.locator('button', { hasText: '+ Добавить' }).click();
        const card = pg.locator('.drawer, .sf-drawer, [role=dialog]').filter({ hasText: 'OLD_NEW' }).last();
        await card.waitFor();
        const selects = await card.locator('select').evaluateAll(els => els.map(s => ({ id: s.id, opts: Array.from(s.options, o => o.value) })));
        const has = vals => selects.some(s => vals.every(v => s.opts.includes(v)));
        assert.ok(has(['ENT1', 'ENT2']), 'юрлицо списком: ' + JSON.stringify(selects));
        assert.ok(has(['TRADING', 'HTM', 'DERIVS', 'FUTURES']), 'тип книги списком');
        // деск списком: дески книг файла и справочника; заглушка авто-книги AUTO — только как текущее значение
        // («нет в списке»), последним перед «другое значение…»
        const desk = selects.find(s => s.opts.includes('LEGACY'));
        assert.ok(desk && desk.opts.includes('SYN_DESK') && desk.opts.includes('\u0000other'), JSON.stringify(selects));
        assert.deepStrictEqual(desk.opts.filter(v => v === 'AUTO').length, 1);
        assert.strictEqual(await card.locator('#' + desk.id.replace(/\./g, '\\.')).inputValue(), 'AUTO');
        const cardNoScroll = await pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);
        assert.ok(cardNoScroll, 'карточка без горизонтальной прокрутки');
        if (process.env.SHOT_DIR) await pg.screenshot({ path: path.join(process.env.SHOT_DIR, 'book-card-' + w + '.png') });
        // из карточки: «Изменить» открывает запись настроенной книги
        await pg.keyboard.press('Escape');
        await pg.locator('#ocp-books-card tr[data-ocp-book="OLD_SET"] button', { hasText: 'Изменить' }).click();
        await pg.locator('.drawer, .sf-drawer, [role=dialog]').filter({ hasText: 'OLD_SET' }).last().waitFor();
        if (process.env.SHOT_DIR) { await pg.keyboard.press('Escape'); await pg.locator('#ocp-books-card').screenshot({ path: path.join(process.env.SHOT_DIR, 'ocp-card-' + w + '.png') }); }
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      });
    }
  } finally { await browser.close(); }
});
