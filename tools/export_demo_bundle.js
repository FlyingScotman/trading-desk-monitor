#!/usr/bin/env node
/*
 * Выгружает демо-набор prototypes/data.js в JSON-пакет контракта (docs/DATA_CONTRACT.md).
 *   node tools/export_demo_bundle.js > fixtures/demo_bundle.json
 *   node tools/export_demo_bundle.js --desk   # сырой window.DESK (для сверки)
 *   node tools/export_demo_bundle.js --market fixtures/demo_market.json.gz
 *        # рыночная история за год для облигаций (md_curve_point, md_bond_quote, ref_instrument, pos_snapshot);
 *        # загружается командой python -m tdm demo
 */
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), zlib = require('zlib');
const src = fs.readFileSync(path.join(__dirname, '..', 'prototypes', 'data.js'), 'utf8');
const ctx = { window: {} }; vm.createContext(ctx); vm.runInContext(src, ctx);
const D = JSON.parse(JSON.stringify(ctx.window.DESK));
if (process.argv.includes('--desk')) { process.stdout.write(JSON.stringify(D)); process.exit(0); }
const pid = p => 'P' + String(p.id).padStart(4, '0');
const mi = process.argv.indexOf('--market');
if (mi > 0) {
  // компактно: столбцы по датам; python -m tdm demo разворачивает в строки таблиц
  const H = ctx.window.DESK.demo, bonds = ctx.window.DESK.positions.filter(p => p.instrType === 'BOND');
  const col = (rows, k) => rows.map(r => r[k]);
  const market = {
    as_of: D.asOf, dates: H.histDates, pillars: D.PILLARS,
    curves: Object.fromEntries(Object.keys(D.CURVES).map(c => [c, H.curveHist[c]])),
    instruments: bonds.map(p => ({ instrument_id: p.instrumentId, isin: p.isin, name: p.name, issuer_id: p.issuer, asset_class: p.assetClass,
      segment: p.segment, ccy: p.ccy, face_value: 1000, maturity: p.maturity,
      coupon_type: p.couponType, coupon_rate: p.coupon, coupon_freq: p.couponFreq, index_name: p.index, margin: p.margin,
      offer_date: p.yieldDate, is_mbs: p.wal ? 1 : 0 })),
    quotes: Object.fromEntries(bonds.map(p => { const q = H.bondQuotes(p); return [p.instrumentId, {
      book_id: p.book, position_id: pid(p), clean_price: col(q, 'clean_price'), ytm: col(q, 'ytm'), z_spread_bp: col(q, 'z_spread_bp'),
      g_spread_bp: col(q, 'g_spread_bp'), accrued: col(q, 'accrued'), volume_rub: col(q, 'volume_rub'), face_amount: col(q, 'face_amount') }]; })),
  };
  fs.writeFileSync(process.argv[mi + 1], zlib.gzipSync(Buffer.from(JSON.stringify(market)), { level: 9 }));
  process.exit(0);
}

const B = D.BUCKETS;
const byBucket = arr => { const o = {}; arr.forEach((v, i) => { if (v) o[B[i]] = v; }); return o; };
const fid = f => 'F' + String(f.id).padStart(4, '0');

const bundle = {
  contract_version: '1.2',
  run: { as_of: D.asOf, run_type: 'EOD', source: 'demo (prototypes/data.js)', lib_version: null, market_snapshot: 'demo', comment: 'Демо-набор: все цифры вымышленные' },
  buckets: B.map((b, i) => ({ bucket: b, pillar_years: D.PILLARS[i] })),
  ratings: D.RATING_ORDER,
  books: Object.entries(D.BOOKS).map(([k, b]) => ({ book_id: k, name: b.name, desk: b.desk, trader: b.trader })),
  issuers: Object.entries(D.ISSUERS).map(([k, x]) => ({ issuer: k, issuer_group: x.group, sector: x.sector, rating: x.rating, is_gov: !!x.gov, is_quasi: !!x.quasi })),
  curves: Object.entries(D.CURVES).map(([k, c]) => ({ curve_id: k, name: c.name, ccy: c.ccy, kind: c.kind,
    points: B.map((b, i) => ({ bucket: b, rate: c.pts[i], move_bp: D.CURVE_MOVES[k][i] })) })),
  fx: Object.keys(D.FX).map(c => ({ ccy: c, rate: D.FX[c], move_pct: D.FX_MOVE_PCT[c] || 0 })),
  indices: Object.entries(D.INDICES).map(([k, v]) => ({ index_name: k, value: v })),
  scenarios: D.SCENARIOS.map(s => { const { id, name, ...def } = s; return { scenario_id: id, name, definition: def }; }),
  positions: D.positions.map(p => ({
    position_id: pid(p), book_id: p.book, instr_type: p.instrType, asset_class: p.assetClass, segment: p.segment,
    name: p.name, isin: p.isin || null, issuer: p.issuer || null, ccy: p.ccy, maturity: p.maturity, tenor_y: p.tenorY,
    coupon_type: p.couponType || null, coupon: p.coupon, index_name: p.index || null, margin: p.margin == null ? null : p.margin,
    side: p.side || null, notional: p.notional, notional_rub: p.notionalRub == null ? null : p.notionalRub,
    price: p.price, mv: p.mv, mv_rub: p.mvRub, ytm: p.ytm, spread_bp: p.spreadBp, mdur: p.mdur,
    spr_dur: p.sprDur == null ? null : p.sprDur, convexity: p.convexity == null ? null : p.convexity,
    dv01: p.dv01, cs01: p.cs01, jtd: p.jtd, wal: p.wal == null ? null : p.wal, cpr: p.cpr == null ? null : p.cpr,
    counterparty: p.counterparty || null, new_today: !!p.newToday,
    ...(p.instrType === 'BOND' ? { instrument_id: p.instrumentId, accrued: p.accrued, coupon_freq: p.couponFreq, yield_date: p.yieldDate,
      spread_type: p.spreadType, spread_curve: p.spreadCurve } : {}),
    risk: Object.fromEntries(Object.entries(p.risk).map(([c, a]) => [c, byBucket(a)])),
    fx_delta: p.fxDelta,
    gamma: p.gamma || [],
    pnl: { carry: p.pnl.carry, rolldown: p.pnl.rolldown, rates: p.pnl.rates, spread: p.pnl.spread, fx_gross: p.pnl.fxGross,
      fx_funding: p.pnl.fxFunding, convexity: p.pnl.convexity, new_trades: p.pnl.newTrades, other: p.pnl.other,
      official: p.pnl.official, break_note: p.pnl.breakNote },
  })),
  funding: D.funding.map(f => ({
    deal_id: fid(f), book_id: f.book, type: f.type, ccy: f.ccy, amount: f.amount, rate_expr: f.rateExpr, rate: f.rate,
    start_date: f.start, maturity: f.maturity, counterparty: f.counterparty, collateral: f.collateral,
    pnl: { carry: f.pnl.carry, fx_gross: f.pnl.fxGross, fx_allocated: f.pnl.fxAllocated },
  })),
  history: D.history.slice(0, -1).map(h => ({ as_of: h.date, pnl_official: h.pnlOfficial, pnl_explained: h.pnlExplained, dv01: h.dv01, cs01: h.cs01 })),
};
process.stdout.write(JSON.stringify(bundle, null, 1) + '\n');
