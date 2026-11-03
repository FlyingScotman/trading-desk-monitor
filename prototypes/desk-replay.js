/*
 * «Перечитать отчёты» (TDM#553): кнопка вместо `desk.bat inbox replay`. Источник — из списка адаптера (с датой его
 * последнего принятого отчёта), период — по умолчанию прошлый рабочий день (кнопки «прошлый рабочий день» и «последний
 * отчёт источника»), оценка объёма («будет перечитано N файлов»), запуск в фоне с ходом и итогом. Если сервер как раз
 * читает папки, перечитывание ждёт и запускается после него («идёт чтение папок, запущу после него»), а не падает.
 * Новые ревизии отчётов автообновление пересчитывает само (затронутые даты и сегодняшний расчёт).
 * Данные: GET/POST /api/v1/inbox/replay (api/replay_routes.py; только с этого компьютера или администратору — другим
 * пользователям блок не показывается).
 * Встраивание: DeskReplay.mount(элемент, {api?, base?, toast?, source?, box?}) — box: обёртка, которую блок прячет,
 * когда перечитывание этому пользователю недоступно (по умолчанию сам элемент); api(method, url, body) -> Promise — как на
 * странице настроек; без него — свой fetch с префиксом base (на страницах concepts/ — '../'). Элементов с атрибутом
 * data-desk-replay может быть несколько. Стили — свои, с префиксом rp-.
 * Чистая часть (DeskReplay._test) проверяется tests/js/desk_replay.test.js.
 */
(function (root) {
  'use strict';
  var R = {};
  var POLL_MS = 1500;

  /* ---------- чистая часть ---------- */
  R.dm = function (s) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] + '.' + m[1] : '—'; };
  R.files = function (n) {
    n = n || 0;
    var m10 = n % 10, m100 = n % 100;
    return n + ' ' + (m10 === 1 && m100 !== 11 ? 'файл' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'файла' : 'файлов');
  };
  R.label = function (s) {
    return (s.names && s.names.length ? s.names.join(', ') : s.source) + ' · ' + s.source + (s.latest ? ' · последний отчёт ' + R.dm(s.latest) : ' · отчётов ещё нет');
  };
  R.estimate = function (est) {
    if (!est) return '';
    if (!est.files) return 'За этот период принятых файлов этого источника нет: перечитывать нечего.';
    var d = est.dates || [];
    return 'Будет перечитано: ' + R.files(est.files) + (d.length ? ' (' + (d.length === 1 ? 'за ' + R.dm(d[0]) : 'с ' + R.dm(d[0]) + ' по ' + R.dm(d[d.length - 1])) + ')' : '') + '.';
  };
  R.period = function (from, to, maxDays) {
    if (!from || !to) return 'Укажите обе даты.';
    if (from > to) return 'Дата «с» позже даты «по».';
    var days = (new Date(to) - new Date(from)) / 86400000;
    if (maxDays && days >= maxDays) return 'Не больше ' + maxDays + ' дней за раз.';
    return null;
  };
  // итог задания: {cls, text}
  R.result = function (j) {
    if (!j) return null;
    var req = j.request || {}, what = req.source + ' за ' + (req.from === req.to ? R.dm(req.from) : R.dm(req.from) + '–' + R.dm(req.to));
    if (j.state === 'running') return { cls: '', text: j.waiting ? 'Сервер сейчас читает папки — перечитаю ' + what + ' сразу после него…' : 'Перечитываю ' + what + '…' };
    if (j.state !== 'done') return { cls: 'bad', text: 'Не перечитано (' + what + '): ' + (j.error_code === 'busy' ? 'сервер долго был занят чтением папок, попробуйте позже' : j.error || j.error_code || 'ошибка') + '.' };
    var r = j.result || {}, files = r.files || {}, n = Object.keys(files).reduce(function (a, k) { return a + files[k]; }, 0);
    var bad = (files.rejected || 0) + (files.failed || 0) + (files.timed_out || 0);
    var text = 'Перечитано ' + what + ': ' + R.files(n) + ', новых ревизий — ' + (r.revisions || 0) + '.' +
      (r.revisions ? ' Затронутые расчёты пересчитает автообновление.' : ' Строки не изменились — расчёты прежние.') +
      (bad ? ' Не принято файлов: ' + bad + ' (подробнее — C14 «Статус дня»).' : '');
    return { cls: bad ? 'warn' : 'ok', text: text };
  };

  /* ---------- DOM ---------- */
  function h(tag, attrs) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'class') e.className = v; else if (k.slice(0, 2) === 'on') e.addEventListener(k.slice(2), v); else e.setAttribute(k, v === true ? '' : v);
    });
    for (var i = 2; i < arguments.length; i++) add(e, arguments[i]);
    return e;
  }
  function add(e, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(function (x) { add(e, x); }); return; }
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  var CSS = '.rp-box[hidden]{display:none!important}.rp-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 8px}.rp-row > label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--fg-2)}' +
    '.rp-src{min-width:0;max-width:100%;flex:1 1 260px}.rp-src select{width:100%}.rp-res{margin:8px 0 0;padding:8px 10px;border-radius:6px;border:1px solid var(--line-2,#ccc)}' +
    '.rp-res.ok{border-color:var(--pos);background:var(--pos-bg,transparent)}.rp-res.bad{border-color:var(--neg);background:var(--neg-bg,transparent)}.rp-res.warn{border-color:var(--warn);background:var(--warn-bg,transparent)}' +
    '.rp-go[disabled]{opacity:.55;cursor:default}.rp-est{font-size:12px;color:var(--fg-2);margin:0 0 8px}.rp-q{border:1px solid var(--line-2,#ccc);background:none;color:var(--accent);border-radius:999px;padding:1px 9px;font-size:11.5px;cursor:pointer}';
  function style() {
    if (document.getElementById('rp-style')) return;
    var s = document.createElement('style'); s.id = 'rp-style'; s.textContent = CSS; document.head.append(s);
  }
  function ownApi(base) {
    return function (method, url, body) {
      var opt = { method: method, headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' };
      if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
      return fetch((base || '') + url.replace(/^\//, ''), opt).then(function (r) {
        return r.json().catch(function () { return null; }).then(function (j) {
          if (!r.ok) { var e = new Error((j && j.message) || 'HTTP ' + r.status); e.status = r.status; throw e; }
          return j;
        });
      }, function (e) { throw new Error('сервер недоступен: ' + e.message); });
    };
  }

  function Widget(el, opts) {
    this.el = el; this.api = opts.api || ownApi(opts.base != null ? opts.base : '/');
    this.toast = opts.toast || null; this.want = opts.source || el.getAttribute('data-source') || null;
    this.box = opts.box || el;  // what to hide when the replay is not this user's (a panel around el)
    this.box.classList.add('rp-box');  // [hidden] wins over a panel's own display
    this.data = null; this.timer = null; this.estTimer = null;
  }
  Widget.prototype.load = function () {
    var w = this;
    return w.api('GET', '/api/v1/inbox/replay').then(function (r) { w.data = r; w.draw(); if (r.running) w.poll(); }, function (e) {
      if (e.status === 401 || e.status === 403) { w.box.hidden = true; return; }  // the replay is the admin's
      w.el.textContent = ''; w.el.append(h('div', { class: 'note k-note err' }, 'Перечитывание недоступно: ' + e.message));
    });
  };
  Widget.prototype.draw = function () {
    var w = this, r = w.data, el = w.el;
    el.textContent = ''; w.box.hidden = false;
    var srcs = r.sources || [];
    if (!srcs.length) { el.append(h('p', { class: 'mut' }, 'Адаптер не загружен или не объявляет источников — перечитывать нечего.')); return; }
    var pick = w.source && srcs.some(function (s) { return s.source === w.source; }) ? w.source
      : (w.want && srcs.some(function (s) { return s.source === w.want; }) ? w.want : (srcs.filter(function (s) { return s.latest; })[0] || srcs[0]).source);
    w.source = pick;
    w.from = w.from || r.default.from; w.to = w.to || r.default.to;
    var sel = h('select', { class: 'inp k-input', 'aria-label': 'Источник', onchange: function () { w.source = this.value; w.estimate(); } },
      srcs.map(function (s) { return h('option', { value: s.source, selected: s.source === pick }, R.label(s)); }));
    var from = h('input', { class: 'inp k-input mono', type: 'date', value: w.from, 'aria-label': 'С даты', onchange: function () { w.from = this.value; w.estimate(); } });
    var to = h('input', { class: 'inp k-input mono', type: 'date', value: w.to, 'aria-label': 'По дату', onchange: function () { w.to = this.value; w.estimate(); } });
    var set = function (a, b) { w.from = a; w.to = b; from.value = a; to.value = b; w.estimate(); };
    var latest = function () { var s = srcs.filter(function (x) { return x.source === w.source; })[0]; return s && s.latest; };
    el.append(h('div', { class: 'rp-row' }, h('label', { class: 'rp-src' }, 'Источник', sel)),
      h('div', { class: 'rp-row' }, h('label', {}, 'С даты', from), h('label', {}, 'По дату', to),
        h('span', { class: 'rp-row', style: 'margin:0' },
          h('button', { class: 'rp-q', type: 'button', onclick: function () { set(r.default.from, r.default.to); } }, 'прошлый рабочий день'),
          h('button', { class: 'rp-q', type: 'button', onclick: function () { var d = latest(); if (d) set(d, d); } }, 'последний отчёт источника'))));
    w.estEl = h('p', { class: 'rp-est', 'aria-live': 'polite' });
    w.btn = h('button', { class: 'btn k-btn rp-go', type: 'button', onclick: function () { w.start(); } }, 'Перечитать');
    w.resEl = h('div', { 'aria-live': 'polite' });
    el.append(w.estEl, h('div', { class: 'rp-row' }, w.btn, h('span', { class: 'mut3' }, 'Читает заново уже принятые файлы источника из архива монитора; новая ревизия — только там, где строки изменились.')), w.resEl);
    w.drawJob();
    w.estimate();
  };
  Widget.prototype.estimate = function () {
    var w = this;
    var why = R.period(w.from, w.to, w.data.max_days);
    if (why) { w.estEl.textContent = why; w.btn.disabled = true; return; }
    w.btn.disabled = !!w.data.running;
    clearTimeout(w.estTimer);
    w.estTimer = setTimeout(function () {
      w.api('GET', '/api/v1/inbox/replay?source=' + encodeURIComponent(w.source) + '&from=' + w.from + '&to=' + w.to).then(function (r) {
        w.estEl.textContent = r.store === false ? 'Хранилище недоступно: сколько файлов, не узнать.' : R.estimate(r.estimate);
        if (r.estimate && !r.estimate.files) w.btn.disabled = true;
      }, function (e) { w.estEl.textContent = 'Оценка недоступна: ' + e.message; });
    }, 150);
  };
  Widget.prototype.drawJob = function () {
    var w = this, j = w.data.job, x = R.result(j);
    w.resEl.textContent = '';
    if (x) w.resEl.append(h('div', { class: 'rp-res ' + x.cls }, x.text, j && j.finished_at ? h('span', { class: 'mut3' }, ' · ' + new Date(j.finished_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })) : null));
    if (w.btn) w.btn.disabled = !!w.data.running;
  };
  Widget.prototype.start = function () {
    var w = this;
    w.btn.disabled = true;
    w.api('POST', '/api/v1/inbox/replay', { source: w.source, from: w.from, to: w.to }).then(function (r) { w.data = r; w.drawJob(); w.poll(); }, function (e) {
      w.btn.disabled = false;
      w.resEl.textContent = '';
      w.resEl.append(h('div', { class: 'rp-res bad' }, e.status === 409 ? 'Перечитывание уже идёт — дождитесь его итога.' : 'Не запущено: ' + e.message));
    });
  };
  Widget.prototype.poll = function () {
    var w = this;
    clearTimeout(w.timer);
    w.timer = setTimeout(function () {
      w.api('GET', '/api/v1/inbox/replay').then(function (r) {
        var was = w.data && w.data.running;
        w.data.job = r.job; w.data.running = r.running; w.drawJob();
        if (r.running) w.poll();
        else if (was && w.toast && r.job) { var x = R.result(r.job); w.toast(x.text, x.cls === 'bad' ? 'err' : 'ok'); }
      }, function () { w.poll(); });
    }, POLL_MS);
  };

  R.mount = function (el, opts) {
    if (!el) return null;
    style();
    var w = new Widget(el, opts || {});
    w.load();
    return w;
  };
  R.mountAll = function (opts) {
    return Array.prototype.map.call(document.querySelectorAll('[data-desk-replay]'), function (el) { return R.mount(el, opts); });
  };

  R._test = R;
  root.DeskReplay = R;
})(typeof window !== 'undefined' ? window : globalThis);
