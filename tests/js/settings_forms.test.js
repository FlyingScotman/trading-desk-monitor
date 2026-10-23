// Формы настроек адаптера (R40, prototypes/settings-forms.js): что рисуется по схеме вкладки (таблицы, поля, виды полей,
// выпадающие списки), проверка с кодами адаптера (та же, что на сервере: backend/tests/test_settings_forms.py), тексты
// по-русски, сравнение «было → станет», книги из позиций «не настроено», история ставок фондирования.
// Схемы — вкладки trades-data-adapter (backend/tests/fixtures/settings_tabs, синтетика).
//   node tests/js/settings_forms.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const FIX = path.join(ROOT, 'backend', 'tests', 'fixtures', 'settings_tabs');
const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'prototypes', 'settings-forms.js'), 'utf8'), ctx);
const SF = ctx.SETTINGS_FORMS;
const TABS = JSON.parse(fs.readFileSync(path.join(FIX, 'tda_settings_tabs.json'), 'utf8'));
const S = {}; TABS.forEach(t => { S[t.id] = t.schema; });
const file = n => JSON.parse(fs.readFileSync(path.join(FIX, n), 'utf8'));
const plain = v => v === undefined ? v : JSON.parse(JSON.stringify(v));
const eq = (a, b, m) => assert.deepStrictEqual(plain(a), plain(b), m);  // массивы из vm — другой realm
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

t('раскладка: основная таблица первой, группы полей, поля верхнего уровня', () => {
  const b = SF.layout(S.books);
  eq(b.tables.map(x => x.key), ['books']);
  assert.ok(b.tables[0].required);
  const f = SF.layout(S.funding);
  eq(f.tables.map(x => x.key), ['rules', 'bond_class_spreads']);
  eq(f.objects.map(x => x.key), ['repo_policy']);
  const o = SF.layout(S.sources);
  eq(o.fields.map(c => c.key + ':' + c.kind), ['auto_books:bool', 'report_dirs:strlist', 'auto_books_scan_files:integer', 'contract_1_1_fields:bool']);
  eq(o.objects.map(x => x.key), ['sources', 'report_risk', 'provisional_trade_terms']);
});

t('столбцы: вид поля по схеме, обязательные и заголовки по-русски', () => {
  const cols = SF.columns(S.books.properties.books.items), by = {};
  cols.forEach(c => { by[c.key] = c; });
  assert.strictEqual(by.book_type.kind, 'enum');
  eq(plain(by.book_type.enum), ['TRADING', 'HTM', 'DERIVS', 'FUTURES']);
  assert.strictEqual(by.active.kind, 'bool'); assert.strictEqual(by.active.default, true);
  assert.strictEqual(by.valid_from.kind, 'date');
  assert.strictEqual(by.codes.kind, 'codes');
  assert.ok(by.book_id.required && by.entity.required && !by.ocp_to.required);
  assert.strictEqual(by.ocp_to.title, 'ОВП переносится в');
  const r = {}; SF.columns(S.funding.properties.rules.items).forEach(c => { r[c.key] = c; });
  assert.strictEqual(r.spread_bp.kind, 'amount');
  assert.strictEqual(r.day_count.kind, 'enum');
  const repo = {}; SF.columns(S.funding.properties.repo_policy).forEach(c => { repo[c.key] = c; });
  assert.ok(repo.rate.nullable && repo.rate.kind === 'enum');
});

t('выпадающие списки: enum, юрлица, книги (ОВП), валюты, индексы хаба', () => {
  const L = { entities: [{ value: 'ENT1', label: 'Первое' }], books: [{ value: 'T1' }], currencies: [{ value: 'RUB' }], indices: [{ value: 'KEY_RATE' }] };
  const col = (schema, list, key) => SF.columns(schema.properties[list].items).find(c => c.key === key);
  eq(plain(SF.optionsFor(col(S.books, 'books', 'book_type'), L, 'books')).map(o => o.value), ['TRADING', 'HTM', 'DERIVS', 'FUTURES']);
  assert.strictEqual(SF.optionsFor(col(S.books, 'books', 'entity'), L, 'books')[0].value, 'ENT1');
  assert.strictEqual(SF.optionsFor(col(S.books, 'books', 'ocp_to'), L, 'books')[0].value, 'T1');
  assert.strictEqual(SF.optionsFor(col(S.books, 'books', 'book_id'), L, 'books'), null);  // свой код, не ссылка
  assert.strictEqual(SF.optionsFor(col(S.entities, 'entities', 'entity'), L, 'entities'), null);
  assert.strictEqual(SF.optionsFor(col(S.funding, 'rules', 'book_id'), L, 'funding')[0].value, 'T1');
  assert.strictEqual(SF.optionsFor(col(S.funding, 'rules', 'ccy'), L, 'funding')[0].value, 'RUB');
  assert.strictEqual(SF.optionsFor(col(S.funding, 'rules', 'index_code'), L, 'funding')[0].value, 'KEY_RATE');
  assert.strictEqual(SF.optionsFor(col(S.funding, 'rules', 'index_code'), {}, 'funding'), null);  // нет списка — ввод текстом
  assert.strictEqual(SF.optionsFor(col(S.books, 'books', 'name'), L, 'books'), null);
});

t('проверка: синтетические файлы проходят свои вкладки', () => {
  for (const [tab, name] of [['books', 'books.json'], ['entities', 'entities.json'], ['funding', 'funding.json'], ['sources', 'options.json']])
    eq(plain(SF.validate(S[tab], file(name))), [], tab);
});

t('проверка: те же коды и пути, что у сервера (desk.settings_forms.check_schema)', () => {
  const doc = { books: [{ book_id: 'T1', name: '', desk: 'D', book_type: 'SWAPS', entity: 'E', valid_from: '2026-13-01', colour: 'red', x_portfolio: 'P', active: null },
                        { book_id: 'T2', name: 'N', desk: 'D', book_type: 'HTM' }], _comment: 'kept', extra: 1 };
  const got = SF.validate(S.books, doc).map(i => i.code + '|' + i.path.join('.') + '|' + i.severity).sort();
  eq(got, ['bad_date|books.0.valid_from|error', 'bad_value|books.0.book_type|error', 'bad_value|books.0.name|error',
    'missing_required|books.1.entity|error', 'unknown_config_key|extra|warning', 'unknown_field|books.0.colour|warning']);
  const rules = { rules: [{ rate_kind: 'index_spread', spread_bp: '1,5', ccy: 'RUBL', day_count: '30/360', index_code: 'KEY_RATE' },
                          { rate_kind: 'fixed', fixed_rate_pct: 5.5, valid_from: '2026-10-19' }],
                  repo_policy: { rate: null, contract_rate_from_days: -1 } };
  eq(SF.validate(S.funding, rules).map(i => i.code + '|' + i.path.join('.')).sort(),
    ['bad_number|repo_policy.contract_rate_from_days', 'bad_number|rules.0.spread_bp', 'bad_value|rules.0.ccy', 'bad_value|rules.0.day_count']);
  assert.strictEqual(SF.validate(S.books, [])[0].code, 'bad_config');
  const codes = { books: [{ book_id: 'T', name: 'N', desk: 'D', book_type: 'HTM', entity: 'E', codes: { no_such_source: ['A'], '*': [''] } }] };
  eq([...new Set(SF.validate(S.books, codes).map(i => i.code))], ['bad_value']);
});

t('правила адаптера сверх схемы: «индекс + спред» без индекса, фикс без ставки', () => {
  const iss = SF.validate(S.funding, { rules: [{ rate_kind: 'index_spread', spread_bp: 10 }, { rate_kind: 'fixed' }] });
  eq(iss.map(i => i.hint + '|' + i.path.join('.')), ['needs_index|rules.0.index_code', 'needs_fixed_rate|rules.1.fixed_rate_pct']);
  assert.strictEqual(SF.say(iss[0], S.funding), 'Индекс: для «индекс + спред» нужен индекс');
});

t('тексты по-русски: заголовок поля из схемы и смысл кода', () => {
  const [i] = SF.validate(S.books, { books: [{ book_id: 'T', name: 'N', desk: 'D', book_type: 'HTM', entity: 'E', valid_from: '19.10.2026' }] });
  assert.strictEqual(SF.say(i, S.books), 'Действует с: нужна дата (ГГГГ-ММ-ДД)');
  assert.strictEqual(SF.say({ code: 'unknown_ocp_to', message: 'books: ocp_to names no book' }, S.books), 'ОВП переносится в книгу, которой нет в списке книг');
  assert.strictEqual(SF.say({ code: 'bad_value', field: 'book_type', message: 'field book_type' }, S.books), 'Тип книги: недопустимое значение');
  assert.strictEqual(SF.say({ code: 'some_new_code', message: 'as the adapter says' }, S.books), 'as the adapter says');
});

t('замечание адаптера → файл (у reference() в замечании нет имени файла)', () => {
  const f = (code, message) => SF.issueFile({ code, message });
  assert.strictEqual(f('bad_value', 'field book_type'), 'books.json');
  assert.strictEqual(f('unknown_ocp_to', 'books: ocp_to names no book'), 'books.json');
  assert.strictEqual(f('unknown_entity', 'books: entity not in reference() entities'), 'books.json');
  assert.strictEqual(f('duplicate_key', 'entities: entity and valid_from repeated'), 'entities.json');
  assert.strictEqual(f('bad_date', 'entity: field valid_from'), 'entities.json');
  assert.strictEqual(f('missing_required', 'rule: index_spread needs index_code'), 'funding.json');
  assert.strictEqual(f('unknown_entity', 'funding_rules: entity not in entities'), 'funding.json');
  assert.strictEqual(f('bad_options', 'options.json: report_risk has the wrong type'), 'options.json');
  assert.strictEqual(f('bad_reference', 'reference() must return {books: [...], issues: [...]}'), null);
});

t('ввод: суммы без потерь, коды книги, канонический вид', () => {
  assert.strictEqual(SF.parseAmount('1,5'), 1.5);
  assert.strictEqual(SF.parseAmount(' 150 '), 150);
  assert.strictEqual(SF.parseAmount('1.50'), '1.50');  // строкой: «1.5» потеряло бы запись
  assert.strictEqual(SF.parseAmount('abc'), 'abc');
  assert.strictEqual(SF.parseAmount(''), undefined);
  assert.strictEqual(SF.parseNumber('5', true), 5);
  assert.strictEqual(SF.parseNumber('5.5', true), '5.5');
  const codes = SF.parseCodes('positions_daily: A, B; *: C');
  eq(plain(codes), { positions_daily: ['A', 'B'], '*': ['C'] });
  assert.strictEqual(SF.fmtCodes(codes), 'positions_daily: A, B; *: C');
  eq(plain(SF.parseCodes('X1')), { '*': ['X1'] });
  assert.strictEqual(SF.parseCodes('  '), undefined);
  assert.strictEqual(SF.canon({ b: 1, a: [1, { d: 2, c: 3 }] }), SF.canon({ a: [1, { c: 3, d: 2 }], b: 1 }));
  assert.strictEqual(SF.display({ kind: 'date' }, '2026-10-19'), '19.10.2026');
  eq(plain(SF.empty(S.funding)), { rules: [] });
});

t('сравнение «было → станет»: строки, номера, пропуски без изменений', () => {
  const a = SF.text(file('books.json'));
  const d = file('books.json'); d.books[1].name = 'Переименована'; d.books.push({ book_id: 'N1' });
  const lines = SF.diff(a, SF.text(d)), st = SF.diffStats(lines);
  eq(st, { added: 4, removed: 1 });  // имя: −1 +1; новая книга: «},» «{» «book_id» (закрывающая «}» совпала)
  assert.ok(lines.some(x => x.op === '-' && /Synthetic derivatives book/.test(x.text)));
  assert.ok(lines.some(x => x.op === '+' && /Переименована/.test(x.text)));
  const hk = SF.hunks(lines, 2);
  assert.ok(hk.some(x => x.op === '…' && x.skipped > 0) && hk.length < lines.length);
  assert.strictEqual(SF.diffStats(SF.diff(a, a)).added, 0);
  eq(plain(SF.diff('', 'x\n')).map(x => x.op + x.text), ['+x']);
});

t('книги из позиций, которых нет в books.json: по коду книги и по кодам отчётов', () => {
  const doc = { books: [{ book_id: 'T1', codes: { positions_daily: ['LOC1'] } }] };
  const recent = [{ book_id: 'T1' }, { book_id: 'LOC1' }, { book_id: 'NEW', entity: 'ООО Ромашка', book_type: 'TRADING', name: 'NEW', desk: 'auto' },
                  { book_id: 'B7@ENT9', entity: 'ENT9' }];
  eq(SF.unconfigured(doc, recent).map(r => r.book_id), ['NEW', 'B7@ENT9']);
  eq(plain(SF.bookPrefill(recent[2])), { book_id: 'NEW', name: 'NEW', book_type: 'TRADING', desk: 'auto', entity: 'ООО Ромашка' });
  eq(plain(SF.bookPrefill(recent[3]).codes), { '*': ['B7'] });  // авто-книга «код@юрлицо»
  assert.strictEqual(SF.bookPrefill({ book_id: 'X' }).book_type, 'TRADING');
});

t('фондирование: история ставок по rule_id и сводка «тип книги × валюта» на дату', () => {
  const rules = file('funding.json').rules;
  const g = SF.ruleGroups(rules, '2026-10-22'), tr = g.find(x => x.id === 'trading_rub');
  eq(tr.rows.map(x => x.state + ':' + (x.rule.valid_from || '-') + ':' + (x.until || '-')), ['past:-:2026-10-19', 'current:2026-10-19:-']);
  assert.strictEqual(SF.rateText(tr.current), 'KEY_RATE + 150 б.п.');
  const before = SF.ruleGroups(rules, '2026-10-18').find(x => x.id === 'trading_rub');
  eq(before.rows.map(x => x.state), ['current', 'future']);
  assert.strictEqual(SF.rateText({ rate_kind: 'fixed', fixed_rate_pct: 5.25 }), '5,25 % фикс');
  assert.strictEqual(SF.rateText({ rate_kind: 'index_spread', index_code: 'RUONIA', spread_bp: -20 }), 'RUONIA − 20 б.п.');
  assert.strictEqual(SF.rateText({ rate_kind: 'none' }), 'не фондируется');
  assert.strictEqual(SF.ruleId({ book_type: 'HTM', ccy: 'RUB' }), 'auto:ccy=RUB;book_type=HTM');
  const M = SF.fundingMatrix(rules, '2026-10-22', ['TRADING', 'HTM', 'DERIVS', 'FUTURES']);
  eq(plain(M.cols), ['RUB', 'USD', '*']);
  assert.strictEqual(M.cell('TRADING', 'RUB').text, 'KEY_RATE + 150 б.п.');
  assert.strictEqual(M.cell('TRADING', 'USD').text, '6 % фикс');
  assert.strictEqual(M.cell('HTM', '*').text, 'контрактная');
  assert.strictEqual(M.cell('HTM', 'RUB'), null);
  assert.ok(M.special.some(x => x.current.entity === 'ENT2'));  // частное правило юрлица — не в сводке
});

console.log(n + ' tests ok');
