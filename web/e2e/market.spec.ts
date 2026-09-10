import { expect, test, type Page } from '@playwright/test';
import type {
  MarketCaptureSummary,
  MarketCatalog,
  MarketEvidenceEvent,
  MarketSeries,
  MarketView,
  MarketViewRequest,
  ObservedCandle,
  ResearchResponse,
} from '../src/lib/api/types.gen';

const captureId = (value: string) => value.repeat(64);
const observed = '2026-09-10T09:00:00+00:00';
const eventId = captureId('9');
const sourceContract = captureId('f');

function capture(
  value: string,
  symbol: string,
  interval: '1m' | '1d',
  adjusted = false,
): MarketCaptureSummary {
  return {
    capture_id: captureId(value),
    endpoint: '/api/v1/candles',
    symbol,
    interval,
    adjusted,
    currencies: [symbol === 'ALPHA' ? 'KRW' : 'USD'],
    retrieved_at: observed,
    candle_count: 20,
    status: 'supported',
    reason: null,
    response_contract_sha256: sourceContract,
  };
}

function points(series: MarketCaptureSummary): ObservedCandle[] {
  return Array.from({ length: 20 }, (_, index) => {
    const base = series.symbol === 'ALPHA' ? 1000 + index * 6 : 100 + index;
    const instant = new Date(Date.parse('2026-09-10T00:00:00Z') + index * 60_000);
    const day = `2026-08-${String(index + 10).padStart(2, '0')}`;
    const close =
      series.symbol === 'BETA' && index === 19
        ? '119.123456789012345678901234'
        : String(base + (index % 3 === 0 ? -2 : 4));
    const value = {
      id: (index + 1).toString(16).padStart(64, '0'),
      open: String(base),
      high: String(base + 7),
      low: String(base - 5),
      close,
      volume: index === 19 ? '9007199254740993' : String(100 + index * 13),
      observed_at: observed,
      last_observed_at: observed,
      capture_ids: [series.capture_id],
    };
    return {
      ...value,
      source_timestamp:
        series.interval === '1m'
          ? new Date(instant.getTime() + 60_000).toISOString()
          : `${day}T00:00:00${series.symbol === 'ALPHA' ? '+09:00' : '-04:00'}`,
      period_start: series.interval === '1m' ? instant.toISOString() : null,
      period_end:
        series.interval === '1m' ? new Date(instant.getTime() + 60_000).toISOString() : null,
      session_date: series.interval === '1d' ? day : null,
      revision_count: 0,
      revisions: [value],
      finality: 'unknown',
    };
  });
}

const event: MarketEvidenceEvent = {
  record_id: eventId,
  symbol: 'ALPHA',
  market: 'KR',
  event_kind: 'earnings',
  occurred_at: '2026-09-10T00:01:30Z',
  source_published_at: '2026-09-10T00:01:35Z',
  retrieved_at: '2026-09-10T08:58:00Z',
  recorded_at: observed,
  claim: '합성 ALPHA 실적 발표의 반대 근거 확인',
  mode: 'synthetic',
  source_locator: 'https://example.com/synthetic-market-evidence',
  verification: 'unverified',
};

/** Market routes are synthetic browser fixtures, not real provider observations. */
async function mockMarket(page: Page) {
  const entries = [
    capture('a', 'ALPHA', '1d'),
    capture('b', 'ALPHA', '1m'),
    capture('c', 'ALPHA', '1d', true),
    capture('d', 'BETA', '1d'),
    capture('e', 'ALPHA', '1d'),
  ];
  const catalog: MarketCatalog = {
    items: entries,
    total_count: 5,
    supported_count: 5,
    unsupported_count: 0,
    invalid_count: 0,
    truncated_count: 0,
  };
  const state = {
    catalog,
    catalogError: false,
    viewError: false,
    requests: [] as MarketViewRequest[],
    hold: undefined as Promise<void> | undefined,
    holdCapture: '',
  };
  await page.route('**/api/v1/market/catalog', async (route) => {
    await route.fulfill(
      state.catalogError
        ? { status: 503, json: { error: { code: 'internal_error', message: 'Synthetic failure' } } }
        : { json: state.catalog },
    );
  });
  await page.route('**/api/v1/market/view', async (route) => {
    const request = route.request().postDataJSON() as MarketViewRequest;
    state.requests.push(request);
    if (state.hold && request.capture_ids.includes(state.holdCapture)) await state.hold;
    if (state.viewError) {
      await route.fulfill({
        status: 503,
        json: { error: { code: 'internal_error', message: 'Synthetic failure' } },
      });
      return;
    }
    const item = entries.find((item) => item.capture_id === request.capture_ids[0])!;
    const future = !!request.as_of && Date.parse(request.as_of) < Date.parse(observed);
    const series: MarketSeries = {
      id: item.capture_id,
      provider: 'toss',
      symbol: item.symbol!,
      currency: item.currencies[0],
      interval: item.interval as '1m' | '1d',
      adjusted: item.adjusted!,
      points: points(item),
    };
    const response: MarketView = {
      schema_version: 1,
      kind: 'market_observation_view',
      id: item.capture_id,
      as_of: request.as_of ?? observed,
      generated_at: observed,
      source_capture_ids: request.capture_ids,
      excluded_future_capture_ids: future ? request.capture_ids : [],
      response_contract_sha256: sourceContract,
      series: future ? [] : [series],
      total_point_count: future ? 0 : 20,
      truncated_point_count: 0,
      historical_reproducibility: false,
      orders_enabled: false,
      warnings: [],
      events: !future && item.symbol === 'ALPHA' ? [event] : [],
      event_count: !future && item.symbol === 'ALPHA' ? 1 : 0,
      omitted_event_count: 0,
    };
    await route.fulfill({ json: response }).catch(() => {});
  });
  await page.route(`**/api/v1/research/${eventId}`, async (route) => {
    const response: ResearchResponse = {
      id: eventId,
      record: {
        schema_version: 1,
        kind: 'evidence',
        mode: 'synthetic',
        recorded_at: observed,
        author: {
          interface: 'human',
          model: null,
          reasoning_effort: null,
          identity_source: 'unknown',
        },
        payload: {
          claim: event.claim,
          source_kind: 'web',
          source_locator: event.source_locator,
          retrieved_at: event.retrieved_at,
          source_published_at: event.source_published_at,
          verification: event.verification,
          market_event: {
            symbol: event.symbol,
            market: event.market,
            event_kind: event.event_kind,
            occurred_at: event.occurred_at,
          },
          artifact: undefined,
        },
      },
    };
    await route.fulfill({ json: response });
  });
  return state;
}

const panel = (page: Page) => page.getByRole('region', { name: '시장 관측', exact: true });
const choose = (page: Page, symbol: string, currency: string) =>
  panel(page)
    .getByRole('combobox', { name: '시장 종목', exact: true })
    .selectOption(JSON.stringify([symbol, currency]));

test('the real empty market store leaves saved account and research controls available', async ({
  page,
}) => {
  await page.goto('/');
  await expect(panel(page).getByText('표시할 시장 관측이 없습니다.')).toBeVisible();
  await expect(
    page.getByRole('button', { name: /합성 계좌 101에서 근거와 판단의 연결 확인/ }),
  ).toBeVisible();
  await expect(panel(page).locator('canvas')).toHaveCount(0);
});

test('selected captures preserve decimal values, currencies and price basis in the rendered chart and table', async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const state = await mockMarket(page);
  await page.goto('/');
  await expect(page).toHaveTitle('투자 작업실 · Trading Research');
  await expect(panel(page).getByText('종목을 선택하면 저장된 관측을 표시합니다.')).toBeVisible();
  expect(state.requests).toHaveLength(0);
  await choose(page, 'ALPHA', 'KRW');
  await expect(
    panel(page).getByRole('img', { name: 'ALPHA 일봉 OHLCV 관측 차트' }).locator('canvas').first(),
  ).toBeVisible();
  expect(state.requests.at(-1)?.capture_ids).toEqual([captureId('a'), captureId('e')]);
  await panel(page).getByRole('combobox', { name: '가격 기준', exact: true }).selectOption('true');
  await expect.poll(() => state.requests.at(-1)?.capture_ids).toEqual([captureId('c')]);
  await choose(page, 'BETA', 'USD');
  await expect(panel(page).getByRole('img', { name: 'BETA 일봉 OHLCV 관측 차트' })).toBeVisible();
  await expect(
    panel(page).getByRole('cell', { name: '119.123456789012345678901234', exact: true }),
  ).toBeVisible();
  await expect(
    panel(page).getByRole('cell', { name: '9,007,199,254,740,993', exact: true }),
  ).toBeVisible();
  await expect(panel(page).getByText('BETA · USD · 일봉')).toBeVisible();
  await choose(page, 'ALPHA', 'KRW');
  await panel(page).getByRole('combobox', { name: '봉 단위', exact: true }).selectOption('1m');
  await expect(panel(page).getByRole('img', { name: 'ALPHA 1분봉 OHLCV 관측 차트' })).toBeVisible();
  await panel(page).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('feature-19-market.png') });
  expect(errors).toEqual([]);
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
});

test('explicit as-of excludes future captures and clears the old chart', async ({ page }) => {
  const state = await mockMarket(page);
  await page.goto('/');
  await choose(page, 'ALPHA', 'KRW');
  await expect(panel(page).locator('canvas').first()).toBeVisible();
  await panel(page)
    .getByLabel(/조회 기준 시각/)
    .fill('2026-01-01T12:00');
  await panel(page).getByRole('button', { name: '시각 적용' }).click();
  await expect(
    panel(page).getByText('이 기준 시각까지 수집한 표시 가능한 봉이 없습니다.'),
  ).toBeVisible();
  expect(state.requests.at(-1)?.as_of).toMatch(/Z$/);
  await expect(panel(page).locator('canvas')).toHaveCount(0);
  await expect(panel(page).getByText(/기준 이후 제외 2개/)).toBeVisible();
});

test('late responses from an earlier symbol never replace the selected chart and chart cleanup removes canvases', async ({
  page,
}) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  const state = await mockMarket(page);
  let release!: () => void;
  state.hold = new Promise<void>((resolve) => {
    release = resolve;
  });
  state.holdCapture = captureId('a');
  await page.goto('/');
  await choose(page, 'ALPHA', 'KRW');
  await expect(panel(page).getByText('선택한 시장 관측을 검증하고 있습니다.')).toBeVisible();
  await choose(page, 'BETA', 'USD');
  await expect(panel(page).getByRole('img', { name: 'BETA 일봉 OHLCV 관측 차트' })).toBeVisible();
  release();
  await expect(panel(page).getByRole('img', { name: 'ALPHA 일봉 OHLCV 관측 차트' })).toHaveCount(0);
  await panel(page).getByRole('combobox', { name: '시장 종목', exact: true }).selectOption('');
  await expect(panel(page).locator('canvas')).toHaveCount(0);
  await page.setViewportSize({ width: 800, height: 700 });
  await choose(page, 'BETA', 'USD');
  await expect(
    panel(page).getByRole('img', { name: 'BETA 일봉 OHLCV 관측 차트', exact: true }),
  ).toHaveCount(1);
  expect(errors).toEqual([]);
});

test('market errors hide stale charts and values without removing the saved account view', async ({
  page,
  request,
}) => {
  const state = await mockMarket(page);
  const { items } = await (await request.get('/api/v1/account-snapshots')).json();
  await page.goto('/');
  await page.getByLabel('계좌 관측', { exact: true }).selectOption(items[0].id);
  await choose(page, 'ALPHA', 'KRW');
  await expect(panel(page).locator('canvas').first()).toBeVisible();
  state.viewError = true;
  await panel(page).getByRole('button', { name: '시장 자료 다시 읽기' }).click();
  await expect(panel(page).getByRole('alert')).toContainText('시장 자료를 읽지 못했습니다.');
  await expect(panel(page).locator('canvas')).toHaveCount(0);
  await expect(panel(page).getByRole('table')).toHaveCount(0);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  state.viewError = false;
  await panel(page).getByRole('button', { name: '시장 자료 다시 읽기' }).click();
  await expect(panel(page).locator('canvas').first()).toBeVisible();
  state.catalogError = true;
  await panel(page).getByRole('button', { name: '시장 자료 다시 읽기' }).click();
  await expect(panel(page).getByRole('alert')).toContainText('시장 자료를 읽지 못했습니다.');
  await expect(panel(page).locator('canvas')).toHaveCount(0);
});

test('market events navigate to evidence with separate event, publication and recorded times', async ({
  page,
}) => {
  await mockMarket(page);
  await page.goto('/');
  await choose(page, 'ALPHA', 'KRW');
  const events = panel(page).getByRole('region', { name: '연결된 시장 사건' });
  await expect(
    events.getByText('일봉의 세션 범위가 미확인이므로 사건은 목록으로 표시합니다.', {
      exact: false,
    }),
  ).toBeVisible();
  await events.getByRole('button', { name: /합성 ALPHA 실적 발표/ }).click();
  const detail = page.getByRole('region', { name: '연구 기록 상세' });
  await expect(detail.getByRole('heading', { name: '연결된 시장 사건' })).toBeVisible();
  await expect(detail.getByText('ALPHA / KR', { exact: true })).toBeVisible();
  await expect(detail.getByRole('link', { name: event.source_locator })).toHaveAttribute(
    'rel',
    'noopener noreferrer',
  );
  await expect(detail.getByText('발생 시각', { exact: true })).toBeVisible();
  await expect(detail.getByText('발표 시각', { exact: true })).toBeVisible();
});

test('mobile chart, exact-value scrolling and unavailable catalog counts remain readable', async ({
  page,
}, testInfo) => {
  const state = await mockMarket(page);
  state.catalog.unsupported_count = 2;
  state.catalog.invalid_count = 1;
  state.catalog.truncated_count = 8;
  state.catalog.total_count = 16;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await choose(page, 'ALPHA', 'KRW');
  await expect(panel(page).getByText(/미지원 2개 · 검증 실패 1개/)).toBeVisible();
  await expect(panel(page).getByText(/목록 조회 한도로 8개가 생략/)).toBeVisible();
  await panel(page)
    .getByRole('img', { name: 'ALPHA 일봉 OHLCV 관측 차트', exact: true })
    .scrollIntoViewIfNeeded();
  await expect(panel(page).locator('canvas').first()).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.screenshot({ path: testInfo.outputPath('market-mobile.png') });
});
