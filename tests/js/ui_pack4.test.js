// Вёрстка мониторов (prototypes/concepts): узкий экран 390 px без прокрутки страницы вбок (#263), деления осей через
// K.tick и подписи водопада PnL (#261), красный тепловой карты — классом от kit.js и одним оттенком в колонке (#265).
//   node tests/js/ui_pack4.test.js
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
function load(repr) {
  const store = new Map(repr ? [['concepts-repr', repr]] : []);
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
const PAGES = fs.readdirSync(DIR).filter(f => /^c\d\d-.*\.html$/.test(f));

t('#263: smoke проверяет 390 px — scrollWidth равен innerWidth', () => {
  const s = P('tools/smoke.js');
  assert.ok(/setViewportSize\(\{ width: 390/.test(s));
  assert.ok(s.includes('document.documentElement.scrollWidth, window.innerWidth') && s.includes("errs.push('390px'"));
  assert.ok(s.includes("const TABS = '.k-tabs button, .presets [data-p]';"), 'обходит вкладки C01 и раскладки C06');
});

t('#263: узкий экран — шапка панели переносится, переключатель прокручивается внутри себя', () => {
  const css = P('lib/kit.css'), m = css.slice(css.indexOf('@media (max-width: 720px)'));
  const block = m.slice(0, m.indexOf('\n}\n'));
  assert.ok(/\.k-panel > \.hd \{ flex-wrap: wrap; \}/.test(block));
  assert.ok(/\.k-panel > \.hd h3 \{ white-space: normal; \}/.test(block), 'длинный заголовок панели переносится');
  assert.ok(/\.k-seg \{ max-width: 100%; overflow-x: auto; \}/.test(block));
});

t('#263: одноколоночная сетка — minmax(0, 1fr): колонка не шире окна из-за графика или таблицы', () => {
  PAGES.forEach(f => {
    const st = (P(f).match(/<style>[\s\S]*?<\/style>/) || [''])[0];
    assert.ok(!/grid-template-columns:\s*1fr\s*;/.test(st), f + ': grid-template-columns: 1fr');
  });
  assert.ok(/table\.mini \{ display: block; overflow-x: auto; \}/.test(P('c05-morning-brief.html')), 'C05: широкая таблица прокручивается сама');
});

t('#261: деления осей в mm — K.tick, без f.mn(v, 0)', () => {
  ['c01-cockpit.html', 'c02-risk-ladder.html', 'c10-pnl-explain.html', 'c11-history.html', 'c12-terminal.html', 'c13-issuers.html'].forEach(f => {
    const s = P(f);
    assert.ok(!/axisLabel: \{ formatter: v => f\.mn\(v, 0\)/.test(s), f + ': ось через f.mn(v, 0)');
  });
  assert.ok(/axisLabel: \{ formatter: x => K\.tick\(x, m\.sc\) \}/.test(P('c13-issuers.html')), 'C13: деления по масштабу метрики');
  const { K } = load('USD');
  // в USD деления 0.2 / 0.4 mm различимы (с f.mn(v, 0) было «0 0 0»)
  assert.deepStrictEqual([0, 2e5, 4e5, -2e5].map(v => K.tick(v, 1e6)), ['0', '0.2', '0.4', '(0.2)']);
});

t('#261: водопад PnL — в узком графике подписи категорий в два ряда', () => {
  const kit = P('lib/kit.js');
  assert.ok(kit.includes("formatter: (v, i) => stag && i % 2 ? '\\n' + v : v"));
  assert.ok(/K\.WF_AXIS = \d+; K\.WF_GAP = \d+;/.test(kit) && kit.includes('if (narrow() !== stag) c.redraw();'));
  // ширина подписи без canvas — оценка по числу знаков
  const { K } = load();
  assert.ok(K.labelWidth('Фондир.', 10) > 30 && K.labelWidth('Фондир.', 10) < 50);
});

t('#265: красный тепловой карты — класс k-heatneg от kit.js, без селектора по тексту style', () => {
  const css = P('lib/kit.css');
  assert.ok(!/style\*=/.test(css), 'селектор [style*=…] в kit.css');
  assert.ok(/\.k-accn\.k-heatneg, \.k-tbl td\.n\.k-accn\.k-heatneg \{ color: var\(--neg-heat\); \}/.test(css));
  const { K } = load();
  const mk = (text, style, parent, tag) => {
    const c = new Set();
    return { tagName: tag || 'TD', textContent: text, parentElement: parent || null, getAttribute: k => k === 'style' ? style || null : null,
      classList: { toggle: (k, on) => on ? c.add(k) : c.delete(k), contains: k => c.has(k) }, has: k => c.has(k), dataset: {} };
  };
  const heat = mk('(4)', K.heatRisk(-4, 10)), foot = mk('(4)', K.HEAT_PLAIN), plain = mk('(4)', null), sign = mk('(4)', K.heat(-4, 10));
  const inner = mk('(4)', null, mk('', K.heatMono(-4, 10)), 'SPAN'), spaced = mk('(4)', 'font-weight:600; --k-heat: plain');
  K.accMarkAll({ querySelectorAll: () => [heat, foot, plain, sign, inner, spaced] });
  [heat, foot, inner, spaced].forEach(e => assert.ok(e.has('k-accn') && e.has('k-heatneg'), 'тепловая карта и её «Итого» — k-heatneg'));
  assert.ok(plain.has('k-accn') && !plain.has('k-heatneg'), 'обычная ячейка — --neg');
  assert.ok(!sign.has('k-accn') && !sign.has('k-heatneg'), 'K.heat — смысловой цвет');
  const pos = mk('4', K.heatRisk(4, 10)); K.accMarkAll({ querySelectorAll: () => [pos] }); assert.ok(!pos.has('k-heatneg'), 'положительное — без класса');
});

t('#265: «Итого» тепловой карты помечено K.HEAT_PLAIN (без фона) — один оттенок красного в колонке', () => {
  assert.ok(P('lib/kit.js').includes("const tn = mode === 'heat' ? '<td class=\"n\" style=\"' + K.HEAT_PLAIN + '\">' : '<td class=\"n\">';"), 'K.bucketTable');
  assert.ok(P('lib/instr.js').includes("const heat = ms.what === 'n' ? '' : opt.total ? K.HEAT_PLAIN :"), 'instr.js: итоги');
  assert.ok(P('c04-pivot.html').includes("if (heat === 'tot') style = K.HEAT_PLAIN;"), 'C04: итоги');
  const c02 = P('c02-risk-ladder.html');
  assert.ok(c02.includes("const tn = '<td class=\"n\" style=\"' + K.HEAT_PLAIN + '\">'") && c02.includes("tot.map((v, k) => tn + K.d("), 'C02: подвал карты');
  const { K } = load();
  assert.ok(!/background/.test(K.HEAT_PLAIN), 'метка без фона: липкий подвал не просвечивает');
});

t('#265: --neg-heat на фоне «Итого» (--panel-2) — контраст не ниже 3:1 в обеих темах', () => {
  const css = P('lib/kit.css');
  const hex = s => [1, 3, 5].map(i => parseInt(s.slice(i, i + 2), 16));
  const lum = c => { const r = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * r[0] + 0.7152 * r[1] + 0.0722 * r[2]; };
  const cr = (a, b) => { const x = lum(hex(a)), y = lum(hex(b)); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
  [':root {', ':root[data-theme="light"] {'].forEach(sel => {
    const b = css.slice(css.indexOf(sel)); const v = k => b.match(new RegExp('--' + k + ': (#[0-9a-f]{6})'))[1];
    ['panel-2', 'panel', 'bg-2'].forEach(bg => { const c = cr(v('neg-heat'), v(bg)); assert.ok(c >= 3, sel + ' --' + bg + ': ' + c.toFixed(2)); });
  });
});

console.log(n + ' tests passed');
