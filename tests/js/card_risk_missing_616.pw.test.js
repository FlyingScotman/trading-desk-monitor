// TDM#616: the position card of a derivative valued without core says why it has no rates risk (extra.risk_missing:
// no curve of a currency, an NDF without terms, an NDF core does not value) — and says nothing when the row has the
// report's risk; a leg whose settlement currency was taken as RUB says so. Synthetic API-shaped data only.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const LABEL = 'Процентного риска нет:';
const TEXT = 'нет данных: нет кривой INR';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function card(browser, id, withRisk, legs) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  const risky = D.positions.find(p => Object.values(p.risk || {}).some(a => Array.isArray(a) && a.some(v => v)));
  const curve = Object.keys(risky.risk).find(c => risky.risk[c].some(v => v));
  const fx = D.positions.find(p => p.instrType === 'FX' && !Object.values(p.risk || {}).some(a => Array.isArray(a) && a.some(v => v)));
  D.positions.push({ ...fx, id, name: 'Synthetic NDF', isin: null, ccy: 'USD', notValued: false, mv: 1, mvRub: 80,
    risk: withRisk ? { [curve]: risky.risk[curve] } : {}, fxDelta: { USD: 1e6 }, instrType: 'FX', segment: 'Деривативы',
    assetClass: 'FX', extra: { quality: 'reported_value', not_exact_reason: 'no_foreign_curve',
      reported: { x: { x_product_group: 'fx_ndf' } }, risk_missing: { reason: 'no_curve', ccy: 'INR', text: TEXT },
      ...(legs ? { legs } : {}) } });
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
  await pg.waitForFunction(x => window.MOCK && MOCK.pos[x], id);
  await pg.evaluate(x => KIT.posList('Synthetic positions', [x]), id);
  await pg.locator('.k-drawer').last().locator('tr[data-id="' + id + '"] td').first().click();
  const text = await pg.locator('.k-drawer').last().innerText();
  await ctx.close();
  return { text, errors };
}

test('#616: card note «Процентного риска нет»', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('no risk: the run\'s reason is in the card', async () => {
      const { text, errors } = await card(browser, 'SYN_NDF_NO_RISK', false);
      assert.deepStrictEqual(errors, []);
      assert.ok(text.includes(LABEL + ' ' + TEXT), 'the card says why there is no risk');
    });
    await t.test('the report\'s risk: no contradicting note', async () => {
      const { text, errors } = await card(browser, 'SYN_NDF_REPORT_RISK', true);
      assert.deepStrictEqual(errors, []);
      assert.ok(!text.includes(LABEL), 'a row with risk must not say it has none');
    });
    await t.test('settlement currency taken as RUB: said on the leg', async () => {
      const leg = { leg: 'single', trade_id: 'T', value_date: '2026-12-17', buy_ccy: 'EUR', buy_amount: 1e6, sell_ccy: 'USD',
        sell_amount: 1.08e6, contract_rate: 1.08, rate_quote: 'EUR/USD', settlement: 'cash', settlement_ccy: 'RUB',
        settlement_ccy_assumed: true, payment_date: '2026-12-17', fixing_date: '2026-12-15', fixing_source: 'CBR' };
      const a = await card(browser, 'SYN_NDF_ASSUMED', false, [leg]);
      assert.deepStrictEqual(a.errors, []);
      assert.ok(a.text.includes('валюта расчёта не указана в условиях — принята RUB'), 'the leg says RUB was taken');
      const b = await card(browser, 'SYN_NDF_GIVEN', false, [{ ...leg, settlement_ccy_assumed: undefined }]);
      assert.ok(!b.text.includes('не указана в условиях'), 'a settlement currency in the terms: no note');
      // the adapter's default is a setting: the note names the currency it took
      const c = await card(browser, 'SYN_NDF_ASSUMED_USD', false, [{ ...leg, settlement_ccy: 'USD' }]);
      assert.ok(c.text.includes('валюта расчёта не указана в условиях — принята USD'), 'the taken currency is named');
    });
  } finally {
    await browser.close();
  }
});
