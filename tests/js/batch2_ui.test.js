// Батч 2 мониторов (prototypes/concepts/, R37–R39, R43): валюта и эмитент — обычные столбцы таблиц позиций (сортировка,
// фильтр по значениям, группировка по желанию), отчёт «валюта × тип» — матрица (по вертикали валюта, по горизонтали
// тип, одна мера), Минфин — отдельный тип и в валютных бумагах (по эмитенту), ОВП в шапке — мини-таблица.
//   node tests/js/batch2_ui.test.js
// kit.js грузится в vm с заглушкой DOM (как ui1_batch.test.js): K.table пишет разметку в innerHTML, её и проверяем.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(data) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (data) { vm.runInContext(P('data.js'), ctx); data(ctx.window.DESK); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, I: ctx.window.INSTR };
}
const rowIds = html => [...html.matchAll(/<tr class="row[^"]*" data-id="([^"]+)"/g)].map(m => m[1]);
const mx = box => box.children[box.children.length - 2].innerHTML;  // таблица матрицы (el.append в заглушке копит детей)
const groupRows = html => [...html.matchAll(/<tr class="grp l(\d)"/g)].length;

// ---------- R37: валюта и эмитент — обычные столбцы ----------
t('R37: таблица позиций по умолчанию плоская, валюта и эмитент — столбцы с сортировкой и фильтром', () => {
  const { M, K } = load();
  const box = el();
  const api = K.table(box, { rows: M.positions, groupable: K.defaultGroups, columns: K.cols(['name', 'ccy', 'issuer', 'dv01']) });
  const html = () => box.children[1].innerHTML;
  assert.strictEqual(groupRows(html()), 0, 'без групп');
  assert.strictEqual(rowIds(html()).length, M.positions.length);
  for (const k of ['ccy', 'issuerName']) assert.ok(html().includes('data-flt="' + k + '"'), 'фильтр столбца ' + k);
  // сортировка по эмитенту — как по любому столбцу
  api.setSort([['issuerName', 1]]);
  const names = rowIds(html()).map(id => M.pos[id].issuerName);
  assert.deepStrictEqual(names, names.slice().sort((a, b) => a.localeCompare(b, 'ru')));
  // по валюте — в порядке валют деска, обратный тоже
  api.setSort([['ccy', -1]]);
  const cc = rowIds(html()).map(id => M.ccyOrder[M.pos[id].ccy]);
  assert.deepStrictEqual(cc, cc.slice().sort((a, b) => b - a));
  // фильтр по значениям: только USD и только Минфин
  const items = K.fltItems(M.positions, K.posCols.ccy);
  assert.strictEqual(items[0].v, 'RUB'); assert.strictEqual(items.reduce((s, x) => s + x.n, 0), M.positions.length);
  api.setFilter('ccy', ['USD']);
  assert.ok(rowIds(html()).every(id => M.pos[id].ccy === 'USD') && rowIds(html()).length > 3);
  api.setFilter('issuerName', ['Минфин']);
  const mf = rowIds(html());
  assert.ok(mf.length >= 3 && mf.every(id => M.pos[id].ccy === 'USD' && M.pos[id].issuer === 'MINFIN'), 'фильтры складываются');
  // список значений эмитента — под остальными фильтрами (только эмитенты USD)
  const iss = K.fltItems(K.applyFilters(M.positions, api.state.filters, k => K.posCols[k === 'issuerName' ? 'issuer' : k], 'issuerName'), K.posCols.issuer);
  assert.ok(iss.every(x => M.positions.some(p => p.ccy === 'USD' && p.issuerName === x.v)));
  api.setFilter('ccy', null); api.setFilter('issuerName', null);
  // группировка — по желанию
  api.setGrouped(true);
  assert.ok(groupRows(html()) > 5, 'валюта → эмитент');
});

t('R37: все таблицы позиций мониторов — с валютой и эмитентом, группы «валюта → эмитент» не по умолчанию', () => {
  for (const f of ['c01-cockpit.html', 'c06-launchpad.html', 'c07-portfolio-map.html', 'c09-market.html', 'c10-pnl-explain.html', 'c11-history.html', 'c12-terminal.html']) {
    const src = P('concepts/' + f);
    assert.ok(!/groupBy:\s*(K\.defaultGroups|\[K\.defaultGroups)/.test(src), f + ': группы по умолчанию');
    const lists = [...src.matchAll(/(K\.cols\(\[|Cols = \[\s*)'name'[^\n]*/g)].map(m => m[0]);
    assert.ok(lists.length || f === 'c11-history.html', f);
    lists.forEach(l => assert.ok(/'issuer'/.test(l) && (/'ccy'|ccyCol/.test(l)), f + ': ' + l.slice(0, 90)));
  }
  assert.ok(/K\.posCols\.ccy, K\.posCols\.issuer/.test(P('concepts/c11-history.html')));
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/grouping: 'none'/.test(c03), 'C03 по умолчанию без групп');
  const presets = [...c03.matchAll(/\['(\w+)', '[^']+', \[([^\]]+)\]\]/g)];
  assert.ok(presets.length >= 6);
  presets.forEach(m => assert.ok(/'ccy'/.test(m[2]) && /'issuer'/.test(m[2]), 'C03 набор ' + m[1]));
  assert.ok(c03.includes('K.applyFilters(') && c03.includes('K.fltIcon('), 'C03: фильтр по значениям столбца');
});

// ---------- R38: матрица «валюта × тип» ----------
t('R38: матрица — строки валюты, столбцы типы; ячейка, итог строки и итог столбца сходятся', () => {
  const { M, I } = load();
  M.toRepr = M.toRepr || {};
  const T = I.table(M.positions, { attribution: 'legs', units: 'repr' });
  assert.deepStrictEqual(T.kinds, T.kinds.slice().sort((a, b) => I.order[a] - I.order[b]));
  assert.ok(T.kinds.includes('MINFIN_FX') && T.kinds.includes('OFZ_PD') && T.kinds.includes('FX_FWD'));
  for (const mm of ['dv01', 'mv', 'notional', 'cs01', 'fx']) {
    for (const b of T.blocks) near(b.total.sum(mm), T.kinds.reduce((s, kd) => s + (T.cell[b.ccy + '|' + kd] ? T.cell[b.ccy + '|' + kd].sum(mm) : 0), 0), b.ccy + ' ' + mm);
    for (const c of T.columns) near(c.total.sum(mm), T.ccys.reduce((s, cc) => s + (T.cell[cc + '|' + c.kind] ? T.cell[cc + '|' + c.kind].sum(mm) : 0), 0), c.kind + ' ' + mm);
    near(T.total.sum(mm), T.columns.reduce((s, c) => s + c.total.sum(mm), 0), 'угол ' + mm);
  }
  // позиции: в своей валюте один раз; у столбца — без двойного счёта ног
  assert.strictEqual(T.columns.reduce((s, c) => s + c.total.ids.size, 0), M.positions.length);
  const ccs = T.columns.find(c => c.kind === 'CCS').total;
  assert.strictEqual(ccs.legs.size, 0, 'ноги CCS в рублях — те же позиции');
});

t('R38: на странице — мера одна, по горизонтали типы (группы сверху), итог строки и столбца; переключение меры', () => {
  const { M, K, I } = load();
  const box = el();
  I.state.measure = 'dv01'; I.state.units = 'repr';
  K.ccyKindTable(box);
  const html = mx(box);
  assert.ok(/<th rowspan="2"[^>]*>Валюта<\/th>/.test(html) && /<th rowspan="2" class="n"[^>]*>Итого<\/th>/.test(html));
  assert.ok(html.includes('>Минфин</th>') && html.includes('Минфин вал.'), 'группа «Минфин», тип «Минфин вал.»');
  const T = I.table(K.list(), I.state);
  assert.strictEqual((html.match(/<tbody>[\s\S]*<\/tbody>/)[0].match(/<tr /g) || []).length, T.ccys.length, 'строка на валюту');
  assert.ok(/Итого, все валюты/.test(html) && /разн\. вал\./.test(html), 'DV01 по всем валютам — с пометкой');
  // в валюте строки суммы по валютам нет; позиции — суммируются всегда
  I.state.units = 'own'; K.ccyKindTable(box);
  assert.ok(/не суммируется/.test(mx(box)));
  I.state.measure = 'n'; K.ccyKindTable(box);
  assert.ok(/Итого, все валюты/.test(mx(box)));
  I.state.measure = 'curve'; I.state.curve = 'USD SOFR'; K.ccyKindTable(box);
  assert.ok(/Итого, все валюты \(в USD\)/.test(mx(box)), 'DV01 одной кривой — в её валюте');
  assert.strictEqual(JSON.stringify(I.MEASURES.map(m => m.id)), JSON.stringify(['dv01', 'curve', 'cs01', 'basis', 'fx', 'mv', 'notional', 'n']));
  // меры совпадают с API (api/concepts.MATRIX_MEASURES)
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', 'api', 'concepts.py'), 'utf8');
  const pm = [...py.slice(py.indexOf('MATRIX_MEASURES:'), py.indexOf('"""The measures of the matrix')).matchAll(/\("(\w+)", "([^"]+)", "(\w+)"\)/g)].map(m => [m[1], m[3]]);
  const MAP = { dv01: 'dv01', dv01_curve: 'curve', cs01: 'cs01', ofz_basis: 'basis', fx_delta: 'fx', mv: 'mv', notional: 'notional', n_positions: 'n' };
  assert.strictEqual(JSON.stringify(pm.map(x => MAP[x[0]])), JSON.stringify(I.MEASURES.map(m => m.id)));
  // DV01 по всем валютам — с пометкой и там и там (API — всегда в рублях; на странице «repr» — только в валюте отчёта)
  pm.filter(([, a]) => a === 'labelled').forEach(([id]) => assert.strictEqual(I.MEASURE[MAP[id]].across, 'labelled', id));
});

// ---------- R39: Минфин — по эмитенту ----------
t('R39: Минфин — отдельный тип и в валютных бумагах, по эмитенту, не по списку ISIN', () => {
  const { M, I } = load(D => {
    const b = D.positions.find(p => p.instrType === 'BOND' && p.ccy === 'USD' && p.issuer !== 'Минфин РФ');
    const mk = (id, o) => D.positions.push(Object.assign({}, b, { id, isin: 'X' + id }, o));
    mk('EB1', { issuer: 'Минфин РФ', assetClass: 'sovereign_fx', segment: 'Корп' });            // класс хаба
    mk('EB2', { issuer: 'Минфин РФ', assetClass: 'Корпоративные', segment: 'Корп' });           // флаг эмитента
    D.ISSUERS['Республика Беларусь'] = { group: 'Республика Беларусь', sector: 'Суверен', gov: true };
    mk('FS1', { issuer: 'Республика Беларусь', assetClass: 'foreign_sovereign', segment: 'Корп' });  // не Минфин РФ
    mk('SX1', { issuer: 'Х', assetClass: 'sovereign_fx', segment: 'Корп', ccy: 'EUR' });
  });
  for (const id of ['EB1', 'EB2', 'SX1']) assert.strictEqual(I.kindOf(M.pos[id]), 'MINFIN_FX', id);
  assert.strictEqual(I.kindOf(M.pos.FS1), 'CORP_FIX');
  assert.ok(M.positions.filter(p => p.issuer === 'Минфин РФ' && p.instrType === 'BOND' && p.ccy === 'RUB').every(p => /^OFZ_/.test(I.kindOf(p))), 'в рублях — ОФЗ');
  assert.ok(M.positions.filter(p => p.issuer === 'Минфин РФ' && p.instrType === 'BOND' && p.ccy !== 'RUB').every(p => I.kindOf(p) === 'MINFIN_FX'));
  assert.ok(/^Минфин/.test(I.label.MINFIN_FX) && I.GROUP_LABEL.GOV === 'Минфин' && !I.KINDS.some(k => /уверен/.test(k[2])));
  // правило — одно с api: классы Минфина и «не Минфин» те же, что в publish/summary.py
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', 'publish', 'summary.py'), 'utf8');
  const real = P('concepts/lib/real.js');
  for (const [name, js] of [['MINFIN_CLASSES', 'MINFIN_CLASSES'], ['NOT_MINFIN_CLASSES', 'NOT_MINFIN']]) {
    const pv = [...py.match(new RegExp(name + ' = frozenset\\(\\{([^}]+)\\}\\)'))[1].matchAll(/"(\w+)"/g)].map(m => m[1]).sort();
    const jv = [...real.match(new RegExp(js + ' = \\{([^}]+)\\}'))[1].matchAll(/(\w+): 1/g)].map(m => m[1]).sort();
    assert.deepStrictEqual(jv, pv, name);
  }
  assert.ok(py.includes('MINFIN_INN = "7710168360"'));
});

t('R39: демо — замещающие и евробонды Минфина в USD, EUR, CNY — тип «Минфин вал.»', () => {
  const { M, I } = load();
  const fx = M.positions.filter(p => p.issuer === 'MINFIN' && p.ccy !== 'RUB');
  assert.deepStrictEqual([...new Set(fx.map(p => p.ccy))].sort(), ['CNY', 'EUR', 'USD']);
  assert.ok(fx.some(p => /^XS/.test(p.isin)), 'евробонд');
  assert.ok(fx.every(p => I.kindOf(p) === 'MINFIN_FX' && /Минфин/.test(p.kindLabel)));
});

// ---------- R43: ОВП в шапке ----------
t('R43: ОВП — мини-таблица: валюта на строку, по |эквиваленту|, мелкие — в «+N прочих»', () => {
  const { M, K, I } = load();
  const R = K.fxRows(M.positions);
  assert.deepStrictEqual(R.map(r => Math.abs(r.eq)), R.map(r => Math.abs(r.eq)).sort((a, b) => b - a));
  const html = K.fxMini(M.positions);
  assert.ok(/<table class="k-fxm">/.test(html) && /mm в вал\./.test(html) && /≈ mm ₽/.test(html), 'единицы — в заголовке');
  const minor = R.filter(r => Math.abs(r.v) < K.FX_MINOR);
  assert.ok(minor.length >= 2 && minor.some(r => r.c === 'GBP') && minor.some(r => r.c === 'AED'), 'демо: мелкие GBP и AED');
  assert.ok(html.includes('+' + minor.length + ' прочих'));
  assert.strictEqual((html.match(/<tr class="small">/g) || []).length, minor.length);
  const big = R.filter(r => Math.abs(r.v) >= K.FX_MINOR);
  assert.ok(big.length >= 3 && html.indexOf('>' + big[0].c + '<') < html.indexOf('>' + big[1].c + '<'));
  // один формат: знак, 1 знак после точки, разделитель тысяч
  const cny = R.find(r => r.c === 'CNY');
  assert.ok(html.includes(K.f.mns(cny.v, 1)) && /^[+−]?[\d,]+\.\d$/.test(K.f.mns(cny.v, 1)));
  // по ногам: вторая валюта кросса — своя строка (FX-дельта по ключам fx_delta)
  const usd = R.find(r => r.c === 'USD');
  near(usd.v, M.positions.reduce((s, p) => s + I.fxIn(p, 'USD'), 0), 'USD по ногам');
  assert.ok(/c06-launchpad\.html#fx/.test(P('concepts/lib/kit.js')) && /location\.hash === '#fx'/.test(P('concepts/c06-launchpad.html')), 'щелчок — ОВП в C06');
});

console.log(n + ' tests passed');
