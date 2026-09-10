import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  InvestigationView,
  WorkflowProposal,
  WorkflowView,
  WorkflowStep,
  WorkflowCreate,
  WorkflowMutation,
  WorkflowObserve,
  WorkflowReconcile,
  CapitalPlanResponse,
  BrokerScanSummary,
} from '../src/lib/api/types.gen';
const now = '2026-09-10T09:00:00Z';
const sourceId = '25000000-0000-4000-8000-000000000001';
const otherSourceId = '25000000-0000-4000-8000-000000000002';
const flowId = '25000000-0000-4000-8000-000000000003';
const otherFlowId = '25000000-0000-4000-8000-000000000004';
const intentId = '25000000-0000-4000-8000-000000000005';
const reservationId = '25000000-0000-4000-8000-000000000006';
const planId = 'a'.repeat(64),
  scanId = 'b'.repeat(64),
  otherScanId = 'c'.repeat(64),
  reportId = 'd'.repeat(64);
const stages = ['funding_refresh', 'capital_plan', 'reservation', 'order_intent'] as const;
const panel = (page: Page) => page.getByRole('region', { name: 'AI 운용 흐름', exact: true });
const detail = (page: Page) => panel(page).getByRole('region', { name: '선택 운용 흐름' });
async function fixture(request: APIRequestContext) {
  const accounts = (await (
    await request.get('/api/v1/account-snapshots')
  ).json()) as AccountSnapshotsResponse;
  const first = accounts.items.find((item) => item.account_seq === '101')!;
  const second = accounts.items.find((item) => item.account_seq === '202')!;
  const context = (await (
    await request.get(`/api/v1/context?snapshot_id=${first.id}`)
  ).json()) as InvestmentContext;
  return { first, second, context };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;
function investigation(data: Fixture, id = sourceId): InvestigationView {
  return {
    id,
    workspace_key: 'f'.repeat(64),
    request_key: id,
    status: 'active',
    current_revision: 1,
    context_input: {
      input_id: 'e'.repeat(64),
      purpose: id === sourceId ? '합성 운용 제안' : '다른 합성 제안',
      snapshot_id: data.first.id,
      capture_ids: [],
      evidence_ids: [],
      symbols: ['SYNTH'],
      as_of: now,
      mode: 'synthetic',
    },
    active_job_id: null,
    latest_completed_revision: 1,
    latest_result: { output_id: 'f'.repeat(64), run_id: 'e'.repeat(64) },
    next_review_at: null,
    event_conditions: [],
    created_at: now,
    updated_at: now,
    revisions: [],
    omitted_revision_count: 0,
  };
}
function proposal(data: Fixture, id = sourceId): WorkflowProposal {
  return {
    investigation_id: id,
    investigation_revision: 1,
    input_id: 'e'.repeat(64),
    output_id: 'f'.repeat(64),
    run_id: '1'.repeat(64),
    snapshot_id: data.first.id,
    account_seq: '101',
    mode: 'synthetic',
    orders_enabled: false,
    capital_proposal: {
      snapshot_id: data.first.id,
      alternatives: [
        {
          key: 'buy-synth',
          label: id === sourceId ? 'SYNTH 분할 검토' : '다른 제안 대안',
          rationale: '합성 근거로 비교한 전체 묶음',
          legs: [
            {
              action: 'buy',
              symbol: 'SYNTH',
              market: 'US',
              currency: 'USD',
              quantity: '2',
              price: '10.01',
              fee_bps: '0',
              fixed_fee: '0.100000000000001',
              tax_bps: '0',
              rationale: '합성 비교',
              sizing_rationale: '여러 대안의 수량 비교',
              price_rationale: '저장 관측 가격 가정',
              cost_rationale: '원문 비용은 가정임을 기록',
              evidence_ids: [],
              capture_ids: [scanId],
            },
          ],
        },
      ],
    },
    completeness: { complete_alternative_keys: ['buy-synth'], incomplete_alternatives: [] },
    capital_context: {
      status: 'available',
      account_seq: '101',
      mode: 'synthetic',
      funding: [{ currency: 'USD', limit_amount: '1000.000000000000001', reserve_amount: '100' }],
      expected_pool_revisions: {},
      pools: [
        {
          id: '2'.repeat(64),
          kind: 'cash',
          currency: 'USD',
          market: null,
          symbol: null,
          snapshot_id: data.first.id,
          observed_at: now,
          mode: 'synthetic',
          revision: 1,
          capacity: '900.000000000000001',
          reserved: '0',
          available: '900.000000000000001',
          overallocated: false,
          basis: {},
        },
      ],
    },
  };
}
function flow(data: Fixture, id = flowId): WorkflowView {
  const source = proposal(data);
  return {
    id,
    account_seq: id === flowId ? '101' : '202',
    mode: 'synthetic',
    seed: {
      investigation_id: sourceId,
      investigation_revision: 1,
      input_id: source.input_id,
      output_id: source.output_id,
      run_id: source.run_id,
      snapshot_id: data.first.id,
      alternative_id: 'buy-synth',
    },
    revision: 1,
    status: 'active',
    stage: null,
    steps: [],
    created_at: now,
    updated_at: now,
    orders_enabled: false,
    execution_ready: false,
  };
}
function plan(data: Fixture): CapitalPlanResponse {
  const source = proposal(data);
  const a = source.capital_proposal!.alternatives[0];
  return {
    id: planId,
    record: {
      kind: 'capital_plan',
      schema_version: 1,
      recorded_at: now,
      request: {
        snapshot_id: data.first.id,
        source: { kind: 'investigation_output', id: source.output_id },
        mode: 'synthetic',
        funding: source.capital_context!.funding!,
        alternatives: [
          {
            key: a.key,
            label: a.label,
            rationale: a.rationale,
            legs: a.legs.map((item) => ({
              action: item.action,
              symbol: item.symbol,
              market: item.market,
              currency: item.currency,
              quantity: item.quantity!,
              price: item.price,
              fee_bps: item.fee_bps!,
              fixed_fee: item.fixed_fee!,
              tax_bps: item.tax_bps!,
              rationale: item.rationale,
            })),
          },
        ],
      },
      snapshot: data.context.account!.snapshot,
      source_context: {
        kind: 'investigation_output',
        id: source.output_id,
        mode: 'synthetic',
        recorded_at: now,
        account_snapshot_id: data.first.id,
        account_seq: '101',
      },
      reservations: { known: true, cash: [], holdings: [] },
      calculation: {
        arithmetic_precision: 256,
        arithmetic_rounding: 'ROUND_HALF_EVEN',
        local_reservations_known: true,
        cash_capacity: [],
        alternatives: [],
        assumptions: [],
        warnings: [],
        orders_enabled: false,
        execution_ready: false,
      },
    },
  };
}
function scan(id: string): BrokerScanSummary {
  return {
    id,
    account_seq: '101',
    mode: 'synthetic',
    recorded_at: now,
    collection_started_at: now,
    collection_completed_at: now,
    observations_count: 1,
    orders_count: 1,
    coverage: {
      open_complete: true,
      closed_complete: true,
      details_complete: true,
      complete: true,
      closed_pages: 1,
      stop_reason: 'complete',
      unresolved_detail_ids: [],
      ordered_at_from: null,
      ordered_at_to: null,
      date_basis: 'orderedAt_KST',
      atomic_account_instant: false,
      all_order_types: false,
      individual_fills: false,
      order_lineage: false,
      source_authenticity: false,
    },
  };
}
type Body = WorkflowCreate | WorkflowMutation | WorkflowObserve | WorkflowReconcile;
/** HTTP fixtures track receipts and selection behavior, not real models or financial arithmetic. */
async function mocks(page: Page, data: Fixture, existing = false) {
  const state = {
    proposals: [proposal(data), proposal(data, otherSourceId)],
    investigations: [investigation(data), investigation(data, otherSourceId)],
    flows: existing ? [flow(data), flow(data, otherFlowId)] : ([] as WorkflowView[]),
    calls: [] as { kind: string; id: string; body: Body }[],
    receipts: new Map<string, WorkflowView>(),
    lost: '',
    reject: '',
    proposalError: false,
    detailError: false,
    listError: false,
    heldProposalId: '',
    heldProposal: undefined as Promise<void> | undefined,
    heldFlowId: '',
    heldFlow: undefined as Promise<void> | undefined,
    heldCreate: undefined as Promise<void> | undefined,
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), jobs_enabled: true, read_only: false },
    });
  });
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: state.investigations } }),
  );
  await page.route(`**/api/v1/capital-plans/${planId}`, (route) =>
    route.fulfill({ json: plan(data) }),
  );
  await page.route(/\/api\/v1\/broker\/scans(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: { items: [scan(scanId), scan(otherScanId)], total_count: 2, omitted_count: 0 },
    }),
  );
  await page.route(/\/api\/v1\/workflows(?:\/.*|\?.*)?$/, async (route) => {
    const request = route.request();
    const parts = new URL(request.url()).pathname.split('/').slice(4);
    const id = parts[0];
    if (request.method() === 'GET') {
      if (id === 'proposal') {
        const value = structuredClone(
          state.proposals.find((item) => item.investigation_id === parts[1]),
        );
        if (parts[1] === state.heldProposalId && state.heldProposal) await state.heldProposal;
        await route
          .fulfill(
            state.proposalError
              ? {
                  status: 503,
                  json: { error: { code: 'workflows_unavailable', message: 'Synthetic failure' } },
                }
              : { json: value },
          )
          .catch(() => {});
        return;
      }
      if (!id) {
        await route.fulfill(
          state.listError
            ? {
                status: 503,
                json: { error: { code: 'workflows_unavailable', message: 'Synthetic failure' } },
              }
            : { json: { items: state.flows, total_count: state.flows.length, omitted_count: 0 } },
        );
        return;
      }
      const value = structuredClone(state.flows.find((item) => item.id === id));
      if (id === state.heldFlowId && state.heldFlow) await state.heldFlow;
      await route
        .fulfill(
          state.detailError
            ? {
                status: 503,
                json: { error: { code: 'workflows_unavailable', message: 'Synthetic failure' } },
              }
            : { json: value },
        )
        .catch(() => {});
      return;
    }
    const body = request.postDataJSON() as Body;
    const kind = id ? parts[1] : 'create';
    state.calls.push({ kind, id: id ?? '', body });
    if (state.reject) {
      const code = state.reject;
      state.reject = '';
      await route.fulfill({
        status: 409,
        json: { error: { code, message: 'Synthetic conflict' } },
      });
      return;
    }
    let result = state.receipts.get(body.request_key);
    if (!result) {
      if (kind === 'create') {
        result = flow(data);
        state.flows.push(result);
      } else {
        result = state.flows.find((item) => item.id === id)!;
        result.revision += 1;
        if (kind === 'pause') result.status = 'paused';
        else if (kind === 'resume') result.status = 'active';
        else if (kind === 'recover') {
          result.status = 'active';
          result.steps
            .filter((item) => item.state === 'needs_check' || item.state === 'running')
            .forEach((item) => (item.state = 'prepared'));
        } else {
          const pending = result.steps.find((item) => item.state === 'prepared');
          if (pending) {
            pending.state = 'succeeded';
            pending.result = { intent_id: intentId, scan_id: scanId };
            pending.attempt_count += 1;
          } else {
            const stage =
              kind === 'advance'
                ? stages.find((name) => !result!.steps.some((item) => item.kind === name))!
                : kind === 'observe'
                  ? 'order_observation'
                  : 'reconciliation';
            const requestBody =
              stage === 'funding_refresh'
                ? { snapshot_id: data.first.id }
                : stage === 'capital_plan'
                  ? plan(data).record.request
                  : stage === 'reservation'
                    ? { plan_id: planId, alternative_id: 'buy-synth' }
                    : stage === 'order_intent'
                      ? {
                          plan_id: planId,
                          alternative_id: 'buy-synth',
                          reservation_id: reservationId,
                        }
                      : stage === 'order_observation'
                        ? { intent_id: intentId, scan_id: (body as WorkflowObserve).scan_id }
                        : { ...body, before_snapshot_id: data.first.id };
            const resultBody =
              stage === 'funding_refresh'
                ? { funding: { account_seq: '101', pools: proposal(data).capital_context!.pools } }
                : stage === 'capital_plan'
                  ? { plan_id: planId }
                  : stage === 'reservation'
                    ? { reservation_id: reservationId }
                    : stage === 'order_intent'
                      ? { intent_id: intentId }
                      : stage === 'order_observation'
                        ? { intent_id: intentId, scan_id: (body as WorkflowObserve).scan_id }
                        : { reconciliation_id: reportId };
            result.steps.push({
              id: `25000000-0000-4000-8000-${String(100 + result.steps.length).padStart(12, '0')}`,
              workflow_id: id,
              sequence: result.steps.length + 1,
              kind: stage,
              input: {
                expected_workflow_revision: (body as WorkflowMutation).expected_revision,
                request: requestBody,
              },
              request_key: body.request_key,
              request_sha256: '3'.repeat(64),
              state: 'succeeded',
              attempt_count: 1,
              lease_expires_at: null,
              result: resultBody,
              error_code: null,
              created_at: now,
              updated_at: now,
              completed_at: now,
            });
            if (stage === 'reconciliation') result.status = 'completed';
          }
        }
      }
      state.receipts.set(body.request_key, structuredClone(result));
    }
    if (kind === 'create' && state.heldCreate) await state.heldCreate;
    if (state.lost === kind) {
      state.lost = '';
      await route.fulfill({
        status: 503,
        json: { error: { code: 'workflows_unavailable', message: 'Synthetic lost receipt' } },
      });
    } else await route.fulfill({ json: result });
  });
  return state;
}
async function open(page: Page) {
  await page.goto('/');
  await panel(page).getByText('AI 운용 흐름', { exact: true }).click();
}
async function choose(page: Page) {
  await panel(page).getByText('완료된 AI 대안 연결하기', { exact: true }).click();
  await panel(page)
    .getByRole('combobox', { name: '운용에 연결할 완료 조사', exact: true })
    .selectOption(sourceId);
}
async function create(page: Page) {
  await choose(page);
  await panel(page).getByRole('button', { name: '조사 계좌 관측 선택', exact: true }).click();
  await panel(page)
    .getByRole('combobox', { name: 'AI 제안 대안', exact: true })
    .selectOption('buy-synth');
  await panel(page).getByRole('button', { name: '대안으로 운용 흐름 만들기', exact: true }).click();
}
async function select(page: Page, id = flowId) {
  await panel(page)
    .getByRole('combobox', { name: '저장된 운용 흐름', exact: true })
    .selectOption(id);
  await expect(detail(page)).toBeVisible();
}
async function advanceFour(page: Page) {
  for (let i = 1; i <= 4; i++) {
    await detail(page).getByRole('button', { name: '다음 단계 진행', exact: true }).click();
    await expect(detail(page).locator('.paper-intent')).toHaveCount(i);
  }
}

test('workflows are disabled by default without disturbing account reading', async ({ page }) => {
  await open(page);
  await expect(panel(page).getByText(/운용 흐름 저장이 꺼져/)).toBeVisible();
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toBeEnabled();
});
test('V1, unconfigured budgets and incomplete quantities never become authorized capital proposals', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.proposals[0].capital_proposal = null;
  state.proposals[0].capital_context = null;
  await open(page);
  await choose(page);
  await expect(panel(page).getByText(/V2 자금 제안이 없습니다/)).toBeVisible();
  await expect(
    panel(page).getByRole('button', { name: '대안으로 운용 흐름 만들기' }),
  ).toBeDisabled();
  state.proposals[0] = proposal(data);
  state.proposals[0].capital_proposal!.alternatives[0].legs[0].quantity = null;
  state.proposals[0].completeness = {
    complete_alternative_keys: [],
    incomplete_alternatives: [{ key: 'buy-synth', missing_fields: ['legs[0].quantity'] }],
  };
  await panel(page).getByRole('button', { name: '운용 자료 재조회' }).click();
  await panel(page).getByRole('button', { name: '조사 계좌 관측 선택' }).click();
  await panel(page).getByRole('combobox', { name: 'AI 제안 대안' }).selectOption('buy-synth');
  await expect(panel(page).getByText('미확인: 1번 항목 · 수량', { exact: true })).toBeVisible();
  await expect(
    panel(page).getByRole('button', { name: '대안으로 운용 흐름 만들기' }),
  ).toBeDisabled();
  state.proposals[0] = proposal(data);
  state.proposals[0].capital_context!.status = 'unconfigured';
  state.proposals[0].capital_context!.funding = null;
  await panel(page).getByRole('button', { name: '운용 자료 재조회' }).click();
  await expect(
    panel(page).getByRole('button', { name: '대안으로 운용 흐름 만들기' }),
  ).toBeDisabled();
  expect(state.calls).toEqual([]);
});
test('creation binds the investigation revision and exact snapshot without sending new budget authority', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.lost = 'create';
  await open(page);
  await create(page);
  await expect(panel(page).getByText('1,000.000000000000001', { exact: true })).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page).getByRole('button', { name: '같은 운용 요청 확인' }).click();
  await expect.poll(() => state.calls.length).toBe(2);
  expect(state.calls[0].body).toEqual(state.calls[1].body);
  expect(Object.keys(state.calls[0].body).sort()).toEqual([
    'alternative_id',
    'investigation_id',
    'investigation_revision',
    'request_key',
  ]);
  await expect(
    panel(page).getByRole('combobox', { name: '저장된 운용 흐름', exact: true }),
  ).toHaveValue('');
  expect(state.flows).toHaveLength(1);
});
test('one stage per click prepares an intent and explicit linked scan gates reconciliation', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page);
  await create(page);
  await expect(detail(page)).toBeVisible();
  await advanceFour(page);
  expect(state.calls.map((item) => item.kind)).toEqual([
    'create',
    'advance',
    'advance',
    'advance',
    'advance',
  ]);
  await expect(
    detail(page).getByRole('button', { name: '다음 단계 진행', exact: true }),
  ).toHaveCount(0);
  await expect(
    detail(page)
      .getByText(/900.000000000000001/)
      .first(),
  ).toBeVisible();
  await detail(page).getByText('후속 관측과 대조', { exact: true }).click();
  await detail(page)
    .getByRole('combobox', { name: '대조할 이후 계좌 관측', exact: true })
    .selectOption(data.first.id);
  await detail(page)
    .getByRole('combobox', { name: '대조 이후 브로커 관측', exact: true })
    .selectOption(scanId);
  await expect(detail(page).getByRole('button', { name: '선택 자료로 대조' })).toBeDisabled();
  await detail(page)
    .getByRole('combobox', { name: '흐름에 연결할 브로커 관측' })
    .selectOption(scanId);
  await detail(page).getByRole('button', { name: '흐름에 관측 연결' }).click();
  await expect(detail(page).locator('.paper-intent')).toHaveCount(5);
  await detail(page).getByText('후속 관측과 대조', { exact: true }).click();
  await detail(page).getByRole('button', { name: '선택 자료로 대조' }).click();
  await expect(detail(page).getByText('대조 완료 · 버전 7', { exact: true })).toBeVisible();
  expect(state.calls.at(-1)!.body).toMatchObject({
    after_snapshot_id: data.first.id,
    before_scan_id: null,
    after_scan_id: scanId,
  });
  await panel(page).screenshot({ path: testInfo.outputPath('feature-25-workflow-desktop.png') });
});
test('pause and resume are explicit while uncertain advances retry the original flow and CAS', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page);
  await select(page);
  await detail(page).getByRole('button', { name: '운용 흐름 일시정지' }).click();
  await expect(detail(page).getByRole('button', { name: '다음 단계 진행' })).toHaveCount(0);
  await detail(page).getByRole('button', { name: '운용 흐름 재개' }).click();
  await expect(detail(page).getByRole('button', { name: '다음 단계 진행' })).toBeVisible();
  state.lost = 'advance';
  await detail(page).getByRole('button', { name: '다음 단계 진행' }).click();
  await expect(panel(page).getByRole('button', { name: '같은 운용 요청 확인' })).toBeVisible();
  await select(page, otherFlowId);
  await panel(page).getByRole('button', { name: '같은 운용 요청 확인' }).click();
  await expect.poll(() => state.calls.length).toBe(4);
  expect(state.calls[2]).toEqual(state.calls[3]);
  await expect(
    detail(page).getByRole('heading', { name: '계좌 202 · 합성 운용 흐름' }),
  ).toBeVisible();
  expect(state.flows[0].steps).toHaveLength(1);
});
test('recovered observation checkpoints can resume even after all four preparation stages', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page);
  await create(page);
  await advanceFour(page);
  const current = state.flows[0];
  current.status = 'attention';
  current.steps.push({
    ...current.steps[3],
    id: '25000000-0000-4000-8000-000000000199',
    sequence: 5,
    kind: 'order_observation',
    state: 'needs_check',
    result: null,
    error_code: 'stage_validation_failed',
    request_key: 'recovery',
    input: {
      expected_workflow_revision: current.revision,
      request: { intent_id: intentId, scan_id: scanId },
    },
  });
  await panel(page).getByRole('button', { name: '운용 자료 재조회' }).click();
  await detail(page).getByRole('button', { name: '저장 결과 확인·복구' }).click();
  await expect(detail(page).getByRole('button', { name: '다음 단계 진행' })).toBeVisible();
  await detail(page).getByRole('button', { name: '다음 단계 진행' }).click();
  await expect(detail(page).getByText('결과 저장', { exact: true })).toHaveCount(5);
  expect(state.calls.at(-2)!.kind).toBe('recover');
  expect(state.calls.at(-1)!.kind).toBe('advance');
});
test('late proposal and detail responses and failed reloads hide stale data', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page);
  await choose(page);
  await panel(page).getByRole('combobox', { name: 'AI 제안 대안' }).selectOption('buy-synth');
  let release!: () => void;
  state.heldProposalId = otherSourceId;
  state.heldProposal = new Promise((resolve) => (release = resolve));
  await panel(page)
    .getByRole('combobox', { name: '운용에 연결할 완료 조사' })
    .selectOption(otherSourceId);
  await expect(
    panel(page).getByRole('heading', { name: 'SYNTH 분할 검토', exact: true }),
  ).toHaveCount(0);
  await panel(page)
    .getByRole('combobox', { name: '운용에 연결할 완료 조사' })
    .selectOption(sourceId);
  release();
  await expect(panel(page).getByRole('combobox', { name: 'AI 제안 대안' })).toHaveValue('');
  await select(page);
  let releaseFlow!: () => void;
  state.heldFlowId = otherFlowId;
  state.heldFlow = new Promise((resolve) => (releaseFlow = resolve));
  await panel(page).getByRole('combobox', { name: '저장된 운용 흐름' }).selectOption(otherFlowId);
  await expect(detail(page)).toHaveCount(0);
  await select(page);
  releaseFlow();
  state.detailError = true;
  state.proposalError = true;
  await panel(page).getByRole('button', { name: '운용 자료 재조회' }).click();
  await expect(detail(page)).toHaveCount(0);
  await expect(panel(page).getByRole('combobox', { name: 'AI 제안 대안' })).toHaveCount(0);
});
test('mobile keeps proposal decimals and workflow cards inside the viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  await mocks(page, data);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  await create(page);
  await expect(detail(page)).toBeVisible();
  await expect(panel(page).getByText('0.100000000000001', { exact: true })).toBeVisible();
  await advanceFour(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).screenshot({ path: testInfo.outputPath('feature-25-workflow-mobile.png') });
});
