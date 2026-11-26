'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { TEXT } = require('./fixtures/concepts_run');
const { settle } = require('./fixtures/pw_pages');
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();

const ROOT = path.resolve(__dirname, '../../prototypes');
const ORIGIN = 'http://c18.test';
const PAGE_URL = ORIGIN + '/concepts/c18-carry.html';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' };
const unsafeName = '<img src=x onerror="window.c18Injected=true"> Тестовая позиция';
const unsafeBook = '<b>Тестовая книга A</b>';
const bookFundingLabel = 'Фондирование вне позиций (книги)';

function syntheticRun() {
  const d = JSON.parse(TEXT);
  const template = d.positions.find(p => p.instrType === 'BOND' && p.ccy === 'RUB');
  const book = (name, entity) => ({ name, entity, desk: 'Тестовый деск', bookType: 'TRADING' });
  const position = (id, bookId, carry, rolldown, funding, pnl = {}, extra = {}) => ({
    ...template, id, book: bookId, isin: null, instrumentId: 'test-' + id,
    name: id === 'T1' ? unsafeName : 'Тестовая позиция ' + id,
    issuer: 'Тестовый эмитент ' + bookId, issuerGroup: null, sector: null,
    entity: 'Тестовое юрлицо ' + bookId, portfolio: 'Тестовый портфель', maturity: '2027-01-15', tenorY: 1,
    notional: 1e7, notionalRub: 1e7, mv: 1e7, mvRub: 1e7,
    risk: { 'RUB RUONIA': d.BUCKETS.map((_, i) => i === 2 ? (id === 'T4' ? -5 : -1000) : 0) },
    pnl: { available: true, carry, rolldown, funding, explained: carry + rolldown + funding, ...pnl }, extra,
  });
  Object.assign(d, {
    asOf: '2026-01-15', prevDate: '2026-01-12', pnlAvailable: true, accrualDays: 3,
    BOOKS: { A: book(unsafeBook, 'Тестовое юрлицо A'), B: book('Тестовая книга B', 'Тестовое юрлицо B') },
    ISSUERS: {}, FX: { ...d.FX, USD: 100, EUR: 125, CNY: 10 },
    fundingStatus: { state: 'computed', bookLevel: -90000, inPositions: -123000, total: -213000,
      byBook: { A: { bookLevel: -30000 }, B: { bookLevel: -60000 } } },
    positions: [
      position('T1', 'A', 120000, 30000, -120000, { carryCoupon: 90000, carryPull: 30000 },
        { funding: { lines: [{ rule_source: 'default', rate_pct: 10, body_ccy: 1e7, ccy: 'RUB' }] } }),
      position('T2', 'B', 270000, 0, null, {}, { funding: { state: 'not_configured', reason: 'Синтетический пропуск' } }),
      position('T3', 'B', null, null, -3000),
      position('T4', 'B', -90000, 0, 0),
    ],
  });
  return d;
}

async function open(browser, { width, run } = {}) {
  const pg = await browser.newPage({ viewport: { width, height: 1000 } });
  const errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const apiCalls = [];
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ contentType: TYPES['.js'], body: 'window.CONCEPTS_RUN = ' + JSON.stringify(run) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: TYPES['.js'],
      body: 'window.DESK_EXTRAS = { histDates: ["2026-01-09", "2026-01-12", "2026-01-15"], hist: {accrualDays:[1,3,3],pnl:{carry:[10000,30000,30000],funding:[-6000,-18000,null],netCarry:[3000,9000,null]}} };' });
    if (u.pathname.startsWith('/api/')) {
      apiCalls.push(u.pathname);
      if (u.pathname.endsWith('/events/stream')) return route.fulfill({ contentType: 'text/event-stream', body: ': test\n\n' });
      return route.fulfill({ json: { enabled: false, user: null, last_id: 0, events: [], runs: [] } });
    }
    const f = path.resolve(ROOT, '.' + u.pathname);
    if (!f.startsWith(ROOT + path.sep) || !fs.existsSync(f) || !fs.statSync(f).isFile()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: TYPES[path.extname(f)] || 'application/octet-stream', path: f });
  });
  await pg.goto(PAGE_URL + (run ? '' : '?demo=1'));
  return { pg, errors, apiCalls };
}

async function chart(pg, id) {
  await pg.locator('#' + id).scrollIntoViewIfNeeded();
  await pg.waitForFunction(el => echarts.getInstanceByDom(document.getElementById(el)), id);
  return pg.evaluate(el => {
    const option = echarts.getInstanceByDom(document.getElementById(el)).getOption();
    return { labels: option.xAxis[0].data, series: option.series };
  }, id);
}

async function clickPoint(pg, id, seriesIndex, dataIndex) {
  await chart(pg, id);
  await settle(pg);
  const point = await pg.evaluate(({ id, seriesIndex, dataIndex }) => {
    const el = document.getElementById(id), ec = echarts.getInstanceByDom(el);
    const shape = ec.getModel().getSeriesByIndex(seriesIndex).getData().getItemGraphicEl(dataIndex);
    const bounds = shape.getBoundingRect().clone();
    bounds.applyTransform(shape.getComputedTransform());
    const parent = el.getBoundingClientRect();
    return { x: parent.left + bounds.x + bounds.width / 2, y: parent.top + bounds.y + bounds.height / 2 };
  }, { id, seriesIndex, dataIndex });
  await pg.mouse.click(point.x, point.y);
}

const kpi = (pg, key) => pg.locator('[data-c18-kpi="' + key + '"] .v');
const cell = (pg, row, key) => pg.locator(row + ' td').nth(columnIndex[key]);
const columnIndex = { carryCouponDay: 7, carryPullDay: 8, fundingDay: 11, netDay: 13, be30: 15 };
const closeDrawer = pg => pg.locator('.k-drawer .close').last().click();
const opts = { skip: !pw && 'Playwright unavailable' };

test('C18 demo paints all widgets, runs chart drilldowns and survives theme, scale and currency changes', opts, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, errors } = await open(browser, { width: 1280 });
    assert.equal(await pg.locator('#kpis .k-kpi').count(), 6);
    assert.equal(await pg.locator('#ban').isVisible(), false);
    assert.ok((await chart(pg, 'chWf')).labels.includes('Купон'));
    assert.ok((await chart(pg, 'chSc')).series.some(s => s.type === 'scatter' && s.data.length));
    assert.equal((await chart(pg, 'chDim')).series.length, 3);
    assert.equal((await chart(pg, 'chHist')).series.length, 3);
    for (const id of ['bleedTbl', 'genTbl', 'posTbl']) assert.ok(await pg.locator('#' + id + ' tbody tr.row').count());
    assert.equal(await pg.locator('#posTbl button', { hasText: 'Колонки' }).count(), 1);
    assert.equal(await pg.locator('#posTbl button', { hasText: 'CSV' }).count(), 1);

    await clickPoint(pg, 'chWf', 1, 0);
    await pg.locator('.k-drawer .t').filter({ hasText: 'Купон' }).waitFor();
    await closeDrawer(pg);

    await pg.locator('#scale').getByRole('button', { name: 'в месяц ×30' }).click();
    assert.match(await kpi(pg, 'net').innerText(), /mm ₽\/мес/);
    await pg.locator('#scale').getByRole('button', { name: 'в год ×365' }).click();
    assert.match(await kpi(pg, 'net').innerText(), /mm ₽\/год/);
    await pg.locator('#dim').getByRole('button', { name: 'Срочность' }).click();
    assert.match(await pg.locator('#dimTbl').innerText(), /1–3Y/);
    await pg.locator('#scX').getByRole('button', { name: '|CS01|' }).click();
    await pg.locator('#scY').getByRole('button', { name: 'BE 30д' }).click();
    assert.equal((await chart(pg, 'chSc')).series[0].type, 'scatter');
    await pg.locator('#posTbl tbody tr.row td.c18-name').first().click();
    await pg.locator('.k-drawer .t').first().waitFor();
    await closeDrawer(pg);

    await pg.locator('[data-a=theme]').click();
    assert.equal(await pg.locator('html').getAttribute('data-theme'), 'light');
    for (const width of [1280, 1920]) {
      await pg.setViewportSize({ width, height: 1000 });
      assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
    await pg.locator('[data-a=repr]').click();
    await pg.locator('.mi').filter({ hasText: 'в USD $' }).click();
    await pg.waitForFunction(() => window.KIT?.repr === 'USD');
    assert.match(await kpi(pg, 'net').innerText(), /mm \$\/год/);
    assert.equal(await pg.locator('#dim button[aria-pressed=true]').innerText(), 'Срочность');
    assert.equal(await pg.locator('html').getAttribute('data-theme'), 'light');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C18 server view keeps incomplete rows outside net, totals only selected books, and escapes data', opts, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, errors, apiCalls } = await open(browser, { width: 1920, run: syntheticRun() });
    assert.match(await pg.locator('#ban').innerText(), /2 из 4 поз.*1 поз. без фондирования.*1 поз. на рыночной ставке по умолчанию.*1 поз. без расчёта кэрри/s);
    assert.equal(await kpi(pg, 'net').locator('[data-dr]').innerText(), '(20)');
    await pg.locator('[data-a=numfmt]').click();
    assert.equal(await kpi(pg, 'net').locator('[data-dr]').innerText(), '−20');
    await pg.locator('[data-a=numfmt]').click();
    assert.match(await pg.locator('#posTbl tfoot').innerText(), /2 \/ 4 с полным нетто/);
    const r = id => '#posTbl tbody tr[data-id="' + id + '"]';
    assert.match(await pg.locator(r('T1')).innerText(), /Тестовая позиция/);
    assert.equal(await pg.locator(r('T1') + ' img').count(), 0);
    assert.equal(await pg.locator('#dimTbl tbody td').getByText(unsafeBook, { exact: true }).count(), 1);
    assert.equal(await pg.evaluate(() => window.c18Injected), undefined);
    assert.equal(await cell(pg, r('T1'), 'carryCouponDay').innerText(), '+30');
    assert.equal(await cell(pg, r('T4'), 'carryPullDay').innerText(), '—');
    assert.equal(await cell(pg, r('T4'), 'fundingDay').innerText(), '0');
    assert.equal(await cell(pg, r('T2'), 'netDay').innerText(), '—');
    assert.equal(await cell(pg, r('T3'), 'fundingDay').innerText(), '(1)');
    assert.equal(await cell(pg, r('T3'), 'netDay').innerText(), '—');
    assert.equal(await pg.evaluate(() => MOCK.pos.T4.dv01), -5);
    assert.equal(await cell(pg, r('T4'), 'be30').innerText(), '—');

    const wf = await chart(pg, 'chWf');
    assert.deepEqual(wf.series[1].data.map(d => [d.step.key, d.step.day]),
      [['coupon', 30000], ['pull', 10000], ['roll', 10000], ['unsplit', -30000], ['funding', -40000], ['net', -20000]]);
    const h = await chart(pg, 'chHist');
    assert.deepEqual(h.series[2].data, [0.003, 0.003, null]);
    await pg.locator('#histNorm').getByRole('button', { name: 'за прогон' }).click();
    assert.deepEqual((await chart(pg, 'chHist')).series[2].data, [0.003, 0.009, null]);
    assert.ok(!apiCalls.some(u => /\/(carry|pnl|history)\b/.test(u)));

    await pg.getByRole('button', { name: 'Показать без фондирования' }).click();
    assert.equal(await pg.locator('#posTbl tbody tr.row').count(), 1);
    assert.equal(await pg.locator(r('T2')).count(), 1);
    await pg.locator('#show').getByRole('button', { name: 'Все', exact: true }).click();
    await pg.locator('#tblGrp').getByRole('button', { name: 'Эмитенты' }).click();
    assert.equal(await pg.locator('#posTbl tr.grp').count(), 2);

    await pg.locator('#inclBook').check();
    assert.equal(await kpi(pg, 'net').locator('[data-dr]').innerText(), '(50)');
    for (const root of ['[data-c18-kpi="net"]', '#wfNote']) {
      const note = pg.locator(root).getByText('Добавлено · ' + bookFundingLabel, { exact: true });
      assert.ok(await note.isVisible());
      assert.match(await note.getAttribute('title'), /остатков денежных средств, закрытых позиций и строк уровня книги без расчёта PnL/);
    }
    await kpi(pg, 'net').locator('[data-dr]').click();
    assert.match(await pg.locator('.k-drawer .st').innerText(), /3 строк дают вклад/);
    assert.ok((await pg.locator('.k-drawer tbody').innerText()).includes(bookFundingLabel));
    await closeDrawer(pg);
    await pg.locator('[data-f=books]').click();
    await pg.locator('.mi').filter({ hasText: unsafeBook }).click();
    await pg.locator('#kpis').click({ position: { x: 2, y: 2 } });
    assert.equal(await kpi(pg, 'net').locator('[data-dr]').innerText(), '0');
    assert.match(await pg.locator('#ban').innerText(), /1 из 1 поз/);
    assert.equal(await pg.locator('#posTbl tbody tr.row').count(), 1);
    await clickPoint(pg, 'chSc', 0, 0);
    await pg.locator('.k-drawer .t').filter({ hasText: 'Тестовая позиция' }).waitFor();
    assert.equal(await pg.locator('.k-drawer .t img').count(), 0);
    await closeDrawer(pg);

    await pg.locator('[data-f=reset]').click();
    await pg.locator('[data-f=books]').click();
    await pg.locator('.mi').filter({ hasText: 'Тестовая книга B' }).click();
    await pg.locator('#kpis').click({ position: { x: 2, y: 2 } });
    assert.deepEqual((await chart(pg, 'chWf')).labels, ['Кэрри (купон + pull)', 'Rolldown', 'Фондирование', bookFundingLabel, 'Нетто-кэрри']);
    for (const [currency, currencySymbol, spot] of [['USD', '$', 100], ['EUR', '€', 125], ['CNY', '¥', 10]]) {
      await pg.locator('[data-a=repr]').click();
      await pg.locator('.mi').filter({ hasText: 'в ' + currency + ' ' + currencySymbol }).click();
      await pg.waitForFunction(c => window.KIT?.repr === c, currency);
      const parts = await pg.evaluate(() => {
        const p = MOCK.pos.T1;
        return [p.carryCouponDay, p.carryPullDay, p.carryPureDay];
      });
      assert.deepEqual(parts, [30000 / spot, 10000 / spot, 40000 / spot]);
      assert.ok((await kpi(pg, 'net').innerText()).includes(currencySymbol));
    }
    assert.equal(await pg.evaluate(() => window.c18Injected), undefined);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
