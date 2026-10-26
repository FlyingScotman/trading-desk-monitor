// #291: причина неточной оценки в списке позиций (K.posList: «не оценены», «оценены не ядром») — из позиции модели
// (p.estReason, lib/real.js из extra прогона), не из window.DESK. Порядок: reason, not_exact_reason, assumptions; без
// причины — пусто. Прогон — фикстура tests/js/fixtures/concepts-run.json, extra задаётся через mutate.
//   node tests/js/est_reason_291.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

// маленький DOM: querySelector возвращает один и тот же дочерний элемент на селектор (у ящика: .t, .st, .bd, .close)
function el() {
  const kids = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', textContent: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(s) { return kids[s] || (kids[s] = el()); }, querySelectorAll() { return []; },
    addEventListener() {}, setAttribute() {}, remove() {}, getBoundingClientRect: () => ({ width: 80 }) };
}
function load(mutate) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open() {} send() { this.status = 404; } }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  mutate(loadRun(ctx));
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.CONCEPTS_RUN };
}

const CASES = [
  ['reason', { reason: 'нет котировки', not_exact_reason: 'не это', assumptions: ['и не это'] }, 'нет котировки'],
  ['not_exact_reason', { not_exact_reason: 'риск по дюрации', assumptions: ['не это'] }, 'риск по дюрации'],
  ['assumptions', { assumptions: ['фиксинг перенесён', 'условия предварительные'] }, 'фиксинг перенесён; условия предварительные'],
  ['без причины', { quality: 'exact' }, ''],
];
let ids;
const { M, K } = load(D => {
  ids = D.positions.slice(0, CASES.length).map(p => p.id);
  CASES.forEach(([, extra], i) => { D.positions[i].extra = extra; });
});

t('p.estReason: reason → not_exact_reason → assumptions; без причины — пустая строка', () => {
  CASES.forEach(([name, , want], i) => assert.strictEqual(M.pos[ids[i]].estReason, want, name));
});

t('K.posList показывает причину из p.estReason в столбце «Причина»', () => {
  const o = K.posList('Оценены не ядром', ids);
  const rows = o.bd.innerHTML.split('<tr class="row"').slice(1);
  assert.strictEqual(rows.length, CASES.length);
  CASES.forEach(([name, , want], i) => {
    const cell = rows[i].match(/<td class="mut3" style="max-width:360px">([^<]*)<\/td>/);
    assert.ok(cell, name + ': нет ячейки причины');
    assert.strictEqual(cell[1], K.esc(want), name);
  });
});

t('K.posList не читает window.DESK', () => {
  const kit = P('concepts/lib/kit.js');
  assert.ok(!/window\.DESK\b(?!_)/.test(kit));
});

console.log(n + ' tests passed');
