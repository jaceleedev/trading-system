<script lang="ts">
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { CandlestickChart, RotateCw } from '@lucide/svelte';
  import {
    CandlestickSeries,
    ColorType,
    HistogramSeries,
    createChart,
    createSeriesMarkers,
    type IChartApi,
    type MouseEventParams,
    type Time,
  } from 'lightweight-charts';
  import { Button } from '$lib/components/ui/button';
  import type { MarketEvidenceEvent, MarketSeries } from '$lib/api/types.gen';
  import {
    captureGroups,
    chartSeries,
    eventLabels,
    eventMarkers,
    fetchMarketCatalog,
    fetchMarketView,
    localCutoff,
    seriesBars,
  } from '$lib/market';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import { modeLabels } from '$lib/research';

  let { ready, onselect }: { ready: boolean; onselect: (id: string) => void } = $props();
  let subject = $state('');
  let interval = $state<'1m' | '1d'>('1d');
  let adjusted = $state('false');
  let cutoffDraft = $state('');
  let cutoff = $state<string | undefined>();
  let formError = $state('');
  let chartError = $state('');
  const client = useQueryClient();
  const catalogQuery = createQuery(() => ({
    queryKey: ['market-catalog'],
    enabled: ready,
    queryFn: ({ signal }) => fetchMarketCatalog(signal),
  }));
  let catalog = $derived(
    ready && catalogQuery.isSuccess && !catalogQuery.isFetching ? catalogQuery.data : undefined,
  );
  let groups = $derived(catalog ? captureGroups(catalog) : []);
  let subjects = $derived([...new Map(groups.map((group) => [group.subject, group])).values()]);
  let group = $derived(
    groups.find(
      (item) =>
        item.subject === subject &&
        item.interval === interval &&
        String(item.adjusted) === adjusted,
    ),
  );
  const viewQuery = createQuery(() => {
    const ids = group?.captureIds ?? [];
    const asOf = cutoff;
    return {
      queryKey: ['market-view', ids, asOf],
      enabled: ready && !!catalog && ids.length > 0,
      queryFn: ({ signal }) => fetchMarketView(ids, asOf, signal),
    };
  });
  let view = $derived(
    ready && catalog && group && viewQuery.isSuccess && !viewQuery.isFetching
      ? viewQuery.data
      : undefined,
  );
  let series = $derived(
    view?.series.find(
      (item) =>
        item.symbol === group?.symbol &&
        item.currency === group?.currency &&
        item.interval === group?.interval &&
        item.adjusted === group?.adjusted,
    ),
  );
  let events = $derived(view?.events.filter((item) => item.symbol === group?.symbol) ?? []);
  let reading = $derived(catalogQuery.isFetching || viewQuery.isFetching);

  function selectSubject(value: string) {
    subject = value;
    const available = groups.filter((item) => item.subject === value);
    const next =
      available.find((item) => item.interval === '1d' && !item.adjusted) ??
      available.find((item) => !item.adjusted) ??
      available[0];
    interval = next?.interval ?? '1d';
    adjusted = String(next?.adjusted ?? false);
    chartError = '';
  }
  function selectInterval(value: '1m' | '1d') {
    interval = value;
    const available = groups.filter((item) => item.subject === subject && item.interval === value);
    if (!available.some((item) => String(item.adjusted) === adjusted))
      adjusted = String(available[0]?.adjusted ?? false);
    chartError = '';
  }
  function applyCutoff(event: SubmitEvent) {
    event.preventDefault();
    try {
      cutoff = localCutoff(cutoffDraft);
      formError = '';
      chartError = '';
    } catch (error) {
      formError = (error as Error).message;
    }
  }
  async function refresh() {
    chartError = '';
    await client.cancelQueries({ queryKey: ['market-view'] });
    await client.resetQueries({ queryKey: ['market-catalog'] });
    await client.resetQueries({ queryKey: ['market-view'] });
  }

  function renderChart(
    node: HTMLDivElement,
    input: { series: MarketSeries; events: MarketEvidenceEvent[] },
  ) {
    let chart: IChartApi | undefined;
    let click: ((event: MouseEventParams<Time>) => void) | undefined;
    try {
      const data = chartSeries(seriesBars(input.series));
      chart = createChart(node, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: '#ffffff' },
          textColor: '#65748b',
          attributionLogo: true,
        },
        grid: { vertLines: { color: '#f1f5f9' }, horzLines: { color: '#edf1f7' } },
        rightPriceScale: { borderColor: '#e2e8f0' },
        timeScale: {
          borderColor: '#e2e8f0',
          timeVisible: input.series.interval === '1m',
          secondsVisible: false,
        },
        localization: { locale: 'ko-KR' },
      });
      const candles = chart.addSeries(CandlestickSeries, {
        upColor: '#2563eb',
        downColor: '#bf5665',
        borderVisible: false,
        wickUpColor: '#2563eb',
        wickDownColor: '#bf5665',
      });
      candles.setData(data.candles);
      const volume = chart.addSeries(
        HistogramSeries,
        { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false },
        1,
      );
      volume.setData(data.volumes);
      chart.panes()[1].setHeight(90);
      const markers = eventMarkers(input.series, input.events);
      createSeriesMarkers(candles, markers);
      click = (event) => {
        if (
          typeof event.hoveredObjectId === 'string' &&
          markers.some((marker) => marker.id === event.hoveredObjectId)
        )
          onselect(event.hoveredObjectId);
      };
      chart.subscribeClick(click);
      chart.timeScale().fitContent();
    } catch {
      chartError = '차트를 표시하지 못했습니다. 아래 표에서 관측한 정확한 값을 확인할 수 있습니다.';
    }
    return {
      destroy() {
        if (chart && click) chart.unsubscribeClick(click);
        chart?.remove();
      },
    };
  }
</script>

<section class="panel market-panel" aria-label="시장 관측" aria-busy={reading}>
  <div class="market-heading">
    <div>
      <h2>시장 관측</h2>
      <p class="muted">저장된 가격과 사건의 근거를 함께 읽습니다.</p>
    </div>
    <Button variant="outline" class="reload-button" onclick={refresh} disabled={!ready || reading}
      ><RotateCw size={15} aria-hidden="true" />시장 자료 다시 읽기</Button
    >
  </div>
  {#if !ready}<p class="empty-state muted">작업실 연결을 확인하고 있습니다.</p>
  {:else if catalogQuery.isFetching}<p class="empty-state" role="status">
      저장된 시장 자료 목록을 읽고 있습니다.
    </p>
  {:else if catalogQuery.isError}<p class="empty-state error-state" role="alert">
      {catalogQuery.error.message}
    </p>
  {:else if catalog}
    <p class="muted market-catalog-count">
      {[
        `저장 ${catalog.total_count}개`,
        `사용 가능 ${catalog.supported_count}개`,
        catalog.unsupported_count ? `미지원 ${catalog.unsupported_count}개` : '',
        catalog.invalid_count ? `검증 실패 ${catalog.invalid_count}개` : '',
      ]
        .filter(Boolean)
        .join(' · ')}
    </p>
    {#if catalog.truncated_count}<p class="market-notice">
        목록 조회 한도로 {catalog.truncated_count}개가 생략되었습니다. 아래 선택에는 현재 목록에
        있는 자료만 포함됩니다.
      </p>{/if}
    {#if !groups.length}
      <div class="empty-state market-empty">
        <CandlestickChart size={27} aria-hidden="true" /><strong
          >표시할 시장 관측이 없습니다.</strong
        >
        <p>저장된 지원 형식의 분봉·일봉을 여기서 읽을 수 있습니다.</p>
      </div>
    {:else}
      <div class="market-controls">
        <label
          >시장 종목<select
            value={subject}
            onchange={(event) => selectSubject(event.currentTarget.value)}
            ><option value="">시장 종목 선택</option>{#each subjects as item}<option
                value={item.subject}>{item.symbol} · {item.currency}</option
              >{/each}</select
          ></label
        >
        <label
          >봉 단위<select
            value={interval}
            onchange={(event) => selectInterval(event.currentTarget.value as '1m' | '1d')}
            disabled={!subject}
            >{#each ['1m', '1d'] as value}<option
                {value}
                disabled={!groups.some(
                  (item) => item.subject === subject && item.interval === value,
                )}>{value === '1m' ? '1분봉' : '일봉'}</option
              >{/each}</select
          ></label
        >
        <label
          >가격 기준<select
            value={adjusted}
            onchange={(event) => {
              adjusted = event.currentTarget.value;
              chartError = '';
            }}
            disabled={!subject}
            >{#each [false, true] as value}<option
                value={String(value)}
                disabled={!groups.some(
                  (item) =>
                    item.subject === subject &&
                    item.interval === interval &&
                    item.adjusted === value,
                )}>{value ? '수정주가' : '수정 전 가격'}</option
              >{/each}</select
          ></label
        >
        <form class="market-cutoff" onsubmit={applyCutoff}>
          <label
            >조회 기준 시각 · 기기 현지 시각<input
              type="datetime-local"
              bind:value={cutoffDraft}
            /></label
          ><Button variant="outline" type="submit">시각 적용</Button>
        </form>
      </div>
      {#if formError}<p role="alert" class="error-state">{formError}</p>{/if}
      {#if !subject}<p class="empty-state muted">종목을 선택하면 저장된 관측을 표시합니다.</p>
      {:else if !group}<p class="empty-state muted">이 조합으로 저장된 관측이 없습니다.</p>
      {:else if viewQuery.isFetching}<p class="empty-state" role="status">
          선택한 시장 관측을 검증하고 있습니다.
        </p>
      {:else if viewQuery.isError}<p class="empty-state error-state" role="alert">
          {viewQuery.error.message}
        </p>
      {:else if view}
        <div class="market-scope">
          <span
            >조회 기준 <time datetime={view.as_of} title={view.as_of}>{formatTime(view.as_of)}</time
            ></span
          ><span
            >읽은 시각 <time datetime={view.generated_at} title={view.generated_at}
              >{formatTime(view.generated_at)}</time
            ></span
          ><span
            >선택 원자료 {group.captureIds.length}개 · 기준 이후 제외 {view
              .excluded_future_capture_ids.length}개</span
          >
        </div>
        {#if view.truncated_point_count}<p class="market-notice">
            조회 한도로 봉 {view.truncated_point_count}개가 생략되었습니다.
          </p>{/if}
        {#if !series?.points.length}<p class="empty-state muted">
            이 기준 시각까지 수집한 표시 가능한 봉이 없습니다.
          </p>
        {:else}
          <div class="market-series-heading">
            <h3>
              {series.symbol} · {series.currency} · {series.interval === '1m' ? '1분봉' : '일봉'}
            </h3>
            <span class="muted"
              >{series.points.length}개 봉 · {series.adjusted ? '수정주가' : '수정 전 가격'}</span
            >
          </div>
          <p class="muted">
            {series.interval === '1m'
              ? '가로축 UTC · 봉 종료 시각 기준'
              : '가로축 공급자가 기록한 현지 거래일'} · 봉 확정 여부 미확인
          </p>
          {#key `${view.id}-${series.id}`}<div
              class="market-chart"
              role="img"
              aria-label={`${series.symbol} ${series.interval === '1m' ? '1분봉' : '일봉'} OHLCV 관측 차트`}
              use:renderChart={{ series, events }}
            ></div>{/key}
          {#if chartError}<p class="error-state" role="alert">{chartError}</p>{/if}
          <p class="muted chart-attribution">
            차트 숫자는 표시용 근삿값입니다. 정확한 값은 아래 표에 보존됩니다. <a
              href="https://www.tradingview.com/"
              target="_blank"
              rel="noopener noreferrer">TradingView Lightweight Charts™</a
            > · Copyright © 2025 TradingView, Inc.
          </p>
          <details class="subtle-details market-values" open>
            <summary>관측한 정확한 값 · {series.points.length}개</summary>
            <!-- svelte-ignore a11y_no_noninteractive_tabindex (The bounded table scroll area must be reachable for keyboard scrolling.) -->
            <div class="table-scroll" tabindex="0" role="region" aria-label="정확한 OHLCV 값">
              <table>
                <thead
                  ><tr
                    ><th>봉 기준</th><th>시가</th><th>고가</th><th>저가</th><th>종가</th><th
                      >거래량</th
                    ><th>관측·변경</th></tr
                  ></thead
                ><tbody
                  >{#each [...series.points].reverse() as point}<tr
                      ><td title={point.source_timestamp}
                        >{series.interval === '1d'
                          ? point.session_date
                          : formatTime(point.source_timestamp)}</td
                      ><td class="numeric">{formatDecimal(point.open)}</td><td class="numeric"
                        >{formatDecimal(point.high)}</td
                      ><td class="numeric">{formatDecimal(point.low)}</td><td class="numeric"
                        >{formatDecimal(point.close)}</td
                      ><td class="numeric">{formatDecimal(point.volume)}</td><td
                        ><details>
                          <summary>값 변경 {point.revision_count}회</summary>
                          <p title={point.observed_at}>
                            이 값 첫 확인 {formatTime(point.observed_at)}
                          </p>
                          <p title={point.last_observed_at}>
                            최근 확인 {formatTime(point.last_observed_at)}
                          </p>
                          <p class="identifier">{shortId(point.id)}</p>
                          <p class="muted">
                            원자료 {point.capture_ids.length}개 · 확정 여부 미확인
                          </p>
                          {#each point.revisions as revision}<p
                              class="numeric"
                              title={revision.observed_at}
                            >
                              종가 {formatDecimal(revision.close)} · {formatTime(
                                revision.observed_at,
                              )}
                            </p>{/each}
                        </details></td
                      ></tr
                    >{/each}</tbody
                >
              </table>
            </div>
          </details>
        {/if}
        <section class="market-events" aria-label="연결된 시장 사건">
          <h3>연결된 사건과 근거 <span class="muted">{events.length}개</span></h3>
          <p class="muted">
            기록에 적힌 종목·시장 연결입니다. 사건 내용은 해당 근거에서 확인하세요. {interval ===
            '1d'
              ? '일봉의 세션 범위가 미확인이므로 사건은 목록으로 표시합니다.'
              : '발생 시각이 확인된 사건만 해당 분봉에 표시합니다.'}
          </p>
          {#if view.omitted_event_count}<p class="market-notice">
              사건 조회 한도로 {view.omitted_event_count}개가 생략되었습니다.
            </p>{/if}{#if !events.length}<p class="muted market-events-empty">
              이 조회 범위에 연결된 사건 기록이 없습니다.
            </p>{:else}<ul class="market-event-list">
              {#each events as event}<li>
                  <button class="market-event-button" onclick={() => onselect(event.record_id)}
                    >{event.claim}<span class="muted"
                      >{eventLabels[event.event_kind]} · {event.symbol} / {event.market} · {modeLabels[
                        event.mode
                      ]}</span
                    ></button
                  >
                  <dl class="market-event-times">
                    <div>
                      <dt>발생</dt>
                      <dd title={event.occurred_at ?? ''}>{formatTime(event.occurred_at)}</dd>
                    </div>
                    <div>
                      <dt>발표</dt>
                      <dd title={event.source_published_at ?? ''}>
                        {formatTime(event.source_published_at)}
                      </dd>
                    </div>
                    <div>
                      <dt>수집</dt>
                      <dd title={event.retrieved_at}>{formatTime(event.retrieved_at)}</dd>
                    </div>
                    <div>
                      <dt>기록</dt>
                      <dd title={event.recorded_at}>{formatTime(event.recorded_at)}</dd>
                    </div>
                  </dl>
                </li>{/each}
            </ul>{/if}
        </section>
        <details class="subtle-details market-provenance">
          <summary>자료 해석과 조회 범위</summary>
          <p>
            수집 시각 이전의 공개·이용 가능 여부와 봉 확정 시점은 증명되지 않습니다. 수정주가는
            총수익 자료를 뜻하지 않습니다.
          </p>
          <p class="identifier">관측 뷰 {view.id}</p>
          <p class="identifier">응답 계약 {view.response_contract_sha256}</p>
          <p>봉 {view.total_point_count}개 중 {view.truncated_point_count}개 생략</p>
          {#each view.warnings as warning}<p class="muted">{warning}</p>{/each}
        </details>
      {/if}
    {/if}
  {/if}
</section>
