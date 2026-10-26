// Фикстура прогона для JS-тестов мониторов (prototypes/concepts/): window.CONCEPTS_RUN в форме /concepts-run.js и
// /api/v1/concepts/run (backend api/concepts_run.py). concepts-run.json собран один раз из демо-набора prototypes/data.js:
// только ключи, которые отдаёт сервер, эмитенты (кроме Минфина), группы и контрагенты — нейтральные названия.
// Что у сервера есть все её ключи, проверяет backend/tests/test_concepts_run_fixture.py
// (test_js_fixture_keys_are_in_the_servers_answer).
// loadRun(ctx) — window.CONCEPTS_RUN внутри vm-контекста (объекты контекста, как у скрипта сервера); вернёт его.
//
// Ключи, которые тест дописывает в прогон (mutate, #310), сверяются с server-keys.json — ключами, которые сервер может
// отдать, по уровням: верх документа, source, позиция, её pnl. Файл пишет и сверяет с сервером тот же бэкенд-тест.
// Сверка — при каждом чтении window.CONCEPTS_RUN (геттер): тест правит прогон до lib/real.js, а real.js читает его
// после правки, поэтому поле, которого у сервера нет, роняет тест на загрузке real.js, как бы тест ни правил прогон
// (mutate(loadRun(ctx)), mutate(ctx.window.CONCEPTS_RUN) или присвоив новый документ).
'use strict';
const fs = require('fs');
const path = require('path');

const FILE = path.join(__dirname, 'concepts-run.json');
const TEXT = fs.readFileSync(FILE, 'utf8');
const SERVER_KEYS = JSON.parse(fs.readFileSync(path.join(__dirname, 'server-keys.json'), 'utf8'));
const KNOWN = Object.fromEntries(Object.entries(SERVER_KEYS).map(([level, ks]) => [level, new Set(ks)]));

// ключи документа, которых сервер не отдаёт: ['position.foo', …]
function unknownKeys(D) {
  const bad = new Set(), check = (level, o) => {
    if (o && typeof o === 'object' && !Array.isArray(o)) Object.keys(o).forEach(k => { if (!KNOWN[level].has(k)) bad.add(level + '.' + k); });
  };
  check('top', D);
  check('source', D.source);
  (Array.isArray(D.positions) ? D.positions : []).forEach(p => { check('position', p); check('pnl', p && p.pnl); });
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
