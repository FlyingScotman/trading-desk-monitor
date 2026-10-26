// Правило B1 в браузере (Playwright), без сервера — страницы prototypes/ из файлов, прогон — JS-фикстура:
//  - #327: фондирование не посчитано ни у одной позиции — в водопаде (C01, C06, C10) столбца нет, на его месте «—»,
//    подсказка «—»; KPI C10 — «—» и «не посчитано», «Итого» матрицы факторов — «—»;
//  - #322: в CURVE_MOVES нет КБД — «ОФЗ вчера» на C05 и C09 нет (null), а не копия КБД; CURVE_MOVES пуст — на C09 в
//    «изм. дня» столбцов нет, надпись «сдвигов нет в прогоне»;
//  - #325: позиция без купона (фикстура) — C01–C17 открываются без ошибок, и там, где читается тип купона: C03 по
//    группам, C04 «Тип купона», C10 «Прочее» (MTD тоже), C13 «Наши бумаги эмитента» по группам;
//  - #333: прогон без PnL — в столбцах PnL / кэрри / фондирования таблиц (строки, группы, «Итого») и в KPI PnL нет
//    «0» любой точности («0», «0.0», «(0.00)»): C01, C03, C06, C07, C10, C12, C13; в таблицах PnL по позициям (C01
//    вкладка «PnL» и C03 набор «Кэрри и PnL», по валютам) все столбцы PnL, факторов, MTD и YTD — «—»; итоги групп
//    K.table и заголовок разбора K.drill — «—»; Парето C13 по PnL — null;
//  - #339: C10 «История PnL», ряд «накопл.» — за непосчитанный день null (разрыв), дальше сумма продолжается;
//    точка между пропусками — значком;
//  - #332: C10 MTD у позиции без PnL — факторы null, итог MTD остаётся; C05 без сдвигов — «сдвигов нет в прогоне», а
//    не «почти не изменились»; C04 «Месяц события» с позицией без даты погашения — без ошибок, группа «без даты» последней
//    и по порядку, и по величине.
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

// D — прогон (объект) или ничего (фикстура как есть); X — window.DESK_EXTRAS (история, PnL MTD…) или ничего;
// store — localStorage до загрузки; hash — адрес страницы после #
async function open(browser, file, { D, X, store, hash = '' } = {}) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  if (store) await ctx.addInitScript(s => { for (const k in s) localStorage.setItem(k, s[k]); }, store);
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + (D ? JSON.stringify(D) : RUN) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(X || { notes: [] }) + ';' });
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
const noPnl = () => { const D = JSON.parse(RUN); D.positions.forEach(p => { p.pnl = { available: false, reasonText: 'нет прошлой оценки' }; }); return D; };
// «0» любой точности на месте значения: «0», «0.0», «+0.00», «(0.0)», «0%» — не «10», не «0.05», не «—»
const ZERO_ANY = /^[+(−-]?0(\.0+)?%?\)?$/;
// ячейки столбцов PnL / кэрри / фондирования во всех таблицах страницы: [таблица, столбец, вид строки, текст]; номер
// столбца — по последней строке заголовка, с учётом colspan. fac — ещё и столбцы факторов PnL, MTD и YTD (таблицы
// PnL по позициям: C01 вкладка «PnL», C03 набор «Кэрри и PnL»)
const FAC_HDR = '^(PnL|Кэрри|Фондир|Ставки|Спред|FX|Сделки|Прочее|MTD|YTD)';
const pnlCells = (pg, fac) => pg.evaluate(fac => [...document.querySelectorAll('table')].flatMap((tb, ti) => {
  const hr = tb.tHead && tb.tHead.rows[tb.tHead.rows.length - 1]; if (!hr) return [];
  const hs = []; [...hr.cells].forEach(c => { for (let i = 0; i < (c.colSpan || 1); i++) hs.push(c.innerText.replace(/\s+/g, ' ').trim()); });
  const idx = hs.map((t, i) => (fac ? new RegExp(fac, 'i').test(t) : /PnL|кэрри|фондир/i.test(t) && !/%|год|дн\.|ставк/i.test(t)) ? i : -1).filter(i => i >= 0);
  if (!idx.length) return [];
  return [...tb.querySelectorAll('tbody tr, tfoot tr')].flatMap(tr => {
    const kind = tr.closest('tfoot') ? 'итог' : tr.classList.contains('grp') ? 'группа' : 'строка';
    let pos = 0; const out = [];
    for (const td of tr.cells) { if (idx.includes(pos)) out.push([ti, hs[pos], kind, td.innerText.trim()]); pos += td.colSpan || 1; }
    return out;
  });
}), fac || null);
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
    await t.test('#333: прогон без PnL — в столбцах PnL / кэрри / фондирования (строки, группы, «Итого») и KPI PnL нет «0»', async () => {
      const D = noPnl();
      const cases = [
        ['c01-cockpit.html', {}],
        // C01, вкладка «PnL»: «PnL по позициям» по валютам — факторы, итоги групп и «Итого» — «—»
        ['c01-cockpit.html', { store: { 'c01-tab': 'pnl' }, fac: true, grpBtn: 'по валютам' }],
        ['c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, grouping: 'ccy' }) } }],
        // C03, набор «Кэрри и PnL» по валютам: свой sumCell (итоги групп и «Итого»)
        ['c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, preset: 'pnl', grouping: 'ccy' }) }, fac: true }],
        ['c06-launchpad.html', { store: { 'c06-preset': 'rates', 'c06-modes': JSON.stringify({ 'rates.carry': 'table' }) } }],
        ['c07-portfolio-map.html', {}],
        ['c10-pnl-explain.html', {}],
        ['c10-pnl-explain.html', { store: { 'c10-mode': 'mtd' } }],
        ['c12-terminal.html', {}],
        ['c13-issuers.html', {}],
        ['c13-issuers.html', { grp: true }],
      ];
      for (const [file, o] of cases) {
        const { pg, ctx, errors } = await open(browser, file, Object.assign({ D }, o));
        if (o.grp) {  // C13 «Наши бумаги эмитента» по группам — итоги групп K.table
          await pg.locator('.k-panel', { has: pg.locator('h3', { hasText: 'Наши бумаги эмитента' }) }).locator('.k-tbl-tools button').nth(1).click();
          await pg.waitForTimeout(200);
        }
        if (o.grpBtn) { await pg.locator('.k-tbl-tools button', { hasText: o.grpBtn }).first().click(); await pg.waitForTimeout(200); }
        const cells = await pnlCells(pg, o.fac && FAC_HDR);
        const kpi = await pg.evaluate(() => [...document.querySelectorAll('.k-kpi')].map(k => [k.querySelector('.l') && k.querySelector('.l').innerText, k.querySelector('.v') && k.querySelector('.v').innerText.trim()]).filter(k => /PnL/i.test(k[0] || '')));
        const bad = cells.filter(c => ZERO_ANY.test(c[3])).concat(kpi.filter(k => ZERO_ANY.test(k[1])));
        assert.deepStrictEqual(bad, [], file + ' ' + JSON.stringify(o));
        if (/c07|c13/.test(file)) {  // «Итого» PnL — «—»
          const tot = cells.filter(c => c[2] === 'итог' && /^PnL/.test(c[1]));
          assert.ok(tot.length && tot.every(c => c[3] === '—'), file + ': Итого PnL: ' + JSON.stringify(tot));
        }
        if (o.fac) {  // все столбцы PnL, факторов, MTD и YTD — «—» в «Итого», в группах и в строках
          const kinds = new Set(cells.map(c => c[2]));
          assert.ok(kinds.has('итог') && kinds.has('группа') && cells.some(c => /^MTD|^Ставки/.test(c[1])), file + ': есть итог, группы и факторы: ' + [...kinds] + ' ' + [...new Set(cells.map(c => c[1]))]);
          const notDash = cells.filter(c => c[3] !== '—');
          assert.deepStrictEqual(notDash, [], file + ' ' + JSON.stringify(o));
        }
        if (o.grp) {
          const g = cells.filter(c => c[2] === 'группа' && /^PnL/.test(c[1]));
          assert.ok(g.length && g.every(c => c[3] === '—'), 'C13: итоги групп PnL: ' + JSON.stringify(g));
        }
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
    await t.test('#333: C13 Парето по «PnL дня» без PnL — столбцов и накопленной доли нет (null), а не 0%', async () => {
      const { pg, ctx, errors } = await open(browser, 'c13-issuers.html', { D: noPnl(), store: { c13: JSON.stringify({ meas: 'pnl' }) } });
      const ser = await series(pg, '#pareto');
      assert.ok(ser.length === 2 && ser.every(x => x[2].length && x[2].every(v => v == null)), JSON.stringify(ser));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#333: прогон без PnL — заголовок разбора K.drill «—», а не «0»', async () => {
      const { pg, ctx, errors } = await open(browser, 'c13-issuers.html', { D: noPnl() });
      // «PnL дня» эмитента в дашборде — K.d по списку: «—», разбор по щелчку
      const kv = pg.locator('#dash [data-dr]', { hasText: '—' }).first();
      await kv.click(); await pg.waitForTimeout(200);
      const t = (await pg.locator('.k-drawer .t').last().innerText()).trim();
      assert.ok(/—/.test(t) && !/(^|\s)[+(−-]?0(\.0+)?\)?(\s|$)/.test(t), 'заголовок разбора: ' + t);
      const foot = await pg.locator('.k-drawer table.k-tbl tfoot td').allInnerTexts();
      assert.ok(foot.map(x => x.trim()).includes('—'), '«Итого» вклада в разборе — «—»: ' + foot);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#339: C10 «История PnL», «накопл.» — непосчитанный день null (разрыв), дальше сумма продолжается', async () => {
      // без истории: сегодня PnL не посчитан — точки 0 нет
      let { pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D: noPnl() });
      const hist = async () => (await series(pg, '.hist-wrap .chart')).find(x => x[0] === 'накопл.')[2];
      assert.deepStrictEqual(await hist(), [null]);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      // история деска: день посередине не посчитан — разрыв, следующий день продолжает сумму
      const D = JSON.parse(RUN), dates = ['2026-10-20', '2026-10-21', D.asOf];
      const ser = v => ['carry', 'funding', 'rates', 'spread', 'fx', 'trading', 'other', 'total'].reduce((o, k) => (o[k] = v, o), {});
      const X = { notes: [], histDates: dates, hist: { pnl: ser([1e6, null, 2e6]) } };
      ({ pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D, X }));
      assert.deepStrictEqual(await hist(), [1e6, null, 3e6]);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      // точка между двумя пропусками — значком (у линии с ней нет отрезка); у остальных точек значка нет
      const X2 = { notes: [], histDates: ['2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22', D.asOf], hist: { pnl: ser([1e6, 1e6, null, 2e6, null]) } };
      ({ pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D, X: X2 }));
      const sym = await pg.evaluate(() => { const ch = echarts.getInstanceByDom(document.querySelector('.hist-wrap .chart'));
        const sr = ch.getOption().series.find(s => s.name === 'накопл.');
        return sr.data.map(x => x && typeof x === 'object' && x.symbol ? x.symbol : sr.symbol); });
      assert.deepStrictEqual(sym, ['none', 'none', 'none', 'circle', 'none'], 'значок только у одиночной точки: ' + sym);
      assert.deepStrictEqual(await hist(), [1e6, 2e6, null, 4e6, null]);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#332: C10 MTD — у позиции без PnL факторы null, итог MTD остаётся', async () => {
      const D = noPnl(), id = D.positions[0].id;
      const X = { notes: [], positions: { [id]: { pnlMtd: 5e6 } } };
      const { pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D, X, store: { 'c10-mode': 'mtd' } });
      const kpi = await pg.evaluate(() => [...document.querySelectorAll('.k-kpi')].map(k => [k.querySelector('.l').innerText, k.querySelector('.v').innerText.trim()]));
      const tot = kpi.find(k => /PnL MTD/i.test(k[0]));
      assert.ok(tot && /5/.test(tot[1]), 'итог MTD — число: ' + JSON.stringify(tot));
      kpi.filter(k => /кэрри|фондир|ставк|спред|сделк|прочее/i.test(k[0])).forEach(k => assert.strictEqual(k[1], '—', JSON.stringify(k)));
      const cells = await pnlCells(pg);
      assert.deepStrictEqual(cells.filter(c => ZERO_ANY.test(c[3])), []);
      const txt = await pg.locator('#page').innerText();
      assert.ok(txt.includes('MTD не разложен') && !/— —/.test(txt), 'остаток MTD словами, без «— —»');
      const wf = (await series(pg, '.g2 .chart'))[0][2], nf = await pg.evaluate(() => KIT.PNL_FACTORS.length);
      assert.ok(wf.slice(0, nf).every(x => x[3] === null), 'водопад MTD: столбцы факторов — null: ' + JSON.stringify(wf));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#332: C05 без сдвигов кривых — «сдвигов нет в прогоне», а не «ставки почти не изменились»', async () => {
      const D = JSON.parse(RUN); D.CURVE_MOVES = {};
      const { pg, ctx, errors } = await open(browser, 'c05-morning-brief.html', { D });
      const s6 = await pg.locator('#s6 p').first().innerText();
      assert.ok(/сдвигов нет в прогоне/.test(s6) && !/почти не изменились|сильнее всего/.test(s6), s6);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#332: C04 «Месяц события» — позиция без даты события не роняет страницу, группа «без даты»', async () => {
      const D = JSON.parse(RUN), no = D.positions.find(p => p.maturity == null);
      assert.ok(no, 'в фикстуре позиция без даты погашения');
      const { pg, ctx, errors } = await open(browser, 'c04-pivot.html', { D, hash: '#r=evmonth&c=none&m=count&v=table' });
      // дата события такой позиции — null (lib/real.js больше не подставляет M.asOf, #342); ещё позиции без даты — и
      // перерисовка «Месяц события»
      // ещё 4 позиции без даты — группа больше большинства месяцев: «по величине» без правки она встала бы не в конец
      await pg.evaluate(id => { MOCK.positions.filter(p => p.id === id || p.event.date > '2029').slice(0, 5).forEach(p => { p.event.date = null; }); location.hash = '#r=evmonth&c=none&m=count&v=bars'; }, no.id);
      await pg.waitForTimeout(300);
      await pg.evaluate(() => { location.hash = '#r=evmonth&c=none&m=count&v=table'; });
      await pg.waitForTimeout(300);
      assert.ok((await pg.locator('body').innerText()).includes('без даты'), 'C04: группа «без даты»');
      // «без даты» — последней строкой и по порядку, и по величине 
      const labels = () => pg.evaluate(() => [...document.querySelectorAll('#page table tbody tr, body table tbody tr')].map(tr => tr.cells[0].innerText.trim()).filter(Boolean));
      let L = await labels();
      assert.strictEqual(L[L.length - 1], 'без даты', 'по порядку: ' + L);
      await pg.evaluate(() => { location.hash = '#r=evmonth&c=none&m=count&v=table&rs=abs'; }); await pg.waitForTimeout(300);
      L = await labels();
      assert.strictEqual(L[L.length - 1], 'без даты', 'по величине: ' + L);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('#342: даты события нет — null без подстановки asOf: C04 «без даты» и «—», карточка, C03, C08, C09, C13', async () => {
      const D = JSON.parse(RUN), no = D.positions.find(p => p.maturity == null);
      // и облигация без даты погашения и без оферты (бессрочная без колла): у неё карточка с «Ближайшим событием»
      const bd = D.positions.find(p => p.instrType === 'BOND' && p.issuer && !(p.extra || {}).offer_date);
      bd.maturity = null; bd.yieldDate = null;
      const { pg, ctx, errors } = await open(browser, 'c04-pivot.html', { D, hash: '#r=evmonth&c=none&m=count&v=table' });
      const ev = await pg.evaluate(id => { const p = MOCK.positions.find(x => x.id === id); return { e: p.event, asOf: MOCK.asOf }; }, no.id);
      assert.deepStrictEqual(ev.e, { date: null, kind: null }, 'событие без даты — null, не ' + ev.asOf);
      const labels = () => pg.evaluate(() => [...document.querySelectorAll('#page table tbody tr, body table tbody tr')].map(tr => tr.cells[0].innerText.trim()).filter(Boolean));
      const nNull = await pg.evaluate(() => MOCK.positions.filter(p => !p.event.date).length);
      let L = await labels();
      assert.strictEqual(L[L.length - 1], 'без даты', 'C04 «Месяц события»: без правки страницы — группа «без даты»: ' + L);
      await pg.evaluate(() => { location.hash = '#r=evkind&c=none&m=count&v=table'; }); await pg.waitForTimeout(300);
      L = await labels();
      assert.strictEqual(L[L.length - 1], '—', 'C04 «Вид события»: без даты — «—», последней: ' + L);
      const cnt = await pg.evaluate(() => { const tr = [...document.querySelectorAll('#page table tbody tr, body table tbody tr')].find(r => r.cells[0].innerText.trim() === '—'); return tr && tr.cells[1].innerText.trim(); });
      assert.strictEqual(cnt, String(nNull), 'в «—» — все позиции без даты');
      // карточка позиции: «Ближайшее событие» — «—», не дата прогона и не «погашение»
      await pg.evaluate(id => KIT.card(id), bd.id); await pg.waitForTimeout(200);
      const card = await pg.evaluate(() => { const l = [...document.querySelectorAll('.l')].find(x => x.innerText.trim() === 'Ближайшее событие'); return l && l.nextElementSibling.innerText; });
      assert.ok(card != null && /^—/.test(card) && !/погашение/.test(card), 'карточка: ' + card);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      for (const f of ['c03-blotter.html', 'c08-calendar.html', 'c09-market.html', 'c13-issuers.html']) {
        const o = await open(browser, f, { D: JSON.parse(JSON.stringify(D)) });
        assert.deepStrictEqual(o.errors, [], f);
        await o.ctx.close();
      }
    });
    await t.test('#342: C05 без сдвигов — «сдвигов нет в прогоне» одной фразой, без «б.п.» после неё', async () => {
      const D = JSON.parse(RUN); D.CURVE_MOVES = {};
      const { pg, ctx, errors } = await open(browser, 'c05-morning-brief.html', { D });
      const txt = await pg.locator('body').innerText();
      assert.ok(!/нет данных б\.п\.|в прогоне б\.п\./.test(txt), 'после «нет» — без единицы');
      const s6 = await pg.locator('#s6 p').first().innerText();
      assert.ok(/сдвигов нет в прогоне/.test(s6) && !/USD нет данных|CNY нет данных б/.test(s6), s6);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
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
