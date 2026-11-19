// TDM#775 без браузера: цвет столбца «Сделки за день» (куплено — зелёным, продано — красным) и новый столбец «Изм. к вчера»
// (K.prevDelta, K.posCols.posDelta, K.prevClosedLine; M.prevRun и поля позиции prevNotional / newToPrev / prevNoFact из
// lib/real.js), его место в наборах C03 и в таблице C01. Данные вымышленные.
//   node tests/js/prev_delta_775.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..');
const P = f => fs.readFileSync(path.join(ROOT, 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  const q = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true, parentNode: {}, q,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(sel) { return q[sel] || (q[sel] = el()); }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, remove() {}, insertAdjacentHTML() {} };
}
function ctxOf() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open() {} send() { this.status = 404; } }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c03-blotter.html', pathname: '/concepts/c03-blotter.html', search: '', hostname: '127.0.0.1', protocol: 'http:' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, Promise, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }),
    MutationObserver: class { observe() {} }, XMLHttpRequest: XHR, fetch: () => new Promise(() => {}) };
  ctx.window = ctx;
  vm.createContext(ctx);
  return ctx;
}
// демо-данные (lib/mock.js) — для самих столбцов
function loadMock() {
  const ctx = ctxOf();
  vm.runInContext(P('concepts/lib/mock.js'), ctx);
  ctx.window.CONCEPTS_SOURCE = 'real';
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
// прогон из фикстуры и extras сервера — для lib/real.js
function loadReal(extras) {
  const ctx = ctxOf();
  loadRun(ctx);
  ctx.window.DESK_EXTRAS = extras;
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.CONCEPTS_RUN };
}
const strip = h => h.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const cls = h => (/^<span class="([^"]*)"/.exec(h) || [])[1];

t('«Сделки за день»: куплено — класс pos (зелёный), продано — neg (красный), нетто 0 — без цвета; продано в ноль — по штукам', () => {
  const { K } = loadMock();
  const col = K.posCols.tradeDay;
  const bought = { id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 1, counted: 1, priced: 1, bonds: 4, face: 4e6, avg: 99 }, net: { bonds: 4, face: 4e6 }, n: 1 } };
  const sold = { id: 'X', ccy: 'RUB', tradeToday: { sell: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3e6, avg: 98 }, net: { bonds: -3, face: -3e6 }, n: 1 } };
  const flat = { id: 'X', ccy: 'RUB', tradeToday: { buy: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 99 }, sell: { n: 1, counted: 1, priced: 1, bonds: 3, face: 3000, avg: 101 }, net: { bonds: 0, face: 0 }, n: 2 } };
  const zero = { id: 'X', ccy: 'RUB', tradeToday: { sell: { n: 1, counted: 1, priced: 1, bonds: 9, face: null, avg: 98 }, net: { bonds: -9, face: null }, n: 1 } };
  assert.strictEqual(cls(col.fmt(col.val(bought), bought)), 'pos');
  assert.strictEqual(cls(col.fmt(col.val(sold), sold)), 'neg');
  assert.strictEqual(cls(col.fmt(col.val(flat), flat)), '');
  assert.strictEqual(cls(col.fmt(col.val(zero), zero)), 'neg');
  assert.ok(/^\+4\.00/.test(strip(col.fmt(col.val(bought), bought))), strip(col.fmt(col.val(bought), bought)));
  assert.strictEqual(col.fmt(null, { id: 'X', ccy: 'RUB' }), '', 'без сделок — пусто, как раньше');
});

const RUN = { runId: 7, date: '2026-10-20', closed: [] };
const pos = o => Object.assign({ id: 'X', ccy: 'RUB', notional: 12e6, prevNotional: 10e6, newToPrev: false, prevNoFact: false }, o);

t('«Изм. к вчера»: номинал сегодня минус вчера, mm в валюте бумаги; больше — pos, меньше — neg, без изменений — точка', () => {
  const { M, K } = loadMock();
  M.prevRun = RUN;
  const col = K.posCols.posDelta;
  assert.strictEqual(col.label, 'Изм. к вчера');
  assert.strictEqual(col.unit, 'mm в вал.');
  const up = pos({}), down = pos({ notional: 7e6, ccy: 'USD' }), same = pos({ notional: 10e6 }), noise = pos({ notional: 10e6 + 0.004 });
  assert.strictEqual(col.val(up), 2e6);
  assert.strictEqual(col.val(down), -3e6);
  assert.strictEqual(cls(col.fmt(col.val(up), up)), 'pos');
  assert.strictEqual(cls(col.fmt(col.val(down), down)), 'neg');
  assert.ok(/^\+2\.00 mm RUB$/.test(strip(col.fmt(col.val(up), up))), strip(col.fmt(col.val(up), up)));
  assert.ok(/mm USD$/.test(strip(col.fmt(col.val(down), down))));
  assert.strictEqual(col.val(same), 0);
  assert.strictEqual(col.val(noise), 0, 'округление номиналов прогона (до копеек) — не изменение');
  assert.ok(/^·$/.test(strip(col.fmt(0, same))), strip(col.fmt(0, same)));
  // подсказка: что было и что стало, прогон какой даты
  assert.ok(/В прогоне за 20\.10\.26 номинал был 10\.00 mm RUB, сейчас 12\.00/.test(K.prevDelta(up).tip), K.prevDelta(up).tip);
  assert.ok(/амортизация, индексация/.test(K.prevDelta(up).tip));
});

t('«Изм. к вчера»: новая позиция — весь номинал и пометка «новая»', () => {
  const { M, K } = loadMock();
  M.prevRun = RUN;
  const col = K.posCols.posDelta, fresh = pos({ prevNotional: null, newToPrev: true });
  assert.strictEqual(col.val(fresh), 12e6);
  const h = col.fmt(col.val(fresh), fresh);
  assert.ok(/^\+12\.00 mm RUB новая$/.test(strip(h)), strip(h));
  assert.strictEqual(cls(h), 'pos');
  assert.ok(/Новая позиция: в прогоне за 20\.10\.26 её не было/.test(h), h);
});

t('«Изм. к вчера»: нет прогона прошлой даты — прочерк с подсказкой, null и пусто в CSV; позиция была, но не оценена — не «новая»', () => {
  const { M, K } = loadMock();
  const col = K.posCols.posDelta;
  M.prevRun = null;
  assert.strictEqual(col.val(pos({})), null);
  assert.ok(/title="нет прогона прошлой даты"/.test(col.fmt(null, pos({}))));
  assert.strictEqual(col.csvFmt(null, pos({})), '');
  M.prevRun = RUN;
  const unvalued = pos({ prevNotional: null, prevNoFact: true });
  assert.strictEqual(col.val(unvalued), null);
  const h = col.fmt(null, unvalued);
  assert.ok(!/новая/.test(h) && /позиция была, но не оценена/.test(h), h);
  assert.strictEqual(col.csvFmt(null, unvalued), '');
  assert.ok(/не оценена в одном из двух прогонов/.test(col.fmt(null, pos({ prevNotional: null }))), 'сегодня не оценена');
});

t('«Изм. к вчера»: CSV — число в mm валюты бумаги, у «новой» тоже; ноль — 0.00', () => {
  const { M, K } = loadMock();
  M.prevRun = RUN;
  const col = K.posCols.posDelta;
  assert.strictEqual(col.csvFmt(col.val(pos({})), pos({})), '2.00');
  // выгрузка (K.table) пишет числа в минусовом формате (знак «−»), а не скобками; K.csvNum приводит его к «-3.00»
  K.withNegFmt('minus', () => assert.strictEqual(col.csvFmt(col.val(pos({ notional: 7e6 })), pos({ notional: 7e6 })), '−3.00'));
  assert.strictEqual(col.csvFmt(0, pos({ notional: 10e6 })), '0.00');
  const fresh = pos({ prevNotional: null, newToPrev: true });
  assert.strictEqual(col.csvFmt(col.val(fresh), fresh), '12.00');
});

t('закрытые с прошлой даты: строка с числом и суммой по валютам, подсказка — список; нет закрытых или нет прогона — пусто', () => {
  const { M, K } = loadMock();
  M.prevRun = null;
  assert.strictEqual(K.prevClosedLine(), '');
  M.prevRun = Object.assign({}, RUN);
  assert.strictEqual(K.prevClosedLine(), '');
  const book = M.books[0].id;
  M.prevRun = { runId: 7, date: '2026-10-20', closed: [
    { positionId: 'A', book, ccy: 'RUB', name: 'Бумага А', notional: 150e6 }, { positionId: 'B', book, ccy: 'RUB', name: 'Бумага Б', notional: 50e6 },
    { positionId: 'C', book, ccy: 'USD', name: 'Бумага В', notional: 5e6 }] };
  const h = K.prevClosedLine();
  // отрицательное — в формате пользователя (по умолчанию скобки): «(200.0) mm RUB»; закрытие — уменьшение номинала
  K.withNegFmt('minus', () => assert.ok(/^Закрыто с 20\.10: 3 поз\. · −200\.0 mm RUB · −5\.0 mm USD$/.test(strip(K.prevClosedLine())), strip(K.prevClosedLine())));
  assert.ok(/^Закрыто с 20\.10: 3 поз\. · \(200\.0\) mm RUB · \(5\.0\) mm USD$/.test(strip(h)), strip(h));
  assert.ok(/mm RUB/.test(strip(h)) && /mm USD/.test(strip(h)), strip(h));
  assert.ok(/Были в прогоне за 20\.10\.26, в этом прогоне их нет/.test(h) && /Бумага А — 150\.00 mm RUB/.test(h), h);
});

t('lib/real.js: prevNotional, newToPrev, prevNoFact позиции и M.prevRun — из extras сервера; без prevRun — null', () => {
  let ids;
  const probe = loadReal({ positions: {} });
  ids = probe.D.positions.slice(0, 4).map(p => p.id);
  assert.strictEqual(probe.M.prevRun, null, 'сервер prevRun не прислал (нет прогона прошлой даты)');
  assert.ok(probe.M.positions.every(p => p.prevNotional == null && !p.newToPrev && !p.prevNoFact));
  const xs = { positions: { [ids[0]]: { prevNotional: 5e6 }, [ids[1]]: { newToPrev: true }, [ids[2]]: { prevNoFact: true } },
    prevRun: { runId: 3, date: '2026-10-20', closed: [{ positionId: 'Z', book: 'B1', ccy: 'EUR', name: 'Закрытая', notional: 2e6 }] } };
  const { M, K } = loadReal(xs);
  assert.strictEqual(M.pos[ids[0]].prevNotional, 5e6);
  assert.strictEqual(M.pos[ids[1]].newToPrev, true);
  assert.strictEqual(M.pos[ids[2]].prevNoFact, true);
  assert.strictEqual(M.pos[ids[3]].prevNotional, null);
  assert.deepStrictEqual(JSON.parse(JSON.stringify(M.prevRun)), { runId: 3, date: '2026-10-20', closed: [{ positionId: 'Z', book: 'B1', ccy: 'EUR', name: 'Закрытая', notional: 2e6 }] });
  const p = M.pos[ids[0]], d = K.prevDelta(p);
  assert.strictEqual(d.face, p.notional - 5e6);
});

t('столбец в C03 (наборы «Базовый», «Риск», «Всё», рядом с «Номинал» и «Сделки за день») и в таблице C01', () => {
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/posDelta: Object\.assign\(K\.prevDeltaCol\(\)/.test(c03), 'описание колонки C03 — общее');
  for (const key of ['base', 'risk', 'all']) {
    const m = new RegExp("\\['" + key + "', '[^']+', \\[([^\\]]*)\\]\\]").exec(c03);
    assert.ok(m && /'notional', 'tradeDay', 'posDelta'/.test(m[1]), 'набор ' + key);
  }
  assert.ok(/K\.cols\(\[[^\]]*'tradeDay', 'posDelta'/.test(P('concepts/c01-cockpit.html')), 'C01');
  assert.ok(/K\.prevClosedLine\(\)/.test(c03), 'строка закрытых позиций в шапке таблицы C03');
});

console.log(n + ' tests');
