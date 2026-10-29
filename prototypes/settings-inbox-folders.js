/*
 * Что найдено в папках с отчётами (TDM#445): под каждой папкой карточки «Папки с отчётами» — что входящие знают о её
 * файлах по последним просмотрам: сколько файлов, сколько адаптер узнал и по каким источникам (наборам), сколько не
 * узнал, сколько старше окна загрузки, сколько отклонено или с ошибкой чтения, дата последнего прочитанного отчёта
 * каждого набора; папка, которую сервер не видит, и папка, которую он ещё не просматривал.
 * Данные: GET /api/v1/inbox/folders (desk/daystatus.folders; только с этого компьютера или админу, как настройки).
 * Встраивание: DeskInboxFolders.node(путь, home) — элемент под полем папки; DeskInboxFolders.load() — запросить и
 * заполнить все такие элементы (после загрузки и после сохранения настроек). Папку страницы и папку сервера сравнивает
 * по пути без «\**», регистра и разделителя; относительный путь — от папки данных (home), как сервер.
 * Чистая часть (DeskInboxFolders._test) проверяется tests/js/settings_inbox_folders.test.js.
 */
(function (root) {
  'use strict';
  var F = {}, data = null, error = null;
  var ORDER = ['accepted', 'partially_accepted', 'superseded', 'rejected', 'failed', 'timed_out', 'match_failed', 'waiting_calendar', 'duplicate', 'stale_copy'];
  var BAD = { rejected: 1, failed: 1, timed_out: 1, match_failed: 1 };

  /* ---------- чистая часть ---------- */
  F.esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  F.date = function (s) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] + '.' + m[1] : F.esc(s || '—'); };
  F.when = function (s) { if (!s) return ''; var d = new Date(s); return isNaN(d) ? F.esc(s) : d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }); };
  // ключ папки: без «**» в конце, без разделителя в конце, «\» как «/», без регистра (Windows)
  F.key = function (p) { return String(p || '').trim().replace(/[\\/]\*\*$/, '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase(); };
  F.isAbs = function (p) { return /^([a-zA-Z]:)?[\\/]/.test(String(p || '').trim()) || /^~/.test(String(p || '').trim()); };
  // папка сервера для папки страницы: по пути как есть, относительный — от home
  F.find = function (doc, path, home) {
    if (!doc || !path || !String(path).trim()) return null;
    var want = [F.key(path)];
    if (home && !F.isAbs(path)) want.push(F.key(String(home).replace(/[\\/]+$/, '') + '/' + String(path).trim()));
    var fs = (doc.folders || []).filter(function (f) { return !f.output; });
    for (var i = 0; i < want.length; i++) for (var j = 0; j < fs.length; j++) if (F.key(fs[j].path) === want[i]) return fs[j];
    return null;
  };
  F.statuses = function (by, text) {
    var keys = ORDER.filter(function (k) { return by && by[k]; }).concat(Object.keys(by || {}).filter(function (k) { return ORDER.indexOf(k) < 0; }));
    return keys.map(function (k) { return '<span' + (BAD[k] ? ' class="bad"' : '') + '>' + F.esc((text && text[k]) || k) + ' ' + by[k] + '</span>'; }).join(', ');
  };
  // сводка одной папки: [{cls, html}] — первая строка итог, дальше источники, наборы, когда просмотрена
  F.lines = function (f, doc) {
    var text = (doc && doc.status_text) || {}, out = [];
    if (!f) {
      out.push({ cls: '', html: 'Сервер ещё не просматривал эту папку: сохраните настройки — файлы появятся здесь после очередного просмотра папок.' });
      return out;
    }
    if (!f.exists) out.push({ cls: 'bad', html: 'Сервер не видит эту папку: нет доступа или на сервере у неё другой путь.' });
    if (!f.files) {
      if (f.exists) out.push({ cls: 'warn', html: 'Файлов отчётов не найдено' + (f.recursive ? '' : ' (подпапки не просматриваются)') + '.' });
      return out;
    }
    var parts = ['найдено файлов: <b>' + f.files + '</b>', 'адаптер узнал: ' + f.matched];
    if (f.ignored) parts.push('<span class="warn">не узнал: ' + f.ignored + '</span>');
    if (f.too_old) parts.push('старше окна загрузки' + (doc && doc.lookback_days ? ' (' + doc.lookback_days + ' дн.)' : '') + ': ' + f.too_old);
    if (f.lost) parts.push('<span class="bad">отклонено или с ошибкой: ' + f.lost + '</span>');
    out.push({ cls: !f.matched ? 'warn' : '', html: parts.join(' · ') });
    (f.sources || []).forEach(function (s) {
      out.push({ cls: '', html: '<span class="mono">' + F.esc(s.source) + '</span>' + (s.datasets && s.datasets.length ? ' (' + F.esc(s.datasets.join(', ')) + ')' : '') +
        ': ' + F.statuses(s.by_status, text) });
    });
    if ((f.datasets || []).length) {
      out.push({ cls: 'ok', html: 'последний прочитанный отчёт: ' + f.datasets.map(function (d) { return F.esc(d.name || d.dataset) + ' — ' + F.date(d.latest_valid_date); }).join(', ') });
    } else if (f.matched) out.push({ cls: 'warn', html: 'ни один отчёт из этой папки не прочитан' });
    if (f.last_seen) out.push({ cls: 'mut3', html: 'последний новый файл замечен ' + F.when(f.last_seen) });
    return out;
  };
  F.html = function (f, doc) {
    return F.lines(f, doc).map(function (l) { return '<div' + (l.cls ? ' class="' + l.cls + '"' : '') + '>' + l.html + '</div>'; }).join('');
  };

  /* ---------- страница ---------- */
  var CSS = '.dir-found{font-size:12px;color:var(--fg-2);margin:4px 2px 0;line-height:1.45}.dir-found .ok{color:var(--fg-2)}' +
    '.dir-found .warn{color:var(--warn)}.dir-found .bad{color:var(--neg)}.dir-found .mut3{color:var(--fg-3)}.dir-found .mono{font-family:var(--mono)}';
  function style() {
    if (!root.document || root.document.getElementById('dir-found-css')) return;
    var s = root.document.createElement('style'); s.id = 'dir-found-css'; s.textContent = CSS; root.document.head.appendChild(s);
  }
  function fill(el) {
    if (error) { el.innerHTML = '<div class="mut3">что найдено в папке — не загрузилось: ' + F.esc(error) + '</div>'; return; }
    el.innerHTML = data ? F.html(F.find(data, el.dataset.path, el.dataset.home), data) : '';
  }
  F.node = function (path, home) {
    style();
    var el = root.document.createElement('div');
    el.className = 'dir-found'; el.dataset.foundDir = '1'; el.dataset.path = path || ''; el.dataset.home = home || '';
    if (path && String(path).trim()) fill(el);
    return el;
  };
  F.load = function () {
    return fetch('/api/v1/inbox/folders', { headers: { 'Accept': 'application/json' }, cache: 'no-store' }).then(function (r) {
      if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
      return r.json();
    }).then(function (d) { data = d; error = null; }, function (e) { error = e.message; }).then(function () {
      root.document.querySelectorAll('[data-found-dir]').forEach(function (el) { if (el.dataset.path.trim()) fill(el); });
    });
  };
  F._test = F;
  root.DeskInboxFolders = F;
})(window);
