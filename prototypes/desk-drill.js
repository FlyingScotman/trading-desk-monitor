/*
 * DESK.drill — «из чего состоит число» (R19): клик по числу, столбику, ячейке открывает панель с позициями, которые
 * его образуют, и вкладом каждой. Подключается загрузчиком (desk-loader.js) после desk-scope.js; страница передаёт
 * тот же отфильтрованный набор позиций, по которому посчитано число, поэтому панель видит тот же фильтр.
 *
 *   DESK.drill.open({
 *     title: 'DV01 · RUB RUONIA · 5Y',      // заголовок
 *     unit: 'тыс ₽/б.п.',                   // подпись единиц
 *     total: 12345,                         // показанное число; строки не сходятся — строка «Остаток/округление»
 *     rows: [{ id, name, book, issuer, isin, segment, value, sub, extra: {k: v} }],   // вклады
 *     columns: [{ key, label, fmt }],       // доп. колонки из row.extra; fmt(v, row) → текст
 *     note: 'как считается число',
 *     scale: 1e3,                           // делитель для показа (value, total — в исходных единицах), по умолчанию 1
 *     digits: 0,                            // знаков после запятой (по умолчанию — по величине)
 *     groups: [{ key, label }],             // доп. группировки по row.extra[key] (к книге / эмитенту / сегменту)
 *     actions: [{ label, run }]             // кнопки панели (например, «Фильтр доски: книга …»)
 *   });
 *   DESK.drill.attach(el, () => spec);      // el кликабелен (курсор, подсказка), открывает панель по клику и Enter
 *   DESK.drill.attr(key, () => spec)        // то же для разметки строкой: ' data-drill="key" tabindex="0" …'
 *   DESK.drill.put(key, () => spec); DESK.drill.spec(key)   // ключ без разметки (столбик графика) и спецификация по ключу
 *
 * Строки сортируются по |вкладу|, доля — от итога, накопленная доля — полоской; поиск, группировка с подытогами,
 * копирование CSV (;), Esc — закрыть. Строка-позиция (row.id) открывает карточку позиции DESK.card.open(id), где она
 * есть (Desk Monitor), иначе — ссылку на карточку в Desk Monitor. Помощники: rows(list, f, o) — строки из позиций,
 * pos(list, f, spec) — спецификация по позициям (итог — сумма), pnl(list, key, o) — компонента PnL с методом и
 * причиной у каждой позиции. model(spec) — расчёт панели без DOM (тесты), last — модель последней открытой панели.
 */
(function () {
  'use strict';
  const D = window.DESK;
  if (!D) return;
  const U = D.util || {};

  /* ---------- формат ---------- */
  const NF = {};
  const nf = d => NF[d] || (NF[d] = new Intl.NumberFormat('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d }));
  function num(v, d, sign) {
    if (v == null || !isFinite(v)) return '—';
    if (Math.abs(v) < 0.5 * Math.pow(10, -d)) v = 0;
    let s = nf(d).format(v).replace(/\s/g, ' ').replace('-', '−');
    if (sign && v > 0) s = '+' + s;
    return s;
  }
  const esc = s => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const finite = v => typeof v === 'number' && isFinite(v);

  /* ---------- модель панели: строки, доли, остаток, группы (без DOM) ---------- */
  const GROUPS = [{ key: '', label: 'Без группировки' }, { key: 'book', label: 'Книга' }, { key: 'issuer', label: 'Эмитент' }, { key: 'segment', label: 'Сегмент' }];
  const REST = 'Остаток/округление';
  // книга вне фильтра (desk-scope) не перечисляется, но читается по id
  const bookName = b => b == null ? '' : (D.BOOKS && D.BOOKS[b] && D.BOOKS[b].name) || String(b);
  function groupOf(r, key) {
    if (!key) return '';
    if (key === 'book') return r.bookName || '— без книги';
    if (key === 'issuer') return r.issuer || '— без эмитента';
    if (key === 'segment') return r.segment || '— без сегмента';
    const v = r.extra && r.extra[key];
    return v == null || v === '' ? '—' : String(v);
  }
  function model(spec, ui) {
    spec = spec || {}; ui = ui || {};
    const scale = finite(spec.scale) && spec.scale ? spec.scale : 1;
    const rows = (spec.rows || []).filter(r => r && finite(r.value) && (spec.keepZero || r.value !== 0)).map((r, i) => Object.assign({}, r, {
      i, name: r.name == null || r.name === '' ? (r.id != null ? String(r.id) : '—') : String(r.name),
      bookName: r.bookName || bookName(r.book) || '', value: r.value }));
    rows.sort((a, b) => Math.abs(b.value) - Math.abs(a.value) || a.i - b.i);
    const sumAll = rows.reduce((s, r) => s + r.value, 0), gross = rows.reduce((s, r) => s + Math.abs(r.value), 0);
    const total = finite(spec.total) ? spec.total : sumAll;
    const mx = Math.max(Math.abs(total), gross, 0) / scale;
    const digits = finite(spec.digits) ? spec.digits : mx >= 1000 ? 0 : mx >= 10 ? 1 : mx >= 0.1 ? 2 : 3;
    const tol = 0.5 * Math.pow(10, -digits) * scale;   // меньше половины последнего знака — округление, не остаток
    const rest = total - sumAll;
    const restRow = Math.abs(rest) >= Math.max(tol, 1e-9 * Math.max(gross, Math.abs(total))) ? { rest: true, name: REST, value: rest } : null;
    const base = Math.abs(total) > 1e-12 * Math.max(1, gross) ? total : null;   // итог ≈ 0 — доли от брутто
    const share = v => base != null ? v / base : gross ? Math.abs(v) / gross : null;
    const q = (ui.q || '').trim().toLowerCase();
    const shown = q ? rows.filter(r => [r.name, r.bookName, r.book, r.issuer, r.isin, r.segment, r.sub].concat(Object.values(r.extra || {}))
      .some(x => x != null && String(x).toLowerCase().includes(q))) : rows;
    let cum = 0;
    shown.forEach(r => { r.share = share(r.value); cum += r.value; r.cum = share(cum); });
    const gk = ui.group || '';
    let groups = null;
    if (gk) {
      const by = new Map();
      shown.forEach(r => { const k = groupOf(r, gk); if (!by.has(k)) by.set(k, { key: k, rows: [], value: 0 }); const g = by.get(k); g.rows.push(r); g.value += r.value; });
      groups = [...by.values()].sort((a, b) => Math.abs(b.value) - Math.abs(a.value));
      let c = 0;   // накопленная доля — в порядке показа: группа за группой
      groups.forEach(g => { g.share = share(g.value); g.rows.forEach(r => { c += r.value; r.cum = share(c); }); });
    }
    const shownSum = shown.reduce((s, r) => s + r.value, 0);
    return { spec, scale, digits, rows, shown, groups, total, sumAll, shownSum, rest: restRow, gross, share,
      filtered: shown.length !== rows.length, n: rows.length };
  }

  /* ---------- стили: переменные страницы (у страниц разные имена), свои — запасные ---------- */
  const CSS = '.dd-scrim{position:fixed;inset:0;background:rgba(5,9,14,.42);z-index:50}' +
    '.dd{--dd-bg:var(--panel,var(--surface,#fff));--dd-bg2:var(--panel-2,var(--surface-2,#f4f6f8));--dd-fg:var(--fg,var(--text,#1b2129));' +
    '--dd-fg2:var(--fg-2,var(--text-2,#4d5866));--dd-fg3:var(--fg-3,var(--muted,#6b7685));--dd-line:var(--line,#d9dee5);' +
    '--dd-line2:var(--line-2,var(--line-strong,#c5ced8));--dd-acc:var(--accent,#2f6fd6);--dd-accw:var(--accent-weak,var(--accent-soft,rgba(47,111,214,.12)));' +
    '--dd-pos:var(--pos,#2a78d6);--dd-neg:var(--neg,#d03b3b);--dd-negtx:var(--neg-tx,var(--neg,#b8302f));' +
    '--dd-mono:var(--font-mono,var(--font-num,ui-monospace,"SFMono-Regular",Consolas,monospace));' +
    'position:fixed;top:0;right:0;bottom:0;width:min(900px,100vw);z-index:51;display:flex;flex-direction:column;background:var(--dd-bg);' +
    'color:var(--dd-fg);border-left:1px solid var(--dd-line2);box-shadow:0 12px 40px rgba(0,0,0,.35);' +
    'font:13px/1.4 var(--font-ui,var(--font-sans,system-ui,-apple-system,"Segoe UI",sans-serif))}' +
    '.dd *{box-sizing:border-box}.dd[hidden],.dd-scrim[hidden]{display:none}' +
    '.dd-h{padding:14px 18px 10px;border-bottom:1px solid var(--dd-line)}' +
    '.dd-k{font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--dd-fg3);display:flex;justify-content:space-between;gap:10px;align-items:center}' +
    '.dd-t{margin:4px 0 6px;font-size:17px;font-weight:600;line-height:1.25}' +
    '.dd-tot{display:flex;gap:6px 18px;align-items:baseline;flex-wrap:wrap;font-size:12px;color:var(--dd-fg2)}' +
    '.dd-tot b{font:600 22px var(--dd-mono);color:var(--dd-fg);letter-spacing:-.01em}.dd-tot .u{color:var(--dd-fg3)}' +
    '.dd-note{margin:8px 0 0;font-size:12px;color:var(--dd-fg2);line-height:1.45}' +
    '.dd-x{background:transparent;border:1px solid var(--dd-line2);border-radius:2px;color:var(--dd-fg2);padding:2px 9px;cursor:pointer;font:inherit;font-size:12px}' +
    '.dd-x:hover{color:var(--dd-fg);border-color:var(--dd-acc)}' +
    '.dd-bar{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;padding:8px 18px;border-bottom:1px solid var(--dd-line);background:var(--dd-bg2)}' +
    '.dd .dd-q{background:var(--dd-bg);border:1px solid var(--dd-line2);border-radius:2px;padding:3px 8px;font:inherit;font-size:12px;color:var(--dd-fg);width:220px;max-width:100%}' +
    '.dd .dd-g{display:inline-flex;border:1px solid var(--dd-line2);border-radius:2px;overflow:hidden;flex-wrap:wrap}' +
    '.dd-g button{background:transparent;border:0;border-right:1px solid var(--dd-line2);padding:3px 9px;font:inherit;font-size:12px;cursor:pointer;color:var(--dd-fg2)}' +
    '.dd-g button:last-child{border-right:0}.dd-g button[aria-pressed="true"]{background:var(--dd-accw);color:var(--dd-fg)}' +
    '.dd-b{background:transparent;border:1px solid var(--dd-line2);border-radius:2px;padding:3px 9px;font:inherit;font-size:12px;cursor:pointer;color:var(--dd-fg2)}' +
    '.dd-b:hover{color:var(--dd-fg);border-color:var(--dd-acc)}.dd-st{font-size:11.5px;color:var(--dd-fg3)}' +
    '.dd-w{flex:1;overflow:auto;min-height:0}' +
    '.dd table{border-collapse:collapse;width:100%;font-size:12px}' +
    '.dd th,.dd td{padding:4px 8px;border-bottom:1px solid var(--dd-line);text-align:left;vertical-align:top}' +
    '.dd th{position:sticky;top:0;z-index:1;background:var(--dd-bg2);color:var(--dd-fg3);font-weight:500;font-size:11px;white-space:nowrap}' +
    '.dd td.n,.dd th.n{text-align:right;white-space:nowrap;font-family:var(--dd-mono);font-variant-numeric:tabular-nums}' +
    '.dd td.neg{color:var(--dd-negtx)}.dd td.i{color:var(--dd-fg3);font-family:var(--dd-mono);width:1%}' +
    '.dd td.nm{min-width:260px;max-width:420px}.dd .nm b{font-weight:500}.dd .sb{display:block;font-size:11px;color:var(--dd-fg3);line-height:1.35;margin-top:1px}' +
    '.dd tr.r{cursor:default}.dd tr.r.pos{cursor:pointer}.dd tr.r:hover td{background:var(--dd-accw)}' +
    '.dd tr.gr td{background:var(--dd-bg2);font-weight:600;cursor:pointer}.dd tr.gr td .c{display:inline-block;width:12px;color:var(--dd-fg3)}' +
    '.dd tr.rest td{color:var(--dd-fg2);font-style:italic}.dd tfoot td{font-weight:600;background:var(--dd-bg2);border-top:1px solid var(--dd-line2)}' +
    '.dd .cb{display:inline-block;position:relative;width:64px;height:7px;background:var(--dd-line);border-radius:1px;vertical-align:middle;margin-right:6px;overflow:hidden}' +
    '.dd .cb i{position:absolute;left:0;top:0;bottom:0;background:var(--dd-acc);opacity:.75}' +
    '.dd .sh{display:inline-block;position:relative;width:46px;height:7px;vertical-align:middle;margin-right:6px}' +
    '.dd .sh::before{content:"";position:absolute;left:50%;top:-2px;bottom:-2px;width:1px;background:var(--dd-line2)}' +
    '.dd .sh i{position:absolute;top:0;bottom:0}.dd .sh i.p{left:50%;background:var(--dd-pos)}.dd .sh i.m{right:50%;background:var(--dd-neg)}' +
    '.dd a.lk{color:var(--dd-acc);font-size:11px;margin-left:6px}' +
    '.dd-f{padding:6px 18px;border-top:1px solid var(--dd-line);font-size:11px;color:var(--dd-fg3)}' +
    '@media (max-width:640px){.dd-h,.dd-bar,.dd-f{padding-inline:12px}.dd .cb{width:36px}.dd td.bk,.dd th.bk{display:none}}' +
    '[data-drill]{cursor:pointer}[data-drill]:hover,[data-drill]:focus-visible{text-decoration:underline dotted;text-underline-offset:3px;' +
    'outline:1px dashed var(--accent,#2f6fd6);outline-offset:2px}' +
    '@media print{.dd,.dd-scrim{display:none}}';

  /* ---------- панель ---------- */
  let el = null, scrim = null, cur = null, ui = { q: '', group: '' }, back = null, closed = new Set();
  function mount() {
    if (el) return;
    const st = document.createElement('style'); st.textContent = CSS; (document.head || document.body).appendChild(st);
    scrim = document.createElement('div'); scrim.className = 'dd-scrim'; scrim.hidden = true;
    el = document.createElement('aside'); el.className = 'dd'; el.hidden = true;
    el.setAttribute('role', 'dialog'); el.setAttribute('aria-modal', 'true'); el.setAttribute('aria-label', 'Из чего состоит число');
    document.body.appendChild(scrim); document.body.appendChild(el);
    scrim.addEventListener('click', close);
    el.addEventListener('click', onPanelClick);
    el.addEventListener('input', e => { if (e.target.classList && e.target.classList.contains('dd-q')) { ui.q = e.target.value; render(true); } });
    el.addEventListener('keydown', e => {
      if ((e.key === 'Enter' || e.key === ' ') && e.target.closest && e.target.closest('tr[data-i], tr[data-g]')) { e.preventDefault(); onPanelClick(e); }
    });
  }
  const cardOpen = () => !!(D.card && D.card.isOpen && D.card.isOpen());
  function rowHref(id) {
    const nav = window.DESK_NAV, page = 'desk-monitor.html';
    const h = nav && nav.href ? nav.href(page) : page;
    return h + '#card=' + encodeURIComponent(id);
  }
  function onPanelClick(e) {
    const t = e.target;
    if (t.closest('[data-dd="close"]')) return close();
    if (t.closest('[data-dd="csv"]')) return copyCsv();
    const g = t.closest('[data-dd-g]');
    if (g) { ui.group = g.getAttribute('data-dd-g'); closed = new Set(); return render(); }
    const a = t.closest('[data-dd-a]');
    if (a) { const x = (cur.spec.actions || [])[+a.getAttribute('data-dd-a')]; if (x && x.run) { close(); x.run(); } return; }
    if (t.closest('a')) return;
    const gr = t.closest('tr[data-g]');
    if (gr) { const k = gr.getAttribute('data-g'); if (closed.has(k)) closed.delete(k); else closed.add(k); return render(); }
    const tr = t.closest('tr[data-i]');
    if (tr) {
      const r = cur.rows.find(x => String(x.i) === tr.getAttribute('data-i'));
      if (r && r.id != null && D.card && D.card.open) D.card.open(r.id, tr);
    }
  }
  function html(m) {
    const s = m.spec, cols = s.columns || [], unit = s.unit ? ' ' + s.unit : '';
    const fmtT = v => num(v / m.scale, m.digits, s.signed);
    const pct = v => v == null || !isFinite(v) ? '—' : num(v * 100, 1) + '%';
    const card = !!(D.card && D.card.open);
    const tdv = r => {   // крошечный вклад — с лишними знаками (до +3), а не «0,000»
      const v = r.value / m.scale, cls = 'n' + (v < 0 ? ' neg' : '');
      let d = m.digits; while (v && d < m.digits + 3 && Math.abs(v) < 0.5 * Math.pow(10, -d)) d++;
      return `<td class="${cls}">${num(v, d, s.signed)}</td>`;
    };
    const shareBar = v => v == null ? '' : `<span class="sh"><i class="${v < 0 ? 'm' : 'p'}" style="width:${Math.min(50, Math.abs(v) * 50).toFixed(1)}%"></i></span>`;
    const cumBar = v => v == null ? '' : `<span class="cb"><i style="width:${Math.max(0, Math.min(100, v * 100)).toFixed(1)}%"></i></span>`;
    const colTd = (c, r) => { const v = r.extra ? r.extra[c.key] : null; const x = c.fmt ? c.fmt(v, r) : v; return `<td${typeof v === 'number' ? ' class="n"' : ''}>${esc(x == null || x === '' ? '—' : x)}</td>`; };
    const rowTr = (r, n) => {
      const pos = r.id != null;
      const link = pos && !card ? `<a class="lk" href="${esc(rowHref(r.id))}" title="Карточка позиции в Desk Monitor">карточка →</a>` : '';
      return `<tr class="r${pos && card ? ' pos' : ''}" data-i="${r.i}"${pos && card ? ' tabindex="0" title="Открыть карточку позиции"' : ''}><td class="i">${n}</td>` +
        `<td class="nm"><b>${esc(r.name)}</b>${link}${r.isin ? ` <span class="sb" style="display:inline;font-family:var(--dd-mono)">${esc(r.isin)}</span>` : ''}${r.sub ? `<span class="sb">${esc(r.sub)}</span>` : ''}</td>` +
        `<td class="bk">${esc(r.bookName || '—')}</td><td>${esc(r.issuer || '—')}</td>${cols.map(c => colTd(c, r)).join('')}` +
        `${tdv(r)}<td class="n">${shareBar(r.share)}${pct(r.share)}</td><td class="n">${cumBar(r.cum)}${pct(r.cum)}</td></tr>`;
    };
    const ncol = 6 + cols.length;
    let body = '';
    if (m.groups) {
      m.groups.forEach(g => {
        const open = !closed.has(g.key);
        body += `<tr class="gr" data-g="${esc(g.key)}" tabindex="0" aria-expanded="${open}"><td class="i"><span class="c">${open ? '▾' : '▸'}</span></td><td colspan="${3 + cols.length}">${esc(g.key)} <span class="sb" style="display:inline">· ${g.rows.length}</span></td>` +
          `<td class="n${g.value < 0 ? ' neg' : ''}">${fmtT(g.value)}</td><td class="n">${pct(g.share)}</td><td></td></tr>`;
        if (open) g.rows.forEach((r, j) => { body += rowTr(r, j + 1); });
      });
    } else m.shown.forEach((r, j) => { body += rowTr(r, j + 1); });
    if (!m.shown.length) body = `<tr><td colspan="${ncol}" style="padding:16px 8px;color:var(--dd-fg3)">${m.n ? 'Ничего не найдено по поиску.' : 'Нет позиций с ненулевым вкладом.'}</td></tr>`;
    if (m.rest && !m.filtered) body += `<tr class="rest" title="Итог числа минус сумма строк: позиции без значения, фондирование книги, округление"><td class="i"></td><td colspan="${3 + cols.length}">${REST}</td>${tdv(m.rest)}<td class="n">${pct(m.share(m.rest.value))}</td><td></td></tr>`;
    const foot = m.filtered
      ? `<tr><td></td><td colspan="${3 + cols.length}">Показано ${m.shown.length} из ${m.n}</td><td class="n">${fmtT(m.shownSum)}</td><td class="n">${pct(m.share(m.shownSum))}</td><td></td></tr>`
      : `<tr><td></td><td colspan="${3 + cols.length}">Итого · ${m.n} ${plural(m.n, 'позиция', 'позиции', 'позиций')}</td><td class="n">${fmtT(m.total)}</td><td class="n">${m.share(m.total) == null ? '—' : '100,0%'}</td><td></td></tr>`;
    const groups = GROUPS.concat(s.groups || []);
    return `<div class="dd-h"><div class="dd-k"><span>Из чего состоит</span><button type="button" class="dd-x" data-dd="close" aria-label="Закрыть">Закрыть ✕</button></div>` +
      `<h2 class="dd-t" id="dd-title">${esc(s.title || 'Состав числа')}</h2>` +
      `<div class="dd-tot"><span><b>${fmtT(m.total)}</b><span class="u">${esc(unit)}</span></span><span>${m.n} ${plural(m.n, 'позиция', 'позиции', 'позиций')} с вкладом</span>` +
      (m.rest ? `<span title="Итог минус сумма строк">остаток ${fmtT(m.rest.value)}</span>` : '') + `</div>` +
      (s.note ? `<p class="dd-note">${esc(s.note)}</p>` : '') + `</div>` +
      `<div class="dd-bar"><input class="dd-q" type="search" placeholder="Поиск: позиция, ISIN, книга, эмитент…" aria-label="Поиск по строкам" value="${esc(ui.q)}">` +
      `<span class="dd-g" role="group" aria-label="Группировка">${groups.map(g => `<button type="button" data-dd-g="${esc(g.key)}" aria-pressed="${ui.group === g.key}">${esc(g.label)}</button>`).join('')}</span>` +
      `<button type="button" class="dd-b" data-dd="csv" title="Скопировать строки в буфер (CSV, разделитель «;»)">Копировать CSV</button>` +
      (s.actions || []).map((a, i) => `<button type="button" class="dd-b" data-dd-a="${i}">${esc(a.label)}</button>`).join('') +
      `<span class="dd-st" id="dd-st" aria-live="polite"></span></div>` +
      `<div class="dd-w"><table><thead><tr><th>#</th><th>Позиция</th><th class="bk">Книга</th><th>Эмитент</th>${cols.map(c => `<th>${esc(c.label)}</th>`).join('')}` +
      `<th class="n">Вклад${esc(unit)}</th><th class="n">Доля</th><th class="n">Накопл.</th></tr></thead><tbody>${body}</tbody><tfoot>${foot}</tfoot></table></div>` +
      `<div class="dd-f">Сортировка — по |вкладу| · доля — от итога${m.share(m.total) == null ? ' (итог ≈ 0: от суммы модулей)' : ''} · ${card ? 'клик по строке — карточка позиции · ' : ''}Esc — закрыть</div>`;
  }
  function plural(n, a, b, c) { const m10 = n % 10, m100 = n % 100; return m10 === 1 && m100 !== 11 ? a : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? b : c; }
  function render(keepFocus) {
    cur = model(cur.spec, ui);
    api.last = cur;
    if (!el) return;
    const ae = document.activeElement, inQ = keepFocus && ae && ae.classList && ae.classList.contains('dd-q'), pos = inQ ? ae.selectionStart : 0;
    el.innerHTML = html(cur);
    if (inQ) { const q = el.querySelector('.dd-q'); if (q && q.focus) { q.focus(); try { q.setSelectionRange(pos, pos); } catch (e) {} } }
  }
  function open(spec) {
    if (!spec) return null;
    mount();
    back = document.activeElement;
    ui = { q: '', group: '' }; closed = new Set();
    cur = { spec };
    render();
    el.hidden = false; scrim.hidden = false;
    const x = el.querySelector && el.querySelector('[data-dd="close"]'); if (x && x.focus) x.focus();
    return cur;
  }
  function close() {
    if (!el || el.hidden) return;
    el.hidden = true; scrim.hidden = true;
    if (back && back.focus && document.body.contains && document.body.contains(back)) back.focus();
  }
  const isOpen = () => !!el && !el.hidden;
  function csvText(m) {
    const s = m.spec, cols = s.columns || [], q = v => { const x = v == null ? '' : String(v); return /[";\n]/.test(x) ? '"' + x.replace(/"/g, '""') + '"' : x; };
    const n = v => v == null || !isFinite(v) ? '' : String(+(v).toFixed(10)).replace('.', ',');
    const lines = [['Позиция', 'ISIN', 'Книга', 'Эмитент', 'Сегмент'].concat(cols.map(c => c.label), ['Вклад' + (s.unit ? ', ' + s.unit : ''), 'Доля, %'])];
    m.shown.forEach(r => lines.push([r.name, r.isin, r.bookName, r.issuer, r.segment].concat(cols.map(c => r.extra ? r.extra[c.key] : ''), [n(r.value / m.scale), n(r.share == null ? null : r.share * 100)])));
    if (m.rest && !m.filtered) lines.push([REST, '', '', '', ''].concat(cols.map(() => ''), [n(m.rest.value / m.scale), n(m.share(m.rest.value) == null ? null : m.share(m.rest.value) * 100)]));
    lines.push(['Итого', '', '', '', ''].concat(cols.map(() => ''), [n((m.filtered ? m.shownSum : m.total) / m.scale), '']));
    return lines.map(l => l.map(q).join(';')).join('\n');
  }
  function copyCsv() {
    const txt = csvText(cur), st = el.querySelector('#dd-st');
    const ok = () => { if (st) st.textContent = 'Скопировано: ' + cur.shown.length + ' строк'; };
    const fallback = () => {
      const ta = document.createElement('textarea'); ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select();
      try { document.execCommand('copy'); ok(); } catch (e) { if (st) st.textContent = 'Не удалось скопировать'; }
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(ok, fallback); else fallback();
  }

  /* ---------- привязка к элементам ---------- */
  const REG = new Map(); let seq = 0;
  const HINT = 'Нажмите — из каких позиций состоит';
  function attr(key, fn, hint) {
    if (typeof key === 'function') { hint = fn; fn = key; key = 'a' + (++seq); if (seq % 500 === 0) [...REG.keys()].filter(k => /^a\d+$/.test(k) && +k.slice(1) < seq - 2000).forEach(k => REG.delete(k)); }
    REG.set(String(key), fn);
    return ` data-drill="${esc(key)}" tabindex="0" role="button" title="${esc(hint || HINT)}"`;
  }
  function attach(node, fn, hint) {
    if (!node) return node;
    const key = 'e' + (++seq);
    REG.set(key, fn);
    node.setAttribute('data-drill', key);
    if (!node.hasAttribute('tabindex')) node.setAttribute('tabindex', '0');
    node.setAttribute('role', 'button');
    if (!node.getAttribute('title')) node.setAttribute('title', hint || HINT);
    return node;
  }
  const specOf = key => { const f = REG.get(String(key)); return f ? f() : null; };
  // спецификация под ключом без разметки (столбик графика: chart.on('click', … open(spec(key))))
  const put = (key, fn) => { REG.set(String(key), fn); return String(key); };
  function trigger(t, e) {
    const k = t.getAttribute('data-drill'), f = REG.get(k);
    if (!f) return;
    if (e) { e.preventDefault(); e.stopPropagation(); if (e.stopImmediatePropagation) e.stopImmediatePropagation(); }
    let spec = null;
    try { spec = f(); } catch (err) { console.error('drill', err); return; }
    if (spec) open(spec);
  }
  if (document.addEventListener) {
    document.addEventListener('click', e => {
      const t = e.target && e.target.closest && e.target.closest('[data-drill]');
      if (t && !(el && el.contains(t))) trigger(t, e);
    }, true);
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && isOpen() && !cardOpen()) { close(); e.preventDefault(); if (e.stopImmediatePropagation) e.stopImmediatePropagation(); return; }
      if ((e.key === 'Enter' || e.key === ' ') && e.target && e.target.closest) {
        const t = e.target.closest('[data-drill]');
        if (t && !(el && el.contains(t))) trigger(t, e);
      }
    }, true);
  }

  /* ---------- строки из позиций ---------- */
  const issuerOf = p => p.issuer || p.issuerGroup || null;
  function rows(list, f, o) {
    o = o || {};
    const out = [];
    (list || []).forEach(p => {
      let v; try { v = f(p); } catch (e) { v = null; }
      if (!finite(v)) return;
      out.push({ id: p.id, name: p.name, book: p.book, issuer: issuerOf(p), isin: p.isin || null, segment: p.segment || null, value: v,
        sub: o.sub ? o.sub(p) : undefined, extra: o.extra ? o.extra(p) : undefined });
    });
    return out;
  }
  // Спецификация по позициям: итог — сумма f по list, если spec.total не задан.
  function pos(list, f, spec) {
    const r = rows(list, f, spec);
    const o = Object.assign({}, spec || {}, { rows: (spec && spec.extraRows ? r.concat(spec.extraRows) : r) });
    delete o.sub; delete o.extra; delete o.extraRows;
    if (!finite(o.total)) o.total = o.rows.reduce((s, x) => s + x.value, 0);
    return o;
  }
  // Компонента PnL k по позициям с PnL: метод, оценка и причина («Прочее») у каждой строки.
  const QL = { exact: 'точно', assumed: 'допущение', market_value: 'рыночная стоимость', stale_price: 'прошлая цена', model_price: 'модельная цена', par_proxy: 'по номиналу', reported_value: 'по отчёту' };
  const qual = p => QL[(p.extra && p.extra.quality) || ''] || (p.extra && p.extra.quality) || '—';
  const pnlOk = U.pnlOk || (p => !!p.pnl && p.pnl.available !== false);
  // как посчитан PnL позиции: причина «Прочего» (R21, в ней уже сказан метод), иначе метод и пометка
  function pnlWhy(p) {
    const e = p.pnl || {}, m = e.methodText || e.method;
    if (e.otherWhy) return e.otherWhy;
    return [m, e.flag && !(m && m.includes(e.flag)) ? e.flag : null].filter(Boolean).join(' · ') || null;
  }
  function pnl(list, k, o) {
    o = o || {};
    const part = (U.PNL_PART || {})[k] || { label: k };
    const ok = (list || []).filter(pnlOk), f = o.value || (p => p.pnl[k]);
    const nNa = (list || []).length - ok.length;
    const notes = [part.tip, nNa ? `Без PnL ${nNa} ${plural(nNa, 'позиция', 'позиции', 'позиций')} — в сумму не входят.` : '', o.note || ''].filter(Boolean).join(' ');
    return pos(ok, f, Object.assign({ title: 'PnL дня · ' + part.label, unit: 'тыс ₽', scale: 1e3, signed: true,
      sub: p => pnlWhy(p),
      extra: p => ({ method: (p.pnl && (p.pnl.methodText || p.pnl.method)) || '—', quality: qual(p) }),
      columns: [{ key: 'quality', label: 'Оценка' }],
      groups: [{ key: 'method', label: 'Метод' }, { key: 'quality', label: 'Оценка' }] }, o, { note: notes }));
  }

  const api = { open, close, isOpen, attach, attr, put, spec: specOf, model, rows, pos, pnl, pnlWhy, qual, csv: () => cur ? csvText(cur) : '', last: null, REST };
  D.drill = api;
})();
