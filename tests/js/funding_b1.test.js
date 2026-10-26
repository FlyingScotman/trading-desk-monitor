// Волна B1: фондирование в мониторах (prototypes/concepts/). На прогоне — D.fundingStatus и pnl.funding позиции
// (api/compat.py) через lib/real.js: кэрри = carry + rolldown + funding (за вычетом фондирования), M.funding — итоги по
// книгам и юрлицам и проблемы; «не настроено» / «не считалось» — словами (заметка в шапке, панель кокпита, строка
// карточки), а не 0. В демо (lib/mock.js) — демо-фондирование в кэрри.
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
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) {
    vm.runInContext(P('data.js'), ctx);
    if (opts.mutate) opts.mutate(ctx.window.DESK);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
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

t('посчитано: кэрри за вычетом фондирования, строка карточки, книги и проблемы в панели', () => {
  let want;
  const { M, K } = load({ real: true, mutate: D => {
    const b = bond(D);
    want = { id: b.id, carry: b.pnl.carry + b.pnl.rolldown + b.pnl.funding, funding: b.pnl.funding };
    b.extra = Object.assign({}, b.extra, { funding: { state: 'funded', amount_rub: b.pnl.funding, in_pnl: true,
      lines: [{ kind: 'POS', rate_expr: 'KEY_RATE + 100 bp', days: 3 }] } });
    D.fundingStatus = { state: 'computed', text: 'фондирование посчитано', period: { start: '2026-10-16', end: '2026-10-19', days: 3 },
      total: -1.5e6, inPositions: -1.2e6, bookLevel: -3e5, byEntity: { ENT1: -1e6, ENT2: -5e5 },
      byBook: { [b.book]: { total: -1.5e6, inPositions: -1.2e6, bookLevel: -3e5, problems: 1, base: 'mv_clean', writeOff: { periodicity: 'monthly', last: '2026-09-30', since_write_off: -9e6 } } },
      problems: [{ kind: 'HTM', ref: 'H1|X', book: 'H1', reason: 'нет purchase_accrued_ccy' }], provisional: ['fx_date'] };
  } });
  const p = M.pos[want.id];
  near(p.pnl.carry, want.carry, 'кэрри = carry + rolldown + funding');
  near(p.funding, want.funding, 'фондирование позиции');
  assert.ok(K.fundingLine(p).includes('KEY_RATE + 100 bp') && K.fundingLine(p).includes('в кэрри позиции'), K.fundingLine(p));
  assert.strictEqual(M.funding.state, 'computed');
  assert.ok(M.notes.some(x => x.startsWith('Фондирование: проблем 1')), M.notes.join(' | '));
  const box = el(); K.fundingPanel(box);
  for (const s of ['<table', 'mv_clean', 'monthly', 'нет purchase_accrued_ccy', 'по юрлицам', 'fx_date', '3 дн.'])
    assert.ok(box.innerHTML.includes(s), s + ': ' + box.innerHTML);
});

t('рыночная ставка по умолчанию (#237): помечена в карточке и в панели кокпита, источник правила виден', () => {
  let id, other;
  const { M, K } = load({ real: true, mutate: D => {
    const bs = D.positions.filter(p => p.instrType === 'BOND' && p.pnl && p.pnl.funding);
    const b = bs[0]; id = b.id; other = bs[1] && bs[1].id;
    b.extra = Object.assign({}, b.extra, { funding: { state: 'funded', amount_rub: b.pnl.funding, in_pnl: true, default_rate: true, rule_source: 'default',
      lines: [{ kind: 'POS', rate_expr: 'RUONIA (рыночная ставка по умолчанию)', days: 1, rule_source: 'default', rule_id: 'default:RUB', default_rate: true }] } });
    if (other) bs[1].extra = Object.assign({}, bs[1].extra, { funding: { state: 'funded', amount_rub: bs[1].pnl.funding, in_pnl: true, rule_source: 'settings_override',
      lines: [{ kind: 'POS', rate_expr: '12.00% fixed', days: 1, rule_source: 'settings_override', rule_id: 's_t1' }] } });
    D.fundingStatus = { state: 'computed', text: 'фондирование посчитано', period: { start: '2026-10-14', end: '2026-10-15', days: 1 },
      total: -1e5, inPositions: -1e5, bookLevel: 0, byEntity: {}, byBook: {}, problems: [], provisional: [],
      defaultRate: { label: 'рыночная ставка по умолчанию', enabled: true, lines: 3, positions: 2,
        by_ccy: { RUB: { kind: 'ruonia', expr: 'RUONIA (рыночная ставка по умолчанию)' }, CNY: { kind: 'index_spread', expr: 'RUSFARCNY (рыночная ставка по умолчанию)' }, USD: { kind: 'none', expr: null } } } };
  } });
  const line = K.fundingLine(M.pos[id]);
  assert.ok(line.includes('рыночная ставка по умолчанию') && line.includes('⚑'), line);
  if (other) assert.ok(K.fundingLine(M.pos[other]).includes('правило настроек (главнее адаптера) s_t1'), K.fundingLine(M.pos[other]));
  const box = el(); K.fundingPanel(box);
  for (const s of ['Рыночная ставка по умолчанию', 'RUB — RUONIA', 'CNY — RUSFARCNY', 'строк по ней 3', 'settings.html#funding'])
    assert.ok(box.innerHTML.includes(s), s + ': ' + box.innerHTML);
  assert.ok(!box.innerHTML.includes('USD —'), box.innerHTML);
});

t('демо: фондирование в кэрри позиций, итог = сумме книг', () => {
  const { M, K } = load();
  assert.strictEqual(M.funding.state, 'demo');
  near(M.funding.total, Object.values(M.funding.byBook).reduce((s, b) => s + b.total, 0), 'итог по книгам');
  assert.ok(M.funding.total < 0);
  const p = M.positions.find(x => x.fundRate != null && x.book !== 'HTM');
  assert.ok(K.fundingLine(p).includes('демо'), K.fundingLine(p));
  const box = el(); K.fundingPanel(box);
  assert.ok(box.innerHTML.includes('<table'), box.innerHTML);
});

for (const [name, f] of tests) { f(); n++; console.log('ok', name); }
console.log(n + ' tests passed');
