import { expect, test, type Page } from '@playwright/test';
import type { JobServiceStatus, JobView, WorkerObservation } from '../src/lib/api/types.gen';

const instant = '2026-09-14T07:00:00Z';
const jobId = '27000000-0000-4000-8000-000000000001';
const region = (page: Page) => page.getByRole('region', { name: '지금 확인할 상태', exact: true });

function worker(overrides: Partial<WorkerObservation> = {}): WorkerObservation {
  return {
    id: '27000000-0000-4000-8000-000000000002',
    owner: 'synthetic-idle-worker',
    started_at: instant,
    heartbeat_at: instant,
    expires_at: '2026-09-14T07:01:00Z',
    stopped_at: null,
    state: 'idle',
    liveness: 'live',
    allow_network: false,
    allow_codex: false,
    codex_web_search_allowed: null,
    current_job_id: null,
    ...overrides,
  };
}

function job(): JobView {
  return {
    id: jobId,
    workspace_key: 'a'.repeat(64),
    kind: 'research-context',
    parameters: {},
    request_key: 'synthetic-operations-job',
    status: 'queued',
    available_at: instant,
    created_at: instant,
    updated_at: instant,
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

/** HTTP UI fixtures only; independent real DB/worker checks cover server observations. */
async function operationalFixture(page: Page) {
  const state = {
    status: {
      enabled: true,
      database: 'reachable',
      checked_at: instant,
      workspace_key: 'a'.repeat(64),
      workers: [worker()],
      workers_truncated: false,
      queued_count: 1,
      running_count: 0,
      waiting_jobs: [
        {
          job_id: jobId,
          kind: 'research-context',
          available_at: instant,
          required_capabilities: [],
          eligible_worker_ids: [],
          reasons: ['awaiting_worker_claim'],
        },
      ],
      waiting_jobs_truncated: false,
    } as JobServiceStatus,
    jobs: [job()],
    statusCalls: 0,
    jobCalls: 0,
    failStatus: false,
  };
  await page.route('**/api/v1/jobs/status', (route) => {
    state.statusCalls++;
    return state.failStatus ? route.abort('failed') : route.fulfill({ json: state.status });
  });
  await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) => {
    state.jobCalls++;
    return route.fulfill({ json: { items: state.jobs } });
  });
  return state;
}

test('first screen distinguishes idle, stale and stopped worker capabilities with unknown balances', async ({
  page,
}) => {
  const state = await operationalFixture(page);
  state.status.workers.push(
    worker({
      id: '27000000-0000-4000-8000-000000000003',
      owner: 'old-network-worker',
      liveness: 'stale',
      allow_network: true,
    }),
    worker({
      id: '27000000-0000-4000-8000-000000000004',
      owner: 'stopped-codex-worker',
      liveness: 'stopped',
      state: 'stopped',
      stopped_at: instant,
      allow_codex: true,
      codex_web_search_allowed: true,
    }),
  );
  state.status.waiting_jobs[0].reasons = ['scheduled', 'capability_not_allowed'];
  await page.goto('/');
  const panel = region(page);
  await expect(panel.getByText('응답 확인', { exact: true })).toBeVisible();
  await expect(panel.getByText('접근 확인', { exact: true })).toBeVisible();
  await expect(panel.getByText('1개 생존 관측 유효')).toBeVisible();
  await expect(panel.getByText(/예약 시각 전 · 필요 기능을 허용한 worker 없음/)).toBeVisible();
  await panel.getByText('worker별 관측 시각과 허용 설정').click();
  await expect(panel.getByText('old-network-worker · 생존 관측 만료')).toBeVisible();
  await expect(panel.getByText('stopped-codex-worker · 종료')).toBeVisible();
  await expect(panel.getByText('Codex 웹 검색 미확인', { exact: true }).first()).toBeVisible();
  await expect(panel.getByText('Codex 웹 검색 허용 설정', { exact: true })).toBeVisible();
  await expect(panel.getByText(/로그인이나 외부 API 성공을 확인한 결과가 아닙니다/)).toBeVisible();
  await expect(page.getByRole('combobox', { name: '계좌 관측', exact: true })).toHaveValue('');
  await expect(page.getByTestId('buying-power-KRW')).toHaveCount(0);
});

test('DB outage preserves saved research and unknown operational counts without stale success', async ({
  page,
  request,
}) => {
  const state = await operationalFixture(page);
  await page.goto('/');
  const panel = region(page);
  await expect(panel.getByText('접근 확인', { exact: true })).toBeVisible();
  state.status = {
    ...state.status,
    database: 'unavailable',
    workers: [],
    queued_count: null,
    running_count: null,
    waiting_jobs: [],
  };
  await panel.getByRole('button', { name: '운영 상태 다시 확인' }).click();
  await expect(panel.getByText('접근 실패', { exact: true })).toBeVisible();
  await expect(panel.getByText('실행 중 미확인 · 대기 미확인')).toBeVisible();
  await expect(panel.getByText('1개 생존 관측 유효')).toHaveCount(0);
  const { items } = await (await request.get('/api/v1/account-snapshots')).json();
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(items[0].id);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  await expect(
    panel.getByRole('region', { name: '계좌 자료 신선도' }).getByText('오래된 저장 관측'),
  ).toBeVisible();
  state.failStatus = true;
  await panel.getByRole('button', { name: '운영 상태 다시 확인' }).click();
  await expect(panel.getByRole('alert')).toContainText('운영 상태를 확인하지 못했습니다.');
  await expect(panel.getByText('접근 실패', { exact: true })).toHaveCount(0);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
});

test('market observations outside the returned catalog stay unknown instead of absent', async ({
  page,
}) => {
  await page.route('**/api/v1/market/catalog*', (route) =>
    route.fulfill({
      json: {
        items: [],
        total_count: 101,
        supported_count: 1,
        unsupported_count: 100,
        invalid_count: 0,
        truncated_count: 1,
        observation_age: {
          checked_at: instant,
          capture_id: null,
          observed_at: null,
          age_seconds: null,
          status: 'not_observed',
          max_age_seconds: null,
        },
      },
    }),
  );
  await page.goto('/');
  const market = region(page).getByRole('region', { name: '시장 자료 신선도' });
  await expect(market).toContainText('현재 목록의 지원 시장 관측 미확인');
  await expect(market).toContainText('목록 밖 1개');
  await expect(market).not.toContainText('관측 없음');
});

test('pending work refreshes while visible and stops after terminal state or hidden screen', async ({
  page,
}) => {
  await page.clock.install();
  const state = await operationalFixture(page);
  await page.goto('/');
  await expect(region(page).getByText('실행 중 0개 · 대기 1개')).toBeVisible();
  const firstCalls = state.jobCalls;
  await page.clock.runFor(5100);
  await expect.poll(() => state.jobCalls).toBeGreaterThan(firstCalls);
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  const hiddenCalls = { jobs: state.jobCalls, status: state.statusCalls };
  await page.clock.runFor(20000);
  expect(state.jobCalls).toBe(hiddenCalls.jobs);
  expect(state.statusCalls).toBe(hiddenCalls.status);
  state.jobs[0].status = 'succeeded';
  state.status.queued_count = 0;
  state.status.waiting_jobs = [];
  await page.evaluate(() => {
    Object.defineProperty(document, 'visibilityState', {
      configurable: true,
      get: () => 'visible',
    });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await page.clock.runFor(5100);
  await expect(page.getByTestId(`job-${jobId}`).getByText('완료', { exact: true })).toBeVisible();
  const finishedCalls = state.jobCalls;
  await page.clock.runFor(20000);
  expect(state.jobCalls).toBe(finishedCalls);
});

test('automatic operational refresh has a five-minute limit and manual refresh starts a new window', async ({
  page,
}) => {
  await page.clock.install();
  const state = await operationalFixture(page);
  state.status.queued_count = 0;
  state.status.waiting_jobs = [];
  state.jobs = [];
  await page.goto('/');
  await expect(region(page).getByText('접근 확인', { exact: true })).toBeVisible();
  const beforeWindow = state.statusCalls;
  await page.clock.fastForward(301000);
  await expect.poll(() => state.statusCalls).toBeGreaterThan(beforeWindow);
  await expect(region(page).getByRole('button', { name: '운영 상태 다시 확인' })).toBeEnabled();
  const afterWindow = state.statusCalls;
  await page.clock.runFor(60000);
  expect(state.statusCalls).toBe(afterWindow);
  await region(page).getByRole('button', { name: '운영 상태 다시 확인' }).click();
  await expect(region(page).getByRole('button', { name: '운영 상태 다시 확인' })).toBeEnabled();
  const afterManual = state.statusCalls;
  await page.clock.runFor(30100);
  await expect.poll(() => state.statusCalls).toBeGreaterThan(afterManual);
});
