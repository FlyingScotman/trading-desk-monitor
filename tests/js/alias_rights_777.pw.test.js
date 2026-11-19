// TDM#777: кто может переименовать бумагу в карточке монитора. Страница на ЧУЖОМ адресе (desk.test, не 127.0.0.1):
// вход включён — трейдер и администратор правят (✎ доступна), роль «просмотр» — только чтение с причиной в подсказке;
// вход выключен — только компьютер сервера, с чужого адреса — только чтение с этой причиной. Режим входа и роль страница
// берёт из GET /api/v1/auth/state, который уже спрашивает desk-auth.js (второго запроса нет). Ответ 403 на запись тоже
// переводит в только чтение. Страница как файл, API — заглушки, данные синтетические.
//   node --test tests/js/alias_rights_777.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const json = (route, body, status) => route.fulfill({ status: status || 200, contentType: 'application/json', body: JSON.stringify(body) });

// state — ответ /api/v1/auth/state; put — ответ на PUT /api/v1/instruments/alias
async function open(browser, state, put) {
  const D = JSON.parse(RUN);
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], calls = { state: 0, put: 0 };
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify({ notes: [] }) + ';' });
    if (u.pathname === '/api/v1/auth/state') { calls.state++; return json(route, state); }
    if (u.pathname === '/api/v1/instruments/alias') { calls.put++; return json(route, put || {}, put ? 200 : 403); }
    if (u.pathname.startsWith('/api/')) return json(route, u.pathname.endsWith('/aliases') ? { aliases: [] } : {}, u.pathname.endsWith('/aliases') ? 200 : 404);
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
  await pg.waitForFunction(() => window.MOCK && window.KIT && window.MOCK.nameRights && window.MOCK.nameRights.authKnown);  // ответ входа применён
  return { pg, ctx, errors, calls };
}
const me = role => ({ enabled: true, user: { username: 'u', role, display_name: null, must_change_password: false }, setup_needed: false, setup_allowed: false, loopback: false,
  secure: false, plain_http_lan: false, admin_from_network: false, roles: { viewer: 'просмотр', trader: 'трейдер', admin: 'администратор' } });
const OFF = { enabled: false, user: { username: 'local', role: 'admin' }, setup_needed: true, setup_allowed: false, loopback: false, secure: false, plain_http_lan: false, admin_from_network: false, roles: {} };
// карточка бумаги с ISIN: кнопка ✎ (disabled и подсказка)
const edit = pg => pg.evaluate(() => {
  const p = MOCK.positions.find(x => x.isin);
  KIT.card(p.id);
  const b = document.querySelector('.k-nm [data-al=edit]');
  return { disabled: b.disabled, title: b.title, isin: p.isin };
});

test('who may rename a security (TDM#777)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    for (const role of ['trader', 'admin']) {
      await t.test('sign-in on, ' + role + ' from another computer: the pencil works', async () => {
        const { pg, ctx, errors, calls } = await open(browser, me(role));
        try {
          const b = await edit(pg);
          assert.strictEqual(b.disabled, false, b.title);
          assert.strictEqual(calls.state, 1, 'режим входа уже спрашивает desk-auth.js: второго запроса нет');
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      });
    }
    await t.test('sign-in on, role «просмотр»: read-only, the reason says the role', async () => {
      const { pg, ctx, errors } = await open(browser, me('viewer'));
      try {
        const b = await edit(pg);
        assert.strictEqual(b.disabled, true);
        assert.ok(/^только чтение: роль «просмотр»/.test(b.title) && b.title.includes('«трейдер»') && !/127\.0\.0\.1/.test(b.title), b.title);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('sign-in off, another computer: read-only, the reason says the server computer', async () => {
      const { pg, ctx, errors } = await open(browser, OFF);
      try {
        const b = await edit(pg);
        assert.strictEqual(b.disabled, true);
        assert.ok(/^только чтение: вход выключен — переименовать можно только на компьютере, где работает сервер/.test(b.title) && /127\.0\.0\.1/.test(b.title), b.title);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('sign-in on, trader: the server still says 403 — read-only with the server\'s refusal', async () => {
      const { pg, ctx, errors, calls } = await open(browser, me('trader'));  // PUT отвечает 403
      try {
        await edit(pg);
        await pg.evaluate(() => {  // карточка открыта функцией (KIT.card): правка — по DOM, панель может быть за краем экрана
          document.querySelector('.k-nm [data-al=edit]').click();
          document.querySelector('.k-alias-ed input').value = 'Новое имя';
          document.querySelector('.k-alias-ed [data-al=save]').click();
        });
        await pg.waitForFunction(() => /только чтение/.test((document.querySelector('.k-alias-msg') || {}).textContent || ''));
        const msg = await pg.evaluate(() => document.querySelector('.k-alias-msg').textContent);
        assert.ok(/сервер отказал/.test(msg), msg);
        assert.strictEqual(calls.put, 1);
        assert.strictEqual(await pg.evaluate(() => MOCK.aliasApi.editable), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
