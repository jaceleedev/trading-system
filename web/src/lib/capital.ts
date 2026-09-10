import {
  createCapitalPlan,
  getCapitalPlan,
  getFunding,
  listCapitalPlans,
  previewCapitalPlan,
  refreshFunding,
  releaseFundingReservation,
  reserveCapitalPlan,
} from './api/sdk.gen';
import type {
  CapitalPlanCreate,
  CapitalPlanRequest,
  CapitalPlanReserve,
  ErrorResponse,
  FundingRefresh,
  FundingState,
} from './api/types.gen';

const capitalErrors: Record<string, string> = {
  capital_disabled: '이 작업실에서는 계획 저장과 자금 배정이 꺼져 있습니다.',
  capital_unavailable:
    '계획 저장소에 연결하지 못했습니다. 같은 요청으로 처리 결과를 다시 확인해 주세요.',
  capital_conflict:
    '선택한 자료나 계획 예산·배정 상태가 현재 기록과 맞지 않습니다. 최신 상태를 다시 읽고 확인해 주세요.',
  invalid_request: '수량, 가격, 비용과 계획 예산을 확인해 주세요.',
  foreign_origin: '이 화면의 계획 요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
  not_found: '요청한 자금 계획을 찾지 못했습니다.',
};
export class CapitalRequestError extends Error {
  constructor(public readonly code: string) {
    super(
      capitalErrors[code] ??
        '계획 처리 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.',
    );
    this.name = 'CapitalRequestError';
  }
}
export function definiteCapitalError(error: unknown): boolean {
  return (
    error instanceof CapitalRequestError &&
    [
      'capital_disabled',
      'capital_conflict',
      'invalid_request',
      'foreign_origin',
      'not_found',
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
    throw new CapitalRequestError(result.error?.error.code ?? 'unknown');
  return result.data;
}
export async function fetchCapitalPlans(signal?: AbortSignal) {
  return unwrap(await listCapitalPlans({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchCapitalPlan(id: string, signal?: AbortSignal) {
  return unwrap(await getCapitalPlan({ ...options(signal), path: { id } }));
}
export async function previewCapital(body: CapitalPlanRequest) {
  return unwrap(await previewCapitalPlan({ ...options(), body }));
}
export async function saveCapital(body: CapitalPlanCreate) {
  return unwrap(await createCapitalPlan({ ...options(), body }));
}
export async function fetchFunding(accountSeq: string, snapshotId: string, signal?: AbortSignal) {
  return unwrap(
    await getFunding({
      ...options(signal),
      query: { account_seq: accountSeq, snapshot_id: snapshotId },
    }),
  );
}
export async function applyFunding(body: FundingRefresh) {
  return unwrap(await refreshFunding({ ...options(), body }));
}
export async function allocateCapital(id: string, body: CapitalPlanReserve) {
  return unwrap(await reserveCapitalPlan({ ...options(), path: { id }, body }));
}
export async function releaseCapital(id: string) {
  return unwrap(await releaseFundingReservation({ ...options(), path: { id } }));
}
export function poolRevisions(state: FundingState): Record<string, number> {
  const result: Record<string, number> = {};
  for (const [id, revision] of Object.entries(state.expected_pool_revisions)) {
    if (
      !/^[a-f0-9]{64}$/.test(id) ||
      typeof revision !== 'number' ||
      !Number.isSafeInteger(revision) ||
      revision < 0
    )
      throw new CapitalRequestError('capital_conflict');
    result[id] = revision;
  }
  return result;
}

export function capitalCondition(value: string): string {
  const parts = value.split(':');
  const reason = parts.pop()!;
  const conditions: Record<string, string> = {
    assumed_price_unknown: '가정 가격이 없어 금액을 확인할 수 없습니다.',
    local_reservations_unknown: '기존 계획 배정 상태가 미확인입니다.',
    spending_capacity_unknown: '계획에 사용할 수 있는 금액이 미확인입니다.',
    insufficient_spending_capacity: '필요 자금이 계획 여력을 초과합니다.',
    available_holding_unknown: '매도에 사용할 수 있는 보유 수량이 미확인입니다.',
    insufficient_holding: '매도 수량이 배정 가능한 보유 수량을 초과합니다.',
    hold_exceeds_observed_quantity: '유지 수량이 관측된 보유 수량을 초과합니다.',
    no_remaining_position: '변경 후 남는 보유 수량이 없습니다.',
    mixed_actions_require_execution_order:
      '같은 종목의 매수·매도 순서가 정해지지 않아 평균가가 미확인입니다.',
    existing_average_unknown: '기존 평균 매수가가 미확인입니다.',
  };
  return `${parts.length ? `${parts.join(' / ')} · ` : ''}${conditions[reason] ?? '이 항목은 추가 확인이 필요합니다.'}`;
}

/** Form values remain strings until the server performs exact decimal calculations. */
export const capitalActionLabels = {
  buy: '신규 매수',
  add: '추가 매수',
  hold: '유지',
  trim: '일부 매도',
  sell: '매도',
} as const;

export function capitalDecimal(value: string, label: string): string {
  const text = value.trim();
  if (text.length > 64 || !/^\d+(?:\.\d+)?$/.test(text))
    throw new Error(`${label}을 음수가 아닌 소수로 직접 입력해 주세요.`);
  return text;
}

export type CapitalAction = keyof typeof capitalActionLabels;
export type CapitalCurrency = 'KRW' | 'USD';
export type CapitalLegDraft = {
  rowId: string;
  action: CapitalAction;
  symbol: string;
  market: 'KR' | 'US';
  currency: CapitalCurrency;
  quantity: string;
  price: string;
  fee_bps: string;
  fixed_fee: string;
  tax_bps: string;
  rationale: string;
};
export type CapitalAlternativeDraft = {
  key: string;
  label: string;
  rationale: string;
  legs: CapitalLegDraft[];
};
export type CapitalFundingDraft = {
  currency: CapitalCurrency;
  enabled: boolean;
  limit_amount: string;
  reserve_amount: string;
};
export function capitalFundingRequest(funding: CapitalFundingDraft[]) {
  const selected = funding.filter((item) => item.enabled);
  if (!selected.length) throw new Error('계획에 사용할 통화와 예산을 입력해 주세요.');
  return selected.map((item) => ({
    currency: item.currency,
    limit_amount: capitalDecimal(item.limit_amount, `${item.currency} 계획 한도`),
    reserve_amount: capitalDecimal(item.reserve_amount, `${item.currency} 남겨둘 금액`),
  }));
}
export function newCapitalLeg(): CapitalLegDraft {
  return {
    rowId: crypto.randomUUID(),
    action: 'buy',
    symbol: '',
    market: 'KR',
    currency: 'KRW',
    quantity: '',
    price: '',
    fee_bps: '',
    fixed_fee: '',
    tax_bps: '',
    rationale: '',
  };
}
export function newCapitalAlternative(number: number): CapitalAlternativeDraft {
  return {
    key: crypto.randomUUID(),
    label: `대안 ${number}`,
    rationale: '',
    legs: [newCapitalLeg()],
  };
}
export function capitalFormRequest(
  snapshotId: string,
  sourceValue: string,
  mode: 'synthetic' | 'prospective' | 'retrospective',
  funding: CapitalFundingDraft[],
  alternatives: CapitalAlternativeDraft[],
) {
  const [kind, id] = sourceValue.split(':');
  if (!/^[a-f0-9]{64}$/.test(snapshotId)) throw new Error('계좌 관측을 명시적으로 선택해 주세요.');
  if (!['decision', 'investigation_output'].includes(kind) || !/^[a-f0-9]{64}$/.test(id ?? ''))
    throw new Error('자금 계획의 바탕이 되는 판단이나 완료한 조사를 선택해 주세요.');
  return {
    snapshot_id: snapshotId,
    source: { kind: kind as 'decision' | 'investigation_output', id },
    mode,
    funding: capitalFundingRequest(funding),
    alternatives: alternatives.map((alternative) => {
      if (!alternative.label.trim() || !alternative.rationale.trim())
        throw new Error('각 대안의 이름과 이유를 입력해 주세요.');
      return {
        key: alternative.key,
        label: alternative.label.trim(),
        rationale: alternative.rationale.trim(),
        legs: alternative.legs.map((leg) => {
          if (!/^[A-Za-z0-9][A-Za-z0-9.\-_]{0,31}$/.test(leg.symbol.trim()))
            throw new Error('종목 코드를 확인해 주세요.');
          if (!leg.rationale.trim())
            throw new Error('각 매매 또는 유지 항목의 이유를 입력해 주세요.');
          return {
            action: leg.action,
            symbol: leg.symbol.trim(),
            market: leg.market,
            currency: leg.currency,
            quantity: capitalDecimal(leg.quantity, '수량'),
            price: leg.price.trim() ? capitalDecimal(leg.price, '가정 가격') : null,
            fee_bps: capitalDecimal(leg.fee_bps, '수수료율'),
            fixed_fee: capitalDecimal(leg.fixed_fee, '고정 수수료'),
            tax_bps: capitalDecimal(leg.tax_bps, '세율'),
            rationale: leg.rationale.trim(),
          };
        }),
      };
    }),
  };
}

/** Match calculations to all current form inputs, including the explicitly selected account. */
export function capitalInputKey(value: unknown): string {
  return JSON.stringify(value);
}
