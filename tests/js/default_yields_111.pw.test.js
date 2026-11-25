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

async function open(browser, file, { extras = X, storage = {}, marketHistory } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } }), pg = await ctx.newPage(), errors = [];
  await ctx.addInitScript(s => Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, JSON.stringify(v))), storage);
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(extras) + ';' });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname === '/api/v1/history/market' && marketHistory) return route.fulfill({ json: marketHistory });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + file);
  await pg.waitForFunction(n => window.MOCK && MOCK.universeRub && MOCK.universeRub.length === n, extras.universeRub.length);
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

test('#766: C05, C09 и C13 показывают расчёт ядра и причину пропуска', { skip: pw ? false : 'нет Playwright' }, async () => {
  const coreNote = 'Расчёт ядра по цене MOEX и графику выплат. Применена ручная правка графика погашений.';
  const missingNote = 'Аналитика MOEX недостоверна. Нет чистой цены.';
  const issuer = JSON.parse(RUN).positions.find(p => p.instrType === 'BOND' && p.ccy === 'USD').issuer;
  const rub = { ...bond('NO-PRICE', null, null, null), yieldNote: missingNote };
  const recalculated = { ...bond('CORE', 7.63, 300, 29), ytmMoex: null, yieldSource: 'core', yieldNote: coreNote, yieldManualOverride: true };
  const fx = { ...recalculated, id: 'FX-CORE', ccy: 'USD', issuer };
  const extras = { ...X, universeRub: [rub, recalculated],
    universeFx: [fx, { ...fx, id: 'FX-MISSING', name: 'Синт FX-MISSING', ytm: null, yieldSource: null, yieldNote: missingNote }],
    marketMovers: [rub, recalculated].map(b => ({ ...b, rating: 'без рейтинга', vol: b.value })) };
  const marketHistory = { bonds: { [rub.id]: { y: [null], p: [null] }, [recalculated.id]: { y: [29], p: [null] } } };
  const browser = await pw.chromium.launch();
  try {
    const market = await open(browser, 'c09-market.html', { extras, marketHistory });
    const cell = market.pg.locator('#movers tr').filter({ hasText: rub.name }).locator('.k-hint');
    assert.strictEqual(await cell.getAttribute('title'), missingNote);
    assert.strictEqual(await cell.innerText(), '—');
    const coreCell = market.pg.locator('#movers tr').filter({ hasText: recalculated.name }).locator('.k-hint');
    assert.match(await coreCell.innerText(), /7[.,]63/);
    assert.strictEqual(await coreCell.getAttribute('title'), coreNote);
    assert.deepStrictEqual(market.errors, []);
    await market.ctx.close();
    const morning = await open(browser, 'c05-morning-brief.html', { extras });
    const morningCell = morning.pg.locator('#s6 .k-hint').filter({ hasText: /7[.,]63/ }).first();
    assert.strictEqual(await morningCell.getAttribute('title'), coreNote);
    assert.deepStrictEqual(morning.errors, []);
    await morning.ctx.close();

    const issuers = await open(browser, 'c13-issuers.html', { extras, storage: { c13: { sel: issuer, curve: 'ytm' } } });
    const panel = issuers.pg.locator('.k-panel').filter({ has: issuers.pg.getByRole('heading', { name: 'Кривая эмитента', exact: true }) });
    assert.match(await panel.locator('.k-note').innerText(), /FX-MISSING.*Нет чистой цены/);
    const el = panel.locator('.ch[_echarts_instance_]');
    await el.waitFor();
    const data = await el.evaluate(e => {
      const option = echarts.getInstanceByDom(e).getOption();
      const points = option.series.find(s => s.name === 'USD рынок').data;
      return { values: points.map(d => d.value), tooltip: option.tooltip[0].formatter({ data: points[0], value: points[0].value }) };
    });
    assert.deepStrictEqual(data.values, [[fx.tenor, 7.63]]);
    assert.ok(data.tooltip.includes(coreNote));
    assert.deepStrictEqual(issuers.errors, []);
  } finally { await browser.close(); }
});
