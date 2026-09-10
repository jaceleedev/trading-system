import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  CapitalPlanResponse,
  CapitalPlanRequest,
  PaperBookDetail,
  PaperBookCreate,
  PaperSubmit,
  PaperAdvance,
  PaperCancel,
  PaperMutation,
  PaperIntent,
} from '../src/lib/api/types.gen';
const now = '2026-09-10T09:00:00Z';
const later = '2026-09-10T09:03:00Z';
const bookId = '22000000-0000-4000-8000-000000000001';
const secondBookId = '22000000-0000-4000-8000-000000000002';
const intentId = '22000000-0000-4000-8000-000000000011';
const planId = 'a'.repeat(64);
const otherPlanId = 'b'.repeat(64);
const captureId = 'c'.repeat(64);
const otherCaptureId = 'd'.repeat(64);
const panel = (page: Page) => page.getByRole('region', { name: '모의 매매', exact: true });
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
function plan(data: Fixture, identity = planId): CapitalPlanResponse {
  const request: CapitalPlanRequest = {
    snapshot_id: data.first.id,
    source: { kind: 'decision', id: 'e'.repeat(64) },
    mode: 'synthetic',
    funding: [{ currency: 'USD', limit_amount: '1000', reserve_amount: '0' }],
    alternatives: [
      {
        key: identity === planId ? 'add-beta' : 'other-plan',
        label: identity === planId ? 'BETA 추가 검토' : '다른 계획 대안',
        rationale: '합성 분할 체결 검토',
        legs: [
          {
            action: 'add',
            symbol: 'BETA',
            market: 'US',
            currency: 'USD',
            quantity: '0.25',
            price: '100',
            fee_bps: '10',
            fixed_fee: '0.5',
            tax_bps: '0',
            rationale: '합성 가정',
          },
        ],
      },
    ],
  };
  return {
    id: identity,
    record: {
      kind: 'capital_plan',
      schema_version: 1,
      recorded_at: now,
      request,
      snapshot: data.context.account!.snapshot,
      source_context: {
        ...request.source,
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
        execution_ready: false,
        orders_enabled: false,
      },
    },
  };
}
function book(data: Fixture, id = bookId): PaperBookDetail {
  return {
    id,
    label: id === bookId ? '합성 모의 원장' : '다른 모의 원장',
    account_seq: id === bookId ? '101' : '202',
    mode: 'synthetic',
    snapshot_id: id === bookId ? data.first.id : data.second.id,
    seed: {
      label: '합성 모의 원장',
      account_seq: '101',
      snapshot_id: data.first.id,
      mode: 'synthetic',
      initial_cash: [{ currency: 'USD', amount: '1000' }],
      holdings: [
        {
          market: 'US',
          symbol: 'BETA',
          currency: 'USD',
          quantity: '0.125',
          average_purchase_price: null,
        },
      ],
    },
    state: {
      schema_version: 1,
      cash: [{ currency: 'USD', amount: '1000' }],
      positions: [
        { market: 'US', symbol: 'BETA', currency: 'USD', quantity: '0.125', cost_basis: null },
      ],
      costs: [{ currency: 'USD', amount: '0' }],
      realized: [{ currency: 'USD', known_amount: '0', unknown_sales: 0 }],
      marks: [],
      arithmetic_precision: 256,
      arithmetic_rounded: false,
      orders_enabled: false,
      valuation: [
        {
          currency: 'USD',
          cash: '1000',
          position_value: null,
          unrealized_pnl: null,
          realized_pnl: '0',
          known_realized_pnl: '0',
          unknown_realized_sales: 0,
          modeled_cost: '0',
          missing_price_symbols: ['BETA'],
          unknown_cost_symbols: ['BETA'],
          equity: null,
        },
      ],
    },
    revision: 1,
    created_at: now,
    updated_at: now,
    execution_ready: false,
    orders_enabled: false,
    intents: [],
    events: [],
    intent_count: 0,
    event_count: 0,
    omitted_intent_count: 0,
    omitted_event_count: 0,
  };
}
/** Synthetic HTTP state only; engine and database arithmetic are covered by Python tests. */
async function mocks(page: Page, data: Fixture, initial = false) {
  const state = {
    books: initial ? [book(data), book(data, secondBookId)] : ([] as PaperBookDetail[]),
    creates: [] as PaperBookCreate[],
    submits: [] as { id: string; body: PaperSubmit }[],
    advances: [] as { id: string; body: PaperAdvance }[],
    cancels: [] as { id: string; intentId: string; body: PaperCancel }[],
    lost: '' as '' | 'create' | 'submit' | 'advance' | 'cancel',
    detailError: false,
    listError: false,
    planError: false,
    heldId: '',
    holdDetail: undefined as Promise<void> | undefined,
    heldPlan: '',
    holdPlan: undefined as Promise<void> | undefined,
    receipts: new Map<string, PaperMutation>(),
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), jobs_enabled: true, read_only: false },
    });
  });
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route('**/api/v1/market/catalog', (route) =>
    route.fulfill({
      json: {
        items: [captureId, otherCaptureId].map((id) => ({
          capture_id: id,
          endpoint: 'charts',
          symbol: 'BETA',
          interval: '1m',
          adjusted: false,
          currencies: ['USD'],
          retrieved_at: later,
          candle_count: 1,
          status: 'supported',
          reason: null,
          response_contract_sha256: 'f'.repeat(64),
        })),
        total_count: 2,
        supported_count: 2,
        unsupported_count: 0,
        invalid_count: 0,
        truncated_count: 0,
      },
    }),
  );
  await page.route(/\/api\/v1\/capital-plans(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        items: [planId, otherPlanId].map((id) => ({
          id,
          recorded_at: now,
          mode: 'synthetic',
          snapshot_id: data.first.id,
          source: { kind: 'decision', id: 'e'.repeat(64) },
          alternative_count: 1,
          eligible_count: 1,
        })),
        total_count: 2,
        omitted_count: 0,
      },
    }),
  );
  await page.route(/\/api\/v1\/capital-plans\/[a-f0-9]{64}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    if (id === state.heldPlan && state.holdPlan) await state.holdPlan;
    await route
      .fulfill(
        state.planError
          ? {
              status: 503,
              json: { error: { code: 'capital_unavailable', message: 'Synthetic failure' } },
            }
          : { json: plan(data, id) },
      )
      .catch(() => {});
  });
  await page.route(/\/api\/v1\/paper\/books(?:\/.*|\?.*)?$/, async (route) => {
    const request = route.request();
    const parts = new URL(request.url()).pathname.split('/').slice(5);
    const id = parts[0];
    if (request.method() === 'GET') {
      if (!id) {
        await route.fulfill(
          state.listError
            ? {
                status: 503,
                json: { error: { code: 'paper_unavailable', message: 'Synthetic failure' } },
              }
            : { json: { items: state.books, total_count: state.books.length, omitted_count: 0 } },
        );
        return;
      }
      const response = structuredClone(state.books.find((item) => item.id === id));
      if (state.heldId === id && state.holdDetail) await state.holdDetail;
      await route
        .fulfill(
          state.detailError
            ? {
                status: 503,
                json: { error: { code: 'paper_unavailable', message: 'Synthetic failure' } },
              }
            : { json: response },
        )
        .catch(() => {});
      return;
    }
    const body = request.postDataJSON() as PaperBookCreate &
      PaperSubmit &
      PaperAdvance &
      PaperCancel;
    const kind = !id
      ? 'create'
      : parts[1] === 'advance'
        ? 'advance'
        : parts[3] === 'cancel'
          ? 'cancel'
          : 'submit';
    if (kind === 'create') state.creates.push(body);
    if (kind === 'submit') state.submits.push({ id, body });
    if (kind === 'advance') state.advances.push({ id, body });
    if (kind === 'cancel') state.cancels.push({ id, intentId: parts[2], body });
    let response = state.receipts.get(body.request_key);
    if (!response) {
      if (kind === 'create') {
        const created = book(data);
        created.label = body.label;
        created.seed.initial_cash = body.initial_cash;
        created.state.cash = body.initial_cash;
        created.state.valuation[0].cash =
          body.initial_cash.find((item) => item.currency === 'USD')?.amount ?? null;
        state.books.push(created);
        response = { book: created, intent: null, events: [] };
      } else {
        const current = state.books.find((item) => item.id === id)!;
        current.revision++;
        current.updated_at = later;
        if (kind === 'submit') {
          const intent: PaperIntent = {
            id: intentId,
            book_id: id,
            plan_id: body.plan_id,
            alternative_id: body.alternative_id,
            account_seq: current.account_seq,
            mode: 'synthetic',
            request_key: body.request_key,
            created_at: now,
            updated_at: now,
            state: {
              id: intentId,
              created_at: now,
              status: 'pending',
              submission_sequence: 1,
              alternative_key: body.alternative_id,
              profile: body.profile,
              legs: [
                {
                  index: 0,
                  request: plan(data, body.plan_id).record.request.alternatives[0].legs[0],
                  remaining_quantity: '0.25',
                  filled_quantity: '0',
                  cash_budget_remaining: '25.525',
                  fixed_fee_charged: false,
                  status: 'pending',
                },
              ],
            },
          };
          current.intents.push(intent);
          current.intent_count++;
        }
        if (kind === 'advance') {
          if (current.intents[0]) {
            current.intents[0].state.status = 'partially_filled';
            current.intents[0].state.legs[0] = {
              ...current.intents[0].state.legs[0],
              status: 'partially_filled',
              remaining_quantity: '0.125',
              filled_quantity: '0.125',
              cash_budget_remaining: '12.5125',
              fixed_fee_charged: true,
            };
          }
          current.state.cash[0].amount = '986.9875';
          current.state.positions[0].quantity = '0.250';
          current.state.valuation[0] = {
            ...current.state.valuation[0],
            cash: '986.9875',
            position_value: '25.000000000000001',
            equity: '1011.987500000000001',
            modeled_cost: '0.5125',
            missing_price_symbols: [],
          };
          current.state.marks = [
            {
              market: 'US',
              symbol: 'BETA',
              currency: 'USD',
              price: '100.000000000000004',
              point_id: 'e'.repeat(64),
              revision_id: 'f'.repeat(64),
              capture_id: body.capture_ids[0],
              period_end: '2026-09-10T09:02:00Z',
              observed_at: later,
            },
          ];
          current.events.push({
            id: '22000000-0000-4000-8000-000000000021',
            book_id: id,
            sequence: 1,
            kind: 'simulated_fill',
            intent_id: intentId,
            capture_id: null,
            recorded_at: later,
            payload: {
              kind: 'simulated_fill',
              at: later,
              intent_id: intentId,
              leg_index: 0,
              data: {
                symbol: 'BETA',
                currency: 'USD',
                quantity: '0.125',
                price: '100',
                fee: '0.5125',
                tax: '0',
                cash_delta: '-13.0125',
                modeled_at: '2026-09-10T09:02:00Z',
                capture_id: body.capture_ids[0],
              },
            },
          });
          current.event_count++;
        }
        if (kind === 'cancel') {
          current.intents[0].state.status = 'cancelled';
          current.intents[0].state.legs[0].status = 'cancelled';
          current.intents[0].state.legs[0].cash_budget_remaining = '0';
        }
        response = { book: current, intent: current.intents[0] ?? null, events: current.events };
      }
      state.receipts.set(body.request_key, structuredClone(response));
    }
    if (state.lost === kind) {
      state.lost = '';
      await route.abort('failed');
    } else await route.fulfill({ json: response });
  });
  return state;
}
async function open(page: Page, data: Fixture, selectBook = false) {
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByText('모의 매매', { exact: true }).click();
  if (selectBook)
    await panel(page).getByRole('combobox', { name: '모의 원장 선택' }).selectOption(bookId);
}
async function fillCreate(page: Page) {
  await panel(page).getByText('새 모의 원장 만들기', { exact: true }).click();
  await panel(page)
    .getByRole('textbox', { name: '모의 원장 이름', exact: true })
    .fill('직접 입력한 모의 원장');
  await panel(page).getByRole('checkbox', { name: 'USD 모의 현금 사용' }).check();
  await panel(page)
    .getByRole('textbox', { name: 'USD 초기 모의 현금', exact: true })
    .fill('9007199254740993.00100');
}
async function fillIntent(page: Page) {
  await panel(page).getByRole('combobox', { name: '모의 주문할 저장 계획' }).selectOption(planId);
  await panel(page)
    .getByRole('combobox', { name: '모의 주문 대안', exact: true })
    .selectOption('add-beta');
  await panel(page).getByRole('textbox', { name: '불리한 슬리피지 (bp)', exact: true }).fill('0');
  await panel(page)
    .getByRole('textbox', { name: '봉 거래량 참여율 (bp)', exact: true })
    .fill('5000');
  await panel(page)
    .getByRole('textbox', { name: '모의 체결 수량 단위', exact: true })
    .fill('0.125');
}
async function submit(page: Page) {
  await panel(page).getByRole('button', { name: '가정 고정하고 모의 주문', exact: true }).click();
  await expect(
    panel(page).getByRole('region', { name: '모의 주문 상태' }).getByText('add-beta · 미체결'),
  ).toBeVisible();
}

test('paper is disabled by default and existing account viewing still works', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const mutations: string[] = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') mutations.push(request.url());
  });
  await open(page, data);
  await expect(
    panel(page).getByText('이 작업실에서는 모의 원장 저장이 꺼져 있습니다.', { exact: false }),
  ).toBeVisible();
  await expect(page.getByTestId('buying-power-USD')).toHaveText('3,500.5');
  expect(mutations).toEqual([]);
});
test('explicit cash stays exact and unknown valuation is never displayed as zero', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await fillCreate(page);
  await expect(panel(page).getByRole('textbox', { name: 'KRW 초기 모의 현금' })).toHaveValue('');
  await panel(page).getByRole('button', { name: '모의 원장 만들기', exact: true }).click();
  const detail = panel(page).getByRole('region', { name: '선택한 모의 원장', exact: true });
  await expect(
    detail.getByRole('cell', { name: '9,007,199,254,740,993.00100', exact: true }),
  ).toBeVisible();
  await expect(detail.getByText('USD 평가 가격 미확인: BETA', { exact: true })).toBeVisible();
  expect(state.creates[0]).toMatchObject({
    snapshot_id: data.first.id,
    mode: 'synthetic',
    initial_cash: [{ currency: 'USD', amount: '9007199254740993.00100' }],
  });
  expect(state.submits).toEqual([]);
});
test('fixed plan and profile progress to partial fill with exact costs and cancellation', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page, data, true);
  await fillIntent(page);
  await submit(page);
  expect(state.submits[0]).toMatchObject({
    id: bookId,
    body: {
      plan_id: planId,
      alternative_id: 'add-beta',
      expected_revision: 1,
      profile: {
        kind: 'next_observed_minute_close_v1',
        slippage_bps: '0',
        participation_bps: '5000',
        quantity_step: '0.125',
      },
    },
  });
  await panel(page)
    .getByRole('listbox', { name: '모의 진행에 사용할 분봉 캡처' })
    .selectOption(captureId);
  await panel(page).getByRole('button', { name: '선택 관측으로 모의 진행' }).click();
  await expect(panel(page).getByText('add-beta · 부분 체결', { exact: true })).toBeVisible();
  await expect(
    panel(page).getByRole('cell', { name: '1,011.987500000000001', exact: true }),
  ).toBeVisible();
  await expect(panel(page).getByRole('cell', { name: '-13.0125', exact: true })).toBeVisible();
  await expect(panel(page).getByRole('cell', { name: '0.5125 / 0', exact: true })).toBeVisible();
  expect(state.advances[0]).toMatchObject({
    id: bookId,
    body: { capture_ids: [captureId], expected_revision: 2 },
  });
  await panel(page)
    .getByRole('region', { name: '선택한 모의 원장', exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-22-paper.png') });
  await panel(page).getByRole('button', { name: '남은 모의 주문 취소' }).click();
  await expect(panel(page).getByText('add-beta · 취소', { exact: true })).toBeVisible();
  expect(state.cancels[0]).toMatchObject({ id: bookId, intentId, body: { expected_revision: 3 } });
});
test('lost create response keeps original cash and account after changing selection', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.lost = 'create';
  await open(page, data);
  await fillCreate(page);
  await panel(page).getByRole('button', { name: '모의 원장 만들기', exact: true }).click();
  await expect(panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' })).toBeEnabled();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page).getByRole('textbox', { name: 'USD 초기 모의 현금', exact: true }).fill('1');
  await panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' }).click();
  await expect(panel(page).getByRole('status')).toContainText('원장 만들기 결과를 저장했습니다');
  expect(state.creates).toHaveLength(2);
  expect(state.creates[1]).toEqual(state.creates[0]);
  await expect(panel(page).getByRole('combobox', { name: '모의 원장 선택' })).toHaveValue('');
  await expect(
    panel(page).getByRole('region', { name: '선택한 모의 원장', exact: true }),
  ).toHaveCount(0);
});
test('uncertain submit advance and cancel retry original body despite changed source or book', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page, data, true);
  await fillIntent(page);
  state.lost = 'submit';
  await panel(page).getByRole('button', { name: '가정 고정하고 모의 주문', exact: true }).click();
  await expect(panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' })).toBeEnabled();
  await panel(page)
    .getByRole('combobox', { name: '모의 주문할 저장 계획' })
    .selectOption(otherPlanId);
  await panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' }).click();
  await expect(panel(page).getByText('add-beta · 미체결', { exact: true })).toBeVisible();
  expect(state.submits[1]).toEqual(state.submits[0]);
  state.lost = 'advance';
  await panel(page)
    .getByRole('listbox', { name: '모의 진행에 사용할 분봉 캡처' })
    .selectOption(captureId);
  await panel(page).getByRole('button', { name: '선택 관측으로 모의 진행' }).click();
  await expect(panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' })).toBeEnabled();
  await panel(page)
    .getByRole('listbox', { name: '모의 진행에 사용할 분봉 캡처' })
    .selectOption(otherCaptureId);
  await panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' }).click();
  await expect(panel(page).getByText('add-beta · 부분 체결', { exact: true })).toBeVisible();
  expect(state.advances[1]).toEqual(state.advances[0]);
  state.lost = 'cancel';
  await panel(page).getByRole('button', { name: '남은 모의 주문 취소' }).click();
  await expect(panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' })).toBeEnabled();
  await panel(page).getByRole('combobox', { name: '모의 원장 선택' }).selectOption(secondBookId);
  await panel(page).getByRole('button', { name: '같은 모의 요청 결과 확인' }).click();
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 모의 원장', exact: true })
      .getByRole('heading', { name: '다른 모의 원장' }),
  ).toBeVisible();
  expect(state.cancels[1]).toEqual(state.cancels[0]);
  await expect(panel(page).getByText('add-beta · 취소', { exact: true })).toHaveCount(0);
});
test('late old book and failed refresh cannot display previous selected valuation', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  let release!: () => void;
  state.heldId = bookId;
  state.holdDetail = new Promise<void>((resolve) => {
    release = resolve;
  });
  await open(page, data, true);
  await panel(page).getByRole('combobox', { name: '모의 원장 선택' }).selectOption(secondBookId);
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 모의 원장', exact: true })
      .getByRole('heading', { name: '다른 모의 원장' }),
  ).toBeVisible();
  release();
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 모의 원장', exact: true })
      .getByRole('heading', { name: '합성 모의 원장' }),
  ).toHaveCount(0);
  state.detailError = true;
  await panel(page).getByRole('button', { name: '모의 자료 다시 읽기' }).click();
  await expect(panel(page).getByRole('alert')).toBeVisible();
  await expect(
    panel(page).getByRole('region', { name: '선택한 모의 원장', exact: true }),
  ).toHaveCount(0);
});
test('plan selection change clears alternative and hides late or failed plan assumptions', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page, data, true);
  await fillIntent(page);
  let release!: () => void;
  state.heldPlan = otherPlanId;
  state.holdPlan = new Promise<void>((resolve) => {
    release = resolve;
  });
  await panel(page)
    .getByRole('combobox', { name: '모의 주문할 저장 계획' })
    .selectOption(otherPlanId);
  await expect(
    panel(page).getByRole('combobox', { name: '모의 주문 대안', exact: true }),
  ).toHaveCount(0);
  await panel(page).getByRole('combobox', { name: '모의 주문할 저장 계획' }).selectOption(planId);
  release();
  await expect(
    panel(page).getByRole('combobox', { name: '모의 주문 대안', exact: true }),
  ).toHaveValue('');
  await expect(
    panel(page)
      .getByRole('combobox', { name: '모의 주문 대안', exact: true })
      .getByRole('option', { name: '다른 계획 대안' }),
  ).toHaveCount(0);
  await expect(
    panel(page).getByRole('button', { name: '가정 고정하고 모의 주문', exact: true }),
  ).toBeDisabled();
  state.planError = true;
  await panel(page).getByRole('button', { name: '모의 자료 다시 읽기' }).click();
  await expect(
    panel(page).getByRole('region', { name: '대안 모의 주문', exact: true }).getByRole('alert'),
  ).toBeVisible();
  await expect(
    panel(page).getByRole('combobox', { name: '모의 주문 대안', exact: true }),
  ).toHaveCount(0);
});
test('mobile paper form and exact valuation tables remain inside viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  await mocks(page, data, true);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, data, true);
  await fillIntent(page);
  await submit(page);
  await panel(page)
    .getByRole('region', { name: '선택한 모의 원장', exact: true })
    .scrollIntoViewIfNeeded();
  const overflow = await page.evaluate(() => ({
    width: document.documentElement.scrollWidth,
    items: [...document.querySelectorAll('body *')]
      .filter(
        (node) =>
          node.getBoundingClientRect().right > innerWidth && !node.closest('.table-container'),
      )
      .map((node) => ({
        tag: node.tagName,
        class: node.className,
        right: node.getBoundingClientRect().right,
        text: node.textContent?.slice(0, 80),
      }))
      .slice(-12),
  }));
  expect(overflow.width, JSON.stringify(overflow)).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath('feature-22-paper-mobile.png') });
  await panel(page)
    .getByRole('region', { name: '대안 모의 주문', exact: true })
    .scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
});
