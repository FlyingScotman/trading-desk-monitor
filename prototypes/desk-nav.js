/*
 * Общая строка навигации всех страниц (прототипы и React-приложение /app/): ссылки на основные страницы, выбранный прогон,
 * автопилот, «Тестовые данные». Подключается до desk-loader.js: выбор прогона должен попасть в адрес раньше,
 * чем загрузчик запросит desk-data.js.
 *
 * Выбранный прогон общий для всех страниц: ?run_id= / ?as_of= / ?demo=1 в адресе, его же помнит localStorage
 * (desk.run.v1, 12 часов: наутро страницы снова открываются на последнем прогоне). Адрес без параметров берёт
 * запомненный выбор; ссылки строки несут выбор в адресе (работает и без localStorage). «Последний прогон» — пустой
 * выбор. Открыто как файл (file://) — только ссылки и демо-плашка: сервера нет.
 *
 * Новый прогон после цикла автопилота (GET api/v1/autopilot/status: раз в 30 с, во время цикла раз в 5 с):
 * страница на последнем прогоне перезагружается сама, если вкладка скрыта, иначе — кнопка «Новый прогон — показать»;
 * страница без данных деска (Настройки, главная) только обновляет список прогонов и шлёт событие desk-nav:new-run.
 * Прогон назван своим срезом рынка (R11): «20.10 14:30 МСК · внутри дня» (портфель дня на внутридневном срезе
 * хаба), «19.10 · закрытие предв.», «19.10 · закрытие»; «последний прогон» — самый новый показываемый (днём —
 * внутридневной). Если PnL посчитан не к закрытию предыдущего рабочего дня, это сказано в подсказке.
 * Фильтр, который Scenario Lab применил к своим данным (DESK.scope, desk-scope.js; сохранён ещё удалённым Desk Monitor),
 * виден здесь же: что выбрано, сколько позиций и ✕ (снять на всех страницах).
 * Вход (desk-auth.js, подключается отсюда сразу): CSRF-заголовок изменяющих запросов, пользователь и «Выйти» в строке.
 * window.DESK_NAV: { sel, pinned, href(page), select(sel) }.
 */
(function () {
  'use strict';
  var KEY = 'desk.run.v1', TTL = 12 * 3600e3, PARAMS = ['run_id', 'as_of', 'demo'];
  var http = /^https?:$/.test(location.protocol);
  var inApp = /\/app\/[^\/]*$/.test(location.pathname);
  var root = inApp ? '../' : '';
  var here = inApp ? 'app/' : location.pathname.split('/').pop() || 'index.html';
  if (http && !window.DESK_AUTH) {  // до первых запросов страницы: CSRF-заголовок, пользователь в строке (desk-auth.js)
    var au = document.createElement('script');
    au.src = root + 'desk-auth.js';
    (document.head || document.documentElement).appendChild(au);
  }
  // Основной набор — мониторы (concepts/). Устаревший scenario-lab.html в строке не показан, открывается только по
  // прямому адресу (README.md, «Устаревшие экраны»).
  var PAGES = [
    ['concepts/index.html', 'Мониторы', '13 мониторов на реальных данных: кокпит, риски, позиции, PnL, календари, рынок, история'],
    ['app/', 'Сценарии (точно)', 'Тейлор и точная переоценка на сервере'],
    ['settings.html', 'Настройки', 'Хаб, папки отчётов, адаптер, автопилот']
  ];

  /* ---------- выбор прогона ---------- */
  function fromUrl() {
    var u = new URL(location.href), s = {};
    PARAMS.forEach(function (k) { var v = u.searchParams.get(k); if (v) s[k] = v; });
    return s;
  }
  function stored() {
    try {
      var s = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (s && s.sel && Date.now() - s.at < TTL) return s.sel;
    } catch (e) {}
    return {};
  }
  function remember(sel) {
    try {
      if (Object.keys(sel).length) localStorage.setItem(KEY, JSON.stringify({ sel: sel, at: Date.now() }));
      else localStorage.removeItem(KEY);
    } catch (e) {}
  }
  function withSel(href, sel) {
    var u = new URL(href, location.href);
    PARAMS.forEach(function (k) { u.searchParams.delete(k); });
    PARAMS.forEach(function (k) { if (sel[k]) u.searchParams.set(k, sel[k]); });
    return u.toString();
  }

  var sel = http ? fromUrl() : {};
  if (http) {
    if (Object.keys(sel).length) remember(sel);
    else if (Object.keys(sel = stored()).length) history.replaceState(history.state, '', withSel(location.href, sel));
  }
  var pinned = Object.keys(sel).length > 0;

  function href(page) { return http ? withSel(root + page, sel) : root + page; }
  function select(next) { remember(next); location.href = withSel(location.href, next); }
  window.DESK_NAV = { sel: sel, pinned: pinned, href: href, select: select };

  /* ---------- строка ---------- */
  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  var dt = function (d) { return d ? String(d).slice(0, 10).split('-').reverse().join('.') : ''; };
  var CUTS = { intraday: 'внутри дня', eod_preliminary: 'закрытие предв.', eod_final: 'закрытие', prev_close: 'закрытие пред. дня' };
  var msk = function (ts) { var t = new Date(ts).getTime(); return isNaN(t) ? '' : new Date(t + 3 * 3600e3).toISOString().slice(11, 16) + ' МСК'; };
  // срез прогона: дата, для внутридневного — время среза по Москве, и вид среза (сервер присылает его же в label)
  function sliceLabel(day, cut, cutTs, type) {
    return dt(day) + (cut === 'intraday' && cutTs ? ' ' + msk(cutTs) : '') + ' · ' + (CUTS[cut] || cut || type || '') +
      (type === 'TEST' || type === 'REPLAY' ? ' · ' + type : '');
  }
  function runLabel(r) { return r.label || sliceLabel(r.business_date || r.as_of, r.cut_type, r.cut_ts, r.run_type); }
  function isCurrent(r) { return r.is_current != null ? r.is_current : r.is_default; }
  var hm = function (s) { var d = new Date(s); return isNaN(d) ? '' : d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }); };

  // Цвета — переменные страницы (у страниц разные имена: --panel/--surface, --fg/--text, --fg-3/--muted),
  // поэтому строка следует светлой/тёмной теме страницы; свои значения — только запасные.
  var css = '#desk-nav{--n-bg:var(--panel,var(--surface,#fff));--n-fg:var(--fg,var(--text,#1b2129));' +
    '--n-fg2:var(--fg-2,var(--text-2,#4d5866));--n-fg3:var(--fg-3,var(--muted,#6b7685));--n-line:var(--line,#d9dee5);' +
    '--n-acc:var(--accent,#2f6fd6);--n-warn:var(--warn,#fab219);--n-crit:var(--crit,var(--critical,#d03b3b));' +
    '--n-good:var(--good,var(--ok,#1a7f37));position:relative;z-index:35;display:flex;flex-wrap:wrap;align-items:center;' +
    'justify-content:space-between;gap:2px 16px;padding:2px 16px;min-height:30px;box-sizing:border-box;' +
    'background:var(--n-bg);border-bottom:1px solid var(--n-line);color:var(--n-fg2);' +
    'font:12px/1.3 var(--font-ui,var(--font-sans,system-ui,-apple-system,"Segoe UI",sans-serif))}' +
    '#desk-nav *{box-sizing:border-box}' +
    '#desk-nav .dn-links{display:flex;align-items:center;gap:2px;overflow-x:auto;scrollbar-width:none;min-width:0;max-width:100%}' +
    '#desk-nav .dn-links::-webkit-scrollbar{display:none}' +
    '#desk-nav a{color:var(--n-fg2);text-decoration:none;white-space:nowrap;padding:5px 8px 4px;border-bottom:2px solid transparent}' +
    '#desk-nav a:hover{color:var(--n-fg)}' +
    '#desk-nav a[aria-current="page"]{color:var(--n-fg);border-bottom-color:var(--n-acc);font-weight:600}' +
    '#desk-nav a.dn-home{font-weight:700;color:var(--n-fg);padding-left:0;letter-spacing:.02em}' +
    '#desk-nav a.dn-home[aria-current="page"]{border-bottom-color:transparent}' +
    '#desk-nav .dn-st{display:flex;align-items:center;gap:4px 12px;flex-wrap:wrap;min-width:0}' +
    '#desk-nav .dn-run{display:inline-flex;align-items:center;gap:6px;white-space:nowrap}' +
    '#desk-nav .dn-run b{color:var(--n-fg);font-weight:600}' +
    '#desk-nav select{font:inherit;color:var(--n-fg);background:transparent;border:1px solid var(--n-line);border-radius:2px;' +
    'padding:1px 4px;max-width:270px}' +
    '#desk-nav select option{background:var(--n-bg);color:var(--n-fg)}' +
    '#desk-nav button{font:inherit;color:var(--n-fg2);background:transparent;border:1px solid var(--n-line);border-radius:2px;' +
    'padding:1px 8px;cursor:pointer;white-space:nowrap}' +
    '#desk-nav button:hover{color:var(--n-fg);border-color:var(--n-acc)}#desk-nav button:disabled{opacity:.5;cursor:default}' +
    '#desk-nav button.dn-new{border-color:var(--n-warn);color:var(--n-fg)}' +
    '#desk-nav .dn-demo{border:1px dashed var(--n-warn);color:var(--n-fg);padding:0 6px;font-size:11px;letter-spacing:.04em;' +
    'text-transform:uppercase;border-radius:2px;white-space:nowrap}' +
    '#desk-nav .dn-scope{display:inline-flex;align-items:center;gap:6px;min-width:0;padding:0 0 0 8px;border-left:2px solid var(--n-acc)}' +
    '#desk-nav .dn-scope b{color:var(--n-fg);font-weight:600;max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '#desk-nav .dn-scope button{padding:0 5px;line-height:1.2}' +
    '#desk-nav .dn-ap{display:inline-flex;align-items:center;gap:6px;flex-wrap:wrap;min-width:0}' +
    '#desk-nav .dn-ap[hidden]{display:none}' +
    '#desk-nav .dn-ap b{color:var(--n-fg);font-weight:500}' +
    '#desk-nav .dot{width:8px;height:8px;border-radius:50%;background:var(--n-fg3);flex:none}' +
    '#desk-nav [data-state="idle"] .dot{background:var(--n-good)}#desk-nav [data-state="running"] .dot{background:var(--n-acc)}' +
    '#desk-nav [data-state="error"] .dot{background:var(--n-crit)}' +
    '#desk-nav .dn-m{color:var(--n-fg3);white-space:nowrap}' +
    '#desk-nav .dn-prog{color:var(--n-fg);max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '#desk-nav a.dn-err{color:var(--n-crit);max-width:300px;overflow:hidden;text-overflow:ellipsis;padding:0;border:0}' +
    '@media (max-width:760px){#desk-nav{padding-inline:8px}#desk-nav .dn-st{padding-bottom:4px}}' +
    '@media print{#desk-nav{display:none}}';

  var nav = el('nav', { id: 'desk-nav', 'aria-label': 'Страницы деска' });
  var links = el('div', { 'class': 'dn-links' }), st = el('div', { 'class': 'dn-st' });
  var home = el('a', { href: href('index.html'), 'class': 'dn-home', title: 'Главная: страницы и последние прогоны' }, 'TDM');
  if (here === 'index.html') home.setAttribute('aria-current', 'page');
  links.appendChild(home);
  PAGES.forEach(function (p) {
    if (p[0] === 'app/' && !http) return;  // React-приложение есть только на сервере
    var a = el('a', { href: href(p[0]), title: p[2] }, p[1]);
    if (p[0] === here) a.setAttribute('aria-current', 'page');
    links.appendChild(a);
  });
  nav.appendChild(links);
  nav.appendChild(st);

  function mountBar() {
    document.head.appendChild(el('style', {}, css));
    // встаёт вплотную к краям окна поверх полей body (у части страниц поле браузера по умолчанию, 8px)
    var m = getComputedStyle(document.body);
    nav.style.margin = '-' + m.marginTop + ' -' + m.marginRight + ' ' + m.marginTop + ' -' + m.marginLeft;
    document.body.insertBefore(nav, document.body.firstChild);
  }
  if (document.body) mountBar(); else document.addEventListener('DOMContentLoaded', mountBar);

  /* ---------- статус: данные, прогон, автопилот (после загрузки данных страницы) ---------- */
  function mountStatus() {
    var D = window.DESK, src = D && D.source, empty = !!window.DESK_EMPTY;
    var db = !!(src && src.kind === 'db');
    if (window.DESK_EMPTY && window.DESK_EMPTY.reason === 'not_found') remember({});  // запомненного прогона больше нет
    // «Тестовые данные» — только когда страница и правда на демо-наборе (data.js), а не на прогоне из базы
    if (D && !db && !empty) {
      st.appendChild(el('span', { 'class': 'dn-demo', title: 'Все цифры вымышленные' + (window.DESK_MISSING ? ': ' + window.DESK_MISSING : '') },
        'Тестовые данные'));
    }
    var sc = D && !empty && D.scope && D.scope.state;
    if (sc && sc.active) {
      var f = el('span', { 'class': 'dn-scope', title: 'Действует сохранённый фильтр позиций: ' + sc.label + '. Позиций ' + sc.n + ' из ' + sc.total });
      f.appendChild(el('span', {}, 'Фильтр: '));
      f.appendChild(el('b', {}, sc.label));
      f.appendChild(el('span', { 'class': 'dn-m' }, sc.n + ' из ' + sc.total + ' поз.'));
      var x = el('button', { type: 'button', title: 'Снять фильтр на всех страницах', 'aria-label': 'Снять фильтр' }, '✕');
      x.addEventListener('click', function () { D.scope.clear(); });
      f.appendChild(x);
      st.appendChild(f);
    }
    var run = el('span', { 'class': 'dn-run' });
    var label = db ? (src.slice || (src.cutType ? sliceLabel(D.asOf, src.cutType, src.cutTs, src.runType) : dt(D.asOf) + ' · ' + (src.runType || ''))) + ' #' + src.runId
      : D && !empty ? 'демо-данные' : sel.run_id ? 'прогон #' + sel.run_id : sel.as_of ? dt(sel.as_of) : sel.demo ? 'демо-данные' : 'последний прогон';
    run.appendChild(el('span', {}, 'Прогон'));
    var tip = db ? [src.publishedAt ? 'опубликован ' + src.publishedAt.replace('T', ' ').slice(0, 16) : '', src.pnlBasis || ''].filter(Boolean).join('; ') : '';
    var cur = el('b', { title: tip }, label);
    run.appendChild(cur);
    st.appendChild(run);
    if (!http) return;
    var up = el('script', { src: root + 'desk-updater.js', 'data-mode': 'nav', 'data-root': root });  // «Новая версия …»
    document.head.appendChild(up);

    function picker() {
      fetch(root + 'api/v1/runs?limit=200&status=complete', { cache: 'no-store' })
        .then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
          if (!j || !j.runs) return;
          var box = el('select', { 'aria-label': 'Дата и прогон (для всех страниц)', title: 'Прогон для всех страниц' });
          var latest = el('option', { value: '' }, 'последний прогон');
          box.appendChild(latest);
          // по датам (новые сверху), в дате показываемый прогон (is_current: закрытие, иначе внутридневной) первым;
          // прочие — по убыванию номера. «заменён»: не показываемый прогон даты, у которой есть более новый показываемый.
          var dayOf = function (r) { return r.business_date || r.as_of; };  // as_of: compat alias
          var newestCurrent = {};
          j.runs.forEach(function (r) { if (isCurrent(r)) newestCurrent[dayOf(r)] = Math.max(newestCurrent[dayOf(r)] || 0, +r.run_id); });
          var runs = j.runs.slice().sort(function (a, b) {
            return dayOf(a) < dayOf(b) ? 1 : dayOf(a) > dayOf(b) ? -1 : (isCurrent(b) ? 1 : 0) - (isCurrent(a) ? 1 : 0) || +b.run_id - +a.run_id;
          });
          if (!pinned && runs.length) {
            var top = runs.filter(isCurrent)[0] || runs[0];
            latest.textContent = 'последний: ' + (db && src.slice ? src.slice : runLabel(top)) + ' #' + (db ? src.runId : top.run_id);
          }
          var shown = db ? String(src.runId) : sel.run_id;
          runs.forEach(function (r) {
            var day = dayOf(r), done = r.completed_at || r.published_at;  // published_at: compat alias
            var old = !isCurrent(r) && newestCurrent[day] > +r.run_id;
            var o = el('option', { value: r.run_id, title: r.pnl_basis || '' }, runLabel(r) + ' #' + r.run_id + (done && r.cut_type !== 'intraday' ? ' · ' + String(done).slice(11, 16) : '') + (old ? ' (заменён)' : ''));
            if (pinned && String(r.run_id) === shown) o.selected = true;
            box.appendChild(o);
          });
          if (pinned && !db && !sel.demo && !box.querySelector('option:checked:not([value=""])')) {
            // дата (на странице без данных деска) или прогон, которого нет в списке: показать выбор как есть
            var o = sel.as_of ? el('option', { value: 'as_of:' + sel.as_of }, dt(sel.as_of) + ' · основной')
              : el('option', { value: sel.run_id }, 'прогон #' + sel.run_id + (empty ? ' — не найден' : ''));
            o.selected = true;
            box.insertBefore(o, latest.nextSibling);
          }
          var demo = el('option', { value: 'demo' }, 'демо-данные');
          if (sel.demo) demo.selected = true;
          box.appendChild(demo);
          if (!runs.length && !sel.demo) latest.textContent = 'прогонов ещё нет';
          box.addEventListener('change', function () {
            var v = box.value;
            select(v === 'demo' ? { demo: '1' } : v.indexOf('as_of:') === 0 ? { as_of: v.slice(6) } : v ? { run_id: v } : {});
          });
          var old = run.querySelector('select');
          if (old) run.replaceChild(box, old); else { run.replaceChild(box, cur); box.title = cur.title || box.title; }
        }).catch(function () {});
    }
    picker();
    autopilot(D, db, empty, picker);
  }

  function autopilot(D, db, empty, picker) {
    var ST = { idle: 'ожидание', running: 'идёт цикл', error: 'ошибка' };
    var data = (!!D && !empty) || inApp;  // страница показывает данные прогона
    var box = el('span', { 'class': 'dn-ap', role: 'status', 'aria-live': 'polite', hidden: '' });
    st.appendChild(box);
    // base — прогон на экране: новый в latest_run значит «посчитан новый день/прогон»; страница, закреплённая на
    // другом прогоне (выбор помнится 12 часов), сразу предлагает последний: иначе вчерашний или посчитанный до
    // обновления прогон выглядит как текущий (R14, R17)
    var base = db ? String(D.source.runId) : null, fastUntil = 0, timer = null, busy = false, last = null;
    function schedule() { clearTimeout(timer); timer = setTimeout(poll, Date.now() < fastUntil || (last && last.state === 'running') ? 5000 : 30000); }
    function once(runId) {  // одна авто-перезагрузка на прогон: не зациклиться, если сервер его всё же не отдал
      try {
        if (sessionStorage.getItem('desk-nav.reload') === runId) return false;
        sessionStorage.setItem('desk-nav.reload', runId);
      } catch (e) { return false; }
      return true;
    }
    function render(j) {
      last = j;
      var s = !j.enabled ? 'off' : (j.state || 'idle'), lr = j.latest_run, fresh = data && lr && base != null && String(lr.run_id) !== base;
      box.textContent = '';
      box.dataset.state = s;
      box.appendChild(el('span', { 'class': 'dot', 'aria-hidden': 'true' }));
      var t = el('span', {}, 'Автопилот: ');
      t.appendChild(el('b', {}, j.enabled ? (ST[s] || s) : 'выключен'));
      box.appendChild(t);
      if (s === 'running' && j.progress && j.message) box.appendChild(el('span', { 'class': 'dn-prog', title: j.progress.detail || j.message }, j.message));
      if (j.last_cycle_at) {
        box.appendChild(el('span', { 'class': 'dn-m', title: 'Последний цикл' + (j.last_ok_at ? '; последний успешный ' + j.last_ok_at.replace('T', ' ').slice(0, 16) : '') },
          'обн. ' + hm(j.last_cycle_at)));
      }
      if (s === 'error' && j.message) box.appendChild(el('a', { 'class': 'dn-err', href: href('settings.html'), title: j.message + ' — Настройки' }, j.message));
      if (fresh) {
        box.appendChild(el('button', { type: 'button', 'class': 'dn-new', 'data-act': 'load',
          title: pinned ? 'Перейти на последний прогон (на всех страницах)' : 'Загрузить новый прогон; фильтры и доски сохранятся' },
          (pinned ? 'Последний прогон ' : 'Новый прогон ') + (lr.label || dt(lr.as_of)) + ' — показать'));
      }
      var b = el('button', { type: 'button', 'data-act': 'run', title: 'Запустить цикл автопилота сейчас: хаб, папки отчётов, EOD' }, 'Обновить');
      if (busy || s === 'running') b.disabled = true;
      var au = window.DESK_AUTH && window.DESK_AUTH.state;  // роль «просмотр» цикл не запускает (сервер ответит 403)
      if (!(au && au.enabled && au.user && au.user.role === 'viewer')) box.appendChild(b);
      box.hidden = false;
    }
    function poll() {
      fetch(root + 'api/v1/autopilot/status', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
        if (!j || typeof j !== 'object') { box.hidden = true; return; }
        var lr = j.latest_run && String(j.latest_run.run_id);
        if (base == null && lr) base = lr;  // страница закреплена на прогоне или без данных: новое — после загрузки
        else if (lr && lr !== base) {
          if (!data) { base = lr; picker(); window.dispatchEvent(new CustomEvent('desk-nav:new-run', { detail: j.latest_run })); }
          else if (!pinned && document.hidden && !Object.keys(stored()).length && once(lr)) { location.reload(); return; }  // другой вкладкой не выбран свой прогон
        }
        render(j);
      }).catch(function () { box.hidden = true; }).then(schedule);
    }
    box.addEventListener('click', function (e) {
      var act = e.target.getAttribute && e.target.getAttribute('data-act');
      if (act === 'load') { select({}); return; }  // последний прогон — и для остальных страниц
      if (act !== 'run') return;
      busy = true; if (last) render(last);
      fetch(root + 'api/v1/autopilot/run', { method: 'POST' }).then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); })
        .catch(function (err) { if (last) render(Object.assign({}, last, { state: 'error', message: 'Не удалось запустить: ' + err.message })); })
        .then(function () { busy = false; fastUntil = Date.now() + 60000; clearTimeout(timer); timer = setTimeout(poll, 1500); });
    });
    poll();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mountStatus); else mountStatus();
})();
