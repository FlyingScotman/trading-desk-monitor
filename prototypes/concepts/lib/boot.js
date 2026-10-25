/*
 * Откуда концепты берут данные (синхронно, до kit.js и кода страницы):
 *  - открыто через сервер (desk serve, http/https): прогон из базы — ../desk-data.js (window.DESK) и
 *    ../concepts-data.js (window.DESK_EXTRAS: история, потоки, рынок), из них lib/real.js собирает window.MOCK;
 *    прогона нет (база пуста) — real.js подставляет демо-набор и говорит об этом в шапке;
 *    и lib/live.js (#28: лента событий по SSE или опросом, полоса «доступен новый прогон»);
 *  - ?demo=1 или открыто как файл (file://): демо-набор lib/mock.js, без живых обновлений.
 * Выбранный прогон — как у остальных страниц (desk-nav.js): ?run_id= / ?as_of= / ?demo=1 в адресе, иначе выбор,
 * запомненный в localStorage (desk.run.v1, 12 часов).
 */
(function () {
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
  // lib/live.js — живые обновления (#28): лента событий сервера, «доступен новый прогон»
  document.write('<script src="../desk-data.js' + q + '"><\/script><script src="../concepts-data.js' + q + '"><\/script><script src="lib/real.js"><\/script><script src="lib/live.js"><\/script>');
})();
