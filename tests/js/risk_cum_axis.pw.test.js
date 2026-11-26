'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { settle } = require('./fixtures/pw_pages');

const pw = (() => { try { return require('playwright'); } catch { return null; } })();
const skip = !pw && 'нет Playwright';
const KEY = 'concepts-risk-cum-axis';
const pages = ['c01-cockpit', 'c02-risk-ladder', 'c06-launchpad', 'c12-terminal', 'c13-issuers'];
const url = name => pathToFileURL(path.join(__dirname, '../../prototypes/concepts', name + '.html')).href;

let browser;
test.before(async () => { if (pw) browser = await pw.chromium.launch(); });
test.after(async () => { if (browser) await browser.close(); });

async function open(context, name, errors) {
  const page = await context.newPage();
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  await page.goto(url(name));
  return page;
}

async function axes(page, selector, expectedCount, lineAxis) {
  const charts = await page.locator(selector).evaluateAll(nodes => nodes.map(el => {
    const chart = echarts.getInstanceByDom(el), option = chart.getOption();
    const line = option.series.find(s => s.name === 'кумулятив');
    if (!line) return null;
    const model = chart.getModel();
    return {
      axes: option.yAxis.length,
      right: option.yAxis[1]?.position,
      line: line.yAxisIndex,
      others: model.getSeries().filter(s => s.name !== line.name).map(s => s.coordinateSystem.getAxis('y').index),
      types: option.series.map(s => s.type),
    };
  }).filter(Boolean));
  assert.equal(charts.length, expectedCount, selector);
  for (const chart of charts) {
    assert.equal(chart.axes, lineAxis + 1);
    assert.equal(chart.line, lineAxis);
    assert.equal(chart.right, lineAxis ? 'right' : undefined);
    assert.ok(chart.types.includes('bar') && chart.types.includes('scatter'));
    assert.ok(chart.others.every(i => i === 0));
  }
}

const charts = '[_echarts_instance_]';

for (const name of pages) test(`${name}: общая и отдельная ось риска`, { skip }, async () => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  try {
    const page = await open(context, name, errors);
    const count = name === 'c01-cockpit' ? await page.locator('.ccy-block').count()
      : name === 'c02-risk-ladder' ? await page.locator('#secs .sec').count()
      : name === 'c06-launchpad' ? 2 : 1;
    assert.ok(count > 0);
    assert.equal(await page.evaluate(key => localStorage.getItem(key), KEY), null);
    await axes(page, charts, count, 0);

    if (name === 'c02-risk-ladder') {
      await page.locator('#secs .wide').getByRole('button', { name: 'типы', exact: true }).click();
      await axes(page, '#secs .wide ' + charts, 1, 0);
    }

    await page.getByRole('button', { name: 'Вид', exact: true }).click();
    const menu = page.getByRole('dialog', { name: 'Вид' });
    assert.ok(await menu.getByText('Ось кумулятива', { exact: true }).isVisible());
    const same = menu.getByRole('button', { name: 'общая', exact: true });
    const split = menu.getByRole('button', { name: 'отдельная', exact: true });
    assert.equal(await same.getAttribute('aria-pressed'), 'true');
    await split.click();
    assert.equal(await page.evaluate(key => localStorage.getItem(key), KEY), 'split');
    await axes(page, charts, count, 1);

    await split.press('Escape');
    await page.locator('[data-a=theme]').click();
    await page.setViewportSize({ width: 820, height: 900 });
    await settle(page);
    await axes(page, charts, count, 1);

    await page.getByRole('button', { name: 'Вид', exact: true }).click();
    assert.equal(await split.getAttribute('aria-pressed'), 'true');
    await same.click();
    assert.equal(await page.evaluate(key => localStorage.getItem(key), KEY), 'same');
    await same.press('Escape');
    await page.locator('[data-a=theme]').click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await settle(page);
    await axes(page, charts, count, 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});

test('K.riskStack: сохранённый режим, переопределение оси и синхронизация вкладок', { skip }, async () => {
  const context = await browser.newContext();
  const errors = [];
  try {
    await context.addInitScript(key => localStorage.setItem(key, 'split'), KEY);
    const page = await open(context, 'c01-cockpit', errors);
    await page.evaluate(() => {
      for (const [id, axis] of [['default', undefined], ['same', true], ['split', false]]) {
        const el = document.createElement('div');
        el.id = 'risk-axis-' + id;
        el.style.height = '300px';
        document.body.appendChild(el);
        KIT.riskStack(el, { curves: ['RUB RUONIA'], cumSameAxis: axis });
      }
    });
    await axes(page, '#risk-axis-default', 1, 1);
    await axes(page, '#risk-axis-same', 1, 0);
    await axes(page, '#risk-axis-split', 1, 1);

    const mirror = await open(context, 'c01-cockpit', errors);
    const count = await mirror.locator('.ccy-block').count();
    assert.ok(count > 0);
    await axes(mirror, charts, count, 1);
    await mirror.getByRole('button', { name: 'Вид', exact: true }).click();
    const same = mirror.getByRole('dialog', { name: 'Вид' }).getByRole('button', { name: 'общая', exact: true });

    await page.evaluate(() => KIT.setRiskCumAxis('same'));
    await axes(page, '#risk-axis-default', 1, 0);
    await axes(page, '#risk-axis-same', 1, 0);
    await axes(page, '#risk-axis-split', 1, 1);
    await mirror.waitForFunction(() => KIT.riskCumAxis === 'same');
    assert.equal(await same.getAttribute('aria-pressed'), 'true');
    await axes(mirror, charts, count, 0);
    assert.deepEqual(errors, []);
  } finally { await context.close(); }
});
