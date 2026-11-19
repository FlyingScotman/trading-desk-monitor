// TDM#802: колонка «Имя» таблиц позиций — короткое имя бумаги (своё по ISIN ?? биржевое), где его нет — обычное название (алиас ?? биржевое).
// Сортировка, поиск и CSV — по показанному тексту; у короткого имени подсказка — полное название; имя изменили — ячейка следует за ним.
//   node tests/js/name_col_802.test.js
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

// у ISIN полное название длиннее биржевого короткого; у ISIN2 название и короткое имя одинаковы
const FULL = 'ОФЗ-ПД 26242 с постоянным купонным доходом';
const withFull = D => { withExch(D); D.positions.forEach(d => { if (d.isin === ISIN) d.name = FULL; }); };

t('значение: короткое имя, иначе название; своё короткое — поверх биржевого; алиас — название', () => {
  const { M, K } = load({ real: true, mutate: withFull });
  const col = K.posCols.nameShort;
  assert.strictEqual(col.label, 'Имя'); assert.strictEqual(col.key, 'nameShort');
  const p = M.positions.find(x => x.isin === ISIN);
  assert.strictEqual(col.val(p), 'ОФЗ 26242', 'биржевое короткое имя');
  K.setShort(ISIN, 'Моя 29');
  assert.strictEqual(col.val(p), 'Моя 29', 'своё короткое поверх биржевого');
  K.setShort(ISIN, null);
  assert.strictEqual(col.val(p), 'ОФЗ 26242');
  // нет короткого имени — обычное название: деривативы (без ISIN) и бумага без биржевого короткого имени
  const der = M.positions.find(x => !x.isin);
  assert.strictEqual(der.shortName, null); assert.strictEqual(col.val(der), der.name); assert.ok(der.name);
  const q = M.positions.find(x => x.isin && !x.exchShort);
  assert.ok(q, 'бумага с ISIN без биржевого короткого имени'); assert.strictEqual(col.val(q), q.name);
  // алиас меняет «обычное название», не короткое имя
  K.setAlias(q.isin, 'Моё полное');
  assert.strictEqual(q.name, 'Моё полное'); assert.strictEqual(col.val(q), 'Моё полное');
  K.setAlias(ISIN, 'Алиас полный');
  assert.strictEqual(col.val(p), 'ОФЗ 26242', 'короткое имя есть — алиас не показан');
  assert.strictEqual(K.shownName(null), ''); assert.strictEqual(K.shownName({ shortName: '', name: 'N' }), 'N');
});

t('ячейка: у короткого имени подсказка — полное название (и биржевое короткое при своём); без короткого — текст без подсказки; экранирование; data-pi', () => {
  const { M, K } = load({ real: true, mutate: withFull });
  const p = M.positions.find(x => x.isin === ISIN), col = K.posCols.nameShort;
  let html = col.fmt(col.val(p), p);
  assert.ok(html.startsWith('<span data-pi="' + p.id + '" title="полное название: '), html);
  assert.strictEqual(p.name, FULL);
  assert.ok(html.includes('>ОФЗ 26242</span>') && html.includes('полное название: ' + FULL + '"') && !html.includes('биржевое короткое имя'), html);
  K.setShort(ISIN, 'Моя <29>');
  html = col.fmt(col.val(p), p);
  assert.ok(html.includes('Моя &lt;29&gt;') && !html.includes('<29>') && html.includes('биржевое короткое имя: ОФЗ 26242'), html);
  const same = M.positions.find(x => x.isin === ISIN2);
  assert.strictEqual(col.fmt(col.val(same), same), '<span data-pi="' + same.id + '">ОФЗ 26239</span>', 'короткое равно названию — подсказки нет');
  const q = M.positions.find(x => x.isin && !x.exchShort);
  assert.strictEqual(col.fmt(col.val(q), q), '<span data-pi="' + q.id + '">' + q.name.replace(/&/g, '&amp;') + '</span>', 'название — без подсказки: она была бы им же');
  assert.strictEqual(K.shownNameCell({ id: 'x', shortName: null, name: null }, ''), '<span class="mut3">—</span>');
  const der = M.positions.find(x => !x.isin);
  assert.ok(!/data-pi/.test(col.fmt(col.val(der), der)), 'строка без ISIN — без пометки');
  assert.strictEqual(K.shownNameTip({ shortName: 'А', name: 'А' }), '', 'короткое равно названию — подсказки нет');
});

t('таблица: сортировка, поиск и CSV — по показанному тексту', () => {
  const { M, K } = load({ real: true, mutate: withFull });
  K.setShort(ISIN, 'Ааа своя');
  const rows = M.positions.filter(x => x.isin === ISIN || x.isin === ISIN2 || !x.isin).slice(0, 12);
  assert.ok(rows.length >= 3 && rows.some(r => r.isin === ISIN));
  const api = K.table(el(), { rows, columns: K.cols(['name', 'nameShort']), search: false, viewKey: 'n802' });
  api.setSort([['nameShort', 1]]);
  const lines = api.csv().replace(/^\ufeff/, '').split(/\r?\n/).filter(Boolean);
  const cells = l => { const o = []; let c = '', q = false; for (const ch of l) { if (ch === '"') q = !q; else if (ch === ',' && !q) { o.push(c); c = ''; } else c += ch; } return o.concat(c); };
  const hi = lines.findIndex(l => /Имя/.test(l)), at = cells(lines[hi]).findIndex(h => h.includes('Имя'));
  assert.ok(at > 0, lines[hi]);
  const col = lines.slice(hi + 1).map(l => cells(l)[at]).filter(v => v != null && v !== '');
  const shown = rows.map(r => K.shownName(r));
  assert.ok(col.length >= rows.length, 'строки таблицы: ' + col.length);
  for (const v of col.slice(0, rows.length)) assert.ok(shown.includes(v), 'в CSV показанный текст: ' + v);
  assert.strictEqual(col[0], 'Ааа своя', 'сортировка по показанному, не по названию бумаги');
  // поиск (searchText) находит и по короткому, и по названию: показанное — всегда одно из двух
  const p = rows.find(x => x.isin === ISIN);
  assert.ok(p.searchText.includes('ааа своя') && p.searchText.includes(p.exchShort.toLowerCase()));
  assert.ok(rows.every(r => r.searchText.includes(K.shownName(r).toLowerCase())), 'показанный текст есть в поиске');
});

t('каталог и наборы: «Имя» в «Добавить» всех таблиц позиций на K.cols; в «Базовый» C03 и в кокпите вместо «Кор. имя»; «Кор. имя» осталась в каталоге, «Статике» и «Всё»', () => {
  const { K } = load({ real: true });
  assert.ok(Object.values(K.posCols).some(c => c.key === 'nameShort'), 'каталог K.posCols');
  assert.strictEqual(K.cols(['name', 'nameShort'])[1].label, 'Имя');
  const c03 = P('concepts/c03-blotter.html'), c01 = P('concepts/c01-cockpit.html');
  const preset = id => (c03.match(new RegExp("\\['" + id + "', '[^']+', \\[([^\\]]+)\\]")) || [])[1];
  assert.ok(/^'name', 'nameShort', 'ccy'/.test(preset('base')) && !/'short'/.test(preset('base')), preset('base'));
  assert.ok(/'name', 'short', 'nameShort', 'isin'/.test(preset('stat')) && /'name', 'short', 'nameShort', 'isin'/.test(preset('all')));
  for (const id of ['risk', 'px', 'pnl']) assert.ok(!/nameShort/.test(preset(id)), id);
  assert.ok(/K\.cols\(\['name', 'nameShort', 'ccy'/.test(c01) && !/K\.cols\(\['name', 'short'/.test(c01));
  assert.ok(/nameShort: \{ label: 'Имя', val: r => K\.shownName\(r\)/.test(c03), 'каталог C у C03');
});

t('демо: колонка работает и на данных демо', () => {
  const { M, K } = load();
  const col = K.posCols.nameShort;
  const p = M.positions.find(x => x.isin === 'RU000A105RL4');
  assert.strictEqual(col.val(p), 'Россия-43'); assert.ok(K.shownNameTip(p).startsWith('полное название: '));
  const der = M.positions.find(x => !x.isin); assert.strictEqual(col.val(der), der.name);
});

(async () => {
  for (const [name, f] of tests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
