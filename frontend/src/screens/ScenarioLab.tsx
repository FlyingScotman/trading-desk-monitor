import { useEffect, useMemo, useRef, useState } from 'react';
import { getDefaultRun, getJob, getRisk, getScenarioDefs, listRuns, submitJob } from '../api/client';
import type { RiskPayload, RunHeader, ScenarioDefOut, ScenarioJob } from '../api/types';
import { ResidualTable, ResultTable } from '../components/ResultTable';
import { type EditorState, ScenarioEditor, effective } from '../components/ScenarioEditor';
import { StatusChip } from '../components/StatusChip';
import { num, smln } from '../format';
import { type NormDef, canon, normalize, runTaylor } from '../taylor/taylor';

const ACCURACY_BP = 500;
const POLL_MS = 500;
const queryRun = Number(new URLSearchParams(location.search).get('run_id')) || null;

export function ScenarioLab() {
  const [runId, setRunId] = useState<number | null>(queryRun);
  const [runs, setRuns] = useState<RunHeader[]>([]);
  const [payload, setPayload] = useState<RiskPayload | null>(null);
  const [presets, setPresets] = useState<ScenarioDefOut[]>([]);
  const [presetId, setPresetId] = useState('');
  const [ed, setEd] = useState<EditorState>({ base: { curves: {} }, add: 0 });
  const [curvePick, setCurvePick] = useState('RUB OFZ');
  const [job, setJob] = useState<ScenarioJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pollTick, setPollTick] = useState(0);  // bumped on a failed poll, so polling retries
  const runRef = useRef(runId);
  runRef.current = runId;

  useEffect(() => {
    listRuns().then(setRuns).catch(e => setError(String(e.message)));
    if (runId == null) getDefaultRun().then(r => setRunId(r.run_id)).catch(e => setError(String(e.message)));
    getScenarioDefs().then(list => {
      setPresets(list);
      const first = list.find(p => p.scenario_id === 'keyhike') ?? list[0];
      if (first) { setPresetId(first.scenario_id); setEd({ base: first.definition, add: 0 }); }
    }).catch(e => setError(String(e.message)));
  }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (runId == null) return;
    let alive = true;  // a slower response for an earlier run must not replace this one
    setPayload(null);
    setJob(null);
    getRisk(runId).then(p => { if (alive && p.run_id === runId) setPayload(p); })
      .catch(e => { if (alive) setError(String(e.message)); });
    return () => { alive = false; };
  }, [runId]);

  // Poll the exact job until it settles; a failed poll retries, so the job never hangs in queued/running.
  useEffect(() => {
    if (!job || job.status === 'done' || job.status === 'failed') return;
    let alive = true;  // false once the job was replaced or cleared (run switch)
    const t = setTimeout(() => getJob(job.job_id)
      .then(j => { if (alive) { setPollError(null); setJob(j); } })
      .catch(e => { if (alive) { setPollError(String(e.message)); setPollTick(n => n + 1); } }), POLL_MS);
    return () => { alive = false; clearTimeout(t); };
  }, [job, pollTick]);

  const curves = payload?.curves ?? ['RUB OFZ'];
  const curve = curves.includes(curvePick) ? curvePick : curves.includes('RUB OFZ') ? 'RUB OFZ' : curves[0];
  const norm = useMemo((): NormDef | string => {
    try { return normalize(effective(ed), payload?.curves); } catch (e) { return (e as Error).message; }
  }, [ed, payload]);
  const taylor = useMemo(() => (payload && typeof norm !== 'string' ? runTaylor(payload, norm) : null), [payload, norm]);

  const stale = useMemo(() => {
    if (!job || typeof norm === 'string') return true;
    try { return job.base_run_id !== runId || canon(normalize(job.definition)) !== canon(norm); } catch { return true; }
  }, [job, norm, runId]);

  const pickRun = (id: number) => {
    const u = new URL(location.href);
    u.searchParams.set('run_id', String(id));
    history.replaceState(null, '', u);
    setRunId(id);
  };
  const pickPreset = (id: string) => {
    const p = presets.find(x => x.scenario_id === id);
    if (p) { setPresetId(id); setEd({ base: p.definition, add: 0 }); }
  };
  const exact = () => {
    if (runId == null || typeof norm === 'string') return;
    setError(null);
    submitJob(runId, norm).then(j => { if (runRef.current === j.base_run_id) setJob(j); })
      .catch(e => setError(String(e.message)));
  };

  // Chips: share of MV with first-order Taylor only; largest move beyond the 2nd-order accuracy range.
  const absMv = payload?.positions.reduce((s, p) => s + Math.abs(p.pv_rub), 0) ?? 0;
  const firstOnlyMv = payload && taylor
    ? taylor.positions.reduce((s, r, i) => s + (r.second_order ? 0 : Math.abs(payload.positions[i].pv_rub)), 0) : 0;
  const maxMove = taylor?.positions.reduce((m, r) => Math.max(m, r.max_move_bp), 0) ?? 0;
  const result = job?.status === 'done' ? job.result : null;
  const cov = result?.coverage;
  const tot = result?.totals._total;
  const missingTitle = cov?.missing.map(m => `${m.position_id}: ${m.reason}`).join('\n');

  return (
    <>
      <header className="top">
        <div className="brand"><b>Scenario <i>Lab</i></b><span>FI-деск · Тейлор ≈ в браузере, точная переоценка на сервере</span></div>
        <div className="spacer" />
        <label className="runpick">
          <span className="muted">Прогон</span>
          <select className="sel" value={runId ?? ''} onChange={e => pickRun(+e.target.value)}>
            {runId != null && !runs.some(r => r.run_id === runId) && <option value={runId}>#{runId}</option>}
            {runs.map(r => (
              <option key={r.run_id} value={r.run_id}>
                {r.business_date} · #{r.run_id} · {r.cut_type}{r.is_default ? ' · по умолчанию' : ''}
              </option>
            ))}
          </select>
        </label>
      </header>

      <main className="scen">
        <ScenarioEditor presets={presets} presetId={presetId} onPreset={pickPreset} buckets={payload?.buckets ?? []}
          curves={curves} curve={curve} onCurve={setCurvePick} ed={ed} onChange={setEd} />

        <section className="res">
          {error && <div className="panel err" role="alert">{error}</div>}
          {pollError && <div className="panel err" role="alert">Опрос точного расчёта: {pollError}; повторяю…</div>}
          {typeof norm === 'string' && <div className="panel err" role="alert">Сценарий некорректен: {norm}</div>}
          {!payload && !error && <div className="panel empty">Загрузка риска прогона…</div>}

          {payload && taylor && (
            <div className="panel">
              <div className="hero">
                <div><div className="lbl">PnL сценария, Тейлор</div>
                  <div className="big approx">{smln(taylor.totals._total?.total ?? 0, 1)}<small>млн ₽</small></div></div>
                {tot && (
                  <div className={stale ? 'stale' : undefined}
                    title="Точная переоценка покрытых позиций + Тейлор для остальных: сравнимо с итогом по Тейлору">
                    <div className="lbl">Точно{tot.n_exact < tot.n_positions ? ` + ≈ для ${tot.n_positions - tot.n_exact} непокрытых` : ''}</div>
                    <div className="big">{smln(tot.blended, 1)}<small>млн ₽</small></div>
                    <div className="sub">покрытые {tot.n_exact}/{tot.n_positions}: точно {smln(tot.exact, 1)} vs ≈ {smln(tot.taylor_covered, 1)},
                      остаток {smln(tot.residual, 1)}</div>
                  </div>
                )}
                <div className="side">
                  <span>{payload.business_date} · прогон #{payload.run_id} · {payload.positions.length} позиций</span>
                  <div className="chips">
                    {firstOnlyMv > 0 && <StatusChip tone="warn" title="У этих позиций нет гаммы (резервные ступени оценки)">
                      только 1-й порядок · {num(firstOnlyMv / (absMv || 1) * 100, 0)} % MV</StatusChip>}
                    {maxMove > ACCURACY_BP && <StatusChip tone="crit" title={`Сдвиг до ${num(maxMove)} б.п.: вне области точности 2-го порядка`}>
                      &gt;{ACCURACY_BP} б.п.: нужен точный расчёт</StatusChip>}
                    {job?.status === 'queued' && <StatusChip tone="info">точный расчёт: в очереди</StatusChip>}
                    {job?.status === 'running' && <StatusChip tone="info">точный расчёт: считается…</StatusChip>}
                    {job?.status === 'failed' && <StatusChip tone="crit" title={job.error ?? undefined}>точный расчёт: ошибка</StatusChip>}
                    {cov && <StatusChip tone={cov.status === 'complete' ? 'ok' : 'warn'} title={missingTitle || undefined}>
                      {cov.status === 'complete' ? 'точно' : 'частично'} {cov.n_exact}/{cov.n_positions}</StatusChip>}
                    {result && stale && <StatusChip tone="info">сценарий изменён: точные цифры устарели</StatusChip>}
                  </div>
                </div>
                <div className="spacer" />
                <button className="btn primary" onClick={exact} disabled={typeof norm === 'string' || job?.status === 'queued' || job?.status === 'running'}>
                  Посчитать точно
                </button>
              </div>
              <div className="ph"><h3>По книгам</h3><span className="hint">млн ₽ · ≈ — разложение Тейлора</span></div>
              <ResultTable taylor={taylor} result={result} stale={stale} />
            </div>
          )}

          {payload && result && (
            <div className="panel">
              <div className="ph"><h3>Топ-20 позиций по |остатку|</h3><span className="hint">точно − Тейлор, млн ₽</span></div>
              <ResidualTable payload={payload} result={result} stale={stale} />
            </div>
          )}
        </section>
      </main>
    </>
  );
}
