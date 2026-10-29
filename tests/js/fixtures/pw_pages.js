// Общие шаги Playwright-тестов мониторов (prototypes/concepts/, страницы как файлы), #453: настройки страницы через
// localStorage и ожидание, пока графики ECharts дорисованы.
//
// Почему не «записать и перезагрузить»: Chromium отдаёт запись localStorage / sessionStorage в хранилище браузера
// асинхронно, и документ после pg.reload() / pg.goto() иногда её ещё не видит (на file:// — 4–9 перезагрузок из 45).
// Так терялись мера C13 и режим C06 (страница рисовала MV вместо CS01 / PnL — «нет отрицательных столбцов с
// подписью»), а ещё чаще — флаг «первая загрузка» в sessionStorage, и тогда localStorage очищался заново.
//
// themeInit(ctx, theme, repr) — тема и валюта отчёта в каждом документе контекста, до скриптов страницы (новый
//   контекст и так начинается с пустого хранилища, чистить нечего).
// openWith(pg, url, ready, store) — открыть страницу с настройками store ({ключ: строка}) в localStorage: записать,
//   перезагрузить, дождаться ready и проверить, что записанное видел сам документ в начале загрузки (снимок
//   localStorage до скриптов страницы: запись, дошедшая позже, страницу уже не меняет); не видел — записать снова
//   (до 5 раз), иначе ошибка.
// settle(pg) — графики дорисованы: два кадра и 400 мс (отложенные перерисовки страниц, ResizeObserver), затем
//   не меньше 3 кадров подряд у всех экземпляров ECharts размер совпадает с элементом, анимаций нет, кадр и
//   отложенные действия не ждут отрисовки (то же условие, по которому ECharts шлёт событие finished). Не дождались
//   за 10 с — ошибка.
'use strict';

const themeInit = (ctx, theme, repr) => ctx.addInitScript(([t, r]) => {
  try { localStorage.setItem('concepts-theme', t); localStorage.setItem('concepts-repr', r); } catch (e) { }
}, [theme, repr]);

// снимок localStorage в начале каждого документа страницы — то, что прочтут её скрипты
const snapped = new WeakSet();
const snapAtStart = async pg => {
  if (snapped.has(pg)) return;
  snapped.add(pg);
  await pg.addInitScript(() => {
    try { const o = {}; for (let i = 0; i < localStorage.length; i++) { const k = localStorage.key(i); o[k] = localStorage.getItem(k); } window.__pwStoreAtStart = o; } catch (e) { }
  });
};
const sameStore = (pg, store) => pg.evaluate(s => Object.entries(s).every(([k, v]) => (window.__pwStoreAtStart || {})[k] === v), store);

async function openWith(pg, url, ready, store) {
  store = store || {};
  await snapAtStart(pg);
  await pg.goto(url);
  for (let i = 0; ; i++) {
    await pg.evaluate(s => Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)), store);
    await pg.reload();
    await pg.waitForFunction(ready);
    if (await sameStore(pg, store)) return;
    if (i >= 4) throw new Error('документ после перезагрузки не видит в localStorage ' + JSON.stringify(store));
  }
}

const settle = async pg => {
  await pg.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => setTimeout(r, 400)))));
  await pg.evaluate(() => new Promise((resolve, reject) => {
    const busy = () => [...document.querySelectorAll('[_echarts_instance_]')].some(el => {
      const inst = window.echarts && echarts.getInstanceByDom(el);
      if (!inst || inst.isDisposed()) return false;
      const zr = inst.getZr();
      return (el.clientWidth > 0 && (inst.getWidth() !== el.clientWidth || inst.getHeight() !== el.clientHeight)) ||
        !zr.animation.isFinished() || zr._needsRefresh || (inst._scheduler && inst._scheduler.unfinished) ||
        (inst._pendingActions && inst._pendingActions.length > 0);
    });
    const t0 = performance.now();
    let calm = 0;
    const tick = () => {
      calm = busy() ? 0 : calm + 1;
      if (calm >= 3) return resolve();
      if (performance.now() - t0 > 10e3) return reject(new Error('графики ECharts не дорисовались за 10 с'));
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }));
};

module.exports = { themeInit, openWith, settle };
