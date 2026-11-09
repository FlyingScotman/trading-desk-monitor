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
    adapter_reconfigured: 'Настройки адаптера изменились во время ожидания — загрузка запущена заново.',
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
    history_unsupported: 'Адаптер не умеет сделки прошлых дат.',
    adapter_unavailable: 'Адаптер не загружен.', services_unavailable: 'Адаптер не описывает подключение к сервисам.',
    calculation_busy: 'Занят расчёт: данные будут приняты при следующем запуске.', ingestion_busy: 'Загрузка данных занята: повтор позже.',
    files_not_accepted: 'Файлы не приняты.', feed_source_invalid: 'Источник загрузки не объявлен адаптером.'
  };
  // итоги действий адаптера (#615): слова вместо кода. Неизвестный код — первая строка итога, код — во всплывающей подсказке
  const CODE = {
    checked: 'Проверка сервисов завершена.', applied: 'Найденные настройки применены.',
    nothing_to_apply: 'Применять нечего: проверка ничего нового не нашла.',
    environments_loaded: 'Список окружений загружен.', environment_chosen: 'Окружение выбрано.',
    environments_covered: 'Окружения подобраны по книгам.', environments_not_matched: 'Окружения по книгам не подобрались.',
    environments_unknown: 'Окружения ещё не загружены.', environment_missing: 'Не выбрано окружение.',
    coverage_cleared: 'Подбор окружений сброшен.',
    logged_in: 'Вход выполнен.', login_check_failed: 'Вход выполнен, но сервис не ответил на проверочный запрос.',
    login_not_needed: 'Вход не нужен.', login_pending: 'Вход начат: завершите его в браузере.',
    login_required: 'Нужен вход в сервис.', logged_out: 'Выход выполнен: сохранённые ключи забыты.',
    redirect_taken: 'Адрес возврата после входа подставлен.', redirect_missing: 'Не задан адрес возврата после входа.',
    redirect_not_found: 'Адрес возврата после входа не найден.',
    apikey_saved: 'Ключ API принят.', apikey_missing: 'Нет ключа API.',
    product_checked: 'Проверка продукта завершена.', reports_listed: 'Список отчётов получен.',
    names_listed: 'Список названий получен.', listed: 'Список получен.',
    history_probed: 'Проверка прошлых дат завершена.', history_applied: 'Глубина истории применена.',
    portfolios_chosen: 'Портфели книг подобраны.', portfolios_not_matched: 'Портфели для книг не подобрались.',
    portfolios_missing: 'Портфели книг ещё не подобраны.',
    connected: 'Сервис подключён.', connected_partially: 'Сервис подключён частично.',
    no_positions: 'Нет позиций: сначала загрузите позиции.', config_incomplete: 'В настройках не заполнены нужные поля.',
    config_missing: 'Нет файла настроек подключения.', access_denied: 'Нет доступа: у учётной записи нет прав на этот сервис.',
    unknown_action: 'Адаптер не знает этого действия. Нажмите «Обновить».', not_configured: 'Подключение не настроено.',
    source_disabled: 'Источник выключен в настройках адаптера.', service_disabled: 'Сервис выключен в настройках.',
    service_not_configured: 'Не задан адрес сервиса.', not_allowed: 'Такой запрос адаптер не выполняет.',
    auth_required: 'Вход не выполнен.', auth_failed: 'Вход не выполнен: сервер входа отказал.',
    auth_cancelled: 'Вход отменён.', auth_timeout: 'Вход не завершён вовремя.',
    auth_state_mismatch: 'Ответ входа пришёл не на этот запрос: войдите снова.',
    auth_endpoint_missing: 'Не заданы адреса входа.', auth_redirect_rejected: 'Сервер входа не принял адрес возврата после входа.',
    auth_port_busy: 'Порт для возврата после входа занят другой программой: закройте её и войдите снова.',
    auth_port_reserved: 'Порт для возврата после входа зарезервирован системой: перезагрузите компьютер или выберите другой адрес возврата.',
    auth_pending_missing: 'Вход через браузер не начат или его срок истёк: войдите снова.',
    dpapi_unavailable: 'Защищённое хранение ключей недоступно на этом компьютере.', dpapi_failed: 'Защищённое хранение ключей не сработало.',
    secret_missing: 'Сохранённого ключа нет.', windows_auth_unavailable: 'Вход учётной записью Windows здесь недоступен.',
    winhttp_failed: 'Запрос через систему Windows не выполнен.',
    connection_failed: 'Нет соединения с сервером.', tls_failed: 'Ошибка проверки сертификата сервера.',
    timeout: 'Сервер не ответил вовремя.', http_error: 'Сервер ответил ошибкой.', http_not_allowed: 'Адрес без шифрования (http) запрещён.',
    response_too_large: 'Ответ сервера слишком большой.', response_invalid: 'Сервер ответил в неожиданном виде.',
    result_too_large: 'Итог действия слишком большой.', discovery_failed: 'Настройки входа на сервере не найдены.',
    out_dir_missing: 'Нет папки для выгрузок.', date_invalid: 'Неверная дата.', date_unavailable: 'Сервис не отдаёт эту дату.',
    write_failed: 'Файл не записан.', export_failed: 'Выгрузка не выполнена.', export_incomplete: 'Выгружено не всё.',
    export_unreadable: 'Выгрузку не удалось прочитать.', nothing_changed: 'Изменённых сделок нет.',
    no_trade_ids: 'В ответе нет номеров сделок.', trade_id_invalid: 'Номер сделки — только цифры.',
    probe_unavailable: 'Эта проверка не входит в комплект адаптера.', recon_unavailable: 'Сверка не входит в комплект адаптера.',
    envelope_missing: 'Нет выгрузки позиций на эту дату.', file_not_found: 'Файл не найден.',
    marketdata_target_missing: 'Не задана цель рыночных данных.', filter_invalid: 'Фильтр выгрузки задан не по правилам сервиса.',
    books_not_covered: 'Не все книги покрыты.', books_not_mapped: 'Книги сервиса не сопоставлены с книгами монитора.',
    books_unmapped: 'Книги сервиса не сопоставлены с книгами монитора.',
    scope_empty: 'Загружать нечего.', scope_changed: 'Отчёт позиций изменился во время чтения: повторите позже.',
    scope_report_ambiguous: 'Отчётов позиций несколько, непонятно, какой брать.',
    failed: 'Операция не выполнена.', error: 'Операция не выполнена.', partial: 'Выполнено частично.',
    login_restored: 'Вход восстановлен по сохранённому ключу.', source_enabled: 'Источник сделок включён.',
    source_on: 'Источник сделок уже включён.', v2_configured: 'Вход настроен.', exported: 'Выгрузка выполнена.'
  };
  // таблица-чек-лист шагов действия (подключение сервиса сделок): строка = шаг, «Итог» ✓ / ⚠ (готово с предупреждением,
  // например портфели подобраны не для всех книг) / ✗ / — (не дошли). ok: true / 'warn' / false / null
  const CHECKLIST = ['Шаг', 'Итог', 'Секунд', 'Код', 'id'];
  const MARK = {'✓': true, '⚠': 'warn', '⚠\uFE0F': 'warn', '✗': false};
  function checklist(job) {
    const t = job.table;
    if (!t || !Array.isArray(t.columns) || t.columns.join('|') !== CHECKLIST.join('|')) return null;
    const lines = (job.lines || []).slice(), steps = t.rows.map(([title, mark, sec, code, id]) => {
      const ok = Object.prototype.hasOwnProperty.call(MARK, mark) ? MARK[mark] : null, head = String(mark || '') + ' ' + String(title || '');
      // строка адаптера «✓ <шаг>: <итог> (N с)» — итог шага: в чек-лист, не второй раз списком
      const i = lines.findIndex(l => typeof l === 'string' && l.startsWith(head));
      const line = i < 0 ? '' : lines.splice(i, 1)[0].slice(head.length).replace(/^:\s*/, '').replace(/\s*\(\d+(?:[.,]\d+)?\s*с\)\s*$/, '');
      return {id: id || '', title: title == null ? '' : String(title), ok, seconds: typeof sec === 'number' ? sec : null, code: code || null, line};
    });
    return {steps, lines};
  }
  const STATE = {complete: 'Готово.', partial: 'Выполнено частично.', blocked: 'Остановлено.', error: 'Ошибка.', cancelled: 'Отменено.'};
  const RUN = {complete: 'получено', partial: 'получено частично', skipped: 'нечего загружать', blocked: 'остановлено',
    error: 'ошибка', cancelled: 'остановлено'};
  const RUN_CODE = {login_required: 'нужен вход', not_configured: 'не настроено', feed_disabled: 'выключено в адаптере',
    no_change: 'изменений нет', no_books: 'нет книг с позициями', not_due: 'ещё рано', no_positions: 'нет позиций', config_invalid: 'проверьте настройки',
    source_disabled: 'источник выключен', helper_timeout: 'время ожидания истекло', helper_response_invalid: 'некорректный ответ модуля',
    files_not_accepted: 'файлы не приняты', calculation_busy: 'занят расчёт, повтор позже', ingestion_busy: 'загрузка занята, повтор позже',
    runtime_unavailable: 'нет Python для получения данных', adapter_unavailable: 'адаптер не загружен',
    feed_source_invalid: 'источник не объявлен адаптером', cancelled: '',
    adapter_reconfigured: 'настройки адаптера изменились во время ожидания — загрузка запущена заново', export_incomplete: 'выгружено не всё',
    books_not_covered: 'не все книги покрыты', books_not_mapped: 'книги не сопоставлены', books_unmapped: 'книги не сопоставлены',
    date_unavailable: 'дата недоступна', scope_empty: 'нечего загружать', scope_changed: 'отчёт позиций менялся, повтор позже',
    scope_report_ambiguous: 'неоднозначный отчёт позиций', result_too_large: 'слишком большой ответ', failed: 'не выполнено',
    timeout: 'сервер не ответил вовремя', connection_failed: 'нет соединения', access_denied: 'нет доступа',
    http_error: 'сервер ответил ошибкой', portfolios_missing: 'портфели книг не подобраны', apikey_missing: 'нет ключа API',
    config_incomplete: 'настройки не заполнены', export_failed: 'выгрузка не выполнена'};
  const FILE = {accepted: 'принят', partially_accepted: 'принят частично', superseded: 'заменён более новым', duplicate: 'повтор',
    rejected: 'отклонён', failed: 'ошибка чтения', timed_out: 'таймаут чтения', match_failed: 'не распознан', ignored: 'не формат адаптера'};
  const ACTIVE = ['queued', 'waiting', 'running'];
  const GENERIC = ['failed', 'error', 'partial', 'operation_failed'];
  const LOGIN_HINT = 'Если откроется окно входа, завершите вход в нём.';
  let data = null, panelJson = null, typed = {}, timer = null, ticker = null;
  let notice = null;  // отказ «Остановить»/«Отменить» ждущего действия: {text, job}, виден рядом с его кнопками
  const kept = {};  // раздел панели → последнее действие его кнопок: итог виден, пока в разделе не нажата другая кнопка
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
          b.addEventListener('click', () => { if (!a.confirm || window.confirm(a.confirm)) submit('action', {action: a.id}, s.id); });
          row.append(b);
        }
        box.append(row);
      }
      const res = el('div', null, 'svc-sres'); res.dataset.for = s.id; box.append(res);
      host.append(box);
    }
    buttons();
  }
  function buttons() {
    const on = !!(data && data.available && data.panel) && !busy();
    $('save').disabled = !on || !fields().length;
    $('panel').querySelectorAll('button[data-action]').forEach(b => { b.disabled = !on; });
    $('card').querySelectorAll('button[data-pick]').forEach(b => { b.disabled = !on; });
    $('cancel').hidden = !busy();
  }

  function status(text) { const n = $('status'); n.textContent = text || ''; n.removeAttribute('title'); }
  const clock = since => { const sec = Math.max(0, Math.round((Date.now() - new Date(since)) / 1000)); return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0'); };
  const dm = d => d ? d.slice(8, 10) + '.' + d.slice(5, 7) : '';
  // чего ждёт действие владельца: идущая фоновая загрузка (её название и сколько идёт)
  function waitText(w) {
    return 'Ждёт окончания фоновой загрузки «' + (w.title || w.feed) + '»' + (w.kind === 'history' && w.date ? ' за ' + dm(w.date) : '') +
      (w.since ? ', идёт ' + clock(w.since) : '') + (w.phase === 'ingesting' ? ' (данные уже записываются в базу)' : '') + '.';
  }
  // строка состояния действия: {text, cls, title (код), skip — первая строка итога уже в строке состояния}
  function jobView(job) {
    if (!job || job.state === 'idle') return {text: ''};
    if (ACTIVE.includes(job.state)) {
      if (job.state === 'queued' && job.waiting_for) return {text: waitText(job.waiting_for), cls: 'warn'};
      let t = TEXT[job.code] || TEXT.running;
      if (job.state === 'running' && job.started_at) {
        t += ' ' + clock(job.started_at);
        if (job.login) t += ' ' + LOGIN_HINT;  // только у действий входа (#615)
      }
      return {text: t};
    }
    const cls = ['error', 'blocked'].includes(job.state) ? 'bad' : job.state === 'partial' ? 'warn' : '';
    const title = job.code ? 'код: ' + job.code : '';
    if (job.code === 'config_invalid' && job.field) {
      const f = fields().find(x => x.key === job.field);
      return {text: 'Проверьте поле «' + (f ? f.label : job.field) + '».', cls: 'bad', title};
    }
    const prefix = job.state === 'complete' ? '' : (STATE[job.state] || 'Операция не выполнена.') + ' ';
    const ck = checklist(job), first = (ck ? ck.lines : job.lines || [])[0];
    // общий код («не выполнено») скажет меньше, чем первая строка адаптера: тогда строка
    const words = GENERIC.includes(job.code) && first ? null : TEXT[job.code] || CODE[job.code];
    if (words) return {text: (job.state === 'partial' && /частично/.test(words) || prefix.trim() === words ? '' : prefix) + words, cls, title};
    if (first) return {text: prefix + first, cls, title, skip: 1};
    return {text: (prefix.trim() || 'Готово.'), cls, title};
  }
  function jobText(job) { return jobView(job).text; }
  const secs = v => v < 10 ? String(Math.round(v * 10) / 10).replace('.', ',') + ' с' : v < 60 ? Math.round(v) + ' с' : Math.floor(Math.round(v) / 60) + ' мин ' + Math.round(v) % 60 + ' с';
  // итог действия: шаги ✓/✗ (если адаптер их дал), строки, таблица, список выбора
  function resultNodes(job, view, current) {
    const out = [], ck = checklist(job);
    if (ck && ck.steps.length) {
      const ol = el('ol', null, 'svc-steps');
      for (const x of ck.steps) {
        const li = el('li', null, x.ok === true ? 'ok' : x.ok === 'warn' ? 'warn' : x.ok === false ? 'bad' : 'skip');
        li.append(el('span', x.ok === true ? '✓' : x.ok === 'warn' ? '⚠' : x.ok === false ? '✗' : '—', 'mk'), el('span', (x.title || x.id || '') + (x.line ? ': ' + x.line : '')));
        if (typeof x.seconds === 'number') li.append(el('span', secs(x.seconds), 'sec'));
        if (x.code) li.title = 'код: ' + x.code;
        ol.append(li);
      }
      out.push(ol);
    }
    const lines = (ck ? ck.lines : job.lines || []).slice(view.skip || 0);
    if (lines.length) { const ul = el('ul', null, 'at-lines'); ul.append(...lines.map(t => el('li', t))); out.push(ul); }
    if (job.feed_queued) out.push(feedLine(job));
    if (job.table && job.table.columns && !ck) {
      const wrap = el('div'); wrap.style.cssText = 'overflow-x:auto;margin-top:8px';
      const t = el('table', null, 't'), hr = el('tr');
      job.table.columns.forEach(c => hr.append(el('th', c)));
      const head = el('thead'); head.append(hr); const tb = el('tbody');
      for (const row of job.table.rows) { const tr = el('tr'); row.forEach(c => tr.append(el('td', c == null ? '' : String(c)))); tb.append(tr); }
      t.append(head, tb); wrap.append(t); out.push(wrap);
    }
    // список выбора — только у последнего действия: адаптер принимает выбор лишь из последнего ответа
    if (current && Array.isArray(job.choices) && job.choices.length && job.choose_action) {
      const row = el('div', null, 'row'); row.style.cssText = 'margin-top:8px;flex-wrap:wrap';
      const sel = el('select', null, 'inp'); sel.id = 'svc-choice'; sel.style.maxWidth = '100%';
      const empty = el('option', 'Выберите…'); empty.value = ''; sel.append(empty);
      for (const c of job.choices) { const o = el('option', c.label); o.value = c.id; sel.append(o); }
      const b = el('button', 'Выбрать', 'ibtn'); b.type = 'button'; b.id = 'svc-choose'; b.dataset.pick = '1';
      b.addEventListener('click', () => { if (sel.value) submit('action', {action: job.choose_action, arg: sel.value}, job.section); });
      row.append(sel, b); out.push(row);
    }
    return out;
  }
  // подключение дошло: первая загрузка идёт фоновой загрузкой (её запуск поставил монитор) — как она идёт и ссылка на C20
  let dayTrades = null;  // {job, n}: сделки дня по C20 после этой загрузки, спрошены один раз
  const plural3 = (n, a, b, c) => n % 100 > 10 && n % 100 < 15 ? c : n % 10 === 1 ? a : n % 10 > 1 && n % 10 < 5 ? b : c;
  function feedLine(job) {
    const f = ((data && data.feeds) || []).find(x => x.id === job.feed_queued), last = (f && f.last) || {};
    const p = el('p', null, 'svc-feedline'); p.style.margin = '6px 0 0';
    const done = !!(f && !f.running && !f.queued && last.last_finished && job.finished_at && last.last_finished >= job.finished_at);
    let t = 'Сделки дня: ';
    if (!f) t += 'загрузка поставлена в очередь.';
    else if (!done) t += f.running ? 'загружаются…' : 'загрузка в очереди…';
    else if (['complete', 'partial', 'skipped'].includes(last.state)) {
      if (dayTrades && dayTrades.job === job.id && dayTrades.n != null) t += dayTrades.n + ' ' + plural3(dayTrades.n, 'сделка', 'сделки', 'сделок') + '.';
      else { t += RUN[last.state] + '.'; if (!dayTrades || dayTrades.job !== job.id) countTrades(job.id, feedDay(last)); }
    } else { const c = runCode(last); t += (RUN[last.state] || 'не загружены') + (c ? ' (' + c + ')' : '') + '.'; }
    const a = el('a', 'Открыть C20 «Сделки дня» →'); a.href = 'concepts/c20-trades.html';
    p.append(document.createTextNode(t + ' '), a);
    return p;
  }
  // рабочий день, за который загрузка принесла сделки: последняя из её дат; нет дат — сводка берёт последний день со сделками
  function feedDay(last) {
    const ds = (Array.isArray(last.dates) ? last.dates : []).concat(last.date || []).filter(d => /^\d{4}-\d{2}-\d{2}$/.test(d)).sort();
    return ds.length ? ds[ds.length - 1] : null;
  }
  async function countTrades(id, day) {
    dayTrades = {job: id, n: null};
    try {
      const res = await fetch('/api/v1/trades/summary' + (day ? '?date=' + encodeURIComponent(day) : ''), {cache: 'no-store'});
      if (!res.ok) return;
      const r = await res.json();
      if (typeof r.trades === 'number' && dayTrades.job === id) { dayTrades.n = r.trades; if (data) renderResult(data.job); }
    } catch (_) { /* без счёта: ссылка остаётся */ }
  }
  // кнопки, пока действие ждёт: остановить фоновую загрузку, которую оно ждёт, или отменить само действие
  function liveButtons(job, withCancel) {
    const row = el('div', null, 'row'); row.style.cssText = 'margin-top:4px;flex-wrap:wrap;gap:12px';
    if (job.state === 'queued' && job.waiting_for) {
      const stop = el('button', 'Остановить загрузку', 'lnk'); stop.type = 'button'; stop.dataset.stopFeed = '1';
      stop.title = 'Загрузка «' + (job.waiting_for.title || job.waiting_for.feed) + '» прервётся; она не считается ошибкой и запустится снова по своему расписанию.';
      stop.addEventListener('click', () => cancel('feed'));
      row.append(stop);
    }
    if (withCancel) { const c = el('button', 'Отменить', 'lnk'); c.type = 'button'; c.addEventListener('click', () => cancel('job')); row.append(c); }
    if (notice && notice.job === job.id) row.append(el('span', notice.text, 'hint'));
    return row;
  }
  const hostOf = sec => $('panel').querySelector('.svc-sres[data-for="' + CSS.escape(sec) + '"]');
  const sectionOf = job => job && job.op === 'action' && job.section && hostOf(job.section) ? job.section : null;
  function markField(job) {
    $('panel').querySelectorAll('[aria-invalid]').forEach(n => { n.removeAttribute('aria-invalid'); n.style.borderColor = ''; });
    const bad = job && !ACTIVE.includes(job.state) && job.code === 'config_invalid' && job.field && document.getElementById('svc-f-' + job.field);
    if (bad) { bad.setAttribute('aria-invalid', 'true'); bad.style.borderColor = 'var(--neg)'; }
  }
  // итог действия — под кнопками его раздела (#615); без раздела (сохранение, «Обновить») — внизу, под «Сохранить»
  function renderResult(job) {
    markField(job);
    const sec = sectionOf(job), mine = sec && kept[sec];
    // строка, которую раздел показал сам (ожидание ответа или отказ), держится, пока сервер не сообщит о новом действии
    if (sec && !(mine && mine.local && mine.after === job.id)) kept[sec] = job;
    for (const k of Object.keys(kept)) if (!kept[k].local && ACTIVE.includes(kept[k].state) && (!job || kept[k].id !== job.id)) delete kept[k];
    $('panel').querySelectorAll('.svc-sres').forEach(host => {
      const j = kept[host.dataset.for];
      host.replaceChildren();
      if (!j) return;
      const v = j.local ? {text: j.text, cls: j.cls} : jobView(j), st = el('p', v.text, 'svc-st' + (v.cls ? ' ' + v.cls : ''));
      st.setAttribute('role', 'status'); st.setAttribute('aria-live', 'polite');
      if (v.title) st.title = v.title;
      host.append(st);
      if (j.local) return;
      if (ACTIVE.includes(j.state)) host.append(liveButtons(j, true));
      else host.append(...resultNodes(j, v, !!job && j.id === job.id));
    });
    const host = $('result'); host.replaceChildren();
    if (!job || sec) return;
    if (ACTIVE.includes(job.state)) { if (job.waiting_for) host.append(liveButtons(job, false)); return; }
    host.append(...resultNodes(job, jobView(job), true));
  }

  function scheduleText(s, after) {
    let t = !s ? 'вручную' : s.every_min ? 'каждые ' + s.every_min + ' мин, ' + s.from + '–' + s.to : (s.weekdays_only ? 'по будням' : 'ежедневно') + ' в ' + s.daily_at.join(', ');
    if (s && s.every_min && s.weekdays_only) t += ', по будням';
    return after ? t + '; и после новых позиций' : t;
  }
  // код последнего запуска словами: короткие слова строки фида, иначе слова итога, иначе первая строка адаптера
  function runCode(last) {
    const c = last.code;
    if (!c) return '';
    if (Object.prototype.hasOwnProperty.call(RUN_CODE, c)) return RUN_CODE[c];
    if (['complete', 'partial'].includes(last.state)) return '';
    const w = TEXT[c] || CODE[c];
    if (w) return w.charAt(0).toLowerCase() + w.slice(1).replace(/\.$/, '');
    return (last.lines || [])[0] || c;
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
        const code = runCode(last);
        const head = el('div', when(last.last_finished || last.last_started) + ' · ' + (f.running ? 'идёт' : (RUN[last.state] || last.state || '')) + (code && !f.running ? ' (' + code + ')' : ''));
        if (last.code) head.title = 'код: ' + last.code;
        lastTd.append(head);
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
        : f.waits_history ? 'ждёт запроса прошлой даты' : !f.enabled ? 'выключено' : f.queued ? 'в очереди' : f.next_run ? when(f.next_run) + (last.failures ? ' (повтор ' + last.failures + ')' : '') : '—';
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
    else if (job && job.state !== 'idle' && sectionOf(job)) status('');  // состояние — под кнопкой раздела
    else if (job && job.state !== 'idle') { const v = jobView(job); status(v.text); if (v.title) $('status').title = v.title; }
    else if (!data.panel) status(data.describe_error ? 'Панель адаптера не загрузилась: ' + (TEXT[data.describe_error] || data.describe_error) + '. Нажмите «Обновить».' : TEXT.loading);
    else status('');
    renderResult(job); buttons();
    clearTimeout(timer); clearInterval(ticker);
    if (busy()) {
      timer = setTimeout(refresh, 1000);
      if (job.state === 'running' || job.waiting_for) ticker = setInterval(tick, 1000);
    } else if (data.available && (!data.panel && !data.describe_error || data.running || (data.feeds || []).some(f => f.queued || f.running)))
      timer = setTimeout(refresh, 2500);
  }
  function tick() {  // отсчёт времени между опросами: в разделе действия или в общей строке
    const job = data && data.job;
    if (!job) return;
    const sec = sectionOf(job), v = jobView(job);
    if (!sec) { status(v.text); return; }
    const n = hostOf(sec).querySelector('.svc-st');
    if (n) n.textContent = v.text;
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
  async function submit(op, extra, sec) {
    const b = op === 'describe' ? new URLSearchParams() : body(extra);
    forgetSecrets();
    // новое действие раздела: прежний итог раздела убран, состояние — сразу под его кнопками
    const say = (text, cls) => {
      if (sec && hostOf(sec)) { kept[sec] = {local: true, text, cls, after: data && data.job && data.job.id}; renderResult(data && data.job); }
      else status(text);
    };
    say(TEXT.queued, '');
    try {
      const [res, job] = await post('/' + op, b);
      if (!res.ok) { say(TEXT[job.code] || TEXT.operation_failed, 'bad'); return; }
      data = Object.assign({}, data, {job}); render();
    } catch (_) { say(TEXT.status_unknown, 'bad'); }
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
    const job = data && data.job, say = t => { if (job && ACTIVE.includes(job.state)) notice = {text: t, job: job.id}; else status(t); };
    try {
      const [res, r] = await post('/cancel', new URLSearchParams({target}));
      if (!res.ok) say(TEXT[r.code] || TEXT.operation_failed);
    } catch (_) { say(TEXT.status_unknown); }
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
    for (const [v, t] of [['any', 'В любое время (по умолчанию)'], ['off_hours', 'Только вне торговых часов']]) { const o = el('option', t); o.value = v; when.append(o); }
    const row2 = el('div', null, 'row'); row2.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    const wl = el('label', 'Когда спрашивать сервис'); wl.htmlFor = 'svc-hist-when'; row2.append(wl, when);
    const hrs = el('p', 'Торговые часы — будни 09:30–19:45 по Москве. При «в любое время» пересчёт прошлых дат идёт и днём (шаг «История прогонов») и удлиняет цикл автопилота до времени, отведённого истории прогонов за цикл.', 'hint');
    const rf = el('button', 'Перезапросить период…', 'ibtn'); rf.type = 'button'; rf.id = 'svc-hist-refetch';
    const row3 = el('div', null, 'row'); row3.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    const rfrom = el('input', null, 'inp mono'), rto = el('input', null, 'inp mono'), go = el('button', 'Запросить заново', 'btn');
    rfrom.type = rto.type = 'date'; rfrom.id = 'svc-hist-rf'; rto.id = 'svc-hist-rt'; rfrom.style.width = rto.style.width = '170px'; go.type = 'button'; go.id = 'svc-hist-go';
    const range = el('div', null, 'row'); range.id = 'svc-hist-range'; range.hidden = true; range.style.cssText = 'flex-wrap:wrap;align-items:center;gap:8px;margin-top:8px';
    range.append(el('span', 'С'), rfrom, el('span', 'по'), rto, go);
    row3.append(rf);
    const sum = el('p'); sum.id = 'svc-hist-sum'; sum.style.marginBottom = '2px';
    const app = el('p', null, 'hint'); app.id = 'svc-hist-applied';
    const next = el('p', null, 'hint'); next.id = 'svc-hist-next'; next.append(document.createTextNode(''));
    const hintp = el('p', null, 'hint'); hintp.id = 'svc-hist-msg-hint'; hintp.style.color = 'var(--warn)';
    const msg = el('p', null, 'hint'); msg.id = 'svc-hist-msg'; msg.setAttribute('role', 'status');
    const diag = el('details'); diag.id = 'svc-hist-diag'; diag.hidden = true; diag.append(el('summary', 'Диагностика', 'mut3'));
    const stop = el('button', 'Остановить запрос', 'lnk'); stop.type = 'button'; stop.id = 'svc-hist-stop'; stop.hidden = true;
    stop.title = 'Догрузка продолжится со следующей даты через пару минут; чтобы выключить — снимите переключатель.';
    stop.addEventListener('click', () => cancel('feed'));
    next.append(' ', stop);
    host.append(row1, row2, hrs, row3, range, sum, app, next, hintp, msg, diag);
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
    $('hist-when').value = h.when || 'any';
    $('hist-sum').textContent = w.summary;
    $('hist-applied').textContent = ok ? w.applied : '';
    $('hist-next').firstChild.textContent = ok && w.next ? w.next : '';
    $('hist-stop').hidden = !(ok && h.current);
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
