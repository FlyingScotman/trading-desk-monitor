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
    gap: 'пауза между запросами', backoff: 'повтор после ошибки' };
  T.HIST_REASON = { no_prev_close: 'нет предыдущего закрытия', no_pnl: 'нет PnL позиции на начало дня', lost: 'бумага не оценена',
    unpriced: 'сделки без цены или курса', no_pnl_run: 'PnL в прогоне не считался', unvalued: 'бумага не оценена совсем' };
  T.HIST_CODE = { date_unavailable: 'сервис не отдаёт эту дату', books_unmapped: 'книги сервиса не сопоставлены с книгами монитора',
    export_incomplete: 'выгрузка неполная', files_not_accepted: 'файлы не приняты', empty: 'пусто, причина не указана' };
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
    else if (!h.enabled) out.next = 'выключено';
    else if (h.gate && T.HIST_GATE[h.gate]) out.next = T.HIST_GATE[h.gate] + (h.next_at ? ', дальше в ' + T.when(h.next_at, now) : '');
    else if (c.todo) out.next = 'Дальше: сейчас';
    else if (h.pending) out.next = 'Файлы ждут окончания расчёта';
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
