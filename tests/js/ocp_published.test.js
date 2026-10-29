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

t('C16: панель публикуемой ОВП (по валютам с источниками, по книгам, добавлено из позиций) и чего не хватает', () => {
  const page = P('concepts/c16-ocp.html');
  const code = page.slice(page.lastIndexOf('<script>') + 8, page.lastIndexOf('</script>'));
  const page16 = (src) => { const x = load(null); x.ctx.KIT.top = () => {}; x.ctx.KIT.tableToolsAll = () => {}; vm.runInContext(src, x.ctx); return dump(x.view); };
  const html = page16(code);  // демо-набор страницы (не сервер); шапка страницы (K.top) — вне заглушки DOM
  assert.ok(html.includes('Публикуемая ОВП') && html.includes('data-vk="c16-published"'), 'панель');
  assert.ok(html.includes('data-pub="B2|USD"') && html.includes('data-pub="B3|USD"') && html.includes('прочее <span class="k-num">'), 'по книгам, «прочее» добавлено из позиций');
  assert.ok(/c16-badge st-ok">отчёт</.test(html) && /c16-badge st-none">из позиций</.test(html), 'источник');
  assert.ok(html.includes('Чего не хватает публикуемой ОВП') && html.includes('data-gap="books_outside_report"'));
  assert.ok(html.includes('из настроек адаптера (ocp_policy): report_components'));
  // прогон до TDM#416: блок без published — панели нет, остальное как раньше
  const old = page.replace("published: { basis: 'report_plus_outside',", "published0: { basis: 'report_plus_outside',");
  const h2 = page16(old.slice(old.lastIndexOf('<script>') + 8, old.lastIndexOf('</script>')));
  assert.ok(!h2.includes('Публикуемая ОВП') && h2.includes('ОВП из позиций по валютам') && h2.includes('c16-recon'));
});

t('C16: «отчёта ОВП нет» — панель «Почему», по журналу входящих (TDM#445), только при no_report', () => {
  const page = P('concepts/c16-ocp.html');
  assert.ok(page.includes('<script src="lib/inbox-why.js"></script>') && page.includes("'../api/v1/inbox/datasets/ocp'"), 'запрос и библиотека');
  const page16 = (src) => {
    const x = load(null); x.ctx.KIT.top = () => {}; x.ctx.KIT.tableToolsAll = () => {};
    vm.runInContext(P('concepts/lib/inbox-why.js'), x.ctx); vm.runInContext(src, x.ctx); return dump(x.view);
  };
  const code = page.slice(page.lastIndexOf('<script>') + 8, page.lastIndexOf('</script>'));
  assert.ok(!page16(code).includes('Почему отчёта ОВП нет'), 'отчёт есть — панели нет');
  const none = code.replace("state: 'computed', text:", "state: 'no_report', text:");
  assert.notStrictEqual(none, code);
  const html = page16(none);  // демо: вымышленный ответ /api/v1/inbox/datasets/ocp той же формы
  assert.ok(html.includes('Почему отчёта ОВП нет') && html.includes('<div data-why="ocp">'), 'панель');
  assert.ok(html.includes('Отчёт ОВП отклонён: книги нет в справочнике') && html.includes('title="unknown_book ×2"'), html);
  assert.ok(html.includes('ocp_demo.csv</span> · ещё 3 файла не распознано') && html.includes('href="c14-day-status.html?on='), html);
  assert.strictEqual((html.match(/Почему отчёта ОВП нет/g) || []).length, 1);
});

console.log(n + ' tests passed');
