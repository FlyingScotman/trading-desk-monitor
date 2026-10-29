/* C19 «PnL за период», экран F · Контроль (TDM#409): /api/v1/history/pnl/checks — пороги необъяснённого, карточки
   проверок со статусом (фильтр списка), календарь покрытия, «Необъяснённое» по причине, затем по методу расчёта —
   столбцы, дни и крупнейшие позиции (TDM#436, TDM#442, unexplainedBy), тепловая карта необъяснённого «позиции × дни», список находок; каждая
   находка по позиции, клетка карты и строка крупнейших открывают E. Missing values stay «—», never 0. */
(function () {
  'use strict';
  const SCREENS = window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
  const UNIT = 'млн RUB', PATH = '/api/v1/history/pnl/checks';
  let charts = [], cur = null;  // cur — the mounted render: {ctx, main, key}; null after dispose
  const load = PNL_HISTORY.loader((q, signal) => cur.ctx.fetchJson(PATH, q, signal), r => {
    if (!cur) return;
    const box = cur.main.querySelector('#ckBody');
    if (!box) return;
    if (r.loading) box.innerHTML = '<div class="k-panel"><div class="bd k-empty">Загружаю проверки периода…</div></div>';
    else if (r.error) box.innerHTML = '<div class="k-panel"><div class="bd k-empty">' + cur.ctx.K.esc(r.error.message) + '</div></div>';
    else { remember(cur.ctx.state.ui, cur.key, r.data); draw(cur.ctx, box, r.data); }
  });
  function remember(ui, key, v) {
    const c = ui.cache || (ui.cache = new Map());
    if (v === undefined) return c.get(key);
    c.set(key, v); if (c.size > 40) c.delete(c.keys().next().value);
    return v;
  }
  const disposeCharts = () => { charts.forEach(c => c.dispose()); charts = []; };
  function dispose() { cur = null; load.invalidate(); disposeCharts(); }

  SCREENS.control = {
    render(main, ctx) {
      const { K, P, state } = ctx, ui = state.ui, esc = K.esc;
      if (ui.thrAbs == null) ui.thrAbs = P.THR_DEFAULT.abs;
      if (ui.thrPct == null) ui.thrPct = P.THR_DEFAULT.pct;
      main.innerHTML = lede(K, 'control') + '<div class="k-panel"><div class="hd ctl"><h3>Пороги</h3>' +
        '<label>необъяснённое от <input type="number" id="thrAbs" min="0" step="0.5" value="' + esc(String(ui.thrAbs)) + '" style="width:76px"> млн RUB</label>' +
        '<label>и от <input type="number" id="thrPct" min="0" step="5" value="' + esc(String(ui.thrPct)) + '" style="width:64px"> % объяснённого</label>' +
        '<span class="sp"></span><span class="hint">проверки идут по текущему периоду и фильтру; пороги — и для отметок ▲ на экране E</span></div></div><div id="ckBody"></div>';
      const set = (id, key) => { const el = main.querySelector('#' + id); el.onchange = () => { const v = parseFloat(el.value); if (!isFinite(v) || v < 0) { el.value = ui[key]; return; } ui[key] = v; ctx.rerender(); }; };
      set('thrAbs', 'thrAbs'); set('thrPct', 'thrPct');
      const q = ctx.query(P.thrQuery(ui)), key = PATH + '?' + q;
      cur = { ctx, main, key };
      const hit = remember(ui, key);
      if (hit) draw(ctx, main.querySelector('#ckBody'), hit); else load(q);
    },
    dispose,
  };

  function lede(K, key) {
    const b = document.querySelector('#tabs [data-tab=' + key + ']');
    const t = b ? b.textContent.replace(/^[A-F]/, '') : '', l = b ? b.querySelector('b').textContent : '';
    return '<div class="lede"><h1>' + K.esc(l + ' · ' + t) + '</h1><p>' + K.esc(b ? b.title : '') + '</p></div>';
  }
  const sevChip = (P, s, n) => '<span class="sev ' + s + '" title="' + P.SEV[s].label + '">' + P.SEV[s].icon + ' ' + (n == null ? P.SEV[s].label : n) + '</span>';

  function draw(ctx, box, ck) {
    const { K, P, state } = ctx, f = K.f, esc = K.esc, ui = state.ui;
    disposeCharts();
    const cards = P.checkCards(ck), labels = new Map(cards.map(c => [c.key, c.label]));
    if (ui.chk && !labels.has(ui.chk)) ui.chk = null;
    const card = c => '<button class="check" data-k="' + esc(c.key) + '" aria-pressed="' + (ui.chk === c.key) + '"><span class="h"><b>' + esc(c.label) + '</b><span class="chips">' +
      (c.status === 'ok' ? '<span class="sev ok">✓ в норме</span>' : c.status === 'no_data' ? '<span class="sev nd">нет данных</span>' : c.bySev.map(([s, n]) => sevChip(P, s, n)).join('')) + '</span></span>' +
      '<span class="d">' + esc(c.note || (c.status === 'no_data' ? 'нет входных данных для проверки' : '')) + '</span></button>';
    const thr = 'порог ' + f.mn(ck.thrAbs, 1) + ' млн RUB и ' + f.n((ck.thrPct || 0) * 100, 0) + '% объяснённого';
    box.innerHTML = '<div class="checks">' + cards.map(card).join('') + '</div>' +
      '<div class="k-panel"><div class="hd"><h3>Покрытие по дням</h3><span class="sp"></span><span class="legendrow"><span><i class="cal-ok"></i>прогон без находок</span><span><i class="cal-issue"></i>есть находки</span><span><i class="cal-norun"></i>нет прогона</span></span></div><div class="bd"><div class="cal" id="ckCal"></div></div></div>' +
      '<div id="ckWhy"></div>' +
      '<div class="k-panel"><div class="hd"><h3>Необъяснённое: позиции × дни</h3><span class="sp"></span><span class="hint">' + UNIT + ' · шкала до порога (' + esc(thr) + '), выше — насыщенный цвет · щелчок — позиция на этот день</span></div><div class="bd"><div class="ch" id="ckHeat"></div></div></div>' +
      '<div class="k-panel"><div class="hd"><h3 id="ckTitle"></h3><span class="sp"></span><span id="ckClr"></span></div><div class="bd flush tscroll" id="ckList"></div></div>';
    box.querySelectorAll('.check').forEach(b => b.onclick = () => { ui.chk = ui.chk === b.dataset.k ? null : b.dataset.k; draw(ctx, box, ck); });
    drawCalendar(ctx, box.querySelector('#ckCal'), ck);
    drawWhy(ctx, box.querySelector('#ckWhy'), ck);
    drawHeat(ctx, box.querySelector('#ckHeat'), ck);
    drawList(ctx, box, ck, labels);
  }

  function drawCalendar(ctx, el, ck) {
    const { K, P } = ctx, f = K.f, days = P.calendar(ck);
    if (!days.length) { el.outerHTML = '<div class="k-empty">нет рыночных дней и прогонов в периоде</div>'; return; }
    const what = d => (d.state === 'norun' ? 'нет прогона' : d.state === 'issue' ? 'находок: ' + d.n : 'прогон без находок');
    el.innerHTML = days.map(d => '<i class="cal-' + d.state + (d.month ? ' month' : '') + '" title="' + f.date(d.date) + ' · ' + what(d) + '"></i>').join('');
  }

  function drawHeat(ctx, el, ck) {
    const { K, P, prep } = ctx, f = K.f, esc = K.esc, Hm = P.heatmap(ck.unexplained, ck.thrAbs);
    if (!Hm.data.length) { el.outerHTML = '<div class="k-empty">нет данных: необъяснённое не сохранено ни в одной записи выборки</div>'; return; }
    const pos = new Map(prep.positions.map(p => [p.ref, p])), name = ref => { const p = pos.get(ref); return p ? (p.name || p.positionId) + ' · ' + p.book : ref.replace('|', ' · '); };
    if (Hm.capped) el.insertAdjacentHTML('beforebegin', '<p class="hint" id="ckHeatCap" style="margin:0 0 4px">показаны ' + Hm.refs.length + ' из ' + Hm.total + ' позиций с наибольшим необъяснённым</p>');
    el.style.height = (Hm.refs.length * 22 + 110) + 'px';
    const c = K.chart(el, () => ({
      grid: { left: 8, right: 16, top: 10, bottom: 52, containLabel: true },
      tooltip: { trigger: 'item', formatter: p => '<b>' + esc(name(Hm.refs[p.value[1]])) + '</b><br>' + f.date(Hm.dates[p.value[0]]) + ' · необъяснённое ' + f.mn(p.value[2], 2) + ' млн' },
      xAxis: { type: 'category', data: Hm.dates, axisTick: { show: false }, splitArea: { show: false }, axisLabel: { formatter: d => d.slice(8, 10) + '.' + d.slice(5, 7), hideOverlap: true } },
      yAxis: { type: 'category', data: Hm.refs.map(name), axisTick: { show: false }, axisLabel: { fontSize: 11, width: el.clientWidth < 600 ? 110 : 170, overflow: 'truncate' } },
      visualMap: { min: -Hm.cap, max: Hm.cap, calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemHeight: 160, itemWidth: 10,
        text: ['+' + f.mn(Hm.cap, 1), '−' + f.mn(Hm.cap, 1)], textStyle: { color: K.css('--fg-3'), fontSize: 11 }, inRange: { color: [K.css('--neg'), K.css('--panel-2'), K.css('--accent')] } },
      series: [{ type: 'heatmap', data: Hm.data, itemStyle: { borderColor: K.css('--panel'), borderWidth: 1 }, progressive: 0, emphasis: { itemStyle: { borderColor: K.css('--fg'), borderWidth: 1 } } }],
    }));
    c.on('click', p => { if (p && p.value) ctx.openPosition(Hm.refs[p.value[1]], Hm.dates[p.value[0]]); });
    charts.push(c);
  }

  function drawList(ctx, box, ck, labels) {
    const { K, P, prep, state } = ctx, f = K.f, esc = K.esc, ui = state.ui, all = ck.findings || [];
    const list = ui.chk ? all.filter(x => x.check === ui.chk) : all, pos = new Map(prep.positions.map(p => [p.ref, p]));
    box.querySelector('#ckTitle').textContent = (ui.chk ? labels.get(ui.chk) + ': ' : 'Все находки: ') + list.length;
    const clr = box.querySelector('#ckClr');
    clr.innerHTML = ui.chk ? '<button class="k-btn" id="ckAll">Показать все</button>' : '';
    if (ui.chk) clr.querySelector('#ckAll').onclick = () => { ui.chk = null; draw(ctx, box, ck); };
    const NA = why => '<span class="mut3" title="' + esc(why || 'нет данных') + '">—</span>';
    const value = x => {
      const v = x.value, k = P.VALUE_KIND[x.check];
      if (typeof v !== 'number' || !isFinite(v) || !k) return NA();
      return k === 'rub' ? (Math.abs(v) < 1e4 ? f.n(v, 0) + ' <span class="mut3">RUB</span>' : f.mn(v, 2) + ' <span class="mut3">млн</span>') : k === 'price' ? f.px(v) + ' <span class="mut3">цена</span>' : k === 'share' ? f.pct(v * 100, 1) : f.n(v, 0) + ' <span class="mut3">шт.</span>';
    };
    const who = x => {
      if (x.ref) { const p = pos.get(x.ref); return '<button class="link" data-ref="' + esc(x.ref) + '" data-d="' + esc(x.date || '') + '" title="Открыть позицию на этот день (E)">' + esc(x.name || (p && p.name) || x.positionId) + '</button> <span class="mut3">' + esc(x.book || '') + '</span>'; }
      return x.check === 'fxrev' && x.name ? 'валюта ' + esc(x.name) + ' <span class="mut3">· весь портфель</span>' : '<span class="mut3">весь портфель</span>';
    };
    const el = box.querySelector('#ckList');
    el.innerHTML = list.length ? '<table class="k-tbl" data-vk="c19-period-findings" data-title="Находки проверок PnL за период"><thead><tr><th>Уровень</th><th>Проверка</th><th>Дата</th><th>Позиция</th><th class="n">Значение</th><th>Что видно</th></tr></thead><tbody>' +
      list.map(x => '<tr class="' + (x.sev === 'crit' ? 'critrow' : x.sev === 'warn' ? 'warnrow' : '') + '"><td>' + (P.SEV[x.sev] ? sevChip(P, x.sev) : esc(x.sev)) + '</td><td>' + esc(labels.get(x.check) || x.check) + '</td>' +
        '<td>' + (x.date ? f.date(x.date) : NA()) + (x.dateTo && x.dateTo !== x.date ? '–' + f.date(x.dateTo) : '') + '</td><td>' + who(x) + '</td><td class="n">' + value(x) + '</td><td class="txt">' + esc(x.text || '') + '</td></tr>').join('') + '</tbody></table>'
      : '<p class="k-empty">' + (ui.chk ? 'У этой проверки находок нет.' : 'Находок нет.') + '</p>';
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => ctx.openPosition(b.dataset.ref, b.dataset.d || null));
  }

  // «Необъяснённое» (TDM#436, TDM#442): other + reported of the period by reason first (its split by method and its
  // largest positions in the row's tip; a click filters the table), by method group second, by valuation quality; daily
  // stacked columns coloured by reason; the largest positions with reason, method, quality and notes; sums as /checks
  // unexplainedBy gives them
  function drawWhy(ctx, el, ck) {
    const { K, P, state } = ctx, f = K.f, esc = K.esc, ui = state.ui, V = P.unexplainedView(ck);
    const head = '<div class="hd"><h3>Необъяснённое: что внутри</h3><span class="sp"></span><span class="hint">' + UNIT + ' · «Необъяснённое / прочее» и «Разница PV по отчётам» за период: по причине, затем по методу расчёта</span></div>';
    if (!V) {
      el.innerHTML = '<div class="k-panel">' + head + '<div class="bd k-empty">' + (ck && ck.unexplainedBy ? 'нет данных: необъяснённое и разница PV по отчётам не сохранены ни в одной записи выборки' : 'нет данных: сервер не отдаёт разбивку необъяснённого по методу') + '</div></div>';
      return;
    }
    if (ui.whyReason && !V.reasons.some(r => r.key === ui.whyReason)) ui.whyReason = null;
    const col = g => P.colour(K, g.color), max = Math.max(1, ...V.groups.map(g => Math.abs(g.total || 0)), ...V.reasons.map(r => Math.abs(r.total || 0)));
    const NA = why => '<span class="mut3" title="' + esc(why || 'нет данных') + '">—</span>';
    const mn = v => (v == null ? NA() : f.mn(v, 2));
    const bar = g => '<span class="n">' + mn(g.total) + '</span><span class="bars' + (g.total < 0 ? ' l' : '') + '"><span class="bar" style="width:' + Math.max(2, Math.round(Math.abs(g.total || 0) / max * 92)) + 'px;background:' + col(g) + '"></span></span>';
    const rtip = r => [r.tip, ...(r.methods || []).map(m => (m.method || 'метод не указан') + ': ' + f.mn(m.total, 2) + ' млн, ' + m.rows + ' зап.'),
      ...(r.top || []).length ? ['крупнейшие: ' + r.top.map(t => (t.name || t.positionId) + ' ' + f.mn(t.total, 2)).join('; ')] : []].filter(Boolean).join('\n');
    const reasons = V.reasons.length ? '<h4>по причине</h4>' + V.reasons.map(r => '<button class="nm link why" data-reason="' + esc(r.key) + '" aria-pressed="' + (ui.whyReason === r.key) + '" title="' + esc(rtip(r)) + '"><span class="sw" style="background-color:' + col(r) + '"></span>' + esc(r.label) + ' <span class="mut3">· ' + r.rows + ' зап.</span></button>' + bar(r)).join('') : '';
    const bars = '<h4>' + (V.reasons.length ? 'по методу расчёта' : 'по методу расчёта (причины не записаны)') + '</h4>' + V.groups.map(g => '<span class="nm" title="' + esc(g.tip) + '"><span class="sw" style="background-color:' + col(g) + '"></span>' + esc(g.label) + ' <span class="mut3">· ' + g.rows + ' зап.</span></span>' + bar(g)).join('');
    const qual = V.quality.length ? '<h4>по качеству оценки D</h4>' + V.quality.map(q => '<span class="nm">' + esc(q.label) + ' <span class="mut3">· ' + q.rows + ' зап.</span></span><span class="n">' + mn(q.total) + '</span><span></span>').join('') : '';
    const gap = V.reasons.length ? V.reasonGap : V.gap;
    const sum = '<p class="hint" style="margin:8px 0 0">Итого ' + mn(V.total) + ' млн: необъяснённое ' + mn(V.other) + ', разница PV по отчётам ' + mn(V.reported) + '.' +
      (gap != null && Math.abs(gap) >= 1 ? ' <span class="sevtxt warn">Разбивка не сходится с итогом на ' + esc(f.n(gap, 0)) + ' RUB.</span>' : ' Разбивка сходится с итогом.') +
      (V.reasons.length ? ' Щелчок по причине — её позиции в таблице.' : '') + '</p>';
    el.innerHTML = '<div class="k-panel" id="ckWhyPanel">' + head + '<div class="bd"><div class="grid2e"><div><div class="contrib">' + reasons + bars + qual + '</div>' + sum + '</div><div class="ch short" id="ckWhyDays"></div></div></div>' +
      '<div class="bd flush tscroll" id="ckWhyTop"></div></div>';
    el.querySelectorAll('[data-reason]').forEach(b => b.onclick = () => { ui.whyReason = ui.whyReason === b.dataset.reason ? null : b.dataset.reason; drawWhy(ctx, el, ck); });
    drawWhyDays(ctx, el.querySelector('#ckWhyDays'), V);
    drawWhyTop(ctx, el.querySelector('#ckWhyTop'), V, NA, ui.whyReason);
  }

  function drawWhyDays(ctx, el, V) {
    const { K, P } = ctx, f = K.f, esc = K.esc;
    if (!V.dates.length) { el.outerHTML = '<div class="k-empty">нет дней с необъяснённым</div>'; return; }
    const tip = ps => '<b>' + esc(f.date(ps[0].axisValue)) + '</b> · ' + UNIT + '<table>' + ps.filter(p => p.value != null).map(p => '<tr><td>' + p.marker + esc(p.seriesName) + '</td><td style="text-align:right;padding-left:14px;font-variant-numeric:tabular-nums">' + f.mn(p.value, 2) + '</td></tr>').join('') + '</table>';
    charts.push(K.chart(el, () => ({
      grid: { left: 8, right: 12, top: 30, bottom: 8, containLabel: true }, legend: { top: 0, type: 'scroll' }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: tip },
      xAxis: { type: 'category', data: V.dates, axisTick: { show: false }, axisLabel: { formatter: d => d.slice(8, 10) + '.' + d.slice(5, 7), hideOverlap: true } },
      yAxis: { type: 'value', axisLabel: { formatter: v => K.tick(v, 1e6) } },
      series: (V.reasonSeries.length ? V.reasonSeries : V.series).map(s => ({ name: s.label, type: 'bar', stack: 'u', barMaxWidth: 26, color: P.colour(K, s.color), data: s.vals })),
    })));
  }

  function drawWhyTop(ctx, el, V, NA, only) {
    const { K } = ctx, f = K.f, esc = K.esc;
    const rows = only ? V.top.filter(t => (t.reasons || []).includes(only)) : V.top;
    if (!V.top.length) { el.innerHTML = ''; return; }
    const cut = s => (s.length > 70 ? s.slice(0, 69) + '…' : s);  // the full text in the cell's title
    const reason = t => [...(t.noPnl || []).map(x => 'нет PnL: ' + x.text + (x.days > 1 ? ' (дней: ' + x.days + ')' : '')), ...(t.reasonDetails || []), ...(t.notes || [])];
    const sw = c => '<span class="sw" style="background-color:' + ctx.P.colour(K, c) + '"></span>';
    const lbl = only ? (V.reasons.find(r => r.key === only) || {}).label : null;
    el.innerHTML = (rows.length ? '<table class="k-tbl" data-vk="c19-period-unexpl-top" data-title="Крупнейшие позиции по необъяснённому, ' + UNIT + '"><thead><tr><th class="stick">Позиция</th><th>Книга</th><th class="n">Необъяснённое + отчёты</th><th>Причина</th><th>Метод</th><th>Качество оценки</th><th>Примечание</th></tr></thead><tbody>' +
      rows.map(t => '<tr><td class="stick"><button class="link" data-ref="' + esc(t.ref) + '" title="Открыть позицию (E)">' + esc(t.name || t.positionId) + '</button></td><td>' + esc(t.book) + '</td><td class="n">' + (t.total == null ? NA() : f.mn(t.total, 2)) + '</td>' +
        '<td>' + (t.reasonLabel ? sw(t.reasonColor) + esc(t.reasonLabel) + ((t.reasons || []).length > 1 ? ' <span class="mut3">и ещё ' + (t.reasons.length - 1) + '</span>' : '') : '<span class="mut3">—</span>') + '</td>' +
        '<td>' + sw(t.color) + esc(t.groupLabel) + ' <span class="mut3">' + esc((t.methods || []).map(m => m || 'метод не указан').join(', ')) + '</span></td>' +
        '<td>' + esc((t.quality || []).map(V.qualityLabel).join(', ')) + '</td><td class="txt" title="' + esc(reason(t).join(' · ')) + '">' + (reason(t).length ? esc(cut(reason(t).join(' · '))) : '<span class="mut3">—</span>') + '</td></tr>').join('') + '</tbody></table>'
      : '<p class="k-empty">Среди крупнейших позиций нет позиций с причиной «' + esc(lbl || only) + '».</p>') +
      '<p class="hint" style="padding:0 12px">' + (only ? 'Только причина «' + esc(lbl || only) + '». ' : '') + (V.topTotal > V.top.length ? 'Показаны ' + V.top.length + ' из ' + V.topTotal + ' позиций с наибольшим |необъяснённым| за период. ' : '') + 'Причина и метод — с наибольшей суммой у позиции; щелчок — позиция на экране E.</p>';
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => ctx.openPosition(b.dataset.ref));
  }
})();
