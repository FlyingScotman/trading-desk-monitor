// Неполные итоги (#335, вариант «а») в браузере (Playwright), без сервера — страницы prototypes/ из файлов, прогон —
// JS-фикстура, у половины позиций PnL не посчитан (52 из 104):
//  - KPI PnL дня C01, C06, C10, C12 и C05 («потеряли … mm ₽») — число и «· без 52 из 104 поз.», в подсказке — позиции;
//  - итоги таблиц: C10 «Итого» матрицы факторов, C03 «Кэрри и PnL» по валютам — «Итого» и группы; CSV C03 — число в
//    ячейке, пометка — колонкой «Неполный итог»;
//  - водопад C10: день — «Итого» = известный итог; MTD без разложения — «Итого» = итог MTD, столбец «Не разложено»;
//  - C17 (демо): «Нетто» — число с пометкой, а не «—» (раньше netKnown, вариант «б»): плитка, «Итог до …», «Итого»;
//  - не посчитано ни у одной позиции (N = M) — «—» без пометки.
//   node --test tests/js/partial_sums_335.pw.test.js
// Нужен Playwright (как b1_null_sums.pw.test.js); без него тесты пропускаются.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

// D — прогон (объект) или ничего; X — window.DESK_EXTRAS; store — localStorage; demo — демо-набор из файла
async function open(browser, file, { D, X, store, demo, width, hash = '' } = {}) {
  const ctx = await browser.newContext({ viewport: { width: width || 1500, height: 1000 } }), pg = await ctx.newPage();
  if (store) await ctx.addInitScript(s => { for (const k in s) localStorage.setItem(k, s[k]); }, store);
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  if (!demo) await pg.route('**/*', route => {
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
  await pg.goto((demo ? 'file://' + path.join(ROOT, 'concepts', file) : ORIGIN + '/concepts/' + file) + hash);
  await pg.waitForTimeout(500);
  return { pg, ctx, errors };
}
const NO = { available: false, reasonText: 'нет прошлой оценки' };
const half = () => { const D = JSON.parse(RUN); D.positions.forEach((p, i) => { if (i % 2) p.pnl = Object.assign({}, NO); }); return D; };
const NOTE = '· без 52 из 104 поз.';
const series = (pg, sel) => pg.evaluate(sel => { const o = echarts.getInstanceByDom(document.querySelector(sel)).getOption(); return { cats: o.xAxis[0].data, data: o.series[0].data }; }, sel);
// KPI: [подпись, цифра без пометки, пометка, её подсказка]
const kpis = pg => pg.evaluate(() => [...document.querySelectorAll('.k-kpi')].map(k => { const p = k.querySelector('.v .k-part');
  return [k.querySelector('.l').innerText, k.querySelector('.v').innerText.replace(p ? p.innerText : '', '').trim(), p ? p.innerText : '', p ? p.title : '']; }));
// позиция без PnL (её имя должно быть в подсказке)
const missedName = pg => pg.evaluate(() => MOCK.positions.find(p => p.pnl.available === false).name);

test('#335: неполные итоги — число и «без N из M поз.»', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('KPI «PnL дня» C01, C10, C12 — число, пометка под цифрой, в подсказке позиции; C06 — в плитке', async () => {
      for (const [file, store] of [['c01-cockpit.html'], ['c10-pnl-explain.html'], ['c12-terminal.html']]) {
        const { pg, ctx, errors } = await open(browser, file, { D: half(), store });
        const k = (await kpis(pg)).find(x => /PnL дня/i.test(x[0]));
        assert.ok(k, file + ': KPI PnL дня');
        assert.ok(/\d/.test(k[1]) && !/—/.test(k[1]), file + ': число: ' + JSON.stringify(k));
        assert.strictEqual(k[2], NOTE, file);
        assert.ok(k[3].includes(await missedName(pg)) && k[3].includes('не посчитано у 52'), file + ': подсказка: ' + k[3]);
        // пометка — под цифрой (.k-parts), не в строке цифры
        assert.ok(await pg.evaluate(() => [...document.querySelectorAll('.k-kpi .v > .k-parts > .k-part')].length > 0), file);
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
      const { pg, ctx, errors } = await open(browser, 'c06-launchpad.html', { D: half() });
      const hud = await pg.locator('.tile', { has: pg.locator('h3', { hasText: 'Ключевые цифры' }) }).innerText();
      assert.ok(hud.includes(NOTE), 'C06 «Ключевые цифры»: ' + hud.slice(0, 200));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('C05: «потеряли … mm ₽ · без 52 из 104 поз.» и у главной цифры', async () => {
      const { pg, ctx, errors } = await open(browser, 'c05-morning-brief.html', { D: half() });
      const s1 = await pg.locator('#s1').innerText();
      assert.ok(/За день (потеряли|заработали) [\d,.]+ mm ₽ · без 52 из 104 поз\. Главное/.test(s1), s1.slice(0, 400));
      assert.ok(!/поз\.\./.test(await pg.locator('body').innerText()), 'без двух точек «поз..» (ревью #372)');
      const big = await pg.locator('#s1 .hl .big').innerText();
      assert.ok(big.includes('mm ₽') && big.includes(NOTE) && big.indexOf('mm ₽') < big.indexOf(NOTE), 'единица — до пометки: ' + big);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('C10 день: «Итого» матрицы — числа с пометкой; водопад «Итого» = известный итог, без «Не разложено»', async () => {
      const { pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D: half() });
      const foot = await pg.evaluate(() => [...document.querySelectorAll('table.mx tfoot td')].slice(2).map(td => td.innerText.replace(/\s+/g, ' ').trim()));
      assert.ok(foot.length >= 8 && foot.every(x => /\d/.test(x) && x.includes(NOTE)), JSON.stringify(foot));
      // группы матрицы — свои N из M
      const grp = await pg.evaluate(() => [...document.querySelectorAll('table.mx tbody .k-part')].map(e => e.innerText));
      assert.ok(grp.length && grp.every(x => /^· без \d+ из \d+ поз\.$/.test(x)), JSON.stringify(grp.slice(0, 5)));
      const wf = await series(pg, '.g2 .chart'), known = await pg.evaluate(() => MOCK.sum(MOCK.positions, p => p.pnl.total));
      assert.ok(!wf.cats.includes('Не разложено'), wf.cats.join());
      assert.strictEqual(wf.cats[wf.cats.length - 1], 'Итого');
      assert.ok(Math.abs(wf.data[wf.data.length - 1][3] - known) < 1, 'Итого = известный итог: ' + JSON.stringify(wf.data[wf.data.length - 1]) + ' vs ' + known);
      assert.ok((await pg.locator('.k-panel', { has: pg.locator('h3', { hasText: 'Водопад' }) }).locator('.hd').innerText()).includes('Итого ' + NOTE));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('C10 MTD (#341): PnL MTD есть, разложения нет — «Итого» водопада = итог MTD, столбец «Не разложено»', async () => {
      const D = JSON.parse(RUN); D.positions.forEach(p => { p.pnl = Object.assign({}, NO); });
      const X = { notes: [], positions: { [D.positions[0].id]: { pnlMtd: 5e6 } } };
      const { pg, ctx, errors } = await open(browser, 'c10-pnl-explain.html', { D, X, store: { 'c10-mode': 'mtd' } });
      const wf = await series(pg, '.g2 .chart'), nf = await pg.evaluate(() => KIT.PNL_FACTORS.length);
      assert.deepStrictEqual(wf.cats.slice(nf), ['Не разложено', 'Итого']);
      assert.ok(wf.data.slice(0, nf).every(x => x[3] === null), 'факторы — null');
      assert.strictEqual(wf.data[nf][3], 5e6, '«Не разложено» — весь итог');
      assert.strictEqual(wf.data[nf + 1][3], 5e6, '«Итого» — итог MTD, а не «—»');
      const k = (await kpis(pg)).find(x => /PnL MTD/i.test(x[0]));
      assert.ok(/5/.test(k[1]) && k[2] === '· без 103 из 104 поз.', JSON.stringify(k));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('C03 «Кэрри и PnL» по валютам: «Итого» и группы — пометка; CSV — число и колонка «Неполный итог»', async () => {
      const { pg, ctx, errors } = await open(browser, 'c03-blotter.html', { D: half(), store: { 'c03-state': JSON.stringify({ v: 2, preset: 'pnl', grouping: 'ccy' }) } });
      const foot = await pg.evaluate(() => [...document.querySelectorAll('#tbl tfoot .k-part')].map(e => e.innerText));
      assert.ok(foot.length >= 3 && foot.every(x => x === '· без 52 из 104 поз.'), JSON.stringify(foot));
      assert.ok(await pg.evaluate(() => document.querySelectorAll('#tbl tr.grp .k-part').length) > 0, 'итоги групп');
      const csv = (await pg.evaluate(() => KIT.tableCsv(document.querySelector('#tbl table'), {}))).split('\r\n');
      const head = csv[2].split(','), tot = csv[csv.length - 2];
      assert.strictEqual(head[head.length - 1], 'Неполный итог', csv[2]);
      const cells = tot.split(','), pi = head.indexOf('PnL дня');
      assert.ok(/^Итого 104 поз\./.test(cells[0]), tot);
      assert.ok(/^-?\d+(\.\d+)?$/.test(cells[pi]), '«PnL дня» в «Итого» — число: ' + cells[pi]);
      assert.ok(/(^|; )PnL дня: без 52 из 104 поз\.$/.test(cells[cells.length - 1]), 'пометка — в последней колонке: ' + tot);
      assert.ok(!/· без/.test(tot), 'в ячейках чисел пометки нет: ' + tot);
      // строки позиций — без пометки (в колонке «Неполный итог» пусто)
      assert.ok(csv.slice(3, 6).every(l => /,$/.test(l)), csv.slice(3, 6).join('\n'));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('C17 (демо): «Нетто» — число с пометкой (было «—»), в подсказке позиции без фондирования; «Итог до …» и «Итого»', async () => {
      const { pg, ctx, errors } = await open(browser, 'c17-forward-carry.html', { demo: true });
      const k = (await kpis(pg)).find(x => /Нетто/i.test(x[0]));
      assert.ok(k && /\d/.test(k[1]) && !/^—/.test(k[1]), 'плитка «Нетто» — число: ' + JSON.stringify(k));
      const miss = await pg.evaluate(() => CARRY.rows(MOCK.forwardCarryDemo({}), { pos: MOCK.pos, books: MOCK.book }).filter(r => r.funding == null));
      assert.ok(miss.length > 0);
      assert.ok(/^· без \d+ из \d+ поз\.$/.test(k[2]) && k[2].startsWith('· без ' + miss.length + ' из '), JSON.stringify(k));
      assert.ok(miss.every(r => k[3].includes(r.name)), 'подсказка: ' + k[3]);
      const sum = await pg.evaluate(() => { const l = [...document.querySelectorAll('.c17-sum > div')].findIndex(d => d.innerText.trim() === 'Нетто'); return document.querySelectorAll('.c17-sum > div')[l + 1].innerText; });
      assert.ok(/\d/.test(sum) && sum.includes('· без ' + miss.length + ' из '), '«Итог до …» Нетто: ' + sum);
      const tot = await pg.evaluate(() => { const t = document.querySelector('.k-panel table.k-tbl'); const h = [...t.tHead.rows[0].cells].map(c => c.innerText.replace(/\s+/g, ' ').trim());
        const i = h.findIndex(x => /^Нетто/.test(x)); return t.tFoot.rows[0].cells[i - (t.tFoot.rows[0].cells.length < h.length ? h.length - t.tFoot.rows[0].cells.length : 0)].innerText; });
      assert.ok(/\d/.test(tot) && tot.includes('без ' + miss.length + ' из '), '«Итого» таблицы, Нетто: ' + tot);
      // «Итог до …» складывается в неполное «Нетто»: кэрри + фондирование + без прогноза − кэрри позиций без фондирования
      const num = t => { const m = /^\(?[\d,.]+\)?/.exec(t.trim()); return m ? (m[0].startsWith('(') ? -1 : 1) * +m[0].replace(/[(),]/g, '') : null; };
      const L = await pg.evaluate(() => { const d = [...document.querySelectorAll('.c17-sum > div')]; const o = {}; for (let i = 0; i + 1 < d.length; i += 2) o[d[i].innerText.trim()] = d[i + 1].innerText; return o; });
      const parts = ['Кэрри', 'Фондирование позиций с прогнозом', 'фондирование без прогноза кэрри', 'минус кэрри позиций без фондирования (не в нетто)'].map(k => num(L[k]));
      assert.ok(parts.every(x => x != null), JSON.stringify(L));
      assert.ok(Math.abs(parts.reduce((a, b) => a + b, 0) - num(L['Нетто'])) < 0.015, 'строки сходятся с «Нетто»: ' + JSON.stringify(L));
      assert.ok(/кэрри \+ фондирование позиций с фондированием/.test(await pg.locator('.k-kpi', { hasText: 'Нетто' }).first().innerText()), 'подпись плитки «Нетто»');
      const fk = await pg.locator('.k-kpi', { hasText: 'Фондирование' }).first().innerText();
      assert.ok(/с прогнозом кэрри[\s\S]*· без \d+ из \d+ поз\./.test(fk), 'плитка «Фондирование»: ' + fk);
      const ban = await pg.locator('#ban').innerText();
      assert.ok(!/Нетто этих книг и итога — «—»/.test(ban), ban);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('ревью #372: без прокрутки вбок на 390, 1280 и 1500 px; единица — до пометки, а не после', async () => {
      const mtdX = () => { const X = { notes: [], positions: {} }; JSON.parse(RUN).positions.forEach((p, i) => { if (i % 3) X.positions[p.id] = { pnlMtd: 1e6 * (i % 5 - 2) }; }); return X; };
      const cases = [['c01-cockpit.html', {}], ['c01-cockpit.html', { store: { 'c01-tab': 'pnl' } }], ['c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, preset: 'pnl', grouping: 'ccy' }) } }],
        ['c04-pivot.html', { hash: '#p=pnl-book' }], ['c05-morning-brief.html', {}], ['c06-launchpad.html', {}], ['c07-portfolio-map.html', {}], ['c10-pnl-explain.html', {}],
        ['c10-pnl-explain.html', { X: mtdX(), store: { 'c10-mode': 'mtd' } }], ['c12-terminal.html', {}], ['c13-issuers.html', {}], ['c17-forward-carry.html', { demo: true }]];
      for (const width of [390, 1280, 1500]) for (const [file, o] of cases) {
        const { pg, ctx, errors } = await open(browser, file, Object.assign({ D: half(), width }, o));
        const w = await pg.evaluate(() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]);
        assert.ok(w[0] <= w[1], width + ' ' + file + ' ' + JSON.stringify(o.store || o.hash || '') + ': прокрутка вбок ' + w);
        // текст сразу за пометкой — не единица («· без 3 из 30 поз. тыс ₽»): единицу ставит suffix до пометки
        const after = await pg.evaluate(() => [...document.querySelectorAll('.k-part')].map(e => { let n = e.nextSibling, t = ''; while (n && !t.trim()) { t = n.textContent || ''; n = n.nextSibling; } return t.trim(); })
          .filter(t => /^(тыс|mm|bn|₽|\$|€|¥|б\.п|шт)/.test(t)));
        assert.deepStrictEqual(after, [], width + ' ' + file + ': единица после пометки');
        assert.deepStrictEqual(errors, [], width + ' ' + file);
        await ctx.close();
      }
    });
    await t.test('ревью #372: C04 сводная PnL — итог и ячейки с пометкой; разбор из «Итого» K.table (C07) — с пометкой в заголовке', async () => {
      let { pg, ctx, errors } = await open(browser, 'c04-pivot.html', { D: half(), hash: '#p=pnl-book' });
      const sum = await pg.locator('#sum').innerText();
      assert.ok(/итого [(\d]/.test(sum) && sum.includes(NOTE) && sum.indexOf('тыс') < sum.indexOf(NOTE), 'итог сводной: ' + sum);
      const foot = await pg.evaluate(() => [...document.querySelectorAll('#out tfoot .k-part')].map(e => e.innerText));
      assert.ok(foot.length >= 2 && foot.every(x => x === '· без 52 из 104 поз.'), JSON.stringify(foot));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      ({ pg, ctx, errors } = await open(browser, 'c07-portfolio-map.html', { D: half() }));
      const cell = pg.locator('table.k-tbl tfoot td', { has: pg.locator('.k-part') }).first();
      const note = await cell.locator('.k-part').innerText();
      await cell.locator('[data-dr]').click(); await pg.waitForTimeout(200);
      const t = await pg.locator('.k-drawer .t').last().innerText();
      assert.ok(t.includes(note), 'заголовок разбора «' + t + '» — с пометкой ' + note);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('N = M: PnL не посчитан ни у одной позиции — «—» без пометки (C10, C05)', async () => {
      const D = JSON.parse(RUN); D.positions.forEach(p => { p.pnl = Object.assign({}, NO); });
      for (const file of ['c10-pnl-explain.html', 'c05-morning-brief.html']) {
        const { pg, ctx, errors } = await open(browser, file, { D });
        assert.strictEqual(await pg.evaluate(() => document.querySelectorAll('.k-part').length), 0, file);
        if (file === 'c10-pnl-explain.html') assert.strictEqual((await kpis(pg)).find(x => /PnL дня/i.test(x[0]))[1], '—');
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
  } finally {
    await browser.close();
  }
});
