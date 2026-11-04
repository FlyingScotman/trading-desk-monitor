/*
 * «ОВП: типы строк отчёта» (TDM#554): один экран, на котором владелец включает новую ОВП — типы строк последних
 * отчётов ОВП, у каждого выпадающий список вида дериватива, подсказка вида, предупреждения на месте и одна кнопка
 * «Сохранить и пересчитать». Ничего не вводится руками, командная строка не нужна.
 *  - Данные — результат действия адаптера, объявившего choices «ocp_derivative_types» (08 §4): строка на тип (число
 *    строк, доля строк позже даты отчёта, подтипы — по раскрытию), текущий вид, предложенный вид с причиной, «новый».
 *    Нет результата, он устарел (файл настроек менялся после него) или старше MAX_AGE_H — действие запускается само.
 *  - Подсказка: у типа без вида, с будущими строками и с предложенным видом, выпадающий список сразу стоит на
 *    предложенном виде с пометкой «предложено — проверьте»; кнопка записывает его, только когда владелец её нажал.
 *  - Предупреждения: тип с будущими строками, оставленный «не дериватив», когда его вид (предложенный) не входит в
 *    отчёт (risky_unset адаптера) или назначен другому типу, — «риск двойного счёта»; тип, которого раньше не было, —
 *    «новый».
 *  - «Сохранить и пересчитать»: POST /api/v1/adapter/choices/ocp_derivative_types {values, replay: true} — запись
 *    соответствия в настройки адаптера (как форма вкладки, с историей правок) и перечитывание последнего отчёта ОВП
 *    (/api/v1/inbox/replay), затем ожидание пересчёта автообновлением (/api/v1/autopilot/status); итог — «Готово, ОВП
 *    пересчитана».
 * Встраивание — одна точка: элемент с атрибутом data-ocp-types и DeskOcpTypes.mount(элемент, {api, toast});
 * DeskOcpTypes.shown() — экран открыт (данные — заново). Стили — свои, с префиксом ot-, вставляются один раз.
 * Чистая часть (DeskOcpTypes._test) проверяется tests/js/settings_ocp_types.test.js.
 */
(function (root) {
  'use strict';
  var T = {};
  var KIND = 'ocp_derivative_types';
  var URL = '/api/v1/adapter/choices/' + KIND;
  var POLL_MS = 1500, MAX_AGE_H = 6, RECALC_WAIT_MS = 10 * 60 * 1000;

  /* ---------- чистая часть ---------- */
  T.esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  T.n = function (v) { return v == null ? '—' : typeof v === 'number' ? v.toLocaleString('ru-RU') : String(v); };
  T.pct = function (v) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    var p = v * 100;
    return (p > 0 && p < 1 ? '<1' : String(Math.round(p))) + '%';
  };
  T.types = function (n) {
    var m10 = n % 10, m100 = n % 100;
    return n + ' ' + (m10 === 1 && m100 !== 11 ? 'тип' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'типа' : 'типов');
  };
  T.dm = function (s) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] + '.' + m[1] : String(s || ''); };
  T.title = function (choices, v) {
    var o = (choices.options || []).filter(function (x) { return x.value === v; })[0];
    return o ? o.title : String(v);
  };
  // начальный выбор: сохранённый вид; у типа без вида с будущими строками — предложенный (помечается)
  T.initial = function (choices) {
    var out = {};
    (choices.rows || []).forEach(function (r) {
      out[r.key] = r.value != null ? r.value : (r.suggested != null && r.future > 0 ? r.suggested : null);
    });
    return out;
  };
  T.saved = function (choices) {
    var out = {};
    (choices.rows || []).forEach(function (r) { out[r.key] = r.value; });
    return out;
  };
  // что запишет кнопка: только строки, где выбор отличается от сохранённого
  T.changes = function (choices, sel) {
    var out = {};
    (choices.rows || []).forEach(function (r) {
      var v = sel.hasOwnProperty(r.key) ? sel[r.key] : r.value;
      if (v !== r.value) out[r.key] = v;
    });
    return out;
  };
  // предупреждения строки при текущем выборе: [{level, text}]
  T.warnings = function (choices, sel, r) {
    var out = [], v = sel.hasOwnProperty(r.key) ? sel[r.key] : r.value;
    var risky = choices.risky_unset || [];
    if (v == null && r.future > 0 && r.suggested != null) {
      var other = (choices.rows || []).some(function (x) { return x.key !== r.key && (sel.hasOwnProperty(x.key) ? sel[x.key] : x.value) === r.suggested; });
      var name = T.title(choices, r.suggested);
      if (risky.indexOf(r.suggested) >= 0)
        out.push({ level: 'bad', text: 'Риск двойного счёта: похоже на «' + name + '», а этот вид монитор считает отдельно, из позиций. Оставленные в отчёте строки (' + T.n(r.future) + ' с датой позже даты отчёта) войдут ещё раз по номиналу.' });
      else if (other)
        out.push({ level: 'bad', text: 'Риск двойного счёта: вид «' + name + '» назначен другому типу, поэтому монитор добавит PV всех позиций этого вида, а строки этого типа останутся в отчёте по номиналу.' });
    }
    if (v != null && r.value == null && v === r.suggested) out.push({ level: 'warn', text: 'Предложено по подсказке (' + (r.hint || 'по типу') + ') — проверьте.' });
    if (v != null && !r.future) out.push({ level: '', text: 'Строк с датой позже даты отчёта нет: вид сейчас ни на что не влияет.' });
    return out;
  };
  // порядок: сначала типы с будущими строками, затем по числу строк (как дал адаптер)
  T.order = function (rows) {
    return rows.map(function (r, i) { return [r, i]; }).sort(function (a, b) {
      return (b[0].future > 0) - (a[0].future > 0) || a[1] - b[1];
    }).map(function (x) { return x[0]; });
  };
  T.filter = function (choices, sel, rows, mode) {
    if (mode === 'future') return rows.filter(function (r) { return r.future > 0; });
    if (mode === 'attention') return rows.filter(function (r) { return r.new || T.warnings(choices, sel, r).some(function (w) { return w.level; }); });
    return rows;
  };
  // нужно ли запустить действие само: нет результата, файл настроек менялся после него, результат старый
  T.needsRun = function (r, now) {
    if (!r || r.running) return false;
    if (!r.choices) return true;
    if (r.stale) return true;
    var at = r.job && (r.job.finished_at || r.job.started_at);
    return !at || (now || Date.now()) - new Date(at).getTime() > MAX_AGE_H * 3600 * 1000;
  };
  // ход «Сохранить и пересчитать»: {state: 'replay'|'recalc'|'done'|'fail', text}
  T.progress = function (replay, auto, savedAt) {
    if (!replay) return { state: 'replay', text: 'Перечитываю отчёт…' };
    if (replay.state === 'running') return { state: 'replay', text: replay.waiting ? 'Идёт чтение папок — перечитаю отчёт сразу после него…' : 'Перечитываю отчёт за ' + T.dm(replay.request && replay.request.to) + '…' };
    if (replay.state !== 'done') return { state: 'fail', text: 'Отчёт не перечитан: ' + (replay.error || replay.error_code || 'ошибка') + '. Соответствие сохранено; перечитайте кнопкой «Перечитать» (Адаптер и источники).' };
    var revs = (replay.result && replay.result.revisions) || 0;
    if (!revs) return { state: 'done', text: 'Готово: отчёт перечитан, строки в нём не изменились — ОВП прежняя.' };
    if (!auto) return { state: 'recalc', text: 'Отчёт перечитан. Пересчитываю ОВП…' };
    if (auto.enabled === false) return { state: 'done', text: 'Отчёт перечитан. Автообновление выключено: ОВП пересчитается при следующем расчёте.' };
    var fin = replay.finished_at ? new Date(replay.finished_at).getTime() : savedAt;
    var run = auto.latest_run;
    if (run && run.created_at && new Date(run.created_at).getTime() >= fin) return { state: 'done', text: 'Готово, ОВП пересчитана' + (run.label ? ' (расчёт ' + run.label + ')' : '') + '.' };
    if (auto.state !== 'running' && auto.last_cycle_at && new Date(auto.last_cycle_at).getTime() >= fin)
      return { state: 'done', text: 'Отчёт перечитан. Автообновление: ' + (auto.message || 'цикл завершён') + '.' };
    return { state: 'recalc', text: 'Отчёт перечитан. Пересчитываю ОВП…' };
  };

  /* ---------- DOM ---------- */
  var S = { el: null, api: null, toast: null, data: null, sel: {}, open: {}, mode: 'future', timer: null, saving: false, autoRun: false };
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
  function badge(cls, text, title) { return h('span', { class: 'badge ' + (cls || ''), title: title || null }, text); }
  var CSS = '.ot-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 10px}' +
    '.ot-chips{display:flex;gap:6px;flex-wrap:wrap}.ot-chip{border:1px solid var(--line-2);background:var(--panel);color:var(--fg-2);border-radius:999px;padding:2px 10px;font-size:12px;cursor:pointer}' +
    '.ot-chip.on{border-color:var(--accent);color:var(--fg);background:var(--panel-2)}' +
    '.ot-list{border:1px solid var(--line);border-radius:8px;overflow:hidden}' +
    '.ot-row{display:grid;grid-template-columns:28px minmax(0,1.4fr) 64px 88px minmax(260px,1.3fr);gap:8px;align-items:center;padding:8px 10px;border-top:1px solid var(--line)}' +
    '.ot-row:first-child{border-top:0}.ot-row.ot-hd{background:var(--panel-2);color:var(--fg-3);font-size:11.5px;font-weight:600}' +
    '.ot-row.ot-dirty{background:var(--warn-bg)}.ot-name{min-width:0;overflow-wrap:anywhere;font-weight:550}.ot-num{text-align:right;font-variant-numeric:tabular-nums}' +
    '.ot-tg{border:0;background:none;color:var(--fg-2);cursor:pointer;font-size:13px;width:24px;height:24px;border-radius:4px}.ot-tg:hover{background:var(--panel-2)}' +
    '.ot-sel{width:100%;min-width:0}.ot-w{grid-column:2 / -1;font-size:12px;margin:-2px 0 0;line-height:1.35}.ot-w.bad{color:var(--neg)}.ot-w.warn{color:var(--warn)}.ot-w.mut{color:var(--fg-3)}' +
    '.ot-sub{grid-column:2 / -1;display:grid;grid-template-columns:minmax(0,1fr) 70px 92px;gap:2px 8px;font-size:12px;color:var(--fg-2);background:var(--panel-2);border-radius:6px;padding:6px 8px}' +
    '.ot-sub .ot-num{color:var(--fg-2)}.ot-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0 0}' +
    '.ot-steps{margin:10px 0 0}.ot-lbl{display:none;color:var(--fg-3);font-size:11px}' +
    '@media (max-width:700px){.ot-row{grid-template-columns:28px minmax(0,1fr) auto;grid-auto-rows:auto}.ot-row.ot-hd{display:none}' +
    '.ot-row > .ot-num{grid-column:2;text-align:left}.ot-row > .ot-num + .ot-num{grid-column:3;text-align:right}.ot-lbl{display:inline}' +
    '.ot-row > .ot-pick{grid-column:2 / -1}.ot-sub{grid-template-columns:minmax(0,1fr) auto auto}}';
  function style() {
    if (document.getElementById('ot-style')) return;
    var s = document.createElement('style'); s.id = 'ot-style'; s.textContent = CSS; document.head.append(s);
  }

  // «требуют внимания» — точка раздела «ОВП» в меню и строка сводки сверху (settings-nav.js): видны без захода на экран.
  // n — число типов с риском двойного счёта, без вида после подсказки или новых; null/0 — снять
  function signal(n) {
    var DS = root.DeskSettings;
    if (!DS) return;
    var text = n ? 'ОВП: типов строк отчёта требуют внимания — ' + n : '';
    if (DS.dot) DS.dot('ovp', n ? 'warn' : '', text);
    if (DS.status) DS.status('ovp-types', n ? { cls: 'warn', href: '#ovp', k: 'ОВП: типы строк', text: 'требуют внимания: ' + n, title: text } : null);
  }

  function render() {
    var el = S.el, r = S.data;
    el.textContent = '';
    if (!r) { el.append(h('p', { class: 'mut' }, 'Загружаю…')); return; }
    if (r.error) {
      el.append(h('div', { class: 'note ' + (r.status === 404 ? 'warn' : 'err') }, r.status === 404
        ? 'Адаптер не отдаёт типы строк отчёта ОВП для этого экрана. Обновите адаптер: Настройки → «Обновление адаптера». Пока соответствие можно задать на вкладке адаптера «ОВП: отчёт и состав».'
        : 'Не удалось загрузить: ' + r.error));
      return;
    }
    var ch = r.choices, running = r.running;
    if (!ch) signal(0);
    var head = h('div', { class: 'ot-head' });
    var refresh = h('button', { class: 'ibtn', type: 'button', disabled: running || S.saving, onclick: function () { runAction(); } }, running ? 'Читаю отчёты…' : 'Обновить');
    head.append(refresh);
    if (r.job && r.job.finished_at && !running) head.append(h('span', { class: 'mut3' }, 'по отчётам на ' + (ch && ch.replay ? T.dm(ch.replay.to) : '—') + ' · обновлено ' + new Date(r.job.finished_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })));
    el.append(head);
    if (running && !ch) { el.append(h('p', { class: 'mut' }, 'Адаптер читает последние отчёты ОВП…')); return; }
    if (!ch) {
      el.append(h('div', { class: 'note ' + (r.ok === false ? 'warn' : '') }, r.ok === false ? 'Типы строк не получены: ' + (r.summary || []).join('; ') : 'Типов строк ещё нет: нажмите «Обновить».'));
      return;
    }
    var rows = T.order(ch.rows || []);
    var nFuture = rows.filter(function (x) { return x.future > 0; }).length;
    var nAttn = T.filter(ch, S.sel, rows, 'attention').length;
    signal(nAttn);
    var chips = h('div', { class: 'ot-chips', role: 'tablist' });
    [['future', 'С будущими строками ' + nFuture], ['attention', 'Требуют внимания ' + nAttn], ['all', 'Все ' + rows.length]].forEach(function (m) {
      chips.append(h('button', { class: 'ot-chip' + (S.mode === m[0] ? ' on' : ''), type: 'button', role: 'tab', 'aria-selected': S.mode === m[0] ? 'true' : 'false',
        onclick: function () { S.mode = m[0]; render(); } }, m[1]));
    });
    el.append(h('div', { class: 'ot-head' }, chips));
    var list = h('div', { class: 'ot-list' });
    list.append(h('div', { class: 'ot-row ot-hd' }, h('span'), h('span', {}, 'Тип строки'), h('span', { class: 'ot-num' }, 'Строк'),
      h('span', { class: 'ot-num', title: 'Доля строк с датой валютирования позже даты отчёта' }, 'Позже даты'), h('span', {}, 'Вид')));
    var shown = T.filter(ch, S.sel, rows, S.mode);
    if (!shown.length) list.append(h('div', { class: 'ot-row' }, h('span'), h('span', { class: 'mut' }, S.mode === 'attention' ? 'Нечего проверять.' : 'Нет таких типов.')));
    shown.forEach(function (x) { list.append(rowEl(ch, x)); });
    el.append(list);
    (ch.notes || []).forEach(function (n) { el.append(h('p', { class: 'hint' }, n)); });
    var diff = T.changes(ch, S.sel), nd = Object.keys(diff).length;
    var bySuggest = Object.keys(diff).filter(function (k) { var x = (ch.rows || []).filter(function (y) { return y.key === k; })[0]; return x && x.value == null && diff[k] === x.suggested; }).length;
    var save = h('button', { class: 'btn', type: 'button', disabled: S.saving || running || !ch.replay, onclick: saveAll }, 'Сохранить и пересчитать');
    var reset = h('button', { class: 'ibtn', type: 'button', disabled: S.saving || !nd, onclick: function () { S.sel = T.saved(ch); render(); } }, 'Вернуть сохранённое');
    el.append(h('div', { class: 'ot-bar' }, save, reset, h('span', { class: 'mut3' }, nd ? 'Будет записано: ' + T.types(nd) + (bySuggest ? ' (по подсказке — ' + bySuggest + ')' : '') + '; затем перечитаю отчёт за ' + T.dm(ch.replay && ch.replay.to) + ' и пересчитаю ОВП.'
      : 'Изменений нет. Кнопка всё равно перечитает отчёт за ' + T.dm(ch.replay && ch.replay.to) + ' и пересчитает ОВП.')));
    el.append(S.steps = S.steps || h('div', { class: 'ot-steps', 'aria-live': 'polite' }));
  }
  function rowEl(ch, x) {
    var v = S.sel.hasOwnProperty(x.key) ? S.sel[x.key] : x.value;
    var sel = h('select', { class: 'inp ot-sel', 'aria-label': 'Вид для типа ' + x.label, disabled: S.saving, onchange: function () { S.sel[x.key] = this.value === '' ? null : this.value; render(); } },
      (ch.options || []).map(function (o) {
        var t = o.title + (o.value != null && o.value === x.suggested ? ' — предложено' : '');
        return h('option', { value: o.value == null ? '' : o.value, selected: o.value === v }, t);
      }));
    var kids = x.children || [], isOpen = !!S.open[x.key];
    var tg = kids.length ? h('button', { class: 'ot-tg', type: 'button', 'aria-expanded': isOpen ? 'true' : 'false', title: 'Подтипы: ' + kids.length,
      onclick: function () { S.open[x.key] = !isOpen; render(); } }, isOpen ? '▾' : '▸') : h('span');
    var cells = x.cells || {};
    var name = h('span', { class: 'ot-name' }, x.label, ' ', x.new ? badge('warn', 'новый', 'Этого типа не было в прежних отчётах') : null,
      kids.length ? h('span', { class: 'mut3', style: 'font-weight:400' }, ' · подтипов ' + kids.length) : null);
    var row = h('div', { class: 'ot-row' + (v !== x.value ? ' ot-dirty' : '') }, tg, name,
      h('span', { class: 'ot-num' }, h('span', { class: 'ot-lbl' }, 'строк '), T.n(cells.lines)),
      h('span', { class: 'ot-num', title: T.n(x.future) + ' строк с датой позже даты отчёта' }, h('span', { class: 'ot-lbl' }, 'позже даты '), T.pct(cells.future_share)),
      h('span', { class: 'ot-pick' }, sel));
    T.warnings(ch, S.sel, x).forEach(function (w) { row.append(h('div', { class: 'ot-w ' + (w.level || 'mut') }, w.text)); });
    if (isOpen) {
      var sub = h('div', { class: 'ot-sub' }, h('b', {}, 'Подтип'), h('b', { class: 'ot-num' }, 'Строк'), h('b', { class: 'ot-num' }, 'Позже даты'));
      kids.forEach(function (k) {
        var c = k.cells || {};
        sub.append(h('span', { style: 'overflow-wrap:anywhere' }, k.label), h('span', { class: 'ot-num' }, T.n(c.lines)), h('span', { class: 'ot-num' }, T.pct(c.future_share)));
      });
      row.append(sub);
    }
    return row;
  }

  function load(autorun) {
    return S.api('GET', URL).then(function (r) {
      var first = !S.data || !S.data.choices || (r.job && S.data.job && r.job.finished_at !== S.data.job.finished_at);
      S.data = r;
      if (r.choices && (first || !Object.keys(S.sel).length)) S.sel = T.initial(r.choices);
      render();
      if (r.running) poll();
      else if (autorun && T.needsRun(r)) runAction();
    }, function (e) { S.data = { error: e.message, status: e.status }; render(); });
  }
  function runAction() {
    var id = S.data && S.data.action && S.data.action.id;
    if (!id) return;
    S.data.running = true; render();
    S.api('POST', '/api/v1/adapter/actions/' + encodeURIComponent(id) + '/run', { params: {} }).then(poll, function (e) {
      S.data.running = false; render();
      if (S.toast) S.toast(e.status === 409 ? 'Сейчас идёт другое действие адаптера — экран обновится после него.' : 'Типы строк не обновлены: ' + e.message, 'err');
      if (e.status === 409) poll();
    });
  }
  function poll() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { load(false); }, POLL_MS);
  }
  function step(text, cls) {
    if (!S.steps) return;
    S.steps.textContent = '';
    S.steps.append(h('div', { class: 'verdict ' + (cls || '') }, text));
  }
  function saveAll() {
    var ch = S.data.choices, values = T.changes(ch, S.sel);
    S.saving = true; render();
    step('Сохраняю соответствие…');
    var savedAt = Date.now();
    S.api('POST', URL, { values: values, replay: true, base_sha256: ch.file_sha256 == null ? null : ch.file_sha256 }).then(function (res) {
      var rev = res.saved && res.saved.revision;
      if (S.toast) S.toast(res.changed ? 'Сохранено: ' + T.types(res.changed) + (rev ? ' · правка №' + rev.rev_id : '') : 'Соответствие не менялось', 'ok');
      if (!res.replay || !res.replay.started) {
        S.saving = false; step('Соответствие сохранено, но отчёт не перечитан: ' + ((res.replay && res.replay.error) || 'адаптер не сказал, что перечитать') + '.', 'warn');
        load(false); return;
      }
      follow(savedAt, Date.now() + RECALC_WAIT_MS);
    }, function (e) {
      S.saving = false;
      step(e.status === 409 ? 'Настройки менялись после того, как таблица построена: строю её заново, повторите выбор.' : 'Не сохранено: ' + e.message, 'bad');
      if (e.status === 409) { S.sel = {}; runAction(); } else render();
    });
  }
  function follow(savedAt, deadline) {
    Promise.all([S.api('GET', '/api/v1/inbox/replay').catch(function () { return null; }),
      S.api('GET', '/api/v1/autopilot/status').catch(function () { return null; })]).then(function (got) {
      var rp = got[0], job = rp && rp.job, auto = got[1];
      var p = T.progress(job, job && job.state === 'done' ? auto : null, savedAt);
      if (p.state === 'recalc' && Date.now() > deadline) p = { state: 'done', text: 'Отчёт перечитан. ОВП пересчитается в ближайшем цикле автообновления.' };
      step(p.text, p.state === 'done' ? 'ok' : p.state === 'fail' ? 'bad' : '');
      if (p.state === 'done' || p.state === 'fail') {
        S.saving = false; S.sel = {};
        if (S.toast) S.toast(p.text, p.state === 'done' ? 'ok' : 'err');
        runAction();  // the table again, with the saved kinds
        return;
      }
      setTimeout(function () { follow(savedAt, deadline); }, POLL_MS);
    });
  }

  // opts: {api(method, url, body) -> Promise, toast(text, cls)?}
  T.mount = function (el, opts) {
    if (!el) return;
    style();
    S.el = el; S.api = opts.api; S.toast = opts.toast || null;
    render();
    load(false);  // the table once at the page's opening: the dot of the section «ОВП» needs it before the screen is opened
  };
  // экран открыт: данные заново (и действие, если результата нет или он устарел)
  T.shown = function () { if (S.el && !S.saving) load(true); };

  T._test = T;
  root.DeskOcpTypes = T;
})(typeof window !== 'undefined' ? window : globalThis);
