// TDM#567 (D25, D24): плитка C06 «ОВП и курсы» в режиме «бары» — столбцы названы FX-дельтой позиций, публикуемая ОВП —
// ромбом на валюте; разбор по клику не называется «ОВП …»; ОВП не по текущей методике названа подписью сервера.
//   node --test tests/js/ocp_c06_567.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml' };

async function open(browser, note) {
  const D = JSON.parse(RUN), ent = (D.positions[0] || {}).entity || 'E';
  D.OCP = { asOf: D.asOf, state: 'computed', text: 'ОВП', basis: 'report_base_plus_day', methodNote: note || null,
    byCcy: { USD: { total: 5e6, rate: 80, totalRub: 4e8, bySource: { report: 5e6 } } },
    byBook: [{ entity: ent, book: null, books: [], ccy: 'USD', amount: 5e6, source: 'report', components: [], reported: 5e6, baseKind: note ? 'with_derivatives' : 'without_derivatives', baseDate: D.asOf, parts: { base: 5e6 } }],
    notes: [], gaps: [], sourceNames: { report: 'отчёт' }, componentNames: {}, books: {} };
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } }), pg = await ctx.newPage(), errors = [];
  await ctx.addInitScript(() => { try { localStorage.setItem('c06-modes', JSON.stringify({ 'morning.fx': 'bars' })); localStorage.setItem('c06-preset', 'morning'); } catch (e) { } });
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
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
  await pg.goto(ORIGIN + '/concepts/c06-launchpad.html');
  await pg.waitForTimeout(600);
  return { pg, ctx, errors };
}
const fxTile = pg => pg.evaluate(() => {
  const t = [...document.querySelectorAll('.tile')].find(x => /ОВП и курсы/.test(x.querySelector('h3').textContent));
  if (!t) return null;
  const c = t.querySelector('[_echarts_instance_]');
  return { text: t.innerText, series: c ? echarts.getInstanceByDom(c).getOption().series.map(s => [s.name, s.type]) : null };
});

test('C06 «ОВП и курсы», бары: FX-дельта позиций, ромб публикуемой ОВП, подпись методики', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const note of [null, 'не по текущей методике: отчёт ещё с деривативами, их PV не добавлен (FXB)']) {
      const { pg, ctx, errors } = await open(browser, note);
      const t = await fxTile(pg);
      assert.ok(t && t.series, 'плитка с графиком (режим «бары»)');
      assert.ok(t.text.includes('FX-дельта позиций') && t.text.includes('не публикуемая ОВП'), t.text);
      assert.deepEqual(t.series.map(s => s[0]), ['бонды', 'деривативы', 'ОВП публикуемая']);
      assert.equal(t.text.includes('не по текущей методике'), !!note);
      assert.deepEqual(errors, []);
      await ctx.close();
    }
  } finally { await browser.close(); }
});
