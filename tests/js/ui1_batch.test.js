// Батч 1, пакет UI-1 (prototypes/concepts/, R29–R32): подсказки к DV01 RUONIA / Базис01 / CS01 (конвенция бампа и
// почему базис ОФЗ не равен риску RUONIA), сортировка групп таблиц как обычных полей, в карточке — риск по кривым и
// срокам у каждой позиции и спред (Z к RUONIA у фиксов и флоатеров, DM у флоатеров).
//   node tests/js/ui1_batch.test.js
// kit.js грузится в vm с заглушкой DOM: K.table пишет разметку в innerHTML, её и проверяем.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const NB = 11, at = (k, v) => Array.from({ length: NB }, (_, i) => i === k ? v : 0);

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
  ctx.window = ctx;  // как в браузере: window.INSTR — это и глобальный INSTR (карточка kit.js)
  vm.createContext(ctx);
  if (data) { vm.runInContext(P('data.js'), ctx); data(ctx.window.DESK); vm.runInContext(P('concepts/lib/real.js'), ctx); }
  else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, K: ctx.window.KIT };
}
// строки таблицы по порядку: группы (уровень и подпись) и позиции
const order = html => [...html.matchAll(/<tr class="(grp l(\d)|row[^"]*)"[^>]*data-(?:g|id)="([^"]+)"/g)].map(m => m[2] != null ? 'g' + m[2] + ':' + m[3].split('/').pop() : m[3]);
const groupsAt = (html, l) => order(html).filter(x => x.startsWith('g' + l + ':')).map(x => x.slice(3));

t('подсказки (0.2.6, #32; 0.2.7, #213): DV01 RUONIA — бамп котировок OIS; базис — к par КБД при неизменной RUONIA, свой риск ядра; спред к КБД и КБД — свои', () => {
  const { K } = load();
  assert.ok(/котировках OIS/.test(K.tips.ruonia) && /пересобирается/.test(K.tips.ruonia) && /до 0\.2\.6/i.test(K.tips.ruonia));
  assert.ok(/1M…20Y\+/.test(K.tips.ruonia) && /треугольными/.test(K.tips.ruonia) && /центральная разность/.test(K.tips.ruonia));
  assert.ok(/par-доходностям КБД/.test(K.tips.basis01) && /при неизменных котировках OIS RUONIA/.test(K.tips.basis01)
    && /два разных риска/.test(K.tips.basis01) && /ZX/.test(K.tips.basis01) && /ОФЗ-ПК/.test(K.tips.basis01));
  assert.ok(/≈ DV01 RUONIA/.test(K.tips.cs01) && /ACT\/365F/.test(K.tips.ofzSpr01) && /rub_ofz_kbd_v3/.test(K.tips.kbd));
  assert.strictEqual(K.curveTip('OFZSPR'), K.tips.ofzSpr01);
  assert.ok(K.tips.dv01.includes(K.tips.ruonia));
  // заголовки колонок и ячейки KPI — с подсказкой
  for (const k of ['dv01', 'cs01', 'basis01', 'ofzSpr01', 'zdisc']) assert.ok(K.posCols[k].tip && K.posCols[k].title.length > 80, k);
  assert.ok(K.kpi('X', '1', '', '', 'подсказка').includes('class="k-hint" title="подсказка"'));
  assert.ok(K.fieldsHtml([['DV01', '1', false, 'как считается'], ['MV', '2']]).includes('<div class="l"><span class="k-hint" title="как считается">DV01</span></div>'));
  assert.ok(K.fieldsHtml([['MV', '2']]).includes('<div class="l">MV</div>'));
  assert.strictEqual(K.curveTip('RUB OFZ'), K.tips.basis01);
  assert.ok(K.curveTip('RUB RUONIA').endsWith(K.tips.ruonia));
});

t('демо (0.2.6): базис фикс-ОФЗ par–par равен её DV01 RUONIA (в виде «RUONIA + базис»), спред к КБД — свой', () => {
  const { M } = load();
  const fixGov = M.positions.filter(p => p.group === 'GOV' && p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix' && p.basis01);
  assert.ok(fixGov.length >= 3);
  for (const p of fixGov) {
    assert.ok(Math.abs(p.basis01 / p.dv01ByCurve['RUB RUONIA'] - 1) < 1e-9, p.name);
    assert.ok(p.ofzSpr01 && Math.abs(p.ofzSpr01 - p.basis01) < 1e-9, 'спред к КБД: ' + p.name);
  }
});

t('таблица: без щелчка — группы в своём порядке, строки по сортировке страницы', () => {
  const { M, K } = load();
  const box = el();
  K.table(box, { rows: M.positions, groupBy: K.defaultGroups, sort: [['dv01', 1]], columns: K.cols(['name', 'dv01', 'mv']) });
  const ccys = groupsAt(box.children[1].innerHTML, 0);
  assert.strictEqual(ccys[0], 'RUB');
  assert.deepStrictEqual(ccys, ccys.slice().sort((a, b) => M.ccyOrder[a] - M.ccyOrder[b]), 'валюты — в порядке деска');
});

t('таблица: после щелчка по столбцу группы — обычные поля: по сумме группы на каждом уровне, текст — по первой строке', () => {
  const { M, K } = load();
  const box = el();
  const api = K.table(box, { rows: M.positions, groupBy: K.defaultGroups, columns: K.cols(['name', 'issuer', 'dv01', 'mv']) });
  const html = () => box.children[1].innerHTML;
  const dv = list => list.reduce((s, p) => s + p.dv01, 0);
  api.state.groupSort = true;  // как после щелчка по заголовку
  for (const d of [1, -1]) {
    api.setSort([['dv01', d]]);
    const ccys = groupsAt(html(), 0);
    const tot = c => dv(M.positions.filter(p => p.ccy === c));
    assert.deepStrictEqual(ccys, ccys.slice().sort((a, b) => d * (tot(a) - tot(b))), 'валюты по DV01 ' + d);
    // эмитенты внутри валюты
    const rub = order(html()), i0 = rub.indexOf('g0:RUB'), i1 = rub.findIndex((x, i) => i > i0 && x.startsWith('g0:'));
    const iss = rub.slice(i0 + 1, i1 < 0 ? undefined : i1).filter(x => x.startsWith('g1:')).map(x => x.slice(3));
    const tIss = k => dv(M.positions.filter(p => p.ccy === 'RUB' && (p.group === 'DER' ? 'Деривативы' : p.issuerName) === k));
    assert.ok(iss.length > 3);
    assert.deepStrictEqual(iss, iss.slice().sort((a, b) => d * (tIss(a) - tIss(b))), 'эмитенты по DV01 ' + d);
  }
  // текстовый столбец самой группы — по имени группы
  api.setSort([['issuerName', -1]]);
  const o = order(html()), i0 = o.indexOf('g0:RUB'), i1 = o.findIndex((x, i) => i > i0 && x.startsWith('g0:'));
  // (у группы «Деривативы» поле «Эмитент» строк — контрагенты: она встаёт по своей первой строке)
  const iss = o.slice(i0 + 1, i1).filter(x => x.startsWith('g1:') && x !== 'g1:Деривативы').map(x => x.slice(3));
  assert.deepStrictEqual(iss, iss.slice().sort((a, b) => b.localeCompare(a, 'ru')), 'эмитенты по имени, обратный порядок');
  // сброс сортировки — порядок уровня (Минфин первым)
  api.setSort([]);
  const o2 = order(html()), j = o2.indexOf('g0:RUB');
  assert.strictEqual(o2[j + 1], 'g1:Минфин');
});

t('таблица: уровень fixed (недели календаря) не пересортировывается', () => {
  const { K } = load();
  const rows = [{ id: 'a', d: '2026-10-05', v: 1 }, { id: 'b', d: '2026-10-12', v: 5 }, { id: 'c', d: '2026-10-19', v: 3 }];
  const box = el();
  const api = K.table(box, { rows, search: false, groupBy: [{ key: r => r.d, order: (a, b) => a.localeCompare(b), fixed: true }], columns: [{ key: 'id', label: 'id' }, { key: 'v', label: 'v', num: true, sum: true }] });
  api.state.groupSort = true; api.setSort([['v', -1]]);
  assert.deepStrictEqual(groupsAt(box.children[0].innerHTML, 0), ['2026-10-05', '2026-10-12', '2026-10-19']);
});

t('C03: группы сортируются тем же общим правилом (KIT.groupKeys), строки — KIT.rowCmp', () => {
  const c03 = P('concepts/c03-blotter.html');
  assert.ok(c03.includes('K.groupKeys(') && c03.includes('K.rowCmp(') && !/keys\.sort\(g\.order\)/.test(c03));
  assert.ok(c03.includes('K.curveRiskTable(p)') && c03.includes('K.spreadFields(p)') && c03.includes("'zdisc'"));
  // риск по кривым — и у облигаций, и у деривативов (раньше concat цеплялся только к полям дериватива)
  for (const src of [c03, P('concepts/lib/kit.js')]) assert.ok(src.includes(']).concat(K.curveRiskFields(p));'));
});

t('карточка: риск по кривым у каждой позиции с риском, таблица кривая × срок с базисом и CS01', () => {
  const { M, K } = load();
  const ofz = M.positions.find(p => p.group === 'GOV' && p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix' && p.basis01);
  const f = K.curveRiskFields(ofz);
  assert.strictEqual(f.length, 1, 'одна кривая — тоже показываем');
  assert.ok(/RUONIA/.test(f[0][1]) && f[0][3] === K.tips.dv01);
  const tb = K.curveRiskTable(ofz);
  assert.ok(/RUONIA/.test(tb) && /ОФЗ-базис/.test(tb) && /<th class="n">Итого<\/th>/.test(tb));
  const corp = M.positions.find(p => p.group === 'CORP' && p.ccy === 'RUB' && p.cs01);
  assert.ok(/CS01/.test(K.curveRiskTable(corp)));
  const flt = M.positions.find(p => p.group === 'CORP' && p.cpn && p.cpn.type === 'float' && p.cpn.index === 'КС');
  const ft = K.curveRiskTable(flt);
  assert.ok(/КС/.test(ft) && /RUONIA/.test(ft) && /Σ ставки/.test(ft), 'флоатер на КС: две кривые и их сумма');
  // бакеты без риска у позиции — не столбцами
  const nb = (tb.match(/<th class="n">/g) || []).length - 1;
  assert.ok(nb >= 1 && nb <= NB);
});

t('прогон: спред в карточке — Z к RUONIA у фикса; у флоатера DM ядра и Z к RUONIA из того же прогона', () => {
  const { M, K } = load(D => {
    const b = D.positions.find(p => p.instrType === 'BOND' && p.ccy === 'RUB' && p.couponType === 'fix');
    D.positions.push(Object.assign({}, b, { id: 'F1', name: 'Флоатер КС+1', couponType: 'float', index: 'KEY_RATE', margin: 0.01, spreadBp: 899.1, spreadCurve: 'RUB RUONIA',
      risk: { 'RUB KEY': at(2, 153), 'RUB RUONIA': at(2, -104) }, extra: Object.assign({}, b.extra, { dm_bp: 878.0 }) }));
    D.positions.push(Object.assign({}, b, { id: 'F2', name: 'Флоатер старого прогона', couponType: 'float', spreadBp: 120, extra: Object.assign({}, b.extra, { dm_bp: undefined }) }));
  });
  const F1 = M.pos.F1, F2 = M.pos.F2;
  assert.strictEqual(F1.dm, 878.0); assert.strictEqual(F1.zSpreadZ, 899.1); assert.strictEqual(F1.dmSource, 'core');
  const s1 = K.spreadFields(F1).map(x => x[0]);
  assert.strictEqual(s1.join(' | '), 'Дисконт-маржа | Z-спред к RUONIA');
  // без DM ядра единственный спред показан один раз и помечен
  const s2 = K.spreadFields(F2);
  assert.strictEqual(s2.length, 1); assert.ok(/DM ядра нет/.test(s2[0][1]));
  const fix = M.positions.find(p => p.cpn && p.cpn.type === 'fix' && p.ccy === 'RUB' && p.zSpreadZ != null);
  assert.strictEqual(K.spreadFields(fix)[0][0], 'Z-спред к RUONIA');
  // риск по кривым — у флоатера на КС обе кривые
  assert.ok(/КС/.test(K.curveRiskFields(F1)[0][1]) && /RUONIA/.test(K.curveRiskFields(F1)[0][1]));
});

console.log(n + ' tests passed');
