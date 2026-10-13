/*
 * Загрузка данных деска (синхронно, до кода страницы):
 *  - открыто через сервер (http/https) — /desk-data.js из базы, параметры ?as_of=&run_id= передаются как есть;
 *    если сервер не отдал данные (нет прогонов, ?demo=1 или статический хостинг без API) — демо data.js;
 *  - открыто как файл (file://) — сразу демо data.js.
 * Затем desk-util.js (DESK.util) и desk-runs.js (выбор даты/прогона).
 */
(function () {
  var http = /^https?:$/.test(location.protocol);
  var q = location.search || '';
  var html = http ? '<script src="desk-data.js' + q + '"><\/script><script src="desk-fallback.js"><\/script>'
                  : '<script src="data.js"><\/script>';
  document.write(html + '<script src="desk-util.js"><\/script><script src="desk-runs.js"><\/script>');
})();
