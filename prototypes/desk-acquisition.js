/* Explicit local acquisition. Credentials never enter settings, storage, URLs or diagnostic output. */
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
    acquisition_busy: 'Предыдущая операция ещё выполняется.', calculation_busy: 'Уже выполняется расчёт. Повторите после его завершения.',
    starting: 'Подготовка запуска…', acquiring: 'Получение данных…',
    ingesting: 'Проверка и загрузка полученных данных… Отмена уже недоступна.',
    valuating: 'Новый расчёт выбранной даты… Отмена уже недоступна.',
    market_missing: 'Для этой даты нет загруженного рынка.', positions_missing: 'Для выбранных книг нет загруженных позиций на эту дату.',
    cancelled: 'Операция отменена до загрузки данных.', cancel_requested: 'Отмена запрошена…',
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
  let reports = [], scopeFingerprint = null, active = false, available = false, timer = null;
  const el = (tag, text) => { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; return n; };
  function message(code) { $('status').textContent = TEXT[code] || 'Операция не выполнена. Проверьте диагностику программы.'; }
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
  function show(job) {
    active = job.state === 'running'; message(job.code); $('result').replaceChildren();
    if (job.code === 'scope_ready') { scopeFingerprint = job.adapter_fingerprint; scope(job.reports || []); }
    $('cancel').disabled = !active || ['ingesting', 'valuating'].includes(job.phase);
    if (job.code === 'completed') {
      if (job.state === 'partial') $('result').append(el('p', 'Результат частичный: часть сделок не оценена или использует данные отчёта. Смотрите причины в новом расчёте.'));
      if (job.valuation) for (const [group, values] of Object.entries(job.valuation)) {
        $('result').append(el('p', (group === 'counts' ? 'Статусы: ' : 'Причины: ') +
          Object.entries(values).map(([key, count]) => key + ': ' + count).join(', ')));
      }
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
      if (data.job.state === 'idle') { active = false; message(available ? 'idle' : data.code); buttons(); }
      else show(data.job);
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
      body.set('username', $('user').value); body.set('password', $('password').value);
      $('user').value = ''; $('password').value = '';
    }
    active = true; buttons(); message('starting');
    try {
      const res = await fetch('/api/v1/acquisition/start', {method:'POST', cache:'no-store',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body});
      const job = await res.json();
      if (!res.ok) { active = false; message(job.code); buttons(); } else show(job);
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
