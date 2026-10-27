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
    let seq = 0;
    const run = async query => {
      const id = ++seq; publish({ loading: true });
      try { const data = await fetcher(query); if (id === seq) publish({ data }); }
      catch (error) { if (id === seq) publish({ error }); }
    };
    run.invalidate = () => { ++seq; };
    return run;
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
  const api = { factors, loader, scope };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PNL_HISTORY = api;
})(typeof window === 'object' ? window : globalThis);
