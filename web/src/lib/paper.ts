import {
  listPaperBooks,
  getPaperBook,
  createPaperBook,
  submitPaperIntent,
  advancePaperBook,
  cancelPaperIntent,
} from './api/sdk.gen';
import type {
  ErrorResponse,
  PaperBookCreate,
  PaperSubmit,
  PaperAdvance,
  PaperCancel,
} from './api/types.gen';
import { capitalDecimal } from './capital';

export type PaperCashDraft = {
  currency: 'KRW' | 'USD';
  enabled: boolean;
  amount: string;
};
export type PaperProfileDraft = {
  slippage_bps: string;
  participation_bps: string;
  quantity_step: string;
};
export function paperCashRequest(cash: PaperCashDraft[]) {
  const selected = cash.filter((item) => item.enabled);
  if (!selected.length) throw new Error('모의 현금을 시작할 통화를 선택해 주세요.');
  return selected.map((item) => ({
    currency: item.currency,
    amount: capitalDecimal(item.amount, `${item.currency} 초기 모의 현금`),
  }));
}
function positive(value: string, label: string) {
  const text = capitalDecimal(value, label);
  if (!/[1-9]/.test(text)) throw new Error(`${label}은 0보다 커야 합니다.`);
  return text;
}
function belowBasisLimit(value: string, inclusive: boolean) {
  const [whole, fraction = ''] = value.split('.');
  const scale = 10n ** BigInt(fraction.length);
  const number = BigInt(whole) * scale + BigInt(fraction || '0');
  return inclusive ? number <= 10000n * scale : number < 10000n * scale;
}
export function paperProfileRequest(profile: PaperProfileDraft) {
  const slippage = capitalDecimal(profile.slippage_bps, '불리한 슬리피지');
  const participation = positive(profile.participation_bps, '봉 거래량 참여율');
  if (!belowBasisLimit(slippage, false)) throw new Error('슬리피지는 10,000 bp 미만이어야 합니다.');
  if (!belowBasisLimit(participation, true))
    throw new Error('봉 거래량 참여율은 10,000 bp 이하여야 합니다.');
  return {
    kind: 'next_observed_minute_close_v1' as const,
    slippage_bps: slippage,
    participation_bps: participation,
    quantity_step: positive(profile.quantity_step, '모의 체결 수량 단위'),
  };
}

export class PaperRequestError extends Error {
  constructor(public readonly code: string) {
    const messages: Record<string, string> = {
      paper_disabled: '이 작업실에서는 모의 원장 저장이 꺼져 있습니다.',
      paper_unavailable:
        '모의 원장 저장소에 연결하지 못했습니다. 같은 요청으로 처리 결과를 다시 확인해 주세요.',
      paper_conflict:
        '원장 상태나 선택한 계획·관측이 현재 조건과 맞지 않습니다. 최신 자료와 통화·수량·체결 가정을 확인해 주세요.',
      invalid_request: '모의 현금, 체결 가정, 선택 자료를 확인해 주세요.',
      foreign_origin: '이 화면의 모의 요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
      not_found: '요청한 모의 원장을 찾지 못했습니다.',
    };
    super(
      messages[code] ?? '모의 처리 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.',
    );
    this.name = 'PaperRequestError';
  }
}
export function definitePaperError(error: unknown) {
  return (
    error instanceof PaperRequestError &&
    ['paper_disabled', 'paper_conflict', 'invalid_request', 'foreign_origin', 'not_found'].includes(
      error.code,
    )
  );
}
export function paperRevision(value: number) {
  if (!Number.isSafeInteger(value) || value < 1) throw new PaperRequestError('paper_conflict');
  return value;
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
    throw new PaperRequestError(result.error?.error.code ?? 'unknown');
  return result.data;
}
export async function fetchPaperBooks(signal?: AbortSignal) {
  return unwrap(await listPaperBooks({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchPaperBook(id: string, signal?: AbortSignal) {
  return unwrap(await getPaperBook({ ...options(signal), path: { id } }));
}
export async function createPaper(body: PaperBookCreate) {
  return unwrap(await createPaperBook({ ...options(), body }));
}
export async function submitPaper(id: string, body: PaperSubmit) {
  return unwrap(await submitPaperIntent({ ...options(), path: { id }, body }));
}
export async function advancePaper(id: string, body: PaperAdvance) {
  return unwrap(await advancePaperBook({ ...options(), path: { id }, body }));
}
export async function cancelPaper(id: string, intentId: string, body: PaperCancel) {
  return unwrap(await cancelPaperIntent({ ...options(), path: { id, intent_id: intentId }, body }));
}
export const paperStatus = {
  pending: '미체결',
  partially_filled: '부분 체결',
  filled: '모의 완료',
  cancelled: '취소',
  held: '유지',
} as const;
export const paperEventLabels: Record<string, string> = {
  book_created: '원장 생성',
  request: '처리 접수',
  submitted: '대안 접수',
  simulated_fill: '모의 체결',
  unfilled: '미체결',
  cancelled: '취소',
  observation: '평가 관측',
  capture_receipt: '캡처 접수',
};
export const paperUnfilledReason: Record<string, string> = {
  no_observed_volume: '관측 거래량 없음',
  volume_or_cash_below_step: '거래량 또는 배정 현금이 수량 단위보다 적음',
  cost_exceeds_assigned_budget: '비용이 사전 배정 금액을 초과함',
};

/** Event payloads have multiple shapes. Only named scalar fields enter the presentation. */
export function paperEventData(
  event: { payload: { [key: string]: unknown } },
  key: string,
): string | null {
  const data = event.payload.data;
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const value = (data as Record<string, unknown>)[key];
  return typeof value === 'string' ? value : null;
}
