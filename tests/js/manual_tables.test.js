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
// таблица из описания строк: [[html | {h, cls, span, date, v, k, csv}]]
function table(spec) {
  const T = el('table');
  const cell = (tag, c) => { const x = el(tag); if (typeof c === 'string') x.innerHTML = c; else { x.innerHTML = c.h || ''; x.className = c.cls || ''; x.colSpan = c.span || 1; if (c.date) x.dataset.date = c.date; if (c.v) x.dataset.v = c.v; if (c.k) x.dataset.k = c.k; if (c.csv != null) x.dataset.csv = c.csv; } if (!x.colSpan) x.colSpan = 1; return x; };
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

t('#131 + #153: CSV ручной таблицы — отрицательные с обычным минусом и в режиме «(1)», и в режиме «−1»', () => {
  const { K } = load(new Map());
  const neg = (fm, x) => fm === 'acc' ? '(' + x + ')' : '−' + x;
  const rows = fm => [[{ h: neg(fm, '1,234.5'), cls: 'n' }, { h: neg(fm, '4.44%'), cls: 'n' }, { h: neg(fm, '1.50 bn'), cls: 'n' },
    { h: '<span class="dr">' + neg(fm, '7') + '</span>', cls: 'n' }, { h: '2.5', cls: 'n' }, '(3 поз.)', 'fwd <span class="k-num' + (fm === 'acc' ? ' k-accn' : '') + '">' + neg(fm, '16.3') + '</span>, 98.399 (0.28) (2029)', { h: '94.58 ' + neg(fm, '0.12%'), cls: 'n' },
    { h: neg(fm, '1,500.00') + '<span class="mut3">mm RUB</span>', cls: 'n' }, { h: neg(fm, '2,010') + '<small>+5бп</small>', cls: 'n' },
    { h: '124<small>' + neg(fm, '1') + 'бп</small>', cls: 'n' }, { h: '·<small>' + neg(fm, '3') + 'бп</small>', cls: 'n' }]];
  // таблица нарисована в текущем режиме: K.negFmt тот же, что при отрисовке; C03 «Номинал» и C10 «кривая × бакет» —
  // число со склеенной единицей
  const csvOf = fm => { K.negFmt = fm; return K.tableCsv(table({ head: [['a', 'b', 'c', 'd', 'e', 'f', 'f2', 'g', 'h', 'i', 'j', 'k']], body: rows(fm) }), {}).split('\r\n')[3]; };
  assert.strictEqual(csvOf('acc'), '-1234.5,-4.44%,"=""-1.50 bn""",-7,2.5,(3 поз.),"fwd -16.3, 98.399 (0.28) (2029)",94.58 -0.12%,"=""-1,500.00mm RUB""","=""-2,010+5бп""",124-1бп,·-3бп');
  assert.strictEqual(csvOf('minus'), csvOf('acc'), 'одинаково в обоих режимах');
  // текст без метки k-accn — как есть в обоих режимах: скобки там поставил не формат отрицательных (Bid/Ask «98.399 (0.28)»,
  // пояснение без разметки числа)
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    assert.strictEqual(K.tableCsv(table({ head: [['a', 'b']], body: [['98.399 (0.28)', 'ставки (1.6)']] }), {}).split('\r\n')[3], '98.399 (0.28),ставки (1.6)', fm);
  }
  // в режиме «−1» скобки в числовой ячейке — не минус (их рисует не формат отрицательных)
  K.negFmt = 'minus';
  assert.strictEqual(K.tableCsv(table({ head: [['a']], body: [[{ h: '12 (3)', cls: 'n' }]] }), {}).split('\r\n')[3], '12 (3)');
  // K.table выгружает в режиме «−1» при любой настройке
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const api = K.table(el(), { rows: [{ id: 'a', x: -1234.5 }], search: false, viewKey: 'c99-neg-' + fm, columns: [{ key: 'x', label: 'X', num: true, fmt: v => K.f.n(v, 1) }] });
    assert.strictEqual(api.csv().split('\r\n')[3], '-1234.5', fm);
  }
});

t('#222: метка отрицательного для CSV (data-kneg) не зависит от цвета; скобка во вложенном теге', () => {
  const { K } = load(new Map());
  // K.d() ставит neg отрицательному: красный уступает смысловому цвету (k-accn нет), а метка data-kneg есть
  const cells = fm => fm === 'acc'
    ? ['<span class="dr neg" data-kneg="">(1.5)</span>', 'итого <span class="dr" data-kneg=""><b>(2.5)</b> mm</span>', '<span class="dr pos">(3)</span> лот', '<span data-kneg="">((1))</span>']
    : ['<span class="dr neg">−1.5</span>', 'итого <span class="dr"><b>−2.5</b> mm</span>', '<span class="dr pos">(3)</span> лот', '<span>((1))</span>'];
  const csvOf = fm => { K.negFmt = fm; return K.tableCsv(table({ head: [['a', 'b', 'c', 'd']], body: [cells(fm)] }), {}).split('\r\n')[3]; };
  assert.strictEqual(csvOf('acc'), '"=""-1.5""",итого -2.5 mm,(3) лот,((1))', 'без метки скобки — не минус; «((1))» — не число');
  assert.strictEqual(csvOf('minus'), csvOf('acc'), 'одинаково в обоих режимах');
  assert.strictEqual(K.accMarkedToMinus('<i data-kneg="">(1,234.5)</i> и <i data-kneg="">(7%)</i>'), '<i data-kneg="">-1,234.5</i> и <i data-kneg="">-7%</i>');
  assert.strictEqual(K.accMarkedToMinus('<i data-knegx="">(1)</i>'), '<i data-knegx="">(1)</i>', 'похожий атрибут — не метка');
});

t('#183: дата оферты — в CSV датой Excel, вид оферты — своей колонкой; «Сумма» C08 — числом', () => {
  const { K } = load(new Map());
  assert.ok(K.posCols.offer.date, 'колонка «Оферта» — колонка-дата');
  const rows = [{ id: 'a', offer: '2029-03-15', offerKind: 'put' }, { id: 'b', offer: '2030-01-31', offerKind: 'call' }, { id: 'c', offer: null, offerKind: null }];
  const api = K.table(el(), { rows, search: false, viewKey: 'c99-offer', columns: K.cols(['offer', 'offerKind']) });
  same(api.csv().split('\r\n').slice(2, 6), ['Оферта,Вид оферты', '03/15/2029,пут', '01/31/2030,колл', '—,—']);
  assert.ok(!/['"]P['"]/.test(String(K.posCols.offer.fmt)), 'на экране дата без «P/C»');
  // C08 «Все потоки по неделям»: сумма — число (mm в валюте выпуска), валюта — в колонке «Вал»
  const c08 = P('concepts/c08-calendar.html');
  const amt = /\{ key: 'amt', label: 'Сумма', num: true, unit: 'mm в вал.', fmt: \(v, r\) => (.*?), csvFmt: v => f\.mn\(v, 2\) \}/.exec(c08);
  assert.ok(amt, 'C08: у «Суммы» — единица и csvFmt');
  const fl = K.table(el(), { rows: [{ id: 'a', ccy: 'RUB', amt: 27.74e6 }], search: false, viewKey: 'c99-amt',
    columns: [{ key: 'ccy', label: 'Вал', fmt: (v, r) => K.ccyTag(r.ccy) }, { key: 'amt', label: 'Сумма', num: true, unit: 'mm в вал.', fmt: (v, r) => K.f.mn(v, 2) + '<span class="mut3">mm ' + r.ccy + '</span>', csvFmt: v => K.f.mn(v, 2) }] });
  same(fl.csv().split('\r\n').slice(2, 4), ['Вал,"Сумма, mm в вал."', 'RUB,27.74']);
  // C08 «Оферты и коллы»: дата — data-date, вид — своя колонка
  assert.ok(/<th>Дата<\/th><th>Вид<\/th><th class="n">Дней<\/th>/.test(c08));
  assert.ok(/<td data-date="' \+ esc\(p\.event\.date\) \+ '">' \+ f\.date\(p\.event\.date\) \+ '<\/td><td class="mut3">' \+ K\.offerKindText\(p\.event\.kind\) \+ '<\/td>/.test(c08));
  // C03: «Вид оферты» рядом с «Офертой» в наборах колонок, дата — data-date
  const c03 = P('concepts/c03-blotter.html');
  assert.strictEqual((c03.match(/'offer', 'offerKind', 'maturity'/g) || []).length, 3);
  assert.ok(/\(c\.date && v \? ' data-date="' \+ esc\(v\) \+ '"' : ''\)/.test(c03));
  const T = table({ head: [['Оферта', 'Вид оферты']], body: [[{ h: '15.03.29', date: '2029-03-15' }, 'пут']] });
  assert.strictEqual(K.tableCsv(T, {}).split('\r\n')[3], '03/15/2029,пут');
});

t('#184: таблицы карточки позиции и разбора — CSV и ширина колонок', () => {
  const { K } = load(new Map());
  const p = { id: 'P1', isin: 'RU000TEST001', name: 'Тест 1', ccy: 'RUB' };
  // потоки: класс для поиска, дата — data-date
  const html = K.flowsTable(p, [{ date: '2027-03-15', kind: 'cpn', amount: 1000, amountRub: 1e6 }]);
  assert.ok(/<table class="k-tbl k-cflows">/.test(html) && /<td data-date="2027-03-15">15\.03\.27<\/td>/.test(html));
  const legs = K.legsHtml({ legs: [{ leg: 'near', valueDate: '2026-11-02', buyCcy: 'USD', buyAmount: 1e6, sellCcy: 'RUB', sellAmount: 9e7, contractRate: 90 }] });
  assert.ok(/<table class="k-tbl k-compact k-legs">/.test(legs) && /data-date="2026-11-02"/.test(legs));
  // K.cardTableTools: кнопки у каждой найденной таблицы карточки; имя файла — с ISIN, название — с именем позиции
  const T = table({ head: [['Дата', 'Тип']], body: [[{ h: '15.03.27', date: '2027-03-15' }, 'купон']] });
  const root = { querySelector: sel => sel === 'table.k-cflows' ? T : null };
  K.cardTableTools(root, p);
  assert.strictEqual(T.__kt.viewKey, 'table:card-flows');
  assert.strictEqual(T.__kt.csvName(), 'tdm_card-flows_RU000TEST001_2026-10-21_run-demo.csv');
  assert.ok(T.__kt.csv().startsWith('run_id,demo,as_of,10/21/2026,таблица,Потоки: Тест 1,строк,1'));
  assert.strictEqual(T.__kt.csv().split('\r\n')[3], '03/15/2027,купон');
  same(K.CARD_TABLES.map(x => x[1]), ['card-legs', 'card-curve-risk', 'card-flows']);
  const kit = P('concepts/lib/kit.js'), c03 = P('concepts/c03-blotter.html');
  assert.ok(/K\.cardTableTools\(o\.bd, p\);/.test(kit), 'полная карточка');
  assert.ok(/K\.cardTableTools\(card, p\);/.test(c03), 'карточка C03');
  assert.ok(/K\.tableTools\(body, \{ viewKey: 'drill-' \+ dim,/.test(kit), 'разбор: ключ ширины — свой у каждого разреза');
  // C16 «ОВП по книгам»: число в тексте «Слагаемых» размечено k-num — его знак виден CSV в обоих режимах (#222)
  assert.ok(/' <span class="k-num">' \+ f\.s\(x\.components\[k\], 0\) \+ '<\/span>'/.test(P('concepts/c16-ocp.html')));
});

t('#131 + #105: CSV ручной таблицы — единица второй строкой шапки через запятую, без повтора; валюта отчёта', () => {
  const head = [['Книга', { h: 'DV01<span class="k-thu">тыс ₽/бп</span>', cls: 'n' }, { h: 'MV, mm ₽<span class="k-thu">mm ₽</span>', cls: 'n' }, { h: 'Итого, mm ₽', cls: 'n' }]];
  const body = [['A', { h: '1', cls: 'n' }, { h: '2', cls: 'n' }, { h: '3', cls: 'n' }]];
  const rub = load(new Map()).K;
  const L = rub.tableCsv(table({ head, body }), { title: 'Кэрри, тыс ₽' }).split('\r\n');
  assert.strictEqual(L[2], 'Книга,"DV01, тыс ₽/бп","MV, mm ₽","Итого, mm ₽"', 'единица не задваивается');
  assert.ok(L[0].includes('таблица,"Кэрри, тыс ₽"'));
  // отчёт в долларах: «₽» в подписях и названии — символ валюты отчёта (K.u)
  const usd = load(new Map([['concepts-repr', 'USD']])).K;
  assert.strictEqual(usd.repr, 'USD');
  const U = usd.tableCsv(table({ head, body }), { title: 'Кэрри, тыс ₽' }).split('\r\n');
  assert.strictEqual(U[2], 'Книга,"DV01, тыс $/бп","MV, mm $","Итого, mm $"');
  assert.ok(U[0].includes('таблица,"Кэрри, тыс $"'));
  assert.ok(!U.join('\n').includes('₽'));
  // ключ ширины — без единицы и с «¤» вместо символа валюты: не зависит от валюты отчёта
  same(usd.colKeys(table({ head }).tHead.rows[0].cells), ['h:Книга', 'h:DV01', 'h:MV, mm ¤', 'h:Итого, mm ¤']);
  same(usd.colKeys(table({ head: [['Книга', 'MV, mm $']] }).tHead.rows[0].cells), ['h:Книга', 'h:MV, mm ¤'], 'символ уже подменён на странице — тот же ключ');
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

t('#131: строка кнопок — сразу после таблицы в её контейнере (сетка страницы не меняется), «Итого» — над ней', () => {
  const { K } = load(new Map());
  // C03: прокручиваемый блоттер — ячейка сетки страницы; строка кнопок остаётся внутри него, рядом с таблицей
  const grid = el(), side = el(), sc = el(); sc.classList.add('c3-tbl'); grid.append(sc, side);
  const T = table({ head: [['a']], body: [['1']], foot: [['Итого']] }); sc.children = []; sc.appendChild(T);
  K.tableTools(T, { viewKey: 'c99-grid' });
  same(grid.children.map(c => c === sc ? 'scroller' : c === side ? 'side' : 'other'), ['scroller', 'side'], 'у сетки те же ячейки');
  assert.strictEqual(T.nextElementSibling, T.__kt.bar, 'строка — сразу после таблицы');
  assert.strictEqual(T.__kt.bar.parent, sc);
  // «Итого» закреплено над строкой кнопок, а строка у коротких таблиц брифинга — просто под таблицей
  const css = P('concepts/lib/kit.css');
  assert.ok(/\.k-tbl-ctlbar-m \{ height: 24px;/.test(css) && /table\.k-tooled tfoot td \{ bottom: 24px; \}/.test(css));
  assert.ok(/table\.mini \+ \.k-tbl-ctlbar-m \{ position: static;/.test(css));
  // длинные mini-таблицы в прокрутке (C08 «Оферты и коллы», «Крупные погашения», C13 «События эмитента», #223) — строка закреплена
  assert.ok(/\.k-panel > \.bd\.scroll > table\.mini \+ \.k-tbl-ctlbar-m, \.k-drawer > \.bd > table\.mini \+ \.k-tbl-ctlbar-m \{ position: sticky;/.test(css));
  assert.ok(!/^\.k-tbl-ctlbar-m \{[^}]*position/m.test(css), 'в прокручиваемом контейнере строка закреплена, как у K.table');
});

t('#131: таблица с data-vk получает кнопки сама; ушла со страницы — строка кнопок уходит с ней', () => {
  const { K } = load(new Map());
  assert.ok(observed, 'наблюдатель зарегистрирован');
  same(observed[2], { childList: true, subtree: true });
  const [cb] = observed;
  const host = el(), A = table({ head: [['a']], body: [['1']] }), B = table({ head: [['b']], body: [['2']] });
  A.dataset.vk = 'c99-auto-a'; B.dataset.vk = 'c99-auto-b'; host.appendChild(A.parent);
  host.querySelectorAll = sel => /table\[data-vk\]/.test(sel) ? [A] : [];
  cb([{ addedNodes: [host], removedNodes: [] }, { addedNodes: [B], removedNodes: [] }, { addedNodes: [{ nodeType: 3 }], removedNodes: [] }]);
  assert.ok(A.__kt && A.nextElementSibling.dataset.vk === 'table:c99-auto-a');
  assert.ok(B.__kt && B.nextElementSibling.dataset.vk === 'table:c99-auto-b');
  const before = A.__kt; cb([{ addedNodes: [host], removedNodes: [] }]);
  assert.strictEqual(A.__kt, before, 'повторно не подключается');
  // смена режима «таблица → график» убрала только таблицу: строка кнопок (её CSV — скрытая таблица) уходит тоже
  const box = A.parent, bar = A.__kt.bar;
  A.remove(); A.isConnected = false;
  cb([{ addedNodes: [], removedNodes: [A] }]);
  assert.ok(!box.children.includes(bar), 'строки кнопок без таблицы нет');
  // таблица на месте (перенос внутри страницы) — строка остаётся
  const bbar = B.__kt.bar; cb([{ addedNodes: [], removedNodes: [B] }]);
  assert.ok(B.parent.children.includes(bbar));
  // подписи колонок под общим заголовком с colspan (C05 «Кривые»): data-csv
  const C = table({ head: [['', '1Y', { h: 'неделя', span: 2 }]], body: [['RUONIA', '15.00', '+5', '+7']] });
  C.tHead.rows[0].cells[2].dataset.csv = 'неделя 1Y|неделя 5Y';
  assert.strictEqual(K.tableCsv(C, {}).split('\r\n')[2], ',1Y,неделя 1Y,неделя 5Y');
});

t('#131: у всех таблиц мониторов есть CSV; ключи раскладки уникальны среди K.table и ручных таблиц', () => {
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts'), seen = new Map();
  const add = (k, where) => { assert.ok(!seen.has(k), 'повтор ключа ' + k + ': ' + where + ' и ' + seen.get(k)); seen.set(k, where); };
  // таблицы, которые CSV не нужен: справка команд терминала
  const exempt = { 'c12-terminal.html': ['<table class="k-tbl help">'], 'c03-blotter.html': ['<table class="k-tbl">'] };
  // общие таблицы kit.js (#184: карточка позиции, разбор; панели) — ключи не совпадают с ключами страниц
  const kit = fs.readFileSync(path.join(dir, 'lib', 'kit.js'), 'utf8');
  [...kit.matchAll(/K\.tableTools\([^;]*?viewKey: '([a-z0-9-]+)'/g)].forEach(m => add(m[1], 'kit.js'));
  [...kit.matchAll(/\['table\.[a-z-]+', '([a-z0-9-]+)'/g)].forEach(m => add(m[1], 'kit.js K.CARD_TABLES'));
  assert.ok(['drill-', 'card-legs', 'card-curve-risk', 'card-flows'].every(k => seen.has(k)), 'ключи kit.js найдены');
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

t('#258: значок фильтра и метка сортировки в заголовке — не в CSV', () => {
  const { K } = load(new Map());
  const T = table({ head: [['Эмитент' + K.fltIcon({ key: 'issuer', filter: true }, { issuer: new Set(['x']) }), 'Вал' + K.fltIcon({ key: 'ccy', filter: true }, {}) + '<span class="si">▲1</span>', 'Бумага']], body: [['A', 'RUB', 'Б']] });
  assert.strictEqual(K.tableCsv(T, {}).split('\r\n')[2], 'Эмитент,Вал,Бумага');
  // C03: заголовок блоттера — со значком фильтра (K.fltIcon) и меткой сортировки (span.si)
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/K\.fltIcon\(c, st\.filters\) \+ '<\/th>'/.test(c03) && /'<span class="si">'/.test(c03));
});

t('#258: число с единицей и валютой — в CSV числом, единица в заголовке, валюта своей колонкой', () => {
  const { K } = load(new Map());
  const attr = s => K.htmlText(/ data-csv="([^"]*)"/.exec(s)[1]);  // значение атрибута, как его отдаёт dataset
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const v = attr(K.csvv(() => [K.f.mn(-1.25e9, 0), 'RUB']));
    assert.strictEqual(v, '-1,250|RUB', fm + ': значения data-csv — в формате «−1»');
    // ручная таблица: заголовок «Номинал, mm в вал.|Вал» — две колонки CSV, ячейка с data-csv — число и валюта
    const T = table({ head: [['Дата', { h: 'Номинал', cls: 'n', csv: 'Номинал, mm в вал.|Вал' }, { h: 'Цена', cls: 'n' }]],
      body: [[{ h: '11.03.27', date: '2027-03-11' }, { h: K.f.mn(-1.25e9, 0) + '<span class="mut3">mm RUB</span>', cls: 'n', csv: v }, { h: '99.5', cls: 'n' }],
        [{ h: '20.04.27', date: '2027-04-20' }, { h: '450<span class="mut3">mm USD</span>', cls: 'n', csv: '450|USD' }, { h: '—', cls: 'n' }]],
      foot: [['Итого', { h: '', cls: 'n' }, { h: '', cls: 'n' }]] });
    same(K.tableCsv(T, {}).split('\r\n').slice(2, 6), ['Дата,"Номинал, mm в вал.",Вал,Цена', '03/11/2027,-1250,RUB,99.5', '04/20/2027,450,USD,', 'Итого,,,'], fm);
    // K.table: «Номинал» позиций (K.posCols.notional) — как «Сумма» C08 (#257): число в mm, валюта — в колонке «Вал»
    const api = K.table(el(), { rows: [{ id: 'a', ccy: 'RUB', notional: -9e9 }, { id: 'b', ccy: 'USD', notional: 25e6 }], search: false, viewKey: 'c99-ntl-' + fm, columns: K.cols(['ccy', 'notional']) });
    same(api.csv().split('\r\n').slice(2, 5), ['Вал,"Номинал, mm в вал."', 'RUB,-9000.00', 'USD,25.00'], fm);
    // ноги сделки в карточке: «Покупка» и «Продажа» — число в mm и валюта отдельно
    const legs = K.legsHtml({ legs: [{ leg: 'near', valueDate: '2026-11-02', buyCcy: 'USD', buyAmount: 1e6, sellCcy: 'RUB', sellAmount: 9e7, contractRate: 90 }, { leg: 'far', buyCcy: 'RUB', buyAmount: null, sellCcy: 'USD', sellAmount: -1.5e6 }] });
    assert.ok(legs.includes('<th class="n" data-csv="Покупка, mm в вал.|Вал покупки">Покупка</th><th class="n" data-csv="Продажа, mm в вал.|Вал продажи">Продажа</th>'), fm);
    assert.ok(legs.includes(' data-csv="1.00|USD">1.00 <small>mm USD</small>') && legs.includes(' data-csv="90.00|RUB">'), fm);
    assert.ok(legs.includes(' data-csv="|RUB">—') && legs.includes(' data-csv="-1.50|USD">'), fm + ': пустая сумма — пустая ячейка, отрицательная — с минусом');
    // курс сделки — числом, котировка — своей колонкой; расчёт и фиксинг — подписи своими колонками, фиксинг — датой
    const ndf = K.legsHtml({ legs: [{ leg: 'single', valueDate: '2026-11-21', buyCcy: 'CNY', buyAmount: 3e8, sellCcy: 'RUB', sellAmount: 3.459e9, contractRate: 11.53, rateQuote: 'CNY/RUB',
      settlement: 'cash', settlementCcy: 'RUB', fixingDate: '2026-11-19', fixingSource: 'ЦБ', fixingRate: 11.41 }] });
    assert.ok(ndf.includes('<th class="n" data-csv="Курс сделки|Котировка">Курс сделки</th>') && ndf.includes(' data-csv="11.5300|CNY/RUB">11.5300 <small>CNY/RUB</small>'), fm);
    assert.ok(ndf.includes('<th data-csv="Расчёт|Вал расчёта">Расчёт</th>') && ndf.includes(' data-csv="расчётный (NDF)|RUB">'), fm);
    assert.ok(ndf.includes('<th class="n" data-csv="Фиксинг|Источник фиксинга|Курс фиксинга">Фиксинг</th>') && ndf.includes(' data-date="2026-11-19" data-csv="19.11.26|ЦБ|11.4100">'), fm);
  }
  // ячейка с data-csv и data-date: первое значение — датой, лишние значения сверх колонок заголовка отбрасываются
  const F = table({ head: [[{ h: 'Фиксинг', cls: 'n', csv: 'Фиксинг|Источник фиксинга|Курс фиксинга' }, { h: 'Курс сделки', cls: 'n', csv: 'Курс сделки|Котировка' }]],
    body: [[{ h: '19.11.26 ЦБ 11.4100', cls: 'n', date: '2026-11-19', csv: '19.11.26|ЦБ|11.4100' }, { h: '11.5300 CNY/RUB', cls: 'n', csv: '11.5300|CNY/RUB|лишнее' }]] });
  same(K.tableCsv(F, {}).split('\r\n').slice(2, 4), ['Фиксинг,Источник фиксинга,Курс фиксинга,Курс сделки,Котировка', '11/19/2026,ЦБ,11.4100,11.5300,CNY/RUB']);
  K.negFmt = 'acc';
  // страницы: C08 «Оферты и коллы», C13 «События эмитента», C03 «Номинал», разбор вклада
  const c08 = P('concepts/c08-calendar.html'), c13 = P('concepts/c13-issuers.html'), c03 = P('concepts/c03-blotter.html'), kit = P('concepts/lib/kit.js');
  assert.ok(c08.includes('<th class="n" data-csv="Номинал, mm в вал.|Вал">Номинал</th>') && c08.includes(`<td class="n"' + K.csvv(() => [f.mn(p.notional, 0), p.ccy]) + '>' + f.mn(p.notional, 0) + '<span class="mut3">mm '`));
  assert.ok(c13.includes('<th class="n" data-csv="Сумма, mm в вал.|Вал">Сумма</th>') && c13.includes(`<td class="n"' + K.csvv(() => [f.mn(c.amount, 1), c.ccy]) + '>'`));
  assert.ok(/notional: \{[^\n]*csvHead: 'Номинал, mm в вал.', csvFmt: v => f\.mn\(v, 2\)/.test(c03) && c03.includes("(c.csvHead ? ' data-csv=\"' + esc(c.csvHead) + '\"' : '')") && c03.includes('(c.csvFmt ? K.csvv(() => c.csvFmt(v, r)) : \'\')'));
  assert.ok(kit.includes('<th class="n" title="в валюте позиции" data-csv="Номинал, mm в вал.">Номинал</th>') && kit.includes(`<td class="n"' + K.csvv(() => f.mn(x.p.notional, 2)) + '>'`), 'разбор вклада');
});

console.log(n + ' tests passed');
