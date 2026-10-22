/*
 * window.MOCK из настоящего прогона: window.DESK (/desk-data.js, api/compat.py) и window.DESK_EXTRAS
 * (/concepts-data.js, api/concepts.py). Та же форма, что у демо-набора lib/mock.js, поэтому страницы концептов
 * работают на ней без изменений. Чего в данных нет — честный null (страницы показывают «—»), а не выдуманное число;
 * что именно отсутствует, перечислено в MOCK.notes и видно в шапке (kit.js).
 *
 * Соответствия (держать в согласии с api/concepts.py):
 *  - кривые: RUB RUONIA, RUB KEY — как в прогоне; RUB OFZ страниц — базис ОФЗ − RUONIA (КБД минус RUONIA, б.п.),
 *    его риск — ofzSpr01 позиции по бакетам срока (ядро 0.33, extra.spread_buckets); риск прогона на «RUB OFZ» (КБД: только в день без RUONIA)
 *    добавлен к RUONIA; кривые валют USD/EUR/CNY — USD SOFR / EUR ESTR / CNY XCCY (точек нет, только риск);
 *  - тип: GOV PD/PK/IN/SOVFX, SUB FIX/FLT, CORP FIX/FLT, DER OTC/FX, OTH (api/concepts.kind_of); тип инструмента
 *    таблицы «валюта × тип» (ОФЗ-ПД, свопы, фьючерсы… по группе продукта отчёта) — lib/instr.js (api/concepts.instr_kind)
 *    из instrType, assetClass, segment, productGroup (extra.reported.x_product_group) и fxDeltaBy позиции;
 *  - срок — до оферты или колла, по которым считается доходность, иначе до погашения;
 *  - у флоатера доходности нет (ytm = null), спред — дисконт-маржа ядра (extra.dm_bp: спред к кривой проекции; в
 *    прогоне до ядра 0.33 — Z-спред); G-спред — ядра (extra.g_spread_bp, определение MOEX), иначе MOEX;
 *  - CS01 и базис по бакетам срока — ядра (extra.spread_buckets: CS по бакетам пропорционально ставочному риску
 *    дисконтирования); в прогоне до ядра 0.33 — по двум соседним бакетам срока;
 *  - PnL: кэрри = carry + rolldown, ставки = rates + convexity, FX = fx, сделки = newTrades, прочее = funding + other +
 *    reported; итог = explained.
 * История позиции (M.posHistory) — синхронный запрос /api/v1/history/positions: первые две позиции по одной,
 * дальше сразу все (страницы C11, C13 просят историю всех позиций). Кроме цен рынка в ней номинал, DV01, CS01,
 * базис, MV и DV01 по кривым каждого прогона (M.histSource = 'runs': история риска — настоящая, не «номинал ×
 * сегодняшние чувствительности»).
 * Календарь рынка (M.marketCal) — тоже лениво, /api/v1/calendar/market: агрегаты по дням, типам и валютам из
 * статики хаба; строки за ячейкой — M.marketCal.flows({...}) тем же запросом с фильтром.
 * M.posHistory(id, { deep: true }) —
 * вся история бумаги (с 2024-01-03 через API истории хаба: карточка), с Z-спредом MOEX (zspr), дисконт-маржой
 * флоатера по прогонам (dm) и ICPI / IR / BEI линкера, когда они есть.
 * История рынка (M.marketHistory()) — тоже лениво, синхронно, один раз: /api/v1/history/market (изменения выпусков
 * за 1Д/1Н/1М/3М/с начала года, индексы и фиксинги, курсы ЦБ, кривые RUONIA OIS / КС-свопов / КБД по датам);
 * null, если её нет. Страницы, которым она не нужна, её не грузят.
 */
(function () {
  'use strict';
  const D = window.DESK, X = window.DESK_EXTRAS || {};
  if (!D) {  // в базе нет прогона: демо-набор, шапка скажет об этом
    window.CONCEPTS_SOURCE = window.DESK_EMPTY ? 'empty' : 'demo';
    document.write('<script src="lib/mock.js"><\/script>');
    return;
  }
  window.CONCEPTS_SOURCE = 'real';
  const M = window.MOCK = {};

  // ---------- даты и мелочи (как в mock.js) ----------
  const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const parse = s => new Date(s + 'T00:00:00');
  const D0 = parse(D.asOf);
  const yearsTo = s => s ? (parse(s) - D0) / (365.25 * 864e5) : null;
  const addDays = (s, n) => { const d = parse(s); d.setDate(d.getDate() + n); return iso(d); };
  const addMonths = (s, n) => { const d = parse(s); const day = d.getDate(); d.setDate(1); d.setMonth(d.getMonth() + n); const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate(); d.setDate(Math.min(day, last)); return iso(d); };
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rng(s) { let x = hash(String(s)) || 1; return () => { x ^= x << 13; x >>>= 0; x ^= x >> 17; x ^= x << 5; x >>>= 0; return x / 4294967296; }; }
  const round = (v, d) => v == null ? null : Math.round(v * Math.pow(10, d)) / Math.pow(10, d);
  const num = v => (v == null || !isFinite(v)) ? null : +v;
  M.util = { iso, parse, yearsTo, addDays, addMonths, rng, hash, round };
  M.source = 'real';
  M.asOf = D.asOf;
  M.prevDate = D.prevDate;
  const src = D.source || {};
  M.run = { id: src.runId, runType: src.runType, slice: src.slice, cutType: src.cutType, publishedAt: src.publishedAt, pnlBasis: src.pnlBasis };
  M.cut = { type: src.cutType || 'eod_final', time: src.cutTs ? src.cutTs.slice(11, 16) : '', label: src.slice || src.cutType || '' };
  M.notes = (X.notes || []).slice();
  const runQ = 'run_id=' + encodeURIComponent((D.source || {}).runId || '');
  (D.riskNotes || []).forEach(n => M.notes.push(n.text + (n.detail ? ' (' + n.detail + ')' : '')));
  if (!D.pnlAvailable && D.pnlReasonText) M.notes.push('PnL: ' + D.pnlReasonText);
  if (!window.DESK_EXTRAS) M.notes.push('нет данных /concepts-data.js: история, потоки и рынок пусты');

  // ---------- валюты, курсы, индексы ----------
  const BASE_CCY = ['RUB', 'USD', 'EUR', 'CNY'];
  const posCcy = [...new Set(D.positions.map(p => p.ccy).filter(Boolean))];
  M.ccys = BASE_CCY.filter(c => c === 'RUB' || posCcy.includes(c) || D.FX[c] != null).concat(posCcy.filter(c => !BASE_CCY.includes(c)));
  M.ccyOrder = Object.fromEntries(M.ccys.map((c, i) => [c, i]));
  M.fx = Object.assign({ RUB: 1 }, D.FX);
  M.ccys.forEach(c => { if (M.fx[c] == null) { M.fx[c] = 1; M.notes.push('нет курса ' + c + ': суммы этой валюты не пересчитаны'); } });
  M.fxChgPct = Object.assign({ RUB: 0 }, D.FX_MOVE_PCT);
  // курс, по которому посчитан PnL FX прогона: рыночный (решение владельца 21.10, R26 — MOEX 19:00, иначе 14:00,
  // иначе закрытие CETS, иначе ЦБ; источник по валюте — D.FX_SOURCE), то есть курс шапки; курс ЦБ, действующий на
  // дату (официальный PnL), — рядом (M.fxCbr). Прогон до R26 (FX_SOURCE нет) считал PnL FX по курсу ЦБ.
  M.fxSource = Object.assign({}, D.FX_SOURCE || {});
  M.fxCbr = D.FX_CBR ? { label: 'ЦБ', rate: Object.assign({ RUB: 1 }, D.FX_CBR), chgPct: Object.assign({ RUB: 0 }, D.FX_CBR_MOVE_PCT) } : null;
  const fxLabels = [...new Set(Object.values(M.fxSource).filter(Boolean))];
  if (fxLabels.length) M.fxPnl = { label: fxLabels.join(' / '), rate: Object.assign({}, M.fx), chgPct: Object.assign({}, M.fxChgPct), source: M.fxSource };
  else if (M.fxCbr) M.fxPnl = M.fxCbr;
  M.indices = Object.assign({}, D.INDICES);
  M.buckets = D.BUCKETS.slice();
  M.pillars = D.PILLARS.slice();
  const NB = M.buckets.length;
  const zeros = () => new Array(NB).fill(0);

  // ---------- кривые ----------
  const CURVE_MAP = Object.assign({ 'RUB OFZ': 'RUB RUONIA', USD: 'USD SOFR', EUR: 'EUR ESTR', CNY: 'CNY XCCY' }, X.curveMap || {});
  const C = D.CURVES || {}, MV = D.CURVE_MOVES || {};
  const pts = id => (C[id] && C[id].pts) ? C[id].pts.map(num) : new Array(NB).fill(null);
  const chg = id => (MV[id] || zeros()).map(v => num(v) || 0);
  const kbd = pts('RUB OFZ'), ruo = pts('RUB RUONIA');
  M.curves = [
    { id: 'RUB RUONIA', ccy: 'RUB', name: (C['RUB RUONIA'] || {}).name || 'RUONIA OIS', short: 'RUONIA', kind: 'rate', pts: ruo, chg: chg('RUB RUONIA') },
    { id: 'RUB KEY', ccy: 'RUB', name: (C['RUB KEY'] || {}).name || 'Ключевая ставка (КС)', short: 'КС', kind: 'rate', pts: pts('RUB KEY'), chg: chg('RUB KEY') },
    { id: 'RUB OFZ', ccy: 'RUB', name: 'Базис ОФЗ − RUONIA', short: 'ОФЗ-базис', kind: 'basis',
      pts: kbd.map((v, i) => v != null && ruo[i] != null ? (v - ruo[i]) * 100 : null), chg: chg('RUB OFZ').map((v, i) => v - chg('RUB RUONIA')[i]) },
    { id: 'USD SOFR', ccy: 'USD', name: 'USD (кривой нет, риск по дюрации)', short: 'USD', kind: 'rate', pts: new Array(NB).fill(null), chg: zeros() },
    { id: 'EUR ESTR', ccy: 'EUR', name: 'EUR (кривой нет, риск по дюрации)', short: 'EUR', kind: 'rate', pts: new Array(NB).fill(null), chg: zeros() },
    { id: 'CNY XCCY', ccy: 'CNY', name: 'CNY (кривой нет, риск по дюрации)', short: 'CNY', kind: 'rate', pts: new Array(NB).fill(null), chg: zeros() },
  ];
  const curveOf = c => CURVE_MAP[c] || c;
  // кривые риска, которых страницы не знают (валюта вне USD/EUR/CNY и т. п.), — своими строками
  D.positions.forEach(p => Object.keys(p.risk || {}).forEach(c => {
    const id = curveOf(c);
    if (!M.curves.some(x => x.id === id)) {
      const ccy = (C[c] && C[c].ccy) || c.split(' ')[0];
      M.curves.push({ id, ccy, name: (C[c] && C[c].name) || id, short: id, kind: 'rate', pts: pts(c), chg: chg(c) });
      if (!M.ccys.includes(ccy)) { M.ccys.push(ccy); M.ccyOrder[ccy] = M.ccys.length - 1; if (M.fx[ccy] == null) M.fx[ccy] = 1; }
    }
  }));
  M.curve = Object.fromEntries(M.curves.map(c => [c.id, c]));
  M.ofzCurve = kbd;
  if (kbd.every(v => v == null)) M.notes.push('нет КБД в прогоне: кривая ОФЗ и базис пусты');
  function curveAt(id, t) {
    const P = M.pillars, a = id === 'OFZ' ? M.ofzCurve : (M.curve[id] || {}).pts;
    if (!a || t == null) return null;
    if (t <= P[0]) return a[0];
    for (let i = 1; i < P.length; i++) if (t <= P[i]) { if (a[i - 1] == null || a[i] == null) return null; const w = (t - P[i - 1]) / (P[i] - P[i - 1]); return a[i - 1] * (1 - w) + a[i] * w; }
    return a[a.length - 1];
  }
  M.curveAt = curveAt;
  function bucketWeights(t) {
    const P = M.pillars, w = new Array(P.length).fill(0);
    if (t == null || !isFinite(t) || t <= P[0]) { w[0] = 1; return w; }
    for (let i = 1; i < P.length; i++) if (t <= P[i]) { const a = (t - P[i - 1]) / (P[i] - P[i - 1]); w[i - 1] = 1 - a; w[i] = a; return w; }
    w[P.length - 1] = 1; return w;
  }

  // ---------- книги, юрлица, эмитенты ----------
  M.books = Object.entries(D.BOOKS || {}).map(([id, b]) => ({ id, name: b.name || id, entity: b.entity || '—', desk: b.desk || '—', type: b.bookType || '—', trader: b.trader || '—' }));
  D.positions.forEach(p => { if (p.book && !M.books.some(b => b.id === p.book)) M.books.push({ id: p.book, name: p.book, entity: p.entity || '—', desk: '—', type: '—', trader: '—' }); });
  M.book = Object.fromEntries(M.books.map(b => [b.id, b]));
  M.entities = [...new Set(M.books.map(b => b.entity))].map(e => ({ id: e, name: e }));
  const NO = v => (v == null || v === 'нет данных') ? null : v;
  const issuerNames = [...new Set(D.positions.map(p => p.issuer).filter(Boolean))];
  // короткое имя эмитента для списков и осей (в статике хаба short_name пуст): без организационно-правовой формы и
  // кавычек, не длиннее 30 знаков; полное — в name
  const LEGAL = /^(публичное|непубличное)?\s*(акционерное общество|общество с ограниченной ответственностью)\s*|\s*\((п?ао|ооо)\)|^(пао|ао|ооо|мкпао|нао)\s+|\s+(пао|ао|ооо)$/gi;
  const shortName = (n, gov) => { if (gov) return 'Минфин РФ'; let t = n.replace(LEGAL, '').replace(/[«»"]/g, '').replace(/\s+/g, ' ').trim() || n; return t.length > 30 ? t.slice(0, 29) + '…' : t; };
  M.issuers = issuerNames.map(n => { const i = (D.ISSUERS || {})[n] || {}; return { id: n, name: n, short: shortName(n, i.gov), sector: NO(i.sector) || '—', rating: NO(i.rating) || '—', ratingInfo: i.ratingInfo || null, group: i.group && i.group !== n ? i.group : null, quasi: !!i.quasi, gov: !!i.gov }; });
  M.issuer = Object.fromEntries(M.issuers.map(i => [i.id, i]));

  // ---------- классификация ----------
  M.groupLabel = { GOV: 'Гос', SUB: 'Суб', CORP: 'Корп', DER: 'Деривативы', OTH: 'Прочее' };
  M.kindLabel = { PD: 'ОФЗ-ПД (фикс)', PK: 'ОФЗ-ПК (флоатер)', IN: 'ОФЗ-ИН (линкер)', SOVFX: 'Суверен. валютные', FIX: 'Фикс', FLT: 'Флоатер', OTC: 'OTC (по отчёту)', FX: 'Валюта / FX', OTH: 'Прочее', IRS: 'IRS / OIS', KSW: 'Своп на КС', BAS: 'Базис КС/RUONIA', FXF: 'FX форвард', XCCY: 'XCCY' };
  M.kindShort = { PD: 'ПД', PK: 'ПК', IN: 'ИН', SOVFX: 'Сув', FIX: 'Фикс', FLT: 'Флоат', OTC: 'OTC', FX: 'FX', OTH: 'Пр', IRS: 'IRS', KSW: 'КС-своп', BAS: 'Базис', FXF: 'FX fwd', XCCY: 'XCCY' };
  M.typeRows = [
    { id: 'GOV.PD', group: 'GOV', kind: 'PD', label: 'ОФЗ-ПД' }, { id: 'GOV.PK', group: 'GOV', kind: 'PK', label: 'ОФЗ-ПК (флоатеры)' },
    { id: 'GOV.IN', group: 'GOV', kind: 'IN', label: 'ОФЗ-ИН' }, { id: 'GOV.SOVFX', group: 'GOV', kind: 'SOVFX', label: 'Суверенные валютные' },
    { id: 'SUB.FIX', group: 'SUB', kind: 'FIX', label: 'Суб. фикс' }, { id: 'SUB.FLT', group: 'SUB', kind: 'FLT', label: 'Суб. флоатеры' },
    { id: 'CORP.FIX', group: 'CORP', kind: 'FIX', label: 'Корп. фикс' }, { id: 'CORP.FLT', group: 'CORP', kind: 'FLT', label: 'Корп. флоатеры' },
    { id: 'DER.OTC', group: 'DER', kind: 'OTC', label: 'OTC-деривативы (по отчёту)' }, { id: 'DER.FX', group: 'DER', kind: 'FX', label: 'Валюта / FX' },
    { id: 'OTH.OTH', group: 'OTH', kind: 'OTH', label: 'Прочее' },
  ];
  const GOV = { 'ОФЗ-ПД': 'PD', 'ОФЗ-АД': 'PD', 'ОФЗ-ПК': 'PK', 'ОФЗ-ИН': 'IN' };
  function kindOf(p) {  // api/concepts.kind_of
    if (p.instrType === 'FX') return ['DER', 'FX'];
    if (p.instrType === 'OTC') return ['DER', 'OTC'];
    if (p.instrType && p.instrType !== 'BOND') return ['OTH', 'OTH'];
    if (!p.instrType && p.segment === 'Деривативы') return ['DER', 'OTC'];
    if (!p.instrType && (p.segment === 'FX')) return ['DER', 'FX'];
    if (!p.instrType && p.segment === 'Прочее') return ['OTH', 'OTH'];
    const flt = p.couponType === 'float' ? 'FLT' : 'FIX';
    if (p.segment === 'Гос' || GOV[p.assetClass]) return (p.ccy || 'RUB') !== 'RUB' ? ['GOV', 'SOVFX'] : ['GOV', GOV[p.assetClass] || 'PD'];
    if (p.segment === 'Суб') return ['SUB', flt];
    return ['CORP', flt];
  }

  // ---------- позиции ----------
  const FREQ = { 12: 'M', 4: 'Q', 2: 'S', 1: 'A' };
  const IDX = { KEY_RATE: 'КС', 'КС': 'КС', RUONIA: 'RUONIA' };
  const pct = v => { const t = String(+(+v).toFixed(3)); const d = (t.split('.')[1] || '').length; return (+v).toFixed(Math.max(2, d)); };
  const XP = X.positions || {};
  const accrualDays = D.accrualDays || 1;
  const positions = D.positions.map(d => {
    const [group, kind] = kindOf(d), ex = d.extra || {}, q = ex.quote || {}, xp = XP[d.id] || {};
    const isBond = group !== 'DER' && group !== 'OTH';
    const p = {
      id: d.id, isin: d.isin || null, name: d.name || d.id, issuer: d.issuer || null, group, kind, ccy: d.ccy || 'RUB', book: d.book,
      maturity: d.maturity || null, notional: num(d.notional) || 0, notionalRub: num(d.notionalRub) || 0, spreadBp: num(d.spreadBp),
      notValued: !!d.notValued, cpty: d.counterparty || null, portfolio: d.portfolio || null, quality: ex.quality || null,
    };
    // для типа инструмента таблицы «валюта × тип» (lib/instr.js, api/concepts.instr_kind): поля факта и группа продукта отчёта
    p.instrType = d.instrType || null; p.assetClass = d.assetClass || null; p.segment = d.segment || null;
    p.productGroup = (ex.reported || {}).x_product_group || null;
    p.ocp = !!(d.ocpTransfer || d.assetClass === 'Перенос ОВП');
    p.accrualOnly = !!d.accrualOnly;  // HTM по начислению: рыночного риска нет по правилу, это не пропуск
    p.fxDeltaBy = Object.fromEntries(Object.entries(d.fxDelta || {}).filter(([, v]) => num(v)).map(([c, v]) => [c, +v]));
    const b = M.book[d.book] || {};
    p.bookName = b.name || d.book; p.entity = b.entity || d.entity || '—'; p.desk = b.desk || '—';
    p.issuerName = d.issuer || d.counterparty || (group === 'DER' ? 'Деривативы' : '—');
    p.issuerFull = d.issuer || (d.counterparty ? 'Контрагент: ' + d.counterparty : p.issuerName);
    p.sector = NO(d.sector) || (group === 'DER' ? 'Деривативы' : '—'); p.rating = NO(d.rating) || '—';
    p.ratingInfo = d.ratingInfo || null; p.ratingAgency = (d.ratingInfo || {}).agency || null;  // рейтинг деска (R25)
    p.issuerGroup = d.issuerGroup && d.issuerGroup !== d.issuer ? d.issuerGroup : null;
    p.latName = ex.lat_name || null; p.issueSize = num(ex.issue_size);
    p.groupLabel = M.groupLabel[group]; p.kindLabel = M.kindLabel[kind];
    // купон
    const F = FREQ[d.couponFreq] || '';
    if (isBond) {
      const type = d.assetClass === 'ОФЗ-ИН' ? 'linker' : d.couponType === 'float' ? 'float' : d.couponType === 'fix' ? 'fix' : (d.couponType || 'unknown');
      p.cpn = { type, rate: num(d.coupon), index: IDX[d.index] || d.index || null, margin: d.margin == null ? null : Math.round(d.margin * 100), freq: F };
      p.couponFmt = type === 'fix' ? (d.coupon != null ? pct(d.coupon) + '% ' + F : '— ' + F)
        : type === 'float' ? (p.cpn.index || 'плав.') + (p.cpn.margin ? (p.cpn.margin > 0 ? '+' : '') + p.cpn.margin : '') + ' ' + F
          : type === 'linker' ? (d.coupon != null ? pct(d.coupon) + '%+CPI ' : 'CPI ') + F
            : type === 'zero' ? 'дисконт' : (d.coupon != null ? pct(d.coupon) + '% ' + F : '—');
      p.curCpn = num(d.coupon);
    } else { p.cpn = null; p.couponFmt = d.notValued ? 'не оценено' : (p.kindLabel || ''); p.curCpn = null; p.rate = null; }
    p.couponFmt = p.couponFmt.trim();
    // событие и срок
    const yd = d.yieldDate && (!d.maturity || d.yieldDate < d.maturity) ? d.yieldDate : null;
    p.offer = ex.offer_date || yd || null;
    p.offerKind = p.offer ? (ex.offer_kind || (ex.horizon_kind === 'call' ? 'call' : 'put')) : null;
    p.event = p.offer ? { date: p.offer, kind: p.offerKind } : { date: d.maturity || null, kind: 'mat' };
    p.yearsToMat = d.tenorY != null ? d.tenorY : yearsTo(d.maturity);
    p.tenor = p.event.date ? Math.max(0.01, yearsTo(p.event.date)) : (p.yearsToMat != null ? p.yearsToMat : 0);
    if (p.event.date == null) p.event.date = M.asOf;  // бессрочная без колла: событие не определено
    // цены, доходности, спреды
    p.price = num(d.price); p.prevPrice = num(xp.prevPrice);
    p.bid = num(q.bid); p.ask = num(q.ask); p.volumeDay = num(q.value);
    const flt = p.cpn && p.cpn.type === 'float';
    p.ytm = flt ? null : num(d.ytm); p.ytmProxy = num(d.ytm);
    p.zSpreadZ = num(d.spreadBp); p.gspreadMoex = flt ? null : num(q.gspread_bp);
    p.gspread = flt ? null : (num(ex.g_spread_bp) != null ? num(ex.g_spread_bp) : p.gspreadMoex);
    p.dm = flt ? (num(ex.dm_bp) != null ? num(ex.dm_bp) : p.zSpreadZ) : null;
    p.zspread = flt ? p.dm : p.zSpreadZ; p.spreadLabel = flt ? 'DM' : (p.zspread != null ? 'Z' : '');
    p.mdur = num(d.mdur); p.sprDur = num(d.sprDur);
    p.accrued = num(d.accrued) || 0;
    p.mv = num(d.mvRub); p.mvCcy = num(d.mv);
    p.indexation = null; p.fundRate = null;
    // риск по кривым
    p.risk = {}; M.curves.forEach(c => p.risk[c.id] = zeros());
    Object.entries(d.risk || {}).forEach(([c, arr]) => { const id = curveOf(c); arr.forEach((v, k) => p.risk[id][k] += v || 0); });
    p.basis01 = num(d.ofzSpr01) || 0;
    p.cs01 = num(d.cs01) || 0;
    // по бакетам — ядра (spread_buckets: сумма = ofzSpr01 или cs01); без них — по двум соседним бакетам срока
    const sb = ex.spread_buckets, w = bucketWeights(p.tenor);
    const split = tot => sb ? M.buckets.map(b => (num(sb[b]) || 0)) : w.map(x => tot * x);
    p.spreadBucketsSource = sb ? 'core' : 'tenor';
    if (p.basis01) split(p.basis01).forEach((x, k) => p.risk['RUB OFZ'][k] += x);
    p.cs01b = p.cs01 ? split(p.cs01) : zeros();
    p.dv01ByCurve = {}; let tot = 0;
    M.curves.forEach(c => { const s = p.risk[c.id].reduce((a, v) => a + v, 0); p.dv01ByCurve[c.id] = s; if (c.kind === 'rate') tot += s; });
    p.dv01 = tot;
    p.fxDelta = Object.values(d.fxDelta || {}).reduce((a, v) => a + (v || 0), 0);
    // PnL
    const e = d.pnl || {};
    if (e.available === false || !d.pnl) {
      p.pnl = { carry: null, rates: null, spread: null, fx: null, trading: null, other: null, total: null, available: false, reason: e.reasonText || null };
      p.carryDay = null; p.rollDay = null;
    } else {
      const z = v => v || 0;
      p.pnl = { carry: z(e.carry) + z(e.rolldown), rates: z(e.rates) + z(e.convexity), spread: z(e.spread), fx: z(e.fx), trading: z(e.newTrades),
        other: z(e.funding) + z(e.other) + z(e.reported), total: z(e.explained), available: true, method: e.methodText || e.method || null };
      p.carryDay = (z(e.carry) + z(e.rolldown)) / accrualDays; p.rollDay = z(e.rolldown) / accrualDays;
    }
    p.pnlMtd = num(xp.pnlMtd); p.pnlYtd = num(xp.pnlYtd);
    p.spreadChgBp = num(xp.spreadChgBp);
    p.tradedToday = !!(xp.trade || d.newToday); p.tradeToday = xp.trade || null;
    p.searchText = [p.name, p.isin, p.issuerName, p.issuerFull, p.issuerGroup, p.latName, p.book, p.bookName, p.ccy, p.kindLabel, p.groupLabel, p.sector, p.couponFmt, p.portfolio, p.rating, p.ratingAgency].join(' ').toLowerCase();
    return p;
  });
  M.positions = positions;
  M.pos = Object.fromEntries(positions.map(p => [p.id, p]));
  const groupOrder = { GOV: 0, SUB: 1, CORP: 2, DER: 3, OTH: 4 };
  const kindOrder = { PD: 0, PK: 1, IN: 2, SOVFX: 3, FLT: 0, FIX: 1, OTC: 0, FX: 1, OTH: 0 };
  M.defaultOrder = (a, b) => ((M.ccyOrder[a.ccy] ?? 99) - (M.ccyOrder[b.ccy] ?? 99)) || (groupOrder[a.group] - groupOrder[b.group]) ||
    (a.issuerName || '').localeCompare(b.issuerName || '', 'ru') || ((kindOrder[a.kind] || 0) - (kindOrder[b.kind] || 0)) || (a.tenor - b.tenor);
  positions.sort(M.defaultOrder);
  if (positions.some(p => p.notValued)) M.notes.push(positions.filter(p => p.notValued).length + ' поз. не оценены (нет данных): показаны без риска и MV');

  // ---------- история деска ----------
  const H = X.hist || {};
  M.histDates = (X.histDates && X.histDates.length) ? X.histDates.slice() : [M.asOf];
  M.histSource = X.histSource || 'positions';  // 'runs': суммы прогонов по датам
  M.pnlPeriods = X.pnlPeriods || null;
  const n = M.histDates.length;
  const series = (src, keys, today) => { const o = {}; keys.forEach(k => { o[k] = (src && src[k]) ? src[k].map(v => v == null ? null : +v) : new Array(n).fill(0); if (!src && today) o[k][n - 1] = today(k); }); return o; };
  const sumBy = (f) => positions.reduce((s, p) => s + (f(p) || 0), 0);
  M.hist = {
    dv01: series(H.dv01, M.curves.map(c => c.id), id => sumBy(p => p.dv01ByCurve[id])),
    cs01: H.cs01 ? H.cs01.slice() : new Array(n).fill(0),
    dv01Book: series(H.dv01Book, M.books.map(b => b.id), id => sumBy(p => p.book === id ? p.dv01 : 0)),
    mvType: series(H.mvType, M.typeRows.map(t => t.id), id => sumBy(p => p.group + '.' + p.kind === id ? (p.group === 'DER' ? Math.abs(p.notionalRub) : p.mv) : 0)),
    mvCcy: series(H.mvCcy, M.ccys, c => sumBy(p => p.ccy === c && p.group !== 'DER' ? p.mv : 0)),
    pnl: series(H.pnl, ['carry', 'rates', 'spread', 'fx', 'trading', 'other', 'total'], k => sumBy(p => p.pnl[k])),
  };
  if (!H.cs01) M.hist.cs01[n - 1] = sumBy(p => p.cs01);
  // базис в истории — по ofzSpr01 прогонов (кривая «RUB OFZ» страниц)
  if (H.basis01) M.hist.dv01['RUB OFZ'] = H.basis01.slice();

  // ---------- история позиции (лениво, синхронно) ----------
  const histCache = {}, deepCache = {}; let misses = 0, all = false;
  function fetchHist(ids, from) {
    try {
      const x = new XMLHttpRequest();
      x.open('GET', '../api/v1/history/positions?' + runQ + (ids ? '&ids=' + encodeURIComponent(ids.join(',')) : '') + (from ? '&from=' + from : ''), false);
      x.send();
      if (x.status !== 200) return null;
      return JSON.parse(x.responseText);
    } catch (e) { return null; }
  }
  const OPT = ['zspr', 'dm', 'icpi', 'ir', 'bei', 'cpi'];
  M.posHistory = function (id, opts) {
    if (opts && opts.deep) {
      if (!(id in deepCache)) { const j = fetchHist([id], '2000-01-01'); deepCache[id] = j && j.series && j.series[id] ? Object.assign({ dates: j.dates }, j.series[id]) : null; }
      if (deepCache[id] && deepCache[id].dates.length) {
        return conv(deepCache[id], { deep: true });
      }
    }
    if (!histCache[id] && !all) {
      misses++;
      const j = fetchHist(misses > 2 ? null : [id]);
      if (misses > 2) all = true;
      if (j) Object.entries(j.series || {}).forEach(([pid, s]) => histCache[pid] = Object.assign({ dates: j.dates }, s));
    }
    const p = M.pos[id], s = histCache[id];
    if (!s || !s.dates.length) {
      const c = {}; Object.keys(p.dv01ByCurve).forEach(id => { if (p.dv01ByCurve[id] && id !== 'RUB OFZ') c[id] = [p.dv01ByCurve[id]]; });
      return { dates: [M.asOf], price: [p.price], ytm: [p.ytm], spr: [p.zspread], notional: [p.notional], dv01: [p.dv01], cs01: [p.cs01], basis: [p.basis01], mv: [p.group === 'DER' ? Math.abs(p.notionalRub) : p.mv], curves: c, bench: [p.ccy === 'RUB' ? curveAt('OFZ', p.tenor) : null], fromRuns: true };
    }
    return conv(s);
  };

  // ряды истории позиции с сервера (null вместо пустого ряда) — в массивы страницы; DV01, риск и MV в валюте отчёта
  function conv(s, extra) {
    const k = (M.toRepr && M.toRepr.RUB) || 1;
    // сервер шлёт null вместо ряда без значений: здесь он снова массив (нули для риска, null для цен)
    const n = s.dates.length, nulls = () => new Array(n).fill(null);
    const sc = a => a ? a.map(v => v == null ? null : v * k) : null;
    const risk = a => a ? sc(a) : (s.notional || []).map(v => v == null ? null : 0);
    const curves = {}; Object.entries(s.curves || {}).forEach(([c, a]) => curves[curveOf(c)] = sc(a));
    const o = Object.assign({ dates: s.dates, price: s.price || nulls(), ytm: s.ytm || nulls(), spr: s.spr || nulls(), notional: s.notional || nulls(),
      dv01: risk(s.dv01), cs01: risk(s.cs01), basis: risk(s.basis), mv: risk(s.mv), curves, bench: s.bench || nulls(), fromRuns: true }, extra || {});
    OPT.forEach(key => { if (s[key]) o[key] = s[key]; });
    return o;
  }

  // ---------- история рынка (лениво, синхронно, один раз) ----------
  let market;
  M.marketHistory = function () {
    if (market !== undefined) return market;
    market = null;
    try {
      const x = new XMLHttpRequest();
      x.open('GET', '../api/v1/history/market?' + runQ, false);
      x.send();
      if (x.status === 200) market = JSON.parse(x.responseText);
    } catch (e) { market = null; }
    if (market) {
      // индексы, которых нет в прогоне (RUSFAR, RUSFAR CNY, SOFR, €STR, CPI г/г), — из фиксингов хаба
      Object.entries(market.indices || {}).forEach(([k, v]) => { if (M.indices[k] == null && v.value != null) M.indices[k] = v.value; });
      (market.notes || []).forEach(n => M.notes.push(n));
    }
    // кросс-курсы (R25): ряды хаба (контракт 1.3) или кросс курсов ЦБ с сервера; без истории рынка — по курсам прогона
    M.crosses = market && market.crosses && Object.keys(market.crosses).length ? market.crosses : Object.fromEntries(
      [['EUR/USD', 'EUR', 'USD'], ['USD/CNY', 'USD', 'CNY']].filter(([, b, q]) => M.fx[b] && M.fx[q] && M.fx[b] !== 1 && M.fx[q] !== 1)
        .map(([pr, b, q]) => [pr, { value: M.fx[b] / M.fx[q], kind: 'run', label: 'кросс курсов прогона', chgPct: [M.fxChgPct && M.fxChgPct[b] != null && M.fxChgPct[q] != null ? ((1 + M.fxChgPct[b] / 100) / (1 + M.fxChgPct[q] / 100) - 1) * 100 : null] }]));
    return market;
  };

  // ---------- потоки, рынок, ЦБ ----------
  M.cashflows = (X.cashflows || []).filter(c => M.pos[c.pos]).map(c => ({ date: c.date, pos: c.pos, kind: c.kind === 'amort' ? 'mat' : c.kind, amort: c.kind === 'amort', ccy: c.ccy, amount: c.amount, est: c.est, amountRub: c.amount * (M.fx[c.ccy] || 1) }));
  // сектор ОФЗ — «ОФЗ» (у Минфина в статике сектор «суверен»), остальные — по статике хаба; рейтинг деска — по новейшей
  // статике (R25): грейд, «б/р» (рейтингов у выпуска и эмитента нет), без рейтингов в статике — «без рейтинга»
  M.universeRub = (X.universeRub || []).map(b => Object.assign({}, b, { rating: b.rating || 'без рейтинга', sector: b.isOfz ? 'ОФЗ' : (b.sector || '—'), size: b.size || 0, cpn: b.cpn, dur: b.dur || 0, tenor: b.tenor || 0 }));
  M.universeFx = (X.universeFx || []).map(b => Object.assign({}, b, { rating: b.rating || 'без рейтинга', sector: b.sector || '—', outstanding: b.outstanding || 0, cpn: b.cpn || 0, freq: b.freq || '', tenor: b.tenor || 0, coupons: b.coupons || [] }));
  M.marketMovers = X.marketMovers || [];
  M.marketDate = X.marketDate || null;
  M.staticDate = X.staticDate || null;
  const CB = X.cbr || {};
  M.cbr = {
    next: CB.next ? Object.assign({}, CB.next, { consensus: CB.next.consensus || 'нет данных', priced: CB.next.priced || 'нет данных' })
      : { date: null, label: 'нет календаря заседаний ЦБ', consensus: '—', priced: '—' },
    meetings: CB.meetings || [], estimated: CB.estimated || [], past: CB.past || [], keyRate: CB.keyRate == null ? null : CB.keyRate,
  };

  // ---------- все потоки позиции (лениво, карточка): /api/v1/positions/flows — график выпуска из статики хаба до
  // погашения × бумаги позиции, оферты внутри; null — не облигация или статики нет (карточка берёт потоки прогона) ----------
  const flowCache = {};
  M.posFlows = function (id) {  // промис; повторно — из памяти
    if (!flowCache[id]) flowCache[id] = fetch('../api/v1/positions/flows?' + runQ + '&id=' + encodeURIComponent(id)).then(r => r.ok ? r.json() : null).then(j => {
      if (!j || !j.flows) return null;
      const k = (M.toRepr && M.toRepr[j.ccy]) || M.fx[j.ccy] || 1;  // в валюту отчёта, как и всё на страницах
      return j.flows.map(([date, kind, amount, est]) => ({ date, pos: id, kind, amort: kind === 'amort', ccy: j.ccy, amount, est, amountRub: amount == null ? null : amount * k }));
    }).catch(() => null);
    return flowCache[id];
  };

  // ---------- календарь рынка (лениво: только страницам, которые его просят) ----------
  const RUB_K = () => (M.toRepr && M.toRepr.RUB) || 1;  // рублёвые суммы — в валюте отчёта, как и всё на страницах
  function getJson(path) {
    try { const x = new XMLHttpRequest(); x.open('GET', path, false); x.send(); return x.status === 200 ? JSON.parse(x.responseText) : null; } catch (e) { return null; }
  }
  let calCache;
  Object.defineProperty(M, 'marketCal', { configurable: true, get() {
    if (calCache !== undefined) return calCache;
    const c = getJson('../api/v1/calendar/market?' + runQ);
    if (!c || c.missing) { calCache = null; return null; }
    const held = new Set(c.held || []), k = RUB_K(), F = c.fields;
    const rows = (key, conv) => (c[key] || []).map(r => { const o = {}; F[key].forEach((f, i) => o[f] = r[i]); if (conv) conv(o); return o; });
    const posBySec = {}; D.positions.forEach(p => { if (p.instrumentId) posBySec[p.instrumentId] = p.id; });
    calCache = {
      source: 'real', staticDate: c.staticDate, asOf: c.asOf, horizonEnd: c.horizonEnd, bigRub: c.bigRub * k,
      types: c.types.map(id => ({ id, label: c.typeLabels[id] || id })), ccys: c.ccys || [], coverage: c.coverage || {},
      rub: rows('rub', o => { o.amount *= k; o.bigAmount *= k; o.estAmount *= k; }),
      rubSector: rows('rubSector', o => { o.amount *= k; }),
      rubBig: rows('rubBig', o => { o.amount *= k; o.held = held.has(o.secid); o.pos = posBySec[o.secid] || null; }),
      fx: rows('fx'), fxIssuers: rows('fxIssuers'),
      fxEvents: rows('fxEvents', o => { o.held = held.has(o.secid); o.pos = posBySec[o.secid] || null; }),
      notes: ['календарь рынка — по статике хаба на ' + c.staticDate + ': купоны (ещё не объявленные — по последнему, «прогноз»), амортизации, погашения и оферты всех выпусков × объём выпуска MOEX'],
      flows(q) {  // строки за ячейкой: {market, from, to, type, kind, ccy, sector, big, issuer, secid}
        const qs = Object.entries(q || {}).filter(([, v]) => v != null && v !== false && v !== '').map(([a, v]) => a + '=' + encodeURIComponent(v)).join('&');
        const j = getJson('../api/v1/calendar/market?' + runQ + '&' + qs);
        if (!j) return { count: 0, amount: 0, rows: [] };
        const kk = q && q.market === 'rub' ? k : 1;
        j.amount = (j.amount || 0) * kk;
        j.rows.forEach(r => { r.amount = r.amount == null ? null : r.amount * kk; r.pos = posBySec[r.secid] || null; });
        return j;
      },
    };
    return calCache;
  } });

  // ---------- как в mock.js ----------
  function priceFromYield(cpn, y, T, f) {
    if (T <= 0) return 100;
    const nn = Math.max(1, Math.ceil(T * f - 1e-9)), first = T * f - (nn - 1);
    let pv = 0;
    for (let k = 0; k < nn; k++) pv += (cpn / f) / Math.pow(1 + y / 100 / f, first + k);
    pv += 100 / Math.pow(1 + y / 100 / f, first + nn - 1);
    return pv - (cpn / f) * (1 - first);
  }
  function modDur(cpn, y, T, f) { const h = 0.01; return (priceFromYield(cpn, y - h, T, f) - priceFromYield(cpn, y + h, T, f)) / (2 * h) / priceFromYield(cpn, y, T, f) * 100; }
  M.math = { priceFromYield, modDur };
  M.sum = (arr, f) => arr.reduce((s, x) => s + ((typeof f === 'function' ? f(x) : x[f]) || 0), 0);
  M.kpis = function (list) {
    list = list || positions;
    const S = f => M.sum(list, f);
    const k = { dv01: S('dv01'), cs01: S('cs01'), basis01: S('basis01'), mvBonds: S(p => p.group === 'DER' ? 0 : p.mv), pnl: S(p => p.pnl.total), carryDay: S('carryDay'),
      pnlMtd: S('pnlMtd'), pnlYtd: S('pnlYtd'), byCcy: {}, byCurve: {} };
    M.ccys.forEach(c => k.byCcy[c] = { dv01: S(p => p.ccy === c ? p.dv01 : 0), mv: S(p => p.ccy === c && p.group !== 'DER' ? p.mv : 0), fxDelta: S(p => p.ccy === c ? p.fxDelta : 0) });
    M.curves.forEach(c => k.byCurve[c.id] = S(p => p.dv01ByCurve[c.id]));
    return k;
  };
})();
