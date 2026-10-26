// CSV мониторов и карточка позиции (prototypes/concepts): «Спред» — числом, вид спреда — колонкой только для CSV
// (K.table csvExtra, ручная таблица — data-csv с двумя подписями), C14 и C15 перерисовываются при смене «(1) / −1»
// (#276); CSV C03 без строк групп, «Цена 30д» — изменение цены за 30 дней числом (#277); потоки карточки без
// графика выпуска — «потоков нет» с причиной (#288).
//   node tests/js/ui_pack5_csv.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const T = [];
const t = (name, f) => T.push([name, f]);
const same = (a, b, m) => assert.strictEqual(JSON.stringify(a), JSON.stringify(b), m);  // объекты из vm — другого realm

function el(tag) {
  const e = { nodeType: 1, tagName: (tag || 'div').toUpperCase(), style: {}, dataset: {}, children: [], innerHTML: '', className: '', isConnected: true, parent: null, get parentElement() { return e.parent; },
    classList: { add(c) { if (!e.classList.contains(c)) e.className = (e.className + ' ' + c).trim(); }, remove(c) { e.className = e.className.split(' ').filter(x => x !== c).join(' '); },
      toggle() {}, contains(c) { return e.className.split(' ').includes(c); } },
    appendChild(c) { c.parent = e; e.children.push(c); return c; }, append(...c) { c.forEach(x => e.appendChild(x)); },
    remove() { if (e.parent) { const i = e.parent.children.indexOf(e); if (i >= 0) e.parent.children.splice(i, 1); } },
    after(c) { if (c.parent) c.remove(); const p = e.parent; c.parent = p; p.children.splice(p.children.indexOf(e) + 1, 0, c); },
    get nextElementSibling() { const p = e.parent; return p ? p.children[p.children.indexOf(e) + 1] || null : null; },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, getBoundingClientRect: () => ({ width: 80 }) };
  return e;
}
// ручная таблица из описания строк: [[html | {h, cls, span, csv, csvN}]], строка {cells, tr: {dataset}}
function table(spec) {
  const Tb = el('table');
  const cell = (tag, c) => { const x = el(tag); if (typeof c === 'string') x.innerHTML = c; else { x.innerHTML = c.h || ''; x.className = c.cls || ''; x.colSpan = c.span || 1; if (c.csv != null) x.dataset.csv = c.csv; if (c.csvN) x.dataset.csvN = ''; } if (!x.colSpan) x.colSpan = 1; return x; };
  const row = (tag, cells, o) => { const r = el('tr'); r.cells = cells.map(c => cell(tag, c)); r.cells.forEach(c => r.appendChild(c)); if (o) Object.assign(r.dataset, o.dataset || {}); return r; };
  const sec = rows => rows ? { rows } : null;
  Tb.tHead = sec(spec.head.map(h => row('th', h)));
  Tb.tBodies = [sec(spec.body.map(b => Array.isArray(b) ? row('td', b) : row('td', b.cells, b.tr)))];
  Tb.tFoot = null;
  el('div').appendChild(Tb);
  return Tb;
}
function load() {
  const store = new Map();
  const document = { addEventListener() {}, removeEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const rowsCsv = s => s.replace(/(\r\n)+$/, '').split('\r\n').slice(2);  // без строки прогона, пустой строки и перевода строки в конце

t('#276: K.table — «Спред» в CSV числом, вид спреда — колонкой только для CSV; одинаково в «(1)» и «−1»', () => {
  const { K } = load();
  const rows = [{ id: 'a', ccy: 'RUB', zspread: -27, spreadLabel: 'Z' }, { id: 'b', ccy: 'RUB', zspread: 1390, spreadLabel: 'DM' }, { id: 'c', ccy: 'USD', zspread: null, spreadLabel: '' }];
  const out = {};
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const api = K.table(el(), { rows, search: false, viewKey: 'c99-spr-' + fm, columns: K.cols(['ccy', 'spread']) });
    out[fm] = api.csv();
    same(rowsCsv(out[fm]), ['Вал,"Спред, б.п.",Вид спреда', 'RUB,-27,Z', 'RUB,1390,DM', 'USD,,'], fm);
    // колонка только для CSV — не колонка экрана: в раскладке (#102) её нет, перенос «Спреда» переносит и её
    same(api.layout(), {}, fm);
    api.setLayout({ order: ['zspread', 'ccy'] });
    same(rowsCsv(api.csv())[0], '"Спред, б.п.",Вид спреда,Вал', fm);
  }
  K.negFmt = 'acc';
  assert.strictEqual(out.acc, out.minus, 'CSV не зависит от формата отрицательных');
  // колонка без csvExtra и csvFmt-строка — как раньше; лишнее значение массива без подписи не выгружается
  const plain = K.table(el(), { rows: [{ id: 'a', x: -5 }], search: false, viewKey: 'c99-plain', columns: [{ key: 'x', label: 'X', num: true, csvFmt: v => [K.f.n(v, 0), 'лишнее'] }] });
  same(rowsCsv(plain.csv()), ['X', '-5']);
});

t('#276: ручная таблица C03 — «Спред» и «Вид спреда» из data-csv; вид спреда на странице и в K.posCols', () => {
  const { K } = load();
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const csv = (v, l) => K.htmlText(/ data-csv="([^"]*)"/.exec(K.csvv(() => v == null ? ['', ''] : [K.f.bp(v), l]))[1]);
    const Tb = table({ head: [['Бумага', { h: 'Спред', cls: 'n', csv: 'Спред, б.п.|Вид спреда' }]],
      body: [['А', { h: K.f.bp(-27) + ' <span class="mut3">Z</span>', cls: 'n', csv: csv(-27, 'Z') }], ['Б', { h: '139 <span class="mut3">DM</span>', cls: 'n', csv: csv(139, 'DM') }], ['В', { h: '—', cls: 'n', csv: csv(null) }]] });
    same(rowsCsv(K.tableCsv(Tb, {})), ['Бумага,"Спред, б.п.",Вид спреда', 'А,-27,Z', 'Б,139,DM', 'В,,'], fm);
  }
  K.negFmt = 'acc';
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/spread: \{ label: 'Спред'[^\n]*\n\s+csvHead: 'Спред, б\.п\.\|Вид спреда', csvFmt: \(v, r\) => v == null \? \['', ''\] : \[f\.bp\(v\), r\.spreadLabel \|\| ''\] \}/.test(c03), 'C03');
  assert.ok(/csvExtra: \['Вид спреда'\], csvFmt: \(v, r\) => v == null \? \['', ''\] : \[f\.bp\(v\), r\.spreadLabel \|\| ''\]/.test(P('concepts/lib/kit.js')), 'K.posCols.spread');
});

t('#276: C14 и C15 перерисовываются при смене «(1) / −1», как C16 (#262); не поверх сообщения об ошибке', () => {
  for (const f of ['c14-day-status.html', 'c15-cash.html']) {
    assert.ok(P('concepts/' + f).includes("K.on('numfmt', () => { if (doc && view.querySelector('.k-grid')) render(); });"), f);
  }
  assert.ok(P('concepts/c16-ocp.html').includes("K.on('numfmt', () => { if (last) render(last); });"));
});

t('#277: CSV C03 — строки групп не выгружаются, «Цена 30д» — изменение за 30 дней числом с заголовком', () => {
  const { K } = load();
  for (const fm of ['acc', 'minus']) {
    K.negFmt = fm;
    const d = v => K.htmlText(/ data-csv="([^"]*)"/.exec(K.csvv(() => v == null ? '' : K.f.n(v, 3)))[1]);
    const Tb = table({ head: [['', 'Бумага', { h: 'Цена 30д', csv: 'Δ цены за 30д, % ном.' }]],
      body: [{ cells: [{ h: '' }, { h: '<span class="gl"><span class="tw">▾</span>RUB Рубли <span class="mut3">2</span></span>', span: 2 }], tr: { dataset: { csvSkip: '1' } } },
        [{ h: '' }, 'А', { h: '<svg class="spark"></svg>', csv: d(-1.2346), csvN: true }],
        [{ h: '' }, 'Б', { h: '<svg class="spark"></svg>', csv: d(0.5), csvN: true }],
        [{ h: '' }, 'В', { h: '<span class="mut3">—</span>', csv: d(null), csvN: true }]] });
    same(rowsCsv(K.tableCsv(Tb, {})), ['Бумага,"Δ цены за 30д, % ном."', 'А,-1.235', 'Б,0.500', 'В,'], fm);
  }
  K.negFmt = 'acc';
  // без data-csv-n нечисловая ячейка с «-1.235» была бы защищена от формулы — текстом
  const Tx = table({ head: [['X']], body: [[{ h: '', csv: '-1.235' }]] });
  same(rowsCsv(K.tableCsv(Tx, {}))[1], '"=""-1.235"""');
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(c03.includes(`'" data-g="' + esc(p) + '" data-csv-skip="1"><td class="stripe"`), 'строка группы C03 — data-csv-skip');
  assert.ok(c03.includes("csvHead: 'Δ цены за 30д, % ном.', csvNum: true, csvFmt: v => v == null ? '' : f.n(v, 3),"), '«Цена 30д»');
  assert.ok(c03.includes("(c.csvNum ? ' data-csv-n' : '')"), 'data-csv-n у ячейки');
});

t('#288: потоки карточки — графика выпуска нет: потоки прогона и причина, а без них «потоков нет: причина»', async () => {
  const { M, K } = load();
  const p = { id: 'PX', isin: 'RU000TEST001', name: 'Тест', ccy: 'RUB' };
  assert.strictEqual(K.flowsTable(p, [], false, 'нет статики хаба за дату прогона'), '<div class="k-empty">потоков нет: нет статики хаба за дату прогона</div>');
  assert.strictEqual(K.flowsTable(p, [], false), '<div class="k-empty">потоков нет</div>');
  assert.strictEqual(K.flowsTable(p, [], true), '<div class="k-empty">загрузка потоков…</div>');
  const own = [{ date: '2027-03-15', pos: 'PX', kind: 'cpn', amount: 1000, amountRub: 1e6 }];
  const h = K.flowsTable(p, own, false, 'график выпуска не загружен: 500');
  assert.ok(h.startsWith('<div class="mut" style="font-size:11px;margin-bottom:4px">Весь график выпуска недоступен: график выпуска не загружен: 500. Показаны потоки прогона.</div>') && h.includes('k-cflows'));
  // K.flows: ответ M.posFlows {flows, reason, error}
  const run = async (ans, cfs) => {
    M.cashflows = cfs; M.posFlows = () => Promise.resolve(ans);
    const box = el(); K.flows(box, p); await new Promise(r => setTimeout(r, 0));
    return box.innerHTML;
  };
  const saved = [M.cashflows, M.posFlows];
  assert.strictEqual(await run({ flows: null, reason: 'не облигация' }, []), '<div class="k-empty">потоков нет: не облигация</div>');
  assert.ok((await run({ flows: null, error: 'график выпуска не загружен: 502' }, own)).includes('Весь график выпуска недоступен: график выпуска не загружен: 502.'));
  const full = await run({ flows: own.concat([{ date: '2028-03-15', pos: 'PX', kind: 'mat', amount: 1e5, amountRub: 1e8 }]) }, own);
  assert.ok(full.includes('2 потоков') && !full.includes('недоступен'));
  [M.cashflows, M.posFlows] = saved;
  // real.js: 404 — «нет данных», а не сбой; код причины сервера — словами
  const real = P('concepts/lib/real.js');
  assert.ok(real.includes("if (r.status === 404) return { flows: null, reason:") && real.includes("if (!r.ok) return { flows: null, error:"));
  assert.ok(/no_static: 'нет статики хаба за дату прогона'/.test(real) && real.includes('FLOW_REASON[j && j.reason]'));
});

(async () => {
  for (const [name, f] of T) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
