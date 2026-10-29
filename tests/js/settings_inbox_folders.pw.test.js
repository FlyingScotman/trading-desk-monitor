// Страница настроек в браузере (Playwright), сервер — заглушка маршрутов: под каждой папкой карточки «Папки с отчётами»
// — что найдено в ней по последним просмотрам (TDM#445, settings-inbox-folders.js, GET /api/v1/inbox/folders); новая
// несохранённая папка — «ещё не просматривал»; после сохранения сводка запрашивается снова.
//   node --test tests/js/settings_inbox_folders.pw.test.js
// Нужен Playwright (как settings_adapter.pw.test.js); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';
const FOLDERS = { adapter: 'ok synthetic 0.9.0 abababababab', status_text: { accepted: 'принят', rejected: 'отклонён' }, lookback_days: 35, last_scan: { at: '2026-10-28T10:00:00+00:00', files: {} },
  folders: [
    { dir: 'C:\\in', path: 'C:\\in', recursive: false, output: false, exists: true, files: 7, matched: 4, ignored: 3, too_old: 0, lost: 1, by_status: { accepted: 3, rejected: 1 },
      sources: [{ source: 'pos_csv', datasets: ['positions'], files: 2, by_status: { accepted: 2 } }, { source: 'ocp_src', datasets: ['ocp'], files: 2, by_status: { accepted: 1, rejected: 1 } }],
      datasets: [{ dataset: 'ocp', name: 'ОВП', latest_valid_date: '2026-10-27', files: 1 }, { dataset: 'positions', name: 'Позиции', latest_valid_date: '2026-10-28', files: 2 }],
      last_seen: '2026-10-28T09:00:00+00:00' },
    { dir: 'D:\\ovp\\**', path: 'D:\\ovp', recursive: true, output: false, exists: false, files: 0, matched: 0, ignored: 0, too_old: 0, lost: 0, by_status: {}, sources: [], datasets: [], last_seen: null },
    { dir: HOME + '\\adapter-output\\**', path: HOME + '\\adapter-output', recursive: true, output: true, exists: true, files: 0, matched: 0, ignored: 0, too_old: 0, lost: 0, by_status: {}, sources: [], datasets: [], last_seen: null }] };

function server() {
  const st = { puts: [], folderGets: 0 };
  st.settings = { home: HOME, hub_url: 'https://hub.example', hub_token_set: true, proxy_password_set: false,
    inbox_dirs: [{ path: 'C:\\in', label: 'Позиции', recursive: false }, { path: 'd:\\OVP\\', label: 'ОВП', recursive: true }],
    effective: { home: HOME, adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: false }, env_overrides: [], error: null };
  st.handle = (u, method, body) => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings' && method === 'GET') return st.settings;
    if (p === '/api/v1/settings' && method === 'PUT') { st.puts.push(body); Object.assign(st.settings, body); return st.settings; }
    if (p === '/api/v1/inbox/folders') { st.folderGets++; return FOLDERS; }
    return null;
  };
  return st;
}

async function open(browser, st, shot) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } }), pg = await ctx.newPage();
  const errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push('console: ' + m.text()); });
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
  await pg.goto(ORIGIN + '/settings.html#adapter');
  await pg.locator('#dirs .dir-found >> text=найдено файлов').waitFor();
  return { pg, ctx, errors };
}

test('настройки: что найдено в папках с отчётами (TDM#445)', { skip: pw ? false : 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const st = server(), { pg, ctx, errors } = await open(browser, st);
    const found = pg.locator('#dirs .dir-found');
    assert.strictEqual(await found.count(), 2);
    const a = await found.nth(0).innerText(), b = await found.nth(1).innerText();
    assert.ok(/найдено файлов: 7 · адаптер узнал: 4 · не узнал: 3 · отклонено или с ошибкой: 1/.test(a), a);
    assert.ok(/ocp_src \(ocp\): принят 1, отклонён 1/.test(a) && /последний прочитанный отчёт: ОВП — 27\.10\.2026, Позиции — 28\.10\.2026/.test(a), a);
    assert.ok(/Сервер не видит эту папку/.test(b), 'папка другим регистром и с «\\» в конце — та же: ' + b);
    if (process.env.SHOTS) await pg.locator('#dirs').locator('xpath=ancestor::div[contains(@class,"card")]').screenshot({ path: path.join(process.env.SHOTS, 'settings-dirs.png') });
    // новая папка — «ещё не просматривал»; после сохранения сводка запрашивается снова
    await pg.locator('#dir-add').click();
    await pg.locator('#dirs input.inp.mono').nth(2).fill('E:\\new');
    assert.strictEqual(await found.count(), 3);
    assert.strictEqual(await found.nth(2).innerText(), '', 'пустая новая строка — без сводки');
    const before = st.folderGets;
    await pg.locator('#save').click();
    await pg.locator('#dirs .dir-found >> nth=2 >> text=Сервер ещё не просматривал эту папку').waitFor();
    assert.ok(st.folderGets > before && st.puts.length === 1);
    assert.strictEqual(st.puts[0].inbox_dirs.length, 3, 'папки сохраняются как раньше');
    // мобильная ширина: сводки папок не шире экрана (строка навигации страницы шире и без них — её перестраивает #449)
    await pg.setViewportSize({ width: 390, height: 800 });
    assert.ok(await pg.evaluate(() => [...document.querySelectorAll('#dirs .dir-found')].every(e => e.getBoundingClientRect().right <= window.innerWidth + 1)));
    if (process.env.SHOTS) await pg.locator('#dirs').locator('xpath=ancestor::div[contains(@class,"card")]').screenshot({ path: path.join(process.env.SHOTS, 'settings-dirs-390.png') });
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
