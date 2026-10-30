// Невозможный риск позиции на экранах текущего прогона (#460, за #170): позиция с extra.risk_problem прогона —
// без риска, как не оценённая по риску. lib/real.js: её DV01 по кривым, CS01, базис и бакеты — нули, ни в одной сумме
// экранов; MV и номинал остаются; причина — в позиции (p.riskProblem: карточка) и в примечаниях прогона. lib/instr.js
// (таблица «валюта × тип»): причина implausible, текст — зеркало api/concepts.MISSING_TEXT; в ячейке, как у
// api/concepts.risk_table, только номинал и MV. Демо-прогон tests/js/fixtures/concepts-run.json, синтетика.
//   node --test tests/js/risk_problem_460.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const test = require('node:test');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const WHY = 'риск RUB RUONIA 5Y 100,000,000,000 ₽/бп невозможен: больше 1,000,000,000 ₽/бп';

function load(mutate) {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  loadRun(ctx);
  if (mutate) mutate(ctx.window.CONCEPTS_RUN);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  const M = ctx.window.MOCK;
  M.toRepr = {}; Object.keys(M.fx).forEach(c => M.toRepr[c] = M.fx[c]);  // как kit.js в рублях
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M, I: ctx.window.INSTR };
}
// облигация с риском на RUONIA и базисом ОФЗ: на ней видно и DV01, и базис
const pick = D => D.positions.find(p => p.instrType === 'BOND' && !p.notValued && p.ccy === 'RUB' && p.risk && p.risk['RUB RUONIA'] && p.ofzSpr01);
const flag = D => { const d = pick(D); d.extra = Object.assign({}, d.extra, { risk_problem: WHY }); return d.id; };
const sumBy = (L, f) => L.reduce((s, p) => s + (f(p) || 0), 0);
const sum = (r, mm) => (r.m[mm] || []).reduce((s, x) => s + x.v, 0);

test('#460 real.js: позиция с extra.risk_problem — без риска, в суммах DV01 экранов её нет, MV остаётся', () => {
  const base = load(), bad = load(flag);
  const id = pick(JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'concepts-run.json'), 'utf8'))).id;
  const p0 = base.M.positions.find(p => p.id === id), p = bad.M.positions.find(p => p.id === id);
  assert.ok(p0.dv01 && p0.dv01ByCurve['RUB RUONIA'], 'у позиции в обычном прогоне риск есть');
  assert.strictEqual(p0.riskProblem, null);
  assert.strictEqual(p.riskProblem, WHY, 'причина — в позиции (карточка)');
  assert.strictEqual(p.dv01, 0); assert.strictEqual(p.cs01, 0); assert.strictEqual(p.basis01, 0);
  assert.ok(Object.values(p.dv01ByCurve).every(v => v === 0), 'DV01 по кривым — нули');
  assert.ok(Object.values(p.risk).every(a => a.every(v => v === 0)), 'бакеты — нули');
  assert.strictEqual(p.mv, p0.mv); assert.strictEqual(p.notional, p0.notional);
  // суммы экранов (K.list() — позиции прогона): DV01 по кривой, итог DV01 — без неё, MV — с ней
  const L0 = base.M.positions, L = bad.M.positions;
  assert.ok(Math.abs(sumBy(L, x => x.dv01ByCurve['RUB RUONIA']) - (sumBy(L0, x => x.dv01ByCurve['RUB RUONIA']) - p0.dv01ByCurve['RUB RUONIA'])) < 1e-6);
  assert.ok(Math.abs(sumBy(L, x => x.dv01) - (sumBy(L0, x => x.dv01) - p0.dv01)) < 1e-6);
  assert.strictEqual(sumBy(L, x => x.mv), sumBy(L0, x => x.mv));
  assert.ok(bad.M.notes.some(n => n.includes('невозможным риском') && n.includes(String(id))), 'примечание прогона называет позицию');
  assert.ok(!base.M.notes.some(n => n.includes('невозможным риском')));
});

test('#460 instr.js: «валюта × тип» — причина implausible, риска в ячейке нет, номинал и MV есть', () => {
  const base = load(), bad = load(flag);
  const id = bad.M.positions.find(p => p.riskProblem).id;
  const p = bad.M.positions.find(x => x.id === id), p0 = base.M.positions.find(x => x.id === id);
  const kind = bad.I.kindOf(p);
  assert.strictEqual(bad.I.missing(p, kind), 'implausible');
  assert.strictEqual(base.I.missing(p0, kind), null);
  const T0 = base.I.table(base.M.positions, { units: 'repr' }), T = bad.I.table(bad.M.positions, { units: 'repr' });
  const cell = T.cell['RUB|' + kind], cell0 = T0.cell['RUB|' + kind];
  assert.strictEqual(JSON.stringify(cell.missing.filter(x => x.p.id === id).map(x => x.why)), '["implausible"]');
  for (const mm of ['dv01', 'curve:RUB RUONIA', 'cs01', 'basis', 'ofzSpr', 'fx'])
    assert.ok(!(cell.m[mm] || []).some(x => x.p.id === id), mm + ': позиции нет');
  assert.ok((cell.m.mv || []).some(x => x.p.id === id), 'MV: позиция есть');
  const own0 = (cell0.m.dv01 || []).filter(x => x.p.id === id).reduce((s, x) => s + x.v, 0);
  assert.ok(own0, 'в обычном прогоне DV01 позиции — в ячейке');
  assert.ok(Math.abs(sum(cell, 'dv01') - (sum(cell0, 'dv01') - own0)) < 1e-3, 'DV01 ячейки — без позиции');
  assert.ok(Math.abs(sum(T.total, 'dv01') - (sum(T0.total, 'dv01') - p0.dv01)) < 1e-3, 'итог DV01 таблицы — без позиции');
  assert.strictEqual(sum(T.total, 'mv'), sum(T0.total, 'mv'));
});

test('#460 паритет причин: INSTR.MISSING_TEXT = api/concepts.MISSING_TEXT (ключи и тексты)', () => {
  const { I } = load();
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', 'api', 'concepts.py'), 'utf8');
  const block = py.slice(py.indexOf('MISSING_TEXT = {'), py.indexOf('GROUPS = ('));
  // словарь Python: "ключ": "текст" или "текст" "продолжение" (неявная склейка строк)
  const out = {};
  for (const m of block.matchAll(/"([a-z_]+)":\s*((?:"[^"]*"\s*)+)/g)) out[m[1]] = [...m[2].matchAll(/"([^"]*)"/g)].map(x => x[1]).join('');
  assert.ok(out.implausible, 'implausible в Python');
  assert.deepStrictEqual(Object.assign({}, I.MISSING_TEXT), out);
});
