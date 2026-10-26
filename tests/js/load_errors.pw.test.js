// Мониторы в браузере (Playwright), без сервера: страницы prototypes/ отдаются из файлов через page.route, ответы
// сервера подменяются.
//  - #309: /concepts-run.js ответил 401, 500 или не дошёл (сеть) — монитор не показывает демо-цифры: вместо него полоса
//    «данные не загрузились» (источник 'error') или переход на вход (401 при включённом входе); демо — только ?demo=1;
//  - #308: у прогона нет кривой RUONIA, у КС точек нет (старый прогон) — C06 и C12 не падают, вместо графика — «нет».
//   node --test tests/js/load_errors.pw.test.js
// Нужен Playwright (глобальный dev-машины или NODE_PATH, как у tools/smoke.js); без него тесты пропускаются — в CI они
// идут отдельным шагом после установки Playwright.
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

// run: { status, body } ответа /concepts-run.js или 'abort' (сеть); auth — ответ /api/v1/auth/state
async function open(browser, file, { run, auth, query = '' } = {}) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') {
      if (run === 'abort') return route.abort('connectionrefused');
      return route.fulfill(Object.assign({ status: 200, contentType: 'application/javascript' }, run || { body: 'window.CONCEPTS_RUN = ' + RUN + ';' }));
    }
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(auth || { enabled: false, user: null }) });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    if (u.pathname === '/login.html') return route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><title>Вход</title><p>вход</p>' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + file + query);
  await pg.waitForTimeout(400);
  return { pg, ctx, errors };
}
// что видно на странице: источник, есть ли демо-набор/кит, полоса ошибки и текст страницы
const look = pg => pg.evaluate(() => ({
  source: window.CONCEPTS_SOURCE, mock: !!window.MOCK, kit: !!window.KIT,
  fail: (document.querySelector('.k-loadfail') || {}).textContent || null,
  text: document.body.innerText
}));

test('#309 и #308 в браузере', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('500 от /concepts-run.js: на всех C-страницах — «данные не загрузились», без демо-цифр', async () => {
      for (const file of PAGES) {
        const { pg, ctx, errors } = await open(browser, file, { run: { status: 500, body: 'Internal Server Error' } });
        const s = await look(pg);
        assert.strictEqual(s.source, 'error', file);
        assert.ok(s.fail && s.fail.includes('Данные не загрузились'), file + ': полоса ошибки');
        assert.strictEqual(s.mock, false, file + ': демо-набор не загружен');
        assert.strictEqual(s.kit, false, file + ': монитор не рисуется');
        assert.ok(!/\d{3}/.test(s.text.replace(/#\d+|\/concepts-run\.js/g, '')), file + ': на странице нет цифр: ' + s.text);
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
    await t.test('сеть (запрос не дошёл): то же; «Повторить» и ссылка на тестовые данные по выбору', async () => {
      const { pg, ctx, errors } = await open(browser, 'c01-cockpit.html', { run: 'abort', query: '?run_id=7' });
      const s = await look(pg);
      assert.strictEqual(s.source, 'error');
      assert.ok(s.fail.includes('прогон #7'), s.fail);
      assert.ok(await pg.$('.k-loadfail [data-a=retry]'));
      assert.strictEqual(await pg.getAttribute('.k-loadfail a', 'href'), '/concepts/c01-cockpit.html?demo=1');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('401 при включённом входе: переход на /login.html?next=эта страница', async () => {
      const { pg, ctx } = await open(browser, 'c06-launchpad.html', { run: { status: 401, body: '{"detail":"нужен вход"}' }, auth: { enabled: true, user: null }, query: '?run_id=3' });
      await pg.waitForURL(/\/login\.html/);
      const u = new URL(pg.url());
      assert.strictEqual(u.pathname, '/login.html');
      assert.strictEqual(u.searchParams.get('next'), '/concepts/c06-launchpad.html?run_id=3');
      await ctx.close();
    });
    await t.test('ответ не скрипт прогона (страница прокси вместо него): тоже ошибка, не демо', async () => {
      const { pg, ctx } = await open(browser, 'c03-blotter.html', { run: { status: 200, contentType: 'text/html', body: '<html>proxy</html>' } });
      const s = await look(pg);
      assert.strictEqual(s.source, 'error');
      assert.strictEqual(s.mock, false);
      await ctx.close();
    });
    await t.test('?demo=1 — демо по выбору; прогон пришёл — настоящий; база пуста — «empty»', async () => {
      let r = await open(browser, 'c01-cockpit.html', { run: { status: 500, body: '' }, query: '?demo=1' });
      assert.strictEqual((await look(r.pg)).source, 'demo'); await r.ctx.close();
      r = await open(browser, 'c01-cockpit.html');
      let s = await look(r.pg);
      assert.strictEqual(s.source, 'real'); assert.strictEqual(s.fail, null); assert.ok(s.kit); await r.ctx.close();
      r = await open(browser, 'c01-cockpit.html', { run: { status: 200, body: 'window.CONCEPTS_RUN_EMPTY = {"reason":"no_runs","message":"нет опубликованных прогонов"};' } });
      s = await look(r.pg);
      assert.strictEqual(s.source, 'empty'); assert.strictEqual(s.fail, null); await r.ctx.close();
    });
    await t.test('#308: прогон без кривой RUONIA и без точек КС — C06 и C12 не падают, вместо графика «нет»', async () => {
      const D = JSON.parse(RUN);
      delete D.CURVES['RUB RUONIA'];
      D.CURVES['RUB KEY'].pts = D.CURVES['RUB KEY'].pts.map(() => null);
      D.CURVES['RUB OFZ'].pts[3] = null;  // и одна пропущенная точка у КБД — разрыв, не падение
      const run = { status: 200, body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' };
      let { pg, ctx, errors } = await open(browser, 'c06-launchpad.html', { run });
      assert.ok((await look(pg)).text.includes('кривой RUONIA нет в этом прогоне'));
      for (const [mode, text] of [['КС', 'кривой КС нет в этом прогоне'], ['ОФЗ', null], ['RUONIA', 'кривой RUONIA нет в этом прогоне']]) {
        const b = pg.locator('.tile', { has: pg.locator('h3', { hasText: 'Кривые' }) }).locator('.ms button', { hasText: mode });
        await b.click(); await pg.waitForTimeout(200);  // переключатель кривой плитки «Кривые»
        const tb = await pg.locator('.tile', { has: pg.locator('h3', { hasText: 'Кривые' }) }).locator('.tb').innerText();
        if (text) assert.ok(tb.includes(text), mode + ': ' + tb); else assert.ok(!tb.includes('нет в этом прогоне'), mode + ': график КБД');
      }
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      ({ pg, ctx, errors } = await open(browser, 'c12-terminal.html', { run }));
      const inp = await pg.$('input');
      await inp.fill('кривая'); await inp.press('Enter'); await pg.waitForTimeout(300);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally {
    await browser.close();
  }
});
