// Одно правило вида инструмента (#345): lib/real.js kindOf ([группа, вид], зеркало publish/summary.kind_of) и
// lib/instr.js INSTR.kindOf (тип таблицы «валюта × тип», зеркало api/concepts.instr_kind) на общем списке случаев
// tests/fixtures/instr_kinds.json — том же, что сверяет backend/tests/test_instr_kinds.py с Python. Группа kindOf
// равна группе типа таблицы (кроме облигации без класса); OTC без группы продукта — DER (тип «OTC»), не
// «Не классифицировано»; РЕПО (группа продукта repo, оценённое или нет) — тип REPO в группе MM «Денежный рынок и
// фондирование» и у kindOf, и в таблице (#361); OTC с незнакомой группой (новая) — «Не классифицировано», известное
// расхождение (#367); XCCY старого tdm — XCCY у kindOf и CCS в таблице (тот же вид, имя таблицы).
//   node --test tests/js/instr_kinds.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const test = require('node:test');
const { loadRun } = require('./fixtures/concepts_run');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');
const DOC = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'fixtures', 'instr_kinds.json'), 'utf8'));

function load() {
  const ctx = { window: {}, URLSearchParams, console, location: { href: 'http://x/' } };
  vm.createContext(ctx);
  const D = loadRun(ctx);
  const tpl = D.positions.find(p => p.instrType === 'BOND' && !p.notValued);
  D.positions = DOC.cases.map((c, i) => {
    const p = Object.assign(JSON.parse(JSON.stringify(tpl)), { id: 'K' + i, isin: null, issuer: null }, c.row);
    if (!c.row.extra) delete p.extra;
    if (!c.row.notValued) delete p.notValued;
    return p;
  });
  vm.runInContext(P('concepts/lib/real.js'), ctx);
  vm.runInContext(P('concepts/lib/instr.js'), ctx);
  return { M: ctx.window.MOCK, I: ctx.window.INSTR };
}

test('#345: kindOf real.js и INSTR.kindOf — по правилу api/concepts на каждом instr_type', () => {
  const { M, I } = load();
  assert.strictEqual(M.positions.length, DOC.cases.length);
  DOC.cases.forEach((c, i) => {
    const p = M.positions.find(x => x.id === 'K' + i);  // real.js упорядочивает позиции по-своему
    assert.strictEqual(I.kindOf(p), c.table, c.name + ': тип таблицы');
    // не оценённая позиция: группа kindOf real.js — группа типа таблицы (РЕПО — MM, #361), кроме облигации без класса
    if (c.unvalued) { if (c.table !== 'BOND_NA') assert.strictEqual(p.group, I.group[c.table], c.name + ': группа'); return; }
    assert.strictEqual(JSON.stringify([p.group, p.kind]), JSON.stringify(c.kindOf), c.name + ": kindOf");
    if (c.bondNa) assert.strictEqual(I.group[c.table], 'OTH', c.name);
    // OTC с незнакомой группой продукта (новая): таблица — «Не классифицировано», kindOf — DER.OTC; известное
    // расхождение (#367); РЕПО — не оно (MM и там, и там, #361)
    else if (c.knownMismatch) assert.ok(c.knownMismatch === '#367' && c.table === 'UNCLASSIFIED' && I.group[c.table] === 'OTH' && p.group === 'DER' && p.productGroup !== 'repo', c.name);
    else assert.strictEqual(I.group[c.table], c.kindOf[0], c.name + ': группа таблицы — группа kindOf');
  });
});

test('#345: список полон, таблицы JS и Python одни', () => {
  const seen = new Set(DOC.cases.filter(c => !c.unvalued).map(c => c.row.instrType));
  assert.deepStrictEqual([...seen].sort(), DOC.instrTypes.slice().sort());
  const { I } = load();
  const pgs = new Set(DOC.cases.map(c => ((c.row.extra || {}).reported || {}).x).filter(Boolean).map(x => x.x_product_group));
  Object.keys(I.PRODUCT_KIND).forEach(pg => assert.ok(pgs.has(pg), 'группа продукта ' + pg + ' — в списке'));
  // INSTR.KINDS — зеркало api/concepts.INSTR_KINDS: тип и группа, в том же порядке
  const py = fs.readFileSync(path.join(__dirname, '..', '..', 'backend', 'src', 'desk', 'api', 'concepts.py'), 'utf8');
  const block = py.slice(py.indexOf('INSTR_KINDS:'), py.indexOf('KIND_GROUP ='));
  const pyKinds = [...block.matchAll(/\("([A-Z_]+)", "([A-Z]+)", "/g)].map(m => [m[1], m[2]]);
  assert.strictEqual(JSON.stringify(I.KINDS.map(k => [k[0], k[1]])), JSON.stringify(pyKinds));
  assert.strictEqual(I.group.OTC, 'DER');
  // группы — как api/concepts.GROUPS, в том же порядке; РЕПО — своя группа денежного рынка и фондирования (#361)
  const pyGroups = py.match(/^GROUPS = \(([^)]*)\)/m)[1].match(/"([A-Z]+)"/g).map(s => s.slice(1, -1));
  assert.strictEqual(JSON.stringify(I.GROUPS), JSON.stringify(pyGroups));
  assert.deepStrictEqual([I.group.REPO, I.label.REPO, I.SHORT.REPO, I.GROUP_LABEL.MM], ['MM', 'РЕПО', 'РЕПО', 'Денежный рынок и фондирование']);
  assert.strictEqual(I.PRODUCT_KIND.repo, 'REPO');
});

test('#361: РЕПО — свой цвет и своё место в таблице «валюта × тип», не как у других типов', () => {
  const kit = P('concepts/lib/kit.js');
  const kc = Function('return ' + kit.match(/K\.kindColor = (\{[^}]*\})/)[1])();
  const others = Object.entries(kc).filter(([k]) => k !== 'REPO').map(([, v]) => v.toLowerCase());
  assert.ok(kc.REPO && !others.includes(kc.REPO.toLowerCase()), 'K.kindColor.REPO не совпадает с цветами других видов');
  const pal = Function('return ' + kit.match(/K\.PAL = (\[[^\]]*\])/)[1])().map(v => v.toLowerCase());
  assert.ok(!pal.includes(kc.REPO.toLowerCase()), 'и с палитрой типов без своего цвета');
  const { I } = load();
  const orders = Object.values(I.order);
  assert.strictEqual(new Set(orders).size, orders.length, 'порядок типов таблицы без повторов');
  assert.ok(I.order.REPO > I.order.OTC && I.order.REPO < I.order.EQUITY, 'РЕПО — после деривативов, перед «Прочим»');
});
