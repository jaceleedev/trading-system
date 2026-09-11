import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import type { AccountSnapshotsResponse, InvestmentContext } from '../src/lib/api/types.gen';

async function savedData(request: APIRequestContext) {
  const snapshots = await request.get('/api/v1/account-snapshots');
  expect(snapshots.ok()).toBeTruthy();
  const accounts = (await snapshots.json()) as AccountSnapshotsResponse;
  const context = (await (await request.get('/api/v1/context')).json()) as InvestmentContext;
  return { accounts: accounts.items, context };
}

function trackRuntime(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  return errors;
}

test('empty stores render a useful account and research state without inventing data', async ({
  page,
}) => {
  const errors = trackRuntime(page);
  await page.goto('http://127.0.0.1:8872');
  await expect(page).toHaveTitle(/투자 작업실/);
  await expect(page.getByRole('heading', { name: '투자 작업실', exact: true })).toBeVisible();
  await expect(page.getByText('저장된 연구 기록이 없습니다.')).toBeVisible();
  await expect(page.getByRole('combobox', { name: '계좌 관측' })).toHaveValue('');
  await expect(page.getByTestId('buying-power-KRW')).toHaveCount(0);
  await page.getByRole('button', { name: '저장 자료 다시 읽기' }).click();
  await expect(page.getByText('저장된 연구 기록이 없습니다.')).toBeVisible();
  expect(errors).toEqual([]);
});

test('account selection preserves fractions, currencies, unknown values, and explicit switching', async ({
  page,
  request,
}) => {
  const errors = trackRuntime(page);
  const { accounts } = await savedData(request);
  const first = accounts.find((item) => item.account_seq === '101')!;
  const second = accounts.find((item) => item.account_seq === '202')!;
  await page.goto('/');
  const selector = page.getByRole('combobox', { name: '계좌 관측' });
  await expect(selector).toHaveValue('');
  await expect(page.getByTestId('buying-power-KRW')).toHaveCount(0);
  await selector.selectOption(first.id);
  await expect(page.getByTestId('buying-power-KRW')).toHaveText('5,000,000');
  await expect(page.getByTestId('buying-power-USD')).toHaveText('3,500.5');
  const account = page.getByRole('region', { name: '계좌', exact: true });
  await expect(account.getByRole('cell', { name: '0.125', exact: true })).toBeVisible();
  await expect(account.getByText('현금 잔고 미확인 · 통화별 금액')).toBeVisible();
  await account.getByText('진행 중 주문 보기', { exact: true }).click();
  await expect(account.getByRole('cell', { name: 'KRW 미확인', exact: true })).toBeVisible();
  await selector.selectOption(second.id);
  await expect(page.getByTestId('buying-power-KRW')).toHaveText('250,000');
  await expect(page.getByTestId('buying-power-USD')).toHaveText('125.25');
  await selector.selectOption('');
  await expect(page.getByTestId('buying-power-KRW')).toHaveCount(0);
  expect(errors).toEqual([]);
});

test('a saved decision leads to its hypothesis and evidence while preserving the original account reference', async ({
  page,
  request,
}, testInfo) => {
  const errors = trackRuntime(page);
  const { accounts } = await savedData(request);
  await page.goto('/');
  await page
    .getByRole('combobox', { name: '계좌 관측' })
    .selectOption(accounts.find((item) => item.account_seq === '202')!.id);
  await page.getByRole('button', { name: /합성 계좌 101에서 근거와 판단의 연결 확인/ }).click();
  const detail = page.getByRole('region', { name: '연구 기록 상세' });
  await expect(
    detail.getByRole('heading', { name: '합성 계좌 101에서 근거와 판단의 연결 확인', exact: true }),
  ).toBeVisible();
  const originalSnapshot = accounts.find((item) => item.account_seq === '101')!.id;
  await expect(
    detail.getByText(`${originalSnapshot.slice(0, 10)}…${originalSnapshot.slice(-6)}`, {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByRole('heading', { name: '투자 작업실', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('desktop.png') });
  await detail.getByRole('button', { name: /이 판단의 재검토/ }).click();
  await expect(
    detail.getByRole('heading', { name: '재검토 결과 · 미해결', exact: true }),
  ).toBeVisible();
  await detail.getByRole('button', { name: /검토한 판단/ }).click();
  await detail.getByRole('button', { name: /연결 가설/ }).click();
  await expect(
    detail.getByRole('heading', { name: '합성 시연 ALPHA의 실적 개선 지속 여부', exact: true }),
  ).toBeVisible();
  await detail.getByRole('button', { name: /찬성 근거/ }).click();
  await expect(
    detail
      .getByText('합성 시연: ALPHA의 실적 개선을 가정한다. 실제 시장 자료가 아니다.', {
        exact: true,
      })
      .first(),
  ).toBeVisible();
  await expect(page.getByTestId('buying-power-KRW')).toHaveText('250,000');
  expect(errors).toEqual([]);
});

test('search and record-kind filters respond to keyboard input', async ({ page }) => {
  await page.goto('/');
  const research = page.getByRole('region', { name: '판단과 근거', exact: true });
  await page.getByRole('combobox', { name: '기록 종류' }).selectOption('hypothesis');
  await expect(
    research.getByRole('button', { name: /합성 시연 ALPHA의 실적 개선 지속 여부/ }),
  ).toBeVisible();
  const search = page.getByRole('textbox', { name: '기록 검색' });
  await search.focus();
  await page.keyboard.type('no-matching-record');
  await expect(research.getByText('검색 결과가 없습니다.')).toBeVisible();
  await search.fill('');
  await expect(
    research.getByRole('button', { name: /합성 시연 ALPHA의 실적 개선 지속 여부/ }),
  ).toBeVisible();
});

test('a failed reload stops showing old account data as a successful response and can recover', async ({
  page,
  request,
}) => {
  const { accounts } = await savedData(request);
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측' }).selectOption(accounts[0].id);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  await page.route('**/api/v1/context?*', (route) =>
    route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({
        error: { code: 'invalid_record', message: 'Saved records failed validation.' },
      }),
    }),
  );
  await page.getByRole('button', { name: '저장 자료 다시 읽기' }).click();
  await expect(
    page.getByRole('region', { name: '계좌', exact: true }).getByRole('alert'),
  ).toBeVisible();
  await expect(page.getByTestId('buying-power-KRW')).toHaveCount(0);
  await page.unroute('**/api/v1/context?*');
  await page.getByRole('button', { name: '저장 자료 다시 읽기' }).click();
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
});

test('a delayed old account response cannot replace the currently selected account', async ({
  page,
  request,
}) => {
  const { accounts } = await savedData(request);
  const first = accounts.find((item) => item.account_seq === '101')!;
  const second = accounts.find((item) => item.account_seq === '202')!;
  let release!: () => void;
  const waiting = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const intercepted = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(`**/api/v1/context?snapshot_id=${first.id}*`, async (route) => {
    const response = await route.fetch();
    started();
    await waiting;
    await route.fulfill({ response }).catch(() => {});
  });
  await page.goto('/');
  const selector = page.getByRole('combobox', { name: '계좌 관측' });
  await selector.selectOption(first.id);
  await intercepted;
  await selector.selectOption(second.id);
  await expect(page.getByTestId('buying-power-KRW')).toHaveText('250,000');
  release();
  await expect(selector).toHaveValue(second.id);
  await expect(page.getByTestId('buying-power-KRW')).toHaveText('250,000');
});

test('mobile keeps account controls and record details usable without page overflow', async ({
  page,
  request,
}, testInfo) => {
  const errors = trackRuntime(page);
  const { accounts } = await savedData(request);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  await page.getByRole('combobox', { name: '계좌 관측' }).selectOption(accounts[0].id);
  await expect(page.getByTestId('buying-power-KRW')).toBeVisible();
  await page.getByRole('button', { name: /합성 계좌 101에서 근거와 판단의 연결 확인/ }).click();
  await expect(page.getByRole('region', { name: '연구 기록 상세' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  );
  await page.getByRole('heading', { name: '투자 작업실', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true });
  expect(errors).toEqual([]);
});
