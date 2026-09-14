import type { InvestigationView, JobView } from './api/types.gen';

export const waitReasonLabels: Record<string, string> = {
  scheduled: '예약 시각 전',
  no_worker: '같은 작업실의 worker 없음',
  worker_observation_expired: 'worker 생존 관측 만료',
  capability_not_allowed: '필요 기능을 허용한 worker 없음',
  eligible_workers_busy: '실행 가능한 worker가 다른 작업 처리 중',
  awaiting_worker_claim: 'worker의 작업 인수 대기',
};

export const reviewReasonLabels: Record<string, string> = {
  review_deadline_reached: '재검토 시각 도래',
  revision_requested: '판단 수정 요청',
  review_judgment_unresolved: '재검토 판단 미해결',
};

export function investigationNeedsPolling(
  item: Pick<InvestigationView, 'status' | 'active_job_id'>,
  jobs?: Pick<JobView, 'id' | 'status'>[],
): boolean {
  if (item.status === 'paused' || !item.active_job_id) return false;
  const job = jobs?.find((job) => job.id === item.active_job_id);
  return !job || job.status === 'queued' || job.status === 'running';
}

export function elapsedText(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '미확인';
  if (seconds < 0) return '조회 기준 이후';
  if (seconds < 60) return `${Math.floor(seconds)}초`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}분`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}시간`;
  return `${Math.floor(seconds / 86400)}일`;
}
