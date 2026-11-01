// Settings uses only synthetic optional-capability responses. No private implementation or live service.
'use strict';
const test = require('node:test'), assert = require('assert');
const fs = require('fs'), path = require('path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (_) { try { return require('playwright'); } catch (_) { return null; } } })();
const ROOT = process.env.ACQUISITION_STATIC_ROOT || path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://127.0.0.1:8100', USER = 'SYNTH_BROWSER_USER', PASSWORD = 'SYNTH_BROWSER_PASSWORD';
const BOOK = 'Synthetic <img src=x onerror="window.acqInjected=true"> & ' + 'X'.repeat(90);
const REPORT = {id:'a'.repeat(64), sha256:'b'.repeat(64), name:'Synthetic_20261015.csv', books:[BOOK],
  policy:{status:'VERIFIED', excluded_products:['Cash', 'SimpleTransfer'], books:{[BOOK]:{eligible:2, products:['FX', 'Swap']}}}};
const TYPES = {'.html':'text/html', '.js':'application/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.woff2':'font/woff2'};

async function setup(browser, {mode = 'success', width = 1440, multiple = false} = {}) {
  const ctx = await browser.newContext({viewport:{width, height:1000}}), pg = await ctx.newPage(), errors = [], requests = [];
  let job = {state:'idle', phase:'idle', code:'idle'}, statusGap = mode === 'status_gap';
  pg.on('pageerror', e => errors.push(e.message));
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    const json = (data, status = 200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(data)});
    if (u.pathname === '/api/v1/auth/state') return json({enabled:false, user:null});
    if (u.pathname === '/api/v1/settings') return json({home:'/synthetic', effective:{}, inbox_dirs:[], env_overrides:[]});
    if (u.pathname === '/api/v1/adapter') return json({loaded:false, name:'synthetic', error:'not loaded', config_files:[], settings_tabs:[]});
    if (u.pathname === '/api/v1/acquisition') {
      if (statusGap && job.state === 'running') { statusGap = false; return json({}, 503); }
      return json({available:true, code:'available', job});
    }
    if (u.pathname === '/api/v1/acquisition/start') {
      assert.strictEqual(req.headers()['content-type'], 'application/x-www-form-urlencoded');
      assert.strictEqual(u.search, '');
      const p = new URLSearchParams(req.postData()); requests.push(p);
      if (p.get('op') === 'inspect') job = {state:'complete', phase:'complete', code:'scope_ready', adapter_fingerprint:'d'.repeat(64), reports:multiple ? [REPORT, {...REPORT,id:'c'.repeat(64),name:'Synthetic_other_20261015.csv',policy:{...REPORT.policy,skipped:{trades:3,books:2}}}] : [REPORT]};
      else if (mode === 'auth') job = {state:'error', phase:'acquiring', code:'auth_failed'};
      else if (mode === 'running' || mode === 'status_gap') job = {state:'running', phase:'acquiring', code:'acquiring'};
      else job = {state:'partial', phase:'complete', code:'completed', run_id:17,
        valuation:{counts:{both:1,neither:1},reasons:{trade_terms_missing:1}}};
      return json(job, 202);
    }
    if (u.pathname === '/api/v1/acquisition/cancel') { job = {state:'cancelled',phase:'acquiring',code:'cancelled'}; return json(job); }
    if (u.pathname.startsWith('/api/')) return json({}, 404);
    const file = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({status:404,body:''});
    return route.fulfill({status:200,contentType:TYPES[path.extname(file)] || 'application/octet-stream', body:fs.readFileSync(file)});
  });
  await pg.goto(ORIGIN + '/settings.html#adapter');
  await pg.locator('#acquisition-card').waitFor({timeout:5000});
  await pg.waitForFunction(() => !document.getElementById('acq-inspect').disabled);
  return {ctx, pg, errors, requests};
}

async function scope(pg) {
  assert.strictEqual(await pg.locator('#acq-date').inputValue(), '', 'no hidden date choice');
  await pg.locator('#acq-date').fill('2026-10-15');
  await pg.locator('#acq-inspect').click();
  await pg.locator('#acq-books input').waitFor();
  assert.strictEqual(await pg.locator('#acq-books input:checked').count(), 0, 'no hidden all-books choice');
  assert.ok(await pg.locator('#acq-fetch').isDisabled());
}

test('explicit local acquisition in Settings', {skip: pw ? false : 'нет Playwright'}, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('actual clicks preview filters and selected scope, transient credentials, partial fresh run', async () => {
      const {ctx,pg,errors,requests} = await setup(browser, {width:390});
      try {
        await scope(pg);
        const preview = await pg.locator('#acq-books').innerText();
        for (const value of ['VERIFIED','Cash','SimpleTransfer','FX','Swap',BOOK]) assert.ok(preview.includes(value));
        assert.strictEqual(await pg.locator('#acq-books img, #acq-books script').count(), 0);
        assert.strictEqual(await pg.evaluate(() => window.acqInjected), undefined);
        const overflow = await pg.locator('#acq-books').evaluate(n => n.scrollWidth > n.clientWidth + 1);
        assert.strictEqual(overflow, false);
        await pg.locator('#acq-books input').check();
        await pg.locator('#acq-user').fill(USER); await pg.locator('#acq-password').fill(PASSWORD);
        assert.strictEqual(await pg.locator('#savebar').isVisible(), false, 'credentials are not dirty settings');
        await pg.locator('#acq-fetch').click();
        await pg.locator('#acq-result a').waitFor();
        assert.strictEqual(await pg.locator('#acq-user').inputValue(), '');
        assert.strictEqual(await pg.locator('#acq-password').inputValue(), '');
        const p = requests.find(p => p.get('op') === 'fetch');
        assert.deepStrictEqual(p.getAll('book'), [BOOK]); assert.strictEqual(p.get('date'), '2026-10-15');
        assert.strictEqual(p.get('adapter_fingerprint'), 'd'.repeat(64));
        assert.strictEqual(p.get('username'), USER); assert.strictEqual(p.get('password'), PASSWORD);
        assert.ok((await pg.locator('#acq-result').innerText()).includes('частичный'));
        assert.ok((await pg.locator('#acq-result a').getAttribute('href')).endsWith('?run_id=17'));
        const persisted = await pg.evaluate(() => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}) + document.body.innerText);
        assert.ok(!persisted.includes(USER) && !persisted.includes(PASSWORD));
        if (process.env.ACQUISITION_SHOTS) {
          fs.mkdirSync(process.env.ACQUISITION_SHOTS,{recursive:true});
          await pg.locator('#acquisition-card').screenshot({path:path.join(process.env.ACQUISITION_SHOTS,'partial-mobile.png')});
        }
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('ambiguous reports require explicit selection', async () => {
      const {ctx,pg,errors} = await setup(browser,{multiple:true});
      try {
        await pg.locator('#acq-date').fill('2026-10-15'); await pg.locator('#acq-inspect').click();
        await pg.locator('#acq-report').waitFor();
        assert.strictEqual(await pg.locator('#acq-report').inputValue(), '');
        assert.strictEqual(await pg.locator('#acq-books input').count(), 0);
        assert.ok(await pg.locator('#acq-fetch').isDisabled());
        await pg.locator('#acq-report').selectOption('c'.repeat(64));
        await pg.locator('#acq-books input').waitFor();
        assert.match(await pg.locator('#acq-books').innerText(), /Пропущено сделок: 3 \(книг: 2\)/);
        await pg.locator('#acq-date').fill('2026-10-16');
        assert.strictEqual(await pg.locator('#acq-books input').count(), 0);
        assert.ok(await pg.locator('#acq-fetch').isDisabled());
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('auth error is actionable and fields clear, cancel remains explicit', async () => {
      for (const mode of ['auth','running']) {
        const {ctx,pg,errors} = await setup(browser,{mode});
        try {
          await pg.locator('#acq-user').fill(USER); await pg.locator('#acq-password').fill(PASSWORD);
          await pg.locator('#acq-check').click();
          await pg.waitForFunction(() => !document.getElementById('acq-password').value);
          if (mode === 'auth') await pg.getByText('Вход во внутреннюю систему не выполнен.',{exact:true}).waitFor();
          else {
            await pg.locator('#acq-cancel').click();
            await pg.getByText('Операция отменена до загрузки данных.',{exact:true}).waitFor();
          }
          const text = await pg.locator('#acquisition-card').innerText();
          assert.ok(!text.includes(USER) && !text.includes(PASSWORD)); assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });
    await t.test('lost status connection preserves a manual status check without another acquisition', async () => {
      const {ctx,pg,requests,errors} = await setup(browser,{mode:'status_gap'});
      try {
        await pg.locator('#acq-user').fill(USER); await pg.locator('#acq-password').fill(PASSWORD);
        await pg.locator('#acq-check').click();
        await pg.getByText('Нет связи с программой. Запуск мог продолжиться; обновите статус перед повторным запуском.',{exact:true}).waitFor();
        assert.ok(await pg.locator('#acq-refresh').isEnabled());
        await pg.locator('#acq-refresh').click();
        await pg.getByText('Получение данных…',{exact:true}).waitFor();
        assert.strictEqual(requests.length, 1);
        await pg.locator('#acq-cancel').click();
        await pg.getByText('Операция отменена до загрузки данных.',{exact:true}).waitFor();
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
  } finally { await browser.close(); }
});
