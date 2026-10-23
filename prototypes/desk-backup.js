/*
 * Раздел «Резервные копии» настроек (trading-desk-monitor#78; сервер — desk.backup, маршруты /api/v1/backup/*).
 * Самостоятельный: свой стиль, свои запросы. Встаёт в элемент с атрибутом data-desk-backup и сам добавляет раздел
 * «backup» в навигацию settings.html (массив SECTIONS страницы), поэтому в settings.html — только ссылка, пустой
 * раздел и <script src="desk-backup.js">.
 *
 * Что умеет: расписание (вкл/выкл, время, сколько копий хранить, папка — ключи settings.json backup, backup_time,
 * backup_keep, backup_dir), «Создать копию сейчас», «Проверить восстановление» (копия разворачивается во временную
 * базу рядом с рабочей и сверяется; рабочие данные не трогаются). Заменить рабочие данные копией — только командой
 * при остановленном сервере (страницу отдаёт этот же сервер): раздел показывает команду и объясняет её.
 * CSRF-заголовок к POST/PUT добавляет desk-auth.js (его подключает desk-nav.js).
 */
(function () {
  'use strict';
  if (window.DeskBackup) return;
  var KIND = { nightly: 'ежедневная', manual: 'вручную', 'pre-restore': 'перед восстановлением' };
  var css = '#bk .bk-tw{overflow-x:auto;border:1px solid var(--line-2);border-radius:6px;max-height:420px;margin-top:10px}' +
    '#bk .bk-tw table{margin:0} #bk th.n{text-align:right} #bk td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}' +
    '#bk .bk-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}' +
    '#bk .bk-job{margin-top:10px} #bk .bk-prob{margin:4px 0 0 18px;padding:0}' +
    '#bk ol.bk-steps{margin:6px 0 0 18px;padding:0} #bk ol.bk-steps li{margin:0 0 8px}' +
    '#bk ul.bk-args{margin:4px 0 0 16px;padding:0;color:var(--fg-2)} #bk code{font-family:var(--mono);font-size:12px}';

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs || {}) { if (k === 'class') e.className = attrs[k]; else e.setAttribute(k, attrs[k]); }
    if (text != null) e.textContent = text;
    return e;
  }
  function api(method, url, body) {
    var opt = { method: method, headers: { Accept: 'application/json' }, cache: 'no-store' };
    if (method !== 'GET') { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body || {}); }
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return null; }).then(function (j) {
        if (!r.ok) throw new Error((j && (j.message || j.detail)) || ('HTTP ' + r.status));
        return j;
      });
    });
  }
  function mb(n) { return n == null ? '—' : (n / 1e6).toLocaleString('ru-RU', { maximumFractionDigits: 1 }) + ' МБ'; }
  function num(n) { return n == null ? '—' : Number(n).toLocaleString('ru-RU'); }
  function when(s) { return s ? String(s).slice(0, 16).replace('T', ' ') : '—'; }
  function hours(h) {
    if (h == null) return '';
    if (h < 1) return 'меньше часа назад';
    if (h < 48) return Math.round(h) + ' ч назад';
    return Math.round(h / 24) + ' дн назад';
  }

  var root = null, S = { status: null, stored: {}, overrides: [], timer: null };

  function build() {
    var st = el('style'); st.textContent = css; document.head.appendChild(st);
    root.id = 'bk';
    root.innerHTML =
      '<div class="note err" id="bk-err" hidden></div>' +
      '<div class="note warn" id="bk-disk" hidden></div>' +
      '<div class="note warn" id="bk-priv" hidden></div>' +
      '<div class="card"><div class="hd"><h2>Расписание</h2><span class="mut3" id="bk-next"></span></div>' +
      '<div class="bd form">' +
      '<label for="bk-on">Ежедневная копия</label><div><label class="tgl"><input type="checkbox" id="bk-on"><span class="tgl-s"></span><span id="bk-on-l"></span></label>' +
      '<p class="hint">Копию делает сам сервер (окно start.bat): ни службы Windows, ни Планировщика заданий, ни прав администратора не нужно. ' +
      'Если в это время компьютер был выключен — копия будет сделана сразу при следующем запуске.</p></div>' +
      '<label for="bk-time">Время</label><div><input class="inp mono" id="bk-time" type="time" style="width:130px"><p class="hint">Пусто — 20:30, время этого компьютера.</p></div>' +
      '<label for="bk-keep">Хранить копий</label><div><input class="inp mono num" id="bk-keep" inputmode="numeric" placeholder="14"><p class="hint">Пусто — 14 последних (ежедневные и сделанные вручную); старые удаляются после каждой новой. Копии «перед восстановлением» — отдельно, 3 последние.</p></div>' +
      '<label for="bk-dir">Папка</label><div><input class="inp mono" id="bk-dir" autocomplete="off" placeholder=""><p class="hint">Лучше на другом диске или в сетевой папке: если откажет диск с базой, копия на нём же пропадёт вместе с ней. В копиях позиции: папка должна быть доступна только вам — новая папка в корне диска (D:\\DeskBackup) или сетевая папка обычно открыта всем пользователям компьютера, проверьте её «Свойства → Безопасность». Пусто — папка backups в папке данных (доступна только вам).</p></div>' +
      '<span></span><div class="bk-row"><button class="btn" id="bk-save" type="button">Сохранить расписание</button><span id="bk-save-msg" class="muted"></span></div>' +
      '</div></div>' +
      '<div class="card"><div class="hd"><h2>Копии</h2><span class="mut3" id="bk-sum"></span><span class="sp"></span>' +
      '<button class="ibtn" id="bk-run" type="button">Создать копию сейчас</button>' +
      '<button class="ibtn" id="bk-check" type="button">Проверить восстановление</button></div>' +
      '<div class="bd"><div id="bk-last" class="muted"></div><div class="bk-job" id="bk-job" hidden></div>' +
      '<div class="bk-tw" id="bk-tw" hidden><table class="t"><thead><tr><th>Создана</th><th>Вид</th><th class="n">Размер</th><th class="n">Строк</th><th class="n">Прогонов</th><th></th></tr></thead><tbody id="bk-rows"></tbody></table></div>' +
      '<p class="hint" id="bk-where"></p></div></div>' +
      '<div class="card"><div class="hd"><h2>Проверка восстановления</h2></div><div class="bd"><div id="bk-chk" class="muted">Ещё не проверялось.</div>' +
      '<p class="hint">Проверка разворачивает копию во временную базу рядом с рабочей, сверяет число строк каждой таблицы с записанным при создании копии и пробует обновить её схему до этой версии программы; потом временную базу удаляет. Рабочие данные не меняются. Проверяйте хотя бы раз в месяц и после обновления программы.</p></div></div>' +
      '<div class="card"><div class="hd"><h2>Восстановить данные из копии</h2></div><div class="bd" id="bk-howto"></div></div>';
    $('bk-on').addEventListener('change', function () { $('bk-on-l').textContent = $('bk-on').checked ? 'включена' : 'выключена'; });
    $('bk-save').addEventListener('click', save);
    $('bk-run').addEventListener('click', function () { start('/api/v1/backup/run', {}); });
    $('bk-check').addEventListener('click', function () { start('/api/v1/backup/check', {}); });
    howto(null);
  }
  function $(id) { return document.getElementById(id); }

  function howto(name) {
    var file = name || 'ИМЯ-ФАЙЛА.dump';
    var box = $('bk-howto'); box.textContent = '';
    box.appendChild(el('p', { class: 'muted' }, 'Заменить рабочие данные копией можно только из командной строки и при остановленном сервере: ' +
      'эту страницу отдаёт тот же сервер, а базу, с которой он работает, подменять нельзя. Сначала программа сама проверит копию во временной базе, ' +
      'потом спросит подтверждение, сделает копию текущих данных («перед восстановлением») и только тогда поставит данные из копии на место рабочих. ' +
      'Прежняя рабочая база остаётся рядом до следующего восстановления.'));
    var ol = el('ol', { class: 'bk-steps' });
    var li1 = el('li'); li1.appendChild(document.createTextNode('Остановить Desk Monitor: двойной щелчок по ')); li1.appendChild(el('code', null, 'stop.bat'));
    li1.appendChild(document.createTextNode(' в папке программы (или закрыть окно start.bat).')); ol.appendChild(li1);
    var li2 = el('li'); li2.appendChild(document.createTextNode('Открыть командную строку (Пуск → cmd) в папке программы, например '));
    li2.appendChild(el('code', null, 'cd /d C:\\DeskMonitor')); li2.appendChild(document.createTextNode(', и выполнить:'));
    li2.appendChild(el('div', { class: 'mono', style: 'margin:6px 0' }, 'desk.bat restore ' + file + ' --replace'));
    var ul = el('ul', { class: 'bk-args' });
    [['desk.bat', 'команды Desk Monitor из этой папки;'],
     ['restore', 'восстановление из копии; без --replace — только проверка, рабочие данные не трогаются;'],
     [file, 'файл копии из таблицы выше (или полный путь к файлу; без имени — самая новая копия);'],
     ['--replace', 'после проверки заменить рабочие данные; программа спросит подтверждение — напечатайте restore и Enter.']]
      .forEach(function (a) { var li = el('li'); li.appendChild(el('code', null, a[0])); li.appendChild(document.createTextNode(' — ' + a[1])); ul.appendChild(li); });
    li2.appendChild(ul); ol.appendChild(li2);
    var li3 = el('li'); li3.appendChild(document.createTextNode('Запустить Desk Monitor: ')); li3.appendChild(el('code', null, 'start.bat'));
    li3.appendChild(document.createTextNode('. Подробно — START_HERE.txt, раздел 7а.')); ol.appendChild(li3);
    box.appendChild(ol);
  }

  function badge(s) {
    var b = $('bk-st'); if (!b) return;
    var last = s.last || {};
    var text, cls;
    if (!s.enabled) { text = 'выключено'; cls = 'warn'; }
    else if (last.ok === false && (!s.backups.length || last.at > (s.backups[0] || {}).created)) { text = 'ошибка копии'; cls = 'bad'; }
    else if (s.stale) { text = s.backups.length ? 'копия устарела' : 'копий нет'; cls = 'warn'; }
    else { text = 'копия ' + hours(s.newest_age_hours); cls = 'ok'; }
    if (s.same_disk && cls === 'ok') cls = 'warn';
    b.textContent = text; b.className = 'badge st ' + cls;
    var dot = document.querySelector('.dot[data-nav="backup"]');
    if (dot) dot.className = 'dot ' + cls;
  }

  function draw(s) {
    S.status = s;
    badge(s);
    $('bk-err').hidden = s.tools; $('bk-err').textContent = s.tools ? '' : 'Копии не создаются: ' + (s.tools_error || 'нет pg_dump');
    $('bk-disk').hidden = !s.same_disk;
    $('bk-disk').textContent = s.same_disk ? 'Копии лежат на том же диске, что и база: если диск откажет, пропадут и данные, и копии. ' +
      'Укажите в «Папке» другой диск (например D:\\DeskBackup) или сетевую папку.' : '';
    var open = s.dir_private === false;  // Windows: outside the user's profile; POSIX: the folder is readable by others
    $('bk-priv').hidden = !open;
    $('bk-priv').textContent = open ? 'В копиях позиции и данные прогонов, а папку копий, похоже, могут читать и другие пользователи ' +
      'этого компьютера или сетевой папки. Выберите папку, которую читаете только вы (свойства папки → «Безопасность»), ' +
      'или оставьте поле «Папка» пустым: папка backups в папке данных доступна только вам.' : '';
    $('bk-next').textContent = s.enabled ? (s.next_at ? 'следующая: ' + when(s.next_at) : '') : 'выключено';
    var on = $('bk-on'); if (document.activeElement !== on && !S.dirty) { on.checked = s.enabled; $('bk-on-l').textContent = s.enabled ? 'включена' : 'выключена'; }
    $('bk-dir').placeholder = s.dir_set ? '' : s.dir;
    $('bk-sum').textContent = s.backups.length ? s.backups.length + ' шт., ' + mb(s.total_size) : '';
    var last = s.last;
    $('bk-last').textContent = last ? ('Последняя попытка ' + when(last.at) + ': ' + (last.ok ? 'готово, ' + last.file + ', ' + mb(last.size) + ', ' + last.seconds + ' с'
      + (last.pruned && last.pruned.length ? '; удалено старых: ' + last.pruned.length : '') : 'ошибка — ' + last.error)) : 'Копий ещё не было.';
    $('bk-last').className = last && last.ok === false ? 'err' : 'muted';
    $('bk-tw').hidden = !s.backups.length;
    var tb = $('bk-rows'); tb.textContent = '';
    s.backups.forEach(function (b) {
      var tr = el('tr');
      tr.appendChild(el('td', { class: 'mono' }, when(b.created)));
      tr.appendChild(el('td', null, KIND[b.kind] || b.kind));
      tr.appendChild(el('td', { class: 'n' }, mb(b.size)));
      tr.appendChild(el('td', { class: 'n' }, num(b.rows)));
      tr.appendChild(el('td', { class: 'n' }, num(b.runs)));
      var td = el('td');
      var btn = el('button', { class: 'lnk', type: 'button', title: 'Развернуть во временную базу и сверить' }, 'проверить');
      btn.addEventListener('click', function () { start('/api/v1/backup/check', { file: b.name }); });
      var how = el('button', { class: 'lnk', type: 'button', title: 'Команда восстановления из этой копии', style: 'margin-left:10px' }, 'как восстановить');
      how.addEventListener('click', function () { howto(b.name); $('bk-howto').scrollIntoView({ block: 'start', behavior: 'smooth' }); });
      td.appendChild(btn); td.appendChild(how); tr.appendChild(td);
      tb.appendChild(tr);
    });
    $('bk-where').textContent = 'Папка копий: ' + s.dir + (s.store === 'external' ? ' (база — внешний PostgreSQL, DESK_STORE_URL)' : '');
    var c = s.last_check, box = $('bk-chk'); box.textContent = '';
    if (c) {
      box.className = c.ok ? '' : 'err';
      box.appendChild(el('div', null, when(c.started) + ' — ' + c.file + ': ' + (c.ok ? 'восстановление проходит, ' + num(c.rows) + ' строк в ' + c.tables + ' таблицах совпали с копией (' + c.seconds + ' с)' : 'НЕ ПРОШЛА')));
      var probs = (c.problems || []).concat((c.warnings || []).map(function (w) { return 'предупреждение: ' + w; }));
      if (probs.length) { var ul = el('ul', { class: 'bk-prob' }); probs.forEach(function (p) { ul.appendChild(el('li', null, p)); }); box.appendChild(ul); }
    } else { box.className = 'muted'; box.textContent = 'Ещё не проверялось.'; }
    if (s.last_restore) box.appendChild(el('div', { class: 'muted', style: 'margin-top:6px' }, 'Последнее восстановление: ' + when(s.last_restore.at) + ' из ' + s.last_restore.file + ' (' + s.last_restore.by + '); данные до него — ' + s.last_restore.pre_restore));
    job(s.job);
  }

  function job(j) {
    var box = $('bk-job'), busy = !!(j && j.state === 'running');
    $('bk-run').disabled = busy || !S.status.tools; $('bk-check').disabled = busy || !S.status.backups.length;
    if (!j) { box.hidden = true; return; }
    box.hidden = false; box.textContent = '';
    var what = j.kind === 'run' ? 'Копия' : 'Проверка восстановления ' + j.file;
    var res = j.result || {};
    var cls = j.state === 'running' ? 'res' : (j.state === 'error' || res.ok === false) ? 'res bad' : 'res ok';
    var line = j.state === 'running' ? what + ': идёт… ' + (j.step || '')
      : j.state === 'error' ? what + ': ошибка — ' + j.error
      : j.kind === 'run' ? 'Копия готова: ' + res.file + ', ' + mb(res.size) + ', ' + num(res.rows) + ' строк'
      : res.ok ? what + ': проходит' : what + ': НЕ ПРОШЛА — ' + (res.problems || []).join('; ');
    box.appendChild(el('div', { class: cls }, line));
    clearTimeout(S.timer);
    if (busy) S.timer = setTimeout(load, 1500);
  }

  function load() {
    return Promise.all([api('GET', '/api/v1/backup/status'), api('GET', '/api/v1/settings').catch(function () { return {}; })])
      .then(function (r) {
        var cfg = r[1] || {};
        S.stored = cfg; S.overrides = cfg.env_overrides || [];
        if (!S.dirty) {
          $('bk-time').value = cfg.backup_time || ''; $('bk-keep').value = cfg.backup_keep == null ? '' : cfg.backup_keep;
          $('bk-dir').value = cfg.backup_dir || '';
        }
        ['backup', 'backup_time', 'backup_keep', 'backup_dir'].forEach(function (k) {
          var id = { backup: 'bk-on', backup_time: 'bk-time', backup_keep: 'bk-keep', backup_dir: 'bk-dir' }[k];
          if (S.overrides.indexOf(k) >= 0) $(id).title = 'задано переменной окружения DESK_' + k.toUpperCase() + ': изменение здесь не подействует';
        });
        draw(r[0]);
      })
      .catch(function (e) { $('bk-err').hidden = false; $('bk-err').textContent = 'Не удалось получить состояние копий: ' + (e.message || e); });
  }

  function save() {
    var keep = $('bk-keep').value.trim(), time = $('bk-time').value.trim(), dir = $('bk-dir').value.trim();
    if (keep && !/^\d+$/.test(keep)) { $('bk-save-msg').className = 'err'; $('bk-save-msg').textContent = 'Хранить копий — целое число от 1'; return; }
    var body = { backup: $('bk-on').checked, backup_time: time || null, backup_keep: keep ? parseInt(keep, 10) : null, backup_dir: dir || null };
    $('bk-save').disabled = true; $('bk-save-msg').className = 'muted'; $('bk-save-msg').textContent = 'сохраняю…';
    api('PUT', '/api/v1/settings', body).then(function () {
      S.dirty = false; $('bk-save-msg').className = 'okmsg'; $('bk-save-msg').textContent = 'Сохранено, действует сразу.';
      return load();
    }).catch(function (e) { $('bk-save-msg').className = 'err'; $('bk-save-msg').textContent = 'Не сохранено: ' + (e.message || e); })
      .then(function () { $('bk-save').disabled = false; });
  }

  function start(url, body) {
    api('POST', url, body).then(function (j) { job(j); setTimeout(load, 800); })
      .catch(function (e) { job({ kind: url.indexOf('check') >= 0 ? 'check' : 'run', state: 'error', error: e.message || String(e), file: body.file || '' }); });
  }

  function mount(slot) {
    if (root) return load();
    root = slot;
    build();
    ['bk-time', 'bk-keep', 'bk-dir', 'bk-on'].forEach(function (id) { $(id).addEventListener('input', function () { S.dirty = true; }); });
    return load();
  }
  window.DeskBackup = { mount: mount, reload: load };

  function boot() {
    var slot = document.querySelector('[data-desk-backup]');
    if (!slot) return;
    /* раздел «backup» в навигации settings.html: массив SECTIONS и функция show() — из основного скрипта страницы */
    if (typeof SECTIONS !== 'undefined' && SECTIONS.indexOf('backup') < 0) {
      SECTIONS.push('backup');
      if (location.hash === '#backup' && typeof show === 'function') show(location.hash);
    }
    if (/^https?:$/.test(location.protocol)) mount(slot);
    window.addEventListener('hashchange', function () { if (location.hash === '#backup' && root) load(); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot); else boot();
})();
