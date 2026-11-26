'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const C = require('../../prototypes/concepts/lib/carry-analysis');

const position = (p = {}) => ({ carryDay: 80, fundingDay: -60, mv: 200000, dv01: -100, cs01: -40,
  cpn: { type: 'fix' }, ...p });
const near = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-10, actual + ' != ' + expected);

test('B1: net only uses the intersection, including measured zero', () => {
  const list = C.rows([
    position({ id: 'complete', fundingDay: 0 }),
    position({ id: 'zero', carryDay: 0, fundingDay: 0 }),
    position({ id: 'unfunded', carryDay: 300, fundingDay: null }),
    position({ id: 'no-carry', carryDay: null, fundingDay: -120 }),
    position({ id: 'missing', carryDay: null, fundingDay: null }),
    position({ id: 'default', fundingInfo: { lines: [{ rule_source: 'default' }] } }),
    position({ id: 'default-flag', fundingInfo: { default_rate: true } }),
  ]);
  assert.deepEqual(list.map(p => p.netDay), [80, 0, null, null, null, 20, 20]);
  const s = C.summarize(list);
  assert.equal(s.net, 120);
  assert.equal(s.gross, 540);
  assert.equal(s.funding, -240);
  assert.deepEqual(s.noFunding.map(p => p.id), ['unfunded']);
  assert.equal(s.omittedCarry, 300);
  assert.deepEqual(s.noCarry.map(p => p.id), ['no-carry', 'missing']);
  assert.deepEqual(s.defaultFunding.map(p => p.id), ['default', 'default-flag']);
  assert.equal(C.summarize(list.slice(2, 5)).net, null);
  assert.equal(C.summarize([]).net, null);
  assert.equal(C.summarize(C.rows([position()], { fundingReady: false })).net, null);
});

test('scales daily amounts by calendar days and preserves missing amounts', () => {
  assert.deepEqual(['day', 'month', 'year'].map(s => C.money(-4, s)), [-4, -120, -1460]);
  assert.equal(C.money(0, 'year'), 0);
  assert.equal(C.money(null, 'month'), null);
  assert.equal(C.money(undefined, 'day'), null);
});

test('net spreads use complete MV only and funding rate weights signed positions by absolute exposure', () => {
  const list = C.rows([
    position({ mv: -200000, carryDay: -20, fundingDay: 50, fundRate: 10, ytm: 3 }),
    position({ mv: 300000, carryDay: 150, fundingDay: -100, fundRate: 20, ytm: 6 }),
    position({ mv: 100000, carryDay: 999, fundingDay: null, fundRate: 50 }),
    position({ mv: 20, notionalRub: 100000, group: 'DER', carryDay: 130, fundingDay: -30, fundRate: 30,
      fundingInfo: { lines: [{ ccy: 'USD', rate_pct: 30, body_ccy: -1000 }] } }),
  ], { fx: { USD: 100 } });
  const s = C.summarize(list);
  assert.equal(s.net, 180);
  near(s.netPct, 80 * 365 / 500000 * 100);
  near(s.netBp, s.netPct * 100);
  near(s.netPct, s.carryPct - s.fundingPct);
  near(s.fundRate, (10 * 200000 + 20 * 300000 + 30 * 100000) / 600000);
  near(s.ytm, 4.8);
  assert.equal(list[3].toNotional, true);
  near(list[3].netBp, 100 * 365 / 100000 * 10000);
  assert.equal(C.summarize(list.slice(3)).netBp, null);
});

test('breakeven uses DV01 for rates, CS01 for floaters and CDS, signed netted risk and the same eligible numerator', () => {
  const list = C.rows([
    position(), position({ group: 'DER', carryDay: 100, fundingDay: -50, dv01: 40 }),
    position({ cpn: { type: 'float' }, dv01: -200, cs01: -40 }),
    position({ productGroup: 'cds', cpn: null, cs01: -20 }),
    position({ fundingDay: null, dv01: 10 }),
    position({ dv01: 0 }), position({ cpn: { type: 'float' }, cs01: 0 }),
  ]);
  assert.equal(C.positionBE(list[0], 'net'), 6);
  assert.equal(C.positionBE(list[2], 'net'), 15);
  assert.equal(C.positionBE(list[3], 'gross'), 120);
  assert.equal(C.positionBE(list[4], 'net'), null);
  assert.equal(C.positionBE(list[5], 'gross'), null);
  assert.equal(C.positionBE(list[6], 'net'), null);
  assert.equal(C.breakeven(1e6, 0.1), null);
  assert.equal(C.breakeven(0, -100), 0);
  assert.equal(C.breakeven(-60, -100), -18);
  const net = C.summarize(list, 'net');
  assert.deepEqual(net.rates, { risk: -60, be: 35 });
  assert.deepEqual(net.credit, { risk: -60, be: 20 });
  assert.deepEqual(C.summarize(list, 'gross').rates, { risk: -50, be: 156 });
  assert.equal(C.summarize(C.rows([position(), position({ dv01: 100.01 })])).rates.be, null);
  near(C.breakeven(20 / 100, -100 / 100, 1 / 100), C.breakeven(20, -100));
});

test('waterfall reconciles complete rows, rolls through zero and preserves absent or partial splits', () => {
  const list = C.rows([
    position({ carryDay: 80, carryCouponDay: 70, carryPullDay: -10, carryPureDay: 60, rollDay: 20 }),
    position({ carryDay: 20, carryCouponDay: 0, carryPullDay: null, carryPureDay: 23, rollDay: -3 }),
    position({ carryDay: 10, carryCouponDay: null, carryPullDay: null, carryPureDay: null, rollDay: -1 }),
    position({ carryDay: 5000, fundingDay: null }), position({ carryDay: null, fundingDay: -3000 }),
  ]);
  const s = C.summarize(list);
  const steps = C.waterfall(s, -15, true);
  assert.deepEqual(steps.map(p => [p.key, p.day]), [
    ['coupon', 70], ['pull', -10], ['roll', 16], ['unsplit', 34], ['funding', -180], ['book', -15], ['net', -85],
  ]);
  assert.deepEqual(steps.slice(-3).map(p => [p.start, p.end]), [[110, -70], [-70, -85], [0, -85]]);
  assert.equal(steps[0].rows[1].v, 0);
  assert.equal(steps[4].rows.length, 3);
  const old = C.waterfall(C.summarize([list[2]]), null, false);
  assert.deepEqual(old.map(p => [p.label, p.day]), [['Кэрри (купон + pull)', 11], ['Rolldown', -1], ['Фондирование', -60], ['Нетто-кэрри', -50]]);
  assert.deepEqual(C.waterfall(C.summarize(list.slice(3)), -15, true), []);
});

test('book-level funding is divided by accrual days and scoped to selected books', () => {
  const funding = { state: 'computed', bookLevel: -300,
    byBook: { 'A': { bookLevel: -600 }, 'B': { bookLevel: 300 }, 'C': { bookLevel: 0 }, 'D': { bookLevel: null } } };
  assert.equal(C.bookFunding(funding, 3), -100);
  assert.equal(C.bookFunding(funding, 3, b => b === 'B'), 100);
  assert.equal(C.bookFunding(funding, 3, b => b === 'C'), 0);
  assert.equal(C.bookFunding(funding, 3, b => b === 'D'), null);
  assert.equal(C.bookFunding({ ...funding, state: 'not_configured' }, 3), null);
  assert.equal(C.bookFunding(funding, 0), null);
  const s = C.summarize(C.rows([position()]));
  assert.deepEqual(C.withBook(s, 100, false), { net: 20, funding: -60, book: null });
  assert.deepEqual(C.withBook(s, 100, true), { net: 120, funding: 40, book: 100 });
  assert.deepEqual(C.withBook(C.summarize([]), 100, true), { net: null, funding: 100, book: 100 });
});

test('all structural dimensions retain B1 and issuer roll-up retains its contributions', () => {
  const list = C.rows([
    position({ book: '__proto__', bookName: '<1>', entity: 'A', kind: 'FIX', ccy: 'RUB', tenor: 3, issuer: 'X' }),
    position({ book: 'C', entity: 'B', kind: 'FLT', ccy: 'USD', tenor: null, issuer: 'Y', fundingDay: null }),
  ]);
  for (const dim of ['book', 'entity', 'ccy', 'kind', 'bucket', 'issuer']) {
    const groups = C.groups(list, dim);
    assert.deepEqual(groups.map(g => g.net), [20, null]);
    assert.equal(groups[1].funding, null);
    assert.equal(groups[1].full.length, 0);
  }
  assert.deepEqual(C.groups(list, 'bucket').map(g => g.label), ['3–5Y', 'Без срока']);
  assert.equal(C.groups(list, 'book')[0].label, '<1>');
  const many = C.rows(Array.from({ length: 15 }, (_, i) => position({ issuer: 'T' + i, carryDay: 60 + i })));
  const groups = C.groups(many, 'issuer');
  assert.equal(groups.length, 13);
  assert.equal(groups.at(-1).label, 'Прочие');
  assert.equal(groups.at(-1).net, 3);
});

test('history uses each run accrual, including the first run and a missing date in the timeline', () => {
  const dates = ['2026-01-09', '2026-01-12', '2026-01-14', '2026-01-15'];
  const pnl = { carry: [100, 300, 200, 0], funding: [-30, -90, null, 0], netCarry: [60, 180, null, 0] };
  const days = [1, 3, 1, 1];
  assert.deepEqual(C.history(dates, pnl, 'day', 4, days).map(d => [d.carry, d.funding, d.net]),
    [[100, -30, 60], [100, -30, 60], [200, null, null], [0, 0, 0]]);
  assert.deepEqual(C.history(dates, pnl, 'day', 2, days).map(d => d.carry), [200, 0]);
  assert.deepEqual(C.history(dates, pnl, 'run', 4, days).map(d => d.net), [60, 180, null, 0]);
  assert.equal(C.history(dates, pnl, 'day', 4, [null, 3, 1, 1])[0].net, null);
  assert.equal(C.history(dates, pnl, 'day', 4, [0, 3, 1, 1])[0].net, null);
  assert.ok(C.history(dates, pnl, 'day', 4).every(d => d.carry === null));
  assert.ok(C.history(dates, { carry: pnl.carry }, 'day', 4, days).every(d => d.net === null));
  assert.ok(C.history(dates, { ...pnl, netCarry: undefined }, 'day', 4, days).every(d => d.net === null));
});

test('leaders rank only complete, nonzero net positions and limit both lists to ten', () => {
  const list = C.rows(Array.from({ length: 25 }, (_, i) => position({ carryDay: i - 12, fundingDay: 0 })));
  const ranked = C.leaders(list.concat(C.rows([position({ carryDay: 1000, fundingDay: null })])));
  assert.deepEqual(ranked.bleeders.map(p => p.netDay), [-12, -11, -10, -9, -8, -7, -6, -5, -4, -3]);
  assert.deepEqual(ranked.generators.map(p => p.netDay), [12, 11, 10, 9, 8, 7, 6, 5, 4, 3]);
  assert.equal(ranked.bleedRows.length, 12);
  assert.equal(ranked.generatorCount, 12);
  assert.equal(ranked.bleedDay, -78);
  assert.equal(C.leaders([]).bleedDay, null);
});
