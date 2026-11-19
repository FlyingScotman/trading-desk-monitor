// TDM#776: колонка «Кор. имя» (C03 в наборах «Базовый», «Статика», «Всё»; кокпит C01 после «Бумага») и строка «Короткое имя» в
// карточке позиции: ✎ — своё короткое имя по ISIN (PUT /api/v1/instruments/short-name), ⟲ — история; новое имя встаёт в уже
// нарисованные таблицы без перезагрузки; поиск по таблице находит и по короткому имени. Страницы как файлы, API — заглушки.
//   node --test tests/js/short_name_776.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const json = (route, body, status) => route.fulfill({ status: status || 200, contentType: 'application/json', body: JSON.stringify(body) });
const TRADER = { enabled: true, user: { username: 'tom', role: 'trader', display_name: null, must_change_password: false }, setup_needed: false, setup_allowed: false, loopback: false,
  secure: false, plain_http_lan: false, admin_from_network: false, roles: { viewer: 'просмотр', trader: 'трейдер', admin: 'администратор' } };

// прогон с биржевыми короткими именами у всех облигаций: БИРЖ-<последние 4 знака ISIN>; кроме облигаций без имени (none)
function rundata() {
  const D = JSON.parse(RUN);
  const bonds = D.positions.filter(p => p.isin && p.instrType === 'BOND'), none = bonds[bonds.length - 1];
  D.positions.forEach(p => { if (p.isin && p.instrType === 'BOND' && p.isin !== none.isin) p.extra = Object.assign({}, p.extra, { short_name: 'БИРЖ-' + p.isin.slice(-4) }); });
  return { D, none };
}
// две первые строки таблицы с ISIN (в порядке страницы, они в окне таблицы): { id, isin, exch }
const firstTwo = pg => pg.evaluate(() => { const L = MOCK.positions.filter(p => p.isin && p.exchShort); return [L[0], L.find(p => p.isin !== L[0].isin)].map(p => ({ id: p.id, isin: p.isin, exch: p.exchShort })); });
// short-names отвечает, когда тест отпустил (release({isin: имя})), с hold: false — сразу пустым
async function open(browser, page, { D, hold = false, store = {} }) {
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], puts = [], hist = [];
  let own = {}, gate;
  const opened = new Promise(r => { gate = r; });
  if (!hold) gate();
  pg.on('pageerror', e => errors.push(e.message));
  await pg.addInitScript(s => { try { Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, store);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url()), req = route.request();
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify({ notes: [] }) + ';' });
    if (u.pathname === '/api/v1/auth/state') return json(route, TRADER);
    if (u.pathname === '/api/v1/instruments/aliases') return json(route, { aliases: [] });
    if (u.pathname === '/api/v1/instruments/short-names') return opened.then(() => json(route, { short_names: Object.entries(own).map(([isin, short_name]) => ({ isin, short_name, changed_by: 'boss', changed_at: '2026-11-10T09:00:00+00:00' })) }));
    if (u.pathname === '/api/v1/instruments/short-name') {
      const b = JSON.parse(req.postData() || '{}'); puts.push(b);
      return json(route, { isin: b.isin, short_name: (b.short_name || '').split(/\s+/).filter(Boolean).join(' ') || null, changed_by: 'tom', changed_at: '2026-11-19T10:00:00+00:00', changed: true });
    }
    if (/^\/api\/v1\/instruments\/short-names\/[^/]+\/history$/.test(u.pathname)) {
      hist.push(u.pathname);
      return json(route, { isin: 'X', revisions: [{ rev_id: 2, short_name: null, changed_by: 'tom', changed_at: '2026-11-18T10:00:00+00:00' }, { rev_id: 1, short_name: 'ПРОШЛОЕ', changed_by: 'boss', changed_at: '2026-11-10T09:00:00+00:00' }] });
    }
    if (u.pathname.startsWith('/api/')) return json(route, {}, 404);
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + page);
  await pg.waitForFunction(() => window.MOCK && window.KIT && window.MOCK.nameRights && window.MOCK.nameRights.authKnown && window.MOCK.positions && document.querySelector('table.k-tbl'));
  return { pg, ctx, errors, puts, hist, release: m => { own = m || {}; gate(); } };
}
const heads = pg => pg.evaluate(() => Array.from(document.querySelectorAll('table.k-tbl thead th')).map(th => th.innerText.replace(/[⏷▲▼↑↓\s]+$/g, '').trim()));
const ROW = id => 'table.k-tbl tbody tr[data-id="' + id + '"], table.k-tbl tbody tr[data-card="' + id + '"]';
const rowShown = (pg, id, re) => pg.waitForFunction(([sel, src]) => { const tr = document.querySelector(sel); return tr && new RegExp(src).test(tr.innerText); }, [ROW(id), re.source]);
// ячейка колонки «Кор. имя» в строке позиции: текст и подсказка
const cellOf = (pg, id) => pg.evaluate(sel => {
  const th = Array.from(document.querySelectorAll('table.k-tbl thead th')), i = th.findIndex(x => /^Кор\. имя/.test(x.innerText));
  const tr = document.querySelector(sel);
  if (i < 0 || !tr) return null;
  const td = tr.children[i];
  return { text: td.innerText.trim(), title: (td.querySelector('[title]') || {}).title || null };
}, ROW(id));

test('short names (TDM#776)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('C03: the «Кор. имя» column is in «Базовый» right after «Бумага»; the exchange name, and our own over it with the exchange one in the hint', async () => {
      const { D } = rundata();
      const { pg, ctx, errors, release } = await open(browser, 'c03-blotter.html', { D, hold: true });
      try {
        const h = await heads(pg);
        assert.ok(h.indexOf('Кор. имя') === h.indexOf('Бумага') + 1 && h.indexOf('Бумага') >= 0, h.join(' | '));
        const [a, b] = await firstTwo(pg);
        await rowShown(pg, a.id, new RegExp(a.exch));
        assert.deepStrictEqual(await cellOf(pg, a.id), { text: a.exch, title: null });
        release({ [b.isin]: 'МОЁ-Б' });  // свои имена приходят позже страницы: ячейка меняется на месте
        await rowShown(pg, b.id, /МОЁ-Б/);
        assert.deepStrictEqual(await cellOf(pg, b.id), { text: 'МОЁ-Б', title: 'биржевое короткое имя: ' + b.exch });
        assert.deepStrictEqual(await cellOf(pg, a.id), { text: a.exch, title: null });
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C03: the column is also in «Статика» and «Всё», not in «Риск»', async () => {
      const { D } = rundata();
      for (const [preset, has] of [['stat', true], ['all', true], ['risk', false]]) {
        const { pg, ctx, errors } = await open(browser, 'c03-blotter.html', { D, store: { 'c03-state': JSON.stringify({ v: 2, preset }) } });
        try {
          const h = await heads(pg);
          assert.strictEqual(h.includes('Кор. имя'), has, preset + ': ' + h.join(' | '));
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });

    await t.test('C03: the table search finds by the exchange short name and by our own', async () => {
      const { D } = rundata();
      const { pg, ctx, errors, release } = await open(browser, 'c03-blotter.html', { D, hold: true });
      try {
        const [a, b] = await firstTwo(pg);
        release({ [b.isin]: 'МОЁ-Б' });
        await rowShown(pg, b.id, /МОЁ-Б/);
        const q = async text => {
          return pg.evaluate(text => MOCK.positions.filter(p => p.searchText.includes(text.toLowerCase())).map(p => p.id), text);
        };
        const byExch = await q(a.exch);
        assert.ok(byExch.includes(a.id) && !byExch.includes(b.id), 'по биржевому короткому имени: ' + byExch);
        const byOwn = await q('МОЁ-Б');
        assert.ok(byOwn.includes(b.id) && !byOwn.includes(a.id), 'по своему: ' + byOwn);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('card: «Короткое имя» ✎ saves by ISIN, the new name replaces the cell in the open table without a reload, ⟲ shows the history, «Биржевое» resets', async () => {
      const { D } = rundata();
      const { pg, ctx, errors, puts, hist } = await open(browser, 'c03-blotter.html', { D });
      try {
        const [a] = await firstTwo(pg);
        await pg.evaluate(id => KIT.card(id), a.id);
        const row = await pg.evaluate(() => { const r = document.querySelector('.k-drawer .k-shortrow'); return r ? r.innerText.replace(/\s+/g, ' ').trim() : null; });
        assert.ok(row && row.startsWith('Короткое имя ' + a.exch), row);
        await pg.evaluate(() => { document.querySelector('.k-drawer .k-shortrow [data-al=hist]').click(); });
        await pg.waitForSelector('.k-alias-pop .k-tbl');
        const pop = await pg.evaluate(() => document.querySelector('.k-alias-pop').innerText);
        assert.ok(/История короткого имени/.test(pop) && /ПРОШЛОЕ/.test(pop) && /сброшено — биржевое короткое имя/.test(pop) && pop.includes('биржевое короткое имя: ' + a.exch), pop);
        assert.ok(hist.length === 1 && hist[0].includes(a.isin), hist.join());
        await pg.evaluate(() => {
          document.querySelector('.k-drawer .k-shortrow [data-al=hist]').click();  // закрыть
          document.querySelector('.k-drawer .k-shortrow [data-al=edit]').click();
          document.querySelector('.k-drawer .k-shortrow .k-alias-ed input').value = '  Новое   имя ';
          document.querySelector('.k-drawer .k-shortrow .k-alias-ed [data-al=save]').click();
        });
        await pg.waitForFunction(id => MOCK.pos[id].shortName === 'Новое имя', a.id);
        assert.deepStrictEqual(puts, [{ isin: a.isin, short_name: '  Новое   имя ' }]);
        const after = await pg.evaluate(id => ({ own: MOCK.pos[id].shortOwn, exch: MOCK.pos[id].exchShort, alias: MOCK.pos[id].alias, row: document.querySelector('.k-drawer .k-shortrow').innerText.replace(/\s+/g, ' ') }), a.id);
        assert.strictEqual(after.own, 'Новое имя'); assert.strictEqual(after.exch, a.exch); assert.strictEqual(after.alias, null);
        assert.ok(after.row.includes('Новое имя ' + a.exch), after.row);
        await rowShown(pg, a.id, /Новое имя/);
        assert.deepStrictEqual(await cellOf(pg, a.id), { text: 'Новое имя', title: 'биржевое короткое имя: ' + a.exch });
        await pg.evaluate(() => { document.querySelector('.k-drawer .k-shortrow [data-al=edit]').click(); document.querySelector('.k-drawer .k-shortrow [data-al=clear]').click(); });
        await pg.waitForFunction(([id, ex]) => MOCK.pos[id].shortName === ex, [a.id, a.exch]);
        assert.deepStrictEqual(puts[1], { isin: a.isin, short_name: null });
        await rowShown(pg, a.id, new RegExp(a.exch));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('markup in short names (exchange, own, history) is shown as text in the cell, the card row and the history', async () => {
      const { D } = rundata();
      const EVIL = '<img src=x onerror="window.__xss=1">', OWN = '"><svg onload="window.__xss=2">';
      const isin = D.positions.find(p => p.isin && p.instrType === 'BOND').isin;
      D.positions.forEach(p => { if (p.isin === isin) p.extra = Object.assign({}, p.extra, { short_name: EVIL }); });
      const { pg, ctx, errors, release } = await open(browser, 'c03-blotter.html', { D, hold: true });
      try {
        await pg.route('**/api/v1/instruments/short-names/*/history', route => json(route, { isin, revisions: [
          { rev_id: 2, short_name: OWN, changed_by: '<b onmouseover="window.__xss=3">x</b>', changed_at: '2026-11-18T10:00:00+00:00' },
          { rev_id: 1, short_name: EVIL, changed_by: 'boss', changed_at: '2026-11-10T09:00:00+00:00' }] }));
        const id = await pg.evaluate(i => MOCK.positions.find(p => p.isin === i).id, isin);
        // биржевое (EVIL) — до ответа сервера, свое (OWN) — после
        await rowShown(pg, id, /<img src=x/);
        assert.deepStrictEqual((await cellOf(pg, id)).text, EVIL, 'ячейка: биржевое как текст');
        release({ [isin]: OWN });
        await rowShown(pg, id, /<svg onload/);
        assert.deepStrictEqual(await cellOf(pg, id), { text: OWN, title: 'биржевое короткое имя: ' + EVIL }, 'ячейка: своё как текст, биржевое в подсказке');
        await pg.evaluate(i => KIT.card(i), id);
        const row = await pg.evaluate(() => { const r = document.querySelector('.k-drawer .k-shortrow'); return { text: r.innerText.replace(/\s+/g, ' ').trim(), tags: [...r.querySelectorAll('*')].map(e => e.tagName.toLowerCase()) }; });
        assert.ok(row.text.includes(OWN) && row.text.includes(EVIL), row.text);
        assert.ok(row.tags.every(x => ['span', 'button'].includes(x)), 'в строке карточки только свои теги: ' + row.tags);
        await pg.evaluate(() => { document.querySelector('.k-drawer .k-shortrow [data-al=hist]').click(); });
        await pg.waitForSelector('.k-alias-pop .k-tbl');
        const hist = await pg.evaluate(() => { const e = document.querySelector('.k-alias-pop'); return { text: e.innerText, tags: [...e.querySelectorAll('*')].map(x => x.tagName.toLowerCase()), on: [...e.querySelectorAll('*')].some(x => [...x.attributes].some(a => /^on/.test(a.name))) }; });
        assert.ok(hist.text.includes(OWN) && hist.text.includes(EVIL) && hist.text.includes('<b onmouseover'), hist.text);
        assert.ok(hist.tags.every(x => ['div', 'span', 'table', 'tbody', 'tr', 'td'].includes(x)) && !hist.on, 'в истории только свои теги, без on…: ' + hist.tags);
        assert.strictEqual(await pg.evaluate(() => window.__xss), undefined, 'скрипты из имён не выполнились');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C03 detail panel: the same row with ✎ and ⟲ under the title; a bond without an exchange name shows «—»', async () => {
      const { D, none } = rundata();
      const { pg, ctx, errors } = await open(browser, 'c03-blotter.html', { D, store: { 'c03-state': JSON.stringify({ v: 2, preset: 'base', sel: String(none.id) }) } });  // выбранная позиция — в панели справа
      try {
        await pg.waitForFunction(() => document.querySelector('.cd-hd .k-shortrow'));
        const r = await pg.evaluate(() => { const e = document.querySelector('.cd-hd .k-shortrow'); return { t: e.innerText.replace(/\s+/g, ' ').trim(), edit: !!e.querySelector('[data-al=edit]'), hist: !!e.querySelector('[data-al=hist]') }; });
        assert.ok(r.edit && r.hist && /^Короткое имя/.test(r.t), JSON.stringify(r));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C01 cockpit: the «Позиции» table has «Кор. имя» right after «Бумага»', async () => {
      const { D } = rundata();
      const { pg, ctx, errors } = await open(browser, 'c01-cockpit.html', { D, store: { 'c01-tab': 'pos' } });
      try {
        const h = await heads(pg);
        assert.ok(h.indexOf('Кор. имя') === h.indexOf('Бумага') + 1 && h.indexOf('Бумага') >= 0, h.join(' | '));
        const [a] = await firstTwo(pg);
        await rowShown(pg, a.id, new RegExp(a.exch));
        assert.deepStrictEqual(await cellOf(pg, a.id), { text: a.exch, title: null });
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
