// C19 «PnL за период» in the browser (Playwright; skipped without it).
//  1. Without a server: the pages from the files, a run from tests/js/fixtures/concepts_run.js, /api/v1/history/pnl/*
//     answered from the synthetic tests/js/fixtures/pnl_period.json — screens A, B, C, the request parameters, the
//     latest request winning (no stale result while loading), «—» for missing data, 390 px, both themes, no errors.
//  2. With PNL_HISTORY_BASE=http://127.0.0.1:<port> (desk serve on a synthetic store, e.g. test_pnl_history.seed_history):
//     the real /period answer renders on A, B, C without console errors.
//   node --test tests/js/pnl_history.pw.test.js        PNL_HISTORY_SHOTS=<dir> also saves screenshots
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const FX = require('./fixtures/pnl_period.json');
const BASE = process.env.PNL_HISTORY_BASE, SHOTS = process.env.PNL_HISTORY_SHOTS;
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bnull\b|\bundefined\b/;

async function open(browser, { width = 1440, theme = 'dark', delay } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(t => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); } catch (e) { } }, theme);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) }).catch(() => { });  // an aborted (superseded) request
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname.startsWith('/api/v1/history/pnl/')) calls.push(u);
    if (u.pathname === '/api/v1/history/pnl/period') {
      const d = delay ? await delay(u) : null;
      return json(d || FX.period);
    }
    if (u.pathname === '/api/v1/history/pnl/position') return json(FX.position[u.searchParams.get('position_book') + '|' + u.searchParams.get('position_id')]);
    if (u.pathname === '/api/v1/history/pnl/curves') return json(FX.curves);
    if (u.pathname === '/api/v1/history/pnl/checks') return json(FX.checks);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c19-pnl-history.html');
  await pg.waitForSelector('.k-kpis');
  return { pg, ctx, errors, calls };
}
const mn = (pg, v) => pg.evaluate(x => KIT.f.mn(x, 1), v);
// the screen is drawn in the same task as its tab is marked (hashchange → renderScreen)
const tab = async (pg, key) => { await pg.locator('#tabs [data-tab=' + key + ']').click(); await pg.waitForSelector('#tabs [data-tab=' + key + '].on'); };
const wfNames = pg => pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('wf')).getOption().xAxis[0].data);

test('C19 «PnL за период» on the synthetic fixture: A, B, C, requests, stale guard, missing data, widths', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls } = await open(browser);
    const S = FX.period.summary;
    // the request: MTD of the run's date, auto granularity, the run, the catalogue for the filter menus
    const q = calls.find(u => u.pathname.endsWith('/period')).searchParams;
    assert.equal(q.get('from'), '2026-10-01'); assert.equal(q.get('to'), '2026-10-13'); assert.equal(q.get('gran'), 'day');
    assert.ok(q.get('run_id')); assert.equal(q.get('catalogue'), 'true');
    // A: tiles, waterfall, contributors, the table by periods
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(await mn(pg, S.total)));
    assert.deepEqual((await wfNames(pg)).slice(0, 2), ['Кэрри нетто', 'Ставки']);
    assert.equal((await wfNames(pg)).at(-1), 'Итог');
    // FX only net (owner, 28.10.2026): no asset / liability FX as separate sources anywhere on the page
    const NO_SPLIT_FX = /FX актива|FX фондирования|актив [(\d]/;
    assert.ok((await wfNames(pg)).includes('FX (нетто ОВП)'));
    assert.doesNotMatch(await pg.locator('#main').innerText(), NO_SPLIT_FX);
    assert.ok((await pg.locator('#bt tbody tr').count()) >= 7);
    assert.equal(await pg.locator('#bt thead th').count(), FX.period.buckets.length + 2);
    await pg.locator('#netA').uncheck();
    assert.deepEqual((await wfNames(pg)).slice(0, 3), ['Кэрри брутто', 'Фондирование', 'Ставки']);
    await pg.locator('#lensSeg button', { hasText: 'Explain по кривым' }).click();
    assert.ok((await wfNames(pg)).includes('Ставки · кривая не указана'));
    // attribution without the coupon split of every row — «—», not 0
    await pg.locator('#btSeg button', { hasText: 'Атрибуция' }).click();
    const attrCells = await pg.locator('#bt tbody td.n').allInnerTexts();
    assert.ok(attrCells.length && attrCells.every(t => t.trim() === '—'), attrCells.join('|'));
    await pg.locator('#lensSeg button', { hasText: 'Книги' }).click();
    assert.deepEqual((await wfNames(pg)).slice(0, -1).sort(), ['DEMO-A', 'DEMO-B']);
    assert.match(await pg.locator('#contribTitle').textContent(), /Итог/);
    assert.ok(!BAD.test(await pg.locator('#main').innerText()));
    // a contributor opens E with the position (placeholder screen until TDM#409)
    await pg.locator('#contrib [data-ref]').first().click();
    await pg.waitForSelector('#tabs [data-tab=position].on');
    assert.equal(await pg.evaluate(() => location.hash), '#position');
    assert.match(await pg.locator('#main').innerText(), /экран в работе/);
    // B: a position cell asks /position for that position and shows its largest days
    await tab(pg, 'matrix');
    await pg.locator('#mt tbody tr:not(.grp) td.cell').first().click();
    await pg.waitForSelector('#cellDays table');
    const pq = calls.filter(u => u.pathname.endsWith('/position')).at(-1).searchParams;
    assert.ok(pq.get('position_book') && pq.get('position_id')); assert.equal(pq.get('from'), '2026-10-01');
    await pg.locator('#mlSeg button', { hasText: 'Атрибуция' }).click();
    assert.ok((await pg.locator('#mt thead').innerText()).includes('Цена (чистая)'));
    for (const lens of ['Атрибуция', 'Explain']) { await pg.locator('#mlSeg button', { hasText: lens }).click(); assert.doesNotMatch(await pg.locator('#main').innerText(), NO_SPLIT_FX, lens); }
    // C: no split in the selection — coupon «—» with its coverage; the swap has no funding % and no funding
    await tab(pg, 'carry');
    assert.match(await pg.locator('.k-kpi').first().innerText(), /—[\s\S]*нет разбивки кэрри: \d+ из \d+ записей/);
    const irs = await pg.locator('#ct tr', { hasText: 'Synthetic IRS pay' }).innerText();
    assert.ok(irs.split('\t').filter(t => t.trim() === '—').length >= 4, irs);
    await pg.locator('#cm1 button', { hasText: 'Брутто' }).click();
    const names = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('cc1')).getOption().series.map(s => s.name));
    assert.deepEqual(names, ['Кэрри', 'Roll-down', 'Кэрри брутто']);
    // the rate decision of the fixture takes effect on 05.10: a mark on «Кэрри в день»
    const marks = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('cc2')).getOption().series.flatMap(x => ((x.markLine || {}).data || []).map(m => m.xAxis)));
    assert.deepEqual(marks, ['2026-10-05']);
    // granularity is a request parameter
    await tab(pg, 'decomp');
    await pg.locator('#granSeg button', { hasText: 'Месяцы' }).click();
    await pg.waitForSelector('.k-kpis');
    assert.equal(calls.filter(u => u.pathname.endsWith('/period')).at(-1).searchParams.get('gran'), 'month');
    // widths and themes: no horizontal scroll of the page on any of A, B, C
    for (const theme of ['dark', 'light']) {
      await pg.evaluate(t => { if (KIT.theme() !== t) KIT.toggleTheme(); }, theme);
      for (const width of [1440, 390]) {
        await pg.setViewportSize({ width, height: 900 });
        for (const key of ['decomp', 'matrix', 'carry']) {
          await tab(pg, key); await pg.waitForTimeout(150);
          assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth), width, key + ' ' + theme + ' ' + width);
          if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, `c19-${key}-${theme}-${width}.png`), fullPage: true }); }
        }
      }
    }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19: the latest request wins and a loading period never shows the previous result', { skip: pw ? false : 'нет Playwright', timeout: 60000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    let release;
    const gate = new Promise(r => { release = r; });
    const stale = JSON.parse(JSON.stringify(FX.period)); stale.summary.total += 7e6;
    const fresh = JSON.parse(JSON.stringify(FX.period)); fresh.summary.total -= 3e6;
    // the YTD answer is held back and arrives after the 3М one
    const { pg, ctx, errors } = await open(browser, { delay: async u => {
      if (u.searchParams.get('from') === '2026-01-01') { await gate; return stale; }
      if (u.searchParams.get('from') === '2026-07-14') return fresh;
      return null;
    } });
    await pg.locator('#preset button', { hasText: 'YTD' }).click();
    await pg.waitForFunction(() => /Загружаю/.test(document.getElementById('status').textContent));
    assert.equal(await pg.locator('#main').innerHTML(), '');
    assert.equal(await pg.locator('#coveragePanel').isVisible(), false);
    await pg.locator('#preset button', { hasText: '3М' }).click();
    const want = await mn(pg, fresh.summary.total);
    await pg.waitForFunction(t => (document.querySelector('.k-kpi') || {}).innerText?.includes(t), want);
    release(); await pg.waitForTimeout(400);
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(want), 'the held-back YTD answer must not replace 3М');
    // a period over the limit: a message, no figures
    await pg.locator('#from').fill('2020-01-01'); await pg.locator('#from').dispatchEvent('change');
    assert.match(await pg.locator('#status').innerText(), /1096/);
    assert.equal(await pg.locator('.k-kpi').count(), 0);
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19 as a file: the demo says the period needs the server, tabs work, no errors', { skip: pw ? false : 'нет Playwright', timeout: 30000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const pg = await browser.newPage({ viewport: { width: 390, height: 800 } }), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await pg.goto('file://' + path.join(ROOT, 'concepts', 'c19-pnl-history.html'));
    assert.match(await pg.locator('#main').innerText(), /сохранённые прогоны сервера/);
    for (const key of ['matrix', 'carry', 'curves', 'position', 'control', 'decomp']) {
      await pg.locator('#tabs [data-tab=' + key + ']').click();
      assert.match(await pg.locator('#main').innerText(), /сохранённые прогоны сервера/);
    }
    assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C19 on a served synthetic store renders A, B, C from the real /period', { skip: !pw || !BASE, timeout: 60000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const pg = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    const response = pg.waitForResponse(r => r.url().includes('/api/v1/history/pnl/period?'));
    await pg.goto(BASE + '/concepts/c19-pnl-history.html');
    const d = await (await response).json();
    for (const k of ['summary', 'days', 'positions', 'buckets', 'keys']) assert.ok(k in d, k);
    await pg.waitForSelector('.k-kpis');
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(await mn(pg, d.summary.total)));
    for (const key of ['matrix', 'carry', 'decomp']) { await tab(pg, key); await pg.waitForSelector('.k-kpis, .k-empty'); assert.ok(!BAD.test(await pg.locator('#main').innerText()), key); }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
