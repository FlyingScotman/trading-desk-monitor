'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const H = require('../../prototypes/concepts/lib/pnl-history.js');

test('period bound is inclusive, validates dates and preserves ordinary/leap years', () => {
  const limit = /1096/;
  assert.equal(H.periodError('2023-10-17', '2026-10-16', '2026-10-16'), null);
  assert.match(H.periodError('2023-10-16', '2026-10-16', '2026-10-16'), limit);
  assert.match(H.periodError('0001-01-01', '2026-10-16', '2026-10-16'), limit);
  for (const [start, end] of [['2024-01-01', '2024-12-31'], ['2025-01-01', '2025-12-31'],
    ['0001-01-01', '0001-01-02'], ['2024-02-29', '2024-02-29']]) assert.equal(H.periodError(start, end, '2026-10-16'), null);
  for (const [start, end] of [['invalid', '2026-10-16'], ['', '2026-10-16'], ['0000-01-01', '0001-01-01'], ['2025-02-29', '2026-10-16'],
    ['2026-10-17', '2026-10-16'], ['2026-10-16', '2026-10-17']]) assert.ok(H.periodError(start, end, '2026-10-16'));
});

test('loader aborts superseded main/detail transport and retains latest success/error guards', async () => {
  const queue = [], states = [];
  const load = H.loader((q, signal) => new Promise((resolve, reject) => queue.push({ q, signal, resolve, reject })), s => states.push(s));
  const first = load('old'), second = load('new');
  assert.equal(queue[0].signal.aborted, true);
  assert.equal(queue[1].signal.aborted, false);
  queue[0].reject(new DOMException('cancelled', 'AbortError'));
  queue[1].resolve({ total: 2 }); await Promise.all([first, second]);
  assert.deepEqual(states.at(-1), { data: { total: 2 } });
  const detail = load('detail'); load.invalidate();
  assert.equal(queue[2].signal.aborted, true);
  queue[2].resolve({ stale: true }); await detail;
  assert.deepEqual(states.at(-1), { loading: true });
  const latest = load('error'); queue[3].reject(new Error('latest error')); await latest;
  assert.equal(states.at(-1).error.message, 'latest error');
});

test('only newest response publishes; invalidation also suppresses stale detail and stale errors', async () => {
  const queue = [], states = [];
  const load = H.loader(q => new Promise((resolve, reject) => queue.push({ q, resolve, reject })), s => states.push(s));
  const first = load('old'), second = load('new');
  assert.deepEqual(states, [{ loading: true }, { loading: true }]);
  queue[1].resolve({ total: 2 }); await second;
  queue[0].resolve({ total: 1 }); await first;
  assert.deepEqual(states.at(-1), { data: { total: 2 } });
  const third = load('detail'); load.invalidate(); queue[2].resolve({ total: 3 }); await third;
  assert.deepEqual(states.at(-1), { loading: true });
  const fourth = load('old-error'), fifth = load('latest');
  queue[3].reject(new Error('old')); queue[4].resolve({ total: 4 }); await Promise.all([fourth, fifth]);
  assert.deepEqual(states.at(-1), { data: { total: 4 } });
});

test('historical filter catalogue preserves closed books, portfolios and safe identities', () => {
  const rows = [
    { book: 'A', positionId: 'same', entity: 'Synthetic', desk: 'Example', portfolio: 'Closed' },
    { book: 'B', positionId: 'same', entity: 'Synthetic', desk: 'Example', portfolio: 'Active' },
    { book: 'A', positionId: 'same', entity: 'Synthetic', desk: 'Example', portfolio: 'Closed' },
    { book: '__proto__', positionId: 'same' },
  ];
  const s = H.scope(rows);
  assert.equal(s.books.length, 3); assert.equal(s.positions.length, 3);
  assert.equal(s.book.__proto__.id, '__proto__');
  assert.ok(s.positions.some(p => p.portfolio === 'Closed'));
});

test('historical book memberships are invariant under catalogue permutations', () => {
  const rows = [
    { book: 'A', positionId: 'one', entity: 'Earlier entity', desk: 'Earlier desk', portfolio: 'Active' },
    { book: 'A', positionId: 'one', entity: 'Earlier entity', desk: 'Later desk', portfolio: 'Active' },
    { book: 'A', positionId: 'one', entity: 'Later entity', desk: 'Later desk', portfolio: 'Active' },
    { book: 'B', positionId: 'two', entity: null, desk: null, portfolio: 'Historical' },
  ];
  const permutations = a => a.length ? a.flatMap((r, i) => permutations(a.filter((_, j) => j !== i)).map(p => [r, ...p])) : [[]];
  const expected = [
    ['A', 'Earlier entity', 'Earlier desk'], ['A', 'Earlier entity', 'Later desk'],
    ['A', 'Later entity', 'Later desk'], ['B', '—', '—'],
  ].sort();
  for (const permutation of permutations(rows)) {
    const s = H.scope(permutation.concat(permutation[0]));
    assert.deepEqual(s.books.map(b => [b.id, b.entity, b.desk]).sort(), expected);
    assert.equal(s.positions.length, 4); // distinct position + historical membership; duplicate date row ignored
    assert.ok(s.positions.some(p => p.id === 'two' && p.entity === '—' && p.desk === '—'));
  }
});
