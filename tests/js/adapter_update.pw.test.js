// «Обновление адаптера» на странице настроек в браузере (Playwright), сервер — заглушка маршрутов (TDM#427):
// состояние и кнопки, токен (сохраняется, на страницу не возвращается), установка с сервера, подтверждение старой
// версии при загрузке архива, предупреждение о заданной папке адаптера.
//   node --test tests/js/adapter_update.pw.test.js
// Нужен Playwright; без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';
const TOKEN = 'github_pat_' + 'Q'.repeat(40);

function state(over) {
  return Object.assign({ repo: 'FlyingScotman/trades-data-adapter', token_set: false, token_source: 'not set', token_file: HOME + '\\github_token',
    source: 'bundle', explicit: false, explicit_note: null, pinned: false, pinned_note: null, in_use: { path: 'C:\\DM\\adapter\\tda', version: '0.4.0', source: 'bundle' },
    installed: null, bundle: { version: '0.4.0', path: 'C:\\DM\\adapter\\tda' }, base_version: '0.4.0', versions: [], previous_available: false,
    checked_at: null, check: null, release: null, no_release: false, compare: null, update_available: false, job: null,
    downloads_dir: 'C:\\Users\\u\\Downloads', downloads_dir_exists: true, browser_url: 'https://github.com/FlyingScotman/trades-data-adapter/releases/latest/download/tda-adapter.zip',
    releases_page: 'https://github.com/FlyingScotman/trades-data-adapter/releases/latest', layers: {}, codes: {}, it_hints: {} }, over || {});
}
const RELEASE = { tag: 'v0.5.0', version: '0.5.0', published_at: '2026-10-29T08:00:00Z', notes: '',
  asset: { name: 'tda-adapter.zip', size: 120000, sha256: 'a'.repeat(64), id: 11, browser_url: 'https://github.com/x/y/releases/download/v0.5.0/tda-adapter.zip' } };

function server() {
  const st = { calls: [], st: state() };
  st.handle = (u, method, body) => {
    const p = u.pathname;
    st.calls.push([method, p, body]);
    if (p === '/api/v1/auth/state') return [200, { enabled: false, user: null }];
    if (p === '/api/v1/settings' && method === 'GET') return [200, { home: HOME, inbox_dirs: [], hub_token_set: true, proxy_password_set: false, github_token_set: st.st.token_set,
      effective: { home: HOME, adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: true, private_adapter_source: 'bundle', private_adapter: 'C:\\DM\\adapter\\tda' }, env_overrides: [], error: null }];
    if (p === '/api/v1/settings/github-token') { st.st.token_set = !!body.token; st.st.token_source = body.token ? 'file' : 'not set'; return [200, { github_token_set: st.st.token_set, github_token_source: st.st.token_source }]; }
    if (p === '/api/v1/update/adapter/check') {
      st.st.check = { ok: true, at: '2026-10-29T10:00:00', layer: 'curl', attempts: [] };
      st.st.release = RELEASE; st.st.compare = 'newer'; st.st.update_available = true;
      return [200, st.st];
    }
    if (p === '/api/v1/update/adapter/install') {
      st.st.job = { state: 'downloading', via: 'server', layer: 'curl', bytes: 1000, total: 120000, attempts: [], message: 'Скачиваю tda-adapter.zip 0.5.0' };
      st.installs = (st.installs || 0) + 1;
      return [202, { job: st.st.job }];
    }
    if (p === '/api/v1/update/adapter') {
      if (st.st.job && st.st.job.state === 'downloading' && ++st.polls > 1) {
        st.st.job = { state: 'done', message: 'Адаптер 0.5.0 установлен и проверен загрузчиком; работает без перезапуска.' };
        st.st.installed = { version: '0.5.0', previous: null, installed_at: '2026-10-29T10:01:00', source: 'server', exists: true };
        st.st.source = 'installed'; st.st.in_use = { version: '0.5.0', source: 'installed' }; st.st.compare = 'same'; st.st.update_available = false;
      }
      return [200, st.st];
    }
    if (p === '/api/v1/update/adapter/upload') {
      if (u.searchParams.get('allow_older') !== '1') return [409, { code: 'invalid_parameter', message: 'Адаптер 0.3.0: эта версия старее используемой (0.4.0). Установить можно только явно.', details: { needs_confirm: true, version: '0.3.0', compare: 'older' } }];
      st.olderInstalled = true;
      return [200, { installed: true, version: '0.3.0', message: 'Адаптер 0.3.0 установлен и проверен загрузчиком; работает без перезапуска.', state: st.st }];
    }
    return null;
  };
  st.polls = 0;
  return st;
}

async function open(browser, st) {
  const ctx = await browser.newContext(), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
  await pg.route('**/*', route => {
    const req = route.request(), u = new URL(req.url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      let body = null; try { body = req.postDataJSON(); } catch (e) { /* GET or a zip */ }
      const out = st.handle(u, req.method(), body);
      return out ? route.fulfill({ status: out[0], contentType: 'application/json', body: JSON.stringify(out[1]) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  pg.on('dialog', d => d.accept());
  await pg.goto(ORIGIN + '/settings.html#adapter');
  await pg.waitForSelector('#ad-upd button[data-act=check]');
  return { pg, ctx, errors };
}

test('настройки: обновление адаптера (TDM#427)', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('токен, проверка, установка с сервера', async () => {
      const st = server(), { pg, ctx, errors } = await open(browser, st);
      const box = pg.locator('#ad-upd');
      let text = await box.innerText();
      assert.ok(/Используется: версия 0\.4\.0, из комплекта программы/.test(text) && /Токен GitHub: не задан/.test(text), text);
      assert.ok(/Fine-grained tokens/.test(text) && /Contents: Read-only/.test(text) && /trades-data-adapter/.test(text), 'подсказка, как получить токен');
      assert.ok(await box.locator('button[data-act=check]').isDisabled(), 'без токена сервер не проверяет');
      await box.locator('input[type=password]').fill(TOKEN);
      await box.locator('button', { hasText: 'Сохранить токен' }).click();
      await box.locator('button[data-act=install]').waitFor();
      assert.deepStrictEqual(st.calls.find(c => c[1] === '/api/v1/settings/github-token')[2], { token: TOKEN });
      assert.strictEqual(await box.locator('input[type=password]').inputValue(), '', 'поле токена очищено');
      text = await box.innerText();
      assert.ok(/последний выпуск 0\.5\.0/.test(text) && /новее/.test(text) && /Токен GitHub: задан/.test(text) && !text.includes(TOKEN), text);
      await box.locator('button[data-act=install]').click();
      await box.locator('text=Адаптер 0.5.0 установлен').waitFor({ timeout: 10000 });
      assert.strictEqual(st.installs, 1);
      text = await box.innerText();
      assert.ok(/Используется: версия 0\.5\.0, установлен обновлением/.test(text) && /Использовать адаптер из комплекта/.test(text), text);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
    await t.test('старая версия из архива — только с подтверждением; заданная папка адаптера — предупреждение', async () => {
      const st = server();
      st.st = state({ explicit: true, explicit_note: 'В настройках задана папка адаптера (private_adapter): установленный выпуск не используется, пока поле не очищено.', source: 'settings' });
      const { pg, ctx, errors } = await open(browser, st);
      const box = pg.locator('#ad-upd');
      assert.ok(/установленный выпуск не используется/.test(await box.innerText()));
      await box.locator('input[type=file]').setInputFiles({ name: 'tda-adapter.zip', mimeType: 'application/zip', buffer: Buffer.from('PK') });
      await box.locator('text=Адаптер 0.3.0 установлен').waitFor({ timeout: 10000 });
      assert.ok(st.olderInstalled, 'после подтверждения — с allow_older=1');
      assert.strictEqual(st.calls.filter(c => c[1] === '/api/v1/update/adapter/upload').length, 2);
      assert.deepStrictEqual(errors, []);
      await ctx.close();
    });
  } finally {
    await browser.close();
  }
});
