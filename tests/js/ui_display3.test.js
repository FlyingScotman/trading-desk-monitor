// Отображение в мониторах (prototypes/concepts): положительные пояснения без круглых скобок (#189), деления осей и
// подписи единиц в валюте отчёта (#218), липкие подвалы таблиц (#210, #224), нейтральные контрагенты демо (#236).
//   node tests/js/ui_display3.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(repr, neg) {
  const store = new Map(repr ? [['concepts-repr', repr]] : []);
  if (neg) store.set('tdm.view.v1:ui:numfmt', JSON.stringify({ neg }));
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el(),
    createTreeWalker: () => ({ nextNode: () => null }) };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, NodeFilter: { SHOW_TEXT: 4 } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}

t('#218: деление оси K.tick — мелкие деления не превращаются в «0», крупные — без хвоста знаков', () => {
  const { K } = load();
  assert.strictEqual(K.tick(0, 1e6), '0');
  assert.strictEqual(K.tick(0.2e6, 1e6), '0.2');
  assert.strictEqual(K.tick(0.35e6, 1e6), '0.35');
  assert.strictEqual(K.tick(-0.452e6, 1e6), '(0.45)', 'отрицательное — в текущем формате');
  assert.strictEqual(K.tick(1.2e6, 1e6), '1.2');
  assert.strictEqual(K.tick(0.005e6, 1e6), '0.005', 'мелкие деления не совпадают (0.005 и 0.015)');
  assert.notStrictEqual(K.tick(0.005e6, 1e6), K.tick(0.015e6, 1e6));
  assert.strictEqual(K.tick(28.51e6, 1e6), '29');
  assert.strictEqual(K.tick(40000e3, 1e3), '40,000');
  assert.strictEqual(load('USD', 'minus').K.tick(-0.2e3, 1e3), '−0.2');
  // оси C06 и водопада PnL — через K.tick, а не f.mn(v, 0) / f.dv / f.k
  const c06 = P('c06-launchpad.html');
  assert.ok(!/axisLabel: \{ formatter: (v => f\.mn\(v, 0\)|v => f\.dv\(v\)|f\.k|fm) \}/.test(c06), 'C06: деления осей не округляются до целых');
  assert.ok(/formatter: v => K\.tick\(v, 1e6\), hideOverlap: true/.test(P('lib/kit.js')), 'водопад PnL');
});

t('#218: C08 — порог крупных выпусков в валюте отчёта, единицы у объёма и KPI; C11 — единица номинала в шапке', () => {
  const c08 = P('c08-calendar.html');
  assert.ok(!c08.includes("f.bn(C.bigRub, 0) + ' bn в обращении"), 'порог не округляется до «0 bn»');
  assert.ok(c08.includes("f.bn(C.bigRub, K.repr === 'RUB' ? 0 : 2)") && c08.includes("' bn ₽'"), 'порог: bn ₽ (символ валюты отчёта)');
  const { M } = load('USD'), C = M.marketCal;
  assert.ok(C.bigRub > 0 && C.bigRub < 1e9, 'в USD порог меньше 1 bn: ' + C.bigRub);
  assert.ok(c08.includes('<th class="n">Объём, bn ₽</th>') && !c08.includes('Объём, bn</th>'));
  assert.ok(c08.includes('font-size:11px">bn ₽</span>'), 'KPI рынка с валютой');
  const c11 = P('c11-history.html');
  assert.ok(/key: 'not0', label: 'Номинал было', num: true, unit: 'mm в вал\.', fmt: v => f\.mn\(v, 2\)/.test(c11), 'ячейка — число, единица — в шапке');
});

t('#218: C09 — под подписью оси X место в сетке', () => {
  const c09 = P('c09-market.html');
  assert.ok(c09.includes("grid: { left: 10, right: 20, top: 40, bottom: 30, containLabel: true }"));
  assert.ok(c09.includes("grid: { left: 8, right: 16, top: 36, bottom: 28, containLabel: true }"));
});

t('#189: положительные пояснения — без круглых скобок (разделитель или двоеточие)', () => {
  const c05 = P('c05-morning-brief.html');
  assert.ok(!c05.includes("' (' + f.bps(") && !c05.includes("' (' + f.mns(") && !c05.includes("' (' + f.pcts("));
  assert.ok(!P('c07-portfolio-map.html').includes("' (' + C.abs("));
  assert.ok(!P('c08-calendar.html').includes("(' + a.length + ')"));
  assert.ok(!P('c02-risk-ladder.html').includes("'показать всех (' + n + ')'"));
  const kit = P('lib/kit.js');
  assert.ok(!kit.includes("'все книги (' + booksIn().length + ')'") && !kit.includes("'свернуть прочие (' + t.dataset.fxmore + ')'"));
  assert.ok(!kit.includes("фильтры (' + nf + ')'") && !kit.includes('(в них ещё '));
  assert.ok(!c05.includes("'<span class=\"mut\">(' + c.short") && c05.includes("fxRatesLine().replace(/\\.$/, '') + '. Курсы: '"), 'C05: без скобок и без «б.п..»');
});

t('#210, #224: липкие «Итого» — матрица прокручивается сама, многострочный подвал липкий целиком', () => {
  const css = P('lib/kit.css');
  assert.ok(/\.tile > \.tb:has\(> \.k-rm\) \{ display: flex; flex-direction: column; \}/.test(css), 'только плитка C06');
  assert.ok(!/^:has\(/m.test(css), ':has без области не делает колонкой любой контейнер');
  // «Итого» матрицы — без полупрозрачного фона тепловой карты (сквозь липкую строку не видны строки)
  const kit = P('lib/kit.js');
  assert.ok(kit.includes("row('Итого', L, 0, null, null, true)") && kit.includes("mode === 'heat' ? (foot ? K.HEAT_PLAIN : K.heatRisk(v, allMax))"));
  // C06: ряды сетки — до отрисовки плиток; плитка «Концентрация» (fit) перерисовывается при изменении высоты
  const c06 = P('c06-launchpad.html');
  assert.ok(c06.indexOf("board.style.gridTemplateRows") < c06.indexOf("renderTile(t);\n      // плитка, содержимое"));
  assert.ok(/fit: true,/.test(c06) && /new ResizeObserver/.test(c06));
  assert.ok(/\.k-rm \{ flex: 0 1 auto; min-height: 0; \}/.test(css));
  assert.ok(/\.k-tbl tfoot:has\(> tr \+ tr\) \{ position: sticky; bottom: 0;/.test(css));
  assert.ok(/\.k-tbl tfoot:has\(> tr \+ tr\) td \{ position: static; bottom: auto; \}/.test(css));
});

t('#216: красный на фоне heatMono / heatRisk — контраст не ниже 3:1 в обеих темах', () => {
  const css = P('lib/kit.css'), { K } = load();
  const hex = s => [1, 3, 5].map(i => parseInt(s.slice(i, i + 2), 16));
  const lum = c => { const r = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r[0] + 0.7152 * r[1] + 0.0722 * r[2]; };
  const cr = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  const alpha = st => +st.match(/,([\d.]+)\)$/)[1];
  const amax = Math.max(alpha(K.heatRisk(-1, 1)), alpha(K.heatMono(-1, 1)), alpha(K.heatMono(-1, 1, '227,179,65')));
  assert.ok(amax <= 0.44 + 1e-9, 'фон не плотнее 0.44: ' + amax);
  const theme = sel => { const b = css.slice(css.indexOf(sel)); const v = k => b.match(new RegExp('--' + k + ': (#[0-9a-f]{6})'))[1]; return { neg: v('neg-heat'), bgs: [v('panel'), v('panel-2'), v('hover')] }; };
  [':root {', ':root[data-theme="light"] {'].forEach(sel => {
    const T = theme(sel);
    T.bgs.forEach(bg => [[76, 157, 255], [227, 179, 65], [227, 160, 60]].forEach(rgb => {
      const mix = hex(bg).map((v, i) => rgb[i] * amax + v * (1 - amax));
      const c = cr(hex(T.neg), mix);
      assert.ok(c >= 3, sel + ' ' + bg + ' ' + rgb + ': ' + c.toFixed(2));
    }));
  });
  assert.ok(/\.k-accn\.k-heatneg/.test(css) && css.includes('color: var(--neg-heat)'));  // класс от kit.js (#265)
});

t('#236: контрагенты демо-деривативов — нейтральные названия, без реальных организаций', () => {
  const { M } = load();
  const der = M.positions.filter(p => p.group === 'DER');
  assert.ok(der.length > 5);
  const real = /сбер|втб|газпромбанк|нкц|moex|мосбирж|альфа|райффайзен|открытие|россельхоз/i;
  der.forEach(p => assert.ok(!real.test(p.cpty || ''), p.name + ': ' + p.cpty));
  assert.ok(der.some(p => /^Контрагент [А-Я]$/.test(p.cpty)));
});

console.log(n + ' tests passed');
