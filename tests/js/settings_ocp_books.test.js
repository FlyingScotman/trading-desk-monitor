// Книги ОВП без позиций в настройках книг (TDM#556, prototypes/settings-ocp-books.js): какие книги только из отчёта ОВП,
// деск списком, тексты строки «не настроено». Чистая часть, без браузера; страница — settings_ocp_books.pw.test.js.
//   node tests/js/settings_ocp_books.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const ROOT = path.join(__dirname, '..', '..');
const ctx = { console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'prototypes', 'settings-forms.js'), 'utf8'), ctx);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'prototypes', 'settings-ocp-books.js'), 'utf8'), ctx);
const SF = ctx.SETTINGS_FORMS, SOB = ctx.SETTINGS_OCP_BOOKS;
const plain = v => v === undefined ? v : JSON.parse(JSON.stringify(v));
const eq = (a, b, m) => assert.deepStrictEqual(plain(a), plain(b), m);
let n = 0;
const t = (name, f) => { f(); n++; console.log('ok', name); };

const RECENT = [
  { book_id: 'POS1', last_date: '2026-10-20', positions: 3, desk: 'Rates', entity: 'E1', book_type: 'TRADING', ocp_only: false },
  { book_id: 'OLD1', last_date: '2026-10-19', positions: null, desk: 'AUTO', entity: 'E1', book_type: 'TRADING', ocp_only: true },
  { book_id: 'OLD2', last_date: '2026-10-18', positions: null, desk: null, entity: 'E2', book_type: null, ocp_only: true },
];

t('книги только из отчёта ОВП: по признаку сервера', () => {
  eq(SOB.ocpOnlyIds(RECENT), { OLD1: true, OLD2: true });
  assert.ok(SOB.isOcpOnly(RECENT[1]) && !SOB.isOcpOnly(RECENT[0]) && !SOB.isOcpOnly({ book_id: 'X' }) && !SOB.isOcpOnly(null));
  eq(SOB.ocpOnlyIds(null), {});
});

t('деск списком: из книг файла и справочника сервера, без заглушки AUTO, без повторов, по алфавиту', () => {
  const books = [{ book_id: 'A', desk: 'Credit' }, { book_id: 'B', desk: 'Rates' }, { book_id: 'C' }, null];
  eq(SOB.desks(books, RECENT), [{ value: 'Credit', label: '' }, { value: 'Rates', label: '' }]);
  eq(SOB.desks([], [{ desk: 'FX' }, { desk: '—' }, { desk: ' ' }]), [{ value: 'FX', label: 'из справочника' }]);
});

t('строка «не настроено»: у книги ОВП — дата ОВП и «позиций нет»; заголовок группы по составу', () => {
  assert.strictEqual(SOB.unconfiguredText(RECENT[1], SF.fmtDate), ' ОВП на ' + SF.fmtDate('2026-10-19') + ', позиций нет');
  assert.strictEqual(SOB.unconfiguredText(RECENT[0], SF.fmtDate), ' позиции на ' + SF.fmtDate('2026-10-20') + ', 3 строки');
  assert.strictEqual(SOB.groupTitle([RECENT[1], RECENT[2]]), 'Есть в отчёте ОВП, не настроено — 2');
  assert.strictEqual(SOB.groupTitle(RECENT), 'Есть в позициях или в отчёте ОВП, не настроено — 3');
  assert.strictEqual(SOB.groupTitle([RECENT[0]]), 'Есть в позициях, не настроено — 1');
});

t('выпадающий список деска в форме книг; книга ОВП добавляется в books.json как обычная', () => {
  const col = { key: 'desk', kind: 'text' };
  eq(SF.optionsFor(col, { desks: SOB.desks([], RECENT) }, 'books'), [{ value: 'Rates', label: 'из справочника' }]);
  assert.strictEqual(SF.optionsFor(col, {}, 'books'), null, 'деск без списка — поле ввода, как раньше');
  eq(SF.unconfigured({ books: [{ book_id: 'OLD1' }] }, RECENT).map(r => r.book_id), ['POS1', 'OLD2']);
  eq(SF.bookPrefill(RECENT[2]), { book_id: 'OLD2', name: 'OLD2', book_type: 'TRADING', entity: 'E2' });
});

t('сводка для карточки: настроена ли книга (по book_id или кодам), строка записи для «Изменить»', () => {
  const doc = { books: [{ book_id: 'X', codes: { '*': ['OLD2'] } }, { book_id: 'OLD1' }] };
  eq(SOB.summary(doc, RECENT, SF.configuredIds(doc)).map(x => [x.r.book_id, x.configured, x.row]), [['OLD1', true, 1], ['OLD2', true, -1]]);
  eq(SOB.summary({}, RECENT, {}).map(x => x.configured), [false, false]);
  assert.strictEqual(SOB.card(null, RECENT, { slot() { throw new Error('нет книг — не открывать'); }, unslot() {} }, SF), null);
});

console.log(n + ' tests passed');
