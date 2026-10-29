// #111 в браузере: дефолтный выпуск с доходностью MOEX «29 574 %» на C09 (медианы G-спреда, «Движения дня») и C05
// (рост и снижение доходностей) — пометка вместо числа, вне медиан и шкал. Синтетические выпуски, прогон —
// fixtures/concepts_run.
//   node --test tests/js/default_yields_111.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };

// три обычных корпоративных фикс-выпуска срока 1–2 г и один дефолтный: медиана без него — 300 б.п., с ним — 350
const bond = (id, ytm, spread, chgBp) => ({ id, isin: null, name: 'Синт ' + id, tenor: 1.5, dur: 1.4, ytm, ytmMoex: ytm, spread, chgBp,
  value: 5e9, flt: false, isOfz: false, rating: null, sector: 'Синтетика', size: 1e10, issuer: 'S' + id, issuerName: 'Синт ' + id });
const U = [bond('A', 16, 200, 5), bond('B', 17, 300, -8), bond('C', 18, 400, 12), bond('DEF', 29574, 2955000, 2900000)];
const X = { notes: [], marketDate: '2026-10-20', universeRub: U, universeFx: [],
  marketMovers: U.map(b => ({ name: b.name, rating: 'без рейтинга', sector: b.sector, ytm: b.ytmMoex, chgBp: b.chgBp, vol: b.value })) };

async function open(browser, file) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(X) + ';' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + file);
  await pg.waitForFunction(() => window.MOCK && MOCK.universeRub && MOCK.universeRub.length === 4);
  await pg.waitForTimeout(300);
  return { pg, ctx, errors };
}
const digits = s => s.replace(/[\s   ]/g, '');

test('#111: дефолтные выпуски на C09 и C05', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('C09: медиана G-спреда без дефолтного выпуска, «Движения дня» — пометка, шкала без него', async () => {
      const { pg, ctx, errors } = await open(browser, 'c09-market.html');
      try {
        const row = await pg.locator('#sprtab tbody tr').filter({ hasText: 'без рейтинга' }).first().locator('td').allInnerTexts();
        assert.strictEqual(digits(row[2]), '300', 'медиана 1–2г без дефолтного выпуска: ' + row.join(' | '));
        const def = pg.locator('#movers tr').filter({ hasText: 'Синт DEF' });
        await def.waitFor();
        const text = await def.innerText();
        assert.ok(text.includes('дистресс'), text);
        assert.ok(!digits(text).includes('29574') && !digits(text).includes('2900000'), 'ни доходности, ни изменения числом: ' + text);
        // шкала теплоты и полосы — по обычным выпускам: у +12 б.п. полоса во всю длину (иначе ~0 при шкале 2 900 000)
        const c = await pg.locator('#movers tr').filter({ hasText: 'Синт C' }).innerText();
        assert.ok(digits(c).includes('+12') || digits(c).includes('12'), c);
        const heat = await pg.locator('#movers tr').filter({ hasText: 'Синт C' }).locator('td').nth(4).getAttribute('style');
        assert.ok(heat && /rgba?\(/.test(heat), 'у обычного выпуска есть подсветка шкалы: ' + heat);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('C05: рост и снижение доходностей — без дефолтного выпуска, сказано сколько исключено', async () => {
      const { pg, ctx, errors } = await open(browser, 'c05-morning-brief.html');
      try {
        const s6 = await pg.locator('#s6').innerText();
        assert.ok(!s6.includes('Синт DEF'), 'дефолтный выпуск не в списках');
        assert.ok(!digits(s6).includes('29574'));
        assert.ok(s6.includes('Синт C') && s6.includes('Синт B'));
        assert.ok(s6.includes('без 1 дефолтн./дистрессн.'), 'сказано, что исключено');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
