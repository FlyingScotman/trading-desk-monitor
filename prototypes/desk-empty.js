/*
 * Экран «Прогонов ещё нет» (desk-fallback.js подключает его, когда сервер ответил window.DESK_EMPTY).
 * Вместо демо-данных — что делает автопилот сейчас (GET api/v1/autopilot/status: раз в 5 с во время цикла,
 * иначе раз в 30 с) и ссылка на Настройки. Появился прогон — страница перезагружается сама.
 * Код страницы под экраном работает на демо-наборе, но скрыт целиком; явное демо — ?demo=1.
 */
(function () {
  'use strict';
  var E = window.DESK_EMPTY || {}, noRuns = E.reason !== 'not_found';
  document.documentElement.classList.add('desk-empty');
  var css = 'html.desk-empty body>*:not(#desk-empty){display:none!important}' +
    '#desk-empty{--e-bg:#f6f7f9;--e-card:#fff;--e-fg:#1b2129;--e-fg2:#5a6573;--e-line:#d9dee5;--e-acc:#2f6fd6;' +
    'min-height:100vh;box-sizing:border-box;display:flex;align-items:center;justify-content:center;padding:24px 16px;' +
    'background:var(--e-bg);color:var(--e-fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}' +
    '@media (prefers-color-scheme:dark){html:not([data-theme="light"]) #desk-empty{--e-bg:#0f1318;--e-card:#171c23;' +
    '--e-fg:#e8ecf1;--e-fg2:#9aa6b4;--e-line:#2a323d;--e-acc:#6ea0ff}}' +
    'html[data-theme="dark"] #desk-empty{--e-bg:#0f1318;--e-card:#171c23;--e-fg:#e8ecf1;--e-fg2:#9aa6b4;' +
    '--e-line:#2a323d;--e-acc:#6ea0ff}' +
    '#desk-empty .card{max-width:560px;width:100%;background:var(--e-card);border:1px solid var(--e-line);' +
    'border-radius:8px;padding:24px}' +
    '#desk-empty h1{font-size:20px;margin:0 0 12px}#desk-empty p{margin:0 0 10px}' +
    '#desk-empty .ap{color:var(--e-fg2)}#desk-empty .ap b{color:var(--e-fg);font-weight:600}' +
    '#desk-empty .links{display:flex;gap:16px;flex-wrap:wrap;margin-top:16px}' +
    '#desk-empty a{color:var(--e-acc)}#desk-empty .minor{font-size:13px;color:var(--e-fg2)}';
  var st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function without(keys, add) {
    var u = new URL(location.href);
    keys.forEach(function (k) { u.searchParams.delete(k); });
    for (var k in add || {}) u.searchParams.set(k, add[k]);
    return u.toString();
  }

  var apLine = el('b', {}, 'проверяем…');
  function mount() {
    var box = el('div', { id: 'desk-empty', role: 'main' }), card = el('div', { 'class': 'card' });
    card.appendChild(el('h1', {}, noRuns ? 'Прогонов ещё нет' : 'Прогон не найден'));
    if (!noRuns) card.appendChild(el('p', {}, (E.message || '') + '.'));
    var ap = el('p', { 'class': 'ap', role: 'status', 'aria-live': 'polite' }, 'Автопилот: ');
    ap.appendChild(apLine);
    card.appendChild(ap);
    card.appendChild(el('p', {}, noRuns ? 'Как только посчитается первый день — данные появятся здесь.'
                                        : 'Откройте последний прогон или выберите другую дату.'));
    var links = el('div', { 'class': 'links' });
    links.appendChild(el('a', { href: 'settings.html' }, 'Настройки'));
    if (!noRuns) links.appendChild(el('a', { href: without(['as_of', 'run_id', 'demo']) }, 'Последний прогон'));
    links.appendChild(el('a', { href: without(['as_of', 'run_id'], { demo: '1' }), 'class': 'minor' }, 'показать демо-данные'));
    card.appendChild(links);
    box.appendChild(card);
    document.body.appendChild(box);
    poll();
  }

  function render(j) {
    if (!j || typeof j !== 'object') { apLine.textContent = 'нет ответа (сервер без автопилота?)'; return; }
    var t = !j.enabled ? 'выключен' + (j.message ? ' (' + j.message + ')' : '')
      : j.state === 'running' ? (j.progress && j.message ? j.message : 'идёт цикл')
      : (j.state === 'error' ? 'ошибка: ' : '') + (j.message || 'ожидание');
    apLine.textContent = t;
  }
  function once(runId) {  // перезагрузка один раз на прогон: не зациклиться, если сервер его всё же не отдал
    try {
      if (sessionStorage.getItem('desk-empty.reload') === runId) return false;
      sessionStorage.setItem('desk-empty.reload', runId);
    } catch (e) { return false; }
    return true;
  }
  function poll() {
    fetch('api/v1/autopilot/status', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (noRuns && j && j.latest_run && once(String(j.latest_run.run_id))) { location.reload(); return; }  // первый прогон готов
        render(j);
        setTimeout(poll, j && j.state === 'running' ? 5000 : 30000);
      })
      .catch(function () { render(null); setTimeout(poll, 30000); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
