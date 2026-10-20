// R16: купон в карточке позиции, в таблице облигаций и в истории выпуска — DESK.util.couponView. Фикс — ставка,
// флоатер — «индекс + маржа» и прогноз «≈ …, прогноз» с подсказкой, бескупонная — «бескупонная», неизвестный — «—»;
// ни «null», ни «undefined», ни «NaN» — и на прогоне до правки D4 (флоатер без индекса и маржи).
//   node tests/js/coupon_r16.test.js
// Страница (prototypes/desk-monitor.html) исполняется на заглушке DOM, карточка открывается щелчком по строке позиции.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const BAD = /\bnull\b|\bundefined\b|\bNaN\b/;

// позиции-облигации прогона по бэкенду: фикс, флоатер с прогнозом, бескупонная, без купона, прогон до D4
const BONDS = {
  FIX: { couponType: 'fix', coupon: 12.5, index: null, margin: null, extra: { coupon_basis: 'rate' } },
  FLT: { couponType: 'float', coupon: 17.2, index: 'RUONIA', margin: 1.2, extra: { coupon_basis: 'projected' } },
  FLA: { couponType: 'float', coupon: 15.7558, index: 'КС', margin: 1.75, extra: { coupon_basis: 'amount' } },
  ZER: { couponType: 'zero', coupon: 0, index: null, margin: null, extra: { coupon_basis: 'zero' } },
  UNK: { couponType: 'fix', coupon: null, index: null, margin: null, extra: { coupon_basis: null } },
  OLD: { couponType: 'float', coupon: null },                                  // прогон до D4: ни индекса, ни маржи
  ODD: { couponType: 'float', coupon: NaN, index: 'null', margin: undefined },  // мусор не печатается
  ASM: { couponType: 'unknown', coupon: 7.1, extra: { coupon_basis: 'assumed' } },
};

function load(mutate, extra) {
  const ctx = Object.assign({ window: {}, URLSearchParams, console }, extra || {});
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.window.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  return ctx;
}
// первые облигации демо получают купон по образцам BONDS (остальные поля демо — как есть)
const withBonds = D => {
  const bonds = D.positions.filter(p => p.instrType === 'BOND');
  Object.keys(BONDS).forEach((k, i) => {
    const p = bonds[i], b = BONDS[k];
    ['couponType', 'coupon', 'index', 'margin'].forEach(f => { if (f in b) p[f] = b[f]; else delete p[f]; });
    p.extra = Object.assign({}, p.extra || {}, b.extra || {});
    if (!b.extra) delete p.extra.coupon_basis;
    p.name = 'R16 ' + k;
  });
};

t('couponView: фикс, флоатер, прогноз, бескупонная, неизвестный', () => {
  const U = load().window.DESK.util, v = k => U.couponView(BONDS[k]);
  assert.strictEqual(v('FIX').text, '12,50%');
  assert.strictEqual(v('FLT').text, 'RUONIA + 1,20% (≈17,20%, прогноз)');
  assert.strictEqual(v('FLT').title, 'прогноз: RUONIA + 1,20% по последнему фиксингу индекса');
  assert.strictEqual(v('FLA').text, 'КС + 1,75% (15,76%)');
  assert.ok(!v('FLA').title.includes('прогноз'));
  assert.strictEqual(v('ZER').text, 'бескупонная');
  assert.strictEqual(v('UNK').text, '—');
  assert.strictEqual(v('OLD').text, 'плав.');
  assert.strictEqual(v('ODD').text, 'плав.');
  assert.strictEqual(v('ASM').text, '7,10%');
  assert.ok(v('ASM').title.startsWith('последняя известная ставка'));
  assert.strictEqual(U.couponView({ couponType: 'float', index: 'КС', margin: -0.5, coupon: null }).text, 'КС − 0,50%');
  assert.strictEqual(U.couponText({ couponType: 'fix', coupon: '9.5' }), '9,50%');  // число строкой
  Object.keys(BONDS).forEach(k => {
    const x = v(k);
    assert.ok(x.text && !BAD.test(x.text) && !BAD.test(x.title), k + ': ' + JSON.stringify(x));
  });
});

t('демо: у каждой позиции текст купона без null', () => {
  const D = load().window.DESK;
  D.positions.forEach(p => { const x = D.util.couponView(p); assert.ok(!BAD.test(x.text + x.title), p.name + ': ' + x.text); });
});

// ---------- страница на заглушке DOM ----------
function fakeEl(id) {
  return { id, innerHTML: '', textContent: '', hidden: false, value: '', checked: false, open: false, dataset: {},
    clientWidth: 900, style: {}, classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, focus() {}, click() {},
    closest: () => null, querySelector: () => fakeEl(id + ' *'), querySelectorAll: () => [], appendChild() {}, contains: () => false };
}
function page(mutate, tab) {
  const els = {}, store = { 'fidm.tab': tab || 'summary' }, clicks = [];
  const document = {
    getElementById: id => els[id] || (els[id] = fakeEl(id)), documentElement: fakeEl('html'), body: fakeEl('body'),
    querySelectorAll: () => [], querySelector: () => null, addEventListener: (ev, f) => { if (ev === 'click') clicks.push(f); },
    activeElement: null, title: '', createElement: () => fakeEl('x'),
  };
  const ctx = load(mutate, {
    document, location: { protocol: 'file:', search: '', hash: '' }, localStorage: { getItem: k => store[k] ?? null, setItem(k, v) { store[k] = v; }, removeItem(k) { delete store[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }), getComputedStyle: () => ({ getPropertyValue: () => '#808080' }),
    MutationObserver: class { observe() {} }, setTimeout, clearTimeout, Intl, innerWidth: 1400,
    CSS: { escape: s => s },
  });
  ctx.window.addEventListener = () => {}; ctx.window.innerWidth = 1400;
  Object.assign(ctx, { addEventListener() {} });
  const html = P('desk-monitor.html');
  const src = html.slice(html.indexOf('<script>\n(function(){') + '<script>'.length, html.lastIndexOf('</script>'));
  vm.runInContext(src, ctx);
  // щелчок по строке позиции: делегированный обработчик страницы видит [data-act="pos"][data-v=id]
  const open = id => {
    const target = { closest: s => s === '[data-act]' ? { dataset: { act: 'pos', v: id } } : null };
    clicks.forEach(f => f({ target, preventDefault() {} }));
    return els.drawer.innerHTML;
  };
  return { els, D: ctx.window.DESK, open };
}
const text = h => h.replace(/<[^>]*>/g, ' ');                                   // видимый текст разметки
const coupon = h => { const m = h.match(/<dt>Купон<\/dt><dd title="([^"]*)">([^<]*)<\/dd>/); return m && { title: m[1], text: m[2] }; };

t('карточка позиции: купон по образцам, без null в тексте и подсказках', () => {
  const { D, open } = page(withBonds, 'positions');
  const want = { FIX: '12,50%', FLT: 'RUONIA + 1,20% (≈17,20%, прогноз)', FLA: 'КС + 1,75% (15,76%)', ZER: 'бескупонная',
    UNK: '—', OLD: 'плав.', ODD: 'плав.', ASM: '7,10%' };
  Object.keys(want).forEach(k => {
    const p = D.positions.find(x => x.name === 'R16 ' + k), h = open(p.id), c = coupon(h);
    assert.ok(c, k + ': нет строки «Купон»');
    assert.strictEqual(c.text, want[k], k);
    const kv = h.slice(h.indexOf('<h4>Параметры</h4>'), h.indexOf('</dl>', h.indexOf('<h4>Параметры</h4>')));
    assert.ok(!BAD.test(kv), k + ': ' + kv.match(/.{0,80}(null|undefined|NaN).{0,40}/));
  });
  const flt = coupon(open(D.positions.find(x => x.name === 'R16 FLT').id));
  assert.ok(flt.title.includes('прогноз: RUONIA + 1,20%'), flt.title);
});

t('карточка: пустые поля отчёта — «—», не null', () => {
  const { D, open } = page(D => {
    withBonds(D);
    D.positions.find(x => x.name === 'R16 FIX').extra.reported = { source_instrument_id: 'X1', x: { x_float_index: null, x_yield: 5 } };
  }, 'positions');
  const h = open(D.positions.find(x => x.name === 'R16 FIX').id), rep = h.slice(h.indexOf('<h4>Из отчёта</h4>'));
  assert.ok(rep.includes('x_float_index') && !BAD.test(text(rep).split('DV01')[0]), rep.slice(0, 400));
});

t('каждая облигация демо: карточка без null', () => {
  const { D, open } = page(null, 'positions');
  D.positions.filter(p => p.instrType === 'BOND').forEach(p => {
    const h = open(p.id), kv = h.slice(h.indexOf('<h4>Параметры</h4>'), h.indexOf('</dl>', h.indexOf('<h4>Параметры</h4>')));
    assert.ok(coupon(h) && !BAD.test(kv), p.name);
  });
});

t('таблица облигаций: купон по образцам, без null', () => {
  const { els } = page(withBonds, 'bonds');
  const tw = els['bd-tw'].innerHTML;
  assert.ok(tw.includes('RUONIA + 1,20% (≈17,20%, прогноз)') && tw.includes('title="прогноз: RUONIA + 1,20%'), 'прогноз флоатера');
  assert.ok(tw.includes('>12,50%<') && tw.includes('>бескупонная<'));
  assert.ok(!BAD.test(text(tw)), text(tw).match(/.{0,60}(null|undefined|NaN).{0,30}/));
});

console.log(n + ' tests passed');
