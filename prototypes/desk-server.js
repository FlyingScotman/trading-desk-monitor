/*
 * Раздел настроек «Доступ из сети» (settings.html#net): перезапуск сервера, доступ коллегам по сети (вкл/выкл, HTTPS или
 * HTTP, порт, адрес, новый сертификат), адреса для коллег, правило брандмауэра для ИТ. Сервер: /api/v1/server
 * (backend/src/desk/api/server_routes.py) — те же изменения, что desk.bat lan enable / lan disable; только
 * администратор; при выключенном входе — только с этого компьютера.
 *
 * Перезапуск: сервер завершается, запускатель (окно start.bat) запускает его снова уже с новыми настройками. Страница
 * ждёт: опрашивает /health/live (новый процесс — другой X-Desk-Instance), затем перезагружается. Если адрес сменился
 * (порт, HTTPS) — опрашивает новый адрес и переходит туда; пока браузер не доверяет новому сертификату, проверка не
 * проходит — тогда показывает ссылку и объясняет предупреждение браузера. Не заработало — запускатель вернёт прежние
 * настройки, старый адрес снова ответит, страница перезагрузится и покажет причину.
 *
 * window.DeskServer = { mount(), _test: {...} } — чистые функции для tests/js/desk_server.test.js.
 */
(function () {
  'use strict';
  if (window.DeskServer) return;
  var LOOP = ['127.0.0.1', 'localhost', '::1', '[::1]'];
  var WILD = ['0.0.0.0', '::'];
  var KEY_NAMES = { auth: 'вход по паролю', bind_host: 'адрес', port: 'порт', tls_cert: 'сертификат', tls_key: 'ключ сертификата',
    allow_plain_http: 'HTTP без шифрования', admin_from_network: 'настройки с других компьютеров' };

  /* ---------- чистые функции ---------- */

  function isLoopHost(host) { return LOOP.indexOf(String(host || '').toLowerCase()) >= 0 || /^127\./.test(host || ''); }

  /* Куда смотреть после перезапуска с настройками conf ({lan, bind_host, port, https}), если страница открыта по loc
     ({protocol, hostname, port, origin}): {origin, same, lost}. lost — с этого компьютера страница больше не откроется
     (доступ из сети выключен, а открыта она с другого компьютера). */
  function targetOrigin(conf, loc) {
    var scheme = conf.https ? 'https:' : 'http:', host;
    if (isLoopHost(loc.hostname)) host = loc.hostname;
    else if (!conf.lan) return { origin: null, same: false, lost: true };
    else if (WILD.indexOf(conf.bind_host) >= 0) host = loc.hostname;
    else host = conf.bind_host.indexOf(':') >= 0 ? '[' + conf.bind_host + ']' : conf.bind_host;
    var origin = scheme + '//' + host + ':' + conf.port;
    var cur = loc.origin || (loc.protocol + '//' + loc.hostname + (loc.port ? ':' + loc.port : ''));
    return { origin: origin, same: origin === cur, lost: false };
  }

  /* Одна проверка во время перезапуска. f — fetch. Возвращает 'reload' (этот адрес отвечает новым процессом),
     'go' (новый адрес отвечает), 'back' (не заработало: старый адрес снова отвечает новым процессом с прежними
     настройками), 'old' (ещё отвечает прежний процесс: ждёт конца расчёта) или 'wait'. */
  function probe(st, f) {
    function own() {
      return f('/health/live', { cache: 'no-store', headers: { Accept: 'application/json' } }).then(function (r) {
        if (!r.ok) return 'wait';
        var inst = r.headers && r.headers.get ? r.headers.get('X-Desk-Instance') : null;
        if (inst && inst === st.instance) return 'old';
        return st.same ? 'reload' : 'back';
      }, function () { return 'wait'; });
    }
    if (st.same || !st.origin) return own();
    return f(st.origin + '/health/live', { mode: 'no-cors', cache: 'no-store', credentials: 'omit' })
      .then(function () { return 'go'; }, own);
  }

  /* Предупреждения формы: [{cls: 'warn'|'err'|'', text}], block — «Применить» недоступна. s — GET /api/v1/server,
     v — значения формы, loopback — страница открыта на этом компьютере. */
  function warnings(s, v, loopback) {
    var out = [], block = false, c = s.configured, admins = s.admins || [];
    if (!s.client.may_change_network) {
      out.push({ cls: 'warn', text: 'Доступ из сети меняют только на компьютере, где работает Desk Monitor (настройка «с других компьютеров» выключена).' });
      block = true;
    }
    if (v.lan && !admins.length) {
      out.push({ cls: 'err', text: s.admins == null ? 'База недоступна: не проверить, есть ли администратор.'
        : 'Сначала добавьте себя администратором в разделе «Пользователи»: с доступом из сети включается вход по паролю, и без администратора после перезапуска никто не сможет войти.' });
      block = true;
    } else if (v.lan && !c.auth) {
      out.push({ cls: '', text: 'Включится вход по паролю. После перезапуска войдите под администратором (' + admins.join(', ') + ') — паролем, заданным в «Пользователях».' });
    }
    if (v.lan && !v.https) out.push({ cls: 'warn', text: 'HTTP без шифрования: пароли и данные идут по сети открытым текстом, на каждой странице будет жёлтое предупреждение. Лучше HTTPS.' });
    if (v.lan && v.https && (!c.https || v.regen)) out.push({ cls: '', text: 'Будет создан новый сертификат. Браузер коллеги один раз предупредит «Подключение не защищено» — «Дополнительно» → «Перейти на сайт».' });
    if (!loopback && !v.lan) out.push({ cls: 'warn', text: 'Вы открыли эту страницу с другого компьютера. После выключения доступа из сети она здесь больше не откроется — только на компьютере Desk Monitor.' });
    if (!loopback && v.lan && !v.afn) out.push({ cls: 'warn', text: 'После перезапуска эти настройки откроются только на компьютере Desk Monitor.' });
    if (!(v.port >= 1 && v.port <= 65535)) { out.push({ cls: 'err', text: 'Порт — число от 1 до 65535 (обычно 8100 или 8443).' }); block = true; }
    if (v.lan && v.host === '') { out.push({ cls: 'err', text: 'Укажите адрес этого компьютера или выберите «все адреса».' }); block = true; }
    return { list: out, block: block };
  }

  /* Изменилось ли что-то против сохранённых настроек. */
  function changed(c, v) {
    if (v.lan !== c.lan || v.port !== c.port || v.regen) return true;
    if (!v.lan) return v.authOff && c.auth;
    return v.https !== c.https || v.host !== c.bind_host || v.afn !== c.admin_from_network;
  }

  /* ---------- страница ---------- */

  function $(id) { return document.getElementById(id); }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) {
      if (attrs[k] == null || attrs[k] === false) continue;
      if (k === 'onclick') e.addEventListener('click', attrs[k]); else e.setAttribute(k, attrs[k] === true ? '' : attrs[k]);
    }
    [].concat(kids == null ? [] : kids).forEach(function (x) { if (x != null) e.append(x.nodeType ? x : document.createTextNode(String(x))); });
    return e;
  }
  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function api(method, path, body) {
    var h = { Accept: 'application/json' };
    if (method !== 'GET') { h['Content-Type'] = 'application/json'; h['X-CSRF-Token'] = cookie('desk_csrf'); }
    return fetch(path, { method: method, headers: h, cache: 'no-store', body: body === undefined ? undefined : JSON.stringify(body) })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) { var e = new Error((j && j.message) || ('HTTP ' + r.status)); e.status = r.status; throw e; }
          return j;
        });
      }, function (e) { throw new Error('сервер недоступен: ' + e.message); });
  }
  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (ok, no) {
      var ta = el('textarea', { style: 'position:fixed;left:-9999px;top:0', readonly: true }); ta.value = text;
      document.body.append(ta); ta.select(); var done = document.execCommand('copy'); ta.remove();
      if (done) ok(); else no(new Error('буфер обмена недоступен'));
    });
  }
  function copyBtn(text, label, done) {
    var b = el('button', { 'class': 'ibtn', type: 'button' }, label || 'Скопировать');
    b.addEventListener('click', function () {
      copy(text).then(function () { b.textContent = done || 'Скопировано'; setTimeout(function () { b.textContent = label || 'Скопировать'; }, 2000); },
        function (e) { b.textContent = 'Не скопировано: ' + e.message; });
    });
    return b;
  }
  var when = function (s) { return s ? new Date(s).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }) : ''; };
  var loopback = function () { return isLoopHost(location.hostname); };

  var S = { s: null, dirty: false, watching: false };

  function msg(text, cls) { var m = $('nf-msg'); m.className = cls || 'muted'; m.textContent = text || ''; }

  function values() {
    var hostSel = $('nf-host').value;
    return {
      lan: $('nf-lan').checked,
      https: (document.querySelector('input[name="nf-mode"]:checked') || {}).value !== 'http',
      port: parseInt($('nf-port').value, 10),
      host: hostSel === 'other' ? $('nf-host-x').value.trim() : hostSel,
      regen: $('nf-regen').checked,
      afn: $('nf-afn').checked,
      authOff: $('nf-authoff').checked
    };
  }

  function fill(s) {
    var c = s.configured;
    $('nf-lan').checked = c.lan;
    document.querySelector('input[name="nf-mode"][value="' + (c.lan && !c.https && c.plain_http ? 'http' : 'https') + '"]').checked = true;
    $('nf-port').value = c.port;
    var sel = $('nf-host'); sel.textContent = '';
    sel.append(el('option', { value: '0.0.0.0' }, 'все адреса этого компьютера (обычно подходит)'));
    var addrs = (s.addresses || []).slice();
    if (c.lan && WILD.indexOf(c.bind_host) < 0 && addrs.indexOf(c.bind_host) < 0) addrs.push(c.bind_host);
    addrs.forEach(function (a) { sel.append(el('option', { value: a }, 'только ' + a)); });
    sel.append(el('option', { value: 'other' }, 'другой адрес…'));
    sel.value = c.lan && WILD.indexOf(c.bind_host) < 0 ? c.bind_host : '0.0.0.0';
    $('nf-host-x').value = ''; $('nf-host-x').hidden = true;
    $('nf-regen').checked = false;
    $('nf-afn').checked = c.admin_from_network;
    $('nf-authoff').checked = false;
    var locked = s.locked || [];
    $('nf-port').disabled = locked.indexOf('port') >= 0;
    $('nf-port-lock').hidden = locked.indexOf('port') < 0;
    S.dirty = false;
    sync();
  }

  function sync() {
    var s = S.s; if (!s) return;
    var v = values(), c = s.configured;
    $('nf-lan-l').textContent = v.lan ? 'включён: коллеги открывают по адресу этого компьютера' : 'выключен: только этот компьютер';
    $('nf-afn-l').textContent = v.afn ? 'можно' : 'только на этом компьютере';
    document.querySelectorAll('#net .nf-on').forEach(function (e) { e.hidden = !v.lan; });
    document.querySelectorAll('#net .nf-off').forEach(function (e) { e.hidden = v.lan || !c.auth; });
    $('nf-cert-row').hidden = !(v.lan && v.https && c.https);
    $('nf-cert-row').previousElementSibling.hidden = $('nf-cert-row').hidden;
    $('nf-host-x').hidden = $('nf-host').value !== 'other';
    var w = warnings(s, v, loopback()), box = $('nf-warn'); box.textContent = '';
    w.list.forEach(function (x) { box.append(el('div', { 'class': 'note ' + x.cls }, x.text)); });
    var diff = changed(c, v);
    var can = !w.block && !S.watching && (diff || (s.restart_needed || []).length > 0);
    $('nf-apply').disabled = !can || !s.can_restart;
    $('nf-save').disabled = w.block || S.watching || !diff;
    $('nf-reset').hidden = !diff;
    $('nf-apply').title = s.can_restart ? '' : s.unavailable || '';
  }

  function renderServer(s) {
    var r = s.running, c = s.configured;
    $('net-st').textContent = c.lan ? 'доступ из сети включён' : 'только этот компьютер';
    $('net-st').className = 'badge st ' + (c.lan ? 'ok' : '');
    var kv = $('srv-kv'); kv.textContent = '';
    var item = function (l, v, mono) { kv.append(el('div', {}, [el('div', { 'class': 'l' }, l), el('div', { 'class': 'v' + (mono ? ' mono' : '') }, v)])); };
    item('Версия', s.version, true);
    item('Работает с', when(s.started_at));
    item('Адрес на этом компьютере', r ? (r.https ? 'https' : 'http') + '://127.0.0.1:' + r.port + '/' : '—', true);
    item('Доступ из сети', r ? (r.lan ? 'включён (' + r.bind_host + ')' : 'выключен') : '—');
    item('Вход по паролю', r ? (r.auth ? 'включён' : 'выключен') : '—');
    $('srv-restart').disabled = !s.can_restart || S.watching;
    $('srv-hint').textContent = s.can_restart
      ? 'Перезапуск занимает 10–30 секунд; если идёт расчёт, сервер дождётся его окончания. Страница обновится сама.'
      : s.unavailable;
    var need = s.restart_needed || [];
    $('net-pending').hidden = !need.length;
    if (need.length) {
      $('net-pending').textContent = '';
      $('net-pending').append(el('b', {}, 'Изменения ещё не действуют. '),
        'Сохранены новые настройки (' + need.map(function (k) { return KEY_NAMES[k] || ({ https: 'шифрование' })[k] || k; }).join(', ') + '), а сервер работает со старыми. ',
        s.can_restart ? el('button', { 'class': 'btn', type: 'button', onclick: restartNow }, 'Перезапустить сейчас') : s.unavailable);
    }
    var l = s.last, box = $('net-last');
    box.hidden = !l;
    if (l) {
      box.className = 'note ' + (l.ok ? 'ok' : 'err');
      box.textContent = (l.ok ? 'Последний перезапуск (' : 'Последний перезапуск не удался (') + when(l.at) + '): ' + (l.message || '');
    }
    // адреса для коллег
    var ul = $('net-urls-list'); ul.textContent = '';
    if (!c.lan) ul.append(el('p', { 'class': 'muted' }, 'Доступ из сети выключен: Desk Monitor открывается только на этом компьютере — ' + s.local_url));
    else {
      s.urls.forEach(function (u) { ul.append(el('div', { 'class': 'row', style: 'margin:3px 0' }, [el('span', { 'class': 'mono', style: 'min-width:260px' }, u), copyBtn(u)])); });
      ul.append(el('p', { 'class': 'hint' }, 'Отправьте коллеге один из адресов (обычно первый — имя компьютера). Он входит под своим именем и паролем из раздела «Пользователи».'
        + (c.https ? ' Сертификат создан здесь, поэтому браузер коллеги один раз предупредит «Подключение не защищено»: «Дополнительно» → «Перейти на сайт».' : '')));
    }
    var cert = $('nf-cert'); cert.textContent = '';
    if (s.cert) {
      cert.append(el('div', {}, !s.cert.exists ? 'Файл сертификата не найден — создайте заново.' : s.cert.error ? s.cert.error
        : 'Действует до ' + s.cert.not_after + (c.self_signed ? ' (создан здесь)' : ' (от ИТ)') + '; имена: ' + (s.cert.sans || []).join(', ')));
      if (s.cert.missing && s.cert.missing.length) cert.append(el('div', { 'class': 'err' }, 'Нет в сертификате (браузер предупредит): ' + s.cert.missing.join(', ') + ' — создайте заново.'));
    }
    // брандмауэр
    var fw = $('fw-body'); fw.textContent = '';
    $('fw').hidden = !c.lan;
    if (s.firewall) {
      var st = { 'true': ['есть', 'okmsg'], 'false': ['нет — коллеги, скорее всего, не откроют', 'err'], 'null': ['неизвестно', 'muted'] }[String(s.firewall.present)];
      fw.append(el('p', { style: 'margin:0 0 8px' }, ['Правило «' + s.firewall.rule + '»: ', el('b', { 'class': st[1] }, st[0])]));
      fw.append(el('p', { 'class': 'hint', style: 'margin:0 0 8px' }, 'Брандмауэр Windows меняют только с правами администратора компьютера, поэтому здесь кнопки нет. Отправьте строку ниже в ИТ (или, если права есть: Пуск → cmd → правой кнопкой «Запуск от имени администратора», вставить и Enter; или там же desk.bat firewall add в папке программы).'));
      fw.append(el('pre', { 'class': 'mono', style: 'white-space:pre-wrap;word-break:break-all;margin:0 0 8px;background:var(--bg);border:1px solid var(--line-2);border-radius:6px;padding:8px 10px;font-size:11.5px' }, s.firewall.add));
      var who = s.computer || location.hostname;
      var it = 'Прошу разрешить входящие подключения к программе Desk Monitor на компьютере ' + who + ': TCP-порт ' + c.port +
        ', сети домена и частные (не общественные). Команда для cmd, запущенной от имени администратора:\r\n' + s.firewall.add +
        '\r\nЕсли порты закрываются и на сетевом оборудовании — нужен этот же порт (TCP ' + c.port + ') к этому компьютеру.';
      fw.append(el('div', { 'class': 'row' }, [copyBtn(it, 'Скопировать для ИТ', 'Скопировано: вставьте в письмо или заявку'), copyBtn(s.firewall.add, 'Скопировать только команду')]));
    }
  }

  function load() {
    return api('GET', '/api/v1/server').then(function (s) {
      S.s = s;
      $('net-denied').hidden = true; $('net-main').hidden = false;
      renderServer(s);
      if (!S.dirty) fill(s); else sync();
      return s;
    }, function (e) {
      $('net-denied').hidden = false; $('net-main').hidden = true;
      $('net-denied').textContent = e.status === 403 || e.status === 401 ? 'Доступом из сети и перезапуском управляет администратор.' : 'Не загрузилось: ' + e.message;
    });
  }

  /* ---------- перезапуск и ожидание ---------- */

  function overlay(step, text, cls) {
    var o = $('rs'); o.hidden = false;
    var lis = $('rs-steps').children;
    for (var i = 0; i < lis.length; i++) lis[i].className = i < step ? 'done' : i === step ? (cls === 'err' ? 'fail' : 'on') : '';
    $('rs-msg').className = cls === 'err' ? 'err' : cls === 'ok' ? 'okmsg' : 'muted';
    $('rs-msg').textContent = text || '';
  }

  function watch(t, instance) {
    S.watching = true; sync();
    var started = Date.now(), down = 0, shown = false;  // down: when the old process stopped answering
    $('rs-close').hidden = true; $('rs-open').hidden = true; $('rs-addr').textContent = '';
    if (t.lost) {
      overlay(1, 'Сервер перезапускается без доступа из сети. С этого компьютера страница больше не откроется — только на компьютере Desk Monitor.', 'ok');
      return;
    }
    var path = location.pathname + location.search + location.hash;
    if (!t.same) {
      $('rs-addr').textContent = 'Новый адрес: ' + t.origin + '/';
      $('rs-open').href = t.origin + path;
    }
    overlay(0, 'Сервер останавливается…');
    var st = { origin: t.origin, same: t.same, instance: instance };
    (function tick() {
      probe(st, fetch.bind(window)).then(function (res) {
        var sec = (Date.now() - started) / 1000;
        if (res !== 'old' && !down) down = Date.now();
        var up = down ? (Date.now() - down) / 1000 : 0;  // seconds since the old process stopped
        if (res === 'reload') { overlay(3, 'Сервер снова работает. Обновляю страницу…', 'ok'); setTimeout(function () { location.reload(); }, 800); return; }
        if (res === 'go') { overlay(3, 'Сервер работает по новому адресу. Перехожу…', 'ok'); setTimeout(function () { location.href = t.origin + path; }, 800); return; }
        if (res === 'back') { overlay(3, 'Новые настройки не заработали — сервер вернул прежние. Обновляю страницу, там будет причина…', 'err'); setTimeout(function () { location.reload(); }, 1500); return; }
        if (res === 'old') overlay(0, sec > 5 ? 'Сервер дожидается окончания текущего расчёта (до 5 минут), затем перезапустится…' : 'Сервер останавливается…');
        else overlay(up < 3 ? 1 : 2, 'Сервер запускается заново (обычно 10–30 секунд)…');
        if (!t.same && !shown && up > 20) {
          shown = true; $('rs-open').hidden = false;
          $('rs-addr').textContent = 'Новый адрес: ' + t.origin + '/. ' + (t.origin.indexOf('https:') === 0
            ? 'Если сервер уже запустился, а страница всё ждёт, — браузер ещё не доверяет новому сертификату: нажмите «Открыть новый адрес», затем в предупреждении «Дополнительно» → «Перейти на сайт».'
            : 'Если страница не переходит сама, нажмите «Открыть новый адрес».');
        }
        if (up > 240 || sec > 600) {
          overlay(2, 'Сервер не ответил за 4 минуты. Посмотрите окно start.bat: там причина. Журнал: logs\\serve.log в папке данных. Если окно закрыто — запустите start.bat.', 'err');
          $('rs-close').hidden = false; $('rs-open').hidden = t.same; S.watching = false;
          return;
        }
        setTimeout(tick, 1500);
      });
    })();
  }

  function restartNow() {
    if (!confirm('Перезапустить сервер?\n\nDesk Monitor будет недоступен 10–30 секунд (у коллег тоже). Если сейчас идёт расчёт, сервер дождётся его окончания. Страница обновится сама.')) return;
    var inst = S.s && S.s.instance;
    api('POST', '/api/v1/server/restart', {}).then(function (r) {
      watch({ origin: location.origin, same: true, lost: false }, r.instance || inst);
    }, function (e) { overlay(0, 'Не перезапущен: ' + e.message, 'err'); $('rs-close').hidden = false; });
  }

  function apply(restart) {
    var v = values(), s = S.s;
    var body = { lan: v.lan, https: v.https, port: v.port, host: v.lan ? v.host : null, regenerate_cert: v.lan && v.https && v.regen,
      admin_from_network: v.afn, auth_off: !v.lan && v.authOff, restart: restart };
    var t = targetOrigin({ lan: v.lan, bind_host: v.lan ? v.host : '127.0.0.1', port: v.port, https: v.lan ? v.https : s.configured.https }, location);
    if (restart) {
      var lines = ['Применить и перезапустить сервер?', '', 'Desk Monitor будет недоступен 10–30 секунд (у коллег тоже).'];
      if (t.lost) lines.push('Вы на другом компьютере: после этого страница здесь не откроется.');
      else if (!t.same) lines.push('Адрес изменится: ' + t.origin + '/ — страница перейдёт туда сама.');
      lines.push('Если сервер не запустится с новыми настройками, вернутся прежние.');
      if (!confirm(lines.join('\n'))) return;
    }
    msg(restart ? 'Применяю…' : 'Сохраняю…');
    $('nf-apply').disabled = $('nf-save').disabled = true;
    api('POST', '/api/v1/server/network', body).then(function (r) {
      S.dirty = false;
      if (r.restarting) {
        msg(r.message, 'okmsg');
        watch(targetOrigin(r.configured, location), r.instance);
      } else { msg(r.message, 'okmsg'); load(); }
    }, function (e) { msg('Не применено: ' + e.message, 'err'); sync(); });
  }

  function mount() {
    if (!$('net')) return;
    if ($('net').__ds) { if (!S.watching) load(); return; }  // the section shown again: fresh state
    $('net').__ds = true;
    $('net').addEventListener('input', function (e) { if (e.target.closest('.form')) { S.dirty = true; sync(); } });
    $('net').addEventListener('change', function (e) { if (e.target.closest('.form')) { S.dirty = true; sync(); } });
    $('nf-apply').addEventListener('click', function () { apply(true); });
    $('nf-save').addEventListener('click', function () { apply(false); });
    $('nf-reset').addEventListener('click', function () { if (S.s) fill(S.s); msg(''); });
    $('srv-restart').addEventListener('click', restartNow);
    $('rs-close').addEventListener('click', function () { $('rs').hidden = true; load(); });
    load();
  }

  window.DeskServer = { mount: mount, load: load,
    _test: { targetOrigin: targetOrigin, probe: probe, warnings: warnings, changed: changed, isLoopHost: isLoopHost } };
})();
