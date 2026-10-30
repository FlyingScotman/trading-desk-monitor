// Формат отрицательных чисел в мониторах (#153, prototypes/concepts/lib/kit.js): настройка K.negFmt —
// 'acc' (бухгалтерский, (1,234.5) красным; по умолчанию) или 'minus' (−1,234.5); хранится в K.viewStore,
// меняется без перезагрузки; нули, null и NaN — как раньше; CSV — всегда с обычным минусом.
//   node tests/js/neg_format.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(store) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) };
  let reloads = 0;
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x', reload: () => reloads++ }, setTimeout,
    localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, reloads: () => reloads };
}
const KEY = 'tdm.view.v1:ui:numfmt';

t('по умолчанию — бухгалтерский формат: отрицательные в скобках, единица внутри скобок', () => {
  const { K } = load(new Map()), f = K.f;
  assert.strictEqual(K.negFmt, 'acc');
  assert.strictEqual(f.n(-1234.5, 1), '(1,234.5)');
  assert.strictEqual(f.n(1234.5, 1), '1,234.5');
  assert.strictEqual(f.n(-1234567), '(1,234,567)');
  assert.strictEqual(f.s(-12), '(12)', 'знаковый формат: минус — скобками, без «+»');
  assert.strictEqual(f.s(12), '+12');
  assert.strictEqual(f.k(-12345), '(12)');
  assert.strictEqual(f.dv(-12345), '(12)');
  assert.strictEqual(f.dvs(-12345), '(12)');
  assert.strictEqual(f.mn(-2.5e6, 1), '(2.5)');
  assert.strictEqual(f.mns(-2.5e6, 1), '(2.5)');
  assert.strictEqual(f.bn(-1.5e9), '(1.50)');
  assert.strictEqual(f.bp(-7), '(7)');
  assert.strictEqual(f.bps(-7), '(7)');
  assert.strictEqual(f.pct(-4.444), '(4.44%)');
  assert.strictEqual(f.pct(4.444), '4.44%');
  assert.strictEqual(f.pcts(-0.5), '(0.50%)');
  assert.strictEqual(f.pcts(0.5), '+0.50%');
  assert.strictEqual(f.money(-1.5e9), '(1.50 bn)');
  assert.strictEqual(f.money(-2.5e6), '(2.5 mm)');
  assert.strictEqual(f.money(-2500), '(3k)');
  assert.strictEqual(f.moneys(2500), '+3k');
});

t('нули, округлённые до нуля, null и NaN — без изменений', () => {
  const { K } = load(new Map()), f = K.f;
  assert.strictEqual(f.n(0), '0');
  assert.strictEqual(f.n(-0.04, 1), '0.0', 'отрицательное, округлённое до нуля, — без скобок');
  assert.strictEqual(f.s(-0.04, 1), '0.0');
  assert.strictEqual(f.n(null), '—');
  assert.strictEqual(f.n(NaN), '—');
  assert.strictEqual(f.n(-Infinity), '—');
  assert.strictEqual(f.pct(null), '—');
  assert.strictEqual(f.pcts(null), '—');
  assert.strictEqual(f.bp(null), '—');
});

t('настройка «минус»: как до #153; хранится в K.viewStore и применяется без перезагрузки', () => {
  const store = new Map(), L = load(store), K = L.K, f = K.f;
  let redraws = 0, painted = 0;
  K.onFilter(() => redraws++); K.on('numfmt', () => painted++);
  K.setNegFmt('minus');
  assert.strictEqual(K.negFmt, 'minus');
  assert.strictEqual(f.n(-1234.5, 1), '−1,234.5');
  assert.strictEqual(f.s(-12), '−12');
  assert.strictEqual(f.pct(-4.444), '−4.44%');
  assert.strictEqual(f.money(-1.5e9), '−1.50 bn');
  assert.strictEqual(f.n(0), '0');
  assert.deepStrictEqual(JSON.parse(store.get(KEY)), { neg: 'minus' });
  assert.strictEqual(redraws, 1, 'страница перерисована по событию фильтра');
  assert.strictEqual(painted, 1);
  assert.strictEqual(L.reloads(), 0, 'без перезагрузки');
  K.setNegFmt('minus'); K.setNegFmt('nonsense');
  assert.strictEqual(redraws, 1, 'тот же или неизвестный формат — ничего не делает');
  // следующая страница читает настройку
  assert.strictEqual(load(store).K.negFmt, 'minus');
  assert.strictEqual(load(store).K.f.n(-1), '−1');
  K.setNegFmt('acc');
  assert.strictEqual(load(store).K.negFmt, 'acc');
  // мусор в хранилище — формат по умолчанию
  for (const bad of ['"x"', '{"neg":"red"}', 'not json', '[1]'])
    assert.strictEqual(load(new Map([[KEY, bad]])).K.negFmt, 'acc', bad);
});

t('K.withNegFmt: формат на время вызова, настройка после — прежняя (и при исключении)', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.withNegFmt('minus', () => K.f.n(-5)), '−5');
  assert.strictEqual(K.f.n(-5), '(5)');
  assert.throws(() => K.withNegFmt('minus', () => { throw new Error('x'); }));
  assert.strictEqual(K.negFmt, 'acc');
});

t('красный и выравнивание — по тексту цифры (K.accClass) в числовых ячейках, плитках, полях карточки', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.accClass('(1,234.5)'), 'k-accn');
  assert.strictEqual(K.accClass(' (2.5) mm ₽ '), 'k-accn', 'число в скобках с единицей после');
  assert.strictEqual(K.accClass('(4.44%)'), 'k-accn');
  assert.strictEqual(K.accClass('(1.50 bn)'), 'k-accn');
  assert.strictEqual(K.accClass('1,234.5'), 'k-accp');
  assert.strictEqual(K.accClass('+12'), 'k-accp');
  assert.strictEqual(K.accClass('4.40%'), 'k-accp');
  assert.strictEqual(K.accClass('0'), 'k-accp', 'ноль — не красный');
  assert.strictEqual(K.accClass('1.00mm RUB'), '', 'число с единицей — без выравнивания');
  assert.strictEqual(K.accClass('98.399 (0.28)'), '', 'скобки не в начале — не отрицательное число');
  assert.strictEqual(K.accClass('(сд)'), '');
  assert.strictEqual(K.accClass('—'), '');
  assert.strictEqual(K.accClass(null), '');
  assert.strictEqual(K.ACC_SEL, 'td.n, .dr, .k-kpi .v, .k-fields .v, .k-num');
  K.setNegFmt('minus');
  assert.strictEqual(K.accClass('(1,234.5)'), '', 'формат «минус» — классы снимаются');
  assert.strictEqual(K.accClass('1'), '');
  const css = P('concepts/lib/kit.css');
  assert.ok(/\.k-accn[^{]*\{\s*color:\s*var\(--neg\);/.test(css), 'kit.css: красный');
  assert.ok(/td\.n\.k-accp::after\s*\{\s*content:\s*'\)';\s*visibility:\s*hidden;/.test(css), 'kit.css: невидимая «)»');
});

t('разметка DOM: K.accMarkAll ставит и снимает классы', () => {
  const { K } = load(new Map());
  const mk = (tag, text, cls, style, parent) => {
    const c = new Set(cls || []);
    return { tagName: tag, textContent: text, parentElement: parent || null, getAttribute: k => k === 'style' ? style || null : null,
      classList: { toggle: (k, on) => on ? c.add(k) : c.delete(k), contains: k => c.has(k) }, has: k => c.has(k) };
  };
  const a = mk('TD', '(1,234.5)'), b = mk('TD', '1,234.5'), d = mk('SPAN', '7');
  const root = { querySelectorAll: sel => { assert.strictEqual(sel, K.ACC_SEL); return [a, b, d]; } };
  K.accMarkAll(root);
  assert.ok(a.has('k-accn') && !a.has('k-accp'));
  assert.ok(b.has('k-accp') && !b.has('k-accn'));
  assert.ok(!d.has('k-accp'), 'невидимая «)» — только в ячейке таблицы');
  K.withNegFmt('minus', () => K.accMarkAll(root));
  assert.ok(!a.has('k-accn') && !b.has('k-accp'), '«минус» — классы сняты');
});

t('смысловой цвет страницы важнее бухгалтерского красного (#103): chg-up/chg-dn, pos/neg, фон heat', () => {
  const { K } = load(new Map());
  const mk = (tag, text, cls, style, parent) => {
    const c = new Set(cls || []);
    return { tagName: tag, textContent: text, parentElement: parent || null, getAttribute: k => k === 'style' ? style || null : null,
      classList: { toggle: (k, on) => on ? c.add(k) : c.delete(k), contains: k => c.has(k) }, has: k => c.has(k), dataset: {} };
  };
  const dn = mk('TD', '(20)', ['n', 'chg-dn']), up = mk('TD', '(5)', ['n', 'chg-up']);
  const heat = mk('TD', '(1)', ['n'], 'background:rgba(63,182,122,.3)');
  const tdChg = mk('TD', '(3)', ['n', 'chg-dn']), drIn = mk('SPAN', '(3)', ['dr'], null, tdChg);
  const tr = mk('TR', ''), plainTd = mk('TD', '(7)', ['n'], null, tr), drPlain = mk('SPAN', '(7)', ['dr'], null, plainTd);
  const all = [dn, up, heat, tdChg, drIn, plainTd, drPlain];
  K.accMarkAll({ querySelectorAll: () => all });
  [dn, up, heat, tdChg, drIn].forEach(e => assert.ok(!e.has('k-accn'), e.textContent + ' — свой цвет страницы'));
  assert.ok(plainTd.has('k-accn') && drPlain.has('k-accn'), 'без смыслового цвета — красным');
  // метка «отформатировано как отрицательное» для CSV (#222) — независимо от цвета
  all.forEach(e => assert.ok('kneg' in e.dataset, e.textContent + ' — data-kneg'));
  const pos = mk('TD', '1,234.5', ['n']); K.accMarkAll({ querySelectorAll: () => [pos] }); assert.ok(!('kneg' in pos.dataset));
  K.withNegFmt('minus', () => K.accMarkAll({ querySelectorAll: () => all }));
  all.forEach(e => assert.ok(!('kneg' in e.dataset), e.textContent + ' — «минус»: метки нет'));
  assert.ok(/(^|,\s*)\.k-accn\s*[,{]/m.test(P('concepts/lib/kit.css')));
});

t('фон по величине (K.heatMono) и фон риска (K.heatRisk) — не смысловой цвет: красный минус остаётся (#216)', () => {
  const { K } = load(new Map());
  const mk = (text, style, cls) => {
    const c = new Set(['n'].concat(cls || []));
    return { tagName: 'TD', textContent: text, parentElement: null, getAttribute: k => k === 'style' ? style || null : null,
      classList: { toggle: (k, on) => on ? c.add(k) : c.delete(k), contains: k => c.has(k) }, has: k => c.has(k) };
  };
  const mono = mk('(4)', K.heatMono(-4, 10)), monoRgb = mk('(4)', K.heatMono(-4, 10, '227,179,65')), risk = mk('(4)', K.heatRisk(-4, 10));
  const sign = mk('(4)', K.heat(-4, 10)), colored = mk('(4)', K.heatMono(-4, 10) + ';color:var(--warn)'), monoChg = mk('(4)', K.heatMono(-4, 10), ['chg-up']);
  K.accMarkAll({ querySelectorAll: () => [mono, monoRgb, risk, sign, colored, monoChg] });
  [mono, monoRgb, risk].forEach(e => assert.ok(e.has('k-accn'), 'фон по величине/риска — минус красным'));
  [sign, colored, monoChg].forEach(e => assert.ok(!e.has('k-accn'), 'K.heat, цвет текста и класс изменения — смысловые'));
  assert.ok(/^background:/.test(K.heat(-4, 10)), 'K.heat без метки');
  assert.strictEqual(K.heatMono(0, 10), '', 'ноль — без фона');
});

t('CSV: всегда обычный минус, без скобок, при любой настройке', () => {
  const { K } = load(new Map());
  assert.strictEqual(K.csvNum('(1,234.5)'), '-1234.5');
  assert.strictEqual(K.csvNum('(4.44%)'), '-4.44%');
  assert.strictEqual(K.csvNum('(12)'), '-12');
  assert.strictEqual(K.csvNum('(−12)'), null, 'знак внутри скобок — не число');
  assert.strictEqual(K.csvNum('(abc)'), null);
  assert.strictEqual(K.csvNum('−1,234.5'), '-1234.5');
  assert.strictEqual(K.csvCell('(1,234.5)', true), '-1234.5');
  const rows = [{ id: 'a', x: 1, p: 4.444 }, { id: 'b', x: -1234.5, p: -4.444 }];
  const columns = [{ key: 'x', label: 'Икс', num: true, sum: true, fmt: v => K.f.n(v, 1) }, { key: 'p', label: 'Пэ', num: true, fmt: v => K.f.pct(v) }];
  for (const mode of ['acc', 'minus']) {
    K.setNegFmt(mode);
    const L = K.table(el(), { rows, search: false, viewKey: 'neg-' + mode, sort: [['x', 1]], columns }).csv().split('\r\n');
    assert.strictEqual(L[3], '-1234.5,-4.44%', mode);
    assert.strictEqual(L[4], '1.0,4.44%', mode);
    assert.strictEqual(K.negFmt, mode, 'выгрузка не меняет настройку');
  }
});

t('шапка: кнопка формата отрицательных чисел рядом с темой', () => {
  const src = P('concepts/lib/kit.js');
  assert.ok(src.includes('data-a="numfmt"') && /K\.setNegFmt\(K\.negFmt === 'acc' \? 'minus' : 'acc'\)/.test(src));
});

t('#262: C16 перерисовывается при смене формата отрицательных — из последнего ответа, без нового запроса', () => {
  const src = P('concepts/c16-ocp.html');
  assert.ok(/function render\(\) \{\n\s+if \(!H\) return;/.test(src) && src.includes('.then(x => { H = x; render(); })'), 'render — из запомненного ответа');
  assert.ok(src.includes("K.on('numfmt', render);"), 'подписка на numfmt');
  // подписчики 'numfmt' вызываются K.setNegFmt
  const { K } = load(new Map());
  let calls = 0; K.on('numfmt', () => calls++);
  K.setNegFmt(K.negFmt === 'acc' ? 'minus' : 'acc');
  assert.strictEqual(calls, 1);
});

console.log(n + ' tests passed');
