// #106: история Scenario Lab не называет официальным PnL, которого нет. Сервер (compat) отдаёт в истории pnlOfficial =
// флэш + курсы ЦБ, если официальный не пришёл, и officialReceived: false; страница рисует такой день линией
// «Explain ЦБ», без unexplained и с подписями «не официальный». Демо (поля нет) — как раньше. Синтетика data.js.
//   node --test tests/js/official_label_106.test.js
'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

function fakeEl(name) {
  const kids = {};
  const el = { name, kids, innerHTML: '', textContent: '', value: '', checked: false, hidden: false, open: false, dataset: {}, style: { setProperty() {}, removeProperty() {} },
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} }, isConnected: true,
    clientWidth: 900, clientHeight: 400, offsetWidth: 900, offsetHeight: 400, scrollTop: 0, scrollLeft: 0,
    addEventListener(ev, fn) { (el.on = el.on || {})[ev] = fn; }, removeEventListener() {}, setAttribute() {}, getAttribute: () => null, removeAttribute() {}, hasAttribute: () => false,
    focus() {}, blur() {}, click() {}, select() {}, closest: () => null, remove() {}, contains: () => false, scrollIntoView() {},
    querySelector: s => kids[s] || (kids[s] = fakeEl(name + ' ' + s)), querySelectorAll: () => [],
    appendChild: c => c, insertBefore: c => c, replaceChildren() {}, append() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 900, bottom: 400, width: 900, height: 400 }) };
  Object.defineProperty(el, 'firstChild', { get: () => el.querySelector(':first-child') });
  return el;
}
function page(mutate) {
  const errors = [], con = { log() {}, info() {}, warn() {}, error: (...a) => errors.push(a.map(String).join(' ')) };
  const doc = fakeEl('document'), ls = { 'scenlab.mode': '"expl"' }, charts = new Map();
  Object.assign(doc, { documentElement: fakeEl('html'), body: fakeEl('body'), activeElement: null, title: '',
    getElementById: id => doc.querySelector('#' + id), createElement: tag => fakeEl(tag), write() {} });
  const init = el => {
    const c = { opt: null, setOption(o) { this.opt = o; }, on() {}, off() {}, resize() {}, dispose() {}, isDisposed: () => false, getWidth: () => 900, getHeight: () => 400,
      getDom: () => el, getZr: () => ({ on() {} }), containPixel: () => false, convertFromPixel: () => [0, 0], dispatchAction() {}, group: null };
    charts.set(el, c); return c;
  };
  const ctx = { window: {}, document: doc, console: con, URLSearchParams, Intl, Math, JSON, Date, setTimeout: () => 0, clearTimeout() {},
    requestAnimationFrame: () => 0, cancelAnimationFrame() {}, innerWidth: 1400, innerHeight: 900,
    location: { protocol: 'file:', search: '', hash: '', href: 'file:///x' }, CSS: { escape: s => s },
    localStorage: { getItem: k => ls[k] ?? null, setItem(k, v) { ls[k] = String(v); }, removeItem(k) { delete ls[k]; } },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    getComputedStyle: () => ({ getPropertyValue: () => '#808080' }), addEventListener() {}, removeEventListener() {},
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} }, MutationObserver: class { observe() {} disconnect() {} },
    echarts: { init, getInstanceByDom: el => charts.get(el) || null, connect() {}, graphic: {} } };
  vm.createContext(ctx);
  ctx.window = ctx; ctx.self = ctx;
  vm.runInContext(P('data.js'), ctx);
  if (mutate) mutate(ctx.DESK);
  vm.runInContext(P('desk-util.js'), ctx);
  vm.runInContext(P('desk-scope.js'), ctx);
  const html = P('scenario-lab.html'), start = html.lastIndexOf('<script>');
  vm.runInContext(html.slice(start + '<script>'.length, html.lastIndexOf('</script>')), ctx);
  assert.deepEqual(errors, []);
  const opt = id => charts.get(doc.querySelector('#' + id)).opt;
  return { $: s => doc.querySelector(s), D: ctx.DESK, opt };
}
// как сервер без официального PnL: за каждый день official = explained + курсы ЦБ, officialReceived: false
const noOfficial = D => {
  D.official = { asOf: D.asOf, received: false, basis: 'CBR' };
  D.positions.forEach(p => { if (p.pnl && p.pnl.available !== false) p.pnl.official = null; });
  D.history.forEach(h => { if (h.pnlExplained != null) h.pnlOfficial = h.pnlExplained + 1.5e6; h.officialReceived = false; });
};
const names = o => [...o.series].map(s => s.name);

test('#106: без официального PnL история не называет линию «Official», unexplained не считает', () => {
  const { $, opt, D } = page(noOfficial);
  const m = opt('h-main');
  assert.deepEqual(names(m), ['Explain ЦБ', 'Explained']);
  assert.deepEqual([...m.legend.data], ['Explain ЦБ', 'Explained']);
  assert.deepEqual([...m.series[0].data], [...D.history.map(h => h.pnlOfficial)]);
  assert.match($('#h-main-cap').textContent, /официального PnL нет/);
  assert.doesNotMatch($('#h-main-cap').textContent, /^Official/);
  assert.match($('#h-cum-cap').textContent, /не официальный/);
  assert.ok([...opt('h-un').series[0].data].every(d => d.value == null), 'unexplained по «Explain ЦБ» не считается');
});

test('#106: прошлый день без официального — «Официального нет», а не «Official PnL» числом', () => {
  const { $, D } = page(noOfficial);
  $('#ex-date').on.change({ target: { value: D.history[D.history.length - 2].date } });  // выбор даты в списке
  const recon = $('#recon').innerHTML;
  assert.match(recon, /Официального нет/);
  assert.match(recon, /Explain ЦБ/);  // подсказка у «—»
});

test('#106: смешанная история — Official только за дни, где он получен', () => {
  const { opt, D } = page(D => { noOfficial(D); D.history.slice(0, 5).forEach(h => { h.officialReceived = true; }); });
  const m = opt('h-main');
  assert.deepEqual(names(m), ['Official', 'Explain ЦБ', 'Explained']);
  const off = [...m.series[0].data], cbr = [...m.series[1].data];
  D.history.forEach((h, i) => {
    assert.equal(off[i], i < 5 ? h.pnlOfficial : null);
    assert.equal(cbr[i], i < 5 ? null : h.pnlOfficial);
  });
});

test('#106: демо (поля officialReceived нет) — как раньше: Official и Explained', () => {
  const { $, opt } = page();
  assert.deepEqual(names(opt('h-main')), ['Official', 'Explained']);
  assert.equal($('#h-main-cap').textContent, 'Official vs explained, млн ₽');
  assert.equal($('#h-cum-cap').textContent, 'Накопленный official PnL, млн ₽');
});

// #489: официальный PnL приходит по книгам целиком. Как сервер за день, где он пришёл не по всем книгам:
// officialState 'partial', officialBooks — сколько книг с ним, каких нет и итоги только по книгам с ним
const PART_DAY = 3;  // индекс дня истории (прошлый день)
const partialDay = D => {
  const h = D.history[PART_DAY];
  Object.assign(h, { officialReceived: false, officialState: 'partial',
    officialBooks: { received: 7, books: 8, missing: ['CORP_RUB'], pnlOfficial: 9e6, pnlExplained: 6.5e6 } });
};

test('#489: частичный день истории — «частично официальный», unexplained только по книгам с официальным', () => {
  const { $, opt, D } = page(partialDay);
  const h = D.history[PART_DAY], m = opt('h-main');
  assert.deepEqual(names(m), ['Official', 'Частично официальный', 'Explained']);
  const [off, part] = [m.series[0].data, m.series[1].data];
  assert.equal(off[PART_DAY], null, 'частичный день не на линии Official');
  assert.equal(part[PART_DAY], h.pnlOfficial);
  D.history.forEach((x, i) => { if (i !== PART_DAY) assert.equal(part[i], null); });
  assert.match($('#h-main-cap').textContent, /частично официальный/);
  // unexplained — официальный минус флэш только книг с официальным, не от смеси pnlOfficial − pnlExplained
  assert.equal(opt('h-un').series[0].data[PART_DAY].value, 9e6 - 6.5e6);
  $('#ex-date').on.change({ target: { value: h.date } });
  const recon = $('#recon').innerHTML;
  assert.match(recon, /Частично официальный/);
  assert.match(recon, /7 из 8 книг/);
  assert.match(recon, /Корпы RUB/);  // книга без официального названа — его можно запросить в финансах
  assert.match(recon, /запросить в финансах/);
  assert.match(recon, /Official PnL \(ЦБ\) · частично/);
  assert.match(opt('wf').xAxis.data.join('|'), /Unexplained/);
});

test('#489: сегодня официальный пришёл не по всем книгам — книга без него «—», unexplained без неё', () => {
  const NO = 'CORP_RUB';
  const { $, opt, D } = page(D => D.positions.forEach(p => {
    if (p.book === NO && p.pnl && p.pnl.available !== false) { p.pnl.official = null; p.pnl.unexplained = 0; }
  }));
  const recon = $('#recon').innerHTML;
  assert.match(recon, /Частично официальный/);
  assert.match(recon, /Корпы RUB/);
  const wf = opt('wf'), i = wf.xAxis.data.indexOf('Unexplained');
  assert.ok(i >= 0 && wf.xAxis.data.includes('= Частично офиц.') && !wf.xAxis.data.includes('= Official'));
  const [, s, e] = wf.series[0].data[i];
  const expect = D.positions.filter(p => p.book !== NO && p.pnl && p.pnl.available !== false).reduce((a, p) => a + (p.pnl.unexplained || 0), 0);
  assert.ok(Math.abs((e - s) - expect) < 1e-3, 'unexplained — только книги с официальным');
  const hier = $('#hier-wrap').innerHTML;
  assert.match(hier, /Официального нет/);
  assert.match(hier, /нет по: Корпы RUB/);  // итог деска и деска Credit: official только книг с ним
});

test('#489: день, где официальный есть у всех книг, — как раньше Official', () => {
  const { $, opt } = page(D => D.history.forEach(h => { h.officialReceived = true; h.officialState = 'all'; }));
  assert.deepEqual(names(opt('h-main')), ['Official', 'Explained']);
  assert.doesNotMatch($('#recon').innerHTML, /Частично официальный/);
});
