// FX-форварды в мониторах (prototypes/concepts/, пакеты UI F1–F4 gap-анализа FX-форвардов): FX-дельта по валютам
// (fx_delta {ccy}) доходит до страниц, DV01 и ОВП по валюте — по валюте кривой и ключам FX-дельты («по ногам»,
// lib/instr.js), кривые блоков валют кокпита — из данных прогона, ноги FX-сделки (extra.legs) — в карточку.
//   node tests/js/fx_forward_ui.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);
const NB = 11, at = (k, v) => Array.from({ length: NB }, (_, i) => i === k ? v : 0);

function ctxWith(src) {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  src(ctx);
  const M = ctx.window.MOCK;
  M.toRepr = {}; Object.keys(M.fx).forEach(c => M.toRepr[c] = M.fx[c]);  // как kit.js в рублях
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M, I: ctx.window.INSTR };
}
// прогон (tests/js/fixtures/concepts-run.json через lib/real.js) с FX-сделками форвардного вида: кросс USD/CNY без рубля и FX-своп на
// implied-кривой юаня, которой нет в старом списке кривых кокпита
function real(mutate) {
  return ctxWith(ctx => {
    loadRun(ctx);
    const D = ctx.window.CONCEPTS_RUN, b = D.positions.find(p => p.instrType === 'FX');
    D.CURVES['CNY FWD'] = { name: 'CNY implied (CETS)', ccy: 'CNY', kind: 'swap', pts: new Array(NB).fill(5.5) };
    D.positions.push(Object.assign({}, b, { id: 'X1', name: 'USD/CNY fwd 3M', instrType: 'FX', ccy: 'USD', notional: 10e6, mvRub: 1e6,
      risk: { 'USD SOFR': at(0, -800), CNY: at(0, 790) }, fxDelta: { USD: 9.9e6, CNY: -70.5e6 },
      extra: { legs: [{ leg: 'single', value_date: '2027-01-21', buy_ccy: 'USD', buy_amount: 10e6, sell_ccy: 'CNY', sell_amount: 71.2e6, contract_rate: 7.12, fwd_rate: 7.16, settlement: 'deliverable' }] } }));
    D.positions.push(Object.assign({}, b, { id: 'X2', name: 'CNY/RUB swap TOM/1M', instrType: 'FX', ccy: 'CNY', notional: -300e6, mvRub: 2e6,
      risk: { 'CNY FWD': at(0, 2300), 'RUB RUONIA': at(0, -2330) }, fxDelta: { CNY: 1.4e6 },
      extra: { legs: [{ leg: 'near', value_date: '2026-10-15', buy_ccy: 'CNY', buy_amount: 300e6, sell_ccy: 'RUB', sell_amount: 3426e6, contract_rate: 11.42 },
        { leg: 'far', value_date: '2026-11-14', buy_ccy: 'RUB', buy_amount: 3459e6, sell_ccy: 'CNY', sell_amount: 300e6, contract_rate: 11.53, fixing_date: null }] } }));
    if (mutate) mutate(D);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  });
}
const demo = () => ctxWith(ctx => vm.runInContext(P('concepts/lib/mock.js'), ctx));
const rateCurves = (M, c) => M.curves.filter(x => x.ccy === c && x.kind === 'rate').map(x => x.id);

t('прогон: FX-дельта по валютам не сворачивается; p.fxDelta — в валюте позиции; ноги — в карточку', () => {
  const { M } = real();
  const x = M.pos.X1;
  assert.deepStrictEqual(JSON.parse(JSON.stringify(x.fxDeltaBy)), { USD: 9.9e6, CNY: -70.5e6 });
  assert.strictEqual(x.fxDelta, 9.9e6, 'не сумма разных валют');
  assert.strictEqual(x.legs.length, 1);
  assert.strictEqual(x.legs[0].buyCcy, 'USD'); assert.strictEqual(x.legs[0].contractRate, 7.12); assert.strictEqual(x.legs[0].fwdRate, 7.16);
  assert.strictEqual(M.pos.X2.legs.map(l => l.leg).join(','), 'near,far');
  assert.strictEqual(M.positions.filter(p => p.legs).length, 2, 'ног нет — null');
});

t('прогон: DV01 и ОВП по валюте — по кривой и ключам FX-дельты (по ногам), как в таблице «валюта × тип»', () => {
  const { M, I } = real();
  const k = M.kpis(), T = I.table(M.positions, { attribution: 'legs', units: 'repr' });
  for (const c of M.ccys) {
    const want = M.positions.reduce((s, p) => s + rateCurves(M, c).reduce((a, id) => a + (p.dv01ByCurve[id] || 0), 0), 0);
    near(k.byCcy[c].dv01, want, 'DV01 ' + c + ' по кривым');
    const blk = T.blocks.find(b => b.ccy === c);
    if (blk) near(k.byCcy[c].dv01, blk.total.sum('dv01'), 'шапка = таблица ' + c);
    near(k.byCcy[c].fxDelta, M.positions.reduce((s, p) => s + ((p.fxDeltaBy || {})[c] || 0), 0), 'ОВП ' + c);
  }
  // рублёвая нога FX-свопа — в рублях, а не в юанях; юаневая нога кросса — в юанях, хотя позиция долларовая
  assert.ok(I.dv01In(M.pos.X2, 'RUB') < 0 && I.dv01In(M.pos.X2, 'CNY') > 0);
  assert.strictEqual(I.fxIn(M.pos.X1, 'CNY'), -70.5e6);
  assert.strictEqual(I.ccysOf(M.pos.X1).sort().join(','), 'CNY,USD');
});

t('прогон: кривые — из данных (implied CNY в блоке юаня, точки USD SOFR и CNY из прогона, CHF — своя валюта)', () => {
  const { M } = real();
  assert.strictEqual(M.curve['CNY FWD'].ccy, 'CNY');
  assert.ok(rateCurves(M, 'CNY').includes('CNY XCCY') && rateCurves(M, 'CNY').includes('CNY FWD'));
  assert.strictEqual(M.curve['CHF SARON'].ccy, 'CHF');
  assert.ok(M.curve['USD SOFR'].pts.every(v => v != null), 'кривая прогона с точками — не «кривой нет»');
  assert.strictEqual(M.curve['CNY XCCY'].pts[0], 5.4, 'точки кривой «CNY» прогона');
  // без кривой в прогоне — заглушка по дюрации, как было
  const { M: M2 } = real(D => { delete D.CURVES['EUR ESTR']; });
  assert.ok(/кривой нет/.test(M2.curve['EUR ESTR'].name) && M2.curve['EUR ESTR'].pts.every(v => v == null));
  // кокпит: блоки валют — по curve.ccy, без жёсткого списка кривых
  const c01 = P('concepts/c01-cockpit.html');
  assert.ok(!c01.includes("['CNY XCCY']") && c01.includes('M.curves.filter(x => x.ccy === c)'));
});

t('прогон: вторая валюта кросса — в валютах деска (ОВП и курс)', () => {
  const { M } = real(D => { D.positions = D.positions.filter(p => p.ccy !== 'CNY' || p.id === 'X2'); D.positions.find(p => p.id === 'X2').ccy = 'USD'; delete D.FX.CNY; });
  assert.ok(M.ccys.includes('CNY'), JSON.stringify(M.ccys));
  assert.strictEqual(M.fx.CNY, 1, 'нет курса — 1 и пометка');
  assert.ok(M.notes.some(x => /нет курса CNY/.test(x)));
});

t('демо: FX-форвард, NDF, кросс, своп и CHF — риск в двух валютах, FX-дельта по валютам, ноги', () => {
  const { M, I } = demo();
  const fx = M.positions.filter(p => p.kind === 'FXF' && p.productGroup !== 'fx_future');
  assert.ok(fx.length >= 6 && fx.every(p => p.legs && p.legs.length));
  const cross = fx.find(p => /USD\/CNY/.test(p.name)), ndf = fx.find(p => p.productGroup === 'fx_ndf'), swap = fx.find(p => p.legs.length === 2), chf = fx.find(p => p.ccy === 'CHF');
  assert.ok(cross.fxDeltaBy.USD > 0 && cross.fxDeltaBy.CNY < 0, 'кросс: FX-дельта в двух валютах');
  assert.ok(I.dv01In(cross, 'USD') && I.dv01In(cross, 'CNY') && !I.dv01In(cross, 'RUB'), 'кросс: риск на двух иностранных кривых');
  assert.ok(I.dv01In(ndf, 'USD') && I.dv01In(ndf, 'RUB'), 'NDF: иностранная кривая и RUONIA');
  assert.strictEqual(ndf.legs[0].settlement, 'cash'); assert.ok(ndf.legs[0].fixingDate && ndf.legs[0].paymentDate);
  assert.ok(swap.dv01ByCurve['CNY FWD'] && Math.abs(swap.fxDeltaBy.CNY) < 0.02 * Math.abs(swap.notional), 'своп: implied-кривая, ноги гасят FX-дельту');
  assert.ok(chf.dv01ByCurve['CHF SARON'] && M.ccys.includes('CHF'));
  const k = M.kpis();
  near(k.byCcy.CNY.fxDelta, M.positions.reduce((s, p) => s + ((p.fxDeltaBy || {}).CNY || 0), 0), 'ОВП CNY с ногой кросса');
  near(k.byCcy.RUB.dv01, M.positions.reduce((s, p) => s + rateCurves(M, 'RUB').reduce((a, id) => a + p.dv01ByCurve[id], 0), 0), 'DV01 RUB по кривым');
  // таблица «валюта × тип»: юаневая нога кросса — в блоке юаня, CHF — свой блок
  const T = I.table(M.positions, { attribution: 'legs', units: 'own' });
  assert.ok(T.blocks.find(b => b.ccy === 'CNY').total.legs.has(cross.id));
  assert.ok(T.ccys.includes('CHF') && T.curves.includes('CNY FWD'));
  // PnL FX — по всем валютам FX-дельты
  near(cross.pnl.fx, Object.entries(cross.fxDeltaBy).reduce((s, [c, a]) => s + a * M.fx[c] * M.fxChgPct[c] / 100, 0), 'PnL FX кросса');
});

console.log(n + ' tests passed');
