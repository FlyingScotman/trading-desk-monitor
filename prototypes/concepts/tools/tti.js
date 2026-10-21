// Время до готовности страниц концептов на сервере (R23): от начала перехода до конца события load (страницы
// строятся синхронно в своих скриптах, данные — синхронные запросы), лучшее из --n заходов, первый заход отдельно.
// node prototypes/concepts/tools/tti.js --base http://127.0.0.1:8197/concepts/ [--n 3] [page.html[#c08mode] ...]
// c08-calendar.html#rub / #fx — календарь в режиме рынка (его данные — отдельный запрос /api/v1/calendar/market).
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
let args = process.argv.slice(2), base = null, n = 3;
const take = k => { const i = args.indexOf(k); if (i < 0) return null; const v = args[i + 1]; args.splice(i, 2); return v; };
base = take('--base'); n = +(take('--n') || 3);
const pages = args.length ? args : ['c01-cockpit.html', 'c03-blotter.html', 'c08-calendar.html', 'c08-calendar.html#rub', 'c08-calendar.html#fx', 'c11-history.html'];
(async () => {
  const b = await chromium.launch();
  const out = {};
  for (const spec of pages) {
    const [name, mode] = spec.split('#');
    const times = [];
    const ctx = await b.newContext({ viewport: { width: 1600, height: 1000 } });  // один контекст: кэш браузера как у владельца
    for (let k = 0; k < n; k++) {
      const pg = await ctx.newPage();
      const errs = [];
      pg.on('pageerror', e => errs.push(e.message));
      await pg.addInitScript(m => { try { localStorage.setItem('c08', JSON.stringify({ mode: m || 'pf', hz: '12M' })); } catch (e) { } }, mode || null);
      await pg.goto(base + name, { waitUntil: 'load', timeout: 120000 });
      const t = await pg.evaluate(() => { const e = performance.getEntriesByType('navigation')[0]; return Math.round(e.loadEventEnd - e.startTime); });
      times.push(t);
      if (errs.length) times.push('errors: ' + errs.slice(0, 2).join('; '));
      await pg.close();
    }
    await ctx.close();
    const nums = times.filter(x => typeof x === 'number');
    out[spec] = { first_ms: nums[0], best_ms: Math.min(...nums), all: times };
    console.log(spec.padEnd(26), JSON.stringify(out[spec]));
  }
  await b.close();
})();
