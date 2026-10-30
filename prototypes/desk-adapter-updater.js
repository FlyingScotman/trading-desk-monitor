/*
 * «Обновление адаптера» на странице настроек (#ad-upd; сервер: desk.updater.adapter, /api/v1/update/adapter*).
 * Приватный адаптер берётся из релизов закрытого репозитория. Сервер читает их с токеном GitHub (только чтение,
 * один репозиторий; хранится в папке данных, как токен хаба, и не возвращается: PUT api/v1/settings/github-token).
 * Без токена или если сервер до GitHub не достаёт — кнопка «Скачать через браузер»: браузер, где вы вошли в GitHub,
 * скачивает tda-adapter.zip в «Загрузки», сервер находит файл, проверяет его по списку файлов внутри архива и ставит.
 * Архив можно и перетащить на блок. Ставится только по нажатию; перезапуск не нужен.
 * window.DESK_ADAPTER_UPDATER: { panel(el), _test }.
 */
(function () {
  'use strict';
  var me = document.currentScript;
  var root = (me && me.dataset.root) || '';
  var ACTIVE = { starting: 1, downloading: 1, verifying: 1, waiting_browser: 1, installing: 1 };
  var SOURCE = {
    env: 'задан переменной окружения DESK_PRIVATE_ADAPTER',
    settings: 'папка из поля «Папка адаптера»',
    installed: 'установлен обновлением',
    bundle: 'из комплекта программы',
    reference: 'эталонный адаптер (CSV)'
  };
  var JOB = { starting: 'начинаю', downloading: 'скачивание сервером', verifying: 'проверка', waiting_browser: 'жду файл из браузера', installing: 'проверка и установка', done: 'установлен', failed: 'ошибка', cancelled: 'отменено' };
  var LAYER = { python: 'Python', curl: 'curl.exe', powershell: 'PowerShell', browser: 'браузер' };
  var TOKEN_HELP = 'Как получить токен: GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → ' +
    'Generate new token. Repository access: Only select repositories → trades-data-adapter. Permissions → Repository permissions → ' +
    'Contents: Read-only (остальное не нужно). Expiration — срок действия (например, 90 дней; потом создайте новый). ' +
    'Токен хранится только в папке данных этого компьютера и уходит только на api.github.com.';

  function el(tag, attrs) {
    var e = document.createElement(tag), kids = Array.prototype.slice.call(arguments, 2);
    Object.keys(attrs || {}).forEach(function (k) {
      var v = attrs[k];
      if (v == null || v === false) return;
      if (k === 'onclick') e.addEventListener('click', v); else e.setAttribute(k, v === true ? '' : v);
    });
    kids.forEach(function (c) { if (c != null && c !== false) e.append(c instanceof Node ? c : String(c)); });
    return e;
  }
  function api(method, path, body, raw) {
    var opt = { method: method, headers: { Accept: 'application/json' }, cache: 'no-store' };
    if (raw) { opt.headers['Content-Type'] = 'application/zip'; opt.body = raw; }
    else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(root + path, opt).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) {
        if (!r.ok) { var e = new Error((j && j.message) || ('HTTP ' + r.status)); e.status = r.status; e.details = j && j.details; throw e; }
        return j;
      });
    });
  }
  var mb = function (n) { return n == null ? '' : n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' КБ' : (n / 1048576).toFixed(1).replace('.', ',') + ' МБ'; };
  var when = function (s) { return s ? new Date(s).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : ''; };

  /* ---------- что показать (чистые функции, проверяются в tests/js) ---------- */
  function sourceLabel(src) { return SOURCE[src] || src || '—'; }
  function inUse(st) {
    var u = st.in_use || {};
    return (u.version ? 'версия ' + u.version + ', ' : '') + sourceLabel(st.source);
  }
  // какие кнопки показать: id → подпись
  function actions(st) {
    var out = [], running = !!(st.job && ACTIVE[st.job.state]), rel = st.release;
    if (running) { out.push(['cancel', 'Отменить']); return out; }
    out.push(['check', 'Проверить']);
    if (!st.pinned) {
      if (rel && st.update_available) out.push(['install', 'Установить ' + rel.version]);
      else if (rel && (st.compare === 'older' || st.compare === 'other')) out.push(['install-older', 'Установить ' + rel.version + ' всё равно']);
      out.push(['browser', 'Скачать через браузер']);
    }
    out.push(['scan', 'Найти в «Загрузках»']);
    var inst = st.installed;
    if (inst && inst.previous && st.previous_available) out.push(['rollback', 'Вернуть ' + inst.previous]);
    if (inst) out.push(['bundle', st.bundle ? 'Использовать адаптер из комплекта' : 'Не использовать установленный']);
    return out;
  }
  function checkLine(st) {
    var chk = st.check;
    if (!chk) return 'GitHub ещё не проверялся';
    var at = 'GitHub проверен ' + when(chk.at) + (chk.layer ? ' (' + (LAYER[chk.layer] || chk.layer) + ')' : '');
    if (chk.ok) return at + ': ' + (st.release ? 'последний выпуск ' + st.release.version : 'выпуска не видно');
    var e = chk.error || {};
    return at + ': ' + (e.what || e.code || 'не удалось') + (e.message ? ' — ' + e.message : '');
  }

  /* ---------- блок ---------- */
  var P = { box: null, st: null, timer: null, busy: false, scan: null, tokenOpen: false };

  function panel(box) {
    P.box = box || document.getElementById('ad-upd');
    if (!P.box) return;
    dropZone(P.box.closest('.card') || P.box);
    load();
  }
  function load() {
    return api('GET', 'api/v1/update/adapter').then(render, function (e) {
      P.box.textContent = ''; P.box.append(el('div', { 'class': 'err' }, 'Обновление адаптера: ' + e.message));
    });
  }
  function poll() {
    clearTimeout(P.timer);
    P.timer = setTimeout(function () { load().then(function () { if (P.st && P.st.job && ACTIVE[P.st.job.state]) poll(); else refreshAdapter(); }); }, 1500);
  }
  function refreshAdapter() { var b = document.getElementById('ad-refresh'); if (b) b.click(); }
  function flash(msg, cls) { var m = P.box.querySelector('.au-msg'); if (m) { m.className = 'au-msg ' + (cls || 'muted'); m.textContent = msg; } }
  function run(p, okMsg) {
    P.busy = true; if (P.st) render(P.st);
    return p.then(function (r) {
      P.busy = false;
      var st = r && r.state ? r.state : r && r.repo ? r : null;
      if (st) render(st); else load();
      if (r && r.message) flash(r.message, 'okmsg'); else if (okMsg) flash(okMsg, 'okmsg');
      if (r && (r.installed || r.state)) refreshAdapter();
    }, function (e) { P.busy = false; if (P.st) render(P.st); flash(e.message, 'err'); throw e; });
  }
  function quiet(p) { return p.catch(function () {}); }
  function browserDownload(url) {  // обычное скачивание: только сразу после нажатия кнопки
    var a = el('a', { href: url, rel: 'noopener' });
    document.body.appendChild(a); a.click(); a.remove();
  }
  // установка с подтверждением, если версия не новее (409 с details.needs_confirm)
  function confirmed(call, again) {
    return call().catch(function (e) {
      if (e.details && e.details.needs_confirm && confirm(e.message + '\n\nУстановить всё равно?')) return again();
      throw e;
    });
  }

  function doAction(id) {
    var st = P.st, rel = st.release;
    if (id === 'check') return quiet(run(api('POST', 'api/v1/update/adapter/check', {})));
    if (id === 'cancel') return quiet(run(api('POST', 'api/v1/update/adapter/cancel', {})));
    if (id === 'install' || id === 'install-older' || id === 'browser') {
      var older = id === 'install-older', via = id === 'browser' ? 'browser' : undefined;
      var what = rel ? 'адаптер ' + rel.version + (rel.asset ? ' (' + mb(rel.asset.size) + ')' : '') : 'последний выпуск адаптера';
      if (!confirm('Установить ' + what + '?\nАрхив проверяется по списку файлов внутри него и загрузчиком программы; прежний адаптер остаётся, если что-то не так. Перезапуск не нужен.' +
        (via ? '\n\nБраузер скачает tda-adapter.zip (нужно войти в GitHub в этом браузере); программа найдёт файл в «Загрузках».' : ''))) return;
      P.busy = true; render(st);
      return api('POST', 'api/v1/update/adapter/install', { allow_older: older, via: via }).then(function (r) {
        P.busy = false;
        if (r.job && r.job.via === 'browser' && r.job.browser_url) browserDownload(r.job.browser_url);  // ещё в пределах нажатия
        load().then(poll);
      }, function (e) { P.busy = false; render(st); flash(e.message, 'err'); });
    }
    if (id === 'scan') return quiet(run(api('GET', 'api/v1/update/adapter/downloads').then(function (j) { P.scan = j; return api('GET', 'api/v1/update/adapter'); })));
    if (id === 'rollback') {
      if (!confirm('Вернуть адаптер ' + st.installed.previous + '?')) return;
      return quiet(run(api('POST', 'api/v1/update/adapter/rollback', { to: 'previous' })));
    }
    if (id === 'bundle') {
      if (!confirm(st.bundle ? 'Использовать адаптер из комплекта программы (' + (st.bundle.version || 'версия неизвестна') + ')?\nУстановленные версии остаются, вернуться можно.'
        : 'Не использовать установленный адаптер? Будет работать эталонный (CSV), пока не установите адаптер снова.')) return;
      return quiet(run(api('POST', 'api/v1/update/adapter/rollback', { to: 'bundle' })));
    }
  }
  function installFound(it) {
    if (!confirm('Установить ' + it.file + ' (адаптер ' + it.version + ')?')) return;
    var body = { file: it.file, sha256: it.sha256 };
    quiet(run(confirmed(function () { return api('POST', 'api/v1/update/adapter/install-found', body); },
      function () { return api('POST', 'api/v1/update/adapter/install-found', Object.assign({}, body, { allow_older: true })); }))
      .then(function () { P.scan = null; }));
  }
  function upload(f) {
    if (!/\.zip$/i.test(f.name)) { flash('Нужен архив tda-adapter.zip со страницы выпуска адаптера.', 'err'); return; }
    flash('Загружаю и проверяю ' + f.name + ' (' + mb(f.size) + ')…');
    quiet(run(confirmed(function () { return api('POST', 'api/v1/update/adapter/upload', undefined, f); },
      function () { return api('POST', 'api/v1/update/adapter/upload?allow_older=1', undefined, f); })));
  }
  function dropZone(zone) {
    var over = function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) { e.preventDefault(); e.stopPropagation(); zone.style.outline = '2px dashed var(--accent)'; } };
    zone.addEventListener('dragenter', over);
    zone.addEventListener('dragover', over);
    zone.addEventListener('dragleave', function () { zone.style.outline = ''; });
    zone.addEventListener('drop', function (e) {
      zone.style.outline = '';
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      e.preventDefault(); e.stopPropagation();
      upload(f);
    });
  }
  function saveToken(input) {
    var token = input.value.trim();
    if (!token) { flash('Вставьте токен в поле.', 'err'); return; }
    quiet(run(api('PUT', 'api/v1/settings/github-token', { token: token }).then(function () { input.value = ''; return api('POST', 'api/v1/update/adapter/check', {}); }), 'Токен сохранён.'));
  }
  function removeToken() {
    if (!confirm('Удалить токен GitHub с этого компьютера?')) return;
    quiet(run(api('PUT', 'api/v1/settings/github-token', { token: '' }).then(function () { return api('GET', 'api/v1/update/adapter'); }), 'Токен удалён.'));
  }

  function jobBox(j) {
    if (!j) return null;
    var box = el('div', { 'class': j.state === 'failed' ? 'res bad' : j.state === 'done' ? 'res ok' : 'res', style: 'margin-top:8px' });
    var pct = j.total && j.bytes ? ' · ' + mb(j.bytes) + ' из ' + mb(j.total) : j.bytes ? ' · ' + mb(j.bytes) : '';
    box.append(el('div', {}, el('b', {}, (JOB[j.state] || j.state) + pct), j.layer ? el('span', { 'class': 'muted' }, ' · ' + (LAYER[j.layer] || j.layer)) : null));
    if (j.message) box.append(el('div', { 'class': j.state === 'failed' ? 'err' : 'muted', style: 'margin-top:4px' }, j.message));
    if (j.state === 'waiting_browser' && j.browser_url) {
      box.append(el('div', { 'class': 'row', style: 'margin-top:6px' },
        el('a', { 'class': 'btn', href: j.browser_url, target: '_blank', rel: 'noopener', style: 'text-decoration:none' }, 'Скачать через браузер'),
        el('span', { 'class': 'muted' }, 'tda-adapter.zip → папка ' + j.downloads_dir)));
      if (j.found && j.found.state === 'mismatch') box.append(el('div', { 'class': 'err' }, j.found.message));
      if (j.found && j.found.state === 'downloading') box.append(el('div', { 'class': 'muted' }, 'Браузер скачивает: ' + mb(j.found.size)));
    }
    if (j.state === 'failed' && j.needs_confirm && j.found_file) box.append(el('div', { 'class': 'muted' }, 'Чтобы установить эту версию всё равно: «Найти в «Загрузках»» → «Установить».'));
    (j.attempts || []).filter(function (a) { return !a.ok; }).forEach(function (a) {
      box.append(el('div', { 'class': 'muted', style: 'font-size:11.5px' }, (LAYER[a.layer] || a.layer || '') + ': ' + (a.what || a.code) + (a.message ? ' — ' + a.message : '') + (a.it_hint ? ' ' + a.it_hint : '')));
    });
    return box;
  }
  function scanBox() {
    var s = P.scan;
    if (!s) return null;
    var box = el('div', { 'class': 'res', style: 'margin-top:8px' });
    box.append(el('div', {}, el('b', {}, 'Папка загрузок: '), el('span', { 'class': 'mono' }, s.downloads_dir), s.exists ? '' : el('span', { 'class': 'err' }, ' — папки нет')));
    if (!s.items.length) box.append(el('div', { 'class': 'muted' }, 'Файлов tda-adapter*.zip там нет.'));
    s.items.forEach(function (it) {
      box.append(el('div', { 'class': 'row', style: 'margin-top:4px' },
        el('span', { 'class': 'mono', style: 'word-break:break-all' }, it.file), el('span', { 'class': 'muted' }, (it.version || '?') + ' · ' + mb(it.size) + ' · ' + when(it.modified)),
        it.ok ? el('span', { 'class': 'okmsg' }, 'архив проверен' + (it.is_release ? ', это выпуск с GitHub' : '')) : el('span', { 'class': 'err' }, it.reason || 'не проверен'),
        it.ok && !(P.st && P.st.pinned) ? el('button', { type: 'button', 'class': 'ibtn', disabled: P.busy, onclick: function () { installFound(it); } }, 'Установить') : null));
    });
    return box;
  }

  function render(st) {
    P.st = st;
    var b = P.box;
    b.textContent = '';
    var head = el('div', { 'class': 'row', style: 'gap:6px 14px;flex-wrap:wrap' });
    head.append(el('span', {}, el('b', {}, 'Используется: '), inUse(st)));
    if (st.installed) head.append(el('span', { 'class': 'muted' }, 'установлен выпуск ' + st.installed.version + (st.installed.installed_at ? ' (' + when(st.installed.installed_at) + ')' : '') + (st.installed.exists ? '' : ' — папки нет')));
    if (st.bundle) head.append(el('span', { 'class': 'muted' }, 'в комплекте: ' + (st.bundle.version || 'версия неизвестна')));
    b.append(head);
    if (st.explicit_note) b.append(el('div', { 'class': 'note warn', style: 'margin-top:6px' }, st.explicit_note));
    if (st.pinned_note) b.append(el('div', { 'class': 'note warn', style: 'margin-top:6px' }, st.pinned_note));

    var rel = st.release;
    var line = el('div', { style: 'margin-top:6px' }, el('a', { href: st.releases_page, target: '_blank', rel: 'noopener', 'class': 'mono' }, st.repo), ' ');
    if (rel) line.append(el('span', {}, 'последний выпуск ', el('span', { 'class': 'mono' }, rel.version), rel.published_at ? el('span', { 'class': 'muted' }, ' от ' + when(rel.published_at)) : null,
      st.compare ? el('span', { 'class': st.compare === 'newer' ? 'tag new' : 'tag', style: 'margin-left:6px' }, { newer: 'новее', same: 'используется', older: 'старее', other: 'другая сборка' }[st.compare] || st.compare) : null));
    b.append(line);
    var chk = st.check;
    b.append(el('div', { 'class': chk && !chk.ok ? 'err' : 'muted', style: 'margin-top:2px' }, checkLine(st)));
    if (chk && chk.ok && chk.note) b.append(el('div', { 'class': 'muted' }, chk.note));
    if (chk && !chk.ok && chk.error && chk.error.it_hint) b.append(el('div', { 'class': 'note', style: 'margin-top:4px' }, chk.error.it_hint));
    if (rel && rel.notes) b.append(el('div', { 'class': 'muted', style: 'margin-top:2px;white-space:pre-wrap' }, rel.notes));

    var row = el('div', { 'class': 'row', style: 'margin-top:8px;gap:6px;flex-wrap:wrap' });
    actions(st).forEach(function (a, i) {
      row.append(el('button', { type: 'button', 'class': a[0] === 'install' && i === 1 ? 'btn' : 'ibtn', disabled: P.busy || (a[0] === 'check' && !st.token_set), 'data-act': a[0],
        title: a[0] === 'check' && !st.token_set ? 'Без токена сервер не видит закрытый репозиторий: вставьте токен ниже или скачайте через браузер' : null,
        onclick: function () { doAction(a[0]); } }, a[1]));
    });
    var file = el('input', { type: 'file', accept: '.zip,application/zip', hidden: true });
    file.addEventListener('change', function () { if (file.files && file.files[0]) upload(file.files[0]); file.value = ''; });
    row.append(file, el('button', { type: 'button', 'class': 'ibtn', disabled: P.busy || st.pinned, onclick: function () { file.click(); } }, 'Загрузить tda-adapter.zip'),
      el('span', { 'class': 'mut3' }, 'или перетащите архив сюда'));
    b.append(row);
    b.append(el('div', { 'class': 'au-msg muted', style: 'margin-top:4px' }));
    var jb = jobBox(st.job); if (jb) b.append(jb);
    var sb = scanBox(); if (sb) b.append(sb);

    var det = el('details', { style: 'margin-top:10px' }, el('summary', {}, 'Токен GitHub: ' + (st.token_set ? 'задан' + (st.token_source === 'env' ? ' (переменной окружения DESK_GITHUB_TOKEN)' : '') : 'не задан')));
    det.open = P.tokenOpen || !st.token_set;
    det.addEventListener('toggle', function () { P.tokenOpen = det.open; });
    var input = el('input', { 'class': 'inp mono', type: 'password', autocomplete: 'off', placeholder: st.token_set ? 'новый токен — заменит сохранённый' : 'github_pat_…', style: 'min-width:320px' });
    det.append(el('div', { 'class': 'row', style: 'margin-top:6px;gap:6px;flex-wrap:wrap' }, input,
      el('button', { type: 'button', 'class': 'ibtn', disabled: P.busy, onclick: function () { saveToken(input); } }, 'Сохранить токен'),
      st.token_set && st.token_source === 'file' ? el('button', { type: 'button', 'class': 'ibtn', disabled: P.busy, onclick: removeToken }, 'Удалить токен') : null),
      el('p', { 'class': 'hint' }, TOKEN_HELP),
      el('p', { 'class': 'hint' }, 'Файл токена: ', el('span', { 'class': 'mono' }, st.token_file), '. Токен не показывается и не попадает в журналы и диагностику.'));
    b.append(det);
    if (st.job && ACTIVE[st.job.state]) poll();
  }

  window.DESK_ADAPTER_UPDATER = { panel: panel, _test: { actions: actions, inUse: inUse, checkLine: checkLine, sourceLabel: sourceLabel } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { panel(); }); else panel();
})();
