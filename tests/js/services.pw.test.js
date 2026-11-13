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
    {id:'pick', label:'Найти', confirm:'', long:false}]},
  {id:'v2', title:'Второй сервис', help:'', fields:[], actions:[{id:'v2_check', label:'Проверить второй', confirm:'', long:true},
    {id:'v2_connect', label:'Подключить', confirm:'', long:true}, {id:'odd', label:'Странное', confirm:'', long:false}]}]};
const FEEDS = [
  {id:'terms', title:'Условия сделок', dataset:'trade_terms', source:'s1', enabled:true, enabled_default:true, ready:true, why_not_ready:'',
   schedule:{every_min:30, from:'08:00', to:'20:00', weekdays_only:true}, after_positions:true, running:false, queued:false,
   next_run:'2026-10-16T09:30:00+03:00', last:{last_started:'2026-10-16T09:00:00+03:00', last_finished:'2026-10-16T09:01:00+03:00',
     state:'complete', code:'fetched', lines:['Получена 1 сделка'], files:[{file:'terms.csv', status:'accepted'}],
     recomputed:[{run_id:41, label:'16.10 · закрытие 15.10', business_date:'2026-10-16'}], recompute_failed:[], failures:0, needs_login:false}},
  {id:'eodpos', title:'Позиции', dataset:'positions', source:'s2', enabled:true, enabled_default:false, ready:true, why_not_ready:'',
   schedule:{daily_at:['08:30']}, after_positions:false, running:false, queued:false, next_run:null,
   last:{last_started:'2026-10-16T08:30:00+03:00', state:'blocked', code:'login_required', needs_login:true, failures:0}}];

const HIST = {available:true, code:'available', feed:'trades', enabled:true, when:'any', from:'2026-01-05', history_from:null,
  from_min:'2026-01-05', from_default:'2026-01-05', backfill_enabled:true, paused:false, waiting:null, dates:10,
  counts:{fetched:4, no_trades:2, no_books:0, unmapped:1, unavailable:1, partial:0, todo:2, covered:0}, current:null,
  applied:{full:1, partial:1, outside:1, unknown:0, waiting_recompute:1, no_effect:0, in_pnl:2, reasons:{no_prev_close:1, lost:1}},
  last:null, next_at:null, gate:'off', pending:false, calls_today:0, limit_per_day:300,
  problems:[{date:'2026-10-13', state:'unavailable', code:'date_unavailable', attempts:1, next_at:'2026-10-17T18:00:00+00:00'},
    {date:'2026-10-12', state:'unmapped', code:'books_unmapped', attempts:0, next_at:null}]};
const BF = {enabled:true, from:'2026-01-05', to:'2026-10-15', dates:10, done:8, todo:2, no_positions:0, no_market:0, failed:0, hub:true,
  next:[], problem_dates:[], hub_problem_dates:[], stalled:null, oldest_run:'2026-01-05', first_positions:'2026-01-05', inbox_scanned_from:null};

async function setup(browser, {width = 1440, available = true, code = 'available', hash = '#adapter/services', http = 200, noLogin = false,
  history = HIST, bf = null, waitsHistory = false, waitFeed = false, cancelRefused = false, connectOk = false, concurrent = false, tradeCheck = false} = {}) {
  const ctx = await browser.newContext({viewport:{width, height:1000}}), pg = await ctx.newPage(), errors = [], posts = [], dialogs = [], summaries = [];
  const hist = history ? JSON.parse(JSON.stringify(history)) : null;
  let job = {state:'idle', code:'idle'}, polls = 0, feedRun = -1;
  const feeds = JSON.parse(JSON.stringify(FEEDS)), panel = JSON.parse(JSON.stringify(PANEL));
  // #731: the adapter flags a read-only action; a feed is fetching meanwhile (the worker is busy)
  const running = concurrent ? {kind:'feed', feed:'terms', since:new Date(Date.now() - 20000).toISOString(), phase:'fetching'} : null;
  if (tradeCheck) panel.sections[0].actions.push({id:'trade_check', label:'Проверить сделку', confirm:'', long:true});  // the adapter's «Проверить сделку»
  if (concurrent) { panel.sections[0].actions.push({id:'peek', label:'Посмотреть', confirm:'', long:false, concurrent:true}); feeds[0].running = true; }
  if (waitsHistory) feeds[0].waits_history = true;
  if (noLogin) { feeds[1].last = {last_started:'2026-10-16T08:30:00+03:00', state:'complete', code:'fetched', needs_login:false, failures:0}; }
  pg.on('pageerror', e => errors.push(e.message));
  pg.on('dialog', d => { dialogs.push(d.message()); d.accept(); });
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    const json = (data, status = 200) => route.fulfill({status, contentType:'application/json', body:JSON.stringify(data)});
    if (u.pathname === '/api/v1/auth/state') return json({enabled:false, user:null});
    if (u.pathname === '/api/v1/settings') return json({home:'/synthetic', effective:{}, inbox_dirs:[{path:'C:\\in', label:'', recursive:false}], env_overrides:[]});
    if (u.pathname === '/api/v1/adapter') return json({loaded:true, name:'synthetic', version:'0.1', describe:{sources:[]}, optional:[], actions:[], config_files:[], settings_tabs:[]});
    if (u.pathname === '/api/v1/acquisition') return json({available:false, code:'acquisition_unavailable', job:{state:'idle', phase:'idle', code:'idle'}});
    if (u.pathname === '/api/v1/adapter-services') {
      polls++;
      if (http !== 200) return json({message:'нет'}, http);
      if (job.state === 'running' && polls > 3) job = job.next;  // the long action ends after a few polls
      if (job.state === 'queued' && job.waiting_for && job.stopped && polls > 1) job = job.next;  // the feed stopped: the action runs
      const ti = feeds.find(x => x.id === 'trades_intraday');
      if (ti && feedRun >= 0 && ++feedRun === 2) Object.assign(ti, {queued:false, running:true});
      if (ti && feedRun === 4) Object.assign(ti, {running:false, last:{last_started:new Date().toISOString(), last_finished:new Date().toISOString(),
        state:'complete', code:'exported', files:[], dates:['2026-10-16'], failures:0, needs_login:false}});
      return json(available ? {available, code, name:'synthetic', version:'0.1', panel, feeds, job, running, scheduler:true}
        : {available:false, code, panel:null, feeds:[], job});
    }
    if (u.pathname === '/api/v1/backfill/status' && bf) return json(bf);
    if (u.pathname === '/api/v1/trades/summary') return summaries.push(u.searchParams.get('date')), json({date:'2026-10-16', trades:12, live:12, cancelled:0});
    if (u.pathname === '/api/v1/adapter-services/history' && req.method() === 'GET') return hist ? json(hist) : json({message:'нет'}, 404);
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
        const sec = a.startsWith('v2') || a === 'odd' ? 'v2' : 'conn', base = {op:'action', action:a, section:sec};
        if (a === 'login' || a === 'v2_check') { polls = 0; job = {...base, id:'j2' + a, state:'running', code:'running', long:true, login:a === 'login',
          started_at:new Date().toISOString(), next:a === 'login' ? {...base, id:'j2' + a, state:'complete', code:'logged_in', lines:['Вход выполнен']}
            : {...base, id:'j2' + a, state:'error', code:'failed', lines:['Проверка: сервер не ответил за 20 с', 'Что сделать: проверьте адрес']}}; }
        else if (a === 'peek') { polls = 0; job = {...base, id:'jp', state:'running', code:'running', long:false, login:false, started_at:new Date().toISOString(),
          next:{...base, id:'jp', state:'complete', code:'peeked', lines:['Только чтение: 5 строк'], refreshed:true, finished_at:new Date().toISOString()}};
          return json(job, 202); }   // the real server answers a flagged action as running at once (#731)
        else if (waitFeed && a === 'check') { polls = 0; job = {...base, id:'jw', state:'queued', code:'waiting_feed',
          waiting_for:{kind:'feed', feed:'terms', title:'Сделки дня', since:new Date(Date.now() - 190000).toISOString(), phase:'fetching'},
          next:{...base, id:'jw', state:'complete', code:'checked', lines:['Все сервисы отвечают']}}; }
        else if (a === 'trade_check') job = {...base, id:'jt', state:p.get('arg') === 'partial' ? 'partial' : 'complete', code:'trade_checked',
          lines:['Итог: получена (кэш фона от 2026-10-22), в условия войдёт.', 'Дата сделок: 2026-10-22']};
        else if (a === 'pick') job = {...base, id:'j3', state:'complete', code:'found', lines:['Найдено 2'], choose_action:'choose',
          choices:[{id:'x1', label:'Первый'}, {id:'x2', label:EVIL}]};
        else if (a === 'choose') job = {...base, id:'j4', state:'complete', code:'chosen', lines:['Выбрано: ' + p.get('arg')]};
        else if (a === 'v2_connect' && connectOk) {
          job = {...base, id:'j8', state:'complete', code:'connected', feed_queued:'trades_intraday', finished_at:new Date(Date.now() - 1000).toISOString(),
            lines:['✓ Настройка входа: готово (1 с)', '✓ Первая загрузка сделок: 12 сделок (4 с)'],
            table:{columns:['Шаг', 'Итог', 'Секунд', 'Код', 'id'], rows:[['Настройка входа', '✓', 1.4, 'v2_configured', 'setup'],
              ['Первая загрузка сделок', '✓', 4, 'exported', 'trades']]}};
          feeds.push({id:'trades_intraday', title:'Сделки дня', dataset:'trades', source:'s3', enabled:false, enabled_default:false, ready:true,
            schedule:null, after_positions:false, running:false, queued:true, next_run:null, last:{}});
          feedRun = 0;
        }
        else if (a === 'v2_connect') job = {...base, id:'j6', state:'error', code:'login_check_failed',
          lines:['✓ Настройка входа: готово (1 с)', '⚠ Портфели книг: подобраны для 1 из 2 книг (2 с)', '✗ Проверка API: сервер не ответил (20 с)',
            'Что сделать: проверьте адрес'],
          table:{columns:['Шаг', 'Итог', 'Секунд', 'Код', 'id'], rows:[['Настройка входа', '✓', 1.4, 'v2_configured', 'setup'],
            ['Портфели книг', '⚠', 2, 'portfolios_chosen', 'portfolios'],
            ['Проверка API', '✗', 20, 'timeout', 'check'], ['Первая загрузка сделок', '—', null, '', 'trades']]}};
        else if (a === 'odd') job = {...base, id:'j7', state:'partial', code:'zz_unknown_code', lines:['Сделано не всё: 2 из 3', 'Подробности']};
        else job = {...base, id:'j5', state:'complete', code:'connected', lines:['Подключено', EVIL],
          table:{columns:['Среда', 'Отчётов'], rows:[['test', 3], [EVIL, null]]}};
        return json({...job, state:'queued', code:'queued'}, 202);
      }
      if (u.pathname.endsWith('/history')) {
        for (const k of ['enabled', 'from', 'when']) if (p.has(k)) hist[k] = k === 'enabled' ? p.get(k) === 'true' : p.get(k);
        return json({state:'complete', code:'saved'});
      }
      if (u.pathname.endsWith('/history/refetch')) {
        if (p.get('from') === '2026-10-20') return json({state:'blocked', code:'invalid_request'}, 422);
        return json(p.get('confirm') === 'true' ? {state:'complete', code:'queued', dates:3} : {state:'confirm', code:'confirm', dates:3});
      }
      if (op === 'enabled') { const f = feeds.find(x => u.pathname.includes('/' + x.id + '/')); f.enabled = p.get('enabled') === 'true'; return json({state:'complete', code:'saved'}); }
      if (op === 'run') { feeds.find(x => u.pathname.includes('/' + x.id + '/')).queued = true; return json({state:'queued', code:'queued'}, 202); }
      if (op === 'cancel' && p.get('target') === 'feed' && job.waiting_for) {
        if (cancelRefused) return json({state:'blocked', code:'cancel_after_commit_unavailable'}, 409);
        job.stopped = true; polls = 0; return json(job);
      }
      return json({state:'idle', code:'idle'});
    }
    if (u.pathname.startsWith('/api/')) return json({}, 404);
    const file = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) return route.fulfill({status:404, body:''});
    return route.fulfill({status:200, contentType:TYPES[path.extname(file)] || 'application/octet-stream', body:fs.readFileSync(file)});
  });
  await pg.goto(ORIGIN + '/settings.html' + hash);
  return {ctx, pg, errors, posts, dialogs, summaries};
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
        // the save done: what was typed is forgotten, the panel is the server's again (else typing races the reset)
        await pg.waitForFunction(() => document.getElementById('svc-f-env').value === 'test');
        p = posts.filter(x => x[0].endsWith('/save'))[1][1];
        assert.strictEqual(p.get('f.password'), SECRET); assert.strictEqual(p.get('f.env'), 'prod'); assert.strictEqual(p.get('f.verbose'), 'true');

        await pg.locator('#svc-f-url').fill('bad');
        await pg.locator('#svc-save').click();
        await pg.getByText('Проверьте поле «Адрес».', {exact:true}).waitFor();
        assert.strictEqual(await pg.locator('#svc-f-url').getAttribute('aria-invalid'), 'true');
        await pg.locator('#svc-f-url').fill('https://synthetic.invalid');

        await pg.locator('button[data-action=check]').click();
        const conn = pg.locator('.svc-sres[data-for=conn]');
        await conn.locator('table').waitFor();
        const res = await conn.innerText();
        assert.ok(res.includes('Сервис подключён.') && res.includes('Подключено') && res.includes('Отчётов') && res.includes(EVIL), res);
        assert.strictEqual(await pg.locator('#svc-card img').count(), 0);
        assert.strictEqual(await pg.locator('#svc-result').innerText(), '', 'not at the bottom of the panel');
        const overflow = await pg.locator('#svc-card').evaluate(n => n.scrollWidth > n.clientWidth + 1);
        assert.strictEqual(overflow, false, 'no horizontal overflow on a phone');

        await pg.locator('button[data-action=pick]').click();
        await pg.locator('#svc-choice').waitFor();
        await pg.locator('#svc-choice').selectOption('x2');
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Найдено 2', 'an unknown code: the first line, not the code');
        assert.strictEqual(await conn.locator('.svc-st').getAttribute('title'), 'код: found');
        await pg.locator('#svc-choose').click();
        await pg.getByText('Выбрано: x2', {exact:true}).waitFor();
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Выбрано: x2');
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
        const conn = pg.locator('.svc-sres[data-for=conn]');
        await conn.getByText(/Выполняется… \d+:\d\d Если откроется окно входа, завершите вход в нём\./).waitFor();
        assert.ok(await pg.locator('#svc-cancel').isVisible());
        assert.ok(await conn.getByRole('button', {name:'Отменить'}).isVisible());
        assert.ok(await pg.locator('#svc-save').isDisabled());
        assert.strictEqual(await pg.locator('#svc-status').textContent(), '', 'the state is under the section, not at the top');
        await conn.getByText('Вход выполнен', {exact:true}).waitFor({timeout:10000});
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Вход выполнен.');
        assert.ok(posts.some(x => x[1].get('action') === 'login'));
        // a long action that is not a login: no hint of a login window; its failure in words, its lines under its own button
        await pg.locator('button[data-action=v2_check]').click();
        const v2 = pg.locator('.svc-sres[data-for=v2]');
        await v2.getByText(/Выполняется… \d+:\d\d/).waitFor();
        assert.doesNotMatch(await v2.innerText(), /окно входа/);
        await v2.getByText('Ошибка. Проверка: сервер не ответил за 20 с', {exact:true}).waitFor({timeout:10000});
        assert.strictEqual(await v2.locator('.svc-st').getAttribute('title'), 'код: failed');
        assert.ok((await v2.innerText()).includes('Что сделать: проверьте адрес'));
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Вход выполнен.', 'the result of the other section stays');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('no button shows a bare code; a checklist of steps with ✓/⚠/✗ and seconds', async () => {
      const {ctx, pg, errors} = await setup(browser, {width:390});
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await pg.locator('button[data-action=v2_connect]').click();
        const v2 = pg.locator('.svc-sres[data-for=v2]');
        await v2.locator('.svc-steps').waitFor();
        assert.strictEqual(await v2.locator('.svc-st').textContent(), 'Ошибка. Вход выполнен, но сервис не ответил на проверочный запрос.');
        assert.strictEqual(await v2.locator('.svc-st').getAttribute('title'), 'код: login_check_failed');
        const steps = await v2.locator('.svc-steps li').evaluateAll(n => n.map(x => [x.className, x.innerText.replace(/\s+/g, ' ').trim(), x.title]));
        assert.deepStrictEqual(steps, [['ok', '✓ Настройка входа: готово 1,4 с', 'код: v2_configured'],
          ['warn', '⚠ Портфели книг: подобраны для 1 из 2 книг 2 с', 'код: portfolios_chosen'],
          ['bad', '✗ Проверка API: сервер не ответил 20 с', 'код: timeout'], ['skip', '— Первая загрузка сделок', '']]);
        // ⚠ — свой цвет (--warn) в обеих темах, не серый «не дошли»; знак остаётся текстом
        for (const theme of ['light', 'dark']) {
          const c = await v2.evaluate((host, th) => {
            document.documentElement.dataset.theme = th;
            const probe = document.createElement('span'); probe.style.color = 'var(--warn)'; host.append(probe);
            const want = getComputedStyle(probe).color; probe.remove();
            const col = sel => getComputedStyle(host.querySelector(sel)).color;
            return {want, warn: col('.svc-steps li.warn .mk'), skip: col('.svc-steps li.skip .mk')};
          }, theme);
          assert.strictEqual(c.warn, c.want, theme + ': ⚠ in the warning colour');
          assert.notStrictEqual(c.warn, c.skip, theme + ': ⚠ is not drawn as «not reached»');
        }
        assert.deepStrictEqual(await v2.locator('.at-lines li').allTextContents(), ['Что сделать: проверьте адрес'], 'step lines only in the checklist');
        assert.strictEqual(await v2.locator('table').count(), 0, 'the checklist instead of the table');
        await pg.locator('button[data-action=odd]').click();
        await v2.getByText('Подробности', {exact:true}).waitFor();
        assert.strictEqual(await v2.locator('.svc-st').textContent(), 'Выполнено частично. Сделано не всё: 2 из 3');
        assert.strictEqual(await v2.locator('.at-lines li').count(), 1, 'the first line is in the state, not twice');
        assert.doesNotMatch(await pg.locator('#svc-card').innerText(), /zz_unknown_code|login_check_failed/);
        assert.strictEqual(await pg.locator('#svc-card').evaluate(n => n.scrollWidth > n.clientWidth + 1), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('a connect that got in: the checklist, then the trades feed it queued, the count and C20', async () => {
      const {ctx, pg, errors, summaries} = await setup(browser, {connectOk:true});
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await pg.locator('button[data-action=v2_connect]').click();
        const v2 = pg.locator('.svc-sres[data-for=v2]');
        await v2.locator('.svc-steps').waitFor();
        assert.strictEqual(await v2.locator('.svc-st').textContent(), 'Сервис подключён.');
        assert.deepStrictEqual(await v2.locator('.svc-steps li').evaluateAll(n => n.map(x => x.innerText.replace(/\s+/g, ' ').trim())),
          ['✓ Настройка входа: готово 1,4 с', '✓ Первая загрузка сделок: 12 сделок 4 с']);
        await v2.getByText(/Сделки дня: (загрузка в очереди|загружаются)…/).waitFor();
        await v2.getByText(/^Сделки дня: 12 сделок\./).waitFor({timeout:15000});
        assert.deepStrictEqual(summaries, ['2026-10-16'], 'counted for the business date the feed brought');
        assert.strictEqual(await v2.getByRole('link', {name:/Открыть C20/}).getAttribute('href'), 'concepts/c20-trades.html');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('an action that waits for a background feed names it and may stop it', async () => {
      const {ctx, pg, errors, posts} = await setup(browser, {waitFeed:true});
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await pg.locator('button[data-action=check]').click();
        const conn = pg.locator('.svc-sres[data-for=conn]');
        await conn.getByText(/^Ждёт окончания фоновой загрузки «Сделки дня», идёт 3:1\d\.$/).waitFor();
        const stop = conn.getByRole('button', {name:'Остановить загрузку'});
        assert.match(await stop.getAttribute('title'), /не считается ошибкой и запустится снова по своему расписанию/);
        await stop.click();
        await conn.getByText('Все сервисы отвечают', {exact:true}).waitFor({timeout:10000});
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Проверка сервисов завершена.');
        assert.deepStrictEqual(posts.filter(x => x[0].endsWith('/cancel')).map(x => Object.fromEntries(x[1])), [{target:'feed'}]);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
      const o = await setup(browser, {waitFeed:true, cancelRefused:true});
      try {
        await o.pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await o.pg.locator('button[data-action=check]').click();
        const conn = o.pg.locator('.svc-sres[data-for=conn]');
        await conn.getByRole('button', {name:'Остановить загрузку'}).click();
        await conn.getByText('Данные уже загружаются в базу: отмена на этой фазе недоступна.', {exact:true}).waitFor();
        assert.deepStrictEqual(o.errors, []);
      } finally { await o.ctx.close(); }
    });

    await t.test('«Проверить сделку»: a heading for its code, the verdict of the adapter is the first line below it', async () => {
      const {ctx, pg, errors} = await setup(browser, {tradeCheck:true});
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        await pg.locator('button[data-action=trade_check]').click();
        const conn = pg.locator('.svc-sres[data-for=conn]');
        await conn.getByText('Проверка сделки завершена.', {exact:true}).waitFor({timeout:10000});
        assert.strictEqual(await conn.locator('.svc-st').textContent(), 'Проверка сделки завершена.');
        const lines = await conn.locator('li, p').allInnerTexts();
        assert.ok(lines.some(x => x === 'Итог: получена (кэш фона от 2026-10-22), в условия войдёт.'), 'the verdict is a line, not the heading: ' + lines.join(' | '));
        assert.strictEqual(await conn.locator('.svc-st').getAttribute('title'), 'код: trade_checked');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('an action the adapter flagged as concurrent runs beside a fetching feed: no waiting, its own result (#731)', async () => {
      const {ctx, pg, errors, posts} = await setup(browser, {concurrent:true});
      try {
        await pg.waitForFunction(() => !document.getElementById('svc-save').disabled);
        assert.match(await pg.locator('#svc-feeds tr[data-feed=terms]').innerText(), /идёт/, 'the feed is fetching meanwhile');
        const peek = pg.locator('button[data-action=peek]');
        assert.strictEqual(await peek.isDisabled(), false, 'the action is offered while a feed runs');
        await peek.click();
        const conn = pg.locator('.svc-sres[data-for=conn]');
        await conn.getByText(/^Выполняется… \d+:\d\d$/).waitFor();
        assert.strictEqual(await conn.getByText(/Ждёт окончания/).count(), 0, 'no «ждёт окончания загрузки» for a flagged action');
        assert.strictEqual(await conn.getByRole('button', {name:'Остановить загрузку'}).count(), 0, 'there is no feed of its to stop');
        await conn.getByText('Только чтение: 5 строк', {exact:true}).waitFor({timeout:10000});
        assert.deepStrictEqual(posts.filter(x => x[0].endsWith('/action')).map(x => x[1].get('action')), ['peek']);
        assert.match(await pg.locator('#svc-feeds tr[data-feed=terms]').innerText(), /идёт/, 'the feed was not touched by it');
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

    await t.test('прямой адрес #adapter/services: открыт подраздел «Сервисы», его пункт виден и выбран', async () => {
      const {ctx, pg, errors} = await setup(browser);
      try {
        await pg.locator('#svc-card').waitFor({state:'visible'});
        assert.strictEqual(await pg.evaluate(() => location.hash), '#adapter/services');
        assert.strictEqual(await pg.locator('#sec-adapter .sub.on').getAttribute('data-sub'), 'services');
        const link = pg.locator('.subnav a[data-sub-link="services"]');
        assert.ok(await link.isVisible());
        assert.strictEqual(await link.getAttribute('aria-current'), 'page');
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('«нужен вход» на виду: сводка, точки, строка в «Обзоре»; без входа — «работают»', async () => {
      const {ctx, pg, errors} = await setup(browser, {hash:'#adapter/overview'});
      try {
        await pg.waitForFunction(() => /нужен вход/.test(document.getElementById('svc-line').textContent));
        const item = pg.locator('#st-sum a[href="#adapter/services"]');
        assert.match(await item.innerText(), /Сервисы:\s*нужен вход: Позиции/);
        assert.strictEqual(await pg.locator('.subnav .dot[data-subdot="services"]').getAttribute('class'), 'dot warn');
        assert.match(await pg.locator('.st-nav .dot[data-nav="adapter"]').getAttribute('class'), /\bwarn\b/);
        assert.match(await pg.locator('#svc-line').innerText(), /Подключение к сервисам: нужен вход: Позиции\s+открыть →/);
        await item.click();
        await pg.waitForFunction(() => document.querySelector('#sec-adapter .sub.on').dataset.sub === 'services');
        assert.ok(await pg.locator('#svc-card').isVisible());
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
      const ok = await setup(browser, {hash:'#adapter/overview', noLogin:true});
      try {
        await ok.pg.waitForFunction(() => /работают/.test(document.getElementById('svc-line').textContent));
        assert.strictEqual(await ok.pg.locator('#st-sum a[href="#adapter/services"]').innerText(), 'Сервисы', 'зелёный пункт — только название');
        assert.match(await ok.pg.locator('#st-sum a[href="#adapter/services"]').getAttribute('title'), /работает/);
        assert.match(await ok.pg.locator('#st-sum a[href="#adapter/services"]').getAttribute('class'), /\bok\b/);
        assert.doesNotMatch(await ok.pg.locator('.subnav .dot[data-subdot="services"]').getAttribute('class'), /warn|bad/);
        assert.deepStrictEqual(ok.errors, []);
      } finally { await ok.ctx.close(); }
    });

    await t.test('адаптер без сервисов (не объявлены или 404): нет пункта, строки в сводке и «Обзоре»; адрес открывает запасной подраздел', async () => {
      for (const opt of [{available:false, code:'services_unavailable'}, {http:404}]) {
        const {ctx, pg, errors} = await setup(browser, opt);
        try {
          await pg.waitForSelector('#st-sum:not([hidden]) a');
          await pg.waitForTimeout(300);
          assert.strictEqual(await pg.locator('.subnav a[data-sub-link="services"]').isVisible(), false);
          assert.strictEqual(await pg.locator('#svc-card').isVisible(), false);
          assert.doesNotMatch(await pg.locator('#st-sum').innerText(), /Сервисы/);
          assert.strictEqual(await pg.evaluate(() => document.getElementById('svc-line').textContent), '');
          assert.strictEqual(await pg.locator('#sec-adapter .sub.on').count(), 1);
          assert.notStrictEqual(await pg.locator('#sec-adapter .sub.on').getAttribute('data-sub'), 'services');
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });

    await t.test('поиск: «подключение к сервисам» и поле панели находятся при открытом месте и ведут в «Сервисы»', async () => {
      const {ctx, pg, errors} = await setup(browser, {hash:'#hub'});
      try {
        await pg.locator('#st-sum a[href="#adapter/services"]').waitFor();
        const q = pg.locator('#st-q');
        await q.fill('Тестовые сервисы');  // заголовок блока — название панели, которое дал адаптер
        await pg.locator('#st-q-res .st-q-it').first().waitFor();
        await q.press('Enter');
        await pg.waitForFunction(() => document.querySelector('#sec-adapter .sub.on') && document.querySelector('#sec-adapter .sub.on').dataset.sub === 'services');
        assert.ok(await pg.locator('#svc-card').isVisible());
        await q.fill('Адрес');
        assert.match(await pg.locator('#st-q-res').innerText(), /Адрес[\s\S]*Сервисы/);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
      const off = await setup(browser, {hash:'#hub', available:false, code:'services_unavailable'});
      try {
        await off.pg.waitForSelector('#st-sum:not([hidden]) a');
        await off.pg.waitForTimeout(300);
        await off.pg.locator('#st-q').fill('Тестовые сервисы');
        assert.match(await off.pg.locator('#st-q-res').innerText(), /Ничего не нашлось/);
      } finally { await off.ctx.close(); }
    });

    await t.test('the trades of past dates: summary, switch with the first date, hours, the period button with the number of dates', async () => {
      const {ctx, pg, errors, posts, dialogs} = await setup(browser, {width: 390, bf: BF});
      try {
        await pg.locator('#svc-hist-box').waitFor();
        await pg.waitForFunction(() => document.getElementById('svc-hist-sum').textContent.length > 0);
        assert.strictEqual(await pg.locator('#svc-hist-sum').textContent(),
          'Сделки прошлых дат: получено 4 из 10 дат · без сделок 2 · не сопоставлено 1 · недоступно 1 · ждут запроса 2');
        const applied = await pg.locator('#svc-hist-applied').textContent();
        assert.match(applied, /PnL сделок учтён полностью в 1 дате, частично в 1 дате, вне PnL в 1 дате \(/);
        assert.match(applied, /нет предыдущего закрытия: 1/); assert.match(applied, /Ждут пересчёта: 1/);
        assert.strictEqual(await pg.locator('#svc-hist-on').isChecked(), true, 'по умолчанию включено');
        assert.strictEqual(await pg.locator('#svc-hist-from').inputValue(), '2026-01-05');
        assert.strictEqual(await pg.locator('#svc-hist-when').inputValue(), 'any');
        assert.match(await pg.locator('#svc-hist-when option:checked').textContent(), /В любое время/);
        await pg.locator('#svc-hist-diag summary').click();
        const diag = await pg.locator('#svc-hist-diag').innerText();
        assert.ok(diag.includes('13.10 — недоступно: сервис не отдаёт эту дату, попыток: 1') && diag.includes('12.10 — не сопоставлено: книги сервиса не сопоставлены с книгами монитора'));
        assert.ok(!diag.includes('date_unavailable') && !diag.includes('books_unmapped'), 'no raw codes');
        assert.match(await pg.locator('#svc-hist-box').innerText(), /будни 09:30–19:45 по Москве/);
        // the page itself does not overflow on a phone
        assert.strictEqual(await pg.locator('#svc-card').evaluate(n => n.scrollWidth > n.clientWidth + 1), false);

        await pg.locator('#svc-hist-from').fill('2026-03-02');
        await pg.waitForFunction(() => document.getElementById('svc-hist-from').value === '2026-03-02');
        await pg.locator('#svc-hist-box .tgl-s').click();
        await pg.waitForFunction(() => !document.getElementById('svc-hist-on').checked);
        await pg.locator('#svc-hist-box .tgl-s').click();
        await pg.waitForFunction(() => document.getElementById('svc-hist-on').checked);
        // the field changes before its POST reaches the route: wait for the requests, not the field
        const histPosts = () => posts.filter(x => x[0].endsWith('/adapter-services/history')).map(x => Object.fromEntries(x[1]));
        for (const t0 = Date.now(); histPosts().length < 3 && Date.now() - t0 < 5000;) await pg.waitForTimeout(50);
        let p = histPosts();
        assert.deepStrictEqual(p, [{from:'2026-03-02'}, {enabled:'false'}, {enabled:'true', from:'2026-03-02'}]);
        await pg.locator('#svc-hist-when').selectOption('off_hours');
        await pg.waitForFunction(() => document.getElementById('svc-hist-when').value === 'off_hours');
        for (const t0 = Date.now(); histPosts().length < 4 && Date.now() - t0 < 5000;) await pg.waitForTimeout(50);
        p = histPosts();
        assert.deepStrictEqual(p.pop(), {when:'off_hours'});

        await pg.locator('#svc-hist-refetch').click();
        await pg.locator('#svc-hist-rf').fill('2026-10-01'); await pg.locator('#svc-hist-rt').fill('2026-10-15');
        await pg.locator('#svc-hist-go').click();
        await pg.getByText('Поставлено в очередь дат: 3.', {exact:true}).waitFor();
        assert.strictEqual(dialogs.length, 1);
        assert.match(dialogs[0], /за 3 даты \(01\.10\.2026 — 15\.10\.2026\)/);
        const rf = posts.filter(x => x[0].endsWith('/history/refetch')).map(x => Object.fromEntries(x[1]));
        assert.deepStrictEqual(rf, [{from:'2026-10-01', to:'2026-10-15'}, {from:'2026-10-01', to:'2026-10-15', confirm:'true'}]);
        await pg.locator('#svc-hist-refetch').click();
        await pg.locator('#svc-hist-rf').fill('2026-10-20');
        await pg.locator('#svc-hist-go').click();
        await pg.getByText('Проверьте заполненные поля.', {exact:true}).waitFor();
        assert.strictEqual(dialogs.length, 1, 'no confirmation for a refused period');

        // the settings page, «История прогонов»: how many dates have the PnL of their trades
        await pg.waitForFunction(() => /Сделки прошлых дат/.test(document.getElementById('bf-rows').textContent));
        assert.match(await pg.locator('#bf-rows').textContent(), /PnL сделок учтён в 2 датах \(полностью 1, частично 1\), вне PnL 1, ждут пересчёта 1/);
        assert.strictEqual(await pg.locator('#svc-hist-box button, #svc-hist-box a').evaluateAll(n => n.some(x => /desk\.bat|командн/i.test(x.textContent))), false);
        assert.deepStrictEqual(errors, []);
      } finally { await ctx.close(); }
    });

    await t.test('the trades of past dates: a login pause, trading hours, backfill off, an adapter without the feature', async () => {
      const waits = {...HIST, enabled:true, paused:true, waiting:'login_required', gate:null, current:null, backfill_enabled:false,
        counts:{...HIST.counts, todo:0}, problems:[]};
      let o = await setup(browser, {history: waits});
      try {
        await o.pg.waitForFunction(() => /нужен вход/.test((document.getElementById('svc-hist-next') || {}).textContent || ''));
        assert.match(await o.pg.locator('#svc-hist-applied').textContent(), /Ждут пересчёта: 1 \(история прогонов выключена\)/);
        assert.strictEqual(await o.pg.locator('#svc-hist-diag').isHidden(), true);
      } finally { await o.ctx.close(); }
      const hours = {...HIST, enabled:true, gate:'trading_hours', next_at:'2026-10-16T16:45:00+00:00', current:null};
      o = await setup(browser, {history: hours});
      try {
        await o.pg.waitForFunction(() => /ждёт конца торговых часов, дальше в/.test((document.getElementById('svc-hist-next') || {}).textContent || ''));
      } finally { await o.ctx.close(); }
      const empty = {...HIST, enabled:true, gate:'no_trades_week', next_at:null, current:null};   // TDM#677: «Сделки дня» пусты неделю
      o = await setup(browser, {history: empty});
      try {
        await o.pg.waitForFunction(() => /ждёт сделок: «Сделки дня» не принесли ни одной сделки за последние 5 рабочих дней/.test((document.getElementById('svc-hist-next') || {}).textContent || ''));
        assert.doesNotMatch(await o.pg.locator('#svc-hist-next').textContent(), /дальше в/);
      } finally { await o.ctx.close(); }
      const now = {...HIST, enabled:true, gate:null, current:'2026-10-14'};
      o = await setup(browser, {history: now, waitsHistory: true});
      try {
        await o.pg.waitForFunction(() => /^Сейчас: 14\.10/.test((document.getElementById('svc-hist-next') || {}).textContent || ''));
        assert.strictEqual(await o.pg.locator('#svc-hist-stop').isVisible(), true);
        assert.match(await o.pg.locator('tr[data-feed=terms]').innerText(), /ждёт запроса прошлой даты/);
        assert.strictEqual(await o.pg.locator('#svc-hist-stop').textContent(), 'Остановить запрос');
        assert.match(await o.pg.locator('#svc-hist-stop').getAttribute('title'), /со следующей даты через пару минут; чтобы выключить — снимите переключатель/);
        await o.pg.locator('#svc-hist-stop').click();
        await o.pg.waitForFunction(() => true);
        await o.pg.waitForTimeout(200);
        assert.deepStrictEqual(o.posts.filter(x => x[0].endsWith('/cancel')).map(x => Object.fromEntries(x[1])), [{target:'feed'}]);
      } finally { await o.ctx.close(); }
      const none = {available:false, code:'history_unsupported', feed:null, enabled:false, when:'off_hours', from:'2026-01-05', counts:{}, applied:null, problems:[]};
      o = await setup(browser, {history: none});
      try {
        await o.pg.waitForFunction(() => document.getElementById('svc-hist-sum') && document.getElementById('svc-hist-sum').textContent.length > 0);
        assert.strictEqual(await o.pg.locator('#svc-hist-sum').textContent(), 'Адаптер не умеет сделки прошлых дат.');
        assert.strictEqual(await o.pg.locator('#svc-hist-on').isDisabled(), true);
        assert.strictEqual(await o.pg.locator('#svc-hist-refetch').isDisabled(), true);
      } finally { await o.ctx.close(); }
      o = await setup(browser, {history: null});   // the route is not there (an older program): the block stays hidden
      try {
        await o.pg.locator('#svc-card').waitFor();
        await o.pg.waitForTimeout(400);
        assert.strictEqual(await o.pg.locator('#svc-hist-box').isHidden(), true);
      } finally { await o.ctx.close(); }
    });

    await t.test('without the capability the block stays hidden; a broken one says why', async () => {
      for (const [code, hidden] of [['services_unavailable', true], ['capability_invalid', false]]) {
        const {ctx, pg, errors} = await setup(browser, {available:false, code});
        try {
          await pg.locator('#st-sum a').first().waitFor();
          await pg.waitForTimeout(300);
          assert.strictEqual(await pg.locator('#svc-card').isHidden(), hidden);
          assert.strictEqual(await pg.locator('.subnav a[data-sub-link="services"]').isHidden(), hidden, 'пункт «Сервисы» — как карточка');
          if (!hidden) assert.match(await pg.locator('#svc-status').innerText(), /несовместимом виде/);
          assert.deepStrictEqual(errors, []);
        } finally { await ctx.close(); }
      }
    });
  } finally { await browser.close(); }
});
