/*
 * Откуда концепты берут данные (синхронно, до kit.js и кода страницы):
 *  - открыто через сервер (desk serve, http/https): прогон из базы — ../concepts-run.js (window.CONCEPTS_RUN) и
 *    ../concepts-data.js (window.DESK_EXTRAS: история, потоки, рынок), из них lib/real.js собирает window.MOCK;
 *    прогона нет (window.CONCEPTS_RUN_EMPTY: база пуста — no_runs, выбранного нет — not_found) — real.js показывает
 *    пустое состояние без цифр (#319, источник 'empty'), демо-набор не подставляется; страницы без прогона
 *    (<html data-run="optional">: C14, C15) работают как обычно, демо-набор у них — только форма MOCK для шапки;
 *    и lib/live.js (#28: лента событий по SSE или опросом, полоса «доступен новый прогон»);
 *  - ?demo=1 или открыто как файл (file://): демо-набор lib/mock.js, без живых обновлений.
 *  - сервер не отдал прогон (#309: 401 — сессия кончилась, 5xx, сеть): это не демо. Тег скрипта /concepts-run.js
 *    (/concepts-data.js) отмечает ошибку в window.CONCEPTS_LOAD_ERROR, lib/real.js показывает вместо монитора
 *    «данные не загрузились» (источник 'error') или ведёт на вход; демо-цифры не подставляются.
 * Выбранный прогон — как у остальных страниц (desk-nav.js): ?run_id= / ?as_of= / ?demo=1 в адресе, иначе выбор,
 * запомненный в localStorage (desk.run.v1, 12 часов).
 */
(function () {
  // слова догрузки истории (чип шапки kit.js) — общие с Настройками
  document.write('<script src="../desk-backfill-text.js"><\/script>');
  var http = /^https?:$/.test(location.protocol), u = new URL(location.href), sel = {};
  ['run_id', 'as_of', 'demo'].forEach(function (k) { var v = u.searchParams.get(k); if (v) sel[k] = v; });
  if (http && !Object.keys(sel).length) {
    try {
      var s = JSON.parse(localStorage.getItem('desk.run.v1') || 'null');
      if (s && s.sel && Date.now() - s.at < 12 * 3600e3) sel = s.sel;
    } catch (e) { }
  }
  window.CONCEPTS_SEL = sel;
  if (!http || sel.demo) {
    window.CONCEPTS_SOURCE = http ? 'demo' : 'file';
    document.write('<script src="lib/mock.js"><\/script>');
    return;
  }
  var q = ['run_id', 'as_of'].filter(function (k) { return sel[k]; }).map(function (k) { return k + '=' + encodeURIComponent(sel[k]); }).join('&');
  q = q ? '?' + q : '';
  // ошибка загрузки (#309): тег скрипта не сообщает код ответа, только сам факт; что это было (вход или сбой),
  // lib/real.js выясняет по /api/v1/auth/state
  window.CONCEPTS_LOAD_ERROR = null;
  window.conceptsLoadFailed = function (what) { (window.CONCEPTS_LOAD_ERROR = window.CONCEPTS_LOAD_ERROR || {})[what] = true; };
  var tag = function (src, what) { return '<script src="' + src + '" onerror="conceptsLoadFailed(\'' + what + '\')"><\/script>'; };
  // lib/live.js — живые обновления (#28): лента событий сервера, «доступен новый прогон»
  document.write(tag('../concepts-run.js' + q, 'run') + tag('../concepts-data.js' + q, 'data') + '<script src="lib/real.js"><\/script><script src="lib/live.js"><\/script>');
})();
