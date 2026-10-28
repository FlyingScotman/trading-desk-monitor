/* Saved PnL history helpers shared by C19: request ordering, period validation, filter scope. */
(function (root) {
  'use strict';
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
  const api = { loader, scope, periodError };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PNL_HISTORY = api;
})(typeof window === 'object' ? window : globalThis);
