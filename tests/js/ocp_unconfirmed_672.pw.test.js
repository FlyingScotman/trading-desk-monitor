// TDM#672: an FX row on a cross without trade terms whose currency is not the first of the pair has no FX delta and is
// not in the OCP. The screens say so: C16 «ОВП» names the rows (day.unconfirmed of /api/v1/history/ocp) and the
// position card (C03) shows extra.ocp_unconfirmed. Playwright, skipped without it. Synthetic API-shaped data only.
//   node --test tests/js/ocp_unconfirmed_672.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const AS_OF = JSON.parse(RUN).asOf;
const TEXT = 'валюта строки отчёта (USD) не совпадает с первой валютой пары EUR/USD, единица номинала не подтверждена: в ОВП не включено до загрузки условий сделки';
const GAP = 'позиций без условий сделки на кросс-паре (EUR/USD) не включено в ОВП: 1';

function history(unconfirmed) {
  const day = {
    date: AS_OF, runId: 100, runType: 'EOD', state: 'computed', basis: 'report_plus_outside', methodNote: null, books: {},
    fx: { USD: { market: 81.45, cbr: 81.12 }, EUR: { market: 94.62, cbr: 94.38 } }, reportFx: { USD: 81.12, EUR: 94.38 },
    lines: [{ entity: 'Головной банк', book: 'CORP_FX', books: ['CORP_FX'], ccy: 'USD', amount: 14e6, source: 'report', reported: null, validDate: null }],
  };
  if (unconfirmed) day.unconfirmed = unconfirmed;
  return { runId: null, asOf: AS_OF, from: AS_OF, to: AS_OF, books: {}, days: [day] };
}

async function open(browser, url, { hist = null, run = RUN, ready }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + run + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname === '/api/v1/history/ocp' && hist) return json(hist);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  await ready(pg);
  return { pg, ctx, errors };
}

test('#672 C16: the rows left out of the OCP are named with the reason', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ready = pg => pg.waitForSelector('[data-ocp-total]').then(() => pg.waitForTimeout(200));
    const a = await open(browser, '/concepts/c16-ocp.html', { hist: history({ text: GAP + '. ' + TEXT, items: ['CORP_FX|N1'] }), ready });
    const note = a.pg.locator('[data-c16-unconfirmed]');
    assert.equal(await note.count(), 1, 'the warning is on the screen');
    const text = await note.innerText();
    assert.ok(text.includes('единица номинала не подтверждена') && text.includes('в ОВП не включено') && text.includes('EUR/USD'), text);
    const view = await a.pg.locator('#view').textContent();
    assert.ok(view.includes('Не вошло в ОВП') && view.includes('CORP_FX|N1'), 'the position is named');
    assert.equal(await a.pg.locator('a[href*="c03-blotter.html?pos=CORP_FX%7CN1"]').count(), 1, 'a link to the position');
    assert.ok(!/\bNaN\b|\bundefined\b/.test(view), view);
    assert.deepEqual(a.errors, []);
    await a.ctx.close();
    const b = await open(browser, '/concepts/c16-ocp.html', { hist: history(null), ready });
    assert.equal(await b.pg.locator('[data-c16-unconfirmed]').count(), 0, 'a day without such a row: no warning');
    assert.ok(!(await b.pg.locator('#view').textContent()).includes('Не вошло в ОВП'));
    assert.deepEqual(b.errors, []);
    await b.ctx.close();
  } finally { await browser.close(); }
});

test('#672 card: the position says it is not in the OCP', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const card = async (id, unconfirmed) => {
      const D = JSON.parse(RUN);
      const fx = D.positions.find(p => p.instrType === 'FX' && !Object.values(p.risk || {}).some(a => Array.isArray(a) && a.some(v => v)));
      D.positions.push({ ...fx, id, name: 'Synthetic NDF', isin: null, ccy: 'USD', notValued: false, mv: 0, mvRub: 0, risk: {}, fxDelta: {},
        instrType: 'FX', segment: 'Деривативы', assetClass: 'FX',
        extra: { quality: 'assumed', reported: { x: { x_product_group: 'fx_ndf', x_prim_cur: 'EUR', x_sec_cur: 'USD' } },
          ...(unconfirmed ? { ocp_unconfirmed: { reason: 'unit_unconfirmed', pair: 'EUR/USD', ccy: 'USD', text: TEXT } } : {}) } });
      const ready = pg => pg.waitForFunction(x => window.MOCK && MOCK.pos[x], id);
      const { pg, ctx, errors } = await open(browser, '/concepts/c03-blotter.html', { run: JSON.stringify(D), ready });
      await pg.evaluate(x => KIT.posList('Synthetic positions', [x]), id);
      await pg.locator('.k-drawer').last().locator('tr[data-id="' + id + '"] td').first().click();
      const text = await pg.locator('.k-drawer').last().innerText();
      await ctx.close();
      return { text, errors };
    };
    const a = await card('SYN_NDF_UNCONFIRMED', true);
    assert.deepStrictEqual(a.errors, []);
    assert.ok(a.text.includes('Не в ОВП: ' + TEXT), 'the card names the reason');
    const b = await card('SYN_NDF_CONFIRMED', false);
    assert.deepStrictEqual(b.errors, []);
    assert.ok(!b.text.includes('Не в ОВП:'), 'a row in the OCP: no warning');
  } finally { await browser.close(); }
});
