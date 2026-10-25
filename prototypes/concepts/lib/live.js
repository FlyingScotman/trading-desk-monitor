/*
 * Живые обновления мониторов (#28): лента событий сервера /api/v1/events без перезагрузки страницы.
 *
 * DESK_LIVE.subscribe({ types, after, onEvents, onMode, base, pollMs }) — подписка на ленту:
 *  - SSE: EventSource на ../api/v1/events/stream?after=&type= (backend desk/api/daystatus_routes.py). Сервер сначала
 *    шлёт hello, потом события (id: = event_id, event: = тип, data: = событие, как в JSON-ленте), молчание
 *    прерывает комментариями keep-alive и раз в 10 минут закрывает ответ: браузер переподключается сам и шлёт
 *    Last-Event-ID, пропущенное приходит;
 *  - откат на опрос GET ../api/v1/events?after=<курсор> (раз в pollMs, по умолчанию 30 с, только видимая вкладка),
 *    если EventSource нет, поток не открылся (ответ не text/event-stream, 401/404), соединение рвётся три раза
 *    подряд без hello или hello не пришёл за 10 с (прокси копит ответ). Из опроса раз в 5 минут снова пробуем SSE;
 *  - курсор — id последнего полученного события (после подписки: last_id ленты, если after не дан); onEvents(list)
 *    получает новые события по порядку, без повторов при переходе между SSE и опросом. onMode('sse' | 'poll').
 *  Возвращает { stop(), mode(), cursor() }.
 *
 * Реакция страницы прогона на run.published: ОДНА ТОЧКА ВЫБОРА — DESK_LIVE.REACTION (ниже):
 *  - 'banner' (по умолчанию): полоса под шапкой «Доступен новый прогон … — показать», страница не меняется под
 *    пользователем;
 *  - 'switch': страница сама переходит на новый прогон (перезагрузка на «последний прогон»), один раз на прогон.
 *  Закреплённый прогон (?run_id= / ?as_of= или выбор, запомненный на 12 часов) никогда не переключается сам: только
 *  полоса «Показать последний». Выбор по умолчанию ждёт решения владельца (#28); в этом браузере его можно
 *  переопределить: localStorage 'desk.live.reaction' = 'banner' | 'switch'.
 *  Страницы с прогоном (K.source 'real' или 'empty') подписываются сами после загрузки; страница со своей
 *  обработкой ленты (C14) ставит DESK_LIVE.manual = true до DOMContentLoaded.
 *
 * Чистые функции для тестов (tests/js/live_updates.test.js): newRun, decide, reaction.
 */
(function () {
  'use strict';
  if (typeof window !== 'undefined' && window.DESK_LIVE) return;
  const L = {};
  L.REACTION = 'banner';  // 'banner' | 'switch' — дефолт ждёт решения владельца (#28)
  L.REACTIONS = ['banner', 'switch'];
  L.KEY = 'desk.live.reaction';
  // типы ленты (desk/daystatus.py: LEVELS, EVENT_TEXT; inbox/scan.py; store.run_event): EventSource отдаёт событие
  // с полем event: только подписчику на этот тип, поэтому список нужен целиком (tests/js/live_updates.test.js
  // сверяет его с бэкендом)
  L.TYPES = ['run.published', 'run.failed',
    'input.accepted', 'input.partially_accepted', 'input.superseded', 'input.rejected', 'input.failed',
    'input.match_failed', 'input.waiting_calendar', 'input.ignored', 'input.stale', 'input.duplicate',
    'books.changed', 'reference.changed'];
  L.HELLO_MS = 10e3;
  L.RETRY_SSE_MS = 300e3;
  L.MAX_ERRORS = 3;

  // выбранная реакция: переопределение этого браузера или дефолт
  L.reaction = function (storage) {
    try {
      const v = (storage || localStorage).getItem(L.KEY);
      if (L.REACTIONS.indexOf(v) >= 0) return v;
    } catch (e) { }
    return L.REACTION;
  };

  // run.published, которого нет на экране и который не старше его даты (досчёт истории публикует прошлые даты)
  // ctx: { source: 'real' | 'empty', runId, asOf }
  L.newRun = function (e, ctx) {
    if (!e || e.type !== 'run.published' || e.run_id == null) return false;
    const rt = (e.detail || {}).run_type;
    if (rt === 'TEST' || rt === 'REPLAY') return false;
    if (ctx.source === 'empty') return true;
    if (String(e.run_id) === String(ctx.runId)) return false;
    return !(e.valid_date && ctx.asOf && e.valid_date < ctx.asOf);
  };

  // что делать с новым прогоном: 'banner' | 'switch'; закреплённый выбор — только полоса; переключение — один раз
  // на прогон (switched: id, на который страница уже переключалась в этой вкладке)
  L.decide = function (mode, ctx, e, switched) {
    if (mode !== 'switch' || ctx.pinned) return 'banner';
    return switched != null && String(switched) === String(e.run_id) ? 'banner' : 'switch';
  };

  // ---------- подписка ----------
  L.subscribe = function (opt) {
    const base = opt.base == null ? '../' : opt.base, pollMs = opt.pollMs || 30e3;
    const typeQ = opt.types ? '&type=' + encodeURIComponent(opt.types) : '';
    let cursor = opt.after == null ? null : +opt.after, mode = null, es = null, timer = null, hello = null, retry = null, stopped = false, errors = 0;
    const getJSON = p => fetch(base + p, { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error(r.status + ''); return r.json(); });
    function setMode(m) { if (m !== mode) { mode = m; if (opt.onMode) opt.onMode(m); } }
    function deliver(list) {
      const fresh = list.filter(e => cursor == null || e.id > cursor);
      if (!fresh.length) return;
      cursor = fresh[fresh.length - 1].id;
      try { opt.onEvents(fresh); } catch (err) { console.error(err); }
    }
    function closeSSE() { if (es) { es.close(); es = null; } clearTimeout(hello); }
    function poll() {
      clearTimeout(timer);
      if (stopped) return;
      const next = () => { if (!stopped && mode === 'poll') timer = setTimeout(poll, pollMs); };
      if (typeof document !== 'undefined' && document.hidden) return next();
      const page = () => getJSON('api/v1/events?after=' + cursor + typeQ + '&limit=200').then(j => {
        deliver(j.events || []);
        if (j.has_more && !stopped) return page();
      });
      page().catch(() => { }).then(next);
    }
    function toPoll() {
      closeSSE(); setMode('poll'); poll();
      if (typeof window !== 'undefined' && window.EventSource && !opt.noSSE) { clearTimeout(retry); retry = setTimeout(() => { if (!stopped && mode === 'poll') sse(); }, L.RETRY_SSE_MS); }
    }
    function sse() {
      if (stopped) return;
      if (typeof window === 'undefined' || !window.EventSource || opt.noSSE) return toPoll();
      closeSSE(); clearTimeout(timer); errors = 0;
      es = new window.EventSource(base + 'api/v1/events/stream?after=' + cursor + typeQ);
      const armHello = () => { clearTimeout(hello); hello = setTimeout(toPoll, L.HELLO_MS); };
      armHello();
      es.addEventListener('hello', () => { clearTimeout(hello); errors = 0; setMode('sse'); });
      const on = m => { try { deliver([JSON.parse(m.data)]); } catch (err) { console.error(err); } };
      L.TYPES.forEach(t => es.addEventListener(t, on));
      es.onmessage = on;  // событие без поля event: (на будущее)
      es.onerror = () => {
        if (!es) return;
        // CLOSED: браузер не будет переподключаться (не text/event-stream, 401, 404); иначе — переподключение
        if (es.readyState === 2 || ++errors >= L.MAX_ERRORS) return toPoll();
        armHello();
      };
    }
    function start() {
      if (cursor != null) return sse();
      getJSON('api/v1/events?limit=1').then(j => { cursor = j.last_id == null ? 0 : j.last_id; sse(); })
        .catch(() => { timer = setTimeout(start, pollMs); });
    }
    start();
    return {
      stop() { stopped = true; closeSSE(); clearTimeout(timer); clearTimeout(retry); },
      mode: () => mode,
      cursor: () => cursor
    };
  };

  // ---------- страница прогона: новый прогон ----------
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  function goLatest() {  // как kit.js go({}): «последний прогон» — пустой выбор, и для остальных страниц
    try { localStorage.removeItem('desk.run.v1'); } catch (e) { }
    const u = new URL(location.href); ['run_id', 'as_of', 'demo'].forEach(k => u.searchParams.delete(k));
    location.href = u.toString();
  }
  function switched(id) {
    try {
      if (id === undefined) return sessionStorage.getItem('desk.live.switched');
      sessionStorage.setItem('desk.live.switched', String(id));
    } catch (e) { return id === undefined ? null : undefined; }
  }
  let bar = null;
  function banner(e, ctx) {
    if (!bar) {
      const st = document.createElement('style');
      st.textContent = '.k-live{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:6px 14px;background:var(--accent-bg);border-bottom:1px solid var(--accent);font-size:12.5px}' +
        '.k-live .t{flex:1;min-width:200px}.k-live button{font:inherit;cursor:pointer}';
      document.head.appendChild(st);
      bar = document.createElement('div'); bar.className = 'k-live'; bar.setAttribute('role', 'status'); bar.setAttribute('aria-live', 'polite');
      const top = document.querySelector('.k-top');
      if (top) top.after(bar); else document.body.prepend(bar);
    }
    bar.innerHTML = '<span class="t">Доступен новый прогон: ' + esc(e.text || ('#' + e.run_id)) + '</span>' +
      '<button class="k-btn" data-a="show">' + (ctx.pinned ? 'Показать последний' : 'Показать') + '</button>' +
      '<button class="k-btn" data-a="close" title="Скрыть до следующего прогона">✕</button>';
    bar.querySelector('[data-a=show]').onclick = () => ctx.pinned ? goLatest() : location.reload();
    bar.querySelector('[data-a=close]').onclick = () => { bar.remove(); bar = null; };
  }
  L.watchRuns = function (ctx) {
    return L.subscribe({
      types: 'run.published',
      onEvents(list) {
        const runs = list.filter(e => L.newRun(e, ctx));
        if (!runs.length) return;
        const e = runs[runs.length - 1];
        if (L.decide(L.reaction(), ctx, e, switched()) === 'switch') { switched(e.run_id); location.reload(); return; }
        banner(e, ctx);
      }
    });
  };
  function auto() {
    const K = window.KIT, M = window.MOCK;
    if (L.manual || !K || (K.source !== 'real' && K.source !== 'empty')) return;
    const sel = window.CONCEPTS_SEL || {};
    L.page = L.watchRuns({ source: K.source, runId: M && M.run && M.run.id, asOf: M && M.asOf, pinned: !!(sel.run_id || sel.as_of) });
  }

  if (typeof window !== 'undefined') {
    window.DESK_LIVE = L;
    if (typeof document !== 'undefined' && document.addEventListener) {
      if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', auto); else setTimeout(auto, 0);
    }
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = L;
})();
