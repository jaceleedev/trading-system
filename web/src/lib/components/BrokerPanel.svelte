<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { tick } from 'svelte';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    BrokerCoverage,
    InvestmentContext,
    ReconciliationRequest,
    ReconciliationResponse,
  } from '$lib/api/types.gen';
  import { fetchSnapshots } from '$lib/queries';
  import { fetchJobs } from '$lib/jobs';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import {
    brokerScanParameters,
    enqueueBrokerScan,
    fetchBrokerScans,
    fetchBrokerScan,
    fetchReconciliations,
    fetchReconciliation,
    calculateReconciliation,
    storeReconciliation,
    definiteBrokerError,
    brokerOrderStatus,
    reconciliationLabels,
    brokerStopReason,
    type BrokerSubmission,
  } from '$lib/broker';
  let {
    ready = false,
    jobsEnabled = false,
    synthetic = false,
    selectedSnapshot = '',
    context,
  }: {
    ready?: boolean;
    jobsEnabled?: boolean;
    synthetic?: boolean;
    selectedSnapshot?: string;
    context?: InvestmentContext;
  } = $props();
  let opened = $state(false);
  let scanForm = $state({
    fromDate: '',
    toDate: '',
    symbol: '',
    maxPages: '10',
    pageSize: '100',
    detailOrderIds: '',
  });
  let selectedScanId = $state('');
  let beforeSnapshot = $state('');
  let afterSnapshot = $state('');
  let beforeScan = $state('');
  let afterScan = $state('');
  let comparisonMode = $state<'prospective' | 'retrospective'>('prospective');
  let selectedReportId = $state('');
  let preview = $state<{ key: string; response: ReconciliationResponse } | null>(null);
  let calculating = $state(false);
  let busy = $state(false);
  let calculationVersion = 0;
  let pendingScan = $state<BrokerSubmission | null>(null);
  let pendingSave = $state<ReconciliationRequest | null>(null);
  let saveTarget = $state<{ key: string; reportId: string } | null>(null);
  let actionError = $state('');
  let feedback = $state('');
  let account = $derived(
    ready && context?.account?.id === selectedSnapshot ? context.account : undefined,
  );
  let accountSeq = $derived(account?.snapshot.account_seq);
  const snapshotsQuery = createQuery(() => ({
    queryKey: ['snapshots'],
    enabled: ready && opened,
    queryFn: ({ signal }) => fetchSnapshots(signal),
  }));
  const scansQuery = createQuery(() => {
    const seq = accountSeq;
    return {
      queryKey: ['broker-scans', seq],
      enabled: ready && opened,
      queryFn: ({ signal }) => fetchBrokerScans(seq, signal),
    };
  });
  const scanQuery = createQuery(() => {
    const id = selectedScanId;
    return {
      queryKey: ['broker-scan', id],
      enabled: ready && opened && !!id,
      queryFn: ({ signal }) => fetchBrokerScan(id, signal),
    };
  });
  const reportsQuery = createQuery(() => ({
    queryKey: ['reconciliations'],
    enabled: ready && opened,
    queryFn: ({ signal }) => fetchReconciliations(signal),
  }));
  const reportQuery = createQuery(() => {
    const id = selectedReportId;
    return {
      queryKey: ['reconciliation', id],
      enabled: ready && opened && !!id,
      queryFn: ({ signal }) => fetchReconciliation(id, signal),
    };
  });
  const jobsQuery = createQuery(() => ({
    queryKey: ['jobs'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchJobs(signal),
  }));
  let snapshots = $derived(
    ready && snapshotsQuery.isSuccess && !snapshotsQuery.isFetching
      ? snapshotsQuery.data.items.filter((item) => !accountSeq || item.account_seq === accountSeq)
      : undefined,
  );
  let scans = $derived(
    ready && scansQuery.isSuccess && !scansQuery.isFetching ? scansQuery.data : undefined,
  );
  let scan = $derived(
    scans?.items.some((item) => item.id === selectedScanId) &&
      scanQuery.isSuccess &&
      !scanQuery.isFetching &&
      scanQuery.data.id === selectedScanId
      ? scanQuery.data
      : undefined,
  );
  let reports = $derived(
    ready && reportsQuery.isSuccess && !reportsQuery.isFetching ? reportsQuery.data : undefined,
  );
  let saved = $derived(
    reports &&
      reportQuery.isSuccess &&
      !reportQuery.isFetching &&
      reportQuery.data.id === selectedReportId
      ? reportQuery.data
      : undefined,
  );
  let mode = $derived(synthetic ? ('synthetic' as const) : comparisonMode);
  let formKey = $derived(
    JSON.stringify({
      selectedSnapshot,
      beforeSnapshot,
      afterSnapshot,
      beforeScan,
      afterScan,
      mode,
    }),
  );
  let visiblePreview = $derived(
    ready && preview?.key === formKey && !calculating && !actionError && snapshots && scans
      ? preview.response
      : undefined,
  );
  let collectionJobs = $derived(
    ready && jobsEnabled && jobsQuery.isSuccess && !jobsQuery.isFetching
      ? jobsQuery.data.items.filter(
          (item) =>
            item.kind === 'broker-sync' &&
            (!accountSeq || item.parameters.account_seq === accountSeq),
        )
      : undefined,
  );
  const jobStates = {
    queued: '수집 대기',
    running: '수집 실행 중',
    succeeded: '작업 완료',
    failed: '실패',
    cancelled: '취소',
  };
  function request(): ReconciliationRequest {
    if (
      !snapshots?.some((item) => item.id === beforeSnapshot) ||
      !snapshots.some((item) => item.id === afterSnapshot)
    )
      throw new Error('이전·이후 계좌 관측을 명시적으로 선택해 주세요.');
    if (
      !scans?.items.some((item) => item.id === afterScan) ||
      (beforeScan && !scans.items.some((item) => item.id === beforeScan))
    )
      throw new Error('대조할 저장 스캔을 선택해 주세요.');
    const before = snapshots.find((item) => item.id === beforeSnapshot)!;
    const after = snapshots.find((item) => item.id === afterSnapshot)!;
    if (before.account_seq !== after.account_seq)
      throw new Error('이전·이후 관측은 같은 계좌여야 합니다.');
    return {
      before_snapshot_id: beforeSnapshot,
      after_snapshot_id: afterSnapshot,
      before_scan_id: beforeScan || null,
      after_scan_id: afterScan,
      mode,
      as_of: null,
    };
  }
  async function calculate() {
    const version = ++calculationVersion;
    const key = formKey;
    preview = null;
    actionError = '';
    feedback = '';
    try {
      const body = request();
      calculating = true;
      const response = await calculateReconciliation(body);
      if (version === calculationVersion && key === formKey) preview = { key, response };
    } catch (error) {
      if (version === calculationVersion && key === formKey)
        actionError = error instanceof Error ? error.message : '대조 결과를 계산하지 못했습니다.';
    } finally {
      if (version === calculationVersion) calculating = false;
    }
  }
  async function save() {
    if (!ready || !jobsEnabled || busy || pendingScan) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingSave) {
        if (!visiblePreview) throw new Error('현재 입력으로 먼저 대조 계산을 확인해 주세요.');
        pendingSave = structuredClone(request());
        saveTarget = { key: formKey, reportId: selectedReportId };
      }
      busy = true;
      const result = await storeReconciliation(pendingSave);
      pendingSave = null;
      preview = null;
      if (saveTarget?.key === formKey && saveTarget.reportId === selectedReportId)
        selectedReportId = result.id;
      saveTarget = null;
      feedback = `계좌 ${result.record.account_seq} 대조 결과를 저장했습니다. 누적 관측 비교이며 개별 체결이나 손익 실적이 아닙니다.`;
      await reportsQuery.refetch();
      await tick();
      if (selectedReportId) await reportQuery.refetch();
    } catch (error) {
      if (definiteBrokerError(error)) pendingSave = null;
      actionError =
        error instanceof Error ? error.message : '대조 저장 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  async function enqueue(event?: SubmitEvent) {
    event?.preventDefault();
    if (!ready || !jobsEnabled || synthetic || busy || pendingSave) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingScan) {
        if (!account) throw new Error('수집 대상 계좌 관측을 선택하고 조회 완료를 기다려 주세요.');
        pendingScan = {
          kind: 'broker-sync',
          parameters: brokerScanParameters(account.snapshot.account_seq, scanForm),
          request_key: crypto.randomUUID(),
          max_attempts: 3,
        };
      }
      busy = true;
      await enqueueBrokerScan(pendingScan);
      pendingScan = null;
      feedback =
        '브로커 읽기 스캔을 접수했습니다. 네트워크 수집이 허용된 워커의 실행과 저장 결과를 확인해 주세요.';
      await jobsQuery.refetch();
    } catch (error) {
      if (definiteBrokerError(error)) pendingScan = null;
      actionError =
        error instanceof Error ? error.message : '수집 접수 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  async function refresh() {
    actionError = '';
    preview = null;
    ++calculationVersion;
    calculating = false;
    await Promise.all([
      scansQuery.refetch(),
      reportsQuery.refetch(),
      snapshotsQuery.refetch(),
      ...(jobsEnabled ? [jobsQuery.refetch()] : []),
    ]);
    await Promise.all([
      ...(selectedScanId ? [scanQuery.refetch()] : []),
      ...(selectedReportId ? [reportQuery.refetch()] : []),
    ]);
  }
</script>

{#snippet coverage(value: BrokerCoverage)}
  <p class:warning-state={!value.complete} class:muted={value.complete}>
    요청 범위 내 페이지 조회: {value.complete ? '완료' : '미완료'} · OPEN {value.open_complete
      ? '완료'
      : '미완료'} · CLOSED {value.closed_complete ? '완료' : '미완료'} ({value.closed_pages}페이지)
    · 개별 조회 {value.details_complete ? '완료' : '미완료'}
  </p>
  <p class="muted">
    주문 생성일 KST: {value.ordered_at_from ?? '시작 미지정'} ~ {value.ordered_at_to ??
      '종료 미지정'} · 최종 체결일 범위가 아닙니다.
  </p>
  {#if value.stop_reason}<p class="warning-state">
      조회 중단: {brokerStopReason[value.stop_reason]}
    </p>{/if}
  {#if value.unresolved_detail_ids.length}<p class="warning-state identifier">
      개별 조회 미해결: {value.unresolved_detail_ids.join(', ')}
    </p>{/if}
{/snippet}
{#snippet comparison(value: ReconciliationResponse, title: string)}
  {@const record = value.record}
  <section class="capital-result broker-result" aria-label={title}>
    <h3>{title} · 계좌 {record.account_seq}</h3>
    <p class="muted">
      {record.mode === 'synthetic'
        ? '합성 대조'
        : record.mode === 'retrospective'
          ? '사후 대조'
          : '관측 대조'} · 자료 기준 {formatTime(record.as_of)} · {shortId(value.id)}
    </p>
    <p class="warning-state">
      주문 출처와 원주문·정정 연결은 미확인입니다. 누적 관측의 차이를 신규 개별 체결 또는 손익
      실적으로 간주하지 않습니다.
    </p>
    <div class="broker-source-grid">
      {#each [{ label: '이전', snapshot: record.sources.before_snapshot, scan: record.sources.before_scan, coverage: record.coverage.before_scan }, { label: '이후', snapshot: record.sources.after_snapshot, scan: record.sources.after_scan, coverage: record.coverage.after_scan }] as source}<div
        >
          <strong>{source.label} 자료</strong>
          <p class="muted identifier">
            계좌 {shortId(source.snapshot.id)} · 보유 확인 {formatTime(
              source.snapshot.holdings_observed_at,
            )}
          </p>
          <p class="muted identifier">
            스캔 {source.scan ? shortId(source.scan.id) : '이전 비교 관측 없음'} · {formatTime(
              source.scan?.collection_completed_at,
            )}
          </p>
          {#if source.scan}<p
              class:warning-state={source.coverage?.complete !== true}
              class:muted={source.coverage?.complete === true}
            >
              요청 범위 내 수집 {source.coverage?.complete === true
                ? '완료'
                : source.coverage?.complete === false
                  ? '미완료'
                  : '미확인'} · 계좌 전체 주문 확보 여부 미확인
            </p>{/if}
        </div>{/each}
    </div>
    <p class="muted">
      주문 {record.counts.orders}개 · 변화 {record.counts.changed_orders}개 · 비교값 없음 {record
        .counts.baseline_orders}개 · 충돌 {record.counts.conflicted_orders}개
    </p>
    <div class="table-container">
      <table>
        <caption class="sr-only">주문 누적 관측 대조</caption><thead
          ><tr
            ><th>주문·종목</th><th>판정·출처</th><th>이전 / 이후 누적 수량</th><th>수량 변화</th><th
              >누적 금액 변화</th
            ><th>누적 수수료 / 세금 변화</th><th>이전 / 이후 확인</th></tr
          ></thead
        ><tbody
          >{#each record.orders as item}<tr
              ><th scope="row" title={item.order_id}
                >{shortId(item.order_id)}<small
                  >{(item.after ?? item.before)?.order.symbol ?? '종목 미확인'} · {(
                    item.after ?? item.before
                  )?.order.currency ?? '통화 미확인'}</small
                ></th
              ><td
                >{item.classification
                  .map((code) => reconciliationLabels[code] ?? code)
                  .join(' · ')}<small>출처 미연결 · 개별 체결 ID 없음</small></td
              ><td class="numeric"
                >{item.before
                  ? formatDecimal(item.before.order.execution.filledQuantity)
                  : '이전 관측 없음'} / {item.after
                  ? formatDecimal(item.after.order.execution.filledQuantity)
                  : '이후 관측 없음'}</td
              ><td class="numeric">{formatDecimal(item.deltas.filled_quantity)}</td><td
                class="numeric">{formatDecimal(item.deltas.filled_amount)}</td
              ><td class="numeric"
                >{formatDecimal(item.deltas.commission)} / {formatDecimal(item.deltas.tax)}</td
              ><td
                >{formatTime(item.before?.observed_at)}<small
                  >{formatTime(item.after?.observed_at)}</small
                ></td
              ></tr
            >{/each}</tbody
        >
      </table>
    </div>
    {#if !record.orders.length}<p class="muted">
        선택한 스캔에서 비교할 주문 관측이 없습니다.
      </p>{/if}
    <details class="broker-history">
      <summary>주문별 원래 누적 값과 시각</summary>{#each record.orders as item}<section
          class="broker-order-history"
        >
          <h4 title={item.order_id}>{shortId(item.order_id)}</h4>
          {#each [{ label: '이전', version: item.before }, { label: '이후', version: item.after }] as entry}<div
              class="broker-history-version"
            >
              <strong>{entry.label}</strong>{#if entry.version}<p>
                  {brokerOrderStatus[entry.version.order.status] ?? entry.version.order.status} · {entry.version.groups_seen.join(
                    ' / ',
                  )}
                </p>
                <p class="muted">
                  누적 수량 {formatDecimal(entry.version.order.execution.filledQuantity)} · 누적 금액
                  {formatDecimal(entry.version.order.execution.filledAmount)} · 평균 체결가 {formatDecimal(
                    entry.version.order.execution.averageFilledPrice,
                  )} · 수수료 {formatDecimal(entry.version.order.execution.commission)} · 세금 {formatDecimal(
                    entry.version.order.execution.tax,
                  )}
                </p>
                <p class="muted">
                  주문 생성 {formatTime(entry.version.order.orderedAt)} · 최종 체결 보고 {formatTime(
                    entry.version.order.execution.filledAt,
                  )}
                </p>
                <p class="muted">
                  조회 {formatTime(entry.version.observed_at)} · 기록 {formatTime(
                    entry.version.recorded_at,
                  )}
                </p>
                <p class="muted identifier">
                  출처 관측 {entry.version.observation_ids.map(shortId).join(', ')}
                </p>{:else}<p class="muted">연결할 단일 비교 관측 없음</p>{/if}
            </div>{/each}
        </section>{/each}
    </details>
    <h3 class="broker-subheading">보유 변화 · 주문과의 연결 미확인</h3>
    <p class="muted">
      보유 목록에서 종목이 사라져도 0주로 채우지 않습니다. 관측 시점 차이와 선택 범위 때문에 수량
      변화의 원인이 미해결일 수 있습니다.
    </p>
    <div class="table-container">
      <table>
        <caption class="sr-only">계좌 보유 변화</caption><thead
          ><tr
            ><th>종목</th><th>이전 수량</th><th>이후 수량</th><th>수량 변화</th><th>관측 상태</th
            ></tr
          ></thead
        ><tbody
          >{#each record.holdings as item}<tr
              ><th scope="row"
                >{item.symbol} / {item.market}<small
                  >{item.before_currency ?? '미확인'} → {item.after_currency ?? '미확인'}</small
                ></th
              ><td class="numeric">{formatDecimal(item.before_quantity)}</td><td class="numeric"
                >{formatDecimal(item.after_quantity)}</td
              ><td class="numeric">{formatDecimal(item.quantity_delta)}</td><td
                >{reconciliationLabels[item.classification]} · 연결 미확인</td
              ></tr
            >{/each}</tbody
        >
      </table>
    </div>
    <h3 class="broker-subheading">통화별 매수 가능 금액 변화</h3>
    <p class="muted">현금 잔고·입출금·손익을 계산한 값이 아닙니다.</p>
    <div class="table-container">
      <table>
        <caption class="sr-only">매수 가능 금액 대조</caption><thead
          ><tr><th>통화</th><th>이전</th><th>이후</th><th>변화</th></tr></thead
        ><tbody
          >{#each record.buying_power as item}<tr
              ><th scope="row">{item.currency}</th><td class="numeric"
                >{formatDecimal(item.before_amount)}</td
              ><td class="numeric">{formatDecimal(item.after_amount)}</td><td class="numeric"
                >{formatDecimal(item.delta)}</td
              ></tr
            >{/each}</tbody
        >
      </table>
    </div>
    {#if record.warnings.length}<details class="broker-history">
        <summary>자료 범위와 대조 제한</summary>
        <ul>
          {#each record.warnings as warning}<li class="muted">{warning}</li>{/each}
        </ul>
      </details>{/if}
  </section>
{/snippet}

<section class="panel capital-panel broker-panel" aria-label="브로커 관측 대조">
  <details bind:open={opened}>
    <summary>브로커 관측 대조</summary>
    <p class="muted">
      저장된 주문·누적 체결 관측으로 계좌 변화를 대조합니다. 실제 주문 전송은 비활성입니다.
    </p>
    {#if !ready}<p class="muted small-empty">작업실 상태를 확인하고 있습니다.</p>{:else}
      <div class="capital-actions">
        <Button variant="outline" onclick={refresh} disabled={busy || scansQuery.isFetching}
          ><RotateCw size={15} aria-hidden="true" />브로커 자료 다시 읽기</Button
        >
      </div>
      {#if actionError}<p role="alert" class="error-state">{actionError}</p>{/if}{#if feedback}<p
          role="status"
          class="feedback-state"
        >
          {feedback}
        </p>{/if}
      {#if pendingScan}<div class="warning-state broker-pending">
          <p>
            수집 접수 확인 중 · 계좌 {pendingScan.parameters.account_seq}. 선택을 바꿔도 원래 조회
            범위를 다시 확인합니다.
          </p>
          <Button
            variant="outline"
            onclick={() => enqueue()}
            disabled={busy || synthetic || !jobsEnabled}>같은 수집 요청 결과 확인</Button
          >
        </div>{/if}
      {#if pendingSave}<div class="warning-state broker-pending">
          <p>
            대조 저장 확인 중 · 이전 {shortId(pendingSave.before_snapshot_id)} → 이후 {shortId(
              pendingSave.after_snapshot_id,
            )}. 원래 선택한 자료로 재확인합니다.
          </p>
          <Button variant="outline" onclick={save} disabled={busy || !jobsEnabled}
            >같은 대조 저장 결과 확인</Button
          >
        </div>{/if}
      <details class="broker-collection">
        <summary>브로커 읽기 스캔 접수</summary>
        {#if synthetic}<p class="warning-state">
            합성 작업실에서는 저장된 예시 관측으로 대조합니다. 실제 수집 요청은 접수하지 않습니다.
          </p>{/if}
        {#if !jobsEnabled}<p class="muted">
            수집 작업 접수와 결과 저장이 꺼져 있습니다. 저장 자료 조회와 대조 계산은 계속 사용할 수
            있습니다.
          </p>{/if}
        <p class="muted">
          수집 대상: {account
            ? `계좌 ${account.snapshot.account_seq} · 관측 ${shortId(account.id)}`
            : '상단에서 계좌 관측을 선택해 주세요.'}
        </p>
        <form class="capital-form" onsubmit={enqueue}>
          <label>주문 생성 시작일 (KST)<input type="date" bind:value={scanForm.fromDate} /></label
          ><label>주문 생성 종료일 (KST)<input type="date" bind:value={scanForm.toDate} /></label>
          <p class="muted capital-full">
            양 끝 날짜를 포함한 orderedAt 기준입니다. CLOSED의 최종 체결일 기간 조회가 아니며,
            미지정 날짜의 범위를 확대 해석하지 않습니다.
          </p>
          <label
            >종목 필터 (선택)<input
              bind:value={scanForm.symbol}
              maxlength="32"
              placeholder="미입력 시 종목 필터 없음"
            /></label
          >
          <details class="capital-full broker-history">
            <summary>페이지 한도·개별 주문 조회</summary>
            <div class="capital-form">
              <label
                >최대 CLOSED 페이지 수<input
                  inputmode="numeric"
                  bind:value={scanForm.maxPages}
                /></label
              ><label
                >페이지당 주문 수<input inputmode="numeric" bind:value={scanForm.pageSize} /></label
              ><label class="capital-full"
                >개별 조회 주문 ID (한 줄에 하나, 최대 20개)<textarea
                  bind:value={scanForm.detailOrderIds}
                  rows="3"
                  maxlength="10260"></textarea></label
              >
            </div>
          </details>
          <p class="muted capital-full">
            접수 후 네트워크 수집이 허용된 워커가 실행해야 관측이 저장됩니다. 작업 접수는 수집
            완료가 아닙니다.
          </p>
          <div class="capital-full">
            <Button
              type="submit"
              disabled={!jobsEnabled ||
                synthetic ||
                !account ||
                busy ||
                !!pendingSave ||
                !!pendingScan}>브로커 읽기 스캔 접수</Button
            >
          </div>
        </form>
        {#if jobsEnabled && jobsQuery.isError}<p role="alert" class="error-state">
            {jobsQuery.error.message}
          </p>{/if}
        {#if collectionJobs?.length}<ul class="broker-job-list">
            {#each collectionJobs as job}<li>
                <strong>{jobStates[job.status]}</strong> · 계좌 {typeof job.parameters
                  .account_seq === 'string'
                  ? job.parameters.account_seq
                  : '미확인'} · {shortId(job.id)} · {formatTime(job.updated_at)}
              </li>{/each}
          </ul>
          <p class="muted">실행 시도와 취소 요청은 아래 작업 실행 패널에서 확인합니다.</p>{/if}
      </details>
      <section class="capital-result" aria-label="저장 브로커 스캔">
        <h3>저장 브로커 스캔</h3>
        <p class="muted">
          공식 지원 주문 유형만 조회됩니다. PARTIAL_FILLED는 양 그룹에 나타날 수 있고 페이지 완료는
          계좌의 모든 주문 확보를 뜻하지 않습니다.
        </p>
        <div class="capital-form">
          <label class="capital-full"
            >확인할 저장 스캔<select bind:value={selectedScanId}
              ><option value="">저장 스캔을 선택하세요</option
              >{#each scans?.items ?? [] as item}<option value={item.id}
                  >{item.account_seq} · {formatTime(item.collection_completed_at)} · {item.mode ===
                  'synthetic'
                    ? '합성'
                    : '관측'} · {shortId(item.id)}</option
                >{/each}</select
            ></label
          >
        </div>
        {#if scansQuery.isFetching}<p class="muted">
            스캔 목록을 읽고 있습니다.
          </p>{:else if scansQuery.isError}<p role="alert" class="error-state">
            {scansQuery.error.message}
          </p>{:else if scans && !scans.items.length}<p class="muted">
            저장된 브로커 스캔이 없습니다.
          </p>{/if}
        {#if scans && (scans.omitted_count || scans.invalid_count)}<p class="warning-state">
            목록 생략 {scans.omitted_count}개 · 검증 불가 {scans.invalid_count}개
          </p>{/if}
        {#if selectedScanId && scanQuery.isFetching}<p class="muted">
            스캔 관측을 읽고 있습니다.
          </p>{:else if selectedScanId && scanQuery.isError}<p role="alert" class="error-state">
            {scanQuery.error.message}
          </p>{/if}
        {#if scan}<section class="broker-scan-detail" aria-label="선택한 스캔 상세">
            <h4>
              {scan.mode === 'synthetic' ? '합성 스캔' : '저장 관측'} · 계좌 {scan.account_seq}
            </h4>
            <p class="muted">
              조회 {formatTime(scan.collection_started_at)} ~ {formatTime(
                scan.collection_completed_at,
              )} · 기록 {formatTime(scan.recorded_at)}
            </p>
            {@render coverage(scan.coverage)}
            <p class="muted">
              종목 필터 {scan.request.symbol ?? '없음'} · 주문 관측 행 {scan.orders.length}개 · 출처
              관측 {scan.observation_ids.length}개
            </p>
            <div class="table-container">
              <table>
                <caption class="sr-only">저장된 주문 누적 보고</caption><thead
                  ><tr
                    ><th>주문·종목</th><th>그룹·상태</th><th>주문 / 누적 체결 수량</th><th
                      >평균 체결가</th
                    ><th>누적 금액</th><th>누적 수수료 / 세금</th><th>주문 생성 / 최종 체결 보고</th
                    ><th>조회 / 기록</th></tr
                  ></thead
                ><tbody
                  >{#each scan.orders as item}<tr
                      ><th scope="row" title={item.order.orderId}
                        >{shortId(item.order.orderId)}<small
                          >{item.order.symbol} · {item.order.currency} · {item.order.side === 'BUY'
                            ? '매수'
                            : '매도'}</small
                        ></th
                      ><td
                        >{item.source_group}<small
                          >{brokerOrderStatus[item.order.status] ?? item.order.status}</small
                        ></td
                      ><td class="numeric"
                        >{formatDecimal(item.order.quantity)} / {formatDecimal(
                          item.order.execution.filledQuantity,
                        )}</td
                      ><td class="numeric"
                        >{formatDecimal(item.order.execution.averageFilledPrice)}</td
                      ><td class="numeric">{formatDecimal(item.order.execution.filledAmount)}</td
                      ><td class="numeric"
                        >{formatDecimal(item.order.execution.commission)} / {formatDecimal(
                          item.order.execution.tax,
                        )}</td
                      ><td
                        >{formatTime(item.order.orderedAt)}<small
                          >{formatTime(item.order.execution.filledAt)}</small
                        ></td
                      ><td
                        >{formatTime(item.retrieved_at)}<small>{formatTime(item.recorded_at)}</small
                        ></td
                      ></tr
                    >{/each}</tbody
                >
              </table>
            </div>
            {#each scan.warnings as warning}<p class="muted">{warning}</p>{/each}
          </section>{/if}
      </section>
      <section class="capital-result" aria-label="계좌와 주문 대조 입력">
        <h3>이전·이후 관측 대조</h3>
        <form
          class="capital-form"
          onsubmit={(event) => {
            event.preventDefault();
            void calculate();
          }}
        >
          <label
            >이전 계좌 관측<select bind:value={beforeSnapshot}
              ><option value="">이전 관측 선택</option>{#each snapshots ?? [] as item}<option
                  value={item.id}
                  >{item.account_seq} · {formatTime(item.collection_completed_at)} · {shortId(
                    item.id,
                  )}</option
                >{/each}</select
            ></label
          >
          <label
            >이후 계좌 관측<select bind:value={afterSnapshot}
              ><option value="">이후 관측 선택</option>{#each snapshots ?? [] as item}<option
                  value={item.id}
                  >{item.account_seq} · {formatTime(item.collection_completed_at)} · {shortId(
                    item.id,
                  )}</option
                >{/each}</select
            ></label
          >
          <label
            >이전 스캔 (선택)<select bind:value={beforeScan}
              ><option value="">이전 비교 스캔 없음</option
              >{#each scans?.items ?? [] as item}<option value={item.id}
                  >{item.account_seq} · {formatTime(item.collection_completed_at)} · {shortId(
                    item.id,
                  )}</option
                >{/each}</select
            ></label
          >
          <label
            >이후 스캔<select bind:value={afterScan}
              ><option value="">이후 스캔 선택</option>{#each scans?.items ?? [] as item}<option
                  value={item.id}
                  >{item.account_seq} · {formatTime(item.collection_completed_at)} · {shortId(
                    item.id,
                  )}</option
                >{/each}</select
            ></label
          >
          {#if synthetic}<p class="muted capital-full">합성 대조로 계산합니다.</p>{:else}<label
              >대조 구분<select bind:value={comparisonMode}
                ><option value="prospective">관측 대조</option><option value="retrospective"
                  >사후 대조</option
                ></select
              ></label
            >{/if}
          <p class="muted capital-full">
            기준 시각은 선택한 자료의 마지막 기록 시각으로 고정합니다. 이전 스캔이 없으면 기존 누적
            값과의 차이는 미확인입니다.
          </p>
          {#if snapshotsQuery.isError}<p role="alert" class="error-state capital-full">
              {snapshotsQuery.error.message}
            </p>{/if}
          <div class="capital-actions capital-full">
            <Button
              type="submit"
              disabled={calculating ||
                busy ||
                !!pendingSave ||
                !!pendingScan ||
                !snapshots ||
                !scans}>관측 대조 계산</Button
            ><Button
              type="button"
              variant="outline"
              onclick={save}
              disabled={!jobsEnabled || !visiblePreview || busy || !!pendingSave || !!pendingScan}
              >대조 결과 저장</Button
            >
          </div>
          {#if !jobsEnabled}<p class="muted capital-full">
              현재 작업실은 대조 계산과 조회를 지원하며 결과 저장은 꺼져 있습니다.
            </p>{/if}
        </form>
      </section>
      {#if calculating}<p class="muted" role="status">선택한 관측을 대조하고 있습니다.</p>{/if}
      {#if visiblePreview}{@render comparison(visiblePreview, '대조 계산 결과')}{/if}
      <section class="capital-result">
        <h3>저장한 대조 결과</h3>
        <div class="capital-form">
          <label class="capital-full"
            >저장 대조 선택<select bind:value={selectedReportId}
              ><option value="">저장 결과를 선택하세요</option
              >{#each reports?.items ?? [] as item}<option value={item.id}
                  >{item.account_seq} · {formatTime(item.as_of)} · {item.mode === 'synthetic'
                    ? '합성'
                    : item.mode === 'retrospective'
                      ? '사후'
                      : '관측'} · {shortId(item.id)}</option
                >{/each}</select
            ></label
          >
        </div>
        {#if reportsQuery.isError}<p role="alert" class="error-state">
            {reportsQuery.error.message}
          </p>{/if}{#if reports && (reports.omitted_count || reports.invalid_count)}<p
            class="warning-state"
          >
            목록 생략 {reports.omitted_count}개 · 검증 불가 {reports.invalid_count}개
          </p>{/if}{#if reports && !reports.items.length}<p class="muted">
            저장한 대조 결과가 없습니다.
          </p>{/if}{#if selectedReportId && reportQuery.isError}<p role="alert" class="error-state">
            {reportQuery.error.message}
          </p>{/if}{#if selectedReportId && reportQuery.isFetching}<p class="muted">
            저장 대조를 읽고 있습니다.
          </p>{/if}
      </section>
      {#if saved}{@render comparison(saved, '저장 대조 상세')}{/if}
    {/if}
  </details>
</section>
