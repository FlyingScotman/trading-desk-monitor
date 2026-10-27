// Правило B1 («не посчитано» — null и «—», а не 0) в мониторах (prototypes/concepts/), без браузера:
//  - #327: M.sum / M.sumX (lib/real.js, lib/mock.js) не превращают null в 0: все слагаемые null — null, часть — сумма
//    посчитанных и признак неполноты; K.wfTip(null) — «—», K.wfLabel(null) — ''; K.d по списку без значений — «—»;
//  - #322: изменение кривой, которого нет в CURVE_MOVES, — null (lib/real.js chg), «вчера» по нему не строится (K.yest),
//    Δ ОФЗ — Δ КБД прогона (K.ofzChg), «сдвигов нет» — K.noMoves;
//  - #325: K.cpnType — тип купона или null у позиции без купона; в фикстуре есть такая позиция с эмитентом.
//  - #333: K.d по строкам (rows), где ни одно значение не посчитано, — «—», а не «0»; часть посчитана — сумма посчитанных
//    и пометка «· без N из M поз.» (#335; подробно — partial_sums_335.test.js).
// Браузерная часть (страницы C01–C17) — b1_null_sums.pw.test.js.
//   node tests/js/b1_null_sums.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const tests = [];
const t = (name, f) => tests.push([name, f]);

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(opts) {
  opts = opts || {};
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open() {} send() { this.status = 404; } }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) {
    loadRun(ctx);
    if (opts.mutate) opts.mutate(ctx.window.CONCEPTS_RUN);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const plain = x => JSON.parse(JSON.stringify(x));

t('#327: M.sum и M.sumX — null не превращается в 0 (прогон и демо)', () => {
  for (const real of [true, false]) {
    const { M } = load({ real }), who = real ? 'прогон' : 'демо';
    const L = [{ a: 1, b: null }, { a: 2, b: null }, { a: null, b: null }];
    assert.strictEqual(M.sum(L, 'b'), null, who + ': ни одно не посчитано — null');
    assert.strictEqual(M.sum(L, 'a'), 3, who + ': часть посчитана — сумма посчитанных');
    // #335: признак неполноты — и какие слагаемые не посчитаны (missed, для подсказки «без N из M поз.»)
    assert.deepStrictEqual(plain(M.sumX(L, 'a')), { v: 3, n: 2, miss: 1, missed: [L[2]] }, who + ': признак неполноты');
    assert.deepStrictEqual(plain(M.sumX(L, x => x.b)), { v: null, n: 0, miss: 3, missed: L }, who);
    assert.strictEqual(M.sum([], 'a'), 0, who + ': пустой список — 0');
    assert.strictEqual(M.sum([{ a: 0 }, { a: null }], 'a'), 0, who + ': посчитанный 0 — это 0, не null');
    assert.strictEqual(M.sum([{ a: undefined }, { a: NaN }], 'a'), null, who + ': undefined и NaN — тоже «не посчитано»');
  }
});

t('#327: фондирование не посчитано ни у одной позиции — KPI и сумма фактора null, «—» в K.d; у части — сумма посчитанных', () => {
  const { M, K } = load({ real: true, mutate: D => D.positions.forEach(p => { if (p.pnl) p.pnl.funding = null; }) });
  assert.strictEqual(M.sum(M.positions, p => p.pnl.funding), null);
  assert.strictEqual(M.kpis().fundingDay, null, 'KPI фондирования за день');
  assert.ok(M.kpis().carryDay !== null, 'кэрри посчитано');
  const html = K.d({ title: 'PnL · фондирование', fmt: K.f.k, list: M.positions, val: p => p.pnl.funding });
  assert.ok(html.includes('—') && html.includes('data-dr') && !/>0</.test(html), html);
  // у одной позиции посчитано — число и признак неполноты
  const { M: M2 } = load({ real: true, mutate: D => D.positions.forEach((p, i) => { if (p.pnl) p.pnl.funding = i === 0 ? -1000 : null; }) });
  const x = M2.sumX(M2.positions, p => p.pnl.funding);
  assert.strictEqual(x.v, -1000); assert.strictEqual(x.n, 1); assert.strictEqual(x.miss, M2.positions.length - 1);
});

t('#333: K.d по строкам (rows) — все null: «—», а не «0»; часть посчитана — сумма посчитанных', () => {
  const { M, K } = load({ real: true });
  const p = M.positions[0];
  const html = K.d({ title: 'PnL', fmt: K.f.k, rows: [{ p, v: null }, { p, v: null }] });
  assert.ok(html.includes('—') && html.includes('data-dr') && !/>0</.test(html), html);
  // часть посчитана — сумма посчитанных и пометка «без 1 из 2 поз.» (#335); пометка — после числа
  const part = K.d({ title: 'PnL', fmt: K.f.k, rows: [{ p, v: 2000 }, { p, v: null }] });
  assert.ok(/>2<\/span> <span class="k-part"[^>]*>· без 1 из 2 поз\.<\/span>$/.test(part), 'часть посчитана — 2: ' + part);
  assert.ok(/>0<\/span> <span class="k-part"/.test(K.d({ title: 'PnL', fmt: K.f.k, rows: [{ p, v: 0 }, { p, v: null }] })), 'посчитанный 0 — это 0');
  assert.ok(/>2<\/span>$/.test(K.d({ title: 'PnL', fmt: K.f.k, rows: [{ p, v: 2000 }, { p, v: null }], part: false })), 'part: false — без пометки');
  assert.ok(K.d({ title: 'PnL', fmt: K.f.k, rows: [{ p, v: null }] }, '1.5').includes('>1.5<'), 'текст задан — он и остаётся');
});

t('#327: водопад — K.wfTip(null) «—», K.wfLabel(null) пустая; числа — как раньше', () => {
  const { K } = load({ real: true });
  assert.strictEqual(K.wfTip(null), '—');
  assert.strictEqual(K.wfTip(undefined), '—');
  assert.strictEqual(K.wfLabel(null), '');
  assert.strictEqual(K.wfLabel(null, 100, true), '', '«Итого» без значения — тоже без подписи');
  assert.strictEqual(K.wfTip(0), '0.000');
  assert.strictEqual(K.wfLabel(0, 5, true), '0.00', '«Итого», равное нулю, подписано');
  // #335: пометка — «без N из M поз.» (раньше «не посчитано у N из M поз.»), в подсказке графика — и список позиций
  assert.ok(K.missNote({ v: 5, n: 2, miss: 1, missed: [{ name: 'ОФЗ 26238' }] }).includes('без 1 из 3 поз.'));
  assert.ok(K.missNote({ v: 5, n: 2, miss: 1, missed: [{ name: 'ОФЗ 26238' }] }).includes('ОФЗ 26238'));
  assert.strictEqual(K.missNote({ v: null, n: 0, miss: 3 }), '', 'не посчитано ни у одной — «—» и так');
  assert.strictEqual(K.missNote({ v: 5, n: 3, miss: 0 }), '');
});

t('#327: форматтеры сумм (тыс, mm, bn, DV01) на null — «—», а не «0.0»; числа — как раньше', () => {
  const { K } = load({ real: true }), f = K.f;
  for (const [n, fn] of Object.entries({ k: f.k, ks: f.ks, mn: f.mn, mns: f.mns, bn: f.bn, dv: f.dv, dvs: f.dvs })) {
    assert.strictEqual(fn(null), '—', n + '(null)'); assert.strictEqual(fn(undefined), '—', n + '(undefined)');
  }
  assert.strictEqual(f.mns(null, 2), '—');
  assert.strictEqual(f.mn(1.5e6, 1), '1.5'); assert.strictEqual(f.mns(2.5e6, 2), '+2.50'); assert.strictEqual(f.k(0), '0'); assert.strictEqual(f.bn(3e9, 1), '3.0');
});

t('#322: кривой нет в CURVE_MOVES — изменение null, «вчера» нет, Δ ОФЗ — Δ КБД прогона', () => {
  const { M, K } = load({ real: true, mutate: D => { delete D.CURVE_MOVES['RUB OFZ']; } });
  assert.ok(M.ofzChg.every(v => v === null), 'Δ КБД: ' + M.ofzChg);
  assert.ok(K.ofzChg().every(v => v === null));
  assert.ok(M.basisCurve.chg.every(v => v === null), 'Δ базиса без Δ КБД — null: ' + M.basisCurve.chg);
  assert.ok(K.yest(M.ofzCurve, K.ofzChg()).every(v => v === null), '«ОФЗ вчера» — нет, а не копия КБД');
  assert.ok(K.noMoves(K.ofzChg()));
  assert.ok(M.curve['RUB RUONIA'].chg.every(v => typeof v === 'number'), 'RUONIA — свои сдвиги');
  // Δ КБД есть, Δ RUONIA нет: Δ ОФЗ — из прогона, а не RUONIA + базис
  const r = load({ real: true, mutate: D => { delete D.CURVE_MOVES['RUB RUONIA']; } }), mv = JSON.parse(require('./fixtures/concepts_run').TEXT).CURVE_MOVES;
  assert.deepStrictEqual(plain(r.K.ofzChg()), mv['RUB OFZ']);
  assert.ok(r.M.curve['RUB RUONIA'].chg.every(v => v === null) && r.M.basisCurve.chg.every(v => v === null));
  const y = r.K.yest(r.M.ofzCurve, r.K.ofzChg());
  y.forEach((v, k) => assert.ok(Math.abs(v - (r.M.ofzCurve[k] - mv['RUB OFZ'][k] / 100)) < 1e-9, 'ОФЗ вчера = КБД − Δ КБД/100: ' + k));
});

t('#322: CURVE_MOVES пуст — у всех кривых изменение null, у кривой без точек — тоже; PnL ставок не NaN', () => {
  const { M, K } = load({ real: true, mutate: D => { D.CURVE_MOVES = {}; } });
  M.curves.forEach(c => assert.ok(c.chg.every(v => v === null), c.id + ': ' + c.chg));
  assert.ok(K.noMoves(M.curve['RUB RUONIA'].chg) && K.noMoves(K.ofzChg()));
  assert.ok(K.yest(M.curve['RUB RUONIA'].pts, M.curve['RUB RUONIA'].chg).every(v => v === null));
  const p = M.positions.find(x => x.dv01);
  const rates = M.curves.reduce((s, c) => s + p.risk[c.id].reduce((a, v, k) => a + v * c.chg[k], 0), 0);
  assert.ok(Number.isFinite(rates), 'риск × null — 0, а не NaN');
  // в демо сдвиги есть всегда
  const d = load({ real: false });
  assert.ok(!d.K.noMoves(d.K.ofzChg()) && d.K.ofzChg().every(v => typeof v === 'number'));
});

t('#325: K.cpnType — тип купона или null; в фикстуре — позиция без купона (не облигация и не дериватив) с эмитентом', () => {
  const { M, K } = load({ real: true });
  const no = M.positions.filter(p => !p.cpn && p.group === 'OTH' && p.issuer);
  assert.ok(no.length, 'позиция без купона с эмитентом в фикстуре');
  assert.ok(no.every(p => K.cpnType(p) === null));
  assert.strictEqual(K.cpnType(null), null);
  assert.ok(M.positions.filter(p => p.cpn).every(p => K.cpnType(p) === p.cpn.type));
  // в prototypes/concepts/ нет p.cpn.type без проверки: только K.cpnType, «p.cpn && p.cpn.type» или под isBond (карточка)
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
  for (const f of fs.readdirSync(dir).filter(f => /^c\d\d-.*\.html$/.test(f))) {
    fs.readFileSync(path.join(dir, f), 'utf8').split('\n').forEach((line, i) => {
      if (/\b(p|r)\.cpn\.type/.test(line)) assert.ok(/(p|r)\.cpn && (p|r)\.cpn\.type/.test(line), f + ':' + (i + 1) + ': ' + line.trim().slice(0, 120));
    });
  }
});

for (const [name, f] of tests) { f(); n++; console.log('ok', name); }
console.log(n + ' tests passed');
