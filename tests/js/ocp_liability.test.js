// ОВП облигаций за вычетом валютного пассива фондирования в Desk Monitor (TDM#54, TDM#169): fxDelta облигации прогона
// монитора уже за вычетом пассива (extra.fx_liability), строки фондирования fxDelta не несут. Вкладка «Экспозиции»:
// «Облигации» — грязная MV, «Фондирование» — пассив, нетто — их сумма (пассив один раз); подсказка KPI ОВП — из чего цифра.
//   node tests/js/ocp_liability.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

// data.js (демо) → правка DESK под прогон (mutate) → desk-util.js
function load(mutate, extra) {
  const ctx = Object.assign({ window: {}, URLSearchParams, console }, extra || {});
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.window.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  return ctx;
}
// ---------- страница на заглушке DOM ----------
function fakeEl(id) {
  const el = { id, innerHTML: '', textContent: '', hidden: false, value: '', checked: false, open: false, dataset: {},
    clientWidth: 900, style: {}, classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, focus() {}, click() {},
    closest: () => null, querySelector: () => fakeEl(id + ' *'), querySelectorAll: () => [], appendChild() {}, contains: () => false };
  return el;
}
function page(mutate, tab) {
  const els = {}, store = { 'fidm.tab': tab || 'summary' };
  const document = {
    getElementById: id => els[id] || (els[id] = fakeEl(id)), documentElement: fakeEl('html'), body: fakeEl('body'),
    querySelectorAll: () => [], querySelector: () => null, addEventListener() {}, activeElement: null, title: '',
    createElement: () => fakeEl('x'),
  };
  const ctx = load(mutate, {
    document, location: { protocol: 'file:', search: '', hash: '' }, localStorage: { getItem: k => store[k] ?? null, setItem(k, v) { store[k] = v; }, removeItem(k) { delete store[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {} }), getComputedStyle: () => ({ getPropertyValue: () => '#808080' }),
    MutationObserver: class { observe() {} }, setTimeout, clearTimeout, Intl, innerWidth: 1400,
  });
  ctx.window.addEventListener = () => {}; ctx.window.innerWidth = 1400;
  Object.assign(ctx, { addEventListener() {} });
  const html = P('desk-monitor.html');
  const src = html.slice(html.indexOf('<script>\n(function(){') + '<script>'.length, html.lastIndexOf('</script>'));
  vm.runInContext(src, ctx);
  return els;
}


const USD = 1e6;
// прогон монитора: у валютных облигаций fxDelta = грязная MV − пассив, пассив в extra.fx_liability; у строк — пусто
const net = D => {
  D.funding.forEach(f => { f.fxDelta = {}; });
  D.positions.filter(p => p.instrType === 'BOND' && p.ccy === 'USD').forEach(p => {
    const dirty = p.fxDelta.USD, liab = -(dirty - 0.01 * USD);
    p.fxDelta = { USD: dirty + liab };
    p.extra = Object.assign({}, p.extra, { fx_liability: { state: 'computed', ccy: 'USD', amount_ccy: liab, fx_delta_net: dirty + liab } });
  });
};
const cells = html => [...html.matchAll(/<td class="n[^"]*">(?:<b>)?([^<]*)</g)].map(m => m[1]);

t('Экспозиции: облигации — грязная MV, фондирование — пассив, нетто — пассив один раз', () => {
  let bonds = 0, liabs = 0;
  const els = page(D => { net(D); D.positions.filter(p => p.extra && p.extra.fx_liability).forEach(p => { bonds++; liabs += p.extra.fx_liability.amount_ccy; }); }, 'expo');
  assert.ok(bonds > 0, 'в демо есть USD-облигации');
  const row = els['ex-ocp'].innerHTML.split('<tr><td>USD</td>')[1].split('</tr>')[0];
  const v = cells(row);  // облигации, фондирование, перенос, деривативы, FX, нетто, нетто ₽
  const n = s => +s.replace(/[\u2212−]/, '-').replace(/\s/g, '').replace(',', '.');
  assert.ok(Math.abs(n(v[1]) - liabs / USD) < 0.11, 'фондирование = пассив: ' + v[1] + ' vs ' + liabs / USD);
  assert.ok(Math.abs(n(v[0]) + n(v[1]) + n(v[2]) + n(v[3]) + n(v[4]) - n(v[5])) < 0.31, 'нетто = сумма слагаемых: ' + v.join(' | '));
});

t('KPI ОВП: подсказка называет слагаемые, обещаний сверх кода нет', () => {
  const k = page(net).kpis.innerHTML;
  assert.ok(k.includes('ОВП с фондированием') && k.includes('валютный пассив фондирования: грязная MV на прошлый перенос ОВП'), k.slice(0, 400));
  assert.ok(!P('desk-monitor.html').includes('FX бумаги гасится её фондированием'));
});

console.log(n + ' ok');
