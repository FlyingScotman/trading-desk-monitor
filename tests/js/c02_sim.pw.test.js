'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { TEXT: RUN } = require('./fixtures/concepts_run');

const pw = (() => { try { return require('playwright'); } catch { return null; } })();
const skip = !pw && 'нет Playwright';
const root = path.join(__dirname, '../../prototypes');
const origin = 'http://desk.test';
const pageUrl = origin + '/concepts/c02-risk-ladder.html';
const riskRx = /\/api\/v1\/runs\/(\d+)\/risk$/;

async function open(t, options = {}) {
  const browser = await pw.chromium.launch();
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1500, height: 900 } });
  const errors = [], requests = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });

  const D = JSON.parse(RUN);
  if (options.repr) await page.addInitScript(repr => localStorage.setItem('concepts-repr', repr), options.repr);
  const rub = D.positions.find(p => p.ccy === 'RUB' && p.instrType === 'BOND' && !p.notValued);
  const usd = D.positions.find(p => p.ccy === 'USD' && p.instrType === 'BOND' && !p.notValued);
  const scale = options.scale || 1;
  const coords = [['test-rub', rub, 'RUB RUONIA', '5Y', -10_000 * scale, 100 * scale], ['test-usd', usd, 'USD', '2Y', -20_000 * scale, 200 * scale]];
  D.positions = coords.map(([id, pos, curve, tenor, dv]) => ({
    ...pos, id, name: id, risk: { [curve]: D.BUCKETS.map(b => b === tenor ? dv : 0) }, cs01: 0, ofzSpr01: 0, extra: {},
  }));
  await page.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.origin !== origin) return route.abort();
    if (u.pathname === '/concepts-run.js') {
      const runId = Number(u.searchParams.get('run_id'));
      const run = { ...D, source: { ...D.source, runId } };
      return route.fulfill({ contentType: 'application/javascript', body: `window.CONCEPTS_RUN = ${JSON.stringify(run)};` });
    }
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: 'application/javascript', body: 'window.DESK_EXTRAS = {};' });
    const match = u.pathname.match(riskRx);
    if (match) {
      const runId = Number(match[1]);
      requests.push(runId);
      await options.wait;
      if (options.fail && requests.length === 1) return route.fulfill({ status: 503, json: {} });
      const secondOrder = runId !== 24;
      const buckets = runId === 26 ? D.BUCKETS.slice().reverse() : D.BUCKETS;
      return route.fulfill({ json: {
        run_id: runId, buckets, ...(secondOrder ? {} : { second_order: false }),
        factors: coords.map(([, , curve, tenor]) => `IR|${curve}|${tenor}`),
        positions: coords.map(([position_id, , , , dv, h], i) => ({
          position_id, spread_kind: null, delta: [[i, dv]], gamma: secondOrder && (runId !== 25 || i === 0) ? [[i, i, h]] : [],
        })),
      } });
    }
    if (u.pathname === '/api/v1/events/stream') return route.fulfill({ contentType: 'text/event-stream', body: 'event: hello\ndata: {}\n\n' });
    if (u.pathname.startsWith('/api/') || u.pathname === '/healthz/ready') return route.fulfill({ json: {} });
    return route.fulfill({ path: path.join(root, u.pathname) });
  });
  return { page, errors, requests };
}

const mode = (page, text) => page.locator('#sim [role=status]').getByText(text).waitFor();
const button = (page, text) => page.locator('#sim').getByRole('button', { name: text, exact: true });
async function result(page) {
  await page.waitForFunction(() => echarts.getInstanceByDom(document.querySelector('#sim .schart')));
  return page.evaluate(() => {
    const panel = document.querySelector('#sim');
    const chart = echarts.getInstanceByDom(panel.querySelector('.schart')).getOption();
    const texts = selector => [...panel.querySelectorAll(selector)].map(c => c.textContent.trim());
    const cvx = chart.series.find(s => s.name === 'выпуклость');
    return {
      total: texts('.bigpnl .dr')[0], comp: texts('.comp > span'), heads: texts('.stbl thead th'), foot: texts('.stbl tfoot td'),
      gammaStack: cvx?.stack, gammaByBucket: cvx && Object.fromEntries(chart.xAxis[0].data.map((b, i) => [b, cvx.data[i]])),
      stackedTotal: chart.series.filter(s => s.stack === 'p').flatMap(s => s.data).reduce((s, x) => s + x, 0),
    };
  });
}

test('C02 загружает /risk один раз и пересчитывает таблицу, график и итог при переключении и фильтрах', { skip }, async t => {
  let release;
  const { page, errors, requests } = await open(t, { wait: new Promise(r => { release = r; }) });
  const fmt = value => page.evaluate(v => KIT.f.mns(v, 1), value);
  try {
    await page.goto(pageUrl + '?run_id=23');
    await mode(page, 'линейно: загружаем вторые производные');
    assert.equal((await result(page)).total, await fmt(-3_000_000));
  } finally { release(); }
  await mode(page, 'с выпуклостью · 2-й порядок Тейлора');
  assert.ok((await button(page, '+ выпуклость').getAttribute('class')).includes('on'));
  const full = await result(page);
  const col = full.heads.indexOf('Выпукл.');
  assert.ok(col > 0);
  assert.equal(full.foot[col], await fmt(1_500_000));
  const typeValues = await page.locator(`#sim .stbl tbody td:nth-child(${col + 1}) .dr`).allTextContents();
  assert.deepEqual(typeValues.sort(), [await fmt(500_000), await fmt(1_000_000)].sort());
  assert.equal(full.total, await fmt(-1_500_000));
  assert.equal(full.stackedTotal, -1_500_000);
  assert.equal(full.gammaStack, 'p');
  assert.equal(full.gammaByBucket['2Y'], 1_000_000);
  assert.equal(full.gammaByBucket['5Y'], 500_000);
  const contribution = page.locator('#sim .comp > span').filter({ hasText: 'выпуклость' }).locator('.dr');
  await contribution.click();
  await page.locator('.k-drawer .t').getByText('Сдвиг · выпуклость', { exact: false }).waitFor();
  assert.match(await page.locator('.k-drawer .st').innerText(), /2 позиций дают вклад/);
  await page.keyboard.press('Escape');

  await button(page, 'линейно').click();
  const linear = await result(page);
  assert.equal(linear.total, await fmt(-3_000_000));
  assert.equal(linear.stackedTotal, -3_000_000);
  assert.ok(!linear.heads.includes('Выпукл.') && !linear.gammaStack && !linear.comp.some(s => s.includes('выпуклость')));
  await button(page, '+ выпуклость').click();
  await page.locator('#sim input[data-k=par]').fill('50');
  assert.equal((await result(page)).stackedTotal, -1_125_000);
  await page.locator('#sim input[data-k=par]').fill('100');
  await button(page, 'RUB').click();
  assert.equal((await result(page)).total, await fmt(-500_000));
  await button(page, 'все валюты').click();
  await page.evaluate(() => KIT.setFilter({ ccy: 'USD' }));
  assert.equal((await result(page)).total, await fmt(-1_000_000));
  await page.evaluate(() => KIT.setFilter({ ccy: null }));
  assert.deepEqual(requests, [23]);

  await page.goto(pageUrl + '?run_id=24');
  await mode(page, 'линейно: в этом прогоне нет вторых производных');
  const historical = await result(page);
  assert.equal(historical.total, await fmt(-3_000_000));
  assert.ok(!historical.gammaStack && !historical.heads.includes('Выпукл.'));
  assert.equal(await button(page, '+ выпуклость').count(), 0);
  assert.deepEqual(requests, [23, 24]);
  assert.deepEqual(errors, []);
});

test('C02 сохраняет линейный расчёт при сбое /risk и в демо', { skip }, async t => {
  const { page, errors, requests } = await open(t, { fail: true });
  await page.goto(pageUrl + '?run_id=23');
  await mode(page, 'линейно: вторые производные не загрузились');
  const failed = await result(page);
  assert.equal(failed.stackedTotal, -3_000_000);
  assert.ok(!failed.heads.includes('Выпукл.') && !failed.gammaStack);
  const banner = page.locator('.k-load-err').filter({ hasText: 'риск прогона' });
  assert.match(await banner.innerText(), /503/);
  assert.equal(await banner.getByRole('link', { name: 'Повторить' }).count(), 1);
  const runIds = await page.evaluate(async () => (await Promise.all([MOCK.loadRisk(), MOCK.loadRisk()])).map(r => r?.run_id));
  assert.deepEqual(runIds, [23, 23]);
  assert.equal(await page.evaluate(async () => (await MOCK.loadRisk())?.run_id), 23);
  assert.deepEqual(errors.splice(0), ['Failed to load resource: the server responded with a status of 503 (Service Unavailable)']);
  await page.goto(pageUrl + '?demo=1');
  await mode(page, 'линейно: демо без вторых производных');
  const demo = await result(page);
  assert.ok(Number.isFinite(demo.stackedTotal) && !demo.gammaStack);
  assert.deepEqual(requests, [23, 23]);
  assert.deepEqual(errors, []);
});

test('C02 показывает PnL в USD до загрузки /risk и в обоих режимах после загрузки', { skip }, async t => {
  let release;
  const { page, errors } = await open(t, { repr: 'USD', scale: 100, wait: new Promise(r => { release = r; }) });
  const assertReportTotal = async rub => {
    const expected = await page.evaluate(v => ({ repr: KIT.repr, value: v / MOCK.fx.USD, label: KIT.f.mns(v / MOCK.fx.USD, 1) }), rub);
    assert.equal(expected.repr, 'USD');
    const actual = await result(page);
    assert.equal(actual.total, expected.label);
    assert.ok(Math.abs(actual.stackedTotal - expected.value) < 1e-8);
    assert.match(await page.locator('#sim .bigpnl').innerText(), /mm \$/);
  };
  try {
    await page.goto(pageUrl + '?run_id=23');
    await mode(page, 'линейно: загружаем вторые производные');
    await assertReportTotal(-300_000_000);
  } finally { release(); }
  await mode(page, 'с выпуклостью · 2-й порядок Тейлора');
  await assertReportTotal(-150_000_000);
  await button(page, 'линейно').click();
  await assertReportTotal(-300_000_000);
  assert.deepEqual(errors, []);
});

test('C02 отмечает частичное покрытие и использует линейные факты при чужом порядке бакетов', { skip }, async t => {
  const { page, errors, requests } = await open(t);
  const note = page.locator('#sim [role=status]');
  await page.goto(pageUrl + '?run_id=25');
  await mode(page, '1 позиция — линейно, без вторых производных');
  assert.equal((await result(page)).stackedTotal, -2_500_000);
  await button(page, 'RUB').click();
  assert.doesNotMatch(await note.innerText(), /позиция — линейно/);
  await button(page, 'все валюты').click();
  await mode(page, '1 позиция — линейно, без вторых производных');
  await button(page, 'линейно').click();
  assert.doesNotMatch(await note.innerText(), /позиция — линейно/);

  await page.goto(pageUrl + '?run_id=26');
  await mode(page, 'линейно: вторые производные не загрузились');
  assert.match(await page.locator('.k-load-err').filter({ hasText: 'риск прогона' }).innerText(), /бакеты/);
  const invalid = await result(page);
  assert.equal(invalid.stackedTotal, -3_000_000);
  assert.equal(invalid.gammaStack, undefined);
  assert.deepEqual(requests, [25, 26]);
  assert.deepEqual(errors, []);
});
