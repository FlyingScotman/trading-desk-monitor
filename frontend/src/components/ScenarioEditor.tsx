import type { ScenarioDefOut, ScenarioDefinition } from '../api/types';
import { snum } from '../format';
import { N_BUCKETS, RUB_CURVES } from '../taylor/taylor';

/** What the user edits: a base definition (from a preset) plus a parallel add-on to every RUB curve. */
export interface EditorState {
  base: ScenarioDefinition;
  add: number;
}

/** The parallel add-on of a curve: every RUB curve moves with it (all rates), a currency's curve does not. */
const addOf = (add: number, curve: string) => (RUB_CURVES.includes(curve) ? add : 0);

export const baseCurve = (def: ScenarioDefinition, curve: string): number[] => {
  const v = def.curves?.[curve] ?? 0;
  return Array.isArray(v) ? v : new Array<number>(N_BUCKETS).fill(v);
};

/** The definition sent to Taylor and to the exact job: the add-on on each RUB curve (an all-zero curve drops out). */
export function effective({ base, add }: EditorState): ScenarioDefinition {
  const curves = { ...base.curves };
  for (const c of RUB_CURVES) curves[c] = baseCurve(base, c).map(x => x + add);
  return { ...base, curves };
}

interface Props {
  presets: ScenarioDefOut[];
  presetId: string;
  onPreset: (id: string) => void;
  buckets: string[];
  curves: string[];
  curve: string;
  onCurve: (curve: string) => void;
  ed: EditorState;
  onChange: (ed: EditorState) => void;
}

function Slider({ id, label, min, max, value, onChange, title }: {
  id: string; label: string; min: number; max: number; value: number; onChange: (v: number) => void; title?: string;
}) {
  return (
    <div className="row" title={title}>
      <label htmlFor={id}>{label}</label>
      <input type="range" id={id} min={min} max={max} step={5} value={value} onChange={e => onChange(+e.target.value)} />
      <output className="num" htmlFor={id}>{snum(value)} б.п.</output>
    </div>
  );
}

export function ScenarioEditor({ presets, presetId, onPreset, buckets, curves, curve, onCurve, ed, onChange }: Props) {
  const { base, add } = ed;
  const curveNow = baseCurve(base, curve).map(x => x + addOf(add, curve));
  const setBucket = (i: number, v: number) => {
    const c = baseCurve(base, curve).slice();
    c[i] = v - addOf(add, curve);
    onChange({ add, base: { ...base, curves: { ...base.curves, [curve]: c } } });
  };
  const setSpreads = (k: '_all', v: number) => onChange({ add, base: { ...base, spreads: { ...base.spreads, [k]: v } } });

  const s = base.spreads ?? {};
  const extras = [
    s._mult != null && s._mult !== 1 ? `спреды ×${s._mult}` : '',
    ...(['sector', 'issuer', 'rating'] as const).flatMap(g =>
      Object.entries(s[g] ?? {}).map(([k, v]) => `${k} ${snum(v)} б.п.`)),
    ...Object.entries(base.fx ?? {}).map(([c, v]) => `${c} ${snum(v)} %`),
  ].filter(Boolean);

  return (
    <aside className="panel builder" aria-label="Конструктор сценария">
      <div className="sect">
        <div className="ph"><h2>Сценарий</h2><span className="hint">пресет как основа</span></div>
        <div className="pb">
          <select className="sel" value={presetId} onChange={e => onPreset(e.target.value)} aria-label="Пресет">
            {presetId === '' && <option value="">— свой —</option>}
            {presets.map(p => <option key={p.scenario_id} value={p.scenario_id}>{p.name}</option>)}
          </select>
          {extras.length > 0 && <p className="note">Также в сценарии: {extras.join(' · ')}</p>}
        </div>
      </div>

      <div className="sect">
        <div className="ph"><h2>Кривые</h2><span className="hint">б.п., шаг 5</span></div>
        <div className="pb">
          <Slider id="add" label="Параллельно" min={-500} max={500} value={add} onChange={v => onChange({ base, add: v })}
            title={`Параллельная надбавка ко всем бакетам рублёвых кривых (${RUB_CURVES.join(', ')})`} />
          <select className="sel" value={curve} onChange={e => onCurve(e.target.value)} aria-label="Кривая">
            {curves.map(c => <option key={c} value={c}>{c === 'RUB OFZ' ? `${c} — доходности ОФЗ (КБД)` : c}</option>)}
          </select>
          <div className="buckets" role="group" aria-label="Шок по бакетам, б.п.">
            {buckets.map((b, i) => (
              <label key={b}>
                <span>{b}</span>
                <input key={curveNow[i]} type="number" step={5} defaultValue={curveNow[i]}
                  onBlur={e => { const v = e.target.valueAsNumber; if (Number.isFinite(v) && v !== curveNow[i]) setBucket(i, v); }}
                  onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }} />
              </label>
            ))}
          </div>
          <p className="note">Итоговый вектор шока кривой {curve}; каждая кривая сдвигается только своим вектором
            (флоатер видит базис).{curve === 'RUB OFZ' && <> RUB OFZ — сдвиг доходностей ОФЗ (КБД): базис ОФЗ к RUONIA
            сдвигается на RUB OFZ − RUB RUONIA.</>} Правка бакета — Enter или уход из поля.</p>
        </div>
      </div>

      <div className="sect">
        <div className="ph"><h2>Спреды</h2><span className="hint">б.п.</span></div>
        <div className="pb">
          <Slider id="ofz" label="Спред ОФЗ" min={-200} max={300} value={base.ofzSpread ?? 0}
            onChange={v => onChange({ add, base: { ...base, ofzSpread: v } })} title="Z-спред ОФЗ к RUONIA" />
          <Slider id="all" label="Кредит, все" min={-300} max={1000} value={s._all ?? 0}
            onChange={v => setSpreads('_all', v)} title="Сдвиг кредитного спреда всех корпоратов (_all)" />
        </div>
      </div>
    </aside>
  );
}
