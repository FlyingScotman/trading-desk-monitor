/*
 * Тип инструмента и таблица агрегированного риска «валюта × тип инструмента» (UI-02).
 * Зеркало api/concepts.py (INSTR_KINDS, PRODUCT_KIND, LEGACY_KIND, instr_kind, risk_table): правка там — правка здесь.
 *
 * Классификация — одна таблица соответствий (INSTR.KINDS, порядок строк таблицы):
 *  - облигации — по типу эмитента и купону, как api/concepts.kind_of: бумаги Минфина России (R39) — по эмитенту
 *    (lib/real.js kindOf, api/concepts.is_minfin: флаг эмитента в справочнике прогона — is_sovereign хаба, ИНН
 *    7710168360 — или класс хаба ofz_* / sovereign_fx, который хаб выводит из того же ИНН; не по списку ISIN; не
 *    иностранный суверен) — в рублях ОФЗ-ПД / -ПК / -ИН, в валюте — свой тип «Минфин валютные» (евробонды и замещающие),
 *    а не «суверены» или «прочие гос.»; плюс класс выпуска статики: субфедеральные / муниципальные, секьюритизация (ИЦБ, ABS);
 *    облигация без класса (не сопоставлена с MOEX, не оценена) — «Облигации без класса»;
 *  - деривативы и FX — по группе продукта отчёта (x_product_group отчёта адаптера в extra.reported.x: irs, ccs, cds, trs,
 *    bond_future, bond_forward, fx_future, fx_ndf, equity), иначе по типу старого tdm (IRS, XCCY, CDS), в демо-наборе —
 *    по его типу (IRS / КС-своп / базис → IRS, FX форвард, XCCY → CCS); FX-строка без группы — «Валюта», перенос ОВП —
 *    свой тип; OTC-строка без группы продукта — «Внебиржевые без группы продукта» (DER, как kind_of: DER.OTC, #345),
 *    с незнакомой группой (новая) — «Не классифицировано» (известное расхождение с kind_of, #367); чего таблица не знает — «Не классифицировано» (видно, не выброшено);
 *  - РЕПО (группа продукта repo, оценённое или нет) — свой тип «РЕПО» в группе «Денежный рынок и фондирование» (MM), вне
 *    деривативов, как kind_of: MM.REPO (решение владельца, #361). Группа типа совпадает с группой
 *    kindOf lib/real.js (api/concepts.kind_of) для любого instrType, кроме облигации без класса и незнакомой группы: tests/fixtures/instr_kinds.json сверяет все три
 *    классификации; имя типа таблицы может быть подробнее (CCS — это XCCY kind_of, CDS — его OTC).
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
    ['MINFIN_FX', 'GOV', 'Минфин валютные (евробонды, замещающие)'],
    ['SUBFED', 'SUB', 'Субфедеральные'], ['MUNI', 'SUB', 'Муниципальные'],
    ['CORP_FIX', 'CORP', 'Корпоративные фикс'], ['CORP_FLT', 'CORP', 'Корпоративные флоатеры'],
    ['SECUR', 'CORP', 'Секьюритизация (ИЦБ, ABS)'],
    ['IRS', 'DER', 'Процентные свопы (IRS / OIS)'], ['CCS', 'DER', 'Валютно-процентные свопы (CCS)'],
    ['CDS', 'DER', 'CDS'], ['TRS', 'DER', 'TRS'],
    ['BOND_FUT', 'DER', 'Фьючерсы на облигации'], ['BOND_FWD', 'DER', 'Форварды на облигации'],
    ['FX_FUT', 'DER', 'Валютные фьючерсы'], ['FX_FWD', 'DER', 'FX форварды / NDF'],
    ['FX', 'DER', 'Валюта (спот, прочие FX)'], ['OCP', 'DER', 'Перенос ОВП'],
    ['OTC', 'DER', 'Внебиржевые без группы продукта'],
    ['REPO', 'MM', 'РЕПО'],
    ['EQUITY', 'OTH', 'Акции'], ['BOND_NA', 'OTH', 'Облигации без класса (не сопоставлены)'],
    ['UNCLASSIFIED', 'OTH', 'Не классифицировано'],
  ];
  I.GROUPS = ['GOV', 'SUB', 'CORP', 'DER', 'MM', 'OTH'];
  I.GROUP_LABEL = { GOV: 'Минфин', SUB: 'Субфедеральные и муниципальные', CORP: 'Корпоративные', DER: 'Деривативы и FX', MM: 'Денежный рынок и фондирование', OTH: 'Прочее' };
  I.group = Object.fromEntries(I.KINDS.map(k => [k[0], k[1]]));
  I.label = Object.fromEntries(I.KINDS.map(k => [k[0], k[2]]));
  I.order = Object.fromEntries(I.KINDS.map((k, i) => [k[0], i]));
  // короткие подписи для столбцов матрицы «валюта × тип» (R38); полная — I.label, в подсказке
  I.SHORT = { OFZ_PD: 'ОФЗ-ПД', OFZ_PK: 'ОФЗ-ПК', OFZ_IN: 'ОФЗ-ИН', MINFIN_FX: 'Минфин вал.', SUBFED: 'Субфед.', MUNI: 'Муницип.',
    CORP_FIX: 'Корп. фикс', CORP_FLT: 'Корп. флоат.', SECUR: 'ИЦБ / ABS', IRS: 'IRS / OIS', CCS: 'CCS', CDS: 'CDS', TRS: 'TRS',
    BOND_FUT: 'Фьюч. обл.', BOND_FWD: 'Форв. обл.', FX_FUT: 'FX фьюч.', FX_FWD: 'FX форв. / NDF', FX: 'FX спот', OCP: 'Перенос ОВП', OTC: 'OTC прочие',
    REPO: 'РЕПО', EQUITY: 'Акции', BOND_NA: 'Без класса', UNCLASSIFIED: 'Не классиф.' };
  I.PRODUCT_KIND = { irs: 'IRS', ccs: 'CCS', cds: 'CDS', trs: 'TRS', bond_future: 'BOND_FUT', bond_forward: 'BOND_FWD', fx_future: 'FX_FUT', fx_ndf: 'FX_FWD', equity: 'EQUITY', repo: 'REPO' };
  I.LEGACY_KIND = { IRS: 'IRS', XCCY: 'CCS', CDS: 'CDS' };
  I.DEMO_KIND = { IRS: 'IRS', KSW: 'IRS', BAS: 'IRS', FXF: 'FX_FWD', XCCY: 'CCS' };
  I.BOND_KIND = { 'GOV.PD': 'OFZ_PD', 'GOV.PK': 'OFZ_PK', 'GOV.IN': 'OFZ_IN', 'GOV.SOVFX': 'MINFIN_FX', 'CORP.FIX': 'CORP_FIX', 'CORP.FLT': 'CORP_FLT' };
  I.CROSS_CCY = ['CCS', 'FX_FWD', 'FX_FUT', 'FX'];
  I.ATTRIBUTIONS = {
    legs: 'по ногам: DV01 по кривой — в валюте кривой, FX-дельта — в своей валюте; позиция, номинал, MV, CS01 и базис — в валюте позиции',
    position: 'по валюте сделки: всё — в валюте позиции (CCY отчёта); риск на кривых другой валюты — колонкой «DV01 чужих кривых», в DV01 блока не входит',
  };
  I.ATTRIBUTION_DEFAULT = 'legs';  // предварительно: решение владельца (UI-02)
  I.RISK_KINDS = new Set(I.KINDS.filter(k => ['GOV', 'SUB', 'CORP'].includes(k[1])).map(k => k[0])
    .concat(['IRS', 'CCS', 'CDS', 'TRS', 'BOND_FUT', 'BOND_FWD', 'FX_FUT', 'FX_FWD', 'BOND_NA']));
  I.MISSING_TEXT = { not_valued: 'не оценена', reported_no_risk: 'оценена по отчёту, риска в отчёте нет', no_risk: 'риск не посчитан',
    implausible: 'риск невозможен (|DV01| не сходится с MV и номиналом): не в суммах, причина в журнале прогона' };

  // тип позиции страниц (lib/real.js или lib/mock.js): api/concepts.instr_kind
  I.kindOf = function (p) {
    const pg = p.productGroup;
    if (pg && I.PRODUCT_KIND[pg]) return I.PRODUCT_KIND[pg];
    if (p.instrType && I.LEGACY_KIND[p.instrType]) return I.LEGACY_KIND[p.instrType];
    if (p.group === 'MM') return p.kind === 'REPO' ? 'REPO' : 'UNCLASSIFIED';
    if (p.group === 'DER' || p.group === 'OTH') {
      if (I.DEMO_KIND[p.kind]) return I.DEMO_KIND[p.kind];
      if (p.kind === 'FX') return p.ocp ? 'OCP' : 'FX';
      // OTC без группы продукта — DER, как api/concepts (#345); с незнакомой группой (новая) — «Не классифицировано»
      // (известное расхождение с kindOf, #367); РЕПО — выше, по группе продукта (#361)
      if (p.kind === 'OTC' && !pg) return 'OTC';
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
    if (p.riskProblem) return 'implausible';  // невозможный риск прогона (#460, lib/real.js): риска в суммах нет
    if (p.accrualOnly || !I.RISK_KINDS.has(kind || I.kindOf(p))) return null;  // HTM по начислению — без риска по правилу
    const any = Object.values(p.dv01ByCurve || {}).some(v => v) || p.cs01 || p.basis01 || p.ofzSpr01;
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
      if (why === 'implausible') return;  // как api/concepts.risk_table: номинал и MV, ни одной меры риска (и FX-дельты)
      add(c, 'cs01', p, (p.cs01 || 0) * k(home));
      add(c, 'basis', p, (p.basis01 || 0) * k(home));
      add(c, 'ofzSpr', p, (p.ofzSpr01 || 0) * k(home));
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
    // матрица (R38): строки — валюты, столбцы — типы инструментов (только те, что есть), ячейка — валюта × тип,
    // итог строки — валюта (blocks[].total), итог столбца — тип по всем валютам (DV01 — по всем кривым, как total)
    const kinds = [...new Set(Object.values(cells).map(c => c.kind))].sort((a, b) => I.order[a] - I.order[b]);
    const byCell = {};
    Object.values(cells).forEach(c => { byCell[c.ccy + '|' + c.kind] = merge(c.ccy, I.label[c.kind], [c], 'kind', c.kind, I.group[c.kind]); });
    const columns = kinds.map(kd => {
      const r = merge(null, I.label[kd], Object.values(cells).filter(c => c.kind === kd), 'column', kd, I.group[kd]);
      if (r.m.foreign) { r.m.dv01 = (r.m.dv01 || []).concat(r.m.foreign); delete r.m.foreign; }
      return { kind: kd, group: I.group[kd], total: r };
    });
    return { attribution: attr, units: own ? 'own' : 'repr', ccys, curves, curveCcy, blocks, total, kinds, cell: byCell, columns, n: new Set(list.map(p => p.id)).size };
  };

  // ---------- таблица на странице (kit.js) ----------
  const K = window.KIT;
  if (!K || typeof document === 'undefined') return;
  const M = window.MOCK, f = K.f, esc = K.esc, h = K.h;
  const tint = (hex, a) => hex && hex[0] === '#' && hex.length === 7 ? hex + Math.round(a * 255).toString(16).padStart(2, '0') : 'transparent';
  const KIND_COLOR = { OFZ_PD: 'PD', OFZ_PK: 'PK', OFZ_IN: 'IN', MINFIN_FX: 'SOVFX', CORP_FIX: 'FIX', CORP_FLT: 'FLT', IRS: 'IRS', CCS: 'XCCY', FX_FWD: 'FXF', FX: 'FX', OTC: 'OTC', REPO: 'REPO', UNCLASSIFIED: 'OTH', BOND_NA: 'OTH' };
  I.color = kind => K.kindColor[KIND_COLOR[kind]] || K.PAL[I.order[kind] % K.PAL.length];
  const KEY = 'concepts-ccykind';
  I.state = { attribution: I.ATTRIBUTION_DEFAULT, units: 'own', measure: 'dv01', curve: null };
  try { Object.assign(I.state, JSON.parse(localStorage.getItem(KEY) || '{}')); } catch (e) { }
  if (!I.ATTRIBUTIONS[I.state.attribution]) I.state.attribution = I.ATTRIBUTION_DEFAULT;

  // меры матрицы (R38; api/concepts.MATRIX_MEASURES): одна на экране. across — как суммируется по валютам (строка итога):
  // yes — всегда (позиции; DV01 одной кривой — в её валюте), repr — только в валюте отчёта, labelled — в валюте отчёта
  // с пометкой (DV01 по кривым разных валют — не риск одной валюты)
  I.MEASURES = [
    { id: 'dv01', label: 'DV01', what: 'dv', risk: true, across: 'labelled', tip: 'DV01 по кривым валюты строки (ставки, без базиса ОФЗ)' },
    { id: 'curve', label: 'DV01 по кривой', what: 'dv', risk: true, across: 'yes', tip: 'DV01 одной кривой: в валюте кривой' },
    { id: 'cs01', label: 'CS01', what: 'dv', risk: true, across: 'repr' },
    { id: 'basis', label: 'Базис ОФЗ', what: 'dv', across: 'repr', tip: 'базис ОФЗ − RUONIA: риск ОФЗ к par-доходностям КБД при неизменной RUONIA — равен её DV01 на КБД (прогоны 0.2.6+; до них — спред ОФЗ к RUONIA). С 0.2.7 (#213) риск RUONIA ОФЗ посчитан отдельно (котировки OIS при неизменном базисе, хвост 15Y–20Y из КБД) и лежит в DV01 RUONIA; базис — отдельный фактор, к DV01 не прибавлять. Без ОФЗ-ИН: её номинальный прокси (#38) — только в DV01. В виде «КБД» — 0: риск ОФЗ на кривой КБД' },
    { id: 'ofzSpr', label: 'Спред ОФЗ к КБД', what: 'dv', across: 'repr', tip: 'собственный Z-спред ОФЗ к КБД, тыс ₽/бп (прогоны 0.2.6+)' },
    { id: 'fx', label: 'FX-дельта', what: 'mm', across: 'repr', tip: 'Открытая валютная позиция: сумма в валюте (в валюте отчёта — по споту)' },
    { id: 'mv', label: 'MV', what: 'mm', across: 'repr' },
    { id: 'notional', label: 'Номинал', what: 'mm', across: 'repr' },
    { id: 'n', label: 'Позиций', what: 'n', across: 'yes', tip: 'число позиций в валюте строки; «+N» — позиции другой валюты с риском здесь (правило «по ногам»)' },
  ];
  I.MEASURE = Object.fromEntries(I.MEASURES.map(m => [m.id, m]));

  K.ccyKindTable = function (el) {
    const bar = h('div', 'k-rm-bar'), bar2 = h('div', 'k-rm-bar'), box = h('div', 'k-rm'), foot = h('div', 'k-note');
    foot.style.cssText = 'padding:8px 10px;line-height:1.5';
    el.innerHTML = ''; el.append(bar, bar2, box, foot);
    const save = () => { try { localStorage.setItem(KEY, JSON.stringify(I.state)); } catch (e) { } };
    if (!I.MEASURE[I.state.measure]) I.state.measure = 'dv01';
    // мера
    bar.append(h('span', 'mut', 'мера:'));
    const sm = h('span'); bar.appendChild(sm);
    K.seg(sm, I.MEASURES.map(m => [m.id, m.label]), I.state.measure, v => { I.state.measure = v; save(); draw(); });
    const cs = h('select', 'k-input'); cs.style.cssText = 'min-width:0;padding:2px 6px';
    cs.onchange = () => { I.state.curve = cs.value; save(); draw(); };
    bar.appendChild(cs);
    // отнесение и единицы
    bar2.append(h('span', 'mut', 'многовалютные (CCS, FX форварды и фьючерсы, FX):'));
    const sa = h('span'); bar2.appendChild(sa);
    K.seg(sa, [['legs', 'по ногам'], ['position', 'по валюте сделки']], I.state.attribution, v => { I.state.attribution = v; save(); draw(); });
    const prov = h('span', 'k-chip', 'правило предварительное');
    prov.style.borderColor = 'var(--warn)';
    prov.title = 'Как относить многовалютные инструменты к валюте, решает владелец (UI-02). По умолчанию — «по ногам»: тогда риск валюты равен риску по её кривым в блоках «риск по бакетам».';
    bar2.appendChild(prov);
    bar2.append(h('span', 'mut', 'суммы:'));
    const su = h('span'); bar2.appendChild(su);
    K.seg(su, [['own', 'в валюте строки'], ['repr', 'в ' + K.repr + ' ' + K.SYM[K.repr]]], I.state.units, v => { I.state.units = v; save(); draw(); });
    bar2.appendChild(h('span', 'k-note', 'по горизонтали — тип, по вертикали — валюта · щелчок по цифре — из чего она'));

    function draw() {
      const T = I.table(K.list(), I.state), own = T.units === 'own';
      const ms = I.MEASURE[I.state.measure];
      // кривая меры «DV01 по кривой»: выбранная, если она есть в данных, иначе первая
      cs.style.display = ms.id === 'curve' ? '' : 'none';
      if (ms.id === 'curve') {
        if (!T.curves.includes(I.state.curve)) I.state.curve = T.curves[0] || null;
        cs.innerHTML = T.curves.map(id => '<option value="' + esc(id) + '"' + (id === I.state.curve ? ' selected' : '') + '>' + esc(((M.curve[id] || {}).name || id) + ' · ' + T.curveCcy(id)) + '</option>').join('');
      }
      const curve = ms.id === 'curve' ? I.state.curve : null, mm = curve ? 'curve:' + curve : ms.id;
      // единицы ячейки: своя валюта строки (DV01 кривой — валюта кривой) или валюта отчёта
      const unitCcy = rowCcy => !own ? K.repr : curve ? T.curveCcy(curve) : rowCcy;
      const dvD = c => c === 'RUB' ? 0 : 1;
      const fmtOf = c => ms.what === 'dv' ? v => f.n(v / 1e3, dvD(c)) : ms.what === 'mm' ? v => f.mn(v, 1) : v => f.n(v, 0);
      const unitTxt = c => ms.what === 'dv' ? 'тыс ' + c + '/бп' : ms.what === 'mm' ? 'mm ' + c : 'поз.';
      const val = r => ms.what === 'n' ? r.ids.size : r.sum(mm);
      const reasons = miss => { const c = {}; miss.forEach(x => c[x.why] = (c[x.why] || 0) + 1); return Object.entries(c).map(([w, n]) => I.MISSING_TEXT[w] + ' — ' + n).join('; '); };
      const plain = s => String(s).replace(/<[^>]+>/g, '');
      const posRows = r => [...r.ids].concat([...r.legs]).map(id => M.pos[id]).filter(Boolean).map(p => ({ p, v: 1 }));
      // насыщенность — по наибольшей ячейке меры (не по итогам)
      const cells = T.ccys.flatMap(c => T.kinds.map(kd => T.cell[c + '|' + kd]).filter(Boolean));
      const max = Math.max(0, ...cells.map(r => Math.abs(val(r) * (own && ms.what !== 'n' ? ((M.toRepr && M.toRepr[unitCcy(r.ccy)]) || 1) : 1))));
      const cellHtml = (r, title, ccy, opt) => {
        opt = opt || {};
        if (!r) return '<td class="n"><span class="mut3">·</span></td>';
        const c = unitCcy(ccy), v = val(r), fmt = fmtOf(c);
        const allMissing = ms.risk && r.missing.length && r.missing.length === r.ids.size && !(r.m[mm] || []).length;
        if (allMissing) return '<td class="n"><span class="mut" title="' + esc('риска нет: ' + reasons(r.missing)) + '">нет</span></td>';
        const d = ms.what === 'dv' ? dvD(c) : ms.what === 'mm' ? 1 : 0;
        const tiny = ms.what === 'dv' ? Math.abs(v / 1e3) < 0.5 * Math.pow(10, -d) : ms.what === 'mm' ? Math.abs(v / 1e6) < 0.05 : !v && !r.legs.size;
        if (tiny) return '<td class="n"><span class="mut3">·</span></td>';
        let html;
        if (ms.what === 'n') {
          html = K.d({ title: plain(title) + ' · позиции', unit: 'поз.', fmt: x => f.n(x, 0), rows: posRows(r), color: false }, f.n(v, 0), '') +
            (r.legs.size ? ' <span class="mut3" title="позиции другой валюты с риском здесь (правило «по ногам»)">+' + r.legs.size + '</span>' : '');
        } else html = K.d({ title: plain(title) + ' · ' + ms.label + (curve ? ' ' + curve : ''), unit: unitTxt(c), fmt, rows: r.m[mm] || [], color: ms.id === 'mv' || ms.id === 'notional' ? false : undefined }, null);
        if (ms.risk && r.missing.length) html += '<sup class="mut" style="color:var(--warn)" title="' + esc('без риска ' + r.missing.length + ' поз.: ' + reasons(r.missing)) + '">*</sup>';
        if (opt.mark) html += opt.mark;
        const heat = ms.what === 'n' ? '' : opt.total ? K.HEAT_PLAIN : (ms.risk || ms.id === 'basis' || ms.id === 'ofzSpr' ? K.heatRisk : K.heatMono)(v * (own ? ((M.toRepr && M.toRepr[c]) || 1) : 1), max);
        return '<td class="n"' + (heat || opt.total ? ' style="' + heat + (opt.total ? ';font-weight:600' : '') + '"' : '') + '>' + html + '</td>';
      };
      // заголовок: группы типов (Минфин, Корпоративные, Деривативы…) и типы
      const groups = I.GROUPS.map(g => [g, T.kinds.filter(kd => I.group[kd] === g)]).filter(x => x[1].length);
      const head = '<thead><tr><th rowspan="2" style="vertical-align:bottom">Валюта</th>' + groups.map(([g, ks]) => '<th class="n" colspan="' + ks.length + '" style="text-align:center;border-bottom:1px solid var(--line)">' + esc(I.GROUP_LABEL[g]) + '</th>').join('') +
        '<th rowspan="2" class="n" style="vertical-align:bottom">Итого</th></tr><tr>' +
        groups.flatMap(x => x[1]).map(kd => '<th class="n" title="' + esc(I.label[kd]) + '" style="white-space:nowrap"><b style="color:' + I.color(kd) + '">■</b> ' + esc(I.SHORT[kd] || I.label[kd]) + '</th>').join('') + '</tr></thead>';
      const order = groups.flatMap(x => x[1]);
      let body = '';
      T.blocks.forEach(b => {
        const unit = own ? ' <span class="mut3" style="font-weight:400">' + esc(unitTxt(unitCcy(b.ccy))) + '</span>' : '';
        body += '<tr style="background:' + tint(K.ccyColor(b.ccy), 0.08) + '"><td style="white-space:nowrap">' + K.ccyTag(b.ccy) + ' ' + esc(K.ccyName(b.ccy)) + unit + '</td>' +
          order.map(kd => cellHtml(T.cell[b.ccy + '|' + kd], b.ccy + ' · ' + I.label[kd], b.ccy)).join('') +
          cellHtml(b.total, b.ccy + ' · все типы', b.ccy, { total: true }) + '</tr>';
      });
      // строка итога — только где мера суммируется по валютам (в валюте отчёта; позиции и DV01 одной кривой — всегда)
      let tfoot;
      const sums = ms.across === 'yes' || !own;
      if (sums) {
        const lab = ms.across === 'labelled' ? 'разн. вал.' : ms.id === 'fx' ? 'нетто' : '';
        const tip = ms.across === 'labelled' ? 'Сумма DV01 по кривым разных валют: не риск одной валюты и не мера хеджа; риск валюты — в её строке' : ms.id === 'fx' ? 'Нетто FX-дельта всех валют в валюте отчёта по споту' : '';
        const mark = lab && T.ccys.length > 1 ? '<span class="mut3" title="' + esc(tip) + '"> ' + lab + '</span>' : '';
        const label = 'Итого, все валюты' + (own && ms.what !== 'n' ? ' (в ' + unitCcy('RUB') + ')' : ms.what !== 'n' ? ' (в ' + K.repr + ')' : '');
        const corner = T.total || { ids: new Set(T.blocks.flatMap(b => [...b.total.ids])), legs: new Set(), missing: T.blocks.flatMap(b => b.total.missing), m: { [mm]: T.blocks.flatMap(b => b.total.m[mm] || []) }, sum: x => T.blocks.reduce((s, b) => s + b.total.sum(x), 0) };
        tfoot = '<tfoot><tr><td style="white-space:nowrap">' + label + mark + '</td>' + order.map(kd => cellHtml(T.columns.find(c => c.kind === kd).total, 'Итого · ' + I.label[kd], 'RUB', { total: true })).join('') +
          cellHtml(corner, 'Итого', 'RUB', { total: true }) + '</tr></tfoot>';
      } else {
        const why = 'Итог по всем валютам в валютах строк не суммируется: переключите «суммы: в ' + K.repr + '»';
        tfoot = '<tfoot><tr><td colspan="' + (order.length + 2) + '"><span class="mut3">' + esc(why) + '</span></td></tr></tfoot>';
      }
      box.innerHTML = T.blocks.length ? '<table class="k-tbl k-compact">' + head + '<tbody>' + body + '</tbody>' + tfoot + '</table>' : '';
      if (!T.blocks.length) K.noData(box, 'нет позиций под фильтром шапки');
      const miss = T.blocks.flatMap(b => b.total.missing);
      const missDrill = miss.length ? K.d({ title: 'Без риска', unit: 'поз.', fmt: v => f.n(v, 0), color: false, rows: miss.map(x => ({ p: x.p, v: 1 })), note: reasons(miss) }, miss.length + ' поз.', '') : '';
      foot.innerHTML = '<b>Мера</b>: ' + esc(ms.label + (curve ? ' — ' + ((M.curve[curve] || {}).name || curve) : '')) + (ms.tip ? ' (' + esc(ms.tip) + ')' : '') +
        '; ' + (own ? 'в валюте строки' + (curve ? ' (кривой)' : '') : 'в ' + K.repr) + '; строка «Итого» — только где мера суммируется по валютам. ' +
        '<b>Отнесение к валюте</b> (' + esc(I.CROSS_CCY.map(k => I.label[k]).join(', ')) + '): ' + esc(I.ATTRIBUTIONS[T.attribution]) +
        '. <span style="color:var(--warn)">Правило предварительное — решение владельца.</span> ' +
        (T.attribution === 'legs' ? 'DV01 валюты (столбец «Итого») равен риску по её кривым в кокпите (вкладка «Риски», блоки «риск по бакетам»). ' : 'DV01 строки — риск позиций этой валюты на её кривых; их риск на кривых другой валюты в DV01 строки не входит (виден в мере «DV01 по кривой»). ') +
        '<br><b>Тип</b>: облигации — по эмитенту, классу выпуска и купону; бумаги Минфина России — по эмитенту (флаг эмитента в справочнике хаба, ИНН 7710168360, класс ОФЗ / валютных бумаг Минфина): в рублях — ОФЗ, в валюте — «Минфин вал.» (евробонды и замещающие); деривативы и FX — по группе продукта отчёта, без неё — «Не классифицировано». ' +
        (miss.length ? '<b style="color:var(--warn)">Без риска ' + missDrill + '</b> — «нет» / «*», не нули.' : '');
    }
    draw(); K.live(el, 'filter', draw); K.live(el, 'theme', draw);
    return { redraw: draw };
  };
})();
