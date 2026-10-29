// Настройки → «История прогонов» (#bf) в браузере (Playwright), сервер — заглушка маршрутов (TDM#447): остановка
// догрузки на ошибке сети или базы — строкой «Догрузка стоит» с причиной и следующей попыткой; даты, которые не
// сходятся по своим данным, и даты, которые не загружаются (своя ошибка сети, даты хаба), — разными строками.
//   node --test tests/js/settings_backfill_447.pw.test.js
// Нужен Playwright (как settings_adapter.pw.test.js); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

const STATUS = { enabled: true, from: '2026-01-05', to: '2026-10-23', dates: 210, done: 48, todo: 150, no_positions: 4, no_market: 0, failed: 1,
  total: 199, hub: true, next: ['2026-03-02', '2026-03-03'], first_positions: '2026-01-05', oldest_run: '2026-01-05', inbox_scanned_from: '2026-01-05',
  failed_dates: ['2026-02-10'], eta_s: 9000,
  problem_dates: [{ date: '2026-02-10', kind: 'unconverged', why: 'program updated', error: 'does not converge: program updated after 2 runs', attempts: 2 },
    { date: '2026-02-12', kind: 'unreachable', why: 'no default run', error: 'HubError: bundle: 502 bad gateway', attempts: 3 }],
  hub_problem_dates: [{ date: '2026-02-20', error: 'SnapshotError: digest mismatch', attempts: 2, next_attempt: '2026-10-23T09:30:00+00:00' }],
  stalled: { error: 'TimeoutError: timed out', date: '2026-03-02', source: 'network', since: '2026-10-23T06:00:00+00:00', at: '2026-10-23T09:00:00+00:00',
    next_attempt: '2026-10-23T09:01:00+00:00' } };

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

test('настройки: «История прогонов» — догрузка стоит, не сходятся и не загружаются отдельно (TDM#447)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('остановка на сети: причина, с какого времени, следующая попытка; даты по видам', async () => {
      const { pg, ctx, errors } = await open(browser, STATUS);
      const stall = await row(pg, 'Догрузка стоит');
      assert.ok(/TimeoutError: timed out/.test(stall) && /сети или хаба, а не данных дат/.test(stall) && /02\.03\.2026/.test(stall), stall);
      assert.ok(/с 23\.10.*09:00 МСК/.test(stall) && /Следующая попытка ≈ 23\.10.*12:01 МСК/.test(stall), stall);
      const conv = await row(pg, 'Не сходятся');
      assert.ok(/^1 /.test(conv) && /10\.02\.2026 — не сходится после пересчёта, попыток: 2/.test(conv) && !/12\.02|20\.02|Timeout/.test(conv), conv);
      const nl = await row(pg, 'Не загружаются');
      assert.ok(/^2:/.test(nl) && /12\.02\.2026 — HubError: bundle: 502 bad gateway, попыток: 3/.test(nl) && /20\.02\.2026 — с хаба: SnapshotError/.test(nl), nl);
      const all = await pg.locator('#bf-rows').innerText();
      assert.ok(!/\bnull\b|undefined|NaN/.test(all), all);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('догрузка идёт: строки «Догрузка стоит» нет, пустые виды — «—»', async () => {
      const { pg, ctx, errors } = await open(browser, { ...STATUS, stalled: null, failed: 0, failed_dates: [], problem_dates: [], hub_problem_dates: [] });
      assert.strictEqual(await pg.locator('#bf-rows td.k', { hasText: 'Догрузка стоит' }).count(), 0);
      assert.strictEqual(await row(pg, 'Не сходятся'), '—');
      assert.strictEqual(await row(pg, 'Не загружаются'), '—');
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally {
    await browser.close();
  }
});
