// TDM#786 в браузере (Playwright, демо-данные): на экранах с таблицей позиций рядом со «Спред» — «Z к КБД» и «Z к RUONIA»,
// страница без горизонтальной прокрутки на 1024 и 390 px, ошибок страницы нет; карточка позиции показывает обе строки;
// C08: таблица «Оферты и коллы» — заголовки, ячейки и CSV; C09: подсказка точки «наши» на карте доходностей.
//   node --test tests/js/spreads_786.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
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
        // кнопка «Колонки ▾» (скрыть колонку галочкой) есть у таблиц K.table; таблица «Оферты и коллы» на C08 ручная — кнопки нет
        const colBtns = await pg.locator('.k-colbtn').count();
        if (page === 'c08-calendar') assert.strictEqual(await pg.locator('.bd:has(table[data-vk="c08-offers-calls"]) .k-colbtn').count(), 0, name);
        else assert.ok(colBtns > 0, name + ': нет кнопки «Колонки ▾»');
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

test('C08: таблица «Оферты и коллы» — «Спред», «Z к КБД», «Z к RUONIA» (заголовки, ячейки по строкам позиций, CSV с единицей)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, 1024, 'c08-calendar');
    try {
      await pg.waitForSelector('table[data-vk="c08-offers-calls"] tbody tr[data-card]');
      const r = await pg.evaluate(() => {
        const t = document.querySelector('table[data-vk="c08-offers-calls"]');
        const ths = [...t.tHead.rows[0].cells].map(c => c.textContent.trim());
        const rows = [...t.tBodies[0].rows].filter(tr => tr.dataset.card).map(tr => {
          const p = MOCK.positions.find(x => x.id === tr.dataset.card), c = [...tr.cells].map(x => x.textContent.trim());
          return { c, zspread: p.zspread, zKbd: p.zKbd, zRuonia: p.zRuonia };
        });
        return { ths, rows, csv: KIT.tableCsv(t) };
      });
      const i = r.ths.indexOf('Спред');
      assert.ok(i > 0 && r.ths[i + 1] === 'Z к КБД' && r.ths[i + 2] === 'Z к RUONIA', r.ths.join('|'));
      assert.ok(r.rows.length > 0);
      // ячейка — число поля позиции (разряды через запятую) или «—», когда значения нет
      const shown = v => v == null ? '—' : String(Math.round(v)).replace('-', '−').replace(/\B(?=(\d{3})+(?!\d))/g, ',');
      const digits = x => x.replace(/[^\d]/g, ''), norm = (cell, v) => v == null ? cell === '—' : digits(cell) === digits(shown(v));
      for (const row of r.rows) {
        assert.ok(norm(row.c[i], row.zspread), 'Спред: ' + row.c[i] + ' / ' + row.zspread);
        assert.ok(norm(row.c[i + 1], row.zKbd), 'Z к КБД: ' + row.c[i + 1] + ' / ' + row.zKbd);
        assert.ok(norm(row.c[i + 2], row.zRuonia), 'Z к RUONIA: ' + row.c[i + 2] + ' / ' + row.zRuonia);
      }
      assert.ok(r.rows.some(x => x.zKbd != null || x.zRuonia != null), 'в демо есть оферта с Z');
      // CSV: единица в заголовках трёх колонок
      const head = r.csv.replace(/^﻿/, '').split(/\r?\n/).find(l => /Z к КБД/.test(l));
      for (const h of ['Спред, б.п.', 'Z к КБД, б.п.', 'Z к RUONIA, б.п.']) assert.ok(head.includes(h), h + ' в ' + head);
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('C09: подсказка точки «наши» на карте доходностей — «Z к КБД» и «Z к RUONIA» рядом со спредом', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, 1024, 'c09-market');
    try {
      await pg.waitForSelector('#ymap canvas');
      const tips = await pg.evaluate(() => {
        const ch = echarts.getInstanceByDom(document.getElementById('ymap')), o = ch.getOption();
        const fm = (Array.isArray(o.tooltip) ? o.tooltip[0] : o.tooltip).formatter, n = o.series.find(s => s.name === 'наши').data.length;
        return Array.from({ length: n }, (_, i) => fm({ seriesName: 'наши', data: { i } }));
      });
      assert.ok(tips.length > 0);
      for (const t of tips) assert.ok(/Z к КБД [^<]*· Z к RUONIA [^<]*б\.п\./.test(t), t);
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
