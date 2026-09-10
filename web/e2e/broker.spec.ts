import { test, expect, type Page, type APIRequestContext } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  BrokerCoverage,
  BrokerScanView,
  OpenOrder,
  ReconciliationRequest,
  ReconciliationResponse,
  JobView,
} from '../src/lib/api/types.gen';
import type { BrokerSubmission } from '../src/lib/broker';
const now = '2026-09-10T09:00:00Z';
const beforeTime = '2026-09-10T08:58:00Z';
const beforeId = 'a'.repeat(64);
const afterId = 'b'.repeat(64);
const afterSnapshot = 'c'.repeat(64);
const reportId = 'd'.repeat(64);
const otherReportId = 'e'.repeat(64);
const panel = (page: Page) => page.getByRole('region', { name: '브로커 관측 대조', exact: true });
async function fixture(request: APIRequestContext) {
  const accounts = (await (
    await request.get('/api/v1/account-snapshots')
  ).json()) as AccountSnapshotsResponse;
  const first = accounts.items.find((item) => item.account_seq === '101')!;
  const second = accounts.items.find((item) => item.account_seq === '202')!;
  const context = (await (
    await request.get(`/api/v1/context?snapshot_id=${first.id}`)
  ).json()) as InvestmentContext;
  return { accounts, first, second, context };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;
function coverage(): BrokerCoverage {
  return {
    open_complete: true,
    closed_complete: false,
    details_complete: true,
    complete: false,
    closed_pages: 10,
    stop_reason: 'page_limit',
    unresolved_detail_ids: [],
    ordered_at_from: '2026-09-01',
    ordered_at_to: '2026-09-10',
    date_basis: 'orderedAt_KST',
    atomic_account_instant: false,
    all_order_types: false,
    individual_fills: false,
    order_lineage: false,
    source_authenticity: false,
  };
}
function order(later = false): OpenOrder {
  return {
    orderId: 'ORDER/OPAQUE,A',
    symbol: 'BETA',
    side: 'BUY',
    orderType: 'MARKET',
    timeInForce: 'DAY',
    status: 'PARTIAL_FILLED',
    quantity: '1',
    currency: 'USD',
    price: null,
    orderedAt: '2026-09-01T09:00:00+09:00',
    execution: {
      filledQuantity: later ? '0.375' : '0.125',
      averageFilledPrice: '100.000000000000001',
      filledAmount: later ? '37.5' : '12.5',
      commission: later ? '0.4' : '0.5',
      tax: null,
      filledAt: later ? now : beforeTime,
      settlementDate: null,
    },
  };
}
function scan(id: string): BrokerScanView {
  return {
    id,
    kind: 'broker_scan',
    schema_version: 1,
    mode: 'synthetic',
    account_seq: '101',
    recorded_at: id === beforeId ? beforeTime : now,
    collection_started_at: id === beforeId ? beforeTime : now,
    collection_completed_at: id === beforeId ? beforeTime : now,
    request: {
      account_seq: '101',
      mode: 'synthetic',
      from_date: '2026-09-01',
      to_date: '2026-09-10',
      symbol: null,
      max_pages: 10,
      page_size: 100,
      detail_order_ids: [],
    },
    observation_ids: ['f'.repeat(64)],
    coverage: coverage(),
    orders: [
      {
        order: order(id === afterId),
        observation_id: 'f'.repeat(64),
        retrieved_at: id === beforeId ? beforeTime : now,
        recorded_at: id === beforeId ? beforeTime : now,
        source_group: 'CLOSED',
      },
    ],
    warnings: ['Synthetic fixture: selected scope only.'],
  };
}
function report(body: ReconciliationRequest, data: Fixture, id = reportId): ReconciliationResponse {
  const before = scan(beforeId),
    after = scan(afterId);
  const snapshot = (identity: string, time: string) => ({
    id: identity,
    collection_started_at: time,
    collection_completed_at: time,
    holdings_observed_at: time,
    buying_power_observed_at: { USD: time, KRW: time },
    contract_sha256: 'f'.repeat(64),
  });
  const source = (value: BrokerScanView) => ({
    id: value.id,
    mode: value.mode,
    collection_started_at: value.collection_started_at,
    collection_completed_at: value.collection_completed_at,
    recorded_at: value.recorded_at,
    request: value.request,
    observation_ids: value.observation_ids,
  });
  const version = (value: BrokerScanView) => ({
    order: value.orders[0].order,
    observed_at: value.collection_completed_at,
    recorded_at: value.recorded_at,
    observation_ids: value.observation_ids,
    groups_seen: ['CLOSED' as const],
  });
  return {
    id,
    record: {
      kind: 'broker_reconciliation',
      schema_version: 1,
      mode: body.mode,
      as_of: now,
      account_seq: '101',
      request: body,
      sources: {
        before_snapshot: snapshot(body.before_snapshot_id, beforeTime),
        after_snapshot: snapshot(body.after_snapshot_id, now),
        before_scan: body.before_scan_id ? source(before) : null,
        after_scan: source(after),
      },
      orders: [
        {
          order_key: 'f'.repeat(64),
          order_id: 'ORDER/OPAQUE,A',
          before: body.before_scan_id ? version(before) : null,
          after: version(after),
          before_conflict_observation_ids: [],
          after_conflict_observation_ids: [],
          deltas: {
            filled_quantity: body.before_scan_id ? '0.250' : null,
            filled_amount: body.before_scan_id ? '25' : null,
            commission: body.before_scan_id ? '-0.1' : null,
            tax: null,
          },
          classification: body.before_scan_id
            ? ['cumulative_increase', 'financial_revision']
            : ['baseline_only'],
          origin: 'unattributed',
          lineage_known: false,
          individual_fills_available: false,
        },
      ],
      holdings: [
        {
          market: 'US',
          symbol: 'BETA',
          before_currency: 'USD',
          after_currency: 'USD',
          before_present: true,
          after_present: true,
          before_quantity: '0.125',
          after_quantity: '0.375',
          quantity_delta: '0.250',
          classification: 'quantity_changed',
          absence_zero_assumed: false,
        },
        {
          market: 'KR',
          symbol: 'ALPHA',
          before_currency: 'KRW',
          after_currency: null,
          before_present: true,
          after_present: false,
          before_quantity: '100',
          after_quantity: null,
          quantity_delta: null,
          classification: 'disappeared',
          absence_zero_assumed: false,
        },
      ],
      buying_power: [
        {
          currency: 'USD',
          before_amount: '9007199254740993.001',
          after_amount: '9007199254740992.000',
          delta: '-1.001',
          semantics: 'buying_capacity_not_cash',
        },
      ],
      coverage: {
        before_scan: body.before_scan_id ? before.coverage : null,
        after_scan: after.coverage,
        before_account: data.context.account!.snapshot.coverage,
        after_account: data.context.account!.snapshot.coverage,
        atomic_account_instant: false,
        all_account_orders: false,
        individual_fills_available: false,
        order_lineage_known: false,
        source_authenticity_verified: false,
        comparison_time_alignment: 'non_atomic',
        holdings_absence_implies_zero: false,
      },
      counts: {
        orders: 1,
        unchanged_orders: 0,
        changed_orders: 1,
        baseline_orders: body.before_scan_id ? 0 : 1,
        absent_orders: 0,
        conflicted_orders: 0,
        holdings: 2,
        changed_holdings: 2,
        unknown_holding_deltas: 1,
      },
      warnings: ['Synthetic comparison; no individual fills or PnL.'],
      individual_fills_created: false,
      pnl_computed: false,
      orders_enabled: false,
    },
  };
}
/** Routes use isolated synthetic projections. No broker, credentials or real worker are called. */
async function mocks(page: Page, data: Fixture, { enabled = true, synthetic = true } = {}) {
  const state = {
    previews: [] as ReconciliationRequest[],
    saves: [] as ReconciliationRequest[],
    submissions: [] as BrokerSubmission[],
    reports: [] as ReconciliationResponse[],
    jobs: [] as JobView[],
    lostSave: false,
    lostJob: false,
    previewError: false,
    scanError: false,
    reportError: false,
    heldScan: '',
    holdScan: undefined as Promise<void> | undefined,
    holdPreview: undefined as Promise<void> | undefined,
    heldReport: '',
    holdReport: undefined as Promise<void> | undefined,
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), jobs_enabled: enabled, read_only: !enabled, synthetic },
    });
  });
  await page.route('**/api/v1/account-snapshots', (route) =>
    route.fulfill({
      json: {
        items: [
          ...data.accounts.items,
          {
            ...data.first,
            id: afterSnapshot,
            collection_started_at: now,
            collection_completed_at: now,
          },
        ],
      },
    }),
  );
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route(/\/api\/v1\/broker\/scans(?:\?.*)?$/, (route) => {
    const seq = new URL(route.request().url()).searchParams.get('account_seq');
    const values = !seq || seq === '101' ? [scan(beforeId), scan(afterId)] : [];
    return route.fulfill({
      json: {
        items: values.map((value) => ({
          ...value,
          orders_count: value.orders.length,
          observations_count: value.observation_ids.length,
        })),
        total_count: values.length,
        omitted_count: 0,
        invalid_count: 0,
      },
    });
  });
  await page.route(/\/api\/v1\/broker\/scans\/[a-f0-9]{64}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    if (id === state.heldScan && state.holdScan) await state.holdScan;
    await route
      .fulfill(
        state.scanError
          ? {
              status: 500,
              json: { error: { code: 'internal_error', message: 'Synthetic failure' } },
            }
          : { json: scan(id) },
      )
      .catch(() => {});
  });
  await page.route('**/api/v1/reconciliations/preview', async (route) => {
    const body = route.request().postDataJSON() as ReconciliationRequest;
    state.previews.push(body);
    if (state.holdPreview) await state.holdPreview;
    await route.fulfill(
      state.previewError
        ? { status: 500, json: { error: { code: 'internal_error', message: 'Synthetic failure' } } }
        : { json: report(body, data) },
    );
  });
  await page.route(/\/api\/v1\/reconciliations(?:\?.*)?$/, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        json: {
          items: state.reports.map((value) => ({
            id: value.id,
            mode: value.record.mode,
            account_seq: value.record.account_seq,
            as_of: value.record.as_of,
            before_snapshot_id: value.record.request.before_snapshot_id,
            after_snapshot_id: value.record.request.after_snapshot_id,
            before_scan_id: value.record.request.before_scan_id,
            after_scan_id: value.record.request.after_scan_id,
            counts: value.record.counts,
          })),
          total_count: state.reports.length,
          omitted_count: 0,
          invalid_count: 0,
        },
      });
      return;
    }
    const body = route.request().postDataJSON() as ReconciliationRequest;
    state.saves.push(body);
    if (!state.reports.length) state.reports.push(report(body, data));
    if (state.lostSave) {
      state.lostSave = false;
      await route.abort('failed');
    } else await route.fulfill({ json: state.reports[0] });
  });
  await page.route(/\/api\/v1\/reconciliations\/[a-f0-9]{64}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    if (id === state.heldReport && state.holdReport) await state.holdReport;
    await route
      .fulfill(
        state.reportError
          ? {
              status: 500,
              json: { error: { code: 'internal_error', message: 'Synthetic failure' } },
            }
          : { json: state.reports.find((value) => value.id === id) },
      )
      .catch(() => {});
  });
  await page.route('**/api/v1/jobs/status', (route) => route.fulfill({ json: { enabled } }));
  await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ json: { items: state.jobs } });
      return;
    }
    const body = route.request().postDataJSON() as BrokerSubmission;
    state.submissions.push(body);
    if (!state.jobs.length)
      state.jobs.push({
        id: '23000000-0000-4000-8000-000000000001',
        workspace_key: 'f'.repeat(64),
        kind: 'broker-sync',
        parameters: body.parameters,
        request_key: body.request_key,
        status: 'queued',
        available_at: now,
        created_at: now,
        updated_at: now,
        finished_at: null,
        max_attempts: 3,
        attempt_count: 0,
        cancel_requested: false,
        lease_expires_at: null,
        result: null,
        error_code: null,
        attempts: [],
      });
    if (state.lostJob) {
      state.lostJob = false;
      await route.abort('failed');
    } else await route.fulfill({ json: { job: state.jobs[0] } });
  });
  return state;
}
async function open(page: Page, data: Fixture) {
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByText('브로커 관측 대조', { exact: true }).click();
}
async function choose(page: Page, data: Fixture, baseline = true) {
  await panel(page)
    .getByRole('combobox', { name: '이전 계좌 관측', exact: true })
    .selectOption(data.first.id);
  await panel(page)
    .getByRole('combobox', { name: '이후 계좌 관측', exact: true })
    .selectOption(afterSnapshot);
  await panel(page)
    .getByRole('combobox', { name: '이전 스캔 (선택)', exact: true })
    .selectOption(baseline ? beforeId : '');
  await panel(page).getByRole('combobox', { name: '이후 스캔', exact: true }).selectOption(afterId);
}
async function calculate(page: Page) {
  await panel(page).getByRole('button', { name: '관측 대조 계산', exact: true }).click();
  await expect(
    panel(page).getByRole('region', { name: '대조 계산 결과', exact: true }),
  ).toBeVisible();
}

test('default empty broker stores preserve the saved account and disable collection', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  await open(page, data);
  await expect(
    panel(page).getByText('저장된 브로커 스캔이 없습니다.', { exact: true }),
  ).toBeVisible();
  await panel(page).getByText('브로커 읽기 스캔 접수', { exact: true }).first().click();
  await expect(
    panel(page).getByRole('button', { name: '브로커 읽기 스캔 접수', exact: true }),
  ).toBeDisabled();
  await expect(page.getByTestId('buying-power-USD')).toHaveText('3,500.5');
});
test('synthetic workspace blocks collection while scans and baseline comparison work without jobs', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, { enabled: false, synthetic: true });
  await open(page, data);
  await panel(page).getByText('브로커 읽기 스캔 접수', { exact: true }).first().click();
  await expect(
    panel(page).getByRole('button', { name: '브로커 읽기 스캔 접수', exact: true }),
  ).toBeDisabled();
  await panel(page)
    .getByRole('combobox', { name: '확인할 저장 스캔', exact: true })
    .selectOption(afterId);
  const scan = panel(page).getByRole('region', { name: '선택한 스캔 상세', exact: true });
  await expect(scan.getByText('조회 중단: 설정한 페이지 상한 도달')).toBeVisible();
  await expect(scan.getByRole('cell', { name: '100.000000000000001', exact: true })).toBeVisible();
  await choose(page, data, false);
  await calculate(page);
  await expect(
    panel(page)
      .getByRole('region', { name: '대조 계산 결과', exact: true })
      .getByRole('cell', { name: /이전 비교값 없음/ }),
  ).toBeVisible();
  await expect(
    panel(page).getByRole('button', { name: '대조 결과 저장', exact: true }),
  ).toBeDisabled();
  expect(state.submissions).toEqual([]);
  expect(state.previews[0].before_scan_id).toBeNull();
});
test('comparison preserves explicit references cumulative cost corrections and unknown holdings', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await choose(page, data);
  await calculate(page);
  const result = panel(page).getByRole('region', { name: '대조 계산 결과', exact: true });
  await expect(result.getByRole('cell', { name: '-0.1 / 미확인', exact: true })).toBeVisible();
  await expect(
    result.getByRole('cell', { name: '9,007,199,254,740,993.001', exact: true }),
  ).toBeVisible();
  await expect(
    result.getByRole('cell', { name: '이후 보유 목록에서 미관측 · 연결 미확인', exact: true }),
  ).toBeVisible();
  await panel(page).getByRole('button', { name: '대조 결과 저장', exact: true }).click();
  await expect(
    panel(page).getByRole('region', { name: '저장 대조 상세', exact: true }),
  ).toBeVisible();
  expect(state.saves[0]).toEqual({
    before_snapshot_id: data.first.id,
    after_snapshot_id: afterSnapshot,
    before_scan_id: beforeId,
    after_scan_id: afterId,
    mode: 'synthetic',
    as_of: null,
  });
  await panel(page)
    .getByRole('region', { name: '저장 대조 상세', exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-23-broker.png') });
});
test('uncertain comparison save retries original sources after changing account and inputs', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.lostSave = true;
  await open(page, data);
  await choose(page, data);
  await calculate(page);
  await panel(page).getByRole('button', { name: '대조 결과 저장', exact: true }).click();
  await expect(panel(page).getByRole('button', { name: '같은 대조 저장 결과 확인' })).toBeEnabled();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page).getByRole('button', { name: '같은 대조 저장 결과 확인' }).click();
  await expect(panel(page).getByRole('status')).toContainText('계좌 101 대조 결과를 저장했습니다');
  expect(state.saves).toHaveLength(2);
  expect(state.saves[1]).toEqual(state.saves[0]);
  await expect(panel(page).getByRole('combobox', { name: '저장 대조 선택' })).toHaveValue('');
});
test('standard workspace queues a read scan and retries its original KST range and opaque IDs', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, { enabled: true, synthetic: false });
  state.lostJob = true;
  await open(page, data);
  await panel(page).getByText('브로커 읽기 스캔 접수', { exact: true }).first().click();
  await panel(page).getByLabel('주문 생성 시작일 (KST)').fill('2026-09-01');
  await panel(page).getByLabel('주문 생성 종료일 (KST)').fill('2026-09-10');
  await panel(page).getByText('페이지 한도·개별 주문 조회', { exact: true }).click();
  await panel(page)
    .getByRole('textbox', { name: '개별 조회 주문 ID (한 줄에 하나, 최대 20개)' })
    .fill('ORDER/OPAQUE,A\nORDER-B');
  await panel(page).getByRole('button', { name: '브로커 읽기 스캔 접수', exact: true }).click();
  await expect(panel(page).getByRole('button', { name: '같은 수집 요청 결과 확인' })).toBeEnabled();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page).getByLabel('주문 생성 종료일 (KST)').fill('2026-09-11');
  await panel(page).getByRole('button', { name: '같은 수집 요청 결과 확인' }).click();
  await expect(panel(page).getByRole('status')).toContainText('접수했습니다');
  expect(state.submissions[1]).toEqual(state.submissions[0]);
  expect(state.submissions[0]).toMatchObject({
    kind: 'broker-sync',
    parameters: {
      account_seq: '101',
      mode: 'prospective',
      from_date: '2026-09-01',
      to_date: '2026-09-10',
      detail_order_ids: ['ORDER/OPAQUE,A', 'ORDER-B'],
    },
  });
  expect(state.jobs[0].status).toBe('queued');
});
test('late calculations and failed refresh hide previous scan and comparison data', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await choose(page, data);
  let release!: () => void;
  state.holdPreview = new Promise<void>((resolve) => {
    release = resolve;
  });
  await panel(page).getByRole('button', { name: '관측 대조 계산', exact: true }).click();
  await panel(page)
    .getByRole('combobox', { name: '이전 스캔 (선택)', exact: true })
    .selectOption('');
  release();
  await expect(
    panel(page).getByRole('region', { name: '대조 계산 결과', exact: true }),
  ).toHaveCount(0);
  state.holdPreview = undefined;
  await calculate(page);
  state.previewError = true;
  await panel(page).getByRole('button', { name: '관측 대조 계산', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toBeVisible();
  await expect(
    panel(page).getByRole('region', { name: '대조 계산 결과', exact: true }),
  ).toHaveCount(0);
  let releaseScan!: () => void;
  state.heldScan = beforeId;
  state.holdScan = new Promise<void>((resolve) => {
    releaseScan = resolve;
  });
  await panel(page)
    .getByRole('combobox', { name: '확인할 저장 스캔', exact: true })
    .selectOption(beforeId);
  await panel(page)
    .getByRole('combobox', { name: '확인할 저장 스캔', exact: true })
    .selectOption(afterId);
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 스캔 상세', exact: true })
      .getByRole('cell', { name: '1 / 0.375', exact: true }),
  ).toBeVisible();
  releaseScan();
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 스캔 상세', exact: true })
      .getByRole('cell', { name: '1 / 0.125', exact: true }),
  ).toHaveCount(0);
  await expect(
    panel(page).getByRole('region', { name: '선택한 스캔 상세', exact: true }),
  ).toBeVisible();
  state.scanError = true;
  await panel(page).getByRole('button', { name: '브로커 자료 다시 읽기' }).click();
  await expect(
    panel(page).getByRole('region', { name: '선택한 스캔 상세', exact: true }),
  ).toHaveCount(0);
});
test('late saved comparison cannot replace the new selection and failed detail hides it', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  const body: ReconciliationRequest = {
    before_snapshot_id: data.first.id,
    after_snapshot_id: afterSnapshot,
    before_scan_id: beforeId,
    after_scan_id: afterId,
    mode: 'synthetic',
    as_of: null,
  };
  state.reports = [
    report(body, data),
    report({ ...body, before_scan_id: null }, data, otherReportId),
  ];
  let release!: () => void;
  state.heldReport = reportId;
  state.holdReport = new Promise<void>((resolve) => {
    release = resolve;
  });
  await open(page, data);
  await panel(page).getByRole('combobox', { name: '저장 대조 선택' }).selectOption(reportId);
  await panel(page).getByRole('combobox', { name: '저장 대조 선택' }).selectOption(otherReportId);
  await expect(
    panel(page).getByRole('region', { name: '저장 대조 상세', exact: true }),
  ).toContainText('이전 비교값 없음');
  release();
  await expect(
    panel(page).getByRole('region', { name: '저장 대조 상세', exact: true }),
  ).not.toContainText('누적 금액·비용 정정');
  state.reportError = true;
  await panel(page).getByRole('button', { name: '브로커 자료 다시 읽기' }).click();
  await expect(
    panel(page).getByRole('region', { name: '저장 대조 상세', exact: true }),
  ).toHaveCount(0);
});
test('mobile broker forms and decimal tables stay within viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  await mocks(page, data);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, data);
  await choose(page, data);
  await calculate(page);
  await panel(page)
    .getByRole('region', { name: '대조 계산 결과', exact: true })
    .scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('feature-23-broker-mobile.png') });
  await panel(page).getByRole('region', { name: '계좌와 주문 대조 입력' }).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});
