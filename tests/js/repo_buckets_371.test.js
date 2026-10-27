// РЕПО в группировках «по эмитенту» (ревью PR #371, #361): сделка — не бумага, её «эмитент» — корзина группы, а не имя
// контрагента. Деривативы — «Деривативы», РЕПО (группа MM) — «Денежный рынок и фондирование», как группа MM таблицы
// «валюта × тип» (INSTR.GROUP_LABEL.MM). Проверяются на демо-наборе (lib/mock.js) все ключи «эмитент» мониторов: K.defaultGroups
// («валюта → эмитент» на C01, C03, C13), C03, C04 (измерения «Эмитент» и «Тип купона»), C07 (уровни карты и список
// позиций), C12 («эмитент»), и запасные issuerName / sector у lib/real.js; команда терминала C12 «repo».
//   node --test tests/js/repo_buckets_371.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const test = require('node:test');
const { loadRun } = require('./fixtures/concepts_run');
const DIR = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
const P = f => fs.readFileSync(path.join(DIR, f), 'utf8');
const MM = 'Денежный рынок и фондирование';

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function context() {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; },
    querySelectorAll() { return []; }, getElementById: () => el(), createTreeWalker: () => ({ nextNode: () => null }) };
  const localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/concepts/c99.html', pathname: '/concepts/c99.html', search: '', hostname: 'x' },
    setTimeout, localStorage, getComputedStyle: () => ({ getPropertyValue: () => '#888888' }), MutationObserver: class { observe() {} },
    NodeFilter: { SHOW_TEXT: 4 }, CSS: { escape: s => String(s).replace(/["\\]/g, '\\$&') } };
  ctx.window = ctx;
  vm.createContext(ctx);
  return ctx;
}
function demo() {
  const ctx = context();
  vm.runInContext(P('lib/mock.js'), ctx);
  vm.runInContext(P('lib/kit.js'), ctx);
  vm.runInContext(P('lib/instr.js'), ctx);
  return ctx;
}

// ключи «эмитент» страниц: каждая стрелка, что возвращает issuerName позиции, с её проверкой «сделка или нет»
const PAGES = ['c03-blotter.html', 'c04-pivot.html', 'c07-portfolio-map.html', 'c12-terminal.html'];
// (и выражения `const kk = … p.issuerName;` списка позиций C07 — как стрелка от p)
const issuerKeys = src => [...src.matchAll(/\b([a-z]) => (M\.deal\(\1\) \? [^:]+ : \1\.issuerName|\1\.group === 'DER' \? [^:]+ : \1\.issuerName)/g)].map(m => m[0])
  .concat([...src.matchAll(/const kk = ((?:M\.deal\(p\)|p\.group === 'DER') \? [^:]+ : p\.issuerName);/g)].map(m => 'p => ' + m[1]));

test('#371: РЕПО на демо — в корзине своей группы, не под именем контрагента, на всех ключах «эмитент»', () => {
  const ctx = demo(), M = ctx.MOCK, K = ctx.KIT, I = ctx.INSTR;
  const repo = M.positions.find(p => p.group === 'MM'), irs = M.positions.find(p => p.group === 'DER' && p.cpty);
  assert.ok(repo && repo.cpty && repo.kind === 'REPO', 'в демо есть РЕПО с контрагентом');
  assert.strictEqual(M.dealName(repo), MM);
  assert.strictEqual(I.GROUP_LABEL[repo.group], MM, 'корзина — подпись группы MM таблицы «валюта × тип»');
  assert.notStrictEqual(repo.sector, repo.cpty);
  // «валюта → эмитент» (C01, C03, C13)
  const g = K.defaultGroups[1];
  assert.strictEqual(g.key(repo), MM);
  assert.strictEqual(g.key(irs), 'Деривативы');
  let n = 0;
  PAGES.forEach(f => {
    const keys = issuerKeys(P(f));
    assert.ok(keys.length, f + ': ключи «эмитент» найдены');
    keys.forEach(src => {
      assert.ok(!/group === 'DER' \?/.test(src), f + ': ключ «эмитент» проверяет M.deal, а не только DER: ' + src);
      const key = vm.runInContext('(' + src + ')', Object.assign(ctx, { M }));
      const v = key(repo);
      assert.notStrictEqual(v, repo.cpty, f + ': РЕПО не под именем контрагента: ' + src);
      assert.strictEqual(v, MM, f + ': ' + src);
      assert.ok(key(irs) === 'Деривативы' || key(irs) === irs.cpty, f + ': дериватив — как раньше: ' + src);
      n++;
    });
  });
  assert.ok(n >= 8, 'ключей «эмитент» на страницах: ' + n);
  // C04 «Тип купона»: РЕПО — своё значение, не «без купона»
  const cpn = /cpn: \{ label: 'Тип купона', key: (p => .*?), order: idxOrder\((\[[^\]]*\])\)/.exec(P('c04-pivot.html'));
  const ck = vm.runInContext('(' + cpn[1] + ')', ctx);
  assert.strictEqual(ck(repo), 'РЕПО');
  assert.strictEqual(ck(irs), 'дериватив');
  assert.ok(JSON.parse(cpn[2].replace(/'/g, '"')).includes('РЕПО'), 'РЕПО в порядке значений');
  // C12: команда repo / репо находит РЕПО, справка её называет
  const c12 = P('c12-terminal.html');
  assert.ok(/\['repo', 'репо'[^\]]*\]\.includes\(t\)\) L = L\.filter\(p => p\.group === 'MM'\)/.test(c12));
  assert.ok(/ofz\/corp\/der\/repo/.test(c12), 'справка терминала');
});

test('#371: lib/real.js — у РЕПО без контрагента «эмитент» и сектор — корзина группы MM, а не «—»', () => {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  const D = loadRun(ctx);
  const tpl = D.positions.find(p => p.instrType === 'BOND' && !p.notValued);
  const row = (id, pg, cpty) => Object.assign(JSON.parse(JSON.stringify(tpl)), { id, isin: null, issuer: null, sector: null, counterparty: cpty,
    instrType: 'OTC', segment: 'Деривативы', assetClass: 'OTC-деривативы', couponType: null, extra: { reported: { x: { x_product_group: pg } } } });
  D.positions.push(row('R1', 'repo', null), row('R2', 'repo', 'Контрагент Х'), row('S1', 'irs', null));
  vm.runInContext(P('lib/real.js'), ctx);
  const M = ctx.window.MOCK, pos = id => M.positions.find(p => p.id === id);
  assert.deepStrictEqual([pos('R1').group, pos('R1').kind, pos('R1').issuerName, pos('R1').sector], ['MM', 'REPO', MM, MM]);
  assert.strictEqual(pos('R2').cpty, 'Контрагент Х');
  assert.deepStrictEqual([pos('S1').group, pos('S1').issuerName, pos('S1').sector], ['DER', 'Деривативы', 'Деривативы']);
  assert.ok(M.deal(pos('R2')) && M.dealName(pos('R2')) === MM);
});
