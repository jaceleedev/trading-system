import type { Page } from '@playwright/test';

/** Synthetic fixture identity for durable browser request receipts; no operational DB is enabled. */
export async function mockWorkspaceStatus(page: Page, enabled = true) {
  await page.route('**/api/v1/jobs/status', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      json: {
        ...(await response.json()),
        enabled,
        database: enabled ? 'reachable' : 'not_checked',
        workspace_key: 'f'.repeat(64),
      },
    });
  });
}
