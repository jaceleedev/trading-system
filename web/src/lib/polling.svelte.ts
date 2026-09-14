import { onMount } from 'svelte';
import { ACTIVE_POLL_MS, pollInterval } from './polling';

export function createPollingWindow() {
  let visible = $state(false);
  let startedAt = $state(Date.now());
  onMount(() => {
    const update = () => {
      const next = document.visibilityState === 'visible';
      if (next && !visible) startedAt = Date.now();
      visible = next;
    };
    update();
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  });
  return {
    restart() {
      startedAt = Date.now();
    },
    interval(active: boolean, interval = ACTIVE_POLL_MS) {
      return pollInterval(active, visible, startedAt, Date.now(), interval);
    },
  };
}
