// Скрытые колонки таблиц позиций в браузере (TDM#763, Playwright): кнопка «Колонки ▾» в шапке таблицы, список колонок с
// галочками, «Как было», колонка-идентификатор не скрывается, выбор переживает перезагрузку, CSV — по видимым колонкам;
// C03: выбор поверх набора колонок, отдельно по наборам; страница на 390 px: меню целиком на экране, листается, закрывается;
// с сервером (страница через http, ответы сервера подставлены): выбор берётся с сервера и уходит на сервер (PUT / DELETE),
// у другого пользователя — свой.
//   node --test tests/js/table_columns_763.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const { openWith } = require('./fixtures/pw_pages');
const { TEXT: RUN } = require('./fixtures/concepts_run');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const PAGE = f => pathToFileURL(path.join(ROOT, 'concepts', f)).href;
const skip = !pw && 'нет Playwright';
const POS = ['name', 'shortName', 'ccy', 'issuerName', 'kind', 'book', 'couponFmt', 'event', 'maturity', 'tenor', 'notional', 'tradeDay', 'price', 'ytm', 'zspread', 'mdur', 'dv01', 'cs01', 'carryDay', 'fundingDay', 'pnl', 'mv'];  // C01 «Позиции»: key колонок

async function launch(width) {
  const browser = await pw.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  return { browser, ctx, pg, errors };
}
// колонки таблицы позиций на экране: ключи заголовков первой таблицы с кнопкой «Колонки»
const heads = pg => pg.evaluate(() => { const b = document.querySelector('.k-colbtn'), h = b && b.closest('.bd, .k-panel, body'); return [...h.querySelectorAll('table.k-tbl th[data-k]')].map(th => th.dataset.k); });
const btnText = pg => pg.evaluate(() => document.querySelector('.k-colbtn').textContent);
const openMenu = async pg => { await pg.click('.k-colbtn'); await pg.waitForSelector('.k-colmenu'); };
const menuRows = pg => pg.evaluate(() => [...document.querySelectorAll('.k-colmenu-row')].map(r => { const i = r.querySelector('input'); return { key: i.dataset.col, on: i.checked, fixed: i.disabled, text: r.textContent.trim() }; }));
const toggle = (pg, key) => pg.click('.k-colmenu input[data-col="' + key + '"]');
const csvOf = async (pg, sel) => { const [d] = await Promise.all([pg.waitForEvent('download'), pg.click(sel)]); return fs.readFileSync(await d.path(), 'utf8').replace(/^﻿/, ''); };
const csvHead = text => text.split(/\r?\n/)[2];
// готовность страницы не зависит от записи в localStorage (Chromium отдаёт её документу не сразу, см. fixtures/pw_pages.js):
// у C01 — кнопка выбранной вкладки (class on), дальше тест сам ждёт .k-colbtn
const READY = () => window.KIT && document.querySelector('button.on');
const C01 = PAGE('c01-cockpit.html');
const openC01 = async (pg, extra) => { await openWith(pg, C01, READY, Object.assign({ 'c01-tab': 'pos' }, extra)); await pg.waitForSelector('.k-colbtn'); };
// «перезагрузка с тем, что страница записала»: снимок localStorage страницы (проверяется тестом) и открытие с ним через openWith,
// чтобы запись не потерялась по дороге в браузер
const reopen = async (pg, url, ready) => {
  const store = await pg.evaluate(() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])));
  await openWith(pg, url, ready, store);
  return store;
};

test('C01 «Позиции»: скрыть и вернуть колонки, «Как было», идентификатор не скрывается, перезагрузка, CSV', { skip }, async () => {
  const { browser, pg, errors } = await launch(1400);
  try {
    await openC01(pg);
    assert.deepStrictEqual(await heads(pg), POS);
    assert.strictEqual(await btnText(pg), 'Колонки ▾');
    await openMenu(pg);
    const rows = await menuRows(pg);
    assert.deepStrictEqual(rows.map(r => r.key), POS, 'в меню все колонки таблицы, по порядку');
    assert.ok(rows.every(r => r.on), 'по умолчанию все показаны');
    assert.deepStrictEqual(rows.filter(r => r.fixed).map(r => r.key), ['name'], 'идентификатор — не скрывается');
    assert.ok(/Бумага \(всегда\)/.test(rows[0].text) && /Дюр\./.test(rows.find(r => r.key === 'mdur').text));
    assert.ok(/DV01\s*тыс ₽\/бп/.test(rows.find(r => r.key === 'dv01').text), 'единица рядом с подписью: ' + rows.find(r => r.key === 'dv01').text);
    assert.ok(/Показано 22 из 22/.test(await pg.textContent('.k-colmenu-t')));
    assert.strictEqual(await pg.isDisabled('.k-colmenu-ft .k-btn:first-child'), true, '«Как было» не нужна, пока ничего не скрыто');
    // скрыть две колонки
    await toggle(pg, 'zspread'); await toggle(pg, 'mdur');
    assert.deepStrictEqual(await heads(pg), POS.filter(k => k !== 'zspread' && k !== 'mdur'));
    assert.ok(/Показано 20 из 22/.test(await pg.textContent('.k-colmenu-t')));
    assert.strictEqual(await btnText(pg), 'Колонки · скрыто 2 ▾');
    // CSV — без скрытых колонок
    await pg.keyboard.press('Escape');
    assert.strictEqual(await pg.locator('.k-colmenu').count(), 0, 'Esc закрывает меню');
    const head = csvHead(await csvOf(pg, '.k-tbl-ctl .k-tbtn:has-text("CSV")'));
    assert.ok(!/Спред|Дюр/.test(head) && /Бумага/.test(head) && /DV01/.test(head), 'CSV без скрытых колонок: ' + head);
    // перезагрузка: выбор на месте (демо: в браузере)
    const saved = await reopen(pg, C01, READY); await pg.waitForSelector('.k-colbtn');
    assert.deepStrictEqual(JSON.parse(saved['tdm.view.v1:cols:c01-positions']), { hidden: ['zspread', 'mdur'] }, 'страница записала выбор');
    assert.deepStrictEqual(await heads(pg), POS.filter(k => k !== 'zspread' && k !== 'mdur'));
    assert.strictEqual(await btnText(pg), 'Колонки · скрыто 2 ▾');
    // «Как было»
    await openMenu(pg);
    assert.ok(/только в этом браузере/.test(await pg.textContent('.k-colmenu-note')), 'без сервера меню говорит, где хранится выбор');
    await pg.click('.k-colmenu-ft .k-btn:first-child');
    assert.deepStrictEqual(await heads(pg), POS);
    assert.ok((await menuRows(pg)).every(r => r.on));
    await pg.click('.k-colmenu-ft .k-btn:last-child');  // Готово
    assert.strictEqual(await pg.locator('.k-colmenu').count(), 0);
    assert.strictEqual(await btnText(pg), 'Колонки ▾');
    const after = await reopen(pg, C01, READY); await pg.waitForSelector('.k-colbtn');
    assert.ok(!('tdm.view.v1:cols:c01-positions' in after), 'копия выбора удалена');
    assert.deepStrictEqual(await heads(pg), POS, 'и после перезагрузки — как было');
    // новая колонка в будущей версии видна: в хранилище скрыта только 'dv01' и ключ, которого уже нет
    await openC01(pg, { 'tdm.view.v1:cols:c01-positions': JSON.stringify({ hidden: ['dv01', 'removed-in-v9', 'name'] }) });
    assert.deepStrictEqual(await heads(pg), POS.filter(k => k !== 'dv01'), 'лишний ключ пропущен, идентификатор сохранённым скрытым не стал');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('C03: выбор поверх набора колонок — отдельно по наборам, «Бумага» всегда, CSV по видимым', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openWith(pg, PAGE('c03-blotter.html'), () => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt, { 'c03-state': JSON.stringify({ v: 2, preset: 'base', grouping: 'none', color: 'issuer' }) });
    const ths = () => pg.evaluate(() => [...document.querySelectorAll('#tbl table th[data-k]')].map(th => th.dataset.k));
    const base = await ths();
    assert.ok(base.includes('dv01') && base.includes('isin') && base[0] === 'name');
    assert.strictEqual(await btnText(pg), 'Колонки ▾');
    await openMenu(pg);
    const rows = await menuRows(pg);
    assert.deepStrictEqual(rows.map(r => r.key), base, 'в меню — состав выбранного набора');
    assert.deepStrictEqual(rows.filter(r => r.fixed).map(r => r.key), ['name']);
    await toggle(pg, 'dv01'); await toggle(pg, 'isin');
    assert.deepStrictEqual(await ths(), base.filter(k => k !== 'dv01' && k !== 'isin'));
    // сортировка по скрытой колонке снимается вместе с ней
    await pg.keyboard.press('Escape');
    await pg.click('#tbl th[data-k="cs01"]');
    assert.ok(await pg.locator('#tbl th[data-k="cs01"] .si').count());
    await openMenu(pg); await toggle(pg, 'cs01'); await pg.keyboard.press('Escape');
    assert.strictEqual(await pg.locator('#tbl tbody tr.row').first().getAttribute('data-id') !== null, true);
    const head = csvHead(await csvOf(pg, '#tbl + .k-tbl-ctlbar .k-tbtn:has-text("CSV"), .k-tbl-ctlbar .k-tbtn:has-text("CSV")'));
    assert.ok(!/DV01|ISIN|CS01/.test(head) && /Бумага/.test(head), 'CSV — видимые колонки набора: ' + head);
    // другой набор — свой выбор: его dv01 на месте
    await pg.click('#sCols button:has-text("Риск")');
    const risk = await ths();
    assert.ok(risk.includes('dv01') && risk.includes('cs01'), 'в наборе «Риск» скрытое в «Базовом» не скрыто: ' + risk);
    await pg.click('#sCols button:has-text("Базовый")');
    assert.deepStrictEqual(await ths(), base.filter(k => k !== 'dv01' && k !== 'isin' && k !== 'cs01'));
    assert.strictEqual(await btnText(pg), 'Колонки · скрыто 3 ▾');
    await reopen(pg, PAGE('c03-blotter.html'), () => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt);
    assert.deepStrictEqual(await ths(), base.filter(k => k !== 'dv01' && k !== 'isin' && k !== 'cs01'), 'после перезагрузки — тот же выбор');
    await openMenu(pg); await pg.click('.k-colmenu-ft .k-btn:first-child');
    assert.deepStrictEqual(await ths(), base, '«Как было» — набор целиком');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('390 px: меню колонок целиком на экране, список листается, «Как было» и «Готово» достижимы', { skip }, async () => {
  const { browser, pg, errors } = await launch(390);
  try {
    await openC01(pg);
    await pg.evaluate(() => document.querySelector('.k-colbtn').scrollIntoView({ block: 'center' }));
    await openMenu(pg);
    const geo = () => pg.evaluate(() => {
      const m = document.querySelector('.k-colmenu').getBoundingClientRect(), l = document.querySelector('.k-colmenu-list');
      const bt = [...document.querySelectorAll('.k-colmenu-ft .k-btn')].map(b => b.getBoundingClientRect());
      return { left: m.left, right: m.right, top: m.top, bottom: m.bottom, vw: document.documentElement.clientWidth, vh: innerHeight, listScroll: l.scrollHeight > l.clientHeight, listH: l.clientHeight, btns: bt.map(r => ({ top: r.top, bottom: r.bottom, left: r.left, right: r.right })) };
    });
    let g = await geo();
    assert.ok(g.left >= 0 && g.right <= g.vw, 'по ширине экрана: ' + JSON.stringify(g));
    assert.ok(g.top >= 0 && g.bottom <= g.vh, 'по высоте экрана: ' + JSON.stringify(g));
    assert.ok(g.listScroll, '21 колонка на узком экране — список листается внутри меню');
    assert.ok(g.btns.every(b => b.top >= g.top && b.bottom <= g.bottom && b.left >= 0 && b.right <= g.vw), 'кнопки меню видны: ' + JSON.stringify(g.btns));
    assert.ok(g.listH >= 100, 'список не схлопнулся: ' + g.listH);
    // последняя колонка списка достижима прокруткой и скрывается тапом
    await pg.locator('.k-colmenu input[data-col="mv"]').scrollIntoViewIfNeeded();
    await toggle(pg, 'mv');
    assert.ok(!(await heads(pg)).includes('mv'));
    assert.strictEqual(await btnText(pg), 'Колонки · скрыто 1 ▾');
    // строки списка не уже пальца
    const rowH = await pg.evaluate(() => document.querySelector('.k-colmenu-row').getBoundingClientRect().height);
    assert.ok(rowH >= 30, 'строка списка ' + rowH + ' px');
    // щелчок снаружи закрывает
    await pg.mouse.click(5, 5);
    assert.strictEqual(await pg.locator('.k-colmenu').count(), 0);
    // поворот экрана и т. п.: сам размер окна закрывает меню
    await openMenu(pg);
    await pg.setViewportSize({ width: 360, height: 700 });
    await pg.waitForFunction(() => !document.querySelector('.k-colmenu'));
    // страница не получила горизонтальной прокрутки от меню
    assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), 'горизонтальной прокрутки страницы нет');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

// ---- с сервером: страница открыта через http, ответы сервера подставлены; у каждого пользователя — своё ----
async function serve(ctx, users) {
  const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' }, log = [];
  await ctx.route('http://desk.test/**', route => {
    const req = route.request(), u = new URL(req.url()), who = users.current;
    if (u.pathname === '/concepts-run.js') return route.fulfill({ contentType: types['.js'], body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: types['.js'], body: 'window.DESK_EXTRAS = { notes: [] };' });
    const m = /^\/api\/v1\/me\/table-columns(?:\/(.+))?$/.exec(u.pathname);
    if (m) {
      const mine = users[who].tables;
      if (req.method() === 'GET') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ user: who, tables: Object.fromEntries(Object.entries(mine).map(([k, h]) => [k, { hidden: h, updated_at: '2026-11-17T10:00:00Z' }])) }) });
      const key = decodeURIComponent(m[1]);
      log.push({ method: req.method(), key, body: req.postData(), type: req.headers()['content-type'], who });
      if (users.failWrites) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
      if (req.method() === 'PUT') mine[key] = JSON.parse(req.postData()).hidden; else delete mine[key];
      return route.fulfill(req.method() === 'PUT' ? { contentType: 'application/json', body: JSON.stringify({ hidden: mine[key], updated_at: '2026-11-17T10:01:00Z' }) } : { status: 204, body: '' });
    }
    const api = { '/api/v1/instruments/aliases': { aliases: [] }, '/api/v1/instruments/short-names': { short_names: [] }, '/api/v1/auth/state': { enabled: false, user: null }, '/api/v1/autopilot/status': { latest_run: null },
      '/api/v1/backfill/status': null, '/api/v1/views': { views: [] }, '/api/v1/events': { events: [], last_id: 0 }, '/api/v1/history/market': null, '/api/v1/calendar/market': null };
    if (Object.hasOwn(api, u.pathname)) return route.fulfill({ contentType: 'application/json', body: JSON.stringify(api[u.pathname]) });
    if (u.pathname === '/api/v1/events/stream') return route.fulfill({ contentType: 'text/event-stream', body: 'event: hello\ndata: {"last_id":0}\n\n' });
    const file = path.join(ROOT, u.pathname);
    if (!file.startsWith(ROOT + path.sep) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ contentType: types[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
  });
  return log;
}

test('с сервером: выбор берётся с сервера, правка уходит PUT, «Как было» — DELETE; у другого пользователя — свой', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const users = { ann: { tables: { 'c01-positions': ['mdur', 'removed-in-v9'] } }, bob: { tables: {} }, current: 'ann' };
    const open = async () => {  // новый контекст = чистый браузер: localStorage пуст, выбор может прийти только с сервера
      const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } }), pg = await ctx.newPage(), errors = [];
      pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
      const log = await serve(ctx, users);
      await ctx.addInitScript(() => { try { localStorage.setItem('c01-tab', 'pos'); } catch (e) { } });
      await pg.goto('http://desk.test/concepts/c01-cockpit.html');
      await pg.waitForFunction(() => window.KIT && document.querySelector('button.on'), null, { timeout: 20000 });
      await pg.waitForSelector('.k-colbtn');
      return { ctx, pg, errors, log };
    };
    // ann: сервер знает её выбор («Дюр.» скрыта; ключа, которого в таблице нет, — не видно)
    let s = await open();
    await s.pg.waitForFunction(() => document.querySelector('.k-colbtn').textContent.includes('скрыто 1'));
    assert.deepStrictEqual(await heads(s.pg), POS.filter(k => k !== 'mdur'), 'выбор — с сервера, в чистом браузере');
    await openMenu(s.pg);
    assert.strictEqual(await s.pg.textContent('.k-colmenu-note'), '', 'сервер на связи — предупреждения нет');
    await toggle(s.pg, 'cs01');
    await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(s.log.map(x => [x.method, x.key]), [['PUT', 'c01-positions']]);
    assert.strictEqual(s.log[0].type, 'application/json');
    assert.deepStrictEqual(JSON.parse(s.log[0].body), { hidden: ['mdur', 'cs01'] }, 'на сервер — скрытое (известные ключи), не показанное');
    assert.deepStrictEqual(users.ann.tables['c01-positions'], ['mdur', 'cs01']);
    // перезагрузка в чистом браузере — то же
    await s.ctx.close();
    s = await open();
    await s.pg.waitForFunction(() => document.querySelector('.k-colbtn').textContent.includes('скрыто 2'));
    assert.deepStrictEqual(await heads(s.pg), POS.filter(k => k !== 'mdur' && k !== 'cs01'));
    // bob в том же приложении: свой выбор — по умолчанию
    await s.ctx.close();
    users.current = 'bob';
    s = await open();
    assert.deepStrictEqual(await heads(s.pg), POS, 'у другого пользователя — все колонки');
    assert.strictEqual(await btnText(s.pg), 'Колонки ▾');
    await openMenu(s.pg); await toggle(s.pg, 'kind'); await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(users.bob.tables, { 'c01-positions': ['kind'] });
    assert.deepStrictEqual(users.ann.tables['c01-positions'], ['mdur', 'cs01'], 'выбор ann не тронут');
    // «Как было» — DELETE
    await s.pg.click('.k-colmenu-ft .k-btn:first-child'); await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(s.log.map(x => x.method), ['PUT', 'DELETE']);
    assert.deepStrictEqual(users.bob.tables, {});
    assert.strictEqual(s.log[1].type, 'application/json');
    // сервер не принимает запись: выбор остаётся на экране и в браузере, меню говорит об этом
    users.failWrites = true;
    await toggle(s.pg, 'book'); await s.pg.waitForTimeout(300);
    assert.ok(!(await heads(s.pg)).includes('book'));
    assert.ok(/сохранён в этом браузере и уйдёт на сервер/.test(await s.pg.textContent('.k-colmenu-note')), await s.pg.textContent('.k-colmenu-note'));
    assert.deepStrictEqual(s.errors, []);
    await s.ctx.close();
  } finally { await browser.close(); }
});
