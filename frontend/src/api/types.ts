// Aliases of the generated API schema (src/api/schema.d.ts, `npm run gen:api` from openapi.json).
import type { components } from './schema';

type S = components['schemas'];

export type SpreadRules = S['SpreadRules'];
export type ScenarioDefinition = S['ScenarioDefinition'];
export type ScenarioDefOut = S['ScenarioDefOut'];
export type RiskPosition = S['RiskPosition'];
export type RiskPayload = S['RiskPayload'];
export type TaylorParts = S['TaylorParts'];
export type ResultPosition = S['ResultPosition'];
export type ResultTotals = S['ResultTotals'];
export type ScenarioResult = S['ScenarioResult'];
export type ScenarioJob = S['ScenarioJob'];
export type RunHeader = Pick<S['RunHeader'], 'run_id' | 'business_date' | 'cut_type' | 'status' | 'is_default'>;
