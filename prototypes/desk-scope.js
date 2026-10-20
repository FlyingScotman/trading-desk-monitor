/*
 * Фильтр Desk Monitor на остальных страницах (Widget Board, Pivot Studio, Scenario Lab): юрлицо ∩ деск ∩ портфель ∩
 * книги ∩ сегмент ∩ валюта из localStorage fidm.sel.v1, который пишет Desk Monitor. DESK.scope.apply() в начале кода
 * страницы оставляет в window.DESK только позиции, фондирование и книги выбора; страница считает всё по ним.
 * Правила те же, что у applySel Desk Monitor: применяются только значения, которые есть в прогоне; сохранённое
 * сочетание, пустое в этом прогоне, ослабляется (портфель, затем деск, юрлицо); выбранные книги вне юрлица/деска/
 * портфеля не считаются, а не осталось ни одной — фильтр по книгам не действует. Портфель фильтрует позиции, книги —
 * те, где такие позиции есть. История (D.history) остаётся по всему деску.
 * Строка навигации (desk-nav.js) показывает действующий фильтр и снимает его: DESK.scope.clear().
 */
(function () {
  'use strict';
  var D = window.DESK, KEY = 'fidm.sel.v1';
  if (!D) return;

  function strs(a) { return Array.isArray(a) ? a.filter(function (x) { return typeof x === 'string'; }) : []; }
  function uniq(a) { return a.filter(function (x, i) { return x != null && a.indexOf(x) === i; }); }
  function has(a, x) { return a.indexOf(x) >= 0; }
  var portOf = function (p) { return p.portfolio || '—'; };
  var segOf = function (p) { return p.segment || '—'; };

  function read() {
    try { var o = JSON.parse(localStorage.getItem(KEY) || 'null'); return o && typeof o === 'object' ? o : {}; } catch (e) { return {}; }
  }

  function apply() {
    var sel = read(), P = D.positions, books = Object.keys(D.BOOKS);
    var present = function (ids, all) { return strs(ids).filter(function (x) { return has(all, x); }); };
    var ents = present(sel.entities, uniq(books.map(function (b) { return D.BOOKS[b].entity; })));
    var desks = present(sel.desks, uniq(books.map(function (b) { return D.BOOKS[b].desk; })));
    var ports = present(sel.portfolios, uniq(P.map(portOf)));
    var pick = present(sel.books, books);
    var segs = present(sel.segments, uniq(['Гос', 'Корп', 'Секьюритизация', 'Деривативы', 'FX'].concat(P.map(segOf))));
    var ccy = present(sel.ccys, uniq(['RUB'].concat(Object.keys(D.FX || {}), P.map(function (p) { return p.ccy; }))))[0];
    var bookPorts = {};
    P.forEach(function (p) { (bookPorts[p.book] = bookPorts[p.book] || []).push(portOf(p)); });
    var facet = function () {
      return books.filter(function (b) {
        var x = D.BOOKS[b];
        return (!ents.length || has(ents, x.entity)) && (!desks.length || has(desks, x.desk))
          && (!ports.length || (bookPorts[b] || []).some(function (v) { return has(ports, v); }));
      });
    };
    if (!facet().length) ports = [];
    if (!facet().length) desks = [];
    if (!facet().length) ents = [];
    var f = facet(), pk = f.filter(function (b) { return has(pick, b); }), bk = pk.length ? pk : f;
    var scope = { active: bk.length < books.length || ports.length > 0 || segs.length > 0 || !!ccy, total: P.length };
    D.scope.state = scope;
    if (!scope.active) return scope;

    D.positions = P.filter(function (p) {
      return has(bk, p.book) && (!ports.length || has(ports, portOf(p))) && (!segs.length || has(segs, segOf(p))) && (!ccy || p.ccy === ccy);
    });
    D.funding = (D.funding || []).filter(function (x) { return has(bk, x.book) && (!ccy || x.ccy === ccy); });
    var all = D.BOOKS;
    // в списках книг (Object.keys) — только книги выбора; остальные не перечисляются, но читаются по id: сохранённые
    // настройки виджетов и видов могут ссылаться на книгу вне фильтра
    D.BOOKS = {};
    books.forEach(function (b) {
      if (has(bk, b)) D.BOOKS[b] = all[b];
      else Object.defineProperty(D.BOOKS, b, { value: all[b], enumerable: false });
    });
    if (D.fundingPnlByBook) {  // итог фондирования уровня книг — по книгам выбора
      var fp = { carry: 0, fx: 0, fxCbr: 0 };
      bk.forEach(function (b) {
        var x = D.fundingPnlByBook[b];
        if (x) { fp.carry += x.carry || 0; fp.fx += x.fx || 0; fp.fxCbr += x.fxCbr != null ? x.fxCbr : x.fx || 0; }
      });
      D.fundingPnl = fp;
    }
    var parts = [].concat(ents, desks, ports.map(function (v) { return v === '—' ? 'без портфеля' : v; }),
      pk.length ? [pk.length === 1 ? all[pk[0]].name : 'книг: ' + pk.length] : [], segs, ccy ? [ccy] : []);
    scope.label = parts.join(' · ');
    scope.n = D.positions.length;
    return scope;
  }

  function clear() {
    try { localStorage.removeItem(KEY); } catch (e) {}
    location.reload();
  }

  D.scope = { apply: apply, clear: clear, state: { active: false } };
})();
