// TDM#762: Z-спред к КБД и Z-спред к RUONIA рублёвой фикс-бумаги. С 0.2.6 ОФЗ-ПД/АД/ИН дисконтируются по КБД, и их Z ядра
// (spreadBp) — спред к КБД; экраны подписывали его «к RUONIA». Теперь: колонка «Спред» подписывает свою кривую («к КБД» /
// «к RUONIA» / «DM»), есть колонки «Z к КБД» и «Z к RUONIA» (zKbdBp, zRuoniaBp прогона), карточка показывает оба спреда, а
// G-спреда к КБД в карточке нет (в таблицах он остался). Ни одна подсказка не называет спред к КБД спредом к RUONIA.
//   node tests/js/z_kbd_ruonia_762.test.js
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

// числа заведомо разные: подпись, перепутавшая кривые, показала бы чужое число
const OFZ = { spreadBp: 7, zKbdBp: 7, zRuoniaBp: 52, spreadCurve: 'RUB OFZ' };
const CORP = { spreadBp: 121, zKbdBp: 131, zRuoniaBp: 121, spreadCurve: 'RUB RUONIA' };
const NO_OIS = { spreadBp: 133, zKbdBp: 133, zRuoniaBp: null, spreadCurve: 'RUB OFZ' };  // день без RUONIA OIS: корпоративная на КБД
function run(D) {
  const fix = D.positions.find(p => p.instrType === 'BOND' && p.ccy === 'RUB' && p.couponType === 'fix');
  const add = (id, name, over, extra) => D.positions.push(Object.assign({}, fix, { id, name }, over, extra ? { extra: Object.assign({}, fix.extra, extra) } : {}));
  add('OFZ1', 'ОФЗ-ПД тест', OFZ); add('CORP1', 'Корп тест', CORP); add('NOOIS', 'Корп без OIS', NO_OIS);
  add('OLD', 'Прогон до поля', { spreadBp: 99, spreadCurve: 'RUB RUONIA', zKbdBp: undefined, zRuoniaBp: undefined });
  add('OLD2', 'ОФЗ прогона до поля', { spreadBp: 8, spreadCurve: 'RUB OFZ', zKbdBp: undefined, zRuoniaBp: undefined });
  add('FX1', 'Валютная', { ccy: 'USD', spreadBp: 310, spreadCurve: 'USD SOFR', zKbdBp: null, zRuoniaBp: null });
  add('FL1', 'Флоатер RUONIA', { couponType: 'float', index: 'RUONIA', margin: 0.5, spreadBp: 205, spreadCurve: 'RUB RUONIA', zKbdBp: null, zRuoniaBp: null }, { dm_bp: 190 });
  add('FL2', 'Флоатер на КБД', { couponType: 'float', index: 'RUONIA', margin: 0.5, spreadBp: 215, spreadCurve: 'RUB OFZ', zKbdBp: null, zRuoniaBp: null }, { dm_bp: 190 });
}

t('прогон: спреды позиции — свои поля, подпись кривой в «Спред», пустые поля — null (прогон до них, нет кривой, флоатер)', () => {
  const { M } = load(run);
  const o = M.pos.OFZ1, c = M.pos.CORP1, no = M.pos.NOOIS, old = M.pos.OLD, fx = M.pos.FX1, f1 = M.pos.FL1, f2 = M.pos.FL2;
  // TDM#801: «Спред» ОФЗ — Z к RUONIA (52), не к КБД (7); Z к КБД остаётся в своём поле и колонке, кривая дисконтирования (spreadCurve) — КБД
  assert.strictEqual(o.spreadLabel, 'к RUONIA'); assert.strictEqual(o.spreadName, 'Z к RUONIA'); assert.strictEqual(o.zspread, 52); assert.strictEqual(o.zKbd, 7); assert.strictEqual(o.zRuonia, 52);
  assert.strictEqual(o.spreadCurve, 'RUB OFZ'); assert.strictEqual(o.zSpreadZ, 7);
  assert.strictEqual(c.spreadLabel, 'к RUONIA'); assert.strictEqual(c.zspread, 121); assert.strictEqual(c.zKbd, 131); assert.strictEqual(c.zRuonia, 121);
  // день без RUONIA OIS: корпоративная на КБД — «Спред» к КБД, Z к RUONIA нет (к другой кривой не считается), а не её Z к КБД
  assert.strictEqual(no.spreadLabel, 'к КБД'); assert.strictEqual(no.zKbd, 133); assert.strictEqual(no.zRuonia, null);
  // прогон до этих полей: «—», а не Z к кривой дисконтирования, выданный за Z к RUONIA
  // прогон до полей (ключей нет): основной спред с известной кривой — это Z к ней, второй пуст; явный null нового прогона не трогаем
  assert.strictEqual(old.zKbd, null); assert.strictEqual(old.zRuonia, 99); assert.strictEqual(old.spreadLabel, 'к RUONIA');
  assert.strictEqual(M.pos.OLD2.zKbd, 8); assert.strictEqual(M.pos.OLD2.zRuonia, null); assert.strictEqual(M.pos.OLD2.spreadLabel, 'к КБД');
  assert.strictEqual(no.zRuonia, null, 'явный null нового прогона остаётся пустым');
  // валюта и кривая, у которой нет короткой подписи: просто Z; Z к КБД и к RUONIA у валютной бумаги нет
  assert.strictEqual(fx.spreadLabel, 'Z'); assert.strictEqual(fx.zKbd, null); assert.strictEqual(fx.zRuonia, null);
  // флоатер: DM ядра в «Спред», его Z (spreadBp) — к кривой дисконтирования: на RUONIA это Z к RUONIA, Z к КБД нет;
  // на КБД (нет RUONIA OIS) это Z к КБД (TDM#786), Z к RUONIA нет
  assert.strictEqual(f1.spreadLabel, 'DM'); assert.strictEqual(f1.zRuonia, 205); assert.strictEqual(f1.zKbd, null);
  assert.strictEqual(f2.spreadLabel, 'DM'); assert.strictEqual(f2.zRuonia, null); assert.strictEqual(f2.zKbd, 215);
});

t('таблицы: «Спред» подписан кривой, колонки «Z к КБД» и «Z к RUONIA» читают свои поля; CSV — число и кривая основной колонки', () => {
  const { M, K } = load(run);
  const sp = K.posCols.spread, kb = K.posCols.zkbd, rn = K.posCols.zdisc;
  assert.deepStrictEqual([sp.label, kb.label, rn.label], ['Спред', 'Z к КБД', 'Z к RUONIA']);
  const o = M.pos.OFZ1, c = M.pos.CORP1, no = M.pos.NOOIS;
  assert.ok(/^52 <span class="mut3">к RUONIA<\/span>$/.test(sp.fmt(o.zspread, o)) && !/КБД/.test(sp.fmt(o.zspread, o)));
  assert.ok(/<span class="mut3">к RUONIA<\/span>/.test(sp.fmt(c.zspread, c)) && !/КБД/.test(sp.fmt(c.zspread, c)));
  assert.deepStrictEqual(Array.from(sp.csvFmt(o.zspread, o)), ['52', 'к RUONIA']); assert.strictEqual(sp.csvExtra[0], 'Вид спреда');
  assert.deepStrictEqual(Array.from(sp.csvFmt(M.pos.FL1.zspread, M.pos.FL1)), ['190', 'DM']);
  assert.strictEqual(kb.val(o), 7); assert.strictEqual(rn.val(o), 52);
  assert.strictEqual(kb.val(c), 131); assert.strictEqual(rn.val(c), 121);
  assert.strictEqual(rn.val(no), null); assert.ok(/—/.test(rn.fmt(rn.val(no), no)));
  // колонки живут в таблице: заголовки и значения в разметке
  const box = el();
  K.table(box, { rows: [o, c, no], columns: K.cols(['name', 'spread', 'zkbd', 'zdisc']), search: false, viewKey: 'z762' });
  const html = box.children.map(x => x.innerHTML).join('');
  for (const label of ['Спред', 'Z к КБД', 'Z к RUONIA']) assert.ok(html.includes(label), label);
  assert.ok(html.includes('к КБД</span>') && html.includes('к RUONIA</span>'));
  assert.ok(html.includes('>52<') || html.includes('52</td>'), 'Z к RUONIA ОФЗ — своё число');
});

t('C03: колонка «Z к КБД» — в C03 и в наборах «Цены и спреды» и «Всё», рядом с «Z к RUONIA»; «Спред» и «Z к RUONIA» — те же поля', () => {
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(/zkbd: \{ label: 'Z к КБД', csvHead: 'Z к КБД, б\.п\.', num: true, nf: K\.NF\.zkbd, val: r => r\.zKbd,/.test(c03));
  assert.ok(/zdisc: \{ label: 'Z к RUONIA', csvHead: 'Z к RUONIA, б\.п\.', num: true, nf: K\.NF\.zdisc, val: r => r\.zRuonia,/.test(c03));
  const presets = [...c03.matchAll(/\['(px|all)', '[^']+', \[([^\]]*)\]\]/g)];
  assert.strictEqual(presets.length, 2);
  for (const [, key, list] of presets) assert.ok(/'spread', 'zkbd', 'zdisc', 'gspread'/.test(list), key);
  assert.ok(c03.includes('title: K.tips.zspread') && c03.includes('title: K.tips.zkbd') && c03.includes('title: K.tips.zdisc'));
  // всё, что показывает «Z к RUONIA» там, где его показывает kit.js, читает то же поле (раньше — spreadBp, Z к кривой дисконтирования)
  assert.ok(!/zdisc:[^\n]*r\.zSpreadZ/.test(c03) && !/zdisc:[^\n]*r\.zSpreadZ/.test(P('concepts/lib/kit.js')));
});

t('карточка: рублёвая фикс-бумага — «Z-спред к КБД» и «Z-спред к RUONIA» со своими числами; G-спреда к КБД в карточке нет, в таблицах он есть', () => {
  const { M, K } = load(run);
  const rows = p => Object.fromEntries(K.spreadFields(p).map(x => [x[0], x[1]]));
  const o = rows(M.pos.OFZ1), c = rows(M.pos.CORP1), no = rows(M.pos.NOOIS), old = rows(M.pos.OLD);
  assert.deepStrictEqual(Object.keys(o), ['Z-спред к КБД', 'Z-спред к RUONIA']);
  assert.ok(o['Z-спред к КБД'].startsWith('7 ') && o['Z-спред к RUONIA'].startsWith('52 '));
  assert.ok(c['Z-спред к КБД'].startsWith('131 ') && c['Z-спред к RUONIA'].startsWith('121 '));
  assert.ok(/—/.test(no['Z-спред к RUONIA']) && no['Z-спред к КБД'].startsWith('133 '), 'нет RUONIA OIS — прочерк, не Z к КБД');
  assert.ok(/—/.test(old['Z-спред к КБД']) && old['Z-спред к RUONIA'].startsWith('99 '), 'прогон до полей: спред к своей кривой, второй — прочерк');
  const old2 = rows(M.pos.OLD2);
  assert.ok(old2['Z-спред к КБД'].startsWith('8 ') && /—/.test(old2['Z-спред к RUONIA']));
  // у каждой из двух строк — своя подсказка, и ни одна не называет КБД «RUONIA»
  const tips = K.spreadFields(M.pos.OFZ1).map(x => x[3]);
  assert.strictEqual(tips[0], K.tips.zkbd); assert.strictEqual(tips[1], K.tips.zdisc);
  // валютная бумага — одна строка, к своей кривой; рублёвый флоатер — DM и оба Z (TDM#786), число — под кривой дисконтирования,
  // на КБД — «к КБД», не «к RUONIA», а второй — прочерк
  assert.strictEqual(Object.keys(rows(M.pos.FX1)).join('|'), 'Z-спред к кривой USD');
  const f1 = rows(M.pos.FL1), f2 = rows(M.pos.FL2);
  assert.strictEqual(Object.keys(f1).join('|'), 'Дисконт-маржа|Z-спред к КБД|Z-спред к RUONIA');
  assert.strictEqual(Object.keys(f2).join('|'), 'Дисконт-маржа|Z-спред к КБД|Z-спред к RUONIA');
  assert.ok(/—/.test(f1['Z-спред к КБД']) && f1['Z-спред к RUONIA'].startsWith('205 '));
  assert.ok(f2['Z-спред к КБД'].startsWith('215 ') && /—/.test(f2['Z-спред к RUONIA']));
  // строка G-спреда — из обеих карточек (kit.js и C03); колонка G-спред в таблицах осталась
  for (const src of [P('concepts/lib/kit.js'), P('concepts/c03-blotter.html')]) assert.ok(!/\['G-спред к ОФЗ'/.test(src) && !/G-спред к КБД/.test(src));
  assert.ok(/gspread: \{ (key: 'gspread', )?label: 'G-спред'/.test(P('concepts/lib/kit.js')) && /gspread: \{ label: 'G-спред'/.test(P('concepts/c03-blotter.html')));
});

t('подсказки и заголовки: нигде спред к КБД не назван спредом к RUONIA', () => {
  const { K } = load();
  const texts = { spread: K.posCols.spread.title, zkbd: K.posCols.zkbd.title, zdisc: K.posCols.zdisc.title };
  assert.strictEqual(texts.spread, K.tips.zspread); assert.strictEqual(texts.zkbd, K.tips.zkbd); assert.strictEqual(texts.zdisc, K.tips.zdisc);
  // «Спред» (TDM#801): у рублёвой фикс-бумаги, ОФЗ-ПД/АД/ИН тоже, — Z к RUONIA OIS; к КБД — только в день без RUONIA OIS; подпись ячейки названа
  for (const w of ['ОФЗ-ПД', 'ОФЗ-АД', 'ОФЗ-ИН', 'КБД', 'RUONIA OIS', '«к КБД»', '«к RUONIA»', 'DM']) assert.ok(texts.spread.includes(w), 'Спред: ' + w);
  assert.ok(!/ОФЗ-ИН это КБД/.test(texts.spread) && !/у ОФЗ-ПД, ОФЗ-АД и ОФЗ-ИН это КБД/.test(texts.spread));
  assert.ok(/оценка и риск ОФЗ от этого не меняются/.test(texts.spread) && /колонка «Спред» у них с подписью «к RUONIA»/i.test(texts.zkbd));
  // «Z к RUONIA» — к RUONIA OIS, у ОФЗ-ПД/АД/ИН тоже, не к кривой дисконтирования вообще; без кривой — прочерк
  assert.ok(texts.zdisc.includes('RUONIA OIS') && texts.zdisc.includes('ОФЗ-ПД') && /прочерк/.test(texts.zdisc));
  assert.ok(!/в день без неё/.test(texts.zdisc) && !/RUONIA OIS; в день/.test(texts.zdisc) && !/к кривой дисконтирования/.test(texts.zdisc));
  assert.ok(texts.zkbd.includes('КБД') && !/RUONIA OIS плюс Z/.test(texts.zkbd));
  for (const k of ['zkbd', 'zdisc']) assert.ok(/на запасной оценке \(без потоков\) есть только спред к её собственной кривой, второй пуст/.test(texts[k]), k);
  assert.ok(texts.spread.includes('не в рублях — Z к кривой её валюты (подпись «Z»)'));
  // прежние формулировки не вернулись ни в одном экране монитора (в том числе в тексте, не в K.tips)
  const old = [/Z-спред к RUONIA \(фикс\)/, /Z-спред к кривой дисконтирования \(RUONIA OIS; в день без неё/, /в день без неё — к КБД/];
  const walk = d => fs.readdirSync(d, { withFileTypes: true }).flatMap(e => e.isDirectory() ? (e.name === 'node_modules' ? [] : walk(path.join(d, e.name))) : /\.(js|html)$/.test(e.name) ? [path.join(d, e.name)] : []);
  for (const f of walk(ROOT)) { const s = fs.readFileSync(f, 'utf8'); for (const re of old) assert.ok(!re.test(s), path.relative(ROOT, f) + ' ' + re); }
});

t('демо: ОФЗ-ПД — «Спред» к RUONIA и равен Z к RUONIA (TDM#801), Z к КБД — другое число, кривая дисконтирования КБД; корпоративная — то же', () => {
  const { M, K } = load();
  const ofz = M.positions.filter(p => p.group === 'GOV' && p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix');
  assert.ok(ofz.length >= 3);
  for (const p of ofz) {
    assert.strictEqual(p.spreadCurve, 'RUB OFZ', p.name); assert.strictEqual(p.spreadLabel, 'к RUONIA', p.name);
    assert.strictEqual(p.zRuonia, p.zspread, p.name); assert.strictEqual(p.zKbd, p.zSpreadZ, p.name); assert.notStrictEqual(p.zRuonia, p.zKbd, p.name);
    assert.strictEqual(K.spreadFields(p).map(x => x[0]).join('|'), 'Z-спред к КБД|Z-спред к RUONIA');
  }
  const corp = M.positions.filter(p => p.group === 'CORP' && p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix');
  assert.ok(corp.length >= 3);
  for (const p of corp) { assert.strictEqual(p.spreadLabel, 'к RUONIA', p.name); assert.strictEqual(p.zRuonia, p.zspread, p.name); assert.strictEqual(p.spreadName, 'Z к RUONIA'); }
  for (const p of M.positions.filter(p => p.cpn && p.cpn.type === 'float' && p.ccy === 'RUB')) assert.ok(p.zKbd == null && p.zRuonia === p.zSpreadZ, p.name);
});

t('TDM#801: сдвиг спреда у ОФЗ — по Z к КБД, и это подписано (C05, PnL за период), C08 подписывает «Спред», история без истории — G-спред', () => {
  const c05 = P('concepts/c05-morning-brief.html'), pp = P('concepts/lib/pnl-period-position.js'), c08 = P('concepts/c08-calendar.html');
  assert.ok(/K\.hint\('Спред сдвинулся на 5\+ б\.п\.', 'сдвиг спреда к кривой дисконтирования \(у ОФЗ — к КБД\), по нему считается PnL «Спред»'\)/.test(c05));
  assert.ok(/p\.spreadCurve === 'RUB OFZ' \? ' <span class="mut3">к КБД<\/span>' : ''/.test(c05), 'пометка «к КБД» у ОФЗ в списке');
  assert.ok(/kpi\('Спред', c\('spread'\)[^\n]*у ОФЗ — к КБД/.test(pp) && /title="Δ сдвиг спреда к кривой дисконтирования \(у ОФЗ — к КБД\)[^"]*">Спред Δ, bp/.test(pp));
  assert.ok(/data-csv="Спред, б\.п\.\|Вид спреда" title="'\+esc\(K\.tips\.zspread\)\+'">Спред<\/th>/.test(c08) && /K\.csvv\(\(\) => \[f\.bp\(p\.zspread\), p\.spreadLabel \|\| ''\]\)/.test(c08) && c08.includes("esc(p.spreadLabel)"));
  const { M } = load(run);
  const h = M.posHistory('OFZ1');
  assert.strictEqual(h.spr[0], M.pos.OFZ1.gspread); assert.ok(!(M.pos.OFZ1.gspread !== M.pos.OFZ1.zspread) || h.spr[0] !== M.pos.OFZ1.zspread, 'не основной спред');
});

console.log(n + ' tests passed');
