// Таблицы мониторов (prototypes/concepts/lib/kit.js, K.table): раскладка колонок по таблице (#102) — хранение в
// K.viewStore (get / set / reset, сейчас localStorage), порядок колонок из сохранённой раскладки, сброс вида;
// выгрузка в CSV (#110): run_id и as_of, единицы в заголовках, числа для Excel с русской локалью.
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
const columns = () => [{ key: 's', label: 'Имя', filter: true }, { key: 'x', label: 'Икс', num: true, sum: true, unit: 'тыс ₽' }, { key: 'y', label: 'Игрек, %', num: true, fmt: v => v == null ? '—' : v.toFixed(2) + '%' }];

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

t('#102: ключ таблицы — без списка колонок; opts.viewKey — явный; все мониторы передают свой viewKey', () => {
  const { K } = load(new Map());
  const a = el(); a.id = 'list';
  assert.strictEqual(K.tableKey(a, { columns: columns() }), 'table:c99.html|list');
  assert.strictEqual(K.tableKey(a, { columns: columns().slice(1) }), 'table:c99.html|list', 'другой набор колонок — тот же ключ');
  assert.strictEqual(K.tableKey(a, { columns: columns(), viewKey: 'mine' }), 'table:mine');
  // у нескольких таблиц в одной панели без id ключ «страница + id» совпал бы: каждый вызов K.table на мониторах
  // передаёт свой viewKey, и ключи не повторяются
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts'), seen = new Set();
  fs.readdirSync(dir).filter(f => /^c\d+.*\.html$/.test(f)).forEach(f => {
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    [...src.matchAll(/K\.table\([^{]*\{\s*(viewKey: '([a-z0-9-]+)')?/g)].forEach(m => {
      assert.ok(m[2], f + ': K.table без viewKey');
      assert.ok(!seen.has(m[2]), 'повтор viewKey ' + m[2]); seen.add(m[2]);
    });
  });
  assert.ok(seen.size >= 16, 'вызовы найдены: ' + seen.size);
});

t('#102: раскладка из хранилища проверяется: мусор и старый формат не ломают таблицу', () => {
  const { K } = load(new Map());
  const keys = ['s', 'x', 'y'];
  for (const bad of [5, 'abc', null, [], [1, 2], true, { order: 'abc' }, { w: 5 }, { w: [] }, { order: [1, null, {}] }])
    same(K.viewLayout(bad, keys), {}, JSON.stringify(bad));
  same(K.viewLayout({ order: ['y', 'nosuch', 'y', 7, 's'], w: { nosuch: 50, x: 120.4, y: -5, s: 'a', z: Infinity } }, keys),
    { order: ['y', 's'], w: { x: 120 } }, 'неизвестные колонки и плохие ширины отброшены');
  same(K.viewLayout({ w: { nosuch: 50 } }, keys), {}, 'ширины только чужих колонок — не фиксированная раскладка');
  same(K.viewLayout({ w: { x: 1, y: 1e9 } }, keys), { w: { x: 32, y: 2000 } }, 'ширины в разумных пределах');
  // таблица с мусором в хранилище рисуется в исходном виде, ширину потом задать можно
  for (const raw of ['5', '"abc"', '{"w":{"nosuch":50}}', '{"order":"abc"}', 'not json']) {
    const store = new Map([['tdm.view.v1:table:c99.html|tb', raw]]), { K: K2 } = load(store);
    const box = el(); box.id = 'tb';
    const api = K2.table(box, { rows, search: false, columns: columns() });
    same(headKeys(box.children[0].innerHTML), ['s', 'x', 'y'], raw);
    assert.ok(box.children[0].innerHTML.includes('<table class="k-tbl">'), raw + ': не фиксированная раскладка');
    assert.doesNotThrow(() => api.setLayout({ w: { x: 90 } }), raw);
    assert.ok(box.children[0].innerHTML.includes('<col data-k="x" style="width:90px">'), raw);
  }
});

t('#102: порядок из сохранённой раскладки; добавленная колонка — в конце, пропавшая — забыта; сброс', () => {
  const store = new Map(), { K } = load(store);
  const box = el(); box.id = 'tb';
  // раскладку сохранили, когда колонки y ещё не было, а была колонка old
  store.set('tdm.view.v1:table:c99.html|tb', JSON.stringify({ order: ['x', 'old', 's'], w: { x: 120, s: 200, old: 70 } }));
  const api = K.table(box, { rows, search: false, columns: columns() });
  const html = () => box.children[0].innerHTML;
  same(headKeys(html()), ['x', 's', 'y'], 'новая колонка y — в конце');
  same(api.layout(), { order: ['x', 's'], w: { x: 120, s: 200 } }, 'old забыта');
  assert.ok(html().includes('<table class="k-tbl k-fixed" style="width:420px">'), 'новая колонка — 100 px, ширина таблицы — по видимым');
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

t('#102: колонка с итогом на первом месте — итог в подвале и в строках групп не пропадает', () => {
  const { K } = load(new Map());
  const box = el(); box.id = 'tb';
  const R = [{ id: 'a', g: 'A', x: 1000, s: 'один', y: 1 }, { id: 'b', g: 'A', x: 2000, s: 'два', y: 2 }, { id: 'c', g: 'B', x: 4000, s: 'три', y: 3 }];
  const api = K.table(box, { rows: R, search: false, groupBy: [{ key: r => r.g, span: 2 }], columns: columns() });
  const html = () => box.children[0].innerHTML;
  const tds = tr => [...tr.matchAll(/<td[^>]*>[\s\S]*?<\/td>/g)].map(m => m[0]);
  const foot = () => tds(/<tfoot><tr>([\s\S]*?)<\/tr><\/tfoot>/.exec(html())[1]);
  const grp = () => [...html().matchAll(/<tr class="grp l0"[^>]*>([\s\S]*?)<\/tr>/g)].map(m => tds(m[1]));
  // исходный порядок s, x, y: подпись в s; span 2 у группы урезан до колонок без итога — итог x виден
  assert.ok(/Итого/.test(foot()[0]) && />7,000</.test(foot()[1]), foot().join(' '));
  assert.ok(/colspan="1"/.test(grp()[0][0]) && />3,000</.test(grp()[0][1]), 'группа A: итог x ' + grp()[0].join(' '));
  // x — первой: итог x на месте, «Итого» и подпись группы — в первой колонке без итога (s)
  api.setLayout({ order: ['x', 's', 'y'] });
  same(headKeys(html()), ['x', 's', 'y']);
  assert.ok(/class="n[^"]*">7,000</.test(foot()[0]), 'итог x в подвале: ' + foot()[0]);
  assert.ok(/Итого/.test(foot()[1]));
  assert.strictEqual(foot().length, 3);
  const g = grp();
  assert.ok(/>3,000</.test(g[0][0]) && />4,000</.test(g[1][0]), 'итоги x в группах: ' + g.map(x => x[0]).join(' '));
  assert.ok(/colspan="2"/.test(g[0][1]) && /A/.test(g[0][1]), 'подпись группы — в s, span по s и y (у y итога нет)');
  assert.strictEqual(g[0].length, 2);
});

t('#110: CSV — run_id и as_of в первой строке и имени файла, единицы в заголовках, порядок колонок как на экране', () => {
  const { M, K } = load(new Map());
  const box = el(); box.id = 'csv';
  const api = K.table(box, { rows, search: false, title: 'Позиции · тест', sort: [['x', 1]], columns: columns() });
  api.setLayout({ order: ['y', 's', 'x'] });
  const L = api.csv().split('\r\n');
  assert.strictEqual(L[0], 'run_id;demo;as_of;' + M.asOf + ';таблица;Позиции · тест;строк;2');
  assert.strictEqual(L[1], '');
  assert.strictEqual(L[2], 'Игрек, %;Имя;Икс, тыс ₽', 'единица из unit, если её нет в подписи');
  // сортировка по x: −1234.5 раньше 1; числа — с десятичной запятой, без разделителей тысяч; «—» — пусто;
  // текст с «;» и кавычками — в кавычках
  assert.strictEqual(L[3], ';"два; ""три""";-1234,5');
  assert.strictEqual(L[4], '2,50%;один;1');
  assert.strictEqual(L[5], '');
  assert.strictEqual(L.length, 6);
  assert.strictEqual(api.csvName(), 'tdm_c99_csv_' + M.asOf + '_run-demo.csv');
  const box2 = el();
  assert.strictEqual(K.table(box2, { rows, search: false, viewKey: 'c99-list', columns: columns() }).csvName(), 'tdm_c99-list_' + M.asOf + '_run-demo.csv', 'имя — по viewKey');
  // прогон из базы: номер прогона
  M.run = { id: 4321 };
  assert.ok(api.csv().startsWith('run_id;4321;as_of;'));
  assert.ok(api.csvName().endsWith('_run-4321.csv'));
  // фильтр — выгружаются только видимые строки
  api.setFilter('s', ['один']);
  same(api.csv().split('\r\n').slice(3), ['2,50%;один;1', ''], 'только строки после фильтра');
});

t('#110: ячейки CSV — числа экрана в числа Excel, формулы обезврежены', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.csvCell('−1,234,567.89', true), '-1234567,89');
  assert.strictEqual(K.csvCell('+12', true), '12');
  assert.strictEqual(K.csvCell('4.44%', true), '4,44%');
  assert.strictEqual(K.csvCell('1.1г', true), '1.1г', 'число с единицей в ячейке — текстом');
  assert.strictEqual(K.csvCell('·', true), '');
  assert.strictEqual(K.csvCell('=HYPERLINK("x")'), '"\'=HYPERLINK(""x"")"');
  assert.strictEqual(K.csvCell('@SUM(A1)'), "'@SUM(A1)");
  assert.strictEqual(K.csvCell('● Минфин'), 'Минфин', 'цветная метка — не данные');
  assert.strictEqual(K.htmlText('<b style="x">●</b> Минфин &amp; Ко <span class="mut3">RUB</span>'), '● Минфин & Ко RUB');
  assert.strictEqual(K.htmlText('<span title="a > b">MV</span>'), 'MV', '«>» внутри атрибута');
});

console.log(n + ' tests passed');
