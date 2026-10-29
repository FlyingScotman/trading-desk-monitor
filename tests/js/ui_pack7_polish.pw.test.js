// Мониторы в браузере (Playwright, #329). Подпись столбца бара — у его конца (K.barEnd), и под неё есть место
// (barFit у K.chart: отступ оси по ширине подписи, K.labelWidth): C06 «Топ вклад в PnL дня», «Концентрация по
// эмитентам» (MV, CS01, DV01) и «Кэрри и фондирование по книгам», C13 «по секторам» и «по рейтингам» (вертикальный
// бар) по всем мерам (MV, CS01, DV01, PnL дня) и разбор K.drill в виде «график» — при ширине окна 1024, 1280, 1600 и
// 390, в RUB и USD, в тёмной и светлой теме. Геометрия — по элементам ECharts: прямоугольник столбца, его подпись и область
// столбцов (grid) после отрисовки. У отрицательного горизонтального столбца подпись целиком левее его левого края и не
// заходит левее области столбцов (там подписи категорий); у положительного — правее правого края. У вертикального
// бара: у отрицательного — ниже нижнего края и не ниже области столбцов, у положительного — выше верхнего. Подпись не
// уходит за край графика. Ось не раздута ради подписей: размах оси не больше AXIS_MAX размахов данных (с нулём), а
// столбцы занимают не меньше BARS_MIN области (в узкой плитке подпись, которой нет места снаружи, — внутри столбца, #352,
// а нет места и там — не рисуется). CSV C03: «Δ цены, % ном.» — число без «+» и без единицы (#110, #131) в любом формате
// отрицательных. Ошибок консоли нет. Страницы открываются как файлы (file://), данные — демо-набор.
//   node --test tests/js/ui_pack7_polish.pw.test.js
// Снимки: UI_PACK7_POLISH_SHOTS=<папка> node --test tests/js/ui_pack7_polish.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');
// настройки страницы в localStorage и ожидание отрисовки графиков (#453)
const { themeInit, openWith, settle } = require('./fixtures/pw_pages');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = f => pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', f)).href;
const SHOTS = process.env.UI_PACK7_POLISH_SHOTS;

// столбцы с подписью во всех барах внутри root: значение, столбец, подпись и область столбцов в px графика
const bars = (pg, root) => pg.evaluate(sel => {
  const out = [];
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach(el => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt || !el.clientWidth) return;
    const horiz = !!(opt.yAxis || [])[0] && opt.yAxis[0].type === 'category';
    const gr = inst.getModel().getComponent('grid').coordinateSystem.getRect();  // область столбцов, без подписей осей
    (opt.series || []).forEach((s, si) => {
      if (s.type !== 'bar') return;
      const data = inst.getModel().getSeriesByIndex(si).getData();
      for (let i = 0; i < data.count(); i++) {
        const v = data.get(data.mapDimension(horiz ? 'x' : 'y'), i), g = data.getItemGraphicEl(i), tl = g && g.getTextContent();
        if (v == null || isNaN(v) || !tl || tl.ignore || !tl.style || !tl.style.text) continue;
        const r = g.getBoundingRect().clone(), t = tl.getBoundingRect().clone();
        r.applyTransform(g.getComputedTransform()); t.applyTransform(tl.getComputedTransform());
        const d = (s.data || [])[i], pos = (d && d.label && d.label.position) || (s.label && s.label.position) || null;
        out.push({ el: el.id || el.parentElement.id || el.className, v, horiz, pos, text: tl.style.text, W: el.clientWidth, H: el.clientHeight,
          grid: { l: gr.x, r: gr.x + gr.width, t: gr.y, b: gr.y + gr.height },
          bar: { l: r.x, r: r.x + r.width, t: r.y, b: r.y + r.height }, lab: { l: t.x, r: t.x + t.width, t: t.y, b: t.y + t.height } });
      }
    });
  });
  return out;
}, root);
// графики с барами внутри root: размах оси значений и данных (с нулём), длина области столбцов и самих столбцов, px
const axes = (pg, root) => pg.evaluate(sel => {
  const out = [];
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach(el => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt || !el.clientWidth || !(opt.series || []).some(s => s.type === 'bar')) return;
    const horiz = !!(opt.yAxis || [])[0] && opt.yAxis[0].type === 'category';
    const ax = inst.getModel().getComponent(horiz ? 'xAxis' : 'yAxis').axis, ext = ax.scale.getExtent();
    const gr = inst.getModel().getComponent('grid').coordinateSystem.getRect();
    let lo = 0, hi = 0;
    (opt.series || []).forEach((s, si) => {
      if (s.type !== 'bar') return;
      const data = inst.getModel().getSeriesByIndex(si).getData();
      for (let i = 0; i < data.count(); i++) { const v = data.get(data.mapDimension(horiz ? 'x' : 'y'), i); if (isFinite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    });
    const len = horiz ? gr.width : gr.height, px = v => Math.abs(ax.dataToCoord(v) - ax.dataToCoord(0));
    out.push({ el: el.id || el.parentElement.id || el.className, axis: ext[1] - ext[0], data: hi - lo, len, bars: px(lo) + px(hi) });
  });
  return out;
}, root);
const AXIS_MAX = 2.5, BARS_MIN = 0.35;
const notBloated = (L, what) => L.filter(x => x.data > 0).forEach(x => {
  const s = what + ' ' + x.el + ' ' + JSON.stringify(x);
  assert.ok(x.axis <= x.data * AXIS_MAX, s + ': ось не раздута');
  assert.ok(x.bars >= x.len * BARS_MIN, s + ': столбцам хватает места');
});
const E = 0.5;  // допуск, px
const atEnd = (L, what, opt) => {
  opt = opt || {};
  assert.ok(L.length > 0, what + ': есть подписанные столбцы');
  if (opt.neg) assert.ok(L.some(x => x.v < 0), what + ': есть отрицательные столбцы с подписью');
  if (opt.vert != null) assert.ok(L.every(x => x.horiz === !opt.vert), what + ': ' + (opt.vert ? 'вертикальный' : 'горизонтальный') + ' бар');
  L.forEach(x => {
    const s = what + ' ' + x.el + ' «' + x.text + '» ' + JSON.stringify({ v: x.v, pos: x.pos, bar: x.bar, lab: x.lab, grid: x.grid });
    // подпись, которой снаружи нет места, — внутри столбца, у его конца (#352; подробно — ui_pack8_charts.pw.test.js)
    if (/^inside/.test(x.pos || '')) {
      if (x.horiz) assert.ok(x.lab.l >= x.bar.l - E && x.lab.r <= x.bar.r + E, s + ': подпись внутри столбца');
      else assert.ok(x.lab.t >= x.bar.t - E && x.lab.b <= x.bar.b + E, s + ': подпись внутри столбца');
    } else if (x.horiz && x.v < 0) {
      assert.ok(x.lab.r <= x.bar.l + E, s + ': подпись левее конца столбца');
      assert.ok(x.lab.l >= x.grid.l - E, s + ': подпись не заходит на подписи категорий');
    } else if (x.horiz) assert.ok(x.lab.l >= x.bar.r - E, s + ': подпись правее конца столбца');
    else if (x.v < 0) {
      assert.ok(x.lab.t >= x.bar.b - E, s + ': подпись ниже конца столбца');
      assert.ok(x.lab.b <= x.grid.b + E, s + ': подпись не заходит на подписи категорий');
    } else assert.ok(x.lab.b <= x.bar.t + E, s + ': подпись выше конца столбца');
    assert.ok(x.lab.l >= -E && x.lab.r <= x.W + E && x.lab.t >= -E && x.lab.b <= x.H + E, s + ': подпись внутри графика');
  });
};
const SIZES = [[1024, 768], [1280, 800], [1600, 1000], [390, 844]];

for (const [w, h] of SIZES) for (const theme of ['dark', 'light']) for (const repr of ['RUB', 'USD']) {
  test('#329: подписи столбцов у конца и с местом — C06, C13, разбор; ' + w + ' px, ' + repr + ', тема ' + theme, { skip: !pw && 'нет Playwright' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      await themeInit(ctx, theme, repr);
      const pg = await ctx.newPage(), errors = [];
      pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const shot = async name => { if (SHOTS) await pg.screenshot({ path: path.join(SHOTS, name + '-' + w + '-' + repr + '-' + theme + '.png'), fullPage: w < 600 }); };
      const open = async (file, ready, store) => {
        await openWith(pg, PAGE(file), ready, store);
        await settle(pg);
        assert.strictEqual(await pg.evaluate(() => [KIT.theme(), KIT.repr].join()), theme + ',' + repr);
      };

      // C06: все плитки с барами; «Концентрация по эмитентам» — по каждой мере
      for (const mode of ['mv', 'cs01', 'dv01']) {
        await open('c06-launchpad.html', () => window.KIT && document.querySelector('[_echarts_instance_]'), { 'c06-modes': JSON.stringify({ issuers: mode }) });
        const c06 = await bars(pg, 'body');
        atEnd(c06, 'C06 (' + mode + ')', { neg: w !== 1024 });  // в 1024 подписи, которым нет места, не рисуются
        if (w !== 1024) assert.ok(c06.some(x => x.v < 0 && /^\(|^−|^-/.test(x.text)), 'C06: отрицательный вклад подписан');
        notBloated(await axes(pg, 'body'), 'C06 (' + mode + ')');
        if (mode === 'mv') await shot('c06');
      }

      // C13: сектора (горизонтальный бар) и рейтинги (вертикальный) — по каждой мере
      for (const meas of ['mv', 'cs01', 'dv01', 'pnl']) {
        await open('c13-issuers.html', () => window.KIT && document.querySelector('#sector[_echarts_instance_]') && document.querySelector('#rating[_echarts_instance_]'), { c13: JSON.stringify({ meas }) });
        atEnd(await bars(pg, '#sector'), 'C13 сектора, ' + meas, { vert: false, neg: meas !== 'mv' });
        atEnd(await bars(pg, '#rating'), 'C13 рейтинги, ' + meas, { vert: true });
        notBloated(await axes(pg, '#sector'), 'C13 сектора, ' + meas);
        notBloated(await axes(pg, '#rating'), 'C13 рейтинги, ' + meas);
        if (meas === 'dv01') await shot('c13');
      }

      // разбор из столбца сектора с самым отрицательным PnL: вид «график»
      await open('c13-issuers.html', () => window.KIT && document.querySelector('#sector[_echarts_instance_]'), { c13: JSON.stringify({ meas: 'pnl' }) });
      // щелчок по столбцу — событие click графика (как щелчок мышью: обработчик страницы открывает разбор)
      await pg.evaluate(() => {
        const inst = echarts.getInstanceByDom(document.querySelector('#sector')), data = inst.getModel().getSeriesByIndex(0).getData();
        let i = 0; for (let k = 0; k < data.count(); k++) if (data.get(data.mapDimension('x'), k) < data.get(data.mapDimension('x'), i)) i = k;
        inst.trigger('click', { componentType: 'series', seriesIndex: 0, dataIndex: i, value: data.get(data.mapDimension('x'), i) });
      });
      await pg.waitForSelector('.k-drawer');
      await pg.click('.k-drawer button:has-text("график")');
      await pg.waitForFunction(() => document.querySelector('.k-drawer [_echarts_instance_]'));
      await settle(pg);
      atEnd(await bars(pg, '.k-drawer'), 'разбор', { vert: false, neg: true });
      notBloated(await axes(pg, '.k-drawer'), 'разбор');
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
