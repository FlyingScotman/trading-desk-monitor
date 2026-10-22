import type { RiskPayload, RunHeader, ScenarioDefOut, ScenarioDefinition, ScenarioJob } from './types';

/** The session's CSRF token (cookie desk_csrf, set at sign-in; backend api/auth.py): sent with every write. */
function csrf(): string {
  const m = document.cookie.match(/(?:^|; )desk_csrf=([^;]*)/);
  return m ? decodeURIComponent(m[1]) : '';
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const write = !!init?.method && !/^(GET|HEAD)$/i.test(init.method);
  const headers = new Headers(init?.headers);
  if (write && csrf()) headers.set('X-CSRF-Token', csrf());
  const r = await fetch('/api/v1' + path, { ...init, headers, credentials: 'same-origin' });
  if (r.status === 401) {  // signed out or the session expired: the login page, back here afterwards
    location.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search);
  }
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
