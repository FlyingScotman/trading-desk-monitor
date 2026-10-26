// C17 «Прогнозное кэрри» (#66, #72): разбор ответа GET /api/v1/concepts/carry (prototypes/concepts/lib/carry.js) и
// демо-ответ той же формы (lib/mock.js M.forwardCarryDemo). Кэрри + Фондирование + «фондирование без прогноза кэрри»
// = Нетто; без фильтров итоги экрана совпадают с totals сервера; фондирование null не подменяется нулём; held / curve.
//   node --test tests/js/forward_carry.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const src = f => fs.readFileSync(path.join(DIR, f), 'utf8');

const ctx = { document: { write() { } }, location: { protocol: 'file:' }, console, URLSearchParams };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(src('lib/mock.js'), ctx);
vm.runInContext(src('lib/instr.js'), ctx);
vm.runInContext(src('lib/carry.js'), ctx);
const M = ctx.MOCK, C = ctx.CARRY;
const near = (a, b, msg, eps) => assert.ok(Math.abs(a - b) <= (eps || 1e-6) * Math.max(1, Math.abs(b)), `${msg}: ${a} vs ${b}`);
const model = (resp, k) => C.rows(resp, { pos: M.pos, k, books: M.book }).concat(C.cashRows(resp, { k, books: M.book }));

// ответ сервера на золотых данных бэкенда (backend/tests, test_forward_carry: прогон с пятью позициями), сокращён
const REAL = {
  method: 'desk-forward-carry-v1', runId: 2, asOf: '2026-10-15', horizonDate: '2027-01-15', days: 92, reportingCcy: 'RUB', ks: 'flat', spread: 'held',
  spreadCurves: [], meetings: [{ date: '2026-10-23', effective: '2026-10-26', estimated: true, stepBp: 0 }, { date: '2026-12-18', effective: '2026-12-21', estimated: true, stepBp: 0 }],
  keyRatePath: [{ date: '2026-10-15', keyRatePct: 14 }], fundingStatus: 'computed', fundingText: 'фондирование посчитано',
  positions: [
    { positionId: 'C1|26238', bookId: 'C1', entity: 'ENT2', secid: 'SU26238RMFS4', ccy: 'RUB', kind: 'OFZ_PD', quality: 'exact', carry: 20214.63211743014, coupon: 0, pull: 0, rolldown: 0, funding: 0, net: 20214.63211743014, fundingState: 'not_funded', fundingSource: 'none', fundingRate: null, fundingRuleId: null, fundingNote: 'юрлицо фондируется по всему остатку ДС', spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: 'core', code: null, detail: null },
    { positionId: 'D1|USD', bookId: 'D1', entity: 'ENT1', secid: null, ccy: 'USD', kind: 'FX', quality: 'market_value', carry: null, coupon: null, pull: null, rolldown: null, funding: 0, net: null, fundingState: 'not_funded', fundingSource: 'none', fundingRate: null, fundingRuleId: null, fundingNote: 'не облигация', spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: null, code: 'market_value', detail: null },
    { positionId: 'H1|29008', bookId: 'H1', entity: 'ENT1', secid: 'SU29008RMFS8', ccy: 'RUB', kind: 'OFZ_PK', quality: 'assumed', carry: 735422.0534714386, coupon: 0, pull: 0, rolldown: 0, funding: -686030.1369863013, net: 49391.916485137306, fundingState: 'funded', fundingSource: 'adapter', fundingRate: '14.25% contract', fundingRuleId: 'htm', fundingNote: null, spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: 'core', code: null, detail: null },
    { positionId: 'T1|26238', bookId: 'T1', entity: 'ENT1', secid: 'SU26238RMFS4', ccy: 'RUB', kind: 'OFZ_PD', quality: 'exact', carry: 202146.3211743014, coupon: 0, pull: 0, rolldown: 0, funding: -194523.28767119625, net: 7623.033503105165, fundingState: 'funded', fundingSource: 'adapter', fundingRate: 'KEY_RATE + 100 bp', fundingRuleId: 'trading_rub', fundingNote: null, spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: 'core', code: null, detail: null },
    { positionId: 'T1|XR1', bookId: 'T1', entity: 'ENT1', secid: 'RU000A109XR1', ccy: 'RUB', kind: 'CORP_FLT', quality: 'assumed', carry: 117226.7736035152, coupon: 0, pull: 0, rolldown: 0, funding: -91184.35068493096, net: 26042.42291858424, fundingState: 'funded', fundingSource: 'adapter', fundingRate: 'KEY_RATE + 100 bp', fundingRuleId: 'trading_rub', fundingNote: null, spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: 'core', code: null, detail: null },
  ],
  totals: { carry: 1075009.7803666852, funding: -3099584.350684894, fundingNoCarry: 0, fundingBookLevel: -2127846.575342465, net: -2024574.5703182085, nPositions: 5, nCovered: 4, nNotCovered: 1, nFunded: 3, nFundingMissing: 0 },
  byBook: {}, byEntity: {}, byCcy: {}, byKind: {},
  fundingBookLevel: [
    { book: 'D1', entity: 'ENT1', ccy: 'RUB', kind: 'CASH', amount: -4207298.630136986, rateExpr: 'RUONIA', ruleId: 'derivs_rub', ruleSource: 'adapter', defaultRate: false },
    { book: 'C1', entity: 'ENT2', ccy: 'RUB', kind: 'ENTITY_CASH', amount: 2079452.0547945208, rateExpr: 'KEY_RATE + 100 bp', ruleId: 'ent2_cash', ruleSource: 'adapter', defaultRate: false },
  ],
  fundingProblems: [], provisional: [], notes: ['спред держится (spread=held): spreadRoll не считается'],
};
// поля ответа (backend api/models.py CarryOut, CarryPosition, CarrySums)
const TOP = ['method', 'runId', 'asOf', 'horizonDate', 'days', 'reportingCcy', 'ks', 'spread', 'spreadCurves', 'meetings', 'keyRatePath', 'fundingStatus', 'fundingText',
  'positions', 'totals', 'byBook', 'byEntity', 'byCcy', 'byKind', 'fundingBookLevel', 'fundingProblems', 'provisional', 'notes'];
const POS = ['positionId', 'bookId', 'entity', 'secid', 'ccy', 'kind', 'quality', 'carry', 'coupon', 'pull', 'rolldown', 'funding', 'net', 'fundingState', 'fundingSource',
  'fundingRate', 'fundingRuleId', 'fundingNote', 'spreadRoll', 'spreadRollBp', 'spreadCurve', 'carryMethod', 'code', 'detail'];
const SUMS = ['carry', 'coupon', 'pull', 'rolldown', 'spreadRoll', 'funding', 'fundingNoCarry', 'fundingBookLevel', 'net', 'nPositions', 'nCovered', 'nNotCovered', 'nFunded', 'nFundingMissing'];

test('запрос: горизонт, путь КС с шагами по заседаниям, режим спреда', () => {
  assert.strictEqual(C.query({ run: 7, horizon: '6M', ks: 'steps', steps: [25, -50, 0, 0], spread: 'curve' }), 'run=7&horizon=6M&ks=steps&steps=25,-50&spread=curve');
  assert.strictEqual(C.query({ horizon: '2027-01-15', ks: 'flat', steps: [25], spread: 'held' }), 'horizon=2027-01-15&ks=flat&spread=held');
  assert.strictEqual(C.query({ ks: 'steps', steps: [0, 0] }), 'horizon=3M&ks=steps&spread=held');
  assert.strictEqual(C.url({ run: 1 }), '../api/v1/concepts/carry?run=1&horizon=3M&ks=flat&spread=held');
  const ms = C.meetingList([[{ date: '2026-12-18', effective: '2026-12-21' }], [{ date: '2026-10-23', effective: '2026-10-26', estimated: true }, { date: '2026-12-18', effective: '2026-12-21' }]]);
  assert.strictEqual(JSON.stringify(ms.map(m => m.date)), '["2026-10-23","2026-12-18"]');
  assert.strictEqual(JSON.stringify(C.stepsFor(ms, { '2026-12-18': -50 }, '2026-10-15')), '[0,-50]');
});

test('демо-ответ той же формы, что ответ сервера', () => {
  for (const o of [{}, { horizon: '1Y', ks: 'steps', steps: [-50, -50], spread: 'curve' }, { funding: 'not_configured' }]) {
    const d = M.forwardCarryDemo(o);
    assert.deepStrictEqual(Object.keys(d).sort(), TOP.slice().sort());
    d.positions.forEach(p => assert.deepStrictEqual(Object.keys(p).sort(), POS.slice().sort(), p.positionId));
    [d.totals, ...Object.values(d.byBook)].forEach(g => assert.deepStrictEqual(Object.keys(g).sort(), SUMS.slice().sort()));
  }
  // в демо есть всё, что экран должен показать: позиции без прогноза, null-фондирование в одной книге, остатки ДС
  const d = M.forwardCarryDemo({});
  assert.ok(d.positions.some(p => p.carry == null && p.code === 'not_a_bond'));
  assert.ok(d.positions.some(p => p.code === 'core_unsupported_product' && p.funding != null), 'валютная бумага без прогноза, но с фондированием');
  const nullBooks = new Set(d.positions.filter(p => p.funding == null).map(p => p.bookId));
  assert.deepStrictEqual([...nullBooks], ['IC-BND']);
  assert.ok(d.fundingBookLevel.length >= 2);
});

test('Кэрри + Фондирование + фондирование без прогноза = Нетто; без фильтров — итоги сервера', () => {
  const demo = M.forwardCarryDemo({ horizon: '6M' });
  // на реальном ответе и на демо, где книге без правил дано фондирование
  const demoFull = Object.assign({}, demo, { positions: demo.positions.map(p => p.bookId === 'IC-BND' ? Object.assign({}, p, { funding: -1000, net: p.carry == null ? null : p.carry - 1000, fundingState: 'funded' }) : p) });
  for (const resp of [REAL, demoFull]) {
    const s = C.sums(model(resp), true);
    near(s.carry + s.fundingCovered + s.fundingRest, s.net, 'сумма на глазах');
    if (resp === REAL) {
      near(s.carry, resp.totals.carry, 'кэрри = totals.carry');
      near(s.funding, resp.totals.funding, 'фондирование = totals.funding');
      near(s.net, resp.totals.net, 'нетто = totals.net');
      near(s.fundingCash, resp.totals.fundingBookLevel, 'остатки ДС');
      near(s.fundingNoCarry, resp.totals.fundingNoCarry, 'без прогноза');
      assert.strictEqual(s.nCovered, 4); assert.strictEqual(s.nNotCovered, 1);
    }
  }
  // демо: сервер-образец в mock.js считает totals как assemble; разбор совпадает с ним
  const s = C.sums(model(demoFull), true);
  const t = demoFull.totals;
  near(s.carry, t.carry, 'демо кэрри');
  assert.strictEqual(s.nNotCovered, t.nNotCovered);
});

test('фондирование null: «не настроено», нетто «—», не ноль', () => {
  const d = M.forwardCarryDemo({});
  const rows = model(d);
  const ic = rows.filter(r => r.book === 'IC-BND');
  assert.ok(ic.length && ic.every(r => r.funding === null && r.net === null && r.fundingState === 'not_configured'));
  const all = C.sums(rows, true);
  assert.strictEqual(all.net, null, 'нетто деска — «—», пока у позиции с прогнозом нет фондирования');
  assert.ok(all.nFundingMissing === ic.filter(r => r.covered).length && all.nFundingMissing > 0);
  near(all.netPartial, all.carry - all.fundingMissing.reduce((a, r) => a + r.carry, 0) + all.fundingCovered + all.fundingRest, 'частичное нетто — по позициям с фондированием');
  const other = C.sums(rows.filter(r => r.book !== 'IC-BND'), true);
  assert.notStrictEqual(other.net, null);
  // группы: у книги без правил нетто «—», у остальных — число
  const g = C.groups(rows, 'book', true);
  assert.strictEqual(g.find(x => x.key === 'IC-BND').sums.net, null);
  assert.ok(g.filter(x => x.key !== 'IC-BND').every(x => x.sums.net != null));
  // не настроено целиком: всё фондирование null, остатков ДС нет, нетто null
  const nc = M.forwardCarryDemo({ funding: 'not_configured' });
  const rn = model(nc), sn = C.sums(rn, nc.fundingStatus === 'computed');
  assert.strictEqual(nc.fundingStatus, 'not_configured');
  assert.ok(rn.every(r => r.funding === null && r.net === null && !r.isCash));
  assert.strictEqual(sn.funding, null); assert.strictEqual(sn.net, null); assert.ok(sn.carry > 0 || sn.carry < 0);
});

test('спред держится / roll по спреду: разница — spreadRoll, он входит в кэрри', () => {
  const held = M.forwardCarryDemo({ spread: 'held' }), curve = M.forwardCarryDemo({ spread: 'curve' });
  const sh = C.sums(model(held), true), sc = C.sums(model(curve), true);
  assert.strictEqual(sh.spreadRoll, null, 'held: roll по спреду не считается');
  assert.ok(sc.spreadRoll != null && Math.abs(sc.spreadRoll) > 0);
  near(sc.carry - sh.carry, sc.spreadRoll, 'кэрри curve − held = roll по спреду');
  near(sc.spreadRoll, curve.totals.spreadRoll, 'итог сервера');
  const p = curve.positions.find(x => x.spreadRoll != null);
  const ph = held.positions.find(x => x.positionId === p.positionId);
  near(p.carry - ph.carry, p.spreadRoll, 'по позиции');
  assert.ok(curve.positions.some(x => x.spreadCurve === 'ofz_held') && curve.positions.some(x => x.spreadCurve === 'floater_held'));
  // фондирование от режима спреда не зависит
  near(sc.funding, sh.funding, 'фондирование то же');
});

test('путь КС: шаги меняют фондирование, не кэрри', () => {
  const flat = M.forwardCarryDemo({ horizon: '1Y' }), st = M.forwardCarryDemo({ horizon: '1Y', ks: 'steps', steps: [-100, -100] });
  assert.ok(st.meetings.length >= 2 && st.meetings[0].stepBp === -100 && st.meetings[1].stepBp === -100);
  assert.ok(st.keyRatePath.length === st.meetings.length + 1);
  const a = C.sums(model(flat), true), b = C.sums(model(st), true);
  near(a.carry, b.carry, 'кэрри');
  assert.ok(b.fundingCovered > a.fundingCovered, 'КС ниже — фондирование дешевле');
});

test('валюта показа, carry/DV01, лидеры и аутсайдеры', () => {
  const d = M.forwardCarryDemo({});
  const rub = model(d), usd = model(d, 1 / M.fx.USD);
  near(C.sums(usd, true).carry, C.sums(rub, true).carry / M.fx.USD, 'в USD');
  const r = rub.find(x => x.covered && x.dv01);
  near(r.carryDv01, r.carry / Math.abs(r.dv01), 'кэрри / |DV01|');
  assert.strictEqual(C.perDv01(100, 0), null);
  const L = C.leaders(rub, 10);
  assert.ok(L.top.length <= 10 && L.bottom.length <= 10 && L.top.length > 0);
  assert.ok(L.top.every((x, i) => x.net > 0 && (!i || L.top[i - 1].net >= x.net)));
  assert.ok(L.bottom.every((x, i) => x.net < 0 && (!i || L.bottom[i - 1].net <= x.net)));
  assert.ok([...L.top, ...L.bottom].every(x => !x.isCash && x.net != null));
});

test('причины «нет прогноза» и фильтры строк без позиции', () => {
  assert.strictEqual(C.reasonText('core_unsupported_product'), C.REASON.core_unsupported_product);
  assert.match(C.reasonText('core_xyz'), /ядро отклонило: xyz/);
  assert.match(C.reasonText('approx_dur'), /ниже точной/);
  const keep = C.keep({ entity: 'ENT1' }, {});
  assert.ok(keep({ book: 'D1', entity: 'ENT1', ccy: 'RUB' }) && !keep({ book: 'C1', entity: 'ENT2', ccy: 'RUB' }));
  assert.ok(C.keep({ books: ['C1'] }, {})({ book: 'C1', ccy: 'RUB' }));
  assert.ok(!C.keep({ desk: 'Rates' }, { X: { desk: 'Credit' } })({ book: 'X', ccy: 'RUB' }));
});
