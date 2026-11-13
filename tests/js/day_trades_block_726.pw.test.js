// TDM#726: блок «Сделки за день» в карточке позиции — в общей карточке (K.card) и в карточке справа C03. Данные — то, что
// сервер положил в позицию (DESK_EXTRAS.positions[id].trade: куплено, продано, нетто), страница ничего не запрашивает у
// сервера о сделках; у позиции без сделок блока нет; ссылка ведёт на «Сделки дня» с отбором по позиции. Вымышленные данные.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

// две покупки по разным ценам (3 бумаги по 99 и 1 по 100) и продажа 2 бумаг по 101,5; номинал бумаги 1000
const TRADE = { qty: 2000, price: 99, n: 3, buy: { n: 2, counted: 2, priced: 2, bonds: 4, face: 4000, avg: 99.25 },
  sell: { n: 1, counted: 1, priced: 1, bonds: 2, face: 2000, avg: 101.5 }, net: { bonds: 2, face: 2000 } };

async function open(browser, ids) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], api = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  const positions = Object.fromEntries(ids.map(id => [id, { trade: TRADE }]));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify({ notes: [], positions }) + ';' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) { api.push(u.pathname); return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' }); }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(x => window.MOCK && MOCK.pos[x], ids[0]);
  return { pg, ctx, errors, api };
}

async function check(root, id, asOf) {
  const sec = root.locator('.dtb');
  assert.strictEqual(await sec.count(), 1, 'один блок');
  const text = (await sec.innerText()).replace(/\s+/g, ' ');
  for (const s of ['Куплено', 'Продано', 'Нетто: куплено', 'Количество', 'Цена, % ном.', 'Сделок', '99.2500', '101.5000', '4k RUB', '2k RUB'])
    assert.ok(text.includes(s), 'в блоке «' + s + '»: ' + text);
  assert.ok(/Куплено 4 шт\./.test(text) && /Продано 2 шт\./.test(text) && /Нетто: куплено 2 шт\./.test(text), text);
  const link = root.locator('a:has-text("сделки позиции на экране «Сделки дня»")');
  assert.strictEqual(await link.count(), 1);
  const href = new URL(await link.getAttribute('href'), ORIGIN + '/concepts/c03-blotter.html');
  assert.strictEqual(href.pathname, '/concepts/c20-trades.html');
  assert.strictEqual(href.searchParams.get('pos'), id);
  assert.strictEqual(href.searchParams.get('date'), asOf);
  assert.ok(/Сделки за день/.test(await root.textContent()), 'заголовок раздела (на экране он прописными)');
}

test('#726: блок «Сделки за день» в карточке позиции', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    const D = JSON.parse(RUN);
    const bonds = D.positions.filter(p => p.instrType === 'BOND').slice(0, 2).map(p => String(p.id));
    assert.strictEqual(bonds.length, 2);
    await t.test('K.card: блок есть у позиции со сделками, у другой нет', async () => {
      const { pg, ctx, errors, api } = await open(browser, [bonds[0]]);
      try {
        const asOf = await pg.evaluate(() => MOCK.asOf);
        await pg.waitForTimeout(300);
        const before = api.length;   // запросы самой страницы (строка «Сделки дня» в C03 и др.) к карточке не относятся
        await pg.evaluate(id => KIT.card(id), bonds[0]);
        await check(pg.locator('.k-drawer').last(), bonds[0], asOf);
        await pg.evaluate(id => KIT.card(id), bonds[1]);
        assert.strictEqual(await pg.locator('.k-drawer').last().locator('.dtb').count(), 0, 'без сделок блока нет');
        assert.ok(!(await pg.locator('.k-drawer').last().textContent()).includes('Сделки за день'));
        assert.deepStrictEqual(api.slice(before).filter(x => /trade/.test(x)), [], 'о сделках карточка сервер не спрашивает (история и потоки — её прежние запросы)');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('карточка справа в C03: тот же блок', async () => {
      const { pg, ctx, errors, api } = await open(browser, [bonds[0]]);
      try {
        const asOf = await pg.evaluate(() => MOCK.asOf);
        await pg.waitForSelector('tr[data-id="' + bonds[0] + '"]');
        await pg.waitForTimeout(300);
        const before = api.length;
        await pg.locator('tr[data-id="' + bonds[0] + '"] td').first().click();
        await pg.locator('#card .dtb').waitFor();
        await check(pg.locator('#card'), bonds[0], asOf);
        await pg.waitForSelector('tr[data-id="' + bonds[1] + '"]');
        await pg.locator('tr[data-id="' + bonds[1] + '"] td').first().click();
        await pg.waitForFunction(() => !document.querySelector('#card .dtb'));
        assert.deepStrictEqual(api.slice(before).filter(x => /trade/.test(x)), [], 'о сделках карточка сервер не спрашивает (история и потоки — её прежние запросы)');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally {
    await browser.close();
  }
});
