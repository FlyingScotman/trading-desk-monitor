// Выброс в истории не задаёт шкалу (#170, prototypes/concepts/lib/kit.js: K.spikes): дата на порядки выше соседних —
// разрыв линии с пометкой и значением в подсказке; ступень (книга выросла), нули, обычный шум — не выбросы; неконечное
// значение — всегда выброс; даты с позициями без риска в сумме (сводка прогона riskBad) отмечены, но не вырезаны.
// Синтетика.
//   node tests/js/hist_spikes_170.test.js
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
function load() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x', reload() {} }, setTimeout,
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return ctx.window.KIT;
}
const K = load();
const dates = Array.from({ length: 40 }, (_, i) => '2026-03-' + String(i % 28 + 1).padStart(2, '0') + (i >= 28 ? 'x' + i : ''));
const fmt = v => String(Math.round(v / 1e3));
const base = dates.map((_, i) => -1e6 - 2e4 * Math.sin(i));  // RUONIA ~ −1 mm ₽/бп с шумом
const small = dates.map(() => 3e4);  // другая кривая, на порядки меньше

t('выброс одной кривой: вырезан из линии, остальное как было, дата и значение в пометке', () => {
  const r = base.slice(); r[12] = 1e14; r[13] = 2e14;  // две даты подряд
  const S = K.spikes([r, small], dates, ['RUONIA', 'USD'], fmt);
  assert.deepStrictEqual(Array.from(S.idx), [12, 13]);
  assert.strictEqual(S.data[0][12], null); assert.strictEqual(S.data[0][13], null);
  assert.strictEqual(S.data[0][11], base[11]); assert.deepStrictEqual(Array.from(S.data[1]), small, 'другие кривые на дате выброса не тронуты');
  assert.ok(Math.max(...S.data[0].filter(v => v != null).map(Math.abs)) < 2e6, 'шкала — по обычным датам');
  assert.ok(S.tip(12).includes('RUONIA ' + fmt(1e14)) && S.tip(11) === '');
  assert.strictEqual(S.markLine.data.length, 2); assert.strictEqual(S.markLine.data[0].xAxis, dates[12]);
  assert.ok(S.note.includes('выброс') && S.note.includes('RUONIA'));
  assert.strictEqual(r[12], 1e14, 'входной ряд не меняется');
});

t('ступень, рост позиции, нули и шум — не выбросы', () => {
  const step = dates.map((_, i) => i < 20 ? -1e5 : -3e6);  // книга выросла в 30 раз и осталась такой
  const ramp = dates.map((_, i) => -1e5 * (1 + i));  // плавный рост в 40 раз
  const zeros = dates.map((_, i) => i === 30 ? 5e5 : 0);  // кривая появилась на одну дату среди нулей
  for (const r of [step, ramp, base, zeros]) {
    const S = K.spikes([r], dates, ['X'], fmt);
    assert.deepStrictEqual(Array.from(S.idx), [], JSON.stringify(r.slice(0, 3)));
    assert.deepStrictEqual(Array.from(S.data[0]), r); assert.strictEqual(S.markLine, undefined); assert.strictEqual(S.note, '');
  }
});

t('неконечное значение — выброс даже без соседей; null — просто нет точки', () => {
  const r = [null, -1e6, Infinity, -1e6, null];
  const S = K.spikes([r], dates.slice(0, 5), ['RUONIA'], fmt);
  assert.deepStrictEqual(Array.from(S.idx), [2]); assert.strictEqual(S.data[0][2], null); assert.ok(S.tip(2).includes('Infinity'));
});

t('даты с позициями без риска в сумме (riskBad прогона) отмечены чертой и в подсказке, значения не вырезаны', () => {
  const S = K.spikes([base], dates, ['RUONIA'], fmt, { 5: 'без риска в сумме (невозможный риск): P7' });
  assert.deepStrictEqual(Array.from(S.idx), []); assert.deepStrictEqual(Array.from(S.data[0]), base);
  assert.strictEqual(S.markLine.data.length, 1); assert.strictEqual(S.markLine.data[0].xAxis, dates[5]);
  assert.ok(S.tip(5).includes('P7') && S.note.includes('1 дат'));
});

console.log(n + ' tests passed');
