import {
  listBrokerScans,
  getBrokerScan,
  listReconciliations,
  getReconciliation,
  previewReconciliation,
  saveReconciliation,
  submitJob,
} from './api/sdk.gen';
import type {
  BrokerScanRequest,
  ErrorResponse,
  JobSubmission,
  ReconciliationRequest,
} from './api/types.gen';
export type BrokerScanDraft = {
  fromDate: string;
  toDate: string;
  symbol: string;
  maxPages: string;
  pageSize: string;
  detailOrderIds: string;
};
function date(value: string, label: string) {
  if (!value.trim()) return null;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000-'))
    throw new Error(`${label}을 YYYY-MM-DD 형식으로 입력해 주세요.`);
  const parsed = new Date(`${value}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value)
    throw new Error(`${label}을 실제 달력 날짜로 입력해 주세요.`);
  return value;
}
function boundedInteger(value: string, maximum: number, label: string) {
  if (!/^\d+$/.test(value) || value.length > 3)
    throw new Error(`${label}을 1~${maximum} 사이 정수로 입력해 주세요.`);
  const result = Number(value);
  if (result < 1 || result > maximum)
    throw new Error(`${label}을 1~${maximum} 사이 정수로 입력해 주세요.`);
  return result;
}
export function brokerScanParameters(accountSeq: string, draft: BrokerScanDraft) {
  if (!/^[1-9][0-9]{0,18}$/.test(accountSeq) || BigInt(accountSeq) > 9223372036854775807n)
    throw new Error('수집할 계좌 관측을 명시적으로 선택해 주세요.');
  const from = date(draft.fromDate, '주문 생성 시작일');
  const to = date(draft.toDate, '주문 생성 종료일');
  if (from && to && from > to) throw new Error('주문 생성 종료일은 시작일 이후여야 합니다.');
  const ids = [
    ...new Set(
      draft.detailOrderIds
        .split(/\r?\n/)
        .map((id) => id.trim())
        .filter(Boolean),
    ),
  ];
  if (ids.length > 20) throw new Error('개별 조회할 주문 ID는 최대 20개입니다.');
  if (ids.some((id) => id.length > 512 || /[\s\x00-\x1f\x7f]/.test(id)))
    throw new Error('주문 ID는 한 줄에 하나씩, 공백 없이 512자 이내로 입력해 주세요.');
  const symbol = draft.symbol.trim();
  if (symbol && !/^[A-Za-z0-9.\-]{1,32}$/.test(symbol))
    throw new Error('종목은 영문·숫자·점·하이픈 32자 이내로 입력해 주세요.');
  return {
    account_seq: accountSeq,
    mode: 'prospective' as const,
    from_date: from,
    to_date: to,
    symbol: symbol || null,
    max_pages: boundedInteger(draft.maxPages, 10, '최대 페이지 수'),
    page_size: boundedInteger(draft.pageSize, 100, '페이지당 주문 수'),
    detail_order_ids: ids,
  };
}

export class BrokerRequestError extends Error {
  constructor(public readonly code: string) {
    const messages: Record<string, string> = {
      broker_conflict:
        '계좌·스캔 자료와 대조 조건을 검증하지 못했습니다. 선택한 자료와 시각을 확인해 주세요.',
      reconciliation_disabled:
        '이 작업실에서는 대조 결과 저장이 꺼져 있습니다. 계산과 조회는 계속 사용할 수 있습니다.',
      invalid_request: '대조 자료, 조회 범위와 입력 형식을 확인해 주세요.',
      foreign_origin: '이 화면의 요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
      jobs_disabled: '이 작업실에서는 수집 작업 접수가 꺼져 있습니다.',
      job_conflict: '이 요청이 저장된 작업과 맞지 않습니다. 최신 상태를 확인해 주세요.',
      jobs_unavailable: '작업 저장소에 연결하지 못했습니다. 같은 요청으로 다시 확인해 주세요.',
    };
    super(
      messages[code] ??
        '브로커 자료 처리 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.',
    );
    this.name = 'BrokerRequestError';
  }
}
export function definiteBrokerError(error: unknown) {
  return (
    error instanceof BrokerRequestError &&
    [
      'broker_conflict',
      'reconciliation_disabled',
      'invalid_request',
      'foreign_origin',
      'jobs_disabled',
      'job_conflict',
    ].includes(error.code)
  );
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
    throw new BrokerRequestError(result.error?.error.code ?? 'unknown');
  return result.data;
}
export type BrokerSubmission = Omit<JobSubmission, 'kind' | 'parameters'> & {
  kind: 'broker-sync';
  parameters: BrokerScanRequest;
};
export async function enqueueBrokerScan(body: BrokerSubmission) {
  return unwrap(await submitJob({ ...options(), body }));
}
export async function fetchBrokerScans(accountSeq?: string, signal?: AbortSignal) {
  return unwrap(
    await listBrokerScans({ ...options(signal), query: { account_seq: accountSeq, limit: 50 } }),
  );
}
export async function fetchBrokerScan(id: string, signal?: AbortSignal) {
  return unwrap(await getBrokerScan({ ...options(signal), path: { id } }));
}
export async function fetchReconciliations(signal?: AbortSignal) {
  return unwrap(await listReconciliations({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchReconciliation(id: string, signal?: AbortSignal) {
  return unwrap(await getReconciliation({ ...options(signal), path: { id } }));
}
export async function calculateReconciliation(body: ReconciliationRequest) {
  return unwrap(await previewReconciliation({ ...options(), body }));
}
export async function storeReconciliation(body: ReconciliationRequest) {
  return unwrap(await saveReconciliation({ ...options(), body }));
}
export const brokerOrderStatus: Record<string, string> = {
  PENDING: '대기',
  PARTIAL_FILLED: '부분 체결 보고',
  FILLED: '전량 체결 보고',
  CANCELED: '취소 보고',
  REJECTED: '거부 보고',
  REPLACED: '정정 보고',
  PENDING_CANCEL: '취소 대기',
  PENDING_REPLACE: '정정 대기',
  CANCEL_REJECTED: '취소 거부',
  REPLACE_REJECTED: '정정 거부',
};
export const reconciliationLabels: Record<string, string> = {
  unchanged: '변화 없음',
  baseline_only: '이전 비교값 없음',
  absent_from_selected_scope: '이후 선택 범위에서 미관측',
  identity_conflict: '주문 식별 충돌',
  observation_conflict: '동시 관측 충돌',
  temporal_conflict: '관측 시각 충돌',
  cumulative_increase: '누적 보고 수량 증가',
  cumulative_regression: '누적 보고 수량 감소',
  financial_revision: '누적 금액·비용 정정',
  execution_information_changed: '체결 보고 정보 변경',
  status_only: '상태 변경',
  order_terms_changed: '주문 조건 변경',
  metadata_changed: '기타 보고 정보 변경',
  quantity_changed: '보유 수량 변화',
  appeared: '이후 새로 관측',
  disappeared: '이후 보유 목록에서 미관측',
  currency_conflict: '통화 충돌',
};
export const brokerStopReason: Record<string, string> = {
  page_limit: '설정한 페이지 상한 도달',
  cursor_cycle: '페이지 커서 반복',
  request_failed: '요청 실패',
  ambiguous_pages: '페이지 연결 불명확',
};
