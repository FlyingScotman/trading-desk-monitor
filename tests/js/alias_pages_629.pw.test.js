// TDM#629, повторное ревью: алиасы бумаг приходят позже страницы (ответ GET /api/v1/instruments/aliases задержан до команды
// теста), и имя меняется на месте на страницах, где раньше оставалось биржевым: C12 (потоки портфеля), C17 (таблица,
// лидеры, панель позиции) и подписи точек карты эмитента C13. Страницы как файлы, API — заглушки, данные синтетические.
//   node --test tests/js/alias_pages_629.pw.test.js
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');
const { TEXT: RUN } = require('./fixtures/concepts_run');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' };
const json = (route, body, status) => route.fulfill({ status: status || 200, contentType: 'application/json', body: JSON.stringify(body) });
const ALIAS = 'АЛИАС-ТЕСТ-629';

// страница на файлах prototypes/; ответ алиасов ждёт release(isin); extras — window.DESK_EXTRAS страницы; api — остальные ответы
async function open(browser, page, { D = JSON.parse(RUN), extras = { notes: [] }, api, store = {}, search = '', ready = () => document.documentElement.dataset.ready === '1' || (window.MOCK && !window.CONCEPTS_RUN_EMPTY && document.querySelector('.k-page')) }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [];
  let gate, isin = null;
  const opened = new Promise(r => { gate = r; });
  pg.on('pageerror', e => errors.push(e.message));
  await pg.addInitScript(s => { try { Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, store);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = ' + JSON.stringify(extras) + ';' });
    if (u.pathname === '/api/v1/auth/state') return json(route, { enabled: false, user: null });
    if (u.pathname === '/api/v1/instruments/aliases') {
      // ответ приходит, только когда тест его отпустил
      return opened.then(() => json(route, { aliases: [{ isin, alias: ALIAS, changed_by: 'local', changed_at: '2026-11-10T09:00:00+00:00' }] }));
    }
    if (u.pathname.startsWith('/api/')) {
      if (api && api(u, route) === true) return undefined;
      return json(route, {}, 404);
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + page + search);
  await pg.waitForFunction(ready);
  return { pg, ctx, errors, release: i => { isin = i; gate(); } };
}
const weekdays = (end, n) => { const out = []; const d = new Date(end + 'T00:00:00Z'); while (out.length < n) { if (d.getUTCDay() % 6) out.unshift(d.toISOString().slice(0, 10)); d.setUTCDate(d.getUTCDate() + 1); } return out; };

test('aliases that arrive after the page (TDM#629 re-review)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('C12: the cash flows pane renames the security when the alias arrives late', async () => {
      const D = JSON.parse(RUN), P = D.positions.find(p => p.isin && p.instrType === 'BOND');
      const day = weekdays(D.asOf, 6)[3];
      const { pg, ctx, errors, release } = await open(browser, 'c12-terminal.html', { D, ready: () => window.MOCK && document.getElementById('quick') && document.getElementById('quick').children.length, extras: { notes: [], cashflows: [{ date: day, pos: P.id, kind: 'cpn', ccy: 'RUB', amount: 1e6, est: false }] } });
      try {
        await pg.locator('#quick [data-run="cal 30d"]').click();
        const cell = pg.locator('tr[data-card="' + P.id + '"] td:nth-child(2)');
        await cell.first().waitFor();
        assert.ok((await cell.first().innerText()).includes(P.name), 'сначала — биржевое имя: ' + await cell.first().innerText());
        assert.ok(!(await cell.first().innerText()).includes(ALIAS));
        release(P.isin);
        await pg.waitForFunction(a => Array.from(document.querySelectorAll('tr[data-card] td:nth-child(2)')).some(td => td.innerText.includes(a)), ALIAS);
        const txt = await cell.first().innerText();
        assert.ok(txt.includes(ALIAS) && !txt.includes(P.name), txt);
        assert.ok(await pg.locator('tr[data-card="' + P.id + '"] td:nth-child(2) b').count() >= 1, 'цветная точка эмитента осталась');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C17: the table, the leaders and the position panel take the alias', async () => {
      const D = JSON.parse(RUN);
      D.positions.forEach(p => { p.id = String(p.id); });  // у сервера номера позиций — строки, ответ кэрри называет их так же
      const P = D.positions.find(p => p.isin && p.instrType === 'BOND');
      const base = JSON.parse(/const REAL = (\{.*\});/.exec(fs.readFileSync(path.join(__dirname, 'forward_carry.test.js'), 'utf8'))[1]);
      const x = base.positions.find(r => r.carry != null && r.net > 0);
      x.positionId = P.id; x.secid = P.isin;
      base.positions = [x];
      let asked = 0;
      const { pg, ctx, errors, release } = await open(browser, 'c17-forward-carry.html', { D, search: '?pos=' + encodeURIComponent(P.id), api: (u, route) => {
        if (u.pathname !== '/api/v1/concepts/carry') return false;
        asked++; json(route, base); return true;
      } });
      try {
        const names = () => pg.evaluate(() => ({
          table: Array.from(document.querySelectorAll('#body table.k-tbl tbody td')).map(td => td.innerText),
          lead: Array.from(document.querySelectorAll('#body .c17-lead td.nm')).map(td => td.innerText),
          panel: (document.querySelector('.c17-focus h3') || {}).innerText || '' }));
        await pg.waitForFunction(() => document.querySelector('.c17-focus h3'));
        const before = await names();
        assert.ok(before.panel.includes(P.name), before.panel);
        assert.ok(before.table.some(s => s.includes(P.name)), JSON.stringify(before.table));
        assert.ok(before.lead.some(s => s.includes(P.name)), JSON.stringify(before.lead));
        const askedBefore = asked;
        release(P.isin);
        await pg.waitForFunction(a => (document.querySelector('.c17-focus h3') || {}).innerText.includes(a), ALIAS);
        const after = await names();
        assert.ok(after.panel.includes(ALIAS) && !after.panel.includes(P.name), after.panel);
        assert.ok(after.table.some(s => s.includes(ALIAS)) && !after.table.some(s => s.includes(P.name)), JSON.stringify(after.table));
        assert.ok(after.lead.some(s => s.includes(ALIAS)) && !after.lead.some(s => s.includes(P.name)), JSON.stringify(after.lead));
        assert.strictEqual(asked, askedBefore, 'алиас — перерисовка по уже полученному ответу, нового запроса кэрри нет');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C13: the point labels of the issuer map take the alias, and only that chart is redrawn', async () => {
      const D = JSON.parse(RUN);
      const P = D.positions.find(p => p.instrType === 'BOND' && p.issuer && p.issuer !== 'Минфин РФ' && p.isin && p.couponType === 'fix' && p.ccy === 'RUB');
      assert.ok(P, 'в фикстуре есть рублёвый корпоративный фикс');
      const { pg, ctx, errors, release } = await open(browser, 'c13-issuers.html', { D, store: { c13: JSON.stringify({ sel: P.issuer, meas: 'mv', curve: 'ytm' }) } });
      try {
        const labels = () => pg.evaluate(() => {
          for (const e of document.querySelectorAll('.ch')) {
            const c = echarts.getInstanceByDom(e), o = c && c.getOption();
            const s = o && o.series && o.series.find(x => /наши$/.test(x.name || ''));
            if (s) return s.data.map(d => d.name);
          }
          return null;
        });
        await pg.waitForFunction(() => Array.from(document.querySelectorAll('.ch')).some(e => { const c = echarts.getInstanceByDom(e), o = c && c.getOption(); return o && o.series && o.series.some(x => /наши$/.test(x.name || '')); }));
        assert.ok((await labels()).some(n => n.includes(P.name)), 'сначала — биржевое имя');
        // остальные графики дашборда не перерисовываются: помечаем их и смотрим, остались ли метки
        await pg.evaluate(() => document.querySelectorAll('.ch').forEach(e => { e.dataset.mark = '1'; }));
        const before = await pg.evaluate(() => document.querySelectorAll('#dash [data-mark]').length);
        release(P.isin);
        await pg.waitForFunction(a => Array.from(document.querySelectorAll('.ch')).some(e => { const c = echarts.getInstanceByDom(e), o = c && c.getOption(); const s = o && o.series && o.series.find(x => /наши$/.test(x.name || '')); return s && s.data.some(d => d.name === a); }), ALIAS);
        const L = await labels();
        assert.ok(L.includes(ALIAS) && !L.some(n => n.includes(P.name)), JSON.stringify(L));
        const after = await pg.evaluate(() => document.querySelectorAll('#dash [data-mark]').length);
        assert.strictEqual(after, before, 'дашборд целиком не перерисован: помеченных графиков было ' + before + ', стало ' + after);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('a rejected derivatives request does not leave the rates panel waiting forever', async () => {
      const D = JSON.parse(RUN);
      D.source = Object.assign({}, D.source, { runId: 7 });  // у прогона есть номер: отчёт по деривативам запрашивается
      // ответ по деривативам не приходит: панель остаётся в состоянии «загрузка»
      const { pg, ctx, errors } = await open(browser, 'c02-risk-ladder.html', { D, api: u => /\/derivatives$/.test(u.pathname) });
      try {
        const r = await pg.evaluate(async () => {
          let calls = 0;
          const state = MOCK.derivParallel();
          if (!state || state.state !== 'loading') return { skip: JSON.stringify(state) };
          MOCK.loadDeriv = () => { calls++; return Promise.reject(new Error('boom')); };
          const el = document.createElement('div'); document.body.appendChild(el);
          KIT.ratesCorePanel(el, KIT.list());
          const first = el._rcWait;
          await new Promise(r => setTimeout(r, 50));
          const afterFirst = { wait: el._rcWait, calls };
          KIT.ratesCorePanel(el, KIT.list());  // следующий вызов спрашивает снова, а не считает, что ответ уже идёт
          await new Promise(r => setTimeout(r, 50));
          return { first, afterFirst, calls, wait: el._rcWait };
        });
        assert.ok(!r.skip, 'параллельный отчёт в этой фикстуре не в состоянии загрузки: ' + r.skip);
        assert.strictEqual(r.first, true);
        assert.deepStrictEqual(r.afterFirst, { wait: false, calls: 1 });
        assert.strictEqual(r.calls, 2);
        assert.strictEqual(r.wait, false);
        assert.deepStrictEqual(errors.filter(e => !/boom/.test(e)), []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
