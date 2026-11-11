// Why a run's Taylor estimate is first order only (TDM#627). The history of runs is made in two passes: the first leaves out
// the second derivatives (convexity, gamma) so every date has its value, risk and PnL early, the second adds them from the
// newest dates. A date the second pass has not reached says so: RiskPayload.second_order is false (and a scenario job's
// ScenarioResult.taylor_second_order is false); both fields are absent when true, so they are read as `?? true`.
import type { RiskPayload, ScenarioResult } from '../api/types';

export const SECOND_PENDING_TITLE = 'Вторые производные для этой даты ещё не посчитаны (идёт второй проход пересчёта истории): '
  + 'оценка сценария линейная, а точный расчёт этим не затронут';
export const NO_GAMMA_TITLE = 'У этих позиций нет гаммы (резервные ступени оценки)';

/** The second derivatives of the run are not computed yet (not that the positions have none). */
export function secondPending(payload: Pick<RiskPayload, 'second_order'> | null | undefined,
  result?: Pick<ScenarioResult, 'taylor_second_order'> | null): boolean {
  return (payload?.second_order ?? true) === false || (result?.taylor_second_order ?? true) === false;
}

/** The title of the "first order only" chip: the true reason. */
export const firstOrderTitle = (pending: boolean): string => (pending ? SECOND_PENDING_TITLE : NO_GAMMA_TITLE);
