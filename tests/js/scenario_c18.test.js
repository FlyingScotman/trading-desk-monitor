// C18 «Сценарии» (#363, шаг 1 переноса Scenario Lab, #72): Тейлор на клиенте (prototypes/concepts/lib/scenario.js).
// - сверка с backend scenario/taylor.py на общей фикстуре backend/tests/fixtures/taylor_parity.json (её пишет pytest,
//   test_slice_s6.py: DESK_REGEN_FIXTURES=1), включая случай редактора (FX, спред ОФЗ, все, множитель, рейтинг с
//   корзиной «без рейтинга», сектор);
// - нормализация определения как scenario/definition.py; редактор ↔ определение; состояние в адресе;
// - правило B1: позиция без нужной чувствительности «не посчитана», в итоги не входит;
// - вклад по факторам для тепловой карты (#364): сумма ячеек кривых = кривые итога;
// - демо: риск из позиций lib/mock.js, пресеты демо; страница, галерея, загрузка с сервера.
//   node --test tests/js/scenario_c18.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const DIR = path.join(ROOT, 'prototypes', 'concepts');
const src = f => fs.readFileSync(path.join(DIR, f), 'utf8');
const S = require(path.join(DIR, 'lib', 'scenario.js'));
const FIX = JSON.parse(fs.readFileSync(path.join(ROOT, 'backend', 'tests', 'fixtures', 'taylor_parity.json'), 'utf8'));
const near = (a, b, msg, rel) => assert.ok(Math.abs(a - b) <= (rel || 1e-9) * Math.max(1, Math.abs(b)), `${msg}: ${a} vs ${b}`);
const clone = x => JSON.parse(JSON.stringify(x));

test('Тейлор: сверка с scenario/taylor.py на общей фикстуре (все случаи, по позициям и книгам)', () => {
  assert.ok(FIX.cases.length >= 10);
  assert.ok(FIX.cases.some(c => c.name === 'editor'), 'в фикстуре есть случай редактора C18');
  for (const c of FIX.cases) {
    const norm = S.normalize(c.definition, c.payload.curves);
    assert.strictEqual(S.canon(norm), S.canon(c.definition), c.name + ': нормализованная форма как у Python');
    const got = S.runTaylor(clone(c.payload), norm);
    assert.strictEqual(got.positions.length, c.expected.positions.length);
    c.expected.positions.forEach((e, i) => {
      const g = got.positions[i];
      assert.strictEqual(g.position_id, e.position_id);
      assert.strictEqual(g.second_order, e.second_order, c.name + ' ' + e.position_id);
      S.PARTS.concat(['max_move_bp']).forEach(k => near(g[k], e[k], `${c.name} ${e.position_id}.${k}`, FIX.tolerance_rel));
    });
    assert.deepStrictEqual(Object.keys(got.totals).sort(), Object.keys(c.expected.totals).sort());
    Object.entries(c.expected.totals).forEach(([b, e]) => S.PARTS.forEach(k => near(got.totals[b][k], e[k], `${c.name} totals.${b}.${k}`, FIX.tolerance_rel)));
  }
});

test('Тейлор: корзина «без рейтинга» (б/р, нет данных) и сектор «нет данных» — как own_spread_shock', () => {
  const c = FIX.cases.find(x => x.name === 'editor'), norm = S.normalize(c.definition);
  const s04 = c.payload.positions.find(p => p.position_id === 'S04');
  assert.strictEqual(s04.rating, 'б/р');
  assert.strictEqual(S.ownSpreadShock(norm, s04), 10 + 0.25 * 520 + 35 + 90);
  const s03 = c.payload.positions.find(p => p.position_id === 'S03');
  assert.strictEqual(S.ownSpreadShock(norm, s03), 10 + 20 + 60, 'spread_bp null: множитель добавляет 0');
});

test('normalize: ошибки и канонический вид, как scenario/definition.py', () => {
  assert.deepStrictEqual(S.normalize({ curves: { 'RUB OFZ': 50 }, spreads: { _mult: 1, _all: 0, rating: { A: 0 } }, fx: { USD: 0 } }),
    { curves: { 'RUB OFZ': new Array(11).fill(50) } });
  assert.deepStrictEqual(S.normalize({ ofzSpread: null, spreads: null, fx: null }), {});
  assert.throws(() => S.normalize({ shock: 1 }), /лишние ключи/);
  assert.throws(() => S.normalize({ spreads: { _mult: 0 } }), /_mult/);
  assert.throws(() => S.normalize({ spreads: { bad: 1 } }), /spreads: лишние/);
  assert.throws(() => S.normalize({ fx: { USD: -100 } }), /−100/);
  assert.throws(() => S.normalize({ curves: { 'RUB OFZ': [1, 2] } }), /11 бакетов/);
  assert.throws(() => S.normalize({ ofzSpread: 6000 }), /±5000/);
  assert.throws(() => S.normalize({ ofzSpread: true }), /конечное число/);
  assert.throws(() => S.normalize({ curves: { EUR: 10 } }, ['RUB OFZ', 'USD']), /не публикует/);
  assert.ok(S.normalize({ curves: { 'RUB KEY': 10 } }, ['USD']).curves['RUB KEY'], 'RUB_CURVES — у любого прогона');
  assert.deepStrictEqual(S.normalize({ basis: { CNY: 10, USD: 0 } }), { basis: { CNY: 10 } });
});

test('редактор: «без рейтинга» под обоими ключами, пресет → редактор → то же определение', () => {
  const ed = { fx: { USD: 10, CNY: 0 }, ofz: 5, all: 20, mult: 1.5, rating: { BB: 40, _none: 90 }, sector: { 'Лизинг': 30 } };
  assert.deepStrictEqual(S.editorDef(ed, null), {
    fx: { USD: 10 }, ofzSpread: 5, spreads: { _all: 20, _mult: 1.5, rating: { BB: 40, 'б/р': 90, 'нет данных': 90 }, sector: { 'Лизинг': 30 } } });
  assert.deepStrictEqual(S.editorDef(S.emptyEditor(), null), {}, 'пустой редактор — пустой сценарий');
  // кривые, базис и эмитенты — из пресета base как есть; FX и спреды — из полей
  const defs = S.DEMO_PRESETS.map(p => p.definition).concat(FIX.cases.map(c => c.definition), [{ curves: { USD: 5 }, basis: { CNY: 15 }, spreads: { issuer: { X: 10 }, rating: { 'б/р': 70, 'нет данных': 70 } } }]);
  defs.forEach(d => {
    const e = S.editorFrom('p', d);
    assert.strictEqual(S.canon(S.normalize(S.editorDef(e, d))), S.canon(S.normalize(d)), JSON.stringify(d));
  });
  const e = S.editorFrom('p', { spreads: { rating: { 'б/р': 70 } } });
  assert.deepStrictEqual(e.rating, { _none: 70 });
});

test('состояние в адресе: туда и обратно, битое — null', () => {
  const ed = { base: 'stress22', fx: { USD: 12.5 }, ofz: 10, all: 0, mult: 2, rating: { _none: 50 }, sector: { 'Нефть и газ': -10 } };
  assert.deepStrictEqual(S.decode(S.encode(ed)), S.cleanEditor(ed));
  assert.ok(!/[#&\s]/.test(S.encode(ed)), 'в адресе без # и &');
  ['', '%7B', 'null', '[1]', '%E0%A4%A'].forEach(s => assert.strictEqual(S.decode(s), null, s));
  assert.deepStrictEqual(S.cleanEditor({ fx: { USD: 'x', EUR: 0, CNY: '5' }, mult: -1 }).fx, { CNY: 5 });
  assert.strictEqual(S.cleanEditor({ mult: -1 }).mult, 1);
  assert.strictEqual(S.fxAfter(80, 15), 92);
  assert.strictEqual(S.fxAfter(null, 15), null);
});

// синтетический риск: OFZ, кредит, валютная облигация, своп, облигация без CS01 и без FX-дельты
const P = {
  buckets: S.BUCKETS, curves: ['RUB OFZ', 'RUB RUONIA', 'USD'],
  factors: ['IR|RUB OFZ|5Y', 'IR|RUB RUONIA|2Y', 'IR|USD|3Y', 'CS', 'FX|USD'],
  positions: [
    { position_id: 'ofz', book_id: 'A', spread_kind: 'ofz', spread_bp: 20, rating: null, sector: null, issuer: 'MF', delta: [[0, -100], [3, -10]], gamma: [[0, 0, 0.5]] },
    { position_id: 'corp', book_id: 'A', spread_kind: 'credit', spread_bp: 300, rating: 'BB', sector: 'Лизинг', issuer: 'L', delta: [[1, -50], [3, -40]], gamma: [] },
    { position_id: 'usd', book_id: 'B', spread_kind: 'credit', spread_bp: 250, rating: 'б/р', sector: 'нет данных', issuer: 'U', delta: [[2, -30], [3, -20], [4, 1000]], gamma: [] },
    { position_id: 'swap', book_id: 'B', spread_kind: null, spread_bp: null, rating: null, sector: null, issuer: null, delta: [[1, 70]], gamma: [] },
    { position_id: 'nocs', book_id: 'B', spread_kind: null, spread_bp: null, rating: 'A', sector: null, issuer: 'N', delta: [[1, -5]], gamma: [] },
    { position_id: 'cny', book_id: 'C', spread_kind: 'credit', spread_bp: 100, rating: 'A', sector: null, issuer: 'C', delta: [[1, -8], [3, -2]], gamma: [] },
  ],
};
const INFO = {
  ofz: { book: 'A', group: 'GOV', ccy: 'RUB' }, corp: { book: 'A', group: 'CORP', ccy: 'RUB' }, usd: { book: 'B', group: 'CORP', ccy: 'USD', fxDeltaBy: { USD: 12 } },
  swap: { book: 'B', group: 'DER', ccy: 'RUB' }, nocs: { book: 'B', group: 'CORP', ccy: 'RUB' }, cny: { book: 'C', group: 'CORP', ccy: 'CNY', fxDeltaBy: { CNY: 5 } },
  nv: { book: 'C', group: 'CORP', ccy: 'RUB', notValued: true },
};
const IDS = ['ofz', 'corp', 'usd', 'swap', 'nocs', 'cny', 'nv'];
const ev = def => S.evaluate(clone(P), S.normalize(def), { ids: IDS, info: id => INFO[id] || null });

test('B1: позиция без нужной чувствительности не посчитана и в итоги не входит', () => {
  // только кривые: не оценённая — не посчитана; у облигаций риск есть
  let r = ev({ curves: { 'RUB RUONIA': 10 } });
  assert.deepStrictEqual(r.missing.map(x => [x.id, x.why.code]), [['nv', 'not_valued']]);
  assert.deepStrictEqual([r.total.n, r.total.miss], [6, 1]);
  assert.deepStrictEqual([r.byBook.C.n, r.byBook.C.miss], [1, 1]);
  // спред: облигация без CS01 при ненулевом кредитном шоке не посчитана; своп — не облигация, посчитан (0)
  r = ev({ spreads: { _all: 50 } });
  assert.deepStrictEqual(r.missing.map(x => [x.id, x.why.code]).sort(), [['nocs', 'no_spread'], ['nv', 'not_valued']]);
  assert.strictEqual(r.total.spread, -40 * 50 + -20 * 50 + -2 * 50, 'итог — только посчитанные');
  // шок ОФЗ к кредитной облигации без CS01 не относится — она посчитана
  r = ev({ ofzSpread: 10 });
  assert.deepStrictEqual(r.missing.map(x => x.id), ['nv']);
  // FX: облигация в CNY без фактора FX|CNY при шоке CNY — не посчитана; без шока CNY — посчитана
  r = ev({ fx: { CNY: 10, USD: 5 } });
  assert.deepStrictEqual(r.missing.map(x => [x.id, x.why.code]).sort(), [['cny', 'no_fx'], ['nv', 'not_valued']]);
  assert.strictEqual(r.total.fx, 1000 * 5);
  assert.strictEqual(ev({ fx: { USD: 5 } }).missing.length, 1);
  // B: посчитаны все три, A — обе, C: обе не посчитаны → итог книги null на экране (n = 0)
  assert.deepStrictEqual([r.byBook.C.n, r.byBook.C.miss], [0, 2]);
  // пустой сценарий: ничего не «не посчитано»
  r = ev({});
  assert.strictEqual(r.total.miss, 0);
  assert.strictEqual(r.total.total, 0);
  // облигация без ставочного риска при сдвиге кривых
  const p2 = clone(P); p2.positions[1].delta = [[3, -40]];
  const r2 = S.evaluate(p2, S.normalize({ curves: { USD: 5 } }), { ids: ['corp', 'swap'], info: id => INFO[id] });
  assert.deepStrictEqual(r2.missing.map(x => [x.id, x.why.code]), [['corp', 'no_rates']]);
});

test('итоги = сумма посчитанных позиций; вклад по факторам (#364): ячейки кривых = кривые итога', () => {
  const def = { curves: { 'RUB OFZ': 30, 'RUB RUONIA': [0, 0, 0, 0, 25, 0, 0, 0, 0, 0, 0], USD: 15 }, ofzSpread: 5, spreads: { _all: 10, _mult: 1.5 }, fx: { USD: 8 } };
  const r = ev(def), counted = r.rows.filter(x => x.parts);
  S.PARTS.forEach(k => near(r.total[k], counted.reduce((a, x) => a + x.parts[k], 0), k));
  Object.keys(r.byBook).forEach(b => near(r.byBook[b].total, counted.filter(x => x.book === b).reduce((a, x) => a + x.parts.total, 0), b));
  const cells = Object.values(r.cells).reduce((a, v) => a + v.reduce((s, x) => s + x, 0), 0);
  near(cells, r.total.rates, 'ячейки кривая × бакет');
  near(r.spreadOfz + r.spreadCredit, r.total.spread, 'спреды ОФЗ + кредит');
  near(Object.values(r.fxBy).reduce((a, v) => a + v, 0), r.total.fx, 'FX по валютам');
  assert.deepStrictEqual(Object.keys(r.cells).sort(), ['RUB OFZ', 'RUB RUONIA', 'USD']);
  assert.strictEqual(r.cells['RUB RUONIA'][4], (-50 + 70 - 8) * 25, 'облигация без CS01 (не посчитана) в ячейки не входит');
  // топ по убытку: худшие первыми, только убыточные
  const w = S.worst(r, 3);
  assert.ok(w.every((x, i) => x.parts.total < 0 && (!i || w[i - 1].parts.total <= x.parts.total)));
  // на фикстуре Python: та же сумма ячеек
  FIX.cases.forEach(c => { const rr = S.evaluate(clone(c.payload), S.normalize(c.definition)); near(Object.values(rr.cells).reduce((a, v) => a + v.reduce((s, x) => s + x, 0), 0), rr.total.rates, c.name, 1e-9); });
});

test('корзины редактора: грейды по шкале, «без рейтинга» последней, CS01 корзин', () => {
  const RANK = ['AAA', 'AA', 'A', 'BBB', 'BB'];
  const b = S.buckets(clone(P), null, r => { const i = RANK.indexOf(r); return i < 0 ? 99 : i; });
  assert.deepStrictEqual(b.ratings, ['A', 'BB', '_none']);
  assert.deepStrictEqual(b.rating._none, { cs01: -20, n: 1 });
  assert.deepStrictEqual(b.sectors, ['Лизинг', 'нет данных']);
  assert.strictEqual(b.ofzCs01, -10);
  assert.strictEqual(b.creditCs01, -62);
  assert.deepStrictEqual(b.fxCcys, ['USD']);
  assert.strictEqual(b.fxDelta.USD, 1000);
  const keep = S.buckets(clone(P), id => id !== 'usd');
  assert.ok(!keep.rating._none, 'фильтр шапки: позиции вне экрана не входят');
});

test('демо: риск из позиций lib/mock.js той же формы, пресеты демо считаются', () => {
  const ctx = { document: { write() { } }, location: { protocol: 'file:' }, console, URLSearchParams };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(src('lib/mock.js'), ctx);
  const M = ctx.MOCK, p = S.demoPayload(M, 1);
  assert.ok(p.positions.length > 20 && p.factors.includes('CS') && p.factors.some(f => f.startsWith('FX|')) && p.factors.some(f => f.startsWith('IR|')));
  p.positions.forEach(x => x.delta.forEach(([i, d]) => assert.ok(i >= 0 && i < p.factors.length && isFinite(d) && d !== 0)));
  assert.ok(p.positions.some(x => x.spread_kind === 'ofz') && p.positions.some(x => x.spread_kind === 'credit'));
  // курс показа: payload в рублях при любой валюте показа
  const p2 = S.demoPayload(M, 0.5);
  near(p2.positions[0].delta[0][1], p.positions[0].delta[0][1] * 2, 'деление на курс показа');
  const ids = M.positions.map(x => x.id);
  S.DEMO_PRESETS.forEach(pr => {
    const r = S.evaluate(p, S.normalize(pr.definition, p.curves), { ids, info: id => M.pos[id] });
    assert.ok(isFinite(r.total.total) && r.total.n > 0, pr.scenario_id);
    assert.strictEqual(r.total.n + r.total.miss, ids.length, pr.scenario_id + ': каждая позиция посчитана или нет');
  });
});

test('страница C18: загрузка риска и пресетов с сервера, галерея, место для #335 и структура для #364', () => {
  const html = src('c18-scenarios.html');
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(m => m[1]);
  assert.ok(scripts.indexOf('lib/kit.js') < scripts.indexOf('lib/instr.js') && scripts.indexOf('lib/instr.js') < scripts.indexOf('lib/scenario.js'));
  assert.ok(html.includes("const server = K.source === 'real'"));
  assert.ok(html.includes('S.load(S.riskUrl(M.run.id))') && html.includes('S.load(S.defsUrl())'));
  assert.strictEqual(S.riskUrl(7), '../api/v1/runs/7/risk');
  assert.strictEqual(S.defsUrl(), '../api/v1/scenario-defs');
  assert.ok(/#335/.test(html) && html.includes('const missMark = '), 'одно место для пометки неполного итога (#335)');
  assert.ok(html.includes('window.C18 = {'), 'состояние и результат сценария для тепловой карты (#364)');
  // состояние — в адресе и localStorage, оба в try/catch
  assert.ok(/try \{ localStorage\.setItem\('c18-state'/.test(html) && /try \{ history\.replaceState/.test(html));
  assert.ok(src('index.html').includes("['c18-scenarios', 'C18', 'Сценарии'"));
  assert.ok(fs.existsSync(path.join(DIR, 'thumbs', 'c18-scenarios.jpg')));
});

test('загрузка: ответ с ошибкой — Error со статусом и текстом сервера', async () => {
  const fake = (status, body) => () => Promise.resolve({ ok: status < 400, status, json: () => Promise.resolve(JSON.parse(body)), text: () => Promise.resolve(body) });
  assert.deepStrictEqual(await S.load('x', { fetch: fake(200, '{"a":1}') }), { a: 1 });
  await assert.rejects(S.load('x', { fetch: fake(404, '{"error":{"message":"run 9 not found"}}') }), e => e.status === 404 && e.text === 'run 9 not found');
});
