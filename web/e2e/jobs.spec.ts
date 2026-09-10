import { expect, test, type Page } from '@playwright/test';
import type { AccountSnapshotsResponse, JobSubmission, JobView } from '../src/lib/api/types.gen';

const firstJobId = '18a00000-0000-4000-8000-000000000001';
const secondJobId = '18a00000-0000-4000-8000-000000000002';
const instant = '2026-09-10T09:00:00+00:00';

function job(overrides: Partial<JobView> = {}): JobView {
  return {
    id: firstJobId,
    workspace_key: 'a'.repeat(64),
    kind: 'research-context',
    parameters: { snapshot_id: null, max_records: 50 },
    request_key: 'synthetic-browser-job',
    status: 'queued',
    available_at: instant,
    created_at: instant,
    updated_at: instant,
    finished_at: null,
    max_attempts: 3,
    attempt_count: 0,
    cancel_requested: false,
    lease_expires_at: null,
    result: null,
    error_code: null,
    attempts: [],
    ...overrides,
  };
}

/** Browser interaction fixtures only; no job DB, worker, broker or credentials are used. */
async function mockJobs(page: Page, initial: JobView[] = [], loseFirstSubmission = false) {
  const state = {
    enabled: true,
    unavailable: false,
    items: initial,
    submissions: [] as JobSubmission[],
    cancellations: [] as string[],
    loseSubmission: loseFirstSubmission,
  };
  await page.route('**/api/v1/jobs/status', (route) =>
    route.fulfill({ json: { enabled: state.enabled } }),
  );
  await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, async (route) => {
    if (route.request().method() === 'GET') {
      if (state.unavailable) {
        await route.fulfill({
          status: 503,
          json: { error: { code: 'jobs_unavailable', message: 'Synthetic unavailable state' } },
        });
      } else {
        await route.fulfill({
          json: { items: state.items.map((item) => ({ ...item, attempts: [] })) },
        });
      }
      return;
    }
    const body = route.request().postDataJSON() as JobSubmission;
    state.submissions.push(body);
    let saved = state.items.find((item) => item.request_key === body.request_key);
    if (!saved) {
      saved = job({
        id: state.items.length ? secondJobId : firstJobId,
        kind: body.kind,
        parameters: body.parameters,
        request_key: body.request_key,
        available_at: body.available_at ?? instant,
        max_attempts: body.max_attempts ?? 3,
      });
      state.items.unshift(saved);
    }
    if (state.loseSubmission) {
      state.loseSubmission = false;
      await route.abort('failed');
    } else {
      await route.fulfill({ json: { job: saved } });
    }
  });
  await page.route(/\/api\/v1\/jobs\/[a-f0-9-]{36}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1);
    await route.fulfill({ json: { job: state.items.find((item) => item.id === id) } });
  });
  await page.route(/\/api\/v1\/jobs\/[a-f0-9-]{36}\/cancel$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    state.cancellations.push(id);
    const saved = state.items.find((item) => item.id === id)!;
    if (saved.status === 'queued') saved.status = 'cancelled';
    saved.cancel_requested = true;
    await route.fulfill({ json: { job: saved } });
  });
  return state;
}

test('disabled jobs leave the real offline account and research views usable', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  await expect(panel.getByText('이 작업실에서는 작업 실행이 꺼져 있습니다.')).toBeVisible();
  await expect(panel.getByRole('button', { name: '검증 작업 접수' })).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: /합성 계좌 101에서 근거와 판단의 연결 확인/ }),
  ).toBeVisible();
  await panel.getByRole('button', { name: '작업 목록 다시 읽기' }).click();
  await expect(panel.getByText('이 작업실에서는 작업 실행이 꺼져 있습니다.')).toBeVisible();
  expect(errors).toEqual([]);
});

test('enabled empty jobs submit only saved-data validation with the explicitly selected snapshot', async ({
  page,
  request,
}, testInfo) => {
  const state = await mockJobs(page);
  const response = await request.get('/api/v1/account-snapshots');
  const { items } = (await response.json()) as AccountSnapshotsResponse;
  const snapshot = items.find((item) => item.account_seq === '101')!;
  await page.goto('/');
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  await expect(panel.getByText('등록된 작업이 없습니다.')).toBeVisible();
  await page.getByRole('combobox', { name: '계좌 관측' }).selectOption(snapshot.id);
  await panel.getByLabel(/예약 시각/).fill('2026-12-01T18:30');
  await panel.getByRole('button', { name: '검증 작업 접수' }).click();
  await expect(panel.getByText('저장 자료 검증 작업을 접수했습니다.')).toBeVisible();
  await expect(
    panel.getByTestId(`job-${firstJobId}`).getByText('대기', { exact: true }),
  ).toBeVisible();
  expect(state.submissions).toHaveLength(1);
  expect(state.submissions[0]).toMatchObject({
    kind: 'research-context',
    parameters: { snapshot_id: snapshot.id, max_records: 50 },
    max_attempts: 3,
  });
  expect(state.submissions[0].request_key).toMatch(/^[a-f0-9-]{36}$/);
  expect(state.submissions[0].available_at).toMatch(/Z$/);
  await expect(panel.getByLabel(/token|secret|password|토큰|비밀번호|자격증명/i)).toHaveCount(0);
  await expect(panel.locator('input[type="password"]')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /계좌 관측 수집|시장 자료 수집/ })).toHaveCount(0);
  await panel.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-18-jobs.png') });
});

test('an uncertain submission retries the same UUID and original snapshot after the selection changes', async ({
  page,
  request,
}) => {
  const state = await mockJobs(page, [], true);
  const response = await request.get('/api/v1/account-snapshots');
  const { items } = (await response.json()) as AccountSnapshotsResponse;
  await page.goto('/');
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  const selector = page.getByRole('combobox', { name: '계좌 관측' });
  await selector.selectOption(items[0].id);
  await panel.getByRole('button', { name: '검증 작업 접수' }).click();
  await expect(panel.getByText(/접수 여부가 아직 확인되지 않았습니다/)).toBeVisible();
  await selector.selectOption(items[1].id);
  await panel.getByRole('button', { name: '같은 요청 다시 확인' }).click();
  await expect(panel.getByText('저장 자료 검증 작업을 접수했습니다.')).toBeVisible();
  expect(state.submissions).toHaveLength(2);
  expect(state.submissions[1]).toEqual(state.submissions[0]);
  expect(state.submissions[1].parameters.snapshot_id).toBe(items[0].id);
  expect(state.items).toHaveLength(1);
});

test('a running cancellation stays a request and its detailed attempts are loaded separately', async ({
  page,
}) => {
  const state = await mockJobs(page, [
    job({
      status: 'running',
      attempt_count: 1,
      attempts: [
        {
          number: 1,
          owner: 'synthetic-browser-worker',
          status: 'running',
          started_at: instant,
          heartbeat_at: instant,
          finished_at: null,
          error_code: null,
        },
      ],
    }),
  ]);
  await page.goto('/');
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  const row = panel.getByTestId(`job-${firstJobId}`);
  await expect(row.getByText('실행 중', { exact: true })).toBeVisible();
  await row.getByRole('button', { name: '시도 기록', exact: true }).click();
  await expect(
    panel.getByRole('region', { name: '선택한 작업의 실행 시도' }).getByText('1회 · 실행 중'),
  ).toBeVisible();
  await row.getByRole('button', { name: /작업 취소 요청/ }).click();
  await expect(row.getByText('취소 요청됨', { exact: true })).toBeVisible();
  await expect(row.getByText('실행 중', { exact: true })).toBeVisible();
  await expect(row.getByText('취소됨', { exact: true })).toHaveCount(0);
  await expect(row.getByRole('button', { name: /작업 취소 요청/ })).toHaveCount(0);
  expect(state.cancellations).toEqual([firstJobId]);
});

test('job errors hide stale rows while account data stays available, then refresh recovers', async ({
  page,
  request,
}) => {
  const state = await mockJobs(page, [job()]);
  const response = await request.get('/api/v1/account-snapshots');
  const { items } = (await response.json()) as AccountSnapshotsResponse;
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측' }).selectOption(items[0].id);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  await expect(panel.getByTestId(`job-${firstJobId}`)).toBeVisible();
  state.unavailable = true;
  await panel.getByRole('button', { name: '작업 목록 다시 읽기' }).click();
  await expect(panel.getByRole('alert')).toContainText('작업 저장소에 연결하지 못했습니다.');
  await expect(panel.getByTestId(`job-${firstJobId}`)).toHaveCount(0);
  await expect(panel.getByRole('button', { name: '검증 작업 접수' })).toBeDisabled();
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  state.unavailable = false;
  await panel.getByRole('button', { name: '작업 목록 다시 읽기' }).click();
  await expect(panel.getByTestId(`job-${firstJobId}`)).toBeVisible();
  await expect(panel.getByRole('button', { name: '검증 작업 접수' })).toBeEnabled();
});

test('mobile job controls remain usable and queued work can be cancelled', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await mockJobs(page, [job()]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const panel = page.getByRole('region', { name: '작업 실행', exact: true });
  const row = panel.getByTestId(`job-${firstJobId}`);
  await row.getByRole('button', { name: /작업 취소 요청/ }).click();
  await expect(row.getByText('취소됨', { exact: true })).toBeVisible();
  await panel.scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: testInfo.outputPath('jobs-mobile.png') });
  expect(errors).toEqual([]);
});
