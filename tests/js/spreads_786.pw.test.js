// TDM#786 в браузере (Playwright, демо-данные): на экранах с таблицей позиций рядом со «Спред» — «Z к КБД» и «Z к RUONIA»,
// страница без горизонтальной прокрутки на 1024 и 390 px, ошибок страницы нет; карточка позиции показывает обе строки.
//   node --test tests/js/spreads_786.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

async function open(browser, width, page, before) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', r => {
    const u = new URL(r.request().url());
    if (u.origin !== ORIGIN) return r.abort();
    if (u.pathname === '/api/v1/auth/state') return r.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return r.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return r.fulfill({ status: 404, body: '' });
    return r.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  if (before) await pg.addInitScript(before);
  await pg.goto(ORIGIN + '/concepts/' + page + '.html?demo=1');
  return { ctx, pg, errors };
}
const heads = pg => pg.evaluate(() => [...document.querySelectorAll('th')].map(t => t.textContent.trim().replace(/б\.п\.$/, '')));
const noHScroll = pg => pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1);

test('экраны с позициями и спредом: «Спред», «Z к КБД», «Z к RUONIA» рядом, без горизонтальной прокрутки (1024, 390 px)', { skip: pw ? false : 'нет Playwright', timeout: 240000 }, async t => {
  const browser = await pw.chromium.launch();
  try {
    const SCREENS = [
      ['C01 · вкладка «Позиции»', 'c01-cockpit', () => { try { localStorage.setItem('c01-tab', 'pos'); } catch (e) { } }, null],
      ['C03 · «Позиции»', 'c03-blotter', null, null],
      ['C07 · карта портфеля', 'c07-portfolio-map', null, null],
      ['C08 · календарь', 'c08-calendar', null, null],
      ['C12 · терминал, команда pos', 'c12-terminal', null, async pg => { await pg.fill('#cmd', 'pos'); await pg.press('#cmd', 'Enter'); }],
      ['C13 · эмитенты', 'c13-issuers', null, null],
    ];
    for (const width of [1024, 390]) for (const [name, page, before, act] of SCREENS) await t.test(name + ', ' + width + ' px', async () => {
      const { ctx, pg, errors } = await open(browser, width, page, before);
      try {
        await pg.waitForTimeout(800);
        if (act) { await act(pg); await pg.waitForTimeout(500); }
        const h = await heads(pg), i = h.findIndex(x => x === 'Z к КБД');
        assert.ok(i > 0, name + ': нет «Z к КБД» среди ' + h.filter(x => /Z|Спред/.test(x)));
        assert.strictEqual(h[i + 1], 'Z к RUONIA', name);
        assert.ok(h.includes('Спред') && h.indexOf('Спред') < i, name + ': «Спред» перед «Z к КБД»');
        assert.ok(await noHScroll(pg), name + ': горизонтальная прокрутка страницы');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    for (const width of [1024, 390]) await t.test('карточка позиции (KIT.card): «Z-спред к КБД» и «Z-спред к RUONIA», ' + width + ' px', async () => {
      const { ctx, pg, errors } = await open(browser, width, 'c03-blotter');
      try {
        await pg.waitForSelector('.k-tbl tbody tr');
        // рублёвая фикс-бумага: в демо — любая, у которой в строке есть оба числа Z
        const id = await pg.evaluate(() => { const p = MOCK.positions.find(p => p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix' && p.zKbd != null && p.zRuonia != null); return p && p.id; });
        assert.ok(id);
        await pg.evaluate(id => KIT.card(id), id);
        await pg.waitForTimeout(400);
        const txt = await pg.locator('body').innerText();
        assert.ok(txt.includes('Z-спред к КБД') && txt.includes('Z-спред к RUONIA'));
        assert.ok(await noHScroll(pg));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
