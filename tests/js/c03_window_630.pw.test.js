// C03 «Позиции» с окном строк (TDM#630, K.vwin): длинная таблица рисует в DOM только строки около экрана. Порог окна
// (KIT.VWIN_MIN) занижен до 10, чтобы демо-набор (десятки позиций) шёл тем же путём, что две тысячи позиций прогона.
// Проверяется: CSV окна равен CSV полной таблицы (группировки, свёрнутые группы, поиск, формат отрицательных), строка
// выбирается щелчком и стрелками, выбранная строка листается в виду и рисуется, итог и число позиций — по всем строкам.
// Страница открывается как файл (file://), данные — демо-набор.
//   node --test tests/js/c03_window_630.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c03-blotter.html')).href;

const state = pg => pg.evaluate(() => {
  const t = document.querySelector('#tbl table');
  return { csv: t.__kt.csv(), windowed: !!t.__vwin, rows: t.querySelectorAll('tbody tr.row').length, foot: (t.querySelector('tfoot') || {}).textContent || '' };
});
async function redraw(pg, act) {
  await pg.evaluate(() => { const t = document.querySelector('#tbl table'); if (t) t.dataset.old = '1'; });
  await act();
  await pg.waitForFunction(() => { const t = document.querySelector('#tbl table'); return t && !t.dataset.old && t.__kt; });
  return state(pg);
}
const setMin = (pg, v) => pg.evaluate(v => { KIT.VWIN_MIN = v; }, v);

test('C03 с окном строк: CSV и подвал как у полной таблицы, выбор строки, стрелки, прокрутка к выбранной', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 700 } });
    await ctx.addInitScript(() => { try { localStorage.setItem('concepts-theme', 'dark'); localStorage.removeItem('c03-state'); } catch (e) { } });
    const pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    await pg.goto(PAGE);
    await pg.waitForFunction(() => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt);

    // сравнение одного состояния в обоих режимах: нажатие кнопки перерисовывает таблицу
    const compare = async (what, act) => {
      await setMin(pg, 1e9);
      const full = await redraw(pg, act);
      await setMin(pg, 10);
      const win = await redraw(pg, async () => { await pg.evaluate(() => document.querySelector('#sCols button.on, #sCols button').click()); });
      assert.ok(!full.windowed && win.windowed, what + ': режимы');
      assert.strictEqual(win.csv, full.csv, what + ': CSV');
      assert.strictEqual(win.foot, full.foot, what + ': подвал');
      assert.ok(win.rows <= full.rows, what + ': в окне не больше строк, чем в таблице');
      return win;
    };
    const flat = await compare('без групп', () => pg.click('#sGrp button:has-text("без групп")'));
    assert.ok(flat.rows < 80, 'в окне только часть позиций: ' + flat.rows);
    await compare('валюта → эмитент', () => pg.click('#sGrp button:has-text("валюта → эмитент")'));
    await compare('свёрнуто всё', () => pg.click('#sub [data-a=collapse]'));
    await redraw(pg, () => pg.click('#sub [data-a=expand]'));
    await compare('поиск', async () => { await pg.fill('#q', 'ОФЗ'); });
    await redraw(pg, () => pg.fill('#q', ''));
    await compare('формат «−1»', () => pg.evaluate(() => KIT.setNegFmt('minus')));
    await pg.evaluate(() => KIT.setNegFmt('acc'));

    // выбор: щелчок, стрелки; выбранная строка в окне и в виду
    await setMin(pg, 10);
    await redraw(pg, () => pg.click('#sGrp button:has-text("без групп")'));
    const n = await pg.evaluate(() => document.querySelector('#tbl table').__vwin.items.length);
    assert.ok(n > 40, 'в демо-наборе есть что листать: ' + n);
    await pg.click('#tbl tbody tr.row >> nth=2');
    assert.strictEqual(await pg.evaluate(() => document.querySelectorAll('#tbl tr.row.sel').length), 1);
    for (let i = 0; i < n - 4; i++) await pg.keyboard.press('ArrowDown');
    const s = await pg.evaluate(() => {
      const t = document.getElementById('tbl'), sel = t.querySelector('tr.row.sel'), r = sel && sel.getBoundingClientRect(), c = t.getBoundingClientRect();
      return { sel: !!sel, inView: !!sel && r.top >= c.top - 1 && r.bottom <= c.bottom + 1, id: sel && sel.dataset.id, rows: t.querySelectorAll('tr.row').length };
    });
    assert.ok(s.sel && s.inView, 'выбранная стрелками строка нарисована и в виду: ' + JSON.stringify(s));
    assert.strictEqual(await pg.evaluate(() => document.getElementById('tbl').querySelectorAll('tr.row.sel').length), 1);
    // вверх до начала — первая строка
    for (let i = 0; i < n; i++) await pg.keyboard.press('ArrowUp');
    assert.strictEqual(await pg.evaluate(() => document.querySelector('#tbl tr.row.sel') && document.querySelector('#tbl tr.row.sel') === document.querySelector('#tbl tr.row')), true);
    // двойной щелчок открывает карточку
    await pg.dblclick('#tbl tbody tr.row >> nth=1');
    await pg.waitForSelector('.k-drawer');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally {
    await browser.close();
  }
});

// ---------- замечания ревью ----------
async function openC03(browser, viewport) {
  const ctx = await browser.newContext({ viewport: viewport || { width: 1600, height: 700 } });
  await ctx.addInitScript(() => { try { localStorage.setItem('concepts-theme', 'dark'); localStorage.removeItem('c03-state'); } catch (e) { } });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await pg.goto(PAGE);
  await pg.waitForFunction(() => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt);
  await pg.evaluate(() => { KIT.VWIN_MIN = 10; });
  return { ctx, pg, errors };
}
const settle = pg => pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 200)))));

test('C03 с окном: «свернуть» сворачивает все группы верхнего уровня, а не только те, что в окне; «развернуть» раскрывает все', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await openC03(browser);
    await pg.click('#sGrp button:has-text("книга")');
    await settle(pg);
    const l0 = () => pg.evaluate(() => {
      const v = document.querySelector('#tbl table').__vwin;
      const top = v.items.filter(x => x.k === 'grp' && x.html().includes('grp l0'));
      return { windowed: !!v, groups: top.length, open: top.filter(x => x.html().includes('▾')).length, inWin: document.querySelectorAll('#tbl tr.grp.l0').length, items: v.items.length };
    });
    const a = await l0();
    assert.ok(a.windowed && a.groups > a.inWin + 5, 'групп верхнего уровня больше, чем в окне: ' + JSON.stringify(a));
    assert.strictEqual(a.open, a.groups);
    await pg.click('#sub [data-a=collapse]'); await settle(pg);
    const b = await pg.evaluate(() => { const t = document.querySelector('#tbl table'); return { windowed: !!t.__vwin, grp: t.querySelectorAll('tr.grp.l0').length, open: [...t.querySelectorAll('tr.grp.l0 .tw')].filter(x => x.textContent === '▾').length, rows: t.querySelectorAll('tbody tr.row').length }; });
    const bm = await pg.evaluate(() => { const v = document.querySelector('#tbl table').__vwin; const L = v ? v.items : []; return { items: L.length, open: L.filter(x => x.k === 'grp' && x.html().includes('▾')).length, rows: L.filter(x => x.k === 'row').length }; });
    assert.strictEqual(bm.open, 0, 'свёрнуты все группы, не только видимые: ' + JSON.stringify(bm));
    assert.strictEqual(bm.rows, 0, 'в таблице остались одни строки групп');
    assert.strictEqual(b.open, 0);
    await pg.click('#sub [data-a=expand]'); await settle(pg);
    const c = await l0();
    assert.strictEqual(c.open, c.groups, 'развернуть — все группы развёрнуты');
    assert.strictEqual(c.items, a.items);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C03 с окном: строки групп берутся из памяти при прокрутке (реестр K.d не растёт), перерисовка освобождает свои записи', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await openC03(browser);
    await pg.click('#sGrp button:has-text("книга")');
    await settle(pg);
    const pass = () => pg.evaluate(async () => {
      const h = document.getElementById('tbl'), r0 = KIT._reg.length;
      for (let i = 0; i <= 40; i++) { h.scrollTop = (h.scrollHeight - h.clientHeight) * (i / 40); await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))); }
      return KIT._reg.length - r0;
    });
    await pass();
    const grow = await pass();
    assert.ok(grow <= 5, 'за второй проход реестр K.d вырос на ' + grow);
    // записи прошлой отрисовки освобождены: число живых не растёт от перерисовок
    const live = () => pg.evaluate(() => KIT._reg.filter(Boolean).length);
    await pg.evaluate(() => { document.getElementById('tbl').scrollTop = 0; }); await settle(pg);
    const l1 = await live();
    for (let k = 0; k < 4; k++) { await pg.click('#tbl th.sortable >> nth=2'); await settle(pg); }
    assert.ok((await live()) <= l1 + 40, 'живых записей после перерисовок ' + await live() + ', было ' + l1);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C03 с окном: ячейка «Цена 30д» из памяти строки считается заново после поздней истории', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await openC03(browser);
    await pg.click('#sGrp button:has-text("без групп")'); await settle(pg);
    const idx = await pg.evaluate(() => [...document.querySelectorAll('#tbl thead th')].findIndex(th => th.textContent.includes('Цена 30д')));
    assert.ok(idx > 0, 'в наборе колонок есть «Цена 30д»');
    const cell = () => pg.evaluate(i => document.querySelector('#tbl tbody tr.row').cells[i].innerHTML, idx);
    const before = await cell();
    assert.ok(before.includes('<svg') || before.includes('canvas') || before.length > 20, 'спарклайн нарисован');
    await pg.evaluate(() => {
      const M = window.MOCK, orig = M.posHistory;
      M.posHistory = function (id, o) { const h = orig.call(this, id, o); return Object.assign({}, h, { price: h.price.slice().reverse() }); };
      document.dispatchEvent(new CustomEvent('concepts-data', { detail: { name: 'history' } }));
    });
    // окно заново: прокрутка вниз и назад
    await pg.evaluate(() => { document.getElementById('tbl').scrollTop = document.getElementById('tbl').scrollHeight; }); await settle(pg);
    await pg.evaluate(() => { document.getElementById('tbl').scrollTop = 0; }); await settle(pg);
    const after = await cell();
    assert.notStrictEqual(after, before, 'после поздних данных спарклайн посчитан заново');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
