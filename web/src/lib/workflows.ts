/** UI names preserve the separation between local preparation and broker observation. */
export const workflowStatusLabels = {
  active: '진행 중',
  paused: '일시 정지',
  attention: '확인 필요',
  completed: '대조 완료',
} as const;
export const workflowStepLabels: Record<string, string> = {
  funding_refresh: '저장 예산 적용',
  capital_plan: '자금 계획 계산',
  reservation: '대안에 자금 배정',
  order_intent: '비활성 주문 의도 저장',
  order_observation: '브로커 관측 연결',
  observe: '브로커 관측 연결',
  observation: '브로커 관측 연결',
  reconcile: '후속 자료 대조',
  reconciliation: '후속 자료 대조',
};
export const workflowStepStateLabels = {
  prepared: '진행 대기',
  running: '처리 중',
  succeeded: '결과 저장',
  needs_check: '결과 확인 필요',
} as const;
export function workflowRevision(value: number) {
  if (!Number.isSafeInteger(value) || value < 1)
    throw new Error('워크플로의 최신 버전을 다시 확인해 주세요.');
  return value;
}
/** Only explicitly named scalar fields are shown from extensible step metadata. */
export function workflowText(
  value: Record<string, unknown> | null | undefined,
  key: string,
): string | null {
  const field = value?.[key];
  return typeof field === 'string' ? field : null;
}

import {
  getWorkflowProposal,
  listWorkflows,
  getWorkflow,
  createWorkflow,
  advanceWorkflow,
  pauseWorkflow,
  resumeWorkflow,
  recoverWorkflow,
  observeWorkflow,
  reconcileWorkflow,
} from './api/sdk.gen';
import type {
  ErrorResponse,
  WorkflowCreate,
  WorkflowMutation,
  WorkflowObserve,
  WorkflowReconcile,
} from './api/types.gen';
export class WorkflowRequestError extends Error {
  constructor(
    public readonly code: string,
    public readonly status?: number,
  ) {
    const messages: Record<string, string> = {
      workflows_disabled: '이 작업실에서는 AI 운용 흐름 저장이 꺼져 있습니다.',
      workflows_unavailable:
        '운용 기록 저장소에 연결하지 못했습니다. 같은 요청으로 처리 결과를 다시 확인해 주세요.',
      workflow_conflict:
        '고정된 조사 결과·예산·계좌 또는 현재 단계가 맞지 않습니다. 최신 자료를 다시 읽고 확인해 주세요.',
      invalid_request: '조사 버전, 대안과 대조 자료를 확인해 주세요.',
      foreign_origin: '이 화면의 요청이 허용되지 않았습니다. 작업실 주소를 확인해 주세요.',
      not_found: '요청한 운용 흐름을 찾지 못했습니다.',
    };
    super(
      messages[code] ??
        ([400, 403, 409, 422].includes(status ?? 0)
          ? '요청이 거부되었습니다. 입력과 최신 운용 상태를 확인해 주세요.'
          : '처리 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인해 주세요.'),
    );
    this.name = 'WorkflowRequestError';
  }
}
export function definiteWorkflowError(error: unknown) {
  return error instanceof WorkflowRequestError && [400, 403, 409, 422].includes(error.status ?? 0);
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
    throw new WorkflowRequestError(result.error?.error.code ?? 'unknown', result.response?.status);
  return result.data;
}
export async function fetchWorkflowProposal(id: string, signal?: AbortSignal) {
  return unwrap(await getWorkflowProposal({ ...options(signal), path: { id } }));
}
export async function fetchWorkflows(signal?: AbortSignal) {
  return unwrap(await listWorkflows({ ...options(signal), query: { limit: 50 } }));
}
export async function fetchWorkflow(id: string, signal?: AbortSignal) {
  return unwrap(await getWorkflow({ ...options(signal), path: { id } }));
}
export async function createFlow(body: WorkflowCreate) {
  return unwrap(await createWorkflow({ ...options(), body }));
}
export async function controlFlow(
  id: string,
  action: 'advance' | 'pause' | 'resume' | 'recover',
  body: WorkflowMutation,
) {
  const controls = {
    advance: advanceWorkflow,
    pause: pauseWorkflow,
    resume: resumeWorkflow,
    recover: recoverWorkflow,
  };
  return unwrap(await controls[action]({ ...options(), path: { id }, body }));
}
export async function observeFlow(id: string, body: WorkflowObserve) {
  return unwrap(await observeWorkflow({ ...options(), path: { id }, body }));
}
export async function reconcileFlow(id: string, body: WorkflowReconcile) {
  return unwrap(await reconcileWorkflow({ ...options(), path: { id }, body }));
}
export function workflowObject(
  value: Record<string, unknown> | null | undefined,
  key: string,
): Record<string, unknown> | null {
  const field = value?.[key];
  return field !== null && typeof field === 'object' && !Array.isArray(field)
    ? (field as Record<string, unknown>)
    : null;
}
export function workflowMissingLabel(field: string) {
  const names: Record<string, string> = {
    quantity: '수량',
    price: '가격',
    fee_bps: '수수료율',
    fixed_fee: '고정 수수료',
    tax_bps: '세율',
  };
  const match = /^legs\[(\d+)\]\.(\w+)$/.exec(field);
  return match ? `${Number(match[1]) + 1}번 항목 · ${names[match[2]] ?? match[2]}` : field;
}
export const workflowReferenceLabels: Record<string, string> = {
  investigation_id: '조사',
  input_id: '조사 입력',
  output_id: 'AI 결과',
  run_id: '실행 기록',
  snapshot_id: '계좌 관측',
  plan_id: '자금 계획',
  alternative_id: '대안',
  reservation_id: '배정',
  intent_id: '주문 의도',
  scan_id: '브로커 관측',
  before_snapshot_id: '이전 계좌 관측',
  after_snapshot_id: '이후 계좌 관측',
  before_scan_id: '이전 브로커 관측',
  after_scan_id: '이후 브로커 관측',
  reconciliation_id: '대조 보고서',
};
