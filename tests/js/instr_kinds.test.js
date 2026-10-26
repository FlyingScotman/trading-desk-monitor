// Одно правило вида инструмента (#345): lib/real.js kindOf ([группа, вид], зеркало publish/summary.kind_of) и
// lib/instr.js INSTR.kindOf (тип таблицы «валюта × тип», зеркало api/concepts.instr_kind) на общем списке случаев
// tests/fixtures/instr_kinds.json — том же, что сверяет backend/tests/test_instr_kinds.py с Python. Группа kindOf
// равна группе типа таблицы (кроме облигации без класса); OTC без группы продукта — DER (тип «OTC»), не
// «Не классифицировано»; OTC с незнакомой группой (repo, новая) — «Не классифицировано», известное расхождение (#361); XCCY старого tdm — XCCY у kindOf и CCS в таблице (тот же вид, имя таблицы).
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
    if (c.unvalued) return;
    assert.strictEqual(JSON.stringify([p.group, p.kind]), JSON.stringify(c.kindOf), c.name + ": kindOf");
    if (c.bondNa) assert.strictEqual(I.group[c.table], 'OTH', c.name);
    // OTC с незнакомой группой продукта (repo, новая): таблица — «Не классифицировано», kindOf — DER.OTC; известное
    // расхождение, пока владелец не решит, где показывать РЕПО (#361)
    else if (c.knownMismatch) assert.ok(c.knownMismatch === '#361' && c.table === 'UNCLASSIFIED' && I.group[c.table] === 'OTH' && p.group === 'DER', c.name);
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
});
