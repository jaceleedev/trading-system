<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    InvestigationCreate,
    InvestigationResponse,
    InvestigationRevise,
    ResearchResponse,
  } from '$lib/api/types.gen';
  import {
    executionText,
    fetchInvestigation,
    fetchInvestigations,
    investigationState,
    InvestigationRequestError,
    isDefiniteInvestigationError,
    parseInvestigationSymbols,
    reviewInvestigation,
    stopInvestigation,
    submitInvestigation,
  } from '$lib/investigations';
  import { fetchMarketCatalog } from '$lib/market';
  import { actionLabels, modeLabels, recordTitle } from '$lib/research';
  import { formatTime, safeSourceUrl, shortId } from '$lib/format';

  let {
    ready,
    jobsEnabled,
    synthetic,
    selectedSnapshot,
    knownRecords,
    onselect,
  }: {
    ready: boolean;
    jobsEnabled: boolean;
    synthetic: boolean;
    selectedSnapshot: string;
    knownRecords: ResearchResponse[];
    onselect: (id: string) => void;
  } = $props();
  let purpose = $state('');
  let symbols = $state('');
  let captureIds = $state<string[]>([]);
  let evidenceIds = $state<string[]>([]);
  let selectedId = $state<string | null>(null);
  let busy = $state(false);
  let feedback = $state('');
  let actionError = $state('');
  let createOpen = $state(false);
  let pendingCreate = $state<InvestigationCreate | null>(null);
  let reviewInput = $state<InvestigationRevise | null>(null);
  let reviewSymbols = $state('');
  let reviewId = $state<string | null>(null);
  let reviewStoredCaptureIds = $state<string[]>([]);
  let reviewStoredEvidenceIds = $state<string[]>([]);
  let pendingReview = $state<{ id: string; body: InvestigationRevise } | null>(null);
  let pendingPause = $state<{ id: string; revision: number } | null>(null);

  const listQuery = createQuery(() => ({
    queryKey: ['investigations'],
    enabled: ready && jobsEnabled,
    queryFn: ({ signal }) => fetchInvestigations(signal),
  }));
  let available = $derived(ready && jobsEnabled && listQuery.isSuccess && !listQuery.isFetching);
  let items = $derived(available ? listQuery.data?.items : undefined);
  let disabled = $derived(
    !jobsEnabled ||
      (listQuery.isError &&
        listQuery.error instanceof InvestigationRequestError &&
        listQuery.error.code === 'investigations_disabled'),
  );
  const catalogQuery = createQuery(() => ({
    queryKey: ['market-catalog'],
    enabled: available,
    queryFn: ({ signal }) => fetchMarketCatalog(signal),
  }));
  let captures = $derived(
    catalogQuery.isSuccess && !catalogQuery.isFetching
      ? catalogQuery.data.items.filter((item) => item.status === 'supported')
      : [],
  );
  let evidence = $derived(knownRecords.filter((item) => item.record.kind === 'evidence'));
  const detailQuery = createQuery(() => {
    const id = selectedId;
    return {
      queryKey: ['investigation', id],
      enabled: available && !!id,
      queryFn: ({ signal }) => fetchInvestigation(id!, signal),
    };
  });
  let detail = $derived(
    available &&
      detailQuery.isSuccess &&
      !detailQuery.isFetching &&
      detailQuery.data.investigation.id === selectedId
      ? detailQuery.data
      : undefined,
  );
  let output = $derived(detail?.latest_output);
  let execution = $derived(detail?.latest_execution);
  let unresolved = $derived(!!pendingCreate || !!pendingReview || !!pendingPause);
  let refreshing = $derived(listQuery.isFetching || detailQuery.isFetching);

  function selectInvestigation(id: string) {
    selectedId = id;
    if (!pendingReview) {
      reviewInput = null;
      reviewId = null;
    }
  }
  async function refresh() {
    const listed = await listQuery.refetch();
    if (listed.isSuccess && pendingCreate) {
      const found = listed.data.items.find(
        (item) => item.request_key === pendingCreate?.request_key,
      );
      if (found) {
        pendingCreate = null;
        selectedId = found.id;
        feedback = '접수된 조사를 확인했습니다.';
        actionError = '';
      }
    }
    if (selectedId) {
      const result = await detailQuery.refetch();
      if (
        result.isSuccess &&
        pendingPause?.id === result.data.investigation.id &&
        result.data.investigation.status === 'paused'
      ) {
        pendingPause = null;
        feedback = '조사 일시 정지를 확인했습니다.';
        actionError = '';
      }
    }
  }
  async function accepted(result: InvestigationResponse, message: string) {
    selectedId = result.investigation.id;
    feedback = message;
    actionError = '';
    await listQuery.refetch();
    await detailQuery.refetch();
  }
  async function create(event?: SubmitEvent) {
    event?.preventDefault();
    if (!available || busy || pendingReview || pendingPause) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingCreate) {
        if (!purpose.trim()) throw new Error('조사 목적을 적어 주세요.');
        pendingCreate = {
          purpose: purpose.trim(),
          symbols: parseInvestigationSymbols(symbols),
          snapshot_id: selectedSnapshot || null,
          capture_ids: [...captureIds],
          evidence_ids: [...evidenceIds],
          mode: synthetic ? 'synthetic' : 'prospective',
          request_key: crypto.randomUUID(),
        };
      }
      busy = true;
      const result = await submitInvestigation(pendingCreate);
      pendingCreate = null;
      purpose = '';
      symbols = '';
      captureIds = [];
      evidenceIds = [];
      createOpen = false;
      await accepted(
        result,
        '조사를 접수했습니다. 모델 실행 여부는 아래 작업 상태에서 확인하세요.',
      );
    } catch (error) {
      if (isDefiniteInvestigationError(error)) pendingCreate = null;
      actionError =
        error instanceof Error ? error.message : '조사 접수 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  function prepareReview() {
    if (!detail || busy || unresolved) return;
    const item = detail.investigation;
    reviewId = item.id;
    reviewInput = {
      purpose: item.context_input.purpose,
      mode: item.context_input.mode,
      snapshot_id: item.context_input.snapshot_id,
      capture_ids: [...item.context_input.capture_ids],
      evidence_ids: [...item.context_input.evidence_ids],
      symbols: [...item.context_input.symbols],
      expected_revision: item.current_revision,
      request_key: crypto.randomUUID(),
    };
    reviewSymbols = item.context_input.symbols.join(', ');
    reviewStoredCaptureIds = [...item.context_input.capture_ids];
    reviewStoredEvidenceIds = [...item.context_input.evidence_ids];
    feedback = '';
    actionError = '';
  }
  async function revise(event?: SubmitEvent) {
    event?.preventDefault();
    if (!available || busy || pendingCreate || pendingPause) return;
    feedback = '';
    actionError = '';
    try {
      if (!pendingReview) {
        if (!reviewInput || !reviewId || !reviewInput.purpose.trim())
          throw new Error('재검토 목적과 입력 자료를 확인해 주세요.');
        pendingReview = {
          id: reviewId,
          body: {
            ...structuredClone($state.snapshot(reviewInput)),
            purpose: reviewInput.purpose.trim(),
            symbols: parseInvestigationSymbols(reviewSymbols),
          },
        };
      }
      busy = true;
      const result = await reviewInvestigation(pendingReview.id, pendingReview.body);
      pendingReview = null;
      reviewInput = null;
      reviewId = null;
      await accepted(
        result,
        '새 입력으로 재검토를 접수했습니다. 이전 결과와 새 버전의 실행 상태를 구분해 확인하세요.',
      );
    } catch (error) {
      if (isDefiniteInvestigationError(error)) pendingReview = null;
      actionError =
        error instanceof Error ? error.message : '재검토 접수 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  async function pause() {
    if (!available || busy || pendingCreate || pendingReview) return;
    if (!pendingPause) {
      if (!detail) return;
      pendingPause = {
        id: detail.investigation.id,
        revision: detail.investigation.current_revision,
      };
    }
    feedback = '';
    actionError = '';
    busy = true;
    try {
      const result = await stopInvestigation(pendingPause.id, pendingPause.revision);
      pendingPause = null;
      reviewInput = null;
      reviewId = null;
      await accepted(
        result,
        '조사를 일시 정지했습니다. 진행 중인 작업은 취소 요청과 완료를 구분해 확인하세요.',
      );
    } catch (error) {
      if (isDefiniteInvestigationError(error)) pendingPause = null;
      actionError =
        error instanceof Error ? error.message : '일시 정지 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  const jobStatusLabels = {
    queued: '대기',
    running: '실행 중',
    succeeded: '완료',
    failed: '실패',
    cancelled: '취소됨',
  };
  const runtimeFields = [
    ['cli_version', 'Codex CLI 버전'],
    ['started_at', '프로세스 시작'],
    ['finished_at', '프로세스 종료'],
    ['exit_code', '종료 코드'],
    ['requested_model', '요청한 모델'],
    ['requested_reasoning_effort', '요청한 추론 설정'],
    ['model_source', '모델 설정 출처'],
    ['reported_model', '보고된 모델'],
    ['web_search_count', '관측된 웹 검색 수'],
    ['input_sha256', '입력 해시'],
    ['output_schema_sha256', '출력 계약 해시'],
    ['event_stream_sha256', '실행 이벤트 해시'],
  ];
</script>

{#snippet listSection(title: string, values: string[])}<section>
    <h3>{title}</h3>
    {#if values.length}<ul>
        {#each values as value}<li>{value}</li>{/each}
      </ul>{:else}<p class="muted">기록된 항목이 없습니다.</p>{/if}
  </section>{/snippet}

<section class="panel investigations-panel" aria-label="AI 조사" aria-busy={refreshing || busy}>
  <header class="investigation-heading">
    <div>
      <h2>AI 조사</h2>
      <p class="muted">목적과 근거를 정하고, 새로운 관측에 따라 다시 판단합니다.</p>
    </div>
    <Button
      variant="outline"
      class="reload-button"
      onclick={refresh}
      disabled={!ready || refreshing || busy}
      ><RotateCw size={15} aria-hidden="true" />조사 목록 다시 읽기</Button
    >
  </header>
  {#if !ready}<p class="empty-state muted">작업실 연결을 확인하고 있습니다.</p>
  {:else if listQuery.isFetching}<p class="empty-state" role="status">
      저장된 조사를 읽고 있습니다.
    </p>
  {:else if disabled}<p class="empty-state muted">이 작업실에서는 AI 조사 실행이 꺼져 있습니다.</p>
  {:else if listQuery.isError}<p class="empty-state error-state" role="alert">
      {listQuery.error.message}
    </p>
  {:else if items}
    <details class="investigation-create" bind:open={createOpen}>
      <summary>새 조사 시작</summary>
      <form class="investigation-form" onsubmit={create}>
        <label class="investigation-objective"
          >조사 목적<textarea
            maxlength="2000"
            required
            bind:value={purpose}
            disabled={busy || unresolved}
            placeholder="확인할 기회와 비교할 대안, 판단에 필요한 내용을 적어 주세요."
          ></textarea></label
        >
        <div>
          <p class="muted">선택 계좌</p>
          <p class="investigation-account">
            {selectedSnapshot
              ? `상단에서 선택한 관측 · ${shortId(selectedSnapshot)}`
              : '계좌 관측 선택 없음'}
          </p>
          <p class="muted">계좌를 포함하려면 위 계좌 관측에서 명시적으로 선택하세요.</p>
        </div>
        <label
          >관심 종목 · 선택<input
            bind:value={symbols}
            disabled={busy || unresolved}
            placeholder="ALPHA, BETA"
          /></label
        >
        <label
          >시장 원자료 · 선택<select
            aria-label="시장 원자료 · 선택"
            multiple
            bind:value={captureIds}
            disabled={busy || unresolved || !catalogQuery.isSuccess || catalogQuery.isFetching}
            >{#each captures as capture}<option value={capture.capture_id}
                >{capture.symbol ?? '시장'} · {capture.interval ?? ''} · {capture.adjusted
                  ? '수정'
                  : '수정 전'} · {formatTime(capture.retrieved_at)} · {shortId(
                  capture.capture_id,
                )}</option
              >{/each}</select
          ><span class="muted">여러 항목 선택 가능 · 현재 목록의 지원 자료만 표시</span
          >{#if catalogQuery.isError}<span class="error-state"
              >시장 목록을 읽지 못했습니다. 선택을 확인해 주세요.</span
            >{/if}</label
        >
        <label
          >연구 근거 · 선택<select
            aria-label="연구 근거 · 선택"
            multiple
            bind:value={evidenceIds}
            disabled={busy || unresolved}
            >{#each evidence as item}<option value={item.id}
                >{recordTitle(item)} · {modeLabels[item.record.mode]}</option
              >{/each}</select
          ><span class="muted">현재 연구 목록에 포함된 근거만 표시</span></label
        >
        <div class="investigation-submit">
          <Button type="submit" disabled={busy || unresolved || !purpose.trim()}>조사 접수</Button>
          <p class="muted">
            {synthetic ? '합성 입력으로 접수합니다. ' : ''}접수 후 실행을 허용한 worker에서 Codex를
            시작합니다.
          </p>
        </div>
      </form>
    </details>
    <div class="investigation-feedback" aria-live="polite">
      {#if feedback}<p>{feedback}</p>{/if}{#if actionError}<p class="error-state" role="alert">
          {actionError}
        </p>{/if}
      {#if pendingCreate}<p>
          접수 여부 확인 대기 · 원래 목적: {pendingCreate.purpose} · 계좌 {pendingCreate.snapshot_id
            ? shortId(pendingCreate.snapshot_id)
            : '선택 없음'}
        </p>
        <Button variant="outline" onclick={() => create()} disabled={busy || !available}
          >같은 접수 다시 확인</Button
        >{/if}
      {#if pendingReview}<p>
          재검토 접수 여부 확인 대기 · 조사 {shortId(pendingReview.id)} · 기준 버전 {pendingReview
            .body.expected_revision}
        </p>
        <Button variant="outline" onclick={() => revise()} disabled={busy || !available}
          >같은 재검토 다시 확인</Button
        >{/if}
      {#if pendingPause}<p>일시 정지 여부 확인 대기 · 조사 {shortId(pendingPause.id)}</p>
        <Button variant="outline" onclick={pause} disabled={busy || !available}
          >일시 정지 다시 확인</Button
        >{/if}
    </div>
    {#if !items.length}<p class="empty-state muted">
        등록된 조사가 없습니다. 목적을 적어 첫 조사를 시작하세요.
      </p>
    {:else}<ul class="investigation-list">
        {#each items as item}<li>
            <button
              class="investigation-select"
              aria-pressed={selectedId === item.id}
              onclick={() => selectInvestigation(item.id)}
              ><span
                ><strong>{item.context_input.purpose}</strong><small
                  >버전 {item.current_revision} · {modeLabels[item.context_input.mode]} · {formatTime(
                    item.updated_at,
                  )}</small
                ></span
              ><span class="investigation-state"
                >{investigationState(
                  item,
                  detail?.investigation.id === item.id ? detail.active_job : undefined,
                )}</span
              ></button
            >
          </li>{/each}
      </ul>
      <p class="muted">최신 조사 최대 50개 · 실행 상태는 선택 후 또는 다시 읽기로 확인</p>{/if}
    {#if selectedId}
      <section class="investigation-detail" aria-label="선택한 조사 상세">
        {#if detailQuery.isFetching}<p class="empty-state" role="status">
            조사 입력과 실행 결과를 읽고 있습니다.
          </p>
        {:else if detailQuery.isError}<p class="empty-state error-state" role="alert">
            {detailQuery.error.message}
          </p>
        {:else if detail}
          <div class="investigation-heading">
            <h3>{detail.investigation.context_input.purpose}</h3>
            <span class="investigation-state"
              >{investigationState(detail.investigation, detail.active_job)}</span
            >
          </div>
          <p class="muted">
            현재 버전 {detail.investigation.current_revision} · 최근 결과 {detail.investigation
              .latest_completed_revision === null
              ? '없음'
              : `${detail.investigation.latest_completed_revision}번 버전`} · 입력 기준 {formatTime(
              detail.investigation.context_input.as_of,
            )}
          </p>
          {#if detail.active_job}<p class="muted">
              작업 {jobStatusLabels[detail.active_job.status]} · 실행 시도 {detail.active_job
                .attempt_count}/{detail.active_job.max_attempts}{detail.active_job.cancel_requested
                ? ' · 취소 요청됨'
                : ''}{detail.active_job.error_code ? ` · 오류 ${detail.active_job.error_code}` : ''}
            </p>{/if}
          {#if detail.investigation.latest_completed_revision !== null && detail.investigation.latest_completed_revision !== detail.investigation.current_revision}<p
              class="market-notice"
            >
              아래 결과는 이전 버전 {detail.investigation.latest_completed_revision}의 결과입니다.
              현재 버전의 판단 완료를 뜻하지 않습니다.
            </p>{/if}
          {#if output}
            <section class="detail-section">
              <h3>최근 AI 결과</h3>
              <p>{output.summary}</p>
              <p>{output.rationale}</p>
            </section>
            <div class="investigation-result-grid">
              <section>
                <h3>기회 제안과 지지 근거</h3>
                {#if !output.opportunities.length}<p class="muted">
                    기록된 제안이 없습니다.
                  </p>{/if}{#each output.opportunities as opportunity}<div class="action-line">
                    <strong
                      >{opportunity.symbol} / {opportunity.market} · {actionLabels[
                        opportunity.action
                      ] ?? opportunity.action}</strong
                    >
                    <p>{opportunity.rationale}</p>
                    {#each opportunity.evidence_ids as id}<button
                        class="text-button"
                        onclick={() => onselect(id)}>근거 {shortId(id)} 보기</button
                      >{/each}
                  </div>{/each}
              </section>
              {@render listSection('반대 근거', output.opposing_evidence)}{@render listSection(
                '불확실성',
                output.uncertainties,
              )}{@render listSection('비교한 대안', output.alternatives)}
            </div>
            <section class="detail-section">
              <h3>후속 검토 조건</h3>
              <p>제안 시각 {formatTime(output.review_after)}</p>
              {#each output.review_conditions as condition}<p>
                  {condition.kind === 'market' ? '시장 관측 변경' : '새 근거'} · {condition.symbol ??
                    '특정 종목 제한 없음'}
                </p>{/each}
              <p class="muted">실제 예약 상태와 실행은 작업 기록에서 확인합니다.</p>
              {#if output.research_requests.length}<p class="muted">
                  추가 자료 요청 {output.research_requests.length}개 · 요청 기록만으로 수집 완료를
                  뜻하지 않습니다.
                </p>{/if}
            </section>
            {#if output.source_findings.length}<section class="detail-section">
                <h3>모델이 제시한 출처 · 확인 전</h3>
                {#each output.source_findings as source}<div class="action-line">
                    {#if safeSourceUrl(source.url)}<a
                        href={safeSourceUrl(source.url)}
                        target="_blank"
                        rel="noopener noreferrer">{source.title}</a
                      >{:else}<strong>{source.title}</strong>{/if}
                    <p>{source.claim}</p>
                    <p class="muted">제시된 발표 시각 {formatTime(source.source_published_at)}</p>
                  </div>{/each}
              </section>{/if}
          {:else}<p class="empty-state muted">
              저장된 AI 결과가 아직 없습니다. 접수와 모델 실행 완료는 구분됩니다.
            </p>{/if}
          {#if detail.research_jobs?.length}
            <section class="detail-section" aria-label="현재 버전의 후속 자료 수집">
              <h3>현재 버전 {detail.investigation.current_revision}의 후속 자료 수집</h3>
              {#each detail.research_jobs as job (job.id)}
                <div class="action-line">
                  <strong
                    >{job.kind === 'account-sync'
                      ? '계좌 관측 수집'
                      : job.kind === 'market-capture'
                        ? '시장 자료 수집'
                        : job.kind} · {jobStatusLabels[job.status]}</strong
                  >
                  <p class="muted">
                    실행 시도 {job.attempt_count}/{job.max_attempts}{job.cancel_requested
                      ? ' · 취소 요청됨'
                      : ''}{job.error_code ? ` · 오류 ${job.error_code}` : ''}
                  </p>
                  {#if job.status === 'queued'}<p class="muted">
                      실행 가능 시각 {formatTime(job.available_at)}
                    </p>{/if}
                </div>
              {/each}
            </section>
          {/if}
          <details class="subtle-details investigation-runtime">
            <summary>실행 정보와 입력·버전</summary>
            <p class="muted">
              {synthetic ? '합성 입력 작업실입니다. ' : ''}프로세스 실행 기록과 모델 이름의 선언은
              구분합니다.
            </p>
            {#if execution}<p>
                {execution.source === 'local_subprocess'
                  ? '서버가 기록한 로컬 프로세스 실행'
                  : '실행 출처 미확인'} · 모델 식별 검증 없음
              </p>
              <dl class="metadata-list">
                {#each runtimeFields as [key, label]}<dt>{label}</dt>
                  <dd class="identifier">{executionText(execution, key)}</dd>{/each}
                <dt>완료 이벤트 관측</dt>
                <dd>{execution.completed_event === true ? '관측됨' : '미확인'}</dd>
                <dt>웹 검색 허용 설정</dt>
                <dd>
                  {execution.allow_web_search === true
                    ? '허용'
                    : execution.allow_web_search === false
                      ? '허용 안 함'
                      : '미확인'}
                </dd>
              </dl>{:else}<p class="muted">저장된 프로세스 실행 정보가 없습니다.</p>{/if}
            <dl class="metadata-list">
              <dt>선택 계좌 ID</dt>
              <dd class="identifier">
                {detail.investigation.context_input.snapshot_id ?? '선택 없음'}
              </dd>
              <dt>현재 입력 ID</dt>
              <dd class="identifier">{detail.investigation.context_input.input_id}</dd>
              <dt>시장 원자료</dt>
              <dd>{detail.investigation.context_input.capture_ids.length}개</dd>
              <dt>연구 근거</dt>
              <dd>{detail.investigation.context_input.evidence_ids.length}개</dd>
              <dt>관심 종목</dt>
              <dd>{detail.investigation.context_input.symbols.join(', ') || '종목 제한 없음'}</dd>
            </dl>
            {#each detail.investigation.revisions as revision}<p class="muted">
                버전 {revision.number} · {revision.completed_at
                  ? `결과 저장 ${formatTime(revision.completed_at)}`
                  : '결과 미확인'} · 입력 {shortId(revision.input_sha256)}
              </p>{/each}{#if detail.investigation.omitted_revision_count}<p class="muted">
                과거 버전 {detail.investigation.omitted_revision_count}개 생략
              </p>{/if}
          </details>
          <div class="investigation-review-actions">
            <Button variant="outline" onclick={prepareReview} disabled={busy || unresolved}
              >입력을 정해 재검토</Button
            >{#if detail.investigation.status !== 'paused'}<Button
                variant="outline"
                onclick={pause}
                disabled={busy || unresolved}>조사 일시 정지</Button
              >{/if}
          </div>
          {#if reviewInput && reviewId === detail.investigation.id}
            <form class="investigation-review" onsubmit={revise}>
              <h3>버전 {reviewInput.expected_revision}을 기준으로 재검토</h3>
              <p class="muted">
                기존 입력을 불러왔습니다. 다음 조사에 포함할 전체 자료를 선택하세요. 일시 정지된
                조사는 다시 시작합니다.
              </p>
              <label
                >재검토 목적<textarea
                  required
                  maxlength="2000"
                  bind:value={reviewInput.purpose}
                  disabled={busy || unresolved}></textarea></label
              ><label
                >재검토 관심 종목<input
                  bind:value={reviewSymbols}
                  disabled={busy || unresolved}
                /></label
              >
              <div class="investigation-review-actions">
                <span class="muted"
                  >계좌 {reviewInput.snapshot_id
                    ? shortId(reviewInput.snapshot_id)
                    : '선택 없음'}</span
                ><Button
                  type="button"
                  variant="outline"
                  disabled={busy || unresolved}
                  onclick={() => {
                    if (reviewInput) reviewInput.snapshot_id = selectedSnapshot || null;
                  }}>상단 선택 계좌 적용</Button
                >
              </div>
              <label
                >재검토 시장 원자료<select
                  aria-label="재검토 시장 원자료"
                  multiple
                  bind:value={reviewInput.capture_ids}
                  disabled={busy || unresolved || catalogQuery.isFetching}
                  >{#each captures as capture}<option value={capture.capture_id}
                      >{capture.symbol ?? '시장'} · {capture.interval ?? ''} · {formatTime(
                        capture.retrieved_at,
                      )} · {shortId(capture.capture_id)}</option
                    >{/each}{#each reviewStoredCaptureIds.filter((id) => !captures.some((item) => item.capture_id === id)) as id}<option
                      value={id}>목록 밖 기존 원자료 · {shortId(id)}</option
                    >{/each}</select
                ></label
              ><label
                >재검토 연구 근거<select
                  aria-label="재검토 연구 근거"
                  multiple
                  bind:value={reviewInput.evidence_ids}
                  disabled={busy || unresolved}
                  >{#each evidence as item}<option value={item.id}>{recordTitle(item)}</option
                    >{/each}{#each reviewStoredEvidenceIds.filter((id) => !evidence.some((item) => item.id === id)) as id}<option
                      value={id}>목록 밖 기존 근거 · {shortId(id)}</option
                    >{/each}</select
                ></label
              ><Button type="submit" disabled={busy || unresolved}>새 버전 조사 접수</Button>
            </form>
          {/if}
        {/if}
      </section>
    {/if}
  {/if}
</section>
