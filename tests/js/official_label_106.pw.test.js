// #106, #489 в браузере: Scenario Lab, PnL Explain, без официального PnL (как отдаёт сервер: officialReceived false) —
// линия истории «Explain ЦБ», подписи не называют её официальной; демо — как раньше. Синтетика data.js.
//   node --test tests/js/official_label_106.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
// как сервер без официального PnL: official = флэш + курсы ЦБ, officialReceived: false
const NO_OFFICIAL = ';(function (D) { D.official = { asOf: D.asOf, received: false, basis: "CBR" };' +
  ' D.positions.forEach(function (p) { if (p.pnl && p.pnl.available !== false) p.pnl.official = null; });' +
  ' D.history.forEach(function (h) { if (h.pnlExplained != null) h.pnlOfficial = h.pnlExplained + 1.5e6; h.officialReceived = false; }); })(window.DESK);';

// #489: официальный PnL пришёл не по всем книгам — сегодня без него книга CORP_RUB, в истории один день «partial»
const PARTIAL = ';(function (D) { D.positions.forEach(function (p) { if (p.book === "CORP_RUB" && p.pnl && p.pnl.available !== false) { p.pnl.official = null; p.pnl.unexplained = 0; } });' +
  ' Object.assign(D.history[3], { officialReceived: false, officialState: "partial", officialBooks: { received: 7, books: 8, missing: ["CORP_RUB"], pnlOfficial: 9e6, pnlExplained: 6.5e6 } }); })(window.DESK);';

async function open(browser, server, patch = NO_OFFICIAL) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await ctx.addInitScript(() => localStorage.setItem('scenlab.mode', '"expl"'));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/desk-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: server ? fs.readFileSync(path.join(ROOT, 'data.js'), 'utf8') + patch : '' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/scenario-lab.html' + (server ? '' : '?demo=1'));
  await pg.waitForFunction(() => window.echarts && echarts.getInstanceByDom(document.getElementById('h-main')));
  return { pg, ctx, errors };
}
const legend = pg => pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('h-main')).getOption().series.map(s => s.name));

test('#106: Scenario Lab — подпись «официальный» только у официального PnL', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('сервер без официального PnL', async () => {
      const { pg, ctx, errors } = await open(browser, true);
      try {
        assert.deepStrictEqual(await legend(pg), ['Explain ЦБ', 'Explained']);
        assert.match(await pg.locator('#h-main-cap').textContent(), /официального PnL нет/);
        assert.match(await pg.locator('#h-cum-cap').textContent(), /не официальный/);
        await pg.selectOption('#ex-date', { index: 1 });  // прошлый день
        assert.match(await pg.locator('#recon').textContent(), /Официального нет/);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('демо — как раньше', async () => {
      const { pg, ctx, errors } = await open(browser, false);
      try {
        assert.deepStrictEqual(await legend(pg), ['Official', 'Explained']);
        assert.strictEqual(await pg.locator('#h-main-cap').textContent(), 'Official vs explained, млн ₽');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('#489: официальный не по всем книгам — «частично официальный», книга без него названа', async () => {
      const { pg, ctx, errors } = await open(browser, true, PARTIAL);
      try {
        assert.deepStrictEqual(await legend(pg), ['Official', 'Частично официальный', 'Explained']);
        const part = pg.locator('#off-part');  // сегодня: книга без официального названа, чтобы запросить его
        assert.match(await part.textContent(), /Корпы RUB/);
        assert.match(await part.textContent(), /запросить в финансах/);
        assert.match(await pg.locator('#hier-wrap').textContent(), /Официального нет/);
        await pg.selectOption('#ex-date', await pg.evaluate(() => window.DESK.history[3].date));  // прошлый частичный день
        assert.match(await pg.locator('#recon').textContent(), /7 из 8 книг/);
        assert.match(await pg.locator('#recon').textContent(), /Official PnL \(ЦБ\) · частично/);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
