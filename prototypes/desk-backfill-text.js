/*
 * Догрузка истории одними словами (#447, #449): чип в шапке мониторов (concepts/lib/kit.js, K.backfillStatus) и
 * Настройки (сводка сверху, «История прогонов» в Автообновлении и Диагностике) по GET /api/v1/backfill/status.
 *  T.CAUSE, T.cause(st)   — почему догрузка стоит (stalled.cause, backend: backfill.stall_cause; без него — по
 *                           stalled.source: база или сеть);
 *  T.when(iso)            — момент по МСК: «ЧЧ:ММ» сегодня, иначе «ДД.ММ ЧЧ:ММ»;
 *  T.dates(j)             — {conv, load}: даты, которые «не сходятся» (своя проблема расчёта: failed, unconverged) и
 *                           «не загружаются» (своя ошибка сети или базы, когда другие даты грузятся, — kind
 *                           unreachable; даты хаба, и при выключенной догрузке: их грузит шаг sync), без повторов;
 *  T.n(n, one, many)      — «1 не сходится», «2 не сходятся»: число со своим словом;
 *  T.stall(j)             — {why, retry, text, title} застоя или null (догрузка выключена, дат к расчёту нет):
 *                           «стоит: <причина>, повтор в ЧЧ:ММ» (retry — «, повтор в ЧЧ:ММ», пусто, когда время
 *                           прошло или автопилот выключен), в подсказке — с какого времени, дата и ошибка целиком;
 *  T.passes(j)            — два прохода пересчёта истории (TDM#627): строки «Проход 1 из 2 …» / «Проход 2 из 2 …», коротко для чипа,
 *                           проблемные даты второго прохода; null — ответ прежнего сервера, без pass;
 *  T.pending(h)           — «Файлы ждут окончания расчёта» и причина (TDM#732): файлы дат ждут блокировки расчёта;
 *  T.history(h)           — сделки прошлых дат (TDM#569, GET /api/v1/adapter-services/history): {unsupported, summary,
 *                           applied, runs, next, problems}: слова экрана «Подключение к сервисам» и строка «История
 *                           прогонов» («PnL сделок учтён в X датах, ждут пересчёта Y»).
 * Подключается раньше kit.js (concepts/lib/boot.js) и в settings.html; window.DESK_BACKFILL_TEXT.
 */
(function (root) {
  'use strict';
  var T = {}, MSK_MS = 3 * 3600e3;  // московское время — UTC+3 круглый год
  T.CAUSE = { network: 'нет связи', proxy: 'прокси не пропускает', access: 'нет доступа к хабу (токен)', setup: 'хаб не настроен',
    hub: 'хаб не отвечает', store: 'база недоступна', store_slow: 'база не успевает', disk: 'ошибка диска' };
  T.cause = function (st) {
    var s = st && typeof st === 'object' ? st : { cause: st };
    return T.CAUSE[s.cause] || (s.source === 'store' ? T.CAUSE.store : 'ошибка сети или хаба');
  };
  function msk(t) { return new Date(+new Date(t) + MSK_MS).toISOString(); }
  function dmy(iso) { return iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) : ''; }
  T.when = function (iso, now) {
    if (!iso) return '—';
    var t = msk(iso), today = msk(now == null ? Date.now() : now);
    return (t.slice(0, 10) === today.slice(0, 10) ? '' : t.slice(8, 10) + '.' + t.slice(5, 7) + ' ') + t.slice(11, 16);
  };
  T.n = function (n, one, many) { return n + (n % 10 === 1 && n % 100 !== 11 ? one : many); };
  // число с формой слова: 1 дата, 2 даты, 5 дат
  T.plural = function (n, one, few, many) {
    var m = n % 100, k = n % 10;
    return n + ' ' + (m > 10 && m < 15 ? many : k === 1 ? one : k >= 2 && k <= 4 ? few : many);
  };
  // оценка времени: «5 мин» до полутора часов, дальше в часах
  T.eta = function (s) { return s < 5400 ? Math.max(1, Math.round(s / 60)) + ' мин' : Math.round(s / 3600) + ' ч'; };
  // ---- два прохода пересчёта истории (TDM#627): pass и second в GET /api/v1/backfill/status ----
  // Первый проход считает стоимость, риск и PnL каждой даты без вторых производных (дёшево), второй досчитывает вторые
  // производные от самых новых дат; читает их только быстрая оценка сценариев. «Готово» — только когда pass == «done».
  // Ответ без pass (прежний сервер) — null: страницы показывают по-старому.
  T.SECOND = 'вторые производные (выпуклость, гамма)';
  T.passes = function (j) {
    var p = j && j.pass;
    if (p !== 'first' && p !== 'second' && p !== 'done') return null;
    var sec = j.second || {}, d1 = j.done || 0, m1 = j.total != null ? j.total : d1 + (j.todo || 0) + (j.failed || 0),
      d2 = sec.done || 0, m2 = sec.total != null ? sec.total : d2 + (sec.todo || 0) + (sec.failed || 0),
      eta = p === 'second' && sec.todo && sec.eta_s ? T.eta(sec.eta_s) : null, failed2 = sec.failed || 0,
      tries = function (x) { return x.attempts ? ', попыток: ' + x.attempts : ''; };
    return { pass: p, done: p === 'done', eta: eta, failed2: failed2, next: p === 'second' ? sec.next || [] : j.next || [],
      first: 'Проход 1 из 2: стоимость, риск и PnL — ' + d1 + ' из ' + m1 + ' дат' + (p === 'first' ? '' : j.failed ? ', остальные не сходятся' : ', готово'),
      second: 'Проход 2 из 2: ' + T.SECOND + ' — ' + (p === 'first' ? 'начнётся, когда первый проход посчитает все даты'
        : d2 + ' из ' + m2 + ' дат' + (eta ? ', осталось ≈ ' + eta : '') + (p === 'done' ? (failed2 ? ', не вышло у ' + failed2 : ', готово') : '')),
      // коротко, для чипа шапки и сводки Настроек
      short: p === 'first' ? 'проход 1 из 2: ' + d1 + ' из ' + m1 + ' дат' : p === 'second' ? 'проход 2 из 2 (вторые производные): ' + d2 + ' из ' + m2 + ' дат' : '',
      noSecond: failed2 ? T.plural(failed2, 'дата', 'даты', 'дат') + ' без вторых производных' : '',
      problems: (sec.problem_dates || []).map(function (x) { return dmy(x.date) + ' — ' + (x.error || 'расчёт упал') + tries(x); }) };
  };
  T.dates = function (j) {
    var on = j && j.enabled !== false, seen = {}, conv = [], load = [];
    (on ? j.problem_dates || [] : []).forEach(function (p) { seen[p.date] = 1; (p.kind === 'unreachable' ? load : conv).push(p); });
    ((j && j.hub_problem_dates) || []).forEach(function (p) { if (!seen[p.date]) { seen[p.date] = 1; load.push(p); } });
    return { conv: conv, load: load };
  };
  // ---- сделки прошлых дат (TDM#569): GET /api/v1/adapter-services/history ----
  T.HIST_WAIT = { login_required: 'нужен вход: выполните вход в панели выше', config_invalid: 'проверьте настройки подключения',
    source_disabled: 'источник выключен в адаптере', not_ready: 'загрузка сделок не готова' };
  T.HIST_GATE = { trading_hours: 'ждёт конца торговых часов', daily_limit: 'на сегодня достигнут предел запросов',
    gap: 'пауза между запросами', backoff: 'повтор после ошибки',
    no_trades_week: 'ждёт сделок: «Сделки дня» не принесли ни одной сделки за последние 5 рабочих дней (пока их нет, прошлые даты не запрашиваются)' };
  T.HIST_REASON = { no_prev_close: 'нет предыдущего закрытия', no_pnl: 'нет PnL позиции на начало дня', lost: 'бумага не оценена',
    unpriced: 'сделки без цены или курса', no_pnl_run: 'PnL в прогоне не считался', unvalued: 'бумага не оценена совсем' };
  T.HIST_CODE = { date_unavailable: 'сервис не отдаёт эту дату', books_unmapped: 'книги сервиса не сопоставлены с книгами монитора',
    export_incomplete: 'выгрузка неполная', files_not_accepted: 'файлы не приняты', empty: 'пусто, причина не указана',
    filtered_empty: 'пусто по выбранным портфелям; повтор после новой версии адаптера' };
  // файлы дат, которые ждут блокировки расчёта (pending: сколько дат, или true у прежнего сервера; pending_why — почему)
  T.pending = function (h) {
    var n = typeof h.pending === 'number' ? h.pending : 1;
    return 'Файлы ждут окончания расчёта' + (n > 1 ? ' (' + T.plural(n, 'дата', 'даты', 'дат') + ')' : '')
      + (h.pending_why === 'calculation_busy' ? ': ждёт блокировки расчёта, файлы принимаются между датами пересчёта истории' : '');
  };
  T.history = function (h, now) {
    if (!h) return { unsupported: false, summary: '', applied: '', runs: '', next: '', problems: [] };
    var c = h.counts || {}, a = h.applied, dm = function (d) { return d.slice(8, 10) + '.' + d.slice(5, 7); };
    var out = { unsupported: h.available === false && h.code === 'history_unsupported', summary: '', applied: '', runs: '', next: '', problems: [] };
    out.hint = '';
    if (h.available === false) { out.summary = out.unsupported ? 'Адаптер не умеет сделки прошлых дат.' : ''; return out; }
    var sum = 'Сделки прошлых дат: получено ' + (c.fetched || 0) + ' из ' + (h.dates || 0) + ' дат';
    if (c.no_trades) sum += ' · без сделок ' + c.no_trades;
    if (c.unmapped) sum += ' · не сопоставлено ' + c.unmapped;
    if (c.unavailable) sum += ' · недоступно ' + c.unavailable;
    if (c.partial) sum += ' · получено частично ' + c.partial;
    if (c.todo) sum += ' · ждут запроса ' + c.todo;
    // TDM#715: после исправления адаптера монитор сам заново запрашивает уже полученные даты (ничего нажимать не нужно)
    var win = h.window && h.window.left ? h.window : null;
    if (win) sum += '. Исправление адаптера: сделки уже полученных дат запрашиваются заново, осталось ' + win.left + ' из ' + Math.max(win.total || 0, win.left);
    out.summary = sum;
    if (a) {
      var parts = [];
      if (a.full) parts.push('полностью в ' + T.n(a.full, ' дате', ' датах'));
      if (a.partial) parts.push('частично в ' + T.n(a.partial, ' дате', ' датах'));
      if (a.outside) parts.push('вне PnL в ' + T.n(a.outside, ' дате', ' датах'));
      if (a.unknown) parts.push('нет данных о полноте в ' + T.n(a.unknown, ' дате', ' датах'));
      var why = Object.keys(a.reasons || {}).map(function (k) { return (T.HIST_REASON[k] || k) + ': ' + a.reasons[k]; });
      var line = parts.length ? 'PnL сделок учтён ' + parts.join(', ') + (why.length ? ' (' + why.join('; ') + ')' : '') + '.' : '';
      var tail = [];
      if (a.waiting_recompute) tail.push('Ждут пересчёта: ' + a.waiting_recompute + (h.backfill_enabled === false ? ' (история прогонов выключена)' : '') + '.');
      if (a.no_effect) tail.push('Не влияют на PnL: ' + a.no_effect + '.');
      out.applied = [line].concat(tail).filter(Boolean).join(' ');
      if (h.enabled || c.fetched) {
        var rest = [];
        if (a.outside) rest.push('вне PnL ' + a.outside);
        if (a.unknown) rest.push('нет данных о полноте ' + a.unknown);
        out.runs = 'PnL сделок учтён в ' + (a.in_pnl || 0) + ' датах (полностью ' + (a.full || 0) + ', частично ' + (a.partial || 0) + ')'
          + (rest.length ? ', ' + rest.join(', ') : '') + ', ждут пересчёта ' + (a.waiting_recompute || 0)
          + (h.backfill_enabled === false && a.waiting_recompute ? ' (история прогонов выключена)' : '');
      }
    }
    if (h.current) out.next = 'Сейчас: ' + dm(h.current);
    else if (h.paused && h.waiting) out.next = T.HIST_WAIT[h.waiting] || h.waiting;
    else if (!h.enabled) out.next = win ? 'догрузка новых дат выключена; уже полученные даты запрашиваются заново' : 'выключено';
    else if (h.gate && T.HIST_GATE[h.gate]) out.next = T.HIST_GATE[h.gate] + (h.next_at ? ', дальше в ' + T.when(h.next_at, now) : '');
    // TDM#732: файлы, которые ждут блокировки расчёта, видны раньше «Дальше: сейчас» (пока шёл перезапрос, их не было видно)
    else if (h.pending) out.next = T.pending(h);
    else if (c.todo || win) out.next = 'Дальше: сейчас';
    else if ((h.problems || []).length) out.next = 'Ждут повтора: ' + h.problems.length;
    else out.next = 'Все даты получены';
    if (h.hint === 'all_unavailable') out.hint = 'Сервис не отдал ни одной прошлой даты. Скорее всего, дело в настройках подключения, а не в датах: проверьте адрес и вход в панели выше.';
    out.problems = (h.problems || []).map(function (p) {
      return dm(p.date) + ' — ' + ({ unavailable: 'недоступно', unmapped: 'не сопоставлено', partial: 'получено частично' }[p.state] || p.state)
        + (p.code ? ': ' + (T.HIST_CODE[p.code] || p.code) : '') + (p.attempts ? ', попыток: ' + p.attempts : '')
        + (p.next_at ? ', следующая попытка ' + T.when(p.next_at, now) : '');
    });
    return out;
  };

  T.stall = function (j, now) {
    var st = j && j.enabled !== false && j.todo ? j.stalled : null;
    if (!st) return null;
    var why = T.cause(st), err = st.error || '', t = now == null ? Date.now() : +new Date(now);
    var retry = st.next_attempt && +new Date(st.next_attempt) > t ? ', повтор в ' + T.when(st.next_attempt, now) : '';
    return { why: why, retry: retry, text: 'стоит: ' + why + retry,
      title: 'Стоит с ' + T.when(st.since, now) + ': ' + why + retry + '.' +
        (err ? ' Ошибка' + (st.date ? ' на ' + dmy(st.date) : '') + ': ' + err + (st.status && err.indexOf(String(st.status)) < 0 ? ' (код ' + st.status + ')' : '') : '') };
  };
  root.DESK_BACKFILL_TEXT = T;
})(typeof window !== 'undefined' ? window : this);
