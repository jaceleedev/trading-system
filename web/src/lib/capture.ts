import {
  captureOptions,
  getObservationCaptureResult,
  recoverObservationCapture,
  submitObservationCapture,
} from './api/sdk.gen';
import type { ErrorResponse, JobResponse, CaptureSubmission } from './api/types.gen';

export type CaptureOptions = NonNullable<Awaited<ReturnType<typeof captureOptions>>['data']>;
export type CaptureResult = NonNullable<
  Awaited<ReturnType<typeof getObservationCaptureResult>>['data']
>;
export type CaptureBody = CaptureSubmission;
export type CaptureEndpoint = CaptureOptions['market_endpoints'][number];
export type CaptureReceipt = {
  version: 1;
  workspaceKey: string;
  body: CaptureBody;
  contextSnapshotId: string;
  contextAccountSeq: string | null;
  jobId: string | null;
};

export class CaptureError extends Error {
  constructor(public readonly code: string) {
    super(
      code === 'not_found'
        ? '원래 요청으로 접수된 작업을 찾지 못했습니다. 같은 입력으로 다시 접수할 수 있습니다.'
        : code === 'invalid_request'
          ? '수집 대상과 허용 endpoint, 조회 범위 및 페이지 한도를 확인해 주세요.'
          : code === 'job_conflict'
            ? '저장된 요청과 입력이 다릅니다. 이 요청의 자동 접수는 중단했습니다.'
            : code === 'invalid_record'
              ? '완료 자료를 원래 수집 요청과 대조하지 못했습니다. 결과 연결을 중단했습니다.'
              : code === 'jobs_disabled'
                ? '이 작업실에서는 새 수집 접수가 꺼져 있습니다.'
                : '수집 요청의 결과를 확인하지 못했습니다. 원래 요청으로 상태를 확인해 주세요.',
    );
  }
}
function options(signal?: AbortSignal) {
  return {
    baseUrl: window.location.origin,
    credentials: 'same-origin' as const,
    cache: 'no-store' as const,
    signal,
  };
}
function unwrap<T>(result: { data?: T; error?: ErrorResponse }): T {
  if (result.error || result.data === undefined)
    throw new CaptureError(result.error?.error.code ?? 'unknown');
  return result.data;
}
export async function fetchCaptureOptions(signal?: AbortSignal) {
  return unwrap(await captureOptions(options(signal)));
}
export async function postCapture(body: CaptureBody): Promise<JobResponse> {
  return unwrap(await submitObservationCapture({ ...options(), body }));
}
export async function recoverCapture(
  body: CaptureBody,
  signal?: AbortSignal,
): Promise<JobResponse> {
  return unwrap(await recoverObservationCapture({ ...options(signal), body }));
}
export async function fetchCaptureResult(id: string, signal?: AbortSignal) {
  return unwrap(await getObservationCaptureResult({ ...options(signal), path: { id } }));
}

export function marketParameters(
  endpoint: CaptureEndpoint,
  fields: Record<string, string>,
  pageText: string,
) {
  if (!/^\d+$/.test(pageText) || Number(pageText) < 1 || Number(pageText) > endpoint.max_pages) {
    throw new Error(`최대 수집 페이지는 1~${endpoint.max_pages} 범위에서 선택해 주세요.`);
  }
  const query: Record<string, string | number | boolean> = {};
  for (const field of endpoint.query_fields) {
    const value = fields[field.name] ?? '';
    if (!value) {
      if (field.required) throw new Error(`${field.name} 값을 입력해 주세요.`);
      continue;
    }
    if (field.type === 'integer') {
      const number = Number(value);
      if (
        !/^-?\d+$/.test(value) ||
        !Number.isSafeInteger(number) ||
        (field.minimum !== null && field.minimum !== undefined && number < field.minimum) ||
        (field.maximum !== null && field.maximum !== undefined && number > field.maximum)
      ) {
        throw new Error(`${field.name} 조회 범위를 확인해 주세요.`);
      }
      query[field.name] = number;
    } else if (field.type === 'boolean') {
      if (value !== 'true' && value !== 'false')
        throw new Error(`${field.name} 값을 선택해 주세요.`);
      query[field.name] = value === 'true';
    } else {
      if (
        field.format === 'date-time' &&
        (!/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value)))
      ) {
        throw new Error(`${field.name}에 시간대를 포함한 시각을 입력해 주세요.`);
      }
      if (
        field.format === 'date' &&
        (!/^\d{4}-\d{2}-\d{2}$/.test(value) ||
          !Number.isFinite(Date.parse(`${value}T00:00:00Z`)) ||
          new Date(`${value}T00:00:00Z`).toISOString().slice(0, 10) !== value)
      ) {
        throw new Error(`${field.name} 날짜를 확인해 주세요.`);
      }
      query[field.name] = value;
    }
    if (field.enum_values?.length && !field.enum_values.includes(String(query[field.name])))
      throw new Error(`${field.name} 허용 값을 선택해 주세요.`);
    if (field.pattern && !new RegExp(`^(?:${field.pattern})$`).test(value))
      throw new Error(`${field.name} 형식을 확인해 주세요.`);
  }
  return { endpoint: endpoint.alias, query, pages: Number(pageText) };
}

export function receiptStorageKey(workspaceKey: string) {
  return `trading-observation-capture-v1:${workspaceKey}`;
}
/** A stored receipt is only a recovery hint; the server verifies its request and artifacts. */
export function readCaptureReceipt(
  raw: string | null,
  workspaceKey: string,
): CaptureReceipt | null {
  if (!raw) return null;
  try {
    const value = JSON.parse(raw);
    if (
      value.version !== 1 ||
      value.workspaceKey !== workspaceKey ||
      typeof value.contextSnapshotId !== 'string' ||
      (value.contextAccountSeq !== null && typeof value.contextAccountSeq !== 'string') ||
      (value.jobId !== null && typeof value.jobId !== 'string') ||
      !value.body ||
      !['account-sync', 'market-capture'].includes(value.body.kind) ||
      typeof value.body.request_key !== 'string' ||
      !value.body.request_key ||
      !value.body.parameters ||
      typeof value.body.parameters !== 'object' ||
      Array.isArray(value.body.parameters)
    )
      return null;
    return value;
  } catch {
    return null;
  }
}

export function captureMatchesContext(receipt: CaptureReceipt, accountSeq: string | null) {
  const expected =
    receipt.body.kind === 'account-sync'
      ? receipt.body.parameters.account_seq
      : receipt.contextAccountSeq;
  return expected === accountSeq;
}
