// День без кривой RUONIA (#31): рублёвая бумага на прокси-кривой RUB OFZ (extra.curve_proxy прогона) — p.curveProxy
// в lib/real.js (пометка в таблицах и карточке: K.curveProxyTag, K.curveProxyNote); у позиции без PnL — причина
// сервера (pnl.reasonText: смена кривой дисконтирования с кривыми вчера и сегодня) в p.pnl.reason. Демо-прогон
// tests/js/fixtures/concepts-run.json, синтетика.
//   node --test tests/js/proxy_curve_31.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const test = require('node:test');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const TEXT = 'на прокси-кривой RUB OFZ: кривой RUONIA за дату нет';
const WHY = 'смена кривой дисконтирования: вчера RUB RUONIA, сегодня RUB OFZ (сегодня кривой RUONIA не было — бумага на '
  + 'прокси-кривой RUB OFZ): спреды к разным кривым';

function load(mutate) {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  loadRun(ctx);
  if (mutate) mutate(ctx.window.CONCEPTS_RUN);
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  return ctx.window.MOCK;
}
const floater = D => D.positions.find(p => p.instrType === 'BOND' && p.couponType === 'float' && !p.notValued);

test('#31 real.js: extra.curve_proxy — пометка позиции, без поля — нет', () => {
  let id, other;
  const M = load(D => {
    const d = floater(D); id = d.id;
    other = D.positions.find(p => p.instrType === 'BOND' && p.id !== id).id;
    d.spreadCurve = 'RUB OFZ';
    d.extra = Object.assign({}, d.extra, { curve_proxy: { curve: 'RUB OFZ', instead_of: 'RUB RUONIA', reason: 'no_ruonia_curve', text: TEXT } });
  });
  assert.strictEqual(M.pos[id].curveProxy, TEXT);
  assert.strictEqual(M.pos[other].curveProxy, null, 'бумага без поля не помечена');
  // поле без текста (иная версия сервера): текст по кривой, не пусто
  const M2 = load(D => { const d = floater(D); d.extra = Object.assign({}, d.extra, { curve_proxy: { curve: 'RUB OFZ' } }); });
  assert.strictEqual(M2.pos[id].curveProxy, TEXT);
});

test('#31 real.js: позиция без PnL несёт причину сервера, а не только «—»', () => {
  let id;
  const M = load(D => {
    const d = floater(D); id = d.id;
    d.pnl = { available: false, reasonText: WHY, carry: null, rates: null, spread: null, other: null, explained: null };
  });
  const p = M.pos[id];
  assert.strictEqual(p.pnl.available, false);
  assert.strictEqual(p.pnl.total, null);
  assert.strictEqual(p.pnl.reason, WHY);
});
