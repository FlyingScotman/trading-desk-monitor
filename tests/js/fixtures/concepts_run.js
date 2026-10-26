// Фикстура прогона для JS-тестов мониторов (prototypes/concepts/): window.CONCEPTS_RUN в форме /concepts-run.js и
// /api/v1/concepts/run (backend api/concepts_run.py). concepts-run.json собран один раз из демо-набора prototypes/data.js:
// только ключи, которые отдаёт сервер, эмитенты (кроме Минфина), группы и контрагенты — нейтральные названия.
// Что у сервера есть все её ключи, проверяет backend/tests/test_concepts_run_fixture.py
// (test_js_fixture_keys_are_in_the_servers_answer).
// loadRun(ctx) — window.CONCEPTS_RUN внутри vm-контекста (объекты контекста, как у скрипта сервера); вернёт его.
'use strict';
const fs = require('fs');
const path = require('path');

const FILE = path.join(__dirname, 'concepts-run.json');
const TEXT = fs.readFileSync(FILE, 'utf8');

function loadRun(ctx) {
  const vm = require('vm');
  vm.runInContext('window.CONCEPTS_RUN = ' + TEXT + ';', ctx);
  return vm.runInContext('window.CONCEPTS_RUN', ctx);
}

module.exports = { loadRun, FILE, TEXT };
