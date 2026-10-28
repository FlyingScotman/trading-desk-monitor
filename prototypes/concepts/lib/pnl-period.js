/* PnL за период (C19): pure helpers for screens A–F. Every number a screen shows comes from here; null stays null. */
(function (root) {
  'use strict';
  // One colour and one name per source on every screen. color: 'pnl:<KIT.PNL_COLORS key>' | 'pal:<KIT.PAL index>' |
  // '--<kit token>'; shade < 1 — a lighter member of the same family (carry, rates, FX).
  const COMPONENTS = [
    { key: 'carry', label: 'Кэрри', color: 'pnl:carry', d: 'Купон и амортизация к номиналу; выплаты купонов уже внутри.' },
    { key: 'carry_coupon', label: 'Купон / НКД', color: 'pnl:carry', d: 'Начисленный купон за интервал (разбивка кэрри, pnl.extra).' },
    { key: 'carry_pull', label: 'Амортизация к номиналу', color: 'pnl:carry', shade: 0.62, d: 'Приближение цены к номиналу со временем (разбивка кэрри, pnl.extra).' },
    { key: 'rolldown', label: 'Roll-down', color: 'pnl:carry', shade: 0.36, d: 'Сохранённый эффект скатывания по кривой.' },
    { key: 'funding', label: 'Фондирование', color: 'pnl:funding', d: 'Только записанное в PnL позиций; фондирование уровня книги не входит.' },
    { key: 'rates', label: 'Ставки', color: 'pnl:rates', d: 'Точная переоценка на сдвиге кривых; нелинейность уже внутри.' },
    { key: 'spread', label: 'Кредитный спред', color: 'pnl:spread', d: 'Сохранённый эффект изменения спреда.' },
    { key: 'convexity', label: 'Выпуклость', color: 'pnl:rates', shade: 0.5, d: 'В текущем flash равна 0: уже включена в точные ставки и спред.' },
    // FX only net (owner, 28.10.2026): the API keeps fx_gross and fx_funding apart, the page shows their sum
    { key: 'fx', label: 'FX (нетто ОВП)', color: 'pnl:fx', parts: ['fx_gross', 'fx_funding'], d: 'Переоценка валютного актива и валютного пассива вместе — нетто открытой валютной позиции.' },
    { key: 'new_trades', label: 'Сделки', color: 'pnl:trading', d: 'В текущем flash 0 — заглушка, не расчёт торгового результата.' },
    { key: 'other', label: 'Необъяснённое / прочее', color: 'pnl:other', hatch: true, d: 'Flash: остаток переоценки; core FX: прочие эффекты ставок, пунктов и времени. Не единый экономический фактор.' },
    { key: 'reported', label: 'Разница PV по отчётам', color: 'pal:10', d: 'Метод desk-reported-v1: разница отчётных PV; сделки могут быть внутри.' },
  ];
  const BY_KEY = Object.fromEntries(COMPONENTS.map(c => [c.key, c]));
  const DERIVED = {
    carryNet: { label: 'Кэрри нетто', color: 'pnl:carry', d: 'Кэрри + roll-down + фондирование.' },
    carryGross: { label: 'Кэрри брутто', color: 'pnl:carry', d: 'Кэрри + roll-down, до фондирования.' },
    carryBase: { label: 'Кэрри без roll-down', color: 'pnl:carry', d: 'Кэрри + фондирование.' },
    price: { label: 'Цена (чистая)', color: 'pal:9', d: 'Итог − купон − FX нетто − фондирование − сделки. Внутри — ставки, спред, roll-down, амортизация и необъяснённое.' },
    gap: { label: 'Не сходится с итогом', color: '--neg', d: 'Итог минус сумма показанных компонент. Должно быть 0.' },
    total: { label: 'Итог', color: '--fg-2', d: 'Сумма сохранённых компонент (net-конвенция).' },
  };
  // Explain order; carry is replaced by the chosen carry view, carry_coupon / carry_pull belong to «Компоненты» and attribution.
  const EXPLAIN = ['rates', 'spread', 'convexity', 'fx', 'new_trades', 'other', 'reported'];
  const OPTIONAL = new Set(['reported']);  // method-specific: hidden when the selection has none
  const MODES = [['explain', 'Explain'], ['curves', 'Explain по кривым'], ['attr', 'Атрибуция'], ['type', 'Типы'], ['book', 'Книги'], ['ccy', 'Валюты'], ['pos', 'Позиции']];
  const BT_MODES = MODES.slice(0, 6).concat([['rating', 'Рейтинг'], ['pos', 'Позиции']]);
  const COMPONENT_MODES = ['explain', 'curves', 'attr'];
  const CARRY_MODES = ['explain', 'curves'];
  const PRESETS = [['day', 'День'], ['week', 'Неделя'], ['mtd', 'MTD'], ['3m', '3М'], ['qtd', 'Квартал'], ['ytd', 'YTD'], ['custom', 'свой']];
  const MONTHS = ['янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const NO_CURVE = 'кривая не указана', NO_RATING = 'без рейтинга';
  const MIN_CONTRIB = 5e3;  // RUB: smaller contributions are not listed (their count is)

  // ---------- missing data ----------
  const num = v => typeof v === 'number' && isFinite(v);
  // sum of the available values; none available — null (not 0)
  function sumAvail(vals) { let s = 0, n = 0; for (const v of vals) if (num(v)) { s += v; n++; } return n ? s : null; }
  // strict: any part missing — null
  function sumAll(vals) { let s = 0; for (const v of vals) { if (!num(v)) return null; s += v; } return s; }
  const val = (a, key) => a && a.c && num(a.c[key]) ? a.c[key] : null;
  // a source's value: a component of the API or the available sum of its parts (FX net: null only when both are null)
  const srcVal = (a, key) => (BY_KEY[key] && BY_KEY[key].parts ? sumAvail(BY_KEY[key].parts.map(k => val(a, k))) : val(a, key));
  const rowsOf = a => a && num(a.rows) ? a.rows : 1;  // a /position day is one row
  // rows that have the component: Agg.m is the count of rows without it
  function coverage(a, key) {
    const rows = rowsOf(a), v = val(a, key);
    const miss = a && a.m && num(a.m[key]) ? a.m[key] : (v == null ? rows : 0);
    const have = v == null ? 0 : Math.max(0, rows - miss);
    return { have, rows, full: rows > 0 && have === rows, none: have === 0 };
  }
  // '' — complete; 'нет данных'; 'N из M записей' — partial (the least covered key)
  function coverText(a, keys) {
    const cs = (Array.isArray(keys) ? keys : [keys]).map(k => coverage(a, k));
    if (!cs.length) return '';
    const w = cs.reduce((x, y) => (y.have / (y.rows || 1) < x.have / (x.rows || 1) ? y : x));
    return w.full ? '' : w.none ? 'нет данных' : w.have + ' из ' + w.rows + ' записей';
  }
  const hasCoupon = a => coverage(a, 'carry_coupon').full;
  const hasSplit = a => coverage(a, 'carry_coupon').full && coverage(a, 'carry_pull').full;

  // Agg sum: totals and components — available sums, counts — plain sums
  function sumAggs(list) {
    list = list.filter(Boolean);
    if (!list.length) return null;
    const keys = new Set(), curves = new Set();
    list.forEach(a => { Object.keys(a.c || {}).forEach(k => keys.add(k)); Object.keys(a.ratesByCurve || {}).forEach(k => curves.add(k)); });
    const cnt = k => list.reduce((s, a) => s + (num(a[k]) ? a[k] : 0), 0);
    const out = { total: sumAvail(list.map(a => a.total)), rows: list.reduce((s, a) => s + rowsOf(a), 0), noTotal: cnt('noTotal'), partialRows: cnt('partialRows'),
      grossCheck: sumAvail(list.map(a => a.grossCheck)), grossCount: cnt('grossCount'), c: {}, m: {} };
    keys.forEach(k => { out.c[k] = sumAvail(list.map(a => val(a, k))); out.m[k] = list.reduce((s, a) => s + (rowsOf(a) - coverage(a, k).have), 0); });
    if (curves.size) { out.ratesByCurve = {}; curves.forEach(id => { out.ratesByCurve[id] = sumAvail(list.map(a => a.ratesByCurve ? a.ratesByCurve[id] : null)); }); }
    return out;
  }

  // ---------- dates, presets, granularity, buckets ----------
  const ms = d => Date.parse(d + 'T00:00:00Z');
  const iso = t => new Date(t).toISOString().slice(0, 10);
  const addDays = (d, n) => iso(ms(d) + n * 864e5);
  const dayDiff = (a, b) => Math.round((ms(b) - ms(a)) / 864e5);
  function presetRange(p, asOf) {
    const y = asOf.slice(0, 4), m = +asOf.slice(5, 7);
    if (p === 'day') return [asOf, asOf];
    if (p === 'week') return [addDays(asOf, -6), asOf];
    if (p === 'mtd') return [asOf.slice(0, 8) + '01', asOf];
    if (p === '3m') { const t = new Date(ms(asOf)); t.setUTCMonth(t.getUTCMonth() - 3); return [addDays(iso(t), 1), asOf]; }
    if (p === 'qtd') return [y + '-' + String(m - (m - 1) % 3).padStart(2, '0') + '-01', asOf];
    if (p === 'ytd') return [y + '-01-01', asOf];
    return null;
  }
  // weekdays in [from, to]: the market-day count is known only after the response, the granularity is a request parameter
  function weekdays(from, to) {
    let n = 0;
    for (let t = ms(from), e = ms(to); t <= e; t += 864e5) { const w = new Date(t).getUTCDay(); if (w && w < 6) n++; }
    return n;
  }
  function autoGran(from, to) { const n = weekdays(from, to); return n <= 25 ? 'day' : n <= 95 ? 'week' : 'month'; }
  const dmy = d => d.slice(8, 10) + '.' + d.slice(5, 7) + '.' + d.slice(0, 4);
  // ISO week of a Monday: the week belongs to the year of its Thursday
  function isoWeek(monday) {
    const th = addDays(monday, 3), y = th.slice(0, 4), jan4 = y + '-01-04', w1 = addDays(jan4, -((new Date(ms(jan4)).getUTCDay() + 6) % 7));
    return y + '-W' + String(dayDiff(w1, monday) / 7 + 1).padStart(2, '0');
  }
  // the server's buckets (API TDM#407): calendar days, ISO weeks from Monday, calendar months, from / to clipped to
  // [from, to]; key 2026-10-28 / 2026-W44 / 2026-10, label 28.10.2026 / 26.10–01.11.2026 / окт 2026. The page shows
  // the server's buckets; this rule is for tests and synthetic responses
  function makeBuckets(from, to, gran) {
    const out = [];
    for (let d = from; d <= to;) {
      let end, key, label;
      if (gran === 'day') { end = d; key = d; label = dmy(d); }
      else if (gran === 'week') {
        const mon = addDays(d, -((new Date(ms(d)).getUTCDay() + 6) % 7)), sun = addDays(mon, 6);
        end = sun; key = isoWeek(mon); label = mon.slice(8, 10) + '.' + mon.slice(5, 7) + '–' + dmy(sun);
      } else { const t = new Date(ms(d)); end = iso(Date.UTC(t.getUTCFullYear(), t.getUTCMonth() + 1, 0)); key = d.slice(0, 7); label = MONTHS[+d.slice(5, 7) - 1] + ' ' + d.slice(0, 4); }
      if (end > to) end = to;
      out.push({ key, label, from: d, to: end });
      d = addDays(end, 1);
    }
    return out;
  }
  const inBucket = (b, d) => d >= b.from && d <= b.to;
  // days summed per bucket; a bucket without saved days — null
  const bucketAggs = (days, buckets) => buckets.map(b => sumAggs(days.filter(d => inBucket(b, d.date))));
  // a bucket without a run: null value plus a market date inside — «нет прогона»; no market date — not a trading period
  const bucketHasMarket = (b, marketDates) => marketDates.some(d => inBucket(b, d));
  // x-axis of daily charts: market dates of the period and dates with rows
  function periodDates(period) {
    const s = new Set((period.marketDates || []).filter(d => d >= period.from && d <= period.to));
    (period.days || []).forEach(d => s.add(d.date));
    return [...s].sort();
  }
  // running sum over a series with gaps: a missing value is a gap, the sum goes on
  function cumulate(vals) { let acc = 0; return vals.map(v => { if (!num(v)) return null; acc += v; return acc; }); }
  // sum of interval lengths (prevDate → date): what the saved days cover, in calendar days
  const calDays = days => days.reduce((s, d) => s + (d.prevDate ? Math.max(0, dayDiff(d.prevDate, d.date)) : 1), 0);
  const perCalDay = (day, v) => !num(v) ? null : v / Math.max(1, day.prevDate ? dayDiff(day.prevDate, day.date) : 1);

  // ---------- preparing a /period response ----------
  // positions get ratesByCurve (their rates on their curve), the summary gets it from the days when absent
  function prepare(period) {
    const days = (period.days || []).slice().sort((a, b) => (a.date < b.date ? -1 : 1));
    const positions = (period.positions || []).map(p => Object.assign({}, p, { ratesByCurve: { [p.curve || '']: val(p, 'rates') } }));
    const summary = Object.assign({}, period.summary);
    if (!summary.ratesByCurve) summary.ratesByCurve = (sumAggs(days.length ? days : positions) || {}).ratesByCurve || {};
    const buckets = period.buckets || [];
    const curves = (period.curves || []).slice();
    const ids = new Set(curves.map(c => c.id));
    positions.forEach(p => { const id = p.curve || ''; if (!ids.has(id)) { ids.add(id); curves.push({ id, name: id || NO_CURVE }); } });
    return { period, days, positions, summary, buckets, curves, keys: period.keys || COMPONENTS.flatMap(c => c.parts || [c.key]),
      bucketAggs: bucketAggs(days, buckets), dates: periodDates(period), split: hasSplit(summary), coupon: hasCoupon(summary),
      curveName: id => (curves.find(c => c.id === id) || {}).name || id || NO_CURVE };
  }

  // ---------- rows of a breakdown ----------
  // def: {k, l, color, shade, hatch, d, parts, v(agg)}; agg — summary, day, bucket, position or a /position day
  const comp = (key, extra) => Object.assign({ k: key, l: BY_KEY[key].label, color: BY_KEY[key].color, shade: BY_KEY[key].shade, hatch: BY_KEY[key].hatch, d: BY_KEY[key].d,
    parts: BY_KEY[key].parts || [key], v: a => srcVal(a, key) }, extra);
  const derived = (k, parts, v) => Object.assign({ k, l: DERIVED[k].label, color: DERIVED[k].color, d: DERIVED[k].d, parts, v });
  // carry: 'net' — one bar; 'split' — gross and funding; 'comp' — coupon, pull-to-par (or carry whole), roll-down, funding
  function carryDefs(carry, split) {
    if (carry === 'net') return [derived('carryNet', ['carry', 'rolldown', 'funding'], a => sumAvail(['carry', 'rolldown', 'funding'].map(k => val(a, k))))];
    if (carry === 'split') return [derived('carryGross', ['carry', 'rolldown'], a => sumAvail([val(a, 'carry'), val(a, 'rolldown')])), comp('funding')];
    return (split ? [comp('carry_coupon'), comp('carry_pull')] : [comp('carry')]).concat([comp('rolldown'), comp('funding')]);
  }
  function attrDefs() {
    // without the coupon split of every row the attribution is «нет данных» (the price would hold the coupon)
    const gate = f => a => hasCoupon(a) ? f(a) : null;
    const z = (a, k) => val(a, k) || 0;  // a missing component is missing from the total too: the identity holds
    return [
      derived('price', ['carry_coupon'], gate(a => a.total == null ? null : a.total - z(a, 'carry_coupon') - z(a, 'fx_gross') - z(a, 'fx_funding') - z(a, 'funding') - z(a, 'new_trades'))),
      Object.assign(comp('carry_coupon'), { v: gate(a => val(a, 'carry_coupon')) }),
      Object.assign(comp('fx'), { parts: ['carry_coupon', 'fx_gross', 'fx_funding'], v: gate(a => srcVal(a, 'fx')) }),
      Object.assign(comp('funding'), { parts: ['carry_coupon', 'funding'], v: gate(a => val(a, 'funding')) }),
      Object.assign(comp('new_trades'), { parts: ['carry_coupon', 'new_trades'], v: gate(a => val(a, 'new_trades')) }),
    ];
  }
  // Explain (one rates bar or one per curve) and attribution; gap — total minus the shown rows
  function componentRows(mode, opt) {
    opt = opt || {};
    if (mode === 'attr') return attrDefs();
    const keys = new Set(opt.keys || COMPONENTS.flatMap(c => c.parts || [c.key]));
    let out = carryDefs(opt.carry || 'net', !!opt.split);
    EXPLAIN.forEach(k => {
      if (!(BY_KEY[k].parts || [k]).some(x => keys.has(x))) return;
      if (k === 'rates' && mode === 'curves') (opt.curves || []).forEach((cv, i, all) => out.push({ k: 'rates:' + cv.id, l: 'Ставки · ' + (cv.name || cv.id || NO_CURVE), color: 'pnl:rates',
        shade: cv.id ? [1, 0.72, 0.52, 0.36, 0.26][i % 5] : 0.2, d: 'Точная переоценка позиций с кривой дисконтирования «' + (cv.name || cv.id || NO_CURVE) + '» — отнесение к одной кривой, а не разложение по кривым.',
        parts: ['rates'], curve: cv.id, v: a => a && a.ratesByCurve && num(a.ratesByCurve[cv.id]) ? a.ratesByCurve[cv.id] : null }));
      else out.push(comp(k));
    });
    const shown = out.slice();
    out.push(derived('gap', [], a => a == null || a.total == null ? null : a.total - (sumAvail(shown.map(d => d.v(a))) || 0)));
    return out;
  }
  // drop what has nothing to say for this selection: the gap within 1000 RUB, method-specific rows without values
  const visibleRows = (defs, summary) => defs.filter(d => d.k === 'gap' ? Math.abs(d.v(summary) || 0) > 1e3 : !(OPTIONAL.has(d.k) && d.v(summary) == null));

  // groups of positions; the label of a type and of a curve comes from the page (labels: {kind(p), curve(id)})
  const GROUPS = {
    type: { title: 'Тип', key: p => (p.group || '—') + '.' + (p.kind || '—'), name: (p, L) => L && L.kind ? L.kind(p) : (p.group || '—') + ' · ' + (p.kind || '—') },
    book: { title: 'Книга', key: p => p.book || '—', name: p => p.book || '—' },
    ccy: { title: 'Валюта', key: p => p.ccy || '—', name: p => p.ccy || '—' },
    curve: { title: 'Кривая', key: p => p.curve || '', name: (p, L) => p.curve ? (L && L.curve ? L.curve(p.curve) : p.curve) : NO_CURVE },
    rating: { title: 'Рейтинг', key: p => p.rating || '', name: p => p.rating || NO_RATING },
    pos: { title: 'Позиция', key: p => p.ref, name: p => (p.name || p.positionId) + ' · ' + p.book },
  };
  const byAbs = (a, b) => (b.v == null) - (a.v == null) || Math.abs(b.v || 0) - Math.abs(a.v || 0);
  function groupBy(positions, mode, labels) {
    const g = GROUPS[mode], m = new Map();
    positions.forEach(p => { const k = g.key(p); if (!m.has(k)) m.set(k, { key: k, l: g.name(p, labels), positions: [] }); m.get(k).positions.push(p); });
    return [...m.values()];
  }
  // a group: total, bucket totals (the positions' own, by bucket index) and the summed Agg for component columns
  function groupRows(positions, mode, labels, nBuckets) {
    return groupBy(positions, mode, labels).map(x => Object.assign(x, {
      n: x.positions.length, v: sumAvail(x.positions.map(p => p.total)), agg: sumAggs(x.positions),
      buckets: Array.from({ length: nBuckets || 0 }, (_, i) => sumAvail(x.positions.map(p => (p.buckets || [])[i]))),
    })).sort(byAbs);
  }
  // rows of any mode: component rows carry total (summary) and buckets (bucket Aggs); group rows — the positions' totals.
  // v(position) — the position's share of the row (contributors list), daily — the row has a value per saved day
  function rowsFor(mode, prep, opt) {
    opt = opt || {};
    if (COMPONENT_MODES.includes(mode)) {
      const defs = visibleRows(componentRows(mode, { carry: CARRY_MODES.includes(mode) ? opt.carry : null, split: prep.split, keys: prep.keys, curves: prep.curves }), prep.summary);
      return defs.map(d => Object.assign(d, { daily: true, total: d.v(prep.summary), buckets: prep.bucketAggs.map(a => (a ? d.v(a) : null)) }));
    }
    const g = GROUPS[mode];
    return groupRows(prep.positions, mode, opt.labels, prep.buckets.length).map((x, i) => ({
      k: 'g:' + x.key, l: x.l, color: i < 7 ? 'pal:' + i : 'pal:13', d: g.title + ': ' + x.n + ' поз.', n: x.n, ref: mode === 'pos' ? x.key : null,
      group: x, total: x.v, buckets: x.buckets, v: p => (g.key(p) === x.key ? p.total : null), parts: [],
    }));
  }
  // chart: up to 8 group rows, the tail as one «Остальные N»
  function chartRows(defs) {
    if (defs.length <= 8 || !defs[0].k.startsWith('g:')) return defs;
    const rest = defs.slice(7);
    return defs.slice(0, 7).concat([{ k: 'rest', l: 'Остальные ' + rest.length, color: 'pal:13', total: sumAvail(rest.map(d => d.total)),
      buckets: rest[0].buckets.map((_, i) => sumAvail(rest.map(d => d.buckets[i]))), v: p => sumAvail(rest.map(d => d.v(p))), parts: [] }]);
  }
  // waterfall: component rows in order; group rows by value, the middle of a long list folded
  function waterfallItems(defs) {
    let items = defs.map(d => ({ k: d.k, l: d.l, v: d.total, color: d.color, shade: d.shade, hatch: d.hatch, d: d.d, parts: d.parts }));
    if (defs.length && defs[0].k.startsWith('g:')) {
      items.sort((a, b) => (b.v == null) - (a.v == null) || (b.v || 0) - (a.v || 0));
      if (items.length > 12) { const mid = items.slice(6, -5); items = items.slice(0, 6).concat([{ k: 'g:*', l: 'Остальные ' + mid.length, v: sumAvail(mid.map(x => x.v)), keys: mid.map(x => x.k) }], items.slice(-5)); }
    }
    return items;
  }
  // who gave a row: positions that added and that subtracted the most, plus how many have no value
  function contrib(positions, def, top) {
    const all = positions.map(p => ({ ref: p.ref, p, v: def.v(p) }));
    const missing = all.filter(x => x.v == null && (!def.k.startsWith('g:') || def.k === 'g:*')).length;
    const per = all.filter(x => x.v != null && Math.abs(x.v) > MIN_CONTRIB).sort((a, b) => b.v - a.v);
    return { plus: per.filter(x => x.v > 0).slice(0, top || 7), minus: per.filter(x => x.v < 0).reverse().slice(0, top || 7), n: per.length, missing, total: sumAvail(all.map(x => x.v)) };
  }

  // ---------- matrix ----------
  // groups → positions × columns (defs); v arrays by column; a group's cells — its summed Agg
  function matrix(positions, mode, defs, labels) {
    const cell = a => defs.map(d => (d.k === 'total' ? (a ? a.total : null) : d.v(a)));
    return groupBy(positions, mode, labels).map(x => ({ key: x.key, l: x.l, n: x.positions.length, agg: sumAggs(x.positions), positions: x.positions,
      kids: x.positions.map(p => ({ ref: p.ref, p, v: cell(p) })) })).map(x => Object.assign(x, { v: cell(x.agg) }));
  }
  const cmpCol = (j, dir) => (a, b) => (a.v[j] == null) - (b.v[j] == null) || dir * ((a.v[j] || 0) - (b.v[j] || 0));
  function sortMatrix(groups, j, dir) { groups.sort(cmpCol(j, dir)); groups.forEach(g => g.kids.sort(cmpCol(j, dir))); return groups; }
  // the largest days of one cell (a /position response) and its running sum
  function cellDays(days, def) {
    const pts = days.map(d => ({ date: d.date, v: def.k === 'total' ? (num(d.total) ? d.total : null) : def.v(d) }));
    return { dates: pts.map(x => x.date), cum: cumulate(pts.map(x => x.v)), top: pts.filter(x => x.v != null && Math.abs(x.v) > 1e3).sort((a, b) => Math.abs(b.v) - Math.abs(a.v)).slice(0, 8), total: sumAvail(pts.map(x => x.v)) };
  }

  // ---------- carry ----------
  const isDeal = p => p.group === 'DER' || p.group === 'MM';
  // net = carry without roll-down (carry + funding) + roll-down; gross = coupon + pull-to-par (or carry whole) + roll-down
  function carryParts(mode, split) {
    if (mode === 'net') return { parts: [derived('carryBase', ['carry', 'funding'], a => sumAvail([val(a, 'carry'), val(a, 'funding')])), comp('rolldown')],
      tot: derived('carryNet', ['carry', 'rolldown', 'funding'], a => sumAvail(['carry', 'rolldown', 'funding'].map(k => val(a, k)))) };
    return { parts: (split ? [comp('carry_coupon'), comp('carry_pull')] : [comp('carry')]).concat([comp('rolldown')]),
      tot: derived('carryGross', ['carry', 'rolldown'], a => sumAvail([val(a, 'carry'), val(a, 'rolldown')])) };
  }
  const annual = (v, pv, days) => (num(v) && num(pv) && Math.abs(pv) > 0 && days > 0 ? v / Math.abs(pv) / (days / 365) * 100 : null);
  // one position: pv avg., parts, gross / net, carry and funding % p.a., margin bp; deals (DER, MM) have no %: PV ≈ 0
  function carryPosition(p) {
    const c = k => val(p, k), sp = hasSplit(p);  // a position's own split, whatever the selection has
    const gross = sumAvail([c('carry'), c('rolldown')]), net = sumAvail([c('carry'), c('rolldown'), c('funding')]);
    const pct = !isDeal(p), cy = pct ? annual(gross, p.pvAvg, p.calDays) : null;
    const fr = !pct ? null : num(p.fundingRatePct) ? p.fundingRatePct : (c('funding') == null ? null : annual(-c('funding'), p.pvAvg, p.calDays));
    const mg = pct ? annual(net, p.pvAvg, p.calDays) : null;
    return { ref: p.ref, p, pv: num(p.pvAvg) ? p.pvAvg : null, coupon: sp ? c('carry_coupon') : null, pull: sp ? c('carry_pull') : null, carry: c('carry'), roll: c('rolldown'),
      gross, funding: c('funding'), net, cy, fr, mg: mg == null ? null : mg * 100 };
  }
  // tiles of C: sums of the selection and % p.a. on the average PV of the positions with a PV (not deals)
  function carrySummary(prep) {
    const s = prep.summary, c = k => val(s, k), bonds = prep.positions.filter(p => !isDeal(p) && num(p.pvAvg));
    const pv = sumAvail(bonds.map(p => p.pvAvg)), days = calDays(prep.days);
    const bsum = k => sumAvail(bonds.map(p => val(p, k)));
    const gross = sumAvail([c('carry'), c('rolldown')]), net = sumAvail([c('carry'), c('rolldown'), c('funding')]);
    const bGross = sumAvail([bsum('carry'), bsum('rolldown')]), bNet = sumAvail([bsum('carry'), bsum('rolldown'), bsum('funding')]);
    const mg = annual(bNet, pv, days);
    return { coupon: prep.split ? c('carry_coupon') : null, pull: prep.split ? c('carry_pull') : null, carry: c('carry'), roll: c('rolldown'), funding: c('funding'), gross, net,
      pv, days, grossPct: annual(bGross, pv, days), fundingPct: annual(bsum('funding') == null ? null : -bsum('funding'), pv, days), marginBp: mg == null ? null : mg * 100,
      cover: { coupon: coverText(s, 'carry_coupon'), pull: coverText(s, 'carry_pull'), funding: coverText(s, 'funding'), carry: coverText(s, ['carry', 'rolldown']) } };
  }
  // group of carry rows: sums (no % — the average of rates is not a sum); coupon / pull only when every row with carry
  // has the split — otherwise a part of the carry would be missing from them, as on the tiles
  const carryTotal = rows => Object.fromEntries(['pv', 'coupon', 'pull', 'carry', 'roll', 'gross', 'funding', 'net'].map(k => [k,
    (k === 'coupon' || k === 'pull') && rows.some(r => r.carry != null && r[k] == null) ? null : sumAvail(rows.map(r => r[k]))]));

  // ---------- tiles of A ----------
  function tilesA(prep) {
    const s = prep.summary, c = k => val(s, k), absExpl = sumAvail(COMPONENTS.filter(x => !['other', 'reported', 'carry_coupon', 'carry_pull'].includes(x.key)).map(x => (srcVal(s, x.key) == null ? null : Math.abs(srcVal(s, x.key)))));
    const un = c('other');
    return { total: s.total, carryNet: sumAvail([c('carry'), c('rolldown'), c('funding')]), carryGross: sumAvail([c('carry'), c('rolldown')]), funding: c('funding'),
      market: sumAvail([c('rates'), c('spread')]), rates: c('rates'), spread: c('spread'), fx: srcVal(s, 'fx'),
      other: un, otherShare: un == null || !absExpl ? null : Math.abs(un) / absExpl * 100,
      cover: { total: s.rows ? '' : 'нет данных', carry: coverText(s, ['carry', 'rolldown', 'funding']), market: coverText(s, ['rates', 'spread']), fx: coverText(s, ['fx_gross', 'fx_funding']), other: coverText(s, 'other') } };
  }

  // ---------- colours (all screens) ----------
  // a source colour: 'pnl:<KIT.PNL_COLORS key>' | 'pal:<KIT.PAL index>' | '--<kit token>'; shade < 1 — toward the panel
  // (light) or the ink (dark). K — the kit (css, rgbOf, PNL_COLORS, PAL)
  function colour(K, spec, shade) {
    const s = String(spec || '--fg-2');
    let c = s.startsWith('pnl:') ? K.PNL_COLORS[s.slice(4)] : s.startsWith('pal:') ? K.PAL[+s.slice(4)] : K.css(s);
    if (!c) c = K.css('--fg-2');
    if (shade == null || shade >= 1) return c;
    const x = K.rgbOf(c), panel = K.rgbOf(K.css('--panel')) || [22, 27, 34];
    if (!x) return c;
    const dark = panel.reduce((a, b) => a + b, 0) < 384, y = dark ? K.rgbOf(K.css('--fg')) || [230, 237, 243] : panel, w = dark ? (1 - shade) * 0.7 : 1 - shade;
    return 'rgb(' + x.map((v, i) => Math.round(v * (1 - w) + y[i] * w)).join(',') + ')';
  }
  // diverging tint of a table cell by sign and size (lighter than K.heat: figures stay readable); good — the sign that
  // reads as green (+1: positive, −1: negative); rgb — [pos, neg]
  function tint(v, max, good, rgb) {
    if (!num(v) || !v || !max) return '';
    const a = Math.min(1, Math.abs(v) / max), c = (good || 1) * v > 0 ? rgb[0] : rgb[1];
    return 'background:rgba(' + c.join(',') + ',' + (0.07 + 0.36 * Math.pow(a, 0.8)).toFixed(3) + ')';
  }

  // ---------- D · Ставки по кривым (/curves) ----------
  // a signed move in bp: «+12» / «−15» / «0» — not a gain or a loss, no accounting brackets; under 10 bp one decimal
  function signedBp(v, d) {
    if (!num(v)) return '—';
    const dp = d == null ? (Math.abs(v) < 10 ? 1 : 0) : d, s = Math.abs(v).toFixed(dp).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    return (/[1-9]/.test(s) ? (v > 0 ? '+' : '−') : '') + s.replace(/^(\d+)\.0+$/, '$1');
  }
  const cell = (src, id, b) => (src && src[id] && num(src[id][b]) ? src[id][b] : null);
  const absMax = rows => Math.max(0, ...rows.flatMap(r => r.vals.filter(num).map(Math.abs)));
  // three curve × term grids of a /curves answer: move (bp), mean DV01 (RUB / bp), linear rates PnL (RUB); Σ — DV01 and
  // PnL only (a sum of moves means nothing); a curve without any value in a grid is not a row of it
  function curveGrids(cv) {
    const B = (cv && cv.buckets) || [], curves = (cv && cv.curves) || [];
    const grid = (src, sum) => {
      const rows = curves.map(c => { const vals = B.map(b => cell(src, c.id, b)); return { id: c.id, name: c.name || c.id, vals, sum: sum ? sumAvail(vals) : null }; })
        .filter(r => r.vals.some(num));
      return { rows, max: absMax(rows) };
    };
    const cov = (cv && cv.coverage) || { rows: 0, linearRows: 0, bucketRows: 0 };
    return { buckets: B, move: grid(cv && cv.move, false), dv01: grid(cv && cv.dv01, true), linear: grid(cv && cv.linear, true), coverage: cov,
      // linear by term is saved only by runs after TDM#406 (pnl.extra.rates_linear_curves); none — «нет данных», not zeros
      linearCover: !cov.rows ? 'нет записей' : !cov.bucketRows ? 'нет данных' : cov.bucketRows < cov.rows ? cov.bucketRows + ' из ' + cov.rows + ' записей' : '' };
  }
  const LIN_WARN_PCT = 3, LIN_WARN_ABS = 1e6;
  // linear (DV01 × move, Σ rates_linear) against exact (Σ rates) by discounting curve; '' — «кривая не указана».
  // The warning: |diff| > 3 % of |linear| and > 1 mn RUB, only when every row has the linear estimate (otherwise rows
  // without it would sit in the difference)
  function linearVsExact(cv, name) {
    const lt = (cv && cv.linearTotal) || {}, ex = (cv && cv.exact) || {}, cov = (cv && cv.coverage) || {}, full = cov.rows > 0 && cov.linearRows === cov.rows;
    const ids = [...new Set(Object.keys(ex).concat(Object.keys(lt)))];
    return ids.map(id => {
      const lin = num(lt[id]) ? lt[id] : null, exact = num(ex[id]) ? ex[id] : null, diff = lin != null && exact != null ? exact - lin : null;
      const pct = diff != null && lin ? diff / Math.abs(lin) * 100 : null;
      return { id, name: name ? name(id) : id || NO_CURVE, linear: lin, exact, diff, pct, warn: full && diff != null && pct != null && Math.abs(diff) > LIN_WARN_ABS && Math.abs(pct) > LIN_WARN_PCT };
    }).sort((a, b) => (a.id === '') - (b.id === '') || Math.abs(b.exact || 0) - Math.abs(a.exact || 0));
  }
  // cumulative rates PnL by curve on the date axis (days' ratesByCurve); a curve without a value in the period is left out
  function curveSeries(prep) {
    const byDate = new Map(prep.days.map(d => [d.date, d]));
    return prep.curves.map(c => ({ id: c.id, name: c.name || c.id || NO_CURVE,
      vals: prep.dates.map(d => (byDate.has(d) && byDate.get(d).ratesByCurve && num(byDate.get(d).ratesByCurve[c.id]) ? byDate.get(d).ratesByCurve[c.id] : null)) }))
      .filter(s => s.vals.some(num)).map(s => Object.assign(s, { cum: cumulate(s.vals), total: sumAvail(s.vals) }));
  }
  // positions on each curve: their exact rates and their share of the curve's
  function curvePositions(prep) {
    const tot = {};
    prep.positions.forEach(p => { const id = p.curve || '', v = val(p, 'rates'); if (v != null) tot[id] = (tot[id] || 0) + v; });
    return prep.positions.filter(p => val(p, 'rates') != null).map(p => {
      const id = p.curve || '', v = val(p, 'rates'), t = tot[id];
      return { ref: p.ref, p, curve: id, rates: v, share: t ? v / Math.abs(t) * 100 : null };
    }).sort((a, b) => (a.curve === '') - (b.curve === '') || (a.curve < b.curve ? -1 : a.curve > b.curve ? 1 : 0) || Math.abs(b.rates) - Math.abs(a.rates));
  }

  // ---------- E · Позиция (/position) and F · Контроль (/checks) ----------
  const SEV = { crit: { icon: '●', label: 'критично', rank: 0 }, warn: { icon: '▲', label: 'внимание', rank: 1 }, info: { icon: '○', label: 'инфо', rank: 2 } };
  // thresholds of F (state.ui): mn RUB and % of the explained → the query of /checks (RUB, share)
  const THR_DEFAULT = { abs: 3, pct: 25 };
  const thrQuery = ui => ({ thr_abs: String((num(ui && ui.thrAbs) ? ui.thrAbs : THR_DEFAULT.abs) * 1e6), thr_pct: String((num(ui && ui.thrPct) ? ui.thrPct : THR_DEFAULT.pct) / 100) });
  // a finding covers its days [date, dateTo]
  const covers = (x, d) => !!x.date && d >= x.date && d <= (x.dateTo || x.date);
  // findings by position; currency findings (fxrev: ref null, the currency as name) — by currency; flagged — positions
  // with a finding above «инфо»
  function findingsIndex(findings) {
    const byRef = new Map(), byCcy = new Map(), flagged = new Set();
    const push = (m, k, x) => { if (!m.has(k)) m.set(k, []); m.get(k).push(x); };
    (findings || []).forEach(x => {
      if (x.ref) { push(byRef, x.ref, x); if (x.sev !== 'info') flagged.add(x.ref); }
      else if (x.check === 'fxrev' && x.name) push(byCcy, x.name, x);
    });
    return { byRef, byCcy, flagged,
      of: (ref, ccy) => (byRef.get(ref) || []).concat(ccy ? byCcy.get(ccy) || [] : []),
      on: (ref, ccy, date) => (byRef.get(ref) || []).concat(ccy ? byCcy.get(ccy) || [] : []).filter(x => covers(x, date)) };
  }
  const worstSev = list => list.reduce((w, x) => (SEV[x.sev] && (!w || SEV[x.sev].rank < SEV[w].rank) ? x.sev : w), null);
  // the list of E: positions of the selection by book, the largest period total first; flag — a finding above «инфо»
  function positionList(positions, flagged) {
    const m = new Map();
    positions.forEach(p => { if (!m.has(p.book)) m.set(p.book, []); m.get(p.book).push({ ref: p.ref, p, total: num(p.total) ? p.total : null, flag: !!(flagged && flagged.has(p.ref)) }); });
    return [...m.keys()].sort().map(book => ({ book, items: m.get(book).sort((a, b) => (a.total == null) - (b.total == null) || Math.abs(b.total || 0) - Math.abs(a.total || 0)) }));
  }
  // the same instrument in other books of the selection (their histories are not merged)
  const otherBooks = (positions, p) => (p && p.instrument ? [...new Set(positions.filter(x => x.instrument === p.instrument && x.book !== p.book).map(x => x.book))].sort() : []);
  // columns of E's day table: the source keys with a value on some day (FX net as one); the carry split replaces the
  // carry only when every day with carry has it; «Необъяснённое» always
  function dayColumns(days) {
    const has = k => days.some(d => { const v = srcVal(d, k); return v != null && Math.abs(v) >= 0.5; });
    const split = days.some(d => val(d, 'carry') != null) && days.every(d => val(d, 'carry') == null || (val(d, 'carry_coupon') != null && val(d, 'carry_pull') != null));
    const keys = (split ? ['carry_coupon', 'carry_pull'] : ['carry']).concat(['rolldown', 'funding', 'rates', 'spread', 'convexity', 'fx', 'new_trades']).filter(has);
    keys.push('other');
    if (has('reported')) keys.push('reported');
    return keys;
  }
  // one row of E's day table: inputs of D−1 and D, Δ yield and Δ spread in bp (null without both ends), the interval
  const dDiff = (a, b, k) => (num(a) && num(b) ? (b - a) * k : null);
  function dayRow(d) {
    const i = d.inputs || {};
    return { date: d.date, prevDate: d.prevDate, cal: d.prevDate ? dayDiff(d.prevDate, d.date) : null, src: i.source || null,
      cleanPrev: num(i.cleanPrev) ? i.cleanPrev : null, clean: num(i.clean) ? i.clean : null, dy: dDiff(i.ytmPrev, i.ytm, 100), dz: dDiff(i.zPrev, i.z, 1),
      dv01: num(i.dv01) ? i.dv01 : null, quantity: num(i.quantity) ? i.quantity : null, quantityPrev: num(i.quantityPrev) ? i.quantityPrev : null,
      total: num(d.total) ? d.total : null, ratesLinear: num(d.ratesLinear) ? d.ratesLinear : null, gap: num(d.grossCheck) ? d.grossCheck : null,
      events: d.events || [], noPnl: d.noPnl || null, day: d };
  }

  // calendar of F: market days and run days of the period — 'norun' (market day without a run), 'issue' (a finding above
  // «инфо» other than the missing run itself covers the day), 'ok'; month — the first day of a new month
  function calendar(ck) {
    const cov = (ck && ck.coverage) || {}, missing = new Set(cov.missingRuns || []), runs = new Set(cov.runDates || []);
    const dates = [...new Set((cov.marketDates || []).concat(cov.runDates || []))].sort();
    const bad = ((ck && ck.findings) || []).filter(x => x.sev !== 'info' && x.check !== 'norun');
    return dates.map((d, i) => {
      const n = bad.filter(x => covers(x, d)).length;
      return { date: d, state: missing.has(d) && !runs.has(d) ? 'norun' : n ? 'issue' : 'ok', n, month: i > 0 && d.slice(0, 7) !== dates[i - 1].slice(0, 7) };
    });
  }
  // heat map of F: unexplained (other) positions × days; the scale is capped at the threshold (above — full colour), at
  // least 0.1 mn RUB; rows — positions by the largest |other| (largest on top: the y axis goes up)
  function heatmap(un, thrAbs) {
    const dates = (un && un.dates) || [], refs = (un && un.refs) || [], cells = ((un && un.cells) || []).filter(c => num(c[2]));
    const peak = refs.map(() => 0);
    cells.forEach(([, y, v]) => { peak[y] = Math.max(peak[y], Math.abs(v)); });
    const order = refs.map((r, i) => i).sort((a, b) => peak[a] - peak[b] || (refs[a] < refs[b] ? 1 : -1));  // bottom → top
    const at = new Map(order.map((y, i) => [y, i]));
    const max = Math.max(0, ...peak), thr = num(thrAbs) && thrAbs > 0 ? thrAbs : max;
    return { dates, refs: order.map(i => refs[i]), data: cells.map(([x, y, v]) => [x, at.get(y), v]), max, cap: Math.max(1e5, Math.min(max, thr)) };
  }
  // what a finding's value is (API TDM#407): an amount in RUB, a price, a share of the FX move, a quantity
  const VALUE_KIND = { recon: 'rub', unexpl: 'rub', fsign: 'rub', lin: 'rub', outlier: 'rub', stale: 'price', fxrev: 'share', life: 'qty' };
  // check cards: status, findings by severity (worst first); the list of the page keeps the API order (sev, then date)
  function checkCards(ck) {
    const fs = (ck && ck.findings) || [];
    return ((ck && ck.checks) || []).map(c => {
      const own = fs.filter(x => x.check === c.key), by = {};
      own.forEach(x => { by[x.sev] = (by[x.sev] || 0) + 1; });
      return Object.assign({}, c, { n: own.length, bySev: Object.keys(SEV).filter(s => by[s]).map(s => [s, by[s]]), worst: worstSev(own) });
    });
  }

  const api = { COMPONENTS, BY_KEY, DERIVED, MODES, BT_MODES, COMPONENT_MODES, CARRY_MODES, PRESETS, GROUPS, NO_CURVE, NO_RATING, MIN_CONTRIB,
    colour, tint, signedBp, curveGrids, linearVsExact, curveSeries, curvePositions, LIN_WARN_PCT, LIN_WARN_ABS,
    SEV, THR_DEFAULT, thrQuery, covers, findingsIndex, worstSev, positionList, otherBooks, dayColumns, dayRow, calendar, heatmap, VALUE_KIND, checkCards,
    sumAvail, sumAll, val, srcVal, coverage, coverText, hasCoupon, hasSplit, sumAggs,
    addDays, dayDiff, presetRange, weekdays, autoGran, isoWeek, makeBuckets, bucketAggs, bucketHasMarket, periodDates, cumulate, calDays, perCalDay,
    prepare, componentRows, carryDefs, attrDefs, visibleRows, groupRows, rowsFor, chartRows, waterfallItems, contrib,
    matrix, sortMatrix, cellDays, isDeal, carryParts, carryPosition, carrySummary, carryTotal, annual, tilesA };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PNL_PERIOD = api;
})(typeof window === 'object' ? window : globalThis);
