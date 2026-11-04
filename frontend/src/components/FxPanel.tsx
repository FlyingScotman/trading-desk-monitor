import type { ScenarioResult } from '../api/types';
import { num, smln } from '../format';
import type { FxShock } from '../taylor/fxshock';

interface Props { fx: FxShock; result: ScenarioResult | null; stale: boolean }

/** What the FX shock is made of (TDM#577): the published OCP by currency x the shock, and the non-linear derivatives apart. */
export function FxPanel({ fx, result, stale }: Props) {
  if (fx.mode === 'positions') {
    return (
      <div className="panel">
        <div className="ph"><h3>FX-шок</h3><span className="hint">запасной расчёт</span></div>
        <p className="note">ОВП нет: {fx.reason}. FX-шок посчитан от валютных дельт позиций монитора (облигации за вычетом валютного
          фондирования, деривативы) — это не ОВП и может с ней расходиться. Итог по FX: {smln(fx.linearTotal)} млн ₽.</p>
      </div>
    );
  }
  const nl = fx.nonlinear;
  return (
    <div className="panel">
      <div className="ph"><h3>FX-шок</h3><span className="hint">линейная часть — ОВП × шок · {fx.asOf ?? ''}</span></div>
      <p className="note">Линейная часть: открытая валютная позиция прогона (та же, что на экране «ОВП», весь деск — фильтр книг на этом
        экране не применяется) × шок курса, без строк нелинейных деривативов. Откуда возникла позиция — от облигации или от кэша, —
        неважно. Разбивки по позициям нет.{fx.methodNote ? ` ОВП ${fx.methodNote}.` : ''}
        {fx.missingRate.length > 0 && ` Нет курса для ${fx.missingRate.join(', ')}: в линейную часть они не входят.`}</p>
      <div className="scroll">
        <table className="t">
          <thead><tr><th className="l">Валюта</th><th>ОВП, в валюте</th><th>из неё нелинейные</th><th>линейная</th><th>Курс, ₽</th>
            <th>Шок, %</th><th>FX, млн ₽</th></tr></thead>
          <tbody>
            {fx.ccys.map(c => (
              <tr key={c.ccy}>
                <td className="l">{c.ccy}</td><td className="n">{num(c.amount)}</td><td className="n">{num(c.nonlinear)}</td>
                <td className="n">{num(c.linear)}</td><td className="n">{c.rate == null ? '—' : num(c.rate, 4)}</td>
                <td className="n">{num(c.pct, 1)}</td><td className="n approx">{smln(c.rub)}</td>
              </tr>
            ))}
            <tr className="tot"><td className="l" colSpan={6}>Линейная часть, итого</td><td className="n approx">{smln(fx.linearTotal)}</td></tr>
          </tbody>
        </table>
      </div>
      <div className="ph"><h3>Нелинейные деривативы</h3><span className="hint">опционы и всё, где ОВП меняется нелинейно</span></div>
      {nl.n === 0 ? <p className="note">Нелинейных деривативов нет.</p> : (
        <>
          <p className="note">{nl.n} поз.: шок по ним — не ОВП × шок. Оценка по дельте и гамме (≈, только валютные факторы);
            ставки и спред этих позиций — в таблице по книгам.</p>
          <div className="hero">
            <div><div className="lbl">Переоценка при шоке, дельта-гамма ≈</div>
              <div className="big approx">{smln(nl.taylor, 2)}<small>млн ₽</small></div></div>
            {result && nl.exact != null && (
              <div className={stale ? 'stale' : undefined}
                title="Точная переоценка позиций целиком, по всем факторам сценария (не только FX), против Тейлора по тем же позициям">
                <div className="lbl">Полная переоценка позиций целиком</div>
                <div className="big">{smln(nl.exact, 2)}<small>млн ₽</small></div>
                <div className="sub">по Тейлору (все факторы) ≈ {smln(nl.taylorAll, 2)}</div>
              </div>
            )}
            {result && nl.exact == null && <div className="sub">Полная переоценка есть не по всем этим позициям — только дельта-гамма.</div>}
          </div>
        </>
      )}
    </div>
  );
}
