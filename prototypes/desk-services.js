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
    runtime_unavailable: 'Укажите и сохраните «Python для получения данных» (ниже, «Получить условия сделок» → «Дополнительно»).',
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
    cancel_after_commit_unavailable: 'Данные уже загружаются в базу: отмена на этой фазе недоступна.'
  };
  const STATE = {complete: 'Готово.', partial: 'Выполнено частично.', blocked: 'Остановлено.', error: 'Ошибка.'};
  const RUN = {complete: 'получено', partial: 'получено частично', skipped: 'нечего загружать', blocked: 'остановлено',
    error: 'ошибка', cancelled: 'отменено'};
  const RUN_CODE = {login_required: 'нужен вход', not_configured: 'не настроено', feed_disabled: 'выключено в адаптере',
    no_change: 'изменений нет', not_due: 'ещё рано', no_positions: 'нет позиций', config_invalid: 'проверьте настройки',
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
    const t = el('table', null, 't'), hr = el('tr');
    ['Данные', 'Включено', 'Расписание', 'Последний запуск', 'Следующий', ''].forEach(c => hr.append(el('th', c)));
    const head = el('thead'); head.append(hr); const tb = el('tbody');
    for (const f of feeds) {
      const tr = el('tr'); tr.dataset.feed = f.id; const last = f.last || {};
      const name = el('td'); name.append(el('div', f.title)); name.append(el('div', f.dataset, 'mut3 mono'));
      const sw = el('td'), lab = el('label', null, 'tgl'), box = el('input'); box.type = 'checkbox'; box.checked = !!f.enabled;
      box.setAttribute('aria-label', 'Фоновая загрузка «' + f.title + '»');
      lab.append(box, el('span', null, 'tgl-s')); sw.append(lab);
      box.addEventListener('change', () => enable(f.id, box.checked, box));
      const lastTd = el('td');
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
      const next = el('td');
      next.textContent = last.needs_login ? 'нужен вход: выполните вход в панели выше'
        : !f.ready ? 'не готово' + (f.why_not_ready ? ': ' + f.why_not_ready : '')
        : !f.enabled ? 'выключено' : f.queued ? 'в очереди' : f.next_run ? when(f.next_run) + (last.failures ? ' (повтор ' + last.failures + ')' : '') : '—';
      if (last.needs_login) next.style.color = 'var(--warn)';
      const act = el('td'), run = el('button', 'Запустить сейчас', 'ibtn'); run.type = 'button';
      run.disabled = f.running || f.queued; run.addEventListener('click', () => runNow(f.id, run));
      act.append(run);
      if (f.running) { const stop = el('button', 'Остановить', 'lnk'); stop.type = 'button'; stop.addEventListener('click', () => cancel('feed')); act.append(stop); }
      tr.append(name, sw, el('td', scheduleText(f.schedule, f.after_positions)), lastTd, next, act); tb.append(tr);
    }
    t.append(head, tb); host.replaceChildren(t);
  }

  function render() {
    const card = $('card');
    if (!data) return;
    // без возможности в адаптере (или без адаптера) блока нет: о сервисах монитор ничего не знает
    card.hidden = !data.available && ['services_unavailable', 'adapter_unavailable', undefined].includes(data.code);
    if (card.hidden) return;
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
      if (res.status === 403) { $('card').hidden = true; return; }
      if (!res.ok) { status(TEXT.status_unknown); return; }
      const before = data && data.job && data.job.id, was = before && ACTIVE.includes(data.job.state);
      data = await res.json();
      // сохранено или перечитано: панель заново с сервера, набранное забыто
      if (was && data.job && data.job.id === before && ['saved', 'described'].includes(data.job.code)) { typed = {}; panelJson = null; }
      render();
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
  $('save').addEventListener('click', () => submit('save'));
  $('refresh').addEventListener('click', () => { if (data && data.available) submit('describe'); else refresh(); });
  $('cancel').addEventListener('click', () => cancel('job'));
  window.addEventListener('pagehide', () => { clearTimeout(timer); clearInterval(ticker); forgetSecrets(); });
  refresh();
})();
