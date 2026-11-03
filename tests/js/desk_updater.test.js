// Проверка версии из браузера (prototypes/desk-updater.js): запрос к GitHub API «простой» (без preflight и cookie),
// ответы 404 / лимит / сетевая ошибка разбираются, повтор не чаще раза в 6 часов, строка «GitHub проверен …».
//   node tests/js/desk_updater.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', 'desk-updater.js'), 'utf8');

// минимальный DOM: элемент помнит класс, стиль, текст и дочерние узлы
function FakeNode() {}
function fakeEl(tag) {
  const e = Object.create(FakeNode.prototype);
  e.tag = tag; e.attrs = {}; e.kids = [];
  e.setAttribute = (k, v) => { e.attrs[k] = v; };
  e.append = (...c) => { e.kids.push(...c); };
  e.addEventListener = () => {};
  return e;
}
const textOf = (e) => (typeof e === 'string' ? e : e.kids.map(textOf).join(''));

function load() {
  const ctx = {
    window: {}, Node: FakeNode, performance: { now: () => 0 }, setTimeout, clearTimeout, console,
    location: { protocol: 'file:', pathname: '/settings.html' },
    localStorage: { getItem: () => null, setItem: () => {} },
    document: { createElement: fakeEl, currentScript: { dataset: { mode: 'nav' } }, readyState: 'complete', querySelector: () => null,
                getElementById: () => null, addEventListener: () => {} },
    fetch: () => { throw new Error('no network in tests'); }
  };
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  return ctx.window.DESK_UPDATER._test;
}
const T = load();
const SAFE = new Set(['accept', 'accept-language', 'content-language', 'content-type']);
let n = 0;
const t = async (name, f) => { await f(); n++; console.log('ok', name); };
const resp = (status, json, headers) => ({ status, ok: status >= 200 && status < 300, json: async () => json,
  headers: { get: k => (headers || {})[k.toLowerCase()] || null } });

(async () => {
  await t('простой CORS-запрос: GET, только Accept, без cookie', async () => {
    let seen;
    await T.ghFetch('https://api.github.com/repos/o/r/releases/latest', (url, opt) => { seen = { url, opt }; return Promise.resolve(resp(200, {})); });
    assert.strictEqual(seen.opt.method, 'GET');
    assert.strictEqual(seen.opt.credentials, 'omit');
    assert.strictEqual(seen.opt.mode, 'cors');
    assert.ok(Object.keys(seen.opt.headers).every(h => SAFE.has(h.toLowerCase())), 'только CORS-safelisted заголовки');
    assert.ok(!('cache' in seen.opt), 'без cache: no-store (лишние заголовки)');
  });
  await t('список релизов, их отсутствие, лимит, сеть', async () => {
    const rels = [{ tag_name: '0.2.2', assets: [] }, { tag_name: 'v1', assets: [] }];
    let asked;
    assert.deepStrictEqual(JSON.parse(JSON.stringify(await T.browserCheck('o/r', (url) => { asked = url; return Promise.resolve(resp(200, rels)); }))), { releases: rels });
    assert.strictEqual(asked, 'https://api.github.com/repos/o/r/releases?per_page=10', 'список: сервер возьмёт последний с файлами');
    assert.deepStrictEqual(JSON.parse(JSON.stringify(await T.browserCheck('o/r', () => Promise.resolve(resp(200, []))))), { none: true });
    assert.deepStrictEqual(JSON.parse(JSON.stringify(await T.browserCheck('o/r', () => Promise.resolve(resp(404, {}))))), { none: true });
    const lim = await T.browserCheck('o/r', () => Promise.resolve(resp(403, {}, { 'x-ratelimit-remaining': '0' })));
    assert.ok(/лимит/.test(lim.error));
    const net = await T.browserCheck('o/r', () => Promise.reject(new TypeError('Failed to fetch')));
    assert.ok(/недоступен из браузера/.test(net.error) && /Failed to fetch/.test(net.error));
  });
  await t('повтор не чаще раза в 6 часов', async () => {
    assert.ok(T.due(0, Date.now()));
    assert.ok(!T.due(Date.now() - 3600e3, Date.now()));
    assert.ok(T.due(Date.now() - 7 * 3600e3, Date.now()));
  });
  await t('когда и с каким итогом проверялся GitHub', async () => {
    assert.strictEqual(T.lastCheck({}), 'ещё не проверялось');
    const ok = T.lastCheck({ checked_at: '2026-10-23T10:00:00', check: { ok: true, layer: 'curl' }, release: { tag: 'v0.2.4' } });
    assert.ok(/^GitHub проверен .+ \(сервер, curl\.exe.*\): найден v0\.2\.4$/.test(ok), ok);
    const bad = T.lastCheck({ checked_at: '2026-10-23T10:00:00', check: { ok: false, layer: 'python', error: { code: 'release' } } });
    assert.ok(/: релиз не подходит$/.test(bad), bad);
    const net = T.lastCheck({ checked_at: '2026-10-23T10:00:00', check: { ok: false, layer: null, error: { code: 'dns' } } });
    assert.ok(/\(сервер\): не удалось$/.test(net), net);
    const br = T.lastCheck({ checked_at: '2026-10-23T10:00:00', check: { ok: false, error: { code: 'dns' } },
                             browser: { checked_at: '2026-10-23T11:00:00', ok: true }, no_release: true });
    assert.ok(/\(браузер\): релизов нет$/.test(br), br);
  });
  await t('ручной переход на вид «без пакетов»: заметка с шагами и ссылкой, а не ошибка', async () => {
    assert.strictEqual(T.manualNote(null).length, 0);
    assert.strictEqual(T.manualNote({ manual: false, reason: 'Установлена последняя версия.' }).length, 0);
    const reason = 'С версии 0.2.15 программа выходит только в виде «без пакетов».\n1. Остановите программу: stop.bat.';
    const notes = T.manualNote({ manual: true, reason, manual_url: 'https://github.com/o/r/releases/download/v0.2.15/DeskMonitorMirror-0.2.15.zip' });
    assert.strictEqual(notes.length, 2);
    assert.ok(/note warn/.test(notes[0].attrs['class']) && !/err/.test(notes[0].attrs['class']), 'не красная ошибка');
    assert.ok(/pre-line/.test(notes[0].attrs.style), 'шаги в столбик');
    assert.strictEqual(textOf(notes[0]), reason);
    assert.strictEqual(notes[1].kids[0].attrs.href, 'https://github.com/o/r/releases/download/v0.2.15/DeskMonitorMirror-0.2.15.zip');
    assert.strictEqual(T.manualNote({ manual: true, reason }).length, 1, 'без ссылки — только текст');
  });
  console.log(n + ' tests ok');
})().catch(e => { console.error(e); process.exit(1); });
