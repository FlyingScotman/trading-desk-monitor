// C16 «ОВП» in the browser (Playwright, skipped without it): TDM#713 — a figure «из позиций» in the drill-down
// «Из чего складывается ОВП» is opened to its first sources: each position, cash balance and funding liability with the
// date and file of the set it was read from, «перенесён» for a set older than the run's date; the items sum to the figure
// (and a response whose items do not is said); the book without positions in the run says which set it has; the basis of a
// position (nominal: with its PV); the rate the card converts at: the one chosen in the toggle («курс MOEX 19:00» /
// «курс ЦБ»), the other next to it, the USD rate too in «в USD». A response without `origins` (a server before it) keeps
// the one line.
//   node --test tests/js/ocp_c16_origin_713.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;

async function openDrawer({ origins, fx, reportFx = {}, ccy = 'USD', rate = null, unit = null }) {
  const { TEXT: RUN } = require('./fixtures/concepts_run');
  const AS_OF = JSON.parse(RUN).asOf;
  const L = { entity: 'Головной банк', book: 'FXB', books: ['FXB'], ccy, amount: 10.5e6, source: 'derived_stale_report', reported: null, validDate: null };
  const hist = { runId: 8, asOf: AS_OF, from: AS_OF, to: AS_OF, books: {}, days: [
    { date: AS_OF, runId: 8, runType: 'EOD', state: 'computed', basis: 'report_base_plus_day', fx, reportFx, lines: [L] }] };
  const block = { run_id: 8, state: 'computed', components: [{ key: 'cash', name: 'кэш и к расчёту' }, { key: 'fx_legs', name: 'PV валютных ног' }], parts: [],
    ...(origins ? { origins } : {}),
    published: { basis: 'report_base_plus_day', by_book: [{ entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy, amount: 10.5e6, source: 'derived_stale_report', components: [], outside: {}, reported: null, valid_date: '2026-10-13' }] } };
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
  // the toggles of the card (the rate, the unit) are set before the window is opened: the note follows what the card converts at
  for (const name of [rate, unit].filter(Boolean)) {
    await pg.getByRole('button', { name, exact: true }).click();
    await pg.waitForSelector('[data-ocp-row="FXB"]');
  }
  await pg.locator('[data-ocp-row="FXB"] td').first().click();
  await pg.locator('.k-drawer [data-c16-sum]').waitFor();
  return { pg, browser, errors };
}

const SET = { set_id: 5, valid_date: '2026-10-09', carried: true, age_days: 6, source: 'inbox', file_name: 'positions_20261012.csv', report_date: null };
const ITEMS = [
  { kind: 'position', key: 'FXB|FWD', position_id: 'FXB|FWD', book_id: 'FXB', component: 'fx_legs', basis: 'nominal', amount: 4e6, pv: 3.85e6, note: '', name: 'USD forward', set: SET },
  { kind: 'cash', key: 'CASH|FXB|USD', position_id: null, book_id: 'FXB', component: 'cash', basis: 'balance', amount: 6.5e6, pv: null, note: '', restored: true,
    accounts: [{ account_id: 'ACC-1', account_kind: null, ledger: null, balance_ccy: 7e6 }, { account_id: 'ACC-2', account_kind: null, ledger: null, balance_ccy: -5e5 }],
    sets: [{ set_id: 6, valid_date: '2026-10-01', carried: true, age_days: 14, source: 'inbox', file_name: 'cash_balances_20261001.csv', report_date: null }] }];
const ORG = (over = {}) => ({ entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: over.ccy || 'USD', source: 'derived_stale_report', amount: 10.5e6, sum: 10.5e6,
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
    assert.match(pos, /вид строки: PV валютных ног · по номиналу; PV \+?3[.,]85 mm · набор позиций/);
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
    assert.match(rate, /^Курс пересчёта в рубли: применён курс MOEX 19:00 \(источник «MOEX 19:00»\): 1 USD = 81[.,]4500 ₽; второй курс — ЦБ: 81[.,]1200 ₽$/);
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
    assert.match(await dr.locator('[data-c16-ratenote]').innerText(), /курса USD у дня нет/);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C16 on a run: the rate note names the rate the card converts at, the one chosen in the toggle, with the other next to it (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const note = async o => { const { pg, browser, errors } = await openDrawer(o); try { const t = await pg.locator('.k-drawer [data-c16-ratenote]').innerText(); assert.deepEqual(errors, []); return t; } finally { await browser.close(); } };
  const both = { origins: [ORG()], fx: FX };
  // «курс MOEX 19:00» (the default): the market rate with its source; the CBR rate next to it
  assert.match(await note(both), /^Курс пересчёта в рубли: применён курс MOEX 19:00 \(источник «MOEX 19:00»\): 1 USD = 81[.,]4500 ₽; второй курс — ЦБ: 81[.,]1200 ₽$/);
  // «курс ЦБ»: the CBR rate is the applied one, the market rate with its source is next to it
  const cbr = await note({ ...both, rate: 'курс ЦБ' });
  assert.match(cbr, /^Курс пересчёта в рубли: применён курс ЦБ: 1 USD = 81[.,]1200 ₽; второй курс — MOEX 19:00 \(источник «MOEX 19:00»\): 81[.,]4500 ₽$/);
  assert.ok(!/курса прогона нет/.test(cbr));
  // a currency with the market rate but without the CBR rate of the run: «курса прогона нет» only under «курс ЦБ», where it is the report's rate
  const noCbr = { origins: [ORG()], fx: { USD: { market: 81.45, cbr: null, source: 'MOEX 19:00' } }, reportFx: { USD: 80.5 } };
  const m = await note(noCbr);
  assert.match(m, /применён курс MOEX 19:00 \(источник «MOEX 19:00»\): 1 USD = 81[.,]4500 ₽$/);
  assert.ok(!/курса прогона нет/.test(m) && !/отчёта ОВП/.test(m), m);
  assert.match(await note({ ...noCbr, rate: 'курс ЦБ' }), /курса прогона нет \(ЦБ\); применён курс из отчёта ОВП: 1 USD = 80[.,]5000 ₽; второй курс — MOEX 19:00 \(источник «MOEX 19:00»\): 81[.,]4500 ₽ \(не применяется\)$/);
  // no rate of the applied kind and none of the report: said, the other kind is named as not applied
  assert.match(await note({ origins: [ORG()], fx: { USD: { market: null, cbr: 81.12, source: null } } }),
    /^Курс пересчёта в рубли: курса USD у дня нет; второй курс — ЦБ: 81[.,]1200 ₽ \(не применяется\)$/);
});

test('C16 on a run: «в USD» names the USD rate the figure is divided by, in the toggle\'s kind (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const note = async o => { const { pg, browser, errors } = await openDrawer(o); try { const t = await pg.locator('.k-drawer [data-c16-ratenote]').innerText(); assert.deepEqual(errors, []); return t; } finally { await browser.close(); } };
  const fx = { CNY: { market: 11.05, cbr: 11.2, source: 'MOEX 19:00' }, USD: { market: 81.45, cbr: 81.12, source: 'MOEX 19:00' } };
  const base = { origins: [ORG({ ccy: 'CNY' })], fx, ccy: 'CNY' };
  // «в валюте»: only the rate of the currency of the line
  const ccy = await note(base);
  assert.match(ccy, /1 CNY = 11[.,]0500 ₽; второй курс — ЦБ: 11[.,]2000 ₽$/);
  assert.ok(!/USD/.test(ccy.replace(/«MOEX 19:00»/g, '')), ccy);
  // «в USD»: and the USD rate, the market one
  assert.match(await note({ ...base, unit: 'в USD' }),
    /1 CNY = 11[.,]0500 ₽; второй курс — ЦБ: 11[.,]2000 ₽\. В USD — делим на курс USD: применён курс MOEX 19:00 \(источник «MOEX 19:00»\): 1 USD = 81[.,]4500 ₽; второй курс — ЦБ: 81[.,]1200 ₽$/);
  // «в USD» under «курс ЦБ»: both rates are the CBR ones
  assert.match(await note({ ...base, unit: 'в USD', rate: 'курс ЦБ' }),
    /применён курс ЦБ: 1 CNY = 11[.,]2000 ₽; второй курс — MOEX 19:00 \(источник «MOEX 19:00»\): 11[.,]0500 ₽\. В USD — делим на курс USD: применён курс ЦБ: 1 USD = 81[.,]1200 ₽; второй курс — MOEX 19:00 \(источник «MOEX 19:00»\): 81[.,]4500 ₽$/);
  // the USD rate of the run is missing: the report's, as the card does
  assert.match(await note({ ...base, fx: { CNY: fx.CNY }, reportFx: { USD: 80.9 }, unit: 'в USD' }),
    /В USD — делим на курс USD: курса прогона нет \(MOEX 19:00\); применён курс из отчёта ОВП: 1 USD = 80[.,]9000 ₽$/);
});

test('C16 on a run: a position says what its sum stands on; a nominal comes with its PV (TDM#713)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const item = (id, basis, pv) => ({ kind: 'position', key: 'FXB|' + id, position_id: 'FXB|' + id, book_id: 'FXB', component: 'fx_legs', basis, amount: 1e6, pv, note: '', name: id, set: SET });
  const items = [item('NOM', 'nominal', 9.5e5), item('NOPV', 'nominal', null), item('PV', 'spot_equivalent', null), item('NTL', 'notional', null),
    item('AMT', 'amount', null), item('MV', 'mv', null), item('MVN', 'mv_net', null), item('ODD', 'something_new', null),
    { kind: 'liability', key: 'FUND|FXB|USD|B1', position_id: 'FXB|B1', book_id: 'FXB', component: 'funding', basis: 'liability', amount: -1e6, pv: null, note: '', name: 'Bond' }];
  const { pg, browser, errors } = await openDrawer({ origins: [ORG({ items, sum: 7e6, amount: 7e6, matches: true })], fx: FX });
  try {
    const dr = pg.locator('.k-drawer');
    const by = async id => dr.locator('[data-c16-origin]', { hasText: 'FXB|' + id }).first();
    const basis = async id => (await (await by(id)).locator('[data-c16-origin-basis]').allInnerTexts());
    assert.match((await basis('NOM'))[0], /^по номиналу; PV \+?0[.,]95 mm$/);
    assert.equal((await basis('NOPV'))[0], 'по номиналу; PV нет');
    assert.equal((await basis('PV'))[0], 'по спот-эквиваленту (PV)');
    assert.equal((await basis('NTL'))[0], 'по номиналу, PV нет');
    assert.equal((await basis('AMT'))[0], 'по сумме позиции');
    assert.equal((await basis('MV'))[0], 'по грязной MV');
    assert.equal((await basis('MVN'))[0], 'по грязной MV за вычетом пассива');
    assert.equal((await basis('ODD'))[0], 'основание: something_new');
    // the liability has no basis of a position: it is named by its kind
    assert.equal((await basis('B1')).length, 0);
    assert.ok(!BAD.test(await dr.innerText()));
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
