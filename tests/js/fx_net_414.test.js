// FX PnL — один источник «FX (нетто ОВП)» (TDM#414): общий помощник desk-util.js (fxNet / fxNetPartial),
// разложение сценария и Scenario Lab без отдельной строки или колонки FX фондирования.
//   node --test tests/js/fx_net_414.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path'), vm = require('vm');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

const ctx = { window: {}, URLSearchParams };
vm.createContext(ctx);
vm.runInContext(P('data.js'), ctx);
vm.runInContext(P('desk-util.js'), ctx);
const D = ctx.window.DESK, U = D.util;

test('fxNet: сумма частей; null — только без обеих; одна часть — она и пометка «частично»', () => {
  assert.strictEqual(U.fxNet(120, -100), 20);
  assert.strictEqual(U.fxNet(0, 0), 0);
  assert.strictEqual(U.fxNet(null, null), null);
  assert.strictEqual(U.fxNet(undefined, undefined), null);
  assert.strictEqual(U.fxNet(5, null), 5);
  assert.strictEqual(U.fxNet(null, -7), -7);
  assert.strictEqual(U.fxNetPartial(5, null), true);
  assert.strictEqual(U.fxNetPartial(null, -7), true);
  assert.strictEqual(U.fxNetPartial(5, -7), false);
  assert.strictEqual(U.fxNetPartial(null, null), false);
  assert.strictEqual(U.FX_NET_LABEL, 'FX (нетто ОВП)');
});

test('сценарий: fx позиции — нетто (FX актива + FX его фондирования)', () => {
  const sc = { id: 'fx-test', fx: { USD: 20, EUR: 20, CNY: 20 } };
  const r = U.runScenario(sc);
  r.byPosition.forEach(x => assert.ok(Math.abs(x.fx - (x.fxGross + x.fxFunding)) < 1e-6, x.id));
});

test('Scenario Lab: нет колонок и строк «Фонд. FX» / «Фонд. ДС FX», FX остатков книг — в FX', () => {
  const html = P('scenario-lab.html');
  assert.ok(!/Фонд\.( ДС)? FX|фонд\. FX|fundFx/.test(html), 'отдельный показ FX фондирования');
  assert.ok(/fx:U\.fxNet\(r\.fundingFx\)/.test(html), 'итог FX сценария включает FX остатков книг');
  assert.ok(/book\[b\]\.fx \+= v/.test(html), 'FX остатков книги — в FX книги');
  assert.ok(/r\.fx = f\.fx/.test(html), 'FX строки фондирования книги — в колонке FX explain');
});

test('страницы концептов: один фактор fx, ни одной подписи FX актива / FX фондирования', () => {
  const dir = path.join(__dirname, '..', '..', 'prototypes', 'concepts');
  const skip = /^(c19-pnl-history\.html|pnl-history.*\.js|pnl-period.*\.js)$/;  // C19 ведёт своя задача (#386)
  const files = fs.readdirSync(dir).filter(f => f.endsWith('.html')).map(f => path.join(dir, f))
    .concat(fs.readdirSync(path.join(dir, 'lib')).filter(f => f.endsWith('.js')).map(f => path.join(dir, 'lib', f)))
    .filter(f => !skip.test(path.basename(f)));
  files.forEach(f => {
    const s = fs.readFileSync(f, 'utf8');
    assert.ok(!/fxGross|fxFunding|FX актив|FX фондир|FX gross|FX funding/i.test(s), path.basename(f));
  });
  const kit = fs.readFileSync(path.join(dir, 'lib', 'kit.js'), 'utf8');
  assert.ok(/\['fx', 'FX'\]/.test(kit) && /K\.tips\.fx = 'FX \(нетто ОВП\)/.test(kit));
});
