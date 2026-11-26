/* C18: расчёты по дневным суммам в валюте отчёта, без DOM. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.CARRY_ANALYSIS = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const DAYS = { day: 1, month: 30, year: 365 };
  const BOOK_FUNDING_LABEL = 'Фондирование вне позиций (книги)';
  const BUCKETS = ['< 1Y', '1–3Y', '3–5Y', '5–10Y', '10Y+', 'Без срока'];
  const value = v => Number.isFinite(v) ? v : null;
  const abs = v => value(v) == null ? null : Math.abs(v);
  const money = (v, scale) => value(v) == null ? null : v * DAYS[scale];
  const net = (carry, funding) => value(carry) == null || value(funding) == null ? null : carry + funding;
  const sum = (list, pick) => {
    let total = null;
    for (const row of list) {
      const v = value(pick(row));
      if (v != null) total = (total ?? 0) + v;
    }
    return total;
  };
  const bucketOf = t => value(t) == null ? BUCKETS[5] : BUCKETS[[1, 3, 5, 10].filter(end => t >= end).length];
  const annualPct = (v, base) => v == null || !(base > 0) ? null : v * DAYS.year * 100 / base;
  const bp = pct => pct == null ? null : pct * 100;
  const defaultRate = p => !!(p.fundingInfo?.default_rate || p.fundingInfo?.rule_source === 'default' ||
    p.fundingInfo?.lines?.some(l => l.rule_source === 'default'));

  function rows(positions, { fundingReady = true, fx = {}, isDeal = p => p.group === 'DER' || p.group === 'MM',
    isCds = p => p.kind === 'CDS' || p.instrType === 'CDS' || p.productGroup === 'cds' } = {}) {
    return positions.map(p => {
      const carryDay = value(p.carryDay), fundingDay = fundingReady ? value(p.fundingDay) : null;
      const netDay = net(carryDay, fundingDay);
      const mvAbs = isDeal(p) ? null : abs(p.mv);
      const toNotional = mvAbs == null && abs(p.notionalRub) > 0;
      const relativeBase = toNotional ? abs(p.notionalRub) : mvAbs;
      const tenor = p.event && !p.event.date && p.yearsToMat == null ? null : value(p.tenor);
      const fundingBase = mvAbs > 0 ? mvAbs : sum(p.fundingInfo?.lines || [], l =>
        value(l.rate_pct) != null && fx[l.ccy] > 0 && value(l.body_ccy) != null ? abs(l.body_ccy) * fx[l.ccy] : null);
      return { ...p, carryDay, fundingDay, netDay, mvAbs, relativeBase, toNotional, tenor, fundingBase,
        netBp: bp(annualPct(netDay, relativeBase)), bucket: bucketOf(tenor), defaultFunding: fundingDay != null && defaultRate(p),
        riskKey: p.cpn?.type === 'float' || p.kind === 'FLT' || p.kind === 'PK' || isCds(p) ? 'cs01' : 'dv01' };
    });
  }

  const basis = (p, base) => base === 'gross' ? p.carryDay : p.netDay;
  const hasRisk = (risk, floor) => Number.isFinite(risk) && Math.abs(risk) >= floor && risk !== 0;
  const breakeven = (carry, risk, floor = 1) => carry == null || !hasRisk(risk, floor) ? null : value(carry * DAYS.month / Math.abs(risk));
  const positionBE = (p, base, floor) => breakeven(basis(p, base), p[p.riskKey], floor);
  function riskGroup(list, key, base, floor = 1) {
    const covered = list.filter(p => p.riskKey === key && basis(p, base) != null && hasRisk(p[key], floor));
    const risk = sum(covered, p => p[key]);
    return { risk, be: breakeven(sum(covered, p => basis(p, base)), risk, floor) };
  }

  function weighted(list, pick, weight) {
    const eligible = list.filter(p => value(pick(p)) != null && weight(p) > 0);
    const w = sum(eligible, weight);
    return w > 0 ? sum(eligible, p => pick(p) * weight(p)) / w : null;
  }

  function summarize(list, base = 'net', floor) {
    const full = list.filter(p => p.netDay != null);
    const noFunding = list.filter(p => p.carryDay != null && p.fundingDay == null);
    const withMV = full.filter(p => p.mvAbs > 0);
    const spreadBase = sum(withMV, p => p.mvAbs);
    const carryPct = annualPct(sum(withMV, p => p.carryDay), spreadBase);
    const fundingPct = annualPct(sum(withMV, p => -p.fundingDay), spreadBase);
    const netPct = annualPct(sum(withMV, p => p.netDay), spreadBase);
    return {
      full, noFunding, noCarry: list.filter(p => p.carryDay == null), defaultFunding: list.filter(p => p.defaultFunding),
      gross: sum(list, p => p.carryDay), funding: sum(list, p => p.fundingDay), net: sum(full, p => p.netDay),
      omittedCarry: sum(noFunding, p => p.carryDay), mv: sum(list, p => p.mvAbs),
      carryPct, fundingPct, netPct, netBp: bp(netPct),
      fundRate: weighted(list.filter(p => p.fundingDay != null), p => p.fundRate, p => p.fundingBase),
      ytm: weighted(withMV.filter(p => p.cpn?.type === 'fix'), p => p.ytm, p => p.mvAbs),
      rates: riskGroup(list, 'dv01', base, floor), credit: riskGroup(list, 'cs01', base, floor),
    };
  }

  const fundingReady = funding => funding?.state === 'computed' || funding?.state === 'demo';
  function bookFunding(funding, accrualDays, bookIn) {
    if (!fundingReady(funding) || !(accrualDays > 0)) return null;
    const amount = bookIn
      ? sum(Object.entries(funding.byBook || {}).filter(([book]) => bookIn(book)), ([, v]) => v.bookLevel)
      : value(funding.bookLevel);
    return amount == null ? null : amount / accrualDays;
  }

  function withBook(summary, bookDay, include) {
    const book = include ? bookDay : null;
    return {
      book, net: summary.net == null ? null : summary.net + (book ?? 0),
      funding: summary.funding == null ? book : summary.funding + (book ?? 0),
    };
  }

  const component = (key, label, list, pick) => {
    const rows = list.map(p => ({ p, v: value(pick(p)) })).filter(r => r.v != null);
    return { key, label, rows, day: sum(rows, r => r.v) };
  };
  function carryParts(list) {
    const known = list.filter(p => p.carryDay != null);
    const unsplit = p => {
      const coupon = value(p.carryCouponDay), pull = value(p.carryPullDay);
      if (coupon != null && pull != null) return null;
      const pure = value(p.carryPureDay) ?? (p.carryDay - (value(p.rollDay) ?? 0));
      return pure - (coupon ?? 0) - (pull ?? 0);
    };
    const coupon = component('coupon', 'Купон', known, p => p.carryCouponDay);
    const pull = component('pull', 'Pull-to-par', known, p => p.carryPullDay);
    return [coupon, pull, component('roll', 'Rolldown', known, p => p.rollDay),
      component('unsplit', coupon.day == null && pull.day == null ? 'Кэрри (купон + pull)' : 'Без разбивки', known, unsplit)];
  }

  function waterfall(summary, bookDay, include) {
    if (summary.net == null) return [];
    const list = summary.full, total = withBook(summary, bookDay, include);
    const parts = carryParts(list).filter(c => c.day != null);
    // При целиком отсутствующей разбивке кэрри предшествует роллдауну.
    if (parts[0]?.key === 'roll' && parts[1]?.key === 'unsplit') parts.reverse();
    parts.push(component('funding', 'Фондирование', list, p => p.fundingDay));
    if (total.book != null) parts.push({ key: 'book', label: BOOK_FUNDING_LABEL, day: total.book, rows: [] });
    const final = component('net', 'Нетто-кэрри', list, p => p.netDay);
    let start = 0;
    return parts.map(part => {
      const end = start + part.day;
      const step = { ...part, start, end };
      start = end;
      return step;
    }).concat({ ...final, day: total.net, book: total.book, start: 0, end: total.net });
  }

  const groupKey = (p, dim) => ({
    book: p.bookName || p.book, entity: p.entityName || p.entity, ccy: p.ccy,
    kind: p.kindLabel || p.kind, bucket: p.bucket, issuer: p.issuerName || p.issuer,
  })[dim] || '—';
  const groupId = (p, dim) => p[dim] ?? groupKey(p, dim);
  function groups(list, dim, base, floor) {
    const map = new Map();
    for (const p of list) {
      // Группируем по идентификатору там, где отображаемое имя может совпадать.
      const key = groupId(p, dim);
      if (!map.has(key)) map.set(key, []);
      map.get(key).push(p);
    }
    const group = (rows, label) => ({ label, rows, ...summarize(rows, base, floor) });
    const out = Array.from(map.values(), rows => group(rows, groupKey(rows[0], dim)));
    if (dim === 'bucket') return out.sort((a, b) => BUCKETS.indexOf(a.label) - BUCKETS.indexOf(b.label));
    out.sort((a, b) => (b.net ?? -Infinity) - (a.net ?? -Infinity) || a.label.localeCompare(b.label, 'ru'));
    if (dim === 'issuer' && out.length > 12) {
      out.sort((a, b) => (abs(b.net) ?? -1) - (abs(a.net) ?? -1));
      out.splice(12, out.length - 12, group(out.slice(12).flatMap(g => g.rows), 'Прочие'));
    }
    return out;
  }

  function history(dates, pnl, mode, windowSize, accrualDays) {
    const start = Math.max(0, dates.length - windowSize);
    return dates.slice(start).map((date, offset) => {
      const i = start + offset;
      const div = mode === 'run' ? 1 : value(accrualDays?.[i]);
      const scale = v => div > 0 && value(v) != null ? v / div : null;
      const carry = scale(pnl?.carry?.[i]), funding = scale(pnl?.funding?.[i]);
      // Старые сводки без парной суммы оставляют разрыв линии.
      return { date, carry, funding, net: carry == null || funding == null ? null : scale(pnl.netCarry?.[i]) };
    });
  }

  function leaders(list) {
    const bleeders = list.filter(p => p.netDay < 0).sort((a, b) => a.netDay - b.netDay);
    const generators = list.filter(p => p.netDay > 0).sort((a, b) => b.netDay - a.netDay);
    const mv = sum(list, p => p.mvAbs);
    return {
      bleeders: bleeders.slice(0, 10), generators: generators.slice(0, 10),
      bleedRows: bleeders, generatorCount: generators.length, bleedDay: sum(bleeders, p => p.netDay),
      bleedMvPct: mv > 0 ? (sum(bleeders, p => p.mvAbs) ?? 0) * 100 / mv : null,
    };
  }

  return { BOOK_FUNDING_LABEL, money, net, sum, rows, summarize, breakeven, positionBE, fundingReady, bookFunding, withBook,
    carryParts, waterfall, groupKey, groupId, groups, history, leaders };
});
