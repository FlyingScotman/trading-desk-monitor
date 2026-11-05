// PnL на допущении (#576): вместо «нет PnL» — число и текст допущения сервера (pnl.assumption). lib/real.js кладёт его
// в p.pnl.assumption, lib/kit.js показывает пометку «~» в таблице (K.pnlAssumeMark) и строку в карточке
// (K.pnlAssumeNote); у позиции без допущения и у позиции без PnL пометки нет. Демо-прогон — синтетика.
//   node --test tests/js/pnl_assumption_576.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const test = require('node:test');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const TEXT = 'цены на начало дня нет: принята равной сегодняшней с 03.11.2026, движения цены в PnL нет';

function load(mutate) {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  loadRun(ctx);
  if (mutate) mutate(ctx.window.CONCEPTS_RUN);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  return ctx.window.MOCK;
}
const bond = D => D.positions.find(p => p.instrType === 'BOND' && !p.notValued && p.pnl && p.pnl.available !== false);

test('#576 real.js: pnl.assumption доходит до позиции, без него — null', () => {
  let id, other;
  const M = load(D => {
    const d = bond(D); id = d.id;
    other = D.positions.find(p => p.instrType === 'BOND' && p.id !== id && p.pnl && p.pnl.available !== false).id;
    d.pnl = Object.assign({}, d.pnl, { assumption: TEXT });
  });
  assert.strictEqual(M.pos[id].pnl.available, true);
  assert.strictEqual(M.pos[id].pnl.assumption, TEXT);
  assert.strictEqual(M.pos[other].pnl.assumption, null);
});

test('#576 kit.js: пометка и строка карточки только у PnL на допущении', () => {
  const src = P('concepts/lib/kit.js');
  assert.match(src, /K\.pnlAssumeMark = /);
  assert.match(src, /K\.pnlAssumeNote\(p\) \+ '<div class="k-fields">'/);
  const K = {}, esc = s => String(s);
  // helpers of the file, taken as they are
  const lines = src.split('\n').filter(l => /^  K\.pnlAssumed = |^  K\.pnlAssumeMark = |^  K\.pnlAssumeNote = /.test(l));
  assert.strictEqual(lines.length, 3);
  new Function('K', 'esc', lines.join('\n'))(K, esc);
  const withA = { pnl: { available: true, assumption: TEXT } }, plain = { pnl: { available: true, assumption: null } };
  const none = { pnl: { available: false, assumption: TEXT } };
  assert.match(K.pnlAssumeMark(withA), /title="допущение: цены на начало дня нет/);
  assert.strictEqual(K.pnlAssumeMark(plain), '');
  assert.strictEqual(K.pnlAssumeMark(none), '', 'без PnL допущения не показываем');
  assert.match(K.pnlAssumeNote(withA), /PnL на допущении:<\/b> цены на начало дня нет/);
  assert.strictEqual(K.pnlAssumeNote(plain), '');
});

test('#576 kit.js: покрытие прогона называет число позиций и сумму PnL на допущении', () => {
  const src = P('concepts/lib/kit.js');
  const a = src.indexOf('K.runCoverage = function () {'), b = src.indexOf('\n  };', a);
  assert.ok(a > 0 && b > a);
  const K = {}, f = { k: v => String(Math.round(v / 1000)) };
  const ps = [
    { book: 'A', pnl: { available: true, total: 1500000, assumption: 'допущение 1' } },
    { book: 'A', pnl: { available: true, total: 500000, assumption: 'допущение 2' } },
    { book: 'B', pnl: { available: true, total: 9000000, assumption: null } },
    { book: 'B', pnl: { available: false, total: null, assumption: null } },
    { book: 'B', notValued: true, pnl: { available: false } }];
  const run = (M) => { new Function('K', 'M', 'f', 'runLabel', src.slice(a, b + 5))(K, M, f, () => ({ text: 'рынок' })); return K.runCoverage(); };
  const row = run({ positions: ps, books: [] }).rows.find(r => r.k === 'PnL на допущении');
  assert.ok(row, 'строка есть');
  assert.match(row.v, /^2 поз\., 2000 тыс ₽/);
  assert.strictEqual(row.warn, true);
  const none = run({ positions: ps.map(p => Object.assign({}, p, { pnl: Object.assign({}, p.pnl, { assumption: null }) })), books: [] });
  assert.ok(!none.rows.some(r => r.k === 'PnL на допущении'), 'без допущений строки нет');
});
