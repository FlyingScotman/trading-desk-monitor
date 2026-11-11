// «Индексы ставок» в браузере (Playwright), сервер — заглушка маршрутов (TDM#565):
//  - экран настроек: названия индексов из последних файлов условий, у каждого выпадающий список кодов, предложенный код
//    стоит сразу и помечен, «новый» на месте; «Сохранить и пересчитать» пишет только отличия, затем перечитывание и
//    пересчёт до «Готово: условия перечитаны, сделки пересчитаны»; без горизонтальной прокрутки на 390 и 1024 px;
//  - сигнал: точка в меню и строка в сводке видны, не заходя на экран; без сигнала и без строк без кода — нет;
//  - адаптер без действия (404): понятный текст «обновите адаптер».
//   node --test tests/js/index_codes.pw.test.js
// Нужен Playwright (как ocp_types.pw.test.js); без него тест пропускается. Скриншоты — при INDEX_CODES_SHOTS=<папка>.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const SHOTS = process.env.INDEX_CODES_SHOTS || null;

const OPTIONS = [[null, 'не выбрано (нога уйдёт как «другой индекс»)'], ['RUONIA', 'RUONIA'], ['KEY_RATE', 'Ключевая ставка'], ['RUSFAR', 'RUSFAR'],
  ['RUSFARCNY', 'RUSFAR CNY'], ['SOFR', 'SOFR'], ['ESTR', '€STR'], ['OTHER', 'другой индекс — сделка «не оценено»']].map(([value, title]) => ({ value, title }));
const COLUMNS = [{ id: 'source', title: 'Источник' }, { id: 'legs', title: 'Ног' }, { id: 'deals', title: 'Сделок' }, { id: 'books', title: 'Книг' },
  { id: 'ccy', title: 'Валюты' }, { id: 'kind', title: 'Вид' }, { id: 'last', title: 'Последний файл' }];
const ROWS = [
  { key: 'IDX_FAKE_CNY_ON', label: 'IDX_FAKE_CNY_ON', value: null, suggested: 'RUSFARCNY', hint: 'по названию и валюте ноги', new: true, future: 4, replaces: [],
    cells: { source: 'terms_a', legs: 4, deals: 2, books: 1, ccy: 'CNY', kind: 'ccs', last: '2026-11-04' } },
  { key: 'IDX_FAKE_A_VERY_LONG_NAME_OF_AN_OVERNIGHT_INDEX_WITH_SUFFIX', label: 'IDX_FAKE_A_VERY_LONG_NAME_OF_AN_OVERNIGHT_INDEX_WITH_SUFFIX', value: null, suggested: null, new: false, future: 2, replaces: [],
    cells: { source: 'terms_a, terms_b', legs: 2, deals: 2, books: 2, ccy: 'USD, EUR', kind: 'ccs, irs', last: '2026-11-04' } },
  { key: 'IDX_FAKE_KEY', label: 'IDX_FAKE_KEY', value: 'KEY_RATE', suggested: 'KEY_RATE', hint: 'по названию', new: false, future: 7, replaces: [],
    cells: { source: 'terms_b', legs: 9, deals: 5, books: 3, ccy: 'RUB', kind: 'irs', last: '2026-11-04' } },
  { key: 'IDX_FAKE_SHIB', label: 'IDX_FAKE_SHIB', value: 'OTHER', suggested: 'OTHER', hint: 'индекс без данных в хабе', new: false, future: 3, replaces: [],
    cells: { source: 'terms_a', legs: 3, deals: 3, books: 1, ccy: 'CNY', kind: 'irs', last: '2026-11-04' } }];
const CALM_ROWS = ROWS.map(r => ({ ...r, new: false, value: r.suggested !== null && r.suggested !== undefined ? r.suggested : 'OTHER' }));
const CHOICES = { kind: 'trade_index_codes', title: 'Индексы ставок в условиях сделок', save: { tab: 'index_codes', key: 'index_codes' }, options: OPTIONS, columns: COLUMNS,
  rows: ROWS, risky_unset: [], replay: { source: 'terms_a', from: '2026-11-04', to: '2026-11-04' }, file_sha256: 'f'.repeat(64),
  notes: ['Названия из последних файлов условий; другой источник перечитывается кнопкой «Перечитать отчёты за период».'] };

function server(opts) {
  const o = opts || {};
  const st = { saves: [], replayPolls: 0, autoPolls: 0 };
  const choices = o.calm ? { ...CHOICES, rows: CALM_ROWS } : CHOICES;
  const signalNow = () => (o.signal ? { code: 'unmapped_index', count: 5, files: 2, latest: '2026-11-04' } : null);
  st.handle = (u, method, body) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return { home: 'C:\\desk', inbox_dirs: [], effective: {}, env_overrides: [], error: null };
    if (p === '/api/v1/adapter/choices/trade_index_codes' && method === 'GET') {
      if (o.old) return { __status: 404 };
      if (o.noAdapter) return { __status: 404, body: { code: 'not_found', message: 'the adapter has no action', details: { state: { code: 'adapter_error', level: 'bad', text: 'Экран «Индексы ставок»: версия договора адаптера не подходит к этому монитору: обновите адаптер или монитор (Настройки → «Обновления»).' } } } };
      if (o.failed) return { action: { id: 'index_names', title: 'Индексы ставок в условиях сделок' }, running: false, ok: null, summary: [], stale: false, signal: null, choices: null, choices_source: null,
        job: { state: 'timeout', error_code: 'timeout', started_at: new Date(Date.now() - 60000).toISOString(), finished_at: new Date(Date.now() - 50000).toISOString() },
        state: { code: 'job_timeout', level: 'bad', text: 'Адаптер не дал индексы: адаптер не успел прочитать условия сделок за отведённое время. Нажмите «Обновить».' } };
      if (o.store) return { action: { id: 'index_names', title: 'Индексы ставок в условиях сделок' }, running: false, ok: false, summary: ['Файлов условий сделок в папках отчётов нет'], stale: false, signal: null,
        choices: { ...CHOICES, source: 'store', read_only: false, replay: null }, choices_source: 'store',
        job: { state: 'done', started_at: new Date(Date.now() - 60000).toISOString(), finished_at: new Date(Date.now() - 50000).toISOString() },
        state: { code: 'adapter_declined', level: 'warn', text: 'Адаптер ответил, что индексов нет: Файлов условий сделок в папках отчётов нет.' } };
      return { choices_source: 'adapter', state: { code: 'ok', level: 'ok', text: '' }, action: { id: 'index_names', title: 'Индексы ставок в условиях сделок' }, running: false, ok: true, summary: [], stale: false, signal: st.saved ? null : signalNow(),
        job: { state: 'done', started_at: new Date(Date.now() - 60000).toISOString(), finished_at: new Date(Date.now() - 50000).toISOString() }, choices };
    }
    if (p === '/api/v1/adapter/choices/trade_index_codes' && method === 'POST') {
      st.saves.push(body); st.saved = true; st.replay = { state: 'running', waiting: true, request: { source: 'terms_a', from: '2026-11-04', to: '2026-11-04' } };
      return { saved: { revision: { rev_id: 9 } }, changed: Object.keys(body.values).length, replay: { started: true } };
    }
    if (p === '/api/v1/adapter/actions/index_names/run') return { running: true };
    if (p === '/api/v1/inbox/replay') {
      if (st.replay && st.replay.state === 'running' && ++st.replayPolls > 1)
        st.replay = { state: 'done', request: st.replay.request, finished_at: new Date().toISOString(), result: { files: { accepted: 1 }, revisions: 1 } };
      return { sources: [], store: true, max_days: 400, job: st.replay || null, running: !!(st.replay && st.replay.state === 'running') };
    }
    if (p === '/api/v1/autopilot/status') {
      st.autoPolls++;
      return { enabled: true, state: 'idle', last_cycle_at: new Date().toISOString(), message: 'Пересчитано',
        latest_run: { created_at: new Date(Date.now() + 1000).toISOString(), label: '05.11 15:00 МСК · внутри дня' } };
    }
    return null;
  };
  return st;
}

async function page(browser, st, url, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 1 }), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      let body = null; try { body = req.postDataJSON(); } catch (e) { /* GET */ }
      const out = st.handle(u, req.method(), body);
      if (out && out.__status) return route.fulfill({ status: out.__status, contentType: 'application/json', body: JSON.stringify(out.body || { message: 'адаптер не отдаёт этот экран' }) });
      return out ? route.fulfill({ status: req.method() === 'POST' && /run$/.test(u.pathname) ? 202 : 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  return { pg, ctx, errors };
}

const noHScroll = pg => pg.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth + 1);
const shot = async (pg, name) => { if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, name), fullPage: true }); } };

test('Индексы ставок (TDM#565)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1024, 390]) {
      await t.test('экран, ' + width + ' px', async () => {
        const st = server({ signal: true }), { pg, ctx, errors } = await page(browser, st, '/settings.html#indexes', width);
        const box = pg.locator('[data-slot="index-codes"]');
        await box.locator('.ic-row', { hasText: 'IDX_FAKE_CNY_ON' }).waitFor();
        assert.ok(/видны только на этом компьютере/.test(await box.innerText()));
        // по умолчанию — строки без сохранённого кода; сохранённые — в «Все»
        assert.strictEqual(await box.locator('.ic-row', { hasText: 'IDX_FAKE_KEY' }).count(), 0);
        const cny = box.locator('.ic-row', { hasText: 'IDX_FAKE_CNY_ON' });
        assert.strictEqual(await cny.locator('select').inputValue(), 'RUSFARCNY');  // подсказка стоит сразу
        assert.ok(/Предложено по подсказке/.test(await cny.innerText()) && /новый/.test(await cny.innerText()));
        const none = box.locator('.ic-row', { hasText: 'A_VERY_LONG_NAME' });
        assert.strictEqual(await none.locator('select').inputValue(), '');
        assert.ok(/сделка будет «не оценено»/.test(await none.innerText()));
        assert.ok(/пришло: 5 сделок/.test(await box.innerText()));
        await shot(pg, 'index-codes-' + width + '.png');
        assert.ok(/Последний файл\s+04\.11\.2026/.test(await box.innerText()), 'дата дд.мм.гггг');
        assert.ok(await noHScroll(pg), 'без горизонтальной прокрутки');
        await box.locator('.ic-chip', { hasText: 'Все' }).click();
        await box.locator('.ic-row', { hasText: 'IDX_FAKE_KEY' }).waitFor();
        assert.ok(await noHScroll(pg), 'без горизонтальной прокрутки во всех');
        await shot(pg, 'index-codes-all-' + width + '.png');
        // выбор только из списка; одна кнопка: записать отличия, перечитать, пересчитать
        await none.locator('select').selectOption('SOFR');
        assert.ok(/Будет записано: 2 названия \(по подсказке — 1\)/.test(await box.innerText()));
        await box.locator('button', { hasText: 'Сохранить и пересчитать' }).click();
        await box.locator('.verdict.ok', { hasText: 'Готово: условия перечитаны, сделки пересчитаны' }).waitFor({ timeout: 15000 });
        assert.deepStrictEqual(st.saves[0], { values: { IDX_FAKE_CNY_ON: 'RUSFARCNY', IDX_FAKE_A_VERY_LONG_NAME_OF_AN_OVERNIGHT_INDEX_WITH_SUFFIX: 'SOFR' }, replay: true, base_sha256: 'f'.repeat(64) });
        assert.ok(/расчёт 05\.11 15:00 МСК/.test(await box.innerText()));
        await shot(pg, 'index-codes-done-' + width + '.png');
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      });
    }
    await t.test('сигнал виден в меню и сводке, не заходя на экран; без сигнала — нет', async () => {
      const seen = pg => pg.evaluate(() => ({ items: window.DeskSettings.statusItems().filter(x => x.k === 'Индексы ставок'),
        dot: document.querySelector('a[data-sec="indexes"] .dot').className }));
      let r = await page(browser, server({ signal: true }), '/settings.html#hub', 1024);
      await r.pg.waitForFunction(() => /warn/.test(document.querySelector('a[data-sec="indexes"] .dot').className));
      let s = await seen(r.pg);
      assert.strictEqual(s.items.length, 1);
      assert.ok(/6 ног без сопоставления — открыть/.test(s.items[0].text) && s.items[0].cls === 'warn' && s.items[0].href === '#indexes', JSON.stringify(s.items));
      assert.ok(await r.pg.locator('a[data-sec="indexes"]').isVisible());
      await r.ctx.close();
      // все названия сопоставлены, сигнала нет: ни точки, ни строки
      r = await page(browser, server({ calm: true }), '/settings.html#indexes', 1024);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.ic-chip', { hasText: 'Все' }).waitFor();
      s = await seen(r.pg);
      assert.ok(!/warn/.test(s.dot) && s.items.length === 0, JSON.stringify(s));
      assert.ok(/Нечего проверять/.test(await box.innerText()) === false);  // нет строк без кода: открыт «Все»
      assert.deepStrictEqual(r.errors, []);
      await r.ctx.close();
    });
    await t.test('сигнал есть, а все индексы уже выбраны: сделки из сигнала, подсказка про перечитывание', async () => {
      const r = await page(browser, server({ calm: true, signal: true }), '/settings.html#indexes', 390);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.note', { hasText: 'пришло: 5 сделок' }).waitFor();
      assert.ok(/Перечитать отчёты за период/.test(await box.innerText()) && !/не выбран ниже/.test(await box.innerText()));
      await r.pg.waitForFunction(() => /warn/.test(document.querySelector('a[data-sec="indexes"] .dot').className));
      const item = await r.pg.evaluate(() => window.DeskSettings.statusItems().filter(x => x.k === 'Индексы ставок')[0].text);
      assert.ok(/^5 сделок без сопоставления/.test(item), item);
      assert.deepStrictEqual(r.errors, []);
      await r.ctx.close();
    });
    await t.test('адаптер не загружен (404 с причиной): причина и что нажать, не общее «обновите»', async () => {
      const r = await page(browser, server({ noAdapter: true }), '/settings.html#indexes', 390);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.note', { hasText: 'версия договора адаптера не подходит' }).waitFor();
      assert.ok(/Настройки → «Обновления»/.test(await box.innerText()));
      assert.deepStrictEqual(r.errors, []);
      await r.ctx.close();
    });
    await t.test('действие упало: на экране причина, а не «индексов ещё нет»', async () => {
      const r = await page(browser, server({ failed: true }), '/settings.html#indexes', 390);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.note', { hasText: 'не успел прочитать условия' }).waitFor();
      assert.ok(!/Индексов ещё нет/.test(await box.innerText()) && !/Индексы не получены/.test(await box.innerText()));
      assert.ok(await box.locator('button', { hasText: 'Обновить' }).isEnabled());
      assert.deepStrictEqual(r.errors, []);
      await r.ctx.close();
    });
    await t.test('адаптер не ответил, но названия в загруженных условиях есть: таблица из монитора, выбор доступен', async () => {
      const r = await page(browser, server({ store: true }), '/settings.html#indexes', 390);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.note', { hasText: 'Адаптер ответил, что индексов нет' }).waitFor();
      await box.locator('.ic-row', { hasText: 'IDX_FAKE_CNY_ON' }).waitFor();
      assert.ok(/уже загруженных в монитор/.test(await box.innerText()));
      assert.ok(await box.locator('button', { hasText: 'Сохранить' }).isEnabled());
      assert.deepStrictEqual(r.errors, []);
      await r.ctx.close();
    });
    await t.test('адаптер без действия (404): «обновите адаптер»', async () => {
      const r = await page(browser, server({ old: true }), '/settings.html#indexes', 390);
      const box = r.pg.locator('[data-slot="index-codes"]');
      await box.locator('.note', { hasText: 'обновите адаптер' }).waitFor();
      assert.ok(/Настройки → Обновления/.test(await box.innerText()));
      assert.ok(await noHScroll(r.pg));
      await shot(r.pg, 'index-codes-404-390.png');
      await r.ctx.close();
    });
  } finally {
    await browser.close();
  }
});
