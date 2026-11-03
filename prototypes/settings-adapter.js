/*
 * Инструменты адаптера на странице настроек (TDM#421): владелец не запускает командную строку и не правит JSON.
 *  - «Состояние адаптера» — необязательная функция адаптера status(ctx) (08 §3): его панели (уровень, строки, таблица).
 *    Запрашивается при открытии раздела «Адаптер и источники» и по «Обновить»; нет функции — карточки нет.
 *  - «Действия адаптера» — describe()["actions"] и run_action (08 §3, §4): карточка на действие с описанием, формой
 *    параметров (тот же редактор по JSON Schema, SF.Editor), «Запустить», ход, итог (строки, таблица), записанные файлы
 *    и что с ними сделали входящие (принят, отклонён, …).
 *  - «Проверка адаптера» — комплект проверки (desk adapter check, 08 §8) в фоне: папки входящих флажками и своя папка,
 *    сводка таблицей (версии, отпечаток, по источнику и дате: файлы, строки, принято / отклонено, коды замечаний с
 *    числом — без значений ячеек), «Скопировать сводку» (текст для чата), последний результат — до следующего запуска.
 *  - Результаты не висят на странице (TDM#552): итог действия и подробности проверки — строкой «Последний запуск»,
 *    раскрываются по щелчку (сразу после запуска на этой странице — раскрыты); длинные таблицы — DeskLongTable
 *    (первые строки, «Показать все», поиск, CSV); технические сведения — в «Подробностях».
 * Данные: /api/v1/adapter/check, /api/v1/adapter/status, /api/v1/adapter/actions (api/adapter_routes.py).
 * Чистая часть (DeskAdapterTools._test) проверяется tests/js/settings_adapter.test.js.
 */
(function (root) {
  'use strict';
  var A = {};
  var POLL_MS = 1500;

  /* ---------- чистая часть ---------- */
  A.LEVEL = { ok: ['ok', 'в порядке'], info: ['', 'к сведению'], warning: ['warn', 'внимание'], error: ['bad', 'ошибка'] };
  A.INBOX = {
    accepted: ['ok', 'принят'], partially_accepted: ['warn', 'принят частично'], rejected: ['bad', 'отклонён'],
    superseded: ['', 'заменён более новым'], duplicate: ['', 'такой уже был'], stale_copy: ['warn', 'копия старого отчёта'],
    ignored: ['warn', 'адаптер его не узнал'], ignored_too_old: ['', 'слишком старый'], match_failed: ['bad', 'ошибка распознавания'],
    failed: ['bad', 'ошибка чтения'], timed_out: ['bad', 'не прочитан вовремя'], waiting_calendar: ['warn', 'ждёт календарь'],
    waiting: ['', 'ждёт']
  };
  A.STATE = { running: ['warn', 'идёт'], done: ['ok', 'готово'], failed: ['bad', 'ошибка'], timeout: ['bad', 'не успело'] };
  A.WHY = {  // почему задание не удалось — простыми словами (error_code)
    timeout: 'не уложилось в отведённое время', failed: 'адаптер завершился с ошибкой', bad_result: 'адаптер вернул ответ неверного вида',
    write: 'файлы не записаны', interrupted: 'сервер перезапускался во время работы', internal: 'внутренняя ошибка сервера'
  };
  A.UNITS = { accepted: 'принято', rejected: 'отклонено', superseded: 'заменено' };
  A.isNum = function (v) { return typeof v === 'number' && isFinite(v); };
  A.n = function (v) { return v == null ? '—' : A.isNum(v) ? v.toLocaleString('ru-RU') : String(v); };
  // доля замечаний от строк, когда оба числа известны (счёт меньше 10 комплект пишет «<10»)
  A.share = function (count, rows) {
    if (!A.isNum(count) || !A.isNum(rows) || rows <= 0) return '';
    var p = 100 * count / rows;
    return (p < 0.1 ? '<0,1' : p.toFixed(p < 10 ? 1 : 0).replace('.', ',')) + '% строк';
  };
  A.when = function (s) { return s ? new Date(s).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'medium' }) : ''; };
  A.seconds = function (started, now) { return started ? Math.max(0, Math.round(((now || Date.now()) - new Date(started).getTime()) / 1000)) : 0; };
  A.kb = function (b) { return b == null ? '' : b < 1024 ? b + ' Б' : b < 1048576 ? Math.round(b / 1024) + ' КБ' : (b / 1048576).toFixed(1).replace('.', ',') + ' МБ'; };
  A.failText = function (j) { return (A.WHY[j.error_code] || 'не удалось') + (j.error ? ' (' + j.error + ')' : ''); };
  // сводка комплекта: шапка (что и чем проверено) и строки по источнику и дате отчёта
  A.kitHead = function (r) {
    return [['Бэкенд', r.backend], ['SPI', r.spi], ['Контракт', r.contract], ['Адаптер', (r.name || '—') + ' ' + (r.version || '')],
      ['Отпечаток', r.fingerprint ? String(r.fingerprint).slice(0, 16) + '…' : '—'], ['Справочник инструментов', r.instruments],
      ['Календарь', r.calendar]];
  };
  A.kitFiles = function (r) {
    var f = r.files || {};
    return 'файлов узнано: ' + A.n(f.matched) + ', не узнано: ' + A.n(f.ignored) + ', ошибок распознавания: ' + A.n(f.match_failed) +
      (r.match_seconds != null ? ' · распознавание ' + String(r.match_seconds).replace('.', ',') + ' с' : '');
  };
  A.kitRows = function (r) {
    return (r.sources || []).map(function (s) {
      var units = Object.keys(s.units || {}).map(function (k) { return (A.UNITS[k] || k) + ' ' + A.n(s.units[k]); }).join(', ') || '—';
      var share = s.unmapped_share_pct ? Object.keys(s.unmapped_share_pct).map(function (c) { return String(s.unmapped_share_pct[c]).replace('.', ',') + '% номинала ' + c; }).join(', ') : '';
      return { source: s.source, report_date: s.report_date, valid_date: s.valid_date || 'ждёт календарь', files: A.n(s.files), rows: A.n(s.rows),
        units: units, statuses: Object.keys(s.statuses || {}).map(function (k) { return k + ' ' + A.n(s.statuses[k]); }).join(', '),
        controls: (s.controls || []).join('/') || '—', unmapped: A.n(s.unmapped) + (share ? ' (' + share + ')' : ''),
        issues: (s.issues || []).map(function (i) { var sh = A.share(i.count, s.rows); return { severity: i.severity, code: i.code, count: A.n(i.count), share: sh }; }) };
    });
  };
  A.inbox = function (x) { return x ? A.INBOX[x.status] || ['', x.status] : ['', 'ещё не прочитан']; };

  /* ---------- DOM ---------- */
  var S = { api: null, toast: null, SF: null, h: null, adapter: null, statusBusy: false, check: null, checkTimer: null, actions: {}, actionTimer: null,
    fresh: {}, checkFresh: false, open: {}, checkOpen: false };  // fresh: итог пришёл на этой странице после «Запустить» — раскрыт один раз; open/checkOpen: как оставил читатель (до перезагрузки страницы)
  function h() { return S.h.apply(null, arguments); }
  function $(id) { return document.getElementById(id); }
  function subdot(sub, cls, title) {  // точка у подраздела «Адаптер и источники» (settings-nav.js)
    if (root.DeskSettingsNav) root.DeskSettingsNav.paintSub('adapter', sub, cls || '', title);
  }
  function badge(cls, text, title) { return h('span', { class: 'badge ' + (cls || ''), title: title || null }, text); }
  function numCell(c) { return { class: A.isNum(c) ? 'n mono' : null, text: c == null ? '—' : A.isNum(c) ? A.n(c) : String(c) }; }
  // длинная таблица — свёрнута (первые строки, «Показать все», поиск, CSV); без settings-longtable.js — целиком
  function longTable(columns, rows, title, td) {
    var LT = root.DeskLongTable;
    if (LT) return LT.el({ h: S.h, columns: columns, rows: rows, title: title, cls: 'k-tbl at-t', td: td || numCell });
    return h('div', { class: 'k-tbl-wrap' }, h('table', { class: 'k-tbl at-t' },
      h('thead', {}, h('tr', {}, columns.map(function (c) { return h('th', {}, c); }))),
      h('tbody', {}, rows.map(function (r, ri) { return h('tr', {}, r.map(function (c, i) { var x = (td || numCell)(c, i, r, ri); return h('td', { class: x.class || null }, x.node != null ? x.node : x.text); })); }))));
  }
  function table(t, title) {
    if (!t) return null;
    return h('div', {}, longTable(t.columns || [], t.rows || [], title),
      t.truncated ? h('div', { class: 'hint' }, 'Показаны первые строки таблицы.') : null);
  }
  // «Последний запуск: …» — свёрнутый итог; open — раскрыт (итог только что пришёл)
  function lastRun(summaryKids, bodyKids, open, cls, onToggle) {
    var d = h('details', { class: 'at-last ' + (cls || '') }, h('summary', {}, summaryKids), h('div', { class: 'at-last-bd' }, bodyKids));
    d.open = !!open;
    if (onToggle) d.addEventListener('toggle', function () { onToggle(d.open); });
    return d;
  }
  function issuesList(list) {
    if (!list || !list.length) return null;
    return h('ul', { class: 'sf-issues' }, list.map(function (i) {
      return h('li', {}, badge(i.severity === 'error' ? 'bad' : i.severity === 'warning' ? 'warn' : '', (S.SF.SEVERITY[i.severity] || i.severity)),
        ' ', S.SF.CODES[i.code] || i.message || i.code, ' ', h('span', { class: 'mono mut3 code' }, i.code + (i.count > 1 ? ' ×' + i.count : '')));
    }));
  }

  // ---- «Состояние адаптера» ----
  A.status = function () {
    var card = $('ad-status'), box = $('ad-status-bd');
    if (!card || !S.adapter || !(S.adapter.optional || []).includes('status')) { if (card) card.hidden = true; return Promise.resolve(); }
    card.hidden = false;
    if (S.statusBusy) return Promise.resolve();
    S.statusBusy = true;
    $('ad-status-at').textContent = 'запрашиваю…';
    return S.api('GET', '/api/v1/adapter/status').then(function (r) {
      box.textContent = '';
      $('ad-status-at').textContent = r.at ? 'на ' + A.when(r.at) : '';
      if (!r.available) { card.hidden = true; return; }
      if (r.error) box.append(h('div', { class: 'note err' }, r.error_code === 'timeout'
        ? 'Адаптер не ответил за ' + r.timeout_s + ' с (предел — «Состояние адаптера» в «Для опытных»).' : 'Адаптер не смог ответить: ' + r.error));
      (Array.isArray(r.panels) ? r.panels : []).forEach(function (p) {
        var lv = A.LEVEL[p.level] || ['', p.level];
        box.append(h('div', { class: 'at-panel lv-' + p.level },
          h('div', { class: 'at-ph' }, badge(lv[0], lv[1]), h('b', {}, p.title)),
          p.lines && p.lines.length ? h('ul', { class: 'at-lines' }, p.lines.map(function (l) { return h('li', {}, l); })) : null,
          table(p.table, p.title)));
      });
      if (!(r.panels || []).length && !r.error) box.append(h('p', { class: 'mut' }, 'Адаптер ничего не сообщил.'));
      var iss = issuesList(r.issues);
      if (iss) box.append(h('h4', { class: 'at-h4' }, 'Замечания адаптера'), iss);
      if ((r.problems || []).length) box.append(h('div', { class: 'note warn' }, 'Часть ответа адаптера неверного вида и не показана: ' + r.problems.join('; ')));
    }, function (e) { box.textContent = ''; box.append(h('div', { class: 'note err' }, e.message)); $('ad-status-at').textContent = ''; })
      .then(function () { S.statusBusy = false; });
  };

  // ---- «Действия адаптера» ----
  A.actions = function () {
    var card = $('ad-actions'), box = $('ad-actions-bd');
    if (!card) return Promise.resolve();
    if (!S.adapter || !(S.adapter.actions || []).length) { card.hidden = true; return Promise.resolve(); }
    card.hidden = false;
    return S.api('GET', '/api/v1/adapter/actions').then(function (r) {
      box.textContent = '';
      $('ad-actions-root').textContent = r.output_root || '';
      (r.actions || []).forEach(function (a) { box.append(actionCard(a, r)); });
      if (r.running) pollAction(r.running.replace(/^action:/, ''));
    }, function (e) { box.textContent = ''; box.append(h('div', { class: 'note err' }, e.message)); });
  };
  function actionCard(a, all) {
    var last = a.job || null, params = last && last.request && last.request.params ? S.SF.clone(last.request.params) : S.SF.empty(a.params);
    var ed = new S.SF.Editor({ schema: a.params, data: params });
    ed.validate();
    var props = Object.keys((a.params && a.params.properties) || {});
    var form = props.length ? ed.renderFields(S.SF.columns(a.params), ed.data, function () { ed.validate(); ed.refresh(); }, []) : null;
    var res = h('div', { class: 'at-res' }), run = h('button', { class: 'btn', type: 'button' }, 'Запустить');
    var st = h('span', { class: 'mut3' });
    var el = h('div', { class: 'at-action', id: 'ad-action-' + a.id },
      h('div', { class: 'at-ah' }, h('b', {}, a.title), a.output_dir ? badge('', 'пишет файлы для входящих', 'Файлы — в папке результатов адаптера; входящие читают их, как любой отчёт') : null),
      a.description ? h('p', { class: 'help' }, a.description) : null, form,
      h('div', { class: 'row', style: 'margin-top:8px' }, run, st), res);
    S.actions[a.id] = { el: el, res: res, run: run, st: st, decl: a, ed: ed };
    run.addEventListener('click', function () {
      var errs = ed.validate().filter(function (i) { return i.severity === 'error'; });
      ed.refresh();
      if (errs.length) { st.className = 'err'; st.textContent = 'Исправьте параметры: ' + errs.map(function (i) { return S.SF.say(i, a.params); }).slice(0, 3).join('; '); return; }
      run.disabled = true; st.className = 'mut3'; st.textContent = 'Запускаю…';
      S.api('POST', '/api/v1/adapter/actions/' + encodeURIComponent(a.id) + '/run', { params: ed.data }).then(function (r) {
        drawAction(a.id, r); pollAction(a.id);
      }, function (e) {
        run.disabled = false; st.className = 'err';
        st.textContent = e.status === 409 ? 'Сейчас идёт другое действие — дождитесь его итога.' : 'Не запущено: ' + e.message;
      });
    });
    drawAction(a.id, { job: last, running: all && all.running === 'action:' + a.id });
    return el;
  }
  function drawAction(id, r) {
    var x = S.actions[id]; if (!x) return;
    var j = r.job, res = x.res;
    res.textContent = '';
    x.run.disabled = !!r.running;
    if (!j) { x.st.className = 'mut3'; x.st.textContent = 'Ещё не запускалось.'; return; }
    var s = A.STATE[j.state] || ['', j.state];
    x.st.className = 'mut3';
    x.st.textContent = '';
    x.st.append(badge(s[0], s[1]), ' ', j.state === 'running' ? 'идёт ' + A.seconds(j.started_at) + ' с (предел ' + (r.timeout_s || '') + ')' :
      'запущено ' + A.when(j.started_at) + (j.seconds != null ? ', ' + String(j.seconds).replace('.', ',') + ' с' : ''));
    if (j.state === 'running') return;
    if (j.state !== 'done') { res.append(h('div', { class: 'note err' }, 'Не удалось: ' + A.failText(j))); return; }
    var out = j.result || {}, body = [];
    // ok: false — обычный итог (нечего записать, данных не хватает): строки адаптера объясняют, прежние файлы остаются
    body.push(h('div', { class: 'verdict ' + (out.ok ? 'ok' : 'warn') }, out.ok ? 'Выполнено.' : 'Не выполнено — причина в строках ниже; прежние файлы действия оставлены.'));
    if ((out.summary || []).length) body.push(h('ul', { class: 'at-lines' }, out.summary.map(function (l) { return h('li', {}, l); })));
    if (out.table) body.push(table(out.table, x.decl.title || id));
    body.push(issuesList(out.issues));
    if (j.written) {
      body.push(h('h4', { class: 'at-h4' }, 'Файлы', h('span', { class: 'mut3' }, ' · папка ', h('span', { class: 'mono' }, j.output_dir || ''))));
      if (!(j.files || []).length) body.push(h('p', { class: 'mut' }, 'Файлов нет: прежние файлы действия убраны.'));
      else body.push(longTable(['Файл', 'Размер', 'Входящие', 'Источник', 'Дата отчёта'],
        j.files.map(function (f) { var o = A.inbox(f.inbox); return [f.name, A.kb(f.bytes), o[1], (f.inbox && f.inbox.source) || '—', (f.inbox && f.inbox.report_date) || '—']; }),
        (x.decl.title || id) + ' файлы', function (c, i, r, ri) {
          if (i === 2) { var f = j.files.filter(function (y) { return y.name === r[0]; })[0], o = A.inbox(f && f.inbox); return { node: badge(o[0], o[1]) }; }
          return { class: i === 1 ? 'n mono' : 'mono', text: c };
        }));
      if (j.inbox_error) body.push(h('p', { class: 'hint' }, 'Что сделали входящие, не узнать: ' + j.inbox_error));
      if (j.scan && j.scan.busy) body.push(h('p', { class: 'hint' }, 'Входящие были заняты: файлы прочтёт следующий цикл автообновления.'));
      else if (j.scan && j.scan.error) body.push(h('p', { class: 'hint' }, j.scan.error));
    }
    // строка «Последний запуск»: когда, итог, первая строка сводки; подробности — по щелчку
    // раскрыт: итог только что пришёл (один раз) или читатель сам раскрыл; свёрнутый им — не раскрывается при «Обновить статус»
    var openNow = !!S.fresh[id] || S.open[id] === true;
    if (S.fresh[id]) { S.fresh[id] = false; S.open[id] = true; }
    var first = (out.summary || [])[0], nFiles = j.written ? (j.files || []).length : null;
    res.append(lastRun([h('b', {}, 'Последний запуск'), ' ', A.when(j.finished_at || j.started_at), ' · ',
      h('span', { class: out.ok ? 'okmsg' : 'warnmsg' }, out.ok ? 'выполнено' : 'не выполнено'),
      first ? h('span', { class: 'mut3' }, ' · ' + first) : null,
      out.table ? h('span', { class: 'mut3' }, ' · таблица: ' + ((out.table.rows || []).length).toLocaleString('ru-RU') + ' стр.') : null,
      nFiles != null ? h('span', { class: 'mut3' }, ' · файлов: ' + nFiles) : null,
      h('span', { class: 'lnk at-open', 'aria-hidden': 'true' })], body, openNow, '', function (v) { S.open[id] = v; }));
  }
  function pollAction(id) {
    clearTimeout(S.actionTimer);
    S.actionTimer = setTimeout(function () {
      S.api('GET', '/api/v1/adapter/actions/' + encodeURIComponent(id)).then(function (r) {
        if (!r.running) S.fresh[id] = true;  // итог пришёл здесь — раскрыт
        drawAction(id, r);
        if (r.running) pollAction(id);
        else if (r.job && r.job.state === 'done' && S.toast) S.toast('Действие «' + (r.action.title || id) + '»: готово', 'ok');
      }, function () { pollAction(id); });
    }, POLL_MS);
  }

  // ---- «Проверка адаптера» ----
  A.check = function () {
    var box = $('ad-check-opts');
    if (!box) return Promise.resolve();
    return S.api('GET', '/api/v1/adapter/check').then(function (r) {
      S.check = r;
      var picked = r.job && r.job.request ? r.job.request.dirs : null;
      box.textContent = '';
      r.options = Array.isArray(r.options) ? r.options : [];
      r.options.forEach(function (o) {
        var on = picked ? picked.indexOf(o.path) >= 0 : o.exists;
        box.append(h('label', { class: 'chk at-dir' + (o.exists ? '' : ' mut3') },
          h('input', { type: 'checkbox', 'data-dir': o.path, checked: on && o.exists, disabled: !o.exists }),
          h('span', { class: 'mono' }, o.path), o.label ? h('span', { class: 'mut3' }, ' — ' + o.label) : null, o.exists ? null : h('span', { class: 'mut3' }, ' (папки нет)')));
      });
      if (!r.options.length) box.append(h('p', { class: 'mut' }, 'Папок с отчётами нет — укажите папку ниже.'));
      var known = r.options.map(function (o) { return o.path; });
      if (picked) $('ad-check-dir').value = picked.filter(function (d) { return known.indexOf(d) < 0; }).join('; ');
      if (r.job && r.job.request) $('ad-check-stub').checked = !!r.job.request.stub_ctx;
      drawCheck(r);
      if (r.running) pollCheck();
    }, function (e) { box.textContent = ''; box.append(h('div', { class: 'note err' }, e.message)); });
  };
  function drawCheck(r) {
    var res = $('ad-check-res'), st = $('ad-check-st'), j = r.job;
    $('ad-check-run').disabled = !!r.running;
    $('ad-check-copy').disabled = !(j && j.text);
    res.textContent = ''; st.textContent = '';
    if (!j) { st.textContent = 'Ещё не запускалась.'; return; }
    var s = A.STATE[j.state] || ['', j.state];
    st.append(badge(s[0], s[1]), ' ', j.state === 'running' ? 'идёт ' + A.seconds(j.started_at) + ' с (предел ' + r.timeout_s + ' с)'
      : 'проверено ' + A.when(j.finished_at || j.started_at) + (j.seconds != null ? ', ' + String(j.seconds).replace('.', ',') + ' с' : '') +
        ' · папок: ' + ((j.request && j.request.dirs) || []).length);
    if (j.state === 'running') return;
    if (j.state !== 'done' || !j.report) { subdot('check', 'bad', 'проверка не удалась'); res.append(h('div', { class: 'note err' }, 'Проверка не удалась: ' + A.failText(j))); return; }
    var rep = j.report, body = [];
    subdot('check', rep.ok ? 'ok' : 'bad', rep.ok ? 'адаптер соответствует контракту' : 'адаптер не прошёл проверку');
    // на виду — вердикт и нарушения; остальное (версии, таблица по источникам, текст) — «Подробности проверки» по щелчку
    res.append(h('div', { class: 'verdict ' + (rep.ok ? 'ok' : 'bad') }, rep.ok ? 'Адаптер соответствует контракту.' : 'Адаптер не прошёл проверку' + (rep.load_error ? ': не загружается (' + rep.load_error + ')' : ' — см. «Нарушения» ниже.')));
    var probs = Object.keys(rep.problems || {}), notes = Object.keys(rep.notes || {});
    if (probs.length) res.append(h('div', { class: 'note err', style: 'margin-top:10px' }, h('b', {}, 'Нарушения контракта: '), probs.map(function (k) { return k + ' ' + A.n(rep.problems[k]); }).join(', ')));
    body.push(h('div', { class: 'kv', style: 'margin-top:4px' }, A.kitHead(rep).map(function (kv) { return h('div', {}, h('div', { class: 'l' }, kv[0]), h('div', { class: 'v mono' }, kv[1] == null ? '—' : String(kv[1]))); })));
    var rows = rep.load_error ? [] : A.kitRows(rep);
    if (!rep.load_error) {
      body.push(h('p', { class: 'mut', style: 'margin:10px 0 6px' }, A.kitFiles(rep)));
      if (rep.reference && Object.keys(rep.reference).length) {
        body.push(h('p', { class: 'mut', style: 'margin:0 0 6px' }, 'Справочник адаптера: ' + Object.keys(rep.reference).map(function (k) { return k + ' ' + A.n(rep.reference[k]); }).join(', ')));
        body.push(issuesList(rep.reference_issues));
      }
      if (rows.length) {
        var arr = rows.map(function (x) {
          var r = [x.source, x.report_date + ' → ' + x.valid_date, x.files, x.rows, x.units, x.controls, x.unmapped,
            x.issues.map(function (i) { return i.code + ' ' + i.count + (i.share ? ' (' + i.share + ')' : ''); }).join(', ') || '—'];
          r.x = x; return r;
        });
        body.push(longTable(['Источник', 'Дата отчёта → дата данных', 'Файлов', 'Строк', 'Единицы', 'Контроль', 'Без бумаги', 'Замечания'], arr, 'проверка адаптера', function (c, i, r) {
          var x = r.x;
          if (i === 7) return { node: x.issues.length ? x.issues.map(function (it) {
            return h('div', {}, badge(it.severity === 'error' ? 'bad' : it.severity === 'warning' ? 'warn' : '', it.code), ' ' + it.count + (it.share ? ' (' + it.share + ')' : ''));
          }) : '—' };
          if (i === 4) return { node: h('span', { title: x.statuses }, c) };
          return { class: i === 2 || i === 3 ? 'n mono' : i === 6 ? null : 'mono', text: c };
        }));
      } else body.push(h('p', { class: 'mut' }, 'Ни одного файла адаптер не узнал.'));
      if (notes.length) body.push(h('p', { class: 'hint' }, 'Заметки: ' + notes.map(function (k) { return k + ' ' + A.n(rep.notes[k]); }).join(', ')));
    }
    body.push(h('details', { class: 'more' }, h('summary', {}, 'Текст сводки'), h('pre', { class: 'at-pre mono' }, j.text)));
    var openNow = !!S.checkFresh || S.checkOpen === true;
    if (S.checkFresh) { S.checkFresh = false; S.checkOpen = true; }
    var warnN = rows.reduce(function (s, x) { return s + x.issues.length; }, 0);
    res.append(lastRun([h('b', {}, 'Подробности проверки'), h('span', { class: 'mut3' }, ' · источников: ' + new Set(rows.map(function (x) { return x.source; })).size + ', строк по датам: ' + rows.length +
      (warnN ? ', кодов замечаний: ' + warnN : '') + ' · версии, таблица по источникам, текст сводки'), ' ', h('span', { class: 'lnk at-open', 'aria-hidden': 'true' })],
      body, openNow, 'at-check', function (v) { S.checkOpen = v; }));
  }
  function pollCheck() {
    clearTimeout(S.checkTimer);
    S.checkTimer = setTimeout(function () {
      S.api('GET', '/api/v1/adapter/check').then(function (r) {
        S.check = r; if (!r.running) S.checkFresh = true;  // итог пришёл здесь — подробности раскрыты
        drawCheck(r);
        if (r.running) pollCheck(); else if (S.toast) S.toast('Проверка адаптера: ' + (r.job && r.job.report ? (r.job.report.ok ? 'OK' : 'есть нарушения') : 'не удалась'), r.job && r.job.report && r.job.report.ok ? 'ok' : 'err');
      }, function () { pollCheck(); });
    }, POLL_MS);
  }
  A.copyText = function (text) {
    if (root.navigator && navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (ok, fail) {
      var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.append(ta); ta.select();
      try { if (document.execCommand('copy')) ok(); else fail(new Error('копирование недоступно')); } catch (e) { fail(e); } finally { ta.remove(); }
    });
  };
  function startCheck() {
    var dirs = Array.prototype.map.call(document.querySelectorAll('#ad-check-opts input[data-dir]:checked'), function (c) { return c.getAttribute('data-dir'); });
    $('ad-check-dir').value.split(';').map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (d) { if (dirs.indexOf(d) < 0) dirs.push(d); });
    var st = $('ad-check-st');
    if (!dirs.length) { st.textContent = ''; st.append(h('span', { class: 'err' }, 'Отметьте папку или укажите свою.')); return; }
    $('ad-check-run').disabled = true;
    S.api('POST', '/api/v1/adapter/check', { dirs: dirs, stub_ctx: $('ad-check-stub').checked }).then(function (r) { S.check = r; drawCheck(r); pollCheck(); },
      function (e) { $('ad-check-run').disabled = false; st.textContent = ''; st.append(h('span', { class: 'err' }, e.status === 409 ? 'Проверка уже идёт.' : 'Не запущено: ' + e.message)); });
  }

  // opts: {api, toast, SF}; вызывается один раз, после settings-forms.js
  A.mount = function (opts) {
    S.api = opts.api; S.toast = opts.toast; S.SF = opts.SF; S.h = opts.SF.h;
    if ($('ad-status-refresh')) $('ad-status-refresh').addEventListener('click', function () { A.status(); });
    if ($('ad-check-run')) $('ad-check-run').addEventListener('click', startCheck);
    if ($('ad-check-copy')) $('ad-check-copy').addEventListener('click', function () {
      var j = S.check && S.check.job;
      if (!j || !j.text) return;
      A.copyText(j.text).then(function () { if (S.toast) S.toast('Сводка проверки скопирована: её можно вставить в чат', 'ok'); },
        function (e) { if (S.toast) S.toast('Не скопировано: ' + e.message, 'err'); });
    });
  };
  // адаптер загружен или перезагружен (GET /api/v1/adapter): что он умеет — status, actions
  A.update = function (a) { S.adapter = a; A.actions(); A.check(); };
  // раздел «Адаптер и источники» открыт: состояние адаптера — заново
  A.shown = function () { return A.status(); };

  A._test = A;
  root.DeskAdapterTools = A;
})(typeof window !== 'undefined' ? window : globalThis);
