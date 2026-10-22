// Playwright check of the settings page (R40, R41) on a running desk serve: every section opens without console
// errors (dark and light), the adapter's forms render, the books table lists books of recent positions that books.json
// lacks («не настроено»), one is added in one click, checked by the server with a diff, saved, and shows in the history.
//   node tools/settings_smoke.js http://127.0.0.1:8100 [out_dir] [--no-write] [--login USER:PASSWORD]
// --login: sign in first (sign-in on, backend api/auth.py; an admin). The «Пользователи» section lists the users.
// Needs a desk serve whose adapter declares settings_tabs (the trades-data-adapter) and positions of a book its
// books.json does not name (e.g. `desk load-positions backend/examples/demo_book.csv --date …`). --no-write: look only.
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const path = require('path'), fs = require('fs');
const args = process.argv.slice(2), noWrite = args.includes('--no-write');
const li = args.indexOf('--login'), login = li >= 0 ? args.splice(li, 2)[1] : null;
const pos = args.filter(a => !a.startsWith('--'));
const base = (pos[0] || 'http://127.0.0.1:8100').replace(/\/$/, '');
const out = path.resolve(pos[1] || path.join(__dirname, '..', '.shots', 'settings'));
fs.mkdirSync(out, { recursive: true });
const SECTIONS = ['hub', 'adapter', 'entities', 'books', 'funding', 'auto', 'up', 'diag', 'users'];
let bad = 0;
const fail = (m) => { bad++; console.log('FAIL ' + m); };
const ok = (m) => console.log('ok   ' + m);

(async () => {
  const b = await chromium.launch();
  for (const theme of ['dark', 'light']) {
    const ctx = await b.newContext({ viewport: { width: 1440, height: 960 } });
    await ctx.addInitScript(t => { try { localStorage.setItem('concepts-theme', t); } catch (e) { } }, theme);
    if (login) {
      const [username, ...pw] = login.split(':');
      const r = await ctx.request.post(base + '/api/v1/auth/login', { data: { username, password: pw.join(':') } });
      if (!r.ok()) { fail('login ' + username + ': ' + r.status()); break; }
    }
    const pg = await ctx.newPage();
    const errs = [];
    pg.on('pageerror', e => errs.push('pageerror: ' + e.message));
    pg.on('console', m => { if (m.type() === 'error' && !/favicon|Failed to load resource/.test(m.text())) errs.push('console: ' + m.text()); });
    pg.on('dialog', d => d.accept());
    await pg.goto(base + '/settings.html#hub');
    await pg.waitForSelector('[data-tab-slot="books"] .sf-head', { state: 'attached', timeout: 20000 }).catch(() => fail(theme + ': the books form did not render'));
    await pg.waitForTimeout(800);
    for (const s of SECTIONS) {
      await pg.evaluate(id => { location.hash = id; }, s);
      await pg.waitForTimeout(350);
      const visible = await pg.$eval('section.sec.on', el => el.dataset.sec).catch(() => null);
      if (visible !== s) fail(theme + ': #' + s + ' shows ' + visible);
      await pg.screenshot({ path: path.join(out, theme + '-' + s + '.png'), fullPage: true });
    }
    if (theme === 'dark') {
      // «Пользователи» (desk-users.js): the list (or «пока нет»), and with sign-in off how to enable it (START_HERE §9)
      await pg.evaluate(() => { location.hash = 'users'; });
      await pg.waitForSelector('#sec-users [data-desk-users] h3', { timeout: 10000 }).catch(() => fail('users: the section did not render'));
      await pg.waitForTimeout(600);
      const u = await pg.evaluate(() => ({ off: !document.getElementById('users-off').hidden, text: document.querySelector('#sec-users [data-desk-users]').textContent,
        badge: document.getElementById('auth-st').textContent, nav: !document.querySelector('a[data-sec=users]').hidden }));
      if (!u.nav) fail('users: no link in the nav');
      if (login ? u.off || !u.text.includes(login.split(':')[0]) : !u.off) fail('users: ' + JSON.stringify(u).slice(0, 300));
      else ok('users: ' + u.badge + (login ? ', lists ' + login.split(':')[0] : ', how to enable shown'));
      await pg.screenshot({ path: path.join(out, 'users.png'), fullPage: true });
      await pg.evaluate(() => { location.hash = 'books'; });
      await pg.waitForTimeout(300);
      const unconf = await pg.$$eval('#sec-books tr.unconf', rows => rows.map(r => r.textContent));
      if (!unconf.length) fail('books: no «не настроено» rows (positions of a book books.json lacks?)');
      else ok('books: не настроено — ' + unconf.length);
      const nav = await pg.$eval('[data-count="books"]', el => el.textContent);
      ok('nav books badge: ' + nav);
      if (unconf.length && !noWrite) {
        await pg.click('#sec-books tr.unconf button');
        await pg.waitForSelector('.sf-drawer');
        await pg.screenshot({ path: path.join(out, 'books-add-drawer.png') });
        // fill the required fields the report could not give: desk and entity (the first entity of the list)
        const fields = await pg.$$('.sf-drawer .sf-grid > label');
        for (const l of fields) {
          const t = await l.textContent(), id = await l.getAttribute('for');
          if (!id) continue;
          if (/^Деск/.test(t)) await pg.fill('#' + id, 'SYN_DESK');
          if (/^Юрлицо/.test(t)) { const opts = await pg.$$eval('#' + id + ' option', o => o.map(x => x.value)); if (opts[1]) await pg.selectOption('#' + id, opts[1]); }
        }
        await pg.click('.sf-drawer .ft .btn');
        await pg.waitForTimeout(300);
        await pg.click('[data-tab-slot="books"] .sf-head .btn');
        await pg.waitForSelector('.sf-drawer .verdict', { timeout: 40000 });
        const verdict = await pg.$eval('.sf-drawer .verdict', el => el.className + ' ' + el.textContent);
        const diff = await pg.$$eval('.sf-drawer .diff .d-add', d => d.length);
        await pg.screenshot({ path: path.join(out, 'books-review.png') });
        if (!/ok|warn/.test(verdict)) fail('review verdict: ' + verdict); else ok('review: ' + verdict.replace(/^\S+ \S+ /, ''));
        if (!diff) fail('review: no added lines in the diff'); else ok('review diff: +' + diff);
        await pg.click('.sf-drawer .ft .btn');
        await pg.waitForSelector('.toast:not([hidden])', { timeout: 20000 });
        const t = await pg.$eval('.toast', el => el.textContent);
        if (!/Сохранено/.test(t)) fail('save toast: ' + t); else ok('saved: ' + t);
        await pg.waitForTimeout(600);
        await pg.click('[data-tab-slot="books"] .sf-head .ibtn:nth-of-type(1)');  // История
        await pg.waitForSelector('.sf-drawer tbody tr', { timeout: 10000 }).catch(() => fail('history: no revisions'));
        const revs = await pg.$$eval('.sf-drawer tbody tr', r => r.map(x => x.textContent));
        ok('history: ' + revs.length + ' revision(s): ' + (revs[0] || ''));
        await pg.screenshot({ path: path.join(out, 'books-history.png') });
        await pg.keyboard.press('Escape');
      }
      // inline validation: a spread that is not a number shows under the field, in Russian
      await pg.evaluate(() => { location.hash = 'funding'; });
      await pg.waitForTimeout(300);
      await pg.click('[data-tab-slot="funding"] tr.row');
      await pg.waitForSelector('.sf-drawer');
      const vf = await pg.$$('.sf-drawer .sf-grid > label');
      for (const l of vf) if (/^Спред/.test(await l.textContent())) { const id = await l.getAttribute('for'); await pg.fill('#' + id, '1,5 б.п.'); }
      await pg.waitForTimeout(1000);
      const errsShown = await pg.$$eval('.sf-drawer .err', e => e.map(x => x.textContent));
      if (!errsShown.length) fail('funding: no inline error for a bad spread'); else ok('inline: ' + errsShown[0]);
      await pg.screenshot({ path: path.join(out, 'funding-inline.png') });
      await pg.click('.sf-drawer .ft .ibtn');  // Отмена
      // expert toggle: raw JSON
      await pg.click('[data-tab-slot="funding"] .sf-head input[type=checkbox]');
      const raw = await pg.$eval('[data-tab-slot="funding"] textarea', t => t.value.length).catch(() => 0);
      if (!raw) fail('expert: no raw JSON'); else ok('expert: raw JSON ' + raw + ' chars');
    }
    if (errs.length) fail(theme + ': ' + errs.slice(0, 5).join(' | ')); else ok(theme + ': no console errors');
    await ctx.close();
  }
  // phone width: navigation on top, no horizontal scroll
  const mc = await b.newContext({ viewport: { width: 390, height: 844 } });
  if (login) { const [username, ...pw] = login.split(':'); await mc.request.post(base + '/api/v1/auth/login', { data: { username, password: pw.join(':') } }); }
  const m = await mc.newPage();
  await m.goto(base + '/settings.html#books');
  await m.waitForTimeout(1500);
  const wide = await m.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (wide > 2) fail('phone: page scrolls sideways by ' + wide + 'px'); else ok('phone: no sideways scroll');
  await m.screenshot({ path: path.join(out, 'phone-books.png'), fullPage: true });
  await b.close();
  console.log((bad ? 'FAILED ' + bad : 'all ok') + ' · screenshots: ' + out);
  process.exit(bad ? 1 : 0);
})();
