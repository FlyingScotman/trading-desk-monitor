/*
 * Навигация страницы настроек (TDM#551): важное на виду, редкое — в «Дополнительно», поиск, подразделы.
 *  - Подразделы раздела: <nav class="subnav" data-subnav="<раздел>"> со ссылками #<раздел>/<подраздел> и панели
 *    <div class="sub" data-sub="<подраздел>">. Адрес подраздела открывается напрямую и переживает обновление страницы;
 *    без подраздела в адресе — последний открытый (помнит браузер), иначе первый.
 *  - «Дополнительно» в левой навигации: редкие разделы под кнопкой; открытость помнит браузер; раздел оттуда, открытый
 *    по ссылке или из сводки, раскрывает группу сам (это не запоминается); точка «требует внимания» видна на кнопке.
 *  - Поиск по настройкам: подписи полей, их ключи settings.json и подсказки, заголовки карточек, подразделы, поля форм
 *    адаптера — из того, что на странице (без сервера). Стрелки, Enter, Esc; «/» — в поиск.
 *  - Места для блоков других модулей: DeskSettings.slot(имя) открывает контейнер [data-slot] и его пункт навигации
 *    [data-slot-nav]; DeskSettings.status(ключ, пункт) — строка в сводке сверху; DeskSettings.dot(куда, cls, title).
 *    Прямая ссылка на место, которого ещё нет (#adapter/services, #ovp), не теряется: до slot() открывается запасной
 *    раздел или подраздел, адрес и запомненный выбор не меняются; slot() по тому же адресу открывает нужное.
 *  - DeskSettings.onShow(fn) — подписка на открытие раздела или подраздела: fn({sec, sub, hash}) вызывается сразу с
 *    текущим состоянием и при каждом показе (страница шлёт событие window 'desk-settings:shown' с тем же detail);
 *    возвращает функцию отписки. DeskSettings.unslot(имя) — скрыть место обратно (если оно открыто — откроется запасное).
 *  - Точка подраздела поднимается к пункту раздела в левом меню (N.rollup): bad или warn подраздела видны без захода;
 *    «изменено» (dirty) раздела не затирается.
 * Чистая часть (DeskSettingsNav._test) проверяется tests/js/settings_nav.test.js.
 */
(function (root) {
  'use strict';
  var N = {};

  /* ---------- чистая часть ---------- */
  // '#adapter/folders' → {id:'adapter', sub:'folders'}; '#dirs' → {id:'dirs', sub:null}
  N.split = function (hash) {
    var raw = String(hash || '').replace(/^#/, '');
    var m = /^([A-Za-z][\w-]*)\/([A-Za-z][\w-]*)$/.exec(raw);
    return m ? { id: m[1], sub: m[2] } : { id: raw, sub: null };
  };
  // какой подраздел открыть: из адреса, если он есть; иначе запомненный; иначе первый
  N.pickSub = function (asked, stored, available) {
    if (asked && available.indexOf(asked) >= 0) return asked;
    if (stored && available.indexOf(stored) >= 0) return stored;
    return available[0] || null;
  };
  N.norm = function (s) { return String(s == null ? '' : s).toLowerCase().replace(/ё/g, 'е').replace(/[«»"“”„]/g, '').replace(/\s+/g, ' ').trim(); };
  // items: [{label, path, extra}] → лучшие совпадения: все слова запроса есть в подписи, пути или ключе/подсказке
  N.search = function (items, q, limit) {
    var words = N.norm(q).split(' ').filter(Boolean);
    if (!words.length) return [];
    var out = [];
    items.forEach(function (it, i) {
      var lab = N.norm(it.label), all = lab + ' ' + N.norm(it.path) + ' ' + N.norm(it.extra);
      if (!words.every(function (w) { return all.indexOf(w) >= 0; })) return;
      var score = 0;
      words.forEach(function (w) {
        if (lab.indexOf(w) === 0) score += 6;
        else if (new RegExp('(^|[^a-zа-я0-9])' + w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).test(lab)) score += 4;
        else if (lab.indexOf(w) >= 0) score += 3;
        else if (N.norm(it.path).indexOf(w) >= 0) score += 1;
      });
      if (it.kind === 'field') score += 0.5;
      out.push({ it: it, score: score, i: i });
    });
    out.sort(function (a, b) { return b.score - a.score || a.i - b.i; });
    var seen = {};
    return out.filter(function (x) { var k = x.it.target + '|' + x.it.label; if (seen[k]) return false; seen[k] = 1; return true; })
      .slice(0, limit || 12).map(function (x) { return x.it; });
  };

  /* ---------- DOM ---------- */
  var O = null;  // {show, store, h}
  var STATUS = {}, statusHook = null;
  function $(id) { return document.getElementById(id); }
  function txt(el) {  // подпись без ключа settings.json и вложенных подсказок
    if (!el) return '';
    var c = el.cloneNode(true);
    Array.prototype.forEach.call(c.querySelectorAll('.key,.adv-n,.dot,.nb,.tag,button,input,select,textarea'), function (x) { x.remove(); });
    return c.textContent.replace(/\s+/g, ' ').trim();
  }

  // ---- подразделы ----
  N.subsOf = function (sec) {
    var nav = sec && sec.querySelector('.subnav');
    if (!nav) return [];
    return Array.prototype.filter.call(nav.querySelectorAll('a[data-sub-link]'), function (a) { return !a.hidden; })
      .map(function (a) { return a.getAttribute('data-sub-link'); });
  };
  // подраздел существует, но его пункт скрыт местом для модуля (data-slot-nav): адрес ждёт, пока модуль вызовет slot()
  N.pending = function (sec, sub) {
    var a = sec && sub && sec.querySelector('.subnav a[data-sub-link="' + sub + '"]');
    return !!(a && a.hidden && a.hasAttribute('data-slot-nav'));
  };
  function keepVisible(nav, a) {  // выбранный подраздел виден в полосе (телефон)
    if (nav.scrollWidth > nav.clientWidth && (a.offsetLeft < nav.scrollLeft || a.offsetLeft + a.offsetWidth > nav.scrollLeft + nav.clientWidth))
      nav.scrollLeft = Math.max(0, a.offsetLeft - 16);
  }
  // открыть подраздел; вернуть открытый (null — у раздела нет подразделов); {remember:false} — не запоминать выбор
  N.sub = function (sec, asked, opts) {
    var nav = sec && sec.querySelector('.subnav');
    if (!nav) return null;
    var name = sec.getAttribute('data-sec'), key = 'settings.sub.' + name;
    var sub = N.pickSub(asked, O.store.get(key), N.subsOf(sec));
    Array.prototype.forEach.call(sec.querySelectorAll('[data-sub]'), function (p) { p.classList.toggle('on', p.getAttribute('data-sub') === sub); });
    Array.prototype.forEach.call(nav.querySelectorAll('a[data-sub-link]'), function (a) {
      var on = a.getAttribute('data-sub-link') === sub;
      a.classList.toggle('on', on);
      if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
      if (on) keepVisible(nav, a);
    });
    if (sub && !(opts && opts.remember === false)) O.store.set(key, sub);
    return sub;
  };
  function subnavKeys(nav) {  // стрелки влево / вправо — между подразделами
    nav.addEventListener('keydown', function (e) {
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft' && e.key !== 'Home' && e.key !== 'End') return;
      var links = Array.prototype.filter.call(nav.querySelectorAll('a[data-sub-link]'), function (a) { return !a.hidden; });
      var i = links.indexOf(document.activeElement); if (i < 0) return;
      e.preventDefault();
      var j = e.key === 'Home' ? 0 : e.key === 'End' ? links.length - 1 : (i + (e.key === 'ArrowRight' ? 1 : -1) + links.length) % links.length;
      links[j].focus();
    });
  }

  // точка пункта раздела в левом меню: худшее из своей (data-base) и точек подразделов (bad / warn); dirty не затирается
  N.rollup = function (secName) {
    var d = document.querySelector('.st-nav .dot[data-nav="' + secName + '"]');
    var sec = document.querySelector('section.sec[data-sec="' + secName + '"]');
    if (!d || !sec || /\bdirty\b/.test(d.className)) return;
    var base = d.getAttribute('data-base') || '', worst = '', why = '';
    Array.prototype.forEach.call(sec.querySelectorAll('.subnav .dot'), function (x) {
      var c = /\bbad\b/.test(x.className) ? 'bad' : /\bwarn\b/.test(x.className) ? 'warn' : '';
      if (c && (!worst || (c === 'bad' && worst === 'warn'))) { worst = c; why = x.title || ''; }
    });
    var rank = { bad: 2, warn: 1 }, up = worst && (rank[worst] || 0) > (rank[base] || 0);
    d.className = 'dot ' + (up ? worst : base);
    if (up) d.title = why || 'в подразделе есть замечания';
    else if (d.hasAttribute('data-base-title')) d.title = d.getAttribute('data-base-title');
    N.moreDot();
  };

  // точка подраздела: базовый класс cls (undefined — только перерисовать) в data-base; «изменено» (панель помечена
  // data-dirty / data-form-dirty) её перекрывает, но не затирает
  N.paintSub = function (secName, sub, cls, title) {
    var a = document.querySelector('section.sec[data-sec="' + secName + '"] .subnav .dot[data-subdot="' + sub + '"]');
    if (!a) return;
    if (cls !== undefined) { a.setAttribute('data-base', cls || ''); a.title = title || ''; }
    var p = document.querySelector('section.sec[data-sec="' + secName + '"] [data-sub="' + sub + '"]');
    var dirty = p && (p.hasAttribute('data-dirty') || p.hasAttribute('data-form-dirty'));
    a.className = 'dot ' + (dirty ? 'dirty' : (a.getAttribute('data-base') || ''));
    N.rollup(secName);
    var cur = a.closest('.subnav') && a.closest('.subnav').querySelector('a.on');
    if (cur) keepVisible(cur.parentNode, cur);  // точка расширила пункт — выбранный не уезжает за край
  };
  // пометить панель подраздела «изменено»: kind 'dirty' (поля settings.json) или 'form-dirty' (формы адаптера)
  N.subDirty = function (panel, kind, on) {
    var sec = panel && panel.closest('section.sec');
    if (!sec) return;
    if (on) panel.setAttribute('data-' + kind, '1'); else panel.removeAttribute('data-' + kind);
    N.paintSub(sec.getAttribute('data-sec'), panel.getAttribute('data-sub'));
  };

  // ---- «Дополнительно» в навигации ----
  var MORE = 'settings.more';
  function moreOpen(open, remember) {
    var b = $('st-more'), l = $('st-more-l'); if (!b || !l) return;
    l.hidden = !open; b.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (remember) O.store.set(MORE, open ? '1' : '0');
  }
  N.moreDot = function () {  // точка на кнопке: в свёрнутой группе есть «требует внимания»
    var b = $('st-more'), l = $('st-more-l'); if (!b || !l) return;
    var d = b.querySelector('.dot'); if (!d) return;
    var dots = Array.prototype.map.call(l.querySelectorAll('.dot'), function (x) { return x.className; }).join(' ');
    d.className = 'dot ' + (/\bbad\b/.test(dots) ? 'bad' : /\bwarn\b/.test(dots) ? 'warn' : /\bdirty\b/.test(dots) ? 'dirty' : '');
  };
  // раздел открыт: если он в «Дополнительно» — группа раскрывается (не запоминается)
  N.shown = function (secName) {
    var l = $('st-more-l'); if (!l) return;
    if (l.querySelector('a[data-sec="' + secName + '"]') && l.hidden) moreOpen(true, false);
  };

  // ---- поиск ----
  function pathOf(el) {
    var sec = el.closest('section.sec'), parts = [];
    if (sec) { var h1 = sec.querySelector('.sec-h h1'); if (h1) parts.push(txt(h1)); }
    var sub = el.closest('[data-sub]');
    if (sub && sec) { var a = sec.querySelector('.subnav a[data-sub-link="' + sub.getAttribute('data-sub') + '"]'); if (a) parts.push(txt(a)); }
    var card = el.closest('.card');
    if (card) { var h2 = card.querySelector(':scope > .hd h2'); if (h2 && h2 !== el) parts.push(txt(h2)); }
    return parts.join(' › ');
  }
  var seq = 0;
  function idOf(el) { if (!el.id) el.id = 'st-t' + (++seq); return el.id; }
  function visibleFor(el) {
    if (el.closest('[hidden]')) return false;
    var sec = el.closest('section.sec'), a = sec && document.querySelector('.st-nav a[data-sec="' + sec.getAttribute('data-sec') + '"]');
    return !(a && a.hidden);  // раздел скрыт от этого пользователя (data-users-nav) или ждёт модуль — его полей в поиске нет
  }
  N.index = function () {
    var items = [];
    var add = function (kind, label, el, target, extra) {
      if (!label || !target || !visibleFor(el)) return;
      items.push({ kind: kind, label: label, path: pathOf(el), extra: extra || '', target: target });
    };
    // поля settings.json и прочих форм страницы: подпись (+ ключ, подсказка)
    Array.prototype.forEach.call(document.querySelectorAll('main .form > label, main .form > span'), function (l) {
      var cell = l.nextElementSibling;
      var ctl = (l.htmlFor && $(l.htmlFor)) || (cell && cell.querySelector('input:not([type=hidden]),select,textarea,button'));
      var key = l.querySelector('.key'), hint = cell && cell.querySelector('.hint');
      add('field', txt(l), l, ctl ? idOf(ctl) : idOf(l), (key ? key.textContent : '') + ' ' + (hint ? hint.textContent : ''));
    });
    // поля форм адаптера (settings-forms.js)
    Array.prototype.forEach.call(document.querySelectorAll('main .sf-grid > .sf-l'), function (l) {
      var cell = l.nextElementSibling, ctl = cell && cell.querySelector('input,select,textarea,button');
      add('field', txt(l), l, ctl ? idOf(ctl) : idOf(l));
    });
    // карточки, таблицы форм, действия адаптера
    Array.prototype.forEach.call(document.querySelectorAll('main .card > .hd h2, main .sf-th h4, main .at-action > .at-ah b'), function (t) {
      var box = t.closest('.card, .sf-table, .sf-obj, .at-action');
      var hint = box && box.querySelector(':scope > .hd .mut3');
      add('card', txt(t), t, idOf(box || t), hint ? hint.textContent : '');
    });
    // подразделы и разделы
    Array.prototype.forEach.call(document.querySelectorAll('.subnav a[data-sub-link]'), function (a) {
      if (a.hidden) return;
      add('sub', txt(a), a, (a.getAttribute('href') || '').replace(/^#/, ''), '');
    });
    Array.prototype.forEach.call(document.querySelectorAll('.st-nav a[data-sec]'), function (a) {
      if (a.hidden) return;
      var sec = document.querySelector('section.sec[data-sec="' + a.getAttribute('data-sec') + '"]'), p = sec && sec.querySelector('.sec-h p');
      items.push({ kind: 'sec', label: txt(a.querySelector('.lbl') || a), path: 'раздел', extra: p ? p.textContent : '', target: a.getAttribute('data-sec') });
    });
    return items;
  };
  N.go = function (target) {
    var el = $(target);
    try { history.pushState(null, '', '#' + target); } catch (e) { /* файл */ }  // сначала запись истории: show() не должен подменить текущую
    O.show('#' + target);
    if (el) {
      if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(el.tagName)) setTimeout(function () { try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); } }, 40);
      var box = el.closest('.form > div, .sf-grid > *, .card, .at-action') || el;
      box.classList.remove('st-hit'); void box.offsetWidth; box.classList.add('st-hit');
      setTimeout(function () { box.classList.remove('st-hit'); }, 2200);
    }
  };
  function mountSearch() {
    var q = $('st-q'), list = $('st-q-res');
    if (!q || !list) return;
    var res = [], cur = -1;
    function close() { list.hidden = true; q.setAttribute('aria-expanded', 'false'); q.removeAttribute('aria-activedescendant'); cur = -1; }
    function mark() {
      Array.prototype.forEach.call(list.children, function (li, i) { li.setAttribute('aria-selected', i === cur ? 'true' : 'false'); });
      var it = cur >= 0 && list.children[cur];
      if (!it) return;
      q.setAttribute('aria-activedescendant', it.id);
      // выбранный — в видимой части списка (прокрутка только списка, не страницы)
      if (it.offsetTop < list.scrollTop) list.scrollTop = it.offsetTop;
      else if (it.offsetTop + it.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = it.offsetTop + it.offsetHeight - list.clientHeight;
    }
    function pick(i) { var it = res[i]; if (!it) return; close(); q.blur(); N.go(it.target); }
    function draw() {
      res = N.search(N.index(), q.value, 12); cur = res.length ? 0 : -1;
      list.textContent = '';
      if (!N.norm(q.value)) { close(); return; }
      if (!res.length) list.append(O.h('div', { class: 'st-q-none' }, 'Ничего не нашлось. Попробуйте другое слово или ключ settings.json.'));
      res.forEach(function (it, i) {
        list.append(O.h('div', { class: 'st-q-it', role: 'option', id: 'st-q-o' + i, 'aria-selected': 'false',
          onmousedown: function (e) { e.preventDefault(); }, onclick: function () { pick(i); } },
          O.h('span', { class: 'st-q-l' }, it.label), O.h('span', { class: 'st-q-p' }, it.path)));
      });
      list.hidden = false; q.setAttribute('aria-expanded', 'true'); mark();
    }
    q.addEventListener('input', draw);
    q.addEventListener('focus', function () { if (N.norm(q.value)) draw(); });
    q.addEventListener('blur', function () { setTimeout(close, 120); });
    q.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (list.hidden) draw();
        if (!res.length) return;
        e.preventDefault(); cur = (cur + (e.key === 'ArrowDown' ? 1 : -1) + res.length) % res.length; mark();
      } else if (e.key === 'Enter') { e.preventDefault(); if (res.length) pick(cur < 0 ? 0 : cur); }
      else if (e.key === 'Escape') { if (!list.hidden) close(); else { q.value = ''; q.blur(); } }
    });
    document.addEventListener('keydown', function (e) {  // «/» — в поиск (не из поля ввода)
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return;
      var t = e.target; if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
      e.preventDefault(); q.focus(); q.select();
    });
  }

  // ---- места для блоков других модулей ----
  var API = {
    // открыть место name: контейнер [data-slot=name] и его пункт навигации [data-slot-nav=name]; вернуть контейнер
    slot: function (name) {
      var el = document.querySelector('[data-slot="' + name + '"]');
      if (!el) return null;
      el.hidden = false;
      Array.prototype.forEach.call(document.querySelectorAll('[data-slot-nav="' + name + '"]'), function (x) { x.hidden = false; });
      Array.prototype.forEach.call(document.querySelectorAll('.subnav a[data-sub-link].on'), function (a) { keepVisible(a.parentNode, a); });  // пункт вставлен слева — выбранный не уезжает
      // раздел или подраздел из адреса мог ждать этот блок (модуль загрузился после первого show): show() не трогал адрес
      var sec = el.closest('section.sec'), sub = el.closest('[data-sub]'), s = N.split(location.hash);
      if (O && sec && s.id === sec.getAttribute('data-sec') && (!sec.classList.contains('on') || (sub && s.sub === sub.getAttribute('data-sub') && !sub.classList.contains('on'))))
        O.show(location.hash);
      return el;
    },
    // скрыть место обратно: контейнер и пункты навигации; если оно было открыто — откроется запасное
    unslot: function (name) {
      var el = document.querySelector('[data-slot="' + name + '"]');
      if (!el) return;
      var sec = el.closest('section.sec'), sub = el.closest('[data-sub]');
      var open = !!(sec && sec.classList.contains('on') && (!sub || sub.classList.contains('on')) && !el.hidden);
      el.hidden = true;
      Array.prototype.forEach.call(document.querySelectorAll('[data-slot-nav="' + name + '"]'), function (x) { x.hidden = true; });
      if (!open || !O) return;
      var secName = sec.getAttribute('data-sec'), placeSec = !!document.querySelector('.st-nav a[data-sec="' + secName + '"][data-slot-nav="' + name + '"]');
      O.show('#' + secName);
      if (placeSec) { var cur = document.querySelector('section.sec.on'); try { history.replaceState(null, '', '#' + (cur ? cur.getAttribute('data-sec') : '')); } catch (e) { /* файл */ } }
    },
    // подписка на показ раздела / подраздела; fn({sec, sub, hash}) сразу с текущим состоянием; вернуть функцию отписки
    onShow: function (fn) {
      var cb = function (e) { fn(e.detail); };
      window.addEventListener('desk-settings:shown', cb);
      var sec = document.querySelector('section.sec.on'), sb = sec && sec.querySelector('.sub.on');
      fn({ sec: sec ? sec.getAttribute('data-sec') : null, sub: sb ? sb.getAttribute('data-sub') : null, hash: location.hash });
      return function () { window.removeEventListener('desk-settings:shown', cb); };
    },
    // строка в сводке сверху: {cls: 'ok'|'warn'|'bad'|'unk'|'', href, k, text, title}; null — убрать
    status: function (key, item) { if (item) STATUS[key] = item; else delete STATUS[key]; if (statusHook) statusHook(); },
    statusItems: function () { return Object.keys(STATUS).map(function (k) { return STATUS[k]; }); },
    // точка у раздела ('ovp') или подраздела ('adapter/services'): cls 'ok'|'warn'|'bad'|'dirty'|''
    dot: function (where, cls, title) {
      var s = N.split('#' + where);
      if (s.sub) { N.paintSub(s.id, s.sub, cls || '', title); N.moreDot(); return; }
      var d = document.querySelector('.st-nav .dot[data-nav="' + s.id + '"]');
      if (!d) return;
      d.className = 'dot ' + (cls || ''); d.title = title || '';
      d.setAttribute('data-base', cls || ''); d.setAttribute('data-base-title', title || '');
      N.rollup(s.id);
      N.moreDot();
    },
    show: function (hash) { if (O) O.show(hash); }
  };
  N.api = API;

  // opts: {show(hash), store {get,set}, h, onStatus()}; вызывается один раз, до первого show()
  N.init = function (opts) {
    O = opts; statusHook = opts.onStatus || null;
    Object.assign(API, { h: opts.h, apiCall: opts.api, toast: opts.toast });
    var b = $('st-more');
    if (b) {
      moreOpen(O.store.get(MORE) === '1', false);
      b.addEventListener('click', function () { moreOpen($('st-more-l').hidden, true); });
    }
    Array.prototype.forEach.call(document.querySelectorAll('.subnav'), subnavKeys);
    // повторный щелчок по открытому разделу или подразделу — не новая запись истории, а возврат наверх
    document.addEventListener('click', function (e) {
      if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      var a = e.target && e.target.closest && e.target.closest('.st-nav a[data-sec], .subnav a[data-sub-link]');
      var sec = document.querySelector('section.sec.on');
      if (!a || !sec) return;
      var same;
      if (a.hasAttribute('data-sub-link')) { var cur = sec.querySelector('.sub.on'); same = a.closest('section.sec') === sec && !!cur && cur.getAttribute('data-sub') === a.getAttribute('data-sub-link'); }
      else same = a.getAttribute('data-sec') === sec.getAttribute('data-sec');
      if (!same) return;
      e.preventDefault(); window.scrollTo(0, 0);
    });
    mountSearch();
  };

  N._test = N;
  root.DeskSettingsNav = N;
  root.DeskSettings = API;
})(typeof window !== 'undefined' ? window : globalThis);
