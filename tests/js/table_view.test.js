// Таблицы мониторов (prototypes/concepts/lib/kit.js, K.table): раскладка колонок по таблице (#102) — хранение в
// K.viewStore (get / set / reset, сейчас localStorage), порядок колонок из сохранённой раскладки, сброс вида.
// Перетаскивание мышкой проверено в браузере (описание PR); здесь — то, что не зависит от вёрстки.
//   node tests/js/table_view.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const same = (a, b, m) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), m);  // объекты из vm — другого realm

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(store, ls) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const localStorage = ls || { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const headKeys = html => [...html.matchAll(/<th class="sortable[^"]*" data-k="([^"]+)"/g)].map(m => m[1]);
const rows = [{ id: 'a', x: 1, y: 2.5, s: 'один' }, { id: 'b', x: -1234.5, y: null, s: 'два; "три"' }];
const columns = () => [{ key: 's', label: 'Имя' }, { key: 'x', label: 'Икс', num: true, sum: true, unit: 'тыс ₽' }, { key: 'y', label: 'Игрек, %', num: true, fmt: v => v == null ? '—' : v.toFixed(2) + '%' }];

t('#102: viewStore — get/set/reset поверх localStorage, недоступное хранилище не ломает страницу', () => {
  const store = new Map(), { K } = load(store);
  assert.strictEqual(K.viewStore.get('k'), null);
  K.viewStore.set('k', { order: ['b', 'a'] });
  same(JSON.parse(store.get('tdm.view.v1:k')), { order: ['b', 'a'] });
  const v = K.viewStore.get('k'); v.order.push('c');
  same(K.viewStore.get('k').order, ['b', 'a'], 'get отдаёт копию');
  K.viewStore.reset('k');
  assert.strictEqual(K.viewStore.get('k'), null); assert.ok(!store.has('tdm.view.v1:k'));
  // хранилище бросает (приватное окно, запрет сайта) — вид живёт до перезагрузки, ошибок нет
  const no = () => { throw new Error('denied'); };
  const { K: K2 } = load(null, { getItem: no, setItem: no, removeItem: no });
  assert.doesNotThrow(() => K2.viewStore.set('z', { a: 1 }));
  same(K2.viewStore.get('z'), { a: 1 });
  assert.doesNotThrow(() => K2.viewStore.reset('z'));
  assert.strictEqual(K2.viewStore.get('z'), null);
});

t('#102: ключ таблицы стабилен (страница, якорь, колонки); opts.viewKey — явный', () => {
  const { K } = load(new Map());
  const a = el(); a.id = 'list';
  assert.strictEqual(K.tableKey(a, { columns: columns() }), 'table:c99.html|list|s,x,y');
  assert.strictEqual(K.tableKey(a, { columns: columns(), viewKey: 'mine' }), 'table:mine');
});

t('#102: порядок колонок из сохранённой раскладки, новые колонки — в конце, сброс возвращает исходный', () => {
  const store = new Map(), { K } = load(store);
  const box = el(); box.id = 'tb';
  store.set('tdm.view.v1:table:c99.html|tb|s,x,y', JSON.stringify({ order: ['y', 's'] }));
  const api = K.table(box, { rows, search: false, columns: columns() });
  const html = () => box.children[0].innerHTML;
  same(headKeys(html()), ['y', 's', 'x']);
  api.setLayout({ order: ['x', 'y', 's'], w: { x: 120, y: 80, s: 200 } });
  same(headKeys(html()), ['x', 'y', 's']);
  assert.ok(html().includes('<table class="k-tbl k-fixed" style="width:400px">'), 'ширины — фиксированная раскладка');
  assert.ok(html().includes('<col data-k="x" style="width:120px">'));
  same(JSON.parse(store.get('tdm.view.v1:' + api.viewKey)).w, { x: 120, y: 80, s: 200 });
  api.resetView();
  same(headKeys(html()), ['s', 'x', 'y']);
  assert.ok(!store.has('tdm.view.v1:' + api.viewKey));
  assert.ok(html().includes('<table class="k-tbl">'));
});

console.log(n + ' tests passed');
