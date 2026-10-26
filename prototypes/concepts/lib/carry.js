/*
 * Прогнозное кэрри (C17, trading-desk-monitor#66, #72): запрос к GET ../api/v1/concepts/carry и разбор ответа.
 * Без DOM: страница c17-forward-carry.html рисует, этот файл считает (его же проверяет tests/js/forward_carry.test.js).
 *
 * Ответ сервера (backend scenario/carry.py, api/models.py CarryOut) — суммы в рублях прогона, со знаком:
 *  - по позиции carry (полное изменение стоимости при кривых, перенесённых в горизонт: купоны + изменение грязной
 *    цены; при spread=curve в него входит spreadRoll), funding (за период, null — не посчитано), net = carry + funding;
 *    без прогноза — carry null и code (причина);
 *  - totals / byBook / ...: новый формат (#297) — funding только позиций с прогнозом, fundingNoCarry — позиций без
 *    прогноза, fundingBookLevel — строк без позиции (остатки ДС), fundingTotal — их сумма; net = carry + fundingTotal,
 *    null по правилу B1 (фондирование не посчитано, нет позиций с прогнозом или у позиции с прогнозом фондирование
 *    null), netKnown — нетто позиций, у которых есть и кэрри, и фондирование. Старый формат (до #297): funding группы —
 *    всё фондирование; поддерживается, пока #297 не в main.
 * На экране «Кэрри», «Фондирование», «Нетто» (решение владельца 26.10). Сумма складывается на глазах: Кэрри
 * (позиции с прогнозом) + Фондирование тех же позиций + «фондирование без прогноза кэрри» (позиции без прогноза
 * и остатки ДС) = Нетто. Фондирование null не подменяется нулём.
 * Итоги считаются здесь по строкам по тому же правилу, что у сервера: экран режет позиции общими фильтрами
 * (юрлицо, деск, книга, валюта). Без фильтров страница берёт итоги сервера (C.withServer); они совпадают (тест).
 */
(function (root) {
  'use strict';
  const C = {};
  C.HORIZONS = ['1M', '3M', '6M', '1Y'];
  C.KS = [['flat', 'КС стоит'], ['steps', 'по заседаниям']];
  C.SPREADS = [['held', 'спред держится'], ['curve', 'roll по спреду']];
  C.ISO = /^\d{4}-\d{2}-\d{2}$/;

  // ---------- запрос ----------
  // o: { run, horizon, ks, steps: [б.п. по заседаниям по порядку], spread }
  C.query = function (o) {
    const q = [];
    if (o.run != null && o.run !== '') q.push('run=' + encodeURIComponent(o.run));
    q.push('horizon=' + encodeURIComponent(o.horizon || '3M'));
    q.push('ks=' + (o.ks === 'steps' ? 'steps' : 'flat'));
    if (o.ks === 'steps') {
      const s = (o.steps || []).map(v => +v || 0);
      while (s.length && !s[s.length - 1]) s.pop();  // хвостовые нули не нужны: «нет» = 0
      if (s.length) q.push('steps=' + s.join(','));
    }
    q.push('spread=' + (o.spread === 'curve' ? 'curve' : 'held'));
    return q.join('&');
  };
  C.url = o => '../api/v1/concepts/carry?' + C.query(o);
  // асинхронно: промис с телом ответа; ошибка — Error с полями status и text (detail сервера)
  C.load = function (o, opts) {
    opts = opts || {};
    const f = opts.fetch || root.fetch;
    return f(C.url(o), { signal: opts.signal, headers: { Accept: 'application/json' } }).then(r => {
      if (r.ok) return r.json();
      return r.text().then(t => {
        let msg = t;
        try { const j = JSON.parse(t); msg = (j.error && (j.error.message || j.error.detail)) || j.detail || j.message || t; } catch (e) { }
        throw Object.assign(new Error(r.status + ': ' + msg), { status: r.status, text: String(msg) });
      });
    });
  };

  // ---------- подписи ----------
  // почему нет прогноза (code позиции): качество оценки, not_a_bond, base_mismatch, core_<код>
  C.REASON = {
    not_valued: 'не оценена',
    not_a_bond: 'не облигация: кэрри свопов, РЕПО и FX пока не подключено',
    base_mismatch: 'стартовая PV ядра не совпала с прогоном',
    market_value: 'оценка по рыночной стоимости, не ядром',
    reported: 'оценка по отчёту, не ядром',
    reported_value: 'оценка по отчёту, не ядром',
    fx: 'валютная позиция: прогноза кэрри нет',
    par_proxy: 'оценка ниже точной (по номиналу)',
    core_unsupported_product: 'ядро не несёт бумагу на горизонт (валютные облигации)',
    core_accrued_failed: 'НКД на горизонте не посчитан',
  };
  C.reasonText = function (code) {
    if (!code) return '';
    if (C.REASON[code]) return C.REASON[code];
    if (/^core_/.test(code)) return 'ядро отклонило: ' + code.slice(5);
    return 'оценка ниже точной: ' + code;
  };
  C.FUNDING_STATE = { funded: 'фондируется', not_funded: 'не фондируется по MV', problem: 'проблема', not_configured: 'не настроено', not_valued: 'не оценена' };
  C.FUNDING_SOURCE = { adapter: 'правило адаптера', settings: 'правило в настройках', settings_override: 'правило настроек поверх адаптера',
    default: 'рыночная ставка по умолчанию', contract: 'ставка договора (HTM)', none: '' };
  C.FUNDING_STATUS = { computed: 'фондирование посчитано', not_configured: 'фондирование не настроено', invalid_config: 'настройки фондирования с ошибкой', failed: 'фондирование не посчитано' };
  C.SPREAD_HELD = { no_rating: 'нет рейтинга', no_peer_curve: 'нет кривой спредов своего и соседнего рейтинга', ofz_held: 'ОФЗ: спред к КБД держится',
    floater_held: 'флоатер: DM держится', not_rub: 'не рубли', redeemed: 'погашается до горизонта', no_cs01: 'нет CS01', no_quote: 'нет котировки', not_covered: 'нет прогноза' };
  C.spreadText = s => !s ? '' : C.SPREAD_HELD[s] || 'по кривой ' + s;

  // ---------- строки ----------
  // opts: { pos: {id: позиция модели монитора (M.pos)}, k: множитель рубли → валюта показа, books: {id: книга},
  //         kindLabel: код → подпись }
  // Два формата ответа (пока #297 не в main): новый — у позиции dv01, cs01, fxRate, inCcy, у групп fundingTotal,
  // netKnown, nCoveredNoFunding, а funding группы — только позиций с прогнозом (C.isNew); старый — без них.
  // dv01 и cs01 — из ответа (рубли за б.п. → валюта показа), в старом формате — из модели монитора (того же прогона).
  // opts.repr — валюта показа: позиция в этой же валюте берёт суммы из inCcy, а не пересчитывает рубли по курсу.
  // Знаменатель «кэрри на единицу риска» (решение по #66): у флоатера (p.cpn.type 'float' модели
  // монитора) основной риск спредовый — CS01, у остальных — DV01 ставок
  C.rows = function (resp, opts) {
    opts = opts || {};
    const P = opts.pos || {}, k = opts.k == null ? 1 : opts.k, B = opts.books || {}, KL = opts.kindLabel || (x => x);
    const ok = resp.fundingStatus === 'computed';
    const m = v => v == null || !isFinite(v) ? null : v * k;
    return (resp.positions || []).map(x => {
      const p = P[x.positionId] || null, b = B[x.bookId] || {};
      const own = opts.repr && x.inCcy && x.ccy === opts.repr;  // в своей валюте: inCcy как есть
      const pick = key => own ? (x.inCcy[key] == null || !isFinite(x.inCcy[key]) ? null : x.inCcy[key]) : m(x[key]);
      const carry = pick('carry'), funding = ok ? pick('funding') : null;
      const fin = v => v != null && isFinite(v) ? v : null;
      const dv01 = 'dv01' in x ? m(x.dv01) : p ? fin(p.dv01) : null;
      const cs01 = 'cs01' in x ? m(x.cs01) : p ? fin(p.cs01) : null;
      const floater = p ? C.isFloater(p) : C.FLOAT_KINDS.includes(x.kind);
      const r = {
        id: x.positionId, p, name: p ? p.name : (x.secid || x.positionId), secid: x.secid, issuer: p ? p.issuer : null,
        book: x.bookId, bookName: p ? p.bookName : (b.name || x.bookId), entity: x.entity || (p && p.entity) || b.entity || '—',
        desk: (p && p.desk) || b.desk || '—', ccy: x.ccy || (p && p.ccy) || '—', kind: x.kind || '—', kindLabel: x.kind ? KL(x.kind) : (p && p.kindLabel) || '—',
        quality: x.quality, covered: carry != null, carry, funding,
        net: carry == null || funding == null ? null : carry + funding,
        fundingState: ok ? x.fundingState : (resp.fundingStatus === 'not_configured' ? 'not_configured' : x.fundingState),
        fundingSource: x.fundingSource, fundingRate: x.fundingRate || null, fundingRuleId: x.fundingRuleId || null, fundingNote: x.fundingNote || null,
        spreadRoll: pick('spreadRoll'), spreadRollBp: x.spreadRollBp == null ? null : x.spreadRollBp, spreadCurve: x.spreadCurve || null,
        carryMethod: x.carryMethod || null, code: x.code || null, detail: x.detail || null, reason: C.reasonText(x.code),
        fxRate: x.fxRate == null ? null : x.fxRate, inCcy: x.inCcy || null,
        dv01, cs01, floater, denom: floater ? 'CS01' : 'DV01', carryDv01: C.perDv01(carry, floater ? cs01 : dv01),
        raw: x,
      };
      r.searchText = [r.name, r.id, r.secid, r.book, r.bookName, r.entity, r.ccy, r.kindLabel, r.reason, p && p.searchText].filter(Boolean).join(' ').toLowerCase();
      return r;
    });
  };
  // кэрри на единицу DV01, б.п.: на сколько б.п. ставки могут уйти против позиции за горизонт, пока переоценка не
  // съест кэрри (кэрри / |DV01|); DV01 ≈ 0 (флоатер без риска, своп с нулевым риском) — нет
  C.isFloater = p => !!(p && p.cpn && p.cpn.type === 'float');
  C.FLOAT_KINDS = ['OFZ_PK', 'CORP_FLT'];  // позиции нет в модели монитора: флоатер по типу инструмента
  C.isNew = resp => !!(resp && resp.totals && 'fundingTotal' in resp.totals);
  C.perDv01 = (carry, dv01) => carry == null || dv01 == null || Math.abs(dv01) < C.MIN_DEN ? null : carry / Math.abs(dv01);

  // строки фондирования без позиции (остатки ДС книги или юрлица): псевдострока таблицы, в валюте показа; в кэрри
  // не входит, в фондирование и нетто группы — входит (как у сервера: fundingBookLevel)
  C.CASH_KIND = { CASH: 'остатки ДС книги', ENTITY_CASH: 'остатки ДС юрлица' };
  C.cashRows = function (resp, opts) {
    opts = opts || {};
    const k = opts.k == null ? 1 : opts.k, B = opts.books || {};
    if (resp.fundingStatus !== 'computed') return [];
    return (resp.fundingBookLevel || []).map((x, i) => {
      const b = B[x.book] || {}, nm = C.CASH_KIND[x.kind] || 'фондирование без позиции';
      const r = { id: 'cash:' + i + ':' + x.book + ':' + (x.ccy || '') + ':' + (x.kind || ''), isCash: true, p: null, name: nm + ' · ' + (x.ccy || '—'),
        book: x.book, bookName: b.name || x.book, entity: x.entity || b.entity || '—', desk: b.desk || '—', ccy: x.ccy || '—', kind: 'CASH', kindLabel: 'остатки ДС',
        covered: false, carry: null, funding: x.amount == null ? null : x.amount * k, net: null, fundingState: 'funded', fundingSource: x.ruleSource || 'none',
        fundingRate: x.rateExpr || null, fundingRuleId: x.ruleId || null, fundingNote: x.defaultRate ? 'рыночная ставка по умолчанию' : null,
        spreadRoll: null, spreadRollBp: null, spreadCurve: null, carryMethod: null, code: null, detail: null,
        reason: 'строка фондирования без позиции: в кэрри не входит', dv01: null, cs01: null, floater: false, denom: null, carryDv01: null, raw: x };
      r.searchText = [r.name, r.book, r.bookName, r.entity, r.ccy, 'остатки'].join(' ').toLowerCase();
      return r;
    });
  };

  // знаменатель меньше этого (0, пусто) — отношения нет
  C.MIN_DEN = 1e-6;
  // итоги группы по строкам C.rows и C.cashRows; fundingOk — fundingStatus === 'computed'. Кэрри — позиции
  // с прогнозом; фондирование — их же (fundingCovered); fundingRest — «фондирование без прогноза кэрри»: позиции
  // без прогноза (fundingNoCarry) и остатки ДС (fundingCash); net = carry + fundingCovered + fundingRest (= carry +
  // funding сервера), null, если у позиции с прогнозом фондирования нет или оно не посчитано вовсе
  C.sums = function (rows, fundingOk) {
    const s = { n: 0, nCovered: 0, nNotCovered: 0, nFundingMissing: 0, nFundingMissingNoCarry: 0, carry: 0, fundingCovered: 0, fundingNoCarry: 0, fundingCash: 0,
      spreadRoll: null, dv01: 0, carryFix: 0, nFix: 0, cs01Flt: 0, carryFlt: 0, nFlt: 0, netKnown: 0, notCovered: [], fundingMissing: [], nCash: 0 };
    rows.forEach(r => {
      if (r.isCash) { s.nCash++; if (r.funding != null) s.fundingCash += r.funding; return; }
      s.n++;
      if (r.covered) {
        s.nCovered++; s.carry += r.carry;
        // кэрри/DV01 итога — по фиксам (без флоатеров), флоатеры — кэрри/CS01 отдельно
        // позиция без знаменателя (0 или нет) не входит ни в числитель, ни в знаменатель
        const den = r.floater ? r.cs01 : r.dv01;
        if (den != null && Math.abs(den) >= C.MIN_DEN) {
          if (r.floater) { s.nFlt++; s.carryFlt += r.carry; s.cs01Flt += den; } else { s.nFix++; s.carryFix += r.carry; s.dv01 += den; }
        }
        if (r.spreadRoll != null) s.spreadRoll = (s.spreadRoll || 0) + r.spreadRoll;
        if (r.funding != null) { s.fundingCovered += r.funding; s.netKnown += r.carry + r.funding; } else { s.nFundingMissing++; s.fundingMissing.push(r); }
      } else {
        s.nNotCovered++; s.notCovered.push(r);
        if (r.funding != null) s.fundingNoCarry += r.funding; else { s.nFundingMissingNoCarry++; s.fundingMissing.push(r); }
      }
    });
    s.fundingOk = !!fundingOk;
    if (!s.nCovered) s.carry = null;
    if (!fundingOk) {
      ['fundingCovered', 'fundingNoCarry', 'fundingCash', 'fundingRest', 'funding', 'net', 'netKnown'].forEach(k => { s[k] = null; });
    } else {
      s.fundingRest = s.fundingNoCarry + s.fundingCash;  // «фондирование без прогноза кэрри»
      s.funding = s.fundingCovered + s.fundingRest;
      // нетто — правило сервера B1 (#297): «—», если в группе нет позиций с прогнозом или у позиции с прогнозом
      // фондирования нет; фондирование null у позиции без прогноза считается 0 (её видно в баннере)
      s.net = s.nFundingMissing || !s.nCovered ? null : s.carry + s.funding;
    }
    if (!s.nCovered && s.netKnown === 0) s.netKnown = null;
    s.carryDv01 = C.perDv01(s.carryFix, s.nFix ? s.dv01 : null);
    s.carryCs01Flt = C.perDv01(s.carryFlt, s.nFlt ? s.cs01Flt : null);
    return s;
  };

  // итоги группы с сервера (новый формат, без клиентских фильтров): суммы — значения сервера в валюте показа;
  // кэрри/DV01 и списки для баннера остаются клиентскими (сервер суммирует DV01 всех позиций, включая флоатеры)
  C.withServer = function (s, g, k) {
    if (!g || !('fundingTotal' in g)) return s;
    const m = v => v == null ? null : v * (k == null ? 1 : k);
    return Object.assign(s, { carry: m(g.carry), spreadRoll: m(g.spreadRoll), fundingCovered: m(g.funding), fundingNoCarry: m(g.fundingNoCarry),
      fundingCash: m(g.fundingBookLevel), fundingRest: g.fundingTotal == null ? null : m((g.fundingNoCarry || 0) + (g.fundingBookLevel || 0)),
      funding: m(g.fundingTotal), net: m(g.net), netKnown: m(g.netKnown), nCovered: g.nCovered, nNotCovered: g.nNotCovered, nFundingMissing: g.nCoveredNoFunding, server: true });
  };

  // фильтр строк без позиции в модели монитора и строк остатков: те же общие фильтры, что K.list (юрлицо, деск,
  // книги, валюта); books — {id: {entity, desk}}
  C.keep = function (filt, books) {
    filt = filt || {}; books = books || {};
    return x => {
      const b = books[x.book] || {};
      const ent = x.entity || b.entity, desk = x.desk && x.desk !== '—' ? x.desk : b.desk;
      return (!filt.entity || ent === filt.entity) && (!filt.desk || desk === filt.desk) &&
        (!filt.books || !filt.books.length || filt.books.includes(x.book)) && (!filt.ccy || x.ccy === filt.ccy);
    };
  };

  // группы: dim — 'book' | 'entity' | 'ccy' | 'kind'
  C.DIMS = [['book', 'книга'], ['entity', 'юрлицо'], ['ccy', 'валюта'], ['kind', 'тип']];
  C.groups = function (rows, dim, fundingOk) {
    const g = new Map();
    rows.forEach(r => { const k = r[dim] || '—'; if (!g.has(k)) g.set(k, []); g.get(k).push(r); });
    return [...g.entries()].map(([key, list]) => ({ key, rows: list, sums: C.sums(list, fundingOk) }));
  };

  // лидеры и аутсайдеры по нетто (позиции, где нетто есть)
  C.leaders = function (rows, n) {
    n = n || 10;
    const L = rows.filter(r => !r.isCash && r.net != null).sort((a, b) => b.net - a.net);
    return { top: L.filter(r => r.net > 0).slice(0, n), bottom: L.filter(r => r.net < 0).reverse().slice(0, n) };
  };

  // шаги КС: заседания (дата → б.п.) по порядку вступления
  C.meetingList = function (lists) {
    const by = new Map();
    lists.forEach(L => (L || []).forEach(m => { if (m && m.date && !by.has(m.date)) by.set(m.date, { date: m.date, effective: m.effective || m.date, estimated: !!m.estimated }); }));
    return [...by.values()].sort((a, b) => a.effective < b.effective ? -1 : a.effective > b.effective ? 1 : a.date < b.date ? -1 : 1);
  };
  C.stepsFor = (meetings, byDate, asOf) => meetings.filter(m => !asOf || m.effective > asOf).map(m => +(byDate || {})[m.date] || 0);

  root.CARRY = C;
  if (typeof module !== 'undefined' && module.exports) module.exports = C;
})(typeof window !== 'undefined' ? window : globalThis);
