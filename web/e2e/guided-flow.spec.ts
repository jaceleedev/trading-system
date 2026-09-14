import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { mockWorkspaceStatus } from './workspace-status';
import type {
  AccountSnapshotsResponse,
  InvestmentContext,
  InvestigationOutput,
  InvestigationResponse,
} from '../src/lib/api/types.gen';

const investigationId = '29000000-0000-4000-8000-000000000001';
const otherInvestigationId = '29000000-0000-4000-8000-000000000002';
const bookId = '29000000-0000-4000-8000-000000000003';
const outputId = '2'.repeat(64);
const newerOutputId = '3'.repeat(64);
const planId = '4'.repeat(64);
const reportId = '5'.repeat(64);
const newerSnapshotId = '6'.repeat(64);
const now = '2026-09-14T07:00:00Z';
const later = '2026-09-14T08:00:00Z';
const alternativeId = 'compare-beta';
const panel = (page: Page) => page.getByRole('region', { name: '투자 단계 이어가기', exact: true });
const account = (page: Page) => page.getByRole('combobox', { name: '계좌 관측', exact: true });
const choice = (page: Page, name: string) =>
  panel(page).getByRole('combobox', { name, exact: true });
const next = (page: Page, name: string) => panel(page).getByRole('button', { name, exact: true });

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
type Selection = {
  snapshot_id: string | null;
  investigation_id: string | null;
  revision: number | null;
  output_id: string | null;
  plan_id: string | null;
  alternative_id: string | null;
  book_id: string | null;
  report_id: string | null;
};
type Issue = {
  code: string;
  stage: 'investigations' | 'capital' | 'paper' | 'outcomes';
  message: string;
};
const output: InvestigationOutput = {
  summary: '과거 버전 1에 고정한 합성 조사 결과',
  rationale: '실제 외부 실행 없이 단계 선택을 검증하는 HTTP fixture입니다.',
  opportunities: [],
  opposing_evidence: ['합성 반대 근거'],
  uncertainties: ['실제 가격과 손익은 미확인'],
  alternatives: ['기존 보유 유지', 'BETA 비교'],
  review_after: null,
  review_conditions: [],
  research_requests: [],
  source_findings: [],
};

function investigation(data: Fixture, id = investigationId): InvestigationResponse {
  return {
    investigation: {
      id,
      workspace_key: 'a'.repeat(64),
      request_key: `synthetic-guided-${id}`,
      status: 'active',
      current_revision: 2,
      context_input: {
        input_id: '7'.repeat(64),
        purpose: id === investigationId ? '합성 과거 조사' : '합성 다른 계좌 조사',
        snapshot_id: id === investigationId ? data.first.id : data.second.id,
        capture_ids: [],
        evidence_ids: [],
        symbols: [],
        as_of: later,
        mode: 'synthetic',
      },
      active_job_id: null,
      latest_completed_revision: 2,
      latest_result: null,
      next_review_at: null,
      event_conditions: [],
      created_at: now,
      updated_at: later,
      revisions: [],
      omitted_revision_count: 0,
    },
    active_job: null,
    latest_output: { ...output, summary: '현재 버전 2의 별도 합성 조사 결과' },
    latest_execution: null,
  };
}

function selected(params: URLSearchParams): Selection {
  return {
    snapshot_id: params.get('snapshot_id'),
    investigation_id: params.get('investigation_id'),
    revision: params.has('revision') ? Number(params.get('revision')) : null,
    output_id: params.get('output_id'),
    plan_id: params.get('plan_id'),
    alternative_id: params.get('alternative_id'),
    book_id: params.get('book_id'),
    report_id: params.get('report_id'),
  };
}

function url(data: Fixture, extra: Record<string, string> = {}) {
  return `/?${new URLSearchParams({
    snapshot_id: newerSnapshotId,
    investigation_id: investigationId,
    revision: '1',
    output_id: outputId,
    ...extra,
  })}`;
}

/** HTTP fixtures exercise client behavior only. Server lineage checks and runtime DB effects have separate tests. */
async function mocks(page: Page, data: Fixture, enabled = true) {
  const state = {
    reads: [] as Selection[],
    writes: [] as { method: string; url: string; body: string | null }[],
    failure: false,
    missingPlans: false,
    missingBooks: false,
    missingReports: false,
    reject: null as Issue | null,
    heldSnapshot: '',
    heldReport: '',
    hold: null as Promise<void> | null,
  };
  page.on('request', (request) => {
    if (
      request.url().includes('/api/v1/') &&
      !['GET', 'HEAD', 'OPTIONS'].includes(request.method())
    ) {
      state.writes.push({ method: request.method(), url: request.url(), body: request.postData() });
    }
  });
  await mockWorkspaceStatus(page, enabled);
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), jobs_enabled: enabled, read_only: !enabled },
    });
  });
  await page.route('**/api/v1/account-snapshots', (route) =>
    route.fulfill({
      json: {
        items: [
          ...data.accounts.items,
          {
            ...data.first,
            id: newerSnapshotId,
            collection_started_at: now,
            collection_completed_at: later,
          },
        ],
      },
    }),
  );
  await page.route(`**/api/v1/context?snapshot_id=${newerSnapshotId}*`, (route) =>
    route.fulfill({
      json: {
        ...data.context,
        account: { ...data.context.account, id: newerSnapshotId },
        snapshot_freshness: { ...data.context.snapshot_freshness, snapshot_id: newerSnapshotId },
      },
    }),
  );
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: {
        items: [
          investigation(data).investigation,
          investigation(data, otherInvestigationId).investigation,
        ],
      },
    }),
  );
  await page.route(/\/api\/v1\/investigations\/[a-f0-9-]{36}$/, (route) =>
    route.fulfill({ json: investigation(data) }),
  );
  await page.route(/\/api\/v1\/jobs(?:\?.*)?$/, (route) => route.fulfill({ json: { items: [] } }));
  await page.route(/\/api\/v1\/workflows(?:\?.*)?$/, (route) =>
    route.fulfill({ json: { items: [], total_count: 0, omitted_count: 0 } }),
  );
  await page.route(/\/api\/v1\/(?:capital-plans|paper\/books|outcomes)(?:\?.*)?$/, (route) =>
    route.fulfill({
      json: { items: [], total_count: 0, omitted_count: 0, invalid_count: 0 },
    }),
  );
  // Destination detail errors deliberately test that a failed independent read cannot invent a completed action.
  await page.route(/\/api\/v1\/(?:capital-plans|paper\/books|outcomes)\/[a-f0-9-]+$/, (route) =>
    route.fulfill({
      status: 503,
      json: {
        error: {
          code: 'synthetic_detail_unavailable',
          message: 'Synthetic destination detail unavailable.',
        },
      },
    }),
  );
  await page.route(/\/api\/v1\/guided-flow(?:\?.*)?$/, async (route) => {
    const selection = selected(new URL(route.request().url()).searchParams);
    state.reads.push({ ...selection });
    const shouldFail = state.failure;
    const issues: Issue[] = [];
    const otherAccount = selection.snapshot_id === data.second.id;
    if (state.reject) {
      issues.push(state.reject);
      if (state.reject.stage === 'investigations') {
        selection.investigation_id = null;
        selection.revision = null;
        selection.output_id = null;
      }
      if (['investigations', 'capital'].includes(state.reject.stage)) {
        selection.plan_id = null;
        selection.alternative_id = null;
      }
      if (['investigations', 'capital', 'paper'].includes(state.reject.stage))
        selection.book_id = null;
      selection.report_id = null;
    }
    if (otherAccount && selection.investigation_id === investigationId) {
      issues.push({
        code: 'account_mismatch',
        stage: 'investigations',
        message: '선택한 조사와 현재 계좌가 달라 이어갈 선택을 해제했습니다.',
      });
      Object.assign(selection, {
        investigation_id: null,
        revision: null,
        output_id: null,
        plan_id: null,
        alternative_id: null,
        book_id: null,
        report_id: null,
      });
    }
    const hasInvestigation = !!selection.investigation_id;
    const hasSavedSource = hasInvestigation || !!selection.plan_id;
    if (hasInvestigation && !selection.revision) selection.revision = 1;
    if (hasInvestigation && !selection.output_id)
      selection.output_id = selection.revision === 1 ? outputId : newerOutputId;
    if (hasInvestigation && selection.snapshot_id === newerSnapshotId)
      issues.push({
        code: 'frozen_snapshot_preserved',
        stage: 'investigations',
        message: '조사에 고정된 과거 계좌 관측을 유지합니다. 상단 계좌 관측은 바꾸지 않습니다.',
      });
    if (state.missingPlans && hasInvestigation)
      issues.push({
        code: 'plan_missing',
        stage: 'capital',
        message: '연결된 계획이 없습니다. 자금 계획에서 예산과 대안을 직접 입력해 주세요.',
      });
    if (state.missingBooks && selection.plan_id)
      issues.push({
        code: 'book_missing',
        stage: 'paper',
        message: '연결할 모의 원장이 없습니다. 모의 매매에서 원장을 직접 만들어 주세요.',
      });
    if (state.missingReports && selection.book_id)
      issues.push({
        code: 'report_missing',
        stage: 'outcomes',
        message: '연결된 기간 보고서가 없습니다. 기간과 원장을 선택해 결과를 직접 계산해 주세요.',
      });
    const response = {
      workspace_key: 'f'.repeat(64),
      selection,
      context: {
        account_seq: otherAccount ? '202' : selection.snapshot_id ? '101' : null,
        frozen_snapshot_id: hasSavedSource ? data.first.id : null,
        mode: hasSavedSource ? 'synthetic' : null,
        currencies: hasSavedSource ? ['USD'] : [],
      },
      investigation: hasInvestigation
        ? {
            id: selection.investigation_id,
            purpose: '합성 과거 조사',
            current_revision: 2,
            revisions: [1, 2].map((number) => ({
              number,
              input_id: '7'.repeat(64),
              output_id: number === 1 ? outputId : newerOutputId,
              snapshot_id: data.first.id,
              mode: 'synthetic',
            })),
            output:
              selection.revision === 1
                ? output
                : { ...output, summary: '현재 버전 2의 별도 합성 조사 결과' },
          }
        : null,
      plans:
        hasSavedSource && !state.missingPlans
          ? [
              {
                id: planId,
                recorded_at: now,
                snapshot_id: data.first.id,
                mode: 'synthetic',
                alternatives: [
                  {
                    key: alternativeId,
                    label: 'BETA 비교 대안',
                    eligibility: 'eligible',
                    currencies: ['USD'],
                  },
                ],
              },
            ]
          : [],
      books:
        selection.plan_id && !state.missingBooks
          ? [
              {
                id: bookId,
                label: '합성 이어갈 원장',
                snapshot_id: data.first.id,
                mode: 'synthetic',
                currencies: ['USD'],
                linked: true,
              },
            ]
          : [],
      reports:
        selection.book_id && !state.missingReports
          ? [{ id: reportId, start_at: now, end_at: later, recorded_at: later, mode: 'synthetic' }]
          : [],
      issues,
      jobs_available: enabled,
      orders_enabled: false,
    };
    if (
      state.hold &&
      (state.heldSnapshot === selection.snapshot_id || state.heldReport === selection.report_id)
    )
      await state.hold;
    await route
      .fulfill(
        shouldFail
          ? {
              status: 503,
              json: { error: { code: 'guided_flow_unavailable', message: '합성 연결 조회 실패' } },
            }
          : { json: response },
      )
      .catch(() => {});
  });
  return state;
}

test('historical revision and its frozen observation survive stage navigation, back and reload without writes', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.goto(url(data));
  await expect(account(page)).toHaveValue(newerSnapshotId);
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  await expect(choice(page, '이어갈 조사 버전')).toHaveValue('1');
  await expect(panel(page)).toContainText('조사에 고정된 과거 계좌 관측을 유지');
  await choice(page, '연결된 자금 계획').selectOption(planId);
  await choice(page, '이어갈 대안').selectOption(alternativeId);
  await next(page, '계획·대안으로 이동').click();
  await expect(page).toHaveURL(/stage=capital/);
  await choice(page, '이어갈 모의 원장').selectOption(bookId);
  await next(page, '모의 원장으로 이동').click();
  await expect(page).toHaveURL(/stage=paper/);
  await choice(page, '연결된 기간 보고서').selectOption(reportId);
  await next(page, '기간 결과로 이동').click();
  await expect(page).toHaveURL(/stage=outcomes/);
  const selectedUrl = page.url();
  await page.reload();
  await expect(choice(page, '연결된 기간 보고서')).toHaveValue(reportId);
  await expect(choice(page, '이어갈 조사 버전')).toHaveValue('1');
  await expect(account(page)).toHaveValue(newerSnapshotId);
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/stage=paper/);
  await page.goForward();
  await expect(page).toHaveURL(selectedUrl);
  await expect(choice(page, '이어갈 모의 원장')).toHaveValue(bookId);
  expect(state.reads.some((value) => value.output_id === outputId && value.revision === 1)).toBe(
    true,
  );
  expect(state.writes).toEqual([]);
});

test('changing revision requires a fresh server linkage and drops dependent choices', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.goto(
    url(data, {
      plan_id: planId,
      alternative_id: alternativeId,
      book_id: bookId,
      report_id: reportId,
      stage: 'outcomes',
    }),
  );
  await expect(choice(page, '이어갈 모의 원장')).toHaveValue(bookId);
  await choice(page, '이어갈 조사 버전').selectOption('2');
  await expect(
    panel(page).getByText('현재 버전 2의 별도 합성 조사 결과', { exact: true }),
  ).toBeVisible();
  await expect(choice(page, '연결된 자금 계획')).toHaveValue('');
  await expect(choice(page, '이어갈 대안')).toHaveValue('');
  await expect.poll(() => state.reads.at(-1)?.revision).toBe(2);
  expect(state.reads.at(-1)?.plan_id).toBeNull();
  expect(state.reads.at(-1)?.book_id).toBeNull();
  expect(state.reads.at(-1)?.report_id).toBeNull();
  expect(state.writes).toEqual([]);
});

test('a late account response cannot restore the previous account or attach its result', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  let release!: () => void;
  state.heldSnapshot = newerSnapshotId;
  state.hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.goto(url(data, { plan_id: planId, alternative_id: alternativeId, book_id: bookId }));
  await expect.poll(() => state.reads.length).toBeGreaterThan(0);
  await account(page).selectOption(data.second.id);
  await expect(account(page)).toHaveValue(data.second.id);
  await expect
    .poll(() => state.reads.some((value) => value.snapshot_id === data.second.id))
    .toBe(true);
  release();
  await expect(panel(page).getByText(output.summary, { exact: true })).toHaveCount(0);
  await expect(choice(page, '연결된 자금 계획')).toHaveValue('');
  await expect(account(page)).toHaveValue(data.second.id);
  expect(state.writes).toEqual([]);
});

test('a failed revalidation hides old results and the next step until a successful explicit retry', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.goto(url(data, { plan_id: planId, alternative_id: alternativeId }));
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  state.failure = true;
  await next(page, '연결 다시 확인').click();
  await expect(panel(page).getByRole('alert')).toBeVisible();
  await expect(panel(page).getByText(output.summary, { exact: true })).toHaveCount(0);
  await expect(next(page, '모의 원장으로 이동')).toHaveCount(0);
  state.failure = false;
  await next(page, '연결 다시 확인').click();
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
});

test('back navigation during a pending connection read fences its late response and forward revalidates', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  await page.goto(
    url(data, { plan_id: planId, alternative_id: alternativeId, book_id: bookId, stage: 'paper' }),
  );
  await expect(choice(page, '이어갈 모의 원장')).toHaveValue(bookId);
  let release!: () => void;
  state.heldReport = reportId;
  state.hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await choice(page, '연결된 기간 보고서').selectOption(reportId);
  await expect.poll(() => state.reads.some((value) => value.report_id === reportId)).toBe(true);
  await page.goBack();
  await expect(page).not.toHaveURL(new RegExp(`report_id=${reportId}`));
  release();
  await expect(choice(page, '연결된 기간 보고서')).toHaveValue('');
  await expect(choice(page, '이어갈 모의 원장')).toHaveValue(bookId);
  await page.goForward();
  await expect(choice(page, '연결된 기간 보고서')).toHaveValue(reportId);
  await expect(account(page)).toHaveValue(newerSnapshotId);
  expect(state.writes).toEqual([]);
});

for (const rejected of [
  {
    code: 'source_mismatch',
    stage: 'capital',
    message: '각 자료는 존재하지만 선택한 조사 출력에서 만든 계획이 아닙니다.',
  },
  {
    code: 'currency_mismatch',
    stage: 'paper',
    message: '선택한 대안과 모의 원장의 통화가 일치하지 않습니다.',
  },
  {
    code: 'mode_mismatch',
    stage: 'paper',
    message: '선택한 계획과 모의 원장의 모드가 일치하지 않습니다.',
  },
  {
    code: 'report_lineage_mismatch',
    stage: 'outcomes',
    message: '이 기간 보고서에는 선택한 원장과 대안의 연결이 없습니다.',
  },
] as const) {
  test(`persisted IDs cannot bypass server rejection: ${rejected.code}`, async ({
    page,
    request,
  }) => {
    const data = await fixture(request);
    const state = await mocks(page, data);
    state.reject = rejected;
    await page.goto(
      url(data, {
        plan_id: planId,
        alternative_id: alternativeId,
        book_id: bookId,
        report_id: reportId,
        stage: 'outcomes',
      }),
    );
    await expect(panel(page)).toContainText(rejected.message);
    await expect(choice(page, '연결된 기간 보고서')).toHaveValue('');
    if (rejected.stage === 'capital')
      await expect(choice(page, '연결된 자금 계획')).toHaveValue('');
    if (rejected.stage === 'paper') await expect(choice(page, '이어갈 모의 원장')).toHaveValue('');
    expect(state.writes).toEqual([]);
  });
}

test('missing plans provide the existing input route without inventing a budget or plan', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.missingPlans = true;
  await page.goto(url(data));
  await expect(panel(page)).toContainText('자금 계획에서 예산과 대안을 직접 입력');
  await next(page, '계획·대안으로 이동').click();
  await expect(page).toHaveURL(/stage=capital/);
  await expect(page.getByRole('region', { name: '자금 계획', exact: true })).toBeVisible();
  expect(state.writes).toEqual([]);
});

test('saved guided references can be revalidated when execution is disabled', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data, false);
  await page.goto(
    `/?${new URLSearchParams({ snapshot_id: newerSnapshotId, plan_id: planId, alternative_id: alternativeId, stage: 'capital' })}`,
  );
  await expect(choice(page, '연결된 자금 계획')).toHaveValue(planId);
  await expect(panel(page).getByText(output.summary, { exact: true })).toHaveCount(0);
  await next(page, '2. 계획·대안').click();
  await page.reload();
  await expect(choice(page, '연결된 자금 계획')).toHaveValue(planId);
  expect(state.writes).toEqual([]);
});

test('missing books and reports guide explicit creation or calculation without writing on navigation', async ({
  page,
  request,
}) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  state.missingBooks = true;
  await page.goto(url(data, { plan_id: planId, alternative_id: alternativeId }));
  await expect(panel(page)).toContainText('모의 매매에서 원장을 직접 만들어');
  await next(page, '모의 원장으로 이동').click();
  await expect(page).toHaveURL(/stage=paper/);
  expect(state.writes).toEqual([]);
  state.missingBooks = false;
  state.missingReports = true;
  await page.goto(url(data, { plan_id: planId, alternative_id: alternativeId, book_id: bookId }));
  await expect(panel(page)).toContainText('기간과 원장을 선택해 결과를 직접 계산');
  await next(page, '기간 결과로 이동').click();
  await expect(page).toHaveURL(/stage=outcomes/);
  expect(state.writes).toEqual([]);
});

test('mobile guided selection and next actions remain readable within the viewport', async ({
  page,
  request,
}, testInfo) => {
  const data = await fixture(request);
  const state = await mocks(page, data);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(url(data, { plan_id: planId, alternative_id: alternativeId, book_id: bookId }));
  await expect(page).toHaveTitle('투자 작업실 · Trading Research');
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  await panel(page).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('feature-29-guided-mobile.png') });
  await next(page, '기간 결과로 이동').click();
  await expect(page).toHaveURL(/stage=outcomes/);
  expect(errors).toEqual([]);
  expect(state.writes).toEqual([]);
});
