// The last drawer is the position card; the list drawer remains underneath it. Synthetic API-shaped data only.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const ID = 'SYNTHETIC_UNVALUED';
const LABEL = 'Причина отсутствия оценки:';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function card(browser, { reason, notValued = true, width = 1440 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 } });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  D.positions.push({ ...D.positions[0], id: ID, name: 'Synthetic swap', isin: null, ccy: 'RUB',
    notValued, mv: notValued ? null : 1, mvRub: notValued ? null : 1, risk: {}, fxDelta: {},
    instrType: null, segment: 'Деривативы', assetClass: 'OTC-деривативы',
    extra: { quality: notValued ? 'not_valued' : 'exact', ...(reason == null ? {} : { reason }),
      reported: { x: { x_product_group: 'irs' } } } });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(id => window.MOCK && MOCK.pos[id], ID);
  await pg.evaluate(id => KIT.posList('Synthetic positions', [id]), ID);
  const list = pg.locator('.k-drawer').last();
  if (reason) assert.ok((await list.innerText()).includes(reason.trim()), 'reason already exists in the list');
  await list.locator('tr[data-id="' + ID + '"] td').first().click(); // Real event handler opens K.card.
  assert.strictEqual(await pg.locator('.k-drawer').count(), 2, 'list stays below the new card');
  return { pg, ctx, drawer: pg.locator('.k-drawer').last(), errors };
}

async function screenshot(pg, name) {
  if (process.env.CARD_REASON_SHOTS) {
    fs.mkdirSync(process.env.CARD_REASON_SHOTS, { recursive: true });
    await pg.screenshot({ path: path.join(process.env.CARD_REASON_SHOTS, name + '.png'), animations: 'disabled' });
  }
}

test('unvalued position card reason in the browser', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('click from list shows the existing reason in the card itself', async () => {
      const reason = 'no_rub_curve: synthetic missing curve';
      const { pg, ctx, drawer, errors } = await card(browser, { reason });
      try {
        await screenshot(pg, 'card-reason');
        assert.ok((await drawer.innerText()).includes(reason), 'last/card drawer must show the existing reason');
        assert.ok(await drawer.getByText(LABEL, { exact: true }).isVisible());
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('HTML special characters and injection remain literal text', async () => {
      const reason = 'synthetic <img src=x onerror="window.cardInjected=true"> & <script>window.cardInjected=true</script> "double" \'single\' >';
      const { pg, ctx, drawer, errors } = await card(browser, { reason });
      try {
        assert.ok((await drawer.innerText()).includes(reason));
        assert.strictEqual(await drawer.locator('img, script').count(), 0);
        assert.strictEqual(await pg.evaluate(() => window.cardInjected), undefined);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('long reason wraps inside a 390px card', async () => {
      const reason = 'synthetic_long_reason_' + 'x'.repeat(700) + ': ' + 'нет входных данных; '.repeat(35);
      const { pg, ctx, drawer, errors } = await card(browser, { reason, width: 390 });
      try {
        assert.ok((await drawer.innerText()).includes(reason.trim()));
        const note = drawer.getByText(LABEL, { exact: true }).locator('..');
        await note.scrollIntoViewIfNeeded();
        const size = await note.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth,
          left: el.getBoundingClientRect().left, right: el.getBoundingClientRect().right }));
        assert.ok(size.scroll <= size.client + 1, 'reason has no horizontal overflow');
        assert.ok(size.left >= 0 && size.right <= 390, 'reason stays inside the narrow viewport');
        await screenshot(pg, 'card-reason-390');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('absent reason is not invented', async () => {
      const { ctx, drawer, errors } = await card(browser);
      try {
        assert.strictEqual(await drawer.getByText(LABEL, { exact: true }).count(), 0);
        assert.ok((await drawer.innerText()).includes('оценка: not_valued'));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('valued card gets no new reason label even when estReason exists', async () => {
      const { ctx, drawer, errors } = await card(browser, { reason: 'synthetic existing explanation', notValued: false });
      try {
        assert.strictEqual(await drawer.getByText(LABEL, { exact: true }).count(), 0);
        assert.ok(!(await drawer.innerText()).includes('synthetic existing explanation'));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
