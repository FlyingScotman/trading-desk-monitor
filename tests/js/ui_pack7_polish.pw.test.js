// Мониторы в браузере (Playwright, #329). Подпись отрицательного столбца горизонтального бара — у конца столбца, а не
// у нуля (K.barEnd): C06 «Топ вклад в PnL дня» (и остальные горизонтальные бары C06), C13 «по секторам» и разбор
// K.drill в виде «график» — в RUB и USD, в тёмной и светлой теме. Геометрия — по элементам ECharts: прямоугольник
// столбца и его подпись после отрисовки. У отрицательного горизонтального столбца подпись целиком левее его левого
// края, у вертикального — ниже нижнего; подпись не заходит на подписи категорий (K.barGap) и не уходит за край
// графика. CSV C03: «Δ цены, % ном.» — число без «+» и без единицы (#110, #131) в любом формате отрицательных. Ошибок
// консоли нет. Страницы открываются как файлы (file://), данные — демо-набор.
//   node --test tests/js/ui_pack7_polish.pw.test.js
// Снимки: UI_PACK7_SHOTS=<папка> node --test tests/js/ui_pack7_polish.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = f => pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', f)).href;
const SHOTS = process.env.UI_PACK7_SHOTS;

// отрицательные столбцы с подписью во всех барах внутри root: столбец и подпись в px графика
const negBars = (pg, root) => pg.evaluate(sel => {
  const out = [];
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach(el => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt) return;
    const horiz = !!(opt.yAxis || [])[0] && opt.yAxis[0].type === 'category';
    const gr = inst.getModel().getComponent('grid').coordinateSystem.getRect();  // область столбцов, без подписей осей
    (opt.series || []).forEach((s, si) => {
      if (s.type !== 'bar') return;
      const data = inst.getModel().getSeriesByIndex(si).getData();
      for (let i = 0; i < data.count(); i++) {
        const v = data.get(data.mapDimension(horiz ? 'x' : 'y'), i), g = data.getItemGraphicEl(i), tl = g && g.getTextContent();
        if (!(v < 0) || !tl || tl.ignore || !tl.style || !tl.style.text) continue;
        const r = g.getBoundingRect().clone(), t = tl.getBoundingRect().clone();
        r.applyTransform(g.getComputedTransform()); t.applyTransform(tl.getComputedTransform());
        out.push({ el: el.id || el.parentElement.id || el.className, v, horiz, text: tl.style.text, W: el.clientWidth, H: el.clientHeight,
          grid: { l: gr.x, r: gr.x + gr.width, t: gr.y, b: gr.y + gr.height },
          bar: { l: r.x, r: r.x + r.width, t: r.y, b: r.y + r.height }, lab: { l: t.x, r: t.x + t.width, t: t.y, b: t.y + t.height } });
      }
    });
  });
  return out;
}, root);
const atEnd = (L, what) => {
  assert.ok(L.length > 0, what + ': есть отрицательные столбцы с подписью');
  L.forEach(x => {
    const s = what + ' ' + x.el + ' «' + x.text + '» ' + JSON.stringify({ bar: x.bar, lab: x.lab, grid: x.grid });
    if (x.horiz) assert.ok(x.lab.r <= x.bar.l + 0.5, s + ': подпись левее конца столбца');
    else assert.ok(x.lab.t >= x.bar.b - 0.5, s + ': подпись ниже конца столбца');
    // и не заходит на подписи категорий (K.barGap: ось значений расширена влево)
    if (x.horiz) assert.ok(x.lab.l >= x.grid.l - 0.5, s + ': подпись не на подписях категорий');
    assert.ok(x.lab.l >= -0.5 && x.lab.r <= x.W + 0.5 && x.lab.t >= -0.5 && x.lab.b <= x.H + 0.5, s + ': подпись внутри графика');
  });
};
const settle = pg => pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 400)))));

for (const theme of ['dark', 'light']) for (const repr of ['RUB', 'USD']) {
  test('#329: подпись отрицательного столбца у его конца — C06, C13 сектора, разбор; ' + repr + ', тема ' + theme, { skip: !pw && 'нет Playwright' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
      await ctx.addInitScript(([t, r]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); localStorage.setItem('concepts-repr', r); localStorage.setItem('c13', JSON.stringify({ meas: 'pnl' })); } catch (e) { } }, [theme, repr]);
      const pg = await ctx.newPage(), errors = [];
      pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const shot = async name => { if (SHOTS) await pg.screenshot({ path: path.join(SHOTS, name + '-' + repr + '-' + theme + '.png') }); };

      await pg.goto(PAGE('c06-launchpad.html'));
      await pg.waitForFunction(() => window.KIT && document.querySelector('[_echarts_instance_]'));
      await settle(pg);
      assert.strictEqual(await pg.evaluate(() => [KIT.theme(), KIT.repr].join()), theme + ',' + repr);
      const c06 = await negBars(pg, 'body');
      atEnd(c06, 'C06');
      assert.ok(c06.some(x => /^\(|^−|^-/.test(x.text)), 'C06: «Топ вклад в PnL дня» — отрицательный вклад подписан');
      await shot('c06');

      await pg.goto(PAGE('c13-issuers.html'));
      await pg.waitForFunction(() => window.KIT && document.querySelector('#sector[_echarts_instance_]'));
      await settle(pg);
      atEnd(await negBars(pg, '#sector'), 'C13 сектора');
      await shot('c13');

      // разбор из столбца сектора с отрицательным PnL: вид «график»
      const at = await pg.evaluate(() => {
        const el = document.querySelector('#sector'), inst = echarts.getInstanceByDom(el);
        const data = inst.getModel().getSeriesByIndex(0).getData(), box = el.getBoundingClientRect();
        let i = 0; for (let k = 0; k < data.count(); k++) if (data.get(data.mapDimension('x'), k) < data.get(data.mapDimension('x'), i)) i = k;
        const r = data.getItemGraphicEl(i).getBoundingRect().clone(); r.applyTransform(data.getItemGraphicEl(i).getComputedTransform());
        return { x: box.left + r.x + r.width / 2, y: box.top + r.y + r.height / 2 };
      });
      await pg.mouse.click(at.x, at.y);
      await pg.waitForSelector('.k-drawer');
      await pg.click('.k-drawer button:has-text("график")');
      await pg.waitForFunction(() => document.querySelector('.k-drawer [_echarts_instance_]'));
      await settle(pg);
      atEnd(await negBars(pg, '.k-drawer'), 'разбор');
      await shot('drill');
      assert.deepStrictEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}

test('#329: CSV C03 — «Δ цены, % ном.» числом, без «+» и без единицы', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    await ctx.addInitScript(() => { try { localStorage.clear(); } catch (e) { } });
    const pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    await pg.goto(PAGE('c03-blotter.html'));
    await pg.waitForFunction(() => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt);
    await pg.evaluate(() => { document.querySelector('#tbl table').dataset.old = '1'; });
    await pg.click('#sCols button:has-text("Цены и спреды")');
    await pg.waitForFunction(() => { const t = document.querySelector('#tbl table'); return t && !t.dataset.old && t.__kt; });
    for (const neg of ['minus', 'acc']) {
      const { csv, screen } = await pg.evaluate(nf => {
        KIT.setNegFmt(nf);
        const t = document.querySelector('#tbl table');
        return { csv: t.__kt.csv(), screen: [...t.querySelectorAll('tbody td')].map(td => td.textContent.trim()) };
      }, neg);
      const lines = csv.split(/\r?\n/), hi = lines.findIndex(l => l.includes('Δ цены, % ном.'));
      assert.ok(hi >= 0, neg + ': колонка «Δ цены, % ном.» в CSV');
      // заголовок с единицей в кавычках: «"Δ цены, % ном."» — номер колонки по разбору CSV-строки
      const cells = l => { const out = []; let cur = '', q = false; for (const ch of l) { if (ch === '"') q = !q; else if (ch === ',' && !q) { out.push(cur); cur = ''; } else cur += ch; } out.push(cur); return out; };
      const head = cells(lines[hi]), j = head.indexOf('Δ цены, % ном.');
      assert.ok(j >= 0, head.join('|'));
      const vals = lines.slice(hi + 1).filter(l => l).map(l => cells(l)[j]);
      assert.ok(vals.length > 10, neg + ': строки позиций');
      vals.forEach(v => assert.ok(v === '' || /^-?\d+(\.\d+)?$/.test(v), neg + ': «' + v + '» — число или пусто'));
      assert.ok(vals.some(v => +v > 0) && vals.some(v => +v < 0), neg + ': есть рост и падение цены');
      assert.ok(screen.some(x => /^\+\d/.test(x)), neg + ': на экране у роста «+» есть, в CSV — нет');
    }
    assert.deepStrictEqual(errors, []);
  } finally {
    await browser.close();
  }
});
