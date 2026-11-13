// TDM#732: Настройки → «Автообновление» → карточка «Цикл» → «Дополнительно» → поле «Пауза между пересчётами дня из-за новых
// сделок» (Playwright), сервер — заглушка маршрутов. Подпись и подсказка по-русски объясняют пусто и 0; «Сохранить» отправляет
// число (пустое поле — null), дробное, отрицательное и слова не уходят на сервер, ошибка названа у поля.
//   node --test tests/js/settings_rerun_732.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const STATUS = { enabled: true, from: '2026-01-05', to: '2026-10-23', dates: 210, done: 190, todo: 9, no_positions: 4, no_market: 0, failed: 0, total: 199,
  hub: true, next: [], problem_dates: [], hub_problem_dates: [], stalled: null, pass: 'first', workers: 3, workers_auto: true,
  second: { done: 190, todo: 0, failed: 0, total: 190, next: [], problem_dates: [], eta_s: null } };

function server(stored) {
  const st = { puts: [], stored: { ...stored } };
  st.doc = () => ({ home: 'C:\\desk', inbox_dirs: [], ...st.stored, effective: { intraday_rerun_min_s: st.stored.intraday_rerun_min_s == null ? 300 : st.stored.intraday_rerun_min_s }, env_overrides: [], error: null });
  return st;
}

async function open(browser, st) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  const json = (route, status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
  await pg.route('**/*', route => {
    const u = new URL(route.request().url()), m = route.request().method();
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/api/v1/auth/state') return json(route, 200, { enabled: false, user: null });
    if (u.pathname === '/api/v1/backfill/status') return json(route, 200, STATUS);
    if (u.pathname === '/api/v1/settings' && m === 'GET') return json(route, 200, st.doc());
    if (u.pathname === '/api/v1/settings' && m === 'PUT') {
      const body = JSON.parse(route.request().postData() || '{}');
      st.puts.push(body);
      for (const [k, v] of Object.entries(body)) { if (v === null) delete st.stored[k]; else st.stored[k] = v; }
      return json(route, 200, st.doc());
    }
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) return json(route, 404, { message: 'нет в тесте' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#autopilot');
  await pg.waitForSelector('#intraday_rerun_min_s', { state: 'attached' });
  await pg.waitForFunction(() => document.querySelector('[data-key="intraday_rerun_min_s"]') && document.getElementById('save-msg') !== null);
  await pg.evaluate(() => { const d = document.getElementById('adv-cycle'); if (d) d.open = true; });
  return { pg, ctx, errors };
}

const field = pg => pg.locator('#intraday_rerun_min_s');
const save = async pg => { await pg.locator('#save').click(); await pg.waitForTimeout(150); };

test('настройки: пауза между пересчётами дня из-за новых сделок (TDM#732)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('подпись и подсказка понятны: что значит пусто и 0, что не ждёт паузы', async () => {
      const { pg, ctx, errors } = await open(browser, server({}));
      assert.ok((await pg.locator('label[for="intraday_rerun_min_s"]').innerText()).trim().startsWith('Пауза между пересчётами дня из-за новых сделок'));
      const box = pg.locator('#intraday_rerun_min_s').locator('xpath=ancestor::div[1]');
      const hint = await box.locator('.hint').first().innerText();
      assert.ok(/Пусто — 300, 0 — без паузы/.test(hint), hint);
      assert.ok(/0 — без паузы/.test(await box.locator('.unit').innerText()));
      assert.ok(/Новый срез хаба, другие позиции и поздний отчёт пересчитываются сразу/.test(await box.locator('.more p').textContent()));
      assert.strictEqual(await field(pg).inputValue(), '');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('сохранённое число показано; «Сохранить» отправляет число, 0 и пусто (null)', async () => {
      const st = server({ intraday_rerun_min_s: 120 });
      const { pg, ctx, errors } = await open(browser, st);
      assert.strictEqual(await field(pg).inputValue(), '120');
      await field(pg).fill('60'); await save(pg);
      assert.strictEqual(st.puts[0].intraday_rerun_min_s, 60);
      assert.strictEqual(typeof st.puts[0].intraday_rerun_min_s, 'number');
      await field(pg).fill('0'); await save(pg);
      assert.strictEqual(st.puts[1].intraday_rerun_min_s, 0);
      await field(pg).fill(''); await save(pg);
      assert.strictEqual(st.puts[2].intraday_rerun_min_s, null);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('дробное, отрицательное и слова не уходят на сервер: ошибка названа у поля', async () => {
      const st = server({});
      const { pg, ctx, errors } = await open(browser, st);
      for (const [v, text] of [['1.5', 'нужно целое число (0 — без паузы)'], ['-1', 'нужно целое число (0 — без паузы)'], ['скоро', 'нужно положительное число']]) {
        await field(pg).fill(v); await save(pg);
        const msg = await pg.locator('#save-msg').innerText();
        assert.ok(msg.includes('Пауза между пересчётами дня из-за новых сделок') && msg.includes(text), v + ' → ' + msg);
      }
      assert.strictEqual(st.puts.length, 0);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally { await browser.close(); }
});
