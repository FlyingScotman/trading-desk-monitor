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
  };
  // изменения за день (б.п.) — для PnL explain
  const CURVE_MOVES = {
    'RUB OFZ':    [14, 13, 12, 11, 9, 8, 6, 5, 4, 4, 3],
    'RUB RUONIA': [6, 7, 8, 8, 7, 6, 5, 4, 3, 3, 3],
    'RUB KEY':    [9, 9, 9, 8, 7, 6, 5, 4, 3, 3, 3],
    'USD SOFR':   [0, -1, -1, -2, -3, -3, -3, -2, -2, -2, -2],
    'CNY':        [1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0],
    'EUR ESTR':   [0, 0, 1, 1, 1, 2, 2, 2, 2, 2, 2],
  };
  const FX = { RUB: 1, USD: 81.45, EUR: 94.62, CNY: 11.38 };
  const FX_MOVE_PCT = { RUB: 0, USD: 0.62, EUR: 0.21, CNY: 0.44 };
  const INDICES = { 'КС': 14.50, 'RUONIA': 14.32, 'SOFR': 4.08, 'RUSFAR CNY': 5.45, '€STR': 2.15 };

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

  // ---------- книги ----------
  const BOOKS = {
    OFZ:      { name: 'ОФЗ',               desk: 'Rates',  trader: 'А. Иванов' },
    RATES:    { name: 'Свопы RUB',         desk: 'Rates',  trader: 'А. Иванов' },
    CORP_RUB: { name: 'Корпы RUB',         desk: 'Credit', trader: 'Е. Петрова' },
    CREDIT:   { name: 'CDS',               desk: 'Credit', trader: 'Е. Петрова' },
    CORP_FX:  { name: 'Корпы валютные',    desk: 'FX Credit', trader: 'Д. Сидоров' },
    SOV_FX:   { name: 'Суверен валютный',  desk: 'FX Credit', trader: 'Д. Сидоров' },
    SECUR:    { name: 'Секьюритизация',    desk: 'Credit', trader: 'М. Козлова' },
    FXB:      { name: 'FX',                desk: 'FX',     trader: 'И. Морозов' },
  };

  const CCY_CURVE = { RUB: 'RUB OFZ', USD: 'USD SOFR', CNY: 'CNY', EUR: 'EUR ESTR' };

  const positions = [];
  let nextId = 1;

  // ---------- облигации ----------
  // [book, assetClass, name, issuer, ccy, maturity, coupon/margin, freq, notional(ccy), opts]
  function bond(book, assetClass, name, issuer, ccy, maturity, cpn, freq, notional, opts) {
    opts = opts || {};
    const T = Math.max(0.05, yearsTo(maturity));
    const iss = ISSUERS[issuer];
    const isFloat = !!opts.index;
    const curve = isFloat ? (opts.index === 'КС' ? 'RUB KEY' : 'RUB RUONIA') : (ccy === 'RUB' ? 'RUB OFZ' : CCY_CURVE[ccy]);
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
      const base = curveRate(curve, opts.wal || T);
      ytm = base + spreadBp / 100;
      const effT = opts.wal ? opts.wal * 1.15 : T;
      const m = bondMath(effT, cpn, freq, ytm / 100);
      price = m.price; mdur = opts.wal ? m.mdur * 0.9 : m.mdur; convexity = m.convexity;
      rateDur = mdur; sprDur = mdur;
    }
    const mv = notional * price / 100;
    const mvRub = mv * FX[ccy];
    const dv01Total = -mvRub * rateDur * 1e-4;
    // ОФЗ-ПД сами задают кривую ОФЗ: спредового риска к ней нет
    const cs01 = (iss.gov && ccy === 'RUB' && !isFloat) ? 0 : -mvRub * sprDur * 1e-4;
    const bucketT = isFloat ? (opts.index === 'КС' ? 1 / 12 : 0.25) : (opts.wal || T);
    const risk = {}; risk[curve] = allocate(bucketT, dv01Total, isFloat ? 0 : 0.2);
    const fxDelta = {}; if (ccy !== 'RUB') fxDelta[ccy] = mv;
    const segment = iss.gov ? 'Гос' : (assetClass === 'Секьюритизация' ? 'Секьюритизация' : 'Корп');
    positions.push({
      id: nextId++, book, assetClass, segment, instrType: 'BOND', name, isin: opts.isin || fakeIsin(),
      issuer, issuerGroup: iss.group, sector: iss.sector, rating: iss.rating, ccy, maturity, tenorY: T,
      couponType: isFloat ? 'float' : 'fix', coupon: isFloat ? null : cpn, index: opts.index || null, margin: isFloat ? cpn : null,
      notional, price, mv, mvRub, ytm, spreadBp, mdur, sprDur, convexity,
      dv01: dv01Total, cs01, risk, fxDelta, jtd: mvRub * (1 - 0.35),
      wal: opts.wal || null, cpr: opts.cpr || null, newToday: !!opts.newToday,
    });
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
  cf('МТС 002Р-08', 'МТС', '2029-11-20', 14.90, 4, 0.75 * bn);
  cf('Ростелеком 002Р-17R', 'Ростелеком', '2028-03-14', 15.20, 4, 0.6 * bn);
  cf('Магнит БО-004Р-05', 'Магнит', '2028-09-08', 14.40, 4, 0.8 * bn);
  cf('X5 Финанс 003Р-04', 'X5', '2029-05-25', 14.10, 4, 0.7 * bn, { newToday: true });
  cf('Норникель БО-001Р-09', 'Норникель', '2030-07-19', 13.60, 2, 0.95 * bn);
  cf('Сибур Холдинг ПБО-10', 'Сибур', '2029-10-03', 13.90, 2, 0.5 * bn);
  cf('Самолёт БО-П15', 'Самолёт', '2027-12-15', 17.50, 12, 0.42 * bn);
  cf('Сегежа 003Р-02R', 'Сегежа', '2027-06-20', 19.50, 12, 0.28 * bn);
  cf('ГТЛК 002Р-04', 'ГТЛК', '2031-02-27', 15.40, 12, 0.65 * bn);
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
      dv01: 0, cs01, risk: {}, fxDelta: {}, jtd: -s * notional * (1 - 0.35), counterparty: opts.cpty || 'Банк-контрагент А', newToday: !!opts.newToday,
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

  // ---------- фондирование ----------
  const funding = [];
  let fid = 1;
  function fund(book, type, ccy, amount, rateExpr, rate, start, maturity, cpty, collateral) {
    const T = Math.max(1 / 365, yearsTo(maturity));
    funding.push({ id: fid++, book, type, ccy, amount, amountRub: amount * FX[ccy], rateExpr, rate, start, maturity, tenorY: T, counterparty: cpty, collateral: collateral || null });
  }
  fund('OFZ', 'РЕПО с ЦК', 'RUB', -14.5 * bn, 'RUSFAR O/N', 14.28, '2026-10-13', '2026-10-14', 'НКЦ', 'ОФЗ-ПД, ОФЗ-ПК');
  fund('OFZ', 'РЕПО с ЦК', 'RUB', -6.0 * bn, 'RUSFAR 1W', 14.35, '2026-10-09', '2026-10-16', 'НКЦ', 'ОФЗ-ПД');
  fund('OFZ', 'TSY FTP', 'RUB', -9.0 * bn, 'RUONIA + 0.20%', 14.52, '2026-07-01', '2027-01-15', 'Казначейство', null);
  fund('OFZ', 'Обратное РЕПО', 'RUB', 2.1 * bn, 'RUSFAR O/N − 0.10%', 14.18, '2026-10-13', '2026-10-14', 'НКЦ', 'шорт ОФЗ 26246/26248');
  fund('CORP_RUB', 'РЕПО с ЦК', 'RUB', -7.5 * bn, 'RUSFAR O/N + 0.05%', 14.33, '2026-10-13', '2026-10-14', 'НКЦ', 'корп. облигации');
  fund('CORP_RUB', 'TSY FTP', 'RUB', -6.0 * bn, 'КС + 0.35%', 14.85, '2026-04-01', '2027-04-01', 'Казначейство', null);
  fund('CORP_RUB', 'TSY FTP', 'RUB', -4.2 * bn, '15.10% фикс', 15.10, '2026-02-15', '2027-08-15', 'Казначейство', null);
  fund('CORP_FX', 'TSY FTP', 'USD', -42 * mm, 'SOFR + 1.80%', 5.88, '2026-05-10', '2027-05-10', 'Казначейство', null);
  fund('CORP_FX', 'РЕПО с ЦК', 'CNY', -720 * mm, 'RUSFAR CNY O/N', 5.42, '2026-10-13', '2026-10-14', 'НКЦ', 'облигации в CNY');
  fund('CORP_FX', 'TSY FTP', 'CNY', -350 * mm, 'RUSFAR CNY + 0.60%', 6.05, '2026-08-01', '2027-02-01', 'Казначейство', null);
  fund('CORP_FX', 'TSY FTP', 'EUR', -8 * mm, '€STR + 2.00%', 4.15, '2026-03-01', '2027-09-01', 'Казначейство', null);
  fund('SOV_FX', 'РЕПО с ЦК', 'CNY', -600 * mm, 'RUSFAR CNY 1W', 5.48, '2026-10-08', '2026-10-15', 'НКЦ', 'ОФЗ-CNY');
  fund('SOV_FX', 'TSY FTP', 'CNY', -170 * mm, 'RUSFAR CNY + 0.55%', 6.00, '2026-09-01', '2027-03-01', 'Казначейство', null);
  fund('SOV_FX', 'TSY FTP', 'USD', -50 * mm, 'SOFR + 1.60%', 5.68, '2026-06-01', '2027-06-01', 'Казначейство', null);
  fund('SOV_FX', 'TSY FTP', 'EUR', -6 * mm, '€STR + 1.90%', 4.05, '2026-03-01', '2027-03-01', 'Казначейство', null);
  fund('SECUR', 'TSY FTP', 'RUB', -5.5 * bn, 'КС + 0.40%', 14.90, '2026-01-20', '2027-07-20', 'Казначейство', null);
  fund('SECUR', 'РЕПО с ЦК', 'RUB', -2.4 * bn, 'RUSFAR 1M', 14.45, '2026-09-29', '2026-10-29', 'НКЦ', 'ИЦБ ДОМ.РФ');
  fund('FXB', 'FX-своп', 'USD', 5 * mm, 'SOFR − 0.20%', 3.88, '2026-10-13', '2026-10-20', 'Мосбиржа', null);
  fund('FXB', 'FX-своп', 'CNY', -120 * mm, 'RUSFAR CNY', 5.45, '2026-10-13', '2026-11-13', 'Мосбиржа', null);
  fund('FXB', 'TSY FTP', 'RUB', 3.0 * bn, 'RUONIA − 0.15%', 14.17, '2026-10-01', '2026-11-01', 'Казначейство', null);
  fund('RATES', 'Гарантийное обеспечение', 'RUB', 1.4 * bn, 'RUONIA − 0.50%', 13.82, '2026-01-01', '2026-10-14', 'НКЦ', 'ВМ/ГО по свопам');
  fund('CREDIT', 'TSY FTP', 'RUB', 0.4 * bn, 'RUONIA', 14.32, '2026-10-01', '2026-10-14', 'Казначейство', null);
  // финансирование валютной ликвидности — влияет на OCP
  funding.forEach(f => { f.fxDelta = {}; if (f.ccy !== 'RUB') f.fxDelta[f.ccy] = f.amount; });

  // ---------- PnL за день и explain ----------

  positions.forEach(p => {
    const e = { carry: 0, rolldown: 0, rates: 0, spread: 0, fx: 0, convexity: 0, newTrades: 0, other: 0 };
    if (p.instrType === 'BOND') {
      e.carry = p.mvRub * p.ytm / 100 / 365; // валовый купонный/доходностный кэрри; фондирование — отдельной строкой
      e.rolldown = p.couponType === 'fix' ? Math.abs(p.dv01) * 0.02 * Math.sign(p.mvRub) : 0;
      const sm = SPREAD_MOVES[p.issuer] != null ? SPREAD_MOVES[p.issuer] : SPREAD_MOVES._default;
      e.spread = p.cs01 * sm;
      e.convexity = 0.5 * p.mvRub * (p.convexity || 0) * Math.pow(8e-4, 2);
    } else if (p.instrType === 'IRS') {
      const fl = p.index === 'КС' ? INDICES['КС'] : p.index === 'RUONIA' ? INDICES.RUONIA : INDICES['RUSFAR CNY'];
      const sgn = p.side === 'PAY' ? 1 : -1;
      e.carry = sgn * p.notional * FX[p.ccy] * (fl - p.coupon) / 100 / 365;
      e.rolldown = -Math.abs(p.dv01) * 0.015 * sgn;
    } else if (p.instrType === 'XCCY') {
      e.carry = (p.side === 'RECFX' ? -1 : 1) * p.notional * FX[p.ccy] * 0.065 / 365;
    } else if (p.instrType === 'CDS') {
      const sgn = p.side === 'BUY' ? -1 : 1;
      e.carry = sgn * p.notional * (p.coupon / 100) / 365;
      const sm = SPREAD_MOVES[p.issuer] != null ? SPREAD_MOVES[p.issuer] : SPREAD_MOVES._default;
      e.spread = p.cs01 * sm;
    } else if (p.instrType === 'FX') {
      e.carry = 0;
    }
    Object.keys(p.risk).forEach(c => { p.risk[c].forEach((dv, i) => { e.rates += dv * CURVE_MOVES[c][i]; }); });
    Object.keys(p.fxDelta).forEach(c => { e.fx += p.fxDelta[c] * FX[c] * FX_MOVE_PCT[c] / 100 / (1 + FX_MOVE_PCT[c] / 100); });
    if (p.newToday) { e.newTrades = Math.abs(p.mvRub) * (0.0004 + rnd() * 0.0012) * (rnd() < 0.25 ? -1 : 1); }
    const explained = Object.values(e).reduce((a, b) => a + b, 0);
    let unexplained = rndn() * Math.max(40000, Math.abs(explained) * 0.04);
    let breakNote = null;
    if (p.name === 'Сегежа 003Р-02R') { unexplained = -4.6e6; breakNote = 'Расхождение котировки: фронт — оценочная цена, бэк — BVAL Мосбиржи'; }
    if (p.name === 'ИЦБ ДОМ.РФ 2Р-12R') { unexplained = 2.1e6; breakNote = 'Обновлён CPR (12.5% → 13.4%) в бэк-офисной модели, фронт не перезагрузил'; }
    if (p.name.startsWith('XCCY USD/RUB 3Y')) { unexplained = -1.35e6; breakNote = 'Фиксинг SOFR не загружен во фронт-систему'; }
    if (p.name === 'ОФЗ 26247') { unexplained = 0.85e6; breakNote = 'Сделка дня: цена сделки vs закрытие — разные время среза'; }
    p.pnl = Object.assign({}, e, { explained, unexplained, official: explained + unexplained, breakNote });
  });
  // Валютное фондирование: FX-переоценка привлечения в валюте относится к тем позициям книги,
  // которые оно фондирует (pro-rata по валютной дельте позиций противоположного знака).
  // Так книга видит FX-риск нетто: облигация в CNY на CNY-РЕПО почти не даёт FX-PnL.
  // Остаток (если в книге нечего фондировать в этой валюте) остаётся строкой фондирования книги.
  function allocateFundingFx(shockFn, list) {
    list = list || positions;
    const toPos = {}, residualByBook = {}, byBook = {}, residualByDeal = {};
    funding.forEach(f => {
      if (f.ccy === 'RUB') return;
      const x = shockFn(f.ccy, f.amount);
      if (!x) return;
      byBook[f.book] = (byBook[f.book] || 0) + x;
      const sgn = -Math.sign(f.amount); // привлечение (−) фондирует длинные (+) позиции
      const cands = list.filter(p => p.book === f.book && Math.sign(p.fxDelta[f.ccy] || 0) === sgn);
      const w = cands.reduce((a, p) => a + Math.abs(p.fxDelta[f.ccy]), 0);
      if (!w) { residualByBook[f.book] = (residualByBook[f.book] || 0) + x; residualByDeal[f.id] = x; return; }
      cands.forEach(p => { toPos[p.id] = (toPos[p.id] || 0) + x * Math.abs(p.fxDelta[f.ccy]) / w; });
    });
    return { toPos, residualByBook, byBook, residualByDeal };
  }
  const dayFxShock = (ccy, amt) => amt * FX[ccy] * FX_MOVE_PCT[ccy] / 100 / (1 + FX_MOVE_PCT[ccy] / 100);
  const dayAlloc = allocateFundingFx(dayFxShock);
  positions.forEach(p => {
    const ff = dayAlloc.toPos[p.id] || 0;
    p.pnl.fxGross = p.pnl.fx; p.pnl.fxFunding = ff; p.pnl.fx += ff;
    p.pnl.explained += ff; p.pnl.official += ff;
  });

  // стоимость фондирования (кэрри) — отдельной строкой по книге; FX — только нераспределённый остаток
  const fundingPnl = { carry: 0, fx: 0 };
  const fundingPnlByBook = {};
  Object.keys(BOOKS).forEach(b => { fundingPnlByBook[b] = { carry: 0, fx: dayAlloc.residualByBook[b] || 0, fxAllocated: 0 }; });
  funding.forEach(f => {
    const c = f.amountRub * f.rate / 100 / 365;
    const x = f.ccy !== 'RUB' ? dayFxShock(f.ccy, f.amount) : 0;
    // fx — нераспределённый остаток (обычно 0), fxAllocated — ушло в позиции книги
    const xr = dayAlloc.residualByDeal[f.id] || 0;
    f.pnl = { carry: c, fx: xr, fxGross: x, fxAllocated: x - xr };
    fundingPnl.carry += c;
    fundingPnlByBook[f.book].carry += c;
  });
  Object.keys(BOOKS).forEach(b => {
    fundingPnlByBook[b].fxAllocated = (dayAlloc.byBook[b] || 0) - fundingPnlByBook[b].fx;
    fundingPnl.fx += fundingPnlByBook[b].fx;
  });

  // ---------- история (22 раб. дня) ----------
  const history = [];
  (function () {
    const d = new Date(asOf); const days = [];
    while (days.length < 22) { if (d.getDay() !== 0 && d.getDay() !== 6) days.unshift(new Date(d)); d.setDate(d.getDate() - 1); }
    const dv0 = positions.reduce((s, p) => s + p.dv01, 0);
    const cs0 = positions.reduce((s, p) => s + p.cs01, 0);
    const offPnl0 = positions.reduce((s, p) => s + p.pnl.official, 0) + fundingPnl.carry + fundingPnl.fx;
    const expPnl0 = positions.reduce((s, p) => s + p.pnl.explained, 0) + fundingPnl.carry + fundingPnl.fx;
    let cum = 0;
    days.forEach((dt, i) => {
      const last = i === days.length - 1;
      const off = last ? offPnl0 : rndn() * 18e6 + 4e6;
      const exp = last ? expPnl0 : off - rndn() * 1.4e6;
      cum += off;
      history.push({
        date: dt.toISOString().slice(0, 10),
        pnlOfficial: off, pnlExplained: exp, pnlCum: cum,
        dv01: last ? dv0 : dv0 * (1 + 0.18 * Math.sin(i / 3) + rndn() * 0.05),
        cs01: last ? cs0 : cs0 * (1 + 0.08 * Math.cos(i / 4) + rndn() * 0.03),
      });
    });
  })();

  // ---------- сценарии (пресеты) ----------
  // shocks: curves (б.п. по бакетам или скаляр), fx (% к RUB), spreads (б.п. по рейтингу/_all), issuers
  const SCENARIOS = [
    { id: 'par+100', name: 'Параллельный +100 б.п. (RUB)', curves: { 'RUB OFZ': 100, 'RUB RUONIA': 100, 'RUB KEY': 100 } },
    { id: 'par-100', name: 'Параллельный −100 б.п. (RUB)', curves: { 'RUB OFZ': -100, 'RUB RUONIA': -100, 'RUB KEY': -100 } },
    { id: 'keyhike', name: 'Повышение КС +200 б.п.', curves: { 'RUB OFZ': [200, 190, 170, 140, 100, 80, 60, 50, 40, 35, 30], 'RUB RUONIA': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35], 'RUB KEY': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35] }, spreads: { _all: 25 } },
    { id: 'bullsteep', name: 'Bull steepener (смягчение ДКП)', curves: { 'RUB OFZ': [-150, -140, -120, -100, -70, -55, -40, -30, -20, -15, -10], 'RUB RUONIA': [-160, -150, -130, -105, -75, -60, -45, -35, -25, -20, -15], 'RUB KEY': [-160, -150, -130, -105, -75, -60, -45, -35, -25, -20, -15] } },
    { id: 'bearflat', name: 'Bear flattener', curves: { 'RUB OFZ': [120, 110, 95, 80, 55, 40, 25, 15, 10, 5, 5], 'RUB RUONIA': [110, 100, 90, 75, 50, 35, 20, 12, 8, 5, 5], 'RUB KEY': [110, 100, 90, 75, 50, 35, 20, 12, 8, 5, 5] } },
    { id: 'rubdep', name: 'Ослабление рубля −15%', fx: { USD: 15, CNY: 15, EUR: 15 }, curves: { 'RUB OFZ': 60, 'RUB RUONIA': 50, 'RUB KEY': 50 } },
    { id: 'credit', name: 'Кредитный стресс: спреды ×2', spreads: { _mult: 2 }, curves: { 'RUB OFZ': 40 } },
    { id: 'stress22', name: 'Стресс «2022»', curves: { 'RUB OFZ': [900, 850, 750, 600, 450, 380, 300, 250, 220, 200, 180], 'RUB RUONIA': [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200], 'RUB KEY': [1050, 1000, 850, 680, 500, 410, 330, 280, 250, 230, 210] }, fx: { USD: 35, CNY: 30, EUR: 32 }, spreads: { _mult: 2.5 } },
  ];

  window.DESK = {
    asOf: AS_OF, reportingCcy: 'RUB',
    BUCKETS, PILLARS, CURVES, CURVE_MOVES, FX, FX_MOVE_PCT, INDICES, ISSUERS, RATING_ORDER, BOOKS, SCENARIOS,
    positions, funding, fundingPnl, fundingPnlByBook, history,
    source: { kind: 'demo', runId: null, runType: 'DEMO', publishedAt: null },
  };
  // DESK.util добавляет desk-util.js
})();
