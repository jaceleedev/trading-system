import {
  createInvestigation,
  getInvestigation,
  listInvestigations,
  pauseInvestigation,
  reviseInvestigation,
} from './api/sdk.gen';
import type {
  ErrorResponse,
  InvestigationCreate,
  InvestigationRevise,
  InvestigationView,
  JobView,
} from './api/types.gen';

const errorMessages: Record<string, string> = {
  investigations_disabled: '이 작업실에서는 AI 조사 실행이 꺼져 있습니다.',
  investigations_unavailable:
    '조사 저장소에 연결하지 못했습니다. 연결을 확인한 뒤 다시 읽어 주세요.',
  investigation_conflict:
    '조사 버전이나 선택한 자료가 현재 기록과 맞지 않습니다. 최신 조사를 다시 읽어 주세요.',
  invalid_request: '조사 목적과 선택한 입력 자료를 확인해 주세요.',
  foreign_origin: '이 화면의 조사 요청이 허용되지 않았습니다. 로컬 작업실 주소를 확인해 주세요.',
  not_found: '요청한 조사를 찾지 못했습니다. 목록을 다시 읽어 주세요.',
};

export class InvestigationRequestError extends Error {
  constructor(public readonly code: string) {
    super(
      errorMessages[code] ??
        '조사 처리 결과를 확인하지 못했습니다. 같은 요청을 다시 확인해 주세요.',
    );
    this.name = 'InvestigationRequestError';
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
  if (result.error || result.data === undefined)
    throw new InvestigationRequestError(result.error?.error.code ?? 'unknown');
  return result.data;
}
export async function fetchInvestigations(signal?: AbortSignal) {
  return unwrap(await listInvestigations({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchInvestigation(id: string, signal?: AbortSignal) {
  return unwrap(await getInvestigation({ ...options(signal), path: { id } }));
}
export async function submitInvestigation(body: InvestigationCreate) {
  return unwrap(await createInvestigation({ ...options(), body }));
}
export async function reviewInvestigation(id: string, body: InvestigationRevise) {
  return unwrap(await reviseInvestigation({ ...options(), path: { id }, body }));
}
export async function stopInvestigation(id: string, expectedRevision: number) {
  return unwrap(
    await pauseInvestigation({
      ...options(),
      path: { id },
      body: { expected_revision: expectedRevision },
    }),
  );
}
export function isDefiniteInvestigationError(error: unknown): boolean {
  return (
    error instanceof InvestigationRequestError &&
    [
      'investigations_disabled',
      'investigation_conflict',
      'invalid_request',
      'foreign_origin',
      'not_found',
    ].includes(error.code)
  );
}
export function investigationState(
  item: Pick<
    InvestigationView,
    'status' | 'active_job_id' | 'latest_completed_revision' | 'current_revision'
  >,
  job?: Pick<JobView, 'id' | 'status'> | null,
): string {
  if (item.status === 'paused') return '일시 정지';
  if (job?.id === item.active_job_id) {
    const labels = {
      queued: '대기',
      running: '조사 중',
      succeeded: '작업 완료',
      failed: '작업 실패',
      cancelled: '작업 취소',
    };
    return labels[job.status];
  }
  if (item.active_job_id) return '작업 상태 확인 필요';
  if (item.latest_completed_revision === item.current_revision) return '결과 저장 완료';
  return '현재 결과 미확인';
}
/** Show only expected primitive metadata, never stringify unknown execution objects. */
export function executionText(execution: Record<string, unknown>, key: string): string {
  const value = execution[key];
  return typeof value === 'string' || typeof value === 'number' ? String(value) : '미확인';
}

/** Optional caller-selected symbols; never infer a market or a portfolio position. */
export function parseInvestigationSymbols(value: string): string[] {
  const symbols = [...new Set(value.split(/[\s,]+/).filter(Boolean))];
  if (
    symbols.length > 100 ||
    symbols.some((symbol) => !/^[A-Za-z0-9][A-Za-z0-9.\-_]{0,31}$/.test(symbol))
  ) {
    throw new Error('관심 종목은 영문·숫자로 시작하고 최대 100개를 쉼표로 구분해 주세요.');
  }
  return symbols;
}
