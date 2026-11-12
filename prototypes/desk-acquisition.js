/* Explicit local acquisition. Credentials never enter settings, storage, URLs or diagnostic output.
   A provider without a login: the server reports `login: "none"` (from its last inspection of this adapter);
   then the login fields are hidden and check/fetch go without them. The server, not this page, decides.
   Before the first inspection of this adapter the way to log in is not known (`login_known: false`): no fields as if a login
   were certain, a hint instead; the server still refuses a start without credentials until it knows (`credentials_required`).
   TDM#673: a start while a background load holds the helper is accepted and waits: the job says what it waits for. */
(() => {
  'use strict';
  const $ = id => document.getElementById('acq-' + id);
  if (!$('status')) return;
  const TEXT = {
    idle: 'Выберите дату и найдите отчёт.', available: 'Получение данных доступно.',
    adapter_unavailable: 'Адаптер не загружен. Проверьте папку адаптера.',
    acquisition_unavailable: 'Этот адаптер не поддерживает получение данных. Нужен совместимый приватный комплект.',
    helper_unavailable: 'В комплекте адаптера отсутствует модуль получения данных.',
    capability_invalid: 'Несовместимое описание получения данных в адаптере.',
    runtime_unavailable: 'Укажите и сохраните путь к Python с библиотекой внутренней системы.',
    settings_invalid: 'Проверьте настройки программы.', config_invalid: 'Проверьте форму подключения адаптера.',
    source_disabled: 'Нужный источник выключен. Включите его через форму источников и сохраните.',
    scope_report_missing: 'В папках отчётов нет подходящего отчёта выбранной даты.',
    scope_report_ambiguous: 'Выберите один отчёт этой даты.', scope_changed: 'Отчёт или настройки изменились. Найдите отчёт заново.',
    scope_book_missing: 'В отчёте не найдены выбранные книги.', scope_book_conflict: 'Один номер сделки относится к разным книгам. Исправьте состав отчёта до запуска.',
    scope_empty: 'Выберите книги с подходящими сделками.', scope_too_large: 'Слишком много отчётов. Уточните папки.',
    scope_invalid: 'Отчёт выбранной даты не прочитан. Проверьте его формат.', pack_failed: 'Не удалось собрать полученные данные для загрузки.',
    scope_ready: 'Проверьте отчёт, книги и фильтры перед получением данных.',
    credentials_required: 'Введите логин и пароль для этого запуска.',
    invalid_request: 'Проверьте заполненные поля.', local_only: 'Запуск доступен только на компьютере с программой.',
    origin_invalid: 'Откройте настройки по локальному адресу программы.',
    connection_failed: 'Не удалось подключиться. Проверьте подключение и настройки адаптера.',
    auth_failed: 'Вход во внутреннюю систему не выполнен.', connected: 'Вход проверен. Данные не загружались.',
    connected_open: 'Подключение проверено. Данные не загружались.',
    acquisition_busy: 'Предыдущая операция ещё выполняется.', calculation_busy: 'Уже выполняется расчёт. Повторите после его завершения.',
    starting: 'Подготовка запуска…', acquiring: 'Получение данных…',
    ingesting: 'Проверка и загрузка полученных данных… Отмена уже недоступна.',
    valuating: 'Новый расчёт выбранной даты… Отмена уже недоступна.',
    market_missing: 'Для этой даты нет загруженного рынка.', positions_missing: 'Для выбранных книг нет загруженных позиций на эту дату.',
    cancelled: 'Операция отменена до загрузки данных.', cancelled_waiting: 'Ожидание отменено: ничего не запускалось.',
    cancel_requested: 'Отмена запрошена…', waiting_helper: 'Ждёт окончания другой операции с адаптером. Запуск начнётся сам.',
    stop_unavailable: 'Фоновая загрузка уже записывает данные в базу: остановить её сейчас нельзя. Запуск начнётся сам, когда она закончится.',
    cancel_after_commit_unavailable: 'Загрузка уже началась. Дождитесь результата; отмена на этой фазе недоступна.',
    acquisition_timeout: 'Время ожидания истекло. Процесс получения данных остановлен.',
    adapter_changed: 'Адаптер или его настройки изменились. Найдите отчёт заново.',
    snapshot_not_accepted: 'Полученный состав данных не принят. Проверьте диагностику входящих данных.',
    snapshot_not_current: 'Полученные данные не стали текущими для выбранных книг и даты.',
    ingestion_busy: 'Загрузка входящих данных занята. Повторите позже.',
    ingestion_failed: 'Не удалось загрузить данные. Проверьте диагностику входящих данных.',
    valuation_failed: 'Данные загружены, но новый расчёт не завершён. Проверьте состояние расчётов.',
    run_not_current: 'После загрузки входные данные изменились. Свежесть расчёта не подтверждена.',
    completed: 'Данные приняты, новый расчёт завершён.',
    provider_response_invalid: 'Модуль получения данных вернул некорректный ответ.',
    provider_failed: 'Получение данных завершилось ошибкой.', operation_failed: 'Операция не выполнена.',
    status_unknown: 'Нет связи с программой. Запуск мог продолжиться; обновите статус перед повторным запуском.',
    export_failed: 'Не удалось сохранить полученные данные.', export_invalid: 'Некорректные данные для выгрузки.',
    cache_invalid: 'Некорректный журнал выгрузки.', cache_invalidate_failed: 'Не удалось обновить локальную выгрузку.'
  };
  let reports = [], scopeFingerprint = null, active = false, available = false, timer = null, noLogin = false, loginKnown = true, loginAsked = false;
  const el = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
  const clock = since => { const sec = Math.max(0, Math.round((Date.now() - new Date(since)) / 1000)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); };
  const dm = d => typeof d === 'string' && d.length >= 10 ? d.slice(8, 10) + '.' + d.slice(5, 7) : '';
  // чего ждёт действие (TDM#673): идущая фоновая загрузка или операция раздела подключения к сервисам, и сколько она идёт
  function waitText(job) {
    const w = job.waiting_for || {}, run = w.since ? ', идёт ' + clock(w.since) : '';
    if (job.code === 'waiting_feed') return 'Ждёт окончания фоновой загрузки «' + (w.title || w.feed || '') + '»' +
      (w.kind === 'history' && w.date ? ' за ' + dm(w.date) : '') + run + (w.phase === 'ingesting' ? ' (данные уже записываются в базу)' : '') +
      '. Запуск начнётся сам.';
    if (job.code === 'waiting_services') return 'Ждёт окончания операции в разделе «Подключение к сервисам»' + run + '. Запуск начнётся сам.';
    return TEXT.waiting_helper;
  }
  function message(code, job) {
    if (code === 'connected' && noLogin) code = 'connected_open';
    if (job && job.phase === 'waiting' && String(code).startsWith('waiting_')) { $('status').textContent = waitText(job); return; }
    if (code === 'cancelled' && job && job.phase === 'waiting') code = 'cancelled_waiting';
    $('status').textContent = TEXT[code] || 'Операция не выполнена. Проверьте диагностику программы.';
  }
  // Only the exact "none" hides the login; "required" (older backend, absent, unknown value) keeps the fields. `known` false: no
  // inspection of this adapter has answered yet: neither the fields nor "no login needed", only the hint (TDM#673).
  // `loginAsked`: the server refused a start without credentials: the fields stay while it does not know better, so the next poll
  // (still "not known") does not hide them again; the server's own answer ends it.
  function login(mode, known = true) {
    if (known) loginAsked = false; else if (loginAsked && available) known = true;
    noLogin = mode === 'none'; loginKnown = known;
    const hide = noLogin || !known;
    for (const n of [document.querySelector('label[for=acq-user]'), $('user'), document.querySelector('label[for=acq-password]'),
      $('password').parentElement]) if (n) n.hidden = hide;
    if ($('nologin')) $('nologin').hidden = !noLogin;
    if ($('loginunknown')) $('loginunknown').hidden = known || !available;
    $('check').textContent = noLogin ? 'Проверить подключение' : 'Проверить вход';
    if (hide) { $('user').value = ''; $('password').value = ''; }
  }
  // «Остановить загрузку»: тот же маршрут, что в разделе «Сервисы»; загрузка не считается ошибкой и идёт снова по расписанию
  function stopLink(w) {
    const row = el('span'), stop = el('button', 'Остановить загрузку', 'lnk'), note = el('span', '', 'hint');
    stop.type = 'button'; stop.dataset.stopFeed = '1'; stop.className = 'lnk'; note.className = 'hint'; note.style.marginLeft = '8px';
    stop.title = 'Загрузка «' + (w.title || w.feed || '') + '» прервётся; она не считается ошибкой и запустится снова по своему расписанию.';
    stop.addEventListener('click', async () => {
      note.textContent = '';
      try {
        const res = await fetch('/api/v1/adapter-services/cancel', {method:'POST', cache:'no-store',
          headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:'target=feed'});
        if (!res.ok) { const r = await res.json().catch(() => ({})); note.textContent = TEXT[r.code === 'cancel_after_commit_unavailable' ? 'stop_unavailable' : 'operation_failed']; }
      } catch (_) { note.textContent = TEXT.status_unknown; }
      refresh();
    });
    row.append(stop, note); return row;
  }
  function selected() { return [...$('scope').querySelectorAll('input[type=checkbox]:checked')].map(n => n.value); }
  function report() { return reports.find(r => r.id === $('report')?.value); }
  function buttons() {
    for (const id of ['inspect', 'check', 'date']) $(id).disabled = active;
    $('inspect').disabled ||= !available;
    $('check').disabled ||= !available;
    $('fetch').disabled = active || !available || !selected().length || !report();
    $('scope').querySelectorAll('input,select').forEach(n => { n.disabled = active; });
  }
  function books() {
    const r = report(), target = $('books'); target.replaceChildren();
    if (!r) { buttons(); return; }
    const p = r.policy;
    target.append(el('p', 'Статус: ' + p.status + '. Исключены продукты: ' + (p.excluded_products.join(', ') || 'нет') + '.'));
    if (p.skipped && p.skipped.trades > 0) target.append(el('p', 'Пропущено сделок: ' + p.skipped.trades + ' (книг: ' + p.skipped.books +
      '). Это сделки книг, у которых в отчёте позиций нет деривативов; эти книги в списке не предлагаются.'));
    target.append(el('p', 'Все подходящие сделки выбранных книг из этого отчёта; состав не ограничивается позициями портфеля.'));
    for (const book of r.books) {
      const info = p.books[book], label = el('label'), check = el('input');
      check.type = 'checkbox'; check.value = book; check.addEventListener('change', buttons);
      label.style.cssText = 'display:block;overflow-wrap:anywhere;margin:8px 0';
      label.append(check, document.createTextNode(' ' + book + ' — сделок: ' + info.eligible + '; ' + info.products.join(', ')));
      target.append(label);
    }
    if (!r.books.length) target.append(el('p', 'Подходящих сделок нет. Другие статусы автоматически не включаются.'));
    buttons();
  }
  function scope(rows) {
    reports = rows; $('scope').replaceChildren();
    const select = el('select'); select.id = 'acq-report'; select.className = 'inp'; select.style.maxWidth = '100%';
    const empty = el('option', rows.length === 1 ? 'Выберите отчёт' : 'Найдено несколько отчётов — выберите один'); empty.value = ''; select.append(empty);
    for (const r of rows) { const o = el('option', r.name); o.value = r.id; select.append(o); }
    const target = el('div'); target.id = 'acq-books';
    select.addEventListener('change', books); $('scope').append(select, target);
    if (rows.length === 1) select.value = rows[0].id;
    books();
  }
  // `current`: the status route's answer for the current adapter; it wins over the login an inspection job carries;
  // `known`: whether an inspection of this adapter has answered (an older backend does not say: known)
  function show(job, current, known = true) {
    if (current !== undefined || job.code === 'scope_ready') login(current !== undefined ? current : job.login, current !== undefined ? known : true);
    active = job.state === 'running'; message(job.code, job);
    // a wait for a background load: its stop link stays put between polls (it is not rebuilt under the owner's mouse)
    const w = active && job.phase === 'waiting' && job.waiting_for && ['feed', 'history'].includes(job.waiting_for.kind) ? job.waiting_for : null;
    if (!(w && $('result').querySelector('[data-stop-feed]'))) {
      $('result').replaceChildren();
      if (w) $('result').append(stopLink(w));
    }
    if (job.code === 'scope_ready') { scopeFingerprint = job.adapter_fingerprint; scope(job.reports || []); }
    $('cancel').disabled = !active || ['ingesting', 'valuating'].includes(job.phase);
    if (job.code === 'completed') {
      if (job.state === 'partial') $('result').append(el('p', 'Результат частичный: часть сделок не оценена или использует данные отчёта. Смотрите причины в новом расчёте.'));
      if (job.valuation) for (const [group, values] of Object.entries(job.valuation)) {
        $('result').append(el('p', (group === 'counts' ? 'Статусы: ' : 'Причины: ') +
          Object.entries(values).map(([key, count]) => key + ': ' + count).join(', ')));
      }
      // #547: сегодняшние расчёты, пересчитанные на новых условиях (утренний или последний дневной)
      if (Array.isArray(job.recomputed) && job.recomputed.length)
        $('result').append(el('p', 'Пересчитаны также: ' + job.recomputed.map(r => r.label + ' (#' + r.run_id + ')').join(', ') + '.'));
      if (Array.isArray(job.recompute_failed) && job.recompute_failed.length)
        $('result').append(el('p', 'Не пересчитаны: ' + job.recompute_failed.map(r => r.label).join(', ') + '. Их пересчитает автопилот.'));
      if (Number.isSafeInteger(job.run_id)) {
        const link = el('a', 'Открыть новый расчёт'); link.href = '/concepts/c01-cockpit.html?run_id=' + job.run_id; $('result').append(link);
      }
    }
    buttons();
    clearTimeout(timer);
    if (active) timer = setTimeout(refresh, 700);
  }
  async function refresh() {
    try {
      const res = await fetch('/api/v1/acquisition', {cache:'no-store'});
      if (!res.ok) { available = false; message(res.status === 403 ? 'local_only' : 'status_unknown'); buttons(); return; }
      const data = await res.json(); available = !!data.available;
      const known = data.login_known !== false;
      if (data.job.state === 'idle') { active = false; login(data.login, known); message(available ? 'idle' : data.code); buttons(); }
      else show(data.job, data.login || 'required', known);  // unavailable adapter: no current answer, keep the login
    } catch (_) { message('status_unknown'); }
  }
  async function submit(op) {
    const body = new URLSearchParams({op});
    if (op !== 'check') body.set('date', $('date').value);
    if (op === 'fetch') {
      const r = report(); if (!r) { message('scope_report_missing'); return; }
      body.set('report', r.id); body.set('report_sha256', r.sha256); selected().forEach(b => body.append('book', b));
      body.set('adapter_fingerprint', scopeFingerprint || '');
    }
    if (op !== 'inspect') {
      if (!noLogin && loginKnown) { body.set('username', $('user').value); body.set('password', $('password').value); }
      $('user').value = ''; $('password').value = '';
    }
    active = true; buttons(); message('starting');
    try {
      const res = await fetch('/api/v1/acquisition/start', {method:'POST', cache:'no-store',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
      const job = await res.json();
      if (!res.ok) { active = false; if (job.code === 'credentials_required') { login('required'); loginAsked = true; } message(job.code, job); buttons(); } else show(job);
    } catch (_) { active = false; message('status_unknown'); buttons(); }
    finally { body.delete('username'); body.delete('password'); }
  }
  $('inspect').addEventListener('click', () => submit('inspect'));
  $('check').addEventListener('click', () => submit('check'));
  $('fetch').addEventListener('click', () => submit('fetch'));
  $('refresh').addEventListener('click', refresh);
  $('date').addEventListener('change', () => { reports = []; scopeFingerprint = null; $('scope').replaceChildren(); buttons(); });
  $('cancel').addEventListener('click', async () => {
    try {
      const res = await fetch('/api/v1/acquisition/cancel', {method:'POST', headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:''});
      const job = await res.json(); if (res.ok) show(job); else message(job.code);
    } catch (_) { message('operation_failed'); }
  });
  window.addEventListener('pagehide', () => { clearTimeout(timer); $('user').value = ''; $('password').value = ''; });
  refresh();
})();
