'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { TEXT } = require('./fixtures/concepts_run');
const { DEAL, IDS, dealSearchRun } = require('./fixtures/deal_search');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.join(__dirname, '../../prototypes');
const rowIds = rows => rows.evaluateAll(nodes => nodes.map(r => r.dataset.id).sort());

test('C03: Ctrl+K и поиск в таблице находят сделку в двух книгах и отдельную позицию по полному номеру', { skip: !pw && 'нет Playwright' }, async () => {
  const D = JSON.parse(TEXT);
  dealSearchRun(D);
  const browser = await pw.chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1600, height: 900 } });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('http://desk.test/**', route => {
      const { pathname } = new URL(route.request().url());
      if (pathname === '/concepts-run.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
      if (pathname === '/concepts-data.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.DESK_EXTRAS = {};' });
      if (pathname === '/api/v1/auth/state') return route.fulfill({ json: { enabled: false } });
      if (pathname.startsWith('/api/')) return route.fulfill({ status: 404, json: {} });
      return route.fulfill({ path: path.join(ROOT, pathname) });
    });
    await page.goto('http://desk.test/concepts/c03-blotter.html');
    const tableRows = page.locator('#tbl tbody tr.row');
    await tableRows.first().waitFor();
    assert.deepEqual(await rowIds(tableRows), [...IDS].sort());
    assert.ok((await tableRows.allTextContents()).every(text => !text.includes(DEAL) && IDS.every(id => !text.includes(id))));

    await page.keyboard.press('Control+k');
    const palette = page.locator('.k-pal');
    const paletteInput = palette.locator('input');
    const results = palette.locator('.it');
    await paletteInput.fill(DEAL);
    assert.equal(await results.count(), 2);
    const hints = await results.locator('.r').allTextContents();
    assert.ok(IDS.slice(0, 2).every(id => hints.some(hint => hint.includes(id))));
    await paletteInput.fill(IDS[1]);
    assert.equal(await results.count(), 1);
    assert.ok((await results.locator('.r').textContent()).includes(IDS[1]));
    await paletteInput.press('Enter');
    const card = page.locator('.k-drawer .st');
    await card.waitFor();
    assert.match(await card.textContent(), /TEST-B/);
    await page.keyboard.press('Escape');

    const tableInput = page.locator('#q');
    await tableInput.fill(DEAL);
    assert.deepEqual(await rowIds(tableRows), IDS.slice(0, 2));
    await tableInput.fill(IDS[1]);
    assert.deepEqual(await rowIds(tableRows), [IDS[1]]);

    // Общий компонент таблиц на других экранах с одной видимой колонкой.
    await page.evaluate(() => {
      const host = document.createElement('div');
      host.id = 'test-position-table';
      document.querySelector('#tbl').appendChild(host);
      KIT.table(host, { rows: MOCK.positions, columns: [{ key: 'name', label: 'Бумага' }] });
    });
    const genericTable = page.locator('#test-position-table');
    const genericInput = genericTable.getByPlaceholder('поиск в таблице…');
    const genericRows = genericTable.locator('tbody tr.row');
    await genericInput.fill(DEAL);
    assert.deepEqual(await rowIds(genericRows), IDS.slice(0, 2));
    await genericInput.fill(IDS[1]);
    assert.deepEqual(await rowIds(genericRows), [IDS[1]]);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
