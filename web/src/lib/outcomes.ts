import { formatDecimal } from './format';
import {
  createOutcomeReport as createReport,
  getOutcomeReport,
  listOutcomeReports,
} from './api/sdk.gen';
import type { ErrorResponse, OutcomeCreate } from './api/types.gen';

/** The API returns a ratio, so retain its exact digits instead of rounding to percent. */
export function outcomeRatio(value: string | null | undefined) {
  return formatDecimal(value);
}

export function outcomeTime(value: string, label: string): string | null {
  if (!value.trim()) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label}을 확인해 주세요.`);
  return date.toISOString();
}

export function outcomeSelection(ids: string[], label: string) {
  const unique = [...new Set(ids)];
  if (unique.length > 4) throw new Error(`${label}은 최대 4개까지 선택할 수 있습니다.`);
  return unique;
}

const outcomeNotes: Record<string, string> = {
  broker_cumulative_deltas_are_not_individual_fills:
    '브로커 누적 변화는 개별 체결 내역이 아닙니다.',
  buying_power_is_not_cash_balance: '매수 가능 금액은 현금 잔고가 아닙니다.',
  external_cash_flows_and_fx_attribution_unavailable:
    '외부 현금 흐름과 환율 효과는 확인할 수 없습니다.',
  order_acknowledgement_is_not_a_fill: '주문 응답 확인은 체결을 의미하지 않습니다.',
  broker_source_intervals_differ_from_requested_paper_window:
    '브로커 대조의 원본 관측 기간이 요청한 비교 기간과 다릅니다.',
  paper_books_may_reuse_the_same_hypothetical_capital_and_are_not_summed:
    '여러 모의 원장이 같은 가상 자금을 반복해서 사용할 수 있어 원장별 결과를 합산하지 않습니다.',
  paper_values_are_synthetic_execution_results_not_actual_profit:
    '모의 금액은 체결 가정에 따른 계산 결과이며 실제 수익이 아닙니다.',
  historical_cost_realized_pnl_is_not_causal_ai_attribution:
    '과거 취득원가 기준 실현 손익을 이번 AI 판단의 성과로 귀속하지 않습니다.',
  currency_totals_are_not_converted_or_added_without_fx_evidence:
    '환율 근거 없이 서로 다른 통화의 금액을 환산하거나 더하지 않습니다.',
  fees_taxes_and_slippage_are_already_reflected_in_paper_equity:
    '수수료·세금·슬리피지는 모의 평가액에 이미 반영되어 있습니다. 다시 차감하지 않습니다.',
  book_returns_are_not_allocated_between_multiple_decision_methods:
    '한 원장의 결과를 여러 판단 방식 사이에 나누어 귀속하지 않습니다.',
  requested_model_and_local_hashes_do_not_attest_runtime_model_identity:
    '요청 모델 이름과 로컬 해시는 실제 실행 모델의 정체를 인증하지 않습니다.',
  stored_system_recording_times_do_not_prove_transaction_visibility_times:
    '시스템 기록 시각은 그 자료가 당시 거래에 이용 가능했음을 증명하지 않습니다.',
  matching_seed_and_profile_do_not_establish_statistical_comparability:
    '초기 설정과 체결 가정이 같아도 통계적으로 동등한 비교 조건이 보장되지는 않습니다.',
};
export function outcomeNote(value: string) {
  return outcomeNotes[value] ?? value;
}

export class OutcomeRequestError extends Error {
  constructor(
    public readonly code: string,
    public readonly status?: number,
  ) {
    const messages: Record<string, string> = {
      outcomes_disabled:
        '이 작업실에서는 새 결과 계산이 꺼져 있습니다. 저장 보고서는 계속 조회할 수 있습니다.',
      outcomes_unavailable:
        '결과 자료 저장소에 연결하지 못했습니다. 같은 계산 요청으로 결과를 다시 확인해 주세요.',
      outcome_conflict:
        '선택 자료와 기간을 비교할 수 없습니다. 원장 생성 이후의 시작 시각, 자료 모드와 기록 범위를 확인해 주세요.',
      invalid_request: '비교할 자료와 시작·종료 시각을 확인해 주세요.',
      not_found: '요청한 결과 보고서를 찾지 못했습니다.',
      foreign_origin: '이 화면의 요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
    };
    super(messages[code] ?? '결과를 확인하지 못했습니다. 원래 계산 요청으로 다시 확인해 주세요.');
    this.name = 'OutcomeRequestError';
  }
}
export function definiteOutcomeError(error: unknown) {
  return error instanceof OutcomeRequestError && [400, 403, 409, 422].includes(error.status ?? 0);
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
    throw new OutcomeRequestError(result.error?.error.code ?? 'unknown', result.response?.status);
  return result.data;
}
export async function fetchOutcomes(signal?: AbortSignal) {
  return unwrap(await listOutcomeReports({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchOutcome(id: string, signal?: AbortSignal) {
  return unwrap(await getOutcomeReport({ ...options(signal), path: { id } }));
}
export async function createOutcomeReport(body: OutcomeCreate) {
  return unwrap(await createReport({ ...options(), body }));
}
