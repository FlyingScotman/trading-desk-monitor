'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const H = require('../../prototypes/concepts/lib/pnl-history.js');

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
