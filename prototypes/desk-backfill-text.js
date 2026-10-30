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
 *                           прошло или автопилот выключен), в подсказке — с какого времени, дата и ошибка целиком.
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
