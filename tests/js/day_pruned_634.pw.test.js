// TDM#634: экран дня (c14), таблица «Прогоны», столбец «Что изменилось»: у прогона, заменённый расчёт которого удалён
// (supersedes_pruned), стоит текст supersedes_pruned_text от сервера, а не «входы те же (пересчёт)»; прогон с изменениями
// и прогон, ничего не заменявший, показаны как были.
//   node --test tests/js/day_pruned_634.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const TEXT = 'заменённый расчёт удалён (хранится только актуальный)';

test('экран дня: «заменённый расчёт удалён» вместо «входы те же (пересчёт)»', { skip: pw ? false : 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    await pg.route('**/*', r => {
      const u = new URL(r.request().url());
      if (u.origin !== ORIGIN) return r.abort();
      if (u.pathname === '/api/v1/auth/state') return r.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
      if (u.pathname.startsWith('/api/')) return r.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return r.fulfill({ status: 404, body: '' });
      return r.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c14-day-status.html?demo=1');
    await pg.waitForSelector('table[data-vk="c14-runs"] tbody tr');
    const cells = await pg.evaluate(() => Object.fromEntries([...document.querySelectorAll('table[data-vk="c14-runs"] tbody tr')]
      .map(tr => [tr.cells[0].textContent.trim().split(' ')[0], tr.cells[6].textContent.trim()])));
    assert.strictEqual(cells['39'], TEXT, 'удалённый заменённый расчёт назван');
    assert.ok(!/входы те же/.test(cells['39']));
    assert.ok(/срез/.test(cells['42']), 'прогон с изменениями как был: ' + cells['42']);
    assert.strictEqual(cells['38'], '', 'прогон, ничего не заменявший: пусто');
    assert.deepStrictEqual(errors, []);
  } finally { await browser.close(); }
});
