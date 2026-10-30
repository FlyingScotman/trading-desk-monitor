// День без кривой RUONIA (#31) в браузере: C03 — пометка «прокси RUB OFZ» у бумаги с extra.curve_proxy в таблице, её
// текст в карточке справа и в полной карточке (K.card); у позиции без PnL — причина сервера (смена кривой
// дисконтирования: вчера/сегодня), а не только «—». Синтетические данные в форме ответа API.
//   node --test tests/js/proxy_curve_31.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const MARK = 'на прокси-кривой RUB OFZ: кривой RUONIA за дату нет';
const WHY = 'смена кривой дисконтирования: вчера RUB RUONIA, сегодня RUB OFZ (сегодня кривой RUONIA не было — бумага на '
  + 'прокси-кривой RUB OFZ): спреды к разным кривым';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function open(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  const bonds = D.positions.filter(p => p.instrType === 'BOND' && !p.notValued);
  const d = bonds.find(p => p.couponType === 'float'), plain = bonds.find(p => p.id !== d.id);
  d.spreadCurve = 'RUB OFZ';
  d.extra = Object.assign({}, d.extra, { curve_proxy: { curve: 'RUB OFZ', instead_of: 'RUB RUONIA', reason: 'no_ruonia_curve', text: MARK } });
  d.pnl = { carry: null, rolldown: null, rates: null, spread: null, fx: null, funding: null, convexity: null, newTrades: null,
    other: null, reported: null, explained: null, available: false, reasonText: WHY, method: null, methodText: null };
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
  // пресет колонок «Кэрри и PnL»: видны колонки PnL дня и его компонент
  await pg.addInitScript(() => { try { localStorage.setItem('c03-state', JSON.stringify({ v: 2, preset: 'pnl', grouping: 'none', color: 'issuer', sel: null })); } catch (e) { } });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(id => window.MOCK && MOCK.pos[id], d.id);
  await pg.waitForSelector('tr[data-id="' + d.id + '"]');
  return { pg, ctx, errors, id: d.id, plain: plain.id };
}

test('#31 C03: прокси-кривая и причина отсутствия PnL видны', { skip: pw ? false : 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, id, plain } = await open(browser);
    try {
      // таблица: пометка у бумаги на прокси, у другой — нет; «—» PnL с причиной в подсказке
      const row = pg.locator('tr[data-id="' + id + '"]').first();
      const tag = row.locator('.k-tag.k-proxy');
      assert.strictEqual(await tag.count(), 1, 'пометка в строке бумаги на прокси');
      assert.strictEqual((await tag.innerText()).trim(), 'прокси RUB OFZ');
      assert.strictEqual(await tag.getAttribute('title'), MARK);
      assert.strictEqual(await pg.locator('tr[data-id="' + plain + '"] .k-tag.k-proxy').count(), 0, 'у бумаги без поля пометки нет');
      const titles = await row.locator('span.mut3[title]').evaluateAll(els => els.map(e => e.getAttribute('title')));
      assert.ok(titles.includes('нет PnL: ' + WHY), 'у «—» PnL — причина в подсказке: ' + JSON.stringify(titles));
      // карточка справа
      await row.locator('td').first().click();
      await pg.waitForFunction(() => document.querySelector('.cd-fields') && document.querySelector('.cd-fields').innerText.includes('прокси'));
      assert.ok((await pg.locator('.cd-fields').innerText()).includes(MARK));
      assert.ok((await pg.locator('.cd-pnl').innerText()).includes('Нет PnL: ' + WHY));
      // полная карточка
      await pg.evaluate(x => KIT.card(x), id);
      const card = pg.locator('.k-drawer').last();
      const text = await card.innerText();
      assert.ok(text.includes('Кривая: ' + MARK), 'пометка в карточке');
      assert.ok(text.includes('Нет PnL: ' + WHY), 'причина отсутствия PnL в карточке');
      // бумага без прокси и с PnL фикстуры — без пометки и без строки «Нет PnL» с выдуманной причиной
      await pg.evaluate(x => KIT.card(x), plain);
      const other = await pg.locator('.k-drawer').last().innerText();
      assert.ok(!other.includes(MARK) && !other.includes('смена кривой дисконтирования'));
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
