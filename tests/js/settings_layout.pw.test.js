// Экран настроек короткий и понятный (TDM#449), в браузере (Playwright), сервер — заглушка маршрутов:
//  - сводка сверху «что работает / что требует внимания» со ссылками в разделы;
//  - редкое — в свёрнутых «Дополнительно»; открытость помнит браузер; блок с ошибкой раскрывается сам; #bf, #sess-card
//    открывают свой раздел и блок;
//  - у поля — ключ settings.json мелко; «определено автоматически» с «изменить» / «вернуть авто»;
//  - подсказки автоопределения (GET /api/v1/settings/detect): папка из настроек адаптера — в «Папки с отчётами» одной
//    кнопкой, Python для получения условий сделок — только подстановкой.
//   node --test tests/js/settings_layout.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';
const PY = 'C:\\Users\\u\\AppData\\Local\\Programs\\Python\\Python311\\python.exe';

function server() {
  const st = { puts: [], hubOk: false };
  st.settings = { home: HOME, hub_url: 'https://hub.example', inbox_dirs: [{ path: 'C:\\in', label: 'Позиции', recursive: false }],
    hub_token_set: false, proxy_password_set: false, env_overrides: [], error: null,
    effective: { home: HOME, hub_url: 'https://hub.example', adapter_output_dir: HOME + '\\adapter-output', private_adapter: 'C:\\desk\\adapter',
      private_adapter_default: true, private_adapter_config: HOME + '\\adapter-config', snapshot_cache: HOME + '\\snapshots',
      inbox_archive: HOME + '\\inbox-archive', autopilot: true } };
  st.detect = { proxy: { settings: null, environment: null, windows: 'http://proxy.example:3128', pac: false, system: 'http://proxy.example:3128',
    source: 'windows', effective_source: 'windows' }, trust: { kind: 'system', path: null, exists: null }, curl: 'C:\\Windows\\System32\\curl.exe',
    python: [{ path: PY, source: 'registry', version: '3.11', own: false }, { path: 'C:\\desk\\python\\python.exe', source: 'own', version: '3.12', own: true }],
    acquisition_python: { path: null, exists: null },
    defaults: { snapshot_cache: HOME + '\\snapshots', inbox_archive: HOME + '\\inbox-archive', private_adapter_config: HOME + '\\adapter-config' },
    adapter: { default: 'C:\\desk\\adapter', config_default: HOME + '\\adapter-config', config_default_exists: true, config_files: 1 },
    inbox: { from_adapter_config: [{ path: 'D:\\Выгрузки', recursive: false, file: 'options.json', key: 'report_dirs[1]', exists: true }],
      from_accepted: [{ path: 'E:\\Старые', files: 3, last_report: '2026-10-21', exists: true }], accepted_error: null } };
  st.handle = (u, method, body) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return st.settings;
    if (p === '/api/v1/settings' && method === 'PUT') { st.puts.push(body); Object.assign(st.settings, body); return st.settings; }
    if (p === '/api/v1/settings/detect') return st.detect;
    if (p === '/api/v1/settings/test-hub') return st.hubOk ? { ok: true, host: 'hub.example' } : { ok: false, error: 'прокси: 407', host: 'hub.example' };
    if (p === '/api/v1/adapter') return { loaded: true, path: 'C:\\desk\\adapter', reference: false, default: true, name: 'synthetic', version: '0.9.0',
      fingerprint: 'cd'.repeat(32), config_dir: HOME + '\\adapter-config', describe: { contract: 'private/1.5', acquisition: { version: 1 } },
      config_files: [], settings_tabs: [], optional: [], actions: [], warnings: [], error: null };
    if (p === '/api/v1/autopilot/status') return { enabled: true, state: 'idle', steps: [{ name: 'sync', status: 'ok', detail: 'срезов: 1', at: '' }] };
    if (p === '/api/v1/backfill/status') return { enabled: true, dates: 20, done: 10, todo: 5, no_positions: 4, no_market: 1, failed: 0 };
    if (p === '/api/v1/acquisition') return { available: false, code: 'runtime_unavailable', job: { state: 'idle', phase: 'idle', code: 'idle' } };
    return null;
  };
  return st;
}

async function open(browser, st, hash, opts) {
  const ctx = (opts && opts.ctx) || await browser.newContext(opts && opts.viewport ? { viewport: opts.viewport } : {});
  const pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      let body = null; try { body = req.postDataJSON(); } catch (e) { /* GET */ }
      const out = st.handle(u, req.method(), body);
      return out ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html' + hash);
  await pg.waitForSelector('#st-sum:not([hidden]) a');
  await pg.waitForFunction(() => document.querySelector('#dirs-hint .sug'));
  return { pg, ctx, errors };
}

test('настройки: главное сверху, редкое по кнопке, автоопределение (TDM#449)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('сводка, ключи полей, папка из настроек адаптера — одной кнопкой', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter');
      await pg.waitForFunction(() => /Адаптер/.test(document.getElementById('st-sum').innerText));
      const sum = await pg.locator('#st-sum').innerText();
      assert.ok(/Требует внимания: 2/.test(sum) && /Токен:\s*не задан/.test(sum) && /Папки отчётов:\s*1 папка · подсказка/.test(sum), sum);
      assert.ok(/Хаб:\s*работает/.test(sum) && /Адаптер:\s*загружен/.test(sum) && /История прогонов:\s*10 из 15/.test(sum), sum);
      assert.strictEqual(await pg.locator('#st-sum a').first().getAttribute('href'), '#token', 'сначала — что требует внимания');
      assert.strictEqual(await pg.locator('label[for="hub_url"] .key').textContent(), 'hub_url');
      assert.strictEqual(await pg.locator('label[for="tls_cert"] .key').textContent(), 'tls_cert, tls_key');
      const hint = await pg.locator('#dirs-hint').innerText();
      assert.ok(/options\.json → report_dirs\[1\]/.test(hint) && /D:\\Выгрузки/.test(hint) && /не просматривает/.test(hint), hint);
      assert.ok(/3 файла, последний за 21\.10\.2026/.test(hint) && /E:\\Старые/.test(hint), hint);
      await pg.locator('#dirs-hint button', { hasText: 'Добавить в папки с отчётами' }).click();
      assert.ok(await pg.locator('#savebar').isVisible());
      assert.ok(!/D:\\Выгрузки/.test(await pg.locator('#dirs-hint').innerText()), 'подсказка ушла: папка в списке');
      assert.ok(/2 папки/.test(await pg.locator('#st-sum').innerText()));
      await pg.locator('#save').click();
      await pg.waitForTimeout(300);
      assert.deepStrictEqual(st.puts[0].inbox_dirs.map(d => d.path), ['C:\\in', 'D:\\Выгрузки']);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('редкое свёрнуто, открытость помнится; блок с ошибкой и ссылки #bf, #sess-card раскрывают своё', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter');
      assert.ok(!(await pg.locator('#adapter_kit_timeout_s').isVisible()) && !(await pg.locator('#inbox_archive').isVisible()));
      assert.ok(!(await pg.locator('#adv-adapter').evaluate(d => d.open)));
      await pg.locator('#adv-adapter > summary').click();
      assert.ok(await pg.locator('#adapter_kit_timeout_s').isVisible());
      // неверное значение в свёрнутом блоке: при сохранении он раскрывается, поле — в фокусе
      await pg.locator('#adv-dirs').evaluate(d => { d.open = false; });
      await pg.locator('#inbox_archive').evaluate(e => { e.value = 'x'; e.dispatchEvent(new Event('input', { bubbles: true })); });
      await pg.goto(ORIGIN + '/settings.html#hub');
      await pg.waitForSelector('#st-sum:not([hidden]) a');
      await pg.locator('#hub_timeout_s').evaluate(e => { e.value = '-5'; e.dispatchEvent(new Event('input', { bubbles: true })); });
      await pg.locator('#save').click();
      assert.ok(/нужно положительное число/.test(await pg.locator('#save-msg').innerText()));
      assert.ok(await pg.locator('#adv-hub').evaluate(d => d.open) && await pg.evaluate(() => document.activeElement.id) === 'hub_timeout_s');
      // открытый вручную блок открыт и после перезагрузки, раскрытый сам — нет
      await pg.locator('#save-undo').click();
      await pg.goto(ORIGIN + '/settings.html#adapter');
      await pg.reload();
      await pg.waitForSelector('#st-sum:not([hidden]) a');
      assert.ok(await pg.locator('#adv-adapter').evaluate(d => d.open), 'помнит браузер');
      assert.ok(!(await pg.locator('#adv-hub').evaluate(d => d.open)), 'раскрытый из-за ошибки не запоминается');
      await pg.goto(ORIGIN + '/settings.html#sess-card');
      await pg.waitForTimeout(200);
      assert.ok(await pg.locator('#sec-users').evaluate(s => s.classList.contains('on')) && await pg.locator('#sess-card').evaluate(d => d.open));
      await pg.goto(ORIGIN + '/settings.html#bf');
      await pg.waitForTimeout(200);
      assert.ok(await pg.locator('#sec-diag').evaluate(s => s.classList.contains('on')) && await pg.locator('#bf').isVisible());
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('определено автоматически: «изменить», «вернуть авто»; нет связи — прокси раскрыт; Python — подстановкой', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter');
      const auto = pg.locator('.auto[data-auto="private_adapter_config"]');
      assert.ok(/Определено автоматически:/.test(await auto.innerText()) && (await auto.innerText()).includes(HOME + '\\adapter-config'));
      await auto.locator('button', { hasText: 'изменить' }).click();
      assert.strictEqual(await pg.locator('#private_adapter_config').inputValue(), HOME + '\\adapter-config');
      assert.ok(/вручную/.test(await auto.innerText()) && await pg.locator('#savebar').isVisible());
      await auto.locator('button', { hasText: 'вернуть авто' }).click();
      assert.strictEqual(await pg.locator('#private_adapter_config').inputValue(), '');
      // у адаптера есть получение условий сделок, а Python не задан: блок раскрыт, найденный подставляется, не выбирается сам
      assert.ok(await pg.locator('#adv-acq').evaluate(d => d.open));
      assert.strictEqual(await pg.locator('#acquisition_python').inputValue(), '');
      assert.ok(/обычно без нужной библиотеки/.test(await pg.locator('#py-found').innerText()));
      await pg.locator('#py-found button').first().click();
      assert.strictEqual(await pg.locator('#acquisition_python').inputValue(), PY);
      // отпечаток: «подставить текущий»
      await pg.locator('#adv-adapter > summary').click();
      await pg.locator('#sha-cur').click();
      assert.strictEqual(await pg.locator('#private_adapter_sha256').inputValue(), 'cd'.repeat(32));
      // прокси и сертификаты: свёрнуто, в заголовке — что найдено; нет связи — раскрывается
      await pg.goto(ORIGIN + '/settings.html#hub');
      await pg.waitForSelector('#st-sum:not([hidden]) a');
      await pg.waitForFunction(() => /proxy\.example/.test(document.getElementById('proxy-sum').textContent));
      assert.ok(!(await pg.locator('#proxy-card').evaluate(d => d.open)));
      assert.ok(/определено автоматически: прокси http:\/\/proxy\.example:3128 \(Windows\)/.test(await pg.locator('#proxy-sum').innerText()));
      await pg.locator('#test-hub').click();
      await pg.waitForFunction(() => document.getElementById('proxy-card').open);
      assert.ok(/Хаб:\s*нет связи/.test(await pg.locator('#st-sum').innerText()));
      assert.ok(/из настроек Windows/.test(await pg.locator('.auto[data-auto="https_proxy"]').innerText()));
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('телефон: без горизонтальной прокрутки', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st, '#adapter', { viewport: { width: 390, height: 800 } });
      const over = await pg.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      assert.ok(over <= 1, 'лишняя ширина ' + over);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally { await browser.close(); }
});
