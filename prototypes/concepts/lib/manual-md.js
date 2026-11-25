/*
 * «Ручные рыночные данные» (C22, TDM#652): логика экрана без DOM — разбор введённого, строки таблицы, права на правку и
 * два источника кривых CDS с одним набором вызовов: сервер (api/v1/market-data/manual/cds, backend
 * desk/api/manual_md_routes.py, desk/marketdata/manual_cds.py) и демо в памяти страницы (файл или ?demo=1).
 *
 * Конвенция котировки (решение владельца от 19.11.2026, допущение, которое можно сменить; правится здесь и в backend
 * manual_cds.py): par-спред CDS (par coupon spread), б.п. в год; сроки 3M 6M 1Y 2Y 3Y 5Y 7Y 10Y; доля возмещения хранится
 * с кривой (по умолчанию 25 %, строго меньше 100 %). Валюта котировки — USD, если не задана другая: это допущение
 * разработки, не решение владельца.
 *
 * Имена: строки таблицы склеиваются по name_key, который отдаёт сервер (кривые и CDS-позиции). Свой nameKey нужен только
 * для имени, только что набранного на странице; на редких символах (например «ß», «ς») он может разойтись с casefold
 * сервера — тогда такое имя после сохранения (ответ сервера с его name_key) убирается из «добавленных», дубля нет.
 *
 * Вызовы api (оба источника) возвращают Promise<{ status, body }>, как M.aliasApi: 200 — ответ, 403 — нет прав
 * (страница переходит в «только чтение»), 422 — ввод не принят (body.message), 0 — нет связи.
 *   list(date) · put({ name, date, spreads_bp, recovery, ccy, clear }) · copy({ to_date, from_date, names }) · history(name)
 */
(function (root) {
  'use strict';
  const TENORS = ['3M', '6M', '1Y', '2Y', '3Y', '5Y', '7Y', '10Y'];
  const DEFAULTS = { recovery: 0.25, ccy: 'USD', maxSpread: 10000, maxName: 120, minDate: '2000-01-01', maxDate: '2100-12-31' };
  const CONVENTION = 'Котировка — par-спред CDS (par coupon spread) в базисных пунктах в год; сроки 3M, 6M, 1Y, 2Y, 3Y, 5Y, 7Y, 10Y ' +
    '(срок можно оставить пустым). Доля возмещения хранится вместе с кривой, по умолчанию 25 %, меняется по каждому имени. ' +
    'Это решение владельца от 19.11.2026; если договорённость о котировке изменится (например, upfront и фиксированный купон), ' +
    'хранилище и подпись обновятся. Валюта котировки — USD, если не указана другая: это допущение разработки, не решение владельца. ' +
    'Имя референса — как его пишет отчёт; имена, записанные по-разному (сокращение, форма собственности, кириллица и латиница, дефис), ' +
    'система считает разными.';

  const clean = s => String(s == null ? '' : s).split(/\s+/).filter(Boolean).join(' ');
  // идентификатор имени, как на сервере (backend manual_cds.name_key): NFKC, без невидимых символов Cf, строчные, пробелы в один
  const nameKey = s => clean(String(s == null ? '' : s).normalize('NFKC').replace(/\p{Cf}/gu, '').toLowerCase().replace(/ß/g, 'ss'));
  const num = t => Number(String(t).trim().replace(/\s/g, '').replace(',', '.'));

  // ---------- разбор введённого: { ok, value, error } ----------
  function parseSpread(text, tenor) {
    const t = String(text == null ? '' : text).trim();
    if (!t) return { ok: true, value: null };
    if (!/^[+-]?\d+([.,]\d+)?$/.test(t.replace(/\s/g, ''))) return { ok: false, value: null, error: (tenor ? tenor + ': ' : '') + 'не число (пример: 150 или 150,5)' };
    const v = num(t);
    if (v < 0) return { ok: false, value: null, error: (tenor ? tenor + ': ' : '') + 'спред не бывает отрицательным' };
    if (v > DEFAULTS.maxSpread) return { ok: false, value: null, error: (tenor ? tenor + ': ' : '') + 'больше ' + DEFAULTS.maxSpread + ' б.п. — похоже на опечатку' };
    return { ok: true, value: Math.round(v * 1e4) / 1e4 };
  }
  // доля возмещения вводится в процентах («25» или «25 %»), хранится долей; пусто — по умолчанию (null)
  function parseRecovery(text) {
    const t = String(text == null ? '' : text).replace('%', '').trim();
    if (!t) return { ok: true, value: null };
    if (!/^\d+([.,]\d+)?$/.test(t)) return { ok: false, value: null, error: 'доля возмещения — число от 0 до 100 %' };
    const v = num(t);
    const frac = Math.round(v * 100) / 10000;
    if (frac >= 1) return { ok: false, value: null, error: 'доля возмещения — от 0 до 100 %, не включая 100' };
    return { ok: true, value: frac };
  }
  function parseCcy(text) {
    const t = String(text == null ? '' : text).trim().toUpperCase();
    if (!t) return { ok: true, value: null };
    return /^[A-Z]{3}$/.test(t) ? { ok: true, value: t } : { ok: false, value: null, error: 'валюта — три латинские буквы (USD, EUR)' };
  }
  function parseName(text) {
    const n = clean(text);
    if (!n) return { ok: false, error: 'введите имя референса' };
    if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(String(text)) || /\p{Cf}/u.test(String(text))) return { ok: false, error: 'в имени служебные или невидимые символы' };
    if (n.length > DEFAULTS.maxName) return { ok: false, error: 'имя длиннее ' + DEFAULTS.maxName + ' знаков' };
    return { ok: true, name: n, key: nameKey(n) };
  }

  // ---------- вид числа в ячейке ----------
  const fmtSpread = v => v == null ? "" : String(+(+v).toFixed(4)).replace('.', ',');
  const fmtPct = v => v == null ? '' : String(+(v * 100).toFixed(2)).replace('.', ',');

  // черновик строки — тексты полей ввода; из кривой сервера (или пустой)
  function draftOf(curve) {
    const sp = {};
    TENORS.forEach(t => { sp[t] = curve ? fmtSpread(curve.spreads_bp[t]) : ''; });
    return { spreads: sp, recovery: curve ? fmtPct(curve.recovery) : '', ccy: curve ? curve.ccy : '' };
  }
  // черновик → тело PUT или ошибки по полям: { ok, body, errors: { поле: текст } }
  function readDraft(name, date, d) {
    const errors = {}, spreads = {};
    TENORS.forEach(t => { const r = parseSpread(d.spreads[t], t); if (!r.ok) errors[t] = r.error; else spreads[t] = r.value; });
    const rec = parseRecovery(d.recovery), ccy = parseCcy(d.ccy);
    if (!rec.ok) errors.recovery = rec.error;
    if (!ccy.ok) errors.ccy = ccy.error;
    if (!Object.keys(errors).length && TENORS.every(t => spreads[t] == null)) errors.spreads = 'введите хотя бы один срок';
    return { ok: !Object.keys(errors).length, errors, body: { name, date, spreads_bp: spreads, recovery: rec.value, ccy: ccy.value } };
  }
  // изменён ли черновик против сохранённого (сравнение по смыслу: «150» и «150,0» — одно и то же)
  function isDirty(d, curve) {
    const filled = x => String(x == null ? '' : x).trim() !== '';
    if (!curve) return TENORS.some(t => filled(d.spreads[t])) || filled(d.recovery) || filled(d.ccy);
    const r = readDraft('x', '2000-01-01', d);
    if (!r.ok) return true;
    const b = readDraft('x', '2000-01-01', draftOf(curve)).body, c = r.body;
    return TENORS.some(t => c.spreads_bp[t] !== b.spreads_bp[t]) || (c.recovery == null ? DEFAULTS.recovery : c.recovery) !== b.recovery ||
      (c.ccy || DEFAULTS.ccy) !== b.ccy;
  }

  // ---------- строки таблицы: кривые даты + CDS-позиции прогона + имена, добавленные на странице ----------
  function mergeRows(list, added) {
    const by = new Map();
    ((list && list.curves) || []).forEach(c => by.set(c.name_key, { key: c.name_key, name: c.name, curve: c, positions: 0 }));
    ((list && list.positions) || []).forEach(p => {
      const r = by.get(p.name_key) || { key: p.name_key, name: p.name, curve: null, positions: 0, deals: [] };
      r.positions = p.positions; r.deals = p.deals || []; r.currencies = p.currencies || []; by.set(p.name_key, r);
    });
    (added || []).forEach(n => { const k = nameKey(n); if (!by.has(k)) by.set(k, { key: k, name: clean(n), curve: null, positions: 0 }); });
    return [...by.values()].sort((a, b) => a.name.localeCompare(b.name, 'ru'));
  }
  // имена, которым копирование с прошлой даты что-то даст: кривая есть, введена раньше выбранной даты
  const copyEligible = (rows, date) => rows.filter(r => r.curve && r.curve.valid_from < date).map(r => r.name);

  // ---------- права на правку ----------
  const LOOPBACK = /^(127\.\d+\.\d+\.\d+|localhost|\[?::1\]?)$/i;
  const WHY = {
    viewer: 'ваша роль — «просмотр»: кривые меняют трейдер и администратор',
    network: 'вход в монитор выключен, поэтому кривые меняются только на компьютере, где работает сервер деска (адрес 127.0.0.1)',
    forbidden: 'сервер отказал в правке: кривые меняют трейдер и администратор, а при выключенном входе — только компьютер сервера',
  };
  // ответ /api/v1/auth/state (null — не получен): роль оттуда, без него — по адресу страницы
  function rights(state, hostname) {
    if (state && state.enabled) {
      const role = state.user && state.user.role;
      return role === 'trader' || role === 'admin' ? { editable: true, reason: '' } : { editable: false, reason: role === 'viewer' ? WHY.viewer : WHY.forbidden };
    }
    const local = state ? !!state.loopback : LOOPBACK.test(hostname || '');
    return local ? { editable: true, reason: '' } : { editable: false, reason: WHY.network };
  }

  // ---------- сервер ----------
  function serverApi(fetchFn, base) {
    base = base || '../api/v1/market-data/manual/cds';
    const call = (method, path, body) => Promise.resolve().then(() => fetchFn(base + path, method === 'GET' ? { cache: 'no-store' } :
      { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }))
      .then(r => r.json().catch(() => null).then(j => ({ status: r.status, body: j })))
      .catch(() => ({ status: 0, body: { message: 'нет связи с сервером' } }));
    return {
      demo: false,
      list: date => call('GET', '/curves?date=' + encodeURIComponent(date)),
      put: b => call('PUT', '/curve', b),
      copy: b => call('POST', '/copy', b),
      history: name => call('GET', '/history?name=' + encodeURIComponent(name)),
    };
  }

  // ---------- демо: вымышленные имена, хранение в памяти страницы, те же правила, что у сервера ----------
  const addDays = (iso, n) => { const d = new Date(iso + 'T00:00:00Z'); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  function demoApi(asOf) {
    const revs = []; let id = 0;
    const at = () => new Date().toISOString();
    const spreadsOf = o => Object.fromEntries(TENORS.map(t => [t, o[t] == null ? null : o[t]]));
    const add = (name, validFrom, spreads, rec, ccy, by, cleared) => {
      revs.push({ rev_id: ++id, name_key: nameKey(name), name: clean(name), valid_from: validFrom, cleared: !!cleared, spreads_bp: cleared ? spreadsOf({}) : spreadsOf(spreads),
        recovery: cleared ? DEFAULTS.recovery : rec, ccy: cleared ? DEFAULTS.ccy : ccy, changed_by: by, changed_at: at(), manual: true });
      return revs[revs.length - 1];
    };
    const view = (r, on) => Object.assign({}, r, { spreads_bp: Object.assign({}, r.spreads_bp), entered_on_date: on ? r.valid_from === on : null });
    const inForce = (key, on, strict) => {
      const L = revs.filter(r => r.name_key === key && (strict ? r.valid_from < on : r.valid_from <= on));
      if (!L.length) return null;
      const best = L.reduce((a, b) => (b.valid_from > a.valid_from || (b.valid_from === a.valid_from && b.rev_id > a.rev_id)) ? b : a);
      return best.cleared ? null : best;
    };
    const keys = () => [...new Set(revs.map(r => r.name_key))];
    const ok = body => ({ status: 200, body });
    const bad = message => ({ status: 422, body: { code: 'invalid_parameter', message } });
    // демо-набор: имена вымышленные, кривые введены за несколько дней до даты экрана
    add('Эмитент А', addDays(asOf, -3), { '3M': 85, '6M': 100, '1Y': 130, '2Y': 175, '3Y': 220, '5Y': 285, '7Y': 315, '10Y': 340 }, 0.25, 'USD', 'demo');
    add('Эмитент Б', addDays(asOf, -3), { '1Y': 260, '3Y': 410, '5Y': 520 }, 0.4, 'USD', 'demo');
    add('Эмитент Б', addDays(asOf, -1), { '1Y': 270, '3Y': 425, '5Y': 540 }, 0.4, 'USD', 'demo');
    add('Эмитент В', addDays(asOf, -10), { '5Y': 150, '10Y': 210 }, 0.25, 'EUR', 'demo');
    // CDS-позиции прогона: имя референса — x_issuer отчёта, названия сделок — подсказка
    const POSITIONS = [{ name: 'Эмитент Г', positions: 2, deals: ['CDS Эмитент Г 3Y', 'CDS Эмитент Г 5Y'], currencies: ['RUB'] }, { name: 'Эмитент А', positions: 1, deals: ['CDS Эмитент А 5Y'], currencies: ['USD'] }];
    return {
      demo: true,
      list(date) {
        const curves = keys().map(k => inForce(k, date)).filter(Boolean).sort((a, b) => a.name_key < b.name_key ? -1 : 1);
        const have = new Set(curves.map(c => c.name_key));
        return Promise.resolve(ok({ date, run_id: null, tenors: TENORS.slice(), default_recovery: DEFAULTS.recovery, default_ccy: DEFAULTS.ccy,
          max_spread_bp: DEFAULTS.maxSpread, curves: curves.map(c => view(c, date)),
          positions: POSITIONS.map(p => ({ ...p, name_key: nameKey(p.name), has_curve: have.has(nameKey(p.name)) })) }));
      },
      put(b) {
        const n = parseName(b.name);
        if (!n.ok) return Promise.resolve(bad(n.error));
        if (!(b.date >= DEFAULTS.minDate && b.date <= DEFAULTS.maxDate)) return Promise.resolve(bad('the date is outside ' + DEFAULTS.minDate + '..' + DEFAULTS.maxDate));
        if (b.clear) {
          if (!inForce(n.key, b.date)) return Promise.resolve(ok({ changed: false, curve: null }));
          add(n.name, b.date, {}, null, null, 'demo', true);
          return Promise.resolve(ok({ changed: true, curve: null }));
        }
        const sp = {};
        for (const t of Object.keys(b.spreads_bp || {})) {
          if (TENORS.indexOf(t) < 0) return Promise.resolve(bad('unknown tenor ' + t));
          const r = parseSpread(b.spreads_bp[t] == null ? '' : String(b.spreads_bp[t]), t);
          if (!r.ok) return Promise.resolve(bad(r.error));
          sp[t] = r.value;
        }
        if (TENORS.every(t => sp[t] == null)) return Promise.resolve(bad('a curve needs at least one spread'));
        const rec = b.recovery == null ? DEFAULTS.recovery : +b.recovery;
        if (!(rec >= 0 && Math.round(rec * 1e4) / 1e4 < 1)) return Promise.resolve(bad('the recovery rate must be from 0 up to (not including) 1'));
        const ccy = parseCcy(b.ccy || '');
        if (!ccy.ok) return Promise.resolve(bad(ccy.error));
        const now = inForce(n.key, b.date), c = ccy.value || DEFAULTS.ccy;
        if (now && now.name === n.name && now.recovery === rec && now.ccy === c && TENORS.every(t => (now.spreads_bp[t] == null ? null : now.spreads_bp[t]) === (sp[t] == null ? null : sp[t])))
          return Promise.resolve(ok({ changed: false, curve: view(now, b.date) }));
        return Promise.resolve(ok({ changed: true, curve: view(add(n.name, b.date, sp, rec, c, 'demo'), b.date) }));
      },
      copy(b) {
        for (const d of [b.to_date, b.from_date]) if (d && !(d >= DEFAULTS.minDate && d <= DEFAULTS.maxDate)) return Promise.resolve(bad('the date is outside ' + DEFAULTS.minDate + '..' + DEFAULTS.maxDate));
        if (b.from_date && b.from_date >= b.to_date) return Promise.resolve(bad('the date to copy from must be earlier than the date to copy to'));
        const want = b.names ? [...new Set(b.names.map(nameKey))] : null, copied = [], skipped = [];
        for (const k of want || keys().filter(k => inForce(k, b.from_date || b.to_date, !b.from_date))) {
          const src = b.from_date ? inForce(k, b.from_date) : inForce(k, b.to_date, true);
          if (!src) { skipped.push({ name_key: k, name: (revs.find(r => r.name_key === k) || { name: k }).name, why: 'нет кривой на дату, с которой копируем' }); continue; }
          const own = revs.filter(r => r.name_key === k && r.valid_from === b.to_date).pop();
          // копирование «всех» (без списка имён) снятие на эту дату не отменяет; названное имя — возвращает
          if (own && (!want || !own.cleared)) { skipped.push({ name_key: k, name: src.name, why: own.cleared ? 'на эту дату имя снято' : 'на эту дату кривая уже введена' }); continue; }
          copied.push(view(add(src.name, b.to_date, src.spreads_bp, src.recovery, src.ccy, 'demo'), b.to_date));
        }
        return Promise.resolve(ok({ to_date: b.to_date, copied, skipped }));
      },
      history(name) {
        return Promise.resolve(ok({ name_key: nameKey(name), revisions: revs.filter(r => r.name_key === nameKey(name)).sort((a, b) => b.rev_id - a.rev_id).map(r => view(r)) }));
      },
    };
  }

  root.MANUAL_MD = { TENORS, DEFAULTS, CONVENTION, nameKey, parseSpread, parseRecovery, parseCcy, parseName, fmtSpread, fmtPct, draftOf, readDraft, isDirty,
    mergeRows, copyEligible, rights, WHY, serverApi, demoApi, addDays };
  if (typeof module !== 'undefined' && module.exports) module.exports = root.MANUAL_MD;
})(typeof window !== 'undefined' ? window : globalThis);
