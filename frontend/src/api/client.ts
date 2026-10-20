import type { RiskPayload, RunHeader, ScenarioDefOut, ScenarioDefinition, ScenarioJob } from './types';

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch('/api/v1' + path, init);
  if (!r.ok) {
    const body = await r.json().catch(() => null);  // ErrorOut {code, message, ...}
    throw new Error(body?.message ?? `${r.status} ${r.statusText}`);
  }
  return r.json();
}

export const getDefaultRun = (businessDate?: string | null) =>
  api<RunHeader>('/runs/default' + (businessDate ? `?business_date=${encodeURIComponent(businessDate)}` : ''));
export const getRisk = (runId: number) => api<RiskPayload>(`/runs/${runId}/risk`);
export const getScenarioDefs = () =>
  api<{ scenarios: ScenarioDefOut[] }>('/scenario-defs').then(r => r.scenarios);
export const submitJob = (runId: number, definition: ScenarioDefinition) =>
  api<ScenarioJob>('/scenario-jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ base_run_id: runId, definition }),
  });
export const getJob = (id: number) => api<ScenarioJob>(`/scenario-jobs/${id}`);
