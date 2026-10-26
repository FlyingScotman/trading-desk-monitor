// Мониторы (prototypes/concepts): единица «Δ цены» в CSV C03, раскрытые строки C14/C16 переживают смену «(1) / −1»
// (#317); оси с форматом из спецификации меры — через K.tick (K.tickOf: разбор, C04, C08), подпись отрицательного
// столбца C13 у его конца (#321); подписи графика вклада позиций C06 — по правилу водопада (#323); сверка вложенных
// структур прогона с сервером: extra, CURVES, risk, fxDelta (#324); K.spark: пропуск — разрыв линии, без NaN (#326).
//   node tests/js/ui_pack6.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun, unknownKeys, SERVER_KEYS } = require('./fixtures/concepts_run');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function context(repr) {
  const store = new Map(repr ? [['concepts-repr', repr]] : []);
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el(),
    createTreeWalker: () => ({ nextNode: () => null }) };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, NodeFilter: { SHOW_TEXT: 4 },
    CSS: { escape: s => String(s).replace(/["\\]/g, '\\$&') } };
  ctx.window = ctx;
  vm.createContext(ctx);
  return ctx;
}
function load(repr) {
  const ctx = context(repr);
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}

t('#326: K.spark — пропуск ряда даёт разрыв линии, а не точку NaN; пустой ряд — без линии', () => {
  const { K } = load();
  const d = s => (/ d="([^"]*)"/.exec(s) || [])[1];
  const full = K.spark([1, 2, 3, 2], 30, 10, '#000');
  assert.ok(/^M[\d.]+,[\d.]+(L[\d.]+,[\d.]+){3}$/.test(d(full)), full);
  const gap = K.spark([1, null, 3, 2, NaN, 4, undefined], 60, 10, '#000');
  assert.ok(!/NaN|undefined/.test(gap), gap);
  assert.strictEqual((d(gap).match(/M/g) || []).length, 3, 'три куска: [1], [3, 2], [4]');
  assert.ok(d(gap).startsWith('M0.0,') && d(gap).includes('M20.0,') && d(gap).includes('M50.0,'), 'x — по месту точки в ряду, пропуски не сдвигают');
  assert.ok(!/NaN/.test(K.spark([null, null, 5, 5], 20, 10, '#000')), 'ровный ряд после пропусков');
  assert.strictEqual(K.spark([null, null, null], 20, 10), '');
  assert.strictEqual(K.spark([null, 7, null], 20, 10), '', 'одна непустая точка — линии нет');
  assert.strictEqual(K.spark([], 20, 10), '');
});

t('#321: K.tickOf — деления оси с масштабом формата меры; не голое число — формат как есть', () => {
  for (const repr of ['RUB', 'USD']) {
    const { K } = load(repr), f = K.f;
    assert.strictEqual(K.tickOf(f.k)(250e3), '250');
    assert.strictEqual(K.tickOf(f.dv)(-1500), '(1.5)');
    assert.strictEqual(K.tickOf(v => f.mn(v, 0))(2e5), '0.2', repr + ': mm, мелкое деление — не «0»');
    assert.strictEqual(K.tickOf(v => f.mns(v, 1))(5e6), '5');
    assert.strictEqual(K.tickOf(v => f.bn(v, 1))(25e8), '2.5');
    assert.strictEqual(K.tickOf(v => f.n(v))(3), '3');
    [v => f.pct(v), f.money, v => '<span>' + f.date(v) + '</span>', () => { throw new Error('x'); }].forEach(fmt => assert.strictEqual(K.tickOf(fmt), fmt));
  }
  // в USD ряд мелких делений различим: 0.1, 0.2, 0.3 mm, а не «0 0 0»
  const { K } = load('USD');
  const ax = K.tickOf(v => K.f.mn(v, 0));
  assert.deepStrictEqual([0, 1e5, 2e5, 3e5].map(ax), ['0', '0.1', '0.2', '0.3']);
  assert.deepStrictEqual([0, 1e3, 2e3].map(K.tickOf(K.f.k)), ['0', '1', '2']);
  const kit = P('lib/kit.js'), c04 = P('c04-pivot.html'), c08 = P('c08-calendar.html');
  assert.ok(kit.includes("xAxis: { type: 'value', unit: spec.unit, axisLabel: { formatter: K.tickOf(fmt), hideOverlap: true } }"), 'разбор');
  assert.ok(c04.includes("axisLabel: { formatter: K.tickOf(A.meas.fmt), hideOverlap: true }"), 'C04');
  assert.ok(c08.includes("axisLabel: { formatter: K.tickOf(o.fmt) }") && c08.includes("unit: 'mm ' + ccy, axisLabel: { formatter: v => K.tick(v), hideOverlap: true }"), 'C08');
  assert.ok(!/axisLabel: \{ formatter: v => (fmt|A\.meas\.fmt|o\.fmt|f\.n)\(v/.test(kit + c04 + c08), 'нет осей через формат меры');
});

t('#321: C13 — подпись отрицательного столбца рейтингов у его конца', () => {
  const s = P('c13-issuers.html');
  assert.ok(s.includes("label: { position: v < 0 ? 'bottom' : 'top' }"));
});

t('#323: C06 — график вклада позиций подписан по правилу водопада, без «+0.0» в USD', () => {
  const s = P('c06-launchpad.html');
  const top = s.slice(s.indexOf("title: 'Топ вклад в PnL дня'"), s.indexOf("waterfall: {"));
  assert.ok(top.includes("formatter: p => K.wfLabel(p.value)") && top.includes("valueFormatter: v => K.wfTip(v) + ' mm ₽'"));
  assert.ok(!top.includes('f.mns('), 'нет f.mns в подписях и подсказке');
  const { K } = load('USD');
  [38e3, -6e3, 0.35e6].forEach(v => assert.ok(!/^[+(]?0\.0\)?$/.test(K.wfLabel(v)), K.wfLabel(v)));
});

t('#317: C03 — у «Δ цены» в CSV единица; C14/C16 — раскрытые строки переживают смену формата', () => {
  const c03 = P('c03-blotter.html');
  assert.ok(c03.includes("chg: { label: 'Δ цены', num: true, val: r => r.price == null ? null : r.price - r.prevPrice, csvHead: 'Δ цены, % ном.',"));
  assert.ok(P('c14-day-status.html').includes("K.keepOpen(view, 'data-f', render)"));
  assert.ok(P('c16-ocp.html').includes("K.keepOpen(view, 'data-r', () => render(last))"));
  // K.keepOpen: что было раскрыто до перерисовки, раскрывается щелчком после
  const { K } = load();
  const rows = (ids, open) => ids.map(id => ({ id, exp: open.includes(id), clicks: 0, getAttribute(a) { return a === 'aria-expanded' ? String(this.exp) : this.id; }, click() { this.clicks++; this.exp = !this.exp; } }));
  let cur = rows(['0', '1', '2'], ['0', '2']);
  const root = {
    querySelectorAll: sel => (assert.strictEqual(sel, 'tr[data-f][aria-expanded="true"]'), cur.filter(r => r.exp)),
    querySelector: sel => cur.find(r => sel === 'tr[data-f="' + r.id + '"]') || null,
  };
  K.keepOpen(root, 'data-f', () => { cur = rows(['0', '1'], []); });  // строки «2» после перерисовки нет
  assert.deepStrictEqual(cur.map(r => [r.id, r.exp, r.clicks]), [['0', true, 1], ['1', false, 0]]);
});

t('#324: server-keys.json — вложенные уровни extra, curve и шаблоны id; фикстура в них укладывается', () => {
  assert.ok(SERVER_KEYS.extra.includes('legs') && SERVER_KEYS.extra.includes('funding') && SERVER_KEYS.extra.includes('reason'));
  assert.deepStrictEqual(SERVER_KEYS.curve, ['ccy', 'kind', 'name', 'pts']);
  assert.deepStrictEqual(Object.keys(SERVER_KEYS.ids), ['CURVES', 'risk', 'fxDelta']);
  assert.deepStrictEqual(unknownKeys(loadRun(context())), []);
  // всё, что lib/real.js читает из extra позиции, сервер может отдать (EXTRA_KEYS_READ бэкенд-теста)
  const real = P('lib/real.js'), read = new Set();
  for (const m of real.matchAll(/\b(?:ex|extra|d\.extra)\.([a-z][a-z0-9_]*)/g)) read.add(m[1]);
  assert.ok(read.size > 10);
  assert.deepStrictEqual([...read].filter(k => !SERVER_KEYS.extra.includes(k)).sort(), []);
});

t('#324: тест, дописавший неизвестное поле во вложенную структуру прогона, падает на чтении прогона', () => {
  const real = mutate => { const ctx = context(); mutate(loadRun(ctx)); vm.runInContext(P('lib/real.js'), ctx); return ctx.window.MOCK; };
  const withRisk = D => D.positions.find(p => Object.keys(p.risk || {}).length);
  const bad = [
    [D => { D.positions[0].extra = { legs: [], fooBar: 1 }; }, /extra\.fooBar/],
    [D => { D.CURVES['RUB OFZ'].points = []; }, /curve\.points/],
    [D => { D.CURVES.ruonia = D.CURVES['RUB RUONIA']; }, /CURVES\.ruonia/],
    [D => { const p = withRisk(D), c = Object.keys(p.risk)[0]; p.risk[c] = { '1Y': 5 }; }, /risk\./],  // форма факта, не массив по бакетам
    [D => { const p = withRisk(D), c = Object.keys(p.risk)[0]; p.risk[c] = p.risk[c].slice(1); }, /risk\./],
    [D => { withRisk(D).risk.ruonia = D.BUCKETS.map(() => 0); }, /risk\.ruonia/],
    [D => { D.positions[0].fxDelta = { usd: 1 }; }, /fxDelta\.usd/],
    [D => { D.positions[0].fxDelta = { USD: { delta: 1 } }; }, /fxDelta\.USD/],
  ];
  bad.forEach(([mutate, re]) => assert.throws(() => real(mutate), e => /server-keys\.json/.test(e.message) && re.test(e.message), String(mutate)));
  // то, что сервер отдаёт, разрешено: новая кривая с риском, FX-дельта, extra из контракта страниц
  assert.ok(real(D => {
    D.CURVES['CNY FWD'] = { name: 'CNY implied', ccy: 'CNY', kind: 'swap', pts: D.BUCKETS.map(() => 5) };
    Object.assign(withRisk(D), { fxDelta: { USD: 1e6 }, extra: { legs: [], reason: 'x', funding: { state: 'x' } } });
    withRisk(D).risk['CNY FWD'] = D.BUCKETS.map((b, i) => i ? 0 : null);
  }).positions.length);
});

console.log(n + ' tests ok');
