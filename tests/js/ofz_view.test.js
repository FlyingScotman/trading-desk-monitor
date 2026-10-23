// Вид риска ОФЗ (#32, 0.2.6): прогон, где ОФЗ на КБД (risk["RUB OFZ"] в par-бакетах, extra.ofz_basis01, ofzSpr01 —
// спред к КБД), через lib/real.js в обоих видах: «RUONIA + базис» (по умолчанию) и «КБД» (localStorage
// concepts-ofz-view). Сумма DV01 одна и та же; базис — это риск КБД; рыночный базис (M.basisCurve) от вида не зависит.
//   node tests/js/ofz_view.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
let n = 0; const t = (name, f) => { f(); n++; console.log('ok', name); };
const near = (a, b, msg) => assert.ok(Math.abs(a - b) <= 1e-9 * Math.max(1, Math.abs(a), Math.abs(b)), msg + ': ' + a + ' vs ' + b);

const KBD = [0, 0, 0, 0, 120, 250, 900, 1800, 400, 0, 0].map(v => -v);  // par-бакеты КБД: 1M–6M нули
const SPR = -3000;  // спред к КБД
function load(view, scheme = 'kbd_par') {
  const ctx = { window: {}, console, location: { href: 'http://x/', search: '', hostname: 'x' },
    localStorage: { getItem: k => (k === 'concepts-ofz-view' ? view : null), setItem() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('data.js'), ctx);
  const D = ctx.window.DESK;
  D.source.ofzScheme = scheme;
  const p = D.positions[0];  // ОФЗ-ПД демо-прогона: переводим на схему КБД
  p.risk = { 'RUB OFZ': KBD.slice() };
  p.ofzSpr01 = SPR;
  p.extra = Object.assign({}, p.extra, { ofz_basis01: KBD.reduce((a, b) => a + b, 0), spread_buckets: null });
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

console.log(n + ' tests passed');
