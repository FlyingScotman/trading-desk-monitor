// Мониторы (prototypes/concepts): подписи водопада PnL без «+0.0» и денежные оси через K.tick, переключатель на 390 px
// в две строки (#272); единица меры C13 «Концентрация» — в подписи над графиком, а не у оси (#284); ключи, которые тест
// дописывает в фикстуру прогона, сверяются с server-keys.json (#310).
//   node tests/js/ui_pack5.test.js
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
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, NodeFilter: { SHOW_TEXT: 4 } };
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
const PAGES = fs.readdirSync(DIR).filter(f => /^c\d\d-.*\.html$/.test(f));

t('#272: подпись столбца водопада — без «+0.0»: малым факторам больше знаков; не помещается или почти ноль — без подписи', () => {
  for (const repr of ['RUB', 'USD']) {
    const { K } = load(repr);
    assert.strictEqual(K.wfLabel(1.24e6), '+1.2');
    assert.strictEqual(K.wfLabel(0.35e6), '+0.35');
    assert.strictEqual(K.wfLabel(38e3), '+0.038');
    assert.strictEqual(K.wfLabel(-6e3), '(0.006)');
    assert.strictEqual(K.wfLabel(200), '', 'меньше 500 — без подписи');
    assert.strictEqual(K.wfLabel(0), '');
    const w = K.labelWidth('+0.012', 11);
    assert.strictEqual(K.wfLabel(12e3, w - 1), '', 'шире столбца — без подписи');
    assert.strictEqual(K.wfLabel(12e3, w + 1), '+0.012', 'место есть — подписан');
    for (let v = -2e6; v <= 2e6; v += 777) assert.ok(!/^[+(]?0\.0+\)?$/.test(K.wfLabel(v)), repr + ' ' + v + ': ' + K.wfLabel(v));
  }
});

t('#272: «Итого» водопада подписан всегда; подсказка — точное значение без порога', () => {
  const { K } = load('USD');
  assert.strictEqual(K.wfLabel(200, 5, true), '+0.00', 'итого меньше 500 и в узком столбце — подписано');
  assert.strictEqual(K.wfLabel(0, 0, true), '0.00');
  assert.strictEqual(K.wfLabel(-94e3, 0, true), '(0.09)', 'не больше двух знаков, как PnL дня в KPI C10');
  assert.strictEqual(K.wfLabel(-2.46e6, 0, true), '(2.5)');
  assert.deepStrictEqual([K.wfTip(200), K.wfTip(-6e3), K.wfTip(0.35e6), K.wfTip(1.234e6), K.wfTip(0)], ['+0.000', '(0.006)', '+0.350', '+1.23', '0.000']);
  const kit = P('lib/kit.js'), c10 = P('c10-pnl-explain.html');
  assert.ok(kit.includes("text: K.wfLabel(v, api.size([1, 0])[0] - 1, i === vals.length)"), 'C06: водопад K.pnlWaterfall');
  assert.ok(kit.includes("<b>' + K.wfTip(p.dataIndex < vals.length ? vals[p.dataIndex] : run) + '</b> mm ₽'"), 'C06: подсказка');
  assert.ok(c10.includes("text: K.wfLabel(v, api.size([1, 0])[0] - 1, last)"), 'C10: свой водопад');
  assert.ok(c10.includes("<b>' + K.wfTip(p.value[3]) + '</b> mm ₽'"), 'C10: подсказка');
  assert.ok(!/text: f\.mns\(v, 1\)|K\.wfLabel\([^)]*\) \|\| '0'/.test(kit + c10));
});

t('#272: денежные оси (тыс/бп, bn) — K.tick, без f.dv и f.bn', () => {
  PAGES.concat(['lib/kit.js']).forEach(f => {
    const s = P(f);
    assert.ok(!/axisLabel: \{ formatter: v => f\.(dv|bn)\(/.test(s), f + ': ось через f.dv / f.bn');
  });
  const c11 = P('c11-history.html');
  assert.ok(c11.includes("const ax = opts.tick ? v => K.tick(v, opts.tick) : opts.fmt;"), 'C11: деления по масштабу');
  assert.ok(c11.includes("{ fmt: v => f.dv(v), tick: 1e3, unit: 'тыс ₽/бп' }") && c11.includes("{ fmt: v => f.bn(v, 1), tick: 1e9, unit: 'bn ₽', stack: true }"));
  assert.ok(/yAxis: \[\{ type: 'value', unit: 'тыс ₽\/бп', axisLabel: \{ formatter: v => K\.tick\(v, 1e3\) \} \}/.test(P('c02-risk-ladder.html')), 'C02: ось DV01');
  const { K } = load('USD');
  assert.deepStrictEqual([K.tick(500, 1e3), K.tick(-2500, 1e3), K.tick(0.6e9, 1e9), K.tick(20e3, 1e3)], ['0.5', '(2.5)', '0.6', '20']);
});

t('#272: смоук на 390 px проверяет, что кнопки переключателя видны без прокрутки; переключатель переносится, а не сжимается', () => {
  const s = P('tools/smoke.js');
  assert.ok(s.includes("querySelectorAll('.k-seg')") && s.includes('s.scrollWidth > s.clientWidth + 1') && s.includes('кнопки переключателя за краем'));
  const css = P('lib/kit.css'), m = css.slice(css.indexOf('@media (max-width: 720px)')), block = m.slice(0, m.indexOf('\n}\n'));
  assert.ok(/\n\.k-seg \{ display: inline-flex; flex: none;/.test(css), 'переключатель не сжимается');
  assert.ok(/\n\.k-seg button \{[^}]*white-space: nowrap; \}/.test(css), 'подпись кнопки в одну строку');
  assert.ok(/\.k-seg \{ max-width: 100%; flex-wrap: wrap; \}/.test(block) && /\.k-seg button \{ flex: 1 1 auto;[^}]*margin: 0 -1px -1px 0; \}/.test(block), '390 px: в две строки, крайние границы под рамкой');
  assert.ok(/\n\.k-rm-bar \.k-seg \{ max-width: 100%; flex-wrap: wrap; \}/.test(css), 'полоса матрицы рисков (C06 на 1024 px)');
  assert.ok(/\n\.k-panel > \.hd \{ display: flex; flex-wrap: wrap;/.test(css), 'шапка панели переносится на любой ширине (C17 на 721–800 px)');
});

t('#284: C13 «Концентрация» — единица меры в подписи над графиком, у осей её нет; без данных — без единицы', () => {
  const s = P('c13-issuers.html');
  assert.strictEqual((s.match(/<span class="mu"><\/span>/g) || []).length, 3, 'Парето, сектора, рейтинги');
  assert.ok(s.includes('топ-15 эмитентов<span class="mu"></span> · накопленная доля, % ·'), 'Парето: единица у эмитентов, доля — в %');
  assert.ok(s.includes("u.textContent = on ? ', ' + K.u(m.unit) : '';"));
  assert.ok(s.includes("unitOf('pareto', true); unitOf('sector', !NOSEC); unitOf('rating', !NORAT);"));
  const strip = s.slice(s.indexOf('function renderStrip'), s.indexOf('stripCharts.push(rc)'));
  assert.ok(strip && !/type: 'value', unit: m\.unit/.test(strip), 'у осей полосы нет name');
  assert.ok(strip.includes("xAxis: { type: 'value', splitNumber: 3, axisLabel: { formatter: x => K.tick(x, m.sc), hideOverlap: true } }"), 'сектора: деления не слипаются');
});

t('#310: server-keys.json — ключи сервера по уровням, фикстура в них укладывается', () => {
  assert.deepStrictEqual(Object.keys(SERVER_KEYS), ['top', 'source', 'position', 'pnl']);
  Object.values(SERVER_KEYS).forEach(ks => assert.ok(ks.length && ks.every(k => typeof k === 'string')));
  assert.ok(SERVER_KEYS.position.includes('risk') && !SERVER_KEYS.position.includes('dv01'), 'DV01 позиции real.js считает из risk');
  const ctx = context();
  assert.deepStrictEqual(unknownKeys(loadRun(ctx)), []);
});

t('#310: тест, дописавший в прогон поле, которого сервер не отдаёт, падает на чтении прогона', () => {
  const real = mutate => { const ctx = context(); mutate(loadRun(ctx)); vm.runInContext(P('lib/real.js'), ctx); return ctx.window.MOCK; };
  const bad = [
    D => { D.positions[0].dv01 = 0; },
    D => { D.positions[1].pnl.carryDay = 1; },
    D => { D.source.runKind = 'x'; },
    D => { D.FX_RATES = {}; },
  ];
  bad.forEach(mutate => assert.throws(() => real(mutate), /server-keys\.json/));
  // и когда тест правит window.CONCEPTS_RUN после loadRun или подменяет документ целиком
  const ctx = context(); loadRun(ctx); ctx.window.CONCEPTS_RUN.positions[0].foo = 1;
  assert.throws(() => vm.runInContext(P('lib/real.js'), ctx), /position\.foo/);
  const ctx2 = context(); const D = loadRun(ctx2); ctx2.window.CONCEPTS_RUN = Object.assign({}, D, { extraTop: 1 });
  assert.throws(() => ctx2.window.CONCEPTS_RUN, /top\.extraTop/);
  // ключ, который сервер отдаёт, а демо-фикстура — нет, разрешён
  const fixture = new Set(loadRun(context()).positions.flatMap(p => Object.keys(p)));
  const known = SERVER_KEYS.position.find(k => !fixture.has(k) && k !== 'notValued');
  if (known) assert.ok(real(D => { D.positions[0][known] = D.positions[0][known] === undefined ? null : D.positions[0][known]; }));
  assert.ok(real(D => { D.positions[0].pnl.funding = null; delete D.fundingStatus; }).positions.length);
});

console.log(n + ' tests ok');
