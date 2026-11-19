// Формат чисел колонки таблиц позиций (TDM#789): K.colFmt (выбор пользователя: знаки после точки и единицы; сервер по пользователю,
// копия в браузере), K.NF / K.nfNum / K.nfCol (описание числа колонки и число в выбранном формате), K.table (ячейки, итоги, подпись
// единицы, CSV — одни и те же числа; подсветка и сортировка — по исходному числу), K.posCols (формат по умолчанию совпадает с прежним).
// Меню «Колонки ▾» с кнопкой формата в браузере и C03 — tests/js/col_format_789.pw.test.js.
//   node --test tests/js/col_format_789.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const same = (a, b, m) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), m);  // объекты из vm — другого realm

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
const LS = store => ({ getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) });
// real: страница открыта через сервер (M.source === 'real'): запросы идут в fetch — stub функцией (url, init) → {status, body}
function load(store, { real, fetch, ls } = {}) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const calls = [];
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage: ls || LS(store), getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  if (fetch) ctx.fetch = (url, init) => { calls.push({ url, method: (init && init.method) || 'GET', body: init && init.body, headers: init && init.headers });
    return Promise.resolve(fetch(url, init || {})).then(r => ({ ok: r.status >= 200 && r.status < 300, status: r.status, json: () => Promise.resolve(r.body) })); };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  if (real) ctx.window.MOCK.source = 'real';
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, calls };
}
const tick = () => new Promise(r => setImmediate(r));
const KEY = 'tdm.view.v1:colfmt';
const API = '../api/v1/me/column-formats';

// ---------- выбор пользователя: K.colFmt ----------

test('K.colFmt без сервера (демо, файл): формат — в браузере, запросов нет, чтение и запись в try/catch', async () => {
  const store = new Map(), { K, calls } = load(store, { fetch: () => { throw new Error('нет запроса без сервера'); } });
  assert.strictEqual(K.colFmt.get('mv'), null, 'по умолчанию формата нет');
  assert.strictEqual(K.colFmt.where(), 'browser');
  await K.colFmt.set('mv', { decimals: 3, scale: 'bn' });
  same(K.colFmt.get('mv'), { decimals: 3, scale: 'bn' });
  same(JSON.parse(store.get(KEY)), { mv: { decimals: 3, scale: 'bn' } });
  assert.strictEqual(calls.length, 0, 'в демо запросов к серверу нет');
  // каждая часть отдельно; мусор отбрасывается, ничего не выбрано — по умолчанию
  await K.colFmt.set('dv01', { decimals: 0 });
  same(K.colFmt.get('dv01'), { decimals: 0, scale: null });
  await K.colFmt.set('carry', { decimals: 9, scale: 'tys' });
  assert.strictEqual(K.colFmt.get('carry'), null, 'знаков больше 6 и неизвестные единицы — не формат');
  await K.colFmt.set('carry', { decimals: 1.5, scale: 'k' });
  same(K.colFmt.get('carry'), { decimals: null, scale: 'k' }, 'знаки — целое 0…6; годная часть остаётся');
  await K.colFmt.set('bad key', { decimals: 1 });
  assert.strictEqual(K.colFmt.get('bad key'), null, 'ключ колонки — как принимает сервер');
  // «по умолчанию» колонки: запись убирается
  await K.colFmt.reset('dv01');
  assert.strictEqual(K.colFmt.get('dv01'), null);
  await K.colFmt.set('mv', { decimals: null, scale: null });
  assert.strictEqual(K.colFmt.get('mv'), null);
  same(Object.keys(JSON.parse(store.get(KEY))), ['carry']);
  await K.colFmt.reset('carry');
  assert.ok(!store.has(KEY), 'нет формата — нет записи');
  // страница после перезагрузки: формат — из браузера
  store.set(KEY, '{"mv":{"decimals":2,"scale":"mm"},"dv01":{"decimals":9},"x y":{"decimals":1},"carry":5,"price":null}');
  const k2 = load(store).K;
  same(k2.colFmt.get('mv'), { decimals: 2, scale: 'mm' });
  assert.strictEqual(k2.colFmt.get('dv01'), null, 'чужое значение — по умолчанию');
  for (const raw of ['5', '"abc"', '[]', 'not json', '{"mv":"x"}']) {
    store.set(KEY, raw);
    assert.strictEqual(load(store).K.colFmt.get('mv'), null, raw);
  }
  // хранилище бросает (приватное окно): формат живёт до перезагрузки, ошибок нет
  const no = () => { throw new Error('denied'); };
  const { K: K3 } = load(null, { ls: { getItem: no, setItem: no, removeItem: no } });
  await assert.doesNotReject(async () => K3.colFmt.set('mv', { decimals: 4 }));
  same(K3.colFmt.get('mv'), { decimals: 4, scale: null });
});

test('K.colFmt с сервером: ответ — слово сервера, правка — PUT, «по умолчанию» — DELETE, по очереди', async () => {
  const store = new Map([[KEY, '{"mv":{"decimals":1,"scale":null},"dv01":{"decimals":4,"scale":null}}']]);
  const srv = { mv: { decimals: 3, scale: 'bn', updated_at: '2026-11-19T10:00:00+00:00' } }, log = [];
  const { K, calls } = load(store, { real: true, fetch: (url, init) => {
    log.push((init.method || 'GET') + ' ' + url);
    if (!init.method) return { status: 200, body: { user: 'ann', formats: srv } };
    const col = decodeURIComponent(url.split('/').pop());
    if (init.method === 'PUT') srv[col] = JSON.parse(init.body); else delete srv[col];
    return { status: init.method === 'PUT' ? 200 : 204, body: {} };
  } });
  const seen = []; K.on('colfmt', () => seen.push(JSON.stringify(K.colFmt.get('mv'))));
  same(K.colFmt.get('mv'), { decimals: 1, scale: null }, 'до ответа сервера — копия браузера (страница рисуется сразу)');
  await tick(); await tick();
  assert.strictEqual(calls[0].url, API);
  same(seen, ['{"decimals":3,"scale":"bn"}'], 'пришёл ответ — событие, таблицы перерисуются');
  assert.strictEqual(K.colFmt.get('dv01'), null, 'сервер ответил без формата — по умолчанию, а не копия браузера (другой пользователь того же браузера)');
  same(JSON.parse(store.get(KEY)), { mv: { decimals: 3, scale: 'bn' } }, 'копия в браузере обновлена ответом');
  // правка: PUT с JSON, видна сразу; три быстрых правки одной колонки — по очереди
  K.colFmt.set('mv', { decimals: 0 }); K.colFmt.set('mv', { decimals: 2, scale: 'k' });
  const last = K.colFmt.set('mv', { scale: 'mm' });
  same(K.colFmt.get('mv'), { decimals: null, scale: 'mm' }, 'формат виден сразу, не ждёт сервера');
  assert.strictEqual(await last, true);
  const puts = calls.filter(c => c.method === 'PUT');
  assert.deepStrictEqual(puts.map(c => c.url), Array(3).fill(API + '/mv'));
  same(puts.map(c => JSON.parse(c.body)), [{ decimals: 0, scale: null }, { decimals: 2, scale: 'k' }, { decimals: null, scale: 'mm' }]);
  assert.strictEqual(puts[0].headers['Content-Type'], 'application/json');
  same(srv.mv, { decimals: null, scale: 'mm' });
  // «по умолчанию» — DELETE, ключ кодируется
  await K.colFmt.set('Z.kbd', { decimals: 1 });
  await K.colFmt.reset('Z.kbd');
  const del = calls.filter(c => c.method === 'DELETE');
  assert.strictEqual(del.length, 1); assert.strictEqual(del[0].url, API + '/Z.kbd');
  assert.strictEqual(del[0].headers['Content-Type'], 'application/json', 'DELETE тоже с JSON: так требует сервер');
  assert.ok(!('Z.kbd' in srv));
  assert.strictEqual(K.colFmt.where(), 'server');
  // не правилось здесь — слово сервера главное и дальше
  assert.strictEqual(calls.filter(c => !c.method || c.method === 'GET').length, 1, 'формат с сервера читается один раз за страницу');
});

test('K.colFmt: сервер не ответил или не принял запись — выбор остаётся на экране и в браузере, уходит при следующей загрузке', async () => {
  const store = new Map([[KEY, '{"mv":{"decimals":2,"scale":null}}']]);
  const { K } = load(store, { real: true, fetch: () => ({ status: 503, body: {} }) });
  K.colFmt.get('mv');
  await tick(); await tick();
  same(K.colFmt.get('mv'), { decimals: 2, scale: null }, 'копия браузера');
  assert.strictEqual(K.colFmt.where(), 'offline');
  assert.strictEqual(await K.colFmt.set('mv', { decimals: 5 }), false, 'сервер не принял');
  same(K.colFmt.get('mv'), { decimals: 5, scale: null }, 'выбор на экране не потерян');
  same(JSON.parse(store.get(KEY)), { mv: { decimals: 5, scale: null } });
  same(JSON.parse(store.get('tdm.view.v1:colfmt-dirty')), { ids: ['mv'] }, 'помнится как не отправленное');
  // следующая загрузка страницы: сервер ожил — не принятая правка уходит раньше, чем приходит его ответ, и не перекрывается им
  const srv = { mv: { decimals: 1, scale: 'bn' } }, calls = [];
  const next = load(store, { real: true, fetch: (url, init) => {
    calls.push((init.method || 'GET') + ' ' + url);
    if (!init.method) return { status: 200, body: { user: 'ann', formats: srv } };
    if (init.method === 'PUT') Object.assign(srv, { [url.split('/').pop()]: JSON.parse(init.body) });
    return { status: 200, body: {} };
  } }).K;
  next.colFmt.get('mv');
  await tick(); await tick(); await tick();
  assert.deepStrictEqual(calls, ['PUT ' + API + '/mv', 'GET ' + API]);
  same(next.colFmt.get('mv'), { decimals: 5, scale: null }, 'слово пользователя, а не старое слово сервера');
  assert.ok(!store.has('tdm.view.v1:colfmt-dirty'), 'принято — «не отправлено» снято');
  // не правилось, сервер ответил мусором — по умолчанию, ошибок нет
  const junk = load(new Map(), { real: true, fetch: () => ({ status: 200, body: { formats: { mv: { decimals: 99, scale: 'x' }, 'bad key': { decimals: 1 }, dv01: { decimals: 2, scale: null } } } }) }).K;
  junk.colFmt.get('mv'); await tick(); await tick();
  assert.strictEqual(junk.colFmt.get('mv'), null); assert.strictEqual(junk.colFmt.get('bad key'), null);
  same(junk.colFmt.get('dv01'), { decimals: 2, scale: null });
});

// ---------- число колонки: K.NF, K.nfNum, K.nfEff ----------

test('K.nfNum: формат по умолчанию совпадает с прежним у каждой настраиваемой колонки posCols', () => {
  const { K } = load(new Map());
  const f = K.f, v = 1234567.891, n = -9876.543;
  // [id, значение, прежний текст]: что показывала колонка до TDM#789
  const was = {
    notional: x => f.mn(x, 2), mv: x => f.mn(x, 0), tradeDay: x => f.mns(x, 2), posDelta: x => f.mns(x, 2), price: f.px, ytm: x => f.pct(x),
    spread: f.bp, zkbd: f.bp, zdisc: f.bp, gspread: f.bp, dur: x => f.n(x, 2),
    dv01: f.dv, cs01: f.dv, basis01: f.dv, ofzSpr01: f.dv, carry: f.k, funding: f.k, pnl: f.k, pnlMtd: x => f.mn(x, 1),
    // колонки своих экранов (TDM#789, C09, C10, C17): прежний вид — как строили их страницы
    fwdCarry: x => f.mn(x, 2), fwdFunding: x => f.mn(x, 2), fwdNet: x => f.mn(x, 2), carryDv: f.bp, mktSpread: f.bp, rvDiff: f.bps,
    unexplShare: x => f.pct(x == null ? x : x * 100, 0),
  };
  assert.deepStrictEqual(Object.keys(K.NF).sort(), Object.keys(was).sort(), 'у каждой настраиваемой колонки известен прежний вид');
  for (const [id, fn] of Object.entries(was)) {
    for (const x of [v, n, 0.4, 0, 12.5, null, NaN]) assert.strictEqual(K.nfNum(id, x), fn(x), id + ' ' + x);
  }
  // каждая колонка K.posCols с nf — в K.NF под своим ключом
  for (const [id, c] of Object.entries(K.posCols)) if (c.nf) assert.strictEqual(c.nf, K.NF[id], id);
  assert.strictEqual(K.NF.mv.id, 'mv');
  // единицы по умолчанию — те, что были в заголовках
  const unit = { notional: 'mm в вал.', mv: 'mm ₽', tradeDay: 'mm в вал.', posDelta: 'mm в вал.', dv01: 'тыс ₽/бп', cs01: 'тыс ₽/бп', basis01: 'тыс ₽/бп', ofzSpr01: 'тыс ₽/бп', carry: 'тыс ₽', funding: 'тыс ₽', pnl: 'тыс ₽', pnlMtd: 'mm ₽', fwdCarry: 'mm ₽', fwdFunding: 'mm ₽', fwdNet: 'mm ₽' };
  for (const [id, u] of Object.entries(unit)) assert.strictEqual(K.nfEff(id).unit, u, id);
  for (const id of ['price', 'ytm', 'spread', 'zkbd', 'zdisc', 'gspread', 'dur', 'carryDv', 'mktSpread', 'rvDiff', 'unexplShare']) assert.strictEqual(K.nfEff(id).units, false, id + ': только знаки');
});

test('K.nfNum: колонки экранов C09, C10, C17 — свой ключ формата; «+» вопреки описанию колонки (PnL в C10); доля — проценты', async () => {
  const { K } = load(new Map());
  // sign: PnL в C10 — всегда со знаком, а у «PnL дня» таблиц позиций «+» нет; формат (знаки, единицы) у них один
  assert.strictEqual(K.nfNum('pnl', 2500), '3'); assert.strictEqual(K.nfNum('pnl', 2500, false, true), '+3');
  assert.strictEqual(K.nfNum('pnl', 2500, true, true), '3', 'CSV — без «+»');
  assert.strictEqual(K.nfFn('pnl', true)(-2500), '(3)'); assert.strictEqual(K.nfFn('pnlMtd', true)(2500000), '+2.5');
  assert.strictEqual(K.nfNum('rvDiff', 366), '+366', '«разница» C09 — со знаком по описанию'); assert.strictEqual(K.nfNum('rvDiff', 366, true), '366');
  await K.colFmt.set('pnl', { scale: 'mm' });
  assert.strictEqual(K.nfNum('pnl', 2500000, false, true), '+2.500', 'единицы PnL — те же в C10: тыс → mm, точность та же (на три знака больше)');
  await K.colFmt.reset('pnl');
  // доля — число 0…1, на экране проценты; формат только знаки
  assert.strictEqual(K.nfNum('unexplShare', 0.456), '46%'); assert.strictEqual(K.nfNum('unexplShare', null), '—');
  await K.colFmt.set('unexplShare', { decimals: 1 }); assert.strictEqual(K.nfNum('unexplShare', 0.456), '45.6%');
  // прогнозное кэрри C17: знаки по умолчанию 2, в тыс ₽ — на три знака меньше; три колонки — три формата
  await K.colFmt.set('fwdCarry', { scale: 'k' });
  assert.strictEqual(K.nfNum('fwdCarry', 1234567), '1,235'); assert.strictEqual(K.nfNum('fwdNet', 1234567), '1.23');
  assert.strictEqual(K.nfEff('fwdCarry').unit, 'тыс ₽'); assert.strictEqual(K.nfEff('fwdNet').unit, 'mm ₽');
  assert.strictEqual(K.nfEff('carryDv').units, false);
  // название CSV — строка или функция (единицы колонок в названии)
  assert.strictEqual(K.csvTitle({ title: 'Таблица' }), 'Таблица'); assert.strictEqual(K.csvTitle({ title: () => 'Из функции, ' + K.nfEff('fwdCarry').unit }), 'Из функции, тыс ₽');
});

test('K.nfNum: знаки и единицы пользователя; null — «—»; отрицательные в обоих форматах; сменили единицу — точность та же', async () => {
  const { K } = load(new Map());
  await K.colFmt.set('notional', { decimals: 3 });
  assert.strictEqual(K.nfNum('notional', 1234567.891), '1.235', '3 знака у номинала');
  assert.strictEqual(K.nfNum('notional', null), '—');
  await K.colFmt.reset('notional');
  assert.strictEqual(K.nfNum('notional', 1234567.891), '1.23');
  // единицы: как есть / тыс / mm / bn; знаки по умолчанию — те же цифры (тысячу раз крупнее — на три знака больше)
  await K.colFmt.set('dv01', { scale: '1' });  assert.strictEqual(K.nfNum('dv01', 12345.6), '12,346');
  await K.colFmt.set('dv01', { scale: 'k' });  assert.strictEqual(K.nfNum('dv01', 12345.6), '12');
  await K.colFmt.set('dv01', { scale: 'mm' }); assert.strictEqual(K.nfNum('dv01', 12345.6), '0.012', 'тыс → mm: 3 знака вместо 0');
  await K.colFmt.set('dv01', { scale: 'bn' }); assert.strictEqual(K.nfNum('dv01', 12345.6), '0.000012', 'тыс → bn: до 6 знаков');
  assert.strictEqual(K.nfEff('dv01').d, 6);
  await K.colFmt.set('dv01', { scale: 'bn', decimals: 2 }); assert.strictEqual(K.nfNum('dv01', 1.5e9), '1.50');
  await K.colFmt.set('mv', { scale: 'k' });    assert.strictEqual(K.nfNum('mv', 1234567.891), '1,235', 'mm → тыс: целые, а не 0 знаков при 1.2');
  await K.colFmt.set('mv', { scale: '1' });    assert.strictEqual(K.nfNum('mv', 1234567.891), '1,234,568');
  // у колонки без единиц выбор единиц не действует (его нет в меню; чужая запись не ломает колонку)
  await K.colFmt.set('price', { scale: 'bn', decimals: 1 });
  assert.strictEqual(K.nfNum('price', 98.399), '98.4', 'знаки — да');
  assert.strictEqual(K.nfEff('price').units, false);
  // со знаком «+»: сделки дня и изменение к вчера; CSV — без плюса
  assert.strictEqual(K.nfNum('tradeDay', 2500000), '+2.50'); assert.strictEqual(K.nfNum('tradeDay', 2500000, true), '2.50');
  assert.strictEqual(K.nfNum('tradeDay', -2500000), '(2.50)', 'бухгалтерский формат по умолчанию');
  K.setNegFmt('minus');
  assert.strictEqual(K.nfNum('tradeDay', -2500000), '−2.50'); assert.strictEqual(K.nfNum('mv', -1234567.891), '−1,234,568');
  assert.strictEqual(K.nfNum('ytm', -0.126), '−0.13%', 'суффикс — в формате числа');
  K.setNegFmt('acc');
  assert.strictEqual(K.nfNum('ytm', -0.126), '(0.13%)');
  // отрицательное, округлённое до нуля, — без знака и скобок
  await K.colFmt.set('dv01', { scale: 'k', decimals: 0 });
  assert.strictEqual(K.nfNum('dv01', -300), '0');
});

test('K.nfCol: единица, подпись с единицей, подсказка и подпись CSV — по выбору пользователя; без nf — та же колонка', async () => {
  const { K } = load(new Map());
  const cs = K.cols(['dv01', 'mv', 'notional', 'carry', 'ytm', 'isin']);
  const by = Object.fromEntries(cs.map(c => [c.key, c]));
  for (const c of cs) { const e = K.nfCol(c); assert.strictEqual(e.unit, c.unit, c.key + ': по умолчанию единица прежняя'); assert.strictEqual(e.label, c.label); assert.strictEqual(e.title, c.title); }
  assert.strictEqual(K.nfCol(by.isin), by.isin, 'колонка без nf — та же');
  await K.colFmt.set('dv01', { scale: 'mm' });
  assert.strictEqual(K.nfCol(by.dv01).unit, 'mm ₽/бп'); assert.strictEqual(K.nfCol(by.dv01).nfChanged, true);
  await K.colFmt.set('mv', { scale: 'bn' });
  assert.strictEqual(K.nfCol(by.mv).label, 'MV, bn ₽', 'единица MV — в подписи колонки'); assert.strictEqual(K.colUnit(K.nfCol(by.mv)), '', 'и не второй раз под подписью');
  await K.colFmt.set('carry', { scale: '1' });
  assert.strictEqual(K.nfCol(by.carryDay).unit, '₽');
  assert.ok(K.nfCol(by.carryDay).title.startsWith('₽ в день. '), K.nfCol(by.carryDay).title);
  await K.colFmt.set('notional', { scale: 'k' });
  assert.strictEqual(K.nfCol(by.notional).unit, 'тыс в вал.');
  assert.strictEqual(K.nfCol(by.ytm).unit, undefined, 'знаки без единиц: единицы нет');
  // подпись CSV у блоттера C03 (csvHead): «<подпись>, <единица>»
  const c03 = Object.assign({}, by.notional, { label: 'Номинал', csvHead: 'Номинал, mm в вал.' });
  assert.strictEqual(K.nfCol(c03).csvHead, 'Номинал, тыс в вал.');
  // сброс — снова прежнее
  await K.colFmt.reset('dv01'); assert.strictEqual(K.nfCol(by.dv01).unit, 'тыс ₽/бп'); assert.strictEqual(K.nfCol(by.dv01).nfChanged, false);
});

test('K.posCols: пометки рядом с числом — слово единицы («mm USD» у номинала), вид спреда; CSV — то же число, без значков', async () => {
  const { K } = load(new Map());
  const r = { ccy: 'USD', spreadLabel: 'к КБД', id: 'a' };
  const C = K.posCols;
  assert.strictEqual(C.notional.fmt(2500000, r), '2.50<span class="mut3">mm USD</span>');
  assert.strictEqual(C.spread.fmt(125.4, r), '125 <span class="mut3">к КБД</span>');
  await K.colFmt.set('notional', { decimals: 3, scale: 'bn' });
  assert.strictEqual(C.notional.fmt(2500000, r), '0.003<span class="mut3">bn USD</span>', '3 знака, bn: слово единицы в пометке сменилось');
  assert.strictEqual(C.notional.csvFmt(2500000), '0.003', 'CSV — то же число без пометок');
  await K.colFmt.set('notional', { scale: '1', decimals: 0 });
  assert.strictEqual(C.notional.fmt(2500000, r), '2,500,000<span class="mut3">USD</span>', 'как есть — без слова единицы');
  await K.colFmt.set('spread', { decimals: 2 });
  assert.strictEqual(C.spread.fmt(125.4, r), '125.40 <span class="mut3">к КБД</span>');
  same(C.spread.csvFmt(125.4, r), ['125.40', 'к КБД'], 'число спреда в CSV — как на экране, вид спреда — своей колонкой');
  assert.strictEqual(C.spread.fmt(null, r), '<span class="mut3">—</span>');
  // сделки дня: знак «+» на экране, в CSV без него; нет сделок — пусто
  const tr = { ccy: 'EUR', tradeToday: { net: { bonds: 10, face: 2500000 }, buy: { counted: true, bonds: 10, face: 2500000, n: 1 }, n: 1 } };
  assert.ok(/\+2\.50<span class="mut3">mm EUR<\/span>/.test(C.tradeDay.fmt(2500000, tr)), C.tradeDay.fmt(2500000, tr));
  await K.colFmt.set('tradeDay', { decimals: 1, scale: 'k' });
  assert.ok(/\+2,500\.0<span class="mut3">тыс EUR<\/span>/.test(C.tradeDay.fmt(2500000, tr)), C.tradeDay.fmt(2500000, tr));
  assert.strictEqual(C.tradeDay.csvFmt(2500000, tr), '2,500.0');
  assert.strictEqual(C.tradeDay.fmt(null, { ccy: 'EUR' }), '');
});

// ---------- K.table: ячейки, итоги, подпись единицы, CSV, подсветка и сортировка ----------

const rows = () => [
  { id: 'a', name: 'А', ccy: 'USD', notional: 2500000, mv: 1234567.891, dv01: 12345.6, carryDay: -1500, pnl: { total: 400 }, ytm: 12.345, zspread: 125.4, cs01: 400 },
  { id: 'b', name: 'Б', ccy: 'RUB', notional: -750000, mv: -987654.321, dv01: -400, carryDay: 3000, pnl: { total: -2600 }, ytm: null, zspread: null, cs01: null },
  { id: 'c', name: 'В', ccy: 'EUR', notional: null, mv: null, dv01: null, carryDay: null, pnl: { total: 0 }, ytm: 5, zspread: 10, cs01: 0 },
];
const KEYS = ['name', 'notional', 'mv', 'dv01', 'carry', 'pnl', 'ytm', 'spread', 'cs01'];
function table(K, extra) {
  const box = el(); box.id = 'tb';
  const api = K.table(box, Object.assign({ rows: rows(), search: false, viewKey: 'c99-fmt', columns: K.cols(KEYS) }, extra));
  return { api, box, html: () => box.children[0].innerHTML };
}
// ячейки одной колонки по порядку строк и итог (tfoot)
const cells = (html, key) => {
  const heads = [...html.matchAll(/<th class="sortable[^"]*" data-k="([^"]+)"/g)].map(m => m[1]), i = heads.indexOf(key);
  const body = html.slice(html.indexOf('<tbody>'), html.indexOf('</tbody>')), foot = html.slice(html.indexOf('<tfoot>'));
  const td = tr => [...tr.matchAll(/<td\b[^>]*?(?: class="([^"]*)")?[^>]*>([\s\S]*?)<\/td>/g)].map(m => ({ cls: m[1] || '', text: m[2].replace(/<[^>]+>/g, '') }));
  return { rows: [...body.matchAll(/<tr class="row[\s\S]*?<\/tr>/g)].map(m => td(m[0])[i]), total: td(foot)[i], head: (html.match(new RegExp('<th class="sortable[^"]*" data-k="' + key + '"[^>]*>([\\s\\S]*?)<span class="k-rsz'))[1]) };
};
const lines = csv => csv.split(/\r?\n/).filter(Boolean);
// CSV: строка → ячейки (RFC 4180: кавычки, «""» внутри); первая строка — служебная, вторая — заголовки, дальше строки таблицы
const parseLine = l => { const out = []; let cur = '', q = false; for (let i = 0; i < l.length; i++) { const c = l[i]; if (q) { if (c === '"' && l[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; } else if (c === '"') q = true; else if (c === ',') { out.push(cur); cur = ''; } else cur += c; } out.push(cur); return out; };
const csvTable = csv => { const L = lines(csv).slice(1).map(parseLine), head = L[0]; return { head, col: h => L.slice(1).map(r => r[head.indexOf(h)]), rows: L.slice(1) }; };

test('K.table: формат по умолчанию — прежний вид ячеек, итогов и заголовка', () => {
  const { K } = load(new Map()), t = table(K), h = t.html();
  const dv = cells(h, 'dv01'), mv = cells(h, 'mv');
  assert.deepStrictEqual(dv.rows.map(c => c.text), ['12', '0', '—'], '-0.4 тыс — ноль без знака и скобок');
  assert.strictEqual(dv.total.text, K.f.dv(12345.6 - 400));
  assert.ok(dv.head.includes('DV01') && dv.head.includes('<span class="k-thu">тыс ₽/бп</span>'), dv.head);
  assert.ok(mv.head.startsWith('MV, mm ₽') && !mv.head.includes('k-thu'), mv.head);
  assert.deepStrictEqual(cells(h, 'notional').rows.map(c => c.text), ['2.50mm USD', '(0.75)mm RUB', '—mm EUR']);
});

test('K.table: 3 знака у номинала, mm у MV в валюте отчёта USD, null — «—», итог и CSV — как на экране', async () => {
  const store = new Map([['concepts-repr', 'USD']]);
  const { K } = load(store);
  assert.strictEqual(K.repr, 'USD');
  const t = table(K);
  await K.colFmt.set('notional', { decimals: 3 });
  await K.colFmt.set('mv', { scale: 'mm' });
  let h = t.html();
  assert.deepStrictEqual(cells(h, 'notional').rows.map(c => c.text), ['2.500mm USD', '(0.750)mm RUB', '—mm EUR']);
  assert.deepStrictEqual(cells(h, 'mv').rows.map(c => c.text), ['1', '(1)', '—'], 'mm без знаков: целые');
  await K.colFmt.set('mv', { scale: 'mm', decimals: 2 });
  h = t.html();
  assert.deepStrictEqual(cells(h, 'mv').rows.map(c => c.text), ['1.23', '(0.99)', '—']);
  assert.strictEqual(cells(h, 'mv').total.text, '0.25', 'итог — в том же формате: 1.23 − 0.99 по исходным числам');
  assert.ok(cells(h, 'mv').head.startsWith('MV, mm ₽'), 'подпись единицы: «₽» подменит символ валюты отчёта');
  // CSV — те же числа и та же подпись единицы, что на экране; валюта отчёта — «$»
  let C = csvTable(t.api.csv());
  assert.deepStrictEqual(C.head.slice(0, 5), ['Бумага', 'Номинал, mm в вал.', 'MV, mm $', 'DV01, тыс $/бп', 'Кэрри/д, тыс $']);
  assert.deepStrictEqual(C.col('MV, mm $'), ['1.23', '-0.99', ''], 'то же число, что на экране; с обычным минусом');
  assert.deepStrictEqual(C.col('Номинал, mm в вал.'), ['2.500', '-0.750', '']);
  // единицы сменили — подпись в заголовке и в CSV сменилась вместе с ними
  await K.colFmt.set('dv01', { scale: '1' });
  h = t.html();
  assert.ok(cells(h, 'dv01').head.includes('<span class="k-thu">₽/бп</span>'), cells(h, 'dv01').head);
  assert.deepStrictEqual(cells(h, 'dv01').rows.map(c => c.text), ['12,346', '(400)', '—']);
  C = csvTable(t.api.csv());
  assert.ok(C.head.includes('DV01, $/бп'), C.head.join(' | '));
  assert.deepStrictEqual(C.col('DV01, $/бп'), ['12346', '-400', ''], 'как есть: целые, без разделителей тысяч');
});

test('K.table: отрицательные в обоих форматах (K.negFmt), CSV — всегда с минусом', async () => {
  const { K } = load(new Map());
  const t = table(K);
  await K.colFmt.set('dv01', { scale: '1' });
  assert.deepStrictEqual(cells(t.html(), 'dv01').rows.map(c => c.text), ['12,346', '(400)', '—']);
  K.setNegFmt('minus');
  assert.deepStrictEqual(cells(t.html(), 'dv01').rows.map(c => c.text), ['12,346', '−400', '—']);
  assert.deepStrictEqual(csvTable(t.api.csv()).col('DV01, ₽/бп'), ['12346', '-400', ''], 'CSV — с обычным минусом');
  K.setNegFmt('acc');
  assert.deepStrictEqual(csvTable(t.api.csv()).col('DV01, ₽/бп'), ['12346', '-400', ''], 'и в режиме скобок — тот же');
});

test('K.table: смена формата перерисовывает таблицу сразу, во всех таблицах страницы', async () => {
  const { K } = load(new Map());
  const a = table(K), b = table(K, { viewKey: 'c99-fmt-2', columns: K.cols(['name', 'dv01']) });
  assert.strictEqual(cells(a.html(), 'dv01').rows[0].text, '12');
  assert.strictEqual(cells(b.html(), 'dv01').rows[0].text, '12');
  await K.colFmt.set('dv01', { decimals: 2 });
  assert.strictEqual(cells(a.html(), 'dv01').rows[0].text, '12.35', 'первая таблица');
  assert.strictEqual(cells(b.html(), 'dv01').rows[0].text, '12.35', 'и вторая — без перезагрузки');
  await K.colFmt.reset('dv01');
  assert.strictEqual(cells(a.html(), 'dv01').rows[0].text, '12'); assert.strictEqual(cells(b.html(), 'dv01').rows[0].text, '12');
  // «Как было» состава колонок формат не трогает
  await K.colFmt.set('dv01', { decimals: 1 });
  await a.api.setHidden('mv', true); await a.api.resetCols();
  same(K.colFmt.get('dv01'), { decimals: 1, scale: null });
  assert.strictEqual(cells(a.html(), 'dv01').rows[0].text, '12.3');
});

test('K.table: подсветка и сортировка — по исходному числу, не по округлённому', async () => {
  const { K } = load(new Map());
  const t = table(K, { sort: [] });
  await K.colFmt.set('dv01', { scale: 'mm', decimals: 0 });  // 12345.6 → «0», -400 → «0» (минус без знака), оба — «0» на экране
  const dv = cells(t.html(), 'dv01');
  assert.deepStrictEqual(dv.rows.map(c => c.text), ['0', '0', '—'], 'на экране округлено до нуля');
  assert.ok(/\bpos\b/.test(dv.rows[0].cls) && /\bneg\b/.test(dv.rows[1].cls), 'цвет по исходному числу: ' + dv.rows.map(c => c.cls));
  t.api.setSort([['dv01', 1]]);
  const order = () => [...t.html().matchAll(/<tr class="row[^>]*data-id="([^"]+)"/g)].map(m => m[1]);
  assert.deepStrictEqual(order(), ['b', 'a', 'c'], 'по возрастанию исходных чисел: -400, 12345.6, null — в конце');
  t.api.setSort([['dv01', -1]]);
  assert.deepStrictEqual(order(), ['a', 'b', 'c']);
  // знаков нет совсем, а цифры строк разные: сортировка тоже по исходным
  await K.colFmt.set('ytm', { decimals: 0 });
  t.api.setSort([['ytm', 1]]);
  assert.deepStrictEqual(order(), ['c', 'a', 'b'], 'ytm: 5, 12.345, null');
  assert.deepStrictEqual(cells(t.html(), 'ytm').rows.map(c => c.text), ['5%', '12%', '—']);
});

test('K.table: колонка без описания числа (nf) и нечисловые колонки — как были', async () => {
  const { K } = load(new Map());
  const cols = [{ key: 's', label: 'Имя' }, { key: 'x', label: 'Икс', num: true, sum: true, unit: 'тыс ₽', fmt: v => v.toFixed(1) }, { key: 'dv01', label: 'DV01', num: true, sum: true, unit: 'тыс ₽/бп', fmt: v => v.toFixed(2) }];
  const t = table(K, { rows: [{ id: 'a', s: 'один', x: 1.25, dv01: 3.456 }], columns: cols });
  await K.colFmt.set('dv01', { decimals: 5, scale: 'mm' });  // колонка с тем же ключом, но своя, без nf: формат её не касается
  const h = t.html();
  assert.ok(h.includes('3.46') && !h.includes('3.45600'), 'своя колонка со своим fmt: формат пользователя не действует');
  assert.ok(h.includes('<span class="k-thu">тыс ₽/бп</span>'), 'и её единица прежняя');
});
