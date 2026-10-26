// Мониторы в браузере (Playwright), без сервера: страницы prototypes/ отдаются из файлов через page.route, ответы
// сервера подменяются.
//  - #309: /concepts-run.js ответил 401, 500 или не дошёл (сеть) — монитор не показывает демо-цифры: вместо него полоса
//    «данные не загрузились» (источник 'error') или переход на вход (401 при включённом входе); демо — только ?demo=1;
//  - #319: прогона нет (CONCEPTS_RUN_EMPTY: no_runs, not_found) — тоже не демо: пустое состояние с причиной (источник
//    'empty'); C14 и C15 прогон не нужен, они работают; Ctrl / Shift-щелчок по «Последний прогон» — новая вкладка;
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
// store — localStorage до первой загрузки (один раз на контекст: ни перезагрузка, ни новая вкладка его не восстанавливают);
// api — { путь: тело JSON } ответов /api/… (остальные — 404); viewport — размер окна. Подмена — на весь контекст
// (ctx.route): новая вкладка (Ctrl-щелчок) открывается на тех же ответах
async function open(browser, file, { run, data, auth, store, viewport, api = {}, query = '' } = {}) {
  const ctx = await browser.newContext(viewport ? { viewport } : {}), pg = await ctx.newPage();
  if (store) await ctx.addInitScript(s => { if (localStorage.getItem('test.store')) return; localStorage.setItem('test.store', '1'); for (const k in s) localStorage.setItem(k, s[k]); }, store);
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await ctx.route('**/*', route => {
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
    if (api[u.pathname]) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(api[u.pathname]) });
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
// ответ /concepts-run.js без прогона (api/concepts_run.py: empty_script)
const EMPTY = (reason, message) => 'window.CONCEPTS_RUN_EMPTY = ' + JSON.stringify({ reason, message }) + ';';
// что видно на странице: источник, есть ли демо-набор/кит, полоса ошибки и текст страницы
const look = pg => pg.evaluate(() => ({
  source: window.CONCEPTS_SOURCE, mock: !!window.MOCK, kit: !!window.KIT,
  fail: (document.querySelector('.k-loadfail') || {}).textContent || null,
  norun: (document.querySelector('.k-norun') || {}).textContent || null,
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
      r = await open(browser, 'c01-cockpit.html', { run: { status: 200, body: EMPTY('no_runs', 'нет опубликованных прогонов') } });
      s = await look(r.pg);
      assert.strictEqual(s.source, 'empty'); assert.strictEqual(s.fail, null); assert.ok(s.norun); assert.strictEqual(s.mock, false); await r.ctx.close();
    });
    // #319: прогона нет — пустое состояние, не демо-набор. Мониторы — полоса с причиной и выходами, без цифр; C14 и C15
    // (data-run="optional") прогон не нужен — работают своими запросами, в шапке нет даты, бейджа и заметок демо-набора
    const OPTIONAL = ['c14-day-status.html', 'c15-cash.html'];
    const emptyCases = [
      { name: 'no_runs', body: EMPTY('no_runs', 'нет опубликованных прогонов'), query: '', title: 'Прогонов ещё нет', latest: null, day: 'c14-day-status.html' },
      { name: 'not_found по run_id', body: EMPTY('not_found', 'прогон 999999 не найден'), query: '?run_id=999999', title: 'Прогон #999999 не найден', latest: true, day: 'c14-day-status.html', allow: /999999/g },
      { name: 'not_found по as_of', body: EMPTY('not_found', 'нет опубликованных прогонов на 2000-01-01'), query: '?as_of=2000-01-01', title: 'Прогона за 2000-01-01 нет', latest: true, day: 'c14-day-status.html?on=2000-01-01', allow: /2000-01-01/g }
    ];
    for (const c of emptyCases) {
      await t.test('#319 ' + c.name + ': на всех C-страницах — пустое состояние без демо-цифр', async () => {
        for (const file of PAGES) {
          const { pg, ctx, errors } = await open(browser, file, { run: { status: 200, body: c.body }, query: c.query });
          const s = await look(pg);
          assert.strictEqual(s.source, 'empty', file);
          assert.strictEqual(s.fail, null, file);
          if (OPTIONAL.includes(file)) {  // экран без прогона: kit.js есть, шапка — без даты, бейджа и заметок демо-набора
            assert.ok(s.kit, file);
            assert.strictEqual(s.norun, null, file);
            const top = await pg.evaluate(() => ({ src: document.querySelector('.k-top [data-a=src]').textContent, badge: !!document.querySelector('.k-top [data-a=runage]'), notes: !!document.querySelector('.k-top [data-a=notes]'), text: document.querySelector('.k-top').innerText }));
            assert.ok(/^прогон(ов нет| не найден)/.test(top.src), file + ': ' + top.src);
            assert.ok(!top.badge && !top.notes, file + ': бейдж / заметки демо-набора');
            assert.ok(!/\d\d\.\d\d|поз\.|книги|ОФЗ:|в RUB/.test(top.text), file + ': дата, фильтры или курсы демо-набора в шапке: ' + top.text);
            await pg.evaluate(() => KIT.palette()); await pg.fill('.k-pal input', 'о');  // поиск: только экраны, без бумаг демо-набора
            const kinds = await pg.$$eval('.k-pal .res .k', l => [...new Set(l.map(x => x.textContent))]);
            assert.deepStrictEqual(kinds, ['экран'], file + ': поиск — ' + kinds);
          } else {
            assert.ok(s.norun && s.norun.includes(c.title), file + ': полоса — ' + s.norun);
            assert.strictEqual(s.mock, false, file + ': демо-набор не загружен');
            assert.strictEqual(s.kit, false, file + ': монитор не рисуется');
            const rest = c.allow ? s.text.replace(c.allow, '') : s.text;
            assert.ok(!/\d/.test(rest), file + ': на странице нет цифр: ' + s.text);
            const href = a => pg.evaluate(a => { const e = document.querySelector('.k-norun [data-a=' + a + ']'); return e ? e.getAttribute('href') : null; }, a);
            assert.strictEqual(await href('demo'), '/concepts/' + file + '?demo=1', file);
            assert.strictEqual(await href('index'), 'index.html', file);
            assert.strictEqual(await href('day'), c.day, file);
            assert.strictEqual(await href('settings'), c.latest ? null : '../settings.html', file + ': «Настройки» (автопилот) — только без прогонов');
            assert.strictEqual(await href('latest'), c.latest ? '/concepts/' + file : null, file);
            assert.ok(await pg.evaluate(() => !!(window.DESK_LIVE && DESK_LIVE.page)), file + ': ждёт первого прогона (lib/live.js)');
          }
          assert.deepStrictEqual(errors, [], file);
          await ctx.close();
        }
      });
    }
    await t.test('#319: not_found — «Последний прогон» снимает выбор и открывает последний; пустое состояние — в теме и на 390 px', async () => {
      const saved = JSON.stringify({ sel: { run_id: '999999' }, at: Date.now() });
      const run = u => u.searchParams.get('run_id') === '999999' ? { status: 200, body: EMPTY('not_found', 'прогон 999999 не найден') } : null;
      const { pg, ctx } = await open(browser, 'c03-blotter.html#x', { run, store: { 'desk.run.v1': saved, 'concepts-theme': 'light' }, viewport: { width: 390, height: 800 } });
      assert.ok((await look(pg)).norun.includes('Прогон #999999 не найден'));
      assert.strictEqual(await pg.evaluate(() => document.documentElement.getAttribute('data-theme')), 'light');
      const r = await pg.evaluate(() => { const b = document.querySelector('.k-norun').getBoundingClientRect(); return [b.left, innerWidth - b.right, document.documentElement.scrollWidth, innerWidth]; });
      assert.ok(r[0] >= 16 && r[1] >= 16, 'поля ' + r);
      assert.strictEqual(r[2], r[3], 'без прокрутки вбок');
      await Promise.all([pg.waitForEvent('load'), pg.click('.k-norun [data-a=latest]')]);
      await pg.waitForTimeout(300);
      const s = await look(pg);
      assert.strictEqual(s.source, 'real'); assert.strictEqual(s.norun, null);
      assert.strictEqual(await pg.evaluate(() => localStorage.getItem('desk.run.v1')), null);
      await ctx.close();
    });
    await t.test('#319: Ctrl / Shift / средняя кнопка по «Последний прогон» — новая вкладка на последнем прогоне, эта не перезагружается', async () => {
      const saved = JSON.stringify({ sel: { run_id: '7' }, at: Date.now() });
      const cases = [[{ status: 500, body: '' }, '.k-loadfail'], [{ status: 200, body: EMPTY('not_found', 'прогон 7 не найден') }, '.k-norun']];
      for (const [spec, sel] of cases) {
        for (const how of [{ modifiers: ['Control'] }, { modifiers: ['Shift'] }, { button: 'middle' }]) {
          const tag = sel + ' ' + JSON.stringify(how);
          const run = u => u.searchParams.get('run_id') === '7' ? spec : null;  // прогон #7 не отдаётся, последний — да
          const { pg, ctx } = await open(browser, 'c01-cockpit.html', { run, query: '?run_id=7', store: { 'desk.run.v1': saved } });
          await pg.waitForLoadState('load'); await pg.evaluate(() => { window.__stay = 1; });
          const [np] = await Promise.all([ctx.waitForEvent('page'), pg.click(sel + ' [data-a=latest]', how)]);
          await np.waitForURL(u => u.pathname === '/concepts/c01-cockpit.html' && !u.searchParams.has('run_id'));
          await np.waitForLoadState('load');
          assert.strictEqual(await np.evaluate(() => window.CONCEPTS_SOURCE), 'real', tag + ': новая вкладка — последний прогон');
          assert.strictEqual(await pg.evaluate(() => window.__stay), 1, tag + ': вкладка не перезагрузилась');
          assert.ok(/run_id=7/.test(pg.url()), tag + ': адрес вкладки тот же');
          assert.strictEqual(await pg.evaluate(() => localStorage.getItem('desk.run.v1')), null, tag + ': выбор снят и для новой вкладки');
          await ctx.close();
        }
      }
    });
    await t.test('#319: прогон вышел, пока пустая страница грузилась, — она переходит на него (сверка после загрузки)', async () => {
      let n = 0;
      const run = () => n++ ? null : { status: 200, body: EMPTY('no_runs', 'нет опубликованных прогонов') };  // первый ответ — пусто, дальше прогон
      const { pg, ctx, errors } = await open(browser, 'c01-cockpit.html', { run, api: { '/api/v1/autopilot/status': { latest_run: { run_id: 1, label: 'тест' } } } });
      await pg.waitForFunction(() => window.CONCEPTS_SOURCE === 'real', null, { timeout: 5000 });
      assert.strictEqual(n, 2, 'одна перезагрузка');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
      // прогонов всё ещё нет (latest_run: null) — остаётся пустое состояние, без перезагрузок
      let m = 0;
      const r2 = await open(browser, 'c01-cockpit.html', { run: () => (m++, { status: 200, body: EMPTY('no_runs', 'нет опубликованных прогонов') }), api: { '/api/v1/autopilot/status': { latest_run: null } } });
      await r2.pg.waitForTimeout(800);
      assert.strictEqual(m, 1); assert.strictEqual((await look(r2.pg)).source, 'empty');
      await r2.ctx.close();
    });
    await t.test('#319: CSV C14 и C15 без прогона — без номера и даты демо-набора', async () => {
      for (const file of OPTIONAL) {
        const { pg, ctx } = await open(browser, file, { run: { status: 200, body: EMPTY('no_runs', 'нет опубликованных прогонов') } });
        const c = await pg.evaluate(() => ({ head: KIT.csvBuild('т', ['a'], ['1']).split('\r\n')[0], name: KIT.csvFileName(document.body, { fileName: 'x' }) }));
        assert.ok(c.head.startsWith('run_id,,as_of,,'), file + ': ' + c.head);
        assert.strictEqual(c.name, 'tdm_x_nodate_run-none.csv', file);
        await ctx.close();
      }
    });
    await t.test('#308: прогон без кривой RUONIA и без точек КС — C05, C06, C09, C12 не падают, «вчера» без выдуманных точек', async () => {
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
          assert.ok(ofzY.every((v, k) => (v == null) === (ofz[k] == null)), 'C09 ОФЗ вчера: ' + ofzY);
          for (const mode of ['изм. дня', 'базис ОФЗ']) {
            await pg.locator('#cCurve button', { hasText: mode }).click(); await pg.waitForTimeout(200);
          }
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
