/*
 * Почему набора нет (TDM#445): страница, у прогона которой нет отчёта набора (C16 «отчёта ОВП нет»), объясняет
 * причину по журналу входящих — GET ../api/v1/inbox/datasets/{набор}?on=<дата прогона> (desk/daystatus.dataset_files).
 *  - отчёт уже в базе, но прогон посчитан до него (TDM#181) — «пересчитайте прогон»;
 *  - адаптер не загружен или не объявляет источник набора;
 *  - файлов источников набора нет: сколько файлов адаптер не узнал (у такого файла источник неизвестен — честно
 *    «пропущено M»), подсказка про Настройки → «Папки с отчётами» (папки монитора, не параметры адаптера);
 *  - файлы есть: сколько по статусам, последний — имя, дата, статус, причина и коды диагностики адаптера (без значений
 *    ячеек), файлы старше окна загрузки;
 *  - когда сервер последний раз просматривал папки; ссылки на C14 «Состояние дня» и на настройки.
 * Чистая функция над ответом: INBOX_WHY.lines(x, opt) — строки, INBOX_WHY.html(x, opt) — разметка
 * (tests/js/inbox_why.test.js). Страница-хозяйка даёт класс списка (opt.listClass) и ссылки (opt.c14, opt.settings).
 */
(function (root) {
  'use strict';
  const W = {};
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const date = s => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s || ''); return m ? m[3] + '.' + m[2] + '.' + m[1] : esc(s || '—'); };
  const when = s => { if (!s) return '—'; const d = new Date(s); return isNaN(d) ? esc(s) : d.toLocaleString('ru-RU', { dateStyle: 'short', timeStyle: 'short' }); };
  const plural = (n, one, few, many) => { const a = Math.abs(n) % 100, b = a % 10; return a > 10 && a < 20 ? many : b === 1 ? one : b >= 2 && b <= 4 ? few : many; };
  const files = n => n + ' ' + plural(n, 'файл', 'файла', 'файлов');
  W.ORDER = ['accepted', 'partially_accepted', 'superseded', 'rejected', 'failed', 'timed_out', 'match_failed', 'waiting_calendar'];
  W.BAD = new Set(['rejected', 'failed', 'timed_out', 'match_failed']);

  // строки объяснения: [{cls: ''|'warn'|'bad'|'ok', html}], последней — подсказка, где смотреть
  W.lines = function (x, opt) {
    opt = opt || {};
    const out = [], add = (cls, html) => out.push({ cls, html });
    const name = x.name || x.dataset, rep = 'отчёт ' + esc(name), on = opt.on || x.on;
    const F = x.files || {}, U = x.unread || {}, total = F.total || 0, tooOld = U.too_old || 0, ignored = U.ignored || 0;
    if (x.in_force) {
      add('warn', 'В базе уже есть ' + rep + ' на ' + date(on) + ' (от ' + date(x.in_force.valid_date) + ', записан ' + when(x.in_force.recorded_at) +
        '): прогон посчитан до него — пересчитайте прогон.');
    }
    if (String(x.adapter || '').startsWith('error')) add('bad', 'Адаптер не загружен (' + esc(x.adapter.replace(/^error\s*/, '')) + '): файлы из папок не читаются.');
    else if (!(x.sources || []).length) add('bad', 'Адаптер не объявляет источник набора «' + esc(name) + '»: ни один файл не станет отчётом ' + esc(name) + '.');
    if (!total && !tooOld) {
      add('warn', ignored ? 'Файлов, которые адаптер признал отчётом ' + esc(name) + ', нет; пропущено ' + files(ignored) + ' (адаптер их не узнал).'
        : 'В папках с отчётами нет ни одного файла, который адаптер признал отчётом ' + esc(name) + '.');
      const fo = x.folders || {};
      add('', 'Проверьте, что папка с отчётом добавлена в Настройки → «Папки с отчётами» (папки монитора), а не только в параметры адаптера. ' +
        (fo.count ? 'Папок с отчётами: ' + fo.count + (fo.missing ? ', из них сервер не видит: ' + fo.missing : '') + '.' : 'Папки с отчётами не заданы.'));
    } else {
      const st = F.by_status || {}, txt = F.status_text || {};
      const keys = [...W.ORDER.filter(k => st[k]), ...Object.keys(st).filter(k => !W.ORDER.includes(k))];
      const win = 'старше окна загрузки' + (x.lookback_days ? ' (' + x.lookback_days + ' дн.)' : '');
      if (total) {
        add(keys.some(k => W.BAD.has(k)) ? 'warn' : '', 'Файлов отчёта ' + esc(name) + ' во входящих: ' + total +
          (keys.length ? ' — ' + keys.map(k => esc(txt[k] || k) + ' ' + st[k]).join(', ') : '') + (tooOld ? '; ещё ' + files(tooOld) + ' ' + win + ' — не читались' : '') +
          (ignored ? '; в папках ещё ' + files(ignored) + ', которые адаптер не узнал' : '') + '.');
      } else add('warn', 'Файлы отчёта ' + esc(name) + ' есть, но все ' + win + ': ' + files(tooOld) + ' — не читались.');
      const L = x.latest;
      if (L) {
        const iss = (L.issues || []).map(i => '<span class="mono">' + esc(i.code) + '</span> ×' + i.count + (i.text ? ' (' + esc(i.text) + ')' : ''));
        add(W.BAD.has(L.status) ? 'bad' : L.after ? 'warn' : '', 'Последний: <b>' + esc(L.name) + '</b> · отчёт за ' + date(L.report_date) +
          (L.valid_date && L.valid_date !== L.report_date ? ' (рабочий день ' + date(L.valid_date) + ')' : '') + ' · ' + esc(L.status_text || L.status) +
          (L.why ? ': ' + esc(L.why) : '') + (L.after ? ' — позже даты прогона ' + date(on) : '') + (iss.length ? ' · коды адаптера: ' + iss.join(', ') : '') + '.');
      }
      if (!total && tooOld) add('', 'Окно загрузки — Настройки → «Автообновление» → «Отчёты не старше, дней»; старые отчёты догружает «История прогонов».');
    }
    const ls = x.last_scan;
    add('', (ls ? 'Последний просмотр папок: ' + when(ls.at) : 'После запуска сервера папки ещё не просматривались (автообновление выключено?)') +
      ' · <a href="' + esc(opt.c14 || 'c14-day-status.html?on=' + encodeURIComponent(on || '')) + '">C14 «Состояние дня»</a>' +
      ' · <a href="' + esc(opt.settings || '../settings.html#adapter') + '">Настройки → Папки с отчётами</a>');
    return out;
  };

  W.html = function (x, opt) {
    return '<ul class="' + esc((opt && opt.listClass) || 'k-why') + '" data-why="' + esc(x.dataset) + '">' +
      W.lines(x, opt).map(l => '<li' + (l.cls ? ' class="' + l.cls + '"' : '') + '>' + l.html + '</li>').join('') + '</ul>';
  };

  root.INBOX_WHY = W;
})(typeof window !== 'undefined' ? window : this);
