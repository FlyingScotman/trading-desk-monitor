// Графики на большой книге (TDM#630, K.chart в prototypes/concepts/lib/kit.js): график создаётся, когда его элемент подходит к
// экрану, по одному в задаче; за экраном и в скрытой вкладке — не создаётся, пока его не покажут. До создания работают on и
// setOption (ждут), redraw и update (берут описание при создании), dispose (график не создаётся вовсе), raw (создаёт сразу).
// Порог «большой книги» (KIT.CHART_LAZY_MIN) занижен до 1, чтобы демо-набор шёл тем же путём.
//   node --test tests/js/chart_lazy_630.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c01-cockpit.html')).href;

test('K.chart: график создаётся у экрана по одному в задаче, за экраном — по прокрутке; ожидание и dispose до создания', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1200, height: 800 } }), pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
    await pg.goto(PAGE);
    await pg.waitForFunction(() => window.KIT && window.echarts && window.MOCK);
    const r = await pg.evaluate(async () => {
      const K = window.KIT; K.CHART_LAZY_MIN = 1;
      const wrap = document.createElement('div'); wrap.style.cssText = 'position:absolute;left:0;top:0;width:600px';
      const mk = (top, id) => { const d = document.createElement('div'); d.id = id; d.style.cssText = 'position:absolute;left:0;top:' + top + 'px;width:500px;height:200px'; wrap.appendChild(d); return d; };
      document.body.appendChild(wrap);
      const opt = v => () => ({ xAxis: { type: 'category', data: ['a', 'b'] }, yAxis: { type: 'value' }, series: [{ type: 'bar', data: [v, v + 1] }] });
      const a = mk(100, 'ca'), b = mk(350, 'cb'), far = mk(6000, 'cfar'), gone = mk(500, 'cgone'), raw = mk(6300, 'craw');
      const inst = el => !!echarts.getInstanceByDom(el);
      const ca = K.chart(a, opt(1)), cb = K.chart(b, opt(5)), cfar = K.chart(far, opt(7)), cgone = K.chart(gone, opt(9));
      const clicks = []; ca.on('click', () => clicks.push(1));  // до создания — ждёт
      const out = { syncAfterCall: [inst(a), inst(b)], pendingNow: K.chartsPending() };
      cgone.dispose();  // до создания — графика не будет вовсе
      const t0 = performance.now(); let seen = 0;
      while (K.chartsPending() > 0 && performance.now() - t0 < 5000) { await new Promise(r => setTimeout(r, 20)); }
      out.nearCreated = [inst(a), inst(b)]; out.farCreated = inst(far); out.goneCreated = inst(gone); out.pendingAfter = K.chartsPending();
      out.opts = [ca.raw.getOption().series[0].data, cb.raw.getOption().series[0].data];
      // raw создаёт график сразу, даже за экраном
      const craw = K.chart(raw, opt(3)); out.rawSync = inst(raw); void craw.raw; out.rawAfter = inst(raw);
      // update до создания — описание берётся при создании
      cfar.update(opt(70)); out.farStill = inst(far);
      window.scrollTo(0, 5700); document.documentElement.scrollTop = 5700; document.body.scrollTop = 5700;
      const t1 = performance.now(); while (!inst(far) && performance.now() - t1 < 5000) await new Promise(r => setTimeout(r, 20));
      out.farAfterScroll = inst(far); out.farData = inst(far) ? cfar.raw.getOption().series[0].data : null;
      return out;
    });
    assert.deepStrictEqual(r.syncAfterCall, [false, false], 'сразу после вызова графики не созданы');
    assert.ok(r.pendingNow >= 2, 'у экрана ждут создания: ' + r.pendingNow);
    assert.deepStrictEqual(r.nearCreated, [true, true], 'графики у экрана созданы');
    assert.strictEqual(r.farCreated, false, 'график за экраном не создан');
    assert.strictEqual(r.goneCreated, false, 'dispose до создания: графика нет');
    assert.strictEqual(r.pendingAfter, 0);
    assert.deepStrictEqual(r.opts, [[1, 2], [5, 6]]);
    assert.strictEqual(r.rawSync, false, 'график за экраном не создан сам');
    assert.strictEqual(r.rawAfter, true, 'raw создаёт график сразу');
    assert.strictEqual(r.farStill, false, 'update не создаёт график');
    assert.strictEqual(r.farAfterScroll, true, 'график создан, когда к нему доехали');
    assert.deepStrictEqual(r.farData, [70, 71], 'описание — последнее, из update');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
