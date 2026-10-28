'use strict';
// C19 «PnL за период» (TDM#408): pure helpers of lib/pnl-period.js on a synthetic fixture in the shape of
// docs/architecture/briefs/PNL_PERIOD.md — two books, one instrument in both, a closed position, a market day without
// a run, positions with and without the carry split (carry_coupon / carry_pull), funding null for a swap and a USD bond.
const test = require('node:test'), assert = require('node:assert/strict');
const P = require('../../prototypes/concepts/lib/pnl-period.js');
const FX = require('./fixtures/pnl_period.json');
const near = (a, b, msg) => assert.ok(a != null && b != null && Math.abs(a - b) < 1e-6 * Math.max(1, Math.abs(b)), msg + ': ' + a + ' ≠ ' + b);
const prep = () => P.prepare(FX.period);
const pos = (p, ref) => p.positions.find(x => x.ref === ref);

test('fixture has the contract shape and the cases the screens must handle', () => {
  const d = FX.period, AGG = ['total', 'rows', 'noTotal', 'partialRows', 'grossCheck', 'grossCount', 'c', 'm'];
  for (const k of ['from', 'to', 'gran', 'anchorRunId', 'currency', 'marketDates', 'runs', 'intradayOnlyDates', 'keys', 'curves', 'buckets', 'summary', 'days', 'positions']) assert.ok(k in d, k);
  AGG.forEach(k => assert.ok(k in d.summary, 'summary.' + k));
  d.days.forEach(x => ['date', 'prevDate', 'runId', 'ratesByCurve', ...AGG].forEach(k => assert.ok(k in x, 'day.' + k)));
  d.positions.forEach(x => ['ref', 'book', 'positionId', 'name', 'instrument', 'ccy', 'kind', 'group', 'curve', 'rating', 'issuer', 'firstDate', 'lastDate', 'closed', 'pvAvg', 'calDays', 'fundingRatePct', 'buckets', ...AGG]
    .forEach(k => assert.ok(k in x, 'pos.' + k)));
  d.positions.forEach(x => assert.equal(x.ref, x.book + '|' + x.positionId));
  assert.equal(new Set(d.positions.filter(x => x.instrument === 'SYN-OFZ-1').map(x => x.book)).size, 2);
  assert.ok(d.positions.some(x => x.closed));
  assert.ok(d.marketDates.includes('2026-10-07') && !d.days.some(x => x.date === '2026-10-07'));
  assert.ok(d.positions.some(x => x.c.carry_coupon == null) && d.positions.some(x => x.c.carry_coupon != null));
  assert.ok(d.positions.some(x => x.c.funding == null));
});

test('null-safe sums and coverage: missing stays null, partial says «N из M записей»', () => {
  assert.equal(P.sumAvail([null, undefined, NaN]), null);
  assert.equal(P.sumAvail([1, null, 2]), 3);
  assert.equal(P.sumAvail([0, null]), 0);
  assert.equal(P.sumAll([1, null]), null);
  assert.equal(P.sumAll([1, 2]), 3);
  const a = { rows: 10, c: { x: 5, y: null, z: 1 }, m: { x: 0, y: 10, z: 4 } };
  assert.deepEqual(P.coverage(a, 'x'), { have: 10, rows: 10, full: true, none: false });
  assert.equal(P.coverText(a, 'x'), '');
  assert.equal(P.coverText(a, 'y'), 'нет данных');
  assert.equal(P.coverText(a, 'z'), '6 из 10 записей');
  assert.equal(P.coverText(a, ['x', 'z']), '6 из 10 записей');
  // a /position day has no rows / m: one row, the value decides
  assert.equal(P.coverage({ c: { x: 1 } }, 'x').full, true);
  assert.equal(P.coverage({ c: { x: null } }, 'x').none, true);
  const s = prep().summary;
  assert.equal(P.coverText(s, 'funding'), (s.rows - s.m.funding) + ' из ' + s.rows + ' записей');
  assert.equal(P.coverText(s, 'reported'), 'нет данных');
});

test('summary, days, buckets and positions agree; buckets cover the period, a missing run is not a zero', () => {
  const p = prep(), s = p.summary;
  near(P.sumAvail(p.positions.map(x => x.total)), s.total, 'Σ positions');
  near(P.sumAvail(p.days.map(x => x.total)), s.total, 'Σ days');
  for (const k of FX.period.keys) {
    if (s.c[k] == null) { assert.ok(p.positions.every(x => x.c[k] == null), k); continue; }
    near(P.sumAvail(p.positions.map(x => x.c[k])), s.c[k], 'Σ positions ' + k);
    assert.equal(p.positions.reduce((n, x) => n + x.m[k], 0), s.m[k], 'm ' + k);
  }
  assert.deepEqual(FX.period.buckets, P.makeBuckets(FX.period.from, FX.period.to, FX.period.gran));
  near(P.sumAvail(p.bucketAggs.map(a => a && a.total)), s.total, 'Σ buckets');
  p.buckets.forEach((b, i) => near(P.sumAvail(p.positions.map(x => x.buckets[i])), p.bucketAggs[i].total, 'bucket ' + b.label));
  const merged = P.sumAggs(p.days);
  assert.equal(merged.rows, s.rows); assert.equal(merged.m.funding, s.m.funding); assert.equal(merged.c.reported, null);
  // the day without a run: on the daily axis, null in the running sum, the sum goes on after it
  const i = p.dates.indexOf('2026-10-07'), byDate = new Map(p.days.map(d => [d.date, d]));
  assert.ok(i > 0);
  const cum = P.cumulate(p.dates.map(d => (byDate.has(d) ? byDate.get(d).total : null)));
  assert.equal(cum[i], null); near(cum.at(-1), s.total, 'cumulative');
  assert.equal(P.cumulate([1, null, 2]).join(), '1,,3');
  assert.equal(P.bucketAggs([], p.buckets)[0], null);
  assert.ok(P.bucketHasMarket(p.buckets[1], FX.period.marketDates));
});

test('explain rows: carry net / gross + funding / components, the rows sum to the total, gap hidden', () => {
  const p = prep(), s = p.summary;
  for (const carry of ['net', 'split', 'comp']) {
    const defs = P.rowsFor('explain', p, { carry });
    assert.ok(!defs.some(d => d.k === 'gap'), carry + ': gap hidden when it is within 1000 RUB');
    assert.ok(!defs.some(d => d.k === 'reported'), carry + ': reported hidden without values');
    near(P.sumAvail(defs.map(d => d.total)), s.total, carry + ': Σ rows = total');
    defs.forEach(d => d.buckets.forEach((v, i) => { if (p.bucketAggs[i]) assert.equal(v, d.v(p.bucketAggs[i])); }));
  }
  const net = P.rowsFor('explain', p, { carry: 'net' })[0];
  assert.equal(net.k, 'carryNet'); near(net.total, s.c.carry + s.c.rolldown + s.c.funding, 'carry net');
  const split = P.rowsFor('explain', p, { carry: 'split' });
  assert.deepEqual(split.slice(0, 2).map(d => d.k), ['carryGross', 'funding']);
  // the selection has positions without the split: «Компоненты» keep carry whole, not a partial coupon
  assert.equal(p.split, false);
  assert.deepEqual(P.rowsFor('explain', p, { carry: 'comp' }).slice(0, 3).map(d => d.k), ['carry', 'rolldown', 'funding']);
  assert.deepEqual(P.componentRows('explain', { carry: 'comp', split: true }).slice(0, 4).map(d => d.k), ['carry_coupon', 'carry_pull', 'rolldown', 'funding']);
  // a gap appears when the components do not sum to the total
  const bad = Object.assign({}, s, { total: s.total + 5e4 });
  assert.ok(P.visibleRows(P.componentRows('explain', { carry: 'net', keys: p.keys }), bad).some(d => d.k === 'gap'));
  // one colour and one name per source
  P.COMPONENTS.forEach(c => assert.ok(c.label && c.color && c.d, c.key));
  assert.equal(new Set(P.COMPONENTS.map(c => c.label)).size, P.COMPONENTS.length);
});

test('explain by curves: rates of each position on its discount curve, «кривая не указана» kept', () => {
  const p = prep(), defs = P.rowsFor('curves', p, { carry: 'net' }), rates = defs.filter(d => d.k.startsWith('rates:'));
  assert.deepEqual(rates.map(d => d.curve), ['RUB OFZ', 'RUB RUONIA', '']);
  assert.equal(rates[2].l, 'Ставки · ' + P.NO_CURVE);
  near(P.sumAvail(rates.map(d => d.total)), p.summary.c.rates, 'Σ curves = rates');
  const usd = pos(p, 'DEMO-B|USD-1');
  assert.equal(rates[2].v(usd), usd.c.rates); assert.equal(rates[0].v(usd), null);
  near(P.sumAvail(defs.map(d => d.total)), p.summary.total, 'Σ rows = total');
});

test('attribution: «нет данных» without the coupon split of every row; with it price + coupon + FX + funding + trades = total', () => {
  const p = prep(), attr = P.rowsFor('attr', p, {});
  assert.deepEqual(attr.map(d => d.k), ['price', 'carry_coupon', 'fx', 'funding', 'new_trades']);
  attr.forEach(d => assert.equal(d.total, null, d.k + ' on the selection'));
  const corp = pos(p, 'DEMO-A|CORP-1'), ofz = pos(p, 'DEMO-A|OFZ-1'), usd = pos(p, 'DEMO-B|USD-1');
  attr.forEach(d => assert.equal(d.v(corp), null, d.k + ' without carry_coupon'));
  near(P.sumAvail(attr.map(d => d.v(ofz))), ofz.total, 'OFZ attribution');
  near(attr[1].v(ofz), ofz.c.carry_coupon, 'coupon');
  // funding missing in the row is missing in its total too: the identity holds and funding stays «—»
  near(P.sumAvail(attr.map(d => d.v(usd))), usd.total, 'USD attribution');
  assert.equal(attr[3].v(usd), null);
  near(attr[2].v(usd), usd.c.fx_gross + usd.c.fx_funding, 'FX net');
  // «Валюта» of the attribution is the same source as the explain FX: one name, one colour
  const fx = P.rowsFor('explain', p, { carry: 'net' }).find(d => d.k === 'fx');
  assert.equal(attr[2].l, fx.l); assert.equal(attr[2].color, fx.color);
});

test('carry split rule (A–E): a row without PnL does not blank it; a row with carry but without the split does', () => {
  // Agg level: m[part] − m.carry = 0 — every row with carry has the split
  const a = { rows: 10, total: 9, c: { carry: 5, carry_coupon: 3, carry_pull: 2 }, m: { carry: 1, carry_coupon: 1, carry_pull: 1 } };
  assert.equal(P.hasSplit(a), true); assert.equal(P.hasCoupon(a), true); assert.equal(P.splitCover(a), '');
  const b = { rows: 10, total: 9, c: { carry: 5, carry_coupon: 3, carry_pull: 2 }, m: { carry: 1, carry_coupon: 2, carry_pull: 1 } };
  assert.equal(P.hasSplit(b), false); assert.equal(P.hasCoupon(b), false); assert.equal(P.splitCover(b), '8 из 9 записей');
  assert.equal(P.hasSplit({ rows: 3, c: { carry: null, carry_coupon: null }, m: { carry: 3, carry_coupon: 3 } }), false, 'no carry at all');
  assert.equal(P.splitCover({ rows: 3, c: { carry: 1, carry_coupon: null, carry_pull: null }, m: { carry: 0, carry_coupon: 3, carry_pull: 3 } }), 'нет данных');
  // a selection: the split positions plus a new position whose only day has no PnL (Agg.m counts it as missing everything)
  const split = FX.period.positions.filter(x => P.hasSplit(x)), keys = Object.keys(FX.period.summary.c);
  const fresh = Object.assign(JSON.parse(JSON.stringify(split[0])), { ref: 'DEMO-A|NEW-1', positionId: 'NEW-1', name: 'SYN-NEW-1', total: null, rows: 1, noTotal: 1, partialRows: 0,
    grossCheck: null, grossCount: 0, c: Object.fromEntries(keys.map(k => [k, null])), m: Object.fromEntries(keys.map(k => [k, 1])), buckets: split[0].buckets.map(() => null), pvAvg: null, calDays: null });
  const sel = positions => { const d = JSON.parse(JSON.stringify(FX.period)); d.positions = positions; d.summary = P.sumAggs(positions); return P.prepare(d); };
  const ok = sel(split.concat([fresh])), s = ok.summary;
  assert.ok(s.m.carry_coupon > 0 && s.m.carry_coupon === s.m.carry, 'the no-PnL row is missing carry and coupon alike');
  assert.equal(ok.split, true); assert.equal(ok.coupon, true);
  const attr = P.rowsFor('attr', ok, {});
  attr.forEach(d => assert.ok(d.total != null, d.k + ' shown'));
  near(P.sumAvail(attr.map(d => d.total)), s.total, 'price + coupon + FX + funding + trades = total');
  near(attr[1].total, s.c.carry_coupon, 'coupon');
  assert.equal(P.coverText(s, 'carry'), (s.rows - 1) + ' из ' + s.rows + ' записей', 'the coverage still counts the row without PnL');
  const S = P.carrySummary(ok);
  near(S.coupon, s.c.carry_coupon, 'C coupon tile'); near(S.pull, s.c.carry_pull, 'C pull tile'); assert.equal(S.cover.split, '');
  assert.deepEqual(P.rowsFor('explain', ok, { carry: 'comp' }).slice(0, 2).map(d => d.k), ['carry_coupon', 'carry_pull'], 'B/A components split');
  const rows = ok.positions.map(P.carryPosition);
  near(P.carryTotal(rows).coupon, s.c.carry_coupon, 'C group coupon');
  // the same selection plus one position with carry but no split: «нет данных»
  const corp = FX.period.positions.find(x => x.ref === 'DEMO-A|CORP-1'), bad = sel(split.concat([fresh, corp]));
  assert.equal(bad.split, false); assert.equal(bad.coupon, false);
  P.rowsFor('attr', bad, {}).forEach(d => assert.equal(d.total, null, d.k + ' «нет данных»'));
  const have = P.sumAvail(split.map(x => x.rows));
  assert.equal(P.splitCover(bad.summary), have + ' из ' + (have + corp.rows) + ' записей');
  assert.equal(P.carrySummary(bad).coupon, null);
  assert.equal(P.carryTotal(bad.positions.map(P.carryPosition)).coupon, null);
});

test('groups from positions: type, book, currency, curve, rating, position; buckets by index', () => {
  const p = prep(), L = { kind: x => x.group + ' · ' + x.kind, curve: id => p.curveName(id) };
  const books = P.groupRows(p.positions, 'book', L, p.buckets.length);
  assert.deepEqual(books.map(g => g.key).sort(), ['DEMO-A', 'DEMO-B']);
  books.forEach(g => { near(g.v, P.sumAvail(g.positions.map(x => x.total)), g.key); g.buckets.forEach((v, i) => assert.equal(v, P.sumAvail(g.positions.map(x => x.buckets[i])))); });
  near(P.sumAvail(books.map(g => g.v)), p.summary.total, 'Σ books');
  const curves = P.groupRows(p.positions, 'curve', L, 0);
  assert.ok(curves.some(g => g.key === '' && g.l === P.NO_CURVE));
  assert.ok(curves.some(g => g.key === 'RUB OFZ' && g.l === 'ОФЗ'));
  assert.ok(P.groupRows(p.positions, 'rating', L, 0).some(g => g.l === P.NO_RATING));
  assert.equal(P.groupRows(p.positions, 'type', L, 0).find(g => g.key === 'GOV.PD').n, 2);  // one instrument, two books
  const rows = P.rowsFor('pos', p, { labels: L });
  assert.equal(rows.length, p.positions.length);
  assert.ok(rows.every(r => r.ref && r.color.startsWith('pal:')));
  const closed = rows.find(r => r.ref === 'DEMO-A|CLOSED');
  assert.ok(closed.buckets.at(-1) === null, 'closed before the last bucket: null, not 0');
  assert.equal(P.rowsFor('ccy', p, {}).find(r => r.k === 'g:USD').v(pos(p, 'DEMO-B|USD-1')), pos(p, 'DEMO-B|USD-1').total);
  // long lists: chart folds the tail, the waterfall folds the middle
  const many = Array.from({ length: 15 }, (_, i) => ({ k: 'g:' + i, l: 'g' + i, total: i - 7, buckets: [i, null], v: () => i }));
  const ch = P.chartRows(many);
  assert.equal(ch.length, 8); assert.equal(ch[7].l, 'Остальные 8'); assert.equal(ch[7].buckets[1], null);
  const wf = P.waterfallItems(many);
  assert.equal(wf.length, 12); assert.equal(wf[6].k, 'g:*'); assert.equal(wf[6].keys.length, 4);
  near(P.sumAvail(wf.map(x => x.v)), P.sumAvail(many.map(x => x.total)), 'folded waterfall');
});

test('contributors: who added and who subtracted, positions without a value counted', () => {
  const p = prep(), fund = P.rowsFor('explain', p, { carry: 'split' }).find(d => d.k === 'funding');
  const C = P.contrib(p.positions, fund);
  assert.equal(C.missing, 2);  // the swap and the USD bond have no funding
  assert.ok(C.minus.length && C.minus.every((x, i, a) => x.v < 0 && (!i || a[i - 1].v <= x.v)));
  near(C.total, p.summary.c.funding, 'Σ funding');
  const T = P.contrib(p.positions, { k: 'total', v: x => x.total });
  assert.ok(T.plus.every((x, i, a) => x.v > 0 && (!i || a[i - 1].v >= x.v)));
  assert.equal(T.missing, 0);
});

test('matrix: group cells are the sum of their positions, sorting keeps nulls last', () => {
  const p = prep(), defs = P.visibleRows(P.componentRows('explain', { carry: 'net', keys: p.keys }), p.summary).concat([{ k: 'total', v: a => a.total }]);
  const G = P.sortMatrix(P.matrix(p.positions, 'book', defs, {}), defs.length - 1, -1);
  G.forEach(g => defs.forEach((d, j) => { const kids = P.sumAvail(g.kids.map(k => k.v[j])); if (kids == null) assert.equal(g.v[j], null); else near(g.v[j], kids, g.key + ' ' + d.k); }));
  assert.ok(G[0].v.at(-1) >= G[1].v.at(-1));
  const fj = defs.findIndex(d => d.k === 'carryNet');
  const sorted = P.sortMatrix(P.matrix(p.positions, 'book', defs, {}), fj, -1).find(g => g.key === 'DEMO-B').kids;
  assert.ok(sorted.every((k, i, a) => !i || a[i - 1].v[fj] >= k.v[fj]));
  // cell drill-down on a /position response: running sum and the largest days
  const resp = FX.position['DEMO-A|CORP-1'], D = P.cellDays(resp.days, { k: 'other', v: a => P.val(a, 'other') });
  assert.equal(D.dates.length, resp.days.length);
  assert.equal(D.top[0].date, '2026-10-09');
  near(D.cum.at(-1), pos(p, 'DEMO-A|CORP-1').c.other, 'cell total');
});

test('carry: net = carry without roll-down + roll-down; gross = coupon + pull (or carry whole) + roll-down; % p.a. on pv', () => {
  const p = prep(), s = p.summary;
  const net = P.carryParts('net', false), gross = P.carryParts('gross', false), grossSplit = P.carryParts('gross', true);
  near(net.parts[0].v(s) + net.parts[1].v(s), net.tot.v(s), 'net parts');
  near(net.tot.v(s), s.c.carry + s.c.rolldown + s.c.funding, 'net');
  assert.deepEqual(gross.parts.map(d => d.k), ['carry', 'rolldown']);
  assert.deepEqual(grossSplit.parts.map(d => d.k), ['carry_coupon', 'carry_pull', 'rolldown']);
  const ofz = pos(p, 'DEMO-A|OFZ-1');
  near(grossSplit.parts.reduce((a, d) => a + d.v(ofz), 0), gross.tot.v(ofz), 'coupon + pull + roll = gross');
  const S = P.carrySummary(p);
  assert.equal(S.coupon, null); assert.equal(S.pull, null);  // the selection is not fully split
  assert.match(S.cover.coupon, / из \d+ записей$/);
  near(S.gross, s.c.carry + s.c.rolldown, 'gross'); near(S.net, s.c.carry + s.c.rolldown + s.c.funding, 'net');
  assert.equal(S.days, P.calDays(p.days));
  assert.ok(S.grossPct > 0 && S.marginBp != null);
  // positions: a deal has no %, funding null stays null, the position's own split is shown
  const irs = P.carryPosition(pos(p, 'DEMO-B|IRS-1')), usd = P.carryPosition(pos(p, 'DEMO-B|USD-1')), o = P.carryPosition(ofz);
  assert.equal(irs.cy, null); assert.equal(irs.fr, null); assert.equal(irs.mg, null); assert.equal(irs.funding, null);
  assert.equal(usd.funding, null); assert.equal(usd.fr, null); near(usd.net, usd.gross, 'USD net without funding');
  assert.equal(o.coupon, ofz.c.carry_coupon); assert.equal(o.fr, 16.5);
  // no server rate: «—», never estimated from funding / pv
  const noRate = P.carryPosition(Object.assign({}, ofz, { fundingRatePct: null }));
  assert.equal(noRate.fr, null); assert.equal(noRate.funding, ofz.c.funding); assert.ok(noRate.mg != null);
  near(o.cy, (ofz.c.carry + ofz.c.rolldown) / ofz.pvAvg / (ofz.calDays / 365) * 100, 'carry % p.a.');
  assert.equal(P.carryPosition(pos(p, 'DEMO-A|CORP-1')).coupon, null);
  const T = P.carryTotal([o, usd]);
  near(T.net, o.net + usd.net, 'group net'); assert.equal(P.carryTotal([irs]).funding, null);
  near(T.coupon, o.coupon + usd.coupon, 'group coupon');
  assert.equal(P.carryTotal([o, P.carryPosition(pos(p, 'DEMO-A|CORP-1'))]).coupon, null, 'a row without the split: the group coupon is «—»');
  assert.equal(P.perCalDay({ date: '2026-10-08', prevDate: '2026-10-06' }, 10), 5);
  assert.equal(P.perCalDay({ date: '2026-10-08', prevDate: '2026-10-06' }, null), null);
});

test('tiles of A: total, carry net, rates + spread, FX net, unexplained and its share', () => {
  const p = prep(), s = p.summary, T = P.tilesA(p);
  near(T.total, s.total, 'total'); near(T.carryNet, s.c.carry + s.c.rolldown + s.c.funding, 'carry net');
  near(T.market, s.c.rates + s.c.spread, 'market'); near(T.fx, s.c.fx_gross + s.c.fx_funding, 'fx');
  assert.ok(!('fxGross' in T) && !('fxFunding' in T));
  near(T.other, s.c.other, 'other'); assert.ok(T.otherShare > 0);
  assert.match(T.cover.carry, / из \d+ записей$/);
  const empty = P.tilesA(P.prepare({ from: '2026-10-01', to: '2026-10-01', summary: { total: null, rows: 0, c: {}, m: {} }, days: [], positions: [], buckets: [] }));
  assert.equal(empty.total, null); assert.equal(empty.carryNet, null); assert.equal(empty.otherShare, null);
});

test('presets, auto granularity and the bucket rule', () => {
  const a = '2026-10-13';
  assert.deepEqual(P.presetRange('day', a), [a, a]);
  assert.deepEqual(P.presetRange('week', a), ['2026-10-07', a]);
  assert.deepEqual(P.presetRange('mtd', a), ['2026-10-01', a]);
  assert.deepEqual(P.presetRange('3m', a), ['2026-07-14', a]);
  assert.deepEqual(P.presetRange('qtd', a), ['2026-10-01', a]);
  assert.deepEqual(P.presetRange('qtd', '2026-08-31'), ['2026-07-01', '2026-08-31']);
  assert.deepEqual(P.presetRange('ytd', a), ['2026-01-01', a]);
  assert.equal(P.presetRange('custom', a), null);
  assert.equal(P.PRESETS.map(x => x[1]).join(), 'День,Неделя,MTD,3М,Квартал,YTD,свой');
  assert.equal(P.autoGran('2026-10-01', '2026-10-31'), 'day');      // 22 weekdays
  assert.equal(P.autoGran('2026-09-01', '2026-10-13'), 'week');     // 31
  assert.equal(P.autoGran('2026-07-14', '2026-10-13'), 'week');     // 3М: 66
  assert.equal(P.autoGran('2026-01-01', '2026-10-13'), 'month');    // YTD
  const w = P.makeBuckets('2026-09-30', '2026-10-13', 'week');
  assert.deepEqual(w.map(b => [b.from, b.to]), [['2026-09-30', '2026-10-04'], ['2026-10-05', '2026-10-11'], ['2026-10-12', '2026-10-13']]);
  assert.equal(w[0].key, '2026-W40'); assert.equal(w[0].label, '28.09–04.10.2026');  // label: the whole ISO week
  assert.equal(P.isoWeek('2026-10-26'), '2026-W44'); assert.equal(P.isoWeek('2026-12-28'), '2026-W53'); assert.equal(P.isoWeek('2025-12-29'), '2026-W01');
  const m = P.makeBuckets('2026-01-15', '2026-03-02', 'month');
  assert.deepEqual(m.map(b => [b.key, b.from, b.to, b.label]), [['2026-01', '2026-01-15', '2026-01-31', 'янв 2026'], ['2026-02', '2026-02-01', '2026-02-28', 'фев 2026'], ['2026-03', '2026-03-01', '2026-03-02', 'мар 2026']]);
  assert.deepEqual(P.makeBuckets('2026-10-10', '2026-10-13', 'day').map(b => b.key + ' ' + b.label), ['2026-10-10 10.10.2026', '2026-10-11 11.10.2026', '2026-10-12 12.10.2026', '2026-10-13 13.10.2026']);
  assert.equal(P.dayDiff('2026-10-06', '2026-10-08'), 2);
});

test('FX only net (owner, 28.10.2026): one source «FX (нетто ОВП)» = fx_gross + fx_funding everywhere', () => {
  const p = prep(), s = p.summary;
  // no separate asset / liability FX anywhere: explain, by curves, attribution, components, carry
  for (const mode of ['explain', 'curves', 'attr']) for (const carry of ['net', 'split', 'comp']) {
    const keys = P.componentRows(mode, { carry, keys: p.keys, curves: p.curves }).map(d => d.k);
    assert.ok(!keys.includes('fx_gross') && !keys.includes('fx_funding'), mode + ' ' + carry + ': ' + keys);
    assert.equal(keys.filter(k => k === 'fx').length, 1, mode + ' ' + carry);
  }
  assert.ok(!P.COMPONENTS.some(c => c.key === 'fx_gross' || c.key === 'fx_funding'));
  assert.deepEqual(P.BY_KEY.fx.parts, ['fx_gross', 'fx_funding']);
  assert.equal(P.BY_KEY.fx.label, 'FX (нетто ОВП)');
  const fx = P.rowsFor('explain', p, { carry: 'net' }).find(d => d.k === 'fx');
  near(fx.total, s.c.fx_gross + s.c.fx_funding, 'summary');
  fx.buckets.forEach((v, i) => near(v, p.bucketAggs[i].c.fx_gross + p.bucketAggs[i].c.fx_funding, 'bucket ' + i));
  // null only when both parts are null; one part present — that part, and the row counts as partial
  assert.equal(P.srcVal({ c: { fx_gross: null, fx_funding: null } }, 'fx'), null);
  assert.equal(P.srcVal({ c: { fx_gross: null, fx_funding: -3 } }, 'fx'), -3);
  assert.equal(P.srcVal({ c: { fx_gross: 5, fx_funding: -3 } }, 'fx'), 2);
  const a = { rows: 4, c: { fx_gross: 5, fx_funding: -3 }, m: { fx_gross: 0, fx_funding: 1 } };
  assert.equal(fx.v(a), 2);
  assert.equal(P.coverText(a, fx.parts), '3 из 4 записей');
  assert.equal(P.tilesA(P.prepare({ from: 'x', to: 'x', summary: a, days: [], positions: [], buckets: [] })).cover.fx, '3 из 4 записей');
  // the matrix and the contributors read the same net
  const usd = pos(p, 'DEMO-B|USD-1');
  near(fx.v(usd), usd.c.fx_gross + usd.c.fx_funding, 'position');
  near(P.contrib(p.positions, fx).total, s.c.fx_gross + s.c.fx_funding, 'contributors');
});
