// TDM#617: «Откуда данные» в карточке позиции — в общей карточке (K.card) и в карточке справа C03 (showCard). Раздел
// свёрнут, до раскрытия на сервер не ходит; при раскрытии — источники сделки, расхождения словами, рыночные данные со
// состоянием; повторное раскрытие — без запроса. Синтетические данные только.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const ID = 'SYN|CCS-617';
const NO_TERMS = 'есть в отчёте позиций, но условия сделки не загружены';

function answer(id) {
  const set = (ds, name, extra) => Object.assign({ dataset: ds, dataset_name: name, source: 'syn_' + ds, file_name: 'syn_' + ds + '_20261020.csv',
    report_date: '2026-10-20', valid_date: '2026-10-20', loaded_at: '2026-10-20T15:05:00+00:00' }, extra || {});
  const src = (key, name, level, state, stateText, text, items) => ({ key, name, level, level_text: level === 'deal' ? 'по сделке' : 'по книге и валюте',
    state, state_text: stateText, text, items: items || [] });
  return {
    run_id: 7, business_date: '2026-10-20', position_id: id, book_id: 'SYN', kind: 'derivative', valued: false, off_report: false,
    sources: [
      src('positions', 'Позиции', 'deal', 'present', 'есть', 'строка отчёта позиций', [set('positions', 'Позиции', { ref: 'row <b>12</b>' })]),
      src('trade_terms', 'Условия сделок', 'deal', 'absent', 'нет данных', 'условий этой сделки в загруженных наборах нет'),
      src('trades', 'Сделки дня', 'deal', 'unknown', 'нет сведений', 'нет сведений: прогон не записал, какие сделки дня он читал'),
      src('ocp', 'ОВП', 'book_ccy', 'present', 'есть', 'строки по книге и валюте, у сделки своего ключа нет', [set('ocp', 'ОВП', { ccy: 'CNY', has_row: true })]),
      src('cash_balances', 'Остатки на счетах', 'book_ccy', 'absent', 'нет данных', 'по книге и валюте сделки строк нет',
        [{ dataset: 'cash_balances', dataset_name: 'Остатки на счетах', ccy: 'CNY', has_row: false, source: null, file_name: null, report_date: null, valid_date: null, loaded_at: null }]),
    ],
    mismatches: [{ code: 'report_no_terms', text: NO_TERMS }],
    market: { basis: 'needed', text: 'сделка не оценена: что нужно для оценки по её условиям', notes: ['почему не оценена: нет кривой валюты сделки'],
      items: [{ kind: 'curve', id: 'RUB RUONIA', name: 'RUB RUONIA OIS', state: 'ok', state_text: 'есть', text: '' },
        { kind: 'fx', id: 'CNY', name: 'курс CNY/RUB (спот)', state: 'ok', state_text: 'есть', text: '' },
        { kind: 'curve', id: 'CNY FX', name: 'CNY: кривая валюты (FX implied)', state: 'missing', state_text: 'нет', text: 'в рыночных данных прогона кривой нет' }] },
  };
}

async function open(browser, { width = 1440 } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], asked = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  const base = D.positions.find(p => p.instrType === 'IRS');
  D.positions.push({ ...base, id: ID, name: 'Synthetic CCS 617', ccy: 'CNY', notValued: true, mv: null, mvRub: null, risk: {}, fxDelta: {} });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname === '/api/v1/positions/provenance') {
      const id = u.searchParams.get('id');
      asked.push(id);
      return id === ID ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(answer(id)) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"code":"not_found"}' });
    }
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(id => window.MOCK && MOCK.pos[id] && MOCK.posProvenance, ID);
  return { pg, ctx, errors, asked };
}

async function checkSection(pg, root, asked) {
  const btn = root.locator('.k-prov-btn');
  assert.strictEqual(await btn.count(), 1, 'one button «Откуда данные»');
  assert.ok((await btn.innerText()).includes('Откуда данные'));
  assert.strictEqual(await btn.getAttribute('aria-expanded'), 'false', 'collapsed by default');
  assert.strictEqual(await root.locator('.k-prov-bd').isVisible(), false, 'nothing shown before it is opened');
  await pg.waitForTimeout(150);
  assert.deepStrictEqual(asked, [], 'no request until it is opened');
  await btn.click();
  const bd = root.locator('.k-prov-bd');
  await bd.locator('.k-prov-src').first().waitFor();
  assert.deepStrictEqual(asked, [ID], 'one request on opening');
  const text = await bd.innerText();
  for (const s of ['Позиции', 'Условия сделок', 'Сделки дня', 'ОВП', 'Остатки на счетах', 'по книге и валюте', 'syn_positions_20261020.csv',
    'syn_positions', 'отчёт за', 'загружен', 'нет сведений', 'нет данных'])
    assert.ok(text.includes(s), 'section shows «' + s + '»: ' + text);
  assert.ok(text.includes(NO_TERMS), 'the mismatch in words');
  assert.strictEqual(await bd.locator('.k-prov-mism li[data-code="report_no_terms"]').count(), 1);
  assert.ok(text.includes('row <b>12</b>'), 'server text stays literal');
  assert.strictEqual(await bd.locator('b:text("12")').count(), 0, 'no markup injected');
  // the market data: what is there and what is not
  const miss = bd.locator('.k-prov-mkt tr[data-id="CNY FX"] .k-prov-st');
  assert.strictEqual(await miss.getAttribute('data-state'), 'missing');
  assert.strictEqual((await miss.innerText()).trim(), 'нет');
  assert.strictEqual(await bd.locator('.k-prov-mkt tr[data-id="CNY"] .k-prov-st').getAttribute('data-state'), 'ok');
  assert.ok(text.includes('почему не оценена'));
  // collapse and open again: no second request
  await btn.click();
  assert.strictEqual(await bd.isVisible(), false);
  await btn.click();
  assert.strictEqual(await bd.isVisible(), true);
  await pg.waitForTimeout(100);
  assert.deepStrictEqual(asked, [ID], 'the answer is kept');
}

test('«Откуда данные» в карточке позиции', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('K.card: collapsed, lazy, sources, mismatch, market data', async () => {
      const { pg, ctx, errors, asked } = await open(browser);
      try {
        await pg.evaluate(id => KIT.card(id), ID);
        await checkSection(pg, pg.locator('.k-drawer').last(), asked);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('C03 card on the right: the same section', async () => {
      const { pg, ctx, errors, asked } = await open(browser);
      try {
        await pg.waitForSelector('tr[data-id="' + ID + '"]');
        await pg.locator('tr[data-id="' + ID + '"] td').first().click();
        await pg.locator('.cd-prov .k-prov-btn').waitFor();
        await checkSection(pg, pg.locator('.cd-prov'), asked);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('a server error is said in the section and asked again on the next opening', async () => {
      const { pg, ctx, errors, asked } = await open(browser);
      try {
        const other = await pg.evaluate(() => String(MOCK.positions.find(p => p.instrType === 'BOND').id));
        await pg.evaluate(id => KIT.card(id), other);
        const root = pg.locator('.k-drawer').last();
        await root.locator('.k-prov-btn').click();
        await root.getByText('не загружено: сервер не нашёл позицию в прогоне').waitFor();
        await root.locator('.k-prov-btn').click();
        await root.locator('.k-prov-btn').click();
        await root.getByText('не загружено').waitFor();
        assert.deepStrictEqual(asked, [other, other]);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('a 390px card has no horizontal overflow', async () => {
      const { pg, ctx, errors } = await open(browser, { width: 390 });
      try {
        await pg.evaluate(id => KIT.card(id), ID);
        const root = pg.locator('.k-drawer').last();
        await root.locator('.k-prov-btn').click();
        const bd = root.locator('.k-prov-bd');
        await bd.locator('.k-prov-src').first().waitFor();
        const size = await bd.evaluate(el => ({ scroll: el.scrollWidth, client: el.clientWidth, right: el.getBoundingClientRect().right }));
        assert.ok(size.scroll <= size.client + 1 && size.right <= 390 + 1, JSON.stringify(size));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
