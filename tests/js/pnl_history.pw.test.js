// C19 «PnL за период» in the browser (Playwright; skipped without it).
//  1. Without a server: the pages from the files, a run from tests/js/fixtures/concepts_run.js, /api/v1/history/pnl/* (screens A–F)
//     answered from the synthetic tests/js/fixtures/pnl_period.json — screens A, B, C, the request parameters, the
//     latest request winning (no stale result while loading), «—» for missing data, 390 px, both themes, no errors.
//  2. With PNL_HISTORY_BASE=http://127.0.0.1:<port> (desk serve on a synthetic store, e.g. test_pnl_history.seed_history):
//     the real /period answer renders on A, B, C without console errors.
//   node --test tests/js/pnl_history.pw.test.js        PNL_HISTORY_SHOTS=<dir> also saves screenshots
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const FX = require('./fixtures/pnl_period.json');
const BASE = process.env.PNL_HISTORY_BASE, SHOTS = process.env.PNL_HISTORY_SHOTS;
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bnull\b|\bundefined\b/;

async function open(browser, { width = 1440, theme = 'dark', delay } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(t => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); } catch (e) { } }, theme);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) }).catch(() => { });  // an aborted (superseded) request
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname.startsWith('/api/v1/history/pnl/')) calls.push(u);
    if (u.pathname === '/api/v1/history/pnl/period') {
      const d = delay ? await delay(u) : null;
      return json(d || FX.period);
    }
    if (u.pathname === '/api/v1/history/pnl/position') return json(FX.position[u.searchParams.get('position_book') + '|' + u.searchParams.get('position_id')]);
    if (u.pathname === '/api/v1/history/pnl/curves') return json(FX.curves);
    if (u.pathname === '/api/v1/history/pnl/checks') return json(FX.checks);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c19-pnl-history.html');
  await pg.waitForSelector('.k-kpis');
  return { pg, ctx, errors, calls };
}
const mn = (pg, v) => pg.evaluate(x => KIT.f.mn(x, 1), v);
// the screen is drawn in the same task as its tab is marked (hashchange → renderScreen)
const tab = async (pg, key) => { await pg.locator('#tabs [data-tab=' + key + ']').click(); await pg.waitForSelector('#tabs [data-tab=' + key + '].on'); };
const wfNames = pg => pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('wf')).getOption().xAxis[0].data);

test('C19 «PnL за период» on the synthetic fixture: A, B, C, requests, stale guard, missing data, widths', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls } = await open(browser);
    const S = FX.period.summary;
    // the request: MTD of the run's date, auto granularity, the run, the catalogue for the filter menus
    const q = calls.find(u => u.pathname.endsWith('/period')).searchParams;
    assert.equal(q.get('from'), '2026-10-01'); assert.equal(q.get('to'), '2026-10-13'); assert.equal(q.get('gran'), 'day');
    assert.ok(q.get('run_id')); assert.equal(q.get('catalogue'), 'true');
    // A: tiles, waterfall, contributors, the table by periods
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(await mn(pg, S.total)));
    assert.deepEqual((await wfNames(pg)).slice(0, 4), ['Кэрри нетто', 'Ставки (линейно)', 'Выпуклость (ставки)', 'Ставки (без линейной оценки)']);
    assert.ok(!(await wfNames(pg)).includes('Выпуклость метода'), 'flash convexity 0: hidden');
    assert.equal((await wfNames(pg)).at(-1), 'Итог');
    // FX only net (owner, 28.10.2026): no asset / liability FX as separate sources anywhere on the page
    const NO_SPLIT_FX = /FX актива|FX фондирования|актив [(\d]/;
    assert.ok((await wfNames(pg)).includes('FX (нетто ОВП)'));
    assert.doesNotMatch(await pg.locator('#main').innerText(), NO_SPLIT_FX);
    assert.ok((await pg.locator('#bt tbody tr').count()) >= 7);
    assert.equal(await pg.locator('#bt thead th').count(), FX.period.buckets.length + 2);
    await pg.locator('#netA').uncheck();
    assert.deepEqual((await wfNames(pg)).slice(0, 3), ['Кэрри брутто', 'Фондирование', 'Ставки (линейно)']);
    await pg.locator('#lensSeg button', { hasText: 'Explain по кривым' }).click();
    assert.ok((await wfNames(pg)).includes('Ставки · кривая не указана'));
    // attribution per row (TDM#432): rows without the coupon split keep their carry apart, nothing blanks the rest
    await pg.locator('#btSeg button', { hasText: 'Атрибуция' }).click();
    const attrRows = await pg.locator('#bt tbody tr').allInnerTexts();
    assert.deepEqual(attrRows.map(t => t.split('\t')[0].replace(/\s+\d+ из \d+ записей$/, '').trim()), ['Цена (чистая)', 'Купон / НКД', 'Кэрри без разбивки купона', 'FX (нетто ОВП)', 'Фондирование', 'Сделки']);
    assert.match(attrRows[1], /\d+ из \d+ записей/);
    assert.ok(attrRows.slice(0, 3).every(t => t.split('\t').slice(1).every(x => x.trim() !== '—')), attrRows.join('|'));
    await pg.locator('#lensSeg button', { hasText: 'Книги' }).click();
    assert.deepEqual((await wfNames(pg)).slice(0, -1).sort(), ['DEMO-A', 'DEMO-B']);
    assert.match(await pg.locator('#contribTitle').textContent(), /Итог/);
    assert.ok(!BAD.test(await pg.locator('#main').innerText()));
    // a contributor opens E with the position
    const contributor = pg.locator('#contrib [data-ref]').first(), cref = await contributor.getAttribute('data-ref');
    await contributor.click();
    await pg.waitForSelector('#tabs [data-tab=position].on');
    assert.equal(await pg.evaluate(() => location.hash), '#position');
    await pg.waitForSelector('#pDays table');
    assert.equal(await pg.locator('#plist [aria-current=true]').getAttribute('data-ref'), cref);
    // B: a position cell asks /position for that position and shows its largest days
    await tab(pg, 'matrix');
    await pg.locator('#mt tbody tr:not(.grp) td.cell').first().click();
    await pg.waitForSelector('#cellDays table');
    const pq = calls.filter(u => u.pathname.endsWith('/position')).at(-1).searchParams;
    assert.ok(pq.get('position_book') && pq.get('position_id')); assert.equal(pq.get('from'), '2026-10-01');
    await pg.locator('#mlSeg button', { hasText: 'Атрибуция' }).click();
    assert.ok((await pg.locator('#mt thead').innerText()).includes('Цена (чистая)'));
    for (const lens of ['Атрибуция', 'Explain']) { await pg.locator('#mlSeg button', { hasText: lens }).click(); assert.doesNotMatch(await pg.locator('#main').innerText(), NO_SPLIT_FX, lens); }
    // C: the split per row — coupon of the split rows with its coverage, the carry of the others on its own tile; the
    // swap has no funding % and no funding
    await tab(pg, 'carry');
    assert.match(await pg.locator('.k-kpi').first().innerText(), /\d[\s\S]*разбивка у \d+ из \d+ записей с кэрри/);
    assert.match(await pg.locator('.k-kpis').innerText(), /Без разбивки купона/i);
    const irs = await pg.locator('#ct tr', { hasText: 'Synthetic IRS pay' }).innerText();
    assert.ok(irs.split('\t').filter(t => t.trim() === '—').length >= 4, irs);
    await pg.locator('#cm1 button', { hasText: 'Брутто' }).click();
    const names = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('cc1')).getOption().series.map(s => s.name));
    assert.deepEqual(names, ['Купон / НКД', 'Амортизация к номиналу', 'Кэрри без разбивки купона', 'Roll-down', 'Кэрри брутто']);
    // the rate decision of the fixture takes effect on 05.10: a mark on «Кэрри в день»
    const marks = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('cc2')).getOption().series.flatMap(x => ((x.markLine || {}).data || []).map(m => m.xAxis)));
    assert.deepEqual(marks, ['2026-10-05']);
    // granularity is a request parameter
    await tab(pg, 'decomp');
    await pg.locator('#granSeg button', { hasText: 'Месяцы' }).click();
    await pg.waitForSelector('.k-kpis');
    assert.equal(calls.filter(u => u.pathname.endsWith('/period')).at(-1).searchParams.get('gran'), 'month');
    // widths and themes: no horizontal scroll of the page on any of A, B, C
    for (const theme of ['dark', 'light']) {
      await pg.evaluate(t => { if (KIT.theme() !== t) KIT.toggleTheme(); }, theme);
      for (const width of [1440, 390]) {
        await pg.setViewportSize({ width, height: 900 });
        for (const key of ['decomp', 'matrix', 'carry']) {
          await tab(pg, key); await pg.waitForTimeout(150);
          assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth), width, key + ' ' + theme + ' ' + width);
          if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, `c19-${key}-${theme}-${width}.png`), fullPage: true }); }
        }
      }
    }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

// the fixture's period by calendar days (as the server's gran=day buckets): positions' buckets from their /position days
function dayPeriod() {
  const d = JSON.parse(JSON.stringify(FX.period)), B = [];
  for (let t = Date.parse(d.from + 'T00:00:00Z'); t <= Date.parse(d.to + 'T00:00:00Z'); t += 864e5) { const x = new Date(t).toISOString().slice(0, 10); B.push({ key: x, label: x.slice(8, 10) + '.' + x.slice(5, 7) + '.' + x.slice(0, 4), from: x, to: x }); }
  d.gran = 'day'; d.buckets = B;
  d.positions.forEach(p => { const by = new Map(FX.position[p.ref].days.map(x => [x.date, x.total])); p.buckets = B.map(b => (by.has(b.from) ? by.get(b.from) : null)); });
  return d;
}

test('C19 acceptance (TDM#431–#435): bars split by groups with a drill list, nulls last, per-row attribution, working-day axis', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const DAY = dayPeriod();
    const { pg, ctx, errors } = await open(browser, { delay: async u => (u.searchParams.get('gran') === 'day' ? DAY : null) });
    const wf = () => pg.evaluate(() => { const o = echarts.getInstanceByDom(document.getElementById('wf')).getOption(); return { names: o.xAxis[0].data, data: o.series[0].data }; });
    // #431: «Разбивка столбцов» — each bar a stack of the currencies' shares; a piece opens the drill of its group
    await pg.locator('#wfBreak').selectOption('ccy');
    assert.match(await pg.locator('#wfLegend').innerText(), /Валюта:[\s\S]*RUB[\s\S]*USD/);
    const at = await pg.evaluate(() => {
      const el = document.getElementById('wf'), c = echarts.getInstanceByDom(el), row = c.getOption().series[0].data[0], r = el.getBoundingClientRect();
      const [x, y] = c.convertToPixel({ seriesIndex: 0 }, [0, row[1] / 2]);  // inside the larger, negative piece of «Кэрри нетто»
      return [r.left + x, r.top + y];
    });
    await pg.mouse.click(at[0], at[1]);
    await pg.waitForFunction(() => /Кто дал: Кэрри нетто · RUB/.test(document.getElementById('contribTitle').textContent));
    const drill = await pg.locator('#contrib').innerText();
    assert.match(drill, /desk-flash-exact-v1/); assert.doesNotMatch(drill, /Synthetic USD/);
    assert.match(drill, /не посчитано: 1 поз\. — нет PnL: no_prev_report/);
    const vals = await pg.locator('#contrib .contrib > .n').allInnerTexts();
    const abs = vals.map(t => Math.abs(parseFloat(t.replace(/[()]/g, '').replace(',', '.').replace('−', '-'))));
    assert.ok(abs.every((v, i) => !i || abs[i - 1] >= v), vals.join('|'));
    if (process.env.PNL_HISTORY_SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c19-A-breakdown-ccy.png'), fullPage: true }); }
    // a mixed-sign bar keeps its total: the axis spans the stack, the label is the bar's net
    await pg.locator('#wfBreak').selectOption('type');
    assert.match(await pg.locator('#wfLegend').innerText(), /Тип:/);
    // group lenses: the breakdown is off; a position without a value is last, «не посчитано: N поз.»
    await pg.locator('#lensSeg button', { hasText: 'Позиции' }).click();
    assert.equal(await pg.locator('#wfBreak').isDisabled(), true);
    const pos = await wf();
    assert.equal(pos.names.at(-2), 'не посчитано: 1 поз.'); assert.equal(pos.names.at(-1), 'Итог');
    // #433: the drill opens E on the clicked position
    await pg.locator('#lensSeg button', { hasText: 'Explain' }).first().click();
    await pg.locator('#contrib [data-ref]').first().click();
    await pg.waitForSelector('#tabs [data-tab=position].on');
    await tab(pg, 'decomp');
    // #435: rates of RUB bonds split into linear and convexity; the tile says so
    assert.match(await pg.locator('.k-kpi').nth(2).innerText(), /линейно[\s\S]*выпукл\./);
    // #434: by days — working days only, the day without a run marked and carried, the coverage in the header
    await pg.locator('#granSeg button', { hasText: 'Дни' }).click();
    await pg.waitForSelector('#ts');
    assert.match(await pg.locator('#tsCov').innerText(), /прогонов 11 из 12 рабочих дней/);
    const ts = await pg.evaluate(() => { const o = echarts.getInstanceByDom(document.getElementById('ts')).getOption(), tot = o.series.at(-1); return { x: o.xAxis[0].data, y: tot.data, area: (tot.markArea || {}).data || [] }; });
    assert.ok(!ts.x.some(d => [0, 6].includes(new Date(d + 'T00:00:00Z').getUTCDay())), 'no weekends on the axis');
    const i7 = ts.x.indexOf('2026-10-07');
    assert.ok(i7 > 0 && ts.y[i7] === ts.y[i7 - 1], 'the level carries over the day without a run');
    assert.deepEqual(ts.area.map(a => [a[0].xAxis, a[0].name]), [['2026-10-07', 'нет прогона']]);
    assert.equal(await pg.locator('#bt thead th').count(), ts.x.length + 2, 'day columns: working days only');
    assert.match(await pg.locator('#bt').innerText(), /нет прогона/);
    if (process.env.PNL_HISTORY_SHOTS) await pg.screenshot({ path: path.join(SHOTS, 'c19-A-days-cumulative.png'), fullPage: true });
    await pg.locator('#modeSeg button', { hasText: 'По периодам' }).click();
    const per = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('ts')).getOption().xAxis[0].data);
    assert.ok(!per.some(l => ['03.10.2026', '04.10.2026', '11.10.2026'].includes(l)), per.join());
    await pg.locator('#modeSeg button', { hasText: 'Накопленно' }).click();
    // attribution of the bucket table and of B: per row, with the coverage of the coupon
    await pg.locator('#btSeg button', { hasText: 'Атрибуция' }).click();
    if (process.env.PNL_HISTORY_SHOTS) await pg.locator('#bt').screenshot({ path: path.join(SHOTS, 'c19-A-attribution-table.png') });
    await tab(pg, 'matrix');
    await pg.locator('#mlSeg button', { hasText: 'Атрибуция' }).click();
    assert.match(await pg.locator('#mt thead').innerText(), /Кэрри без разбивки купона/);
    if (process.env.PNL_HISTORY_SHOTS) await pg.screenshot({ path: path.join(SHOTS, 'c19-B-attribution.png'), fullPage: true });
    await tab(pg, 'carry');
    const cc1 = await pg.evaluate(() => { const o = echarts.getInstanceByDom(document.getElementById('cc1')).getOption(); return ((o.series.at(-1).markArea || {}).data || []).map(a => a[0].xAxis); });
    assert.deepEqual(cc1, ['2026-10-07']);
    // nulls last in the carry table (TDM#433): sort by carry % p.a., the deal (no %) at the bottom
    await pg.locator('#ct th[data-c=cy]').click();
    assert.match((await pg.locator('#ct tbody tr').allInnerTexts()).at(-1), /Synthetic IRS pay|Synthetic New 1/);
    if (process.env.PNL_HISTORY_SHOTS) await pg.screenshot({ path: path.join(SHOTS, 'c19-C-carry.png'), fullPage: true });
    assert.ok(!BAD.test(await pg.locator('#main').innerText()));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19 D, E, F on the synthetic fixture: requests, missing data, F → E on its day, themes, 390 px', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls } = await open(browser);
    const NO_SPLIT_FX = /FX актива|FX фондирования/;
    const last = p => calls.filter(u => u.pathname.endsWith(p)).at(-1);
    // D: /curves with the period and filters (no gran); move signed without brackets; linear by term not saved — «нет данных»
    await tab(pg, 'curves');
    await pg.waitForSelector('#cvLin table');
    const cq = last('/curves').searchParams;
    assert.equal(cq.get('from'), '2026-10-01'); assert.ok(cq.get('run_id')); assert.equal(cq.get('gran'), null);
    const move = await pg.locator('table[data-vk=c19-period-curve-move] tbody tr').first().innerText();
    assert.match(move, /−15/); assert.doesNotMatch(move, /\(/);
    assert.match(await pg.locator('#cvBody .grids3').innerText(), /нет данных: линейный PnL по срокам[\s\S]*TDM#406/);
    assert.equal(await pg.locator('#cvLin tbody tr').count(), 3);
    const cum = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('cvCum')).getOption().series.map(s => s.name));
    assert.deepEqual(cum.sort(), ['RUONIA', 'ОФЗ', 'кривая не указана'].sort());
    // E: /position of the opened position; ▲ from /checks; header; inputs D−1; a row marks the day on the charts
    await pg.locator('#cvPos [data-ref="DEMO-A|OFZ-1"]').click();
    await pg.waitForSelector('#pDays table');
    const pq = last('/position').searchParams;
    assert.equal(pq.get('position_book'), 'DEMO-A'); assert.equal(pq.get('position_id'), 'OFZ-1');
    await pg.waitForSelector('#plist [data-ref="DEMO-A|CORP-1"] .sevtxt');
    assert.equal(await pg.locator('#plist [data-ref="DEMO-A|OFZ-1"] .sevtxt').count(), 0);
    assert.match(await pg.locator('#pHead').innerText(), /есть в другой книге: DEMO-B/);
    const days = await pg.locator('#pDays').innerText();
    assert.match(days, /по прогону D−1/); assert.match(days, /из расчёта/);
    assert.ok((await pg.locator('#pAttr .attrbar div').count()) >= 2);
    await pg.locator('#pDays tr[data-d="2026-10-06"]').click();
    assert.equal(await pg.locator('#pDays tr.sel').getAttribute('data-d'), '2026-10-06');
    const marks = await pg.evaluate(() => ['pCh', 'pPrice', 'pYld'].map(id => echarts.getInstanceByDom(document.getElementById(id)).getOption().series.flatMap(s => ((s.markLine || {}).data || []).map(m => m.xAxis))));
    assert.deepEqual(marks, [['2026-10-06'], ['2026-10-06'], ['2026-10-06']]);
    // a position without the coupon split: its carry apart in the attribution (TDM#432); a swap: no price, no yield charts
    await pg.locator('#plist [data-ref="DEMO-A|CORP-1"]').click();
    await pg.waitForSelector('#pAttr .attrbar');
    assert.match(await pg.locator('#pAttr').innerText(), /Кэрри без разбивки купона/);
    assert.doesNotMatch(await pg.locator('#pAttr').innerText(), /нет данных/);
    await pg.locator('#plist [data-ref="DEMO-B|IRS-1"]').click();
    await pg.waitForSelector('#pDays table');
    assert.match(await pg.locator('#pBody').innerText(), /нет данных: у позиции нет чистой цены/);
    // F: /checks with the default thresholds; cards filter the list; a new threshold asks again
    await tab(pg, 'control');
    await pg.waitForSelector('#ckList table');
    const kq = last('/checks').searchParams;
    assert.equal(kq.get('thr_abs'), '3000000'); assert.equal(kq.get('thr_pct'), '0.25');
    assert.equal(await pg.locator('.check').count(), FX.checks.checks.length);
    assert.match(await pg.locator('.check[data-k=stale]').innerText(), /нет данных/);
    assert.equal(await pg.locator('#ckList tbody tr').count(), FX.checks.findings.length);
    await pg.locator('.check[data-k=fxrev]').click();
    assert.equal(await pg.locator('#ckList tbody tr').count(), 1);
    assert.match(await pg.locator('#ckList').innerText(), /валюта USD/);
    await pg.locator('#ckAll').click();
    assert.equal(await pg.locator('#ckList tbody tr').count(), FX.checks.findings.length);
    assert.equal(await pg.locator('#ckCal i.cal-norun').count(), 1);
    await pg.locator('#thrAbs').fill('1.5'); await pg.locator('#thrAbs').dispatchEvent('change');
    await pg.waitForSelector('#ckList table');
    assert.equal(last('/checks').searchParams.get('thr_abs'), '1500000');
    // a heat-map cell opens E on that position and day
    const cellAt = await pg.evaluate(() => {
      const el = document.getElementById('ckHeat'), c = echarts.getInstanceByDom(el), o = c.getOption(), y = o.yAxis[0].data.findIndex(n => n.startsWith('Synthetic Corp 1')), x = o.xAxis[0].data.indexOf('2026-10-09');
      const [px, py] = c.convertToPixel({ seriesIndex: 0 }, [x, y]), r = el.getBoundingClientRect();
      return [r.left + px, r.top + py];
    });
    await pg.mouse.click(cellAt[0], cellAt[1]);
    await pg.waitForSelector('#tabs [data-tab=position].on');
    await pg.waitForSelector('#pDays tr.sel');
    assert.equal(await pg.locator('#plist [aria-current=true]').getAttribute('data-ref'), 'DEMO-A|CORP-1');
    assert.equal(await pg.locator('#pDays tr.sel').getAttribute('data-d'), '2026-10-09');
    assert.match(await pg.locator('#pDays tr.sel').innerText(), /Необъяснённое: /);
    // a finding of a position opens E on its day
    await tab(pg, 'control');
    await pg.waitForSelector('#ckList table');
    await pg.locator('#ckList [data-ref="DEMO-B|USD-1"]').first().click();
    await pg.waitForSelector('#pDays tr.sel');
    assert.equal(await pg.locator('#pDays tr.sel').getAttribute('data-d'), '2026-10-09');
    assert.match(await pg.locator('#pDays tr.sel').getAttribute('class'), /critrow/);
    // themes and widths: no page-wide scroll, no NaN / null / undefined, FX only net
    for (const theme of ['dark', 'light']) {
      await pg.evaluate(t => { if (KIT.theme() !== t) KIT.toggleTheme(); }, theme);
      for (const width of [1440, 390]) {
        await pg.setViewportSize({ width, height: 900 });
        for (const key of ['curves', 'position', 'control']) {
          await tab(pg, key); await pg.waitForSelector(key === 'curves' ? '#cvLin table' : key === 'position' ? '#pDays table' : '#ckList table'); await pg.waitForTimeout(150);
          assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth), width, key + ' ' + theme + ' ' + width);
          const text = await pg.locator('#main').innerText();
          assert.ok(!BAD.test(text), key + ' ' + theme + ' ' + width); assert.doesNotMatch(text, NO_SPLIT_FX, key);
          if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, `c19-${key}-${theme}-${width}.png`), fullPage: true }); }
        }
      }
    }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19 D, E, F: a held-back answer never shows for another slice; a failed request says so', { skip: pw ? false : 'нет Playwright', timeout: 60000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    let release;
    const gate = new Promise(r => { release = r; });
    const { pg, ctx, errors } = await open(browser);
    // /position of OFZ-1 is held back; the user moves on to CORP-1 before it arrives
    await pg.route('**/api/v1/history/pnl/position?*', async route => {
      const u = new URL(route.request().url()), ref = u.searchParams.get('position_book') + '|' + u.searchParams.get('position_id');
      if (ref === 'DEMO-A|OFZ-1') await gate;
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(FX.position[ref]) }).catch(() => { });
    });
    await pg.route('**/api/v1/history/pnl/curves?*', route => route.fulfill({ status: 500, body: '' }));
    await pg.evaluate(() => { location.hash = 'position'; });
    await pg.waitForSelector('#plist [data-ref="DEMO-A|OFZ-1"]');
    await pg.locator('#plist [data-ref="DEMO-A|OFZ-1"]').click();
    await pg.waitForFunction(() => /Загружаю дни позиции/.test(document.getElementById('pBody').textContent));
    assert.equal(await pg.locator('#pDays').count(), 0, 'no previous position while loading');
    await pg.locator('#plist [data-ref="DEMO-A|CORP-1"]').click();
    await pg.waitForSelector('#pDays table');
    release(); await pg.waitForTimeout(400);
    assert.match(await pg.locator('#pHead').innerText(), /Synthetic Corp 1/);
    assert.equal(await pg.locator('#plist [aria-current=true]').getAttribute('data-ref'), 'DEMO-A|CORP-1');
    await tab(pg, 'curves');
    await pg.waitForFunction(() => /HTTP 500/.test(document.getElementById('cvBody').textContent));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19: the latest request wins and a loading period never shows the previous result', { skip: pw ? false : 'нет Playwright', timeout: 60000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    let release;
    const gate = new Promise(r => { release = r; });
    const stale = JSON.parse(JSON.stringify(FX.period)); stale.summary.total += 7e6;
    const fresh = JSON.parse(JSON.stringify(FX.period)); fresh.summary.total -= 3e6;
    // the YTD answer is held back and arrives after the 3М one
    const { pg, ctx, errors } = await open(browser, { delay: async u => {
      if (u.searchParams.get('from') === '2026-01-01') { await gate; return stale; }
      if (u.searchParams.get('from') === '2026-07-14') return fresh;
      return null;
    } });
    await pg.locator('#preset button', { hasText: 'YTD' }).click();
    await pg.waitForFunction(() => /Загружаю/.test(document.getElementById('status').textContent));
    assert.equal(await pg.locator('#main').innerHTML(), '');
    assert.equal(await pg.locator('#coveragePanel').isVisible(), false);
    await pg.locator('#preset button', { hasText: '3М' }).click();
    const want = await mn(pg, fresh.summary.total);
    await pg.waitForFunction(t => (document.querySelector('.k-kpi') || {}).innerText?.includes(t), want);
    release(); await pg.waitForTimeout(400);
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(want), 'the held-back YTD answer must not replace 3М');
    // a period over the limit: a message, no figures
    await pg.locator('#from').fill('2020-01-01'); await pg.locator('#from').dispatchEvent('change');
    assert.match(await pg.locator('#status').innerText(), /1096/);
    assert.equal(await pg.locator('.k-kpi').count(), 0);
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C19 as a file: the demo says the period needs the server, tabs work, no errors', { skip: pw ? false : 'нет Playwright', timeout: 30000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const pg = await browser.newPage({ viewport: { width: 390, height: 800 } }), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    await pg.goto('file://' + path.join(ROOT, 'concepts', 'c19-pnl-history.html'));
    assert.match(await pg.locator('#main').innerText(), /сохранённые прогоны сервера/);
    for (const key of ['matrix', 'carry', 'curves', 'position', 'control', 'decomp']) {
      await pg.locator('#tabs [data-tab=' + key + ']').click();
      assert.match(await pg.locator('#main').innerText(), /сохранённые прогоны сервера/);
    }
    assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth), 390);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('C19 on a served synthetic store renders A, B, C from the real /period', { skip: !pw || !BASE, timeout: 60000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const pg = await browser.newPage({ viewport: { width: 1280, height: 900 } }), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    pg.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
    const response = pg.waitForResponse(r => r.url().includes('/api/v1/history/pnl/period?'));
    await pg.goto(BASE + '/concepts/c19-pnl-history.html');
    const d = await (await response).json();
    for (const k of ['summary', 'days', 'positions', 'buckets', 'keys']) assert.ok(k in d, k);
    await pg.waitForSelector('.k-kpis');
    assert.ok((await pg.locator('.k-kpi').first().innerText()).includes(await mn(pg, d.summary.total)));
    for (const key of ['matrix', 'carry', 'decomp']) { await tab(pg, key); await pg.waitForSelector('.k-kpis, .k-empty'); assert.ok(!BAD.test(await pg.locator('#main').innerText()), key); }
    // D, E, F on the real /curves, /position, /checks: drawn (a table or «нет данных»), no NaN / null, no console error
    const READY = { curves: '#cvLin table, #cvBody .k-empty', position: '#pDays table, #pBody .k-empty', control: '#ckList table, #ckList .k-empty' };
    for (const key of ['curves', 'position', 'control']) {
      await tab(pg, key); await pg.waitForSelector(READY[key], { timeout: 15000 });
      const text = await pg.locator('#main').innerText();
      assert.ok(!BAD.test(text), key); assert.doesNotMatch(text, /FX актива|FX фондирования/, key);
    }
    const cell = pg.locator('#ckList [data-ref]').first();
    if (await cell.count()) { const d = await cell.getAttribute('data-d'); await cell.click(); await pg.waitForSelector('#pDays tr.sel'); assert.equal(await pg.locator('#pDays tr.sel').getAttribute('data-d'), d); }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
