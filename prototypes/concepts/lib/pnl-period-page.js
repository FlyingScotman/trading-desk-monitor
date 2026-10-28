/* C19 «PnL за период»: the shell (tabs by hash, period, filters, /period with the latest request winning) and screens
   A · Декомпозиция, B · Матрица, C · Кэрри. Screens D, E, F register in PNL_PERIOD_SCREENS from their own files and get
   the same ctx. Numbers come from PNL_PERIOD (lib/pnl-period.js); missing values stay «—», never 0. */
(function () {
  'use strict';
  const K = KIT, H = PNL_HISTORY, P = PNL_PERIOD, M = MOCK, f = K.f, esc = K.esc, $ = id => document.getElementById(id);
  const SCREENS = window.PNL_PERIOD_SCREENS = window.PNL_PERIOD_SCREENS || {};
  // Saved history stays in RUB; the ISO code avoids KIT's representation-symbol substitution.
  K.top({ title: 'C19 · PnL за период', tag: 'сохранённые прогоны · RUB' });
  const TABS = [
    ['decomp', 'A', 'Декомпозиция', 'Из чего сложился PnL периода: водопад в любом разрезе, кто дал каждый вклад, как это накапливалось.'],
    ['matrix', 'B', 'Матрица', 'Группы и позиции × компоненты в одной таблице с подсветкой. Ячейка открывает её накопление и крупнейшие дни.'],
    ['carry', 'C', 'Кэрри', 'Купон, амортизация, roll-down и фондирование; кэрри против фондирования по позициям.'],
    ['curves', 'D', 'Ставки по кривым', 'Сдвиг кривой, DV01 и PnL ставок по срокам рядом.'],
    ['position', 'E', 'Позиция', 'Одна позиция за период: компоненты, цена и доходность, дни с входными данными.'],
    ['control', 'F', 'Контроль', 'Проверки PnL периода: сходимость, остаток, устаревшие цены, знаки, пропуски.'],
  ];
  const UNIT = 'млн RUB', DEMO = 'PnL за период читает сохранённые прогоны сервера. В файловом демо истории прогонов нет.';
  const st = {
    screen: 'decomp', preset: 'mtd', from: M.asOf.slice(0, 8) + '01', to: M.asOf, gran: 'auto', assetClass: '', issuer: '', q: '',
    lens: 'explain', carryNet: true, btMode: 'explain', mode: 'cum', selComp: 'total',
    mgroup: 'type', mlens: 'explain', mcarry: 'net', collapsed: new Set(), msort: null, mdir: -1, cell: null, heat: true,
    cmode: 'net', cgroup: 'none', csort: 'net',
    position: null, positionDate: null, ui: {},  // E's selection (openPosition); ui — free room for D/E/F
  };
  let data = null, prep = null, catalogue = [], cataloguePeriod = null, loadedKey = null, current = null, searchTimer = null;

  // ---------- requests ----------
  // period and filters of the page; extra: {name: value | [values] | null (drop)}
  function query(extra) {
    const q = new URLSearchParams({ from: st.from, to: st.to, run_id: M.run.id });
    for (const k of ['entity', 'desk', 'ccy']) if (K.filter[k]) q.set(k, K.filter[k]);
    (K.filter.books || []).forEach(v => q.append('books', v));
    (K.filter.portsOff || []).forEach(v => q.append('ports_off', v));
    for (const [k, v] of [['asset_class', st.assetClass], ['issuer', st.issuer], ['q', st.q]]) if (v) q.set(k, v);
    Object.entries(extra || {}).forEach(([k, v]) => { q.delete(k); if (v != null) (Array.isArray(v) ? v : [v]).forEach(x => q.append(k, String(x))); });
    return q;
  }
  async function fetchJson(path, params, signal) {
    const r = await fetch('..' + (path.startsWith('/') ? path : '/' + path) + (params ? '?' + params : ''), { cache: 'no-store', signal });
    if (!r.ok) throw new Error(r.status === 401 ? 'Сессия закончилась. Войдите снова.' : 'PnL за период не загружен (HTTP ' + r.status + ').');
    return r.json();
  }
  const granReq = () => (st.gran === 'auto' ? P.autoGran(st.from, st.to) : st.gran);
  const load = H.loader((q, signal) => fetchJson('/api/v1/history/pnl/period', q, signal), result => {
    if (result.loading) { data = prep = null; disposeScreen(); $('main').innerHTML = ''; hideExtras(); status('Загружаю выбранный период и фильтры…'); return; }
    if (result.error) { status(result.error.message); return; }
    data = result.data; prep = P.prepare(data); cellCache.clear();
    if (data.catalogue) { catalogue = data.catalogue; cataloguePeriod = data.from + '/' + data.to; K.setFilterScope(H.scope(catalogue)); fillOptions(); }
    status(''); renderScreen();
  });
  function invalidate() { load.invalidate(); data = prep = null; loadedKey = null; disposeScreen(); $('main').innerHTML = ''; hideExtras(); }
  function refresh() {
    clearTimeout(searchTimer); paintPeriod();
    if (K.source !== 'real') return;
    const error = H.periodError(st.from, st.to, M.asOf);
    if (error) { invalidate(); status(error); return; }
    const key = query({ gran: granReq() }).toString();
    if (key === loadedKey && prep) { renderScreen(); return; }  // a format or theme change: same slice, no request
    loadedKey = key;
    load(query({ gran: granReq(), catalogue: String(cataloguePeriod !== st.from + '/' + st.to) }));
  }
  const status = t => { $('status').textContent = t; };
  const hideExtras = () => { $('anchor').hidden = $('coveragePanel').hidden = true; };

  // ---------- period and filters ----------
  function paintPeriod() {
    K.seg($('preset'), P.PRESETS, st.preset, v => {
      st.preset = v;
      const r = P.presetRange(v, M.asOf); if (r) [st.from, st.to] = r;
      refresh();
    });
    $('from').value = st.from; $('to').value = st.to;
  }
  $('from').max = $('to').max = M.asOf;
  for (const id of ['from', 'to']) $(id).onchange = () => { st[id] = $(id).value; st.preset = 'custom'; refresh(); };
  function fillOptions() {
    for (const id of ['ccy', 'assetClass', 'issuer']) {
      const value = id === 'ccy' ? K.filter.ccy || '' : st[id];
      const values = [...new Set(catalogue.map(r => r[id]).filter(Boolean))].sort();
      if (value && !values.includes(value)) values.push(value);
      $(id).innerHTML = '<option value="">Все</option>' + values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
      $(id).value = value;
    }
  }
  fillOptions();
  $('ccy').onchange = () => K.setFilter({ ccy: $('ccy').value || null });
  for (const id of ['assetClass', 'issuer']) $(id).onchange = () => { st[id] = $(id).value; refresh(); };
  $('search').oninput = () => {
    st.q = $('search').value;
    if (K.source !== 'real') return;
    clearTimeout(searchTimer); invalidate(); status('Загружаю выбранный период и фильтры…');
    searchTimer = setTimeout(refresh, 350);
  };
  $('reset').onclick = () => { st.assetClass = st.issuer = st.q = ''; $('search').value = ''; fillOptions(); K.setFilter({ entity: null, desk: null, books: null, ccy: null, portsOff: null }); };
  K.onFilter(() => { $('ccy').value = K.filter.ccy || ''; refresh(); });
  K.on('theme', () => { if (prep) renderScreen(); });

  // ---------- tabs and screens ----------
  function paintTabs() {
    $('tabs').innerHTML = TABS.map(([k, l, n, d]) => '<button data-tab="' + k + '" title="' + esc(d) + '"' + (st.screen === k ? ' class="on" aria-current="page"' : '') + '><b>' + l + '</b>' + n + '</button>').join('');
    $('tabs').querySelectorAll('[data-tab]').forEach(b => b.onclick = () => { if (st.screen === b.dataset.tab) return; location.hash = b.dataset.tab; });
  }
  function disposeScreen() { if (current && current.dispose) current.dispose(); current = null; }
  function openPosition(ref, date) {
    st.position = ref || null; st.positionDate = date || null;
    if (st.screen === 'position') renderScreen(); else location.hash = 'position';
  }
  const ctx = () => ({ K, P, state: st, period: data, prep, query, fetchJson, openPosition, rerender: renderScreen });
  function renderScreen() {
    disposeScreen(); paintTabs();
    const main = $('main');
    if (K.source !== 'real') { main.innerHTML = '<div class="k-panel"><div class="bd">' + esc(DEMO) + '</div></div>'; return; }
    if (!prep) return;  // loading or an error: the status says which; no stale result
    main.innerHTML = '';
    current = SCREENS[st.screen] || null;
    if (current) current.render(main, ctx());
    else main.innerHTML = '<div class="k-panel"><div class="bd k-empty">экран не загрузился</div></div>';
    K.tableToolsAll(main);
    drawAnchor(); drawCoverage();
  }
  function route() { const h = location.hash.slice(1); st.screen = TABS.some(t => t[0] === h) ? h : 'decomp'; renderScreen(); }
  addEventListener('hashchange', () => { route(); scrollTo(0, 0); });

  // ---------- shared pieces ----------
  const mn = v => f.mn(v, 1), mn2 = v => f.mn(v, 2);
  const NA = (why) => '<span class="mut3" title="' + esc(why || 'нет данных') + '">—</span>';
  const tdv = (v, why, cls) => '<td class="n' + (cls ? ' ' + cls : '') + '">' + (v == null ? NA(why) : mn(v)) + '</td>';
  // source colours: KIT.PNL_COLORS families, KIT.PAL slots, kit tokens (P.colour, shared with D, E, F)
  const colour = (spec, shade) => P.colour(K, spec, shade);
  const colOf = d => colour(d.color, d.shade);
  const sw = d => '<span class="sw' + (d.hatch ? ' hatch' : '') + '" style="background-color:' + colOf(d) + '"></span>';
  const lede = key => { const t = TABS.find(x => x[0] === key); return '<div class="lede"><h1>' + t[1] + ' · ' + t[2] + '</h1><p>' + esc(t[3]) + '</p></div>'; };
  const kpi = (label, v, sub, cls, tip) => K.kpi(label, (v == null ? NA() : mn(v)) + K.kpiUnit(UNIT), sub, '', tip).replace('class="k-kpi"', 'class="k-kpi' + (cls ? ' ' + cls : '') + '"');
  const panel = (title, tools, body, bodyCls) => '<div class="k-panel"><div class="hd"><h3>' + title + '</h3><span class="sp"></span>' + (tools || '') + '</div><div class="bd' + (bodyCls ? ' ' + bodyCls : '') + '">' + body + '</div></div>';
  const emptyNote = () => '<div class="k-panel"><div class="bd k-empty">В выбранном периоде и фильтре нет сохранённых записей PnL. Расширьте период или верните книги.</div></div>';
  const labels = () => ({ kind: p => ((M.groupLabel || {})[p.group] || p.group || '—') + ' · ' + ((M.kindLabel || {})[p.kind] || p.kind || '—'), curve: id => prep.curveName(id) });
  const posName = p => (p.name || p.positionId) + ' · ' + p.book;
  const partial = x => x && x.rows ? x.rows + ' зап.' + (x.noTotal ? ' · без итога ' + x.noTotal : '') + (x.partialRows ? ' · неполных ' + x.partialRows : '') : 'нет записей';
  const segInto = (el, items, cur, fn) => { if (el) K.seg(el, items, cur, fn); };
  const netBox = id => '<label class="netbox" title="Кэрри нетто = кэрри + roll-down + фондирование. Без галочки — брутто и фондирование отдельно."><input type="checkbox" id="' + id + '"' + (st.carryNet ? ' checked' : '') + '> кэрри нетто</label>';
  function bindNet(id, on) {
    const el = $(id); if (!el) return;
    el.disabled = !on; el.parentNode.classList.toggle('off', !on);
    el.onchange = () => { st.carryNet = el.checked; st.selComp = 'total'; renderScreen(); };
  }
  // charts of A, B, C: disposed with their screen
  let charts = [];
  const chart = (el, fn) => { const c = K.chart(el, fn); charts.push(c); return c; };
  const disposeCharts = () => { charts.forEach(c => c.dispose()); charts = []; };
  const xCat = (data, fmt) => ({ type: 'category', data, axisTick: { show: false }, axisLabel: { formatter: fmt, hideOverlap: true } });
  const yMln = unit => ({ type: 'value', unit: unit || UNIT, axisLabel: { formatter: v => K.tick(v, 1e6) } });
  const dShort = d => d.slice(8, 10) + '.' + d.slice(5, 7);
  function axisTip(ps) {
    const rows = ps.filter(p => p.value != null && !(Array.isArray(p.value) && p.value[1] == null)).map(p => {
      const v = Array.isArray(p.value) ? p.value[1] : p.value;
      return '<tr><td>' + p.marker + esc(p.seriesName) + '</td><td style="text-align:right;padding-left:14px;font-variant-numeric:tabular-nums">' + mn2(v) + '</td></tr>';
    });
    return ps.length ? '<b>' + esc(ps[0].axisValueLabel) + '</b> · ' + UNIT + '<table>' + rows.join('') + '</table>' : '';
  }
  const legendTop = () => ({ top: 0, type: 'scroll' });

  // waterfall: items [{k, l, v, color, shade, hatch, d}] and the total; a missing item is «—» at the running level
  function waterfall(el, items, total, onClick, selKey, signColours) {
    const rows = items.concat([{ k: 'total', l: 'Итог', v: total, color: '--fg-2', d: P.DERIVED.total.d }]);
    const fill = it => it.k === 'total' ? K.css('--fg-2') : signColours ? K.css(it.v != null && it.v < 0 ? '--neg' : '--pos') : colOf(it);
    let run = 0;
    const data = rows.map((it, i) => {
      if (it.k === 'total') return [i, Math.min(0, total || 0), Math.max(0, total || 0)];
      if (it.v == null) return [i, run, run];
      const a = run, b = run + it.v; run = b; return [i, Math.min(a, b), Math.max(a, b)];
    });
    const c = chart(el, () => ({
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      tooltip: { trigger: 'item', formatter: p => { const it = rows[p.dataIndex]; return '<b>' + esc(it.l) + '</b><br>' + (it.v == null ? 'нет данных' : mn2(it.v) + ' ' + UNIT) + (it.d ? '<div style="max-width:260px;white-space:normal;opacity:.8">' + esc(it.d) + '</div>' : ''); } },
      // labels under the bars: wrapped while a category has room, rotated and shortened when it does not
      xAxis: Object.assign(xCat(rows.map(r => r.l)), { axisLabel: (el.clientWidth - 70) / rows.length < 84 || rows.length > 9
        ? { interval: 0, rotate: 40, fontSize: 10.5, formatter: v => (v.length > 16 ? v.slice(0, 15) + '…' : v) }
        : { interval: 0, width: Math.min(110, (el.clientWidth - 70) / rows.length - 6), overflow: 'break', fontSize: 11, lineHeight: 13 } }),
      yAxis: yMln(),
      series: [{
        type: 'custom', encode: { x: 0, y: [1, 2] }, data,
        renderItem: (params, api) => {
          const i = api.value(0), it = rows[i], lo = api.value(1), hi = api.value(2);
          if (it.v == null) return K.wfNull(api, i, hi, K.css('--fg-3'));
          const a = api.coord([i, lo]), b = api.coord([i, hi]), w = api.size([1, 0])[0] * 0.62, sel = selKey === it.k;
          return { type: 'group', children: [
            { type: 'rect', shape: { x: a[0] - w / 2, y: b[1], width: w, height: Math.max(1.5, a[1] - b[1]), r: 2 },
              style: { fill: fill(it), stroke: sel ? K.css('--fg') : it.hatch ? K.css('--fg-3') : null, lineWidth: sel ? 2 : 1, lineDash: it.hatch && !sel ? [3, 2] : null } },
            { type: 'text', style: { text: mn(it.v), x: a[0], y: it.v >= 0 ? b[1] - 3 : a[1] + 3, align: 'center', verticalAlign: it.v >= 0 ? 'bottom' : 'top', fill: it.v < 0 ? K.css('--neg') : K.css('--fg-2'), font: '11px system-ui, sans-serif' } },
          ] };
        },
      }],
    }));
    if (onClick) c.on('click', p => onClick(rows[p.dataIndex].k));
    return c;
  }

  // ======================= A · Декомпозиция =======================
  function defsA(mode) { return P.rowsFor(mode, prep, { carry: st.carryNet ? 'net' : 'split', labels: labels() }); }
  SCREENS.decomp = {
    render(main) {
      if (!prep.summary.rows) { main.innerHTML = lede('decomp') + emptyNote(); return; }
      const T = P.tilesA(prep), cov = t => (t ? ' · ' + t : '');
      main.innerHTML = lede('decomp') + '<div class="k-kpis">' +
        kpi('Итог за период', T.total, partial(prep.summary), 'main', 'Сумма сохранённых компонент по net-конвенции') +
        kpi('Кэрри нетто', T.carryNet, 'брутто ' + mn(T.carryGross) + ' · фонд. ' + mn(T.funding) + cov(T.cover.carry)) +
        kpi('Ставки + спред', T.market, 'ставки ' + mn(T.rates) + ' · спред ' + mn(T.spread) + cov(T.cover.market)) +
        kpi('FX нетто (ОВП)', T.fx, 'нетто открытой валютной позиции' + cov(T.cover.fx), '', P.BY_KEY.fx.d) +
        kpi('Необъяснённое', T.other, (T.otherShare == null ? 'доля — нет данных' : f.n(T.otherShare, 1) + '% от объяснённого по модулю') + cov(T.cover.other), T.otherShare > 5 ? 'flag' : '') +
        '</div>' +
        '<div class="grid2">' + panel('Водопад периода', '<span id="lensSeg"></span>' + netBox('netA'), '<div class="ch tall" id="wf"></div><div class="hint">Щелчок по столбцу — кто дал этот вклад (справа). «Итог» — сумма компонент периода.</div>') +
        panel('<span id="contribTitle"></span>', '', '<div id="contrib"></div>') + '</div>' +
        panel('Как накапливалось', '<span class="hint">разрез — как у водопада</span><span id="modeSeg"></span><span id="granSeg"></span>', '<div class="ch tall" id="ts"></div><div class="hint" id="tsNote"></div>') +
        panel('По периодам', '<span id="btSeg"></span>' + netBox('netB') + '<span class="hint">' + UNIT + ' · столбцы — ' + ({ day: 'дни', week: 'недели', month: 'месяцы' })[data.gran] + '</span>', '<div class="tscroll" id="bt"></div>', 'flush');
      segInto($('lensSeg'), P.MODES, st.lens, v => { st.lens = v; st.selComp = 'total'; renderScreen(); });
      segInto($('btSeg'), P.BT_MODES, st.btMode, v => { st.btMode = v; renderScreen(); });
      bindNet('netA', P.CARRY_MODES.includes(st.lens)); bindNet('netB', P.CARRY_MODES.includes(st.btMode));
      const defs = defsA(st.lens), group = !P.COMPONENT_MODES.includes(st.lens);
      waterfall($('wf'), P.waterfallItems(defs), prep.summary.total, k => { st.selComp = st.selComp === k ? 'total' : k; renderScreen(); }, st.selComp, group);
      drawContrib(defs);
      segInto($('modeSeg'), [['cum', 'Накопленно'], ['per', 'По периодам']], st.mode, v => { st.mode = v; renderScreen(); });
      segInto($('granSeg'), [['auto', 'Авто'], ['day', 'Дни'], ['week', 'Недели'], ['month', 'Месяцы']], st.gran, v => { st.gran = v; refresh(); });
      drawSeries($('ts'), P.chartRows(defs));
      drawBucketTable($('bt'), defsA(st.btMode));
    },
    dispose: disposeCharts,
  };
  function selectedDef(defs) {
    const key = st.selComp || 'total';
    if (key === 'total') return { k: 'total', l: 'Итог', v: p => p.total };
    if (key === 'g:*') {
      const items = P.waterfallItems(defs), mid = (items.find(x => x.k === 'g:*') || { keys: [] }).keys, ds = defs.filter(d => mid.includes(d.k));
      return { k: 'g:*', l: 'Остальные ' + ds.length, v: p => P.sumAvail(ds.map(d => d.v(p))) };
    }
    return defs.find(d => d.k === key) || { k: 'total', l: 'Итог', v: p => p.total };
  }
  function drawContrib(defs) {
    const def = selectedDef(defs), C = P.contrib(prep.positions, def), max = Math.max(1, ...C.plus.concat(C.minus).map(x => Math.abs(x.v)));
    $('contribTitle').innerHTML = 'Кто дал: ' + esc(def.l) + ' · ' + (C.total == null ? '—' : mn(C.total)) + ' млн';
    const row = x => '<button class="link nm" data-ref="' + esc(x.ref) + '" title="Открыть позицию (E)">' + esc(x.p.name || x.p.positionId) + ' <span class="mut3">' + esc(x.p.book) + '</span></button>' +
      '<span class="n">' + mn(x.v) + '</span><span class="bars' + (x.v < 0 ? ' l' : '') + '"><span class="bar" style="width:' + Math.max(2, Math.abs(x.v) / max * 90) + 'px;background:' + K.css(x.v < 0 ? '--neg' : '--pos') + '"></span></span>';
    $('contrib').innerHTML = '<div class="contrib">' + (C.plus.length ? '<h4>Больше всего добавили</h4>' + C.plus.map(row).join('') : '') + (C.minus.length ? '<h4>Больше всего отняли</h4>' + C.minus.map(row).join('') : '') +
      (!C.n ? '<span class="mut">Нет вкладов больше ' + f.n(P.MIN_CONTRIB / 1e3) + ' тыс. RUB.</span>' : '') + '</div>' +
      (def.d && def.k !== 'total' ? '<p class="hint">' + esc(def.d) + '</p>' : '') +
      '<p class="hint">Позиций с вкладом: ' + C.n + (C.missing ? ' · нет данных у ' + C.missing + ' поз.' : '') + '. Щелчок по позиции — экран E.</p>';
    $('contrib').querySelectorAll('[data-ref]').forEach(b => b.onclick = () => openPosition(b.dataset.ref));
  }
  // cumulative: daily for component rows (saved days), by bucket for group rows (the positions' bucket totals);
  // by period: stacked bars per bucket and the bucket total
  function drawSeries(el, defs) {
    const daily = defs.length && defs[0].daily && st.mode === 'cum', byDate = new Map(prep.days.map(d => [d.date, d]));
    const x = daily ? prep.dates : prep.buckets.map(b => b.label);
    const totals = daily ? prep.dates.map(d => (byDate.has(d) ? byDate.get(d).total : null)) : prep.bucketAggs.map(a => (a ? a.total : null));
    const vals = d => (daily ? prep.dates.map(x => (byDate.has(x) ? d.v(byDate.get(x)) : null)) : d.buckets);
    $('tsNote').textContent = st.mode === 'cum' && !daily ? 'Группы накапливаются по столбцам периода: по дням позиции — на экране E.' : 'Пропуск — дня без прогона или без значения; линия его не соединяет.';
    chart(el, () => {
      const fg = K.css('--fg');
      if (st.mode === 'cum') return {
        grid: { left: 8, right: 64, top: 40, bottom: 8, containLabel: true }, legend: legendTop(), tooltip: { trigger: 'axis', formatter: axisTip },
        xAxis: xCat(x, daily ? dShort : null), yAxis: yMln(UNIT + ', накопленно'),
        series: defs.map(d => ({ name: d.l, type: 'line', showSymbol: false, connectNulls: false, color: colOf(d), lineStyle: { width: 1.6, type: d.hatch ? 'dashed' : 'solid' }, data: P.cumulate(vals(d)) }))
          .concat([{ name: 'Итог', type: 'line', showSymbol: false, connectNulls: false, color: fg, z: 5, lineStyle: { width: 2.6 }, data: P.cumulate(totals), endLabel: { show: true, formatter: p => mn(p.value), color: fg, fontSize: 11 } }]),
      };
      return {
        grid: { left: 8, right: 16, top: 40, bottom: 8, containLabel: true }, legend: legendTop(), tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: axisTip },
        xAxis: xCat(x), yAxis: yMln(UNIT + ' за ' + ({ day: 'день', week: 'неделю', month: 'месяц' })[data.gran]),
        series: defs.map(d => ({ name: d.l, type: 'bar', stack: 'a', stackStrategy: 'samesign', barMaxWidth: 34, color: colOf(d), itemStyle: d.hatch ? { borderColor: K.css('--fg-2'), borderType: 'dashed', borderWidth: 1 } : {}, data: d.buckets }))
          .concat([{ name: 'Итог', type: 'scatter', symbol: 'diamond', symbolSize: 9, color: fg, z: 5, data: totals }]),
      };
    });
  }
  function drawBucketTable(el, defs) {
    const B = prep.buckets, market = data.marketDates || [];
    const empty = i => (P.bucketHasMarket(B[i], market) ? '<span class="mut3">нет прогона</span>' : NA('нет рыночных дней'));
    const cells = (vals, bold) => B.map((b, i) => (prep.bucketAggs[i] == null ? '<td class="n">' + empty(i) + '</td>' : tdv(vals[i]))).join('') + '<td class="n"><b>' + (bold == null ? NA() : mn(bold)) + '</b></td>';
    const head = '<tr><th class="stick">' + (P.COMPONENT_MODES.includes(st.btMode) ? 'Компонента' : P.GROUPS[st.btMode].title) + '</th>' + B.map(b => '<th class="n" title="' + esc(b.from + ' — ' + b.to) + '">' + esc(b.label) + '</th>').join('') + '<th class="n">Период</th></tr>';
    const body = defs.map(d => '<tr><td class="stick">' + sw(d) + (d.ref ? '<button class="link" data-ref="' + esc(d.ref) + '">' + esc(d.l) + '</button>' : esc(d.l)) + '</td>' + cells(d.buckets, d.total) + '</tr>').join('');
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-buckets" data-title="PnL по периодам, ' + UNIT + '"><thead>' + head + '</thead><tbody>' + body + '</tbody><tfoot><tr><td class="stick">Итог</td>' +
      cells(prep.bucketAggs.map(a => (a ? a.total : null)), prep.summary.total) + '</tr></tfoot></table>';
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => openPosition(b.dataset.ref));
  }

  // ======================= B · Матрица =======================
  const MGROUPS = [['type', 'Тип → позиция'], ['book', 'Книга → позиция'], ['ccy', 'Валюта → позиция'], ['curve', 'Кривая → позиция'], ['rating', 'Рейтинг → позиция']];
  const cellCache = new Map();  // /position responses of this slice (loadedKey): a re-render does not ask again
  const cellLoad = H.loader((q, signal) => fetchJson('/api/v1/history/pnl/position', q, signal), r => {
    const el = $('cellDays'); if (!el) return;
    if (r.loading) el.textContent = 'Загружаю дни позиции…';
    else if (r.error) el.textContent = r.error.message;
    else { cellCache.set(cellKey(), r.data); drawCellDays(r.data); }
  });
  let cellCtx = null;
  const cellKey = () => loadedKey + '#' + (cellCtx ? cellCtx.ref : '');
  // highlight by sign and size, lighter than K.heat: the figures stay readable in both themes
  const heatBg = (v, max) => { const c = K.rgbOf(K.css(v < 0 ? '--neg' : '--pos')) || [128, 128, 128], a = Math.min(1, Math.abs(v) / max); return 'background:rgba(' + c.join(',') + ',' + (0.07 + 0.36 * Math.pow(a, 0.8)).toFixed(3) + ')'; };
  SCREENS.matrix = {
    render(main) {
      if (!prep.summary.rows) { main.innerHTML = lede('matrix') + emptyNote(); return; }
      main.innerHTML = lede('matrix') + '<div class="k-panel"><div class="hd ctl"><label>Строки <select id="mg">' + MGROUPS.map(([v, l]) => '<option value="' + v + '">' + l + '</option>').join('') + '</select></label>' +
        '<span id="mlSeg"></span><span id="mcSeg"></span><label class="netbox"><input type="checkbox" id="mheat"' + (st.heat ? ' checked' : '') + '> подсветка</label><span class="sp"></span>' +
        '<button class="k-btn" id="mexp">Раскрыть все</button><button class="k-btn" id="mcol">Свернуть</button></div></div>' +
        '<div class="mwrap"><div class="k-panel"><div class="bd flush tscroll" style="max-height:640px" id="mt"></div></div>' +
        '<div class="k-panel"><div class="hd"><h3 id="cellTitle">Ячейка</h3></div><div class="bd" id="cellBox"><p class="mut">Щёлкните ячейку: здесь появится её накопление и дни с наибольшим вкладом.</p></div></div></div>';
      $('mg').value = st.mgroup; $('mg').onchange = () => { st.mgroup = $('mg').value; st.collapsed.clear(); st.cell = null; renderScreen(); };
      segInto($('mlSeg'), [['explain', 'Explain'], ['attr', 'Атрибуция']], st.mlens, v => { st.mlens = v; st.msort = null; st.cell = null; renderScreen(); });
      if (st.mlens === 'explain') segInto($('mcSeg'), [['net', 'Кэрри нетто'], ['split', 'Брутто + фонд.'], ['comp', 'Компоненты']], st.mcarry, v => { st.mcarry = v; st.msort = null; st.cell = null; renderScreen(); });
      $('mheat').onchange = e => { st.heat = e.target.checked; renderScreen(); };
      const defs = P.visibleRows(P.componentRows(st.mlens, { carry: st.mcarry, split: prep.split, keys: prep.keys, curves: prep.curves }), prep.summary)
        .concat([{ k: 'total', l: 'Итог', d: 'Сумма компонент позиции за период', v: a => (a ? a.total : null) }]);
      const groups = P.matrix(prep.positions, st.mgroup, defs, labels());
      $('mexp').onclick = () => { st.collapsed.clear(); renderScreen(); };
      $('mcol').onclick = () => { groups.forEach(g => st.collapsed.add(g.key)); renderScreen(); };
      const sc = st.msort == null || st.msort >= defs.length ? defs.length - 1 : st.msort;
      P.sortMatrix(groups, sc, st.mdir);
      const maxAbs = defs.map((d, j) => Math.max(1, ...groups.flatMap(g => g.kids.map(k => Math.abs(k.v[j] || 0)))));
      const cell = (v, j, id) => {
        const on = st.cell && st.cell.id === id && st.cell.j === j, heat = st.heat && defs[j].k !== 'total' && v ? heatBg(v, maxAbs[j]) : '';
        return '<td class="n cell' + (on ? ' on' : '') + '"' + (heat ? ' style="' + heat + '"' : '') + ' data-id="' + esc(id) + '" data-j="' + j + '">' + (v == null ? NA() : Math.abs(v) < 5e3 ? '<span class="mut3">·</span>' : mn(v)) + '</td>';
      };
      const head = '<tr><th class="stick">' + MGROUPS.find(x => x[0] === st.mgroup)[1].replace('→', '/') + '</th>' + defs.map((d, j) => '<th class="n sortable' + (j === sc ? ' sorted' : '') + '" data-j="' + j + '" title="' + esc(d.d || '') + '">' + (d.color ? sw(d) : '') + esc(d.l) + (j === sc ? (st.mdir < 0 ? ' ↓' : ' ↑') : '') + '</th>').join('') + '</tr>';
      let body = '';
      groups.forEach(g => {
        const open = !st.collapsed.has(g.key);
        body += '<tr class="grp"><td class="stick"><button class="tw" data-g="' + esc(g.key) + '" aria-expanded="' + open + '">' + (open ? '▾' : '▸') + '</button>' + esc(g.l) + ' <span class="mut3">' + g.n + '</span></td>' + g.v.map((v, j) => cell(v, j, 'g|' + g.key)).join('') + '</tr>';
        if (open) g.kids.forEach(k => { body += '<tr><td class="stick kid"><button class="link" data-ref="' + esc(k.ref) + '">' + esc(k.p.name || k.p.positionId) + '</button> <span class="mut3">' + esc(k.p.book) + (k.p.closed ? ' · закрыта' : '') + '</span></td>' + k.v.map((v, j) => cell(v, j, 'p|' + k.ref)).join('') + '</tr>'; });
      });
      const tot = defs.map(d => (d.k === 'total' ? prep.summary.total : d.v(prep.summary)));
      $('mt').innerHTML = '<table class="k-tbl" data-vk="c19-period-matrix" data-title="Матрица PnL за период, ' + UNIT + '"><thead>' + head + '</thead><tbody>' + body + '</tbody><tfoot><tr><td class="stick">Итого</td>' + tot.map(v => tdv(v)).join('') + '</tr></tfoot></table>';
      $('mt').querySelectorAll('th.sortable').forEach(th => th.onclick = () => { const j = +th.dataset.j; if (j === sc) st.mdir = -st.mdir; else { st.msort = j; st.mdir = -1; } renderScreen(); });
      $('mt').querySelectorAll('[data-g]').forEach(b => b.onclick = () => { const g = b.dataset.g; if (st.collapsed.has(g)) st.collapsed.delete(g); else st.collapsed.add(g); renderScreen(); });
      $('mt').querySelectorAll('[data-ref]').forEach(b => b.onclick = () => openPosition(b.dataset.ref));
      $('mt').querySelectorAll('td.cell').forEach(td => td.onclick = () => { st.cell = { id: td.dataset.id, j: +td.dataset.j }; renderScreen(); });
      if (st.cell) drawCell(groups, defs);
    },
    dispose() { cellLoad.invalidate(); cellCtx = null; disposeCharts(); },
  };
  function drawCell(groups, defs) {
    const [t, id] = [st.cell.id.slice(0, 1), st.cell.id.slice(2)], def = defs[st.cell.j];
    const g = t === 'g' ? groups.find(x => x.key === id) : null, p = t === 'p' ? prep.positions.find(x => x.ref === id) : null;
    if (!def || (!g && !p)) { st.cell = null; return; }
    const v = def.k === 'total' ? (g ? g.agg : p).total : def.v(g ? g.agg : p);
    $('cellTitle').textContent = (g ? g.l : posName(p)) + ' × ' + def.l;
    const tile = kpi('За период', v, def.d || '', 'main');
    if (g) {
      const kids = g.positions.map(x => ({ p: x, v: def.k === 'total' ? x.total : def.v(x) })).filter(x => x.v != null).sort((a, b) => Math.abs(b.v) - Math.abs(a.v)).slice(0, 8);
      $('cellBox').innerHTML = '<div class="k-kpis">' + tile + '</div><h4 class="hint" style="margin:8px 0 4px">Крупнейшие позиции группы</h4><table class="k-tbl"><tbody>' +
        kids.map(x => '<tr><td><button class="link" data-ref="' + esc(x.p.ref) + '">' + esc(posName(x.p)) + '</button></td>' + tdv(x.v) + '</tr>').join('') + '</tbody></table>' +
        '<p class="hint">Дни — в ячейке позиции (сервер отдаёт дни одной позиции).</p>';
      $('cellBox').querySelectorAll('[data-ref]').forEach(b => b.onclick = () => openPosition(b.dataset.ref));
      return;
    }
    cellCtx = { ref: p.ref, def, p };
    $('cellBox').innerHTML = '<div class="k-kpis">' + tile + '</div><div class="ch mini" id="cellCh"></div><div id="cellDays" class="hint"></div><p><button class="k-btn" id="cellOpen">Открыть позицию →</button></p>';
    $('cellOpen').onclick = () => openPosition(p.ref);
    const hit = cellCache.get(cellKey());
    if (hit) drawCellDays(hit);
    else cellLoad(query({ position_book: p.book, position_id: p.positionId }));
  }
  function drawCellDays(resp) {
    if (!cellCtx || !$('cellDays')) return;
    const { def, p } = cellCtx, D = P.cellDays(resp.days || [], def);
    chart($('cellCh'), () => ({ grid: { left: 4, right: 10, top: 10, bottom: 4, containLabel: true }, tooltip: { trigger: 'axis', formatter: axisTip },
      xAxis: xCat(D.dates, dShort), yAxis: { type: 'value', axisLabel: { formatter: v => K.tick(v, 1e6) } },
      series: [{ name: def.l, type: 'line', showSymbol: false, connectNulls: false, color: def.color ? colOf(def) : K.css('--fg'), areaStyle: { opacity: 0.15 }, lineStyle: { width: 2 }, data: D.cum }] }));
    $('cellDays').innerHTML = '<h4 class="hint" style="margin:6px 0">Крупнейшие дни</h4>' + (D.top.length ? '<table class="k-tbl"><tbody>' + D.top.map(x => '<tr><td>' + f.date(x.date) + '</td><td><button class="link" data-d="' + x.date + '">день в E</button></td>' + '<td class="n">' + mn2(x.v) + '</td></tr>').join('') + '</tbody></table>' : '<span class="mut">Нет дней с вкладом больше 1 тыс. RUB.</span>');
    $('cellDays').querySelectorAll('[data-d]').forEach(b => b.onclick = () => openPosition(p.ref, b.dataset.d));
  }

  // ======================= C · Кэрри =======================
  const CCOLS = [['pv', 'pv ср., млн', 0], ['coupon', 'Купон', 1], ['pull', 'Аморт.', 1], ['roll', 'Roll-down', 1], ['gross', 'Брутто', 1], ['funding', 'Фонд.', 1], ['net', 'Нетто', 1],
    ['cy', 'Кэрри, % год.', 2], ['fr', 'Фонд., % год.', 2], ['mg', 'Маржа, bp', 0]];
  const PCT = new Set(['cy', 'fr', 'mg']);
  SCREENS.carry = {
    render(main) {
      if (!prep.summary.rows) { main.innerHTML = lede('carry') + emptyNote(); return; }
      // the split covers every row with carry; rows without PnL (no carry) are counted as for the carry itself
      const S = P.carrySummary(prep), noSplit = 'нет разбивки кэрри: ' + (S.cover.split || 'нет данных'), cc = P.coverText(prep.summary, 'carry'), ccov = cc ? ' · ' + cc : '';
      main.innerHTML = lede('carry') + '<div class="k-kpis">' +
        kpi('Купон / НКД', S.coupon, prep.split ? 'начислено за период' + ccov : noSplit) +
        kpi('Амортизация к номиналу', S.pull, prep.split ? 'дисконт / премия к номиналу' + ccov : noSplit) +
        kpi('Roll-down', S.roll, 'скатывание по кривой') +
        kpi('Кэрри брутто', S.gross, S.grossPct == null ? '% годовых — нет данных' : f.n(S.grossPct, 2) + '% годовых на ср. pv') +
        kpi('Фондирование', S.funding, (S.fundingPct == null ? '% годовых — нет данных' : f.n(S.fundingPct, 2) + '% годовых') + (S.cover.funding ? ' · ' + S.cover.funding : '')) +
        kpi('Кэрри нетто', S.net, S.marginBp == null ? 'маржа — нет данных' : 'маржа ' + f.n(S.marginBp, 0) + ' bp годовых', 'main') + '</div>' +
        '<div class="grid2e">' + panel('Накопленное кэрри', '<span id="cm1"></span>', '<div class="ch" id="cc1"></div>') +
        panel('Кэрри в день', '<span class="hint">' + UNIT + ' на календарный день' + (rateMarks().length ? ' · пунктир — решения по ставке' : '') + '</span><span id="cm2"></span>', '<div class="ch" id="cc2"></div>') + '</div>' +
        panel('Кэрри по позициям', '<label class="netbox">Группировка <select id="cg"><option value="none">нет</option><option value="type">тип</option><option value="book">книга</option></select></label>', '<div class="tscroll" id="ct"></div>', 'flush');
      ['cm1', 'cm2'].forEach(id => segInto($(id), [['net', 'Нетто'], ['gross', 'Брутто']], st.cmode, v => { st.cmode = v; renderScreen(); }));
      drawCarryCharts();
      $('cg').value = st.cgroup; $('cg').onchange = () => { st.cgroup = $('cg').value; renderScreen(); };
      drawCarryTable($('ct'));
    },
    dispose: disposeCharts,
  };
  // rate decisions: /period cbrMeetings [{date, effectiveDate, ratePct, prevRatePct, …}] (md_cbr_meeting) taking effect in
  // the period, marked on the first date of the axis from the effective date; none sent — no marks
  const rateMarks = () => (data.cbrMeetings || []).map(x => {
    const eff = x.effectiveDate || x.date, at = prep.dates.find(d => d >= eff);
    const change = x.ratePct != null && x.prevRatePct != null ? Math.round((x.ratePct - x.prevRatePct) * 100) : null;
    return at && eff <= data.to ? { date: at, change, rate: x.ratePct, estimated: x.estimated } : null;
  }).filter(Boolean);
  const bpTxt = v => (v > 0 ? '+' : v < 0 ? '−' : '') + Math.abs(v) + ' бп';
  function drawCarryCharts() {
    const CS = P.carryParts(st.cmode, prep.split), byDate = new Map(prep.days.map(d => [d.date, d])), dates = prep.dates;
    const at = (d, fn) => (byDate.has(d) ? fn(byDate.get(d)) : null);
    chart($('cc1'), () => {
      const fg = K.css('--fg');
      return { grid: { left: 8, right: 64, top: 40, bottom: 8, containLabel: true }, legend: legendTop(), tooltip: { trigger: 'axis', formatter: axisTip },
        xAxis: xCat(dates, dShort), yAxis: yMln(UNIT + ', накопленно'),
        series: CS.parts.map(d => ({ name: d.l, type: 'bar', stack: 'c', stackStrategy: 'samesign', barCategoryGap: '0%', color: colOf(d), data: P.cumulate(dates.map(x => at(x, d.v))) }))
          .concat([{ name: CS.tot.l, type: 'line', showSymbol: false, connectNulls: false, color: fg, lineStyle: { width: 2.4 }, data: P.cumulate(dates.map(x => at(x, CS.tot.v))), endLabel: { show: true, formatter: p => mn(p.value), color: fg, fontSize: 11 } }]) };
    });
    const marks = rateMarks();
    chart($('cc2'), () => {
      const fg = K.css('--fg'), fg3 = K.css('--fg-3');
      const per = fn => dates.map(x => at(x, d => P.perCalDay(d, fn(d))));
      const lines = CS.parts.map(d => ({ name: d.l, type: 'line', step: 'end', showSymbol: false, connectNulls: false, color: colOf(d), data: per(d.v) }));
      lines.push({ name: CS.tot.l, type: 'line', step: 'end', showSymbol: false, connectNulls: false, color: fg, lineStyle: { width: 2.2 }, data: per(CS.tot.v),
        markLine: marks.length ? { symbol: 'none', silent: true, lineStyle: { color: fg3, type: 'dashed' }, label: { formatter: p => { const m = marks[p.dataIndex] || {}; return 'КС' + (m.rate != null ? ' ' + f.n(m.rate, 2) + '%' : '') + (m.change != null ? ' ' + bpTxt(m.change) : '') + (m.estimated ? ' (оценка)' : ''); }, color: fg3, fontSize: 10, position: 'insideStartTop' }, data: marks.map(m => ({ xAxis: m.date })) } : undefined });
      return { grid: { left: 8, right: 16, top: 40, bottom: 8, containLabel: true }, legend: legendTop(), tooltip: { trigger: 'axis', formatter: axisTip },
        xAxis: xCat(dates, dShort), yAxis: { type: 'value', unit: UNIT + ' / кал. день', axisLabel: { formatter: v => K.tick(v, 1e6) } }, series: lines };
    });
  }
  function drawCarryTable(el) {
    const rows = prep.positions.map(p => P.carryPosition(p)), sc = st.csort;
    rows.sort((a, b) => (b[sc] == null) - (a[sc] == null) || (b[sc] || 0) - (a[sc] || 0));
    const cellOf = (r, [k, , dp]) => (PCT.has(k) ? '<td class="n">' + (r[k] == null ? NA(r.p && P.isDeal(r.p) ? 'для сделок (PV ≈ 0) ставки в % не считаются' : k === 'fr' ? 'нет ставки фондирования от сервера — не оцениваем' : 'нет данных') : f.n(r[k], dp)) + '</td>'
      : k === 'pv' ? '<td class="n">' + (r.pv == null ? NA() : f.mn(r.pv, 0)) + '</td>' : tdv(r[k], k === 'coupon' || k === 'pull' ? 'нет разбивки кэрри' : 'нет данных'));
    const head = '<tr><th class="stick">Позиция</th><th>Книга</th><th>Тип</th>' + CCOLS.map(c => '<th class="n sortable' + (c[0] === sc ? ' sorted' : '') + '" data-c="' + c[0] + '">' + c[1] + (c[0] === sc ? ' ↓' : '') + '</th>').join('') + '</tr>';
    const L = labels(), line = r => '<tr class="' + (r.net != null && r.net < 0 ? 'warnrow' : '') + '"><td class="stick"><button class="link" data-ref="' + esc(r.ref) + '">' + esc(r.p.name || r.p.positionId) + '</button>' + (r.p.closed ? ' <span class="mut3">закрыта</span>' : '') + '</td><td>' + esc(r.p.book) + '</td><td class="mut">' + esc(L.kind(r.p)) + '</td>' + CCOLS.map(c => cellOf(r, c)).join('') + '</tr>';
    const sums = (cls, name, list) => { const t = P.carryTotal(list); return '<tr class="' + cls + '"><td class="stick">' + esc(name) + '</td><td></td><td></td>' + CCOLS.map(c => (PCT.has(c[0]) ? '<td></td>' : cellOf(t, c))).join('') + '</tr>'; };
    let body = '';
    if (st.cgroup === 'none') body = rows.map(line).join('');
    else {
      const key = st.cgroup === 'type' ? r => L.kind(r.p) : r => r.p.book, m = new Map();
      rows.forEach(r => { const k = key(r); if (!m.has(k)) m.set(k, []); m.get(k).push(r); });
      m.forEach((list, name) => { body += sums('grp', name, list) + list.map(line).join(''); });
    }
    el.innerHTML = '<table class="k-tbl" data-vk="c19-period-carry" data-title="Кэрри по позициям, ' + UNIT + '"><thead>' + head + '</thead><tbody>' + body + '</tbody><tfoot>' + sums('', 'Итого', rows).replace('<tr class="">', '<tr>') + '</tfoot></table>' +
      '<p class="hint" style="padding:0 12px">Полоса слева — отрицательное кэрри нетто. % годовых — на средний pv позиции за её календарные дни; фонд. % — ставка сервера, взвешенная по телу (нет ставки — «—», без оценки). Для сделок (деривативы, РЕПО) ставки в % не считаются: PV ≈ 0.</p>';
    el.querySelectorAll('th.sortable').forEach(th => th.onclick = () => { st.csort = th.dataset.c; renderScreen(); });
    el.querySelectorAll('[data-ref]').forEach(b => b.onclick = () => openPosition(b.dataset.ref));
  }

  // ---------- separate anchor, coverage and the limits of the method ----------
  function drawAnchor() {
    const a = data.separateAnchor;
    $('anchor').hidden = !a;
    if (!a) return;
    const run = a.run || {}, s = a.summary || {};
    $('anchorSummary').innerHTML = '<b>' + esc(run.slice || '') + '</b> · ' + esc((run.prevDate || '?') + ' → ' + (run.date || '?')) + ' · итог ' + (s.total == null ? '—' : mn2(s.total)) + ' ' + UNIT +
      '. Интервал пересекает включённые даты ' + esc((a.overlappingDates || []).join(', ')) + '; в итоги периода и накопление не входит.';
  }
  function drawCoverage() {
    const runs = new Set(data.runs.map(r => r.date)), market = (data.marketDates || []).filter(d => d >= data.from && d <= data.to);
    const missing = market.filter(d => !runs.has(d)), intraday = data.intradayOnlyDates || [], intervals = data.runs.filter(r => r.prevDate && r.prevDate < data.from), s = prep.summary;
    $('pnote').textContent = 'Все суммы в RUB · даты включительно · рыночных дней ' + market.length + ' · прогонов ' + runs.size + (missing.length ? ' · без прогона ' + missing.length : '');
    $('coveragePanel').hidden = false;
    $('coverage').innerHTML = '<b>Частичная сумма сохранённых результатов.</b> ' + partial(s) + '. Позиций ' + prep.positions.length + (prep.positions.some(p => p.closed) ? ', из них закрытых ' + prep.positions.filter(p => p.closed).length : '') +
      '. Закрытий рынка без прогона: ' + missing.length + (intraday.length ? '; только intraday, нет закрытия дня: ' + intraday.length : '') +
      '. <details><summary>Даты пропусков и ограничения метода</summary><p>Это не официальный торговый PnL.</p>' +
      'Нет закрытия дня, сохранён только intraday: ' + esc(intraday.join(', ') || 'таких дат нет') + '. Эти дни не входят в накопление.<br>' +
      'Есть закрытие рынка, нет прогона: ' + esc(missing.join(', ') || 'таких дат нет') + '. Следующий прогон покрывает интервал от предыдущего.<br>' +
      'Позиции: ' + prep.positions.length + '; записи: ' + (s.rows || 0) + '; ' + partial(s) + '. Отсутствие строки не означает нулевой результат.<br>' +
      'Граница периода — дата результата; интервал может начинаться раньше: ' + esc(intervals.map(r => r.prevDate + ' → ' + r.date).join(', ') || 'не отмечено') + '. Суммы не распределяются по календарным дням.<br>' +
      'До даты выбранного прогона — основные EOD, на его дату — выбранный прогон. Предварительное закрытие и intraday видны в интервалах детализации.<br>' +
      'Flash облигаций переоценивает сегодняшний состав и количество на обе даты. Покупки, продажи и закрытия полностью не восстановлены; исторические записи закрытых позиций сохраняются. Полнота всех деривативов и ОВП не заявляется.<br>' +
      'Фондирование уровня книги не входит. Итог — сумма сохранённых компонентов по существующей net-конвенции, включая имеющиеся funding и FX (нетто ОВП). Пропуски компонентов остаются пропусками.<br>' +
      '«Прочее» зависит от метода; разница PV по отчётам показана отдельно. Купоны уже в кэрри; выпуклость входит в точные эффекты; новые сделки в flash — заглушка. Полная экономическая атрибуция остатка не выполнена.<br>' +
      'Разбивка кэрри на купон и амортизацию — только где она сохранена; без неё атрибуция «цена / купон» — «нет данных».<br>' +
      'Проверка gross − суммы gross-компонентов: ' + (s.grossCheck == null ? '—' : f.n(s.grossCheck, 2)) + ' RUB по ' + (s.grossCount || 0) + ' полным записям. Это проверка сохранённого тождества, не независимая сверка и не дополнительный PnL.</details>';
  }

  paintPeriod();
  route();
  if (K.source === 'real') refresh();
})();
