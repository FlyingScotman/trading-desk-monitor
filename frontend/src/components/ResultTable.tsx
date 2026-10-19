import type { RiskPayload, ScenarioResult } from '../api/types';
import { smln } from '../format';
import type { TaylorRun } from '../taylor/taylor';

const PARTS = [['rates', 'Ставки'], ['spread', 'Спред'], ['fx', 'FX'], ['convexity', 'Выпуклость'], ['total', 'Итого']] as const;

interface Props {
  taylor: TaylorRun;
  result: ScenarioResult | null;  // exact columns only when a job is done
  stale: boolean;
}

/** Per-book Taylor ≈ table. Exact columns pair like with like: covered ≈ vs covered exact (residual),
 *  and blended (exact covered + ≈ uncovered) vs «Итого ≈»; coverage n/N. */
export function ResultTable({ taylor, result, stale }: Props) {
  if (!taylor.totals._total) return <div className="empty">В прогоне нет оценённых позиций</div>;
  const books = Object.keys(taylor.totals).filter(k => k !== '_total');
  const ex = stale ? 'n stale' : 'n';
  const row = (key: string, label: string) => {
    const t = taylor.totals[key];
    const e = result?.totals[key];
    return (
      <tr key={key} className={key === '_total' ? 'tot' : undefined}>
        <td className="l">{label}</td>
        {PARTS.map(([k]) => <td key={k} className="n approx">{smln(t[k])}</td>)}
        {result && <>
          <td className={ex + ' approx'}>{smln(e?.taylor_covered)}</td>
          <td className={ex}>{smln(e?.exact)}</td>
          <td className={ex}>{smln(e?.residual)}</td>
          <td className={ex}>{smln(e?.blended)}</td>
          <td className={ex + (e && e.n_exact < e.n_positions ? ' partial' : '')}
            title={e && e.n_exact < e.n_positions ? 'Частично: остальные позиции — только по Тейлору' : undefined}>
            {e ? `${e.n_exact}/${e.n_positions}` : '—'}
          </td>
        </>}
      </tr>
    );
  };
  return (
    <div className="scroll">
      <table className="t">
        <thead>
          <tr>
            <th className="l">Книга</th>
            {PARTS.map(([k, n]) => <th key={k}>{n} ≈</th>)}
            {result && <>
              <th className={stale ? 'stale' : undefined} title="Тейлор только по позициям с точной переоценкой">Покрытые ≈</th>
              <th className={stale ? 'stale' : undefined} title="Точная переоценка покрытых позиций">Покрытые точно</th>
              <th className={stale ? 'stale' : undefined} title="Точно − Тейлор по покрытым">Остаток</th>
              <th className={stale ? 'stale' : undefined} title="Точно для покрытых + Тейлор для остальных: сравнимо с «Итого ≈»">Точно + ≈ непокр.</th>
              <th className={stale ? 'stale' : undefined}>Покрытие</th></>}
          </tr>
        </thead>
        <tbody>
          {books.map(b => row(b, b))}
          {row('_total', 'Итого')}
        </tbody>
      </table>
    </div>
  );
}

/** Top 20 positions by |residual| of the exact job. */
export function ResidualTable({ payload, result, stale }: { payload: RiskPayload; result: ScenarioResult; stale: boolean }) {
  const byId = new Map(payload.positions.map(p => [p.position_id, p]));
  const top = result.positions.filter(p => p.residual != null)
    .sort((a, b) => Math.abs(b.residual!) - Math.abs(a.residual!)).slice(0, 20);
  const ex = stale ? 'n stale' : 'n';
  if (!top.length) return <div className="empty">Нет позиций с точной переоценкой</div>;
  return (
    <div className="scroll">
      <table className="t">
        <thead>
          <tr><th className="l">Позиция</th><th className="l">Книга</th><th className="l">Качество</th>
            <th>Итого ≈</th><th>Точно</th><th>Остаток</th></tr>
        </thead>
        <tbody>
          {top.map(p => {
            const rp = byId.get(p.position_id);
            return (
              <tr key={p.position_id}>
                <td className="l" title={p.position_id}>{rp?.name ?? rp?.secid ?? p.position_id}</td>
                <td className="l">{p.book_id}</td>
                <td className="l muted">{p.quality ?? '—'}</td>
                <td className="n approx">{smln(p.taylor.total, 3)}</td>
                <td className={ex}>{smln(p.exact, 3)}</td>
                <td className={ex}>{smln(p.residual, 3)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
