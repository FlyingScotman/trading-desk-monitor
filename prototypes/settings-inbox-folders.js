/*
 * Что найдено в папках с отчётами (TDM#445): под каждой папкой карточки «Папки с отчётами» одна строка — что из неё
 * прочитано («Прочитано: ОВП за 27.10, Позиции за 28.10 · не распознано 3 · отклонено 1»); разбивка по наборам и
 * статусам — в «подробнее». Папка, которую сервер ещё не просматривал (только что сохранена), — «ещё не
 * просматривалась», и сводка запрашивается снова через период опроса; папка, которую сервер не видит; пустая папка;
 * папка, все файлы которой пропущены (старые по дате изменения, слишком большие).
 * Данные: GET /api/v1/inbox/folders (desk/daystatus.folders; только с этого компьютера или админу, как настройки).
 * Папку страницы сервер сопоставляет сам (entries: путь из settings.json → его папка, как сервер её раскрывает).
 * Встраивание: DeskInboxFolders.node(путь) — элемент под полем папки; .retarget(элемент, путь) — путь в поле изменили
 * до сохранения (сведения прежней папки не показываются); DeskInboxFolders.load() — запросить и заполнить все такие
 * элементы (после загрузки и после сохранения настроек); .onLoad(fn) — fn после каждого ответа; .summary(пути) —
 * для сводки настроек (#449): {missing: папок, которых сервер не видит, scanned: просмотренных, read: из скольких
 * что-то прочитано}, null — данных ещё нет.
 * Чистая часть (DeskInboxFolders._test) проверяется tests/js/settings_inbox_folders.test.js.
 */
(function (root) {
  'use strict';
  var F = {}, data = null, error = null, timer = null, polls = 0;
  var MAX_POLLS = 20;
  var ORDER = ['accepted', 'partially_accepted', 'superseded', 'rejected', 'failed', 'timed_out', 'match_failed', 'waiting_calendar', 'duplicate', 'stale_copy'];
  var BAD = { rejected: 1, failed: 1, timed_out: 1, match_failed: 1 };
  var SKIP = { too_old: 'старые по дате изменения', too_large: 'слишком большие' };

  /* ---------- чистая часть ---------- */
  F.esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  F.dm = function (s) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] : F.esc(s || '—'); };
  // папка сервера для папки страницы: по пути из settings.json, как его записала страница
  F.find = function (doc, path) {
    var p = String(path || '').trim();
    if (!doc || !p) return null;
    var e = (doc.entries || []).filter(function (x) { return String(x.path).trim() === p; })[0];
    if (!e) return null;
    return { entry: e, folder: e.folder == null ? null : (doc.folders || [])[e.folder] || null };
  };
  F.statuses = function (by, text) {
    var keys = ORDER.filter(function (k) { return by && by[k]; }).concat(Object.keys(by || {}).filter(function (k) { return ORDER.indexOf(k) < 0; }));
    return keys.map(function (k) { return '<span' + (BAD[k] ? ' class="bad"' : '') + '>' + F.esc((text && text[k]) || k) + ' ' + by[k] + '</span>'; }).join(', ');
  };
  // сводка папки: {cls, html, more} — одна строка и разбивка для «подробнее»; null — показывать нечего
  F.view = function (hit, doc) {
    if (!hit) return null;
    var f = hit.folder;
    if (!f) return { cls: 'mut3', html: doc.from_env ? 'не просматривается: папки заданы переменной окружения' : 'не просматривается' };
    if (!f.scanned) return { cls: 'mut3', html: 'ещё не просматривалась' + (doc.autopilot === false ? ' — автообновление выключено' : ''), wait: true };
    if (f.exists === false) return { cls: 'bad', html: 'сервер не видит папку: нет доступа или на сервере у неё другой путь' };
    var names = doc.dataset_names || {}, parts = [], skipped = 0, why = [];
    Object.keys(f.skipped || {}).forEach(function (k) { if (f.skipped[k]) { skipped += f.skipped[k]; why.push(SKIP[k] || k); } });
    if ((f.datasets || []).length) parts.push('Прочитано: ' + f.datasets.map(function (d) { return F.esc(d.name || names[d.dataset] || d.dataset) + ' за ' + F.dm(d.latest_valid_date); }).join(', '));
    else if (!f.files && !f.listed) parts.push('Папка пуста');
    else parts.push('Прочитанных отчётов нет');
    if (f.ignored) parts.push('<span class="warn">не распознано ' + f.ignored + '</span>');
    if (f.too_old) parts.push('старше окна загрузки ' + f.too_old);
    if (f.lost) parts.push('<span class="bad">отклонено ' + f.lost + '</span>');
    if (!f.files && skipped) parts.push('пропущено ' + skipped + ' (' + why.join(', ') + ')');
    var more = (f.sources || []).map(function (s) {
      var ds = (s.datasets || []).map(function (d) { return names[d] || d; }).join(', ') || s.source;
      return '<div>' + F.esc(ds) + ': ' + F.statuses(s.by_status, doc.status_text) + '</div>';
    }).join('');
    return { cls: f.datasets && f.datasets.length ? '' : 'warn', html: parts.join(' · '), more: more };
  };
  F.count = function (doc, paths) {
    if (!doc) return null;
    var out = { missing: 0, scanned: 0, read: 0 };
    (paths || []).forEach(function (p) {
      var hit = F.find(doc, p), f = hit && hit.folder;
      if (!f || !f.scanned) return;
      out.scanned++;
      if (f.exists === false) out.missing++;
      else if ((f.datasets || []).length) out.read++;
    });
    return out;
  };
  F.html = function (v) {
    if (!v) return '';
    return '<div' + (v.cls ? ' class="' + v.cls + '"' : '') + '>' + v.html + '</div>' +
      (v.more ? '<details><summary>подробнее</summary>' + v.more + '<div class="mut3">файлы по дням — <a href="concepts/c14-day-status.html">C14 «Состояние дня»</a></div></details>' : '');
  };

  /* ---------- страница ---------- */
  var CSS = '.dir-found{font-size:12px;color:var(--fg-2);margin:4px 2px 0;line-height:1.45}.dir-found .warn{color:var(--warn)}' +
    '.dir-found .bad{color:var(--neg)}.dir-found .mut3{color:var(--fg-3)}.dir-found summary{cursor:pointer;color:var(--fg-3)}';
  function style() {
    if (!root.document || root.document.getElementById('dir-found-css')) return;
    var s = root.document.createElement('style'); s.id = 'dir-found-css'; s.textContent = CSS; root.document.head.appendChild(s);
  }
  function fill(el) {
    if (error) { el.innerHTML = '<div class="mut3">что найдено в папке — не загрузилось: ' + F.esc(error) + '</div>'; return null; }
    var v = data ? F.view(F.find(data, el.dataset.path), data) : null;
    el.innerHTML = F.html(v);
    return v;
  }
  F.node = function (path) {
    style();
    var el = root.document.createElement('div');
    el.className = 'dir-found'; el.dataset.foundDir = '1'; el.dataset.path = el.dataset.saved = path || '';
    fill(el);
    return el;
  };
  F.retarget = function (el, path) {
    el.dataset.path = path || '';
    if (String(path || '').trim() === String(el.dataset.saved || '').trim()) fill(el);
    else el.innerHTML = '';  // сведения прежней папки — не об этой; новая покажется после сохранения
  };
  var listeners = [];
  F.onLoad = function (fn) { listeners.push(fn); };
  F.summary = function (paths) { return F.count(data, paths); };
  // запросить и заполнить; папка ещё не просмотрена — ещё раз через период опроса сервера (не чаще 5 с, до MAX_POLLS раз)
  F.load = function (again) {
    if (timer) { root.clearTimeout(timer); timer = null; }
    if (!again) polls = 0;
    return fetch('/api/v1/inbox/folders', { headers: { 'Accept': 'application/json' }, cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
      return r.json();
    }).then(function (d) { data = d; error = null; }, function (e) { error = e.message; }).then(function () {
      var wait = false;
      root.document.querySelectorAll('[data-found-dir]').forEach(function (el) {
        if (el.dataset.path.trim() !== String(el.dataset.saved || '').trim()) return;  // путь изменён, ещё не сохранён
        var v = fill(el); if (v && v.wait) wait = true;
      });
      listeners.forEach(function (fn) { try { fn(data); } catch (e) { /* сводка — не повод ломать папки */ } });
      if (wait && polls++ < MAX_POLLS) timer = root.setTimeout(function () { F.load(true); }, Math.max(5, data && data.poll_seconds != null ? data.poll_seconds : 30) * 1000);
    });
  };
  F._test = F;
  root.DeskInboxFolders = F;
})(window);
