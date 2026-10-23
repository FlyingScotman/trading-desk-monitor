// Фондирование, два базиса FX, спред-шоки и сценарии кэрри (prototypes/data.js + desk-util.js) по
// конвенциям docs/ORG_CONVENTIONS.md.   node tests/js/funding_carry.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

const ctx = { window: {}, URLSearchParams };
vm.createContext(ctx);
vm.runInContext(P('data.js'), ctx);
vm.runInContext(P('desk-util.js'), ctx);
const D = ctx.window.DESK, U = D.util;
const by = n => D.positions.find(p => p.name === n);
const near = (a, b, eps, msg) => assert.ok(Math.abs(a - b) <= eps, `${msg}: ${a} vs ${b}`);
const sum = (a, f) => a.reduce((s, x) => s + f(x), 0);
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

t('облигация торговой книги фондируется на чистую MV в своей валюте по ставке валюты', () => {
  const p = by('Газпром ЗО27-1-Д'), f = D.funding.find(x => x.positionId === p.id);
  assert.strictEqual(f.kind, 'POS'); assert.strictEqual(f.ccy, 'USD'); assert.strictEqual(f.rate, 6);
  near(f.amount, -(p.mv - p.notional * p.accrued / 100), 1e-6, 'тело = −чистая MV');
  const r = D.funding.find(x => x.positionId === by('ОФЗ 26242').id);
  near(r.rate, D.INDICES['КС'] + 0.30, 1e-9, 'RUB: КС + спред деска');
  near(D.funding.find(x => x.positionId === by('Норникель БО-001Р-08-CNY').id).rate, D.INDICES['RUSFAR CNY'] + 0.60, 1e-9, 'CNY');
  near(D.funding.find(x => x.positionId === by('Европлан Авто ABS 2025-1').id).rate, D.INDICES['КС'] + 0.50, 1e-9, 'особая ставка книги');
  near(D.funding.find(x => x.positionId === by('ИЦБ ДОМ.РФ 2Р-12R').id).rate, D.INDICES['КС'] + 0.20, 1e-9, 'юрлицо × бумага важнее книги');
  // шорт размещает деньги: тело фондирования > 0, кэрри > 0
  const s = D.funding.find(x => x.positionId === by('ОФЗ 26246').id);
  assert.ok(s.amount > 0 && s.pnl.carry > 0);
});

t('процент по фондированию — в RUB по курсу ЦБ, в PnL самой бумаги', () => {
  const p = by('Газпром ЗО27-1-Д'), f = D.funding.find(x => x.positionId === p.id);
  near(f.pnl.carry, f.amount * D.FX_CBR.USD * 6 / 100 / 365 * D.accrualDays, 1e-6, 'кэрри фондирования');
  near(p.pnl.funding, f.pnl.carry, 1e-9, 'компонента funding');
  near(p.pnl.fxFunding, f.pnl.fxGross, 1e-9, 'переоценка тела — в fx бумаги');
  assert.ok(Math.abs(p.pnl.fx) < Math.abs(p.pnl.fxGross) * 0.05, 'FX бумаги почти гасится своим фондированием');
});

t('ОВП книг облигаций после вечерней передачи почти нулевая, позиция — в книге деривативов', () => {
  const ocp = {};
  D.positions.concat(D.funding).forEach(x => Object.keys(x.fxDelta || {}).forEach(c => { const k = x.book + '|' + c; ocp[k] = (ocp[k] || 0) + x.fxDelta[c]; }));
  Object.keys(D.BOOKS).filter(b => D.BOOKS[b].ocpTo).forEach(b => Object.keys(ocp).filter(k => k.startsWith(b + '|')).forEach(k => {
    assert.ok(Math.abs(ocp[k]) <= 0.05e6 + 1, `${k}: остаток ${ocp[k]}`);
  }));
  const tr = D.OCP_TRANSFERS[0];
  const legs = D.positions.filter(p => p.ocpTransfer && p.ccy === tr.ccy && (p.book === tr.from || p.book === tr.to) && Math.abs(p.notional) === tr.amount);
  assert.strictEqual(legs.length, 2);
  near(sum(legs, p => p.notional), 0, 1e-9, 'ноги передачи гасятся на деске');
  assert.strictEqual(legs[0].price, D.FX_CBR_NEXT[tr.ccy]);
  assert.ok(legs.every(p => p.pnl.explained === 0), 'сделка дня по курсу ЦБ на завтра — без PnL дня');
});

t('книга деривативов фондируется по остаткам ДС по O/N-ставке валюты', () => {
  const c = D.funding.filter(f => f.kind === 'CASH');
  assert.ok(c.length >= 3);
  c.forEach(f => {
    assert.strictEqual(D.BOOKS[f.book].bookType, 'DERIVS');
    const ix = { RUB: 'RUONIA', CNY: 'RUSFAR CNY', USD: 'SOFR', EUR: '€STR', CHF: 'SARON' }[f.ccy];
    near(f.rate, D.INDICES[ix], 1e-9, f.book + ' ' + f.ccy);
    near(f.pnl.carry, f.amount * D.FX[f.ccy] * f.rate / 100 / f.dayBasis * D.accrualDays, 1e-6, 'кэрри остатка');
  });
  near(D.fundingPnl.carry, sum(D.funding.filter(f => !f.positionId), f => f.pnl.carry), 1e-6, 'фондирование уровня книги — без строк бумаг');
});

t('HTM: начисление минус контрактное фондирование, вне DV01/CS01', () => {
  D.positions.filter(p => p.book === 'HTM').forEach(p => {
    assert.strictEqual(p.dv01, 0); assert.strictEqual(p.cs01, 0); assert.ok(p.accrualOnly);
    const f = D.funding.find(x => x.positionId === p.id);
    assert.strictEqual(f.kind, 'HTM'); assert.strictEqual(f.rate, p.contractRate);
    near(f.amount, -p.notional * p.purchasePrice / 100, 1e-6, 'тело — цена покупки');
    near(p.pnl.explained, p.pnl.carry + p.pnl.funding, 1e-6, 'только начисление');
  });
});

t('два базиса FX: official сверяется с explainedCbr', () => {
  D.positions.forEach(p => {
    const e = p.pnl;
    near(e.explainedCbr - e.explained, e.fxBasis, 1e-6, p.name + ' fxBasis');
    near(e.official - e.explainedCbr, e.unexplained, 1e-6, p.name + ' unexplained');
    const comps = ['carry', 'rolldown', 'rates', 'spread', 'fx', 'funding', 'convexity', 'newTrades', 'other'];
    near(sum(comps, k => e[k]), e.explained, 1e-6, p.name + ' explained = Σ компонент');
  });
  const p = by('Лукойл ЗО30-Д');
  assert.ok(Math.abs(p.pnl.fxBasis) > 0, 'у валютной бумаги базис курсов ненулевой');
});

t('спред-шоки: рейтинг + сектор + эмитент, суверен без «35 %»', () => {
  const sc = { spreads: { _all: 10, rating: { A: 50 }, sector: { 'Девелопмент': 300 }, issuer: { 'Самолёт': 100 } } };
  assert.strictEqual(U.spreadShock(sc, by('Самолёт БО-П15')), 460);
  assert.strictEqual(U.spreadShock(sc, by('РФ ЗО 2030-Д')), 10);
  assert.strictEqual(U.spreadShock({ spreads: { AA: 7, 'Сегежа': 3 } }, by('Сегежа 003Р-02R')), 3);   // старый формат
});

t('сценарий: спред ОФЗ к RUONIA двигает только ОФЗ (ofzSpr01)', () => {
  const r = U.runScenario({ id: 'x', ofzSpread: 10 });
  const pos = id => r.byPosition.find(x => x.id === id);
  const o = by('ОФЗ 26242'), c = by('Роснефть 002Р-13');
  near(pos(o.id).spread, o.ofzSpr01 * 10, 1e-6, 'ОФЗ');
  assert.ok(pos(c.id).spread === 0, 'корпорат не двигается');
  assert.ok(pos(o.id).convexity > 0, 'второй порядок по спреду ОФЗ');
});

t('сценарий FX: облигация на своём валютном фондировании почти не даёт FX', () => {
  const r = U.runScenario({ id: 'x', fx: { USD: 20 } });
  const p = by('Лукойл ЗО30-Д'), x = r.byPosition.find(y => y.id === p.id);
  assert.ok(Math.abs(x.fx) < Math.abs(x.fxGross) * 0.05, `fx ${x.fx} vs gross ${x.fxGross}`);
  near(r.fundingFx, sum(D.funding.filter(f => !f.positionId), f => (f.fxDelta.USD || 0) * D.FX.USD * 0.2), 1e-3, 'остаток — только строки книги');
});

t('кэрри: КС без изменений — доход и фондирование бумаги за горизонт', () => {
  const r = U.runCarry({ ks: { mode: 'flat' } }, { horizon: '3M' });
  const p = by('ОФЗ 26242'), f = D.funding.find(x => x.positionId === p.id), x = r.byPosition[p.id];
  near(x.income, p.mvRub * p.ytm / 100 / 365 * r.days, 1e-3, 'доход');
  near(x.funding, f.pnl.carry / D.accrualDays * r.days, 1e-3, 'фондирование = сегодняшняя ставка × дни');
  near(r.total.net, r.total.income + r.total.funding + r.total.derivs + r.total.cash + r.total.rolldown, 1e-3, 'итог');
});

t('кэрри: бумага выбывает в дату оферты', () => {
  const p = by('МТС 002Р-08'), r = U.runCarry({ ks: { mode: 'flat' } }, { horizon: '2Y' });
  const days = Math.round((new Date(p.yieldDate + 'T00:00:00') - new Date(D.asOf + 'T00:00:00')) / 864e5);
  near(r.byPosition[p.id].income, p.mvRub * p.ytm / 100 / 365 * days, 1e-3, 'доход до оферты');
  assert.ok(r.matures.some(m => m.id === p.id && m.date === p.yieldDate));
});

t('кэрри: траектория КС по заседаниям, флоатер и фондирование следуют за КС', () => {
  const sc = { ks: { mode: 'steps', steps: [-100, -100] } };
  const path = U.ksPath(sc, '2027-01-31');
  assert.strictEqual(JSON.stringify(path.map(x => x.ks)), '[14.5,13.5,12.5]');
  assert.strictEqual(path[1].date, D.CBR_MEETINGS[0].effective);
  const flat = U.runCarry({ ks: { mode: 'flat' } }, { horizon: '6M' }), cut = U.runCarry(sc, { horizon: '6M' });
  const fl = by('РЖД 001Р-32R');                         // флоатер на КС
  assert.ok(cut.byPosition[fl.id].income < flat.byPosition[fl.id].income);
  const fx = by('Газпром ЗО27-1-Д');                     // hard ccy — фикс, от КС не зависит
  near(cut.byPosition[fx.id].funding, flat.byPosition[fx.id].funding, 1e-6, 'hard ccy');
  assert.ok(cut.byPosition[by('ОФЗ 26242').id].funding > flat.byPosition[by('ОФЗ 26242').id].funding, 'RUB дешевле');
});

t('кэрри: +50 б.п. к спреду фондирования = 50 б.п. × тело × дни', () => {
  const a = U.runCarry({ ks: { mode: 'flat' } }, { horizon: '1M' }), b = U.runCarry({ ks: { mode: 'flat' }, fundSpread: { RUB: 50 } }, { horizon: '1M' });
  const exp = sum(D.funding.filter(f => f.kind === 'POS' && f.ccy === 'RUB'), f => {
    const p = D.positions.find(x => x.id === f.positionId);
    const u = p.yieldDate || p.maturity;
    const n = Math.min(a.days, Math.round((new Date(u + 'T00:00:00') - new Date(D.asOf + 'T00:00:00')) / 864e5));
    return f.amount * 0.005 / 365 * n;
  });
  near(b.total.funding - a.total.funding, exp, 1, 'Δ фондирования');
});

console.log(`funding/carry: ${n} тестов OK`);
