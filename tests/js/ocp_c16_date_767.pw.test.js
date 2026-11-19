// C16 «ОВП» on a run (Playwright; skipped without it), TDM#767: the table and its breakdown open on any date of the history.
// The date is chosen in the «Дата» row (a day of the answer, «любая дата», ?date= in the address) or by a click on the day's
// column of the «По дням» chart; the table, the chart's figure of that date and the breakdown (the date's own run, the
// /api/v1/history/ocp/day answer) agree; a date outside the answer's period is asked for; a date with no run, a run of an older
// methodology and a part the run did not save say so (no guess).
//   node --test tests/js/ocp_c16_date_767.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json' };
const BAD = /\bNaN\b|\bundefined\b/;
const AS_OF = JSON.parse(RUN).asOf;

const prevDay = s => { const t = new Date(s + 'T00:00:00Z'); do t.setUTCDate(t.getUTCDate() - 1); while (!(t.getUTCDay() % 6)); return t.toISOString().slice(0, 10); };
const FX = { USD: { market: 81.45, cbr: 81.12 }, EUR: { market: 94.62, cbr: 94.38 } };
// a day of the history: its figures are its own (the date's index), two books in two currencies
function mkDay(date, i, old) {
  const k = 1 + i;
  const L = (book, ccy, amount) => Object.assign({ entity: 'Головной банк', book, books: [book], ccy, amount, source: 'report', reported: null, validDate: null },
    old ? {} : { baseDate: prevDay(date), baseOlder: false, baseKind: 'without_derivatives', setId: 900 + i, parts: { base: amount * 0.9, derivatives: amount * 0.1 } });
  return { date, runId: 100 + i, runType: 'EOD', cutType: 'eod_final', state: 'computed', basis: old ? 'report_plus_outside' : 'report_base_plus_day', fx: FX, reportFx: {},
    methodNote: old ? 'другая методика: отчёт целиком (с деривативами) и слагаемые вне отчёта' : null,
    lines: [L('FXB', 'USD', 1e6 * k), L('FXB', 'EUR', -2e6 * k), L('CORP', 'USD', 3e5 * k)] };
}
// the days of the answer: 25 weekdays up to AS_OF; the older half is of the old methodology
function history() {
  const days = [];
  for (let d = AS_OF, i = 24; i >= 0; d = prevDay(d), i--) days.unshift(mkDay(d, i, i < 12));
  return { runId: null, asOf: AS_OF, from: days[0].date, to: AS_OF, books: {}, days };
}
// the run's /ocp of a day: the line with its parts and contributions (the old methodology: the report whole and outside it)
function runBlock(day) {
  const by = day.lines.map(x => x.parts ? { entity: x.entity, book_id: x.book, books: x.books, ccy: x.ccy, amount: x.amount, source: 'report', components: [], outside: {}, reported: x.parts.base,
    valid_date: x.baseDate, base_date: x.baseDate, set_id: x.setId, base_kind: x.baseKind, parts: x.parts,
    contributions: { base: { items: [{ kind: 'report', valid_date: x.baseDate, file: 'ocp_' + day.date + '.csv', rows: 1, amount: x.parts.base }], rest: null },
      derivatives: { items: [{ kind: 'position', position_id: x.book + '|F1', derivative: 'fx_forward', amount: x.parts.derivatives }], rest: null } }, excluded: [] }
    : { entity: x.entity, book_id: x.book, books: x.books, ccy: x.ccy, amount: x.amount, source: 'report', components: [], outside: {}, reported: x.amount, valid_date: prevDay(day.date) });
  return { run_id: day.runId, state: 'computed', components: [], parts: [{ key: 'base', name: 'база: отчёт ОВП без деривативов' }, { key: 'derivatives', name: 'PV деривативов из позиций' }],
    published: { basis: day.basis, by_book: by } };
}
// /api/v1/history/ocp/day of a date: the day with the breakdown of each line (what the run saved, what it did not)
function dayDoc(day, { noContrib } = {}) {
  return Object.assign({}, day, { books: {}, lines: day.lines.map(x => Object.assign({}, x, { breakdown: x.parts
    ? { status: noContrib ? 'partial' : 'full', rows: Object.entries(x.parts).map(([key, amount]) => ({ key, name: key, amount, items: 1, rest: null, ...(key === 'base' ? { file: 'ocp_' + day.date + '.csv' } : {}) })), sum: x.amount, matches: true,
      absent: noContrib ? [{ code: 'no_contributions', part: 'derivatives', reason: '«PV деривативов из позиций»: из чего сложилась цифра, прогон не сохранил (вкладов нет)' }] : [] }
    : { status: 'partial', rows: [{ key: 'report', name: 'отчёт ОВП целиком, с деривативами', amount: x.amount, items: 0, rest: null }], sum: x.amount, matches: true,
      absent: [{ code: 'old_methodology', part: null, reason: 'прогон считан по прежней методике (отчёт целиком и слагаемые вне отчёта): базы отчёта без деривативов, PV деривативов, изменений дня и переноса в нём нет' }] } })) });
}

async function open(browser, url = '/concepts/c16-ocp.html', { width = 1366, theme = 'light', dayAnswer, mutate } = {}) {
  const hist = history(); if (mutate) mutate(hist);
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(([t]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); } catch (e) { } }, [theme]);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = (o, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname.startsWith('/api/v1/history/ocp') || /^\/api\/v1\/runs\/\d+\/ocp$/.test(u.pathname)) calls.push(u.pathname + u.search);
    if (u.pathname === '/api/v1/history/ocp') return json(hist);
    if (u.pathname === '/api/v1/history/ocp/day') {
      const on = u.searchParams.get('date'), day = hist.days.find(x => x.date === on);
      if (dayAnswer && dayAnswer[on]) return dayAnswer[on](json);
      if (day) return json(dayDoc(day));
      return json({ code: 'not_found', message: 'no saved run of ' + on + ' to show its OCP', details: { reason: 'no_run_on_date', date: on } }, 404);
    }
    const m = u.pathname.match(/^\/api\/v1\/runs\/(\d+)\/ocp$/);
    if (m) { const day = hist.days.find(x => x.runId === +m[1]); return day ? json(runBlock(day)) : json({ message: 'no run' }, 404); }
    if (u.pathname === '/api/v1/inbox/datasets/ocp') return json({ dataset: 'ocp', name: 'ОВП', on: AS_OF, sources: [], in_force: null, files: { total: 0, by_status: {}, status_text: {}, later: { total: 0, by_status: {}, first_date: null } }, latest: null, unread: { ignored: 0, too_old: 0 }, lookback_days: 35, folders: { count: 1, scanned: 1, missing: 0 }, settings_link: false, last_scan: null });
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + url);
  await pg.waitForSelector('[data-c16-days]');
  await pg.waitForTimeout(250);
  return { pg, ctx, errors, calls, hist };
}
const chartLine = pg => pg.evaluate(() => { const o = echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption();
  return { x: o.xAxis[0].data, line: o.series.find(s => s.name === 'Общая').data }; });
// USD equivalent of a day's lines at the market rates, mm — what the table's total and the chart's «Общая» show
const usdMm = day => day.lines.reduce((a, x) => a + x.amount * FX[x.ccy].market / FX.USD.market, 0) / 1e6;
const totalText = pg => pg.locator('[data-ocp-total] .v').innerText();
const num = t => parseFloat(t.replace(/\s/g, '').replace(/[()]/g, '-').replace('−', '-').replace(/[^0-9.\-]/g, ''));

test('C16 date: a day chip opens the table of that date from its run; the figure is the chart\'s; ?date= follows (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls, hist } = await open(browser);
    assert.equal(await pg.locator('[data-c16-today]').count(), 0, 'дата прогона — без возврата');
    assert.equal(new URL(pg.url()).searchParams.get('date'), null);
    const today = await totalText(pg);
    assert.ok(Math.abs(num(today) - usdMm(hist.days[24])) < 0.06, today);
    // выбрать день из строки «Дата»: таблица этой даты — своя цифра, свой прогон, адрес с ?date=
    const target = hist.days[20];
    await pg.locator('[data-c16-day="' + target.date + '"]').click();
    await pg.waitForFunction(d => new URL(location.href).searchParams.get('date') === d, target.date);
    const t = await totalText(pg);
    assert.ok(Math.abs(num(t) - usdMm(target)) < 0.06 && t !== today, t + ' vs ' + usdMm(target));
    assert.match(await pg.locator('.k-panel .hd h3').first().innerText(), /^ОВП на \d+ \S+/i);
    assert.equal(await pg.locator('[data-c16-run]').innerText(), 'итоговый прогон #' + target.runId);
    assert.ok((await pg.locator('[data-c16-dayinfo]').innerText()).includes('#' + target.runId));
    assert.equal(await pg.locator('[data-c16-day="' + target.date + '"].on').count(), 1);
    // цифра даты в таблице = цифре столбца этой даты на графике
    const ch = await chartLine(pg), i = ch.x.indexOf(await pg.evaluate(d => KIT.f.dshort(d), target.date));
    assert.ok(i >= 0 && Math.abs(ch.line[i] - num(t)) < 0.06, ch.line[i] + ' vs ' + t);
    // книга: ячейка USD дня = сумме строки дня
    const cell = num(await pg.locator('[data-ocp-row="FXB"] td[data-ocp-c="USD"]').innerText());
    assert.ok(Math.abs(cell - target.lines[0].amount / 1e6) < 0.06, String(cell));
    // разбор строки — прогон этой даты и день с разбором; сумма слагаемых = итог = ячейка
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    await pg.locator('.k-drawer [data-c16-sum]').first().waitFor();
    assert.ok(calls.includes('/api/v1/runs/' + target.runId + '/ocp'), calls.join());
    assert.ok(calls.some(c => c.startsWith('/api/v1/history/ocp/day?date=' + target.date)), calls.join());
    assert.ok(!calls.includes('/api/v1/runs/' + hist.days[24].runId + '/ocp'), 'прогон дня прогона страницы не спрашивали');
    const dr = pg.locator('.k-drawer');
    assert.ok((await dr.locator('.st').innerText()).includes('#' + target.runId));
    const usd = dr.locator('table[data-vk="c16-drill"]').first();
    const parts = (await usd.locator('tr[data-c16-part] td.n').allInnerTexts()).map(num);
    const total = num((await usd.locator('tr[data-c16-sum]').innerText()).split(/\t|\n/).pop());
    assert.ok(Math.abs(parts.reduce((a, v) => a + v, 0) - total) < 0.02 && Math.abs(total - cell) < 0.02, parts.join() + ' / ' + total + ' / ' + cell);
    assert.match(await dr.locator('[data-c16-base]').first().innerText(), /набор №920, файл ocp_\d{4}-\d\d-\d\d\.csv/);
    assert.equal(await dr.locator('[data-c16-nodata]').count(), 0, 'всё сохранено — без пометок');
    await pg.keyboard.press('Escape');
    // назад к дате прогона
    await pg.locator('[data-c16-today]').click();
    await pg.waitForFunction(() => new URL(location.href).searchParams.get('date') === null);
    assert.equal(await totalText(pg), today);
    assert.ok(!BAD.test(await pg.locator('#view').textContent()));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C16 date: a click on a day\'s column of the chart opens that date (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, hist } = await open(browser);
    await pg.locator('.c16-chart').scrollIntoViewIfNeeded();
    await pg.waitForTimeout(200);
    const where = await pg.evaluate(() => { const el = document.querySelector('.c16-chart'), c = echarts.getInstanceByDom(el), n = c.getOption().xAxis[0].data.length, r = el.getBoundingClientRect(),
        g = c.getModel().getComponent('grid', 0).coordinateSystem.getRect();
      return { n, r: { x: r.x, y: r.y }, pts: Array.from({ length: n }, (_, i) => c.convertToPixel({ xAxisIndex: 0 }, i)), top: g.y + g.height / 2 }; });
    const ch = await chartLine(pg), k = 3, date = hist.days.find(x => KIT_dshort(x.date) === ch.x[k]).date;
    await pg.mouse.click(where.r.x + where.pts[k], where.r.y + where.top);
    await pg.waitForFunction(d => new URL(location.href).searchParams.get('date') === d, date);
    const day = hist.days.find(x => x.date === date);
    assert.ok(Math.abs(num(await totalText(pg)) - usdMm(day)) < 0.06);
    assert.equal(await pg.locator('[data-c16-run]').innerText(), 'итоговый прогон #' + day.runId);
    // выбранная дата на графике — пунктир
    const ml = await pg.evaluate(() => echarts.getInstanceByDom(document.querySelector('.c16-chart')).getOption().series.find(s => s.name === 'Общая').markLine);
    assert.ok(ml && JSON.stringify(ml).includes('"xAxis":' + k), JSON.stringify(ml));
    // щелчок по другому столбцу — другая дата; повторный по тому же — та же
    await pg.mouse.click(where.r.x + where.pts[k + 5], where.r.y + where.top);
    const date2 = hist.days.find(x => KIT_dshort(x.date) === ch.x[k + 5]).date;
    await pg.waitForFunction(d => new URL(location.href).searchParams.get('date') === d, date2);
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
  function KIT_dshort(s) { const m = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']; return (+s.slice(8, 10)) + ' ' + m[+s.slice(5, 7) - 1]; }
});

test('C16 date: ?date= and the date field open any date; one outside the answer is asked for, one with no run says so (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    // дата вне периода ответа: день с разбором — /history/ocp/day; книги и цифры — его
    const old = mkDay('2026-04-14', 90, false), oldDoc = dayDoc(old);
    const a = await open(browser, '/concepts/c16-ocp.html?date=2026-04-14', { dayAnswer: { '2026-04-14': json => json(oldDoc) } });
    assert.ok(a.calls.some(c => c.startsWith('/api/v1/history/ocp/day?date=2026-04-14')), a.calls.join());
    assert.ok(Math.abs(num(await totalText(a.pg)) - usdMm(old)) < 0.06);
    assert.equal(await a.pg.locator('[data-c16-run]').innerText(), 'итоговый прогон #' + old.runId);
    assert.equal(await a.pg.locator('[data-c16-date]').inputValue(), '2026-04-14');
    assert.equal(await a.pg.locator('[data-c16-day].on').getAttribute('data-c16-day'), '2026-04-14');
    assert.deepEqual(a.errors, []);
    await a.ctx.close();
    // выходной день внутри периода: итогового прогона нет — так и сказано, цифр нет; поле даты и возврат работают
    const b = await open(browser);
    await b.pg.locator('[data-c16-date]').fill('2026-10-10');
    await b.pg.locator('[data-c16-date]').dispatchEvent('change');
    await b.pg.waitForSelector('[data-c16-today]');
    await b.pg.locator('.k-empty').first().waitFor();
    assert.match(await b.pg.locator('.k-empty').first().innerText(), /ОВП за 10\.10\.\d\d нет: на эту дату нет сохранённого итогового прогона/);
    assert.equal(await b.pg.locator('[data-ocp-total]').count(), 0, 'цифр нет, не подставлены');
    assert.ok(!BAD.test(await b.pg.locator('#view').textContent()));
    await b.pg.locator('[data-c16-today]').click();
    await b.pg.waitForSelector('[data-ocp-total]');
    // сервер не ответил (500): день назван не загрузившимся, не пустым и не подставленным
    const c = await open(browser, '/concepts/c16-ocp.html?date=2026-03-02', { dayAnswer: { '2026-03-02': json => json({ code: 'internal', message: 'internal error', request_id: 'rq-9' }, 500) } });
    const txt = await c.pg.locator('.k-empty').first().innerText();
    assert.ok(txt.includes('не удалось загрузить') && txt.includes('500') && txt.includes('rq-9'), txt);
    assert.equal(await c.pg.locator('[data-ocp-total]').count(), 0);
    assert.deepEqual(c.errors, []);
    await b.ctx.close(); await c.ctx.close();
  } finally { await browser.close(); }
});

test('C16 drill: a day whose report is still with derivatives says how the file was taken and which button reads it again (TDM#806)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const mutate = h => {
      const d = h.days[h.days.length - 1];
      d.methodNote = 'не по текущей методике: отчёт ещё с деривативами, их PV не добавлен (FXB; отчёт от ' + d.lines[0].baseDate + ')';
      d.lines.filter(x => x.book === 'FXB').forEach(x => { x.baseKind = 'with_derivatives'; });
    };
    const { pg, ctx, errors, hist } = await open(browser, '/concepts/c16-ocp.html', { mutate });
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    const dr = pg.locator('.k-drawer');
    await dr.locator('[data-c16-sum]').first().waitFor();
    assert.match(await dr.locator('[data-c16-method-drill]').innerText(), /^не по текущей методике: отчёт ещё с деривативами/);
    const t = await dr.locator('[data-c16-reread]').innerText();
    for (const w of ['«Настройки»', '«Адаптер и источники»', '«Папки»', '«Перечитать отчёты за период»', '«Источник»', '«С даты»', '«По дату»', '«Перечитать»', '«История прогонов»'])
      assert.ok(t.includes(w), w + ' — ' + t);
    // в «С даты» и «По дату» — дата отчёта, от которого взята база, а не дата разбора
    const bd = hist.days[hist.days.length - 1].lines[0].baseDate, dmy = bd.slice(8, 10) + '.' + bd.slice(5, 7) + '.' + bd.slice(0, 4);
    assert.ok(t.includes('«С даты» и «По дату» (' + dmy), dmy + ' — ' + t);
    assert.ok(!BAD.test(await dr.innerText()));
    // день по текущей методике: ни пометки, ни пояснения
    await dr.locator('[data-c16-day]').selectOption(hist.days[hist.days.length - 5].date);
    await pg.waitForFunction(() => !document.querySelector('.k-drawer [data-c16-reread]'));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C16 date: a run of the old methodology and a part the run did not save say «нет данных в прогоне этой даты» with the reason (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const hist = history(), oldDay = hist.days[5], newDay = hist.days[22];
    const { pg, ctx, errors } = await open(browser, '/concepts/c16-ocp.html?date=' + oldDay.date, { dayAnswer: { [newDay.date]: json => json(dayDoc(newDay, { noContrib: true })) } });
    // старая методика: пометка дня на таблице, в разборе — только то, что есть, и чего нет
    assert.equal(await pg.locator('[data-c16-method-day]').count(), 1);
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    const dr = pg.locator('.k-drawer');
    await dr.locator('[data-c16-sum]').first().waitFor();
    assert.match(await dr.locator('[data-c16-method-drill]').innerText(), /^другая методика/);
    const nd = await dr.locator('[data-c16-nodata]').first().innerText();
    assert.ok(nd.startsWith('Нет данных в прогоне этой даты:') && nd.includes('прежней методике') && nd.includes('PV деривативов'), nd);
    assert.ok((await dr.locator('table[data-vk="c16-drill"]').first().innerText()).includes('отчёт ОВП'), 'то, что есть, показано');
    assert.ok((await dr.locator('[data-c16-sum]').first().innerText()).includes('= ячейке таблицы'));
    // день, у которого не сохранён состав слагаемого: цифры слагаемых есть, состава нет — названо
    await dr.locator('[data-c16-day]').selectOption(newDay.date);
    await pg.waitForFunction(d => (document.querySelector('.k-drawer .st') || {}).textContent.includes(d.slice(8, 10) + '.' + d.slice(5, 7)), newDay.date);
    await dr.locator('[data-c16-nodata]').first().waitFor();
    const nd2 = await dr.locator('[data-c16-nodata]').first().innerText();
    assert.ok(nd2.includes('Нет данных в прогоне этой даты') && nd2.includes('прогон не сохранил'), nd2);
    assert.equal(await dr.locator('[data-c16-method-drill]').count(), 0, 'день по текущей методике — без пометки');
    assert.ok(!BAD.test(await dr.innerText()));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C16 date: 390 px without a horizontal scroll, the date row in both themes (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const theme of ['light', 'dark']) {
      const { pg, ctx, errors, hist } = await open(browser, '/concepts/c16-ocp.html?date=' + history().days[10].date, { width: 390, theme });
      assert.ok(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'нет прокрутки вбок');
      assert.equal(await pg.locator('[data-c16-today]').count(), 1);
      assert.ok(!BAD.test(await pg.locator('#view').textContent()));
      assert.equal(hist.days.length, 25);
      assert.deepEqual(errors, []);
      await ctx.close();
    }
  } finally { await browser.close(); }
});

// the demo (no server): the same screen on any date of its history; an old demo day is of the old methodology and says what it lacks
test('C16 date, demo: the date field opens a past demo day; its breakdown names what the old methodology lacks (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const ctx = await browser.newContext({ viewport: { width: 1366, height: 900 } }), pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
    await pg.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.origin !== ORIGIN) return route.abort();
      if (u.pathname.startsWith('/api/') || u.pathname === '/concepts-run.js' || u.pathname === '/concepts-data.js') return route.fulfill({ status: 404, body: '' });
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
      return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c16-ocp.html?demo=1');
    await pg.waitForSelector('[data-c16-days]');
    await pg.waitForSelector('[data-ocp-total]');
    const asOf = await pg.evaluate(() => MOCK.asOf);
    const old = await pg.evaluate(a => { const t = new Date(a + 'T00:00:00Z'); t.setUTCDate(t.getUTCDate() - 80); while (!(t.getUTCDay() % 6)) t.setUTCDate(t.getUTCDate() - 1); return t.toISOString().slice(0, 10); }, asOf);
    const now = await pg.locator('[data-ocp-total] .v').innerText();
    await pg.locator('[data-c16-date]').fill(old);
    await pg.locator('[data-c16-date]').dispatchEvent('change');
    await pg.waitForSelector('[data-c16-today]');
    assert.equal(new URL(pg.url()).searchParams.get('date'), old);
    assert.notEqual(await pg.locator('[data-ocp-total] .v').innerText(), now, 'цифры того дня — свои');
    assert.equal(await pg.locator('[data-c16-method-day]').count(), 1, 'старый день: пометка методики');
    await pg.locator('[data-ocp-row]').first().locator('td').first().click();
    const dr = pg.locator('.k-drawer');
    await dr.locator('[data-c16-sum]').first().waitFor();
    assert.ok((await dr.locator('[data-c16-nodata]').first().innerText()).startsWith('Нет данных в прогоне этой даты:'));
    assert.ok((await dr.locator('[data-c16-sum]').first().innerText()).includes('= ячейке таблицы'));
    await pg.keyboard.press('Escape');
    // дата вне демо-истории: прогона нет — сказано
    await pg.locator('[data-c16-date]').fill('2025-01-15');
    await pg.locator('[data-c16-date]').dispatchEvent('change');
    await pg.locator('.k-empty').first().waitFor();
    assert.ok((await pg.locator('.k-empty').first().innerText()).includes('на эту дату нет сохранённого итогового прогона'));
    assert.ok(!BAD.test(await pg.locator('#view').textContent()));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

// the run is named by its cut: a past date with only the preliminary close, the page's own intraday run on its date; never «итоговый» for them
test('C16 date: the run is labelled by its cut — preliminary close, intraday, final (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const mutate = h => { h.days[20].cutType = 'eod_preliminary'; h.days[24].cutType = 'intraday'; h.days[24].runType = 'INTRADAY'; };
    const { pg, ctx, errors, hist } = await open(browser, '/concepts/c16-ocp.html', { mutate });
    // страница на прогоне внутри дня: подпись под датой — по нему
    const own = await pg.locator('[data-c16-dayinfo]').innerText();
    assert.ok(own.startsWith('прогон внутри дня #' + hist.days[24].runId) && !own.includes('итоговый') && !own.includes('INTRADAY'), own);
    assert.match(await pg.locator('[data-c16-day="' + hist.days[24].date + '"]').getAttribute('title'), /прогон внутри дня #/);
    // прошлая дата только с предварительным закрытием
    await pg.locator('[data-c16-day="' + hist.days[20].date + '"]').click();
    await pg.waitForSelector('[data-c16-run]');
    assert.equal(await pg.locator('[data-c16-run]').innerText(), 'предварительное закрытие #' + hist.days[20].runId);
    const info = await pg.locator('[data-c16-dayinfo]').innerText();
    assert.ok(info.startsWith('предварительное закрытие #') && !info.includes('итоговый'), info);
    // итоговое закрытие — «итоговый прогон»
    await pg.locator('[data-c16-day="' + hist.days[19].date + '"]').click();
    await pg.waitForFunction(() => /^итоговый прогон #/.test((document.querySelector('[data-c16-run]') || {}).textContent || ''));
    // разбор: шапка панели называет тот же вид прогона
    await pg.locator('[data-ocp-row="FXB"] td').first().click();
    await pg.locator('.k-drawer [data-c16-sum]').first().waitFor();
    assert.ok((await pg.locator('.k-drawer .st').innerText()).includes('итоговый прогон #' + hist.days[19].runId));
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

// a date after the page's run date (?date= or the field against its max): said, not a «сервер ответил 422»
test('C16 date: a date after the run date says so without asking the server (TDM#767)', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls } = await open(browser, '/concepts/c16-ocp.html?date=2026-12-31');
    const txt = await pg.locator('.k-empty').first().innerText();
    assert.ok(txt.includes('ОВП за 31.12.26 нет: дата позже даты прогона страницы (13.10.26)') && !txt.includes('422'), txt);
    assert.ok(!calls.some(c => c.startsWith('/api/v1/history/ocp/day')), calls.join());
    assert.equal(await pg.locator('[data-ocp-total]').count(), 0);
    await pg.locator('[data-c16-today]').click();
    await pg.waitForSelector('[data-ocp-total]');
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
