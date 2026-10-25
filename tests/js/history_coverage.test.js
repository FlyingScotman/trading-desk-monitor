// R44 / R45: история позиции по прогонам и «Чего нет в данных» в мониторах (prototypes/concepts/).
// R44 — ряд DV01 / номинала / DM (K.runSeries) стоит на прогоне своей даты: дата без прогона и не оценённая позиция —
// разрыв, прогон прошлой версии программы — отдельный пунктир, другой метод оценки (резервная ступень) — маркер с
// причиной в подсказке; «на 1 бумагу» отделяет сделку от изменения риска на бумагу.
// R45 — не оценённые позиции по причинам и типам (M.unvalued с сервера, api/coverage.py), закрытые — отдельно; «нет
// курса X» — только если в валюте есть ненулевые суммы оценённых позиций; заметка со ссылкой на «Историю прогонов».
//   node tests/js/history_coverage.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const tests = [];
const t = (name, f) => tests.push([name, f]);

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
// opts.extras — window.DESK_EXTRAS; opts.patch(D) — правка window.DESK до real.js
function load(opts) {
  opts = opts || {};
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  class XHR { open(m, u) { this.u = u; } send() { this.status = 404; } }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  if (opts.patch) opts.patch(ctx.window.DESK);
  if (opts.extras) ctx.window.DESK_EXTRAS = opts.extras;
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.DESK };
}

// ряд флоатера на 8 датах: 0 — до истории прогонов, 1 — прогон прошлой версии, 2..3 — прогоны текущей, 4 — даты без
// прогона (значение перенесено сервером), 5 — резервная ступень (нет котировки), 6 — не оценена, 7 — сделка (+500 бумаг)
const H = {
  dates: ['2026-06-10', '2026-06-11', '2026-06-12', '2026-06-15', '2026-06-16', '2026-06-17', '2026-06-18', '2026-06-19'],
  runAt: [null, { id: 1, old: '0.2.0 (оценка v8, ядро 0.32.1)' }, { id: 2 }, { id: 3 }, null, { id: 5 }, { id: 6 }, { id: 7 }],
  dv01: [null, -30000, -27000, -26000, -26000, -6000, null, -37500],
  notional: [null, 1e6, 1e6, 1e6, 1e6, 1e6, null, 1.5e6],
  qty: [null, 1000, 1000, 1000, 1000, 1000, 1000, 1500],
  pt: { 5: ['method', 'нет котировки дня: старая цена, риск — по дюрации (no_quote)'], 6: ['unvalued', 'позиция не оценена (no_quote)'] },
};

t('R44: точки ряда — прогон своей даты; прошлая версия — пунктир, другой метод — маркер, без прогона — разрыв', () => {
  const { K } = load();
  const kinds = H.dates.map((_, i) => (K.histPoint(H, i) || { k: 'clean' }).k);
  assert.deepStrictEqual(kinds, ['before', 'old', 'clean', 'clean', 'gap', 'method', 'unvalued', 'clean']);
  const R = K.runSeries(H, 'dv01', { color: '#123' });
  const main = R.series[0].data;
  assert.deepStrictEqual(Array.from(main), [null, null, -27, -26, null, null, null, -37.5], 'основная линия — только ядро на прогоне даты');
  const old = R.series.find(s => s.name === 'прошлая версия расчёта' && s.type === 'line');
  assert.strictEqual(old.lineStyle.type, 'dashed'); assert.strictEqual(old.data[1], -30);
  const meth = R.series.find(s => s.name === 'другой метод оценки');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(meth.data)), [[5, -6]], 'всплеск резервной ступени — маркер, не линия');
  assert.ok(R.series.some(s => s.type === 'scatter' && s.data.some(([i]) => i === 7)), 'одиночная точка видна');
  // подсказка: число бумаг и сделка, причина точки
  const tip = R.tooltip.formatter([{ axisValue: '2026-06-19' }]);
  assert.ok(tip.includes('бумаг: 1,500') && tip.includes('+500'), tip);
  const tip5 = R.tooltip.formatter([{ axisValue: '2026-06-17' }]);
  assert.ok(tip5.includes('другой метод оценки') && tip5.includes('no_quote'), tip5);
  const tip4 = R.tooltip.formatter([{ axisValue: '2026-06-16' }]);
  assert.ok(tip4.includes('нет прогона за дату') && tip4.includes(': <b>—</b>'), 'перенесённое значение не показано: ' + tip4);
  // подпись: сколько точек каких, ссылка на досчёт
  assert.ok(R.note.includes('3 — ядро') && R.note.includes('1 — прошлой версией') && R.note.includes('1 — другим методом') &&
    R.note.includes('1 — позиция не оценена') && R.note.includes('1 — прогона за дату нет') && R.note.includes('settings.html#bf'), R.note);
});

t('R44: «на 1 бумагу» — риск на бумагу не меняется со сделкой, DM и номинал — те же правила', () => {
  const { K } = load();
  const per = K.runSeries(H, 'dv01', { perBond: true }).series[0];
  assert.strictEqual(per.name, 'DV01, ₽/бп на 1 бумагу');
  assert.strictEqual(per.data[3], -26); assert.strictEqual(per.data[7], -25, 'сделка: DV01 позиции вырос в 1,5 раза, на бумагу — нет');
  const dm = K.runSeries(Object.assign({}, H, { dm: [null, 110, 95, 96, null, 150, null, 97] }), 'dm').series[0];
  assert.deepStrictEqual(Array.from(dm.data), [null, null, 95, 96, null, null, null, 97]);
  const nt = K.runSeries(H, 'notional').series[0];
  assert.strictEqual(nt.data[7], 1.5);
  // ряд без сведений о прогонах (демо, старый сервер): как раньше, без разрывов
  const plain = K.runSeries({ dates: ['a', 'b'], dv01: [-1000, null], notional: [1, 1] }, 'dv01');
  assert.deepStrictEqual(Array.from(plain.series[0].data), [-1, null]); assert.strictEqual(plain.note, '');
  assert.ok(K.tips.floaterDv01.includes('до ближайшей фиксации') && K.tips.floaterDv01.includes('пилообразный'));
});

const UV = { total: 4, open: 3, closed: 1, fxMissing: ['OMR'], groups: [
  { key: 'swap_terms|IRS', cause: 'swap_terms', kind: 'IRS', kindLabel: 'Процентные свопы (IRS / OIS)', why: 'своп ждёт условий сделки (trade_terms)', todo: 'MV и DV01 отчёта — report_risk', closed: false, n: 2, mvRub: null, notionalRub: 150e6, noRate: [], ids: ['NV1', 'NV2'] },
  { key: 'no_report_mv|UNCLASSIFIED', cause: 'no_report_mv', kind: 'UNCLASSIFIED', kindLabel: 'Не классифицировано', why: 'оценки нет: MV в отчёте нет', todo: 'report_risk', closed: false, n: 1, mvRub: null, notionalRub: null, noRate: ['OMR'], ids: ['NV3'] },
  { key: 'closed|IRS', cause: 'closed', kind: 'IRS', kindLabel: 'Процентные свопы (IRS / OIS)', why: 'закрытая сделка или нулевой объём в отчёте', todo: 'не пропуск', closed: true, n: 1, mvRub: null, notionalRub: null, noRate: [], ids: ['NV4'] }] };
function addUnvalued(D) {
  const base = D.positions[0];
  ['NV1', 'NV2', 'NV3', 'NV4'].forEach((id, i) => D.positions.push(Object.assign({}, base, { id, name: id, isin: null, ccy: i === 2 ? 'OMR' : 'RUB', notValued: true, notional: null, notionalRub: null, mv: null, mvRub: null, risk: {}, fxDelta: {}, instrType: null, segment: 'Деривативы', assetClass: 'OTC-деривативы', extra: { quality: 'not_valued', reason: 'no_reported_value: otc ' + id + ': no mv_rub' } })));
}

t('R45: не оценённые — по причинам, закрытые отдельно; нет курса OMR у одной строки без MV — не общее предупреждение', () => {
  const { M, K } = load({ patch: addUnvalued, extras: { unvalued: UV, approx: [{ key: 'assumed|FX_FWD|1', quality: 'assumed', kind: 'FX_FWD', kindLabel: 'FX форварды / NDF', text: 'PV принят 0; условий сделки (trade_terms) нет: FX-дельта = номинал', n: 1, mvRub: 0, ids: [] }] } });
  assert.ok(!M.notes.some(x => x.startsWith('нет курса OMR')), M.notes.join(' | '));
  const note = M.notes.find(x => x.includes('не оценены'));
  assert.ok(note.startsWith('3 поз. не оценены') && note.includes('ещё 1 закрытых') && note.includes('нет курса OMR'), note);
  const html = K.coverageHtml();
  assert.ok(html.includes('Не оценены: 3 поз. + 1 закрытых') && html.includes('data-cg="u:0"') && html.includes('своп ждёт условий сделки'), html);
  assert.ok(html.includes('150.0') && html.includes('+ OMR') && html.includes('Оценены не ядром: 1 поз.') && html.includes('opacity:.6'), html);
  assert.ok(M.pos.NV3 && M.pos.NV3.ccy === 'OMR');
});

t('R45: курс нужен — предупреждение остаётся; ссылка на «Историю прогонов» в заметке', () => {
  const { M, K } = load({ patch: D => { addUnvalued(D); const p = Object.assign({}, D.positions[0], { id: 'OMRB', ccy: 'OMR', notValued: false, notional: 5e5, mvRub: null, fxDelta: { OMR: 5e5 } }); D.positions.push(p); } });
  assert.ok(M.notes.some(x => x.startsWith('нет курса OMR')), M.notes.join(' | '));
  assert.ok(M.notes.some(x => x.endsWith('поз. не оценены (нет данных): показаны без риска и MV')), 'без разбора с сервера — прежняя заметка');
  const h = K.noteHtml('PnL с начала года — частичный; досчитать: Настройки → «История прогонов» (desk backfill) <b>');
  assert.ok(h.includes('<a href="../settings.html#bf">Настройки → «История прогонов»</a>') && h.includes('&lt;b&gt;'), h);
});

t('#174: подсказка точки называет срез по-русски, без машинных имён; предварительное закрытие — как было', () => {
  const { K } = load();
  const tip = cut => K.histPoint({ dates: ['2026-06-10'], runAt: [{ id: 1, cut }] }, 0).t;
  assert.strictEqual(tip('prev_close'), 'закрытие прошлого дня');
  assert.strictEqual(tip('intraday'), 'внутридневной срез');
  assert.strictEqual(tip('eod_preliminary'), 'предварительное закрытие (финального среза за дату нет)');
  assert.strictEqual(tip('custom_cut'), 'срез custom_cut', 'неизвестный тип — как есть');
  ['prev_close', 'intraday'].forEach(c => assert.ok(!/prev_close|intraday|срез prev|срез intra/.test(tip(c)), tip(c)));
});

(async () => {
  for (const [name, f] of tests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
