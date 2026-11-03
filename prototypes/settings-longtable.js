/*
 * Длинные таблицы на странице настроек (TDM#552): таблица длиннее нескольких десятков строк не растягивает страницу.
 * Показаны сводка («N строк») и первые строки; «Показать все», поиск по строкам и «Скачать CSV» (файл собирается в
 * браузере из того, что уже на странице; на сервер ничего не уходит). Короткая таблица — как раньше, целиком.
 *   DeskLongTable.el({columns, rows, h, cls, title, limit, numeric}) → элемент.
 * Чистая часть (DeskLongTable._test: filter, csv, fold) проверяется tests/js/settings_longtable.test.js.
 */
(function (root) {
  'use strict';
  var L = {};
  L.FOLD = 30;   // длиннее — свёрнута
  L.HEAD = 10;   // столько строк видно в свёрнутой

  /* ---------- чистая часть ---------- */
  L.norm = function (s) { return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е').replace(/\s+/g, ' ').trim(); };
  // поиск: сначала запрос целиком с начала слова («тип 03» — не «подтип 03»); не нашлось ни одной строки — каждое слово
  // запроса с начала слова в любой ячейке
  var esc = function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); };
  var at = function (w) { return new RegExp('(^|[^0-9a-zа-я])' + esc(w)); };
  L.filter = function (rows, q) {
    var nq = L.norm(q), words = nq.split(' ').filter(Boolean);
    if (!words.length) return rows.slice();
    var lines = rows.map(function (r) { return L.norm((r || []).join(' \u0001 ')); });
    var ph = at(nq), hit = rows.filter(function (r, i) { return ph.test(lines[i]); });
    if (hit.length) return hit;
    var res = words.map(at);
    return rows.filter(function (r, i) { return res.every(function (re) { return re.test(lines[i]); }); });
  };
  // что видно: все строки (открыта или короткая), или первые HEAD
  L.fold = function (n, open, limit) {
    var lim = limit || L.FOLD;
    return { folded: n > lim, show: open || n <= lim ? n : Math.min(n, L.HEAD) };
  };
  L.cell = function (v) {
    var s = v == null ? '' : String(v);
    return /[";\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  // CSV для Excel: «;» и BOM — кириллица открывается без мастера импорта
  L.csv = function (columns, rows) {
    return '﻿' + [columns].concat(rows).map(function (r) { return (r || []).map(L.cell).join(';'); }).join('\r\n') + '\r\n';
  };
  L.fileName = function (title) {
    var base = L.norm(title || 'таблица').replace(/[^0-9a-zа-я_-]+/gi, '_').replace(/^_+|_+$/g, '').slice(0, 60) || 'таблица';
    return base + '.csv';
  };
  var plural = function (n, one, few, many) {
    var a = n % 10, b = n % 100;
    return n.toLocaleString('ru-RU') + ' ' + (a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 10 || b >= 20) ? few : many);
  };
  L.plural = plural;

  /* ---------- DOM ---------- */
  function download(name, text) {
    var url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' })), a = document.createElement('a');
    a.href = url; a.setAttribute('download', name); document.body.append(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 10000);  // как «Скачать» диагностики
  }
  // opts: {columns, rows, h, cls ('k-tbl at-t'), title (имя файла), limit, td(c, i) → {class, text|node}}
  L.el = function (o) {
    var h = o.h, cols = o.columns || [], rows = o.rows || [], lim = o.limit || L.FOLD;
    var cellOf = o.td || function (c) { return { text: c == null ? '—' : String(c) }; };
    var tbody = h('tbody', {}), wrap = h('div', { class: 'k-tbl-wrap' }, h('table', { class: o.cls || 'k-tbl at-t' },
      h('thead', {}, h('tr', {}, cols.map(function (c) { return h('th', {}, c); }))), tbody));
    if (rows.length <= lim) { draw(rows, rows.length); return h('div', { class: 'lt' }, wrap); }
    var st = { open: false, q: '' };
    var info = h('span', { class: 'mut3 lt-n' });
    var more = h('button', { class: 'lnk lt-more', type: 'button', 'aria-expanded': 'false' });
    var find = h('input', { class: 'inp lt-q', type: 'search', placeholder: 'поиск по строкам', 'aria-label': 'Поиск по строкам таблицы', autocomplete: 'off' });
    var csv = h('button', { class: 'ibtn lt-csv', type: 'button', title: 'Файл собирается здесь, из строк таблицы' }, 'Скачать CSV');
    function draw(list, show) {
      tbody.textContent = '';
      list.slice(0, show).forEach(function (r, ri) {
        tbody.append(h('tr', {}, (r || []).map(function (c, i) {
          var x = cellOf(c, i, r, ri);
          return h('td', { class: x.class || null }, x.node != null ? x.node : x.text);
        })));
      });
    }
    function redraw() {
      var list = L.filter(rows, st.q), f = L.fold(list.length, st.open || !!st.q && list.length <= lim, lim);
      draw(list, f.show);
      info.textContent = (st.q ? 'найдено ' + plural(list.length, 'строка', 'строки', 'строк') + ' из ' + rows.length.toLocaleString('ru-RU')
        : plural(rows.length, 'строка', 'строки', 'строк')) + (f.show < list.length ? ', показаны первые ' + f.show : '');
      more.hidden = !f.folded && !st.open;
      more.textContent = st.open ? 'Свернуть' : 'Показать все (' + list.length.toLocaleString('ru-RU') + ')';
      more.setAttribute('aria-expanded', st.open ? 'true' : 'false');
    }
    more.addEventListener('click', function () { st.open = !st.open; redraw(); if (!st.open) bar.scrollIntoView({ block: 'nearest' }); });
    find.addEventListener('input', function () { st.q = find.value; redraw(); });
    csv.addEventListener('click', function () { download(L.fileName(o.title), L.csv(cols, L.filter(rows, st.q))); });
    var bar = h('div', { class: 'lt-bar' }, info, find, csv);
    redraw();
    return h('div', { class: 'lt lt-long' }, bar, wrap, h('div', { class: 'lt-foot' }, more));
  };

  L._test = L;
  root.DeskLongTable = L;
})(typeof window !== 'undefined' ? window : globalThis);
