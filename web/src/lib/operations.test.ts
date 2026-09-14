import { describe, expect, it } from 'vitest';
import { elapsedText, investigationNeedsPolling } from './operations';
import { jobIsActive, pollInterval, POLL_WINDOW_MS } from './polling';

describe('bounded visible polling', () => {
  it('stops for hidden, completed, failed and expired sessions', () => {
    expect(pollInterval(true, true, 0, 1)).toBe(5000);
    expect(pollInterval(true, false, 0, 1)).toBe(false);
    expect(pollInterval(false, true, 0, 1)).toBe(false);
    expect(pollInterval(true, true, 0, POLL_WINDOW_MS)).toBe(false);
    expect(['succeeded', 'failed', 'cancelled'].some((status) => jobIsActive({ status }))).toBe(
      false,
    );
  });
  it('does not treat an old active job link or paused investigation as pending work', () => {
    const item = { status: 'active' as const, active_job_id: 'saved-job' };
    expect(investigationNeedsPolling(item)).toBe(true);
    expect(investigationNeedsPolling(item, [{ id: 'saved-job', status: 'running' }])).toBe(true);
    expect(investigationNeedsPolling(item, [{ id: 'saved-job', status: 'succeeded' }])).toBe(false);
    expect(investigationNeedsPolling({ ...item, status: 'paused' })).toBe(false);
    expect(investigationNeedsPolling({ ...item, active_job_id: null })).toBe(false);
  });
});

it('preserves unknown and future observation ages', () => {
  expect(elapsedText(null)).toBe('미확인');
  expect(elapsedText(undefined)).toBe('미확인');
  expect(elapsedText(-1)).toBe('조회 기준 이후');
  expect(elapsedText(0)).toBe('0초');
  expect(elapsedText(90061)).toBe('1일');
});
