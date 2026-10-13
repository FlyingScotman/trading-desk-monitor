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

  // ---------- второй порядок ----------
  // Матрица вторых производных позиции p.gamma = [[f1, f2, d2PV], ...] (верхний треугольник), факторы:
  //   'IR|<кривая>|<бакет>' — 1 б.п.; 'CS' — свой спред, 1 б.п.; 'FX|<валюта>' — 1 %.
  // PnL2 = Σ 0.5·Γaa·Δa² + Σ(a<b) Γab·Δa·Δb.
  const BIDX = {}; BUCKETS.forEach((b, i) => { BIDX[b] = i; });
  function parseFactor(f) {
    const x = f.split('|');
    if (x[0] === 'IR') return { t: 'IR', c: x[1], i: BIDX[x[2]] };
    if (x[0] === 'FX') return { t: 'FX', c: x[1] };
    return { t: 'CS' };
  }
  const gammaCache = new WeakMap();
  function gammaOf(p) {
    if (!p.gamma || !p.gamma.length) return null;
    let g = gammaCache.get(p);
    if (!g) {
      g = p.gamma.map(e => ({ a: parseFactor(e[0]), b: parseFactor(e[1]), v: e[2], w: e[0] === e[1] ? 0.5 : 1 }));
      g.ir = g.some(e => e.a.t !== 'FX' && e.b.t !== 'FX');
      g.fx = g.some(e => e.a.t === 'FX' || e.b.t === 'FX');
      gammaCache.set(p, g);
    }
    return g;
  }
  // Возвращает { convexity: 2-й порядок по ставкам и спредам, fxCross: кросс FX × ставки/спреды, src }.
  //  - есть IR/CS-элементы матрицы → по ним; иначе из выпуклости: 0.5·MV·C·(Δy + Δs)², где Δy = rates/DV01 —
  //    DV01-взвешенный сдвиг кривой (доходность = ставка + спред, поэтому кросс ставки×спред учтён);
  //  - есть FX-элементы → по ним; иначе точный кросс первых порядков: чувствительность к иностранной кривой
  //    и спреду валютной бумаги выражена в рублях по текущему курсу, при шоке курса она масштабируется на (1 + Δfx).
  // Эффективный сдвиг ставок позиции, б.п.: среднее по key-rate с весами |DV01| (ограничено фактическими сдвигами;
  // у позиций с ногами разного знака — XCCY, FX-форварды — rates/DV01 не имеет смысла).
  function effRateMove(p, curveShock) {
    let w = 0, x = 0;
    Object.keys(p.risk || {}).forEach(c => { if (curveShock[c]) p.risk[c].forEach((dv, i) => { w += Math.abs(dv); x += Math.abs(dv) * curveShock[c][i]; }); });
    return w ? x / w : 0;
  }
  function secondOrder(p, curveShock, ds, fxShock, ratesByCurve, spreadPnl) {
    const g = gammaOf(p);
    const d = f => f.t === 'IR' ? ((curveShock[f.c] && f.i != null) ? curveShock[f.c][f.i] : 0) : f.t === 'CS' ? ds : (fxShock[f.c] || 0);
    let conv = 0, cross = 0, src = 'none', convFx = 0;
    // для пересчёта выпуклости валютной части в рубли: курс, в котором номинирован фактор
    const fxOf = f => f.t === 'IR' ? (CURVES[f.c] && CURVES[f.c].ccy !== 'RUB' ? CURVES[f.c].ccy : null) : f.t === 'CS' ? (p.ccy !== 'RUB' ? p.ccy : null) : null;
    if (g) g.forEach(e => {
      const x = e.w * e.v * d(e.a) * d(e.b);
      if (e.a.t === 'FX' || e.b.t === 'FX') cross += x;
      else { conv += x; const c = fxOf(e.a) || fxOf(e.b); if (c) convFx += x * (fxShock[c] || 0) / 100; }
    });
    if (g && g.ir) src = 'gamma';
    else if (p.convexity && p.mvRub) {
      const dy = effRateMove(p, curveShock);
      const dsEff = p.cs01 ? ds : 0;
      conv = 0.5 * p.mvRub * p.convexity * Math.pow((dy + dsEff) * 1e-4, 2);   // Δy — DV01-взвешенный сдвиг кривой
      src = 'convexity';
      if (p.ccy !== 'RUB') convFx = conv * (fxShock[p.ccy] || 0) / 100;
    }
    if (!(g && g.fx)) {
      cross = 0;
      Object.keys(ratesByCurve).forEach(c => { const ccy = CURVES[c] && CURVES[c].ccy; if (ccy && ccy !== 'RUB') cross += ratesByCurve[c] * (fxShock[ccy] || 0) / 100; });
      if (p.ccy !== 'RUB') cross += spreadPnl * (fxShock[p.ccy] || 0) / 100;
    }
    // Стоимость в рублях = курс × стоимость в валюте: при шоке курса масштабируется и выпуклость валютной части
    // (член 3-го порядка Γ·Δr²·Δfx, точный для пересчёта в рубли; включаем в fxCross).
    cross += convFx;
    // эффективный сдвиг доходности позиции, б.п. — для оценки, в пределах ли шок области точности разложения
    const dyEff = effRateMove(p, curveShock) + (p.cs01 ? ds : 0);
    return { convexity: conv, fxCross: cross, src, dyEff };
  }

  // Полная переоценка из расчётного сервиса (scenario_result) — только для неизменённых пресетов.
  const shockKey = sc => JSON.stringify([sc.curves || null, sc.fx || null, sc.spreads || null]);
  function storedResult(sc, list) {
    const R = D.scenarioResults && D.scenarioResults[sc.id];
    const preset = D.SCENARIOS.find(s => s.id === sc.id);
    if (!R || !preset || shockKey(preset) !== shockKey(sc)) return null;
    const books = new Set(list.map(p => p.book));
    const zero = { rates: 0, spread: 0, fx: 0, fxGross: 0, fxFunding: 0, convexity: 0, fxCross: 0, total: 0 };
    const res = list.map(p => {
      const r = R.byPosition[p.id];
      if (!r) return Object.assign({ id: p.id }, zero);
      return { id: p.id, rates: r.rates, spread: r.spread, fx: r.fxGross + r.fxFunding, fxGross: r.fxGross, fxFunding: r.fxFunding,
        convexity: r.convexity, fxCross: r.fxCross || 0, total: r.total };
    });
    const fundingFxByBook = {};
    Object.keys(R.fundingFxByBook || {}).forEach(b => { if (books.has(b)) fundingFxByBook[b] = R.fundingFxByBook[b]; });
    const fundFx = sum(Object.values(fundingFxByBook));
    return { byPosition: res, fundingFx: fundFx, fundingFxByBook, total: sum(res, r => r.total) + fundFx, fullReval: true };
  }

  const ACCURACY_BP = 500;
  let posIndex = null;
  const byIdPos = id => { if (!posIndex) { posIndex = {}; D.positions.forEach(p => { posIndex[p.id] = p; }); } return posIndex[id]; };

  // Спред-шок позиции, б.п.
  function spreadShock(sc, p) {
    let ds = 0;
    if (sc.spreads) {
      if (sc.spreads._mult) ds += (sc.spreads._mult - 1) * (p.spreadBp || 100);
      if (sc.spreads._all) ds += sc.spreads._all;
      if (p.rating && sc.spreads[p.rating]) ds += sc.spreads[p.rating];
      if (p.issuer && sc.spreads[p.issuer]) ds += sc.spreads[p.issuer];
      if (p.segment === 'Гос' && !sc.spreads.includeGov) ds *= 0.35;
    }
    return ds;
  }

  // Сценарий: полная переоценка, если её посчитал расчётный сервис, иначе разложение Тейлора до 2-го порядка:
  //   rates  = Σ key-rate DV01 × Δr;   spread = CS01 × Δs;   fx = FX-дельта × курс × Δfx (+ доля фондирования)
  //   convexity = 2-й порядок по ставкам/спредам (матрица гамм или выпуклость); fxCross = кросс FX × ставки/спреды.
  // Компоненты по позиции включают разнесённую FX-переоценку фондирования (fxFunding);
  // fundingFxByBook — нераспределённый остаток фондирования. Без фондирования: total − fxFunding, без остатка.
  function runScenario(sc, list) {
    list = list || D.positions;
    const stored = storedResult(sc, list);
    if (stored) return stored;
    const curveShock = {};
    Object.keys(CURVES).forEach(c => {
      const v = sc.curves && sc.curves[c];
      curveShock[c] = v == null ? new Array(BUCKETS.length).fill(0) : (Array.isArray(v) ? v : new Array(BUCKETS.length).fill(v));
    });
    const fxShock = sc.fx || {};
    const alloc = sc.fx
      ? allocateFundingFx((ccy, amt) => amt * FX[ccy] * (sc.fx[ccy] || 0) / 100, D.positions)
      : { toPos: {}, residualByBook: {}, byBook: {} };
    const booksInList = new Set(list.map(p => p.book));
    Object.keys(alloc.residualByBook).forEach(b => { if (!booksInList.has(b)) delete alloc.residualByBook[b]; });
    const res = list.map(p => {
      const ratesByCurve = {};
      let rates = 0;
      Object.keys(p.risk || {}).forEach(c => {
        let x = 0;
        if (curveShock[c]) p.risk[c].forEach((dv, i) => { x += dv * curveShock[c][i]; });
        ratesByCurve[c] = x; rates += x;
      });
      const ds = p.cs01 || gammaOf(p) ? spreadShock(sc, p) : 0;
      const spread = p.cs01 ? p.cs01 * ds : 0;
      let fxGross = 0;
      if (sc.fx) Object.keys(p.fxDelta || {}).forEach(c => { fxGross += p.fxDelta[c] * FX[c] * (sc.fx[c] || 0) / 100; });
      const fxFunding = alloc.toPos[p.id] || 0;
      const so = secondOrder(p, curveShock, ds, fxShock, ratesByCurve, spread);
      const fx = fxGross + fxFunding;
      return { id: p.id, rates, spread, fx, fxGross, fxFunding, convexity: so.convexity, fxCross: so.fxCross, secondOrderSrc: so.src,
        dyEff: so.dyEff, total: rates + spread + fx + so.convexity + so.fxCross };
    });
    const fundingFxByBook = alloc.residualByBook;
    const fundFx = sum(Object.values(fundingFxByBook));
    // Разложение 2-го порядка надёжно примерно до ±500 б.п. по доходности позиции (дальше остаток 3-го порядка
    // сравним с поправкой): такие сценарии помечаются — для них нужна полная переоценка (docs/SCENARIOS.md).
    const big = res.filter(r => Math.abs(r.dyEff) > ACCURACY_BP);
    return { byPosition: res, fundingFx: fundFx, fundingFxByBook, fundingFxByDeal: alloc.residualByDeal || {}, total: sum(res, r => r.total) + fundFx, fullReval: false,
      maxDyBp: res.reduce((m, r) => Math.max(m, Math.abs(r.dyEff)), 0),
      accuracyWarning: big.length ? { positions: big.length, mvShare: sum(big, r => Math.abs((byIdPos(r.id) || {}).mvRub || 0)) / (sum(list, p => Math.abs(p.mvRub || 0)) || 1) } : null };
  }

  // Сводные вторые производные (как tdm/assemble.py gamma_summary) — для демо-данных без сервера.
  function gammaSummary(p) {
    let ir = 0, cs = 0, ircs = 0, src = null;
    if (p.gamma && p.gamma.length) {
      p.gamma.forEach(([a, b, v]) => {
        const ta = a.split('|')[0], tb = b.split('|')[0];
        if (ta === 'IR' && tb === 'IR') ir += (a === b ? 1 : 2) * v;
        else if (ta === 'CS' && tb === 'CS') cs += v;
        else if ((ta === 'IR' && tb === 'CS') || (ta === 'CS' && tb === 'IR')) ircs += v;
      });
      src = 'matrix';
    } else if (p.convexity && p.mvRub) {
      ir = p.mvRub * p.convexity * 1e-8; if (p.cs01) { cs = ircs = ir; } src = 'convexity';
    }
    return { gammaIr: ir, gammaCs: cs, gammaIrCs: ircs, gammaSource: src };
  }
  D.positions.forEach(p => { if (p.gammaIr === undefined) Object.assign(p, gammaSummary(p)); });

  D.util = { sum, groupBy, bucketMatrix, netFx, runScenario, curveRate, allocateFundingFx, ACCURACY_BP };
})();
