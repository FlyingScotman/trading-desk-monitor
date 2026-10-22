// Батч 1, п. 3 (R33): редактируемый алиас бумаги в мониторах (prototypes/concepts/). Имя бумаги — алиас ?? биржевое,
// по ISIN, на прогоне — из GET /api/v1/instruments/aliases (lib/real.js), в демо — в памяти (lib/mock.js); поиск — по
// обоим именам; правка — PUT /api/v1/instruments/alias (JSON), 403 — только чтение; K.setAlias меняет все позиции бумаги.
//   node tests/js/alias_ui.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const tests = [];
const t = (name, f) => tests.push([name, f]);

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
// opts.aliases — ответ GET /api/v1/instruments/aliases; opts.fetch — заглушка fetch; opts.host — location.hostname
function load(opts) {
  opts = opts || {};
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const urls = [];
  class XHR {
    open(m, u) { this.u = u; urls.push(u); }
    send() {
      if (/instruments\/aliases$/.test(this.u) && opts.aliases) { this.status = 200; this.responseText = JSON.stringify(opts.aliases); }
      else this.status = 404;
    }
  }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: opts.host || '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR, fetch: opts.fetch };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) { vm.runInContext(P('data.js'), ctx); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.DESK, urls };
}

t('прогон: алиас по ISIN — вместо биржевого имени, поиск по обоим, бумаги без алиаса как были', () => {
  const at = '2026-10-22T09:00:00+00:00';
  const { M, D, urls } = load({ real: true, aliases: { aliases: [{ isin: 'SU26242RMFS0', alias: 'ОФЗ-2029 (26242)', changed_by: 'local', changed_at: at }] } });
  assert.ok(urls.some(u => u.endsWith('../api/v1/instruments/aliases')), urls.join(' '));
  const L = M.positions.filter(p => p.isin === 'SU26242RMFS0');
  assert.ok(L.length >= 1);
  for (const p of L) {
    assert.strictEqual(p.name, 'ОФЗ-2029 (26242)'); assert.strictEqual(p.alias, 'ОФЗ-2029 (26242)'); assert.strictEqual(p.exchName, 'ОФЗ 26242');
    assert.ok(p.searchText.includes('офз-2029') && p.searchText.includes('офз 26242') && p.searchText.includes('su26242rmfs0'));
  }
  assert.deepStrictEqual(JSON.parse(JSON.stringify(M.aliases['SU26242RMFS0'])), { alias: 'ОФЗ-2029 (26242)', changedBy: 'local', changedAt: at });
  const other = M.positions.find(p => p.isin === 'SU26239RMFS6');
  assert.strictEqual(other.name, 'ОФЗ 26239'); assert.strictEqual(other.alias, null); assert.strictEqual(other.exchName, other.name);
  assert.strictEqual(D.positions.find(d => d.isin === 'SU26242RMFS0').name, 'ОФЗ 26242', 'данные прогона не меняются');
});

t('прогон: сервер без алиасов (или без ответа) — биржевые имена, страница работает', () => {
  const { M } = load({ real: true });
  assert.deepStrictEqual(Object.keys(M.aliases), []);
  assert.ok(M.positions.every(p => p.alias === null && p.name === p.exchName));
});

t('прогон: правка — PUT /api/v1/instruments/alias с JSON; с чужого адреса — только чтение без запроса', async () => {
  const calls = [];
  const fetch = (u, o) => { calls.push([u, o]); return Promise.resolve({ status: 200, json: () => Promise.resolve({ isin: 'SU26242RMFS0', alias: 'X', changed_by: 'local', changed_at: 'z', changed: true }) }); };
  const { M } = load({ real: true, fetch });
  assert.strictEqual(M.aliasApi.editable, true);
  const r = await M.aliasApi.put('SU26242RMFS0', 'X');
  assert.strictEqual(r.status, 200); assert.strictEqual(r.body.alias, 'X');
  const [u, o] = calls[0];
  assert.strictEqual(u, '../api/v1/instruments/alias'); assert.strictEqual(o.method, 'PUT');
  assert.strictEqual(o.headers['Content-Type'], 'application/json'); assert.deepStrictEqual(JSON.parse(o.body), { isin: 'SU26242RMFS0', alias: 'X' });
  await M.aliasApi.history('SU26242RMFS0');
  assert.strictEqual(calls[1][0], '../api/v1/instruments/aliases/SU26242RMFS0/history');
  for (const host of ['127.0.0.1', 'localhost', '[::1]']) assert.strictEqual(load({ real: true, host }).M.aliasApi.editable, true, host);
  const ro = load({ real: true, host: '192.168.1.20' });
  assert.strictEqual(ro.M.aliasApi.editable, false);
  const p = ro.M.positions.find(x => x.isin);
  const html = ro.K.aliasName(p);
  assert.ok(/data-al="edit" disabled title="только чтение: [^"]*127\.0\.0\.1/.test(html), html);
  assert.ok(html.includes('data-al="hist"'), 'история — и в режиме только чтения');
  // сеть недоступна — ответ, а не исключение
  const off = load({ real: true, fetch: () => Promise.reject(new Error('down')) });
  assert.strictEqual((await off.M.aliasApi.put('SU26242RMFS0', 'X')).status, 0);
});

t('демо: примерный алиас, биржевое имя серым в карточке и в подсказке ячейки', () => {
  const { M, K } = load();
  const p = M.positions.find(x => x.isin === 'RU000A105RL4');
  assert.strictEqual(p.exchName, 'Минфин РФ 43 USD'); assert.strictEqual(p.name, 'Россия-2043 USD (евробонд)');
  assert.ok(p.searchText.includes('россия-2043') && p.searchText.includes('минфин рф 43 usd'));
  const html = K.aliasName(p);
  assert.ok(html.includes('<span class="k-nm-v">Россия-2043 USD (евробонд)</span>'));
  assert.ok(html.includes('<span class="k-exch" title="биржевое имя">Минфин РФ 43 USD</span>'));
  assert.ok(/data-al="edit" title="переименовать/.test(html));
  assert.ok(K.nameCell(p).includes('title="биржевое имя: Минфин РФ 43 USD"'));
  const q = M.positions.find(x => x.isin && !x.alias);
  assert.ok(!K.aliasName(q).includes('k-exch')); assert.ok(!K.nameCell(q).includes('title'));
  const der = M.positions.find(x => !x.isin);
  assert.ok(der && !K.aliasName(der).includes('data-al'), 'без ISIN — не переименовать');
});

t('K.setAlias: все позиции бумаги, поиск и события страницы; пустое — снова биржевое имя', () => {
  const { M, K } = load();
  const isin = M.positions.find(x => x.isin && x.kind === 'PD').isin;
  const L = M.positions.filter(p => p.isin === isin), exch = L[0].exchName;
  const ev = []; K.on('alias', () => ev.push('alias')); K.onFilter(() => ev.push('filter'));
  K.setAlias(isin, 'Длинная ОФЗ', { changedBy: 'demo' });
  assert.deepStrictEqual(ev, ['alias', 'filter']);
  L.forEach(p => { assert.strictEqual(p.name, 'Длинная ОФЗ'); assert.ok(p.searchText.startsWith('длинная офз ') && p.searchText.includes(exch.toLowerCase())); });
  assert.strictEqual(M.aliases[isin].alias, 'Длинная ОФЗ');
  K.setAlias(isin, null);
  L.forEach(p => { assert.strictEqual(p.name, exch); assert.strictEqual(p.alias, null); assert.strictEqual(p.searchText, p.searchBase); });
  assert.ok(!(isin in M.aliases));
});

t('демо: M.aliasApi как сервер — пробелы схлопываются, пустое сбрасывает, без изменения — без ревизии, длинное — 422', async () => {
  const { M } = load();
  const isin = 'RU000A105RL4';
  let r = await M.aliasApi.put(isin, '  Россия   2043  ');
  assert.strictEqual(r.status, 200); assert.strictEqual(r.body.alias, 'Россия 2043'); assert.strictEqual(r.body.changed, true);
  r = await M.aliasApi.put(isin, 'Россия 2043');
  assert.strictEqual(r.body.changed, false);
  r = await M.aliasApi.put(isin, '   ');
  assert.strictEqual(r.body.alias, null); assert.strictEqual(r.body.changed, true);
  r = await M.aliasApi.put(isin, 'x'.repeat(121));
  assert.strictEqual(r.status, 422);
  r = await M.aliasApi.put(isin, 'a\u0001b');
  assert.strictEqual(r.status, 422);
  assert.strictEqual((await M.aliasApi.put(isin, 'a\tb')).body.alias, 'a b', 'табуляция — пробел');
  const h = await M.aliasApi.history(isin);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.map(x => x.alias))), ['a b', null, 'Россия 2043', 'Россия-2043 USD (евробонд)'], 'новые сверху');
  assert.ok(h.every(x => x.changed_by && x.changed_at && x.rev_id));
});

(async () => {
  for (const [name, f] of tests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
