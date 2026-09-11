import { defineConfig } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  timeout: 30_000,
  reporter: 'list',
  outputDir: process.env.TRADING_WEB_QA_DIR ?? join(tmpdir(), 'trading-web-e2e-results'),
  use: {
    baseURL: 'http://127.0.0.1:8871',
    browserName: 'chromium',
    viewport: { width: 1536, height: 1024 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: 'node e2e/server.mjs populated 8871',
      url: 'http://127.0.0.1:8871/api/v1/health',
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: 'node e2e/server.mjs empty 8872',
      url: 'http://127.0.0.1:8872/api/v1/health',
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
});
