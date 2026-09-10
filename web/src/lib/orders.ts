/** Delivery receipts and later broker observations are deliberately separate states. */
export const orderDeliveryLabels = {
  prepared: '의도 저장',
  dispatching: '전달 처리 중',
  acknowledged: '응답 확인',
  rejected: '거부 응답',
  ambiguous: '결과 미확정',
  aborted: '전달 전 중단',
} as const;
export const syntheticOrderCases = {
  accept: '승인 응답',
  reject: '거부 응답',
  response_lost: '응답 유실',
  before_send_failure: '전송 전 실패',
} as const;

import { capitalDecimal } from './capital';
import type { CapitalPlanResponse, FundingState } from './api/types.gen';

export function orderReservations(
  funding: FundingState,
  plan: CapitalPlanResponse,
  alternativeId: string,
) {
  if (
    funding.account_seq !== plan.record.snapshot.account_seq ||
    plan.record.request.mode === 'retrospective'
  )
    return [];
  return funding.reservations.filter(
    (reservation) =>
      reservation.status === 'active' &&
      reservation.account_seq === funding.account_seq &&
      reservation.plan_id === plan.id &&
      reservation.alternative_id === alternativeId &&
      reservation.mode === plan.record.request.mode,
  );
}
export function orderChangeFields(price: string, quantity: string) {
  const nextPrice = capitalDecimal(price, '정정 가격');
  const nextQuantity = quantity.trim() ? capitalDecimal(quantity, '정정 수량') : null;
  if (!/[1-9]/.test(nextPrice) || (nextQuantity !== null && !/[1-9]/.test(nextQuantity)))
    throw new Error('정정 가격과 입력한 수량은 0보다 커야 합니다.');
  return { price: nextPrice, quantity: nextQuantity };
}

import {
  listOrderIntents,
  getOrderIntent,
  createOrderIntent,
  modifyOrderIntent,
  cancelOrderIntent,
  abortOrderIntent,
  recoverOrderIntent,
  observeOrderIntent,
  simulateOrderOperation,
} from './api/sdk.gen';
import type {
  ErrorResponse,
  OrderIntentCreate,
  OrderModify,
  OrderCancel,
  OrderMutation,
  OrderObserve,
  OrderSimulate,
} from './api/types.gen';
export class OrderRequestError extends Error {
  constructor(
    public readonly code: string,
    public readonly status?: number,
  ) {
    const messages: Record<string, string> = {
      fractional_quantity_requires_us_market_sell:
        '현재 지정가 주문 의도는 정수 수량만 지원합니다. 자금 계획의 수량을 확인해 주세요.',
      integer_quantity_required: '정수 수량이 필요한 주문입니다.',
      invalid_decimal: '수량과 가격의 소수 표기를 확인해 주세요.',
      nonpositive_decimal: '주문 수량과 가격은 0보다 커야 합니다.',
      integer_krw_price_required: '국내 지정 가격은 정수 원 단위로 입력해 주세요.',
      unsupported_us_price_precision:
        '미국 지정 가격은 1달러 이상이면 소수 2자리, 미만이면 소수 4자리까지 지원합니다.',
      high_value_confirmation_required:
        '고액 주문 확인이 필요한 금액입니다. 현재 주문 의도 화면에서는 이 확인을 지원하지 않습니다.',
      local_maximum_order_amount: '현재 지원하는 주문 금액 한도를 초과했습니다.',
      kr_modify_quantity_required: '국내 정정 의도에는 수량을 입력해 주세요.',
      us_modify_quantity_forbidden: '미국 정정 의도에서는 가격만 변경할 수 있습니다.',
      invalid_symbol: '시장에 맞는 종목 식별자를 확인해 주세요.',
      limit_price_required: '지정가 주문에는 가격을 입력해야 합니다.',
      order_management_disabled: '이 작업실에서는 주문 의도 저장이 꺼져 있습니다.',
      order_store_unavailable:
        '주문 저장소에 연결하지 못했습니다. 같은 요청으로 결과를 다시 확인해 주세요.',
      order_conflict:
        '주문 상태나 배정 조건이 맞지 않습니다. 최신 상태, 연결된 배정, 지원 수량·가격을 확인해 주세요.',
      invalid_request: '선택한 계획·배정과 정정 입력을 확인해 주세요.',
      foreign_origin: '요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
      not_found: '요청한 주문 의도를 찾지 못했습니다.',
    };
    super(
      messages[code] ??
        ([400, 403, 409, 422].includes(status ?? 0)
          ? '요청이 거부되었습니다. 입력과 최신 주문 상태를 확인해 주세요.'
          : '주문 의도 처리 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.'),
    );
    this.name = 'OrderRequestError';
  }
}
export function definiteOrderError(error: unknown) {
  return (
    error instanceof OrderRequestError &&
    ([400, 403, 409, 422].includes(error.status ?? 0) ||
      [
        'order_management_disabled',
        'order_conflict',
        'invalid_request',
        'foreign_origin',
        'not_found',
      ].includes(error.code))
  );
}
export function orderRevision(value: number) {
  if (!Number.isSafeInteger(value) || value < 1) throw new OrderRequestError('order_conflict');
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
function unwrap<T>(result: { data?: T; error?: ErrorResponse; response?: Response }): T {
  if (result.error || result.data === undefined)
    throw new OrderRequestError(result.error?.error.code ?? 'unknown', result.response?.status);
  return result.data;
}
export async function fetchOrderIntents(signal?: AbortSignal) {
  return unwrap(await listOrderIntents({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchOrderIntent(id: string, signal?: AbortSignal) {
  return unwrap(await getOrderIntent({ ...options(signal), path: { id } }));
}
export async function createOrder(body: OrderIntentCreate) {
  return unwrap(await createOrderIntent({ ...options(), body }));
}
export async function modifyOrder(id: string, body: OrderModify) {
  return unwrap(await modifyOrderIntent({ ...options(), path: { id }, body }));
}
export async function cancelOrder(id: string, body: OrderCancel) {
  return unwrap(await cancelOrderIntent({ ...options(), path: { id }, body }));
}
export async function abortOrder(id: string, body: OrderMutation) {
  return unwrap(await abortOrderIntent({ ...options(), path: { id }, body }));
}
export async function recoverOrder(id: string, body: OrderMutation) {
  return unwrap(await recoverOrderIntent({ ...options(), path: { id }, body }));
}
export async function observeOrder(id: string, body: OrderObserve) {
  return unwrap(await observeOrderIntent({ ...options(), path: { id }, body }));
}
export async function simulateOrder(id: string, operationId: string, body: OrderSimulate) {
  return unwrap(
    await simulateOrderOperation({ ...options(), path: { id, operation_id: operationId }, body }),
  );
}
export const orderObservationLabels = {
  unobserved: '미관측',
  open: '미종결 주문 관측',
  partially_filled: '부분 체결 상태 관측',
  terminal: '종결 상태 관측',
  unresolved: '연결 확인 필요',
} as const;
export const orderOperationLabels = {
  create: '신규 주문 의도',
  modify: '정정 의도',
  cancel: '취소 의도',
} as const;
