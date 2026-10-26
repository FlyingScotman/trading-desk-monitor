/*
 * KIT — общие компоненты концептов (нужны mock.js и ../echarts.min.js).
 *
 *  KIT.f.*                — форматирование (тыс ₽/бп, mm, bn, б.п., даты)
 *  KIT.negFmt, KIT.setNegFmt(v) — отрицательные числа: 'acc' — (1,234.5) красным (по умолчанию), 'minus' — −1,234.5
 *  KIT.top({title, tag})  — шапка: дата и тип данных, «следующее обновление» и предупреждение без среза дня (#22),
 *                           фильтр книг и портфелей (общий для всех мониторов), поиск, тема
 *  KIT.dataLabel(run), KIT.headerStatus(status, now) — тип данных прогона и состояние шапки по статусу автопилота
 *  KIT.runAgo(ms), KIT.runBadge(info, now), KIT.runCoverage() — бейдж прогона в шапке (#192): сколько прошло с публикации
 *                           (тикает сам; новый прогон — по ленте событий lib/live.js) и что вошло в прогон;
 *                           KIT.runAge(), KIT.setRunAge(patch) — пороги «⚠ устарел» (#255), меню вида ⚙ в шапке
 *  KIT.list()             — позиции после фильтра шапки; KIT.onFilter(fn) — перерисовка при смене фильтра
 *  KIT.d(spec)            — кликабельная цифра: spec = {title, list|rows, val, fmt, unit, v?}; щелчок раскрывает вклад
 *  KIT.drill(spec)        — открыть разбор вклада (позиции, группировка по эмитенту/книге/валюте/типу/бакету)
 *  KIT.card(id)           — карточка позиции: параметры, графики истории, риск по бакетам, потоки, PnL
 *  KIT.table(el, opts)    — таблица: группы, мультисортировка (Shift+щелчок; группы сортируются тем же столбцом), поиск, итоги с разбором;
 *                           ширина и порядок колонок мышкой с запоминанием (KIT.viewStore), «⤓ CSV» — выгрузка
 *  KIT.tableTools(el, opts) — то же для таблиц, свёрстанных вручную: «⤓ CSV» и ширина колонок с запоминанием (#131)
 *  KIT.tips, KIT.hint     — подсказки к мерам риска (конвенция DV01 RUONIA, Базис01, CS01, Z к RUONIA) и подпись с подсказкой
 *  KIT.chart(el, option)  — echarts с цветами темы; KIT.riskStack(el, opts) — риск по бакетам: стек кривых + итог + кумулятив
 *  KIT.seg(el, items, cur, fn) — переключатель режимов (таблица / график и т. п.)
 *  KIT.palette()          — поиск по всему (Ctrl+K или «/»): позиции, эмитенты, книги, команды страницы
 *  KIT.aliasName(p), KIT.aliasBind(el, p, onSaved) — имя бумаги в карточке: алиас и серое биржевое имя, ✎ — правка на
 *                           месте, ⟲ — история; KIT.setAlias(isin, alias) — подставить алиас во все позиции бумаги (R33)
 *  KIT.ratesCorePanel(el), KIT.ratesCoreModel(list) — свопы и РЕПО, оценённые ядром (#201): фиксинги RUONIA / КС, ядро и
 *                           отчёт, риск по кривым RUONIA и КС, «предварительно» (quality assumed); нет данных — словами
 *  KIT.fundingLine(p), KIT.fundingPanel(el) — фондирование (B1): строка карточки позиции и панель кокпита по книгам,
 *                           юрлицам и проблемам; «не настроено» — словами, не 0
 *  KIT.PNL_FACTORS, KIT.PNL_COLORS — факторы PnL в порядке показа: кэрри (купон/доход и roll-down, без фондирования),
 *                           фондирование (отдельно, #168), ставки, спред, FX, сделки, прочее; сумма факторов — итог
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
    const arr = a => a && a.forEach((v, i) => a[i] = v == null ? v : v * k);  // null (не посчитано) остаётся null
    M.positions.forEach(p => {
      sc(p, ['mv', 'accrued', 'carryDay', 'rollDay', 'fundingDay', 'dv01', 'basis01', 'ofzSpr01', 'cs01', 'notionalRub', 'pnlMtd', 'pnlYtd', 'funding']);
      arr(p.cs01b); arr(p.ofzSprb); Object.keys(p.risk).forEach(c => arr(p.risk[c])); Object.keys(p.dv01ByCurve).forEach(c => p.dv01ByCurve[c] *= k);
      Object.keys(p.pnl).forEach(x => { if (typeof p.pnl[x] === 'number') p.pnl[x] *= k; });  // null (нет PnL) остаётся null
    });
    const H = M.hist; [H.dv01, H.dv01Book, H.mvType, H.mvCcy, H.pnl].forEach(g => Object.keys(g).forEach(x => arr(g[x]))); arr(H.cs01); arr(H.ofzSpr01);
    M.cashflows.forEach(c => c.amountRub *= k);
    M.universeRub.forEach(b => b.size *= k);
    if (M.funding) {  // фондирование прогона: итоги, книги (и накопленное с последнего списания), юрлица
      const F = M.funding; sc(F, ['total', 'inPositions', 'bookLevel']);
      Object.values(F.byBook || {}).forEach(b => { sc(b, ['total', 'inPositions', 'bookLevel']); if (b.writeOff) sc(b.writeOff, ['since_write_off']); });
      Object.keys(F.byEntity || {}).forEach(e => { F.byEntity[e] *= k; });
    }
  })(1 / M.fx[K.repr]);
  // подпись единицы в валюте отчёта: «тыс ₽/бп» → «тыс $/бп» (для canvas графиков и title; в разметке «₽» и так
  // подменяется ниже)
  K.u = s => s == null ? '' : String(s).replace(/₽/g, K.SYM[K.repr]);
  // что меняет валюта отчёта (#105): показывается в подсказке и в меню переключателя. Соглашение подписей: символ
  // валюты — валюта отчёта, сумма пересчитана; код (RUB, USD…) — своя валюта, не пересчитывается
  K.REPR_NOTE = [
    ['пересчитываются по споту', 'MV, DV01, CS01, базис и спред к КБД, PnL (день, MTD, YTD), кэрри, потоки, фондирование, ≈ ОВП, объёмы и выплаты рублёвого рынка'],
    ['в своей валюте', 'номинал бумаги (mm RUB, mm USD…), ОВП «mm в вал.», оборот рынка (mm RUB), цены, доходности и спреды (%, б.п.), курсы'],
    ['подписи', 'символ (' + K.SYM[K.repr] + ') — валюта отчёта; код валюты (RUB, USD…) — своя валюта суммы'],
  ];
  K.setRepr = c => { try { localStorage.setItem('concepts-repr', c); } catch (e) { } location.reload(); };
  // вид риска ОФЗ (#32; lib/real.js: M.ofzView): «RUONIA + базис» — по умолчанию, «КБД» — ОФЗ на своей кривой
  K.OFZ_VIEWS = { basis: 'RUONIA + базис', kbd: 'КБД' };
  // базис ОФЗ − RUONIA как рыночная кривая (уровни и изменения в б.п.) — при любом виде риска; в демо — кривая страниц
  K.basisCurve = () => M.basisCurve || M.curve['RUB OFZ'];
  K.setOfzView = v => { try { localStorage.setItem('concepts-ofz-view', v); } catch (e) { } location.reload(); };
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
  // отрицательные (#153) — настройка интерфейса K.negFmt: 'acc' — бухгалтерский формат (1,234.5), красным (по
  // умолчанию), 'minus' — −1,234.5. Хранится в K.viewStore (ключ 'ui:numfmt'), меняется кнопкой в шапке без
  // перезагрузки (K.setNegFmt). CSV — всегда с обычным минусом (K.withNegFmt). Ноль, в том числе отрицательное число,
  // округлённое до нуля, — без знака и без скобок; null / NaN — «—». suf — единица, которая в скобках стоит внутри:
  // (4.44%), (1.2 bn)
  K.NEG_FMTS = [['acc', '(1,234.5)'], ['minus', '−1,234.5']];
  K.negFmt = 'acc';
  const grp = (v, d, suf) => {
    if (v == null || !isFinite(v)) return '—';
    const s = Math.abs(v).toFixed(d || 0).split('.'); s[0] = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
    const t = s.join('.') + (suf || '');
    return v < 0 && /[1-9]/.test(s.join('')) ? (K.negFmt === 'minus' ? '−' + t : '(' + t + ')') : t;
  };
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
    pct: (v, d) => v == null ? '—' : grp(v, d == null ? 2 : d, '%'),
    pcts: (v, d) => v == null ? '—' : (v > 0 ? '+' : '') + grp(v, d == null ? 2 : d, '%'),
    px: v => v == null ? '—' : grp(v, 3),
    money: v => { if (v == null || !isFinite(v)) return '—'; const a = Math.abs(v); return a >= 1e9 ? grp(v / 1e9, 2, ' bn') : a >= 1e6 ? grp(v / 1e6, 1, ' mm') : a >= 1e3 ? grp(v / 1e3, 0, 'k') : grp(v, 0); },
    moneys: v => (v > 0 ? '+' : '') + f.money(v),
    dv: v => grp(v / 1e3, KD),                    // DV01 в тыс ₽/бп
    dvs: v => sgn(v / 1e3, KD),
    date: s => s ? s.slice(8, 10) + '.' + s.slice(5, 7) + '.' + s.slice(2, 4) : '—',
    dshort: s => { if (!s) return '—'; const m = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']; return (+s.slice(8, 10)) + ' ' + m[+s.slice(5, 7) - 1]; },
    yrs: t => t == null ? '—' : t < 1 ? Math.round(t * 12) + 'м' : grp(t, 1) + 'г',
    cls: v => v > 0 ? 'pos' : v < 0 ? 'neg' : '',
  };
  // деление оси графика (#218): v / scale с тем числом знаков после точки, которое есть у самого деления (0.2 → «0.2»,
  // 1 → «1»), но не больше, чем нужно по величине (от 10 — целые, от 1 — один, от 0.1 — два, меньше — три): в валюте отчёта,
  // отличной от рубля, мелкие деления не превращаются в ряд «0», а крайние деления (min/max оси) не тянут хвост знаков
  K.tick = (v, scale) => {
    const x = v / (scale || 1), a = Math.abs(x), cap = a >= 10 ? 0 : a >= 1 ? 1 : a >= 0.1 ? 2 : 3;
    return grp(x, Math.min(cap, (String(+x.toFixed(6)).split('.')[1] || '').length));
  };
  // заседание ЦБ одной фразой: консенсус и ожидание кривой, если они есть в данных, иначе ставка и прошлое решение
  K.cbrLine = () => {
    const c = M.cbr || {}, n = c.next || {}, last = (c.past || [])[0];
    const known = v => v && v !== '—' && v !== 'нет данных';
    if (known(n.consensus) || known(n.priced)) return ('консенсус ' + K.esc(n.consensus) + ', в кривой ' + K.esc(n.priced)).replace(/\.$/, '') + '.';  // «б.п.» в конце — без второй точки
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
    return 'PnL FX по курсу ' + cs.map(c => c + ' ' + f.n(P.rate[c], c === 'CNY' ? 4 : 2) + ' ' + f.pcts(P.chgPct[c], 2) +
      (P.source && P.source[c] && P.source[c] !== P.label ? ' (' + K.esc(P.source[c]) + ')' : '') +
      (C && C !== P && C.rate[c] != null ? ' · ЦБ ' + f.n(C.rate[c], c === 'CNY' ? 4 : 2) + ' ' + f.pcts(C.chgPct[c], 2) : '')).join(', ') +
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

  // ---------- подсказки к мерам риска (R29, R30, #32, #171): в какой конвенции риск RUONIA, КБД, КС и валютных кривых ----------
  // Так считает монитор на ядре 0.38 (решение владельца 23.10: риск всех кривых — в par-котировках): RUONIA OIS ядру
  // передаётся запросом бутстрэпа (orchestrator: EodContext.quote_curves), бакет сдвигает котировки OIS и кривая пересобирается;
  // КБД — спека rub_ofz_kbd_v3 (par-доходности инструментов с годовым купоном 1Y–20Y+); фикс-ОФЗ дисконтируются по КБД.
  // Валютные кривые — по M.curveUnits прогона (orchestrator.EodContext.curve_units; настройка quote_curve_risk, #171).
  K.tips = {
    ruonia: 'Риск RUONIA — в котировках OIS: +1 б.п. к par-котировкам MOEX OIS RUONIA по 11 бакетам 1M…20Y+ с треугольными весами ' +
      '(на любом сроке сумма весов — 1), кривая пересобирается (log-linear по DF, flat-forward за последним узлом), центральная разность; ' +
      'Z-спред бумаги держится. Кривая КС (проекция флоатеров на КС) — +1 б.п. к нулевой ставке: в котировках IRS KEYRATE ' +
      'ядро 0.38 считает риск свопов и фьючерсов на КС, а не проекцию флоатера, поэтому у флоатера на КС ноги в разных единицах. ' +
      'В виде «RUONIA + базис» в RUONIA входит и риск RUONIA у ОФЗ на КБД (с 0.2.7 — свой, посчитанный ядром, #213): сдвиг котировок ' +
      'OIS RUONIA при неизменном базисе двигает КБД вместе с ней. ' +
      'В день без кривой RUONIA бумаги дисконтируются по КБД. До 0.2.6 бамп RUONIA был по непрерывной нулевой ставке (DV01 на ~14 % больше).',
    basis01: 'Базис01 (ОФЗ − RUONIA), тыс ₽/бп: риск ОФЗ к базису — спреду КБД к RUONIA в годовых нулевых ставках: +1 б.п. ' +
      'к par-доходностям КБД по бакетам 1Y–20Y+ при неизменных котировках OIS RUONIA (1M–6M у КБД слиты в 1Y). Он равен риску ' +
      'ОФЗ на кривой КБД (вид «КБД»). С 0.2.7 (ядро 0.39, #213) риск RUONIA и риск базиса — два разных риска ядра: RUONIA — ' +
      'сдвиг котировок OIS, кривая RUONIA пересобирается и её сдвиг годовой нулевой ставки ложится на КБД, базис держится. ' +
      'За 10Y (последняя котировка OIS) кривая RUONIA продолжена котировками 15Y и 20Y из КБД с базисом 10Y (хвост ZX), ' +
      'поэтому на длинном конце RUONIA ≈ базис. Итоги RUONIA и базиса обычно отличаются на доли процента, по срокам — заметнее. ' +
      'Базис — отдельный фактор, как CS01: к DV01 его не прибавлять (сдвиг RUONIA и базиса вместе — это КБД на 2 б.п.). ' +
      'В прогонах 0.2.6 RUONIA у ОФЗ — тот же риск, что базис (приближение basis_view); так же у ОФЗ на запасной оценке. ' +
      'Собственный спред бумаги к КБД — отдельно, «Спред к КБД01». У ОФЗ-ПК (флоатер на RUONIA) базис — его спред к RUONIA. ' +
      'ОФЗ-ИН в базис не входит: ядро линкеры пока не считает (#38), её DV01 на КБД — номинальный прокси по дюрации, он только ' +
      'в риске ставок (в RUONIA или на КБД, по виду). В прогонах до 0.2.6 базис — +1 б.п. к Z-спреду ОФЗ к RUONIA.',
    ofzSpr01: 'Спред к КБД01, тыс ₽/бп: +1 б.п. к собственному Z-спреду ОФЗ к КБД (годовой, ACT/365F) разом по всем срокам, ' +
      'кривые стоят; по бакетам — пропорционально риску дисконтирования (ядро). Сценарий «спред ОФЗ» двигает его. ' +
      'Есть у ОФЗ на КБД с прогонов 0.2.6; у ОФЗ-ПК весь спред — к RUONIA: в виде «RUONIA + базис» он в базисе, в виде «КБД» — здесь.',
    kbd: 'Кривая ОФЗ (КБД MOEX): риск — +1 б.п. к par-доходности инструмента с годовым купоном на КБД по бакетам 1Y–20Y+ ' +
      '(1M–6M слиты в 1Y: ОФЗ там нет), кривая не перестраивается — локальный оверлей годовых нулевых ставок (ядро 0.37, rub_ofz_kbd_v3). ' +
      'Фикс-ОФЗ (ПД, АД, ИН) дисконтируются по КБД + своему Z-спреду. В виде «RUONIA + базис» он — базис ОФЗ (тот же риск), ' +
      'а риск RUONIA посчитан отдельно (с 0.2.7): поэтому DV01 в двух видах отличается на доли процента. ' +
      'КБД-флоатеры (#33): купоны проецируются по КБД, дисконт — RUONIA + Z. В виде «RUONIA + базис» их базис — риск проекции ' +
      'по КБД при неизменной RUONIA, а риск RUONIA посчитан отдельно: котировки RUONIA двигают и дисконт, и КБД проекции.',
    cs01: 'CS01, тыс ₽/бп: +1 б.п. к кредитному Z-спреду к RUONIA (годовой, ACT/365F) разом по всем срокам, кривая стоит; ' +
      'у фикс-бумаг ≈ DV01 RUONIA (оба — к годовым ставкам).',
    // кэрри и фондирование — разные строки (#168): кэрри без фондирования, фондирование — рядом, своей строкой
    carry: 'Кэрри: купонный / процентный доход позиции и roll-down (скатывание по кривой) за период, без фондирования. ' +
      'Фондирование — отдельная строка «Фондирование»; кэрри за вычетом фондирования = кэрри + фондирование.',
    funding: 'Фондирование: стоимость фондирования позиции за период (тело × ставка фондирования по правилам книги), ' +
      'обычно отрицательная. В кэрри не входит. «—» — не посчитано (не настроено или проблема), а не 0.',
    fundRate: '\nСтавка — на конец периода (последний день), средняя по строкам фондирования с весом тела в рублях; ' +
      'фондирование за день — среднее за период, поэтому «тело × ставка» может с ним не совпасть.',
  };
  // факторы PnL в порядке показа (#168: фондирование — свой фактор после кэрри); сумма факторов позиции — её итог
  K.PNL_FACTORS = [['carry', 'Кэрри'], ['funding', 'Фондирование'], ['rates', 'Ставки'], ['spread', 'Спред'], ['fx', 'FX'], ['trading', 'Сделки'], ['other', 'Прочее']];
  K.PNL_COLORS = { carry: '#3fb67a', funding: '#d9667a', rates: '#4c9dff', spread: '#e3b341', fx: '#b48cff', trading: '#22c3d6', other: '#a0b4c8' };
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
  K.curveTip = id => id === 'CS01' ? K.tips.cs01 : id === 'OFZSPR' ? K.tips.ofzSpr01
    : id === 'RUB OFZ' && M.ofzView === 'kbd' ? K.tips.kbd : id === 'RUB OFZ' || (M.curve[id] && M.curve[id].kind === 'basis') ? K.tips.basis01
    : id === 'RUB RUONIA' || id === 'RUB KEY' ? (M.curve[id] ? M.curve[id].name + '.\n' : '') + K.tips.ruonia
      : (M.curve[id] ? M.curve[id].name + ': ' + (K.UNIT_TIPS[(M.curveUnits || {})[id]] || K.UNIT_TIPS.zero) : '');
  // единица бакета валютной кривой в прогоне (M.curveUnits; прогоны до #171 — нулевые ставки)
  K.UNIT_TIPS = {
    zero: '+1 б.п. к нулевой ставке кривой по бакетам (кривая не пересобирается)',
    par_yield_quote: '+1 б.п. к par-доходностям прокси (котировки хаба) по бакетам, кривая пересобирается — риск в котировках ' +
      '(ядро 0.38, настройка quote_curve_risk). Прокси — не OIS-кривая (proxy_curves_for_risk).',
    xccy_quote_fx: 'у FX-сделок — +1 б.п. к par-котировкам XCCY CNY/RUB по бакетам, кривая пересобирается (ядро 0.38, настройка ' +
      'quote_curve_risk); у облигаций в CNY — +1 б.п. к нулевой ставке построенной кривой: котировки XCCY ядро 0.38 берёт только ' +
      'для FX-сделок, поэтому DV01 облигаций и FX-сделок на этой кривой — в разных единицах.',
  };
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
  K.curveColors = { 'RUB RUONIA': '#4c9dff', 'RUB KEY': '#22c3d6', 'RUB OFZ': '#9b7bff', 'USD SOFR': '#3fb67a', 'EUR ESTR': '#d58cff', 'CNY XCCY': '#f0645a', 'CS01': '#e3b341', 'OFZSPR': '#c98a3a' };
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
  // K.heatRisk и K.heatMono (фон только по величине) — не смысловой цвет (#216): метка K.HEAT_PLAIN в style
  // говорит K.accKeep, что бухгалтерский красный у отрицательного числа на таком фоне остаётся. Чтобы красный читался,
  // фон не плотнее 0.44, а цвет цифры — --neg-heat (kit.css): контраст не ниже 3:1 в обеих темах
  K.HEAT_PLAIN = '--k-heat:plain;';
  K.heatRisk = (v, max) => { if (!v || !max) return ''; const a = Math.min(1, Math.abs(v) / max), al = (0.07 + 0.37 * Math.pow(a, 0.7)).toFixed(3); return K.HEAT_PLAIN + 'background:rgba(' + (v < 0 ? '76,157,255' : '227,160,60') + ',' + al + ')'; };
  K.heatMono = (v, max, rgb) => { if (!v || !max) return ''; const a = Math.min(1, Math.abs(v) / max); return K.HEAT_PLAIN + 'background:rgba(' + (rgb || '76,157,255') + ',' + (0.06 + 0.38 * Math.pow(a, 0.7)).toFixed(3) + ')'; };
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
  // по копии списка: подписка K.live, чей элемент удалён, отписывается во время рассылки — без копии forEach
  // пропустил бы следующего подписчика (страница не перерисовалась бы при второй смене фильтра или формата)
  function fire(ev) { (subs[ev] || []).slice().forEach(fn => { try { fn(); } catch (e) { console.error(e); } }); }
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
    const lbl = filt.books && filt.books.length ? (filt.books.length === 1 ? M.book[filt.books[0]].name : filt.books.length + ' книг') : 'все книги · ' + booksIn().length;
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
  // ---------- #22: дата и тип данных, «следующее обновление», предупреждение без среза дня ----------
  // Время — московское (UTC+3 круглый год). Расписание внутридневного шага автопилота — из GET autopilot/status
  // (schedule; здесь — значения по умолчанию, как в desk.autopilot: срезы с 10:00, предварительное закрытие с 19:45,
  // хаб спрашивается раз в 10 минут в :05, :15, …; без среза дня к 10:15 — предупреждение).
  const MSK_MS = 3 * 3600e3, DOW = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
  const SCHEDULE = { intraday_from: '10:00', prelim_from: '19:45', poll_slot_s: 600, poll_lag_s: 300, warn_after: '10:15' };
  const STALE_MS = 30 * 60e3;  // последний цикл автопилота старше — «автопилот не запускался с …»
  const pad2 = n => String(n).padStart(2, '0'), ddmm = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) : '';
  const hmMin = x => { const p = String(x).split(':'); return +p[0] * 60 + +p[1]; };
  const minHm = v => pad2(Math.floor(v / 60) % 24) + ':' + pad2(Math.floor(v % 60));
  function msk(t) {  // момент → день и минуты дня по МСК
    const d = new Date(+new Date(t) + MSK_MS);
    return { iso: d.toISOString().slice(0, 10), dow: d.getUTCDay(), min: d.getUTCHours() * 60 + d.getUTCMinutes() + d.getUTCSeconds() / 60, hm: pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) };
  }
  const CUT_TYPE = { eod_final: 'окончательное закрытие', eod_preliminary: 'предварительное закрытие', intraday: 'внутридневной срез',
    prev_close: 'закрытие прошлого дня' };
  // тип данных прогона: {asOf, cutType, cutTs | time, marketDate, marketCutType, runType} → «23.10 · внутридневной срез 11:30»;
  // рынок старше позиций (портфель дня на закрытии прошлого дня) — «позиции 23.10 · рынок 22.10 · окончательное закрытие»;
  // утренний прогон (cutType prev_close, #21) называет тип своего закрытия (marketCutType: eod_final | eod_preliminary)
  K.dataLabel = function (r) {
    const time = r.cutType === 'intraday' ? (r.cutTs ? msk(r.cutTs).hm : r.time || '') : '';
    const cut = r.cutType === 'prev_close' && CUT_TYPE[r.marketCutType] ? r.marketCutType : r.cutType;
    const type = (CUT_TYPE[cut] || cut || '') + (time ? ' ' + time : '') + (r.runType === 'TEST' || r.runType === 'REPLAY' ? ' · ' + r.runType : '');
    const date = ddmm(r.asOf), market = r.marketDate && r.asOf && r.marketDate < r.asOf ? ddmm(r.marketDate) : null;
    return { date, market, type, text: (market ? 'позиции ' + date + ' · рынок ' + market : date) + ' · ' + type };
  };
  // причина из шага intraday автопилота (steps[].reason, desk.autopilot.INTRADAY_REASONS) — простыми словами
  K.INTRADAY_REASON = {
    off: 'внутридневной расчёт выключен в настройках', before_open: 'автопилот ещё не проверял срез дня',
    not_business_day: 'автопилот считает день нерабочим', no_positions: 'нет позиций в базе',
    no_cut_yet: 'на хабе ещё нет среза', up_to_date: 'прогон дня посчитан — обновите страницу',
    ran: 'прогон дня только что посчитан — обновите страницу',
    failed_before: 'расчёт упал на этом срезе, повтор — когда изменятся данные', run_failed: 'расчёт упал',
    hub_error: 'хаб не ответил', error: 'ошибка автопилота в шаге «внутри дня»'
  };
  // состояние шапки по статусу автопилота s (null — статус не получен) на момент now:
  // { next: «следующее обновление ≈ 11:35» | «автообновление выключено» | null, warn: null | {text, title}, reason }.
  // opt.lastReason — причина прошлого цикла: пока идёт новый, шага intraday в статусе ещё нет.
  K.headerStatus = function (s, now, opt) {
    opt = opt || {};
    const sc = Object.assign({}, SCHEDULE, (s && s.schedule) || {}), m = msk(now);
    const from = hmMin(sc.intraday_from), slot = sc.poll_slot_s / 60, lag = sc.poll_lag_s / 60;
    const steps = (s && s.steps) || [], ist = steps.find(x => x.name === 'intraday');
    const running = !!(s && s.state === 'running');
    const reason = ist ? ist.reason || null : running ? opt.lastReason || null : null;
    const business = m.dow >= 1 && m.dow <= 5 && reason !== 'not_business_day';
    const lr = s && s.latest_run, ofToday = !!(lr && lr.as_of === m.iso);
    const todayCut = ofToday && (lr.cut_type === 'intraday' || lr.cut_type === 'eod_preliminary');
    const prelimDone = ofToday && lr.cut_type === 'eod_preliminary' && m.min >= hmMin(sc.prelim_from);
    const slotAfter = t => lag + (Math.floor((t - lag) / slot) + 1) * slot;  // начало следующего слота опроса хаба
    let next = null;
    if (s && !s.enabled) next = 'автообновление выключено';
    else if (s && reason === 'off') next = 'внутридневное обновление выключено';
    else if (s) {
      let at = !business || prelimDone ? null : slotAfter(Math.max(m.min, from - 1e-6));
      if (at != null && at < 1440) next = minHm(at);
      else {  // следующий рабочий день (пн–пт; праздники знает только сервер)
        const d = new Date(m.iso + 'T00:00:00Z');
        do d.setUTCDate(d.getUTCDate() + 1); while (d.getUTCDay() === 0 || d.getUTCDay() === 6);
        next = DOW[d.getUTCDay()] + ' ' + ddmm(d.toISOString().slice(0, 10)) + ' ' + minHm(slotAfter(from - 1e-6));
      }
      next = 'следующее обновление ≈ ' + next;
    }
    let warn = null;
    if (business && m.min >= hmMin(sc.warn_after) && !todayCut) {
      let cause;
      const bad = steps.find(x => x.status === 'error'), last = s && s.last_cycle_at;
      if (!s) cause = 'сервер не ответил на запрос статуса автопилота';
      else if (!s.enabled) cause = 'автопилот не работает';
      else if (!running && last && +new Date(now) - +new Date(last) > STALE_MS) { const l = msk(last); cause = 'автопилот не запускался с ' + (l.iso === m.iso ? '' : ddmm(l.iso) + ' ') + l.hm; }
      else if (reason) cause = K.INTRADAY_REASON[reason] || reason;
      else if (ist) cause = ist.detail || 'шаг «внутри дня» не назвал причину';
      else if (bad) cause = bad.name === 'store' ? 'нет связи с базой' : bad.name === 'settings' ? 'ошибка в файле настроек' : 'цикл автопилота упал (' + bad.name + ')';
      else if (steps.some(x => x.name === 'cycle')) cause = 'цикл занят другим процессом';
      else if (running) cause = 'идёт цикл автопилота';
      else cause = steps.length ? 'хаб не настроен (Настройки)' : 'автопилот ещё не закончил ни одного цикла';
      warn = { text: 'нет внутридневного среза за ' + ddmm(m.iso) + ': ' + cause,
        title: 'Рабочий день, после ' + sc.warn_after + ' МСК, а прогона на срезе дня нет. Причина — из шага «внутри дня» автопилота' +
          (ist && ist.detail ? ': ' + ist.detail : '') + (lr ? '. Последний прогон: ' + (lr.label || lr.as_of) : '') + '. Подробности — Настройки → «Автопилот».' };
    }
    return { next, warn, reason };
  };
  function runLabel() {
    return K.dataLabel({ asOf: M.asOf, cutType: M.cut.type, cutTs: M.cut.ts, time: M.cut.time, marketDate: M.run && M.run.marketDate, marketCutType: M.run && M.run.marketCutType, runType: M.run && M.run.runType });
  }
  function srcChip() {
    const s = K.source, L = runLabel();
    const html = L.market ? 'позиции <b>' + esc(L.date) + '</b> · рынок <b>' + esc(L.market) + '</b> · ' + esc(L.type) : '<b>' + esc(L.date) + '</b> · ' + esc(L.type);
    if (s === 'real') return '<span class="k-chip asof" data-a="src" title="Прогон из базы: ' + esc(M.cut.label) + (M.run.pnlBasis ? '; ' + esc(M.run.pnlBasis) : '') + '. Щелчок — выбрать другой">' + html + ' · #' + esc(M.run && M.run.id) + ' ▾</span>';
    const t = s === 'empty' ? 'прогонов нет — тестовые данные' : s === 'demo' ? 'тестовые данные' : 'демо (файл)';
    return '<span class="k-chip" data-a="src" style="border-color:var(--warn);color:var(--warn)" title="Вымышленный набор lib/mock.js">' + t + (s === 'file' ? '' : ' ▾') + '</span>' +
      '<span class="asof">' + html + '</span>';
  }
  // «следующее обновление» и предупреждение без среза дня: статус автопилота раз в минуту (прогон из базы)
  function freshness(bar) {
    const nx = h('span', 'k-fresh'), wr = h('a', 'k-chip k-warnchip');
    nx.style.display = wr.style.display = 'none'; wr.href = '../settings.html';
    bar.querySelector('[data-a=search]').before(wr, nx);
    let lastReason = null;
    const paint = (s, now) => {
      const st = K.headerStatus(s, now, { lastReason });
      if (st.reason) lastReason = st.reason;
      nx.style.display = st.next ? '' : 'none'; nx.textContent = st.next || '';
      nx.title = 'Когда автопилот спросит хаб о новом срезе (МСК); прогон появится через минуту-две после этого';
      wr.style.display = st.warn ? '' : 'none';
      if (st.warn) { wr.textContent = '⚠ ' + st.warn.text; wr.title = st.warn.title; }
      return st;
    };
    let timer = null;
    const poll = () => { clearTimeout(timer); return fetch('../api/v1/autopilot/status', { cache: 'no-store' }).then(r => r.ok ? r.json() : null)
      .then(j => { const st = paint(j, j && j.now ? new Date(j.now) : new Date()); runBadgeStatus(j, st.reason); }, () => paint(null, new Date()))
      .catch(() => { }).then(() => { timer = setTimeout(poll, 60e3); }); };
    runState.refresh = poll;  // #192: новый прогон по ленте — сверить сразу
    poll();
  }
  // ---------- #192: бейдж прогона — сколько прошло с публикации и что в него вошло ----------
  // Публикация — source.publishedAt прогона (window.CONCEPTS_RUN, api/concepts_run.py: run.completed_at); в демо — момент загрузки
  // страницы минус 12 минут. Возраст перерисовывается раз в 30 с и при возврате на вкладку; часы — сервера
  // (сдвиг по now статуса автопилота). Новый прогон: событие 'desk:live' от lib/live.js (run.published), затем
  // сверка с latest_run статуса автопилота (R11: какой прогон «последний»).
  // Подсветка: «есть прогон новее» (latest_run — не прогон на экране) или «устарел» (#255): в рабочем окне (пн–пт,
  // кроме дней, которые автопилот назвал нерабочими — причина not_business_day шага intraday, как в K.headerStatus;
  // fresh_from–fresh_to МСК) с более позднего из двух моментов — публикации прогона и fresh_from сегодня — прошло
  // больше fresh_min минут (вчерашний прогон утром подсвечивается с 11:45, если сегодняшнего ещё нет — как автопилот,
  // #156); в любое время — прогон старше max_days календарных дней. fresh_min и max_days — настройка в меню вида
  // (K.runAge, K.setRunAge; 0 — правило выключено). Закреплённый прогон (?run_id= / ?as_of=) и демо не
  // подсвечиваются: их выбрали сами; «есть новее» для закреплённого — только в подсказке и панели.
  K.RUN_AGE = { fresh_min: 90, fresh_from: '10:15', fresh_to: '20:00', max_days: 3 };
  // настройка порогов — в слое вида (K.viewStore, ключ 'ui:runage', {fresh_min, max_days}); целое 0…предел, иначе — умолчание
  const RUNAGE_KEY = 'ui:runage', RUNAGE_MAX = { fresh_min: 1440, max_days: 365 };
  const runAgeOk = (k, v) => typeof v === 'number' && Number.isInteger(v) && v >= 0 && v <= RUNAGE_MAX[k];
  K.runAge = function () {
    const a = Object.assign({}, K.RUN_AGE), s = K.viewStore ? K.viewStore.get(RUNAGE_KEY) : null;
    if (s) Object.keys(RUNAGE_MAX).forEach(k => { if (runAgeOk(k, s[k])) a[k] = s[k]; });
    return a;
  };
  // patch — {fresh_min?, max_days?}: числа или строки поля ввода; неверное значение не сохраняется → false.
  // Меняется только переданный порог; хранится только то, что отличается от умолчания K.RUN_AGE (сменим умолчание —
  // оно дойдёт и до тех, кто открывал меню, но этот порог не трогал)
  K.setRunAge = function (patch) {
    const s = (K.viewStore.get(RUNAGE_KEY)) || {}, v = {};
    Object.keys(RUNAGE_MAX).forEach(k => { if (runAgeOk(k, s[k]) && s[k] !== K.RUN_AGE[k]) v[k] = s[k]; });
    for (const k of Object.keys(patch || {})) {
      if (!(k in RUNAGE_MAX)) continue;
      const x = typeof patch[k] === 'string' && patch[k].trim() !== '' ? Number(patch[k]) : patch[k];
      if (!runAgeOk(k, x)) return false;
      if (x === K.RUN_AGE[k]) delete v[k]; else v[k] = x;
    }
    if (Object.keys(v).length) K.viewStore.set(RUNAGE_KEY, v); else K.viewStore.reset(RUNAGE_KEY);
    fire('runage');
    return true;
  };
  K.runAgo = function (ms) {
    const m = Math.floor(Math.max(0, ms) / 60e3);
    if (m < 1) return 'только что';
    if (m < 60) return m + ' мин';
    const hh = Math.floor(m / 60);
    if (hh < 6) return hh + ' ч' + (m % 60 ? ' ' + (m % 60) + ' мин' : '');
    if (hh < 48) return hh + ' ч';
    return Math.floor(hh / 24) + ' дн.';
  };
  // info: { publishedAt, demo, pinned, newer: {run_id, text} | null, reason: причина шага intraday автопилота };
  // now — момент (по часам сервера) → { short, text (фраза целиком), level: 'ok' | 'stale' | 'newer' | 'unknown', why }
  K.runBadge = function (info, now) {
    const a = K.runAge(), t = info.publishedAt ? +new Date(info.publishedAt) : NaN;
    if (!isFinite(t)) return { short: '—', text: 'Время публикации прогона неизвестно', level: 'unknown', why: null };
    const ms = +new Date(now) - t, short = ms < 60e3 ? 'только что' : K.runAgo(ms) + ' назад';
    const r = { short, text: 'Прогон опубликован ' + short, level: 'ok', why: null };
    if (info.newer && !info.demo) {
      r.level = info.pinned ? 'ok' : 'newer';
      r.why = 'есть прогон новее: ' + (info.newer.text || '#' + info.newer.run_id) + (info.pinned ? ' (этот выбран вручную)' : '');
      return r;
    }
    if (info.pinned || info.demo) return r;
    const m = msk(now), day = m.dow >= 1 && m.dow <= 5 && info.reason !== 'not_business_day' &&
      m.min >= hmMin(a.fresh_from) && m.min < hmMin(a.fresh_to);
    // начало рабочего окна сегодня (fresh_from МСК) — момент; отсчёт от него, если прогон опубликован раньше
    const from = +new Date(m.iso + 'T' + minHm(hmMin(a.fresh_from)) + ':00Z') - MSK_MS, since = +new Date(now) - Math.max(t, from);
    // календарные дни по МСК: опубликован 22.10 в любое время — 25.10 ему 3 дня, «старше 3» — с 26.10
    const days = Math.round((Date.parse(m.iso) - Date.parse(msk(t).iso)) / 864e5);
    if (a.max_days > 0 && days > a.max_days) { r.level = 'stale'; r.why = 'прогон старше ' + a.max_days + ' дн.'; }
    else if (day && a.fresh_min > 0 && since > a.fresh_min * 60e3) {
      r.level = 'stale';
      r.why = t >= from ? 'в рабочие часы прогон старше ' + a.fresh_min + ' мин: новых срезов не публиковалось'
        : 'с ' + a.fresh_from + ' МСК прошло больше ' + a.fresh_min + ' мин, а прогона после ' + a.fresh_from + ' нет';
    }
    return r;
  };
  // что вошло в прогон — из данных прогона на странице (window.MOCK): позиции и их оценка, книги, PnL, фондирование,
  // рынок, замечания; → { short: «48/50 поз.», rows: [{k, v, warn}] }
  K.runCoverage = function () {
    const ps = M.positions || [], nv = ps.filter(p => p.notValued).length, val = ps.length - nv;
    const pnl = ps.filter(p => !p.notValued && p.pnl && p.pnl.available !== false).length;
    const books = new Set(ps.map(p => p.book).filter(Boolean)).size || (M.books || []).length;
    const rows = [];
    rows.push({ k: 'Позиции', v: val + ' из ' + ps.length + ' оценены' + (nv ? ', ' + nv + ' без оценки' : ''), warn: !!nv });
    rows.push({ k: 'Книги', v: String(books) });
    rows.push({ k: 'PnL', v: pnl ? 'у ' + pnl + ' из ' + val + ' оценённых' : 'нет', warn: pnl < val });
    if (M.run && M.run.pnlBasis) rows.push({ k: 'База PnL', v: M.run.pnlBasis });
    if (M.funding) rows.push({ k: 'Фондирование', v: M.funding.state === 'computed' ? 'посчитано' + (M.funding.problems && M.funding.problems.length ? ', проблем ' + M.funding.problems.length : '') : (M.funding.text || M.funding.state), warn: M.funding.state !== 'computed' && M.funding.state !== 'demo' });
    rows.push({ k: 'Рынок', v: runLabel().text });
    if (M.notes && M.notes.length) rows.push({ k: 'Чего нет', v: M.notes.length + ' замеч. (ⓘ в шапке)', warn: true });
    return { short: val + '/' + ps.length + ' поз.', rows };
  };
  const DEMO_PUBLISHED = new Date(Date.now() - 12 * 60e3).toISOString();
  const runState = { newer: null, skew: 0, live: null, refresh: null, paint: null, reason: null, done: null };
  // без aria-live: возраст меняется каждые 30 с, скринридер зачитывал бы его (ревью #254)
  function runBadgeChip() {
    return '<span class="k-chip k-runage" data-a="runage" tabindex="0"><span class="ag"></span><span class="cv"></span></span>';
  }
  // «✓ обновлено до #N» — минуту после автоперехода на новый прогон (lib/live.js: sessionStorage desk.live.switched.at)
  function switchedNow() {
    try {
      const id = sessionStorage.getItem('desk.live.switched'), at = +sessionStorage.getItem('desk.live.switched.at');
      if (id == null || !M.run || String(id) !== String(M.run.id) || !(Date.now() - at < 60e3)) return null;
      sessionStorage.removeItem('desk.live.switched.at');
      return at;
    } catch (e) { return null; }
  }
  function runInfo() {
    const sel = window.CONCEPTS_SEL || {}, real = K.source === 'real';
    return { publishedAt: real ? M.run && M.run.publishedAt : DEMO_PUBLISHED, demo: !real, pinned: !!(sel.run_id || sel.as_of), newer: runState.newer, reason: runState.reason };
  }
  const LIVE_TEXT = { sse: 'поток событий (SSE)', poll: 'опрос сервера', 'signed-out': 'стоят: войдите снова' };
  function bindRunBadge(bar) {
    const el = bar.querySelector('[data-a=runage]');
    if (!el) return;
    const cov = K.runCoverage();
    let panel = null;
    if (K.source === 'real') { const at = switchedNow(); if (at) runState.done = at + 60e3; }
    const paint = () => {
      const info = runInfo(), b = K.runBadge(info, Date.now() + runState.skew), done = runState.done && Date.now() < runState.done;
      el.className = 'k-chip k-runage ' + (done && b.level === 'ok' ? 'done' : b.level);
      el.querySelector('.ag').textContent = done && b.level === 'ok' ? '✓ обновлено до #' + M.run.id : (b.level === 'newer' ? '⟳ ' : b.level === 'stale' ? '⚠ ' : '') + b.short;
      el.querySelector('.cv').textContent = cov.short;
      el.title = b.text + (b.why ? '\n' + b.why[0].toUpperCase() + b.why.slice(1) : '') + '\nВошло: ' + cov.rows.map(r => r.k + ' — ' + r.v).join('; ') + '\nЩелчок — подробнее';
      if (panel) fillPanel(info, b);
    };
    const row = (k, v, warn) => '<tr><td class="mut">' + esc(k) + '</td><td style="white-space:normal' + (warn ? ';color:var(--warn)' : '') + '">' + esc(v) + '</td></tr>';
    function fillPanel(info, b) {
      const pub = info.publishedAt ? msk(info.publishedAt) : null;
      panel.innerHTML = '<div class="k-sec" style="margin-top:0">Прогон' + (K.source === 'real' && M.run ? ' #' + esc(M.run.id) : ' · тестовые данные') + '</div>' +
        '<table class="k-tbl k-compact" style="width:100%"><tbody>' +
        row('Опубликован', pub ? pub.hm + ' МСК ' + ddmm(pub.iso) + ' · ' + b.short + (info.demo ? ' (демо)' : '') : 'неизвестно') +
        (b.why ? row(b.level === 'stale' ? 'Внимание' : 'Новее', b.why, b.level !== 'ok') : '') +
        row('Живые обновления', info.demo ? 'нет (демо)' : LIVE_TEXT[runState.live] || 'подключаются', runState.live === 'signed-out') +
        cov.rows.map(x => row(x.k, x.v, x.warn)).join('') + '</tbody></table>';
    }
    function closePanel() {
      if (!panel) return;
      panel.remove(); if (openMenu === panel) openMenu = null; panel = null;
      document.removeEventListener('keydown', onKey, true); document.removeEventListener('click', onDoc, true);
    }
    function onKey(e) {
      if (e.key !== 'Escape') return;
      const live = panel && panel.isConnected;  // панель могло закрыть другое меню шапки
      closePanel();
      if (live) { e.stopPropagation(); el.focus(); }
    }
    function onDoc(e) { if (panel && !panel.contains(e.target) && !el.contains(e.target)) closePanel(); }
    function openPanel() {
      if (openMenu) openMenu.remove();
      const info = runInfo(), b = K.runBadge(info, Date.now() + runState.skew);
      const W = Math.min(380, innerWidth * 0.92), r = el.getBoundingClientRect();
      panel = h('div', 'k-panel k-runpanel');
      panel.style.cssText = 'position:fixed;z-index:140;width:' + W + 'px;max-height:70vh;overflow:auto;box-shadow:var(--shadow);padding:10px 14px;font-size:12.5px';
      panel.style.left = Math.max(8, Math.min(r.left, innerWidth - W - 10)) + 'px'; panel.style.top = (r.bottom + 4) + 'px';
      fillPanel(info, b);
      document.body.appendChild(panel); openMenu = panel;
      document.addEventListener('keydown', onKey, true);
      setTimeout(() => { if (panel) document.addEventListener('click', onDoc, true); }, 0);
    }
    runState.paint = paint;
    paint();
    K.on('runage', paint);  // пороги поменяли в меню вида
    setInterval(paint, 30e3);
    if (runState.done) setTimeout(paint, runState.done - Date.now() + 50);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) paint(); });
    // щелчок (и Enter / пробел с клавиатуры) — открыть панель, повторный — закрыть; Esc и щелчок мимо — закрыть
    el.onclick = e => { e.stopPropagation(); if (panel && panel.isConnected) closePanel(); else { panel = null; openPanel(); } };
    el.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); el.click(); } };
    if (K.source !== 'real') return;
    // лента событий (lib/live.js): новый прогон — отметка сразу, затем сверка со статусом автопилота
    window.addEventListener('desk:live', ev => {
      const d = ev.detail || {};
      if (d.kind === 'mode') { runState.live = d.mode; return; }
      const L = window.DESK_LIVE, ctx = { source: 'real', runId: M.run && M.run.id, asOf: M.asOf };
      const runs = L ? (d.list || []).filter(x => L.newRun(x, ctx)) : [];
      if (!runs.length) return;
      const e = runs[runs.length - 1];
      runState.newer = { run_id: e.run_id, text: e.text };
      paint();
      if (runState.refresh) runState.refresh();
    });
  }
  // меню вида (⚙ рядом с «(1) / −1», #255): формат отрицательных чисел и пороги «⚠ устарел» бейджа прогона
  // (K.setRunAge: поле меняется по Enter и при уходе с поля; неверное значение не сохраняется — в поле возвращается
  // сохранённое, под полями — что не так). Клавиатура: при открытии фокус в панели, Tab и Shift+Tab ходят по кругу
  // внутри неё, Esc закрывает и возвращает фокус на ⚙
  function bindViewMenu(btn) {
    if (!btn) return;
    let panel = null;
    btn.setAttribute('aria-label', 'Вид'); btn.setAttribute('aria-expanded', 'false');
    const close = () => {
      document.removeEventListener('keydown', onKey, true); document.removeEventListener('click', onDoc, true);
      btn.setAttribute('aria-expanded', 'false');
      if (!panel) return;
      panel.remove(); if (openMenu === panel) openMenu = null; panel = null;
    };
    const focusables = () => [...panel.querySelectorAll('button, input')].filter(x => !x.disabled);
    function onKey(e) {
      if (!panel || !panel.isConnected) { close(); return; }  // панель закрыло другое меню шапки
      if (e.key === 'Escape') { e.stopPropagation(); close(); btn.focus(); return; }
      if (e.key !== 'Tab') return;
      const fs = focusables(), i = fs.indexOf(document.activeElement);
      if (!fs.length) return;
      e.preventDefault();
      fs[i < 0 ? (e.shiftKey ? fs.length - 1 : 0) : (i + (e.shiftKey ? -1 : 1) + fs.length) % fs.length].focus();
    }
    function onDoc(e) { if (panel && !panel.contains(e.target) && !btn.contains(e.target)) close(); }
    const field = (k, label, unit) => '<label class="k-viewf"><span>' + esc(label) + '</span><input class="k-input" type="number" min="0" max="' +
      RUNAGE_MAX[k] + '" step="1" inputmode="numeric" data-k="' + k + '"> <span class="mut3">' + unit + '</span></label>';
    const UNIT = { fresh_min: 'мин', max_days: 'дн.' };
    function open() {
      if (openMenu) openMenu.remove();
      const W = Math.min(340, innerWidth * 0.92), r = btn.getBoundingClientRect(), a = K.runAge();
      panel = h('div', 'k-panel k-viewmenu');
      panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Вид');
      panel.style.cssText = 'position:fixed;z-index:140;width:' + W + 'px;max-height:70vh;overflow:auto;box-shadow:var(--shadow);padding:10px 14px;font-size:12.5px';
      panel.style.left = Math.max(8, Math.min(r.right - W, innerWidth - W - 10)) + 'px'; panel.style.top = (r.bottom + 4) + 'px';
      panel.innerHTML = '<div class="k-sec" style="margin-top:0">Отрицательные числа</div><div data-x="neg"></div>' +
        '<div class="k-sec">Бейдж прогона: «⚠ устарел»</div>' +
        field('fresh_min', 'Прогон устарел через, мин', 'по умолч. ' + K.RUN_AGE.fresh_min) +
        field('max_days', '…через, дней', 'по умолч. ' + K.RUN_AGE.max_days) +
        '<div class="mut3" style="margin-top:6px;white-space:normal">Минуты — в рабочем окне пн–пт ' + K.RUN_AGE.fresh_from + '–' + K.RUN_AGE.fresh_to +
        ' МСК, от публикации прогона или от ' + K.RUN_AGE.fresh_from + ', если прогон вчерашний. Дни — в любое время. 0 — правило выключено. ' +
        'Закреплённый прогон и демо не подсвечиваются. Хранится в этом браузере.</div>';
      const err = h('div', 'k-viewerr'); err.setAttribute('role', 'status');
      panel.querySelectorAll('.k-viewf')[1].after(err);
      K.seg(panel.querySelector('[data-x=neg]'), K.NEG_FMTS, K.negFmt, v => K.setNegFmt(v));
      panel.querySelectorAll('input[data-k]').forEach(inp => {
        const k = inp.dataset.k;
        inp.value = a[k];
        const apply = () => {
          const typed = inp.value, ok = K.setRunAge({ [k]: typed });
          inp.value = K.runAge()[k];  // сохранённое: после неверного ввода — прежнее
          inp.style.borderColor = ok ? '' : 'var(--neg)'; inp.setAttribute('aria-invalid', ok ? 'false' : 'true');
          if (!ok) { err.dataset.k = k; err.textContent = '«' + (typed || 'пусто') + '» не подходит: нужно целое от 0 до ' + RUNAGE_MAX[k] + '. Оставлено ' + inp.value + ' ' + UNIT[k]; }
          else if (err.dataset.k === k) err.textContent = '';  // пояснение — про своё поле
        };
        inp.onchange = apply;
        inp.onkeydown = e => { if (e.key === 'Enter') { e.preventDefault(); apply(); } };
      });
      document.body.appendChild(panel); openMenu = panel; btn.setAttribute('aria-expanded', 'true');
      document.addEventListener('keydown', onKey, true);
      const first = focusables()[0]; if (first) first.focus();
      setTimeout(() => { if (panel) document.addEventListener('click', onDoc, true); }, 0);
    }
    btn.onclick = e => { e.stopPropagation(); if (panel && panel.isConnected) close(); else { close(); open(); } };
  }
  // статус автопилота (freshness): latest_run — какой прогон «последний»; now — часы сервера
  function runBadgeStatus(s, reason) {
    if (!s) return;
    runState.reason = reason || null;
    if (s.now) runState.skew = +new Date(s.now) - Date.now();
    const lr = s.latest_run;
    if (lr !== undefined) runState.newer = lr && M.run && String(lr.run_id) !== String(M.run.id) ? { run_id: lr.run_id, text: (lr.label ? lr.label + ' · ' : '') + '#' + lr.run_id } : null;
    if (runState.paint) runState.paint();
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
  // список позиций группы: имя, книга, валюта, тип, причина (p.estReason — extra прогона, lib/real.js); строка — карточка позиции
  K.posList = function (title, ids, sub) {
    const o = drawer(); o.title = title;
    const L = ids.map(id => M.pos[id]).filter(Boolean);
    o.el.querySelector('.t').textContent = title;
    o.el.querySelector('.st').textContent = L.length + ' поз.' + (sub ? ' · ' + sub : '');
    o.bd.innerHTML = '<table class="k-tbl"><thead><tr><th>Позиция</th><th>Книга</th><th>Ccy</th><th>Тип</th><th>Причина</th></tr></thead><tbody>' +
      L.map(p => '<tr class="row" data-id="' + esc(p.id) + '"><td>' + esc(p.name) + ' <span class="mut3 mono">' + esc(p.isin || p.id) + '</span></td><td class="mut">' + esc(p.book) + '</td><td>' + (p.ccy ? K.ccyTag(p.ccy) : '') + '</td><td class="mut">' + esc(p.kindLabel || '') + '</td><td class="mut3" style="max-width:360px">' + esc(p.estReason || '') + '</td></tr>').join('') + '</tbody></table>';
    o.bd.querySelectorAll('tr.row').forEach(tr => tr.onclick = () => K.card(tr.dataset.id));
    return o;
  };

  // ход «Истории прогонов» (сервер досчитывает прошлые даты от самой ранней, R23): пока есть даты к расчёту, чип
  // в шапке говорит, сколько готово, — история риска, MTD/YTD и изменения позиций прошлых дат появляются по мере расчёта.
  // #23/#163: «догрузка истории N из M · K не сходятся» — K: даты, которые бэкфилл пометил проблемными (не сходятся
  // после двух прогонов, упали, раз за разом упираются в базу или сеть), и даты хаба, которые не загружаются (шаг sync,
  // problem_dates: ошибка, попытки, следующая попытка); в подсказке — даты и причины. Серый — остались только такие даты.
  // j — GET /api/v1/backfill/status; null — индикатора нет.
  const dmy = iso => iso ? iso.slice(8, 10) + '.' + iso.slice(5, 7) + '.' + iso.slice(0, 4) : '';
  const BF_WHY = { 'no default run': 'нет прогона', 'positions changed': 'изменились позиции', 'market snapshot changed': 'исправлен рынок',
    'previous snapshot changed': 'изменилось прошлое закрытие', 'program updated': 'обновилась программа' };
  K.backfillStatus = function (j) {
    if (!j) return null;
    const on = j.enabled !== false, todo = on ? j.todo || 0 : 0, seen = {}, lines = [];
    (on ? j.problem_dates || [] : []).forEach(p => {
      seen[p.date] = 1;
      lines.push(dmy(p.date) + ' — ' + (p.error && p.error.indexOf('does not converge') === 0 ? 'не сходится после пересчёта (' + (BF_WHY[p.why] || p.why) + ')'
        : (BF_WHY[p.why] || p.why || 'расчёт упал') + (p.error ? ': ' + p.error : '')) + (p.attempts ? ', попыток: ' + p.attempts : ''));
    });
    (j.hub_problem_dates || []).forEach(p => {
      if (seen[p.date]) return;
      seen[p.date] = 1;
      const nx = p.next_attempt ? msk(p.next_attempt) : null;
      lines.push(dmy(p.date) + ' — с хаба не загружается: ' + (p.error || 'ошибка') + ', попыток: ' + p.attempts + (nx ? ', следующая попытка ' + ddmm(nx.iso) + ' ' + nx.hm : ''));
    });
    const k = lines.length;
    if (!todo && !k) return null;
    const total = j.total != null ? j.total : (j.done || 0) + (j.todo || 0) + (j.failed || 0), grey = !todo;
    const eta = todo && j.eta_s ? (j.eta_s < 5400 ? Math.max(1, Math.round(j.eta_s / 60)) + ' мин' : Math.round(j.eta_s / 3600) + ' ч') : null;
    const title = (grey ? 'Остались только даты, которые не сходятся: их расчёт не получается, сервер пробует снова, когда изменятся данные или программа. '
      : 'Сервер досчитывает прошлые даты с ' + dmy(j.from) + ', от самой ранней: история риска, PnL с начала месяца и года и позиции прошлых дат появляются по мере расчёта (обновите страницу). ' +
        'Следующие: ' + (j.next || []).map(dmy).join(', ') + '.' + (eta ? ' Осталось расчёта ≈ ' + eta + ' (по одной дате за раз, между циклами автопилота).' : '') + ' ') +
      (k ? '\nНе сходятся (' + k + '):\n' + lines.slice(0, 15).join('\n') + (k > 15 ? '\n… ещё ' + (k - 15) : '') + '\n' : '') +
      'Подробности — Настройки → «История прогонов».';
    return { text: '⟳ догрузка истории ' + (j.done || 0) + ' из ' + total + (k ? ' · ' + k + (k % 10 === 1 && k % 100 !== 11 ? ' не сходится' : ' не сходятся') : ''), title, grey };
  };
  function backfillChip(bar) {
    const ch = h('a', 'k-chip'); ch.href = '../settings.html#bf'; ch.style.cssText = 'display:none;text-decoration:none;border-color:var(--warn)';
    bar.querySelector('[data-a=search]').before(ch);
    const poll = () => fetch('../api/v1/backfill/status', { cache: 'no-store' }).then(r => r.ok ? r.json() : null).then(j => {
      const s = K.backfillStatus(j);
      if (s) {
        ch.style.display = ''; ch.textContent = s.text; ch.title = s.title;
        ch.style.borderColor = s.grey ? 'var(--line)' : 'var(--warn)'; ch.style.color = s.grey ? 'var(--fg-3)' : '';
      } else ch.style.display = 'none';
      if (s) setTimeout(poll, 120e3);
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
      '<span class="k-chip" data-a="ofzview" title="Как показан риск ОФЗ: «RUONIA + базис» — риск к котировкам OIS RUONIA и риск к базису КБД − RUONIA, два разных риска ядра (с 0.2.7); «КБД» — ОФЗ на своей кривой (par-риск КБД = базис)">ОФЗ: ' + K.OFZ_VIEWS[M.ofzView || 'basis'] + ' ▾</span>' +
      '<span class="k-chip" data-a="repr" title="' + esc('Валюта отчёта.\n' + K.REPR_NOTE.map(x => x[0][0].toUpperCase() + x[0].slice(1) + ': ' + x[1] + '.').join('\n')) + '">в ' + K.repr + ' ' + K.SYM[K.repr] + ' ▾</span>' +
      srcChip() + runBadgeChip() + (M.notes && M.notes.length ? '<span class="k-chip" data-a="notes" title="Чего нет в данных прогона" style="border-color:var(--warn)">ⓘ ' + M.notes.length + '</span>' : '') +
      '<button class="k-btn" data-a="search">Поиск <span class="k-kbd">Ctrl K</span></button>' +
      '<button class="k-btn" data-a="numfmt"></button>' +
      '<button class="k-btn" data-a="view" title="Вид: формат отрицательных чисел, когда прогон считается устаревшим" aria-haspopup="dialog">⚙</button>' +
      '<button class="k-btn" data-a="theme" title="Светлая / тёмная тема">◐</button>';
    document.body.prepend(bar);
    if (/^https?:$/.test(location.protocol) && !window.DESK_AUTH) document.head.appendChild(Object.assign(document.createElement('script'), { src: '../desk-auth.js' }));  // вход: пользователь, «Выйти», CSRF
    chipsBox = bar.querySelector('.filters'); renderFilterChips();
    bar.querySelector('[data-a=search]').onclick = () => K.palette();
    bar.querySelector('[data-a=theme]').onclick = K.toggleTheme;
    const nb = bar.querySelector('[data-a=numfmt]');  // формат отрицательных чисел (#153): щелчок — другой формат
    const paintNeg = () => {
      nb.textContent = K.negFmt === 'acc' ? '(1)' : '−1';
      nb.title = 'Отрицательные числа: ' + (K.negFmt === 'acc' ? '(1,234.5) красным' : '−1,234.5') + '. Щелчок — ' + (K.negFmt === 'acc' ? 'со знаком минус' : 'в скобках (бухгалтерский формат)') + '; в CSV — всегда минус';
    };
    paintNeg(); nb.onclick = () => K.setNegFmt(K.negFmt === 'acc' ? 'minus' : 'acc'); K.on('numfmt', paintNeg);
    bindViewMenu(bar.querySelector('[data-a=view]'));
    bindSrc(bar); bindRunBadge(bar);
    if (K.source === 'real') { freshness(bar); backfillChip(bar); }
    const oc = bar.querySelector('[data-a=ofzview]');
    oc.onclick = e => { e.stopPropagation(); menu(oc, Object.keys(K.OFZ_VIEWS).map(v => ({ v, t: K.OFZ_VIEWS[v], sub: v === 'basis' ? 'по умолчанию' : '' })), M.ofzView || 'basis', v => { if (v !== (M.ofzView || 'basis')) K.setOfzView(v); }); };
    const rc = bar.querySelector('[data-a=repr]');
    rc.onclick = e => { e.stopPropagation(); const m = menu(rc, K.REPRS.map(c => ({ v: c, t: 'в ' + c + ' ' + K.SYM[c], sub: c === 'RUB' ? '' : 'курс ' + f.n(M.fx[c], 2) })), K.repr, v => { if (v !== K.repr) K.setRepr(v); });
      if (m) { const n = h('div', 'k-repr-note'); n.innerHTML = K.REPR_NOTE.map(x => '<div><b>' + esc(x[0]) + ':</b> ' + esc(x[1]) + '</div>').join(''); m.appendChild(n); } };
    return bar;
  };

  // ---------- кликабельные цифры ----------
  const reg = []; K._reg = reg;
  K.d = function (spec, text, cls) {
    // spec.nullable (#168, фондирование): ни одно значение не посчитано (все null) — «—», а не 0
    if (spec.nullable && text == null && (spec.rows ? spec.rows.every(r => r.v == null) : spec.list && spec.list.every(p => (typeof spec.val === 'function' ? spec.val(p) : p[spec.val]) == null)))
      return '<span class="mut3" title="не посчитано">—</span>';
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
          xAxis: { type: 'value', unit: spec.unit, axisLabel: { formatter: v => fmt(v) } }, yAxis: { type: 'category', data: L.map(x => x.label.length > 34 ? x.label.slice(0, 33) + '…' : x.label) },
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
      body.innerHTML = '<table class="k-tbl"><thead><tr><th></th><th>' + (dim === 'pos' ? 'Позиция' : D[1][0].toUpperCase() + D[1].slice(1)) + '</th>' + (dim === 'pos' ? '<th>Книга</th><th>Ccy</th><th class="n" title="в валюте позиции" data-csv="Номинал, mm в вал.">Номинал</th>' : '<th class="n">Позиций</th>') + '<th class="n">MV, mm ₽</th><th class="n">Вклад</th><th style="width:120px"></th><th class="n">Доля</th><th class="n">Накопл.</th></tr></thead><tbody>' +
        list.map(x => {
          cum += Math.abs(x.v);
          const stripe = dim === 'issuer' || dim === 'pos' ? K.issuerColor(x.p.issuer) : dim === 'ccy' ? K.ccyColor(x.k) : 'transparent';
          return '<tr class="row" data-k="' + esc(x.k) + '"><td class="stripe" style="background:' + stripe + '"></td><td style="max-width:240px;overflow:hidden;text-overflow:ellipsis" title="' + esc(x.label + (dim === 'pos' ? ' · ' + x.p.issuerName : '')) + '">' + esc(x.label) + (dim === 'pos' ? ' <span class="mut3">' + esc(x.p.issuerName) + '</span>' : '') + '</td>' +
            (dim === 'pos' ? '<td class="mut">' + esc(x.p.book) + '</td><td>' + K.ccyTag(x.p.ccy) + '</td><td class="n"' + K.csvv(() => f.mn(x.p.notional, 2)) + '>' + f.mn(x.p.notional, 2) + '<span class="mut3">mm ' + esc(x.p.ccy) + '</span></td>' : '<td class="n mut">' + x.n.length + '</td>') +
            '<td class="n">' + (dim === 'pos' && x.p.mv == null ? '—' : f.mn(mvOf(x), 1)) + '</td>' +
            '<td class="n ' + f.cls(x.v) + '">' + fmt(x.v) + '</td><td>' + K.bar(x.v, max) + '</td><td class="n mut">' + Math.round(Math.abs(x.v) / absTot * 100) + '%</td><td class="n mut3">' + Math.round(cum / absTot * 100) + '%</td></tr>';
        }).join('') + '</tbody><tfoot><tr><td></td><td>Итого</td><td colspan="' + (dim === 'pos' ? 3 : 1) + '"></td><td class="n">' + f.mn(mvTot, 1) + '</td><td class="n ' + f.cls(tot) + '">' + fmt(tot) + '</td><td colspan="3"></td></tr></tfoot></table>';
      // CSV разбора (#184): то, что на экране, — разрез и фильтр. Колонки без заголовка (цветная полоска слева и
      // столбик вклада) не выгружаются, «Доля» и «Накопл.» — выгружаются. Колонки у разрезов разные — ключ ширины
      // свой у каждого разреза
      K.tableTools(body, { viewKey: 'drill-' + dim, title: spec.title + ' · ' + D[1] + (spec.unit ? ', ' + spec.unit : '') });
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
  // единица оси (#105): ось значений с {unit: 'тыс ₽/бп'} получает подпись — у вертикальной над осью, у
  // горизонтальной под подписями делений; место под подпись добавляется к отступу сетки. «₽» — валюта отчёта (K.u):
  // график рисуется на canvas, подмена символа в разметке до него не доходит
  function axisUnits(o) {
    const arr = ax => (Array.isArray(ax) ? ax : [ax]).filter(Boolean);
    let top = 0, bottom = false;
    arr(o.yAxis).forEach((x, i) => {
      if (x.unit && !x.name) { x.name = K.u(x.unit); x.nameLocation = 'end'; if (x.nameGap == null) x.nameGap = 7; x.nameTextStyle = Object.assign({ align: i ? 'right' : 'left', fontSize: 10, color: css('--fg-3') }, x.nameTextStyle); top = Math.max(top, 16 + x.nameGap - 7); }
      delete x.unit;
    });
    arr(o.xAxis).forEach(x => {
      if (x.unit && !x.name && x.type === 'value') { x.name = K.u(x.unit); x.nameLocation = 'middle'; x.nameGap = 24; x.nameTextStyle = Object.assign({ fontSize: 10, color: css('--fg-3') }, x.nameTextStyle); bottom = true; }
      delete x.unit;
    });
    // легенда сверху над подписью оси — в одну строку (прокрутка), иначе перенос легенды наезжает на подпись
    if (top && o.legend && !Array.isArray(o.legend) && o.legend.orient !== 'vertical' && o.legend.bottom == null) o.legend = Object.assign({ type: 'scroll' }, o.legend);
    if (o.grid && !Array.isArray(o.grid)) {
      o.grid = Object.assign({}, o.grid);
      if (top && typeof o.grid.top === 'number') o.grid.top += top;
      if (bottom) o.grid.bottom = (typeof o.grid.bottom === 'number' ? o.grid.bottom : 0) + 18;
    }
  }
  K.axisUnits = axisUnits;
  K.chart = function (el, optFn) {
    if (!window.echarts) { el.innerHTML = '<div class="k-empty">нет echarts</div>'; return { on() { }, dispose() { }, setOption() { } }; }
    let c = echarts.init(el, null, { renderer: 'canvas' });
    const build = () => {
      const o = Object.assign(K.chartBase(), typeof optFn === 'function' ? optFn() : optFn);
      axisUnits(o); axisTheme(o.xAxis); axisTheme(o.yAxis);
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
        yAxis: [{ type: 'value', unit: 'тыс ₽/бп', axisLabel: { formatter: v => f.dv(v) } }, { type: 'value', position: 'right', splitLine: { show: false }, axisLabel: { formatter: v => f.dv(v), color: css('--warn') } }],
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

  // ---------- фильтр по значениям столбца (R37) ----------
  // Столбец с filter (true — по значению val, или функция r → значение) фильтруется, как любой: ⏷ в заголовке —
  // список значений столбца с числом строк (под остальными фильтрами), отмеченные видны. Валюта и эмитент —
  // обычные столбцы таблиц позиций: сортируются и фильтруются, как остальные.
  K.fltVal = (c, r) => { const v = c.filter === true ? (c.val ? c.val(r) : r[c.key]) : c.filter(r); return v == null || v === '' ? '—' : String(v); };
  K.applyFilters = (rows, filters, colOf, skip) => {
    const ks = Object.keys(filters || {}).filter(k => k !== skip && filters[k] && colOf(k) && colOf(k).filter);
    return ks.length ? rows.filter(r => ks.every(k => filters[k].includes(K.fltVal(colOf(k), r)))) : rows;
  };
  K.fltItems = (rows, c) => {
    const n = new Map(); rows.forEach(r => { const v = K.fltVal(c, r); n.set(v, (n.get(v) || 0) + 1); });
    return [...n].map(([v, k]) => ({ v, t: c.fltLabel ? c.fltLabel(v) : v, n: k })).sort((a, b) => c.fltOrder ? c.fltOrder(a.v, b.v) : a.t.localeCompare(b.t, 'ru'));
  };
  K.fltIcon = (c, filters) => c.filter ? '<span class="k-flt' + (filters && filters[c.key] ? ' on' : '') + '" data-flt="' + esc(c.key) + '" title="фильтр по значениям столбца">⏷</span>' : '';
  let fltPop = null;
  K.closeColFilter = () => { if (fltPop) { fltPop.remove(); fltPop = null; } };
  // items: [{v, t, n}]; sel — выбранные значения или null (все); onChange(массив | null)
  K.colFilter = function (anchor, items, sel, onChange) {
    K.closeColFilter();
    const m = h('div', 'k-panel k-flt-pop'); m.style.cssText = 'position:fixed;z-index:140;min-width:200px;max-width:340px;max-height:60vh;overflow:auto;box-shadow:var(--shadow);padding:4px 0';
    const r = anchor.getBoundingClientRect(); m.style.left = Math.max(4, Math.min(r.left, innerWidth - 350)) + 'px'; m.style.top = (r.bottom + 4) + 'px';
    let cur = sel ? new Set(sel) : null, q = '';
    const tb = h('div'); tb.style.cssText = 'display:flex;gap:6px;padding:4px 8px 6px;border-bottom:1px solid var(--line);position:sticky;top:-4px;background:var(--panel)';
    const inp = h('input', 'k-input'); inp.placeholder = 'найти…'; inp.style.cssText = 'min-width:0;flex:1';
    const all = h('button', 'k-btn', 'все'), none = h('button', 'k-btn', 'снять');
    tb.append(inp, all, none); m.appendChild(tb);
    const list = h('div'); m.appendChild(list);
    const emit = () => onChange(cur && cur.size < items.length ? [...cur] : null);
    function paint() {
      list.innerHTML = '';
      items.filter(it => !q || it.t.toLowerCase().includes(q)).forEach(it => {
        const on = !cur || cur.has(it.v);
        const row = h('div', 'mi', '<span style="width:16px;display:inline-block">' + (on ? '☑' : '☐') + '</span>' + esc(it.t) + ' <span class="mut3" style="font-size:11px">' + it.n + '</span>');
        row.style.cssText = 'padding:4px 12px;cursor:pointer;white-space:nowrap';
        row.onclick = e => { e.stopPropagation(); if (!cur) cur = new Set(items.map(x => x.v)); if (cur.has(it.v)) cur.delete(it.v); else cur.add(it.v); paint(); emit(); };
        list.appendChild(row);
      });
    }
    inp.oninput = () => { q = inp.value.toLowerCase(); paint(); };
    inp.onclick = e => e.stopPropagation();
    m.onclick = e => e.stopPropagation();
    all.onclick = () => { cur = null; paint(); emit(); };
    none.onclick = () => { cur = new Set(); paint(); emit(); };
    paint(); document.body.appendChild(m); fltPop = m;
    if (items.length > 8) setTimeout(() => inp.focus(), 0);
    setTimeout(() => document.addEventListener('click', K.closeColFilter, { once: true }), 0);
    return m;
  };

  // ---------- вид таблиц и панелей: get / set / reset по ключу (#102) ----------
  // Отдельный маленький слой, чтобы перевести хранение на сервер по пользователю (#109, /api/v1/views), не трогая
  // таблицы: сейчас — localStorage браузера (живёт по адресу страницы), чтение и запись в try/catch (в приватном окне
  // и без доступа к хранилищу вид просто не запоминается). Значение — JSON-объект; get отдаёт копию или null.
  K.viewStore = (() => {
    const P = 'tdm.view.v1:', mem = {};
    return {
      get(key) {
        let s = mem[key];
        if (s === undefined) { try { s = localStorage.getItem(P + key); } catch (e) { s = null; } }
        try { return s ? JSON.parse(s) : null; } catch (e) { return null; }
      },
      set(key, val) { const s = JSON.stringify(val); mem[key] = s; try { localStorage.setItem(P + key, s); } catch (e) { } },
      reset(key) { mem[key] = null; try { localStorage.removeItem(P + key); } catch (e) { } },
    };
  })();
  // ---------- формат отрицательных чисел (#153) ----------
  // настройка — в том же слое вида (ключ 'ui:numfmt', {neg: 'acc' | 'minus'}); чужое значение — по умолчанию 'acc'
  const NEG_KEY = 'ui:numfmt', negOk = v => K.NEG_FMTS.some(x => x[0] === v);
  { const s = K.viewStore.get(NEG_KEY); if (s && negOk(s.neg)) K.negFmt = s.neg; }
  // сменить формат: запомнить и перерисовать без перезагрузки — блоки страниц перерисовываются по событию 'filter'
  // (K.onFilter, K.live), графики — по K.charts; подписчики 'numfmt' — то, что от фильтра не зависит (кнопка шапки)
  K.setNegFmt = v => {
    if (!negOk(v) || v === K.negFmt) return;
    K.negFmt = v; K.viewStore.set(NEG_KEY, { neg: v });
    fire('numfmt'); fire('filter');
    K.charts.forEach(c => c.redraw && c.redraw());
    if (typeof document.querySelectorAll === 'function') K.accMarkAll(document);
  };
  // выполнить fn с другим форматом отрицательных (CSV — всегда 'minus'); настройка после вызова — прежняя
  K.withNegFmt = (v, fn) => { const old = K.negFmt; K.negFmt = v; try { return fn(); } finally { K.negFmt = old; } };
  // красный цвет и выравнивание в бухгалтерском формате — по тексту цифры, а не в каждом форматтере: форматтеры
  // отдают простой текст (он идёт и в подписи графиков, и в title), а мониторы рисуют таблицы сами. Где «(…)» —
  // это число: числовая ячейка td.n, кликабельная цифра .dr, значение плитки и поля карточки. Текст, который
  // начинается с числа в скобках, — отрицательное (k-accn, красным); числовая ячейка, в которой только
  // неотрицательное число, — k-accp: невидимая «)» после числа, цифры столбца стоят ровно под скобками
  // .k-num — число форматтера внутри текста (пояснение «ставки (1.6)» на C05): так его знак виден и в CSV (K.tableCsv)
  K.ACC_SEL = 'td.n, .dr, .k-kpi .v, .k-fields .v, .k-num';
  K.accClass = text => {
    if (K.negFmt !== 'acc') return '';
    const t = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
    if (/^\(\d[\d,]*(?:\.\d+)?[^()]*\)/.test(t)) return 'k-accn';
    if (/^\+?\d[\d,]*(?:\.\d+)?%?$/.test(t)) return 'k-accp';
    return '';
  };
  // смысловой цвет страницы важнее бухгалтерского красного (#103): классы знака и изменения (pos, neg, chg-up,
  // chg-dn — у доходности рост красный, падение зелёное), цвет текста в style и фон тепловой карты по знаку (K.heat)
  // — у самого элемента или у его ячейки; такая цифра остаётся в скобках, но красной не становится. Фон по величине
  // (K.heatMono) и фон риска (K.heatRisk) помечены K.HEAT_PLAIN и смысловым не считаются (#216)
  K.ACC_KEEP = ['pos', 'neg', 'chg-up', 'chg-dn'];
  K.accKeep = e => {
    for (let x = e, i = 0; x && i < 4; x = x.parentElement, i++) {
      if (x.classList && K.ACC_KEEP.some(c => x.classList.contains(c))) return true;
      const st = x.getAttribute && x.getAttribute('style');
      if (st && /(^|;)\s*color\s*:/.test(st)) return true;
      if (st && /(^|;)\s*background\s*:/.test(st) && !HEAT_RE.test(st)) return true;
      if (x.tagName === 'TD' || x.tagName === 'TH') break;
    }
    return false;
  };
  // ячейка тепловой карты по величине / риска (метка K.HEAT_PLAIN у элемента или его ячейки) — красный минус в ней
  // получает класс k-heatneg (kit.css: цвет --neg-heat, #265); метку без фона ставит и строка «Итого» тепловой карты,
  // чтобы красный в колонке был одного оттенка
  const HEAT_RE = /(^|;)\s*--k-heat\s*:\s*plain/;
  K.heatPlainOf = e => {
    for (let x = e, i = 0; x && i < 4; x = x.parentElement, i++) {
      const st = x.getAttribute && x.getAttribute('style');
      if (st && HEAT_RE.test(st)) return true;
      if (x.tagName === 'TD' || x.tagName === 'TH') break;
    }
    return false;
  };
  // метка data-kneg (#222) — «число отформатировано как отрицательное, в скобках»: ставится и там, где красный уступил
  // смысловому цвету (K.accKeep); по ней CSV меняет скобки на минус (K.accMarkedToMinus), раскраска на неё не влияет
  const accMark = e => {
    const c = K.accClass(e.textContent);
    const neg = c === 'k-accn' && !K.accKeep(e);
    e.classList.toggle('k-accn', neg); e.classList.toggle('k-heatneg', neg && K.heatPlainOf(e)); e.classList.toggle('k-accp', c === 'k-accp' && e.tagName === 'TD');
    const d = e.dataset; if (d && (c === 'k-accn') !== ('kneg' in d)) { if (c === 'k-accn') d.kneg = ''; else delete d.kneg; }
  };
  K.accMarkAll = root => { const r = root || document; if (r.matches && r.matches(K.ACC_SEL)) accMark(r); if (r.querySelectorAll) r.querySelectorAll(K.ACC_SEL).forEach(accMark); };
  if (typeof MutationObserver === 'function' && typeof requestAnimationFrame === 'function') {
    // разметку меняют страницы и компоненты: новые узлы и узлы с изменённым текстом размечаются в следующем кадре
    let todo = new Set(), queued = false;
    const flush = () => {
      queued = false; const L = [...todo]; todo = new Set();
      L.forEach(n => { if (n.isConnected) { K.accMarkAll(n); const up = n.parentElement && n.parentElement.closest(K.ACC_SEL); if (up) accMark(up); } });
    };
    new MutationObserver(ms => {
      ms.forEach(m => { const n = m.type === 'characterData' ? m.target.parentElement : m.target; if (n && n.nodeType === 1) todo.add(n); });
      if (!queued && todo.size) { queued = true; requestAnimationFrame(flush); }
    }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  }
  // стабильный ключ таблицы — без списка колонок (добавили колонку — раскладка не теряется, K.viewLayout сводит её с
  // текущим набором): opts.viewKey (его передают все мониторы), иначе страница + ближайший id в разметке
  K.tableKey = (el, opts) => {
    if (opts.viewKey) return 'table:' + opts.viewKey;
    let page = '', anchor = '';
    try { page = (location.pathname || '').split('/').pop(); } catch (e) { }
    try { anchor = el.id || (el.closest && el.closest('[id]') ? el.closest('[id]').id : ''); } catch (e) { }
    return 'table:' + page + '|' + anchor;
  };
  // раскладка из хранилища — чужие данные: только объект; order — строки известных колонок без повторов, w — известные
  // колонки с конечной положительной шириной (32…2000 px); остальное отбрасывается, мусор даёт исходный вид
  K.viewLayout = (v, keys) => {
    const out = {}, known = new Set(keys);
    if (!v || typeof v !== 'object' || Array.isArray(v)) return out;
    if (Array.isArray(v.order)) {
      const o = [...new Set(v.order.filter(k => typeof k === 'string' && known.has(k)))];
      if (o.length) out.order = o;
    }
    if (v.w && typeof v.w === 'object' && !Array.isArray(v.w)) {
      const w = {};
      Object.keys(v.w).forEach(k => { const x = v.w[k]; if (known.has(k) && typeof x === 'number' && isFinite(x) && x > 0) w[k] = Math.min(2000, Math.max(32, Math.round(x))); });
      if (Object.keys(w).length) out.w = w;
    }
    return out;
  };

  // ---------- выгрузка таблиц в CSV (#110, #144) ----------
  // CSV для Excel с американскими региональными настройками (решение владельца, #110): UTF-8 с BOM (иначе
  // кириллица в заголовках — кракозябры), разделитель «,», десятичная точка, числа без разделителей тысяч и
  // с обычным минусом, даты MM/DD/YYYY, кавычки по RFC 4180. Значения — как на экране (в единицах заголовка):
  //   число — числом («−1,234.5» → -1234.5); дата — датой Excel (03/15/2029);
  //   текст в ячейке, который Excel целиком прочитал бы как число, дату, время, логическое значение (K.csvExcelConverts)
  //   или формулу («= + - @» в начале), — строковой формулой ="…" (#144): Excel показывает текст как есть, а формула
  //   не выполнится — она внутри строкового литерала. Литерал в формуле Excel — до 255 знаков: длиннее — как есть,
  //   а начало «= + - @» — с апострофом. Остальной текст — как есть. Заголовки и название таблицы — K.csvLabel.
  K.htmlText = s => String(s == null ? '' : s).replace(/<(?:[^>"']|"[^"]*"|'[^']*')*>/g, '').replace(/&nbsp;/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();
  K.csvNum = t => {  // «−1,234.5» и «(1,234.5)» → «-1234.5»; «+12» → «12»; «4.44%» → «4.44%»; не число — null
    const a = /^\(([^()]*)\)$/.exec(t), u = a ? a[1] : t;  // бухгалтерский формат (#153): скобки — минус
    const m = /^([+−-]?)(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(%?)$/.exec(u);
    if (!m || (a && m[1])) return null;
    return (m[1] === '+' ? '' : m[1] || a ? '-' : '') + m[2].replace(/,/g, '') + (m[3] || '') + m[4];
  };
  // день проверяется по длине месяца с учётом високосных лет (#161): «31.02.29» — не дата, остаётся текстом (и защищён)
  const usDate = (y, mo, d) => {
    const Y = +y, m = +mo, D = +d, leap = (Y % 4 === 0 && Y % 100 !== 0) || Y % 400 === 0;
    if (!(m >= 1 && m <= 12 && D >= 1)) return null;
    return D <= [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1] ? mo + '/' + d + '/' + y : null;
  };
  K.csvDate = s => {  // «2029-03-15» (ISO, можно со временем) → «03/15/2029»; не дата — null
    const m = /^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])/.exec(s == null ? '' : String(s));
    return m ? usDate(m[1], m[2], m[3]) : null;
  };
  // даты экрана (K.f.date «15.03.29», а также «15.03.2029») → MM/DD/YYYY. raw — значение ячейки до форматирования:
  // если это ISO-дата, век берётся из неё; иначе двузначный год — 20YY
  K.csvDates = (t, raw) => {
    const iso = K.csvDate(raw);
    if (iso) t = t.split(f.date(String(raw).slice(0, 10))).join(iso);
    return t.replace(/(^|[^\d.,])(\d{2})\.(\d{2})\.(\d{4}|\d{2})(?!\d|\.\d)/g,
      (m, p, d, mo, y) => { const u = usDate(y.length === 2 ? '20' + y : y, mo, d); return u ? p + u : m; });
  };
  const csvQuote = t => /[,"\r\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;  // RFC 4180
  const csvFormula = t => /^[=+\-@\t\r]/.test(t);
  // текст, который Excel с американскими настройками целиком прочитает не как текст (#144): число (знак, скобки,
  // $, разделители тысяч, ведущие нули, экспонента, %), дробь или дата из цифр (3/4, 03-04, 2029-03-15, 5.2.29),
  // дата с месяцем словом (Mar-24, 24 Mar, March 2024), время (12:30, 1 pm), дата со временем, TRUE/FALSE,
  // значение ошибки (#N/A). Текст с цифрами внутри слов («ОФЗ 26238», «пут 03/15/2029», ISIN) Excel не трогает.
  const MON = '(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\\.?';
  const XL_NUM = /^[+-]?\(?[+-]?\$?\s*(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?\s*%?\)?$/i,
    XL_FRAC = /^\d+ \d+\/\d+$/,
    XL_DATE = /^\d{1,4}[\/.-]\d{1,2}(?:[\/.-]\d{1,4})?$|^\d{1,2}[\/-]\d{4}$/,  // и месяц с годом: 12/2029
    XL_MDATE = new RegExp('^(?:\\d{1,4}[\\s/-])?' + MON + '(?:[\\s/,-]+\\d{1,4}){0,2}$', 'i'),
    XL_TIME = /^\d{1,2}(?::\d{1,2}(?::\d{1,2}(?:\.\d+)?)?\s*(?:[ap]m?)?|\s*[ap]m?)$/i,
    XL_ERR = /^#(?:n\/a|div\/0!|value!|ref!|name\?|num!|null!|getting_data|spill!|calc!)$/i;
  K.csvExcelConverts = t => {
    if (/^(?:true|false)$/i.test(t) || XL_ERR.test(t)) return true;
    if (!/\d/.test(t)) return false;
    const i = t.indexOf(' '), dt = i > 0 && XL_TIME.test(t.slice(i + 1)) ? t.slice(0, i) : null;  // «03/15/2029 18:30», «1/2/2024 10:30 AM»
    return XL_NUM.test(t) || XL_FRAC.test(t) || XL_DATE.test(t) || XL_MDATE.test(t) || XL_TIME.test(t) || (dt != null && (XL_DATE.test(dt) || XL_MDATE.test(dt)));
  };
  // заголовок колонки, название таблицы: Excel их не преобразует, нужна только защита от формул — апостроф
  K.csvLabel = t => { t = t == null ? '' : String(t); return csvQuote(csvFormula(t) ? "'" + t : t); };
  K.csvCell = (t, num, raw) => {
    t = (t == null ? '' : String(t)).replace(/^[●■]\s*/, '');  // цветная метка эмитента / кривой — оформление
    t = t.replace(/\u2212/g, '-');  // юникодный минус экрана — обычным и в тексте («−27 Z»), как в числах (#161)
    if (num) { if (t === '—' || t === '·') return ''; const x = K.csvNum(t); if (x != null) return x; }
    t = K.csvDates(t, raw);
    if (/^\d{2}\/\d{2}\/\d{4}$/.test(t)) return t;  // дата целиком — датой Excel
    const formula = csvFormula(t);
    if (formula || K.csvExcelConverts(t)) {
      if (t.length <= 255) return csvQuote('="' + t.replace(/"/g, '""') + '"');
      if (formula) t = "'" + t;
    }
    return csvQuote(t);
  };
  // колонка-дата: в CSV — дата из ISO-значения ячейки (opts.date у колонки K.table, data-date у
  // ячейки ручной таблицы), без дня недели и прочих подписей экрана (#161); не ISO-дата — как обычная ячейка
  K.csvDateCell = (iso, t, num) => K.csvDate(iso) || K.csvCell(t, num, iso);
  // текст CSV: первая строка — номер прогона, дата среза, название таблицы и число строк, пустая строка, заголовок
  // (у таблицы без заголовка — нет), строки; BOM добавляется при скачивании
  K.csvTitle = opts => K.htmlText(opts.title || (typeof document.title === 'string' ? document.title : '') || 'таблица');
  K.csvBuild = (title, head, lines) => {
    const R = K.csvRun(), q = K.csvLabel;
    return ['run_id,' + (/^\d+$/.test(R.run_id) ? R.run_id : q(R.run_id)) + ',as_of,' + (K.csvDate(R.as_of) || q(R.as_of)) + ',таблица,' + q(title) + ',строк,' + lines.length, '']
      .concat(head ? [head.join(',')] : [], lines).join('\r\n') + '\r\n';
  };
  // имя файла — латиницей (кириллицу в имени скачивания браузер может заменить на «download»): opts.fileName, иначе
  // opts.viewKey, иначе страница и якорь таблицы; дальше дата среза и прогон. Дата среза в имени — ISO (YYYY-MM-DD),
  // а не MM/DD/YYYY: «/» в имени файла недопустим, ISO однозначен в любой локали и файлы сортируются по дате
  K.csvFileName = (el, opts) => {
    const R = K.csvRun();
    let page = '', anchor = '';
    try { page = (location.pathname || '').split('/').pop().replace(/\.html?$/, ''); } catch (e) { }
    try { anchor = el.id || (el.closest && el.closest('[id]') ? el.closest('[id]').id : ''); } catch (e) { }
    const base = (opts.fileName || opts.viewKey || [page || 'table', anchor].filter(Boolean).join('_')).replace(/[^A-Za-z0-9_.-]+/g, '_');
    return 'tdm_' + base + '_' + (R.as_of || 'nodate') + '_run-' + (R.run_id || 'none').replace(/[^A-Za-z0-9_-]+/g, '_') + '.csv';
  };
  K.csvRun = () => ({ run_id: M.run && M.run.id != null ? String(M.run.id) : (K.source === 'real' ? '' : 'demo'), as_of: M.asOf || '' });
  K.download = (name, text, type) => {
    const a = document.createElement('a'), u = URL.createObjectURL(new Blob([text], { type: type || 'text/plain;charset=utf-8' }));
    a.href = u; a.download = name; document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(u), 1000);
  };

  // ---------- таблица ----------
  // единица колонки (#105): opts.columns[].unit, если её нет в подписи (подпись с «,» — «MV, mm ₽» — уже с единицей);
  // в шапке таблицы — второй строкой под подписью, в CSV — в заголовке через запятую
  K.colUnit = c => c && c.unit && !K.htmlText(c.label).includes(',') ? c.unit : '';
  // Раскладка (#102): граница заголовка — ширина, двойной щелчок по ней — по содержимому, перетаскивание заголовка —
  // порядок колонок; запоминается по ключу таблицы (K.tableKey) в K.viewStore, «⟲ вид» — сбросить.
  // opts: {columns:[{key,label,val(r),fmt(v,r),cls,title,sum:true|fn,drill:{title,unit,fmt,val}, w}], rows|rowsFn, groupBy:[{key(r),label(k,rows)}], search:bool,
  //        sort:[[key,dir]] (по умолчанию — строки в группах; щелчок по заголовку сортирует и группы, кроме groupBy[i].fixed), stripe(r)->color, onRow(r), rowCls(r), tools(el), maxHeight, footer:bool,
  //        groupable:[уровни] — группировка по желанию (переключатель «плоская / валюта → эмитент»); по умолчанию таблица плоская (R37),
  //        колонка: date:true — колонка-дата (в CSV дата из ISO-значения, без подписей экрана),
  //        csvFmt(v, r) — значение для CSV, если экранное fmt добавляет значки и метки (иначе — fmt),
  //        csvExtra: [подписи] — колонки только для CSV сразу за этой (на экране и в раскладке их нет, #276):
  //        csvFmt тогда отдаёт массив [значение колонки, значения дополнительных] («-27 Z» → -27 и Z),
  //        sumNeutral — итог без цвета знака (расхождение, а не прибыль/убыток), footCount(rows) — подпись числа строк «Итого»
  //        (например «7 из 9», когда итоги считаются не по всем строкам),
  //        viewKey — стабильный ключ раскладки латиницей, он же основа имени CSV (без него — страница + id, K.tableKey), title — название в CSV, fileName — основа имени файла CSV (латиницей)}
  K.table = function (el, opts) {
    const st = { sort: opts.sort ? opts.sort.slice() : [], q: '', collapsed: {}, groupSort: false, filters: {}, grouped: false };
    el.innerHTML = '';
    let tools = null, fltChip = null;
    if (opts.search !== false || opts.tools || opts.groupable) {
      tools = h('div', 'k-tbl-tools'); el.appendChild(tools);
      if (opts.search !== false) { const i = h('input', 'k-input'); i.placeholder = 'поиск в таблице…'; i.oninput = () => { st.q = i.value.toLowerCase(); draw(); }; tools.appendChild(i); }
      if (opts.groupable) {
        const g = h('span'); tools.appendChild(g);
        K.seg(g, [['flat', 'плоская'], ['grp', opts.groupLabel || 'валюта → эмитент']], 'flat', v => { st.grouped = v === 'grp'; st.collapsed = {}; draw(); });
      }
      tools.appendChild(h('span', 'k-note', 'щелчок по заголовку — сортировка · Shift+щелчок — по нескольким · ⏷ — фильтр по значениям'));
      fltChip = h('span', 'k-chip on'); fltChip.style.display = 'none'; fltChip.onclick = () => { st.filters = {}; draw(); }; tools.appendChild(fltChip);
      if (opts.tools) { const sp = h('span'); sp.style.flex = '1'; tools.appendChild(sp); opts.tools(tools); }
    }
    const wrap = h('div', 'k-tbl-wrap'); if (opts.maxHeight) wrap.style.maxHeight = opts.maxHeight; el.appendChild(wrap);
    // кнопки вида: в строке инструментов, а у таблиц без неё — узкой строкой под таблицей
    const ctl = h('span', 'k-tbl-ctl');
    if (tools) { if (!opts.tools) { const sp = h('span'); sp.style.flex = '1'; tools.appendChild(sp); } tools.appendChild(ctl); }
    else { const bar = h('div', 'k-tbl-ctlbar'); bar.appendChild(ctl); el.appendChild(bar); }
    const vkey = K.tableKey(el, opts);
    const colKeys = () => opts.columns.map(c => c.key);
    let lay = K.viewLayout(K.viewStore.get(vkey), colKeys());  // {order: [key], w: {key: px}} — проверенная форма
    const fixedW = () => !!lay.w && cols().some(c => lay.w[c.key]);
    const resetBtn = h('button', 'k-tbtn', '⟲ вид'); resetBtn.title = 'сбросить ширину и порядок колонок этой таблицы';
    resetBtn.onclick = () => { lay = {}; K.viewStore.reset(vkey); draw(); };
    const csvBtn = h('button', 'k-tbtn', '⤓ CSV');
    csvBtn.title = 'выгрузить в CSV (Excel): колонки — в текущем порядке, строки — после поиска, фильтров и сортировки; номер прогона и дата среза — в первой строке и в имени файла';
    csvBtn.onclick = () => K.download(csvName(), '\uFEFF' + csv(), 'text/csv;charset=utf-8');
    ctl.append(csvBtn, resetBtn);
    const paintCtl = () => { resetBtn.style.display = fixedW() || (lay.order && lay.order.length) ? '' : 'none'; };
    const saveLay = () => { K.viewStore.set(vkey, lay); paintCtl(); };
    let noClickTill = 0;  // после перетаскивания щелчок по заголовку — не сортировка
    let shown = [];       // строки на экране: после поиска, фильтров и сортировки (их и выгружает CSV)
    // видимые колонки в сохранённом порядке; новых в сохранённом порядке нет — они в конце, в исходном порядке
    const cols = () => {
      const C = opts.columns.filter(c => !c.hidden), o = lay.order;
      if (!o || !o.length) return C;
      const ix = c => { const i = o.indexOf(c.key); return i < 0 ? 1e6 : i; };
      return C.map((c, i) => [c, i]).sort((a, b) => (ix(a[0]) - ix(b[0])) || (a[1] - b[1])).map(x => x[0]);
    };
    const val = (c, r) => c.val ? c.val(r) : r[c.key];
    // колонка для сортировки строк и групп: значение и, если у колонки есть итог, слагаемое суммы группы
    const colOf = k => { const c = opts.columns.find(x => x.key === k); return c ? { val: r => val(c, r), sum: c.sum ? (typeof c.sum === 'function' ? c.sum : r => val(c, r) || 0) : null, filter: c.filter, key: c.key } : null; };
    function sorted(rows) {
      if (!st.sort.length) return rows;
      return rows.slice().sort(K.rowCmp(st.sort, colOf));
    }
    function draw() {
      let rows = opts.rowsFn ? opts.rowsFn() : opts.rows;
      if (st.q) rows = rows.filter(r => (r.searchText || JSON.stringify(r)).toLowerCase().includes(st.q));
      const base = rows;
      rows = sorted(K.applyFilters(rows, st.filters, colOf));
      shown = rows;
      const C = cols();
      const nf = Object.keys(st.filters).filter(k => st.filters[k]).length;
      if (fltChip) { fltChip.style.display = nf ? '' : 'none'; fltChip.innerHTML = '<span class="x">✕</span> фильтры: ' + nf; }
      const head = '<thead><tr>' + (opts.stripe ? '<th class="stripe"></th>' : '') + C.map(c => { const si = st.sort.findIndex(s => s[0] === c.key); return '<th class="sortable ' + (c.num ? 'n' : '') + '" data-k="' + c.key + '" title="' + esc(c.title || '') + '"' + (c.w && !fixedW() ? ' style="width:' + c.w + '"' : '') + '>' + (c.tip ? '<span class="k-hint">' + c.label + '</span>' : c.label) + (si >= 0 ? '<span class="si">' + (st.sort[si][1] > 0 ? '▲' : '▼') + (st.sort.length > 1 ? si + 1 : '') + '</span>' : '') + K.fltIcon(c, st.filters) + (K.colUnit(c) ? '<span class="k-thu">' + esc(K.colUnit(c)) + '</span>' : '') + '<span class="k-rsz" data-rsz="' + esc(c.key) + '" title="ширина: тянуть · двойной щелчок — по содержимому"></span></th>'; }).join('') + '</tr></thead>';
      // заданные ширины: фиксированная раскладка по colgroup, таблица — шириной суммы колонок
      const cg = fixedW() ? '<colgroup>' + (opts.stripe ? '<col style="width:4px">' : '') + C.map(c => '<col data-k="' + esc(c.key) + '" style="width:' + (lay.w[c.key] || 100) + 'px">').join('') + '</colgroup>' : '';
      const tw = fixedW() ? ' k-fixed" style="width:' + C.reduce((s, c) => s + (lay.w[c.key] || 100), opts.stripe ? 4 : 0) + 'px' : '';
      const cell = (c, r) => { const v = val(c, r); return '<td class="' + (c.num ? 'n ' : '') + (c.cls ? (typeof c.cls === 'function' ? c.cls(v, r) : c.cls) : '') + '"' + (c.style ? ' style="' + c.style(v, r) + '"' : '') + '>' + (c.fmt ? c.fmt(v, r) : esc(v)) + '</td>'; };
      const sumCell = (c, list) => {
        if (!c.sum) return '<td></td>';
        if (c.nullable && list.every(r => val(c, r) == null)) return '<td class="n"><span class="mut3" title="не посчитано">—</span></td>';  // #168
        const fn = typeof c.sum === 'function' ? c.sum : r => val(c, r) || 0;
        const tot = list.reduce((s, r) => s + fn(r), 0);
        const fm = c.sumFmt || c.fmt || (v => f.n(v));
        if (c.drill !== false && list.length && list[0].searchText) return '<td class="n">' + K.d({ title: (c.drillTitle || c.label.replace(/<[^>]+>/g, '')), unit: c.unit, fmt: v => fm(v, {}), rows: list.map(p => ({ p, v: fn(p) })) }, fm(tot, {})) + '</td>';
        return '<td class="n ' + (c.sumNeutral ? '" style="color:var(--fg)' : f.cls(tot)) + '">' + fm(tot, {}) + '</td>';
      };
      let body = '';
      const groups = opts.groupBy || (st.grouped && opts.groupable) || [];
      const nCols = C.length + (opts.stripe ? 1 : 0);
      // подпись группы и «Итого» — в первой колонке без итога (колонку с итогом могли перенести на первое место, #102);
      // span группы — только по колонкам без итога; если итог есть у всех колонок, подпись — в первой
      const li = Math.max(0, C.findIndex(c => !c.sum));
      const spanAt = G => { let n = 1; while (n < (G.span || 1) && li + n < C.length && !C[li + n].sum) n++; return n; };
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
            C.map((c, i) => i === li ? '<td colspan="' + spanAt(G) + '" style="padding-left:' + (8 + level * 14) + 'px"><span class="tw">' + (col ? '▸' : '▾') + '</span>' + (G.label ? G.label(k, sub) : esc(k)) + ' <span class="mut3" style="font-weight:400">' + sub.length + '</span></td>' : (i > li && i < li + spanAt(G) ? '' : sumCell(c, sub))).join('') + '</tr>';
          if (!col) emit(sub, level + 1, p);
        });
      }
      emit(rows, 0, '');
      const foot = opts.footer === false ? '' : '<tfoot><tr>' + (opts.stripe ? '<td></td>' : '') + C.map((c, i) => i === li ? '<td>Итого <span class="mut3" style="font-weight:400">' + (opts.footCount ? esc(opts.footCount(rows)) : rows.length) + '</span></td>' : sumCell(c, rows)).join('') + '</tr></tfoot>';
      wrap.innerHTML = '<table class="k-tbl' + tw + '">' + cg + head + '<tbody>' + (body || '<tr><td colspan="' + nCols + '" class="k-empty">ничего не найдено</td></tr>') + '</tbody>' + foot + '</table>';
      wrap.querySelectorAll('th .k-flt').forEach(ic => ic.onclick = e => {
        e.stopPropagation();
        const c = opts.columns.find(x => x.key === ic.dataset.flt);
        K.colFilter(ic, K.fltItems(K.applyFilters(base, st.filters, colOf, c.key), c), st.filters[c.key] || null, v => { st.filters[c.key] = v; draw(); });
      });
      wrap.querySelectorAll('th.sortable').forEach(th => th.onclick = e => {
        if (Date.now() < noClickTill) return;
        const k = th.dataset.k, i = st.sort.findIndex(s => s[0] === k);
        if (e.shiftKey) { if (i >= 0) { if (st.sort[i][1] > 0) st.sort[i][1] = -1; else st.sort.splice(i, 1); } else st.sort.push([k, 1]); }
        else st.sort = i >= 0 && st.sort.length === 1 ? (st.sort[0][1] > 0 ? [[k, -1]] : []) : [[k, 1]];
        st.groupSort = true;
        draw();
      });
      bindLayout();
      paintCtl();
      wrap.querySelectorAll('tr.grp').forEach(tr => tr.onclick = e => { if (e.target.closest('[data-dr]')) return; const p = tr.dataset.g; const G = groups[(p.match(/\//g) || []).length - 1]; const cur = st.collapsed[p] != null ? st.collapsed[p] : !!(G && G.collapsed); st.collapsed[p] = !cur; draw(); });
      wrap.querySelectorAll('tr.row').forEach(tr => tr.onclick = e => { if (e.target.closest('[data-dr]')) return; const id = tr.dataset.id; const r = rows.find(x => String(x.id) === id); if (opts.onRow) opts.onRow(r); else if (M.pos[id]) K.card(id); });
    }
    // ---------- раскладка мышкой (#102) ----------
    const heads = () => [...wrap.querySelectorAll('th[data-k]')];
    const tableW = () => cols().reduce((s, c) => s + (lay.w[c.key] || 100), opts.stripe ? 4 : 0);  // только видимые колонки
    // первый ресайз: ширины всех колонок — как сейчас на экране, дальше раскладка фиксированная
    function ensureW() { if (!fixedW()) { lay.w = {}; heads().forEach(th => { lay.w[th.dataset.k] = Math.round(th.getBoundingClientRect().width); }); } }
    function setW(k, w) {
      lay.w[k] = Math.min(2000, Math.max(32, Math.round(w)));  // пределы — как в K.viewLayout
      const col = wrap.querySelector('col[data-k="' + CSS.escape(k) + '"]'), t = wrap.querySelector('table');
      if (col) col.style.width = lay.w[k] + 'px';
      if (t) t.style.width = tableW() + 'px';
    }
    // ширина по содержимому: самая широкая ячейка колонки (заголовок, строки, итог) без обрезки
    function fitW(k) {
      const th = heads().find(x => x.dataset.k === k); if (!th) return;
      const i = th.cellIndex, cells = [th];
      wrap.querySelectorAll('tbody tr.row, tfoot tr').forEach((tr, n) => { if (n < 2000 && tr.cells[i]) cells.push(tr.cells[i]); });
      const rg = document.createRange();
      let w = 0;
      wrap.querySelectorAll('th .k-rsz').forEach(x => { x.style.display = 'none'; });  // ручка — во всю ячейку, не в счёт
      cells.forEach(c => { rg.selectNodeContents(c); const cs = getComputedStyle(c); w = Math.max(w, rg.getBoundingClientRect().width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)); });
      ensureW(); lay.w[k] = Math.min(2000, Math.max(32, Math.ceil(w) + 4)); saveLay(); draw();
    }
    function drag(e, onMove, onUp) {
      const mv = ev => onMove(ev), up = ev => { document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up); onUp(ev); };
      document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
    }
    function bindLayout() {
      wrap.querySelectorAll('th .k-rsz').forEach(hd => {
        const k = hd.dataset.rsz;
        hd.onclick = e => e.stopPropagation();
        hd.ondblclick = e => { e.stopPropagation(); fitW(k); };
        hd.onmousedown = e => {
          if (e.button !== 0) return;
          e.preventDefault(); e.stopPropagation();
          if (!fixedW()) { ensureW(); draw(); }
          const x0 = e.clientX, w0 = lay.w[k];
          document.body.classList.add('k-col-resizing');
          drag(e, ev => setW(k, w0 + ev.clientX - x0), ev => {
            document.body.classList.remove('k-col-resizing');
            if (ev.clientX !== x0) { noClickTill = Date.now() + 300; saveLay(); }
          });
        };
      });
      // перенос: заголовок тянут больше чем на 6px — колонка встаёт туда, где отпустили (синяя черта — место)
      heads().forEach(th => th.onmousedown = e => {
        if (e.button !== 0 || e.target.closest('.k-rsz, .k-flt')) return;
        const k = th.dataset.k, x0 = e.clientX, ths = heads();
        let ind = null, to = null;
        drag(e, ev => {
          if (!ind && Math.abs(ev.clientX - x0) < 6) return;
          ev.preventDefault();
          if (!ind) { ind = h('div', 'k-col-ind'); document.body.appendChild(ind); th.classList.add('k-drag'); document.body.classList.add('k-col-moving'); }
          to = ths.findIndex(t => { const r = t.getBoundingClientRect(); return ev.clientX < r.left + r.width / 2; });
          if (to < 0) to = ths.length;
          const r = ths[Math.min(to, ths.length - 1)].getBoundingClientRect(), wr = wrap.getBoundingClientRect();
          ind.style.left = ((to < ths.length ? r.left : r.right) - 1) + 'px';
          ind.style.top = wr.top + 'px'; ind.style.height = Math.min(wr.height, innerHeight - wr.top) + 'px';
        }, () => {
          if (!ind) return;
          ind.remove(); th.classList.remove('k-drag'); document.body.classList.remove('k-col-moving');
          noClickTill = Date.now() + 300;
          const keys = ths.map(t => t.dataset.k), from = keys.indexOf(k);
          let at = to > from ? to - 1 : to;
          if (to == null || at === from) return;
          keys.splice(from, 1); keys.splice(at, 0, k);
          lay.order = keys; saveLay(); draw();
        });
      });
    }
    // ---------- CSV (#110) ----------
    // колонка с date: true — в CSV дата из ISO-значения ячейки, без подписей экрана (день недели и т. п., #161)
    function csv() {
      const C = cols().filter(c => K.htmlText(c.label)), q = K.csvLabel;  // без подписи — оформление (полоска доли)
      // «₽» в подписи — валюта отчёта: в разметке символ подменяет MutationObserver, в CSV — K.u (#105)
      const head = [].concat(...C.map(c => { const l = K.htmlText(c.label), u = K.colUnit(c); return [q(K.u(u ? l + ', ' + u : l))].concat((c.csvExtra || []).map(x => q(K.u(x)))); }));
      // числа — с обычным минусом при любой настройке формата отрицательных (#153); значения колонок csvExtra —
      // числом, если это число, иначе текстом (как дополнительные колонки ручных таблиц, K.tableCsv)
      const line = r => [].concat(...C.map(c => {
        const v = val(c, r), x = K.withNegFmt('minus', () => c.csvFmt ? c.csvFmt(v, r) : c.fmt ? c.fmt(v, r) : v);
        const parts = Array.isArray(x) ? x : [x], t = K.htmlText(parts[0]);
        return [c.date ? K.csvDateCell(v, t, c.num) : K.csvCell(t, c.num, v)].concat((c.csvExtra || []).map((_, j) => K.csvCell(K.htmlText(parts[j + 1] == null ? '' : parts[j + 1]), true)));
      })).join(',');
      return K.csvBuild(K.u(K.csvTitle(opts)), head, shown.map(line));
    }
    const csvName = () => K.csvFileName(el, opts);
    draw();
    const api = { redraw: draw, state: st, csv, csvName, setSort: s => { st.sort = s; draw(); }, setFilter: (k, v) => { st.filters[k] = v; draw(); }, setGrouped: g => { st.grouped = !!g; draw(); }, el,
      viewKey: vkey, layout: () => JSON.parse(JSON.stringify(lay)), setLayout: v => { lay = K.viewLayout(v, colKeys()); saveLay(); draw(); }, resetView: () => resetBtn.onclick() };
    if (opts.autoFilter !== false && !opts.rows) K.live(el, 'filter', draw);
    else K.live(el, 'numfmt', draw);  // формат отрицательных чисел (#153): таблица без перерисовки по фильтру
    return api;
  };

  // ---------- ручные таблицы (#131) ----------
  // Таблицы, свёрстанные строкой HTML (матрицы, плитки, короткие списки), получают то же, что K.table: «⤓ CSV» и ширину
  // колонок мышкой (граница заголовка — тянуть, двойной щелчок — по содержимому) с запоминанием в K.viewStore по
  // viewKey, «⟲ вид» — сбросить. Порядок колонок переставляет только K.table: строки ручных таблиц собраны из HTML.
  // Таблица с data-vk="ключ" (и data-title="название в CSV") получает их сама, как только попадает в страницу
  // (наблюдатель ниже); явно — K.tableTools(таблица или её контейнер, {viewKey (уникальный, латиницей), title, fileName,
  // resize: false — без ширины}) или K.tableToolsAll(контейнер). CSV — из того, что на экране (свёрнутые строки не
  // попадают): числовые ячейки (класс n) — числами, data-date="YYYY-MM-DD" у ячейки — дата без подписей, data-v —
  // ISO-значение для века дат; строка с data-csv-skip (раскрытые подробности) и колонка без заголовка и без текста
  // (полоска, спарклайн) не выгружаются; colspan — пустыми ячейками, у заголовка с colspan подписи колонок можно дать
  // в data-csv="a|b|c". Таблица внутри ячейки другой таблицы — со своей строкой кнопок прямо под ней.
  // Строка кнопок — сразу после таблицы, в том же контейнере (сетка страницы не меняется, при перерисовке контейнера
  // уходит вместе с таблицей); в прокручиваемом контейнере закреплена внизу, а закреплённая строка «Итого» стоит над
  // ней (kit.css, table.k-tooled tfoot). Ширина колонки хранится по её ключу: data-k у заголовка, иначе текст
  // заголовка без единицы и символа валюты — сменился набор колонок (матрица по валютам, сводная) или валюта
  // отчёта — ширина остаётся у своей колонки.
  const rowsOf = sec => sec ? [...sec.rows] : [];
  const cellsOf = tr => {  // ячейки строки с учётом colspan: пропуски — null
    const out = [];
    [...tr.cells].forEach(c => { out.push(c); for (let i = 1; i < (c.colSpan || 1); i++) out.push(null); });
    return out;
  };
  // подпись колонки для CSV: единица второй строкой шапки (span.k-thu, #105) — через запятую, как у K.table;
  // значок фильтра (span.k-flt, «⏷») и метка сортировки (span.si, «▲1») — оформление, в CSV не идут (#258)
  const TH_DECOR = /<span class="(?:k-flt|si)\b[^"]*"[^>]*>[\s\S]*?<\/span>/g;
  K.thText = th => {
    const x = (th.innerHTML || '').replace(TH_DECOR, ''), m = /<span class="k-thu">([\s\S]*?)<\/span>/.exec(x);
    const l = K.htmlText(x.replace(/<span class="k-thu">[\s\S]*?<\/span>/g, '')), u = m ? K.htmlText(m[1]) : '';
    return u && !l.includes(',') ? l + ', ' + u : l;
  };
  // «(1.5)» → «-1.5» в элементе с меткой data-kneg (K.accMark, #222) или k-accn: скобка может стоять и во вложенном
  // теге («<span data-kneg><b>(1.5)</b> mm</span>»); скобки без метки (текст, Bid/Ask «98.399 (0.28)») — как есть
  const KNEG_OPEN = /<[a-z][a-z0-9]*\b(?:[^>"']|"[^"]*"|'[^']*')*?(?:\sdata-kneg\b|\sclass="[^"]*\bk-accn\b[^"]*")(?:[^>"']|"[^"]*"|'[^']*')*>/gi;
  K.accMarkedToMinus = html => {
    const s = String(html == null ? '' : html), tag = /^<[^>]*>/;
    let out = '', i = 0, m;
    KNEG_OPEN.lastIndex = 0;
    while ((m = KNEG_OPEN.exec(s))) {
      let j = m.index + m[0].length, t;
      while ((t = /^\s+/.exec(s.slice(j)) || /^<[a-z][^>]*>/i.exec(s.slice(j)))) j += t[0].length;  // пробелы и вложенные открывающие теги
      if (s[j] !== '(') continue;
      let k = j + 1;
      while (k < s.length && s[k] !== ')' && s[k] !== '(') k += s[k] === '<' && (t = tag.exec(s.slice(k))) ? t[0].length : 1;
      if (s[k] !== ')' || !/^\d[\d,]*(?:\.\d+)?/.test(K.htmlText(s.slice(j + 1, k)))) continue;
      out += s.slice(i, j) + '-' + s.slice(j + 1, k); i = k + 1; KNEG_OPEN.lastIndex = i;
    }
    return out + s.slice(i);
  };
  K.accToMinus = t => String(t).replace(/^\((\d[\d,]*(?:\.\d+)?[^()]*)\)$/, '-$1').replace(/\((\d[\d,]*(?:\.\d+)?%?)\)/g, '-$1');
  K.tableCsv = (table, opts) => {
    opts = opts || {};
    const tx = c => c ? K.htmlText(c.innerHTML) : '';
    const hd = rowsOf(table.tHead), hr = hd.length ? hd[hd.length - 1] : null;
    const skip = tr => tr.hidden || (tr.dataset && tr.dataset.csvSkip) || (tr.style && tr.style.display === 'none') || (tr.cells.length === 1 && /(^|\s)k-empty(\s|$)/.test(tr.cells[0].className || ''));
    const body = [...table.tBodies].reduce((a, b) => a.concat(rowsOf(b)), []).concat(rowsOf(table.tFoot)).filter(tr => !skip(tr)).map(cellsOf);
    // подписи CSV у заголовка с data-csv="a|b": по одной на колонку экрана; лишние (больше, чем colspan) — колонки
    // CSV сразу за последней колонкой заголовка (#258: «Номинал, mm в вал.|Вал» — число и валюта по отдельности)
    let head = null;
    const extra = [];  // extra[i] — подписи колонок CSV, которые идут за колонкой экрана i
    if (hr) {
      head = [];
      [...hr.cells].forEach(c => {
        const span = c.colSpan || 1, parts = c.dataset && c.dataset.csv ? c.dataset.csv.split('|') : null;
        for (let i = 0; i < span; i++) head.push(parts ? parts[i] || '' : i ? '' : K.thText(c));
        if (parts && parts.length > span) extra[head.length - 1] = parts.slice(span);
      });
    }
    const n = Math.max(head ? head.length : 0, ...body.map(r => r.length), 0);
    const keep = [];
    for (let i = 0; i < n; i++) if ((head && head[i]) || extra[i] || body.some(r => tx(r[i]))) keep.push(i);
    // ячейка с data-csv="число|валюта" (K.csvv) — значения CSV вместо текста экрана («250mm RUB» → 250 и RUB, #258):
    // первое — в колонку ячейки (у числовой — числом, с data-date — датой), остальные — в дополнительные колонки
    // заголовка (число — числом, остальное — текстом). data-csv-n — первое значение числом и у нечисловой ячейки
    // (спарклайн «Цена 30д» на C03 → изменение цены за 30 дней, #277)
    const cellVals = (c, x) => {
      const d = (c && c.dataset) || {};
      if (d.csv == null) return [one(c)].concat(Array(x).fill(''));
      const parts = String(d.csv).split('|'), num = /(^|\s)n(\s|$)/.test(c.className || '') || d.csvN != null;
      return [d.date ? K.csvDateCell(d.date, parts[0], num) : K.csvCell(parts[0], num, d.v)].concat(Array.from({ length: x }, (_, j) => K.csvCell(parts[j + 1] || '', true)));
    };
    const one = c => {
      if (!c) return '';
      const num = /(^|\s)n(\s|$)/.test(c.className || ''), d = c.dataset || {};
      // таблица нарисована в текущем формате отрицательных (#153), перерисовать её в другом формате нельзя (это HTML
      // страницы): в режиме «(1)» число в скобках в числовой ячейке — отрицательное, как его пишет режим «−1»:
      // «(1.50 bn)» → «-1.50 bn», «94.58 (0.12%)» → «94.58 -0.12%», «(1,500.00)mm RUB» → «-1,500.00mm RUB»,
      // «(2,010)+5бп» → «-2,010+5бп», «124(1)бп» → «124-1бп» (сдвиг кривой под цифрой); CSV одинаков в обоих режимах
      // В текстовой ячейке скобки — минус только там, где их поставил формат отрицательных: в элементе с меткой
      // data-kneg (её ставит K.accMarkAll независимо от цвета, #222); «98.399 (0.28)», «(2029)», «(3 поз.)» — как есть
      const t = K.negFmt !== 'acc' ? tx(c) : num ? K.accToMinus(tx(c)) : K.htmlText(K.accMarkedToMinus(c.innerHTML));
      return d.date ? K.csvDateCell(d.date, t, num) : K.csvCell(t, num, d.v);
    };
    const cols = i => [head[i] || ''].concat(extra[i] || []);
    return K.csvBuild(K.u(K.csvTitle(opts)), head ? [].concat(...keep.map(cols)).map(l => K.csvLabel(K.u(l))) : null,
      body.map(r => [].concat(...keep.map(i => cellVals(r[i], extra[i] ? extra[i].length : 0))).join(',')));
  };
  // атрибут data-csv ячейки ручной таблицы (K.tableCsv, #258): fn() — значение или массив значений CSV; считается в
  // формате «−1» с обычным минусом, так что CSV не зависит от формата отрицательных на экране. Значения соединяются
  // через «|», поэтому «|» внутри значения разорвёт его на две колонки. Первое значение — колонка самой ячейки
  // (data-date ячейки по-прежнему делает его датой), следующие — дополнительные колонки, которые объявляет заголовок
  // (data-csv="a|b" у th: подписей больше, чем colspan). Значений больше, чем колонок у заголовка, — лишние
  // отбрасываются; меньше — пустые ячейки
  K.csvv = fn => ' data-csv="' + esc([].concat(K.withNegFmt('minus', fn)).join('|').replace(/−/g, '-')) + '"';
  K.tableTools = (t, opts) => {
    const table = t && (t.tagName === 'TABLE' ? t : t.querySelector('table'));
    if (!table) return null;
    if (table.__kt) return table.__kt;  // повторный вызов без перерисовки — та же строка кнопок и те же ручки
    const vkey = 'table:' + opts.viewKey;
    const bar = h('div', 'k-tbl-ctlbar k-tbl-ctlbar-m'), ctl = h('span', 'k-tbl-ctl'); bar.dataset.vk = vkey; bar.appendChild(ctl);
    const csv = () => K.tableCsv(table, opts), csvName = () => K.csvFileName(table, opts);
    const csvBtn = h('button', 'k-tbtn', '⤓ CSV');
    csvBtn.title = 'выгрузить в CSV (Excel): таблица как на экране; номер прогона и дата среза — в первой строке и в имени файла';
    csvBtn.onclick = e => { e.stopPropagation(); K.download(csvName(), '﻿' + csv(), 'text/csv;charset=utf-8'); };
    const resetBtn = h('button', 'k-tbtn', '⟲ вид'); resetBtn.title = 'сбросить ширину колонок этой таблицы';
    ctl.append(csvBtn, resetBtn);
    table.after(bar);
    table.classList.add('k-tooled');
    const hd = rowsOf(table.tHead), hr = hd.length ? hd[hd.length - 1] : null;
    const ths = hr ? [...hr.cells] : [];
    // ширина — по колонкам строки заголовка; заголовок в несколько строк или с colspan (матрица с группами) — без ширины
    const canW = opts.resize !== false && ths.length > 0 && hd.length === 1 && ths.every(th => (th.colSpan || 1) === 1);
    const keys = K.colKeys(ths);
    let lay = canW ? K.viewLayout(K.viewStore.get(vkey), keys) : {};
    const paint = () => { resetBtn.style.display = lay.w ? '' : 'none'; };
    const apply = () => {
      const old = table.querySelector('colgroup.k-cg'); if (old) old.remove();
      if (!lay.w) { table.classList.remove('k-fixed'); table.style.width = ''; paint(); return; }
      const cg = h('colgroup', 'k-cg');
      keys.forEach(k => { const c = document.createElement('col'); c.style.width = (lay.w[k] || 100) + 'px'; cg.appendChild(c); });
      table.insertBefore(cg, table.firstChild);
      table.classList.add('k-fixed'); table.style.width = keys.reduce((s, k) => s + (lay.w[k] || 100), 0) + 'px';
      paint();
    };
    const save = () => { K.viewStore.set(vkey, lay); paint(); };
    const reset = () => { lay = {}; K.viewStore.reset(vkey); apply(); };
    resetBtn.onclick = e => { e.stopPropagation(); reset(); };
    const api = table.__kt = { csv, csvName, viewKey: vkey, el: table, bar, layout: () => JSON.parse(JSON.stringify(lay)), resetView: reset,
      setLayout: v => { lay = canW ? K.viewLayout(v, keys) : {}; save(); apply(); } };
    if (!canW) { paint(); return api; }
    table.classList.add('k-rszable');
    let noClickTill = 0;  // после перетаскивания щелчок — не сортировка и не переход
    table.addEventListener('click', e => { if (Date.now() < noClickTill) { e.stopPropagation(); e.preventDefault(); } }, true);
    const ensureW = () => { if (!lay.w) { lay.w = {}; ths.forEach((th, i) => { lay.w[keys[i]] = Math.max(32, Math.round(th.getBoundingClientRect().width)); }); apply(); } };
    const setW = (k, w) => { lay.w[k] = Math.min(2000, Math.max(32, Math.round(w))); apply(); };  // пределы — как в K.viewLayout
    ths.forEach((th, i) => {
      const k = keys[i], hdl = h('span', 'k-rsz'); hdl.title = 'ширина: тянуть · двойной щелчок — по содержимому';
      th.appendChild(hdl);
      hdl.onclick = e => e.stopPropagation();
      hdl.ondblclick = e => {
        e.stopPropagation();
        const rg = document.createRange(), cells = [th];
        [...table.rows].forEach(tr => { const c = cellsOf(tr)[i]; if (c && c !== th && (c.colSpan || 1) === 1) cells.push(c); });
        let w = 0;
        hdl.style.display = 'none';  // ручка — во всю ячейку, не в счёт
        cells.slice(0, 2000).forEach(c => { rg.selectNodeContents(c); const cs = getComputedStyle(c); w = Math.max(w, rg.getBoundingClientRect().width + parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight)); });
        hdl.style.display = '';
        ensureW(); setW(k, Math.ceil(w) + 4); save();
      };
      hdl.onmousedown = e => {
        if (e.button !== 0) return;
        e.preventDefault(); e.stopPropagation();
        ensureW();
        const x0 = e.clientX, w0 = lay.w[k];
        document.body.classList.add('k-col-resizing');
        const mv = ev => setW(k, w0 + ev.clientX - x0), up = ev => {
          document.removeEventListener('mousemove', mv); document.removeEventListener('mouseup', up);
          document.body.classList.remove('k-col-resizing');
          if (ev.clientX !== x0) noClickTill = Date.now() + 300;
          save();
        };
        document.addEventListener('mousemove', mv); document.addEventListener('mouseup', up);
      };
    });
    apply();
    return api;
  };
  // все ручные таблицы в контейнере, размеченные data-vk="ключ" (и data-title="название в CSV") — после отрисовки
  K.tableToolsAll = root => [...root.querySelectorAll('table[data-vk]')].map(t => K.tableTools(t, { viewKey: t.dataset.vk, title: t.dataset.title }));
  // ключи колонок для ширины: data-k, иначе текст заголовка (повтор — с номером), пустой заголовок — по месту
  const SYM_RE = new RegExp('[' + Object.values(K.SYM).join('').replace(/[$]/g, '\\$') + ']', 'g');
  K.colKeys = ths => {
    const seen = {};
    return ths.map((th, i) => {
      if (th.dataset && th.dataset.k) return th.dataset.k;
      // без единицы второй строки и с «¤» вместо символа валюты: ширина не зависит от валюты отчёта (#105)
      const t = K.htmlText(th.innerHTML.replace(/<span class="k-thu">[\s\S]*?<\/span>/g, '')).replace(SYM_RE, '¤');
      if (!t) return 'c' + i;
      seen[t] = (seen[t] || 0) + 1;
      return 'h:' + t + (seen[t] > 1 ? '#' + seen[t] : '');
    });
  };
  // таблица с data-vk получает кнопки, как только попадает в страницу — даже если отрисовка забыла K.tableToolsAll;
  // таблицу убрали из страницы (смена режима: таблица → график), а строка кнопок осталась — строка уходит с ней
  const tablesIn = (node, sel) => !node || node.nodeType !== 1 ? [] : node.tagName === 'TABLE' ? [node] : node.querySelectorAll ? [...node.querySelectorAll(sel)] : [];
  K.tableToolsAuto = node => tablesIn(node, 'table[data-vk]').forEach(t => { if (!t.__kt && t.dataset && t.dataset.vk) K.tableTools(t, { viewKey: t.dataset.vk, title: t.dataset.title }); });
  K.tableToolsGone = node => tablesIn(node, 'table.k-tooled').forEach(t => { const b = t.__kt && t.__kt.bar; if (b && !t.isConnected && b.isConnected) b.remove(); });
  try {
    new MutationObserver(ms => ms.forEach(m => { m.removedNodes.forEach(K.tableToolsGone); m.addedNodes.forEach(K.tableToolsAuto); }))
      .observe(document.documentElement, { childList: true, subtree: true });
  } catch (e) { }

  // ---------- свопы и РЕПО, оценённые ядром (#201): панель C02 ----------
  // данные: p.ratesCore (extra.swaps / extra.repos / extra.index_fixings, lib/real.js), M.indexFixings (фиксинги прогона),
  // M.derivParallel() — параллельный отчёт /api/v1/runs/{id}/derivatives (ядро и отчёт по позициям, допуск, причины;
  // state 'unavailable' — сервер не ответил, 'not_computed' — прогон до блока).
  // Суммы ядра и отчёта — в рублях прогона, на экране — в валюте отчёта (как все суммы страниц); риск — p.dv01ByCurve.
  // Итоги PV — только по строкам с обеими оценками (ядро − отчёт = разница); строки «по отчёту» (ядро не оценило:
  // CCS, открытое РЕПО…) — отдельной подписью, в итоги PV и DV01 панели не входят. Перенос фиксинга в пределах
  // объявленного (неопубликованный последний день RUONIA — штатно каждое утро) — тихая метка «фикс. перенесён», не
  // «предварительно»; «предварительно» — прочие допущения (quality assumed). Разница — нейтральным цветом (это не PnL),
  // выше допуска — warn.
  // K.ratesCoreModel(list) — строки панели; K.ratesCorePanel(el) — фиксинги, итоги, таблица позиций и сделок (K.table).
  K.RC_IDX = { RUONIA: 'RUONIA', KEY_RATE: 'КС' };
  const RC_PROD = { irs: 'IRS', ois: 'OIS', repo: 'РЕПО', ccs: 'CCS' };
  K.RC_SIDE = { payer: 'плачу фикс', receiver: 'получаю фикс', repo: 'РЕПО (привлечение)', reverse_repo: 'обратное РЕПО (размещение)' };
  // причины, по которым ядро не оценило строку (backend valuation/derivatives.py NotValued; core_<код> — отказ ядра)
  const RC_REASON = { ccs_unsupported: 'CCS ядром пока не оценивается', basis_swap_unsupported: 'базисный своп (две плавающие ноги) ядром пока не оценивается',
    repo_open: 'открытое РЕПО: нет даты окончания (её задаёт владелец)', no_fixings: 'нет фиксингов индекса в прогоне', pv_not_finite: 'ядро дало нечисловую оценку',
    repo_index_unsupported: 'индекс РЕПО не поддерживается', no_key_rate_curve: 'нет кривой КС в прогоне', no_rub_curve: 'нет кривой RUONIA OIS в прогоне',
    swap_dates_mismatch: 'даты начала или окончания ног свопа не совпадают', swap_notional_mismatch: 'номиналы ног свопа не совпадают',
    terms_mixed: 'у строки сделки разных видов', not_valued: 'ядро не вернуло оценку',
    swap_frequency_unsupported: 'частота выплат не поддерживается', day_count_unsupported: 'база начисления не поддерживается', swap_index_unsupported: 'индекс не поддерживается',
    swap_ccy_unsupported: 'валюта свопа не RUB', repo_ccy_unsupported: 'валюта РЕПО не RUB', terms_incomplete: 'условия сделки неполные', terms_invalid: 'условия сделки не приняты ядром', settled: 'сделка исполнена' };
  K.rcReason = c => !c ? '' : RC_REASON[c] || (/^core_/.test(c) ? 'ядро отклонило: ' + c.slice(5) : c);
  const rcProd = s => s ? String(s).split('+').map(x => RC_PROD[x] || x).join(' + ') : '—';
  const RK = () => (M.toRepr && M.toRepr.RUB) || 1;
  const rcMoney = v => v == null || !isFinite(v) ? '<span class="mut3">—</span>' : f.mn(v * RK(), 2);
  const rcDate = s => /^\d{4}-\d{2}-\d{2}$/.test(s) ? f.date(s) : s;
  // допущение «фиксинг ещё не опубликован, перенесён последний» (backend _rates_valuation) — по-русски; null — другое
  const CARRY_RE = /^(\S+) not yet published for ([^:]+): the latest fixing of (\S+) carried(?: \(declared, at most (\d+) business days?\))?/;
  K.rcCarryText = a => {
    const m = CARRY_RE.exec(String(a || ''));
    return m ? 'фиксинг ' + (K.RC_IDX[m[1]] || m[1]) + ' за ' + m[2].split(', ').map(rcDate).join(', ') + ' ещё не опубликован: перенесён последний (' + rcDate(m[3]) + ')' +
      (m[4] ? ', объявлено, не больше ' + m[4] + ' раб. дн.' : '') : null;
  };
  K.rcAssumptionText = a => K.rcCarryText(a) || String(a);
  // метка строки: 'prov' — условия предварительные (допущения кроме переноса фиксинга), 'carry' — только перенос, null — нет
  K.rcFlag = c => !c || !c.provisional ? null : (c.assumptions || []).length && c.assumptions.every(a => CARRY_RE.test(a)) ? 'carry' : 'prov';
  K.rcProv = (flag, a) => {
    if (!flag) return '';
    const list = (a || []).map(K.rcAssumptionText);
    return flag === 'carry'
      ? '<span class="k-tag k-carry" title="' + esc('Фиксинг перенесён штатно (объявленный неопубликованный день), остальные условия — из сделки.' + (list.length ? '\n• ' + list.join('\n• ') : '')) + '">фикс. перенесён</span>'
      : '<span class="k-tag k-prov" title="' + esc('Условия предварительные (оценка ядра с допущениями): цифры не окончательные.' + (list.length ? '\n• ' + list.join('\n• ') : '')) + '">предварительно</span>';
  };
  const FLAG_TEXT = { prov: 'предварительно', carry: 'фикс. перенесён' };
  K.rcFixingsHtml = () => {
    const F = Object.values(M.indexFixings || {});
    if (!F.length) return '<div class="k-rcfix"><span class="mut">Фиксинги индексов: свопы и РЕПО прогона историю фиксингов не читали (нет начавшихся свопов и плавающих РЕПО).</span></div>';
    return F.map(x => {
      const nm = K.RC_IDX[x.id] || x.id, un = x.unpublished || [], ms = x.missing || [];
      const over = un.length && (x.maxUnpublished == null || un.length > x.maxUnpublished);  // сверх объявленного — предупреждение
      return '<div class="k-rcfix ' + (ms.length ? 'bad' : over ? 'warn' : '') + '"><b>' + esc(nm) + '</b>: последний фиксинг ' + f.date(x.latest) + ' — <b>' + f.pct(x.latestRate, 2) + '</b>' +
        (x.knownAt ? ' <span class="mut3">(на срез ' + esc(String(x.knownAt).slice(0, 16).replace('T', ' ')) + ' UTC)</span>' : '') +
        (un.length ? '<br>' + (over ? '<span class="w">⚠ ' : '<span class="mut">') + 'не опубликован за ' + un.map(f.date).join(', ') + ': перенесён фиксинг ' + f.date(x.latest) +
          (over ? ' — больше, чем объявлено' + (x.maxUnpublished != null ? ' (' + x.maxUnpublished + ' раб. дн.)' : '') : ' (штатно: объявлено, не больше ' + x.maxUnpublished + ' раб. дн.)') + '</span>' : '') +
        (ms.length ? '<br><span class="b">⚠ пропуски в истории: ' + ms.map(f.date).join(', ') + ' — своп или РЕПО, который читает эти дни, ядро не оценивает (остаётся по отчёту)</span>' : '') +
        (!un.length && !ms.length ? ' <span class="mut3">· пропусков нет</span>' : '') + '</div>';
    }).join('');
  };
  // строка отчёта без позиции в прогоне: фильтр шапки — по её книге (юрлицо, деск, книга); валюты строки не знаем —
  // при фильтре валюты показываем только рублёвые продукты (IRS, OIS, РЕПО)
  const phantomOk = r => {
    const b = M.book[r.book] || {};
    return (!filt.entity || b.entity === filt.entity) && (!filt.desk || b.desk === filt.desk) && (!filt.books || !filt.books.length || filt.books.includes(r.book)) &&
      (!filt.ccy || (filt.ccy === 'RUB' && !/ccs/.test(r.product || '')));
  };
  K.ratesCoreModel = list => {
    const L = list || K.list();
    const par = M.derivParallel ? M.derivParallel() : null, parBy = {}, prow = (par && par.rows) || [];
    prow.forEach(r => { if (r.positionId != null) parBy[r.positionId] = r; });
    const core = L.filter(p => p.ratesCore), ids = new Set(L.map(p => p.id));
    const rateProd = r => /(^|\+)(irs|ois|repo|ccs)(\+|$)/.test(r.product || '');
    const byReport = prow.filter(r => r.used === 'imported' && rateProd(r) && (M.pos[r.positionId] ? ids.has(r.positionId) : phantomOk(r)));
    const rows = core.map(p => {
      const c = p.ratesCore, pr = parBy[p.id] || {}, sides = [...new Set(c.trades.map(t => K.RC_SIDE[t.side] || t.side).filter(Boolean))];
      const diff = c.pv != null && c.reportedPv != null ? (c.pvDiff != null ? c.pvDiff : c.pv - c.reportedPv) : null;
      return { id: p.id, name: p.name, book: p.book, product: c.product || c.trades.map(t => t.product).join('+'), side: sides.join(', ') || '—',
        maturity: p.maturity, pv: c.pv, rep: c.reportedPv, diff, over: pr.over == null ? null : pr.over, ruo: p.dv01ByCurve['RUB RUONIA'] || 0, key: p.dv01ByCurve['RUB KEY'] || 0,
        flag: K.rcFlag(c), assumptions: c.assumptions || [], src: 'ядро', reason: null };
    }).concat(byReport.map(r => { const p = M.pos[r.positionId];
      return { id: r.positionId, name: p ? p.name : r.positionId, book: r.book, product: r.product, side: '—', maturity: p ? p.maturity : null, pv: null, rep: r.imported, diff: null,
        over: null, ruo: p ? p.dv01ByCurve['RUB RUONIA'] || 0 : 0, key: p ? p.dv01ByCurve['RUB KEY'] || 0 : 0, flag: null, assumptions: [], src: 'по отчёту', reason: r.reason }; }));
    rows.forEach(r => { r.marks = [r.over ? 'выше допуска' : null, FLAG_TEXT[r.flag] || null].filter(Boolean).join(', '); });
    const trades = core.flatMap(p => p.ratesCore.trades.map(t => Object.assign({ id: p.id, pos: p.name, flag: K.rcFlag(p.ratesCore), assumptions: p.ratesCore.assumptions || [] }, t)));
    trades.forEach(t => { t.marks = FLAG_TEXT[t.flag] || ''; });
    const both = rows.filter(r => r.diff != null), coreRows = rows.filter(r => r.src === 'ядро'), rep = rows.filter(r => r.src !== 'ядро');
    const S = (list, k) => list.reduce((s, r) => s + (r[k] || 0), 0);
    const totals = { pv: S(both, 'pv'), rep: S(both, 'rep'), diff: S(both, 'diff'), nBoth: both.length, nOver: rows.filter(r => r.over).length,
      nCoreOnly: coreRows.length - both.length, nRep: rep.length, repPv: S(rep, 'rep'), repRuo: S(rep, 'ruo'), repKey: S(rep, 'key'),
      ruo: S(coreRows, 'ruo'), key: S(coreRows, 'key'), nProv: rows.filter(r => r.flag === 'prov').length, nCarry: rows.filter(r => r.flag === 'carry').length };
    return { core, byReport, rows, trades, totals, par, tolerance: (par && par.tolerance) || null };
  };
  // состояние параллельного отчёта словами; '' — посчитан
  const parNote = par => !par ? 'параллельный отчёт ядро / отчёт для прогона не запрашивался: допуска и строк «по отчёту» нет'
    : par.state === 'unavailable' ? 'параллельный отчёт ядро / отчёт недоступен' + (par.status ? ' (HTTP ' + par.status + ')' : '') + ': допуска и строк «по отчёту» нет'
      : par.state === 'not_computed' ? 'параллельный отчёт ядро / отчёт для этого прогона не посчитан (прогон до него): допуска и строк «по отчёту» нет' : '';
  K.ratesCorePanel = (el, list) => {
    const R = K.ratesCoreModel(list), { core, rows, trades, totals: T } = R, tol = R.tolerance, pn = parNote(R.par);
    el.innerHTML = '';
    if (!rows.length) {
      K.noData(el, 'в прогоне нет свопов и РЕПО, оценённых ядром (extra.swaps / extra.repos): прогон до оценки свопов ядром или в нём нет условий сделок');
      if (Object.keys(M.indexFixings || {}).length) el.innerHTML = K.rcFixingsHtml() + el.innerHTML;
      return R;
    }
    const held = core;  // риск плиток — только оценённые ядром (строки «по отчёту» — подписью)
    const marks = [T.nProv ? '<span style="color:var(--warn)">предварительно ' + T.nProv + '</span>' : '', T.nCarry ? 'фикс. перенесён ' + T.nCarry : ''].filter(Boolean).join(' · ');
    const repRisk = v => v ? '<br><span class="mut3">без строк по отчёту; в них ещё ' + f.dv(v) + '</span>' : '';
    const top = h('div');
    top.innerHTML = K.rcFixingsHtml() + (pn ? '<div class="k-rcfix warn"><span class="w">⚠ ' + esc(pn) + '</span></div>' : '') + '<div class="k-rcstrip">' +
      K.kpi('Позиций', String(rows.length), 'ядром ' + core.length + (T.nRep ? ' · по отчёту ' + T.nRep : '') + (marks ? '<br>' + marks : '')) +
      K.kpi('Ядро − отчёт', rcMoney(T.diff) + K.kpiUnit('mm ₽'),
        'по ' + T.nBoth + ' поз. с обеими оценками: ядро ' + rcMoney(T.pv) + ' · отчёт ' + rcMoney(T.rep) + ' mm ₽' +
        (T.nOver ? '<br><span style="color:var(--warn)">выше допуска ' + T.nOver + '</span>' : '') +
        (T.nCoreOnly ? '<br>только ядро (отчёта нет): ' + T.nCoreOnly + ' поз.' : '') +
        (T.nRep ? '<br>ещё по отчёту: ' + T.nRep + ' поз., ' + rcMoney(T.repPv) + ' mm ₽' : '') +
        (tol ? '<br>допуск: max(' + f.n(tol.abs_rub * RK(), 0) + ' ₽, ' + f.n(tol.rel * 100, 0) + '% |отчёта|)' + (tol.provisional ? ', предварительный' : '') : ''),
        null, 'Сумма разниц по позициям, где есть и оценка ядра, и PV отчёта; это расхождение оценок, не PnL') +
      K.kpi('DV01 RUONIA', K.d({ title: 'Свопы и РЕПО (ядро) · DV01 RUONIA', unit: 'тыс ₽/бп', list: held, val: p => p.dv01ByCurve['RUB RUONIA'] }, null, '') + K.kpiUnit('тыс ₽/бп'), 'кривая «RUB RUONIA»' + repRisk(T.repRuo)) +
      K.kpi('DV01 КС', K.d({ title: 'Свопы и РЕПО (ядро) · DV01 КС', unit: 'тыс ₽/бп', list: held, val: p => p.dv01ByCurve['RUB KEY'] }, null, '') + K.kpiUnit('тыс ₽/бп'), 'кривая «RUB KEY»' + repRisk(T.repKey)) + '</div>';
    const pos = h('div'), note = h('div', 'k-note', 'Итого: «Ядро», «Отчёт» и «Ядро − отчёт» — по строкам с обеими оценками; DV01 — по строкам, оценённым ядром; строки «по отчёту» в итоги не входят.');
    const sub = h('div', 'k-rcsub', 'Сделки: условия и оценка ядром'), trd = h('div');
    el.append(top, pos, note, sub, trd);
    const bothOnly = k => r => r.pv != null && r.rep != null ? r[k] || 0 : 0;
    const coreOnly = k => r => r.src === 'ядро' ? r[k] || 0 : 0;
    const mcol = (key, label, title, sum) => ({ key, label, num: true, unit: 'mm ₽', sum: sum || true, title, fmt: v => rcMoney(v), cls: v => f.cls(v) });
    const dvcol = (key, label, title) => ({ key, label, num: true, unit: 'тыс ₽/бп', sum: coreOnly(key), title, fmt: v => f.dv(v), cls: v => f.cls(v) });
    // разница — нейтральным цветом и в режиме «(1)» (K.accMark красит скобки по знаку): явный цвет ячейки
    const diffStyle = (v, r) => r && r.over ? 'color:var(--warn);font-weight:600' : 'color:var(--fg)';
    const posCount = rs => { const nb = rs.filter(r => r.diff != null).length, nc = rs.filter(r => r.src === 'ядро').length;
      return nb === rs.length && nc === rs.length ? String(rs.length) : nb === nc ? nb + ' из ' + rs.length : 'PV ' + nb + ', DV01 ' + nc + ' из ' + rs.length; };
    K.table(pos, { viewKey: 'c02-rates-core', title: 'Свопы и РЕПО: оценка ядром и из отчёта', search: false, rows, footCount: posCount,
      columns: [
        { key: 'name', label: 'Позиция', fmt: (v, r) => esc(v) + (r && r.flag ? K.rcProv(r.flag, r.assumptions) : ''), csvFmt: v => esc(v) },
        { key: 'book', label: 'Книга' },
        { key: 'product', label: 'Продукт', fmt: v => esc(rcProd(v)) },
        { key: 'side', label: 'Сторона' },
        { key: 'maturity', label: 'Погашение', date: true, fmt: v => f.date(v) },
        mcol('pv', 'Ядро', 'PV ядра из условий сделки, mm ₽ (в валюте отчёта); итог — по строкам с обеими оценками', bothOnly('pv')),
        mcol('rep', 'Отчёт', 'PV из отчёта (mv_rub строки, иначе сумма pv_rub сделок), mm ₽; итог — по строкам с обеими оценками', bothOnly('rep')),
        { key: 'diff', label: 'Ядро − отчёт', num: true, unit: 'mm ₽', sum: true, sumNeutral: true, title: 'Разница PV ядра и отчёта (расхождение оценок, не PnL); ⚠ — выше допуска параллельного отчёта',
          style: diffStyle, fmt: (v, r) => rcMoney(v) + (r && r.over ? ' <span title="выше допуска">⚠</span>' : ''), csvFmt: v => rcMoney(v) },
        dvcol('ruo', 'DV01 RUONIA', 'Риск по кривой «RUB RUONIA», тыс ₽/бп; итог — по строкам, оценённым ядром'),
        dvcol('key', 'DV01 КС', 'Риск по кривой «RUB KEY» (форвардная кривая КС), тыс ₽/бп; итог — по строкам, оценённым ядром'),
        { key: 'src', label: 'Оценка', fmt: (v, r) => r && r.reason ? '<span class="mut" title="' + esc(r.reason) + '">по отчёту: ' + esc(K.rcReason(r.reason)) + '</span>' : esc(v || '') },
        { key: 'marks', label: 'Отметки', title: 'выше допуска; предварительно (условия с допущениями); фикс. перенесён (объявленный неопубликованный день)', fmt: v => v ? '<span class="mut">' + esc(v) + '</span>' : '' },
      ] });
    const trdCount = rs => { const nb = rs.filter(r => r.pv != null && r.reportedPv != null).length; return nb === rs.length ? String(nb) : nb + ' из ' + rs.length; };
    K.table(trd, { viewKey: 'c02-rates-trades', title: 'Свопы и РЕПО: сделки', search: false, rows: trades, footCount: trdCount,
      columns: [
        { key: 'tradeId', label: 'Сделка', fmt: (v, r) => esc(v || '—') + (r && r.flag ? K.rcProv(r.flag, r.assumptions) : ''), csvFmt: v => esc(v || '—') },
        { key: 'pos', label: 'Позиция' },
        { key: 'product', label: 'Продукт', fmt: v => esc(rcProd(v)) },
        { key: 'index', label: 'Индекс', fmt: v => esc(v ? K.RC_IDX[v] || v : 'фикс') },
        { key: 'side', label: 'Сторона', fmt: v => esc(K.RC_SIDE[v] || v || '—') },
        { key: 'notional', label: 'Номинал', num: true, unit: 'mm ₽', title: 'Номинал свопа; у РЕПО — денежная сумма первой части, mm ₽', fmt: v => rcMoney(v) },
        { key: 'fixedRate', label: 'Фикс', num: true, unit: '%', fmt: v => f.pct(v, 2) },
        { key: 'spreadBp', label: 'Спред', num: true, unit: 'б.п.', fmt: v => f.bp(v) },
        { key: 'parRate', label: 'Par', num: true, unit: '%', title: 'Par-ставка свопа по ядру, %', fmt: v => f.pct(v, 2) },
        { key: 'start', label: 'Начало', date: true, fmt: v => f.date(v) },
        { key: 'end', label: 'Окончание', date: true, fmt: v => f.date(v) },
        mcol('pv', 'Ядро', 'PV сделки по ядру, mm ₽; итог — по сделкам с обеими оценками', r => r.pv != null && r.reportedPv != null ? r.pv : 0),
        { key: 'reportedPv', label: 'Отчёт', num: true, unit: 'mm ₽', sum: r => r.pv != null && r.reportedPv != null ? r.reportedPv : 0, title: 'PV сделки из отчёта (pv_rub условий сделки), mm ₽; итог — по сделкам с обеими оценками', fmt: v => rcMoney(v), cls: v => f.cls(v) },
        { key: 'diff', label: 'Ядро − отчёт', num: true, unit: 'mm ₽', sum: true, sumNeutral: true, title: 'Разница PV ядра и отчёта по сделке, mm ₽ (расхождение оценок, не PnL)', fmt: v => rcMoney(v), style: () => 'color:var(--fg)' },
        { key: 'marks', label: 'Отметки', fmt: v => v ? '<span class="mut">' + esc(v) + '</span>' : '' },
      ] });
    return R;
  };

  // стандартные колонки позиций (концепты берут нужные по ключу)
  K.offerKindText = k => ({ put: 'пут', call: 'колл' })[k] || k || '';
  K.posCols = {
    // валюта и эмитент — обычные столбцы (R37): сортировка и фильтр по значениям, как у остальных
    issuer: { key: 'issuerName', label: 'Эмитент', filter: true, title: 'эмитент (у деривативов — контрагент)', fmt: (v, r) => '<b style="color:' + K.issuerColor(r.issuer) + '">●</b> ' + esc(v) },
    name: { key: 'name', label: 'Бумага', fmt: (v, r) => K.nameCell(r, v) + (r.tradedToday ? ' <span class="k-tag" style="border-color:var(--warn);color:var(--warn)" title="сделки сегодня">сд</span>' : '') },
    isin: { key: 'isin', label: 'ISIN', fmt: v => '<span class="mono mut">' + esc(v || '—') + '</span>' },
    ccy: { key: 'ccy', label: 'Вал', fmt: (v, r) => K.ccyTag(r.ccy), val: r => M.ccyOrder[r.ccy] ?? 99, filter: r => r.ccy, fltOrder: (a, b) => ((M.ccyOrder[a] ?? 99) - (M.ccyOrder[b] ?? 99)) || a.localeCompare(b) },
    kind: { key: 'kind', label: 'Тип', filter: r => M.kindLabel[r.kind] || r.kind, fmt: (v, r) => '<span class="tagc" style="border-color:' + K.kindColor[v] + '55">' + esc(M.kindShort[v]) + '</span>' },
    book: { key: 'book', label: 'Книга', filter: true, fmt: v => '<span class="mut">' + esc(v) + '</span>' },
    coupon: { key: 'couponFmt', label: 'Купон', fmt: v => '<span class="mono">' + esc(v || '—') + '</span>' },
    event: { key: 'event', label: 'Событие', val: r => r.event.date, fmt: (v, r) => '<span class="mut3">' + ({ put: 'пут', call: 'колл', mat: 'погаш' }[r.event.kind]) + '</span> ' + f.date(r.event.date) },
    maturity: { key: 'maturity', label: 'Погашение', fmt: v => f.date(v) },
    // дата оферты — колонкой-датой (в CSV — дата Excel), вид оферты — своей колонкой (#183)
    offer: { key: 'offer', label: 'Оферта', date: true, fmt: v => v ? f.date(v) : '<span class="mut3">—</span>' },
    offerKind: { key: 'offerKind', label: 'Вид оферты', filter: r => K.offerKindText(r.offerKind) || '—', val: r => r.offer ? r.offerKind : null, fmt: v => v ? esc(K.offerKindText(v)) : '<span class="mut3">—</span>' },
    tenor: { key: 'tenor', label: 'Срок', num: true, fmt: v => f.yrs(v), title: 'до оферты, если есть, иначе до погашения' },
    // в CSV — число в mm валюты бумаги («Номинал, mm в вал.», как в легенде единиц и на C11), валюта — в колонке «Вал» (#258)
    notional: { key: 'notional', label: 'Номинал', num: true, unit: 'mm в вал.', fmt: (v, r) => f.mn(v, 2) + '<span class="mut3">mm ' + r.ccy + '</span>', csvFmt: v => f.mn(v, 2), title: 'в валюте бумаги' },
    mv: { key: 'mv', label: 'MV, mm ₽', num: true, fmt: v => f.mn(v, 0), sum: true, unit: 'mm ₽', sumFmt: v => f.mn(v, 0) },
    price: { key: 'price', label: 'Цена', num: true, unit: '% ном.', fmt: v => f.px(v) },
    ytm: { key: 'ytm', label: 'Дох.', num: true, fmt: v => f.pct(v) },
    // в CSV — число, вид спреда (Z / DM) — своей колонкой «Вид спреда» за ним (#276)
    spread: { key: 'zspread', label: 'Спред', num: true, unit: 'б.п.', fmt: (v, r) => v == null ? '<span class="mut3">—</span>' : f.bp(v) + ' <span class="mut3">' + (r.spreadLabel || '') + '</span>', title: 'Z-спред к RUONIA (фикс) / дисконт-маржа (флоатер)',
      csvExtra: ['Вид спреда'], csvFmt: (v, r) => v == null ? ['', ''] : [f.bp(v), r.spreadLabel || ''] },
    // Z-спред к кривой дисконтирования (RUONIA) у фикс-бумаг и флоатеров (у флоатера — купоны по прогнозу): сравнимы между собой (R32)
    zdisc: { key: 'zdisc', label: 'Z к RUONIA', num: true, unit: 'б.п.', val: r => r.zSpreadZ, fmt: v => v == null ? '<span class="mut3">—</span>' : f.bp(v), title: K.zdiscTip, tip: true },
    gspread: { key: 'gspread', label: 'G-спред', num: true, unit: 'б.п.', fmt: v => f.bp(v) },
    dur: { key: 'mdur', label: 'Дюр.', num: true, unit: 'лет', fmt: v => v == null ? '—' : f.n(v, 2) },
    dv01: { key: 'dv01', label: 'DV01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп', title: K.tips.dv01, tip: true },
    cs01: { key: 'cs01', label: 'CS01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп', title: K.tips.cs01, tip: true },
    basis01: { key: 'basis01', label: 'Базис01', num: true, fmt: v => v ? f.dv(v) : '<span class="mut3">·</span>', sum: true, unit: 'тыс ₽/бп', title: K.tips.basis01, tip: true },
    ofzSpr01: { key: 'ofzSpr01', label: 'Спред к КБД01', num: true, fmt: v => v ? f.dv(v) : '<span class="mut3">·</span>', sum: true, unit: 'тыс ₽/бп', title: K.tips.ofzSpr01, tip: true },
    carry: { key: 'carryDay', label: 'Кэрри/д', num: true, fmt: v => f.k(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽', title: 'тыс ₽ в день. ' + K.tips.carry, tip: true },
    funding: { key: 'fundingDay', label: 'Фондир./д', num: true, nullable: true, fmt: v => v == null ? '<span class="mut3">—</span>' : f.k(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽', title: 'Фондирование, тыс ₽ в день. ' + K.tips.funding, tip: true },
    pnl: { key: 'pnl', label: 'PnL дня', num: true, val: r => r.pnl.total, fmt: v => f.k(v), cls: v => f.cls(v), sum: r => r.pnl.total, unit: 'тыс ₽' },
    pnlMtd: { key: 'pnlMtd', label: 'PnL MTD', num: true, fmt: v => f.mn(v, 1), cls: v => f.cls(v), sum: true, unit: 'mm ₽', sumFmt: v => f.mn(v, 1) },
    rating: { key: 'rating', label: 'Рейтинг', filter: true, fmt: v => '<span class="mut">' + esc(v) + '</span>' },
    sector: { key: 'sector', label: 'Сектор', filter: true, fmt: v => '<span class="mut">' + esc(v) + '</span>' },
  };
  K.cols = keys => keys.map(k => typeof k === 'string' ? Object.assign({}, K.posCols[k]) : k);
  // порядок эмитентов в группах: Минфин первым, деривативы последними, остальные по алфавиту
  K.issuerOrder = (a, b) => { const r = x => x === 'Минфин' ? 0 : x === 'Деривативы' ? 2 : 1; return (r(a) - r(b)) || a.localeCompare(b, 'ru'); };
  // группировка «валюта → эмитент» — по желанию (opts.groupable таблиц, R37): по умолчанию таблицы позиций плоские,
  // валюта и эмитент в них — обычные столбцы
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
    if (p.ofzSprb && p.ofzSprb.some(v => Math.abs(v) >= 1)) rows.push({ id: 'OFZSPR', label: 'спред к КБД', vals: p.ofzSprb });
    if (!rows.length) return '<div class="k-empty">нет риска ставок и спреда</div>';
    const ks = M.buckets.map((_, k) => k).filter(k => rows.some(r => Math.abs(r.vals[k]) >= 1));
    const cell = v => '<td class="n ' + (Math.abs(v) >= 1 ? f.cls(v) : '') + '">' + (Math.abs(v) >= 1 ? f.dv(v) : '<span class="mut3">·</span>') + '</td>';
    return '<div class="k-tbl-wrap k-fit"><table class="k-tbl k-compact k-crisk"><thead><tr><th>Кривая</th><th class="n">Итого</th>' + ks.map(k => '<th class="n">' + M.buckets[k] + '</th>').join('') + '</tr></thead><tbody>' +
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
  // DM флоатера в истории (R35): DM ядра — только в дни прогонов (с появления позиции), оценка по цене MOEX (dmPx,
  // пунктир) — за всю историю цены; без dmPx — как раньше (DM прогонов, иначе спред). cut — срез рядов, colors — цвета
  K.dmSeries = (H, cut, colors) => {
    const c = cut || (a => a), col = i => colors && colors[i] ? { color: colors[i] } : {};
    if (!H.dmPx) return [Object.assign({ name: 'DM, б.п.', data: c(H.dm || H.spr), connectNulls: true }, col(0))];
    return [Object.assign({ name: 'DM ядра (прогоны), б.п.', data: c(H.dm || H.dmPx.map(() => null)), connectNulls: true }, col(0)),
      Object.assign({ name: 'DM по цене (оценка), б.п.', data: c(H.dmPx), connectNulls: true, lineStyle: { type: 'dashed', width: 1.2 } }, col(1))];
  };
  // ноги FX-сделки (p.legs: extra.legs прогона или демо): даты, суммы, курс сделки и форварда, пойнты, расчёт, фиксинг
  K.legsHtml = p => {
    const L = p.legs || [], any = k => L.some(l => l[k] != null && l[k] !== '');
    const LEG = { single: '—', near: 'ближняя', far: 'дальняя' }, SET = { deliverable: 'поставка', cash: 'расчётный (NDF)' };
    const amt = (c, v) => v == null ? '—' : f.mn(v, 2) + ' <small>mm ' + esc(c || '') + '</small>', amtCsv = v => v == null ? '' : f.mn(v, 2);
    const rate = v => v == null ? '—' : f.n(v, 4), rateCsv = v => v == null ? '' : f.n(v, 4);
    const fwd = l => l.fwdRate != null ? l.fwdRate : l.forwardRate;
    const cols = [['Нога', l => esc(LEG[l.leg] || l.leg || '—')], ['Валютирование', l => l.valueDate ? f.date(l.valueDate) : '—', l => l.valueDate],
      // в CSV сумма — числом в mm валюты («Покупка, mm в вал.»), валюта — своей колонкой за ней; курс — числом,
      // котировка (CNY/RUB) — своей колонкой; у расчёта и фиксинга подписи — тоже своими колонками (#258)
      ['Покупка', l => amt(l.buyCcy, l.buyAmount), null, ['Покупка, mm в вал.', 'Вал покупки'], l => [amtCsv(l.buyAmount), l.buyCcy || '']],
      ['Продажа', l => amt(l.sellCcy, l.sellAmount), null, ['Продажа, mm в вал.', 'Вал продажи'], l => [amtCsv(l.sellAmount), l.sellCcy || '']],
      ['Курс сделки', l => rate(l.contractRate) + (l.rateQuote ? ' <small>' + esc(l.rateQuote) + '</small>' : ''), null, ['Курс сделки', 'Котировка'], l => [rateCsv(l.contractRate), l.rateQuote || '']]]
      .concat(L.some(l => fwd(l) != null) ? [['Форвард', l => rate(fwd(l))]] : [])
      .concat(any('points') ? [['Пойнты', l => l.points == null ? '—' : f.s(l.points, 4)]] : [])
      .concat(any('settlement') ? [['Расчёт', l => esc(SET[l.settlement] || l.settlement || '—') + (l.settlementCcy ? ' <small>в ' + esc(l.settlementCcy) + '</small>' : ''), null,
        ['Расчёт', 'Вал расчёта'], l => [SET[l.settlement] || l.settlement || '', l.settlementCcy || '']]] : [])
      .concat(any('fixingDate') ? [['Фиксинг', l => l.fixingDate ? f.date(l.fixingDate) + (l.fixingSource ? ' <small>' + esc(l.fixingSource) + '</small>' : '') + (l.fixingRate != null ? ' <small>' + rate(l.fixingRate) + '</small>' : '') : '—', l => l.fixingDate,
        ['Фиксинг', 'Источник фиксинга', 'Курс фиксинга'], l => [l.fixingDate ? f.date(l.fixingDate) : '', l.fixingSource || '', rateCsv(l.fixingRate)]]] : [])
      .concat(any('paymentDate') ? [['Выплата', l => l.paymentDate ? f.date(l.paymentDate) : '—', l => l.paymentDate]] : []);
    return '<div class="k-tbl-wrap"><table class="k-tbl k-compact k-legs"><thead><tr>' + cols.map(c => '<th' + (c[0] === 'Нога' || c[0] === 'Расчёт' ? '' : ' class="n"') + (c[3] ? ' data-csv="' + esc(c[3].join('|')) + '"' : '') + '>' + c[0] + '</th>').join('') + '</tr></thead><tbody>' +
      L.map(l => '<tr>' + cols.map(c => '<td' + (c[0] === 'Нога' || c[0] === 'Расчёт' ? '' : ' class="n"') + (c[2] && c[2](l) ? ' data-date="' + esc(c[2](l)) + '"' : '') + (c[4] ? K.csvv(() => c[4](l)) : '') + '>' + c[1](l) + '</td>').join('') + '</tr>').join('') + '</tbody></table></div>';
  };

  // ---------- карточка позиции ----------
  // ---------- фондирование (B1): строка карточки позиции и панель кокпита ----------
  // M.funding — фондирование прогона (real.js: D.fundingStatus; mock.js: демо); p.fundingInfo — extra.funding позиции.
  // Не настроено / не посчитано — так и пишется, а не 0.
  K.fundingStateText = { computed: 'посчитано', not_configured: 'не настроено', invalid_config: 'ошибка настроек', failed: 'ошибка расчёта', not_computed: 'не считалось', demo: 'демо' };
  const fromRub = v => v == null ? null : v / (M.fx[K.repr] || 1);
  // откуда ставка строки (#237): правило адаптера → правило настроек → рыночная ставка по умолчанию
  K.fundingRuleSource = { adapter: 'правило адаптера', settings: 'правило настроек', settings_override: 'правило настроек (главнее адаптера)', default: 'рыночная ставка по умолчанию', contract: 'ставка договора' };
  const ruleText = l => l && l.rule_source && l.rule_source !== 'default' ? ' · <small class="mut">' + esc(K.fundingRuleSource[l.rule_source] || l.rule_source) + (l.rule_id ? ' ' + esc(l.rule_id) : '') + '</small>'
    : l && l.rule_source === 'default' ? ' <small style="color:var(--warn)" title="не трансфертная ставка: правил фондирования для позиции нет (настройки → Фондирование)">⚑</small>' : '';
  // фондирование позиции за день (#168): отдельно от кэрри; null — не посчитано («—», а не 0)
  K.fundingDayHtml = p => p.fundingDay == null ? '<span class="mut3">—</span>' : '<span class="' + f.cls(p.fundingDay) + '">' + f.k(p.fundingDay) + '</span>';
  K.fundingLine = function (p) {
    const F = M.funding || {}, x = p.fundingInfo;
    if (!x && !F.state) return '';
    const st = x ? x.state : F.state;
    let t;
    if (st === 'funded') {
      const l = (x.lines || [])[0] || {};
      t = '<b class="' + f.cls(x.amount_rub) + '">' + f.ks(fromRub(x.amount_rub)) + '</b> тыс ₽ · ' + esc(l.rate_expr || '') + ruleText(l) + (l.days ? ' · ' + l.days + ' дн.' : '') + ' · ' + (x.in_pnl ? 'в PnL позиции строкой «Фондирование» (не в кэрри)' : 'строкой книги (у позиции нет PnL)');
    } else if (st === 'deferred' || st === 'not_funded') t = '0 · ' + esc(x.reason || '');
    else if (st === 'problem') t = '<span style="color:var(--warn)">не посчитано: ' + esc(x.reason || '') + '</span>';
    else if (st === 'demo') t = 'демо: в PnL строкой «Фондирование» по ставке ' + (p.fundRate != null ? f.pct(p.fundRate) : '—');
    else t = '<span style="color:var(--warn)">' + esc(K.fundingStateText[st] || st) + (F.text ? ' — ' + esc(F.text) : '') + '</span>';
    return '<div class="k-note" style="margin-top:6px">Фондирование за период: ' + t + '</div>';
  };
  K.fundingPanel = function (el) {
    const F = M.funding || { state: 'not_computed', text: 'нет данных о фондировании' };
    const head = '<div class="k-note" style="padding:8px 10px">' + (F.state === 'computed' || F.state === 'demo'
      ? (F.period ? 'период ' + f.dshort(F.period.start) + ' → ' + f.dshort(F.period.end) + ' (' + F.period.days + ' дн.) · ' : '') + 'итог <b class="' + f.cls(F.total) + '">' + f.mns(F.total, 2) + '</b> mm ₽, в PnL позиций ' + f.mns(F.inPositions, 2) + ', строками книг ' + f.mns(F.bookLevel, 2) + (F.provisional && F.provisional.length ? ' · по умолчанию (не решено): ' + esc(F.provisional.join(', ')) : '')
      : '<span style="color:var(--warn)">Фондирование ' + esc(K.fundingStateText[F.state] || F.state) + (F.text ? ': ' + esc(F.text) : '') + '</span> — в PnL его нет, а не 0') + '</div>';
    const DR = F.defaultRate, drc = DR ? Object.entries(DR.by_ccy || {}).filter(([, v]) => v.expr) : [];
    const dflt = DR ? '<div class="k-note" style="padding:0 10px 8px">Рыночная ставка по умолчанию (не трансфертная; где нет правил адаптера и настроек): ' + (drc.length ? drc.map(([c, v]) => esc(c) + ' — ' + esc(String(v.expr).replace(' (' + (DR.label || '') + ')', ''))).join(', ') : 'выключена') + (DR.lines ? ' · строк по ней ' + DR.lines + (DR.positions ? ', позиций ' + DR.positions : '') : '') + ' · <a href="../settings.html#funding">настройки</a></div>' : '';
    const books = Object.entries(F.byBook || {}).sort((a, b) => (a[1].total || 0) - (b[1].total || 0));
    const wo = w => !w ? '—' : w.periodicity == null ? '<span style="color:var(--warn)">не задано</span>' : esc(w.periodicity) + (w.last ? ' · ' + f.dshort(w.last) + ': ' + f.mns(w.since_write_off, 2) : '');
    const tbl = books.length ? '<div class="k-tbl-wrap"><table class="k-tbl k-compact"><thead><tr><th>Книга</th><th>Юрлицо</th><th>База</th><th class="n">Итого, mm ₽</th><th class="n">в PnL позиций</th><th class="n">строками книги</th><th>Списание · с него</th><th class="n">Проблем</th></tr></thead><tbody>' +
      books.map(([b, v]) => '<tr><td>' + esc((M.book[b] || {}).name || b) + '</td><td class="mut">' + esc((M.book[b] || {}).entity || '—') + '</td><td class="mut">' + esc(v.base || '—') + '</td><td class="n ' + f.cls(v.total) + '">' + f.mns(v.total, 2) + '</td><td class="n">' + f.mns(v.inPositions, 2) + '</td><td class="n">' + f.mns(v.bookLevel, 2) + '</td><td class="mut">' + wo(v.writeOff) + '</td><td class="n">' + (v.problems || '') + '</td></tr>').join('') + '</tbody></table></div>' : '';
    const ents = Object.entries(F.byEntity || {});
    const ent = ents.length > 1 ? '<div class="k-note" style="padding:6px 10px">по юрлицам: ' + ents.map(([e, v]) => esc(e) + ' ' + f.mns(v, 2)).join(' · ') + '</div>' : '';
    const pr = F.problems || [];
    const probs = pr.length ? '<div class="k-sec" style="margin:10px 10px 4px">Проблемы фондирования · ' + pr.length + '</div><ul class="k-note" style="margin:0 0 8px;padding-left:28px">' + pr.slice(0, 30).map(x => '<li>' + esc(x.kind + ' ' + x.ref + ': ' + x.reason) + '</li>').join('') + (pr.length > 30 ? '<li>ещё ' + (pr.length - 30) + '</li>' : '') + '</ul>' : '';
    el.innerHTML = head + dflt + tbl + ent + probs;
    K.tableTools(el, { viewKey: 'funding-books', title: 'Фондирование по книгам, mm ₽' });
  };

  // ---------- история позиции по прогонам: на чём стоит точка (R44; api/concepts.positions_history) ----------
  // H.runAt[i] — прогон даты (null: прогона нет), H.pt[i] — [вид, текст] точки не по ядру, H.qty — бумаг по прогонам
  const PT_LABEL = { old: 'прошлая версия программы', unit: 'другая единица риска', method: 'другой метод оценки', unvalued: 'не оценена', gap: 'нет прогона за дату', minor: 'замечание', before: 'до истории прогонов' };
  // r.unit (#171): риск валютных кривых прогона в другой единице, чем у показанного (настройка quote_curve_risk) — для DV01
  // позиции с риском на валютной кривой точка как у прошлой версии (пунктир); у рублёвой позиции единица та же
  const foreignRisk = H => Object.keys(H.curves || {}).some(c => !/^RUB /.test(c));
  K.histPoint = (H, i, mode) => {
    const RA = H.runAt; if (!RA) return null;
    if (H._first == null) H._first = RA.findIndex(Boolean);
    const r = RA[i], pt = H.pt && H.pt[i];
    if (!r) return i < H._first || H._first < 0 ? { k: 'before', t: 'прогонов до этой даты нет' } : { k: 'gap', t: 'прогона за дату нет — значения риска за неё нет (не переносим с прошлого прогона)' };
    const notes = [];
    // тип среза — словарём шапки (CUT_TYPE, #174); marketCutType утреннего прогона positions_history не отдаёт
    if (r.cut) notes.push(r.cut === 'eod_preliminary' ? 'предварительное закрытие (финального среза за дату нет)' : CUT_TYPE[r.cut] || 'срез ' + r.cut);
    if (pt && pt[0] !== 'minor') return { k: pt[0], t: pt[1] + (r.old ? '; прогон версии ' + r.old : '') };
    if (pt && pt[1]) notes.push(pt[1]);
    if (r.old) return { k: 'old', t: 'посчитано версией ' + r.old + ' — пересчитается историей прогонов' + (notes.length ? '; ' + notes.join('; ') : '') };
    if (r.unit && (mode == null || mode === 'dv01') && foreignRisk(H)) return { k: 'unit', t: r.unit + ' — DV01 через эту дату сравнивать с поправкой' + (notes.length ? '; ' + notes.join('; ') : '') };
    return notes.length ? { k: 'minor', t: notes.join('; ') } : null;
  };
  // ряды DV01 / номинала / DM позиции: { series, tooltip, note }. opt.perBond — DV01 на 1 бумагу (₽/бп): видно, что
  // изменилось — размер позиции или риск на бумагу
  K.runSeries = function (H, mode, opt) {
    opt = opt || {};
    const xs = H.dates, RA = H.runAt, per = mode === 'dv01' && opt.perBond && H.qty;
    const raw = (mode === 'dm' ? H.dm : mode === 'dv01' ? H.dv01 : H.notional) || [];
    const name = mode === 'dm' ? 'DM, б.п.' : mode === 'notional' ? 'Номинал, mm ' + (opt.ccy || 'в валюте бумаги') : per ? 'DV01, ₽/бп на 1 бумагу' : 'DV01, тыс ₽/бп';
    const val = i => { const v = raw[i]; if (v == null) return RA && RA[i] && mode !== 'dm' ? 0 : null; if (per) { const q = H.qty[i]; return q ? v / q : null; } return mode === 'dv01' ? v / 1e3 : mode === 'notional' ? v / 1e6 : v; };
    const P = xs.map((_, i) => K.histPoint(H, i, mode)), V = xs.map((_, i) => val(i));
    const main = V.map((v, i) => !P[i] || P[i].k === 'minor' ? v : null), old = V.map((v, i) => P[i] && P[i].k === 'old' ? v : null);
    const unit = V.map((v, i) => P[i] && P[i].k === 'unit' ? v : null);
    const iso = a => a.map((v, i) => v != null && a[i - 1] == null && a[i + 1] == null ? [i, v] : null).filter(Boolean);  // одиночная точка линией не видна
    const pts = k => V.map((v, i) => P[i] && P[i].k === k && v != null ? [i, v] : null).filter(Boolean);
    const step = mode === 'dm' ? false : 'end', warn = css('--warn') || '#e3b341', c0 = opt.color || css('--accent');
    const series = [{ type: 'line', name, data: main, step, connectNulls: mode === 'dm', showSymbol: false, lineStyle: { width: 1.6 }, color: c0 }]
      .concat(iso(main).length ? [{ type: 'scatter', name, data: iso(main), symbolSize: 4, color: c0 }] : [])
      .concat(old.some(v => v != null) ? [{ type: 'line', name: 'прошлая версия расчёта', data: old, step, connectNulls: mode === 'dm', showSymbol: false, lineStyle: { width: 1.2, type: 'dashed' }, color: css('--fg-3') }] : [])
      .concat(iso(old).length ? [{ type: 'scatter', name: 'прошлая версия расчёта', data: iso(old), symbolSize: 4, color: css('--fg-3') }] : [])
      .concat(unit.some(v => v != null) ? [{ type: 'line', name: 'другая единица риска', data: unit, step, showSymbol: false, lineStyle: { width: 1.2, type: 'dashed' }, color: warn }] : [])
      .concat(iso(unit).length ? [{ type: 'scatter', name: 'другая единица риска', data: iso(unit), symbolSize: 4, color: warn }] : [])
      .concat(pts('method').length ? [{ type: 'scatter', name: 'другой метод оценки', data: pts('method'), symbol: 'diamond', symbolSize: 7, color: warn }] : []);
    // замечания (допущение ядра, позиции книги из прошлого отчёта, предварительное закрытие) — в подсказке, без маркера
    const at = Object.fromEntries(xs.map((d, i) => [d, i]));
    const fmt = v => v == null ? '—' : mode === 'dm' ? f.n(v, 1) : per ? f.n(v, 3) : f.n(v, mode === 'dv01' ? 3 : 2);
    const tooltip = { trigger: 'axis', formatter: ps => {
      const i = at[(ps[0] || {}).axisValue]; if (i == null) return '';
      const q = H.qty && H.qty[i], q0 = H.qty && i > 0 ? H.qty[i - 1] : null, pt = P[i];
      return '<b>' + esc(f.date(xs[i])) + '</b><br>' + esc(name) + ': <b>' + fmt(pt && (pt.k === 'gap' || pt.k === 'unvalued' || pt.k === 'before') ? null : V[i]) + '</b>' +
        (q != null ? '<br><span style="opacity:.75">бумаг: ' + f.n(q) + (q0 != null && Math.abs(q - q0) > 1e-6 ? ' · ' + f.s(q - q0) + ' к ' + esc(f.date(xs[i - 1])) : '') + '</span>' : '') +
        (pt && pt.k !== 'before' ? '<br><span style="color:' + warn + '">' + esc(PT_LABEL[pt.k]) + ': ' + esc(pt.t) + '</span>' : '');
    } };
    // покрытие ряда: с первой точки, где позиция есть
    const from = V.findIndex((v, i) => (v != null && v !== 0) || (P[i] && P[i].k === 'unvalued'));
    const cnt = { clean: 0, old: 0, unit: 0, method: 0, unvalued: 0, gap: 0 };
    if (from >= 0) for (let i = from; i < xs.length; i++) { const k = P[i] ? P[i].k : 'clean'; if (k === 'minor') cnt.clean++; else if (k === 'gap') { if (raw[i] != null) cnt.gap++; } else if (k in cnt) cnt[k]++; }
    const note = !RA ? '' : 'Точки ряда: ' + cnt.clean + ' — ядро на прогоне своей даты' + (cnt.old ? ' · ' + cnt.old + ' — прошлой версией программы (пунктир)' : '') +
      (cnt.unit ? ' · ' + cnt.unit + ' — с риском валютных кривых в другой единице (пунктир; настройка quote_curve_risk)' : '') +
      (cnt.method ? ' · ' + cnt.method + ' — другим методом (◆, причина в подсказке)' : '') + (cnt.unvalued ? ' · ' + cnt.unvalued + ' — позиция не оценена (разрыв)' : '') +
      (cnt.gap ? ' · ' + cnt.gap + ' — прогона за дату нет (разрыв)' : '') + (cnt.old || cnt.gap ? '. Досчитать: ' + K.noteHtml(BF) : '') +
      (mode === 'dv01' && H.qty ? '. Тренд риска на бумагу и размер позиции различает «на 1 бумагу»; число бумаг и его изменение — в подсказке' : '');
    return { series, tooltip, note };
  };

  // таблицы карточки позиции (#184): ноги сделки, риск по кривым и срокам, потоки — «⤓ CSV» и ширина колонок, как у
  // ручных таблиц мониторов (K.tableTools). Ключ раскладки — по виду таблицы (ширина общая для всех позиций, в полной
  // карточке и в карточке C03), имя файла — с ISIN позиции (иначе с её номером), название в CSV — с именем позиции
  K.CARD_TABLES = [['table.k-legs', 'card-legs', 'Ноги сделки'], ['table.k-crisk', 'card-curve-risk', 'Риск по кривым и срокам, тыс ₽/бп'], ['table.k-cflows', 'card-flows', 'Потоки']];
  K.cardTableTools = (root, p) => K.CARD_TABLES.forEach(([sel, key, title]) => {
    const t = root && root.querySelector && root.querySelector(sel);
    if (t) K.tableTools(t, { viewKey: key, title: title + ': ' + K.htmlText(p.name), fileName: key + '_' + (p.isin || p.id) });
  });
  // ссылка на прогнозное кэрри позиции (C17, #66): тот же прогон (run_id / as_of / demo из адреса)
  K.carryLink = id => { const q = new URLSearchParams(); const u = new URLSearchParams(location.search); ['run_id', 'as_of', 'demo'].forEach(k => { if (u.get(k)) q.set(k, u.get(k)); }); q.set('pos', id); return 'c17-forward-carry.html?' + q.toString(); };
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
      ['Базис ОФЗ01', p.basis01 ? f.dv(p.basis01) + ' <small>тыс ₽/бп</small>' : '—', false, K.tips.basis01],
      ['Спред к КБД01', p.ofzSpr01 ? f.dv(p.ofzSpr01) + ' <small>тыс ₽/бп</small>' : '—', false, K.tips.ofzSpr01], ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽ · <a href="' + K.carryLink(p.id) + '" title="C17: кэрри, фондирование и нетто позиции до горизонта">прогноз →</a></small>', false, K.tips.carry],
      ['Фондирование / день', K.fundingDayHtml(p) + ' <small>тыс ₽ · ставка ' + f.pct(p.fundRate) + '</small>', false, K.tips.funding + K.tips.fundRate],
      ['НКД', f.mn(p.accrued, 2) + ' <small>mm ₽</small>'], ['Рейтинг / сектор', '<span title="' + esc(K.ratingTip(p.rating, p.ratingInfo)) + '">' + esc(K.ratingText(p.rating, p.ratingInfo)) + '</span> <small>' + esc(p.sector) + '</small>'],
    ] : [
      ['Номинал', f.mn(p.notional, 0) + ' <small>mm ' + p.ccy + '</small>'], ['MTM', f.mn(p.mv, 1) + ' <small>mm ₽</small>'],
      ['Ставка / курс', esc(p.couponFmt)], ['Срок', f.date(p.maturity) + ' <small>' + f.yrs(p.tenor) + '</small>'],
      ['DV01', f.dv(p.dv01) + ' <small>тыс ₽/бп</small>', false, K.tips.dv01], ['FX дельта', K.fxDeltaHtml(p), Object.values(INSTR.fxBy(p)).filter(v => v).length > 1],
      ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽</small>', false, K.tips.carry],
      ['Фондирование / день', K.fundingDayHtml(p) + ' <small>тыс ₽</small>', false, K.tips.funding], ['Контрагент', esc(p.cpty || '—')],
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
      '<div class="k-fields">' + [['Итого день', pn.total], ['Кэрри', pn.carry, K.tips.carry], ['Фондирование', pn.funding, K.tips.funding], ['Ставки', pn.rates], ['Спред', pn.spread], ['FX', pn.fx], ['Сделки', pn.trading], ['Прочее', pn.other], ['MTD', p.pnlMtd], ['YTD', p.pnlYtd]].map(([l, v, tip]) => '<div><div class="l">' + K.hint(l, tip) + '</div><div class="v ' + f.cls(v) + '">' + (v == null ? '<span class="mut3">—</span>' : Math.abs(v) >= 1e7 ? f.mns(v, 1) + ' <small>mm ₽</small>' : f.ks(v) + ' <small>тыс ₽</small>') + '</div></div>').join('') + '</div>' + K.fundingLine(p) +
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
      const R = mode === 'dv01' || mode === 'notional' || (mode === 'spread' && isFloat && H.dm) ? K.runSeries(H, mode === 'spread' ? 'dm' : mode, { ccy: p.ccy, color: K.issuerColor(p.issuer), perBond: unit === 'bond' }) : null;
      if (R && mode === 'spread' && H.dmPx) {  // R35: DM ядра по прогонам (R44) и оценка по цене за всю историю пунктиром
        R.series.forEach(x => { if (x.name === 'DM, б.п.') x.name = 'DM ядра (прогоны), б.п.'; });
        R.series.push({ type: 'line', name: 'DM по цене (оценка), б.п.', data: H.dmPx, connectNulls: true, showSymbol: false, lineStyle: { type: 'dashed', width: 1.2 }, color: css('--fg-3') });
        const tf = R.tooltip.formatter, at = Object.fromEntries(H.dates.map((d, i) => [d, i]));
        R.tooltip.formatter = ps => { const i = at[(ps[0] || {}).axisValue], v = i == null ? null : H.dmPx[i]; return tf(ps) + (v != null ? '<br>DM по цене (оценка): <b>' + f.n(v, 1) + '</b>' : ''); };
      }
      hnote.innerHTML = R ? R.note + (isFloat && mode === 'dv01' ? '<div style="margin-top:3px">' + esc(K.tips.floaterDv01) + '</div>' : '')
        + (isFloat && mode === 'spread' && H.dmNote ? '<div style="margin-top:3px">' + esc(H.dmNote) + '</div>' : '') : '';  // #33: почему DM нет
      hunit.style.display = mode === 'dv01' && perBondOk ? '' : 'none';
      const S = R ? R.series : mode === 'price' ? [{ name: 'Цена', data: H.price }]
        : mode === 'yield' ? [{ name: 'Доходность', data: H.ytm }, { name: 'ОФЗ на сроке', data: H.bench, lineStyle: { type: 'dashed', width: 1 } }]
          : mode === 'spread' ? (isFloat ? K.dmSeries(H)
            : [{ name: 'G-спред, б.п.', data: H.spr }].concat(H.zspr ? [{ name: 'Z-спред MOEX, б.п.', data: H.zspr }] : []))
            : mode === 'infl' ? [{ name: 'BEI, %', data: H.bei || [] }, { name: 'IR (реальная), %', data: H.ir || [] }].concat(H.cpi ? [{ name: 'ИПЦ г/г, %', data: H.cpi, step: 'end', lineStyle: { type: 'dashed', width: 1.2 } }] : [])
              : mode === 'dv01' ? [{ name: 'DV01, тыс ₽/бп', data: H.dv01.map(v => v / 1e3), step: 'end' }] : [{ name: 'Номинал, mm ' + p.ccy, data: H.notional.map(v => v / 1e6), step: 'end', areaStyle: { opacity: .15 } }];
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
      xAxis: { type: 'category', data: M.buckets }, yAxis: { type: 'value', unit: 'тыс ₽/бп', axisLabel: { formatter: v => f.dv(v) } },
      series: cids.map(id => ({ name: M.curve[id].short, type: 'bar', stack: 'a', data: p.risk[id], itemStyle: { color: K.curveColors[id] } })).concat(p.cs01 ? [{ name: 'CS01', type: 'bar', data: p.cs01b, itemStyle: { color: K.curveColors.CS01 } }] : []),
    }));
    o.charts.push(rc);
    K.cardTableTools(o.bd, p);
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
  // K.flows(el, p): сразу — потоки набора данных, затем, когда сервер ответит, — весь график; графика нет — потоки
  // прогона и причина словами, сбой запроса — сообщение в карточке (M.posFlows: {flows, reason, error}, #288)
  K.flows = function (el, p) {
    const own = () => M.cashflows.filter(c => c.pos === p.id);
    el.innerHTML = K.flowsTable(p, own(), !!M.posFlows);
    K.cardTableTools(el, p);
    if (M.posFlows) M.posFlows(p.id).then(x => {
      if (!el.isConnected || el.dataset.pos !== p.id) return;
      x = x || {};
      el.innerHTML = x.flows ? K.flowsTable(p, x.flows) : K.flowsTable(p, own(), false, x.error || x.reason || null);
      K.cardTableTools(el, p);
    });
    el.dataset.pos = p.id;
  };
  // why — почему нет всего графика выпуска (показаны потоки прогона или их тоже нет)
  K.flowsTable = function (p, cf, loading, why) {
    const offer = p.offer || p.yieldDate || null;
    const KIND = { cpn: 'купон', amort: 'амортизация', mat: 'погашение', put: 'пут-оферта', call: 'колл' };
    if (!cf.length) return '<div class="k-empty">' + (loading ? 'загрузка потоков…' : 'потоков нет' + (why ? ': ' + esc(why) : '')) + '</div>';
    const sum = cf.reduce((s, c) => s + (c.kind === 'put' || c.kind === 'call' || c.amountRub == null ? 0 : c.amountRub), 0);
    return (why ? '<div class="mut" style="font-size:11px;margin-bottom:4px">Весь график выпуска недоступен: ' + esc(why) + '. Показаны потоки прогона.</div>' : '') +
      '<div class="mut3" style="font-size:11px;margin-bottom:4px">' + (loading ? 'загрузка всех потоков… · ' : '') + cf.length + ' потоков до ' + f.date(cf[cf.length - 1].date) +
      (offer ? ' · после оферты ' + f.date(offer) + ' — бледнее' : '') + ' · оферта — номинал к предъявлению, в сумму не входит</div>' +
      '<div style="max-height:360px;overflow:auto"><table class="k-tbl k-cflows"><thead><tr><th>Дата</th><th>Тип</th><th class="n">Сумма, ' + p.ccy + '</th><th class="n">≈ mm ₽</th></tr></thead><tbody>' +
      cf.map(c => '<tr' + (offer && c.date > offer ? ' style="opacity:.55"' : '') + '><td data-date="' + esc(c.date) + '">' + f.date(c.date) + '</td><td>' + (c.amort ? 'амортизация' : KIND[c.kind] || c.kind) + (c.est ? ' <span class="mut3">прогноз</span>' : '') + '</td><td class="n">' + f.n(c.amount, 0) + '</td><td class="n">' + f.mn(c.amountRub, 1) + '</td></tr>').join('') +
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
      const num = (v, spec, bold) => Math.abs(v) < 500 ? '<span class="mut3">·</span>' : (bold ? '<b>' : '') + K.d(spec, null, '') + (bold ? '</b>' : '');
      let html = '<table class="k-tbl k-compact"><thead><tr><th>Валюта / тип</th><th class="n" title="' + esc(K.tips.dv01) + '"><span class="k-hint">DV01</span></th>' +
        curCols.map(c => '<th class="n" title="' + esc(K.curveTip(c.id) || c.name) + '">' + (ccys.length > 1 ? '<span class="mut3">' + c.ccy + '</span> ' : '') + esc(c.short) + '</th>').join('') +
        '<th class="n" title="' + esc(K.tips.cs01) + '"><span class="k-hint">CS01</span></th><th class="n">MV, mm ₽</th></tr></thead><tbody>';
      // foot — строка «Итого» (липкая, #210): без полупрозрачного фона тепловой карты, иначе сквозь неё видны строки;
      // метка K.HEAT_PLAIN без фона — красный в ней того же оттенка, что в ячейках карты (#265)
      const row = (label, list, lvl, color, ccy, foot) => {
        const dv = M.sum(list, 'dv01'), cs = M.sum(list, 'cs01'), mv = M.sum(list, p => p.group === 'DER' ? 0 : p.mv);
        const heat = mode === 'heat' && !foot, cellStyle = v => mode === 'heat' ? (foot ? K.HEAT_PLAIN : K.heatRisk(v, allMax)) : mode === 'sign' ? 'color:' + (v < 0 ? 'var(--accent)' : 'var(--warn)') : '';
        const rowBg = mode === 'type' ? (color ? tint(color, 0.10) : ccy ? tint(K.ccyColor(ccy), 0.16) : '') : '';
        const lab = mode === 'type' && color ? '<span style="color:' + color + '">' + label + '</span>' : (color && mode !== 'type' ? '<b style="color:' + color + '">●</b> ' : '') + (mode === 'type' && color ? '' : label);
        return '<tr class="' + (lvl === 0 ? 'grp' : '') + '"' + (rowBg ? ' style="background:' + rowBg + '"' : '') + '><td style="padding-left:' + (6 + lvl * 12) + 'px">' + lab + ' <span class="mut3">' + list.length + '</span></td>' +
          '<td class="n" style="' + cellStyle(dv) + '">' + num(dv, { title: label.replace(/<[^>]+>/g, '') + ' · DV01', unit: 'тыс ₽/бп', list, val: 'dv01' }, true) + '</td>' +
          curCols.map(c => { const v = M.sum(list, p => p.dv01ByCurve[c.id]); return '<td class="n" style="' + cellStyle(v) + '">' + num(v, { title: label.replace(/<[^>]+>/g, '') + ' · ' + c.short, unit: 'тыс ₽/бп', list, val: p => p.dv01ByCurve[c.id] }) + '</td>'; }).join('') +
          '<td class="n" style="' + (heat ? K.heatMono(cs, allMax, '227,179,65') : cellStyle(cs)) + '">' + (cs ? num(cs, { title: label.replace(/<[^>]+>/g, '') + ' · CS01', unit: 'тыс ₽/бп', list, val: 'cs01' }) : '<span class="mut3">·</span>') + '</td>' +
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
      html += '</tbody><tfoot>' + row('Итого', L, 0, null, null, true).replace('<tr class="grp"', '<tr') + '</tfoot></table>';
      box.innerHTML = html;
      K.tableTools(box, { viewKey: opt.viewKey || 'risk-matrix', title: 'Матрица рисков, тыс ₽/бп' });
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
      // итоги тепловой карты — без фона, с меткой K.HEAT_PLAIN: красный того же оттенка, что в ячейках (#265)
      const tn = mode === 'heat' ? '<td class="n" style="' + K.HEAT_PLAIN + '">' : '<td class="n">';
      const cell = (v, title, rowsFn) => '<td class="n" style="' + st(v) + '">' + (Math.abs(v) < 500 ? '<span class="mut3">·</span>' : K.d({ title, unit: 'тыс ₽/бп', rows: rowsFn() }, null, '')) + '</td>';
      const rowsOf = (r, k) => L.map(p => ({ p, v: r.id === 'CS01' ? (p.ccy === ccy ? (k == null ? p.cs01 : p.cs01b[k]) : 0) : (k == null ? p.dv01ByCurve[r.id] : p.risk[r.id][k]) }));
      const rateRows = (k, cumul) => L.map(p => ({ p, v: cs.filter(x => x.kind === 'rate').reduce((s, x) => s + (cumul ? p.risk[x.id].slice(0, k + 1).reduce((a, b) => a + b, 0) : p.risk[x.id][k]), 0) }));
      el.innerHTML = '<div class="k-tbl-wrap"><table class="k-tbl k-compact k-pin2"><thead><tr><th>' + K.ccyTag(ccy) + ' кривая</th><th class="n">Итого</th>' + M.buckets.map(b => '<th class="n">' + b + '</th>').join('') + '</tr></thead><tbody>' +
        rows.map(r => {
          const col = K.curveColors[r.id], bg = mode === 'type' ? ' style="background:' + tint(col, 0.10) + '"' : '';
          return '<tr' + bg + '><td><b style="color:' + col + '">■</b> ' + K.hint(mode === 'type' ? '<span style="color:' + col + '">' + esc(r.label) + '</span>' : esc(r.label), K.curveTip(r.id)) + (r.id !== 'CS01' && M.curve[r.id].kind === 'basis' ? ' <span class="mut3">базис</span>' : '') + '</td>' +
            tn + '<b>' + K.d({ title: r.label + ' · всего', unit: 'тыс ₽/бп', rows: rowsOf(r) }, null, '') + '</b></td>' +
            r.vals.map((v, k) => cell(v, r.label + ' · ' + M.buckets[k], () => rowsOf(r, k))).join('') + '</tr>';
        }).join('') +
        '</tbody><tfoot><tr><td>Σ ставки</td>' + tn + f.dv(tot.reduce((a, b) => a + b, 0)) + '</td>' + tot.map((v, k) => tn + K.d({ title: ccy + ' ставки · ' + M.buckets[k], unit: 'тыс ₽/бп', rows: rateRows(k) }, null, '') + '</td>').join('') + '</tr>' +
        '<tr><td class="mut">кумулятив</td><td></td>' + cum.map((v, k) => tn.replace('"n"', '"n mut"') + K.d({ title: ccy + ' кумулятив до ' + M.buckets[k], unit: 'тыс ₽/бп', rows: rateRows(k, true) }, null, '') + '</td>').join('') + '</tr></tfoot></table></div>';
      K.tableTools(el, { viewKey: opt.viewKey || 'buckets-' + ccy, title: 'Риск ' + ccy + ': кривая × бакет, тыс ₽/бп' });
    }
    draw(); K.live(el, 'filter', draw); K.live(el, 'theme', draw); K.live(el, 'riskcolor', draw);
    return { redraw: draw };
  };

  // PnL-водопад: кэрри → фондирование → ставки → спред → FX → сделки → прочее → итог; столбцы кликабельны.
  // Если самая длинная подпись категории (с зазором K.WF_GAP) не помещается в ширину столбца — ширина графика без
  // K.WF_AXIS px под подписи оси, делённая на число столбцов (плитка C06 на 1366 px, #261), — подписи стоят в два ряда
  // вперемешку (чётные выше, нечётные ниже), иначе соседние слова слипаются. Широкая плитка (1920 px) — в один ряд
  K.WF_AXIS = 50; K.WF_GAP = 8;
  let wfCtx = null;
  K.labelWidth = (s, px) => {
    try { if (!wfCtx) wfCtx = document.createElement('canvas').getContext('2d'); } catch (e) { }
    if (!wfCtx || !wfCtx.measureText) return String(s).length * px * 0.6;
    wfCtx.font = px + 'px system-ui, sans-serif'; return wfCtx.measureText(String(s)).width;
  };
  K.pnlWaterfall = function (el, opt) {
    const keys = K.PNL_FACTORS;
    const cats = keys.map(k => k[0] === 'funding' ? 'Фондир.' : k[1]).concat(['Итого']);
    let L, stag = false;
    const need = () => Math.max(...cats.map(s => K.labelWidth(s, 10))) + K.WF_GAP;
    const narrow = () => el.clientWidth > 0 && (el.clientWidth - K.WF_AXIS) / cats.length < need();
    const c = K.chart(el, () => {
      L = K.list(); stag = narrow();
      const vals = keys.map(([k]) => M.sum(L, p => p.pnl[k]));
      // отрезки [от, до]: стек ECharts раскладывает отрицательные значения отдельно, поэтому рисуем прямоугольники сами
      let run = 0; const segs = vals.map(v => { const s = [run, run + v]; run += v; return s; });
      segs.push([0, run]);
      const cols = vals.map(v => v >= 0 ? css('--pos') : css('--neg')).concat([css('--accent')]);
      const lo = Math.min(0, ...segs.map(x => Math.min(x[0], x[1]))), hi = Math.max(0, ...segs.map(x => Math.max(x[0], x[1])));
      return {
        grid: { left: 6, right: 10, top: 18, bottom: 4, containLabel: true }, tooltip: { trigger: 'item', formatter: p => (keys[p.dataIndex] ? keys[p.dataIndex][1] : 'Итого') + ': <b>' + f.mns(p.dataIndex < vals.length ? vals[p.dataIndex] : run, 2) + '</b> mm ₽' },
        xAxis: { type: 'category', data: cats, axisLabel: { interval: 0, fontSize: 10, formatter: (v, i) => stag && i % 2 ? '\n' + v : v } }, yAxis: { type: 'value', unit: 'mm ₽', nameGap: 20, min: lo, max: hi, scale: false, axisLabel: { formatter: v => K.tick(v, 1e6), hideOverlap: true, showMinLabel: !lo, showMaxLabel: !hi } },
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
    if (typeof ResizeObserver === 'function') {
      const ro = new ResizeObserver(() => { if (!el.isConnected) { ro.disconnect(); return; } if (narrow() !== stag) c.redraw(); });
      ro.observe(el);
    }
    return c;
  };

  // KPI-плитка; spec — для разбора по щелчку. sub — строка или разбивка-массив [[подпись, значение], …] (B2-3, #103):
  // строки в стиле плитки ОВП — подпись слева, цифра справа, табличные цифры.
  K.kpiRows = rows => '<table class="k-kb"><tbody>' + rows.map(r => '<tr' + (r[2] ? ' title="' + esc(r[2]) + '"' : '') + '><td class="c">' + r[0] + '</td><td>' + r[1] + '</td></tr>').join('') + '</tbody></table>';
  K.kpi = (label, valueHtml, sub, spark, tip) => '<div class="k-kpi"><div class="l">' + K.hint(label, tip) + '</div><div class="v">' + valueHtml + '</div><div class="s">' +
    (Array.isArray(sub) ? (sub.length ? K.kpiRows(sub) : '<span class="mut3">—</span>') : (sub || '&nbsp;')) + '</div>' + (spark || '') + '</div>';
  K.kpiUnit = u => ' <span class="u">' + u + '</span>';
  // разбивка по валютам: не больше n строк (по умолчанию 3, #103). Валюты по |rank| (по умолчанию — по самим значениям;
  // для сумм в родных валютах передают рублёвый эквивалент), показываются значения vals. Если валют больше n —
  // первые n−1 строками, остальные одной строкой «+N» с подсказкой. Точные нули не показываются; значения, которые
  // в формате fmt округляются до нуля, — в «+N», как «прочие» у ОВП.
  K.kpiCcyRows = (vals, cell, n, fmt, rank) => {
    rank = rank || vals; n = n || 3;
    const zero = v => /^[−+-]?0(?:[.,]0*)?(?:\s|$)/.test(String(fmt(v)).trim());  // fmt может нести единицу: «0.0 mm»
    const R = M.ccys.map(c => ({ c, v: vals[c] || 0, r: rank[c] || 0 })).filter(x => Math.abs(x.v) > 1e-9)
      .sort((a, b) => (Math.abs(b.r) - Math.abs(a.r)) || a.c.localeCompare(b.c));
    const big = R.filter(x => !zero(x.v)), minor = R.filter(x => zero(x.v));
    const top = big.length + (minor.length ? 1 : 0) > n ? big.slice(0, n - 1) : big, rest = big.slice(top.length).concat(minor);
    return top.map(x => [esc(x.c), cell(x.c, x.v)])
      .concat(rest.length ? [['<span class="mut3">+' + rest.length + '</span>', '<span class="mut3">' + esc(rest.map(x => x.c).join(', ')) + '</span>', rest.map(x => x.c + ' ' + fmt(x.v)).join(' · ')]] : []);
  };

  // ---------- ОВП в шапке (R43) ----------
  // Мини-таблица: валюта на строку — сумма в валюте (mm) и эквивалент в валюте отчёта, по |эквиваленту| по убыванию,
  // один формат (1 знак, разделитель тысяч, знак цветом); строки, которые округляются до 0.0 mm, — под «+N прочих».
  // Суммы — по ногам (INSTR.fxIn: ключи fx_delta прогона; вторая валюта кросса — в своей строке).
  K.tips.fxMini = 'Открытая валютная позиция по валютам, mm в валюте и ≈ в валюте отчёта по курсу прогона. Слагаемые: облигации — грязная MV в валюте минус валютный пассив фондирования (грязная MV на прошлый перенос ОВП, за стоимостью дня не следует) плюс купоны дня, то есть валютный PnL с прошлого переноса; вечерний перенос уводит его в деривативную книгу (предварительно: residual прошлых дней не накапливается), без пассива (фондирование не настроено, проблема) — вся грязная MV; деривативы и FX — FX-дельта по ногам (у кросса — обе валюты); сделки переноса ОВП — их сумма. Щелчок по цифре — из каких позиций';
  K.fxRows = L => K.fxCcys(L).map(c => { const v = M.sum(L, p => INSTR.fxIn(p, c)); return { c, v, eq: v * ((M.toRepr && M.toRepr[c]) || 1) }; })
    .sort((a, b) => (Math.abs(b.eq) - Math.abs(a.eq)) || a.c.localeCompare(b.c));
  K.FX_MINOR = 0.05e6;  // меньше — 0.0 mm: в «прочих»
  K.fxMini = function (L) {
    const R = K.fxRows(L);
    if (!R.length) return '<span class="mut3">открытых позиций нет</span>';
    const minor = r => Math.abs(r.v) < K.FX_MINOR;
    const big = R.filter(r => !minor(r)), rest = R.filter(minor);
    const row = (r, cls) => '<tr' + (cls ? ' class="' + cls + '"' : '') + '><td class="c">' + esc(r.c) + '</td><td>' +
      K.d({ title: 'ОВП ' + r.c + ' (FX-дельта)', unit: 'mm ' + r.c, fmt: v => f.mns(v, 1), list: L, val: p => INSTR.fxIn(p, r.c) }, f.mns(r.v, 1)) +
      '</td><td class="' + f.cls(r.eq) + '">' + f.mns(r.eq, 1) + '</td></tr>';
    return '<table class="k-fxm"><thead><tr><th>вал.</th><th>mm в вал.</th><th>≈ mm ' + K.SYM[K.repr] + '</th></tr></thead><tbody>' +
      big.map(r => row(r)).join('') +
      (rest.length ? '<tr class="more" data-fxmore="' + rest.length + '"><td colspan="3" title="' + esc(rest.map(r => r.c + ' ' + f.mns(r.v, 3) + ' mm').join(' · ')) + '">+' + rest.length + ' прочих: ' + esc(rest.map(r => r.c).join(', ')) + '</td></tr>' + rest.map(r => row(r, 'small')).join('') : '') +
      '</tbody></table>';
  };
  document.addEventListener('click', e => {
    const t = e.target.closest && e.target.closest('[data-fxmore]');
    if (!t) return;
    const tb = t.closest('.k-fxm'); tb.classList.toggle('open');
    t.firstChild.textContent = tb.classList.contains('open') ? 'свернуть ' + t.dataset.fxmore + ' прочих' : '+' + t.dataset.fxmore + ' прочих';
  });

  // стандартная строка HUD: риск по валютам и кривым, PnL, кэрри, объём
  K.hud = function (el) {
    function draw() {
      const L = K.list(), k = M.kpis(L);
      const pnlH = M.hist.pnl.total.slice(-30);
      el.innerHTML = '<div class="k-kpis">' +
        K.kpi('PnL дня', K.d({ title: 'PnL дня', unit: 'mm ₽', fmt: v => f.mns(v, 2), list: L, val: p => p.pnl.total }, f.mns(k.pnl, 1)) + K.kpiUnit('mm ₽'),
          [['MTD', K.d({ title: 'PnL MTD', unit: 'mm ₽', fmt: v => f.mns(v, 1), list: L, val: 'pnlMtd' }) + K.partial('mtd')],
           ['YTD', K.d({ title: 'PnL YTD', unit: 'mm ₽', fmt: v => f.mns(v, 0), list: L, val: 'pnlYtd' }) + K.partial('ytd')]], K.spark(pnlH, 64, 20)) +
        K.kpi('DV01 ставки', K.d({ title: 'DV01 ставки (все кривые)', unit: 'тыс ₽/бп', list: L, val: 'dv01' }) + K.kpiUnit('тыс ₽/бп'),
          K.kpiCcyRows(M.ccys.reduce((o, c) => (o[c] = k.byCcy[c].dv01, o), {}), c => K.d({ title: 'DV01 ' + c + ' (кривые ' + c + ')', unit: 'тыс ₽/бп', list: L, val: p => INSTR.dv01In(p, c) }), 3, f.dv), null, K.tips.dv01) +
        K.kpi('DV01 RUONIA', K.d({ title: 'DV01 RUONIA', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB RUONIA'] }) + K.kpiUnit('тыс ₽/бп'),
          [['КС', K.d({ title: 'DV01 КС', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB KEY'] })],
           M.ofzView === 'kbd' ? [K.hint('КБД', K.tips.kbd), K.d({ title: 'DV01 КБД (ОФЗ)', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB OFZ'] })]
             : [K.hint('базис ОФЗ', K.tips.basis01), K.d({ title: 'Базис ОФЗ−RUONIA', unit: 'тыс ₽/бп', list: L, val: 'basis01' })],
           [K.hint('спред к КБД', K.tips.ofzSpr01), K.d({ title: 'Спред ОФЗ к КБД', unit: 'тыс ₽/бп', list: L, val: 'ofzSpr01' })]], null,
          K.tips.ruonia + '\n\n' + (M.ofzView === 'kbd' ? K.tips.kbd : K.tips.basis01)) +
        K.kpi('CS01', K.d({ title: 'CS01', unit: 'тыс ₽/бп', list: L, val: 'cs01' }) + K.kpiUnit('тыс ₽/бп'),
          [['корп. RUB', K.d({ title: 'CS01 корп RUB', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy === 'RUB'), val: 'cs01' })],
           ['вал.', K.d({ title: 'CS01 валютные', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy !== 'RUB'), val: 'cs01' })]], null, K.tips.cs01) +
        K.kpi('Кэрри / день', K.d({ title: 'Кэрри за день (без фондирования)', unit: 'тыс ₽', fmt: f.k, list: L, val: 'carryDay' }) + K.kpiUnit('тыс ₽'),
          [[K.hint('фондирование', K.tips.funding), K.d({ title: 'Фондирование за день', unit: 'тыс ₽', fmt: f.k, list: L, val: 'fundingDay', nullable: true })],
           ['в год ≈', '<span class="' + f.cls(k.carryDay) + '">' + f.mn(k.carryDay * 365, 0) + '</span> <span class="u">mm ₽</span>']], null, K.tips.carry) +
        K.kpi('Облигации MV', K.d({ title: 'MV облигаций', unit: 'bn ₽', fmt: v => f.bn(v, 1), list: L.filter(p => p.group !== 'DER'), val: 'mv', color: false }) + K.kpiUnit('bn ₽'),
          K.kpiCcyRows(M.ccys.reduce((o, c) => (o[c] = k.byCcy[c].mv, o), {}), (c, v) => f.bn(v, 1), 3, v => f.bn(v, 1))) +
        '<div class="k-kpi k-fxk"><div class="l">' + K.hint('ОВП', K.tips.fxMini) + '</div><div class="v">' + K.fxMini(L) + '</div><div class="s"><a class="go" href="c06-launchpad.html#fx" title="плитка «ОВП и курсы»: бонды и деривативы по валютам, курсы, PnL FX">подробно (C06) →</a></div></div>' +
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
