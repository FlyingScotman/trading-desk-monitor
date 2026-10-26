// Фикстура прогона для JS-тестов мониторов (prototypes/concepts/): window.CONCEPTS_RUN в форме /concepts-run.js и
// /api/v1/concepts/run (backend api/concepts_run.py). concepts-run.json собран один раз из демо-набора prototypes/data.js:
// только ключи, которые отдаёт сервер, эмитенты (кроме Минфина), группы и контрагенты — нейтральные названия.
// Что у сервера есть все её ключи, проверяет backend/tests/test_concepts_run_fixture.py
// (test_js_fixture_keys_are_in_the_servers_answer).
// loadRun(ctx) — window.CONCEPTS_RUN внутри vm-контекста (объекты контекста, как у скрипта сервера); вернёт его.
//
// Ключи, которые тест дописывает в прогон (mutate, #310), сверяются с server-keys.json — ключами, которые сервер может
// отдать, по уровням: верх документа, source, позиция, её pnl, её extra и элемент CURVES (#324). Файл пишет и сверяет с
// сервером тот же бэкенд-тест. Ключи CURVES, risk и fxDelta позиции — не поля, а id кривых и валют: их сверяем с
// шаблонами ids (id кривой начинается с её валюты), а значения — по форме: у risk — число (или null) на каждый бакет
// BUCKETS, у fxDelta — число. Поле, переименованное сервером внутри этих структур, тоже роняет тест.
// Сверка — при каждом чтении window.CONCEPTS_RUN (геттер): тест правит прогон до lib/real.js, а real.js читает его
// после правки, поэтому поле, которого у сервера нет, роняет тест на загрузке real.js, как бы тест ни правил прогон
// (mutate(loadRun(ctx)), mutate(ctx.window.CONCEPTS_RUN) или присвоив новый документ).
'use strict';
const fs = require('fs');
const path = require('path');

const FILE = path.join(__dirname, 'concepts-run.json');
const TEXT = fs.readFileSync(FILE, 'utf8');
const SERVER_KEYS = JSON.parse(fs.readFileSync(path.join(__dirname, 'server-keys.json'), 'utf8'));
const KNOWN = Object.fromEntries(Object.entries(SERVER_KEYS).filter(([level]) => level !== 'ids').map(([level, ks]) => [level, new Set(ks)]));
const IDS = Object.fromEntries(Object.entries(SERVER_KEYS.ids).map(([k, re]) => [k, new RegExp(re)]));

// ключи документа, которых сервер не отдаёт: ['position.foo', 'extra.foo', 'risk.RUB RUONIA', …]
function unknownKeys(D) {
  const bad = new Set(), isObj = o => o && typeof o === 'object' && !Array.isArray(o), check = (level, o) => {
    if (isObj(o)) Object.keys(o).forEach(k => { if (!KNOWN[level].has(k)) bad.add(level + '.' + k); });
  };
  // id-ключи (кривая, валюта): id по шаблону и значение нужной формы
  const ids = (level, o, ok) => { if (isObj(o)) Object.entries(o).forEach(([k, v]) => { if (!IDS[level].test(k) || !ok(v)) bad.add(level + '.' + k); }); };
  const nb = Array.isArray(D.BUCKETS) ? D.BUCKETS.length : null, isNum = v => typeof v === 'number';
  check('top', D);
  check('source', D.source);
  ids('CURVES', D.CURVES, c => { check('curve', c); return isObj(c); });
  (Array.isArray(D.positions) ? D.positions : []).forEach(p => {
    check('position', p); check('pnl', p && p.pnl);
    if (!p) return;
    check('extra', p.extra);
    ids('risk', p.risk, v => Array.isArray(v) && (nb == null || v.length === nb) && v.every(x => x === null || isNum(x)));
    ids('fxDelta', p.fxDelta, isNum);
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
