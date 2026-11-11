import { describe, expect, it } from 'vitest';
import { NO_GAMMA_TITLE, SECOND_PENDING_TITLE, firstOrderTitle, secondPending } from './secondOrder';

describe('first order chip reason (TDM#627)', () => {
  it('a run without the second derivatives yet: the second pass of the history', () => {
    expect(secondPending({ second_order: false })).toBe(true);
    expect(secondPending({ second_order: false }, { taylor_second_order: true })).toBe(true);
    expect(firstOrderTitle(true)).toBe(SECOND_PENDING_TITLE);
    expect(SECOND_PENDING_TITLE).toContain('ещё не посчитаны');
    expect(SECOND_PENDING_TITLE).toContain('второй проход пересчёта истории');
  });
  it('the job result says it too (only when false)', () => {
    expect(secondPending({ second_order: true }, { taylor_second_order: false })).toBe(true);
  });
  it('absent fields mean the run has them: the old reason', () => {
    expect(secondPending({})).toBe(false);
    expect(secondPending(undefined, undefined)).toBe(false);
    expect(secondPending({ second_order: true }, {})).toBe(false);
    expect(firstOrderTitle(false)).toBe(NO_GAMMA_TITLE);
  });
});
