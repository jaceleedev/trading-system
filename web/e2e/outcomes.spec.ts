import { expect, test, type Page } from '@playwright/test';
import type {
  PaperBook,
  PaperWindowOutcome,
  WorkflowView,
  OutcomeResponse,
  OutcomeCreate,
  OpenOrder,
} from '../src/lib/api/types.gen';

const now = '2026-09-11T09:00:00Z';
const later = '2026-09-11T10:00:00Z';
const bookId = '26000000-0000-4000-8000-000000000001';
const flowId = '26000000-0000-4000-8000-000000000002';
const reportId = 'a'.repeat(64);
const otherReportId = 'b'.repeat(64);
const snapshotId = 'c'.repeat(64);
const panel = (page: Page) => page.getByRole('region', { name: '기간별 결과 비교', exact: true });
const detail = (page: Page) =>
  panel(page).getByRole('region', { name: '선택 결과 보고서', exact: true });

function paperBook(id = bookId): PaperBook {
  return {
    id,
    account_seq: '9007199254740993',
    label: `합성 비교 원장 ${id.slice(-1)}`,
    mode: 'synthetic',
    snapshot_id: snapshotId,
    created_at: now,
    updated_at: later,
    revision: 3,
    orders_enabled: false,
    execution_ready: false,
    seed: {
      label: '합성 비교 원장',
      account_seq: '9007199254740993',
      mode: 'synthetic',
      snapshot_id: snapshotId,
      holdings: [],
      initial_cash: [{ currency: 'USD', amount: '100' }],
    },
    state: {
      schema_version: 1,
      cash: [],
      positions: [],
      costs: [],
      realized: [],
      marks: [],
      valuation: [],
      arithmetic_precision: 256,
      arithmetic_rounded: false,
      orders_enabled: false,
    },
  };
}
function workflow(): WorkflowView {
  return {
    id: flowId,
    account_seq: '9007199254740993',
    mode: 'synthetic',
    revision: 22,
    status: 'completed',
    stage: null,
    seed: {},
    steps: [],
    created_at: now,
    updated_at: later,
    orders_enabled: false,
    execution_ready: false,
  };
}
function paperResult(): PaperWindowOutcome {
  return {
    book_id: bookId,
    account_seq: '9007199254740993',
    mode: 'synthetic',
    window: {
      start_at: now,
      end_at: later,
      basis: 'system_recorded_at',
      start_sequence: 1,
      end_sequence: 3,
    },
    currencies: [
      {
        currency: 'USD',
        start_cash: '9007199254740993.000000000000001',
        end_cash: '9007199254740982.890000000000001',
        cash_delta: '-10.11',
        start_position_value: '0',
        end_position_value: '10',
        start_equity: '9007199254740993.000000000000001',
        end_equity: '9007199254740992.890000000000001',
        equity_delta: '-0.11',
        simple_return: '-0.00000000000000001221245327',
        return_unknown_reason: null,
        fees: '0.10',
        taxes: '0.01',
        slippage_cost: '0.02',
        fill_cash_delta: '-10.11',
        cash_rounding_residual: '0',
        cost_rounding_residual: '0',
        realized_rounding_residual: '0',
        historical_cost_realized: { known_amount: '5', unknown_sales: 1, amount: null },
        start_unrealized_pnl: null,
        end_unrealized_pnl: null,
        start_missing_price_symbols: [],
        end_missing_price_symbols: [],
        start_unknown_cost_symbols: ['SYNTH'],
        end_unknown_cost_symbols: ['SYNTH'],
      },
      {
        currency: 'KRW',
        start_cash: null,
        end_cash: null,
        cash_delta: null,
        start_position_value: null,
        end_position_value: null,
        start_equity: null,
        end_equity: null,
        equity_delta: null,
        simple_return: null,
        return_unknown_reason: 'equity_unknown',
        fees: '0',
        taxes: '0',
        slippage_cost: '0',
        fill_cash_delta: '0',
        cash_rounding_residual: null,
        cost_rounding_residual: '0',
        realized_rounding_residual: '0',
        historical_cost_realized: { known_amount: '0', unknown_sales: 0, amount: '0' },
        start_unrealized_pnl: null,
        end_unrealized_pnl: null,
        start_missing_price_symbols: ['KRTEST'],
        end_missing_price_symbols: ['KRTEST'],
        start_unknown_cost_symbols: ['KRTEST'],
        end_unknown_cost_symbols: ['KRTEST'],
      },
    ],
    actions: [
      {
        market: 'US',
        symbol: 'SYNTH',
        currency: 'USD',
        action: 'buy',
        fill_count: 1,
        quantity: '0.5',
        notional: '10',
        cash_delta: '-10.11',
        fees: '0.10',
        taxes: '0.01',
        slippage_cost: '0.02',
        known_realized_pnl: '0',
        unknown_realized_sales: 0,
      },
    ],
    counts: { fills: 1, submissions: 1, cancellations: 0, unfilled: 1, observations: 1 },
    marks: {
      start: [],
      end: [
        {
          market: 'US',
          symbol: 'SYNTH',
          currency: 'USD',
          price: '20',
          point_id: 'd'.repeat(64),
          revision_id: 'e'.repeat(64),
          capture_id: 'f'.repeat(64),
          period_end: '2026-09-11T09:59:00Z',
          observed_at: '2026-09-11T09:59:05Z',
        },
      ],
    },
    coverage: {
      source_counters_verified: true,
      source_arithmetic_rounded: false,
      report_arithmetic_precision: 1536,
      report_arithmetic_rounded: false,
      external_paper_flows: 'unsupported_after_seed',
      fx_conversion: false,
      actual_pnl_computed: false,
      automatic_winner: false,
      source_authenticity_verified: false,
      historical_cost_pnl_is_ai_attribution: false,
    },
    intent_sources: [
      {
        intent_id: '26000000-0000-4000-8000-000000000003',
        plan_id: '1'.repeat(64),
        alternative_id: 'buy-synth',
      },
    ],
  };
}

function report(id = reportId): OutcomeResponse {
  const order: OpenOrder = {
    orderId: 'SYNTH-ORDER',
    symbol: 'SYNTH',
    side: 'BUY',
    orderType: 'LIMIT',
    timeInForce: 'DAY',
    status: 'PARTIAL_FILLED',
    quantity: '2',
    currency: 'USD',
    price: '10',
    orderedAt: now,
    execution: {
      filledQuantity: '0.5',
      filledAmount: '5',
      averageFilledPrice: '10',
      commission: '0.05',
      tax: null,
      filledAt: later,
      settlementDate: null,
    },
  };
  return {
    id,
    record: {
      kind: 'outcome_report',
      schema_version: 1,
      input_id: '2'.repeat(64),
      mode: 'synthetic',
      start_at: now,
      end_at: later,
      recorded_at: later,
      orders_enabled: false,
      actual_pnl_computed: false,
      paper: [paperResult()],
      broker: [
        {
          workflow_id: flowId,
          account_seq: '9007199254740993',
          mode: 'synthetic',
          workflow_status: 'completed',
          intent_id: '26000000-0000-4000-8000-000000000003',
          reservation_held: true,
          operation_states: ['acknowledged', 'ambiguous'],
          reconciliation_ids: ['3'.repeat(64)],
          actual_pnl: null,
          external_cash_flows: null,
          fx_pnl: null,
          individual_fills_available: false,
          warnings: ['합성 검사: 누적 관측은 개별 체결이 아닙니다.'],
          comparisons: [
            {
              reconciliation_id: '3'.repeat(64),
              as_of: later,
              before_snapshot_at: '2026-09-11T08:59:00Z',
              after_snapshot_at: later,
              period_matches_requested_window: false,
              orders: [
                {
                  order_id: 'SYNTH-ORDER',
                  order_key: '4'.repeat(64),
                  before: null,
                  after: {
                    order,
                    observed_at: later,
                    recorded_at: later,
                    observation_ids: ['5'.repeat(64)],
                    groups_seen: ['DETAIL'],
                  },
                  before_conflict_observation_ids: [],
                  after_conflict_observation_ids: [],
                  deltas: {
                    filled_quantity: '0.5',
                    filled_amount: '5',
                    commission: '-0.10',
                    tax: null,
                  },
                  classification: ['cumulative_increase', 'financial_revision'],
                  origin: 'unattributed',
                  lineage_known: false,
                  individual_fills_available: false,
                },
              ],
              holdings: [
                {
                  market: 'US',
                  symbol: 'SYNTH',
                  before_currency: null,
                  after_currency: 'USD',
                  before_present: false,
                  after_present: true,
                  before_quantity: null,
                  after_quantity: '0.5',
                  quantity_delta: null,
                  classification: 'appeared',
                  absence_zero_assumed: false,
                },
              ],
              buying_power: [
                {
                  currency: 'USD',
                  before_amount: '100',
                  after_amount: '95',
                  delta: '-5',
                  semantics: 'buying_capacity_not_cash',
                },
              ],
            },
          ],
        },
      ],
      methods: [
        {
          plan_id: '1'.repeat(64),
          source_kind: 'investigation_output',
          source_id: '6'.repeat(64),
          purpose: id === reportId ? '합성 판단 방식 비교' : '다른 저장 결과의 판단',
          input_id: '7'.repeat(64),
          output_schema_version: 2,
          instructions_sha256: '8'.repeat(64),
          run_ids: ['9'.repeat(64)],
          run_selection: 'unique',
          requested_model: 'declared-synthetic-model',
          requested_reasoning_effort: 'high',
          reported_model: null,
          model_identity_verified: false,
          cli_version: 'synthetic-cli-1',
          output_schema_sha256: 'f'.repeat(64),
          book_ids: [bookId],
          workflow_ids: [flowId],
        },
      ],
      comparison: {
        window_basis: 'system_recorded_at',
        aggregate_pnl: null,
        automatic_winner: null,
        same_initial_paper_seed: null,
        same_paper_profiles: null,
        mixed_methods_book_ids: [bookId],
        initial_holdings_book_ids: [bookId],
        limitations: ['여러 판단과 기존 보유가 섞여 있어 인과적 모델 성과를 확정하지 않습니다.'],
      },
    },
  };
}

/** Synthetic HTTP contracts only; source replay and financial arithmetic are Python checks. */
async function mocks(page: Page, enabled = true, saved = true) {
  const state = {
    reports: saved ? [report(), report(otherReportId)] : ([] as OutcomeResponse[]),
    creates: [] as OutcomeCreate[],
    lost: false,
    conflict: false,
    detailError: false,
    listError: false,
    heldId: '',
    hold: null as Promise<void> | null,
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        jobs_enabled: enabled,
        read_only: !enabled,
        synthetic: true,
      },
    });
  });
  await page.route(/\/api\/v1\/paper\/books(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        items: Array.from({ length: 5 }, (_, i) =>
          paperBook(`26000000-0000-4000-8000-00000000000${i + 1}`),
        ),
        total_count: 5,
        omitted_count: 0,
      },
    }),
  );
  await page.route(/\/api\/v1\/workflows(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [workflow()], total_count: 1, omitted_count: 0 } }),
  );
  await page.route(/\/api\/v1\/outcomes(?:\/.*|\?.*)?$/, async (route) => {
    const request = route.request();
    const id = new URL(request.url()).pathname.split('/')[4];
    if (request.method() === 'POST') {
      const body = request.postDataJSON() as OutcomeCreate;
      state.creates.push(body);
      if (state.conflict) {
        await route.fulfill({
          status: 409,
          json: { error: { code: 'outcome_conflict', message: 'Synthetic rejected request' } },
        });
        return;
      }
      const result = report();
      if (!state.reports.some((item) => item.id === result.id)) state.reports.push(result);
      if (state.lost) {
        state.lost = false;
        await route.fulfill({
          status: 503,
          json: { error: { code: 'outcomes_unavailable', message: 'Synthetic lost response' } },
        });
        return;
      }
      await route.fulfill({ json: result });
      return;
    }
    if (!id) {
      await route.fulfill(
        state.listError
          ? {
              status: 503,
              json: { error: { code: 'outcomes_unavailable', message: 'Synthetic list failure' } },
            }
          : {
              json: {
                items: state.reports.map(({ id, record }) => ({
                  id,
                  mode: record.mode,
                  start_at: record.start_at,
                  end_at: record.end_at,
                  recorded_at: record.recorded_at,
                  book_count: record.paper.length,
                  workflow_count: record.broker.length,
                })),
                total_count: state.reports.length,
                omitted_count: 0,
                invalid_count: 0,
              },
            },
      );
      return;
    }
    const value = structuredClone(state.reports.find((item) => item.id === id));
    if (state.heldId === id && state.hold) await state.hold;
    await route
      .fulfill(
        state.detailError
          ? {
              status: 503,
              json: { error: { code: 'outcomes_unavailable', message: 'Synthetic read failure' } },
            }
          : { json: value },
      )
      .catch(() => {});
  });
  return state;
}
async function open(page: Page) {
  await page.goto('/');
  await expect(page).toHaveTitle('투자 작업실 · Trading Research');
  await panel(page).locator(':scope > details > summary').click();
}
async function form(page: Page) {
  await panel(page).getByText('자료와 기간 선택하기', { exact: true }).click();
  await panel(page).getByLabel('합성 비교 원장 1', { exact: false }).check();
  await panel(page).getByLabel('시작 시각', { exact: true }).fill('2026-09-11T18:00');
}
test.use({ timezoneId: 'Asia/Seoul' });

test('disabled calculations retain offline saved outcomes and existing account browsing', async ({
  page,
}) => {
  await mocks(page, false);
  await open(page);
  await expect(
    panel(page).getByText('이 작업실에서는 새 결과 계산이 꺼져 있습니다.', { exact: false }),
  ).toBeVisible();
  await panel(page).getByLabel('결과 보고서 선택').selectOption(reportId);
  await expect(detail(page).getByRole('heading', { name: '합성 판단 방식 비교' })).toBeVisible();
  await expect(page.getByLabel('계좌 관측', { exact: true })).toBeEnabled();
});
test('empty outcomes never select account or invent a result', async ({ page }) => {
  await mocks(page, true, false);
  await open(page);
  await expect(panel(page).getByText('저장된 결과 보고서가 없습니다.')).toBeVisible();
  await panel(page).getByText('자료와 기간 선택하기', { exact: true }).click();
  await expect(panel(page).getByRole('button', { name: '기간 결과 계산·저장' })).toBeDisabled();
  await expect(detail(page)).toHaveCount(0);
});
test('each source group requires explicit selection and caps paper books at four', async ({
  page,
}) => {
  const state = await mocks(page, true, false);
  await open(page);
  await form(page);
  for (const index of [2, 3, 4])
    await panel(page).getByLabel(`합성 비교 원장 ${index}`, { exact: false }).check();
  await expect(panel(page).getByLabel('합성 비교 원장 5', { exact: false })).toBeDisabled();
  await panel(page)
    .getByRole('group', { name: '운용 흐름 · 최대 4개' })
    .getByRole('checkbox')
    .check();
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect.poll(() => state.creates.length).toBe(1);
  expect(state.creates[0].book_ids).toHaveLength(4);
  expect(state.creates[0].workflow_ids).toEqual([flowId]);
  expect(state.creates[0].start_at).toBe('2026-09-11T09:00:00.000Z');
  expect(state.creates[0].end_at).toBeNull();
  expect(state.creates[0].mode).toBe('synthetic');
});
test('exact native currency results keep old cost and cumulative broker observations separate', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await mocks(page, true, false);
  await open(page);
  await form(page);
  await panel(page)
    .getByRole('group', { name: '운용 흐름 · 최대 4개' })
    .getByRole('checkbox')
    .check();
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect(detail(page)).toBeVisible();
  await expect(
    detail(page).getByRole('cell', { name: '9,007,199,254,740,993.000000000000001', exact: true }),
  ).toBeVisible();
  await expect(
    detail(page).getByRole('cell', { name: '-0.00000000000000001221245327', exact: true }),
  ).toBeVisible();
  await expect(detail(page).getByRole('row', { name: /^KRW/ })).toContainText('미확인');
  await detail(page).getByText('평가 구성과 미확인 항목', { exact: true }).click();
  await expect(
    detail(page)
      .getByRole('heading', { name: '과거 취득원가 기준 · AI 판단 이후 성과와 구분' })
      .first(),
  ).toBeVisible();
  await expect(
    detail(page).getByText('대조 관측 기간이 요청 기간과 다릅니다.', { exact: false }),
  ).toBeVisible();
  await expect(detail(page).getByRole('cell', { name: '-0.10', exact: true })).toBeVisible();
  await expect(detail(page).getByText('declared-synthetic-model / high')).toBeVisible();
  await expect(detail(page).getByText('실제 모델 정체', { exact: true })).toBeVisible();
  expect(errors).toEqual([]);
  await panel(page).screenshot({ path: testInfo.outputPath('feature-26-outcomes-desktop.png') });
});
test('uncertain response retries original null end and request identity despite changed draft', async ({
  page,
}) => {
  const state = await mocks(page, true, false);
  state.lost = true;
  await open(page);
  await form(page);
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect(panel(page).getByRole('button', { name: '같은 계산 요청 결과 확인' })).toBeEnabled();
  await panel(page).getByLabel('종료 시각 · 선택').fill('2026-09-11T20:00');
  await panel(page).getByLabel('합성 비교 원장 1', { exact: false }).uncheck();
  await panel(page).getByLabel('합성 비교 원장 2', { exact: false }).check();
  await panel(page).getByRole('button', { name: '같은 계산 요청 결과 확인' }).click();
  await expect.poll(() => state.creates.length).toBe(2);
  expect(state.creates[1]).toEqual(state.creates[0]);
  await expect(panel(page).getByLabel('결과 보고서 선택')).toHaveValue('');
  await expect(detail(page)).toHaveCount(0);
});
test('late report responses and failed refresh never present another selected or old result', async ({
  page,
}) => {
  const state = await mocks(page);
  await open(page);
  let release!: () => void;
  state.heldId = reportId;
  state.hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await panel(page).getByLabel('결과 보고서 선택').selectOption(reportId);
  await expect(panel(page).getByText('선택한 결과 보고서를 읽고 있습니다.')).toBeVisible();
  await panel(page).getByLabel('결과 보고서 선택').selectOption(otherReportId);
  release();
  await expect(detail(page).getByRole('heading', { name: '다른 저장 결과의 판단' })).toBeVisible();
  await expect(detail(page).getByRole('heading', { name: '합성 판단 방식 비교' })).toHaveCount(0);
  state.detailError = true;
  await panel(page).getByRole('button', { name: '결과 자료 재조회' }).click();
  await expect(panel(page).getByRole('alert')).toContainText(
    '결과 자료 저장소에 연결하지 못했습니다.',
  );
  await expect(detail(page)).toHaveCount(0);
});
test('invalid windows and definite source conflicts permit a corrected new request', async ({
  page,
}) => {
  const state = await mocks(page, true, false);
  await open(page);
  await form(page);
  await panel(page).getByLabel('종료 시각 · 선택').fill('2026-09-11T17:00');
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect(panel(page).getByRole('alert')).toContainText('종료 시각은 시작 시각 이후');
  expect(state.creates).toHaveLength(0);
  state.conflict = true;
  await panel(page).getByLabel('종료 시각 · 선택').fill('');
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect(panel(page).getByRole('alert')).toContainText(
    '선택 자료와 기간을 비교할 수 없습니다.',
  );
  await expect(panel(page).getByRole('button', { name: '같은 계산 요청 결과 확인' })).toHaveCount(
    0,
  );
  state.conflict = false;
  await panel(page).getByRole('button', { name: '기간 결과 계산·저장' }).click();
  await expect(detail(page)).toBeVisible();
  expect(state.creates[0].request_key).not.toBe(state.creates[1].request_key);
});
test('mobile comparison forms and exact tables stay within viewport', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await mocks(page);
  await open(page);
  await form(page);
  await panel(page).getByLabel('결과 보고서 선택').selectOption(reportId);
  await expect(detail(page)).toBeVisible();
  await detail(page).getByText('종목·행동별 모의 체결과 평가 가격', { exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  expect(errors).toEqual([]);
  await panel(page).screenshot({ path: testInfo.outputPath('feature-26-outcomes-mobile.png') });
});
