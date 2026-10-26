// C03 «Позиции» в браузере (Playwright, #326, ревью PR #316): CSV таблицы со свёрнутыми группами — все позиции, как у
// развёрнутой (#277). Группировка «валюта → эмитент»; CSV побайтно равен CSV развёрнутой таблицы, когда свёрнуто всё,
// одна группа уровня l1, одна группа уровня l0, в формате отрицательных «−1» и при поиске (с тем же поиском). Строки
// свёрнутых групп в CSV есть, на экране — нет (display: none, высота 0); в подвале — число всех позиций. Ошибок консоли
// нет ни в тёмной, ни в светлой теме. Страница открывается как файл (file://), данные — демо-набор.
//   node --test tests/js/c03_csv_groups.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c03-blotter.html')).href;

// состояние таблицы после перерисовки: CSV (через ручку K.tableTools), строки на экране и спрятанные, подвал
const state = pg => pg.evaluate(() => {
  const t = document.querySelector('#tbl table'), hid = [...t.querySelectorAll('tbody tr.csvrow')];
  return {
    csv: t.__kt.csv(), rows: t.querySelectorAll('tbody tr.row').length, hidden: hid.length,
    hiddenShown: hid.filter(tr => tr.offsetHeight !== 0 || getComputedStyle(tr).display !== 'none').length,
    groups: t.querySelectorAll('tbody tr.grp').length, foot: (t.querySelector('tfoot') || {}).textContent || '',
  };
});
// действие перерисовывает таблицу: ждём новую таблицу с ручкой CSV, а не таймаут
async function redraw(pg, act) {
  await pg.evaluate(() => { const t = document.querySelector('#tbl table'); if (t) t.dataset.old = '1'; });
  await act();
  await pg.waitForFunction(() => { const t = document.querySelector('#tbl table'); return t && !t.dataset.old && t.__kt; });
  return state(pg);
}
const footN = s => { const m = /(\d+)\s*поз\./.exec(s.foot); return m ? +m[1] : null; };

for (const theme of ['dark', 'light']) {
  test('C03: CSV со свёрнутыми группами равен CSV развёрнутой таблицы, тема ' + theme, { skip: !pw && 'нет Playwright' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
      await ctx.addInitScript(t => { try { localStorage.setItem('concepts-theme', t); localStorage.removeItem('c03-state'); } catch (e) { } }, theme);
      const pg = await ctx.newPage(), errors = [];
      pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      await pg.goto(PAGE);
      await pg.waitForFunction(() => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt);
      assert.strictEqual(await pg.evaluate(() => KIT.theme()), theme);

      const full = await redraw(pg, () => pg.click('#sGrp button:has-text("валюта → эмитент")'));
      assert.ok(full.groups > 2 && full.rows > 10, 'группы и позиции на экране');
      assert.strictEqual(full.hidden, 0);
      const n = full.rows;
      assert.strictEqual(footN(full), n, 'подвал: число позиций');
      const same = (s, what) => {
        assert.strictEqual(s.csv, full.csv, what + ': CSV как у развёрнутой');
        assert.strictEqual(s.rows + s.hidden, n, what + ': все позиции в таблице');
        assert.strictEqual(s.hiddenShown, 0, what + ': спрятанные строки не видны');
        assert.strictEqual(footN(s), n, what + ': подвал');
      };

      const all = await redraw(pg, () => pg.click('#sub [data-a=collapse]'));
      assert.ok(all.hidden > 0 && all.rows === 0, 'свёрнуто всё: позиций на экране нет');
      same(all, 'свёрнуто всё');
      await redraw(pg, () => pg.click('#sub [data-a=expand]'));
      const l1 = await redraw(pg, () => pg.click('#tbl tr.grp.l1 >> nth=2'));
      assert.ok(l1.hidden > 0 && l1.rows > 0);
      same(l1, 'свёрнута группа l1');
      await redraw(pg, () => pg.click('#sub [data-a=expand]'));
      const l0 = await redraw(pg, () => pg.click('#tbl tr.grp.l0 >> nth=0'));
      assert.ok(l0.hidden > 0 && l0.rows > 0);
      same(l0, 'свёрнута группа l0');

      // формат отрицательных на экране CSV не меняет
      const minus = await redraw(pg, () => pg.evaluate(() => KIT.setNegFmt('minus')));
      same(minus, '«−1», свёрнута группа l0');
      await redraw(pg, () => pg.click('#sub [data-a=collapse]'));
      same(await state(pg), '«−1», свёрнуто всё');
      await redraw(pg, () => pg.evaluate(() => KIT.setNegFmt('acc')));
      same(await state(pg), '«(1)», свёрнуто всё');

      // поиск: свёрнутая таблица — те же строки, что развёрнутая с тем же поиском
      await redraw(pg, () => pg.click('#sub [data-a=expand]'));
      const q = await redraw(pg, () => pg.fill('#q', 'ОФЗ'));
      assert.ok(q.rows > 0 && q.rows < n, 'поиск «ОФЗ» сужает таблицу');
      assert.strictEqual(footN(q), q.rows);
      const qc = await redraw(pg, () => pg.click('#sub [data-a=collapse]'));
      assert.strictEqual(qc.csv, q.csv, 'поиск: CSV свёрнутой равен CSV развёрнутой');
      assert.strictEqual(qc.rows + qc.hidden, q.rows);
      assert.strictEqual(qc.hiddenShown, 0);
      assert.strictEqual(footN(qc), q.rows);

      assert.deepStrictEqual(errors, []);
      await ctx.close();
    } finally {
      await browser.close();
    }
  });
}
