// Скрытые колонки таблиц позиций (TDM#763): K.colKey, K.colPrefs (сервер по пользователю + копия в браузере), K.table с
// кнопкой «Колонки ▾» (hiddenCols, setHidden, CSV по видимым), K.cols — таблицы позиций, C03 — наборы колонок.
// Добавленные колонки («Добавить» из каталога, TDM#778) — в конце файла.
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

// ---------- добавленные колонки (TDM#778): «Добавить» из каталога колонок позиций ----------
// стандартные колонки позиций (K.posCols), которых нет в таблице; хранятся рядом со скрытыми: {hidden, added}
const csvHeads = api => api.csv().split('\r\n')[2];
const posTable = (K, opts) => { const box = el(); box.id = 'tb'; const api = K.table(box, Object.assign({ rows: K.list(), search: false, viewKey: 'c99-pos', columns: K.cols(['name', 'ccy', 'ytm']) }, opts)); return { api, box, html: () => box.children[box.children.length - 2].innerHTML }; };

test('K.colPrefs: добавленные колонки лежат рядом со скрытыми; без сервера — в браузере, «как было» сбрасывает оба списка', async () => {
  const store = new Map(), { K, calls } = load(store, { fetch: () => { throw new Error('нет запроса без сервера'); } });
  same(K.colPrefs.added('t1'), []);
  await K.colPrefs.set('t1', ['a'], ['isin', 'zkbd', 'isin', '', 5]);
  same(K.colPrefs.sel('t1'), { hidden: ['a'], added: ['isin', 'zkbd'] }, 'без повторов и мусора, порядок добавления');
  same(JSON.parse(store.get('tdm.view.v1:cols:t1')), { hidden: ['a'], added: ['isin', 'zkbd'] });
  // правка только скрытых не трогает добавленные
  await K.colPrefs.set('t1', ['b']);
  same(K.colPrefs.sel('t1'), { hidden: ['b'], added: ['isin', 'zkbd'] });
  // только добавленные: выбор есть (скрытых нет)
  await K.colPrefs.set('t2', [], ['isin']);
  same(JSON.parse(store.get('tdm.view.v1:cols:t2')), { hidden: [], added: ['isin'] });
  same(load(store).K.colPrefs.sel('t2'), { hidden: [], added: ['isin'] }, 'после перезагрузки страницы');
  // выбор из версии до TDM#778 — только hidden; мусор в added не ломает
  store.set('tdm.view.v1:cols:t3', '{"hidden":["q"]}');
  same(load(store).K.colPrefs.sel('t3'), { hidden: ['q'], added: [] });
  for (const raw of ['{"added":"abc"}', '{"added":[1,{}]}', '{"hidden":["q"],"added":5}']) {
    store.set('tdm.view.v1:cols:t4', raw);
    same(load(store).K.colPrefs.added('t4'), [], raw);
  }
  // «как было»: и скрытые, и добавленные
  await K.colPrefs.reset('t1');
  same(K.colPrefs.sel('t1'), { hidden: [], added: [] }); assert.ok(!store.has('tdm.view.v1:cols:t1'));
  assert.strictEqual(calls.length, 0);
});

test('K.colPrefs с сервером: PUT несёт и скрытые, и добавленные; только добавленные — PUT, оба пусты — DELETE; старый ответ без added читается', async () => {
  const store = new Map(), srv = { t1: { hidden: ['a'] }, t2: { hidden: [], added: ['isin'] } }, DIRTY = 'tdm.view.v1:cols-dirty';
  let down = false;
  const { K, calls } = load(store, { real: true, fetch: (url, init) => {
    if (down) return { status: 503, body: {} };
    if (!init.method) return { status: 200, body: { user: 'ann', tables: srv } };
    const key = decodeURIComponent(url.split('/').pop());
    if (init.method === 'PUT') srv[key] = JSON.parse(init.body); else delete srv[key];
    return { status: init.method === 'PUT' ? 200 : 204, body: {} };
  } });
  K.colPrefs.get('t1'); await tick(); await tick();
  same(K.colPrefs.sel('t1'), { hidden: ['a'], added: [] }, 'сервер версии до TDM#778 отдаёт только hidden');
  same(K.colPrefs.sel('t2'), { hidden: [], added: ['isin'] });
  same(JSON.parse(store.get('tdm.view.v1:cols:t2')), { hidden: [], added: ['isin'] }, 'копия в браузере обновлена ответом');
  calls.length = 0;
  await K.colPrefs.set('t1', ['a'], ['zkbd']);
  await K.colPrefs.set('t2', [], ['isin', 'zdisc']);
  await K.colPrefs.set('t1', ['b']);  // added не передан — как были
  same(calls.map(c => [c.method, JSON.parse(c.body)]), [['PUT', { hidden: ['a'], added: ['zkbd'] }], ['PUT', { hidden: [], added: ['isin', 'zdisc'] }], ['PUT', { hidden: ['b'], added: ['zkbd'] }]]);
  assert.strictEqual(calls[0].headers['Content-Type'], 'application/json');
  await K.colPrefs.set('t2', [], ['isin']);
  assert.strictEqual(calls[calls.length - 1].method, 'PUT', 'только добавленные — не «как было»');
  await K.colPrefs.reset('t2');
  assert.strictEqual(calls[calls.length - 1].method, 'DELETE'); assert.ok(!('t2' in srv));
  // сервер недоступен: выбор с добавленными помнится как «не отправлено» и уходит при следующей загрузке страницы
  down = true;
  await K.colPrefs.set('t5', ['x'], ['isin']);
  same(JSON.parse(store.get(DIRTY)).keys, ['t5']);
  same(JSON.parse(store.get('tdm.view.v1:cols:t5')), { hidden: ['x'], added: ['isin'] });
  down = false; calls.length = 0;
  const { K: K2, calls: c2 } = load(store, { real: true, fetch: (url, init) => { if (!init.method) return { status: 200, body: { tables: {} } }; srv.t5 = JSON.parse(init.body); return { status: 200, body: {} }; } });
  same(K2.colPrefs.sel('t5'), { hidden: ['x'], added: ['isin'] }, 'до ответа — копия браузера');
  await tick(); await tick(); await tick(); await tick();
  same(c2.filter(c => c.method === 'PUT').map(c => JSON.parse(c.body)), [{ hidden: ['x'], added: ['isin'] }]);
  same(K2.colPrefs.sel('t5'), { hidden: ['x'], added: ['isin'] }, 'ответ сервера досланную правку не перекрыл');
  assert.ok(!store.has(DIRTY));
});

test('K.table: «Добавить» — колонка каталога встаёт в конец, как родная: CSV, итог, сортировка, фильтр', () => {
  const store = new Map(), { K } = load(store), { api, html } = posTable(K);
  same(headKeys(html()), ['name', 'ccy', 'ytm']);
  same(api.addedCols(), []);
  api.addCol('isin'); api.addCol('dv01');
  same(headKeys(html()), ['name', 'ccy', 'ytm', 'isin', 'dv01'], 'в конец, в порядке добавления');
  same(api.addedCols(), ['isin', 'dv01']);
  assert.strictEqual(api.colMenu.textContent, 'Колонки · добавлено 2 ▾');
  same(JSON.parse(store.get(KEY)), { hidden: [], added: ['isin', 'dv01'] });
  // CSV: заголовок с единицей, значения
  assert.strictEqual(csvHeads(api), 'Бумага,Вал,Дох.,ISIN,"DV01, тыс ₽/бп"');
  assert.ok(/RU000A\d+/.test(api.csv().split('\r\n')[3]), 'ISIN в строке CSV');
  // итог: у DV01 в каталоге есть сумма — в подвале (число со скобками: отрицательное)
  const foot = /<tfoot>[\s\S]*<\/tfoot>/.exec(html())[0];
  assert.ok(/\(11,602\)/.test(foot) && (foot.match(/<td/g) || []).length === 5, 'итог добавленной колонки и ячейки подвала по числу колонок: ' + foot);
  // сортировка и фильтр по добавленной колонке работают
  api.setSort([['dv01', 1]]);
  const dv = api.csv().split('\r\n').slice(3).map(l => +l.split(',')[l.split(',').length - 1]).filter(x => !isNaN(x) && x !== 0);
  assert.ok(dv.length > 3 && dv.every((x, i) => i === 0 || dv[i - 1] <= x), 'по возрастанию DV01');
  api.addCol('book');  // колонка со списком значений для фильтра
  const book = K.list()[0].book, n = K.list().filter(p => p.book === book).length;
  api.setFilter('book', [book]);
  assert.strictEqual(api.csv().split('\r\n').length, 3 + n + 1, 'после фильтра по книге — только её строки (' + n + ')');
  assert.ok(n < K.list().length);
  // убрали — уходит из таблицы вместе с сортировкой и фильтром
  api.removeCol('dv01'); api.removeCol('book'); api.removeCol('isin');
  same(headKeys(html()), ['name', 'ccy', 'ytm']);
  same(api.state.sort, []); assert.strictEqual(api.state.filters.book, undefined);
  assert.strictEqual(api.csv().split('\r\n').length, 3 + K.list().length + 1, 'фильтр ушёл вместе с колонкой: строки вернулись');
  assert.strictEqual(api.colMenu.textContent, 'Колонки ▾');
  assert.ok(!store.has(KEY), 'ничего не скрыто и не добавлено — выбора нет');
});

test('K.table: добавить можно только из каталога, один раз и только то, чего в таблице нет', () => {
  const { K } = load(new Map()), { api, html } = posTable(K);
  api.addCol('nosuch'); api.addCol('name'); api.addCol('ccy'); api.addCol('ytm');  // нет в каталоге / уже в таблице
  same(api.addedCols(), []);
  api.addCol('isin'); api.addCol('isin');
  same(api.addedCols(), ['isin']);
  same(headKeys(html()), ['name', 'ccy', 'ytm', 'isin']);
  // таблица не на K.cols — каталога нет, раздел «Добавить» не нужен
  const plain = table(K, { colPick: true });
  plain.api.addCol('isin'); same(plain.api.addedCols(), []);
  same(headKeys(plain.html()), ['s', 'x', 'y', 'z']);
  // свой каталог (colCatalog) у таблицы без K.cols; ключи таблицы и колонки без подписи в него не попадают
  const own = table(K, { colCatalog: [{ key: 'q', label: 'Кью', val: r => r.x * 2, num: true, sum: true }, { key: 'z', label: 'Зет' }, { key: 'nolabel' }] });
  own.api.addCol('nolabel'); own.api.addCol('z'); own.api.addCol('q');
  same(own.api.addedCols(), ['q']);
  same(headKeys(own.html()), ['s', 'x', 'y', 'z', 'q']);
  // colPick: false — ни скрыть, ни добавить; выбор в хранилище таблицу не меняет
  const store = new Map([[KEY, JSON.stringify({ added: ['isin'] })]]), { K: K2 } = load(store);
  const off = posTable(K2, { colPick: false });
  same(headKeys(off.html()), ['name', 'ccy', 'ytm']); assert.strictEqual(off.api.colMenu, null);
  // сохранённое неизвестное и уже имеющееся в таблице пропускаются, известное показывается; скрытое и добавленное — независимо
  const store2 = new Map([[KEY, JSON.stringify({ hidden: ['ytm'], added: ['removed-in-v9', 'ccy', 'zkbd', 'isin', 'zkbd'] })]]), { K: K3 } = load(store2);
  const p3 = posTable(K3);
  same(headKeys(p3.html()), ['name', 'ccy', 'zkbd', 'isin'], 'ytm скрыта, добавлены zkbd и isin (в порядке сохранения), чужое и повтор — нет');
  same(p3.api.addedCols(), ['zkbd', 'isin']); same(p3.api.hiddenCols(), ['ytm']);
  assert.strictEqual(p3.api.colMenu.textContent, 'Колонки · скрыто 1, добавлено 2 ▾');
});

test('K.table: порядок добавленных — как у всех колонок; убрали и добавили снова — в конец; «Как было» убирает и скрытое, и добавленное', () => {
  const store = new Map(), { K } = load(store), { api, html } = posTable(K);
  api.addCol('isin'); api.addCol('zkbd'); api.addCol('dv01');
  api.setVisibleOrder(['zkbd', 'name', 'ccy', 'dv01', 'ytm', 'isin']);  // перетащили мышкой
  same(headKeys(html()), ['zkbd', 'name', 'ccy', 'dv01', 'ytm', 'isin']);
  same(api.layout().order, ['zkbd', 'name', 'ccy', 'dv01', 'ytm', 'isin'], 'добавленные участвуют в порядке');
  // порядок переживает перезагрузку страницы: добавленные колонки знакомы раскладке
  const { K: K2 } = load(store), p2 = posTable(K2);
  same(headKeys(p2.html()), ['zkbd', 'name', 'ccy', 'dv01', 'ytm', 'isin'], 'после перезагрузки порядок с добавленными на месте');
  // убрали zkbd и добавили снова — в конец, а не на старое место
  p2.api.removeCol('zkbd'); same(headKeys(p2.html()), ['name', 'ccy', 'dv01', 'ytm', 'isin']);
  p2.api.addCol('zkbd'); same(headKeys(p2.html()), ['name', 'ccy', 'dv01', 'ytm', 'isin', 'zkbd']);
  // скрытая своя колонка и добавленная в одном выборе
  p2.api.setHidden('ccy', true);
  same(JSON.parse(store.get(KEY)), { hidden: ['ccy'], added: ['isin', 'dv01', 'zkbd'] });
  assert.strictEqual(p2.api.colMenu.textContent, 'Колонки · скрыто 1, добавлено 3 ▾');
  // «Как было»: исходный состав; ширина и порядок («⟲ вид») — отдельно
  p2.api.setSort([['dv01', -1]]); p2.api.setFilter('zkbd', ['x']);
  p2.api.resetCols();
  same(headKeys(p2.html()), ['name', 'ccy', 'ytm']);
  same(p2.api.addedCols(), []); same(p2.api.hiddenCols(), []);
  same(p2.api.state.sort, []); assert.strictEqual(p2.api.state.filters.zkbd, undefined);
  assert.ok(!store.has(KEY)); assert.strictEqual(p2.api.colMenu.textContent, 'Колонки ▾');
});

test('K.table: ответ сервера с добавленными колонками перерисовывает таблицу; на сервер уходят известные страницей ключи', async () => {
  const srv = { 'c99-pos': { hidden: [], added: ['isin', 'nosuch'] } };
  const { K, calls } = load(new Map(), { real: true, fetch: (url, init) => init.method ? { status: 200, body: {} } : { status: 200, body: { user: 'bob', tables: srv } } });
  const a = posTable(K);
  same(headKeys(a.html()), ['name', 'ccy', 'ytm']);
  await tick(); await tick();
  same(headKeys(a.html()), ['name', 'ccy', 'ytm', 'isin'], 'добавленная на другом компьютере колонка пришла с сервером');
  assert.strictEqual(a.api.colMenu.textContent, 'Колонки · добавлено 1 ▾');
  await a.api.addCol('zkbd');
  const put = calls.filter(c => c.method === 'PUT').map(c => JSON.parse(c.body));
  same(put, [{ hidden: [], added: ['isin', 'zkbd'] }], 'на сервер — добавленные известные страницей ключи (чужой «nosuch» не возвращается)');
  await a.api.removeCol('isin'); await a.api.removeCol('zkbd');
  assert.strictEqual(calls[calls.length - 1].method, 'DELETE', 'пусто и скрытого нет — «как было»');
});

test('мониторы: C03 — добавленные колонки поверх набора, таблицы на K.cols получают «Добавить» из K.posCols', () => {
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/added: \(\) => new Set\(add\)/.test(c03) && /avail: \(\) => Object\.keys\(C\)/.test(c03) && /K\.colPrefs\.sel\(colKey\(\)\)\.added/.test(c03), 'C03: каталог — колонки C, которых нет в наборе');
  const { K } = load(new Map());
  const cat = Object.values(K.posCols), keys = cat.map(c => c.key);
  assert.strictEqual(new Set(keys).size, keys.length, 'ключи каталога не повторяются: по ключу колонка и хранится');
  assert.ok(cat.every(c => /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/.test(c.key)), 'ключи подходят серверу');
  ['isin', 'zkbd', 'zspread', 'zdisc'].forEach(k => assert.ok(keys.includes(k), k + ' в каталоге'));
  // каждая колонка каталога рисует все позиции демо без ошибок
  const { api } = posTable(K, { columns: K.cols(['name']) });
  cat.forEach(c => { if (c.key !== 'name') api.addCol(c.key); });
  assert.strictEqual(api.addedCols().length, keys.length - 1);
  assert.ok(api.csv().split('\r\n').length >= K.list().length);
});
