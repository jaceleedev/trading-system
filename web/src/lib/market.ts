import type {
  CandlestickData,
  HistogramData,
  SeriesMarker,
  Time,
  UTCTimestamp,
} from 'lightweight-charts';
import { getMarketView, listMarketCaptures } from './api/sdk.gen';
import type {
  ErrorResponse,
  MarketCatalog,
  MarketEvidenceEvent,
  MarketSeries,
} from './api/types.gen';

function options(signal?: AbortSignal) {
  return {
    baseUrl: window.location.origin,
    cache: 'no-store' as const,
    credentials: 'same-origin' as const,
    signal,
  };
}

function unwrap<T>(result: { data?: T; error?: ErrorResponse }): T {
  if (result.error || result.data === undefined) {
    const code = result.error?.error.code;
    throw new Error(
      code === 'invalid_request'
        ? '선택한 시장 자료와 조회 시각을 확인해 주세요.'
        : code === 'invalid_record'
          ? '저장된 시장 자료를 검증하지 못했습니다. 로컬 자료를 확인해 주세요.'
          : '시장 자료를 읽지 못했습니다. 연결과 로컬 자료를 확인한 뒤 다시 읽어 주세요.',
    );
  }
  return result.data;
}

export async function fetchMarketCatalog(signal?: AbortSignal) {
  return unwrap(await listMarketCaptures(options(signal)));
}

export async function fetchMarketView(captureIds: string[], asOf?: string, signal?: AbortSignal) {
  return unwrap(
    await getMarketView({
      ...options(signal),
      body: { capture_ids: captureIds, as_of: asOf, max_points: 1000 },
    }),
  );
}

export type CaptureGroup = {
  key: string;
  subject: string;
  symbol: string;
  currency: string;
  interval: '1m' | '1d';
  adjusted: boolean;
  captureIds: string[];
};

export function captureGroups(catalog: MarketCatalog): CaptureGroup[] {
  const groups = new Map<string, CaptureGroup>();
  for (const item of catalog.items) {
    if (
      item.status !== 'supported' ||
      !item.symbol ||
      (item.interval !== '1m' && item.interval !== '1d') ||
      item.adjusted === null
    )
      continue;
    for (const currency of item.currencies) {
      const subject = JSON.stringify([item.symbol, currency]);
      const key = JSON.stringify([item.symbol, currency, item.interval, item.adjusted]);
      let group = groups.get(key);
      if (!group) {
        group = {
          key,
          subject,
          symbol: item.symbol,
          currency,
          interval: item.interval,
          adjusted: item.adjusted,
          captureIds: [],
        };
        groups.set(key, group);
      }
      if (!group.captureIds.includes(item.capture_id)) group.captureIds.push(item.capture_id);
    }
  }
  return [...groups.values()]
    .map((group) => ({ ...group, captureIds: group.captureIds.sort() }))
    .sort((a, b) => a.key.localeCompare(b.key));
}

export const eventLabels = {
  price: '가격',
  earnings: '실적',
  filing: '공시',
  news: '뉴스',
  macro: '거시경제',
  other: '기타',
};

export function seriesBars(series: MarketSeries): ChartBar[] {
  return series.points.map((point) => ({
    ...point,
    time: series.interval === '1m' ? intradayTime(point.source_timestamp) : point.session_date!,
  }));
}

/** A daily trading session cannot be inferred from a local-midnight candle timestamp. */
export function eventMarkers(
  series: MarketSeries,
  events: MarketEvidenceEvent[],
): SeriesMarker<Time>[] {
  if (series.interval !== '1m') return [];
  return events
    .flatMap((event): SeriesMarker<Time>[] => {
      if (event.symbol !== series.symbol || !event.occurred_at) return [];
      const instant = Date.parse(event.occurred_at);
      const point = series.points.find(
        (point) =>
          point.period_start &&
          point.period_end &&
          Date.parse(point.period_start) <= instant &&
          instant < Date.parse(point.period_end),
      );
      return point
        ? [
            {
              time: intradayTime(point.source_timestamp),
              id: event.record_id,
              position: 'aboveBar',
              shape: 'circle',
              color: '#2563eb',
              text: eventLabels[event.event_kind],
            },
          ]
        : [];
    })
    .sort((a, b) => Number(a.time) - Number(b.time));
}

/** Exact values remain strings everywhere except the chart renderer. */
export type ChartBar = {
  time: Time;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string;
};

export function chartNumber(value: string): number {
  if (!/^\d+(?:\.\d+)?$/.test(value)) throw new Error('차트 값의 형식이 올바르지 않습니다.');
  const number = Number(value);
  if (!Number.isFinite(number) || (number === 0 && /[1-9]/.test(value))) {
    throw new Error(
      '차트에 표시할 수 있는 숫자 범위를 벗어났습니다. 정확한 값은 표에서 확인하세요.',
    );
  }
  return number;
}

export function intradayTime(instant: string): UTCTimestamp {
  if (!/T.*(?:Z|[+-]\d{2}:\d{2})$/.test(instant)) {
    throw new Error('분봉의 시간대가 명확하지 않습니다.');
  }
  const milliseconds = Date.parse(instant);
  if (!Number.isFinite(milliseconds) || milliseconds % 1000 !== 0) {
    throw new Error('분봉의 시각을 차트에 표시할 수 없습니다.');
  }
  return (milliseconds / 1000) as UTCTimestamp;
}

export function chartSeries(bars: ChartBar[]): {
  candles: CandlestickData<Time>[];
  volumes: HistogramData<Time>[];
} {
  return {
    candles: bars.map((bar) => ({
      time: bar.time,
      open: chartNumber(bar.open),
      high: chartNumber(bar.high),
      low: chartNumber(bar.low),
      close: chartNumber(bar.close),
    })),
    volumes: bars.map((bar) => ({
      time: bar.time,
      value: chartNumber(bar.volume),
      color: chartNumber(bar.close) >= chartNumber(bar.open) ? '#bfd1f9' : '#ead0d3',
    })),
  };
}

export function localCutoff(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) throw new Error('조회 기준 시각을 확인해 주세요.');
  return date.toISOString();
}
