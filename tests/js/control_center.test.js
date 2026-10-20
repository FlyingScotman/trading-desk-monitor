// Центр управления (R20): страница строится на демо-данных и на данных кокпита сервера; в тексте нет null/NaN/undefined;
// каждое число с разбором (data-drill) открывает спецификацию, строки которой суммируются в показанное число, каждая
// точка графика с разбором — тоже; фильтр Desk Monitor ограничивает строки разбора позициями фильтра.
//   node tests/js/control_center.test.js
// Страница исполняется на заглушке DOM (querySelector возвращает один и тот же элемент на селектор) и ECharts.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function fakeEl(name) {
  const kids = {};
  const el = { name, kids, innerHTML: '', textContent: '', value: '', checked: false, hidden: false, open: false, dataset: {}, style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} }, isConnected: true,
    clientWidth: 900, clientHeight: 400, offsetWidth: 900, offsetHeight: 400,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, removeAttribute() {}, hasAttribute: () => false,
    focus() {}, closest: () => null, remove() {}, contains: () => false,
    querySelector: s => kids[s] || (kids[s] = fakeEl(name + ' ' + s)), querySelectorAll: () => [],
    appendChild: c => c, insertBefore: c => c, append() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 400, width: 900, height: 400 }) };
  return el;
}
let OPTS = [];
const chartStub = () => ({ setOption(o) { OPTS.push(o); }, on() {}, off() {}, resize() {}, dispose() {}, isDisposed: () => false });
function page(mutate, store) {
  OPTS = [];
  const errors = [], con = { log() {}, info() {}, warn: (...a) => errors.push('warn ' + a.join(' ')), error: (...a) => errors.push(a.map(String).join(' ')) };
  const doc = fakeEl('document'), ls = Object.assign({}, store || {});
  const listeners = {};
  Object.assign(doc, { documentElement: fakeEl('html'), body: fakeEl('body'), title: '',
    addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
    getElementById: id => doc.querySelector('#' + id), createElement: tag => fakeEl(tag), write() {} });
  const opened = [];
  const ctx = { window: {}, document: doc, console: con, Intl, Math, JSON, Date, Promise, setTimeout: () => 0, clearTimeout() {},
    location: { protocol: 'file:', search: '', hash: '', href: 'file:///x' },
    localStorage: { getItem: k => ls[k] ?? null, setItem(k, v) { ls[k] = String(v); }, removeItem(k) { delete ls[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '#808080' }), addEventListener() {}, removeEventListener() {},
    echarts: { init: chartStub } };
  vm.createContext(ctx);
  ctx.window = ctx; ctx.self = ctx;
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  vm.runInContext(P('desk-scope.js'), ctx);
  ctx.DESK.drill = { open: s => opened.push(s), attach() {} };   // заглушка desk-drill.js
  const html = P('control-center.html'), start = html.lastIndexOf('<script>');
  vm.runInContext(html.slice(start + '<script>'.length, html.lastIndexOf('</script>')), ctx);
  assert.deepStrictEqual(errors, [], 'ошибки в консоли');
  return { doc, ctx, CC: ctx.CC, D: ctx.DESK, opened, listeners };
}
function htmlOf(doc) {   // разметка всех элементов, в которые писала страница
  const out = [];
  (function walk(el) { if (el.innerHTML) out.push(el.innerHTML); if (el.textContent) out.push(el.textContent); Object.values(el.kids).forEach(walk); })(doc);
  return out.join('\n');
}
// числа с разбором: data-drill="k" data-f="fmt"; показанное — .dv-v внутри элемента, иначе его текст
function drills(html) {
  const re = /data-drill="(k\d+)" data-f="(\w+)"[^>]*>/g, out = [];
  let m;
  while ((m = re.exec(html))) {
    const rest = html.slice(re.lastIndex, re.lastIndex + 2000), next = rest.indexOf('data-drill=');
    const own = next >= 0 ? rest.slice(0, next) : rest, v = /class="[^"]*\bdv-v\b[^"]*">/.exec(own), inner = v ? own.slice(v.index + v[0].length) : own;
    out.push({ k: m[1], f: m[2], shown: inner.slice(0, inner.indexOf('<')) });
  }
  return out;
}
const close = (a, b) => Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b));
function checkSpec(s, where) {
  assert.ok(s && typeof s.title === 'string' && s.title, where + ': заголовок');
  assert.ok(Array.isArray(s.rows), where + ': строки');
  s.rows.forEach(r => {
    assert.ok(typeof r.value === 'number' && isFinite(r.value), where + ': значение строки ' + JSON.stringify(r));
    assert.ok(r.name != null && String(r.name) !== '' && !/undefined|NaN|null/.test(String(r.name)), where + ': имя строки ' + JSON.stringify(r));
  });
  const tot = s.rows.reduce((a, r) => a + r.value, 0);
  assert.ok(close(tot, s.total), where + ': строки ' + tot + ' ≠ итог ' + s.total);
}
function checkPage(pg, label) {
  const html = htmlOf(pg.doc), text = html.replace(/<[^>]*>/g, ' ');
  assert.ok(!/\bNaN\b|\bundefined\b|\bnull\b|Infinity/.test(text), label + ': null/NaN/undefined в тексте: ' + (text.match(/.{60}(NaN|undefined|null|Infinity).{40}/) || [''])[0]);
  const ds = drills(html);
  assert.ok(ds.length > 60, label + ': чисел с разбором ' + ds.length);
  ds.forEach(d => {
    const s = pg.CC.spec(d.k);
    checkSpec(s, label + ' ' + d.k + ' «' + d.shown + '»');
    assert.strictEqual(d.shown, pg.CC.F[d.f](s.total), label + ': показано «' + d.shown + '», итог разбора ' + s.total + ' (' + s.title + ')');
  });
  let pts = 0;
  OPTS.forEach(o => (o.series || []).forEach(se => (se.data || []).forEach(it => {
    if (!it || typeof it !== 'object' || !it.drill) return;
    const s = pg.CC.spec(it.drill);
    if (s && typeof s.then === 'function') return;
    checkSpec(s, label + ' график ' + se.name);
    if (it.dv != null) assert.ok(close(it.dv, s.total), label + ': точка ' + it.dv + ' ≠ разбор ' + s.total);
    const v = Array.isArray(it.value) ? null : it.value;
    if (it.dv != null && v != null) assert.ok(close(v, s.total), label + ': столбец ' + v + ' ≠ разбор ' + s.total);
    pts++;
  })));
  assert.ok(pts > 10, label + ': точек графиков с разбором ' + pts);
  return { html, ds, pts };
}

t('демо-данные: страница строится, каждое число и точка раскрываются в позиции, сумма строк = показанное', () => {
  const pg = page();
  const { ds } = checkPage(pg, 'демо');
  const kinds = new Set(ds.map(d => d.f));
  ['m', 'sm', 'k'].forEach(k => assert.ok(kinds.has(k), 'формат ' + k));
});

t('PnL дня = сумма факторов по позициям и фондированию; разбор фактора — позиции', () => {
  const pg = page(), D = pg.D, U = D.util;
  const html = htmlOf(pg.doc), ds = drills(html);
  const specs = ds.map(d => pg.CC.spec(d.k));
  const day = specs.find(s => s.title === 'PnL дня (объяснённый)');
  const want = D.positions.filter(U.pnlOk).reduce((s, p) => s + p.pnl.explained, 0) + D.funding.filter(f => !f.pnl.inPosition).reduce((s, f) => s + f.pnl.carry, 0)
    + Object.keys(D.fundingPnlByBook).reduce((s, b) => s + (D.fundingPnlByBook[b].fx || 0), 0);
  assert.ok(close(day.total, want), day.total + ' ≠ ' + want);
  const rates = specs.find(s => s.title === 'PnL дня: Ставки');
  assert.ok(rates && rates.rows.every(r => D.positions.some(p => p.id === r.id)), 'фактор «Ставки» — строки-позиции');
  const shift = specs.find(s => /^Вклад сдвига RUB RUONIA/.test(s.title)), mv = D.CURVE_MOVES['RUB RUONIA'];
  const wantShift = D.positions.reduce((t, p) => t + ((p.risk || {})['RUB RUONIA'] || []).reduce((a, v, i) => a + (v || 0) * (mv[i] || 0), 0), 0);
  assert.ok(shift && wantShift && close(shift.total, wantShift), 'вклад сдвига кривой = Σ DV01(срок) × Δ(срок): ' + (shift && shift.total) + ' ≠ ' + wantShift);
  const dv01 = specs.find(s => s.title === 'DV01: ₽ на +1 б.п. по всем кривым');
  assert.ok(close(dv01.total, D.positions.filter(p => !p.notValued).reduce((s, p) => s + (p.dv01 || 0), 0)));
});

t('прогон без PnL: «—» с причиной, не 0; без ошибок', () => {
  const NO = 'нет финального среза рынка за предыдущий рабочий день';
  const pg = page(D => {
    D.prevDate = null; D.pnlAvailable = false; D.pnlReason = 'no_prev_market'; D.pnlReasonText = NO;
    D.funding = []; Object.keys(D.fundingPnlByBook).forEach(b => { D.fundingPnlByBook[b] = { carry: 0, fx: 0, fxCbr: 0 }; });
    D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
    D.positions.forEach(p => { p.pnl = { available: false, reason: 'no_prev_market', reasonText: NO, explained: null, other: null, official: null }; });
  });
  const html = htmlOf(pg.doc);
  assert.ok(html.includes('class="na" title="PnL не посчитан: ' + NO + '"'), 'KPI PnL — «—» с причиной');
  assert.ok(html.includes('PnL не посчитан: ' + NO), 'водопад — причина');
  const ds = drills(html);
  ds.forEach(d => { const s = pg.CC.spec(d.k); checkSpec(s, d.k); assert.ok(!/^PnL дня/.test(s.title), 'нет разбора PnL без PnL: ' + s.title); });
});

t('фильтр Desk Monitor: строки разбора — только позиции выбранных книг', () => {
  let books;
  const pg = page(D => { books = Object.keys(D.BOOKS).slice(0, 2); }, {});
  const pg2 = page(null, { 'fidm.sel.v1': JSON.stringify({ books }) });
  assert.ok(pg2.D.scope.state.active && pg2.D.positions.length < pg.D.positions.length);
  const ids = new Set(pg2.D.positions.map(p => p.id).concat(pg2.D.funding.map(f => 'F' + f.id), books.map(b => 'B' + b)));
  const { ds } = checkPage(pg2, 'фильтр');
  ds.forEach(d => pg2.CC.spec(d.k).rows.forEach(r => {
    if (/^PnL \d\d\.\d\d \(весь деск\)/.test(pg2.CC.spec(d.k).title)) return;   // история — по всему деску
    assert.ok(ids.has(r.id), 'строка вне фильтра: ' + r.id + ' в «' + pg2.CC.spec(d.k).title + '»');
  }));
});

t('данные кокпита сервера: календарь × позиции, котировки D/D−1, рынок для RV, ставки и ЦБ', () => {
  const pg = page();
  const D = pg.D, bonds = D.positions.filter(p => p.instrType === 'BOND' && !p.notValued && p.instrumentId);
  const a = bonds[0], b = bonds[1];
  const ck = {
    run: { run_id: 7, market_known_at: '2026-10-13T18:00:00Z' }, log: { counts: { WARN: 2, ERROR: 0 }, lines: [{ level: 'WARN', message: 'P1: оценка assumed' }, { level: 'WARN', message: 'x' }] },
    history_runs: [], gaps: ['лимиты по эмитентам и группам: данных о лимитах в монитор не приходит'],
    quotes: { date: D.asOf, prev_date: D.prevDate, bonds: Object.fromEntries(bonds.map((p, i) => [p.instrumentId, { date: D.asOf, price: 100 + i, ytm: 15, g: 100 + 10 * i, dur_d: 365 * (1 + i % 7), prev: { date: D.prevDate, price: 99.5 + i, ytm: 15.1, g: 98 + 10 * i } }])) },
    universe: { date: D.asOf, cols: ['secid', 'name', 'class', 'ytm', 'g', 'dur_y', 'value'], rows: Array.from({ length: 60 }, (_, i) => ['U' + i, 'Рынок ' + i, i % 2 ? 'corporate' : 'ofz_pd', 15, 50 + 7 * i, 0.5 + i / 8, 1e6]) },
    rates: { known_at: '2026-10-13T18:00:00Z', fixings: { KEY_RATE: [['2026-10-12', 14.5], ['2026-10-13', 14.5]], RUONIA: [['2026-10-12', 14.3], ['2026-10-13', 14.36]] },
      cbr_past: [{ date: '2026-09-12', decision: 'cut', rate: 14.5 }], cbr_ahead: [{ date: '2026-10-16', effective: '2026-10-19', estimated: false }] },
    calendar: { from: D.asOf, to: '2026-11-13', events: [
      { kind: 'coupon', date: '2026-10-14', secid: a.instrumentId, per_bond_rub: 40, estimated: false, record_date: '2026-10-13' },
      { kind: 'coupon', date: '2026-10-20', secid: b.instrumentId, per_bond_rub: null, estimated: false, note: 'сумма купона не опубликована' },
      { kind: 'amortization', date: '2026-10-20', secid: b.instrumentId, per_bond_rub: 250, estimated: false },
      { kind: 'put', date: '2026-11-02', secid: a.instrumentId, per_bond_rub: 1000, estimated: false, note: 'оферта' },
      { kind: 'coupon', date: '2026-11-05', secid: 'NOT_HELD', per_bond_rub: 10 }] },
    holdings: Object.fromEntries(bonds.map(p => [p.id, { secid: p.instrumentId, quantity: 1000 }]))
  };
  OPTS = [];
  pg.CC.applyCockpit(ck);
  const { html, ds } = checkPage(pg, 'кокпит');
  const specs = ds.map(d => pg.CC.spec(d.k));
  const cpn = specs.find(s => /^купон \d\d\.\d\d\.\d{4}: /.test(s.title));
  const held = bonds.filter(p => p.instrumentId === a.instrumentId);
  assert.ok(cpn && close(cpn.total, 40 * 1000 * held.length), 'купон = выплата × бумаги × позиции: ' + (cpn && cpn.total));
  assert.ok(html.includes('сумма купона не опубликована'), 'купон без суммы — «—» с причиной');
  assert.ok(html.includes('Заседание ЦБ <b>16.10.2026</b>') && html.includes('Журнал прогона: предупреждений 2'), 'ЦБ и журнал');
  const rvPts = OPTS.flatMap(o => (o.series || []).filter(s => s.type === 'scatter' && s.name !== 'рынок').flatMap(s => s.data));
  assert.ok(rvPts.length >= 3 && rvPts.every(p => p.drill), 'точки портфеля на RV — с разбором');
  const moveSpec = specs.find(s => /^Эффект изменения цены/.test(s.title));
  assert.ok(moveSpec && moveSpec.rows.length, 'движение цены — по позициям');
  assert.ok(html.includes('лимиты по эмитентам'), 'пробелы в данных показаны');
});

// клик по числу (делегирование на document) открывает DESK.drill.open со спецификацией этого числа
const pgc = page(), kc = drills(htmlOf(pgc.doc))[0].k;
let prevented = false;
pgc.listeners.click.forEach(fn => fn({ target: { closest: () => ({ getAttribute: () => kc }) }, preventDefault() { prevented = true; } }));
setTimeout(() => {
  assert.ok(prevented && pgc.opened.length === 1 && pgc.opened[0].title === pgc.CC.spec(kc).title, 'клик открыл разбор');
  n++; console.log('ok клик по числу открывает DESK.drill со спецификацией');
  console.log(n + ' tests passed');
}, 0);
