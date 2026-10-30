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
function load(mutate, filter) {
  const view = el();
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: id => id === 'view' ? view : el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: 'x' }, setTimeout,
    localStorage: { getItem(k) { return k === 'concepts-filter' && filter ? JSON.stringify(filter) : null; }, setItem() {} },
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

t('без публикуемой ОВП (прогон до v2, демо): шапка — сумма FX-дельт позиций, как раньше', () => {
  for (const mutate of [null, D => { delete D.OCP; }, D => { D.OCP = null; }]) {
    const { M, K, I } = load(mutate);
    assert.ok(!M.ocp && K.ocpScope() === null);
    const R = K.fxRows(M.positions);
    assert.ok(R.length && R.every(r => !r.pub));
    const usd = R.find(r => r.c === 'USD');
    near(usd.v, M.positions.reduce((s, p) => s + I.fxIn(p, 'USD'), 0), 'USD по ногам');
    assert.ok(!K.fxMini(M.positions).includes('k-ocpsrc') && K.ocpNote() === '');
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
// (K.source 'real': fetch отдаёт его, а на /inbox/datasets/ocp — why), hash — адрес демо (#no_report), filter — шапки
function page16(opt) {
  opt = opt || {};
  const page = P('concepts/c16-ocp.html');
  const code = page.slice(page.lastIndexOf('<script>') + 8, page.lastIndexOf('</script>'));
  const x = load(null, opt.filter);
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

ta('C16: ОВП дня по валютам, общая ОВП в USD — бо́льшая из длинных и коротких, книги × валюты, часть отчёта своей строкой', async () => {
  const x = page16({ srv: SRV });
  await settle();
  assert.deepStrictEqual(Array.from(x.asked), ['../api/v1/history/ocp?run_id=42']);
  const html = x.html();
  // USD 10, CNY (−44+22)·11/80 = −3.025, EUR −2·96/80 = −2.4; GBP без курса — не в общей. Длинные 10 > короткие 5.425
  assert.ok(/data-ocp-total[^>]*Без курса, не входят: GBP"><div class="l">Общая ОВП<\/div><div class="v"><span class="[^"]*">\+10\.0</.test(html), html);
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
  near(by['Общая'][0], 88 * 11 / 80, 'общая 19.10'); assert.strictEqual(by['Общая'][1], null);
  assert.strictEqual(by['Общая'][2], null, '21.10: у GBP нет курса — неполная общая ОВП не рисуется');
  assert.strictEqual(by.GBP[2], null, 'без курса — нет столбца');
  x.segs.w2('m3');
  assert.strictEqual(x.chart().xAxis.data.length, 4, '3 мес');
});

ta('C16: в USD и курс ЦБ; правило общей ОВП — одна функция', async () => {
  const x = page16({ srv: SRV });
  await settle();
  // плитка «Общая ОВП» — по валютам с курсом (без курса — названы); строка книги и линия по дням — total: null без курса
  assert.ok(x.code.includes('const overall = usd =>') && x.code.includes('const total = usd => usd.some(v => v == null) ? null : overall(usd);'));
  assert.strictEqual((x.code.match(/overall\(/g) || []).length, 2);
  assert.ok((x.code.match(/total\(/g) || []).length >= 2);
  x.segs.ccy('usd');
  let html = x.html();
  assert.ok(html.includes('(3.0)</span> <span class="u">mm USD') && html.includes('(22.0) mm CNY'), 'в USD: подпись — в валюте');
  x.segs.market('cbr');
  html = x.html();
  near(x.chart().series.find(s => s.name === 'CNY').data[2], -22 * 10 / 79, 'курс ЦБ');
  assert.ok(html.includes('(2.8)</span> <span class="u">mm USD'), html);
});

ta('C16: день без курса — линия «Общая» пустая; курс ЦБ не подменяется рыночным; пустой период — «нет данных»', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.days[1].fx = {};  // старый прогон без курсов: USD входит своей суммой, CNY — без эквивалента
  s.days[3].lines = s.days[3].lines.filter(x => x.ccy !== 'GBP');
  s.days[3].fx.CNY.cbr = null;  // у прогона нет курса ЦБ этой валюты (сервер не подменяет его рыночным)
  const x = page16({ srv: s });
  await settle();
  let by = Object.fromEntries(x.chart().series.map(z => [z.name, z.data]));
  near(by.USD[0], -1, 'USD 19.10'); assert.strictEqual(by.CNY[0], null); assert.strictEqual(by['Общая'][0], null, 'без курса CNY — не по одному USD');
  near(by['Общая'][2], 10, 'общая 21.10, все курсы есть');
  x.segs.market('cbr');
  by = Object.fromEntries(x.chart().series.map(z => [z.name, z.data]));
  assert.strictEqual(by.CNY[2], null); assert.strictEqual(by['Общая'][2], null);
  assert.ok(/data-ocp-ccy="CNY"[\s\S]*?курса нет/.test(x.html()), 'курс ЦБ CNY — «курса нет»');
  const e = JSON.parse(JSON.stringify(SRV)); e.days.forEach(d => { d.state = 'not_computed'; d.basis = null; d.lines = []; });
  const y = page16({ srv: e });
  await settle();
  assert.ok(y.html().includes('Нет данных за период') && y.chart() === null);
});

ta('C16: период «месяц» от конца месяца — без переполнения (31.03 − месяц = 28.02)', async () => {
  const s = JSON.parse(JSON.stringify(SRV));
  s.asOf = '2027-03-31';
  s.days = ['2027-02-26', '2027-03-01', '2027-03-02', '2027-03-31'].map(date => Object.assign({}, SRV.days[3], { date }));
  const x = page16({ srv: s });
  await settle();
  assert.deepStrictEqual(Array.from(x.chart().xAxis.data), ['1 мар', '2 мар', '31 мар']);
});

ta('C16: фильтр книг шапки — строки книг; часть отчёта по юрлицу — только целиком', async () => {
  const one = page16({ srv: SRV, filter: { books: ['FXB'] } });
  await settle();
  let html = one.html();
  assert.ok(html.includes('data-ocp-row="FXB"') && !html.includes('data-ocp-row="AM"') && !html.includes('data-ocp-ccy="EUR"'));
  assert.ok(!html.includes('под фильтром — не вся'));
  const part = page16({ srv: SRV, filter: { books: ['FXB', 'AM-CNY'] } });
  await settle();
  html = part.html();
  assert.ok(!html.includes('data-ocp-row="AM"') && html.includes('под фильтром — не вся') && html.includes('учтена только целиком'));
  // фильтр портфелей: ничего не исключено — без пометки «не вся», заметка — в подсказке общей ОВП
  const ports = page16({ srv: SRV, filter: { portsOff: ['X'] } });
  await settle();
  html = ports.html();
  assert.ok(!html.includes('под фильтром — не вся') && /data-ocp-total title="[^"]*фильтр портфелей к ОВП по книгам не применяется/.test(html), html);
  const ent = page16({ srv: SRV, filter: { entity: 'AM' } });
  await settle();
  assert.ok(ent.html().includes('data-ocp-row="AM"') && !ent.html().includes('data-ocp-row="FXB"'));
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

(async () => {
  for (const [name, f] of asyncTests) { await f(); n++; console.log('ok', name); }
  console.log(n + ' tests passed');
})().catch(e => { console.error(e); process.exit(1); });
