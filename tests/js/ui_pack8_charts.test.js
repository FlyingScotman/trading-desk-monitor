// Мониторы (prototypes/concepts), пакет 8A: подпись столбца, которой нет места снаружи, — внутри столбца, контрастным
// цветом (K.barFit, K.inkOn, #352); подписи категорий горизонтального бара не шире K.BAR_CAT_MAX, деления оси значений не
// слипаются (hideOverlap, #351); C09 «базис ОФЗ − RUONIA» — подпись у конца столбца (K.barEnd), проверка ловит бары с
// position 'right' и 'top' и многострочные описания (#349); группа с итогом «—» — последняя при любом направлении
// сортировки (K.groupTotal, K.groupKeys, C03, #347). Геометрия в браузере — ui_pack8_charts.pw.test.js.
//   node tests/js/ui_pack8_charts.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { jsCode } = require('./fixtures/concepts_run');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function kit() {
  const store = new Map();
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el(),
    createTreeWalker: () => ({ nextNode: () => null }) };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, NodeFilter: { SHOW_TEXT: 4 },
    CSS: { escape: s => String(s).replace(/["\\]/g, '\\$&') } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  vm.runInContext(P('lib/instr.js'), ctx);
  vm.runInContext(P('lib/carry.js'), ctx);
  return ctx.window;
}
const W = kit(), K = W.KIT, M = W.MOCK, CARRY = W.CARRY;

t('#352: K.inkOn — белый или тёмный текст, у какого контраст со столбцом больше; не меньше 4.5', () => {
  ['#3fb67a', '#f0645a', '#4c9dff', '#e3b341', '#9b7bff', '#22c3d6', '#1a8a52', '#ffffff', '#808080', 'rgb(240, 100, 90)', 'rgba(76,157,255,.9)', '#abc'].forEach(c => {
    const ink = K.inkOn(c);
    assert.ok(K.INK.includes(ink), c + ' → ' + ink);
    assert.ok(K.contrast(ink, c) >= 4.5, c + ': контраст ' + K.contrast(ink, c).toFixed(2));
  });
  assert.strictEqual(K.inkOn('#ffffff'), K.INK[1]);
  assert.strictEqual(K.inkOn('#000000'), K.INK[0]);
  assert.strictEqual(K.inkOn({ type: 'linear' }), null, 'градиент — не разобран');
  assert.strictEqual(K.inkOn('red'), null);
  assert.ok(Math.abs(K.contrast('#000', '#fff') - 21) < 1e-9);
});

// горизонтальный (vert — вертикальный) бар в графике W×H: подписи формата fmt, цвет столбца по знаку
const fit = (W, H, vals, vert, fmt, cats, right) => {
  const cat = { type: 'category', data: cats || vals.map((_, i) => 'кат ' + i), axisLabel: { fontSize: 10 } }, val = { type: 'value' };
  const o = { grid: { left: 4, right: right == null ? 46 : right, top: vert ? 4 : 18, bottom: 4 }, xAxis: vert ? cat : val, yAxis: vert ? val : cat,
    series: [{ type: 'bar', data: vals.map(v => ({ value: v, itemStyle: { color: v < 0 ? '#f0645a' : '#3fb67a' }, label: K.barEnd(v, vert) })), label: { show: true, fontSize: 10, color: '#888', formatter: p => (fmt || String)(p.value) } }] };
  K.barFit({ clientWidth: W, clientHeight: H }, o);
  return o;
};
const labs = o => o.series[0].data.map(d => ({ v: d.value, show: d.label.show !== false, pos: d.label.position, color: d.label.color, border: d.label.textBorderColor }));

t('#352: K.barFit — подпись без места снаружи встаёт внутрь длинного столбца, короткий без места — без подписи', () => {
  // узкий график, подписи нужны с обеих сторон (поле grid справа узкое): под подписи — не больше K.BAR_FIT_MAX области,
  // снаружи не помещаются подписи длинных столбцов, но сами столбцы длиннее подписей
  const vals = [-17.7, -16.2, 18.9, 2, -0.5, -9.1, 15.2], fmt = v => '(' + Math.abs(v).toFixed(1) + ')';
  const L = labs(fit(190, 200, vals, false, fmt, null, 4));
  const by = v => L.find(x => x.v === v);
  assert.ok(L.some(x => x.pos === 'insideLeft') && L.some(x => x.pos === 'insideRight'), JSON.stringify(L));
  L.filter(x => /^inside/.test(x.pos)).forEach(x => {
    assert.ok(x.show && K.INK.includes(x.color), JSON.stringify(x));
    assert.ok(K.contrast(x.color, x.border) >= 4.5, 'контраст со столбцом ' + JSON.stringify(x));
    assert.strictEqual(x.pos, x.v < 0 ? 'insideLeft' : 'insideRight', 'у конца столбца');
  });
  // крупнейшие по модулю подписаны (снаружи или внутри); спрятаны только короткие
  [-17.7, -16.2, 18.9, 15.2].forEach(v => assert.ok(by(v).show, v + ': ' + JSON.stringify(by(v))));
  L.filter(x => !x.show).forEach(x => assert.ok(Math.abs(x.v) < 10, 'спрятан короткий ' + x.v));
  assert.ok(L.some(x => !x.show), 'короткому столбцу места нет ни снаружи, ни внутри');
  // место под подписи, ушедшие внутрь, не держим: ось уже, чем при «все снаружи» (K.BAR_FIT_MAX)
  const o = fit(190, 200, vals, false, fmt, null, 4), gap = (o.xAxis.boundaryGap || ['0%', '0%']).map(parseFloat), span = 18.9 + 17.7;
  assert.ok(span * (1 + (gap[0] + gap[1]) / 100) < span / (1 - K.BAR_FIT_MAX) - 1e-9, 'ось ' + gap);
  // вертикальный бар: внутрь — insideBottom у отрицательного
  const V = labs(fit(300, 100, [-40, -38, 3, 41], true, v => String(v), null, 4));
  assert.ok(V.some(x => /^inside/.test(x.pos || '')), JSON.stringify(V));
  V.filter(x => /^inside/.test(x.pos || '')).forEach(x => assert.strictEqual(x.pos, x.v < 0 ? 'insideBottom' : 'insideTop'));
  // столбец не уже подписи, цвет не разобран (градиент) — внутрь не ставим, прячем
  const g = { grid: { left: 4, right: 46, top: 4, bottom: 4 }, xAxis: { type: 'value' }, yAxis: { type: 'category', data: ['a', 'b'] },
    series: [{ type: 'bar', itemStyle: { color: { type: 'linear' } }, data: [{ value: -17.7, label: K.barEnd(-17.7) }, { value: 18.9, label: K.barEnd(18.9) }], label: { show: true, fontSize: 10, formatter: p => fmt(p.value) } }] };
  K.barFit({ clientWidth: 150, clientHeight: 200 }, g);
  g.series[0].data.forEach(d => assert.ok(d.label.show === false || !/^inside/.test(d.label.position), JSON.stringify(d.label)));
  // широкий график — всё снаружи, как было (#329)
  labs(fit(900, 300, vals, false, fmt)).forEach(x => assert.ok(x.show && !/^inside/.test(x.pos), JSON.stringify(x)));
});

t('#352: подписи категорий обрезаются, только если столбцам не хватает места; обрезанные — различимы', () => {
  const long = ['NDF USD/RUB 6M buy (рынок)', 'NDF USD/RUB 6M sell (рынок)', 'Газпром капитал ЗО27-1-Д', 'Газпром капитал ЗО28-2-Д', 'XCCY'];
  const vals = [-17.7, 18.9, 7, -3, 2], fmt = v => '(' + Math.abs(v).toFixed(1) + ')';
  // места хватает (C06 при 1280: плитка шире) — подписи целиком, формат категорий не трогаем
  const wide = fit(700, 200, vals, false, fmt, long);
  assert.strictEqual(wide.yAxis.axisLabel.formatter, undefined, 'широкий график — без обрезки');
  labs(wide).forEach(x => assert.ok(x.show && !/^inside/.test(x.pos), JSON.stringify(x)));
  // узкая плитка: без обрезки подписям столбцов места нет — обрезаются до K.BAR_CAT_MAX, «…» в середине, различимы
  const o = fit(235, 200, vals, false, fmt, long), cap = Math.max(48, Math.round((235 - 4 - 46) * K.BAR_CAT_MAX));
  const fm = o.yAxis.axisLabel.formatter;
  assert.strictEqual(typeof fm, 'function', 'узкий график — обрезка');
  const shown = long.map((t, i) => fm(t, i));
  shown.forEach((t, i) => assert.ok(K.labelWidth(t, 10) <= cap + 1e-9, t));
  assert.strictEqual(new Set(shown).size, shown.length, 'обрезанные подписи различимы: ' + shown.join(' | '));
  assert.strictEqual(shown[4], 'XCCY', 'короткая — как есть');
  assert.ok(shown[0].endsWith('buy (рынок)'.slice(-shown[0].split('…')[1].length)), 'хвост имени остаётся: ' + shown[0]);
  // K.fitLabels: общие начало и конец — больше хвоста, пока подписи не станут различимы
  const L = ['Минфин РФ ОФЗ 26238', 'Минфин РФ ОФЗ 26248', 'Минфин РФ ОФЗ 26240', 'Газпром капитал ЗО27-1-Д', 'Газпром капитал ЗО27-2-Д'];
  [40, 60, 80].forEach(w => {
    const F = K.fitLabels(L, w, 10);
    assert.strictEqual(new Set(F).size, F.length, w + ' px: ' + F.join(' | '));
    F.forEach(t => assert.ok(K.labelWidth(t, 10) <= w && t.includes('…'), t));
  });
  assert.deepStrictEqual([...K.fitLabels(['ОФЗ', 'Сбер'], 80, 10)], ['ОФЗ', 'Сбер']);
});

t('#351: деления горизонтальной оси — шаг закреплён, подписи через равный шаг, ноль подписан', () => {
  const fmt = v => '(' + Math.abs(v).toFixed(1) + ')', tick = v => (v < 0 ? '(' + Math.abs(v).toLocaleString('en-US') + ')' : v.toLocaleString('en-US'));
  const cases = [[235, [-17.7, 18.9, 7, -3, 2]], [180, [-4000, 3000, 59, -200]], [390, [45317, 6477, 1735]], [220, [-0.22, 0.23, 0.14]], [600, [-2645, -147]]];
  cases.forEach(([Wd, vals]) => {
    const o = { grid: { left: 4, right: 46, top: 18, bottom: 4 }, xAxis: { type: 'value', axisLabel: { formatter: tick } }, yAxis: { type: 'category', data: vals.map((_, i) => 'к' + i) },
      series: [{ type: 'bar', data: vals.map(v => ({ value: v, label: K.barEnd(v) })), label: { show: Wd !== 180, fontSize: 10, formatter: p => fmt(p.value) } }] };
    K.barFit({ clientWidth: Wd, clientHeight: 200 }, o);
    const x = o.xAxis, s = Wd + ' ' + JSON.stringify(vals);
    assert.ok(x.interval > 0 && x.axisLabel.hideOverlap === true, s);
    // концы оси — явно, кратные шагу делений (как округлил бы ECharts), данные внутри
    const lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), ne = [x.min, x.max];
    assert.ok(x.min <= lo && x.max >= hi && x.boundaryGap === undefined, s + ': концы ' + ne);
    [x.min, x.max].forEach(v => assert.ok(Math.abs(v / x.interval - Math.round(v / x.interval)) < 1e-9, s + ': конец ' + v + ' кратен шагу ' + x.interval));
    assert.ok((x.max - x.min) / x.interval <= 10, s + ': делений не больше 10');
    const ticks = []; for (let j = Math.round(ne[0] / x.interval); j <= Math.round(ne[1] / x.interval); j++) ticks.push(Number((j * x.interval).toPrecision(12)));
    const lab = ticks.filter(v => x.axisLabel.formatter(v) !== '');
    assert.ok(lab.includes(0) && x.axisLabel.formatter(0) === '0', s + ': ноль подписан');
    assert.ok(lab.length >= 2, s + ': подписей ' + lab.join(','));
    const d = lab.slice(1).map((v, i) => v - lab[i]);
    assert.ok(d.every(v => Math.abs(v - d[0]) < 1e-9 * Math.abs(d[0])), s + ': подписи через равный шаг ' + lab.join(','));
    // соседние подписи не ближе K.TICK_GAP px (ширина области — без подписей категорий)
    const room = Wd - 50 - Math.max(...o.yAxis.data.map(t => K.labelWidth(t, 12))) - 8, ppu = room / (ne[1] - ne[0]);
    const w = Math.max(...lab.map(v => K.labelWidth(tick(v), 12)));
    assert.ok(d[0] * ppu >= w + K.TICK_GAP - 1e-9, s + ': зазор ' + (d[0] * ppu - w).toFixed(1));
  });
  // шаг оси задан страницей — не трогаем
  const f = { grid: {}, xAxis: { type: 'value', interval: 5 }, yAxis: { type: 'category', data: ['a'] }, series: [{ type: 'bar', data: [3] }] };
  K.barFit({ clientWidth: 300, clientHeight: 200 }, f);
  assert.strictEqual(f.xAxis.interval, 5);
  assert.strictEqual(f.xAxis.axisLabel.formatter, undefined);
  // вертикальный бар — деления ECharts, только hideOverlap
  const v = fit(300, 200, [-4, 5], true);
  assert.ok(v.yAxis.axisLabel.hideOverlap === true && v.yAxis.interval === undefined);
});

// описания баров (объект ряда с type: 'bar', и в несколько строк) в коде страниц и kit.js без комментариев
const barSeries = s => {
  const out = [];
  for (const m of s.matchAll(/type: *'bar'/g)) {
    let i = m.index, d = 0;
    for (; i >= 0; i--) { const c = s[i]; if (c === '}') d++; else if (c === '{') { if (!d) break; d--; } }
    let j = i; d = 0;
    for (; j < s.length; j++) { const c = s[j]; if (c === '{') d++; else if (c === '}') { d--; if (!d) break; } }
    out.push(s.slice(i, j + 1));
  }
  return out;
};
// подпись ряда (объект label: {…} целиком, по скобкам): показана (show не false) и стоит справа или сверху
const labelAt = b => {
  const m = /label: *\{/.exec(b);
  if (!m) return null;
  let i = m.index + m[0].length - 1, d = 0, j = i;
  for (; j < b.length; j++) { if (b[j] === '{') d++; else if (b[j] === '}') { d--; if (!d) break; } }
  const L = b.slice(i, j + 1);
  return !/show: *false\b/.test(L) && /show:/.test(L) ? /position: *['"](right|top)['"]/.exec(L) : null;
};

t('#349: C09 «базис ОФЗ − RUONIA» — подпись у конца столбца (K.barEnd, вертикальный) и место под неё (barFit)', () => {
  const c09 = jsCode(P('c09-market.html'));
  const b = barSeries(c09).find(x => x.includes("'базис ОФЗ − RUONIA'"));
  assert.ok(b && b.includes('label: K.barEnd(v, true)'), b);
  assert.ok(/barFit: true, tooltip: \{ trigger: 'axis', valueFormatter: v => f\.bp\(v\)/.test(c09), 'barFit у графика базиса');
});

t('#349: бары с подписью справа или сверху — только через K.barEnd; проверка видит \'top\' и многострочные описания', () => {
  // сама проверка: ловит 'top', 'right', порядок show/position и описание в несколько строк; не ловит show: false
  const bad = ["{ type: 'bar', data: d, label: { show: true, position: 'top' } }",
    "{ name: 'x',\n  type: 'bar',\n  data: d,\n  label: {\n    show: true,\n    position: 'right',\n  },\n}",
    "{ type: 'bar', label: { position: 'top', show: R.length <= 8 }, data: d }",
    "{ type: 'bar', data: d, label: { show: true, formatter: p => { return f(p.value); }, position: \"right\" } }"];
  bad.forEach(s => { const b = barSeries(s); assert.strictEqual(b.length, 1, s); assert.ok(labelAt(b[0]) && !b[0].includes('K.barEnd('), s); });
  assert.ok(!labelAt(barSeries("{ type: 'bar', label: { show: false, position: 'top' } }")[0]));
  assert.ok(!labelAt(barSeries("{ type: 'bar', stack: 's', data: d }")[0]));
  // все страницы и kit.js
  const found = [];
  fs.readdirSync(DIR).filter(f => f.endsWith('.html')).map(f => [f, P(f)]).concat([['lib/kit.js', P('lib/kit.js')]]).forEach(([f, src]) => {
    barSeries(jsCode(src)).forEach(b => {
      if (!labelAt(b)) return;
      found.push(f);
      assert.ok(b.includes('K.barEnd('), f + ': ' + b.replace(/\s+/g, ' ').slice(0, 160));
    });
  });
  // C06 (3), C09 (1), C13 (2), разбор K.drill (1)
  assert.deepStrictEqual(found.sort(), ['c06-launchpad.html', 'c06-launchpad.html', 'c06-launchpad.html', 'c09-market.html', 'c13-issuers.html', 'c13-issuers.html', 'lib/kit.js']);
});

t('#347: K.groupTotal — «—» (null), если ни одно слагаемое не посчитано, есть NaN или nullable-столбец пуст', () => {
  const v = r => r.v;
  assert.strictEqual(K.groupTotal([{ v: null }, { v: null }], v), null);
  assert.strictEqual(K.groupTotal([{ v: null }, { v: 3 }, { v: -1 }], v), 2);
  assert.strictEqual(K.groupTotal([], v), 0);
  assert.strictEqual(K.groupTotal([{ v: NaN }], v), null);
  // NaN — «итог не посчитан» (C17, CARRY.colSum), как и раньше в K.table: при любых остальных слагаемых
  assert.strictEqual(K.groupTotal([{ v: 5 }, { v: NaN }, { v: 1 }], v), null);
  assert.strictEqual(K.groupTotal([{ v: 5, x: null }], v, r => r.x), null, 'nullable: все пусты');
  assert.strictEqual(K.groupTotal([{ v: 5, x: 1 }], v, r => r.x), 5);
});

t('#347: C17 в демо — итог «Нетто» «—» (позиции без фондирования), «Фондирование» — число', () => {
  const d = M.forwardCarryDemo({});
  const rows = CARRY.rows(d, { pos: M.pos, books: M.book }).concat(CARRY.cashRows(d, { books: M.book }));
  assert.ok(rows.length > 10);
  assert.strictEqual(K.groupTotal(rows, CARRY.colSum.net, r => r.net), null, 'итог «Нетто» — «—»');
  assert.ok(Number.isFinite(K.groupTotal(rows, CARRY.colSum.funding, r => r.funding)), 'итог «Фондирование» — число');
  // группа «—» — последней при сортировке групп по нетто (книги)
  const by = new Map(); rows.forEach(r => { if (!by.has(r.book)) by.set(r.book, []); by.get(r.book).push(r); });
  const col = k => (k === 'net' ? { val: r => r.net, sum: CARRY.colSum.net, nullable: true } : null);
  [1, -1].forEach(dir => {
    const keys = K.groupKeys([...by.keys()], k => by.get(k), [['net', dir]], col), tot = keys.map(k => K.groupTotal(by.get(k), CARRY.colSum.net, r => r.net));
    const first = tot.findIndex(x => x == null);
    assert.ok(first >= 0 && tot.slice(first).every(x => x == null), dir + ': ' + JSON.stringify(tot));
  });
});

t('#347: K.groupKeys — группа с итогом «—» последняя при любом направлении сортировки', () => {
  const G = { pos: [{ v: 5 }], neg: [{ v: -7 }], none: [{ v: null }, { v: null }], zero: [{ v: 0 }], part: [{ v: null }, { v: 1 }] };
  const col = k => (k === 'v' ? { val: r => r.v, sum: r => r.v } : null);
  const keys = Object.keys(G);
  assert.deepStrictEqual(K.groupKeys(keys, k => G[k], [['v', 1]], col), ['neg', 'zero', 'part', 'pos', 'none']);
  assert.deepStrictEqual(K.groupKeys(keys, k => G[k], [['v', -1]], col), ['pos', 'part', 'zero', 'neg', 'none']);
  // nullable-столбец: все значения пусты — «—», хотя слагаемое итога (sum) посчитано
  const H = { a: [{ v: 1, f: null }], b: [{ v: 2, f: 3 }], c: [{ v: -2, f: -3 }] };
  const coln = k => (k === 'f' ? { val: r => r.f, sum: r => r.v, nullable: true } : null);
  assert.deepStrictEqual(K.groupKeys(Object.keys(H), k => H[k], [['f', 1]], coln), ['c', 'b', 'a']);
  assert.deepStrictEqual(K.groupKeys(Object.keys(H), k => H[k], [['f', -1]], coln), ['b', 'c', 'a']);
  // строки с null — тоже в конце (то же правило)
  const rows = [{ v: null }, { v: 2 }, { v: -3 }];
  [1, -1].forEach(d => assert.strictEqual(rows.slice().sort(K.rowCmp([['v', d]], col)).pop().v, null));
});

t('#347: K.table и C03 — один расчёт итога (K.groupTotal) для ячейки и сортировки групп, без «|| 0»', () => {
  const kit = P('lib/kit.js'), c03 = P('c03-blotter.html');
  assert.ok(kit.includes("sum: c.sum ? (typeof c.sum === 'function' ? c.sum : r => val(c, r)) : null, nullable: !!c.nullable"), 'colOf K.table');
  assert.ok(kit.includes('const tot = K.groupTotal(list, fn, c.nullable && (r => val(c, r)));'), 'sumCell K.table');
  assert.ok(kit.includes('c.sum ? K.groupTotal(L, c.sum, c.nullable && c.val)'), 'K.groupKeys');
  assert.ok(!/L\.reduce\(\(t, r\) => t \+ \(c\.sum\(r\) \|\| 0\)/.test(kit), 'старый расчёт групп убран');
  assert.ok(c03.includes('nullable: !!C[k].nullable') && c03.includes('const tot = K.groupTotal(list, c.sum, c.nullable && c.val);'), 'C03');
  assert.ok(!c03.includes('(c.sum(r) || 0)'), 'C03: копии расчёта нет');
});

t('#347 (дополнение): C10 — одиночная точка «накопл.» крупнее и с обводкой', () => {
  assert.ok(P('c10-pnl-explain.html').includes("symbol: 'circle', symbolSize: 8, itemStyle: { borderColor: K.css('--fg'), borderWidth: 1.5 }"));
});

console.log(n + ' tests ok');
