'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { loadRun } = require('./fixtures/concepts_run');
const { DEAL, IDS, dealSearchRun } = require('./fixtures/deal_search');

function load(source) {
  const ctx = vm.createContext({ location: { hostname: 'localhost' }, URLSearchParams });
  ctx.window = ctx;
  if (source === 'real') dealSearchRun(loadRun(ctx));
  const file = path.join(__dirname, '../../prototypes/concepts/lib', source + '.js');
  vm.runInContext(fs.readFileSync(file, 'utf8'), ctx);
  return ctx.MOCK;
}

test('позиции ищутся по номеру сделки из отчёта или условий и по полному номеру позиции', () => {
  const M = load('real');
  const ids = q => Array.from(M.positions.filter(p => p.searchText.includes(q.toLowerCase())), p => p.id);
  assert.deepEqual(ids(DEAL), IDS.slice(0, 2));
  assert.deepEqual(ids(IDS[0]), [IDS[0]]);
  assert.deepEqual(ids(IDS[1]), [IDS[1]]);
  assert.deepEqual(ids('88000102'), [IDS[1]]);
  assert.deepEqual(ids('88000103'), [IDS[2]]);
  assert.deepEqual(ids('88000104'), [IDS[2]]);
  assert.deepEqual(ids('тестовая позиция'), IDS);
});

test('в демо ищутся номер позиции и номер сделки из оценки', () => {
  const M = load('mock');
  const bond = M.positions.find(p => p.alias && p.shortOwn);
  assert.ok(bond.searchText.includes(bond.id.toLowerCase()));
  assert.ok(bond.searchText.includes(bond.alias.toLowerCase()));
  assert.ok(bond.searchText.includes(bond.shortOwn.toLowerCase()));
  const deal = M.positions.find(p => p.ratesCore);
  assert.ok(deal.searchText.includes(deal.ratesCore.trades[0].tradeId.toLowerCase()));
});
