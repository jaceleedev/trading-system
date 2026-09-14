<script lang="ts">
  import { untrack } from 'svelte';
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { Button } from '$lib/components/ui/button';
  import { createPollingWindow } from '$lib/polling.svelte';
  import { jobIsActive } from '$lib/polling';
  import { fetchJobsStatus } from '$lib/jobs';
  import { waitReasonLabels } from '$lib/operations';
  import { formatTime } from '$lib/format';
  import {
    CaptureError,
    captureMatchesContext,
    fetchCaptureOptions,
    fetchCaptureResult,
    marketParameters,
    postCapture,
    readCaptureReceipt,
    receiptStorageKey,
    recoverCapture,
    type CaptureReceipt,
  } from '$lib/capture';

  let {
    ready = false,
    jobsEnabled = false,
    selectedSnapshot = '',
    selectedAccountSeq = null,
    onInvestigation,
    onPaper,
  }: {
    ready?: boolean;
    jobsEnabled?: boolean;
    selectedSnapshot?: string;
    selectedAccountSeq?: string | null;
    onInvestigation: (value: { snapshotId: string; captureIds: string[] }) => void;
    onPaper: (value: { captureIds: string[]; accountSeq: string | null }) => void;
  } = $props();
  let kind = $state<'account-sync' | 'market-capture'>('account-sync');
  let sourceSnapshot = $state('');
  let endpointAlias = $state('');
  let fields = $state<Record<string, string>>({});
  let pages = $state('1');
  let confirmed = $state(false);
  let receipt = $state<CaptureReceipt | null>(null);
  let loadedWorkspace = $state('');
  let submitting = $state(false);
  let linking = $state(false);
  let submissionRejected = $state(false);
  let ambiguousSubmission = $state(false);
  let actionError = $state('');
  let storageError = $state('');
  let feedback = $state('');
  const polling = createPollingWindow();
  const queryClient = useQueryClient();
  const optionsQuery = createQuery(() => ({
    queryKey: ['capture-options'],
    enabled: ready,
    queryFn: ({ signal }) => fetchCaptureOptions(signal),
  }));
  const statusQuery = createQuery(() => ({
    queryKey: ['jobs-status'],
    enabled: ready,
    queryFn: ({ signal }) => fetchJobsStatus(signal),
  }));
  let workspaceKey = $derived(optionsQuery.isSuccess ? optionsQuery.data.workspace_key : null);
  let choices = $derived(
    optionsQuery.isSuccess && !optionsQuery.isFetching ? optionsQuery.data : undefined,
  );
  let endpoint = $derived(choices?.market_endpoints.find((item) => item.alias === endpointAlias));
  let source = $derived(choices?.accounts.find((item) => item.id === sourceSnapshot));
  const recoveryQuery = createQuery(() => {
    const original = receipt;
    return {
      queryKey: ['capture-request', original?.workspaceKey, original?.body.request_key],
      enabled:
        ready && jobsEnabled && !!original && original.workspaceKey === workspaceKey && !submitting,
      queryFn: ({ signal }) => recoverCapture(original!.body, signal),
      refetchInterval: (query) =>
        polling.interval(query.state.status === 'success' && jobIsActive(query.state.data?.job)),
      refetchIntervalInBackground: false,
    };
  });
  let job = $derived(
    receipt &&
      recoveryQuery.isSuccess &&
      recoveryQuery.data.job.request_key === receipt.body.request_key
      ? recoveryQuery.data.job
      : undefined,
  );
  const resultQuery = createQuery(() => {
    const id = job?.id;
    return {
      queryKey: ['capture-result', id],
      enabled:
        ready && jobsEnabled && !!id && job?.status === 'succeeded' && !recoveryQuery.isError,
      queryFn: ({ signal }) => fetchCaptureResult(id!, signal),
    };
  });
  let result = $derived(
    job?.status === 'succeeded' &&
      !recoveryQuery.isError &&
      resultQuery.isSuccess &&
      !resultQuery.isFetching &&
      resultQuery.data.job_id === job.id
      ? resultQuery.data
      : undefined,
  );
  let contextMatches = $derived(!!receipt && captureMatchesContext(receipt, selectedAccountSeq));
  let paperIds = $derived(
    result?.observations.filter((item) => item.paper_candidate).map((item) => item.id) ?? [],
  );
  let investigationIds = $derived(
    result?.observations
      .filter((item) => item.normalization === 'supported')
      .map((item) => item.id) ?? [],
  );
  let waiting = $derived(
    statusQuery.isSuccess
      ? statusQuery.data.waiting_jobs.find((item) => item.job_id === job?.id)
      : undefined,
  );
  let absent = $derived(
    recoveryQuery.isError &&
      recoveryQuery.error instanceof CaptureError &&
      recoveryQuery.error.code === 'not_found',
  );
  let rejected = $derived(
    submissionRejected ||
      (recoveryQuery.isError &&
        recoveryQuery.error instanceof CaptureError &&
        recoveryQuery.error.code === 'invalid_request'),
  );
  let terminal = $derived(!!job && !jobIsActive(job));
  let working = $derived(
    submitting || linking || recoveryQuery.isFetching || resultQuery.isFetching,
  );
  let canSubmit = $derived(
    ready &&
      jobsEnabled &&
      !!choices &&
      !!workspaceKey &&
      loadedWorkspace === workspaceKey &&
      !receipt &&
      !storageError &&
      !submitting,
  );
  const fieldLabels: Record<string, string> = {
    symbol: '종목',
    symbols: '종목 목록',
    interval: '봉 간격',
    count: '페이지당 봉 개수',
    before: '이전 기준 시각',
    adjusted: '수정주가',
    market: '시장',
    status: '상장 상태',
    securityType: '증권 종류',
    commonShare: '보통주',
    dateTime: '조회 기준 시각',
    baseCurrency: '기준 통화',
    quoteCurrency: '상대 통화',
    date: '조회 날짜',
    from: '시작 범위',
    to: '종료 범위',
    fromDate: '시작 날짜',
    toDate: '종료 날짜',
  };
  const stateLabels = {
    queued: '접수 완료 · 대기',
    running: '실행 중',
    succeeded: '수집 작업 완료',
    failed: '수집 실패',
    cancelled: '수집 취소',
  };

  $effect(() => {
    const key = workspaceKey;
    if (key && key !== loadedWorkspace)
      untrack(() => {
        loadedWorkspace = key;
        try {
          const stored = localStorage.getItem(receiptStorageKey(key));
          receipt = readCaptureReceipt(stored, key);
          if (stored && !receipt)
            storageError =
              '저장된 수집 요청을 검증하지 못했습니다. 브라우저의 해당 요청 기록을 확인해 주세요.';
        } catch {
          storageError =
            '요청 복구 기록을 브라우저에 보관할 수 없습니다. 저장 기능을 허용한 뒤 다시 열어 주세요.';
        }
      });
  });

  $effect(() => {
    const recovered = job;
    const responseMissing = ambiguousSubmission;
    if (recovered && responseMissing && !submitting)
      untrack(() => {
        ambiguousSubmission = false;
        actionError = '';
        feedback = '접수 응답은 유실됐지만 원래 request key와 입력으로 접수된 작업을 확인했습니다.';
      });
  });

  function persist(value: CaptureReceipt) {
    localStorage.setItem(receiptStorageKey(value.workspaceKey), JSON.stringify(value));
    receipt = value;
  }
  function selectEndpoint(value: string) {
    endpointAlias = value;
    const selected = choices?.market_endpoints.find((item) => item.alias === value);
    fields = Object.fromEntries(
      (selected?.query_fields ?? []).map((item) => [
        item.name,
        item.default === null || item.default === undefined ? '' : String(item.default),
      ]),
    );
    pages = '1';
  }
  async function dispatch(original: CaptureReceipt) {
    if (
      submitting ||
      !ready ||
      !jobsEnabled ||
      receipt?.body.request_key !== original.body.request_key
    )
      return;
    submitting = true;
    submissionRejected = false;
    ambiguousSubmission = false;
    actionError = '';
    feedback = '';
    polling.restart();
    try {
      const accepted = await postCapture(original.body);
      queryClient.setQueryData(
        ['capture-request', original.workspaceKey, original.body.request_key],
        accepted,
      );
      try {
        persist({ ...original, jobId: accepted.job.id });
      } catch {
        storageError =
          '접수는 확인했으나 작업 ID를 브라우저에 갱신하지 못했습니다. 저장된 원래 request key로 재확인할 수 있습니다.';
      }
      feedback = '새 관측 수집을 접수했습니다. 네트워크를 허용한 worker가 실제 조회를 수행합니다.';
      await queryClient.invalidateQueries({ queryKey: ['jobs'] });
      await statusQuery.refetch();
    } catch (error) {
      submissionRejected =
        error instanceof CaptureError &&
        ['invalid_request', 'invalid_record', 'foreign_origin'].includes(error.code);
      ambiguousSubmission =
        !submissionRejected && !(error instanceof CaptureError && error.code === 'job_conflict');
      actionError =
        error instanceof Error ? error.message : '수집 접수 응답을 확인하지 못했습니다.';
    } finally {
      submitting = false;
    }
  }
  async function submit(event: SubmitEvent) {
    event.preventDefault();
    if (!canSubmit || !workspaceKey) return;
    actionError = '';
    try {
      if (kind === 'account-sync' && (!source || !confirmed))
        throw new Error('수집 대상 계좌 관측과 표시된 조회 범위를 확인해 주세요.');
      if (kind === 'market-capture' && !endpoint)
        throw new Error('허용 시장 endpoint를 명시적으로 선택해 주세요.');
      const original: CaptureReceipt = {
        version: 1,
        workspaceKey,
        body: {
          kind,
          request_key: crypto.randomUUID(),
          parameters:
            kind === 'account-sync'
              ? { account_seq: source!.account_seq, source_snapshot_id: source!.id }
              : marketParameters(endpoint!, fields, pages),
        },
        contextSnapshotId: selectedSnapshot,
        contextAccountSeq: selectedAccountSeq,
        jobId: null,
      };
      // Store immutable intent before any request can leave the browser.
      try {
        persist(original);
      } catch {
        storageError = '원래 요청을 브라우저에 보관하지 못해 수집을 접수하지 않았습니다.';
        return;
      }
      await dispatch(original);
    } catch (error) {
      actionError = error instanceof Error ? error.message : '수집 입력을 확인해 주세요.';
    }
  }
  async function refresh() {
    polling.restart();
    actionError = '';
    await statusQuery.refetch();
    if (receipt) {
      await recoveryQuery.refetch();
      if (job?.status === 'succeeded') await resultQuery.refetch();
    } else await optionsQuery.refetch();
  }
  function clearCompleted() {
    if (!receipt || working || (!terminal && !rejected)) return;
    try {
      localStorage.removeItem(receiptStorageKey(receipt.workspaceKey));
    } catch {
      storageError = '요청 복구 기록을 지우지 못했습니다. 같은 요청 상태를 먼저 확인해 주세요.';
      return;
    }
    receipt = null;
    submissionRejected = false;
    ambiguousSubmission = false;
    actionError = '';
    feedback = '';
    confirmed = false;
    void optionsQuery.refetch();
  }
  async function useInvestigation() {
    if (!result || !receipt || !contextMatches || linking) return;
    const original = receipt;
    const selectedAtClick = selectedSnapshot;
    const input = {
      snapshotId: result.snapshot_id ?? selectedSnapshot,
      captureIds: [...investigationIds],
    };
    linking = true;
    try {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['snapshots'] }),
        queryClient.invalidateQueries({ queryKey: ['market-catalog'] }),
      ]);
      if (
        receipt?.body.request_key !== original.body.request_key ||
        selectedSnapshot !== selectedAtClick ||
        !captureMatchesContext(original, selectedAccountSeq)
      )
        return;
      onInvestigation(input);
      feedback =
        '완료 자료를 새 조사 입력에 연결했습니다. 목적과 입력을 확인한 뒤 별도로 접수하세요.';
    } finally {
      linking = false;
    }
  }
  async function usePaper() {
    if (!result || !receipt || !contextMatches || !paperIds.length || linking) return;
    const original = receipt;
    const selectedAtClick = selectedSnapshot;
    const input = { captureIds: [...paperIds], accountSeq: selectedAccountSeq };
    linking = true;
    try {
      await queryClient.invalidateQueries({ queryKey: ['market-catalog'] });
      if (
        receipt?.body.request_key !== original.body.request_key ||
        selectedSnapshot !== selectedAtClick ||
        !captureMatchesContext(original, selectedAccountSeq)
      )
        return;
      onPaper(input);
      feedback =
        '완료 분봉을 모의 진행 입력에 추가했습니다. 원장을 선택하고 적격성을 확인한 뒤 별도로 진행하세요.';
    } finally {
      linking = false;
    }
  }
</script>

<section class="panel capture-panel" id="observation-capture-panel" aria-label="새 관측 수집">
  <div class="capture-heading">
    <div>
      <h2>새 관측 수집</h2>
      <p class="muted">대상과 조회 범위를 정해 계좌·시장 API 관측을 접수합니다.</p>
    </div>
    <Button variant="outline" onclick={refresh} disabled={!ready || working}
      >같은 요청 상태 확인</Button
    >
  </div>
  <p class="muted">
    저장 자료 다시 읽기는 기존 파일 조회입니다. 여기서 새로 접수한 수집만 네트워크 허용 worker가
    처리하며, 접수가 네트워크 허용 설정을 바꾸거나 실제 주문을 만들지 않습니다.
  </p>
  {#if !jobsEnabled}<p class="capture-notice">
      새 수집 접수가 꺼져 있습니다. 저장된 계좌와 시장 자료는 계속 읽을 수 있습니다.
    </p>{/if}
  {#if optionsQuery.isError}<p class="error-state" role="alert">
      {optionsQuery.error.message}
    </p>{/if}
  {#if statusQuery.isError}<p class="error-state" role="alert">{statusQuery.error.message}</p>{/if}
  {#if storageError}<p class="error-state" role="alert">{storageError}</p>{/if}
  {#if choices}
    <form onsubmit={submit} class="capture-form">
      <fieldset disabled={!canSubmit}>
        <label
          >수집 종류<select bind:value={kind}
            ><option value="account-sync">계좌 관측</option><option value="market-capture"
              >시장 관측</option
            ></select
          ></label
        >
        {#if kind === 'account-sync'}
          <label
            >수집 대상 계좌 관측<select
              bind:value={sourceSnapshot}
              onchange={() => (confirmed = false)}
              ><option value="">저장 관측에서 대상 선택</option
              >{#each choices.accounts as item}<option value={item.id}
                  >계좌 {item.account_seq} · {item.account_type} · {formatTime(
                    item.collection_completed_at,
                  )}</option
                >{/each}</select
            ></label
          >
          <p class="muted capture-full">
            저장 관측에 확인된 계좌만 선택합니다. 현재 로그인 계좌 목록을 직접 조회하거나 다른
            계좌의 존재를 추정하지 않습니다.
          </p>
          {#if choices.accounts_truncated}<p class="capture-full muted">
              계좌 관측 목록에 생략된 항목이 있습니다. 전체 계좌 목록을 의미하지 않습니다.
            </p>{/if}
          {#if !choices.accounts.length}<p class="capture-full">
              저장된 계좌 관측이 없습니다. 기존 계좌 CLI의 명시적 계좌 조회로 첫 관측을 만든 뒤 저장
              자료를 다시 읽으세요.
            </p>{/if}
          <div class="capture-full capture-scope">
            <strong>계좌 수집의 고정 조회 범위</strong>{#each choices.account_endpoints as item}<p>
                <code>{item.endpoint}</code> <code>{JSON.stringify(item.query)}</code>
              </p>{/each}
            <p class="muted">
              표시된 endpoint와 고정 한도만 조회합니다. 현금 잔고와 범위 밖 주문·자산은
              미확인입니다.
            </p>
          </div>
          <label class="capture-confirm capture-full"
            ><input type="checkbox" bind:checked={confirmed} />표시된 계좌 조회 범위를 확인했습니다</label
          >
        {:else}
          <label
            >허용 시장 endpoint<select
              value={endpointAlias}
              onchange={(event) => selectEndpoint(event.currentTarget.value)}
              ><option value="">endpoint 선택</option
              >{#each choices.market_endpoints as item}<option value={item.alias}
                  >{item.alias} · {item.endpoint}</option
                >{/each}</select
            ></label
          >
          {#if endpoint}
            {#each endpoint.query_fields as field}<label
                >{fieldLabels[field.name] ?? field.name} ({field.name})
                {#if field.enum_values?.length}<select
                    name={field.name}
                    bind:value={fields[field.name]}
                    required={field.required}
                    ><option value=""
                      >{field.required
                        ? '값 선택'
                        : field.default !== null
                          ? `기본값 사용 (${field.default})`
                          : '지정하지 않음'}</option
                    >{#each field.enum_values as value}<option value={String(value)}
                        >{String(value)}</option
                      >{/each}</select
                  >
                {:else if field.type === 'boolean'}<select
                    name={field.name}
                    bind:value={fields[field.name]}
                    required={field.required}
                    ><option value=""
                      >{field.default !== null
                        ? `기본값 사용 (${field.default})`
                        : '지정하지 않음'}</option
                    ><option value="true"
                      >{field.name === 'adjusted' ? '수정 적용 (true)' : '예 (true)'}</option
                    ><option value="false"
                      >{field.name === 'adjusted' ? '비수정 (false)' : '아니오 (false)'}</option
                    ></select
                  >
                {:else}<input
                    name={field.name}
                    bind:value={fields[field.name]}
                    required={field.required}
                    type={field.format === 'date' ? 'date' : 'text'}
                    inputmode={field.type === 'integer' ? 'numeric' : undefined}
                    placeholder={field.format === 'date-time'
                      ? '2026-09-14T09:00:00+09:00'
                      : undefined}
                  />{/if}
                <small class="muted"
                  >{field.required ? '필수' : '선택'}{field.default !== null
                    ? ` · 기본값 ${field.default}`
                    : ''}{field.minimum !== null && field.minimum !== undefined
                    ? ` · 최소 ${field.minimum}`
                    : ''}{field.maximum !== null && field.maximum !== undefined
                    ? ` · 최대 ${field.maximum}`
                    : ''}{field.format === 'date-time' ? ' · 시간대 필수' : ''}</small
                ></label
              >{/each}
            <label
              >최대 수집 페이지<input bind:value={pages} inputmode="numeric" required /><small
                class="muted">1~{endpoint.max_pages}페이지 · 조회 시점에 제공되는 범위만 수집</small
              ></label
            >
          {/if}
        {/if}
        <div class="capture-full">
          <Button type="submit" disabled={!canSubmit}>새 관측 수집 접수</Button>
        </div>
      </fieldset>
    </form>
  {/if}
  {#if actionError}<p class="error-state" role="alert">{actionError}</p>{/if}
  {#if feedback}<p role="status" class="capture-notice">{feedback}</p>{/if}
  {#if receipt}
    <section class="capture-receipt" aria-label="수집 요청과 결과">
      <h3>
        {job
          ? stateLabels[job.status]
          : submitting
            ? '접수 응답 확인 중'
            : submissionRejected
              ? '입력 거부 · 미접수'
              : '접수 여부 미확인'}
      </h3>
      <p class="muted identifier">request key · {receipt.body.request_key}</p>
      <p class="muted">
        원래 입력을 브라우저에 보관했습니다. 새로고침과 상태 확인은 같은 요청을 조회합니다.
      </p>
      <details>
        <summary>고정된 수집 입력</summary>
        <pre>{JSON.stringify(receipt.body, null, 2)}</pre>
      </details>
      {#if recoveryQuery.isError}<p role="alert" class="error-state">
          {recoveryQuery.error.message}
        </p>{/if}
      {#if absent && !submissionRejected}<Button
          variant="outline"
          onclick={() => receipt && dispatch(receipt)}
          disabled={!jobsEnabled || working}>원래 입력으로 같은 요청 접수</Button
        >{/if}
      {#if waiting}<p class="capture-notice">
          대기 이유: {waiting.reasons
            .map((reason) => waitReasonLabels[reason] ?? reason)
            .join(' · ')}
        </p>{/if}
      {#if job}
        <dl>
          <div>
            <dt>작업 ID</dt>
            <dd class="identifier">{job.id}</dd>
          </div>
          <div>
            <dt>시스템 접수 시각</dt>
            <dd>{formatTime(job.created_at)}</dd>
          </div>
          <div>
            <dt>작업 종료 시각</dt>
            <dd>{formatTime(job.finished_at)}</dd>
          </div>
        </dl>
        {#if job.error_code}<p role="alert" class="error-state">
            수집 오류: {job.error_code}. 완료 관측은 확인되지 않았습니다.
          </p>{/if}
      {/if}
      {#if resultQuery.isFetching}<p role="status">
          작업이 만든 저장 관측을 대조하고 있습니다.
        </p>{:else if resultQuery.isError}<p class="error-state" role="alert">
          {resultQuery.error.message}
        </p>{/if}
      {#if result}
        <div class="capture-result" aria-label="완료 관측 결과">
          <h3>검증된 완료 관측</h3>
          <dl>
            <div>
              <dt>계좌 관측 ID</dt>
              <dd class="identifier">{result.snapshot_id ?? '해당 없음'}</dd>
            </div>
            <div>
              <dt>수집 시작</dt>
              <dd>{formatTime(result.collection_started_at)}</dd>
            </div>
            <div>
              <dt>수집 완료</dt>
              <dd>{formatTime(result.collection_completed_at)}</dd>
            </div>
            <div>
              <dt>요청 / 수신 페이지</dt>
              <dd>
                {result.coverage.requested_pages ?? '미확인'} / {result.coverage.received_pages ??
                  '미확인'}
              </dd>
            </div>
            <div>
              <dt>조회 잘림</dt>
              <dd>
                {result.coverage.truncated === null
                  ? '미확인'
                  : result.coverage.truncated
                    ? '한도에서 잘림'
                    : '표시된 범위에서 잘림 없음'}
              </dd>
            </div>
            <div>
              <dt>후속 페이지</dt>
              <dd>
                {result.coverage.has_more === null
                  ? '미확인'
                  : result.coverage.has_more
                    ? '있음'
                    : '응답 기준 없음'}
              </dd>
            </div>
          </dl>
          {#each result.observations as observation}<article class="capture-observation">
              <strong
                >{observation.symbol ?? '계좌·시장 관측'} · {observation.normalization ===
                'supported'
                  ? '지원 자료'
                  : observation.normalization === 'unsupported'
                    ? '정규화 미지원'
                    : result.kind === 'account-sync'
                      ? '계좌 관측'
                      : '원자료 보관 · 봉 정규화 해당 없음'}</strong
              >
              <p class="identifier">{observation.id}</p>
              <p>
                <code>{observation.endpoint}</code> · 관측 시각 {formatTime(
                  observation.observed_at,
                )}
              </p>
              <p>
                {observation.interval ?? '봉 간격 해당 없음'} · {observation.adjusted === null
                  ? '수정 여부 미확인'
                  : observation.adjusted
                    ? '수정주가'
                    : '비수정'} · 봉 {observation.candle_count ?? '미확인'}개
              </p>
              {#if observation.paper_candidate}<p>
                  모의 진행 후보 분봉 · 선택 이후 관측 여부와 원장별 적격성은 진행 시 서버에서
                  확인합니다.
                </p>{/if}
            </article>{/each}
          {#if result.coverage.unknowns.length}<p>
              미확인 범위: {result.coverage.unknowns.join(' · ')}
            </p>{/if}
          {#each result.warnings as warning}<p class="muted">{warning}</p>{/each}
          <p class="muted">
            관측 시각은 가격·봉의 원래 기준 시각 및 시스템 기록 시각과 다릅니다. 수집 완료는 전체
            범위 확인이나 최종 확정 봉을 뜻하지 않습니다.
          </p>
          {#if !contextMatches}<p class="capture-notice">
              현재 선택 계좌가 원래 수집 대상 또는 수집 당시 선택 계좌와 다릅니다. 같은 계좌 관측을
              상단에서 선택한 뒤 연결하세요. 결과는 현재 계좌에 자동 적용되지 않습니다.
            </p>{/if}
          <div class="capture-actions">
            <Button
              variant="outline"
              onclick={useInvestigation}
              disabled={working ||
                !contextMatches ||
                (!result.snapshot_id && !investigationIds.length)}
              >완료 관측을 다음 조사에 사용</Button
            ><Button
              variant="outline"
              onclick={usePaper}
              disabled={working || !contextMatches || !paperIds.length}
              >완료 분봉을 모의 진행 입력에 추가</Button
            >
          </div>
          <p class="muted">
            연결은 다음 입력만 채웁니다. 과거 조사 입력과 이미 고정한 모의 대안·가정은 유지하며 조사
            접수와 모의 진행은 별도로 선택합니다.
          </p>
        </div>
      {/if}
      {#if terminal || rejected}<Button
          variant="outline"
          onclick={clearCompleted}
          disabled={working}>다른 수집 준비</Button
        >{/if}
      <p class="muted">
        활성 작업은 보이는 동안 5초 간격으로 최대 5분 자동 확인합니다. 오류·완료·숨겨진 화면에서는
        멈추며 위 상태 확인으로 다시 읽습니다.
      </p>
    </section>
  {/if}
</section>

<style>
  .capture-panel {
    margin-top: 1.5rem;
    padding: 1.4rem;
  }
  .capture-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }
  h2 {
    font-size: 1.05rem;
    font-weight: 650;
  }
  h3 {
    font-weight: 650;
    margin-bottom: 0.65rem;
  }
  p {
    margin: 0.6rem 0;
    font-size: 0.85rem;
    line-height: 1.65;
  }
  fieldset {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.9rem;
    border: 0;
    padding: 0;
    margin: 1rem 0;
    min-width: 0;
  }
  label {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    font-size: 0.83rem;
    min-width: 0;
  }
  input,
  select {
    border: 1px solid var(--border, #d9dfe8);
    background: var(--background, #fff);
    border-radius: 5px;
    min-height: 2.3rem;
    padding: 0.4rem 0.6rem;
    width: 100%;
    min-width: 0;
    color: inherit;
  }
  input:disabled,
  select:disabled {
    opacity: 0.65;
  }
  .capture-full {
    grid-column: 1 / -1;
  }
  .capture-confirm {
    flex-direction: row;
    align-items: center;
  }
  .capture-confirm input {
    width: 1rem;
    min-height: 1rem;
  }
  .capture-scope,
  .capture-notice {
    background: #f4f7fb;
    border-radius: 6px;
    padding: 0.8rem;
  }
  .capture-receipt {
    border-top: 1px solid #e1e6ed;
    margin-top: 1rem;
    padding-top: 1.2rem;
  }
  .capture-result {
    margin: 1rem 0;
  }
  dl {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.8rem;
    margin: 1rem 0;
    font-size: 0.82rem;
  }
  dt {
    color: #607084;
    margin-bottom: 0.3rem;
  }
  dd {
    margin: 0;
  }
  .identifier,
  code {
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  code,
  pre {
    font-size: 0.75rem;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    padding: 0.8rem;
    background: #f4f7fb;
  }
  .capture-observation {
    border: 1px solid #e1e6ed;
    padding: 0.8rem;
    border-radius: 6px;
    margin: 0.8rem 0;
  }
  .capture-actions {
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin: 1rem 0;
  }
  summary {
    cursor: pointer;
    font-size: 0.85rem;
  }
  @media (max-width: 640px) {
    .capture-panel {
      padding: 1rem;
    }
    .capture-heading {
      flex-direction: column;
    }
    fieldset,
    dl {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
