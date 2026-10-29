/* C19 «PnL за период», экран D · Ставки по кривым (TDM#409): /api/v1/history/pnl/curves — сдвиг кривой, DV01 средний и
   PnL ставок линейно по срокам; накопленный PnL ставок (/period ratesByFactor, иначе ratesByCurve); линейная против
   точной; позиции. По умолчанию — факторы (TDM#437): RUONIA, базис ОФЗ (КБД − RUONIA), КБД на датах без RUONIA; вид
   «Кривые» — прежний, по кривым. Numbers come from PNL_PERIOD (lib/pnl-period.js); missing values stay «—», never 0. */
(function () {
  'use strict';
  const SCREENS = window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
  const UNIT = 'млн RUB', PATH = '/api/v1/history/pnl/curves';
  let charts = [], cur = null;  // cur — the mounted render: {ctx, main, key}; null after dispose
  // the latest request wins (PNL_HISTORY.loader); a result of a disposed render is dropped
  const load = PNL_HISTORY.loader((q, signal) => cur.ctx.fetchJson(PATH, q, signal), r => {
    if (!cur) return;
    const box = cur.main.querySelector('#cvBody');
    if (!box) return;
    if (r.loading) box.innerHTML = '<div class="k-panel"><div class="bd k-empty">Загружаю кривые периода…</div></div>';
    else if (r.error) box.innerHTML = '<div class="k-panel"><div class="bd k-empty">' + cur.ctx.K.esc(r.error.message) + '</div></div>';
    else { remember(cur.ctx.state.ui, cur.key, r.data); draw(cur.ctx, box, r.data); }
  });
  // answers of this page's slices: a re-render (theme, format, tab) does not ask again; the key holds period and filters
  function remember(ui, key, v) {
    const c = ui.cache || (ui.cache = new Map());
    if (v === undefined) return c.get(key);
    c.set(key, v); if (c.size > 40) c.delete(c.keys().next().value);
    return v;
  }
  function dispose() { cur = null; load.invalidate(); charts.forEach(c => c.dispose()); charts = []; }

  SCREENS.curves = {
    render(main, ctx) {
      const { K, prep, state } = ctx;
      main.innerHTML = lede(K, 'curves') + '<div id="cvBody"></div>';
      const box = main.querySelector('#cvBody');
      if (!prep.summary.rows) { box.innerHTML = '<div class="k-panel"><div class="bd k-empty">В выбранном периоде и фильтре нет сохранённых записей PnL. Расширьте период или верните книги.</div></div>'; return; }
      const q = ctx.query(), key = PATH + '?' + q;
      cur = { ctx, main, key };
      const hit = remember(state.ui, key);
      if (hit) draw(ctx, box, hit); else load(q);
    },
    dispose,
  };

  // the tab's own title and description (the shell's tabs)
  function lede(K, key) {
    const b = document.querySelector('#tabs [data-tab=' + key + ']');
    const t = b ? b.textContent.replace(/^[A-F]/, '') : '', l = b ? b.querySelector('b').textContent : '';
    return '<div class="lede"><h1>' + K.esc(l + ' · ' + t) + '</h1><p>' + K.esc(b ? b.title : '') + '</p></div>';
  }

  function draw(ctx, box, cv) {
    const { K, P, state } = ctx, f = K.f, esc = K.esc, ui = state.ui;
    charts.forEach(c => c.dispose()); charts = [];
    const NA = why => '<span class="mut3" title="' + esc(why || 'нет данных') + '">—</span>';
    const rgb = [K.rgbOf(K.css('--pos')) || [63, 182, 122], K.rgbOf(K.css('--neg')) || [240, 100, 90]];
    // factors when the server gives them (TDM#437), curves on request or from an older server
    const canF = Array.isArray(cv.factors), fac = canF && ui.cvView !== 'curves';
    const C = P.curveGrids(cv), G = fac ? P.factorGrids(cv) : C, B = G.buckets, dmy = d => (d ? f.date(d) : '?');
    const what = fac ? 'Фактор' : 'Кривая';
    const label = r => (fac && r.id === 'kbd' && r.cover ? esc(r.name) + ' <span class="mut3">· ' + esc(r.cover) + '</span>' : esc(r.name));
    const mn2 = v => (v != null && Math.abs(v) < 5e3 ? '<span class="mut3" title="' + esc(f.n(v, 0)) + ' RUB">·</span>' : f.mn(v, 2));
    // one curve × term table; good — the sign that reads as green: a fall of rates, a long DV01 (< 0), a gain
    const table = (vk, title, unit, g, fmt, good, sum, empty) => '<div class="k-panel"><div class="hd"><h3>' + title + '</h3><span class="sp"></span><span class="hint">' + esc(unit) + '</span></div>' +
      '<div class="bd flush tscroll">' + (!g.rows.length ? '<div class="k-empty">' + esc(empty) + '</div>' :
        '<table class="k-tbl hm" data-vk="' + vk + (fac ? '-f' : '') + '" data-title="' + esc(title + ', ' + unit) + '"><thead><tr><th class="stick">' + what + '</th>' + B.map(b => '<th class="n">' + esc(b) + '</th>').join('') +
        (g.rest ? '<th class="n" title="Записи без разбивки по срокам: фактор одной суммой">без сроков</th>' : '') + (sum ? '<th class="n">Σ</th>' : '') + '</tr></thead><tbody>' +
        g.rows.map(r => '<tr><td class="stick"' + (r.tip || r.cover ? ' title="' + esc([r.tip, r.cover].filter(Boolean).join(' · ')) + '"' : '') + '>' + label(r) + '</td>' + r.vals.map(v => '<td class="n"' + (v ? ' style="' + P.tint(v, g.max, good, rgb) + '"' : '') + '>' + (v == null ? NA() : fmt(v)) + '</td>').join('') +
          (g.rest ? '<td class="n">' + (r.rest == null ? NA('у фактора всё по срокам') : fmt(r.rest)) + '</td>' : '') +
          (sum ? '<td class="n"><b>' + (r.sum == null ? NA() : fmt(r.sum)) + '</b></td>' : '') + '</tr>').join('') + '</tbody></table>') + '</div></div>';
    const first = cv.firstRun, last = cv.lastRun;
    const linNote = C.linearCover === 'нет данных'
      ? 'нет данных: линейный PnL по срокам сохраняется только с прогонов после TDM#406 (pnl.extra.rates_linear_curves) — в выборке 0 из ' + C.coverage.rows + ' записей'
      : 'нет данных по срокам';
    if (!(cv.curves || []).length) {
      box.innerHTML = '<div class="k-panel"><div class="bd k-empty">У позиций выборки нет ни риска по кривым, ни кривой дисконтирования.</div></div>';
      return;
    }
    const view = canF ? '<div class="legendrow" style="margin:0 0 10px"><span id="cvView"></span><span class="hint">' + esc(fac ? 'RUONIA — позиции с дисконтированием по RUONIA и RUONIA-часть ОФЗ; базис ОФЗ — член КБД − член RUONIA; КБД — даты, где оценка только по КБД (нет RUONIA). Сумма факторов равна сумме по кривым.' : 'Кривые дисконтирования и риска позиций, как в прогоне.') + '</span></div>' : '';
    box.innerHTML = view + '<div class="grids3">' +
      table('c19-period-curve-move', fac ? 'Сдвиг фактора за период' : 'Сдвиг кривой за период', 'bp, ' + (first ? dmy(first.prevDate) + ' → ' + dmy(last && last.date) : 'прогонов нет'), G.move, v => P.signedBp(v), -1, false, 'нет точек кривых в прогонах периода') +
      table('c19-period-curve-dv01', 'DV01 портфеля, среднее', 'тыс RUB / bp · по прогонам с записями', G.dv01, v => f.dv(v), -1, true, 'нет риска по срокам у позиций выборки') +
      table('c19-period-curve-linear', 'PnL ставок, линейно', UNIT + (C.linearCover && C.linearCover !== 'нет данных' ? ' · по ' + C.linearCover : '') + ' · Σ DV01 × сдвиг по дням', G.linear, mn2, 1, true, linNote) + '</div>' +
      '<p class="hint">Проверка на глаз: длинная позиция по ставкам (DV01 &lt; 0, зелёный) и снижение ставок (зелёный) дают прибыль (зелёный). Если цвета в строке не согласуются — смотрите знак DV01 или сдвиг кривой. PnL считается по дням, поэтому не равен произведению средних.</p>' +
      '<div class="grid2e"><div class="k-panel"><div class="hd"><h3>Накопленный PnL ставок по ' + (fac ? 'факторам' : 'кривым') + '</h3><span class="sp"></span><span class="hint">' + (fac ? 'точная переоценка; у ОФЗ RUONIA — линейный член RUONIA, базис — остальное' : 'точная переоценка, отнесённая к кривой дисконтирования позиции') + '</span></div><div class="bd"><div class="ch" id="cvCum"></div></div></div>' +
      '<div class="k-panel"><div class="hd"><h3>Линейная оценка против точной переоценки</h3></div><div class="bd flush tscroll" id="cvLin"></div></div></div>' +
      '<div class="k-panel"><div class="hd"><h3>Позиции по ' + (fac ? 'факторам' : 'кривым') + '</h3><span class="sp"></span><span class="hint">' + UNIT + ' · ставки, точно</span></div><div class="bd flush tscroll" id="cvPos"></div></div>';
    if (canF) K.seg(box.querySelector('#cvView'), [['factors', 'Факторы'], ['curves', 'Кривые']], fac ? 'factors' : 'curves', v => { ui.cvView = v; draw(ctx, box, cv); });
    drawCum(ctx, box.querySelector('#cvCum'), fac);
    drawLin(ctx, box.querySelector('#cvLin'), cv, NA, fac);
    drawPositions(ctx, box.querySelector('#cvPos'), NA, fac);
  }

  function drawCum(ctx, el, fac) {
    const { K, P, prep } = ctx, f = K.f, esc = K.esc, S = fac && P.hasFactors(prep) ? P.factorSeries(prep) : P.curveSeries(prep);
    if (!S.length) { el.outerHTML = '<div class="k-empty">нет данных: в днях периода нет ставок по ' + (fac ? 'факторам' : 'кривым') + '</div>'; return; }
    const shade = (s, i) => (s.shade != null ? s.shade : s.id ? [1, 0.62, 0.4, 0.26][i % 4] : 0.2);  // the rates family, as «Explain по кривым» on A; dashes tell them apart
    const tip = ps => '<b>' + esc(ps[0].axisValueLabel) + '</b> · ' + UNIT + '<table>' + ps.filter(p => p.value != null).map(p => '<tr><td>' + p.marker + esc(p.seriesName) + '</td><td style="text-align:right;padding-left:14px;font-variant-numeric:tabular-nums">' + f.mn(p.value, 2) + '</td></tr>').join('') + '</table>';
    charts.push(K.chart(el, () => ({
      grid: { left: 8, right: 64, top: 40, bottom: 8, containLabel: true }, legend: { top: 0, type: 'scroll' }, tooltip: { trigger: 'axis', formatter: tip },
      xAxis: { type: 'category', data: prep.dates, axisTick: { show: false }, axisLabel: { formatter: d => d.slice(8, 10) + '.' + d.slice(5, 7), hideOverlap: true } },
      yAxis: { type: 'value', unit: UNIT + ', накопленно', axisLabel: { formatter: v => K.tick(v, 1e6) } },
      series: S.map((s, i) => ({ name: s.name, type: 'line', showSymbol: false, connectNulls: false, color: P.colour(K, 'pnl:rates', shade(s, i)), lineStyle: { width: 2, type: s.id ? ['solid', 'dashed', 'dotted'][i % 3] : 'dotted' }, data: s.cum,
        endLabel: { show: true, formatter: p => f.mn(p.value, 1), color: K.css('--fg-2'), fontSize: 11 } })),
    })));
  }

  function drawLin(ctx, el, cv, NA, fac) {
    const { K, P, prep } = ctx, f = K.f, esc = K.esc, rows = fac ? P.factorLinVsExact(cv) : P.linearVsExact(cv, prep.curveName), cov = cv.coverage || {};
    const mn = v => (v == null ? NA() : f.mn(v, 2));
    const covNote = !cov.rows ? '' : cov.linearRows === cov.rows ? '' : !cov.linearRows ? 'Линейной оценки нет ни в одной записи выборки — нет данных.' :
      'Линейная оценка есть у ' + cov.linearRows + ' из ' + cov.rows + ' записей: разница включает записи без неё, предупреждение не ставится.';
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-curve-linexact' + (fac ? '-f' : '') + '" data-title="Линейная оценка против точной, ' + UNIT + '"><thead><tr><th class="stick">' + (fac ? 'Фактор' : 'Кривая') + '</th><th class="n">DV01 × сдвиг</th><th class="n">Точная</th><th class="n">Разница</th><th class="n">%</th></tr></thead><tbody>' +
      rows.map(r => '<tr class="' + (r.warn ? 'warnrow' : '') + '"><td class="stick">' + esc(r.name) + '</td><td class="n">' + mn(r.linear) + '</td><td class="n">' + mn(r.exact) + '</td><td class="n">' + mn(r.diff) + '</td><td class="n">' +
        (r.pct == null ? NA(r.linear === 0 ? 'линейная оценка 0' : 'нет данных') : f.n(r.pct, 1)) + (r.warn ? ' <span class="sevtxt warn" title="|разница| > ' + P.LIN_WARN_PCT + '% и > ' + f.mn(P.LIN_WARN_ABS, 0) + ' млн RUB">▲</span>' : '') + '</td></tr>').join('') + '</tbody></table>' +
      '<p class="hint" style="padding:0 12px">Разница — выпуклость и нелинейность' + (fac ? ' (у ОФЗ вся в базисе: RUONIA-часть ОФЗ в точной равна линейной)' : '') + '. ▲ — больше ' + P.LIN_WARN_PCT + '% линейной и больше ' + f.mn(P.LIN_WARN_ABS, 0) + ' млн RUB: повод проверить расчёт (экран F, «Ставки: линейно и точно»). ' + esc(covNote) + '</p>';
  }

  function drawPositions(ctx, el, NA, fac) {
    const { K, P, prep } = ctx, f = K.f, esc = K.esc;
    if (fac && P.hasFactors(prep)) { drawFactorPositions(ctx, el, NA); return; }
    const rows = P.curvePositions(prep);
    if (!rows.length) { el.innerHTML = '<div class="k-empty">нет данных: у позиций выборки нет ставок</div>'; return; }
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-curve-positions" data-title="Позиции по кривым, ' + UNIT + '"><thead><tr><th class="stick">Позиция</th><th>Книга</th><th>Кривая</th><th class="n">Ставки, точно</th><th class="n">Доля кривой, %</th></tr></thead><tbody>' +
      rows.map(r => '<tr><td class="stick"><button class="link" data-ref="' + esc(r.ref) + '" title="Открыть позицию (E)">' + esc(r.p.name || r.p.positionId) + '</button>' + (r.p.closed ? ' <span class="mut3">закрыта</span>' : '') + '</td><td>' + esc(r.p.book) + '</td><td class="mut">' + esc(prep.curveName(r.curve)) + '</td>' +
        '<td class="n">' + f.mn(r.rates, 2) + '</td><td class="n">' + (r.share == null ? NA() : f.n(r.share, 1)) + '</td></tr>').join('') + '</tbody></table>' +
      '<p class="hint" style="padding:0 12px">Разбивки позиции по срокам и её линейной оценки в ответе сервера нет (только суммы по кривым) — по срокам см. таблицы выше. Позиция со ставками без кривой дисконтирования — «' + esc(P.NO_CURVE) + '».</p>';
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => ctx.openPosition(b.dataset.ref));
  }

  // positions by factor: an OFZ in RUONIA + basis is two rows (its RUONIA part and its basis)
  function drawFactorPositions(ctx, el, NA) {
    const { K, P } = ctx, f = K.f, esc = K.esc, rows = P.factorPositions(ctx.prep);
    if (!rows.length) { el.innerHTML = '<div class="k-empty">нет данных: у позиций выборки нет ставок</div>'; return; }
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-factor-positions" data-title="Позиции по факторам, ' + UNIT + '"><thead><tr><th class="stick">Позиция</th><th>Книга</th><th>Фактор</th><th class="n">Ставки, точно</th><th class="n">Доля фактора, %</th></tr></thead><tbody>' +
      rows.map(r => '<tr><td class="stick"><button class="link" data-ref="' + esc(r.ref) + '" title="Открыть позицию (E)">' + esc(r.p.name || r.p.positionId) + '</button>' + (r.p.closed ? ' <span class="mut3">закрыта</span>' : '') + '</td><td>' + esc(r.p.book) + '</td><td class="mut" title="' + esc(P.FACTOR_TIPS[r.factor] || '') + '">' + esc(r.name) + '</td>' +
        '<td class="n">' + f.mn(r.rates, 2) + '</td><td class="n">' + (r.share == null ? NA() : f.n(r.share, 1)) + '</td></tr>').join('') + '</tbody></table>' +
      '<p class="hint" style="padding:0 12px">ОФЗ в модели RUONIA + базис — двумя строками: RUONIA-часть (линейный член RUONIA) и базис (остальное, с нелинейностью). Позиции на датах без RUONIA — «' + esc(P.FACTOR_NAMES.kbd) + '».</p>';
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => ctx.openPosition(b.dataset.ref));
  }
})();
