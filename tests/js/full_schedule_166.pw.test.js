// #166: карточка облигации в браузере. /api/v1/positions/flows сначала падает — карточка пишет «Расписание до <дата>,
// полное недоступно: <причина>» над потоками прогона; сбой не запоминается: повторное открытие карточки спрашивает
// снова и показывает весь график до погашения. Синтетические данные только.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

test('карточка облигации: резервный список с причиной, затем весь график', { skip: pw ? false : 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    const D = JSON.parse(RUN);
    const id = 'SYNTHETIC_BOND_166';  // id позиции прогона — строка (run_position.position_id)
    D.positions.push({ ...D.positions.find(p => p.instrType === 'BOND' && p.ccy === 'RUB'), id, name: 'Synthetic bond' });
    const own = [{ date: '2027-02-15', pos: id, kind: 'cpn', ccy: 'RUB', amount: 1e6, est: false },
      { date: '2027-08-15', pos: id, kind: 'cpn', ccy: 'RUB', amount: 1e6, est: false }];
    const full = [];
    for (let y = 2027; y <= 2031; y++) ['02-15', '08-15'].forEach(md => full.push([y + '-' + md, 'cpn', 1e6, false]));
    full.push(['2031-08-15', 'mat', 1e8, false]);
    let asked = 0, failing = true;  // пока failing — сервер падает (карточка может перерисоваться и спросить ещё раз)
    await pg.route('**/*', route => {
      const u = new URL(route.request().url());
      if (u.origin !== ORIGIN) return route.abort();
      if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
      if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify({ notes: [], cashflows: own }) + ';' });
      if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
      if (u.pathname === '/api/v1/positions/flows') {
        asked++;
        return failing ? route.fulfill({ status: 500, contentType: 'application/json', body: '{}' })
          : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ source: 'run', reason: 'no_static', ccy: 'RUB', maturity: '2031-08-15', flows: full }) });
      }
      if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c03-blotter.html');
    await pg.waitForFunction(i => window.MOCK && MOCK.pos[i] && MOCK.posFlows, id);
    await pg.evaluate(i => KIT.card(i), id);
    const cfs = () => pg.locator('.k-drawer').last().locator('.cfs');
    await cfs().getByText('полное недоступно').waitFor();
    const first = await cfs().innerText();
    assert.ok(first.includes('Расписание до 15.08.27, полное недоступно: ошибка сервера 500'), first);
    assert.ok(first.includes('2 потоков'), first);
    const before = asked;
    failing = false;
    await pg.evaluate(i => KIT.card(i), id);  // сбой не запомнен: карточка спрашивает снова
    await cfs().getByText('11 потоков').waitFor();
    const second = await cfs().innerText();
    assert.ok(second.includes('11 потоков до 15.08.31') && second.includes('график по условиям бумаги в прогоне') && !second.includes('недоступно'), second);
    assert.ok(before >= 1 && asked > before, before + ' / ' + asked);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally {
    await browser.close();
  }
});
