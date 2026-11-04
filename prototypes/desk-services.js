/* «Подключение к сервисам» (08 §2.2, #548): панель, которую описывает помощник адаптера, и его фоновые загрузки.
   Монитор о сервисах ничего не знает: поля, действия, строки и таблицы приходят от адаптера и выводятся как текст
   (textContent, никакого HTML). Пароли (поля secret) уходят только в теле POST этого запуска, нигде не хранятся;
   после отправки поля очищаются. Данные: /api/v1/adapter-services (только с этого компьютера, как получение условий). */
(() => {
  'use strict';
  const $ = id => document.getElementById('svc-' + id);
  if (!$('card')) return;
  const TEXT = {
    capability_invalid: 'Адаптер описывает подключение к сервисам в несовместимом виде. Нужен совместимый комплект адаптера.',
    helper_unavailable: 'В комплекте адаптера нет модуля подключения к сервисам.',
    runtime_unavailable: 'Укажите и сохраните «Python для получения данных» (подраздел «Условия сделок» → «Дополнительно»).',
    adapter_changed: 'Адаптер или его настройки только что изменились. Нажмите «Обновить».',
    settings_invalid: 'Проверьте настройки программы.', local_only: 'Подключение к сервисам настраивается только на компьютере с программой.',
    origin_invalid: 'Откройте настройки по локальному адресу программы.', status_unknown: 'Нет связи с программой. Обновите страницу.',
    loading: 'Загрузка панели адаптера…', queued: 'Ожидает очереди…', waiting_feed: 'Ждёт окончания фоновой загрузки…',
    waiting_acquisition: 'Ждёт окончания «Получить условия сделок»…', running: 'Выполняется…',
    saved: 'Настройки сохранены.', described: 'Панель обновлена.', cancelled: 'Отменено.',
    services_busy: 'Предыдущая операция ещё выполняется.', feed_busy: 'Эта загрузка уже идёт или ждёт очереди.',
    feed_unknown: 'Такой загрузки больше нет. Нажмите «Обновить».', field_unknown: 'Панель изменилась. Нажмите «Обновить».',
    action_unknown: 'Панель изменилась. Нажмите «Обновить».', panel_missing: 'Панель адаптера ещё не загружена. Нажмите «Обновить».',
    invalid_request: 'Проверьте заполненные поля.', helper_timeout: 'Время ожидания истекло, процесс адаптера остановлен.',
    helper_response_invalid: 'Модуль подключения вернул некорректный ответ.', helper_failed: 'Модуль подключения завершился с ошибкой.',
    operation_failed: 'Операция не выполнена.', config_invalid: 'Проверьте настройки подключения.',
    cancel_after_commit_unavailable: 'Данные уже загружаются в базу: отмена на этой фазе недоступна.',
    history_unsupported: 'Адаптер не умеет сделки прошлых дат.'
  };
  const STATE = {complete: 'Готово.', partial: 'Выполнено частично.', blocked: 'Остановлено.', error: 'Ошибка.'};
  const RUN = {complete: 'получено', partial: 'получено частично', skipped: 'нечего загружать', blocked: 'остановлено',
    error: 'ошибка', cancelled: 'отменено'};
  const RUN_CODE = {login_required: 'нужен вход', not_configured: 'не настроено', feed_disabled: 'выключено в адаптере',
    no_change: 'изменений нет', no_books: 'нет книг с позициями', not_due: 'ещё рано', no_positions: 'нет позиций', config_invalid: 'проверьте настройки',
    source_disabled: 'источник выключен', helper_timeout: 'время ожидания истекло', helper_response_invalid: 'некорректный ответ модуля',
    files_not_accepted: 'файлы не приняты', calculation_busy: 'занят расчёт, повтор позже', ingestion_busy: 'загрузка занята, повтор позже',
    runtime_unavailable: 'нет Python для получения данных', adapter_unavailable: 'адаптер не загружен',
    feed_source_invalid: 'источник не объявлен адаптером', cancelled: 'отменено'};
  const FILE = {accepted: 'принят', partially_accepted: 'принят частично', superseded: 'заменён более новым', duplicate: 'повтор',
    rejected: 'отклонён', failed: 'ошибка чтения', timed_out: 'таймаут чтения', match_failed: 'не распознан', ignored: 'не формат адаптера'};
  const ACTIVE = ['queued', 'waiting', 'running'];
  let data = null, panelJson = null, typed = {}, timer = null, ticker = null;
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text !== undefined && text !== null) n.textContent = text; if (cls) n.className = cls; return n; };
  const when = s => s ? new Date(s).toLocaleString('ru-RU', {day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const busy = () => !!(data && data.job && ACTIVE.includes(data.job.state));

  function fields() { return ((data && data.panel && data.panel.sections) || []).flatMap(s => s.fields); }
  function value(f) {
    if (Object.prototype.hasOwnProperty.call(typed, f.key)) return typed[f.key];
    return f.type === 'bool' ? !!f.value : f.type === 'secret' ? '' : f.value == null ? '' : String(f.value);
  }
  // f.<ключ> = значение, как набрано сейчас; пароль — только набранный (пустой не отправляется: «не менять»)
  function body(extra) {
    const b = new URLSearchParams(extra || {});
    for (const f of fields()) {
      const v = value(f);
      if (f.type === 'secret') { if (v) b.set('f.' + f.key, v); continue; }
      b.set('f.' + f.key, f.type === 'bool' ? (v ? 'true' : 'false') : v);
    }
    return b;
  }
  function forgetSecrets() {
    for (const f of fields()) if (f.type === 'secret') { delete typed[f.key]; const n = document.getElementById('svc-f-' + f.key); if (n) n.value = ''; }
  }

  function renderField(f, grid) {
    const id = 'svc-f-' + f.key, label = el('label', f.label); label.htmlFor = id;
    const cell = el('div'); let input;
    if (f.type === 'bool') {
      const wrap = el('label', null, 'chk'); input = el('input'); input.type = 'checkbox'; input.checked = !!value(f);
      wrap.append(input, document.createTextNode(' ' + (f.placeholder || 'включено'))); cell.append(wrap);
      input.addEventListener('change', () => { typed[f.key] = input.checked; });
    } else if (f.type === 'select') {
      input = el('select', null, 'inp');
      for (const o of f.options || []) { const opt = el('option', o.label); opt.value = o.value; input.append(opt); }
      input.value = value(f); cell.append(input);
      input.addEventListener('change', () => { typed[f.key] = input.value; });
    } else {
      input = el('input', null, 'inp' + (f.type === 'url' ? ' mono' : '')); input.maxLength = 4096; input.spellcheck = false;
      input.autocomplete = f.type === 'secret' ? 'new-password' : 'off';
      input.type = f.type === 'secret' ? 'password' : 'text';
      if (f.type === 'int') input.inputMode = 'numeric';
      if (f.type === 'url') input.inputMode = 'url';
      input.placeholder = f.type === 'secret' ? (f.set ? 'сохранён; пусто — не менять' : 'не задан') : (f.placeholder || '');
      input.value = value(f); cell.append(input);
      input.addEventListener('input', () => { typed[f.key] = input.value; });
    }
    input.id = id; input.dataset.svcKey = f.key;
    if (f.help) cell.append(el('p', f.help, 'hint'));
    grid.append(label, cell);
  }
  function renderPanel() {
    const p = data && data.panel, host = $('panel');
    $('lines').replaceChildren(...((p && p.status) || []).map(t => el('li', t)));
    if (p && p.title) $('title').textContent = p.title;
    const json = JSON.stringify(p || null);
    if (json === panelJson) { buttons(); return; }  // не перерисовывать набранное без нужды
    panelJson = json; host.replaceChildren();
    for (const s of (p && p.sections) || []) {
      const box = el('div', null, 'at-action'); box.dataset.section = s.id;
      if (s.title) box.append(el('div', s.title, 'at-ah'));
      if (s.help) box.append(el('p', s.help, 'help'));
      if (s.fields.length) { const grid = el('div', null, 'form'); s.fields.forEach(f => renderField(f, grid)); box.append(grid); }
      if (s.actions.length) {
        const row = el('div', null, 'row'); row.style.cssText = 'margin-top:10px;flex-wrap:wrap';
        for (const a of s.actions) {
          const b = el('button', a.label, 'ibtn'); b.type = 'button'; b.dataset.action = a.id;
          b.addEventListener('click', () => { if (!a.confirm || window.confirm(a.confirm)) submit('action', {action: a.id}); });
          row.append(b);
        }
        box.append(row);
      }
      host.append(box);
    }
    buttons();
  }
  function buttons() {
    const on = !!(data && data.available && data.panel) && !busy();
    $('save').disabled = !on || !fields().length;
    $('panel').querySelectorAll('button[data-action]').forEach(b => { b.disabled = !on; });
    $('result').querySelectorAll('button').forEach(b => { b.disabled = !on; });
    $('cancel').hidden = !busy();
  }

  function status(text) { $('status').textContent = text || ''; }
  function jobText(job) {
    if (!job || job.state === 'idle') return '';
    if (ACTIVE.includes(job.state)) {
      let t = TEXT[job.code] || TEXT.running;
      if (job.state === 'running' && job.started_at) {
        const sec = Math.max(0, Math.round((Date.now() - new Date(job.started_at)) / 1000));
        t += ' ' + Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
        if (job.long) t += ' Если откроется окно входа, завершите вход в нём.';
      }
      return t;
    }
    if (job.op === 'action' && !TEXT[job.code]) return (STATE[job.state] || '') + ' ' + job.code;
    if (job.code === 'config_invalid' && job.field) {
      const f = fields().find(x => x.key === job.field);
      return 'Проверьте поле «' + (f ? f.label : job.field) + '».';
    }
    return TEXT[job.code] || ((STATE[job.state] || 'Операция не выполнена.') + ' ' + job.code);
  }
  function renderResult(job) {
    const host = $('result'); host.replaceChildren();
    $('panel').querySelectorAll('[aria-invalid]').forEach(n => { n.removeAttribute('aria-invalid'); n.style.borderColor = ''; });
    if (!job || ACTIVE.includes(job.state)) return;
    const bad = job.code === 'config_invalid' && job.field && document.getElementById('svc-f-' + job.field);
    if (bad) { bad.setAttribute('aria-invalid', 'true'); bad.style.borderColor = 'var(--neg)'; }
    if ((job.lines || []).length) { const ul = el('ul', null, 'at-lines'); ul.append(...job.lines.map(t => el('li', t))); host.append(ul); }
    if (job.table && job.table.columns) {
      const wrap = el('div'); wrap.style.cssText = 'overflow-x:auto;margin-top:8px';
      const t = el('table', null, 't'), hr = el('tr');
      job.table.columns.forEach(c => hr.append(el('th', c)));
      const head = el('thead'); head.append(hr); const tb = el('tbody');
      for (const row of job.table.rows) { const tr = el('tr'); row.forEach(c => tr.append(el('td', c == null ? '' : String(c)))); tb.append(tr); }
      t.append(head, tb); wrap.append(t); host.append(wrap);
    }
    if (Array.isArray(job.choices) && job.choices.length && job.choose_action) {
      const row = el('div', null, 'row'); row.style.cssText = 'margin-top:8px;flex-wrap:wrap';
      const sel = el('select', null, 'inp'); sel.id = 'svc-choice'; sel.style.maxWidth = '100%';
      const empty = el('option', 'Выберите…'); empty.value = ''; sel.append(empty);
      for (const c of job.choices) { const o = el('option', c.label); o.value = c.id; sel.append(o); }
      const b = el('button', 'Выбрать', 'ibtn'); b.type = 'button'; b.id = 'svc-choose';
      b.addEventListener('click', () => { if (sel.value) submit('action', {action: job.choose_action, arg: sel.value}); });
      row.append(sel, b); host.append(row);
    }
  }

  function scheduleText(s, after) {
    let t = !s ? 'вручную' : s.every_min ? 'каждые ' + s.every_min + ' мин, ' + s.from + '–' + s.to : (s.weekdays_only ? 'по будням' : 'ежедневно') + ' в ' + s.daily_at.join(', ');
    if (s && s.every_min && s.weekdays_only) t += ', по будням';
    return after ? t + '; и после новых позиций' : t;
  }
  function renderFeeds() {
    const host = $('feeds'), feeds = (data && data.feeds) || [];
    if (!feeds.length) { host.replaceChildren(el('p', data && data.panel ? 'Адаптер не описывает фоновых загрузок.' : '', 'hint')); return; }
    const t = el('table', null, 't svc-t'), hr = el('tr');
    ['Данные', 'Включено', 'Расписание', 'Последний запуск', 'Следующий', ''].forEach(c => hr.append(el('th', c)));
    const head = el('thead'); head.append(hr); const tb = el('tbody');
    for (const f of feeds) {
      const tr = el('tr'); tr.dataset.feed = f.id; const last = f.last || {};
      const name = el('td'); name.append(el('div', f.title)); name.append(el('div', f.dataset, 'mut3 mono'));
      const sw = el('td'), lab = el('label', null, 'tgl'), box = el('input'); box.type = 'checkbox'; box.checked = !!f.enabled;
      box.setAttribute('aria-label', 'Фоновая загрузка «' + f.title + '»');
      lab.append(box, el('span', null, 'tgl-s')); sw.append(lab);
      box.addEventListener('change', () => enable(f.id, box.checked, box));
      const lastTd = el('td'); lastTd.dataset.l = 'Последний запуск'; sw.dataset.l = 'Включено'; 
      if (last.last_started) {
        const code = !last.code ? '' : RUN_CODE[last.code] || (['complete', 'partial'].includes(last.state) ? '' : last.code);
        lastTd.append(el('div', when(last.last_finished || last.last_started) + ' · ' + (f.running ? 'идёт' : (RUN[last.state] || last.state || '')) + (code && !f.running ? ' (' + code + ')' : '')));
        const more = [];
        for (const x of last.files || []) more.push(x.file + ': ' + (FILE[x.status] || x.status));
        if ((last.recomputed || []).length) more.push('Пересчитаны: ' + last.recomputed.map(r => r.label + ' (#' + r.run_id + ')').join(', '));
        if ((last.recompute_failed || []).length) more.push('Не пересчитаны: ' + last.recompute_failed.map(r => r.label).join(', '));
        more.push(...(last.lines || []));
        if (more.length) { const d = el('details'); d.append(el('summary', 'подробнее', 'mut3')); d.append(...more.map(x => el('div', x, 'hint'))); lastTd.append(d); }
      } else lastTd.append(el('span', f.running ? 'идёт' : 'ещё не запускалось', 'mut3'));
      const next = el('td'); next.dataset.l = 'Следующий';
      next.textContent = last.needs_login ? 'нужен вход: выполните вход в панели выше'
        : !f.ready ? 'не готово' + (f.why_not_ready ? ': ' + f.why_not_ready : '')
        : !f.enabled ? 'выключено' : f.queued ? 'в очереди' : f.next_run ? when(f.next_run) + (last.failures ? ' (повтор ' + last.failures + ')' : '') : '—';
      if (last.needs_login) next.style.color = 'var(--warn)';
      const act = el('td'), run = el('button', 'Запустить сейчас', 'ibtn'); run.type = 'button';
      run.disabled = f.running || f.queued; run.addEventListener('click', () => runNow(f.id, run));
      act.append(run);
      if (f.running) { const stop = el('button', 'Остановить', 'lnk'); stop.type = 'button'; stop.addEventListener('click', () => cancel('feed')); act.append(stop); }
      const sched = el('td', scheduleText(f.schedule, f.after_positions)); sched.dataset.l = 'Расписание';
      tr.append(name, sw, sched, lastTd, next, act); tb.append(tr);
    }
    t.append(head, tb); host.replaceChildren(t);
  }

  // место «Сервисы» на странице настроек (settings-nav.js): блок виден — открыто место и пункт подраздела, скрыт — нет
  function place(hidden) {
    const DS = window.DeskSettings;
    if (!DS) return;
    if (hidden) { DS.unslot('services'); DS.status('services', null); DS.dot('adapter/services', '', ''); } else DS.slot('services');
    const line = document.getElementById('svc-line');
    if (line && hidden) line.textContent = '';
  }
  // что важно на виду: сводка сверху, точка подраздела, строка в «Обзоре» адаптера
  function summarize() {
    const DS = window.DeskSettings, feeds = (data && data.feeds) || [];
    const login = feeds.filter(f => f.last && f.last.needs_login).map(f => f.title);
    const failed = feeds.filter(f => f.enabled && f.last && f.last.state === 'error' && !f.last.needs_login).map(f => f.title);
    let cls = 'ok', text = 'работают', title = 'Подключение к сервисам работает';
    if (!data.available) { cls = 'warn'; text = 'не готово'; title = TEXT[data.code] || data.code || ''; }
    else if (login.length) { cls = 'warn'; text = 'нужен вход: ' + login.join(', '); }
    else if (failed.length) { cls = 'warn'; text = 'ошибка загрузки: ' + failed.join(', '); }
    const line = document.getElementById('svc-line');
    if (line) {
      line.textContent = 'Подключение к сервисам: ' + text + ' ';
      const a = el('a', 'открыть →'); a.href = '#adapter/services'; line.append(a);
    }
    if (!DS) return;
    DS.status('services', {cls, href: '#adapter/services', k: 'Сервисы', text, title});
    DS.dot('adapter/services', cls === 'ok' ? '' : cls, text === 'работают' ? '' : text);
  }

  function render() {
    const card = $('card');
    if (!data) return;
    // без возможности в адаптере (или без адаптера) блока нет: о сервисах монитор ничего не знает
    card.hidden = !data.available && ['services_unavailable', 'adapter_unavailable', undefined].includes(data.code);
    place(card.hidden);
    if (card.hidden) return;
    summarize();
    $('sub').textContent = data.available && data.name ? data.name + (data.version ? ' ' + data.version : '') : '';
    renderPanel(); renderFeeds();
    const job = data.job;
    if (!data.available) status(TEXT[data.code] || data.code);
    else if (job && job.state !== 'idle') status(jobText(job));
    else if (!data.panel) status(data.describe_error ? 'Панель адаптера не загрузилась: ' + (TEXT[data.describe_error] || data.describe_error) + '. Нажмите «Обновить».' : TEXT.loading);
    else status('');
    renderResult(job); buttons();
    clearTimeout(timer); clearInterval(ticker);
    if (busy()) {
      timer = setTimeout(refresh, 1000);
      if (job.state === 'running') ticker = setInterval(() => status(jobText(data.job)), 1000);
    } else if (data.available && (!data.panel && !data.describe_error || data.running || (data.feeds || []).some(f => f.queued || f.running)))
      timer = setTimeout(refresh, 2500);
  }
  async function refresh() {
    try {
      const res = await fetch('/api/v1/adapter-services', {cache: 'no-store'});
      if (res.status === 403) { $('card').hidden = true; place(true); return; }
      if (!res.ok) { status(TEXT.status_unknown); return; }
      const before = data && data.job && data.job.id, was = before && ACTIVE.includes(data.job.state);
      data = await res.json();
      // сохранено или перечитано: панель заново с сервера, набранное забыто
      if (was && data.job && data.job.id === before && ['saved', 'described'].includes(data.job.code)) { typed = {}; panelJson = null; }
      render();
      loadHist();
    } catch (_) { status(TEXT.status_unknown); }
  }
  async function post(path, b) {
    const res = await fetch('/api/v1/adapter-services' + path, {method: 'POST', cache: 'no-store',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: b});
    return [res, await res.json().catch(() => ({}))];
  }
  async function submit(op, extra) {
    const b = op === 'describe' ? new URLSearchParams() : body(extra);
    forgetSecrets();
    status(TEXT.queued);
    try {
      const [res, job] = await post('/' + op, b);
      if (!res.ok) { status(TEXT[job.code] || TEXT.operation_failed); return; }
      data = Object.assign({}, data, {job}); render();
    } catch (_) { status(TEXT.status_unknown); }
    finally { for (const k of [...b.keys()]) b.delete(k); }
    refresh();
  }
  async function enable(id, on, box) {
    try {
      const [res, r] = await post('/feeds/' + encodeURIComponent(id) + '/enabled', new URLSearchParams({enabled: on ? 'true' : 'false'}));
      if (!res.ok) { box.checked = !on; status(TEXT[r.code] || TEXT.operation_failed); return; }
    } catch (_) { box.checked = !on; status(TEXT.status_unknown); return; }
    refresh();
  }
  async function runNow(id, btn) {
    btn.disabled = true;
    try {
      const [res, r] = await post('/feeds/' + encodeURIComponent(id) + '/run', new URLSearchParams());
      if (!res.ok) status(TEXT[r.code] || TEXT.operation_failed);
    } catch (_) { status(TEXT.status_unknown); }
    refresh();
  }
  async function cancel(target) {
    try {
      const [res, r] = await post('/cancel', new URLSearchParams({target}));
      if (!res.ok) status(TEXT[r.code] || TEXT.operation_failed);
    } catch (_) { status(TEXT.status_unknown); }
    refresh();
  }
  // ---- сделки прошлых дат (TDM#569): GET/POST /api/v1/adapter-services/history[/refetch]. Слова — desk-backfill-text.js ----
  let hist = null;
  const ymd = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const dmy = s => s ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(0, 4) : '';
  const histWords = h => (window.DESK_BACKFILL_TEXT || {history: () => ({summary: '', applied: '', next: '', problems: []})}).history(h);
  function buildHist() {
    const host = $('hist');
    if (!host || host.firstChild) return;
    const on = el('label', null, 'tgl'), box = el('input'); box.type = 'checkbox'; box.id = 'svc-hist-on';
    box.setAttribute('aria-label', 'Догружать сделки прошлых дат'); on.append(box, el('span', null, 'tgl-s'));
    const from = el('input', null, 'inp mono'); from.type = 'date'; from.id = 'svc-hist-from'; from.style.width = '170px';
    const row1 = el('div', null, 'row'); row1.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px';
    row1.append(on, el('span', 'Догружать сделки прошлых дат с'), from);
    const when = el('select', null, 'inp'); when.id = 'svc-hist-when'; when.style.maxWidth = '100%';
    for (const [v, t] of [['off_hours', 'Только вне торговых часов (по умолчанию)'], ['any', 'В любое время']]) { const o = el('option', t); o.value = v; when.append(o); }
    const row2 = el('div', null, 'row'); row2.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    const wl = el('label', 'Когда спрашивать сервис'); wl.htmlFor = 'svc-hist-when'; row2.append(wl, when);
    const rf = el('button', 'Перезапросить период…', 'ibtn'); rf.type = 'button'; rf.id = 'svc-hist-refetch';
    const row3 = el('div', null, 'row'); row3.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    const rfrom = el('input', null, 'inp mono'), rto = el('input', null, 'inp mono'), go = el('button', 'Запросить заново', 'btn');
    rfrom.type = rto.type = 'date'; rfrom.id = 'svc-hist-rf'; rto.id = 'svc-hist-rt'; rfrom.style.width = rto.style.width = '170px'; go.type = 'button'; go.id = 'svc-hist-go';
    const range = el('div', null, 'row'); range.id = 'svc-hist-range'; range.hidden = true; range.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    range.append(el('span', 'С'), rfrom, el('span', 'по'), rto, go);
    row3.append(rf);
    const sum = el('p'); sum.id = 'svc-hist-sum'; sum.style.marginBottom = '2px';
    const app = el('p', null, 'hint'); app.id = 'svc-hist-applied';
    const next = el('p', null, 'hint'); next.id = 'svc-hist-next';
    const hintp = el('p', null, 'hint'); hintp.id = 'svc-hist-msg-hint'; hintp.style.color = 'var(--warn)';
    const msg = el('p', null, 'hint'); msg.id = 'svc-hist-msg'; msg.setAttribute('role', 'status');
    const diag = el('details'); diag.id = 'svc-hist-diag'; diag.hidden = true; diag.append(el('summary', 'Диагностика', 'mut3'));
    host.append(row1, row2, row3, range, sum, app, next, hintp, msg, diag);
    box.addEventListener('change', () => histSet(box.checked ? {enabled: 'true', ...(from.value ? {from: from.value} : {})} : {enabled: 'false'}, box));
    from.addEventListener('change', () => { if (from.value) histSet({from: from.value}, from); });
    when.addEventListener('change', () => histSet({when: when.value}, when));
    rf.addEventListener('click', () => {
      range.hidden = !range.hidden;
      if (!range.hidden) { const y = new Date(Date.now() - 864e5); rto.value = rto.value || ymd(y); rfrom.value = rfrom.value || (hist && hist.from) || ''; }
    });
    go.addEventListener('click', () => histRefetch(rfrom.value, rto.value, go));
  }
  const plural = n => n % 100 > 10 && n % 100 < 15 ? 'дат' : n % 10 === 1 ? 'дату' : n % 10 > 1 && n % 10 < 5 ? 'даты' : 'дат';
  function histMsg(t) { const n = $('hist-msg'); if (n) n.textContent = t || ''; }
  function renderHist() {
    const box = $('hist-box'), h = hist;
    if (!box) return;
    // без совместимого фида блок скрыт, если адаптер вообще не про сервисы; с фидом без истории — одна строка почему
    const show = !!(data && data.available && (data.feeds || []).length);
    box.hidden = !show || !h;
    if (box.hidden) return;
    buildHist();
    const w = histWords(h), ok = h.available !== false;
    for (const id of ['on', 'from', 'when', 'refetch']) { const n = $('hist-' + id); if (n) n.disabled = !ok; }
    $('hist-on').checked = !!h.enabled;
    const f = $('hist-from');
    f.min = h.from_min || ''; f.max = ymd(new Date(Date.now() - 864e5));
    if (document.activeElement !== f) f.value = h.from || '';
    $('hist-when').value = h.when || 'off_hours';
    $('hist-sum').textContent = w.summary;
    $('hist-applied').textContent = ok ? w.applied : '';
    $('hist-next').textContent = ok && w.next ? w.next : '';
    $('hist-msg-hint').textContent = ok && w.hint ? w.hint : '';
    const d = $('hist-diag');
    d.hidden = !w.problems.length;
    d.replaceChildren(el('summary', 'Диагностика', 'mut3'), ...w.problems.map(x => el('div', x, 'hint')));
  }
  async function loadHist() {
    try {
      const res = await fetch('/api/v1/adapter-services/history', {cache: 'no-store'});
      if (!res.ok) { hist = null; renderHist(); return; }
      hist = await res.json();
    } catch (_) { return; }
    renderHist();
  }
  async function histSet(fields, ctl) {
    ctl.disabled = true; histMsg('');
    try {
      const [res, r] = await post('/history', new URLSearchParams(fields));
      if (!res.ok) histMsg(TEXT[r.code] || TEXT.operation_failed);
    } catch (_) { histMsg(TEXT.status_unknown); }
    ctl.disabled = false;
    loadHist();
  }
  async function histRefetch(from, to, btn) {
    if (!from || !to) { histMsg('Укажите даты начала и конца периода.'); return; }
    btn.disabled = true; histMsg('');
    try {
      const [res, r] = await post('/history/refetch', new URLSearchParams({from, to}));
      if (!res.ok) histMsg(TEXT[r.code] || TEXT.operation_failed);
      else if (!r.dates) histMsg('В этом периоде нет дат, которые можно запросить заново.');
      else if (window.confirm('Запросить у сервиса заново сделки за ' + r.dates + ' ' + plural(r.dates) + ' (' + dmy(from) + ' — ' + dmy(to) + ')? PnL этих дат будет пересчитан.')) {
        const [res2, r2] = await post('/history/refetch', new URLSearchParams({from, to, confirm: 'true'}));
        histMsg(res2.ok ? 'Поставлено в очередь дат: ' + r2.dates + '.' : (TEXT[r2.code] || TEXT.operation_failed));
        if (res2.ok) $('hist-range').hidden = true;
      }
    } catch (_) { histMsg(TEXT.status_unknown); }
    btn.disabled = false;
    loadHist();
  }

  $('save').addEventListener('click', () => submit('save'));
  $('refresh').addEventListener('click', () => { if (data && data.available) submit('describe'); else refresh(); });
  $('cancel').addEventListener('click', () => cancel('job'));
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearInterval(ticker); forgetSecrets(); });
  refresh();
  const histTimer = setInterval(() => { if (!document.hidden) loadHist(); }, 15000);
  window.addEventListener('pagehide', () => clearInterval(histTimer));
})();
