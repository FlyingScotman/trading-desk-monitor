// TDM#629, ревью: асинхронная загрузка в настоящем браузере (Chromium, страницы как файлы, API — заглушки; данные синтетические).
//  - карточка свопа / РЕПО (не облигации) сама просит историю за год и дорисовывает график, а не остаётся с одной точкой;
//  - C13: PnL эмитента за год не зависит от того, каким запросом пришёл ряд бумаги (оси дат у запросов разные: ряды сводятся
//    по дате, а не по номеру точки);
//  - C08: сбой запроса календаря — своя строка с «Повторить», «статика не разобрана» — только для 404; «загрузка» не висит.
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
const reply = (route, body, status) => { json(route, body, status); return true; };

// страница на файлах prototypes/, API: api(url, route) → true, если ответ дан (иначе 404)
async function open(browser, page, { D = JSON.parse(RUN), api, store = {} }) {
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const pg = await ctx.newPage(), errors = [], calls = [];
  pg.on('pageerror', e => errors.push(e.message));
  await pg.addInitScript(s => { try { Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)); } catch (e) { } }, store);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname === '/concepts-run.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.CONCEPTS_RUN = ' + JSON.stringify(D) + ';' });
    if (u.pathname === '/concepts-data.js') return route.fulfill({ status: 200, contentType: 'application/javascript', body: 'window.DESK_EXTRAS = { notes: [] };' });
    if (u.pathname === '/api/v1/auth/state') return json(route, { enabled: false, user: null });
    if (u.pathname.startsWith('/api/')) {
      calls.push(u.pathname + u.search);
      if (api && api(u, route) === true) return undefined;
      return json(route, {}, 404);
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/concepts/' + page);
  await pg.waitForFunction(() => document.documentElement.dataset.ready === '1' || (window.MOCK && !window.CONCEPTS_RUN_EMPTY && document.querySelector('.k-page')));
  return { pg, ctx, errors, calls };
}
const idsOf = u => decodeURIComponent((u.search.match(/ids=([^&]*)/) || [])[1] || '').split(',').filter(Boolean);
const weekdays = (end, n) => { const out = []; const d = new Date(end + 'T00:00:00Z'); while (out.length < n) { if (d.getUTCDay() % 6) out.unshift(d.toISOString().slice(0, 10)); d.setUTCDate(d.getUTCDate() - 1); } return out; };

test('async loading in the browser (TDM#629 review)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('a swap card asks for its history itself and redraws with it', async () => {
      const D = JSON.parse(RUN), ID = 'SYNTH_SWAP';
      D.positions.push({ ...D.positions[0], id: ID, name: 'Synthetic swap', isin: null, ccy: 'RUB', notValued: false, mv: 1e6, mvRub: 1e6, risk: {}, fxDelta: {},
        instrType: null, segment: 'Деривативы', assetClass: 'OTC-деривативы', extra: { quality: 'exact', reported: { x: { x_product_group: 'irs' } } } });
      const dates = weekdays(D.asOf, 30);
      const { pg, ctx, errors, calls } = await open(browser, 'c01-cockpit.html', { D, api: (u, route) => {
        if (u.pathname !== '/api/v1/history/positions') return false;
        const ids = idsOf(u), series = {};
        ids.forEach(id => { series[id] = { price: dates.map(() => null), notional: dates.map(() => 1e9), dv01: dates.map((_, i) => -1000 * (i + 1)), cs01: null, basis: null, mv: dates.map((_, i) => 1e6 + i), ytm: null, spr: null, bench: null, curves: {}, qty: dates.map(() => null), pt: {} }; });
        return reply(route, { runId: 1, dates, runAt: dates.map(() => null), series });
      } });
      try {
        assert.ok(!calls.some(c => /history\/positions/.test(c)), 'C01 истории позиций не просит');
        await pg.evaluate(id => KIT.card(id), ID);
        await pg.waitForFunction(n => { const el = document.querySelector('.k-drawer:last-of-type .hchart'); const c = el && echarts.getInstanceByDom(el); return c && c.getOption().xAxis[0].data.length === n; }, dates.length);
        const hist = calls.filter(c => /history\/positions/.test(c));
        assert.strictEqual(hist.length, 1, hist.join('\n'));
        assert.ok(hist[0].includes('ids=' + ID) && !/from=/.test(hist[0]), 'одна бумага, год по умолчанию: ' + hist[0]);
        assert.strictEqual(await pg.locator('.k-deep-wait').count(), 0, 'строка «загружается» убрана');
        await pg.evaluate(id => KIT.card(id), ID);  // открыть ещё раз: история уже есть — без второго запроса
        await pg.waitForTimeout(300);
        assert.strictEqual(calls.filter(c => /history\/positions/.test(c)).length, 1);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C13: the issuer PnL for a year is the same however the rows arrived (150-bond chunks have different date axes)', async () => {
      const D = JSON.parse(RUN);
      const base = D.positions.find(p => p.instrType === 'BOND' && p.issuer && p.issuer !== 'Минфин РФ' && p.couponType === 'fix' && p.ccy === 'RUB');
      assert.ok(base, 'в фикстуре есть рублёвый корпоративный фикс');
      const N = 160, ids = [];
      D.positions = D.positions.filter(p => p.issuer !== base.issuer);
      for (let k = 0; k < N; k++) { const id = 'SYN' + k; ids.push(id); D.positions.push({ ...base, id, isin: 'SU' + String(k).padStart(10, '0'), name: 'Synthetic ' + k, notional: base.notional * (1 + k / 100), mv: base.mv * (1 + k / 100), mvRub: base.mvRub * (1 + k / 100) }); }
      const G = weekdays(D.asOf, 24), runDays = [G[0], G[G.length - 1]];
      // котировки: первые 150 бумаг — в чётные дни, остальные — в дни кратные трём: оси двух запросов разные
      const quoted = id => { const k = +id.slice(3); return d => runDays.includes(d) || (k < 150 ? G.indexOf(d) % 2 === 0 : G.indexOf(d) % 3 === 0); };
      const px = (id, d) => 90 + (+id.slice(3)) * 0.01 + G.indexOf(d) * 0.37 + (G.indexOf(d) % 5) * 0.11;
      const series = (id, dates) => ({ price: dates.map(d => quoted(id)(d) ? px(id, d) : null), notional: dates.map(() => base.notional * (1 + (+id.slice(3)) / 100)), dv01: dates.map(() => -1000), cs01: null, basis: null, mv: dates.map(() => 1e6), ytm: null, spr: null, bench: null, curves: {}, qty: dates.map(() => null), pt: {} });
      const axisOf = list => G.filter(d => runDays.includes(d) || list.some(id => quoted(id)(d)));
      const { pg, ctx, errors, calls } = await open(browser, 'c13-issuers.html', { D, store: { c13: JSON.stringify({ sel: base.issuer, meas: 'mv' }) }, api: (u, route) => {
        if (u.pathname !== '/api/v1/history/positions') return false;
        const want = idsOf(u), dates = axisOf(want), out = {};
        want.forEach(id => { out[id] = series(id, dates); });
        return reply(route, { runId: 1, dates, runAt: dates.map(() => null), series: out });
      } });
      try {
        await pg.waitForFunction(() => { const els = Array.from(document.querySelectorAll('.ch')); return els.some(e => { const c = echarts.getInstanceByDom(e); const o = c && c.getOption(); return o && o.series && o.series.length === 2 && o.series[0].name === 'за день'; }); });
        const hist = calls.filter(c => /history\/positions/.test(c));
        assert.ok(hist.length >= 2, 'бумаг больше 150 — запрос частями: ' + hist.length);
        const got = await pg.evaluate(() => {
          const el = Array.from(document.querySelectorAll('.ch')).find(e => { const c = echarts.getInstanceByDom(e); const o = c && c.getOption(); return o && o.series && o.series[0] && o.series[0].name === 'за день'; });
          const o = echarts.getInstanceByDom(el).getOption();
          return { dates: o.xAxis[0].data, daily: o.series[0].data.map(x => x.value), cum: o.series[1].data };
        });
        // эталон: один запрос на все бумаги — общая ось, та же формула
        const truth = await pg.evaluate(({ G, runDays, ids, rows }) => {
          const M = MOCK, quoted = (k, d) => runDays.includes(d) || (k < 150 ? G.indexOf(d) % 2 === 0 : G.indexOf(d) % 3 === 0);
          const dates = G.filter(d => runDays.includes(d) || ids.some((id, k) => quoted(k, d)));
          const px = (k, d) => 90 + k * 0.01 + G.indexOf(d) * 0.37 + (G.indexOf(d) % 5) * 0.11;
          const L = M.positions.filter(p => ids.includes(p.id));
          const daily = dates.map((_, i) => i === 0 ? 0 : L.reduce((s, p) => {
            const k = +p.id.slice(3), n = rows.base * (1 + k / 100), pn = quoted(k, dates[i]) ? px(k, dates[i]) : null, pp = quoted(k, dates[i - 1]) ? px(k, dates[i - 1]) : null;
            const dp = pn != null && pp != null ? pn - pp : 0;
            return s + n * dp / 100 * M.toRepr[p.ccy] * (p.indexation || 1) + (p.notional ? (p.carryDay + (p.fundingDay || 0)) * n / p.notional : 0);
          }, 0));
          return { dates, daily };
        }, { G, runDays, ids, rows: { base: base.notional } });
        assert.deepStrictEqual(got.dates, truth.dates, 'ось — объединение дат всех бумаг');
        got.daily.forEach((v, i) => assert.ok(Math.abs(v - truth.daily[i]) <= 1e-6 * Math.max(1, Math.abs(truth.daily[i])), 'день ' + got.dates[i] + ': ' + v + ' ≠ ' + truth.daily[i]));
        assert.ok(got.daily.some(v => v !== 0), 'PnL не нулевой');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('C08: a failed calendar request is not "the static is not parsed yet"; 404 is; loading never hangs', async () => {
      for (const [status, expect] of [[500, 'не загрузилось'], [404, 'статика хаба ещё не разобрана']]) {
        const { pg, ctx, errors } = await open(browser, 'c08-calendar.html', { store: { c08: JSON.stringify({ mode: 'rub' }) }, api: (u, route) => (u.pathname === '/api/v1/calendar/market' ? reply(route, {}, status) : false) });
        try {
          await pg.waitForFunction(() => !/Загрузка календаря/.test(document.getElementById('view').innerText), null, { timeout: 5000 });
          const text = await pg.locator('#view').innerText();
          if (status === 500) {
            assert.ok(text.includes('календарь рынка не загрузился') && text.includes('Повторить') && !text.includes('статика хаба ещё не разобрана'), text);
            assert.ok(await pg.locator('.k-load-err').count() >= 1, 'и общая полоса сбоя сверху');
          } else assert.ok(text.includes(expect) && !text.includes('не загрузился'), text);
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });
  } finally { await browser.close(); }
});
