/*
 * Тип инструмента и таблица агрегированного риска «валюта × тип инструмента» (UI-02).
 * Зеркало api/concepts.py (INSTR_KINDS, PRODUCT_KIND, LEGACY_KIND, instr_kind, risk_table): правка там — правка здесь.
 *
 * Классификация — одна таблица соответствий (INSTR.KINDS, порядок строк таблицы):
 *  - облигации — по типу эмитента и купону, как api/concepts.kind_of (ОФЗ-ПД / -ПК / -ИН, суверенные валютные — гос.
 *    бумага не в рублях), плюс класс выпуска статики: субфедеральные / муниципальные, секьюритизация (ИЦБ, ABS);
 *    облигация без класса (не сопоставлена с MOEX, не оценена) — «Облигации без класса»;
 *  - деривативы и FX — по группе продукта отчёта (extra.reported.x_product_group адаптера: irs, ccs, cds, trs,
 *    bond_future, bond_forward, fx_future, fx_ndf, equity), иначе по типу старого tdm (IRS, XCCY, CDS), в демо-наборе —
 *    по его типу (IRS / КС-своп / базис → IRS, FX форвард, XCCY → CCS); FX-строка без группы — «Валюта», перенос ОВП —
 *    свой тип; чего таблица не знает — «Не классифицировано» (видно, не выброшено).
 * Отнесение к валюте (INSTR.ATTRIBUTIONS) — для многовалютных типов (INSTR.CROSS_CCY: CCS, FX форварды, FX фьючерсы, FX):
 *  - «по ногам» (legs, по умолчанию, ПРЕДВАРИТЕЛЬНО — решает владелец): каждая мера в своей валюте — DV01 по кривой в
 *    валюте кривой, FX-дельта в своей валюте; позиция, номинал, MV, CS01 и базис ОФЗ — в валюте позиции. Тогда риск
 *    валюты в таблице равен риску по её кривым в кокпите (блоки «риск по бакетам»);
 *  - «по валюте сделки» (position): всё в валюте позиции (CCY отчёта); риск на кривых другой валюты — отдельной
 *    колонкой «DV01 чужих кривых», в DV01 блока не входит.
 *  Одновалютные типы всегда «по ногам» (своя кривая — своя валюта, R27: никакая валюта не попадает в рубли).
 *  Правило «по ногам» для одной позиции (INSTR.dv01By / dv01In, fxBy / fxIn, curveCcy) — общее для всех мониторов: шапка
 *  (DV01 и ОВП по валютам), блоки валют кокпита, ОВП C06, история по валютам C11, карточка; поэтому lib/instr.js
 *  загружается на каждой странице сразу после kit.js.
 * Чего нет — то нет: позиция типа с процентным или спредовым риском (облигации, свопы, фьючерсы и форварды) без
 * всякого риска (кроме HTM по начислению: у неё риска нет по правилу) — «без риска» с причиной (не оценена / оценена по отчёту без риска / не посчитан), а не 0.
 * Суммы — в валюте строки (по умолчанию) или в валюте отчёта; итог по всем валютам — только в валюте отчёта и только
 * там, где он суммируется: DV01 по кривым разных валют — с пометкой, не как риск одной валюты.
 */
(function () {
  'use strict';
  const I = window.INSTR = {};
  I.KINDS = [
    ['OFZ_PD', 'GOV', 'ОФЗ-ПД'], ['OFZ_PK', 'GOV', 'ОФЗ-ПК (флоатеры)'], ['OFZ_IN', 'GOV', 'ОФЗ-ИН (линкеры)'],
    ['SOV_FX', 'GOV', 'Суверенные валютные'],
    ['SUBFED', 'SUB', 'Субфедеральные'], ['MUNI', 'SUB', 'Муниципальные'],
    ['CORP_FIX', 'CORP', 'Корпоративные фикс'], ['CORP_FLT', 'CORP', 'Корпоративные флоатеры'],
    ['SECUR', 'CORP', 'Секьюритизация (ИЦБ, ABS)'],
    ['IRS', 'DER', 'Процентные свопы (IRS / OIS)'], ['CCS', 'DER', 'Валютно-процентные свопы (CCS)'],
    ['CDS', 'DER', 'CDS'], ['TRS', 'DER', 'TRS'],
    ['BOND_FUT', 'DER', 'Фьючерсы на облигации'], ['BOND_FWD', 'DER', 'Форварды на облигации'],
    ['FX_FUT', 'DER', 'Валютные фьючерсы'], ['FX_FWD', 'DER', 'FX форварды / NDF'],
    ['FX', 'DER', 'Валюта (спот, прочие FX)'], ['OCP', 'DER', 'Перенос ОВП'],
    ['EQUITY', 'OTH', 'Акции'], ['BOND_NA', 'OTH', 'Облигации без класса (не сопоставлены)'],
    ['UNCLASSIFIED', 'OTH', 'Не классифицировано'],
  ];
  I.GROUPS = ['GOV', 'SUB', 'CORP', 'DER', 'OTH'];
  I.GROUP_LABEL = { GOV: 'Государственные', SUB: 'Субфедеральные и муниципальные', CORP: 'Корпоративные', DER: 'Деривативы и FX', OTH: 'Прочее' };
  I.group = Object.fromEntries(I.KINDS.map(k => [k[0], k[1]]));
  I.label = Object.fromEntries(I.KINDS.map(k => [k[0], k[2]]));
  I.order = Object.fromEntries(I.KINDS.map((k, i) => [k[0], i]));
  I.PRODUCT_KIND = { irs: 'IRS', ccs: 'CCS', cds: 'CDS', trs: 'TRS', bond_future: 'BOND_FUT', bond_forward: 'BOND_FWD', fx_future: 'FX_FUT', fx_ndf: 'FX_FWD', equity: 'EQUITY' };
  I.LEGACY_KIND = { IRS: 'IRS', XCCY: 'CCS', CDS: 'CDS' };
  I.DEMO_KIND = { IRS: 'IRS', KSW: 'IRS', BAS: 'IRS', FXF: 'FX_FWD', XCCY: 'CCS' };
  I.BOND_KIND = { 'GOV.PD': 'OFZ_PD', 'GOV.PK': 'OFZ_PK', 'GOV.IN': 'OFZ_IN', 'GOV.SOVFX': 'SOV_FX', 'CORP.FIX': 'CORP_FIX', 'CORP.FLT': 'CORP_FLT' };
  I.CROSS_CCY = ['CCS', 'FX_FWD', 'FX_FUT', 'FX'];
  I.ATTRIBUTIONS = {
    legs: 'по ногам: DV01 по кривой — в валюте кривой, FX-дельта — в своей валюте; позиция, номинал, MV, CS01 и базис — в валюте позиции',
    position: 'по валюте сделки: всё — в валюте позиции (CCY отчёта); риск на кривых другой валюты — колонкой «DV01 чужих кривых», в DV01 блока не входит',
  };
  I.ATTRIBUTION_DEFAULT = 'legs';  // предварительно: решение владельца (UI-02)
  I.RISK_KINDS = new Set(I.KINDS.filter(k => ['GOV', 'SUB', 'CORP'].includes(k[1])).map(k => k[0])
    .concat(['IRS', 'CCS', 'CDS', 'TRS', 'BOND_FUT', 'BOND_FWD', 'FX_FUT', 'FX_FWD', 'BOND_NA']));
  I.MISSING_TEXT = { not_valued: 'не оценена', reported_no_risk: 'оценена по отчёту, риска в отчёте нет', no_risk: 'риск не посчитан' };

  // тип позиции страниц (lib/real.js или lib/mock.js): api/concepts.instr_kind
  I.kindOf = function (p) {
    const pg = p.productGroup;
    if (pg && I.PRODUCT_KIND[pg]) return I.PRODUCT_KIND[pg];
    if (p.instrType && I.LEGACY_KIND[p.instrType]) return I.LEGACY_KIND[p.instrType];
    if (p.group === 'DER' || p.group === 'OTH') {
      if (I.DEMO_KIND[p.kind]) return I.DEMO_KIND[p.kind];
      if (p.kind === 'FX') return p.ocp ? 'OCP' : 'FX';
      return 'UNCLASSIFIED';
    }
    if (p.notValued || p.assetClass === 'Не сопоставлено') return 'BOND_NA';
    if (p.group === 'SUB') return p.assetClass === 'Муниципальные' ? 'MUNI' : 'SUBFED';
    if (p.group === 'CORP' && p.segment === 'Секьюритизация') return 'SECUR';
    return I.BOND_KIND[p.group + '.' + p.kind] || 'UNCLASSIFIED';
  };
  // причина, по которой у позиции нет риска (null — риск есть или по природе не нужен)
  I.missing = function (p, kind) {
    if (p.notValued) return 'not_valued';
    if (p.accrualOnly || !I.RISK_KINDS.has(kind || I.kindOf(p))) return null;  // HTM по начислению — без риска по правилу
    const any = Object.values(p.dv01ByCurve || {}).some(v => v) || p.cs01 || p.basis01;
    if (any) return null;
    return p.quality === 'reported_value' ? 'reported_no_risk' : 'no_risk';
  };
  // ---------- отнесение мер позиции к валюте «по ногам» ----------
  // Одно правило для всех мониторов: таблица «валюта × тип», шапка (DV01 и ОВП по валютам), блоки валют кокпита,
  // ОВП C06, карточка позиции. DV01 кривой — в валюте кривой (у кривой валюта — своя: b3f3643), FX-дельта — по ключам
  // fx_delta {ccy} прогона, не по валюте позиции: у USD/RUB-форварда риск RUONIA — рублёвый, у кросса USD/CNY
  // FX-дельта — в обеих валютах.
  const MK = () => window.MOCK || {};
  I.curveCcy = id => { const c = (MK().curve || {})[id]; return (c && c.ccy) || String(id).split(' ')[0]; };
  I.isRate = id => { const c = (MK().curve || {})[id]; return !c || c.kind === 'rate'; };
  // FX-дельта по валютам: {ccy: сумма в валюте}; в демо-наборе без карты — одна валюта позиции
  I.fxBy = p => p.fxDeltaBy || (p.fxDelta ? { [p.ccy]: p.fxDelta } : {});
  I.fxIn = (p, ccy) => I.fxBy(p)[ccy] || 0;
  // DV01 по валютам кривых (только процентные кривые, без базиса ОФЗ), в валюте отчёта, как dv01ByCurve
  I.dv01By = p => { const o = {}; Object.entries(p.dv01ByCurve || {}).forEach(([id, v]) => { if (v && I.isRate(id)) { const c = I.curveCcy(id); o[c] = (o[c] || 0) + v; } }); return o; };
  I.dv01In = (p, ccy) => I.dv01By(p)[ccy] || 0;
  // валюты, в которых у позиции есть мера: своя, валюты кривых с риском, валюты FX-дельты
  I.ccysOf = p => [...new Set([p.ccy || 'RUB'].concat(Object.keys(I.dv01By(p)), Object.keys(I.fxBy(p)).filter(c => I.fxIn(p, c))))];
  const fxBy = I.fxBy;

  // таблица по списку позиций (K.list()): opt.attribution — legs | position, opt.units — own (валюта строки) | repr
  I.table = function (list, opt) {
    const M = window.MOCK;
    opt = opt || {};
    const attr = opt.attribution || I.ATTRIBUTION_DEFAULT, own = (opt.units || 'own') === 'own';
    const toR = c => (M.toRepr && M.toRepr[c]) || 1;
    const k = c => own ? 1 / toR(c) : 1;  // множитель из валюты отчёта в единицы строки
    const curveCcy = I.curveCcy, rate = I.isRate;
    const cells = {};
    const blank = (ccy, kind) => ({ ccy, kind, ids: new Set(), legs: new Set(), missing: [], m: {} });
    const cell = (ccy, kind) => { const key = ccy + '|' + kind; return cells[key] || (cells[key] = blank(ccy, kind)); };
    const add = (c, measure, p, v) => { if (!v || !isFinite(v)) return; (c.m[measure] = c.m[measure] || []).push({ p, v }); };
    list.forEach(p => {
      const kind = I.kindOf(p), home = p.ccy || 'RUB', legs = attr === 'legs' || !I.CROSS_CCY.includes(kind);
      const c = cell(home, kind);
      c.ids.add(p.id);
      const why = I.missing(p, kind);
      if (why) c.missing.push({ p, why });
      add(c, 'notional', p, own ? p.notional : p.notionalRub);
      add(c, 'mv', p, (p.mv || 0) * k(home));
      add(c, 'cs01', p, (p.cs01 || 0) * k(home));
      add(c, 'basis', p, (p.basis01 || 0) * k(home));
      Object.entries(p.dv01ByCurve || {}).forEach(([id, v]) => {
        if (!v || !rate(id)) return;
        const cc = curveCcy(id), o = (cc === home || !legs) ? c : cell(cc, kind);
        if (o !== c) o.legs.add(p.id);
        add(o, 'curve:' + id, p, v * k(cc));
        if (cc === o.ccy) add(o, 'dv01', p, v * k(cc));
        else add(o, 'foreign', p, v * k(home));
      });
      Object.entries(fxBy(p)).forEach(([cc, amt]) => {
        if (!amt) return;
        if (cc === home) add(c, 'fx', p, own ? amt : amt * toR(cc));
        else if (legs) { const o = cell(cc, kind); o.legs.add(p.id); add(o, 'fx', p, own ? amt : amt * toR(cc)); }
        else add(c, 'fx', p, own ? amt * toR(cc) / toR(home) : amt * toR(cc));
      });
    });
    const merge = (ccy, label, cs, level, kind, group) => {
      const r = { ccy, label, level, kind: kind || null, group: group || null, ids: new Set(), legs: new Set(), missing: [], m: {} };
      cs.forEach(c => { c.ids.forEach(x => r.ids.add(x)); c.legs.forEach(x => r.legs.add(x)); r.missing.push(...c.missing); Object.entries(c.m).forEach(([mm, rows]) => (r.m[mm] = r.m[mm] || []).push(...rows)); });
      r.legs.forEach(x => { if (r.ids.has(x)) r.legs.delete(x); });
      r.sum = mm => (r.m[mm] || []).reduce((s, x) => s + x.v, 0);
      return r;
    };
    const ccyOrder = M.ccyOrder || {};
    const ccys = [...new Set(Object.values(cells).map(c => c.ccy))].sort((a, b) => ((ccyOrder[a] ?? 99) - (ccyOrder[b] ?? 99)) || a.localeCompare(b));
    const curves = [];
    (M.curves || []).forEach(c => { if (c.kind === 'rate' && Object.values(cells).some(x => x.m['curve:' + c.id])) curves.push(c.id); });
    Object.values(cells).forEach(x => Object.keys(x.m).forEach(mm => { if (mm.startsWith('curve:') && !curves.includes(mm.slice(6))) curves.push(mm.slice(6)); }));
    const blocks = ccys.map(ccy => {
      const mine = Object.values(cells).filter(c => c.ccy === ccy);
      const groups = I.GROUPS.map(g => {
        const kinds = mine.filter(c => I.group[c.kind] === g).sort((a, b) => I.order[a.kind] - I.order[b.kind]);
        if (!kinds.length) return null;
        return { group: g, total: merge(ccy, I.GROUP_LABEL[g], kinds, 'group', null, g), kinds: kinds.map(c => merge(ccy, I.label[c.kind], [c], 'kind', c.kind, g)) };
      }).filter(Boolean);
      return { ccy, total: merge(ccy, ccy, mine, 'ccy'), groups };
    });
    const total = own ? null : merge(null, 'Итого', Object.values(cells), 'total');
    if (total) {  // все валюты: DV01 — по всем кривым (и «чужим» в правиле «по валюте сделки»)
      total.legs.clear();
      if (total.m.foreign) { total.m.dv01 = (total.m.dv01 || []).concat(total.m.foreign); delete total.m.foreign; }
    }
    return { attribution: attr, units: own ? 'own' : 'repr', ccys, curves, curveCcy, blocks, total, n: new Set(list.map(p => p.id)).size };
  };

  // ---------- таблица на странице (kit.js) ----------
  const K = window.KIT;
  if (!K || typeof document === 'undefined') return;
  const M = window.MOCK, f = K.f, esc = K.esc, h = K.h;
  const lc = s => /^[А-ЯЁ][а-яё]/.test(s) ? s[0].toLowerCase() + s.slice(1) : s;  // «из них корпоративные», но «из них ОФЗ-ПД»
  const tint = (hex, a) => hex && hex[0] === '#' && hex.length === 7 ? hex + Math.round(a * 255).toString(16).padStart(2, '0') : 'transparent';
  const KIND_COLOR = { OFZ_PD: 'PD', OFZ_PK: 'PK', OFZ_IN: 'IN', SOV_FX: 'SOVFX', CORP_FIX: 'FIX', CORP_FLT: 'FLT', IRS: 'IRS', CCS: 'XCCY', FX_FWD: 'FXF', FX: 'FX', UNCLASSIFIED: 'OTH', BOND_NA: 'OTH' };
  I.color = kind => K.kindColor[KIND_COLOR[kind]] || K.PAL[I.order[kind] % K.PAL.length];
  const KEY = 'concepts-ccykind';
  I.state = { attribution: I.ATTRIBUTION_DEFAULT, units: 'own' };
  try { Object.assign(I.state, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { }
  if (!I.ATTRIBUTIONS[I.state.attribution]) I.state.attribution = I.ATTRIBUTION_DEFAULT;

  K.ccyKindTable = function (el) {
    const bar = h('div', 'k-rm-bar'), box = h('div', 'k-rm'), foot = h('div', 'k-note');
    foot.style.cssText = 'padding:8px 10px;line-height:1.5';
    el.innerHTML = ''; el.append(bar, box, foot);
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(I.state)); } catch (e) { } };
    bar.append(h('span', 'mut', 'многовалютные (CCS, FX форварды и фьючерсы, FX):'));
    const sa = h('span'); bar.appendChild(sa);
    K.seg(sa, [['legs', 'по ногам'], ['position', 'по валюте сделки']], I.state.attribution, v => { I.state.attribution = v; save(); draw(); });
    const prov = h('span', 'k-chip', 'правило предварительное');
    prov.style.borderColor = 'var(--warn)';
    prov.title = 'Как относить многовалютные инструменты к валюте, решает владелец (UI-02). По умолчанию — «по ногам»: тогда риск валюты равен риску по её кривым в блоках «риск по бакетам».';
    bar.appendChild(prov);
    bar.append(h('span', 'mut', 'суммы:'));
    const su = h('span'); bar.appendChild(su);
    K.seg(su, [['own', 'в валюте строки'], ['repr', 'в ' + K.repr + ' ' + K.SYM[K.repr]]], I.state.units, v => { I.state.units = v; save(); draw(); });
    bar.appendChild(h('span', 'k-note', 'DV01, базис, CS01 — тыс/бп · номинал, MV, FX-дельта — mm · щелчок по цифре — из чего она'));

    function draw() {
      const T = I.table(K.list(), I.state), own = T.units === 'own';
      const dvD = c => own ? (c === 'RUB' ? 0 : 1) : (K.repr === 'RUB' ? 0 : 1);
      const dvFmt = c => v => f.n(v / 1e3, dvD(c));
      const mmFmt = v => f.mn(v, 1);
      const allRows = T.blocks.flatMap(b => [b.total].concat(b.groups.flatMap(g => [g.total].concat(g.kinds))));
      const anyM = mm => allRows.some(r => r.m[mm]) || (T.total && T.total.m[mm]);
      const cols = [
        { mm: 'notional', label: 'Номинал', what: 'mm' }, { mm: 'mv', label: 'MV', what: 'mm' },
        { mm: 'dv01', label: 'DV01', what: 'dv', bold: true, tip: 'DV01 по кривым валюты строки (ставки, без базиса ОФЗ)' },
      ].concat(T.curves.map(id => ({ mm: 'curve:' + id, curve: id, label: (M.curve[id] || {}).short || id, ccy: T.curveCcy(id), what: 'dv', tip: (M.curve[id] || {}).name || id })))
        .concat(T.attribution === 'position' && anyM('foreign') ? [{ mm: 'foreign', label: 'DV01 чужих кривых', what: 'dv', tip: 'Риск позиций этой валюты на кривых другой валюты (правило «по валюте сделки»): в DV01 строки не входит' }] : [])
        .concat(anyM('basis') ? [{ mm: 'basis', label: 'Базис ОФЗ', what: 'dv', tip: 'ofz_spr01: базис ОФЗ − RUONIA' }] : [])
        .concat([{ mm: 'cs01', label: 'CS01', what: 'dv' }])
        .concat(anyM('fx') ? [{ mm: 'fx', label: 'FX-дельта', what: 'mm', tip: 'Открытая валютная позиция: сумма в валюте (в режиме «в валюте отчёта» — по споту)' }] : []);
      const reasons = miss => { const c = {}; miss.forEach(x => c[x.why] = (c[x.why] || 0) + 1); return Object.entries(c).map(([w, n]) => I.MISSING_TEXT[w] + ' — ' + n).join('; '); };
      const missDrill = (r, title) => K.d({ title: title + ' · без риска', unit: 'поз.', fmt: v => f.n(v, 0), color: false, rows: r.missing.map(x => ({ p: x.p, v: 1 })), note: reasons(r.missing) }, String(r.missing.length), 'mut');
      const plain = s => String(s).replace(/<[^>]+>/g, '');
      const cellHtml = (r, col, title) => {
        const rows = r.m[col.mm] || [], c = col.curve ? col.ccy : (r.ccy || K.repr), v = rows.reduce((s, x) => s + x.v, 0);
        const d = dvD(col.curve ? c : r.ccy || 'RUB'), fmt = col.what === 'dv' ? dvFmt(col.curve ? c : r.ccy || 'RUB') : mmFmt;
        const riskCol = col.mm === 'dv01' || col.mm === 'cs01';
        const allMissing = riskCol && r.missing.length && r.missing.length === r.ids.size && !rows.length;
        if (allMissing) return '<td class="n"><span class="mut" title="' + esc('риска нет: ' + reasons(r.missing)) + '">нет</span></td>';
        const tiny = col.what === 'dv' ? Math.abs(v / 1e3) < 0.5 * Math.pow(10, -d) : Math.abs(v / 1e6) < 0.05;
        if (!rows.length || tiny) return '<td class="n"><span class="mut3">·</span></td>';
        const unit = (col.what === 'dv' ? 'тыс ' : 'mm ') + (own ? c : K.repr) + (col.what === 'dv' ? '/бп' : '');
        let html = K.d({ title: plain(title) + ' · ' + col.label, unit, fmt, rows, color: col.mm === 'mv' || col.mm === 'notional' ? false : undefined }, null);
        if (riskCol && r.missing.length) html += '<sup class="mut" style="color:var(--warn)" title="' + esc('без риска ' + r.missing.length + ' поз.: ' + reasons(r.missing)) + '">*</sup>';
        return '<td class="n"' + (col.bold ? ' style="font-weight:600"' : '') + '>' + html + '</td>';
      };
      const nHtml = r => r.ids.size + (r.legs.size ? ' <span class="mut3" title="позиции другой валюты с риском здесь (правило «по ногам»)">+' + r.legs.size + '</span>' : '');
      const head = '<thead><tr><th>Валюта / тип</th><th class="n" title="позиций">Поз.</th>' +
        cols.map(c => '<th class="n"' + (c.tip ? ' title="' + esc(c.tip) + '"' : '') + '>' + (c.curve && T.ccys.length > 1 ? '<span class="mut3">' + esc(c.ccy) + '</span> ' : '') + esc(c.label) + '</th>').join('') +
        '<th class="n" title="позиции без риска (не оценены, оценены по отчёту без риска, риск не посчитан) — не нули">Без риска</th></tr></thead>';
      const tr = (r, label, lvl, bg, title) => {
        const cls = lvl === 0 ? 'grp' : lvl === 1 ? 'grp l1' : '';
        return '<tr' + (cls ? ' class="' + cls + '"' : '') + (bg ? ' style="background:' + bg + '"' : '') + '><td style="padding-left:' + (6 + lvl * 14) + 'px;white-space:nowrap">' + label + '</td><td class="n mut">' + nHtml(r) + '</td>' +
          cols.map(c => cellHtml(r, c, title || label)).join('') + '<td class="n">' + (r.missing.length ? missDrill(r, plain(title || label)) : '<span class="mut3">·</span>') + '</td></tr>';
      };
      let body = '';
      T.blocks.forEach(b => {
        const unit = own ? ' <span class="mut3" style="font-weight:400">в ' + b.ccy + '</span>' : '';
        body += tr(b.total, K.ccyTag(b.ccy) + ' ' + esc(K.ccyName(b.ccy)) + ' — всего' + unit, 0, tint(K.ccyColor(b.ccy), 0.16), b.ccy + ' всего');
        b.groups.forEach(g => {
          const one = g.kinds.length === 1;
          if (!one) body += tr(g.total, 'из них ' + esc(lc(I.GROUP_LABEL[g.group])), 1, null, b.ccy + ' · ' + I.GROUP_LABEL[g.group]);
          g.kinds.forEach(r => body += tr(r, '<b style="color:' + I.color(r.kind) + '">■</b> ' + (one ? 'из них ' + esc(lc(I.label[r.kind])) : esc(I.label[r.kind])), one ? 1 : 2, null, b.ccy + ' · ' + I.label[r.kind]));
        });
      });
      let tfoot = '';
      if (T.total) {
        const r = T.total, label = 'Итого, все валюты (в ' + K.repr + ')';
        const CROSS = { dv01: ['разн. вал.', 'Сумма DV01 по кривым разных валют: не риск одной валюты и не мера хеджа; риск валюты — в её строке'],
          cs01: ['разн. вал.', 'Сумма CS01 по спредам бумаг разных валют'], fx: ['нетто', 'Нетто FX-дельта всех валют в валюте отчёта по споту'] };
        const cross = mm => '<span class="mut3" title="' + esc(CROSS[mm][1]) + '"> ' + CROSS[mm][0] + '</span>';
        tfoot = '<tfoot><tr><td>' + label + '</td><td class="n">' + T.n + '</td>' + cols.map(c => {
          if (c.mm === 'foreign') return '<td></td>';
          const html = cellHtml(r, c, label);
          return (c.mm === 'dv01' || c.mm === 'cs01' || c.mm === 'fx') && (r.m[c.mm] || []).length && T.ccys.length > 1 ? html.replace(/<\/td>$/, cross(c.mm) + '</td>') : html;
        }).join('') + '<td class="n">' + (r.missing.length ? missDrill(r, 'Итого') : '<span class="mut3">·</span>') + '</td></tr></tfoot>';
      } else {
        const why = 'Итог по всем валютам в валютах строк не суммируется: переключите «суммы: в ' + K.repr + '»';
        const curveTot = id => T.blocks.reduce((s, b) => s.concat(b.total.m['curve:' + id] || []), []);
        tfoot = '<tfoot><tr><td title="' + esc(why) + '">Итого по кривым <span class="mut3">(каждая — в своей валюте)</span></td><td class="n">' + T.n + '</td>' + cols.map(c => {
          if (!c.curve) return '<td class="n"><span class="mut3" title="' + esc(why) + '">—</span></td>';
          const rows = curveTot(c.curve);
          return '<td class="n">' + (rows.length ? K.d({ title: 'Итого · ' + c.label, unit: 'тыс ' + c.ccy + '/бп', fmt: dvFmt(c.ccy), rows }, null) : '<span class="mut3">·</span>') + '</td>';
        }).join('') + '<td></td></tr></tfoot>';
      }
      box.innerHTML = T.blocks.length ? '<table class="k-tbl k-compact">' + head + '<tbody>' + body + '</tbody>' + tfoot + '</table>' : '';
      if (!T.blocks.length) K.noData(box, 'нет позиций под фильтром шапки');
      const nMiss = T.blocks.reduce((s, b) => s + b.total.missing.length, 0);
      foot.innerHTML = '<b>Отнесение к валюте</b> (' + esc(I.CROSS_CCY.map(k => I.label[k]).join(', ')) + '): ' + esc(I.ATTRIBUTIONS[T.attribution]) +
        '. <span style="color:var(--warn)">Правило предварительное — решение владельца.</span> ' +
        (T.attribution === 'legs' ? 'DV01 валюты здесь равен риску по её кривым в кокпите (вкладка «Риски», блоки «риск по бакетам» по валютам). ' : 'DV01 блока — риск позиций этой валюты на её кривых; их риск на кривых другой валюты — в колонке «DV01 чужих кривых», а риск кривых валюты у позиций другой валюты — в блоках тех валют. ') +
        '<br><b>Тип</b>: облигации — по типу эмитента, классу выпуска и купону; деривативы и FX — по группе продукта отчёта (irs, ccs, cds, trs, фьючерсы и форварды), без неё — «Не классифицировано». ' +
        (nMiss ? '<b style="color:var(--warn)">Без риска ' + nMiss + ' поз.</b> — показаны как «нет» / «*», не нулями (щелчок по числу в колонке «Без риска» — какие и почему).' : '');
    }
    draw(); K.live(el, 'filter', draw); K.live(el, 'theme', draw);
    return { redraw: draw };
  };
})();
