// Публикуемая ОВП в мониторах (TDM#416): D.OCP прогона (api/concepts_run.ocp_status) — отчёт ОВП банка + слагаемые вне
// отчёта, по книгам без отчёта — из позиций. lib/real.js кладёт её в M.ocp; шапка (K.fxRows / K.fxMini), плитка C06
// «ОВП и курсы» и C16 берут её, когда она есть: суммы по валютам — из строк по книгам под фильтром книг (часть отчёта
// по юрлицу — только целиком), с меткой источника; разбор по щелчку — позиции. Без неё (прогон до v2, демо) — сумма
// FX-дельт позиций, как раньше.
//   node tests/js/ocp_published.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-6 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

// заглушка DOM: querySelector возвращает (и запоминает) дочерний элемент, так страница C16 пишет разметку в панели
function el() {
  const q = {};
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true, q,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector(sel) { return q[sel] || (q[sel] = el()); }, querySelectorAll() { return []; }, addEventListener() {},
    setAttribute() {}, getAttribute() { return null; }, remove() {}, insertAdjacentHTML() {} };
}
const dump = e => [e.innerHTML, ...e.children.map(dump), ...Object.values(e.q || {}).map(dump)].join('\n');
function load(mutate, filter, store) {
  const view = el();
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: id => id === 'view' ? view : el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem(k) { return k === 'concepts-filter' && filter ? JSON.stringify(filter) : store && k in store ? JSON.stringify(store[k]) : null; }, setItem(k, v) { if (store) store[k] = JSON.parse(v); } },
    getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} }, fetch: () => new Promise(() => {}) };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (mutate) { loadRun(ctx); mutate(ctx.window.CONCEPTS_RUN); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT, I: ctx.window.INSTR, ctx, view };
}

const HEAD = 'Головной банк';
const OCP = {
  asOf: '2026-10-21', state: 'computed', text: 'ОВП — отчёт ОВП и слагаемые вне отчёта из позиций', basis: 'report_plus_outside',
  byCcy: { GBP: { total: 2e6, rate: 100, totalRub: 2e8, bySource: { report: 2e6 } } },
  byBook: [
    { entity: HEAD, book: 'FXB', books: ['FXB'], ccy: 'USD', amount: 4e6, source: 'report', components: ['bonds'], reported: 3e6 },
    { entity: HEAD, book: null, books: ['CORP_FX', 'SOV_FX', 'RATES'], ccy: 'USD', amount: 100e6, source: 'report', components: [], reported: 100e6 },
    { entity: HEAD, book: null, books: ['CORP_FX', 'SOV_FX', 'RATES'], ccy: 'CNY', amount: 1.8e9, source: 'report', components: [], reported: 1.8e9 },
    { entity: HEAD, book: 'FXB', books: ['FXB'], ccy: 'CNY', amount: 217e6, source: 'derived', components: [], reported: null },
    { entity: HEAD, book: 'FXB', books: ['FXB'], ccy: 'EUR', amount: -3.7e6, source: 'derived', components: [], reported: null },
    { entity: 'Дочерний банк', book: 'SECUR', books: ['SECUR'], ccy: 'GBP', amount: 2e6, source: 'report', components: [], reported: 2e6 }],
  notes: [], gaps: [{ code: 'books_outside_report', text: 'книг с валютной позицией вне отчёта ОВП: 1 (FXB): их ОВП — из позиций', items: ['FXB'] }],
  sourceNames: { report: 'отчёт', derived: 'из позиций', derived_stale_report: 'из позиций: отчёт устарел' }, componentNames: {},
};
const withOcp = D => { D.OCP = JSON.parse(JSON.stringify(OCP)); };

t('без публикуемой ОВП (прогон до v2, демо): шапка пишет «ОВП нет», Σ FX-дельт позиций ОВП не подменяет (TDM#566)', () => {
  for (const mutate of [null, D => { delete D.OCP; }, D => { D.OCP = null; }]) {
    const { M, K, I } = load(mutate);
    assert.ok(!M.ocp && K.ocpScope() === null);
    assert.strictEqual(K.fxRows(M.positions).length, 0);
    const html = K.fxMini(M.positions);
    assert.ok(/ОВП нет/.test(html) && !html.includes('k-fxm') && !html.includes('k-ocpsrc') && K.ocpNote() === '');
    assert.ok(M.positions.some(p => I.fxIn(p, 'USD')), 'FX-дельты позиций остаются у позиций');
  }
});

t('с публикуемой ОВП: суммы по валютам — из строк по книгам, метка источника, разбор — позиции', () => {
  const { M, K, I } = load(withOcp);
  assert.ok(M.ocp && M.ocp.byBook.length === 6 && M.notes.some(x => x.startsWith('ОВП: книг с валютной позицией вне отчёта')));
  const R = Object.fromEntries(K.fxRows(K.list()).map(r => [r.c, r]));
  assert.deepStrictEqual(Object.keys(R).sort(), ['CNY', 'EUR', 'GBP', 'USD']);
  near(R.USD.v, 104e6, 'USD'); near(R.CNY.v, 2.017e9, 'CNY'); near(R.EUR.v, -3.7e6, 'EUR');
  assert.ok(R.USD.pub && K.ocpSrcMark(R.USD.src) === 'отчёт' && K.ocpSrcMark(R.CNY.src) === 'отчёт + поз.' && K.ocpSrcMark(R.EUR.src) === 'из позиций');
  near(R.GBP.eq, 2e6 * 100 / M.fx[K.repr], 'GBP без курса прогона — по курсу блока ОВП');
  near(R.USD.der, M.positions.reduce((s, p) => s + I.fxIn(p, 'USD'), 0), 'FX-дельта позиций остаётся разбором');
  const html = K.fxMini(K.list());
  assert.ok(html.includes('k-ocpsrc') && html.includes('отчёт + поз.') && html.includes(K.f.mns(R.CNY.v, 1)), html);
  assert.ok(K.tips.fxMiniPub.includes('отчёт ОВП') && K.ocpNote() === '');
});

t('фильтр книг: строка книги — по книге, часть отчёта по юрлицу — только целиком (иначе заметка)', () => {
  const only = load(withOcp, { books: ['FXB'] });
  const R1 = Object.fromEntries(only.K.fxRows(only.K.list()).map(r => [r.c, r.v]));
  assert.deepStrictEqual(R1, { CNY: 217e6, USD: 4e6, EUR: -3.7e6 });
  assert.strictEqual(only.K.ocpNote(), '', 'часть юрлица без книг под фильтром — не «частично»');
  const part = load(withOcp, { books: ['FXB', 'CORP_FX'] });
  const R2 = Object.fromEntries(part.K.fxRows(part.K.list()).map(r => [r.c, r.v]));
  assert.deepStrictEqual(R2, R1, 'часть отчёта по юрлицу, покрытая фильтром не вся, не входит');
  assert.ok(part.K.ocpScope().partial && /ОВП под фильтром/.test(part.K.ocpNote()) && /целиком/.test(part.K.ocpScope().notes[0]));
  const all = load(withOcp, { books: ['FXB', 'CORP_FX', 'SOV_FX', 'RATES'] });
  near(Object.fromEntries(all.K.fxRows(all.K.list()).map(r => [r.c, r.v])).USD, 104e6, 'фильтр на все книги части — входит');
  const ent = load(withOcp, { entity: HEAD });
  const R3 = Object.fromEntries(ent.K.fxRows(ent.K.list()).map(r => [r.c, r.v]));
  assert.ok(!('GBP' in R3) && R3.USD === 104e6, 'юрлицо: все его строки, чужие — нет');
  const desk = load(withOcp, { desk: 'FX' });
  assert.deepStrictEqual(Object.fromEntries(desk.K.fxRows(desk.K.list()).map(r => [r.c, r.v])), R1);
  assert.ok(!desk.K.ocpScope().partial, 'ни одной книги части под фильтром: она просто вне фильтра');
  const ccy = load(withOcp, { ccy: 'USD' });
  assert.deepStrictEqual(Array.from(ccy.K.fxRows(ccy.K.list()), r => r.c), ['USD']);
});

t('часть отчёта по юрлицу без книг и чужого юрлица под фильтром не входит (ревью #419)', () => {
  const two = D => { withOcp(D); D.OCP.byBook = [
    { entity: 'A', book: null, books: [], ccy: 'USD', amount: 5e6, source: 'report', components: [], reported: 5e6 },
    { entity: 'B', book: 'B1', books: ['B1'], ccy: 'GBP', amount: 2e6, source: 'report', components: [], reported: 2e6 },
    { entity: 'B', book: null, books: ['B2'], ccy: 'CHF', amount: 1e6, source: 'report', components: [], reported: 1e6 }]; };
  const sums = (filter) => { const L = load(two, filter); return Object.fromEntries(L.K.fxRows(L.K.list()).map(r => [r.c, r.v])); };
  assert.deepStrictEqual(sums(null), { USD: 5e6, GBP: 2e6, CHF: 1e6 }, 'без фильтра — всё');
  assert.ok(!('USD' in sums({ entity: 'B' })), 'фильтр юрлица B: строка юрлица A без книг не входит');
  assert.ok(!('USD' in sums({ books: ['B1'] })), 'фильтр книг: строка без книг не входит');
  assert.ok(!('USD' in sums({ desk: 'FX' })), 'фильтр деска: строка без книг не входит');
  assert.deepStrictEqual(Object.keys(sums({ entity: 'A' })), ['USD'], 'фильтр юрлица A: его остаток входит');
});

t('C06: плитка «ОВП и курсы» — публикуемая ОВП с меткой источника, бонды и деривативы — разбор FX-дельт', () => {
  const src = P('concepts/c06-launchpad.html');
  assert.ok(src.includes('K.ocpScope()') && src.includes('Σ FX-дельт') && src.includes('K.ocpSrcMark(r.src)'));
  assert.ok(src.includes("K.fxMini(L), K.ocpNote())"), 'HUD: заметка под ОВП');
});

// C16 (TDM#446): страница над ответом /api/v1/history/ocp; в демо — вымышленный ответ той же формы. srv — ответ сервера
// (K.source 'real': fetch отдаёт его, а на /inbox/datasets/ocp — why), hash — адрес демо (#no_report), filter — шапки,
// store — localStorage (ключ → значение; свой выбор книг C16 — 'tdm.view.v1:c16:books', TDM#533)
function page16(opt) {
  opt = opt || {};
  const page = P('concepts/c16-ocp.html');
  const code = page.slice(page.lastIndexOf('<script>') + 8, page.lastIndexOf('</script>'));
  const x = load(null, opt.filter, opt.store);
  const K = x.ctx.KIT;
  K.top = () => null; K.tableToolsAll = () => {};
  let chart = null; K.chart = (el, fn) => { chart = fn(); return { dispose() {} }; };
  const segs = {}; const seg0 = K.seg; K.seg = (el, items, cur, fn) => { segs[items[0][0]] = fn; return seg0(el, items, cur, fn); };
  if (opt.hash) x.ctx.location.hash = opt.hash;
  const asked = [];
  if (opt.srv) {
    K.source = 'real'; x.ctx.MOCK.run = { id: 42 }; x.ctx.MOCK.asOf = opt.srv.asOf;
    x.ctx.fetch = u => { asked.push(u); const body = u.includes('/history/ocp') ? opt.srv : opt.why || {}; return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) }); };
  }
  vm.runInContext(P('concepts/lib/inbox-why.js'), x.ctx);
  vm.runInContext(code, x.ctx);
  return Object.assign(x, { html: () => dump(x.view), chart: () => chart, segs, asked, code });
}
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(r => setTimeout(r, 0)); };
const L = (entity, book, books, ccy, amount, source) => ({ entity, book, books, ccy, amount, source: source || 'report', reported: null, validDate: null });
const FX = { USD: { market: 80, cbr: 79 }, EUR: { market: 96, cbr: 95 }, CNY: { market: 11, cbr: 10 } };
const SRV = {
  runId: 42, asOf: '2026-10-21', from: '2026-07-21', to: '2026-10-21', books: { OLD: { name: 'Старая книга', entity: 'BANK', desk: 'FX' } },
  days: [
    { date: '2026-09-15', runId: 1, state: 'not_computed', basis: null, fx: FX, lines: [] },
    { date: '2026-10-19', runId: 2, state: 'computed', basis: 'report_plus_outside', fx: FX, lines: [L('BANK', 'FXB', ['FXB'], 'USD', -1e6), L('BANK', 'OLD', ['OLD'], 'CNY', 88e6)] },
    { date: '2026-10-20', runId: 3, state: 'computed', basis: null, fx: FX, lines: [] },
    { date: '2026-10-21', runId: 42, state: 'computed', basis: 'report_plus_outside', fx: FX, lines: [
      L('BANK', 'FXB', ['FXB'], 'USD', 10e6), L('BANK', 'FXB', ['FXB'], 'CNY', -44e6, 'derived'),
      L('AM', null, ['AM-BOND', 'AM-CNY'], 'CNY', 22e6), L('IC', 'IC-BND', ['IC-BND'], 'EUR', -2e6, 'derived'),
      L('IC', 'IC-BND', ['IC-BND'], 'GBP', 1e6, 'derived')] }] };
const asyncTests = [];
const ta = (name, f) => asyncTests.push([name, f]);

ta('C16: ОВП дня по валютам, общая ОВП в USD — нетто, книги × валюты, часть отчёта своей строкой', async () => {
  const x = page16({ srv: SRV });
  await settle();
  assert.deepStrictEqual(Array.from(x.asked), ['../api/v1/history/ocp?run_id=42']);
  const html = x.html();
  // USD 10, CNY (−44+22)·11/80 = −3.025, EUR −2·96/80 = −2.4; GBP без курса (ни в прогоне, ни в отчёте) — не в общей.
  // Нетто: 10 − 3.025 − 2.4 = 4.575
  assert.ok(/data-ocp-total title="сумма USD-эквивалентов валют со знаком\. Без курса, не входят: GBP"><div class="l">Общая ОВП<\/div><div class="v"><span class="[^"]*">\+4\.6</.test(html), html);
  assert.ok(!html.includes('по курсу из отчёта'), 'курса отчёта нет — пометки нет');
  assert.ok(html.includes('data-ocp-ccy="CNY"') && html.includes('(22.0)</span> <span class="u">mm CNY') && html.includes('≈ (3.0) mm USD'));
  assert.ok(html.includes('data-ocp-ccy="GBP"') && html.includes('курса нет'));
  assert.ok(!html.includes('data-ocp-ccy="RUB"'));
  assert.ok(html.includes('data-vk="c16-books"') && html.includes('data-ocp-row="AM"') && html.includes('часть отчёта') && html.includes('title="книги: Фонд облигаций, Фонд юаневых облигаций"'), 'часть отчёта по юрлицу — строкой, книги в подсказке');
  assert.ok(html.includes('FX и XCCY</span> <span class="c16-src" title="из позиций: CNY">из позиций</span>'), 'источник строки, когда в таблице есть и отчёт, и из позиций; в CSV — через пробел');
  assert.ok(/data-ocp-row="IC-BND">[\s\S]*?<td class="n" title="нет курса одной из валют строки"><span class="mut3">—<\/span><\/td><\/tr>/.test(html), 'ОВП строки без курса GBP — не считается');
  assert.ok(!/Сверка|Перенос ОВП|брейк/.test(html), 'без сверки и переноса');
  assert.ok(!html.includes('Почему отчёта ОВП нет'));
  // история: дни позже месяца назад; день без публикуемой ОВП — пусто; линия — общая ОВП дня
  const o = x.chart();
  assert.deepStrictEqual(Array.from(o.xAxis.data), ['19 окт', '20 окт', '21 окт']);
  assert.deepStrictEqual(Array.from(o.series, s => s.name), ['USD', 'EUR', 'CNY', 'GBP', 'Общая']);
  const by = Object.fromEntries(o.series.map(s => [s.name, s.data]));
  near(by.CNY[0], 88 * 11 / 80, 'книга, которой нет в прогоне, — по справочнику периода'); assert.strictEqual(by.CNY[1], null);
  near(by['Общая'][0], 88 * 11 / 80 - 1, 'общая 19.10 — нетто'); assert.strictEqual(by['Общая'][1], null);
  near(by['Общая'][2], 4.575, '21.10: у GBP нет курса — линия по валютам с курсом, как итог (TDM#534)');
  const tipAt = i => o.tooltip.formatter(o.series.map(z => ({ dataIndex: i, seriesName: z.name, value: z.data[i], marker: '' })));
  assert.ok(tipAt(2).includes('Общая — без GBP: курса нет') && tipAt(2).includes('GBP: <b>—</b>'), tipAt(2));
  assert.ok(!tipAt(0).includes('курса нет') && !tipAt(0).includes('GBP'), 'валюты, которой в этот день нет, в подсказке нет');
  assert.ok(tipAt(1).includes('20.10.26') && tipAt(1).includes('ОВП нет: прогон до публикуемой ОВП'), 'день без публикуемой ОВП — причина');
  assert.strictEqual(by.GBP[2], null, 'без курса — нет столбца');
  x.segs.w2('m3');
  assert.strictEqual(x.chart().xAxis.data.length, 4, '3 мес');
});

ta('C16: в USD и курс ЦБ; правило общей ОВП — одна функция', async () => {
  const x = page16({ srv: SRV });
  await settle();
  // плитка «Общая ОВП» — по валютам с курсом (без курса — названы); строка книги и линия по дням — total: null без курса
  assert.ok(x.code.includes('const overall = usd =>') && x.code.includes('const total = usd => usd.some(v => v == null) ? null : overall(usd);'));
  assert.strictEqual((x.code.match(/overall\(/g) || []).length, 3, 'total, итог и линия по дням');
  assert.ok((x.code.match(/total\(/g) || []).length >= 1);
  x.segs.ccy('usd');
  let html = x.html();
  assert.ok(html.includes('(3.0)</span> <span class="u">mm USD') && html.includes('(22.0) mm CNY'), 'в USD: подпись — в валюте');
  x.segs.market('cbr');
  html = x.html();
  near(x.chart().series.find(s => s.name === 'CNY').data[2], -22 * 10 / 79, 'курс ЦБ');
  assert.ok(html.includes('(2.8)</span> <span class="u">mm USD'), html);
});

ta('C16: день без курса — линия «Общая» без этой валюты, причина в подсказке (TDM#534); курс ЦБ не подменяется рыночным; пустой период — «нет данных»', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.days[1].fx = {};  // старый прогон без курсов: USD входит своей суммой, CNY — без эквивалента
  s.days[3].lines = s.days[3].lines.filter(x => x.ccy !== 'GBP');
  s.days[3].fx.CNY.cbr = null;  // у прогона нет курса ЦБ этой валюты (сервер не подменяет его рыночным)
  const x = page16({ srv: s });
  await settle();
  let by = Object.fromEntries(x.chart().series.map(z => [z.name, z.data]));
  near(by.USD[0], -1, 'USD 19.10'); assert.strictEqual(by.CNY[0], null); near(by['Общая'][0], -1, 'без курса CNY — по USD, CNY назван');
  const o = x.chart();
  assert.ok(o.tooltip.formatter(o.series.map(z => ({ dataIndex: 0, seriesName: z.name, value: z.data[0], marker: '' }))).includes('Общая — без CNY: курса нет'));
  near(by['Общая'][2], 10 - 22 * 11 / 80 - 2.4, 'общая 21.10, все курсы есть: нетто разнознаковых валют');
  x.segs.market('cbr');
  by = Object.fromEntries(x.chart().series.map(z => [z.name, z.data]));
  assert.strictEqual(by.CNY[2], null); near(by['Общая'][2], 10 - 2 * 95 / 79, 'курс ЦБ: без CNY');
  assert.ok(/data-ocp-ccy="CNY"[\s\S]*?курса нет/.test(x.html()), 'курс ЦБ CNY — «курса нет»');
  const e = JSON.parse(JSON.stringify(SRV)); e.days.forEach(d => { d.state = 'not_computed'; d.basis = null; d.lines = []; });
  const y = page16({ srv: e });
  await settle();
  assert.ok(y.html().includes('Нет данных за период') && y.chart() === null);
});

ta('C16: валюта без курса прогона — по курсу из отчёта ОВП, в общей ОВП, в ОВП книги и на линии; пометка «курса нет»', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.days[3].reportFx = { GBP: 100, USD: 81, CNY: 12 };  // курс прогона есть у USD и CNY — берётся он, не отчёта
  s.days[1].fx = { CNY: { market: 11, cbr: 10 } };  // нет курса USD в прогоне: USD — из отчёта, CNY — прогона
  s.days[1].reportFx = { USD: 82, CNY: 12 };
  const x = page16({ srv: s });
  await settle();
  let html = x.html();
  // GBP 1·100/80 = 1.25 (USD — курс прогона); общая 10 − 3.025 − 2.4 + 1.25 = 5.825
  assert.ok(/data-ocp-total title="сумма USD-эквивалентов валют со знаком\. Пересчитано по курсу из отчёта ОВП: GBP"><div class="l">Общая ОВП<\/div><div class="v"><span class="[^"]*">\+5\.8</.test(html), html);
  assert.ok(/data-ocp-ccy="GBP"[\s\S]*?≈ \+1\.3 mm USD · <span title="Пересчитано по курсу из отчёта ОВП">курса нет<\/span>/.test(html), html);
  assert.ok(html.includes('<th class="n" title="Курса нет. Пересчитано по курсу из отчёта ОВП">GBP'), 'столбец валюты');
  assert.ok(/data-ocp-row="IC-BND">[\s\S]*?<td class="n"><span class="[^"]*">\(1\.1\)<\/span><\/td><\/tr>/.test(html), 'ОВП книги: −2.4 + 1.25');
  let by = Object.fromEntries(x.chart().series.map(z => [z.name, z.data]));
  near(by.GBP[2], 1.25, 'столбец GBP'); near(by['Общая'][2], 10 - 3.025 - 2.4 + 1.25, 'линия');
  near(by.USD[0], -1, 'USD 19.10'); near(by.CNY[0], 88 * 11 / 82, 'CNY по курсу прогона, USD — отчёта');
  near(by['Общая'][0], 88 * 11 / 82 - 1, 'общая 19.10');
  // нет курса ЦБ CNY в прогоне — CNY по курсу отчёта, USD — по курсу ЦБ прогона; пометка у CNY
  s.days[3].fx.CNY.cbr = null;
  const y = page16({ srv: s });
  await settle();
  y.segs.market('cbr');
  by = Object.fromEntries(y.chart().series.map(z => [z.name, z.data]));
  near(by.CNY[2], -22 * 12 / 79, 'CNY по курсу отчёта через рубль'); near(by.GBP[2], 100 / 79, 'GBP');
  near(by['Общая'][2], 10 - 22 * 12 / 79 - 2 * 95 / 79 + 100 / 79, 'общая');
  html = y.html();
  assert.ok(/data-ocp-ccy="CNY"[\s\S]*?mm USD · <span title="Пересчитано по курсу из отчёта ОВП">курса нет/.test(html), html);
  assert.ok(html.includes('Пересчитано по курсу из отчёта ОВП: CNY, GBP'), html);
  // нет курса USD в прогоне вовсе — USD тоже по курсу отчёта
  const z = JSON.parse(JSON.stringify(SRV));
  z.days[1].fx = {}; z.days[1].reportFx = { USD: 82, CNY: 11.5 };
  const w = page16({ srv: z });
  await settle();
  by = Object.fromEntries(w.chart().series.map(q => [q.name, q.data]));
  near(by.CNY[0], 88 * 11.5 / 82, 'CNY и USD по отчёту'); near(by['Общая'][0], 88 * 11.5 / 82 - 1, 'общая 19.10');
  // день без курса USD в прогоне (последний): у CNY и EUR курс прогона — входят по USD отчёта, без пометки
  const u = JSON.parse(JSON.stringify(SRV));
  delete u.days[3].fx.USD; u.days[3].reportFx = { USD: 82 }; u.days[3].lines = u.days[3].lines.filter(q => q.ccy !== 'GBP');
  const v = page16({ srv: u });
  await settle();
  html = v.html();
  assert.ok(/data-ocp-total title="сумма USD-эквивалентов валют со знаком\. Курс USD — из отчёта ОВП"><div class="l">Общая ОВП<\/div><div class="v"><span class="[^"]*">\+4\.7</.test(html), html);
  assert.ok(!html.includes('курса нет') && !html.includes('Пересчитано'), 'у CNY и EUR курс прогона есть — пометки нет');
  by = Object.fromEntries(v.chart().series.map(q => [q.name, q.data]));
  near(by['Общая'][2], 10 - 22 * 11 / 82 - 2 * 96 / 82, 'линия');
  // и курса USD нет ни в прогоне, ни в отчёте — CNY и EUR без эквивалента, «курса нет», общая — без них
  u.days[3].reportFx = {};
  const q2 = page16({ srv: u });
  await settle();
  html = q2.html();
  assert.ok(html.includes('Без курса, не входят: EUR, CNY') && /data-ocp-ccy="CNY"[\s\S]*?курса нет/.test(html), html);
  assert.ok(!html.includes('Курс USD — из отчёта ОВП'), 'курса USD нет и в отчёте — пометки нет');
  near(Object.fromEntries(q2.chart().series.map(q => [q.name, q.data]))['Общая'][2], 10, 'линия — только USD, EUR и CNY названы');
});

ta('C16: период «месяц» от конца месяца — без переполнения (31.03 − месяц = 28.02)', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.asOf = '2027-03-31';
  s.days = ['2027-02-26', '2027-03-01', '2027-03-02', '2027-03-31'].map(date => Object.assign({}, SRV.days[3], { date }));
  const x = page16({ srv: s });
  await settle();
  assert.deepStrictEqual(Array.from(x.chart().xAxis.data), ['1 мар', '2 мар', '31 мар']);
});

ta('C16: свой выбор книг (TDM#533): фильтр шапки не действует и назван; выбор запоминается отдельно; часть отчёта — только целиком', async () => {
  // фильтр шапки (книги, портфели) — не к ОВП: все книги, пометка над итогом
  const store = {};
  const one = page16({ srv: SRV, filter: { books: ['FXB'], portsOff: ['HTM'] }, store });
  await settle();
  let html = one.html();
  assert.ok(html.includes('data-ocp-row="FXB"') && html.includes('data-ocp-row="AM"') && html.includes('data-ocp-row="IC-BND"'), 'все книги');
  assert.ok(/data-c16-note><b>Фильтр шапки здесь не действует<\/b> \(в шапке: FX и XCCY · без HTM\): ОВП считается по книгам целиком, портфели \(HTM\) не исключаются/.test(html), html);
  assert.ok(html.includes('data-c16="books"') && html.includes('все книги · 5') && html.includes('все юрлица'), html);
  assert.ok(!/data-ocp-total title="[^"]*фильтр портфелей/.test(html), 'заметка портфелей шапки — не в подсказке итога');
  assert.deepStrictEqual(store['tdm.view.v1:c16:books'], { entity: null, off: [] }, 'первый раз — юрлицо шапки, все книги; запомнено');
  assert.strictEqual(store['concepts-filter'], undefined, 'фильтр шапки не тронут');
  // свой выбор: сняты книги — их нет в итоге, таблице и графике
  const off = page16({ srv: SRV, filter: { books: ['FXB'] }, store: { 'tdm.view.v1:c16:books': { entity: null, off: ['IC-BND', 'OLD'] } } });
  await settle();
  html = off.html();
  assert.ok(html.includes('data-ocp-row="FXB"') && html.includes('data-ocp-row="AM"') && !html.includes('data-ocp-row="IC-BND"') && !html.includes('data-ocp-ccy="EUR"'), html);
  assert.ok(html.includes('книги: 3 из 5'));
  let by = Object.fromEntries(off.chart().series.map(z => [z.name, z.data]));
  assert.ok(!('EUR' in by) && by.CNY[0] === null, 'график: снятая книга OLD (19.10) и IC-BND не входят');
  // часть отчёта по юрлицу — только целиком
  const part = page16({ srv: SRV, store: { 'tdm.view.v1:c16:books': { entity: 'AM', off: ['AM-BOND'] } } });
  await settle();
  html = part.html();
  assert.ok(!html.includes('data-ocp-row="AM"') && html.includes('под выбором книг — не вся') && html.includes('учтена только целиком'), html);
  // ни одной книги
  const none = page16({ srv: SRV, store: { 'tdm.view.v1:c16:books': { entity: 'AM', off: ['AM-BOND', 'AM-CNY'] } } });
  await settle();
  assert.ok(none.html().includes('Не выбрано ни одной книги') && none.html().includes('книги не выбраны'));
  // юрлицо: первый раз — из шапки; запомненное — своё, шапка его не меняет
  const ent = page16({ srv: SRV, filter: { entity: 'AM' } });
  await settle();
  assert.ok(ent.html().includes('data-ocp-row="AM"') && !ent.html().includes('data-ocp-row="FXB"'));
  const own = page16({ srv: SRV, filter: { entity: 'AM' }, store: { 'tdm.view.v1:c16:books': { entity: 'IC', off: [] } } });
  await settle();
  assert.ok(own.html().includes('data-ocp-row="IC-BND"') && !own.html().includes('data-ocp-row="AM"'));
  // другие экраны: K.ocpScope без своего фильтра — по-прежнему фильтр шапки
  assert.deepStrictEqual(Array.from(one.K.ocpScope(SRV.days[3].lines, {}).lines, z => z.book), ['FXB', 'FXB']);
  assert.ok(one.K.ocpScope(SRV.days[3].lines, {}).notes.includes('фильтр портфелей к ОВП по книгам не применяется'));
});

ta('C16: часть отчёта по юрлицу без книг (books: []) — не теряется из-за снятых книг вне ответа и другого юрлица; под выбором — названа (ревью #538)', async () => {
  // SRV + остаток юрлица BANK без книг (как rules/ocp.py при пустом books_at) и книга NEWBK без справочника
  const s = JSON.parse(JSON.stringify(SRV));
  s.days[3].lines.push(L('BANK', null, [], 'USD', 3e6), L('BANK', 'NEWBK', ['NEWBK'], 'USD', 1e6));
  const KEY = 'tdm.view.v1:c16:books';
  const usd = x => { const m = x.html().match(/data-ocp-ccy="USD"><div class="l">[\s\S]*?<div class="v"><span class="[^"]*">([^<]*)</); return m && m[1]; };
  // все книги: часть без книг — своей строкой
  const all = page16({ srv: s, store: { [KEY]: { entity: null, off: [] } } });
  await settle();
  assert.ok(all.html().includes('data-ocp-row="BANK"') && usd(all) === '+14.0', usd(all));
  // A: снятая книга, которой в ответе нет, — выбор не сужает: «все книги», часть без книг на месте, без «не вся»
  const gone = page16({ srv: s, store: { [KEY]: { entity: null, off: ['GONE'] } } });
  await settle();
  let html = gone.html();
  assert.ok(html.includes('data-ocp-row="BANK"') && usd(gone) === '+14.0' && html.includes('все книги · 6') && !html.includes('не вся'), html);
  near(Object.fromEntries(gone.chart().series.map(z => [z.name, z.data])).USD[2], 14, 'линия и столбец — с частью без книг');
  // B: снята книга другого юрлица — у выбранного юрлица часть без книг на месте
  const other = page16({ srv: s, store: { [KEY]: { entity: 'BANK', off: ['AM-BOND'] } } });
  await settle();
  html = other.html();
  assert.ok(html.includes('data-ocp-row="BANK"') && html.includes('все книги · 2') && !html.includes('не вся'), html);
  // выбор книг юрлица действительно сужен — часть без книг не входит и названа («под выбором книг — не вся»)
  const part = page16({ srv: s, store: { [KEY]: { entity: 'BANK', off: ['FXB'] } } });
  await settle();
  html = part.html();
  assert.ok(!html.includes('data-ocp-row="BANK"') && html.includes('под выбором книг — не вся') && html.includes('часть отчёта ОВП по юрлицу BANK без книг — только без выбора книг'), html);
  // «все юрлица», снята книга BANK: остаток AM без книг (у AM ничего не снято) остаётся, «не вся» нет (ревью #538, 2)
  const s2 = JSON.parse(JSON.stringify(SRV));
  s2.days[3].lines.push(L('AM', null, [], 'USD', 2e6));
  const am = page16({ srv: s2, store: { [KEY]: { entity: null, off: ['FXB'] } } });
  await settle();
  html = am.html();
  assert.ok(!html.includes('data-ocp-row="FXB"') && usd(am) === '+2.0' && !html.includes('не вся') && html.includes('книги: 4 из 5'), usd(am) + html);
  near(Object.fromEntries(am.chart().series.map(z => [z.name, z.data])).USD[2], 2, 'линия: остаток AM без книг');
  // а остаток BANK без книг при снятой книге BANK — не входит и назван
  const both = page16({ srv: s, store: { [KEY]: { entity: null, off: ['FXB'] } } });
  await settle();
  assert.ok(!both.html().includes('data-ocp-row="BANK"') && both.html().includes('под выбором книг — не вся'));
  // книга без справочника: под юрлицом не предлагается и не считается; без юрлица — и в выборе, и в итоге
  assert.ok(!other.html().includes('data-ocp-row="NEWBK"'));
  assert.ok(all.html().includes('data-ocp-row="NEWBK"'));
  // запомненного юрлица в ответе нет — все юрлица, запомненное не стирается
  const store = { [KEY]: { entity: 'NOPE', off: [] } };
  const nope = page16({ srv: s, store });
  await settle();
  html = nope.html();
  assert.ok(html.includes('все юрлица') && html.includes('data-ocp-row="FXB"') && html.includes('data-ocp-row="AM"') && !html.includes('Нет валютной позиции'), html);
  assert.deepStrictEqual(store[KEY], { entity: 'NOPE', off: [] });
  // валюта фильтра шапки — тоже названа; одиночные точки линии не прореживаются
  const ccy = page16({ srv: s, filter: { ccy: 'USD' } });
  await settle();
  assert.ok(ccy.html().includes('(в шапке: валюта USD)') && ccy.html().includes('data-ocp-ccy="CNY"'), 'фильтр валюты шапки к ОВП не применяется');
  assert.strictEqual(ccy.chart().series.find(z => z.name === 'Общая').showAllSymbol, true);
});

ta('C16: день без публикуемой ОВП; отчёта ОВП нет — панель «Почему» (TDM#445)', async () => {
  const none = JSON.parse(JSON.stringify(SRV)); none.days[3].state = 'not_computed'; none.days[3].basis = null;
  const a = page16({ srv: none });
  await settle();
  assert.ok(a.html().includes('ОВП за 21.10.26 нет: прогон без расчёта ОВП'), a.html());
  const nr = JSON.parse(JSON.stringify(SRV)); nr.days[3].state = 'no_report';
  const why = { dataset: 'ocp', name: 'ОВП', on: nr.asOf, files: { total: 0, by_status: {}, later: { total: 0, by_status: {} } }, latest: null, unread: {}, folders: { count: 1, scanned: 1, missing: 0 } };
  const b = page16({ srv: nr, why });
  await settle();
  const html = b.html();
  assert.ok(html.includes('отчёта ОВП нет') && html.includes('Почему отчёта ОВП нет') && html.includes('data-why="ocp"'), html);
  assert.deepStrictEqual(Array.from(b.asked), ['../api/v1/history/ocp?run_id=42', '../api/v1/inbox/datasets/ocp?on=2026-10-21']);
  assert.ok(html.includes('data-ocp-ccy="USD"'), 'ОВП дня — из позиций — на месте');
  b.segs.ccy('usd');
  assert.strictEqual(b.asked.length, 2, 'перерисовка не спрашивает причину снова');
});

ta('C16: демо — вымышленный ответ той же формы; #no_report — панель «Почему» с демо-причиной', async () => {
  const page = P('concepts/c16-ocp.html');
  assert.ok(page.includes('<script src="lib/inbox-why.js"></script>') && page.includes("'../api/v1/inbox/datasets/ocp'") && page.includes("'../api/v1/history/ocp' + (M.run.id != null ? '?run_id='"));
  const d = page16();
  let html = d.html();
  assert.ok(html.includes('Общая ОВП') && html.includes('data-ocp-row="AM"') && !html.includes('Почему отчёта ОВП нет'));
  assert.ok(d.chart().xAxis.data.length > 15, 'месяц рабочих дней');
  const n = page16({ hash: '#no_report' });
  html = n.html();
  assert.ok(html.includes('Почему отчёта ОВП нет') && html.includes('<div data-why="ocp">'), 'панель');
  assert.ok(html.includes('Отчёт ОВП отклонён: книги нет в справочнике') && html.includes('title="unknown_book ×2"'), html);
  assert.ok(html.includes('ocp_demo.csv</span> · ещё 3 файла не распознано') && html.includes('href="c14-day-status.html?on='), html);
  assert.strictEqual((html.match(/Почему отчёта ОВП нет/g) || []).length, 1);
});

ta('C16: книга ОВП без позиций (TDM#556) — юрлицо и тип из справочника ОВП; выбор типа книги; часть отчёта без книг — только без типа', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  // OLDX — старая книга: позиций в прогоне нет (M.book её не знает), справочник — из блока ОВП (books, ocpOnly)
  s.days[3].lines.push(L('BANK', 'OLDX', ['OLDX'], 'USD', 5e6), L('BANK', null, [], 'USD', 3e6));
  s.books.OLDX = { name: 'Старая ОВП', entity: 'IC', desk: 'Legacy', type: 'HTM', ocpOnly: true };
  const KEY = 'tdm.view.v1:c16:books';
  const ic = page16({ srv: s, store: { [KEY]: { entity: 'IC', off: [] } } });
  await settle();
  let html = ic.html();
  assert.ok(html.includes('data-ocp-row="OLDX"') && html.includes('data-ocp-row="IC-BND"'), 'книга без позиций — под своим юрлицом из настроек');
  assert.ok(html.includes('data-c16="type"') && html.includes('все типы книг'), html);
  const htm = page16({ srv: s, store: { [KEY]: { entity: null, type: 'HTM', off: [] } } });
  await settle();
  html = htm.html();
  assert.ok(html.includes('data-ocp-row="OLDX"') && !html.includes('data-ocp-row="FXB"') && !html.includes('data-ocp-row="IC-BND"'), html);
  assert.ok(html.includes('тип: HTM') && html.includes('все книги · 1'), html);
  assert.ok(!html.includes('data-ocp-row="BANK"') && !html.includes('data-ocp-row="AM"'), 'части отчёта без книг и с книгами другого типа не входят');
  const tr = page16({ srv: s, store: { [KEY]: { entity: null, type: 'TRADING', off: [] } } });
  await settle();
  html = tr.html();
  assert.ok(html.includes('data-ocp-row="IC-BND"') && !html.includes('data-ocp-row="OLDX"'), html);
  // запомненного типа нет у книг ответа — все типы (как с юрлицом)
  const gone = page16({ srv: s, store: { [KEY]: { entity: null, type: 'FUTURES', off: [] } } });
  await settle();
  assert.ok(gone.html().includes('все типы книг') && gone.html().includes('data-ocp-row="OLDX"'));
  // справочник прогона (M.ocp.books, concepts_run.ocp_status) — для итогов шапки: юрлицо книги без позиций
  const one = load(D => { withOcp(D); D.OCP.byBook.push({ entity: HEAD, book: 'OLDX', books: ['OLDX'], ccy: 'USD', amount: 7e6, source: 'report', components: [], reported: 7e6 });
    D.OCP.books = { OLDX: { name: 'Старая ОВП', entity: 'Дочерний банк', desk: 'Legacy', type: 'HTM', ocpOnly: true } }; });
  assert.strictEqual(one.K.ocpBook('OLDX').entity, 'Дочерний банк');
  const sub = one.K.ocpScope(null, null, { entity: 'Дочерний банк' });
  assert.deepStrictEqual(Array.from(sub.lines, z => z.book).sort(), ['OLDX', 'SECUR']);
  assert.deepStrictEqual(Array.from(one.K.ocpScope(null, null, { type: 'HTM' }).lines, z => z.book), ['OLDX']);
});

t('TDM#567 (D10): K.ocpBook — справочник того же дня (meta) раньше справочника прогона; без meta — прогона', () => {
  const { M, K } = load(withOcp);
  const own = M.books[0].id;
  assert.strictEqual(K.ocpBook(own).entity, M.book[own].entity);
  assert.strictEqual(K.ocpBook(own, { [own]: { entity: 'Тогдашнее юрлицо', type: 'HTM' } }).entity, 'Тогдашнее юрлицо');
  assert.strictEqual(K.ocpBook(own, { OTHER: {} }).entity, M.book[own].entity, 'книги нет в справочнике дня — прогона');
  // прошлый день фильтруется по тогдашнему юрлицу книги: строка книги входит под юрлицом дня, не прогона
  const day = [{ entity: HEAD, book: own, books: [own], ccy: 'USD', amount: 1e6, source: 'report' }];
  assert.strictEqual(K.ocpScope(day, { [own]: { entity: 'Тогдашнее юрлицо' } }, { entity: 'Тогдашнее юрлицо' }).lines.length, 1);
  assert.strictEqual(K.ocpScope(day, { [own]: { entity: 'Тогдашнее юрлицо' } }, { entity: M.book[own].entity }).lines.length, 0);
});

t('TDM#567 (D24): шапка называет ОВП не по текущей методике (M.ocp.methodNote); по текущей — без пометки', () => {
  const now = load(withOcp);
  assert.strictEqual(now.M.ocp.methodNote, null);
  assert.strictEqual(now.K.ocpMethodNote(), '');
  assert.ok(!now.K.fxMini(now.K.list()).includes('data-ocp-method'));
  const note = 'не по текущей методике: отчёт ещё с деривативами, их PV не добавлен (FXB)';
  const old = load(D => { withOcp(D); D.OCP.methodNote = note; D.OCP.byBook[0].baseKind = 'with_derivatives'; });
  assert.strictEqual(old.M.ocp.methodNote, note);
  assert.strictEqual(old.M.ocp.byBook[0].baseKind, 'with_derivatives');
  const html = old.K.fxMini(old.K.list());
  assert.ok(html.includes('data-ocp-method') && html.includes('не по текущей методике: отчёт ещё с деривативами'), html);
  // report_base_plus_day: подпись только если строка с деривативами в срезе (TDM#567, ревью)
  const bpd = f => load(D => { withOcp(D); D.OCP.basis = 'report_base_plus_day'; D.OCP.methodNote = note; D.OCP.byBook[0].baseKind = 'with_derivatives'; }, f);
  assert.ok(bpd(null).K.fxMini(bpd(null).K.list()).includes('data-ocp-method'));
  const cut = bpd({ books: ['SECUR'] });
  assert.strictEqual(cut.K.ocpMethodText(), '', 'книга с отчётом с деривативами не в срезе — пометки нет');
});

ta('TDM#567 (D11, D24): C16 — день другой методики на графике и в подсказке, строка с отчётом с деривативами, справочник дня', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.days[1].methodNote = 'другая методика: отчёт целиком (с деривативами) и слагаемые вне отчёта, день до 03.11.2026';
  const cur = s.days[3];
  cur.basis = 'report_base_plus_day';
  cur.methodNote = 'не по текущей методике: отчёт ещё с деривативами, их PV не добавлен (FXB)';
  cur.lines = cur.lines.map(x => Object.assign({}, x, { baseDate: '2026-10-20', baseOlder: false, baseKind: x.book === 'FXB' ? 'with_derivatives' : 'without_derivatives', parts: { base: x.amount } }));
  const x = page16({ srv: s });
  await settle();
  const html = x.html(), o = x.chart();
  assert.ok(html.includes('data-c16-method-day') && html.includes('data-c16-wd') && x.code.includes('data-c16-method-legend'), 'шапка дня, строка книги, пояснение графика (вставка в заглушке DOM не видна)');
  const k = o.xAxis.data.findIndex((_, j) => o.xAxis.axisLabel.formatter(o.xAxis.data[j], j).endsWith('*'));
  assert.ok(k >= 0 && k !== o.xAxis.data.length - 1, 'звёздочка на оси у дня другой методики (и у текущего дня тоже, он не по текущей): ' + k);
  const usd = o.series.find(z => z.name === 'USD').data;
  assert.ok(usd.some(v => v && typeof v === 'object' && v.itemStyle.opacity < 1), 'бледный столбец');
  const tip = o.tooltip.formatter(o.series.map(z => ({ dataIndex: k, seriesName: z.name, value: typeof z.data[k] === 'object' && z.data[k] ? z.data[k].value : z.data[k], marker: '' })));
  assert.ok(tip.includes('data-c16-method') && tip.includes('другая методика'), tip);
  const plain = page16({ srv: SRV });
  await settle();
  assert.ok(!plain.html().includes('data-c16-method') && !plain.html().includes('data-c16-wd') && !plain.chart().series.some(z => z.data.some(v => v && typeof v === 'object')), 'без пометок сервера пометок нет');
  // справочник дня: книга сменила юрлицо — день фильтруется по своему справочнику
  const h = JSON.parse(JSON.stringify(SRV));
  h.days[1].books = { OLD: { name: 'Старая книга', entity: 'IC', desk: 'FX' } };
  const KEY = 'tdm.view.v1:c16:books';
  const ic = page16({ srv: h, store: { [KEY]: { entity: 'IC', off: [] } } });
  await settle();
  const usdIc = ic.chart().series.find(z => z.name === 'CNY').data;
  assert.ok(usdIc.some(v => v != null), 'день 19.10 по справочнику дня: OLD под IC');
});

(async () => {
  for (const [name, f] of asyncTests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
