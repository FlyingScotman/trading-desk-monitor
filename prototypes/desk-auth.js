/*
 * Вход в Desk Monitor на страницах (пакет 1, п. 9; сервер: backend/src/desk/api/auth.py). Подключают desk-nav.js
 * (страницы с общей строкой и приложение /app/) и lib/kit.js (мониторы, K.top); сам ничего не требует.
 *
 *  - CSRF: к каждому изменяющему запросу этой страницы (fetch и XMLHttpRequest: POST, PUT, PATCH, DELETE на свой
 *    сервер) добавляет заголовок X-CSRF-Token из cookie desk_csrf — без него сервер с включённым входом запрос
 *    отклонит. Сама cookie сессии (desk_session) httpOnly, скрипту не видна.
 *  - Сессия кончилась (сервер ответил 401): переход на /login.html?next=<эта страница>.
 *  - Пользователь в шапке: «имя · роль», для администратора «Пользователи», «Сменить пароль», «Выйти». Встаёт в
 *    #desk-nav (desk-nav.js) или в .k-top (kit.js), иначе в правый верхний угол. Вход выключен (по умолчанию,
 *    только этот компьютер) — ничего не показывает.
 *  - Вход по HTTP в сети (allow_plain_http): жёлтая полоса-предупреждение вверху страницы.
 * window.DESK_AUTH: { ready: Promise<state>, state, logout() }; state — ответ GET /api/v1/auth/state.
 */
(function () {
  'use strict';
  if (window.DESK_AUTH || !/^https?:$/.test(location.protocol)) return;
  var SAFE = /^(GET|HEAD|OPTIONS)$/i, HEADER = 'X-CSRF-Token';
  var ROLE = { viewer: 'просмотр', trader: 'трейдер', admin: 'администратор' };
  var state = null, leaving = false;

  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function same(url) {
    try { return new URL(url, location.href).origin === location.origin; } catch (e) { return false; }
  }
  function toLogin() {
    if (leaving || /\/login\.html$/.test(location.pathname)) return;
    leaving = true;
    location.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search + location.hash);
  }
  function expired(url, status) {
    if (status === 401 && state && state.enabled && same(url) && !/\/api\/v1\/auth\//.test(new URL(url, location.href).pathname)) toLogin();
  }

  /* ---------- CSRF: fetch ---------- */
  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      var url = typeof input === 'string' ? input : (input && input.url) || String(input);
      var method = (init && init.method) || (input && typeof input === 'object' && input.method) || 'GET';
      var token = cookie('desk_csrf');
      if (token && !SAFE.test(method) && same(url)) {
        init = Object.assign({}, init || {});
        var h = new Headers(init.headers || (typeof Request !== 'undefined' && input instanceof Request ? input.headers : undefined));
        h.set(HEADER, token);
        init.headers = h;
      }
      return origFetch.call(this, input, init).then(function (r) { expired(url, r.status); return r; });
    };
  }
  /* ---------- CSRF: XMLHttpRequest ---------- */
  var X = window.XMLHttpRequest && window.XMLHttpRequest.prototype;
  if (X) {
    var open = X.open, send = X.send;
    X.open = function (method, url) { this.__desk = { m: method, u: url }; return open.apply(this, arguments); };
    X.send = function () {
      var d = this.__desk, token = cookie('desk_csrf');
      if (d && token && !SAFE.test(d.m) && same(d.u)) this.setRequestHeader(HEADER, token);
      if (d) this.addEventListener('load', function () { expired(d.u, this.status); });
      return send.apply(this, arguments);
    };
  }

  /* ---------- шапка ---------- */
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  var css = '.da-user{display:inline-flex;align-items:center;gap:6px;white-space:nowrap;font-size:12px;' +
    'color:var(--fg-2,var(--text-2,#4d5866))}' +
    '.da-user b{color:var(--fg,var(--text,#1b2129));font-weight:600}' +
    '.da-user a,.da-user button{font:inherit;color:var(--accent,#2f6fd6);background:none;border:0;padding:0 2px;cursor:pointer;text-decoration:none}' +
    '.da-user a:hover,.da-user button:hover{text-decoration:underline}' +
    '.da-user.float{position:fixed;top:6px;right:10px;z-index:60;background:var(--panel,var(--surface,#fff));' +
    'border:1px solid var(--line,#d9dee5);border-radius:3px;padding:2px 8px}' +
    '.da-warn{background:#fff3cd;color:#5c4400;border-bottom:1px solid #e0c060;padding:4px 16px;font:12px/1.4 system-ui,"Segoe UI",sans-serif}' +
    '@media (max-width:600px){.da-user{white-space:normal;flex-wrap:wrap;max-width:100%}}' +  // телефон: строка не шире экрана
    '@media print{.da-user,.da-warn{display:none}}';

  function widget(s, compact) {  // compact: шапка мониторов (.k-top), места мало — имя (смена пароля) и «Выйти»
    var u = s.user, box = el('span', { 'class': 'da-user', title: 'Вы вошли как ' + u.username + ' (' + (ROLE[u.role] || u.role) + ')' });
    var change = '/login.html?change=1&next=' + encodeURIComponent(location.pathname + location.search);
    box.appendChild(el('span', {}, '👤'));
    if (compact) {
      box.appendChild(el('a', { href: change, title: u.username + ' · ' + (ROLE[u.role] || u.role) + ' — сменить пароль' }, u.display_name || u.username));
    } else {
      box.appendChild(el('b', {}, u.display_name || u.username));
      box.appendChild(el('span', {}, '· ' + (ROLE[u.role] || u.role)));
      if (u.role === 'admin') box.appendChild(el('a', { href: '/settings.html#users', title: 'Пользователи, роли, журнал действий' }, 'Пользователи'));
      box.appendChild(el('a', { href: change, title: 'Сменить свой пароль' }, 'Пароль'));
    }
    var out = el('button', { type: 'button', title: 'Выйти из Desk Monitor' }, 'Выйти');
    out.addEventListener('click', logout);
    box.appendChild(out);
    return box;
  }
  function mount(s) {
    document.head.appendChild(el('style', {}, css));
    if (s.plain_http_lan) {
      var w = el('div', { 'class': 'da-warn', role: 'alert' },
        '⚠ Соединение без шифрования (HTTP): пароль и данные идут по сети открытым текстом. ' +
        'Администратору: включите HTTPS — desk lan enable --self-signed (или сертификат от IT).');
      document.body.insertBefore(w, document.body.firstChild);
    }
    if (!s.enabled || !s.user) return;
    var nav = document.querySelector('#desk-nav .dn-st'), top = document.querySelector('.k-top');
    if (nav) nav.appendChild(widget(s));
    else if (top) top.insertBefore(widget(s, true), top.querySelector('[data-a=search]'));
    else { var box = widget(s); box.className += ' float'; document.body.appendChild(box); }
  }
  function logout() {
    return origFetch.call(window, '/api/v1/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': cookie('desk_csrf') } })
      .catch(function () {}).then(function () { leaving = true; location.href = '/login.html'; });
  }

  var ready = origFetch.call(window, '/api/v1/auth/state', { cache: 'no-store', headers: { Accept: 'application/json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (s) {
      state = s;
      api.state = s;
      if (s) {
        if (s.enabled && !s.user) toLogin();
        else if (document.readyState !== 'loading') mount(s);
        else document.addEventListener('DOMContentLoaded', function () { mount(s); });  // после строки desk-nav
      }
      return s;
    }).catch(function () { return null; });
  var api = { ready: ready, state: null, logout: logout };
  window.DESK_AUTH = api;
})();
