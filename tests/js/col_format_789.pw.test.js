// Формат чисел колонки в браузере (TDM#789, Playwright): кнопка формата в меню «Колонки ▾», выбор «Знаков после точки» и «Единицы»,
// формат сразу во всех таблицах страницы и на другом экране, «Как было» его не трогает, у каждой колонки свой сброс, CSV и итоги —
// как на экране, блоттер C03, страница на 390 px, сервер (GET / PUT / DELETE, у другого пользователя — свой, сервер не принял).
//   node --test tests/js/col_format_789.pw.test.js
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
const KEY = 'tdm.view.v1:colfmt';

async function launch(width) {
  const browser = await pw.chromium.launch();
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true });
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  return { browser, ctx, pg, errors };
}
const READY = () => window.KIT && document.querySelector('button.on');
const C01 = PAGE('c01-cockpit.html'), C03 = PAGE('c03-blotter.html'), C06 = PAGE('c06-launchpad.html');
const READY03 = () => window.KIT && document.querySelector('#tbl table') && document.querySelector('#tbl table').__kt;
const openC01 = async (pg, extra) => { await openWith(pg, C01, READY, Object.assign({ 'c01-tab': 'pos' }, extra)); await pg.waitForSelector('.k-colbtn'); };
const openC03 = async (pg, extra) => { await openWith(pg, C03, READY03, Object.assign({ 'c03-state': JSON.stringify({ v: 2, preset: 'risk', grouping: 'none', color: 'issuer' }) }, extra)); await pg.waitForSelector('.k-colbtn'); };
const store = pg => pg.evaluate(() => Object.fromEntries(Object.keys(localStorage).map(k => [k, localStorage.getItem(k)])));
const openMenu = async pg => { await pg.click('.k-colbtn'); await pg.waitForSelector('.k-colmenu'); };
const fmtBtn = (pg, key) => pg.click('.k-colmenu .k-colmenu-fmtbtn[data-fmt="' + key + '"]');
const selD = (pg, key) => pg.locator('.k-colmenu select[data-fmt-d="' + key + '"]');
const selU = (pg, key) => pg.locator('.k-colmenu select[data-fmt-u="' + key + '"]');
const resetFmt = (pg, key) => pg.click('.k-colmenu button[data-fmt-reset="' + key + '"]');
const csvOf = async (pg, sel) => { const [d] = await Promise.all([pg.waitForEvent('download'), pg.click(sel)]); return fs.readFileSync(await d.path(), 'utf8').replace(/^﻿/, ''); };
const CSV_BTN = '.k-tbl-ctl .k-tbtn:has-text("CSV")';
// ячейки колонки основной таблицы: [текст первой строки, текст итога], заголовок колонки
const colOf = (pg, key, scope) => pg.evaluate(([k, sc]) => {
  const t = [...document.querySelectorAll((sc || '') + ' table.k-tbl')].find(x => x.querySelector('th[data-k="' + k + '"]')), th = t.querySelector('th[data-k="' + k + '"]'), i = th.cellIndex;
  const r = t.querySelector('tbody tr.row'), foot = t.querySelector('tfoot tr');
  // итог: «Итого» занимает несколько колонок (colspan), ячейка колонки — та, что накрывает её номер
  let at = 0, total = null;
  if (foot) for (const c of foot.cells) { if (i >= at && i < at + c.colSpan) { total = c.textContent.trim(); break; } at += c.colSpan; }
  return { head: th.textContent.replace(/\s+/g, ' ').trim(), first: r.cells[i].textContent.trim(), total, firstCls: r.cells[i].className };
}, [key, scope]);

test('C01 «Позиции»: у числовых колонок есть кнопка формата, у текстовых нет; знаки, единицы, сброс колонки, «Как было» формат не трогает', { skip }, async () => {
  const { browser, pg, errors } = await launch(1400);
  try {
    await openC01(pg);
    const before = { notional: await colOf(pg, 'notional'), mv: await colOf(pg, 'mv'), dv01: await colOf(pg, 'dv01') };
    assert.ok(/^\(?[\d,]+\.\d\d\)?mm [A-Z]{3}$/.test(before.notional.first), 'номинал по умолчанию — mm, 2 знака: ' + before.notional.first);
    assert.ok(before.dv01.head.includes('тыс ₽/бп'), before.dv01.head);
    await openMenu(pg);
    const withBtn = await pg.evaluate(() => [...document.querySelectorAll('.k-colmenu-row')].filter(r => r.querySelector('.k-colmenu-fmtbtn')).map(r => r.querySelector('input').dataset.col));
    for (const k of ['notional', 'tradeDay', 'posDelta', 'price', 'ytm', 'zspread', 'mdur', 'dv01', 'cs01', 'carryDay', 'fundingDay', 'pnl', 'mv']) assert.ok(withBtn.includes(k), 'кнопка формата у ' + k);
    for (const k of ['name', 'shortName', 'ccy', 'issuerName', 'kind', 'book', 'couponFmt', 'event', 'maturity', 'tenor']) assert.ok(!withBtn.includes(k), 'нет кнопки у нечисловой ' + k);
    assert.strictEqual(await pg.locator('.k-colmenu-fmt').count(), 0, 'пока не раскрыто — только кнопки');
    // номинал: раскрыть, 3 знака — таблица меняется сразу, меню остаётся открытым
    await fmtBtn(pg, 'notional');
    assert.strictEqual(await pg.getAttribute('.k-colmenu .k-colmenu-fmtbtn[data-fmt="notional"]', 'aria-expanded'), 'true');
    const panel = await pg.textContent('.k-colmenu-fmt');
    assert.ok(panel.includes('Знаков после точки') && panel.includes('Единицы') && panel.includes('Формат по умолчанию') && panel.includes('Действует во всех таблицах позиций'), panel);
    assert.deepStrictEqual(await selD(pg, 'notional').locator('option').allTextContents(), ['по умолчанию (2)', '0', '1', '2', '3', '4', '5', '6']);
    assert.deepStrictEqual(await selU(pg, 'notional').locator('option').allTextContents(), ['по умолчанию (mm)', 'как есть', 'тыс', 'mm', 'bn']);
    assert.strictEqual(await pg.isDisabled('.k-colmenu button[data-fmt-reset="notional"]'), true, 'формат по умолчанию — сбрасывать нечего');
    await selD(pg, 'notional').selectOption('3');
    const n3 = await colOf(pg, 'notional');
    assert.ok(/^\(?[\d,]+\.\d{3}\)?mm [A-Z]{3}$/.test(n3.first), '3 знака, единица та же: ' + n3.first);
    assert.strictEqual(await pg.locator('.k-colmenu').count(), 1, 'меню не закрылось');
    assert.strictEqual(await pg.isDisabled('.k-colmenu button[data-fmt-reset="notional"]'), false);
    assert.ok(await pg.evaluate(() => document.querySelector('.k-colmenu-fmtbtn[data-fmt="notional"]').classList.contains('on')), 'кнопка колонки с изменённым форматом отмечена');
    assert.ok((await selD(pg, 'notional').locator('option').first().textContent()) === 'по умолчанию (2)');
    // единицы у суммы: MV в bn — единица в подписи колонки, итог и ячейки в новых единицах; сортировка и цвет — не сломаны
    await fmtBtn(pg, 'mv'); await selU(pg, 'mv').selectOption('bn');
    const mv = await colOf(pg, 'mv');
    assert.ok(mv.head.startsWith('MV, bn ₽'), 'подпись единицы: ' + mv.head);
    assert.ok(/^\(?[\d,]+\.\d+\)?$/.test(mv.first) && mv.first !== before.mv.first, 'MV в bn: ' + before.mv.first + ' → ' + mv.first);
    assert.ok(/\d/.test(mv.total), 'итог: ' + mv.total);
    // единицы у DV01: подпись под заголовком; у цены единиц нет (только знаки)
    await fmtBtn(pg, 'dv01'); await selU(pg, 'dv01').selectOption('1');
    const dv = await colOf(pg, 'dv01');
    assert.ok(dv.head.includes('₽/бп') && !dv.head.includes('тыс'), 'DV01 как есть: ' + dv.head);
    await fmtBtn(pg, 'price');
    assert.strictEqual(await selU(pg, 'price').count(), 0, 'у цены — только знаки');
    assert.strictEqual(await selD(pg, 'price').count(), 1);
    // CSV: то же число и та же подпись единицы, что на экране
    await pg.keyboard.press('Escape');
    const csv = (await csvOf(pg, CSV_BTN)).split(/\r?\n/);
    assert.ok(/"MV, bn ₽"/.test(csv[2]) && /"DV01, ₽\/бп"/.test(csv[2]) && /"Номинал, mm в вал\."/.test(csv[2]), csv[2]);
    // «Как было» состава колонок формат не трогает; у каждой колонки — свой сброс
    await openMenu(pg);
    await pg.click('.k-colmenu input[data-col="zspread"]');
    await pg.click('.k-colmenu-ft .k-btn:first-child');
    assert.deepStrictEqual(await colOf(pg, 'notional'), n3, '«Как было» формат номинала не тронул');
    assert.ok((await colOf(pg, 'mv')).head.startsWith('MV, bn ₽'));
    assert.ok(/формат чисел колонок не меняется/.test(await pg.getAttribute('.k-colmenu-ft .k-btn:first-child', 'title')));
    await fmtBtn(pg, 'notional'); await resetFmt(pg, 'notional');
    assert.deepStrictEqual(await colOf(pg, 'notional'), before.notional, 'сброс колонки — как было');
    assert.ok((await colOf(pg, 'mv')).head.startsWith('MV, bn ₽'), 'сброс одной колонки других не трогает');
    // выбор — в браузере (демо): запись colfmt, переживает перезагрузку
    const saved = await store(pg);
    assert.deepStrictEqual(JSON.parse(saved[KEY]), { dv01: { decimals: null, scale: '1' }, mv: { decimals: null, scale: 'bn' } }, 'у номинала формата нет — записи нет');
    await openWith(pg, C01, READY, saved); await pg.waitForSelector('.k-colbtn');
    assert.ok((await colOf(pg, 'mv')).head.startsWith('MV, bn ₽'), 'после перезагрузки формат на месте');
    assert.strictEqual((await colOf(pg, 'notional')).first, before.notional.first);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('формат колонки — один на все таблицы: две таблицы одной страницы, другой экран (C03, C06), перезагрузка', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openC01(pg);
    // вторая таблица позиций на той же странице: колонки из K.posCols, как у экранов
    await pg.evaluate(() => {
      const d = document.createElement('div'); d.id = 'second'; document.body.appendChild(d);
      KIT.table(d, { viewKey: 'c01-second-789', rowsFn: () => KIT.list().slice(0, 5), search: false, columns: KIT.cols(['name', 'dv01', 'carry', 'ytm']) });
    });
    const one = () => colOf(pg, 'dv01'), two = () => colOf(pg, 'dv01', '#second');
    const a0 = await one(), b0 = await two();
    assert.strictEqual(a0.first, b0.first, 'до настройки — одно и то же число');
    await openMenu(pg); await fmtBtn(pg, 'dv01'); await selD(pg, 'dv01').selectOption('3'); await selU(pg, 'dv01').selectOption('mm');
    const a1 = await one(), b1 = await two();
    assert.ok(a1.head.includes('mm ₽/бп') && b1.head.includes('mm ₽/бп'), 'подпись единицы в обеих: ' + a1.head + ' / ' + b1.head);
    assert.strictEqual(a1.first, b1.first, 'в обеих — одно число');
    assert.ok(/\.\d{3}\)?$/.test(a1.first), '3 знака: ' + a1.first);
    assert.notStrictEqual(a1.first, a0.first);
    await pg.keyboard.press('Escape');
    // другой экран с этой колонкой: блоттер C03 и запуск C06 — тот же формат из хранилища (копия браузера)
    const st = await store(pg);
    assert.deepStrictEqual(JSON.parse(st[KEY]), { dv01: { decimals: 3, scale: 'mm' } });
    await openC03(pg, st);
    const c3 = await colOf(pg, 'dv01', '#tbl');
    assert.strictEqual(c3.first, a1.first, 'C03: то же число в том же формате');
    assert.ok(c3.head.includes('mm ₽/бп'), 'C03: единица в заголовке, раз она не по умолчанию: ' + c3.head);
    assert.ok(/\.\d{3}\)?$/.test(c3.total), 'итог C03 — в том же формате: ' + c3.total);
    // CSV блоттера: то же число и та же подпись
    const csv3 = (await csvOf(pg, '.k-tbl-ctlbar .k-tbtn:has-text("CSV")')).split(/\r?\n/);
    assert.ok(csv3[2].includes('"DV01, mm ₽/бп"'), csv3[2]);
    await openWith(pg, C06, () => window.KIT && document.querySelector('table.k-tbl th[data-k="dv01"]'), st);
    const c06 = await colOf(pg, 'dv01');
    assert.ok(c06.head.includes('mm ₽/бп') && /\.\d{3}\)?$/.test(c06.first), 'C06: ' + c06.head + ' / ' + c06.first);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('C03: кнопка формата в меню, знаки и единицы в ячейках, итогах и CSV; блоттер перерисовывается без перезагрузки', { skip }, async () => {
  const { browser, pg, errors } = await launch(1500);
  try {
    await openC03(pg);
    const k0 = { mv: await colOf(pg, 'mv', '#tbl'), carry: await colOf(pg, 'carry', '#tbl'), dv01: await colOf(pg, 'dv01', '#tbl'), notional: await colOf(pg, 'notional', '#tbl') };
    assert.ok(k0.mv.head.startsWith('MV, mm ₽') && !k0.dv01.head.includes('тыс'), 'по умолчанию подписи прежние: ' + k0.mv.head + ' / ' + k0.dv01.head);
    await openMenu(pg);
    const withBtn = await pg.evaluate(() => [...document.querySelectorAll('.k-colmenu-row')].filter(r => r.querySelector('.k-colmenu-fmtbtn')).map(r => r.querySelector('input').dataset.col));
    for (const k of ['notional', 'mv', 'dv01', 'cs01', 'carry', 'funding']) assert.ok(withBtn.includes(k), 'C03: кнопка у ' + k);
    assert.ok(!withBtn.includes('name') && !withBtn.includes('issuer') && !withBtn.includes('event'), 'нет у нечисловых');
    await fmtBtn(pg, 'notional'); await selD(pg, 'notional').selectOption('0');
    assert.ok(/^\(?[\d,]+\)?mm [A-Z]{3}$/.test((await colOf(pg, 'notional', '#tbl')).first), 'номинал без знаков после точки: ' + (await colOf(pg, 'notional', '#tbl')).first);
    await fmtBtn(pg, 'carry'); await selU(pg, 'carry').selectOption('mm');
    const carry = await colOf(pg, 'carry', '#tbl');
    assert.ok(carry.head.includes('mm ₽'), 'подпись единицы появилась в заголовке: ' + carry.head);
    assert.ok(/\./.test(carry.first) || carry.first === '0', 'кэрри в mm: ' + carry.first);
    await fmtBtn(pg, 'mv'); await selU(pg, 'mv').selectOption('k');
    assert.ok((await colOf(pg, 'mv', '#tbl')).head.startsWith('MV, тыс ₽'), (await colOf(pg, 'mv', '#tbl')).head);
    await pg.keyboard.press('Escape');
    const csv = (await csvOf(pg, '.k-tbl-ctlbar .k-tbtn:has-text("CSV")')).split(/\r?\n/);
    assert.ok(csv[2].includes('"Номинал, mm в вал."') && csv[2].includes('"MV, тыс ₽"') && csv[2].includes('"Кэрри/д, mm ₽"'), csv[2]);
    // сброс колонки — подписи и числа прежние
    await openMenu(pg); await fmtBtn(pg, 'mv'); await resetFmt(pg, 'mv');
    assert.deepStrictEqual(await colOf(pg, 'mv', '#tbl'), k0.mv);
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

test('390 px: меню с раскрытым форматом целиком на экране, выбор и сброс достижимы пальцем', { skip }, async () => {
  const { browser, pg, errors } = await launch(390);
  try {
    await openC01(pg);
    await openMenu(pg);
    await fmtBtn(pg, 'mv');
    const box = await pg.evaluate(() => { const m = document.querySelector('.k-colmenu').getBoundingClientRect(), p = document.querySelector('.k-colmenu-fmt').getBoundingClientRect(), b = document.querySelector('.k-colmenu-fmtbtn').getBoundingClientRect(); return { m: [m.left, m.right], p: [p.left, p.right], bh: b.height, bw: b.width, vw: innerWidth }; });
    assert.ok(box.m[0] >= 0 && box.m[1] <= box.vw, 'меню в экране: ' + JSON.stringify(box));
    assert.ok(box.p[0] >= box.m[0] && box.p[1] <= box.m[1] + 1, 'панель формата внутри меню: ' + JSON.stringify(box));
    assert.ok(box.bh >= 28, 'кнопка формата не уже пальца: ' + box.bh);
    await selU(pg, 'mv').selectOption('bn');
    assert.ok((await colOf(pg, 'mv')).head.startsWith('MV, bn ₽'));
    await resetFmt(pg, 'mv');
    assert.ok((await colOf(pg, 'mv')).head.startsWith('MV, mm ₽'));
    assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1), 'горизонтальной прокрутки страницы нет');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});

// ---- с сервером: страница открыта через http, ответы сервера подставлены; у каждого пользователя — свой ----
async function serve(ctx, users) {
  const types = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' }, log = [];
  await ctx.route('http://desk.test/**', route => {
    const req = route.request(), u = new URL(req.url()), who = users.current;
    if (u.pathname === '/concepts-run.js') return route.fulfill({ contentType: types['.js'], body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ contentType: types['.js'], body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/me/table-columns') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ user: who, tables: {} }) });
    const m = /^\/api\/v1\/me\/column-formats(?:\/(.+))?$/.exec(u.pathname);
    if (m) {
      const mine = users[who].formats;
      if (req.method() === 'GET') return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ user: who, formats: Object.fromEntries(Object.entries(mine).map(([k, v]) => [k, Object.assign({ updated_at: '2026-11-19T10:00:00Z' }, v)])) }) });
      const col = decodeURIComponent(m[1]);
      log.push({ method: req.method(), col, body: req.postData(), type: req.headers()['content-type'], who });
      if (users.failWrites) return route.fulfill({ status: 503, contentType: 'application/json', body: '{}' });
      if (req.method() === 'PUT') { const b = JSON.parse(req.postData()); mine[col] = b; return route.fulfill({ contentType: 'application/json', body: JSON.stringify(Object.assign({ updated_at: '2026-11-19T10:01:00Z' }, b)) }); }
      delete mine[col];
      return route.fulfill({ status: 204, body: '' });
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

test('с сервером: формат берётся с сервера, правка уходит PUT, сброс — DELETE; у другого пользователя — свой; сервер не принял — формат на экране', { skip }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const users = { ann: { formats: { dv01: { decimals: 2, scale: 'mm' }, 'removed-in-v9': { decimals: 1, scale: null } } }, bob: { formats: {} }, current: 'ann' };
    const open = async () => {  // новый контекст = чистый браузер: localStorage пуст, формат может прийти только с сервера
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
    // ann: формат DV01 пришёл с сервера в чистом браузере, таблица перерисована по ответу
    let s = await open();
    await s.pg.waitForFunction(() => document.querySelector('th[data-k="dv01"]').textContent.includes('mm ₽/бп'), null, { timeout: 10000 });
    const d = await colOf(s.pg, 'dv01');
    assert.ok(/\.\d\d\)?$/.test(d.first), 'DV01 у ann — mm, 2 знака: ' + d.first);
    await openMenu(s.pg);
    await fmtBtn(s.pg, 'dv01');
    assert.strictEqual(await selD(s.pg, 'dv01').inputValue(), '2'); assert.strictEqual(await selU(s.pg, 'dv01').inputValue(), 'mm');
    assert.strictEqual(await s.pg.textContent('.k-colmenu-fmt .k-colmenu-fmtnote:last-child').then(t => /только в этом браузере|не принял/.test(t)), false, 'сервер на связи — предупреждения нет');
    // правка: PUT с JSON, целиком (знаки и единицы)
    await selD(s.pg, 'dv01').selectOption('4');
    await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(s.log.map(x => [x.method, x.col]), [['PUT', 'dv01']]);
    assert.strictEqual(s.log[0].type, 'application/json');
    assert.deepStrictEqual(JSON.parse(s.log[0].body), { decimals: 4, scale: 'mm' });
    // сброс — DELETE
    await resetFmt(s.pg, 'dv01'); await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(s.log.map(x => x.method), ['PUT', 'DELETE']);
    assert.deepStrictEqual(Object.keys(users.ann.formats), ['removed-in-v9'], 'на сервере остался только неизвестный странице ключ — его страница не трогает');
    assert.ok((await colOf(s.pg, 'dv01')).head.includes('тыс ₽/бп'));
    await selU(s.pg, 'dv01').selectOption('bn'); await s.pg.waitForTimeout(300);
    assert.deepStrictEqual(users.ann.formats.dv01, { decimals: null, scale: 'bn' });
    // перезагрузка в чистом браузере — то же
    await s.ctx.close();
    s = await open();
    await s.pg.waitForFunction(() => document.querySelector('th[data-k="dv01"]').textContent.includes('bn ₽/бп'), null, { timeout: 10000 });
    // bob в том же приложении: свой формат — по умолчанию
    await s.ctx.close();
    users.current = 'bob';
    s = await open();
    assert.ok((await colOf(s.pg, 'dv01')).head.includes('тыс ₽/бп'), 'у другого пользователя — по умолчанию');
    // сервер не принимает запись: формат остаётся на экране и в браузере, меню говорит об этом
    users.failWrites = true;
    await openMenu(s.pg); await fmtBtn(s.pg, 'mv'); await selU(s.pg, 'mv').selectOption('bn'); await s.pg.waitForTimeout(400);
    assert.ok((await colOf(s.pg, 'mv')).head.startsWith('MV, bn ₽'), 'на экране остался');
    assert.ok(/сохранён в этом браузере и уйдёт на сервер/.test(await s.pg.textContent('.k-colmenu-fmt')), await s.pg.textContent('.k-colmenu-fmt'));
    assert.deepStrictEqual(JSON.parse(await s.pg.evaluate(k => localStorage.getItem(k), KEY)), { mv: { decimals: null, scale: 'bn' } });
    assert.deepStrictEqual(users.bob.formats, {}, 'на сервере ничего нет');
    assert.deepStrictEqual(s.errors, []);
    await s.ctx.close();
  } finally { await browser.close(); }
});
