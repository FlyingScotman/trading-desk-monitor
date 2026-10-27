// Мониторы в браузере (Playwright), пакет 8A. При ширине окна 1024, 1280, 1600 и 390, в RUB и USD, в тёмной и светлой
// теме:
// - #352: у самых крупных по модулю столбцов C06 «Топ вклад в PnL дня» и «Концентрация по эмитентам» подпись есть —
//   снаружи у конца столбца или внутри него, у конца, — и читается: контраст с фоном плитки (снаружи) или с цветом
//   столбца (внутри) не меньше 4.5. Узкий график с подписями с обеих сторон (собран на странице через K.chart, barFit):
//   подписи длинных столбцов — внутри, целиком в прямоугольнике столбца, контрастные. Подпись, которая по факту
//   помещается в столбец (столбец не короче подписи + 6 px), не спрятана — на всех графиках с барами;
// - #351: подписи делений осей и прочие тексты графиков с барами на C06, C13, C09 («базис ОФЗ − RUONIA») и в разборе
//   не наезжают друг на друга; у горизонтальных баров подписи делений видны (не меньше двух), ноль подписан, подписи
//   идут через равный шаг, при оси по обе стороны нуля — подпись на каждой стороне;
// - #352: подписи категорий горизонтальных баров различимы (разные категории — разные подписи, и в разборе по
//   позициям — MV, PnL, DV01 — на 390), buy / sell / pay в сокращённой подписи остаются; при 1600 у подписей «Топ
//   вклада» C06 сокращено не больше пояснения в скобках;
// - #347, #335: на C17 в демо итог «Нетто» таблицы позиций — нетто посчитанных позиций с пометкой «без N из M поз.» (у
//   позиций с прогнозом нет фондирования; до #335 — «—»);
// - #349: на C09 подпись отрицательного столбца базиса — ниже его нижнего края и не ниже области столбцов,
//   положительного — выше верхнего.
// Ширины подписей зависят от шрифта: в CI шрифт закреплён (Inter, tests/js/fixtures/fonts.conf), проверки не
// опираются на конкретные ширины — проба подбирает ширину сама. Геометрия — по элементам ECharts после отрисовки. Ошибок консоли нет. Страницы открываются как файлы (file://).
//   node --test tests/js/ui_pack8_charts.pw.test.js
// Снимки: UI_PACK8_SHOTS=<папка> node --test tests/js/ui_pack8_charts.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = f => pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', f)).href;
const SHOTS = process.env.UI_PACK8_SHOTS;
const E = 1;  // допуск, px

// подписанные столбцы графиков с барами внутри root: значение, прямоугольники столбца, подписи и области столбцов,
// положение подписи (из опции), цвет текста подписи, цвет столбца, фон плитки
const bars = (pg, root) => pg.evaluate(sel => {
  const out = [];
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = getComputedStyle(e).backgroundColor; if (c && !/rgba\(0, 0, 0, 0\)|transparent/.test(c)) return c; } return getComputedStyle(document.body).backgroundColor; };
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach((el, ci) => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt || !el.clientWidth) return;
    const horiz = !!(opt.yAxis || [])[0] && opt.yAxis[0].type === 'category';
    const gr = inst.getModel().getComponent('grid').coordinateSystem.getRect();
    (opt.series || []).forEach((s, si) => {
      if (s.type !== 'bar') return;
      const data = inst.getModel().getSeriesByIndex(si).getData();
      for (let i = 0; i < data.count(); i++) {
        const v = data.get(data.mapDimension(horiz ? 'x' : 'y'), i), g = data.getItemGraphicEl(i), tl = g && g.getTextContent();
        if (v == null || isNaN(v) || !g) continue;
        const shown = !!(tl && !tl.ignore && tl.style && tl.style.text);
        const r = g.getBoundingRect().clone(); r.applyTransform(g.getComputedTransform());
        let t = null;
        if (shown) { t = tl.getBoundingRect().clone(); t.applyTransform(tl.getComputedTransform()); }
        const d = (s.data || [])[i], pos = (d && d.label && d.label.position) || (s.label && s.label.position) || null;
        // спрятанная подпись: её ширина (у вертикального бара — высота строки) тем же измерением, что у K.barFit
        const sl = s.label || {}, px = sl.fontSize || 12;
        let need = null;
        if (!shown && sl.show && typeof sl.formatter === 'function') { const tx = KIT.htmlText(sl.formatter({ value: v, data: d })); if (tx) need = horiz ? KIT.labelWidth(tx, px) : px * 1.3; }
        out.push({ ci, el: el.id || el.parentElement.id || el.className, v, horiz, shown, pos, need, text: shown ? tl.style.text : '', W: el.clientWidth, H: el.clientHeight,
          fill: shown ? tl.style.fill : null, barColor: g.style && g.style.fill, bg: bgOf(el),
          grid: { l: gr.x, r: gr.x + gr.width, t: gr.y, b: gr.y + gr.height },
          bar: { l: r.x, r: r.x + r.width, t: r.y, b: r.y + r.height }, lab: t && { l: t.x, r: t.x + t.width, t: t.y, b: t.y + t.height } });
      }
    });
  });
  return out;
}, root);
// видимые тексты графиков с барами внутри root (подписи делений, категорий, столбцов, единиц, легенды) — прямоугольники
const texts = (pg, root) => pg.evaluate(sel => {
  const out = [];
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach(el => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt || !el.clientWidth || !(opt.series || []).some(s => s.type === 'bar')) return;
    const L = [];
    inst.getZr().storage.getDisplayList().forEach(e => {
      if (!e.style || typeof e.style.text !== 'string' || !e.style.text.trim() || e.ignore || e.invisible || e.style.opacity === 0) return;
      // легенда с прокруткой обрезана своим окном — её тексты не сравниваем
      for (let p = e.parent; p; p = p.parent) if (p.__ecComponentInfo && p.__ecComponentInfo.mainType === 'legend') return;
      const r = e.getBoundingRect().clone(); r.applyTransform(e.getComputedTransform());
      // прямоугольник строки текста (tspan) — без полей подписи (padding): зазор считаем между буквами
      if (r.width > 0 && r.height > 0) L.push({ text: e.style.text, l: r.x, r: r.x + r.width, t: r.y, b: r.y + r.height });
    });
    out.push({ el: el.id || el.parentElement.id || el.className, L });
  });
  return out;
}, root);
// горизонтальные бары внутри root: подписи делений оси значений, которые видны на экране (значение и текст), концы оси,
// подписи категорий на экране (исходная — из data, показанная) и полные подписи категорий (data)
const hbars = (pg, root) => pg.evaluate(sel => {
  const out = [];
  document.querySelectorAll(sel + '[_echarts_instance_], ' + sel + ' [_echarts_instance_]').forEach(el => {
    const inst = echarts.getInstanceByDom(el), opt = inst && inst.getOption();
    if (!opt || !el.clientWidth || !(opt.series || []).some(s => s.type === 'bar') || !(opt.yAxis || [])[0] || opt.yAxis[0].type !== 'category' || opt.xAxis.length !== 1) return;
    const shown = new Map();
    inst.getZr().storage.getDisplayList().forEach(e => { if (e.style && typeof e.style.text === 'string' && e.style.text && !e.ignore && !e.invisible) shown.set(e.style.text, (shown.get(e.style.text) || 0) + 1); });
    const take = t => { const n = shown.get(t) || 0; if (!n) return false; shown.set(t, n - 1); return true; };
    const xa = inst.getModel().getComponent('xAxis').axis, ya = inst.getModel().getComponent('yAxis').axis;
    const ticks = xa.getViewLabels().filter(l => l.formattedLabel !== '' && take(l.formattedLabel)).map(l => ({ v: +l.tickValue, t: l.formattedLabel }));
    const cats = ya.getViewLabels().filter(l => take(l.formattedLabel)).map(l => ({ raw: String(l.rawLabel), t: l.formattedLabel }));
    out.push({ el: el.id || el.parentElement.id || el.className, W: el.clientWidth, ext: xa.scale.getExtent(), ticks, cats, data: (opt.yAxis[0].data || []).map(x => String(x && typeof x === 'object' ? x.value : x)), n: (opt.yAxis[0].data || []).length });
  });
  return out;
}, root);
// #351: подписи делений видны, ноль подписан (если он на оси), подписи через равный шаг; #352: подписи категорий
// различимы — у разных категорий разные подписи на экране
// подпись, которая по факту помещается внутрь столбца (столбец не короче подписи + 6 px), не спрятана (#352)
const noHiddenFit = (L, what) => L.filter(x => !x.shown && x.need != null).forEach(x => {
  const len = x.horiz ? x.bar.r - x.bar.l : x.bar.b - x.bar.t;
  assert.ok(len < x.need + 6, what + ' ' + x.el + ': подпись столбца ' + x.v + ' спрятана, хотя столбец ' + len.toFixed(1) + ' px, подпись ' + x.need.toFixed(1) + ' px');
});
const axesOk = (G, what, opt) => G.forEach(x => {
  const s = what + ' ' + x.el + ' ' + JSON.stringify({ ext: x.ext, ticks: x.ticks.map(t => t.t) });
  assert.ok(x.ticks.length >= 2, s + ': подписей делений не меньше двух');
  if (x.ext[0] <= 0 && x.ext[1] >= 0) assert.ok(x.ticks.some(t => t.v === 0), s + ': ноль подписан');
  // ось по обе стороны нуля — подпись на каждой стороне, то есть подписей не меньше трёх
  if (x.ext[0] < 0) assert.ok(x.ticks.some(t => t.v < 0), s + ': слева от нуля есть подпись');
  if (x.ext[1] > 0) assert.ok(x.ticks.some(t => t.v > 0), s + ': справа от нуля есть подпись');
  const d = x.ticks.slice(1).map((t, i) => t.v - x.ticks[i].v);
  assert.ok(d.every(v => Math.abs(v - d[0]) <= 1e-9 * Math.abs(d[0])), s + ': подписи делений через равный шаг');
  const by = new Map(); x.cats.forEach(c => { if (!by.has(c.t)) by.set(c.t, new Set()); by.get(c.t).add(c.raw); });
  by.forEach((raws, t) => assert.strictEqual(raws.size, 1, what + ' ' + x.el + ': подпись «' + t + '» у разных категорий ' + [...raws].join(' | ')));
  // направление сделки и вид ставки (buy / sell / pay / rec) в сокращённой подписи остаются
  x.cats.forEach(c => { const m = /\b(buy|sell|pay|rec)\b/.exec(c.raw); if (m) assert.ok(c.t.includes(m[1]), what + ' ' + x.el + ': «' + c.t + '» без «' + m[1] + '» у «' + c.raw + '»'); });
  // места хватает (C06 «Топ вклад» при 1600): имя целиком, сокращено может быть только пояснение в скобках в конце
  if (opt && opt.full) x.cats.forEach(c => {
    const m = /^(.*\S)\s*\(([^()]*)\)$/.exec(c.raw);
    assert.ok(c.t === c.raw || (m && c.t.startsWith(m[1] + ' (') && c.t.endsWith('…)')), what + ' ' + x.el + ': подпись «' + c.t + '» — сокращено больше пояснения в скобках у «' + c.raw + '»');
  });
});
const lum = c => { const m = /(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)\D+(\d+(?:\.\d+)?)/.exec(c) || []; let rgb = m.slice(1, 4).map(Number); if (/^#/.test(c)) { let h = c.slice(1); if (h.length <= 4) h = h.split('').map(x => x + x).join(''); rgb = [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); } const l = rgb.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }); return 0.2126 * l[0] + 0.7152 * l[1] + 0.0722 * l[2]; };
const contrast = (a, b) => { const p = lum(a), q = lum(b); return (Math.max(p, q) + 0.05) / (Math.min(p, q) + 0.05); };
const inside = x => /^inside/.test(x.pos || '');

// подпись у конца столбца: снаружи (K.barEnd, #329) или внутри, у конца (#352); читается
const labelOk = (x, what) => {
  const s = what + ' ' + x.el + ' «' + x.text + '» ' + JSON.stringify({ v: x.v, pos: x.pos, bar: x.bar, lab: x.lab, grid: x.grid, fill: x.fill, barColor: x.barColor, bg: x.bg });
  const { lab, bar } = x;
  if (inside(x)) {
    if (x.horiz) {
      assert.ok(lab.l >= bar.l - E && lab.r <= bar.r + E, s + ': подпись внутри столбца');
      if (x.v < 0) assert.ok(lab.l - bar.l <= 8, s + ': у левого конца'); else assert.ok(bar.r - lab.r <= 8, s + ': у правого конца');
    } else {
      assert.ok(lab.t >= bar.t - E && lab.b <= bar.b + E, s + ': подпись внутри столбца');
      if (x.v < 0) assert.ok(bar.b - lab.b <= 8, s + ': у нижнего конца'); else assert.ok(lab.t - bar.t <= 8, s + ': у верхнего конца');
    }
    assert.ok(contrast(x.fill, x.barColor) >= 4.5, s + ': контраст со столбцом ' + contrast(x.fill, x.barColor).toFixed(2));
  } else {
    if (x.horiz && x.v < 0) assert.ok(lab.r <= bar.l + E && lab.l >= x.grid.l - E, s + ': левее конца, не на подписях категорий');
    else if (x.horiz) assert.ok(lab.l >= bar.r - E, s + ': правее конца');
    else if (x.v < 0) assert.ok(lab.t >= bar.b - E && lab.b <= x.grid.b + E, s + ': ниже конца, не на подписях категорий');
    else assert.ok(lab.b <= bar.t + E, s + ': выше конца');
    assert.ok(contrast(x.fill, x.bg) >= 4.5, s + ': контраст с фоном ' + contrast(x.fill, x.bg).toFixed(2));
  }
  assert.ok(lab.l >= -E && lab.r <= x.W + E && lab.t >= -E && lab.b <= x.H + E, s + ': подпись внутри графика');
};
// тексты одного графика не пересекаются (допуск E); в одной строке между ними не меньше GAP px — не читаются слитно
const GAP = 3;
const noOverlap = (G, what) => G.forEach(({ el, L }) => {
  for (let i = 0; i < L.length; i++) for (let j = i + 1; j < L.length; j++) {
    const a = L[i], b = L[j];
    const ov = Math.min(a.r, b.r) - Math.max(a.l, b.l) > -GAP && Math.min(a.b, b.b) - Math.max(a.t, b.t) > E;
    assert.ok(!ov, what + ' ' + el + ': «' + a.text + '» и «' + b.text + '» наезжают ' + JSON.stringify([a, b]));
  }
});
const settle = pg => pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 400)))));
const SIZES = [[1024, 768], [1280, 800], [1600, 1000], [390, 844]];

for (const [w, h] of SIZES) for (const theme of ['dark', 'light']) for (const repr of ['RUB', 'USD']) {
  test('#352, #351, #349: подписи столбцов и делений — ' + w + ' px, ' + repr + ', тема ' + theme, { skip: !pw && 'нет Playwright' }, async () => {
    const browser = await pw.chromium.launch();
    try {
      const ctx = await browser.newContext({ viewport: { width: w, height: h } });
      await ctx.addInitScript(([t, r]) => { try { if (!sessionStorage.getItem('init')) { sessionStorage.setItem('init', '1'); localStorage.clear(); localStorage.setItem('concepts-theme', t); localStorage.setItem('concepts-repr', r); } } catch (e) { } }, [theme, repr]);
      const pg = await ctx.newPage(), errors = [];
      pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
      const shot = async name => { if (SHOTS) await pg.screenshot({ path: path.join(SHOTS, name + '-' + w + '-' + repr + '-' + theme + '.png'), fullPage: w < 600 }); };
      const open = async (file, ready, store) => {
        await pg.goto(PAGE(file));
        await pg.evaluate(s => Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)), store || {});
        await pg.reload();
        await pg.waitForFunction(ready);
        await settle(pg);
        assert.strictEqual(await pg.evaluate(() => [KIT.theme(), KIT.repr].join()), theme + ',' + repr);
      };

      // C06: крупнейшие по модулю столбцы «Топ вклад» (12) и «Концентрация» подписаны и читаются; тексты не наезжают
      await open('c06-launchpad.html', () => window.KIT && document.querySelector('[_echarts_instance_]'));
      const c06 = await bars(pg, 'body');
      const byTile = {};
      c06.forEach(x => (byTile[x.ci] = byTile[x.ci] || []).push(x));
      const labelled = Object.values(byTile).filter(L => L.some(x => x.shown));
      assert.ok(labelled.length >= 2, 'C06: «Топ вклад» и «Концентрация» с подписями');
      labelled.forEach(L => {
        const top = L.slice().sort((a, b) => Math.abs(b.v) - Math.abs(a.v)).slice(0, 4);
        top.forEach(x => assert.ok(x.shown, 'C06 ' + x.el + ': у столбца ' + x.v + ' (топ-4 по модулю из ' + L.length + ') есть подпись'));
        L.filter(x => x.shown).forEach(x => labelOk(x, 'C06'));
        noHiddenFit(L, 'C06');
      });
      if (w === 1024) {
        // «Топ вклад» — 12 столбцов; спрятаны только те, кому нет места ни снаружи, ни внутри: их меньше трети
        const tc = labelled.find(L => L.length === 12);
        assert.ok(tc, 'C06 «Топ вклад»: 12 столбцов');
        assert.ok(tc.filter(x => !x.shown).length <= 3, 'C06 «Топ вклад»: спрятано ' + tc.filter(x => !x.shown).length);
      }
      noOverlap(await texts(pg, 'body'), 'C06');
      // деления осей и подписи категорий: «Топ вклад», «Концентрация» (все меры), «Кэрри и фондирование»; при 1280 и
      // шире подписи «Топ вклада» не обрезаны (места хватает)
      const c06h = await hbars(pg, 'body');
      assert.ok(c06h.length >= 3, 'C06: три горизонтальных бара');
      axesOk(c06h, 'C06');
      if (w >= 1600) axesOk(c06h.filter(x => x.n === 12), 'C06 «Топ вклад»', { full: true });
      await shot('c06');
      for (const mode of ['cs01', 'dv01']) {
        await open('c06-launchpad.html', () => window.KIT && document.querySelector('[_echarts_instance_]'), { 'c06-modes': JSON.stringify({ issuers: mode }) });
        axesOk(await hbars(pg, 'body'), 'C06 (' + mode + ')');
      }
      await open('c06-launchpad.html', () => window.KIT && document.querySelector('[_echarts_instance_]'));

      // узкий график с подписями с обеих сторон: длинные столбцы — подпись внутри, у конца, контрастно. Ширина пробы —
      // не число под шрифт этой машины (в CI другой шрифт и другие ширины подписей): самая узкая из 110…300 px, при
      // которой K.barFit (с теми же метриками K.labelWidth) ставит подпись внутрь и подписывает все столбцы больше 15 по
      // модулю. Такая ширина обязана найтись; после отрисовки геометрия и контраст проверяются по элементам ECharts
      const probeW = await pg.evaluate(() => {
        const vals = [-17.7, -16.2, 18.9, 2, -0.5, -9.1, 15.2];
        window.__probeOpt = () => ({ barFit: true, grid: { left: 4, right: 4, top: 4, bottom: 2, containLabel: true },
          xAxis: { type: 'value' }, yAxis: { type: 'category', data: vals.map((_, i) => 'кат ' + i), axisLabel: { fontSize: 10 } },
          series: [{ type: 'bar', data: vals.map(v => ({ value: v, itemStyle: { color: v < 0 ? KIT.css('--neg') : KIT.css('--pos') }, label: KIT.barEnd(v) })), label: { show: true, position: 'right', fontSize: 10, color: KIT.css('--fg-2'), formatter: p => '(' + Math.abs(p.value).toFixed(1) + ')' } }] });
        for (let W = 110; W <= 300; W += 5) {
          const o = window.__probeOpt(); delete o.barFit;
          KIT.barFit({ clientWidth: W, clientHeight: 220 }, o);
          const L = o.series[0].data.map(d => d.label);
          if (L.some(l => /^inside/.test(l.position || '') && l.show !== false) && o.series[0].data.every((d, i) => Math.abs(vals[i]) <= 15 || L[i].show !== false)) return W;
        }
        return null;
      });
      assert.ok(probeW, 'проба: есть ширина, при которой длинные столбцы подписаны внутри');
      await pg.evaluate(W => {
        const d = document.createElement('div'); d.id = 'pack8probe';
        d.style.cssText = 'position:fixed;left:0;top:0;width:' + W + 'px;height:220px;z-index:999;background:var(--panel)';
        document.body.appendChild(d);
        KIT.chart(d, window.__probeOpt);
      }, probeW);
      await settle(pg);
      const pr = await bars(pg, '#pack8probe');
      assert.strictEqual(pr.length, 7);
      assert.ok(pr.some(x => inside(x) && x.shown), 'проба ' + probeW + ' px: есть подписи внутри ' + JSON.stringify(pr.map(x => [x.v, x.shown && x.pos])));
      pr.filter(x => Math.abs(x.v) > 15).forEach(x => assert.ok(x.shown, 'проба ' + probeW + ' px: столбец ' + x.v + ' подписан'));
      pr.filter(x => x.shown).forEach(x => labelOk(x, 'проба'));
      noHiddenFit(pr, 'проба');

      // C13: сектора и рейтинги по мерам — подписи читаются, тексты не наезжают
      for (const meas of ['mv', 'pnl']) {
        await open('c13-issuers.html', () => window.KIT && document.querySelector('#sector[_echarts_instance_]') && document.querySelector('#rating[_echarts_instance_]'), { c13: JSON.stringify({ meas }) });
        for (const sel of ['#sector', '#rating']) {
          const L = await bars(pg, sel);
          assert.ok(L.some(x => x.shown), 'C13 ' + sel + ' подписан');
          L.filter(x => x.shown).forEach(x => labelOk(x, 'C13 ' + meas));
          noHiddenFit(L, 'C13 ' + meas);
          noOverlap(await texts(pg, sel), 'C13 ' + sel + ' ' + meas);
        }
        axesOk(await hbars(pg, '#sector'), 'C13 сектора ' + meas);
      }

      // разбор «из чего цифра» по позициям, вид «график» (#352: обрезанные подписи различимы; #351: деления)
      // сектор с самым крупным по модулю итогом, MV и PnL дня (много позиций, у многих общее начало имени: «Минфин РФ …»,
      // «Газпром капитал …»; в PnL — и отрицательные столбцы)
      for (const meas of ['mv', 'pnl', 'dv01']) {
        await open('c13-issuers.html', () => window.KIT && document.querySelector('#sector[_echarts_instance_]'), { c13: JSON.stringify({ meas }) });
        await pg.evaluate(() => {
          const inst = echarts.getInstanceByDom(document.querySelector('#sector')), data = inst.getModel().getSeriesByIndex(0).getData();
          let i = 0; for (let k = 0; k < data.count(); k++) if (Math.abs(data.get(data.mapDimension('x'), k)) > Math.abs(data.get(data.mapDimension('x'), i))) i = k;
          inst.trigger('click', { componentType: 'series', seriesIndex: 0, dataIndex: i, value: data.get(data.mapDimension('x'), i) });
        });
        await pg.waitForSelector('.k-drawer');
        await pg.click('.k-drawer button:has-text("позиции")');
        await pg.click('.k-drawer button:has-text("график")');
        await pg.waitForFunction(() => document.querySelector('.k-drawer [_echarts_instance_]'));
        await settle(pg);
        const dr = await hbars(pg, '.k-drawer');
        assert.strictEqual(dr.length, 1, 'разбор ' + meas + ': график');
        assert.ok(dr[0].n >= 8, 'разбор по позициям: позиций ' + dr[0].n);
        axesOk(dr, 'разбор ' + meas);
        const db = await bars(pg, '.k-drawer');
        db.filter(x => x.shown).forEach(x => labelOk(x, 'разбор ' + meas));
        noHiddenFit(db, 'разбор ' + meas);
        noOverlap(await texts(pg, '.k-drawer'), 'разбор ' + meas);
        await shot('drill-' + meas);
      }


      // C09 «базис ОФЗ − RUONIA» (#349): подпись у конца столбца, отрицательного — снизу; тексты не наезжают
      await open('c09-market.html', () => window.KIT && document.querySelector('#curves[_echarts_instance_]'), { 'c09-state': JSON.stringify({ curve: 'basis' }) });
      const c09 = await bars(pg, '#curves');
      assert.ok(c09.length > 3 && c09.some(x => x.v < 0 && x.shown) && c09.some(x => x.v > 0 && x.shown), 'C09: базис с отрицательными и положительными подписанными столбцами');
      assert.ok(c09.every(x => !x.horiz), 'C09: вертикальный бар');
      c09.filter(x => x.shown).forEach(x => labelOk(x, 'C09 базис'));
      noHiddenFit(c09, 'C09 базис');
      c09.filter(x => x.shown && x.v < 0).forEach(x => assert.strictEqual(x.pos, 'bottom', 'C09: отрицательный — снизу'));
      noOverlap(await texts(pg, '#curves'), 'C09 базис');
      await shot('c09');

      // C17 (#347, #335): в демо у позиций с прогнозом нет фондирования — итог «Нетто» в таблице — число с пометкой
      // «без N из M поз.» (вариант «а», решение владельца 27.10; до #335 — «—»)
      await open('c17-forward-carry.html', () => window.KIT && document.querySelector('table.k-tbl tfoot'));
      const foot = await pg.evaluate(() => {
        const t = [...document.querySelectorAll('table.k-tbl')].find(x => x.tFoot && [...x.tHead.rows[0].cells].some(c => c.textContent.trim().startsWith('Нетто')));
        if (!t) return null;
        const hc = [...t.tHead.rows[0].cells], fc = [...t.tFoot.rows[0].cells];
        const at = name => { const i = hc.findIndex(c => c.textContent.trim().startsWith(name)); return i >= 0 && fc[i] ? fc[i].textContent.trim() : null; };
        return { net: at('Нетто'), funding: at('Фондирование'), carry: at('Кэрри'), n: hc.length === fc.length };
      });
      assert.ok(foot && foot.n, 'C17: таблица позиций с итогом ' + JSON.stringify(foot));
      assert.ok(/\d/.test(foot.net) && /· без \d+ из \d+ поз\./.test(foot.net), 'C17: итог «Нетто» ' + JSON.stringify(foot));
      assert.ok(/\d/.test(foot.funding) && /\d/.test(foot.carry), 'C17: итоги «Кэрри» и «Фондирование» — числа ' + JSON.stringify(foot));
      assert.deepStrictEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}
