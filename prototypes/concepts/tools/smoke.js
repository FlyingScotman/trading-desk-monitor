// Смоук-проверка концептов: открыть каждую страницу, собрать ошибки консоли, снять скриншот,
// щёлкнуть первую кликабельную цифру (разбор) и первую строку позиции (карточка).
// node prototypes/concepts/tools/smoke.js [out_dir] [--base http://127.0.0.1:8199/concepts/] [page.html ...]
// --base: the pages served by desk serve (real runs, lib/real.js) instead of the files (demo data)
// --login USER:PASSWORD: sign in first (desk serve with sign-in on, backend api/auth.py); the pages share the session
// Playwright: the global install of the dev machine, otherwise a resolvable `playwright` (CI: NODE_PATH)
const { chromium } = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { return require('playwright'); } })();
const path = require('path'), fs = require('fs');
const dir = path.resolve(__dirname, '..');
const out = path.resolve(process.argv[2] || path.join(dir, '.shots'));
fs.mkdirSync(out, { recursive: true });
let pages = process.argv.slice(3), base = null;
const bi = pages.indexOf('--base'); if (bi >= 0) { base = pages[bi + 1]; pages.splice(bi, 2); }
let login = null; const li = pages.indexOf('--login'); if (li >= 0) { login = pages[li + 1]; pages.splice(li, 2); }
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
  let bad = 0;
  for (const name of pages) {
    const pg = await ctx.newPage(); await pg.setViewportSize({ width: 1600, height: 1000 });
    const errs = [];
    pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error') errs.push('console: ' + m.text()); });
    await pg.goto(base ? base + name : 'file://' + path.join(dir, name));
    await pg.waitForTimeout(900);
    const src = await pg.evaluate(() => window.CONCEPTS_SOURCE || null);
    if (base && /^c\d/.test(name) && src !== 'real') errs.push('data source: ' + src + ', not the server run');
    await pg.screenshot({ path: path.join(out, name.replace('.html', '.png')) });
    const dr = await pg.$('[data-dr]');
    if (dr) { await dr.click(); await pg.waitForTimeout(300); await pg.screenshot({ path: path.join(out, name.replace('.html', '-drill.png')) }); await pg.keyboard.press('Escape'); }
    const row = await pg.$(base ? 'tr.row[data-id]' : 'tr.row[data-id^="P"]');  // на прогоне id позиции — «книга|бумага»
    if (row) { await row.click(); await pg.waitForTimeout(500); await pg.screenshot({ path: path.join(out, name.replace('.html', '-card.png')) }); await pg.keyboard.press('Escape'); }
    console.log((errs.length ? 'FAIL ' : 'ok   ') + name + (dr ? ' drill' : ' -') + (row ? ' card' : ' -') + (errs.length ? '\n   ' + errs.slice(0, 5).join('\n   ') : ''));
    if (errs.length) bad++;
    await pg.close();
  }
  await b.close();
  process.exit(bad ? 1 : 0);
})();
