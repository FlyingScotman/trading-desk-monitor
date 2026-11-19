// TDM#803: «C01 · Кокпит» — в «DV01 ставки» и «Облигации MV» валюты строками столько, сколько помещается по высоте ряда карточек
// (её задаёт самая высокая из остальных карточек ряда, обычно ОВП): больше трёх, высота ряда не выросла, прокрутки и обрезки в карточке нет.
// Проверяется на широком и узком окне, при смене ширины, при развороте «+N прочих» у ОВП и при смене фильтра шапки; K.hud без opt.fitCcy
// (C12, команда hud) — как раньше, три строки. Страницы как файлы, прогон — фикстура.
//   node --test tests/js/c01_hud_ccy_803.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const skip = !pw && 'нет Playwright';
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c01-cockpit.html')).href;

// что видно у HUD: по карточкам с подгонкой — строки валют, число валют с ненулевым значением, переполнение карточки; высота сетки; базовая
// высота — той же шапки без подгонки (K.hud в соседнем блоке той же ширины)
const probe = pg => pg.evaluate(() => {
  const g = document.querySelector('#hud .k-kpis'), cards = [...g.children], k = MOCK.kpis(KIT.list());
  const count = id => MOCK.ccys.filter(c => Math.abs(k.byCcy[c][id] || 0) > 1e-9).length;
  const box = document.createElement('div'); box.style.cssText = 'position:absolute;left:0;top:2000px;width:' + document.getElementById('hud').clientWidth + 'px';
  document.body.appendChild(box); KIT.hud(box);
  const base = box.querySelector('.k-kpis'), baseCards = [...base.children], baseH = base.getBoundingClientRect().height;
  const baseRows = baseCards.map(c => c.querySelectorAll('.k-kb tr').length);
  const out = {
    grid: g.getBoundingClientRect().height, baseGrid: baseH,
    fit: cards.map((c, i) => ({ id: c.dataset.fit || '', rows: c.querySelectorAll('.k-kb tr').length, baseRows: baseRows[i],
      count: c.dataset.fit ? count(c.dataset.fit === 'dv01' ? 'dv01' : 'mv') : null, over: c.scrollHeight - c.clientHeight, top: Math.round(c.getBoundingClientRect().top), h: c.getBoundingClientRect().height,
      more: (c.querySelector('.s') || {}).innerText && /\+\d/.test(c.querySelector('.s').innerText) })),
    baseFit: baseCards.filter(c => c.dataset.fit).length,
  };
  box.remove();
  return out;
});

async function open(browser, width) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.goto(PAGE);
  await pg.waitForFunction(() => window.KIT && document.querySelector('#hud .k-kpis'));
  await pg.evaluate(() => (document.fonts && document.fonts.ready) || null);
  await pg.waitForTimeout(300);
  return { ctx, pg, errors };
}
// валют в карточках больше, чем строк по умолчанию (в фикстуре их пять): подгонка есть что показывать
const FIT = r => r.fit.filter(c => c.id);

test('C01 HUD: DV01 and MV show more currencies on a wide window, the row does not grow, nothing overflows (TDM#803)', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1900, 1600, 1280]) {
      const { ctx, pg, errors } = await open(browser, width);
      try {
        const r = await probe(pg);
        assert.strictEqual(FIT(r).length, 2, 'две карточки с подгонкой');
        assert.ok(FIT(r).every(c => c.baseRows <= 3), 'без подгонки — не больше трёх строк');
        const dv = FIT(r).find(c => c.id === 'dv01');
        assert.ok(dv.count >= 4, 'в фикстуре валют с DV01 больше трёх: ' + dv.count);
        assert.ok(dv.rows > 3, width + ': DV01 показывает больше трёх строк: ' + dv.rows);
        for (const c of FIT(r)) assert.ok(c.rows >= c.baseRows && c.rows <= Math.max(c.count, 3), width + ': ' + c.id + ' строк ' + c.rows + ' из ' + c.count);
        assert.ok(Math.abs(r.grid - r.baseGrid) < 1, width + ': высота ряда не выросла: ' + r.grid + ' против ' + r.baseGrid);
        r.fit.forEach(c => assert.ok(c.over <= 1, width + ': карточка без прокрутки и обрезки: ' + JSON.stringify(c)));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    }
  } finally { await browser.close(); }
});

test('C01 HUD: one more currency would not fit (the fit is the maximum), where the row has other cards', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1600, 1000, 760, 420]) {
      const { ctx, pg, errors } = await open(browser, width);
      try {
        const r = await pg.evaluate(() => {
          const g = document.querySelector('#hud .k-kpis'), cards = [...g.children], k = MOCK.kpis(KIT.list()), res = [];
          cards.forEach(c => { c.style.alignSelf = 'start'; });
          cards.filter(c => c.dataset.fit).forEach(c => {
            const id = c.dataset.fit, top = Math.round(c.getBoundingClientRect().top), s = c.querySelector('.s'), keep = s.innerHTML, rows = c.querySelectorAll('.k-kb tr').length;
            const vals = MOCK.ccys.reduce((o, x) => (o[x] = k.byCcy[x][id], o), {}), fmt = v => KIT.f.dv(v);
            const others = cards.filter(x => !x.dataset.fit && Math.round(x.getBoundingClientRect().top) === top);
            const limit = others.length ? Math.max(...others.map(x => x.getBoundingClientRect().height)) : null;
            const count = MOCK.ccys.filter(x => Math.abs(vals[x] || 0) > 1e-9).length;
            s.innerHTML = KIT.kpiSub(KIT.kpiCcyRows(vals, (x, v) => fmt(v), rows + 1, fmt));
            res.push({ id, rows, count, limit, grown: c.getBoundingClientRect().height, same: c.querySelectorAll('.k-kb tr').length, left: others.length });
            s.innerHTML = keep;
          });
          cards.forEach(c => { c.style.alignSelf = ''; });
          return res;
        });
        assert.strictEqual(r.length, 2);
        for (const c of r) {
          if (c.rows >= c.count || c.rows >= 12) continue;  // показаны все валюты
          assert.ok(c.left > 0 && c.grown > c.limit + 0.5, width + ': ' + c.id + ' — ещё одна строка (' + c.same + ') не помещается: ' + JSON.stringify(c));
        }
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    }
  } finally { await browser.close(); }
});

test('C01 HUD: narrow windows keep the row height too (rows wrap), no overflow', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1000, 760, 420]) {
      const { ctx, pg, errors } = await open(browser, width);
      try {
        const r = await probe(pg);
        assert.ok(Math.abs(r.grid - r.baseGrid) < 1, width + ': высота шапки не выросла: ' + r.grid + ' против ' + r.baseGrid);
        for (const c of r.fit) assert.ok(c.over <= 1, width + ': без обрезки: ' + JSON.stringify(c));
        for (const c of FIT(r)) assert.ok(c.rows >= 3 || c.count < 3, width + ': не меньше трёх строк: ' + JSON.stringify(c));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    }
  } finally { await browser.close(); }
});

test('C01 HUD: the fit follows the window width, «+N прочих» of the OVP card and the header filter', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, 1600);
    try {
      const wide = await probe(pg);
      await pg.setViewportSize({ width: 1000, height: 900 });
      await pg.waitForTimeout(400);
      const narrow = await probe(pg);
      assert.ok(narrow.grid > wide.grid, 'на узком окне карточки переносятся в два ряда');
      assert.strictEqual(FIT(narrow).find(c => c.id === 'dv01').rows, 3, 'ОВП в другом ряду — высота ряда другая, три строки');
      narrow.fit.forEach(c => assert.ok(c.over <= 1, 'узкое окно, без обрезки: ' + JSON.stringify(c)));
      await pg.setViewportSize({ width: 1600, height: 900 });
      await pg.waitForTimeout(400);
      const back = await probe(pg);
      assert.deepStrictEqual(FIT(back).map(c => c.rows), FIT(wide).map(c => c.rows), 'вернули ширину — те же строки');
      assert.ok(Math.abs(back.grid - back.baseGrid) < 1);
      // «+N прочих» ОВП: ряд стал выше — валют в DV01/MV не меньше; свернули — снова столько же, сколько было
      const has = await pg.evaluate(() => !!document.querySelector('#hud [data-fxmore]'));
      if (has) {
        await pg.evaluate(() => document.querySelector('#hud [data-fxmore]').click());
        await pg.waitForTimeout(400);
        const open = await probe(pg);
        FIT(open).forEach((c, i) => assert.ok(c.rows >= FIT(wide)[i].rows, 'ОВП развёрнута — строк не меньше'));
        open.fit.forEach(c => assert.ok(c.over <= 1, 'ОВП развёрнута, без обрезки: ' + JSON.stringify(c)));
        await pg.evaluate(() => document.querySelector('#hud [data-fxmore]').click());
        await pg.waitForTimeout(400);
        const shut = await probe(pg);
        assert.deepStrictEqual(FIT(shut).map(c => c.rows), FIT(wide).map(c => c.rows), 'ОВП свёрнута — как было');
        assert.ok(Math.abs(shut.grid - shut.baseGrid) < 1);
      }
      // смена фильтра шапки перерисовывает HUD — подгонка снова
      await pg.evaluate(() => KIT.setFilter({ ccy: null }));
      await pg.waitForTimeout(300);
      const again = await probe(pg);
      assert.deepStrictEqual(FIT(again).map(c => c.rows), FIT(wide).map(c => c.rows), 'после перерисовки — те же строки');
      again.fit.forEach(c => assert.ok(c.over <= 1, 'после перерисовки, без обрезки'));
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});

test('K.hud without fitCcy (C12 «hud», other pages): three currency rows as before', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser, 1600);
    try {
      const r = await pg.evaluate(() => { const box = document.createElement('div'); document.body.appendChild(box); KIT.hud(box); return [...box.querySelectorAll('.k-kpi[data-fit]')].length + '|' + [...box.querySelectorAll('.k-kpi')].slice(1, 2).map(c => c.querySelectorAll('.k-kb tr').length).join(); });
      assert.strictEqual(r, '0|3');
      assert.deepStrictEqual(errors, []);
    } finally { await ctx.close(); }
  } finally { await browser.close(); }
});
