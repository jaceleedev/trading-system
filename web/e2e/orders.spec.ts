import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  CapitalPlanResponse,
  FundingState,
  OrderIntentView,
  OrderIntentCreate,
  OrderModify,
  OrderCancel,
  OrderMutation,
  OrderObserve,
  OrderSimulate,
  PreparedOrder,
  BrokerScanSummary,
} from '../src/lib/api/types.gen';
const now = '2026-09-10T09:00:00Z';
const planId = 'a'.repeat(64);
const scanId = 'b'.repeat(64);
const reservationId = '24000000-0000-4000-8000-000000000001';
const intentId = '24000000-0000-4000-8000-000000000002';
const otherId = '24000000-0000-4000-8000-000000000003';
const operationId = '24000000-0000-4000-8000-000000000004';
const panel = (page: Page) => page.getByRole('region', { name: '주문 의도 관리', exact: true });
const detail = (page: Page) => panel(page).getByRole('region', { name: '선택 주문 의도' });
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
function plan(data: Fixture): CapitalPlanResponse {
  return {
    id: planId,
    record: {
      kind: 'capital_plan',
      schema_version: 1,
      recorded_at: now,
      request: {
        snapshot_id: data.first.id,
        source: { kind: 'decision', id: 'e'.repeat(64) },
        mode: 'synthetic',
        funding: [{ currency: 'USD', limit_amount: '1000', reserve_amount: '0' }],
        alternatives: [
          {
            key: 'buy-alpha',
            label: 'ALPHA 매수 대안',
            rationale: '합성 주문 검사',
            legs: [
              {
                action: 'buy',
                symbol: 'ALPHA',
                market: 'US',
                currency: 'USD',
                quantity: '2',
                price: '100.01',
                fee_bps: '0',
                fixed_fee: '0',
                tax_bps: '0',
                rationale: '합성 자료',
              },
            ],
          },
        ],
      },
      snapshot: data.context.account!.snapshot,
      source_context: {
        kind: 'decision',
        id: 'e'.repeat(64),
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
function funding(data: Fixture): FundingState {
  return {
    account_seq: '101',
    provider: 'toss',
    pools: [],
    omitted_reservation_count: 0,
    expected_pool_revisions: {},
    execution_ready: false,
    reservations: [
      {
        id: reservationId,
        account_seq: '101',
        alternative_id: 'buy-alpha',
        plan_id: planId,
        mode: 'synthetic',
        created_at: now,
        status: 'active',
        request_key: reservationId,
        snapshot_ids: [data.first.id],
        requirements: { cash: [{ currency: 'USD', amount: '200.02' }], holdings: [] },
        pool_revisions: {},
        released_at: null,
        replaced_by: null,
        execution_ready: false,
      },
    ],
  };
}
function prepared(): PreparedOrder {
  return {
    operation: 'create',
    account_seq: '101',
    market: 'US',
    currency: 'USD',
    method: 'POST',
    route_template: '/orders/us',
    path_parameters: {},
    body: {
      symbol: 'ALPHA',
      side: 'BUY',
      quantity: '2',
      price: '100.01',
      orderType: 'LIMIT',
      timeInForce: 'DAY',
    },
    contract_sha256: 'c'.repeat(64),
    request_sha256: 'd'.repeat(64),
    transmission_enabled: false,
    validation: {
      execution_ready: false,
      source_authenticity: false,
      unverified_checks: ['live_buying_power'],
    },
  };
}
function intent(data: Fixture, id = intentId): OrderIntentView {
  const value = prepared();
  return {
    id,
    account_seq: id === intentId ? '101' : '202',
    mode: 'synthetic',
    plan_id: planId,
    alternative_id: 'buy-alpha',
    reservation_id: reservationId,
    revision: 1,
    status: 'active',
    legs: [
      {
        index: 0,
        leg: plan(data).record.request.alternatives[0].legs[0],
        prepared: value,
        broker_order_ids: [],
        observation: null,
        observation_state: 'unobserved',
      },
    ],
    operations: [
      {
        id: operationId,
        intent_id: id,
        leg_index: 0,
        kind: 'create',
        state: 'prepared',
        prepared: value,
        outcome: null,
        created_at: now,
        updated_at: now,
      },
    ],
    events: [],
    event_total_count: 0,
    event_omitted_count: 0,
    created_at: now,
    updated_at: now,
    orders_enabled: false,
    execution_ready: false,
    reservation_held: true,
  };
}
function scan(): BrokerScanSummary {
  return {
    id: scanId,
    mode: 'synthetic',
    account_seq: '101',
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
type Mutation =
  OrderIntentCreate | OrderModify | OrderCancel | OrderMutation | OrderObserve | OrderSimulate;
/** This state models HTTP receipts only. It does not execute an adapter or mutate account fixtures. */
async function mocks(page: Page, data: Fixture, existing = false) {
  const state = {
    intents: existing ? [intent(data), intent(data, otherId)] : ([] as OrderIntentView[]),
    funding: funding(data),
    plan: plan(data),
    synthetic: true,
    calls: [] as { kind: string; id: string; body: Mutation }[],
    lost: '',
    reject: '',
    detailError: false,
    listError: false,
    planError: false,
    heldId: '',
    heldResponse: undefined as Promise<void> | undefined,
    heldCreate: undefined as Promise<void> | undefined,
    receipts: new Map<string, OrderIntentView>(),
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        jobs_enabled: true,
        read_only: false,
        synthetic: state.synthetic,
      },
    });
  });
  await page.route(/\/api\/v1\/capital-plans(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        items: [
          {
            id: planId,
            recorded_at: now,
            mode: state.plan.record.request.mode,
            snapshot_id: data.first.id,
            source: state.plan.record.request.source,
            alternative_count: 1,
            eligible_count: 1,
          },
        ],
        total_count: 1,
        omitted_count: 0,
      },
    }),
  );
  await page.route(`**/api/v1/capital-plans/${planId}`, (route) =>
    route.fulfill(
      state.planError
        ? {
            status: 503,
            json: { error: { code: 'capital_unavailable', message: 'Synthetic unavailable' } },
          }
        : { json: state.plan },
    ),
  );
  await page.route(/\/api\/v1\/funding\?.*$/, (route) => {
    const seq = new URL(route.request().url()).searchParams.get('account_seq');
    return route.fulfill({
      json:
        seq === '101' ? state.funding : { ...state.funding, account_seq: seq, reservations: [] },
    });
  });
  await page.route(/\/api\/v1\/broker\/scans(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [scan()], total_count: 1, omitted_count: 0 } }),
  );
  await page.route(/\/api\/v1\/order-intents(?:\/.*|\?.*)?$/, async (route) => {
    const request = route.request();
    const parts = new URL(request.url()).pathname.split('/').slice(4);
    const id = parts[0];
    if (request.method() === 'GET') {
      if (!id) {
        await route.fulfill(
          state.listError
            ? {
                status: 503,
                json: {
                  error: { code: 'order_store_unavailable', message: 'Synthetic unavailable' },
                },
              }
            : {
                json: { items: state.intents, total_count: state.intents.length, omitted_count: 0 },
              },
        );
        return;
      }
      const response = structuredClone(state.intents.find((item) => item.id === id));
      if (id === state.heldId && state.heldResponse) await state.heldResponse;
      await route
        .fulfill(
          state.detailError
            ? {
                status: 503,
                json: {
                  error: { code: 'order_store_unavailable', message: 'Synthetic unavailable' },
                },
              }
            : { json: response },
        )
        .catch(() => {});
      return;
    }
    const body = request.postDataJSON() as Mutation;
    const kind = !id ? 'create' : parts[1] === 'operations' ? 'simulate' : parts[1];
    state.calls.push({ kind, id: id ?? '', body });
    if (state.reject) {
      const code = state.reject;
      state.reject = '';
      await route.fulfill({
        status: 409,
        json: { error: { code, message: 'Synthetic validation rejection' } },
      });
      return;
    }
    let result = state.receipts.get(body.request_key);
    if (!result) {
      if (kind === 'create') {
        state.intents.push(intent(data));
        result = state.intents.at(-1)!;
      } else {
        result = state.intents.find((item) => item.id === id)!;
        result.revision += 1;
        if (kind === 'simulate') {
          const operation = result.operations.find((item) => item.id === parts[2])!;
          const scenario = (body as OrderSimulate).scenario;
          operation.state =
            scenario === 'accept'
              ? 'acknowledged'
              : scenario === 'reject' || scenario === 'before_send_failure'
                ? 'rejected'
                : 'ambiguous';
          operation.outcome = {
            status: operation.state,
            http_status:
              scenario === 'response_lost' || scenario === 'before_send_failure'
                ? null
                : scenario === 'accept'
                  ? 200
                  : 400,
            order_id: scenario === 'accept' ? 'SYNTHETIC/ORDER,A' : null,
            client_order_id: null,
            original_order_id: null,
            error_code:
              scenario === 'accept'
                ? null
                : scenario === 'reject'
                  ? 'insufficient-buying-power'
                  : `synthetic_${scenario}`,
            source_authenticity: false,
            synthetic: true,
            transmitted: false,
            request_sha256: operation.prepared.request_sha256,
            synthetic_dispatched: scenario !== 'before_send_failure',
          };
          if (scenario === 'accept') result.legs[0].broker_order_ids.push('SYNTHETIC/ORDER,A');
        } else if (kind === 'modify' || kind === 'cancel') {
          const original = prepared();
          const change = body as OrderModify;
          result.operations.push({
            id: `24000000-0000-4000-8000-${String(10 + result.operations.length).padStart(12, '0')}`,
            intent_id: id,
            leg_index: 0,
            kind,
            state: 'prepared',
            prepared: {
              ...original,
              operation: kind,
              body:
                kind === 'modify'
                  ? {
                      price: change.price,
                      ...(change.quantity === null ? {} : { quantity: change.quantity! }),
                    }
                  : {},
            },
            outcome: null,
            created_at: now,
            updated_at: now,
          });
        } else if (kind === 'recover')
          result.operations
            .filter((item) => item.state === 'dispatching')
            .forEach((item) => (item.state = 'ambiguous'));
        else if (kind === 'abort') {
          result.status = 'aborted';
          result.reservation_held = false;
          result.operations.forEach((item) => (item.state = 'aborted'));
        } else if (kind === 'observe') {
          result.legs[0].observation_state = 'partially_filled';
          result.legs[0].observation = {
            scan_id: scanId,
            observed_at: now,
            order: {
              orderId: 'SYNTHETIC/ORDER,A',
              symbol: 'ALPHA',
              side: 'BUY',
              orderType: 'LIMIT',
              timeInForce: 'DAY',
              status: 'PARTIAL_FILLED',
              quantity: '2',
              currency: 'USD',
              price: '100.01',
              orderedAt: now,
              execution: {
                filledQuantity: '0.125',
                filledAmount: '12.500000000000000125',
                averageFilledPrice: '100.01',
                commission: null,
                tax: null,
                filledAt: null,
                settlementDate: null,
              },
            },
          };
        }
      }
      state.receipts.set(body.request_key, structuredClone(result));
    }
    if (kind === 'create' && state.heldCreate) await state.heldCreate;
    if (state.lost === kind) {
      state.lost = '';
      await route.fulfill({
        status: 503,
        json: { error: { code: 'order_store_unavailable', message: 'Synthetic response loss' } },
      });
    } else await route.fulfill({ json: result });
  });
  return state;
}
async function open(page: Page) {
  await page.goto('/');
  await panel(page).getByText('주문 의도 관리', { exact: true }).click();
}
async function creation(page: Page, data: Fixture) {
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByText('계획에서 주문 의도 만들기', { exact: true }).click();
  await panel(page).getByLabel('주문에 연결할 저장 계획').selectOption(planId);
  await panel(page)
    .getByRole('combobox', { name: '주문 대안', exact: true })
    .selectOption('buy-alpha');
  await panel(page).getByLabel('연결할 활성 배정').selectOption(reservationId);
}
async function select(page: Page, id = intentId) {
  await panel(page)
    .getByRole('combobox', { name: '저장된 주문 의도', exact: true })
    .selectOption(id);
  await expect(detail(page)).toBeVisible();
}

test('orders stay disabled without jobs while saved account reading remains available', async ({
  page,
}) => {
  await open(page);
  await expect(panel(page).getByText(/주문 의도 저장이 꺼져/)).toBeVisible();
  await expect(
    panel(page).getByRole('button', { name: '주문 의도 저장', exact: true }),
  ).toHaveCount(0);
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toBeEnabled();
});
test('explicit account, alternative and active allocation create one intent; lost response retries same key', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.lost = 'create';
  await open(page);
  await panel(page).getByText('계획에서 주문 의도 만들기', { exact: true }).click();
  await expect(panel(page).getByLabel('주문에 연결할 저장 계획')).toBeDisabled();
  await panel(page).getByText('계획에서 주문 의도 만들기', { exact: true }).click();
  await creation(page, data);
  await expect(panel(page).getByText('100.01', { exact: true })).toBeVisible();
  await panel(page).getByRole('button', { name: '주문 의도 저장', exact: true }).click();
  await panel(page).getByRole('button', { name: '같은 주문 요청 확인' }).click();
  await expect(detail(page).getByText('연결된 주문 상태 관측이 없습니다.')).toBeVisible();
  expect(state.calls).toHaveLength(2);
  expect(state.calls[0].body).toEqual(state.calls[1].body);
  expect(state.intents).toHaveLength(1);
  expect(state.calls[0].body).toMatchObject({
    plan_id: planId,
    alternative_id: 'buy-alpha',
    reservation_id: reservationId,
  });
  await expect(detail(page).getByText('의도 저장', { exact: true })).toBeVisible();
});
test('synthetic acknowledgement is separate from explicitly linked cumulative broker observations', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page);
  await select(page);
  await expect(detail(page).getByText('합성 주문 응답 검사', { exact: true })).toBeVisible();
  await detail(page).getByRole('button', { name: '이 의도에 합성 응답 적용' }).click();
  await expect(detail(page).getByText('응답 확인', { exact: true })).toBeVisible();
  await expect(detail(page).getByText('연결된 주문 상태 관측이 없습니다.')).toBeVisible();
  await detail(page).getByLabel('연결할 브로커 관측').selectOption(scanId);
  await detail(page).getByRole('button', { name: '선택 관측 연결' }).click();
  await expect(detail(page).getByText(/누적 체결 수량 0.125/)).toBeVisible();
  await expect(detail(page).getByText(/12.500000000000000125/)).toBeVisible();
  await expect(
    detail(page).getByText(/누적 수수료\s+미확인\s+·\s+누적 세금\s+미확인/),
  ).toBeVisible();
  expect(state.calls.map((item) => item.kind)).toEqual(['simulate', 'observe']);
  expect(state.calls[1].body).toMatchObject({ expected_revision: 2, scan_id: scanId });
  await panel(page).screenshot({ path: testInfo.outputPath('feature-24-orders-desktop.png') });
});
test('ordinary workspace has no synthetic or real transmission controls', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  state.synthetic = false;
  state.intents.forEach((item) => (item.mode = 'prospective'));
  await open(page);
  await select(page);
  await expect(detail(page).getByText('합성 주문 응답 검사', { exact: true })).toHaveCount(0);
  await expect(detail(page).getByRole('button', { name: /전송|응답 적용/ })).toHaveCount(0);
  expect(state.calls).toEqual([]);
});
test('response loss remains ambiguous and does not resend; recovery records uncertainty only', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page);
  await select(page);
  await detail(page).getByLabel('합성 응답 유형').selectOption('response_lost');
  state.lost = 'simulate';
  await detail(page).getByRole('button', { name: '이 의도에 합성 응답 적용' }).click();
  await panel(page).getByRole('button', { name: '같은 주문 요청 확인' }).click();
  await expect(detail(page).getByText('결과 미확정', { exact: true })).toBeVisible();
  await expect(detail(page).getByRole('button', { name: '이 의도에 합성 응답 적용' })).toHaveCount(
    0,
  );
  await expect(detail(page).getByText(/연결 배정 유지/)).toBeVisible();
  expect(state.calls[0].body).toEqual(state.calls[1].body);
  state.intents[1].operations[0].state = 'dispatching';
  await select(page, otherId);
  await detail(page).getByRole('button', { name: '중단된 처리를 미확정으로 기록' }).click();
  await expect(detail(page).getByText('결과 미확정', { exact: true })).toBeVisible();
  expect(state.calls.map((item) => item.kind)).toEqual(['simulate', 'simulate', 'recover']);
});
test('known broker IDs allow local US price-only modifications and cancellation; mobile stays contained', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  state.intents[0].legs[0].broker_order_ids = ['SYNTHETIC/ORDER,A'];
  state.intents[0].operations[0].state = 'acknowledged';
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page);
  await select(page);
  await detail(page).getByText('정정·취소 의도', { exact: true }).click();
  await detail(page).getByLabel('정정·취소 대상').selectOption('0');
  await expect(detail(page).getByLabel('정정 수량', { exact: true })).toHaveCount(0);
  await detail(page).getByLabel('정정 가격', { exact: true }).fill('99.01');
  await detail(page).getByRole('button', { name: '정정 의도 저장', exact: true }).click();
  await expect.poll(() => state.calls.length).toBe(1);
  expect(state.calls[0].body).toMatchObject({
    price: '99.01',
    quantity: null,
    leg_index: 0,
    expected_revision: 1,
  });
  await expect(detail(page).getByText('의도 저장', { exact: true })).toBeVisible();
  await detail(page).getByText('정정·취소 의도', { exact: true }).click();
  await detail(page).getByRole('button', { name: '취소 의도 저장', exact: true }).click();
  await expect.poll(() => state.calls.length).toBe(2);
  expect(state.calls[1].body).toMatchObject({ leg_index: 0, expected_revision: 2 });
  await expect(detail(page).getByText('1번 항목 · 취소 의도', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).screenshot({ path: testInfo.outputPath('feature-24-orders-mobile.png') });
});
test('stale detail and server errors hide old orders; delayed create does not select another account intent', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  await open(page);
  await select(page);
  let release!: () => void;
  state.heldId = otherId;
  state.heldResponse = new Promise((resolve) => (release = resolve));
  await panel(page)
    .getByRole('combobox', { name: '저장된 주문 의도', exact: true })
    .selectOption(otherId);
  await expect(detail(page)).toHaveCount(0);
  await panel(page)
    .getByRole('combobox', { name: '저장된 주문 의도', exact: true })
    .selectOption(intentId);
  await expect(
    detail(page).getByRole('heading', { name: '계좌 101 · 합성 주문 의도' }),
  ).toBeVisible();
  release();
  state.detailError = true;
  await panel(page).getByRole('button', { name: '주문 자료 재조회' }).click();
  await expect(detail(page)).toHaveCount(0);
  await expect(panel(page).getByRole('alert')).toContainText('저장소에 연결하지 못했습니다');
  state.detailError = false;
  await panel(page).getByRole('button', { name: '주문 자료 재조회' }).click();
  let releaseCreate!: () => void;
  state.heldCreate = new Promise((resolve) => (releaseCreate = resolve));
  await creation(page, data);
  await panel(page).getByRole('button', { name: '주문 의도 저장', exact: true }).click();
  await expect.poll(() => state.calls.length).toBe(1);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page)
    .getByRole('combobox', { name: '저장된 주문 의도', exact: true })
    .selectOption(otherId);
  releaseCreate();
  await expect(
    detail(page).getByRole('heading', { name: '계좌 202 · 합성 주문 의도' }),
  ).toBeVisible();
});
test('mismatched accounts and inactive allocations cannot create; local abort releases an untouched intent', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  state.funding.reservations[0].status = 'released';
  await open(page);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByText('계획에서 주문 의도 만들기', { exact: true }).click();
  await panel(page).getByLabel('주문에 연결할 저장 계획').selectOption(planId);
  await panel(page)
    .getByRole('combobox', { name: '주문 대안', exact: true })
    .selectOption('buy-alpha');
  await expect(panel(page).getByText(/활성 배정이 없습니다/)).toBeVisible();
  await expect(
    panel(page).getByRole('button', { name: '주문 의도 저장', exact: true }),
  ).toBeDisabled();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await expect(panel(page).getByText(/현재 선택 계좌와 작업실 모드에 맞는 계획/)).toBeVisible();
  await select(page);
  await detail(page).getByRole('button', { name: '전달 전 의도 중단', exact: true }).click();
  await expect(detail(page).getByText(/연결 배정 해제/)).toBeVisible();
  expect(state.calls.map((item) => item.kind)).toEqual(['abort']);
});

test('native validation rejection frees the form and KR changes include an explicit quantity', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, true);
  state.intents[0].legs[0].leg.market = 'KR';
  state.intents[0].legs[0].leg.currency = 'KRW';
  state.intents[0].legs[0].leg.symbol = '005930';
  state.intents[0].legs[0].broker_order_ids = ['SYNTHETIC-KR'];
  state.intents[0].operations[0].state = 'acknowledged';
  await open(page);
  await select(page);
  await detail(page).getByText('정정·취소 의도', { exact: true }).click();
  await detail(page).getByLabel('정정·취소 대상').selectOption('0');
  await detail(page).getByLabel('정정 가격', { exact: true }).fill('90.1');
  await detail(page).getByLabel('정정 수량', { exact: true }).fill('1');
  state.reject = 'integer_krw_price_required';
  await detail(page).getByRole('button', { name: '정정 의도 저장', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('정수 원 단위');
  await expect(panel(page).getByRole('button', { name: '같은 주문 요청 확인' })).toHaveCount(0);
  await detail(page).getByLabel('정정 가격', { exact: true }).fill('90');
  await detail(page).getByRole('button', { name: '정정 의도 저장', exact: true }).click();
  await expect(detail(page).getByText('1번 항목 · 정정 의도', { exact: true })).toBeVisible();
  expect(state.calls[1].body).toMatchObject({ price: '90', quantity: '1', expected_revision: 1 });
  expect(state.calls[0].body.request_key).not.toBe(state.calls[1].body.request_key);
});

for (const scenario of ['reject', 'before_send_failure'] as const) {
  test(`synthetic ${scenario} remains a known failure without any observed fill`, async ({
    page,
    request,
  }) => {
    const data = await fixture(request);
    const state = await mocks(page, data, true);
    await open(page);
    await select(page);
    await detail(page)
      .getByRole('combobox', { name: '합성 응답 유형', exact: true })
      .selectOption(scenario);
    await detail(page).getByRole('button', { name: '이 의도에 합성 응답 적용' }).click();
    await expect(
      detail(page)
        .getByText(scenario === 'reject' ? '거부 응답' : '전송 전 실패', { exact: true })
        .last(),
    ).toBeVisible();
    await expect(
      detail(page).getByRole('button', { name: '이 의도에 합성 응답 적용' }),
    ).toHaveCount(0);
    await expect(detail(page).getByText('연결된 주문 상태 관측이 없습니다.')).toBeVisible();
    await expect(
      detail(page).getByRole('button', { name: '전달 전 의도 중단', exact: true }),
    ).toBeEnabled();
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0].body).toMatchObject({ scenario, expected_revision: 1 });
  });
}
