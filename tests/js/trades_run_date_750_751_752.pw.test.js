// TDM#750, #751, #752 в браузере: C20 без даты показывает день прогона — пустым, со ссылкой на последний день со сделками
// («Сделок за 16.11 пока нет; последние — за 13.11»), а не сделки другого дня; в таблице сделок есть «Цена оценки» (у старого
// прогона — «—»); в позициях C03 есть столбец «Сделки за день» (в наборах «Кэрри и PnL» и «Всё»), со знаком, с подсказкой,
// пустой без сделок, с числовой сортировкой, и значение то же, что нетто в блоке карточки. Вымышленные данные.
//   node --test tests/js/trades_run_date_750_751_752.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

const row = (id, o) => Object.assign({ trade_id: id, trade_version: 1, status: 'live', trade_date: '2026-11-13', trade_ts: '2026-11-13T10:15:00', book_id: 'B1', position_id: 'B1|S1',
  asset_class: 'bond', source_instrument_id: 'S1', source_instrument_name: 'Облигация 1', side: 'buy', quantity: 100, price_pct: 99.5, ccy: 'RUB', late: false,
  intraday: 'applied', pnl_rub: 500, pnl_flag: null, mark_pct: 100, mark_origin: 'model', origin: 'feed', received_at: '2026-11-13T07:20:00Z', entity: null, desk: null, portfolio: null }, o || {});
const doc = (date, rows, extra) => Object.assign({ date, book_id: null, rows, books: [], state_text: {}, origin_text: {}, files: [], days: [{ date: '2026-11-13', trades: 2, cancelled: 0 }],
  summary: { trades: rows.length, live: rows.length, cancelled: 0, late: 0, books: 1, versions: 0 }, intraday: { run_id: 7, run_type: 'EOD', cut_type: 'eod_final', applied: rows.length, new_positions: 0, skipped: {} }, earlier: null }, extra || {});

async function open(browser, page, { trade, positions, answer }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], asked = [];
  pg.on('pageerror', e => errors.push(e.message));
  const D = JSON.parse(RUN);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify({ notes: [], positions: positions || {} }) + ';' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname === '/api/v1/trades' && answer) { asked.push(u.search); return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(answer(u.searchParams)) }); }
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + page);
  return { pg, ctx, errors, asked };
}

test('#750/#751: C20 без даты — день прогона пустым со ссылкой на прошлый день; «Цена оценки» в таблице', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    const answer = p => p.get('date') === '2026-11-13'
      ? doc('2026-11-13', [row('TR-1', { mark_origin: 'stale', mark_date: '2026-11-10' }), row('TR-2', { side: 'sell', mark_pct: null, mark_origin: null, pnl_rub: null, intraday: 'pending' })])
      : doc('2026-11-16', [], { earlier: { date: '2026-11-13', trades: 2 }, days: [{ date: '2026-11-13', trades: 2, cancelled: 0 }] });
    const { pg, ctx, errors, asked } = await open(browser, 'c20-trades.html', { answer });
    try {
      await pg.waitForSelector('#earlier');
      const text = (await pg.locator('#view').innerText()).replace(/\s+/g, ' ');
      assert.ok(/Сделок за 16\.11 пока нет; последние — за 13\.11 \(2 сд\.\)/.test(text), text);
      assert.ok(!/TR-1/.test(text), 'сделки другого дня подставлять нельзя');
      assert.strictEqual(asked[0].includes('date='), false, 'без даты страница даты не просит: её выбирает сервер');
      // полоса дат: открытый день без сделок в ней есть, с нулём
      const strip = (await pg.locator('#days').innerText()).replace(/\s+/g, ' ');
      assert.ok(/16 ноя 0 сд\./.test(strip) && /13 ноя 2 сд\./.test(strip), strip);
      // щелчок по ссылке открывает этот день, дата попадает в адрес
      await pg.locator('#earlier').click();
      await pg.waitForSelector('table.k-tbl');
      assert.ok(pg.url().includes('date=2026-11-13'), pg.url());
      assert.ok(asked[asked.length - 1].includes('date=2026-11-13'));
      // «Цена оценки» рядом с «Ценой»; у сделки без оценки — прочерк
      const heads = await pg.locator('table.k-tbl thead th').allInnerTexts();
      const i = heads.findIndex(h => /Цена оценки/.test(h));
      assert.ok(i > 0 && /^Цена/.test(heads[i - 1]) && !/оценки/.test(heads[i - 1]), heads.join('|'));
      const cells = await pg.locator('table.k-tbl tbody tr').evaluateAll((trs, k) => trs.map(tr => tr.children[k].textContent.replace(/\s+/g, ' ').trim()), i);
      assert.deepStrictEqual(cells, ['100.0000 %', '—']);
      assert.ok((await pg.locator('table.k-tbl tbody tr').first().locator('td').nth(i).locator('span[title]').getAttribute('title')).includes('Откуда: последняя цена от 10.11'));
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('#752: столбец «Сделки за день» в позициях C03 — нетто номинала со знаком, подсказка, пусто без сделок, числовая сортировка', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    const D = JSON.parse(RUN);
    const bonds = D.positions.filter(p => p.instrType === 'BOND').slice(0, 4).map(p => String(p.id));
    const mk = (bb, sb) => ({ n: (bb ? 1 : 0) + (sb ? 1 : 0), date: '2026-11-16', buy: bb ? { n: 1, counted: 1, priced: 1, bonds: bb, face: bb * 1000, avg: 99 } : undefined,
      sell: sb ? { n: 1, counted: 1, priced: 1, bonds: sb, face: sb * 1000, avg: 101 } : undefined, net: { bonds: bb - sb, face: (bb - sb) * 1000 } });
    const positions = { [bonds[0]]: { trade: mk(5000, 1000) }, [bonds[1]]: { trade: mk(0, 3000) }, [bonds[2]]: { trade: mk(10000, 0) } };   // +4 mm, −3 mm и +10 mm; у четвёртой сделок нет
    const { pg, ctx, errors } = await open(browser, 'c03-blotter.html', { positions });
    try {
      await pg.waitForFunction(x => window.MOCK && MOCK.pos[x], bonds[0]);
      await pg.evaluate(() => { const b = [...document.querySelectorAll('button, .k-chip')].find(x => x.textContent.trim() === 'Кэрри и PnL'); if (b) b.click(); });
      await pg.waitForSelector('table.k-tbl thead th');
      const heads = await pg.locator('table.k-tbl thead th').allInnerTexts();
      const i = heads.findIndex(h => /Сделки за день/.test(h));
      assert.ok(i >= 0, 'столбец в наборе «Кэрри и PnL»: ' + heads.join('|'));
      const grab = async () => pg.locator('table.k-tbl tbody tr[data-id]').evaluateAll((trs, k) => trs.map(tr => ({ id: tr.dataset.id, text: tr.children[k].textContent.replace(/\s+/g, ' ').trim(), title: (tr.children[k].querySelector('[title]') || {}).title || '' })), i);
      const cells = Object.fromEntries((await grab()).map(c => [c.id, c]));
      assert.ok(/^\+4\.00\s*mm/.test(cells[bonds[0]].text), cells[bonds[0]].text);
      assert.ok(/mm/.test(cells[bonds[1]].text) && /3\.00/.test(cells[bonds[1]].text) && !/\+/.test(cells[bonds[1]].text), cells[bonds[1]].text);
      assert.ok(/Куплено: 5,000 шт\./.test(cells[bonds[0]].title) && /продано: 1,000 шт\./.test(cells[bonds[0]].title) && /Сделок: 2/.test(cells[bonds[0]].title), cells[bonds[0]].title);
      assert.strictEqual(cells[bonds[3]].text, '', 'нет сделок — пусто');
      // сортировка — по числу, а не по тексту: +10.00 больше +4.00 (по тексту «+10» стояло бы перед «+4»), а минус — в конце; без сделок — после всех
      const order = async () => (await grab()).filter(c => [bonds[0], bonds[1], bonds[2], bonds[3]].includes(c.id)).map(c => c.id);
      await pg.locator('table.k-tbl thead th', { hasText: 'Сделки за день' }).first().click();
      await pg.waitForTimeout(150);
      assert.deepStrictEqual(await order(), [bonds[2], bonds[0], bonds[1], bonds[3]]);
      // то же значение, что нетто в блоке карточки
      const net = await pg.evaluate(id => KIT.tradeNet(MOCK.pos[id]).face, bonds[0]);
      assert.strictEqual(net, 4000000);
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
