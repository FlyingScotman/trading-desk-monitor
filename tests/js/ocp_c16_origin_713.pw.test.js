// C16 «ОВП» in the browser (Playwright, skipped without it): TDM#713 — a figure «из позиций» in the drill-down
// «Из чего складывается ОВП» is opened to its first sources: each position, cash balance and funding liability with the
// date and file of the set it was read from, «перенесён» for a set older than the run's date; the items sum to the figure
// (and a response whose items do not is said); the book without positions in the run says which set it has; the rate
// the figure is converted at and its source. A response without `origins` (a server before it) keeps the one line.
//   node --test tests/js/ocp_c16_origin_713.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;

async function openDrawer({ origins, fx }) {
  const { TEXT: RUN } = require('./fixtures/concepts_run');
  const AS_OF = JSON.parse(RUN).asOf;
  const L = { entity: 'Головной банк', book: 'FXB', books: ['FXB'], ccy: 'USD', amount: 10.5e6, source: 'derived_stale_report', reported: null, validDate: null };
  const hist = { runId: 8, asOf: AS_OF, from: AS_OF, to: AS_OF, books: {}, days: [
    { date: AS_OF, runId: 8, runType: 'EOD', state: 'computed', basis: 'report_base_plus_day', fx, reportFx: {}, lines: [L] }] };
  const block = { run_id: 8, state: 'computed', components: [{ key: 'cash', name: 'кэш и к расчёту' }, { key: 'fx_legs', name: 'PV валютных ног' }], parts: [],
    ...(origins ? { origins } : {}),
    published: { basis: 'report_base_plus_day', by_book: [{ entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: 'USD', amount: 10.5e6, source: 'derived_stale_report', components: [], outside: {}, reported: null, valid_date: '2026-10-13' }] } };
  const browser = await pw.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname === '/api/v1/history/ocp') return json(hist);
    if (u.pathname === '/api/v1/runs/8/ocp') return json(block);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c16-ocp.html');
  await pg.waitForSelector('[data-ocp-row="FXB"]');
  await pg.locator('[data-ocp-row="FXB"] td').first().click();
  await pg.locator('.k-drawer [data-c16-sum]').waitFor();
  return { pg, browser, errors };
}

const SET = { set_id: 5, valid_date: '2026-10-09', carried: true, age_days: 6, source: 'inbox', file_name: 'positions_20261012.csv', report_date: null };
const ITEMS = [
  { kind: 'position', key: 'FXB|FWD', position_id: 'FXB|FWD', book_id: 'FXB', component: 'fx_legs', basis: 'notional', amount: 4e6, pv: null, note: '', name: 'USD forward', set: SET },
  { kind: 'cash', key: 'CASH|FXB|USD', position_id: null, book_id: 'FXB', component: 'cash', basis: 'balance', amount: 6.5e6, pv: null, note: '', restored: true,
    accounts: [{ account_id: 'ACC-1', account_kind: null, ledger: null, balance_ccy: 7e6 }, { account_id: 'ACC-2', account_kind: null, ledger: null, balance_ccy: -5e5 }],
    sets: [{ set_id: 6, valid_date: '2026-10-01', carried: true, age_days: 14, source: 'inbox', file_name: 'cash_balances_20261001.csv', report_date: null }] }];
const ORG = (over = {}) => ({ entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: 'USD', source: 'derived_stale_report', amount: 10.5e6, sum: 10.5e6,
  matches: true, items: ITEMS, rest: null, sets: [{ book_id: 'FXB', set: SET }], ...over });
const FX = { USD: { market: 81.45, cbr: 81.12, source: 'MOEX 19:00' } };

test('C16 on a run: «из позиций» lists its positions and balances with the date and file of their sets; the items sum to the figure (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const { pg, browser, errors } = await openDrawer({ origins: [ORG()], fx: FX });
  try {
    const dr = pg.locator('.k-drawer');
    const items = dr.locator('[data-c16-origin]');
    assert.equal(await items.count(), 2);
    const pos = await dr.locator('[data-c16-origin="position"]').innerText(), cash = await dr.locator('[data-c16-origin="cash"]').innerText();
    assert.match(pos, /FXB\|FWD · USD forward · вид строки: PV валютных ног/);
    assert.match(pos, /набор позиций от 09\.10\.26 — перенесён с прошлой даты \(6 дн\.\), файл positions_20261012\.csv/);
    assert.match(cash, /остаток на счетах: 2 сч\./);
    assert.match(cash, /набор остатков от 01\.10\.26 — перенесён с прошлой даты \(14 дн\.\), файл cash_balances_20261001\.csv/);
    assert.match(cash, /ACC-1 7[  ,]?000[  ,]?000; ACC-2 −?500[  ,]?000|ACC-1.*ACC-2/);
    assert.equal(await dr.locator('[data-c16-origin-diff]').count(), 0);
    // the items sum to the figure's total
    const num = t => { const s = t.replace(/\s/g, ''), v = parseFloat(s.replace(/[()+−-]/g, '')); return /^\(|^[−-]/.test(s) ? -v : v; };
    const amounts = await dr.locator('[data-c16-origin] td.n').allInnerTexts();
    const total = await dr.locator('tr[data-c16-sum] td.n').innerText();
    assert.ok(Math.abs(amounts.map(num).reduce((a, v) => a + v, 0) - num(total)) < 0.02, amounts.join() + ' vs ' + total);
    // the position links to C03; the rate of the currency and its source
    assert.match(await dr.locator('[data-c16-origin="position"] a').first().getAttribute('href'), /^c03-blotter\.html\?pos=FXB%7CFWD$/);
    const rate = await dr.locator('[data-c16-ratenote]').innerText();
    assert.match(rate, /1 USD = 81[.,]4500 ₽, источник «MOEX 19:00»; курс ЦБ 81[.,]1200 ₽/);
    assert.ok(!BAD.test(await dr.innerText()));
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C16 on a run: a book without positions in the run says which set it has; items that do not sum are said; the rest is listed (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const cashOnly = ITEMS.slice(1);
  const { pg, browser, errors } = await openDrawer({ origins: [ORG({ items: cashOnly, sum: 6.5e6, matches: false, rest: { count: 3, amount: 1e6 } })], fx: {} });
  try {
    const dr = pg.locator('.k-drawer');
    assert.match(await dr.locator('[data-c16-origin-book]').innerText(), /позиций в расчёте нет; набор позиций от 09\.10\.26 — перенесён/);
    assert.match(await dr.locator('[data-c16-origin-rest]').innerText(), /ещё 3/);
    assert.match(await dr.locator('[data-c16-origin-diff]').innerText(), /разбивка не сходится с суммой строки: в ней \+?6[.,]50 mm/);
    assert.match(await dr.locator('[data-c16-ratenote]').innerText(), /курса этой валюты у дня нет/);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C16 on a run: a response without the breakdown keeps the one line «из позиций: отчёт устарел» (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const { pg, browser, errors } = await openDrawer({ origins: null, fx: FX });
  try {
    const dr = pg.locator('.k-drawer');
    assert.match(await dr.locator('tr.pt').first().innerText(), /из позиций: отчёт устарел — позиции книги на C03/);
    assert.equal(await dr.locator('[data-c16-origin]').count(), 0);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C16 demo: a book derived from positions has the breakdown, its items sum to the cell (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 390, height: 900 } }), pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    await pg.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.origin !== ORIGIN) return route.abort();
      if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
      if (u.pathname.startsWith('/api/') || u.pathname === '/concepts-run.js' || u.pathname === '/concepts-data.js') return route.fulfill({ status: 404, body: '' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c16-ocp.html?demo=1');
    await pg.waitForSelector('[data-ocp-row="IC-BND"]');
    await pg.locator('[data-ocp-row="IC-BND"] td').first().click();
    const dr = pg.locator('.k-drawer');
    await dr.locator('[data-c16-sum]').first().waitFor();
    const num = t => { const s = t.replace(/\s/g, ''), v = parseFloat(s.replace(/[()+−-]/g, '')); return /^\(|^[−-]/.test(s) ? -v : v; };
    const amounts = await dr.locator('[data-c16-origin] td.n').allInnerTexts();
    assert.equal(amounts.length, 2);
    assert.ok(Math.abs(amounts.map(num).reduce((a, v) => a + v, 0) - num(await dr.locator('tr[data-c16-sum] td.n').first().innerText())) < 0.02);
    assert.ok(await dr.locator('[data-c16-origin-set]').count() >= 2);
    assert.ok(!BAD.test(await dr.innerText()));
    assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'нет прокрутки вбок');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
