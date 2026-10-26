// В браузере (Playwright), без сервера — страницы prototypes/ из файлов, прогон — JS-фикстура:
//  - #334: прогон без сдвигов в форме сервера (CURVE_MOVES — null на каждый бакет, изменения курсов — null, кроме RUB):
//    C05, C06, C09, C12 — «сдвигов нет в прогоне», в тексте страниц ни «NaN», ни «null», ни «undefined»;
//  - #336: IRS / XCCY / CDS — деривативы: C03 «тип → срок» и C04 «Тип инструмента» — группы «Деривативы · …», позиция
//    OTHER — «Прочее · Прочее»; C01, C03, C04, C13 без ошибок.
//   node --test tests/js/ui_pack7.pw.test.js
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
const graphicTexts = (pg, sel) => pg.evaluate(sel => ((echarts.getInstanceByDom(document.querySelector(sel)).getOption().graphic || [])[0] || { elements: [] }).elements
  .map(e => e.style && e.style.text).filter(Boolean), sel);
const BAD = /\bNaN\b|\bnull\b|\bundefined\b/;
// прогон без прошлого рынка, как его отдаёт сервер после #334: null, а не 0
const noMoves = () => {
  const D = JSON.parse(RUN);
  D.prevDate = null;
  Object.keys(D.CURVE_MOVES).forEach(c => { D.CURVE_MOVES[c] = D.BUCKETS.map(() => null); });
  ['FX_MOVE_PCT', 'FX_CBR_MOVE_PCT'].forEach(k => Object.keys(D[k]).forEach(c => { D[k][c] = c === 'RUB' ? 0 : null; }));
  return D;
};

test('#334, #336 в браузере', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('#334: сдвигов нет (null от сервера) — C05, C06, C09, C12: «сдвигов нет в прогоне», без NaN / null', async () => {
      const D = noMoves();
      let { pg, ctx, errors } = await open(browser, 'c05-morning-brief.html', { D });
      assert.deepStrictEqual(await graphicTexts(pg, '#curveChart'), ['сдвигов нет в прогоне'], 'C05');
      assert.ok(!BAD.test(await pg.locator('body').innerText()), 'C05: текст');
      assert.deepStrictEqual(errors, [], 'C05'); await ctx.close();

      ({ pg, ctx, errors } = await open(browser, 'c06-launchpad.html', { D }));
      const sel = await pg.evaluate(() => { const t = [...document.querySelectorAll('.tile')].find(x => /Кривые/.test(x.querySelector('h3').textContent)); const c = t.querySelector('[_echarts_instance_]'); c.setAttribute('data-cv', '1'); return '[data-cv]'; });
      assert.deepStrictEqual(await graphicTexts(pg, sel), ['сдвигов нет в прогоне'], 'C06');
      const body6 = await pg.locator('body').innerText();
      assert.ok(!BAD.test(body6), 'C06: текст');
      assert.ok(/USD\/RUB[\s\S]{0,80}—/.test(body6), 'C06: изменение курса — «—»');
      assert.deepStrictEqual(errors, [], 'C06'); await ctx.close();

      ({ pg, ctx, errors } = await open(browser, 'c09-market.html', { D }));
      await pg.locator('#cCurve button', { hasText: 'изм. дня' }).click(); await pg.waitForTimeout(200);
      assert.deepStrictEqual(await graphicTexts(pg, '#curves'), ['сдвигов нет в прогоне'], 'C09');
      assert.ok(!BAD.test(await pg.locator('body').innerText()), 'C09: текст');
      assert.deepStrictEqual(errors, [], 'C09'); await ctx.close();

      ({ pg, ctx, errors } = await open(browser, 'c12-terminal.html', { D }));
      await pg.locator('#cmd').fill('curve'); await pg.locator('#cmd').press('Enter'); await pg.waitForTimeout(300);
      const notes = await pg.locator('.k-note', { hasText: 'сдвигов нет в прогоне' }).allInnerTexts();
      assert.strictEqual(notes.length, 3, 'C12: RUONIA, КС, ОФЗ — ' + notes);
      assert.ok(!BAD.test(await pg.locator('body').innerText()), 'C12: текст');
      assert.deepStrictEqual(errors, [], 'C12'); await ctx.close();
    });
    await t.test('#336: IRS / XCCY / CDS — «Деривативы»: C03 «тип → срок», C04 «Тип инструмента»; OTHER — «Прочее»', async () => {
      let { pg, ctx, errors } = await open(browser, 'c03-blotter.html', { store: { 'c03-state': JSON.stringify({ v: 2, grouping: 'type' }) } });
      let txt = await pg.locator('body').innerText();
      for (const g of ['Деривативы · IRS / OIS', 'Деривативы · XCCY', 'Деривативы · OTC-деривативы (по отчёту)', 'Прочее · Прочее']) assert.ok(txt.includes(g), 'C03: ' + g);
      assert.deepStrictEqual(errors, [], 'C03'); await ctx.close();
      ({ pg, ctx, errors } = await open(browser, 'c04-pivot.html', { hash: '#r=type&c=none&m=count&v=table' }));
      txt = await pg.locator('body').innerText();
      for (const g of ['Деривативы · IRS / OIS', 'Деривативы · XCCY', 'Прочее · Прочее']) assert.ok(txt.includes(g), 'C04: ' + g);
      assert.deepStrictEqual(errors, [], 'C04'); await ctx.close();
      for (const file of ['c01-cockpit.html', 'c13-issuers.html', 'c11-history.html']) {
        ({ pg, ctx, errors } = await open(browser, file));
        assert.deepStrictEqual(errors, [], file); await ctx.close();
      }
    });
  } finally {
    await browser.close();
  }
});
