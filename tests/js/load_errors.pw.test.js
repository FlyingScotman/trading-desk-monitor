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

// run: { status, body } ответа /concepts-run.js или 'abort' (сеть), либо функция (URL запроса) → такой ответ;
// data — то же для /concepts-data.js; auth — тело ответа /api/v1/auth/state, { status } или 'hang' (не отвечает);
// store — localStorage до первой загрузки (один раз: перезагрузка его не восстанавливает); viewport — размер окна
async function open(browser, file, { run, data, auth, store, viewport, query = '' } = {}) {
  const ctx = await browser.newContext(viewport ? { viewport } : {}), pg = await ctx.newPage();
  if (store) await ctx.addInitScript(s => { if (sessionStorage.getItem('test.store')) return; sessionStorage.setItem('test.store', '1'); for (const k in s) localStorage.setItem(k, s[k]); }, store);
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') {
      const spec = typeof run === 'function' ? run(u) : run;
      if (spec === 'abort') return route.abort('connectionrefused');
      return route.fulfill(Object.assign({ status: 200, contentType: 'application/javascript' }, spec || { body: 'window.CONCEPTS_RUN = ' + RUN + ';' }));
    }
    if (u.pathname === '/concepts-data.js') {
      if (data === 'abort') return route.abort('connectionrefused');
      return route.fulfill(Object.assign({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' }, data || {}));
    }
    if (u.pathname === '/api/v1/auth/state') {
      if (auth === 'hang') return;  // не отвечает никогда: страница не должна ждать
      if (auth && auth.status) return route.fulfill({ status: auth.status, contentType: 'application/json', body: '{}' });
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(auth || { enabled: false, user: null }) });
    }
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
      assert.strictEqual(await pg.getAttribute('.k-loadfail [data-a=demo]', 'href'), '/concepts/c01-cockpit.html?demo=1');
      assert.strictEqual(await pg.getAttribute('.k-loadfail [data-a=index]', 'href'), 'index.html');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('«Последний прогон»: снимает run_id / as_of из адреса и выбор desk.run.v1', async () => {
      const saved = JSON.stringify({ sel: { run_id: '7' }, at: Date.now() });
      let { pg, ctx } = await open(browser, 'c02-risk-ladder.html', { run: { status: 500, body: '' }, query: '?run_id=7&x=1#k', store: { 'desk.run.v1': saved } });
      assert.strictEqual(await pg.getAttribute('.k-loadfail [data-a=latest]', 'href'), '/concepts/c02-risk-ladder.html?x=1#k');
      await pg.click('.k-loadfail [data-a=latest]');
      await pg.waitForURL(u => !/run_id/.test(u));
      await ctx.close();
      // выбор только из localStorage, в адресе #… (C04, C06): ссылка совпадает с текущим адресом — щелчок всё равно
      // перезагружает страницу (не переход к якорю), очищает desk.run.v1 и открывает последний прогон
      for (const hash of ['#x', '']) {
        const run7 = u => u.searchParams.get('run_id') === '7' ? { status: 500, body: '' } : null;  // прогон #7 не отдаётся, последний — да
        ({ pg, ctx } = await open(browser, 'c02-risk-ladder.html' + hash, { run: run7, store: { 'desk.run.v1': saved } }));
        assert.ok((await look(pg)).fail.includes('прогон #7'), hash);
        await Promise.all([pg.waitForEvent('load'), pg.click('.k-loadfail [data-a=latest]')]);
        await pg.waitForTimeout(300);
        const s = await look(pg);
        assert.strictEqual(s.source, 'real', hash + ': открыт последний прогон');
        assert.strictEqual(s.fail, null, hash);
        assert.strictEqual(await pg.evaluate(() => localStorage.getItem('desk.run.v1')), null, hash);
        await ctx.close();
      }
    });
    await t.test('auth/state не отвечает: полоса уже на экране, остаётся «Данные не загрузились»', async () => {
      const t0 = Date.now();
      const { pg, ctx, errors } = await open(browser, 'c01-cockpit.html', { run: { status: 500, body: '' }, auth: 'hang' });
      assert.ok(Date.now() - t0 < 2500, 'страница не ждёт auth/state');
      assert.strictEqual((await look(pg)).source, 'error');
      await pg.waitForTimeout(3300);  // таймаут запроса входа (3 с) прошёл
      const s = await look(pg);
      assert.strictEqual(s.source, 'error');
      assert.ok(s.fail.includes('Данные не загрузились'));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('auth/state ответил 500: «Данные не загрузились»; 401 — «войдите снова» и вход', async () => {
      let { pg, ctx } = await open(browser, 'c01-cockpit.html', { run: { status: 500, body: '' }, auth: { status: 500 } });
      assert.strictEqual((await look(pg)).source, 'error');
      await ctx.close();
      ({ pg, ctx } = await open(browser, 'c01-cockpit.html', { run: { status: 401, body: '' }, auth: { status: 401 } }));
      await pg.waitForURL(/\/login\.html/);
      await ctx.close();
    });
    await t.test('полоса — в теме пользователя (светлая) и с полями 16 px на 390 px', async () => {
      const { pg, ctx } = await open(browser, 'c01-cockpit.html', { run: { status: 500, body: '' }, store: { 'concepts-theme': 'light' }, viewport: { width: 390, height: 800 } });
      assert.strictEqual(await pg.evaluate(() => document.documentElement.getAttribute('data-theme')), 'light');
      const r = await pg.evaluate(() => { const b = document.querySelector('.k-loadfail').getBoundingClientRect(); return [b.left, innerWidth - b.right, document.documentElement.scrollWidth, innerWidth]; });
      assert.ok(r[0] >= 16 && r[1] >= 16, 'поля ' + r);
      assert.strictEqual(r[2], r[3], 'без прокрутки вбок');
      await ctx.close();
    });
    await t.test('не загрузился только /concepts-data.js: монитор на прогоне, причина — в «Чего нет в данных»', async () => {
      for (const data of [{ status: 500, body: '' }, 'abort']) {
        const { pg, ctx, errors } = await open(browser, 'c01-cockpit.html', { data });
        const s = await look(pg);
        assert.strictEqual(s.source, 'real'); assert.strictEqual(s.fail, null);
        assert.ok(await pg.evaluate(() => MOCK.notes.some(n => n.includes('/concepts-data.js не загрузился'))));
        assert.deepStrictEqual(errors, []);
        await ctx.close();
      }
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
    await t.test('#308: прогон без кривой RUONIA и без точек КС — C05, C06, C09, C12 не падают, «вчера» без выдуманных точек', async () => {
      const D = JSON.parse(RUN);
      delete D.CURVES['RUB RUONIA'];
      D.CURVES['RUB KEY'].pts = D.CURVES['RUB KEY'].pts.map(() => null);
      D.CURVES['RUB OFZ'].pts[3] = null;  // и одна пропущенная точка у КБД — разрыв, не падение
      // #322: у старого прогона без кривой RUONIA нет и её сдвигов; у КБД пропущен один сдвиг — там null, а не 0
      delete D.CURVE_MOVES['RUB RUONIA'];
      D.CURVE_MOVES['RUB OFZ'][5] = null;
      const MV = D.CURVE_MOVES, KBD = D.CURVES['RUB OFZ'].pts;
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
      assert.ok((await look(pg)).text.includes('кривой RUONIA нет в этом прогоне'));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      // C05 «Кривые RUB» и C09 «Кривые» (уровни): линия «вчера» есть только там, где есть «сегодня»; null − Δ давало
      // точки около 0 % (−Δ/100) — ставки здесь все выше 1 %
      for (const [file, id] of [['c05-morning-brief.html', 'curveChart'], ['c09-market.html', 'curves']]) {
        ({ pg, ctx, errors } = await open(browser, file, { run }));
        const ser = await pg.evaluate(id => echarts.getInstanceByDom(document.getElementById(id)).getOption().series
          .filter(x => x.type === 'line').map(x => [x.name, x.data.map(v => v && typeof v === 'object' ? v.value : v)]), id);
        assert.ok(ser.length >= 4, file + ': линии ' + ser.map(x => x[0]));
        for (const [name, d] of ser) assert.ok(d.every(v => v == null || v > 1), file + ' ' + name + ': ' + d);
        const ruo = ser.filter(x => /RUONIA/.test(x[0]));
        assert.ok(ruo.length && ruo.every(x => x[1].every(v => v == null)), file + ': RUONIA нет — и «вчера» нет');
        if (file.startsWith('c09')) {  // C09: «ОФЗ вчера» считается и без RUONIA (КБД − Δ ОФЗ); режимы «изм. дня» и «базис ОФЗ»
          const ofz = ser.find(x => x[0] === 'ОФЗ')[1], ofzY = ser.find(x => /^ОФЗ /.test(x[0]))[1];
          // #322: значения, а не только места null: ОФЗ вчера = КБД − ΔКБД/100; нет точки КБД или её сдвига — null
          ofzY.forEach((v, k) => {
            if (KBD[k] == null || MV['RUB OFZ'][k] == null) assert.strictEqual(v, null, 'C09 ОФЗ вчера ' + k + ': ' + ofzY);
            else assert.ok(Math.abs(v - (KBD[k] - MV['RUB OFZ'][k] / 100)) < 1e-9, 'C09 ОФЗ вчера ' + k + ': ' + v + ' vs ' + (KBD[k] - MV['RUB OFZ'][k] / 100));
          });
          // «изм. дня»: столбцы — сдвиги прогона; где сдвига нет (RUONIA целиком, КБД в одном бакете) — пропуск, а не 0
          await pg.locator('#cCurve button', { hasText: 'изм. дня' }).click(); await pg.waitForTimeout(200);
          const chg = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('curves')).getOption().series.map(x => [x.name, x.data]));
          const want = { 'ОФЗ': MV['RUB OFZ'], RUONIA: MV['RUB RUONIA'], 'КС': MV['RUB KEY'] };
          assert.strictEqual(chg.length, 3, 'C09 изм. дня: ' + chg.map(x => x[0]));
          for (const [name, d] of chg) {
            const w = want[name.split(' ')[0]];
            d.forEach((v, k) => assert.strictEqual(v, w && w[k] != null ? w[k] : null, 'C09 изм. дня ' + name + ' ' + k + ': ' + d));
          }
          await pg.locator('#cCurve button', { hasText: 'базис ОФЗ' }).click(); await pg.waitForTimeout(200);
          const bas = await pg.evaluate(() => echarts.getInstanceByDom(document.getElementById('curves')).getOption().series
            .map(x => [x.name, x.data.map(v => v && typeof v === 'object' ? v.value : v)]));
          const today = bas.find(x => /базис/.test(x[0]))[1], yest = bas.find(x => x[0] === 'вчера')[1];
          assert.ok(yest.every((v, k) => (v == null) === (today[k] == null)), 'C09 базис: «вчера» только там, где есть «сегодня»: ' + yest);
        }
        assert.deepStrictEqual(errors, [], file);
        await ctx.close();
      }
    });
  } finally {
    await browser.close();
  }
});
