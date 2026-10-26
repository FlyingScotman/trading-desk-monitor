/*
 * Загрузка данных деска (синхронно, до кода страницы):
 *  - открыто через сервер (http/https) — /desk-data.js из базы, параметры ?as_of=&run_id= передаются как есть
 *    (выбранный прогон, общий для всех страниц, desk-nav.js кладёт в адрес раньше загрузчика);
 *    нет прогонов — экран «Прогонов ещё нет» (desk-empty.js), не демо; ?demo=1 или статический хостинг без API — демо data.js;
 *  - открыто как файл (file://) — сразу демо data.js.
 * Затем desk-util.js (DESK.util) и desk-scope.js (DESK.scope: сохранённый фильтр позиций). Грузит его Scenario Lab.
 * Выбор даты/прогона, автопилот и плашка демо-данных — в строке навигации desk-nav.js.
 */
(function () {
  var http = /^https?:$/.test(location.protocol);
  var q = location.search || '';
  var html = http ? '<script src="desk-data.js' + q + '"><\/script><script src="desk-fallback.js"><\/script>'
                  : '<script src="data.js"><\/script>';
  document.write(html + '<script src="desk-util.js"><\/script><script src="desk-scope.js"><\/script>');
})();
