// Run-selection persistence without a browser; browser coverage is in nav_chf.pw.test.js.
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ROOT = path.resolve(__dirname, '../../prototypes');
const KEY = 'desk.run.v1', HOUR = 3600e3, NOW = Date.parse('2026-10-27T12:00:00Z');
const plain = v => JSON.parse(JSON.stringify(v));

function nav(url, record, now = NOW) {
  const store = new Map(record == null ? [] : [[KEY, JSON.stringify(record)]]);
  const element = () => ({ style: {}, children: [], setAttribute(k, v) { this[k] = v; }, appendChild(e) { this.children.push(e); }, addEventListener() {} });
  const location = new URL(url), document = { readyState: 'loading', head: element(), documentElement: element(), createElement: element, addEventListener() {} };
  const ctx = { location, document, URL, console, Date: class extends Date { static now() { return now; } },
    history: { state: null, replaceState(_s, _t, u) { location.href = u; } },
    localStorage: { getItem: k => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: k => store.delete(k) } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'desk-nav.js'), 'utf8'), ctx);
  return { api: ctx.DESK_NAV, store, location };
}

for (const sel of [{ run_id: '42' }, { as_of: '2026-10-21' }, { demo: '1' }]) {
  test('#30: navigation with ' + Object.keys(sel)[0] + ' keeps the explicit selection timestamp', () => {
    const record = { sel, at: NOW - 11 * HOUR };
    const q = new URLSearchParams(sel);
    const { api, store } = nav('http://desk.test/index.html?' + q, record);
    assert.deepEqual(plain(api.sel), sel);
    assert.deepEqual(JSON.parse(store.get(KEY)), record);
    const next = nav(api.href('settings.html'), JSON.parse(store.get(KEY)), NOW + HOUR / 2);
    assert.deepEqual(JSON.parse(next.store.get(KEY)), record);
  });
}

test('#30: an unparameterized page restores a pin before 12h and uses latest at the boundary', () => {
  const record = { sel: { run_id: '42' }, at: NOW - 12 * HOUR };
  const before = nav('http://desk.test/settings.html', record, NOW - 1);
  assert.equal(before.location.search, '?run_id=42');
  assert.equal(before.api.pinned, true);
  const expired = nav('http://desk.test/settings.html', record);
  assert.equal(expired.location.search, '');
  assert.deepEqual(plain(expired.api.sel), {});
  assert.equal(expired.api.pinned, false);
});

test('#30: explicit repeated selection renews TTL; latest clears the shared pin', () => {
  const old = { sel: { run_id: '42' }, at: NOW - 11 * HOUR };
  const selected = nav('http://desk.test/settings.html?run_id=42&tab=details#section', old);
  selected.api.select({ run_id: '42' });
  assert.deepEqual(JSON.parse(selected.store.get(KEY)), { sel: { run_id: '42' }, at: NOW });
  assert.equal(new URL(selected.location.href).searchParams.get('tab'), 'details');
  assert.equal(new URL(selected.location.href).hash, '#section');
  selected.api.select({});
  assert.equal(selected.store.has(KEY), false);
  assert.equal(new URL(selected.location.href).search, '?tab=details');
  assert.equal(new URL(selected.location.href).hash, '#section');
});

for (const age of [11, 12, 13]) {
  test('#30: internal links at ' + age + 'h respect expiry; the exact incoming URL stays pinned', () => {
    const sel = { run_id: '42' }, record = { sel, at: NOW - age * HOUR };
    const { api, location, store } = nav('http://desk.test/index.html?run_id=42', record);
    assert.equal(location.search, '?run_id=42');
    assert.deepEqual(plain(api.sel), sel);
    assert.equal(new URL(api.href('settings.html')).search, age < 12 ? '?run_id=42' : '');
    assert.deepEqual(JSON.parse(store.get(KEY)), record);
  });
}

for (const record of [null, { sel: { run_id: '42' }, at: NOW - HOUR }, { sel: { run_id: '42' }, at: NOW - 13 * HOUR }, { sel: { run_id: '43' }, at: NOW - 13 * HOUR }]) {
  test('#30: exact direct URL is independent of saved browser state: ' + JSON.stringify(record), () => {
    const { api, location, store } = nav('http://desk.test/index.html?run_id=42#detail', record);
    assert.equal(location.search, '?run_id=42');
    assert.equal(location.hash, '#detail');
    assert.deepEqual(plain(api.sel), { run_id: '42' });
    assert.equal(store.get(KEY) ?? null, record ? JSON.stringify(record) : null);
  });
}
