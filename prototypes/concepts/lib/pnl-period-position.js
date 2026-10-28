/* C19 «PnL за период», экран E · Позиция (TDM#409): одна позиция за период из /api/v1/history/pnl/position — список
   позиций выборки (отметка ▲ — находки /checks выше «инфо»), шапка, плитки, атрибуция «цена / купон», накопление,
   чистая цена и доходность отдельными графиками, дни с входными данными D−1 / D, событиями и находками. Numbers come
   from PNL_PERIOD (lib/pnl-period.js); missing values stay «—», never 0. */
(function () {
  'use strict';
  const SCREENS = window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
  const UNIT = 'млн RUB', POS = '/api/v1/history/pnl/position', CHECKS = '/api/v1/history/pnl/checks';
  // cur — the mounted render: {ctx, main, ref, key, ckKey, resp, ck, ckError}; null after dispose
  let charts = [], cur = null;
  const loadPos = PNL_HISTORY.loader((q, signal) => cur.ctx.fetchJson(POS, q, signal), r => {
    if (!cur) return;
    if (r.loading) return body('<div class="k-panel"><div class="bd k-empty">Загружаю дни позиции…</div></div>');
    if (r.error) return body('<div class="k-panel"><div class="bd k-empty">' + cur.ctx.K.esc(r.error.message) + '</div></div>');
    cur.resp = remember(cur.ctx.state.ui, cur.key, r.data); drawPosition();
  });
  // findings for the ▲ of the list and the rows of the table: the thresholds of F (state.ui)
  const loadCk = PNL_HISTORY.loader((q, signal) => cur.ctx.fetchJson(CHECKS, q, signal), r => {
    if (!cur || r.loading) return;
    if (r.error) cur.ckError = r.error.message; else cur.ck = remember(cur.ctx.state.ui, cur.ckKey, r.data);
    paintList(); if (cur.resp) drawPosition();
  });
  function remember(ui, key, v) {
    const c = ui.cache || (ui.cache = new Map());
    if (v === undefined) return c.get(key);
    c.set(key, v); if (c.size > 40) c.delete(c.keys().next().value);
    return v;
  }
  const disposeCharts = () => { charts.forEach(c => c.dispose()); charts = []; };
  function dispose() { cur = null; loadPos.invalidate(); loadCk.invalidate(); disposeCharts(); }
  const body = html => { disposeCharts(); const el = cur && cur.main.querySelector('#pBody'); if (el) el.innerHTML = html; };

  SCREENS.position = {
    render(main, ctx) {
      const { K, P, prep, state } = ctx, ui = state.ui;
      main.innerHTML = lede(K, 'position');
      if (!prep.positions.length && !state.position) { main.innerHTML += '<div class="k-panel"><div class="bd k-empty">В выбранном периоде и фильтре нет позиций с записями PnL. Расширьте период или верните книги.</div></div>'; return; }
      // the position: the one opened (A–D, F) or the largest of the selection; a new position drops the marked day
      const biggest = prep.positions.slice().sort((a, b) => Math.abs(b.total || 0) - Math.abs(a.total || 0))[0];
      const ref = state.position || biggest.ref;
      state.position = ref;
      if (ui.pref !== ref) { ui.pref = ref; ui.pday = null; }
      let scroll = false;
      if (state.positionDate) { ui.pday = state.positionDate; state.positionDate = null; scroll = true; }
      const [book, ...rest] = ref.split('|'), q = ctx.query({ position_book: book, position_id: rest.join('|') }), cq = ctx.query(P.thrQuery(ui));
      cur = { ctx, main, ref, key: POS + '?' + q, ckKey: CHECKS + '?' + cq, resp: null, ck: null, ckError: null, scroll };
      main.insertAdjacentHTML('beforeend', '<div class="pside"><div class="k-panel"><div class="hd"><h3>Позиции</h3><span class="sp"></span><span class="hint">итог периода, млн</span></div><div class="bd flush plist" id="plist"></div></div>' +
        '<div class="pmain"><div class="k-panel"><div class="bd" id="pHead"></div></div><div class="pmain" id="pBody"></div></div></div>');
      cur.ck = remember(ui, cur.ckKey) || null;
      paintList(); paintHead(null);
      const hit = remember(ui, cur.key);
      if (hit) { cur.resp = hit; drawPosition(); } else loadPos(q);
      if (!cur.ck) loadCk(cq);
    },
    dispose,
  };

  function lede(K, key) {
    const b = document.querySelector('#tabs [data-tab=' + key + ']');
    const t = b ? b.textContent.replace(/^[A-F]/, '') : '', l = b ? b.querySelector('b').textContent : '';
    return '<div class="lede"><h1>' + K.esc(l + ' · ' + t) + '</h1><p>' + K.esc(b ? b.title : '') + '</p></div>';
  }
  const idx = () => cur.ctx.P.findingsIndex(cur.ck ? cur.ck.findings : []);
  const kindLabel = p => { const M = window.MOCK || {}; return ((M.groupLabel || {})[p.group] || p.group || '—') + ' · ' + ((M.kindLabel || {})[p.kind] || p.kind || '—'); };

  function paintList() {
    const { K, P, prep } = cur.ctx, f = K.f, esc = K.esc, el = cur.main.querySelector('#plist');
    if (!el) return;
    const groups = P.positionList(prep.positions, cur.ck ? idx().flagged : null);
    el.innerHTML = groups.map(g => '<div class="g">' + esc(g.book) + '</div>' + g.items.map(x => '<button data-ref="' + esc(x.ref) + '" aria-current="' + (x.ref === cur.ref) + '">' +
      '<span class="nm">' + (x.flag ? '<span class="sevtxt warn" title="есть находки проверок (экран F)">▲ </span>' : '') + esc(x.p.name || x.p.positionId) + '</span>' +
      '<span class="n">' + (x.total == null ? '<span class="mut3">—</span>' : f.mn(x.total, 1)) + '</span><span class="sub">' + esc(kindLabel(x.p) + ' · ' + (x.p.ccy || '—')) + (x.p.closed ? ' · закрыта' : '') + '</span></button>').join('')).join('') +
      (cur.ckError ? '<p class="hint" style="padding:6px 12px">Проверки не загрузились: ' + esc(cur.ckError) + '</p>' : !cur.ck ? '<p class="hint" style="padding:6px 12px">Загружаю проверки…</p>' : '');
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => cur.ctx.openPosition(b.dataset.ref));
    const on = el.querySelector('[aria-current=true]');
    if (on && on.scrollIntoView && el.scrollHeight > el.clientHeight) el.scrollTop = Math.max(0, on.offsetTop - el.clientHeight / 2);
  }

  // header: the selection's attributes of the position (Pos), else the /position identity
  function paintHead(resp) {
    const { K, P, prep, period } = cur.ctx, f = K.f, esc = K.esc, el = cur.main.querySelector('#pHead');
    if (!el) return;
    const sel = prep.positions.find(p => p.ref === cur.ref) || null, p = sel || (resp && resp.position) || null;
    if (!p) { el.innerHTML = '<div class="phead"><h2>' + esc(cur.ref.replace('|', ' · ')) + '</h2></div>'; return; }
    const firstRun = (period.runs || [])[0], tags = [];
    if (sel) tags.push(sel.closed ? '<span class="st info" title="нет записи на последнюю дату периода с прогоном">закрыта · последняя запись ' + f.date(sel.lastDate) + '</span>' : '<span class="st ok">открыта</span>');
    if (sel && firstRun && sel.firstDate > firstRun.date) tags.push('<span class="st info">появилась ' + f.date(sel.firstDate) + '</span>');
    const other = P.otherBooks(prep.positions, p);
    if (other.length) tags.push('<span class="st info" title="Тот же инструмент в другой книге; истории не склеиваются">есть в другой книге: ' + esc(other.join(', ')) + '</span>');
    el.innerHTML = '<div class="phead"><h2>' + esc(p.name || p.positionId) + '</h2><span>инструмент <b>' + esc(p.instrument || '—') + '</b></span><span>книга <b>' + esc(p.book) + '</b></span>' +
      '<span>' + esc(kindLabel(p)) + ' · ' + esc(p.ccy || '—') + '</span><span>кривая ' + esc(prep.curveName(p.curve || '')) + '</span><span>' + (p.rating ? 'рейтинг ' + esc(p.rating) : P.NO_RATING) + '</span>' +
      '<span class="tags">' + tags.join('') + '</span></div>';
  }

  function drawPosition() {
    const { K, P, prep, state } = cur.ctx, f = K.f, esc = K.esc, resp = cur.resp, ui = state.ui;
    paintHead(resp);
    if (!resp.position || !(resp.days || []).length) {
      const sel = prep.positions.find(p => p.ref === cur.ref);
      return body('<div class="k-panel"><div class="bd k-empty">В выбранном периоде и фильтре у позиции нет записей PnL' + (sel ? '' : ' (её нет в выборке)') + '. История за другие даты сохранена.</div></div>');
    }
    disposeCharts();
    const days = resp.days.slice().sort((a, b) => (a.date < b.date ? -1 : 1)), agg = P.sumAggs(days), I = idx(), ccy = resp.position.ccy;
    const NA = why => '<span class="mut3" title="' + esc(why || 'нет данных') + '">—</span>', mn = v => (v == null ? NA() : f.mn(v, 1)), mn2 = v => (v == null ? NA() : f.mn(v, 2));
    const kpi = (label, v, sub, cls, tip) => K.kpi(label, mn(v) + K.kpiUnit(UNIT), sub, '', tip).replace('class="k-kpi"', 'class="k-kpi' + (cls ? ' ' + cls : '') + '"');
    const c = k => P.val(agg, k), own = I.of(cur.ref, ccy).filter(x => x.sev !== 'info'), rows = days.map(P.dayRow);
    const lin = P.sumAvail(rows.map(r => r.ratesLinear)), dz = P.sumAvail(rows.map(r => r.dz)), noPnl = days.filter(d => d.noPnl).length;
    const cov = t => (t ? ' · ' + t : '');
    const html = '<div class="k-kpis">' +
      kpi('Итог за период', agg.total, days.length + ' прогонов' + (noPnl ? ' · без PnL ' + noPnl : ''), 'main') +
      kpi('Кэрри нетто', P.sumAvail([c('carry'), c('rolldown'), c('funding')]), 'брутто ' + mn(P.sumAvail([c('carry'), c('rolldown')])) + ' · фонд. ' + mn(c('funding')) + cov(P.coverText(agg, ['carry', 'rolldown', 'funding']))) +
      kpi('Ставки', c('rates'), lin == null ? 'линейно — нет данных' : 'линейно ' + f.mn(lin, 1)) +
      kpi('Спред', c('spread'), dz == null ? 'Δ спреда — нет данных' : 'Δ спреда ' + P.signedBp(dz) + ' bp') +
      kpi('FX (нетто ОВП)', P.srcVal(agg, 'fx'), (ccy === 'RUB' ? 'рублёвая позиция' : 'нетто открытой валютной позиции') + cov(P.coverText(agg, ['fx_gross', 'fx_funding'])), '', P.BY_KEY.fx.d) +
      kpi('Необъяснённое', c('other'), !cur.ck ? (cur.ckError ? 'проверки не загрузились' : 'проверки загружаются…') : own.length ? 'находок проверок: ' + own.length : 'находок нет', own.length ? 'flag' : '') + '</div>' +
      '<div class="k-panel"><div class="hd"><h3>Атрибуция: цена или купон</h3><span class="sp"></span><span class="hint">тот же итог, разложенный по источнику денег</span></div><div class="bd" id="pAttr"></div></div>' +
      '<div class="k-panel"><div class="hd"><h3>PnL позиции</h3><span class="sp"></span><span id="pmSeg"></span></div><div class="bd"><div class="ch" id="pCh"></div></div></div>' +
      '<div class="grid2e"><div class="k-panel"><div class="hd"><h3>Чистая цена</h3><span class="sp"></span><span class="hint">% от номинала, на дату прогона</span></div><div class="bd"><div class="ch short" id="pPrice"></div></div></div>' +
      '<div class="k-panel"><div class="hd"><h3>Доходность</h3><span class="sp"></span><span class="hint">% годовых, на дату прогона</span></div><div class="bd"><div class="ch short" id="pYld"></div></div></div></div>' +
      '<div class="k-panel"><div class="hd"><h3>Переоценка по дням</h3><span class="sp"></span><span class="hint">' + UNIT + ' · полоса слева — находки проверок · щелчок по строке отмечает день на графиках</span></div><div class="bd flush tscroll" style="max-height:560px" id="pDays"></div></div>';
    const el = cur.main.querySelector('#pBody');
    el.innerHTML = html;
    drawAttr(el.querySelector('#pAttr'), agg);
    if (!['cum', 'per'].includes(ui.pmode)) ui.pmode = 'cum';
    K.seg(el.querySelector('#pmSeg'), [['cum', 'Накопленно'], ['per', 'По дням']], ui.pmode, v => { ui.pmode = v; drawPosition(); });
    const dates = [...new Set(prep.dates.concat(days.map(d => d.date)))].sort(), byDate = new Map(days.map(d => [d.date, d]));
    drawPnl(el.querySelector('#pCh'), agg, dates, byDate);
    drawLine(el.querySelector('#pPrice'), dates, byDate, 'Чистая цена', d => d.inputs && d.inputs.clean, v => f.px(v), 'pal:9', 'нет данных: у позиции нет чистой цены (сделка, своп)');
    drawLine(el.querySelector('#pYld'), dates, byDate, 'Доходность', d => d.inputs && d.inputs.ytm, v => f.n(v, 2) + '%', 'pnl:rates', 'нет данных: у позиции нет доходности');
    drawDays(el.querySelector('#pDays'), rows, I, ccy, NA, mn2);
    if (cur.scroll && ui.pday) {
      cur.scroll = false;
      const tr = el.querySelector('#pDays tr[data-d="' + ui.pday + '"]');
      if (tr) tr.scrollIntoView({ block: 'center' });
    }
  }

  // price / coupon: the attribution of P.attrDefs on the position's days; without the coupon of every day — «нет данных»
  function drawAttr(el, agg) {
    const { K, P } = cur.ctx, f = K.f, esc = K.esc;
    if (!P.hasCoupon(agg)) { el.innerHTML = '<span class="mut">нет данных: нет разбивки купона (carry_coupon)' + (P.coverText(agg, 'carry_coupon') !== 'нет данных' ? ' — есть у ' + esc(P.coverText(agg, 'carry_coupon')) : '') + '. Без неё цена включала бы купон.</span>'; return; }
    const items = P.attrDefs().map(d => ({ d, v: d.v(agg) })).filter(x => x.v != null && Math.abs(x.v) >= 0.5), tot = items.reduce((s, x) => s + Math.abs(x.v), 0) || 1;
    const col = d => P.colour(K, d.color, d.shade), sw = d => '<span class="sw" style="background-color:' + col(d) + '"></span>';
    el.innerHTML = '<div class="attrbar">' + items.map(x => '<div title="' + esc(x.d.l + ': ' + f.mn(x.v, 2) + ' млн') + '" style="flex:' + (Math.abs(x.v) / tot).toFixed(4) + ';background-color:' + col(x.d) + (x.v < 0 ? ';background-image:repeating-linear-gradient(135deg,transparent 0 3px,rgba(0,0,0,.35) 3px 5px)' : '') + '"></div>').join('') + '</div>' +
      '<div class="legendrow">' + items.map(x => '<span title="' + esc(x.d.d || '') + '">' + sw(x.d) + esc(x.d.l) + ' <b class="k-num">' + f.mn(x.v, 2) + '</b></span>').join('') + '<span class="mut3">ширина — доля по модулю, штриховка — отрицательный вклад</span></div>';
  }

  const markDay = () => { const ui = cur.ctx.state.ui; return ui.pday ? { symbol: 'none', silent: true, lineStyle: { color: cur.ctx.K.css('--accent'), width: 1.5, type: 'solid' }, label: { show: false }, data: [{ xAxis: ui.pday }] } : undefined; };
  const xCat = dates => ({ type: 'category', data: dates, axisTick: { show: false }, axisLabel: { formatter: d => d.slice(8, 10) + '.' + d.slice(5, 7), hideOverlap: true } });
  function tipOf(K, unit, fmt) {
    return ps => '<b>' + K.esc(ps[0].axisValueLabel) + '</b>' + (unit ? ' · ' + unit : '') + '<table>' + ps.filter(p => p.value != null).map(p => '<tr><td>' + p.marker + K.esc(p.seriesName) + '</td><td style="text-align:right;padding-left:14px;font-variant-numeric:tabular-nums">' + fmt(p.value) + '</td></tr>').join('') + '</table>';
  }
  function drawPnl(el, agg, dates, byDate) {
    const { K, P, state } = cur.ctx, f = K.f, ui = state.ui;
    const defs = P.componentRows('explain', { carry: 'comp', split: P.hasSplit(agg), keys: Object.keys(agg.c || {}) })
      .filter(d => d.k !== 'gap' && d.v(agg) != null && (Math.abs(d.v(agg)) > 1e3 || d.k === 'other'));
    const vals = fn => dates.map(x => (byDate.has(x) ? fn(byDate.get(x)) : null)), totals = vals(d => (d.total == null ? null : d.total));
    const col = d => P.colour(K, d.color, d.shade);
    charts.push(K.chart(el, () => {
      const fg = K.css('--fg');
      if (ui.pmode === 'cum') return {
        grid: { left: 8, right: 64, top: 40, bottom: 8, containLabel: true }, legend: { top: 0, type: 'scroll' }, tooltip: { trigger: 'axis', formatter: tipOf(K, UNIT, v => f.mn(v, 2)) },
        xAxis: xCat(dates), yAxis: { type: 'value', unit: UNIT + ', накопленно', axisLabel: { formatter: v => K.tick(v, 1e6) } },
        series: defs.map(d => ({ name: d.l, type: 'line', showSymbol: false, connectNulls: false, color: col(d), lineStyle: { width: 1.6, type: d.hatch ? 'dashed' : 'solid' }, data: P.cumulate(vals(d.v)) }))
          .concat([{ name: 'Итог', type: 'line', showSymbol: false, connectNulls: false, color: fg, z: 5, lineStyle: { width: 2.6 }, data: P.cumulate(totals), markLine: markDay(), endLabel: { show: true, formatter: p => f.mn(p.value, 1), color: fg, fontSize: 11 } }]),
      };
      return {
        grid: { left: 8, right: 16, top: 40, bottom: 8, containLabel: true }, legend: { top: 0, type: 'scroll' }, tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: tipOf(K, UNIT, v => f.mn(v, 2)) },
        xAxis: xCat(dates), yAxis: { type: 'value', unit: UNIT + ' за день', axisLabel: { formatter: v => K.tick(v, 1e6) } },
        series: defs.map(d => ({ name: d.l, type: 'bar', stack: 'a', stackStrategy: 'samesign', barMaxWidth: 28, color: col(d), itemStyle: d.hatch ? { borderColor: K.css('--fg-2'), borderType: 'dashed', borderWidth: 1 } : {}, data: vals(d.v) }))
          .concat([{ name: 'Итог', type: 'scatter', symbol: 'diamond', symbolSize: 8, color: fg, z: 5, data: totals, markLine: markDay() }]),
      };
    }));
  }
  // one input on the date axis (clean price, yield): its own chart, no dual axis
  function drawLine(el, dates, byDate, name, get, fmt, colour, none) {
    const { K, P } = cur.ctx, data = dates.map(x => { const v = byDate.has(x) ? get(byDate.get(x)) : null; return typeof v === 'number' && isFinite(v) ? v : null; });
    if (!data.some(v => v != null)) { el.outerHTML = '<div class="k-empty">' + K.esc(none) + '</div>'; return; }
    charts.push(K.chart(el, () => ({
      grid: { left: 8, right: 16, top: 16, bottom: 8, containLabel: true }, tooltip: { trigger: 'axis', formatter: tipOf(K, '', fmt) }, xAxis: xCat(dates),
      yAxis: { type: 'value', scale: true, axisLabel: { formatter: v => K.f.n(v, 2) } },
      series: [{ name, type: 'line', showSymbol: data.filter(v => v != null).length < 3, connectNulls: false, color: P.colour(K, colour), lineStyle: { width: 2 }, data, markLine: markDay() }],
    })));
  }

  const COL_LABEL = { carry_coupon: 'Купон', carry_pull: 'Аморт.', carry: 'Кэрри', rolldown: 'Roll', funding: 'Фонд.', rates: 'Ставки', spread: 'Спред', convexity: 'Выпукл.', fx: 'FX нетто', new_trades: 'Сделки', other: 'Необъясн.', reported: 'Разн. PV' };
  const SRC = { prev: ['из расчёта', 'Входы D−1, сохранённые расчётом (pnl.extra.prev)'], previous_row: ['по прогону D−1', 'Из строки той же позиции в предыдущем прогоне периода; при другом количестве поля D−1 не показываются'] };
  function drawDays(el, rows, I, ccy, NA, mn2) {
    const { K, P, state } = cur.ctx, f = K.f, esc = K.esc, ui = state.ui;
    const cols = P.dayColumns(rows.map(r => r.day)), labels = new Map(((cur.ck && cur.ck.checks) || []).map(c => [c.key, c.label]));
    const v = (r, k) => P.srcVal(r.day, k);
    const head = '<tr><th class="stick">Дата</th><th class="n" title="Календарных дней от предыдущего прогона">Дн.</th><th class="n">Цена D−1</th><th class="n">Цена D</th><th>Вход D−1</th><th class="n">Дох. Δ, bp</th><th class="n">Спред Δ, bp</th><th class="n">DV01, тыс/bp</th><th class="n">Итог</th>' +
      cols.map(k => '<th class="n" title="' + esc((P.BY_KEY[k] || {}).label || k) + '">' + esc(COL_LABEL[k] || k) + '</th>').join('') +
      '<th class="n" title="Линейная оценка ставок (pnl.extra.rates_linear)">Ставки лин.</th><th class="n" title="Итог метода минус сумма gross-компонент (grossCheck); должно быть 0">Переоц. − Σ</th><th>События / находки</th></tr>';
    const mon = d => new Date(d + 'T00:00:00Z').getUTCDay() === 1;
    const line = r => {
      const fs = I.on(cur.ref, ccy, r.date).filter(x => x.check !== 'life'), sev = P.worstSev(fs), src = SRC[r.src];
      const qty = r.src === 'previous_row' && r.quantityPrev != null && r.quantity != null && r.quantityPrev !== r.quantity;
      const gap = r.gap == null ? NA('нет проверки тождества') : Math.abs(r.gap) > 1e3 ? '<span class="sevtxt warn" title="' + esc(f.n(r.gap, 0)) + ' RUB">' + f.mn(r.gap, 3) + '</span>' : '<span class="mut3" title="' + esc(f.n(r.gap, 0)) + ' RUB">' + f.mn(r.gap, 3) + '</span>';
      return '<tr class="' + (sev === 'crit' ? 'critrow' : sev === 'warn' ? 'warnrow' : '') + (ui.pday === r.date ? ' sel' : '') + '" data-d="' + r.date + '">' +
        '<td class="stick">' + f.date(r.date) + (r.cal > 1 && !(r.cal === 3 && mon(r.date)) ? ' <span class="mut3" title="Интервал от предыдущего прогона">с ' + esc(f.date(r.prevDate).slice(0, 5)) + '</span>' : '') + '</td>' +
        '<td class="n">' + (r.cal == null ? NA() : r.cal) + '</td>' +
        '<td class="n">' + (r.cleanPrev == null ? NA(qty ? 'количество изменилось: поля D−1 не показываются' : 'нет входа D−1') : f.px(r.cleanPrev)) + '</td><td class="n">' + (r.clean == null ? NA() : f.px(r.clean)) + '</td>' +
        '<td class="mut">' + (src ? '<span title="' + esc(src[1]) + '">' + src[0] + '</span>' : NA('нет входов D−1')) + '</td>' +
        '<td class="n">' + (r.dy == null ? NA() : P.signedBp(r.dy, 1)) + '</td><td class="n">' + (r.dz == null ? NA() : P.signedBp(r.dz, 1)) + '</td><td class="n">' + (r.dv01 == null ? NA() : f.dv(r.dv01)) + '</td>' +
        '<td class="n"><b>' + (r.total == null ? NA(r.noPnl ? 'нет PnL: ' + (r.noPnl.reason || '') : 'нет итога') : f.mn(r.total, 2)) + '</b></td>' +
        cols.map(k => '<td class="n">' + mn2(v(r, k)) + '</td>').join('') +
        '<td class="n mut">' + mn2(r.ratesLinear) + '</td><td class="n">' + gap + '</td>' +
        '<td class="ev">' + r.events.map(e => '<span class="st info">' + esc(e) + '</span> ').join('') +
          fs.map(x => '<div class="flagtxt ' + x.sev + '">' + P.SEV[x.sev].icon + ' ' + esc((labels.get(x.check) || x.check) + ': ' + x.text) + (x.dateTo && x.dateTo !== x.date ? ' (' + f.date(x.date) + '–' + f.date(x.dateTo) + ')' : '') + '</div>').join('') + '</td></tr>';
    };
    const S = k => P.sumAvail(rows.map(r => r[k])), first = rows[0], last = rows[rows.length - 1];
    const foot = '<tr class="tot"><td class="stick">Период</td><td class="n">' + (S('cal') == null ? NA() : S('cal')) + '</td><td class="n">' + (first.cleanPrev == null ? NA() : f.px(first.cleanPrev)) + '</td><td class="n">' + (last.clean == null ? NA() : f.px(last.clean)) + '</td><td></td>' +
      '<td class="n">' + (S('dy') == null ? NA() : P.signedBp(S('dy'), 1)) + '</td><td class="n">' + (S('dz') == null ? NA() : P.signedBp(S('dz'), 1)) + '</td><td></td><td class="n"><b>' + mn2(S('total')) + '</b></td>' +
      cols.map(k => '<td class="n">' + mn2(P.sumAvail(rows.map(r => v(r, k)))) + '</td>').join('') + '<td class="n">' + mn2(S('ratesLinear')) + '</td><td class="n">' + mn2(S('gap')) + '</td><td></td></tr>';
    const life = I.of(cur.ref, ccy).filter(x => x.check === 'life');
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-position-days" data-title="Переоценка позиции по дням, ' + UNIT + '"><thead>' + head + '</thead><tbody>' + rows.slice().reverse().map(line).join('') + '</tbody><tfoot>' + foot + '</tfoot></table>' +
      '<p class="hint" style="padding:0 12px">Δ доходности и спреда — D минус D−1, bp. «Вход D−1»: «из расчёта» — входы, сохранённые расчётом; «по прогону D−1» — строка той же позиции в предыдущем прогоне. «Переоц. − Σ» — итог метода минус сумма компонент.' +
      (life.length ? ' Жизнь позиции — в событиях строк.' : '') + (!cur.ck ? (cur.ckError ? ' Проверки не загрузились: находок в строках нет.' : ' Проверки загружаются…') : '') + '</p>';
    el.querySelectorAll('tr[data-d]').forEach(tr => tr.onclick = () => {
      ui.pday = ui.pday === tr.dataset.d ? null : tr.dataset.d;
      el.querySelectorAll('tr[data-d]').forEach(x => x.classList.toggle('sel', x.dataset.d === ui.pday));
      charts.forEach(c => c.redraw && c.redraw());
    });
  }
})();
