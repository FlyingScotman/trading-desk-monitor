// Мониторы (prototypes/concepts): подпись отрицательного столбца бара у его конца — общий помощник K.barEnd в C06, C13
// и разборе K.drill (#329; геометрия в браузере — ui_pack7_polish.pw.test.js); сверка фикстуры с сервером строже (#337):
// валюта и вид кривой, коды валют в FX, fxDelta и ccy позиции, курсы FX, вложенные структуры extra, сверка
// EXTRA_KEYS_READ без комментариев real.js.
//   node tests/js/ui_pack7_polish.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun, unknownKeys, jsCode, SERVER_KEYS } = require('./fixtures/concepts_run');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function context() {
  const store = new Map();
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

t('#329: K.barEnd — подпись отрицательного столбца у его конца; C06, C13 и разбор берут его', () => {
  const ctx = context();
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  const K = ctx.window.KIT, pos = x => K.barEnd(x).position, vpos = x => K.barEnd(x, true).position;
  assert.deepStrictEqual([-1, 0, 1, null].map(pos), ['left', 'right', 'right', 'right']);
  assert.deepStrictEqual([-1, 0, 1, null].map(vpos), ['bottom', 'top', 'top', 'top']);
  // место слева под подпись — только когда есть отрицательные
  assert.strictEqual(JSON.stringify([[1, 2], [0, null], [3, -0.1], []].map(K.barGap)), JSON.stringify([[0, 0], [0, 0], ['25%', 0], [0, 0]]));
  const c06 = P('c06-launchpad.html'), c13 = P('c13-issuers.html'), kit = P('lib/kit.js');
  assert.ok(c06.includes("label: K.barEnd(p.pnl.total) }))"), 'C06 «Топ вклад в PnL дня»');
  assert.ok(c06.includes("label: K.barEnd(x.v) }))"), 'C06 концентрация по эмитентам');
  assert.ok(c06.includes("return { value: v, label: K.barEnd(v) }; })"), 'C06 кэрри и фондирование по книгам');
  assert.ok(c13.includes("data: secK.map(k => { const v = sumBy(sec[k], m.val); return { value: v, label: K.barEnd(v) }; })"), 'C13 сектора');
  assert.ok(c13.includes('label: K.barEnd(v, true)'), 'C13 рейтинги');
  assert.ok(kit.includes("label: K.barEnd(x.v) })), label: { show: true, position: 'right'"), 'разбор K.drill');
  // горизонтального бара с подписью справа без K.barEnd на страницах не осталось
  fs.readdirSync(DIR).filter(f => f.endsWith('.html')).map(f => [f, P(f)]).concat([['lib/kit.js', kit]]).forEach(([f, s]) => {
    for (const m of s.matchAll(/type: 'bar'[^\n]*?label: \{ show: [^\n]*?position: 'right'/g)) assert.ok(m[0].includes('K.barEnd('), f + ': ' + m[0].slice(0, 120));
  });
});

t('#337: jsCode — комментарии вырезаны, строки, шаблоны и регулярные выражения на месте', () => {
  const src = "const a = ex.quote; // ex.fooBar\n/* extra.barBaz\n ex.qux */ const s = '// ex.inStr', u = `/* ${ex.tpl} */`;\n" +
    "const r = /\\/\\/ ex.inRe/g, q = x / 2 / 3; const z = (s.match(/[/]/g) || []).length; // хвост\n";
  const c = jsCode(src);
  assert.ok(!/fooBar|barBaz|qux|хвост/.test(c), c);
  ['ex.quote', "'// ex.inStr'", '`/* ${ex.tpl} */`', '/\\/\\/ ex.inRe/g', 'x / 2 / 3', '/[/]/g'].forEach(x => assert.ok(c.includes(x), x + ' в ' + c));
  // страница без комментариев — тот же код: компилируется
  ['lib/real.js', 'lib/kit.js', 'lib/mock.js'].forEach(f => new vm.Script(jsCode(P(f)), { filename: f }));
  // ключ только в комментарии real.js не попадает в «прочитанное» (сверка EXTRA_KEYS_READ, ui_pack6.test.js)
  const read = s => { const r = new Set(); for (const m of s.matchAll(/\b(?:ex|extra|d\.extra)\.([a-z][a-z0-9_]*)/g)) r.add(m[1]); return [...r].sort(); };
  const real = P('lib/real.js') + '\n// ex.only_in_comment, extra.another_one\n/* d.extra.third */\n';
  assert.deepStrictEqual(read(jsCode(real)), SERVER_KEYS.extraRead);
  assert.ok(read(real).includes('only_in_comment'), 'без jsCode ключ из комментария считается прочитанным');
});

t('#337: тест, внёсший в прогон согласованную опечатку или не тот код валюты, падает на чтении прогона', () => {
  const real = mutate => { const ctx = context(); mutate(loadRun(ctx)); vm.runInContext(P('lib/real.js'), ctx); return ctx.window.MOCK; };
  const withRisk = D => D.positions.find(p => Object.keys(p.risk || {}).length);
  const zeros = D => D.BUCKETS.map(() => 0);
  const bad = [
    // 1. CURVES + risk одной правкой: валюта кривой не валюта id, вид не из списка
    [D => { D.CURVES['RUB FOO'] = Object.assign({}, D.CURVES['RUB OFZ'], { ccy: 'USD' }); withRisk(D).risk['RUB FOO'] = zeros(D); }, /CURVES\.RUB FOO/],
    [D => { D.CURVES['RUB OFZ'].ccy = 'USD'; }, /CURVES\.RUB OFZ/],
    [D => { D.CURVES['RUB KBD'] = Object.assign({}, D.CURVES['RUB OFZ'], { kind: 'kbd' }); delete D.CURVES['RUB OFZ']; }, /CURVES\.RUB KBD/],
    [D => { D.CURVES['RUB OFZ'].kind = 'Bond'; }, /CURVES\.RUB OFZ/],
    [D => { D.CURVES.CNY.ccy = 'RUB'; }, /CURVES\.CNY/],  // курс CNY в FX есть: кривая CNY — в CNY
    // 2. коды валют и курсы
    [D => { D.positions[0].ccy = 'usd'; D.positions[0].fxDelta = { usd: 1 }; }, /ccy\.\S+.*fxDelta\.usd|fxDelta\.usd/],
    [D => { D.positions[0].ccy = 'usd'; }, /ccy\./],
    [D => { D.positions[0].ccy = 'US D'; }, /ccy\./],
    [D => { D.FX.usd = 80; D.positions[0].fxDelta = { usd: 1 }; }, /FX\.usd/],
    [D => { D.FX.XXX = 'abc'; }, /FX\.XXX/],
    [D => { D.FX.XXX = NaN; }, /FX\.XXX/],
    [D => { D.FX.XXX = true; }, /FX\.XXX/],
    // 3. вложенные структуры extra
    [D => { D.positions[0].extra = { funding: { state: 'funded', fooBar: 1 } }; }, /extra\.funding\.fooBar/],
    [D => { D.positions[0].extra = { funding: { state: 'funded', lines: [{ kind: 'POS', rate: 1 }] } }; }, /extra\.funding\.lines\[\]\.rate\b/],
    [D => { D.positions[0].extra = { funding: { state: 'funded', lines: { kind: 'POS' } } }; }, /extra\.funding\.lines\b/],
    [D => { D.positions[0].extra = { funding: 'funded' }; }, /extra\.funding\b/],
    [D => { D.positions[0].extra = { legs: [{ leg: 'single', buyCcy: 'USD' }] }; }, /extra\.legs\[\]\.buyCcy/],
    [D => { D.positions[0].extra = { legs: [['single']] }; }, /extra\.legs\[\]/],
    [D => { D.positions[0].extra = { legs: { leg: 'single' } }; }, /extra\.legs\b/],
  ];
  bad.forEach(([mutate, re]) => assert.throws(() => real(mutate), e => /server-keys\.json/.test(e.message) && re.test(e.message), String(mutate)));
  // то, что сервер отдаёт, разрешено: кривая валюты без курса — в RUB (как «USD» прогона без курса USD), курс null,
  // позиция без валюты (не оценена), новая кривая с валютой id и видом из списка, funding и legs с ключами контракта
  assert.ok(real(D => {
    D.CURVES.OMR = { name: 'OMR', ccy: 'RUB', kind: 'bond', pts: D.BUCKETS.map(() => null) };
    D.CURVES['CNY FWD'] = { name: 'CNY implied', ccy: 'CNY', kind: 'swap', pts: zeros(D) };
    D.FX.KZT = null;
    D.positions[1].ccy = null;
    Object.assign(withRisk(D), { fxDelta: { USD: 1, KZT: 2 }, extra: {
      funding: { state: 'funded', amount_rub: -1, in_pnl: true, rule_source: 'default', default_rate: true,
        lines: [{ kind: 'POS', base: 'mv_dirty', rate_expr: 'RUONIA', rate_pct: 16, ccy: 'RUB', body_ccy: -1e8, days: 1, interest_ccy: -1, interest_rub: -1, rule_id: 'r', rule_source: 'default', default_rate: true }] },
      legs: [{ leg: 'near', trade_id: 'T', value_date: '2026-11-02', buy_ccy: 'USD', buy_amount: 1, sell_ccy: 'RUB', sell_amount: 90, contract_rate: 90, rate_quote: 'USD/RUB', settlement: 'deliverable', pv_rub: 1, df: 1, fwd_rate: 90, points: 0 }] } });
    withRisk(D).risk.OMR = zeros(D);
  }).positions.length);
  assert.deepStrictEqual(unknownKeys(loadRun(context())), []);
  // ключи валют и видов — из server-keys.json, его пишет бэкенд-тест (контракт CURVE_KINDS, EXTRA_NESTED_KEYS)
  assert.deepStrictEqual(SERVER_KEYS.ids.curveKinds, ['bond', 'swap']);
  assert.strictEqual(SERVER_KEYS.ids.ccyPattern, '^[A-Z]{3}$');
  assert.deepStrictEqual(Object.keys(SERVER_KEYS.extraNested), ['funding', 'funding.lines[]', 'legs[]']);
});

console.log(n + ' tests ok');
