/*
 * MOCK — вымышленный портфель FI-деска для концептов интерфейса (prototypes/concepts/).
 * Не связан с data.js и сервером: цель — играть с видом экранов, а не с расчётами.
 * Всё детерминировано (свой ГПСЧ), цифры правдоподобные, но не настоящие.
 *
 * Конвенции:
 *  - суммы в RUB, если не сказано иное; номинал позиции — в валюте бумаги;
 *  - DV01 / CS01 — изменение PV в RUB при +1 б.п. (длинная облигация => DV01 < 0);
 *  - риск по кривым: p.risk[curveId] — массив по MOCK.buckets;
 *  - ОФЗ = RUONIA + базис (R18): у ОФЗ риск и на «RUB RUONIA», и на «RUB OFZ» (базис);
 *  - срок бумаги — до ближайшей оферты, если она есть, иначе до погашения (p.tenor, p.event);
 *  - FX-дельта — по валютам (p.fxDeltaBy, как fx_delta {ccy} прогона; p.fxDelta — в валюте позиции): у кросса USD/CNY
 *    она в обеих валютах; у FX-форварда риск на двух кривых (иностранной и RUONIA), у FX-свопа — на implied-кривой
 *    юаня (CNY FWD); ноги FX-сделки — p.legs (как extra.legs прогона в lib/real.js); CHF-форвард — на своей кривой;
 *  - имя бумаги (R33): p.name = алиас ?? биржевое (p.exchName), поиск — по обоим; алиасы демо — в памяти страницы
 *    (M.aliasApi как в lib/real.js, но без сервера: после перезагрузки — снова только примерный алиас ниже).
 */
(function () {
  'use strict';
  const M = window.MOCK = {};

  // ---------- ГПСЧ ----------
  let seed = 20261021;
  function rnd() { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; }
  function rndn() { return (rnd() + rnd() + rnd() + rnd() - 2) / 0.8165; }
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function rng(s) { let x = hash(String(s)) || 1; return () => { x ^= x << 13; x >>>= 0; x ^= x >> 17; x ^= x << 5; x >>>= 0; return x / 4294967296; }; }
  const pick = a => a[Math.floor(rnd() * a.length)];
  const round = (v, d) => Math.round(v * Math.pow(10, d)) / Math.pow(10, d);

  // ---------- даты ----------
  const AS_OF = '2026-10-21';
  const D0 = new Date(AS_OF + 'T00:00:00');
  const iso = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const parse = s => new Date(s + 'T00:00:00');
  const yearsTo = s => (parse(s) - D0) / (365.25 * 864e5);
  const addDays = (s, n) => { const d = parse(s); d.setDate(d.getDate() + n); return iso(d); };
  const addMonths = (s, n) => { const d = parse(s); const day = d.getDate(); d.setDate(1); d.setMonth(d.getMonth() + n); const last = new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate(); d.setDate(Math.min(day, last)); return iso(d); };
  function businessDaysBack(n) {
    const out = []; const d = new Date(D0);
    while (out.length < n) { const w = d.getDay(); if (w !== 0 && w !== 6) out.unshift(iso(d)); d.setDate(d.getDate() - 1); }
    return out;
  }
  M.asOf = AS_OF;
  M.prevDate = '2026-10-20';
  M.cut = { type: 'intraday', time: '14:30', label: 'внутридневной 14:30' };
  M.util = { iso, parse, yearsTo, addDays, addMonths, rng, hash, round };

  // ---------- рынок ----------
  M.ccys = ['RUB', 'USD', 'EUR', 'CNY', 'CHF'];
  // GBP и AED — только вторые валюты мелких кроссов (остатки ОВП, R43): валют деска (M.ccys) не добавляют
  M.ccyOrder = { RUB: 0, USD: 1, EUR: 2, CNY: 3, CHF: 4, GBP: 5, AED: 6 };
  M.fx = { RUB: 1, USD: 81.42, EUR: 94.58, CNY: 11.41, CHF: 101.85, GBP: 108.62, AED: 22.17 };
  M.fxChgPct = { RUB: 0, USD: 0.38, EUR: -0.12, CNY: 0.21, CHF: 0.15, GBP: 0.22, AED: 0.38 };
  M.indices = { 'КС': 14.50, 'RUONIA': 14.32, 'RUSFAR': 14.28, 'SOFR': 4.08, 'RUSFAR CNY': 5.45, '€STR': 2.15, 'CPI г/г': 6.8 };
  M.buckets = ['3M', '6M', '1Y', '2Y', '3Y', '4Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];
  M.pillars = [0.25, 0.5, 1, 2, 3, 4, 5, 7, 10, 15, 20];
  M.curves = [
    { id: 'RUB RUONIA', ccy: 'RUB', name: 'RUONIA OIS', short: 'RUONIA', kind: 'rate', pts: [14.20, 14.00, 13.55, 12.90, 12.60, 12.50, 12.45, 12.55, 12.70, 12.80, 12.85], chg: [5, 6, 7, 8, 7, 6, 5, 4, 3, 3, 3] },
    { id: 'RUB KEY', ccy: 'RUB', name: 'Ключевая ставка (КС)', short: 'КС', kind: 'rate', pts: [14.40, 14.15, 13.70, 13.05, 12.75, 12.65, 12.60, 12.70, 12.85, 12.95, 13.00], chg: [6, 7, 8, 8, 7, 6, 5, 4, 3, 3, 3] },
    { id: 'RUB OFZ', ccy: 'RUB', name: 'Базис ОФЗ − RUONIA', short: 'ОФЗ-базис', kind: 'basis', pts: [-30, -20, -5, 25, 45, 55, 65, 75, 80, 80, 75], chg: [4, 4, 3, 3, 2, 2, 2, 1, 1, 1, 0] },
    { id: 'USD SOFR', ccy: 'USD', name: 'USD SOFR', short: 'SOFR', kind: 'rate', pts: [4.05, 3.98, 3.85, 3.65, 3.58, 3.58, 3.62, 3.72, 3.85, 3.98, 4.05], chg: [0, -1, -1, -2, -3, -3, -3, -2, -2, -2, -2] },
    { id: 'EUR ESTR', ccy: 'EUR', name: 'EUR €STR', short: '€STR', kind: 'rate', pts: [2.12, 2.08, 2.05, 2.10, 2.18, 2.25, 2.32, 2.45, 2.60, 2.72, 2.78], chg: [0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2] },
    { id: 'CNY XCCY', ccy: 'CNY', name: 'CNY (RUSFAR CNY / xccy)', short: 'CNY', kind: 'rate', pts: [5.40, 5.30, 5.15, 4.95, 4.88, 4.88, 4.95, 5.10, 5.25, 5.35, 5.40], chg: [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0] },
    // implied-кривая юаня из своп-пойнтов CETS (FX-свопы) и CHF — кривые, которых нет в старом списке блоков кокпита
    { id: 'CNY FWD', ccy: 'CNY', name: 'CNY implied (своп-пойнты CETS)', short: 'CNY impl', kind: 'rate', pts: [5.65, 5.50, 5.30, 5.10, 5.00, 4.98, 5.02, 5.15, 5.28, 5.38, 5.42], chg: [2, 2, 1, 1, 1, 1, 1, 1, 0, 0, 0] },
    { id: 'CHF SARON', ccy: 'CHF', name: 'CHF SARON', short: 'SARON', kind: 'rate', pts: [0.05, 0.06, 0.08, 0.10, 0.18, 0.25, 0.38, 0.48, 0.60, 0.68, 0.72], chg: [0, 0, 0, -1, -1, -1, -1, -1, -1, -1, -1] },
  ];
  M.curve = Object.fromEntries(M.curves.map(c => [c.id, c]));
  M.ofzCurve = M.pillars.map((t, i) => M.curve['RUB RUONIA'].pts[i] + M.curve['RUB OFZ'].pts[i] / 100);
  function curveAt(id, t) {
    const P = M.pillars, pts = id === 'OFZ' ? M.ofzCurve : M.curve[id].pts;
    if (t <= P[0]) return pts[0];
    for (let i = 1; i < P.length; i++) if (t <= P[i]) { const w = (t - P[i - 1]) / (P[i] - P[i - 1]); return pts[i - 1] * (1 - w) + pts[i] * w; }
    return pts[pts.length - 1];
  }
  M.curveAt = curveAt;
  // раскладка срока по двум соседним пилларам
  function bucketWeights(t) {
    const P = M.pillars, w = new Array(P.length).fill(0);
    if (t <= P[0]) { w[0] = 1; return w; }
    for (let i = 1; i < P.length; i++) if (t <= P[i]) { const a = (t - P[i - 1]) / (P[i] - P[i - 1]); w[i - 1] = 1 - a; w[i] = a; return w; }
    w[P.length - 1] = 1; return w;
  }

  // ---------- юрлица, деск, книги ----------
  M.entities = [{ id: 'BANK', name: 'Банк' }, { id: 'IC', name: 'Инвесткомпания' }, { id: 'AM', name: 'Управляющая компания' }];
  M.books = [
    { id: 'OFZ', name: 'ОФЗ трейдинг', entity: 'BANK', desk: 'Rates', type: 'TRADING', trader: 'А. Волков' },
    { id: 'RATES', name: 'Процентные деривативы', entity: 'BANK', desk: 'Rates', type: 'DERIVS', trader: 'А. Волков' },
    { id: 'CORP', name: 'Корпоративные RUB', entity: 'BANK', desk: 'Credit', type: 'TRADING', trader: 'М. Орлова' },
    { id: 'FLOAT', name: 'Флоатеры', entity: 'BANK', desk: 'Credit', type: 'TRADING', trader: 'М. Орлова' },
    { id: 'FXCR', name: 'Валютный кредит', entity: 'BANK', desk: 'Credit', type: 'TRADING', trader: 'Д. Ким' },
    { id: 'FXB', name: 'FX и XCCY', entity: 'BANK', desk: 'FX', type: 'DERIVS', trader: 'Д. Ким' },
    { id: 'HTM', name: 'Инвестиционный (HTM)', entity: 'BANK', desk: 'ALM', type: 'HTM', trader: '—' },
    { id: 'IC-BND', name: 'ИК облигации', entity: 'IC', desk: 'Credit', type: 'TRADING', trader: 'С. Лебедев' },
  ];
  // книг много (синтетическая раскладка): дочерние книги семейств, часть позиций разнесена по ним
  const SUB = {
    OFZ: [['OFZ-ARB', 'ОФЗ арбитраж'], ['OFZ-LONG', 'ОФЗ длинные'], ['OFZ-MM', 'ОФЗ маркет-мейкинг']],
    RATES: [['RATES-OIS', 'OIS RUONIA'], ['RATES-KS', 'Свопы на КС'], ['RATES-HDG', 'Хедж облигаций']],
    CORP: [['CORP-1', 'Корп. 1-й эшелон'], ['CORP-2', 'Корп. 2-й эшелон'], ['CORP-NEW', 'Первичка']],
    FLOAT: [['FLOAT-KS', 'Флоатеры КС'], ['FLOAT-RUO', 'Флоатеры RUONIA']],
    FXCR: [['FXCR-CNY', 'Юаневые бонды'], ['FXCR-USD', 'Замещающие USD']],
    FXB: [['FXB-SWAP', 'FX свопы'], ['FXB-FWD', 'FX форварды']],
    'IC-BND': [['IC-HY', 'ИК высокодоходные'], ['IC-MM', 'ИК маркет-мейкинг']],
  };
  Object.keys(SUB).forEach(id => { const b = M.books.find(x => x.id === id); SUB[id].forEach(([sid, nm]) => M.books.push(Object.assign({}, b, { id: sid, name: nm, parent: id }))); });
  [['AM-BOND', 'Фонд облигаций', 'Credit'], ['AM-OFZ', 'Фонд ОФЗ', 'Rates'], ['AM-CNY', 'Фонд юаневых облигаций', 'Credit'], ['AM-DER', 'Хедж фондов', 'Rates']]
    .forEach(([id, nm, desk]) => M.books.push({ id, name: nm, entity: 'AM', desk, type: 'TRADING', trader: 'Е. Смирнова' }));
  M.book = Object.fromEntries(M.books.map(b => [b.id, b]));

  // ---------- эмитенты ----------
  M.issuers = [
    ['MINFIN', 'Минфин России', 'Минфин', 'Суверен', 'AAA'],
    ['GAZP', 'Газпром капитал', 'Газпром', 'Нефть и газ', 'AAA'],
    ['RZD', 'РЖД', 'РЖД', 'Транспорт', 'AAA'],
    ['SBER', 'Сбербанк', 'Сбер', 'Банки', 'AAA'],
    ['VTB', 'Банк ВТБ', 'ВТБ', 'Банки', 'AAA'],
    ['ROSN', 'Роснефть', 'Роснефть', 'Нефть и газ', 'AAA'],
    ['SIBN', 'Газпром нефть', 'Газпром нефть', 'Нефть и газ', 'AAA'],
    ['GMKN', 'ГМК Норильский никель', 'Норникель', 'Металлы', 'AAA'],
    ['MTSS', 'МТС', 'МТС', 'Телеком', 'AAA'],
    ['RTKM', 'Ростелеком', 'Ростелеком', 'Телеком', 'AA+'],
    ['DOMRF', 'ДОМ.РФ', 'ДОМ.РФ', 'Финансы', 'AAA'],
    ['VEB', 'ВЭБ.РФ', 'ВЭБ.РФ', 'Финансы', 'AAA'],
    ['ATOM', 'Атомэнергопром', 'Атом', 'Энергетика', 'AAA'],
    ['PHOR', 'ФосАгро', 'ФосАгро', 'Химия', 'AAA'],
    ['MINV', 'Металлоинвест', 'Металлоинвест', 'Металлы', 'AA+'],
    ['PLZL', 'Полюс', 'Полюс', 'Металлы', 'AAA'],
    ['SIBUR', 'Сибур Холдинг', 'Сибур', 'Химия', 'AAA'],
    ['MGNT', 'Магнит', 'Магнит', 'Ритейл', 'AA'],
    ['X5', 'ИКС 5', 'X5', 'Ритейл', 'AAA'],
    ['GTLK', 'ГТЛК', 'ГТЛК', 'Лизинг', 'AA-'],
    ['SMLT', 'Самолёт', 'Самолёт', 'Девелопмент', 'A'],
    ['AFKS', 'АФК Система', 'АФК', 'Холдинги', 'AA-'],
    ['FLOT', 'Совкомфлот', 'Совкомфлот', 'Транспорт', 'AA'],
    ['ALFA', 'Альфа-Банк', 'Альфа', 'Банки', 'AA+'],
    ['NVTK', 'Новатэк', 'Новатэк', 'Нефть и газ', 'AAA'],
    ['SEGZ', 'Сегежа', 'Сегежа', 'Лесная', 'BBB+'],
  ].map(([id, name, short, sector, rating]) => ({ id, name, short, sector, rating }));
  M.issuer = Object.fromEntries(M.issuers.map(i => [i.id, i]));

  M.groupLabel = { GOV: 'Гос', CORP: 'Корп', DER: 'Деривативы' };
  M.kindLabel = { PD: 'ОФЗ-ПД (фикс)', PK: 'ОФЗ-ПК (флоатер)', IN: 'ОФЗ-ИН (линкер)', SOVFX: 'Минфин валютные', FIX: 'Фикс', FLT: 'Флоатер', IRS: 'IRS / OIS', KSW: 'Своп на КС', BAS: 'Базис КС/RUONIA', FXF: 'FX форвард', XCCY: 'XCCY', OTC: 'OTC (по отчёту)' };
  M.kindShort = { PD: 'ПД', PK: 'ПК', IN: 'ИН', SOVFX: 'Минфин вал.', FIX: 'Фикс', FLT: 'Флоат', IRS: 'IRS', KSW: 'КС-своп', BAS: 'Базис', FXF: 'FX fwd', XCCY: 'XCCY', OTC: 'OTC' };
  // строки «типов» для сводной таблицы рисков (порядок — как владелец смотрит)
  M.typeRows = [
    { id: 'GOV.PD', group: 'GOV', kind: 'PD', label: 'ОФЗ-ПД' },
    { id: 'GOV.PK', group: 'GOV', kind: 'PK', label: 'ОФЗ-ПК (флоатеры)' },
    { id: 'GOV.IN', group: 'GOV', kind: 'IN', label: 'ОФЗ-ИН' },
    { id: 'GOV.SOVFX', group: 'GOV', kind: 'SOVFX', label: 'Минфин валютные (евробонды, замещающие)' },
    { id: 'CORP.FIX', group: 'CORP', kind: 'FIX', label: 'Корп. фикс' },
    { id: 'CORP.FLT', group: 'CORP', kind: 'FLT', label: 'Корп. флоатеры' },
    { id: 'DER.IRS', group: 'DER', kind: 'IRS', label: 'IRS / OIS' },
    { id: 'DER.KSW', group: 'DER', kind: 'KSW', label: 'Свопы на КС' },
    { id: 'DER.BAS', group: 'DER', kind: 'BAS', label: 'Базисные свопы' },
    { id: 'DER.FXF', group: 'DER', kind: 'FXF', label: 'FX форварды' },
    { id: 'DER.XCCY', group: 'DER', kind: 'XCCY', label: 'XCCY' },
    { id: 'DER.OTC', group: 'DER', kind: 'OTC', label: 'OTC (по отчёту)' },
  ];

  // ---------- облигационная математика (упрощённо) ----------
  const FREQ = { M: 12, Q: 4, S: 2, A: 1 };
  function priceFromYield(cpn, y, T, f) {
    if (T <= 0) return 100;
    const n = Math.max(1, Math.ceil(T * f - 1e-9)), first = T * f - (n - 1);
    let pv = 0;
    for (let k = 0; k < n; k++) { const tt = first + k; pv += (cpn / f) / Math.pow(1 + y / 100 / f, tt); }
    pv += 100 / Math.pow(1 + y / 100 / f, first + n - 1);
    const accr = (cpn / f) * (1 - first);
    return pv - accr;
  }
  function modDur(cpn, y, T, f) { const h = 0.01; return (priceFromYield(cpn, y - h, T, f) - priceFromYield(cpn, y + h, T, f)) / (2 * h) / priceFromYield(cpn, y, T, f) * 100; }
  M.math = { priceFromYield, modDur };

  // ---------- позиции ----------
  // [id, isin, name, issuer, group, kind, ccy, book, maturity, offer(put/call|null), offerKind, cpnFmt{type,rate|index,margin,freq}, notional(ccy, млн), spreadBp]
  const P = [];
  const B = (o) => P.push(o);
  // ОФЗ-ПД
  [['26207', '2027-02-03', 8.15, 4200], ['26212', '2028-01-19', 7.05, 3100], ['26228', '2030-04-10', 7.65, 2600], ['26244', '2034-03-15', 11.25, 3800],
   ['26233', '2035-07-18', 6.10, -1500], ['26246', '2036-03-12', 12.00, 2400], ['26243', '2038-05-19', 9.80, 1800], ['26247', '2039-05-11', 12.25, 2900],
   ['26248', '2040-05-16', 12.25, 3300], ['26238', '2041-05-15', 7.10, 1200]].forEach(([n, mat, c, nom]) =>
    B({ isin: 'RU000A10' + n, name: 'ОФЗ ' + n + '-ПД', issuer: 'MINFIN', group: 'GOV', kind: 'PD', ccy: 'RUB', book: 'OFZ', maturity: mat, cpn: { type: 'fix', rate: c, freq: 'S' }, notional: nom * 1e6, spreadBp: 0 }));
  // ОФЗ-ПК (RUONIA)
  [['29014', '2027-03-03', 2800], ['29022', '2032-01-14', 4100], ['29024', '2034-09-20', 3600], ['29025', '2037-10-27', 2200]].forEach(([n, mat, nom]) =>
    B({ isin: 'RU000A10' + n, name: 'ОФЗ ' + n + '-ПК', issuer: 'MINFIN', group: 'GOV', kind: 'PK', ccy: 'RUB', book: n === '29014' ? 'HTM' : 'FLOAT', maturity: mat, cpn: { type: 'float', index: 'RUONIA', margin: 0, freq: 'Q' }, notional: nom * 1e6, spreadBp: 0 }));
  // ОФЗ-ИН
  [['52002', '2028-02-02', 650], ['52005', '2033-05-18', 900]].forEach(([n, mat, nom]) =>
    B({ isin: 'RU000A10' + n, name: 'ОФЗ ' + n + '-ИН', issuer: 'MINFIN', group: 'GOV', kind: 'IN', ccy: 'RUB', book: 'OFZ', maturity: mat, cpn: { type: 'linker', rate: 2.5, freq: 'S' }, notional: nom * 1e6, spreadBp: 0 }));
  // Минфин в валюте (R39): замещающие облигации и евробонды — свой тип «Минфин валютные», не «суверены»; эмитент — MINFIN
  B({ isin: 'RU000A105RJ8', name: 'Минфин РФ 12-28 USD', issuer: 'MINFIN', group: 'GOV', kind: 'SOVFX', ccy: 'USD', book: 'FXCR', maturity: '2028-06-24', cpn: { type: 'fix', rate: 12.75, freq: 'S' }, notional: 18e6, spreadBp: 190 });
  B({ isin: 'RU000A105RL4', name: 'Минфин РФ 43 USD', issuer: 'MINFIN', group: 'GOV', kind: 'SOVFX', ccy: 'USD', book: 'FXCR', maturity: '2043-06-24', cpn: { type: 'fix', rate: 5.875, freq: 'S' }, notional: 25e6, spreadBp: 260 });
  B({ isin: 'XS0767472458', name: 'Россия-2042 USD (евробонд)', issuer: 'MINFIN', group: 'GOV', kind: 'SOVFX', ccy: 'USD', book: 'FXCR', maturity: '2042-04-04', cpn: { type: 'fix', rate: 5.625, freq: 'S' }, notional: 6e6, spreadBp: 245 });
  B({ isin: 'RU000A1089H3', name: 'Минфин РФ 27 EUR (замещающая)', issuer: 'MINFIN', group: 'GOV', kind: 'SOVFX', ccy: 'EUR', book: 'FXCR', maturity: '2027-05-27', cpn: { type: 'fix', rate: 1.125, freq: 'A' }, notional: 4e6, spreadBp: 210 });
  B({ isin: 'RU000A108Y82', name: 'Минфин РФ 27 CNY', issuer: 'MINFIN', group: 'GOV', kind: 'SOVFX', ccy: 'CNY', book: 'FXCR', maturity: '2027-12-10', cpn: { type: 'fix', rate: 6.50, freq: 'S' }, notional: 320e6, spreadBp: 120 });
  // корп. фикс RUB
  const cf = [
    ['RZD', 'РЖД 001Р-28R', '2031-09-02', '2027-09-07', 'put', 13.40, 'S', 1500, 95, 'CORP'],
    ['RZD', 'РЖД 001Р-32R', '2036-04-14', null, null, 12.10, 'S', 900, 110, 'CORP'],
    ['SBER', 'Сбер Sb39R', '2029-07-18', null, null, 14.20, 'M', 1200, 85, 'CORP'],
    ['VTB', 'ВТБ Б-1-414', '2028-11-02', '2027-05-06', 'call', 15.10, 'M', 800, 130, 'CORP'],
    ['ROSN', 'Роснефть 002Р-22', '2030-03-15', null, null, 13.75, 'S', 1100, 120, 'CORP'],
    ['SIBN', 'Газпром нефть 003Р-18R', '2029-05-21', null, null, 13.50, 'Q', 700, 105, 'CORP'],
    ['GMKN', 'Норникель БО-001Р-10', '2027-11-24', null, null, 12.85, 'Q', 650, 115, 'CORP'],
    ['MTSS', 'МТС 002Р-07', '2031-06-10', '2028-06-15', 'put', 14.40, 'Q', 600, 145, 'CORP'],
    ['RTKM', 'Ростелеком 002Р-15R', '2030-10-02', '2027-10-07', 'put', 14.90, 'Q', 500, 160, 'CORP'],
    ['DOMRF', 'ДОМ.РФ 001Р-55R', '2032-02-09', null, null, 12.60, 'S', 900, 100, 'CORP'],
    ['ATOM', 'Атомэнергопром 001Р-04', '2029-08-01', null, null, 13.20, 'Q', 750, 90, 'CORP'],
    ['MGNT', 'Магнит БО-004Р-08', '2028-03-29', null, null, 15.20, 'M', 400, 175, 'CORP'],
    ['X5', 'X5 001Р-09', '2029-04-13', '2027-04-20', 'put', 14.75, 'Q', 450, 150, 'CORP'],
    ['SMLT', 'Самолёт БО-П15', '2028-09-07', '2027-03-11', 'put', 19.50, 'M', 250, 520, 'IC-BND'],
    ['AFKS', 'АФК Система 002Р-05', '2029-12-18', '2027-12-22', 'put', 18.25, 'Q', 300, 380, 'IC-BND'],
    ['SEGZ', 'Сегежа 003Р-03R', '2028-06-01', null, null, 21.00, 'M', 120, 950, 'IC-BND'],
    ['GTLK', 'ГТЛК 002Р-04', '2030-07-30', '2027-08-04', 'put', 17.40, 'M', 350, 310, 'CORP'],
    ['PHOR', 'ФосАгро БО-02-04', '2028-10-12', null, null, 13.90, 'Q', 400, 125, 'HTM'],
    ['RZD', 'РЖД 001Р-24R', '2027-06-08', null, null, 8.55, 'S', -300, 80, 'CORP'],
  ];
  cf.forEach(([iss, name, mat, off, offk, c, f, nom, sp, book]) =>
    B({ isin: 'RU000A10' + (hash(name) % 9000 + 1000) + 'X' + (hash(name) % 9), name, issuer: iss, group: 'CORP', kind: 'FIX', ccy: 'RUB', book, maturity: mat, offer: off, offerKind: offk, cpn: { type: 'fix', rate: c, freq: f }, notional: nom * 1e6, spreadBp: sp }));
  // корп. флоатеры RUB
  const fl = [
    ['GAZP', 'Газпром капитал БО-05', '2029-02-12', null, null, 'КС', 130, 'M', 2200, 140],
    ['SBER', 'Сбер Sb46R', '2027-12-01', null, null, 'КС', 110, 'M', 1800, 115],
    ['VTB', 'ВТБ Б-1-371', '2028-04-26', null, null, 'КС', 150, 'Q', 1300, 155],
    ['RZD', 'РЖД 001Р-41R', '2030-08-19', '2028-08-21', 'put', 'КС', 120, 'Q', 1000, 125],
    ['ROSN', 'Роснефть 004Р-06', '2028-10-10', null, null, 'КС', 140, 'M', 1600, 150],
    ['GMKN', 'Норникель БО-001Р-14', '2029-01-23', null, null, 'КС', 135, 'Q', 700, 145],
    ['DOMRF', 'ДОМ.РФ БО-002Р-62', '2031-03-17', null, null, 'RUONIA', 125, 'Q', 900, 130],
    ['VEB', 'ВЭБ.РФ ПБО-002Р-49', '2032-05-12', null, null, 'RUONIA', 140, 'Q', 1100, 150],
    ['ATOM', 'Атомэнергопром 001Р-07', '2030-11-27', null, null, 'КС', 145, 'M', 600, 160],
    ['ALFA', 'Альфа-Банк 002Р-38', '2028-07-05', '2027-07-08', 'call', 'КС', 175, 'M', 500, 190],
    ['MTSS', 'МТС 002Р-11', '2029-09-04', null, null, 'КС', 160, 'M', 650, 170],
    ['FLOT', 'Совкомфлот БО-03', '2028-12-11', null, null, 'КС', 185, 'M', 300, 210, 'IC-BND'],
    ['GTLK', 'ГТЛК 002Р-07', '2031-01-29', null, null, 'КС', 230, 'M', 280, 260, 'IC-BND'],
  ];
  fl.forEach(([iss, name, mat, off, offk, idx, mg, f, nom, sp, book]) =>
    B({ isin: 'RU000A10' + (hash(name) % 9000 + 1000) + 'F' + (hash(name) % 9), name, issuer: iss, group: 'CORP', kind: 'FLT', ccy: 'RUB', book: book || 'FLOAT', maturity: mat, offer: off, offerKind: offk, cpn: { type: 'float', index: idx, margin: mg, freq: f }, notional: nom * 1e6, spreadBp: sp }));
  // корп. валютные
  const fxb = [
    ['GAZP', 'Газпром капитал ЗО27-1-Д', 'USD', '2027-11-29', null, null, 4.95, 'S', 22, 210],
    ['GAZP', 'Газпром капитал ЗО37-Д', 'USD', '2037-08-17', null, null, 7.288, 'S', 15, 300],
    ['GAZP', 'Газпром капитал ЗО27-Е', 'EUR', '2027-11-17', null, null, 2.95, 'A', 12, 230],
    ['GMKN', 'Норникель ЗО2028-Д', 'USD', '2028-10-28', null, null, 2.55, 'S', 9, 250],
    ['SIBN', 'Газпром нефть ЗО2026', 'USD', '2027-04-28', null, null, 4.37, 'S', 6, 190],
    ['ROSN', 'Роснефть 002Р-13 CNY', 'CNY', '2028-11-02', null, null, 7.25, 'Q', 180, 140],
    ['MINV', 'Металлоинвест 001Р-06 CNY', 'CNY', '2027-08-23', null, null, 6.90, 'Q', 120, 175],
    ['PLZL', 'Полюс ПБО2П05 CNY', 'CNY', '2029-06-04', '2027-06-07', 'put', 7.10, 'Q', 150, 160],
    ['NVTK', 'Новатэк 001Р-02 CNY', 'CNY', '2028-05-17', null, null, 6.40, 'S', 140, 135],
    ['SIBUR', 'Сибур 001Р-03 CNY', 'CNY', '2030-02-11', null, null, 7.50, 'Q', 90, 185],
    ['AFKS', 'АФК Система 002Р-07 CNY', 'CNY', '2028-03-15', null, null, 9.00, 'Q', 60, 420, 'IC-BND'],
  ];
  fxb.forEach(([iss, name, ccy, mat, off, offk, c, f, nom, sp, book]) =>
    B({ isin: 'RU000A10' + (hash(name) % 9000 + 1000) + 'V' + (hash(name) % 9), name, issuer: iss, group: 'CORP', kind: 'FIX', ccy, book: book || 'FXCR', maturity: mat, offer: off, offerKind: offk, cpn: { type: 'fix', rate: c, freq: f }, notional: nom * 1e6, spreadBp: sp }));
  // деривативы
  const der = [
    { name: 'IRS RUONIA 2Y pay', kind: 'IRS', ccy: 'RUB', curve: 'RUB RUONIA', tenor: 2, notional: -6000e6, rate: 12.95, book: 'RATES', cpty: 'НКЦ' },
    { name: 'IRS RUONIA 3Y rec', kind: 'IRS', ccy: 'RUB', curve: 'RUB RUONIA', tenor: 3, notional: 3500e6, rate: 12.70, book: 'RATES', cpty: 'НКЦ' },
    { name: 'IRS RUONIA 5Y pay', kind: 'IRS', ccy: 'RUB', curve: 'RUB RUONIA', tenor: 5, notional: -9000e6, rate: 12.40, book: 'RATES', cpty: 'НКЦ' },
    { name: 'IRS RUONIA 10Y pay', kind: 'IRS', ccy: 'RUB', curve: 'RUB RUONIA', tenor: 10, notional: -2500e6, rate: 12.60, book: 'RATES', cpty: 'Сбербанк' },
    { name: 'IRS КС 1Y rec', kind: 'KSW', ccy: 'RUB', curve: 'RUB KEY', tenor: 1, notional: 8000e6, rate: 13.80, book: 'RATES', cpty: 'НКЦ' },
    { name: 'IRS КС 2Y pay', kind: 'KSW', ccy: 'RUB', curve: 'RUB KEY', tenor: 2, notional: -5000e6, rate: 13.10, book: 'RATES', cpty: 'ВТБ' },
    { name: 'Базис КС/RUONIA 3Y', kind: 'BAS', ccy: 'RUB', curve: 'RUB KEY', curve2: 'RUB RUONIA', tenor: 3, notional: 4000e6, rate: 0.12, book: 'RATES', cpty: 'НКЦ' },
    { name: 'USD/RUB fwd 3M sell', kind: 'FXF', ccy: 'USD', curve: 'USD SOFR', curve2: 'RUB RUONIA', tenor: 0.25, notional: -55e6, rate: 83.95, book: 'FXB', cpty: 'НКЦ' },
    { name: 'CNY/RUB fwd 6M sell', kind: 'FXF', ccy: 'CNY', curve: 'CNY XCCY', curve2: 'RUB RUONIA', tenor: 0.5, notional: -700e6, rate: 12.05, book: 'FXB', cpty: 'НКЦ' },
    { name: 'EUR/RUB fwd 1M sell', kind: 'FXF', ccy: 'EUR', curve: 'EUR ESTR', curve2: 'RUB RUONIA', tenor: 0.083, notional: -10e6, rate: 95.70, book: 'FXB', cpty: 'Газпромбанк' },
    { name: 'XCCY CNY/RUB 2Y', kind: 'XCCY', ccy: 'CNY', curve: 'CNY XCCY', curve2: 'RUB RUONIA', tenor: 2, notional: -400e6, rate: 5.10, book: 'FXB', cpty: 'Газпромбанк' },
    // группа продукта отчёта (x_product_group адаптера) — для типа инструмента таблицы «валюта × тип» (lib/instr.js)
    { name: 'Si-12.26 фьючерс USD/RUB sell', kind: 'FXF', productGroup: 'fx_future', ccy: 'USD', curve: 'USD SOFR', curve2: 'RUB RUONIA', tenor: 0.15, notional: -20e6, rate: 84.60, book: 'FXB', cpty: 'MOEX' },
    { name: 'CCS USD/RUB 3Y', kind: 'XCCY', productGroup: 'ccs', ccy: 'USD', curve: 'USD SOFR', curve2: 'RUB RUONIA', tenor: 3, notional: 15e6, rate: 4.10, book: 'FXB', cpty: 'ВТБ' },
    // TRS по отчёту без риска: в таблице «валюта × тип» — «без риска», не ноль
    { name: 'TRS корп. облигации 1Y (по отчёту)', kind: 'OTC', productGroup: 'trs', ccy: 'RUB', curve: 'RUB RUONIA', tenor: 1, notional: 1500e6, rate: 14.50, book: 'RATES', cpty: 'Газпромбанк' },
    // FX-сделки с двумя валютами риска: NDF (расчёт разницы в рублях), кросс без рубля (FX-дельта в двух валютах, риск
    // на двух иностранных кривых), FX-своп (две ноги, риск на implied-кривой юаня), CHF-форвард (своя кривая и валюта)
    { name: 'NDF USD/RUB 6M buy (расчёт в RUB)', kind: 'FXF', productGroup: 'fx_ndf', ccy: 'USD', curve: 'USD SOFR', curve2: 'RUB RUONIA', tenor: 0.5, notional: 30e6, rate: 85.10, settle: 'cash', book: 'FXB', cpty: 'Сбербанк' },
    { name: 'FX fwd USD/CNY 3M buy (кросс)', kind: 'FXF', ccy: 'USD', ccy2: 'CNY', curve: 'USD SOFR', curve2: 'CNY XCCY', tenor: 0.25, notional: 20e6, rate: 7.12, book: 'FXB', cpty: 'ВТБ' },
    { name: 'FX swap CNY/RUB TOM/1M buy/sell', kind: 'FXF', ccy: 'CNY', curve: 'CNY FWD', curve2: 'RUB RUONIA', tenor: 0.083, notional: -300e6, rate: 11.53, near: 11.42, book: 'FXB', cpty: 'НКЦ' },
    // мелкие кроссы: вторая валюта (GBP, AED) — остаток ОВП, в плитке ОВП свёрнут в «+N прочих» (R43); своей кривой
    // у GBP и AED в демо нет — дисконт по кривой первой валюты
    { name: 'FX fwd EUR/GBP 1M buy (кросс)', kind: 'FXF', ccy: 'EUR', ccy2: 'GBP', curve: 'EUR ESTR', curve2: 'EUR ESTR', tenor: 0.083, notional: 40e3, rate: 0.8705, book: 'FXB', cpty: 'ВТБ' },
    { name: 'FX fwd USD/AED 1M buy (кросс)', kind: 'FXF', ccy: 'USD', ccy2: 'AED', curve: 'USD SOFR', curve2: 'USD SOFR', tenor: 0.083, notional: 9e3, rate: 3.6725, book: 'FXB', cpty: 'ВТБ' },
    { name: 'CHF/RUB fwd 6M buy', kind: 'FXF', ccy: 'CHF', curve: 'CHF SARON', curve2: 'RUB RUONIA', tenor: 0.5, notional: 8e6, rate: 107.90, book: 'FXB', cpty: 'Газпромбанк' },
  ];
  der.forEach(d => P.push(Object.assign({ isin: null, issuer: null, group: 'DER', maturity: addMonths(AS_OF, Math.round(d.tenor * 12)), spreadBp: 0, cpn: null }, d)));

  // ---------- оценка позиций ----------
  // разнести позиции по дочерним книгам и фондам УК (детерминированно)
  P.forEach(p => {
    const h = hash(p.name) % 10, fam = SUB[p.book];
    if (fam && h < 6) p.book = fam[h % fam.length][0];
    else if (h === 9 && p.ccy !== 'USD') p.book = p.group === 'DER' ? 'AM-DER' : p.group === 'GOV' ? 'AM-OFZ' : p.ccy === 'CNY' ? 'AM-CNY' : 'AM-BOND';
  });
  // FX-сделка: ноги (как extra.legs прогона), форвард по паритету ставок, MtM, FX-дельта по валютам (p.fxDeltaBy)
  function fxDeal(p) {
    const q = p.ccy2 || 'RUB', t = p.tenor, mat = p.maturity;
    const S = M.fx[p.ccy] / M.fx[q], rb = curveAt(p.curve, t) / 100, rq = curveAt(p.curve2 || 'RUB RUONIA', t) / 100;
    const fwd = S * (1 + rq * t) / (1 + rb * t), dfq = 1 / (1 + rq * t), dfb = 1 / (1 + rb * t);
    const leg = (name, date, n, k, f) => ({ leg: name, valueDate: date, buyCcy: n > 0 ? p.ccy : q, buyAmount: n > 0 ? n : -n * k, sellCcy: n > 0 ? q : p.ccy, sellAmount: n > 0 ? n * k : -n,
      contractRate: k, rateQuote: p.ccy + '/' + q, fwdRate: round(f, 4), points: round(f - S, 4), settlement: p.settle === 'cash' ? 'cash' : 'deliverable' });
    p.legs = [];
    if (p.near != null) p.legs.push(leg('near', addDays(AS_OF, 1), -p.notional, p.near, S));  // ближняя нога (TOM) — по споту
    p.legs.push(leg(p.near != null ? 'far' : 'single', mat, p.notional, p.rate, fwd));
    if (p.settle === 'cash') Object.assign(p.legs[p.legs.length - 1], { settlementCcy: 'RUB', fixingDate: addDays(mat, -2), fixingSource: 'ЦБ', paymentDate: mat });
    // MtM в рублях: (форвард − курс сделки) × сумма × DF второй валюты; ближняя нога TOM — по споту
    p.mv = p.legs.reduce((s, l, i) => s + (l.fwdRate - l.contractRate) * (i === 0 && p.near != null ? -p.notional : p.notional) * (l.leg === 'near' ? 1 : dfq), 0) * M.fx[q];
    // FX-дельта (спот-эквивалент): сумма × DF своей валюты; кросс — ещё и во второй валюте; у свопа ноги гасятся
    const delta = p.legs.reduce((s, l, i) => s + (i === 0 && p.near != null ? -p.notional : p.notional * dfb), 0);
    p.fxDeltaBy = { [p.ccy]: round(delta, 0) };
    if (p.ccy2) p.fxDeltaBy[p.ccy2] = round(-p.notional * p.rate * dfq, 0);
    p.couponFmt = (p.productGroup === 'fx_ndf' ? 'NDF ' : p.near != null ? 'swap ' + p.near + ' / ' : 'fwd ') + p.rate + (p.ccy2 ? ' ' + p.ccy2 : '');
  }
  const positions = P.map((p, i) => {
    p.id = 'P' + String(i + 1).padStart(3, '0');
    const r = rng(p.name);
    const iss = p.issuer ? M.issuer[p.issuer] : null;
    p.issuerName = iss ? iss.short : (p.cpty || '—');
    p.issuerFull = iss ? iss.name : (p.cpty ? 'Контрагент: ' + p.cpty : '—');
    p.sector = iss ? iss.sector : 'Деривативы';
    p.rating = iss ? iss.rating : '—';
    p.bookName = M.book[p.book].name;
    p.entity = M.book[p.book].entity;
    p.desk = M.book[p.book].desk;
    p.groupLabel = M.groupLabel[p.group];
    p.kindLabel = M.kindLabel[p.kind];
    p.risk = {}; M.curves.forEach(c => p.risk[c.id] = new Array(M.buckets.length).fill(0));
    p.cs01b = new Array(M.buckets.length).fill(0);
    const fx = M.fx[p.ccy];
    if (p.group !== 'DER') {
      const mat = yearsTo(p.maturity);
      const ev = p.offer ? { date: p.offer, kind: p.offerKind } : { date: p.maturity, kind: 'mat' };
      p.event = ev; p.tenor = Math.max(0.02, yearsTo(ev.date));
      p.yearsToMat = mat;
      const f = FREQ[p.cpn.freq];
      // формула купона, коротко
      const fl = p.cpn.freq;
      const rateTxt = v => { const t = String(+v.toFixed(3)); const d = (t.split('.')[1] || '').length; return v.toFixed(Math.max(2, d)); };
      if (p.cpn.type === 'fix') p.couponFmt = rateTxt(p.cpn.rate) + '% ' + fl;
      else if (p.cpn.type === 'float') p.couponFmt = p.cpn.index + (p.cpn.margin ? '+' + p.cpn.margin : '') + ' ' + fl;
      else p.couponFmt = p.cpn.rate.toFixed(2) + '%+CPI ' + fl;
      // доходность и цена
      const base = p.ccy === 'RUB' ? curveAt(p.group === 'GOV' ? 'OFZ' : 'RUB RUONIA', p.tenor)
        : curveAt(M.curves.find(c => c.ccy === p.ccy).id, p.tenor);
      if (p.cpn.type === 'fix') {
        p.ytm = round(base + p.spreadBp / 100 + (r() - 0.5) * 0.3, 2);
        p.price = round(priceFromYield(p.cpn.rate, p.ytm, p.tenor, f), 3);
        p.mdur = round(modDur(p.cpn.rate, p.ytm, p.tenor, f), 2);
        p.curCpn = p.cpn.rate;
      } else if (p.cpn.type === 'float') {
        const idx = M.indices[p.cpn.index];
        p.curCpn = round(idx + p.cpn.margin / 100, 2);
        p.ytm = round(curveAt('RUB RUONIA', Math.min(p.tenor, 1)) + p.spreadBp / 100 + (r() - 0.5) * 0.2, 2);
        p.price = round(100 + (p.cpn.margin - p.spreadBp) / 100 * Math.min(p.tenor, 6) * 0.9 + (r() - 0.5) * 0.4, 3);
        p.mdur = round(1 / f * 0.5, 2);
      } else { // linker
        p.curCpn = p.cpn.rate;
        p.ytm = round(6.4 + p.tenor * 0.08 + (r() - 0.5) * 0.3, 2);
        p.price = round(priceFromYield(p.cpn.rate, p.ytm - 2.2, p.tenor, f), 3);
        p.mdur = round(modDur(p.cpn.rate, p.ytm, p.tenor, f), 2);
        p.indexation = round(1.18 + p.tenor * 0.01, 4);
      }
      p.sprDur = round(p.cpn.type === 'float' ? Math.min(p.tenor, 7) * 0.88 : p.mdur, 2);
      const plain = p.cpn.type === 'fix';
      p.gspread = p.ccy === 'RUB' && plain ? Math.round((p.ytm - curveAt('OFZ', p.tenor)) * 100) : null;
      p.zspread = p.cpn.type === 'linker' ? null : p.cpn.type === 'float' ? Math.round(p.spreadBp + (r() - 0.5) * 16)
        : Math.round(p.ccy === 'RUB' ? (p.ytm - curveAt('RUB RUONIA', p.tenor)) * 100 : p.spreadBp + (r() - 0.5) * 20);
      p.spreadLabel = p.cpn.type === 'float' ? 'DM' : 'Z';
      // Z-спред к кривой дисконтирования (R32): у фикса — тот же Z, у флоатера на КС — DM + базис КС − RUONIA на сроке
      // (купоны по прогнозу КС, дисконт по RUONIA), у флоатера на RUONIA — равен DM; как spreadBp ядра в прогоне
      p.zSpreadZ = p.cpn.type === 'linker' ? null : p.cpn.type === 'float' && p.ccy === 'RUB' && p.cpn.index === 'КС'
        ? Math.round(p.zspread + (curveAt('RUB KEY', Math.min(p.tenor, 3)) - curveAt('RUB RUONIA', Math.min(p.tenor, 3))) * 100) : p.zspread;
      p.spreadCurve = p.ccy === 'RUB' && p.zSpreadZ != null ? 'RUB RUONIA' : null;
      p.dmSource = p.cpn.type === 'float' ? 'core' : null;
      p.accruedPct = round(p.curCpn / f * r(), 3);
      const dirty = (p.price + p.accruedPct) / 100 * (p.indexation || 1);
      p.mvCcy = p.notional * dirty;
      p.mv = p.mvCcy * fx;
      p.accrued = p.notional * p.accruedPct / 100 * fx;
      const w = bucketWeights(p.tenor);
      const dv = -p.mv * p.mdur * 1e-4;
      // с 0.2.6 (#32) RUONIA и КБД — в par-котировках, спред — годовой Z: базис ОФЗ (par–par) равен DV01 ОФЗ на КБД,
      // CS01 ≈ DV01 RUONIA (до 0.2.6 было |спред01| = |DV01| / (1 + R), R29)
      const zr = 1;
      if (p.ccy === 'RUB') {
        if (p.cpn.type === 'float') {
          const projCurve = p.cpn.index === 'КС' ? 'RUB KEY' : 'RUB RUONIA';
          const disc = -p.mv * (p.tenor > 0.3 ? Math.min(p.tenor, 3) * 0.08 : 0.02) * 1e-4;
          const wd = bucketWeights(Math.min(p.tenor, 3));
          wd.forEach((x, k) => { p.risk['RUB RUONIA'][k] += disc * x; p.risk[projCurve][k] += -disc * x * 0.85; });
          p.risk['RUB RUONIA'][0] += dv * 0.6;
          if (p.group === 'GOV') bucketWeights(p.tenor).forEach((x, k) => p.risk['RUB OFZ'][k] += -p.mv * p.sprDur * 1e-4 * x);
        } else {
          w.forEach((x, k) => p.risk['RUB RUONIA'][k] += dv * x);
          if (p.group === 'GOV') w.forEach((x, k) => p.risk['RUB OFZ'][k] += dv * x * zr);
        }
        if (p.group === 'CORP') bucketWeights(p.tenor).forEach((x, k) => p.cs01b[k] += -p.mv * p.sprDur * 1e-4 * x * (p.cpn.type === 'fix' ? zr : 1));
      } else {
        const cid = M.curves.find(c => c.ccy === p.ccy).id;
        w.forEach((x, k) => p.risk[cid][k] += dv * x);
        w.forEach((x, k) => p.cs01b[k] += dv * x * 0.95);
      }
      p.fxDeltaBy = p.ccy === 'RUB' ? {} : { [p.ccy]: p.mvCcy };
      // кэрри за день — купонный доход без фондирования (#168); фондирование — отдельно: −MV × ставка (HTM — не
      // фондируется в демо)
      const fundRate = p.ccy === 'RUB' ? M.indices['КС'] + 0.25 : p.ccy === 'CNY' ? M.indices['RUSFAR CNY'] + 0.4 : 5.0;
      p.fundRate = fundRate;
      const earn = p.cpn.type === 'fix' ? p.ytm : p.cpn.type === 'float' ? p.curCpn : p.ytm + M.indices['CPI г/г'];
      p.carryDay = p.book === 'HTM' ? p.mv * earn / 100 / 365 * 0.3 : p.mv * earn / 100 / 365;
      p.fundingDay = p.book === 'HTM' ? 0 : -p.mv * fundRate / 100 / 365;
      p.rollDay = p.cpn.type === 'fix' ? -p.mv * p.mdur * (r() - 0.35) * 0.5e-4 / 30 : 0;
      p.lastPrice = p.price;
      p.prevPrice = round(p.price - (r() - 0.55) * 0.6, 3);
      p.bid = round(p.price - 0.03 - r() * 0.08, 3); p.ask = round(p.price + 0.03 + r() * 0.08, 3);
      p.volumeDay = Math.round(r() * 3e9 / (p.group === 'GOV' ? 1 : 3));
    } else {
      p.event = { date: p.maturity, kind: 'mat' }; p.tenor = p.tenor; p.yearsToMat = p.tenor;
      p.couponFmt = p.kind === 'FXF' ? 'fwd ' + p.rate : p.kind === 'BAS' ? 'КС−RUONIA ' + (p.rate * 100).toFixed(0) + 'бп' : 'fix ' + p.rate.toFixed(2) + '%';
      const ann = p.tenor * 0.82;
      const sgn = p.notional > 0 ? 1 : -1;
      const nrub = Math.abs(p.notional) * M.fx[p.ccy];
      const w = bucketWeights(p.tenor);
      if (p.kind === 'IRS' || p.kind === 'KSW') {
        // receiver: длинная ставка => DV01 < 0
        w.forEach((x, k) => p.risk[p.curve][k] += -sgn * nrub * ann * 1e-4 * x);
      } else if (p.kind === 'BAS') {
        w.forEach((x, k) => { p.risk['RUB KEY'][k] += -nrub * ann * 1e-4 * x; p.risk['RUB RUONIA'][k] += nrub * ann * 1e-4 * x * 0.98; });
      } else if (p.kind === 'FXF' || p.kind === 'XCCY') {
        // иностранная нога — на кривой валюты, вторая — на RUONIA (у кросса — на кривой второй валюты)
        w.forEach((x, k) => { p.risk[p.curve][k] += -sgn * nrub * ann * 1e-4 * x; p.risk[p.curve2 || 'RUB RUONIA'][k] += sgn * nrub * ann * 1e-4 * x; });
      }
      p.mv = (rng(p.name + 'mv')() - 0.5) * nrub * 0.01;
      p.fxDeltaBy = (p.kind === 'FXF' || p.kind === 'XCCY') ? { [p.ccy]: p.notional } : {};
      if (p.kind === 'FXF' && p.productGroup !== 'fx_future') fxDeal(p);
      p.mvCcy = p.mv / M.fx[p.ccy];
      p.accrued = 0; p.price = null; p.ytm = p.rate; p.mdur = null;
      p.carryDay = p.kind === 'IRS' || p.kind === 'KSW' ? sgn * nrub * (p.rate - (p.kind === 'KSW' ? M.indices['КС'] : M.indices['RUONIA'])) / 100 / 365
        : p.kind === 'BAS' ? nrub * p.rate / 100 / 365 * 0.6 : -sgn * nrub * (M.indices['RUONIA'] - 5) / 100 / 365 * 0.15;
      p.rollDay = 0; p.fundingDay = 0; p.cs01b.fill(0);
      p.zspread = null; p.zSpreadZ = null; p.gspread = null;
    }
    p.fxDelta = p.fxDeltaBy[p.ccy] || 0;            // в валюте позиции; все валюты — p.fxDeltaBy
    p.dv01ByCurve = {}; let tot = 0;
    M.curves.forEach(c => { const s = p.risk[c.id].reduce((a, b) => a + b, 0); p.dv01ByCurve[c.id] = s; if (c.kind === 'rate') tot += s; });
    p.dv01 = tot;                                   // по ставкам (без базиса ОФЗ)
    p.basis01 = p.dv01ByCurve['RUB OFZ'];
    // собственный спред фикс-ОФЗ к КБД (0.2.6): ≈ её DV01 на КБД, по тем же бакетам
    const fixOfz = p.group === 'GOV' && p.ccy === 'RUB' && p.cpn && p.cpn.type === 'fix';
    p.ofzSprb = fixOfz ? p.risk['RUB OFZ'].slice() : new Array(p.risk['RUB OFZ'].length).fill(0);
    p.ofzSpr01 = fixOfz ? p.basis01 : 0;
    p.cs01 = p.cs01b.reduce((a, b) => a + b, 0);
    p.notionalRub = p.notional * M.fx[p.ccy];
    // PnL дня: разложение по факторам
    let rates = 0; M.curves.forEach(c => p.risk[c.id].forEach((v, k) => rates += v * c.chg[k]));
    const sprMove = p.group === 'CORP' ? Math.round((r() - 0.5) * 12) : 0;
    p.spreadChgBp = sprMove;
    const spread = p.cs01 * sprMove;
    const fxp = Object.entries(p.fxDeltaBy).reduce((s, [c, a]) => s + a * M.fx[c] * M.fxChgPct[c] / 100, 0);
    const carry = p.carryDay + p.rollDay, funding = p.fundingDay;
    const other = (r() - 0.5) * Math.abs(p.mv || p.notionalRub * 0.01) * 0.0006;
    const traded = r() < 0.12;
    const trading = traded ? (r() - 0.4) * Math.abs(p.notionalRub) * 0.0004 : 0;
    p.tradedToday = traded;
    p.tradeToday = traded ? { qty: Math.round((r() - 0.5) * 0.3 * Math.abs(p.notional) / 1e6) * 1e6, price: p.price } : null;
    p.pnl = { carry, funding, rates, spread, fx: fxp, trading, other, total: carry + funding + rates + spread + fxp + trading + other };
    p.funding = p.group === 'DER' || p.book === 'HTM' ? 0 : funding;  // за период (день), как p.funding прогона
    const scale = Math.abs(p.mv || p.notionalRub * 0.02);
    p.pnlMtd = (p.carryDay + p.fundingDay) * 15 + p.pnl.total + (r() - 0.42) * scale * 0.004;
    p.pnlYtd = (p.carryDay + p.fundingDay) * 200 + p.pnlMtd + (r() - 0.35) * scale * 0.025;
    if (p.cpn && p.cpn.type === 'float') { p.ytmProxy = p.ytm; p.ytm = null; p.dm = p.zspread; }
    // портфель (колонка Portfolio отчёта): HTM-книга — HTM, часть торговых бумаг — AFS, остальное — торговый
    const bt = (M.books.find(b => b.id === p.book) || {}).type;
    p.portfolio = bt === 'HTM' ? 'HTM' : bt === 'TRADING' && /[05]$/.test(p.id) ? 'AFS' : 'Торговый';
    p.exchName = p.name; p.alias = null;
    p.searchBase = [p.name, p.isin, p.issuerName, p.issuerFull, p.book, p.bookName, p.ccy, p.kindLabel, p.groupLabel, p.sector, p.couponFmt, p.portfolio].join(' ').toLowerCase();
    p.searchText = p.searchBase;
    return p;
  });
  M.positions = positions;
  M.pos = Object.fromEntries(positions.map(p => [p.id, p]));
  // ---------- фондирование (демо, B1): ставка фондирования × MV бумаг за день, отдельно от кэрри (p.fundingDay, #168) ----------
  (function () {
    const byBook = {}, byEntity = {}; let total = 0;
    positions.forEach(p => {
      if (p.group === 'DER' || p.fundRate == null || p.book === 'HTM') return;
      const v = p.fundingDay, bk = M.book[p.book] || {};
      const b = byBook[p.book] || (byBook[p.book] = { total: 0, inPositions: 0, bookLevel: 0, problems: 0, base: 'mv_clean', writeOff: null });
      b.total += v; b.inPositions += v; total += v;
      byEntity[bk.entity || '—'] = (byEntity[bk.entity || '—'] || 0) + v;
    });
    M.funding = { state: 'demo', text: 'демо-набор: ставка фондирования × MV, в PnL позиций', period: null, total, inPositions: total,
      bookLevel: 0, byBook, byEntity, problems: [], provisional: [] };
  })();

  // ---------- имена бумаг: алиасы по ISIN в памяти (R33; на прогоне — таблица instrument_alias сервера) ----------
  const now = () => new Date().toISOString();
  const aliasRevs = { 'RU000A105RL4': [{ rev_id: 1, alias: 'Россия-2043 USD (евробонд)', changed_by: 'demo', changed_at: AS_OF + 'T09:00:00Z' }] };
  M.aliases = {};
  const applyAlias = (isin, alias) => {
    const last = (aliasRevs[isin] || [])[0];
    if (alias) M.aliases[isin] = { alias, changedBy: last.changed_by, changedAt: last.changed_at }; else delete M.aliases[isin];
    positions.forEach(p => { if (p.isin === isin) { p.alias = alias || null; p.name = p.alias || p.exchName; p.searchText = (p.alias ? p.alias.toLowerCase() + ' ' : '') + p.searchBase; } });
  };
  Object.keys(aliasRevs).forEach(isin => applyAlias(isin, aliasRevs[isin][0].alias));
  let revId = 1;
  M.aliasApi = {
    editable: true, demo: true,
    readOnlyReason: '',
    // как PUT /api/v1/instruments/alias: пробелы схлопываются, пустое — сброс, не длиннее 120 знаков
    put(isin, raw) {
      if (raw != null && /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(String(raw))) return Promise.resolve({ status: 422, body: { code: 'invalid_parameter', message: 'the alias has control characters' } });
      const alias = raw == null ? null : String(raw).split(/\s+/).filter(Boolean).join(' ') || null;
      if (alias && alias.length > 120) return Promise.resolve({ status: 422, body: { code: 'invalid_parameter', message: 'the alias is longer than 120 characters' } });
      const revs = aliasRevs[isin] = aliasRevs[isin] || [], last = revs[0];
      if ((last ? last.alias : null) === alias) return Promise.resolve({ status: 200, body: { isin, alias, changed_by: last && last.changed_by, changed_at: last && last.changed_at, changed: false } });
      revs.unshift({ rev_id: ++revId, alias, changed_by: 'demo', changed_at: now() });
      return Promise.resolve({ status: 200, body: { isin, alias, changed_by: 'demo', changed_at: revs[0].changed_at, changed: true } });
    },
    history: isin => Promise.resolve((aliasRevs[isin] || []).slice()),
  };

  // сортировка по умолчанию: валюта → группа → эмитент → флоатер/фикс → срок до события
  const groupOrder = { GOV: 0, CORP: 1, DER: 2 };
  const kindOrder = { PD: 0, PK: 1, IN: 2, SOVFX: 3, FLT: 0, FIX: 1, IRS: 0, KSW: 1, BAS: 2, FXF: 3, XCCY: 4, OTC: 5 };
  M.defaultOrder = (a, b) => (M.ccyOrder[a.ccy] - M.ccyOrder[b.ccy]) || (groupOrder[a.group] - groupOrder[b.group]) ||
    (a.issuerName || '').localeCompare(b.issuerName || '', 'ru') || (kindOrder[a.kind] - kindOrder[b.kind]) || (a.tenor - b.tenor);
  positions.sort(M.defaultOrder);

  // ---------- история деска ----------
  M.histDates = businessDaysBack(130);
  (function deskHistory() {
    const n = M.histDates.length;
    const today = {
      dv01: {}, mv: {}, notionalType: {}, books: {},
    };
    M.curves.forEach(c => today.dv01[c.id] = positions.reduce((s, p) => s + p.dv01ByCurve[c.id], 0));
    const series = {};
    function walk(key, endVal, vol, trend) {
      const r = rng('hist' + key); const out = new Array(n); out[n - 1] = endVal;
      for (let i = n - 2; i >= 0; i--) out[i] = out[i + 1] - (r() - 0.5 + (trend || 0)) * vol * 2 + (r() < 0.04 ? (r() - 0.5) * vol * 12 : 0);
      return out;
    }
    series.dv01 = {}; M.curves.forEach(c => series.dv01[c.id] = walk('dv' + c.id, today.dv01[c.id], Math.abs(today.dv01[c.id]) * 0.03 + 2e4, 0.02));
    series.cs01 = walk('cs01', positions.reduce((s, p) => s + p.cs01, 0), 3e4);
    series.dv01Book = {}; M.books.forEach(b => series.dv01Book[b.id] = walk('db' + b.id, positions.filter(p => p.book === b.id).reduce((s, p) => s + p.dv01, 0), 2.5e4, 0.01));
    series.mvType = {}; M.typeRows.forEach(t => series.mvType[t.id] = walk('mv' + t.id, positions.filter(p => p.group + '.' + p.kind === t.id).reduce((s, p) => s + (p.group === 'DER' ? Math.abs(p.notionalRub) : p.mv), 0), 4e8, -0.03).map(v => Math.max(0, v)));
    series.mvCcy = {}; M.ccys.forEach(c => series.mvCcy[c] = walk('mvc' + c, positions.filter(p => p.ccy === c && p.group !== 'DER').reduce((s, p) => s + p.mv, 0), 6e8, -0.02).map(v => Math.max(0, v)));
    const pr = rng('pnlhist');
    series.pnl = { carry: [], funding: [], rates: [], spread: [], fx: [], trading: [], other: [] };
    const tp = positions.reduce((a, p) => { Object.keys(series.pnl).forEach(k => a[k] = (a[k] || 0) + p.pnl[k]); return a; }, {});
    for (let i = 0; i < n; i++) {
      const last = i === n - 1;
      series.pnl.carry.push(last ? tp.carry : tp.carry * (0.8 + pr() * 0.4));
      series.pnl.funding.push(last ? tp.funding : tp.funding * (0.95 + pr() * 0.1));
      series.pnl.rates.push(last ? tp.rates : (pr() - 0.5) * 30e6 + rndn() * 6e6);
      series.pnl.spread.push(last ? tp.spread : (pr() - 0.5) * 9e6);
      series.pnl.fx.push(last ? tp.fx : (pr() - 0.5) * 12e6);
      series.pnl.trading.push(last ? tp.trading : (pr() < 0.4 ? (pr() - 0.4) * 3e6 : 0));
      series.pnl.other.push(last ? tp.other : (pr() - 0.5) * 6e5);
    }
    series.pnl.total = series.pnl.carry.map((_, i) => Object.keys(series.pnl).reduce((s, k) => s + series.pnl[k][i], 0));
    M.hist = series;
  })();

  // история одной позиции (лениво): цена, доходность, спред, номинал, DV01
  M.posHistory = function (id) {
    const p = M.pos[id]; const r = rng('ph' + id);
    const dates = businessDaysBack(250); const n = dates.length;
    const price = new Array(n), ytm = new Array(n), spr = new Array(n), notional = new Array(n), dv01 = new Array(n), bench = new Array(n);
    price[n - 1] = p.price; ytm[n - 1] = p.ytm; spr[n - 1] = p.zspread; notional[n - 1] = p.notional; dv01[n - 1] = p.dv01;
    const ofzT = p.ccy === 'RUB' ? curveAt('OFZ', p.tenor || 1) : null; bench[n - 1] = ofzT;
    const vol = p.group === 'DER' ? 0.04 : (p.mdur || 0.3) * 0.08 + 0.02;
    let pos = p.notional;
    for (let i = n - 2; i >= 0; i--) {
      const dy = (r() - 0.5) * 0.12 + (i < 60 ? -0.004 : 0.003);
      ytm[i] = ytm[i + 1] == null ? null : round(ytm[i + 1] - dy, 3);
      bench[i] = bench[i + 1] == null ? null : round(bench[i + 1] - dy * 0.8 + (r() - 0.5) * 0.03, 3);
      price[i] = price[i + 1] == null ? null : round(price[i + 1] + dy * (p.mdur || 0.3) + (r() - 0.5) * vol * 0.2, 3);
      spr[i] = spr[i + 1] == null ? null : Math.round(spr[i + 1] + (r() - 0.5) * 6);
      if (r() < 0.035) pos = pos - Math.round((r() - 0.45) * Math.abs(p.notional) * 0.25 / 1e6) * 1e6;
      notional[i] = pos;
      dv01[i] = p.notional ? p.dv01 * pos / p.notional : 0;
    }
    // риск и MV — номинал дня × сегодняшние чувствительности (в демо прогонов по датам нет: fromRuns = false)
    const sc = i => p.notional ? notional[i] / p.notional : 0;
    const cs01 = dates.map((_, i) => p.cs01 * sc(i)), basis = dates.map((_, i) => p.basis01 * sc(i));
    const mv = dates.map((_, i) => p.group === 'DER' ? 0 : p.mv * sc(i) * (p.price && price[i] != null ? price[i] / p.price : 1));
    const curves = {};
    Object.keys(p.dv01ByCurve).forEach(c => { if (p.dv01ByCurve[c] && (M.curve[c] || {}).kind !== 'basis') curves[c] = dates.map((_, i) => p.dv01ByCurve[c] * sc(i)); });
    // флоатер (R35): DM ядра — по прогонам последних недель, оценка по цене — за всю историю, как у живого прогона
    const dm = p.cpn && p.cpn.type === 'float' ? { dm: spr.map((v, i) => i >= n - 15 ? v : null), dmPx: spr.slice() } : {};
    return Object.assign({ dates, price, ytm, spr, notional, dv01, cs01, basis, mv, curves, bench, fromRuns: false }, dm);
  };
  M.histSource = 'positions';

  // ---------- денежные потоки портфеля ----------
  M.cashflows = (function () {
    const out = [], end = addMonths(AS_OF, 18);
    positions.forEach(p => {
      if (p.group === 'DER') {
        if (p.maturity <= end) out.push({ date: p.maturity, pos: p.id, kind: 'mat', ccy: p.ccy, amount: p.kind === 'FXF' ? p.notional : 0, note: 'исполнение' });
        return;
      }
      const f = FREQ[p.cpn.freq], step = 12 / f;
      let d = p.maturity; const dates = [];
      while (d > AS_OF) { dates.push(d); d = addMonths(d, -step); }
      dates.reverse().forEach(dt => { if (dt <= end && dt <= p.event.date) out.push({ date: dt, pos: p.id, kind: 'cpn', ccy: p.ccy, amount: p.notional * (p.cpn.type === 'float' ? p.curCpn : p.curCpn) / 100 / f, est: p.cpn.type !== 'fix' }); });
      if (p.event.date <= end) out.push({ date: p.event.date, pos: p.id, kind: p.event.kind === 'mat' ? 'mat' : p.event.kind, ccy: p.ccy, amount: p.notional * (p.indexation || 1) });
    });
    out.forEach(c => { c.amountRub = c.amount * M.fx[c.ccy]; });
    return out.sort((a, b) => a.date.localeCompare(b.date));
  })();

  // ---------- рыночная «ойкумена»: валютные бонды российских эмитентов ----------
  M.universeFx = (function () {
    const r = rng('ufx'); const out = [];
    const names = [
      ['GAZP', 'Газпром капитал', ['ЗО26-1-Д', 'ЗО27-1-Д', 'ЗО28-Д', 'ЗО29-1-Д', 'ЗО30-Д', 'ЗО34-Д', 'ЗО37-Д', 'ЗО27-Е', 'ЗО28-Е', 'БО-003Р-02 CNY', 'БО-003Р-05 CNY']],
      ['GMKN', 'Норникель', ['ЗО2026-Д', 'ЗО2028-Д', 'БО-001Р-11 CNY', 'БО-001Р-13 CNY']],
      ['SIBN', 'Газпром нефть', ['ЗО2026', 'ЗО2027', '003Р-07R CNY']],
      ['ROSN', 'Роснефть', ['002Р-13 CNY', '002Р-15 CNY', '002Р-17 CNY']],
      ['NVTK', 'Новатэк', ['001Р-02 CNY', 'ЗО28-Д']],
      ['PLZL', 'Полюс', ['ПБО2П05 CNY', 'ПБО2П06 USD', 'ЗО28']],
      ['MINV', 'Металлоинвест', ['001Р-06 CNY', '001Р-08 CNY', 'ЗО28-Д']],
      ['SIBUR', 'Сибур', ['001Р-03 CNY', 'ЗО25']],
      ['PHOR', 'ФосАгро', ['ЗО2028-Д', 'БО-02-03 CNY']],
      ['MTSS', 'МТС', ['ЗО2026']],
      ['RZD', 'РЖД', ['ЗО26-Е', 'ЗО27-Е', 'ЗО28-Ф', 'ЗО29-Е', 'ЗО30-Ф', 'CNY 001Р-01']],
      ['AFKS', 'АФК Система', ['002Р-07 CNY']],
      ['MINFIN', 'Минфин РФ', ['12-28 USD', '12-43 USD', '27 CNY', '32 CNY', '28 EUR', '36 EUR']],
      ['VTB', 'ВТБ', ['Б-1-CNY-3', 'ЗО-Т2']],
      ['SBER', 'Сбер', ['SbCNY-2', 'SbCNY-4']],
      ['ALFA', 'Альфа-Банк', ['002Р-CNY-1']],
      ['ATOM', 'Атомэнергопром', ['001Р-CNY-1']],
      ['X5', 'X5', ['CNY-001']],
      ['SEGZ', 'Сегежа', ['CNY-002']],
      ['FLOT', 'Совкомфлот', ['ЗО2028-Д']],
    ];
    names.forEach(([iss, issName, list]) => list.forEach(s => {
      const ccy = /CNY/.test(s) ? 'CNY' : /Е$|EUR|-Е/.test(s) ? 'EUR' : /Ф$/.test(s) ? 'CHF' : 'USD';
      const ccyN = ccy === 'CHF' ? 'EUR' : ccy;
      const mat = addDays(AS_OF, Math.round(60 + r() * 365 * (ccy === 'CNY' ? 4 : 8)));
      const hasPut = r() < 0.18; const put = hasPut ? addDays(AS_OF, Math.round(20 + r() * 400)) : null;
      const freq = ccyN === 'CNY' ? pick(['Q', 'Q', 'S', 'M']) : pick(['S', 'S', 'A']);
      const out_ = (ccyN === 'CNY' ? 2e9 + r() * 18e9 : 2e8 + r() * 1.3e9);
      const cpn = ccyN === 'CNY' ? 5.8 + r() * 3.5 : ccyN === 'USD' ? 3 + r() * 6 : 1.5 + r() * 3;
      const t = yearsTo(put || mat);
      const ytm = (ccyN === 'CNY' ? 6.2 : ccyN === 'USD' ? 6.8 : 5.0) + t * 0.15 + (r() - 0.5) * 1.2 + (M.issuer[iss].rating < 'AA' ? 0 : 1.5);
      const b = { isin: 'RU000A10' + (hash(issName + s) % 9000 + 1000) + 'Z', issuer: iss, issuerName: issName, name: issName + ' ' + s, ccy: ccyN, origCcy: ccy, maturity: mat, put, freq, outstanding: out_, cpn: round(cpn, 2), ytm: round(ytm, 2), tenor: t, rating: M.issuer[iss].rating, sector: M.issuer[iss].sector };
      b.held = positions.some(p => p.exchName.replace(/ CNY$/, '').indexOf(s.replace(/ CNY$/, '')) >= 0 && p.issuer === iss);
      const step = 12 / FREQ[freq]; let d = mat; const cps = [];
      while (d > AS_OF) { cps.push(d); d = addMonths(d, -step); }
      b.coupons = cps.reverse();
      out.push(b);
    }));
    return out;
  })();
  // рублёвая «ойкумена» — для агрегатов выплат и карты доходностей
  M.universeRub = (function () {
    const r = rng('urub'); const out = [];
    const ratings = ['AAA', 'AAA', 'AA+', 'AA', 'AA', 'AA-', 'A+', 'A', 'A-', 'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'B'];
    const sectors = ['Банки', 'Нефть и газ', 'Металлы', 'Транспорт', 'Телеком', 'Девелопмент', 'Лизинг', 'МФО', 'Ритейл', 'Химия', 'Энергетика', 'Финансы', 'Субфедеральные'];
    for (let i = 0; i < 640; i++) {
      const rt = i < 40 ? 'AAA' : pick(ratings);
      const sec = i < 26 ? 'ОФЗ' : pick(sectors);
      const isOfz = sec === 'ОФЗ';
      const flt = !isOfz && r() < 0.42;
      const t = 0.1 + Math.pow(r(), isOfz ? 0.7 : 1.6) * (isOfz ? 16 : 7);
      const put = !isOfz && r() < 0.25;
      const ratingPrem = { 'AAA': 0.9, 'AA+': 1.3, 'AA': 1.7, 'AA-': 2.2, 'A+': 2.9, 'A': 3.6, 'A-': 4.4, 'BBB+': 5.5, 'BBB': 6.8, 'BBB-': 8.5, 'BB+': 10.5, 'BB': 13, 'B': 17 }[rt];
      const ytm = isOfz ? curveAt('OFZ', t) + (r() - 0.5) * 0.25 : curveAt('RUB RUONIA', flt ? 0.5 : t) + ratingPrem + (r() - 0.5) * 1.2;
      const size = isOfz ? 1e11 + r() * 4e11 : rt >= 'BBB' && rt[0] === 'B' ? 5e8 + r() * 4e9 : 3e9 + Math.pow(r(), 2) * 6e10;
      const freq = isOfz ? 'S' : pick(['M', 'M', 'Q', 'Q', 'S']);
      const d = addDays(AS_OF, Math.round(t * 365.25));
      const cpn = isOfz ? round(ytm + (r() - 0.5) * 4, 2) : flt ? null : round(ytm + (r() - 0.6) * 3, 2);
      out.push({ id: 'U' + i, name: isOfz ? 'ОФЗ ' + (26200 + i) : sec + ' ' + rt + ' #' + i, sector: sec, rating: rt, flt, isOfz, tenor: t, ytm: round(ytm, 2), dur: flt ? 0.2 : round(Math.min(t, t * (1 - ytm / 100 * 0.35)), 2), size, freq, maturity: put ? null : d, put: put ? d : null, cpn, spread: Math.round((ytm - curveAt('OFZ', t)) * 100) });
    }
    return out;
  })();

  // ---------- рынок: изменения дня и «пульс» ----------
  M.marketMovers = M.universeRub.filter(b => b.size > 8e9).slice(0, 60).map(b => { const r = rng('mv' + b.id); return { name: b.name, rating: b.rating, sector: b.sector, ytm: b.ytm, chgBp: Math.round((r() - 0.5) * 40), vol: Math.round(r() * 4e9) }; });
  M.cbr = { next: { date: '2026-10-24', label: 'Заседание ЦБ (опорное)', consensus: '−50 б.п.', priced: '−38 б.п.' }, meetings: ['2026-10-24', '2026-12-18', '2027-02-13', '2027-03-20', '2027-04-24'] };

  M.cbr.past = [{ date: '2026-09-12', decision: 'cut', decisionText: 'снижение', rate: 17, prev: 18 }];
  M.cbr.estimated = [];
  M.cbr.keyRate = 17;

  // ---------- календарь рынка: агрегаты той же формы, что у real.js (/api/v1/calendar/market) ----------
  // из демо-«ойкумен»: купоны по частоте от погашения назад, оферты и погашения — объём выпуска
  let calCache;
  Object.defineProperty(M, 'marketCal', { configurable: true, get() {
    if (calCache) return calCache;
    const E = addDays(AS_OF, 400), BIG = 1e10 * ((M.toRepr && M.toRepr.RUB) || 1);
    const TYPES = [['PD', 'ОФЗ-ПД'], ['PK', 'ОФЗ-ПК'], ['IN', 'ОФЗ-ИН'], ['CORP_FIX', 'Корп. фикс'], ['CORP_FLT', 'Корп. флоатеры'], ['SUB', 'Субфед. и муни'], ['OTHER', 'Прочие (ИЦБ, ЦБ, структурные)']];
    const rubEv = [], fxEv = [];
    M.universeRub.forEach(b => {
      const d0 = b.maturity || b.put, fq = FREQ[b.freq], rate = b.cpn != null ? b.cpn : b.ytm;
      const type = b.isOfz ? 'PD' : b.flt ? 'CORP_FLT' : 'CORP_FIX';
      const base = { secid: b.id, name: b.name, issuer: b.sector, type, sector: b.sector, ccy: 'RUB', outstanding: b.size, big: b.size >= BIG, held: false };
      let d = fq ? d0 : null;
      while (d && d > AS_OF) { if (d <= E) rubEv.push(Object.assign({ date: d, kind: 'cpn', amount: b.size * rate / 100 / fq, est: !!b.flt }, base)); d = addMonths(d, -12 / fq); }
      if (d0 && d0 > AS_OF && d0 <= E) rubEv.push(Object.assign({ date: d0, kind: b.put ? 'put' : 'mat', amount: b.size, est: !!b.put }, base));
    });
    M.universeFx.forEach(b => {
      const fq = FREQ[b.freq], base = { secid: b.isin, name: b.name, issuer: b.issuerName, type: b.ccy, sector: b.sector, ccy: b.ccy, outstanding: b.outstanding, held: b.held };
      b.coupons.forEach(d => { if (d > AS_OF && d <= E) fxEv.push(Object.assign({ date: d, kind: 'cpn', amount: b.outstanding * b.cpn / 100 / fq, est: false }, base)); });
      if (b.put && b.put > AS_OF && b.put <= E) fxEv.push(Object.assign({ date: b.put, kind: 'put', amount: b.outstanding, est: true }, base));
      if (b.maturity > AS_OF && b.maturity <= E) fxEv.push(Object.assign({ date: b.maturity, kind: 'mat', amount: b.outstanding, est: false }, base));
    });
    const agg = (ev, keyFn, fields) => {
      const m = new Map();
      ev.forEach(x => { const k = keyFn(x).join('|'); let o = m.get(k); if (!o) { o = {}; fields.forEach((f, i) => o[f] = keyFn(x)[i]); o.amount = 0; o.count = 0; o.bigAmount = 0; o.bigCount = 0; o.estAmount = 0; m.set(k, o); }
        o.amount += x.amount; o.count++; if (x.big) { o.bigAmount += x.amount; o.bigCount++; } if (x.est) o.estAmount += x.amount; });
      return [...m.values()];
    };
    calCache = {
      source: 'demo', staticDate: AS_OF, asOf: AS_OF, horizonEnd: E, bigRub: BIG, coverage: {},
      types: TYPES.map(([id, label]) => ({ id, label })), ccys: [...new Set(fxEv.map(x => x.ccy))].sort(),
      rub: agg(rubEv, x => [x.date, x.type, x.kind], ['date', 'type', 'kind']),
      rubSector: agg(rubEv, x => [x.date.slice(0, 7), x.sector, x.kind], ['month', 'sector', 'kind']),
      rubBig: rubEv.filter(x => x.big && x.kind !== 'cpn').map(x => ({ date: x.date, kind: x.kind, secid: x.secid, name: x.name, type: x.type, amount: x.amount, held: false, pos: null })).sort((a, b) => a.date.localeCompare(b.date)),
      fx: agg(fxEv, x => [x.date, x.ccy, x.kind], ['date', 'ccy', 'kind']),
      fxIssuers: agg(fxEv, x => [x.date.slice(0, 7), x.ccy, x.issuer, x.kind], ['month', 'ccy', 'issuer', 'kind']),
      fxEvents: fxEv.filter(x => x.kind !== 'cpn').map(x => ({ date: x.date, kind: x.kind, secid: x.secid, name: x.name, issuer: x.issuer, ccy: x.ccy, amount: x.amount, held: x.held, pos: null })).sort((a, b) => a.date.localeCompare(b.date)),
      notes: ['демо: выплаты рынка придуманы по демо-выпускам'],
      flows(q) {
        q = q || {};
        const ev = (q.market === 'fx' ? fxEv : rubEv).filter(x => (!q.from || x.date >= q.from) && (!q.to || x.date <= q.to) && (!q.type || x.type === q.type) &&
          (!q.kind || (q.kind === 'red' ? x.kind !== 'cpn' : q.kind === 'redemption' ? (x.kind === 'mat' || x.kind === 'amort') : q.kind === 'offer' ? (x.kind === 'put' || x.kind === 'call') : x.kind === q.kind)) && (!q.ccy || x.ccy === q.ccy) && (!q.sector || x.sector === q.sector) &&
          (!q.big || x.big) && (!q.issuer || x.issuer === q.issuer) && (!q.secid || x.secid === q.secid));
        const rows = ev.slice().sort((a, b) => b.amount - a.amount).slice(0, q.limit || 500).map(x => Object.assign({ pos: null }, x));
        return { count: ev.length, amount: ev.reduce((s, x) => s + x.amount, 0), rows };
      },
    };
    return calCache;
  } });

  // ---------- вспомогательное ----------
  M.sum = (arr, f) => arr.reduce((s, x) => s + (typeof f === 'function' ? f(x) : x[f]), 0);
  M.kpis = function (list) {
    list = list || positions;
    const S = f => M.sum(list, f);
    const k = {
      dv01: S('dv01'), cs01: S('cs01'), basis01: S('basis01'),
      mvBonds: S(p => p.group === 'DER' ? 0 : p.mv),
      pnl: S(p => p.pnl.total), carryDay: S('carryDay'), fundingDay: S('fundingDay'),
      pnlMtd: S('pnlMtd'), pnlYtd: S('pnlYtd'),
      byCcy: {}, byCurve: {},
    };
    // по ногам (lib/instr.js): DV01 — по валюте кривой, FX-дельта — по своей валюте; MV — в валюте позиции
    const I = window.INSTR;
    M.ccys.forEach(c => k.byCcy[c] = { dv01: S(p => I.dv01In(p, c)), mv: S(p => p.ccy === c && p.group !== 'DER' ? p.mv : 0), fxDelta: S(p => I.fxIn(p, c)) });
    M.curves.forEach(c => k.byCurve[c.id] = S(p => p.dv01ByCurve[c.id]));
    return k;
  };
})();
