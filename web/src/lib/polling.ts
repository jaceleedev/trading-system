export const ACTIVE_POLL_MS = 5_000;
export const IDLE_POLL_MS = 30_000;
export const POLL_WINDOW_MS = 5 * 60_000;

/** Each visible session is bounded; errors and terminal work require no background retry. */
export function pollInterval(
  active: boolean,
  visible: boolean,
  startedAt: number,
  now = Date.now(),
  interval = ACTIVE_POLL_MS,
): number | false {
  return active && visible && now - startedAt < POLL_WINDOW_MS ? interval : false;
}

export function jobIsActive(job?: { status: string } | null): boolean {
  return job?.status === 'queued' || job?.status === 'running';
}
