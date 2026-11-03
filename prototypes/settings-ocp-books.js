/*
 * Книги ОВП без позиций в настройках книг (TDM#556; владелец, 03.11.2026: «в ОВП отчётах есть старые книги, по которым
 * нет позиций, но которые содержат ОВП — хочу задать им юрлицо, тип книги и т. д.»).
 *
 * Сервер (GET /api/v1/adapter/settings-context, settings_routes.py) отдаёт в recent_books и книги отчёта ОВП последних
 * недель без позиций за то же время — с ocp_only: true. Форма книг (settings-forms.js) показывает их среди «не настроено»
 * с пометкой «только ОВП», а уже настроенные — той же пометкой в таблице; правятся они как обычные книги. Деск — выпадающим
 * списком из десков книг файла и сервера (L.desks), с «другим значением…».
 *
 * Отдельный модуль, чтобы экран настроек можно было переставлять, не трогая логику (поток settings, TDM#551):
 *   SOB.isOcpOnly(r)               — книга только из отчёта ОВП;
 *   SOB.ocpOnlyIds(recent)         — { book_id: true } таких книг;
 *   SOB.desks(books, recent)       — [{value, label}] для выпадающего списка деска;
 *   SOB.badge(h)                   — пометка «только ОВП» (DOM, h — конструктор элементов форм);
 *   SOB.unconfiguredText(r, fmt)   — пояснение строки «не настроено»: позиции на дату или ОВП на дату;
 *   SOB.groupTitle(miss)           — заголовок группы «не настроено»;
 *   SOB.summary(doc, recent, ids)  — книги ОВП и настроены ли они; SOB.card(form, recent, DS, SF) — карточка
 *                                    «Книги только из отчёта ОВП» в месте DeskSettings.slot('books-extra').
 * Чистая часть проверяется tests/js/settings_ocp_books.test.js без браузера.
 */
(function (root) {
  'use strict';
  var SOB = {};
  var PLACEHOLDER_DESKS = { AUTO: true, '—': true };  // заглушки авто-книг, не деск

  SOB.isOcpOnly = function (r) { return !!(r && r.ocp_only === true); };
  SOB.ocpOnlyIds = function (recent) {
    var out = {};
    (recent || []).forEach(function (r) { if (SOB.isOcpOnly(r) && r.book_id) out[r.book_id] = true; });
    return out;
  };
  SOB.desks = function (books, recent) {
    var seen = {}, out = [];
    function add(v, label) {
      if (typeof v !== 'string' || !v.trim() || PLACEHOLDER_DESKS[v] || seen[v]) return;
      seen[v] = true; out.push({ value: v, label: label || '' });
    }
    (books || []).forEach(function (b) { if (b) add(b.desk); });
    (recent || []).forEach(function (r) { if (r) add(r.desk, 'из справочника'); });
    return out.sort(function (a, b) { return String(a.value).localeCompare(String(b.value), 'ru'); });
  };
  SOB.badge = function (h) {
    return h('span', { class: 'badge', title: 'книга есть только в отчёте ОВП: позиций по ней за последние недели нет' }, 'только ОВП');
  };
  SOB.unconfiguredText = function (r, fmtDate) {
    var d = fmtDate ? fmtDate(r.last_date) : r.last_date;
    if (SOB.isOcpOnly(r)) return ' ОВП на ' + d + ', позиций нет';
    return ' позиции на ' + d + (r.positions != null ? ', ' + r.positions + ' ' + plural(r.positions, 'строка', 'строки', 'строк') : '');
  };
  SOB.groupTitle = function (miss) {
    var ocp = (miss || []).filter(SOB.isOcpOnly).length, n = (miss || []).length;
    return (ocp === n ? 'Есть в отчёте ОВП, не настроено — ' : ocp ? 'Есть в позициях или в отчёте ОВП, не настроено — ' : 'Есть в позициях, не настроено — ') + n;
  };
  // сводка книг ОВП для карточки: [{r, configured, row}] — книги ocp_only сервера; configured — запись в books.json
  // (по book_id или кодам книги, SF.configuredIds), row — индекс записи с тем же book_id (для «Изменить»)
  SOB.summary = function (doc, recent, configuredIds) {
    var ids = configuredIds || {}, books = (doc && doc.books) || [];
    return (recent || []).filter(SOB.isOcpOnly).map(function (r) {
      var row = -1;
      books.forEach(function (b, i) { if (row < 0 && b && b.book_id === r.book_id) row = i; });
      return { r: r, configured: !!ids[r.book_id], row: row };
    });
  };

  /*
   * Карточка «Книги только из отчёта ОВП» в месте DeskSettings.slot('books-extra') (раздел «Книги», TDM#551): список
   * таких книг с их юрлицом, типом и деском; «+ Добавить» — в books.json, как строка «не настроено», «Изменить» — карточка
   * записи. form — SF.Form вкладки books (её data, editRow, changed, render), SF — SETTINGS_FORMS, DS — DeskSettings.
   * Книг нет — место скрыто (DS.unslot). Вызывается страницей после каждой загрузки или правки форм (navForms).
   */
  SOB.card = function (form, recent, DS, SF) {
    if (!DS || !DS.slot || !SF) return null;
    var list = form && form.data ? SOB.summary(form.data, recent, SF.configuredIds(form.data)) : [];
    if (!list.length) { if (DS.unslot) DS.unslot('books-extra'); return null; }
    var el = DS.slot('books-extra');
    if (!el) return null;
    var h = SF.h, todo = list.filter(function (x) { return !x.configured; }).length, t = form.L.tables[0];
    var dash = function (v) { return v && v !== 'AUTO' ? v : '—'; };
    var rows = list.map(function (x) {
      var r = x.r, b = x.row >= 0 ? form.data.books[x.row] : r;
      var act = !x.configured
        ? h('button', { class: 'ibtn', type: 'button', onclick: function () {
            form.data.books = form.data.books || []; form.data.books.push(SF.bookPrefill(r)); form.changed(); form.render();
            form.editRow(t, form.data.books.length - 1);
          } }, '+ Добавить')
        : x.row >= 0 ? h('button', { class: 'lnk', type: 'button', onclick: function () { form.editRow(t, x.row); } }, 'Изменить') : null;
      return h('tr', { 'data-ocp-book': r.book_id },
        h('td', { class: 'mono' }, r.book_id),
        h('td', { class: 'mono' }, dash(b.entity)), h('td', {}, dash(b.book_type)), h('td', {}, dash(b.desk)),
        h('td', { class: 'mut' }, SF.fmtDate(r.last_date)),
        h('td', {}, x.configured ? h('span', { class: 'badge ok' }, 'настроено') : h('span', { class: 'badge warn' }, 'не настроено')),
        h('td', { class: 'sf-rowacts' }, act));
    });
    el.textContent = '';
    el.append(h('div', { class: 'card', id: 'ocp-books-card' },
      h('div', { class: 'hd' }, h('h2', {}, 'Книги только из отчёта ОВП'),
        h('span', { class: 'mut3' }, list.length + ' · ' + (todo ? 'не настроено ' + todo : 'все настроены'))),
      h('div', { class: 'bd' },
        h('p', { class: 'mut' }, 'Книги отчёта ОВП последних недель без позиций (например, старые книги, у которых осталась ОВП). Юрлицо, тип книги и деск отсюда идут в итоги ОВП по юрлицу и в выбор типа книги на экране «ОВП» со следующего расчёта.'),
        h('div', { class: 'k-tbl-wrap' }, h('table', { class: 'k-tbl sf-t' },
          h('thead', {}, h('tr', {}, ['Код книги', 'Юрлицо', 'Тип', 'Деск', 'ОВП на', '', ''].map(function (x) { return h('th', {}, x); }))),
          h('tbody', {}, rows))))));
    return el;
  };

  function plural(n, a, b, c) { var m = n % 10, mm = n % 100; return m === 1 && mm !== 11 ? a : m >= 2 && m <= 4 && (mm < 12 || mm > 14) ? b : c; }

  root.SETTINGS_OCP_BOOKS = SOB;
  if (typeof module !== 'undefined' && module.exports) module.exports = SOB;
})(typeof window !== 'undefined' ? window : this);
