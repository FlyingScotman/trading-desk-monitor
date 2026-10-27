// C18 «Сценарии» в браузере (Playwright, #363). Страница открывается как файл (демо: риск из lib/mock.js).
// При ширине 1280 и 390, в светлой и тёмной теме: без ошибок консоли, без прокрутки вбок; есть редактор FX и спредов,
// итог по книгам, разложение, топ по убытку и сравнение (колонки — редактор и все пресеты). Шок USD в редакторе
// меняет итог, курс после шока и адрес (#s=…); после перезагрузки по адресу состояние то же. Без localStorage
// (доступ бросает исключение) страница работает. Итог разложения = итог сценария.
//   node --test tests/js/scenario_c18.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const URL0 = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c18-scenarios.html')).href;

test('C18 в браузере', { skip: !pw && 'нет Playwright' }, async t => {
  const b = await pw.chromium.launch();
  const open = async (w, opts) => {
    opts = opts || {};
    const ctx = await b.newContext({ viewport: { width: w, height: 900 }, colorScheme: opts.dark ? 'dark' : 'light' });
    if (opts.noStorage) await ctx.addInitScript(() => { Object.defineProperty(window, 'localStorage', { get() { throw new Error('blocked'); } }); });
    const pg = await ctx.newPage(), errs = [];
    pg.on('pageerror', e => errs.push(e.message)); pg.on('console', m => { if (m.type() === 'error') errs.push(m.text()); });
    await pg.goto(URL0 + (opts.hash || ''));
    await pg.waitForFunction(() => window.C18 && window.C18.result && document.querySelector('table.k-tbl tfoot'));
    return { pg, ctx, errs };
  };
  const kpiTotal = pg => pg.evaluate(() => document.querySelector('.k-kpis .k-kpi .v').textContent.trim());
  try {
    for (const w of [1280, 390]) for (const dark of [false, true]) {
      await t.test(`ширина ${w}, ${dark ? 'тёмная' : 'светлая'} тема`, async () => {
        const { pg, ctx, errs } = await open(w, { dark });
        const s = await pg.evaluate(() => ({
          sw: document.documentElement.scrollWidth, iw: innerWidth,
          titles: [...document.querySelectorAll('.k-panel h3')].map(h => h.textContent),
          fx: document.querySelectorAll('input[type=number][data-k^="fx:"]').length,
          spreads: ['ofz', 'all', 'mult'].every(k => document.querySelector('input[type=number][data-k="' + k + '"]')),
          rating: document.querySelectorAll('input[type=number][data-k^="rating:"]').length,
          sector: document.querySelectorAll('input[type=number][data-k^="sector:"]').length,
          none: !!document.querySelector('input[data-k="rating:_none"]') || !document.querySelectorAll('input[data-k^="rating:"]').length,
          cols: [...document.querySelectorAll('th[data-k^="s:"]')].map(th => th.dataset.k),
          csv: document.querySelectorAll('.k-tbtn').length,
        }));
        assert.ok(s.sw <= s.iw, `прокрутка вбок: ${s.sw} > ${s.iw}`);
        ['Редактор сценария', 'Разбор сценария', 'Итог по книгам', 'Разложение по компонентам', 'Топ-15 позиций по убытку', 'Сравнение сценариев по книгам']
          .forEach(x => assert.ok(s.titles.some(t2 => t2.startsWith(x)), x + ': ' + s.titles.join(' | ')));
        assert.ok(s.fx >= 3 && s.spreads && s.rating >= 3 && s.sector >= 2, JSON.stringify(s));
        const n = await pg.evaluate(() => SCENARIO.DEMO_PRESETS.length);
        assert.strictEqual(s.cols.length, n + 1, 'колонки сравнения — редактор и все пресеты');
        assert.strictEqual(s.cols[0], 's:ed');
        assert.ok(s.csv >= 2, 'CSV у таблиц');
        assert.deepStrictEqual(errs, []);
        await ctx.close();
      });
    }

    await t.test('шок USD: итог, курс после шока и адрес; после перезагрузки — то же', async () => {
      const { pg, ctx, errs } = await open(1280);
      const before = await kpiTotal(pg);
      await pg.fill('input[type=number][data-k="fx:USD"]', '20');
      await pg.waitForFunction(b0 => document.querySelector('.k-kpis .k-kpi .v').textContent.trim() !== b0, before);
      const st = await pg.evaluate(() => ({ hash: location.hash, side: document.querySelector('[data-side="fx:USD"]').textContent, rate: MOCK.fx.USD, range: document.querySelector('input[type=range][data-k="fx:USD"]').value,
        res: window.C18.result.total, parts: [...document.querySelectorAll('table[data-vk="c18-parts"] tr.tot td')].map(td => td.textContent) }));
      assert.ok(/^#s=/.test(st.hash) && decodeURIComponent(st.hash).includes('"USD":20'), st.hash);
      assert.ok(st.side.includes((st.rate * 1.2).toFixed(2)), 'курс после шока: ' + st.side);
      assert.strictEqual(st.range, '20', 'ползунок следует за полем');
      assert.ok(st.res.fx !== 0);
      assert.strictEqual(st.parts[1], await kpiTotal(pg).then(x => x.replace(/\s*mm.*$/, '').trim()), 'итог разложения = итог сценария');
      const hash = st.hash;
      await ctx.close();
      const again = await open(1280, { hash, noStorage: true });
      const v = await again.pg.evaluate(() => document.querySelector('input[type=number][data-k="fx:USD"]').value);
      assert.strictEqual(v, '20', 'состояние из адреса, без localStorage');
      assert.deepStrictEqual(errs.concat(again.errs), []);
      await again.ctx.close();
    });

    await t.test('начать с пресета: кривые из пресета, FX — в поля редактора; разбор пресета', async () => {
      const { pg, ctx, errs } = await open(390);
      await pg.selectOption('select[data-a=base]', 'rubdep');
      await pg.waitForFunction(() => document.querySelector('input[type=number][data-k="fx:USD"]').value === '15');
      const t1 = await pg.evaluate(() => ({ base: document.querySelector('.c18-base').textContent, rates: window.C18.result.total.rates }));
      assert.ok(/кривые/.test(t1.base) && t1.rates !== 0, JSON.stringify(t1));
      await pg.selectOption('select[data-a=view]', 'credit');
      const t2 = await pg.evaluate(() => ({ cur: document.querySelector('th.c18-cur') && document.querySelector('th.c18-cur').dataset.k, spread: window.C18.result.total.spread, rates: window.C18.result.total.rates }));
      assert.deepStrictEqual([t2.cur, t2.rates], ['s:credit', 0]);
      assert.ok(t2.spread < 0);
      const sw = await pg.evaluate(() => document.documentElement.scrollWidth - innerWidth);
      assert.ok(sw <= 0);
      assert.deepStrictEqual(errs, []);
      await ctx.close();
    });
  } finally { await b.close(); }
});
