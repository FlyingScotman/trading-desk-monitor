'use strict';
// C19 «PnL за период»: rates by factor on D and «Explain по кривым» (TDM#437) and the unexplained by method on F
// (TDM#436) — pure helpers of lib/pnl-period.js on the synthetic answers in tests/js/fixtures/pnl_period.json. The
// factors add up to the totals by curve; the unexplained split adds up to the period's other + reported; missing — null.
const test = require('node:test'), assert = require('node:assert/strict');
const P = require('../../prototypes/concepts/lib/pnl-period.js');
const FX = require('./fixtures/pnl_period.json');
const clone = o => JSON.parse(JSON.stringify(o));
const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= (tol || 1e-6), (msg || '') + ': ' + a + ' vs ' + b);
const sum = vals => vals.reduce((s, v) => s + (v || 0), 0);

test('D: factor grids — RUONIA, the OFZ basis, the KBD-only rows with their coverage; terms + rest = Σ', () => {
  const G = P.factorGrids(FX.curves), cv = FX.curves;
  assert.deepEqual(G.linear.rows.map(r => r.id), ['ruonia', 'basis', 'kbd', '']);
  assert.deepEqual(G.linear.rows.map(r => r.name), ['RUONIA', 'Базис ОФЗ (КБД − RUONIA)', 'КБД (нет RUONIA на дату)', 'кривая не указана']);
  assert.equal(G.linear.rest, true, 'rows without terms: a column of their own');
  G.linear.rows.forEach(r => near(sum(r.vals) + (r.rest || 0), r.sum, 1, r.id));
  const kbd = G.linear.rows.find(r => r.id === 'kbd');
  assert.equal(kbd.cover, cv.factorCoverage.kbd.rows + ' записей · 1 дата');
  assert.equal(kbd.rest, null);
  // the split changes no total: Σ factors = Σ curves, linear and exact
  near(sum(Object.values(cv.factorLinearTotal)), sum(Object.values(cv.linearTotal)), 20, 'linear');
  near(sum(Object.values(cv.factorExact)), sum(Object.values(cv.exact)), 20, 'exact');
  // the basis moves as KBD − RUONIA; a factor without a move is not a row of the move grid
  const basis = G.move.rows.find(r => r.id === 'basis');
  assert.deepEqual(basis.vals, cv.buckets.map(b => cv.move['RUB OFZ'][b] - cv.move['RUB RUONIA'][b]));
  assert.deepEqual(G.move.rows.map(r => r.id), ['ruonia', 'basis', 'kbd']);
  assert.equal(G.move.rows[0].sum, null, 'a sum of moves means nothing');
  // an older server: no factors at all
  assert.deepEqual(P.factorGrids({ buckets: ['1Y'] }).linear.rows, []);
});

test('D: linear against exact by factor; the warning needs full linear coverage of the factor', () => {
  const rows = P.factorLinVsExact(FX.curves);
  assert.deepEqual(rows.map(r => r.id), ['ruonia', 'basis', 'kbd', '']);
  const b = rows.find(r => r.id === 'basis');
  assert.equal(b.diff, FX.curves.factorExact.basis - FX.curves.factorLinearTotal.basis);
  const cv = clone(FX.curves);
  cv.factorExact.basis = cv.factorLinearTotal.basis - 3e6;
  assert.equal(P.factorLinVsExact(cv).find(r => r.id === 'basis').warn, true);
  cv.factorCoverage.basis.linearRows -= 1;
  assert.equal(P.factorLinVsExact(cv).find(r => r.id === 'basis').warn, false);
  cv.factorLinearTotal.kbd = null; cv.factorExact.kbd = null;
  assert.equal(P.factorLinVsExact(cv).some(r => r.id === 'kbd'), false, 'nothing to compare: no row');
});

test('D and A: rates by factor from /period — series, positions and «Explain по кривым» rows add up to the curves', () => {
  const prep = P.prepare(FX.period);
  assert.equal(P.hasFactors(prep), true);
  assert.equal(P.hasFactors(P.prepare(Object.assign(clone(FX.period), { days: clone(FX.period.days).map(d => { delete d.ratesByFactor; return d; }) }))), false);
  const S = P.factorSeries(prep), C = P.curveSeries(prep);
  assert.deepEqual(S.map(s => s.id), ['ruonia', 'basis', 'kbd', '']);
  near(sum(S.map(s => s.total)), sum(C.map(s => s.total)), 20, 'Σ factors = Σ curves');
  const kbd = S.find(s => s.id === 'kbd');
  assert.equal(kbd.vals.filter(v => v != null).length, 1, 'one date valued on the KBD only');
  assert.equal(kbd.vals[prep.dates.indexOf('2026-10-07')], null, 'a market day without a run is a gap');
  // positions: an OFZ in RUONIA + basis is two rows; shares of a factor add up to ±100 %
  const pos = P.factorPositions(prep), ofz = pos.filter(r => r.ref === 'DEMO-A|OFZ-1');
  assert.deepEqual(ofz.map(r => r.factor), ['ruonia', 'basis', 'kbd']);
  near(sum(ofz.map(r => r.rates)), prep.positions.find(p => p.ref === 'DEMO-A|OFZ-1').c.rates, 2);
  ['ruonia', 'basis'].forEach(f => near(Math.abs(sum(pos.filter(r => r.factor === f).map(r => r.share))), 100, 1e-6, f));
  // rows of «Explain по кривым»: one per factor, the rates family, values from ratesByFactor; aggregates by factorAgg
  const rows = P.factorRows(prep);
  assert.deepEqual(rows.map(r => r.k), ['rates:f:ruonia', 'rates:f:basis', 'rates:f:kbd', 'rates:f:']);
  assert.ok(rows.every(r => r.color === 'pnl:rates' && r.parts[0] === 'rates'));
  const agg = { ratesByFactor: P.factorAgg(prep.days) };
  near(sum(rows.map(r => r.v(agg))), sum(prep.days.map(d => sum(Object.values(d.ratesByCurve)))), 20, 'period');
  assert.equal(rows[0].v({}), null, 'an aggregate without the split: null, not 0');
  assert.deepEqual(P.factorAgg([{ ratesByFactor: { kbd: null } }]), { kbd: null });
});

test('F: unexplained by method — groups, days and the top add up to the period other + reported', () => {
  const V = P.unexplainedView(FX.checks), u = FX.checks.unexplainedBy;
  assert.equal(V.total, FX.period.summary.c.other + (FX.period.summary.c.reported || 0));
  assert.deepEqual(V.groups.map(g => g.key), ['exact', 'simple', 'yield', 'other']);
  assert.equal(V.groups.find(g => g.key === 'simple').label, 'без разложения: нет потоков / модельная цена');
  near(sum(V.groups.map(g => g.total)), V.total, 1, 'groups');
  assert.equal(V.gap, 0);
  near(sum(V.series.flatMap(s => s.vals)), V.total, 1, 'days');
  assert.ok(V.series.every(s => s.vals.length === V.dates.length));
  assert.equal(V.groups[0].color, P.UNEXPL_COLOR.exact);
  assert.deepEqual(V.top.map(t => Math.abs(t.total)), V.top.map(t => Math.abs(t.total)).sort((a, b) => b - a));
  assert.equal(V.qualityLabel('model_price'), 'модельная цена');
  assert.equal(V.qualityLabel(null), 'не указано');
  assert.equal(V.topTotal, u.top.length);
  // no data: null, never a zero bar
  assert.equal(P.unexplainedView({}), null);
  assert.equal(P.unexplainedView({ unexplainedBy: Object.assign(clone(u), { total: null }) }), null);
});

test('A: the unexplained bar by method — from /checks when at hand (by row), else from /period methods (by position)', () => {
  const prep = P.prepare(FX.period), other = P.rowsFor('explain', prep, { carry: 'net' }).find(d => d.k === 'other');
  // with F's answer: the groups of unexplainedBy, their other; Σ = the bar
  const C = P.unexplainedSplit(prep.positions, other, FX.checks);
  assert.equal(C.source, 'checks');
  assert.deepEqual(C.groups.map(g => g.key), FX.checks.unexplainedBy.groups.map(g => g.key));
  near(sum(C.groups.map(g => g.total)), other.total, 1, 'checks');
  // without it: by position — one group of methods gives the whole value, several groups — «mixed»
  const S = P.unexplainedSplit(prep.positions, other, null);
  assert.equal(S.source, 'period');
  near(sum(S.groups.map(g => g.total)), other.total, 1, 'period');
  const mixed = S.groups.find(g => g.key === 'mixed');
  assert.equal(mixed.n, 1); assert.equal(mixed.total, prep.positions.find(p => p.ref === 'DEMO-A|CORP-1').c.other);
  assert.equal(S.groups.find(g => g.key === 'other').total, prep.positions.find(p => p.ref === 'DEMO-B|IRS-1').c.other, 'flash-v1: not a known method');
  assert.equal(P.methodGroup('desk-flash-exact-v2'), 'exact'); assert.equal(P.methodGroup(undefined), 'other');
  // «Разница PV по отчётам»: no reported anywhere — nothing to split (null, not zero bars)
  assert.equal(P.unexplainedSplit(prep.positions, { k: 'reported', v: p => P.val(p, 'reported') }, null), null);
  assert.equal(P.unexplainedSplit(prep.positions, { k: 'reported', v: p => P.val(p, 'reported') }, FX.checks), null, 'unexplainedBy.reported null too');
});
