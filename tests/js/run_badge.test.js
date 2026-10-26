// #192: бейдж прогона в шапке мониторов (prototypes/concepts/lib/kit.js) — сколько прошло с публикации прогона
// (K.runAgo, K.runBadge: подсветка «устарел» и «есть новее»), что вошло в прогон (K.runCoverage), и событие окна
// 'desk:live' из lib/live.js, по которому бейдж узнаёт о новом прогоне без перезагрузки.
//   node --test tests/js/run_badge.test.js
'use strict';
const test = require('node:test');
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const P = f => fs.readFileSync(path.join(__dirname, '..', '..', 'prototypes', f), 'utf8');

function el() {
  return { style: {}, dataset: {}, children: [], innerHTML: '', isConnected: true,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(c) { this.children.push(c); return c; }, append(...c) { this.children.push(...c); },
    querySelector() { return null; }, querySelectorAll() { return []; }, addEventListener() {}, setAttribute() {}, remove() {} };
}
function loadKit(withRun, store) {  // store — содержимое localStorage браузера: { ключ: строка }
  const document = { addEventListener() {}, createElement: el, documentElement: el(), body: el(), querySelector() { return null; }, querySelectorAll() { return []; }, getElementById: () => el() };
  const ctx = { window: {}, document, URLSearchParams, console, location: { href: 'http://x/', search: '', hostname: '127.0.0.1', protocol: 'file:' }, setTimeout,
    localStorage: { getItem: k => store && k in store ? store[k] : null, setItem(k, v) { if (store) store[k] = v; }, removeItem(k) { if (store) delete store[k]; } },
    getComputedStyle: () => ({ getPropertyValue: () => '#888' }), MutationObserver: class { observe() {} } };
  ctx.window = ctx;
  vm.createContext(ctx);
  if (withRun) {  // прогон из базы: демо-прогон prototypes/data.js через lib/real.js
    vm.runInContext(P('data.js'), ctx);
    withRun(ctx.window.DESK);
    vm.runInContext(P('concepts/lib/real.js'), ctx);
  } else vm.runInContext(P('concepts/lib/mock.js'), ctx);
  vm.runInContext(P('concepts/lib/kit.js'), ctx);
  return { K: ctx.window.KIT, M: ctx.window.MOCK };
}
const { K } = loadKit();
const msk = s => new Date(s + '+03:00');
const MIN = 60e3;

test('возраст: минуты, часы с минутами, часы, дни', () => {
  assert.strictEqual(K.runAgo(20e3), 'только что');
  assert.strictEqual(K.runAgo(-5e3), 'только что');  // часы браузера чуть отстают от сервера
  assert.strictEqual(K.runAgo(12 * MIN), '12 мин');
  assert.strictEqual(K.runAgo(60 * MIN), '1 ч');
  assert.strictEqual(K.runAgo(135 * MIN), '2 ч 15 мин');
  assert.strictEqual(K.runAgo(9 * 60 * MIN), '9 ч');
  assert.strictEqual(K.runAgo(3 * 24 * 60 * MIN + 5), '3 дн.');
});

test('бейдж: возраст тикает от момента now; без времени публикации — «—»', () => {
  const pub = '2026-10-23T11:00:00+03:00';
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:00:30')).short, 'только что');
  const b = K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:12:00'));
  assert.strictEqual(b.short, '12 мин назад');
  assert.strictEqual(b.text, 'Прогон опубликован 12 мин назад');
  assert.strictEqual(b.level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:40:00')).short, '40 мин назад');
  const u = K.runBadge({ publishedAt: null }, msk('2026-10-23T11:12:00'));
  assert.strictEqual(u.level, 'unknown');
  assert.strictEqual(u.short, '—');
  assert.strictEqual(u.text, 'Время публикации прогона неизвестно');
});

test('устарел: в рабочие часы старше fresh_min, в любое время старше max_days; ночью и в выходные — нет', () => {
  assert.deepStrictEqual(JSON.parse(JSON.stringify(K.runAge())), { fresh_min: 90, fresh_from: '10:15', fresh_to: '20:00', max_days: 3 });
  const pub = '2026-10-23T10:30:00+03:00';  // пятница
  assert.strictEqual(K.runBadge({ publishedAt: pub }, msk('2026-10-23T11:59:00')).level, 'ok');
  const s = K.runBadge({ publishedAt: pub }, msk('2026-10-23T12:01:00'));
  assert.strictEqual(s.level, 'stale');
  assert.match(s.why, /в рабочие часы прогон старше 90 мин/);
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T19:00:00+03:00' }, msk('2026-10-23T23:30:00')).level, 'ok', 'вечером не подсвечиваем');
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T19:00:00+03:00' }, msk('2026-10-25T14:00:00')).level, 'ok', 'воскресенье');
  const old = K.runBadge({ publishedAt: '2026-10-19T19:00:00+03:00' }, msk('2026-10-25T14:00:00'));
  assert.strictEqual(old.level, 'stale');
  assert.match(old.why, /старше 3 дн/);
});

test('утро (#255): прогон до 10:15 — отсчёт от 10:15, «⚠» с 11:45, если нового прогона нет', () => {
  const yest = '2026-10-22T19:00:00+03:00';  // четверг вечером; сейчас пятница
  assert.strictEqual(K.runBadge({ publishedAt: yest }, msk('2026-10-23T10:00:00')).level, 'ok', 'до рабочего окна');
  assert.strictEqual(K.runBadge({ publishedAt: yest }, msk('2026-10-23T10:16:00')).level, 'ok', 'в 10:15 — не сразу');
  assert.strictEqual(K.runBadge({ publishedAt: yest }, msk('2026-10-23T11:44:00')).level, 'ok');
  const s = K.runBadge({ publishedAt: yest }, msk('2026-10-23T11:46:00'));
  assert.strictEqual(s.level, 'stale');
  assert.strictEqual(s.why, 'с 10:15 МСК прошло больше 90 мин, а прогона после 10:15 нет');
  // прогон сегодня до 10:15 — так же: отсчёт от 10:15
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T09:00:00+03:00' }, msk('2026-10-23T11:44:00')).level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T09:00:00+03:00' }, msk('2026-10-23T11:46:00')).level, 'stale');
  // прогон в 10:20 — отсчёт от публикации: «⚠» с 11:50
  assert.strictEqual(K.runBadge({ publishedAt: '2026-10-23T10:20:00+03:00' }, msk('2026-10-23T11:49:00')).level, 'ok');
  assert.match(K.runBadge({ publishedAt: '2026-10-23T10:20:00+03:00' }, msk('2026-10-23T11:51:00')).why, /в рабочие часы прогон старше 90 мин/);
});

test('настройка порогов (#255): меню вида, 0 выключает правило, неверное значение не сохраняется', () => {
  const yest = '2026-10-22T19:00:00+03:00', fri = t => msk('2026-10-23T' + t);
  try {
    assert.strictEqual(K.setRunAge({ fresh_min: '30' }), true);  // строка поля ввода
    assert.strictEqual(K.runAge().fresh_min, 30);
    assert.strictEqual(K.runAge().max_days, 3, 'второй порог не тронут');
    assert.strictEqual(K.runBadge({ publishedAt: yest }, fri('10:44:00')).level, 'ok');
    assert.strictEqual(K.runBadge({ publishedAt: yest }, fri('10:46:00')).level, 'stale');
    assert.match(K.runBadge({ publishedAt: yest }, fri('10:46:00')).why, /больше 30 мин/);
    assert.strictEqual(K.setRunAge({ fresh_min: 0 }), true);
    assert.strictEqual(K.runBadge({ publishedAt: yest }, fri('19:59:00')).level, 'ok', 'правило минут выключено');
    const old = '2026-10-19T19:00:00+03:00', sun = msk('2026-10-25T14:00:00');
    assert.match(K.runBadge({ publishedAt: old }, sun).why, /старше 3 дн/);
    assert.strictEqual(K.setRunAge({ max_days: 7 }), true);
    assert.strictEqual(K.runBadge({ publishedAt: old }, sun).level, 'ok', '6 дней < 7');
    assert.strictEqual(K.setRunAge({ max_days: 0 }), true);
    assert.strictEqual(K.runBadge({ publishedAt: '2026-01-01T10:00:00+03:00' }, sun).level, 'ok', 'правило дней выключено');
    ['', ' ', '-5', '1.5', 'abc', 99999, null].forEach(v => assert.strictEqual(K.setRunAge({ fresh_min: v }), false, JSON.stringify(v)));
    assert.strictEqual(K.setRunAge({ max_days: 400 }), false);
    assert.deepStrictEqual([K.runAge().fresh_min, K.runAge().max_days], [0, 0], 'после неверного ввода — прежние');
  } finally { K.viewStore.reset('ui:runage'); }
  assert.deepStrictEqual([K.runAge().fresh_min, K.runAge().max_days], [90, 3]);
});

test('настройка порогов хранится в localStorage (tdm.view.v1:ui:runage); испорченное значение — умолчание', () => {
  const store = {};
  const { K: K1 } = loadKit(null, store);
  K1.setRunAge({ fresh_min: 45, max_days: 5 });
  assert.deepStrictEqual(JSON.parse(store['tdm.view.v1:ui:runage']), { fresh_min: 45, max_days: 5 });
  const { K: K2 } = loadKit(null, store);  // новая страница — тот же браузер
  assert.deepStrictEqual([K2.runAge().fresh_min, K2.runAge().max_days], [45, 5]);
  const { K: K3 } = loadKit(null, { 'tdm.view.v1:ui:runage': '{"fresh_min":-1,"max_days":"x"}' });
  assert.deepStrictEqual([K3.runAge().fresh_min, K3.runAge().max_days], [90, 3]);
  const { K: K4 } = loadKit(null, { 'tdm.view.v1:ui:runage': 'не JSON' });
  assert.strictEqual(K4.runAge().fresh_min, 90);
});

test('праздник: автопилот назвал день нерабочим (not_business_day шага intraday) — не подсвечиваем', () => {
  const pub = '2026-11-04T09:00:00+03:00', now = msk('2026-11-04T12:00:00');  // среда, праздник
  assert.strictEqual(K.runBadge({ publishedAt: pub }, now).level, 'stale');
  assert.strictEqual(K.runBadge({ publishedAt: pub, reason: 'not_business_day' }, now).level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: pub, reason: 'no_cut_yet' }, now).level, 'stale');
});

test('закреплённый прогон и демо устаревшими не считаются', () => {
  const pub = '2026-10-19T10:00:00+03:00', now = msk('2026-10-23T12:00:00');
  assert.strictEqual(K.runBadge({ publishedAt: pub, pinned: true }, now).level, 'ok');
  assert.strictEqual(K.runBadge({ publishedAt: pub, demo: true }, now).level, 'ok');
});

test('есть прогон новее: подсветка и текст, у закреплённого — с пометкой', () => {
  const pub = '2026-10-23T11:00:00+03:00', now = msk('2026-10-23T11:05:00');
  const n = K.runBadge({ publishedAt: pub, newer: { run_id: 9, text: '23.10 11:30 · #9' } }, now);
  assert.strictEqual(n.level, 'newer');
  assert.strictEqual(n.why, 'есть прогон новее: 23.10 11:30 · #9');
  const pin = K.runBadge({ publishedAt: pub, pinned: true, newer: { run_id: 9 } }, now);
  assert.strictEqual(pin.level, 'ok', 'закреплённый прогон — без «⟳»');
  assert.match(pin.why, /#9 \(этот выбран вручную\)/);
  assert.strictEqual(K.runBadge({ publishedAt: pub, demo: true, newer: { run_id: 9 } }, now).level, 'ok');
});

test('часовой пояс браузера не влияет: весь файл ещё раз в America/New_York и Asia/Vladivostok', () => {
  if (process.env.RUN_BADGE_TZ) {  // дочерний процесс: пояс действительно чужой
    assert.strictEqual(Intl.DateTimeFormat().resolvedOptions().timeZone, process.env.TZ);
    return;
  }
  const { execFileSync } = require('child_process');
  for (const TZ of ['America/New_York', 'Asia/Vladivostok']) {
    // упадёт хоть одна проверка — ненулевой код, execFileSync бросит ошибку с выводом
    execFileSync(process.execPath, ['--test', __filename], { env: Object.assign({}, process.env, { TZ, RUN_BADGE_TZ: '1' }), encoding: 'utf8' });
  }
});

test('что вошло: демо-набор — позиции, книги, PnL, рынок', () => {
  const c = K.runCoverage();
  assert.match(c.short, /^\d+\/\d+ поз\.$/);
  const k = c.rows.map(r => r.k);
  ['Позиции', 'Книги', 'PnL', 'Рынок'].forEach(x => assert.ok(k.includes(x), x));
});

test('что вошло: прогон из базы — без оценки и без PnL подсвечены, время публикации — из source.publishedAt', () => {
  const { K: K2, M } = loadKit(D => {
    D.source.publishedAt = '2026-10-23T08:12:00+00:00';
    D.positions[0].notValued = true;
  });
  assert.strictEqual(M.run.publishedAt, '2026-10-23T08:12:00+00:00');
  const c = K2.runCoverage(), n = M.positions.length;
  assert.strictEqual(c.short, (n - 1) + '/' + n + ' поз.');
  const pos = c.rows.find(r => r.k === 'Позиции');
  assert.strictEqual(pos.v, (n - 1) + ' из ' + n + ' оценены, 1 без оценки');
  assert.strictEqual(pos.warn, true);
  const books = new Set(M.positions.map(p => p.book)).size;
  assert.strictEqual(c.rows.find(r => r.k === 'Книги').v, String(books));
  assert.ok(c.rows.find(r => r.k === 'Фондирование'));
});

test('lib/live.js: подписка сообщает странице о новых событиях и режиме ленты событием окна desk:live', async () => {
  const got = [];
  const ctx = {
    console, URL, setTimeout, clearTimeout, JSON, AbortController,
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ events: [{ id: 5, type: 'run.published', run_id: 9, text: 'r9' }], has_more: false, last_id: 5 }) }),
    document: { hidden: false },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    CustomEvent: class { constructor(type, o) { this.type = type; this.detail = o.detail; } },
    dispatchEvent(e) { got.push(e); return true; }
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(P('concepts/lib/live.js'), ctx);
  const sub = ctx.DESK_LIVE.subscribe({ after: 4, types: 'run.published', onEvents() {}, noShare: true });
  await new Promise(r => setTimeout(r, 10));
  sub.stop();
  assert.ok(got.every(e => e.type === 'desk:live'));
  assert.deepStrictEqual(got.map(e => e.detail.kind), ['mode', 'events']);
  assert.strictEqual(got[0].detail.mode, 'poll');  // EventSource нет — опрос
  assert.deepStrictEqual(JSON.parse(JSON.stringify(got[1].detail.list)).map(e => e.run_id), [9]);
  assert.strictEqual(ctx.DESK_LIVE.newRun(got[1].detail.list[0], { source: 'real', runId: 7, asOf: '2026-10-23' }), true);
});
