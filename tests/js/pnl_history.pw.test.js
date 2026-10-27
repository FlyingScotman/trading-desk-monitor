// Integration against the synthetic saved-run server seeded by test_pnl_history.seed_history.
// PNL_HISTORY_BASE=http://127.0.0.1:<port> node --test tests/js/pnl_history.pw.test.js
// No API replacement: delayed requests below still reach the real endpoint.
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('playwright'); } catch (_) { return null; } })();
const BASE = process.env.PNL_HISTORY_BASE, SHOTS = process.env.PNL_HISTORY_SHOTS;
const isSummary = r => r.url().includes('/api/v1/history/pnl?') && !new URL(r.url()).searchParams.has('position_id');

test('saved PnL: filters, source selection, days, precision, real charts and stale responses', { skip: !pw || !BASE, timeout: 90000 }, async () => {
  const browser = await pw.chromium.launch(), context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  try {
    const response = page.waitForResponse(isSummary);
    await page.goto(BASE + '/concepts/c19-pnl-history.html');
    let current = await (await response).json();
    const check = async d => {
      const formatted = await page.evaluate(v => KIT.f.n(v, 2), d.summary.total);
      await page.waitForFunction(t => document.querySelector('#total').textContent.includes(t) && !document.querySelector('#result').hidden, formatted);
      const rendered = await page.evaluate(() => ({
        chart: echarts.getInstanceByDom(document.getElementById('chart')).getOption(),
        total: document.getElementById('total').innerText, selected: document.getElementById('selected').innerText,
        names: [...document.querySelectorAll('#positions [data-position]')].map(e => e.textContent),
      }));
      assert.deepEqual(rendered.chart.series[0].data, d.days.map(x => x.cumulative));
      assert.deepEqual(rendered.chart.series[1].data, d.days.map(x => x.selectedCumulative));
      assert.equal(rendered.chart.yAxis[0].name, 'тыс RUB');
      const tooltip = await page.evaluate(() => {
        const o = echarts.getInstanceByDom(document.getElementById('chart')).getOption();
        return { value: o.tooltip[0].valueFormatter(1784.05), expected: KIT.f.n(1784.05, 2) + ' RUB' };
      });
      assert.equal(tooltip.value, tooltip.expected);
      assert.equal(rendered.names.length, d.positions.length);
      d.positions.forEach(p => assert.ok(rendered.names.includes(p.row.name + ' · ' + p.row.book)));
      return rendered;
    };
    await check(current);
    assert.ok(current.positions.some(p => p.row.positionId === 'CLOSED'));
    assert.ok(current.days.some(d => d.date === '2026-10-14' && d.total === null));
    await page.locator('#coverage details').evaluate(e => e.open = true);
    assert.match(await page.locator('#coverage').innerText(), /2026-10-14/);
    await page.locator('#coverage details').evaluate(e => e.open = false);
    async function change(action) { const response = page.waitForResponse(isSummary); await action(); current = await (await response).json(); await check(current); }
    await change(() => page.locator('#from').fill('2026-10-12').then(() => page.locator('#from').dispatchEvent('change')));
    const full = current.summary.total;
    await change(() => page.locator('#noFactors').click());
    assert.equal(current.summary.total, full); assert.equal(current.summary.selected, null);
    await change(() => page.locator('[data-factor=other]').check());
    assert.equal(current.summary.total, full); assert.notEqual(current.summary.selected, full);
    assert.match(await page.locator('#otherStatus').innerText(), /Разница PV|разница PV/);
    await change(() => page.locator('#allFactors').click());
    await change(() => page.locator('#books [data-book]').filter({ hasText: 'DEMO-B' }).click());
    assert.ok(current.summary.total < 0); assert.equal(current.positions.length, 1);
    await change(() => page.locator('#reset').click());
    assert.equal(current.summary.total, full);
    await page.locator('[data-f=ports]').click();
    assert.ok(await page.locator('.mi').filter({ hasText: 'Historical' }).count());
    await change(() => page.locator('.mi').filter({ hasText: 'Active' }).click());
    assert.equal(current.positions.length, 1); assert.equal(current.positions[0].row.positionId, 'CLOSED');
    await page.locator('#detailName').click();
    await change(() => page.locator('#reset').click());
    await change(() => page.locator('#ccy').selectOption('USD'));
    assert.ok(current.summary.total < 0); assert.equal(current.positions.length, 1);
    await change(() => page.locator('#reset').click());
    await page.locator('#positions [data-position]').filter({ hasText: 'Synthetic CLOSED' }).click();
    await page.waitForFunction(() => document.querySelector('#days').textContent.includes('desk-reported-v1'));
    const firstDay = await page.locator('#days tbody tr').first().innerText();
    assert.ok(firstDay.includes('2026-10-16')); // newest first, including explicit absent rows
    await page.waitForFunction(() => !!echarts.getInstanceByDom(document.getElementById('positionChart')));
    assert.equal(await page.evaluate(() => echarts.getInstanceByDom(document.getElementById('positionChart')).getOption().yAxis[0].name), 'тыс RUB');
    await page.route('**/api/v1/history/pnl?**', async route => {
      if (new URL(route.request().url()).searchParams.get('q') === 'Synthetic A') await new Promise(r => setTimeout(r, 600));
      await route.continue();
    });
    await page.locator('#search').fill('Synthetic A');
    assert.equal(await page.locator('#result').isVisible(), false);
    await change(() => page.locator('#search').fill('Synthetic B'));
    assert.ok(current.summary.total < 0); await page.waitForTimeout(850); await check(current);
    await change(() => page.locator('#reset').click());
    await change(() => page.locator('#from').fill('2026-10-15').then(() => page.locator('#from').dispatchEvent('change')));
    assert.equal(current.positions.length, 2); assert.equal(current.runs.length, 2);
    await change(() => page.locator('#from').fill('2026-10-12').then(() => page.locator('#from').dispatchEvent('change')));
    await page.locator('#positions [data-position]').filter({ hasText: 'Synthetic CLOSED' }).click();
    await page.waitForFunction(() => document.querySelector('#days').textContent.includes('desk-reported-v1'));
    await page.locator('#detailName').click();
    for (const theme of ['dark', 'light']) {
      await page.evaluate(t => { if (KIT.theme() !== t) KIT.toggleTheme(); }, theme);
      for (const width of [1280, 1024, 390]) {
        await page.setViewportSize({ width, height: 900 }); await page.waitForTimeout(180);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth), width);
        if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await page.screenshot({ path: path.join(SHOTS, `pnl-history-${theme}-${width}.png`), fullPage: true }); }
      }
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); await browser.close(); }
});

test('selected intraday overlap is separate; adjacent anchor remains in cumulative result', { skip: !pw || !BASE || !process.env.PNL_HISTORY_OVERLAP_RUN, timeout: 45000 }, async () => {
  const b = await pw.chromium.launch(), p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = []; p.on('pageerror', e => errors.push(e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  try {
    for (const [run, overlap] of [[process.env.PNL_HISTORY_OVERLAP_RUN, true], [process.env.PNL_HISTORY_ADJACENT_RUN, false]]) {
      const response = p.waitForResponse(isSummary);
      await p.goto(BASE + '/concepts/c19-pnl-history.html?run_id=' + run);
      const d = await (await response).json();
      await p.waitForFunction(() => !document.querySelector('#result').hidden);
      assert.equal(await p.locator('#anchor').isVisible(), overlap);
      const values = await p.evaluate(() => echarts.getInstanceByDom(document.getElementById('chart')).getOption().series[0].data);
      assert.deepEqual(values, d.days.map(x => x.cumulative));
      if (overlap) {
        assert.ok(d.separateAnchor); assert.equal(values.at(-1), null);
        assert.match(await p.locator('#anchorSummary').innerText(), /в итоги периода и накопление не входит/);
        await p.locator('#anchorPositions [data-anchor-position]').first().click();
        await p.waitForFunction(() => document.querySelector('#days').textContent.includes('Отдельно, не в накоплении'));
        if (SHOTS) await p.screenshot({ path: path.join(SHOTS, 'pnl-history-intraday-separate.png'), fullPage: true });
      } else { assert.equal(d.separateAnchor, undefined); assert.notEqual(values.at(-1), null); }
    }
    assert.deepEqual(errors, []);
  } finally { await b.close(); }
});

test('saved USD representation keeps history amounts and labels explicitly in RUB', { skip: !pw || !BASE, timeout: 30000 }, async () => {
  const b = await pw.chromium.launch(), p = await b.newPage({ viewport: { width: 1280, height: 900 } });
  const errors = []; p.on('pageerror', e => errors.push(e.message));
  p.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  try {
    await p.addInitScript(() => localStorage.setItem('concepts-repr', 'USD'));
    for (const run of [null, process.env.PNL_HISTORY_OVERLAP_RUN].filter(x => x !== undefined)) {
      const response = p.waitForResponse(isSummary);
      const detailResponse = p.waitForResponse(r => r.url().includes('/api/v1/history/pnl?') && new URL(r.url()).searchParams.has('position_id'));
      await p.goto(BASE + '/concepts/c19-pnl-history.html' + (run ? '?run_id=' + run : ''));
      const d = await (await response).json();
      await p.waitForFunction(() => !document.querySelector('#result').hidden);
      assert.equal(await p.evaluate(() => KIT.repr), 'USD'); // synthetic run has a real FX reference
      assert.equal(d.currency, 'RUB');
      const expected = await p.evaluate(v => KIT.f.n(v, 2), d.summary.total);
      assert.ok((await p.locator('#total').innerText()).includes(expected)); // no division by the USD rate
      const selectedExpected = await p.evaluate(v => KIT.f.n(v, 2), d.summary.selected);
      assert.ok((await p.locator('#selected').innerText()).includes(selectedExpected));
      assert.match(await p.locator('.ctl').innerText(), /Все суммы в RUB/);
      for (const id of ['positions', 'books']) assert.ok((await p.locator('#' + id + ' th').allTextContents()).includes('Итог, RUB'));
      assert.ok((await p.locator('#components th').allTextContents()).includes('Доступная сумма, RUB'));
      await p.waitForSelector('#days tbody tr');
      const detail = await (await detailResponse).json();
      assert.ok((await p.locator('#days th').allTextContents()).includes('Итог, RUB'));
      assert.equal(await p.evaluate(() => localStorage.getItem('concepts-repr')), 'USD');
      const charts = await p.evaluate(() => ['chart', 'positionChart'].map(id => {
        const o = echarts.getInstanceByDom(document.getElementById(id)).getOption();
        return { axis: o.yAxis[0].name, tooltip: o.tooltip[0].valueFormatter(1784.05), expected: KIT.f.n(1784.05, 2) + ' RUB' };
      }));
      charts.forEach(c => { assert.equal(c.axis, 'тыс RUB'); assert.equal(c.tooltip, c.expected); });
      const series = await p.evaluate(() => ['chart', 'positionChart'].map(id => echarts.getInstanceByDom(document.getElementById(id)).getOption().series.map(s => s.data)));
      assert.deepEqual(series[0], [d.days.map(x => x.cumulative), d.days.map(x => x.selectedCumulative)]);
      assert.deepEqual(series[1], [detail.days.map(x => x.cumulative), detail.days.map(x => x.selectedCumulative)]);
      assert.doesNotMatch(await p.locator('.k-page').innerText(), /[$€¥₽]/);
      if (run) {
        const anchorExpected = await p.evaluate(v => KIT.f.n(v, 2), d.separateAnchor.summary.total);
        assert.ok((await p.locator('#anchorSummary').innerText()).includes(anchorExpected + ' RUB'));
        assert.ok((await p.locator('#anchorPositions th').allTextContents()).includes('Итог, RUB'));
      }
      if (SHOTS) await p.screenshot({ path: path.join(SHOTS, run ? 'pnl-history-saved-usd-intraday.png' : 'pnl-history-saved-usd.png'), fullPage: true });
    }
    assert.deepEqual(errors, []);
  } finally { await b.close(); }
});
