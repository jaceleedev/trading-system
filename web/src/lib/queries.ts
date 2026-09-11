import { getContext, getResearch, health, listAccountSnapshots } from './api/sdk.gen';
import type { ErrorResponse } from './api/types.gen';

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
    const messages: Record<string, string> = {
      invalid_record: '저장 기록을 검증하지 못했습니다. 로컬 자료를 확인해 주세요.',
      not_found: '요청한 저장 기록을 찾을 수 없습니다.',
      foreign_origin:
        '이 화면에서의 자료 접근이 허용되지 않았습니다. 로컬 작업실 주소를 확인해 주세요.',
      internal_error: '저장 자료를 읽는 중 문제가 생겼습니다. 잠시 후 다시 읽어 주세요.',
      invalid_request: '요청 형식이 올바르지 않습니다. 선택한 기록을 확인해 주세요.',
    };
    throw new Error(
      messages[result.error?.error?.code ?? ''] ??
        '저장 자료를 읽지 못했습니다. 연결과 로컬 자료를 확인해 주세요.',
    );
  }
  return result.data;
}

export async function fetchHealth(signal?: AbortSignal) {
  return unwrap(await health(options(signal)));
}
export async function fetchSnapshots(signal?: AbortSignal) {
  return unwrap(await listAccountSnapshots(options(signal)));
}
export async function fetchContext(snapshotId: string, maxRecords: number, signal?: AbortSignal) {
  return unwrap(
    await getContext({
      ...options(signal),
      query: { snapshot_id: snapshotId || undefined, max_records: maxRecords },
    }),
  );
}
export async function fetchRecord(id: string, signal?: AbortSignal) {
  return unwrap(await getResearch({ ...options(signal), path: { id } }));
}
