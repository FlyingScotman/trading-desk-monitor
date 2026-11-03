// «ОВП: типы строк отчёта» и «Перечитать отчёты» в браузере (Playwright), сервер — заглушка маршрутов (TDM#554, TDM#553):
//  - экран настроек: типы из последних отчётов, у каждого выпадающий список вида, подсказка стоит сразу и помечена,
//    «риск двойного счёта» и «новый» на месте, подтипы по раскрытию; «Сохранить и пересчитать» пишет только отличия,
//    затем перечитывание и пересчёт до «Готово, ОВП пересчитана»; без горизонтальной прокрутки на 390 и 1024 px;
//  - перечитывание: источник из списка, период по умолчанию — прошлый рабочий день, оценка «N файлов», ожидание скана,
//    итог; пользователю без прав блок не показывается.
//   node --test tests/js/ocp_types.pw.test.js
// Нужен Playwright (как settings_adapter.pw.test.js); без него тест пропускается. Скриншоты — при OCP_TYPES_SHOTS=<папка>.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const SHOTS = process.env.OCP_TYPES_SHOTS || null;

const OPTIONS = [[null, 'не дериватив (оставить в отчёте)'], ['fx_forward', 'валютный форвард'], ['fx_swap', 'FX-своп'], ['fx_future', 'валютный фьючерс'],
  ['ndf', 'NDF'], ['fx_option', 'валютный опцион'], ['swap', 'процентный / валютно-процентный своп'], ['other', 'прочий дериватив']].map(([value, title]) => ({ value, title }));
const ROWS = [
  { key: 'FAKE CASH SETTLEMENTS', label: 'FAKE CASH SETTLEMENTS', value: null, suggested: null, hint: 'по названию типа: похоже на облигации, спот или кэш', new: false, future: 0,
    cells: { lines: 420, future_share: 0, subtypes: 3 }, children: [{ label: 'SUB A', cells: { lines: 400, future_share: 0 } }, { label: 'SUB B', cells: { lines: 20, future_share: 0 } }] },
  { key: 'FAKE FX SWAP', label: 'FAKE FX SWAP', value: 'fx_swap', suggested: 'fx_swap', hint: 'по названию типа', new: false, future: 96,
    cells: { lines: 140, future_share: 0.6857, subtypes: 2 }, children: [{ label: 'FAKE NEAR', cells: { lines: 70, future_share: 0.4 } }, { label: 'FAKE FAR LEG WITH A LONG NAME', cells: { lines: 70, future_share: 0.97 } }] },
  { key: 'FAKE FUT', label: 'FAKE FUT', value: null, suggested: 'fx_future', hint: 'по названию типа', new: false, future: 12, cells: { lines: 30, future_share: 0.4, subtypes: 1 }, children: [] },
  { key: 'FAKE IRS FLOWS', label: 'FAKE IRS FLOWS', value: null, suggested: 'swap', hint: 'по названию типа', new: true, future: 8, cells: { lines: 8, future_share: 1, subtypes: 1 }, children: [] },
  { key: 'FAKE OTHER FLOW', label: 'FAKE OTHER FLOW', value: null, suggested: null, hint: null, new: false, future: 5, cells: { lines: 9, future_share: 0.5556, subtypes: 1 }, children: [] }];
const CHOICES = { kind: 'ocp_derivative_types', save: { tab: 'ocp_policy', key: 'derivative_types' }, options: OPTIONS, columns: [], rows: ROWS,
  risky_unset: ['fx_future', 'ndf', 'fx_option'], replay: { source: 'ocp_cash', from: '2026-11-02', to: '2026-11-02' }, file_sha256: 'f'.repeat(64),
  notes: ['Тип, оставленный «не дериватив», остаётся в ОВП отчёта по номиналу.'] };
const SOURCES = [{ source: 'ocp_cash', datasets: ['ocp', 'cash_balances'], names: ['ОВП', 'Остатки на счетах'], latest: '2026-11-02', files: 40 },
  { source: 'positions_daily', datasets: ['positions'], names: ['Позиции'], latest: '2026-11-03', files: 60 }];

function server(opts) {
  const st = { saves: [], replayRuns: [], replayPolls: 0, autoPolls: 0, deny: !!(opts && opts.deny) };
  st.handle = (u, method, body) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return { home: 'C:\\desk', inbox_dirs: [], effective: {}, env_overrides: [], error: null };
    if (p === '/api/v1/adapter/choices/ocp_derivative_types' && method === 'GET')
      return { action: { id: 'row_types', title: 'Типы строк отчёта ОВП' }, running: false, ok: true, summary: [], stale: false,
        job: { state: 'done', started_at: new Date(Date.now() - 60000).toISOString(), finished_at: new Date(Date.now() - 50000).toISOString() }, choices: CHOICES };
    if (p === '/api/v1/adapter/choices/ocp_derivative_types' && method === 'POST') {
      st.saves.push(body); st.replay = { state: 'running', waiting: true, request: { source: 'ocp_cash', from: '2026-11-02', to: '2026-11-02' } };
      return { saved: { revision: { rev_id: 7 } }, changed: Object.keys(body.values).length, replay: { started: true } };
    }
    if (p === '/api/v1/adapter/actions/row_types/run') return { running: true };
    if (p === '/api/v1/inbox/replay' && method === 'POST') {
      st.replayRuns.push(body); st.replay = { state: 'running', request: { ...body } };
      return { sources: SOURCES, default: { from: '2026-11-02', to: '2026-11-02' }, job: st.replay, running: true, max_days: 400 };
    }
    if (p === '/api/v1/inbox/replay') {
      if (st.deny) return { __status: 403 };
      if (st.replay && st.replay.state === 'running' && ++st.replayPolls > 1)
        st.replay = { state: 'done', request: st.replay.request, finished_at: new Date().toISOString(), result: { files: { accepted: 1 }, revisions: 1 } };
      const src = u.searchParams.get('source');
      return { sources: SOURCES, default: { from: '2026-11-02', to: '2026-11-02' }, store: true, max_days: 400,
        estimate: src ? { files: src === 'ocp_cash' ? 1 : 2, dates: ['2026-11-02'] } : null, job: st.replay || null, running: !!(st.replay && st.replay.state === 'running') };
    }
    if (p === '/api/v1/autopilot/status') {
      st.autoPolls++;
      return { enabled: true, state: 'idle', last_cycle_at: new Date().toISOString(), message: 'Пересчитано',
        latest_run: { created_at: new Date(Date.now() + 1000).toISOString(), label: '03.11 15:00 МСК · внутри дня' } };
    }
    return null;
  };
  return st;
}

async function page(browser, st, url, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 }), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/replay-harness.html')
      return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">' +
        '<link rel="stylesheet" href="/concepts/lib/kit.css"></head><body><div class="k-page"><div class="k-panel" id="box" hidden><div class="hd"><h3>Перечитать отчёты</h3></div>' +
        '<div class="bd" data-desk-replay></div></div></div><script src="/desk-replay.js"></script><script>DeskReplay.mount(document.querySelector("[data-desk-replay]"), {base: "/", box: document.getElementById("box")});</script></body></html>' });
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      let body = null; try { body = req.postDataJSON(); } catch (e) { /* GET */ }
      const out = st.handle(u, req.method(), body);
      if (out && out.__status) return route.fulfill({ status: out.__status, contentType: 'application/json', body: '{"message":"только администратору"}' });
      return out ? route.fulfill({ status: req.method() === 'POST' && /run$|replay$/.test(u.pathname) ? 202 : 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  return { pg, ctx, errors };
}

async function noHScroll(pg) {
  return pg.evaluate(() => document.scrollingElement.scrollWidth <= window.innerWidth + 1);
}

test('ОВП: типы строк отчёта и перечитывание (TDM#554, TDM#553)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1024, 390]) {
      await t.test('экран типов строк, ' + width + ' px', async () => {
        const st = server(), { pg, ctx, errors } = await page(browser, st, '/settings.html#ocptypes', width);
        const box = pg.locator('[data-ocp-types]');
        await box.locator('.ot-row', { hasText: 'FAKE FX SWAP' }).waitFor();
        // по умолчанию — типы с будущими строками; кэш без будущих строк — в «Все»
        assert.strictEqual(await box.locator('.ot-row', { hasText: 'FAKE CASH SETTLEMENTS' }).count(), 0);
        const fut = box.locator('.ot-row', { hasText: 'FAKE FUT' });
        assert.strictEqual(await fut.locator('select').inputValue(), 'fx_future');  // подсказка стоит сразу
        assert.ok(/Предложено по подсказке/.test(await fut.innerText()));
        // оставить фьючерс «не дериватив» — риск двойного счёта, сказан рядом
        await fut.locator('select').selectOption('');
        assert.ok(/Риск двойного счёта/.test(await box.locator('.ot-row', { hasText: 'FAKE FUT' }).innerText()));
        await box.locator('.ot-row', { hasText: 'FAKE FUT' }).locator('select').selectOption('fx_future');
        assert.ok(/новый/.test(await box.locator('.ot-row', { hasText: 'FAKE IRS FLOWS' }).innerText()));
        // подтипы — по раскрытию
        const swap = box.locator('.ot-row', { hasText: 'FAKE FX SWAP' });
        assert.strictEqual(await swap.locator('.ot-sub').count(), 0);
        await swap.locator('.ot-tg').click();
        assert.ok(/FAKE FAR LEG WITH A LONG NAME/.test(await box.locator('.ot-row', { hasText: 'FAKE FX SWAP' }).innerText()));
        await box.locator('.ot-chip', { hasText: 'Все' }).click();
        await box.locator('.ot-row', { hasText: 'FAKE CASH SETTLEMENTS' }).waitFor();
        assert.ok(await noHScroll(pg), 'без горизонтальной прокрутки');
        if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'ocp-types-' + width + '.png'), fullPage: true }); }
        // одна кнопка: записать отличия, перечитать, пересчитать
        assert.ok(/Будет записано: 2 типа \(по подсказке — 2\)/.test(await box.innerText()));
        await box.locator('button', { hasText: 'Сохранить и пересчитать' }).click();
        await box.locator('.verdict.ok', { hasText: 'Готово, ОВП пересчитана' }).waitFor({ timeout: 15000 });
        assert.deepStrictEqual(st.saves[0], { values: { 'FAKE FUT': 'fx_future', 'FAKE IRS FLOWS': 'swap' }, replay: true, base_sha256: 'f'.repeat(64) });
        assert.ok(/расчёт 03\.11 15:00 МСК · внутри дня/.test(await box.innerText()));
        if (SHOTS) await pg.screenshot({ path: path.join(SHOTS, 'ocp-types-' + width + '-done.png'), fullPage: true });
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      });
      await t.test('перечитывание, ' + width + ' px', async () => {
        const st = server(), { pg, ctx, errors } = await page(browser, st, '/replay-harness.html', width);
        const box = pg.locator('#box');
        await box.locator('.rp-est', { hasText: 'Будет перечитано: 1 файл (за 02.11.2026)' }).waitFor();
        assert.ok(await box.isVisible());
        assert.strictEqual(await box.locator('input[type=date]').first().inputValue(), '2026-11-02');
        await box.locator('select').selectOption('positions_daily');
        await box.locator('.rp-est', { hasText: '2 файла' }).waitFor();
        await box.locator('select').selectOption('ocp_cash');
        assert.ok(await noHScroll(pg), 'без горизонтальной прокрутки');
        if (SHOTS) await pg.screenshot({ path: path.join(SHOTS, 'replay-' + width + '.png'), fullPage: true });
        await box.locator('button', { hasText: 'Перечитать' }).click();
        await box.locator('.rp-res.ok', { hasText: 'новых ревизий — 1' }).waitFor({ timeout: 15000 });
        assert.deepStrictEqual(st.replayRuns[0], { source: 'ocp_cash', from: '2026-11-02', to: '2026-11-02' });
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      });
    }
    await t.test('перечитывание не показывается пользователю без прав', async () => {
      const st = server({ deny: true }), { pg, ctx } = await page(browser, st, '/replay-harness.html', 1024);
      await pg.waitForTimeout(500);
      assert.strictEqual(await pg.locator('#box').isVisible(), false);
      await ctx.close();
    });
  } finally {
    await browser.close();
  }
});
