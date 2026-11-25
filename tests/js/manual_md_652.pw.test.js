// «Ручные рыночные данные» (C22, TDM#652) в браузере (Playwright; без него тесты пропускаются): экран открывается в
// демо (?demo=1) и с сервером (его ответы — те же правила, что у демо lib/manual-md.js, подставлены в запросы), правка
// ячейки и сохранение, ошибка ввода без запроса, копирование с прошлой даты, добавление имени, история имени, «Снять с
// даты», «только чтение» по роли, по адресу и по ответу 403, обе темы, 390 px без горизонтальной прокрутки страницы,
// ошибок страницы нет.
//   node --test tests/js/manual_md_652.pw.test.js
'use strict';
const test = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const { TEXT: RUN } = require('./fixtures/concepts_run');
const MD = require('../../prototypes/concepts/lib/manual-md.js');
const ROOT = path.join(__dirname, '..', '..', 'prototypes'), ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.jpg': 'image/jpeg' };
const BAD = /\bNaN\b|\bundefined\b|\bnull\b/;
const AS_OF = JSON.parse(RUN).asOf;
const BASE = '/api/v1/market-data/manual/cds';
const skip = pw ? false : 'нет Playwright';

// Страница на ORIGIN, сервер — в запросах. auth — ответ /api/v1/auth/state; forbid — ответ 403 на PUT и POST.
async function open(browser, { width = 1366, theme = 'dark', query = '', auth = { enabled: false, user: null, loopback: true }, forbid = false, demo = false } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 } }), pg = await ctx.newPage(), errors = [], calls = [];
  const store = MD.demoApi(AS_OF);
  await ctx.addInitScript(t => { try { localStorage.clear(); localStorage.setItem('concepts-theme', t); } catch (e) { } }, theme);
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', async route => {
    const req = route.request(), u = new URL(req.url()), json = (o, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(o) });
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/')) calls.push({ method: req.method(), url: u.pathname + u.search, body: req.postData() ? JSON.parse(req.postData()) : null });
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + RUN + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json(auth);
    if (u.pathname === BASE + '/curves') { const r = await store.list(u.searchParams.get('date')); return json(r.body, r.status); }
    if (u.pathname === BASE + '/history') { const r = await store.history(u.searchParams.get('name')); return json(r.body, r.status); }
    if (u.pathname === BASE + '/curve' && req.method() === 'PUT') {
      if (forbid) return json({ code: 'forbidden', message: 'нет прав' }, 403);
      const r = await store.put(JSON.parse(req.postData())); return json(r.body, r.status);
    }
    if (u.pathname === BASE + '/copy' && req.method() === 'POST') {
      if (forbid) return json({ code: 'forbidden', message: 'нет прав' }, 403);
      const r = await store.copy(JSON.parse(req.postData())); return json(r.body, r.status);
    }
    if (u.pathname.startsWith('/api/')) return route.fulfill({ status: 404, contentType: 'application/json', body: '{"detail":"нет в тесте"}' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/c22-manual-md.html' + (demo ? '?demo=1' : query));
  await pg.waitForSelector('#view .k-grid');
  await pg.waitForTimeout(300);
  return { pg, ctx, errors, calls };
}
const cell = (pg, name, field) => pg.locator('tr.c22-row', { hasText: name }).locator('input[data-f="' + field + '"]');
const rowOf = (pg, name) => pg.locator('tr.c22-row', { hasText: name });

test('C22 подсказывает валюту позиции и сохраняет новую кривую в этой валюте', { skip }, async () => {
  const browser = await pw.chromium.launch({ headless: true });
  try {
    const { pg, calls } = await open(browser);
    assert.match(await rowOf(pg, 'Эмитент Г').innerText(), /Валюта позиций: RUB/);
    assert.equal(await cell(pg, 'Эмитент Г', 'ccy').getAttribute('placeholder'), 'RUB');
    await cell(pg, 'Эмитент Г', '5Y').fill('135');
    await cell(pg, 'Эмитент Г', '5Y').press('Enter');
    await pg.waitForFunction(() => /Сохранено/.test(document.getElementById('msg').textContent));
    assert.equal(calls.find(c => c.method === 'PUT').body.ccy, 'RUB');
    await cell(pg, 'Эмитент Г', 'ccy').fill('USD');
    await cell(pg, 'Эмитент Г', 'ccy').press('Enter');
    await rowOf(pg, 'Эмитент Г').getByText('Валюта кривой подходит не всем позициям').waitFor();
  } finally {
    await browser.close();
  }
});
const msg = pg => pg.locator('#msg').innerText();

test('C22 с сервером: кривые на дату, правка ячейки, ошибка ввода без запроса, сохранение с историей, обе темы', { skip, timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const theme of ['dark', 'light']) {
      const { pg, ctx, errors, calls } = await open(browser, { theme });
      assert.ok(calls.some(c => c.method === 'GET' && c.url === BASE + '/curves?date=' + AS_OF), calls.map(c => c.url).join());
      assert.ok(calls.some(c => c.url === '/api/v1/auth/state'));
      const text = await pg.locator('#view').textContent();
      assert.ok(!BAD.test(text), text);
      for (const w of ['Как заданы котировки', 'par-спред', 'введено вручную', 'нет кривой на эту дату', 'Добавить имя', 'Скопировать с прошлой даты', '3M', '10Y', 'Возмещение, %']) assert.ok(text.includes(w), w);
      assert.equal(await pg.locator('tr.c22-row').count(), 4);  // три введённых имени и CDS-позиция прогона без кривой
      // CSV таблицы (как у всех таблиц мониторов): значения ячеек, не пустые поля ввода
      const csv = await pg.evaluate(() => KIT.tableCsv(document.querySelector('.c22-tbl'), {}));
      assert.ok(csv.includes('Имя референса,3M,6M,1Y,2Y,3Y,5Y,7Y,10Y,"Возмещение, %",Валюта,Действует с / кем,Действия'), csv);
      assert.ok(/Эмитент А,85,100,130,175,220,285,315,340,25,USD,"?действует с /.test(csv), csv);
      assert.equal(await pg.locator('#bar .big').innerText(), AS_OF.slice(8, 10) + '.' + AS_OF.slice(5, 7) + '.' + AS_OF.slice(0, 4));
      // «Эмитент А»: кривая с прошлой даты, не введённая на эту
      assert.match(await rowOf(pg, 'Эмитент А').locator('.c22-who').innerText(), /действует с .* \(с прошлой даты\)/);
      assert.equal(await cell(pg, 'Эмитент А', '5Y').inputValue(), '285');
      assert.equal(await cell(pg, 'Эмитент А', 'recovery').inputValue(), '25');
      assert.equal(await rowOf(pg, 'Эмитент А').locator('.c22-save').isVisible(), false, 'нет правки — нет кнопки «Сохранить»');
      // ошибка ввода: подсказка у ячейки, запроса на сервер нет
      const put = () => calls.filter(c => c.method === 'PUT');
      await cell(pg, 'Эмитент А', '5Y').fill('abc');
      assert.equal(await rowOf(pg, 'Эмитент А').locator('.c22-save').isVisible(), true);
      await cell(pg, 'Эмитент А', '5Y').press('Enter');
      assert.match(await msg(pg), /Не сохранено: 5Y: не число/);
      assert.equal(await cell(pg, 'Эмитент А', '5Y').evaluate(e => e.classList.contains('bad')), true);
      assert.equal(put().length, 0);
      // Esc возвращает сохранённое
      await cell(pg, 'Эмитент А', '5Y').press('Escape');
      assert.equal(await cell(pg, 'Эмитент А', '5Y').inputValue(), '285');
      // правка и Enter: PUT с телом из ячеек; кривая теперь «введена на эту дату»
      await cell(pg, 'Эмитент А', '5Y').fill('290,5');
      await cell(pg, 'Эмитент А', 'recovery').fill('40');
      await cell(pg, 'Эмитент А', '10Y').press('Enter');
      await pg.waitForFunction(() => /Сохранено/.test(document.getElementById('msg').textContent));
      assert.equal(put().length, 1);
      assert.deepEqual(put()[0].body, { name: 'Эмитент А', date: AS_OF, spreads_bp: { '3M': 85, '6M': 100, '1Y': 130, '2Y': 175, '3Y': 220, '5Y': 290.5, '7Y': 315, '10Y': 340 }, recovery: 0.4, ccy: 'USD' });
      assert.match(await rowOf(pg, 'Эмитент А').locator('.c22-who').innerText(), /введена на эту дату\s+demo/);
      assert.equal(await cell(pg, 'Эмитент А', '5Y').inputValue(), '290,5');
      // то же ещё раз — «без изменений», не новая ревизия
      await cell(pg, 'Эмитент А', '5Y').fill('290,5'); await cell(pg, 'Эмитент А', 'recovery').fill('40 %');
      assert.equal(await rowOf(pg, 'Эмитент А').locator('.c22-save').isVisible(), false);
      // история имени: ревизии, новые сверху, кто и когда
      await rowOf(pg, 'Эмитент А').locator('[data-a=hist]').click();
      await pg.waitForSelector('tr.c22-hist table tbody tr');
      assert.ok(calls.some(c => c.url.startsWith(BASE + '/history?name=')));
      const hist = await pg.locator('tr.c22-hist table tbody tr').allInnerTexts();
      assert.equal(hist.length, 2);
      assert.match(hist[0], new RegExp(AS_OF.slice(8, 10) + '\\.' + AS_OF.slice(5, 7) + '\\.' + AS_OF.slice(0, 4) + '\\s+demo, .*290,5.*40 %\\s+USD'));
      assert.match(hist[1], /285.*25 %\s+USD/);
      await rowOf(pg, 'Эмитент А').locator('[data-a=hist]').click();
      assert.equal(await pg.locator('tr.c22-hist').count(), 0);
      assert.equal(await pg.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      assert.deepEqual(errors, []);
      await ctx.close();
    }
  } finally { await browser.close(); }
});

test('C22: копирование с прошлой даты (выбранные имена и все), добавление имени, «Снять с даты», смена даты', { skip, timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { pg, ctx, errors, calls } = await open(browser);
    const post = () => calls.filter(c => c.method === 'POST');
    // все имена с кривой, введённой раньше даты: А, Б, В; выбрано одно — копируется оно
    assert.match(await pg.locator('[data-a=copy]').innerText(), /Скопировать с прошлой даты \(3\)/);
    await rowOf(pg, 'Эмитент Б').locator('[data-sel]').check();
    assert.match(await pg.locator('[data-a=copy]').innerText(), /Скопировать выбранные с прошлой даты \(1\)/);
    await pg.locator('[data-a=copy]').click();
    await pg.waitForFunction(() => /Скопировано кривых на/.test(document.getElementById('msg').textContent));
    assert.deepEqual(post()[0].body, { to_date: AS_OF, names: ['Эмитент Б'] });
    assert.match(await msg(pg), /Скопировано кривых на .*: 1/);
    assert.match(await rowOf(pg, 'Эмитент Б').locator('.c22-who').innerText(), /введена на эту дату/);
    assert.match(await pg.locator('[data-a=copy]').innerText(), /Скопировать с прошлой даты \(2\)/);
    await pg.locator('[data-a=copy]').click();
    await pg.waitForFunction(() => /: 2/.test(document.getElementById('msg').textContent));
    assert.deepEqual(post()[1].body, { to_date: AS_OF, names: ['Эмитент А', 'Эмитент В'] }, 'всегда явный список — число на кнопке');
    assert.equal(await pg.locator('[data-a=copy]').isDisabled(), true, 'копировать больше нечего');
    // добавить имя: строка без кривой, ввод спредов, сохранение
    await pg.fill('#addname', '  Эмитент   Д ');
    await pg.locator('[data-a=add]').click();
    assert.match(await msg(pg), /Имя добавлено/);
    assert.equal(await rowOf(pg, 'Эмитент Д').count(), 1);
    assert.equal(await pg.evaluate(() => document.activeElement.dataset.f), '5Y', 'фокус — в ячейке 5Y нового имени');
    await pg.keyboard.type('410'); await cell(pg, 'Эмитент Д', '3Y').fill('360'); await cell(pg, 'Эмитент Д', 'ccy').fill('eur');
    await cell(pg, 'Эмитент Д', 'ccy').press('Enter');
    await pg.waitForFunction(() => /Сохранено/.test(document.getElementById('msg').textContent));
    const last = calls.filter(c => c.method === 'PUT').pop().body;
    assert.deepEqual([last.name, last.spreads_bp['5Y'], last.spreads_bp['3Y'], last.recovery, last.ccy], ['Эмитент Д', 410, 360, null, 'EUR']);
    assert.equal(await cell(pg, 'Эмитент Д', 'ccy').inputValue(), 'EUR');
    // то же имя ещё раз — не дубль; пустое — ошибка
    await pg.fill('#addname', 'эмитент д'); await pg.locator('[data-a=add]').click();
    assert.match(await msg(pg), /уже в таблице/); assert.equal(await pg.locator('tr.c22-row').count(), 5);
    await pg.fill('#addname', '  '); await pg.locator('[data-a=add]').click();
    assert.match(await msg(pg), /введите имя/);
    // «Снять с даты»: два нажатия; имя остаётся в таблице без кривой, если оно в CDS-позициях, иначе уходит
    const clearBtn = rowOf(pg, 'Эмитент Д').locator('[data-a=clear]');
    await clearBtn.click();
    assert.equal(await clearBtn.innerText(), 'Подтвердить снятие');
    assert.equal(calls.filter(c => c.method === 'PUT' && c.body.clear).length, 0);
    await clearBtn.click();
    await pg.waitForFunction(() => /снято с/.test(document.getElementById('msg').textContent));
    assert.deepEqual(calls.filter(c => c.body && c.body.clear).pop().body, { name: 'Эмитент Д', date: AS_OF, clear: true });
    assert.equal(await rowOf(pg, 'Эмитент Д').count(), 0);
    // другая дата: до первого ввода кривых нет, остаются CDS-позиции прогона; копировать нечего
    await pg.fill('#pick', '2026-01-05'); await pg.dispatchEvent('#pick', 'change');
    await pg.waitForFunction(() => document.querySelector('#bar .big').textContent === '05.01.2026');
    assert.ok(calls.some(c => c.url === BASE + '/curves?date=2026-01-05'));
    assert.equal(await pg.locator('tr.c22-row').count(), 2);
    assert.equal(await pg.locator('[data-a=copy]').isDisabled(), true);
    assert.ok(pg.url().includes('on=2026-01-05'));
    await pg.locator('#bar [data-d="1"]').click();
    await pg.waitForFunction(() => document.querySelector('#bar .big').textContent === '06.01.2026');
    assert.deepEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('C22 только чтение: роль «просмотр», вход выключен и открыто не с компьютера сервера, ответ 403 на правку', { skip, timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const viewer = await open(browser, { auth: { enabled: true, user: { username: 'vic', role: 'viewer' } } });
    assert.match(await viewer.pg.locator('#ro').innerText(), /Только чтение: ваша роль — «просмотр»/);
    assert.equal(await viewer.pg.locator('input[data-f]').count(), 0);
    assert.equal(await viewer.pg.locator('[data-a=add], [data-a=copy], [data-a=save], [data-a=clear]').count(), 0);
    assert.match(await rowOf(viewer.pg, 'Эмитент А').innerText(), /285/);  // значения видны
    await rowOf(viewer.pg, 'Эмитент А').locator('[data-a=hist]').click();  // история доступна всем ролям
    await viewer.pg.waitForSelector('tr.c22-hist table tbody tr');
    assert.deepEqual(viewer.errors, []);
    await viewer.ctx.close();

    const net = await open(browser, { auth: { enabled: false, user: null, loopback: false } });
    assert.match(await net.pg.locator('#ro').innerText(), /127\.0\.0\.1/);
    assert.equal(await net.pg.locator('input[data-f]').count(), 0);
    await net.ctx.close();

    // сервер отказал на сохранении (роль сменили, сессия другая): страница сама переходит в «только чтение»
    const forbid = await open(browser, { forbid: true, auth: { enabled: true, user: { username: 'tom', role: 'trader' } } });
    assert.equal(await forbid.pg.locator('#ro').count(), 0);
    await cell(forbid.pg, 'Эмитент А', '5Y').fill('300');
    await cell(forbid.pg, 'Эмитент А', '5Y').press('Enter');
    await forbid.pg.waitForSelector('#ro');
    assert.match(await forbid.pg.locator('#ro').innerText(), /сервер отказал в правке/);
    assert.equal(await forbid.pg.locator('input[data-f]').count(), 0);
    assert.deepEqual(forbid.errors, []);
    await forbid.ctx.close();
  } finally { await browser.close(); }
});

test('C22 в демо (?demo=1): вымышленные имена, правка в памяти страницы, 390 px без горизонтальной прокрутки', { skip, timeout: 120000 }, async () => {
  const browser = await pw.chromium.launch();
  try {
    for (const width of [390, 1366]) {
      const { pg, ctx, errors, calls } = await open(browser, { demo: true, width });
      assert.equal(calls.filter(c => c.url.startsWith(BASE)).length, 0, 'демо не ходит на сервер');
      const text = await pg.locator('#view').textContent();
      assert.ok(text.includes('демо: вымышленные имена') && !BAD.test(text));
      await cell(pg, 'Эмитент Б', '3M').fill('200');
      await cell(pg, 'Эмитент Б', '3M').press('Enter');
      await pg.waitForFunction(() => /Сохранено/.test(document.getElementById('msg').textContent));
      assert.equal(await cell(pg, 'Эмитент Б', '3M').inputValue(), '200');
      assert.match(await rowOf(pg, 'Эмитент Б').locator('.c22-who').innerText(), /введена на эту дату\s+demo/);
      // страница не шире окна; таблица прокручивается внутри своей обёртки
      const m = await pg.evaluate(() => ({ page: document.documentElement.scrollWidth - innerWidth, wrap: (w => w.scrollWidth - w.clientWidth)(document.querySelector('.c22-tbl-wrap')) }));
      assert.ok(m.page <= 0, 'горизонтальная прокрутка страницы на ' + width + ' px: ' + m.page);
      if (width === 390) assert.ok(m.wrap > 0, 'таблица шире окна и прокручивается внутри');
      assert.deepEqual(errors, []);
      await ctx.close();
    }
  } finally { await browser.close(); }
});
