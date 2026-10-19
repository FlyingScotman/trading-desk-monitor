/*
 * Плашка «какие данные на экране» + выбор даты/прогона (только при работе через сервер).
 * Источник — window.DESK.source: { kind: 'db'|'demo', runId, runType, publishedAt, asOf }.
 */
(function () {
  'use strict';
  var D = window.DESK, src = (D && D.source) || { kind: 'demo' };
  var http = /^https?:$/.test(location.protocol);

  function el(tag, attrs, text) {
    var e = document.createElement(tag);
    for (var k in attrs) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function go(params) {
    var u = new URL(location.href);
    ['as_of', 'run_id', 'demo'].forEach(function (k) { u.searchParams.delete(k); });
    for (var k in params) if (params[k] != null && params[k] !== '') u.searchParams.set(k, params[k]);
    location.href = u.toString();
  }

  function mount() {
    // «Тестовые данные» — только для демо-набора
    if (src.kind === 'db') document.querySelectorAll('[title="Все цифры вымышленные"]').forEach(function (x) { x.style.display = 'none'; });

    var css = '#desk-src{position:fixed;left:10px;bottom:10px;z-index:60;display:flex;gap:8px;align-items:center;' +
      'font:12px/1.3 system-ui,sans-serif;padding:5px 8px;border-radius:4px;background:rgba(20,24,30,.88);color:#e8ecf1;' +
      'box-shadow:0 2px 8px rgba(0,0,0,.25);max-width:calc(100vw - 20px);flex-wrap:wrap}' +
      '#desk-src b{font-weight:600}#desk-src .demo{color:#ffcf66}#desk-src select{font:inherit;background:#2a313b;color:#e8ecf1;border:1px solid #47515e;border-radius:3px;padding:1px 4px}' +
      '#desk-src button{font:inherit;background:none;border:0;color:#9fb3c8;cursor:pointer;padding:0 2px}' +
      '@media print{#desk-src{display:none}}';
    document.head.appendChild(el('style', {}, css));
    var box = el('div', { id: 'desk-src', role: 'status' });
    if (src.kind === 'db') {
      box.appendChild(el('span', {}, 'Данные: '));
      box.appendChild(el('b', {}, D.asOf + ' · ' + (src.runType || '') + ' #' + src.runId));
      if (src.publishedAt) box.appendChild(el('span', {}, 'опубл. ' + src.publishedAt.replace('T', ' ').slice(0, 16)));
    } else {
      box.appendChild(el('b', { 'class': 'demo' }, 'ДЕМО-данные'));
      if (window.DESK_MISSING) box.appendChild(el('span', {}, '(' + window.DESK_MISSING + ')'));
    }
    document.body.appendChild(box);
    if (!http) return;

    fetch('api/v1/runs?limit=200&status=complete').then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j || !j.runs || !j.runs.length) return;
      var sel = el('select', { 'aria-label': 'Дата и прогон' });
      sel.appendChild(el('option', { value: '' }, 'последний прогон'));
      j.runs.forEach(function (r) {
        var day = r.business_date || r.as_of, done = r.completed_at || r.published_at;  // as_of / published_at: compat aliases
        var o = el('option', { value: r.run_id }, day + ' · ' + r.run_type + ' #' + r.run_id + (done ? ' · ' + String(done).slice(11, 16) : ''));
        if (src.kind === 'db' && String(r.run_id) === String(src.runId) && new URL(location.href).searchParams.get('run_id')) o.selected = true;
        sel.appendChild(o);
      });
      var demo = el('option', { value: 'demo' }, 'демо-данные'); if (src.kind !== 'db') demo.selected = true;
      sel.appendChild(demo);
      sel.addEventListener('change', function () {
        if (sel.value === 'demo') go({ demo: 1 }); else go({ run_id: sel.value });
      });
      box.appendChild(sel);
    }).catch(function () {});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount); else mount();
})();
