// C16 «ОВП» in the browser on the demo response (?demo=1; Playwright, skipped without it):
// TDM#604 — one book selection for every day: the demo has days of two kinds (older ones with an entity-level report
// part without books and a book the newer days lack); with a narrowed selection «месяц» and «3 мес» agree on their
// common dates and no day holds an unchecked or never-chosen book; the point's tooltip counts the day's books.
//   node --test tests/js/ocp_c16_drill.pw.test.js        C16_SHOTS=<dir> also saves screenshots
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const SHOTS = process.env.C16_SHOTS;
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;

async function open(browser, { width = 1366, theme = 'light', store = {}, url = '/concepts/c16-ocp.html?demo=1', api = {} } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(([t, st]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); Object.entries(st).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, [theme, store]);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/')) calls.push(u.pathname + u.search);
    if (api[u.pathname]) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(api[u.pathname]) });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/') || u.pathname === '/concepts-run.js' || u.pathname === '/concepts-data.js') return route.fulfill({ status: 404, body: '' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  await pg.waitForSelector('[data-ocp-total]');
  await pg.waitForTimeout(200);
  return { pg, ctx, errors, calls };
}
const chartOf = pg => pg.evaluate(() => { const o = echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption();
  return { x: o.xAxis[0].data, line: o.series.find(s => s.name === 'Общая').data, series: o.series.map(s => s.name) }; });
const tipAt = (pg, i) => pg.evaluate(k => { const o = echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption();
  return o.tooltip[0].formatter(o.series.map(z => ({ dataIndex: k, seriesName: z.name, value: 1, marker: '' }))); }, i);

test('C16 demo: one book selection for every day — «месяц» and «3 мес» agree, no unchecked book in any day (TDM#604)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors } = await open(browser);
    assert.equal(await pg.evaluate(() => window.CONCEPTS_SOURCE), 'demo');
    // все книги: в старых днях есть часть юрлица без книг и прежняя книга — входят, подсказка без «исключено»
    await pg.getByRole('button', { name: '3 мес' }).click();
    const all = await chartOf(pg);
    const t0 = await tipAt(pg, 0);
    assert.ok(/книг дня: (\d+) из \1</.test(t0) && !t0.includes('без книг не учтена'), t0);
    // сузить выбор: снять одну книгу — прежняя книга (её нет в таблице) и часть юрлица без книг не входят ни в один день
    await pg.getByRole('button', { name: 'месяц' }).click();
    await pg.locator('[data-c16="books"]').click();
    await pg.locator('.mi', { hasText: 'FX: прежняя книга' }).first().click();  // снять прежнюю — сужает выбор
    await pg.mouse.click(5, 5);
    const m1 = await chartOf(pg);
    await pg.getByRole('button', { name: '3 мес' }).click();
    const m3 = await chartOf(pg);
    assert.ok(m3.x.length > m1.x.length + 15, [m1.x.length, m3.x.length].join());
    const at = Object.fromEntries(m3.x.map((d, i) => [d, m3.line[i]]));
    m1.x.forEach((d, i) => assert.ok(Math.abs(at[d] - m1.line[i]) < 1e-9, d + ': ' + at[d] + ' vs ' + m1.line[i]));
    // старый день под суженным выбором меньше, чем без выбора, на часть юрлица и прежнюю книгу
    assert.ok(m3.line[0] < all.line[0] - 5, [m3.line[0], all.line[0]].join());
    const t1 = await tipAt(pg, 0);
    assert.ok(/исключено выбором: 1/.test(t1) && t1.includes('часть отчёта по юрлицу') && t1.includes('без книг не учтена'), t1);
    const tl = await tipAt(pg, m3.x.length - 1);
    assert.ok(/книг дня: (\d+) из \1</.test(tl) && !tl.includes('без книг'), 'в новом дне ни одна книга не снята: ' + tl);
    const saved = JSON.parse(await pg.evaluate(() => localStorage.getItem('tdm.view.v1:c16:books')));
    assert.ok(saved.off.includes('FXB-OLD') && !saved.on.includes('FXB-OLD') && saved.on.length > 3, JSON.stringify(saved));
    assert.ok(!BAD.test(await pg.locator('#view').textContent()));
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c16-604.png'), fullPage: true }); }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
