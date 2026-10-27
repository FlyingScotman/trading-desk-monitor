/* C19. The server owns aggregation; this page renders one selected slice and requests only one position's days. */
(function () {
  'use strict';
  const K = KIT, H = PNL_HISTORY, esc = K.esc, $ = id => document.getElementById(id);
  K.top({ title: 'C19 · История PnL', tag: 'сохранённые результаты · ₽' });
  const keys = H.factors.map(f => f[0]), labels = Object.fromEntries(H.factors.map(f => [f[0], f[1]]));
  const st = { from: MOCK.asOf.slice(0, 7) + '-01', to: MOCK.asOf, components: keys.slice(), assetClass: '', issuer: '', q: '', mode: 'cumulative', position: null };
  let data = null, detail = null, catalogue = [], cataloguePeriod = null, chart = null, positionChart = null;
  const fmt = v => v == null ? '—' : K.f.n(v, 2), signed = v => '<span class="' + K.f.cls(v) + '">' + fmt(v) + '</span>';
  const partial = x => x.rowCount ? (x.rowCount + ' зап.' + (x.noTotal ? ' · без итога ' + x.noTotal : '') + (x.partialRows ? ' · неполных ' + x.partialRows : '')) : 'нет записей';
  function query(position) {
    const q = new URLSearchParams({ from: st.from, to: st.to, run_id: MOCK.run.id, components: st.components.join(','),
      catalogue: String(!position && cataloguePeriod !== st.from + '/' + st.to) });
    for (const k of ['entity', 'desk', 'ccy']) if (K.filter[k]) q.set(k, K.filter[k]);
    (K.filter.books || []).forEach(v => q.append('books', v));
    (K.filter.portsOff || []).forEach(v => q.append('ports_off', v));
    for (const [k, v] of [['asset_class', st.assetClass], ['issuer', st.issuer], ['q', st.q]]) if (v) q.set(k, v);
    if (position) { q.set('position_book', position.book); q.set('position_id', position.positionId); }
    return q;
  }
  async function fetchReport(q) {
    const r = await fetch('../api/v1/history/pnl?' + q, { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status === 401 ? 'Сессия закончилась. Войдите снова.' : 'История не загружена (HTTP ' + r.status + ').');
    return r.json();
  }
  const detailLoad = H.loader(fetchReport, result => {
    detail = result.data || null;
    if (result.loading) { disposePositionChart(); $('days').textContent = 'Загружаю дни выбранной позиции…'; }
    else if (result.error) { disposePositionChart(); $('days').textContent = result.error.message; }
    else drawDetail();
  });
  const load = H.loader(fetchReport, result => {
    data = result.data || null;
    $('result').hidden = !data;
    if (result.loading) { $('status').textContent = 'Загружаю выбранный период и фильтры…'; detailLoad.invalidate(); detail = null; if (chart) chart.dispose(); chart = null; disposePositionChart(); }
    else if (result.error) $('status').textContent = result.error.message;
    else {
      $('status').textContent = '';
      if (data.catalogue) {
        catalogue = data.catalogue; cataloguePeriod = data.from + '/' + data.to;
        K.setFilterScope(H.scope(catalogue)); fillOptions();
      }
      draw();
    }
  });
  function refresh() {
    if (K.source !== 'real') return;
    if (!st.from || !st.to || st.from > st.to || st.to > MOCK.asOf) {
      load.invalidate(); detailLoad.invalidate(); data = detail = null; $('result').hidden = true;
      if (chart) chart.dispose(); chart = null; disposePositionChart();
      $('status').textContent = 'Выберите начало ≤ конец ≤ дата выбранного прогона (' + MOCK.asOf + ').'; return;
    }
    load(query());
  }
  $('from').value = st.from; $('to').value = st.to;
  $('from').max = $('to').max = MOCK.asOf;
  for (const id of ['from', 'to']) $(id).onchange = () => { st[id] = $(id).value; refresh(); };
  function fillOptions() {
    for (const id of ['ccy', 'assetClass', 'issuer']) {
      const value = id === 'ccy' ? K.filter.ccy || '' : st[id];
      const values = [...new Set(catalogue.map(r => r[id]).filter(Boolean))].sort();
      if (value && !values.includes(value)) values.push(value);
      $(id).innerHTML = '<option value="">Все</option>' + values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
      $(id).value = value;
    }
  }
  $('ccy').onchange = () => K.setFilter({ ccy: $('ccy').value || null });
  for (const id of ['assetClass', 'issuer']) $(id).onchange = () => { st[id] = $(id).value; refresh(); };
  $('search').oninput = () => { st.q = $('search').value; refresh(); };
  $('reset').onclick = () => { st.assetClass = st.issuer = st.q = ''; $('search').value = ''; fillOptions(); K.setFilter({ entity: null, desk: null, books: null, ccy: null, portsOff: null }); };
  $('allFactors').onclick = () => { st.components = keys.slice(); refresh(); };
  $('noFactors').onclick = () => { st.components = []; refresh(); };
  K.onFilter(() => { $('ccy').value = K.filter.ccy || ''; refresh(); });
  K.on('theme', () => { if (data) draw(); });
  function table(headers, rows) {
    return '<table class="k-tbl"><thead><tr>' + headers.map(h => '<th>' + h + '</th>').join('') + '</tr></thead><tbody>' + rows.join('') + '</tbody></table>';
  }
  function chartOptions(days) {
    const cumulative = st.mode === 'cumulative';
    return { animation: false, grid: { left: 10, right: 20, top: 35, bottom: 8, containLabel: true },
      legend: { data: ['Доступный итог', 'Выбранные компоненты'] }, tooltip: { trigger: 'axis', valueFormatter: fmt },
      xAxis: { type: 'category', data: days.map(d => d.date) }, yAxis: { type: 'value', name: 'тыс ₽', axisLabel: { formatter: v => K.tick(v, 1e3) } },
      series: [['Доступный итог', cumulative ? 'cumulative' : 'total', K.css('--fg')], ['Выбранные компоненты', cumulative ? 'selectedCumulative' : 'selected', K.css('--accent')]].map(([name, key, color]) =>
        ({ name, type: cumulative ? 'line' : 'bar', data: days.map(d => d[key]), connectNulls: false, showSymbol: true, symbolSize: 5, color })) };
  }
  function draw() {
    const a = data.summary;
    $('total').innerHTML = signed(a.total) + '<div class="k-note">' + partial(a) + '</div>';
    $('selected').innerHTML = signed(a.selected) + '<div class="k-note">' + st.components.length + ' компонентов · общий итог слева сохраняется</div>';
    const preliminary = data.runs.filter(r => r.cutType === 'eod_preliminary').length, intraday = data.runs.filter(r => r.cutType === 'intraday' || r.cutType === 'prev_close').length;
    $('counts').innerHTML = data.positions.length + ' поз. · ' + data.days.filter(d => d.total != null).length + ' дат<div class="k-note">с доступным итогом · ' + data.runs.length + ' прогонов' + (preliminary ? ' · предварительных ' + preliminary : '') + (intraday ? ' · внутри дня ' + intraday : '') + '</div>';
    const anchor = data.separateAnchor;
    $('anchor').hidden = !anchor;
    if (anchor) {
      const ac = Object.fromEntries(anchor.summary.components.map(c => [c.key, c.value]));
      $('anchorSummary').innerHTML = '<b>' + esc(anchor.run.slice) + '</b> · ' + esc(anchor.run.prevDate + ' → ' + anchor.run.date) +
        ' · итог ' + signed(anchor.summary.total) + ' ₽ · выбрано ' + signed(anchor.summary.selected) + ' ₽. ' +
        'Интервал пересекает включённые даты ' + esc(anchor.overlappingDates.join(', ')) + '; в итоги периода и накопление не входит. ' + partial(anchor.summary) +
        '. Прочее: ' + signed(ac.other) + ' ₽; разница PV по отчётам: ' + signed(ac.reported) + ' ₽.';
      $('anchorPositions').innerHTML = table(['Позиция · книга', 'Итог, ₽', 'Выбрано, ₽'], anchor.positions.map((p, i) => '<tr><td><button class="link" data-anchor-position="' + i + '">' + esc(p.row.name + ' · ' + p.row.book) + '</button></td><td class="n">' + signed(p.total) + '</td><td class="n">' + signed(p.selected) + '</td></tr>'));
      $('anchorPositions').querySelectorAll('[data-anchor-position]').forEach(el => el.onclick = () => { st.position = anchor.positions[+el.dataset.anchorPosition].id; openDetail(); });
      K.tableToolsAll($('anchorPositions'));
    }
    K.seg($('mode'), [['cumulative', 'Накопленно'], ['daily', 'По дням']], st.mode, v => { st.mode = v; drawChart(); if (detail) drawDetail(); });
    drawChart();
    const byKey = Object.fromEntries(a.components.map(c => [c.key, c]));
    $('otherStatus').innerHTML = 'Прочее по методу: ' + signed(byKey.other.value) + ' ₽ · разница PV по отчётам: ' + signed(byKey.reported.value) + ' ₽. Состав и методы — в компонентах и днях позиции; эти числа видны при любом выборе источников.';
    $('components').innerHTML = table(['Источник', 'Доступная сумма, ₽'], H.factors.map(([key, label, note]) => {
      const c = byKey[key];
      return '<tr><td><label><input type="checkbox" data-factor="' + key + '" ' + (st.components.includes(key) ? 'checked' : '') + '> ' + label + '</label><div class="k-note">' + note + '</div></td><td class="n">' + signed(c.value) + '<div class="k-note">' + (c.count ? c.missing + ' пропусков из ' + c.count : 'нет записей этого метода') + '</div></td></tr>';
    }));
    $('components').querySelectorAll('[data-factor]').forEach(el => el.onchange = () => { st.components = [...$('components').querySelectorAll('[data-factor]:checked')].map(x => x.dataset.factor); refresh(); });
    const positions = data.positions.slice().sort((a, b) => Math.abs(b.selected ?? 0) - Math.abs(a.selected ?? 0));
    $('positions').innerHTML = table(['Позиция · книга', 'Итог, ₽', 'Выбрано, ₽', 'Доступность'], positions.map((p, i) => '<tr class="' + (p.id === st.position ? 'active' : '') + '"><td><button class="link" data-position="' + i + '">' + esc(p.row.name) + ' · ' + esc(p.row.book) + '</button><div class="k-note">' + esc(p.row.positionId) + ' · ' + esc(p.row.ccy || '—') + '</div></td><td class="n">' + signed(p.total) + '</td><td class="n">' + signed(p.selected) + '</td><td class="k-note">' + partial(p) + '</td></tr>'));
    $('positions').querySelectorAll('[data-position]').forEach(el => el.onclick = () => { st.position = positions[+el.dataset.position].id; openDetail(); });
    $('books').innerHTML = table(['Книга', 'Итог, ₽', 'Выбрано, ₽', 'Доступность'], data.books.map((b, i) => '<tr><td><button class="link" data-book="' + i + '">' + esc(b.id) + '</button></td><td class="n">' + signed(b.total) + '</td><td class="n">' + signed(b.selected) + '</td><td class="k-note">' + partial(b) + '</td></tr>'));
    $('books').querySelectorAll('[data-book]').forEach(el => el.onclick = () => K.setFilter({ books: [data.books[+el.dataset.book].id] }));
    K.tableToolsAll($('positions')); K.tableToolsAll($('books')); K.tableToolsAll($('components'));
    drawCoverage();
    if (!findPosition()) st.position = positions[0]?.id || anchor?.positions[0]?.id || null;
    openDetail();
  }
  function drawChart() {
    if (chart) chart.dispose();
    const days = data.days;
    chart = K.chart($('chart'), () => chartOptions(days));
  }
  function disposePositionChart() { if (positionChart) positionChart.dispose(); positionChart = null; }
  function findPosition() { return data.positions.find(p => p.id === st.position) || data.separateAnchor?.positions.find(p => p.id === st.position); }
  function openDetail() {
    const p = findPosition();
    disposePositionChart();
    $('detailName').textContent = p ? p.row.name + ' · ' + p.row.book : 'нет позиций в выборке';
    if (!p) { detailLoad.invalidate(); detail = null; $('days').textContent = 'Нет сохранённых строк для этой выборки.'; return; }
    detailLoad(query(p.row));
  }
  function drawDetail() {
    disposePositionChart();
    const days = detail.days;
    positionChart = K.chart($('positionChart'), () => chartOptions(days));
    const byDate = Object.fromEntries(detail.observations.map(r => [r.date, r]));
    const runs = Object.fromEntries(detail.runs.map(r => [r.date, r]));
    const anchor = detail.separateAnchor;
    if (anchor) {
      runs[anchor.run.date] = anchor.run;
      anchor.observations.forEach(r => { byDate[r.date] = r; });
    }
    $('days').innerHTML = table(['Дата / интервал ↓', 'Итог, ₽', 'Выбрано, ₽', 'Метод и доступные компоненты'], detail.days.slice().reverse().map(d => {
      const r = byDate[d.date], run = runs[d.date], separate = anchor && d.date === anchor.run.date;
      const note = r ? (r.method || 'метод не записан') + (r.note ? ' · ' + r.note : '') : run ? 'Нет строки позиции; отсутствие позиции и отсутствие данных здесь не различимы.' : 'Нет сохранённого прогона.';
      const comp = r ? H.factors.map(([k, n]) => n + ': ' + fmt(r.components[k])).join(' · ') : '';
      const reason = r && r.noPnl ? ' · ' + (r.noPnl.reason || '') + ': ' + (r.noPnl.detail || '') : '';
      const missing = r ? ' · пропуски: ' + (r.missing.map(k => labels[k] || k).join(', ') || 'нет') : '';
      return '<tr><td>' + esc(d.date) + (separate ? '<div>Отдельно, не в накоплении</div>' : '') + '<div class="k-note">' + esc(run ? (r?.prevDate || run.prevDate || '?') + ' → ' + d.date + ' · ' + run.slice : 'нет прогона') + '</div></td><td class="n">' + signed(separate ? anchor.summary.total : d.total) + '</td><td class="n">' + signed(separate ? anchor.summary.selected : d.selected) + '</td><td><div>' + esc(note + reason + missing) + '</div><div class="k-note">' + esc(comp) + '</div>' + (r ? '<div class="k-note">Сохранённый gross: ' + fmt(r.gross) + '; gross − доступные полные gross-компоненты: ' + fmt(r.grossCheck) + '</div>' : '') + '</td></tr>';
    }));
    K.tableToolsAll($('days'));
  }
  function drawCoverage() {
    const runs = new Set(data.runs.map(r => r.date)), market = new Set(data.marketDates);
    const missing = data.days.filter(d => !runs.has(d.date) && d.date !== data.separateAnchor?.run.date), expected = missing.filter(d => market.has(d.date));
    const unknown = missing.filter(d => !market.has(d.date));
    const intervals = data.runs.filter(r => r.prevDate && r.prevDate < st.from);
    $('coverage').innerHTML = '<b>Частичная сумма сохранённых результатов.</b> ' + partial(data.summary) + '.<br>' +
      'Закрытий без прогона: ' + expected.length + '; дат без сведений о закрытии и прогоне: ' + unknown.length +
      (data.intradayOnlyDates.length ? '; только intraday, нет закрытия дня: ' + data.intradayOnlyDates.length : '') +
      '. <details><summary>Даты пропусков и ограничения метода</summary><p>Это не официальный торговый PnL.</p>' +
      'Нет закрытия дня, сохранён только intraday: ' + esc(data.intradayOnlyDates.join(', ') || 'таких дат нет') + '. Эти дни не входят в накопление.<br>' +
      'Есть закрытие рынка, нет прогона: ' + esc(expected.map(d => d.date).join(', ') || 'таких дат нет') + '.<br>' +
      'Нет прогона и нет сведений о закрытии (включая выходные; ожидаемость неизвестна): ' + esc(unknown.map(d => d.date).join(', ') || 'таких дат нет') + '.<br>' +
      'Позиции: ' + data.positions.length + '; записи: ' + data.summary.rowCount + '; ' + partial(data.summary) + '. Отсутствие строки не означает нулевой результат.<br>' +
      'Граница периода — дата результата; интервал может начинаться раньше: ' + esc(intervals.map(r => r.prevDate + ' → ' + r.date).join(', ') || 'не отмечено') + '. Суммы не распределяются по календарным дням.<br>' +
      'До даты выбранного прогона — основные EOD, на его дату — выбранный прогон. Предварительное закрытие и intraday видны в интервалах детализации.<br>' +
      'Flash облигаций переоценивает сегодняшний состав и количество на обе даты. Покупки, продажи и закрытия полностью не восстановлены; исторические записи закрытых позиций сохраняются. Полнота всех деривативов и ОВП не заявляется.<br>' +
      'Фондирование уровня книги не входит. Доступный итог — сумма сохранённых компонентов по существующей net-конвенции, включая имеющиеся funding и FX funding. Пропуски компонентов остаются пропусками.<br>' +
      '«Прочее» зависит от метода; разница PV по отчётам показана отдельно. Купоны уже в кэрри; выпуклость входит в точные эффекты; новые сделки в flash — заглушка. Полная экономическая атрибуция остатка не выполнена.<br>' +
      'Проверка gross − суммы gross-компонентов: ' + fmt(data.summary.grossCheck) + ' ₽ по ' + data.summary.grossCount + ' полным записям. Это проверка сохранённого тождества, не независимая сверка и не дополнительный PnL.</details>';
  }
  if (K.source === 'real') refresh();
  else $('status').textContent = 'История PnL читает сохранённые прогоны сервера. В файловом демо истории прогонов нет.';
})();
