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
