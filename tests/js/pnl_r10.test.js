// R10 и хвосты R8 в Desk Monitor: PnL не посчитан — «—» с причиной, а не 0; итоги — только по позициям с PnL, с пометкой
// «частично: N из M позиций»; кривая без точек (валютная заглушка «USD») — не ставка 0.
//   node tests/js/pnl_r10.test.js
// Страница (prototypes/desk-monitor.html) исполняется на заглушке DOM: проверяется разметка KPI, вкладки PnL и таблицы позиций.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

const NO_PREV = 'нет финального среза рынка за предыдущий рабочий день';
const NO_CORE = 'бумага оценена по отчёту, без ядра';
const NULL_PNL = { carry: null, rolldown: null, rates: null, spread: null, fx: null, funding: null, convexity: null,
  newTrades: null, other: null, reported: null, fxGross: null, fxFunding: null, fxGrossCbr: null, fxFundingCbr: null,
  fxCbr: null, explained: null, explainedCbr: null, fxBasis: null, unexplained: null, official: null, breakNote: null };
const noPnl = (code, text) => Object.assign({}, NULL_PNL, { available: false, reason: code, reasonText: text });

// data.js (демо) → правка DESK под прогон (mutate) → desk-util.js
function load(mutate, extra) {
  const ctx = Object.assign({ window: {}, URLSearchParams, console }, extra || {});
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.window.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  return ctx;
}
// прогон без среза рынка за предыдущий день: ни у одной позиции нет PnL
const noPrev = D => {
  D.prevDate = null; D.pnlAvailable = false; D.pnlReason = 'no_prev_market'; D.pnlReasonText = NO_PREV;
  D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
  D.funding = []; D.fundingPnl = { carry: 0, fx: 0, fxCbr: 0 };
  D.positions.forEach(p => { p.pnl = noPnl('no_prev_market', NO_PREV); });
};
// частично: у первых двух позиций PnL нет (оценены по отчёту)
const partial = D => {
  D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
  D.funding = []; D.fundingPnl = { carry: 0, fx: 0, fxCbr: 0 };
  D.positions.slice(0, 2).forEach(p => { p.pnl = noPnl('valued_without_core', NO_CORE); });
};

t('кривая без точек: ставка null, спред USD-бумаги не «доходность − 0»', () => {
  const ctx = load(D => {
    Object.keys(D.CURVES).filter(c => D.CURVES[c].ccy === 'USD').forEach(c => { delete D.CURVES[c]; delete D.CURVE_MOVES[c]; });
    D.CURVES.USD = { name: 'USD: кривой нет, риск по дюрации', ccy: 'USD', kind: 'bond', pts: new Array(11).fill(null) };
    D.CURVE_MOVES.USD = new Array(11).fill(0);
  });
  const U = ctx.window.DESK.util, D = ctx.window.DESK;
  assert.strictEqual(U.hasPoints('USD'), false);
  assert.strictEqual(U.hasPoints('RUB OFZ'), true);
  assert.strictEqual(U.curveRate('USD', 3), null);
  assert.strictEqual(U.baseCurve('USD'), 'USD');
  const p = D.positions.find(x => x.instrType === 'BOND' && x.ccy === 'USD' && x.couponType !== 'float' && x.ytm != null);
  assert.ok(p, 'в демо есть USD-облигация с фикс. купоном');
  assert.strictEqual(U.spreadTo(p, 'base'), null);
  assert.strictEqual(U.spreadTo(p, 'USD'), null);
  assert.strictEqual(U.spreadTo(p, 'USD', true), null);  // вчера — тоже
  assert.ok(U.spreadTo(D.positions.find(x => x.instrType === 'BOND' && x.ccy === 'RUB' && x.couponType === 'fix'), 'RUB OFZ') != null);
  assert.ok(isFinite(U.runCarry({ rolldown: true }, { horizon: '3M' }).total.rolldown));  // заглушка не даёт NaN в ролл-дауне
});

t('покрытие PnL: «частично: N из M позиций», причины по убыванию', () => {
  const ctx = load(partial), D = ctx.window.DESK, U = D.util;
  const c = U.pnlCover(D.positions);
  assert.strictEqual(c.n, D.positions.length - 2);
  assert.ok(c.partial && !c.none);
  assert.ok(c.tip.startsWith('частично: ' + c.n + ' из ' + c.m + ' позиций'), c.tip);
  assert.strictEqual(JSON.stringify(c.reasons), JSON.stringify([[NO_CORE, 2]]));
  assert.strictEqual(D.positions[0].pnl.explainedCbr, null, 'compat() не превращает null в 0');
  const none = U.pnlCover(D.positions.slice(0, 2));
  assert.ok(none.none && none.tip.includes(NO_CORE));
  const rows = U.bondRows(D.positions.slice(0, 2));
  assert.ok(rows.every(r => r.pnl === null), 'выпуск без PnL — null, не 0');
});

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

t('прогон без предыдущего рынка: KPI «—» с причиной, не 0', () => {
  const els = page(noPrev);
  const k = els.kpis.innerHTML;
  const pnl = k.slice(k.indexOf('PnL дня'), k.indexOf('MV брутто'));
  assert.ok(pnl.includes('class="na" title="PnL не посчитан: ' + NO_PREV + '"'), pnl);
  assert.ok(!/>[+−]?0,0</.test(pnl), 'нет «0,0» в KPI PnL: ' + pnl);
  assert.ok(pnl.includes('не посчитан'));
});

t('прогон без предыдущего рынка: вкладка PnL — «—» по книгам и в итоге, причина в тексте', () => {
  const els = page(noPrev, 'pnl');
  assert.ok(els['pnl-side'].innerHTML.includes(NO_PREV));
  const bt = els['pnl-books'].innerHTML;
  assert.ok(bt.includes('PnL не посчитан') && bt.includes('class="na"'));
  assert.ok(!/>[+−]?0,00</.test(bt), 'нет нулей в таблице по книгам');
});

t('частично: KPI с пометкой «частично: N из M позиций», итог по книгам — по позициям с PnL', () => {
  const els = page(partial, 'pnl');
  const k = els.kpis.innerHTML;
  assert.ok(/частично: \d+ из \d+ позиций/.test(k), k);
  assert.ok(els['pnl-side'].innerHTML.includes('частично:') && els['pnl-side'].innerHTML.includes(NO_CORE));
  assert.ok(els['pnl-books'].innerHTML.includes('частично:'));
});

t('таблица позиций: «—» с причиной в ячейке PnL', () => {
  const els = page(partial, 'positions');
  const tw = els['pos-tw'].innerHTML;
  assert.ok(tw.includes('title="PnL не посчитан: ' + NO_CORE + '"'), 'подсказка с причиной');
});

t('каждая вкладка строится: демо, прогон без PnL с валютной кривой-заглушкой', () => {
  const usdStub = D => {
    noPrev(D);
    D.CURVES.USD = { name: 'USD: кривой нет, риск по дюрации', ccy: 'USD', kind: 'bond', pts: new Array(11).fill(null) };
    D.CURVE_MOVES.USD = new Array(11).fill(0);
    D.positions.filter(p => p.instrType === 'BOND' && p.ccy === 'USD').forEach(p => { p.risk = { USD: p.risk[Object.keys(p.risk)[0]] }; });
  };
  ['summary', 'positions', 'bonds', 'risk', 'expo', 'funding', 'scen', 'pnl'].forEach(tab => {
    page(null, tab);
    const els = page(usdStub, tab);
    assert.ok(els.kpis.innerHTML.includes('PnL дня'), tab);
  });
});

t('вкладка рисков на кривой без точек: график кривой не строится, ошибок нет', () => {
  const els = page(D => {
    const old = D.CURVES; delete old['RUB RUONIA'];
    D.CURVES = Object.assign({ USD: { name: 'USD: кривой нет', ccy: 'USD', kind: 'bond', pts: new Array(11).fill(null) } }, old);
    delete D.CURVE_MOVES.USD;  // и без изменений за день
  }, 'risk');
  assert.ok(els['risk-crv-title'].textContent.startsWith('USD: кривой нет'));
});

console.log(n + ' tests passed');
