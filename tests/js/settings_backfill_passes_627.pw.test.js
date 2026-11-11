// TDM#627: Настройки → «История прогонов» показывает два прохода пересчёта в браузере (Playwright), статус — заглушка
// GET /api/v1/backfill/status: «Проход 1 из 2: стоимость, риск и PnL — N из M дат», «Проход 2 из 2: вторые производные
// (выпуклость, гамма) — N из M дат, осталось ≈ …», проблемные даты второго прохода, «готово» — только при pass == done.
//   node --test tests/js/settings_backfill_passes_627.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

const BASE = { enabled: true, from: '2026-01-05', to: '2026-10-23', dates: 210, done: 190, todo: 9, no_positions: 4, no_market: 0, failed: 0, total: 190 + 9,
  hub: true, next: ['2026-10-09', '2026-10-12'], first_positions: '2026-01-05', oldest_run: '2026-01-05', inbox_scanned_from: '2026-01-05', failed_dates: [], eta_s: 540,
  problem_dates: [], hub_problem_dates: [], stalled: null, pass: 'first',
  second: { done: 190, todo: 0, failed: 0, total: 190, next: [], problem_dates: [], eta_s: null } };
const SECOND = { ...BASE, todo: 0, done: 199, pass: 'second', eta_s: null,
  second: { done: 100, todo: 99, failed: 1, total: 200, next: ['2026-10-21', '2026-10-20'], eta_s: 1500, problem_dates: [{ date: '2026-10-02', error: 'RuntimeError: no run created', attempts: 3 }] } };
const DONE = { ...BASE, todo: 0, done: 199, pass: 'done', eta_s: null, second: { done: 199, todo: 0, failed: 0, total: 199, next: [], problem_dates: [], eta_s: null } };

async function open(browser, status) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname === '/api/v1/backfill/status') return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(status) });
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#bf');
  await pg.waitForSelector('#bf-rows tr');
  return { pg, ctx, errors };
}

const row = (pg, k) => pg.locator('#bf-rows tr', { has: pg.locator('td.k', { hasText: new RegExp('^' + k + '$') }) }).locator('td.v').innerText();

test('настройки: «История прогонов» показывает два прохода пересчёта (TDM#627)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('идёт первый проход: счётчик первого, второй ждёт, «готово» нет', async () => {
      const { pg, ctx, errors } = await open(browser, BASE);
      assert.strictEqual(await row(pg, 'Ход пересчёта'), 'идёт проход 1 из 2');
      assert.strictEqual(await row(pg, 'Проход 1 из 2'), 'стоимость, риск и PnL — 190 из 199 дат');
      assert.strictEqual(await row(pg, 'Проход 2 из 2'), 'вторые производные (выпуклость, гамма) — начнётся, когда первый проход посчитает все даты');
      assert.ok(/^190 из \d+ дат с позициями$/.test(await row(pg, 'Посчитано в проходе 1')));
      assert.strictEqual(await pg.locator('#bf-rows td.k', { hasText: 'Не вышло во втором проходе' }).count(), 0);
      assert.strictEqual(await pg.locator('#bf-sum').innerText(), 'проход 1 из 2: 190 из 199 дат');
      assert.ok(!/null|undefined|NaN/.test(await pg.locator('#bf-rows').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('идёт второй проход: вторые производные N из M, осталось ≈, ближайшие даты, проблемная дата с ошибкой', async () => {
      const { pg, ctx, errors } = await open(browser, SECOND);
      assert.strictEqual(await row(pg, 'Ход пересчёта'), 'идёт проход 2 из 2');
      assert.strictEqual(await row(pg, 'Проход 1 из 2'), 'стоимость, риск и PnL — 199 из 199 дат, готово');
      const p2 = await row(pg, 'Проход 2 из 2');
      assert.ok(p2.startsWith('вторые производные (выпуклость, гамма) — 100 из 200 дат, осталось ≈ 25 мин'), p2);
      assert.ok(p2.includes('ближайшие 21.10.2026, 20.10.2026') && p2.includes('оценка сценариев') && p2.includes('по первому порядку') && !/гамма.*гамма/.test(p2), p2);
      const bad = await row(pg, 'Не вышло во втором проходе');
      assert.ok(/^1:\n02\.10\.2026 — вторые производные: RuntimeError: no run created, попыток: 3$/.test(bad), bad);
      assert.strictEqual(await pg.locator('#bf-sum').innerText(), 'проход 2 из 2 (вторые производные): 100 из 200 дат');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('«готово» — только когда pass == done', async () => {
      const { pg, ctx, errors } = await open(browser, DONE);
      assert.strictEqual(await row(pg, 'Ход пересчёта'), 'готово: оба прохода');
      assert.strictEqual(await row(pg, 'Проход 2 из 2'), 'вторые производные (выпуклость, гамма) — 199 из 199 дат, готово');
      assert.ok((await pg.locator('#bf-sum').innerText()).startsWith('посчитано 199 из ') && (await pg.locator('#bf-sum').innerText()).endsWith('вторые производные тоже'));
      // готов первый проход, второй с неудачей: не «готово»
      const { pg: pg2, ctx: ctx2 } = await open(browser, { ...DONE, second: { ...SECOND.second, todo: 0, done: 198, failed: 1 } });
      assert.strictEqual(await row(pg2, 'Ход пересчёта'), 'готово, но вторые производные не посчитаны у 1 даты');
      assert.deepStrictEqual(errors, []);
      await ctx.close(); await ctx2.close();
    });
    await t.test('сервер без pass (прежний): строк прохода нет, прежние тексты', async () => {
      const old = { ...BASE }; delete old.pass; delete old.second;
      const { pg, ctx, errors } = await open(browser, old);
      assert.strictEqual(await pg.locator('#bf-rows td.k', { hasText: /^(Ход пересчёта|Проход)/ }).count(), 0);
      assert.ok(/^посчитано 190 из/.test(await pg.locator('#bf-sum').innerText()));
      assert.ok(await row(pg, 'Посчитано'));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    // TDM#660: дата, у которой перенесённый спред устарел, стоит в очереди с этой причиной; прогоны до проверки названы «неизвестно»
    await t.test('очередь: «перенесённый спред устарел — N»; даты с неизвестным спредом названы, пересчёта они не ждут (TDM#660)', async () => {
      const st = { ...BASE, carry_unknown: 7, todo_by_reason: [{ key: 'carry', text: 'перенесённый спред устарел', n: 3 }, { key: 'market', text: 'хаб поправил срез', n: 1 }] };
      const { pg, ctx, errors } = await open(browser, st);
      const q = await row(pg, 'Осталось в проходе 1');
      assert.ok(q.includes('перенесённый спред устарел — 3, хаб поправил срез — 1'), q);
      const u = await row(pg, 'Перенесённый спред');
      assert.ok(u.startsWith('у 7 дат неизвестно, не устарел ли он') && u.includes('сделаны до этой проверки'), u);
      const { pg: pg2, ctx: ctx2 } = await open(browser, { ...BASE, carry_unknown: 0 });
      assert.strictEqual(await pg2.locator('#bf-rows td.k', { hasText: 'Перенесённый спред' }).count(), 0);
      assert.deepStrictEqual(errors, []);
      await ctx.close(); await ctx2.close();
    });
    // TDM#628: сколько процессов считает историю, и выбрано ли число автоматически (прежний сервер без поля — строки нет)
    await t.test('«Процессов пересчёта»: число и пометка «выбрано автоматически»', async () => {
      const { pg, ctx, errors } = await open(browser, { ...BASE, workers: 3, workers_auto: true });
      assert.strictEqual(await row(pg, 'Процессов пересчёта'), '3 (выбрано автоматически)');
      const { pg: pg2, ctx: ctx2 } = await open(browser, { ...BASE, workers: 2, workers_auto: false });
      assert.strictEqual(await row(pg2, 'Процессов пересчёта'), '2');
      const { pg: pg3, ctx: ctx3 } = await open(browser, { ...BASE, workers: 1, workers_auto: true });
      assert.strictEqual(await row(pg3, 'Процессов пересчёта'), '1 (даты по одной)');
      const { pg: pg4, ctx: ctx4 } = await open(browser, BASE);
      assert.strictEqual(await pg4.locator('#bf-rows td.k', { hasText: 'Процессов пересчёта' }).count(), 0);
      assert.deepStrictEqual(errors, []);
      await ctx.close(); await ctx2.close(); await ctx3.close(); await ctx4.close();
    });
  } finally {
    await browser.close();
  }
});
