import { describe, expect, it } from 'vitest';
import {
  captureMatchesContext,
  marketParameters,
  readCaptureReceipt,
  receiptStorageKey,
  type CaptureEndpoint,
  type CaptureReceipt,
} from './capture';
import type { CaptureQueryField } from './api/types.gen';

const field = (
  value: Partial<CaptureQueryField> & Pick<CaptureQueryField, 'name' | 'type'>,
): CaptureQueryField => ({
  required: false,
  default: null,
  enum_values: [],
  minimum: null,
  maximum: null,
  pattern: null,
  format: null,
  ...value,
});
const candles: CaptureEndpoint = {
  alias: 'candles',
  endpoint: '/api/v1/candles',
  max_pages: 10,
  query_fields: [
    field({ name: 'symbol', type: 'string', required: true }),
    field({ name: 'interval', type: 'string', required: true, enum_values: ['1m', '1d'] }),
    field({ name: 'count', type: 'integer', minimum: 1, maximum: 1000 }),
    field({ name: 'adjusted', type: 'boolean' }),
    field({ name: 'to', type: 'string', format: 'date-time' }),
  ],
};
const receipt: CaptureReceipt = {
  version: 1,
  workspaceKey: 'workspace-a',
  body: {
    kind: 'account-sync',
    parameters: { account_seq: '9007199254740993', source_snapshot_id: 'a'.repeat(64) },
    request_key: 'same-request',
  },
  contextSnapshotId: 'a'.repeat(64),
  contextAccountSeq: '9007199254740993',
  jobId: null,
};

describe('explicit capture scope', () => {
  it('keeps symbol and zoned boundary text while parsing only bounded counts and booleans', () => {
    expect(
      marketParameters(
        candles,
        {
          symbol: 'ALPHA',
          interval: '1m',
          count: '250',
          adjusted: 'false',
          to: '2026-09-14T09:01:00+09:00',
        },
        '2',
      ),
    ).toEqual({
      endpoint: 'candles',
      query: {
        symbol: 'ALPHA',
        interval: '1m',
        count: 250,
        adjusted: false,
        to: '2026-09-14T09:01:00+09:00',
      },
      pages: 2,
    });
  });
  it('rejects missing targets, unsupported intervals, range overflow and ambiguous timestamps', () => {
    for (const changes of [
      { symbol: '' },
      { interval: '5m' },
      { count: '1001' },
      { count: '1.5' },
      { adjusted: 'yes' },
      { to: '2026-09-14T09:00:00' },
    ] as Record<string, string>[]) {
      expect(() =>
        marketParameters(candles, { symbol: 'ALPHA', interval: '1m', ...changes }, '1'),
      ).toThrow();
    }
    for (const pages of ['0', '11', '1.5', '9007199254740993'])
      expect(() => marketParameters(candles, { symbol: 'ALPHA', interval: '1m' }, pages)).toThrow();
  });
  it('respects one-page endpoints and rejects invalid calendar dates without leaking RangeError', () => {
    const calendar: CaptureEndpoint = {
      alias: 'calendar-kr',
      endpoint: '/api/v1/market-calendar/KR',
      max_pages: 1,
      query_fields: [field({ name: 'date', type: 'string', format: 'date', required: true })],
    };
    for (const date of ['2026-02-30', '2026-13-01', 'not-a-date'])
      expect(() => marketParameters(calendar, { date }, '1')).toThrow('date 날짜를 확인해 주세요.');
    expect(() => marketParameters(calendar, { date: '2026-09-14' }, '2')).toThrow();
  });
});

describe('capture receipt recovery and account isolation', () => {
  it('round-trips original request key, snapshot evidence and exact account decimal across reload', () => {
    expect(readCaptureReceipt(JSON.stringify(receipt), 'workspace-a')).toEqual(receipt);
    expect(receiptStorageKey('workspace-a')).not.toBe(receiptStorageKey('workspace-b'));
  });
  it('rejects malformed, foreign-workspace or missing original-input receipts', () => {
    for (const raw of [
      'bad json',
      'null',
      JSON.stringify({ ...receipt, body: null }),
      JSON.stringify({ ...receipt, body: { ...receipt.body, parameters: [] } }),
      JSON.stringify({ ...receipt, version: 2 }),
    ])
      expect(readCaptureReceipt(raw, 'workspace-a')).toBeNull();
    expect(readCaptureReceipt(JSON.stringify(receipt), 'workspace-b')).toBeNull();
  });
  it('binds account result to explicit target even when another account was selected at collection time', () => {
    expect(
      captureMatchesContext({ ...receipt, contextAccountSeq: '202' }, '9007199254740993'),
    ).toBe(true);
    expect(captureMatchesContext(receipt, '202')).toBe(false);
    expect(captureMatchesContext(receipt, null)).toBe(false);
  });
  it('binds market handoff to its original optional account context', () => {
    const market: CaptureReceipt = {
      ...receipt,
      body: {
        kind: 'market-capture',
        parameters: { endpoint: 'candles', query: { symbol: 'ALPHA' }, pages: 1 },
        request_key: 'market-key',
      },
    };
    expect(captureMatchesContext(market, '202')).toBe(false);
    expect(captureMatchesContext(market, '9007199254740993')).toBe(true);
    expect(captureMatchesContext({ ...market, contextAccountSeq: null }, null)).toBe(true);
    expect(captureMatchesContext({ ...market, contextAccountSeq: null }, '202')).toBe(false);
  });
});
