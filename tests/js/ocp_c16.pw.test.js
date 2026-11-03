// C16 «ОВП» in the browser on a run (Playwright; skipped without it), TDM#446: the run from
// tests/js/fixtures/concepts_run.js, /api/v1/history/ocp answered by a synthetic history of the published OCP (books of the
// fixture, an entity-level report part, a day before B2, GBP without a run rate at the OCP report's own rate),
// /api/v1/inbox/datasets/ocp for «отчёта ОВП нет». Both themes,
// 1366 and 1920 px, 390 px without a horizontal scroll, no NaN / undefined, no console errors; the switches and periods.
// TDM#533: the screen's own book selection (the header filter does not apply and is named), TDM#534: the «Общая» line
// without a currency that has no rate, the reason of a day without the published OCP in the tooltip.
//   node --test tests/js/ocp_c16.pw.test.js        C16_SHOTS=<dir> also saves screenshots
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const SHOTS = process.env.C16_SHOTS;
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;
const AS_OF = JSON.parse(RUN).asOf;

// синтетическая история: рабочие дни трёх месяцев до даты прогона; первый — прогон до B2
function history(state, opt = {}) {
  const days = [];
  for (let d = new Date(AS_OF + 'T00:00:00Z'), n = 0; n < 66; d.setUTCDate(d.getUTCDate() - 1)) {
    if (!(d.getUTCDay() % 6)) continue;
    const i = n++, k = 1 + 0.05 * Math.sin(i / 3), date = d.toISOString().slice(0, 10);
    // отчёта на дату нет: вся ОВП дня — из позиций, по книгам (части отчёта по юрлицу нет)
    const nr = i === 0 && state === 'no_report';
    const L = (entity, book, books, ccy, amount, source) => nr ? { entity, book: book || books[0], books: book ? books : books.slice(0, 1), ccy, amount: amount * k, source: 'derived', reported: null, validDate: null }
      : { entity, book, books, ccy, amount: amount * k, source: source || 'report', reported: null, validDate: null };
    const failed = opt.failedAt === i;  // TDM#534: день с ошибкой расчёта ОВП внутри периода
    if (opt.bpd && i < 65) {  // TDM#531: «база отчёта + день»; FXB — база старше прошлого рабочего дня, NEWB — нет в отчёте
      const prev = s => { const t = new Date(s + 'T00:00:00Z'); do t.setUTCDate(t.getUTCDate() - 1); while (!(t.getUTCDay() % 6)); return t.toISOString().slice(0, 10); };
      opt.lines = ln => ln.map(x => Object.assign({}, x, { baseDate: x.book === 'FXB' ? prev(prev(date)) : prev(date), baseKind: 'without_derivatives', parts: { base: x.amount } }))
        .concat([{ entity: 'Головной банк', book: 'NEWB', books: ['NEWB'], ccy: 'USD', amount: 1e6, source: 'report', reported: 0, validDate: prev(date), baseDate: prev(date), baseKind: 'book_absent', parts: { base: 0, derivatives: 1e6 } }]);
    }
    days.unshift({ date, runId: 100 - i, runType: 'EOD', state: i === 0 && state ? state : failed ? 'failed' : i === 65 ? 'not_computed' : 'computed', basis: i === 65 || failed ? null : opt.bpd ? 'report_base_plus_day' : 'report_plus_outside',
      fx: { USD: { market: 81.45, cbr: 81.12 }, EUR: { market: 94.62, cbr: 94.38 }, CNY: { market: 11.38, cbr: 10.95 } },
      reportFx: nr ? {} : { USD: 81.12, EUR: 94.38, CNY: 10.95, GBP: 107.3 },
      lines: i === 65 ? [] : [L('Головной банк', 'CORP_FX', ['CORP_FX'], 'USD', 14e6), L('Головной банк', 'FXB', ['FXB'], 'USD', -9e6),
        L('Головной банк', 'FXB', ['FXB'], 'CNY', -120e6, 'derived'), L('Головной банк', null, ['SOV_FX', 'RATES'], 'EUR', 3.5e6),
        L('Головной банк', 'SOV_FX', ['SOV_FX'], 'CNY', 260e6), L('Головной банк', 'CORP_FX', ['CORP_FX'], 'GBP', -6e6)]
        .concat(opt.chf ? [L('Головной банк', 'FXB', ['FXB'], 'CHF', 1e6)] : []) });
    if (opt.lines && days[0].lines.length) days[0].lines = opt.lines(days[0].lines);  // CHF: курса нет ни в прогоне, ни в отчёте
  }
  return { runId: null, asOf: AS_OF, from: days[0].date, to: AS_OF, books: {}, days };
}
const WHY = { dataset: 'ocp', name: 'ОВП', on: AS_OF, adapter: 'ok synthetic', sources: ['ocp_synthetic'], in_force: null,
  files: { total: 0, by_status: {}, status_text: {}, later: { total: 0, by_status: {}, first_date: null } }, latest: null,
  unread: { ignored: 0, too_old: 0 }, lookback_days: 35, folders: { count: 1, scanned: 1, missing: 0 }, settings_link: false, last_scan: null };

async function open(browser, { width = 1366, theme = 'dark', state = null, hist = {}, store = {} } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(([t, st]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); Object.entries(st).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, [theme, store]);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname.startsWith('/api/v1/history/ocp') || u.pathname.startsWith('/api/v1/inbox/datasets/')) calls.push(u.pathname + u.search);
    if (u.pathname === '/api/v1/history/ocp') return json(history(state, hist));
    if (u.pathname === '/api/v1/inbox/datasets/ocp') return json(WHY);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c16-ocp.html');
  await pg.waitForSelector('[data-ocp-total]');
  await pg.waitForTimeout(300);
  return { pg, ctx, errors, calls };
}

test('C16 on a run: the published OCP of the books, switches, history, themes and widths', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const state of [null, 'no_report']) for (const theme of ['light', 'dark']) for (const width of [1366, 1920]) {
      const { pg, ctx, errors, calls } = await open(browser, { width, theme, state });
      assert.equal(await pg.evaluate(() => window.CONCEPTS_SOURCE), 'real');
      assert.equal(calls[0], '/api/v1/history/ocp', 'прогон без номера — новейший прогон сервера');
      const text = await pg.locator('#view').textContent();
      assert.ok(!BAD.test(text), text);
      assert.ok(text.includes('Общая ОВП') && text.includes('По книгам') && text.includes('По дням'));
      assert.equal(text.includes('часть отчёта'), !state, 'часть отчёта по юрлицу — своей строкой');
      assert.ok(!/Сверка|Перенос ОВП|RUB/.test(text), 'без сверки, переноса и рублей');
      // GBP без курса прогона: по курсу отчёта ОВП — в общей ОВП, пометка «курса нет»; без отчёта — не входит
      assert.ok((await pg.locator('[data-ocp-ccy="GBP"] .s').innerText()).includes('курса нет'));
      assert.equal((await pg.locator('[data-ocp-total]').getAttribute('title')).includes('по курсу из отчёта ОВП: GBP'), !state);
      assert.equal(text.includes('Почему отчёта ОВП нет'), state === 'no_report');
      if (state === 'no_report') assert.ok(calls.includes('/api/v1/inbox/datasets/ocp?on=' + AS_OF), calls.join());
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c16-run' + (state ? '-' + state : '') + '-' + theme + '-' + width + '.png'), fullPage: true }); }
      assert.deepEqual(errors, []);
      await ctx.close();
    }
    const { pg, ctx, errors } = await open(browser);
    const tot = () => pg.locator('[data-ocp-total] .v').innerText();
    const before = await tot();
    await pg.getByRole('button', { name: 'курс ЦБ' }).click();
    assert.notEqual(await tot(), before, 'курс ЦБ меняет общую ОВП');
    await pg.getByRole('button', { name: 'в USD' }).click();
    assert.ok((await pg.locator('[data-ocp-ccy="CNY"] .v').innerText()).includes('mm USD'));
    const bars = () => pg.evaluate(() => echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption().xAxis[0].data.length);
    const m1 = await bars();
    await pg.getByRole('button', { name: '3 мес' }).click();
    const m3 = await bars();
    await pg.getByRole('button', { name: '2 нед' }).click();
    assert.ok(await bars() < m1 && m1 < m3, [m1, m3].join());
    assert.deepEqual(errors, []);
    await ctx.close();
    // 390 px: без прокрутки вбок
    const narrow = await open(browser, { width: 390 });
    assert.ok(await narrow.pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'нет прокрутки вбок');
    assert.deepEqual(narrow.errors, []);
    await narrow.ctx.close();
  } finally { await browser.close(); }
});

test('C16: own book selection, the header filter named; the «Общая» line without a rate-less currency (TDM#533, TDM#534)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    // в шапке — фильтр книг и «без HTM»: к ОВП не применяются, пометка видна
    const HDR = JSON.stringify({ books: ['FXB'], portsOff: ['HTM'] });
    // в своём выборе запомнена снятая книга, которой в истории нет: выбор она не сужает и при сохранении остаётся
    const OWN = JSON.stringify({ entity: null, off: ['GONE'] });
    const { pg, ctx, errors } = await open(browser, { store: { 'concepts-filter': HDR, 'tdm.view.v1:c16:books': OWN }, hist: { chf: true, failedAt: 3 } });
    const note = pg.locator('[data-c16-note]');
    assert.ok(await note.isVisible(), 'пометка видна');
    const nt = await note.innerText();
    assert.ok(nt.includes('Фильтр шапки здесь не действует') && nt.includes('без HTM') && nt.includes('портфели (HTM) не исключаются'), nt);
    const rows = () => pg.locator('[data-ocp-row]').evaluateAll(a => a.map(e => e.dataset.ocpRow));
    assert.deepEqual((await rows()).sort(), ['CORP_FX', 'FXB', 'Головной банк', 'SOV_FX'].sort(), 'все книги, не только книга шапки');
    const books = pg.locator('[data-c16="books"]');
    assert.match(await books.innerText(), /все книги · 4/);
    assert.ok(!(await pg.locator('#view').innerText()).includes('не вся'), 'снятая книга вне истории выбор не сужает');
    // снять книгу в своём выборе: пропадает из таблицы, запоминается отдельно, шапка не меняется
    await books.click();
    await pg.locator('.mi', { hasText: 'SOV_FX' }).first().click();
    assert.ok(!(await rows()).includes('SOV_FX') && !(await rows()).includes('Головной банк'), 'книга снята; часть отчёта с ней — только целиком');
    assert.match(await pg.locator('[data-c16="books"]').innerText(), /книги: 3 из 4/);
    const saved = await pg.evaluate(() => [localStorage.getItem('tdm.view.v1:c16:books'), localStorage.getItem('concepts-filter')]);
    assert.deepEqual(JSON.parse(saved[0]).off, ['GONE', 'SOV_FX'], 'GONE не стирается (снятое в другом прогоне не теряется)');
    assert.equal(saved[1], HDR, 'фильтр шапки не тронут');
    await pg.mouse.click(5, 5);
    // CHF без курса: итог — без CHF; линия есть у каждой даты с ОВП, подсказка называет CHF; день с ошибкой — причина
    assert.ok((await pg.locator('[data-ocp-total] .s').innerText()).includes('без CHF'));
    const o = await pg.evaluate(() => { const c = echarts.getInstanceByDom(document.querySelector('.c16-chart')), op = c.getOption(); return { x: op.xAxis[0].data, line: op.series.find(s => s.name === 'Общая').data }; });
    const fail = o.line.findIndex(v => v == null);
    assert.ok(fail >= 0 && o.line.filter(v => v == null).length === 1, 'пусто — только день с ошибкой расчёта: ' + JSON.stringify(o.line));
    // подсказка — наведением мыши на дату (как у человека; у дня без данных точки серии нет)
    const tipAt = async i => {
      const [px, py] = await pg.evaluate(k => { const el = document.querySelector('.c16-chart'), c = echarts.getInstanceByDom(el), r = el.getBoundingClientRect(); return [r.left + c.convertToPixel({ xAxisIndex: 0 }, k), r.top + r.height / 2]; }, i);
      await pg.mouse.move(px, py);
      await pg.waitForTimeout(200);
      return pg.locator('.c16-chart').evaluate(el => [...el.querySelectorAll('div')].map(d => d.innerText).find(t => /\d\d\.\d\d\.\d\d/.test(t)) || '');
    };
    const ok = await tipAt(o.line.length - 1);
    assert.ok(ok.includes('Общая — без CHF: курса нет') && ok.includes('CHF'), ok);
    const bad = await tipAt(fail);
    assert.ok(bad.includes("ОВП нет: ошибка расчёта ОВП"), JSON.stringify([bad, fail]));
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c16-own-books.png'), fullPage: true }); }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C16 under «report base + day»: the base date only in the tooltip, «отчёт от» for an older base, «нет в отчёте» (TDM#531)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors } = await open(browser, { hist: { bpd: true } });
    const row = id => pg.locator('[data-ocp-row="' + id + '"] td').first();
    const corp = await row('CORP_FX').innerText();
    assert.ok(!corp.includes('отчёт от') && !corp.includes('нет в отчёте'), corp);
    assert.match(await row('CORP_FX').locator('span').first().getAttribute('title'), /база \d\d\.\d\d/);
    assert.ok((await row('FXB').innerText()).includes('отчёт от'), 'база старше прошлого рабочего дня названа');
    assert.ok((await row('NEWB').innerText()).includes('нет в отчёте'));
    assert.ok(!BAD.test(await pg.locator('#view').textContent()));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
