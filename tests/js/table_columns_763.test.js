// Скрытые колонки таблиц позиций (TDM#763): K.colKey, K.colPrefs (сервер по пользователю + копия в браузере), K.table с
// кнопкой «Колонки ▾» (hiddenCols, setHidden, CSV по видимым), K.cols — таблицы позиций, C03 — наборы колонок.
// Меню колонок в браузере (список с галочками, 390 px, сервер) — tests/js/table_columns_763.pw.test.js.
//   node --test tests/js/table_columns_763.test.js
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
const headKeys = html => [...html.matchAll(/<th class="sortable[^"]*" data-k="([^"]+)"/g)].map(m => m[1]);
const rows = [{ id: 'a', x: 1, y: 2.5, s: 'один', z: 'a1' }, { id: 'b', x: -1234.5, y: null, s: 'два', z: 'b1' }];
const columns = () => [{ key: 's', label: 'Имя', filter: true }, { key: 'x', label: 'Икс', num: true, sum: true, unit: 'тыс ₽' }, { key: 'y', label: 'Игрек, %', num: true, fmt: v => v == null ? '—' : v.toFixed(2) + '%' }, { key: 'z', label: 'Зет', filter: true }];
const table = (K, opts) => { const box = el(); box.id = 'tb'; const api = K.table(box, Object.assign({ rows, search: false, viewKey: 'c99-pos', colPick: true, columns: columns() }, opts)); return { api, box, html: () => box.children[0].innerHTML }; };
const KEY = 'tdm.view.v1:cols:c99-pos';

test('K.colKey: ключ экран + таблица (+ набор) — как принимает сервер', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.colKey('table:c01-positions'), 'c01-positions');
  assert.strictEqual(K.colKey('c03-blotter:base'), 'c03-blotter:base');
  assert.strictEqual(K.colKey('table:C06 Top|размер'), 'c06_top_');
  assert.strictEqual(K.colKey('table:' + 'a'.repeat(300)).length, 128);
  assert.strictEqual(K.colKey('table:-x'), 'x', 'первый знак — буква или цифра');
  assert.ok(/^[a-z0-9][a-z0-9._:-]{0,127}$/.test(K.colKey('table:c13-issuer-bonds')));
});

test('K.colPrefs без сервера (демо, файл): выбор в браузере, чтение и запись в try/catch', async () => {
  const store = new Map(), { K, calls } = load(store, { fetch: () => { throw new Error('нет запроса без сервера'); } });
  same(K.colPrefs.get('t1'), []);
  assert.strictEqual(K.colPrefs.where(), 'browser');
  await K.colPrefs.set('t1', ['b', 'a', 'b', '', 5, null]);
  same(K.colPrefs.get('t1'), ['b', 'a'], 'без повторов и мусора');
  same(JSON.parse(store.get('tdm.view.v1:cols:t1')), { hidden: ['b', 'a'] });
  assert.strictEqual(calls.length, 0, 'в демо запросов к серверу нет');
  await K.colPrefs.reset('t1');
  same(K.colPrefs.get('t1'), []); assert.ok(!store.has('tdm.view.v1:cols:t1'));
  // страница после перезагрузки: выбор — из браузера
  store.set('tdm.view.v1:cols:t2', '{"hidden":["q"]}');
  same(load(store).K.colPrefs.get('t2'), ['q']);
  for (const raw of ['5', '"abc"', '{"hidden":"abc"}', '{"hidden":[1,{}]}', 'not json', '[]']) {
    store.set('tdm.view.v1:cols:t3', raw);
    same(load(store).K.colPrefs.get('t3'), [], raw);
  }
  // хранилище бросает (приватное окно): выбор живёт до перезагрузки, ошибок нет
  const no = () => { throw new Error('denied'); };
  const { K: K2 } = load(null, { ls: { getItem: no, setItem: no, removeItem: no } });
  await assert.doesNotReject(async () => K2.colPrefs.set('z', ['k']));
  same(K2.colPrefs.get('z'), ['k']);
});

test('K.colPrefs с сервером: ответ — слово сервера, правка — PUT, «как было» — DELETE, по очереди', async () => {
  const store = new Map([['tdm.view.v1:cols:t1', '{"hidden":["old"]}'], ['tdm.view.v1:cols:t2', '{"hidden":["browser"]}']]);
  const srv = { t1: { hidden: ['a', 'b'] } }, log = [];
  const { K, calls } = load(store, { real: true, fetch: (url, init) => {
    log.push((init.method || 'GET') + ' ' + url);
    if (!init.method) return { status: 200, body: { user: 'ann', tables: srv } };
    const key = decodeURIComponent(url.split('/').pop());
    if (init.method === 'PUT') srv[key] = { hidden: JSON.parse(init.body).hidden }; else delete srv[key];
    return { status: init.method === 'PUT' ? 200 : 204, body: {} };
  } });
  const seen = []; K.on('colprefs', () => seen.push(K.colPrefs.get('t1').join()));
  same(K.colPrefs.get('t1'), ['old'], 'до ответа сервера — копия браузера (страница рисуется сразу)');
  await tick(); await tick();
  assert.strictEqual(calls[0].url, '../api/v1/me/table-columns');
  same(seen, ['a,b'], 'пришёл ответ — событие, таблицы перерисуются');
  same(K.colPrefs.get('t1'), ['a', 'b']);
  same(K.colPrefs.get('t2'), [], 'сервер ответил без выбора — по умолчанию, а не копия браузера (другой пользователь того же браузера)');
  same(JSON.parse(store.get('tdm.view.v1:cols:t1')), { hidden: ['a', 'b'] }, 'копия в браузере обновлена ответом');
  // правка: PUT с JSON; три быстрых правки одной таблицы — по очереди
  K.colPrefs.set('t1', ['a']); K.colPrefs.set('t1', ['a', 'c']);
  const last = K.colPrefs.set('t1', ['c']);
  same(K.colPrefs.get('t1'), ['c'], 'выбор виден сразу, не ждёт сервера');
  await last;
  const puts = calls.filter(c => c.method === 'PUT');
  assert.deepStrictEqual(puts.map(c => c.url), Array(3).fill('../api/v1/me/table-columns/t1'));
  same(puts.map(c => JSON.parse(c.body).hidden), [['a'], ['a', 'c'], ['c']]);
  assert.strictEqual(puts[0].headers['Content-Type'], 'application/json');
  same(srv.t1.hidden, ['c']);
  // «как было» — DELETE, ключ с «:» кодируется
  await K.colPrefs.reset('c03-blotter:base');
  const del = calls.filter(c => c.method === 'DELETE');
  assert.strictEqual(del.length, 1); assert.strictEqual(del[0].url, '../api/v1/me/table-columns/c03-blotter%3Abase');
  assert.strictEqual(del[0].headers['Content-Type'], 'application/json', 'DELETE тоже с JSON: так требует сервер');
  assert.strictEqual(K.colPrefs.where(), 'server');
});

test('K.colPrefs: сервер не ответил или не принял запись — выбор остаётся в браузере, ошибок нет', async () => {
  const store = new Map([['tdm.view.v1:cols:t1', '{"hidden":["a"]}']]);
  const { K } = load(store, { real: true, fetch: () => ({ status: 503, body: {} }) });
  K.colPrefs.get('t1');
  await tick(); await tick();
  same(K.colPrefs.get('t1'), ['a'], 'копия браузера');
  assert.strictEqual(K.colPrefs.where(), 'offline');
  await K.colPrefs.set('t1', ['a', 'b']);
  same(K.colPrefs.get('t1'), ['a', 'b']);
  same(JSON.parse(store.get('tdm.view.v1:cols:t1')), { hidden: ['a', 'b'] });
  // сеть бросает
  const { K: K2 } = load(new Map(), { real: true, fetch: () => { throw new Error('network'); } });
  K2.colPrefs.get('t1');
  await tick(); await tick();
  assert.strictEqual(K2.colPrefs.where(), 'offline');
  await assert.doesNotReject(K2.colPrefs.set('q', ['x']));
  // мусор в ответе
  for (const body of [null, 5, 'x', { tables: 5 }, { tables: [] }, { tables: { t: 5 } }, { tables: { t: { hidden: 'abc' } } }]) {
    const { K: K3 } = load(new Map(), { real: true, fetch: () => ({ status: 200, body }) });
    K3.colPrefs.get('t');
    await tick(); await tick();
    same(K3.colPrefs.get('t'), [], JSON.stringify(body));
  }
});

test('K.colPrefs: правка без связи с сервером помнится как «не отправлено» и уходит раньше ответа сервера при следующей загрузке', async () => {
  const store = new Map(), srv = { t1: { hidden: ['old'] } }, DIRTY = 'tdm.view.v1:cols-dirty';
  let down = true;
  const calls1 = [];
  const f = (url, init) => { calls1.push((init.method || 'GET') + ' ' + decodeURIComponent(url.split('table-columns')[1] || ''));
    if (down) return { status: 503, body: {} };
    if (!init.method) return { status: 200, body: { user: 'ann', tables: srv } };
    const key = decodeURIComponent(url.split('/').pop());
    if (init.method === 'PUT') srv[key] = { hidden: JSON.parse(init.body).hidden }; else delete srv[key];
    return { status: init.method === 'PUT' ? 200 : 204, body: {} }; };
  const { K } = load(store, { real: true, fetch: f });
  K.colPrefs.get('t1');
  await tick(); await tick();
  await K.colPrefs.set('t1', ['a', 'b']);       // правка: сервер недоступен
  await K.colPrefs.reset('t2');                  // и «как было» другой таблицы
  same(JSON.parse(store.get(DIRTY)).keys, ['t1', 't2'], 'не отправлено: обе таблицы');
  same(JSON.parse(store.get('tdm.view.v1:cols:t1')), { hidden: ['a', 'b'] });
  same(JSON.parse(store.get('tdm.view.v1:cols:t2')), { hidden: [] }, 'пустой список «как было» помнится, пока сервер не принял');
  assert.strictEqual(K.colPrefs.where(), 'offline');
  // страница открыта снова, сервер на связи (на нём у t2 ещё лежит старый выбор): PUT и DELETE уходят до ответа, ответ их не перекрывает
  down = false; srv.t2 = { hidden: ['x'] };
  calls1.length = 0;
  const { K: K2 } = load(store, { real: true, fetch: f });
  same(K2.colPrefs.get('t1'), ['a', 'b'], 'до ответа — копия браузера');
  await tick(); await tick(); await tick(); await tick();
  assert.deepStrictEqual(calls1.slice().sort(), ['DELETE /t2', 'GET ', 'PUT /t1'].sort());
  assert.strictEqual(calls1[calls1.length - 1], 'GET ', 'ответ сервера читается после досылки');
  same(srv.t1.hidden, ['a', 'b']); assert.ok(!('t2' in srv), 'на сервере «как было»');
  same(K2.colPrefs.get('t1'), ['a', 'b']); same(K2.colPrefs.get('t2'), []);
  assert.ok(!store.has(DIRTY), 'принято — «не отправлено» снято');
  assert.ok(!store.has('tdm.view.v1:cols:t2'));
  assert.strictEqual(K2.colPrefs.where(), 'server');
  // сервер не принял досылку (422): выбор не теряется, остаётся «не отправлено»
  const store3 = new Map([['tdm.view.v1:cols:t9', '{"hidden":["z"]}'], [DIRTY, '{"keys":["t9"]}']]);
  const { K: K3 } = load(store3, { real: true, fetch: (url, init) => init.method ? { status: 422, body: {} } : { status: 200, body: { tables: { t9: { hidden: [] } } } } });
  K3.colPrefs.get('t9'); await tick(); await tick(); await tick(); await tick();
  same(K3.colPrefs.get('t9'), ['z'], 'сервер ответил пусто, но своя непринятая правка важнее');
  same(JSON.parse(store3.get(DIRTY)).keys, ['t9']);
});

test('K.colPrefs: страница без таблиц с выбором колонок сервер не спрашивает', async () => {
  const { K, calls } = load(new Map(), { real: true, fetch: () => ({ status: 200, body: { tables: {} } }) });
  table(K, { colPick: false });
  await tick(); await tick();
  assert.strictEqual(calls.length, 0);
  table(K);
  await tick(); await tick();
  assert.deepStrictEqual(calls.map(c => c.method + ' ' + c.url), ['GET ../api/v1/me/table-columns'], 'один запрос на страницу, сколько бы таблиц ни было');
  table(K, { viewKey: 'c99-more' });
  await tick(); await tick();
  assert.strictEqual(calls.length, 1);
});

test('K.colPrefs: правка до ответа сервера не теряется', async () => {
  let release; const gate = new Promise(r => { release = r; });
  const { K } = load(new Map(), { real: true, fetch: (url, init) => init.method ? { status: 200, body: {} } : gate.then(() => ({ status: 200, body: { tables: { t1: { hidden: ['server'] }, t2: { hidden: ['srv2'] } } } })) });
  K.colPrefs.get('t1');  // страница просит выбор — запрос ушёл и ждёт
  const p = K.colPrefs.set('t1', ['mine']);
  release(); await tick(); await tick(); await p;
  same(K.colPrefs.get('t1'), ['mine'], 'своя правка важнее запоздавшего ответа');
  same(K.colPrefs.get('t2'), ['srv2']);
});

test('K.table: «Колонки» скрывает и возвращает колонки, CSV и подвал — по видимым', () => {
  const store = new Map(), { K } = load(store), { api, box, html } = table(K);
  same(headKeys(html()), ['s', 'x', 'y', 'z']);
  assert.ok(api.colMenu && /^Колонки ▾$/.test(api.colMenu.textContent), 'кнопка в строке кнопок: ' + (api.colMenu && api.colMenu.textContent));
  assert.ok(box.children[1].children[0].children.includes(api.colMenu), 'кнопка — в строке кнопок таблицы, перед «⤓ CSV»');
  api.setHidden('y', true);
  same(headKeys(html()), ['s', 'x', 'z']);
  same(api.hiddenCols(), ['y']);
  assert.strictEqual(api.colMenu.textContent, 'Колонки · скрыто 1 ▾');
  same(JSON.parse(store.get(KEY)), { hidden: ['y'] });
  // CSV — только видимые колонки, в их порядке
  const L = api.csv().split('\r\n');
  assert.strictEqual(L[2], 'Имя,"Икс, тыс ₽",Зет'); assert.strictEqual(L[3], 'один,1,a1');
  assert.ok(!/Игрек/.test(api.csv()), 'скрытой колонки в CSV нет');
  const foot = /<tfoot>[\s\S]*<\/tfoot>/.exec(html())[0];
  assert.ok(/\(1,234\)/.test(foot) && (foot.match(/<td/g) || []).length === 3, 'подвал: итог x на месте, ячеек по числу видимых колонок: ' + foot);
  api.setHidden('x', true); api.setHidden('z', true);
  same(headKeys(html()), ['s']);
  assert.strictEqual(api.csv().split('\r\n')[2], 'Имя');
  api.setHidden('x', false);
  same(headKeys(html()), ['s', 'x'], 'вернули — на своём месте по порядку таблицы');
  api.colMenu.paint();
  assert.strictEqual(api.colMenu.textContent, 'Колонки · скрыто 2 ▾');
});

test('K.table: первая колонка и noHide не скрываются, неизвестный ключ пропускается, новая колонка видна', () => {
  // сохранено в версии, где была колонка 'gone' и не было 'z': 's' (первая) и 'nosuch' — лишнее
  const store = new Map([[KEY, JSON.stringify({ hidden: ['s', 'gone', 'y', 'nosuch'] })]]), { K } = load(store);
  const { api, html } = table(K);
  same(headKeys(html()), ['s', 'x', 'z'], 'скрыта только y; первая колонка и неизвестные ключи — как будто не сохранены; новая z видна');
  same(api.hiddenCols(), ['y']);
  const t2 = table(K, { columns: [{ key: 'id', label: 'Номер' }].concat(columns()) });
  same(headKeys(t2.html()), ['id', 'x', 'z'], 'первая — id: s уже не первая и скрыта, как сохранено');
  // колонка с noHide не скрывается, даже если она не первая и сохранена скрытой
  const store2 = new Map([[KEY, JSON.stringify({ hidden: ['s', 'x'] })]]), { K: K2 } = load(store2);
  const t3 = table(K2, { columns: [{ key: 'x', label: 'Икс', num: true }, { key: 's', label: 'Имя', noHide: true }, { key: 'z', label: 'Зет' }] });
  same(headKeys(t3.html()), ['x', 's', 'z'], 'x — первая колонка, s — с noHide: обе остаются, хотя сохранены скрытыми');
});

test('K.table: скрытая колонка уносит свои фильтр и сортировку; «как было» возвращает всё', () => {
  const store = new Map(), { K } = load(store), { api, html } = table(K, { sort: [['x', 1]] });
  api.setFilter('z', ['a1']); api.setSort([['y', -1], ['z', 1]]);
  assert.strictEqual(api.csv().split('\r\n').length, 5, 'строка a1 — одна');
  api.setHidden('z', true);
  same(api.state.sort, [['y', -1]], 'сортировка по z снята');
  assert.strictEqual(api.state.filters.z, undefined, 'фильтр по z снят — иначе строки пропали бы без видимой причины');
  assert.strictEqual(api.csv().split('\r\n').length, 6, 'обе строки');
  api.setHidden('y', true);
  same(api.state.sort, [], 'сортировка по y снята');
  // «как было» из меню: reset колонок (не «⟲ вид» — ширина и порядок остаются)
  api.setLayout({ order: ['x', 's', 'y', 'z'], w: { x: 90 } });
  api.setHidden('y', false); api.setHidden('z', false);
  same(headKeys(html()), ['x', 's', 'y', 'z'], 'порядок из раскладки сохранён');
  same(api.layout().w, { x: 90 });
  // «Как было» в меню: все колонки, выбор пользователя удалён (в браузере — копия), ширина и порядок («⟲ вид») остаются
  api.setHidden('y', true); api.setHidden('z', true);
  assert.ok(store.has(KEY));
  api.resetCols();
  same(headKeys(html()), ['x', 's', 'y', 'z']); same(api.hiddenCols(), []);
  assert.ok(!store.has(KEY), 'копия выбора в браузере удалена');
  assert.strictEqual(api.colMenu.textContent, 'Колонки ▾');
  same(api.layout().w, { x: 90 }, 'ширина колонок — отдельно от состава');
});

test('K.table: перенос колонки мышкой при скрытых колонках — скрытые остаются на своих местах', () => {
  const store = new Map(), { K } = load(store), { api, html } = table(K, { columns: columns().concat([{ key: 'w', label: 'Дубль' }]) });
  // порядок s, x, y, z, w; скрыта y (в середине)
  api.setHidden('y', true);
  same(headKeys(html()), ['s', 'x', 'z', 'w']);
  api.setVisibleOrder(['s', 'z', 'x', 'w']);  // z перенесли перед x
  same(headKeys(html()), ['s', 'z', 'x', 'w']);
  same(api.layout().order, ['s', 'z', 'y', 'x', 'w'], 'y осталась на месте, между вторым и третьим слотом видимых');
  api.setHidden('y', false);
  same(headKeys(html()), ['s', 'z', 'y', 'x', 'w'], 'вернувшаяся колонка — на прежнем месте, а не в конце');
  // без сохранённого порядка: скрытая колонка — на месте по порядку таблицы
  const t2 = table(K, { viewKey: 'c99-drag2' });
  t2.api.setHidden('x', true);
  t2.api.setVisibleOrder(['z', 's', 'y']);
  same(t2.api.layout().order, ['z', 'x', 's', 'y']);
  t2.api.setHidden('x', false);
  same(headKeys(t2.html()), ['z', 'x', 's', 'y']);
});

test('K.table: ответ сервера позже страницы перерисовывает таблицу; чужая таблица не трогается', async () => {
  const srv = { 'c99-pos': { hidden: ['y', 'nosuch'] } };
  const { K } = load(new Map(), { real: true, fetch: (url, init) => init.method ? { status: 200, body: {} } : { status: 200, body: { user: 'bob', tables: srv } } });
  const a = table(K), b = table(K, { viewKey: 'c99-other' });
  same(headKeys(a.html()), ['s', 'x', 'y', 'z']);
  await tick(); await tick();
  same(headKeys(a.html()), ['s', 'x', 'z'], 'выбор пользователя с сервера');
  same(headKeys(b.html()), ['s', 'x', 'y', 'z']);
  assert.strictEqual(a.api.colMenu.textContent, 'Колонки · скрыто 1 ▾');
  // таблицу нарисовали заново в том же месте: старая подписка не остаётся
  const again = K.table(a.box, { rows, search: false, viewKey: 'c99-pos', colPick: true, columns: columns() });
  same(again.hiddenCols(), ['y']);
});

test('K.table: кнопка «Колонки» — только у таблиц позиций (K.cols) и с colPick', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.cols(['name', 'ccy']).posTable, true);
  assert.strictEqual(K.cols(['name']).length, 1);
  assert.strictEqual(K.posCols.name.noHide, true, 'идентификатор позиции');
  const plain = table(K, { colPick: undefined });
  assert.strictEqual(plain.api.colMenu, null, 'ручной список колонок — без кнопки');
  assert.strictEqual(plain.api.hiddenCols().length, 0);
  const pos = table(K, { colPick: undefined, columns: K.cols(['name', 'ccy', 'issuer', 'kind', 'ytm']), rows: [] });
  assert.ok(pos.api.colMenu, 'K.cols — таблица позиций');
  const off = table(K, { colPick: false, columns: K.cols(['name', 'ytm']), rows: [] });
  assert.strictEqual(off.api.colMenu, null, 'colPick: false — без кнопки');
  // таблица позиций на колонках K.posCols: «Бумага» не скрывается, даже если сохранена скрытой
  const store = new Map([['tdm.view.v1:cols:c99-pos', JSON.stringify({ hidden: ['name', 'ytm'] })]]), { K: K2 } = load(store);
  const p2 = table(K2, { colPick: undefined, columns: K2.cols(['ccy', 'name', 'ytm']), rows: [] });
  same(headKeys(p2.html()), ['ccy', 'name'], 'name — noHide, ccy — первая колонка; скрыта только ytm');
});

test('мониторы: таблицы позиций на K.cols получают кнопку, C03 — выбор поверх набора', () => {
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
  const withCols = [];
  fs.readdirSync(dir).filter(f => /^c\d+.*\.html$/.test(f)).forEach(f => {
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    // K.table(…, { viewKey: 'x', … columns: K.cols(… }): блок до следующего K.table
    src.split(/K\.table\(/).slice(1).forEach(part => {
      const m = /viewKey: '([a-z0-9-]+)'/.exec(part.slice(0, 400));
      if (m && /columns: K\.cols\(/.test(part.slice(0, 3000)) && !/colPick: false/.test(part.slice(0, 400))) withCols.push(f.slice(0, 3) + ':' + m[1]);
    });
  });
  assert.ok(withCols.length >= 9, 'таблицы на K.cols: ' + withCols.join(' '));
  ['c01:c01-positions', 'c06:c06-fx-bonds', 'c07:c07-members', 'c12:c12-positions', 'c13:c13-issuer-bonds'].forEach(x => assert.ok(withCols.includes(x), x));
  assert.ok(/viewKey: 'c11-top-changes', colPick: true/.test(P('concepts/c11-history.html')), 'C11 — таблица позиций на K.posCols');
  assert.ok(/viewKey: 'c17-positions', colPick: true/.test(P('concepts/c17-forward-carry.html')) && /key: 'name', label: 'Позиция', noHide: true/.test(P('concepts/c17-forward-carry.html')), 'C17 — таблица позиций');
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/K\.colKey\('c03-blotter:' \+ st\.preset\)/.test(c03) && /K\.colMenu\(/.test(c03) && /K\.colPrefs\.set\(colKey\(\)/.test(c03), 'C03: ключ — экран + набор');
  assert.ok(/id="sColsPick"/.test(c03));
});
