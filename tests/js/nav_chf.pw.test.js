// Synthetic browser regressions for navigation and currency labels (#375, #359, #101).
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.resolve(__dirname, '../../prototypes');
const ORIGIN = 'http://desk.test';
const SHOTS = process.env.NAV_CHF_SHOTS;
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.jpg': 'image/jpeg' };

async function open(browser, file, { theme = 'dark', real = false, unknown = false, app = false, record, now, extraMonitor = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  await ctx.addInitScript(theme => {
    localStorage.setItem('concepts-theme', theme);
    localStorage.setItem('c03-state', JSON.stringify({ v: 2, grouping: 'ccy' }));
  }, theme);
  if (now) await ctx.addInitScript(({ record, now }) => {
    Date.now = () => +(sessionStorage.getItem('nav-now') || now);
    if (!sessionStorage.getItem('nav-seeded')) {
      sessionStorage.setItem('nav-seeded', '1');
      if (record) localStorage.setItem('desk.run.v1', JSON.stringify(record));
    }
  }, { record, now });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (app && u.pathname === '/app/') return route.fulfill({ contentType: 'text/html', body: '<meta charset="utf-8"><script src="../desk-nav.js"></script>' });
    if (u.pathname === '/concepts-run.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ json: { enabled: false, user: null, runs: now ? [{ run_id: 42, as_of: '2026-10-21', is_current: true }] : [] } });
    const f = path.resolve(ROOT, '.' + u.pathname);
    if (!f.startsWith(ROOT + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: '' });
    let body = fs.readFileSync(f);
    if (extraMonitor && u.pathname.endsWith('/catalog.js')) body = body.toString() + '\nwindow.DESK_MONITORS.push(["c99-test", "C99", "Test", "Synthetic catalog entry"]);';
    if (unknown && u.pathname.endsWith('/mock.js')) body = body.toString() + '\nMOCK.positions.filter(p => p.ccy === "CHF").forEach(p => p.ccy = "XYZ");';
    return route.fulfill({ contentType: TYPES[path.extname(f)] || 'application/octet-stream', body });
  });
  await pg.goto(ORIGIN + '/' + file + (real || app ? '' : '?demo=1'));
  return { ctx, pg, errors };
}

async function shot(pg, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await pg.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: true });
}

test('#375: tooltip counts the gallery on root pages, /app/ and file://', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const gallery = await open(browser, 'concepts/index.html');
    const count = await gallery.pg.locator('#grid article').count();
    assert.ok(count > 13);
    await gallery.ctx.close();
    for (const file of ['index.html', 'settings.html', 'app/']) {
      const { ctx, pg, errors } = await open(browser, file, { app: file === 'app/' });
      try {
        const link = pg.locator('#desk-nav a').filter({ hasText: 'Мониторы' });
        await pg.waitForFunction(() => /^\d+ мониторов/.test(document.querySelector('#desk-nav a[href*="concepts/index.html"]').title))
          .catch(e => { throw new Error(file + ': ' + e.message + '; errors=' + JSON.stringify(errors)); });
        assert.equal(parseInt(await link.getAttribute('title')), count, file);
        if (file === 'index.html') assert.equal(await pg.locator('#monitor-count').innerText(), String(count));
        assert.equal(new URL(await link.getAttribute('href')).pathname, '/concepts/index.html');
        await shot(pg, 'nav-count-' + file.replace(/\W/g, '-'));
        assert.deepEqual(errors, []);
      } finally { await ctx.close(); }
    }
    const ctx = await browser.newContext(), pg = await ctx.newPage();
    try {
      await pg.goto(pathToFileURL(path.join(ROOT, 'index.html')).href);
      await pg.waitForFunction(() => /^\d+ мониторов/.test(document.querySelector('#desk-nav a[href*="concepts/index.html"]').title));
      assert.equal(parseInt(await pg.locator('#desk-nav a').filter({ hasText: 'Мониторы' }).getAttribute('title')), count);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('#375: extending the single catalog updates both gallery and navigation', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, 'concepts/index.html', { extraMonitor: true });
    try {
      const count = await pg.locator('#grid article').count();
      assert.equal(await pg.locator('a[href="c99-test.html"]').count(), 2);
      await pg.goto(ORIGIN + '/index.html');
      await pg.waitForFunction(() => document.getElementById('monitor-count').textContent !== '');
      assert.equal(await pg.locator('#monitor-count').textContent(), String(count));
      assert.equal(parseInt(await pg.locator('#desk-nav a').filter({ hasText: 'Мониторы' }).getAttribute('title')), count);
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('#30: click/auxclick after expiry, exact URL and explicit menu selection', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  const now = Date.parse('2026-10-27T12:00:00Z'), hour = 3600e3;
  const record = { sel: { run_id: '42' }, at: now - 11 * hour };
  try {
    const { ctx, pg, errors } = await open(browser, 'index.html?run_id=42', { real: true, now, record });
    try {
      const settings = pg.locator('#desk-nav a').filter({ hasText: 'Настройки' });
      assert.equal(new URL(await settings.getAttribute('href')).search, '?run_id=42');
      await pg.evaluate(now => sessionStorage.setItem('nav-now', now), now + 2 * hour);
      await settings.evaluate(a => {
        a.addEventListener('auxclick', e => e.preventDefault(), { once: true });
        a.dispatchEvent(new MouseEvent('auxclick', { bubbles: true, cancelable: true, button: 1 }));
      });
      assert.equal(new URL(await settings.getAttribute('href')).search, '');
      await settings.click();
      await pg.waitForURL(ORIGIN + '/settings.html');
      assert.equal(await pg.evaluate(() => localStorage.getItem('desk.run.v1')), JSON.stringify(record));
      await pg.goto(ORIGIN + '/index.html?run_id=42');
      assert.equal(new URL(pg.url()).search, '?run_id=42');
      assert.equal(await pg.evaluate(() => Date.now()), now + 2 * hour);
      await pg.waitForSelector('#desk-nav select');
      await Promise.all([pg.waitForEvent('domcontentloaded'), pg.locator('#desk-nav select').selectOption('42')]);
      const stored = await pg.evaluate(() => JSON.parse(localStorage.getItem('desk.run.v1')));
      assert.deepEqual(stored, { sel: { run_id: '42' }, at: now + 2 * hour });
      await shot(pg, 'nav-explicit-selection');
      assert.deepEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

for (const theme of ['dark', 'light']) {
  test('#359: CHF uses the shared name; unknown currency appears once, ' + theme, { skip: !pw && 'Playwright unavailable' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      for (const mode of [{}, { unknown: true }, { real: true }]) {
        const { ctx, pg, errors } = await open(browser, 'concepts/c03-blotter.html', { theme, ...mode });
        try {
          await pg.waitForSelector('#tbl tr.grp.l0');
          assert.equal(await pg.evaluate(() => KIT.theme()), theme);
          const code = mode.unknown ? 'XYZ' : 'CHF';
          const group = pg.locator('#tbl tr.grp.l0').filter({ hasText: code });
          assert.equal(await group.count(), 1);
          const label = await group.innerText();
          assert.ok(!label.includes('undefined'), label);
          assert.ok(!(await pg.locator('#tbl tr.grp').allTextContents()).some(s => s.includes('undefined')));
          assert.equal(label.match(new RegExp(code, 'g')).length, 1, label);
          if (!mode.unknown) assert.ok(label.includes('Франки'), label);
          const csv = await pg.evaluate(() => document.querySelector('#tbl table').__kt.csv());
          assert.ok(!csv.includes('undefined'));
          await group.scrollIntoViewIfNeeded();
          await shot(pg, 'c03-' + code + '-' + theme + (mode.real ? '-fixture' : ''));
          assert.deepEqual(errors, []);
        } finally { await ctx.close(); }
      }
    } finally { await browser.close(); }
  });
}

test('#101: CHF ladder comes from demo and server-shaped synthetic positions and curves', { skip: !pw && 'Playwright unavailable' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const real of [false, true]) {
      const { ctx, pg, errors } = await open(browser, 'concepts/c02-risk-ladder.html', { real });
      try {
        await pg.waitForSelector('#secs .sec');
        const currencies = await pg.evaluate(() => MOCK.ccys.filter(c => KIT.list().some(p => p.ccy === c || MOCK.curves.some(x => x.ccy === c && x.kind === 'rate' && p.dv01ByCurve[x.id]))));
        assert.ok(currencies.includes('CHF'));
        const headings = await pg.locator('#secs h3').allTextContents();
        assert.equal(headings.length, currencies.length);
        for (const ccy of currencies) assert.ok(headings.some(h => h.includes(ccy)), ccy);
        const section = pg.locator('#secs .sec').filter({ has: pg.locator('h3', { hasText: 'CHF' }) });
        assert.ok((await section.locator('h3').textContent()).includes('Франки'), JSON.stringify(headings));
        const data = await section.locator('[_echarts_instance_]').evaluate(el => echarts.getInstanceByDom(el).getOption().series.flatMap(s => s.data));
        assert.ok(data.some(v => typeof v === 'number' && v !== 0), 'CHF risk is actually rendered');
        await shot(pg, 'c02-CHF-' + (real ? 'fixture' : 'demo'));
        assert.deepEqual(errors, []);
      } finally { await ctx.close(); }
    }
  } finally { await browser.close(); }
});
