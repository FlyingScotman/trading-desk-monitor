// Щёлкнуть каждую вкладку (.k-tabs button) и каждый переключатель (.k-seg button) каждой страницы, собрать ошибки консоли.
// node prototypes/concepts/tools/clickthrough.js [--base http://127.0.0.1:8199/concepts/] [page.html ...]
// --login USER:PASSWORD: sign in first (desk serve with sign-in on, backend api/auth.py); the pages share the session
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const path = require('path'), fs = require('fs');
const dir = path.resolve(__dirname, '..');
let pages = process.argv.slice(2), base = null;
const bi = pages.indexOf('--base'); if (bi >= 0) { base = pages[bi + 1]; pages.splice(bi, 2); }
let login = null; const li = pages.indexOf('--login'); if (li >= 0) { login = pages[li + 1]; pages.splice(li, 2); }
if (!pages.length) pages = fs.readdirSync(dir).filter(f => /^c\d.*\.html$/.test(f));
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
    await pg.addInitScript(() => { try { localStorage.clear(); } catch (e) { } });
    await pg.goto(base ? base + name : 'file://' + path.join(dir, name));
    await pg.waitForTimeout(700);
    let clicks = 0;
    for (const sel of ['.k-tabs button', '.k-seg button']) {
      const n = await pg.$$eval(sel, els => els.length);
      for (let i = 0; i < n && i < 80; i++) {
        const els = await pg.$$(sel);
        if (!els[i]) break;
        try { if (await els[i].isVisible()) { await els[i].click({ timeout: 1500 }); clicks++; await pg.waitForTimeout(120); } } catch (e) { }
        await pg.keyboard.press('Escape');
      }
    }
    console.log((errs.length ? 'FAIL ' : 'ok   ') + name + ' clicks ' + clicks + (errs.length ? '\n   ' + [...new Set(errs)].slice(0, 6).join('\n   ') : ''));
    if (errs.length) bad++;
    await pg.close();
  }
  await b.close();
  process.exit(bad ? 1 : 0);
})();
