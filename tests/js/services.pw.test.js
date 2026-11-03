// «Подключение к сервисам» in Settings (08 §2.2, #548): a synthetic panel and feeds only. No private implementation.
'use strict';
const test = require('node:test'), assert = require('assert');
const fs = require('fs'), path = require('path');
const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (_) { try { return require('playwright'); } catch (_) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://127.0.0.1:8100', SECRET = 'SYNTH_BROWSER_SECRET';
const EVIL = 'Synthetic <img src=x onerror="window.svcInjected=true">';
const TYPES = {'.html':'text/html', '.js':'application/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.woff2':'font/woff2'};
const PANEL = {title:'Тестовые сервисы', status:['Среда: тест', EVIL], sections:[{id:'conn', title:'Подключение', help:EVIL, fields:[
  {key:'url', label:'Адрес', type:'url', value:'https://synthetic.invalid', help:'', placeholder:''},
  {key:'password', label:'Пароль', type:'secret', value:'', set:true, help:'', placeholder:''},
  {key:'env', label:'Среда', type:'select', value:'test', options:[{value:'test', label:'Тест'}, {value:'prod', label:'Прод'}], help:'', placeholder:''},
  {key:'verbose', label:'Подробно', type:'bool', value:false, help:'', placeholder:''},
  {key:'port', label:'Порт', type:'int', value:443, help:'', placeholder:''}],
  actions:[{id:'check', label:'Проверить', confirm:'', long:false}, {id:'login', label:'Войти', confirm:'Откроется окно входа', long:true},
    {id:'pick', label:'Найти', confirm:'', long:false}]}]};
const FEEDS = [
  {id:'terms', title:'Условия сделок', dataset:'trade_terms', source:'s1', enabled:true, enabled_default:true, ready:true, why_not_ready:'',
   schedule:{every_min:30, from:'08:00', to:'20:00', weekdays_only:true}, after_positions:true, running:false, queued:false,
   next_run:'2026-10-16T09:30:00+03:00', last:{last_started:'2026-10-16T09:00:00+03:00', last_finished:'2026-10-16T09:01:00+03:00',
     state:'complete', code:'fetched', lines:['Получена 1 сделка'], files:[{file:'terms.csv', status:'accepted'}],
     recomputed:[{run_id:41, label:'16.10 · закрытие 15.10', business_date:'2026-10-16'}], recompute_failed:[], failures:0, needs_login:false}},
  {id:'eodpos', title:'Позиции', dataset:'positions', source:'s2', enabled:true, enabled_default:false, ready:true, why_not_ready:'',
   schedule:{daily_at:['08:30']}, after_positions:false, running:false, queued:false, next_run:null,
   last:{last_started:'2026-10-16T08:30:00+03:00', state:'blocked', code:'login_required', needs_login:true, failures:0}}];

async function setup(browser, {width = 1440, available = true, code = 'available'} = {}) {
  const ctx = await browser.newContext({viewport:{width, height:1000}}), pg = await ctx.newPage(), errors = [], posts = [];
  let job = {state:'idle', code:'idle'}, polls = 0;
  const feeds = JSON.parse(JSON.stringify(FEEDS));
  pg.on('pageerror', e => errors.push(e.message));
  pg.on('dialog', d => d.accept());
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    const json = (data, status = 200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(data)});
    if (u.pathname === '/api/v1/auth/state') return json({enabled:false, user:null});
    if (u.pathname === '/api/v1/settings') return json({home:'/synthetic', effective:{}, inbox_dirs:[], env_overrides:[]});
    if (u.pathname === '/api/v1/adapter') return json({loaded:false, name:'synthetic', error:'not loaded', config_files:[], settings_tabs:[]});
    if (u.pathname === '/api/v1/acquisition') return json({available:false, code:'acquisition_unavailable', job:{state:'idle', phase:'idle', code:'idle'}});
    if (u.pathname === '/api/v1/adapter-services') {
      polls++;
      if (job.state === 'running' && polls > 3) job = job.next;  // the long action ends after a few polls
      return json(available ? {available, code, name:'synthetic', version:'0.1', panel:PANEL, feeds, job, running:null, scheduler:true}
        : {available:false, code, panel:null, feeds:[], job});
    }
    if (u.pathname.startsWith('/api/v1/adapter-services/')) {
      assert.strictEqual(req.method(), 'POST');
      assert.strictEqual(req.headers()['content-type'], 'application/x-www-form-urlencoded');
      assert.strictEqual(u.search, '');
      const p = new URLSearchParams(req.postData() || ''); posts.push([u.pathname, p]);
      const op = u.pathname.split('/').pop();
      if (op === 'save') {
        job = p.get('f.url') === 'bad' ? {id:'j1', op:'save', state:'blocked', code:'config_invalid', field:'url'} : {id:'j1', op:'save', state:'complete', code:'saved'};
        return json({id:'j1', op:'save', state:'queued', code:'queued'}, 202);
      }
      if (op === 'action') {
        const a = p.get('action');
        if (a === 'login') { polls = 0; job = {id:'j2', op:'action', state:'running', code:'running', long:true, started_at:new Date().toISOString(),
          next:{id:'j2', op:'action', state:'complete', code:'logged_in', lines:['Вход выполнен']}}; }
        else if (a === 'pick') job = {id:'j3', op:'action', state:'complete', code:'found', lines:['Найдено 2'], choose_action:'choose',
          choices:[{id:'x1', label:'Первый'}, {id:'x2', label:EVIL}]};
        else if (a === 'choose') job = {id:'j4', op:'action', state:'complete', code:'chosen', lines:['Выбрано: ' + p.get('arg')]};
        else job = {id:'j5', op:'action', state:'complete', code:'connected', lines:['Подключено', EVIL],
          table:{columns:['Среда', 'Отчётов'], rows:[['test', 3], [EVIL, null]]}};
        return json({...job, state:'queued', code:'queued'}, 202);
      }
      if (op === 'enabled') { const f = feeds.find(x => u.pathname.includes('/' + x.id + '/')); f.enabled = p.get('enabled') === 'true'; return json({state:'complete', code:'saved'}); }
      if (op === 'run') { feeds.find(x => u.pathname.includes('/' + x.id + '/')).queued = true; return json({state:'queued', code:'queued'}, 202); }
      return json({state:'idle', code:'idle'});
    }
    if (u.pathname.startsWith('/api/')) return json({}, 404);
    const file = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({status:404, body:''});
    return route.fulfill({status:200, contentType:TYPES[path.extname(file)] || 'application/octet-stream', body:fs.readFileSync(file)});
  });
  await pg.goto(ORIGIN + '/settings.html#adapter');
  return {ctx, pg, errors, posts};
}

test('services block in Settings', {skip: pw ? false : 'нет Playwright'}, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('a generic panel: fields, save with a typed secret only, actions, table and choices', async () => {
      const {ctx, pg, errors, posts} = await setup(browser, {width:390});
      try {
        await pg.locator('#svc-card').waitFor();
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        assert.strictEqual(await pg.locator('#svc-title').textContent(), 'Тестовые сервисы');
        const text = await pg.locator('#svc-card').innerText();
        assert.ok(text.includes(EVIL) && text.includes('Среда: тест'));
        assert.strictEqual(await pg.locator('#svc-card img, #svc-card script').count(), 0);
        assert.strictEqual(await pg.evaluate(() => window.svcInjected), undefined);
        assert.strictEqual(await pg.locator('#svc-f-password').getAttribute('type'), 'password');
        assert.strictEqual(await pg.locator('#svc-f-password').inputValue(), '');
        assert.match(await pg.locator('#svc-f-password').getAttribute('placeholder'), /сохранён/);
        assert.strictEqual(await pg.locator('#svc-f-env').inputValue(), 'test');
        assert.strictEqual(await pg.locator('#svc-f-port').inputValue(), '443');

        await pg.locator('#svc-save').click();  // nothing typed into the secret: it is not sent
        await pg.getByText('Настройки сохранены.', {exact:true}).waitFor();
        let p = posts.find(x => x[0].endsWith('/save'))[1];
        assert.ok(!p.has('f.password'));
        assert.strictEqual(p.get('f.url'), 'https://synthetic.invalid');
        assert.strictEqual(p.get('f.verbose'), 'false'); assert.strictEqual(p.get('f.port'), '443');

        await pg.locator('#svc-f-password').fill(SECRET);
        await pg.locator('#svc-f-env').selectOption('prod');
        await pg.locator('#svc-f-verbose').check();
        assert.strictEqual(await pg.locator('#savebar').isVisible(), false, 'the panel is not settings.json');
        await pg.locator('#svc-save').click();
        await pg.waitForFunction(() => !document.getElementById('svc-f-password').value);
        p = posts.filter(x => x[0].endsWith('/save'))[1][1];
        assert.strictEqual(p.get('f.password'), SECRET); assert.strictEqual(p.get('f.env'), 'prod'); assert.strictEqual(p.get('f.verbose'), 'true');

        await pg.locator('#svc-f-url').fill('bad');
        await pg.locator('#svc-save').click();
        await pg.getByText('Проверьте поле «Адрес».', {exact:true}).waitFor();
        assert.strictEqual(await pg.locator('#svc-f-url').getAttribute('aria-invalid'), 'true');
        await pg.locator('#svc-f-url').fill('https://synthetic.invalid');

        await pg.locator('button[data-action=check]').click();
        await pg.locator('#svc-result table').waitFor();
        const res = await pg.locator('#svc-result').innerText();
        assert.ok(res.includes('Подключено') && res.includes('Отчётов') && res.includes(EVIL));
        assert.strictEqual(await pg.locator('#svc-result img').count(), 0);
        const overflow = await pg.locator('#svc-card').evaluate(n => n.scrollWidth > n.clientWidth + 1);
        assert.strictEqual(overflow, false, 'no horizontal overflow on a phone');

        await pg.locator('button[data-action=pick]').click();
        await pg.locator('#svc-choice').waitFor();
        await pg.locator('#svc-choice').selectOption('x2');
        await pg.locator('#svc-choose').click();
        await pg.getByText('Выбрано: x2', {exact:true}).waitFor();
        p = posts.filter(x => x[0].endsWith('/action')).pop()[1];
        assert.strictEqual(p.get('action'), 'choose'); assert.strictEqual(p.get('arg'), 'x2');

        const persisted = await pg.evaluate(() => JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage}) + document.body.innerText);
        assert.ok(!persisted.includes(SECRET));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('a long action shows its progress, then its result', async () => {
      const {ctx, pg, errors, posts} = await setup(browser);
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await pg.locator('button[data-action=login]').click();  // its confirm is accepted
        await pg.getByText(/Выполняется… \d+:\d\d Если откроется окно входа/).waitFor();
        assert.ok(await pg.locator('#svc-cancel').isVisible());
        assert.ok(await pg.locator('#svc-save').isDisabled());
        await pg.getByText('Вход выполнен', {exact:true}).waitFor({timeout:10000});
        assert.ok(posts.some(x => x[1].get('action') === 'login'));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('feeds: schedule, last run, «нужен вход», the switch and «Запустить сейчас»', async () => {
      const {ctx, pg, errors, posts} = await setup(browser);
      try {
        await pg.locator('#svc-feeds tr[data-feed=terms]').waitFor();
        const terms = await pg.locator('#svc-feeds tr[data-feed=terms]').innerText();
        assert.ok(terms.includes('каждые 30 мин, 08:00–20:00, по будням; и после новых позиций'), terms);
        assert.ok(terms.includes('получено'));
        await pg.locator('#svc-feeds tr[data-feed=terms] summary').click();
        const more = await pg.locator('#svc-feeds tr[data-feed=terms] details').innerText();
        assert.ok(more.includes('terms.csv: принят') && more.includes('Пересчитаны: 16.10 · закрытие 15.10 (#41)'), more);
        const pos = await pg.locator('#svc-feeds tr[data-feed=eodpos]').innerText();
        assert.ok(pos.includes('ежедневно в 08:30') && pos.includes('нужен вход'), pos);
        await pg.locator('#svc-feeds tr[data-feed=terms] .tgl').click();
        await pg.waitForFunction(() => !document.querySelector('#svc-feeds tr[data-feed=terms] input').checked);
        assert.strictEqual(posts.find(x => x[0].endsWith('/feeds/terms/enabled'))[1].get('enabled'), 'false');
        await pg.locator('#svc-feeds tr[data-feed=eodpos] button', {hasText:'Запустить сейчас'}).click();
        await pg.waitForFunction(() => document.querySelector('#svc-feeds tr[data-feed=eodpos]').innerText.includes('в очереди') ||
          document.querySelector('#svc-feeds tr[data-feed=eodpos]').innerText.includes('нужен вход'));
        assert.ok(posts.some(x => x[0].endsWith('/feeds/eodpos/run')));
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('without the capability the block stays hidden; a broken one says why', async () => {
      for (const [code, hidden] of [['services_unavailable', true], ['capability_invalid', false]]) {
        const {ctx, pg, errors} = await setup(browser, {available:false, code});
        try {
          await pg.locator('#acquisition-card').waitFor();
          await pg.waitForTimeout(300);
          assert.strictEqual(await pg.locator('#svc-card').isHidden(), hidden);
          if (!hidden) assert.match(await pg.locator('#svc-status').innerText(), /несовместимом виде/);
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });
  } finally { await browser.close(); }
});
