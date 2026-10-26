// Фикстура прогона для JS-тестов мониторов (prototypes/concepts/): window.CONCEPTS_RUN в форме /concepts-run.js и
// /api/v1/concepts/run (backend api/concepts_run.py). concepts-run.json собран один раз из демо-набора prototypes/data.js:
// только ключи, которые отдаёт сервер, эмитенты (кроме Минфина), группы и контрагенты — нейтральные названия.
// Что у сервера есть все её ключи, проверяет backend/tests/test_concepts_run_fixture.py
// (test_js_fixture_keys_are_in_the_servers_answer).
// loadRun(ctx) — window.CONCEPTS_RUN внутри vm-контекста (объекты контекста, как у скрипта сервера); вернёт его.
//
// Ключи, которые тест дописывает в прогон (mutate, #310), сверяются с server-keys.json — ключами, которые сервер может
// отдать, по уровням: верх документа, source, позиция, её pnl, её extra и элемент CURVES (#324). Файл пишет и сверяет с
// сервером тот же бэкенд-тест. Ключи CURVES, risk и fxDelta позиции — не поля, а id кривых и валют (#324): ключ risk —
// id из ids.curves (кривые golden-прогонов) или из CURVES прогона (и исходной фикстуры), ключ fxDelta — валюта из
// ids.ccys, из FX прогона или валюта одной из его позиций (курса может не быть: «нет курса OMR»), ключ CURVES — по
// ids.curvePattern. Значения — по форме: risk и pts кривой — конечное число или null на
// каждый бакет BUCKETS, fxDelta — конечное число, у кривой name / ccy / kind — строки, extra — объект. Так опечатка в id
// кривой или поле, переименованное сервером внутри этих структур, тоже роняет тест.
// Сверка — при каждом чтении window.CONCEPTS_RUN (геттер): тест правит прогон до lib/real.js, а real.js читает его
// после правки, поэтому поле, которого у сервера нет, роняет тест на загрузке real.js, как бы тест ни правил прогон
// (mutate(loadRun(ctx)), mutate(ctx.window.CONCEPTS_RUN) или присвоив новый документ).
'use strict';
const fs = require('fs');
const path = require('path');

const FILE = path.join(__dirname, 'concepts-run.json');
const TEXT = fs.readFileSync(FILE, 'utf8');
const SERVER_KEYS = JSON.parse(fs.readFileSync(path.join(__dirname, 'server-keys.json'), 'utf8'));
const LEVELS = ['top', 'source', 'position', 'pnl', 'extra', 'curve'];
const KNOWN = Object.fromEntries(LEVELS.map(level => [level, new Set(SERVER_KEYS[level])]));
const CURVE_ID = new RegExp(SERVER_KEYS.ids.curvePattern);
// кривые самой фикстуры (её проверяет бэкенд-тест): тест может убрать кривую из CURVES, оставив риск на ней (прогон
// старого сервера, «кривой нет»), и это не опечатка
const BASE_CURVES = Object.keys(JSON.parse(TEXT).CURVES);

// ключи документа, которых сервер не отдаёт: ['position.foo', 'extra.foo', 'risk.RUB FOO', …]
function unknownKeys(D) {
  const bad = new Set(), isObj = o => !!o && typeof o === 'object' && !Array.isArray(o), check = (level, o) => {
    if (isObj(o)) Object.keys(o).forEach(k => { if (!KNOWN[level].has(k)) bad.add(level + '.' + k); });
  };
  const nb = Array.isArray(D.BUCKETS) ? D.BUCKETS.length : null;
  const isNum = v => typeof v === 'number' && isFinite(v), isStr = v => typeof v === 'string';
  const isRow = v => Array.isArray(v) && (nb == null || v.length === nb) && v.every(x => x === null || isNum(x));
  const own = (o, more) => (isObj(o) ? Object.keys(o) : []).concat(more || []);
  const ps = Array.isArray(D.positions) ? D.positions : [];
  const curves = new Set(SERVER_KEYS.ids.curves.concat(BASE_CURVES, own(D.CURVES)));
  const ccys = new Set(SERVER_KEYS.ids.ccys.concat(own(D.FX), ps.map(p => p && p.ccy)));
  // id-ключи (кривая, валюта): id известен и значение нужной формы
  const ids = (level, o, known, ok) => { if (isObj(o)) Object.entries(o).forEach(([k, v]) => { if (!known(k) || !ok(v)) bad.add(level + '.' + k); }); };
  check('top', D);
  check('source', D.source);
  ids('CURVES', D.CURVES, k => CURVE_ID.test(k), c => { check('curve', c); return isObj(c) && isStr(c.name) && isStr(c.kind) && isStr(c.ccy) && /^[A-Z]{3}$/.test(c.ccy) && isRow(c.pts); });
  (Array.isArray(D.positions) ? D.positions : []).forEach(p => {
    check('position', p); check('pnl', p && p.pnl);
    if (!p) return;
    if (p.extra != null && !isObj(p.extra)) bad.add('extra.' + p.id); else check('extra', p.extra);
    ids('risk', p.risk, k => curves.has(k), isRow);
    ids('fxDelta', p.fxDelta, k => ccys.has(k), isNum);
  });
  return [...bad].sort();
}

function checkRun(D) {
  if (!D || typeof D !== 'object') return D;
  const bad = unknownKeys(D);
  if (bad.length) throw new Error('тест дописал в прогон ключи, которых сервер не отдаёт (tests/js/fixtures/server-keys.json, #310): ' + bad.join(', '));
  return D;
}

function loadRun(ctx) {
  const vm = require('vm');
  vm.runInContext('window.CONCEPTS_RUN = ' + TEXT + ';', ctx);
  let D = vm.runInContext('window.CONCEPTS_RUN', ctx);
  Object.defineProperty(vm.runInContext('window', ctx), 'CONCEPTS_RUN', {
    configurable: true, enumerable: true, get: () => checkRun(D), set: v => { D = v; },
  });
  return D;
}

module.exports = { loadRun, unknownKeys, FILE, TEXT, SERVER_KEYS };
