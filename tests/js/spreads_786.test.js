// TDM#786: везде, где виден спред позиции, рядом оба Z-спреда — «Z к КБД» и «Z к RUONIA» (TDM#762):
//   - в исходниках экранов: в каждом списке колонок, где есть 'spread', следом идут 'zkbd', 'zdisc' (C01, C03 «Базовый»/«Цены и
//     спреды»/«Всё», C07, C12, C13), а в таблице оферт календаря (C08) — свои две колонки;
//   - карточка и правая панель C03 (K.spreadFields): у рублёвой бумаги обе строки всегда, «—» без значения, подсказки колонок;
//   - CSV таблицы: оба Z числом, пустое — пустой ячейкой;
//   - подсказки у заголовков экранируются.
//   node tests/js/spreads_786.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const P = f => fs.readFileSync(path.join(ROOT, f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function load(data) {
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '' }, setTimeout,
    localStorage: { getItem() { return null; }, setItem() {} }, getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (data) { data(loadRun(ctx)); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
const OFZ = { spreadBp: 7, zKbdBp: 7, zRuoniaBp: 52, spreadCurve: 'RUB OFZ' };
const NO_OIS = { spreadBp: 133, zKbdBp: 133, zRuoniaBp: null, spreadCurve: 'RUB OFZ' };
function run(D) {
  const fix = D.positions.find(p => p.instrType === 'BOND' && p.ccy === 'RUB' && p.couponType === 'fix');
  const add = (id, name, over) => D.positions.push(Object.assign({}, fix, { id, name }, over));
  add('OFZ1', 'ОФЗ-ПД тест', OFZ); add('NOOIS', 'Корп без OIS', NO_OIS);
  add('FX1', 'Валютная', { ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR', zKbdBp: null, zRuoniaBp: null });
}

const SCREENS = fs.readdirSync(path.join(ROOT, 'concepts')).filter(f => /^c\d+.*\.html$/.test(f));

t('исходники: каждый список колонок со «spread» — со «zkbd» и «zdisc» сразу за ним', () => {
  let found = 0;
  for (const f of SCREENS) {
    const src = P('concepts/' + f);
    // 'spread' — элемент списка ключей (за ним — ключ латиницей или конец списка), а не пара ['spread', 'Подпись'] и не сравнение
    for (const m of src.matchAll(/'spread'(?=,\s*'[A-Za-z][A-Za-z0-9]*'|\s*\])/g)) {
      if (!src.slice(src.lastIndexOf('[', m.index), m.index).includes("'name'")) continue;  // факторы PnL и т. п. — не колонки позиций
      found++;
      assert.ok(src.slice(m.index).startsWith("'spread', 'zkbd', 'zdisc'"), f + ': ' + src.slice(m.index, m.index + 60));
    }
  }
  // C01, C03 («Базовый», «Цены и спреды», «Всё»), C07, C12, C13
  assert.ok(found >= 7, 'нашлось списков: ' + found);
  for (const [f, cnt] of [['c01-cockpit.html', 1], ['c03-blotter.html', 3], ['c07-portfolio-map.html', 1], ['c12-terminal.html', 1], ['c13-issuers.html', 1]])
    assert.strictEqual((P('concepts/' + f).match(/'spread', 'zkbd', 'zdisc'/g) || []).length, cnt, f);
});

t('C03: все наборы со «spread» — «Базовый», «Цены и спреды», «Всё» — с обоими Z, «Z к КБД» перед «Z к RUONIA»', () => {
  const c03 = P('concepts/c03-blotter.html');
  const sets = [...c03.matchAll(/\['(\w+)', '[^']+', \[([^\]]*)\]\]/g)].filter(m => /'spread'/.test(m[2]));
  assert.deepStrictEqual(sets.map(m => m[1]), ['base', 'px', 'all']);
  for (const [, key, list] of sets) assert.ok(/'spread', 'zkbd', 'zdisc'/.test(list), key);
});

t('C08: таблица оферт — «Спред», «Z к КБД», «Z к RUONIA» рядом; значения читают zKbd и zRuonia; подсказки экранируются', () => {
  const s = P('concepts/c08-calendar.html');
  assert.ok(s.includes('<th class="n">Спред</th><th class="n" title="\'+esc(K.tips.zkbd)+\'">Z к КБД</th><th class="n" title="\'+esc(K.tips.zdisc)+\'">Z к RUONIA</th>'));
  assert.ok(s.includes("f.bp(p.zspread) + '</td><td class=\"n\">' + f.bp(p.zKbd) + '</td><td class=\"n\">' + f.bp(p.zRuonia) + '</td></tr>'"));
});

t('карточка и панель C03: рублёвая фикс-бумага и флоатер — обе строки («—» без значения), у валютной — Z к своей кривой', () => {
  const { M, K } = load(run);
  const names = p => K.spreadFields(p).map(x => x[0]).join('|');
  assert.strictEqual(names(M.pos.OFZ1), 'Z-спред к КБД|Z-спред к RUONIA');
  assert.strictEqual(names(M.pos.NOOIS), 'Z-спред к КБД|Z-спред к RUONIA');
  const rows = Object.fromEntries(K.spreadFields(M.pos.NOOIS).map(x => [x[0], x[1]]));
  assert.ok(/—/.test(rows['Z-спред к RUONIA']) && rows['Z-спред к КБД'].startsWith('133 '));
  assert.strictEqual(names(M.pos.FX1), 'Z-спред к кривой USD');
  // подсказки — те же, что у колонок
  for (const p of [M.pos.OFZ1, M.pos.NOOIS]) { const tips = K.spreadFields(p).map(x => x[3]); assert.strictEqual(tips[0], K.posCols.zkbd.title); assert.strictEqual(tips[1], K.posCols.zdisc.title); }
  // карточка и панель C03 берут строки из одной функции
  assert.ok(P('concepts/lib/kit.js').includes('...K.spreadFields(p)') && P('concepts/c03-blotter.html').includes('...K.spreadFields(p)'));
});

t('демо: у каждой рублёвой бумаги (фикс и флоатер) в карточке обе строки Z, у прочих — нет', () => {
  const { M, K } = load();
  let fl = 0, fix = 0;
  for (const p of M.positions.filter(p => p.cpn && (p.cpn.type === 'fix' || p.cpn.type === 'float') && p.ccy === 'RUB')) {
    const nm = K.spreadFields(p).map(x => x[0]);
    assert.ok(nm.includes('Z-спред к КБД') && nm.includes('Z-спред к RUONIA'), p.name + ': ' + nm);
    p.cpn.type === 'float' ? fl++ : fix++;
  }
  assert.ok(fl > 0 && fix > 0);
  for (const p of M.positions.filter(p => p.cpn && p.cpn.type === 'fix' && p.ccy !== 'RUB'))
    assert.ok(!K.spreadFields(p).some(x => /к КБД$|к RUONIA$/.test(x[0])), p.name);
});

t('CSV: «Спред», «Z к КБД», «Z к RUONIA» — числом, пусто — пустой ячейкой; заголовки с единицей', () => {
  const { M, K } = load(run);
  const box = el();
  const api = K.table(box, { rows: [M.pos.OFZ1, M.pos.NOOIS], columns: K.cols(['name', 'spread', 'zkbd', 'zdisc']), search: false, viewKey: 'z786' });
  const lines = api.csv().replace(/^﻿/, '').split(/\r?\n/).filter(Boolean);
  const cells = l => { const o = []; let c = '', q = false; for (const ch of l) { if (ch === '"') q = !q; else if (ch === ',' && !q) { o.push(c); c = ''; } else c += ch; } return o.concat(c); },
    hi = lines.findIndex(l => /Z к КБД/.test(l)), head = cells(lines[hi]), at = k => head.findIndex(h => h.includes(k));
  assert.ok(at('Z к КБД') > 0 && at('Z к RUONIA') > at('Z к КБД') && at('Спред') >= 0);
  assert.ok(/Z к КБД, б\.п\./.test(lines[hi]) && /Z к RUONIA, б\.п\./.test(lines[hi]));
  const row = nm => lines.slice(hi + 1).map(cells).find(c => c[0].includes(nm));
  const o = row('ОФЗ-ПД тест'), no = row('Корп без OIS');
  assert.strictEqual(o[at('Z к КБД')], '7'); assert.strictEqual(o[at('Z к RUONIA')], '52');
  assert.strictEqual(no[at('Z к КБД')], '133'); assert.strictEqual(no[at('Z к RUONIA')], '');
});

t('подсказки заголовков: в разметке таблицы подсказка Z-спредов экранирована (без сырых кавычек и скобок)', () => {
  const { M, K } = load(run);
  const box = el();
  K.table(box, { rows: [M.pos.OFZ1], columns: K.cols(['name', 'spread', 'zkbd', 'zdisc']), search: false, viewKey: 'z786h' });
  const html = box.children.map(x => x.innerHTML).join('') + box.innerHTML;
  for (const col of ['zkbd', 'zdisc']) {
    const tip = K.posCols[col].title;
    assert.ok(tip.length > 80);
    assert.ok(!html.includes(tip) || !/["<>]/.test(tip), col);   // сырой текст с кавычками в атрибут не попал
    assert.ok(html.includes(K.esc(tip)), col + ': подсказка в атрибуте экранирована');
  }
});

console.log(n + ' tests passed');
