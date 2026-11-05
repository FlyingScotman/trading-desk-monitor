/*
 * «Индексы ставок» (TDM#565): один экран, на котором владелец говорит, какой индекс хаба стоит за названием индекса
 * плавающей ноги в условиях сделок. Названия берёт адаптер из последних файлов условий, у каждого — выпадающий список
 * кодов (ключевая ставка, RUONIA, RUSFAR, SOFR, €STR, «другой индекс»), подсказка и одна кнопка «Сохранить и пересчитать».
 * Ничего не вводится руками, командная строка не нужна. Названия индексов видны только на этом компьютере: они
 * приходят в результате действия адаптера и никуда, кроме этой страницы, не уходят.
 *  - Данные — результат действия адаптера, объявившего choices «trade_index_codes» (08 §3, §4): строка на название
 *    (число ног, сделок, книг, валюты, вид), текущий код, предложенный код с причиной, «новый». Нет результата, он
 *    устарел (файл настроек менялся после него) или старше MAX_AGE_H — действие запускается само. Адаптер без
 *    действия (404) — «обновите адаптер».
 *  - Подсказка: у названия без кода выпадающий список сразу стоит на предложенном коде с пометкой «предложено —
 *    проверьте»; кнопка записывает его, только когда владелец её нажал.
 *  - Сигнал: ответ несёт signal — число ног, пришедших без сопоставления («unmapped_index» в последних файлах условий).
 *    Точка в меню и строка в сводке видны, не заходя на экран.
 *  - «Сохранить и пересчитать»: POST /api/v1/adapter/choices/trade_index_codes {values, replay: true} — запись в
 *    настройки адаптера (как форма вкладки, с историей правок) и перечитывание условий (/api/v1/inbox/replay), затем
 *    ожидание пересчёта автообновлением (/api/v1/autopilot/status); итог — «Готово: условия перечитаны, сделки
 *    пересчитаны».
 * Встраивание — одна точка: DeskIndexCodes.mount(элемент, {api, toast}); DeskIndexCodes.shown() — экран открыт
 * (данные — заново). Стили — свои, с префиксом ic-, вставляются один раз. Чистая часть (DeskIndexCodes._test)
 * проверяется tests/js/settings_index_codes.test.js.
 */
(function (root) {
  'use strict';
  var T = {};
  var KIND = 'trade_index_codes';
  var URL = '/api/v1/adapter/choices/' + KIND;
  var POLL_MS = 1500, MAX_AGE_H = 6, RECALC_WAIT_MS = 10 * 60 * 1000;

  /* ---------- чистая часть ---------- */
  T.n = function (v) { return v == null ? '—' : typeof v === 'number' ? v.toLocaleString('ru-RU') : String(v); };
  T.names = function (n) {
    var m10 = n % 10, m100 = n % 100;
    return n + ' ' + (m10 === 1 && m100 !== 11 ? 'название' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'названия' : 'названий');
  };
  T.legs = function (n) {
    var m10 = n % 10, m100 = n % 100;
    return n + ' ' + (m10 === 1 && m100 !== 11 ? 'нога' : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? 'ноги' : 'ног');
  };
  T.dm = function (s) { var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] + '.' + m[1] : String(s || ''); };
  T.title = function (choices, v) {
    var o = (choices.options || []).filter(function (x) { return x.value === v; })[0];
    return o ? o.title : String(v);
  };
  // начальный выбор: сохранённый код; у названия без кода — предложенный (помечается)
  T.initial = function (choices) {
    var out = {};
    (choices.rows || []).forEach(function (r) { out[r.key] = r.value != null ? r.value : (r.suggested != null ? r.suggested : null); });
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
  T.cur = function (sel, r) { return sel.hasOwnProperty(r.key) ? sel[r.key] : r.value; };
  // предупреждения строки при текущем выборе: [{level, text}]
  T.warnings = function (choices, sel, r) {
    var out = [], v = T.cur(sel, r);
    if (v == null) {
      out.push({ level: r.future > 0 ? 'warn' : '', text: 'Код не выбран: нога уйдёт в монитор как «другой индекс», сделка будет «не оценено»' +
        (r.future > 0 ? ' (ног живых сделок: ' + T.n(r.future) + ')' : '') + '.' });
    } else if (v === 'OTHER') {
      out.push({ level: '', text: 'Сделка с этим индексом останется «не оценено»: монитор не считает «другой индекс».' });
    }
    if (v != null && r.value == null && v === r.suggested) out.push({ level: 'warn', text: 'Предложено по подсказке (' + (r.hint || 'по названию') + ') — проверьте.' });
    else if (r.hint && r.suggested !== v) out.push({ level: '', text: 'Подсказка адаптера: ' + r.hint });
    return out;
  };
  // «без сопоставления» — сохранённый код не задан: ноги таких строк уходят как «другой индекс»
  T.unmapped = function (choices) {
    return (choices.rows || []).filter(function (r) { return r.value == null; });
  };
  // число ног без сопоставления для точки в меню и сводки: строки без сохранённого кода и сигнал по последним файлам
  T.attention = function (data) {
    var ch = data && data.choices, sig = data && data.signal;
    var byRows = ch ? T.unmapped(ch).reduce(function (a, r) { return a + (r.future > 0 ? r.future : 0); }, 0) : 0;
    return Math.max(byRows, sig && sig.count > 0 ? sig.count : 0);
  };
  // порядок: сначала без кода, затем новые, дальше как дал адаптер
  T.order = function (rows) {
    return rows.map(function (r, i) { return [r, i]; }).sort(function (a, b) {
      return (b[0].value == null) - (a[0].value == null) || (!!b[0].new) - (!!a[0].new) || a[1] - b[1];
    }).map(function (x) { return x[0]; });
  };
  T.filter = function (choices, sel, rows, mode) {
    if (mode === 'open') return rows.filter(function (r) { return T.cur(sel, r) == null || r.value == null; });
    if (mode === 'attention') return rows.filter(function (r) { return r.new || T.warnings(choices, sel, r).some(function (w) { return w.level; }); });
    return rows;
  };
  // «Подпись: значение» по колонкам адаптера для строки
  T.cells = function (choices, r) {
    var c = r.cells || {};
    return (choices.columns || []).filter(function (col) { return c[col.id] != null && c[col.id] !== ''; })
      .map(function (col) { return { title: col.title, value: T.n(c[col.id]) }; });
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
    if (!replay) return { state: 'replay', text: 'Перечитываю условия сделок…' };
    if (replay.state === 'running') return { state: 'replay', text: replay.waiting ? 'Идёт чтение папок — перечитаю условия сразу после него…' : 'Перечитываю условия сделок за ' + T.dm(replay.request && replay.request.to) + '…' };
    if (replay.state !== 'done') return { state: 'fail', text: 'Условия не перечитаны: ' + (replay.error || replay.error_code || 'ошибка') + '. Соответствие сохранено; перечитайте кнопкой «Перечитать» (Адаптер и источники).' };
    var revs = (replay.result && replay.result.revisions) || 0;
    if (!revs) return { state: 'done', text: 'Готово: условия перечитаны, сделки не изменились.' };
    if (!auto) return { state: 'recalc', text: 'Условия перечитаны. Пересчитываю сделки…' };
    if (auto.enabled === false) return { state: 'done', text: 'Условия перечитаны. Автообновление выключено: сделки пересчитаются при следующем расчёте.' };
    var fin = replay.finished_at ? new Date(replay.finished_at).getTime() : savedAt;
    var run = auto.latest_run;
    if (run && run.created_at && new Date(run.created_at).getTime() >= fin) return { state: 'done', text: 'Готово: условия перечитаны, сделки пересчитаны' + (run.label ? ' (расчёт ' + run.label + ')' : '') + '.' };
    if (auto.state !== 'running' && auto.last_cycle_at && new Date(auto.last_cycle_at).getTime() >= fin)
      return { state: 'done', text: 'Условия перечитаны. Автообновление: ' + (auto.message || 'цикл завершён') + '.' };
    return { state: 'recalc', text: 'Условия перечитаны. Пересчитываю сделки…' };
  };

  /* ---------- DOM ---------- */
  var S = { el: null, api: null, toast: null, data: null, sel: {}, mode: null, timer: null, saving: false, steps: null };
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
  var CSS = '.ic-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 10px}' +
    '.ic-chips{display:flex;gap:6px;flex-wrap:wrap}.ic-chip{border:1px solid var(--line-2);background:var(--panel);color:var(--fg-2);border-radius:999px;padding:2px 10px;font-size:12px;cursor:pointer}' +
    '.ic-chip.on{border-color:var(--accent);color:var(--fg);background:var(--panel-2)}' +
    '.ic-list{border:1px solid var(--line);border-radius:8px;overflow:hidden}' +
    '.ic-row{display:grid;grid-template-columns:minmax(0,1.5fr) 84px minmax(220px,1.2fr);gap:4px 10px;align-items:center;padding:8px 10px;border-top:1px solid var(--line)}' +
    '.ic-row:first-child{border-top:0}.ic-row.ic-hd{background:var(--panel-2);color:var(--fg-3);font-size:11.5px;font-weight:600}' +
    '.ic-row.ic-dirty{background:var(--warn-bg)}.ic-name{min-width:0;overflow-wrap:anywhere;font-weight:550}.ic-num{text-align:right;font-variant-numeric:tabular-nums}' +
    '.ic-meta{grid-column:1 / -1;font-size:12px;color:var(--fg-3);display:flex;gap:4px 12px;flex-wrap:wrap}.ic-meta b{font-weight:500;color:var(--fg-2)}' +
    '.ic-sel{width:100%;min-width:0}.ic-w{grid-column:1 / -1;font-size:12px;margin:0;line-height:1.35}.ic-w.bad{color:var(--neg)}.ic-w.warn{color:var(--warn)}.ic-w.mut{color:var(--fg-3)}' +
    '.ic-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0 0}.ic-steps{margin:10px 0 0}.ic-lbl{display:none;color:var(--fg-3);font-size:11px}' +
    '@media (max-width:700px){.ic-row{grid-template-columns:minmax(0,1fr) auto}.ic-row.ic-hd{display:none}.ic-lbl{display:inline}' +
    '.ic-row > .ic-pick{grid-column:1 / -1}}';
  function style() {
    if (document.getElementById('ic-style')) return;
    var s = document.createElement('style'); s.id = 'ic-style'; s.textContent = CSS; document.head.append(s);
  }

  // точка раздела «Индексы ставок» в меню и строка сводки сверху (settings-nav.js): видны без захода на экран
  function signal(n) {
    var DS = root.DeskSettings;
    if (!DS) return;
    var text = n ? 'Индексы ставок: ' + T.legs(n) + ' без сопоставления' : '';
    if (DS.dot) DS.dot('indexes', n ? 'warn' : '', text);
    if (DS.status) DS.status('index-codes', n ? { cls: 'warn', href: '#indexes', k: 'Индексы ставок', text: T.legs(n) + ' без сопоставления — открыть', title: text } : null);
  }

  function render() {
    var el = S.el, r = S.data;
    el.textContent = '';
    if (!r) { el.append(h('p', { class: 'mut' }, 'Загружаю…')); return; }
    if (r.error) {
      el.append(h('div', { class: 'note ' + (r.status === 404 ? 'warn' : 'err') }, r.status === 404
        ? 'Адаптер не отдаёт индексы ставок для этого экрана: обновите адаптер (Настройки → Обновления). Пока такие сделки в мониторе «не оценено».'
        : 'Не удалось загрузить: ' + r.error));
      return;
    }
    var ch = r.choices, running = r.running;
    signal(T.attention(r));
    var head = h('div', { class: 'ic-head' });
    head.append(h('button', { class: 'ibtn', type: 'button', disabled: running || S.saving, onclick: function () { runAction(); } }, running ? 'Читаю условия…' : 'Обновить'));
    if (r.job && r.job.finished_at && !running) head.append(h('span', { class: 'mut3' }, 'по файлам на ' + (ch && ch.replay ? T.dm(ch.replay.to) : '—') + ' · обновлено ' + new Date(r.job.finished_at).toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' })));
    el.append(head);
    el.append(h('p', { class: 'hint', style: 'margin:0 0 10px' }, 'Названия индексов видны только на этом компьютере.'));
    if (r.signal && r.signal.count > 0)
      el.append(h('div', { class: 'note warn' }, 'В последних файлах условий ' + T.legs(r.signal.count) + ' пришли без сопоставления индекса' +
        (r.signal.latest ? ' (последний файл за ' + T.dm(r.signal.latest) + ')' : '') + ': сделки по ним «не оценено», пока индекс не выбран ниже.'));
    if (running && !ch) { el.append(h('p', { class: 'mut' }, 'Адаптер читает последние файлы условий сделок…')); return; }
    if (!ch) {
      el.append(h('div', { class: 'note ' + (r.ok === false ? 'warn' : '') }, r.ok === false ? 'Индексы не получены: ' + (r.summary || []).join('; ') : 'Индексов ещё нет: нажмите «Обновить».'));
      return;
    }
    var rows = T.order(ch.rows || []);
    var nOpen = T.filter(ch, S.sel, rows, 'open').length;
    var nAttn = T.filter(ch, S.sel, rows, 'attention').length;
    if (S.mode == null) S.mode = nOpen ? 'open' : 'all';
    var chips = h('div', { class: 'ic-chips', role: 'tablist' });
    [['open', 'Без кода ' + nOpen], ['attention', 'Требуют внимания ' + nAttn], ['all', 'Все ' + rows.length]].forEach(function (m) {
      chips.append(h('button', { class: 'ic-chip' + (S.mode === m[0] ? ' on' : ''), type: 'button', role: 'tab', 'aria-selected': S.mode === m[0] ? 'true' : 'false',
        onclick: function () { S.mode = m[0]; render(); } }, m[1]));
    });
    el.append(h('div', { class: 'ic-head' }, chips));
    var list = h('div', { class: 'ic-list' });
    list.append(h('div', { class: 'ic-row ic-hd' }, h('span', {}, 'Название индекса в условиях'), h('span', { class: 'ic-num', title: 'Ноги живых сделок' }, 'Ног'), h('span', {}, 'Индекс хаба')));
    var shown = T.filter(ch, S.sel, rows, S.mode);
    if (!shown.length) list.append(h('div', { class: 'ic-row' }, h('span', { class: 'mut' }, S.mode === 'all' ? 'Названий индексов в условиях нет.' : 'Нечего проверять.')));
    shown.forEach(function (x) { list.append(rowEl(ch, x)); });
    el.append(list);
    (ch.notes || []).forEach(function (n) { el.append(h('p', { class: 'hint' }, n)); });
    var diff = T.changes(ch, S.sel), nd = Object.keys(diff).length;
    var bySuggest = Object.keys(diff).filter(function (k) { var x = (ch.rows || []).filter(function (y) { return y.key === k; })[0]; return x && x.value == null && diff[k] === x.suggested; }).length;
    var save = h('button', { class: 'btn', type: 'button', disabled: S.saving || running || !ch.replay, onclick: saveAll }, 'Сохранить и пересчитать');
    var reset = h('button', { class: 'ibtn', type: 'button', disabled: S.saving || !nd, onclick: function () { S.sel = T.saved(ch); render(); } }, 'Вернуть сохранённое');
    var when = ch.replay ? T.dm(ch.replay.to) : '—';
    el.append(h('div', { class: 'ic-bar' }, save, reset, h('span', { class: 'mut3' }, nd ? 'Будет записано: ' + T.names(nd) + (bySuggest ? ' (по подсказке — ' + bySuggest + ')' : '') + '; затем перечитаю условия за ' + when + ' и пересчитаю сделки.'
      : 'Изменений нет. Кнопка всё равно перечитает условия за ' + when + ' и пересчитает сделки.')));
    el.append(S.steps = S.steps || h('div', { class: 'ic-steps', 'aria-live': 'polite' }));
  }
  function rowEl(ch, x) {
    var v = T.cur(S.sel, x);
    var sel = h('select', { class: 'inp ic-sel', 'aria-label': 'Индекс хаба для «' + x.label + '»', disabled: S.saving, onchange: function () { S.sel[x.key] = this.value === '' ? null : this.value; render(); } },
      (ch.options || []).map(function (o) {
        var t = o.title + (o.value != null && o.value === x.suggested ? ' — предложено' : '');
        return h('option', { value: o.value == null ? '' : o.value, selected: o.value === v }, t);
      }));
    var name = h('span', { class: 'ic-name' }, x.label, ' ', x.new ? badge('warn', 'новый', 'Этого названия не было в прежних файлах условий') : null);
    var row = h('div', { class: 'ic-row' + (v !== x.value ? ' ic-dirty' : '') }, name,
      h('span', { class: 'ic-num', title: T.n(x.future) + ' ног живых сделок' }, h('span', { class: 'ic-lbl' }, 'ног '), T.n(x.future)),
      h('span', { class: 'ic-pick' }, sel));
    var cells = T.cells(ch, x);
    if (cells.length) row.append(h('div', { class: 'ic-meta' }, cells.map(function (c) { return h('span', {}, c.title + ' ', h('b', {}, c.value)); })));
    T.warnings(ch, S.sel, x).forEach(function (w) { row.append(h('div', { class: 'ic-w ' + (w.level || 'mut') }, w.text)); });
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
      if (S.toast) S.toast(e.status === 409 ? 'Сейчас идёт другое действие адаптера — экран обновится после него.' : 'Индексы не обновлены: ' + e.message, 'err');
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
      if (S.toast) S.toast(res.changed ? 'Сохранено: ' + T.names(res.changed) + (rev ? ' · правка №' + rev.rev_id : '') : 'Соответствие не менялось', 'ok');
      if (!res.replay || !res.replay.started) {
        S.saving = false; step('Соответствие сохранено, но условия не перечитаны: ' + ((res.replay && res.replay.error) || 'адаптер не сказал, что перечитать') + '.', 'warn');
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
      if (p.state === 'recalc' && Date.now() > deadline) p = { state: 'done', text: 'Условия перечитаны. Сделки пересчитаются в ближайшем цикле автообновления.' };
      step(p.text, p.state === 'done' ? 'ok' : p.state === 'fail' ? 'bad' : '');
      if (p.state === 'done' || p.state === 'fail') {
        S.saving = false; S.sel = {};
        if (S.toast) S.toast(p.text, p.state === 'done' ? 'ok' : 'err');
        runAction();  // the table again, with the saved codes
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
    load(false);  // the table once at the page's opening: the dot of the section needs it before the screen is opened
  };
  // экран открыт: данные заново (и действие, если результата нет или он устарел)
  T.shown = function () { if (S.el && !S.saving) load(true); };

  T._test = T;
  root.DeskIndexCodes = T;
})(typeof window !== 'undefined' ? window : globalThis);
