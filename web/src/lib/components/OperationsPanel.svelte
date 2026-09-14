<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { RotateCw } from '@lucide/svelte';
  import type { InvestmentContext } from '$lib/api/types.gen';
  import { Button } from '$lib/components/ui/button';
  import { fetchJobs, fetchJobsStatus } from '$lib/jobs';
  import { fetchInvestigations, investigationState } from '$lib/investigations';
  import { fetchMarketCatalog } from '$lib/market';
  import { formatTime, shortId } from '$lib/format';
  import { recordTitle } from '$lib/research';
  import { elapsedText, reviewReasonLabels, waitReasonLabels } from '$lib/operations';
  import { createPollingWindow } from '$lib/polling.svelte';
  import { ACTIVE_POLL_MS, IDLE_POLL_MS } from '$lib/polling';

  let {
    ready,
    jobsEnabled,
    context,
    contextError,
    onselect,
    onInvestigation,
  }: {
    ready: boolean;
    jobsEnabled: boolean;
    context?: InvestmentContext;
    contextError?: string;
    onselect: (id: string) => void;
    onInvestigation: (id: string) => void;
  } = $props();
  const polling = createPollingWindow();
  const statusQuery = createQuery(() => ({
    queryKey: ['jobs-status'],
    enabled: ready,
    queryFn: ({ signal }) => fetchJobsStatus(signal),
    refetchInterval: (query) =>
      polling.interval(
        query.state.status === 'success' &&
          !!query.state.data &&
          query.state.data.enabled &&
          query.state.data.database === 'reachable',
        (query.state.data?.queued_count ?? 0) + (query.state.data?.running_count ?? 0) > 0
          ? ACTIVE_POLL_MS
          : IDLE_POLL_MS,
      ),
    refetchIntervalInBackground: false,
  }));
  const jobsQuery = createQuery(() => ({
    queryKey: ['jobs'],
    enabled: ready && statusQuery.isSuccess && statusQuery.data.enabled,
    queryFn: ({ signal }) => fetchJobs(signal),
  }));
  const investigationsQuery = createQuery(() => ({
    queryKey: ['investigations'],
    enabled: ready && jobsEnabled,
    queryFn: ({ signal }) => fetchInvestigations(signal),
  }));
  const marketQuery = createQuery(() => ({
    queryKey: ['market-catalog'],
    enabled: ready,
    queryFn: ({ signal }) => fetchMarketCatalog(signal),
  }));
  let status = $derived(ready && statusQuery.isSuccess ? statusQuery.data : undefined);
  let market = $derived(ready && marketQuery.isSuccess ? marketQuery.data : undefined);
  let freshness = $derived(context?.snapshot_freshness);
  let liveWorkers = $derived(status?.workers?.filter((worker) => worker.liveness === 'live'));
  let jobs = $derived(jobsQuery.isSuccess ? jobsQuery.data.items : undefined);
  let investigations = $derived(
    ready && investigationsQuery.isSuccess ? investigationsQuery.data.items : undefined,
  );
  let activeInvestigations = $derived(
    investigations?.filter((item) => {
      if (item.status === 'paused' || !item.active_job_id) return false;
      const job = jobs?.find((job) => job.id === item.active_job_id);
      return !job || job.status === 'queued' || job.status === 'running';
    }),
  );
  const workerStateLabels = { idle: '대기 중', running: '작업 처리 중', stopped: '정상 종료' };
  const workerLifeLabels = { live: '유효', stale: '생존 관측 만료', stopped: '종료' };

  async function refresh() {
    polling.restart();
    await statusQuery.refetch();
    if (jobsEnabled) await Promise.allSettled([jobsQuery.refetch(), investigationsQuery.refetch()]);
  }

  function openReview(id: string) {
    onselect(id);
    document.getElementById('record-detail')?.scrollIntoView({ block: 'start' });
  }
</script>

<section class="panel operations-panel" aria-labelledby="operations-heading">
  <div class="operations-heading">
    <div>
      <h2 id="operations-heading">지금 확인할 상태</h2>
      <p class="muted">자료의 시점과 작업이 기다리는 이유를 확인합니다.</p>
    </div>
    <Button variant="outline" onclick={refresh} disabled={!ready || statusQuery.isFetching}
      ><RotateCw size={14} aria-hidden="true" />운영 상태 다시 확인</Button
    >
  </div>
  <dl class="operations-status-grid">
    <div>
      <dt>웹 API</dt>
      <dd>{ready ? '응답 확인' : '연결 미확인'}</dd>
    </div>
    <div>
      <dt>작업 API 설정</dt>
      <dd>{status ? (status.enabled ? '활성' : '비활성') : '미확인'}</dd>
    </div>
    <div>
      <dt>작업 DB 접근</dt>
      <dd>
        {status?.database === 'reachable'
          ? '접근 확인'
          : status?.database === 'unavailable'
            ? '접근 실패'
            : status?.database === 'not_checked'
              ? '확인하지 않음'
              : '미확인'}
      </dd>
    </div>
    <div>
      <dt>같은 작업실의 worker</dt>
      <dd>
        {status?.database === 'reachable' && liveWorkers
          ? `${liveWorkers.length}개 생존 관측 유효${status.workers_truncated ? ' · 일부 목록' : ''}`
          : '미확인'}
      </dd>
    </div>
  </dl>
  {#if statusQuery.isError}<p class="error-state" role="alert">
      운영 상태를 확인하지 못했습니다. {statusQuery.error.message}
    </p>{/if}
  {#if status?.database === 'unavailable'}<p class="warning-state">
      작업 DB에 접근하지 못했습니다. 저장된 계좌·시장·연구 자료는 계속 읽을 수 있습니다.
    </p>{/if}
  <div class="operations-summary-grid">
    <section aria-label="계좌 자료 신선도">
      <h3>선택 계좌 관측</h3>
      {#if contextError}<p class="error-state">계좌 신선도 미확인 · {contextError}</p>
      {:else if freshness?.status === 'not_selected'}<p>계좌 관측을 먼저 선택하세요.</p>
      {:else if freshness}<p class:warning-state={freshness.status !== 'fresh'}>
          <strong
            >{freshness.status === 'fresh'
              ? '최근 저장 관측'
              : freshness.status === 'stale'
                ? '오래된 저장 관측'
                : '조회 기준 이후 관측'}</strong
          >
        </p>
        <p class="muted">
          수집 완료 {formatTime(context?.account?.snapshot.collection_completed_at)} · 경과 {elapsedText(
            freshness.completed_age_seconds,
          )}
        </p>
        <p class="muted">
          가장 오래된 관측 경과 {elapsedText(freshness.oldest_observation_age_seconds)} · 기존 기준 {elapsedText(
            freshness.max_age_seconds,
          )}
        </p>
      {:else}<p class="muted">선택 계좌의 신선도를 확인하고 있습니다.</p>{/if}
      <a href="#account-heading">계좌 관측 상세</a>
    </section>
    <section aria-label="시장 자료 신선도">
      <h3>시장 자료 관측</h3>
      {#if marketQuery.isError}<p class="error-state">시장 관측 시각 미확인</p>
      {:else if market?.observation_age?.observed_at}<p>
          최근 수집 {formatTime(market.observation_age.observed_at)}
        </p>
        <p class="muted">
          조회 기준 경과 {elapsedText(market.observation_age.age_seconds)} · {market.observation_age
            .status === 'future'
            ? '조회 기준 이후 관측'
            : '신선도 기준 미설정'}
        </p>
        <p class="muted">
          지원 자료 중 최근 수집 · {shortId(
            market.observation_age.capture_id ?? '',
          )}{market.truncated_count ? ` · 목록 밖 ${market.truncated_count}개` : ''}
        </p>
      {:else if market}<p class="muted">현재 목록의 지원 시장 관측 미확인 · 신선도 미확인</p>
        {#if market.truncated_count}<p class="muted">목록 밖 {market.truncated_count}개</p>{/if}
      {:else}<p class="muted">시장 자료의 시각을 확인하고 있습니다.</p>{/if}
      <a href="#market-heading">종목별 저장 관측 보기</a>
    </section>
    <section aria-label="재검토 요약">
      <h3>재검토할 판단</h3>
      {#if context}<p>{context.review_queue.length}개 · 저장 문맥에서 계산</p>
        {#each context.review_queue.slice(0, 3) as review}<button
            class="text-button operations-review"
            aria-label={`재검토 판단 상세 ${shortId(review.decision_id)}`}
            onclick={() => openReview(review.decision_id)}
            >{context.records.find((item) => item.id === review.decision_id)
              ? recordTitle(context.records.find((item) => item.id === review.decision_id)!)
              : `판단 ${shortId(review.decision_id)}`}<small
              >{review.reasons.map((reason) => reviewReasonLabels[reason] ?? reason).join(' · ')} · {formatTime(
                review.review_after,
              )}</small
            ></button
          >{/each}
        {#if context.review_queue.length > 3}<a href="#research-heading">전체 재검토 목록 보기</a
          >{/if}
      {:else}<p class="muted">재검토 판단 미확인</p>{/if}
    </section>
  </div>
  <div class="operations-work">
    <div>
      <h3>현재 조사와 작업</h3>
      <p>
        실행 중 {status?.running_count == null ? '미확인' : `${status.running_count}개`} · 대기 {status?.queued_count ==
        null
          ? '미확인'
          : `${status.queued_count}개`}
      </p>
    </div>
    <a href="#jobs-panel">작업과 대기 이유 보기</a>
  </div>
  {#if activeInvestigations?.length}<ul class="operations-investigations">
      {#each activeInvestigations.slice(0, 3) as item}<li>
          <button class="text-button" onclick={() => onInvestigation(item.id)}
            >{item.context_input.purpose}</button
          ><span class="muted"
            >{investigationState(
              item,
              jobs?.find((job) => job.id === item.active_job_id),
            )} · 버전 {item.current_revision}</span
          >
        </li>{/each}
    </ul>
  {:else if investigations}<p class="muted">최근 조사 목록에 실행·대기 조사가 없습니다.</p>
  {:else}<p class="muted">
      {jobsEnabled ? '현재 조사 목록 미확인' : '조사 조회·실행이 비활성입니다.'}
    </p>{/if}
  {#if investigations}<p class="muted">
      최신 조사 최대 50개 중 요약 · <a href="#investigations-panel">조사 목록 보기</a>
    </p>{/if}
  {#if status?.waiting_jobs?.length}<ul class="operations-waiting">
      {#each status.waiting_jobs.slice(0, 3) as waiting}<li>
          <span class="identifier">{shortId(waiting.job_id)}</span> · {waiting.reasons
            .map((reason) => waitReasonLabels[reason] ?? reason)
            .join(' · ')}
        </li>{/each}
    </ul>{/if}
  {#if status?.waiting_jobs_truncated}<p class="muted">대기 이유는 일부 작업만 표시합니다.</p>{/if}
  {#if status?.workers?.length}<details class="subtle-details operations-workers">
      <summary>worker별 관측 시각과 허용 설정</summary>
      <ul>
        {#each status.workers as worker}<li>
            <strong>{worker.owner} · {workerLifeLabels[worker.liveness]}</strong>
            <p>
              {workerStateLabels[worker.state]} · Codex {worker.allow_codex
                ? '허용 설정'
                : '허용 안 함'} · 토스 자료 수집 {worker.allow_network ? '허용 설정' : '허용 안 함'}
            </p>
            <p>
              Codex 웹 검색 {worker.codex_web_search_allowed === true
                ? '허용 설정'
                : worker.codex_web_search_allowed === false
                  ? '허용 안 함'
                  : '미확인'}
            </p>
            <p>
              생존 관측 {formatTime(worker.heartbeat_at)} · 유효 시각 {formatTime(
                worker.expires_at,
              )}
            </p>
            <p>
              실행 세션 {shortId(worker.id)}{worker.current_job_id
                ? ` · 작업 ${shortId(worker.current_job_id)}`
                : ''}
            </p>
          </li>{/each}
      </ul>
    </details>{/if}
  <p class="operations-boundary muted">
    worker의 생존·허용 설정은 로그인이나 외부 API 성공을 확인한 결과가 아닙니다. 실제 주문 전송은
    비활성입니다.
  </p>
  {#if status?.checked_at}<p class="muted">
      운영 상태 조회 {formatTime(status.checked_at)} · 활성 작업 5초 / 그 외 30초 간격, 화면이 보이는
      동안 최대 5분 자동 확인
    </p>{/if}
</section>
