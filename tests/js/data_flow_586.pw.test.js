// C21 «Схема данных» (TDM#586) и C20 «Сделки дня» под фильтром шапки (TDM#585) в браузере (Playwright; без него тесты
// пропускаются): прогон из tests/js/fixtures/concepts_run.js, ответы /api/v1/dataflow, /api/v1/inbox/days, /api/v1/trades и
// /api/v1/trades/summary — синтетические. C21: блоки трёх столбцов со статусами словами, красная стрелка на каждую
// неполную связь, «Где данные неполные», «Что впереди»; обе темы; 1024 px — схема со стрелками, 390 px — один столбец без
// горизонтальной прокрутки. C20: фильтр шапки скрывает сделки других книг, у сделки — источник и время получения.
// C01: строка сделок дня по книгам шапки со ссылкой на C20.
//   node --test tests/js/data_flow_586.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.jpg': 'image/jpeg' };
const BAD = /\bNaN\b|\bundefined\b|\bnull\b/;
const AS_OF = JSON.parse(RUN).asOf;

const L = (t, at, tone) => ({ t, at: at || null, tone: tone || null });
const B = (id, column, title, status, extra) => Object.assign({ id, column, title, what: 'что делает ' + title, status, status_text: { ok: 'готово', running: 'идёт', waiting: 'ждёт', error: 'ошибка', off: 'выключено' }[status],
  got: [L('получено ' + id, AS_OF + 'T10:00:00+00:00')], dropped: [], waits: null, progress: null, at: null, link: { href: 'c14-day-status.html', text: 'Статус дня' }, fix: null, fix_note: null }, extra || {});
const FLOW = {
  date: AS_OF, generated_at: AS_OF + 'T12:00:00+00:00', statuses: { ok: 'готово', running: 'идёт', waiting: 'ждёт', error: 'ошибка', off: 'выключено' },
  columns: [{ id: 'source', title: 'Откуда приходят данные' }, { id: 'process', title: 'Что с ними делается' }, { id: 'result', title: 'Где это видно' }],
  run: { run_id: 7, label: '13.10 · закрытие', is_default: true }, day_state: 'warn',
  blocks: [
    B('positions', 'source', 'Отчёт о позициях', 'ok', { fix: { href: '../settings.html#adapter/folders', text: 'Папки с отчётами' } }),
    B('cash', 'source', 'Остатки на счетах', 'waiting', { waits: 'остатки на счетах за 13.10', got: [] }),
    B('past_trades', 'source', 'Сделки прошлых дат', 'running', { progress: { done: 30, total: 120, text: 'дат обработано' } }),
    B('pnl_official', 'source', 'Официальный PnL', 'off', { got: [L('адаптер этот отчёт не даёт')] }),
    B('read', 'process', 'Чтение отчётов адаптером', 'error', { dropped: [L('файл.csv: отклонён', null, 'bad')] }),
    B('ocp', 'process', 'Расчёт ОВП', 'ok', { dropped: [L('на входе неполные данные: ОВП посчитана без остатков на счетах', null, 'warn')] }),
    B('backfill', 'process', 'Пересчёт прошлых дат', 'waiting', { waits: 'очередь: 3 дат', progress: { done: 7, total: 10, text: 'дат посчитано' } }),
    B('r_ocp', 'result', 'ОВП', 'ok'), B('r_history', 'result', 'История и PnL за период', 'ok')],
  edges: [{ from: 'positions', to: 'read', what: 'отчёты о позициях', incomplete: false, why: null },
    { from: 'cash', to: 'ocp', what: 'остатки на счетах', incomplete: true, why: 'ОВП посчитана без остатков на счетах' },
    { from: 'past_trades', to: 'backfill', what: 'сделки прошлых дат', incomplete: false, why: null },
    { from: 'read', to: 'ocp', what: 'строки', incomplete: false, why: null },
    { from: 'ocp', to: 'r_ocp', what: 'ОВП', incomplete: true, why: 'ОВП посчитана без остатков на счетах' },
    { from: 'backfill', to: 'r_history', what: 'прогоны', incomplete: false, why: null }],
  counts: { ok: 4, waiting: 2, running: 1, error: 1, off: 1 }, incomplete: 2,
  ahead: [{ id: 'backfill', title: 'Пересчёт прошлых дат', text: 'осталось дат: 3', at: null, eta_s: 600, progress: { done: 7, total: 10, text: 'дат посчитано' } },
    { id: 'wait:cash', title: 'Ждём: Остатки на счетах', text: 'остатки на счетах за 13.10', at: null, eta_s: null }],
  autopilot: { state: 'idle', enabled: true, message: null },
};
const DAYS = { days: [{ date: AS_OF, state: 'warn', default_run_id: 7, runs: 1 }] };
const row = (id, book, extra) => Object.assign({ trade_id: id, trade_version: 1, status: 'live', trade_date: AS_OF, trade_ts: AS_OF + 'T11:20:00', book_id: book, position_id: book + '|S1', asset_class: 'bond',
  source_instrument_id: 'S1', source_instrument_name: 'Бумага ' + id, side: 'buy', quantity: 10, price_pct: 99.5, ccy: 'RUB', late: false, intraday: 'applied', pnl_rub: 1000, pnl_flag: null,
  origin: 'feed', received_at: AS_OF + 'T08:21:00+00:00', entity: 'Головной банк', desk: 'Rates', portfolio: null }, extra || {});
const TRADES = {
  date: AS_OF, book_id: null, rows: [row('TR-1', 'OFZ'), row('TR-2', 'RATES', { intraday: 'not_bond', asset_class: 'irs', pnl_rub: null, origin: 'file' }), row('TR-3', 'CORP_RUB', { desk: 'Credit', intraday: 'no_instrument', pnl_rub: null })],
  books: [], summary: { trades: 3, live: 3, cancelled: 0, late: 0, books: 3, versions: 0 }, files: [], days: [{ date: AS_OF, trades: 3, cancelled: 0 }],
  intraday: { run_id: 7, run_type: 'EOD', applied: 1, new_positions: 0, skipped: {} },
};
const SUMMARY = { date: AS_OF, books: null, run_id: 7, trades: 3, live: 3, cancelled: 0, applied: 1, problems: 1, last_trade_ts: AS_OF + 'T11:20:00',
  states: [{ state: 'applied', text: 'в позициях', n: 1 }, { state: 'not_bond', text: 'не облигация — в позиции дня пока не входит', n: 1 }, { state: 'no_instrument', text: 'бумага не найдена', n: 1 }],
  sources: [{ origin: 'feed', text: 'фид сервисов адаптера', trades: 2, last_at: AS_OF + 'T08:21:00+00:00' }], by_book: {}, top: [], state_text: {} };

async function open(browser, page, { width = 1366, theme = 'dark', store = {}, ready = '#view .k-grid' } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  await ctx.addInitScript(([t, st]) => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); Object.entries(st).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, [theme, store]);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const u = new URL(route.request().url()), json = o => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/')) calls.push(u.pathname + u.search);
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json({ enabled: false, user: null });
    if (u.pathname === '/api/v1/dataflow') return json(FLOW);
    if (u.pathname === '/api/v1/inbox/days') return json(DAYS);
    if (u.pathname === '/api/v1/trades') return json(TRADES);
    if (u.pathname === '/api/v1/trades/summary') return json(SUMMARY);
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + page);
  await pg.waitForSelector(ready);
  await pg.waitForTimeout(400);
  return { pg, ctx, errors, calls };
}

test('C21: blocks with statuses in words, red arrows where incomplete data went on, both themes, 1024 and 390 px', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const theme of ['dark', 'light']) for (const width of [1024, 1440]) {
      const { pg, ctx, errors, calls } = await open(browser, 'c21-data-flow.html', { width, theme });
      assert.ok(calls.includes('/api/v1/dataflow'), calls.join());
      assert.ok(!calls.some(c => /hub|adapter-services|history/.test(c)), 'экран не спрашивает хаб и сервисы: ' + calls.join());
      const text = await pg.locator('#view').textContent();
      assert.ok(!BAD.test(text), text);
      for (const w of ['готово', 'идёт', 'ждёт', 'ошибка', 'выключено', 'Где данные неполные', 'Что впереди', 'осталось ≈ 10 мин']) assert.ok(text.includes(w), w);
      assert.equal(await pg.locator('.c21-b').count(), FLOW.blocks.length);
      assert.equal(await pg.locator('.c21-col').count(), 3);
      // стрелки — между столбцами, красные — неполные; блоки трёх столбцов стоят рядом
      assert.equal(await pg.locator('.c21-svg path[data-from]').count(), 5);
      assert.equal(await pg.locator('.c21-svg path.bad').count(), 2);
      const cols = await pg.$$eval('.c21-col', l => l.map(c => Math.round(c.getBoundingClientRect().left)));
      assert.ok(cols[0] < cols[1] && cols[1] < cols[2], cols.join());
      assert.equal(await pg.locator('#b-positions a[href="../settings.html#adapter/folders"]').count(), 1, 'настройка блока');
      assert.equal(await pg.locator('#b-cash .wait').innerText(), 'ждёт: остатки на счетах за 13.10');
      assert.ok((await pg.locator('#b-past_trades .c21-prog .t').innerText()).startsWith('30 из 120'));
      // наведение на блок подсвечивает его связи
      await pg.hover('#b-ocp');
      assert.equal(await pg.locator('.c21-svg path.hl').count(), 2);
      assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await ctx.close();
    }
    const { pg, ctx, errors } = await open(browser, 'c21-data-flow.html', { width: 390 });
    assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'без горизонтальной прокрутки');
    assert.equal(await pg.evaluate(() => getComputedStyle(document.querySelector('.c21-svg')).display), 'none');
    const lefts = await pg.$$eval('.c21-col', l => l.map(c => Math.round(c.getBoundingClientRect().left)));
    assert.ok(lefts.every(x => x === lefts[0]), 'столбцы — один под другим: ' + lefts.join());
    assert.ok((await pg.locator('#b-cash .io').innerText()).includes('ОВП посчитана без остатков на счетах'), 'неполная связь — словами в блоке');
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C20 follows the header filter, shows the source and receipt time; C01 shows the day trades line', { skip: pw ? false : 'нет Playwright', timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    let r = await open(browser, 'c20-trades.html');
    const ids = () => r.pg.$$eval('table[data-vk="c20-trades"] tbody tr td.mono', l => l.map(x => x.textContent));
    assert.deepEqual(await ids(), ['TR-1', 'TR-2', 'TR-3']);
    const text = await r.pg.locator('#view').textContent();
    assert.ok(text.includes('фид сервисов адаптера') && text.includes('из папки с отчётами') && text.includes('Не в позициях — почему'), text);
    assert.ok(text.includes('бумага не найдена') && text.includes('не облигация'));
    assert.equal(await r.pg.locator('.k-top .filters [data-nofilter]').count(), 0, 'чипы шапки, а не «фильтр здесь не действует»');
    assert.deepEqual(r.errors, []);
    await r.ctx.close();
    r = await open(browser, 'c20-trades.html', { store: { 'concepts-filter': JSON.stringify({ books: ['OFZ', 'CORP_RUB'] }) } });
    assert.deepEqual(await ids(), ['TR-1', 'TR-3']);
    assert.ok((await r.pg.locator('#view').innerText()).includes('под фильтром шапки: 2 из 3'));
    await r.ctx.close();
    r = await open(browser, 'c20-trades.html?pos=OFZ%7CS1');
    assert.deepEqual(await ids(), ['TR-1']);
    await r.ctx.close();
    r = await open(browser, 'c01-cockpit.html', { ready: '#trades .k-dt b', store: { 'concepts-filter': JSON.stringify({ books: ['OFZ'] }) } });
    const sum = r.calls.find(c => c.startsWith('/api/v1/trades/summary'));
    assert.ok(sum && /books=OFZ/.test(sum) && new RegExp('date=' + AS_OF).test(sum), sum);
    const line = await r.pg.locator('#trades').innerText();
    assert.ok(line.includes('Сделки дня: 3, в позициях 1, последняя в 11:20'), line);
    assert.equal(await r.pg.locator('#trades a[href^="c20-trades.html?date=' + AS_OF + '"]').count(), 1);
    assert.deepEqual(r.errors, []);
    await r.ctx.close();
  } finally { await browser.close(); }
});
