// Длинные таблицы (TDM#630, K.vwin в prototypes/concepts/lib/kit.js): таблица в 2000 строк держит в DOM только строки около
// видимой области; полоса прокрутки, итоги, сортировка, фильтр, поиск, группы, щелчок по строке, выгрузка CSV и ширина колонок
// — как у полной таблицы. Страница — c20 как файл (демо-набор), таблица — K.table на синтетических строках во вставленном
// контейнере. «Полная» таблица для сравнения — тот же K.table при K.VWIN_MIN = 1e9.
//   node --test tests/js/table_window_630.pw.test.js
// Нужен Playwright, который находит require('playwright') (NODE_PATH, как в CI); без него тест пропускается.
'use strict';
const test = require('node:test');
const assert = require('assert');
const path = require('path');
const { pathToFileURL } = require('url');

const pw = (() => { try { return require('playwright'); } catch (e) { return null; } })();
const PAGE = pathToFileURL(path.join(__dirname, '..', '..', 'prototypes', 'concepts', 'c20-trades.html')).href;
const N = 2000;

// строит K.table из N строк в контейнере (id), возвращает управление; rows — детерминированные
const SETUP = ([id, n, grouped, min]) => {
  const K = window.KIT, host = document.createElement('div');
  host.id = id; host.style.cssText = id === 'p' ? 'margin:8px' : 'height:420px;overflow:auto;margin:8px';  // 'p' — без своей прокрутки: таблицу листает страница
  document.body.appendChild(host);
  K.VWIN_MIN = min;
  const rows = Array.from({ length: n }, (_, i) => ({ id: 'r' + i, i, g: 'g' + (i % 7), name: 'Бумага ' + i + (i % 97 === 0 ? ' с очень длинным названием выпуска номер ' + i : ''), v: ((i * 37) % 1000) - 500, w: i % 13 === 0 ? null : (i % 50) / 4 }));
  window.__clicked = [];
  const api = K.table(host, {
    viewKey: 'win630-' + id, rows, search: true,
    columns: [{ key: 'i', label: '№', num: true }, { key: 'name', label: 'Бумага', filter: true }, { key: 'g', label: 'Группа', filter: true },
      { key: 'v', label: 'Сумма', num: true, sum: true, fmt: v => v.toLocaleString('en-US'), drill: false }, { key: 'w', label: 'Вес', num: true, nullable: true, fmt: v => v == null ? '—' : v.toFixed(2) }],
    groupBy: grouped ? [{ key: r => r.g, label: k => k, order: (a, b) => a.localeCompare(b) }] : undefined,
    onRow: r => window.__clicked.push(r && r.id),
  });
  window.__api = window.__api || {}; window.__api[id] = api;
};

const info = (pg, id) => pg.evaluate(id => {
  const host = document.getElementById(id), t = host.querySelector('table');
  const rows = [...t.querySelectorAll('tbody tr.row')];
  return {
    windowed: !!t.__vwin, rows: rows.length, first: rows[0] && rows[0].dataset.id, last: rows.length && rows[rows.length - 1].dataset.id,
    scrollH: host.scrollHeight, nodes: t.getElementsByTagName('*').length,
    widths: [...t.querySelectorAll('thead th')].map(th => Math.round(th.getBoundingClientRect().width)),
    foot: t.tFoot ? t.tFoot.textContent.replace(/\s+/g, ' ').trim() : '', grp: t.querySelectorAll('tbody tr.grp').length,
  };
}, id);
const csv = (pg, id) => pg.evaluate(id => window.__api[id].csv().split('\r\n').slice(2).join('\n'), id);
const idle = pg => pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 120)))));

async function open(browser) {
  const ctx = await browser.newContext({ viewport: { width: 1400, height: 900 } }), pg = await ctx.newPage(), errors = [];
  pg.on('pageerror', e => errors.push('pageerror: ' + e.message));
  pg.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  await pg.goto(PAGE);
  await pg.waitForFunction(() => window.KIT && window.MOCK);
  return { ctx, pg, errors };
}

test('K.table на 2000 строк: окно строк, полоса прокрутки как у полной таблицы, итог по всем строкам', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    const t0 = Date.now();
    await pg.evaluate(SETUP, ['w', N, false, 400]);
    await pg.evaluate(SETUP, ['f', N, false, 1e9]);
    await idle(pg);
    const w = await info(pg, 'w'), f = await info(pg, 'f');
    assert.ok(w.windowed && !f.windowed);
    assert.strictEqual(f.rows, N);
    assert.ok(w.rows >= 40 && w.rows < 300, 'в окне ' + w.rows + ' строк из ' + N);
    assert.ok(w.nodes < f.nodes / 5, 'узлов ' + w.nodes + ' вместо ' + f.nodes);
    assert.ok(Math.abs(w.scrollH - f.scrollH) <= f.scrollH * 0.01 + 2, 'высота прокрутки ' + w.scrollH + ' и ' + f.scrollH);
    assert.strictEqual(w.foot, f.foot, 'итог и число строк — по всем строкам');
    // самая широкая ячейка колонки выбирается по длине текста, а не по пикселям: расхождение в пределах пикселя-двух
    w.widths.forEach((x, i) => assert.ok(Math.abs(x - f.widths[i]) <= 2, 'ширина колонки ' + i + ': ' + x + ' и ' + f.widths[i]));
    assert.strictEqual(await csv(pg, 'w'), await csv(pg, 'f'), 'CSV — все строки');
    assert.ok(Date.now() - t0 < 20000);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table на 2000 строк: прокрутка рисует нужные строки, колонки не прыгают, строки не пропадают', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['w', N, false, 400]);
    await idle(pg);
    const top = await info(pg, 'w');
    for (const frac of [0.25, 0.5, 0.77, 1]) {
      await pg.evaluate(([id, fr]) => { const h = document.getElementById(id); h.scrollTop = (h.scrollHeight - h.clientHeight) * fr; }, ['w', frac]);
      await idle(pg);
      const s = await pg.evaluate(() => {
        const h = document.getElementById('w'), hr = h.getBoundingClientRect(), t = h.querySelector('table');
        // строки, лежащие в видимой части контейнера: подряд, без дыр, и они нарисованы (не распорка)
        const vis = [...t.querySelectorAll('tbody tr.row')].filter(tr => { const r = tr.getBoundingClientRect(); return r.bottom > hr.top + 40 && r.top < hr.bottom; }).map(tr => +tr.dataset.id.slice(1));
        const sp = [...t.querySelectorAll('tr.k-vs')].map(tr => { const r = tr.getBoundingClientRect(); return r.height > 0 && r.bottom > hr.top + 40 && r.top < hr.bottom; });
        return { vis, spacerVisible: sp.some(Boolean), widths: [...t.querySelectorAll('thead th')].map(th => Math.round(th.getBoundingClientRect().width)) };
      });
      assert.ok(s.vis.length >= 10, 'на экране строки при прокрутке ' + frac);
      assert.ok(!s.spacerVisible, 'на экране не пустая распорка при прокрутке ' + frac);
      s.vis.forEach((v, i) => i && assert.strictEqual(v, s.vis[i - 1] + 1, 'строки подряд'));
      const expected = Math.round(((N * 25 - 420) * frac) / 25);
      assert.ok(Math.abs(s.vis[0] - expected) < 60, 'на экране строки около ' + expected + ', а первая ' + s.vis[0]);
      assert.deepStrictEqual(s.widths, top.widths, 'ширина колонок не меняется при прокрутке ' + frac);
    }
    const end = await info(pg, 'w');
    assert.strictEqual(end.last, 'r' + (N - 1), 'внизу — последняя строка');
    assert.ok(end.rows < 300);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table на 2000 строк: сортировка, поиск, фильтр, щелчок по строке и группам — как у полной таблицы', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['w', N, true, 400]);
    await pg.evaluate(SETUP, ['f', N, true, 1e9]);
    await idle(pg);
    const both = async act => { await pg.evaluate(act, 'w'); await pg.evaluate(act, 'f'); await idle(pg); };
    const same = async what => {
      const w = await info(pg, 'w'), f = await info(pg, 'f');
      assert.strictEqual(w.foot, f.foot, what + ': подвал');
      assert.strictEqual(await csv(pg, 'w'), await csv(pg, 'f'), what + ': CSV');
      const first = await pg.evaluate(() => ['w', 'f'].map(id => document.querySelector('#' + id + ' tbody tr.row').dataset.id));
      assert.strictEqual(first[0], first[1], what + ': первая строка');
    };
    await same('группы');
    // сортировка по столбцу «Сумма» по убыванию: два щелчка по заголовку
    for (let k = 0; k < 2; k++) await both(id => document.querySelector('#' + id + ' th[data-k=v]').click());
    await same('сортировка');
    await both(id => { const i = document.querySelector('#' + id + ' input.k-input'); i.value = 'бумага 1'; i.dispatchEvent(new Event('input')); });
    await same('поиск');
    await both(id => { const i = document.querySelector('#' + id + ' input.k-input'); i.value = ''; i.dispatchEvent(new Event('input')); });
    await both(id => window.__api[id].setFilter('g', ['g1', 'g3']));
    await same('фильтр');
    await both(id => window.__api[id].setFilter('g', null));
    // свернуть первую группу: её строки уходят из таблицы и из подвала нет, но группа остаётся
    const before = await info(pg, 'w');
    await pg.evaluate(() => document.querySelector('#w tbody tr.grp').click());
    await idle(pg);
    const after = await info(pg, 'w');
    assert.ok(after.scrollH < before.scrollH - 200, 'свёрнутая группа убрала строки из высоты: ' + before.scrollH + ' → ' + after.scrollH);
    await pg.evaluate(() => document.querySelector('#w tbody tr.grp').click());
    await idle(pg);
    assert.strictEqual((await info(pg, 'w')).scrollH, before.scrollH);
    // щелчок по строке окна — onRow получает строку модели
    await pg.evaluate(() => { window.__clicked.length = 0; });
    const id = await pg.evaluate(() => { const tr = document.querySelectorAll('#w tbody tr.row')[3]; tr.querySelector('td').click(); return tr.dataset.id; });
    assert.deepStrictEqual(await pg.evaluate(() => window.__clicked), [id]);
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table на 2000 строк: рисуется быстро, при печати — все строки, потом снова окно', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    const took = await pg.evaluate(([setup, args]) => { const t = performance.now(); (new Function('return ' + setup)())(args); return performance.now() - t; }, [SETUP.toString(), ['w', N, true, 400]]);
    await idle(pg);
    const draw = await pg.evaluate(() => { const t = performance.now(); window.__api.w.setSort([['v', -1]]); return performance.now() - t; });
    assert.ok(took < 600 && draw < 400, 'первая отрисовка ' + Math.round(took) + ' мс, сортировка ' + Math.round(draw) + ' мс');
    await pg.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
    const printed = await info(pg, 'w');
    assert.strictEqual(printed.rows, N, 'печать — все строки');
    await pg.evaluate(() => window.dispatchEvent(new Event('afterprint')));
    await idle(pg);
    assert.ok((await info(pg, 'w')).rows < 300, 'после печати — снова окно');
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});

test('K.table на 2000 строк без своей прокрутки: окно идёт за прокруткой страницы', { skip: !pw && 'нет Playwright' }, async () => {
  const browser = await pw.chromium.launch();
  try {
    const { ctx, pg, errors } = await open(browser);
    await pg.evaluate(SETUP, ['p', N, false, 400]);
    await idle(pg);
    for (const frac of [0.3, 0.65, 1]) {
      // страница листается своим прокручиваемым корнем: ищем предка, который реально прокручивается, иначе окно
      const s = await pg.evaluate(fr => {
        const host = document.getElementById('p');
        let sc = null; for (let e = host.parentElement; e; e = e.parentElement) { const o = getComputedStyle(e).overflowY; if ((o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 5) { sc = e; break; } }
        const top = host.getBoundingClientRect().top + (sc ? sc.scrollTop : window.scrollY), H = host.getBoundingClientRect().height;
        const y = top + (H - innerHeight) * fr;
        if (sc) sc.scrollTop = y; else window.scrollTo(0, y);
        return { sc: sc ? sc.tagName + '.' + sc.className : 'window' };
      }, frac);
      await idle(pg);
      const r = await pg.evaluate(() => {
        const t = document.querySelector('#p table');
        const vis = [...t.querySelectorAll('tbody tr.row')].filter(tr => { const q = tr.getBoundingClientRect(); return q.bottom > 60 && q.top < innerHeight; }).length;
        const sp = [...t.querySelectorAll('tr.k-vs')].some(tr => { const q = tr.getBoundingClientRect(); return q.height > 0 && q.bottom > 60 && q.top < innerHeight; });
        return { vis, sp, rows: t.querySelectorAll('tbody tr.row').length };
      });
      assert.ok(r.vis >= 10 && !r.sp, 'при прокрутке ' + frac + ' (' + s.sc + ') на экране строки: ' + JSON.stringify(r));
      assert.ok(r.rows < 400);
    }
    assert.deepStrictEqual(errors, []);
    await ctx.close();
  } finally { await browser.close(); }
});
