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
// кривой риска (которой нет ни в списках, ни в CURVES) или поле, переименованное сервером внутри этих структур, тоже
// роняет тест.
// #337: у кривой ccy — валюта её id (первые три буквы; кривая валюты с id из одного кода — в RUB, если курса этой
// валюты в FX прогона нет, как у сервера), kind — из ids.curveKinds; ключи FX и fxDelta и ccy позиции (или null) — по
// ids.ccyPattern, курс FX — конечное число или null; вложенные структуры extra, которые читают страницы (extraNested:
// funding — объект, funding.lines[] и legs[] — массивы объектов), — с известными ключами. Опечатка, одинаково внесённая
// в CURVES и risk, с валютой id и известным kind проходит: так выглядит новая кривая сервера.
// jsCode(src) — исходник без комментариев (сверка EXTRA_KEYS_READ с тем, что читает lib/real.js, #337).
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
const CURVE_ID = new RegExp(SERVER_KEYS.ids.curvePattern), CCY = new RegExp(SERVER_KEYS.ids.ccyPattern);
const KINDS = new Set(SERVER_KEYS.ids.curveKinds);
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
  const isCcy = v => isStr(v) && CCY.test(v), fx = isObj(D.FX) ? D.FX : {};
  // валюта кривой — валюта её id; кривая валюты (id — код) без курса в FX — в RUB (tdm_run.curves_reference;
  // с курсом сервер возвращает ей валюту: api/concepts_run.py:196–198, assemble)
  const ownCcy = (id, c) => c.ccy === id.slice(0, 3) || (c.ccy === 'RUB' && CCY.test(id) && !(id in fx));
  // id-ключи (кривая, валюта): id известен и значение нужной формы
  const ids = (level, o, known, ok) => { if (isObj(o)) Object.entries(o).forEach(([k, v]) => { if (!known(k) || !ok(v)) bad.add(level + '.' + k); }); };
  check('top', D);
  check('source', D.source);
  if (isObj(D.CURVES)) Object.entries(D.CURVES).forEach(([k, c]) => {
    check('curve', c);
    if (!CURVE_ID.test(k) || !(isObj(c) && isStr(c.name) && KINDS.has(c.kind) && isCcy(c.ccy) && ownCcy(k, c) && isRow(c.pts))) bad.add('CURVES.' + k);
  });
  ids('FX', D.FX, k => CCY.test(k), v => v === null || isNum(v));
  (Array.isArray(D.positions) ? D.positions : []).forEach(p => {
    check('position', p); check('pnl', p && p.pnl);
    if (!p) return;
    if (p.ccy != null && !isCcy(p.ccy)) bad.add('ccy.' + p.id);
    if (p.extra != null && !isObj(p.extra)) bad.add('extra.' + p.id); else { check('extra', p.extra); if (p.extra) nested(p.extra).forEach(x => bad.add(x)); }
    ids('risk', p.risk, k => curves.has(k), isRow);
    ids('fxDelta', p.fxDelta, k => ccys.has(k) && CCY.test(k), isNum);
  });
  return [...bad].sort();
}

// вложенные структуры extra (extraNested, #337): «a» — объект, «a[]» — массив объектов, «a.b[]» — внутри «a»; ключи —
// из списка. Ошибка — «extra.<путь>.<ключ>» или «extra.<путь>» (не та форма); нет поля или null — не ошибка
function nested(extra) {
  const bad = [], isObj = o => !!o && typeof o === 'object' && !Array.isArray(o);
  Object.entries(SERVER_KEYS.extraNested).forEach(([spec, known]) => {
    const parts = spec.split('.');
    let nodes = [['extra', extra]];
    parts.forEach((part, i) => {
      const many = part.endsWith('[]'), k = many ? part.slice(0, -2) : part, last = i === parts.length - 1, next = [];
      nodes.forEach(([path, o]) => {
        const v = o[k], p = path + '.' + k;
        if (v == null) return;
        const items = many ? (Array.isArray(v) ? v : null) : [v];
        if (!items || !items.every(isObj)) { if (last) bad.push(p + (items && many ? '[]' : '')); return; }  // родитель — ошибка своей строки
        items.forEach(x => next.push([p + (many ? '[]' : ''), x]));
      });
      nodes = next;
    });
    nodes.forEach(([path, o]) => Object.keys(o).forEach(key => { if (!known.includes(key)) bad.push(path + '.' + key); }));
  });
  return bad;
}

// исходник JS без комментариев // и /* */ (строки, шаблоны и регулярные выражения — как есть): ключ, упомянутый только в
// комментарии, не считается прочитанным
function jsCode(src) {
  let out = '', i = 0, prev = '';
  const n = src.length;
  while (i < n) {
    const c = src[i], d = src[i + 1];
    if (c === '/' && d === '/') { while (i < n && src[i] !== '\n') i++; continue; }
    if (c === '/' && d === '*') { const e = src.indexOf('*/', i + 2); i = e < 0 ? n : e + 2; out += ' '; continue; }
    if (c === '"' || c === "'" || c === '`') {
      let j = i + 1;
      while (j < n && src[j] !== c) { if (src[j] === '\\') j++; j++; }
      out += src.slice(i, j + 1); i = j + 1; prev = c; continue;
    }
    // регулярное выражение: «/» там, где не может стоять деление (после оператора, скобки, запятой, return …)
    if (c === '/' && (prev === '' || /[(,=:[!&|?{};+\-*%<>~^]/.test(prev) || /\b(return|typeof|case|in|of)$/.test(out.trimEnd()))) {
      let j = i + 1, cls = false;
      while (j < n && (cls || src[j] !== '/')) { if (src[j] === '\\') j++; else if (src[j] === '[') cls = true; else if (src[j] === ']') cls = false; j++; }
      out += src.slice(i, j + 1); i = j + 1; prev = 'a'; continue;
    }
    out += c; i++;
    if (!/\s/.test(c)) prev = c;
  }
  return out;
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

module.exports = { loadRun, unknownKeys, jsCode, FILE, TEXT, SERVER_KEYS };
