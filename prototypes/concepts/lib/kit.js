/*
 * KIT — общие компоненты концептов (нужны mock.js и ../echarts.min.js).
 *
 *  KIT.f.*                — форматирование (тыс ₽/бп, mm, bn, б.п., даты)
 *  KIT.top({title, tag})  — шапка: дата среза, фильтр книг (общий для всех концептов), поиск, тема
 *  KIT.list()             — позиции после фильтра шапки; KIT.onFilter(fn) — перерисовка при смене фильтра
 *  KIT.d(spec)            — кликабельная цифра: spec = {title, list|rows, val, fmt, unit, v?}; щелчок раскрывает вклад
 *  KIT.drill(spec)        — открыть разбор вклада (позиции, группировка по эмитенту/книге/валюте/типу/бакету)
 *  KIT.card(id)           — карточка позиции: параметры, графики истории, риск по бакетам, потоки, PnL
 *  KIT.table(el, opts)    — таблица: группы, мультисортировка (Shift+щелчок), поиск, итоги с разбором
 *  KIT.chart(el, option)  — echarts с цветами темы; KIT.riskStack(el, opts) — риск по бакетам: стек кривых + итог + кумулятив
 *  KIT.seg(el, items, cur, fn) — переключатель режимов (таблица / график и т. п.)
 *  KIT.palette()          — поиск по всему (Ctrl+K или «/»): позиции, эмитенты, книги, команды страницы
 */
(function () {
  'use strict';
  const M = window.MOCK;
  const K = window.KIT = {};

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
      Object.keys(p.pnl).forEach(x => p.pnl[x] *= k);
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

  // ---------- цвета ----------
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  K.css = css;
  K.ccyColor = c => css('--' + String(c).toLowerCase()) || css('--accent');
  K.ccyTag = c => '<span class="k-ccy" style="background:' + K.ccyColor(c) + '">' + c + '</span>';
  K.curveColors = { 'RUB RUONIA': '#4c9dff', 'RUB KEY': '#22c3d6', 'RUB OFZ': '#9b7bff', 'USD SOFR': '#3fb67a', 'EUR ESTR': '#d58cff', 'CNY XCCY': '#f0645a', 'CS01': '#e3b341' };
  K.PAL = ['#4c9dff', '#3fb67a', '#e3b341', '#f0645a', '#b48cff', '#22c3d6', '#ff8f3f', '#8bc34a', '#ef6fb0', '#7f8cff', '#c9a26b', '#5fd4a8', '#ff6b8b', '#a0b4c8'];
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
  const filt = { entity: null, desk: null, books: null, ccy: null };
  try { Object.assign(filt, JSON.parse(localStorage.getItem('concepts-filter') || '{}')); } catch (e) { }
  K.filter = filt;
  K.setFilter = (patch) => { Object.assign(filt, patch); try { localStorage.setItem('concepts-filter', JSON.stringify(filt)); } catch (e) { } fire('filter'); renderFilterChips(); };
  K.list = () => M.positions.filter(p => (!filt.entity || p.entity === filt.entity) && (!filt.desk || p.desk === filt.desk) && (!filt.books || !filt.books.length || filt.books.includes(p.book)) && (!filt.ccy || p.ccy === filt.ccy));
  let chipsBox = null;
  // каскад: юрлицо → деск → книги; в каждом списке только варианты, совместимые с уже выбранным
  const booksIn = () => M.books.filter(b => (!filt.entity || b.entity === filt.entity) && (!filt.desk || b.desk === filt.desk));
  const nPos = {}; M.positions.forEach(p => nPos[p.book] = (nPos[p.book] || 0) + 1);
  function renderFilterChips() {
    if (!chipsBox) return;
    const ent = M.entities;
    const desks = [...new Set(M.books.filter(b => !filt.entity || b.entity === filt.entity).map(b => b.desk))];
    const lbl = filt.books && filt.books.length ? (filt.books.length === 1 ? M.book[filt.books[0]].name : filt.books.length + ' книг') : 'все книги (' + booksIn().length + ')';
    chipsBox.innerHTML =
      '<span class="k-chip' + (filt.entity ? ' on' : '') + '" data-f="entity">' + (filt.entity ? esc(ent.find(e => e.id === filt.entity).name) : 'все юрлица') + ' ▾</span>' +
      '<span class="k-chip' + (filt.desk ? ' on' : '') + '" data-f="desk">' + (filt.desk ? esc(filt.desk) : 'все дески') + ' ▾</span>' +
      '<span class="k-chip' + (filt.books && filt.books.length ? ' on' : '') + '" data-f="books">' + esc(lbl) + ' ▾</span>' +
      ((filt.entity || filt.desk || (filt.books && filt.books.length) || filt.ccy) ? '<span class="k-chip" data-f="reset"><span class="x">✕</span> сбросить</span>' : '');
    chipsBox.querySelectorAll('[data-f]').forEach(ch => ch.onclick = ev => {
      const k = ch.dataset.f;
      if (k === 'reset') return K.setFilter({ entity: null, desk: null, books: null, ccy: null });
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
      }, k === 'books');
      ev.stopPropagation();
    });
  }
  let openMenu = null;
  function menu(anchor, items, cur, pick, multi) {
    if (openMenu) openMenu.remove();
    const m = h('div', 'k-panel'); m.style.cssText = 'position:fixed;z-index:140;min-width:220px;max-height:60vh;overflow:auto;box-shadow:var(--shadow);padding:4px 0';
    const r = anchor.getBoundingClientRect(); m.style.left = Math.min(r.left, innerWidth - 300) + 'px'; m.style.top = (r.bottom + 4) + 'px';
    let rowsBox = m, q = multi ? (menu._q || '') : '';
    const reopen = () => { m.remove(); openMenu = null; const nb = chipsBox.querySelector('[data-f=books]'); if (nb) nb.click(); };
    const vis = () => items.filter(it => !q || (it.t + ' ' + (it.sub || '')).toLowerCase().includes(q));
    if (multi) {
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
      const m = h('div', 'k-panel'); m.style.cssText = 'position:fixed;z-index:140;width:min(460px,92vw);max-height:60vh;overflow:auto;box-shadow:var(--shadow);padding:10px 14px;font-size:12.5px';
      const r = nb.getBoundingClientRect(); m.style.left = Math.max(8, Math.min(r.left, innerWidth - 470)) + 'px'; m.style.top = (r.bottom + 4) + 'px';
      m.innerHTML = '<div class="k-sec" style="margin-top:0">Чего нет в данных</div><ul style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:4px">' + M.notes.map(t => '<li>' + esc(t) + '</li>').join('') + '</ul>';
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

  // ---------- шапка ----------
  K.top = function (opt) {
    opt = opt || {};
    document.title = opt.title || document.title;
    const bar = h('div', 'k-top');
    bar.innerHTML = '<div class="brand"><a href="index.html" title="Все концепты">◂ концепты</a><span>' + esc(opt.title || '') + '</span>' + (opt.tag ? '<span class="tag">' + esc(opt.tag) + '</span>' : '') + '</div>' +
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
      body.innerHTML = '<table class="k-tbl"><thead><tr><th></th><th>' + (dim === 'pos' ? 'Позиция' : D[1][0].toUpperCase() + D[1].slice(1)) + '</th>' + (dim === 'pos' ? '<th>Книга</th><th>Ccy</th>' : '<th class="n">Позиций</th>') + '<th class="n">Вклад</th><th style="width:120px"></th><th class="n">Доля</th><th class="n">Накопл.</th></tr></thead><tbody>' +
        list.map(x => {
          cum += Math.abs(x.v);
          const stripe = dim === 'issuer' || dim === 'pos' ? K.issuerColor(x.p.issuer) : dim === 'ccy' ? K.ccyColor(x.k) : 'transparent';
          return '<tr class="row" data-k="' + esc(x.k) + '"><td class="stripe" style="background:' + stripe + '"></td><td>' + esc(x.label) + (dim === 'pos' ? ' <span class="mut3">' + esc(x.p.issuerName) + '</span>' : '') + '</td>' +
            (dim === 'pos' ? '<td class="mut">' + esc(x.p.book) + '</td><td>' + K.ccyTag(x.p.ccy) + '</td>' : '<td class="n mut">' + x.n.length + '</td>') +
            '<td class="n ' + f.cls(x.v) + '">' + fmt(x.v) + '</td><td>' + K.bar(x.v, max) + '</td><td class="n mut">' + Math.round(Math.abs(x.v) / absTot * 100) + '%</td><td class="n mut3">' + Math.round(cum / absTot * 100) + '%</td></tr>';
        }).join('') + '</tbody><tfoot><tr><td></td><td>Итого</td><td colspan="' + (dim === 'pos' ? 2 : 1) + '"></td><td class="n ' + f.cls(tot) + '">' + fmt(tot) + '</td><td colspan="3"></td></tr></tfoot></table>';
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

  // ---------- таблица ----------
  // opts: {columns:[{key,label,val(r),fmt(v,r),cls,title,sum:true|fn,drill:{title,unit,fmt,val}, w}], rows|rowsFn, groupBy:[{key(r),label(k,rows)}], search:bool,
  //        sort:[[key,dir]], stripe(r)->color, onRow(r), rowCls(r), tools(el), maxHeight, footer:bool}
  K.table = function (el, opts) {
    const st = { sort: opts.sort ? opts.sort.slice() : [], q: '', collapsed: {} };
    el.innerHTML = '';
    let tools = null;
    if (opts.search !== false || opts.tools) {
      tools = h('div', 'k-tbl-tools'); el.appendChild(tools);
      if (opts.search !== false) { const i = h('input', 'k-input'); i.placeholder = 'поиск в таблице…'; i.oninput = () => { st.q = i.value.toLowerCase(); draw(); }; tools.appendChild(i); }
      tools.appendChild(h('span', 'k-note', 'Shift+щелчок по заголовку — сортировка по нескольким колонкам'));
      if (opts.tools) { const sp = h('span'); sp.style.flex = '1'; tools.appendChild(sp); opts.tools(tools); }
    }
    const wrap = h('div', 'k-tbl-wrap'); if (opts.maxHeight) wrap.style.maxHeight = opts.maxHeight; el.appendChild(wrap);
    const cols = () => opts.columns.filter(c => !c.hidden);
    const val = (c, r) => c.val ? c.val(r) : r[c.key];
    function sorted(rows) {
      if (!st.sort.length) return rows;
      const C = opts.columns;
      return rows.slice().sort((a, b) => {
        for (const [k, d] of st.sort) { const c = C.find(x => x.key === k); let x = val(c, a), y = val(c, b); if (x == null && y == null) continue; if (x == null) return 1; if (y == null) return -1; const r = typeof x === 'string' ? x.localeCompare(y, 'ru') : x - y; if (r) return d * r; }
        return 0;
      });
    }
    function draw() {
      let rows = opts.rowsFn ? opts.rowsFn() : opts.rows;
      if (st.q) rows = rows.filter(r => (r.searchText || JSON.stringify(r)).toLowerCase().includes(st.q));
      rows = sorted(rows);
      const C = cols();
      const head = '<thead><tr>' + (opts.stripe ? '<th class="stripe"></th>' : '') + C.map(c => { const si = st.sort.findIndex(s => s[0] === c.key); return '<th class="sortable ' + (c.num ? 'n' : '') + '" data-k="' + c.key + '" title="' + esc(c.title || '') + '"' + (c.w ? ' style="width:' + c.w + '"' : '') + '>' + c.label + (si >= 0 ? '<span class="si">' + (st.sort[si][1] > 0 ? '▲' : '▼') + (st.sort.length > 1 ? si + 1 : '') + '</span>' : '') + '</th>'; }).join('') + '</tr></thead>';
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
        let keys = [...buckets.keys()]; if (G.order) keys.sort(G.order);
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
    name: { key: 'name', label: 'Бумага', fmt: (v, r) => esc(v) + (r.tradedToday ? ' <span class="k-tag" style="border-color:var(--warn);color:var(--warn)" title="сделки сегодня">сд</span>' : '') },
    isin: { key: 'isin', label: 'ISIN', fmt: v => '<span class="mono mut">' + esc(v || '—') + '</span>' },
    ccy: { key: 'ccy', label: 'Вал', fmt: (v, r) => K.ccyTag(r.ccy), val: r => M.ccyOrder[r.ccy] },
    kind: { key: 'kind', label: 'Тип', fmt: (v, r) => '<span class="tagc" style="border-color:' + K.kindColor[v] + '55">' + esc(M.kindShort[v]) + '</span>' },
    book: { key: 'book', label: 'Книга', fmt: v => '<span class="mut">' + esc(v) + '</span>' },
    coupon: { key: 'couponFmt', label: 'Купон', fmt: v => '<span class="mono">' + esc(v || '—') + '</span>' },
    event: { key: 'event', label: 'Событие', val: r => r.event.date, fmt: (v, r) => '<span class="mut3">' + ({ put: 'пут', call: 'колл', mat: 'погаш' }[r.event.kind]) + '</span> ' + f.date(r.event.date) },
    maturity: { key: 'maturity', label: 'Погашение', fmt: v => f.date(v) },
    offer: { key: 'offer', label: 'Оферта', fmt: (v, r) => v ? f.date(v) + ' <span class="mut3">' + (r.offerKind === 'put' ? 'P' : 'C') + '</span>' : '<span class="mut3">—</span>' },
    tenor: { key: 'tenor', label: 'Срок', num: true, fmt: v => f.yrs(v), title: 'до оферты, если есть, иначе до погашения' },
    notional: { key: 'notional', label: 'Номинал', num: true, fmt: (v, r) => f.mn(v, 0) + ' <span class="mut3">' + r.ccy + '</span>', title: 'mm, в валюте бумаги' },
    mv: { key: 'mv', label: 'MV, mm ₽', num: true, fmt: v => f.mn(v, 0), sum: true, unit: 'mm ₽', sumFmt: v => f.mn(v, 0) },
    price: { key: 'price', label: 'Цена', num: true, fmt: v => f.px(v) },
    ytm: { key: 'ytm', label: 'Дох.', num: true, fmt: v => f.pct(v) },
    spread: { key: 'zspread', label: 'Спред', num: true, fmt: (v, r) => v == null ? '<span class="mut3">—</span>' : f.bp(v) + ' <span class="mut3">' + (r.spreadLabel || '') + '</span>', title: 'Z-спред к RUONIA (фикс) / дисконт-маржа (флоатер)' },
    gspread: { key: 'gspread', label: 'G-спред', num: true, fmt: v => f.bp(v) },
    dur: { key: 'mdur', label: 'Дюр.', num: true, fmt: v => v == null ? '—' : f.n(v, 2) },
    dv01: { key: 'dv01', label: 'DV01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп', title: 'тыс ₽/бп, по ставкам' },
    cs01: { key: 'cs01', label: 'CS01', num: true, fmt: v => f.dv(v), cls: v => f.cls(v), sum: true, unit: 'тыс ₽/бп' },
    basis01: { key: 'basis01', label: 'Базис01', num: true, fmt: v => v ? f.dv(v) : '<span class="mut3">·</span>', sum: true, unit: 'тыс ₽/бп', title: 'ОФЗ − RUONIA' },
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
    { key: r => r.ccy, label: k => K.ccyTag(k) + ' ' + ({ RUB: 'Рубли', USD: 'Доллары', EUR: 'Евро', CNY: 'Юани' }[k] || k), order: (a, b) => M.ccyOrder[a] - M.ccyOrder[b], color: k => K.ccyColor(k), span: 1 },
    { key: r => r.group === 'DER' ? 'Деривативы' : r.issuerName, label: (k, rows) => esc(k) + (rows[0].group === 'DER' ? ' <span class="mut3" style="font-weight:400">(по контрагентам)</span>' : ''), order: K.issuerOrder, color: (k, rows) => K.issuerColor(rows[0].issuer) },
  ];

  // ---------- карточка позиции ----------
  K.card = function (id) {
    const p = M.pos[id]; if (!p) return;
    const o = drawer(true); o.title = p.name; o.charts = [];
    o.el.querySelector('.t').innerHTML = '<span style="color:' + K.issuerColor(p.issuer) + '">●</span> ' + esc(p.name) + ' ' + K.ccyTag(p.ccy);
    o.el.querySelector('.st').innerHTML = esc(p.issuerFull) + (p.isin ? ' · <span class="mono">' + esc(p.isin) + '</span>' : '') + ' · ' + esc(p.book) + ' — ' + esc(p.bookName) + ' · ' + esc(p.groupLabel) + ' / ' + esc(p.kindLabel);
    const isBond = !!p.cpn;
    const ev = { put: 'пут-оферта', call: 'колл-опцион', mat: 'погашение' }[p.event.kind];
    const fields = isBond ? [
      ['Номинал', f.mn(p.notional, 0) + ' <small>mm ' + p.ccy + '</small>'], ['MV', f.mn(p.mv, 1) + ' <small>mm ₽</small>'],
      ['Цена (bid/ask)', f.px(p.price) + ' <small>' + f.px(p.bid) + ' / ' + f.px(p.ask) + '</small>'], ['Изм. цены', p.prevPrice == null || p.price == null ? '<span class="mut3">—</span>' : '<span class="' + f.cls(p.price - p.prevPrice) + '">' + f.s(p.price - p.prevPrice, 3) + '</span>'],
      p.cpn.type === 'float' ? ['Доходность', '<span class="mut3">—</span> <small>флоатер: смотрим DM</small>'] : ['Доходность', f.pct(p.ytm) + ' <small>к ' + (p.event.kind === 'mat' ? 'погаш.' : 'оферте') + '</small>'], [(p.spreadLabel === 'DM' ? 'Дисконт-маржа' : 'Z-спред к RUONIA'), f.bp(p.zspread) + ' <small>б.п.</small>'],
      ['G-спред к ОФЗ', f.bp(p.gspread) + ' <small>б.п.</small>'], ['Мод. дюрация', p.mdur == null ? '—' : f.n(p.mdur, 2)],
      ['Купон', '<span class="mono">' + esc(p.couponFmt) + '</span>'], ['Текущий купон', f.pct(p.curCpn) + (p.cpn.type === 'float' ? ' <small>прогноз</small>' : '')],
      ['Ближайшее событие', f.date(p.event.date) + ' <small>' + ev + ', ' + f.yrs(p.tenor) + '</small>'], ['Погашение', f.date(p.maturity) + ' <small>' + f.yrs(p.yearsToMat) + '</small>'],
      ['DV01 ставки', K.d({ title: 'DV01 ' + p.name, unit: 'тыс ₽/бп', rows: [{ p, v: p.dv01 }] }, f.dv(p.dv01), '') + ' <small>тыс ₽/бп</small>'], ['CS01', f.dv(p.cs01) + ' <small>тыс ₽/бп</small>'],
      ['Базис ОФЗ01', p.basis01 ? f.dv(p.basis01) : '—'], ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽ (фонд. ' + f.pct(p.fundRate) + ')</small>'],
      ['НКД', f.mn(p.accrued, 2) + ' <small>mm ₽</small>'], ['Рейтинг / сектор', esc(p.rating) + ' <small>' + esc(p.sector) + '</small>'],
    ] : [
      ['Номинал', f.mn(p.notional, 0) + ' <small>mm ' + p.ccy + '</small>'], ['MTM', f.mn(p.mv, 1) + ' <small>mm ₽</small>'],
      ['Ставка / курс', esc(p.couponFmt)], ['Срок', f.date(p.maturity) + ' <small>' + f.yrs(p.tenor) + '</small>'],
      ['DV01', f.dv(p.dv01) + ' <small>тыс ₽/бп</small>'], ['FX дельта', p.fxDelta ? f.mn(p.fxDelta, 1) + ' <small>mm ' + p.ccy + '</small>' : '—'],
      ['Кэрри / день', '<span class="' + f.cls(p.carryDay) + '">' + f.k(p.carryDay) + '</span> <small>тыс ₽</small>'], ['Контрагент', esc(p.cpty || '—')],
    ];
    if (isBond && p.kind === 'IN' && M.posHistory) {  // линкер: последние ICPI / IR / BEI MOEX (контракт 1.2), если есть
      const LH = M.posHistory(p.id, { deep: true }), last = a => { for (let i = (a || []).length - 1; i >= 0; i--) if (a[i] != null) return a[i]; return null; };
      if (LH.icpi || LH.bei) fields.push(['ICPI (MOEX)', last(LH.icpi) == null ? '—' : f.n(last(LH.icpi), 4)], ['IR / BEI (MOEX)', f.pct(last(LH.ir)) + ' <small>/ ' + f.pct(last(LH.bei)) + '</small>']);
    }
    const pn = p.pnl;
    o.bd.innerHTML = '<div class="k-tags">' + [p.desk, (M.entities.find(e => e.id === p.entity) || {}).name, p.rating !== '—' ? 'рейтинг ' + p.rating : null, p.tradedToday ? 'сделки сегодня' : null, p.quality && p.quality !== 'exact' ? 'оценка: ' + p.quality : null].filter(t => t && t !== '—').map(t => '<span class="k-tag">' + esc(t) + '</span>').join('') + '</div>' +
      '<div class="k-sec">Параметры</div><div class="k-fields">' + fields.map(([l, v]) => '<div><div class="l">' + l + '</div><div class="v">' + v + '</div></div>').join('') + '</div>' +
      '<div class="k-sec">PnL <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">тыс ₽</span></div>' +
      '<div class="k-fields">' + [['Итого день', pn.total], ['Кэрри', pn.carry], ['Ставки', pn.rates], ['Спред', pn.spread], ['FX', pn.fx], ['Сделки', pn.trading], ['Прочее', pn.other], ['MTD', p.pnlMtd], ['YTD', p.pnlYtd]].map(([l, v]) => '<div><div class="l">' + l + '</div><div class="v ' + f.cls(v) + '">' + (Math.abs(v) >= 1e7 ? f.mns(v, 1) + ' <small>mm</small>' : f.ks(v)) + '</div></div>').join('') + '</div>' +
      '<div class="k-sec">История <span class="sp"></span><span class="hseg"></span></div><div class="hchart" style="height:230px"></div>' +
      '<div class="k-sec">Риск по бакетам <span class="sp"></span><span class="mut3" style="text-transform:none;letter-spacing:0">тыс ₽/бп</span></div><div class="rchart" style="height:200px"></div>' +
      (isBond ? '<div class="k-sec">Ближайшие потоки</div><div class="cfs"></div>' : '');
    // история: у облигации вся (M.posHistory deep: с 2024 через API истории хаба), видно последний год, колесо и
    // ползунок — глубже; спред фикс-купона — G и Z (MOEX, к КБД), флоатера — DM (по прогонам); у линкера BEI и IR
    const H = M.posHistory(p.id, isBond ? { deep: true } : undefined);
    const isFloat = isBond && p.cpn.type === 'float';
    const isLinker = isBond && (p.kind === 'IN' || !!H.bei);
    let mode = isFloat ? 'spread' : isBond ? 'yield' : 'notional';
    const hc = K.chart(o.bd.querySelector('.hchart'), () => {
      const xs = H.dates, long = xs.length > 300;
      const S = mode === 'price' ? [{ name: 'Цена', data: H.price }]
        : mode === 'yield' ? [{ name: 'Доходность', data: H.ytm }, { name: 'ОФЗ на сроке', data: H.bench, lineStyle: { type: 'dashed', width: 1 } }]
          : mode === 'spread' ? (isFloat ? [{ name: 'DM, б.п.', data: H.dm || H.spr, connectNulls: true }]
            : [{ name: 'G-спред, б.п.', data: H.spr }].concat(H.zspr ? [{ name: 'Z-спред MOEX, б.п.', data: H.zspr }] : []))
            : mode === 'infl' ? [{ name: 'BEI, %', data: H.bei || [] }, { name: 'IR (реальная), %', data: H.ir || [] }]
              : mode === 'dv01' ? [{ name: 'DV01, тыс ₽/бп', data: H.dv01.map(v => v / 1e3), step: 'end' }] : [{ name: 'Номинал, mm', data: H.notional.map(v => v / 1e6), step: 'end', areaStyle: { opacity: .15 } }];
      return { grid: { left: 6, right: 10, top: 26, bottom: long ? 30 : 4, containLabel: true }, legend: { top: 0, left: 0 }, tooltip: { trigger: 'axis' }, xAxis: { type: 'category', data: xs, axisLabel: { formatter: v => f.dshort(v) } }, yAxis: { type: 'value', scale: true }, series: S.map((s, i) => Object.assign({ type: 'line', showSymbol: false, lineStyle: { width: 1.6 }, color: i ? css('--fg-3') : K.issuerColor(p.issuer) }, s)),
        dataZoom: long ? [{ type: 'inside', startValue: xs.length - 260 }, { type: 'slider', height: 14, bottom: 4, startValue: xs.length - 260, labelFormatter: (i, v) => f.dshort(v) }] : [{ type: 'inside' }] };
    });
    o.charts.push(hc);
    K.seg(o.bd.querySelector('.hseg'), (isFloat ? [['price', 'цена'], ['spread', 'DM']] : isBond ? [['price', 'цена'], ['yield', 'доходность'], ['spread', 'спред']] : []).concat(isLinker && H.bei ? [['infl', 'BEI/IR']] : []).concat([['notional', 'позиция'], ['dv01', 'DV01']]), mode, v => { mode = v; hc.redraw(); });
    // риск по бакетам
    const cids = M.curves.filter(c => p.risk[c.id].some(v => Math.abs(v) > 1)).map(c => c.id);
    const rc = K.chart(o.bd.querySelector('.rchart'), () => ({
      grid: { left: 6, right: 10, top: 26, bottom: 4, containLabel: true }, legend: { top: 0, left: 0 }, tooltip: { trigger: 'axis', valueFormatter: v => f.dv(v) },
      xAxis: { type: 'category', data: M.buckets }, yAxis: { type: 'value', axisLabel: { formatter: v => f.dv(v) } },
      series: cids.map(id => ({ name: M.curve[id].short, type: 'bar', stack: 'a', data: p.risk[id], itemStyle: { color: K.curveColors[id] } })).concat(p.cs01 ? [{ name: 'CS01', type: 'bar', data: p.cs01b, itemStyle: { color: K.curveColors.CS01 } }] : []),
    }));
    o.charts.push(rc);
    if (isBond) {
      const cf = M.cashflows.filter(c => c.pos === p.id).slice(0, 8);
      o.bd.querySelector('.cfs').innerHTML = cf.length ? '<table class="k-tbl"><thead><tr><th>Дата</th><th>Тип</th><th class="n">Сумма, ' + p.ccy + '</th><th class="n">≈ mm ₽</th></tr></thead><tbody>' +
        cf.map(c => '<tr><td>' + f.date(c.date) + '</td><td>' + ({ cpn: 'купон', mat: 'погашение', put: 'пут-оферта', call: 'колл' }[c.kind]) + (c.est ? ' <span class="mut3">прогноз</span>' : '') + '</td><td class="n">' + f.n(c.amount, 0) + '</td><td class="n">' + f.mn(c.amountRub, 1) + '</td></tr>').join('') + '</tbody></table>' : '<div class="k-empty">нет потоков в горизонте</div>';
    }
    return o;
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
      const cols = [];
      ccys.forEach(c => M.curves.filter(x => x.ccy === c).forEach(x => cols.push(x)));
      const curCols = opt.curves ? cols.filter(c => opt.curves.includes(c.id)) : cols;
      const allMax = Math.max(1, ...M.typeRows.map(t => Math.abs(M.sum(L.filter(p => p.group + '.' + p.kind === t.id), 'dv01'))));
      const cellStyle = v => mode === 'heat' ? K.heatRisk(v, allMax) : mode === 'sign' ? 'color:' + (v < 0 ? 'var(--accent)' : 'var(--warn)') : '';
      const num = (v, spec, bold) => Math.abs(v) < 500 ? '<span class="mut3">·</span>' : (bold ? '<b>' : '') + K.d(spec, null, '') + (bold ? '</b>' : '');
      let html = '<table class="k-tbl k-compact"><thead><tr><th>Валюта / тип</th><th class="n">DV01</th>' +
        curCols.map(c => '<th class="n" title="' + esc(c.name) + '">' + (ccys.length > 1 ? '<span class="mut3">' + c.ccy + '</span> ' : '') + esc(c.short) + '</th>').join('') +
        '<th class="n">CS01</th><th class="n">MV, mm</th></tr></thead><tbody>';
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
        html += row(K.ccyTag(c) + ' ' + ({ RUB: 'Рубли', USD: 'Доллары', EUR: 'Евро', CNY: 'Юани' }[c]), Lc, 0, null, c);
        ['GOV', 'CORP', 'DER'].forEach(g => {
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
          return '<tr' + bg + '><td><b style="color:' + col + '">■</b> ' + (mode === 'type' ? '<span style="color:' + col + '">' + esc(r.label) + '</span>' : esc(r.label)) + (r.id !== 'CS01' && M.curve[r.id].kind === 'basis' ? ' <span class="mut3">базис</span>' : '') + '</td>' +
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
  K.kpi = (label, valueHtml, sub, spark) => '<div class="k-kpi"><div class="l">' + label + '</div><div class="v">' + valueHtml + '</div><div class="s">' + (sub || '&nbsp;') + '</div>' + (spark || '') + '</div>';

  // стандартная строка HUD: риск по валютам и кривым, PnL, кэрри, объём
  K.hud = function (el) {
    function draw() {
      const L = K.list(), k = M.kpis(L);
      const pnlH = M.hist.pnl.total.slice(-30);
      el.innerHTML = '<div class="k-kpis">' +
        K.kpi('PnL дня', K.d({ title: 'PnL дня', unit: 'mm ₽', fmt: v => f.mns(v, 2), list: L, val: p => p.pnl.total }, f.mns(k.pnl, 1) + ' mm'), 'MTD ' + K.d({ title: 'PnL MTD', unit: 'mm ₽', fmt: v => f.mns(v, 1), list: L, val: 'pnlMtd' }) + K.partial('mtd') + ' · YTD ' + K.d({ title: 'PnL YTD', unit: 'mm ₽', fmt: v => f.mns(v, 0), list: L, val: 'pnlYtd' }) + K.partial('ytd'), K.spark(pnlH, 70, 20)) +
        K.kpi('DV01 ставки', K.d({ title: 'DV01 ставки (все кривые)', unit: 'тыс ₽/бп', list: L, val: 'dv01' }) + ' <span class="mut" style="font-size:11px">тыс/бп</span>', M.ccys.map(c => c + ' ' + K.d({ title: 'DV01 ' + c, unit: 'тыс ₽/бп', list: L.filter(p => p.ccy === c), val: 'dv01' }, null, '')).join(' · ')) +
        K.kpi('RUONIA · КС', K.d({ title: 'DV01 RUONIA', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB RUONIA'] }) + ' · ' + K.d({ title: 'DV01 КС', unit: 'тыс ₽/бп', list: L, val: p => p.dv01ByCurve['RUB KEY'] }), 'базис ОФЗ ' + K.d({ title: 'Базис ОФЗ−RUONIA', unit: 'тыс ₽/бп', list: L, val: 'basis01' }, null, '')) +
        K.kpi('CS01', K.d({ title: 'CS01', unit: 'тыс ₽/бп', list: L, val: 'cs01' }), 'корп. RUB ' + K.d({ title: 'CS01 корп RUB', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy === 'RUB'), val: 'cs01' }, null, '') + ' · вал. ' + K.d({ title: 'CS01 валютные', unit: 'тыс ₽/бп', list: L.filter(p => p.ccy !== 'RUB'), val: 'cs01' }, null, '')) +
        K.kpi('Кэрри / день', K.d({ title: 'Кэрри за день', unit: 'тыс ₽', fmt: f.k, list: L, val: 'carryDay' }) + ' <span class="mut" style="font-size:11px">тыс</span>', 'в год ≈ ' + f.mn(k.carryDay * 365, 0) + ' mm') +
        K.kpi('Облигации MV', K.d({ title: 'MV облигаций', unit: 'bn ₽', fmt: v => f.bn(v, 1), list: L.filter(p => p.group !== 'DER'), val: 'mv', color: false }) + ' <span class="mut" style="font-size:11px">bn</span>', M.ccys.map(c => c + ' ' + f.bn(k.byCcy[c].mv, 1)).join(' · ')) +
        K.kpi('ОВП', M.ccys.filter(c => c !== 'RUB').map(c => c + ' ' + K.d({ title: 'FX дельта ' + c, unit: 'mm ' + c, fmt: v => f.mn(v, 1), list: L.filter(p => p.ccy === c), val: 'fxDelta' })).join(' '), 'mm в валюте') +
        '</div>';
    }
    draw(); K.live(el, 'filter', draw);
    return { redraw: draw };
  };

  // стандартные команды поиска
  K.commands([
    { label: 'Сбросить фильтр книг', run: () => K.setFilter({ entity: null, desk: null, books: null, ccy: null }) },
    { label: 'Переключить тему', run: () => K.toggleTheme() },
    { label: 'Все концепты', run: () => location.href = 'index.html' },
  ]);
})();
