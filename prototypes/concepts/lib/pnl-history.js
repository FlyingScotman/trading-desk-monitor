/* Saved PnL report: the same filtered row set feeds every aggregate; missing values stay null. */
(function (root) {
  'use strict';
  const factors = [
    ['carry', 'Кэрри', 'Включает купоны и погашения; выплаты повторно не прибавляются.'],
    ['rolldown', 'Roll-down', 'Сохранённый эффект сдвига по кривой.'],
    ['rates', 'Ставки', 'Точная переоценка; нелинейность уже внутри ставок и спреда.'],
    ['spread', 'Спред', 'Сохранённый эффект изменения спреда.'],
    ['convexity', 'Выпуклость', 'В текущем flash равна 0: уже включена в точные ставки и спред.'],
    ['fx_gross', 'FX активов', 'По рыночному FX; альтернативный FX ЦБ не складывается с ним.'],
    ['fx_funding', 'FX фондирования', 'Сохранённый эффект валютного пассива.'],
    ['funding', 'Фондирование позиций', 'Только записанное в PnL позиций; строки уровня книги не входят.'],
    ['new_trades', 'Новые сделки', 'В текущем flash 0 — заглушка, не расчёт торгового результата.'],
    ['reported', 'Разница PV по отчётам', 'Метод desk-reported-v1: разница отчётных PV; сделки могут быть внутри.'],
    ['other', 'Прочее по методу', 'Flash: остаток переоценки; core FX: прочие эффекты ставок, пунктов и времени. Это не единый экономический фактор.'],
  ];
  // Latest request wins even if the transport cannot cancel an earlier response. Invalidate before starting it.
  function loader(fetcher, publish) {
    let seq = 0, controller = null;
    const invalidate = () => { ++seq; if (controller) controller.abort(); controller = null; };
    const run = async query => {
      invalidate();
      const id = seq;
      controller = new AbortController();
      const signal = controller.signal;
      publish({ loading: true });
      try { const data = await fetcher(query, signal); if (id === seq) publish({ data }); }
      catch (error) { if (id === seq) publish({ error }); }
      finally { if (id === seq) controller = null; }
    };
    run.invalidate = invalidate;
    return run;
  }
  function periodError(from, to, asOf) {
    const day = value => {
      if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return NaN;
      const ms = Date.parse(value + 'T00:00:00Z');
      return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === value ? ms : NaN;
    };
    const start = day(from), end = day(to);
    if (!Number.isFinite(start) || !Number.isFinite(end) || from > to || to > asOf)
      return 'Выберите начало ≤ конец ≤ дата выбранного прогона (' + asOf + ').';
    if ((end - start) / 86400000 + 1 > 1096) return 'Период — не более 1096 календарных дней включительно.';
    return null;
  }
  function scope(rows) {
    const books = new Map(), positions = new Map(), entities = new Map();
    rows.forEach(r => {
      const entity = r.entity || '—', desk = r.desk || '—';
      books.set(JSON.stringify([r.book, entity, desk]), { id: r.book, name: r.bookName || r.book, entity, desk });
      entities.set(entity, { id: entity, name: entity });
      positions.set(JSON.stringify([r.book, r.positionId, entity, desk, r.portfolio]),
        { id: r.positionId, book: r.book, entity, desk, portfolio: r.portfolio });
    });
    const sorted = map => [...map].sort(([a], [b]) => a.localeCompare(b)).map(([, value]) => value);
    const memberships = sorted(books);
    return { books: memberships, book: Object.fromEntries(memberships.map(b => [b.id, b])), entities: sorted(entities), positions: sorted(positions) };
  }
  const api = { factors, loader, scope, periodError };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PNL_HISTORY = api;
})(typeof window === 'object' ? window : globalThis);
