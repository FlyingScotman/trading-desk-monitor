// Батч 1, п. 3 (R33): редактируемый алиас бумаги в мониторах (prototypes/concepts/). Имя бумаги — алиас ?? биржевое,
// по ISIN, на прогоне — из GET /api/v1/instruments/aliases (lib/real.js), в демо — в памяти (lib/mock.js); поиск — по
// обоим именам; правка — PUT /api/v1/instruments/alias (JSON), 403 — только чтение; K.setAlias меняет все позиции бумаги.
//   node tests/js/alias_ui.test.js
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
// opts.aliases — ответ GET /api/v1/instruments/aliases (приходит асинхронно: await ready); opts.fetch — заглушка fetch для
// остальных запросов; opts.host — location.hostname. Синхронных запросов нет (TDM#629): XMLHttpRequest бросает.
function load(opts) {
  opts = opts || {};
  const handlers = {};  // события document: real.js шлёт concepts-data, kit.js слушает (TDM#629)
  const document = { addEventListener(t, f) { (handlers[t] = handlers[t] || []).push(f); }, dispatchEvent(e) { (handlers[e.type] || []).forEach(f => f(e)); }, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const urls = [];
  class XHR { constructor() { throw new Error('синхронный запрос запрещён (TDM#629)'); } }
  const fetch = (u, o) => {
    urls.push(u);
    if (/instruments\/aliases$/.test(u) && !(o && o.method)) {
      if (opts.aliases) return Promise.resolve({ status: 200, json: () => Promise.resolve(opts.aliases) });
      if (!opts.fetch) return Promise.resolve({ status: 404 });
    }
    if (/instruments\/short-names$/.test(u) && !(o && o.method)) {  // свои короткие имена (TDM#776), приходят тоже позже страницы
      if (opts.shortNames) return Promise.resolve({ status: 200, json: () => Promise.resolve(opts.shortNames) });
      if (!opts.fetch) return Promise.resolve({ status: 404 });
    }
    return opts.fetch ? opts.fetch(u, o) : Promise.reject(new Error('нет сети'));
  };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: opts.host || '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR, fetch, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) { loadRun(ctx); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.CONCEPTS_RUN, urls, ready: ctx.window.MOCK.aliasesReady, shortReady: ctx.window.MOCK.shortNamesReady };
}

t('прогон: алиас по ISIN — вместо биржевого имени, поиск по обоим, бумаги без алиаса как были', async () => {
  const at = '2026-10-22T09:00:00+00:00';
  const { M, K, D, urls, ready } = load({ real: true, aliases: { aliases: [{ isin: 'SU26242RMFS0', alias: 'ОФЗ-2029 (26242)', changed_by: 'local', changed_at: at }] } });
  // запрос уходит при загрузке, а имена подставляются по ответу (TDM#629): до него — биржевые, по ответу — алиас и событие страницам
  const ev = []; K.on('alias', () => ev.push('alias')); K.onFilter(() => ev.push('filter'));
  assert.ok(M.positions.every(p => p.alias === null));
  await ready;
  // по приходу — только alias (имена в таблицах меняет K.patchNames); filter перерисовывал бы тяжёлые страницы целиком (ревью TDM#629)
  assert.deepStrictEqual(ev, ['alias'], 'алиасы пришли: событие alias, без filter');
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

t('прогон: сервер без алиасов (или без ответа) — биржевые имена, страница работает', async () => {
  const { M, ready } = load({ real: true });
  await ready;
  assert.deepStrictEqual(Object.keys(M.aliases), []);
  assert.ok(M.positions.every(p => p.alias === null && p.name === p.exchName));
});

t('прогон: правка — PUT /api/v1/instruments/alias с JSON; с чужого адреса — только чтение без запроса', async () => {
  const calls = [];
  const fetch = (u, o) => { if (!/instruments\/(aliases|short-names)$/.test(u)) calls.push([u, o]); return Promise.resolve({ status: 200, json: () => Promise.resolve({ isin: 'SU26242RMFS0', alias: 'X', changed_by: 'local', changed_at: 'z', changed: true }) }); };
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
  // ответа входа ещё нет: причина нейтральная, не «вход выключен» (TDM#777)
  assert.ok(/data-al="edit" disabled title="только чтение: права на правку уточняются — обновите страницу, если не исчезнет"/.test(html), html);
  ro.M.nameRights.setAuth({ enabled: false, user: { username: 'local', role: 'admin' }, loopback: false });
  assert.ok(/data-al="edit" disabled title="только чтение: вход выключен[^"]*127\.0\.0\.1/.test(ro.K.aliasName(p)), ro.K.aliasName(p));
  assert.ok(html.includes('data-al="hist"'), 'история — и в режиме только чтения');
  // сеть недоступна — ответ, а не исключение
  const off = load({ real: true, fetch: () => Promise.reject(new Error('down')) });
  assert.strictEqual((await off.M.aliasApi.put('SU26242RMFS0', 'X')).status, 0);
});

t('права на правку (TDM#777): вход включён — трейдер и администратор с любого компьютера, «просмотр» — только чтение, вход выключен — компьютер сервера', () => {
  const st = (enabled, role, loopback) => ({ enabled, user: enabled ? (role ? { username: 'u', role } : null) : { username: 'local', role: 'admin' }, loopback });
  const net = () => load({ real: true, host: '192.168.1.20' });
  const btn = ({ M, K }) => K.aliasName(M.positions.find(x => x.isin)).match(/<button class="k-ico" data-al="edit"[^>]*>/)[0];
  for (const role of ['trader', 'admin']) {  // с другого компьютера по сети
    const x = net();
    assert.strictEqual(x.M.aliasApi.editable, false, 'до ответа входа — по адресу страницы, как раньше');
    x.M.nameRights.setAuth(st(true, role, false));
    assert.strictEqual(x.M.aliasApi.editable, true, role);
    assert.ok(!/disabled/.test(btn(x)), role + ': ✎ доступна');
  }
  const viewer = net();
  viewer.M.nameRights.setAuth(st(true, 'viewer', false));
  assert.strictEqual(viewer.M.aliasApi.editable, false);
  assert.ok(/disabled title="только чтение: роль «просмотр»[^"]*«трейдер»/.test(btn(viewer)), btn(viewer));
  // даже со своего компьютера: роль решает, а не адрес
  const viewerHere = load({ real: true, host: '127.0.0.1' });
  viewerHere.M.nameRights.setAuth(st(true, 'viewer', true));
  assert.strictEqual(viewerHere.M.aliasApi.editable, false);
  // вход выключен: только компьютер сервера (loopback от сервера, иначе — по адресу)
  const offNet = net();
  offNet.M.nameRights.setAuth(st(false, null, false));
  assert.strictEqual(offNet.M.aliasApi.editable, false);
  assert.ok(/disabled title="только чтение: вход выключен — переименовать можно только на компьютере[^"]*127\.0\.0\.1/.test(btn(offNet)), btn(offNet));
  const offHere = load({ real: true, host: 'localhost' });
  offHere.M.nameRights.setAuth(st(false, null, true));
  assert.strictEqual(offHere.M.aliasApi.editable, true);
  // ответа нет или он непонятный, вход включён без пользователя — права не меняются
  const keep = net();
  for (const bad of [null, undefined, {}, st(true, null, false)]) keep.M.nameRights.setAuth(bad);
  assert.strictEqual(keep.M.aliasApi.editable, false);
  // 403 сервера переводит в только чтение и у трейдера (роль сменили, пока страница была открыта)
  const late = net(); late.M.nameRights.setAuth(st(true, 'trader', false));
  late.M.aliasApi.editable = false;
  assert.ok(/disabled title="только чтение: сервер отказал/.test(btn(late)), btn(late));
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
