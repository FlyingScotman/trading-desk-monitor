// #30 в браузере: «прогноз →» карточки позиции ведёт на C17 без выбора прогона, срок которого (desk.run.v1, 12 ч)
// истёк, в том числе если вкладка простояла открытой до истечения. Только синтетические данные (fixtures/concepts_run).
//   node --test tests/js/carry_link_30.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const HOUR = 3600e3;
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function open(browser, ageHours) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], c17 = [];
  pg.on('pageerror', e => errors.push(e.message));
  await ctx.addInitScript(age => {
    if (location.pathname.endsWith('c03-blotter.html') && !sessionStorage.getItem('seeded')) {
      sessionStorage.setItem('seeded', '1');
      localStorage.setItem('desk.run.v1', JSON.stringify({ sel: { run_id: '42' }, at: Date.now() - age }));
    }
  }, ageHours * HOUR);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.endsWith('c17-forward-carry.html')) { c17.push(u.search); return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>C17</title>' }); }
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html?run_id=42');
  await pg.waitForFunction(() => window.MOCK && window.KIT && MOCK.pos[1]);
  await pg.evaluate(() => KIT.card(1));
  const link = pg.locator('.k-drawer').last().locator('a[data-carry-pos]');
  await link.waitFor();
  return { pg, ctx, link, errors, c17 };
}
const params = s => Object.fromEntries(new URLSearchParams(s));

test('#30: ссылка «прогноз →» на C17 и срок выбора прогона', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const [age, want] of [[11, { run_id: '42', pos: '1' }], [13, { pos: '1' }]]) {
      await t.test('сохранённый выбор возрастом ' + age + ' ч', async () => {
        const { pg, ctx, link, errors, c17 } = await open(browser, age);
        try {
          await Promise.all([pg.waitForURL(/c17-forward-carry/), link.click()]);
          assert.deepStrictEqual(params(c17[0]), want);
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      });
    }
    await t.test('вкладка простояла открытой до истечения срока', async () => {
      const { pg, ctx, link, errors, c17 } = await open(browser, 11);
      try {
        assert.deepStrictEqual(params(new URL(await link.getAttribute('href'), ORIGIN + '/concepts/').search), { run_id: '42', pos: '1' });
        await pg.evaluate(() => localStorage.setItem('desk.run.v1', JSON.stringify({ sel: { run_id: '42' }, at: Date.now() - 13 * 3600e3 })));
        await Promise.all([pg.waitForURL(/c17-forward-carry/), link.click()]);
        assert.deepStrictEqual(params(c17[0]), { pos: '1' });
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
