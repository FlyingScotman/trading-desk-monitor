// #170, C01 «История» → «DV01 по кривым, история» в браузере (демо-данные, синтетика): выброс RUONIA на двух датах
// не задаёт шкалу — в линии разрыв, на датах пунктир, в шапке панели пометка; без выброса график как был.
// Run with Playwright on NODE_PATH.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const pw = (() => { try { return require('playwright'); } catch { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '../../prototypes/concepts/c01-cockpit.html')).href;
let browser;
test.before(async () => { if (pw) browser = await pw.chromium.launch(); });
test.after(async () => { if (browser) await browser.close(); });

const state = page => page.evaluate(() => {
  const w = KIT.charts.find(c => (c.raw.getOption().yAxis || [])[0]?.unit === 'тыс ₽/бп' || c.raw.getOption().series?.some(s => s.name === 'RUONIA'));
  const o = w.raw.getOption(), axis = w.raw.getModel().getComponent('yAxis').axis;
  const hd = [...document.querySelectorAll('.k-panel .hd')].find(h => h.textContent.includes('DV01 по кривым, история'));
  return { extent: axis.scale.getExtent(), ruo: o.series[0].data, marks: (o.series[0].markLine || {}).data || [],
    note: hd.querySelector('.k-note')?.textContent || '', dates: o.xAxis[0].data };
});

for (const spike of [false, true]) test(`#170 C01 история DV01 по кривым ${spike ? 'с выбросом' : 'без выброса'}`, { skip: !pw && 'Playwright unavailable' }, async () => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 800 } });
  try {
    await context.addInitScript(() => { localStorage.clear(); localStorage.setItem('c01-tab', 'risk'); });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await page.goto(PAGE);
    await page.waitForFunction(() => window.KIT && window.MOCK && document.querySelector('#tabs button'));
    const k = await page.evaluate(spike => {
      const r = MOCK.hist.dv01[MOCK.curves[0].id], k = Math.floor(r.length / 2);
      if (spike) { r[k] = 1e14; r[k + 1] = -2e14; }
      return k;
    }, spike);
    await page.click('#tabs button[data-k="hist"]');
    await page.waitForFunction(() => document.body.textContent.includes('DV01 по кривым, история') && KIT.charts.length > 0);
    const s = await state(page);
    const top = Math.max(Math.abs(s.extent[0]), Math.abs(s.extent[1]));
    assert.ok(top < 1e12, 'шкала по обычным датам: ' + s.extent);
    if (spike) {
      assert.equal(s.ruo[k], null); assert.equal(s.ruo[k + 1], null);
      assert.notEqual(s.ruo[k - 1], null);
      assert.deepEqual(s.marks.map(m => m.xAxis), [s.dates[k], s.dates[k + 1]]);
      assert.match(s.note, /выброс не в шкале/);
    } else {
      assert.ok(s.ruo.every(v => v != null), 'без выброса ряд целиком');
      assert.equal(s.marks.length, 0); assert.equal(s.note, '');
    }
    await page.hover('.k-panel .chart');
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
