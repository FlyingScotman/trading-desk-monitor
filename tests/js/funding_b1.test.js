// Волна B1: фондирование в мониторах (prototypes/concepts/). На прогоне — D.fundingStatus и pnl.funding позиции
// (api/compat.py) через lib/real.js: кэрри = carry + rolldown (без фондирования, #168), фондирование = funding — свой
// фактор PnL (p.pnl.funding, p.fundingDay), сумма факторов = итог; M.funding — итоги по книгам и юрлицам и проблемы;
// «не настроено» / «не считалось» — словами (заметка в шапке, панель кокпита, строка карточки), а не 0. В демо
// (lib/mock.js) — демо-фондирование отдельным фактором.
//   node tests/js/funding_b1.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const tests = [];
const t = (name, f) => tests.push([name, f]);
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

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
    localStorage: { getItem(k) { return k === 'concepts-repr' && opts.repr ? opts.repr : null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) {
    vm.runInContext(P('data.js'), ctx);
    if (opts.mutate) opts.mutate(ctx.window.DESK);
    if (opts.extras) ctx.window.DESK_EXTRAS = opts.extras(ctx.window.DESK);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.DESK };
}
const bond = D => D.positions.find(p => p.instrType === 'BOND' && p.pnl && p.pnl.funding);

t('прогон без расчёта фондирования (до B1): «не считалось» в шапке и в панели, а не 0', () => {
  const { M, K } = load({ real: true, mutate: D => { delete D.fundingStatus; } });
  assert.strictEqual(M.funding.state, 'not_computed');
  assert.ok(M.notes.some(x => x.startsWith('Фондирование: ')), M.notes.join(' | '));
  const box = el(); K.fundingPanel(box);
  assert.ok(box.innerHTML.includes('не считалось') && box.innerHTML.includes('а не 0'), box.innerHTML);
});

t('не настроено: заметка, панель и карточка словами; фондирование позиции — null', () => {
  const { M, K } = load({ real: true, mutate: D => {
    D.fundingStatus = { state: 'not_configured', text: 'фондирование не настроено: адаптер не передал правил', total: null, byBook: {}, byEntity: {}, problems: [] };
    D.positions.forEach(p => { if (p.pnl) p.pnl.funding = null; p.extra = Object.assign({}, p.extra, { funding: { state: 'not_configured', amount_rub: null } }); });
  } });
  assert.ok(M.notes.includes('Фондирование: фондирование не настроено: адаптер не передал правил'));
  const p = M.positions.find(x => x.pnl.available);
  assert.strictEqual(p.funding, null);
  assert.ok(K.fundingLine(p).includes('не настроено'), K.fundingLine(p));
  const box = el(); K.fundingPanel(box);
  assert.ok(box.innerHTML.includes('не настроено') && !box.innerHTML.includes('<table'), box.innerHTML);
});

t('посчитано: кэрри без фондирования, фондирование — свой фактор, строка карточки, книги и проблемы в панели', () => {
  let want;
  const { M, K } = load({ real: true, mutate: D => {
    const b = bond(D);
    want = { id: b.id, carry: b.pnl.carry + b.pnl.rolldown, funding: b.pnl.funding, days: D.accrualDays || 1 };
    b.extra = Object.assign({}, b.extra, { funding: { state: 'funded', amount_rub: b.pnl.funding, in_pnl: true,
      lines: [{ kind: 'POS', rate_expr: 'KEY_RATE + 100 bp', days: 3, rate_pct: 17.5, body_ccy: -3e8 },
        { kind: 'POS', rate_expr: 'KEY_RATE + 100 bp', days: 3, rate_pct: 18.5, body_ccy: -1e8 }] } });
    D.fundingStatus = { state: 'computed', text: 'фондирование посчитано', period: { start: '2026-10-16', end: '2026-10-19', days: 3 },
      total: -1.5e6, inPositions: -1.2e6, bookLevel: -3e5, byEntity: { ENT1: -1e6, ENT2: -5e5 },
      byBook: { [b.book]: { total: -1.5e6, inPositions: -1.2e6, bookLevel: -3e5, problems: 1, base: 'mv_clean', writeOff: { periodicity: 'monthly', last: '2026-09-30', since_write_off: -9e6 } } },
      problems: [{ kind: 'HTM', ref: 'H1|X', book: 'H1', reason: 'нет purchase_accrued_ccy' }], provisional: ['fx_date'] };
  } });
  const p = M.pos[want.id];
  near(p.pnl.carry, want.carry, 'кэрри = carry + rolldown, без фондирования');
  near(p.pnl.funding, want.funding, 'фондирование — свой фактор');
  near(p.funding, want.funding, 'фондирование позиции');
  near(p.carryDay * want.days, want.carry, 'кэрри за день — без фондирования');
  near(p.fundingDay * want.days, want.funding, 'фондирование за день');
  near(p.fundRate, (17.5 * 3 + 18.5 * 1) / 4, 'ставка фондирования — rate_pct строк, средняя по телу, %');
  assert.ok(K.fundingLine(p).includes('KEY_RATE + 100 bp') && K.fundingLine(p).includes('строкой «Фондирование» (не в кэрри)'), K.fundingLine(p));
  assert.strictEqual(M.funding.state, 'computed');
  assert.ok(M.notes.some(x => x.startsWith('Фондирование: проблем 1')), M.notes.join(' | '));
  const box = el(); K.fundingPanel(box);
  for (const s of ['<table', 'mv_clean', 'monthly', 'нет purchase_accrued_ccy', 'по юрлицам', 'fx_date', '3 дн.'])
    assert.ok(box.innerHTML.includes(s), s + ': ' + box.innerHTML);
});

t('демо: фондирование отдельно от кэрри, итог = сумме книг и фондированию позиций', () => {
  const { M, K } = load();
  assert.strictEqual(M.funding.state, 'demo');
  near(M.funding.total, Object.values(M.funding.byBook).reduce((s, b) => s + b.total, 0), 'итог по книгам');
  assert.ok(M.funding.total < 0);
  near(M.funding.total, M.positions.reduce((s, p) => s + (p.group === 'DER' || p.book === 'HTM' ? 0 : p.pnl.funding), 0), 'итог = фондированию позиций');
  const p = M.positions.find(x => x.fundRate != null && x.book !== 'HTM');
  assert.ok(K.fundingLine(p).includes('демо'), K.fundingLine(p));
  const box = el(); K.fundingPanel(box);
  assert.ok(box.innerHTML.includes('<table'), box.innerHTML);
});

// #168: кэрри без фондирования, фондирование — свой фактор; сумма факторов PnL по-прежнему равна итогу
const factorSum = (K, p) => K.PNL_FACTORS.reduce((s, [k]) => s + (p.pnl[k] || 0), 0);
t('#168: сумма факторов (кэрри, фондирование, ставки, …) = итог — прогон и демо; фондирование в истории отдельным рядом', () => {
  for (const opts of [{ real: true }, {}]) {
    const { M, K } = load(opts);
    assert.strictEqual(K.PNL_FACTORS.map(x => x[0]).join(), 'carry,funding,rates,spread,fx,trading,other');
    const ok = M.positions.filter(p => p.pnl.available !== false && p.pnl.total != null);
    assert.ok(ok.length > 5);
    ok.forEach(p => near(factorSum(K, p), p.pnl.total, (opts.real ? 'прогон ' : 'демо ') + p.id + ': сумма факторов = итог'));
    assert.ok(ok.some(p => p.pnl.funding), 'есть позиции с фондированием');
    assert.ok(Array.isArray(M.hist.pnl.funding) && M.hist.pnl.funding.length === M.hist.pnl.total.length, 'ряд funding в истории');
    if (!opts.real) M.hist.pnl.total.forEach((v, i) => near(K.PNL_FACTORS.reduce((s, [k]) => s + M.hist.pnl[k][i], 0), v, 'история: сумма факторов = итог'));
    assert.ok(K.posCols.funding && K.posCols.funding.key === 'fundingDay' && /без фондирования/.test(K.posCols.carry.title) && /roll-down/.test(K.tips.carry));
    const k = M.kpis(); near(k.fundingDay, M.sum(M.positions, p => p.fundingDay || 0), 'KPI фондирования за день');
  }
});

t('#168: фондирование не посчитано — фактор null («—»), кэрри и итог те же', () => {
  let want;
  const { M, K } = load({ real: true, mutate: D => {
    const b = bond(D); want = { id: b.id, carry: b.pnl.carry + b.pnl.rolldown, total: b.pnl.explained }; b.pnl.funding = null;
  } });
  const p = M.pos[want.id];
  assert.strictEqual(p.pnl.funding, null); assert.strictEqual(p.fundingDay, null); assert.strictEqual(p.funding, null);
  near(p.pnl.carry, want.carry, 'кэрри'); near(p.pnl.total, want.total, 'итог');
  assert.ok(K.posCols.funding.fmt(p.fundingDay).includes('—'));
  assert.ok(K.fundingDayHtml(p).includes('—'));
});

t('#168: история — фондирование не посчитано (прогон до B1) — null («—»), а не 0, и в валюте отчёта тоже', () => {
  const extras = D => {
    const asOf = D.asOf || D.AS_OF || D.businessDate, d0 = '2026-01-05';
    const pnl = { carry: [1e6, 2e6], funding: [null, -5e5], rates: [3e5, -1e5], spread: [0, 0], fx: [0, 0], trading: [0, 0], other: [0, 0] };
    pnl.total = [1.3e6, 1.4e6];
    return { hist: { pnl }, histDates: asOf ? [d0, asOf] : [d0, '2026-01-06'], histSource: 'runs' };
  };
  for (const repr of [null, 'USD']) {
    const { M, K } = load({ real: true, extras, repr });
    assert.strictEqual(K.repr, repr || 'RUB');
    assert.strictEqual(M.hist.pnl.funding[0], null, (repr || 'RUB') + ': день без фондирования — null');
    assert.ok(M.hist.pnl.funding[1] < 0 && M.hist.pnl.carry[0] > 0);
    // сумма посчитанных факторов = итог и в дне без фондирования
    [0, 1].forEach(i => near(K.PNL_FACTORS.reduce((s, [k]) => s + (M.hist.pnl[k][i] || 0), 0), M.hist.pnl.total[i], 'история: сумма факторов = итог'));
  }
});

t('#168: итог фондирования по группе, где оно нигде не посчитано, — «—», а не 0; где посчитано хоть у одной — число', () => {
  const { M, K } = load({ real: true, mutate: D => D.positions.forEach(p => { if (p.pnl) p.pnl.funding = null; }) });
  const spec = list => ({ title: 'Фондирование', unit: 'тыс ₽', fmt: K.f.k, list, val: 'fundingDay', nullable: true });
  assert.ok(K.d(spec(M.positions)).includes('—') && !K.d(spec(M.positions)).includes('data-dr'), K.d(spec(M.positions)));
  assert.ok(K.posCols.funding.nullable);
  const { M: M2, K: K2 } = load({ real: true });
  const html = K2.d(spec(M2.positions)); assert.ok(html.includes('data-dr') && !/>—</.test(html), html);
  // без nullable — прежнее поведение (0), как у остальных мер
  assert.ok(K.d({ title: 'x', fmt: K.f.k, list: M.positions, val: 'fundingDay' }).includes('data-dr'));
});

t('#253: C04 — шапка «итого» как строка «Итого»: мера, не посчитанная ни у одной позиции, — «—», а не 0', () => {
  const c04 = P('concepts/c04-pivot.html');
  // ячейка (и строка «Итого»): nul без посчитанных значений — «—»
  assert.ok(c04.includes("if (a && a.nul && !a.n) return '<td class=\"n zero ' + (cls || '') + '\" title=\"не посчитано\">—</td>'"));
  // шапка: то же условие для итога всей таблицы
  assert.ok(/\(tot && !\(tot\.nul && !tot\.n\) \? K\.d\(/.test(c04), 'шапка C04 проверяет tot.nul && !tot.n');
});

for (const [name, f] of tests) { f(); n++; console.log('ok', name); }
console.log(n + ' tests passed');
