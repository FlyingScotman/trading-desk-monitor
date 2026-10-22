/*
 * Обновление из GitHub Releases (сервер: desk.updater, /api/v1/update/remote*). Два режима, по атрибуту
 * data-mode тега <script>:
 *   nav   — значок «Новая версия …» в общей строке навигации (desk-nav.js подключает этот файл сам);
 *   panel — блок «Новая версия с GitHub» в разделе «Обновление программы» на странице настроек (#up-gh).
 *
 * Проверка: GET api/v1/update/remote (сервер сам спрашивает GitHub, если пора). Если сервер до GitHub не достаёт
 * (browser_check_due), страница спрашивает GitHub API из браузера: api.github.com отвечает на CORS-запросы
 * (Access-Control-Allow-Origin: *). Запрос «простой» — только заголовок Accept, без предварительного OPTIONS, без
 * cookie и токена — и ответ уходит серверу (POST …/browser-release), разбирает его сервер.
 * Сами файлы релиза браузер fetch()-ем не получит: github.com/…/releases/download отвечает 302 без CORS-заголовков,
 * хранилище release-assets.githubusercontent.com — тоже (проверено 22.10.2026). Поэтому кнопка запускает обычное
 * скачивание браузером, а сервер ждёт файл в папке «Загрузки», сверяет размер и SHA-256 с релизом и ставит.
 * window.DESK_UPDATER: { nav(), panel(el), _test }.
 */
(function () {
  'use strict';
  var me = document.currentScript;
  var mode = (me && me.dataset.mode) || 'nav';
  var root = (me && me.dataset.root) || '';
  if (window.DESK_UPDATER) { window.DESK_UPDATER.run(mode); return; }

  var BROWSER_KEY = 'desk.update.browser-check.v1', BROWSER_EVERY = 6 * 3600e3;
  var ACTIVE = { starting: 1, downloading: 1, verifying: 1, waiting_browser: 1, staging: 1 };

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
  function api(method, path, body) {
    var opt = { method: method, headers: { Accept: 'application/json' }, cache: 'no-store' };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    return fetch(root + path, opt).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) {
        if (!r.ok) throw new Error((j && j.message) || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  var mb = function (n) { return n == null ? '' : n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' КБ' : (n / 1048576).toFixed(1).replace('.', ',') + ' МБ'; };
  var when = function (s) { return s ? new Date(s).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : ''; };

  /* ---------- GitHub API из браузера ---------- */
  // «простой» CORS-запрос: GET, только Accept (CORS-safelisted), без cookie — preflight не нужен
  function ghFetch(url, f) {
    return (f || fetch)(url, { method: 'GET', headers: { Accept: 'application/vnd.github+json' }, credentials: 'omit', mode: 'cors', redirect: 'follow' });
  }
  function browserCheck(repo, f) {
    return ghFetch('https://api.github.com/repos/' + repo + '/releases/latest', f).then(function (r) {
      if (r.status === 404) return { none: true };
      if (!r.ok) {
        var lim = r.headers.get('x-ratelimit-remaining') === '0';
        return { error: lim ? 'лимит GitHub API на внешний адрес сети исчерпан (60 запросов в час)' : 'GitHub API ответил HTTP ' + r.status };
      }
      return r.json().then(function (j) { return { release: j }; });
    }, function (e) { return { error: 'GitHub API недоступен из браузера: ' + (e && e.message || e) }; });
  }
  function due(lastTs, now) { return !(lastTs > 0) || now - lastTs > BROWSER_EVERY; }
  function lastBrowserCheck() { try { return +localStorage.getItem(BROWSER_KEY) || 0; } catch (e) { return 0; } }
  function markBrowserCheck() { try { localStorage.setItem(BROWSER_KEY, String(Date.now())); } catch (e) {} }
  function browserReport(st, force) {  // браузер спрашивает GitHub, когда сервер не может; ответ — серверу
    if (!st || (!force && (!st.browser_check_due || !due(lastBrowserCheck(), Date.now())))) return Promise.resolve(st);
    markBrowserCheck();
    return browserCheck(st.repo).then(function (body) { return api('POST', 'api/v1/update/remote/browser-release', body); })
      .catch(function () { return st; });
  }
  function timed(p) {
    var t0 = performance.now();
    return p.then(function () { return { ok: true, ms: Math.round(performance.now() - t0) }; },
      function (e) { return { ok: false, ms: Math.round(performance.now() - t0), error: String(e && e.message || e) }; });
  }
  function browserDiag() {  // api: настоящий CORS-запрос; github и файлы: no-cors (видно только, что путь открыт)
    return Promise.all([
      timed(ghFetch('https://api.github.com/rate_limit').then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); })),
      timed(fetch('https://github.com/favicon.ico', { mode: 'no-cors', credentials: 'omit', cache: 'no-store' })),
      timed(fetch('https://release-assets.githubusercontent.com/', { mode: 'no-cors', credentials: 'omit', cache: 'no-store' }))
    ]).then(function (r) { return api('POST', 'api/v1/update/remote/browser-diag', { api: r[0], github: r[1], assets: r[2] }); });
  }
  // обычное скачивание браузером: переход по ссылке на файл релиза (ответ — вложение, страница остаётся на месте);
  // только сразу после нажатия кнопки, иначе — кнопка «Скачать через браузер» (её нажимает человек)
  function browserDownload(url) {
    var a = el('a', { href: url, rel: 'noopener' });
    document.body.appendChild(a); a.click(); a.remove();
  }

  /* ---------- значок в строке навигации ---------- */
  var navBox = null;
  function nav() {
    if (!/^https?:$/.test(location.protocol)) return;
    api('GET', 'api/v1/update/remote').then(function (st) {
      return st.auto_check ? browserReport(st) : st;
    }).then(function (st) {
      var bar = document.querySelector('#desk-nav .dn-st');
      if (!bar || !st || !st.update_available) return;
      if (navBox) navBox.remove();
      var settings = /settings\.html$/.test(location.pathname);
      navBox = el('a', { href: settings ? '#up' : root + 'settings.html#up', 'class': 'dn-upd',
        title: 'Доступна версия ' + st.release.version + ' (установлена ' + st.current_version + '). Настройки → «Обновление программы»',
        style: 'color:var(--n-acc);border:1px solid var(--n-acc);border-radius:2px;padding:0 6px;white-space:nowrap' },
        'Новая версия ' + st.release.version.split('-')[0]);
      bar.insertBefore(navBox, bar.firstChild);
    }).catch(function () {});
  }

  /* ---------- блок на странице настроек ---------- */
  var P = { box: null, st: null, timer: null, waiting: null, scan: null, busy: false, detOpen: false };
  var LAYER = { python: 'Python (сертификаты и прокси Windows)', curl: 'curl.exe (вход в прокси под пользователем Windows)', powershell: 'PowerShell (.NET: прокси/PAC и вход как у браузера)', browser: 'Браузер' };
  var JOB = { starting: 'начинаю', downloading: 'скачивание сервером', verifying: 'проверка контрольной суммы', waiting_browser: 'жду файл из браузера', staging: 'подготовка установки', restarting: 'перезапуск', ready: 'скачано', failed: 'ошибка', cancelled: 'отменено' };

  function panel(box) {
    P.box = box || document.getElementById('up-gh');
    if (!P.box) return;
    dropZone(document.getElementById('up') || P.box);
    load(true);
  }
  // zip, перетащенный на раздел, — то же, что «Загрузить zip с компьютера» (POST api/v1/update/upload)
  function dropZone(zone) {
    var over = function (e) { if (e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') >= 0) { e.preventDefault(); zone.style.outline = '2px dashed var(--accent)'; } };
    zone.addEventListener('dragenter', over);
    zone.addEventListener('dragover', over);
    zone.addEventListener('dragleave', function () { zone.style.outline = ''; });
    zone.addEventListener('drop', function (e) {
      zone.style.outline = '';
      var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (!f) return;
      e.preventDefault();
      if (!/\.zip$/i.test(f.name)) { flash('Нужен zip-архив DeskMonitor-….zip.', 'err'); return; }
      flash('Загружаю ' + f.name + ' (' + mb(f.size) + ')…');
      fetch(root + 'api/v1/update/upload?name=' + encodeURIComponent(f.name), { method: 'POST', headers: { 'Content-Type': 'application/zip', Accept: 'application/json' }, body: f })
        .then(function (r) { return r.json().catch(function () { return null; }).then(function (j) { if (!r.ok) throw new Error((j && j.message) || ('HTTP ' + r.status)); return j; }); })
        .then(function (j) {
          flash(j.compare === 'same' ? 'Загружен: эта версия уже установлена.' : j.error ? 'Загружен, но установить нельзя: ' + j.error
            : 'Загружен: версия ' + j.version + '. Нажмите «Установить» в списке ниже.', j.error && j.compare !== 'same' ? 'err' : 'okmsg');
          var refresh = document.getElementById('up-refresh'); if (refresh) refresh.click();
        }, function (err) { flash('Не загружен: ' + err.message, 'err'); });
    });
  }
  function load(first) {
    return api('GET', 'api/v1/update/remote').then(function (st) {
      render(st);
      if (first) return browserReport(st).then(function (st2) { if (st2 !== st) render(st2); });
    }).catch(function (e) { P.box.textContent = ''; P.box.append(el('div', { 'class': 'err' }, 'Обновление с GitHub: ' + e.message)); });
  }
  function poll() {
    clearTimeout(P.timer);
    P.timer = setTimeout(function () { load(false).then(function () { if (P.st && P.st.job && ACTIVE[P.st.job.state]) poll(); }); }, 2000);
  }

  function act(label, fn, primary, disabled, title) {
    return el('button', { type: 'button', 'class': primary ? 'btn' : 'ibtn', disabled: disabled || P.busy, title: title, onclick: fn }, label);
  }
  function run(p) {
    P.busy = true; if (P.st) render(P.st);
    return p.then(function (st) { P.busy = false; if (st && st.repo) render(st); else load(false); },
      function (e) { P.busy = false; if (P.st) render(P.st); flash(e.message, 'err'); });
  }
  function flash(msg, cls) { var m = P.box.querySelector('.ug-msg'); if (m) { m.className = 'ug-msg ' + (cls || 'muted'); m.textContent = msg; } }

  function checkNow() {
    return run(api('POST', 'api/v1/update/remote/check', {}).then(function (st) {
      return st.check && st.check.ok ? st : browserReport(st, true);
    }));
  }
  function install(allow) {
    var st = P.st, ch = st && st.choice;
    if (!ch || !(ch.asset || allow)) return;
    var how = '\nПрограмма скачает архив, сверит контрольную сумму с релизом и перезапустится (около минуты); окно start.bat не закрывайте.';
    if (!confirm(ch.asset ? 'Установить Desk Monitor ' + st.release.version + ' (' + (ch.asset.kind === 'patch' ? 'заплатка' : 'полный архив') + ', ' + mb(ch.asset.size) + ')?' + how
      : ch.reason + '\n\nВсё равно установить ' + st.release.version + ' (полный архив)?' + how)) return;
    P.busy = true; render(st);
    api('POST', 'api/v1/update/remote/install', { allow_downgrade: !!allow }).then(function (r) {
      P.busy = false;
      var j = r.job;
      if (j.via === 'browser' && j.browser_url) browserDownload(j.browser_url);  // ещё в пределах нажатия кнопки
      load(false).then(poll);
    }, function (e) { P.busy = false; render(st); flash(e.message, 'err'); });
  }
  function diagnose() {
    flash('Проверяю связь всеми способами (до 20 секунд)…');
    return run(api('POST', 'api/v1/update/remote/diagnose', {}).then(function () { return browserDiag(); }));
  }
  function scan() {
    return run(api('GET', 'api/v1/update/remote/downloads').then(function (j) { P.scan = j; return api('GET', 'api/v1/update/remote'); }));
  }
  function installFound(it) {
    var older = it.compare === 'older';
    if (!confirm('Установить ' + it.file + ' (версия ' + it.version + ')?' + (older ? '\n\nВнимание: эта версия СТАРЕЕ установленной.' : ''))) return;
    run(api('POST', 'api/v1/update/remote/install-found', { file: it.file, sha256: it.sha256, allow_downgrade: older }).then(function (r) {
      if (r.restarting) waitRestart(r.version || it.version);
      else flash(r.message || 'Архив скопирован в загруженные: установите его из списка ниже.', 'okmsg');
      return api('GET', 'api/v1/update/remote');
    }));
  }
  function rollback() {
    var v = P.st.rollback.version;
    if (!confirm('Вернуть предыдущую версию ' + v + '?\nФайлы восстановятся из резервной копии, программа перезапустится.')) return;
    run(api('POST', 'api/v1/update/rollback', {}).then(function (r) { waitRestart(r.version); return api('GET', 'api/v1/update/remote'); }));
  }
  function saveSettings(changes) {
    return run(api('PUT', 'api/v1/settings', changes).then(function () { return api('GET', 'api/v1/update/remote'); }));
  }
  function waitRestart(target) {  // как в разделе выше: ждём, пока сервер ответит новой версией
    if (P.waiting) return;
    var started = Date.now();
    P.waiting = setInterval(function () {
      fetch(root + 'api/v1/update', { headers: { Accept: 'application/json' }, cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }, function () { return null; }).then(function (d) {
        if (d && d.current_version === target && !d.pending && !d.applying) {
          clearInterval(P.waiting); flash('Установлена версия ' + target + '. Перезагружаю страницу…', 'okmsg');
          setTimeout(function () { location.reload(); }, 1500);
        } else if (d && !d.pending && !d.applying && d.last && d.last.version === target && !d.last.ok && new Date(d.last.at) >= new Date(started - 60000)) {
          clearInterval(P.waiting); P.waiting = null; flash(d.last.message || 'Обновление не удалось.', 'err');
        } else if (Date.now() - started > 15 * 60e3) {
          clearInterval(P.waiting); P.waiting = null; flash('Новая версия не ответила за 15 минут: смотрите окно start.bat и logs\\update.log.', 'err');
        }
      });
    }, 2000);
  }

  function cell(r) {
    if (!r) return el('td', { 'class': 'muted' }, '—');
    if (r.unavailable) return el('td', { 'class': 'muted' }, 'нет');
    return r.ok ? el('td', { 'class': 'okmsg', title: (r.status ? 'HTTP ' + r.status : '') + (r.proxy ? ' через ' + r.proxy : ' напрямую') }, '✓ ' + (r.ms != null ? r.ms + ' мс' : ''))
      : el('td', { 'class': 'err', title: r.message || r.error || '' }, '✗ ' + (r.what || r.error || r.code || 'ошибка'));
  }
  function diagTable(st) {
    var d = st.diagnose;
    var wrap = el('div', {});
    if (!d) { wrap.append(el('p', { 'class': 'muted' }, 'Диагностика ещё не запускалась.')); return wrap; }
    var t = el('table', { 'class': 't', style: 'margin-top:6px' });
    var hd = el('tr', {}, el('th', {}, 'Способ'));
    (d.targets || []).forEach(function (x) { hd.append(el('th', { title: x.url }, x.label)); });
    t.append(el('thead', {}, hd));
    var tb = el('tbody', {}), hints = {};
    Object.keys(d.layers || {}).forEach(function (la) {
      var row = el('tr', {}, el('td', {}, LAYER[la] || la, d.working && d.working.indexOf(la) >= 0 ? el('span', { 'class': 'tag', style: 'margin-left:6px' }, 'работает') : null));
      var L = d.layers[la];
      (d.targets || []).forEach(function (x) {
        var r = L.unavailable ? { unavailable: L.unavailable } : L[x.key];
        row.append(cell(r));
        if (r && !r.ok && r.code && r.it_hint) hints[r.code] = r;
      });
      tb.append(row);
    });
    if (d.browser) {
      var b = d.browser;
      tb.append(el('tr', {}, el('td', {}, LAYER.browser), cell(b.api), cell(b.github), cell(b.assets), el('td', { 'class': 'muted' }, '')));
    }
    t.append(tb);
    wrap.append(t, el('p', { 'class': 'hint' }, 'Проверено ' + when(d.at) + '. Строка «Браузер»: API — настоящий запрос; github.com и файлы — только «путь открыт» (скачивание файла браузером здесь не проверить).'));
    Object.keys(hints).forEach(function (k) {
      wrap.append(el('div', { 'class': 'note', style: 'margin-top:6px' }, el('b', {}, hints[k].what + ': '), hints[k].it_hint));
    });
    return wrap;
  }

  function jobBox(st) {
    var j = st.job;
    if (!j) return null;
    var cls = j.state === 'failed' ? 'res bad' : j.state === 'restarting' || j.state === 'ready' ? 'res ok' : 'res';
    var box = el('div', { 'class': cls });
    var pct = j.total && j.bytes ? ' · ' + mb(j.bytes) + ' из ' + mb(j.total) : j.bytes ? ' · ' + mb(j.bytes) : '';
    box.append(el('div', {}, el('b', {}, (JOB[j.state] || j.state) + pct), j.layer ? el('span', { 'class': 'muted' }, ' · ' + (LAYER[j.layer] || j.layer)) : null));
    if (j.message) box.append(el('div', { 'class': j.state === 'failed' ? 'err' : 'muted', style: 'margin-top:4px' }, j.message));
    if (j.state === 'waiting_browser' && j.browser_url) {
      box.append(el('div', { 'class': 'row', style: 'margin-top:6px' },
        el('a', { 'class': 'btn', href: j.browser_url, target: '_blank', rel: 'noopener', style: 'text-decoration:none' }, 'Скачать через браузер'),
        el('span', { 'class': 'muted' }, 'файл ' + (j.asset && j.asset.name) + ' → папка ' + j.downloads_dir)));
      if (j.found && j.found.state === 'mismatch') box.append(el('div', { 'class': 'err' }, j.found.message));
      if (j.found && j.found.state === 'downloading') box.append(el('div', { 'class': 'muted' }, 'Браузер скачивает: ' + mb(j.found.size)));
    }
    if (ACTIVE[j.state]) box.append(el('div', { style: 'margin-top:6px' }, act('Отменить', function () { run(api('POST', 'api/v1/update/remote/cancel', {})); })));
    if (j.state === 'restarting' && j.target) waitRestart(j.target);
    (j.attempts || []).filter(function (a) { return !a.ok; }).forEach(function (a) {
      box.append(el('div', { 'class': 'muted', style: 'font-size:11.5px' }, (LAYER[a.layer] || a.layer) + ': ' + (a.what || a.code) + (a.message ? ' — ' + a.message : '')));
    });
    return box;
  }

  function scanBox() {
    var s = P.scan;
    if (!s) return null;
    var box = el('div', { 'class': 'res' });
    box.append(el('div', {}, el('b', {}, 'Папка загрузок: '), el('span', { 'class': 'mono' }, s.downloads_dir), s.exists ? '' : el('span', { 'class': 'err' }, ' — папки нет')));
    if (!s.items.length) box.append(el('div', { 'class': 'muted' }, 'Архивов DeskMonitor-….zip там нет.'));
    s.items.forEach(function (it) {
      box.append(el('div', { 'class': 'row', style: 'margin-top:4px' },
        el('span', { 'class': 'mono', style: 'word-break:break-all' }, it.file), el('span', { 'class': 'muted' }, it.version + ' · ' + mb(it.size)),
        it.verified ? el('span', { 'class': 'okmsg' }, 'контрольная сумма совпала') : el('span', { 'class': 'err' }, it.reason || 'не проверен'),
        it.verified && it.compare !== 'same' ? act('Установить', function () { installFound(it); }, false, !(P.st && P.st.can_install)) : null));
    });
    return box;
  }

  function render(st) {
    P.st = st;
    var b = P.box;
    b.textContent = '';
    var rel = st.release, ch = st.choice;
    var head = el('div', { 'class': 'row', style: 'gap:6px 14px;flex-wrap:wrap' });
    head.append(el('b', {}, 'Новая версия с GitHub'), el('a', { href: st.releases_page, target: '_blank', rel: 'noopener', 'class': 'mono' }, st.repo));
    if (rel) head.append(el('span', {}, 'последняя ', el('span', { 'class': 'mono' }, rel.version), rel.published_at ? el('span', { 'class': 'muted' }, ' от ' + when(rel.published_at)) : null,
      ch ? el('span', { 'class': ch.compare === 'newer' ? 'tag new' : 'tag', style: 'margin-left:6px' }, { newer: 'новее', same: 'установлена', older: 'старее', other: 'другая сборка' }[ch.compare] || ch.compare) : null));
    else if (st.no_release) head.append(el('span', { 'class': 'muted' }, 'релизов пока нет'));
    var chk = st.check, via = chk && chk.ok ? 'сервер, ' + (LAYER[chk.layer] || chk.layer) : st.browser && st.browser.ok ? 'браузер' : null;
    head.append(el('span', { 'class': 'muted' }, st.checking ? 'проверяю…' : st.checked_at || (st.browser && st.browser.checked_at)
      ? 'проверено ' + when((st.browser && st.browser.checked_at > (st.checked_at || '')) ? st.browser.checked_at : st.checked_at) + (via ? ' (' + via + ')' : '') : 'ещё не проверялось'));
    b.append(head);
    if (chk && !chk.ok && chk.error) {
      var e = chk.error;
      b.append(el('div', { 'class': (st.browser && st.browser.ok) ? 'muted' : 'err', style: 'margin-top:4px' },
        'Сервер до GitHub не достаёт: ' + (e.what || e.code || '') + (e.message ? ' — ' + e.message : '') +
        (st.browser && st.browser.ok ? '. Версию узнал браузер; скачивать тоже будет браузер.' : '')));
    }
    if (st.browser && st.browser.error && !(chk && chk.ok)) b.append(el('div', { 'class': 'err' }, st.browser.error));

    var row = el('div', { 'class': 'row', style: 'margin-top:8px;gap:6px;flex-wrap:wrap' });
    var running = st.job && ACTIVE[st.job.state];
    if (ch && ch.asset && ch.compare === 'newer') {
      row.append(act('Обновить до ' + rel.version.split('-')[0] + ' (' + (ch.asset.kind === 'patch' ? 'заплатка' : 'полный архив') + ', ' + mb(ch.asset.size) + ')',
        function () { install(false); }, true, !st.can_install || running, st.unavailable || null));
    } else if (ch && ch.needs_confirm && rel) {
      row.append(el('span', { 'class': 'muted' }, ch.reason));
      row.append(act('Установить ' + rel.version + ' всё равно', function () { install(true); }, false, !st.can_install || running));
    } else if (ch && ch.reason) row.append(el('span', { 'class': ch.compare === 'same' ? 'okmsg' : 'err' }, ch.reason));
    row.append(act('Проверить сейчас', checkNow, false, st.checking || running));
    row.append(act('Диагностика связи', diagnose, false, running));
    row.append(act('Найти в «Загрузках»', scan, false, running, 'Архив и SHA256SUMS, скачанные со страницы релиза вручную'));
    if (st.rollback) row.append(act('Вернуть ' + st.rollback.version.split('-').slice(0, 2).join('-'), rollback, false, !st.can_install || running, 'Предыдущая версия из резервной копии: ' + st.rollback.version));
    b.append(row);
    b.append(el('div', { 'class': 'ug-msg muted', style: 'margin-top:4px' }));
    var jb = jobBox(st); if (jb) b.append(jb);
    var sb = scanBox(); if (sb) b.append(sb);

    var det = el('details', { style: 'margin-top:8px' }, el('summary', {}, 'Связь с GitHub и настройки обновления'));
    det.open = !!(P.detOpen);
    det.addEventListener('toggle', function () { P.detOpen = det.open; });
    det.append(diagTable(st));
    var dir = el('input', { 'class': 'inp mono', value: st.downloads_dir_set ? st.downloads_dir : '', placeholder: st.downloads_dir, autocomplete: 'off', style: 'min-width:320px' });
    var tr = el('select', { 'class': 'inp' });
    ['auto', 'python', 'curl', 'powershell', 'browser'].forEach(function (k) {
      var o = el('option', { value: k }, k === 'auto' ? 'автоматически (что работает)' : k === 'browser' ? 'только браузер' : LAYER[k]);
      if (st.transport === k) o.selected = true;
      if (st.layers && st.layers[k]) { o.disabled = true; o.textContent += ' — ' + st.layers[k]; }
      tr.append(o);
    });
    var auto = el('input', { type: 'checkbox' }); auto.checked = !!st.auto_check_setting;
    det.append(el('div', { 'class': 'form', style: 'margin-top:10px' },
      el('label', {}, 'Папка загрузок браузера'), el('div', {}, el('div', { 'class': 'row' }, dir,
        act('Сохранить', function () { saveSettings({ update_downloads_dir: dir.value.trim() || null }); })),
        el('p', { 'class': 'hint' }, 'Куда браузер сохраняет файлы (пусто — папка «Загрузки» Windows). Сюда программа смотрит, когда скачивает браузер.')),
      el('label', {}, 'Как сервер ходит в GitHub'), el('div', {}, el('div', { 'class': 'row' }, tr,
        act('Сохранить', function () { saveSettings({ update_transport: tr.value === 'auto' ? null : tr.value }); }))),
      el('label', {}, 'Проверять сам'), el('div', {}, el('label', { 'class': 'row' }, auto, 'искать новую версию при открытии страниц (раз в 6 часов)'),
        act('Сохранить', function () { saveSettings({ update_auto_check: auto.checked ? null : false }); }))));
    det.append(el('p', { 'class': 'hint' }, 'Адаптер (приватный) в публичный релиз не входит: при обновлении установленный адаптер остаётся. Ядро расчётов входит в архив (версия закреплена в сборке).'));
    b.append(det);
    if (running) poll();
  }

  function runMode(m) { if (m === 'panel') panel(); else nav(); }
  window.DESK_UPDATER = { run: runMode, nav: nav, panel: panel, _test: { ghFetch: ghFetch, browserCheck: browserCheck, due: due } };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { runMode(mode); }); else runMode(mode);
})();
