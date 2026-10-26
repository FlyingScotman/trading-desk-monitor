// R10 в Scenario Lab и DESK.util: суммы PnL пропускают позиции без PnL (null, не 0), «—» с причиной,
// если значения нет ни у одной позиции, «частично: N из M позиций» при неполном покрытии; официальный PnL до T+1 — «—».
// Рейтинг «нет данных» — корзина «без рейтинга», а не грейд. Проверки Widget Board и Pivot Studio удалены вместе
// с экранами (#292).
//   node tests/js/pnl_r10_pages.test.js
// Страницы исполняются на заглушке DOM (querySelector возвращает один и тот же элемент на селектор) и ECharts.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

const NO_PREV = 'нет финального среза рынка за предыдущий рабочий день';
const NO_CORE = 'бумага оценена по отчёту, без ядра';
const NULL_PNL = { carry: null, rolldown: null, rates: null, spread: null, fx: null, funding: null, convexity: null,
  newTrades: null, other: null, reported: null, fxGross: null, fxFunding: null, fxGrossCbr: null, fxFundingCbr: null,
  fxCbr: null, explained: null, explainedCbr: null, fxBasis: null, unexplained: null, official: null, breakNote: null };
const noPnl = (code, text) => Object.assign({}, NULL_PNL, { available: false, reason: code, reasonText: text });
const noRating = D => {   // хаб без рейтинга: «нет данных» у нескольких корпоративных бумаг, в конце RATING_ORDER
  D.positions.filter(p => p.cs01 && p.segment === 'Корп').slice(0, 3).forEach(p => { p.rating = 'нет данных'; });
  D.RATING_ORDER = D.RATING_ORDER.concat(['нет данных']);
};
const noPrev = D => {
  noRating(D);
  D.prevDate = null; D.pnlAvailable = false; D.pnlReason = 'no_prev_market'; D.pnlReasonText = NO_PREV;
  D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
  D.funding = []; D.fundingPnl = { carry: 0, fx: 0, fxCbr: 0 };
  Object.keys(D.fundingPnlByBook).forEach(b => { D.fundingPnlByBook[b] = { carry: 0, fx: 0, fxCbr: 0 }; });
  D.positions.forEach(p => { p.pnl = noPnl('no_prev_market', NO_PREV); });
  const h = D.history[D.history.length - 1]; h.pnlOfficial = null; h.pnlExplained = null;
};
const partial = D => {   // у каждой пятой позиции PnL нет; у одной — PnL «по отчёту»
  noRating(D);
  D.positions.forEach((p, i) => { if (i % 5 === 0) p.pnl = noPnl('valued_without_core', NO_CORE); });
  const r = D.positions[1]; r.pnl.reported = 2e6; r.pnl.explained += 2e6;
};
const awaitingOfficial = D => {   // частично и официальный ещё не пришёл (T+1)
  partial(D);
  D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
  D.positions.forEach(p => { if (p.pnl.available !== false) { p.pnl.official = null; p.pnl.unexplained = 0; } });
};

// ---------- заглушка DOM ----------
function fakeEl(name) {
  const kids = {};
  const el = { name, kids, innerHTML: '', textContent: '', value: '', checked: false, hidden: false, open: false, dataset: {}, style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} }, isConnected: true,
    clientWidth: 900, clientHeight: 400, offsetWidth: 900, offsetHeight: 400, scrollTop: 0, scrollLeft: 0,
    addEventListener() {}, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, removeAttribute() {}, hasAttribute: () => false,
    focus() {}, blur() {}, click() {}, select() {}, closest: () => null, remove() {}, contains: () => false, scrollIntoView() {},
    querySelector: s => kids[s] || (kids[s] = fakeEl(name + ' ' + s)), querySelectorAll: () => [],
    appendChild: c => c, insertBefore: c => c, replaceChildren() {}, append() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 400, width: 900, height: 400 }) };
  Object.defineProperty(el, 'firstChild', { get: () => el.querySelector(':first-child') });
  return el;
}
const chartStub = () => ({ setOption() {}, on() {}, off() {}, resize() {}, dispose() {}, isDisposed: () => false, getWidth: () => 900, getHeight: () => 400,
  getDom: () => fakeEl('chart'), getZr: () => ({ on() {} }), containPixel: () => false, convertFromPixel: () => [0, 0], dispatchAction() {}, group: null });
function page(file, mutate, store) {
  const errors = [], con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(String).join(' ')) };
  const doc = fakeEl('document'), ls = Object.assign({}, store || {});
  Object.assign(doc, { documentElement: fakeEl('html'), body: fakeEl('body'), activeElement: null, title: '',
    getElementById: id => doc.querySelector('#' + id), createElement: tag => fakeEl(tag), write() {} });
  const ctx = { window: {}, document: doc, console: con, URLSearchParams, Intl, Math, JSON, Date, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, innerWidth: 1400, innerHeight: 900,
    location: { protocol: 'file:', search: '', hash: '', href: 'file:///x' }, CSS: { escape: s => s },
    localStorage: { getItem: k => ls[k] ?? null, setItem(k, v) { ls[k] = String(v); }, removeItem(k) { delete ls[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '#808080' }), addEventListener() {}, removeEventListener() {},
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} }, MutationObserver: class { observe() {} disconnect() {} },
    echarts: { init: chartStub, getInstanceByDom: () => null, connect() {}, graphic: {} } };
  vm.createContext(ctx);
  ctx.window = ctx; ctx.self = ctx;
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  vm.runInContext(P('desk-scope.js'), ctx);
  const html = P(file), start = html.lastIndexOf('<script>');
  vm.runInContext(html.slice(start + '<script>'.length, html.lastIndexOf('</script>')), ctx);
  assert.deepStrictEqual(errors, [], file + ': ошибки в консоли');
  return { $: s => doc.querySelector(s), D: ctx.DESK, ctx };
}
const ZERO = /(^|[^\d,])[+−]?0,0+(?!\d)/;   // «0,0», «+0,00» — ноль на месте отсутствующего PnL

// ---------- desk-util ----------
t('U.pnlSum: позиции без PnL и значения null пропускаются; ни одного значения — null, не 0', () => {
  const { D } = page('scenario-lab.html', partial), U = D.util, ps = D.positions;
  const ok = ps.filter(U.pnlOk);
  assert.strictEqual(U.pnlSum(ps, p => p.pnl.explained), ok.reduce((s, p) => s + p.pnl.explained, 0));
  assert.strictEqual(U.pnlSum(ps.filter(p => !U.pnlOk(p)), p => p.pnl.explained), null);
  assert.strictEqual(U.pnlSum([], p => p.pnl.explained), null);
  assert.strictEqual(U.pnlSum(ok, () => null), null, 'значение null у позиций с PnL — тоже не 0');
  const pc = U.pnlCover(ps);
  assert.strictEqual(U.pnlPartText(pc), 'частично: ' + ok.length + ' из ' + ps.length + ' позиций');
  assert.ok(U.pnlPartHtml(pc).includes('class="part"') && U.pnlPartHtml(pc).includes(NO_CORE));
  assert.strictEqual(U.pnlPartText(U.pnlCover(ok)), '');
  assert.strictEqual(U.pnlNaHtml('a "b"'), '<span class="na" title="a &quot;b&quot;">—</span>');
  assert.strictEqual(U.pnlPartText({ partial: true, n: 1, m: 21 }), 'частично: 1 из 21 позиции');
});

t('рейтинг «нет данных» — «без рейтинга», не грейд', () => {
  const { D } = page('scenario-lab.html', partial), U = D.util;
  assert.strictEqual(U.ratingLabel('нет данных'), 'без рейтинга');
  assert.strictEqual(U.ratingLabel(null), 'без рейтинга');
  assert.strictEqual(U.ratingLabel('AA'), 'AA');
  assert.ok(!U.isRated('нет данных') && U.isRated('BB'));
});

// ---------- Scenario Lab ----------
t('Scenario Lab: «без рейтинга» — одна корзина после грейдов, шок применяется к бумагам «нет данных»', () => {
  const { $, D } = page('scenario-lab.html', partial);
  const box = $('#cr-box').innerHTML;
  assert.ok(!box.includes('>нет данных<'), 'нет слайдера «нет данных» как грейда');
  const rows = [...box.matchAll(/<div class="row[^"]*"><label[^>]*>([^<]+)</g)].map(m => m[1]);
  assert.strictEqual(JSON.stringify(rows.slice(0, D.RATING_ORDER.length)), JSON.stringify(D.RATING_ORDER.filter(r => r !== 'нет данных').concat(['без рейтинга'])));
  assert.ok(box.includes('class="row norat"') && box.includes('data-r="нет данных"'));
  const p = D.positions.find(x => x.rating === 'нет данных');
  assert.strictEqual(D.util.spreadShock({ spreads: { rating: { 'нет данных': 150 } } }, p), 150);
});

t('Scenario Lab, прогон без PnL: сверка, дерево и итоги — «—» и «PnL не посчитан», не 0', () => {
  const { $, ctx } = page('scenario-lab.html', noPrev, { 'scenlab.mode': '"expl"' });
  const rec = $('#recon').innerHTML, hier = $('#hier-wrap').innerHTML;
  assert.ok(rec.includes('class="na"') && rec.includes(NO_PREV) && rec.includes('PnL не посчитан'), rec);
  assert.ok(!ZERO.test(rec.replace(/<option[^]*?<\/option>/g, '').replace(/<[^>]+>/g, ' ')), rec);
  const foot = hier.slice(hier.indexOf('<tfoot>'));
  assert.ok(foot.includes('PnL не посчитан') && !ZERO.test(foot.replace(/<[^>]+>/g, ' ')), foot);
  assert.ok($('#rx-wrap').innerHTML.includes('PnL не посчитан'));
  assert.ok(ctx);
});

t('Scenario Lab, частично: итог деска по позициям с PnL, «частично», колонка «По отчёту»; до T+1 official — «—»', () => {
  const a = page('scenario-lab.html', partial, { 'scenlab.mode': '"expl"' }), D = a.D;
  const ok = D.positions.filter(D.util.pnlOk), hier = a.$('#hier-wrap').innerHTML;
  assert.ok(hier.includes('<th>По отчёту</th>'), 'PnL «по отчёту» — своей колонкой');
  assert.ok(hier.includes('частично: ' + ok.length + ' из ' + D.positions.length + ' позиций'));
  assert.ok(a.$('#recon').innerHTML.includes('частично:'));
  const b = page('scenario-lab.html', awaitingOfficial, { 'scenlab.mode': '"expl"' });
  assert.ok(b.$('#recon').innerHTML.includes('Ожидается T+1'));
  const foot = b.$('#hier-wrap').innerHTML.split('<tfoot>')[1];
  assert.ok(foot.includes('T+1') && foot.includes('class="na"'), foot);
});

console.log(n + ' tests passed');
