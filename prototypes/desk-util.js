/*
 * DESK.util — функции поверх данных window.DESK (из базы или демо data.js).
 * Подключается сразу после скрипта с данными.
 */
(function () {
  'use strict';
  const D = window.DESK;
  if (!D) { document.title = 'Нет данных'; throw new Error('window.DESK не загружен'); }
  const { BUCKETS, PILLARS, CURVES, FX } = D;

  function sum(arr, f) { return arr.reduce((s, x) => s + (f ? f(x) : x), 0); }
  function groupBy(arr, key) {
    const out = {};
    arr.forEach(x => { const k = typeof key === 'function' ? key(x) : x[key]; (out[k] = out[k] || []).push(x); });
    return out;
  }
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
  function bucketMatrix(list, curves) {
    curves = curves || Object.keys(CURVES);
    const m = {}; curves.forEach(c => { m[c] = new Array(BUCKETS.length).fill(0); });
    list.forEach(p => Object.keys(p.risk || {}).forEach(c => { if (m[c]) p.risk[c].forEach((v, i) => { m[c][i] += v; }); }));
    return m;
  }
  function netFx(list, withFunding) {
    const out = {};
    Object.keys(FX).forEach(c => { if (c !== 'RUB') out[c] = 0; });
    list.forEach(p => Object.keys(p.fxDelta || {}).forEach(c => { out[c] = (out[c] || 0) + p.fxDelta[c]; }));
    if (withFunding) {
      const books = new Set(list.map(p => p.book));
      D.funding.filter(f => books.has(f.book)).forEach(f => Object.keys(f.fxDelta).forEach(c => { out[c] = (out[c] || 0) + f.fxDelta[c]; }));
    }
    return out;
  }
  // FX-переоценка валютного фондирования относится к позициям книги, которые оно фондирует
  // (pro-rata по валютной дельте противоположного знака); остаток — строка фондирования книги.
  function allocateFundingFx(shockFn, list) {
    list = list || D.positions;
    const toPos = {}, residualByBook = {}, byBook = {}, residualByDeal = {};
    D.funding.forEach(f => {
      if (f.ccy === 'RUB') return;
      const x = shockFn(f.ccy, f.amount);
      if (!x) return;
      byBook[f.book] = (byBook[f.book] || 0) + x;
      const sgn = -Math.sign(f.amount);
      const cands = list.filter(p => p.book === f.book && Math.sign(p.fxDelta[f.ccy] || 0) === sgn);
      const w = cands.reduce((a, p) => a + Math.abs(p.fxDelta[f.ccy]), 0);
      if (!w) { residualByBook[f.book] = (residualByBook[f.book] || 0) + x; residualByDeal[f.id] = x; return; }
      cands.forEach(p => { toPos[p.id] = (toPos[p.id] || 0) + x * Math.abs(p.fxDelta[f.ccy]) / w; });
    });
    return { toPos, residualByBook, byBook, residualByDeal };
  }

  // Полная переоценка из расчётного сервиса (scenario_result) — только для неизменённых пресетов.
  const shockKey = sc => JSON.stringify([sc.curves || null, sc.fx || null, sc.spreads || null]);
  function storedResult(sc, list) {
    const R = D.scenarioResults && D.scenarioResults[sc.id];
    const preset = D.SCENARIOS.find(s => s.id === sc.id);
    if (!R || !preset || shockKey(preset) !== shockKey(sc)) return null;
    const books = new Set(list.map(p => p.book));
    const res = list.map(p => {
      const r = R.byPosition[p.id];
      if (!r) return { id: p.id, rates: 0, spread: 0, fx: 0, fxGross: 0, fxFunding: 0, convexity: 0, total: 0 };
      return { id: p.id, rates: r.rates, spread: r.spread, fx: r.fxGross + r.fxFunding, fxGross: r.fxGross, fxFunding: r.fxFunding, convexity: r.convexity, total: r.total };
    });
    const fundingFxByBook = {};
    Object.keys(R.fundingFxByBook || {}).forEach(b => { if (books.has(b)) fundingFxByBook[b] = R.fundingFxByBook[b]; });
    const fundFx = sum(Object.values(fundingFxByBook));
    return { byPosition: res, fundingFx: fundFx, fundingFxByBook, total: sum(res, r => r.total) + fundFx, fullReval: true };
  }

  // Сценарий: полная переоценка, если её посчитал расчётный сервис, иначе
  // линейная оценка по чувствительностям (дельта + выпуклость облигаций, FX линейно).
  function runScenario(sc, list) {
    list = list || D.positions;
    const stored = storedResult(sc, list);
    if (stored) return stored;
    const curveShock = {};
    Object.keys(CURVES).forEach(c => {
      const v = sc.curves && sc.curves[c];
      curveShock[c] = v == null ? new Array(BUCKETS.length).fill(0) : (Array.isArray(v) ? v : new Array(BUCKETS.length).fill(v));
    });
    const alloc = sc.fx
      ? allocateFundingFx((ccy, amt) => amt * FX[ccy] * (sc.fx[ccy] || 0) / 100, D.positions)
      : { toPos: {}, residualByBook: {}, byBook: {} };
    const booksInList = new Set(list.map(p => p.book));
    Object.keys(alloc.residualByBook).forEach(b => { if (!booksInList.has(b)) delete alloc.residualByBook[b]; });
    const res = list.map(p => {
      let rates = 0, spread = 0, conv = 0;
      Object.keys(p.risk).forEach(c => { if (curveShock[c]) p.risk[c].forEach((dv, i) => { rates += dv * curveShock[c][i]; }); });
      if (p.instrType === 'BOND' && p.convexity) {
        const avg = Object.keys(p.risk).reduce((s, c) => s + (curveShock[c] ? curveShock[c].reduce((a, b) => a + b, 0) / BUCKETS.length : 0), 0);
        conv = 0.5 * p.mvRub * p.convexity * Math.pow(avg * 1e-4, 2);
      }
      if (p.cs01) {
        let ds = 0;
        if (sc.spreads) {
          if (sc.spreads._mult) ds += (sc.spreads._mult - 1) * (p.spreadBp || 100);
          if (sc.spreads._all) ds += sc.spreads._all;
          if (p.rating && sc.spreads[p.rating]) ds += sc.spreads[p.rating];
          if (p.issuer && sc.spreads[p.issuer]) ds += sc.spreads[p.issuer];
          if (p.segment === 'Гос' && !sc.spreads.includeGov) ds *= 0.35;
        }
        spread = p.cs01 * ds;
      }
      let fxGross = 0;
      if (sc.fx) Object.keys(p.fxDelta).forEach(c => { fxGross += p.fxDelta[c] * FX[c] * (sc.fx[c] || 0) / 100; });
      const fxFunding = alloc.toPos[p.id] || 0;
      const fxr = fxGross + fxFunding;
      return { id: p.id, rates, spread, fx: fxr, fxGross, fxFunding, convexity: conv, total: rates + spread + fxr + conv };
    });
    const fundingFxByBook = alloc.residualByBook;
    const fundFx = sum(Object.values(fundingFxByBook));
    return { byPosition: res, fundingFx: fundFx, fundingFxByBook, total: sum(res, r => r.total) + fundFx, fullReval: false };
  }

  D.util = { sum, groupBy, bucketMatrix, netFx, runScenario, curveRate, allocateFundingFx };
})();
