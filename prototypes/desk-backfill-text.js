/*
 * Догрузка истории одними словами (#447, #449): чип в шапке мониторов (concepts/lib/kit.js, K.backfillStatus) и
 * Настройки (сводка сверху, «История прогонов» в Автообновлении и Диагностике) по GET /api/v1/backfill/status.
 *  T.CAUSE, T.cause(code) — почему догрузка стоит (backend: backfill.stall_cause);
 *  T.when(iso)            — момент по МСК: «ЧЧ:ММ» сегодня, иначе «ДД.ММ ЧЧ:ММ»;
 *  T.dates(j)             — {conv, load}: даты, которые «не сходятся» (своя проблема расчёта) и «не загружаются»
 *                           (одна дата раз за разом упирается в сеть или базу — kind transient; даты хаба, и при
 *                           выключенной догрузке: их грузит шаг sync), без повторов;
 *  T.n(n, one, many)      — «1 не сходится», «2 не сходятся»: число со своим словом;
 *  T.stall(j)             — {text, title} застоя или null: «стоит: <причина>, повтор в ЧЧ:ММ», в подсказке — с какого
 *                           времени, следующая попытка и ошибка целиком.
 * Подключается раньше kit.js (concepts/lib/boot.js) и в settings.html; window.DESK_BACKFILL_TEXT.
 */
(function (root) {
  'use strict';
  var T = {}, MSK_MS = 3 * 3600e3;  // московское время — UTC+3 круглый год
  T.CAUSE = { network: 'нет связи', proxy: 'прокси не пропускает', access: 'нет доступа к хабу (токен)', setup: 'хаб не настроен',
    hub: 'хаб не отвечает', store: 'база недоступна', store_slow: 'база не успевает', disk: 'ошибка диска' };
  T.cause = function (code) { return T.CAUSE[code] || 'ошибка сети или базы'; };
  function msk(t) { return new Date(+new Date(t) + MSK_MS).toISOString(); }
  T.when = function (iso, now) {
    if (!iso) return '—';
    var t = msk(iso), today = msk(now == null ? Date.now() : now);
    return (t.slice(0, 10) === today.slice(0, 10) ? '' : t.slice(8, 10) + '.' + t.slice(5, 7) + ' ') + t.slice(11, 16);
  };
  T.n = function (n, one, many) { return n + (n % 10 === 1 && n % 100 !== 11 ? one : many); };
  T.dates = function (j) {
    var on = j && j.enabled !== false, seen = {}, conv = [], load = [];
    (on ? j.problem_dates || [] : []).forEach(function (p) { seen[p.date] = 1; (p.kind === 'transient' ? load : conv).push(p); });
    ((j && j.hub_problem_dates) || []).forEach(function (p) { if (!seen[p.date]) { seen[p.date] = 1; load.push(p); } });
    return { conv: conv, load: load };
  };
  T.stall = function (j, now) {
    var st = j && j.enabled !== false ? j.stall : null;
    if (!st) return null;
    var why = T.cause(st.cause), err = st.error || '';
    return { why: why, text: 'стоит: ' + why + (st.next_attempt ? ', повтор в ' + T.when(st.next_attempt, now) : ''),
      title: 'Стоит с ' + T.when(st.since, now) + ': ' + why + '.' + (st.next_attempt ? ' Следующая попытка ' + T.when(st.next_attempt, now) + '.' : '') +
        (err ? ' Ошибка: ' + err + (st.status && err.indexOf(String(st.status)) < 0 ? ' (код ' + st.status + ')' : '') : '') };
  };
  root.DESK_BACKFILL_TEXT = T;
})(typeof window !== 'undefined' ? window : this);
