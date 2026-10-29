/*
 * Почему набора нет (TDM#445): страница, у прогона которой нет отчёта набора (C16 «отчёта ОВП нет»), одной фразой
 * говорит причину по журналу входящих — GET ../api/v1/inbox/datasets/{набор}?on=<дата прогона>
 * (desk/daystatus.dataset_files). Причины по порядку: отчёт уже в базе (прогон посчитан до него, TDM#181); адаптер не
 * загружен или не знает набора; последний файл отклонён или не прочитан (причина словами, коды — в подсказке);
 * принят частично; отчёт есть только позже даты прогона; файлы старше окна загрузки; папка ещё не просматривалась
 * (только что добавлена; автообновление выключено); сервер не видит папку; файлы пропущены (старые по дате
 * изменения, слишком большие); файла нет — добавить папку в «Папки с отчётами» (папки монитора, не параметры
 * адаптера). Второй строкой мелко: имя файла · сколько файлов в папках не распознано · ссылки на C14 и (админу) на
 * настройки; сказать второй строке нечего — ссылки в конце первой.
 * Чистая функция над ответом: INBOX_WHY.view(x, opt) — {cls, html, title, sub}, INBOX_WHY.html(x, opt) — разметка
 * (tests/js/inbox_why.test.js).
 */
(function (root) {
  'use strict';
  const W = {};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const dm = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] : esc(s || ''); };
  const plural = (n, one, few, many) => { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
  W.LOST = { rejected: 'отклонён', failed: 'не прочитан', timed_out: 'не прочитан: не уложился в таймаут', match_failed: 'не распознан адаптером' };
  W.SETTINGS = '../settings.html#adapter';

  // причина одной фразой и вторая строка
  W.view = function (x, opt) {
    opt = opt || {};
    const name = esc(x.name || x.dataset), rep = 'Отчёт ' + name, on = opt.on || x.on;
    const F = x.files || {}, U = x.unread || {}, L = x.latest, fo = x.folders || {};
    const at = d => d && d !== on ? ' за ' + dm(d) : '';  // дату прогона не повторяем
    const link = !!x.settings_link;
    const where = link ? '<a href="' + esc(opt.settings || W.SETTINGS) + '">Настройки → Папки с отчётами</a>' : 'Настройки → Папки с отчётами';
    let cls = 'warn', html, title = '', said = false;
    if (x.in_force) html = rep + ' уже в базе' + at(x.in_force.valid_date) + ' — прогон посчитан раньше, пересчитайте его';
    else if (String(x.adapter || '').startsWith('error')) { cls = 'bad'; html = 'Адаптер не загружен — отчёты не читаются'; title = x.adapter; }
    else if (!(x.sources || []).length) { cls = 'bad'; html = 'Адаптер не знает отчёта ' + name; }
    else if (L && !L.after) {
      const day = at(L.valid_date || L.report_date);
      title = (L.issues || []).map(i => i.code + ' ×' + i.count).join(', ') || L.reason || '';
      const why = L.reason ? ': ' + esc(L.reason) : '';
      if (W.LOST[L.status]) { cls = 'bad'; html = rep + day + ' ' + W.LOST[L.status] + (L.status === 'rejected' ? why : ''); }
      else if (L.status === 'partially_accepted' || (L.units || {}).rejected) { cls = 'bad'; html = rep + day + ' принят частично' + why; }
      else if (L.status === 'waiting_calendar') html = rep + day + ' ждёт календарь с хаба';
      else html = rep + day + ': ' + esc(L.status_text || L.status);
    } else if (L) html = rep + ' есть только позже даты прогона — за ' + dm(L.valid_date || L.report_date);
    else if (U.too_old) html = 'Отчёты ' + name + ' в папках старше окна загрузки' + (x.lookback_days ? ' (' + x.lookback_days + ' дн.)' : '') + ' — не читались';
    else if (!x.last_scan || (fo.scanned || 0) < (fo.count || 0)) {  // папку только что добавили: скан до неё ещё не дошёл
      html = (!x.last_scan || fo.count - (fo.scanned || 0) > 1 ? 'Папки ещё не просматривались' : 'Папка ещё не просматривалась') +
        (x.autopilot === false ? ' — автообновление выключено' : '');
    } else if (fo.missing) { cls = 'bad'; html = 'Сервер не видит папку с отчётами' + (fo.count > 1 ? ' (' + fo.missing + ' из ' + fo.count + ')' : ''); }
    else if (SK.some(k => ((x.last_scan || {}).skipped || {})[k[0]])) {
      html = 'Файлы в папках пропущены: ' + SK.filter(k => x.last_scan.skipped[k[0]]).map(k => k[1]).join(' и ');
    } else { html = 'Файла отчёта ' + name + ' в папках нет — добавьте папку в ' + where; said = true; }
    const sub = [], links = ['<a href="' + esc(opt.c14 || 'c14-day-status.html?on=' + encodeURIComponent(on || '')) + '">C14</a>'];
    if (L) sub.push('<span class="mono">' + esc(L.name) + '</span>');
    if (U.ignored) sub.push((L ? 'ещё ' : 'в папках ') + U.ignored + ' ' + plural(U.ignored, 'файл не распознан', 'файла не распознано', 'файлов не распознано'));
    if (link && !said) links.push('<a href="' + esc(opt.settings || W.SETTINGS) + '">Настройки</a>');
    if (sub.length) sub.push(...links); else html += ' · ' + links.join(' · ');  // одна строка, когда второй нечего сказать
    return { cls, html, title, sub };
  };
  const SK = [['too_old', 'старые по дате изменения'], ['too_large', 'слишком большие']];

  W.html = function (x, opt) {
    const v = W.view(x, opt);
    return '<div data-why="' + esc(x.dataset) + '"><div style="color:var(--' + (v.cls === 'bad' ? 'neg' : 'warn') + ')"' + (v.title ? ' title="' + esc(v.title) + '"' : '') + '>' + v.html + '</div>' +
      (v.sub.length ? '<div class="mut3" style="font-size:12px;margin-top:2px">' + v.sub.join(' · ') + '</div>' : '') + '</div>';
  };

  root.INBOX_WHY = W;
})(typeof window !== 'undefined' ? window : this);
