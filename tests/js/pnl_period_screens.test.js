'use strict';
// C19 «PnL за период», screens D, E, F (TDM#409): pure helpers of lib/pnl-period.js on the synthetic answers of
// /curves, /position and /checks in tests/js/fixtures/pnl_period.json (shapes of TDM#407, PNL_PERIOD.md «Уточнения
// реализации»): signed bp, curve grids, linear against exact, findings by position and day, the calendar of runs, the
// heat map capped at the threshold, the day rows of E. Missing stays null, never 0.
const test = require('node:test'), assert = require('node:assert/strict');
const P = require('../../prototypes/concepts/lib/pnl-period.js');
const FX = require('./fixtures/pnl_period.json');
const clone = o => JSON.parse(JSON.stringify(o));

test('fixture answers have the API shapes the screens read', () => {
  for (const k of ['from', 'to', 'firstRun', 'lastRun', 'buckets', 'curves', 'move', 'dv01', 'linear', 'linearTotal', 'exact', 'coverage']) assert.ok(k in FX.curves, 'curves.' + k);
  for (const k of ['from', 'to', 'anchorRunId', 'thrAbs', 'thrPct', 'checks', 'findings', 'coverage', 'unexplained']) assert.ok(k in FX.checks, 'checks.' + k);
  FX.checks.findings.forEach(x => ['sev', 'check', 'date', 'dateTo', 'ref', 'book', 'positionId', 'name', 'value', 'text'].forEach(k => assert.ok(k in x, 'finding.' + k)));
  for (const [ref, r] of Object.entries(FX.position)) {
    assert.equal(r.position.ref, ref);
    r.days.forEach(d => ['date', 'prevDate', 'runId', 'method', 'noPnl', 'total', 'c', 'ratesLinear', 'grossCheck', 'inputs', 'events'].forEach(k => assert.ok(k in d, ref + ' day.' + k)));
  }
  // the cases: linear by term not saved yet (TDM#406), an FX finding without a position, a market day without a run
  assert.equal(FX.curves.coverage.bucketRows, 0);
  assert.ok(FX.checks.findings.some(x => x.check === 'fxrev' && x.ref === null && x.name === 'USD'));
  assert.deepEqual(FX.checks.coverage.missingRuns, ['2026-10-07']);
});

test('signed bp: a move is «+12» / «−15», not an accounting bracket; missing — «—»', () => {
  assert.equal(P.signedBp(12), '+12');
  assert.equal(P.signedBp(-15), '−15');
  assert.equal(P.signedBp(0), '0');
  assert.equal(P.signedBp(-0.04), '0');
  assert.equal(P.signedBp(3.25), '+3.3');  // under 10 bp one decimal
  assert.equal(P.signedBp(-1234.4), '−1,234');
  assert.equal(P.signedBp(-7.44, 1), '−7.4');
  assert.equal(P.signedBp(null), '—');
  assert.equal(P.signedBp(NaN), '—');
});

test('D: curve grids — move, DV01 and linear by term; linear not saved is «нет данных», not zeros', () => {
  const G = P.curveGrids(FX.curves);
  assert.deepEqual(G.buckets, FX.curves.buckets);
  assert.deepEqual(G.move.rows.map(r => r.id), ['RUB OFZ', 'RUB RUONIA']);
  assert.deepEqual(G.move.rows[0].vals, FX.curves.buckets.map(b => FX.curves.move['RUB OFZ'][b]));
  assert.equal(G.move.rows[0].sum, null, 'a sum of moves means nothing');
  assert.equal(G.dv01.rows[0].sum, FX.curves.buckets.reduce((s, b) => s + FX.curves.dv01['RUB OFZ'][b], 0));
  assert.equal(G.move.max, 20);
  assert.equal(G.linear.rows.length, 0);
  assert.equal(G.linearCover, 'нет данных');
  // partial: two buckets of one curve saved by some rows
  const cv = clone(FX.curves);
  cv.linear['RUB OFZ']['1Y'] = 1.5e5; cv.linear['RUB OFZ']['5Y'] = -2.5e5; cv.coverage.bucketRows = 12;
  const G2 = P.curveGrids(cv);
  assert.deepEqual(G2.linear.rows.map(r => r.id), ['RUB OFZ']);
  assert.equal(G2.linear.rows[0].sum, -1e5);
  assert.equal(G2.linear.rows[0].vals.filter(v => v == null).length, cv.buckets.length - 2);
  assert.equal(G2.linearCover, '12 из ' + cv.coverage.rows + ' записей');
  cv.coverage.bucketRows = cv.coverage.rows;
  assert.equal(P.curveGrids(cv).linearCover, '');
  assert.equal(P.curveGrids({ buckets: [], curves: [], coverage: { rows: 0, linearRows: 0, bucketRows: 0 } }).linearCover, 'нет записей');
  // a curve with no point in a grid is not a row of it
  const cv3 = clone(FX.curves); FX.curves.buckets.forEach(b => { cv3.move['RUB RUONIA'][b] = null; });
  assert.deepEqual(P.curveGrids(cv3).move.rows.map(r => r.id), ['RUB OFZ']);
});

test('D: diverging tint by sign and size; good = −1 reads a fall of rates / a long DV01 as green', () => {
  const rgb = [[0, 128, 0], [200, 0, 0]];
  assert.match(P.tint(5, 10, 1, rgb), /^background:rgba\(0,128,0,/);
  assert.match(P.tint(-5, 10, 1, rgb), /^background:rgba\(200,0,0,/);
  assert.match(P.tint(-5, 10, -1, rgb), /^background:rgba\(0,128,0,/);
  assert.equal(P.tint(0, 10, 1, rgb), '');
  assert.equal(P.tint(null, 10, 1, rgb), '');
  const a = +P.tint(10, 10, 1, rgb).match(/,([\d.]+)\)$/)[1], b = +P.tint(100, 10, 1, rgb).match(/,([\d.]+)\)$/)[1];
  assert.equal(a, b, 'capped at the max');
});

test('D: linear against exact by curve; a warning only over 3 % and 1 mn RUB with full coverage', () => {
  const rows = P.linearVsExact(FX.curves, id => id || 'кривая не указана');
  assert.deepEqual(rows.map(r => r.id).slice(-1), [''], '«кривая не указана» last');
  const ofz = rows.find(r => r.id === 'RUB OFZ');
  assert.equal(ofz.diff, FX.curves.exact['RUB OFZ'] - FX.curves.linearTotal['RUB OFZ']);
  assert.ok(Math.abs(ofz.pct - ofz.diff / Math.abs(ofz.linear) * 100) < 1e-9);
  assert.ok(rows.every(r => !r.warn), 'the fixture is within 3 % or 1 mn');
  const cv = clone(FX.curves);
  cv.exact['RUB OFZ'] = cv.linearTotal['RUB OFZ'] - 2e6;  // −2 mn, > 3 % of ~4.6 mn
  assert.equal(P.linearVsExact(cv).find(r => r.id === 'RUB OFZ').warn, true);
  cv.exact['RUB OFZ'] = cv.linearTotal['RUB OFZ'] * 1.02 - 9e5;  // > 3 % but under 1 mn
  assert.equal(P.linearVsExact(cv).find(r => r.id === 'RUB OFZ').warn, Math.abs(cv.exact['RUB OFZ'] - cv.linearTotal['RUB OFZ']) > 1e6);
  cv.exact['RUB OFZ'] = cv.linearTotal['RUB OFZ'] - 2e6; cv.coverage.linearRows = cv.coverage.rows - 1;
  assert.equal(P.linearVsExact(cv).find(r => r.id === 'RUB OFZ').warn, false, 'rows without the estimate: no warning');
  cv.linearTotal['RUB OFZ'] = null;
  const r = P.linearVsExact(cv).find(x => x.id === 'RUB OFZ');
  assert.equal(r.linear, null); assert.equal(r.diff, null); assert.equal(r.pct, null);
});

test('D: cumulative rates by curve from the days; positions by curve with their share', () => {
  const prep = P.prepare(FX.period), S = P.curveSeries(prep);
  assert.deepEqual(S.map(s => s.id).sort(), ['', 'RUB OFZ', 'RUB RUONIA']);
  const ofz = S.find(s => s.id === 'RUB OFZ');
  assert.equal(ofz.vals.length, prep.dates.length);
  assert.equal(ofz.vals[prep.dates.indexOf('2026-10-07')], null, 'a market day without a run is a gap');
  assert.ok(Math.abs(ofz.total - FX.curves.exact['RUB OFZ']) < 1);
  const pos = P.curvePositions(prep);
  assert.equal(pos.length, prep.positions.filter(p => p.c.rates != null).length);
  assert.equal(pos.at(-1).curve, '', 'no curve last');
  const share = pos.filter(x => x.curve === 'RUB OFZ').reduce((s, x) => s + x.share, 0);
  assert.ok(Math.abs(Math.abs(share) - 100) < 1e-6, 'shares of a curve add up to ±100 %');
});

test('E/F: findings by position and day; FX findings by currency; ▲ only above «инфо»', () => {
  const I = P.findingsIndex(FX.checks.findings);
  assert.ok(I.flagged.has('DEMO-A|CORP-1') && I.flagged.has('DEMO-B|USD-1'));
  assert.ok(!I.flagged.has('DEMO-A|CLOSED'), 'life only — «инфо»');
  assert.equal(I.on('DEMO-B|USD-1', 'USD', '2026-10-09').map(x => x.check).sort().join(), 'fxrev,recon');
  assert.equal(I.on('DEMO-B|USD-1', 'USD', '2026-10-08').map(x => x.check).join(), 'fxrev', 'the jump covers date…dateTo');
  assert.equal(I.on('DEMO-A|OFZ-1', 'RUB', '2026-10-08').length, 0);
  assert.equal(I.on('DEMO-A|CLOSED', 'RUB', '2026-10-05').map(x => x.check).join(), 'life');
  assert.equal(I.of('DEMO-A|CORP-1', 'RUB').length, 3);
  assert.equal(P.worstSev(I.of('DEMO-B|USD-1', 'USD')), 'crit');
  assert.equal(P.worstSev([]), null);
  assert.ok(P.covers({ date: '2026-10-05', dateTo: '2026-10-08' }, '2026-10-06'));
  assert.ok(!P.covers({ date: '2026-10-05', dateTo: null }, '2026-10-06'));
});

test('E: the list by book, the largest first; other books of the instrument; thresholds into the /checks query', () => {
  const prep = P.prepare(FX.period), flagged = P.findingsIndex(FX.checks.findings).flagged, L = P.positionList(prep.positions, flagged);
  assert.deepEqual(L.map(g => g.book), ['DEMO-A', 'DEMO-B']);
  L.forEach(g => g.items.forEach((x, i, a) => { if (i && x.total != null) assert.ok(Math.abs(a[i - 1].total) >= Math.abs(x.total)); }));
  assert.equal(L.flatMap(g => g.items).find(x => x.ref === 'DEMO-A|CORP-1').flag, true);
  assert.equal(L.flatMap(g => g.items).find(x => x.ref === 'DEMO-A|OFZ-1').flag, false);
  assert.deepEqual(P.otherBooks(prep.positions, prep.positions.find(p => p.ref === 'DEMO-A|OFZ-1')), ['DEMO-B']);
  assert.deepEqual(P.otherBooks(prep.positions, prep.positions.find(p => p.ref === 'DEMO-A|CORP-1')), []);
  assert.deepEqual(P.thrQuery({}), { thr_abs: '3000000', thr_pct: '0.25' });
  assert.deepEqual(P.thrQuery({ thrAbs: 1.5, thrPct: 10 }), { thr_abs: '1500000', thr_pct: '0.1' });
});

test('E: day rows — inputs D−1 / D, Δ yield and Δ spread in bp, the interval; columns with values; FX net only', () => {
  const days = FX.position['DEMO-A|OFZ-1'].days, rows = days.map(P.dayRow), r = rows[3], d = days[3];
  assert.equal(r.src, 'previous_row');
  assert.ok(Math.abs(r.dy - (d.inputs.ytm - d.inputs.ytmPrev) * 100) < 1e-9);
  assert.ok(Math.abs(r.dz - (d.inputs.z - d.inputs.zPrev)) < 1e-9);
  assert.equal(r.cal, P.dayDiff(d.prevDate, d.date));
  assert.equal(rows[0].dy, null, 'no D−1 on the first day: «—»');
  assert.equal(rows[0].src, null);
  assert.equal(rows.find(x => x.date === '2026-10-08').cal, 2, 'the interval spans the day without a run');
  assert.ok(FX.position['DEMO-A|OFZ-1'].days.some(x => x.inputs.source === 'prev'));
  // a quantity change: D−1 fields hidden, quantityPrev kept
  const q = FX.position['DEMO-A|CORP-1'].days.map(P.dayRow).find(x => x.date === '2026-10-08');
  assert.equal(q.cleanPrev, null); assert.equal(q.dy, null); assert.ok(q.quantityPrev !== q.quantity);
  assert.ok(q.events.includes('количество изменилось'));
  // a swap: no clean price, no yield
  assert.ok(FX.position['DEMO-B|IRS-1'].days.map(P.dayRow).every(x => x.clean == null && x.dy == null));
  // columns: carry split when every day has it, FX as one net column, unexplained always; none of the parts
  const cols = P.dayColumns(days);
  assert.deepEqual(cols.slice(0, 2), ['carry_coupon', 'carry_pull']);
  assert.ok(!cols.includes('fx_gross') && !cols.includes('fx_funding') && cols.includes('other'));
  const usd = P.dayColumns(FX.position['DEMO-B|USD-1'].days);
  assert.ok(usd.includes('fx'));
  assert.ok(!P.dayColumns(FX.position['DEMO-A|CORP-1'].days).includes('carry_coupon'), 'no split: carry whole');
  const one = clone(days.slice(0, 2)); one[0].c.fx_gross = 5000; one[0].c.fx_funding = -4800;
  assert.equal(P.srcVal(one[0], 'fx'), 200);
});

test('F: calendar — no run, findings above «инфо», ok; the month boundary', () => {
  const C = P.calendar(FX.checks);
  assert.equal(C.length, FX.checks.coverage.marketDates.length);
  assert.equal(C.find(x => x.date === '2026-10-07').state, 'norun');
  assert.equal(C.find(x => x.date === '2026-10-09').state, 'issue');
  assert.equal(C.find(x => x.date === '2026-10-08').state, 'issue', 'the FX jump covers its return day');
  assert.equal(C.find(x => x.date === '2026-10-02').state, 'ok', 'a «инфо» finding does not mark the day');
  assert.equal(C.find(x => x.date === '2026-10-01').month, true);
  assert.equal(C.filter(x => x.month).length, 1);
  assert.deepEqual(P.calendar({}), []);
});

test('F: heat map — cells by position and day, the scale capped at the threshold, the largest on top', () => {
  const Hm = P.heatmap(FX.checks.unexplained, FX.checks.thrAbs), U = FX.checks.unexplained;
  assert.equal(Hm.data.length, U.cells.length);
  assert.equal(Hm.refs.at(-1), 'DEMO-A|CORP-1', 'the largest |other| on top');
  const max = Math.max(...U.cells.map(c => Math.abs(c[2])));
  assert.equal(Hm.max, max);
  assert.equal(Hm.cap, Math.max(1e5, Math.min(max, FX.checks.thrAbs)));
  assert.equal(P.heatmap(U, 1e6).cap, 1e6, 'capped at the threshold');
  assert.equal(P.heatmap(U, 10).cap, 1e5, 'at least 0.1 mn');
  // every cell keeps its value and lands on its position's row
  Hm.data.forEach(([x, y, v]) => { const src = U.cells.find(c => c[0] === x && U.refs[c[1]] === Hm.refs[y]); assert.ok(src && src[2] === v); });
  assert.deepEqual(P.heatmap(null, 3e6), { dates: [], refs: [], data: [], max: 0, cap: 1e5 });
});

test('F: check cards — status, findings by severity; value kinds of the API', () => {
  const cards = P.checkCards(FX.checks), by = Object.fromEntries(cards.map(c => [c.key, c]));
  assert.equal(cards.length, 9);
  assert.deepEqual(by.recon.bySev, [['crit', 1]]); assert.equal(by.recon.worst, 'crit');
  assert.deepEqual(by.life.bySev, [['info', 2]]);
  assert.equal(by.stale.status, 'no_data'); assert.equal(by.stale.n, 0);
  assert.equal(by.lin.status, 'ok');
  const all = cards.reduce((s, c) => s + c.n, 0);
  assert.equal(all, FX.checks.findings.length);
  assert.equal(P.VALUE_KIND.fxrev, 'share'); assert.equal(P.VALUE_KIND.norun, undefined);
});
