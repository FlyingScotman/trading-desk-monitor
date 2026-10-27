// Неполные итоги (#335, решение владельца 27.10 — вариант «а») в мониторах (prototypes/concepts/), без браузера:
//  - итог, посчитанный не у всех позиций, — число (сумма посчитанных) и пометка «· без N из M поз.»; какие позиции не
//    посчитаны — в подсказке пометки (K.partNote, K.partTip); не посчитано ни у одной (N = M) — «—», как и раньше;
//  - общий механизм: M.sumX / K.groupSum дают признак неполноты {v, n, miss, missed}; K.d ставит пометку сама (после
//    spec.suffix), K.kpi переносит её под цифру, итоги K.table (группы и «Итого») — K.groupSum (partRow — какие строки
//    считать), разбор K.drill — в заголовке;
//  - CSV ручной таблицы (K.tableCsv): в ячейке — число, пометка — отдельной колонкой «Неполный итог»;
//  - водопад PnL: «Итого» — известный итог, часть без разложения по факторам — столбец «Не разложено» (K.wfTotals).
// Браузерная часть (C01, C05, C06, C10, C12, C17, CSV) — partial_sums_335.pw.test.js.
//   node tests/js/partial_sums_335.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'concepts', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const plain = x => JSON.parse(JSON.stringify(x));

function el(tag) {
  const e = { nodeType: 1, tagName: (tag || 'div').toUpperCase(), style: {}, dataset: {}, children: [], innerHTML: '', className: '', isConnected: true, parent: null,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { c.parent = e; e.children.push(c); return c; }, append(...c) { c.forEach(x => e.appendChild(x)); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
  return e;
}
// таблица для K.tableCsv: [[html | {h, cls}]]
function table(spec) {
  const cell = (tag, c) => { const x = el(tag); if (typeof c === 'string') x.innerHTML = c; else { x.innerHTML = c.h || ''; x.className = c.cls || ''; } x.colSpan = 1; return x; };
  const row = (tag, cells) => { const r = el('tr'); r.cells = cells.map(c => cell(tag, c)); return r; };
  const T = el('table');
  T.tHead = { rows: spec.head.map(h => row('th', h)) };
  T.tBodies = [{ rows: spec.body.map(b => row('td', b)) }];
  T.tFoot = spec.foot ? { rows: spec.foot.map(f => row('td', f)) } : null;
  return T;
}
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/instr.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const { M, K } = load();
const pos = (name, v) => ({ id: name, name, v, searchText: name.toLowerCase() });

t('K.partNote: «· без N из M поз.», в подсказке — какие позиции; всё или ничего не посчитано — без пометки', () => {
  const L = [pos('ОФЗ 26238', 1e6), pos('ОФЗ 26240', null), pos('Сбер 001Р', 2e6), pos('ВТБ Б-1', NaN)];
  const x = M.sumX(L, 'v');
  assert.deepStrictEqual([x.v, x.n, x.miss], [3e6, 2, 2]);
  assert.deepStrictEqual(plain(x.missed.map(p => p.name)), ['ОФЗ 26240', 'ВТБ Б-1']);
  assert.ok(K.isPartial(x));
  assert.strictEqual(K.partText(x), 'без 2 из 4 поз.');
  const note = K.partNote(x);
  assert.ok(/^ <span class="k-part" title="[^"]*">· без 2 из 4 поз\.<\/span>$/.test(note), note);
  assert.ok(K.partTip(x).includes('ОФЗ 26240, ВТБ Б-1') && K.partTip(x).includes('Итог по 2 поз. из 4'), K.partTip(x));
  // ни одна не посчитана — «—» (B1), пометки нет; все посчитаны — нет
  assert.strictEqual(K.partNote(M.sumX([pos('a', null)], 'v')), '');
  assert.strictEqual(K.partNote(M.sumX([pos('a', 1)], 'v')), '');
  assert.strictEqual(K.partNote(null), '');
  // длинный список — первые K.PART_NAMES и «и ещё»; строки {p, v} — по имени позиции; имя экранируется
  const many = Array.from({ length: K.PART_NAMES + 5 }, (_, i) => pos('P' + i, null)).concat([pos('x', 1)]);
  assert.ok(K.partTip(M.sumX(many, 'v')).endsWith('P' + (K.PART_NAMES - 1) + ' и ещё 5'), K.partTip(M.sumX(many, 'v')));
  assert.ok(K.partTip(M.sumX([{ p: pos('Газпром БО-05'), v: null }, { p: pos('y'), v: 1 }], 'v')).includes('Газпром БО-05'));
  assert.ok(K.partNote(M.sumX([pos('<b>"x"', null), pos('y', 1)], 'v')).includes('&lt;b&gt;&quot;x&quot;'));
});

t('K.d: пометка сама по списку и строкам, после spec.suffix; part: false и spec.v — без неё; N = M — «—»', () => {
  const L = [pos('a', 1500), pos('b', null)];
  const h = K.d({ title: 'PnL', fmt: K.f.k, list: L, val: 'v', suffix: '&nbsp;тыс ₽' });
  assert.ok(/>2<\/span>&nbsp;тыс ₽ <span class="k-part"[^>]*>· без 1 из 2 поз\.<\/span>$/.test(h), h);
  assert.ok(!K.d({ title: 'PnL', fmt: K.f.k, list: L, val: 'v', part: false }).includes('k-part'));
  assert.ok(!K.d({ title: 'PnL', fmt: K.f.k, v: 5, list: L, val: 'v' }).includes('k-part'), 'итог задан (spec.v) — пометку ставит вызывающий');
  const none = K.d({ title: 'PnL', fmt: K.f.k, list: [pos('a', null)], val: 'v', suffix: ' mm' });
  assert.ok(none.includes('—') && !none.includes('k-part') && none.endsWith(' mm'), none);
  assert.ok(K.d({ title: 'F', fmt: K.f.k, list: [pos('a', null)], val: 'v', nullable: true, suffix: ' тыс' }).endsWith('—</span> тыс'), 'nullable «—» — с единицей');
  // текст задан (цифра в своём формате) — пометка всё равно по списку
  assert.ok(K.d({ title: 'PnL', list: L, val: 'v' }, '1.5 mm').includes('· без 1 из 2 поз.'));
});

t('K.kpi: пометка из цифры — строкой под цифрой, после единицы; K.accClass её не читает', () => {
  const L = [pos('a', 1500), pos('b', null)];
  const k = K.kpi('PnL дня', K.d({ title: 'PnL', fmt: K.f.k, list: L, val: 'v' }) + K.kpiUnit('тыс ₽'), 'sub');
  assert.ok(/<span class="u">тыс ₽<\/span><div class="k-parts"><span class="k-part"[^>]*>· без 1 из 2 поз\.<\/span><\/div><\/div><div class="s">sub/.test(k), k);
  assert.strictEqual(K.kpi('x', '5'), K.kpi('x', '5').replace('k-parts', ''), 'без пометки — как раньше');
  assert.strictEqual(K.accClass('(1.5) · без 3 из 30 поз.'), 'k-accn');
  assert.strictEqual(K.accClass('1.5 · без 3 из 30 поз.'), 'k-accp');
});

t('K.groupSum: сумма посчитанных и признак; NaN — «не посчитано»; cnt — какие строки считать; nullable', () => {
  const v = r => r.v;
  const x = K.groupSum([{ v: 5 }, { v: NaN }, { v: null }, { v: 1 }], v);
  assert.deepStrictEqual([x.v, x.n, x.miss, x.missed.length], [6, 2, 2, 2]);
  assert.strictEqual(K.groupTotal([{ v: 5 }, { v: NaN }], v), 5, 'итог — та же сумма');
  // cnt: остатки ДС в сумме, но не в счёте позиций; позиций нет совсем — сумма остатков
  const c = K.groupSum([{ v: 3, cash: 1 }, { v: null }, { v: 2 }], v, null, r => !r.cash);
  assert.deepStrictEqual([c.v, c.n, c.miss], [5, 1, 1]);
  assert.strictEqual(K.groupSum([{ v: 3, cash: 1 }, { v: null }], v, null, r => !r.cash).v, null, 'ни одна позиция не посчитана — «—»');
  assert.strictEqual(K.groupSum([{ v: 3, cash: 1 }], v, null, r => !r.cash).v, 3);
  assert.strictEqual(K.groupSum([{ v: 5, x: null }], v, r => r.x).v, null, 'nullable: все пусты');
});

t('K.table: «Итого» и итоги групп — число и пометка; partRow; CSV K.table — строки позиций, без итогов', () => {
  const rows = [pos('a', 1000), pos('b', null), pos('c', 2000), Object.assign(pos('d', 500), { cash: 1 })];
  rows.forEach((r, i) => { r.g = i < 2 ? 'G1' : 'G2'; });
  const host = el();
  const api = K.table(host, { rows, search: false, viewKey: 'c99-part', groupBy: [{ key: r => r.g }], partRow: r => !r.cash,
    columns: [{ key: 'name', label: 'Позиция' }, { key: 'v', label: 'PnL', num: true, sum: true, fmt: v => K.f.k(v) }] });
  const html = host.children.find(c => /k-tbl-wrap/.test(c.className)).innerHTML;
  const foot = /<tfoot>[\s\S]*<\/tfoot>/.exec(html)[0], grp = html.match(/<tr class="grp[\s\S]*?<\/tr>/g);
  assert.ok(foot.includes('· без 1 из 3 поз.') && />4<\/span>/.test(foot), 'Итого: 1+2+0.5 (остатки — в сумме), без 1 из 3 (остатки — не в счёте): ' + foot);
  assert.ok(grp[0].includes('· без 1 из 2 поз.') && grp[0].includes('>1<'), grp[0]);
  assert.ok(!grp[1].includes('k-part'), 'G2 посчитана целиком: ' + grp[1]);
  assert.ok(/title="[^"]*: b"/.test(foot), 'в подсказке — позиция b: ' + foot);
  const csv = api.csv().split('\r\n');
  assert.deepStrictEqual(csv.slice(2, 4), ['Позиция,PnL', 'a,1']);
  assert.ok(!csv.join('\n').includes('без'), 'в CSV K.table итогов нет — и пометок нет');
});

t('K.tableCsv: число в ячейке — числом, пометка — колонкой «Неполный итог»; без пометок — как раньше', () => {
  const note = K.partNote(M.sumX([pos('a', 1), pos('b', null), pos('c', null)], 'v'));
  const T = table({
    head: [['Книга', { h: 'Кэрри<span class="k-thu">тыс ₽</span>', cls: 'n' }, { h: 'Фондир.', cls: 'n' }]],
    body: [['OFZ', { h: '<span class="dr pos" data-dr="1">1,500</span>', cls: 'n' }, { h: '(2,000)' + note, cls: 'n' }]],
    foot: [['Итого', { h: '<span data-dr="2">(1,234.5)</span>' + note, cls: 'n' }, { h: '(2,000)' + note, cls: 'n' }]],
  });
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const L = K.tableCsv(T, {}).split('\r\n');
    assert.strictEqual(L[2], 'Книга,"Кэрри, тыс ₽",Фондир.,Неполный итог', fm);
    assert.strictEqual(L[3], 'OFZ,1500,-2000,Фондир.: без 2 из 3 поз.', fm);
    assert.strictEqual(L[4], 'Итого,-1234.5,-2000,"Кэрри, тыс ₽: без 2 из 3 поз.; Фондир.: без 2 из 3 поз."', fm);
  }
  K.negFmt = 'acc';
  const T2 = table({ head: [['a', { h: 'b', cls: 'n' }]], body: [['x', { h: '1,000', cls: 'n' }]] });
  assert.deepStrictEqual(K.tableCsv(T2, {}).split('\r\n').slice(2, 4), ['a,b', 'x,1000']);
});

t('K.wfTotals: «Итого» — известный итог, «Не разложено» — итог минус факторы; без итога — сумма факторов; ничего — null', () => {
  const tot = v => ({ v, n: 1, miss: 0 });
  assert.deepStrictEqual(plain(K.wfTotals([1e6, null, -2e5], tot(8e5))), { total: 8e5, unexp: null }, 'итог = сумма факторов');
  assert.deepStrictEqual(plain(K.wfTotals([null, null], tot(5e6))), { total: 5e6, unexp: 5e6 }, '#341: MTD без разложения — +5 mm «не разложено»');
  assert.deepStrictEqual(plain(K.wfTotals([1e6, 2e6], tot(4e6))), { total: 4e6, unexp: 1e6 });
  assert.deepStrictEqual(plain(K.wfTotals([1e6, 2e6], tot(3e6 + 100))), { total: 3e6 + 100, unexp: null }, 'меньше K.WF_UNEXP_MIN — столбца нет');
  assert.deepStrictEqual(plain(K.wfTotals([1e6, 2e6], { v: null })), { total: 3e6, unexp: null }, 'итога нет — как раньше, сумма факторов');
  assert.deepStrictEqual(plain(K.wfTotals([null, null], { v: null })), { total: null, unexp: null });
});

t('страницы: пометки — общим механизмом (K.partNote / K.d / K.groupSum), без своих «без N из M» и без netKnown на экране', () => {
  for (const f of fs.readdirSync(path.join(__dirname, '..', '..', 'prototypes', 'concepts')).filter(f => /^c\d\d-.*\.html$/.test(f))) {
    const s = P(f);
    assert.ok(!/'без ' \+ [\w.]+miss/.test(s), f + ': своя пометка «без N из M»');
    assert.ok(!/S\.netKnown/.test(s), f + ': netKnown на экране (вариант «б»)');
  }
  const c17 = P('c17-forward-carry.html');
  assert.ok(c17.includes('S.part.net') && c17.includes('partRow: r => !r.isCash'), 'C17: «Нетто» — неполный итог, остатки ДС — не позиции');
});

console.log(n + ' tests ok');
