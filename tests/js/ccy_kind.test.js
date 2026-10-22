// Таблица агрегированного риска «валюта × тип инструмента» мониторов (UI-02, prototypes/concepts/lib/instr.js):
// тип инструмента по одной таблице соответствий, отнесение многовалютных инструментов к валюте (по ногам / по валюте
// сделки), риск валюты = риск по её кривым, итоги валюты = сумма типов, риск без данных — «без риска», а не 0.
//   node tests/js/ccy_kind.test.js
// Демо-прогон window.DESK (prototypes/data.js) через lib/real.js (как прогон из базы), затем lib/instr.js без kit.js.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const same = (a, b, msg) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), msg);
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

function load(mutate) {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.window.DESK);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  const M = ctx.window.MOCK;
  M.toRepr = {}; Object.keys(M.fx).forEach(c => M.toRepr[c] = M.fx[c]);  // как kit.js в рублях
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M, I: ctx.window.INSTR };
}
const sum = (rows, mm) => (rows.m[mm] || []).reduce((s, x) => s + x.v, 0);
const block = (T, c) => T.blocks.find(b => b.ccy === c);
const kindRows = b => b.groups.flatMap(g => g.kinds);

t('тип инструмента: облигации по эмитенту и классу, деривативы по группе продукта, неизвестное — не выброшено', () => {
  const { M, I } = load(D => {
    const irs = D.positions.find(p => p.instrType === 'IRS');
    Object.assign(irs, { instrType: 'OTC', extra: { reported: { x_product_group: 'irs' } } });  // склад деска
    const otc = D.positions.find(p => p.instrType === 'CDS');
    Object.assign(otc, { instrType: 'OTC', extra: { reported: { x_product_group: 'bond_future' } } });
    const odd = D.positions.filter(p => p.instrType === 'CDS')[1];
    Object.assign(odd, { instrType: 'OTC', extra: { reported: {} } });
  });
  const kinds = {}; M.positions.forEach(p => { const k = I.kindOf(p); kinds[k] = (kinds[k] || 0) + 1; });
  for (const k of ['OFZ_PD', 'OFZ_PK', 'SOV_FX', 'CORP_FIX', 'CORP_FLT', 'SECUR', 'IRS', 'CCS', 'CDS', 'BOND_FUT', 'FX', 'OCP', 'UNCLASSIFIED'])
    assert.ok(kinds[k] > 0, k + ' ' + JSON.stringify(kinds));
  assert.strictEqual(Object.values(kinds).reduce((a, b) => a + b, 0), M.positions.length);
  assert.ok(Object.keys(kinds).every(k => I.group[k]), 'каждый тип — из таблицы');
  const chf = M.positions.find(p => p.ccy === 'CHF');
  assert.strictEqual(I.kindOf(chf), 'CORP_FIX');
  assert.strictEqual(I.kindOf({ group: 'DER', kind: 'OTC', productGroup: 'fx_future' }), 'FX_FUT');
  assert.strictEqual(I.kindOf({ group: 'DER', kind: 'FX', productGroup: 'fx_ndf' }), 'FX_FWD');
  assert.strictEqual(I.kindOf({ group: 'SUB', kind: 'FIX', assetClass: 'Муниципальные' }), 'MUNI');
  assert.strictEqual(I.kindOf({ group: 'CORP', kind: 'FIX', notValued: true }), 'BOND_NA');
  assert.strictEqual(I.kindOf({ group: 'DER', kind: 'XCCY' }), 'CCS');  // демо-набор mock.js
});

t('по ногам: риск валюты = риск по её кривым у всех позиций, CHF — не в рублях; итог валюты = сумма типов', () => {
  const { M, I } = load();
  const T = I.table(M.positions, { attribution: 'legs', units: 'repr' });
  same(T.ccys.slice(0, 2), ['RUB', 'USD']);
  assert.ok(T.ccys.includes('CHF'));
  for (const c of T.ccys) {
    const want = M.positions.reduce((s, p) => s + M.curves.filter(x => x.ccy === c && x.kind === 'rate').reduce((a, x) => a + (p.dv01ByCurve[x.id] || 0), 0), 0);
    near(sum(block(T, c).total, 'dv01'), want, 'DV01 ' + c);
    for (const mm of ['dv01', 'mv', 'notional', 'cs01', 'basis', 'fx'])
      near(sum(block(T, c).total, mm), kindRows(block(T, c)).reduce((s, r) => s + sum(r, mm), 0), c + ' ' + mm);
    block(T, c).groups.forEach(g => near(sum(g.total, 'dv01'), g.kinds.reduce((s, r) => s + sum(r, 'dv01'), 0), c + ' группа ' + g.group));
  }
  const chf = M.positions.find(p => p.ccy === 'CHF');
  assert.ok(chf.dv01 && Math.abs(sum(block(T, 'CHF').total, 'dv01') - chf.dv01) < 1e-6, 'CHF на своей кривой');
  // XCCY USD/RUB: нога RUB — в рублях (позиция «+» к счёту рублёвого блока), FX-дельта и MV — в долларах
  const rubCcs = kindRows(block(T, 'RUB')).find(r => r.kind === 'CCS');
  assert.ok(rubCcs && rubCcs.ids.size === 0 && rubCcs.legs.size > 0 && sum(rubCcs, 'dv01') !== 0 && !rubCcs.m.mv);
  const usdCcs = kindRows(block(T, 'USD')).find(r => r.kind === 'CCS');
  assert.ok(usdCcs.ids.size === 2 && sum(usdCcs, 'fx') !== 0 && !usdCcs.m['curve:RUB RUONIA']);
  // итог по всем валютам — только в валюте отчёта; DV01 — по всем кривым
  near(sum(T.total, 'dv01'), M.positions.reduce((s, p) => s + p.dv01, 0), 'DV01 всех кривых');
  near(sum(T.total, 'mv'), M.positions.reduce((s, p) => s + (p.mv || 0), 0), 'MV');
  assert.strictEqual(I.table(M.positions, { units: 'own' }).total, null);
});

t('по валюте сделки: всё в валюте позиции, чужие кривые отдельно; итоги по всем валютам те же', () => {
  const { M, I } = load();
  const legs = I.table(M.positions, { attribution: 'legs', units: 'repr' }), pos = I.table(M.positions, { attribution: 'position', units: 'repr' });
  assert.ok(!kindRows(block(pos, 'RUB')).some(r => r.kind === 'CCS' || r.kind === 'FX'));
  const usd = block(pos, 'USD').total;
  assert.ok(sum(usd, 'foreign') !== 0 && sum(usd, 'curve:RUB RUONIA') === sum(usd, 'foreign'));
  near(sum(usd, 'dv01'), M.positions.filter(p => p.ccy === 'USD').reduce((s, p) => s + (p.dv01ByCurve['USD SOFR'] || 0), 0), 'USD на своей кривой');
  for (const mm of ['dv01', 'mv', 'notional', 'cs01', 'fx']) near(sum(pos.total, mm), sum(legs.total, mm), 'итог ' + mm);
  // по валюте сделки DV01 рублёвого блока = DV01 рублёвых позиций (как в шапке кокпита)
  near(sum(block(pos, 'RUB').total, 'dv01'), M.positions.filter(p => p.ccy === 'RUB').reduce((s, p) => s + p.dv01, 0), 'RUB как в HUD');
});

t('в валюте строки: суммы валюты — в её единицах; FX-дельта — в валюте', () => {
  const { M, I } = load();
  const own = I.table(M.positions, { units: 'own' }), repr = I.table(M.positions, { units: 'repr' });
  near(sum(block(own, 'USD').total, 'dv01') * M.fx.USD, sum(block(repr, 'USD').total, 'dv01'), 'DV01 USD');
  near(sum(block(own, 'USD').total, 'mv') * M.fx.USD, sum(block(repr, 'USD').total, 'mv'), 'MV USD');
  near(sum(block(own, 'USD').total, 'fx') * M.fx.USD, sum(block(repr, 'USD').total, 'fx'), 'FX USD');
  near(sum(block(own, 'USD').total, 'fx'), M.positions.reduce((s, p) => s + ((p.fxDeltaBy || {}).USD || 0), 0), 'ОВП USD в долларах');
});

t('риска нет — «без риска» с причиной, не 0; FX-спот без процентного риска — не пропуск', () => {
  const { M, I } = load(D => {
    const x = D.positions.find(p => p.instrType === 'XCCY');
    Object.assign(x, { instrType: 'OTC', risk: {}, dv01: 0, extra: { quality: 'reported_value', reported: { x_product_group: 'ccs' } } });
    D.positions.push({ id: 'NV1', book: x.book, ccy: 'RUB', instrType: null, segment: 'Деривативы', notValued: true, risk: {}, fxDelta: {},
      extra: { quality: 'not_valued', reason: 'нет данных', reported: { x_product_group: 'irs' } }, pnl: { available: false } });
  });
  const T = I.table(M.positions, { units: 'repr' });
  const rub = block(T, 'RUB'), irs = kindRows(rub).find(r => r.kind === 'IRS');
  same(irs.missing.map(m => m.why), ['not_valued']);
  assert.ok(T.blocks.flatMap(kindRows).some(r => r.kind === 'CCS' && r.missing.some(m => m.why === 'reported_no_risk')), 'CCS по отчёту без риска');
  const htm = M.positions.filter(p => p.accrualOnly);
  assert.ok(htm.length && htm.every(p => I.missing(p) === null), 'HTM по начислению — без риска по правилу, не пропуск');
  assert.ok(!T.blocks.flatMap(kindRows).some(r => (r.kind === 'FX' || r.kind === 'OCP') && r.missing.length), 'спот и перенос ОВП — без пропусков');
  assert.strictEqual(T.total.missing.length, 2);
});

t('та же таблица соответствий, что в api/concepts.py', () => {
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', 'api', 'concepts.py'), 'utf8');
  const { I } = load();
  const rows = [...py.slice(py.indexOf('INSTR_KINDS:'), py.indexOf('"""The instrument kinds')).matchAll(/\("(\w+)", "(\w+)", "([^"]+)"\)/g)].map(m => [m[1], m[2], m[3]]);
  same(rows, I.KINDS);
  const pk = Object.fromEntries([...py.slice(py.indexOf('PRODUCT_KIND = {'), py.indexOf('"""The private adapter')).matchAll(/"(\w+)": "(\w+)"/g)].map(m => [m[1], m[2]]));
  same(pk, I.PRODUCT_KIND);
  assert.ok(py.includes('CROSS_CCY_KINDS = (' + I.CROSS_CCY.map(k => '"' + k + '"').join(', ') + ')'));
  assert.ok(py.includes('ATTRIBUTION_DEFAULT = "' + I.ATTRIBUTION_DEFAULT + '"'));
});

console.log(n + ' tests passed');
