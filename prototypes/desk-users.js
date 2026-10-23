/*
 * Раздел «Пользователи» (пакет 1, п. 9): список, добавление, роль, отключение, удаление, сброс пароля, снятие блокировки
 * и журнал действий. Самостоятельный: свой стиль, свои запросы (/api/v1/users, /api/v1/audit, только администратор;
 * при выключенном входе — только с этого компьютера), CSRF-заголовок ставит сам.
 *
 * Где показывать: любой элемент с атрибутом data-desk-users (раздел встаёт в него при загрузке), например
 *   <section data-desk-users></section><script src="desk-users.js"></script>
 * или window.DeskUsers.mount(элемент). Показывается в настройках, раздел «Пользователи» (settings.html#users;
 * users.html переводит туда).
 */
(function () {
  'use strict';
  if (window.DeskUsers) return;
  var ROLE = { viewer: 'просмотр', trader: 'трейдер', admin: 'администратор' };
  var ROLE_HINT = { viewer: 'видит все страницы', trader: '+ точные сценарии, «Обновить» автопилота, алиасы', admin: '+ настройки, адаптер, обновления, пользователи' };
  var ACT = { login: 'вход', login_failed: 'неудачный вход', logout: 'выход', user_add: 'новый пользователь', user_change: 'изменён пользователь',
    password_reset: 'сброс пароля', password_change: 'смена пароля', settings: 'настройки', adapter: 'адаптер', update: 'обновление',
    alias: 'алиас', scenario_job: 'точный сценарий', autopilot_run: 'цикл автопилота', user_delete: 'удалён пользователь',
    server_restart: 'перезапуск сервера', network_change: 'доступ из сети' };

  var css = '.du{font:13px/1.4 var(--font-ui,var(--font-sans,system-ui,-apple-system,"Segoe UI",sans-serif));color:var(--fg,var(--text,#1b2129))}' +
    '.du h2{font-size:15px;margin:0 0 6px}.du h3{font-size:13px;margin:16px 0 6px}' +
    '.du .du-note{color:var(--fg-2,var(--text-2,#4d5866));margin:0 0 10px}' +
    '.du table{border-collapse:collapse;width:100%;margin:6px 0}' +
    '.du th,.du td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--line,#d9dee5);vertical-align:middle}' +
    '.du th{font-weight:600;color:var(--fg-2,var(--text-2,#4d5866));font-size:12px}' +
    '.du td.du-off{color:var(--fg-3,var(--muted,#6b7685))}' +
    '.du input,.du select{font:inherit;color:inherit;background:transparent;border:1px solid var(--line,#d9dee5);border-radius:2px;padding:3px 6px}' +
    '.du select option{background:var(--panel,var(--surface,#fff))}' +
    '.du button{font:inherit;color:var(--accent,#2f6fd6);background:transparent;border:1px solid var(--line,#d9dee5);border-radius:2px;padding:2px 8px;cursor:pointer;margin:1px 2px}' +
    '.du button:hover{border-color:var(--accent,#2f6fd6)}' +
    '.du form{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:flex-end;margin:6px 0}' +
    '.du form label{display:flex;flex-direction:column;font-size:12px;color:var(--fg-2,var(--text-2,#4d5866));gap:2px}' +
    '.du .du-err{color:var(--crit,var(--critical,#c62f2f));white-space:pre-wrap;margin:4px 0}' +
    '.du .du-ok{color:var(--good,var(--ok,#1a7f37));margin:4px 0}' +
    '.du .du-tag{font-size:11px;border:1px solid var(--line,#d9dee5);border-radius:2px;padding:0 4px;margin-left:4px}' +
    '.du details{margin-top:12px}.du summary{cursor:pointer;font-weight:600}';

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function cookie(name) {
    var m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function api(method, path, body) {
    var h = { Accept: 'application/json' };
    if (method !== 'GET') { h['Content-Type'] = 'application/json'; h['X-CSRF-Token'] = cookie('desk_csrf'); }
    return fetch(path, { method: method, headers: h, cache: 'no-store', body: body == null ? undefined : JSON.stringify(body) })
      .then(function (r) {
        if (r.status === 204) return null;
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok) throw new Error((j && j.message) || ('HTTP ' + r.status));
          return j;
        });
      });
  }
  var ts = function (s) { return s ? String(s).replace('T', ' ').slice(0, 16) : '—'; };

  function mount(root) {
    if (!root || root.__du) return;
    root.__du = true;
    if (!document.getElementById('du-css')) document.head.appendChild(el('style', { id: 'du-css' }, css));
    root.classList.add('du');
    root.innerHTML = '';
    root.appendChild(el('h2', {}, 'Пользователи'));
    var note = el('p', { 'class': 'du-note' }), msg = el('div'), list = el('div');
    root.append(note, msg);
    function say(text, ok) { msg.className = ok ? 'du-ok' : 'du-err'; msg.textContent = text || ''; }

    var form = el('form', { autocomplete: 'off' });
    form.innerHTML = '<label>Имя (латиницей)<input name="u" required pattern="[A-Za-z0-9][A-Za-z0-9._\\-]{0,63}" placeholder="ivanov"></label>' +
      '<label>Как показывать<input name="d" maxlength="120" placeholder="Иванов И."></label>' +
      '<label>Роль<select name="r"></select></label>' +
      '<label>Временный пароль<input name="p" type="password" minlength="8" required autocomplete="new-password"></label>' +
      '<label style="flex-direction:row;align-items:center;gap:4px"><input name="t" type="checkbox" checked>сменить при первом входе</label>' +
      '<button type="submit">Добавить</button>';
    Object.keys(ROLE).forEach(function (r) { var o = el('option', { value: r, title: ROLE_HINT[r] }, ROLE[r]); if (r === 'viewer') o.selected = true; form.r.appendChild(o); });
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      api('POST', '/api/v1/users', { username: form.u.value, display_name: form.d.value || null, role: form.r.value, password: form.p.value, must_change_password: form.t.checked })
        .then(function (u) { say('Добавлен ' + u.username + ' (' + ROLE[u.role] + '). Сообщите ему имя и временный пароль лично.', true); form.reset(); form.r.value = 'viewer'; load(); })
        .catch(function (err) { say(err.message); });
    });
    root.appendChild(el('h3', {}, 'Учётные записи'));
    root.appendChild(list);
    root.appendChild(el('h3', {}, 'Новый пользователь'));
    root.appendChild(el('p', { 'class': 'du-note' }, 'Роли: просмотр — ' + ROLE_HINT.viewer + '; трейдер ' + ROLE_HINT.trader + '; администратор ' + ROLE_HINT.admin + '.'));
    root.appendChild(form);
    var log = el('details'), logBody = el('div');
    log.appendChild(el('summary', {}, 'Журнал действий (последние 100)'));
    log.appendChild(logBody);
    log.addEventListener('toggle', function () { if (log.open) loadAudit(); });
    root.appendChild(log);

    function act(method, path, body, done) {
      return api(method, path, body).then(function () { say(done, true); load(); }).catch(function (err) { say(err.message); });
    }
    function row(u, me) {
      var tr = el('tr');
      var name = el('td', u.active ? {} : { 'class': 'du-off' });
      name.appendChild(el('b', {}, u.username));
      if (u.display_name) name.appendChild(el('span', {}, ' · ' + u.display_name));
      if (!u.active) name.appendChild(el('span', { 'class': 'du-tag' }, 'отключён'));
      if (u.locked) name.appendChild(el('span', { 'class': 'du-tag', title: 'Слишком много неверных паролей' }, 'заблокирован'));
      if (u.must_change_password) name.appendChild(el('span', { 'class': 'du-tag', title: 'Сменит пароль при входе' }, 'временный пароль'));
      tr.appendChild(name);
      var rc = el('td'), sel = el('select', { 'aria-label': 'Роль ' + u.username });
      Object.keys(ROLE).forEach(function (r) { var o = el('option', { value: r, title: ROLE_HINT[r] }, ROLE[r]); if (r === u.role) o.selected = true; sel.appendChild(o); });
      sel.addEventListener('change', function () { act('PATCH', '/api/v1/users/' + encodeURIComponent(u.username), { role: sel.value }, u.username + ': роль «' + ROLE[sel.value] + '»'); });
      rc.appendChild(sel);
      tr.appendChild(rc);
      tr.appendChild(el('td', {}, ts(u.last_login_at)));
      var a = el('td');
      var reset = el('button', { type: 'button', title: 'Задать временный пароль; его сеансы закроются' }, 'Сбросить пароль');
      reset.addEventListener('click', function () {
        var p = prompt('Временный пароль для ' + u.username + ' (не короче 8 знаков). Он сменит его при входе.');
        if (p) act('POST', '/api/v1/users/' + encodeURIComponent(u.username) + '/password', { password: p, must_change_password: true }, u.username + ': пароль сброшен');
      });
      a.appendChild(reset);
      if (u.username !== me) {
        var tog = el('button', { type: 'button', title: u.active ? 'Запретить вход (сеансы закроются)' : 'Разрешить вход' }, u.active ? 'Отключить' : 'Включить');
        tog.addEventListener('click', function () {
          if (u.active && !confirm('Отключить ' + u.username + '? Его сеансы закроются.')) return;
          act('PATCH', '/api/v1/users/' + encodeURIComponent(u.username), { active: !u.active }, u.username + (u.active ? ': отключён' : ': включён'));
        });
        a.appendChild(tog);
      }
      if (u.username !== me) {
        var del = el('button', { type: 'button', title: 'Удалить учётную запись совсем (в журнале действий имя останется). Чтобы только запретить вход — «Отключить».' }, 'Удалить');
        del.addEventListener('click', function () {
          if (!confirm('Удалить пользователя ' + u.username + ' совсем?\nЕго сеансы закроются, вернуть учётную запись нельзя (можно завести заново). Чтобы только запретить вход, нажмите «Отключить».')) return;
          act('DELETE', '/api/v1/users/' + encodeURIComponent(u.username), null, u.username + ': удалён');
        });
        a.appendChild(del);
      }
      if (u.locked) {
        var un = el('button', { type: 'button' }, 'Снять блокировку');
        un.addEventListener('click', function () { act('PATCH', '/api/v1/users/' + encodeURIComponent(u.username), { unlock: true }, u.username + ': блокировка снята'); });
        a.appendChild(un);
      }
      tr.appendChild(a);
      return tr;
    }
    function load() {
      return Promise.all([api('GET', '/api/v1/users'), api('GET', '/api/v1/auth/state').catch(function () { return null; })]).then(function (r) {
        var j = r[0], st = r[1] || {}, me = st.user && st.user.username;
        note.textContent = st.enabled ? 'Вход по паролю включён: коллеги входят под своими именами. Пароли хранятся только в виде хэша (Argon2id).'
          : 'Вход по паролю выключен: Desk Monitor открыт только на этом компьютере. Заведите администратора и пользователей здесь, затем включите доступ в разделе «Доступ из сети» (Настройки → «Доступ из сети» → «Применить и перезапустить»).';
        list.innerHTML = '';
        if (!j.users.length) { list.appendChild(el('p', { 'class': 'du-note' }, 'Пользователей пока нет.')); return; }
        var t = el('table'), hd = el('tr');
        ['Пользователь', 'Роль', 'Последний вход', ''].forEach(function (h) { hd.appendChild(el('th', {}, h)); });
        t.appendChild(hd);
        j.users.forEach(function (u) { t.appendChild(row(u, me)); });
        list.appendChild(t);
      }).catch(function (err) { list.innerHTML = ''; say('Список пользователей недоступен: ' + err.message); });
    }
    function loadAudit() {
      api('GET', '/api/v1/audit?limit=100').then(function (j) {
        logBody.innerHTML = '';
        var t = el('table'), hd = el('tr');
        ['Когда', 'Кто', 'Что', 'Объект', 'Адрес'].forEach(function (h) { hd.appendChild(el('th', {}, h)); });
        t.appendChild(hd);
        j.entries.forEach(function (x) {
          var tr = el('tr');
          tr.appendChild(el('td', {}, ts(x.at)));
          tr.appendChild(el('td', {}, x.actor));
          tr.appendChild(el('td', x.ok ? {} : { 'class': 'du-off' }, (ACT[x.action] || x.action) + (x.ok ? '' : ' ✕')));
          tr.appendChild(el('td', { title: x.detail ? JSON.stringify(x.detail) : '' }, x.target || ''));
          tr.appendChild(el('td', {}, x.ip || ''));
          t.appendChild(tr);
        });
        logBody.appendChild(t);
      }).catch(function (err) { logBody.textContent = err.message; });
    }
    load();
  }

  window.DeskUsers = { mount: mount };
  function auto() { Array.prototype.forEach.call(document.querySelectorAll('[data-desk-users]'), mount); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', auto); else auto();
})();
