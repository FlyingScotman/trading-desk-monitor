// Вид риска ОФЗ (#32, 0.2.6): прогон, где ОФЗ на КБД (risk["RUB OFZ"] в par-бакетах, extra.ofz_basis01, ofzSpr01 —
// спред к КБД), через lib/real.js в обоих видах: «RUONIA + базис» (по умолчанию) и «КБД» (localStorage
// concepts-ofz-view). Сумма DV01 одна и та же; базис — это риск КБД; рыночный базис (M.basisCurve) от вида не зависит.
//   node tests/js/ofz_view.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

const KBD = [0, 0, 0, 0, 120, 250, 900, 1800, 400, 0, 0].map(v => -v);  // par-бакеты КБД: 1M–6M нули
const SPR = -3000;  // спред к КБД
function load(view, scheme = 'kbd_par', more) {
  const ctx = { window: {}, console, location: { href: 'http://x/', search: '', hostname: 'x' },
    localStorage: { getItem: k => (k === 'concepts-ofz-view' ? view : null), setItem() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  loadRun(ctx);
  const D = ctx.window.CONCEPTS_RUN;
  D.source.ofzScheme = scheme;
  const p = D.positions[0];  // ОФЗ-ПД демо-прогона: переводим на схему КБД
  p.risk = { 'RUB OFZ': KBD.slice() };
  p.ofzSpr01 = SPR;
  p.extra = Object.assign({}, p.extra, { ofz_basis01: KBD.reduce((a, b) => a + b, 0), spread_buckets: null });
  if (more) more(D, ctx);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  return { M: ctx.window.MOCK, id: p.id };
}
const sum = a => a.reduce((x, y) => x + y, 0);

t('«RUONIA + базис»: риск КБД в RUONIA и в строке базиса, базис01 = DV01 на КБД, спред к КБД — отдельно', () => {
  const { M, id } = load(null);
  const p = M.pos[id];
  assert.strictEqual(M.ofzView, 'basis');
  assert.strictEqual(M.curve['RUB OFZ'].kind, 'basis');
  KBD.forEach((v, k) => { near(p.risk['RUB RUONIA'][k], v, 'RUONIA ' + k); near(p.risk['RUB OFZ'][k], v, 'базис ' + k); });
  near(p.basis01, sum(KBD), 'базис01');
  near(p.dv01, sum(KBD), 'DV01 без базиса');
  near(p.ofzSpr01, SPR, 'спред к КБД');
  near(sum(p.ofzSprb), SPR, 'спред к КБД по бакетам');
});

t('«КБД»: риск ОФЗ на кривой КБД, RUONIA без него, DV01 тот же, базиса нет', () => {
  const { M, id } = load('kbd');
  const p = M.pos[id];
  assert.strictEqual(M.ofzView, 'kbd');
  assert.strictEqual(M.curve['RUB OFZ'].kind, 'rate');
  KBD.forEach((v, k) => { near(p.risk['RUB RUONIA'][k], 0, 'RUONIA ' + k); near(p.risk['RUB OFZ'][k], v, 'КБД ' + k); });
  near(p.dv01, sum(KBD), 'DV01');
  assert.strictEqual(p.basis01, 0);
  near(p.ofzSpr01, SPR, 'спред к КБД');
  assert.strictEqual(M.basisCurve.kind, 'basis');  // рыночный базис — в любом виде
});

t('прогон до 0.2.6: базис — прежний ofzSpr01 (спред к RUONIA), спреда к КБД нет; вид «КБД» — с пометкой', () => {
  assert.ok(load('kbd', null).M.notes.some(x => /прогон до 0\.2\.6/.test(x)));
  const { M, id } = load(null, null);
  const q = M.positions.find(p => p.assetClass === 'ОФЗ-ПД' && p.id !== id);  // строка без ofz_basis01
  assert.ok(q.basis01 && q.ofzSpr01 === null, 'старый прогон: базис есть, спреда к КБД нет');
});

t('«КБД»: у ОФЗ-ПК риска КБД нет — её базис (спред к RUONIA) уходит в спред ОФЗ, RUONIA не трогается', () => {
  const basisView = load(null).M, kbdView = load('kbd').M;
  const pk = basisView.positions.find(p => p.group === 'GOV' && p.kind === 'PK' && p.basis01);
  assert.ok(pk, 'в демо-прогоне есть ОФЗ-ПК с базисом');
  const k = kbdView.pos[pk.id];
  k.risk['RUB OFZ'].forEach(v => assert.strictEqual(v, 0));
  pk.risk['RUB RUONIA'].forEach((v, i) => near(k.risk['RUB RUONIA'][i], v, 'RUONIA ' + i));
  near(k.ofzSpr01, (pk.ofzSpr01 || 0) + pk.basis01, 'спред ОФЗ-ПК');
  near(k.dv01, pk.dv01, 'DV01');
  assert.strictEqual(k.basis01, 0);
});

// #173: ОФЗ-ИН схемы КБД без оценки ядром — номинальный прокси по дюрации на КБД, не базис: в «RUONIA + базис» — только
// в RUONIA (p.ofzIn01 — сколько), в «КБД» — на кривой КБД; в истории прогонов basis01 без неё (basisIn01 — отдельно)
const LIN = [0, 0, 0, 0, 0, 0, 700, 0, 0, 0, 0].map(v => -v);
const linker = (D, ctx) => {
  const q = D.positions.filter(p => p.assetClass === 'ОФЗ-ПД')[1];  // вторая ОФЗ-ПД демо-прогона — линкер на прокси
  q.assetClass = 'ОФЗ-ИН'; q.risk = { 'RUB OFZ': LIN.slice() }; q.ofzSpr01 = -650;
  q.extra = Object.assign({}, q.extra, { ofz_basis01: sum(LIN), quality: 'market_value', spread_buckets: null });
  ctx.LIN_ID = q.id;
  ctx.window.DESK_EXTRAS = { histDates: ['2026-10-22', '2026-10-23'], hist: { dv01: { 'RUB RUONIA': [-9000, -9100] },
    basis01: [-3000, -3100], basisPk01: [-200, -200], basisIn01: [-700, -700], ofzSpr01: [0, 0] } };
};
t('#173 «RUONIA + базис»: ОФЗ-ИН (прокси) — в RUONIA, не в базисе; ofzIn01 — доля прокси', () => {
  const ctx = {}; const { M } = load(null, 'kbd_par', (D, c) => { linker(D, c); ctx.id = c.LIN_ID; });
  const p = M.pos[ctx.id];
  assert.strictEqual(p.group + '.' + p.kind, 'GOV.IN');
  assert.strictEqual(p.basis01, 0);
  near(p.ofzIn01, sum(LIN), 'ofzIn01');
  LIN.forEach((v, k) => { near(p.risk['RUB RUONIA'][k], v, 'RUONIA ' + k); near(p.risk['RUB OFZ'][k], 0, 'базис ' + k); });
  near(p.dv01, sum(LIN), 'DV01');
  near(p.ofzSpr01, -650, 'спред к КБД');
  assert.deepStrictEqual(Array.from(M.hist.dv01['RUB OFZ']), [-3000, -3100]);  // базис без прокси ОФЗ-ИН
});
t('#173 «КБД»: ОФЗ-ИН (прокси) — на кривой КБД, RUONIA без неё; в истории КБД = базис − ОФЗ-ПК + ОФЗ-ИН', () => {
  const ctx = {}; const { M } = load('kbd', 'kbd_par', (D, c) => { linker(D, c); ctx.id = c.LIN_ID; });
  const p = M.pos[ctx.id];
  assert.strictEqual(p.basis01, 0);
  LIN.forEach((v, k) => { near(p.risk['RUB RUONIA'][k], 0, 'RUONIA ' + k); near(p.risk['RUB OFZ'][k], v, 'КБД ' + k); });
  near(p.dv01, sum(LIN), 'DV01');
  assert.deepStrictEqual(Array.from(M.hist.dv01['RUB OFZ']), [-3000 + 200 - 700, -3100 + 200 - 700]);
  assert.deepStrictEqual(Array.from(M.hist.dv01['RUB RUONIA']), [-9000 + 3500, -9100 + 3600]);
});

// #213 (0.2.7): риск RUONIA и риск базиса ОФЗ посчитаны ядром порознь. Строка прогона — в координатах сценариев:
// risk["RUB OFZ"] — базис (par-риск КБД), risk["RUB RUONIA"] — RUONIA минус базис, extra.ofz_ruonia01 — риск RUONIA
const RUO = [-0.4, -0.1, -1.3, 2.2, 1.4, 5.0, 11.0, 25.0, 41.0, -400.5, -1.1];
const BAS = [0, 0, 0, 0.0, 2.1, 5.4, 12.4, 27.5, 46.2, -411.4, 1.1];
const computed = (D) => {
  const p = D.positions[0];
  p.risk = { 'RUB OFZ': BAS.slice(), 'RUB RUONIA': RUO.map((v, k) => v - BAS[k]) };
  p.extra = Object.assign({}, p.extra, { ofz_basis01: sum(BAS), ofz_ruonia01: sum(RUO) });
  D.positions = [p];
};
t('#213 «RUONIA + базис»: RUONIA ОФЗ — её риск RUONIA, базис — её риск базиса: два разных риска, DV01 = RUONIA', () => {
  const { M, id } = load(null, 'ruonia_basis', computed);
  const p = M.pos[id];
  RUO.forEach((v, k) => near(p.risk['RUB RUONIA'][k], v, 'RUONIA ' + k));
  BAS.forEach((v, k) => near(p.risk['RUB OFZ'][k], v, 'базис ' + k));
  near(p.dv01, sum(RUO), 'DV01 = риск RUONIA');
  near(p.basis01, sum(BAS), 'базис01');
  near(p.ofzRuo01, sum(RUO), 'ofzRuo01');
  assert.ok(Math.abs(p.basis01 - p.dv01) > 1e-6, 'риски разные');
  assert.ok(!M.notes.some(x => /прогон 0\.2\.6/.test(x)));
  assert.ok(load(null).M.notes.some(x => /прогон 0\.2\.6: риск ОФЗ к RUONIA/.test(x)));  // kbd_par: приближение
});
t('#213 «КБД»: у ОФЗ RUONIA нет совсем, на КБД — её базис (par-риск КБД); DV01 = риск КБД', () => {
  const { M, id } = load('kbd', 'ruonia_basis', computed);
  const p = M.pos[id];
  RUO.forEach((v, k) => near(p.risk['RUB RUONIA'][k], 0, 'RUONIA ' + k));
  BAS.forEach((v, k) => near(p.risk['RUB OFZ'][k], v, 'КБД ' + k));
  near(p.dv01, sum(BAS), 'DV01 = риск КБД');
  assert.strictEqual(p.basis01, 0);
});
t('#213 история «КБД»: из RUONIA вычитается риск RUONIA ОФЗ (ofzRuo01), а не её базис; без него — как раньше', () => {
  const hist = (ruo) => (D, ctx) => {
    computed(D);
    ctx.window.DESK_EXTRAS = { histDates: ['2026-10-22', '2026-10-23'], hist: { dv01: { 'RUB RUONIA': [-9000, -9100] },
      basis01: [-3000, -3100], basisPk01: [0, 0], basisIn01: [0, 0], ofzSpr01: [0, 0], ...(ruo ? { ofzRuo01: ruo } : {}) } };
  };
  let O = load('kbd', 'ruonia_basis', hist([-3010, null])).M.hist.dv01;
  assert.deepStrictEqual(Array.from(O['RUB OFZ']), [-3000, -3100]);
  assert.deepStrictEqual(Array.from(O['RUB RUONIA']), [-9000 + 3010, -9100 + 3100]);  // null: сводка без него — базис
  O = load('kbd', 'ruonia_basis', hist(null)).M.hist.dv01;
  assert.deepStrictEqual(Array.from(O['RUB RUONIA']), [-9000 + 3000, -9100 + 3100]);
});

t('#213 история позиции: «RUONIA + базис» — RUONIA и базис как в прогонах; «КБД» — RUONIA минус её риск RUONIA (ofzRuo)', () => {
  const xhr = (id) => function () {
    this.open = () => {};
    this.send = () => { this.status = 200; this.responseText = JSON.stringify({ dates: ['2026-10-22', '2026-10-23'], runAt: [null, null],
      series: { [id]: { price: [100, 101], notional: [1, 1], dv01: [-505, -506], basis: [-500, -501], ofzRuo: [-505, null],
        curves: { 'RUB RUONIA': [-505, -506] } } } }); };
  };
  const run = view => { let id; const { M } = load(view, 'ruonia_basis', (D, ctx) => { computed(D); id = D.positions[0].id; ctx.XMLHttpRequest = xhr(id); }); return M.posHistory(id); };
  const b = run(null);  // до #213 здесь падало: «p is not defined»
  assert.deepStrictEqual(Array.from(b.curves['RUB RUONIA']), [-505, -506]);
  assert.deepStrictEqual(Array.from(b.basis), [-500, -501]);
  const k = run('kbd');
  assert.deepStrictEqual(Array.from(k.curves['RUB RUONIA']), [0, -506 + 501]);  // null: прогон без ofzRuo — базис
  assert.deepStrictEqual(Array.from(k.curves['RUB OFZ']), [-500, -501]);
});

console.log(n + ' tests passed');
