// Деривативы вне отчёта позиций (TDM#468), в браузере (Playwright), данные синтетические:
//  - «Настройки → Допущения расчёта»: выпадающий список «Деривативы вне отчёта позиций», поиск его находит, выбор
//    уходит в PUT как off_report_derivatives; без горизонтальной прокрутки на 390 и 1024 px;
//  - таблица позиций (c03): метка «вне» у позиции вне отчёта, «Чего нет в данных» со списком исключённых и
//    «возможно, уже в отчёте»; карточка позиции с тегом «вне отчёта позиций».
//   node --test tests/js/off_report_468.pw.test.js        (снимки: OFF_REPORT_SHOTS=<папка>)
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const SHOTS = process.env.OFF_REPORT_SHOTS;
const ID = 'FXB|N1';

async function shot(pg, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await pg.screenshot({ path: path.join(SHOTS, name + '.png'), animations: 'disabled' });
}
const noHScroll = pg => pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);

function route(pg, handle) {
  return pg.route('**/*', r => {
    const u = new URL(r.request().url());
    if (u.origin !== ORIGIN) return r.abort();
    const out = handle(u, r.request());
    if (out) return r.fulfill(out);
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return r.fulfill({ status: 404, body: '' });
    return r.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
}
const json = body => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });

async function settings(browser, width, saved) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 });
  const pg = await ctx.newPage(), errors = [], puts = [];
  pg.on('pageerror', e => errors.push(e.message));
  const doc = { home: 'C:\\Users\\u\\AppData\\Local\\DeskMonitor', hub_token_set: false, proxy_password_set: false, env_overrides: [], error: null,
    effective: { home: 'C:\\Users\\u\\AppData\\Local\\DeskMonitor', autopilot: true }, ...(saved || {}) };
  await route(pg, (u, req) => {
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname === '/api/v1/settings' && req.method() === 'GET') return json(doc);
    if (u.pathname === '/api/v1/settings' && req.method() === 'PUT') { const b = req.postDataJSON(); puts.push(b); Object.assign(doc, b); return json(doc); }
    if (u.pathname.startsWith('/api/')) return { status: 404, contentType: 'application/json', body: '{}' };
    return null;
  });
  await pg.goto(ORIGIN + '/settings.html#calc');
  await pg.waitForSelector('#off_report_derivatives');
  return { pg, ctx, errors, puts };
}

async function blotter(browser, width) {
  const D = JSON.parse(RUN);
  D.positions.push({ ...D.positions[0], id: ID, book: D.positions[0].book, name: 'CNY/RUB FX forward N1', isin: null, ccy: 'CNY',
    notValued: false, mv: 1000, mvRub: 12500, risk: {}, fxDelta: { CNY: 1e6 }, instrType: 'FX', segment: 'Деривативы',
    assetClass: 'OTC-деривативы', offReport: true, newToday: true, extra: { quality: 'exact', off_report: true, reported: {} } });
  const X = { notes: [], offReport: { mode: 'exclude', trades: 3, included: 0, valued: 0, suspect: 1, invalid: 0, excluded: 2, matured: 0,
    books: { FXB: 2, IRB: 1 }, truncated: false, list: [
      { book: 'IRB', trade_id: 'S7', kind: 'ois', maturity: '2027-03-15', state: 'excluded' },
      { book: 'FXB', trade_id: 'N5', kind: 'fx_forward', maturity: '2026-12-17', state: 'excluded' },
      { book: 'FXB', trade_id: 'D7', kind: 'fx_forward', maturity: '2027-01-20', state: 'suspect', why: 'номер сделки есть в отчёте другой книги' }] } };
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await route(pg, u => {
    if (u.pathname === '/concepts-run.js') return { status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' };
    if (u.pathname === '/concepts-data.js') return { status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(X) + ';' };
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname.startsWith('/api/')) return { status: 404, contentType: 'application/json', body: '{}' };
    return null;
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(id => window.MOCK && MOCK.pos[id], ID);
  return { pg, ctx, errors };
}

test('деривативы вне отчёта позиций в браузере (TDM#468)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1024, 390]) {
      await t.test('настройки, ' + width + ' px: список, поиск, сохранение', async () => {
        const { pg, ctx, errors, puts } = await settings(browser, width);
        try {
          const sel = pg.locator('#off_report_derivatives');
          assert.ok(await sel.isVisible());
          assert.deepStrictEqual(await sel.locator('option').allInnerTexts(), ['исключать (по умолчанию)', 'включать']);
          assert.strictEqual(await sel.inputValue(), '');
          assert.strictEqual(await pg.locator('#fx_unlinked_terms').count(), 0, 'старого флажка нет');
          assert.ok(/Деривативы вне отчёта позиций/.test(await pg.locator('label[for="off_report_derivatives"]').innerText()));
          const hint = await pg.locator('#off_report_derivatives').locator('xpath=..').innerText();
          assert.ok(/Включать — оцениваются отдельной позицией/.test(hint) && !/desk |--|python/i.test(hint), hint);
          assert.ok(await noHScroll(pg), 'нет горизонтальной прокрутки');
          await sel.scrollIntoViewIfNeeded();
          await shot(pg, 'settings-' + width);
          // поиск по настройкам находит пункт
          await pg.locator('#st-q').fill('вне отчёта');
          await pg.waitForSelector('#st-q-res li, #st-q-res [role=option]');
          assert.ok(/Деривативы вне отчёта позиций/.test(await pg.locator('#st-q-res').innerText()));
          await pg.locator('#st-q').fill('');
          await sel.selectOption('include');
          await pg.locator('#save').click();
          await pg.waitForTimeout(300);
          assert.strictEqual(puts[0].off_report_derivatives, 'include');
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      });
    }
    await t.test('старый флажок в сохранённом: список показывает «включать»', async () => {
      const { pg, ctx } = await settings(browser, 1024, { off_report_derivatives: 'include' });
      try { assert.strictEqual(await pg.locator('#off_report_derivatives').inputValue(), 'include'); } finally { await ctx.close(); }
    });
    for (const width of [1024, 390]) {
      await t.test('таблица позиций и «Чего нет в данных», ' + width + ' px', async () => {
        const { pg, ctx, errors } = await blotter(browser, width);
        try {
          const label = await pg.evaluate(id => { const p = MOCK.pos[id]; return KIT.nameCell(p, p.name) + KIT.offReportTag(p); }, ID);
          assert.ok(/>вне</.test(label) && /вне отчёта позиций: оценена по условиям сделки/.test(label), label);
          const notes = await pg.evaluate(() => MOCK.notes.filter(n => /отчёте позиций|вне отчёта/i.test(n)));
          assert.ok(notes.some(n => /исключены настройкой: сделок 2/.test(n)) && notes.some(n => /Возможно, уже в отчёте позиций/.test(n)), notes.join('|'));
          const html = await pg.evaluate(() => KIT.coverageHtml());
          assert.ok(/Деривативы вне отчёта позиций: 3/.test(html) && /Настройки → Допущения расчёта/.test(html) && /возможно, уже в отчёте/.test(html) && /форвард/.test(html) && !/fx_forward/.test(html), html.slice(0, 300));
          assert.ok(/S7/.test(html) && /15\.03\.2027/.test(html));
          await pg.evaluate(id => KIT.card(id), ID);
          const drawer = pg.locator('.k-drawer').last();
          assert.ok(/вне отчёта позиций/.test(await drawer.innerText()));
          assert.ok(await noHScroll(pg), 'нет горизонтальной прокрутки');
          await shot(pg, 'card-' + width);
          await pg.evaluate(() => document.querySelectorAll('.k-drawer').forEach(d => d.remove()));
          const nb = pg.locator('[data-a=notes]');
          if (await nb.count()) { await nb.first().evaluate(e => e.click()); await shot(pg, 'notes-' + width); assert.ok(await noHScroll(pg));
            const fits = await pg.evaluate(() => { const p = document.querySelector('.k-panel'); const th = [...p.querySelectorAll('th')].find(x => x.textContent === 'Состояние'); const r = th.getBoundingClientRect(); return p.scrollWidth <= p.clientWidth + 1 && r.right <= innerWidth + 1 && r.left >= 0; });
            assert.ok(fits, 'столбец «Состояние» виден без прокрутки'); }
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      });
    }
  } finally { await browser.close(); }
});
