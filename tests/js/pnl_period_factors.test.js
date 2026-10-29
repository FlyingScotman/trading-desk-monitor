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
  assert.equal(V.groups.find(g => g.key === 'simple').label, 'без разложения: нет потоков ядра');
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

test('F: unexplained by reason first — reasons, their methods, days and top add up; the method groups stay second', () => {
  const V = P.unexplainedView(FX.checks), u = FX.checks.unexplainedBy;
  assert.deepEqual(V.reasons.map(r => r.key), ['fallback_price', 'price_source_switch', 'yield_no_curve', 'flash_residual', 'unrecorded']);
  assert.equal(V.reasons.find(r => r.key === 'unrecorded').label, 'причина не записана (прогон до версии 23)');
  near(sum(V.reasons.map(r => r.total)), V.total, 1, 'reasons');
  assert.equal(V.reasonGap, 0);
  V.reasons.forEach(r => near(sum(r.methods.map(m => m.total)), r.total, 1, r.key + ' by method'));
  near(sum(V.reasonSeries.flatMap(s => s.vals)), V.total, 1, 'days by reason');
  assert.ok(V.reasonSeries.every(s => s.vals.length === V.dates.length));
  assert.equal(V.reasons.find(r => r.key === 'flash_residual').color, P.REASON_COLOR.flash_residual);
  assert.equal(V.reasons.find(r => r.key === 'fallback_price').methods[0].label, 'без разложения: нет потоков ядра');
  assert.ok(V.reasons.every(r => r.tip), 'every reason has its tip');
  const corp = V.top.find(t => t.ref === 'DEMO-A|CORP-1');
  assert.equal(corp.reason, 'price_source_switch'); assert.equal(corp.reasonLabel, 'смена источника цены: модель ↔ рынок');
  assert.equal(corp.reasonColor, P.REASON_COLOR.price_source_switch);
  // a response before TDM#442: no reasons, the method groups alone
  const old = P.unexplainedView({ unexplainedBy: Object.assign(clone(u), { reasons: undefined, byReasonDate: undefined }) });
  assert.deepEqual(old.reasons, []); assert.deepEqual(old.reasonSeries, []); assert.equal(old.reasonGap, null);
  assert.deepEqual(old.groups.map(g => g.key), V.groups.map(g => g.key));
});

test('A: the unexplained bar by reason, then by method — from /checks by row, else from /period codes by position', () => {
  const prep = P.prepare(FX.period), other = P.rowsFor('explain', prep, { carry: 'net' }).find(d => d.k === 'other');
  const C = P.unexplainedSplit(prep.positions, other, FX.checks);
  assert.deepEqual(C.reasons.map(r => r.key), FX.checks.unexplainedBy.reasons.map(r => r.key));
  near(sum(C.reasons.map(r => r.total)), other.total, 1, 'checks by reason');
  near(sum(C.groups.map(g => g.total)), other.total, 1, 'checks by method');
  const S = P.unexplainedSplit(prep.positions, other, null);
  near(sum(S.reasons.map(r => r.total)), other.total, 1, 'period by reason');
  const by = Object.fromEntries(S.reasons.map(r => [r.key, r]));
  assert.equal(by.mixed.total, prep.positions.find(p => p.ref === 'DEMO-A|CORP-1').c.other, 'several reasons: mixed');
  assert.equal(by.unrecorded.total, prep.positions.find(p => p.ref === 'DEMO-B|IRS-1').c.other, 'no code: unrecorded');
  assert.equal(by.yield_no_curve.n, 1);
  assert.deepEqual(S.reasons.map(r => r.key), ['yield_no_curve', 'flash_residual', 'unrecorded', 'mixed'], 'in the backend order');
  assert.equal(by.unrecorded.label, P.REASON_LABEL.unrecorded);
});

// the frontend mirror of the method groups and the reasons against the backend source (TDM#452): a renamed method or
// a new reason fails here, not silently in «другой метод»
test('mirror: METHOD_GROUP, UNEXPL_LABEL and REASON_LABEL match the backend', () => {
  const fs = require('node:fs'), path = require('node:path');
  const src = f => fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', ...f.split('/')), 'utf8');
  const consts = text => Object.fromEntries([...text.matchAll(/^([A-Z][A-Z0-9_]*) = "([^"]*)"$/gm)].map(m => [m[1], m[2]]));
  const mods = { 'pnl.flash': consts(src('pnl/flash.py')), 'valuation.fallback': consts(src('valuation/fallback.py')), 'pnl.reasons': consts(src('pnl/reasons.py')) };
  const checks = src('api/pnl_checks.py');
  // the names pnl_checks imports, by alias
  const names = {};
  for (const m of checks.matchAll(/^from \.\.(pnl\.flash|valuation\.fallback) import (\w+)(?: as (\w+))?$/gm)) names[m[3] || m[2]] = mods[m[1]][m[2]];
  const block = checks.match(/^from \.\.pnl\.reasons import \(\n([\s\S]*?)\)/m)[1];
  for (const n of block.match(/\w+/g)) if (mods['pnl.reasons'][n] != null) names[n] = mods['pnl.reasons'][n];
  const at = k => checks.slice(checks.indexOf(k + ' = {'), checks.indexOf('}', checks.indexOf(k + ' = {')));
  const py = Object.fromEntries([...at('METHOD_GROUP').matchAll(/(\w+): "(\w+)"/g)].map(m => { assert.ok(names[m[1]], 'resolved ' + m[1]); return [names[m[1]], m[2]]; }));
  assert.deepEqual(P.METHOD_GROUP, py);
  const labels = Object.fromEntries([...at('UNEXPL_GROUPS').matchAll(/"(\w+)": "([^"]+)"/g)].map(m => [m[1], m[2]]));
  const { mixed, ...js } = P.UNEXPL_LABEL;
  assert.deepEqual(js, labels); assert.ok(mixed);
  const r = src('pnl/reasons.py'), rc = mods['pnl.reasons'];
  const ot = r.slice(r.indexOf('OTHER_REASON_TEXT = {'), r.indexOf('}', r.indexOf('OTHER_REASON_TEXT = {')));
  const reasons = Object.fromEntries([...ot.matchAll(/(\w+): "([^"]+)"/g)].map(m => [rc[m[1]], m[2]]));
  const since = r.match(/^REASONS_SINCE = (\d+)$/m)[1];
  reasons[rc.UNRECORDED] = r.match(/^NO_REASON_TEXT = f"([^"]+)"$/m)[1].replace('{REASONS_SINCE}', since);
  const { mixed: m2, ...jr } = P.REASON_LABEL;
  assert.deepEqual(jr, reasons); assert.ok(m2);
  assert.deepEqual(P.REASON_ORDER.filter(k => k !== 'mixed'), Object.keys(reasons), 'the backend order');
  assert.ok(P.REASON_ORDER.every(k => P.REASON_COLOR[k] && P.REASON_TIP[k]), 'a colour and a tip per reason');
});
