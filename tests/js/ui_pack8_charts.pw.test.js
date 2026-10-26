// Мониторы в браузере (Playwright), пакет 8A. При ширине окна 1024, 1280, 1600 и 390, в RUB и USD, в тёмной и светлой
// теме:
// - #352: у самых крупных по модулю столбцов C06 «Топ вклад в PnL дня» и «Концентрация по эмитентам» подпись есть —
//   снаружи у конца столбца или внутри него, у конца, — и читается: контраст с фоном плитки (снаружи) или с цветом
//   столбца (внутри) не меньше 4.5. Узкий график с подписями с обеих сторон (собран на странице через K.chart, barFit):
//   подписи длинных столбцов — внутри, целиком в прямоугольнике столбца, контрастные;
// - #351: подписи делений осей и прочие тексты графиков с барами на C06, C13 и C09 («базис ОФЗ − RUONIA») не
//   наезжают друг на друга;
// - #349: на C09 подпись отрицательного столбца базиса — ниже его нижнего края и не ниже области столбцов,
//   положительного — выше верхнего.
// Геометрия — по элементам ECharts после отрисовки. Ошибок консоли нет. Страницы открываются как файлы (file://).
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
        out.push({ ci, el: el.id || el.parentElement.id || el.className, v, horiz, shown, pos, text: shown ? tl.style.text : '', W: el.clientWidth, H: el.clientHeight,
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
      });
      if (w === 1024) {
        // «Топ вклад» — 12 столбцов; спрятаны только те, кому нет места ни снаружи, ни внутри: их меньше трети
        const tc = labelled.find(L => L.length === 12);
        assert.ok(tc, 'C06 «Топ вклад»: 12 столбцов');
        assert.ok(tc.filter(x => !x.shown).length <= 3, 'C06 «Топ вклад»: спрятано ' + tc.filter(x => !x.shown).length);
      }
      noOverlap(await texts(pg, 'body'), 'C06');
      await shot('c06');

      // узкий график с подписями с обеих сторон: длинные столбцы — подпись внутри, у конца, контрастно
      await pg.evaluate(() => {
        const d = document.createElement('div'); d.id = 'pack8probe';
        d.style.cssText = 'position:fixed;left:0;top:0;width:160px;height:220px;z-index:999;background:var(--panel)';
        document.body.appendChild(d);
        const vals = [-17.7, -16.2, 18.9, 2, -0.5, -9.1, 15.2];
        KIT.chart(d, () => ({ barFit: true, grid: { left: 4, right: 4, top: 4, bottom: 2, containLabel: true },
          xAxis: { type: 'value' }, yAxis: { type: 'category', data: vals.map((_, i) => 'кат ' + i), axisLabel: { fontSize: 10 } },
          series: [{ type: 'bar', data: vals.map(v => ({ value: v, itemStyle: { color: v < 0 ? KIT.css('--neg') : KIT.css('--pos') }, label: KIT.barEnd(v) })), label: { show: true, position: 'right', fontSize: 10, color: KIT.css('--fg-2'), formatter: p => '(' + Math.abs(p.value).toFixed(1) + ')' } }] }));
      });
      await settle(pg);
      const pr = await bars(pg, '#pack8probe');
      assert.strictEqual(pr.length, 7);
      assert.ok(pr.some(x => inside(x) && x.shown), 'проба: есть подписи внутри ' + JSON.stringify(pr.map(x => [x.v, x.shown && x.pos])));
      pr.filter(x => Math.abs(x.v) > 15).forEach(x => assert.ok(x.shown, 'проба: столбец ' + x.v + ' подписан'));
      pr.filter(x => x.shown).forEach(x => labelOk(x, 'проба'));

      // C13: сектора и рейтинги по мерам — подписи читаются, тексты не наезжают
      for (const meas of ['mv', 'pnl']) {
        await open('c13-issuers.html', () => window.KIT && document.querySelector('#sector[_echarts_instance_]') && document.querySelector('#rating[_echarts_instance_]'), { c13: JSON.stringify({ meas }) });
        for (const sel of ['#sector', '#rating']) {
          const L = await bars(pg, sel);
          assert.ok(L.some(x => x.shown), 'C13 ' + sel + ' подписан');
          L.filter(x => x.shown).forEach(x => labelOk(x, 'C13 ' + meas));
          noOverlap(await texts(pg, sel), 'C13 ' + sel + ' ' + meas);
        }
      }

      // C09 «базис ОФЗ − RUONIA» (#349): подпись у конца столбца, отрицательного — снизу; тексты не наезжают
      await open('c09-market.html', () => window.KIT && document.querySelector('#curves[_echarts_instance_]'), { 'c09-state': JSON.stringify({ curve: 'basis' }) });
      const c09 = await bars(pg, '#curves');
      assert.ok(c09.length > 3 && c09.some(x => x.v < 0 && x.shown) && c09.some(x => x.v > 0 && x.shown), 'C09: базис с отрицательными и положительными подписанными столбцами');
      assert.ok(c09.every(x => !x.horiz), 'C09: вертикальный бар');
      c09.filter(x => x.shown).forEach(x => labelOk(x, 'C09 базис'));
      c09.filter(x => x.shown && x.v < 0).forEach(x => assert.strictEqual(x.pos, 'bottom', 'C09: отрицательный — снизу'));
      noOverlap(await texts(pg, '#curves'), 'C09 базис');
      await shot('c09');
      assert.deepStrictEqual(errors, []);
    } finally {
      await browser.close();
    }
  });
}
