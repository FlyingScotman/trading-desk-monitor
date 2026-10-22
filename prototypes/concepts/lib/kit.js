/*
 * KIT — общие компоненты концептов (нужны mock.js и ../echarts.min.js).
 *
 *  KIT.f.*                — форматирование (тыс ₽/бп, mm, bn, б.п., даты)
 *  KIT.top({title, tag})  — шапка: дата среза, фильтр книг и портфелей (общий для всех мониторов), поиск, тема
 *  KIT.list()             — позиции после фильтра шапки; KIT.onFilter(fn) — перерисовка при смене фильтра
 *  KIT.d(spec)            — кликабельная цифра: spec = {title, list|rows, val, fmt, unit, v?}; щелчок раскрывает вклад
 *  KIT.drill(spec)        — открыть разбор вклада (позиции, группировка по эмитенту/книге/валюте/типу/бакету)
 *  KIT.card(id)           — карточка позиции: параметры, графики истории, риск по бакетам, потоки, PnL
 *  KIT.table(el, opts)    — таблица: группы, мультисортировка (Shift+щелчок; группы сортируются тем же столбцом), поиск, итоги с разбором
 *  KIT.tips, KIT.hint     — подсказки к мерам риска (конвенция DV01 RUONIA, Базис01, CS01, Z к RUONIA) и подпись с подсказкой
 *  KIT.chart(el, option)  — echarts с цветами темы; KIT.riskStack(el, opts) — риск по бакетам: стек кривых + итог + кумулятив
 *  KIT.seg(el, items, cur, fn) — переключатель режимов (таблица / график и т. п.)
 *  KIT.palette()          — поиск по всему (Ctrl+K или «/»): позиции, эмитенты, книги, команды страницы
 *  KIT.aliasName(p), KIT.aliasBind(el, p, onSaved) — имя бумаги в карточке: алиас и серое биржевое имя, ✎ — правка на
 *                           месте, ⟲ — история; KIT.setAlias(isin, alias) — подставить алиас во все позиции бумаги (R33)
 */
(function () {
  'use strict';
  const M = window.MOCK;
  const K = window.KIT = {};
  // группы бумаг в порядке показа: демо знает GOV/CORP/DER, прогон из базы — ещё SUB (субфедеральные) и OTH
  K.GROUPS = ['GOV', 'SUB', 'CORP', 'DER', 'OTH'];
  M.groupLabel = Object.assign({ GOV: 'Гос', SUB: 'Суб', CORP: 'Корп', DER: 'Деривативы', OTH: 'Прочее' }, M.groupLabel);
  // Минфин РФ: в демо — эмитент MINFIN, в прогоне — эмитент с флагом gov статики хаба
  M.isMinfin = M.isMinfin || (id => id === 'MINFIN' || !!(M.issuer && M.issuer[id] && M.issuer[id].gov));

  // ---------- валюта отчёта: все суммы деска (MV, риск, PnL, кэрри, потоки) пересчитываются по споту ----------
  K.REPRS = ['RUB', 'USD', 'EUR', 'CNY'];
  K.SYM = { RUB: '₽', USD: '$', EUR: '€', CNY: '¥' };
  K.repr = 'RUB';
  try { const r = localStorage.getItem('concepts-repr'); if (r && M.fx[r]) K.repr = r; } catch (e) { }
  M.repr = K.repr;
  M.toRepr = {}; Object.keys(M.fx).forEach(c => M.toRepr[c] = M.fx[c] / M.fx[K.repr]);
  if (K.repr !== 'RUB') (function convert(k) {
    const sc = (o, keys) => keys.forEach(x => { if (o[x] != null) o[x] *= k; });
    const arr = a => a && a.forEach((v, i) => a[i] = v * k);
    M.positions.forEach(p => {
      sc(p, ['mv', 'accrued', 'carryDay', 'rollDay', 'dv01', 'basis01', 'cs01', 'notionalRub', 'pnlMtd', 'pnlYtd']);
      arr(p.cs01b); Object.keys(p.risk).forEach(c => arr(p.risk[c])); Object.keys(p.dv01ByCurve).forEach(c => p.dv01ByCurve[c] *= k);
      Object.keys(p.pnl).forEach(x => { if (typeof p.pnl[x] === 'number') p.pnl[x] *= k; });  // null (нет PnL) остаётся null
    });
    const H = M.hist; [H.dv01, H.dv01Book, H.mvType, H.mvCcy, H.pnl].forEach(g => Object.keys(g).forEach(x => arr(g[x]))); arr(H.cs01);
    M.cashflows.forEach(c => c.amountRub *= k);
    M.universeRub.forEach(b => b.size *= k);
  })(1 / M.fx[K.repr]);
  K.setRepr = c => { try { localStorage.setItem('concepts-repr', c); } catch (e) { } location.reload(); };
  // подписи «₽» в разметке страниц заменяются символом валюты отчёта
  if (K.repr !== 'RUB') {
    const sym = K.SYM[K.repr], re = /₽/g;
    const fixNode = n => { if (n.nodeValue.indexOf('₽') >= 0) n.nodeValue = n.nodeValue.replace(re, sym); };
    const fix = root => { const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let n; while ((n = w.nextNode())) fixNode(n); };
    new MutationObserver(ms => ms.forEach(m => { m.addedNodes.forEach(n => n.nodeType === 3 ? fixNode(n) : n.nodeType === 1 && fix(n)); if (m.type === 'characterData') fixNode(m.target); }))
      .observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    document.addEventListener('DOMContentLoaded', () => fix(document.body));
  }

  // ---------- форматирование ----------
  // формат чисел: запятая — разделитель тысяч, точка — дробной части (1,234.56)
  const grp = (v, d) => { if (v == null || !isFinite(v)) return '—'; const s = Math.abs(v).toFixed(d || 0).split('.'); s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, ','); return (v < 0 && /[1-9]/.test(s.join('')) ? '−' : '') + s.join('.'); };
  // в валюте отчёта, отличной от рубля, суммы в тысячах мелкие: показываем с одним знаком после точки
  const KD = K.repr === 'RUB' ? 0 : 1;
  const sgn = (v, d) => (v > 0 ? '+' : '') + grp(v, d);
  const f = K.f = {
    n: grp, s: sgn,
    k: v => grp(v / 1e3, KD),                     // тыс
    ks: v => sgn(v / 1e3, KD),
    mn: (v, d) => grp(v / 1e6, d == null ? 1 : d),
    mns: (v, d) => sgn(v / 1e6, d == null ? 1 : d),
    bn: (v, d) => grp(v / 1e9, d == null ? 2 : d),
    bp: v => v == null ? '—' : grp(v, 0),
    bps: v => v == null ? '—' : sgn(v, 0),
    pct: (v, d) => v == null ? '—' : grp(v, d == null ? 2 : d) + '%',
    px: v => v == null ? '—' : grp(v, 3),
    money: v => { if (v == null || !isFinite(v)) return '—'; const a = Math.abs(v); return a >= 1e9 ? grp(v / 1e9, 2) + ' bn' : a >= 1e6 ? grp(v / 1e6, 1) + ' mm' : a >= 1e3 ? grp(v / 1e3, 0) + 'k' : grp(v, 0); },
    moneys: v => (v > 0 ? '+' : '') + f.money(v),
    dv: v => grp(v / 1e3, KD),                    // DV01 в тыс ₽/бп
    dvs: v => sgn(v / 1e3, KD),
    date: s => s ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(2, 4) : '—',
    dshort: s => { if (!s) return '—'; const m = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']; return (+s.slice(8, 10)) + ' ' + m[+s.slice(5, 7) - 1]; },
    yrs: t => t == null ? '—' : t < 1 ? Math.round(t * 12) + 'м' : grp(t, 1) + 'г',
    cls: v => v > 0 ? 'pos' : v < 0 ? 'neg' : '',
  };
  // заседание ЦБ одной фразой: консенсус и ожидание кривой, если они есть в данных, иначе ставка и прошлое решение
  K.cbrLine = () => {
    const c = M.cbr || {}, n = c.next || {}, last = (c.past || [])[0];
    const known = v => v && v !== '—' && v !== 'нет данных';
    if (known(n.consensus) || known(n.priced)) return 'консенсус ' + K.esc(n.consensus) + ', в кривой ' + K.esc(n.priced) + '.';
    return (c.keyRate != null ? 'ключевая ' + f.pct(c.keyRate) : '') + (last ? ' (' + f.date(last.date) + ': ' + K.esc(last.decisionText || last.decision) + ')' : '') +
      '; консенсуса и ожиданий кривой в данных нет.';
  };
  // курс PnL FX одной фразой (R26): по какому курсу посчитан FX (рыночный MOEX 19:00, иначе 14:00 / CETS / ЦБ — по
  // валюте) и курс ЦБ, действующий на дату, рядом; demo — пусто
  K.fxLine = (ccys) => {
    const P = M.fxPnl, C = M.fxCbr;
    if (!P) return '';
    const cs = (ccys || M.ccys).filter(c => c !== 'RUB' && P.rate[c] != null);
    if (!cs.length) return '';
    return 'PnL FX по курсу ' + cs.map(c => c + ' ' + f.n(P.rate[c], c === 'CNY' ? 4 : 2) + ' ' + f.s(P.chgPct[c], 2) + '%' +
      (P.source && P.source[c] && P.source[c] !== P.label ? ' (' + K.esc(P.source[c]) + ')' : '') +
      (C && C !== P && C.rate[c] != null ? ' · ЦБ ' + f.n(C.rate[c], c === 'CNY' ? 4 : 2) + ' ' + f.s(C.chgPct[c], 2) + '%' : '')).join(', ') +
      ' — ' + K.esc(P.label) + (C && C !== P ? '; курс ЦБ (действующий на дату) — для официального PnL' : '');
  };
  // разрез, которого в данных нет (секторов и рейтингов в статике хаба нет): у всех позиций «—» / «без рейтинга»
  K.unknownDim = v => v == null || v === '—' || v === 'нет данных' || v === 'без рейтинга';
  // рейтинги (R25): грейды национальной шкалы по убыванию, корзины деска и их цвета; «б/р» — рейтинга нет у бумаги и
  // эмитента (рейтинги в статике есть), «нет данных» / «—» — рейтингов в данных нет вовсе
  K.RATINGS = ['AAA', 'AA+', 'AA', 'AA-', 'A+', 'A', 'A-', 'BBB+', 'BBB', 'BBB-', 'BB+', 'BB', 'BB-', 'B+', 'B', 'B-', 'CCC+', 'CCC', 'CCC-', 'CC', 'C', 'RD', 'SD', 'D'];
  K.RATING_BUCKETS = [['AAA', '#4c9dff'], ['AA', '#22c3d6'], ['A', '#3fb67a'], ['BBB', '#e3b341'], ['≤BB', '#f0645a'], ['б/р', '#8a96a3']];
  K.ratingRank = r => { const i = K.RATINGS.indexOf(r); return i < 0 ? 99 : i; };
  K.ratingBucket = r => K.RATINGS.indexOf(r) < 0 ? 'б/р' : r === 'AAA' ? 'AAA' : /^AA[+-]?$/.test(r) ? 'AA' : /^A[+-]?$/.test(r) ? 'A' : /^BBB/.test(r) ? 'BBB' : '≤BB';
  K.ratingColor = r => (K.RATING_BUCKETS.find(b => b[0] === K.ratingBucket(r)) || K.RATING_BUCKETS[5])[1];
  const RB_BASIS = { issue: 'рейтинг выпуска', issuer: 'рейтинг эмитента', issuer_bonds: 'уровень эмитента по выпускам' };
  // «AA- · АКРА» и подсказка: все агентства (как пишут рейтинг, дата, прогноз), чей рейтинг
  K.ratingText = (r, info) => !r || r === '—' ? '—' : info && info.agency ? r + ' · ' + info.agency : r;
  K.ratingTip = (r, info) => !info ? (r === 'б/р' ? 'нет рейтинга АКРА / Эксперт РА / НКР / НРА у бумаги и эмитента в статике хаба' : '') :
    (info.agencies || []).map(a => a[0] + ' ' + a[1] + (a[3] ? ' от ' + f.date(a[3]) : '') + (a[4] ? ', прогноз ' + a[4] : '')).join('; ') + (info.basis ? ' · ' + (RB_BASIS[info.basis] || info.basis) : '') + ' · лучший по национальной шкале';
  K.dimMissing = (list, key) => list.length > 0 && list.every(p => K.unknownDim(p[key]));
  // панель без данных: «нет данных» и причина вместо пустого графика или нулей
  K.noData = (el, why) => { if (el) el.innerHTML = '<div class="k-empty"><b>нет данных</b>' + (why ? '<br><span class="mut">' + K.esc(why) + '</span>' : '') + '</div>'; };
  // PnL MTD / YTD, посчитанные не за весь период (прогонов нет за часть дней, позиции в базе позже начала): метка «*»
  K.partial = key => {
    const q = M.pnlPeriods && M.pnlPeriods[key];
    if (!q || q.complete) return '';
    const t = 'частично: сумма ' + q.runs + ' прогонов' + (q.from ? ' с ' + f.date(q.from) : '') + ((q.why && q.why.length) ? '; ' + q.why.join('; ') : '');
    return '<sup class="mut3" title="' + K.esc(t) + '" style="cursor:help">*</sup>';
  };
  K.esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const esc = K.esc;
  const h = (tag, cls, html) => { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; };
  K.h = h;

  // ---------- подсказки к мерам риска (R29, R30): в какой конвенции риск RUONIA и почему базис ОФЗ с ним не совпадает ----------
  // Так считает монитор на ядре (backend: orchestrator передаёт ядру уже построенную кривую RUONIA OIS, поэтому бамп —
  // нулевой ставки, а не котировок; valuation/bonds.py: Z-спред годовой, ACT/365F). Сверка на прогоне фикстур —
  // docs/program/OWNER_REQUESTS.md R29: у фикс-бумаг Базис01 / DV01 = 1 / (1 + R) с точностью 0,2 %.
  K.tips = {
    ruonia: 'Риск RUONIA (R30) — в нулевых ставках, не в котировках OIS: к непрерывной нулевой ставке кривой RUONIA OIS ' +
      '(бутстрэп котировок MOEX OIS RUONIA, log-linear по DF, flat-forward за последним узлом, время ACT/365F) ' +
      'прибавляется +1 б.п. по 11 бакетам 1M…20Y+ с треугольными весами (на любом сроке сумма весов — 1), центральная разность. ' +
      'Котировки не бампаются, кривая не пересобирается, Z-спред бумаги держится. Кривая КС (проекция флоатеров на КС) — так же. ' +
      'В день без кривой RUONIA бумаги дисконтируются по КБД, и этот риск — к КБД.',
    basis01: 'Базис01 (ОФЗ − RUONIA), тыс ₽/бп: +1 б.п. к Z-спреду ОФЗ к RUONIA разом по всем срокам; кривая RUONIA стоит. ' +
      'Z-спред — годовое начисление, ACT/365F, прибавляется к годовой нулевой ставке RUONIA; по бакетам — пропорционально риску дисконтирования (ядро).\n' +
      'Поэтому он не равен DV01 RUONIA (R29): +1 б.п. к непрерывной ставке — это +(1 + R) б.п. к годовой, и у фикс-ОФЗ ' +
      '|Базис01| ≈ |DV01 RUONIA| / (1 + R), R — ставка RUONIA на сроке бумаги: на 13–14 % меньше при ставках 15–16 %. ' +
      'У ОФЗ-ПК (флоатер на RUONIA) DV01 ≈ 0 — купон растёт вместе с кривой, а Базис01 — весь спредовый риск.',
    cs01: 'CS01, тыс ₽/бп: +1 б.п. к кредитному Z-спреду к RUONIA (годовой, ACT/365F) разом по всем срокам, кривая стоит — ' +
      'как Базис01 у ОФЗ; у фикс-бумаг ≈ DV01 RUONIA / (1 + R), R — ставка RUONIA на сроке (R29).',
  };
  K.tips.dv01 = 'DV01 ставок, тыс ₽/бп: сумма бакетов по кривым ставок (RUONIA, КС, валютные), без базиса ОФЗ и CS01.\n' + K.tips.ruonia;
  K.tips.zdisc = 'Z-спред к кривой дисконтирования (RUONIA OIS; в день без неё — к КБД), б.п., годовой, ACT/365F (R32): у фикс-бумаги — ' +
    'обычный Z-спред, у флоатера — купоны по прогнозу своей кривой проекции (КС или RUONIA), дисконт по RUONIA + Z. Сравним у фиксов ' +
    'и флоатеров. Дисконт-маржа (DM) флоатера — спред к его кривой проекции; у флоатера на RUONIA (ОФЗ-ПК) Z и DM равны.';
  K.zdiscTip = K.tips.zdisc;
  // каким должен быть DV01 флоатера во времени (R44): подпись под графиком истории карточки
  K.tips.floaterDv01 = 'Флоатер: ставочный DV01 мал — купон пересматривается, и ставка двигает цену только до ближайшей фиксации купона: ' +
    'DV01 ≈ −(время до фиксации) × MV × 1 б.п. (у флоатера на КС — сумма дисконтирования по RUONIA и проекции по КС). Он пилообразный: ' +
    'к дате фиксации плавно уходит к нулю, после неё прыгает на полный купонный период; у флоатера на RUONIA с начислением ' +
    'за период — около нуля всегда. Основной риск флоатера — спредовый: CS01 (базис у ОФЗ-ПК) и DM — по всему сроку до погашения, ' +
    'он больше. Плавное снижение к нулю — подход фиксации купона, ступень — изменение числа бумаг (видно в «на 1 бумагу» и подсказке).';
  // подсказка к кривой риска: RUONIA и КС — конвенция бампа, базис ОФЗ, CS01; прочие — название кривой
  K.curveTip = id => id === 'CS01' ? K.tips.cs01 : id === 'RUB OFZ' || (M.curve[id] && M.curve[id].kind === 'basis') ? K.tips.basis01
    : id === 'RUB RUONIA' || id === 'RUB KEY' ? (M.curve[id] ? M.curve[id].name + '.\n' : '') + K.tips.ruonia
      : (M.curve[id] ? M.curve[id].name + ': +1 б.п. к нулевой ставке кривой по бакетам' : '');
  // подпись с подсказкой (title): пунктир снизу, курсор «?» (K.tip — другое: плавающая подсказка у мыши)
  K.hint = (label, tip) => tip ? '<span class="k-hint" title="' + esc(tip) + '">' + label + '</span>' : label;
  // поля карточки: [подпись, значение, во всю ширину, подсказка]
  K.fieldsHtml = F => '<div class="k-fields">' + F.map(([l, v, wide, tip]) => '<div' + (wide ? ' class="wide"' : '') + '><div class="l">' + K.hint(l, tip) + '</div><div class="v">' + v + '</div></div>').join('') + '</div>';

  // ---------- цвета ----------
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  K.css = css;
  K.ccyColor = c => css('--' + String(c).toLowerCase()) || css('--accent');
  const CCY_NAMES = { RUB: 'Рубли', USD: 'Доллары', EUR: 'Евро', CNY: 'Юани', CHF: 'Франки', GBP: 'Фунты', HKD: 'Гонконгские доллары', JPY: 'Иены', AED: 'Дирхамы', KZT: 'Тенге', BYN: 'Белорусские рубли', TRY: 'Лиры', INR: 'Рупии' };
  K.ccyName = c => CCY_NAMES[c] || c;
  K.ccyTag = c => '<span class="k-ccy" style="background:' + K.ccyColor(c) + '">' + c + '</span>';
  K.curveColors = { 'RUB RUONIA': '#4c9dff', 'RUB KEY': '#22c3d6', 'RUB OFZ': '#9b7bff', 'USD SOFR': '#3fb67a', 'EUR ESTR': '#d58cff', 'CNY XCCY': '#f0645a', 'CS01': '#e3b341' };
  K.PAL = ['#4c9dff', '#3fb67a', '#e3b341', '#f0645a', '#b48cff', '#22c3d6', '#ff8f3f', '#8bc34a', '#ef6fb0', '#7f8cff', '#c9a26b', '#5fd4a8', '#ff6b8b', '#a0b4c8'];
  // кривые прогона, которых нет в списке цветов (CNY implied, прокси, CHF…), — цвет из палитры, детерминированно по имени
  M.curves.forEach(c => { if (!K.curveColors[c.id]) K.curveColors[c.id] = K.PAL[(M.util && M.util.hash ? M.util.hash(c.id) : c.id.length) % K.PAL.length]; });
  const issuerIdx = {}; M.issuers.forEach((x, i) => issuerIdx[x.id] = i);
  K.issuerColor = id => id && issuerIdx[id] != null ? K.PAL[issuerIdx[id] % K.PAL.length] : '#7a8794';
  K.kindColor = { PD: '#4c9dff', PK: '#22c3d6', IN: '#7f8cff', SOVFX: '#b48cff', FIX: '#3fb67a', FLT: '#e3b341', IRS: '#f0645a', KSW: '#ff8f3f', BAS: '#ef6fb0', FXF: '#a0b4c8', XCCY: '#c9a26b', OTC: '#f0645a', FX: '#a0b4c8', OTH: '#8a96a3' };
  // фон ячейки тепловой карты: синий — короткий риск/убыток не важно; знак цвета задаётся pos/neg
  K.heat = (v, max, opt) => {
    if (!v || !max) return '';
    const a = Math.min(1, Math.abs(v) / max), alpha = (0.08 + 0.62 * Math.pow(a, 0.7)).toFixed(3);
    const pos = (opt && opt.invert) ? v < 0 : v > 0;
    const c = pos ? '63,182,122' : '240,100,90';
    return 'background:rgba(' + c + ',' + alpha + ')';
  };
  // риск (не «хорошо/плохо»): синий — длинная дюрация (DV01 < 0), янтарный — короткая (DV01 > 0)
  K.heatRisk = (v, max) => { if (!v || !max) return ''; const a = Math.min(1, Math.abs(v) / max), al = (0.07 + 0.6 * Math.pow(a, 0.7)).toFixed(3); return 'background:rgba(' + (v < 0 ? '76,157,255' : '227,160,60') + ',' + al + ')'; };
  K.heatMono = (v, max, rgb) => { if (!v || !max) return ''; const a = Math.min(1, Math.abs(v) / max); return 'background:rgba(' + (rgb || '76,157,255') + ',' + (0.06 + 0.6 * Math.pow(a, 0.7)).toFixed(3) + ')'; };
  K.bar = (v, max, one) => { if (!max) return ''; const w = Math.min(50, Math.abs(v) / max * 50); return one ? '<div class="k-bar one"><i class="a" style="left:0;width:' + (w * 2) + '%"></i></div>' : '<div class="k-bar"><i class="' + (v >= 0 ? 'p' : 'n') + '" style="' + (v >= 0 ? 'left:50%' : 'right:50%') + ';width:' + w + '%"></i></div>'; };
  K.spark = (arr, w, hgt, color) => {
    w = w || 80; hgt = hgt || 22; const a = arr.filter(x => x != null); if (a.length < 2) return '';
    const mn = Math.min(...a), mx = Math.max(...a), r = mx - mn || 1;
    const pts = arr.map((v, i) => (i / (arr.length - 1) * w).toFixed(1) + ',' + (hgt - (v - mn) / r * (hgt - 2) - 1).toFixed(1)).join(' ');
    return '<svg class="spark" width="' + w + '" height="' + hgt + '"><polyline fill="none" stroke="' + (color || css('--accent')) + '" stroke-width="1.3" points="' + pts + '"/></svg>';
  };

  // ---------- тема ----------
  K.theme = () => document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
  (function initTheme() { let t = null; try { t = localStorage.getItem('concepts-theme'); } catch (e) { } if (t === 'light') document.documentElement.setAttribute('data-theme', 'light'); })();
  K.toggleTheme = () => {
    const t = K.theme() === 'light' ? 'dark' : 'light';
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('concepts-theme', t); } catch (e) { }
    K.charts.forEach(c => c.redraw && c.redraw());
    fire('theme');
  };

  // ---------- события ----------
  const subs = {};
  function fire(ev) { (subs[ev] || []).forEach(fn => { try { fn(); } catch (e) { console.error(e); } }); }
  // подписка; возвращает функцию отписки
  K.on = (ev, fn) => { (subs[ev] = subs[ev] || []).push(fn); return () => { const a = subs[ev], i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }; };
  // перерисовка блока, пока его элемент в документе; отписывается, когда элемент удалён
  K.live = (el, ev, fn) => { const off = K.on(ev, () => { if (!el.isConnected) return off(); fn(); }); return off; };
  K.onFilter = fn => K.on('filter', fn);

  // ---------- фильтр книг (верхний уровень) ----------
  // portsOff — скрытые портфели (колонка Portfolio отчёта, x_portfolio адаптера; '—' — позиции без неё): новые
  // портфели прогона видны, пока их не сняли; сохранённые, которых в прогоне нет, ни на что не влияют
  const filt = { entity: null, desk: null, books: null, ccy: null, portsOff: null };
  try { Object.assign(filt, JSON.parse(localStorage.getItem('concepts-filter') || '{}')); } catch (e) { }
  K.filter = filt;
  K.setFilter = (patch) => { Object.assign(filt, patch); try { localStorage.setItem('concepts-filter', JSON.stringify(filt)); } catch (e) { } fire('filter'); renderFilterChips(); };
  const portOf = p => p.portfolio || '—';
  const PORTS = [...new Set(M.positions.map(portOf))].sort((a, b) => (a === '—') - (b === '—') || a.localeCompare(b, 'ru'));
  const portsOff = () => (filt.portsOff || []).filter(v => PORTS.includes(v));
  K.list = () => { const off = portsOff(); return M.positions.filter(p => (!filt.entity || p.entity === filt.entity) && (!filt.desk || p.desk === filt.desk) && (!filt.books || !filt.books.length || filt.books.includes(p.book)) && (!filt.ccy || p.ccy === filt.ccy) && (!off.length || !off.includes(portOf(p)))); };
  let chipsBox = null;
  // каскад: юрлицо → деск → книги; в каждом списке только варианты, совместимые с уже выбранным
  const booksIn = () => M.books.filter(b => (!filt.entity || b.entity === filt.entity) && (!filt.desk || b.desk === filt.desk));
  const nPos = {}, nPort = {}; M.positions.forEach(p => { nPos[p.book] = (nPos[p.book] || 0) + 1; nPort[portOf(p)] = (nPort[portOf(p)] || 0) + 1; });
  const portName = v => v === '—' ? 'без портфеля' : v;
  function renderFilterChips() {
    if (!chipsBox) return;
    const ent = M.entities;
    const desks = [...new Set(M.books.filter(b => !filt.entity || b.entity === filt.entity).map(b => b.desk))];
    const lbl = filt.books && filt.books.length ? (filt.books.length === 1 ? M.book[filt.books[0]].name : filt.books.length + ' книг') : 'все книги (' + booksIn().length + ')';
    // чип портфелей — когда адаптер их передал (x_portfolio); у прогона без колонки все позиции «без портфеля»
    const off = portsOff(), showPorts = PORTS.some(v => v !== '—');
    const plbl = !off.length ? 'все портфели' : off.length === 1 ? 'без ' + portName(off[0]) : 'портфели: ' + (PORTS.length - off.length) + ' из ' + PORTS.length;
    chipsBox.innerHTML =
      '<span class="k-chip' + (filt.entity ? ' on' : '') + '" data-f="entity">' + (filt.entity ? esc(ent.find(e => e.id === filt.entity).name) : 'все юрлица') + ' ▾</span>' +
      '<span class="k-chip' + (filt.desk ? ' on' : '') + '" data-f="desk">' + (filt.desk ? esc(filt.desk) : 'все дески') + ' ▾</span>' +
      '<span class="k-chip' + (filt.books && filt.books.length ? ' on' : '') + '" data-f="books">' + esc(lbl) + ' ▾</span>' +
      (showPorts ? '<span class="k-chip' + (off.length ? ' on' : '') + '" data-f="ports" title="Портфель — колонка Portfolio отчёта о позициях">' + esc(plbl) + ' ▾</span>' : '') +
      ((filt.entity || filt.desk || (filt.books && filt.books.length) || filt.ccy || off.length) ? '<span class="k-chip" data-f="reset"><span class="x">✕</span> сбросить</span>' : '');
    chipsBox.querySelectorAll('[data-f]').forEach(ch => ch.onclick = ev => {
      const k = ch.dataset.f;
      if (k === 'reset') return K.setFilter({ entity: null, desk: null, books: null, ccy: null, portsOff: null });
      if (k === 'ports') {  // отмеченные — видны; щелчок снимает или возвращает портфель
        const items = PORTS.map(v => ({ v, t: portName(v), sub: nPort[v] + ' поз', multi: true }));
        menu(ch, items, PORTS.filter(v => !off.includes(v)), v => {
          const s = new Set(off); s.has(v) ? s.delete(v) : s.add(v);
          K.setFilter({ portsOff: s.size >= PORTS.length ? [] : [...s] });  // снять все — значит показать все
        }, 'ports');
        return ev.stopPropagation();
      }
      const items = k === 'entity' ? [{ v: null, t: 'все юрлица' }].concat(ent.map(e => ({ v: e.id, t: e.name, sub: M.books.filter(b => b.entity === e.id).length + ' книг' })))
        : k === 'desk' ? [{ v: null, t: 'все дески' }].concat(desks.map(d => ({ v: d, t: d, sub: M.books.filter(b => b.desk === d && (!filt.entity || b.entity === filt.entity)).length + ' книг' })))
          : booksIn().map(b => ({ v: b.id, t: b.name, sub: b.id + ' · ' + b.desk + (filt.entity ? '' : ' · ' + M.entities.find(e => e.id === b.entity).name) + ' · ' + (nPos[b.id] || 0) + ' поз', multi: true, empty: !nPos[b.id] }));
      menu(ch, items, k === 'books' ? (filt.books || []) : filt[k], v => {
        if (k === 'books') { const s = new Set(filt.books || []); s.has(v) ? s.delete(v) : s.add(v); K.setFilter({ books: [...s] }); }
        else {
          // смена юрлица или деска убирает выбранные книги, которые в новый срез не входят
          const patch = { [k]: v };
          if (k === 'entity' && v && filt.desk && !M.books.some(b => b.entity === v && b.desk === filt.desk)) patch.desk = null;
          Object.assign(filt, patch);
          const ok = new Set(booksIn().map(b => b.id)); patch.books = (filt.books || []).filter(b => ok.has(b));
          K.setFilter(patch);
        }
      }, k === 'books' && 'books');
      ev.stopPropagation();
    });
  }
  let openMenu = null;
  // multi — ключ чипа шапки ('books', 'ports'), меню которого переоткрывается после щелчка; у книг — поиск и «все / снять»
  function menu(anchor, items, cur, pick, multi) {
    if (openMenu) openMenu.remove();
    const m = h('div', 'k-panel'); m.style.cssText = 'position:fixed;z-index:140;min-width:220px;max-height:60vh;overflow:auto;box-shadow:var(--shadow);padding:4px 0';
    const r = anchor.getBoundingClientRect(); m.style.left = Math.min(r.left, innerWidth - 300) + 'px'; m.style.top = (r.bottom + 4) + 'px';
    let rowsBox = m, q = multi === 'books' ? (menu._q || '') : '';
    const reopen = () => { m.remove(); openMenu = null; const nb = chipsBox.querySelector('[data-f=' + multi + ']'); if (nb) nb.click(); };
    const vis = () => items.filter(it => !q || (it.t + ' ' + (it.sub || '')).toLowerCase().includes(q));
    if (multi === 'books') {
      const tb = h('div'); tb.style.cssText = 'display:flex;gap:6px;padding:4px 8px 6px;border-bottom:1px solid var(--line);position:sticky;top:-4px;background:var(--panel)';
      const inp = h('input', 'k-input'); inp.placeholder = 'найти книгу…'; inp.value = q; inp.style.minWidth = '0'; inp.style.flex = '1';
      const all = h('button', 'k-btn', 'все видимые'), none = h('button', 'k-btn', 'снять');
      tb.append(inp, all, none); m.appendChild(tb); rowsBox = h('div'); m.appendChild(rowsBox);
      inp.oninput = () => { q = menu._q = inp.value.toLowerCase(); paintRows(); };
      inp.onclick = e => e.stopPropagation();
      all.onclick = e => { e.stopPropagation(); K.setFilter({ books: [...new Set((filt.books || []).concat(vis().map(it => it.v)))] }); reopen(); };
      none.onclick = e => { e.stopPropagation(); K.setFilter({ books: null }); reopen(); };
      setTimeout(() => inp.focus(), 0);
    }
    function paintRows() {
    rowsBox.querySelectorAll('.mi').forEach(x => x.remove());
    vis().forEach(it => {
      const on = multi ? cur.includes(it.v) : cur === it.v;
      const row = h('div', '', (multi ? '<span style="width:16px;display:inline-block">' + (on ? '☑' : '☐') + '</span>' : (on ? '• ' : '&nbsp; ')) + esc(it.t) + (it.sub ? ' <span class="mut3" style="font-size:11px">' + esc(it.sub) + '</span>' : ''));
      row.className = 'mi'; row.style.cssText = 'padding:5px 12px;cursor:pointer;white-space:nowrap' + (it.empty ? ';opacity:.5' : ''); row.onmouseenter = () => row.style.background = 'var(--hover)'; row.onmouseleave = () => row.style.background = '';
      row.onclick = e => { e.stopPropagation(); pick(it.v); if (!multi) close(); else reopen(); };
      rowsBox.appendChild(row);
    });
    }
    paintRows();
    document.body.appendChild(m); openMenu = m;
    const close = () => { if (openMenu === m) { m.remove(); openMenu = null; } };
    setTimeout(() => document.addEventListener('click', close, { once: true }), 0);
    return m;
  }
  K.menu = menu;

  // ---------- источник данных: прогон из базы (desk serve) или демо-набор (lib/boot.js, lib/real.js) ----------
  K.source = window.CONCEPTS_SOURCE || 'file';
  function srcChip() {
    const s = K.source;
    if (s === 'real') return '<span class="k-chip asof" data-a="src" title="Прогон из базы: выбрать другой">срез <b>' + f.date(M.asOf) + '</b> · ' + esc(M.cut.label) + ' · #' + esc(M.run && M.run.id) + ' ▾</span>';
    const t = s === 'empty' ? 'прогонов нет — тестовые данные' : s === 'demo' ? 'тестовые данные' : 'демо (файл)';
    return '<span class="k-chip" data-a="src" style="border-color:var(--warn);color:var(--warn)" title="Вымышленный набор lib/mock.js">' + t + (s === 'file' ? '' : ' ▾') + '</span>' +
      '<span class="asof">срез <b>' + f.date(M.asOf) + '</b> · ' + esc(M.cut.label) + '</span>';
  }
  function go(sel) {  // как desk-nav.js: выбор в адресе и в localStorage (desk.run.v1), общий со страницами прогона
    try { if (Object.keys(sel).length) localStorage.setItem('desk.run.v1', JSON.stringify({ sel, at: Date.now() })); else localStorage.removeItem('desk.run.v1'); } catch (e) { }
    const u = new URL(location.href); ['run_id', 'as_of', 'demo'].forEach(k => u.searchParams.delete(k)); Object.keys(sel).forEach(k => u.searchParams.set(k, sel[k]));
    location.href = u.toString();
  }
  function bindSrc(bar) {
    const ch = bar.querySelector('[data-a=src]'), nb = bar.querySelector('[data-a=notes]');
    if (nb) nb.onclick = e => {
      e.stopPropagation(); if (openMenu) openMenu.remove();
      const m = h('div', 'k-panel'), wide = !!(M.unvalued && M.unvalued.total || M.approx && M.approx.length), W = wide ? 640 : 460;
      m.style.cssText = 'position:fixed;z-index:140;width:min(' + W + 'px,92vw);max-height:70vh;overflow:auto;box-shadow:var(--shadow);padding:10px 14px;font-size:12.5px';
      const r = nb.getBoundingClientRect(); m.style.left = Math.max(8, Math.min(r.left, innerWidth - W - 10)) + 'px'; m.style.top = (r.bottom + 4) + 'px';
      m.innerHTML = '<div class="k-sec" style="margin-top:0">Чего нет в данных</div><ul style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px">' + M.notes.map(t => '<li>' + K.noteHtml(t) + '</li>').join('') + '</ul>' + K.coverageHtml();
      m.querySelectorAll('tr[data-cg]').forEach(tr => tr.onclick = () => { const [sec, i] = tr.dataset.cg.split(':'), g = (sec === 'u' ? M.unvalued.groups : M.approx)[+i]; K.posList(g.why || g.text, g.ids, g.kindLabel + (g.todo ? ' · что сделать: ' + g.todo : '')); });
      document.body.appendChild(m); openMenu = m;
      setTimeout(() => document.addEventListener('click', () => { if (openMenu === m) { m.remove(); openMenu = null; } }, { once: true }), 0);
    };
    if (!ch || K.source === 'file') return;
    ch.onclick = e => {
      e.stopPropagation();
      fetch('../api/v1/runs?status=complete&limit=60').then(r => r.json()).then(j => {
        const items = [{ v: 'latest', t: 'последний прогон' }].concat((j.runs || []).filter(r => r.run_type !== 'TEST').map(r => ({ v: String(r.run_id), t: (r.label || r.business_date) + ' · #' + r.run_id, sub: r.is_default ? 'основной' : r.run_type }))).concat([{ v: 'demo', t: 'тестовые данные (демо)' }]);
        menu(ch, items, K.source === 'real' ? String(M.run.id) : (K.source === 'demo' ? 'demo' : null), v => go(v === 'latest' ? {} : v === 'demo' ? { demo: '1' } : { run_id: v }));
      }).catch(() => menu(ch, [{ v: 'latest', t: 'последний прогон' }, { v: 'demo', t: 'тестовые данные (демо)' }], null, v => go(v === 'latest' ? {} : { demo: '1' })));
    };
  }

  // ---------- «Чего нет в данных»: ссылки и разбор не оценённых позиций (R45, api/coverage.py) ----------
  // «Настройки → «История прогонов»» в тексте заметки — ссылка на страницу досчёта истории
  const BF = 'Настройки → «История прогонов»';
  K.noteHtml = t => esc(t).split(esc(BF)).join('<a href="../settings.html#bf">' + esc(BF) + '</a>');
  // таблицы причин: не оценены (причина × тип: позиций, сумма в ₽ где есть курс, MV отчёта) и оценены не ядром;
  // строка — список позиций (K.posList), из него — карточка
  K.coverageHtml = () => {
    const U = M.unvalued, A = M.approx || [], mm = v => v == null ? '<span class="mut3">—</span>' : f.mn(v, 1);
    let out = '';
    if (U && U.groups && U.groups.length) {
      out += '<div class="k-sec">Не оценены: ' + U.open + ' поз.' + (U.closed ? ' + ' + U.closed + ' закрытых' : '') + ' <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">строка — список позиций</span></div>' +
        '<table class="k-tbl k-compact" style="width:100%"><thead><tr><th>Причина · что сделать</th><th>Тип</th><th class="n">Поз.</th><th class="n" title="номинал / сумма строки в рублях по курсу прогона">Сумма, mm ₽</th><th class="n" title="MV из отчёта, где он есть">MV отч.</th></tr></thead><tbody>' +
        U.groups.map((g, i) => '<tr class="row" data-cg="u:' + i + '" style="cursor:pointer' + (g.closed ? ';opacity:.6' : '') + '"><td><b>' + esc(g.why) + '</b><div class="mut3">' + esc(g.todo) + '</div></td><td class="mut">' + esc(g.kindLabel) + '</td><td class="n">' + g.n + '</td><td class="n">' + mm(g.notionalRub) +
          (g.noRate && g.noRate.length ? ' <span class="mut3" title="нет курса в прогоне: сумма этих строк не в рублях">+ ' + esc(g.noRate.join(', ')) + '</span>' : '') + '</td><td class="n">' + mm(g.mvRub) + '</td></tr>').join('') + '</tbody></table>';
    }
    if (A.length) {
      out += '<div class="k-sec">Оценены не ядром: ' + A.reduce((s, g) => s + g.n, 0) + ' поз. <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">риск — отчёта или по дюрации</span></div>' +
        '<table class="k-tbl k-compact" style="width:100%"><thead><tr><th>Как оценены</th><th>Тип</th><th class="n">Поз.</th><th class="n">MV, mm ₽</th></tr></thead><tbody>' +
        A.map((g, i) => '<tr class="row" data-cg="a:' + i + '" style="cursor:pointer"><td>' + esc(g.text) + '</td><td class="mut">' + esc(g.kindLabel) + '</td><td class="n">' + g.n + '</td><td class="n">' + mm(g.mvRub) + '</td></tr>').join('') + '</tbody></table>';
    }
    return out;
  };
  // список позиций группы: имя, книга, валюта, тип, причина (extra прогона); строка — карточка позиции
  K.posList = function (title, ids, sub) {
    const o = drawer(); o.title = title;
    const why = {}; ((window.DESK || {}).positions || []).forEach(d => { const e = d.extra || {}; why[d.id] = e.reason || e.not_exact_reason || (e.assumptions || []).join('; ') || ''; });
    const L = ids.map(id => M.pos[id]).filter(Boolean);
    o.el.querySelector('.t').textContent = title;
    o.el.querySelector('.st').textContent = L.length + ' поз.' + (sub ? ' · ' + sub : '');
    o.bd.innerHTML = '<table class="k-tbl"><thead><tr><th>Позиция</th><th>Книга</th><th>Ccy</th><th>Тип</th><th>Причина</th></tr></thead><tbody>' +
      L.map(p => '<tr class="row" data-id="' + esc(p.id) + '"><td>' + esc(p.name) + ' <span class="mut3 mono">' + esc(p.isin || p.id) + '</span></td><td class="mut">' + esc(p.book) + '</td><td>' + (p.ccy ? K.ccyTag(p.ccy) : '') + '</td><td class="mut">' + esc(p.kindLabel || '') + '</td><td class="mut3" style="max-width:360px">' + esc(why[p.id] || '') + '</td></tr>').join('') + '</tbody></table>';
    o.bd.querySelectorAll('tr.row').forEach(tr => tr.onclick = () => K.card(tr.dataset.id));
    return o;
  };

  // ход «Истории прогонов» (сервер досчитывает прошлые даты от самой ранней, R23): пока есть даты к расчёту, чип
  // в шапке говорит, сколько готово, — история риска, MTD/YTD и изменения позиций прошлых дат появляются по мере расчёта
  function backfillChip(bar) {
    const ch = h('a', 'k-chip'); ch.href = '../settings.html#bf'; ch.style.cssText = 'display:none;text-decoration:none;border-color:var(--warn)';
    bar.querySelector('[data-a=search]').before(ch);
    const poll = () => fetch('../api/v1/backfill/status', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(j => {
      if (!j || !j.enabled || !j.todo) { ch.style.display = 'none'; return; }
      const all = j.done + j.todo + (j.failed || 0);
      ch.style.display = ''; ch.textContent = '⟳ история: ' + j.done + ' из ' + all + ' дат';
      ch.title = 'Сервер досчитывает прошлые даты с ' + j.from + ', от самой ранней: история риска, PnL с начала месяца и года и позиции прошлых дат появляются по мере расчёта (обновите страницу). Следующие: ' + (j.next || []).join(', ') + '. Подробности — Настройки → «История прогонов».';
      setTimeout(poll, 120e3);
    }).catch(() => { });
    poll();
  }

  // ---------- шапка ----------
  K.top = function (opt) {
    opt = opt || {};
    document.title = opt.title || document.title;
    const bar = h('div', 'k-top');
    bar.innerHTML = '<div class="brand"><a href="index.html" title="Все мониторы">◂ мониторы</a><span>' + esc(opt.title || '') + '</span>' + (opt.tag ? '<span class="tag">' + esc(opt.tag) + '</span>' : '') + '</div>' +
      '<div class="filters" style="display:flex;gap:6px;flex-wrap:wrap"></div><div class="sp"></div>' +
      (opt.extra || '') +
      '<span class="k-chip" data-a="repr" title="Валюта отчёта: все суммы деска пересчитываются по споту">в ' + K.repr + ' ' + K.SYM[K.repr] + ' ▾</span>' +
      srcChip() + (M.notes && M.notes.length ? '<span class="k-chip" data-a="notes" title="Чего нет в данных прогона" style="border-color:var(--warn)">ⓘ ' + M.notes.length + '</span>' : '') +
      '<button class="k-btn" data-a="search">Поиск <span class="k-kbd">Ctrl K</span></button>' +
      '<button class="k-btn" data-a="theme" title="Светлая / тёмная тема">◐</button>';
    document.body.prepend(bar);
    chipsBox = bar.querySelector('.filters'); renderFilterChips();
    bar.querySelector('[data-a=search]').onclick = () => K.palette();
    bar.querySelector('[data-a=theme]').onclick = K.toggleTheme;
    bindSrc(bar);
    if (K.source === 'real') backfillChip(bar);
    const rc = bar.querySelector('[data-a=repr]');
    rc.onclick = e => { e.stopPropagation(); menu(rc, K.REPRS.map(c => ({ v: c, t: 'в ' + c + ' ' + K.SYM[c], sub: c === 'RUB' ? '' : 'курс ' + f.n(M.fx[c], 2) })), K.repr, v => { if (v !== K.repr) K.setRepr(v); }); };
    return bar;
  };

  // ---------- кликабельные цифры ----------
  const reg = []; K._reg = reg;
  K.d = function (spec, text, cls) {
    const id = reg.push(spec) - 1;
    const v = spec.v != null ? spec.v : spec.rows ? spec.rows.reduce((a, r) => a + (r.v || 0), 0) : (spec.list ? M.sum(spec.list, spec.val) : null);
    const t = text != null ? text : (spec.fmt || f.dv)(v);
    return '<span class="dr ' + (cls != null ? cls : (spec.color === false ? '' : f.cls(v))) + '" data-dr="' + id + '">' + t + '</span>';
  };
  document.addEventListener('click', e => {
    const t = e.target.closest('[data-dr]');
    if (t) { e.stopPropagation(); e.preventDefault(); K.drill(reg[+t.dataset.dr]); return; }
    const c = e.target.closest('[data-card]');
    if (c) { e.stopPropagation(); K.card(c.dataset.card); }
  }, true);

  // ---------- выезжающие панели ----------
  const stack = [];
  function drawer(wide) {
    const scrim = h('div', 'k-scrim'), d = h('div', 'k-drawer' + (wide ? ' wide' : ''));
    d.innerHTML = '<div class="hd"><div style="min-width:0;flex:1"><div class="k-crumbs"></div><div class="t"></div><div class="st"></div></div><button class="close" title="Закрыть (Esc)">×</button></div><div class="bd"></div>';
    document.body.append(scrim, d);
    const o = { el: d, scrim, bd: d.querySelector('.bd'), close: () => { const i = stack.indexOf(o); if (i >= 0) stack.splice(i).reverse().forEach(x => { x.el.remove(); x.scrim.remove(); (x.charts || []).forEach(c => c.dispose()); }); } };
    scrim.onclick = o.close; d.querySelector('.close').onclick = o.close;
    if (stack.length) { scrim.style.background = 'transparent'; d.style.right = '0'; }
    stack.push(o);
    const crumbs = d.querySelector('.k-crumbs');
    crumbs.innerHTML = stack.slice(0, -1).map((x, i) => '<a data-i="' + i + '">' + esc(x.title || '') + '</a> ›').join(' ');
    crumbs.querySelectorAll('a').forEach(a => a.onclick = () => { const i = +a.dataset.i; stack.slice(i + 1).forEach(x => x.close()); });
    return o;
  }
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (palEl) return closePal(); if (stack.length) stack[stack.length - 1].close(); }
    if ((e.key === 'k' || e.key === 'K' || e.key === 'л' || e.key === 'Л') && (e.ctrlKey || e.metaKey)) { e.preventDefault(); K.palette(); }
    if (e.key === '/' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) { e.preventDefault(); K.palette(); }
  });

  // ---------- разбор вклада ----------
  const DIMS = [
    ['pos', 'позиции', p => p.id, p => p.name],
    ['issuer', 'эмитенты', p => p.issuerName, p => p.issuerName],
    ['book', 'книги', p => p.book, p => p.bookName],
    ['ccy', 'валюты', p => p.ccy, p => p.ccy],
    ['kind', 'типы', p => p.group + '.' + p.kind, p => p.groupLabel + ' · ' + p.kindLabel],
    ['tenor', 'сроки', p => tenorBucket(p.tenor), p => tenorBucket(p.tenor)],
  ];
  const TB = [[0.5, '< 6м'], [1, '6м–1г'], [2, '1–2г'], [3, '2–3г'], [5, '3–5г'], [7, '5–7г'], [10, '7–10г'], [99, '10г+']];
  function tenorBucket(t) { for (const [x, l] of TB) if (t <= x) return l; return '10г+'; }
  K.tenorBucket = tenorBucket;
  K.drill = function (spec) {
    const o = drawer(); o.title = spec.title;
    const fmt = spec.fmt || f.dv;
    // строки: [{p, v}] — из spec.rows или spec.list+val
    const rows = (spec.rows || spec.list.map(p => ({ p, v: typeof spec.val === 'function' ? spec.val(p) : p[spec.val] }))).filter(r => r.v && Math.abs(r.v) > 1e-9);
    const tot = rows.reduce((s, r) => s + r.v, 0);
    o.el.querySelector('.t').innerHTML = esc(spec.title) + ' <span class="' + f.cls(tot) + '">' + fmt(tot) + '</span> <span class="mut" style="font-size:12px;font-weight:400">' + esc(spec.unit || '') + '</span>';
    o.el.querySelector('.st').textContent = rows.length + ' позиций дают вклад' + (spec.note ? ' · ' + spec.note : '');
    let dim = spec.dim || 'pos', q = '', view = 'table';
    const tools = h('div', '', ''); tools.style.cssText = 'display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px';
    const segBox = h('span'), viewBox = h('span'); const inp = h('input', 'k-input'); inp.placeholder = 'фильтр…';
    tools.append(h('span', 'mut', 'по:'), segBox, inp, h('span', 'sp'), viewBox); tools.children[3].style.flex = '1';
    const body = h('div');
    o.bd.append(tools, body);
    K.seg(segBox, DIMS.map(d => [d[0], d[1]]), dim, v => { dim = v; draw(); });
    K.seg(viewBox, [['table', 'таблица'], ['chart', 'график']], view, v => { view = v; draw(); });
    inp.oninput = () => { q = inp.value.toLowerCase(); draw(); };
    function draw() {
      const D = DIMS.find(d => d[0] === dim);
      const g = {};
      rows.filter(r => !q || r.p.searchText.includes(q)).forEach(r => { const k = D[2](r.p); (g[k] = g[k] || { k, label: D[3](r.p), v: 0, n: [], p: r.p }).v += r.v; g[k].n.push(r); });
      const list = Object.values(g).sort((a, b) => Math.abs(b.v) - Math.abs(a.v));
      const max = Math.max(...list.map(x => Math.abs(x.v)), 1); const absTot = list.reduce((s, x) => s + Math.abs(x.v), 0) || 1;
      if (o.charts) { o.charts.forEach(c => c.dispose()); o.charts = []; }
      if (view === 'chart') {
        body.innerHTML = ''; const el = h('div'); el.style.height = Math.max(240, Math.min(list.length, 30) * 22 + 40) + 'px'; body.appendChild(el);
        const L = list.slice(0, 30).reverse();
        const c = K.chart(el, () => ({
          grid: { left: 10, right: 60, top: 10, bottom: 20, containLabel: true },
          xAxis: { type: 'value', axisLabel: { formatter: v => fmt(v) } }, yAxis: { type: 'category', data: L.map(x => x.label.length > 34 ? x.label.slice(0, 33) + '…' : x.label) },
          tooltip: { trigger: 'axis', valueFormatter: v => fmt(v) },
          series: [{ type: 'bar', data: L.map(x => ({ value: x.v, itemStyle: { color: dim === 'ccy' ? K.ccyColor(x.k) : dim === 'issuer' ? K.issuerColor(x.p.issuer) : (x.v >= 0 ? css('--pos') : css('--neg')) } })), label: { show: true, position: 'right', formatter: p => fmt(p.value), color: css('--fg-2'), fontSize: 11 } }],
        }));
        c.on('click', pp => { const x = L[pp.dataIndex]; if (dim === 'pos') K.card(x.k); else K.drill(Object.assign({}, spec, { title: spec.title + ' · ' + x.label, rows: x.n, dim: 'pos' })); });
        o.charts = [c];
        return;
      }
      let cum = 0;
      // размер: у позиции — номинал в её валюте и MV, у группы — MV её позиций (у деривативов MV — их PV)
      const mvOf = x => x.n.reduce((s, r) => s + (r.p.mv || 0), 0), mvTot = list.reduce((s, x) => s + mvOf(x), 0);
      body.innerHTML = '<table class="k-tbl"><thead><tr><th></th><th>' + (dim === 'pos' ? 'Позиция' : D[1][0].toUpperCase() + D[1].slice(1)) + '</th>' + (dim === 'pos' ? '<th>Книга</th><th>Ccy</th><th class="n" title="в валюте позиции">Номинал</th>' : '<th class="n">Позиций</th>') + '<th class="n">MV, mm ₽</th><th class="n">Вклад</th><th style="width:120px"></th><th class="n">Доля</th><th class="n">Накопл.</th></tr></thead><tbody>' +
        list.map(x => {
          cum += Math.abs(x.v);
          const stripe = dim === 'issuer' || dim === 'pos' ? K.issuerColor(x.p.issuer) : dim === 'ccy' ? K.ccyColor(x.k) : 'transparent';
          return '<tr class="row" data-k="' + esc(x.k) + '"><td class="stripe" style="background:' + stripe + '"></td><td style="max-width:240px;overflow:hidden;text-overflow:ellipsis" title="' + esc(x.label + (dim === 'pos' ? ' · ' + x.p.issuerName : '')) + '">' + esc(x.label) + (dim === 'pos' ? ' <span class="mut3">' + esc(x.p.issuerName) + '</span>' : '') + '</td>' +
            (dim === 'pos' ? '<td class="mut">' + esc(x.p.book) + '</td><td>' + K.ccyTag(x.p.ccy) + '</td><td class="n">' + f.mn(x.p.notional, 2) + '<span class="mut3">mm ' + esc(x.p.ccy) + '</span></td>' : '<td class="n mut">' + x.n.length + '</td>') +
            '<td class="n">' + (dim === 'pos' && x.p.mv == null ? '—' : f.mn(mvOf(x), 1)) + '</td>' +
            '<td class="n ' + f.cls(x.v) + '">' + fmt(x.v) + '</td><td>' + K.bar(x.v, max) + '</td><td class="n mut">' + Math.round(Math.abs(x.v) / absTot * 100) + '%</td><td class="n mut3">' + Math.round(cum / absTot * 100) + '%</td></tr>';
        }).join('') + '</tbody><tfoot><tr><td></td><td>Итого</td><td colspan="' + (dim === 'pos' ? 3 : 1) + '"></td><td class="n">' + f.mn(mvTot, 1) + '</td><td class="n ' + f.cls(tot) + '">' + fmt(tot) + '</td><td colspan="3"></td></tr></tfoot></table>';
      body.querySelectorAll('tr.row').forEach(tr => tr.onclick = () => {
        const x = g[tr.dataset.k];
        if (dim === 'pos') K.card(x.k); else K.drill(Object.assign({}, spec, { title: spec.title + ' · ' + x.label, rows: x.n, dim: 'pos' }));
      });
    }
    draw();
    return o;
  };

  // ---------- графики ----------
  K.charts = [];
  K.chartBase = () => ({
    backgroundColor: 'transparent', textStyle: { color: css('--fg-2'), fontFamily: 'system-ui, sans-serif', fontSize: 11 },
    color: K.PAL, animationDuration: 300,
  });
  function axisTheme(ax) {
    const a = Array.isArray(ax) ? ax : [ax];
    a.forEach(x => { if (!x) return; x.axisLine = Object.assign({ lineStyle: { color: css('--line-2') } }, x.axisLine); x.splitLine = Object.assign({ lineStyle: { color: css('--line') } }, x.splitLine); x.axisLabel = Object.assign({ color: css('--fg-2') }, x.axisLabel); x.nameTextStyle = Object.assign({ color: css('--fg-3') }, x.nameTextStyle); });
  }
  K.chart = function (el, optFn) {
    if (!window.echarts) { el.innerHTML = '<div class="k-empty">нет echarts</div>'; return { on() { }, dispose() { }, setOption() { } }; }
    let c = echarts.init(el, null, { renderer: 'canvas' });
    const build = () => {
      const o = Object.assign(K.chartBase(), typeof optFn === 'function' ? optFn() : optFn);
      axisTheme(o.xAxis); axisTheme(o.yAxis);
      if (o.tooltip) o.tooltip = Object.assign({ backgroundColor: css('--panel-2'), borderColor: css('--line-2'), textStyle: { color: css('--fg'), fontSize: 12 }, confine: true }, o.tooltip);
      if (o.legend) o.legend = Object.assign({ textStyle: { color: css('--fg-2') }, inactiveColor: css('--fg-3'), itemWidth: 12, itemHeight: 8 }, o.legend);
      return o;
    };
    c.setOption(build());
    const ro = new ResizeObserver(() => c.resize()); ro.observe(el);
    const w = { on: (...a) => c.on(...a), setOption: (o, x) => c.setOption(o, x), raw: c, redraw: () => { if (typeof optFn === 'function') c.setOption(build(), true); }, dispose: () => { ro.disconnect(); c.dispose(); const i = K.charts.indexOf(w); if (i >= 0) K.charts.splice(i, 1); }, update: fn => { optFn = fn; c.setOption(build(), true); } };
    K.charts.push(w);
    return w;
  };

  // Риск по бакетам: стек по кривым, ромб — итог бакета, линия — кумулятивный риск до срока.
  // opts: {curves:[id], list?, cs01?:bool, title?, height?}
  K.riskStack = function (el, opts) {
    opts = opts || {};
    const list = () => opts.list ? opts.list() : K.list();
    const ids = opts.curves;
    const ser = () => {
      const L = list();
      const data = ids.map(id => M.buckets.map((_, k) => id === 'CS01' ? M.sum(L, p => (!opts.ccy || p.ccy === opts.ccy) ? p.cs01b[k] : 0) : M.sum(L, p => p.risk[id][k])));
      // итог и кумулятив — только по процентным кривым; базис ОФЗ и CS01 — отдельными столбцами рядом
      const isRate = id => id !== 'CS01' && M.curve[id].kind === 'rate';
      const tot = M.buckets.map((_, k) => data.reduce((s, d, i) => s + (isRate(ids[i]) ? d[k] : 0), 0));
      let c = 0; const cum = tot.map(v => (c += v));
      return { data, tot, cum, L };
    };
    let cur = ser();
    let side = false, names = [], totals = {};
    const chart = K.chart(el, () => {
      cur = ser();
      side = opts.legend === 'side' || (opts.legend !== 'top' && el.clientWidth >= 560);
      names = ids.map(id => id === 'CS01' ? 'CS01' : M.curve[id].short);
      totals = {}; names.forEach((n, i) => totals[n] = cur.data[i].reduce((a, b) => a + b, 0));
      totals['итог бакета'] = cur.tot.reduce((a, b) => a + b, 0);
      return {
        // широкая панель: легенда слева столбиком — название кривой и её сумма; узкая — легенда сверху
        grid: side ? { left: 168, right: 8, top: 12, bottom: 4, containLabel: true } : { left: 8, right: 8, top: 34, bottom: 4, containLabel: true },
        legend: side ? { orient: 'vertical', left: 0, top: 'middle', itemGap: 9, data: names.concat(['итог бакета', 'кумулятив']), formatter: n => { const t = totals[n]; return t == null ? n : '{n|' + n + '}{v|' + f.dv(t) + '}'; }, textStyle: { color: css('--fg-2'), rich: { n: { width: 84, color: css('--fg-2') }, v: { width: 54, align: 'right', fontWeight: 600, color: css('--fg') } } } }
          : { top: 0, left: 0, data: names.concat(['итог бакета', 'кумулятив']) },
        tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: ps => { const k = ps[0].dataIndex; return '<b>' + M.buckets[k] + '</b><br>' + ps.map(p => p.marker + p.seriesName + ': <b>' + f.dv(p.value) + '</b>').join('<br>') + '<br><span style="opacity:.6">тыс ₽/бп · щелчок — вклад позиций</span>'; } },
        xAxis: { type: 'category', data: M.buckets },
        yAxis: [{ type: 'value', axisLabel: { formatter: v => f.dv(v) } }, { type: 'value', position: 'right', splitLine: { show: false }, axisLabel: { formatter: v => f.dv(v), color: css('--warn') } }],
        series: ids.map((id, i) => ({ name: id === 'CS01' ? 'CS01' : M.curve[id].short, type: 'bar', stack: id !== 'CS01' && M.curve[id].kind === 'rate' ? 'r' : 'x' + i, barMaxWidth: 38, data: cur.data[i], itemStyle: { color: K.curveColors[id] } }))
          .concat([{ name: 'итог бакета', type: 'scatter', symbol: 'diamond', symbolSize: 11, data: cur.tot, itemStyle: { color: css('--fg'), borderColor: css('--bg'), borderWidth: 1 }, z: 5, label: { show: opts.labels !== false, position: 'top', formatter: p => Math.abs(p.value) < 500 ? '' : f.dv(p.value), color: css('--fg-2'), fontSize: 10 } },
          { name: 'кумулятив', type: 'line', yAxisIndex: opts.cumSameAxis ? 0 : 1, smooth: false, data: cur.cum, symbol: 'circle', symbolSize: 4, lineStyle: { width: 2, type: 'dashed', color: css('--warn') }, itemStyle: { color: css('--warn') }, z: 4 }]),
      };
    });
    chart.on('click', pp => {
      const k = pp.dataIndex, L = cur.L;
      const rids = ids.filter(id => id !== 'CS01' && M.curve[id].kind === 'rate');
      if (pp.seriesName === 'кумулятив') return K.drill({ title: 'Кумулятивный риск до ' + M.buckets[k], unit: 'тыс ₽/бп', rows: L.map(p => ({ p, v: rids.reduce((s, id) => s + p.risk[id].slice(0, k + 1).reduce((a, b) => a + b, 0), 0) })) });
      if (pp.seriesName === 'итог бакета') return K.drill({ title: 'Риск ставок ' + M.buckets[k], unit: 'тыс ₽/бп', rows: L.map(p => ({ p, v: rids.reduce((s, id) => s + p.risk[id][k], 0) })) });
      const id = ids[pp.seriesIndex];
      K.drill({ title: (id === 'CS01' ? 'CS01' : M.curve[id].short) + ' · ' + M.buckets[k], unit: 'тыс ₽/бп', rows: L.map(p => ({ p, v: id === 'CS01' ? ((!opts.ccy || p.ccy === opts.ccy) ? p.cs01b[k] : 0) : p.risk[id][k] })) });
    });
    K.live(el, 'filter', () => chart.redraw());
    return chart;
  };

  // ---------- переключатель ----------
  K.seg = function (el, items, cur, fn) {
    el.innerHTML = ''; const box = h('span', 'k-seg');
    items.forEach(([v, t]) => { const b = h('button', v === cur ? 'on' : '', t); b.onclick = () => { box.querySelectorAll('button').forEach(x => x.classList.remove('on')); b.classList.add('on'); fn(v); }; box.appendChild(b); });
    el.appendChild(box); return box;
  };

  // ---------- сортировка строк и групп (R31) ----------
  // значения: пустые — в конец при любом направлении, строки — по алфавиту, числа — по величине
  const cmpVal = (x, y) => typeof x === 'string' ? x.localeCompare(y, 'ru') : x - y;
  const cmpList = (A, B, sort) => {
    for (let i = 0; i < sort.length; i++) { const x = A[i], y = B[i]; if (x == null && y == null) continue; if (x == null) return 1; if (y == null) return -1; const r = cmpVal(x, y); if (r) return sort[i][1] * r; }
    return 0;
  };
  // sort: [[ключ, ±1]]; col(ключ) → {val(r), sum(r)?} или null (колонки нет)
  K.rowCmp = (sort, col) => { const C = sort.map(([k]) => col(k)); const vs = r => C.map(c => c ? c.val(r) : null); return (a, b) => cmpList(vs(a), vs(b), sort); };
  // Порядок групп одного уровня (валюта, эмитент…). Без сортировки — порядок уровня (order: рубль первым, Минфин первым…).
  // С сортировкой группы — обычные поля: сортируются по тому же столбцу на каждом уровне. Столбец с итогом (DV01, MV, PnL…)
  // — по сумме группы, остальные — по первой строке группы в этом порядке (то есть столбец самой группы — валюта,
  // эмитент — по её имени, «Погашение» — по ближайшему); при равенстве — порядок уровня.
  K.groupKeys = (keys, rowsOf, sort, col, order) => {
    if (!sort.length) return order ? keys.slice().sort(order) : keys.slice();
    const C = sort.map(([k]) => col(k));
    const agg = new Map(keys.map(k => { const L = rowsOf(k); return [k, C.map(c => !c ? null : c.sum ? L.reduce((t, r) => t + (c.sum(r) || 0), 0) : (L.length ? c.val(L[0]) : null))]; }));
    return keys.slice().sort((a, b) => cmpList(agg.get(a), agg.get(b), sort) || (order ? order(a, b) : 0));
  };

  // ---------- таблица ----------
  // opts: {columns:[{key,label,val(r),fmt(v,r),cls,title,sum:true|fn,drill:{title,unit,fmt,val}, w}], rows|rowsFn, groupBy:[{key(r),label(k,rows)}], search:bool,
  //        sort:[[key,dir]] (по умолчанию — строки в группах; щелчок по заголовку сортирует и группы, кроме groupBy[i].fixed), stripe(r)->color, onRow(r), rowCls(r), tools(el), maxHeight, footer:bool}
  K.table = function (el, opts) {
    const st = { sort: opts.sort ? opts.sort.slice() : [], q: '', collapsed: {}, groupSort: false };
    el.innerHTML = '';
    let tools = null;
    if (opts.search !== false || opts.tools) {
      tools = h('div', 'k-tbl-tools'); el.appendChild(tools);
      if (opts.search !== false) { const i = h('input', 'k-input'); i.placeholder = 'поиск в таблице…'; i.oninput = () => { st.q = i.value.toLowerCase(); draw(); }; tools.appendChild(i); }
      tools.appendChild(h('span', 'k-note', 'щелчок по заголовку сортирует строки и группы · Shift+щелчок — по нескольким колонкам'));
      if (opts.tools) { const sp = h('span'); sp.style.flex = '1'; tools.appendChild(sp); opts.tools(tools); }
    }
    const wrap = h('div', 'k-tbl-wrap'); if (opts.maxHeight) wrap.style.maxHeight = opts.maxHeight; el.appendChild(wrap);
    const cols = () => opts.columns.filter(c => !c.hidden);
    const val = (c, r) => c.val ? c.val(r) : r[c.key];
    // колонка для сортировки строк и групп: значение и, если у колонки есть итог, слагаемое суммы группы
    const colOf = k => { const c = opts.columns.find(x => x.key === k); return c ? { val: r => val(c, r), sum: c.sum ? (typeof c.sum === 'function' ? c.sum : r => val(c, r) || 0) : null } : null; };
    function sorted(rows) {
      if (!st.sort.length) return rows;
      return rows.slice().sort(K.rowCmp(st.sort, colOf));
    }
    function draw() {
      let rows = opts.rowsFn ? opts.rowsFn() : opts.rows;
      if (st.q) rows = rows.filter(r => (r.searchText || JSON.stringify(r)).toLowerCase().includes(st.q));
      rows = sorted(rows);
      const C = cols();
      const head = '<thead><tr>' + (opts.stripe ? '<th class="stripe"></th>' : '') + C.map(c => { const si = st.sort.findIndex(s => s[0] === c.key); return '<th class="sortable ' + (c.num ? 'n' : '') + '" data-k="' + c.key + '" title="' + esc(c.title || '') + '"' + (c.w ? ' style="width:' + c.w + '"' : '') + '>' + (c.tip ? '<span class="k-hint">' + c.label + '</span>' : c.label) + (si >= 0 ? '<span class="si">' + (st.sort[si][1] > 0 ? '▲' : '▼') + (st.sort.length > 1 ? si + 1 : '') + '</span>' : '') + '</th>'; }).join('') + '</tr></thead>';
      const cell = (c, r) => { const v = val(c, r); return '<td class="' + (c.num ? 'n ' : '') + (c.cls ? (typeof c.cls === 'function' ? c.cls(v, r) : c.cls) : '') + '"' + (c.style ? ' style="' + c.style(v, r) + '"' : '') + '>' + (c.fmt ? c.fmt(v, r) : esc(v)) + '</td>'; };
      const sumCell = (c, list) => {
        if (!c.sum) return '<td></td>';
        const fn = typeof c.sum === 'function' ? c.sum : r => val(c, r) || 0;
        const tot = list.reduce((s, r) => s + fn(r), 0);
        const fm = c.sumFmt || c.fmt || (v => f.n(v));
        if (c.drill !== false && list.length && list[0].searchText) return '<td class="n">' + K.d({ title: (c.drillTitle || c.label.replace(/<[^>]+>/g, '')), unit: c.unit, fmt: v => fm(v, {}), rows: list.map(p => ({ p, v: fn(p) })) }, fm(tot, {})) + '</td>';
        return '<td class="n ' + f.cls(tot) + '">' + fm(tot, {}) + '</td>';
      };
      let body = '';
      const groups = opts.groupBy || [];
      const nCols = C.length + (opts.stripe ? 1 : 0);
      function emit(list, level, path) {
        if (level >= groups.length) { list.forEach(r => { body += '<tr class="row ' + (opts.rowCls ? opts.rowCls(r) : '') + '" data-id="' + esc(r.id) + '">' + (opts.stripe ? '<td class="stripe" style="background:' + opts.stripe(r) + '"></td>' : '') + C.map(c => cell(c, r)).join('') + '</tr>'; }); return; }
        const G = groups[level], buckets = new Map();
        list.forEach(r => { const k = G.key(r); if (!buckets.has(k)) buckets.set(k, []); buckets.get(k).push(r); });
        // группы — обычные поля (R31): после щелчка по заголовку сортируются и они; сортировка страницы по умолчанию
        // (opts.sort) — только строки внутри групп; уровень с fixed (недели календаря) — всегда в своём порядке
        const keys = K.groupKeys([...buckets.keys()], k => buckets.get(k), st.groupSort && !G.fixed ? st.sort : [], colOf, G.order);
        keys.forEach(k => {
          const sub = buckets.get(k), p = path + '/' + k, col = st.collapsed[p] != null ? st.collapsed[p] : !!G.collapsed;
          body += '<tr class="grp l' + level + '" data-g="' + esc(p) + '">' + (opts.stripe ? '<td class="stripe" style="background:' + (G.color ? G.color(k, sub) : 'transparent') + '"></td>' : '') +
            C.map((c, i) => i === 0 ? '<td colspan="' + (G.span || 1) + '" style="padding-left:' + (8 + level * 14) + 'px"><span class="tw">' + (col ? '▸' : '▾') + '</span>' + (G.label ? G.label(k, sub) : esc(k)) + ' <span class="mut3" style="font-weight:400">' + sub.length + '</span></td>' : (i < (G.span || 1) ? '' : sumCell(c, sub))).join('') + '</tr>';
          if (!col) emit(sub, level + 1, p);
        });
      }
      emit(rows, 0, '');
      const foot = opts.footer === false ? '' : '<tfoot><tr>' + (opts.stripe ? '<td></td>' : '') + C.map((c, i) => i === 0 ? '<td>Итого <span class="mut3" style="font-weight:400">' + rows.length + '</span></td>' : sumCell(c, rows)).join('') + '</tr></tfoot>';
      wrap.innerHTML = '<table class="k-tbl">' + head + '<tbody>' + (body || '<tr><td colspan="' + nCols + '" class="k-empty">ничего не найдено</td></tr>') + '</tbody>' + foot + '</table>';
      wrap.querySelectorAll('th.sortable').forEach(th => th.onclick = e => {
        const k = th.dataset.k, i = st.sort.findIndex(s => s[0] === k);
        if (e.shiftKey) { if (i >= 0) { if (st.sort[i][1] > 0) st.sort[i][1] = -1; else st.sort.splice(i, 1); } else st.sort.push([k, 1]); }
        else st.sort = i >= 0 && st.sort.length === 1 ? (st.sort[0][1] > 0 ? [[k, -1]] : []) : [[k, 1]];
        st.groupSort = true;
        draw();
      });
      wrap.querySelectorAll('tr.grp').forEach(tr => tr.onclick = e => { if (e.target.closest('[data-dr]')) return; const p = tr.dataset.g; const G = groups[(p.match(/\//g) || []).length - 1]; const cur = st.collapsed[p] != null ? st.collapsed[p] : !!(G && G.collapsed); st.collapsed[p] = !cur; draw(); });
      wrap.querySelectorAll('tr.row').forEach(tr => tr.onclick = e => { if (e.target.closest('[data-dr]')) return; const id = tr.dataset.id; const r = rows.find(x => String(x.id) === id); if (opts.onRow) opts.onRow(r); else if (M.pos[id]) K.card(id); });
    }
    draw();
    const api = { redraw: draw, state: st, setSort: s => { st.sort = s; draw(); }, el };
    if (opts.autoFilter !== false && !opts.rows) K.live(el, 'filter', draw);
    return api;
  };

  // стандартные колонки позиций (концепты берут нужные по ключу)
  K.posCols = {
    issuer: { key: 'issuerName', label: 'Эмитент', fmt: (v, r) => '<b style="color:' + K.issuerColor(r.issuer) + '">●</b> ' + esc(v) },
    name: { key: 'name', label: 'Бумага', fmt: (v, r) => K.nameCell(r, v) + (r.tradedToday ? ' <span class="k-tag" style="border-color:var(--warn);color:var(--warn)" title="сделки сегодня">сд</span>' : '') },
    isin: { key: 'isin', label: 'ISIN', fmt: v => '<span class="mono mut">' + esc(v || '—') + '</span>' },
    ccy: { key: 'ccy', label: 'Вал', fmt: (v, r) => K.ccyTag(r.ccy), val: r => M.ccyOrder[r.ccy] },
    kind: { key: 'kind', label: 'Тип', fmt: (v, r) => '<span class="tagc" style="border-color:' + K.kindColor[v] + '55">' + esc(M.kindShort[v]) + '</span>' },
    book: { key: 'book', label: 'Книга', fmt: v => '<span class="mut">' + esc(v) + '</span>' },
    coupon: { key: 'couponFmt', label: 'Купон', fmt: v => '<span class="mono">' + esc(v || '—') + '</span>' },
    event: { key: 'event', label: 'Событие', val: r => r.event.date, fmt: (v, r) => '<span class="mut3">' + ({ put: 'пут', call: 'колл', mat: 'погаш' }[r.event.kind]) + '</span> ' + f.date(r.event.date) },
    maturity: { key: 'maturity', label: 'Погашение', fmt: v => f.date(v) },
    offer: { key: 'offer', label: 'Оферта', fmt: (v, r) => v ? f.date(v) + ' <span class="mut3">' + (r.offerKind === 'put' ? 'P' : 'C') + '</span>' : '<span class="mut3">—</span>' },
    tenor: { key: 'tenor', label: 'Срок', num: true, fmt: v => f.yrs(v), title: 'до оферты, если есть, иначе до погашения' },
    notional: { key: 'notional', label: 'Номинал', num: true, fmt: (v, r) => f.mn(v, 2) + '<span class="mut3">mm ' + r.ccy + '</span>', title: 'в валюте бумаги' },
    mv: { key: 'mv', label: 'MV, mm ₽', num: true, fmt: v => f.mn(v, 0), sum: true, unit: 'mm ₽', sumFmt: v => f.mn(v, 0) },
    price: { key: 'price', label: 'Цена', num: true, fmt: v => f.px(v) },
    ytm: { key: 'ytm', label: 'Дох.', num: true, fmt: v => f.pct(v) },
    spread: { key: 'zspread', label: 'Спред', num: true, fmt: (v, r) => v == null ? '<span class="mut3">—</span>' : f.bp(v) + ' <span class="mut3">' + (r.spreadLabel || '') + '</span>', title: 'Z-спред к RUONIA (фикс) / дисконт-маржа (флоатер)' },
    // Z-спред к кривой дисконтирования (RUONIA) у фикс-бумаг и флоатеров (у флоатера — купоны по прогнозу): сравнимы между собой (R32)
    zdisc: { key: 'zdisc', label: 'Z к RUONIA', num: true, val: r => r.zSpreadZ, fmt: v => v == null ? '<span class="mut3">—</span>' : f.bp(v), title: K.zdiscTip, tip: true },
    gspread: { key: 'gspread', label: 'G-спред', num: true, fmt: v => f.bp(v) },
    dur: { key: 'mdur', label: 'Дюр.', num: true, fmt: v => v == null ? '—' : f.n(v, 2) },
    dv01: { key: 'dv01', label: 'DV01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп', title: K.tips.dv01, tip: true },
    cs01: { key: 'cs01', label: 'CS01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп', title: K.tips.cs01, tip: true },
    basis01: { key: 'basis01', label: 'Базис01', num: true, fmt: v => v ? f.dv(v) : '<span class="mut3">·</span>', sum: true, unit: 'тыс ₽/бп', title: K.tips.basis01, tip: true },
    carry: { key: 'carryDay', label: 'Кэрри/д', num: true, fmt: v => f.k(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽', title: 'тыс ₽ в день' },
    pnl: { key: 'pnl', label: 'PnL дня', num: true, val: r => r.pnl.total, fmt: v => f.k(v), cls: v => f.cls(v), sum: r => r.pnl.total, unit: 'тыс ₽' },
    pnlMtd: { key: 'pnlMtd', label: 'PnL MTD', num: true, fmt: v => f.mn(v, 1), cls: v => f.cls(v), sum: true, unit: 'mm ₽', sumFmt: v => f.mn(v, 1) },
    rating: { key: 'rating', label: 'Рейтинг', fmt: v => '<span class="mut">' + esc(v) + '</span>' },
    sector: { key: 'sector', label: 'Сектор', fmt: v => '<span class="mut">' + esc(v) + '</span>' },
  };
  K.cols = keys => keys.map(k => typeof k === 'string' ? Object.assign({}, K.posCols[k]) : k);
  // порядок эмитентов в группах: Минфин первым, деривативы последними, остальные по алфавиту
  K.issuerOrder = (a, b) => { const r = x => x === 'Минфин' ? 0 : x === 'Деривативы' ? 2 : 1; return (r(a) - r(b)) || a.localeCompare(b, 'ru'); };
  // группировка по умолчанию: валюта → эмитент (у рублёвых эмитентов — флоатеры отдельно от фикса)
  K.defaultGroups = [
    { key: r => r.ccy, label: k => K.ccyTag(k) + ' ' + K.ccyName(k), order: (a, b) => M.ccyOrder[a] - M.ccyOrder[b], color: k => K.ccyColor(k), span: 1 },
    { key: r => r.group === 'DER' ? 'Деривативы' : r.issuerName, label: (k, rows) => esc(k) + (rows[0].group === 'DER' ? ' <span class="mut3" style="font-weight:400">(по контрагентам)</span>' : ''), order: K.issuerOrder, color: (k, rows) => K.issuerColor(rows[0].issuer) },
  ];

  // ---------- FX-дельта, риск по кривым, ноги FX-сделки (карточки C01–C13) ----------
  // валюты ОВП под фильтром: валюты деска, кроме рубля, и любая валюта FX-дельты позиций (вторая валюта кросса)
  K.fxCcys = L => [...new Set(M.ccys.filter(c => c !== 'RUB').concat((L || K.list()).flatMap(p => Object.keys(INSTR.fxBy(p)).filter(c => c !== 'RUB' && INSTR.fxIn(p, c)))))];
  // FX-дельта позиции по валютам (fx_delta {ccy}): «−55.0 mm USD · +391.6 mm CNY»
  K.fxDeltaHtml = p => {
    const e = Object.entries(INSTR.fxBy(p)).filter(([, v]) => v).sort((a, b) => ((M.ccyOrder[a[0]] ?? 99) - (M.ccyOrder[b[0]] ?? 99)));
    return e.length ? e.map(([c, v]) => '<span class="' + f.cls(v) + '">' + f.mns(v, 1) + '</span> <small>mm ' + esc(c) + '</small>').join(' · ') : '—';
  };
  // DV01 по кривым — у каждой позиции с риском ставок (R32; раньше — только когда кривых несколько или кривая другой
  // валюты): поле [подпись, значение, во всю ширину, подсказка]
  K.curveRiskFields = p => {
    const ids = M.curves.filter(c => c.kind === 'rate' && Math.abs(p.dv01ByCurve[c.id] || 0) >= 1).map(c => c.id);
    if (!ids.length) return [];
    const by = INSTR.dv01By(p), ccys = Object.keys(by), cv = M.curve;
    const rows = [['DV01 по кривым', ids.map(id => '<span style="color:' + K.curveColors[id] + '">■</span> ' + esc(cv[id].short) + (cv[id].short.includes(cv[id].ccy) ? '' : ' <small>' + esc(cv[id].ccy) + '</small>') + ' ' + f.dv(p.dv01ByCurve[id])).join(' · ') + ' <small>тыс ₽/бп</small>', true, K.tips.dv01]];
    // по валютам — когда у валюты несколько кривых (иначе то же, что по кривым)
    if (ccys.length < ids.length) rows.push(['DV01 по валютам кривых', ccys.map(c => esc(c) + ' ' + f.dv(by[c])).join(' · ') + ' <small>тыс ₽/бп · по ногам</small>', true]);
    return rows;
  };
  // риск позиции по кривым и срокам (R32): кривые ставок, Σ ставок (если кривых несколько), базис ОФЗ, CS01; столбцы —
  // итог и бакеты, где у позиции есть риск; подпись строки — подсказка, в какой конвенции бамп
  K.curveRiskTable = p => {
    const rate = M.curves.filter(c => c.kind === 'rate' && p.risk[c.id] && p.risk[c.id].some(v => Math.abs(v) >= 1));
    const basis = M.curves.filter(c => c.kind !== 'rate' && p.risk[c.id] && p.risk[c.id].some(v => Math.abs(v) >= 1));
    const rows = rate.map(c => ({ id: c.id, label: c.short, vals: p.risk[c.id] }));
    if (rate.length > 1) rows.push({ id: 'Σ', label: 'Σ ставки', vals: M.buckets.map((_, k) => rate.reduce((s, c) => s + p.risk[c.id][k], 0)), tot: true });
    basis.forEach(c => rows.push({ id: c.id, label: c.short, vals: p.risk[c.id] }));
    if (p.cs01b && p.cs01b.some(v => Math.abs(v) >= 1)) rows.push({ id: 'CS01', label: 'CS01', vals: p.cs01b });
    if (!rows.length) return '<div class="k-empty">нет риска ставок и спреда</div>';
    const ks = M.buckets.map((_, k) => k).filter(k => rows.some(r => Math.abs(r.vals[k]) >= 1));
    const cell = v => '<td class="n ' + (Math.abs(v) >= 1 ? f.cls(v) : '') + '">' + (Math.abs(v) >= 1 ? f.dv(v) : '<span class="mut3">·</span>') + '</td>';
    return '<div class="k-tbl-wrap"><table class="k-tbl k-compact k-crisk"><thead><tr><th>Кривая</th><th class="n">Итого</th>' + ks.map(k => '<th class="n">' + M.buckets[k] + '</th>').join('') + '</tr></thead><tbody>' +
      rows.map(r => '<tr' + (r.tot ? ' class="grp"' : '') + '><td>' + (r.tot ? '' : '<b style="color:' + K.curveColors[r.id] + '">■</b> ') + K.hint(esc(r.label), r.tot ? K.tips.dv01 : K.curveTip(r.id)) + '</td><td class="n"><b>' + f.dv(r.vals.reduce((a, b) => a + b, 0)) + '</b></td>' + ks.map(k => cell(r.vals[k])).join('') + '</tr>').join('') +
      '</tbody></table></div>';
  };
  // спред бумаги в карточке (R32): фикс — Z-спред к кривой дисконтирования; флоатер — дисконт-маржа к кривой проекции и
  // Z-спред к RUONIA (данные прогона: spreadBp — Z ядра к кривой дисконтирования, extra.dm_bp — DM); поля [подпись, значение, -, подсказка]
  K.spreadFields = p => {
    const to = p.spreadCurve === 'RUB OFZ' ? 'КБД' : p.spreadCurve === 'RUB RUONIA' || p.ccy === 'RUB' ? 'RUONIA' : 'кривой ' + p.ccy;
    if (!p.cpn || p.cpn.type !== 'float') return [['Z-спред к ' + to, f.bp(p.zSpreadZ != null ? p.zSpreadZ : p.zspread) + ' <small>б.п.</small>', false, K.tips.zdisc]];
    const out = [['Дисконт-маржа', f.bp(p.dm) + ' <small>б.п.' + (p.dmSource === 'z' ? ' · = Z: DM ядра нет' : ' · к кривой проекции') + '</small>', false, 'Дисконт-маржа (DM) — спред к кривой проекции купонов флоатера (КС или RUONIA), годовой, ACT/365F']];
    if (p.zSpreadZ != null && p.dmSource !== 'z') out.push(['Z-спред к ' + to, f.bp(p.zSpreadZ) + ' <small>б.п. · как у фиксов</small>', false, K.tips.zdisc]);
    return out;
  };
  // ноги FX-сделки (p.legs: extra.legs прогона или демо): даты, суммы, курс сделки и форварда, пойнты, расчёт, фиксинг
  K.legsHtml = p => {
    const L = p.legs || [], any = k => L.some(l => l[k] != null && l[k] !== '');
    const LEG = { single: '—', near: 'ближняя', far: 'дальняя' }, SET = { deliverable: 'поставка', cash: 'расчётный (NDF)' };
    const amt = (c, v) => v == null ? '—' : f.mn(v, 2) + ' <small>mm ' + esc(c || '') + '</small>';
    const rate = v => v == null ? '—' : f.n(v, 4);
    const fwd = l => l.fwdRate != null ? l.fwdRate : l.forwardRate;
    const cols = [['Нога', l => esc(LEG[l.leg] || l.leg || '—')], ['Валютирование', l => l.valueDate ? f.date(l.valueDate) : '—'],
      ['Покупка', l => amt(l.buyCcy, l.buyAmount)], ['Продажа', l => amt(l.sellCcy, l.sellAmount)],
      ['Курс сделки', l => rate(l.contractRate) + (l.rateQuote ? ' <small>' + esc(l.rateQuote) + '</small>' : '')]]
      .concat(L.some(l => fwd(l) != null) ? [['Форвард', l => rate(fwd(l))]] : [])
      .concat(any('points') ? [['Пойнты', l => l.points == null ? '—' : f.s(l.points, 4)]] : [])
      .concat(any('settlement') ? [['Расчёт', l => esc(SET[l.settlement] || l.settlement || '—') + (l.settlementCcy ? ' <small>в ' + esc(l.settlementCcy) + '</small>' : '')]] : [])
      .concat(any('fixingDate') ? [['Фиксинг', l => l.fixingDate ? f.date(l.fixingDate) + (l.fixingSource ? ' <small>' + esc(l.fixingSource) + '</small>' : '') + (l.fixingRate != null ? ' <small>' + rate(l.fixingRate) + '</small>' : '') : '—']] : [])
      .concat(any('paymentDate') ? [['Выплата', l => l.paymentDate ? f.date(l.paymentDate) : '—']] : []);
    return '<div class="k-tbl-wrap"><table class="k-tbl k-compact"><thead><tr>' + cols.map(c => '<th' + (c[0] === 'Нога' || c[0] === 'Расчёт' ? '' : ' class="n"') + '>' + c[0] + '</th>').join('') + '</tr></thead><tbody>' +
      L.map(l => '<tr>' + cols.map(c => '<td' + (c[0] === 'Нога' || c[0] === 'Расчёт' ? '' : ' class="n"') + '>' + c[1](l) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
  };

  // ---------- карточка позиции ----------
  // ---------- история позиции по прогонам: на чём стоит точка (R44; api/concepts.positions_history) ----------
  // H.runAt[i] — прогон даты (null: прогона нет), H.pt[i] — [вид, текст] точки не по ядру, H.qty — бумаг по прогонам
  const PT_LABEL = { old: 'прошлая версия программы', method: 'другой метод оценки', unvalued: 'не оценена', gap: 'нет прогона за дату', minor: 'замечание', before: 'до истории прогонов' };
  K.histPoint = (H, i) => {
    const RA = H.runAt; if (!RA) return null;
    if (H._first == null) H._first = RA.findIndex(Boolean);
    const r = RA[i], pt = H.pt && H.pt[i];
    if (!r) return i < H._first || H._first < 0 ? { k: 'before', t: 'прогонов до этой даты нет' } : { k: 'gap', t: 'прогона за дату нет — значения риска за неё нет (не переносим с прошлого прогона)' };
    const notes = [];
    if (r.cut) notes.push(r.cut === 'eod_preliminary' ? 'предварительное закрытие (финального среза за дату нет)' : 'срез ' + r.cut);
    if (pt && pt[0] !== 'minor') return { k: pt[0], t: pt[1] + (r.old ? '; прогон версии ' + r.old : '') };
    if (pt && pt[1]) notes.push(pt[1]);
    if (r.old) return { k: 'old', t: 'посчитано версией ' + r.old + ' — пересчитается историей прогонов' + (notes.length ? '; ' + notes.join('; ') : '') };
    return notes.length ? { k: 'minor', t: notes.join('; ') } : null;
  };
  // ряды DV01 / номинала / DM позиции: { series, tooltip, note }. opt.perBond — DV01 на 1 бумагу (₽/бп): видно, что
  // изменилось — размер позиции или риск на бумагу
  K.runSeries = function (H, mode, opt) {
    opt = opt || {};
    const xs = H.dates, RA = H.runAt, per = mode === 'dv01' && opt.perBond && H.qty;
    const raw = (mode === 'dm' ? H.dm : mode === 'dv01' ? H.dv01 : H.notional) || [];
    const name = mode === 'dm' ? 'DM, б.п.' : mode === 'notional' ? 'Номинал, mm' : per ? 'DV01, ₽/бп на 1 бумагу' : 'DV01, тыс ₽/бп';
    const val = i => { const v = raw[i]; if (v == null) return RA && RA[i] && mode !== 'dm' ? 0 : null; if (per) { const q = H.qty[i]; return q ? v / q : null; } return mode === 'dv01' ? v / 1e3 : mode === 'notional' ? v / 1e6 : v; };
    const P = xs.map((_, i) => K.histPoint(H, i)), V = xs.map((_, i) => val(i));
    const main = V.map((v, i) => !P[i] || P[i].k === 'minor' ? v : null), old = V.map((v, i) => P[i] && P[i].k === 'old' ? v : null);
    const iso = a => a.map((v, i) => v != null && a[i - 1] == null && a[i + 1] == null ? [i, v] : null).filter(Boolean);  // одиночная точка линией не видна
    const pts = k => V.map((v, i) => P[i] && P[i].k === k && v != null ? [i, v] : null).filter(Boolean);
    const step = mode === 'dm' ? false : 'end', warn = css('--warn') || '#e3b341', c0 = opt.color || css('--accent');
    const series = [{ type: 'line', name, data: main, step, connectNulls: mode === 'dm', showSymbol: false, lineStyle: { width: 1.6 }, color: c0 }]
      .concat(iso(main).length ? [{ type: 'scatter', name, data: iso(main), symbolSize: 4, color: c0 }] : [])
      .concat(old.some(v => v != null) ? [{ type: 'line', name: 'прошлая версия расчёта', data: old, step, connectNulls: mode === 'dm', showSymbol: false, lineStyle: { width: 1.2, type: 'dashed' }, color: css('--fg-3') }] : [])
      .concat(iso(old).length ? [{ type: 'scatter', name: 'прошлая версия расчёта', data: iso(old), symbolSize: 4, color: css('--fg-3') }] : [])
      .concat(pts('method').length ? [{ type: 'scatter', name: 'другой метод оценки', data: pts('method'), symbol: 'diamond', symbolSize: 7, color: warn }] : []);
    // замечания (допущение ядра, позиции книги из прошлого отчёта, предварительное закрытие) — в подсказке, без маркера
    const at = Object.fromEntries(xs.map((d, i) => [d, i]));
    const fmt = v => v == null ? '—' : mode === 'dm' ? f.n(v, 1) : per ? f.n(v, 3) : f.n(v, mode === 'dv01' ? 3 : 2);
    const tooltip = { trigger: 'axis', formatter: ps => {
      const i = at[(ps[0] || {}).axisValue]; if (i == null) return '';
      const q = H.qty && H.qty[i], q0 = H.qty && i > 0 ? H.qty[i - 1] : null, pt = P[i];
      return '<b>' + esc(f.date(xs[i])) + '</b><br>' + esc(name) + ': <b>' + fmt(pt && (pt.k === 'gap' || pt.k === 'unvalued' || pt.k === 'before') ? null : V[i]) + '</b>' +
        (q != null ? '<br><span style="opacity:.75">бумаг: ' + f.n(q) + (q0 != null && Math.abs(q - q0) > 1e-6 ? ' (' + f.s(q - q0) + ' к ' + esc(f.date(xs[i - 1])) + ')' : '') + '</span>' : '') +
        (pt && pt.k !== 'before' ? '<br><span style="color:' + warn + '">' + esc(PT_LABEL[pt.k]) + ': ' + esc(pt.t) + '</span>' : '');
    } };
    // покрытие ряда: с первой точки, где позиция есть
    const from = V.findIndex((v, i) => (v != null && v !== 0) || (P[i] && P[i].k === 'unvalued'));
    const cnt = { clean: 0, old: 0, method: 0, unvalued: 0, gap: 0 };
    if (from >= 0) for (let i = from; i < xs.length; i++) { const k = P[i] ? P[i].k : 'clean'; if (k === 'minor') cnt.clean++; else if (k === 'gap') { if (raw[i] != null) cnt.gap++; } else if (k in cnt) cnt[k]++; }
    const note = !RA ? '' : 'Точки ряда: ' + cnt.clean + ' — ядро на прогоне своей даты' + (cnt.old ? ' · ' + cnt.old + ' — прошлой версией программы (пунктир)' : '') +
      (cnt.method ? ' · ' + cnt.method + ' — другим методом (◆, причина в подсказке)' : '') + (cnt.unvalued ? ' · ' + cnt.unvalued + ' — позиция не оценена (разрыв)' : '') +
      (cnt.gap ? ' · ' + cnt.gap + ' — прогона за дату нет (разрыв)' : '') + (cnt.old || cnt.gap ? '. Досчитать: ' + K.noteHtml(BF) : '') +
      (mode === 'dv01' && H.qty ? '. Тренд риска на бумагу и размер позиции различает «на 1 бумагу»; число бумаг и его изменение — в подсказке' : '');
    return { series, tooltip, note };
  };

  K.card = function (id) {
    const p = M.pos[id]; if (!p) return;
    const o = drawer(true); o.title = p.name; o.charts = [];
    const te = o.el.querySelector('.t');
    te.innerHTML = '<span style="color:' + K.issuerColor(p.issuer) + '">●</span> ' + K.aliasName(p) + ' ' + K.ccyTag(p.ccy);
    K.aliasBind(te, p, () => { o.title = p.name; });
    o.el.querySelector('.st').innerHTML = esc(p.issuerFull) + (p.isin ? ' · <span class="mono">' + esc(p.isin) + '</span>' : '') + ' · ' + esc(p.book) + ' — ' + esc(p.bookName) + ' · ' + esc(p.groupLabel) + ' / ' + esc(p.kindLabel);
    const isBond = !!p.cpn;
    const ev = { put: 'пут-оферта', call: 'колл-опцион', mat: 'погашение' }[p.event.kind];
    const fields = (isBond ? [
      ['Номинал', f.mn(p.notional, 0) + ' <small>mm ' + p.ccy + '</small>'], ['MV', f.mn(p.mv, 1) + ' <small>mm ₽</small>'],
      ['Цена (bid/ask)', f.px(p.price) + ' <small>' + f.px(p.bid) + ' / ' + f.px(p.ask) + '</small>'], ['Изм. цены', p.prevPrice == null || p.price == null ? '<span class="mut3">—</span>' : '<span class="' + f.cls(p.price - p.prevPrice) + '">' + f.s(p.price - p.prevPrice, 3) + '</span>'],
      p.cpn.type === 'float' ? ['Доходность', '<span class="mut3">—</span> <small>флоатер: смотрим DM</small>'] : ['Доходность', f.pct(p.ytm) + ' <small>к ' + (p.event.kind === 'mat' ? 'погаш.' : 'оферте') + '</small>'], ...K.spreadFields(p),
      ['G-спред к ОФЗ', f.bp(p.gspread) + ' <small>б.п.</small>'], ['Мод. дюрация', p.mdur == null ? '—' : f.n(p.mdur, 2)],
      ['Купон', '<span class="mono">' + esc(p.couponFmt) + '</span>'], ['Текущий купон', f.pct(p.curCpn) + (p.cpn.type === 'float' ? ' <small>прогноз</small>' : '')],
      ['Ближайшее событие', f.date(p.event.date) + ' <small>' + ev + ', ' + f.yrs(p.tenor) + '</small>'], ['Погашение', f.date(p.maturity) + ' <small>' + f.yrs(p.yearsToMat) + '</small>'],
      ['DV01 ставки', K.d({ title: 'DV01 ' + p.name, unit: 'тыс ₽/бп', rows: [{ p, v: p.dv01 }] }, f.dv(p.dv01), '') + ' <small>тыс ₽/бп</small>', false, K.tips.dv01], ['CS01', f.dv(p.cs01) + ' <small>тыс ₽/бп</small>', false, K.tips.cs01],
      ['Базис ОФЗ01', p.basis01 ? f.dv(p.basis01) + ' <small>тыс ₽/бп</small>' : '—', false, K.tips.basis01], ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽ (фонд. ' + f.pct(p.fundRate) + ')</small>'],
      ['НКД', f.mn(p.accrued, 2) + ' <small>mm ₽</small>'], ['Рейтинг / сектор', '<span title="' + esc(K.ratingTip(p.rating, p.ratingInfo)) + '">' + esc(K.ratingText(p.rating, p.ratingInfo)) + '</span> <small>' + esc(p.sector) + '</small>'],
    ] : [
      ['Номинал', f.mn(p.notional, 0) + ' <small>mm ' + p.ccy + '</small>'], ['MTM', f.mn(p.mv, 1) + ' <small>mm ₽</small>'],
      ['Ставка / курс', esc(p.couponFmt)], ['Срок', f.date(p.maturity) + ' <small>' + f.yrs(p.tenor) + '</small>'],
      ['DV01', f.dv(p.dv01) + ' <small>тыс ₽/бп</small>', false, K.tips.dv01], ['FX дельта', K.fxDeltaHtml(p), Object.values(INSTR.fxBy(p)).filter(v => v).length > 1],
      ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽</small>'], ['Контрагент', esc(p.cpty || '—')],
    ]).concat(K.curveRiskFields(p));  // у облигаций тоже (R32)
    if (isBond && p.kind === 'IN' && M.posHistory) {  // линкер: последние ICPI / IR / BEI MOEX (контракт 1.2), если есть
      const LH = M.posHistory(p.id, { deep: true }), last = a => { for (let i = (a || []).length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };
      if (LH.icpi || LH.bei) fields.push(['ICPI (MOEX)', last(LH.icpi) == null ? '—' : f.n(last(LH.icpi), 4)], ['IR / BEI (MOEX)', f.pct(last(LH.ir)) + ' <small>/ ' + f.pct(last(LH.bei)) + '</small>']);
      // ИПЦ г/г рядом с ICPI (R25): ЦБ (контракт 1.3), иначе по ИПЦ м/м Росстата — последний известный на дату
      const cpiNow = last(LH.cpi) != null ? last(LH.cpi) : M.indices['CPI г/г'];
      if (cpiNow != null) fields.push(['ИПЦ г/г', f.pct(cpiNow) + ' <small>' + (LH.bei && last(LH.bei) != null ? 'BEI − ИПЦ ' + f.bps((last(LH.bei) - cpiNow) * 100) + ' б.п.' : 'последний известный') + '</small>']);
    }
    const pn = p.pnl;
    o.bd.innerHTML = '<div class="k-tags">' + [p.desk, (M.entities.find(e => e.id === p.entity) || {}).name, p.rating !== '—' ? 'рейтинг ' + K.ratingText(p.rating, p.ratingInfo) : null, p.tradedToday ? 'сделки сегодня' : null, p.quality && p.quality !== 'exact' ? 'оценка: ' + p.quality : null].filter(t => t && t !== '—').map(t => '<span class="k-tag">' + esc(t) + '</span>').join('') + '</div>' +
      '<div class="k-sec">Параметры</div>' + K.fieldsHtml(fields) +
      (p.legs && p.legs.length ? '<div class="k-sec">Ноги сделки <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">' + p.legs.length + '</span></div>' + K.legsHtml(p) : '') +
      '<div class="k-sec">PnL <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">тыс ₽</span></div>' +
      '<div class="k-fields">' + [['Итого день', pn.total], ['Кэрри', pn.carry], ['Ставки', pn.rates], ['Спред', pn.spread], ['FX', pn.fx], ['Сделки', pn.trading], ['Прочее', pn.other], ['MTD', p.pnlMtd], ['YTD', p.pnlYtd]].map(([l, v]) => '<div><div class="l">' + l + '</div><div class="v ' + f.cls(v) + '">' + (Math.abs(v) >= 1e7 ? f.mns(v, 1) + ' <small>mm</small>' : f.ks(v)) + '</div></div>').join('') + '</div>' +
      '<div class="k-sec">История <span class="sp"></span><span class="hunit" style="margin-right:6px"></span><span class="hseg"></span></div><div class="hchart" style="height:230px"></div><div class="hnote mut3" style="font-size:11.5px;line-height:1.45;margin:4px 0 0"></div>' +
      '<div class="k-sec">Риск по бакетам <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">тыс ₽/бп</span></div><div class="rchart" style="height:200px"></div>' +
      '<div class="k-sec">Риск по кривым и срокам <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">тыс ₽/бп · подпись — как считается</span></div>' + K.curveRiskTable(p) +
      (isBond ? '<div class="k-sec">Потоки</div><div class="cfs"></div>' : '');
    // история: у облигации вся (M.posHistory deep: с 2024 через API истории хаба), видно последний год, колесо и
    // ползунок — глубже; спред фикс-купона — G и Z (MOEX, к КБД), флоатера — DM (по прогонам); у линкера BEI и IR
    const H = M.posHistory(p.id, isBond ? { deep: true } : undefined);
    const isFloat = isBond && p.cpn.type === 'float';
    const isLinker = isBond && (p.kind === 'IN' || !!H.bei);
    let mode = isFloat ? 'spread' : isBond ? 'yield' : 'notional', unit = 'pos';
    const hnote = o.bd.querySelector('.hnote'), hunit = o.bd.querySelector('.hunit');
    const perBondOk = !!(H.qty && H.qty.some(v => v));
    const hc = K.chart(o.bd.querySelector('.hchart'), () => {
      const xs = H.dates, long = xs.length > 300;
      // ряды по прогонам (R44): точка — прогон своей даты; прошлой версией — пунктиром, другим методом — маркером,
      // без прогона или без оценки — разрыв (причина в подсказке)
      const R = mode === 'dv01' || mode === 'notional' || (mode === 'spread' && isFloat && H.dm) ? K.runSeries(H, mode === 'spread' ? 'dm' : mode, { color: K.issuerColor(p.issuer), perBond: unit === 'bond' }) : null;
      hnote.innerHTML = R ? R.note + (isFloat && mode === 'dv01' ? '<div style="margin-top:3px">' + esc(K.tips.floaterDv01) + '</div>' : '') : '';
      hunit.style.display = mode === 'dv01' && perBondOk ? '' : 'none';
      const S = R ? R.series : mode === 'price' ? [{ name: 'Цена', data: H.price }]
        : mode === 'yield' ? [{ name: 'Доходность', data: H.ytm }, { name: 'ОФЗ на сроке', data: H.bench, lineStyle: { type: 'dashed', width: 1 } }]
          : mode === 'spread' ? (isFloat ? [{ name: 'DM, б.п.', data: H.dm || H.spr, connectNulls: true }]
            : [{ name: 'G-спред, б.п.', data: H.spr }].concat(H.zspr ? [{ name: 'Z-спред MOEX, б.п.', data: H.zspr }] : []))
            : mode === 'infl' ? [{ name: 'BEI, %', data: H.bei || [] }, { name: 'IR (реальная), %', data: H.ir || [] }].concat(H.cpi ? [{ name: 'ИПЦ г/г, %', data: H.cpi, step: 'end', lineStyle: { type: 'dashed', width: 1.2 } }] : [])
              : mode === 'dv01' ? [{ name: 'DV01, тыс ₽/бп', data: H.dv01.map(v => v / 1e3), step: 'end' }] : [{ name: 'Номинал, mm', data: H.notional.map(v => v / 1e6), step: 'end', areaStyle: { opacity: .15 } }];
      return { grid: { left: 6, right: 10, top: 26, bottom: long ? 30 : 4, containLabel: true }, legend: { top: 0, left: 0 }, tooltip: R ? R.tooltip : { trigger: 'axis' }, xAxis: { type: 'category', data: xs, axisLabel: { formatter: v => f.dshort(v) } }, yAxis: { type: 'value', scale: true }, series: S.map((s, i) => Object.assign({ type: 'line', showSymbol: false, lineStyle: { width: 1.6 }, color: i ? css('--fg-3') : K.issuerColor(p.issuer) }, s)),
        dataZoom: long ? [{ type: 'inside', startValue: xs.length - 260 }, { type: 'slider', height: 14, bottom: 4, startValue: xs.length - 260, labelFormatter: (i, v) => f.dshort(v) }] : [{ type: 'inside' }] };
    });
    o.charts.push(hc);
    K.seg(o.bd.querySelector('.hseg'), (isFloat ? [['price', 'цена'], ['spread', 'DM']] : isBond ? [['price', 'цена'], ['yield', 'доходность'], ['spread', 'спред']] : []).concat(isLinker && (H.bei || H.cpi) ? [['infl', H.cpi ? 'BEI/IR/ИПЦ' : 'BEI/IR']] : []).concat([['notional', 'позиция'], ['dv01', 'DV01']]), mode, v => { mode = v; hc.redraw(); });
    if (perBondOk) K.seg(hunit, [['pos', 'вся позиция'], ['bond', 'на 1 бумагу']], unit, v => { unit = v; hc.redraw(); });
    // риск по бакетам
    const cids = M.curves.filter(c => p.risk[c.id].some(v => Math.abs(v) > 1)).map(c => c.id);
    const rc = K.chart(o.bd.querySelector('.rchart'), () => ({
      grid: { left: 6, right: 10, top: 26, bottom: 4, containLabel: true }, legend: { top: 0, left: 0 }, tooltip: { trigger: 'axis', valueFormatter: v => f.dv(v) },
      xAxis: { type: 'category', data: M.buckets }, yAxis: { type: 'value', axisLabel: { formatter: v => f.dv(v) } },
      series: cids.map(id => ({ name: M.curve[id].short, type: 'bar', stack: 'a', data: p.risk[id], itemStyle: { color: K.curveColors[id] } })).concat(p.cs01 ? [{ name: 'CS01', type: 'bar', data: p.cs01b, itemStyle: { color: K.curveColors.CS01 } }] : []),
    }));
    o.charts.push(rc);
    if (isBond) {
      K.flows(o.bd.querySelector('.cfs'), p);
    }
    return o;
  };

  // ---------- имя бумаги: алиас по ISIN (батч 1, п. 3, R33) ----------
  // p.name — алиас ?? биржевое имя (p.exchName); алиас хранится на сервере по ISIN (M.aliasApi: PUT
  // /api/v1/instruments/alias, только с компьютера сервера; в демо — в памяти страницы) и подставляется во все позиции
  // бумаги и на все экраны (события 'alias' и 'filter': страницы перерисовываются, поиск ищет по обоим именам)
  K.setAlias = (isin, alias, meta) => {
    alias = alias || null;
    if (M.aliases) { if (alias) M.aliases[isin] = Object.assign({ alias }, meta || {}); else delete M.aliases[isin]; }
    M.positions.forEach(p => {
      if (p.isin !== isin) return;
      p.alias = alias; p.name = alias || p.exchName;
      p.searchText = (alias ? alias.toLowerCase() + ' ' : '') + (p.searchBase != null ? p.searchBase : p.searchText);
    });
    fire('alias'); fire('filter');
  };
  // ячейка «Бумага»: у бумаги с алиасом биржевое имя — в подсказке
  K.nameCell = (r, v) => r && r.alias && r.exchName && r.exchName !== r.alias ? '<span title="' + esc('биржевое имя: ' + r.exchName) + '">' + esc(v == null ? r.name : v) + '</span>' : esc(v == null ? (r && r.name) : v);
  const canAlias = p => !!(p && p.isin && M.aliasApi);
  const aliasView = p => {
    const ro = M.aliasApi && M.aliasApi.editable === false;
    return '<span class="k-nm-v">' + esc(p.name) + '</span>' +
      (p.alias && p.exchName && p.exchName !== p.alias ? ' <span class="k-exch" title="биржевое имя">' + esc(p.exchName) + '</span>' : '') +
      (canAlias(p) ? '<span class="k-alias-tools">' +
        '<button class="k-ico" data-al="edit"' + (ro ? ' disabled title="' + esc('только чтение: ' + M.aliasApi.readOnlyReason) + '"' : ' title="переименовать бумагу (алиас по ISIN — на всех экранах)"') + '>✎</button>' +
        '<button class="k-ico" data-al="hist" title="история имени">⟲</button></span>' : '');
  };
  K.aliasName = p => '<span class="k-nm">' + aliasView(p) + '</span>';
  const WHO = { local: 'этот компьютер', demo: 'демо' };
  const stamp = s => { if (!s) return '—'; const d = new Date(s); if (isNaN(d)) return esc(s); const z = n => String(n).padStart(2, '0'); return z(d.getDate()) + '.' + z(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + z(d.getHours()) + ':' + z(d.getMinutes()); };
  // ответы сервера (английские) — по-русски; незнакомое — как есть
  const aliasWhy = m => [[/control characters/, 'в имени управляющие символы'], [/longer than (\d+)/, 'имя длиннее $1 знаков'],
    [/not an ISIN/, 'ISIN бумаги не принят сервером'], [/invalid request parameters/, 'неверный запрос']].reduce((s, [re, ru]) => re.test(m) ? m.match(re)[0].replace(re, ru) : s, m);
  let aliasPop = null;
  function closeAliasPop() { if (aliasPop) { aliasPop.remove(); aliasPop = null; } }
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && aliasPop) { closeAliasPop(); e.stopImmediatePropagation(); e.preventDefault(); } }, true);
  document.addEventListener('mousedown', e => { if (aliasPop && !aliasPop.contains(e.target) && !(e.target.closest && e.target.closest('[data-al=hist]'))) closeAliasPop(); }, true);
  // история имени: всплывающий список ревизий (новые сверху)
  K.aliasHistory = function (p, anchor) {
    closeAliasPop();
    const pop = aliasPop = h('div', 'k-alias-pop');
    pop.innerHTML = '<div class="hd">История имени <span class="mono mut3">' + esc(p.isin) + '</span></div><div class="bd"><div class="mut3">загрузка…</div></div>';
    document.body.appendChild(pop);
    if (anchor && anchor.getBoundingClientRect) {
      const r = anchor.getBoundingClientRect();
      pop.style.top = Math.round(r.bottom + 4) + 'px';
      pop.style.left = Math.round(Math.max(8, Math.min(r.left, (window.innerWidth || 1200) - 380))) + 'px';
    }
    return M.aliasApi.history(p.isin).then(revs => {
      if (aliasPop !== pop) return;
      const bd = pop.querySelector('.bd');
      if (revs == null) { bd.innerHTML = '<div class="neg">история недоступна</div>'; return; }
      bd.innerHTML = (revs.length ? '<table class="k-tbl k-compact"><tbody>' + revs.map(r => '<tr><td class="mut">' + stamp(r.changed_at) + '</td><td class="mut3">' + esc(WHO[r.changed_by] || r.changed_by || '—') + '</td><td>' +
        (r.alias ? esc(r.alias) : '<span class="mut3">сброшено — биржевое имя</span>') + '</td></tr>').join('') + '</tbody></table>' : '<div class="mut3">имя не менялось</div>') +
        '<div class="mut3 ft">биржевое имя: ' + esc(p.exchName || '—') + '</div>';
    });
  };
  // правка на месте: el — заголовок с K.aliasName(p); onSaved — после сохранения (заголовок уже перерисован)
  K.aliasBind = function (el, p, onSaved) {
    const wrap = el && el.querySelector && el.querySelector('.k-nm');
    if (!wrap || !canAlias(p)) return;
    const view = () => { wrap.innerHTML = aliasView(p); wire(); };
    function wire() {
      const ed = wrap.querySelector('[data-al=edit]'), hs = wrap.querySelector('[data-al=hist]');
      if (ed) ed.onclick = e => { e.stopPropagation(); edit(); };
      if (hs) hs.onclick = e => { e.stopPropagation(); if (aliasPop) closeAliasPop(); else K.aliasHistory(p, hs); };
    }
    function edit() {
      closeAliasPop();
      wrap.innerHTML = '<span class="k-alias-ed"><input class="k-input" maxlength="120" placeholder="' + esc(p.exchName) + '" value="' + esc(p.alias || '') + '">' +
        '<button class="k-btn pri" data-al="save">Сохранить</button>' + (p.alias ? '<button class="k-btn" data-al="clear" title="убрать алиас: снова биржевое имя">Биржевое имя</button>' : '') +
        '<button class="k-btn" data-al="cancel">Отмена</button><span class="k-alias-msg mut3">пусто — биржевое имя · Enter — сохранить · Esc — отмена' + (M.aliasApi.demo ? ' · демо: до перезагрузки' : '') + '</span></span>';
      const inp = wrap.querySelector('input'), msg = wrap.querySelector('.k-alias-msg');
      const busy = b => wrap.querySelectorAll('button[data-al=save],button[data-al=clear],input').forEach(x => x.disabled = b);
      const save = raw => {
        busy(true); msg.className = 'k-alias-msg mut3'; msg.textContent = 'сохранение…';
        return M.aliasApi.put(p.isin, raw).then(r => {
          if (r.status === 200 && r.body) {
            K.setAlias(p.isin, r.body.alias, { changedBy: r.body.changed_by, changedAt: r.body.changed_at });
            view(); if (onSaved) onSaved(r.body);
            return;
          }
          const why = aliasWhy((r.body && r.body.message) || ('ошибка ' + r.status));
          if (r.status === 403) {  // не компьютер сервера: дальше только чтение, имя — как есть
            M.aliasApi.editable = false;
            msg.className = 'k-alias-msg warn'; msg.textContent = 'только чтение: ' + M.aliasApi.readOnlyReason;
            wrap.querySelectorAll('button[data-al=save],button[data-al=clear],input').forEach(x => x.disabled = true);
            return;
          }
          busy(false);
          msg.className = 'k-alias-msg neg'; msg.textContent = (r.status === 422 ? 'не принято: ' : 'не сохранено: ') + why;
          inp.focus();
        });
      };
      inp.onkeydown = e => {
        e.stopPropagation();  // Esc не закрывает карточку, стрелки и Enter не ходят по таблице (C03)
        if (e.key === 'Enter') { e.preventDefault(); save(inp.value); }
        if (e.key === 'Escape') { e.preventDefault(); view(); }
      };
      wrap.querySelector('[data-al=save]').onclick = e => { e.stopPropagation(); save(inp.value); };
      const cl = wrap.querySelector('[data-al=clear]'); if (cl) cl.onclick = e => { e.stopPropagation(); save(null); };
      wrap.querySelector('[data-al=cancel]').onclick = e => { e.stopPropagation(); view(); };
      if (inp.focus) { inp.focus(); if (inp.select) inp.select(); }
    }
    wire();
    return { edit, view };
  };

  // все потоки позиции до погашения: на прогоне — график выпуска с сервера (M.posFlows), иначе потоки набора данных;
  // потоки после оферты, к которой оценена бумага, приглушены: они будут, если бумагу не предъявят
  // K.flows(el, p): сразу — потоки набора данных, затем, когда сервер ответит, — весь график
  K.flows = function (el, p) {
    el.innerHTML = K.flowsTable(p, M.cashflows.filter(c => c.pos === p.id), !!M.posFlows);
    if (M.posFlows) M.posFlows(p.id).then(cf => { if (el.isConnected && el.dataset.pos === p.id) el.innerHTML = K.flowsTable(p, cf || M.cashflows.filter(c => c.pos === p.id)); });
    el.dataset.pos = p.id;
  };
  K.flowsTable = function (p, cf, loading) {
    const offer = p.offer || p.yieldDate || null;
    const KIND = { cpn: 'купон', amort: 'амортизация', mat: 'погашение', put: 'пут-оферта', call: 'колл' };
    if (!cf.length) return '<div class="k-empty">' + (loading ? 'загрузка потоков…' : 'нет потоков') + '</div>';
    const sum = cf.reduce((s, c) => s + (c.kind === 'put' || c.kind === 'call' || c.amountRub == null ? 0 : c.amountRub), 0);
    return '<div class="mut3" style="font-size:11px;margin-bottom:4px">' + (loading ? 'загрузка всех потоков… · ' : '') + cf.length + ' потоков до ' + f.date(cf[cf.length - 1].date) +
      (offer ? ' · после оферты ' + f.date(offer) + ' — бледнее' : '') + ' · оферта — номинал к предъявлению, в сумму не входит</div>' +
      '<div style="max-height:360px;overflow:auto"><table class="k-tbl"><thead><tr><th>Дата</th><th>Тип</th><th class="n">Сумма, ' + p.ccy + '</th><th class="n">≈ mm ₽</th></tr></thead><tbody>' +
      cf.map(c => '<tr' + (offer && c.date > offer ? ' style="opacity:.55"' : '') + '><td>' + f.date(c.date) + '</td><td>' + (c.amort ? 'амортизация' : KIND[c.kind] || c.kind) + (c.est ? ' <span class="mut3">прогноз</span>' : '') + '</td><td class="n">' + f.n(c.amount, 0) + '</td><td class="n">' + f.mn(c.amountRub, 1) + '</td></tr>').join('') +
      '</tbody><tfoot><tr><td></td><td>Итого без оферт</td><td></td><td class="n">' + f.mn(sum, 1) + '</td></tr></tfoot></table></div>';
  };

  // ---------- палитра поиска ----------
  let palEl = null, cmds = [];
  K.commands = list => { cmds = cmds.concat(list); };
  function closePal() { if (palEl) { palEl.remove(); palEl = null; } }
  K.palette = function () {
    if (palEl) return;
    palEl = h('div', 'k-pal'); palEl.innerHTML = '<input placeholder="бумага, ISIN, эмитент, книга, валюта, экран…"><div class="res"></div><div class="ft"><span>↑↓ выбор</span><span>Enter открыть</span><span>Esc закрыть</span></div>';
    document.body.appendChild(palEl);
    const inp = palEl.querySelector('input'), res = palEl.querySelector('.res'); let items = [], sel = 0;
    function run(q) {
      q = q.toLowerCase().trim(); const words = q.split(/\s+/).filter(Boolean);
      const m = s => words.every(w => s.includes(w));
      items = [];
      cmds.forEach(c => { if (!q || m(c.label.toLowerCase())) items.push({ k: 'экран', t: c.label, r: c.hint || '', run: c.run }); });
      M.issuers.forEach(i => { const L = M.positions.filter(p => p.issuer === i.id); if (L.length && q && m((i.name + ' ' + i.short + ' ' + i.sector).toLowerCase())) items.push({ k: 'эмитент', t: i.name, r: L.length + ' поз · DV01 ' + f.dv(M.sum(L, 'dv01')), run: () => K.drill({ title: 'Эмитент ' + i.short, unit: 'mm ₽ MV', fmt: v => f.mn(v, 1), list: L, val: p => p.group === 'DER' ? p.notionalRub : p.mv }) }); });
      M.books.forEach(b => { if (q && m((b.id + ' ' + b.name + ' ' + b.desk).toLowerCase())) items.push({ k: 'книга', t: b.name + ' · ' + b.id, r: 'фильтр шапки', run: () => K.setFilter({ books: [b.id] }) }); });
      M.positions.forEach(p => { if (q && m(p.searchText)) items.push({ k: 'позиция', t: p.name, r: p.ccy + ' · ' + p.book + ' · DV01 ' + f.dv(p.dv01), run: () => K.card(p.id) }); });
      items = items.slice(0, 60); sel = 0; paint();
    }
    function paint() { res.innerHTML = items.map((it, i) => '<div class="it' + (i === sel ? ' sel' : '') + '" data-i="' + i + '"><span class="k">' + it.k + '</span><span class="m">' + esc(it.t) + '</span><span class="r">' + esc(it.r) + '</span></div>').join('') || '<div class="k-empty">ничего</div>'; res.querySelectorAll('.it').forEach(e => e.onclick = () => go(+e.dataset.i)); const s = res.querySelector('.sel'); if (s) s.scrollIntoView({ block: 'nearest' }); }
    function go(i) { const it = items[i]; closePal(); if (it) it.run(); }
    inp.oninput = () => run(inp.value);
    inp.onkeydown = e => { if (e.key === 'ArrowDown') { sel = Math.min(items.length - 1, sel + 1); paint(); e.preventDefault(); } else if (e.key === 'ArrowUp') { sel = Math.max(0, sel - 1); paint(); e.preventDefault(); } else if (e.key === 'Enter') go(sel); };
    setTimeout(() => document.addEventListener('click', function cl(e) { if (palEl && !palEl.contains(e.target)) { closePal(); document.removeEventListener('click', cl); } }), 0);
    run(''); inp.focus();
  };

  // ---------- подсказка ----------
  let tip = null;
  K.tip = (e, html) => { if (!tip) { tip = h('div', 'k-tip'); document.body.appendChild(tip); } if (!html) { tip.style.display = 'none'; return; } tip.innerHTML = html; tip.style.display = 'block'; const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8), y = Math.min(e.clientY + 14, innerHeight - tip.offsetHeight - 8); tip.style.left = x + 'px'; tip.style.top = y + 'px'; };

  // ---------- готовые блоки ----------
  // сводная таблица рисков: строки — валюта → гос/корп/деривативы → тип, колонки — кривые; значения кликабельны.
  // Раскраска (переключатель над таблицей, запоминается): по типу (фон строки и цвет подписи), тепловая, по знаку, без цвета.
  K.RISK_COLOR = [['type', 'по типу'], ['heat', 'тепловая'], ['sign', 'по знаку'], ['none', 'без цвета']];
  K.riskColor = (() => { try { return localStorage.getItem('concepts-riskcolor') || 'type'; } catch (e) { return 'type'; } })();
  const tint = (hex, a) => hex && hex[0] === '#' && hex.length === 7 ? hex + Math.round(a * 255).toString(16).padStart(2, '0') : 'transparent';
  K.riskMatrix = function (el, opt) {
    opt = opt || {};
    const box = h('div', 'k-rm'); const bar = h('div', 'k-rm-bar');
    el.innerHTML = ''; if (opt.controls !== false) el.appendChild(bar); el.appendChild(box);
    if (opt.controls !== false) {
      bar.append(h('span', 'mut', 'цвет:'));
      const sb = h('span'); bar.appendChild(sb);
      K.seg(sb, K.RISK_COLOR, K.riskColor, v => { K.riskColor = v; try { localStorage.setItem('concepts-riskcolor', v); } catch (e) { } fire('riskcolor'); });
      bar.appendChild(h('span', 'k-note', 'тыс ' + K.SYM[K.repr] + '/бп · щелчок по цифре — из чего она'));
    }
    function draw() {
      const mode = K.riskColor, L = K.list();
      const ccys = M.ccys.filter(c => L.some(p => p.ccy === c));
      // колонки — кривые валют строк и любые кривые с риском (нога USD/CNY-кросса на кривой юаня видна и без юаневых позиций)
      const cols = [], used = id => L.some(p => p.dv01ByCurve[id]);
      M.ccys.forEach(c => M.curves.filter(x => x.ccy === c && (ccys.includes(c) || used(x.id))).forEach(x => cols.push(x)));
      M.curves.filter(x => !cols.includes(x) && used(x.id)).forEach(x => cols.push(x));
      const curCols = opt.curves ? cols.filter(c => opt.curves.includes(c.id)) : cols;
      const allMax = Math.max(1, ...M.typeRows.map(t => Math.abs(M.sum(L.filter(p => p.group + '.' + p.kind === t.id), 'dv01'))));
      const cellStyle = v => mode === 'heat' ? K.heatRisk(v, allMax) : mode === 'sign' ? 'color:' + (v < 0 ? 'var(--accent)' : 'var(--warn)') : '';
      const num = (v, spec, bold) => Math.abs(v) < 500 ? '<span class="mut3">·</span>' : (bold ? '<b>' : '') + K.d(spec, null, '') + (bold ? '</b>' : '');
      let html = '<table class="k-tbl k-compact"><thead><tr><th>Валюта / тип</th><th class="n" title="' + esc(K.tips.dv01) + '"><span class="k-hint">DV01</span></th>' +
        curCols.map(c => '<th class="n" title="' + esc(K.curveTip(c.id) || c.name) + '">' + (ccys.length > 1 ? '<span class="mut3">' + c.ccy + '</span> ' : '') + esc(c.short) + '</th>').join('') +
        '<th class="n" title="' + esc(K.tips.cs01) + '"><span class="k-hint">CS01</span></th><th class="n">MV, mm</th></tr></thead><tbody>';
      const row = (label, list, lvl, color, ccy) => {
        const dv = M.sum(list, 'dv01'), cs = M.sum(list, 'cs01'), mv = M.sum(list, p => p.group === 'DER' ? 0 : p.mv);
        const rowBg = mode === 'type' ? (color ? tint(color, 0.10) : ccy ? tint(K.ccyColor(ccy), 0.16) : '') : '';
        const lab = mode === 'type' && color ? '<span style="color:' + color + '">' + label + '</span>' : (color && mode !== 'type' ? '<b style="color:' + color + '">●</b> ' : '') + (mode === 'type' && color ? '' : label);
        return '<tr class="' + (lvl === 0 ? 'grp' : '') + '"' + (rowBg ? ' style="background:' + rowBg + '"' : '') + '><td style="padding-left:' + (6 + lvl * 12) + 'px">' + lab + ' <span class="mut3">' + list.length + '</span></td>' +
          '<td class="n" style="' + cellStyle(dv) + '">' + num(dv, { title: label.replace(/<[^>]+>/g, '') + ' · DV01', unit: 'тыс ₽/бп', list, val: 'dv01' }, true) + '</td>' +
          curCols.map(c => { const v = M.sum(list, p => p.dv01ByCurve[c.id]); return '<td class="n" style="' + cellStyle(v) + '">' + num(v, { title: label.replace(/<[^>]+>/g, '') + ' · ' + c.short, unit: 'тыс ₽/бп', list, val: p => p.dv01ByCurve[c.id] }) + '</td>'; }).join('') +
          '<td class="n" style="' + (mode === 'heat' ? K.heatMono(cs, allMax, '227,179,65') : cellStyle(cs)) + '">' + (cs ? num(cs, { title: label.replace(/<[^>]+>/g, '') + ' · CS01', unit: 'тыс ₽/бп', list, val: 'cs01' }) : '<span class="mut3">·</span>') + '</td>' +
          '<td class="n">' + (mv ? K.d({ title: label.replace(/<[^>]+>/g, '') + ' · MV', unit: 'mm ₽', fmt: v => f.mn(v, 0), list, val: p => p.group === 'DER' ? 0 : p.mv, color: false }) : '<span class="mut3">·</span>') + '</td></tr>';
      };
      ccys.forEach(c => {
        const Lc = L.filter(p => p.ccy === c);
        html += row(K.ccyTag(c) + ' ' + K.ccyName(c), Lc, 0, null, c);
        K.GROUPS.forEach(g => {
          const Lg = Lc.filter(p => p.group === g); if (!Lg.length) return;
          const kinds = M.typeRows.filter(t => t.group === g && Lg.some(p => p.kind === t.kind));
          if (kinds.length > 1) { html += row(M.groupLabel[g], Lg, 1); kinds.forEach(t => html += row(t.label, Lg.filter(p => p.kind === t.kind), 2, K.kindColor[t.kind])); }
          else kinds.forEach(t => html += row(M.groupLabel[g] + ' · ' + t.label, Lg.filter(p => p.kind === t.kind), 1, K.kindColor[t.kind]));
        });
      });
      html += '</tbody><tfoot>' + row('Итого', L, 0).replace('<tr class="grp"', '<tr') + '</tfoot></table>';
      box.innerHTML = html;
    }
    draw(); K.live(el, 'filter', draw); K.live(el, 'theme', draw); K.live(el, 'riskcolor', () => { draw(); const b = bar.querySelector('.k-seg'); if (b) b.querySelectorAll('button').forEach((x, i) => x.classList.toggle('on', K.RISK_COLOR[i][0] === K.riskColor)); });
    return { redraw: draw };
  };

  // таблица «кривая × бакет» для одной валюты: название кривой и итог закреплены слева, справа — бакеты; снизу Σ и кумулятив
  K.bucketTable = function (el, ccy, opt) {
    opt = opt || {};
    function draw() {
      const L = K.list(), mode = K.riskColor;
      const cs = M.curves.filter(c => c.ccy === ccy);
      const rows = cs.map(c => ({ label: c.short, id: c.id, vals: M.buckets.map((_, k) => M.sum(L, p => p.risk[c.id][k])) }));
      if (opt.cs01) rows.push({ label: 'CS01', id: 'CS01', vals: M.buckets.map((_, k) => M.sum(L.filter(p => p.ccy === ccy), p => p.cs01b[k])) });
      const isRate = r => r.id !== 'CS01' && M.curve[r.id].kind === 'rate';
      const tot = M.buckets.map((_, k) => rows.filter(isRate).reduce((s, r) => s + r.vals[k], 0));
      let c = 0; const cum = tot.map(v => c += v);
      const max = Math.max(1, ...rows.flatMap(r => r.vals.map(Math.abs)));
      const st = v => mode === 'heat' ? K.heatRisk(v, max) : mode === 'sign' ? 'color:' + (v < 0 ? 'var(--accent)' : 'var(--warn)') : '';
      const cell = (v, title, rowsFn) => '<td class="n" style="' + st(v) + '">' + (Math.abs(v) < 500 ? '<span class="mut3">·</span>' : K.d({ title, unit: 'тыс ₽/бп', rows: rowsFn() }, null, '')) + '</td>';
      const rowsOf = (r, k) => L.map(p => ({ p, v: r.id === 'CS01' ? (p.ccy === ccy ? (k == null ? p.cs01 : p.cs01b[k]) : 0) : (k == null ? p.dv01ByCurve[r.id] : p.risk[r.id][k]) }));
      const rateRows = (k, cumul) => L.map(p => ({ p, v: cs.filter(x => x.kind === 'rate').reduce((s, x) => s + (cumul ? p.risk[x.id].slice(0, k + 1).reduce((a, b) => a + b, 0) : p.risk[x.id][k]), 0) }));
      el.innerHTML = '<div class="k-tbl-wrap"><table class="k-tbl k-compact k-pin2"><thead><tr><th>' + K.ccyTag(ccy) + ' кривая</th><th class="n">Итого</th>' + M.buckets.map(b => '<th class="n">' + b + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(r => {
          const col = K.curveColors[r.id], bg = mode === 'type' ? ' style="background:' + tint(col, 0.10) + '"' : '';
          return '<tr' + bg + '><td><b style="color:' + col + '">■</b> ' + K.hint(mode === 'type' ? '<span style="color:' + col + '">' + esc(r.label) + '</span>' : esc(r.label), K.curveTip(r.id)) + (r.id !== 'CS01' && M.curve[r.id].kind === 'basis' ? ' <span class="mut3">базис</span>' : '') + '</td>' +
            '<td class="n"><b>' + K.d({ title: r.label + ' · всего', unit: 'тыс ₽/бп', rows: rowsOf(r) }, null, '') + '</b></td>' +
            r.vals.map((v, k) => cell(v, r.label + ' · ' + M.buckets[k], () => rowsOf(r, k))).join('') + '</tr>';
        }).join('') +
        '</tbody><tfoot><tr><td>Σ ставки</td><td class="n">' + f.dv(tot.reduce((a, b) => a + b, 0)) + '</td>' + tot.map((v, k) => '<td class="n">' + K.d({ title: ccy + ' ставки · ' + M.buckets[k], unit: 'тыс ₽/бп', rows: rateRows(k) }, null, '') + '</td>').join('') + '</tr>' +
        '<tr><td class="mut">кумулятив</td><td></td>' + cum.map((v, k) => '<td class="n mut">' + K.d({ title: ccy + ' кумулятив до ' + M.buckets[k], unit: 'тыс ₽/бп', rows: rateRows(k, true) }, null, '') + '</td>').join('') + '</tr></tfoot></table></div>';
    }
    draw(); K.live(el, 'filter', draw); K.live(el, 'theme', draw); K.live(el, 'riskcolor', draw);
    return { redraw: draw };
  };

  // PnL-водопад: кэрри → ставки → спред → FX → сделки → прочее → итог; столбцы кликабельны
  K.pnlWaterfall = function (el, opt) {
    const keys = [['carry', 'Кэрри'], ['rates', 'Ставки'], ['spread', 'Спред'], ['fx', 'FX'], ['trading', 'Сделки'], ['other', 'Прочее']];
    let L;
    const c = K.chart(el, () => {
      L = K.list();
      const vals = keys.map(([k]) => M.sum(L, p => p.pnl[k]));
      // отрезки [от, до]: стек ECharts раскладывает отрицательные значения отдельно, поэтому рисуем прямоугольники сами
      let run = 0; const segs = vals.map(v => { const s = [run, run + v]; run += v; return s; });
      segs.push([0, run]);
      const cols = vals.map(v => v >= 0 ? css('--pos') : css('--neg')).concat([css('--accent')]);
      const lo = Math.min(0, ...segs.map(x => Math.min(x[0], x[1]))), hi = Math.max(0, ...segs.map(x => Math.max(x[0], x[1])));
      return {
        grid: { left: 6, right: 10, top: 18, bottom: 4, containLabel: true }, tooltip: { trigger: 'item', formatter: p => (keys[p.dataIndex] ? keys[p.dataIndex][1] : 'Итого') + ': <b>' + f.mns(p.dataIndex < vals.length ? vals[p.dataIndex] : run, 2) + '</b> mm' },
        xAxis: { type: 'category', data: keys.map(k => k[1]).concat(['Итого']) }, yAxis: { type: 'value', min: lo, max: hi, scale: false, axisLabel: { formatter: v => f.mn(v, 0) } },
        series: [{
          type: 'custom', data: segs.map((s, i) => [i, s[0], s[1]]),
          renderItem: (params, api) => {
            const i = api.value(0), a = api.coord([i, api.value(1)]), b = api.coord([i, api.value(2)]), w = Math.min(48, api.size([1, 0])[0] * 0.6);
            const y0 = Math.min(a[1], b[1]), hh = Math.max(1, Math.abs(a[1] - b[1]));
            const v = i < vals.length ? vals[i] : run;
            return { type: 'group', children: [
              { type: 'rect', shape: { x: a[0] - w / 2, y: y0, width: w, height: hh }, style: { fill: cols[i] } },
              { type: 'text', style: { x: a[0], y: y0 - 4, text: f.mns(v, 1), fill: css('--fg-2'), fontSize: 11, align: 'center', verticalAlign: 'bottom' } }] };
          },
        }],
      };
    });
    c.on('click', pp => { const i = pp.dataIndex; if (i < keys.length) K.drill({ title: 'PnL · ' + keys[i][1], unit: 'тыс ₽', fmt: f.k, list: L, val: p => p.pnl[keys[i][0]] }); else K.drill({ title: 'PnL дня', unit: 'тыс ₽', fmt: f.k, list: L, val: p => p.pnl.total }); });
    K.live(el, 'filter', () => c.redraw());
    return c;
  };

  // KPI-плитка; spec — для разбора по щелчку
  K.kpi = (label, valueHtml, sub, spark, tip) => '<div class="k-kpi"><div class="l">' + K.hint(label, tip) + '</div><div class="v">' + valueHtml + '</div><div class="s">' + (sub || '&nbsp;') + '</div>' + (spark || '') + '</div>';

  // стандартная строка HUD: риск по валютам и кривым, PnL, кэрри, объём
  K.hud = function (el) {
    function draw() {
      const L = K.list(), k = M.kpis(L);
      const pnlH = M.hist.pnl.total.slice(-30);
      el.innerHTML = '<div class="k-kpis">' +
        K.kpi('PnL дня', K.d({ title: 'PnL дня', unit: 'mm ₽', fmt: v => f.mns(v, 2), list: L, val: p => p.pnl.total }, f.mns(k.pnl, 1) + ' mm'), 'MTD ' + K.d({ title: 'PnL MTD', unit: 'mm ₽', fmt: v => f.mns(v, 1), list: L, val: 'pnlMtd' }) + K.partial('mtd') + ' · YTD ' + K.d({ title: 'PnL YTD', unit: 'mm ₽', fmt: v => f.mns(v, 0), list: L, val: 'pnlYtd' }) + K.partial('ytd'), K.spark(pnlH, 70, 20)) +
        K.kpi('DV01 ставки', K.d({ title: 'DV01 ставки (все кривые)', unit: 'тыс ₽/бп', list: L, val: 'dv01' }) + ' <span class="mut" style="font-size:11px">тыс/бп</span>', M.ccys.map(c => c + ' ' + K.d({ title: 'DV01 ' + c + ' (кривые ' + c + ')', unit: 'тыс ₽/бп', list: L, val: p => INSTR.dv01In(p, c) }, null, '')).join(' · '), null, K.tips.dv01) +
        K.kpi('RUONIA · КС', K.d({ title: 'DV01 RUONIA', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB RUONIA'] }) + ' · ' + K.d({ title: 'DV01 КС', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB KEY'] }), K.hint('базис ОФЗ', K.tips.basis01) + ' ' + K.d({ title: 'Базис ОФЗ−RUONIA', unit: 'тыс ₽/бп', list: L, val: 'basis01' }, null, ''), null, K.tips.ruonia + '\n\n' + K.tips.basis01) +
        K.kpi('CS01', K.d({ title: 'CS01', unit: 'тыс ₽/бп', list: L, val: 'cs01' }), 'корп. RUB ' + K.d({ title: 'CS01 корп RUB', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy === 'RUB'), val: 'cs01' }, null, '') + ' · вал. ' + K.d({ title: 'CS01 валютные', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy !== 'RUB'), val: 'cs01' }, null, ''), null, K.tips.cs01) +
        K.kpi('Кэрри / день', K.d({ title: 'Кэрри за день', unit: 'тыс ₽', fmt: f.k, list: L, val: 'carryDay' }) + ' <span class="mut" style="font-size:11px">тыс</span>', 'в год ≈ ' + f.mn(k.carryDay * 365, 0) + ' mm') +
        K.kpi('Облигации MV', K.d({ title: 'MV облигаций', unit: 'bn ₽', fmt: v => f.bn(v, 1), list: L.filter(p => p.group !== 'DER'), val: 'mv', color: false }) + ' <span class="mut" style="font-size:11px">bn</span>', M.ccys.map(c => c + ' ' + f.bn(k.byCcy[c].mv, 1)).join(' · ')) +
        K.kpi('ОВП', K.fxCcys(L).map(c => c + ' ' + K.d({ title: 'FX дельта ' + c, unit: 'mm ' + c, fmt: v => f.mn(v, 1), list: L, val: p => INSTR.fxIn(p, c) })).join(' '), 'mm в валюте') +
        '</div>';
    }
    draw(); K.live(el, 'filter', draw);
    return { redraw: draw };
  };

  // стандартные команды поиска
  K.commands([
    { label: 'Сбросить фильтр книг', run: () => K.setFilter({ entity: null, desk: null, books: null, ccy: null }) },
    { label: 'Переключить тему', run: () => K.toggleTheme() },
    { label: 'Все мониторы', run: () => location.href = 'index.html' },
  ]);
})();
