// TDM#776: короткое имя бумаги — своя колонка «Кор. имя» и своя правка в карточке, отдельно от полного имени (алиаса).
// p.shortName = своё по ISIN (p.shortOwn, GET /api/v1/instruments/short-names) ?? биржевое (p.exchShort, extra.short_name
// прогона, MOEX SHORTNAME); правка — PUT /api/v1/instruments/short-name; K.setShort меняет все позиции бумаги; поиск находит
// по обоим; в демо — в памяти страницы. Права правки — те же, что у алиаса (TDM#777).
//   node tests/js/short_name_776.test.js
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
// opts.shortNames — ответ GET /api/v1/instruments/short-names; opts.mutate(D) — правка прогона до real.js (extra.short_name);
// opts.fetch — заглушка fetch для остальных запросов; opts.host — location.hostname
function load(opts) {
  opts = opts || {};
  const handlers = {};
  const document = { addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); }, dispatchEvent(e) { (handlers[e.type] || []).forEach(f => f(e)); }, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const urls = [];
  class XHR { constructor() { throw new Error('синхронный запрос запрещён (TDM#629)'); } }
  const fetch = (u, o) => {
    urls.push(u);
    if (/instruments\/(aliases|short-names)$/.test(u) && !(o && o.method)) {
      const body = /short-names$/.test(u) ? opts.shortNames : opts.aliases;
      if (body) return Promise.resolve({ status: 200, json: () => Promise.resolve(body) });
      if (!opts.fetch) return Promise.resolve({ status: 404 });
    }
    return opts.fetch ? opts.fetch(u, o) : Promise.reject(new Error('нет сети'));
  };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: opts.host || '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR, fetch, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) { const D = loadRun(ctx); if (opts.mutate) opts.mutate(D); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.CONCEPTS_RUN, urls, ready: ctx.window.MOCK.shortNamesReady };
}
const ISIN = 'SU26242RMFS0', ISIN2 = 'SU26239RMFS6';
const withExch = D => { D.positions.forEach(d => { if (d.isin === ISIN) d.extra = Object.assign({}, d.extra, { short_name: 'ОФЗ 26242' }); if (d.isin === ISIN2) d.extra = Object.assign({}, d.extra, { short_name: 'ОФЗ 26239' }); }); };

t('прогон: биржевое короткое имя из extra.short_name, своё по ISIN — поверх; отдельно от алиаса; поиск по обоим', async () => {
  const at = '2026-11-10T09:00:00+00:00';
  const { M, K, urls, ready } = load({ real: true, mutate: withExch, shortNames: { short_names: [{ isin: ISIN, short_name: 'Моя 29', changed_by: 'boss', changed_at: at }] } });
  const L = M.positions.filter(p => p.isin === ISIN), O = M.positions.filter(p => p.isin === ISIN2);
  assert.ok(L.length && O.length);
  // запрос уходит при загрузке, имена подставляются по ответу: до него — биржевое
  for (const p of L) { assert.strictEqual(p.exchShort, 'ОФЗ 26242'); assert.strictEqual(p.shortName, 'ОФЗ 26242'); assert.strictEqual(p.shortOwn, null); }
  const ev = []; K.on('alias', () => ev.push('alias')); K.onFilter(() => ev.push('filter'));
  await ready;
  assert.deepStrictEqual(ev, ['alias'], 'короткие имена пришли: событие alias, без filter');
  assert.ok(urls.some(u => u.endsWith('../api/v1/instruments/short-names')), urls.join(' '));
  for (const p of L) {
    assert.strictEqual(p.shortName, 'Моя 29'); assert.strictEqual(p.shortOwn, 'Моя 29'); assert.strictEqual(p.exchShort, 'ОФЗ 26242');
    assert.strictEqual(p.name, 'ОФЗ 26242', 'полное имя (алиас) не тронуто'); assert.strictEqual(p.alias, null);
    assert.ok(p.searchText.startsWith('моя 29 ') && p.searchText.includes('su26242rmfs0'), p.searchText);
    assert.ok(p.searchText.includes('офз 26242'), 'поиск и по биржевому');
  }
  assert.deepStrictEqual(JSON.parse(JSON.stringify(M.shortNames[ISIN])), { shortName: 'Моя 29', changedBy: 'boss', changedAt: at });
  for (const p of O) { assert.strictEqual(p.shortName, 'ОФЗ 26239'); assert.strictEqual(p.shortOwn, null); }
  // нет короткого имени ни своего, ни биржевого — null
  const none = M.positions.find(p => p.isin !== ISIN && p.isin !== ISIN2);
  assert.strictEqual(none.shortName, null); assert.strictEqual(none.exchShort, null);
});

t('прогон: сервер без коротких имён (или без ответа) — биржевые, страница работает', async () => {
  const { M, ready } = load({ real: true, mutate: withExch });
  await ready;
  assert.deepStrictEqual(Object.keys(M.shortNames), []);
  assert.ok(M.positions.every(p => p.shortOwn === null && p.shortName === p.exchShort));
});

t('K.setShort: все позиции бумаги, поиск и события; пустое — снова биржевое; алиас не затронут', () => {
  const { M, K } = load({ real: true, mutate: withExch });
  const L = M.positions.filter(p => p.isin === ISIN);
  K.setAlias(ISIN, 'Длинный алиас');
  const ev = []; K.on('alias', () => ev.push('alias')); K.onFilter(() => ev.push('filter'));
  K.setShort(ISIN, 'Кор', { changedBy: 'boss' });
  assert.deepStrictEqual(ev, ['alias', 'filter']);
  L.forEach(p => { assert.strictEqual(p.shortName, 'Кор'); assert.strictEqual(p.name, 'Длинный алиас'); assert.ok(p.searchText.startsWith('длинный алиас кор ') && p.searchText.includes('офз 26242'), p.searchText); });
  assert.strictEqual(M.shortNames[ISIN].shortName, 'Кор');
  K.setShort(ISIN, null);
  L.forEach(p => { assert.strictEqual(p.shortName, 'ОФЗ 26242'); assert.strictEqual(p.shortOwn, null); assert.ok(p.searchText.startsWith('длинный алиас офз 26242')); });
  assert.ok(!(ISIN in M.shortNames));
  K.setAlias(ISIN, null);
  L.forEach(p => assert.strictEqual(p.searchText, p.searchBase));
});

t('колонка «Кор. имя» и строка карточки: своё — с биржевым в подсказке, нет имени — «—», без ISIN строки нет', () => {
  const { M, K } = load({ real: true, mutate: withExch });
  const col = K.posCols.short;
  assert.strictEqual(col.label, 'Кор. имя'); assert.strictEqual(col.key, 'shortName');
  const p = M.positions.find(x => x.isin === ISIN);
  assert.ok(/^<span data-ps="[^"]+">ОФЗ 26242<\/span>$/.test(K.shortCell(p, p.shortName)), K.shortCell(p, p.shortName));
  K.setShort(ISIN, 'Моя <29>');
  const html = K.shortCell(p, p.shortName);
  assert.ok(html.includes('&lt;29&gt;') && html.includes('title="биржевое короткое имя: ОФЗ 26242"'), html);
  const q = M.positions.find(x => x.isin && !x.exchShort);
  assert.ok(q, 'бумага без биржевого короткого имени');
  assert.ok(K.shortCell(q, q.shortName).includes('—'));
  assert.strictEqual(K.shortCell({ id: 'x', shortName: null }, null), '<span class="mut3">—</span>', 'строка вне прогона — без пометки');
  const row = K.shortRow(p);
  assert.ok(row.includes('Короткое имя') && row.includes('data-al="edit"') && row.includes('data-al="hist"') && row.includes('Моя &lt;29&gt;'), row);
  assert.ok(row.includes('title="изменить короткое имя бумаги'), 'подсказка ✎ — про короткое имя, не про алиас');
  const der = M.positions.find(x => !x.isin);
  assert.strictEqual(K.shortRow(der), '', 'без ISIN — не править');
  assert.ok(!K.aliasName(p).includes('Короткое имя'), 'заголовок карточки — прежний, алиас');
});

t('прогон: правка — PUT /api/v1/instruments/short-name с JSON, история — GET; права общие с алиасом', async () => {
  const calls = [];
  const fetch = (u, o) => { if (!/instruments\/(aliases|short-names)$/.test(u)) calls.push([u, o]); return Promise.resolve({ status: 200, json: () => Promise.resolve({ isin: ISIN, short_name: 'X', changed_by: 'local', changed_at: 'z', changed: true }) }); };
  const { M } = load({ real: true, fetch });
  const r = await M.shortNameApi.put(ISIN, 'X');
  assert.strictEqual(r.status, 200); assert.strictEqual(r.body.short_name, 'X');
  const [u, o] = calls[0];
  assert.strictEqual(u, '../api/v1/instruments/short-name'); assert.strictEqual(o.method, 'PUT');
  assert.strictEqual(o.headers['Content-Type'], 'application/json'); assert.deepStrictEqual(JSON.parse(o.body), { isin: ISIN, short_name: 'X' });
  await M.shortNameApi.history(ISIN);
  assert.strictEqual(calls[1][0], '../api/v1/instruments/short-names/' + ISIN + '/history');
  // права: чужой адрес — только чтение, вход включён — трейдер правит (TDM#777); общие для двух имён
  const net = load({ real: true, host: '192.168.1.20', mutate: withExch });
  assert.strictEqual(net.M.shortNameApi.editable, false);
  const p = net.M.positions.find(x => x.isin === ISIN);
  assert.ok(/data-al="edit" disabled title="только чтение: [^"]*127\.0\.0\.1/.test(net.K.shortRow(p)), net.K.shortRow(p));
  net.M.nameRights.setAuth({ enabled: true, user: { username: 'u', role: 'trader' }, loopback: false });
  assert.strictEqual(net.M.shortNameApi.editable, true); assert.strictEqual(net.M.aliasApi.editable, true);
  assert.ok(!/disabled/.test(net.K.shortRow(p).match(/<button class="k-ico" data-al="edit"[^>]*>/)[0]));
  net.M.nameRights.setAuth({ enabled: true, user: { username: 'v', role: 'viewer' }, loopback: false });
  assert.ok(/disabled title="только чтение: роль «просмотр»/.test(net.K.shortRow(p)));
  const off = load({ real: true, fetch: () => Promise.reject(new Error('down')) });
  assert.strictEqual((await off.M.shortNameApi.put(ISIN, 'X')).status, 0, 'сеть недоступна — ответ, а не исключение');
});

t('демо: примерное короткое имя, биржевое — начало имени; M.shortNameApi как сервер — до 40 знаков, сброс, история', async () => {
  const { M, K } = load();
  const p = M.positions.find(x => x.isin === 'RU000A105RL4');
  assert.strictEqual(p.shortOwn, 'Россия-43'); assert.strictEqual(p.shortName, 'Россия-43'); assert.strictEqual(p.exchShort, p.exchName.slice(0, 10).trim());
  assert.ok(p.searchText.includes('россия-43'));
  const q = M.positions.find(x => x.isin && x.isin !== 'RU000A105RL4');
  assert.strictEqual(q.shortName, q.exchShort); assert.ok(q.exchShort);
  assert.strictEqual(M.positions.find(x => !x.isin).shortName, null);
  assert.strictEqual(K.posCols.short.fmt(p.shortName, p).includes('Россия-43'), true);
  const isin = 'RU000A105RL4';
  let r = await M.shortNameApi.put(isin, '  Россия   43 ');
  assert.strictEqual(r.status, 200); assert.strictEqual(r.body.short_name, 'Россия 43'); assert.strictEqual(r.body.changed, true);
  assert.strictEqual((await M.shortNameApi.put(isin, 'Россия 43')).body.changed, false);
  r = await M.shortNameApi.put(isin, '   ');
  assert.strictEqual(r.body.short_name, null);
  assert.strictEqual((await M.shortNameApi.put(isin, 'x'.repeat(41))).status, 422);
  assert.strictEqual((await M.shortNameApi.put(isin, 'a\u0001b')).status, 422);
  assert.strictEqual((await M.shortNameApi.put(isin, 'x'.repeat(40))).status, 200);
  const h = await M.shortNameApi.history(isin);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(h.map(x => x.short_name))), ['x'.repeat(40), null, 'Россия 43', 'Россия-43'], 'новые сверху');
  assert.ok(h.every(x => x.changed_by && x.changed_at && x.rev_id));
});

(async () => {
  for (const [name, f] of tests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
