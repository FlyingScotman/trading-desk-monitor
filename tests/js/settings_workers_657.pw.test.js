// TDM#657: Настройки → «История прогонов» → «Дополнительно»: поле «Процессов пересчёта» (Playwright), сервер — заглушка маршрутов.
// Поле показывает сохранённое число (пусто — по умолчанию), подсказка по-русски объясняет 0 и 1; «Сохранить» отправляет число
// (пустое поле — null, как у остальных полей), дробное, отрицательное и больше 8 не уходят на сервер, а ответ сервера с отказом
// показан у поля; строка «Процессов пересчёта» в ходе пересчёта говорит число, с которым пул стартовал.
//   node --test tests/js/settings_workers_657.pw.test.js
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
  st.doc = () => ({ home: 'C:\\desk', inbox_dirs: [], ...st.stored, effective: { backfill_workers: st.stored.backfill_workers || 0 }, env_overrides: [], error: null });
  return st;
}

async function open(browser, st, refuse) {
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
      if (refuse) return json(route, 422, { code: 'invalid_parameter', message: 'backfill_workers: must be a whole number from 0 to 8 (0: the program chooses)' });
      for (const [k, v] of Object.entries(body)) { if (v === null) delete st.stored[k]; else st.stored[k] = v; }
      return json(route, 200, st.doc());
    }
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) return json(route, 404, { message: 'нет в тесте' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#autopilot');
  await pg.waitForSelector('#backfill_workers', { state: 'attached' });
  await pg.waitForFunction(() => document.querySelector('[data-key="backfill_workers"]') && document.getElementById('save-msg') !== null);
  return { pg, ctx, errors };
}

const field = pg => pg.locator('#backfill_workers');
async function show(pg) { await pg.evaluate(() => { const d = document.getElementById('adv-bf'); if (d) d.open = true; }); }

test('настройки: поле «Процессов пересчёта» (TDM#657)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('подпись и подсказка понятны трейдеру: что значит 0, что 1, что поставить, если ПК тормозит', async () => {
      const st = server({});
      const { pg, ctx, errors } = await open(browser, st);
      await show(pg);
      assert.ok((await pg.locator('label[for="backfill_workers"]').innerText()).trim().startsWith('Процессов пересчёта'));
      const hint = await pg.locator('#backfill_workers').locator('xpath=ancestor::div[1]').locator('.hint').innerText();
      assert.ok(/0 или пусто — число выбирает программа/.test(hint), hint);
      assert.ok(/1 — по одной дате/.test(hint) && /тормозит/.test(hint), hint);
      assert.ok(/от 0 до 8/.test(hint) && /перезапускать монитор не нужно/.test(hint), hint);
      assert.strictEqual(await field(pg).inputValue(), '', 'ничего не записано — поле пустое, действует выбор программы');
      assert.ok(/выбирает программа/.test(await pg.locator('#backfill_workers').locator('xpath=ancestor::div[1]').locator('.unit').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('сохранённое число показано; «Сохранить» отправляет число, пустое поле — null', async () => {
      const st = server({ backfill_workers: 2 });
      const { pg, ctx, errors } = await open(browser, st);
      await show(pg);
      assert.strictEqual(await field(pg).inputValue(), '2');
      await field(pg).fill('1');
      await pg.locator('#save').click();
      await pg.waitForFunction(() => document.getElementById('save').disabled === false);
      await pg.waitForTimeout(100);
      assert.strictEqual(st.puts.length, 1);
      assert.strictEqual(st.puts[0].backfill_workers, 1);
      assert.strictEqual(typeof st.puts[0].backfill_workers, 'number');
      await field(pg).fill('');
      await pg.locator('#save').click();
      await pg.waitForFunction(() => document.getElementById('save').disabled === false);
      await pg.waitForTimeout(100);
      assert.strictEqual(st.puts.length, 2);
      assert.strictEqual(st.puts[1].backfill_workers, null, 'пусто — настройка снята, программа выбирает сама');
      await field(pg).fill('0');
      await pg.locator('#save').click();
      await pg.waitForTimeout(150);
      assert.strictEqual(st.puts[2].backfill_workers, 0);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('дробное, отрицательное, больше 8 и слова не уходят на сервер: ошибка названа у поля', async () => {
      const st = server({});
      const { pg, ctx, errors } = await open(browser, st);
      await show(pg);
      for (const [v, text] of [['1.5', 'нужно целое число (0 — программа выберет сама)'], ['-1', 'нужно целое число (0 — программа выберет сама)'],
        ['9', 'не больше 8'], ['много', 'нужно положительное число']]) {
        await field(pg).fill(v);
        await pg.locator('#save').click();
        await pg.waitForTimeout(100);
        const msg = await pg.locator('#save-msg').innerText();
        assert.ok(msg.includes('Процессов пересчёта') && msg.includes(text), v + ' → ' + msg);
      }
      assert.deepStrictEqual(st.puts, [], 'ни одна проверка не дошла до сервера');
      await field(pg).fill('8');
      await pg.locator('#save').click();
      await pg.waitForTimeout(150);
      assert.strictEqual(st.puts.length, 1);
      assert.strictEqual(st.puts[0].backfill_workers, 8);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('отказ сервера показан, число в поле остаётся', async () => {
      const st = server({});
      const { pg, ctx, errors } = await open(browser, st, true);
      await show(pg);
      await field(pg).fill('5');
      await pg.locator('#save').click();
      await pg.waitForTimeout(200);
      assert.strictEqual(st.puts.length, 1);
      assert.ok(/backfill_workers|Процессов пересчёта/.test(await pg.locator('#save-msg').innerText()), await pg.locator('#save-msg').innerText());
      assert.strictEqual(await field(pg).inputValue(), '5');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('в «Что действует сейчас» ноль называется «выбирает программа»', async () => {
      const st = server({});
      const { pg, ctx } = await open(browser, st);
      const row = pg.locator('#eff > div', { has: pg.locator('.l', { hasText: 'процессов пересчёта' }) }).locator('.v');
      assert.strictEqual((await row.innerText()).trim(), 'выбирает программа');
      await ctx.close();
    });
  } finally { await browser.close(); }
});
