// Блокировки главного потока при открытии страниц концептов на сервере (TDM#629): самая длинная задача (Long Tasks API,
// от 50 мс), сумма задач дольше 50 мс сверх 50 мс (total blocking time), синхронные запросы (по числу вызовов
// XMLHttpRequest с async=false), время до конца load и до «данные на экране» (метка html[data-ready] и её время
// html[data-ready-at], их ставит страница, ждущая данные: KIT.whenData), ошибки консоли. С --dump DIR — ещё и текст страницы для сравнения до и после.
// node prototypes/concepts/tools/longtask.js --base http://127.0.0.1:8197/concepts/ [--dump DIR] [--n 1] [page.html ...]
const { chromium } = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { return require('playwright'); } })();
const fs = require('fs'), path = require('path');
let args = process.argv.slice(2);
const take = k => { const i = args.indexOf(k); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v; };
const base = take('--base'), dump = take('--dump'), n = +(take('--n') || 1);
if (!base) { console.log('нужен --base'); process.exit(2); }
const pages = args.length ? args : fs.readdirSync(path.resolve(__dirname, '..')).filter(f => /^c\d\d-.*\.html$/.test(f)).sort();
if (dump) fs.mkdirSync(dump, { recursive: true });
(async () => {
  const b = await chromium.launch();
  for (const name of pages) {
    const rows = [];
    for (let k = 0; k < n; k++) {
      const ctx = await b.newContext({ viewport: { width: 1600, height: 1000 } }), pg = await ctx.newPage();
      const errs = [];
      pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
      pg.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
      await pg.addInitScript(() => {
        window.__lt = []; window.__syncXhr = 0;
        try { new PerformanceObserver(l => l.getEntries().forEach(e => window.__lt.push([Math.round(e.startTime), Math.round(e.duration)]))).observe({ type: 'longtask', buffered: true }); } catch (e) { }
        const o = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function (m, u, a) { if (a === false) window.__syncXhr++; return o.apply(this, arguments); };
      });
      const t0 = Date.now();
      await pg.goto(base + name, { waitUntil: 'load', timeout: 180000 });
      const loadMs = await pg.evaluate(() => Math.round(performance.getEntriesByType('navigation')[0].loadEventEnd));
      // данные на экране: метка страницы (html[data-ready]) или спокойствие сети
      let ready = null;
      try { await pg.waitForFunction(() => document.documentElement.dataset.ready === '1', null, { timeout: 4000 }); ready = await pg.evaluate(() => +document.documentElement.dataset.readyAt); } catch (e) { console.log('ready wait:', String(e.message).split('\n')[0]); }
      await pg.waitForLoadState('networkidle', { timeout: 120000 }).catch(() => { });
      await pg.waitForTimeout(1500);
      const r = await pg.evaluate(() => ({ lt: window.__lt, sx: window.__syncXhr }));
      const longest = r.lt.reduce((m, x) => Math.max(m, x[1]), 0), tbt = r.lt.reduce((s, x) => s + Math.max(0, x[1] - 50), 0);
      rows.push({ load_ms: loadMs, ready_ms: ready, longest_ms: longest, tbt_ms: tbt, tasks_over_50: r.lt.length, sync_xhr: r.sx, errors: errs.slice(0, 3), wall_s: +((Date.now() - t0) / 1000).toFixed(1) });
      if (dump && k === 0) fs.writeFileSync(path.join(dump, name.replace('.html', '.txt')), await pg.evaluate(() => document.body.innerText));
      await ctx.close();
    }
    console.log(name.padEnd(22), JSON.stringify(rows.length === 1 ? rows[0] : rows));
  }
  await b.close();
})();
