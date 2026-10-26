// Мониторы (prototypes/concepts), без браузера:
//  - #334: прогон без сдвигов в форме сервера (api/concepts_run: CURVE_MOVES — null на каждый бакет, FX_MOVE_PCT и
//    FX_CBR_MOVE_PCT — null, кроме RUB) — lib/real.js принимает null как «нет данных»: изменения кривых null,
//    K.noMoves у всех, изменения курсов null, в тексте «—», ни NaN, ни «null»; PnL ставок (риск × сдвиг) — число;
//  - #336: вид позиции (kindOf) — как у бэкенда: IRS / XCCY / CDS старого tdm — деривативы (DER), не «Прочее»;
//    позиция 104 фикстуры (OTHER) — «Прочее»; группа совпадает с группой таблицы «валюта × тип» (lib/instr.js);
//    каждая пара «группа.вид» — в M.typeRows (иначе C01, C06, C11 её не показывают).
// Браузерная часть (C05, C06, C09, C12: «сдвигов нет в прогоне») — ui_pack7.pw.test.js.
//   node tests/js/ui_pack7.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..');
const P = f => fs.readFileSync(path.join(ROOT, 'prototypes', 'concepts', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(mutate) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open() {} send() { this.status = 404; } }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  loadRun(ctx);
  if (mutate) mutate(ctx.window.CONCEPTS_RUN);
  vm.runInContext(P('lib/real.js'), ctx);
  vm.runInContext(P('lib/instr.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, I: ctx.window.INSTR };
}
// прогон без прошлого рынка, как его отдаёт сервер после #334
const noMoves = D => {
  D.prevDate = null;
  Object.keys(D.CURVE_MOVES).forEach(c => { D.CURVE_MOVES[c] = D.BUCKETS.map(() => null); });
  ['FX_MOVE_PCT', 'FX_CBR_MOVE_PCT'].forEach(k => Object.keys(D[k]).forEach(c => { D[k][c] = c === 'RUB' ? 0 : null; }));
};

t('#334: сдвигов нет (null от сервера) — изменения кривых null, K.noMoves у всех, «вчера» нет; PnL ставок — число', () => {
  const { M, K } = load(noMoves);
  assert.ok(M.curves.length > 3);
  M.curves.forEach(c => { assert.ok(K.noMoves(c.chg), c.id + ': ' + c.chg); assert.ok(K.yest(c.pts, c.chg).every(v => v === null), c.id); });
  assert.ok(K.noMoves(K.ofzChg()) && K.noMoves(M.basisCurve.chg));
  M.positions.filter(p => p.risk).forEach(p => {
    const r = M.curves.reduce((s, c) => s + (p.risk[c.id] || []).reduce((a, v, k) => a + v * c.chg[k], 0), 0);
    assert.ok(Number.isFinite(r), p.id + ': риск × null — 0, а не NaN');
  });
  // часть бакетов без сдвига (точек кривой нет): null только там, остальные — числа
  const part = load(D => { D.CURVE_MOVES['RUB RUONIA'][0] = null; });
  const ru = part.M.curve['RUB RUONIA'].chg;
  assert.ok(ru[0] === null && ru.slice(1).every(v => typeof v === 'number') && !part.K.noMoves(ru), String(ru));
});

t('#334: изменений курсов нет — null, в тексте «—», ни «null», ни «NaN»; RUB — 0', () => {
  const { M, K } = load(noMoves);
  const ccys = M.ccys.filter(c => c !== 'RUB');
  assert.ok(ccys.length);
  ccys.forEach(c => { assert.strictEqual(M.fxChgPct[c], null, c); assert.strictEqual(M.fxCbr.chgPct[c], null, 'ЦБ ' + c); });
  assert.strictEqual(M.fxChgPct.RUB, 0);
  const line = K.fxLine();
  assert.ok(line && !/null|NaN|undefined/.test(line) && line.includes('—'), line);
  ccys.forEach(c => assert.strictEqual(K.f.pcts(M.fxChgPct[c], 2), '—'));
  assert.strictEqual(K.f.cls(M.fxChgPct.USD), '');
  // со сдвигами — числа, как раньше
  const { M: M2 } = load();
  assert.ok(ccys.filter(c => M2.fxChgPct[c] != null).every(c => typeof M2.fxChgPct[c] === 'number') && typeof M2.fxChgPct.USD === 'number');
});

t('#336: IRS / XCCY / CDS — деривативы, как у бэкенда; OTHER (позиция 104) — «Прочее»', () => {
  const { M, I } = load();
  const by = it => M.positions.filter(p => p.instrType === it);
  assert.ok(by('IRS').length && by('XCCY').length && by('CDS').length);
  by('IRS').forEach(p => assert.deepStrictEqual([p.group, p.kind], ['DER', 'IRS'], p.id));
  by('XCCY').forEach(p => assert.deepStrictEqual([p.group, p.kind], ['DER', 'XCCY'], p.id));
  by('CDS').forEach(p => assert.deepStrictEqual([p.group, p.kind], ['DER', 'OTC'], p.id));
  const oth = M.pos['104'];
  assert.ok(oth && oth.instrType === 'OTHER' && oth.group === 'OTH' && oth.kind === 'OTH', JSON.stringify(oth && [oth.instrType, oth.group, oth.kind]));
  assert.strictEqual(oth.groupLabel, 'Прочее');
  // группа — та же, что у таблицы «валюта × тип» (api/concepts.instr_kind → KIND_GROUP)
  [...by('IRS'), ...by('XCCY'), ...by('CDS')].forEach(p => assert.strictEqual(I.group[I.kindOf(p)], p.group, p.id + ' ' + I.kindOf(p)));
  // каждая пара «группа.вид» прогона — строка M.typeRows: C01 (риск по типам), C06, C11 берут строки из неё
  const rows = new Set(M.typeRows.map(r => r.id));
  M.positions.forEach(p => assert.ok(rows.has(p.group + '.' + p.kind), p.id + ': ' + p.group + '.' + p.kind));
  M.positions.forEach(p => assert.ok(p.kindLabel && p.groupLabel, p.id));
});

t('#336: зеркало — LEGACY_DER в lib/real.js и в backend publish/summary.py одинаковые', () => {
  const js = /const LEGACY_DER = (\{[^}]*\})/.exec(P('lib/real.js'))[1];
  const py = /^LEGACY_DER = (\{[^}]*\})/m.exec(fs.readFileSync(path.join(ROOT, 'backend', 'src', 'desk', 'publish', 'summary.py'), 'utf8'))[1];
  const parse = s => JSON.parse(s.replace(/'/g, '"').replace(/([{,]\s*)([A-Z]+):/g, '$1"$2":'));
  assert.deepStrictEqual(parse(js), parse(py));
});

console.log(n + ' tests passed');
