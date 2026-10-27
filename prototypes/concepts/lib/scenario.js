/*
 * Сценарии по Тейлору (C18, trading-desk-monitor#363, шаг 1 переноса Scenario Lab, #72): расчёт без DOM.
 * Страница c18-scenarios.html рисует, этот файл считает (его проверяет tests/js/scenario_c18.test.js).
 *
 * Данные:
 *  - риск прогона по факторам — GET ../api/v1/runs/{run_id}/risk (backend scenario/taylor.py risk_payload, тот же, что у
 *    /app/): factors = IR|<кривая>|<бакет>, CS, FX|<валюта>; у позиции delta [[i, ₽ за единицу]] (IR, CS — ₽/б.п.,
 *    FX — ₽ за 1 %), gamma [[i, j, v]] (i <= j), spread_kind (ofz / credit / null), spread_bp, rating, sector, issuer.
 *    В демо — S.demoPayload из позиций lib/mock.js той же формы (без второго порядка);
 *  - пресеты — GET ../api/v1/scenario-defs (таблица scenario_def); в демо — S.DEMO_PRESETS.
 *
 * Определение сценария — формат backend scenario/definition.py (curves, ofzSpread, spreads{_all,_mult,sector,issuer,
 * rating}, fx, basis). S.normalize — его порт, S.runTaylor — порт run_taylor: те же суммы слева направо в порядке
 * payload; сверка с Python — общая фикстура backend/tests/fixtures/taylor_parity.json (её пишет pytest, test_slice_s6).
 *
 * Правило B1 (S.evaluate): позиция без нужной сценарию чувствительности не даёт 0 — она «не посчитана» и в итоги не
 * входит; итог несёт число таких позиций (miss из n + miss), как M.sumX. Не посчитана позиция:
 *  - монитора, которой нет в риске прогона (не оценена) — при любом непустом сценарии;
 *  - облигация без спредовой чувствительности (spread_kind null), когда шок её спреда не 0;
 *  - позиция с валютной экспозицией (FX-дельта монитора или облигация в валюте) без фактора FX|<валюта>, когда шок
 *    этой валюты не 0 (например, курса для пересчёта дельты не было);
 *  - облигация без ставочного риска, когда сценарий двигает кривые.
 *
 * Для тепловой карты «кривая × бакет» (#364) и групп эмитентов (#365): S.evaluate отдаёт по каждой посчитанной позиции
 * её части, группу эмитента (group — issuer_group риска прогона, #366) и вклад по факторам первого порядка
 * (S.factorParts); сумма по позициям — cells (кривая → 11 бакетов). #365 сворачивает rows по group.
 */
(function (root) {
  'use strict';
  const S = {};
  S.BUCKETS = ['1M', '3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y', '15Y', '20Y+'];
  S.MAX_BP = 5000;
  S.RUB_CURVES = ['RUB OFZ', 'RUB RUONIA', 'RUB KEY'];
  S.PARTS = ['rates', 'spread', 'fx', 'convexity', 'total'];
  S.SPREAD_RULES = ['sector', 'issuer', 'rating'];
  // корзина «без рейтинга» редактора: рейтинга нет у бумаги и эмитента (б/р) или рейтингов в данных нет вовсе
  // (нет данных) — в определение пишется под обоими ключами, как в Scenario Lab
  S.NOT_RATED = 'б/р';
  S.NO_DATA = 'нет данных';
  S.UNRATED_KEYS = [S.NOT_RATED, S.NO_DATA];
  S.NONE = '_none';
  // приближение второго порядка надёжно примерно до ±500 б.п. по позиции (Scenario Lab, docs/SCENARIOS.md)
  S.ACCURACY_BP = 500;

  // ---------- определение (порт scenario/definition.py normalize) ----------
  const fin = x => typeof x === 'number' && isFinite(x);
  function num(x, what, limit) {
    if (!fin(x)) throw new Error(what + ': нужно конечное число, а не ' + JSON.stringify(x));
    if (limit !== null && Math.abs(x) > (limit == null ? S.MAX_BP : limit)) throw new Error(what + ': ' + x + ' за пределами ±' + (limit == null ? S.MAX_BP : limit));
    return x + 0;  // -0 → 0
  }
  function map(x, what) {
    if (x == null) return {};
    if (typeof x !== 'object' || Array.isArray(x) || Object.keys(x).some(k => !k)) throw new Error(what + ': нужен объект с непустыми ключами');
    return x;
  }
  function rules(x, what) {
    const out = {};
    Object.entries(map(x, what)).forEach(([k, v]) => { const n = num(v, what + '.' + k); if (n) out[k] = n; });
    return out;
  }
  const nonEmpty = o => Object.keys(o).length > 0;
  // curves: кривые прогона (payload.curves); шок другой кривой, кроме RUB_CURVES, — ошибка
  S.normalize = function (defn, curves) {
    defn = map(defn, 'definition');
    const extra = Object.keys(defn).filter(k => !['curves', 'ofzSpread', 'spreads', 'fx', 'basis'].includes(k));
    if (extra.length) throw new Error('лишние ключи ' + extra.sort().join(', '));
    const out = {}, shocked = {};
    Object.entries(map(defn.curves, 'curves')).forEach(([cid, v]) => {
      let vec = Array.isArray(v) ? v.slice() : new Array(S.BUCKETS.length).fill(v);
      if (vec.length !== S.BUCKETS.length) throw new Error('curves.' + cid + ': нужно ' + S.BUCKETS.length + ' бакетов, а не ' + vec.length);
      vec = vec.map(x => num(x, 'curves.' + cid));
      if (vec.some(x => x)) shocked[cid] = vec;
    });
    if (curves) {
      const unknown = Object.keys(shocked).filter(c => !curves.includes(c) && !S.RUB_CURVES.includes(c));
      if (unknown.length) throw new Error('кривые ' + unknown.sort().join(', ') + ' прогон не публикует');
    }
    if (nonEmpty(shocked)) out.curves = shocked;
    const ofz = num(defn.ofzSpread || 0, 'ofzSpread');
    if (ofz) out.ofzSpread = ofz;
    const raw = map(defn.spreads, 'spreads');
    const ex2 = Object.keys(raw).filter(k => !['_all', '_mult'].concat(S.SPREAD_RULES).includes(k));
    if (ex2.length) throw new Error('spreads: лишние ключи ' + ex2.sort().join(', '));
    const sp = {};
    const a = num(raw._all || 0, 'spreads._all');
    if (a) sp._all = a;
    if (raw._mult != null) {
      const m = num(raw._mult, 'spreads._mult', null);
      if (m <= 0) throw new Error('spreads._mult: нужен множитель > 0, а не ' + m);
      if (m !== 1) sp._mult = m;
    }
    S.SPREAD_RULES.forEach(k => { const r = rules(raw[k], 'spreads.' + k); if (nonEmpty(r)) sp[k] = r; });
    if (nonEmpty(sp)) out.spreads = sp;
    const fx = {};
    Object.entries(map(defn.fx, 'fx')).forEach(([c, v]) => {
      if (num(v, 'fx.' + c, null) <= -100) throw new Error('fx.' + c + ': нужен сдвиг больше −100 %, а не ' + v);
      if (v) fx[c] = v + 0;
    });
    if (nonEmpty(fx)) out.fx = fx;
    const basis = rules(defn.basis, 'basis');
    if (nonEmpty(basis)) out.basis = basis;
    return out;
  };
  S.isEmpty = norm => !norm || !Object.keys(norm).length;
  // каноническая запись (ключи по алфавиту): сравнить два нормализованных определения
  S.canon = function canon(v) {
    if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
    if (v && typeof v === 'object') return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}';
    return JSON.stringify(v);
  };

  // ds позиции риска, б.п. (own_spread_shock): ОФЗ — ofzSpread; кредит — _all + (_mult − 1) × spread_bp + sector +
  // issuer + rating, слева направо; нет правила или spread_bp null — 0; без вида спреда — 0
  S.ownSpreadShock = function (norm, p) {
    const kind = p.spread_kind;
    if (kind === 'ofz') return norm.ofzSpread || 0;
    if (kind !== 'credit') return 0;
    const s = norm.spreads || {};
    let ds = s._all || 0;
    if (s._mult != null && p.spread_bp != null) ds += (s._mult - 1) * p.spread_bp;
    S.SPREAD_RULES.forEach(k => { if (p[k] != null) ds += (s[k] && s[k][p[k]]) || 0; });
    return ds;
  };

  // ---------- Тейлор (порт run_taylor) ----------
  // факторы payload: kind, кривая и номер бакета (IR), валюта (FX)
  S.factors = function (payload) {
    if (payload._f) return payload._f;
    const f = payload.factors.map(k => {
      const i = k.indexOf('|'), kind = i < 0 ? k : k.slice(0, i), rest = i < 0 ? '' : k.slice(i + 1);
      if (kind === 'IR') { const j = rest.lastIndexOf('|'); return { kind, curve: rest.slice(0, j), bucket: (payload.buckets || S.BUCKETS).indexOf(rest.slice(j + 1)) }; }
      return kind === 'FX' ? { kind, ccy: rest } : { kind };
    });
    Object.defineProperty(payload, '_f', { value: f, enumerable: false, configurable: true });
    return f;
  };
  // вектор шока по факторам без CS (его ds у каждой позиции свой): IR — сдвиг кривой в бакете (нет кривой — 0), FX — %
  S.baseVector = function (payload, norm) {
    const curves = norm.curves || {}, fx = norm.fx || {};
    return S.factors(payload).map(f => f.kind === 'IR' ? (curves[f.curve] ? curves[f.curve][f.bucket] : 0) : f.kind === 'FX' ? (fx[f.ccy] || 0) : 0);
  };
  S.zero = () => ({ rates: 0, spread: 0, fx: 0, convexity: 0, total: 0 });
  S.runTaylor = function (payload, norm) {
    const F = S.factors(payload), kinds = F.map(f => f.kind), base = S.baseVector(payload, norm);
    let irMax = 0;
    kinds.forEach((k, i) => { if (k === 'IR' && Math.abs(base[i]) > irMax) irMax = Math.abs(base[i]); });
    const out = [], totals = { _total: S.zero() };
    payload.positions.forEach(p => {
      const ds = S.ownSpreadShock(norm, p);
      const x = kinds.map((k, i) => k === 'CS' ? ds : base[i]);
      let rates = 0, spread = 0, fxp = 0, conv = 0;
      p.delta.forEach(([i, d]) => { if (kinds[i] === 'IR') rates += d * x[i]; else if (kinds[i] === 'CS') spread += d * x[i]; else fxp += d * x[i]; });
      p.gamma.forEach(([i, j, v]) => { conv += (i === j ? 0.5 : 1.0) * v * x[i] * x[j]; });
      const row = { rates, spread, fx: fxp, convexity: conv, total: rates + spread + fxp + conv };
      out.push(Object.assign({ position_id: p.position_id }, row, { second_order: p.gamma.length > 0, max_move_bp: Math.max(irMax, Math.abs(ds)) }));
      [p.book_id, '_total'].forEach(b => { const t = totals[b] || (totals[b] = S.zero()); S.PARTS.forEach(k => { t[k] += row[k]; }); });
    });
    return { positions: out, totals };
  };
  // вклад позиции по факторам первого порядка, ₽: [[i, d × x_i]] (для «кривая × бакет», #364); сумма по IR — rates
  S.factorParts = function (payload, norm, p, base) {
    const F = S.factors(payload), b = base || S.baseVector(payload, norm), ds = S.ownSpreadShock(norm, p);
    return p.delta.map(([i, d]) => [i, d * (F[i].kind === 'CS' ? ds : b[i])]);
  };

  // ---------- правило B1 и итоги ----------
  const BOND_GROUPS = ['GOV', 'SUB', 'CORP'];
  S.isBond = info => !!info && (info.instrType === 'BOND' || BOND_GROUPS.includes(info.group));
  // валюты, к курсу которых позиция чувствительна по данным монитора: FX-дельта (ноги) и валюта облигации
  S.fxExposure = info => {
    if (!info) return [];
    const out = Object.keys(info.fxDeltaBy || {}).filter(c => c !== 'RUB' && info.fxDeltaBy[c]);
    if (S.isBond(info) && info.ccy && info.ccy !== 'RUB' && !out.includes(info.ccy)) out.push(info.ccy);
    return out;
  };
  S.REASON = {
    not_valued: 'не оценена: чувствительностей в риске прогона нет',
    no_spread: 'нет спредовой чувствительности (CS01), а шок её спреда не 0',
    no_fx: 'нет FX-дельты по валюте, а шок курса не 0',
    no_rates: 'нет ставочного риска, а сценарий двигает кривые',
  };
  // почему позиция не посчитана в сценарии (null — посчитана). p — позиция риска (null — её нет в риске прогона),
  // info — позиция монитора (группа, тип, валюта, FX-дельта по валютам)
  S.missing = function (norm, p, info, F) {
    if (S.isEmpty(norm)) return null;
    if (!p) return { code: 'not_valued', text: S.REASON.not_valued };
    F = F || [];
    const has = kind => p.delta.some(([i]) => F[i] && F[i].kind === kind);
    if (S.isBond(info) && !p.spread_kind) {
      // вид спреда по монитору: рублёвые ОФЗ — спред ОФЗ, остальное — кредитный
      const guess = info.group === 'GOV' && (info.ccy || 'RUB') === 'RUB' ? 'ofz' : 'credit';
      if (S.ownSpreadShock(norm, Object.assign({}, p, { spread_kind: guess }))) return { code: 'no_spread', text: S.REASON.no_spread };
    }
    const fx = norm.fx || {};
    const lacks = S.fxExposure(info).filter(c => fx[c] && !p.delta.some(([i]) => F[i] && F[i].kind === 'FX' && F[i].ccy === c));
    if (lacks.length) return { code: 'no_fx', text: S.REASON.no_fx + ' (' + lacks.join(', ') + ')', ccys: lacks };
    if (S.isBond(info) && norm.curves && !has('IR')) return { code: 'no_rates', text: S.REASON.no_rates };
    return null;
  };
  // Итоги сценария по правилу B1. opt.ids — позиции монитора на экране (после фильтров шапки), по порядку;
  // opt.info(id) — позиция монитора. Позиции риска вне ids не входят никуда.
  // rows: по позиции {id, book, parts | null, why, maxMove, second}; byBook / total: части по посчитанным позициям,
  // n — посчитано, miss — не посчитано; cells: кривая → вклад по бакетам (первый порядок), byFactor: фактор → вклад;
  // spreadOfz / spreadCredit — спредовая часть по виду спреда; fxBy: валюта → вклад.
  S.evaluate = function (payload, norm, opt) {
    opt = opt || {};
    const info = opt.info || (() => null);
    const F = S.factors(payload), base = S.baseVector(payload, norm), T = S.runTaylor(payload, norm);
    const byId = new Map(payload.positions.map((p, i) => [p.position_id, i]));
    const ids = opt.ids || payload.positions.map(p => p.position_id);
    const acc = () => Object.assign(S.zero(), { n: 0, miss: 0 });
    const res = { rows: [], byBook: {}, total: acc(), cells: {}, byFactor: {}, spreadOfz: 0, spreadCredit: 0, fxBy: {}, missing: [], approx: 0 };
    ids.forEach(id => {
      const i = byId.get(id), p = i == null ? null : payload.positions[i], m = info(id);
      const book = p ? p.book_id : (m && m.book) || '—';
      const why = S.missing(norm, p, m, F);
      const bk = res.byBook[book] || (res.byBook[book] = acc());
      if (why) { bk.miss++; res.total.miss++; const r = { id, book, parts: null, why }; res.rows.push(r); res.missing.push(r); return; }
      if (!p) { bk.n++; res.total.n++; res.rows.push({ id, book, parts: S.zero(), why: null, maxMove: 0, second: false }); return; }  // пустой сценарий
      const t = T.positions[i], parts = { rates: t.rates, spread: t.spread, fx: t.fx, convexity: t.convexity, total: t.total };
      [bk, res.total].forEach(a => { S.PARTS.forEach(k => { a[k] += parts[k]; }); a.n++; });
      if (t.max_move_bp > S.ACCURACY_BP) res.approx++;
      res.rows.push({ id, book, parts, why: null, maxMove: t.max_move_bp, second: t.second_order, spreadKind: p.spread_kind, group: p.issuer_group == null ? null : p.issuer_group });
      S.factorParts(payload, norm, p, base).forEach(([j, v]) => {
        if (!v) return;
        const f = F[j];
        res.byFactor[payload.factors[j]] = (res.byFactor[payload.factors[j]] || 0) + v;
        if (f.kind === 'IR') (res.cells[f.curve] || (res.cells[f.curve] = new Array(S.BUCKETS.length).fill(0)))[f.bucket] += v;
        else if (f.kind === 'FX') res.fxBy[f.ccy] = (res.fxBy[f.ccy] || 0) + v;
        else if (p.spread_kind === 'ofz') res.spreadOfz += v; else res.spreadCredit += v;
      });
    });
    return res;
  };
  // топ позиций по убытку: посчитанные с итогом < 0, худшие первыми
  S.worst = (res, n) => res.rows.filter(r => r.parts && r.parts.total < 0).sort((a, b) => a.parts.total - b.parts.total).slice(0, n || 15);

  // ---------- редактор ----------
  // состояние: base — пресет, с которого начат (его кривые, базис и правила по эмитентам берутся как есть),
  // fx {валюта: %}, ofz, all — б.п., mult — множитель, rating {грейд | _none: б.п.}, sector {сектор: б.п.}
  S.emptyEditor = () => ({ base: null, fx: {}, ofz: 0, all: 0, mult: 1, rating: {}, sector: {} });
  const clean = o => { const r = {}; Object.keys(o || {}).forEach(k => { const v = +o[k]; if (isFinite(v) && v) r[k] = v; }); return r; };
  S.cleanEditor = function (ed) {
    const e = Object.assign(S.emptyEditor(), ed && typeof ed === 'object' ? ed : {});
    const n = (v, d) => { v = +v; return isFinite(v) ? v : d; };
    return { base: e.base == null ? null : String(e.base), fx: clean(e.fx), ofz: n(e.ofz, 0), all: n(e.all, 0), mult: n(e.mult, 1) > 0 ? n(e.mult, 1) : 1, rating: clean(e.rating), sector: clean(e.sector) };
  };
  // определение редактора (формат definition.py); preset — определение пресета base (или null)
  S.editorDef = function (ed, preset) {
    ed = S.cleanEditor(ed);
    const d = {}, b = preset || {};
    if (b.curves) d.curves = JSON.parse(JSON.stringify(b.curves));
    if (b.basis) d.basis = Object.assign({}, b.basis);
    if (nonEmpty(ed.fx)) d.fx = Object.assign({}, ed.fx);
    if (ed.ofz) d.ofzSpread = ed.ofz;
    const sp = {};
    if (ed.all) sp._all = ed.all;
    if (ed.mult !== 1) sp._mult = ed.mult;
    const rating = {};
    Object.keys(ed.rating).forEach(k => { if (k === S.NONE) S.UNRATED_KEYS.forEach(u => { rating[u] = ed.rating[k]; }); else rating[k] = ed.rating[k]; });
    if (nonEmpty(rating)) sp.rating = rating;
    if (nonEmpty(ed.sector)) sp.sector = Object.assign({}, ed.sector);
    if (b.spreads && b.spreads.issuer && nonEmpty(b.spreads.issuer)) sp.issuer = Object.assign({}, b.spreads.issuer);
    if (nonEmpty(sp)) d.spreads = sp;
    return d;
  };
  // редактор из пресета: его FX и спреды — в поля редактора, кривые, базис и эмитенты остаются в пресете (base)
  S.editorFrom = function (id, def) {
    const ed = S.emptyEditor(), s = (def && def.spreads) || {};
    ed.base = id == null ? null : String(id);
    ed.fx = clean(def && def.fx); ed.ofz = +(def && def.ofzSpread) || 0; ed.all = +s._all || 0; ed.mult = s._mult > 0 ? +s._mult : 1;
    Object.keys(s.rating || {}).forEach(k => { if (S.UNRATED_KEYS.includes(k)) { if (s.rating[k]) ed.rating[S.NONE] = +s.rating[k]; } else ed.rating[k] = +s.rating[k]; });
    ed.rating = clean(ed.rating); ed.sector = clean(s.sector);
    return ed;
  };
  // состояние в адресе (#s=…) и обратно; битое — null
  S.encode = ed => encodeURIComponent(JSON.stringify(S.cleanEditor(ed)));
  S.decode = function (s) {
    if (!s) return null;
    try { const o = JSON.parse(decodeURIComponent(s)); return o && typeof o === 'object' && !Array.isArray(o) ? S.cleanEditor(o) : null; } catch (e) { return null; }
  };
  S.fxAfter = (rate, pct) => rate == null || !isFinite(rate) ? null : rate * (1 + (+pct || 0) / 100);

  // корзины редактора по позициям риска с кредитным спредом (на экране): грейды по шкале, «без рейтинга» последней;
  // секторы по алфавиту, «нет данных» последним. CS01 корзины — сумма дельт CS, ₽/б.п.
  S.buckets = function (payload, keep, rank) {
    const F = S.factors(payload), cs = p => p.delta.reduce((a, [i, d]) => a + (F[i].kind === 'CS' ? d : 0), 0);
    const rating = {}, sector = {}, ccy = {};
    let ofz = 0, credit = 0, nCredit = 0;
    payload.positions.forEach(p => {
      if (keep && !keep(p.position_id)) return;
      p.delta.forEach(([i, d]) => { if (F[i].kind === 'FX') ccy[F[i].ccy] = (ccy[F[i].ccy] || 0) + d; });
      if (p.spread_kind === 'ofz') { ofz += cs(p); return; }
      if (p.spread_kind !== 'credit') return;
      const v = cs(p), r = p.rating == null || S.UNRATED_KEYS.includes(p.rating) || (rank && rank(p.rating) >= 99) ? S.NONE : p.rating;
      credit += v; nCredit++;
      (rating[r] = rating[r] || { cs01: 0, n: 0 }).cs01 += v; rating[r].n++;
      const s = p.sector == null ? S.NO_DATA : p.sector;
      (sector[s] = sector[s] || { cs01: 0, n: 0 }).cs01 += v; sector[s].n++;
    });
    const rk = rank || (() => 0);
    const ratings = Object.keys(rating).sort((a, b) => (a === S.NONE) - (b === S.NONE) || rk(a) - rk(b) || a.localeCompare(b));
    const sectors = Object.keys(sector).sort((a, b) => (a === S.NO_DATA) - (b === S.NO_DATA) || a.localeCompare(b, 'ru'));
    const fxCcys = F.filter(f => f.kind === 'FX').map(f => f.ccy);
    return { rating, sector, ratings, sectors, ofzCs01: ofz, creditCs01: credit, nCredit, fxDelta: ccy, fxCcys };
  };

  // ---------- демо: риск прогона из позиций lib/mock.js ----------
  // k — курс показа (M.toRepr.RUB): суммы монитора делятся на него, чтобы payload был в рублях, как у сервера
  S.demoPayload = function (M, k) {
    k = k || 1;
    const curves = [...new Set(M.curves.map(c => c.id).concat(S.RUB_CURVES))].sort();
    const firsts = [], seen = new Set(), valued = M.positions.filter(p => !p.notValued);
    valued.forEach(p => {
      const d = {};
      Object.keys(p.risk || {}).forEach(c => (p.risk[c] || []).forEach((x, b) => { if (x) d['IR|' + c + '|' + S.BUCKETS[b]] = x / k; }));
      const cs = (p.cs01 || p.ofzSpr01 || 0) / k;
      if (cs) d.CS = cs;
      Object.keys(p.fxDeltaBy || {}).forEach(c => { const x = p.fxDeltaBy[c]; if (x && c !== 'RUB' && M.fx[c]) d['FX|' + c] = x * M.fx[c] / 100; });
      firsts.push(d); Object.keys(d).forEach(x => seen.add(x));
    });
    const factors = curves.flatMap(c => S.BUCKETS.map(b => 'IR|' + c + '|' + b)).concat(['CS'], [...seen].filter(x => x.startsWith('FX|')).sort()).filter(x => seen.has(x));
    const idx = Object.fromEntries(factors.map((x, i) => [x, i]));
    const iss = id => (M.issuer && M.issuer[id]) || {};
    return {
      run_id: null, business_date: M.asOf, buckets: S.BUCKETS.slice(), curves, factors,
      positions: valued.map((p, n) => ({
        position_id: p.id, book_id: p.book, secid: p.isin || null, name: p.name, quality: p.quality || null, issuer: p.issuer || null,
        issuer_group: p.issuerGroup || iss(p.issuer).group || (p.issuer ? iss(p.issuer).name || p.issuer : null),
        sector: p.sector || iss(p.issuer).sector || null, rating: p.rating || iss(p.issuer).rating || null,
        spread_kind: p.ofzSpr01 ? 'ofz' : p.cs01 ? 'credit' : null, spread_bp: p.zspread != null ? p.zspread : null, pv_rub: p.mv != null ? p.mv / k : null,
        delta: Object.entries(firsts[n]).map(([f, x]) => [idx[f], x]).sort((a, b) => a[0] - b[0]), gamma: [],
      })),
    };
  };
  // пресеты демо (в рабочем режиме — GET /api/v1/scenario-defs: миграции 0002 и 0008 хранилища)
  const flat = v => new Array(11).fill(v);
  S.DEMO_PRESETS = [
    { scenario_id: 'par+100', name: 'Параллельный +100 б.п. (RUB)', kind: 'preset', description: 'Все рублёвые кривые +100 б.п.', definition: { curves: { 'RUB OFZ': flat(100), 'RUB RUONIA': flat(100), 'RUB KEY': flat(100) } } },
    { scenario_id: 'keyhike', name: 'Повышение КС +200 б.п.', kind: 'preset', description: 'Короткий конец сильнее, спреды шире', definition: { curves: { 'RUB OFZ': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35], 'RUB RUONIA': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35], 'RUB KEY': [200, 195, 180, 150, 105, 85, 65, 55, 45, 40, 35] }, ofzSpread: 10, spreads: { _all: 25 } } },
    { scenario_id: 'rubdep', name: 'Ослабление рубля −15%', kind: 'preset', description: 'Валюты +15 %, ставки +50 б.п.', definition: { curves: { 'RUB OFZ': flat(50), 'RUB RUONIA': flat(50), 'RUB KEY': flat(50) }, ofzSpread: 10, fx: { CNY: 15, EUR: 15, USD: 15 } } },
    { scenario_id: 'credit', name: 'Кредитный стресс: спреды ×2', kind: 'preset', description: 'Кредитные спреды вдвое, спред ОФЗ +40 б.п.', definition: { ofzSpread: 40, spreads: { _mult: 2 } } },
    { scenario_id: 'sector', name: 'Стресс девелоперов и лизинга', kind: 'preset', description: 'Спреды по сектору и рейтингу', definition: { spreads: { sector: { 'Девелопмент': 300, 'Лизинг': 150 }, rating: { A: 80, BB: 250 } } } },
    { scenario_id: 'stress22', name: 'Стресс «2022»', kind: 'preset', description: 'Ставки до +1000 б.п., валюты +30–35 %, спреды ×2.5', definition: { curves: { 'RUB OFZ': [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200], 'RUB RUONIA': [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200], 'RUB KEY': [1000, 950, 800, 650, 480, 400, 320, 270, 240, 220, 200] }, ofzSpread: -20, fx: { CNY: 30, EUR: 32, USD: 35 }, spreads: { _mult: 2.5 } } },
  ];

  // ---------- загрузка (рабочий режим) ----------
  S.riskUrl = runId => '../api/v1/runs/' + encodeURIComponent(runId) + '/risk';
  S.defsUrl = () => '../api/v1/scenario-defs';
  S.load = function (url, opts) {
    opts = opts || {};
    const f = opts.fetch || root.fetch;
    return f(url, { signal: opts.signal, headers: { Accept: 'application/json' } }).then(r => {
      if (r.ok) return r.json();
      return r.text().then(t => {
        let msg = t;
        try { const j = JSON.parse(t); msg = (j.error && (j.error.message || j.error.detail)) || j.detail || j.message || t; } catch (e) { }
        throw Object.assign(new Error(r.status + ': ' + msg), { status: r.status, text: String(msg) });
      });
    });
  };

  root.SCENARIO = S;
  if (typeof module !== 'undefined' && module.exports) module.exports = S;
})(typeof window !== 'undefined' ? window : globalThis);
