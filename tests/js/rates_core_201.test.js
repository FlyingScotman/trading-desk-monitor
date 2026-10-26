// #201: свопы и РЕПО, оценённые ядром, в мониторе (prototypes/concepts/). На прогоне lib/real.js читает extra.swaps /
// extra.repos (условия и цифры ядра по сделке), extra.index_fixings (последний фиксинг, объявленный неопубликованный
// день, пропуски внутри истории), reported_pv_rub / pv_diff_rub и quality 'assumed' («условия предварительные») в
// p.ratesCore, фиксинги прогона — в M.indexFixings, параллельный отчёт /api/v1/runs/{id}/derivatives — M.derivParallel().
// Панель C02 (kit.js K.ratesCorePanel): фиксинги, ядро и отчёт, риск по кривым RUONIA и КС, пометка «предварительно».
// Прогон до оценки свопов ядром (без extra.swaps) — «нет данных», а не ошибка.
//   node tests/js/rates_core_201.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const NB = 11, at = (k, v) => Array.from({ length: NB }, (_, i) => i === k ? v : 0);

// маленький DOM: столько, сколько нужно K.table и панели без вёрстки; html(el) — вся разметка поддерева
function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {},
    getBoundingClientRect: () => ({ width: 80 }) };
}
const html = e => e.innerHTML + e.children.map(html).join('');
function load(opts) {
  opts = opts || {};
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const asked = [];
  class XHR {
    open(m, url) { asked.push(url); this.url = url; }
    send() { const j = opts.derivatives; this.status = j ? 200 : 404; this.responseText = j ? JSON.stringify(j) : ''; }
  }
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} },
    XMLHttpRequest: XHR };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (opts.real) {
    loadRun(ctx);
    if (opts.mutate) opts.mutate(ctx.window.CONCEPTS_RUN);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, D: ctx.window.CONCEPTS_RUN, asked };
}

// строки прогона в форме tdm_run.direct_row (backend valuation/derivatives.py: _swap_fact, _repo_fact, index_fixings)
const RUONIA = { snapshot_id: 'fx-1', known_at: '2026-10-15T09:00:00Z', first: '2025-10-01', latest: '2026-10-13', latest_rate_pct: 16.2,
  unpublished_business_days: ['2026-10-14'], max_unpublished_business_days: 1, missing_business_days: [] };
const SWAP = { trade_id: 'T-OIS-1', product: 'ois', index: 'RUONIA', side: 'payer', notional_rub: 1e9, fixed_rate_pct: 15.5, spread_bp: 0,
  start_date: '2026-04-15', maturity_date: '2028-04-15', convention: 'rub_ruonia_ois_moex_v2', pv_rub: 2.5e6, clean_pv_rub: 2.4e6, accrued_rub: 1e5,
  fixed_leg_pv_rub: -2e8, float_leg_pv_rub: 2.025e8, par_rate_pct: 15.62, annuity: 1.7, discount_curve: 'RUB RUONIA', projection_curve: 'RUB RUONIA',
  fixing_id: 'RUONIA', fixing_snapshot_id: 'fx-1', reported_pv_rub: 2.4e6 };
const REPO = { trade_id: 'R-1', side: 'reverse_repo', start_date: '2026-10-01', end_date: '2027-01-12', fixed_rate_pct: null, index: 'KEY_RATE', spread_bp: -50,
  start_amount_rub: 5e8, end_amount_rub: 5.2e8, end_amount_source: 'projected', known_interest_rub: 1e6, accrued_interest_rub: 2e6,
  current_repurchase_amount_rub: 5.02e8, pv_rub: 1.1e6, discount_curve: 'RUB RUONIA', projection_curve: 'RUB KEY', fixing_id: 'KEY_RATE', fixing_snapshot_id: 'fx-1',
  security: 'RU000A0JX0J2' };
const runId = D => { D.source = Object.assign({}, D.source, { runId: 42 }); };
function withRates(D) {
  runId(D);
  const b = D.positions.find(p => p.instrType === 'FX') || D.positions[0];
  D.positions.push(Object.assign({}, b, { id: 'S1', name: 'OIS RUONIA 2Y', instrType: 'OTC', ccy: 'RUB', notional: 1e9, mvRub: 2.5e6, mv: 2.5e6, fxDelta: {},
    risk: { 'RUB RUONIA': at(3, 18000) }, pnl: null,
    extra: { valuation: 'computed', quality: 'assumed', assumptions: ['RUONIA not yet published for 2026-10-14: the latest fixing of 2026-10-13 carried'],
      core: { product: 'ois', trades: ['T-OIS-1'] }, swaps: [SWAP], index_fixings: { RUONIA }, reported_pv_rub: 2.4e6, pv_diff_rub: 1e5 } }));
  D.positions.push(Object.assign({}, b, { id: 'S2', name: 'РЕПО на КС', instrType: 'OTC', ccy: 'RUB', notional: 5e8, mvRub: 1.1e6, mv: 1.1e6, fxDelta: {},
    risk: { 'RUB KEY': at(1, -900), 'RUB RUONIA': at(1, -40) }, pnl: null,
    extra: { valuation: 'computed', quality: 'exact', assumptions: [], core: { product: 'repo' }, repos: [REPO],
      index_fixings: { KEY_RATE: { latest: '2026-10-15', latest_rate_pct: 16.5, unpublished_business_days: [], missing_business_days: ['2026-06-12'] },
        RUONIA: Object.assign({}, RUONIA, { latest: '2026-10-12', latest_rate_pct: 16.1, missing_business_days: ['2026-05-04'] }) } } }));
}

t('прогон: extra.swaps / extra.repos / index_fixings — в p.ratesCore, фиксинги прогона — в M.indexFixings', () => {
  const { M } = load({ real: true, mutate: withRates });
  const s = M.pos.S1.ratesCore, r = M.pos.S2.ratesCore;
  assert.strictEqual(s.trades.length, 1);
  const tr = s.trades[0];
  assert.deepStrictEqual([tr.kind, tr.tradeId, tr.product, tr.index, tr.side, tr.notional, tr.fixedRate, tr.parRate, tr.start, tr.end],
    ['swap', 'T-OIS-1', 'ois', 'RUONIA', 'payer', 1e9, 15.5, 15.62, '2026-04-15', '2028-04-15']);
  assert.strictEqual(tr.pv, 2.5e6); assert.strictEqual(tr.reportedPv, 2.4e6); assert.ok(Math.abs(tr.diff - 1e5) < 1e-6);
  assert.strictEqual(s.provisional, true, 'quality assumed — условия предварительные');
  assert.ok(s.assumptions[0].includes('not yet published'));
  assert.deepStrictEqual([s.pv, s.reportedPv, s.pvDiff], [2.5e6, 2.4e6, 1e5]);
  const rp = r.trades[0];
  assert.deepStrictEqual([rp.kind, rp.product, rp.side, rp.index, rp.spreadBp, rp.notional, rp.end, rp.endAmount, rp.parRate],
    ['repo', 'repo', 'reverse_repo', 'KEY_RATE', -50, 5e8, '2027-01-12', 5.2e8, null]);
  assert.strictEqual(r.provisional, false);
  assert.strictEqual(r.reportedPv, null, 'нет PV отчёта — null, не 0');
  // риск по кривым — из risk строки (RUONIA и КС)
  assert.strictEqual(M.pos.S1.dv01ByCurve['RUB RUONIA'], 18000);
  assert.strictEqual(M.pos.S2.dv01ByCurve['RUB KEY'], -900);
  // фиксинги прогона: последний — самый поздний, пропуски и неопубликованные — объединение по позициям
  const ru = M.indexFixings.RUONIA;
  assert.deepStrictEqual([ru.latest, ru.latestRate, ru.maxUnpublished], ['2026-10-13', 16.2, 1]);
  assert.deepStrictEqual(Array.from(ru.unpublished), ['2026-10-14']);
  assert.deepStrictEqual(Array.from(ru.missing), ['2026-05-04']);
  assert.deepStrictEqual(Array.from(M.indexFixings.KEY_RATE.missing), ['2026-06-12']);
  assert.ok(M.notes.some(x => x.startsWith('фиксинги RUONIA: пропуски') && x.includes('2026-05-04')), M.notes.join(' | '));
  assert.ok(M.notes.some(x => x.startsWith('фиксинги KEY_RATE: пропуски')));
  assert.strictEqual(M.positions.filter(p => p.ratesCore).length, 2, 'остальные строки — null');
});

t('прогон: панель C02 — фиксинги, «предварительно» с допущениями, риск по двум кривым, обе таблицы', () => {
  const { M, K } = load({ real: true, mutate: withRates });
  const box = el(), R = K.ratesCorePanel(box), out = html(box);
  assert.strictEqual(R.rows.length, 2); assert.strictEqual(R.trades.length, 2);
  assert.ok(out.includes('последний фиксинг 13.10.26') && out.includes('16.20%'), 'дата и значение последнего фиксинга RUONIA');
  assert.ok(out.includes('не опубликован за 14.10.26') && out.includes('штатно'), 'объявленный неопубликованный день — нейтрально');
  assert.ok(!/⚠ не опубликован/.test(out), 'перенос в пределах объявленного — без ⚠');
  assert.ok(out.includes('пропуски в истории: 04.05.26'), 'пропуск фиксинга');
  // единственное допущение S1 — перенос фиксинга: тихая метка «фикс. перенесён», подсказка по-русски
  assert.ok(out.includes('фикс. перенесён') && out.includes('ещё не опубликован: перенесён последний (13.10.26)') && !out.includes('not yet published'), 'метка с подсказкой');
  assert.ok(!out.includes('>предварительно<'), 'не «предварительно»');
  assert.ok(out.includes('DV01 RUONIA') && out.includes('DV01 КС'));
  assert.ok(out.includes('T-OIS-1') && out.includes('обратное РЕПО'));
  const s1 = R.rows.find(r => r.id === 'S1');
  assert.deepStrictEqual([s1.ruo, s1.key, s1.flag, s1.src], [18000, 0, 'carry', 'ядро']);
  assert.ok(Math.abs(s1.diff - 1e5) < 1e-6);
  assert.strictEqual(R.rows.find(r => r.id === 'S2').key, -900);
  assert.ok(!/NaN|undefined/.test(out), 'без NaN/undefined');
});

t('прогон без extra.swaps (старый): нет данных словами, не падает; /derivatives не ответил — «недоступен», не молча', () => {
  const { M, K, asked } = load({ real: true, mutate: runId });
  assert.ok(M.positions.every(p => p.ratesCore === null));
  assert.deepStrictEqual(Object.keys(M.indexFixings), []);
  const par = M.derivParallel();
  assert.deepStrictEqual([par.state, par.status, par.rows.length], ['unavailable', 404, 0], 'нет ответа /derivatives — состояние и код');
  assert.ok(asked.includes('../api/v1/runs/42/derivatives'), asked.join(' '));
  const box = el(); K.ratesCorePanel(box);
  assert.ok(box.innerHTML.includes('нет данных') && box.innerHTML.includes('extra.swaps'), box.innerHTML);
  // кривые поля: не массив, null-элементы, index_fixings не объект — тоже без ошибки
  let ids;
  const { M: M2, K: K2 } = load({ real: true, mutate: D => {
    ids = [D.positions[0].id, D.positions[1].id];
    D.positions[0].extra = Object.assign({}, D.positions[0].extra, { swaps: 'x', repos: [null, 5], index_fixings: [1] });
    D.positions[1].extra = Object.assign({}, D.positions[1].extra, { swaps: [{ trade_id: 7 }], index_fixings: { RUONIA: null } });
  } });
  assert.strictEqual(M2.pos[ids[0]].ratesCore, null);
  const odd = M2.pos[ids[1]];
  assert.strictEqual(odd.ratesCore.trades[0].tradeId, '7');
  assert.strictEqual(odd.ratesCore.trades[0].pv, null);
  assert.deepStrictEqual(Object.keys(odd.ratesCore.fixings), []);
  const box2 = el(); K2.ratesCorePanel(box2);
  assert.ok(!/NaN|undefined/.test(html(box2)), html(box2));
});

t('прогон: параллельный отчёт /derivatives — допуск, «выше допуска», строки по отчёту с причиной', () => {
  const derivatives = { run_id: 5, state: 'computed', tolerance: { abs_rub: 10000, rel: 0.01, provisional: true }, totals: { n: 3 },
    by_book: {}, by_product: {}, reasons: { ccs_unsupported: 1 }, rows: [
      { position_id: 'S1', book_id: 'B', product: 'ois', status: 'both', used: 'computed', computed_pv_rub: 2.5e6, imported_pv_rub: 2.4e6, diff_rub: 1e5, over: true },
      { position_id: 'C9', book_id: 'B', product: 'ccs', status: 'imported_only', used: 'imported', computed_pv_rub: null, imported_pv_rub: 3e5, reason: 'ccs_unsupported' },
      { position_id: 'F1', book_id: 'B', product: 'fx_forward', status: 'both', used: 'computed', computed_pv_rub: 1, imported_pv_rub: 1, diff_rub: 0, over: false },
      { position_id: 'X', book_id: 'B', product: 'repo', status: 'imported_only', used: 'imported', computed_pv_rub: null, imported_pv_rub: 1, reason: 'core_fixing_missing' }] };
  const { M, K } = load({ real: true, mutate: withRates, derivatives });
  const par = M.derivParallel();
  assert.strictEqual(par.state, 'computed'); assert.strictEqual(par.rows.length, 4);
  assert.deepStrictEqual([par.rows[1].positionId, par.rows[1].imported, par.rows[1].computed, par.rows[1].reason], ['C9', 3e5, null, 'ccs_unsupported']);
  assert.strictEqual(M.derivParallel(), par, 'один запрос на страницу');
  const R = K.ratesCoreModel();
  assert.strictEqual(R.rows.find(r => r.id === 'S1').over, true);
  const rep = R.rows.filter(r => r.src === 'по отчёту');
  assert.deepStrictEqual(Array.from(rep, r => r.id), ['C9', 'X'], 'FX — не сюда; строки по отчёту — свопы и РЕПО');
  assert.strictEqual(K.rcReason('ccs_unsupported'), 'CCS ядром пока не оценивается');
  assert.strictEqual(K.rcReason('core_fixing_missing'), 'ядро отклонило: fixing_missing');
  const box = el(); K.ratesCorePanel(box); const out = html(box);
  assert.ok(out.includes('выше допуска') && out.includes('по отчёту: CCS') && out.includes('10,000'), out.slice(0, 400));
  assert.strictEqual(M.parseDerivParallel(null), null);
  assert.deepStrictEqual(Array.from(M.parseDerivParallel({ state: 'not_computed', rows: [] }).rows), []);
});

t('демо: та же форма — предварительная сделка, РЕПО, пропуск и неопубликованный день RUONIA, строки по отчёту', () => {
  const { M, K } = load();
  const core = M.positions.filter(p => p.ratesCore);
  assert.ok(core.length >= 5);
  assert.ok(core.some(p => p.ratesCore.provisional && p.quality === 'assumed'), 'одна сделка с quality assumed');
  assert.ok(core.some(p => !p.ratesCore.provisional), 'и точные');
  assert.ok(core.some(p => p.ratesCore.trades[0].kind === 'repo'), 'РЕПО');
  assert.ok(core.some(p => p.dv01ByCurve['RUB KEY']) && core.some(p => p.dv01ByCurve['RUB RUONIA']), 'риск по обеим кривым');
  const ru = M.indexFixings.RUONIA;
  assert.ok(ru.missing.length === 1 && ru.unpublished.length === 1 && ru.latest && ru.latestRate != null);
  // пропуск — раньше старта сделок, которые читают RUONIA (иначе ядро их отклонило бы)
  core.forEach(p => p.ratesCore.trades.filter(x => x.fixingId === 'RUONIA').forEach(x => assert.ok(x.start > ru.missing[0], x.tradeId)));
  const box = el(), R = K.ratesCorePanel(box), out = html(box);
  assert.ok(R.rows.some(r => r.src === 'по отчёту'));
  assert.ok(out.includes('предварительно') && out.includes('не опубликован') && out.includes('пропуски в истории'));
  assert.ok(!/NaN|undefined/.test(out));
});

// ревью PR #233: итоги по строкам с обеими оценками, CSV без значков, перенос фиксинга отдельно от допущений, риск
// плиток без строк «по отчёту», нейтральная разница, состояние /derivatives, словарь причин, фильтр строк без позиции
const findBtn = (e, text) => e.innerHTML === text && e.onclick ? e : e.children.map(c => findBtn(c, text)).find(Boolean) || null;
function csvOf(K, box, n) {   // n-я таблица панели: CSV через «⤓ CSV»
  let got = []; K.download = (name, text) => got.push(text);
  const btns = []; (function walk(e) { if (e.innerHTML === '⤓ CSV' && e.onclick) btns.push(e); e.children.forEach(walk); })(box);
  btns[n].onclick(); return got[0].replace(/^﻿/, '').split('\r\n');
}
// строка CSV в ячейки (кавычки с запятыми внутри)
const cells = l => { const out = []; let cur = '', q = false; for (const ch of l) { if (ch === '"') q = !q; else if (ch === ',' && !q) { out.push(cur); cur = ''; } else cur += ch; } out.push(cur); return out; };
const DER = { run_id: 42, state: 'computed', tolerance: { abs_rub: 10000, rel: 0.01, provisional: true }, rows: [
  { position_id: 'S1', book_id: 'B', product: 'ois', status: 'both', used: 'computed', computed_pv_rub: 2.5e6, imported_pv_rub: 2.4e6, diff_rub: 1e5, over: true },
  { position_id: 'C9', book_id: 'B', product: 'ccs', status: 'imported_only', used: 'imported', computed_pv_rub: null, imported_pv_rub: 3e5, reason: 'ccs_unsupported' }] };
function withMore(D) {
  withRates(D);
  const b = D.positions.find(p => p.id === 'S1');
  // предварительная (не только перенос фиксинга) и CCS по отчёту с риском RUONIA
  D.positions.push(Object.assign({}, b, { id: 'S3', name: 'IRS КС 3Y', mvRub: -4e6, mv: -4e6, risk: { 'RUB KEY': at(4, 5000) },
    extra: Object.assign({}, b.extra, { assumptions: ['swap convention from the exchange (provisional)'], swaps: [Object.assign({}, SWAP, { trade_id: 'T-IRS-3', index: 'KEY_RATE', pv_rub: -4e6, reported_pv_rub: -3.9e6 })],
      index_fixings: {}, reported_pv_rub: -3.9e6, pv_diff_rub: -1e5 }) }));
  D.positions.push(Object.assign({}, b, { id: 'C9', name: 'CCS USD/RUB', mvRub: 3e5, mv: 3e5, risk: { 'RUB RUONIA': at(2, 300000) }, extra: { valuation: 'reported', quality: 'reported_value' } }));
}

t('ревью: итоги PV по строкам с обеими оценками, строки «по отчёту» и их риск — отдельно', () => {
  const { K } = load({ real: true, mutate: withMore, derivatives: DER });
  const box = el(), R = K.ratesCorePanel(box), T = R.totals, out = html(box);
  // S1 (2.5 / 2.4), S3 (−4 / −3.9); S2 — без отчёта, C9 — по отчёту
  assert.ok(Math.abs(T.pv - (2.5e6 - 4e6)) < 1e-6 && Math.abs(T.rep - (2.4e6 - 3.9e6)) < 1e-6);
  assert.ok(Math.abs(T.pv - T.rep - T.diff) < 1e-6, 'ядро − отчёт = разница');
  assert.deepStrictEqual([T.nBoth, T.nCoreOnly, T.nRep, T.repPv, T.nOver], [2, 1, 1, 3e5, 1]);
  assert.ok(out.includes('ещё по отчёту: 1 поз.') && out.includes('только ядро (отчёта нет): 1'), out.slice(0, 600));
  // плитки DV01 — только ядро; риск CCS подписан
  assert.strictEqual(T.ruo, 18000 - 40); assert.strictEqual(T.repRuo, 300000);
  assert.ok(out.includes('без строк по отчёту; в них ещё 300'));
  // «Итого» таблицы — те же суммы: ядро (1.50), отчёт (1.50), разница 0.00, DV01 RUONIA 18 (без CCS)
  const foot = (out.match(/<tfoot>[\s\S]*?<\/tfoot>/) || [''])[0];
  assert.ok(/>\(1\.50\)<\/td><td[^>]*>\(1\.50\)<\/td><td[^>]*>0\.00<\/td><td[^>]*>18<\/td>/.test(foot), foot);
  // «Итого» говорит, по скольким строкам посчитан: PV — 2 строки с обеими оценками, DV01 — 3 строки ядра из 4
  assert.ok(foot.includes('Итого <span class="mut3" style="font-weight:400">PV 2, DV01 3 из 4</span>'), foot);
  const tfoot2 = (out.match(/<tfoot>[\s\S]*?<\/tfoot>/g) || [])[1] || '';
  assert.ok(tfoot2.includes('>2 из 3</span>'), tfoot2);
  // ячейки и итог разницы — явным нейтральным цветом (иначе «(1)» покрасил бы скобки красным)
  assert.ok(/style="color:var\(--fg\)">\(0\.10\)/.test(out) && /style="color:var\(--fg\)">0\.00/.test(foot), 'нейтральный цвет');
  // разница — нейтральная: в итоге без класса знака
  assert.ok(!/class="n (pos|neg)"[^<]*>0\.00/.test(out));
});

t('ревью: CSV — числа числами, имя без меток, отметки отдельной колонкой', () => {
  const { K } = load({ real: true, mutate: withMore, derivatives: DER });
  const box = el(); K.ratesCorePanel(box);
  const csv = csvOf(K, box, 0), head = cells(csv[2]), col = name => head.findIndex(h => h.startsWith(name));
  const row = cells(csv.find(l => l.startsWith('OIS RUONIA 2Y,')));
  assert.strictEqual(row[0], 'OIS RUONIA 2Y', 'имя без «фикс. перенесён»');
  assert.strictEqual(row[col('Ядро − отчёт')], '0.10', '«0.10 ⚠» — число без значка');
  assert.strictEqual(row[col('Отметки')], 'выше допуска, фикс. перенесён');
  const s3 = cells(csv.find(l => l.startsWith('IRS КС 3Y,')));
  assert.deepStrictEqual([s3[0], s3[col('Ядро − отчёт')], s3[col('Отметки')]], ['IRS КС 3Y', '-0.10', 'предварительно']);
  const trd = csvOf(K, box, 1);
  assert.ok(trd.some(l => l.startsWith('T-OIS-1,')), trd.join('\n'));
  // итог сделок: ядро и отчёт — по сделкам с обеими оценками (S1 2.5 / 2.4, S3 −4 / −3.9; РЕПО без отчёта — нет)
  const tf = (html(box).match(/<tfoot>[\s\S]*?<\/tfoot>/g) || [])[1] || '';
  assert.ok(/>\(1\.50\)<\/td><td[^>]*>\(1\.50\)<\/td><td[^>]*>0\.00<\/td>/.test(tf), tf);
});

t('ревью: перенос фиксинга и прочие допущения — раздельные счётчики; перенос сверх объявленного — ⚠', () => {
  const { K, M } = load({ real: true, mutate: withMore, derivatives: DER });
  const box = el(), R = K.ratesCorePanel(box), out = html(box);
  assert.deepStrictEqual([R.totals.nProv, R.totals.nCarry], [1, 1]);
  assert.ok(out.includes('предварительно 1') && out.includes('фикс. перенесён 1'));
  assert.strictEqual(K.rcFlag({ provisional: true, assumptions: ['RUONIA not yet published for 2026-10-14: the latest fixing of 2026-10-13 carried', 'x'] }), 'prov');
  assert.strictEqual(K.rcFlag({ provisional: false, assumptions: [] }), null);
  M.indexFixings.RUONIA.unpublished = ['2026-10-13', '2026-10-14'];
  assert.ok(/⚠ не опубликован за 13\.10\.26, 14\.10\.26.*больше, чем объявлено/.test(K.rcFixingsHtml()));
});

t('ревью: /derivatives не посчитан или недоступен — строкой; причины бэкенда — словами; строки без позиции — по книге фильтра', () => {
  const { K } = load({ real: true, mutate: withRates, derivatives: { run_id: 42, state: 'not_computed', rows: [] } });
  const box = el(); K.ratesCorePanel(box);
  assert.ok(html(box).includes('не посчитан (прогон до него)'));
  const { K: K2 } = load({ real: true, mutate: withRates });
  const b2 = el(); K2.ratesCorePanel(b2);
  assert.ok(html(b2).includes('недоступен (HTTP 404)'));
  ['repo_index_unsupported', 'no_key_rate_curve', 'no_rub_curve', 'swap_dates_mismatch', 'swap_notional_mismatch', 'terms_mixed', 'not_valued']
    .forEach(c => assert.ok(K.rcReason(c) !== c && /[а-я]/.test(K.rcReason(c)), c));
  // демо: строка «открытое РЕПО» без позиции — видна при фильтре своей книги, скрыта при чужой
  const { M: M3, K: K3 } = load();
  const shown = books => { K3.filter.books = books; return K3.ratesCoreModel().rows.some(r => r.id === 'DEMO-REPO-OPEN'); };
  assert.ok(shown(null), 'без фильтра');
  assert.ok(shown(['RATES']), 'фильтр своей книги');
  assert.ok(!shown(['FXB']), 'фильтр чужой книги');
  K3.filter.books = null;
  // демо: обратное РЕПО — PV кредитора положительный, около первой части; риск RUONIA отрицательный
  const repo = M3.positions.find(p => p.ratesCore && p.ratesCore.trades[0].kind === 'repo');
  assert.ok(repo.ratesCore.pv > 0.99 * repo.ratesCore.trades[0].notional && repo.dv01ByCurve['RUB RUONIA'] < 0);
});

t('C02: панель свопов и РЕПО — через K.ratesCorePanel и перерисовывается фильтром шапки', () => {
  const c02 = P('concepts/c02-risk-ladder.html');
  assert.ok(c02.includes('id="rates"') && /K\.ratesCorePanel\(document\.querySelector\('#rates \.bd'\)\)/.test(c02));
  assert.ok(/K\.onFilter\(\(\) => \{[^}]*drawRates\(\)/.test(c02));
});

t('#291: причина неточной оценки — из позиции модели (p.estReason, extra прогона), не из window.DESK', () => {
  const { M } = load({ real: true, mutate: D => {
    D.positions[0].extra = { reason: 'нет котировки' };
    D.positions[1].extra = { assumptions: ['фиксинг перенесён', 'условия предварительные'] };
  } });
  const [a, b, c] = M.positions;
  assert.strictEqual(a.estReason, 'нет котировки');
  assert.strictEqual(b.estReason, 'фиксинг перенесён; условия предварительные');
  assert.strictEqual(c.estReason, '');
  const kit = P('concepts/lib/kit.js');
  assert.ok(!/window\.DESK\b(?!_)/.test(kit) && /esc\(p\.estReason/.test(kit), 'K.posList берёт причину из p.estReason');
});

console.log(n + ' tests passed');
