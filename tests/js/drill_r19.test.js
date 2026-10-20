// R19 «из чего состоит число» и R21 «Прочее» в PnL: клик по числу открывает панель DESK.drill (prototypes/desk-drill.js)
// с позициями, из которых оно сложено. Проверяется: панель открывается из KPI и из компоненты «Без разложения» вкладки PnL,
// строки складываются в показанное число (строки «Остаток/округление» нет), нет null/NaN; каждое кликабельное число каждой
// вкладки Desk Monitor, Widget Board и Scenario Lab сходится со своей панелью с точностью до округления показа.
//   node tests/js/drill_r19.test.js
// Страницы исполняются на заглушке DOM (как в pnl_r10_pages.test.js).
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

// ---------- заглушка DOM ----------
function fakeEl(name) {
  const kids = {};
  const el = { name, kids, innerHTML: '', textContent: '', value: '', checked: false, hidden: false, open: false, dataset: {}, style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} }, isConnected: true,
    clientWidth: 900, clientHeight: 400, offsetWidth: 900, offsetHeight: 400, scrollTop: 0, scrollLeft: 0,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, removeAttribute() {}, hasAttribute: () => false,
    focus() {}, blur() {}, click() {}, select() {}, closest: () => null, remove() {}, contains: () => false, scrollIntoView() {},
    querySelector: s => kids[s] || (kids[s] = fakeEl(name + ' ' + s)), querySelectorAll: () => [],
    appendChild: c => c, insertBefore: c => c, replaceChildren() {}, append() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 400, width: 900, height: 400 }) };
  Object.defineProperty(el, 'firstChild', { get: () => el.querySelector(':first-child') });
  return el;
}
const chartStub = () => ({ setOption() {}, on() {}, off() {}, resize() {}, dispose() {}, isDisposed: () => false, getWidth: () => 900, getHeight: () => 400,
  getDom: () => fakeEl('chart'), getZr: () => ({ on() {} }), containPixel: () => false, convertFromPixel: () => [0, 0], dispatchAction() {}, group: null });
function page(file, mutate, store) {
  const errors = [], con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(String).join(' ')) };
  const doc = fakeEl('document'), ls = Object.assign({}, store || {});
  Object.assign(doc, { documentElement: fakeEl('html'), body: fakeEl('body'), head: fakeEl('head'), activeElement: null, title: '',
    getElementById: id => doc.querySelector('#' + id), createElement: tag => fakeEl(tag), write() {} });
  const ctx = { window: {}, document: doc, console: con, URLSearchParams, Intl, Math, JSON, Date, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, innerWidth: 1400, innerHeight: 900, navigator: {},
    location: { protocol: 'file:', search: '', hash: '', href: 'file:///x' }, CSS: { escape: s => s },
    localStorage: { getItem: k => ls[k] ?? null, setItem(k, v) { ls[k] = String(v); }, removeItem(k) { delete ls[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '#808080', position: 'static' }), addEventListener() {}, removeEventListener() {},
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} }, MutationObserver: class { observe() {} disconnect() {} },
    echarts: { init: chartStub, getInstanceByDom: () => null, connect() {}, graphic: {} } };
  vm.createContext(ctx);
  ctx.window = ctx; ctx.self = ctx;
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.DESK);
  ['desk-util.js', 'desk-scope.js', 'desk-drill.js'].forEach(f => vm.runInContext(P(f), ctx));
  const html = P(file), start = html.lastIndexOf('<script>');
  vm.runInContext(html.slice(start + '<script>'.length, html.lastIndexOf('</script>')), ctx);
  assert.deepStrictEqual(errors, [], file + ': ошибки в консоли');
  return { $: s => doc.querySelector(s), D: ctx.DESK, ctx };
}

// Сервер (R21): «Прочее» поделено. Две позиции «без разложения» (оценка без денежных потоков), одна с невязкой цены.
const WHY_ND = 'без разложения: у оценки нет денежных потоков для пересчёта ядром (рыночная стоимость в оба дня); PnL = количество × (грязная цена D − грязная цена D−1) + выплаты';
const WHY_RES = 'невязка цены: рыночная грязная цена D минус стоимость вчерашних денежных потоков ядром при Z-спреде D';
const split = D => {
  const bonds = D.positions.filter(p => p.instrType === 'BOND');
  [[bonds[0], 2.5e6], [bonds[3], -0.8e6]].forEach(([p, v]) => {
    Object.assign(p.pnl, { noDecomp: v, residual: 0, other: v, otherWhy: WHY_ND, method: 'desk-flash-simple-v1', methodText: 'флэш: изменение грязной цены, без разложения' });
    p.pnl.explained += v; if (p.pnl.explainedCbr != null) p.pnl.explainedCbr += v; if (p.pnl.official != null) p.pnl.official += v;
    p.extra = Object.assign({}, p.extra, { quality: 'market_value' });
  });
  const r = bonds[5];
  Object.assign(r.pnl, { noDecomp: 0, residual: 1234, other: 1234, otherWhy: WHY_RES, method: 'desk-flash-exact-v2', methodText: 'флэш: пересчёт ядром по кривым бумаги' });
  r.pnl.explained += 1234; if (r.pnl.explainedCbr != null) r.pnl.explainedCbr += 1234; if (r.pnl.official != null) r.pnl.official += 1234;
};

// число из разметки: содержимое элемента с data-drill="key" до первого числа; null — «—»
function shown(html, key) {
  const i = html.indexOf('data-drill="' + key + '"'); if (i < 0) return null;
  const txt = html.slice(html.indexOf('>', i) + 1, html.indexOf('>', i) + 400).replace(/^(\s*<[^/][^>]*>)*/, '').split('<')[0];
  const m = /^\s*([+−-]?\d[\d\s  ]*(?:,\d+)?)/.exec(txt); if (!m) return null;
  const s = m[1].replace(/[\s  ]/g, ''), dec = (s.split(',')[1] || '').length;
  return { v: +s.replace('−', '-').replace('+', '').replace(',', '.'), dec };
}
const keys = html => [...new Set([...html.matchAll(/data-drill="([^"]+)"/g)].map(m => m[1]))];
// панель по ключу: строки складываются в итог, нет NaN/null; показанное число = итог панели с точностью до округления
function check(D, html, key, where) {
  const spec = D.drill.spec(key); assert.ok(spec, where + ': нет спецификации ' + key);
  const m = D.drill.model(spec);
  assert.ok(isFinite(m.total), where + ' ' + key + ': итог ' + m.total);
  m.rows.forEach(r => assert.ok(isFinite(r.value) && r.name && r.name !== 'null' && r.name !== 'undefined', where + ' ' + key + ': строка ' + JSON.stringify(r)));
  assert.strictEqual(m.rest, null, where + ' ' + key + ' «' + spec.title + '»: строки не сходятся с итогом, остаток ' + (m.rest && m.rest.value));
  const csv = D.drill.csv ? (D.drill.open(spec), D.drill.csv()) : '';
  assert.ok(!/NaN|undefined|null/.test(csv), where + ' ' + key + ': CSV ' + csv.slice(0, 200));
  const s = shown(html, key);
  if (s) assert.ok(Math.abs(s.v - m.total / m.scale) <= 0.5 * Math.pow(10, -s.dec) + 1e-9 * Math.abs(s.v) + 1e-12,
    where + ' ' + key + ' «' + spec.title + '»: показано ' + s.v + ', итог панели ' + m.total / m.scale);
  return m;
}

// ---------- модель панели ----------
t('модель: сортировка по |вкладу|, доли, остаток, группы с подытогами, поиск', () => {
  const { D } = page('desk-monitor.html');
  const rows = [{ id: 1, name: 'A', book: Object.keys(D.BOOKS)[0], value: 10 }, { id: 2, name: 'B', book: Object.keys(D.BOOKS)[1], value: -30 },
    { id: 3, name: 'C', book: Object.keys(D.BOOKS)[0], value: 25 }, { name: 'zero', value: 0 }, { name: 'nan', value: NaN }];
  const m = D.drill.model({ title: 'x', total: 10, rows });
  assert.strictEqual(m.rows.map(r => r.name).join(), 'B,C,A', 'ноль и NaN не строки; по |вкладу|');
  assert.strictEqual(m.rest.value, 5, 'итог 10, строки дают 5: остаток 5');
  assert.strictEqual(m.rows[0].share, -3);
  const g = D.drill.model({ title: 'x', rows }, { group: 'book' });
  assert.strictEqual(g.groups.length, 2); assert.strictEqual(g.rest, null);
  assert.strictEqual(g.groups.find(x => x.rows.some(r => r.name === 'A')).value, 35);
  assert.strictEqual(D.drill.model({ rows }, { q: 'c' }).shown.map(r => r.name).join(), 'C');
  assert.strictEqual(D.drill.model({ rows: [{ name: 'a', value: 1 }, { name: 'b', value: -1 }] }).rows[0].share, 0.5, 'итог 0 — доли от суммы модулей');
});

// ---------- Desk Monitor ----------
t('Desk Monitor: DV01 из KPI — позиции, сумма = показанному числу, без остатка', () => {
  const { $, D } = page('desk-monitor.html', split);
  const k = $('#kpis').innerHTML;
  assert.ok(k.includes('data-drill="kpi:dv01"') && k.includes('data-drill="kpi:pnl"'), k.slice(0, 300));
  const m = check(D, k, 'kpi:dv01', 'KPI');
  assert.strictEqual(m.total, D.positions.reduce((s, p) => s + p.dv01, 0));
  assert.ok(m.rows.length > 10 && m.rows.every(r => r.id != null && r.bookName));
  const f = check(D, k, 'kpi:pnl', 'KPI');
  assert.ok(f.rows.some(r => r.id == null && /Фондирование книги/.test(r.name)), 'фондирование книги — строками панели');
  assert.strictEqual(D.drill.last.spec.title, 'PnL дня · флэш (рынок)');
});

t('Desk Monitor, PnL: «Без разложения» и «Невязка цены» вместо «Прочего», клик — позиции с причиной', () => {
  const { $, D } = page('desk-monitor.html', split, { 'fidm.tab': 'pnl' });
  const side = $('#pnl-side').innerHTML;
  assert.ok(side.includes('Без разложения') && side.includes('Невязка цены') && !side.includes('>Прочее<'), side.slice(0, 600));
  assert.ok(side.includes(D.util.PNL_PART.noDecomp.tip.slice(0, 40)), 'подсказка «что это» у компоненты');
  const m = check(D, side, 'pnl:part:noDecomp', 'PnL');
  assert.strictEqual(m.total, 2.5e6 - 0.8e6);
  assert.strictEqual(JSON.stringify(m.rows.map(r => r.value)), JSON.stringify([2.5e6, -0.8e6]));
  assert.ok(m.rows.every(r => r.sub.includes(WHY_ND) && r.extra.quality === 'рыночная стоимость'), JSON.stringify(m.rows[0]));
  const res = check(D, side, 'pnl:part:residual', 'PnL');
  assert.strictEqual(res.rows.length, 1); assert.ok(res.rows[0].sub.includes(WHY_RES));
  // компоненты складываются во флэш: Σ по компонентам = флэш KPI
  const parts = keys(side).filter(k => k.startsWith('pnl:part:') || k.startsWith('pnl:fund:')).map(k => D.drill.model(D.drill.spec(k)).total);
  const flash = D.drill.model(D.drill.spec('pnl:flash')).total;
  assert.ok(Math.abs(parts.reduce((s, v) => s + v, 0) - flash) < 1e-3, parts + ' vs ' + flash);
});

const TABS = ['summary', 'positions', 'bonds', 'risk', 'expo', 'scen', 'pnl'];
const BODY = { summary: ['#sum-issuers'], positions: ['#pos-tw'], bonds: ['#bd-sum', '#bd-tw'], risk: ['#risk-hm', '#risk-extra'], expo: ['#ex-table', '#ex-conc', '#ex-ocp'],
  scen: ['#sc-cards', '#sc-table'], pnl: ['#pnl-side', '#pnl-books'] };
t('Desk Monitor: каждое кликабельное число каждой вкладки сходится со своей панелью', () => {
  let total = 0;
  TABS.forEach(tab => {
    const store = { 'fidm.tab': tab };
    if (tab === 'positions') store['fidm.cols'] = JSON.stringify(['book', 'name', 'mvRub', 'dv01', 'cs01', 'pnlExp', 'pnlOff', 'jtd']);
    const { $, D } = page('desk-monitor.html', split, store);
    const html = [$('#kpis').innerHTML].concat(BODY[tab].map(s => $(s).innerHTML)).join('\n'), ks = keys(html);
    assert.ok(ks.length > (tab === 'summary' ? 7 : 2), tab + ': мало кликабельных чисел: ' + ks.length);
    ks.forEach(k => check(D, html, k, tab)); total += ks.length;
  });
  assert.ok(total > 100, 'проверено чисел: ' + total);
});

t('Desk Monitor: группа таблицы позиций и фильтр страницы — панель по тем же позициям', () => {
  const sel = { books: ['RATES', 'FXB'], segments: [], entities: [], desks: [], portfolios: [], ccys: [] };
  const { $, D } = page('desk-monitor.html', split, { 'fidm.tab': 'positions', 'fidm.sel.v1': JSON.stringify(sel) });
  const html = $('#pos-tw').innerHTML, ks = keys(html);
  assert.ok(ks.length, 'итоги таблицы кликабельны');
  const books = new Set(D.positions.filter(p => Object.keys(D.BOOKS).includes(p.book)).map(p => p.book));
  ks.forEach(k => { const m = check(D, html, k, 'positions');
    m.rows.forEach(r => assert.ok(sel.books.some(b => b === r.book) || !books.size, 'строка вне фильтра: ' + r.book)); });
});

// ---------- Widget Board ----------
function board(mutate, widgets) {
  const st = { v: 1, active: 'b', boards: [{ id: 'b', name: 'R19', widgets: widgets.map((w, i) => Object.assign({ id: 'w' + i, w: 6, h: 2 }, w)) }] };
  const pg = page('widget-board.html', mutate, { 'dwb.boards.v1': JSON.stringify(st) });
  const grid = pg.$('#grid');
  return { D: pg.D, html: widgets.map((w, i) => grid.querySelector(`.wg[data-id="w${i}"]`).querySelector('.wg-b').innerHTML) };
}
const ALL = { book: 'all', segment: 'all', ccy: 'all' };
t('Widget Board: KPI-плитки и таблицы виджетов — панель сходится с числом', () => {
  const { D, html } = board(split, [
    { type: 'kpi', cfg: Object.assign({ metric: 'dv01' }, ALL) }, { type: 'kpi', cfg: Object.assign({ metric: 'pnl' }, ALL) },
    { type: 'kpi', cfg: Object.assign({ metric: 'mv' }, ALL) }, { type: 'kpi', cfg: Object.assign({ metric: 'ocpUSD' }, ALL) },
    { type: 'ocp', cfg: { book: 'all' } }, { type: 'watch', cfg: { items: ['g:Самолёт', 'p:ОФЗ 26247'] } }]);
  const all = html.join('\n'), ks = keys(all);
  assert.ok(ks.length >= 8, 'кликабельных чисел: ' + ks.length + ' ' + ks);
  ks.forEach(k => check(D, all, k, 'board'));
  assert.ok(D.drill.spec('wb:w1:kpi').rows.some(r => r.sub && r.sub.includes(WHY_ND)), 'PnL плитки — с причиной у позиции');
});

t('Widget Board: водопад PnL — «Без разложения» шагом, клик по шагу — позиции', () => {
  const { D } = board(split, [{ type: 'explain', cfg: ALL }]);
  const spec = D.drill.spec('wb:w0:explain:noDecomp');
  assert.ok(spec, 'шаг водопада кликабелен');
  const m = D.drill.model(spec);
  assert.strictEqual(m.total, 2.5e6 - 0.8e6); assert.strictEqual(m.rest, null);
});

// ---------- Scenario Lab ----------
t('Scenario Lab: итог сценария, компоненты, книги и PnL explain — панель сходится с числом', () => {
  const { $, D } = page('scenario-lab.html', split);
  const html = ['#hero', '#decomp', '#by-book', '#by-issuer', '#heat'].map(s => $(s).innerHTML).join('\n'), ks = keys(html);
  assert.ok(ks.length > 20, 'кликабельных чисел: ' + ks.length);
  ks.forEach(k => check(D, html, k, 'lab'));
  const ex = page('scenario-lab.html', split, { 'scenlab.mode': '"expl"' });
  const eh = ['#recon', '#hier-wrap'].map(s => ex.$(s).innerHTML).join('\n'), eks = keys(eh);
  assert.ok(eh.includes('Без разлож') && eh.includes('Невязка'), 'колонки разложения «Прочего»');
  assert.ok(eks.length > 20 && eks.some(k => k.endsWith(':noDecomp')), 'кликабельных чисел PnL explain: ' + eks.length);
  eks.forEach(k => check(ex.D, eh, k, 'lab explain'));
  const nd = ex.D.drill.model(ex.D.drill.spec(eks.find(k => k.startsWith('lab:hier:') && k.endsWith(':noDecomp'))));
  assert.ok(nd.rows.every(r => r.sub && r.sub.includes(WHY_ND)), 'причина у каждой позиции');
});

console.log(n + ' tests ok');
