// TDM#775 в браузере, C03: столбец «Изм. к вчера» в наборе «Базовый» (рост — зелёным со знаком, снижение — красным, «новая» —
// тегом, без изменений — точкой), строка «Закрыто с ДД.ММ …» над таблицей, заголовок CSV «Изм. к вчера, mm в вал.»; «Сделки за
// день» рядом покрашена по знаку. Вымышленные данные, прогон — фикстура tests/js/fixtures/concepts-run.json.
//   node --test tests/js/prev_delta_775.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

async function open(browser, extras) {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, acceptDownloads: true });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(Object.assign({ notes: [] }, extras)) + ';' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  return { pg, ctx, errors };
}

test('#775: C03 «Базовый» — «Изм. к вчера» с цветом и тегом «новая», строка закрытых позиций, заголовок CSV', { skip: pw ? false : 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const D = JSON.parse(RUN);
    const bonds = D.positions.filter(p => p.instrType === 'BOND').slice(0, 5);
    const ids = bonds.map(p => String(p.id)), n = i => bonds[i].notional;
    const mk = (bb, sb) => ({ n: 1, date: '2026-11-16', buy: bb ? { n: 1, counted: 1, priced: 1, bonds: bb, face: bb * 1000, avg: 99 } : undefined,
      sell: sb ? { n: 1, counted: 1, priced: 1, bonds: sb, face: sb * 1000, avg: 101 } : undefined, net: { bonds: bb - sb, face: (bb - sb) * 1000 } });
    const extras = {
      positions: {
        [ids[0]]: { prevNotional: n(0) - 4e6, trade: mk(4000, 0) },        // выросла на 4 mm
        [ids[1]]: { prevNotional: n(1) + 3e6, trade: mk(0, 3000) },        // снизилась на 3 mm
        [ids[2]]: { newToPrev: true },                                      // новая
        [ids[3]]: { prevNotional: n(3) },                                   // без изменений
      },
      prevRun: { runId: 5, date: '2026-11-13', closed: [{ positionId: 'GONE-1', book: bonds[0].book, ccy: 'RUB', name: 'Закрытая бумага', portfolio: null, notional: 150e6 }] },
    };
    const { pg, ctx, errors } = await open(browser, extras);
    try {
      await pg.waitForFunction(x => window.MOCK && MOCK.pos[x], ids[0]);
      await pg.waitForSelector('#tbl table th[data-k]');
      const heads = await pg.locator('#tbl table thead th').allInnerTexts();
      const i = heads.findIndex(h => /Изм\. к вчера/.test(h)), j = heads.findIndex(h => /Сделки за день/.test(h));
      assert.ok(i > 0 && j > 0 && j < i, 'в «Базовом» «Сделки за день», затем «Изм. к вчера»: ' + heads.join('|'));
      const grab = () => pg.locator('#tbl table tbody tr[data-id]').evaluateAll((trs, k) => trs.map(tr => {
        const c = tr.children[k.i], t = tr.children[k.j], first = c.querySelector('span');
        return { id: tr.dataset.id, text: c.textContent.replace(/\s+/g, ' ').trim(), cls: first ? first.className : '', title: first ? first.title : '', tcls: (t.querySelector('span') || {}).className || '' };
      }), { i, j });
      const cells = Object.fromEntries((await grab()).map(c => [c.id, c]));
      assert.ok(/^\+4\.00\s*mm/.test(cells[ids[0]].text) && cells[ids[0]].cls === 'pos', JSON.stringify(cells[ids[0]]));
      assert.ok(/3\.00/.test(cells[ids[1]].text) && !/\+/.test(cells[ids[1]].text) && cells[ids[1]].cls === 'neg', JSON.stringify(cells[ids[1]]));
      assert.ok(/новая$/.test(cells[ids[2]].text) && /Новая позиция: в прогоне за 13\.11\.26 её не было/.test(cells[ids[2]].title), JSON.stringify(cells[ids[2]]));
      assert.strictEqual(cells[ids[3]].text, '·', 'без изменений — точка');
      assert.ok(/В прогоне за 13\.11\.26 номинал был/.test(cells[ids[0]].title), cells[ids[0]].title);
      // «Сделки за день» рядом: куплено — зелёным, продано — красным
      assert.strictEqual(cells[ids[0]].tcls.split(' ')[0], 'pos');
      assert.strictEqual(cells[ids[1]].tcls.split(' ')[0], 'neg');
      // строка закрытых позиций над таблицей (в строке под шапкой экрана), со списком в подсказке
      const sub = (await pg.locator('#sub').innerText()).replace(/\s+/g, ' ');
      assert.ok(/Закрыто с 13\.11: 1 поз\./.test(sub), sub);
      const tip = await pg.locator('#sub .k-dtl[title^="Были в прогоне за 13.11.26"]').getAttribute('title');
      assert.ok(/Закрытая бумага — 150\.00 mm RUB/.test(tip), tip);
      // CSV: заголовок колонки «Изм. к вчера, mm в вал.» и число в строках
      const [d] = await Promise.all([pg.waitForEvent('download'), pg.click('.k-tbl-ctlbar .k-tbtn:has-text("CSV")')]);
      const lines = fs.readFileSync(await d.path(), 'utf8').replace(/^﻿/, '').split(/\r?\n/);
      assert.ok(lines[2].includes('"Изм. к вчера, mm в вал."'), lines[2]);   // поле с запятой — в кавычках
      assert.ok(lines[2].indexOf('"Сделки за день, mm в вал."') < lines[2].indexOf('"Изм. к вчера, mm в вал."'));
      const body = lines.slice(3).find(l => l.startsWith(bonds[0].name) || l.includes(bonds[0].name));
      assert.ok(body && body.includes(',4.00,'), 'в строке бумаги — число 4.00 без единиц: ' + body);
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
