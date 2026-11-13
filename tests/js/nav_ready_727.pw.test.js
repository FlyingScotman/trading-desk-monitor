// #727: старые страницы (prototypes/desk-nav.js): скрытая вкладка перезагружается на новый прогон не сразу, а когда сервер
// построил данные прогона (GET /api/v1/runs/{id}/ready?pages=desk); не дождалась за минуту — не перезагружается; видимая
// вкладка не перезагружается никогда (кнопка «Новый прогон … — показать»).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.resolve(__dirname, '../../prototypes');
const ORIGIN = 'http://localhost:8100';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.jpg': 'image/jpeg' };

// страница /app/ (только строка навигации); статус автопилота: прогон 10, со второго опроса — 11; /ready — по ready(n)
async function open(browser, { hidden, ready }) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const s = { loads: 0, status: 0, readyCalls: [] };
  const pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.clock.install();
  if (hidden) await pg.addInitScript(() => { Object.defineProperty(document, 'hidden', { get: () => true }); });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/app/') {
      s.loads++;
      return route.fulfill({ contentType: 'text/html', body: '<meta charset="utf-8"><script src="../desk-nav.js"></script>' });
    }
    if (u.pathname === '/api/v1/autopilot/status') {
      const id = s.status++ ? 11 : 10;
      return route.fulfill({ json: { enabled: true, state: 'running', latest_run: { run_id: id, label: 'прогон ' + id } } });
    }
    if (u.pathname === '/api/v1/runs/11/ready') {
      s.readyCalls.push(u.search);
      return route.fulfill({ json: ready(s.readyCalls.length) });
    }
    if (u.pathname.startsWith('/api/')) return route.fulfill({ json: { enabled: false, user: null, runs: [] } });
    const f = path.resolve(ROOT, '.' + u.pathname);
    if (!f.startsWith(ROOT + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/app/');
  await pg.waitForSelector('#desk-nav .dn-ap:not([hidden])');
  // время идёт по часам теста: опрос автопилота (5 с при идущем цикле) и опрос готовности (1,5 с)
  const run = async ms => { for (let t = 0; t < ms; t += 500) { await pg.clock.runFor(500); await pg.waitForTimeout(15); } };
  return { ctx, pg, s, errors, run };
}

test('#727: скрытая вкладка перезагружается на новый прогон, когда данные готовы', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, s, errors, run } = await open(browser, { hidden: true, ready: n => ({ run_id: 11, ready: n >= 3, missing: [] }) });
    try {
      assert.equal(s.loads, 1);
      await run(5500);  // второй опрос статуса: прогон 11
      assert.ok(s.readyCalls.length >= 1, 'спросила, готовы ли данные');
      assert.ok(s.readyCalls.every(q => q === '?pages=desk'), s.readyCalls.join());
      assert.equal(s.loads, 1, 'пока данные не готовы — не перезагружается');
      await run(5000);
      assert.equal(s.loads, 2, 'готовы — одна перезагрузка');
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('#727: не дождалась данных за минуту — скрытая вкладка не перезагружается; видимая не перезагружается никогда', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const a = await open(browser, { hidden: true, ready: () => ({ run_id: 11, ready: false, missing: ['desk'] }) });
    try {
      await a.run(5500 + 62000);
      assert.ok(a.s.readyCalls.length >= 10 && a.s.readyCalls.length < 60, 'опрашивала раз в 1,5 с, не дольше минуты: ' + a.s.readyCalls.length);
      assert.equal(a.s.loads, 1, 'таймаут — без перезагрузки');
      const n = a.s.readyCalls.length;
      await a.run(10000);
      assert.equal(a.s.readyCalls.length, n, 'после таймаута этот прогон больше не ждёт');
      assert.equal(a.s.loads, 1);
    } finally { await a.ctx.close(); }
    const v = await open(browser, { hidden: false, ready: () => ({ run_id: 11, ready: true, missing: [] }) });
    try {
      await v.run(12000);
      assert.equal(v.s.loads, 1, 'видимая вкладка не перезагружается');
      assert.equal(v.s.readyCalls.length, 0, 'и готовность не спрашивает');
      assert.match(await v.pg.locator('#desk-nav .dn-new').innerText(), /Новый прогон прогон 11 — показать/);
    } finally { await v.ctx.close(); }
  } finally { await browser.close(); }
});
