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

// `known`: the status route's `login_known` (absent: an older backend; false: no inspection of this adapter has answered yet)
async function setup(browser, {mode = 'success', width = 1440, multiple = false, known} = {}) {
  const ctx = await browser.newContext({viewport:{width, height:1000}}), pg = await ctx.newPage(), errors = [], requests = [], stops = [];
  let job = {state:'idle', phase:'idle', code:'idle'}, statusGap = mode === 'status_gap', login;  // no key: an older backend
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
      return json({available:true, code:'available', ...(login === undefined ? {} : {login}), ...(known === undefined ? {} : {login_known: known}), job});
    }
    if (u.pathname === '/api/v1/acquisition/start') {
      assert.strictEqual(req.headers()['content-type'], 'application/x-www-form-urlencoded');
      assert.strictEqual(u.search, '');
      const p = new URLSearchParams(req.postData()); requests.push(p);
      if (mode === 'needcreds' && p.get('op') !== 'inspect' && !p.has('username')) return json({state:'blocked', code:'credentials_required'}, 422);
      if (mode === 'stale' && p.get('op') !== 'inspect' && !p.has('username')) { login = 'required'; return json({state:'blocked', code:'credentials_required'}, 422); }
      if (p.get('op') === 'inspect' && (mode === 'nologin' || mode === 'stale')) login = 'none';
      if (p.get('op') === 'inspect' && known === false) { known = true; login = login || 'required'; }  // the inspection answered
      if (p.get('op') === 'inspect') job = {state:'complete', phase:'complete', code:'scope_ready', adapter_fingerprint:'d'.repeat(64), reports:multiple ? [REPORT, {...REPORT,id:'c'.repeat(64),name:'Synthetic_other_20261015.csv',policy:{...REPORT.policy,skipped:{trades:3,books:2}}}] : [REPORT], ...(login ? {login} : {})};
      else if (mode === 'auth') job = {state:'error', phase:'acquiring', code:'auth_failed'};
      else if (mode === 'waiting') job = {state:'running', phase:'waiting', code:'waiting_feed', waiting_for:{kind:'feed', feed:'terms',
        title:'Synthetic load', since:new Date(Date.now() - 65000).toISOString(), phase:'fetching'}};
      else if (mode === 'running' || mode === 'status_gap') job = {state:'running', phase:'acquiring', code:'acquiring'};
      else job = {state:'partial', phase:'complete', code:'completed', run_id:17,
        valuation:{counts:{both:1,neither:1},reasons:{trade_terms_missing:1}}};
      return json(job, 202);
    }
    if (u.pathname === '/api/v1/acquisition/cancel') { job = {state:'cancelled',phase:job.phase === 'waiting' ? 'waiting' : 'acquiring',code:'cancelled'}; return json(job); }
    if (u.pathname === '/api/v1/adapter-services/cancel') {  // «Остановить загрузку»: the load ends, the waiting job starts
      assert.strictEqual(req.headers()['content-type'], 'application/x-www-form-urlencoded');
      stops.push(new URLSearchParams(req.postData()));
      if (mode === 'waiting') job = {state:'running', phase:'acquiring', code:'acquiring'};
      return json({state:'idle', code:'idle'});
    }
    if (u.pathname.startsWith('/api/')) return json({}, 404);
    const file = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({status:404,body:''});
    return route.fulfill({status:200,contentType:TYPES[path.extname(file)] || 'application/octet-stream', body:fs.readFileSync(file)});
  });
  await pg.goto(ORIGIN + '/settings.html#adapter/acq');
  await pg.locator('#acquisition-card').waitFor({timeout:5000});
  await pg.waitForFunction(() => !document.getElementById('acq-inspect').disabled);
  return {ctx, pg, errors, requests, stops};
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
    await t.test('a provider without a login: fields hidden after the inspection, nothing sent', async () => {
      const {ctx,pg,errors,requests} = await setup(browser,{mode:'nologin', width:390});
      try {
        assert.ok(await pg.locator('#acq-user').isVisible(), 'login is asked until the server says otherwise');
        await scope(pg);
        await pg.locator('#acq-user').waitFor({state:'hidden'});
        assert.strictEqual(await pg.locator('#acq-password').isVisible(), false);
        assert.ok(await pg.locator('#acq-nologin').isVisible());
        assert.strictEqual(await pg.locator('#acq-check').innerText(), 'Проверить подключение');
        await pg.locator('#acq-books input').check();
        await pg.locator('#acq-fetch').click();
        await pg.locator('#acq-result a').waitFor();
        const p = requests.find(p => p.get('op') === 'fetch');
        assert.ok(!p.has('username') && !p.has('password'), 'no credentials, not even empty ones');
        assert.deepStrictEqual(p.getAll('book'), [BOOK]);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('the server asks for the login again: the fields come back', async () => {
      const {ctx,pg,errors,requests} = await setup(browser,{mode:'stale'});
      try {
        await scope(pg);
        await pg.locator('#acq-user').waitFor({state:'hidden'});
        await pg.locator('#acq-check').click();
        await pg.getByText('Введите логин и пароль для этого запуска.',{exact:true}).waitFor();
        assert.ok(await pg.locator('#acq-user').isVisible() && await pg.locator('#acq-password').isVisible());
        assert.strictEqual(await pg.locator('#acq-nologin').isVisible(), false);
        assert.strictEqual(await pg.locator('#acq-check').innerText(), 'Проверить вход');
        await pg.locator('#acq-user').fill(USER); await pg.locator('#acq-password').fill(PASSWORD);
        await pg.locator('#acq-check').click();
        await pg.locator('#acq-result a').waitFor();  // the synthetic server answers any accepted run as completed
        const last = requests[requests.length - 1];
        assert.strictEqual(last.get('username'), USER); assert.strictEqual(last.get('password'), PASSWORD);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('the way to log in is not known before the inspection: a hint, no fields (TDM#673)', async () => {
      const {ctx,pg,errors,requests} = await setup(browser,{known:false, width:390});
      try {
        await pg.locator('#acq-loginunknown').waitFor({state:'visible'});
        assert.match(await pg.locator('#acq-loginunknown').innerText(), /Способ входа станет известен после «Найти отчёт и книги»/);
        assert.strictEqual(await pg.locator('#acq-user').isVisible(), false);
        assert.strictEqual(await pg.locator('#acq-password').isVisible(), false);
        assert.strictEqual(await pg.locator('#acq-nologin').isVisible(), false, 'not claimed to be without a login either');
        // a check pressed anyway sends no credentials; the server refuses (it does not know) and the fields come back
        await pg.locator('#acq-check').click();
        assert.ok(!requests[0].has('username') && !requests[0].has('password'));
        // the inspection answers "login required": the fields are shown, the hint is gone
        await scope(pg);
        await pg.locator('#acq-user').waitFor({state:'visible'});
        assert.ok(await pg.locator('#acq-password').isVisible());
        assert.strictEqual(await pg.locator('#acq-loginunknown').isVisible(), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('after credentials_required the fields stay on the next status poll while the way is still not known (TDM#673)', async () => {
      const {ctx,pg,errors} = await setup(browser,{mode:'needcreds', known:false});
      try {
        await pg.locator('#acq-loginunknown').waitFor({state:'visible'});
        await pg.locator('#acq-check').click();
        await pg.getByText('Введите логин и пароль для этого запуска.',{exact:true}).waitFor();
        await pg.locator('#acq-user').waitFor({state:'visible'});
        await pg.locator('#acq-refresh').click();  // the status route still says: not known
        await pg.waitForTimeout(300);
        assert.ok(await pg.locator('#acq-user').isVisible() && await pg.locator('#acq-password').isVisible());
        assert.strictEqual(await pg.locator('#acq-loginunknown').isVisible(), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('a provider without a login: no fields before the inspection and none after it (TDM#673)', async () => {
      const {ctx,pg,errors} = await setup(browser,{mode:'nologin', known:false});
      try {
        await pg.locator('#acq-loginunknown').waitFor({state:'visible'});
        assert.strictEqual(await pg.locator('#acq-user').isVisible(), false);
        await scope(pg);
        await pg.locator('#acq-nologin').waitFor({state:'visible'});
        assert.strictEqual(await pg.locator('#acq-loginunknown').isVisible(), false);
        assert.strictEqual(await pg.locator('#acq-user').isVisible(), false);
        assert.strictEqual(await pg.locator('#acq-password').isVisible(), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });
    await t.test('a start during a background load waits: what for and how long, stop the load, cancel the wait (TDM#673)', async () => {
      const {ctx,pg,errors,stops} = await setup(browser,{mode:'waiting', width:390});
      try {
        await pg.locator('#acq-user').fill(USER); await pg.locator('#acq-password').fill(PASSWORD);
        await pg.locator('#acq-check').click();
        const status = pg.locator('#acq-status');
        await pg.getByText(/Ждёт окончания фоновой загрузки «Synthetic load», идёт 1:\d\d\. Запуск начнётся сам\./).waitFor();
        assert.ok(!(await status.innerText()).includes('Повторите'), 'no refusal');
        const stop = pg.locator('#acq-result [data-stop-feed]');
        assert.strictEqual(await stop.innerText(), 'Остановить загрузку');
        assert.ok(await pg.locator('#acq-cancel').isEnabled(), 'the wait can be cancelled');
        assert.ok(await pg.locator('#acq-check').isDisabled());
        const overflow = await pg.locator('#acquisition-card').evaluate(n => n.scrollWidth > n.clientWidth + 1);
        assert.strictEqual(overflow, false);
        await pg.waitForTimeout(1600);  // polled again: the stop link stays, the clock runs on
        assert.strictEqual(await pg.locator('#acq-result [data-stop-feed]').count(), 1);
        await stop.click();
        await pg.getByText('Получение данных…',{exact:true}).waitFor();
        assert.deepStrictEqual(stops.map(p => [...p.entries()]), [[['target', 'feed']]]);
        assert.strictEqual(await pg.locator('#acq-result [data-stop-feed]').count(), 0);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
      const again = await setup(browser,{mode:'waiting'});
      try {
        await again.pg.locator('#acq-user').fill(USER); await again.pg.locator('#acq-password').fill(PASSWORD);
        await again.pg.locator('#acq-check').click();
        await again.pg.locator('#acq-result [data-stop-feed]').waitFor();
        await again.pg.locator('#acq-cancel').click();
        await again.pg.getByText('Ожидание отменено: ничего не запускалось.',{exact:true}).waitFor();
        assert.strictEqual(await again.pg.locator('#acq-result [data-stop-feed]').count(), 0);
        assert.strictEqual(again.stops.length, 0, 'the background load was not touched');
        assert.deepStrictEqual(again.errors, []);
      } finally { await again.ctx.close(); }
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
