// Ручные таблицы мониторов (#131): «⤓ CSV» и ширина колонок с запоминанием у таблиц, свёрстанных строкой HTML
// (prototypes/concepts/lib/kit.js, K.tableTools / K.tableToolsAll / K.tableCsv); даты в CSV (#161): колонка-дата —
// из ISO-значения без дня недели, несуществующий день (31.02) — не дата.
// Перетаскивание мышкой проверено в браузере (описание PR); здесь — то, что не зависит от вёрстки.
//   node tests/js/manual_tables.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const same = (a, b, m) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), m);  // объекты из vm — другого realm

// маленький DOM: столько, сколько нужно K.table и K.tableTools без вёрстки
function el(tag) {
  const e = { nodeType: 1, tagName: (tag || 'div').toUpperCase(), style: {}, dataset: {}, children: [], innerHTML: '', className: '', isConnected: true, parent: null, get parentElement() { return e.parent; },
    classList: { add(c) { if (!e.classList.contains(c)) e.className = (e.className + ' ' + c).trim(); }, remove(c) { e.className = e.className.split(' ').filter(x => x !== c).join(' '); },
      toggle() {}, contains(c) { return e.className.split(' ').includes(c); } },
    appendChild(c) { c.parent = e; e.children.push(c); return c; }, append(...c) { c.forEach(x => e.appendChild(x)); },
    insertBefore(c, ref) { c.parent = e; const i = e.children.indexOf(ref); e.children.splice(i < 0 ? 0 : i, 0, c); return c; },
    remove() { if (e.parent) { const i = e.parent.children.indexOf(e); if (i >= 0) e.parent.children.splice(i, 1); } },
    after(c) { if (c.parent) c.remove(); const p = e.parent; c.parent = p; p.children.splice(p.children.indexOf(e) + 1, 0, c); },
    get nextElementSibling() { const p = e.parent; return p ? p.children[p.children.indexOf(e) + 1] || null : null; },
    querySelector(sel) { const tg = { 'colgroup.k-cg': 'COLGROUP', table: 'TABLE' }[sel]; return tg ? e.children.find(c => c.tagName === tg) || null : null; },
    querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, getBoundingClientRect: () => ({ width: 80 }) };
  return e;
}
// таблица из описания строк: [[html | {h, cls, span, date, v}]]
function table(spec) {
  const T = el('table');
  const cell = (tag, c) => { const x = el(tag); if (typeof c === 'string') x.innerHTML = c; else { x.innerHTML = c.h || ''; x.className = c.cls || ''; x.colSpan = c.span || 1; if (c.date) x.dataset.date = c.date; if (c.v) x.dataset.v = c.v; if (c.k) x.dataset.k = c.k; } if (!x.colSpan) x.colSpan = 1; return x; };
  const row = (tag, cells, o) => { const r = el('tr'); r.cells = cells.map(c => cell(tag, c)); r.cells.forEach(c => r.appendChild(c)); Object.assign(r, o || {}); return r; };
  const sec = rows => rows ? { rows } : null;
  T.tHead = spec.head ? sec(spec.head.map(h => row('th', h))) : null;
  T.tBodies = [sec((spec.body || []).map(b => Array.isArray(b) ? row('td', b) : row('td', b.cells, b.tr)))];
  T.tFoot = spec.foot ? sec(spec.foot.map(f => row('td', f))) : null;
  T.rows = [].concat(T.tHead ? T.tHead.rows : [], T.tBodies[0].rows, T.tFoot ? T.tFoot.rows : []);
  const host = el('div'); host.appendChild(T);
  return T;
}
let observed = null;  // наблюдатель kit.js: [callback, target, options]
function load(store) {
  const document = { addEventListener() {}, removeEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { constructor(cb) { this.cb = cb; } observe(target, o) { observed = [this.cb, target, o]; } } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}

t('#161: дата CSV — только существующий день, с учётом високосных лет', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.csvDate('2029-02-31'), null);
  assert.strictEqual(K.csvDate('2029-04-31'), null);
  assert.strictEqual(K.csvDate('2029-02-29'), null, '2029 — не високосный');
  assert.strictEqual(K.csvDate('2028-02-29'), '02/29/2028');
  assert.strictEqual(K.csvDate('2000-02-29'), '02/29/2000', 'кратный 400 — високосный');
  assert.strictEqual(K.csvDate('2100-02-29'), null, 'кратный 100 — нет');
  assert.strictEqual(K.csvDate('2029-12-31'), '12/31/2029');
  // дата экрана с несуществующим днём не переводится в MM/DD/YYYY: остаётся текстом и защищена от Excel
  assert.strictEqual(K.csvCell('31.02.29'), '"=""31.02.29"""');
  assert.strictEqual(K.csvCell('29.02.28'), '02/29/2028');
  assert.strictEqual(K.csvCell('пут 31.02.2029'), 'пут 31.02.2029');
});

t('#161: Excel читает месяц с годом и дату со временем AM/PM — такой текст защищён; минус экрана — обычный', () => {
  const { K } = load(new Map());
  for (const x of ['12/2029', '3-2029', '1/2/2024 10:30 AM', '03/15/2029 18:30']) assert.ok(K.csvExcelConverts(x), x);
  for (const x of ['1.234.56', 'v1.2.3', '12/20299', 'пут 12/2029']) assert.ok(!K.csvExcelConverts(x), x);
  assert.strictEqual(K.csvCell('−27 Z'), '"=""-27 Z"""', 'U+2212 → «-», а текст с минусом в начале — строковой формулой');
  assert.strictEqual(K.csvCell('КС − 50'), 'КС - 50');
});

t('#161: колонка-дата K.table (date: true) — в CSV дата из ISO-значения, без дня недели', () => {
  const { K } = load(new Map());
  const WD = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const rows = [{ id: 'a', d: '2026-10-23', x: 1 }, { id: 'b', d: '2026-10-26', x: 2 }];
  const fmt = v => K.f.date(v) + ' <span class="mut3">' + WD[new Date(v + 'T00:00:00').getDay()] + '</span>';
  const api = K.table(el(), { rows, search: false, viewKey: 'c99-dates', columns: [{ key: 'd', label: 'Дата', date: true, fmt }, { key: 'x', label: 'X', num: true }] });
  const L = api.csv().split('\r\n');
  assert.strictEqual(L[3], '10/23/2026,1');
  assert.strictEqual(L[4], '10/26/2026,2');
  // без date: true день недели остаётся в тексте ячейки — так было до #161
  const old = K.table(el(), { rows, search: false, viewKey: 'c99-dates-old', columns: [{ key: 'd', label: 'Дата', fmt }] });
  assert.strictEqual(old.csv().split('\r\n')[3], '10/23/2026 пт');
  // колонка-дата без ISO-значения — обычная ячейка
  assert.strictEqual(K.csvDateCell(null, '—', false), '—');
  assert.strictEqual(K.csvDateCell('2029-02-31', '31.02.29', false), '"=""31.02.29"""');
  // C08: недельный список выгружает дату колонкой-датой
  assert.ok(/key: 'date', label: 'Дата', date: true, fmt: v => f\.date\(v\) \+ ' <span class="mut3">' \+ WD\[/.test(P('concepts/c08-calendar.html')));
});

t('#131: CSV ручной таблицы — как на экране: числа, даты, colspan, без оформления и пустых строк', () => {
  const { K } = load(new Map());
  const T = table({
    head: [['Бумага', { h: 'Дата' }, { h: 'DV01, тыс ₽/бп', cls: 'n' }, { h: 'Доля', cls: 'n' }, '']],
    body: [
      [{ h: '<b style="color:red">●</b> ОФЗ 26238' }, { h: '15.03.29 <span class="mut3">пут</span>', date: '2029-03-15' }, { h: '−1,234.5', cls: 'n' }, { h: '12%', cls: 'n' }, '<div class="k-bar"><i class="n"></i></div>'],
      [{ h: 'Итого по группе', span: 2 }, { h: '<span class="mut3">·</span>', cls: 'n' }, { h: '=1+1', cls: 'n' }, ''],
      { cells: ['скрытая', '', '', '', ''], tr: { style: { display: 'none' } } },
    ],
    foot: [['Итого', '', { h: '<span data-dr="1">-1,234.5</span>', cls: 'n' }, '', '']],
  });
  const L = K.tableCsv(T, { title: 'Тест' }).split('\r\n');
  assert.strictEqual(L[0], 'run_id,demo,as_of,10/21/2026,таблица,Тест,строк,3');
  assert.strictEqual(L[1], '');
  assert.strictEqual(L[2], 'Бумага,Дата,"DV01, тыс ₽/бп",Доля', 'колонка без заголовка и текста (полоска) не выгружается');
  assert.strictEqual(L[3], 'ОФЗ 26238,03/15/2029,-1234.5,12%', 'метка ● — оформление; дата из data-date — без подписи');
  assert.strictEqual(L[4], 'Итого по группе,,,"=""=1+1"""', 'colspan — пустой ячейкой; «·» в числе — пусто; формула обезврежена');
  assert.strictEqual(L[5], 'Итого,,-1234.5,');
  assert.strictEqual(L.length, 7, 'скрытая строка не попала; файл кончается CRLF');
  // таблица без заголовка и с «ничего не найдено»
  // и строка раскрытых подробностей (data-csv-skip, C15) не выгружается
  const T2 = table({ body: [['A', { h: '1,000', cls: 'n' }], { cells: [{ h: '<table><tr><td>счёт</td></tr></table>', span: 2 }], tr: { dataset: { csvSkip: '1' } } }, [{ h: 'ничего не найдено', cls: 'k-empty', span: 2 }]] });
  same(K.tableCsv(T2, {}).split('\r\n').slice(1), ['', 'A,1000', '']);
  // дата экрана без data-date: двузначный год — 20YY; data-v даёт век
  const T3 = table({ head: [['Д']], body: [['15.03.29'], [{ h: '15.03.99', v: '1999-03-15' }]] });
  same(K.tableCsv(T3, {}).split('\r\n').slice(3, 5), ['03/15/2029', '03/15/1999']);
});

t('#131: K.tableTools — кнопки под таблицей, ширина колонок запоминается по viewKey, сброс, повторный вызов', () => {
  const store = new Map(), { K } = load(store);
  const mk = () => table({ head: [['Книга', { h: 'MV', cls: 'n', k: 'mv' }, 'Прим.']], body: [['A', { h: '1', cls: 'n' }, 'x']] });
  let T = mk();
  const api = K.tableTools(T.parent, { viewKey: 'c99-manual', title: 'Ручная' });
  assert.strictEqual(api.viewKey, 'table:c99-manual');
  const bar = T.nextElementSibling;
  assert.ok(bar && bar.classList.contains('k-tbl-ctlbar') && bar.dataset.vk === 'table:c99-manual', 'строка кнопок — сразу после таблицы');
  same(bar.children[0].children.map(b => b.innerHTML), ['⤓ CSV', '⟲ вид']);
  assert.strictEqual(bar.children[0].children[1].style.display, 'none', 'без своей ширины «⟲ вид» скрыт');
  assert.ok(T.tHead.rows[0].cells.every(th => th.children.some(c => c.className === 'k-rsz')), 'у каждого заголовка — ручка ширины');
  assert.ok(T.classList.contains('k-rszable'));
  same(api.layout(), {});
  // ширина: ключ колонки — data-k, иначе номер; мусор и чужие колонки отброшены (K.viewLayout)
  // ключ колонки — data-k, иначе текст заголовка: другой набор колонок — ширина остаётся у своей колонки
  api.setLayout({ w: { 'h:Книга': 150, mv: 90.6, nosuch: 50, 'h:Прим.': -1 }, order: ['mv'] });
  same(JSON.parse(store.get('tdm.view.v1:table:c99-manual')), { order: ['mv'], w: { 'h:Книга': 150, mv: 91 } });
  assert.ok(T.classList.contains('k-fixed'));
  assert.strictEqual(T.style.width, (150 + 91 + 100) + 'px', 'колонка без ширины — 100px');
  const cg = T.children.find(c => c.tagName === 'COLGROUP');
  same(cg.children.map(c => c.style.width), ['150px', '91px', '100px']);
  // перерисовка страницы (новая таблица) — ширина из хранилища
  T = mk();
  const api2 = K.tableTools(T, { viewKey: 'c99-manual' });
  assert.ok(T.classList.contains('k-fixed'));
  same(T.children.find(c => c.tagName === 'COLGROUP').children.map(c => c.style.width), ['150px', '91px', '100px']);
  assert.strictEqual(T.nextElementSibling.children[0].children[1].style.display, '', '«⟲ вид» виден');
  // повторный вызов без перерисовки — та же строка кнопок, colgroup один, ручка ширины у заголовка одна
  assert.strictEqual(K.tableTools(T, { viewKey: 'c99-manual' }), api2);
  assert.strictEqual(T.parent.children.filter(c => c.classList.contains('k-tbl-ctlbar')).length, 1);
  assert.strictEqual(T.children.filter(c => c.tagName === 'COLGROUP').length, 1);
  assert.ok(T.tHead.rows[0].cells.every(th => th.children.filter(c => c.className === 'k-rsz').length === 1));
  api2.resetView();
  assert.ok(!store.has('tdm.view.v1:table:c99-manual'));
  assert.ok(!T.classList.contains('k-fixed'));
  // заголовок с colspan (матрица с группами) — только CSV, без ширины
  const G = table({ head: [['', { h: 'неделя', span: 3 }]], body: [['A', '1', '2', '3']] });
  const g = K.tableTools(G, { viewKey: 'c99-grouped' });
  assert.ok(!G.classList.contains('k-rszable'));
  g.setLayout({ w: { c0: 100 } });
  same(g.layout(), {});
  assert.strictEqual(g.csv().split('\r\n')[2], ',неделя,,');
  // K.tableToolsAll — по разметке data-vk / data-title
  const host = el(); const A = table({ head: [['a']], body: [['1']] }); host.appendChild(A); A.dataset.vk = 'c99-a'; A.dataset.title = 'Таблица A';
  host.querySelectorAll = sel => sel === 'table[data-vk]' ? [A] : [];
  const [r] = K.tableToolsAll(host);
  assert.strictEqual(r.viewKey, 'table:c99-a');
  assert.ok(r.csv().startsWith('run_id,demo,as_of,10/21/2026,таблица,Таблица A,строк,1'));
  assert.strictEqual(r.csvName(), 'tdm_c99-a_2026-10-21_run-demo.csv');
  assert.strictEqual(K.tableTools(el(), { viewKey: 'x' }), null, 'нет таблицы — ничего');
  // колонки с одинаковым заголовком и без заголовка
  same(K.colKeys(table({ head: [['', 'Δ', 'Δ', { h: 'x', k: 'kx' }]] }).tHead.rows[0].cells), ['c0', 'h:Δ', 'h:Δ#2', 'kx']);
});

t('#131: строка кнопок — после прокручиваемого контейнера (не закрывает закреплённое «Итого»), прежняя убирается', () => {
  const { K } = load(new Map());
  const panel = el(); panel.classList.add('k-panel');
  const sc = el(); sc.classList.add('scroll'); panel.appendChild(sc);
  const mk = () => { const T = table({ head: [['a']], body: [['1']], foot: [['Итого']] }); sc.children = []; sc.appendChild(T.parent); return T; };
  let T = mk();
  K.tableTools(T, { viewKey: 'c99-scroll', title: 'Т' });
  const bars = () => panel.children.filter(c => c.dataset.vk === 'table:c99-scroll');
  assert.strictEqual(bars().length, 1); assert.strictEqual(sc.nextElementSibling, bars()[0], 'сразу после контейнера, вне его');
  assert.ok(bars()[0].classList.contains('k-tbl-ctlbar-m'), 'не закреплена');
  T = mk(); K.tableTools(T, { viewKey: 'c99-scroll' });  // перерисовка содержимого контейнера
  assert.strictEqual(bars().length, 1, 'строка прежней отрисовки убрана');
  // overflow в style — тоже прокручиваемый; без прокрутки — сразу после таблицы
  assert.ok(K.scrolls(Object.assign(el(), { style: { overflow: 'auto' } })));
  assert.ok(!K.scrolls(el()));
});

t('#131: таблица с data-vk получает кнопки сама, как только попадает в страницу', () => {
  const { K } = load(new Map());
  assert.ok(observed, 'наблюдатель зарегистрирован');
  same(observed[2], { childList: true, subtree: true });
  const [cb] = observed;
  const host = el(), A = table({ head: [['a']], body: [['1']] }), B = table({ head: [['b']], body: [['2']] });
  A.dataset.vk = 'c99-auto-a'; B.dataset.vk = 'c99-auto-b'; host.appendChild(A.parent);
  host.querySelectorAll = sel => /table\[data-vk\]/.test(sel) ? [A] : [];
  cb([{ addedNodes: [host] }, { addedNodes: [B] }, { addedNodes: [{ nodeType: 3 }] }]);
  assert.ok(A.__kt && A.nextElementSibling.dataset.vk === 'table:c99-auto-a');
  assert.ok(B.__kt && B.nextElementSibling.dataset.vk === 'table:c99-auto-b');
  const before = A.__kt; cb([{ addedNodes: [host] }]);
  assert.strictEqual(A.__kt, before, 'повторно не подключается');
  // таблицу нарисовали в элемент вне страницы, потом вставили в прокручиваемый контейнер: строка кнопок переезжает за него
  const T = table({ head: [['a']], body: [['1']], foot: [['Итого']] }), box = T.parent;
  K.tableTools(T, { viewKey: 'c99-late' });
  assert.strictEqual(T.nextElementSibling.dataset.vk, 'table:c99-late');
  const panel = el(), sc = el(); sc.classList.add('scroll'); panel.appendChild(sc); sc.appendChild(box);
  box.querySelectorAll = sel => /k-tooled/.test(sel) ? [T] : [];
  cb([{ addedNodes: [box] }]);
  assert.strictEqual(sc.nextElementSibling && sc.nextElementSibling.dataset.vk, 'table:c99-late');
  assert.ok(!box.children.some(c => c.dataset.vk), 'внутри контейнера строки кнопок больше нет');
  // подписи колонок под общим заголовком с colspan (C05 «Кривые»): data-csv
  const C = table({ head: [['', '1Y', { h: 'неделя', span: 2 }]], body: [['RUONIA', '15.00', '+5', '+7']] });
  C.tHead.rows[0].cells[2].dataset.csv = 'неделя 1Y|неделя 5Y';
  assert.strictEqual(K.tableCsv(C, {}).split('\r\n')[2], ',1Y,неделя 1Y,неделя 5Y');
});

t('#131: у всех таблиц мониторов есть CSV; ключи раскладки уникальны среди K.table и ручных таблиц', () => {
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts'), seen = new Map();
  const add = (k, where) => { assert.ok(!seen.has(k), 'повтор ключа ' + k + ': ' + where + ' и ' + seen.get(k)); seen.set(k, where); };
  // таблицы, которые CSV не нужен: справка команд терминала; eventsTable C08 не вызывается
  const exempt = { 'c12-terminal.html': ['<table class="k-tbl help">'], 'c08-calendar.html': ['<table class="mini">'], 'c03-blotter.html': ['<table class="k-tbl">'] };
  const files = fs.readdirSync(dir).filter(f => /^c\d+.*\.html$/.test(f));
  files.forEach(f => {
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    [...src.matchAll(/K\.table\([^{]*\{\s*viewKey: '([a-z0-9-]+)'/g)].forEach(m => add(m[1], f));
    [...src.matchAll(/data-vk="([a-z0-9-]+)/g)].forEach(m => add(m[1], f));
    [...src.matchAll(/K\.(?:tableTools|riskMatrix|bucketTable)\([^;]*?viewKey: '([a-z0-9-]+)'/g)].forEach(m => add(m[1], f));
    // каждая таблица, собранная строкой, размечена data-vk (её подхватывает K.tableToolsAll) или в списке исключений
    [...src.matchAll(/<table\b[^>]*>/g)].forEach(m => {
      if (/data-vk=/.test(m[0])) return;
      assert.ok((exempt[f] || []).includes(m[0]), f + ': таблица без data-vk: ' + m[0]);
    });
    // после разметки таблицы на странице вызывается K.tableToolsAll
    if (/data-vk=/.test(src)) assert.ok(/K\.tableToolsAll\(/.test(src), f + ': data-vk без K.tableToolsAll');
  });
  // C03: блоттер — K.tableTools по контейнеру; общие панели (матрица рисков, кривая × бакет) — ключ страницы
  assert.ok(/K\.tableTools\(tbl, \{ viewKey: 'c03-blotter'/.test(fs.readFileSync(path.join(dir, 'c03-blotter.html'), 'utf8')));
  files.forEach(f => {
    const src = fs.readFileSync(path.join(dir, f), 'utf8');
    [...src.matchAll(/K\.(riskMatrix|bucketTable)\(([^;]*);/g)].forEach(m => assert.ok(/viewKey: '/.test(m[2]), f + ': ' + m[1] + ' без viewKey'));
  });
  assert.ok(seen.size >= 50, 'ключей найдено: ' + seen.size);
});

console.log(n + ' tests passed');
