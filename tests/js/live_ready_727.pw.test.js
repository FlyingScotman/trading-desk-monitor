// #727: новый прогон не опустошает экран. В браузере, на странице монитора с данными «сервера» (синтетика): по событию
// run.published цифры остаются на экране, под шапкой — плашка «Пересчитан новый прогон, загружаю… · данные на ЧЧ:ММ МСК»,
// страница спрашивает GET /api/v1/runs/{id}/ready и перезагружается только когда данные нового прогона готовы; не
// дождалась — полоса «Доступен новый прогон … — показать» без перезагрузки.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.resolve(__dirname, '../../prototypes');
const ORIGIN = 'http://localhost:8100';  // localhost: безопасный контекст, есть Web Locks (лидер ленты, SSE)
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.jpg': 'image/jpeg' };
const sleep = ms => new Promise(r => setTimeout(r, ms));

// страница монитора на прогоне 42, опубликованном в 11:32 UTC (14:32 МСК); сервер: статус автопилота и /ready — по ready(n)
async function open(browser, ready) {
  const run = JSON.parse(RUN);
  run.source = { ...run.source, runId: 42, publishedAt: '2026-11-13T11:32:00+00:00' };
  const s = { runLoads: 0, readyCalls: [], published: false, release: null };
  const gate = new Promise(r => { s.release = r; });
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(() => localStorage.setItem('c03-state', JSON.stringify({ v: 2, grouping: 'ccy' })));
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') {
      s.runLoads++;
      return route.fulfill({ contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(run) + ';' });
    }
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/events/stream') {
      await gate;  // событие приходит, когда тест его выпустит
      const first = !s.published;
      s.published = true;
      const ev = 'id: 6\nevent: run.published\ndata: ' + JSON.stringify({ id: 6, type: 'run.published', run_id: 43, valid_date: '2026-10-13', text: 'x' }) + '\n\n';
      return route.fulfill({ contentType: 'text/event-stream', body: 'retry: 600000\nevent: hello\ndata: {"after":5}\n\n' + (first ? ev : '') });
    }
    if (u.pathname === '/api/v1/events') return route.fulfill({ json: { events: [], has_more: false, last_id: 5 } });
    if (u.pathname === '/api/v1/autopilot/status') {
      return route.fulfill({ json: { latest_run: s.published ? { run_id: 43, label: '13.11 · внутри дня' } : { run_id: 42 }, now: '2026-11-13T11:40:00+00:00' } });
    }
    if (u.pathname === '/api/v1/runs/43/ready') {
      s.readyCalls.push(u.search);
      return route.fulfill({ json: ready(s.readyCalls.length) });
    }
    if (u.pathname.startsWith('/api/')) return route.fulfill({ json: { enabled: false, user: null, runs: [] } });
    const f = path.resolve(ROOT, '.' + u.pathname);
    if (!f.startsWith(ROOT + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  try { await pg.waitForSelector('#tbl tr.grp.l0', { timeout: 8000 }); } catch (e) {
    const info = await pg.evaluate(() => document.body.innerText.slice(0, 400) + ' | src=' + (window.KIT && KIT.source) + ' load=' + JSON.stringify(window.CONCEPTS_LOAD_ERROR));
    throw new Error(e.message + ' errors=' + JSON.stringify(errors) + ' page=' + info);
  }
  await pg.evaluate(() => { DESK_LIVE.READY_POLL_MS = 100; DESK_LIVE.READY_MS = 1500; });
  return { ctx, pg, errors, s };
}

test('#727: новый прогон — плашка с временем, цифры на месте, перезагрузка только по готовым данным', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors, s } = await open(browser, n => ({ run_id: 43, ready: n >= 4, missing: n >= 4 ? [] : ['extras'], warming: true }));
    try {
      const rows = await pg.locator('#tbl tr.grp.l0').count();
      assert.ok(rows > 0);
      assert.equal(s.runLoads, 1);
      s.release();
      const bar = pg.locator('.k-live');
      await bar.waitFor({ timeout: 5000 });
      assert.match(await bar.innerText(), /Пересчитан новый прогон, загружаю… · данные на 14:32 МСК/);
      assert.equal(await bar.locator('button').count(), 0, 'плашка без кнопок: не мешает работать');
      assert.equal(await pg.locator('#tbl tr.grp.l0').count(), rows, 'цифры остались на экране');
      assert.equal(s.runLoads, 1, 'пока данные не готовы — страница не перезагружается');
      await pg.waitForFunction(() => performance.getEntriesByType('navigation').length > 0 && !document.querySelector('.k-live'), null, { timeout: 5000 });
      await pg.waitForSelector('#tbl tr.grp.l0');
      assert.equal(s.runLoads, 2, 'готово — одна перезагрузка');
      assert.ok(s.readyCalls.length >= 4 && s.readyCalls.every(q => q === '?pages=concepts'), s.readyCalls.join());
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('#727: данные не дождались — полоса «Доступен новый прогон … — показать», страница не перезагружается', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors, s } = await open(browser, () => ({ run_id: 43, ready: false, missing: ['extras'], warming: false }));
    try {
      const rows = await pg.locator('#tbl tr.grp.l0').count();
      s.release();
      const bar = pg.locator('.k-live');
      await bar.waitFor({ timeout: 5000 });
      await bar.locator('button[data-a=show]').waitFor({ timeout: 5000 });  // таймаут ожидания 1,5 с
      assert.match(await bar.innerText(), /Доступен новый прогон: 13\.11 · внутри дня · #43/);
      assert.equal(await bar.locator('button[data-a=show]').innerText(), 'Показать');
      await sleep(300);
      assert.equal(s.runLoads, 1, 'страница не перезагружалась');
      assert.equal(await pg.locator('#tbl tr.grp.l0').count(), rows);
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
