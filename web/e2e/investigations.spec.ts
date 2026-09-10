import { expect, test, type Page } from '@playwright/test';
import type {
  InvestigationCreate,
  InvestigationInput,
  InvestigationOutput,
  InvestigationResponse,
  InvestigationRevise,
  JobView,
  MarketCatalog,
} from '../src/lib/api/types.gen';

const firstId = '20a00000-0000-4000-8000-000000000001';
const secondId = '20a00000-0000-4000-8000-000000000002';
const instant = '2026-09-10T09:00:00Z';
const captureA = 'a'.repeat(64);
const captureB = 'b'.repeat(64);
const panel = (page: Page) => page.getByRole('region', { name: 'AI 조사', exact: true });

function context(purpose: string): InvestigationInput {
  return {
    input_id: 'f'.repeat(64),
    purpose,
    snapshot_id: null,
    capture_ids: [],
    evidence_ids: [],
    symbols: [],
    as_of: instant,
    mode: 'synthetic',
  };
}
function activeJob(id: string, status: JobView['status'] = 'queued'): JobView {
  return {
    id,
    workspace_key: 'c'.repeat(64),
    request_key: 'synthetic-job',
    kind: 'codex-investigation',
    parameters: {},
    status,
    available_at: instant,
    created_at: instant,
    updated_at: instant,
    finished_at: null,
    max_attempts: 1,
    attempt_count: status === 'running' ? 1 : 0,
    cancel_requested: false,
    lease_expires_at: null,
    result: null,
    error_code: null,
    attempts: [],
  };
}
function investigation(id = firstId, purpose = '합성 기회 조사'): InvestigationResponse {
  return {
    investigation: {
      id,
      workspace_key: 'c'.repeat(64),
      request_key: `synthetic-${id}`,
      status: 'active',
      current_revision: 1,
      context_input: context(purpose),
      active_job_id: id,
      latest_completed_revision: null,
      latest_result: null,
      next_review_at: null,
      event_conditions: [],
      created_at: instant,
      updated_at: instant,
      revisions: [],
      omitted_revision_count: 0,
    },
    active_job: activeJob(id),
    latest_output: null,
    latest_execution: null,
  };
}
const output: InvestigationOutput = {
  summary: '합성 결과: ALPHA와 BETA의 기회를 비교했습니다.',
  rationale: '가격·실적·반대 근거를 함께 검토하는 합성 시연입니다.',
  opportunities: [
    {
      symbol: 'ALPHA',
      market: 'KR',
      action: 'watch',
      rationale: '수익성 변화가 이어지는지 조사합니다.',
      evidence_ids: [],
    },
  ],
  opposing_evidence: ['합성 반대 근거: 수요 둔화 가능성'],
  uncertainties: ['실제 수요와 가격 반응은 미확인'],
  alternatives: ['BETA 조사와 현금 유지 비교'],
  review_after: null,
  review_conditions: [{ kind: 'evidence', symbol: 'ALPHA' }],
  research_requests: [],
  source_findings: [
    {
      url: 'https://example.com/synthetic-finding',
      title: '합성 모델 제시 출처',
      claim: '독립 확인 전의 출처 제안',
      source_published_at: null,
    },
  ],
};

/** These responses are UI fixtures, not evidence that a model or broker was called. */
async function mockInvestigations(page: Page, initial: InvestigationResponse[] = []) {
  const state = {
    items: initial,
    creates: [] as InvestigationCreate[],
    reviews: [] as { id: string; body: InvestigationRevise }[],
    pauses: [] as { id: string; expected_revision: number }[],
    unavailable: false,
    detailError: false,
    loseCreate: false,
    loseReview: false,
    holdId: '',
    hold: undefined as Promise<void> | undefined,
  };
  await page.route('**/api/v1/health', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: { ...(await response.json()), jobs_enabled: true, read_only: false },
    });
  });
  const catalog: MarketCatalog = {
    items: [captureA, captureB].map((id, index) => ({
      capture_id: id,
      endpoint: '/api/v1/candles',
      symbol: index ? 'BETA' : 'ALPHA',
      interval: '1d',
      adjusted: false,
      currencies: [index ? 'USD' : 'KRW'],
      retrieved_at: instant,
      candle_count: 1,
      status: 'supported',
      reason: null,
      response_contract_sha256: 'e'.repeat(64),
    })),
    total_count: 2,
    supported_count: 2,
    unsupported_count: 0,
    invalid_count: 0,
    truncated_count: 0,
  };
  await page.route('**/api/v1/market/catalog', (route) => route.fulfill({ json: catalog }));
  await page.route(/\/api\/v1\/investigations(?:\?.*)?$/, async (route) => {
    if (state.unavailable) {
      await route.fulfill({
        status: 503,
        json: { error: { code: 'investigations_unavailable', message: 'Synthetic failure' } },
      });
      return;
    }
    if (route.request().method() === 'GET') {
      await route.fulfill({
        json: {
          items: state.items.map((item) => ({
            ...item.investigation,
            revisions: [],
            omitted_revision_count: item.investigation.current_revision,
          })),
        },
      });
      return;
    }
    const body = route.request().postDataJSON() as InvestigationCreate;
    state.creates.push(body);
    let result = state.items.find((item) => item.investigation.request_key === body.request_key);
    if (!result) {
      result = investigation(state.items.length ? secondId : firstId, body.purpose);
      result.investigation.request_key = body.request_key;
      result.investigation.context_input = {
        ...context(body.purpose),
        snapshot_id: body.snapshot_id ?? null,
        capture_ids: body.capture_ids ?? [],
        evidence_ids: body.evidence_ids ?? [],
        symbols: body.symbols ?? [],
        mode: body.mode ?? 'prospective',
      };
      state.items.unshift(result);
    }
    if (state.loseCreate) {
      state.loseCreate = false;
      await route.abort('failed');
    } else await route.fulfill({ json: result });
  });
  await page.route(/\/api\/v1\/investigations\/[a-f0-9-]{36}$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-1)!;
    if (state.hold && id === state.holdId) await state.hold;
    await route
      .fulfill(
        state.detailError
          ? {
              status: 503,
              json: { error: { code: 'investigations_unavailable', message: 'Synthetic failure' } },
            }
          : { json: state.items.find((item) => item.investigation.id === id) },
      )
      .catch(() => {});
  });
  await page.route(/\/api\/v1\/investigations\/[a-f0-9-]{36}\/revisions$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const body = route.request().postDataJSON() as InvestigationRevise;
    const duplicate = state.reviews.some(
      (item) => item.id === id && item.body.request_key === body.request_key,
    );
    state.reviews.push({ id, body });
    const result = state.items.find((item) => item.investigation.id === id)!;
    if (!duplicate && body.expected_revision !== result.investigation.current_revision) {
      await route.fulfill({
        status: 409,
        json: { error: { code: 'investigation_conflict', message: 'Synthetic conflict' } },
      });
      return;
    }
    if (!duplicate) {
      result.investigation.current_revision += 1;
      result.investigation.status = 'active';
      result.investigation.context_input = {
        ...context(body.purpose),
        snapshot_id: body.snapshot_id ?? null,
        capture_ids: body.capture_ids ?? [],
        evidence_ids: body.evidence_ids ?? [],
        symbols: body.symbols ?? [],
        mode: body.mode ?? 'synthetic',
      };
      result.investigation.active_job_id = id;
      result.active_job = activeJob(id);
    }
    if (state.loseReview) {
      state.loseReview = false;
      await route.abort('failed');
    } else await route.fulfill({ json: result });
  });
  await page.route(/\/api\/v1\/investigations\/[a-f0-9-]{36}\/pause$/, async (route) => {
    const id = new URL(route.request().url()).pathname.split('/').at(-2)!;
    const body = route.request().postDataJSON();
    state.pauses.push({ id, ...body });
    const result = state.items.find((item) => item.investigation.id === id)!;
    result.investigation.status = 'paused';
    if (result.active_job) result.active_job.cancel_requested = true;
    await route.fulfill({ json: result });
  });
  return state;
}

test('disabled investigations preserve the account workbench without starting a model request', async ({
  page,
}) => {
  let requests = 0;
  page.on('request', (request) => {
    if (request.url().includes('/investigations')) requests += 1;
  });
  await page.goto('/');
  await expect(
    panel(page).getByText('이 작업실에서는 AI 조사 실행이 꺼져 있습니다.'),
  ).toBeVisible();
  await expect(panel(page).getByText('새 조사 시작', { exact: true })).toHaveCount(0);
  await expect(
    page.getByRole('button', { name: /합성 계좌 101에서 근거와 판단의 연결 확인/ }),
  ).toBeVisible();
  expect(requests).toBe(0);
});

test('a new investigation submits the explicit snapshot and selected capture and evidence IDs', async ({
  page,
  request,
}) => {
  const state = await mockInvestigations(page);
  const { items } = await (await request.get('/api/v1/account-snapshots')).json();
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측', exact: true }).selectOption(items[0].id);
  await panel(page).getByText('새 조사 시작', { exact: true }).click();
  await panel(page)
    .getByRole('textbox', { name: '조사 목적', exact: true })
    .fill('합성 신규 기회 비교');
  await panel(page)
    .getByRole('textbox', { name: '관심 종목 · 선택', exact: true })
    .fill('ALPHA, BETA');
  await panel(page)
    .getByRole('listbox', { name: '시장 원자료 · 선택', exact: true })
    .selectOption([captureA, captureB]);
  const evidenceSelect = panel(page).getByRole('listbox', {
    name: '연구 근거 · 선택',
    exact: true,
  });
  const evidenceId = await evidenceSelect.locator('option').first().getAttribute('value');
  await evidenceSelect.selectOption(evidenceId!);
  await panel(page).getByRole('button', { name: '조사 접수', exact: true }).click();
  await expect(
    panel(page).getByText('조사를 접수했습니다. 모델 실행 여부는 아래 작업 상태에서 확인하세요.'),
  ).toBeVisible();
  expect(state.creates).toHaveLength(1);
  expect(state.creates[0]).toMatchObject({
    purpose: '합성 신규 기회 비교',
    snapshot_id: items[0].id,
    capture_ids: [captureA, captureB],
    evidence_ids: [evidenceId],
    symbols: ['ALPHA', 'BETA'],
    mode: 'synthetic',
  });
  expect(state.creates[0].request_key).toMatch(/^[a-f0-9-]{36}$/);
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 조사 상세' })
      .getByText('대기', { exact: true }),
  ).toBeVisible();
  await expect(
    panel(page).getByText('저장된 AI 결과가 아직 없습니다. 접수와 모델 실행 완료는 구분됩니다.'),
  ).toBeVisible();
});

test('an uncertain create keeps the same original request after the selected account changes', async ({
  page,
  request,
}) => {
  const state = await mockInvestigations(page);
  state.loseCreate = true;
  const { items } = await (await request.get('/api/v1/account-snapshots')).json();
  await page.goto('/');
  const select = page.getByRole('combobox', { name: '계좌 관측', exact: true });
  await select.selectOption(items[0].id);
  await panel(page).getByText('새 조사 시작', { exact: true }).click();
  await panel(page)
    .getByRole('textbox', { name: '조사 목적', exact: true })
    .fill('합성 응답 유실 검사');
  await panel(page).getByRole('button', { name: '조사 접수', exact: true }).click();
  await expect(panel(page).getByText(/접수 여부 확인 대기 · 원래 목적/)).toBeVisible();
  await select.selectOption(items[1].id);
  await panel(page).getByRole('button', { name: '같은 접수 다시 확인', exact: true }).click();
  await expect(panel(page).getByText(/조사를 접수했습니다/)).toBeVisible();
  expect(state.creates).toHaveLength(2);
  expect(state.creates[1]).toEqual(state.creates[0]);
  expect(state.items).toHaveLength(1);
});

test('completed analysis separates model suggestions, unverified links and process metadata', async ({
  page,
}, testInfo) => {
  const result = investigation();
  result.investigation.active_job_id = null;
  result.active_job = null;
  result.investigation.latest_completed_revision = 1;
  result.latest_output = output;
  result.latest_execution = {
    source: 'local_subprocess',
    cli_version: 'synthetic-cli-version',
    started_at: instant,
    finished_at: instant,
    exit_code: 0,
    requested_model: 'synthetic-model-declaration',
    requested_reasoning_effort: null,
    model_source: 'explicit',
    reported_model: null,
    model_identity_verified: false,
    completed_event: true,
    allow_web_search: true,
    web_search_count: 1,
    input_sha256: 'f'.repeat(64),
    output_schema_sha256: 'e'.repeat(64),
    event_stream_sha256: 'd'.repeat(64),
  };
  await mockInvestigations(page, [result]);
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto('/');
  await panel(page)
    .getByRole('button', { name: /합성 기회 조사/ })
    .click();
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
  for (const title of [
    '반대 근거',
    '불확실성',
    '비교한 대안',
    '후속 검토 조건',
    '모델이 제시한 출처 · 확인 전',
  ])
    await expect(panel(page).getByRole('heading', { name: title, exact: true })).toBeVisible();
  await panel(page).getByText('실행 정보와 입력·버전', { exact: true }).click();
  await expect(panel(page).getByText('synthetic-model-declaration', { exact: true })).toBeVisible();
  await expect(panel(page).getByText(/모델 식별 검증 없음/)).toBeVisible();
  await expect(panel(page).getByRole('link', { name: '합성 모델 제시 출처' })).toHaveAttribute(
    'rel',
    'noopener noreferrer',
  );
  await panel(page).getByRole('heading', { name: '최근 AI 결과' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-20-investigations.png') });
  expect(errors).toEqual([]);
});

test('manual revision preserves its original request on retry and pause does not claim running work has stopped', async ({
  page,
}) => {
  const initial = investigation();
  initial.latest_output = output;
  initial.investigation.latest_completed_revision = 1;
  initial.investigation.active_job_id = null;
  initial.active_job = null;
  const state = await mockInvestigations(page, [initial]);
  state.loseReview = true;
  await page.goto('/');
  await panel(page)
    .getByRole('button', { name: /합성 기회 조사/ })
    .click();
  await panel(page).getByRole('button', { name: '입력을 정해 재검토' }).click();
  await panel(page)
    .getByRole('textbox', { name: '재검토 목적', exact: true })
    .fill('새 합성 근거로 기회를 다시 비교');
  await panel(page)
    .getByRole('listbox', { name: '재검토 시장 원자료', exact: true })
    .selectOption(captureB);
  await panel(page).getByRole('button', { name: '새 버전 조사 접수' }).click();
  await expect(panel(page).getByText(/재검토 접수 여부 확인 대기/)).toBeVisible();
  await panel(page).getByRole('button', { name: '같은 재검토 다시 확인' }).click();
  await expect(panel(page).getByText(/새 입력으로 재검토를 접수했습니다/)).toBeVisible();
  expect(state.reviews).toHaveLength(2);
  expect(state.reviews[1]).toEqual(state.reviews[0]);
  expect(state.reviews[0].body.expected_revision).toBe(1);
  expect(state.reviews[0].body.capture_ids).toEqual([captureB]);
  await expect(panel(page).getByText(/아래 결과는 이전 버전 1의 결과입니다/)).toBeVisible();
  state.items[0].active_job = activeJob(firstId, 'running');
  await panel(page).getByRole('button', { name: '조사 목록 다시 읽기' }).click();
  await panel(page).getByRole('button', { name: '조사 일시 정지' }).click();
  await expect(
    panel(page)
      .getByRole('region', { name: '선택한 조사 상세' })
      .getByText('일시 정지', { exact: true }),
  ).toBeVisible();
  await expect(panel(page).getByText(/작업 실행 중 · 실행 시도 1\/1 · 취소 요청됨/)).toBeVisible();
  expect(state.pauses).toEqual([{ id: firstId, expected_revision: 2 }]);
});

test('current revision follow-up collection jobs stay distinct from older AI output', async ({
  page,
}) => {
  const result = investigation();
  result.investigation.current_revision = 2;
  result.investigation.latest_completed_revision = 1;
  result.latest_output = output;
  result.research_jobs = [
    { ...activeJob(firstId), kind: 'account-sync', max_attempts: 3 },
    {
      ...activeJob(secondId, 'running'),
      kind: 'market-capture',
      cancel_requested: true,
      max_attempts: 3,
    },
  ];
  const state = await mockInvestigations(page, [result]);
  await page.goto('/');
  await panel(page)
    .getByRole('button', { name: /합성 기회 조사/ })
    .click();
  const collection = panel(page).getByRole('region', { name: '현재 버전의 후속 자료 수집' });
  await expect(
    collection.getByRole('heading', { name: '현재 버전 2의 후속 자료 수집' }),
  ).toBeVisible();
  await expect(panel(page).getByText(/아래 결과는 이전 버전 1의 결과입니다/)).toBeVisible();
  await expect(collection.getByText('계좌 관측 수집 · 대기', { exact: true })).toBeVisible();
  await expect(collection.getByText('시장 자료 수집 · 실행 중', { exact: true })).toBeVisible();
  await expect(collection.getByText('실행 시도 1/3 · 취소 요청됨', { exact: true })).toBeVisible();
  state.items[0].research_jobs = [
    { ...result.research_jobs[0], status: 'succeeded', attempt_count: 1 },
    {
      ...result.research_jobs[1],
      status: 'failed',
      cancel_requested: false,
      attempt_count: 3,
      error_code: 'network_disabled',
    },
  ];
  await panel(page).getByRole('button', { name: '조사 목록 다시 읽기' }).click();
  await expect(collection.getByText('계좌 관측 수집 · 완료', { exact: true })).toBeVisible();
  await expect(collection.getByText('시장 자료 수집 · 실패', { exact: true })).toBeVisible();
  await expect(
    collection.getByText('실행 시도 3/3 · 오류 network_disabled', { exact: true }),
  ).toBeVisible();
  await expect(collection.getByText('시장 자료 수집 · 실행 중', { exact: true })).toHaveCount(0);
});

test('late investigation responses and failed refreshes never display an old result as current', async ({
  page,
}) => {
  const first = investigation();
  first.latest_output = output;
  const second = investigation(secondId, '합성 다른 조사');
  second.latest_output = { ...output, summary: '두 번째 조사 결과만 표시' };
  const state = await mockInvestigations(page, [first, second]);
  let release!: () => void;
  state.holdId = firstId;
  state.hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.goto('/');
  await panel(page)
    .getByRole('button', { name: /합성 기회 조사/ })
    .click();
  await expect(panel(page).getByText('조사 입력과 실행 결과를 읽고 있습니다.')).toBeVisible();
  await panel(page)
    .getByRole('button', { name: /합성 다른 조사/ })
    .click();
  await expect(panel(page).getByText('두 번째 조사 결과만 표시')).toBeVisible();
  release();
  await expect(panel(page).getByText(output.summary, { exact: true })).toHaveCount(0);
  state.detailError = true;
  await panel(page).getByRole('button', { name: '조사 목록 다시 읽기' }).click();
  await expect(panel(page).getByRole('alert')).toContainText('조사 저장소에 연결하지 못했습니다.');
  await expect(panel(page).getByText('두 번째 조사 결과만 표시')).toHaveCount(0);
  state.unavailable = true;
  await panel(page).getByRole('button', { name: '조사 목록 다시 읽기' }).click();
  await expect(panel(page).getByRole('button', { name: /합성 다른 조사/ })).toHaveCount(0);
});

test('mobile investigation inputs and results remain within the page width', async ({
  page,
}, testInfo) => {
  const result = investigation();
  result.latest_output = output;
  await mockInvestigations(page, [result]);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await panel(page).getByText('새 조사 시작', { exact: true }).click();
  await panel(page)
    .getByRole('textbox', { name: '조사 목적', exact: true })
    .fill('모바일 합성 조사');
  await panel(page).scrollIntoViewIfNeeded();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: testInfo.outputPath('investigations-mobile.png') });
  await panel(page)
    .getByRole('button', { name: /합성 기회 조사/ })
    .click();
  await expect(panel(page).getByText(output.summary, { exact: true })).toBeVisible();
});
