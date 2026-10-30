'use strict';
// C19 «PnL за период» (TDM#408): pure helpers of lib/pnl-period.js on a synthetic fixture in the shape of
// docs/architecture/briefs/PNL_PERIOD.md — two books, one instrument in both, a closed position, a market day without
// a run, positions with and without the carry split (carry_coupon / carry_pull; carry_nosplit for the others), funding
// null for a swap and a USD bond, linear rates of bonds, the USD one too (rates_linear / rates_exact_lin), a new bond without PnL.
// The /period answer is derived from the /position days of the same fixture.
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
  // the split per row (TDM#432): positions with it give coupon and pull, the others keep their carry whole, Σ = carry
  assert.equal(p.split, true); assert.equal(P.hasSplit(s), false);
  const compRows = P.rowsFor('explain', p, { carry: 'comp' });
  assert.deepEqual(compRows.slice(0, 5).map(d => d.k), ['carry_coupon', 'carry_pull', 'carry_nosplit', 'rolldown', 'funding']);
  near(P.sumAvail(compRows.slice(0, 3).map(d => d.total)), s.c.carry, 'coupon + pull + without the split = carry');
  assert.match(compRows[0].cover(s), /^\d+ из \d+ записей$/);
  compRows.slice(0, 3).forEach(d => d.buckets.forEach((v, i) => assert.ok(v != null, d.k + ' bucket ' + i)));
  assert.deepEqual(P.componentRows('explain', { carry: 'comp', split: false }).slice(0, 3).map(d => d.k), ['carry', 'rolldown', 'funding']);
  // flash convexity is 0 by design: hidden while it is 0, shown when a method saves one
  assert.ok(!P.rowsFor('explain', p, { carry: 'net' }).some(d => d.k === 'convexity'));
  const cvx = Object.assign({}, s, { c: Object.assign({}, s.c, { convexity: 5e4 }) });
  assert.ok(P.visibleRows(P.componentRows('explain', { carry: 'net', keys: p.keys }), cvx).some(d => d.k === 'convexity'));
  // a gap appears when the components do not sum to the total
  const bad = Object.assign({}, s, { total: s.total + 5e4 });
  assert.ok(P.visibleRows(P.componentRows('explain', { carry: 'net', keys: p.keys }), bad).some(d => d.k === 'gap'));
  // one colour and one name per source
  P.COMPONENTS.forEach(c => assert.ok(c.label && c.color && c.d, c.key));
  assert.equal(new Set(P.COMPONENTS.map(c => c.label)).size, P.COMPONENTS.length);
});

test('explain by curves: by factor when the server sends ratesByFactor; Σ factors = rates, buckets and drill from it', () => {
  const p = prep(), defs = P.rowsFor('curves', p, { carry: 'net' }), rates = defs.filter(d => d.k.startsWith('rates:'));
  assert.deepEqual(rates.map(d => d.k), ['rates:f:ruonia', 'rates:f:basis', 'rates:f:kbd', 'rates:f:']);
  assert.equal(rates[3].l, 'Ставки · ' + P.NO_CURVE);
  near(P.sumAvail(rates.map(d => d.total)), p.summary.c.rates, 'Σ factors = rates');
  p.bucketAggs.forEach((a, i) => near(P.sumAvail(rates.map(d => d.buckets[i])), a.c.rates, 'bucket ' + i));
  assert.equal(rates[2].buckets.filter(v => v != null).length, 1, 'KBD only on one date: the other buckets «—»');
  const ofz = pos(p, 'DEMO-A|OFZ-1');
  assert.equal(rates[1].v(ofz), ofz.ratesByFactor.basis);
  near(P.contrib(p.positions, rates[0]).total, rates[0].total, 'drill = bar');
  near(P.sumAvail(defs.map(d => d.total)), p.summary.total, 'Σ rows = total');
  // the bar breakdown (TDM#431): shares of the groups from their positions' ratesByFactor add up to the bar
  const items = P.waterfallItems(defs, p.summary), S = P.stackItems(items, defs, p.positions, 'book');
  S.items.filter(it => it.k.startsWith('rates:f:')).forEach(it => near(P.sumAvail(it.pieces.map(x => x.v)), it.v, it.k));
  assert.ok(!S.items.some(it => it.pieces.some(x => x.g === '')), 'nothing left unassigned');
});

test('explain by curves without factors (an older server): rates of each position on its discount curve, «кривая не указана» kept', () => {
  const raw = JSON.parse(JSON.stringify(FX.period));
  raw.days.concat(raw.positions).forEach(a => { delete a.ratesByFactor; });
  const p = P.prepare(raw), defs = P.rowsFor('curves', p, { carry: 'net' }), rates = defs.filter(d => d.k.startsWith('rates:'));
  assert.deepEqual(rates.map(d => d.curve), ['RUB OFZ', 'RUB RUONIA', '']);
  assert.equal(rates[2].l, 'Ставки · ' + P.NO_CURVE);
  near(P.sumAvail(rates.map(d => d.total)), p.summary.c.rates, 'Σ curves = rates');
  const usd = pos(p, 'DEMO-B|USD-1');
  assert.equal(rates[2].v(usd), usd.c.rates); assert.equal(rates[0].v(usd), null);
  near(P.sumAvail(defs.map(d => d.total)), p.summary.total, 'Σ rows = total');
});

test('attribution per row (TDM#432): price + coupon + carry without the split + FX + funding + trades = total', () => {
  const p = prep(), s = p.summary, attr = P.rowsFor('attr', p, {}), by = k => attr.find(d => d.k === k);
  assert.deepEqual(attr.map(d => d.k), ['price', 'carry_coupon', 'carry_nosplit', 'fx', 'funding', 'new_trades']);
  // rows without the coupon split no longer blank the selection
  attr.forEach(d => assert.ok(d.total != null, d.k + ' on the selection'));
  near(P.sumAvail(attr.map(d => d.total)), s.total, 'identity on the selection');
  near(by('carry_coupon').total, s.c.carry_coupon, 'coupon of the split rows'); near(by('carry_nosplit').total, s.c.carry_nosplit, 'carry of the others');
  assert.match(by('carry_coupon').cover(s), /^\d+ из \d+ записей$/);
  const corp = pos(p, 'DEMO-A|CORP-1'), ofz = pos(p, 'DEMO-A|OFZ-1'), usd = pos(p, 'DEMO-B|USD-1');
  // a position without the split: no coupon, its carry whole apart, so the price holds no coupon
  assert.equal(by('carry_coupon').v(corp), null); near(by('carry_nosplit').v(corp), corp.c.carry, 'CORP carry apart');
  near(by('price').v(corp), corp.total - corp.c.carry - corp.c.funding - corp.c.fx_gross - corp.c.fx_funding - corp.c.new_trades, 'CORP price');
  near(P.sumAvail(attr.map(d => d.v(corp))), corp.total, 'CORP attribution');
  // a split position: nothing apart — 0, not «—»
  assert.equal(by('carry_nosplit').v(ofz), 0);
  near(P.sumAvail(attr.map(d => d.v(ofz))), ofz.total, 'OFZ attribution');
  near(by('carry_coupon').v(ofz), ofz.c.carry_coupon, 'coupon');
  // funding missing in the row is missing in its total too: the identity holds and funding stays «—»
  near(P.sumAvail(attr.map(d => d.v(usd))), usd.total, 'USD attribution');
  assert.equal(by('funding').v(usd), null);
  near(by('fx').v(usd), usd.c.fx_gross + usd.c.fx_funding, 'FX net');
  // an answer without carry_nosplit: unknown for a mixed split («нет данных»), the whole carry when nothing is split
  const old = JSON.parse(JSON.stringify(s)); delete old.c.carry_nosplit;
  attr.forEach(d => assert.equal(d.v(old), null, d.k + ' mixed, no carry_nosplit'));
  const oldCorp = JSON.parse(JSON.stringify(corp)); delete oldCorp.c.carry_nosplit;
  near(by('carry_nosplit').v(oldCorp), corp.c.carry, 'nothing split, no carry_nosplit');
  // «Валюта» of the attribution is the same source as the explain FX: one name, one colour
  const fx = P.rowsFor('explain', p, { carry: 'net' }).find(d => d.k === 'fx');
  assert.equal(by('fx').l, fx.l); assert.equal(by('fx').color, fx.color);
});

test('rates of bonds (TDM#435, TDM#418): linear + convexity for the rows with rates_linear, the rest whole; Σ = rates', () => {
  const p = prep(), s = p.summary, defs = P.rowsFor('explain', p, { carry: 'net' }), by = k => defs.find(d => d.k === k);
  assert.deepEqual(defs.filter(d => d.parts.includes('rates') || d.parts.includes('rates_linear')).map(d => d.k), ['ratesLin', 'ratesCvx', 'ratesRest']);
  assert.ok(!defs.some(d => d.k === 'rates'));
  near(P.sumAvail(['ratesLin', 'ratesCvx', 'ratesRest'].map(k => by(k).total)), s.c.rates, 'Σ = rates');
  near(by('ratesCvx').total, s.c.rates_exact_lin - s.c.rates_linear, 'convexity = exact − linear');
  // the swap keeps its rates whole; the USD bond splits as the RUB ones (its linear rates at the D rate, TDM#418)
  const usd = pos(p, 'DEMO-B|USD-1'), irs = pos(p, 'DEMO-B|IRS-1'), ofz = pos(p, 'DEMO-A|OFZ-1');
  near(by('ratesRest').total, irs.c.rates, 'rest = swap');
  near(by('ratesLin').v(usd), usd.c.rates_linear, 'USD linear'); near(by('ratesLin').v(usd) + by('ratesCvx').v(usd), usd.c.rates, 'USD');
  assert.equal(by('ratesRest').v(usd), 0);
  near(by('ratesLin').v(ofz) + by('ratesCvx').v(ofz), ofz.c.rates, 'OFZ'); assert.equal(by('ratesRest').v(ofz), 0);
  assert.match(by('ratesLin').cover(s), /^\d+ из \d+ записей$/);
  near(P.sumAvail(defs.map(d => d.total)), s.total, 'Σ rows = total');
  // a selection where every row with rates has the linear estimate: no «без линейной оценки» row
  const d = JSON.parse(JSON.stringify(FX.period)); d.positions = d.positions.filter(x => x.c.rates_linear != null); d.summary = P.sumAggs(d.positions);
  assert.ok(!P.rowsFor('explain', P.prepare(d), { carry: 'net' }).some(x => x.k === 'ratesRest'));
  // no rates_linear in the answer: one «Ставки»
  const none = JSON.parse(JSON.stringify(FX.period)); none.keys = none.keys.filter(k => !k.startsWith('rates_'));
  assert.ok(P.rowsFor('explain', P.prepare(none), { carry: 'net' }).some(x => x.k === 'rates'));
  const T = P.tilesA(p);
  near(T.ratesLin, s.c.rates_linear, 'tile linear'); near(T.ratesCvx, s.c.rates_exact_lin - s.c.rates_linear, 'tile convexity');
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
  // the same selection plus one position with carry but no split: the others keep theirs, its carry apart (TDM#432)
  const corp = FX.period.positions.find(x => x.ref === 'DEMO-A|CORP-1'), bad = sel(split.concat([fresh, corp])), bs = bad.summary;
  assert.equal(bad.split, true); assert.equal(P.hasSplit(bs), false);
  const attrB = P.rowsFor('attr', bad, {});
  attrB.forEach(d => assert.ok(d.total != null, d.k + ' shown'));
  near(P.sumAvail(attrB.map(d => d.total)), bs.total, 'identity'); near(attrB.find(d => d.k === 'carry_nosplit').total, corp.c.carry, 'its carry apart');
  const have = P.sumAvail(split.map(x => x.rows));
  assert.equal(P.splitCover(bs), have + ' из ' + (have + corp.rows) + ' записей');
  const SB = P.carrySummary(bad);
  near(SB.coupon, bs.c.carry_coupon, 'C coupon of the split rows'); near(SB.nosplit, corp.c.carry, 'C carry without the split');
  const TB = P.carryTotal(bad.positions.map(P.carryPosition));
  near(TB.coupon + TB.pull + TB.nosplit, bs.c.carry, 'C group: coupon + pull + without the split = carry');
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

test('contributors: by |contribution|, small and zero ones counted, positions without a value last with reasons', () => {
  const p = prep(), fund = P.rowsFor('explain', p, { carry: 'split' }).find(d => d.k === 'funding');
  const C = P.contrib(p.positions, fund);
  assert.equal(C.missing, 3);  // the swap and the USD bond have no funding, the new bond no PnL
  assert.deepEqual(C.why, [{ text: 'метод без этой компоненты: desk-flash-yield-v1', n: 1 }, { text: 'метод без этой компоненты: flash-v1', n: 1 }, { text: 'нет PnL: no_prev_report', n: 1 }]);
  assert.ok(C.minus.length && C.minus.every((x, i, a) => x.v < 0 && (!i || a[i - 1].v <= x.v)));
  assert.ok(C.list.every((x, i, a) => !i || Math.abs(a[i - 1].v) >= Math.abs(x.v)));
  near(C.total, p.summary.c.funding, 'Σ funding');
  const T = P.contrib(p.positions, { k: 'total', v: x => x.total });
  assert.ok(T.plus.every((x, i, a) => x.v > 0 && (!i || a[i - 1].v >= x.v)));
  assert.equal(T.missing, 1); assert.ok(!T.list.some(x => x.v == null));
  // zeros are not contributions; small ones (≤ MIN_CONTRIB) are counted and summed; a long list keeps the rest summed
  const fake = [1e6, -2e6, 0, 0, 3e3, -1e3, null].map((v, i) => ({ ref: 'X|' + i, total: v }));
  const F = P.contrib(fake, { k: 'total', v: x => x.total }, 1);
  assert.deepEqual(F.list.map(x => x.v), [-2e6]); assert.deepEqual(F.more, { n: 1, sum: 1e6 });
  assert.deepEqual(F.small, { n: 2, sum: 2e3 }); assert.equal(F.zero, 2); assert.equal(F.missing, 1);
  assert.ok(!F.plus.concat(F.minus).some(x => x.v === 0));
  assert.deepEqual(F.why, [{ text: 'нет итога', n: 1 }]);
});

test('waterfall of groups: nulls last as one «не посчитано» (TDM#433); bars split by groups sum to the bar (TDM#431)', () => {
  const p = prep(), L = { kind: x => x.group + ' · ' + x.kind, curve: id => p.curveName(id) };
  const wf = P.waterfallItems(P.rowsFor('pos', p, { labels: L }));
  assert.equal(wf.at(-1).k, 'g:null'); assert.equal(wf.at(-1).l, 'не посчитано: 1 поз.'); assert.deepEqual(wf.at(-1).keys, ['g:DEMO-B|NEW-1']);
  assert.ok(wf.slice(0, -1).every((x, i, a) => x.v != null && (!i || a[i - 1].v >= x.v)));
  assert.ok(P.groupRows(p.positions, 'pos', L, 0).at(-1).v == null, 'groups: nulls last');
  for (const mode of ['ccy', 'type', 'book']) for (const lens of ['explain', 'attr']) {
    const defs = P.rowsFor(lens, p, { carry: 'split', labels: L }), items = P.waterfallItems(defs, p.summary);
    const S = P.stackItems(items, defs, p.positions, mode, L);
    assert.equal(S.items.length, items.length);
    S.items.forEach(it => { if (it.v != null) near(it.pieces.reduce((a, x) => a + x.v, 0), it.v, mode + ' ' + lens + ' ' + it.k); });
    assert.ok(S.groups.every((g, i) => g.color === 'pal:' + i));
  }
  // the share of a group is the row on the group's positions
  const defs = P.rowsFor('explain', p, { carry: 'split' }), S = P.stackItems(P.waterfallItems(defs), defs, p.positions, 'ccy', L);
  const usd = S.items.find(x => x.k === 'fx').pieces.find(x => x.g === 'USD');
  near(usd.v, P.srcVal(pos(p, 'DEMO-B|USD-1'), 'fx'), 'USD piece of FX');
  // more groups than STACK_GROUPS: the tail as «Остальные N»
  const many = P.stackItems(P.waterfallItems(defs), defs, p.positions, 'pos', L);
  assert.equal(many.groups.length, p.positions.length <= P.STACK_GROUPS + 1 ? p.positions.length : P.STACK_GROUPS + 1);
});

test('daily axis (TDM#434): working days only, a day without a run marked and carried, day columns follow', () => {
  const p = prep();
  assert.ok(!p.dates.some(d => [0, 6].includes(new Date(d + 'T00:00:00Z').getUTCDay())), 'no weekends');
  const i = p.dates.indexOf('2026-10-07');
  assert.equal(p.axis.state[i], 'norun'); assert.equal(p.axis.hold[i], true);
  assert.equal(p.axis.text, 'прогонов ' + FX.period.runs.length + ' из ' + p.dates.length + ' рабочих дней');
  const byDate = new Map(p.days.map(d => [d.date, d])), tot = p.dates.map(d => (byDate.has(d) ? byDate.get(d).total : null));
  const cum = P.cumulate(tot, p.axis.hold);
  assert.equal(cum[i], cum[i - 1], 'the level carries over the day without a run'); near(cum.at(-1), p.summary.total, 'cumulative');
  assert.equal(P.cumulate([null, 1, null, 2], [true, false, true, false]).join(), ',1,1,3');
  // weekend market dates (a market data store with every day) do not enter the axis; a run on a weekend does
  const wk = JSON.parse(JSON.stringify(FX.period)); wk.marketDates = wk.marketDates.concat(['2026-10-03', '2026-10-04']).sort(); wk.runs.push({ date: '2026-10-04', runId: 1, prevDate: '2026-10-02' });
  const pw = P.prepare(wk);
  assert.ok(!pw.dates.includes('2026-10-03') && pw.dates.includes('2026-10-04'));
  assert.equal(pw.axis.state[pw.dates.indexOf('2026-10-04')], 'norows');
  // no market dates: every weekday of the period
  const nm = JSON.parse(JSON.stringify(FX.period)); nm.marketDates = [];
  assert.equal(P.prepare(nm).dates.length, P.weekdays(FX.period.from, FX.period.to));
  // gran day: calendar-day buckets of the server keep the working days; positions' buckets follow by index
  const day = JSON.parse(JSON.stringify(FX.period)), B = P.makeBuckets(day.from, day.to, 'day');
  day.gran = 'day'; day.buckets = B;
  const byPos = new Map(Object.entries(FX.position).map(([ref, v]) => [ref, new Map(v.days.map(x => [x.date, x.total]))]));
  day.positions.forEach(x => { x.buckets = B.map(b => (byPos.get(x.ref).has(b.from) ? byPos.get(x.ref).get(b.from) : null)); });
  const pd = P.prepare(day);
  assert.deepEqual(pd.buckets.map(b => b.from), pd.dates);
  assert.ok(pd.buckets.length < B.length);
  pd.buckets.forEach((b, k) => { if (pd.bucketAggs[k]) near(P.sumAvail(pd.positions.map(x => x.buckets[k])), pd.bucketAggs[k].total, b.key); });
  assert.equal(pd.bucketAggs[pd.dates.indexOf('2026-10-07')], null);
});

test('matrix: group cells are the sum of their positions, sorting keeps nulls last', () => {
  const p = prep(), defs = P.visibleRows(P.componentRows('explain', { carry: 'net', keys: p.keys }), p.summary).concat([{ k: 'total', v: a => a.total }]);
  const G = P.sortMatrix(P.matrix(p.positions, 'book', defs, {}), defs.length - 1, -1);
  G.forEach(g => defs.forEach((d, j) => { const kids = P.sumAvail(g.kids.map(k => k.v[j])); if (kids == null) assert.equal(g.v[j], null); else near(g.v[j], kids, g.key + ' ' + d.k); }));
  assert.ok(G[0].v.at(-1) >= G[1].v.at(-1));
  const fj = defs.findIndex(d => d.k === 'carryNet');
  const sorted = P.sortMatrix(P.matrix(p.positions, 'book', defs, {}), fj, -1).find(g => g.key === 'DEMO-B').kids;
  assert.ok(sorted.every((k, i, a) => !i || k.v[fj] == null || (a[i - 1].v[fj] != null && a[i - 1].v[fj] >= k.v[fj])));
  assert.ok(sorted.at(-1).v[fj] == null, 'the position without PnL last');
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
  assert.deepEqual(grossSplit.parts.map(d => d.k), ['carry_coupon', 'carry_pull', 'carry_nosplit', 'rolldown']);
  assert.deepEqual(P.carryParts('gross', true, false).parts.map(d => d.k), ['carry_coupon', 'carry_pull', 'rolldown'], 'every row split');
  const ofz = pos(p, 'DEMO-A|OFZ-1');
  near(grossSplit.parts.reduce((a, d) => a + d.v(ofz), 0), gross.tot.v(ofz), 'coupon + pull + roll = gross');
  near(grossSplit.parts.reduce((a, d) => a + d.v(s), 0), gross.tot.v(s), 'selection: coupon + pull + without the split + roll = gross');
  // the selection is split per row (TDM#432): coupon and pull of the rows with it, the carry of the rest
  const S = P.carrySummary(p);
  near(S.coupon, s.c.carry_coupon, 'coupon tile'); near(S.pull, s.c.carry_pull, 'pull tile'); near(S.nosplit, s.c.carry_nosplit, 'without the split');
  near(S.coupon + S.pull + S.nosplit, s.c.carry, 'tiles = carry');
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
  const corp = P.carryPosition(pos(p, 'DEMO-A|CORP-1')), mixed = P.carryTotal([o, corp]);
  near(mixed.coupon, o.coupon, 'a row without the split: the group coupon is that of the split rows');
  near(mixed.nosplit, corp.carry, 'and its carry apart'); near(mixed.coupon + mixed.pull + mixed.nosplit, o.carry + corp.carry, 'group = carry');
  // nulls last in the carry table's sort (TDM#433): the deal has no carry % p.a.
  assert.equal(irs.cy, null);
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
