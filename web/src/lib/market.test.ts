import { describe, expect, it } from 'vitest';
import {
  captureGroups,
  chartNumber,
  chartSeries,
  eventMarkers,
  intradayTime,
  localCutoff,
} from './market';
import type {
  MarketCaptureSummary,
  MarketCatalog,
  MarketEvidenceEvent,
  MarketSeries,
} from './api/types.gen';

describe('chart rendering boundary', () => {
  it('keeps decimal input strings intact while converting only rendering values', () => {
    const input = {
      time: '2026-09-10',
      open: '1.0000000000000000000001',
      high: '1.0000000000000000000003',
      low: '0.0000000000000000000001',
      close: '1.0000000000000000000002',
      volume: '9007199254740993',
    };
    const original = structuredClone(input);
    const rendered = chartSeries([input]);
    expect(rendered.candles[0].close).toBe(1);
    expect(input).toEqual(original);
    expect(input.volume).toBe('9007199254740993');
  });

  it('rejects invalid, nonfinite and underflowed chart values instead of showing a false zero', () => {
    for (const value of [
      '',
      'NaN',
      'Infinity',
      '-1',
      '1e3',
      '1'.repeat(400),
      `0.${'0'.repeat(400)}1`,
    ]) {
      expect(() => chartNumber(value)).toThrow();
    }
    expect(chartNumber('0.00000')).toBe(0);
    expect(chartNumber('0.12500')).toBe(0.125);
  });

  it('uses UTC seconds without moving source instants to simulate a timezone', () => {
    expect(intradayTime('2026-09-10T09:01:00+09:00')).toBe(
      Date.parse('2026-09-10T00:01:00Z') / 1000,
    );
    expect(() => intradayTime('2026-09-10T09:01:00')).toThrow();
    expect(() => intradayTime('2026-09-10T09:01:00.123Z')).toThrow();
  });

  it('leaves daily trade dates as dates rather than changing them to a UTC date', () => {
    const bar = { time: '2026-09-10', open: '1', high: '2', low: '1', close: '2', volume: '3' };
    expect(chartSeries([bar]).candles[0].time).toBe('2026-09-10');
    expect(localCutoff('')).toBeUndefined();
    expect(() => localCutoff('not-a-date')).toThrow();
  });
});

it('does not combine currencies, time intervals, price bases or unsupported captures', () => {
  const entry: MarketCaptureSummary = {
    capture_id: 'a'.repeat(64),
    endpoint: '/api/v1/candles',
    symbol: 'ALPHA',
    interval: '1d',
    adjusted: false,
    currencies: ['KRW'],
    retrieved_at: '2026-09-10T09:00:00Z',
    candle_count: 1,
    status: 'supported',
    reason: null,
    response_contract_sha256: 'f'.repeat(64),
  };
  const catalog: MarketCatalog = {
    items: [
      entry,
      { ...entry, capture_id: 'b'.repeat(64) },
      { ...entry, capture_id: 'c'.repeat(64), currencies: ['USD'] },
      { ...entry, capture_id: 'd'.repeat(64), adjusted: true },
      { ...entry, capture_id: 'e'.repeat(64), interval: '1m' },
      { ...entry, capture_id: 'f'.repeat(64), status: 'unsupported' },
    ],
    total_count: 6,
    supported_count: 5,
    unsupported_count: 1,
    invalid_count: 0,
    truncated_count: 0,
  };
  const groups = captureGroups(catalog);
  expect(groups).toHaveLength(4);
  expect(
    groups.find((item) => item.currency === 'KRW' && item.interval === '1d' && !item.adjusted)
      ?.captureIds,
  ).toEqual(['a'.repeat(64), 'b'.repeat(64)]);
  expect(groups.flatMap((item) => item.captureIds)).not.toContain('f'.repeat(64));
});

it('places events in half-open minute intervals and never invents a daily trading session', () => {
  const value = {
    id: 'a'.repeat(64),
    open: '1',
    high: '2',
    low: '1',
    close: '2',
    volume: '1',
    observed_at: '2026-09-10T09:00:00Z',
    last_observed_at: '2026-09-10T09:00:00Z',
    capture_ids: ['b'.repeat(64)],
  };
  const series: MarketSeries = {
    id: 'c'.repeat(64),
    provider: 'toss',
    symbol: 'ALPHA',
    currency: 'KRW',
    interval: '1m',
    adjusted: false,
    points: [
      {
        ...value,
        source_timestamp: '2026-09-10T09:01:00Z',
        period_start: '2026-09-10T09:00:00Z',
        period_end: '2026-09-10T09:01:00Z',
        session_date: null,
        finality: 'unknown',
        revision_count: 0,
        revisions: [value],
      },
    ],
  };
  const event: MarketEvidenceEvent = {
    record_id: 'd'.repeat(64),
    symbol: 'ALPHA',
    market: 'KR',
    event_kind: 'news',
    occurred_at: '2026-09-10T09:00:00Z',
    source_published_at: null,
    retrieved_at: '2026-09-10T09:01:00Z',
    recorded_at: '2026-09-10T09:02:00Z',
    claim: 'Synthetic event',
    mode: 'synthetic',
    source_locator: 'https://example.com/synthetic',
    verification: 'unverified',
  };
  expect(eventMarkers(series, [event])).toMatchObject([
    { id: event.record_id, time: intradayTime(series.points[0].source_timestamp) },
  ]);
  expect(eventMarkers(series, [{ ...event, occurred_at: series.points[0].period_end }])).toEqual(
    [],
  );
  expect(
    eventMarkers(series, [
      { ...event, occurred_at: null },
      { ...event, symbol: 'BETA' },
    ]),
  ).toEqual([]);
  expect(eventMarkers({ ...series, interval: '1d' }, [event])).toEqual([]);
});
