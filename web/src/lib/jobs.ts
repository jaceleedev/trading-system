import {
  cancelJob as cancelJobRequest,
  getJob,
  jobServiceStatus,
  listJobs,
  submitJob,
} from './api/sdk.gen';
import type { ErrorResponse, JobSubmission } from './api/types.gen';

export type ResearchContextSubmission = Omit<JobSubmission, 'kind' | 'parameters'> & {
  kind: 'research-context';
  parameters: { snapshot_id: string | null; max_records: number };
};

const messages: Record<string, string> = {
  jobs_disabled: '이 작업실에서는 작업 실행이 꺼져 있습니다.',
  jobs_unavailable: '작업 저장소에 연결하지 못했습니다. 작업 상태를 다시 확인해 주세요.',
  job_conflict: '요청이 저장된 작업과 맞지 않습니다. 최신 작업 상태를 확인해 주세요.',
  invalid_request: '작업 요청의 종류나 예약 시각을 확인해 주세요.',
  not_found: '요청한 작업을 찾지 못했습니다. 목록을 다시 읽어 주세요.',
  foreign_origin: '작업 요청이 허용되지 않았습니다. 로컬 작업실 주소를 확인해 주세요.',
  internal_error: '작업 처리 결과를 확인하지 못했습니다. 같은 요청을 다시 확인해 주세요.',
};

export class JobRequestError extends Error {
  constructor(public readonly code: string) {
    super(messages[code] ?? '작업 처리 결과를 확인하지 못했습니다. 연결을 확인해 주세요.');
    this.name = 'JobRequestError';
  }
}

function options(signal?: AbortSignal) {
  return {
    baseUrl: window.location.origin,
    cache: 'no-store' as const,
    credentials: 'same-origin' as const,
    signal,
  };
}

function unwrap<T>(result: { data?: T; error?: ErrorResponse }): T {
  if (result.error || result.data === undefined) {
    throw new JobRequestError(result.error?.error?.code ?? 'unknown');
  }
  return result.data;
}

export async function fetchJobsStatus(signal?: AbortSignal) {
  return unwrap(await jobServiceStatus(options(signal)));
}

export async function fetchJobs(signal?: AbortSignal) {
  return unwrap(await listJobs({ ...options(signal), query: { limit: 50 } }));
}

export async function fetchJob(id: string, signal?: AbortSignal) {
  return unwrap(await getJob({ ...options(signal), path: { id } }));
}

export async function submitResearchContext(body: ResearchContextSubmission) {
  return unwrap(await submitJob({ ...options(), body }));
}

export async function cancelJob(id: string) {
  return unwrap(await cancelJobRequest({ ...options(), path: { id } }));
}
