// TDM#786: везде, где виден спред позиции, рядом оба Z-спреда — «Z к КБД» и «Z к RUONIA» (TDM#762):
//   - в исходниках экранов: в каждом списке колонок, где есть 'spread', следом идут 'zkbd', 'zdisc' (C01, C03 «Базовый»/«Цены и
//     спреды»/«Всё», C07, C12, C13); таблица оферт календаря (C08) — в браузере, spreads_786.pw.test.js;
//   - карточка и правая панель C03 (K.spreadFields): у рублёвой бумаги обе строки всегда, «—» без значения, подсказки колонок;
//     число в карточке то же, что в колонках «Z к КБД» и «Z к RUONIA» (в том числе у флоатера: в день на КБД Z — «к КБД»);
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
  const add = (id, name, over, extra) => D.positions.push(Object.assign({}, fix, { id, name }, over, extra ? { extra: Object.assign({}, fix.extra, extra) } : {}));
  add('OFZ1', 'ОФЗ-ПД тест', OFZ); add('NOOIS', 'Корп без OIS', NO_OIS);
  const FLT = { couponType: 'float', index: 'RUONIA', margin: 0.5, zKbdBp: null, zRuoniaBp: null };
  add('FL1', 'Флоатер RUONIA', Object.assign({ spreadBp: 205, spreadCurve: 'RUB RUONIA' }, FLT), { dm_bp: 190 });
  add('FL2', 'Флоатер на КБД', Object.assign({ spreadBp: 215, spreadCurve: 'RUB OFZ' }, FLT), { dm_bp: 190 });
  // валютные: рублёвых Z нет ни у флоатера, ни у фикса — даже если прогон прислал числа в zKbdBp / zRuoniaBp
  add('FXF', 'Валютный флоатер', Object.assign({ ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR' }, FLT), { dm_bp: 300 });
  add('FXF2', 'Валютный флоатер без DM ядра', Object.assign({ ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR' }, FLT), { dm_bp: undefined });
  add('FXN', 'Валютная фикс-бумага, числа в Z', { ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR', zKbdBp: 5, zRuoniaBp: 6 });
  add('FXO', 'Валютная фикс-бумага прогона до полей', { ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR', zKbdBp: undefined, zRuoniaBp: undefined });
  add('FL3', 'Флоатер без DM ядра', Object.assign({ spreadBp: 120, spreadCurve: 'RUB RUONIA' }, FLT), { dm_bp: undefined });
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

t('карточка = колонки: у каждой рублёвой бумаги число в строках «Z-спред к КБД» и «Z-спред к RUONIA» то же, что в «Z к КБД» и «Z к RUONIA» (флоатеры — тоже)', () => {
  const { M, K } = load(run);
  const txt = h => String(h).replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  const num = h => { const m = /^(−|-)?[\d,.\s]*\d/.exec(txt(h)); return m ? m[0].replace(/\s/g, '') : txt(h); };   // число или «—»
  for (const id of ['OFZ1', 'NOOIS', 'FL1', 'FL2', 'FL3']) {
    const p = M.pos[id], rows = Object.fromEntries(K.spreadFields(p).map(x => [x[0], x[1]]));
    for (const [label, col] of [['Z-спред к КБД', K.posCols.zkbd], ['Z-спред к RUONIA', K.posCols.zdisc]])
      assert.strictEqual(num(rows[label]), num(col.fmt(col.val(p), p)), id + ': ' + label);
  }
  // флоатер: один Z (spreadBp) — в колонке своей кривой дисконтирования, второй «—»; на КБД — «Z к КБД»
  const z = id => { const p = M.pos[id]; return [p.zKbd, p.zRuonia]; };
  assert.deepStrictEqual(z('FL1'), [null, 205]); assert.deepStrictEqual(z('FL2'), [215, null]); assert.deepStrictEqual(z('FL3'), [null, 120]);
  // валютная бумага (флоатер и фикс): колонки «Z к КБД» и «Z к RUONIA» пусты, карточка — «Z-спред к кривой USD» с её Z, а не рублёвые строки
  for (const id of ['FXF', 'FXF2', 'FXN', 'FXO']) {
    const p = M.pos[id], names = Array.from(K.spreadFields(p), x => x[0]);
    assert.deepStrictEqual([p.zKbd, p.zRuonia], [null, null], id);
    assert.deepStrictEqual([K.posCols.zkbd.val(p), K.posCols.zdisc.val(p)], [null, null], id);
    assert.ok(/—/.test(K.posCols.zkbd.fmt(null, p)) && /—/.test(K.posCols.zdisc.fmt(null, p)), id);
    assert.deepStrictEqual(names.filter(x => /^Z-спред/.test(x)), ['Z-спред к кривой USD'], id);
    assert.ok(!names.includes('Z-спред к КБД') && !names.includes('Z-спред к RUONIA'), id);
  }
  assert.deepStrictEqual(Array.from(K.spreadFields(M.pos.FXF), x => x[0]), ['Дисконт-маржа', 'Z-спред к кривой USD']);
  assert.ok(num(K.spreadFields(M.pos.FXF).find(x => x[0] === 'Z-спред к кривой USD')[1]).startsWith('310'));
  assert.strictEqual(txt(K.spreadFields(M.pos.FXF2)[0][1]), '— (DM ядра нет)');
  assert.ok(num(K.spreadFields(M.pos.FXF2)[1][1]).startsWith('310'), 'число Z один раз, в своей строке');
  // без DM ядра в строке DM числа нет — оно один раз, под своей строкой Z
  const fl3 = K.spreadFields(M.pos.FL3);
  assert.strictEqual(txt(fl3[0][1]), '— (DM ядра нет)');
  assert.strictEqual(fl3.slice(1).filter(x => /\d/.test(txt(x[1]))).length, 1);
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
