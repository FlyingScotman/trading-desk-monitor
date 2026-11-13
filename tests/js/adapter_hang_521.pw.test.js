// TDM#521: адаптер не отвечает — монитор не ждёт его на каждом цикле, а пользователь видит причину:
//  - настройки → «Адаптер и источники» → «Обзор»: плашка со строками сервера (GET /api/v1/adapter → hang.lines), подсказка
//    со словами экрана («Чтение одного отчёта», «Для опытных», «Дополнительно» — они есть на этой же странице), пункт
//    «Адаптер» сводки становится «не отвечает»; без пауз плашки нет;
//  - C14 «Статус дня»: панель «Адаптер не отвечает» со строками сервера (GET /api/v1/inbox → adapter_hang.lines).
//   node --test tests/js/adapter_hang_521.pw.test.js
// Нужен Playwright; без него тест пропускается. Данные синтетические.
'use strict';
const test = require('node:test');
const assert = require('assert');
const fs = require('fs'), path = require('path');

const pw = (() => { try { return require('/opt/node22/lib/node_modules/playwright'); } catch (e) { try { return require('playwright'); } catch (e2) { return null; } } })();
const ROOT = path.join(__dirname, '..', '..', 'prototypes');
const ORIGIN = 'http://desk.test';
const TYPES = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };
const HOME = 'C:\\Users\\u\\AppData\\Local\\DeskMonitor';
const LINES = ['Справочник адаптера не отвечает с 02.11 14:05. Монитор не спрашивает его до 02.11 15:05 и пока берёт сохранённые книги.',
  'Чтение источника «positions_daily» не укладывается в таймаут с 02.11 14:05: его файлы ждут в очереди (4947), следующая попытка после 02.11 15:05.'];

const adapter = hang => ({ loaded: true, path: 'C:\\desk\\adapter', reference: false, default: true, name: 'synthetic', version: '0.9.0',
  fingerprint: 'ab'.repeat(32), config_dir: HOME + '\\adapter-config', describe: { contract: 'private/1.6', sources: [{ source: 'positions_daily', dataset: 'positions' }] },
  config_files: [], settings_tabs: [], optional: [], actions: [], warnings: [], error: null, hang });

function handler(hang) {
  return u => {
    const p = u.pathname;
    if (p === '/api/v1/auth/state') return { enabled: false, user: null };
    if (p === '/api/v1/settings') return { home: HOME, hub_url: 'https://hub.example', inbox_dirs: [{ path: 'C:\\in', label: 'Позиции', recursive: false }], hub_token_set: true,
      proxy_password_set: false, env_overrides: [], error: null, autopilot: true,
      effective: { home: HOME, hub_url: 'https://hub.example', adapter_output_dir: HOME + '\\adapter-output', private_adapter_default: true, autopilot: true } };
    if (p === '/api/v1/adapter') return adapter(hang);
    if (p === '/api/v1/autopilot/status') return { enabled: true, state: 'idle', steps: [{ name: 'sync', status: 'ok', detail: 'срезов: 1' }] };
    if (p === '/api/v1/backfill/status') return { enabled: true, dates: 10, done: 10, todo: 0, no_positions: 0, no_market: 0 };
    if (p === '/api/v1/update/adapter') return null;
    return null;
  };
}

async function settings(browser, hang) {
  const ctx = await browser.newContext(), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  const answer = handler(hang);
  await pg.route('**/*', route => {
    const u = new URL(route.request().url());
    if (u.origin !== ORIGIN) return route.abort();
    if (u.pathname.startsWith('/api/') || u.pathname.startsWith('/health')) {
      const out = answer(u);
      return out ? route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(out) })
        : route.fulfill({ status: 404, contentType: 'application/json', body: '{"message":"нет в тесте"}' });
    }
    if (['/settings-ocp-types.js', '/desk-replay.js'].includes(u.pathname)) return route.fulfill({ status: 200, contentType: 'application/javascript', body: '' });
    const f = path.join(ROOT, decodeURIComponent(u.pathname));
    if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return route.fulfill({ status: 404, body: '' });
    return route.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
  });
  await pg.goto(ORIGIN + '/settings.html#adapter/overview');
  await pg.waitForSelector('#st-sum:not([hidden]) a');
  await pg.waitForFunction(() => document.getElementById('ad-st').textContent !== 'проверка…');
  return { pg, ctx, errors };
}

test('настройки, «Обзор» адаптера: плашка «не отвечает» со строками сервера и словами экрана; без пауз её нет', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  try {
    await t.test('есть паузы: строки сервера, подсказка, пункт сводки, точка у «Обзора»', async () => {
      const { pg, errors } = await settings(browser, { reference: {}, sources: [], lines: LINES });
      const box = pg.locator('#ad-hang');
      assert.ok(await box.isVisible(), 'плашка видна в «Обзоре»');
      const text = await box.innerText();
      for (const l of LINES) assert.ok(text.includes(l), l);
      // слова подсказки есть на этой же странице: поле, подраздел, раскрывающийся блок
      for (const w of ['Чтение одного отчёта', 'Для опытных', 'Дополнительно']) assert.ok(text.includes('«' + w + '»'), w);
      assert.ok((await pg.locator('label[for="adapter_read_timeout_s"]').innerText()).startsWith('Чтение одного отчёта'));
      assert.ok((await pg.locator('.subnav a[data-sub-link="expert"]').innerText()).startsWith('Для опытных'));
      assert.ok((await pg.locator('#adv-adapter > summary').innerText()).startsWith('Дополнительно'));
      const sum = await pg.locator('#st-sum a', { hasText: 'Адаптер' }).first().innerText();
      assert.ok(/не отвечает/.test(sum), 'в сводке: ' + sum);
      assert.strictEqual(await pg.locator('.subnav a[data-sub-link="overview"] .dot').evaluate(d => d.className.includes('warn') || d.dataset.base === 'warn'), true);
      assert.deepStrictEqual(errors, []);
    });
    await t.test('пауз нет: плашки нет, сводка как была', async () => {
      const { pg, errors } = await settings(browser, { reference: null, sources: [], lines: [] });
      assert.ok(!(await pg.locator('#ad-hang').isVisible()));
      const ok = pg.locator('#st-sum a.ok', { hasText: 'Адаптер' });  // в порядке — одним словом, подробность в подсказке
      assert.strictEqual(await ok.count(), 1);
      assert.ok(/загружен/.test(await ok.getAttribute('title')) && !/не отвечает/.test(await ok.getAttribute('title')));
      assert.deepStrictEqual(errors, []);
    });
    await t.test('сервер без поля hang (старая версия): как без пауз', async () => {
      const { pg, errors } = await settings(browser, undefined);
      assert.ok(!(await pg.locator('#ad-hang').isVisible()));
      assert.deepStrictEqual(errors, []);
    });
  } finally { await browser.close(); }
});

test('C14 «Статус дня»: панель «Адаптер не отвечает» со строками сервера', { skip: pw ? false : 'нет Playwright' }, async t => {
  const browser = await pw.chromium.launch();
  const page = fs.readFileSync(path.join(ROOT, 'concepts', 'c14-day-status.html'), 'utf8');
  const MARK = "adapter: 'ok demo_adapter 0.1.0',";
  assert.ok(page.includes(MARK), 'демо-набор C14 изменился: поправьте метку теста');
  const open = async lines => {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } }), pg = await ctx.newPage(), errors = [];
    pg.on('pageerror', e => errors.push(e.message));
    await pg.route('**/*', r => {
      const u = new URL(r.request().url());
      if (u.origin !== ORIGIN) return r.abort();
      if (u.pathname === '/api/v1/auth/state') return r.fulfill({ status: 200, contentType: 'application/json', body: '{"enabled":false,"user":null}' });
      if (u.pathname.startsWith('/api/')) return r.fulfill({ status: 404, contentType: 'application/json', body: '{}' });
      if (u.pathname === '/concepts/c14-day-status.html') {  // демо-набор + то, что сервер добавляет в ответ дня
        const body = lines === undefined ? page : page.replace(MARK, MARK + ' adapter_hang: { lines: ' + JSON.stringify(lines) + ' },');
        return r.fulfill({ status: 200, contentType: 'text/html', body });
      }
      const f = path.join(ROOT, decodeURIComponent(u.pathname));
      if (!f.startsWith(ROOT) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) return r.fulfill({ status: 404, body: '' });
      return r.fulfill({ status: 200, contentType: TYPES[path.extname(f)] || 'application/octet-stream', body: fs.readFileSync(f) });
    });
    await pg.goto(ORIGIN + '/concepts/c14-day-status.html?demo=1');
    await pg.waitForSelector('table[data-vk="c14-runs"] tbody tr');
    return { pg, errors };
  };
  try {
    await t.test('с паузами: панель после заголовка даты, строки сервера, ссылка на «Обзор»', async () => {
      const { pg, errors } = await open(LINES);
      const panel = pg.locator('.k-panel', { has: pg.locator('h3', { hasText: 'Адаптер не отвечает' }) });
      assert.strictEqual(await panel.count(), 1);
      const text = await panel.innerText();
      for (const l of LINES) assert.ok(text.includes(l), l);
      assert.ok(text.includes('Настройки → Адаптер и источники → Обзор'));
      assert.ok((await panel.locator('a').getAttribute('href')).endsWith('settings.html#adapter/overview'));
      const order = await pg.evaluate(() => [...document.querySelectorAll('#view .k-panel > .hd h3')].map(h => h.textContent).slice(0, 3));
      assert.deepStrictEqual(order, ['Дата', 'Адаптер не отвечает', 'Что пришло']);
      assert.deepStrictEqual(errors, []);
    });
    await t.test('без пауз или без поля: панели нет', async () => {
      for (const lines of [[], undefined]) {
        const { pg, errors } = await open(lines);
        assert.strictEqual(await pg.locator('h3', { hasText: 'Адаптер не отвечает' }).count(), 0);
        assert.deepStrictEqual(errors, []);
      }
    });
  } finally { await browser.close(); }
});
