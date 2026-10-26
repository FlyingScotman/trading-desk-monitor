// R10 и хвосты R8 в DESK.util (prototypes/desk-util.js, его использует Scenario Lab): PnL не посчитан — null, а не 0;
// покрытие «частично: N из M позиций»; кривая без точек (валютная заглушка «USD») — не ставка 0.
// Проверки разметки Desk Monitor удалены вместе с экраном (#292).
//   node tests/js/pnl_r10.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

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

console.log(n + ' tests passed');
