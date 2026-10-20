/*
 * DESK.util — функции поверх данных window.DESK (из базы или демо data.js).
 * Подключается сразу после скрипта с данными.
 */
(function () {
  'use strict';
  const D = window.DESK;
  if (!D) { document.title = 'Нет данных'; throw new Error('window.DESK не загружен'); }
  const { BUCKETS, PILLARS, CURVES, FX } = D;

  // ---------- совместимость: данные сервера в форме контракта 1.2 (до docs/ORG_CONVENTIONS.md) ----------
  // Недостающие поля заполняются так, чтобы страницы работали: режим книги по составу позиций, курсы ЦБ = рыночные,
  // фондирование-сделки — строки уровня книги без привязки к позициям.
  (function compat() {
    if (!D.BOOKS) D.BOOKS = {};
    Object.keys(D.BOOKS).forEach(b => {
      const x = D.BOOKS[b];
      if (!x.bookType) x.bookType = D.positions.some(p => p.book === b && p.instrType === 'BOND') ? 'TRADING' : 'DERIVS';
      if (!x.entity) x.entity = '—';
    });
    if (!D.FX_CBR) D.FX_CBR = Object.assign({}, FX);
    if (!D.FX_CBR_MOVE_PCT) D.FX_CBR_MOVE_PCT = Object.assign({}, D.FX_MOVE_PCT || {});
    if (!D.INDICES) D.INDICES = {};
    if (!D.CBR_MEETINGS) D.CBR_MEETINGS = [];
    if (!D.CARRY_SCENARIOS) D.CARRY_SCENARIOS = [{ id: 'flat', name: 'КС без изменений', ks: { mode: 'flat' } }];
    if (!D.CARRY_HORIZONS) D.CARRY_HORIZONS = [['1M', 1], ['3M', 3], ['6M', 6], ['1Y', 12]];
    if (D.accrualDays == null) D.accrualDays = 1;
    if (!D.official) D.official = { asOf: D.asOf, received: D.positions.some(p => p.pnl && p.pnl.official != null), basis: 'CBR' };
    D.positions.forEach(p => {
      if (p.ofzSpr01 == null) p.ofzSpr01 = 0;
      if (!p.entity) p.entity = (D.BOOKS[p.book] || {}).entity || '—';
      const e = p.pnl;
      if (!e || e.available === false) return;   // PnL не посчитан: суммы остаются null, не 0
      if (e.funding == null) e.funding = 0;
      if (e.reported == null) e.reported = 0;   // PnL «по отчёту» (позиции, оценённые по отчёту): отдельной строкой
      if (e.fxBasis == null) e.fxBasis = 0;
      if (e.explainedCbr == null) e.explainedCbr = e.explained + e.fxBasis;
    });
    (D.funding || []).forEach(f => {
      if (!f.kind) f.kind = 'DEAL';
      if (f.positionId == null) f.positionId = null;
      if (f.pnl && f.pnl.inPosition == null) f.pnl.inPosition = false;
      if (f.pnl && f.pnl.fxCbr == null) f.pnl.fxCbr = f.pnl.fx || 0;
    });
    if (D.fundingPnl && D.fundingPnl.fxCbr == null) D.fundingPnl.fxCbr = D.fundingPnl.fx;
  })();

  function sum(arr, f) { return arr.reduce((s, x) => s + (f ? f(x) : x), 0); }
  function groupBy(arr, key) {
    const out = {};
    arr.forEach(x => { const k = typeof key === 'function' ? key(x) : x[key]; (out[k] = out[k] || []).push(x); });
    return out;
  }
  // У кривой есть точки: валютная кривая-заглушка ('USD' и т. п.) несёт только риск, pts — все null.
  function hasPoints(curve) {
    const c = CURVES[curve];
    return !!(c && c.pts && c.pts.some(v => v != null && isFinite(v)));
  }
  // Ставка кривой на сроке t, %; null — у кривой нет точек (или нет точки на нужном отрезке).
  function curveRate(curve, t) {
    if (!hasPoints(curve) || t == null || !isFinite(t)) return null;
    const pts = CURVES[curve].pts, ok = v => v != null && isFinite(v);
    if (t <= PILLARS[0]) return ok(pts[0]) ? pts[0] : null;
    for (let i = 1; i < PILLARS.length; i++) {
      if (t <= PILLARS[i]) {
        if (!ok(pts[i - 1]) || !ok(pts[i])) return null;
        const w = (t - PILLARS[i - 1]) / (PILLARS[i] - PILLARS[i - 1]);
        return pts[i - 1] * (1 - w) + pts[i] * w;
      }
    }
    const last = pts[pts.length - 1];
    return ok(last) ? last : null;
  }
  // ---------- PnL: посчитан ли (R10) ----------
  // У позиции без PnL сервер отдаёт p.pnl.available === false, суммы null и причину (reasonText); демо-данные — всегда с PnL.
  const pnlOk = p => !!p.pnl && p.pnl.available !== false;
  // Покрытие PnL списка позиций: n из m посчитаны; reasons — [текст причины, число позиций] по убыванию;
  // tip — текст подсказки к «—» или к пометке «частично».
  function pnlCover(list) {
    const m = list.length, by = {};
    let n = 0;
    list.forEach(p => {
      if (pnlOk(p)) { n++; return; }
      const t = (p.pnl && p.pnl.reasonText) || D.pnlReasonText || 'PnL не посчитан';
      by[t] = (by[t] || 0) + 1;
    });
    const reasons = Object.keys(by).map(t => [t, by[t]]).sort((a, b) => b[1] - a[1]);
    const head = n ? 'частично: ' + n + ' из ' + m + ' позиций' : 'PnL не посчитан';
    const runWhy = !n && D.pnlAvailable === false && D.pnlReasonText ? D.pnlReasonText : null;
    const tip = runWhy ? 'PnL не посчитан: ' + runWhy
      : head + (reasons.length ? '. Без PnL: ' + reasons.map(r => r[0] + ' — ' + r[1]).join('; ') : '');
    return { n, m, none: m > 0 && n === 0, partial: n > 0 && n < m, reasons, tip };
  }
  // Сумма f по позициям с PnL, значения null пропускаются; null — ни у одной позиции значения нет (не 0).
  function pnlSum(list, f) {
    let s = 0, k = 0;
    list.forEach(p => { if (!pnlOk(p)) return; const v = f(p); if (v != null && isFinite(v)) { s += v; k++; } });
    return k ? s : null;
  }
  // Разметка для страниц (стили .na и .part — на странице): «—» с причиной в подсказке; «частично: N из M позиций».
  const escA = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const plural = (n, one, few, many) => { const a = n % 10, b = n % 100; return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many; };
  const pnlPartText = pc => pc && pc.partial ? 'частично: ' + pc.n + ' из ' + pc.m + ' ' + plural(pc.m, 'позиции', 'позиций', 'позиций') : '';
  const pnlNaHtml = tip => `<span class="na" title="${escA(tip || 'PnL не посчитан')}">—</span>`;
  const pnlPartHtml = pc => pc && pc.partial ? `<span class="part" title="${escA(pc.tip)}">${pnlPartText(pc)}</span>` : '';
  // Рейтинг: «нет данных» (в хабе рейтингов нет) и «без рейтинга» (рейтинги есть, у бумаги нет) — не грейды, а одна
  // корзина «без рейтинга»; её ключ в сценариях — NO_RATING.
  const NO_RATING = 'нет данных', NO_RATING_LABEL = 'без рейтинга';
  const isRated = r => !!r && r !== NO_RATING && r !== NO_RATING_LABEL;
  const ratingLabel = r => isRated(r) ? r : NO_RATING_LABEL;
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
  // FX-переоценка тела фондирования: строка, привязанная к бумаге (positionId), — в PnL этой бумаги
  // (облигация торговой книги фондируется в своей валюте, поэтому FX гасится внутри позиции);
  // остальные строки (остатки ДС деривативов, РЕПО, сделки старого формата) — на уровне книги.
  function fundingFx(shockFn) {
    const toPos = {}, byBook = {}, residualByBook = {}, residualByDeal = {};
    (D.funding || []).forEach(f => {
      Object.keys(f.fxDelta || {}).forEach(c => {
        const x = shockFn(c, f.fxDelta[c]);
        if (!x) return;
        byBook[f.book] = (byBook[f.book] || 0) + x;
        if (f.positionId != null) toPos[f.positionId] = (toPos[f.positionId] || 0) + x;
        else { residualByBook[f.book] = (residualByBook[f.book] || 0) + x; residualByDeal[f.id] = (residualByDeal[f.id] || 0) + x; }
      });
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
      const dsEff = ownSpr01(p) ? ds : 0;
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
    const dyEff = effRateMove(p, curveShock) + (ownSpr01(p) ? ds : 0);
    return { convexity: conv, fxCross: cross, src, dyEff };
  }

  // Полная переоценка из расчётного сервиса (scenario_result) — только для неизменённых пресетов.
  const shockKey = sc => JSON.stringify([sc.curves || null, sc.fx || null, sc.spreads || null, sc.ofzSpread || 0]);
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

  // Собственный спред позиции: кредитный (CS01, у RUB-корпоратов — к RUONIA) или спред ОФЗ к RUONIA (ofzSpr01).
  // Это фактор 'CS' матрицы гамм.
  const ownSpr01 = p => p.cs01 || p.ofzSpr01 || 0;
  // Кредитный спред-шок позиции, б.п. Правила аддитивны: _all + rating[r] + sector[s] + issuer[i] + (_mult − 1) × спред.
  // Совместимость: рейтинг или эмитент ключом верхнего уровня ({AA: 50, 'Самолёт': 300}); includeGov игнорируется.
  function spreadShock(sc, p) {
    const sp = sc.spreads;
    if (!sp) return 0;
    let ds = 0;
    if (sp._mult) ds += (sp._mult - 1) * (p.spreadBp || 100);
    if (sp._all) ds += sp._all;
    const pick = (grp, k) => k != null && ((sp[grp] && sp[grp][k]) || 0);
    ds += pick('rating', p.rating && !isRated(p.rating) ? NO_RATING : p.rating) + pick('sector', p.sector) + pick('issuer', p.issuer);
    if (p.rating && typeof sp[p.rating] === 'number') ds += sp[p.rating];
    if (p.issuer && typeof sp[p.issuer] === 'number') ds += sp[p.issuer];
    return ds;
  }
  // Сдвиг собственного спреда позиции в сценарии: ОФЗ — спред ОФЗ к RUONIA, остальные — кредитный.
  function ownSpreadShock(sc, p) {
    if (p.cs01) return spreadShock(sc, p);
    if (p.ofzSpr01) return sc.ofzSpread || 0;
    return gammaOf(p) ? spreadShock(sc, p) : 0;
  }

  // Сценарий: полная переоценка, если её посчитал расчётный сервис, иначе разложение Тейлора до 2-го порядка:
  //   rates  = Σ key-rate DV01 × Δr;   spread = CS01 × Δs (ОФЗ: ofzSpr01 × ofzSpread);
  //   fx = FX-дельта × курс × Δfx (+ переоценка тела фондирования самой бумаги)
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
      ? fundingFx((ccy, amt) => amt * FX[ccy] * (sc.fx[ccy] || 0) / 100)
      : { toPos: {}, residualByBook: {}, byBook: {}, residualByDeal: {} };
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
      const ds = ownSpreadShock(sc, p);
      const spread = ownSpr01(p) * ds;
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

  // ---------- облигации: спред к выбранной кривой, агрегаты по выпуску, история ----------
  // Базовая кривая валюты: гос. кривая ('bond'), иначе первая кривая этой валюты.
  function baseCurve(ccy) {
    const ids = Object.keys(CURVES).filter(c => CURVES[c].ccy === ccy);
    return ids.find(c => CURVES[c].kind === 'bond') || ids[0] || null;
  }
  // Срок, на котором берётся ставка кривой: WAL (ИЦБ), иначе до даты доходности (оферта), иначе до погашения.
  function yieldTenor(p) {
    if (p.wal) return p.wal;
    if (p.yieldDate) { const t = (new Date(p.yieldDate + 'T00:00:00') - new Date(D.asOf + 'T00:00:00')) / (365.25 * 864e5); if (t > 0) return t; }
    return p.tenorY;
  }
  function curveRatePrev(curve, t) {   // кривая вчера = сегодня − изменение за день
    const mv = D.CURVE_MOVES && D.CURVE_MOVES[curve];
    if (!mv || !hasPoints(curve)) return null;
    const saved = CURVES[curve].pts;
    try { CURVES[curve].pts = saved.map((v, i) => v == null ? null : v - (mv[i] || 0) / 100); return curveRate(curve, t); } finally { CURVES[curve].pts = saved; }
  }
  // Спред доходности облигации к кривой, б.п. curve: 'own' — расчётный спред (p.spreadBp, тип p.spreadType),
  // 'base' — базовая кривая валюты бумаги, иначе id кривой. null — не применимо (флоатер, другая валюта, нет доходности).
  function spreadTo(p, curve, prev) {
    if (p.instrType !== 'BOND') return null;
    if (curve === 'own') return prev ? (p.quote && p.quote.prevZSpread != null ? p.quote.prevZSpread : null) : p.spreadBp;
    if (p.couponType === 'float') return null;
    const c = curve === 'base' ? baseCurve(p.ccy) : curve;
    if (!c || !CURVES[c] || CURVES[c].ccy !== p.ccy) return null;
    const t = yieldTenor(p);
    if (prev) {
      const y = p.quote && p.quote.prevYtm, r = curveRatePrev(c, t + 1 / 365.25);
      return y == null || r == null ? null : (y - r) * 100;
    }
    const r = curveRate(c, t);   // null у кривой без точек: спред не «доходность − 0»
    return p.ytm == null || r == null ? null : (p.ytm - r) * 100;
  }
  // Купон позиции (R16) для карточки, таблицы облигаций и истории выпуска: { text, title }. Значение — p.coupon
  // (% годовых), основание — p.extra.coupon_basis (rate / amount / projected / assumed / zero), у флоатера индекс и маржа —
  // p.index, p.margin. Прогноз «индекс + маржа» помечен «≈» и «прогноз»; неизвестный купон — «—». Прогон до правки D4
  // (без индекса и маржи) и любые null / NaN / строка "null" не попадают в текст.
  const pct2 = v => new Intl.NumberFormat('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(v).replace('-', '−') + '%';
  const finite = v => v != null && v !== '' && typeof v !== 'boolean' && isFinite(+v) ? +v : null;
  const label = v => typeof v === 'string' && v.trim() && !/^(null|undefined|nan|none)$/i.test(v.trim()) ? v.trim() : null;
  const CPN_BASIS = { rate: 'ставка текущего купона', amount: 'сумма текущего купона, пересчитанная в ставку',
    assumed: 'последняя известная ставка: текущий купон не опубликован' };
  function couponView(p) {
    const c = finite(p.coupon), basis = p.extra && p.extra.coupon_basis, proj = basis === 'projected' && c != null;
    if (p.couponType === 'zero') return { text: 'бескупонная', title: 'дисконтная бумага: купона нет' };
    const v = c == null ? null : (proj ? '≈' : '') + pct2(c);
    if (p.couponType === 'float') {
      const idx = label(p.index), m = finite(p.margin);
      const formula = (idx || 'плав.') + (m == null ? '' : (m < 0 ? ' − ' : ' + ') + pct2(Math.abs(m)));
      const text = v == null ? formula : formula + ' (' + v + (proj ? ', прогноз' : '') + ')';
      const title = proj ? 'прогноз: ' + formula + ' по последнему фиксингу индекса'
        : v == null ? 'плавающий купон: текущая ставка неизвестна' + (idx ? '' : ', индекс не указан')
        : CPN_BASIS[basis] || 'плавающий купон';
      return { text, title };
    }
    if (v == null) return { text: '—', title: 'купон неизвестен' };
    return { text: v, title: CPN_BASIS[basis] || 'купон, % годовых' };
  }
  const couponText = p => couponView(p).text;
  // Строки таблицы облигаций: позиции BOND, объединённые по выпуску (ISIN) или по (ключ группы, ISIN).
  // Ключ выпуска: ISIN, у бумаги без него — instrumentId (код источника), иначе id позиции.
  const bondKey = p => p.isin || p.instrumentId || String(p.id);
  function bondRows(list, keyFn) {
    const by = {};
    list.filter(p => p.instrType === 'BOND').forEach(p => {
      const k = (keyFn ? keyFn(p) + '|' : '') + bondKey(p);
      (by[k] = by[k] || []).push(p);
    });
    return Object.keys(by).map(k => {
      const ps = by[k], p = ps[0];
      return { key: k, p, positions: ps, books: [...new Set(ps.map(x => x.book))],
        notional: sum(ps, x => x.notional), mvRub: sum(ps, x => x.mvRub), dv01: sum(ps, x => x.dv01), cs01: sum(ps, x => x.cs01),
        ofzSpr01: sum(ps, x => x.ofzSpr01 || 0), gammaIr: sum(ps, x => x.gammaIr || 0),
        pnl: ps.some(pnlOk) ? sum(ps.filter(pnlOk), x => x.pnl.explained) : null, pnlCover: pnlCover(ps),
        newToday: ps.some(x => x.newToday) };
    });
  }
  // Средневзвешенное (веса по |MV|), пропуская null.
  function wavg(list, val, w) {
    let n = 0, d = 0;
    list.forEach(x => { const v = val(x), ww = Math.abs(w ? w(x) : x.mvRub); if (v != null && isFinite(v) && ww) { n += v * ww; d += ww; } });
    return d ? n / d : null;
  }
  // История выпуска за период: демо — синтетика из data.js, база — /api/v1/bonds/history.
  // Ответ нормализуется в столбцы: dates, price, ytm, z, g, accrued, volume, face, tenor, curveRate, curveSpread.
  function normHistory(h) {
    const s = h.series || [], col = k => s.map(r => r[k] == null ? null : r[k]);
    return { instrumentId: h.instrument_id, isin: h.isin, curve: h.curve, note: h.spread_to_curve_note || null, error: h.error || null,
      dates: col('date'), price: col('clean_price'), ytm: col('ytm'), z: col('z_spread_bp'), g: col('g_spread_bp'), accrued: col('accrued'),
      volume: col('volume_rub'), face: col('face_amount'), tenor: col('tenor_y'), curveRate: col('curve_rate'), curveSpread: col('spread_to_curve_bp') };
  }
  const histCache = {};
  function bondHistory(p, curve, from) {
    const c = curve === 'own' || !curve ? p.spreadCurve : curve === 'base' ? baseCurve(p.ccy) : curve;
    const key = [bondKey(p), c, from || ''].join('|');
    if (histCache[key]) return histCache[key];
    let pr;
    if (D.demo && D.demo.bondHistory) pr = Promise.resolve(D.demo.bondHistory(p, c));
    else {
      const q = new URLSearchParams();
      if (p.instrumentId) q.set('instrument_id', p.instrumentId); else q.set('isin', p.isin || '');
      if (c) q.set('curve', c);
      if (p.wal) q.set('wal', p.wal);
      if (from) q.set('from', from);
      if (D.asOf) q.set('to', D.asOf);
      pr = fetch('/api/v1/bonds/history?' + q).then(r => r.json().then(j => (r.ok ? j : Object.assign({ series: [] }, j, { error: j.error || j.message || ('HTTP ' + r.status) }))))
        .catch(e => ({ series: [], error: 'нет связи с сервером: ' + e.message }));
    }
    return (histCache[key] = pr.then(normHistory));
  }

  // ================== Кэрри и фондирование на горизонте ==================
  // Позиции постоянны (MV и остатки ДС не меняются); бумага выбывает в дату оферты / погашения (yieldDate, иначе maturity):
  // после неё нет ни дохода, ни фондирования (денежные потоки уходят казначейству). Шаг — календарный день,
  // начисление дня t идёт по ставкам, действующим в день t (выходные — по ставке пятницы).
  //   income  — доход облигаций по доходности: грязная MV × y(t) / 365; у флоатеров y следует за своим индексом;
  //             HTM — по доходности покупки на цену покупки;
  //   funding — фондирование облигаций: тело (чистая MV, HTM — цена покупки) × ставка(t) / 365, процент в RUB по курсу ЦБ;
  //   derivs  — кэрри деривативов: IRS (плавающий индекс(t) − фикс), CDS (running-купон), XCCY (сегодняшний кэрри, постоянно);
  //   cash    — остатки ДС книг деривативов и РЕПО: остаток × O/N(t) / база;
  //   rolldown (по желанию) — сдвиг по кривой к горизонту: DV01 × (r(T − h) − r(T)), б.п.
  // Индексы: КС — траектория сценария; RUONIA и RUSFAR O/N — КС + сегодняшний базис + сдвиг; прочие — сегодня + сдвиг.
  const isoD = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00'); d.setDate(d.getDate() + n); return isoD(d); };
  function monthsAdd(iso, m) {
    const d = new Date(iso + 'T00:00:00'), day = d.getDate();
    d.setDate(1); d.setMonth(d.getMonth() + m);
    d.setDate(Math.min(day, new Date(d.getFullYear(), d.getMonth() + 1, 0).getDate()));
    return isoD(d);
  }
  const daysBetween = (a, b) => Math.round((new Date(b + 'T00:00:00') - new Date(a + 'T00:00:00')) / 864e5);
  // конец горизонта: '1M' / '3M' / '6M' / '1Y' / число месяцев / дата YYYY-MM-DD
  function carryEnd(h) {
    if (typeof h === 'number') return monthsAdd(D.asOf, h);
    if (/^\d{4}-\d{2}-\d{2}$/.test(h)) return h;
    const m = /^(\d+)([MY])$/.exec(h || '3M');
    return monthsAdd(D.asOf, m ? (+m[1]) * (m[2] === 'Y' ? 12 : 1) : 3);
  }
  const KS0 = () => D.INDICES['КС'] != null ? D.INDICES['КС'] : 0;
  // Траектория КС: [{date — с какого дня действует, ks — %, meeting — дата решения}]
  function ksPath(cfg, end) {
    const k = (cfg && cfg.ks) || { mode: 'flat' }, ks0 = KS0();
    const out = [{ date: D.asOf, ks: ks0, meeting: null }];
    if (k.mode === 'path' && k.path) {
      k.path.filter(x => x.date > D.asOf).sort((a, b) => a.date < b.date ? -1 : 1).forEach(x => out.push({ date: x.date, ks: x.ks, meeting: x.meeting || null }));
      return out;
    }
    const ms = (D.CBR_MEETINGS || []).filter(m => m.effective > D.asOf && (!end || m.effective <= end));
    let cur = ks0;
    ms.forEach((m, i) => {
      if (k.mode === 'steps') cur = Math.max(0, cur + ((k.steps && k.steps[i]) || 0) / 100);
      else if (k.mode === 'implied' && CURVES['RUB KEY']) {
        // средний форвард кривой RUB KEY до следующего заседания (до +3 мес., если заседаний дальше нет), шаг 0.25 п.п.
        const t1 = daysBetween(D.asOf, m.effective) / 365.25;
        const nx = ms[i + 1] ? daysBetween(D.asOf, ms[i + 1].effective) / 365.25 : t1 + 0.25;
        const r1 = curveRate('RUB KEY', t1), r2 = curveRate('RUB KEY', nx);
        if (r1 != null && r2 != null) cur = Math.round(((r2 * nx - r1 * t1) / (nx - t1)) * 4) / 4;
      }
      if (k.mode !== 'flat') out.push({ date: m.effective, ks: cur, meeting: m.date });
    });
    return out;
  }
  // значение индекса на дату при траектории КС (path) и сдвигах cfg.on, %
  function indexAt(name, iso, path, cfg) {
    const on = ((cfg && cfg.on) || {})[name] || 0;
    let ks = path[0].ks;
    for (let i = 1; i < path.length && path[i].date <= iso; i++) ks = path[i].ks;
    if (name === 'КС') return ks + on / 100;
    const today = D.INDICES[name] != null ? D.INDICES[name] : null;
    if (name === 'RUONIA' || name === 'RUSFAR O/N') return ks + ((today != null ? today : KS0()) - KS0()) + on / 100;
    return (today != null ? today : 0) + on / 100;
  }
  const HARD = { USD: 1, EUR: 1, CHF: 1 };
  function lineRate(f, iso, path, cfg) {
    if (f.kind === 'HTM' || f.fixed != null) {
      if (f.kind === 'POS' && HARD[f.ccy] && cfg.hardCcy != null) return cfg.hardCcy;
      return f.fixed != null ? f.fixed : f.rate;
    }
    if (!f.index) return f.rate;                       // старый формат: ставка как есть
    const sh = f.kind === 'POS' ? (((cfg.fundSpread || {})[f.ccy]) || 0) : 0;
    return indexAt(f.index, iso, path, cfg) + ((f.spreadBp || 0) + sh) / 100;
  }
  function runCarry(cfg, opts) {
    cfg = cfg || {}; opts = opts || {};
    const list = opts.positions || D.positions;
    const books = new Set(list.map(p => p.book));
    const end = carryEnd(opts.horizon || cfg.horizon || '3M');
    const N = Math.max(1, daysBetween(D.asOf, end));
    const path = ksPath(cfg, end);
    const days = []; for (let k = 0; k < N; k++) days.push(addDays(D.asOf, k));
    const memo = {};
    const idxSeries = name => memo[name] || (memo[name] = days.map(d => indexAt(name, d, path, cfg)));
    const FXC = D.FX_CBR || FX;
    // контрольные точки: опции (даты) + конец; накопленные суммы по компонентам
    const marks = (opts.marks || []).map(carryEnd).filter(d => d > D.asOf && d <= end);
    if (!marks.includes(end)) marks.push(end);
    marks.sort();
    const markIdx = marks.map(d => daysBetween(D.asOf, d));   // число дней до точки
    const step = opts.step || 7;
    const sampleIdx = []; for (let k = step; k < N; k += step) sampleIdx.push(k); sampleIdx.push(N);
    const KEYS = ['income', 'funding', 'derivs', 'cash'];
    const daily = {}; KEYS.forEach(k => { daily[k] = new Float64Array(N); });
    const byPos = {}, byBook = {};
    const blank = () => ({ income: 0, funding: 0, derivs: 0, cash: 0, rolldown: 0, net: 0 });
    books.forEach(b => { byBook[b] = blank(); });
    const add = (k, b, id, arr) => {
      let tot = 0;
      for (let i = 0; i < N; i++) { daily[k][i] += arr[i]; tot += arr[i]; }
      byBook[b][k] += tot;
      if (id != null) { const r = byPos[id] || (byPos[id] = blank()); r[k] += tot; }
    };
    const until = p => p.yieldDate && p.yieldDate > D.asOf ? p.yieldDate : p.maturity;
    const aliveN = p => { const u = until(p); return u ? Math.max(0, Math.min(N, daysBetween(D.asOf, u))) : N; };
    const fundOf = {}; (D.funding || []).forEach(f => { if (f.positionId != null) fundOf[f.positionId] = f; });
    const matures = [];
    list.forEach(p => {
      const n = aliveN(p), arr = new Float64Array(N);
      if (n < N && p.instrType !== 'FX') matures.push({ id: p.id, date: until(p) });
      if (p.instrType === 'BOND') {
        if (p.accrualOnly) {
          const v = p.notional * (p.purchasePrice || p.price) / 100 * FX[p.ccy] * (p.purchaseYield || p.ytm) / 100 / 365;
          for (let i = 0; i < n; i++) arr[i] = v;
        } else {
          const fl = p.couponType === 'float' && p.index && D.INDICES[p.index] != null ? idxSeries(p.index) : null;
          for (let i = 0; i < n; i++) arr[i] = p.mvRub * (p.ytm + (fl ? fl[i] - D.INDICES[p.index] : 0)) / 100 / 365;
        }
        add('income', p.book, p.id, arr);
        const f = fundOf[p.id];
        if (f) {
          const fa = new Float64Array(N), conv = FXC[f.ccy] || FX[f.ccy];
          for (let i = 0; i < n; i++) fa[i] = f.amount * conv * lineRate(f, days[i], path, cfg) / 100 / (f.dayBasis || 365);
          add('funding', p.book, p.id, fa);
        }
      } else if (p.instrType === 'IRS') {
        const sgn = p.side === 'PAY' ? 1 : -1, ix = p.index && (D.INDICES[p.index] != null) ? idxSeries(p.index) : null;
        for (let i = 0; i < n; i++) arr[i] = sgn * p.notional * FX[p.ccy] * ((ix ? ix[i] : p.coupon) - p.coupon) / 100 / 365;
        add('derivs', p.book, p.id, arr);
      } else if (p.instrType === 'CDS') {
        const v = (p.side === 'BUY' ? -1 : 1) * p.notional * FX[p.ccy] * (p.coupon || 0) / 100 / 365;
        for (let i = 0; i < n; i++) arr[i] = v;
        add('derivs', p.book, p.id, arr);
      } else if (p.instrType === 'XCCY') {
        const v = p.pnl ? p.pnl.carry / (D.accrualDays || 1) : 0;
        for (let i = 0; i < n; i++) arr[i] = v;
        add('derivs', p.book, p.id, arr);
      }
      // rolldown к горизонту (по желанию): фикс. облигации и IRS, пока живы
      if (cfg.rolldown && n === N && !p.accrualOnly && ((p.instrType === 'BOND' && p.couponType && p.couponType !== 'float') || p.instrType === 'IRS')) {
        const h = N / 365.25, T = p.instrType === 'BOND' ? yieldTenor(p) : p.tenorY;
        let rd = 0;
        Object.keys(p.risk || {}).forEach(c => {
          if (!hasPoints(c)) return;   // кривая без точек (валютная заглушка): ролл-дауна не считаем
          const dv = p.risk[c].reduce((a, b) => a + b, 0);
          rd += dv * (curveRate(c, Math.max(0, T - h)) - curveRate(c, T)) * 100;
        });
        byBook[p.book].rolldown += rd;
        (byPos[p.id] || (byPos[p.id] = blank())).rolldown += rd;
      }
    });
    // строки фондирования уровня книги: остатки ДС, РЕПО, сделки старого формата
    (D.funding || []).forEach(f => {
      if (f.positionId != null || !books.has(f.book)) return;
      const arr = new Float64Array(N), conv = FX[f.ccy];
      const n = f.kind === 'CASH' || f.kind === 'REPO' ? N : Math.min(N, Math.max(0, daysBetween(D.asOf, f.maturity)));
      for (let i = 0; i < n; i++) arr[i] = f.amount * conv * lineRate(f, days[i], path, cfg) / 100 / (f.dayBasis || 365);
      add('cash', f.book, null, arr);
    });
    Object.values(byBook).forEach(r => { r.net = r.income + r.funding + r.derivs + r.cash + r.rolldown; });
    Object.values(byPos).forEach(r => { r.net = r.income + r.funding + r.derivs + r.cash + r.rolldown; });
    const total = blank();
    Object.values(byBook).forEach(r => Object.keys(total).forEach(k => { total[k] += r[k]; }));
    const cum = idx => { const o = {}; KEYS.forEach(k => { let s = 0; for (let i = 0; i < idx; i++) s += daily[k][i]; o[k] = s; }); o.rolldown = total.rolldown * idx / N; o.net = o.income + o.funding + o.derivs + o.cash + o.rolldown; return o; };
    const series = sampleIdx.map(k => Object.assign({ date: addDays(D.asOf, k), day: k }, cum(k)));
    const atMarks = marks.map((d, j) => Object.assign({ date: d, day: markIdx[j] }, cum(markIdx[j])));
    // средние ставки фондирования облигаций по валютам за горизонт (взвешенно по телу)
    const avgRate = {};
    (D.funding || []).filter(f => f.kind === 'POS' && books.has(f.book)).forEach(f => {
      const a = avgRate[f.ccy] || (avgRate[f.ccy] = { w: 0, x: 0 });
      const n = Math.min(N, Math.max(1, daysBetween(D.asOf, until(list.find(p => p.id === f.positionId) || {}) || end)));
      let r = 0; for (let i = 0; i < n; i++) r += lineRate(f, days[i], path, cfg);
      a.w += Math.abs(f.amountRub) * n; a.x += Math.abs(f.amountRub) * r;
    });
    Object.keys(avgRate).forEach(c => { avgRate[c] = avgRate[c].w ? avgRate[c].x / avgRate[c].w : null; });
    return { end, days: N, path, total, byBook, byPosition: byPos, series, marks: atMarks, matures, avgFundingRate: avgRate,
      ksAvg: idxSeries('КС').reduce((a, b) => a + b, 0) / N, ksEnd: idxSeries('КС')[N - 1] };
  }

  D.util = { sum, groupBy, bucketMatrix, netFx, runScenario, curveRate, hasPoints, pnlOk, pnlCover, pnlSum, pnlPartText, pnlNaHtml, pnlPartHtml,
    NO_RATING, NO_RATING_LABEL, isRated, ratingLabel, fundingFx, spreadShock, ownSpr01, ACCURACY_BP,
    runCarry, ksPath, carryEnd, indexAt, addDays, monthsAdd,
    baseCurve, yieldTenor, spreadTo, bondKey, bondRows, wavg, bondHistory, normHistory, couponView, couponText };
})();
