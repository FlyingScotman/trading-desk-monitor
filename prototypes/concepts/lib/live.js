/*
 * Живые обновления мониторов (#28): лента событий сервера /api/v1/events без перезагрузки страницы.
 *
 * DESK_LIVE.subscribe({ types, after, onEvents, onMode, base, pollMs }) — подписка на ленту:
 *  - одно соединение на браузер: у Chromium по HTTP/1.1 не больше 6 соединений на сервер на все вкладки, и поток
 *    в каждой вкладке занял бы их все (ревью #191). Вкладка-лидер (Web Locks: navigator.locks, замок
 *    'desk-live-leader') держит поток и раздаёт события остальным вкладкам через BroadcastChannel 'desk-live';
 *    закрылась вкладка-лидер — замок переходит к следующей, та продолжает со своего курсора. Новая вкладка-слушатель
 *    один раз дочитывает ленту JSON-запросом от своего курсора;
 *  - поток лидера: EventSource на ../api/v1/events/stream?after= (backend desk/api/daystatus_routes.py). Сервер
 *    сначала шлёт hello, потом события (id: = event_id, event: = тип, data: = событие, как в JSON-ленте), молчание
 *    прерывает комментариями keep-alive и раз в 10 минут закрывает ответ: браузер переподключается сам и шлёт
 *    Last-Event-ID, пропущенное приходит;
 *  - лидер переходит на опрос GET ../api/v1/events?after=<курсор> (раз в pollMs, по умолчанию 30 с), если
 *    EventSource нет, поток не открылся (ответ не text/event-stream, 401/404), рвётся три раза подряд без hello
 *    или hello не пришёл за 10 с (прокси копит ответ). Из опроса раз в 5 минут снова пробует поток;
 *  - без Web Locks или BroadcastChannel (старый браузер) потока нет совсем: опрашивает только видимая вкладка;
 *  - курсор — id последнего полученного события (после подписки: last_id ленты, если after не дан); onEvents(list)
 *    получает новые события своих типов (префиксы types) по порядку, без повторов. onMode('sse' | 'poll' |
 *    'signed-out': сервер ответил 401/403, сессия истекла — лента стоит, пока не войти снова).
 *  Возвращает { stop(), mode(), cursor(), leader() }.
 *
 * Реакция страницы прогона на run.published: ОДНА ТОЧКА ВЫБОРА — DESK_LIVE.REACTION (ниже):
 *  - 'switch' (по умолчанию, решение владельца 25.10, #28): страница сама переходит на новый прогон (перезагрузка
 *    на «последний прогон»), один раз на прогон: петли перезагрузок нет (после перезагрузки лента начинается
 *    с текущего момента, а sessionStorage помнит прогон, на который уже переключались). Скрытая вкладка
 *    перезагружается, когда её покажут. После загрузки страница один раз сверяет свой прогон с latest_run: прогон,
 *    опубликованный, пока она грузилась, тоже переключает;
 *  - 'banner': полоса под шапкой «Доступен новый прогон … — показать», страница не меняется под пользователем.
 *  Какой прогон новый, говорит сервер: GET ../api/v1/autopilot/status → latest_run, прогон, который откроет
 *  «последний прогон» (R11: основной прогон даты, иначе внутридневной). Если это прогон на экране (опубликован
 *  не показываемый прогон), ничего не происходит: перезагрузка показала бы то же самое.
 *  Закреплённый прогон (?run_id= / ?as_of= или выбор, запомненный на 12 часов) никогда не переключается сам: только
 *  полоса «Показать последний». В этом браузере выбор можно переопределить: localStorage 'desk.live.reaction' =
 *  'banner' | 'switch'.
 *  Страницы с прогоном (K.source 'real' или 'empty') подписываются сами после загрузки; страница со своей
 *  обработкой ленты (C14) ставит DESK_LIVE.manual = true до DOMContentLoaded.
 *
 * Чистые функции для тестов (tests/js/live_updates.test.js): newRun, target, decide, reaction, want, react.
 */
(function () {
  'use strict';
  if (typeof window !== 'undefined' && window.DESK_LIVE) return;
  const L = {};
  L.REACTION = 'switch';  // 'switch' | 'banner' — автопереключение по решению владельца 25.10 (#28)
  L.REACTIONS = ['banner', 'switch'];
  L.KEY = 'desk.live.reaction';
  L.LOCK = 'desk-live-leader';
  L.CHANNEL = 'desk-live';
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

  // событие нужных типов: префиксы через запятую, как ?type= ленты (daystatus.events)
  L.want = function (e, types) {
    const p = String(types || '').split(',').map(s => s.trim()).filter(Boolean);
    return !p.length || p.some(t => String(e.type).indexOf(t) === 0);
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

  // на какой прогон переходить по новому прогону e: latest — latest_run статуса автопилота (что откроет «последний
  // прогон»), undefined — статус не получен. null — ничего не делать: «последний» и есть прогон на экране (R11)
  L.target = function (e, latest, ctx) {
    if (latest === undefined) return { run_id: e.run_id, text: e.text };
    if (!latest) return null;
    if (ctx.source === 'real' && String(latest.run_id) === String(ctx.runId)) return null;
    return { run_id: latest.run_id, text: (latest.label ? latest.label + ' · ' : '') + '#' + latest.run_id };
  };

  // что делать с новым прогоном: 'banner' | 'switch'; закреплённый выбор — только полоса; переключение — один раз
  // на прогон (switched: id, на который страница уже переключалась в этой вкладке)
  L.decide = function (mode, ctx, e, switched) {
    if (mode !== 'switch' || ctx.pinned) return 'banner';
    return switched != null && String(switched) === String(e.run_id) ? 'banner' : 'switch';
  };

  // ---------- подписка ----------
  L.subscribe = function (opt) {
    const base = opt.base == null ? '../' : opt.base, pollMs = opt.pollMs || 30e3, types = opt.types || '';
    const W = typeof window !== 'undefined' ? window : {};
    const nav = W.navigator || (typeof navigator !== 'undefined' ? navigator : null);
    const shared = !opt.noShare && !!W.BroadcastChannel && !!(nav && nav.locks && nav.locks.request);
    let cursor = opt.after == null ? null : +opt.after, mode = null, es = null, timer = null, hello = null, retry = null;
    let stopped = false, errors = 0, lead = false, ch = null, release = null, abort = null, catching = false, queue = [];
    const getJSON = p => fetch(base + p, { cache: 'no-store' }).then(r => {
      if (!r.ok) throw Object.assign(new Error(r.status + ''), { status: r.status });
      return r.json();
    });
    function setMode(m, quiet) {
      if (m === mode) return;
      mode = m;
      if (lead && ch && !quiet) ch.postMessage({ kind: 'mode', mode: m });
      if (opt.onMode) opt.onMode(m);
    }
    // events — все события ленты (лидер и рассылка) или уже отобранные сервером (опрос без лидера): курсор идёт по
    // всем, onEvents получает свои типы
    function deliver(list, fromLeader) {
      const fresh = list.filter(e => cursor == null || e.id > cursor);
      if (!fresh.length) return;
      cursor = fresh[fresh.length - 1].id;
      if (lead && ch && !fromLeader) ch.postMessage({ kind: 'events', list: fresh });
      const mine = fresh.filter(e => L.want(e, types));
      if (mine.length) try { opt.onEvents(mine); } catch (err) { console.error(err); }
    }
    function closeSSE() { if (es) { es.close(); es = null; } clearTimeout(hello); }
    // запросы лидера — без фильтра типов (события нужны всем вкладкам); опрос без лидера — со своими типами
    const typeQ = () => !lead && types ? '&type=' + encodeURIComponent(types) : '';
    function poll() {
      clearTimeout(timer);
      if (stopped) return;
      const next = () => { if (!stopped && (mode === 'poll' || mode === 'signed-out')) timer = setTimeout(poll, pollMs); };
      // без лидера опрашивает только видимая вкладка; лидер опрашивает и скрытым: он один на браузер
      if (!lead && typeof document !== 'undefined' && document.hidden) return next();
      const page = () => getJSON('api/v1/events?after=' + cursor + typeQ() + '&limit=200').then(j => {
        if (mode === 'signed-out') setMode('poll');
        deliver(j.events || []);
        if (j.has_more && !stopped) return page();
      });
      page().catch(err => { if (err && (err.status === 401 || err.status === 403)) setMode('signed-out'); }).then(next);
    }
    function toPoll() {
      closeSSE(); setMode('poll'); poll();
      if (lead && W.EventSource) { clearTimeout(retry); retry = setTimeout(() => { if (!stopped && lead && mode !== 'sse') sse(); }, L.RETRY_SSE_MS); }
    }
    function sse() {
      if (stopped) return;
      if (!lead || !W.EventSource) return toPoll();
      closeSSE(); clearTimeout(timer); errors = 0;
      es = new W.EventSource(base + 'api/v1/events/stream?after=' + cursor);
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
    function becomeLeader() {
      return new Promise(res => {
        if (stopped) return res();
        release = res; lead = true;
        mode = null; sse();
      });
    }
    function join() {
      ch = new W.BroadcastChannel(L.CHANNEL);
      ch.onmessage = m => {
        const d = m.data || {};
        if (d.kind === 'events' && !lead) { if (catching) queue.push(d.list); else deliver(d.list, true); }
        else if (d.kind === 'mode' && !lead) setMode(d.mode, true);
        else if (d.kind === 'who' && lead && mode) ch.postMessage({ kind: 'mode', mode });
      };
      // слушатель: один раз дочитать ленту от своего курсора, потом — рассылка лидера
      catching = true;
      getJSON('api/v1/events?after=' + cursor + '&limit=500').then(j => { if (!lead) deliver(j.events || []); }, () => { })
        .then(() => { catching = false; queue.splice(0).forEach(l => { if (!lead) deliver(l, true); }); });
      ch.postMessage({ kind: 'who' });
      abort = typeof AbortController !== 'undefined' ? new AbortController() : null;
      nav.locks.request(L.LOCK, abort ? { signal: abort.signal } : {}, becomeLeader).catch(() => { });
    }
    function begin() { if (shared) join(); else toPoll(); }
    function start() {
      if (cursor != null) return begin();
      getJSON('api/v1/events?limit=1').then(j => { cursor = j.last_id == null ? 0 : j.last_id; begin(); })
        .catch(() => { timer = setTimeout(start, pollMs); });
    }
    start();
    return {
      stop() {
        stopped = true; closeSSE(); clearTimeout(timer); clearTimeout(retry);
        if (abort) abort.abort();
        if (release) release();
        if (ch) ch.close();
        lead = false;
      },
      mode: () => mode,
      cursor: () => cursor,
      leader: () => lead
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
  function banner(t, ctx) {
    if (!bar) {
      const st = document.createElement('style');
      st.textContent = '.k-live{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:6px 14px;background:var(--accent-bg);border-bottom:1px solid var(--accent);font-size:12.5px}' +
        '.k-live .t{flex:1;min-width:200px}.k-live button{font:inherit;cursor:pointer}';
      document.head.appendChild(st);
      bar = document.createElement('div'); bar.className = 'k-live'; bar.setAttribute('role', 'status'); bar.setAttribute('aria-live', 'polite');
      const top = document.querySelector('.k-top');
      if (top) top.after(bar); else document.body.prepend(bar);
    }
    bar.innerHTML = '<span class="t">Доступен новый прогон: ' + esc(t.text || ('#' + t.run_id)) + '</span>' +
      '<button class="k-btn" data-a="show">' + (ctx.pinned ? 'Показать последний' : 'Показать') + '</button>' +
      '<button class="k-btn" data-a="close" title="Скрыть до следующего прогона">✕</button>';
    bar.querySelector('[data-a=show]').onclick = () => ctx.pinned ? goLatest() : location.reload();
    bar.querySelector('[data-a=close]').onclick = () => { bar.remove(); bar = null; };
  }
  function latestRun(base) {  // undefined — статус не получен
    return fetch(base + 'api/v1/autopilot/status', { cache: 'no-store' }).then(r => r.ok ? r.json() : undefined)
      .then(j => j && typeof j === 'object' ? j.latest_run || null : undefined, () => undefined);
  }
  // скрытая вкладка перезагружается, когда её покажут: не все вкладки разом и не под руками у пользователя
  function reloadWhenSeen() {
    if (typeof document === 'undefined' || !document.hidden) return location.reload();
    const f = () => { if (!document.hidden) { document.removeEventListener('visibilitychange', f); location.reload(); } };
    document.addEventListener('visibilitychange', f);
  }
  // реакция на новый прогон e (null — сверка после загрузки: не опубликован ли прогон, пока страница грузилась);
  // d — зависимости (тесты подменяют): latest() → latest_run | null | undefined, reload, banner, switched
  L.react = function (e, ctx, d) {
    return d.latest().then(lr => {
      const t = e ? L.target(e, lr, ctx) : lr && L.target({}, lr, ctx);
      if (!t) return 'none';
      if (L.decide(L.reaction(d.storage), ctx, t, d.switched()) === 'switch') { d.switched(t.run_id); d.reload(); return 'switch'; }
      if (!e) return 'none';  // сверка при загрузке полосу не показывает (закреплённый прогон сверку не делает)
      d.banner(t, ctx); return 'banner';
    });
  };
  L.watchRuns = function (ctx) {
    const d = { latest: () => latestRun('../'), reload: reloadWhenSeen, banner, switched };
    let busy = Promise.resolve();
    // одно звено не должно остановить реакцию на следующие прогоны (ревью #191)
    const queue = e => { busy = busy.then(() => L.react(e, ctx, d)).catch(err => console.error(err)); };
    // курсор ленты берётся после того, как страница получила прогон: прогон, опубликованный в это окно, ловит сверка
    if (!ctx.pinned && ctx.source === 'real') queue(null);
    return L.subscribe({
      types: 'run.published',
      onEvents(list) {
        const runs = list.filter(e => L.newRun(e, ctx));
        if (runs.length) queue(runs[runs.length - 1]);
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
