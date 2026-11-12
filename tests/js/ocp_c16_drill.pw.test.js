// C16 «ОВП» in the browser on the demo response (?demo=1; Playwright, skipped without it):
// TDM#604 — one book selection for every day: the demo has days of two kinds (older ones with an entity-level report
// part without books and a book the newer days lack); with a narrowed selection «месяц» and «3 мес» agree on their
// common dates and no day holds an unchecked or never-chosen book; the point's tooltip counts the day's books.
// TDM#603 — the drill-down of a book row: parts with their contributions summing to the cell, the report lines taken out
// as derivatives against what the positions add, links to C20 and C03, a past day from /ocp of that day's run.
//   node --test tests/js/ocp_c16_drill.pw.test.js        C16_SHOTS=<dir> also saves screenshots
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const SHOTS = process.env.C16_SHOTS;
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;

async function open(browser, { width = 1366, theme = 'light', store = {}, url = '/concepts/c16-ocp.html?demo=1', api = {} } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(([t, st]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); Object.entries(st).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, [theme, store]);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/')) calls.push(u.pathname + u.search);
    if (api[u.pathname]) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(api[u.pathname]) });
    if (u.pathname === '/api/v1/auth/state') return route.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
    if (u.pathname.startsWith('/api/') || u.pathname === '/concepts-run.js' || u.pathname === '/concepts-data.js') return route.fulfill({ status: 404, body: '' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  await pg.waitForSelector('[data-ocp-total]');
  await pg.waitForTimeout(200);
  return { pg, ctx, errors, calls };
}
const chartOf = pg => pg.evaluate(() => { const o = echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption();
  return { x: o.xAxis[0].data, line: o.series.find(s => s.name === 'Общая').data, series: o.series.map(s => s.name) }; });
const tipAt = (pg, i) => pg.evaluate(k => { const o = echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption();
  return o.tooltip[0].formatter(o.series.map(z => ({ dataIndex: k, seriesName: z.name, value: 1, marker: '' }))); }, i);

test('C16 demo: one book selection for every day — «месяц» and «3 мес» agree, no unchecked book in any day (TDM#604)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors } = await open(browser);
    assert.equal(await pg.evaluate(() => window.CONCEPTS_SOURCE), 'demo');
    // все книги: в старых днях есть часть юрлица без книг и прежняя книга — входят, подсказка без «исключено»
    await pg.getByRole('button', { name: '3 мес' }).click();
    const all = await chartOf(pg);
    const t0 = await tipAt(pg, 0);
    assert.ok(/книг дня: (\d+) из \1</.test(t0) && !t0.includes('без книг не учтена'), t0);
    // сузить выбор: снять одну книгу — прежняя книга (её нет в таблице) и часть юрлица без книг не входят ни в один день
    await pg.getByRole('button', { name: 'месяц' }).click();
    await pg.locator('[data-c16="books"]').click();
    await pg.locator('.mi', { hasText: 'FX: прежняя книга' }).first().click();  // снять прежнюю — сужает выбор
    await pg.mouse.click(5, 5);
    const m1 = await chartOf(pg);
    await pg.getByRole('button', { name: '3 мес' }).click();
    const m3 = await chartOf(pg);
    assert.ok(m3.x.length > m1.x.length + 15, [m1.x.length, m3.x.length].join());
    const at = Object.fromEntries(m3.x.map((d, i) => [d, m3.line[i]]));
    m1.x.forEach((d, i) => assert.ok(Math.abs(at[d] - m1.line[i]) < 1e-9, d + ': ' + at[d] + ' vs ' + m1.line[i]));
    // старый день под суженным выбором меньше, чем без выбора, на часть юрлица и прежнюю книгу
    assert.ok(m3.line[0] < all.line[0] - 5, [m3.line[0], all.line[0]].join());
    const t1 = await tipAt(pg, 0);
    assert.ok(/исключено выбором: 1/.test(t1) && t1.includes('часть отчёта по юрлицу') && t1.includes('без книг не учтена'), t1);
    const tl = await tipAt(pg, m3.x.length - 1);
    assert.ok(/книг дня: (\d+) из \1</.test(tl) && !tl.includes('без книг'), 'в новом дне ни одна книга не снята: ' + tl);
    const saved = JSON.parse(await pg.evaluate(() => localStorage.getItem('tdm.view.v1:c16:books')));
    assert.ok(saved.off.includes('FXB-OLD') && !saved.on.includes('FXB-OLD') && saved.on.length > 3, JSON.stringify(saved));
    assert.ok(!BAD.test(await pg.locator('#view').textContent()));
    if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c16-604.png'), fullPage: true }); }
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

// ревью #608: сужение выбора на одном юрлице не снимает книги других юрлиц, когда выбор снова «все юрлица»
test('C16 demo: unchecking a book of one entity keeps the other entities\' books under «все юрлица» (TDM#604)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors } = await open(browser);
    const rows = () => pg.evaluate(() => [...document.querySelectorAll('[data-ocp-row]')].map(r => r.getAttribute('data-ocp-row')));
    const before = await rows();
    assert.ok(before.includes('IC-BND') && before.includes('AM') && before.includes('HTM'), before.join());
    await pg.locator('[data-c16="entity"]').click();
    await pg.locator('.mi').filter({ hasNotText: 'все юрлица' }).filter({ hasText: /Банк|BANK/ }).first().click();
    await pg.locator('[data-c16="books"]').click();
    await pg.locator('.mi', { hasText: 'HTM' }).first().click();
    await pg.mouse.click(5, 5);
    assert.ok(!(await rows()).includes('HTM'));
    await pg.locator('[data-c16="entity"]').click();
    await pg.locator('.mi', { hasText: 'все юрлица' }).first().click();
    const after = await rows();
    assert.ok(!after.includes('HTM'), 'снятая книга остаётся снятой: ' + after.join());
    assert.ok(after.includes('IC-BND') && after.includes('AM'), 'книги других юрлиц не снимались: ' + after.join());
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

// TDM#603: щелчок по строке книги — из чего складывается её ОВП за день; по сумме валюты — только эта валюта
test('C16 demo: a book row opens what its OCP consists of; the total equals the cell; report lines taken out as derivatives (TDM#603)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [1366, 390]) {
      const { pg, ctx, errors } = await open(browser, { width, theme: width === 390 ? 'dark' : 'light' });
      const cellText = await pg.locator('[data-ocp-row="FXB-FWD"] td[data-ocp-c="USD"]').innerText();
      await pg.locator('[data-ocp-row="FXB-FWD"] td').first().click();
      const dr = pg.locator('.k-drawer[data-c16-drill]');
      await dr.locator('[data-c16-drill-ccy]').first().waitFor();
      assert.deepEqual(await dr.locator('[data-c16-drill-ccy]').evaluateAll(a => a.map(e => e.dataset.c16DrillCcy)), ['USD', 'EUR', 'CNY'], 'все валюты строки');
      // итог USD = ячейке, и слагаемые складываются в итог
      const usd = dr.locator('table[data-vk="c16-drill"]').first();
      const sumRow = await usd.locator('tr[data-c16-sum]').innerText();
      const num = t => { const s = t.replace(/\s/g, ''), v = parseFloat(s.replace(/[()+−-]/g, '')); return /^\(|^[−-]/.test(s) ? -v : v; };
      assert.ok(sumRow.includes('= ячейке таблицы') && Math.abs(num(sumRow.split(/\t|\n/).pop()) - num(cellText)) < 0.051, sumRow + ' | ' + cellText);
      const parts = await usd.locator('tr[data-c16-part] td.n').allInnerTexts();
      assert.ok(Math.abs(parts.map(num).reduce((a, v) => a + v, 0) - num(sumRow.split(/\t|\n/).pop())) < 0.02, parts.join() + ' vs ' + sumRow);
      // база с датой отчёта, сделка дня ведёт на C20 с книгой, позиция — на C03
      assert.match(await dr.innerText(), /База — отчёт ОВП от \d\d\.\d\d\.\d\d/);
      const trade = await usd.locator('a[href^="c20-trades.html"]').first().getAttribute('href');
      assert.match(trade, /^c20-trades\.html\?date=\d{4}-\d\d-\d\d&book_id=FXB-FWD$/);
      assert.match(await usd.locator('a[href^="c03-blotter.html?pos="]').first().getAttribute('href'), /pos=FXB-FWD%7CFWD-1/);
      // строки отчёта, исключённые как деривативы: вынесено больше, чем добавлено из позиций — предупреждение; типы строк
      const ex = dr.locator('[data-c16-drill-ccy="USD"] ~ [data-c16-excluded]').first();
      assert.ok(await ex.isVisible());
      assert.equal(await dr.locator('[data-c16-exwarn]').count(), 1, 'предупреждение только у USD');
      const types = await dr.locator('table[data-c16-types]').first().innerText();
      assert.ok(types.includes('Тип строки B → валютные форварды') && types.includes('Тип строки C: спот оставлен по номиналу'), types);
      // TDM#693: строки спот-типа с будущей датой — вынесена сделкой, без кривой, не сопоставлена
      const sf = dr.locator('[data-c16-spotfuture] ~ table[data-vk="c16-drill-spotfuture"]').first();
      assert.equal(await dr.locator('[data-c16-spotfuture]').count(), 1, 'раздел только у USD');
      assert.match(await sf.locator('[data-c16-sf-out]').innerText(), /вынесено из базы: строка отчёта USD, 22\.12\.\d\d, EUR\/USD → сделка D-1001/);
      assert.match(await sf.locator('[data-c16-sf-df1]').innerText(), /нет кривой EUR: дисконт-фактор 1 · сделка D-1001/);
      assert.match(await sf.locator('[data-c16-sf-un]').innerText(), /не сопоставлено: строка отчёта USD.*осталась в базе/);
      assert.ok(!BAD.test(await dr.innerText()));
      if (width === 390) assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'нет прокрутки вбок');
      if (SHOTS) { fs.mkdirSync(SHOTS, { recursive: true }); await pg.screenshot({ path: path.join(SHOTS, 'c16-603-' + width + '.png') }); }
      await pg.keyboard.press('Escape');
      // сумма валюты: только эта валюта; разбор обрезан — «показаны 10 из 14»
      await pg.locator('[data-ocp-row="FXB-SWAP"] td[data-ocp-c="CNY"]').click();
      await pg.locator('.k-drawer [data-c16-drill-ccy]').first().waitFor();
      assert.deepEqual(await pg.locator('.k-drawer [data-c16-drill-ccy]').evaluateAll(a => a.map(e => e.dataset.c16DrillCcy)), ['CNY']);
      assert.equal(await pg.locator('.k-drawer [data-c16-trunc]').innerText(), 'показаны 10 из 14');
      // другой день — свой разбор, итог = ячейке того дня
      const sel = pg.locator('.k-drawer [data-c16-day]');
      const other = await sel.locator('option').nth(3).getAttribute('value');
      await sel.selectOption(other);
      await pg.waitForFunction(d => (document.querySelector('.k-drawer .st') || {}).textContent.includes(d.slice(8, 10) + '.' + d.slice(5, 7)), other);
      await pg.locator('.k-drawer [data-c16-sum]').first().waitFor();
      assert.ok((await pg.locator('.k-drawer [data-c16-sum]').first().innerText()).includes('= ячейке таблицы'));
      assert.deepEqual(errors, []);
      await ctx.close();
    }
  } finally { await browser.close(); }
});

test('C16 on a run: the drill-down of a past day asks /ocp of that day\'s run (TDM#603)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const { TEXT: RUN } = require('./fixtures/concepts_run');
  const AS_OF = JSON.parse(RUN).asOf;
  const prev = s => { const t = new Date(s + 'T00:00:00Z'); do t.setUTCDate(t.getUTCDate() - 1); while (!(t.getUTCDay() % 6)); return t.toISOString().slice(0, 10); };
  const D1 = prev(AS_OF), B = prev(D1);
  const L = (amount, extra) => Object.assign({ entity: 'Головной банк', book: 'FXB', books: ['FXB'], ccy: 'USD', amount, source: 'report', reported: null, validDate: B }, extra || {});
  const FX = { USD: { market: 81.45, cbr: 81.12 } };
  const hist = { runId: null, asOf: AS_OF, from: D1, to: AS_OF, books: {}, days: [
    { date: D1, runId: 7, runType: 'EOD', state: 'computed', basis: 'report_plus_outside', fx: FX, lines: [L(5e6)] },
    { date: AS_OF, runId: 8, runType: 'EOD', state: 'computed', basis: 'report_base_plus_day', fx: FX, lines: [L(3e6, { baseDate: D1, baseKind: 'without_derivatives', parts: {} })] }] };
  const block = (id, x) => ({ run_id: id, state: 'computed', components: [{ key: 'options', name: 'опционы' }], parts: [{ key: 'base', name: 'база: отчёт ОВП без деривативов' }], published: { basis: x.basis, by_book: [x.line] } });
  // прошлый день — прежняя методика: отчёт целиком + вне отчёта; текущий — база + PV деривативов
  const old = block(7, { basis: 'report_plus_outside', line: { entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: 'USD', amount: 5e6, source: 'report', components: ['options'], outside: { options: 1e6 }, reported: 4e6, valid_date: B } });
  const now = block(8, { basis: 'report_base_plus_day', line: { entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: 'USD', amount: 3e6, source: 'report', components: [], outside: {}, reported: 2e6, valid_date: D1, base_date: D1, base_kind: 'without_derivatives',
    parts: { base: 2e6, derivatives: 1e6, bonds: 0, trades: 0, outside: 0 }, contributions: { base: { items: [{ kind: 'report', valid_date: D1, file: 'ocp.csv', rows: 1, amount: 2e6 }], rest: null }, derivatives: { items: [{ kind: 'position', position_id: 'FXB|F1', derivative: 'fx_forward', amount: 1e6 }], rest: null } },
    excluded: [{ kind: 'fx_forward', component: 'fx_legs', nominal: 1.02e6, added: 1e6, outside: 0, positions: 1, fallback: false, diff: 2e4 }] } });
  const b2 = await pw.chromium.launch();
  try {
    const ctx = await b2.newContext({ viewport: { width: 1366, height: 900 } }), pg = await ctx.newPage(), errors = [], asked = [];
    pg.on('pageerror', e => errors.push(e.message));
    await pg.route('**/*', async route => {
      const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
      if (u.origin !== ORIGIN) return route.abort();
      if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
      if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
      if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
      if (u.pathname.startsWith('/api/v1/runs/') && u.pathname.endsWith('/ocp')) asked.push(u.pathname);
      if (u.pathname === '/api/v1/history/ocp') return json(hist);
      if (u.pathname === '/api/v1/runs/7/ocp') return json(old);
      if (u.pathname === '/api/v1/runs/8/ocp') return json(now);
      if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c16-ocp.html');
    await pg.waitForSelector('[data-ocp-row="FXB"]');
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    await pg.locator('.k-drawer [data-c16-sum]').waitFor();
    assert.deepEqual(asked, ['/api/v1/runs/8/ocp'], 'день таблицы — прогон дня');
    assert.ok((await pg.locator('.k-drawer [data-c16-sum]').innerText()).includes('= ячейке таблицы'));
    assert.equal(await pg.locator('.k-drawer [data-c16-exwarn]').count(), 0, 'небольшая разница PV и номинала — без предупреждения');
    await pg.locator('.k-drawer [data-c16-day]').selectOption(D1);
    await pg.waitForFunction(() => document.querySelector('.k-drawer [data-c16-sum]') && document.querySelector('.k-drawer .st').textContent.includes('#7'));
    assert.deepEqual(asked, ['/api/v1/runs/8/ocp', '/api/v1/runs/7/ocp'], 'прошлый день — /ocp его прогона, не история');
    const txt = await pg.locator('.k-drawer [data-c16-dbody]').innerText();
    assert.ok(txt.includes('целиком, с деривативами') && txt.includes('вне отчёта: опционы') && txt.includes('= ячейке таблицы'), txt);
    // повторно тот же день — без нового запроса
    await pg.locator('.k-drawer [data-c16-day]').selectOption(AS_OF);
    await pg.locator('.k-drawer [data-c16-part="base"]').waitFor();
    assert.equal(asked.length, 2);
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await b2.close(); }
});

// 0.2.21: /ocp прогона ответил 500 — панель называет запрос, код ответа, сообщение сервера и номер запроса для журнала;
// ответ с пропущенными элементами блока (left_out) показывает остальное и говорит, что часть не показана
test('C16 on a run: a failed /ocp says which request and status; a block with left-out elements still shows the line', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const { TEXT: RUN } = require('./fixtures/concepts_run');
  const AS_OF = JSON.parse(RUN).asOf;
  const prev = s => { const t = new Date(s + 'T00:00:00Z'); do t.setUTCDate(t.getUTCDate() - 1); while (!(t.getUTCDay() % 6)); return t.toISOString().slice(0, 10); };
  const D1 = prev(AS_OF);
  const L = amount => ({ entity: 'Головной банк', book: 'FXB', books: ['FXB'], ccy: 'USD', amount, source: 'derived', reported: null, validDate: null });
  const FX = { USD: { market: 81.45, cbr: 81.12 } };
  const hist = { runId: null, asOf: AS_OF, from: D1, to: AS_OF, books: {}, days: [
    { date: D1, runId: 7, runType: 'EOD', state: 'computed', basis: 'report_base_plus_day', fx: FX, lines: [L(5e6)] },
    { date: AS_OF, runId: 8, runType: 'EOD', state: 'computed', basis: 'report_base_plus_day', fx: FX, lines: [L(3e6)] }] };
  const old = { run_id: 7, state: 'computed', components: [], parts: [], left_out: ['gaps.0.code: literal_error'],
    published: { basis: 'report_base_plus_day', by_book: [{ entity: 'Головной банк', book_id: 'FXB', books: ['FXB'], ccy: 'USD', amount: 5e6, source: 'derived', components: [], outside: {}, reported: null, valid_date: null }] } };
  const b2 = await pw.chromium.launch();
  try {
    const ctx = await b2.newContext({ viewport: { width: 1366, height: 900 } }), pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    await pg.route('**/*', async route => {
      const u = new URL(route.request().url()), json = (o, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(o) });
      if (u.origin !== ORIGIN) return route.abort();
      if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
      if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
      if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
      if (u.pathname === '/api/v1/history/ocp') return json(hist);
      if (u.pathname === '/api/v1/runs/8/ocp') return json({ code: 'internal', message: 'internal error', request_id: 'rq-123', details: null }, 500);
      if (u.pathname === '/api/v1/runs/7/ocp') return json(old);
      if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c16-ocp.html');
    await pg.waitForSelector('[data-ocp-row="FXB"]');
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    await pg.locator('.k-drawer [data-c16-dbody] .k-empty').waitFor();
    const msg = await pg.locator('.k-drawer [data-c16-dbody]').innerText();
    assert.ok(msg.includes('Разбор не загрузился') && msg.includes('500') && msg.includes('прогона #8') && msg.includes('/api/v1/runs/8/ocp')
      && msg.includes('internal error') && msg.includes('rq-123') && msg.includes('журнале монитора'), msg);
    await pg.locator('.k-drawer [data-c16-day]').selectOption(D1);
    await pg.locator('.k-drawer [data-c16-leftout]').waitFor();
    const txt = await pg.locator('.k-drawer [data-c16-dbody]').innerText();
    assert.ok(txt.includes('Часть разбора не показана: 1') && txt.includes('gaps.0.code') && txt.includes('= ячейке таблицы'), txt);
    assert.ok(!BAD.test(txt));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await b2.close(); }
});
