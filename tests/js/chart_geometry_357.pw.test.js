// #357, bounded C06/C09 regression. Real ECharts elements, including line symbols (not only text).
// Run with Playwright on NODE_PATH; CHART_GEOMETRY_357_SHOTS saves screenshots before assertions.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { pathToFileURL } = require('node:url');
const pw = (() => { try { return require('playwright'); } catch { return null; } })();
const SHOTS = process.env.CHART_GEOMETRY_357_SHOTS;
const PAGE = name => pathToFileURL(path.join(__dirname, '../../prototypes/concepts', name)).href;
const ROOT = path.join(__dirname, '../../prototypes');
const { TEXT: RUN } = require('./fixtures/concepts_run');
let browser;
test.before(async () => { if (pw) browser = await pw.chromium.launch(); });
test.after(async () => { if (browser) await browser.close(); });

// Bounds in chart coordinates, with all parent transforms applied. getItemGraphicEl on a line is its symbol.
const geometry = (page, name) => page.evaluate(name => {
  const chart = name === 'c06'
    ? KIT.charts.find(c => c.raw.getOption().yAxis?.[0]?.data?.length === 12).raw
    : echarts.getInstanceByDom(document.getElementById('curves'));
  const bounds = el => {
    if (!el || el.ignore || el.invisible) return null;
    const r = el.getBoundingRect().clone(); r.applyTransform(el.getComputedTransform());
    return { x: r.x, y: r.y, width: r.width, height: r.height };
  };
  const opt = chart.getOption(), model = chart.getModel();
  const bars = [], markers = [];
  opt.series.forEach((series, si) => {
    const data = model.getSeriesByIndex(si).getData();
    for (let i = 0; i < data.count(); i++) {
      const g = data.getItemGraphicEl(i), label = g && g.getTextContent();
      const value = data.get(data.mapDimension(name === 'c06' ? 'x' : 'y'), i);
      if (value == null || !Number.isFinite(value)) continue;
      if (series.type === 'line') markers.push({ value, rect: bounds(g) });
      if (series.type === 'bar') bars.push({ value, rect: bounds(g), label: bounds(label), text: label && label.style.text,
        fill: label && label.style.fill, barColor: g && g.style.fill,
        position: series.data[i].label?.position || series.label.position });
    }
  });
  const texts = chart.getZr().storage.getDisplayList().filter(e => e.style?.text && bounds(e)).map(e => ({ text: e.style.text, rect: bounds(e) }));
  const axis = model.getComponent(name === 'c06' ? 'xAxis' : 'yAxis').axis;
  const grid = model.getComponent('grid').coordinateSystem.getRect();
  const ticks = axis.getViewLabels().filter(l => l.formattedLabel && texts.some(t => t.text === l.formattedLabel)).map(l => l.tickValue);
  const tile = chart.getDom().closest('.tile');
  const background = getComputedStyle(tile || chart.getDom().closest('.k-panel') || document.body).backgroundColor;
  const contrast = bars.filter(b => b.label).map(b => KIT.contrast(b.fill, /^inside/.test(b.position) ? b.barColor : background));
  return { extent: axis.scale.getExtent(), ticks, bars, markers, texts, contrast,
    grid: { x: grid.x, y: grid.y, width: grid.width, height: grid.height },
    width: chart.getWidth(), height: chart.getHeight(), values: opt.series.map(s => s.data.map(d => d && typeof d === 'object' ? d.value : d)) };
}, name);
const overlaps = (a, b, gap = 0) => a.x < b.x + b.width + gap && b.x < a.x + a.width + gap && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap;

// Same public synthetic server fixture as the other concept browser tests; no service or real data is read.
async function serveFixture(page) {
  const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' };
  await page.route('http://desk.test/**', route => {
    const u = new URL(route.request().url());
    if (u.pathname === '/concepts-run.js') return route.fulfill({ contentType: types['.js'], body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: types['.js'], body: 'window.DESK_EXTRAS = { notes: [] };' });
    const api = {
      '/api/v1/instruments/aliases': { aliases: [] }, '/api/v1/auth/state': { enabled: false, user: null },
      '/api/v1/autopilot/status': { latest_run: null }, '/api/v1/backfill/status': null,
      '/api/v1/views': { views: [] }, '/api/v1/events': { events: [], last_id: 0 },
      '/api/v1/history/market': null, '/api/v1/calendar/market': null,
    };
    if (Object.hasOwn(api, u.pathname)) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(api[u.pathname]) });
    if (u.pathname === '/api/v1/events/stream') return route.fulfill({ contentType: 'text/event-stream', body: 'event: hello\ndata: {"last_id":0}\n\n' });
    const file = path.join(ROOT, u.pathname);
    if (!file.startsWith(ROOT + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: types[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
  });
}

const basisCases = [
  { name: 'negative extremes', pts: [-90, -80, -70, -60, -50, -40, -30, -20, -10, -5, -2], change: 4 },
  { name: 'zero basis', pts: [-90, -80, -70, -60, -50, -40, -30, -20, -10, -2, 0], change: -4 },
  { name: 'zero adjacent signs above', pts: [-90, -80, -70, -60, -50, -40, -30, -20, -1, 0, 1], change: -4 },
  { name: 'zero adjacent signs below', pts: [-90, -80, -70, -60, -50, -40, -30, -20, -1, 0, 1], change: 4 },
];
for (const width of [1024, 390]) for (const scenario of basisCases) test(`#357 c09 ${scenario.name} ${width}px`, { skip: !pw && 'Playwright unavailable' }, async () => {
  const context = await browser.newContext({ viewport: { width, height: 768 } });
  try {
    await context.addInitScript(() => {
      localStorage.clear();
      localStorage.setItem('c09-state', JSON.stringify({ curve: 'basis' }));
    });
    const page = await context.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await page.goto(PAGE('c09-market.html'));
    await page.waitForFunction(() => window.KIT && KIT.charts.length > 0);
    await page.evaluate(({ pts, change }) => {
      const b = KIT.basisCurve();
      KIT.basisCurve = () => ({ ...b, pts, chg: pts.map(() => change) });
      KIT.charts.find(c => c.raw.getDom().id === 'curves').redraw();
    }, scenario);
    await page.waitForTimeout(500);
    if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `c09-${scenario.name.replaceAll(' ', '-')}-${width}.png`), fullPage: true });
    const g = await geometry(page, 'c09');
    assert.equal(g.bars.length, 11);
    assert.equal(g.markers.filter(m => m.rect).length, 11);
    assert.deepEqual(g.values, [scenario.pts, scenario.pts.map(v => v - scenario.change)], 'bar and yesterday values unchanged');
    if (scenario.pts.includes(0)) assert.equal(g.bars.find(b => b.value === 0).text, '0', 'zero remains labelled');
    await page.evaluate(() => KIT.charts.find(c => c.raw.getDom().id === 'curves').redraw());
    await page.waitForTimeout(200);
    const again = await geometry(page, 'c09');
    assert.deepEqual(again.bars.map(b => b.label), g.bars.map(b => b.label), 'repeat layout keeps the same absolute positions');
    for (const bar of g.bars) {
      assert.ok(bar.label, 'basis value remains labelled');
      assert.ok(bar.label.x >= -1 && bar.label.x + bar.label.width <= g.width + 1 && bar.label.y >= -1, 'label stays within chart');
      assert.ok(bar.label.y + bar.label.height <= g.grid.y + g.grid.height + 1, 'shifted label clears bottom axis');
      for (const marker of g.markers) assert.ok(!overlaps(bar.label, marker.rect, 1), JSON.stringify({ bar, marker }));
    }
    for (let i = 0; i < g.texts.length; i++) for (let j = i + 1; j < g.texts.length; j++) {
      assert.ok(!overlaps(g.texts[i].rect, g.texts[j].rect, -1), JSON.stringify([g.texts[i], g.texts[j]]));
    }
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

for (const source of ['demo', 'fixture']) for (const width of [1024, 1280, 1600, 390]) for (const theme of ['dark', 'light']) for (const repr of ['RUB', 'USD']) {
  for (const name of ['c06', 'c09']) test(`#357 ${name} ${width}px ${theme} ${repr} ${source}`, { skip: !pw && 'Playwright unavailable' }, async () => {
    const context = await browser.newContext({ viewport: { width, height: width === 390 ? 844 : width === 1024 ? 768 : 800 } });
    try {
      await context.addInitScript(({ theme, repr }) => {
        localStorage.clear();
        localStorage.setItem('concepts-theme', theme);
        localStorage.setItem('concepts-repr', repr);
        localStorage.setItem('c09-state', JSON.stringify({ curve: 'basis' }));
      }, { theme, repr });
      const page = await context.newPage(), errors = [];
      page.on('pageerror', e => errors.push(e.message));
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
      const file = name === 'c06' ? 'c06-launchpad.html' : 'c09-market.html';
      if (source === 'fixture') await serveFixture(page);
      await page.goto(source === 'fixture' ? 'http://desk.test/concepts/' + file : PAGE(file));
      await page.waitForFunction(() => window.KIT && KIT.charts.length > 0);
      await page.waitForTimeout(500);
      assert.deepEqual(await page.evaluate(() => [KIT.theme(), KIT.repr]), [theme, repr]);
      if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}-${width}-${theme}-${repr}-${source}.png`), fullPage: true });
      const g = await geometry(page, name), detail = JSON.stringify(g);
      if (name === 'c06') {
        assert.equal(g.bars.length, 12);
        const values = g.bars.map(b => b.value), span = Math.max(...values) - Math.min(...values);
        assert.ok((g.extent[1] - g.extent[0]) / span <= 1.5, 'axis span / data span <= 1.5: ' + detail);
        assert.ok(g.ticks.some(v => v < 0) && g.ticks.includes(0) && g.ticks.some(v => v > 0), 'visible negative, zero, positive ticks: ' + detail);
        g.bars.slice().sort((a, b) => Math.abs(b.value) - Math.abs(a.value)).slice(0, 4).forEach(b => assert.ok(b.label, 'top four labels remain visible: ' + detail));
        const expected = await page.evaluate(() => KIT.list().slice().sort((a, b) => Math.abs(b.pnl.total) - Math.abs(a.pnl.total)).slice(0, 12).reverse().map(p => p.pnl.total));
        assert.deepEqual(values, expected, 'contributions unchanged');
      } else {
        assert.ok(g.bars.some(b => b.value < 0) && g.bars.some(b => b.value > 0), 'exercise both signs');
        assert.equal(g.markers.filter(m => m.rect).length, g.bars.length, 'yesterday markers remain rendered');
        for (const bar of g.bars) {
          assert.ok(bar.label, 'basis label remains visible: ' + detail);
          if (bar.value < 0) assert.ok(bar.label.y >= g.grid.y - 1 && bar.label.y + bar.label.height <= g.grid.y + g.grid.height + 1, 'negative basis label stays within plot: ' + detail);
          for (const marker of g.markers) assert.ok(!overlaps(bar.label, marker.rect, 1), 'label clears rendered marker by at least 1px: ' + JSON.stringify({ bar, marker }));
          if (bar.value < 0 && !/^inside/.test(bar.position)) assert.ok(bar.label.y >= bar.rect.y + bar.rect.height, 'negative label stays below its endpoint');
          if (bar.value > 0 && !/^inside/.test(bar.position)) assert.ok(bar.label.y + bar.label.height <= bar.rect.y, 'positive label stays above its endpoint');
        }
        const expected = await page.evaluate(() => { const b = KIT.basisCurve(); return [b.pts, KIT.yest(b.pts, b.chg, 1)]; });
        assert.deepEqual(g.values, expected, 'bar and yesterday values unchanged');
      }
      for (const b of g.bars.filter(b => b.label)) {
        assert.ok(b.label.x >= -1 && b.label.x + b.label.width <= g.width + 1 && b.label.y >= -1 && b.label.y + b.label.height <= g.height + 1, 'label stays within chart: ' + detail);
      }
      assert.ok(g.contrast.every(c => c >= 4.5), 'label contrast >= 4.5: ' + detail);
      // Category, axis and bar texts keep the existing separation.
      for (let i = 0; i < g.texts.length; i++) for (let j = i + 1; j < g.texts.length; j++) {
        assert.ok(!overlaps(g.texts[i].rect, g.texts[j].rect, -1), 'text overlap: ' + JSON.stringify([g.texts[i], g.texts[j]]));
      }
      assert.deepEqual(errors, []);
    } finally { await context.close(); }
  });
}
