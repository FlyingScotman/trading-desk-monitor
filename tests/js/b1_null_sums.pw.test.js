// Правило B1 в браузере (Playwright), без сервера — страницы prototypes/ из файлов, прогон — JS-фикстура:
//  - #327: фондирование не посчитано ни у одной позиции — в водопаде (C01, C06, C10) столбца нет, на его месте «—»,
//    подсказка «—»; KPI C10 — «—» и «не посчитано», «Итого» матрицы факторов — «—»;
//  - #322: в CURVE_MOVES нет КБД — «ОФЗ вчера» на C05 и C09 нет (null), а не копия КБД; CURVE_MOVES пуст — на C09 в
//    «изм. дня» столбцов нет, надпись «сдвигов нет в прогоне»;
//  - #325: позиция без купона (фикстура) — C01–C17 открываются без ошибок, и там, где читается тип купона: C03 по
//    группам, C04 «Тип купона», C10 «Прочее» (MTD тоже), C13 «Наши бумаги эмитента» по группам.
//   node --test tests/js/b1_null_sums.pw.test.js
// Нужен Playwright (как load_errors.pw.test.js); без него тесты пропускаются.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const PAGES = fs.readdirSync(path.join(ROOT, 'concepts')).filter(f => /^c\d\d-.*\.html$/.test(f));

// D — прогон (объект) или ничего (фикстура как есть); store — localStorage до загрузки; hash — адрес страницы после #
async function open(browser, file, { D, store, hash = '' } = {}) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  if (store) await ctx.addInitScript(s => { for (const k in s) localStorage.setItem(k, s[k]); }, store);
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + (D ? JSON.stringify(D) : RUN) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + file + hash);
  await pg.waitForTimeout(400);
  return { pg, ctx, errors };
}
// подписи-тексты, нарисованные графиком в элементе sel (custom-серия водопада рисует текст сама), и данные серий
const chartTexts = (pg, sel) => pg.evaluate(sel => echarts.getInstanceByDom(document.querySelector(sel)).getZr().storage.getDisplayList()
  .map(e => e.style && e.style.text).filter(x => x != null && x !== ''), sel);
const series = (pg, sel) => pg.evaluate(sel => echarts.getInstanceByDom(document.querySelector(sel)).getOption().series
  .map(x => [x.name, x.type, (x.data || []).map(v => v && typeof v === 'object' && !Array.isArray(v) ? v.value : v)]), sel);
const graphicTexts = (pg, sel) => pg.evaluate(sel => ((echarts.getInstanceByDom(document.querySelector(sel)).getOption().graphic || [])[0] || { elements: [] }).elements
  .map(e => e.style && e.style.text).filter(Boolean), sel);
const noFunding = () => { const D = JSON.parse(RUN); D.positions.forEach(p => { if (p.pnl) p.pnl.funding = null; }); return D; };

test('B1 в браузере: #327, #322, #325', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('#327: фондирование не посчитано ни у одной позиции — C10: KPI, водопад и «Итого» матрицы — «—», а не 0.00', async () => {
      const { pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D: noFunding() });
      const kpi = await pg.evaluate(() => [...document.querySelectorAll('.k-kpi')].map(k => [k.querySelector('.l').innerText, k.querySelector('.v').innerText, k.querySelector('.s').innerText]));
      const fk = kpi.find(k => /фондир/i.test(k[0]));
      assert.ok(fk, 'KPI фондирования: ' + JSON.stringify(kpi));
      assert.strictEqual(fk[1].trim(), '—', JSON.stringify(fk));
      assert.ok(fk[2].includes('не посчитано'), JSON.stringify(fk));
      const ck = kpi.find(k => /кэрри/i.test(k[0]));
      assert.ok(/\d/.test(ck[1]), 'кэрри посчитано — число: ' + JSON.stringify(ck));
      const wf = await series(pg, '.g2 .chart');
      const fi = (await pg.evaluate(() => KIT.PNL_FACTORS.map(x => x[0]))).indexOf('funding');
      assert.strictEqual(wf[0][2][fi][3], null, 'значение столбца фондирования — null: ' + JSON.stringify(wf[0][2][fi]));
      assert.strictEqual(wf[0][2][fi][1], wf[0][2][fi][2], 'столбца нет: от = до');
      const tx = await chartTexts(pg, '.g2 .chart');
      assert.ok(tx.includes('—'), 'на месте столбца «—»: ' + tx);
      assert.ok(!tx.some(x => /^[+(]?0\.0+\)?$/.test(x)), 'нет подписи «0.00»: ' + tx);
      assert.strictEqual(await pg.evaluate(() => KIT.wfTip(null)), '—');
      const foot = await pg.evaluate(() => [...document.querySelectorAll('table.mx tfoot td')].map(td => td.innerText.trim()));
      assert.strictEqual(foot[2 + fi], '—', 'Итого матрицы по фондированию: ' + foot);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#327: C01 и C06 — водопад K.pnlWaterfall без столбца фондирования, на его месте «—»', async () => {
      for (const [file, store] of [['c01-cockpit.html', { 'c01-tab': 'pnl' }], ['c06-launchpad.html', null]]) {  // C06: плитка «PnL дня: разложение», «водопад» по умолчанию
        const { pg, ctx, errors } = await open(browser, file, { D: noFunding(), store });
        const s = await pg.evaluate(() => {  // график водопада — custom-серия
          const c = [...document.querySelectorAll('[_echarts_instance_]')].find(e => { const o = echarts.getInstanceByDom(e).getOption(); return o.series && o.series[0] && o.series[0].type === 'custom'; });
          if (!c) return null; c.setAttribute('data-wf', '1'); return '[data-wf]';
        });
        assert.ok(s, file + ': водопад на странице');
        const tx = await chartTexts(pg, s);
        assert.ok(tx.includes('—'), file + ': «—» на месте фондирования: ' + tx);
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
    await t.test('#322: в CURVE_MOVES нет КБД — «ОФЗ вчера» на C05 и C09 нет, а не копия КБД; RUONIA вчера — есть', async () => {
      const D = JSON.parse(RUN); delete D.CURVE_MOVES['RUB OFZ'];
      for (const [file, sel] of [['c05-morning-brief.html', '#curveChart'], ['c09-market.html', '#curves']]) {
        const { pg, ctx, errors } = await open(browser, file, { D });
        const ser = await series(pg, sel);
        const ofzY = ser.find(x => /^ОФЗ (вчера)/.test(x[0]));
        assert.ok(ofzY && ofzY[2].every(v => v == null), file + ': ОФЗ вчера: ' + JSON.stringify(ofzY));
        const ruY = ser.find(x => /^RUONIA (вчера)/.test(x[0]));
        assert.ok(ruY && ruY[2].every(v => typeof v === 'number'), file + ': RUONIA вчера: ' + JSON.stringify(ruY));
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
    await t.test('#322: CURVE_MOVES пуст — C09 «изм. дня»: столбцов нет, «сдвигов нет в прогоне»; «вчера» нет; C05 — то же словами', async () => {
      const D = JSON.parse(RUN); D.CURVE_MOVES = {};
      let { pg, ctx, errors } = await open(browser, 'c09-market.html', { D });
      let ser = await series(pg, '#curves');
      ser.filter(x => /вчера/.test(x[0])).forEach(x => assert.ok(x[2].every(v => v == null), 'C09 ' + x[0] + ': ' + x[2]));
      await pg.locator('#cCurve button', { hasText: 'изм. дня' }).click(); await pg.waitForTimeout(200);
      ser = await series(pg, '#curves');
      assert.ok(ser.length === 3 && ser.every(x => x[1] === 'bar' && x[2].every(v => v == null)), 'C09 изм. дня: ' + JSON.stringify(ser));
      assert.deepStrictEqual(await graphicTexts(pg, '#curves'), ['сдвигов нет в прогоне']);
      await pg.locator('#cCurve button', { hasText: 'базис ОФЗ' }).click(); await pg.waitForTimeout(200);
      ser = await series(pg, '#curves');
      assert.ok(ser.find(x => x[0] === 'вчера')[2].every(v => v == null), 'C09 базис вчера');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      ({ pg, ctx, errors } = await open(browser, 'c05-morning-brief.html', { D }));
      ser = await series(pg, '#curveChart');
      ser.filter(x => /вчера|^Δ/.test(x[0])).forEach(x => assert.ok(x[2].every(v => v == null), 'C05 ' + x[0] + ': ' + x[2]));
      assert.deepStrictEqual(await graphicTexts(pg, '#curveChart'), ['сдвигов нет в прогоне']);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      // C06, плитка «Кривые»: только «сегодня», без пустых «изм.» и «вчера» в легенде, и надпись
      ({ pg, ctx, errors } = await open(browser, 'c06-launchpad.html', { D }));
      const sel = await pg.evaluate(() => { const t = [...document.querySelectorAll('.tile')].find(x => /Кривые/.test(x.querySelector('h3').textContent)); const c = t.querySelector('[_echarts_instance_]'); c.setAttribute('data-cv', '1'); return '[data-cv]'; });
      ser = await series(pg, sel);
      assert.deepStrictEqual(ser.map(x => x[0]), ['сегодня'], 'C06 Кривые: ' + ser.map(x => x[0]));
      assert.deepStrictEqual(await graphicTexts(pg, sel), ['сдвигов нет в прогоне']);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#327: прогон без PnL — ни одного «0.0» на месте непосчитанного значения: C01, C05, C06, C10, C12', async () => {
      const D = JSON.parse(RUN); D.positions.forEach(p => { p.pnl = { available: false, reasonText: 'нет прошлой оценки' }; });
      const ZERO = /(^|[^\d.,])[+(−-]?0\.0+\)?(?![\d])/;  // «0.0», «+0.00», «(0.0)» — не «10.0», не «0.05»
      const zeros = txt => txt.split('\n').filter(l => ZERO.test(l));
      const tile = (pg, title) => pg.locator('.tile', { has: pg.locator('h3', { hasText: title }) }).innerText();
      const cases = [
        ['c01-cockpit.html', {}, async pg => [await pg.locator('#hud').innerText()]],
        ['c05-morning-brief.html', {}, async pg => { const s1 = await pg.locator('#s1').innerText(); assert.ok(s1.includes('PnL за день не посчитан'), s1); return [s1]; }],
        ['c06-launchpad.html', { store: { 'c06-preset': 'rates', 'c06-modes': JSON.stringify({ 'rates.carry': 'table' }) } },
          async pg => [await tile(pg, 'Ключевые цифры'), await tile(pg, 'Кэрри и фондирование'), await tile(pg, 'PnL дня: разложение')]],
        ['c10-pnl-explain.html', {}, async pg => [await pg.locator('#page').innerText()]],
        ['c10-pnl-explain.html', { store: { 'c10-mode': 'mtd' } }, async pg => [await pg.locator('#page').innerText()]],
        ['c12-terminal.html', {}, async pg => [await pg.locator('body').innerText()]],
      ];
      for (const [file, o, get] of cases) {
        const { pg, ctx, errors } = await open(browser, file, Object.assign({ D }, o));
        const txt = (await get(pg)).join('\n');
        assert.deepStrictEqual(zeros(txt), [], file + ' ' + JSON.stringify(o));
        if (file !== 'c05-morning-brief.html') assert.ok(txt.includes('—'), file + ': «—» на месте PnL');
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
    await t.test('#325: позиция без купона — C01–C17 и экраны, где читается тип купона, без ошибок', async () => {
      const no = JSON.parse(RUN).positions.find(p => p.instrType !== 'BOND' && p.instrType !== 'FX' && p.issuer && p.couponType == null);
      assert.ok(no, 'в фикстуре позиция без купона с эмитентом');
      const runs = PAGES.map(f => [f, {}]).concat([
        ['c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, grouping: 'ccy' }) } }],
        ['c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, grouping: 'type' }) } }],
        ['c04-pivot.html', { hash: '#r=cpn&c=none&m=count&v=table' }],
        ['c10-pnl-explain.html', { store: { 'c10-mode': 'mtd' } }],
        ['c13-issuers.html', { store: { c13: JSON.stringify({ sel: no.issuer }) }, grp: true }],
      ]);
      for (const [file, o] of runs) {
        const { pg, ctx, errors } = await open(browser, file, o);
        if (o.grp) {  // C13: «Наши бумаги эмитента» — группы «валюта · фикс / флоатеры»
          const tools = pg.locator('.k-panel', { has: pg.locator('h3', { hasText: 'Наши бумаги эмитента' }) }).locator('.k-tbl-tools button').nth(1);
          await tools.click(); await pg.waitForTimeout(200);
          assert.ok((await pg.locator('.k-panel', { has: pg.locator('h3', { hasText: 'Наши бумаги эмитента' }) }).innerText()).includes('без купона'), 'C13: группа «без купона»');
        }
        if (file === 'c04-pivot.html' && o.hash) assert.ok((await pg.locator('#page, body').first().innerText()).includes('без купона'), 'C04: «без купона»');
        assert.deepStrictEqual(errors, [], file + ' ' + JSON.stringify(o));
        await ctx.close();
      }
    });
  } finally {
    await browser.close();
  }
});
