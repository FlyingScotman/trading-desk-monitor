import type { ReactNode } from 'react';

export type Tone = 'ok' | 'warn' | 'crit' | 'info';

export function StatusChip({ tone, title, children }: { tone: Tone; title?: string; children: ReactNode }) {
  return (
    <span className={`chip ${tone}`} title={title}>
      <svg viewBox="0 0 10 10" aria-hidden="true"><circle cx="5" cy="5" r="4" /></svg>
      {children}
    </span>
  );
}
