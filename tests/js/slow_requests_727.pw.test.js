// #727: Настройки → «Диагностика» → карточка «Медленные запросы»: список с сервера (/api/v1/diagnostics/slow-requests), без
// значений параметров; пустой список — «С запуска сервера медленных запросов не было.».
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.resolve(__dirname, '../../prototypes');
const ORIGIN = 'http://localhost:8100';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.jpg': 'image/jpeg' };

async function open(browser, slow) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/api/v1/diagnostics/slow-requests') return route.fulfill({ json: slow });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ json: { enabled: false, user: null, runs: [] } });
    const f = path.resolve(ROOT, '.' + u.pathname);
    if (!f.startsWith(ROOT + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#diag');
  return { ctx, pg, errors };
}

test('#727: «Медленные запросы» — строки списка: запрос, параметры (имена), ответ, время, откуда ответ, размер', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, {
      threshold_s: 2, total: 120, keep: 100,
      requests: [
        { at: '2026-11-13T11:40:12+00:00', method: 'GET', route: '/concepts-data.js', query: ['run_id'], status: 200, ms: 12300, cache: 'miss', bytes: 4893696 },
        { at: '2026-11-13T11:39:01+00:00', method: 'GET', route: '/api/v1/runs/{run_id}/positions', query: [], status: 200, ms: 2100, cache: 'hit', bytes: null },
        { at: '2026-11-13T11:38:00+00:00', method: 'GET', route: '/settings.html', query: [], status: 200, ms: 2500, cache: null, bytes: 1024 }
      ]
    });
    try {
      const card = pg.locator('#sr');
      await pg.waitForSelector('#sr-rows tr');
      assert.equal(await card.locator('h2').textContent(), 'Медленные запросы');
      const rows = await pg.locator('#sr-rows tr').allInnerTexts();
      assert.equal(rows.length, 3);
      assert.match(rows[0], /GET \/concepts-data\.js\s+run_id\s+200\s+12300\s+собран заново\s+4779/);
      assert.match(rows[1], /GET \/api\/v1\/runs\/\{run_id\}\/positions\s+—\s+200\s+2100\s+из памяти\s+—/);
      assert.match(rows[2], /\/settings\.html\s+—\s+200\s+2500\s+—\s+1/);
      assert.match(await pg.locator('#sr-msg').innerText(), /Всего было 120, показаны последние 3\./);
      assert.equal(await pg.locator('#sr-thr').innerText(), '2');
      assert.ok(!(await card.innerText()).includes('undefined'));
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
    const empty = await open(browser, { threshold_s: 2, total: 0, keep: 100, requests: [] });
    try {
      await empty.pg.waitForFunction(() => /медленных запросов не было/.test(document.getElementById('sr-msg').textContent));
      assert.equal(await empty.pg.locator('#sr-tw').isHidden(), true);
      assert.deepEqual(empty.errors, []);
    } finally { await empty.ctx.close(); }
  } finally { await browser.close(); }
});
