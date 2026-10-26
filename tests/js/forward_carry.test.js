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

// ответ сервера на золотых данных бэкенда (backend/tests, test_forward_carry: прогон с пятью позициями):
// GET /api/v1/concepts/carry?horizon=3M с бэкендом #297 и правилом B1 строгим (коммит 1ca4e12); notes сокращены
const REAL = {"method": "desk-forward-carry-v1", "runId": 2, "asOf": "2026-10-15", "horizonDate": "2027-01-15", "days": 92, "reportingCcy": "RUB", "ks": "flat", "spread": "held", "spreadCurves": [], "meetings": [{"date": "2026-10-23", "effective": "2026-10-26", "estimated": true, "stepBp": 0.0, "inHorizon": true}, {"date": "2026-12-18", "effective": "2026-12-21", "estimated": true, "stepBp": 0.0, "inHorizon": true}, {"date": "2027-02-12", "effective": "2027-02-15", "estimated": true, "stepBp": 0.0, "inHorizon": false}, {"date": "2027-03-19", "effective": "2027-03-22", "estimated": true, "stepBp": 0.0, "inHorizon": false}, {"date": "2027-04-30", "effective": "2027-05-04", "estimated": true, "stepBp": 0.0, "inHorizon": false}, {"date": "2027-06-11", "effective": "2027-06-15", "estimated": true, "stepBp": 0.0, "inHorizon": false}, {"date": "2027-07-30", "effective": "2027-08-02", "estimated": true, "stepBp": 0.0, "inHorizon": false}, {"date": "2027-09-10", "effective": "2027-09-13", "estimated": true, "stepBp": 0.0, "inHorizon": false}], "keyRatePath": [{"date": "2026-10-15", "keyRatePct": 14.0}], "fundingStatus": "computed", "fundingText": "фондирование посчитано", "positions": [{"positionId": "C1|26238", "bookId": "C1", "entity": "ENT2", "secid": "SU26238RMFS4", "ccy": "RUB", "kind": "OFZ_PD", "quality": "exact", "carry": 20214.63211743014, "coupon": 17900.000000000004, "pull": -2306.7437086339432, "rolldown": 4621.375826064082, "funding": 0.0, "net": 20214.63211743014, "dv01": -317.2534929677719, "cs01": -312.2937028638746, "fxRate": 1.0, "inCcy": {"carry": 20214.63211743014, "coupon": 17900.000000000004, "pull": -2306.7437086339432, "rolldown": 4621.375826064082, "spreadRoll": null, "funding": 0.0, "net": 20214.63211743014}, "fundingState": "not_funded", "fundingSource": "none", "fundingRate": null, "fundingRuleId": null, "fundingNote": "юрлицо фондируется по всему остатку ДС", "spreadRoll": null, "spreadRollBp": null, "spreadCurve": null, "carryMethod": "core", "code": null, "detail": null}, {"positionId": "D1|USD", "bookId": "D1", "entity": "ENT1", "secid": null, "ccy": "USD", "kind": "FX", "quality": "market_value", "carry": null, "coupon": null, "pull": null, "rolldown": null, "funding": 0.0, "net": null, "dv01": 0.0, "cs01": 0.0, "fxRate": 83.76, "inCcy": {"carry": null, "coupon": null, "pull": null, "rolldown": null, "spreadRoll": null, "funding": 0.0, "net": null}, "fundingState": "not_funded", "fundingSource": "none", "fundingRate": null, "fundingRuleId": null, "fundingNote": "не облигация: фондируется по правилам книги (остатки ДС), а не по MV", "spreadRoll": null, "spreadRollBp": null, "spreadCurve": null, "carryMethod": null, "code": "market_value", "detail": null}, {"positionId": "H1|29008", "bookId": "H1", "entity": "ENT1", "secid": "SU29008RMFS8", "ccy": "RUB", "kind": "OFZ_PK", "quality": "assumed", "carry": 735422.0534714386, "coupon": 783400.0, "pull": -50570.26392938802, "rolldown": 2592.317400826687, "funding": -686030.1369863013, "net": 49391.916485137306, "dv01": -1146.323261998532, "cs01": -4425.274484663078, "fxRate": 1.0, "inCcy": {"carry": 735422.0534714386, "coupon": 783400.0, "pull": -50570.26392938802, "rolldown": 2592.317400826687, "spreadRoll": null, "funding": -686030.1369863013, "net": 49391.916485137306}, "fundingState": "funded", "fundingSource": "adapter", "fundingRate": "14.25% contract", "fundingRuleId": "htm", "fundingNote": null, "spreadRoll": null, "spreadRollBp": null, "spreadCurve": null, "carryMethod": "core", "code": null, "detail": null}, {"positionId": "T1|26238", "bookId": "T1", "entity": "ENT1", "secid": "SU26238RMFS4", "ccy": "RUB", "kind": "OFZ_PD", "quality": "exact", "carry": 202146.3211743014, "coupon": 179000.00000000003, "pull": -23067.43708633943, "rolldown": 46213.75826064082, "funding": -194523.28767119625, "net": 7623.033503105165, "dv01": -3172.534929677719, "cs01": -3122.9370286387457, "fxRate": 1.0, "inCcy": {"carry": 202146.3211743014, "coupon": 179000.00000000003, "pull": -23067.43708633943, "rolldown": 46213.75826064082, "spreadRoll": null, "funding": -194523.28767119625, "net": 7623.033503105165}, "fundingState": "funded", "fundingSource": "adapter", "fundingRate": "KEY_RATE + 100 bp", "fundingRuleId": "trading_rub", "fundingNote": null, "spreadRoll": null, "spreadRollBp": null, "spreadCurve": null, "carryMethod": "core", "code": null, "detail": null}, {"positionId": "T1|XR1", "bookId": "T1", "entity": "ENT1", "secid": "RU000A109XR1", "ccy": "RUB", "kind": "CORP_FLT", "quality": "assumed", "carry": 117226.7736035152, "coupon": 95296.16768892654, "pull": 20629.114715826276, "rolldown": 1301.4911987623918, "funding": -91184.35068493096, "net": 26042.42291858424, "dv01": 82.314939563787, "cs01": -125.1632672009464, "fxRate": 1.0, "inCcy": {"carry": 117226.7736035152, "coupon": 95296.16768892654, "pull": 20629.114715826276, "rolldown": 1301.4911987623918, "spreadRoll": null, "funding": -91184.35068493096, "net": 26042.42291858424}, "fundingState": "funded", "fundingSource": "adapter", "fundingRate": "KEY_RATE + 100 bp", "fundingRuleId": "trading_rub", "fundingNote": null, "spreadRoll": null, "spreadRollBp": null, "spreadCurve": null, "carryMethod": "core", "code": null, "detail": null}], "totals": {"carry": 1075009.7803666852, "coupon": 1075596.1676889264, "pull": -55315.33000853511, "rolldown": 54728.94268629398, "spreadRoll": null, "dv01": -4553.796745080235, "cs01": -7985.668483366644, "funding": -971737.7753424285, "fundingNoCarry": 0.0, "fundingBookLevel": -2127846.575342465, "fundingTotal": -3099584.350684894, "net": -2024574.5703182085, "netKnown": 103272.00502425685, "nPositions": 5, "nCovered": 4, "nNotCovered": 1, "nFunded": 3, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}, "byBook": {"C1": {"carry": 20214.63211743014, "coupon": 17900.000000000004, "pull": -2306.7437086339432, "rolldown": 4621.375826064082, "spreadRoll": null, "dv01": -317.2534929677719, "cs01": -312.2937028638746, "funding": 0.0, "fundingNoCarry": null, "fundingBookLevel": 2079452.0547945208, "fundingTotal": 2079452.0547945208, "net": 2099666.686911951, "netKnown": 20214.63211743014, "nPositions": 1, "nCovered": 1, "nNotCovered": 0, "nFunded": 0, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}, "D1": {"carry": null, "coupon": null, "pull": null, "rolldown": null, "spreadRoll": null, "dv01": 0.0, "cs01": 0.0, "funding": null, "fundingNoCarry": 0.0, "fundingBookLevel": -4207298.630136986, "fundingTotal": -4207298.630136986, "net": null, "netKnown": null, "nPositions": 1, "nCovered": 0, "nNotCovered": 1, "nFunded": 0, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}, "H1": {"carry": 735422.0534714386, "coupon": 783400.0, "pull": -50570.26392938802, "rolldown": 2592.317400826687, "spreadRoll": null, "dv01": -1146.323261998532, "cs01": -4425.274484663078, "funding": -686030.1369863013, "fundingNoCarry": null, "fundingBookLevel": 0.0, "fundingTotal": -686030.1369863013, "net": 49391.916485137306, "netKnown": 49391.916485137306, "nPositions": 1, "nCovered": 1, "nNotCovered": 0, "nFunded": 1, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}, "T1": {"carry": 319373.0947778166, "coupon": 274296.16768892657, "pull": -2438.322370513153, "rolldown": 47515.24945940321, "spreadRoll": null, "dv01": -3090.219990113932, "cs01": -3248.100295839692, "funding": -285707.6383561272, "fundingNoCarry": null, "fundingBookLevel": 0.0, "fundingTotal": -285707.6383561272, "net": 33665.45642168942, "netKnown": 33665.45642168941, "nPositions": 2, "nCovered": 2, "nNotCovered": 0, "nFunded": 2, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}}, "byEntity": {}, "byCcy": {"RUB": {"carry": 1075009.7803666852, "coupon": 1075596.1676889264, "pull": -55315.33000853511, "rolldown": 54728.94268629398, "spreadRoll": null, "dv01": -4553.796745080235, "cs01": -7985.668483366644, "funding": -971737.7753424285, "fundingNoCarry": null, "fundingBookLevel": -2127846.575342465, "fundingTotal": -3099584.350684894, "net": -2024574.5703182085, "netKnown": 103272.00502425685, "nPositions": 4, "nCovered": 4, "nNotCovered": 0, "nFunded": 3, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}, "USD": {"carry": null, "coupon": null, "pull": null, "rolldown": null, "spreadRoll": null, "dv01": 0.0, "cs01": 0.0, "funding": null, "fundingNoCarry": 0.0, "fundingBookLevel": 0.0, "fundingTotal": 0.0, "net": null, "netKnown": null, "nPositions": 1, "nCovered": 0, "nNotCovered": 1, "nFunded": 0, "nFundingMissing": 0, "nCoveredNoFunding": 0, "nNoCarryNoFunding": 0}}, "byKind": {}, "fundingBookLevel": [{"book": "D1", "entity": "ENT1", "ccy": "RUB", "kind": "CASH", "amount": -4207298.630136986, "rateExpr": "RUONIA", "ruleId": "derivs_rub", "ruleSource": "adapter", "defaultRate": false}, {"book": "C1", "entity": "ENT2", "ccy": "RUB", "kind": "ENTITY_CASH", "amount": 2079452.0547945208, "rateExpr": "KEY_RATE + 100 bp", "ruleId": "ent2_cash", "ruleSource": "adapter", "defaultRate": false}], "fundingProblems": [], "provisional": ["repo.contract_rate_from_term_days", "repo.security_rate_book_type", "entity_cash_rate_book_type"], "notes": ["кэрри: ядро horizon_portfolio на рынке прогона; спред каждой бумаги откалиброван к цене прогона и держится, кривые стоят (форварды кривой реализуются, roll-down — форма кривой сохраняется)"]};
const REAL297 = REAL;
// поля ответа (backend api/models.py CarryOut, CarryPosition, CarrySums)
const TOP = ['method', 'runId', 'asOf', 'horizonDate', 'days', 'reportingCcy', 'ks', 'spread', 'spreadCurves', 'meetings', 'keyRatePath', 'fundingStatus', 'fundingText',
  'positions', 'totals', 'byBook', 'byEntity', 'byCcy', 'byKind', 'fundingBookLevel', 'fundingProblems', 'provisional', 'notes'];
const POS = ['positionId', 'bookId', 'entity', 'secid', 'ccy', 'kind', 'quality', 'carry', 'coupon', 'pull', 'rolldown', 'funding', 'net', 'fundingState', 'fundingSource',
  'fundingRate', 'fundingRuleId', 'fundingNote', 'spreadRoll', 'spreadRollBp', 'spreadCurve', 'carryMethod', 'code', 'detail',
  'dv01', 'cs01', 'fxRate', 'inCcy'];
const SUMS = ['carry', 'coupon', 'pull', 'rolldown', 'spreadRoll', 'dv01', 'cs01', 'funding', 'fundingNoCarry', 'fundingBookLevel', 'fundingTotal', 'net', 'netKnown',
  'nPositions', 'nCovered', 'nNotCovered', 'nFunded', 'nFundingMissing', 'nCoveredNoFunding', 'nNoCarryNoFunding'];
// демо — в формате #297; ключи ответа сверены с ответом сервера ветки #297 (REAL297)

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
      near(s.funding, resp.totals.fundingTotal, 'фондирование = totals.fundingTotal');
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
  near(all.netKnown, all.carry - all.fundingMissing.reduce((a, r) => a + (r.carry || 0), 0) + all.fundingCovered, 'netKnown — нетто позиций с кэрри и фондированием');
  const other = C.sums(rows.filter(r => r.book !== 'IC-BND'), true);
  assert.notStrictEqual(other.net, null);
  // группы: у книги без правил нетто «—», у остальных — число
  const g = C.groups(rows, 'book', true);
  assert.strictEqual(g.find(x => x.key === 'IC-BND').sums.net, null);
  assert.ok(g.filter(x => x.key !== 'IC-BND').every(x => x.sums.nCovered ? x.sums.net != null : x.sums.net === null), 'без позиций с прогнозом — null, как у сервера');
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

test('флоатеры: знаменатель CS01, итог портфеля — по фиксам, флоатеры отдельно', () => {
  const d = M.forwardCarryDemo({});
  const rows = model(d);
  const flt = rows.filter(r => r.covered && r.floater), fix = rows.filter(r => r.covered && !r.floater);
  assert.ok(flt.length && fix.length);
  flt.forEach(r => {
    assert.strictEqual(r.denom, 'CS01');
    assert.strictEqual(r.p.cpn.type, 'float');
    if (r.cs01) near(r.carryDv01, r.carry / Math.abs(r.cs01), 'флоатер: кэрри / |CS01|');
  });
  fix.forEach(r => { assert.strictEqual(r.denom, 'DV01'); if (r.dv01) near(r.carryDv01, r.carry / Math.abs(r.dv01), 'фикс: кэрри / |DV01|'); });
  // CS01 = 0 или нет — «—» (null)
  const one = cs01 => { const p = Object.assign({}, flt[0].p, { cs01 }); return C.rows({ fundingStatus: 'computed', positions: [flt[0].raw] }, { pos: { [p.id]: p } })[0]; };
  assert.strictEqual(one(0).carryDv01, null);
  assert.strictEqual(one(null).carryDv01, null);
  const s = C.sums(rows, true);
  // позиция без знаменателя (0 или нет) не входит ни в числитель, ни в знаменатель: ОФЗ-ПК с CS01 = 0
  const has = (r, d) => d != null && Math.abs(d) >= 1e-6;
  const fixD = fix.filter(r => has(r, r.dv01)), fltD = flt.filter(r => has(r, r.cs01)), pk = flt.filter(r => r.kind === 'OFZ_PK' && !r.cs01);
  assert.ok(pk.length && pk.every(r => r.carryDv01 === null), 'ОФЗ-ПК: CS01 = 0, в строке «—»');
  near(s.carryDv01, fixD.reduce((a, r) => a + r.carry, 0) / Math.abs(fixD.reduce((a, r) => a + r.dv01, 0)), 'итог по фиксам без флоатеров');
  near(s.carryCs01Flt, fltD.reduce((a, r) => a + r.carry, 0) / Math.abs(fltD.reduce((a, r) => a + r.cs01, 0)), 'флоатеры: кэрри / |CS01| без ОФЗ-ПК');
  const naive = flt.reduce((a, r) => a + r.carry, 0) / Math.abs(fltD.reduce((a, r) => a + r.cs01, 0));
  assert.ok(Math.abs(naive - s.carryCs01Flt) > 1, 'кэрри ОФЗ-ПК в числитель не попадает');
  assert.strictEqual(s.nFlt, fltD.length); assert.strictEqual(s.nFix, fixD.length);
  // строки остатков ДС — без знаменателя
  assert.ok(rows.filter(r => r.isCash).every(r => r.denom === null && r.carryDv01 === null));
});

test('нетто группы: нет позиций с прогнозом или фондирование null у позиции без прогноза — «—»; группы = byBook сервера', () => {
  const d = M.forwardCarryDemo({});
  const rows = model(d);
  // фильтр USD: позиций с прогнозом нет — кэрри и нетто null, как у сервера (net null, пока carry null)
  const usd = C.sums(rows.filter(r => r.ccy === 'USD'), true);
  assert.strictEqual(usd.nCovered, 0); assert.strictEqual(usd.carry, null); assert.strictEqual(usd.net, null);
  assert.strictEqual(d.byCcy.USD.net, null);
  // позиция без прогноза с фондированием null (problem): в списке для баннера, нетто «—» (B1 строгое)
  const d2 = JSON.parse(JSON.stringify(M.forwardCarryDemo({})));
  d2.positions.forEach(p => { if (p.bookId === 'IC-BND') Object.assign(p, { funding: -1000, net: p.carry == null ? null : p.carry - 1000, fundingState: 'funded' }); });
  const nc = d2.positions.find(p => p.carry == null && p.funding != null);
  Object.assign(nc, { funding: null, fundingState: 'problem', fundingNote: 'нет правила для строки' });
  const s2 = C.sums(model(d2), true);
  assert.strictEqual(s2.nFundingMissing, 0); assert.strictEqual(s2.nNoCarryNoFunding, 1);
  // B1 строгое (решение по #286): fundingNoCarry, «без прогноза кэрри», всё фондирование и нетто — null; netKnown — частичная сумма
  assert.strictEqual(s2.net, null); assert.strictEqual(s2.fundingNoCarry, null); assert.strictEqual(s2.fundingRest, null); assert.strictEqual(s2.funding, null);
  assert.ok(s2.netKnown != null);
  near(s2.netKnown, model(d2).filter(r => r.covered && r.funding != null).reduce((a, r) => a + r.carry + r.funding, 0), 'netKnown');
  // итоги сервера со счётчиком nNoCarryNoFunding: строгое правило держится и при числах в остальных полях
  const t2 = Object.assign({}, d2.totals, { fundingNoCarry: -5, fundingTotal: -10, net: 7, nNoCarryNoFunding: 1 });
  const w2 = C.withServer(C.sums(model(d2), true), t2, 1);
  assert.strictEqual(w2.net, null); assert.strictEqual(w2.fundingNoCarry, null);
  assert.strictEqual(s2.fundingMissing.length, 1); assert.strictEqual(s2.fundingMissing[0].id, nc.positionId);
  // без пропусков: группы по книгам — как byBook сервера (демо считает его как assemble); с пропусками — нетто «—»
  const d3 = JSON.parse(JSON.stringify(M.forwardCarryDemo({ horizon: '6M' })));
  d3.positions.forEach(p => { if (p.bookId === 'IC-BND') Object.assign(p, { funding: -1000, net: p.carry == null ? null : p.carry - 1000, fundingState: 'funded' }); });
  const srv = M.forwardCarryDemo({ horizon: '6M' }).byBook;
  C.groups(model(d3), 'book', true).forEach(g => {
    const b = g.key === 'IC-BND' ? null : srv[g.key];
    if (!b) return;
    if (b.carry == null) assert.strictEqual(g.sums.carry, null, g.key); else near(g.sums.carry, b.carry, g.key + ' кэрри');
    near(g.sums.funding, b.fundingTotal, g.key + ' фондирование');
    if (b.net == null) assert.strictEqual(g.sums.net, null, g.key + ' нетто null'); else near(g.sums.net, b.net, g.key + ' нетто');
  });
  C.groups(rows, 'book', true).filter(g => g.key === 'IC-BND').forEach(g => assert.strictEqual(g.sums.net, null, 'книга с null-фондированием'));
});

test('#297: ключи демо = ключи ответа сервера', () => {
  const d = M.forwardCarryDemo({});
  assert.deepStrictEqual(Object.keys(d.positions[0]).sort(), Object.keys(REAL297.positions[0]).sort());
  assert.deepStrictEqual(Object.keys(d.totals).sort(), Object.keys(REAL297.totals).sort());
  assert.deepStrictEqual(Object.keys(d.positions[0].inCcy).sort(), Object.keys(REAL297.positions[0].inCcy).sort());
  assert.deepStrictEqual(Object.keys(d.meetings[0]).sort(), Object.keys(REAL297.meetings[0]).sort());
});

test('#297: поля сервера, тождество нетто, итоги сервера и клиента совпадают', () => {
  const rows = model(REAL297), s = C.sums(rows, true), t = REAL297.totals;
  // dv01 и cs01 — из ответа, не из модели монитора
  const r = rows.find(x => x.id === 'T1|26238');
  near(r.dv01, REAL297.positions.find(x => x.positionId === 'T1|26238').dv01, 'dv01 из ответа');
  // клиентские итоги по правилу сервера = серверные
  near(s.carry, t.carry, 'кэрри'); near(s.fundingCovered, t.funding, 'funding — позиций с прогнозом');
  near(s.fundingNoCarry, t.fundingNoCarry, 'fundingNoCarry'); near(s.fundingCash, t.fundingBookLevel, 'fundingBookLevel');
  near(s.funding, t.fundingTotal, 'fundingTotal'); near(s.net, t.net, 'нетто'); near(s.netKnown, t.netKnown, 'netKnown');
  // тождество: Кэрри + Фонд(с прогнозом) + fundingNoCarry + fundingBookLevel = Нетто
  near(t.carry + t.funding + t.fundingNoCarry + t.fundingBookLevel, t.net, 'тождество сервера');
  const w = C.withServer(C.sums(rows, true), t, 2);
  assert.ok(w.server); near(w.funding, 2 * t.fundingTotal, 'итог сервера в валюте показа'); near(w.fundingRest, 2 * (t.fundingNoCarry + t.fundingBookLevel), 'без прогноза');
  // группы: книга без позиций с прогнозом — net null, как у сервера
  C.groups(rows, 'book', true).forEach(g => { const b = REAL297.byBook[g.key]; if (b.net == null) assert.strictEqual(g.sums.net, null, g.key); else near(g.sums.net, b.net, g.key); near(g.sums.funding, b.fundingTotal, g.key + ' fundingTotal'); });
  // заседания: на год вперёд, inHorizon — до горизонта
  assert.ok(REAL297.meetings.some(m => m.inHorizon === false) && REAL297.meetings.filter(m => m.inHorizon).every(m => m.effective <= REAL297.horizonDate));
});

test('#297: null при частичном фондировании, inCcy в своей валюте, демо в новом формате', () => {
  const d = JSON.parse(JSON.stringify(REAL297));
  const h = d.positions.find(x => x.positionId === 'H1|29008');
  Object.assign(h, { funding: null, net: null, fundingState: 'problem' });
  const s = C.sums(model(d), true);
  assert.strictEqual(s.net, null); assert.strictEqual(s.nFundingMissing, 1);
  near(s.netKnown, REAL297.totals.netKnown - (735422.0534714386 - 686030.1369863013), 'netKnown без позиции без фондирования');
  // позиция в валюте показа: суммы из inCcy, не пересчёт по курсу
  const u = JSON.parse(JSON.stringify(REAL297));
  const fx = u.positions.find(x => x.positionId === 'T1|26238');
  Object.assign(fx, { ccy: 'USD', inCcy: Object.assign({}, fx.inCcy, { carry: 2468, funding: -2375, net: 93 }) });
  const ru = C.rows(u, { k: 1 / 80, repr: 'USD' }).find(x => x.id === 'T1|26238');
  assert.strictEqual(ru.carry, 2468); assert.strictEqual(ru.funding, -2375);
  // демо — новый формат, тождество и B1 по всем группам
  const demo = M.forwardCarryDemo({});
  assert.ok(demo.meetings.some(m => m.inHorizon === false));
  [demo.totals, ...Object.values(demo.byBook)].forEach(g => {
    if (g.net != null) near(g.carry + (g.funding || 0) + (g.fundingNoCarry || 0) + (g.fundingBookLevel || 0), g.net, 'тождество демо');
    else assert.ok(g.nCoveredNoFunding || !g.nCovered || g.fundingTotal == null);
  });
  assert.strictEqual(demo.byBook['IC-BND'].net, null);
  // not_funded (фондирование 0) — не «неизвестно»: счётчик nNoCarryNoFunding его не считает
  assert.ok(demo.positions.some(p => p.carry == null && p.fundingState === 'not_funded' && p.funding === 0));
  assert.strictEqual(demo.totals.nNoCarryNoFunding, 0);
  assert.strictEqual(REAL.totals.nNoCarryNoFunding, 0);
});

test('итог колонок таблицы = итоги плиток: «Фондирование» = fundingTotal, «Нетто» по B1', () => {
  const colTot = (rows, fn) => rows.reduce((a, r) => a + fn(r), 0);
  // демо: у книги IC-BND позиции с прогнозом без фондирования — итог «Фондирования» число (= fundingTotal), нетто «—»
  const d = M.forwardCarryDemo({});
  const rows = model(d), S = C.sums(rows, true);
  assert.ok(S.nFundingMissing > 0);
  near(colTot(rows, C.colSum.funding), S.funding, 'итог колонки = S.funding');
  near(S.funding, d.totals.fundingTotal, 'S.funding = fundingTotal сервера');
  assert.ok(Number.isNaN(colTot(rows, C.colSum.net)) && S.net === null, 'нетто — «—»');
  // золотой ответ: всё известно — оба итога числа
  const rr = model(REAL), sr = C.sums(rr, true);
  near(colTot(rr, C.colSum.funding), REAL.totals.fundingTotal, 'фондирование'); near(colTot(rr, C.colSum.net), REAL.totals.net, 'нетто');
  // позиция без прогноза с null: итог «Фондирования» — «—» (NaN), как fundingTotal null
  const d2 = JSON.parse(JSON.stringify(REAL));
  Object.assign(d2.positions.find(p => p.positionId === 'D1|USD'), { funding: null, fundingState: 'problem' });
  const r2 = model(d2);
  assert.ok(Number.isNaN(colTot(r2, C.colSum.funding)) && C.sums(r2, true).funding === null);
});

test('нестрогий ответ сервера (main без nNoCarryNoFunding): строгое правило — на клиенте', () => {
  const d = JSON.parse(JSON.stringify(REAL));
  delete d.totals.nNoCarryNoFunding;
  Object.values(d.byBook).forEach(g => delete g.nNoCarryNoFunding);
  Object.assign(d.positions.find(p => p.positionId === 'D1|USD'), { funding: null, fundingState: 'problem' });
  // сервер из main считает null нулём: fundingNoCarry 0, net — число
  assert.ok(d.totals.net != null && d.totals.fundingNoCarry === 0);
  const w = C.withServer(C.sums(model(d), true), d.totals, 1);
  assert.strictEqual(w.nNoCarryNoFunding, 1);
  assert.strictEqual(w.net, null); assert.strictEqual(w.fundingNoCarry, null); assert.strictEqual(w.funding, null);
  near(w.netKnown, REAL.totals.netKnown, 'netKnown — частичная сумма');
});

test('позиций без прогноза нет — «позиции без прогноза» «—»; USD: итоги сервера и строки в пределах ε; noFilter', () => {
  const rows = model(REAL).filter(r => r.covered || r.isCash);
  const s = C.sums(rows, true);
  assert.strictEqual(s.nNotCovered, 0); assert.strictEqual(s.fundingNoCarry, null);
  near(s.funding, s.fundingCovered + s.fundingCash, 'всё фондирование без «без прогноза»');
  // USD: суммы строк по inCcy и рубли сервера × курс демо — совпадают до ε (в демо курс прогона = курс монитора)
  const d = M.forwardCarryDemo({}), k = 1 / M.fx.USD;
  const ru = C.rows(d, { pos: M.pos, k, books: M.book, repr: 'USD' }).concat(C.cashRows(d, { k, books: M.book }));
  const su = C.sums(ru, true), w = C.withServer(C.sums(ru, true), d.totals, k);
  near(su.carry, w.carry, 'кэрри USD', 1e-9); near(su.fundingCovered, w.fundingCovered, 'фондирование USD', 1e-9);
  assert.ok(C.noFilter({ entity: null, desk: null, books: [], ccy: null, portsOff: [] }) && !C.noFilter({ ccy: 'USD' }) && !C.noFilter({ books: ['OFZ'] }));
});
