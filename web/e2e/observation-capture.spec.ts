import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  JobServiceStatus,
  JobView,
  PaperBookDetail,
} from '../src/lib/api/types.gen';

const started = '2026-09-14T07:00:00Z';
const completed = '2026-09-14T07:00:08Z';
const jobId = '28000000-0000-4000-8000-000000000001';
const newSnapshotId = '2'.repeat(64);
const captureId = '3'.repeat(64);
const region = (page: Page) => page.getByRole('region', { name: '새 관측 수집', exact: true });
const receipt = (page: Page) =>
  region(page).getByRole('region', { name: '수집 요청과 결과', exact: true });
const submit = (page: Page) => region(page).getByRole('button', { name: '새 관측 수집 접수' });
const investigate = (page: Page) =>
  receipt(page).getByRole('button', { name: '완료 관측을 다음 조사에 사용' });
const paper = (page: Page) =>
  receipt(page).getByRole('button', { name: '완료 분봉을 모의 진행 입력에 추가' });

type CaptureBody = {
  kind: 'account-sync' | 'market-capture';
  parameters: Record<string, unknown>;
  request_key: string;
};

async function fixture(request: APIRequestContext) {
  const accounts = (await (
    await request.get('/api/v1/account-snapshots')
  ).json()) as AccountSnapshotsResponse;
  const first = accounts.items.find((item) => item.account_seq === '101')!;
  const second = accounts.items.find((item) => item.account_seq === '202')!;
  const context = (await (
    await request.get(`/api/v1/context?snapshot_id=${first.id}`)
  ).json()) as InvestmentContext;
  const options = await (await request.get('/api/v1/observation-captures/options')).json();
  return { accounts, first, second, context, options };
}
type Fixture = Awaited<ReturnType<typeof fixture>>;

/** HTTP fixtures use synthetic account files. No credential resolver or external provider is called. */
async function mocks(page: Page, data: Fixture, enabled = true) {
  const state = {
    submissions: [] as CaptureBody[],
    recoveries: [] as CaptureBody[],
    job: null as JobView | null,
    loseSubmit: false,
    absentSubmit: false,
    rejectSubmit: false,
    failResult: false,
    resultCalls: 0,
    jobCalls: 0,
    resultHeld: undefined as Promise<void> | undefined,
    result: {
      job_id: jobId,
      kind: 'account-sync',
      status: 'succeeded',
      account_seq: '101' as string | null,
      snapshot_id: newSnapshotId as string | null,
      capture_ids: [] as string[],
      collection_started_at: started,
      collection_completed_at: completed,
      observations: [
        {
          id: '4'.repeat(64),
          endpoint: '/api/v1/holdings',
          observed_at: '2026-09-14T07:00:02Z',
          symbol: null as string | null,
          interval: null as string | null,
          adjusted: null as boolean | null,
          candle_count: null as number | null,
          normalization: 'not_applicable',
          paper_candidate: false,
        },
      ],
      coverage: {
        requested_pages: null as number | null,
        received_pages: null as number | null,
        truncated: null as boolean | null,
        has_more: null as boolean | null,
        unknowns: ['account_observations_are_non_atomic'],
      },
      warnings: ['Synthetic selected account observations; unknown cash remains unknown.'],
      orders_enabled: false,
    },
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        jobs_enabled: enabled,
        read_only: !enabled,
        synthetic: false,
      },
    });
  });
  await page.route('**/api/v1/observation-captures/options', (route) =>
    route.fulfill({ json: { ...data.options, workspace_key: 'b'.repeat(64) } }),
  );
  await page.route('**/api/v1/jobs/status', (route) => {
    const status: JobServiceStatus = {
      enabled,
      database: enabled ? 'reachable' : 'not_checked',
      checked_at: started,
      workspace_key: 'b'.repeat(64),
      workers: [
        {
          id: '28000000-0000-4000-8000-000000000002',
          owner: 'synthetic-no-network-worker',
          started_at: started,
          heartbeat_at: started,
          expires_at: '2026-09-14T07:01:00Z',
          stopped_at: null,
          state: 'idle',
          liveness: 'live',
          allow_network: false,
          allow_codex: false,
          codex_web_search_allowed: null,
          current_job_id: null,
        },
      ],
      workers_truncated: false,
      queued_count: state.job?.status === 'queued' ? 1 : 0,
      running_count: state.job?.status === 'running' ? 1 : 0,
      waiting_jobs:
        state.job?.status === 'queued'
          ? [
              {
                job_id: state.job.id,
                kind: state.job.kind,
                available_at: started,
                required_capabilities: ['network'],
                eligible_worker_ids: [],
                reasons: ['capability_not_allowed'],
              },
            ]
          : [],
      waiting_jobs_truncated: false,
    };
    return route.fulfill({ json: status });
  });
  await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: state.job ? [state.job] : [] } }),
  );
  await page.route(/\/api\/v1\/jobs\/28000000-0000-4000-8000-\d{12}$/, (route) => {
    state.jobCalls++;
    return route.fulfill({ json: { job: state.job } });
  });
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [] } }),
  );
  await page.route('**/api/v1/observation-captures', async (route) => {
    const body = route.request().postDataJSON() as CaptureBody;
    state.submissions.push(body);
    if (state.rejectSubmit) {
      await route.fulfill({
        status: 422,
        json: {
          error: { code: 'invalid_request', message: 'Synthetic provider contract rejection.' },
        },
      });
      return;
    }
    if (!state.job && !state.absentSubmit) {
      state.job = {
        id: `28000000-0000-4000-8000-${String(state.submissions.length).padStart(12, '0')}`,
        workspace_key: 'b'.repeat(64),
        kind: body.kind,
        parameters: body.parameters,
        request_key: body.request_key,
        status: 'queued',
        available_at: started,
        created_at: started,
        updated_at: started,
        finished_at: null,
        max_attempts: 1,
        attempt_count: 0,
        cancel_requested: false,
        lease_expires_at: null,
        result: null,
        error_code: null,
        attempts: [],
      };
    }
    if (state.loseSubmit) {
      state.loseSubmit = false;
      await route.abort('failed');
    } else await route.fulfill({ json: { job: state.job } });
  });
  await page.route('**/api/v1/observation-captures/recover', (route) => {
    const body = route.request().postDataJSON() as CaptureBody;
    state.recoveries.push(body);
    return route.fulfill(
      state.job
        ? { json: { job: state.job } }
        : { status: 404, json: { error: { code: 'not_found', message: 'No request found.' } } },
    );
  });
  await page.route(
    /\/api\/v1\/observation-captures\/28000000-0000-4000-8000-\d{12}\/result$/,
    async (route) => {
      state.resultCalls++;
      if (state.resultHeld) await state.resultHeld;
      await route
        .fulfill(
          state.failResult
            ? {
                status: 500,
                json: { error: { code: 'internal_error', message: 'Synthetic read failure.' } },
              }
            : { json: state.result },
        )
        .catch(() => {});
    },
  );
  await page.route('**/api/v1/account-snapshots', (route) =>
    route.fulfill({
      json: {
        items: [
          ...data.accounts.items,
          {
            ...data.first,
            id: newSnapshotId,
            collection_started_at: started,
            collection_completed_at: completed,
          },
        ],
      },
    }),
  );
  await page.route(`**/api/v1/context?snapshot_id=${newSnapshotId}*`, (route) =>
    route.fulfill({
      json: { ...data.context, account: { ...data.context.account, id: newSnapshotId } },
    }),
  );
  return state;
}
type State = Awaited<ReturnType<typeof mocks>>;

async function open(page: Page, data: Fixture) {
  await page.goto('/');
  await expect(region(page)).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.first.id);
}
async function account(page: Page, data: Fixture) {
  await region(page).getByRole('combobox', { name: '수집 종류' }).selectOption('account-sync');
  await region(page)
    .getByRole('combobox', { name: '수집 대상 계좌 관측' })
    .selectOption(data.first.id);
  await region(page)
    .getByRole('checkbox', { name: '표시된 계좌 조회 범위를 확인했습니다' })
    .check();
}
async function market(page: Page) {
  await region(page).getByRole('combobox', { name: '수집 종류' }).selectOption('market-capture');
  await region(page).getByRole('combobox', { name: '허용 시장 endpoint' }).selectOption('candles');
  await region(page).locator('[name="symbol"]').fill('ALPHA');
  await region(page).locator('[name="interval"]').selectOption('1m');
  await region(page).locator('[name="count"]').fill('200');
  await region(page)
    .getByLabel(/^최대 수집 페이지/)
    .fill('2');
}
function finish(state: State, status: 'succeeded' | 'failed' = 'succeeded') {
  state.job!.status = status;
  state.job!.finished_at = completed;
  state.job!.updated_at = completed;
  state.job!.attempt_count = 1;
  state.job!.error_code = status === 'failed' ? 'provider_error' : null;
}
async function refresh(page: Page) {
  await region(page).getByRole('button', { name: '같은 요청 상태 확인' }).click();
}

test('saved observations remain readable without DB collection and account choice stays explicit', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, false);
  await open(page, data);
  await expect(page.getByTestId('buying-power-USD')).toHaveText('3,500.5');
  await expect(submit(page)).toBeDisabled();
  expect(state.submissions).toEqual([]);
});

test('explicit account scope queues without enabling network and saved refresh creates no capture', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await expect(region(page).getByRole('combobox', { name: '수집 대상 계좌 관측' })).toHaveValue('');
  await submit(page).click();
  await expect(region(page).getByRole('alert')).toBeVisible();
  expect(state.submissions).toHaveLength(0);
  await account(page, data);
  await submit(page).click();
  await expect(receipt(page)).toContainText('대기');
  await expect(region(page)).toContainText(/필요 기능을 허용한 worker 없음|네트워크.*허용/);
  expect(state.submissions).toHaveLength(1);
  expect(state.submissions[0]).toMatchObject({
    kind: 'account-sync',
    parameters: { account_seq: '101', source_snapshot_id: data.first.id },
  });
  expect(state.submissions[0]).not.toHaveProperty('allow_network');
  await refresh(page);
  await page.getByRole('button', { name: '저장 자료 다시 읽기', exact: true }).click();
  expect(state.submissions).toHaveLength(1);
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue(
    data.first.id,
  );
});

test('market collection validates documented count and page bounds before submitting exact range', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await market(page);
  await region(page).locator('[name="count"]').fill('201');
  await submit(page).click();
  expect(state.submissions).toHaveLength(0);
  await region(page).locator('[name="count"]').fill('200');
  await region(page)
    .getByLabel(/^최대 수집 페이지/)
    .fill('11');
  await submit(page).click();
  expect(state.submissions).toHaveLength(0);
  await region(page)
    .getByLabel(/^최대 수집 페이지/)
    .fill('2');
  await region(page).locator('[name="before"]').fill('2026-09-14T07:00:00Z');
  await submit(page).click();
  await expect(receipt(page)).toContainText('대기');
  expect(state.submissions[0]).toMatchObject({
    kind: 'market-capture',
    parameters: {
      endpoint: 'candles',
      pages: 2,
      query: { symbol: 'ALPHA', interval: '1m', count: 200, before: '2026-09-14T07:00:00Z' },
    },
  });
});

test('lost submit response survives reload and recovers original key and inputs without a duplicate POST', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.loseSubmit = true;
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  await expect(region(page).getByRole('button', { name: '같은 요청 상태 확인' })).toBeEnabled();
  await refresh(page);
  await expect(receipt(page)).toContainText('대기');
  await expect(region(page).getByRole('alert')).toHaveCount(0);
  await page.reload();
  await expect(receipt(page)).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  await refresh(page);
  await expect(receipt(page)).toContainText('대기');
  expect(state.submissions).toHaveLength(1);
  expect(state.recoveries.length).toBeGreaterThan(0);
  expect(
    state.recoveries.every(
      (value) => JSON.stringify(value) === JSON.stringify(state.submissions[0]),
    ),
  ).toBe(true);
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue(
    data.second.id,
  );
});

test('absent recovery requires explicit resubmission of the original key and input', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.loseSubmit = true;
  state.absentSubmit = true;
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  await refresh(page);
  const retry = receipt(page).getByRole('button', { name: '원래 입력으로 같은 요청 접수' });
  await expect(retry).toBeEnabled();
  await expect(receipt(page).getByRole('button', { name: '다른 수집 준비' })).toHaveCount(0);
  await expect(region(page).getByRole('combobox', { name: '수집 종류' })).toBeDisabled();
  expect(state.submissions).toHaveLength(1);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  state.absentSubmit = false;
  await retry.click();
  await expect(receipt(page)).toContainText('대기');
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[1]).toEqual(state.submissions[0]);
});

test('capture failure exposes failed state and never offers a completed observation', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  finish(state, 'failed');
  await refresh(page);
  await expect(receipt(page)).toContainText('실패');
  await expect(receipt(page)).toContainText('provider_error');
  await expect(investigate(page)).toHaveCount(0);
  await expect(paper(page)).toHaveCount(0);
  expect(state.submissions).toHaveLength(1);
});

test('completed account observation changes the next investigation input only after explicit handoff', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  finish(state);
  await refresh(page);
  await expect(receipt(page)).toContainText('완료');
  await expect(receipt(page).getByText(newSnapshotId, { exact: false })).toBeVisible();
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue(
    data.first.id,
  );
  await expect(investigate(page)).toBeEnabled();
  await investigate(page).click();
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue(
    newSnapshotId,
  );
  expect(state.submissions).toHaveLength(1);
});

test('late collection result for another selected account cannot attach to the current selection', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  await expect.poll(() => state.recoveries.length).toBeGreaterThan(0);
  await expect(region(page).getByRole('button', { name: '같은 요청 상태 확인' })).toBeEnabled();
  finish(state);
  let release!: () => void;
  state.resultHeld = new Promise<void>((resolve) => {
    release = resolve;
  });
  await refresh(page);
  await expect.poll(() => state.resultCalls).toBeGreaterThan(0);
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(data.second.id);
  release();
  await expect(investigate(page)).toBeDisabled();
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue(
    data.second.id,
  );
});

test('mobile capture inputs and receipt stay within the viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  await mocks(page, data);
  await page.setViewportSize({ width: 390, height: 844 });
  await open(page, data);
  await market(page);
  await submit(page).click();
  await receipt(page).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('feature-28-capture-mobile.png') });
});

test('storage failure blocks enqueue before any uncertain request can be lost', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith('trading-observation-capture-v1:'))
        throw new DOMException('Synthetic storage denial', 'QuotaExceededError');
      original.call(this, key, value);
    };
  });
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  await expect(region(page).getByRole('alert')).toBeVisible();
  expect(state.submissions).toHaveLength(0);
});

test('persisted job ID is only a hint until original request recovery succeeds', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.addInitScript(
    ({ snapshot, job }) => {
      const workspaceKey = 'b'.repeat(64);
      localStorage.setItem(
        `trading-observation-capture-v1:${workspaceKey}`,
        JSON.stringify({
          version: 1,
          workspaceKey,
          body: {
            kind: 'account-sync',
            parameters: { account_seq: '101', source_snapshot_id: snapshot },
            request_key: 'synthetic-not-enqueued-request',
          },
          contextSnapshotId: snapshot,
          contextAccountSeq: '101',
          jobId: job,
        }),
      );
    },
    { snapshot: data.first.id, job: jobId },
  );
  await open(page, data);
  await refresh(page);
  await expect(
    receipt(page).getByRole('button', { name: '원래 입력으로 같은 요청 접수' }),
  ).toBeEnabled();
  expect(state.jobCalls).toBe(0);
  expect(state.resultCalls).toBe(0);
  expect(state.submissions).toHaveLength(0);
});

test('receipt polling stops when hidden and after the capture finishes', async ({
  page,
  request,
}) => {
  await page.clock.install();
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  await expect(receipt(page)).toContainText('대기');
  const calls = state.jobCalls + state.recoveries.length;
  await page.clock.runFor(5100);
  await expect.poll(() => state.jobCalls + state.recoveries.length).toBeGreaterThan(calls);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  const hiddenCalls = state.jobCalls + state.recoveries.length;
  await page.clock.runFor(20000);
  expect(state.jobCalls + state.recoveries.length).toBe(hiddenCalls);
  finish(state);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.clock.runFor(5100);
  await expect(investigate(page)).toBeEnabled();
  const finishedCalls = state.jobCalls + state.recoveries.length;
  await page.clock.runFor(20000);
  expect(state.jobCalls + state.recoveries.length).toBe(finishedCalls);
});

test('failed result refresh hides prior completion links and pauses automatic reads', async ({
  page,
  request,
}) => {
  await page.clock.install();
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await account(page, data);
  await submit(page).click();
  finish(state);
  await refresh(page);
  await expect(investigate(page)).toBeEnabled();
  state.failResult = true;
  await refresh(page);
  await expect(receipt(page).getByRole('alert')).toBeVisible();
  await expect(investigate(page)).toHaveCount(0);
  const failedCalls = state.resultCalls;
  await page.clock.runFor(30000);
  expect(state.resultCalls).toBe(failedCalls);
  expect(state.submissions).toHaveLength(1);
});

function marketResult(state: State, { eligible = true, interval = '1m', adjusted = false } = {}) {
  state.result.kind = 'market-capture';
  state.result.account_seq = null;
  state.result.snapshot_id = null;
  state.result.capture_ids = [captureId];
  state.result.observations = [
    {
      id: captureId,
      endpoint: '/api/v1/candles',
      observed_at: completed,
      symbol: 'ALPHA',
      interval,
      adjusted,
      candle_count: 200,
      normalization: 'supported',
      paper_candidate: eligible,
    },
  ];
  state.result.coverage = {
    requested_pages: 2,
    received_pages: 2,
    truncated: true,
    has_more: true,
    unknowns: ['candle_finality_unknown', 'full_history_not_observed'],
  };
  state.result.warnings = ['Synthetic selected page scope only; more data exists.'];
}
async function marketCatalog(page: Page, { include = true } = {}) {
  await page.route('**/api/v1/market/catalog*', (route) =>
    route.fulfill({
      json: {
        items: include
          ? [
              {
                capture_id: captureId,
                endpoint: '/api/v1/candles',
                symbol: 'ALPHA',
                interval: '1m',
                adjusted: false,
                currencies: ['KRW'],
                retrieved_at: completed,
                candle_count: 200,
                status: 'supported',
                reason: null,
                response_contract_sha256: 'f'.repeat(64),
              },
            ]
          : [],
        total_count: 101,
        supported_count: 101,
        unsupported_count: 0,
        invalid_count: 0,
        truncated_count: 100,
        observation_age: {
          checked_at: completed,
          capture_id: captureId,
          observed_at: completed,
          age_seconds: 0,
          status: 'observed',
          max_age_seconds: null,
        },
      },
    }),
  );
}

test('truncated market completion preserves each observation and explicitly prepares new investigation inputs', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await marketCatalog(page);
  await open(page, data);
  await market(page);
  await submit(page).click();
  marketResult(state);
  finish(state);
  await refresh(page);
  await expect(receipt(page)).toContainText(/잘림|페이지 상한/);
  await expect(receipt(page)).toContainText(/미확인/);
  await expect(paper(page)).toBeEnabled();
  await investigate(page).click();
  const investigations = page.getByRole('region', { name: 'AI 조사', exact: true });
  await expect(investigations).toBeVisible();
  await expect(investigations.getByRole('listbox', { name: '시장 원자료 · 선택' })).toHaveValues([
    captureId,
  ]);
  expect(state.submissions).toHaveLength(1);
});

test('daily or adjusted completion is available for research but cannot prepare paper execution', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await open(page, data);
  await market(page);
  await submit(page).click();
  marketResult(state, { eligible: false, interval: '1d', adjusted: true });
  finish(state);
  await refresh(page);
  await expect(investigate(page)).toBeEnabled();
  await expect(paper(page)).toBeDisabled();
});

function emptyPaperBook(data: Fixture): PaperBookDetail {
  return {
    id: '28000000-0000-4000-8000-000000000099',
    label: '관측 연결 합성 원장',
    account_seq: '101',
    mode: 'prospective',
    snapshot_id: data.first.id,
    seed: {
      label: '관측 연결 합성 원장',
      account_seq: '101',
      snapshot_id: data.first.id,
      mode: 'prospective',
      initial_cash: [{ currency: 'KRW', amount: '1000' }],
      holdings: [],
    },
    state: {
      schema_version: 1,
      cash: [{ currency: 'KRW', amount: '1000' }],
      positions: [],
      costs: [],
      realized: [],
      marks: [],
      arithmetic_precision: 256,
      arithmetic_rounded: false,
      orders_enabled: false,
      valuation: [],
    },
    revision: 1,
    created_at: started,
    updated_at: started,
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

test('two explicit paper handoffs retain both completed captures outside the bounded catalog', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await marketCatalog(page, { include: false });
  const book = emptyPaperBook(data);
  let paperWrites = 0;
  await page.route(/\/api\/v1\/paper\/books(?:\/.*|\?.*)?$/, (route) => {
    if (route.request().method() !== 'GET') paperWrites++;
    return route.fulfill({
      json: new URL(route.request().url()).pathname.endsWith(book.id)
        ? book
        : { items: [book], total_count: 1, omitted_count: 0 },
    });
  });
  await page.route(/\/api\/v1\/capital-plans(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [], total_count: 0, omitted_count: 0, invalid_count: 0 } }),
  );
  await open(page, data);
  await market(page);
  await submit(page).click();
  marketResult(state);
  finish(state);
  await refresh(page);
  await expect(paper(page)).toBeEnabled();
  await paper(page).click();
  const paperPanel = page.getByRole('region', { name: '모의 매매', exact: true });
  await paperPanel
    .getByRole('combobox', { name: '모의 원장 선택', exact: true })
    .selectOption(book.id);
  const captures = paperPanel.getByRole('listbox', { name: '모의 진행에 사용할 분봉 캡처' });
  await expect(captures).toHaveValues([captureId]);
  await expect(captures.getByRole('option', { selected: true })).toContainText(/연결.*완료 분봉/);
  expect(paperWrites).toBe(0);
  await receipt(page).getByRole('button', { name: '다른 수집 준비' }).click();
  state.job = null;
  await market(page);
  await submit(page).click();
  marketResult(state);
  const secondCapture = '5'.repeat(64);
  state.result.job_id = state.job!.id;
  state.result.capture_ids = [secondCapture];
  state.result.observations[0].id = secondCapture;
  finish(state);
  await refresh(page);
  await expect(paper(page)).toBeEnabled();
  await paper(page).click();
  await expect(captures).toHaveValues([captureId, secondCapture]);
  await expect(captures.getByRole('option', { selected: true })).toHaveCount(2);
  await expect(paperPanel.getByRole('button', { name: '선택 관측으로 모의 진행' })).toBeEnabled();
  expect(paperWrites).toBe(0);
});

test('definite submission rejection permits explicit correction after read-only recovery finds no job', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.rejectSubmit = true;
  await open(page, data);
  await market(page);
  await region(page).locator('[name="symbol"]').fill('A'.repeat(33));
  await submit(page).click();
  await expect(receipt(page)).toContainText('입력 거부 · 미접수');
  await refresh(page);
  expect(state.job).toBeNull();
  expect(state.recoveries.length).toBeGreaterThan(0);
  expect(
    state.recoveries.every((body) => JSON.stringify(body) === JSON.stringify(state.submissions[0])),
  ).toBe(true);
  await expect(
    receipt(page).getByRole('button', { name: '원래 입력으로 같은 요청 접수' }),
  ).toHaveCount(0);
  const correct = receipt(page).getByRole('button', { name: '다른 수집 준비' });
  await expect(correct).toBeEnabled();
  await correct.click();
  await expect(receipt(page)).toHaveCount(0);
  await expect(region(page).getByRole('combobox', { name: '수집 종류' })).toBeEnabled();
  state.rejectSubmit = false;
  await region(page).locator('[name="symbol"]').fill('ALPHA');
  await submit(page).click();
  await expect(receipt(page)).toContainText('대기');
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[1].request_key).not.toBe(state.submissions[0].request_key);
  expect(state.submissions[1].parameters).toMatchObject({
    endpoint: 'candles',
    query: { symbol: 'ALPHA' },
    pages: 2,
  });
});
