<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import { formatTime, shortId } from '$lib/format';
  import {
    cancelJob,
    fetchJob,
    fetchJobs,
    fetchJobsStatus,
    JobRequestError,
    submitResearchContext,
    type ResearchContextSubmission,
  } from '$lib/jobs';

  let { selectedSnapshot = '', ready = false }: { selectedSnapshot?: string; ready?: boolean } =
    $props();
  let scheduledAt = $state('');
  let submitting = $state(false);
  let cancellingId = $state<string | null>(null);
  let submission = $state<ResearchContextSubmission | null>(null);
  let feedback = $state('');
  let actionError = $state('');
  let selectedJobId = $state<string | null>(null);

  const statusQuery = createQuery(() => ({
    queryKey: ['jobs-status'],
    enabled: ready,
    queryFn: ({ signal }) => fetchJobsStatus(signal),
  }));
  const jobsQuery = createQuery(() => ({
    queryKey: ['jobs'],
    enabled: ready && statusQuery.isSuccess && statusQuery.data.enabled,
    queryFn: ({ signal }) => fetchJobs(signal),
  }));
  const detailQuery = createQuery(() => {
    const id = selectedJobId;
    return {
      queryKey: ['job', id],
      enabled: ready && statusQuery.isSuccess && statusQuery.data.enabled && !!id,
      queryFn: ({ signal }) => fetchJob(id!, signal),
    };
  });
  let enabled = $derived(ready && statusQuery.isSuccess && statusQuery.data.enabled);
  let jobs = $derived(
    enabled && jobsQuery.isSuccess && !jobsQuery.isFetching ? jobsQuery.data.items : undefined,
  );
  let refreshing = $derived(statusQuery.isFetching || jobsQuery.isFetching);
  let detail = $derived(
    jobs &&
      detailQuery.isSuccess &&
      !detailQuery.isFetching &&
      detailQuery.data.job.id === selectedJobId
      ? detailQuery.data.job
      : undefined,
  );
  let canSubmit = $derived(
    enabled && jobsQuery.isSuccess && !refreshing && !submitting && !cancellingId,
  );
  const statusLabels = {
    queued: '대기',
    running: '실행 중',
    succeeded: '완료',
    failed: '실패',
    cancelled: '취소됨',
  };
  const kindLabels: Record<string, string> = {
    'research-context': '저장 자료 검증',
    'account-sync': '계좌 관측 수집',
    'market-capture': '시장 자료 수집',
    'broker-sync': '브로커 주문 관측 수집',
  };

  async function refreshJobs() {
    feedback = '';
    actionError = '';
    const status = await statusQuery.refetch();
    if (status.isSuccess && status.data.enabled) {
      const refreshed = await jobsQuery.refetch();
      if (
        submission &&
        refreshed.isSuccess &&
        refreshed.data.items.some((job) => job.request_key === submission?.request_key)
      ) {
        submission = null;
        scheduledAt = '';
        feedback = '접수된 검증 작업을 확인했습니다.';
      }
      if (selectedJobId) await detailQuery.refetch();
    }
  }

  async function submit() {
    if (!canSubmit) return;
    actionError = '';
    feedback = '';
    if (!submission) {
      let availableAt: string | undefined;
      if (scheduledAt) {
        const date = new Date(scheduledAt);
        if (Number.isNaN(date.getTime())) {
          actionError = '예약 시각을 확인해 주세요.';
          return;
        }
        availableAt = date.toISOString();
      }
      submission = {
        kind: 'research-context',
        parameters: { snapshot_id: selectedSnapshot || null, max_records: 50 },
        request_key: crypto.randomUUID(),
        max_attempts: 3,
        ...(availableAt ? { available_at: availableAt } : {}),
      };
    }
    submitting = true;
    try {
      await submitResearchContext(submission);
      submission = null;
      scheduledAt = '';
      feedback = '저장 자료 검증 작업을 접수했습니다.';
      await jobsQuery.refetch();
    } catch (error) {
      if (
        error instanceof JobRequestError &&
        ['invalid_request', 'job_conflict', 'jobs_disabled', 'foreign_origin'].includes(error.code)
      ) {
        submission = null;
      }
      actionError =
        error instanceof Error ? error.message : '작업 접수 결과를 확인하지 못했습니다.';
    } finally {
      submitting = false;
    }
  }

  async function cancel(id: string) {
    if (cancellingId || submitting || !enabled) return;
    cancellingId = id;
    feedback = '';
    actionError = '';
    try {
      const result = await cancelJob(id);
      feedback =
        result.job.status === 'cancelled'
          ? '작업이 취소됐습니다.'
          : result.job.cancel_requested
            ? '취소를 요청했습니다. 실행 중인 작업의 상태를 다시 확인해 주세요.'
            : '작업 상태가 변경됐습니다. 최신 목록을 확인해 주세요.';
      await jobsQuery.refetch();
      if (selectedJobId === id) await detailQuery.refetch();
    } catch (error) {
      actionError =
        error instanceof Error ? error.message : '취소 요청 결과를 확인하지 못했습니다.';
    } finally {
      cancellingId = null;
    }
  }
</script>

<section class="panel jobs-panel" aria-labelledby="jobs-heading" aria-busy={refreshing}>
  <div class="panel-heading jobs-heading">
    <div>
      <h2 id="jobs-heading">작업 실행</h2>
      <p class="muted">저장 자료를 검증하고 작업의 진행 상태를 확인합니다.</p>
    </div>
    <Button
      variant="outline"
      class="jobs-refresh"
      onclick={refreshJobs}
      disabled={!ready || refreshing || submitting || !!cancellingId}
      ><RotateCw size={14} aria-hidden="true" class={refreshing ? 'reading-icon' : ''} />작업 목록
      다시 읽기</Button
    >
  </div>
  {#if !ready || statusQuery.isPending}
    <div class="jobs-empty" role="status">작업 기능의 상태를 확인하고 있습니다.</div>
  {:else if statusQuery.isError}
    <div class="jobs-empty error-state" role="alert">{statusQuery.error.message}</div>
  {:else if !enabled}
    <div class="jobs-empty">
      <strong>이 작업실에서는 작업 실행이 꺼져 있습니다.</strong>
      <p>저장된 계좌 관측과 연구 기록은 계속 조회할 수 있습니다.</p>
    </div>
  {:else}
    <div class="jobs-submit">
      <div class="jobs-submit-description">
        <h3>저장 자료 검증</h3>
        <p class="muted">
          {selectedSnapshot
            ? `선택한 계좌 관측 ${shortId(selectedSnapshot)}와 연구 기록`
            : '계좌 관측을 선택하지 않고 연구 기록 검증'}
        </p>
        <p class="muted">AI 판단이나 주문을 실행하지 않습니다.</p>
      </div>
      <label class="jobs-schedule">
        <span>예약 시각 <span class="muted">선택 · 기기 현지 시각</span></span>
        <input
          type="datetime-local"
          bind:value={scheduledAt}
          disabled={submitting || !!submission}
        />
      </label>
      <Button onclick={submit} disabled={!canSubmit} class="jobs-submit-button"
        >{submitting
          ? '접수 확인 중'
          : submission
            ? '같은 요청 다시 확인'
            : '검증 작업 접수'}</Button
      >
    </div>
    {#if submission && !submitting}
      <p class="inline-notice warning-state">
        접수 여부가 아직 확인되지 않았습니다. 같은 요청을 다시 확인하면 새 작업을 중복 접수하지
        않습니다. 원래 요청:
        {submission.parameters.snapshot_id
          ? `계좌 관측 ${shortId(submission.parameters.snapshot_id)}`
          : '계좌 선택 없이 연구 기록 검증'}.
      </p>
    {/if}
    {#if feedback}<p class="jobs-feedback" role="status">{feedback}</p>{/if}
    {#if actionError}<p class="jobs-feedback error-state" role="alert">{actionError}</p>{/if}
    {#if jobsQuery.isFetching}
      <div class="jobs-empty" role="status">작업 목록을 읽고 있습니다.</div>
    {:else if jobsQuery.isError}
      <div class="jobs-empty error-state" role="alert">{jobsQuery.error.message}</div>
    {:else if !jobs?.length}
      <div class="jobs-empty"><strong>등록된 작업이 없습니다.</strong></div>
    {:else}
      <div class="table-container jobs-table-container">
        <table class="jobs-table">
          <caption class="sr-only">등록된 작업의 상태와 실행 시도, 예약 시각</caption>
          <thead
            ><tr
              ><th scope="col">작업</th><th scope="col">상태</th><th scope="col">실행 시도</th><th
                scope="col">예약 · 최근 변경</th
              ><th scope="col">작업 제어</th></tr
            ></thead
          >
          <tbody>
            {#each jobs as job (job.id)}
              <tr data-testid={`job-${job.id}`}>
                <th scope="row">
                  <strong>{kindLabels[job.kind] ?? job.kind}</strong>
                  <small class="identifier" title={job.id}>{shortId(job.id)}</small>
                  {#if typeof job.parameters.snapshot_id === 'string'}
                    <small title={job.parameters.snapshot_id}
                      >계좌 관측 {shortId(job.parameters.snapshot_id)}</small
                    >
                  {/if}
                  {#if job.error_code}<small class="error-state">오류: {job.error_code}</small>{/if}
                </th>
                <td>
                  <span
                    class:job-success={job.status === 'succeeded'}
                    class:job-failed={job.status === 'failed'}>{statusLabels[job.status]}</span
                  >
                  {#if job.cancel_requested && ['queued', 'running'].includes(job.status)}
                    <small class="job-cancel-requested">취소 요청됨</small>
                  {/if}
                </td>
                <td class="numeric">
                  {job.attempt_count} / {job.max_attempts}회
                  {#if job.attempt_count > 0}<button
                      class="text-button jobs-attempt-button"
                      onclick={() => {
                        selectedJobId = job.id;
                      }}>시도 기록</button
                    >{/if}
                </td>
                <td class="jobs-times">
                  <time datetime={job.available_at} title={job.available_at}
                    >{formatTime(job.available_at)}</time
                  >
                  <small title={job.updated_at}>변경 {formatTime(job.updated_at)}</small>
                </td>
                <td>
                  {#if ['queued', 'running'].includes(job.status) && !job.cancel_requested}
                    <Button
                      variant="outline"
                      size="sm"
                      onclick={() => cancel(job.id)}
                      disabled={!!cancellingId || submitting}
                      aria-label={`${shortId(job.id)} 작업 취소 요청`}
                    >
                      {cancellingId === job.id ? '취소 요청 중' : '취소 요청'}
                    </Button>
                  {:else}<span class="muted">—</span>{/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
      <p class="muted jobs-count">
        최근 작업 {jobs.length}개 · 상태 확인은 목록 다시 읽기를 사용합니다.
      </p>
    {/if}
    {#if selectedJobId && jobs}
      <section class="jobs-attempt-detail" aria-label="선택한 작업의 실행 시도">
        <div class="jobs-attempt-heading">
          <h3>실행 시도 · {shortId(selectedJobId)}</h3>
          <button
            class="text-button"
            onclick={() => {
              selectedJobId = null;
            }}>시도 기록 닫기</button
          >
        </div>
        {#if detailQuery.isFetching}<p class="muted" role="status">실행 시도를 읽고 있습니다.</p>
        {:else if detailQuery.isError}<p class="error-state" role="alert">
            {detailQuery.error.message}
          </p>
        {:else if detail}
          {#if detail.attempts?.length}
            <ol class="jobs-attempts">
              {#each detail.attempts ?? [] as attempt}
                <li>
                  <strong
                    >{attempt.number}회 · {statusLabels[
                      attempt.status as keyof typeof statusLabels
                    ] ?? attempt.status}</strong
                  >
                  <span title={attempt.started_at}>시작 {formatTime(attempt.started_at)}</span>
                  {#if attempt.finished_at}<span title={attempt.finished_at}
                      >종료 {formatTime(attempt.finished_at)}</span
                    >{/if}
                  {#if attempt.error_code}<span class="error-state">오류: {attempt.error_code}</span
                    >{/if}
                </li>
              {/each}
            </ol>
          {:else}<p class="muted">아직 실행 시도가 없습니다.</p>{/if}
        {/if}
      </section>
    {/if}
  {/if}
</section>
