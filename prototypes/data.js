/*
 * Тестовые данные FI-деска для прототипов интерфейса.
 * Все цифры вымышленные; модель расчёта рисков упрощённая (key-rate DV01 через
 * линейную раскладку по пилларам, CS01 через спред-дюрацию).
 *
 * Конвенции:
 *  - суммы в RUB, если не указано иное;
 *  - DV01 / CS01 — изменение PV в RUB при сдвиге на +1 б.п. (длинная облигация => отрицательный DV01);
 *  - fxDelta — позиция в единицах валюты (+ = длинная валюта);
 *  - funding.amount < 0 — привлечение, > 0 — размещение.
 *
 * Демо-модель по общей методике docs/ORG_CONVENTIONS.md. Индексы, спреды, фикс. ставки и базы фондирования —
 * синтетические демо-значения, не правила какой-либо организации; настоящие правила задаёт приватный адаптер
 * (справочник funding_rules, план адаптера, фаза A7):
 *  - книги TRADING: каждая облигация фондируется ежедневно на чистую MV в своей валюте
 *    (демо: RUB — КС + спред, CNY — RUSFAR CNY + спред, USD/EUR/CHF — фикс. 6 %), процент платится в RUB;
 *    вечером ОВП книги облигаций передаётся в книгу деривативов (FXB) сделкой T+1;
 *  - книги DERIVS: фондирование по остаткам денежных средств в каждой валюте по O/N-ставке;
 *  - книга HTM: контрактная ставка на чистую цену покупки, PnL — только начисление, вне DV01;
 *  - два PnL: по рыночным курсам (флэш, основной) и по курсам ЦБ (как официальный, приходит на T+1);
 *  - RUB-облигации: риск ставок на RUONIA; ОФЗ — плюс спред ОФЗ к RUONIA (ofzSpr01), корпораты — CS01 к RUONIA.
 *
 * Форма window.DESK совпадает с тем, что отдаёт сервер (/desk-data.js) из базы:
 * см. docs/DATA_CONTRACT.md. Функции DESK.util — в desk-util.js.
 */
(function () {
  'use strict';

  // ---------- детерминированный ГПСЧ ----------
  let seed = 20261013;
  function rnd() { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; }
  function rndn() { return (rnd() + rnd() + rnd() + rnd() - 2) / 0.8165; } // ~N(0,1)

  const AS_OF = '2026-10-13';
  const asOf = new Date(AS_OF + 'T00:00:00');
  function yearsTo(d) { return (new Date(d + 'T00:00:00') - asOf) / (365.25 * 864e5); }
  // дата по местному времени (toISOString сдвинул бы её на день в часовых поясах восточнее UTC)
  const isoDay = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');

  // ---------- рынок ----------
  const BUCKETS = ['1M', '3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];
  const PILLARS = [1 / 12, 0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20];

  const CURVES = {
    'RUB OFZ':     { name: 'ОФЗ (КБД)',           ccy: 'RUB', kind: 'bond',  pts: [14.10, 13.95, 13.80, 13.55, 13.20, 13.05, 13.10, 13.30, 13.50, 13.60, 13.60] },
    'RUB RUONIA':  { name: 'RUONIA OIS',          ccy: 'RUB', kind: 'swap',  pts: [14.30, 14.10, 13.85, 13.40, 12.80, 12.55, 12.50, 12.60, 12.75, 12.85, 12.90] },
    'RUB KEY':     { name: 'Ключевая ставка (КС)', ccy: 'RUB', kind: 'swap',  pts: [14.50, 14.25, 13.95, 13.50, 12.90, 12.65, 12.60, 12.70, 12.85, 12.95, 13.00] },
    'USD SOFR':    { name: 'USD SOFR',            ccy: 'USD', kind: 'swap',  pts: [4.05, 3.98, 3.90, 3.78, 3.62, 3.58, 3.62, 3.70, 3.82, 3.95, 4.02] },
    'CNY':         { name: 'CNY (RUSFAR CNY)',    ccy: 'CNY', kind: 'swap',  pts: [5.40, 5.30, 5.20, 5.05, 4.90, 4.85, 4.95, 5.10, 5.25, 5.35, 5.40] },
    'EUR ESTR':    { name: 'EUR €STR',            ccy: 'EUR', kind: 'swap',  pts: [2.15, 2.12, 2.08, 2.05, 2.10, 2.18, 2.32, 2.45, 2.60, 2.72, 2.78] },
    'CHF SARON':   { name: 'CHF SARON',           ccy: 'CHF', kind: 'swap',  pts: [0.05, 0.06, 0.08, 0.10, 0.18, 0.25, 0.38, 0.48, 0.60, 0.68, 0.72] },
  };
  // изменения за день (б.п.) — для PnL explain
  const CURVE_MOVES = {
    'RUB OFZ':    [14, 13, 12, 11, 9, 8, 6, 5, 4, 4, 3],
    'RUB RUONIA': [6, 7, 8, 8, 7, 6, 5, 4, 3, 3, 3],
    'RUB KEY':    [9, 9, 9, 8, 7, 6, 5, 4, 3, 3, 3],
    'USD SOFR':   [0, -1, -1, -2, -3, -3, -3, -2, -2, -2, -2],
    'CNY':        [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    'EUR ESTR':   [0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2],
    'CHF SARON':  [0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1],
  };
  // Курсы: FX — рыночные (внешние котировки на ~19:00), основной флэш-PnL;
  // FX_CBR — курс ЦБ, действующий на дату среза (установлен накануне); официальный PnL считается по нему;
  // FX_CBR_NEXT — курс ЦБ, установленный сегодня на завтра: по нему вечером передаётся ОВП книг облигаций.
  const FX = { RUB: 1, USD: 81.45, EUR: 94.62, CNY: 11.38, CHF: 101.80 };
  const FX_MOVE_PCT = { RUB: 0, USD: 0.62, EUR: 0.21, CNY: 0.44, CHF: 0.35 };
  const FX_CBR = { RUB: 1, USD: 81.12, EUR: 94.38, CNY: 11.33, CHF: 101.42 };
  const FX_CBR_MOVE_PCT = { RUB: 0, USD: 0.48, EUR: 0.30, CNY: 0.36, CHF: 0.24 };
  const FX_CBR_NEXT = { RUB: 1, USD: 81.47, EUR: 94.60, CNY: 11.39, CHF: 101.76 };
  const INDICES = { 'КС': 14.50, 'RUONIA': 14.32, 'RUSFAR O/N': 14.28, 'SOFR': 4.08, 'RUSFAR CNY': 5.45, '€STR': 2.15, 'SARON': 0.05 };
  // начисление за день: календарные дни с прошлого рабочего дня (в понедельник — 3)
  const PREV_DATE = '2026-10-12', ACCRUAL_DAYS = 1, NEXT_DATE = '2026-10-14';
  // jump-to-default: recovery — параметр (по умолчанию 0)
  const JTD_RECOVERY = 0;

  function curveRate(curve, t) {
    const pts = CURVES[curve].pts;
    if (t <= PILLARS[0]) return pts[0];
    for (let i = 1; i < PILLARS.length; i++) {
      if (t <= PILLARS[i]) {
        const w = (t - PILLARS[i - 1]) / (PILLARS[i] - PILLARS[i - 1]);
        return pts[i - 1] * (1 - w) + pts[i] * w;
      }
    }
    return pts[pts.length - 1];
  }

  // раскладка DV01 по пилларам: 80% — интерполяция на срок погашения, 20% — купонный поток
  function allocate(T, total, couponShare) {
    const v = new Array(BUCKETS.length).fill(0);
    const cs = couponShare == null ? 0.2 : couponShare;
    const main = total * (1 - cs);
    let placed = false;
    if (T <= PILLARS[0]) { v[0] += main; placed = true; }
    for (let i = 1; i < PILLARS.length && !placed; i++) {
      if (T <= PILLARS[i]) {
        const w = (T - PILLARS[i - 1]) / (PILLARS[i] - PILLARS[i - 1]);
        v[i - 1] += main * (1 - w); v[i] += main * w; placed = true;
      }
    }
    if (!placed) v[v.length - 1] += main;
    const idx = PILLARS.map((p, i) => (p < T ? i : -1)).filter(i => i >= 0);
    if (idx.length && cs > 0) {
      const wsum = idx.reduce((s, i) => s + PILLARS[i], 0);
      idx.forEach(i => { v[i] += total * cs * PILLARS[i] / wsum; });
    } else { v[Math.max(0, v.findIndex(x => x !== 0))] += total * cs; }
    return v;
  }

  // доля купонного периода, прошедшая с последней выплаты (график от даты погашения назад)
  function accruedFrac(T, freq) {
    const n = Math.max(1, Math.ceil(T * freq - 1e-9));
    return Math.max(0, 1 / freq - (T - (n - 1) / freq));
  }
  function bondMath(T, cpn, freq, y) {
    const n = Math.max(1, Math.ceil(T * freq - 1e-9));
    const first = T - (n - 1) / freq; // время до первого купона
    function pv(yy) {
      let p = 0;
      for (let k = 0; k < n; k++) {
        const t = first + k / freq;
        const df = Math.pow(1 + yy / freq, -t * freq);
        p += (cpn / freq) * df;
        if (k === n - 1) p += 100 * df;
      }
      return p;
    }
    const p = pv(y), up = pv(y + 1e-4), dn = pv(y - 1e-4);
    return { price: p, mdur: (dn - up) / (2e-4 * p), convexity: (up + dn - 2 * p) / (1e-8 * p) };
  }

  // ---------- справочник эмитентов ----------
  const ISSUERS = {
    'Минфин РФ':       { group: 'Российская Федерация', sector: 'Суверен',       rating: 'AAA', gov: true },
    'РЖД':             { group: 'РЖД',                  sector: 'Транспорт',     rating: 'AAA', quasi: true },
    'Газпром':         { group: 'Газпром',              sector: 'Нефть и газ',   rating: 'AAA', quasi: true },
    'Газпром нефть':   { group: 'Газпром',              sector: 'Нефть и газ',   rating: 'AAA', quasi: true },
    'Сбербанк':        { group: 'Сбербанк',             sector: 'Банки',         rating: 'AAA', quasi: true },
    'ВТБ':             { group: 'ВТБ',                  sector: 'Банки',         rating: 'AAA', quasi: true },
    'Роснефть':        { group: 'Роснефть',             sector: 'Нефть и газ',   rating: 'AAA', quasi: true },
    'МТС':             { group: 'АФК Система',          sector: 'Телеком',       rating: 'AAA' },
    'Ростелеком':      { group: 'Ростелеком',           sector: 'Телеком',       rating: 'AA+', quasi: true },
    'Магнит':          { group: 'Магнит',               sector: 'Ритейл',        rating: 'AAA' },
    'X5':              { group: 'X5',                   sector: 'Ритейл',        rating: 'AAA' },
    'Норникель':       { group: 'Норникель',            sector: 'Металлургия',   rating: 'AAA' },
    'Сибур':           { group: 'Сибур',                sector: 'Химия',         rating: 'AAA' },
    'Самолёт':         { group: 'Самолёт',              sector: 'Девелопмент',   rating: 'A' },
    'Сегежа':          { group: 'АФК Система',          sector: 'Лесная пром.',  rating: 'BB' },
    'ГТЛК':            { group: 'ГТЛК',                 sector: 'Лизинг',        rating: 'AA-', quasi: true },
    'Атомэнергопром':  { group: 'Росатом',              sector: 'Энергетика',    rating: 'AAA', quasi: true },
    'Россети':         { group: 'Россети',              sector: 'Энергетика',    rating: 'AAA', quasi: true },
    'Альфа-Банк':      { group: 'Альфа-Банк',           sector: 'Банки',         rating: 'AA+' },
    'Совкомбанк':      { group: 'Совкомбанк',           sector: 'Банки',         rating: 'AA' },
    'РусГидро':        { group: 'РусГидро',             sector: 'Энергетика',    rating: 'AAA', quasi: true },
    'Лукойл':          { group: 'Лукойл',               sector: 'Нефть и газ',   rating: 'AAA' },
    'Совкомфлот':      { group: 'Совкомфлот',           sector: 'Транспорт',     rating: 'AA', quasi: true },
    'Металлоинвест':   { group: 'УСМ',                  sector: 'Металлургия',   rating: 'AA+' },
    'Полюс':           { group: 'Полюс',                sector: 'Металлургия',   rating: 'AA+' },
    'Русал':           { group: 'Русал',                sector: 'Металлургия',   rating: 'A+' },
    'Новатэк':         { group: 'Новатэк',              sector: 'Нефть и газ',   rating: 'AAA' },
    'ДОМ.РФ':          { group: 'ДОМ.РФ',               sector: 'Ипотека',       rating: 'AAA', quasi: true },
    'Европлан':        { group: 'Европлан',             sector: 'Лизинг',        rating: 'AA' },
    'ВТБ Лизинг':      { group: 'ВТБ',                  sector: 'Лизинг',        rating: 'AA' },
  };
  const RATING_SPREAD = { 'AAA': 90, 'AA+': 140, 'AA': 190, 'AA-': 250, 'A+': 320, 'A': 420, 'BB': 900 };
  const RATING_ORDER = ['AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'BB'];

  // спред-шоки за день по эмитентам (б.п.), прочим — общий
  const SPREAD_MOVES = { _default: 2, 'Самолёт': 24, 'Сегежа': 38, 'ГТЛК': 6, 'Минфин РФ': 0, 'ДОМ.РФ': 3 };
  const OFZ_SPREAD_MOVE = 2;   // изменение спреда ОФЗ к RUONIA за день, б.п.

  // ---------- книги ----------
  // bookType — режим фондирования: TRADING (облигации, ежедневно на MV), HTM (контрактная ставка), DERIVS (остатки ДС).
  // ocpTo — книга деривативов, в которую вечером передаётся ОВП книги облигаций.
  const E1 = 'Головной банк', E2 = 'Дочерний банк';
  const BOOKS = {
    OFZ:      { name: 'ОФЗ',               desk: 'Rates',  trader: 'А. Иванов',  bookType: 'TRADING', entity: E1 },
    RATES:    { name: 'Свопы RUB',         desk: 'Rates',  trader: 'А. Иванов',  bookType: 'DERIVS',  entity: E1 },
    CORP_RUB: { name: 'Корпы RUB',         desk: 'Credit', trader: 'Е. Петрова', bookType: 'TRADING', entity: E1 },
    CREDIT:   { name: 'CDS',               desk: 'Credit', trader: 'Е. Петрова', bookType: 'DERIVS',  entity: E1 },
    CORP_FX:  { name: 'Корпы валютные',    desk: 'FX Credit', trader: 'Д. Сидоров', bookType: 'TRADING', entity: E1, ocpTo: 'FXB' },
    SOV_FX:   { name: 'Суверен валютный',  desk: 'FX Credit', trader: 'Д. Сидоров', bookType: 'TRADING', entity: E1, ocpTo: 'FXB' },
    SECUR:    { name: 'Секьюритизация',    desk: 'Credit', trader: 'М. Козлова', bookType: 'TRADING', entity: E2 },
    FXB:      { name: 'FX',                desk: 'FX',     trader: 'И. Морозов', bookType: 'DERIVS',  entity: E1 },
    HTM:      { name: 'Инвест. портфель (HTM)', desk: 'Rates', trader: 'А. Иванов', bookType: 'HTM', entity: E2 },
  };

  // кривая риска ставок по валюте: RUB — RUONIA (ОФЗ = RUONIA + спред ОФЗ, корпораты — CS01 к RUONIA)
  const CCY_CURVE = { RUB: 'RUB RUONIA', USD: 'USD SOFR', CNY: 'CNY', EUR: 'EUR ESTR', CHF: 'CHF SARON' };

  const positions = [];
  let nextId = 1;

  // ---------- облигации ----------
  // [book, assetClass, name, issuer, ccy, maturity, coupon/margin, freq, notional(ccy), opts]
  function bond(book, assetClass, name, issuer, ccy, maturity, cpn, freq, notional, opts) {
    opts = opts || {};
    const T = Math.max(0.05, yearsTo(maturity));
    // оферта (put): доходность, спред и риск считаются к дате оферты
    const Ty = opts.offer && yearsTo(opts.offer) > 0 ? Math.max(0.05, yearsTo(opts.offer)) : T;
    const iss = ISSUERS[issuer];
    const isFloat = !!opts.index;
    const curve = isFloat ? (opts.index === 'КС' ? 'RUB KEY' : 'RUB RUONIA') : CCY_CURVE[ccy];
    let spreadBp = opts.spread != null ? opts.spread : (iss.gov && ccy === 'RUB' ? 0 : RATING_SPREAD[iss.rating] * (iss.quasi ? 0.85 : 1));
    if (ccy !== 'RUB' && opts.spread == null) spreadBp = (iss.gov ? 180 : 260 + RATING_SPREAD[iss.rating] * 0.6);
    spreadBp += Math.round(rndn() * 12);
    let price, mdur, ytm, rateDur, sprDur, convexity = 0;
    if (isFloat) {
      const dm = spreadBp; // discount margin
      sprDur = opts.wal ? opts.wal * 0.82 : T * 0.93;
      price = 100 + (cpn * 100 - dm) * sprDur / 100 + rndn() * 0.15;
      rateDur = opts.index === 'КС' ? 0.08 : 0.2;
      mdur = rateDur;
      ytm = INDICES[opts.index] + dm / 100;
    } else {
      // рублёвая доходность строится от КБД (спред к ОФЗ), а спред показывается к RUONIA — кривой риска
      const base = curveRate(ccy === 'RUB' ? 'RUB OFZ' : curve, opts.wal || Ty);
      ytm = base + spreadBp / 100;
      if (ccy === 'RUB') spreadBp = Math.round((ytm - curveRate(curve, opts.wal || Ty)) * 100 * 10) / 10;
      const effT = opts.wal ? opts.wal * 1.15 : Ty;
      const m = bondMath(effT, cpn, freq, ytm / 100);
      price = m.price; mdur = opts.wal ? m.mdur * 0.9 : m.mdur; convexity = m.convexity;
      rateDur = mdur; sprDur = mdur;
    }
    const mv = notional * price / 100;
    const mvRub = mv * FX[ccy];
    const dv01Total = -mvRub * rateDur * 1e-4;
    // ОФЗ: спред к RUONIA — отдельный фактор «спред ОФЗ» (одно число на бумагу), кредитного CS01 нет;
    // корпораты RUB — CS01 к RUONIA; валютные бумаги — CS01 к кривой своей валюты
    const ofz = iss.gov && ccy === 'RUB';
    const spr01 = -mvRub * sprDur * 1e-4;
    const cs01 = ofz ? 0 : spr01, ofzSpr01 = ofz ? spr01 : 0;
    const bucketT = isFloat ? (opts.index === 'КС' ? 1 / 12 : 0.25) : (opts.wal || Ty);
    const risk = {}; risk[curve] = allocate(bucketT, dv01Total, isFloat ? 0 : 0.2);
    const fxDelta = {}; if (ccy !== 'RUB') fxDelta[ccy] = mv;
    const segment = iss.gov ? 'Гос' : (assetClass === 'Секьюритизация' ? 'Секьюритизация' : 'Корп');
    positions.push({
      id: nextId++, book, assetClass, segment, instrType: 'BOND', name, isin: opts.isin || fakeIsin(),
      issuer, issuerGroup: iss.group, sector: iss.sector, rating: iss.rating, ccy, maturity, tenorY: T,
      couponType: isFloat ? 'float' : 'fix', coupon: isFloat ? null : cpn, index: opts.index || null, margin: isFloat ? cpn : null,
      notional, price, mv, mvRub, ytm, spreadBp, mdur, sprDur, convexity,
      // НКД, % номинала; дата, к которой считается доходность (оферта; null = погашение); тип и кривая спреда
      accrued: +((isFloat ? INDICES[opts.index] + cpn : cpn) * accruedFrac(T, freq)).toFixed(4), couponFreq: freq,
      yieldDate: Ty < T ? opts.offer : null, spreadType: isFloat ? 'DM' : 'Z', spreadCurve: curve,
      dv01: dv01Total, cs01, ofzSpr01, risk, fxDelta, jtd: mvRub * (1 - JTD_RECOVERY),
      wal: opts.wal || null, cpr: opts.cpr || null, newToday: !!opts.newToday,
      // фактор 'CS' — собственный спред бумаги: кредитный (CS01) или спред ОФЗ к RUONIA (ofzSpr01)
      gamma: gammaRows(risk, mvRub * convexity * 1e-8, spr01 !== 0, ccy !== 'RUB' ? ccy : null, cs01),
      entity: BOOKS[book].entity,
    });
  }
  // Вторые производные (формат position.gamma, docs/DATA_CONTRACT.md §4.3.3): [[f1, f2, d2PV], ...], f1 <= f2.
  //  - ставки × ставки: трёхдиагональная матрица по пилларам (так выглядит key-rate гамма при линейной
  //    интерполяции кривой), нормированная так, чтобы гамма к параллельному сдвигу = G = MV·C·1e-8 (RUB/б.п.²);
  //  - ставки × спред и спред × спред: доходность = ставка + спред, поэтому те же G·w_i и G;
  //  - курс × ставки/спред: валютная бумага, DV01 и CS01 в рублях масштабируются курсом: DV01_i/100, CS01/100.
  const sig = x => +x.toPrecision(6);
  function gammaRows(risk, G, withCs, fxCcy, cs01) {
    const out = {};
    const put = (a, b, v) => { if (!v) return; const k = a <= b ? [a, b] : [b, a]; const key = k.join('#'); out[key] = [k[0], k[1], sig((out[key] ? out[key][2] : 0) + v)]; };
    const dvTot = Object.values(risk).reduce((s, r) => s + r.reduce((a, b) => a + b, 0), 0);
    Object.keys(risk).forEach(c => {
      const r = risk[c], tot = r.reduce((a, b) => a + b, 0);
      if (!tot || !dvTot) return;
      const w = r.map(x => x / tot), cw = tot / dvTot, f = i => `IR|${c}|${BUCKETS[i]}`;
      if (G) {
        const T = []; let sw = 0;
        for (let i = 0; i < w.length; i++) for (let j = i; j <= Math.min(i + 1, w.length - 1); j++) {
          if (w[i] && w[j]) { const v = w[i] * w[j]; T.push([i, j, v]); sw += (i === j ? 1 : 2) * v; }
        }
        T.forEach(([i, j, v]) => put(f(i), f(j), G * cw * cw * v / sw));
        if (withCs) w.forEach((x, i) => put('CS', f(i), G * cw * x));
      }
      if (fxCcy) r.forEach((x, i) => put(`FX|${fxCcy}`, f(i), x / 100));
    });
    if (G && withCs) put('CS', 'CS', G);
    if (fxCcy && cs01) put('CS', `FX|${fxCcy}`, cs01 / 100);
    return Object.values(out).sort((a, b) => a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0);
  }
  function fakeIsin() { let s = 'RU000A'; const a = '0123456789ABCDEFGHJKLMNPRSTUVWXYZ'; for (let i = 0; i < 6; i++) s += a[Math.floor(rnd() * a.length)]; return s; }

  const bn = 1e9, mm = 1e6;
  // ОФЗ-ПД
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26242', 'Минфин РФ', 'RUB', '2029-08-29', 9.00, 2, 4.2 * bn, { isin: 'SU26242RMFS0' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26239', 'Минфин РФ', 'RUB', '2031-07-23', 6.90, 2, 2.6 * bn, { isin: 'SU26239RMFS6' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26241', 'Минфин РФ', 'RUB', '2032-11-17', 9.50, 2, 3.1 * bn, { isin: 'SU26241RMFS2' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26244', 'Минфин РФ', 'RUB', '2034-03-15', 11.25, 2, 5.4 * bn, { isin: 'SU26244RMFS6' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26245', 'Минфин РФ', 'RUB', '2035-09-26', 12.00, 2, 3.8 * bn, { isin: 'SU26245RMFS3' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26246', 'Минфин РФ', 'RUB', '2036-03-12', 12.00, 2, -1.9 * bn, { isin: 'SU26246RMFS1' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26243', 'Минфин РФ', 'RUB', '2038-05-19', 9.80, 2, 2.2 * bn, { isin: 'SU26243RMFS8' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26247', 'Минфин РФ', 'RUB', '2039-05-11', 12.25, 2, 1.5 * bn, { isin: 'SU26247RMFS5', newToday: true });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26248', 'Минфин РФ', 'RUB', '2040-05-16', 12.25, 2, -2.4 * bn, { isin: 'SU26248RMFS3' });
  bond('OFZ', 'ОФЗ-ПД', 'ОФЗ 26238', 'Минфин РФ', 'RUB', '2041-05-15', 7.10, 2, 1.8 * bn, { isin: 'SU26238RMFS4' });
  // ОФЗ-ПК
  bond('OFZ', 'ОФЗ-ПК', 'ОФЗ 29010', 'Минфин РФ', 'RUB', '2034-12-06', 0.0, 4, 3.0 * bn, { index: 'RUONIA', spread: 45, isin: 'SU29010RMFS4' });
  bond('OFZ', 'ОФЗ-ПК', 'ОФЗ 29022', 'Минфин РФ', 'RUB', '2032-10-06', 0.0, 4, 4.5 * bn, { index: 'RUONIA', spread: 38, isin: 'SU29022RMFS0' });
  bond('OFZ', 'ОФЗ-ПК', 'ОФЗ 29024', 'Минфин РФ', 'RUB', '2034-04-12', 0.0, 4, 2.0 * bn, { index: 'RUONIA', spread: 52, isin: 'SU29024RMFS6' });
  bond('OFZ', 'ОФЗ-ПК', 'ОФЗ 29025', 'Минфин РФ', 'RUB', '2037-08-12', 0.0, 4, 1.2 * bn, { index: 'RUONIA', spread: 60, isin: 'SU29025RMFS3' });
  // Корпы RUB фикс
  const cf = (n, i, m, c, f, nt, o) => bond('CORP_RUB', 'Корп RUB фикс', n, i, 'RUB', m, c, f, nt, o);
  cf('РЖД 001Р-28R', 'РЖД', '2029-04-17', 12.50, 2, 1.6 * bn);
  cf('Газпром Капитал БО-07', 'Газпром', '2030-02-11', 13.10, 2, 1.2 * bn);
  cf('Сбербанк 001Р-SBER44', 'Сбербанк', '2028-06-02', 14.20, 2, 0.9 * bn);
  cf('Роснефть 002Р-13', 'Роснефть', '2031-09-30', 12.80, 2, 1.1 * bn);
  cf('МТС 002Р-08', 'МТС', '2029-11-20', 14.90, 4, 0.75 * bn, { offer: '2027-11-24' });
  cf('Ростелеком 002Р-17R', 'Ростелеком', '2028-03-14', 15.20, 4, 0.6 * bn);
  cf('Магнит БО-004Р-05', 'Магнит', '2028-09-08', 14.40, 4, 0.8 * bn);
  cf('X5 Финанс 003Р-04', 'X5', '2029-05-25', 14.10, 4, 0.7 * bn, { newToday: true });
  cf('Норникель БО-001Р-09', 'Норникель', '2030-07-19', 13.60, 2, 0.95 * bn);
  cf('Сибур Холдинг ПБО-10', 'Сибур', '2029-10-03', 13.90, 2, 0.5 * bn, { offer: '2027-10-07' });
  cf('Самолёт БО-П15', 'Самолёт', '2027-12-15', 17.50, 12, 0.42 * bn);
  cf('Сегежа 003Р-02R', 'Сегежа', '2027-06-20', 19.50, 12, 0.28 * bn);
  cf('ГТЛК 002Р-04', 'ГТЛК', '2031-02-27', 15.40, 12, 0.65 * bn, { offer: '2028-03-02' });
  cf('Атомэнергопром 001Р-07', 'Атомэнергопром', '2032-04-09', 12.90, 2, 0.85 * bn);
  cf('Россети ФСК 002Р-03', 'Россети', '2030-08-21', 13.30, 2, -0.4 * bn);
  // Корпы RUB флоатеры (КС+)
  const cfl = (n, i, m, mg, nt, o) => bond('CORP_RUB', 'Корп RUB флоатер', n, i, 'RUB', m, mg, 12, nt, Object.assign({ index: 'КС' }, o || {}));
  cfl('РЖД 001Р-32R', 'РЖД', '2029-09-14', 1.10, 1.4 * bn, { spread: 105 });
  cfl('Газпром нефть 003Р-12R', 'Газпром нефть', '2028-11-03', 1.30, 1.0 * bn, { spread: 118 });
  cfl('Сбербанк 001Р-SBER51', 'Сбербанк', '2029-02-27', 1.00, 1.8 * bn, { spread: 96 });
  cfl('ВТБ Б-1-312', 'ВТБ', '2028-05-18', 1.20, 1.3 * bn, { spread: 122 });
  cfl('Альфа-Банк 002Р-45', 'Альфа-Банк', '2029-07-11', 1.50, 0.7 * bn, { spread: 156 });
  cfl('ГТЛК 002Р-09', 'ГТЛК', '2030-10-22', 2.00, 0.5 * bn, { spread: 214 });
  cfl('Совкомбанк БО-07', 'Совкомбанк', '2028-08-30', 1.90, 0.45 * bn, { spread: 196 });
  cfl('РусГидро БО-П12', 'РусГидро', '2029-12-04', 1.10, 0.6 * bn, { spread: 110 });
  // Корпы валютные (замещающие и локальные)
  const cx = (n, i, c, m, cpn, f, nt, o) => bond('CORP_FX', 'Корп валютные', n, i, c, m, cpn, f, nt, o);
  cx('Газпром ЗО27-1-Д', 'Газпром', 'USD', '2027-04-27', 4.95, 2, 18 * mm);
  cx('Газпром ЗО28-1-Д', 'Газпром', 'USD', '2028-11-29', 4.95, 2, 12 * mm);
  cx('Газпром ЗО27-1-Е', 'Газпром', 'EUR', '2027-11-04', 2.50, 1, 9 * mm);
  cx('Лукойл ЗО30-Д', 'Лукойл', 'USD', '2030-04-26', 3.88, 2, 14 * mm);
  cx('Совкомфлот ЗО28-Д', 'Совкомфлот', 'USD', '2028-06-16', 5.38, 2, 6 * mm);
  cx('Металлоинвест ЗО28-Д', 'Металлоинвест', 'USD', '2028-10-22', 3.38, 2, 5 * mm);
  cx('Норникель БО-001Р-08-CNY', 'Норникель', 'CNY', '2028-03-21', 7.20, 4, 220 * mm);
  cx('Полюс ПБО-02-CNY', 'Полюс', 'CNY', '2029-06-14', 7.55, 4, 160 * mm);
  cx('Русал БО-001Р-07-CNY', 'Русал', 'CNY', '2027-09-30', 8.25, 4, 140 * mm);
  cx('Металлоинвест 001Р-06-CNY', 'Металлоинвест', 'CNY', '2028-12-12', 7.40, 4, 110 * mm, { newToday: true });
  cx('Газпром Капитал БО-003Р-02-CNY', 'Газпром', 'CNY', '2029-02-07', 6.95, 4, 250 * mm);
  cx('Роснефть 002Р-14-CNY', 'Роснефть', 'CNY', '2030-05-17', 7.10, 4, 180 * mm);
  cx('Новатэк 001Р-03-CNY', 'Новатэк', 'CNY', '2028-08-01', 6.80, 4, 130 * mm);
  cx('Газпром ЗО27-1-Ш', 'Газпром', 'CHF', '2027-06-15', 1.54, 1, 6 * mm);
  // Суверен валютный: ОФЗ в CNY и РФ ЗО
  const sx = (n, c, m, cpn, f, nt, o) => bond('SOV_FX', 'Суверен валютный', n, 'Минфин РФ', c, m, cpn, f, nt, o);
  sx('ОФЗ-CNY 2027', 'CNY', '2027-12-10', 6.00, 2, 350 * mm);
  sx('ОФЗ-CNY 2030', 'CNY', '2030-12-11', 7.00, 2, 420 * mm);
  sx('РФ ЗО 2030-Д', 'USD', '2030-03-31', 7.50, 2, 22 * mm);
  sx('РФ ЗО 2035-Д', 'USD', '2035-06-24', 5.10, 2, 15 * mm);
  sx('РФ ЗО 2043-Д', 'USD', '2043-04-04', 5.88, 2, 8 * mm);
  sx('РФ ЗО 2047-Д', 'USD', '2047-06-23', 5.25, 2, 10 * mm);
  sx('РФ ЗО 2027-Е', 'EUR', '2027-05-27', 2.88, 1, 7 * mm);
  // Секьюритизация
  const sc = (n, i, m, cpn, f, nt, o) => bond('SECUR', 'Секьюритизация', n, i, 'RUB', m, cpn, f, nt, o);
  sc('ИЦБ ДОМ.РФ 2Р-12R', 'ДОМ.РФ', '2045-06-28', 11.40, 4, 2.4 * bn, { wal: 3.4, cpr: 12.5, spread: 140 });
  sc('ИЦБ ДОМ.РФ 2Р-21R', 'ДОМ.РФ', '2047-11-28', 10.20, 4, 1.6 * bn, { wal: 4.1, cpr: 10.8, spread: 155 });
  sc('ИЦБ ДОМ.РФ 2Р-35R', 'ДОМ.РФ', '2050-03-28', 1.30, 12, 1.9 * bn, { index: 'КС', wal: 3.0, cpr: 14.0, spread: 135 });
  sc('Сбер ИЦБ 2024-1', 'Сбербанк', '2049-08-28', 1.20, 12, 1.1 * bn, { index: 'КС', wal: 2.6, cpr: 15.2, spread: 150 });
  sc('Европлан Авто ABS 2025-1', 'Европлан', '2030-04-28', 15.60, 12, 0.55 * bn, { wal: 1.4, cpr: 22.0, spread: 230 });
  sc('ВТБ Лизинг Авто ABS 2026-1', 'ВТБ Лизинг', '2031-01-28', 15.10, 12, 0.48 * bn, { wal: 1.7, cpr: 20.0, spread: 215 });
  // HTM: учёт по начислению. Рыночные атрибуты (цена, доходность) — для справки; рыночных рисков нет (вне DV01/CS01),
  // фондирование — контрактная ставка, зафиксированная при покупке, на чистую цену покупки.
  function htm(name, issuer, maturity, cpn, freq, notional, purchaseDate, purchasePrice, purchaseYield, contractRate, opts) {
    bond('HTM', 'HTM', name, issuer, 'RUB', maturity, cpn, freq, notional, opts);
    const p = positions[positions.length - 1];
    Object.assign(p, { dv01: 0, cs01: 0, ofzSpr01: 0, risk: {}, gamma: [], convexity: 0, accrualOnly: true,
      purchaseDate, purchasePrice, purchaseYield, contractRate });
  }
  htm('ОФЗ 26240', 'Минфин РФ', '2036-07-30', 7.00, 2, 3.0 * bn, '2024-03-12', 74.20, 12.10, 11.85, { isin: 'SU26240RMFS0' });
  htm('РЖД 001Р-19R', 'РЖД', '2030-11-05', 8.10, 2, 1.2 * bn, '2023-11-20', 86.50, 11.70, 11.40);

  // ---------- IRS ----------
  function annuity(curve, T) { let a = 0; for (let t = 0.5; t <= T + 1e-9; t += 0.5) a += 0.5 * Math.pow(1 + curveRate(curve, t) / 100, -t); return a; }
  function irs(book, curve, tenorY, side, notional, fixed, opts) {
    opts = opts || {};
    const ccy = CURVES[curve].ccy;
    const T = tenorY - (opts.agedY || 0);
    const par = curveRate(curve, T);
    const A = annuity(curve, T);
    const sgn = side === 'PAY' ? 1 : -1; // pay fixed => выигрывает от роста ставок
    const dv01Ccy = sgn * notional * A * 1e-4;
    const dv01 = dv01Ccy * FX[ccy];
    const mvCcy = sgn * notional * (par - fixed) / 100 * A;
    const risk = {}; risk[curve] = allocate(T, dv01, 0.25);
    const maturity = new Date(asOf.getTime() + T * 365.25 * 864e5).toISOString().slice(0, 10);
    const idxName = curve === 'RUB KEY' ? 'КС' : curve === 'RUB RUONIA' ? 'RUONIA' : curve === 'USD SOFR' ? 'SOFR' : 'RUSFAR CNY';
    positions.push({
      id: nextId++, book, assetClass: 'IRS', segment: 'Деривативы', instrType: 'IRS',
      name: `IRS ${idxName} ${tenorY}Y ${side === 'PAY' ? 'pay' : 'rec'} ${fixed.toFixed(2)}%`,
      issuer: null, issuerGroup: null, sector: null, rating: null, ccy, maturity, tenorY: T,
      couponType: 'fix', coupon: fixed, index: idxName, side, notional, price: null,
      mv: mvCcy, mvRub: mvCcy * FX[ccy], ytm: par, spreadBp: null, mdur: null,
      dv01, cs01: 0, risk, fxDelta: {}, jtd: 0, counterparty: opts.cpty || 'НКЦ (клиринг)', newToday: !!opts.newToday,
      // pay-fixed = короткая фиксированная облигация → отрицательная выпуклость
      gamma: gammaRows(risk, -sgn * notional * FX[ccy] * bondMath(T, fixed, 2, par / 100).convexity * 1e-8, false, ccy !== 'RUB' ? ccy : null, 0),
    });
  }
  irs('RATES', 'RUB RUONIA', 2, 'PAY', 6.0 * bn, 12.55, { agedY: 0.3 });
  irs('RATES', 'RUB RUONIA', 3, 'PAY', 4.0 * bn, 12.40);
  irs('RATES', 'RUB RUONIA', 5, 'PAY', 3.5 * bn, 12.70, { agedY: 0.6 });
  irs('RATES', 'RUB RUONIA', 7, 'REC', 1.5 * bn, 12.95, { agedY: 0.2 });
  irs('RATES', 'RUB RUONIA', 10, 'PAY', 2.2 * bn, 12.60, { newToday: true });
  irs('RATES', 'RUB KEY', 1, 'REC', 12.0 * bn, 13.80, { agedY: 0.1 });
  irs('RATES', 'RUB KEY', 2, 'PAY', 5.0 * bn, 12.70);
  irs('RATES', 'RUB KEY', 3, 'PAY', 3.0 * bn, 12.95, { agedY: 0.4 });
  irs('RATES', 'RUB KEY', 5, 'PAY', 2.0 * bn, 12.45);
  irs('RATES', 'CNY', 3, 'PAY', 900 * mm, 4.70, { cpty: 'Банк-контрагент А' });

  // ---------- XCCY ----------
  function xccy(book, ccy, tenorY, side, notionalFx, rubFixed, opts) {
    opts = opts || {};
    const fcurve = CCY_CURVE[ccy];
    const T = tenorY - (opts.agedY || 0);
    const A_f = annuity(fcurve, T), A_r = annuity('RUB RUONIA', T);
    // side 'RECFX' = получаем валюту (финальный обмен: +FX, −RUB)
    const s = side === 'RECFX' ? 1 : -1;
    const notionalRub = notionalFx * (opts.initFx || FX[ccy] * 0.97);
    const dvF = -s * notionalFx * (A_f + Math.pow(1 + curveRate(fcurve, T) / 100, -T) * T * 0.0) * 1e-4 * FX[ccy];
    const dvR = s * notionalRub * A_r * 1e-4;
    const risk = {}; risk[fcurve] = allocate(T, dvF * 1.0, 0.1); risk['RUB RUONIA'] = allocate(T, dvR, 0.1);
    const fxDelta = {}; fxDelta[ccy] = s * notionalFx;
    const mvRub = s * (notionalFx * FX[ccy] - notionalRub) * 0.35;
    const maturity = new Date(asOf.getTime() + T * 365.25 * 864e5).toISOString().slice(0, 10);
    positions.push({
      id: nextId++, book, assetClass: 'XCCY', segment: 'Деривативы', instrType: 'XCCY',
      name: `XCCY ${ccy}/RUB ${tenorY}Y ${side === 'RECFX' ? 'rec ' + ccy : 'pay ' + ccy}`,
      issuer: null, issuerGroup: null, sector: null, rating: null, ccy, maturity, tenorY: T,
      couponType: 'fix', coupon: rubFixed, index: null, side, notional: notionalFx, notionalRub, price: null,
      mv: mvRub / FX[ccy], mvRub, ytm: null, spreadBp: null, mdur: null,
      dv01: dvF + dvR, cs01: 0, risk, fxDelta, jtd: 0, counterparty: opts.cpty || 'Банк-контрагент Б', newToday: !!opts.newToday,
    });
  }
  xccy('RATES', 'CNY', 2, 'PAY', 250 * mm, 13.10, { agedY: 0.5 });
  xccy('RATES', 'CNY', 3, 'RECFX', 150 * mm, 12.85);
  xccy('RATES', 'USD', 3, 'PAY', 25 * mm, 12.40, { agedY: 0.8, initFx: 92.1 });
  xccy('RATES', 'USD', 5, 'RECFX', 8 * mm, 12.30, { agedY: 0.2, cpty: 'Банк-контрагент В' });

  // ---------- CDS ----------
  function cds(book, ref, tenorY, side, notional, sprBp, opts) {
    opts = opts || {};
    const T = tenorY - (opts.agedY || 0);
    const A = annuity('RUB OFZ', T);
    const s = side === 'BUY' ? 1 : -1; // покупка защиты => выигрыш при расширении спреда
    const cs01 = s * notional * A * 1e-4;
    const iss = ISSUERS[ref];
    const mktSpr = (RATING_SPREAD[iss.rating] * (iss.gov ? 0.8 : 0.9)) + rndn() * 10;
    const mvRub = s * notional * (mktSpr - sprBp) / 1e4 * A;
    const maturity = new Date(asOf.getTime() + T * 365.25 * 864e5).toISOString().slice(0, 10);
    positions.push({
      id: nextId++, book, assetClass: 'CDS', segment: 'Деривативы', instrType: 'CDS',
      name: `CDS ${ref} ${tenorY}Y ${side === 'BUY' ? 'buy prot' : 'sell prot'}`,
      issuer: ref, issuerGroup: iss.group, sector: iss.sector, rating: iss.rating, ccy: 'RUB', maturity, tenorY: T,
      couponType: 'fix', coupon: sprBp / 100, index: null, side, notional, price: null,
      mv: mvRub, mvRub, ytm: null, spreadBp: mktSpr, mdur: null,
      dv01: 0, cs01, risk: {}, fxDelta: {}, jtd: -s * notional * (1 - JTD_RECOVERY), counterparty: opts.cpty || 'Банк-контрагент А', newToday: !!opts.newToday,
    });
  }
  cds('CREDIT', 'Минфин РФ', 5, 'BUY', 3.0 * bn, 95, { agedY: 0.7 });
  cds('CREDIT', 'Газпром', 5, 'BUY', 2.0 * bn, 110);
  cds('CREDIT', 'РЖД', 3, 'SELL', 1.5 * bn, 95, { agedY: 0.4 });
  cds('CREDIT', 'Сбербанк', 5, 'BUY', 1.0 * bn, 100);
  cds('CREDIT', 'Самолёт', 2, 'BUY', 0.5 * bn, 360, { newToday: true });
  cds('CREDIT', 'ГТЛК', 3, 'SELL', 0.8 * bn, 240, { agedY: 0.3 });

  // ---------- FX ----------
  function fx(book, pair, kind, amountFx, rate, valueDate, opts) {
    opts = opts || {};
    const ccy = pair.slice(0, 3);
    const fxDelta = {}; fxDelta[ccy] = amountFx;
    const T = Math.max(0.003, yearsTo(valueDate));
    const mvRub = amountFx * (FX[ccy] - rate);
    // форвард даёт риск по обеим кривым (точка дифференциала)
    const risk = {};
    if (T > 0.02) {
      risk[CCY_CURVE[ccy]] = allocate(T, -amountFx * FX[ccy] * T * 1e-4, 0);
      risk['RUB RUONIA'] = allocate(T, amountFx * FX[ccy] * T * 1e-4, 0);
    }
    positions.push({
      id: nextId++, book, assetClass: 'FX', segment: 'FX', instrType: 'FX',
      name: `${pair} ${kind} ${valueDate}`, issuer: null, issuerGroup: null, sector: null, rating: null,
      ccy, maturity: valueDate, tenorY: T, couponType: null, coupon: null, index: null, side: amountFx > 0 ? 'BUY' : 'SELL',
      notional: amountFx, price: rate, mv: mvRub / FX[ccy], mvRub, ytm: null, spreadBp: null, mdur: null,
      dv01: Object.values(risk).reduce((s, v) => s + v.reduce((a, b) => a + b, 0), 0), cs01: 0, risk, fxDelta, jtd: 0,
      counterparty: opts.cpty || 'Мосбиржа', newToday: !!opts.newToday,
    });
  }
  fx('FXB', 'USDRUB', 'TOM', 9.5 * mm, 81.10, '2026-10-14');
  fx('FXB', 'CNYRUB', 'TOM', -70 * mm, 11.36, '2026-10-14');
  fx('FXB', 'EURRUB', 'FWD 3M', -4 * mm, 95.80, '2027-01-13', { cpty: 'Банк-контрагент Б' });
  fx('FXB', 'CNYRUB', 'FWD 6M', 260 * mm, 11.70, '2027-04-13', { cpty: 'Банк-контрагент А' });
  fx('FXB', 'USDRUB', 'FWD 1M', -6 * mm, 81.90, '2026-11-13', { newToday: true });

  positions.forEach(p => { if (!p.entity) p.entity = BOOKS[p.book].entity; if (p.ofzSpr01 == null) p.ofzSpr01 = 0; });
  // portfolio — колонка Portfolio отчёта банка (x_portfolio): по режиму книги; длинные ОФЗ — портфель ликвидности,
  // так что в одной книге бывают два портфеля
  const PORTFOLIO = { TRADING: 'Торговый', HTM: 'HTM', DERIVS: 'Деривативы' };
  positions.forEach(p => { p.portfolio = p.book === 'OFZ' && p.tenorY > 10 ? 'Ликвидность' : PORTFOLIO[BOOKS[p.book].bookType]; });
  const cleanMv = p => p.mv - p.notional * (p.accrued || 0) / 100;   // чистая MV в валюте бумаги

  // ---------- ставки фондирования (справочник funding_rates, docs/DATA_REQUIREMENTS.md) ----------
  // Правило подходит, если все заданные поля совпали; побеждает самое конкретное:
  // бумага (16) > книга (8) > юрлицо (4) > деск (2) > режим книги (1). Ставка = индекс + спред или фикс.
  // Все индексы, спреды и фикс. ставки ниже — синтетические демо-значения (docs/ORG_CONVENTIONS.md).
  const FUNDING_RULES = [
    { bookType: 'TRADING', ccy: 'RUB', index: 'КС', spreadBp: 30, note: 'спред деска' },
    { bookType: 'TRADING', ccy: 'CNY', index: 'RUSFAR CNY', spreadBp: 60, note: 'спред деска' },
    { bookType: 'TRADING', ccy: 'USD', fixed: 6.00, note: 'hard ccy' },
    { bookType: 'TRADING', ccy: 'EUR', fixed: 6.00, note: 'hard ccy' },
    { bookType: 'TRADING', ccy: 'CHF', fixed: 6.00, note: 'hard ccy' },
    { book: 'SECUR', ccy: 'RUB', index: 'КС', spreadBp: 50, note: 'особая ставка книги' },
    { entity: E2, instrument: 'ИЦБ ДОМ.РФ 2Р-12R', ccy: 'RUB', index: 'КС', spreadBp: 20, note: 'особый спред юрлица по бумаге' },
    { bookType: 'DERIVS', ccy: 'RUB', index: 'RUONIA', spreadBp: 0 },
    { bookType: 'DERIVS', ccy: 'CNY', index: 'RUSFAR CNY', spreadBp: 0 },
    { bookType: 'DERIVS', ccy: 'USD', index: 'SOFR', spreadBp: 0 },
    { bookType: 'DERIVS', ccy: 'EUR', index: '€STR', spreadBp: 0 },
    { bookType: 'DERIVS', ccy: 'CHF', index: 'SARON', spreadBp: 0 },
  ];
  function fundingRule(book, ccy, p) {
    const b = BOOKS[book]; let best = null, bw = -1;
    FUNDING_RULES.forEach(r => {
      if (r.ccy && r.ccy !== ccy) return;
      if (r.bookType && r.bookType !== b.bookType) return;
      if (r.desk && r.desk !== b.desk) return;
      if (r.book && r.book !== book) return;
      if (r.entity && r.entity !== b.entity) return;
      if (r.instrument && !(p && (p.isin === r.instrument || p.name === r.instrument))) return;
      const w = (r.instrument ? 16 : 0) + (r.book ? 8 : 0) + (r.entity ? 4 : 0) + (r.desk ? 2 : 0) + (r.bookType ? 1 : 0);
      if (w > bw) { best = r; bw = w; }
    });
    return best;
  }
  const pctTxt = x => x.toFixed(2) + '%';
  const ruleExpr = r => r.fixed != null ? pctTxt(r.fixed) + ' фикс' : r.index + (r.spreadBp ? (r.spreadBp > 0 ? ' + ' : ' − ') + pctTxt(Math.abs(r.spreadBp) / 100) : '');
  const ruleRate = r => r.fixed != null ? r.fixed : INDICES[r.index] + r.spreadBp / 100;
  // базы начисления (демо-значения, docs/ORG_CONVENTIONS.md §1.6): процент по облигациям платится в RUB → 365;
  // остатки ДС деривативов — по валюте
  const DAY_BASIS = { RUB: 365, CNY: 365, USD: 360, EUR: 360, CHF: 360 };
  // бенчмарк стоимости фондирования (демо): облигации RUB — КС, CNY — RUSFAR CNY, hard ccy — нет (фикс. ставка); деривативы — O/N своей валюты (RUB — RUONIA)
  const BENCH_POS = { RUB: 'КС', CNY: 'RUSFAR CNY' };

  // ---------- передача ОВП книг облигаций в книгу деривативов (сделка вечером, T+1) ----------
  // ОВП книги облигаций до передачи = Σ FX-дельт бумаг (грязная MV) − фондирование (чистая MV) = НКД + PnL дня в валюте.
  // Передаётся округлённо (0.1 млн), поэтому в книге облигаций остаётся небольшой остаток.
  const OCP_TRANSFERS = [];
  Object.keys(BOOKS).filter(b => BOOKS[b].ocpTo).forEach(b => {
    const ocp = {};
    positions.filter(p => p.book === b && p.instrType === 'BOND').forEach(p => {
      if (p.ccy === 'RUB') return;
      ocp[p.ccy] = (ocp[p.ccy] || 0) + p.fxDelta[p.ccy] - cleanMv(p);
    });
    Object.keys(ocp).forEach(ccy => {
      const amt = Math.round(ocp[ccy] / 1e5) * 1e5;
      if (!amt) return;
      const to = BOOKS[b].ocpTo, rate = FX_CBR_NEXT[ccy];
      [[b, -amt, `Перенос ОВП → ${BOOKS[to].name} (${ccy})`, BOOKS[to].name], [to, amt, `Перенос ОВП ← ${BOOKS[b].name} (${ccy})`, BOOKS[b].name]].forEach(([bk, a, name, cp]) => {
        const fxDelta = {}; fxDelta[ccy] = a;
        positions.push({
          id: nextId++, book: bk, assetClass: 'Перенос ОВП', segment: 'FX', instrType: 'FX', name,
          issuer: null, issuerGroup: null, sector: null, rating: null, ccy, maturity: NEXT_DATE, tenorY: 1 / 365,
          couponType: null, coupon: null, index: null, side: a > 0 ? 'BUY' : 'SELL', notional: a, price: rate,
          mv: 0, mvRub: 0, ytm: null, spreadBp: null, mdur: null, dv01: 0, cs01: 0, ofzSpr01: 0, risk: {}, fxDelta, jtd: 0,
          counterparty: 'Книга «' + cp + '» (внутр.)', newToday: true, ocpTransfer: true, entity: BOOKS[bk].entity,
          portfolio: PORTFOLIO[BOOKS[bk].bookType],
        });
      });
      OCP_TRANSFERS.push({ from: b, to: BOOKS[b].ocpTo, ccy, amount: amt, before: ocp[ccy], rate, valueDate: NEXT_DATE });
    });
  });

  // ---------- фондирование: строки ----------
  // kind: POS — облигация торговой книги (ежедневно на чистую MV, индекс + спред); HTM — контрактная ставка на цену покупки;
  //       CASH — остаток ДС книги деривативов по O/N; REPO — сделка с деском РЕПО против книги (покрытие шорта).
  // Процент по POS/HTM платится в RUB (курс ЦБ) и в ОВП не входит; тело фондирования POS — в валюте бумаги (входит в ОВП).
  const funding = [];
  let fid = 1;
  function fline(o) {
    const T = Math.max(1 / 365, yearsTo(o.maturity));
    const f = Object.assign({ id: fid++, start: AS_OF, tenorY: T, collateral: null, positionId: null }, o);
    f.amountRub = f.amount * FX[f.ccy];
    f.fxDelta = {}; if (f.ccy !== 'RUB' && f.kind !== 'HTM') f.fxDelta[f.ccy] = f.amount;
    funding.push(f);
    return f;
  }
  positions.forEach(p => {
    const bt = BOOKS[p.book].bookType;
    if (p.instrType !== 'BOND' || (bt !== 'TRADING' && bt !== 'HTM')) return;
    if (bt === 'HTM') {
      fline({ kind: 'HTM', type: 'FTP HTM (контракт)', book: p.book, positionId: p.id, ccy: p.ccy,
        amount: -p.notional * p.purchasePrice / 100, index: null, spreadBp: 0, fixed: p.contractRate, rate: p.contractRate,
        rateExpr: pctTxt(p.contractRate) + ' фикс до погашения', benchmark: 'КС', dayBasis: 365, start: p.purchaseDate,
        maturity: p.maturity, counterparty: 'Казначейство' });
      return;
    }
    const r = fundingRule(p.book, p.ccy, p);
    fline({ kind: 'POS', type: 'FTP торговой книги', book: p.book, positionId: p.id, ccy: p.ccy, amount: -cleanMv(p),
      index: r.fixed != null ? null : r.index, spreadBp: r.fixed != null ? 0 : r.spreadBp, fixed: r.fixed != null ? r.fixed : null,
      rate: ruleRate(r), rateExpr: ruleExpr(r), ruleNote: r.note, benchmark: BENCH_POS[p.ccy] || null, dayBasis: 365, maturity: NEXT_DATE, counterparty: 'Казначейство' });
  });
  // остатки ДС книг деривативов (выгрузка из системы): купонные платежи по свопам, премии CDS, ближние ноги FX-свопов, расчёты по FX
  [['RATES', 'RUB', -1.35 * bn], ['RATES', 'CNY', 42 * mm], ['CREDIT', 'RUB', 0.18 * bn],
   ['FXB', 'RUB', 3.0 * bn], ['FXB', 'USD', 5 * mm], ['FXB', 'CNY', -120 * mm], ['FXB', 'EUR', -1.5 * mm]].forEach(([b, ccy, amt]) => {
    const r = fundingRule(b, ccy);
    fline({ kind: 'CASH', type: 'Остаток ДС', book: b, ccy, amount: amt, index: r.index, spreadBp: r.spreadBp, fixed: null,
      rate: ruleRate(r), rateExpr: ruleExpr(r), benchmark: r.index, dayBasis: DAY_BASIS[ccy], maturity: NEXT_DATE, counterparty: 'Казначейство' });
  });
  // РЕПО с деском РЕПО против книги ОФЗ: покрытие шортов 26246/26248 (деньги размещены, бумаги получены).
  // TBD: денежная нога фондируется по ставке РЕПО (кэрри сделки — ставка РЕПО) — принято здесь; альтернатива — по ставке книги.
  fline({ kind: 'REPO', type: 'РЕПО (покрытие шорта)', book: 'OFZ', ccy: 'RUB', amount: 2.1 * bn, index: 'RUSFAR O/N', spreadBp: -10, fixed: null,
    rate: INDICES['RUSFAR O/N'] - 0.10, rateExpr: 'RUSFAR O/N − 0.10%', benchmark: 'КС', dayBasis: 365, start: '2026-10-09', maturity: '2026-10-16',
    counterparty: 'Деск РЕПО', collateral: 'ОФЗ 26246, ОФЗ 26248' });

  // кэрри строки за день, RUB: тело × ставка × дни / база. POS/HTM — процент платится в RUB по курсу ЦБ.
  funding.forEach(f => {
    const conv = f.kind === 'CASH' || f.kind === 'REPO' ? FX[f.ccy] : FX_CBR[f.ccy];
    f.pnl = { carry: f.amount * conv * f.rate / 100 * ACCRUAL_DAYS / f.dayBasis, fx: 0, fxCbr: 0, fxGross: 0, fxAllocated: 0, inPosition: !!f.positionId };
    if (f.ccy !== 'RUB' && f.kind !== 'HTM') {
      f.pnl.fxGross = f.amount * FX[f.ccy] * FX_MOVE_PCT[f.ccy] / 100 / (1 + FX_MOVE_PCT[f.ccy] / 100);
      f.pnl.fxGrossCbr = f.amount * FX_CBR[f.ccy] * FX_CBR_MOVE_PCT[f.ccy] / 100 / (1 + FX_CBR_MOVE_PCT[f.ccy] / 100);
      // переоценка тела фондирования бумаги — в PnL самой бумаги (fxFunding); остатки ДС — на книгу
      if (f.positionId) f.pnl.fxAllocated = f.pnl.fxGross;
      else { f.pnl.fx = f.pnl.fxGross; f.pnl.fxCbr = f.pnl.fxGrossCbr; }
    }
  });
  const fundByPos = {}; funding.forEach(f => { if (f.positionId) fundByPos[f.positionId] = f; });

  // ---------- PnL за день и explain ----------
  // Компоненты: carry (по доходности, валовый), rolldown, rates, spread (кредит и спред ОФЗ), fx (позиция + тело её фондирования),
  // funding (процент по фондированию бумаги), convexity, newTrades, other.
  // Два базиса FX: explained — по рыночным курсам (флэш), explainedCbr — по курсам ЦБ (как официальный PnL).
  // official приходит на T+1 и сравнивается с explainedCbr: unexplained = official − explainedCbr;
  // fxBasis = explainedCbr − explained — разница из-за источника курсов.
  const D_ = ACCRUAL_DAYS;
  const fxMove = (amt, c, cbr) => cbr ? amt * FX_CBR[c] * FX_CBR_MOVE_PCT[c] / 100 / (1 + FX_CBR_MOVE_PCT[c] / 100)
                                      : amt * FX[c] * FX_MOVE_PCT[c] / 100 / (1 + FX_MOVE_PCT[c] / 100);
  positions.forEach(p => {
    const e = { carry: 0, rolldown: 0, rates: 0, spread: 0, fx: 0, funding: 0, convexity: 0, newTrades: 0, other: 0 };
    let fxCbr = 0;
    if (p.accrualOnly) {
      // HTM: начисление по доходности покупки на балансовую (чистую) стоимость покупки, минус контрактное фондирование
      e.carry = p.notional * p.purchasePrice / 100 * p.purchaseYield / 100 / 365 * D_;
    } else if (p.instrType === 'BOND') {
      e.carry = p.mvRub * p.ytm / 100 / 365 * D_; // валовый кэрри по доходности; фондирование — компонента funding
      e.rolldown = p.couponType === 'fix' ? Math.abs(p.dv01) * 0.02 * Math.sign(p.mvRub) : 0;
      const sm = SPREAD_MOVES[p.issuer] != null ? SPREAD_MOVES[p.issuer] : SPREAD_MOVES._default;
      e.spread = p.cs01 * sm + p.ofzSpr01 * OFZ_SPREAD_MOVE;
      e.convexity = 0.5 * p.mvRub * (p.convexity || 0) * Math.pow(8e-4, 2);
    } else if (p.instrType === 'IRS') {
      const fl = p.index === 'КС' ? INDICES['КС'] : p.index === 'RUONIA' ? INDICES.RUONIA : INDICES['RUSFAR CNY'];
      const sgn = p.side === 'PAY' ? 1 : -1;
      e.carry = sgn * p.notional * FX[p.ccy] * (fl - p.coupon) / 100 / 365 * D_;
      e.rolldown = -Math.abs(p.dv01) * 0.015 * sgn;
    } else if (p.instrType === 'XCCY') {
      e.carry = (p.side === 'RECFX' ? -1 : 1) * p.notional * FX[p.ccy] * 0.065 / 365 * D_;
    } else if (p.instrType === 'CDS') {
      const sgn = p.side === 'BUY' ? -1 : 1;
      e.carry = sgn * p.notional * (p.coupon / 100) / 365 * D_;
      const sm = SPREAD_MOVES[p.issuer] != null ? SPREAD_MOVES[p.issuer] : SPREAD_MOVES._default;
      e.spread = p.cs01 * sm;
    }
    Object.keys(p.risk).forEach(c => { p.risk[c].forEach((dv, i) => { e.rates += dv * CURVE_MOVES[c][i]; }); });
    // сделка передачи ОВП заключена вечером по курсу ЦБ на завтра — переоценки за день нет
    if (!p.ocpTransfer) Object.keys(p.fxDelta).forEach(c => { e.fx += fxMove(p.fxDelta[c], c); fxCbr += fxMove(p.fxDelta[c], c, true); });
    const fl = fundByPos[p.id];
    const fxGross = e.fx, fxGrossCbr = fxCbr;
    const fxFunding = fl ? fl.pnl.fxAllocated : 0, fxFundingCbr = fl && fl.pnl.fxGrossCbr ? fl.pnl.fxGrossCbr : 0;
    if (fl) e.funding = fl.pnl.carry;
    e.fx += fxFunding;
    if (p.newToday && !p.ocpTransfer) { e.newTrades = Math.abs(p.mvRub) * (0.0004 + rnd() * 0.0012) * (rnd() < 0.25 ? -1 : 1); }
    const explained = Object.values(e).reduce((a, b) => a + b, 0);
    const fxBasis = (fxGrossCbr + fxFundingCbr) - (fxGross + fxFunding);
    const explainedCbr = explained + fxBasis;
    let unexplained = p.ocpTransfer ? 0 : rndn() * Math.max(40000, Math.abs(explained) * 0.04);
    let breakNote = null;
    if (p.name === 'Сегежа 003Р-02R') { unexplained = -4.6e6; breakNote = 'Расхождение котировки: фронт — оценочная цена, бэк — BVAL Мосбиржи'; }
    if (p.name === 'ИЦБ ДОМ.РФ 2Р-12R') { unexplained = 2.1e6; breakNote = 'Обновлён CPR (12.5% → 13.4%) в бэк-офисной модели, фронт не перезагрузил'; }
    if (p.name.startsWith('XCCY USD/RUB 3Y')) { unexplained = -1.35e6; breakNote = 'Фиксинг SOFR не загружен во фронт-систему'; }
    if (p.name === 'ОФЗ 26247') { unexplained = 0.85e6; breakNote = 'Сделка дня: цена сделки vs закрытие — разные время среза'; }
    if (p.name === 'Газпром ЗО27-1-Д') { unexplained = 0.62e6; breakNote = 'Финансы: спред фондирования по бумаге (особая корректировка) — в нашем расчёте не учтён (TODO)'; }
    p.pnl = Object.assign({}, e, { fxGross, fxFunding, fxGrossCbr, fxFundingCbr, fxCbr: fxGrossCbr + fxFundingCbr,
      explained, explainedCbr, fxBasis, unexplained, official: explainedCbr + unexplained, breakNote });
  });

  // фондирование книги, не привязанное к позициям (остатки ДС деривативов, РЕПО): кэрри и FX-переоценка остатков.
  // Фондирование облигаций уже в PnL самих бумаг (компоненты funding и fx).
  const fundingPnl = { carry: 0, fx: 0, fxCbr: 0 };
  const fundingPnlByBook = {};
  Object.keys(BOOKS).forEach(b => { fundingPnlByBook[b] = { carry: 0, fx: 0, fxCbr: 0, fxAllocated: 0, inPositions: 0 }; });
  funding.forEach(f => {
    const fb = fundingPnlByBook[f.book];
    if (f.pnl.inPosition) { fb.inPositions += f.pnl.carry; fb.fxAllocated += f.pnl.fxAllocated; return; }
    fb.carry += f.pnl.carry; fb.fx += f.pnl.fx; fb.fxCbr += f.pnl.fxCbr;
    fundingPnl.carry += f.pnl.carry; fundingPnl.fx += f.pnl.fx; fundingPnl.fxCbr += f.pnl.fxCbr;
  });

  // ---------- история (22 раб. дня) ----------
  const history = [];
  (function () {
    const d = new Date(asOf); const days = [];
    while (days.length < 22) { if (d.getDay() !== 0 && d.getDay() !== 6) days.unshift(new Date(d)); d.setDate(d.getDate() - 1); }
    const dv0 = positions.reduce((s, p) => s + p.dv01, 0);
    const cs0 = positions.reduce((s, p) => s + p.cs01, 0);
    const offPnl0 = positions.reduce((s, p) => s + p.pnl.official, 0) + fundingPnl.carry + fundingPnl.fxCbr;
    const expPnl0 = positions.reduce((s, p) => s + p.pnl.explained, 0) + fundingPnl.carry + fundingPnl.fx;
    let cum = 0;
    days.forEach((dt, i) => {
      const last = i === days.length - 1;
      const off = last ? offPnl0 : rndn() * 18e6 + 4e6;
      const exp = last ? expPnl0 : off - rndn() * 1.4e6;
      cum += off;
      history.push({
        date: isoDay(dt),
        pnlOfficial: off, pnlExplained: exp, pnlCum: cum,
        dv01: last ? dv0 : dv0 * (1 + 0.18 * Math.sin(i / 3) + rndn() * 0.05),
        cs01: last ? cs0 : cs0 * (1 + 0.08 * Math.cos(i / 4) + rndn() * 0.03),
      });
    });
  })();

  // ---------- рыночная история за год (демо) ----------
  // В базе это md_curve_point, md_bond_quote и pos_snapshot; сервер отдаёт ряды выпуска через
  // /api/v1/bonds/history (docs/SERVICE_DATA_MODEL.md). Здесь — синтетика той же формы.
  // Свой ГПСЧ (основной набор выше не меняется). Первый шаг назад — изменения за день из PnL explain
  // (CURVE_MOVES, SPREAD_MOVES), поэтому «вчера» в истории сходится с объяснением PnL.
  const HIST_DAYS = 261; // ≈ 1 год рабочих дней
  const histDates = (function () {
    const out = [], d = new Date(asOf);
    while (out.length < HIST_DAYS) { if (d.getDay() % 6) out.unshift(isoDay(d)); d.setDate(d.getDate() - 1); }
    return out;
  })();
  const histAge = histDates.map(d => (asOf - new Date(d + 'T00:00:00')) / (365.25 * 864e5)); // лет назад
  function prng(s0) { let x = s0 >>> 0; return () => { x = (Math.imul(x, 1664525) + 1013904223) >>> 0; return x / 4294967296; }; }
  const nrm = r => (r() + r() + r() + r() - 2) / 0.8165;
  function hashStr(str) { let h = 2166136261; for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  const r4 = v => +v.toFixed(4), r2 = v => +v.toFixed(2);

  // кривые: [день (старые → новые)][11 пилларов], %; RUB год назад выше (цикл снижения ставки)
  const CURVE_VOL = { RUB: 5, USD: 3, CNY: 2.5, EUR: 2.5 };
  const curveHist = {};
  Object.keys(CURVES).forEach(c => {
    const r = prng(hashStr('curve|' + c)), ccy = CURVES[c].ccy, vol = CURVE_VOL[ccy] || 3;
    let cur = CURVES[c].pts.slice();
    const back = [cur];
    for (let k = 1; k < HIST_DAYS; k++) {
      let mv = CURVE_MOVES[c];
      if (k > 1) {
        const lvl = nrm(r) * vol, slope = nrm(r) * vol * 0.5;
        mv = cur.map((_, i) => lvl * (1 - i / 14) + slope * (i / 10 - 0.5) + nrm(r) * vol * 0.25 - (ccy === 'RUB' ? (260 - 12 * i) / HIST_DAYS : 0));
      }
      cur = cur.map((v, i) => Math.max(0.25, v - mv[i] / 100));
      back.push(cur);
    }
    curveHist[c] = back.reverse().map(a => a.map(r4));
  });
  function curveAt(k, c, t) {
    const pts = curveHist[c][k];
    if (t <= PILLARS[0]) return pts[0];
    for (let i = 1; i < PILLARS.length; i++) {
      if (t <= PILLARS[i]) { const w = (t - PILLARS[i - 1]) / (PILLARS[i] - PILLARS[i - 1]); return pts[i - 1] * (1 - w) + pts[i] * w; }
    }
    return pts[pts.length - 1];
  }
  function cleanPrice(T, cpn, freq, y) { return bondMath(T, cpn, freq, y).price - cpn * accruedFrac(T, freq); }

  // котировки выпуска: строки в форме md_bond_quote (+ face_amount из pos_snapshot), старые → новые
  const SPREAD_VOL = { AAA: 1.5, 'AA+': 2, AA: 2.5, 'AA-': 3, 'A+': 4, A: 5, BB: 12 };
  const STRESSED = { 'Самолёт': 1, 'Сегежа': 1 };
  const quoteCache = {};
  function bondQuotes(p) {
    if (quoteCache[p.isin]) return quoteCache[p.isin];
    const r = prng(hashStr('bond|' + p.isin)), n = HIST_DAYS, isFloat = p.couponType === 'float';
    const vol = ISSUERS[p.issuer].gov ? 1 : (SPREAD_VOL[p.rating] || 3);
    const anchor = p.spreadBp * (STRESSED[p.issuer] ? 0.55 : 1.12);
    const sm = SPREAD_MOVES[p.issuer] != null ? SPREAD_MOVES[p.issuer] : SPREAD_MOVES._default;
    const s = new Array(n); s[n - 1] = p.spreadBp; s[n - 2] = p.spreadBp - sm;
    for (let k = n - 3; k >= 0; k--) s[k] = s[k + 1] + 0.02 * (anchor - s[k + 1]) + nrm(r) * vol;
    const face = new Array(n).fill(p.notional);
    if (p.newToday) for (let k = 0; k < n - 1; k++) face[k] = 0;
    else {
      const cuts = [0, 1, 2].map(() => 5 + Math.floor(r() * (n - 10))).slice(0, Math.floor(r() * 3));
      cuts.forEach(c => { const f = 0.6 + r() * 0.6; for (let k = 0; k < c; k++) face[k] = Math.round(face[k] * f / 1e6) * 1e6; });
    }
    const Ty = p.yieldDate ? yearsTo(p.yieldDate) : p.tenorY, freq = p.couponFreq;
    const idxCurve = p.index === 'КС' ? 'RUB KEY' : 'RUB RUONIA';
    const effT0 = p.wal ? p.wal * 1.15 : Ty;
    const clean0 = isFloat ? 0 : cleanPrice(effT0, p.coupon, freq, p.ytm / 100);
    const volBase = p.segment === 'Гос' ? 2e9 : 2.5e8;
    const rows = histDates.map((date, k) => {
      const a = histAge[k];
      let ytm, price, g = null, accr;
      if (isFloat) {
        const idx = curveAt(k, idxCurve, PILLARS[0]) - curveAt(n - 1, idxCurve, PILLARS[0]);
        ytm = p.ytm + idx + (s[k] - s[n - 1]) / 100;
        price = p.price - p.sprDur * (s[k] - s[n - 1]) / 100;
        accr = (curveAt(k, idxCurve, PILLARS[0]) + p.margin) * accruedFrac(p.tenorY + a, freq);
      } else {
        const tY = (p.wal || Ty) + a;
        ytm = curveAt(k, p.spreadCurve, tY) + s[k] / 100;
        price = p.price + cleanPrice(effT0 + a, p.coupon, freq, ytm / 100) - clean0;
        if (p.ccy === 'RUB') g = (ytm - curveAt(k, 'RUB OFZ', tY)) * 100;
        accr = p.coupon * accruedFrac(p.tenorY + a, freq);
      }
      if (k === n - 1) { ytm = p.ytm; price = p.price; }
      return { date, clean_price: r4(price), ytm: r4(ytm), z_spread_bp: r2(s[k]), g_spread_bp: g == null ? null : r2(g),
        accrued: r4(accr), volume_rub: Math.round(volBase * Math.exp(nrm(r) * 0.6) / 1e5) * 1e5, face_amount: face[k] };
    });
    return (quoteCache[p.isin] = rows);
  }
  // история выпуска в форме ответа /api/v1/bonds/history: котировки + ставка кривой на сроке доходности и спред к ней
  function bondHistory(p, curve) {
    const c = CURVES[curve] ? curve : p.spreadCurve, okCcy = CURVES[c].ccy === p.ccy, isFloat = p.couponType === 'float';
    const Ty = p.wal || (p.yieldDate ? yearsTo(p.yieldDate) : p.tenorY);
    return {
      instrument_id: p.instrumentId, isin: p.isin, name: p.name, ccy: p.ccy, curve: c,
      spread_to_curve_note: isFloat ? 'флоатер: спред к индексу (DM), не к кривой' : okCcy ? null : 'валюта кривой не совпадает с валютой выпуска',
      series: bondQuotes(p).map((q, k) => {
        const t = Ty + histAge[k], cr = !isFloat && okCcy ? curveAt(k, c, t) : null;
        return Object.assign({}, q, { tenor_y: r4(t), curve_rate: cr == null ? null : r4(cr), spread_to_curve_bp: cr == null ? null : r2((q.ytm - cr) * 100) });
      }),
    };
  }
  positions.forEach(p => {
    if (p.instrType !== 'BOND') return;
    p.instrumentId = p.isin;
    const q = bondQuotes(p), a = q[q.length - 1], b = q[q.length - 2];
    p.quote = { date: a.date, price: a.clean_price, ytm: a.ytm, zSpread: a.z_spread_bp, gSpread: a.g_spread_bp, volume: a.volume_rub,
      prevDate: b.date, prevPrice: b.clean_price, prevYtm: b.ytm, prevZSpread: b.z_spread_bp, prevGSpread: b.g_spread_bp };
  });

  // ---------- сценарии (пресеты) ----------
  // curves — б.п. по бакетам или скаляр; fx — % к RUB (+ = ослабление рубля);
  // ofzSpread — сдвиг спреда ОФЗ к RUONIA, б.п. (× ofzSpr01);
  // spreads — кредитные спреды (× CS01), аддитивно: _all, rating {AA: 50}, sector {'Девелопмент': 300}, issuer {'Самолёт': 300};
  //           _mult — множитель к текущему спреду. Гос не получает «35 % шока»: суверенные бумаги шокируются как все.
  // Базис КС − RUONIA = разница шоков кривых 'RUB KEY' и 'RUB RUONIA'.
  const ruo = (a, k) => a.map(x => x + k);
  const SCENARIOS = [
    { id: 'par+100', name: 'Параллельный +100 б.п. (RUB)', curves: { 'RUB RUONIA': 100, 'RUB KEY': 100 } },
    { id: 'par-100', name: 'Параллельный −100 б.п. (RUB)', curves: { 'RUB RUONIA': -100, 'RUB KEY': -100 } },
    { id: 'keyhike', name: 'Повышение КС +200 б.п.', curves: { 'RUB RUONIA': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35], 'RUB KEY': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35] }, ofzSpread: 10, spreads: { _all: 25 } },
    { id: 'bullsteep', name: 'Bull steepener (смягчение ДКП)', curves: { 'RUB RUONIA': [-160, -150, -130, -105, -75, -60, -45, -35, -25, -20, -15], 'RUB KEY': [-160, -150, -130, -105, -75, -60, -45, -35, -25, -20, -15] }, ofzSpread: 5 },
    { id: 'bearflat', name: 'Bear flattener', curves: { 'RUB RUONIA': [110, 100, 90, 75, 50, 35, 20, 12, 8, 5, 5], 'RUB KEY': [110, 100, 90, 75, 50, 35, 20, 12, 8, 5, 5] }, ofzSpread: 5 },
    { id: 'basis', name: 'Базис КС − RUONIA +50 б.п.', curves: { 'RUB KEY': [50, 50, 45, 40, 30, 25, 20, 15, 10, 10, 10] } },
    { id: 'ofzwide', name: 'Спред ОФЗ к RUONIA +40 б.п.', ofzSpread: 40 },
    { id: 'rubdep', name: 'Ослабление рубля −15%', fx: { USD: 15, CNY: 15, EUR: 15, CHF: 15 }, curves: { 'RUB RUONIA': 50, 'RUB KEY': 50 }, ofzSpread: 10 },
    { id: 'credit', name: 'Кредитный стресс: спреды ×2', spreads: { _mult: 2 }, ofzSpread: 40 },
    { id: 'sector', name: 'Стресс девелоперов и лизинга', spreads: { sector: { 'Девелопмент': 300, 'Лизинг': 150 }, rating: { 'A': 80, 'BB': 250 } } },
    { id: 'stress22', name: 'Стресс «2022»', curves: { 'RUB RUONIA': [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200], 'RUB KEY': [1050, 1000, 850, 680, 500, 410, 330, 280, 250, 230, 210] }, ofzSpread: -20, fx: { USD: 35, CNY: 30, EUR: 32, CHF: 32 }, spreads: { _mult: 2.5 } },
  ];

  // ---------- сценарии кэрри и фондирования (горизонт; позиции постоянны, погашения и оферты учтены) ----------
  // Заседания ЦБ по ключевой ставке: решение в пятницу, новая КС действует со следующего рабочего дня.
  // ОРИЕНТИРОВОЧНЫЙ график — заменить официальным (набор cbr_meetings).
  const CBR_MEETINGS = ['2026-10-23', '2026-12-18', '2027-02-12', '2027-03-19', '2027-04-23', '2027-06-04', '2027-07-23', '2027-09-10', '2027-10-22', '2027-12-17']
    .map(d => { const x = new Date(d + 'T00:00:00'); x.setDate(x.getDate() + 3); return { date: d, effective: isoDay(x) }; });
  // ks.mode: flat — КС не меняется; implied — траектория по форвардам кривой RUB KEY; steps — шаги на заседаниях, б.п.
  // on — сдвиг O/N-индексов (б.п., параллельно на весь горизонт): RUSFAR CNY, SOFR, €STR, SARON;
  //      RUB-индексы (RUONIA, RUSFAR O/N) идут за КС с сегодняшним базисом, плюс сдвиг из on;
  // fundSpread — изменение спреда фондирования торговых книг по валюте, б.п.; hardCcy — ставка hard ccy, %.
  const CARRY_SCENARIOS = [
    { id: 'flat', name: 'КС без изменений', ks: { mode: 'flat' } },
    { id: 'implied', name: 'Рынок: КС по форвардам RUB KEY', ks: { mode: 'implied' } },
    { id: 'cut100', name: 'Смягчение: −100 б.п. на каждом заседании', ks: { mode: 'steps', steps: [-100, -100, -100, -100, -100, -100, -100, -100, -100, -100] } },
    { id: 'cut200', name: 'Быстрое смягчение: 3 × −200 б.п., затем пауза', ks: { mode: 'steps', steps: [-200, -200, -200] } },
    { id: 'hike', name: 'Ужесточение: +200 б.п., затем пауза', ks: { mode: 'steps', steps: [200] } },
    { id: 'fundwide', name: 'Спред фондирования +50 б.п.', ks: { mode: 'flat' }, fundSpread: { RUB: 50, CNY: 50 } },
    { id: 'cny', name: 'Дефицит юаней: RUSFAR CNY +300 б.п.', ks: { mode: 'flat' }, on: { 'RUSFAR CNY': 300 } },
    { id: 'hard7', name: 'Hard ccy фондирование 7 %', ks: { mode: 'flat' }, hardCcy: 7 },
  ];
  const CARRY_HORIZONS = [['1M', 1], ['3M', 3], ['6M', 6], ['1Y', 12]];

  window.DESK = {
    asOf: AS_OF, prevDate: PREV_DATE, accrualDays: ACCRUAL_DAYS, reportingCcy: 'RUB',
    BUCKETS, PILLARS, CURVES, CURVE_MOVES, FX, FX_MOVE_PCT, FX_CBR, FX_CBR_MOVE_PCT, FX_CBR_NEXT, INDICES, ISSUERS, RATING_ORDER, BOOKS, SCENARIOS,
    OFZ_SPREAD_MOVE, JTD_RECOVERY, FUNDING_RULES, OCP_TRANSFERS, CBR_MEETINGS, CARRY_SCENARIOS, CARRY_HORIZONS,
    // официальный PnL за as_of приходит на T+1 (курсы ЦБ); пока его нет — official = null и сверка «ожидается»
    official: { asOf: AS_OF, received: true, basis: 'CBR' },
    positions, funding, fundingPnl, fundingPnlByBook, history,
    source: { kind: 'demo', runId: null, runType: 'DEMO', publishedAt: null },
    // только демо: ряды за год (в режиме базы — /api/v1/bonds/history); функции не попадают в JSON
    demo: { histDates, curveHist, bondQuotes, bondHistory },
  };
  // DESK.util добавляет desk-util.js
})();
