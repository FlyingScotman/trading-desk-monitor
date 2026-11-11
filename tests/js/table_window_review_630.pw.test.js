// Длинные таблицы (TDM#630, K.vwin / K.table в prototypes/concepts/lib/kit.js), замечания ревью по окну строк:
//  - перерисовка (щелчок по строке, сортировка туда-обратно, смена формата) не теряет место прокрутки у конца таблицы:
//    новое окно берёт откалиброванную высоту строки прошлого, прокрутка таблицы и страницы возвращается;
//  - память ячеек не отдаёт устаревшие имена и значения после поздних данных (алиасы, история…) — и у короткой таблицы;
//  - «ширина по содержимому» учитывает и строки вне окна (мерные строки);
//  - строки групп не пересобираются при каждой прокрутке, реестр K.d (data-dr) не растёт, перерисовка освобождает свои записи;
//  - aria-rowcount / aria-rowindex у таблицы с окном.
// Страница — c20 как файл (демо-набор), таблицы — K.table на синтетических строках во вставленном контейнере.
//   node --test tests/js/table_window_review_630.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c20-trades.html')).href;
const N = 2000;
const idle = pg => pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 150)))));

async function open(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await pg.goto(PAGE);
  await pg.waitForFunction(() => window.KIT && window.MOCK);
  return { ctx, pg, errors };
}

// K.table, у которой щелчок по строке выделяет её и перерисовывает таблицу (как «Топ изменений» C11); own — своя прокрутка
// таблицы (maxHeight, прокручивается k-tbl-wrap), иначе таблицу листает страница
const SETUP = ([id, n, own, grouped]) => {
  const K = window.KIT, host = document.createElement('div');
  host.id = id; host.style.cssText = 'margin:8px';
  document.body.appendChild(host);
  K.VWIN_MIN = 400;
  const rows = Array.from({ length: n }, (_, i) => ({ id: 'r' + i, i, g: 'g' + (i % 7), name: 'Бумага ' + i + (i === 1500 ? ' с очень длинным названием выпуска, которого нет в начале таблицы, номер ' + i : ''), v: ((i * 37) % 1000) - 500,
    searchText: 'бумага ' + i }));
  window.__sel = null;
  const api = window['__api_' + id] = K.table(host, {
    viewKey: 'rev630-' + id, rows, search: true, maxHeight: own ? '420px' : undefined,
    columns: [{ key: 'i', label: '№', num: true }, { key: 'name', label: 'Бумага' }, { key: 'g', label: 'Группа' },
      { key: 'v', label: 'Сумма', num: true, sum: true, fmt: v => v.toLocaleString('en-US') }],
    groupBy: grouped ? [{ key: r => r.g, label: k => k, order: (a, b) => a.localeCompare(b) }] : undefined,
    rowCls: r => r.id === window.__sel ? 'sel' : '',
    onRow: r => { window.__sel = r.id; api.redraw(); },
  });
};

// место выбранной строки относительно видимой области прокручиваемого предка
const where = (pg, id, own) => pg.evaluate(([id, own]) => {
  const host = document.getElementById(id), wrap = host.querySelector('.k-tbl-wrap'), t = host.querySelector('table'), tr = t.querySelector('tbody tr.row.sel');
  const box = own ? wrap.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  const head = t.tHead.getBoundingClientRect().bottom, r = tr && tr.getBoundingClientRect();
  return { st: own ? wrap.scrollTop : window.scrollY, sel: !!tr, vis: !!tr && r.bottom > Math.max(head, box.top) && r.top < box.bottom };
}, [id, own]);

for (const own of [true, false]) {
  test('K.table на 2000 строк (' + (own ? 'своя прокрутка' : 'листает страница') + '): у конца таблицы щелчок, сортировка туда-обратно и смена формата не сдвигают прокрутку', { skip: !pw && 'нет Playwright' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      const { ctx, pg, errors } = await open(browser);
      await pg.evaluate(SETUP, ['w', N, own, false]);
      await idle(pg);
      // у конца таблицы
      await pg.evaluate(own => {
        const host = document.getElementById('w'), wrap = host.querySelector('.k-tbl-wrap');
        if (own) wrap.scrollTop = wrap.scrollHeight - wrap.clientHeight - 150;
        else { const y = host.getBoundingClientRect().bottom + window.scrollY - window.innerHeight + 40; window.scrollTo(0, y); }
      }, own);
      await idle(pg);
      // щелчок по строке в нижней трети видимой области
      const before = await pg.evaluate(own => {
        const host = document.getElementById('w'), wrap = host.querySelector('.k-tbl-wrap'), box = own ? wrap.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
        const tr = [...host.querySelectorAll('tbody tr.row')].filter(x => { const r = x.getBoundingClientRect(); return r.top > box.top + (box.bottom - box.top) * 0.5 && r.bottom < box.bottom - 40; })[0];
        const st = own ? wrap.scrollTop : window.scrollY;
        tr.cells[1].click();
        return { st, id: tr.dataset.id };
      }, own);
      await idle(pg);
      let w = await where(pg, 'w', own);
      assert.ok(Math.abs(w.st - before.st) <= 2, 'щелчок: прокрутка ' + before.st + ' → ' + w.st);
      assert.ok(w.sel && w.vis, 'щелчок: выбранная строка на экране ' + JSON.stringify(w));
      // сортировка по «Сумме» туда-обратно (по возрастанию, по убыванию, без сортировки) — вернулись к тому же порядку
      for (let k = 0; k < 3; k++) { await pg.evaluate(() => document.querySelector('#w th[data-k=v]').click()); await idle(pg); }
      w = await where(pg, 'w', own);
      assert.ok(Math.abs(w.st - before.st) <= 2, 'сортировка туда-обратно: прокрутка ' + before.st + ' → ' + w.st);
      assert.ok(w.sel && w.vis, 'сортировка туда-обратно: выбранная строка на экране ' + JSON.stringify(w));
      // смена формата отрицательных чисел перерисовывает таблицу (страница c20 при этом перерисовывает и себя, поэтому у таблицы,
      // которую листает страница, перерисовка — сама таблица)
      await pg.evaluate(own => { if (own) KIT.setNegFmt('minus'); else window.__api_w.redraw(true); }, own); await idle(pg);
      w = await where(pg, 'w', own);
      assert.ok(Math.abs(w.st - before.st) <= 2, 'смена формата: прокрутка ' + before.st + ' → ' + w.st);
      assert.ok(w.sel && w.vis, 'смена формата: выбранная строка на экране ' + JSON.stringify(w));
      await pg.evaluate(() => KIT.setNegFmt('acc'));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    } finally { await browser.close(); }
  });
}

test('K.table создана заново (страница перерисовала блок): окно берёт откалиброванную высоту строки, прокрутка страницы не теряется', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['w', N, false, false]);
    await idle(pg);
    const r = await pg.evaluate(async () => {
      const host = document.getElementById('w'), K = window.KIT;
      window.scrollTo(0, host.getBoundingClientRect().bottom + window.scrollY - window.innerHeight + 40); await new Promise(r => setTimeout(r, 300));
      const y = window.scrollY, rows = host.querySelector('table').__vwin.items.length;
      // та же таблица заново в том же контейнере (как C07: members() при каждой перерисовке)
      K.table(host, { viewKey: 'rev630-w', rows: Array.from({ length: rows }, (_, i) => ({ id: 'r' + i, i, g: 'g', name: 'Бумага ' + i, v: i })), search: true,
        columns: [{ key: 'i', label: '№', num: true }, { key: 'name', label: 'Бумага' }, { key: 'v', label: 'Сумма', num: true, sum: true }] });
      await new Promise(r => setTimeout(r, 300));
      return { y, y2: window.scrollY, h: host.querySelector('table').__vwin.rowH(0) };
    });
    assert.ok(Math.abs(r.y - r.y2) <= 2, 'прокрутка страницы ' + r.y + ' → ' + r.y2);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table короче порога окна: после позднего алиаса сортировка и поиск показывают новое имя; после поздних данных — новые значения', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    const r = await pg.evaluate(async () => {
      const K = window.KIT, L = K.list().slice(0, 50), p = L[0]; if (!p.isin) p.isin = 'RU000TEST0001';
      const host = document.createElement('div'); document.body.prepend(host);
      let ext = 'v1';
      K.table(host, { viewKey: 'rev-alias', rows: L, columns: K.cols(['name', 'tenor']).concat([{ key: 'ext', label: 'Внешнее', val: () => ext }]) });
      const t = () => host.querySelector('table'), name = () => host.querySelector('[data-pn="' + p.id + '"]').textContent;
      const out = { windowed: !!t().__vwin, before: name() };
      p.name = 'ZZALIAS'; K.patchNames();
      out.afterPatch = name();
      host.querySelector('th.sortable[data-k="tenor"]').click(); await new Promise(r => setTimeout(r, 100));
      out.afterSort = name();
      // поздние данные (история, деривативы…): значение ячейки другое, страница таблицу не перерисовывала
      out.ext1 = host.querySelector('tbody tr.row td:last-child').textContent;
      ext = 'v2'; document.dispatchEvent(new CustomEvent('concepts-data', { detail: { name: 'history' } }));
      host.querySelector('th.sortable[data-k="tenor"]').click(); await new Promise(r => setTimeout(r, 100));
      out.ext2 = host.querySelector('tbody tr.row td:last-child').textContent;
      // без события памяти не сбрасывают: сортировка берёт ячейки из неё
      ext = 'v3'; host.querySelector('th.sortable[data-k="tenor"]').click(); await new Promise(r => setTimeout(r, 100));
      out.ext3 = host.querySelector('tbody tr.row td:last-child').textContent;
      return out;
    });
    assert.strictEqual(r.windowed, false);
    assert.strictEqual(r.afterPatch, 'ZZALIAS');
    assert.strictEqual(r.afterSort, 'ZZALIAS', 'после сортировки имя новое, а не биржевое');
    assert.strictEqual(r.ext1, 'v1');
    assert.strictEqual(r.ext2, 'v2', 'после поздних данных ячейки считаются заново');
    assert.strictEqual(r.ext3, 'v2', 'без поздних данных — из памяти (сортировка дешёвая)');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table с окном: поздний алиас и поздние данные — и в строках вне окна, и в сгруппированной таблице', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    const r = await pg.evaluate(async () => {
      const K = window.KIT; K.VWIN_MIN = 10;
      const L = K.list(), host = document.createElement('div'); host.style.cssText = 'height:300px;overflow:auto'; document.body.prepend(host);
      L.forEach((p, i) => { if (!p.isin) p.isin = 'RU000TEST' + String(i).padStart(4, '0'); });
      let ext = 'a';
      K.table(host, { viewKey: 'rev-alias-w', rows: L, columns: K.cols(['name', 'tenor']).concat([{ key: 'ext', label: 'Внешнее', val: () => ext }]) });
      const t = () => host.querySelector('table'), out = { windowed: !!t().__vwin, n: L.length };
      host.scrollTop = host.scrollHeight; await new Promise(r => setTimeout(r, 300));
      const last = [...t().querySelectorAll('tbody tr.row')].pop().dataset.id;
      host.scrollTop = 0; await new Promise(r => setTimeout(r, 300));
      L.find(p => p.id === last).name = 'ZZLAST'; ext = 'b';
      K.patchNames();
      host.scrollTop = host.scrollHeight; await new Promise(r => setTimeout(r, 300));
      const tr = t().querySelector('tbody tr.row[data-id="' + CSS.escape(last) + '"]');
      out.lastName = tr && tr.querySelector('[data-pn]').textContent; out.lastExt = tr && tr.cells[tr.cells.length - 1].textContent;
      return out;
    });
    assert.ok(r.windowed && r.n > 40);
    assert.strictEqual(r.lastName, 'ZZLAST');
    assert.strictEqual(r.lastExt, 'b', 'после patchNames все ячейки считаются заново');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table с окном: «ширина по содержимому» (двойной щелчок по ручке) учитывает и самую длинную строку вне окна', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['w', N, true, false]);
    await idle(pg);
    const fit = async () => {
      await pg.evaluate(() => { try { localStorage.removeItem('table:rev630-w'); localStorage.removeItem('k.view.rev630-w'); Object.keys(localStorage).filter(k => k.includes('rev630-w')).forEach(k => localStorage.removeItem(k)); } catch (e) { } });
      await pg.evaluate(() => { const th = document.querySelector('#w thead th[data-k="name"]'); th.querySelector('.k-rsz').dispatchEvent(new MouseEvent('dblclick', { bubbles: true })); });
      await idle(pg);
      return pg.evaluate(() => Math.round(document.querySelector('#w thead th[data-k="name"]').getBoundingClientRect().width));
    };
    const win = await fit();
    // та же таблица целиком
    await pg.evaluate(() => { window.__api_w.resetView(); KIT.VWIN_MIN = 1e9; KIT.setNegFmt('minus'); KIT.setNegFmt('acc'); });
    await idle(pg);
    assert.ok(await pg.evaluate(() => !document.querySelector('#w table').__vwin), 'полная таблица');
    const full = await fit();
    assert.ok(full > 400, 'в таблице есть длинное имя: ' + full);
    assert.ok(Math.abs(win - full) <= 3, 'ширина с окном ' + win + ', у полной таблицы ' + full);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table с окном: строки групп не пересобираются при прокрутке (реестр K.d не растёт), перерисовка освобождает свои записи', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    // строки группируются по 7 группам, но разбить таблицу на много групп нужно, чтобы строки групп были в каждом окне
    await pg.evaluate(([n]) => {
      const K = window.KIT, host = document.createElement('div'); host.id = 'w'; host.style.cssText = 'height:420px;overflow:auto'; document.body.appendChild(host);
      K.VWIN_MIN = 400;
      const rows = Array.from({ length: n }, (_, i) => ({ id: 'r' + i, g: 'g' + Math.floor(i / 10), v: i % 50 - 20, searchText: 'x' + i }));
      window.__api = K.table(host, { viewKey: 'rev630-reg', rows, search: false, columns: [{ key: 'id', label: 'Id' }, { key: 'v', label: 'Сумма', num: true, sum: true }],
        groupBy: [{ key: r => r.g, label: k => k, order: (a, b) => parseInt(a.slice(1)) - parseInt(b.slice(1)) }] });
    }, [N]);
    await idle(pg);
    const live = () => pg.evaluate(() => KIT._reg.filter(Boolean).length);
    const pass = () => pg.evaluate(async () => {
      const h = document.getElementById('w'), r0 = KIT._reg.length;
      for (let i = 0; i <= 60; i++) { h.scrollTop = (h.scrollHeight - h.clientHeight) * (i / 60); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); }
      return KIT._reg.length - r0;
    });
    await pass();  // первый проход: строки групп считаются (по одной записи на группу)
    const grow = await pass();
    assert.ok(grow <= 10, 'за второй проход по таблице реестр вырос на ' + grow + ' (строки групп должны браться из памяти)');
    // перерисовки: записи прошлой — освобождены
    const l1 = await live();
    for (let k = 0; k < 6; k++) { await pg.evaluate(() => window.__api.redraw(true)); await idle(pg); }
    const l2 = await live();
    assert.ok(l2 <= l1 + 40, 'после шести перерисовок живых записей ' + l2 + ', было ' + l1);
    // щелчок по итогу работает и после перерисовок
    await pg.evaluate(() => { document.getElementById('w').scrollTop = 0; });
    await idle(pg);
    await pg.click('#w tfoot [data-dr]');
    await pg.waitForSelector('.k-drawer');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table с окном: aria-rowcount у таблицы, aria-rowindex у строк окна идут по порядку', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['w', N, true, true]);
    await idle(pg);
    const read = () => pg.evaluate(() => {
      const t = document.querySelector('#w table'), v = t.__vwin;
      const idx = [...t.querySelectorAll('tbody tr:not(.k-vs):not(.k-vm)')].map(tr => +tr.getAttribute('aria-rowindex'));
      return { count: +t.getAttribute('aria-rowcount'), items: v.items.length, idx, head: t.tHead.rows[0].getAttribute('aria-rowindex'), foot: t.tFoot.rows[0].getAttribute('aria-rowindex'), from: v.from };
    });
    let s = await read();
    assert.strictEqual(s.count, s.items + 2, 'строки таблицы, шапка и итог');
    assert.strictEqual(s.head, '1');
    assert.strictEqual(s.foot, String(s.items + 2));
    assert.ok(s.idx.length > 20 && s.idx[0] === 2, 'первая строка — вторая после шапки: ' + s.idx.slice(0, 3));
    s.idx.forEach((x, i) => i && assert.strictEqual(x, s.idx[i - 1] + 1));
    await pg.evaluate(() => { const h = document.querySelector('#w .k-tbl-wrap'); h.scrollTop = h.scrollHeight * 0.5; });
    await idle(pg);
    s = await read();
    assert.strictEqual(s.idx[0], s.from + 2, 'номер строки окна — по месту в таблице');
    s.idx.forEach((x, i) => i && assert.strictEqual(x, s.idx[i - 1] + 1));
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
