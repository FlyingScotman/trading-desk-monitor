// #30: ссылка «прогноз →» карточки позиции на C17 (KIT.carryLink, prototypes/concepts/lib/kit.js) не переносит
// выбор прогона, срок которого (desk.run.v1, 12 ч) истёк, — как ссылки строки desk-nav.js (nav_chf.test.js).
// Точный адрес без совпавшего сохранённого выбора переносится как задан.
//   node --test tests/js/carry_link_30.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const KEY = 'desk.run.v1', HOUR = 3600e3, NOW = Date.parse('2026-10-27T12:00:00Z');

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(search, record) {
  const store = new Map(record == null ? [] : [[KEY, JSON.stringify(record)]]);
  const listeners = {};
  const document = { addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); }, createElement: el,
    documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const clock = { now: NOW };
  const ctx = { document, URLSearchParams, URL, console, setTimeout,
    Date: class extends Date { static now() { return clock.now; } },
    location: { href: 'http://desk.test/concepts/c03-positions.html' + search, search, hostname: 'desk.test', protocol: 'http:' },
    localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) },
    getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { K: ctx.KIT, clock, listeners };
}
const q = href => Object.fromEntries(new URL(href, 'http://desk.test/concepts/').searchParams);

for (const age of [11, 12, 13]) {
  test('#30: ссылка на C17 при сохранённом выборе возрастом ' + age + ' ч', () => {
    const { K } = load('?run_id=42', { sel: { run_id: '42' }, at: NOW - age * HOUR });
    assert.deepEqual(q(K.carryLink('T1|26238')), age < 12 ? { run_id: '42', pos: 'T1|26238' } : { pos: 'T1|26238' });
  });
}

test('#30: точный адрес без совпавшего сохранённого выбора переносится как задан', () => {
  for (const record of [null, { sel: { run_id: '43' }, at: NOW - 13 * HOUR }]) {
    const { K } = load('?run_id=42&as_of=2026-10-21', record);
    assert.deepEqual(q(K.carryLink('7')), { run_id: '42', as_of: '2026-10-21', pos: '7' });
  }
  assert.deepEqual(q(load('?demo=1', { sel: { demo: '1' }, at: NOW - HOUR }).K.carryLink('7')), { demo: '1', pos: '7' });
});

test('#30: вкладка простояла открытой до истечения срока — адрес ссылки пересчитывается при переходе', () => {
  const { K, clock, listeners } = load('?run_id=42', { sel: { run_id: '42' }, at: NOW - 11 * HOUR });
  const a = { href: K.carryLink('7'), getAttribute: k => (k === 'data-carry-pos' ? '7' : null) };
  assert.deepEqual(q(a.href), { run_id: '42', pos: '7' });
  clock.now = NOW + 2 * HOUR;
  const target = { closest: s => (s === 'a[data-carry-pos]' ? a : null) };
  for (const ev of ['click', 'auxclick']) {
    a.href = 'stale';
    (listeners[ev] || []).forEach(fn => fn({ target }));
    assert.deepEqual(q(a.href), { pos: '7' }, ev);
  }
});
