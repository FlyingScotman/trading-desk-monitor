// Валюта и единицы в подписях мониторов (#105, prototypes/concepts): единицы у плиток, колонок и осей графиков;
// «₽» в подписи — валюта отчёта (подменяется символом выбранной валюты), код валюты (RUB, USD) — своя валюта суммы;
// что пересчитывает переключатель валюты отчёта — K.REPR_NOTE.
//   node tests/js/units_105.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(repr) {
  const store = new Map(repr ? [['concepts-repr', repr]] : []);
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el(),
    createTreeWalker: () => ({ nextNode: () => null }) };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, NodeFilter: { SHOW_TEXT: 4 } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const pages = fs.readdirSync(DIR).filter(f => /^c\d+.*\.html$/.test(f));
const sources = pages.map(f => [f, P(f)]).concat([['lib/kit.js', P('lib/kit.js')]]);

t('K.u: «₽» в подписи — символ валюты отчёта (для графиков на canvas)', () => {
  assert.strictEqual(load().K.u('тыс ₽/бп'), 'тыс ₽/бп');
  const { K } = load('USD');
  assert.strictEqual(K.repr, 'USD');
  assert.strictEqual(K.u('тыс ₽/бп'), 'тыс $/бп');
  assert.strictEqual(K.u('mm RUB'), 'mm RUB', 'код валюты — своя валюта, не подменяется');
  assert.strictEqual(K.u(null), '');
});

t('оси графиков: {unit} → подпись оси и место под неё; без unit — как раньше', () => {
  const { K } = load('EUR');
  const grid = { left: 6, top: 28, bottom: 4 };
  const o = { grid, xAxis: { type: 'value', unit: 'mm ₽' }, yAxis: [{ type: 'value', unit: 'тыс ₽/бп' }, { type: 'value' }] };
  K.axisUnits(o);
  assert.strictEqual(o.yAxis[0].name, 'тыс €/бп');
  assert.strictEqual(o.yAxis[0].nameLocation, 'end');
  assert.strictEqual(o.xAxis.name, 'mm €');
  assert.strictEqual(o.xAxis.nameLocation, 'middle');
  assert.ok(!('unit' in o.yAxis[0]) && !('unit' in o.xAxis), 'unit — не опция echarts, убирается');
  assert.strictEqual(o.yAxis[1].name, undefined);
  assert.strictEqual(o.grid.top, 44); assert.strictEqual(o.grid.bottom, 22);
  // легенда сверху — в одну строку (перенос наехал бы на подпись оси); подпись выше подписей столбцов — nameGap
  const lg = { grid: { top: 18 }, legend: { top: 0, left: 0 }, yAxis: { type: 'value', unit: 'mm ₽', nameGap: 20 } };
  K.axisUnits(lg);
  assert.strictEqual(lg.legend.type, 'scroll');
  assert.strictEqual(lg.yAxis.nameGap, 20); assert.strictEqual(lg.grid.top, 18 + 16 + 13);
  const side = { legend: { orient: 'vertical', left: 0 }, yAxis: { type: 'value', unit: 'mm ₽' } }; K.axisUnits(side);
  assert.strictEqual(side.legend.type, undefined, 'вертикальная легенда сбоку не меняется');
  assert.deepStrictEqual([grid.top, grid.bottom], [28, 4], 'общий объект сетки страницы не меняется');
  const p = { grid: { top: 10 }, yAxis: { type: 'value', name: 'своё', unit: 'mm ₽' } };
  K.axisUnits(p);
  assert.strictEqual(p.yAxis.name, 'своё', 'заданное имя оси не перезаписывается');
  assert.strictEqual(p.grid.top, 10);
  const q = { yAxis: { type: 'value', unit: undefined } }; K.axisUnits(q);
  assert.strictEqual(q.yAxis.name, undefined);
});

t('оси значений с суммами в мониторах подписаны единицей', () => {
  // первая ось значений с форматтером сумм (f.dv, f.mn, f.bn, f.k) без unit — нарушение; вторая ось (справа) — та же
  // единица, её не подписываем
  const bad = [];
  const re = /(xAxis|yAxis): \[?\{ type: 'value', (?!unit:)[^{}]*axisLabel: \{ formatter: (?:v => )?f\.(dv|mn|bn|k)\b/g;
  sources.forEach(([f, src]) => { for (const m of src.matchAll(re)) bad.push(f + ': ' + m[0].slice(0, 80)); });
  assert.deepStrictEqual(bad, []);
  let units = 0; sources.forEach(([, src]) => { units += (src.match(/type: 'value', unit: /g) || []).length; });
  assert.ok(units >= 25, 'осей с единицей: ' + units);
});

t('плитки и подписи сумм — с валютой: «mm ₽», «тыс ₽/бп», не «mm», «тыс/бп»', () => {
  const bad = [];
  sources.forEach(([f, src]) => {
    for (const m of src.matchAll(/K\.kpiUnit\('([^']*)'\)/g)) if (/^(mm|bn|тыс)(\/бп)?$/.test(m[1])) bad.push(f + ': ' + m[0]);
    for (const m of src.matchAll(/тыс\/бп/g)) bad.push(f + ': тыс/бп @' + m.index);
    for (const m of src.matchAll(/(?:cell\(|label: )'[^']*, (?:mm|bn|тыс)'/g)) bad.push(f + ': ' + m[0]);
  });
  assert.deepStrictEqual(bad, []);
});

t('колонки таблиц: единица — второй строкой в шапке и в заголовке CSV', () => {
  const { K } = load();
  assert.strictEqual(K.colUnit({ label: 'DV01', unit: 'тыс ₽/бп' }), 'тыс ₽/бп');
  assert.strictEqual(K.colUnit({ label: 'MV, mm ₽', unit: 'mm ₽' }), '', 'единица уже в подписи');
  assert.strictEqual(K.colUnit({ label: '<span class="k-hint">CS01</span>', unit: 'тыс ₽/бп' }), 'тыс ₽/бп');
  assert.strictEqual(K.colUnit({ label: 'Имя' }), '');
  ['spread', 'zdisc', 'gspread'].forEach(k => assert.strictEqual(K.posCols[k].unit, 'б.п.', k));
  assert.strictEqual(K.posCols.dur.unit, 'лет'); assert.strictEqual(K.posCols.price.unit, '% ном.');
  ['dv01', 'cs01', 'basis01', 'ofzSpr01'].forEach(k => assert.strictEqual(K.posCols[k].unit, 'тыс ₽/бп', k));
  const api = K.table(el(), { rows: [{ id: 'a', x: 1500 }], search: false, viewKey: 'u105', columns: [{ key: 'x', label: 'DV01', num: true, unit: 'тыс ₽/бп', fmt: v => K.f.dv(v) }] });
  assert.strictEqual(api.csv().split('\r\n')[2], '"DV01, тыс ₽/бп"');
  const src = P('lib/kit.js');
  assert.ok(src.includes("(K.colUnit(c) ? '<span class=\"k-thu\">' + esc(K.colUnit(c)) + '</span>' : '')"), 'единица в шапке');
  assert.ok(/\.k-tbl th \.k-thu \{[^}]*display: block/.test(P('lib/kit.css')));
});

t('переключатель валюты отчёта объясняет, что пересчитывается, а что остаётся в своей валюте', () => {
  const { K } = load('USD');
  const txt = K.REPR_NOTE.map(x => x.join(' ')).join('\n');
  ['MV', 'DV01', 'PnL', 'кэрри', 'потоки'].forEach(w => assert.ok(txt.includes(w), w));
  ['номинал', 'цены', 'доходности', 'курсы', 'оборот рынка (mm RUB)', 'рублёвого рынка'].forEach(w => assert.ok(txt.includes(w), w));
  assert.ok(txt.includes('символ ($)'), 'символ текущей валюты отчёта');
  const src = P('lib/kit.js');
  assert.ok(src.includes("data-a=\"repr\" title=\"' + esc('Валюта отчёта.") && src.includes('k-repr-note'));
});

t('рублёвый рынок: суммы календаря пересчитаны в валюту отчёта — «bn ₽» (при USD «bn $»); оборот — в RUB', () => {
  const c08 = P('c08-calendar.html'), c09 = P('c09-market.html');
  assert.ok(c08.includes("unit = 'bn ₽'") && !/bn RUB/.test(c08), 'c08: рынок подписан символом валюты отчёта');
  assert.strictEqual(load('USD').K.u('bn ₽'), 'bn $');
  // календарь рынка (демо: lib/mock.js marketCal; прогон: lib/real.js RUB_K) — в валюте отчёта
  const rub = load().M, usd = load('USD').M;
  const a = rub.marketCal.rub[0], b = usd.marketCal.rub[0];
  assert.ok(a.amount > 0 && Math.abs(b.amount - a.amount * usd.toRepr.RUB) < 1e-6 * a.amount, 'пересчитано: ' + a.amount + ' → ' + b.amount);
  assert.ok(Math.abs(b.amount - a.amount) > 0.5 * a.amount, 'в USD сумма иная, чем в рублях');
  assert.ok(/RUB_K = \(\) => \(M\.toRepr && M\.toRepr\.RUB\)/.test(P('lib/real.js')), 'прогон: тот же пересчёт');
  // оборот рынка (C09) — не пересчитывается: «mm RUB»
  assert.ok(c09.includes("label: 'Оборот, mm RUB'"));
  assert.strictEqual(JSON.stringify(usd.marketMovers.map(x => x.vol)), JSON.stringify(rub.marketMovers.map(x => x.vol)));
  assert.ok(!/\bvalue\b/.test(P('lib/kit.js').match(/\(function convert\(k\) \{[\s\S]*?\}\)\(1 \/ M\.fx\[K\.repr\]\);/)[0]), 'convert() не трогает оборот');
});

t('CSV: «₽» в заголовках и названии — символ валюты отчёта', () => {
  const { K } = load('USD');
  const api = K.table(el(), { rows: [{ id: 'a', x: 1, y: 2 }], search: false, viewKey: 'u105-usd', title: 'MV, mm ₽',
    columns: [{ key: 'x', label: 'MV, mm ₽', num: true }, { key: 'y', label: 'DV01', num: true, unit: 'тыс ₽/бп' }] });
  const L = api.csv().split('\r\n');
  assert.ok(L[0].includes('"MV, mm $"'), L[0]);
  assert.strictEqual(L[2], '"MV, mm $","DV01, тыс $/бп"');
});

console.log(n + ' tests passed');
