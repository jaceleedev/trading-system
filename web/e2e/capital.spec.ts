import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  CapitalPlanCreate,
  CapitalPlanRecord,
  CapitalPlanRequest,
  CapitalPlanReserve,
  CapitalPlanResponse,
  FundingRefresh,
  FundingState,
  InvestmentContext,
} from '../src/lib/api/types.gen';

const now = '2026-09-10T09:00:00Z';
const planId = 'a'.repeat(64);
const otherPlanId = 'b'.repeat(64);
const poolId = 'c'.repeat(64);
const reservationId = '21000000-0000-4000-8000-000000000001';
const panel = (page: Page) => page.getByRole('region', { name: '자금 계획', exact: true });

async function fixture(request: APIRequestContext) {
  const accounts = (await (
    await request.get('/api/v1/account-snapshots')
  ).json()) as AccountSnapshotsResponse;
  const first = accounts.items.find((item) => item.account_seq === '101')!;
  const second = accounts.items.find((item) => item.account_seq === '202')!;
  const context = (await (
    await request.get(`/api/v1/context?snapshot_id=${first.id}`)
  ).json()) as InvestmentContext;
  const decision = context.records.find(
    (item) =>
      item.record.kind === 'decision' && item.record.payload.account_snapshot_id === first.id,
  )!;
  return { first, second, context, decision };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

/** Deliberately synthetic HTTP calculations; Python tests verify the actual arithmetic. */
function record(body: CapitalPlanRequest, data: Fixture): CapitalPlanRecord {
  return {
    kind: 'capital_plan',
    schema_version: 1,
    recorded_at: now,
    request: body,
    snapshot: data.context.account!.snapshot,
    source_context: {
      ...body.source,
      mode: body.mode,
      recorded_at: now,
      account_snapshot_id: data.first.id,
      account_seq: '101',
    },
    reservations: { known: true, cash: [], holdings: [] },
    calculation: {
      arithmetic_precision: 256,
      arithmetic_rounding: 'ROUND_HALF_EVEN',
      local_reservations_known: true,
      cash_capacity: [
        {
          currency: 'USD',
          observed_buying_power: '3500.5',
          operator_limit: '1000',
          operator_reserve: '100',
          capacity_before_reservations: '900',
          existing_reserved_amount: '0',
          available_amount: '900',
        },
      ],
      alternatives: body.alternatives.map((alternative) => ({
        key: alternative.key,
        label: alternative.label,
        rationale: alternative.rationale,
        eligibility: alternative.legs.some((leg) => leg.price === null) ? 'unknown' : 'eligible',
        blockers: alternative.legs.some((leg) => leg.price === null)
          ? ['assumed_price_unknown']
          : [],
        legs: alternative.legs.map((leg, index) => ({
          index,
          action: leg.action,
          symbol: leg.symbol,
          market: leg.market,
          currency: leg.currency,
          quantity: leg.quantity,
          price: leg.price,
          notional: leg.price === null ? null : '12.5',
          estimated_fee: leg.price === null ? null : '0',
          estimated_tax: leg.price === null ? null : '0',
          required_cash: leg.price === null ? null : '12.5',
          estimated_sale_proceeds: '0',
          blockers: [],
        })),
        holdings: [
          {
            market: 'US',
            symbol: 'BETA',
            currency: 'USD',
            observed_quantity: '0.125',
            existing_reserved_quantity: '0',
            available_quantity: '0.125',
            buy_quantity: '0.125',
            sell_quantity: '0',
            projected_quantity: '0.250',
            average_purchase_price_before: '80',
            average_purchase_price_after: '90.000000000000001',
            average_price_rounded: false,
            average_price_reason: null,
          },
        ],
        cash_requirements: alternative.legs.some((leg) => leg.price === null)
          ? []
          : [{ currency: 'USD', amount: '12.5' }],
        holding_requirements: [],
        estimated_sale_proceeds: [],
        unknown_cash_currencies: alternative.legs.some((leg) => leg.price === null) ? ['USD'] : [],
        unknown_sale_proceeds_currencies: [],
      })),
      assumptions: [],
      warnings: [],
      execution_ready: false,
      orders_enabled: false,
    },
  };
}

async function mocks(page: Page, data: Fixture) {
  const state = {
    previews: [] as CapitalPlanRequest[],
    creates: [] as CapitalPlanCreate[],
    plans: [] as CapitalPlanResponse[],
    budgets: [] as FundingRefresh[],
    allocations: [] as { id: string; body: CapitalPlanReserve }[],
    releases: [] as string[],
    loseSave: false,
    loseAllocation: false,
    loseRelease: false,
    previewError: false,
    fundingError: false,
    detailError: false,
    holdPreview: undefined as Promise<void> | undefined,
    holdPlan: '',
    holdDetail: undefined as Promise<void> | undefined,
    funding: {
      account_seq: '101',
      provider: 'toss',
      pools: [],
      reservations: [],
      omitted_reservation_count: 0,
      expected_pool_revisions: { [poolId]: 0 },
      execution_ready: false,
    } as FundingState,
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
  await page.route('**/api/v1/capital-plans/preview', async (route) => {
    const body = route.request().postDataJSON() as CapitalPlanRequest;
    state.previews.push(body);
    if (state.holdPreview) await state.holdPreview;
    await route.fulfill(
      state.previewError
        ? {
            status: 503,
            json: { error: { code: 'capital_unavailable', message: 'Synthetic failure' } },
          }
        : { json: record(body, data) },
    );
  });
  await page.route(/\/api\/v1\/capital-plans(?:\?.*)?$/, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        json: {
          items: state.plans.map((item) => ({
            id: item.id,
            recorded_at: item.record.recorded_at,
            mode: item.record.request.mode,
            snapshot_id: item.record.request.snapshot_id,
            source: item.record.request.source,
            alternative_count: item.record.request.alternatives.length,
            eligible_count: 1,
          })),
          total_count: state.plans.length,
          omitted_count: 0,
        },
      });
      return;
    }
    const body = route.request().postDataJSON() as CapitalPlanCreate;
    state.creates.push(body);
    if (!state.plans.length) state.plans.push({ id: planId, record: record(body, data) });
    if (state.loseSave) {
      state.loseSave = false;
      await route.abort('failed');
    } else await route.fulfill({ json: state.plans[0] });
  });
  await page.route(/\/api\/v1\/capital-plans\/[a-f0-9]{64}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    if (state.holdPlan === id && state.holdDetail) await state.holdDetail;
    await route
      .fulfill(
        state.detailError
          ? {
              status: 503,
              json: { error: { code: 'capital_unavailable', message: 'Synthetic failure' } },
            }
          : { json: state.plans.find((item) => item.id === id) },
      )
      .catch(() => {});
  });
  await page.route(/\/api\/v1\/funding\?account_seq=.*/, (route) => {
    if (state.fundingError)
      return route.fulfill({
        status: 503,
        json: { error: { code: 'capital_unavailable', message: 'Synthetic failure' } },
      });
    const accountSeq = new URL(route.request().url()).searchParams.get('account_seq')!;
    return route.fulfill({
      json:
        accountSeq === '101'
          ? state.funding
          : { ...state.funding, account_seq: accountSeq, pools: [], reservations: [] },
    });
  });
  await page.route('**/api/v1/funding/refresh', (route) => {
    const body = route.request().postDataJSON() as FundingRefresh;
    state.budgets.push(body);
    state.funding.expected_pool_revisions = { [poolId]: 1 };
    state.funding.pools = [
      {
        id: poolId,
        kind: 'cash',
        mode: 'synthetic',
        currency: 'USD',
        market: null,
        symbol: null,
        snapshot_id: body.snapshot_id,
        observed_at: now,
        capacity: '900',
        reserved: '0',
        available: '900',
        overallocated: false,
        revision: 1,
        basis: {},
      },
    ];
    return route.fulfill({ json: state.funding });
  });
  await page.route(/\/api\/v1\/capital-plans\/[a-f0-9]{64}\/reserve$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const body = route.request().postDataJSON() as CapitalPlanReserve;
    state.allocations.push({ id, body });
    if (!state.funding.reservations.length) {
      state.funding.reservations.push({
        id: reservationId,
        account_seq: '101',
        plan_id: id,
        alternative_id: body.alternative_id,
        request_key: body.request_key,
        mode: 'synthetic',
        status: 'active',
        requirements: { cash: [{ currency: 'USD', amount: '12.5' }], holdings: [] },
        pool_revisions: { [poolId]: 2 },
        created_at: now,
        released_at: null,
        replaced_by: null,
      });
      state.funding.pools[0] = {
        ...state.funding.pools[0],
        reserved: '12.5',
        available: '887.5',
        revision: 2,
      };
      state.funding.expected_pool_revisions = { [poolId]: 2 };
    }
    if (state.loseAllocation) {
      state.loseAllocation = false;
      await route.abort('failed');
    } else
      await route.fulfill({
        json: { reservation: state.funding.reservations[0], funding: state.funding },
      });
  });
  await page.route(/\/api\/v1\/funding\/reservations\/[a-f0-9-]{36}\/release$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    state.releases.push(id);
    state.funding.expected_pool_revisions = { [poolId]: 3 };
    state.funding.reservations[0] = {
      ...state.funding.reservations[0],
      status: 'released',
      released_at: now,
    };
    state.funding.pools[0] = {
      ...state.funding.pools[0],
      reserved: '0',
      available: '900',
      revision: 3,
    };
    if (state.loseRelease) {
      state.loseRelease = false;
      await route.abort('failed');
    } else
      await route.fulfill({
        json: { reservation: state.funding.reservations[0], funding: state.funding },
      });
  });
  return state;
}

async function fillPlan(page: Page, data: Fixture) {
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByText('자금 계획', { exact: true }).click();
  await panel(page)
    .getByRole('combobox', { name: '계획 근거' })
    .selectOption(`decision:${data.decision.id}`);
  await panel(page).getByRole('checkbox', { name: 'USD 계획 예산 사용' }).check();
  await panel(page).getByRole('textbox', { name: 'USD 계획 한도', exact: true }).fill('1000');
  await panel(page).getByRole('textbox', { name: 'USD 남겨둘 금액', exact: true }).fill('100');
  await panel(page).getByRole('textbox', { name: '대안 1 이유' }).fill('합성 추가 매수 대안');
  const leg = panel(page).getByRole('group', { name: '대안 1 항목 1' });
  await leg.getByRole('combobox', { name: '행동', exact: true }).selectOption('add');
  await leg.getByRole('textbox', { name: '종목', exact: true }).fill('BETA');
  await leg.getByRole('combobox', { name: '시장', exact: true }).selectOption('US');
  await leg.getByRole('combobox', { name: '통화', exact: true }).selectOption('USD');
  await leg.getByRole('textbox', { name: '수량', exact: true }).fill('0.125');
  await leg.getByRole('textbox', { name: '가정 가격', exact: true }).fill('100');
  for (const name of ['수수료율', '고정 수수료', '세율'])
    await leg.getByRole('textbox', { name, exact: true }).fill('0');
  await leg.getByRole('textbox', { name: '항목의 이유' }).fill('합성 원가 비교');
}
async function calculateSave(page: Page) {
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toBeVisible();
  await panel(page).getByRole('button', { name: '계획 저장', exact: true }).click();
  await expect(panel(page).getByRole('region', { name: '저장 계획 상세' })).toBeVisible();
}

test('default disabled planning keeps account viewing usable without mutation requests', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const mutations: string[] = [];
  page.on('request', (item) => {
    if (item.method() === 'POST') mutations.push(item.url());
  });
  await page.goto('/');
  await panel(page).getByText('자금 계획', { exact: true }).click();
  await expect(
    panel(page).getByText('계획 저장소가 꺼져 있습니다. 대안 계산은 계속 사용할 수 있습니다.'),
  ).toBeVisible();
  await expect(panel(page).getByRole('button', { name: '계획 저장', exact: true })).toBeDisabled();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await expect(page.getByTestId('buying-power-USD')).toHaveText('3,500.5');
  expect(mutations).toEqual([]);
});

test('explicit assumptions calculate and save exact decimal projections', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await fillPlan(page, data);
  await calculateSave(page);
  expect(state.previews[0]).toMatchObject({
    snapshot_id: data.first.id,
    source: { kind: 'decision', id: data.decision.id },
    mode: 'synthetic',
    funding: [{ currency: 'USD', limit_amount: '1000', reserve_amount: '100' }],
  });
  expect(state.previews[0].alternatives[0].legs[0]).toMatchObject({
    quantity: '0.125',
    price: '100',
    fee_bps: '0',
    fixed_fee: '0',
    tax_bps: '0',
  });
  const saved = panel(page).getByRole('region', { name: '저장 계획 상세' });
  await expect(saved.getByRole('cell', { name: '90.000000000000001', exact: true })).toBeVisible();
  await expect(saved.getByRole('cell', { name: '0.250', exact: true })).toBeVisible();
  await saved.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-21-capital.png') });
  expect(state.budgets).toEqual([]);
  expect(state.allocations).toEqual([]);
});

test('replacement legs stay together while a separate alternative stays independent', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await fillPlan(page, data);
  await panel(page).getByRole('button', { name: '같은 대안에 항목 추가' }).click();
  const sell = panel(page).getByRole('group', { name: '대안 1 항목 2' });
  await sell.getByRole('combobox', { name: '행동', exact: true }).selectOption('sell');
  await sell.getByRole('textbox', { name: '종목', exact: true }).fill('ALPHA');
  await sell.getByRole('textbox', { name: '수량', exact: true }).fill('1');
  await sell.getByRole('textbox', { name: '가정 가격', exact: true }).fill('100');
  for (const name of ['수수료율', '고정 수수료', '세율'])
    await sell.getByRole('textbox', { name, exact: true }).fill('0');
  await sell.getByRole('textbox', { name: '항목의 이유' }).fill('합성 교체 검토');
  await panel(page).getByRole('button', { name: '다른 대안 추가' }).click();
  await panel(page).getByRole('textbox', { name: '대안 2 이유' }).fill('독립 비교안');
  const other = panel(page).getByRole('group', { name: '대안 2 항목 1' });
  await other.getByRole('textbox', { name: '종목', exact: true }).fill('GAMMA');
  await other.getByRole('textbox', { name: '수량', exact: true }).fill('2');
  await other.getByRole('textbox', { name: '가정 가격', exact: true }).fill('50');
  for (const name of ['수수료율', '고정 수수료', '세율'])
    await other.getByRole('textbox', { name, exact: true }).fill('0');
  await other.getByRole('textbox', { name: '항목의 이유' }).fill('다른 합성 기회');
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toBeVisible();
  expect(state.previews[0].alternatives.map((item) => item.legs.map((leg) => leg.action))).toEqual([
    ['add', 'sell'],
    ['buy'],
  ]);
  expect(new Set(state.previews[0].alternatives.map((item) => item.key)).size).toBe(2);
  await expect(panel(page).getByRole('region', { name: '계산 대안 대안 2' })).toBeVisible();
});

test('save response loss retries the original request after account switching', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.loseSave = true;
  await fillPlan(page, data);
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toBeVisible();
  await panel(page).getByRole('button', { name: '계획 저장', exact: true }).click();
  await expect(panel(page).getByText(/계획 저장 여부 확인 대기/)).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await panel(page).getByRole('button', { name: '같은 계획 저장 다시 확인' }).click();
  await expect(panel(page).getByRole('region', { name: '저장 계획 상세' })).toBeVisible();
  expect(state.creates).toHaveLength(2);
  expect(state.creates[1]).toEqual(state.creates[0]);
  await expect(panel(page).getByRole('button', { name: '대안 1에 자금 배정' })).toBeDisabled();
});

test('budget applies explicitly and allocation and release retry the same identities', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await fillPlan(page, data);
  await calculateSave(page);
  expect(state.budgets).toHaveLength(0);
  await panel(page).getByRole('button', { name: '선택 관측으로 계획 예산 적용' }).click();
  await expect(panel(page).getByText(/선택한 관측과 계획 예산을 적용했습니다/)).toBeVisible();
  expect(state.budgets[0]).toMatchObject({
    snapshot_id: data.first.id,
    funding: [{ currency: 'USD', limit_amount: '1000', reserve_amount: '100' }],
    expected_pool_revisions: { [poolId]: 0 },
  });
  state.loseAllocation = true;
  await panel(page).getByRole('button', { name: '대안 1에 자금 배정' }).click();
  await expect(panel(page).getByText(/대안 배정 여부 확인 대기/)).toBeVisible();
  await panel(page).getByRole('button', { name: '같은 대안 배정 다시 확인' }).click();
  await expect(panel(page).getByText('배정 중 · 합성', { exact: true })).toBeVisible();
  await panel(page).getByRole('region', { name: '계획 예산과 배정' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('capital-allocation.png') });
  expect(state.allocations).toHaveLength(2);
  expect(state.allocations[1]).toEqual(state.allocations[0]);
  expect(state.allocations[0].body.expected_pool_revisions).toEqual({ [poolId]: 1 });
  state.loseRelease = true;
  await panel(page).getByRole('button', { name: '배정 해제', exact: true }).click();
  await expect(panel(page).getByText(/배정 해제 여부 확인 대기/)).toBeVisible();
  await panel(page).getByRole('button', { name: '같은 배정 해제 다시 확인' }).click();
  await expect(panel(page).getByText('해제됨 · 합성', { exact: true })).toBeVisible();
  expect(state.releases).toEqual([reservationId, reservationId]);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await expect(panel(page).getByText('이 계좌에 적용한 계획 예산이 없습니다.')).toBeVisible();
  expect(state.budgets).toHaveLength(1);
});

test('late calculation and failed recalculation never show stale projections', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  let release!: () => void;
  state.holdPreview = new Promise<void>((resolve) => {
    release = resolve;
  });
  await fillPlan(page, data);
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByText('선택한 계좌와 가정으로 계산하고 있습니다.')).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  release();
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toHaveCount(0);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toBeVisible();
  state.previewError = true;
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  await expect(panel(page).getByRole('alert')).toContainText('계획 저장소에 연결하지 못했습니다.');
  await expect(panel(page).getByRole('region', { name: '대안 계산 결과' })).toHaveCount(0);
});

test('unknown prices remain unknown and unavailable funding hides old capacity', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await fillPlan(page, data);
  await panel(page).getByRole('textbox', { name: '가정 가격', exact: true }).fill('');
  await panel(page).getByRole('button', { name: '대안 계산', exact: true }).click();
  const preview = panel(page).getByRole('region', { name: '대안 계산 결과' });
  await expect(preview.getByText('필요 자금 미확인: USD')).toBeVisible();
  await expect(preview.getByText('가정 가격이 없어 금액을 확인할 수 없습니다.')).toBeVisible();
  expect(state.previews[0].alternatives[0].legs[0].price).toBeNull();
  await panel(page).getByRole('button', { name: '선택 관측으로 계획 예산 적용' }).click();
  const funding = panel(page).getByRole('region', { name: '계획 예산과 배정' });
  await expect(funding.getByRole('table')).toBeVisible();
  state.fundingError = true;
  await panel(page).getByRole('button', { name: '자금 계획 다시 읽기' }).click();
  await expect(funding.getByRole('alert')).toBeVisible();
  await expect(funding.getByRole('table')).toHaveCount(0);
  await expect(
    funding.getByRole('button', { name: '선택 관측으로 계획 예산 적용' }),
  ).toBeDisabled();
});

test('mobile form and exact financial tables stay within the viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  await mocks(page, data);
  await page.setViewportSize({ width: 390, height: 844 });
  await fillPlan(page, data);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).getByRole('group', { name: '대안 1 항목 1' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('capital-mobile-form.png') });
  await calculateSave(page);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel(page).getByRole('region', { name: '저장 계획 상세' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('capital-mobile-result.png') });
});

test('late saved-plan details and failed refreshes cannot replace the current selection', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await fillPlan(page, data);
  await calculateSave(page);
  const other = structuredClone(state.plans[0]);
  other.id = otherPlanId;
  other.record.calculation.alternatives[0].label = '두 번째 저장 대안';
  state.plans.push(other);
  await panel(page).getByRole('button', { name: '자금 계획 다시 읽기' }).click();
  const list = panel(page).getByRole('region', { name: '저장한 자금 계획' });
  await list.getByRole('button', { name: /bbbbbbbbbb/ }).click();
  const detail = panel(page).getByRole('region', { name: '저장 계획 상세' });
  await expect(detail.getByRole('heading', { name: '두 번째 저장 대안' })).toBeVisible();
  let release!: () => void;
  state.holdPlan = planId;
  state.holdDetail = new Promise<void>((resolve) => {
    release = resolve;
  });
  await list.getByRole('button', { name: /aaaaaaaaaa/ }).click();
  await expect(panel(page).getByText('계획 상세를 읽고 있습니다.')).toBeVisible();
  await list.getByRole('button', { name: /bbbbbbbbbb/ }).click();
  await expect(detail.getByRole('heading', { name: '두 번째 저장 대안' })).toBeVisible();
  release();
  await expect(detail.getByRole('heading', { name: '대안 1', exact: true })).toHaveCount(0);
  state.detailError = true;
  await panel(page).getByRole('button', { name: '자금 계획 다시 읽기' }).click();
  await expect(panel(page).getByRole('alert')).toBeVisible();
  await expect(detail).toHaveCount(0);
});
