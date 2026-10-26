// Смоук-проверка концептов: открыть каждую страницу, собрать ошибки консоли, снять скриншот,
// щёлкнуть первую кликабельную цифру (разбор) и первую строку позиции (карточка); на ширине 390 px страница
// не прокручивается вбок ни на одной вкладке или раскладке (#263: scrollWidth = innerWidth).
// node prototypes/concepts/tools/smoke.js [out_dir] [--base http://127.0.0.1:8199/concepts/] [page.html ...]
// --base: the pages served by desk serve (real runs, lib/real.js) instead of the files (demo data)
// --login USER:PASSWORD: sign in first (desk serve with sign-in on, backend api/auth.py); the pages share the session
// --all-runs (with --base, #308): every page on every run of the server (GET /api/v1/runs, oldest first) with ?run_id=,
//   then once more without it (the default run); a console error or an uncaught exception on any run fails the page
// --run ID (with --base): every page on that one run (?run_id=ID); a run the server does not have (GET /api/v1/runs/ID 404)
//   must show the empty state (#319). With --all-runs (and --run of an existing run) the empty state is a FAIL
// Playwright: the global install of the dev machine, otherwise a resolvable `playwright` (CI: NODE_PATH)
const { chromium } = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { return require('playwright'); } })();
const path = require('path'), fs = require('fs');
const dir = path.resolve(__dirname, '..');
const out = path.resolve(process.argv[2] || path.join(dir, '.shots'));
fs.mkdirSync(out, { recursive: true });
let pages = process.argv.slice(3), base = null;
const bi = pages.indexOf('--base'); if (bi >= 0) { base = pages[bi + 1]; pages.splice(bi, 2); }
let login = null; const li = pages.indexOf('--login'); if (li >= 0) { login = pages[li + 1]; pages.splice(li, 2); }
let allRuns = false; const ai = pages.indexOf('--all-runs'); if (ai >= 0) { allRuns = true; pages.splice(ai, 1); }
let oneRun = null; const ri = pages.indexOf('--run'); if (ri >= 0) { oneRun = pages[ri + 1]; pages.splice(ri, 2); }
if ((allRuns || oneRun) && !base) { console.log('--all-runs / --run need --base'); process.exit(2); }
if (!pages.length) pages = fs.readdirSync(dir).filter(f => /\.html$/.test(f));
(async () => {
  const b = await chromium.launch();
  const ctx = await b.newContext();  // one session for every page (--login)
  if (login) {
    const [username, ...pw] = login.split(':'), origin = new URL(base).origin;
    const r = await ctx.request.post(origin + '/api/v1/auth/login', { data: { username, password: pw.join(':') } });
    if (!r.ok()) { console.log('FAIL login ' + username + ': ' + r.status()); process.exit(1); }
    console.log('ok   signed in as ' + username);
  }
  // прогоны (#308): [null] — как раньше, один заход без ?run_id=; --all-runs — каждый прогон сервера и затем без него
  let runs = [null];
  // --run ID: есть ли такой прогон на сервере (#319). Нет (404; 422 — не номер) — допустимо только пустое состояние;
  // есть — только настоящий прогон, как в --all-runs (там номера из /api/v1/runs: пустое состояние — всегда FAIL)
  let oneMissing = false;
  if (oneRun) {
    runs = [oneRun];
    const r = await ctx.request.get(new URL(base).origin + '/api/v1/runs/' + encodeURIComponent(oneRun));
    if (r.status() === 404 || r.status() === 422) oneMissing = true;
    else if (!r.ok()) { console.log('FAIL GET /api/v1/runs/' + oneRun + ': ' + r.status()); process.exit(1); }
    console.log('run ' + oneRun + (oneMissing ? ': not on the server — expecting the empty state' : ': on the server'));
  }
  if (allRuns) {
    const r = await ctx.request.get(new URL(base).origin + '/api/v1/runs');
    if (!r.ok()) { console.log('FAIL GET /api/v1/runs: ' + r.status()); process.exit(1); }
    runs = (await r.json()).runs.map(x => String(x.run_id)).sort((a, b) => a - b).concat([null]);
    console.log('runs: ' + runs.map(x => x || 'default').join(', '));
  }
  let bad = 0;
  for (const run of runs) for (const name of pages) {
    const rt = run ? '-run' + run : '', shot = sfx => path.join(out, name.replace('.html', rt + sfx + '.png'));
    const pg = await ctx.newPage(); await pg.setViewportSize({ width: 1600, height: 1000 });
    const errs = [];
    pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await pg.goto(base ? base + name + (run ? '?run_id=' + encodeURIComponent(run) : '') : 'file://' + path.join(dir, name));
    await pg.waitForTimeout(900);
    const src = await pg.evaluate(() => window.CONCEPTS_SOURCE || null);
    // --run ID прогона, которого на сервере нет (#319): сервер отвечает not_found — мониторы показывают пустое
    // состояние (.k-norun), C14 / C15 работают без прогона; это не ошибка, остальное проверяется как обычно (390 px…).
    // Прогон есть — пустое состояние ошибка; такой прогон, наоборот, показанный как настоящий — тоже
    if (oneMissing && src === 'real' && /^c\d/.test(name)) errs.push('run ' + run + ' is not on the server, yet the page shows a run');
    const norun = oneMissing && src === 'empty' && await pg.evaluate(() => !!document.querySelector('.k-norun') || document.documentElement.getAttribute('data-run') === 'optional');
    if (base && /^c\d/.test(name) && src !== 'real' && !norun) errs.push('data source: ' + src + ', not the server run');
    await pg.screenshot({ path: shot('') });
    const dr = await pg.$('[data-dr]');
    if (dr) { await dr.click(); await pg.waitForTimeout(300); await pg.screenshot({ path: shot('-drill') }); await pg.keyboard.press('Escape'); }
    const row = await pg.$(base ? 'tr.row[data-id]' : 'tr.row[data-id^="P"]');  // на прогоне id позиции — «книга|бумага»
    if (row) { await row.click(); await pg.waitForTimeout(500); await pg.screenshot({ path: shot('-card') }); await pg.keyboard.press('Escape'); }
    // узкий экран (#263): на 390 px страница не прокручивается вбок — широкое содержимое прокручивается в своей плитке.
    // Проверяется каждая вкладка страницы (C01 .k-tabs) и каждая раскладка (C06 .presets), а не только открытая
    await pg.setViewportSize({ width: 390, height: 800 }); await pg.waitForTimeout(400);
    const narrow = async tag => {
      const wide = await pg.evaluate(() => [document.documentElement.scrollWidth, window.innerWidth]);
      if (wide[0] !== wide[1]) { errs.push('390px' + (tag ? ' «' + tag + '»' : '') + ': scrollWidth ' + wide[0] + ' ≠ innerWidth ' + wide[1]); await pg.screenshot({ path: shot('-390' + (tag ? '-' + errs.length : '')) }); }
      // переключатель (#272): все кнопки видны без прокрутки — переключатель не прокручивается внутри себя, кнопки в окне
      const hid = await pg.evaluate(() => [...document.querySelectorAll('.k-seg')].filter(s => s.offsetParent).flatMap(s => {
        const r = s.getBoundingClientRect();
        return [...s.querySelectorAll('button')].filter(b => { const q = b.getBoundingClientRect(); return s.scrollWidth > s.clientWidth + 1 || q.right > Math.min(r.right, innerWidth) + 1 || q.left < Math.max(r.left, 0) - 1; }).map(b => b.textContent.trim());
      }));
      if (hid.length) errs.push('390px' + (tag ? ' «' + tag + '»' : '') + ': кнопки переключателя за краем: ' + hid.slice(0, 6).join(', '));
    };
    const TABS = '.k-tabs button, .k-top .presets [data-p]';
    const nTabs = (await pg.$$(TABS)).length;
    if (!nTabs) await narrow('');
    for (let i = 0; i < nTabs; i++) {
      const tab = (await pg.$$(TABS))[i];  // вкладки перерисовываются после щелчка — каждый раз заново
      const tag = (await tab.textContent()).trim(); await tab.click(); await pg.waitForTimeout(500);
      await narrow(tag);
    }
    // меню вида ⚙ (#255) на 390 px — в пределах окна, страница не прокручивается вбок
    const vb = await pg.$('.k-top [data-a="view"]');
    if (vb) {
      await vb.click(); await pg.waitForTimeout(200);
      const m = await pg.evaluate(() => { const p = document.querySelector('.k-viewmenu'); return p && [Math.round(p.getBoundingClientRect().left), Math.round(p.getBoundingClientRect().right)]; });
      if (!m) errs.push('390px: меню ⚙ не открылось');
      else if (m[0] < 0 || m[1] > 390) errs.push('390px: меню ⚙ за краем окна ' + m.join('…'));
      await narrow('меню ⚙'); await pg.keyboard.press('Escape');
    }
    console.log((errs.length ? 'FAIL ' : 'ok   ') + name + (run ? ' run ' + run : '') + (norun ? ' (нет прогона: пустое состояние)' : '') + (dr ? ' drill' : ' -') + (row ? ' card' : ' -') + ' 390' + (nTabs ? '×' + nTabs : '') + (errs.length ? '\n   ' + errs.slice(0, 5).join('\n   ') : ''));
    if (errs.length) bad++;
    await pg.close();
  }
  await b.close();
  process.exit(bad ? 1 : 0);
})();
