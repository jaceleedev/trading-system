<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { untrack } from 'svelte';
  import { Button } from '$lib/components/ui/button';
  import { fetchInvestigations } from '$lib/investigations';
  import { formatTime, shortId } from '$lib/format';
  import { modeLabels } from '$lib/research';
  import {
    fetchGuidedFlow,
    guidedKey,
    changeGuidedSelection,
    type GuidedStage,
    type GuidedResolution,
  } from '$lib/guided';
  import type { GuidedSelection } from '$lib/api/types.gen';

  let {
    ready,
    jobsEnabled,
    selection,
    stage,
    onChange,
    onResolved,
    onOpen,
  }: {
    ready: boolean;
    jobsEnabled: boolean;
    selection: GuidedSelection;
    stage: GuidedStage;
    onChange: (selection: GuidedSelection, stage: GuidedStage) => void;
    onResolved: (key: string, value: GuidedResolution | null) => void;
    onOpen: (stage: GuidedStage) => void;
  } = $props();
  let typedInvestigation = $state('');
  const investigations = createQuery(() => ({
    queryKey: ['investigations'],
    enabled: ready && jobsEnabled,
    queryFn: ({ signal }) => fetchInvestigations(signal),
  }));
  const resolution = createQuery(() => {
    const requested = { ...selection };
    const key = guidedKey(requested, stage);
    return {
      queryKey: ['guided-flow', key],
      enabled: ready,
      queryFn: async ({ signal }: { signal: AbortSignal }) => ({
        key,
        data: await fetchGuidedFlow(requested, signal),
      }),
      retry: false,
    };
  });
  let current = $derived(
    ready &&
      resolution.isSuccess &&
      !resolution.isFetching &&
      resolution.data.key === guidedKey(selection, stage)
      ? resolution.data.data
      : undefined,
  );
  let chosen = $derived(current?.selection);
  let plan = $derived(current?.plans.find((item) => item.id === chosen?.plan_id));
  let book = $derived(current?.books.find((item) => item.id === chosen?.book_id));
  $effect(() => {
    const key = guidedKey(selection, stage);
    const value = current ?? null;
    untrack(() => onResolved(key, value));
  });
  function choose(
    field: Parameters<typeof changeGuidedSelection>[1],
    value: string | number | null,
  ) {
    onChange(changeGuidedSelection(chosen ?? selection, field, value), stage);
  }
  function revision(value: string) {
    const item = current?.investigation?.revisions.find((item) => String(item.number) === value);
    const next = changeGuidedSelection(chosen ?? selection, 'revision', item?.number ?? null);
    next.output_id = item?.output_id ?? null;
    onChange(next, 'investigations');
  }
</script>

<section id="guided-flow" class="panel guided-panel" aria-label="투자 단계 이어가기">
  <div class="guided-heading">
    <div>
      <h2>투자 단계 이어가기</h2>
      <p class="muted">선택한 조사와 자료를 이어 읽고, 다음 작업의 입력을 확인합니다.</p>
    </div>
    <Button
      variant="outline"
      size="sm"
      disabled={!ready || resolution.isFetching}
      onclick={() => resolution.refetch()}>연결 다시 확인</Button
    >
  </div>
  <nav class="guided-steps" aria-label="투자 단계">
    {#each [['investigations', '1. 조사'], ['capital', '2. 계획·대안'], ['paper', '3. 모의 원장'], ['outcomes', '4. 기간 결과']] as [value, label]}
      <button
        type="button"
        aria-current={stage === value ? 'step' : undefined}
        onclick={() => onChange({ ...selection }, value as GuidedStage)}>{label}</button
      >
    {/each}
  </nav>
  {#if !selection.snapshot_id}<p class="market-notice">
      상단에서 계좌 관측을 명시적으로 선택해 주세요. 과거 자료를 열어도 상단 계좌를 자동 변경하지
      않습니다.
    </p>{/if}
  <div class="guided-fields">
    <label
      >이어갈 조사<select
        aria-label="이어갈 조사"
        value={selection.investigation_id ?? ''}
        disabled={!ready || investigations.isFetching}
        onchange={(event) => choose('investigation_id', event.currentTarget.value)}
      >
        <option value="">조사 선택</option>
        {#if selection.investigation_id && !investigations.data?.items.some((item) => item.id === selection.investigation_id)}<option
            value={selection.investigation_id}>{shortId(selection.investigation_id)}</option
          >{/if}
        {#each investigations.isSuccess ? investigations.data.items : [] as item}<option
            value={item.id}>{item.context_input.purpose} · {shortId(item.id)}</option
          >{/each}
      </select></label
    >
    <form
      class="guided-id"
      onsubmit={(event) => {
        event.preventDefault();
        choose('investigation_id', typedInvestigation.trim());
      }}
    >
      <label
        >조사 ID 직접 입력<input
          aria-label="조사 ID 직접 입력"
          bind:value={typedInvestigation}
          placeholder="목록에 없는 저장 조사 ID"
        /></label
      ><Button type="submit" variant="outline" disabled={!ready || !typedInvestigation.trim()}
        >선택한 조사 연결</Button
      >
    </form>
  </div>
  {#if resolution.isFetching}<p role="status" class="muted">
      선택한 자료의 출처와 계좌 연결을 확인하고 있습니다.
    </p>
  {:else if resolution.isError}<p role="alert" class="error-state">{resolution.error.message}</p>
  {:else if current}
    {#each current.issues as issue}<p class="market-notice" role="status">{issue.message}</p>{/each}
    {#if current.investigation}<div class="guided-fields">
        <label
          >이어갈 조사 버전<select
            aria-label="이어갈 조사 버전"
            value={chosen?.revision ?? ''}
            onchange={(event) => revision(event.currentTarget.value)}
          >
            <option value="">결과를 확인할 버전 선택</option>
            {#each current.investigation.revisions as item}<option value={item.number}
                >버전 {item.number} · {item.output_id ? '저장 결과 있음' : '결과 없음'}</option
              >{/each}
          </select></label
        >
        <div class="guided-context">
          <strong>{current.investigation.purpose}</strong>
          <p>
            현재 조사 버전 {current.investigation.current_revision} · 선택 버전 {chosen?.revision ??
              '미선택'}
          </p>
        </div>
      </div>{/if}
    {#if current.context.frozen_snapshot_id}<p class="muted">
        선택 자료의 고정 계좌 관측 {shortId(current.context.frozen_snapshot_id)} · 계좌 {current
          .context.account_seq ?? '미확인'} · {current.context.mode
          ? modeLabels[current.context.mode]
          : '모드 미확인'} · 통화 {(current.context.currencies ?? []).join(' · ') || '미확인'}.
        상단의 최신 관측으로 바꾸지 않습니다.
      </p>{/if}
    {#if current.investigation?.output}<div class="guided-output" aria-label="이어갈 조사 결과">
        <h3>선택 버전 {chosen?.revision}의 저장 결과</h3>
        <p>{current.investigation.output.summary}</p>
        <p>{current.investigation.output.rationale}</p>
        <small class="identifier">출력 {chosen?.output_id}</small>
      </div>{/if}
    <div class="guided-fields">
      <label
        >연결된 자금 계획<select
          aria-label="연결된 자금 계획"
          value={chosen?.plan_id ?? ''}
          disabled={!current.plans.length}
          onchange={(event) => choose('plan_id', event.currentTarget.value)}
        >
          <option value="">계획 선택</option>{#each current.plans as item}<option value={item.id}
              >{shortId(item.id)} · 기록 {formatTime(item.recorded_at)}</option
            >{/each}
        </select></label
      >
      <label
        >이어갈 대안<select
          aria-label="이어갈 대안"
          value={chosen?.alternative_id ?? ''}
          disabled={!plan}
          onchange={(event) => choose('alternative_id', event.currentTarget.value)}
        >
          <option value="">대안 선택</option>{#each plan?.alternatives ?? [] as item}<option
              value={item.key}>{item.label} · {item.currencies.join(' · ') || '통화 미확인'}</option
            >{/each}
        </select></label
      >
      <label
        >이어갈 모의 원장<select
          aria-label="이어갈 모의 원장"
          value={chosen?.book_id ?? ''}
          disabled={!current.books.length}
          onchange={(event) => choose('book_id', event.currentTarget.value)}
        >
          <option value="">원장 선택</option>{#each current.books as item}<option value={item.id}
              >{item.label} · {item.linked ? '선택 대안 연결됨' : '대안 선택 필요'}</option
            >{/each}
        </select></label
      >
      <label
        >연결된 기간 보고서<select
          aria-label="연결된 기간 보고서"
          value={chosen?.report_id ?? ''}
          disabled={!current.reports.length}
          onchange={(event) => choose('report_id', event.currentTarget.value)}
        >
          <option value="">보고서 선택</option>{#each current.reports as item}<option
              value={item.id}
              >{formatTime(item.start_at)} → {formatTime(item.end_at)} · {shortId(item.id)}</option
            >{/each}
        </select></label
      >
    </div>
    {#if chosen?.output_id && !current.plans.length}<p class="muted">
        연결된 계획이 없습니다. 계획 입력에서 예산과 대안을 직접 정해 주세요.
      </p>{/if}
    {#if chosen?.plan_id && !chosen.alternative_id}<p class="muted">
        모의 원장으로 이어갈 대안을 선택해 주세요.
      </p>{/if}
    {#if chosen?.alternative_id && !current.books.length}<p class="muted">
        이어갈 원장이 없습니다. 모의 원장 입력에서 초기 현금을 직접 정해 주세요.
      </p>{/if}
    {#if book && !book.linked}<p class="muted">
        이 원장에는 선택한 대안이 아직 연결되지 않았습니다. 모의 입력에서 체결 가정을 확인하고
        대안을 명시적으로 선택해 주세요.
      </p>{/if}
    {#if chosen?.book_id && !current.reports.length}<p class="muted">
        연결된 기간 보고서가 없습니다. 기간 결과 입력에서 비교 기간을 정해 계산할 수 있습니다.
      </p>{/if}
    <div class="capital-actions guided-actions">
      <Button variant="outline" onclick={() => onOpen('investigations')}>조사 입력 열기</Button>
      <Button variant="outline" disabled={!chosen?.output_id} onclick={() => onOpen('capital')}
        >계획·대안으로 이동</Button
      >
      <Button
        variant="outline"
        disabled={!chosen?.plan_id || !chosen?.alternative_id}
        onclick={() => onOpen('paper')}>모의 원장으로 이동</Button
      >
      <Button
        variant="outline"
        disabled={!chosen?.book_id || (!!chosen?.plan_id && !book?.linked)}
        onclick={() => onOpen('outcomes')}>기간 결과로 이동</Button
      >
    </div>
    <p class="muted">
      단계 이동은 자료 조회와 입력 연결입니다. 예산 반영·자금 배정·모의 실행·결과 계산은 각 화면에서
      직접 실행합니다.
    </p>
  {/if}
</section>

<style>
  .guided-panel {
    padding: 1.4rem;
    margin-bottom: 1.5rem;
  }
  .guided-heading {
    display: flex;
    justify-content: space-between;
    align-items: start;
    gap: 1rem;
  }
  .guided-heading h2 {
    margin: 0;
    font-size: 1.15rem;
  }
  .guided-heading p {
    margin: 0.5rem 0 1rem;
  }
  .guided-steps {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.5rem;
    margin: 0.4rem 0 1.25rem;
  }
  .guided-steps button {
    padding: 0.75rem;
    border: 1px solid var(--border, #d9e1e9);
    border-radius: 0.4rem;
    text-align: left;
  }
  .guided-steps button[aria-current] {
    background: #edf4fa;
    border-color: #7ba4c8;
    font-weight: 650;
  }
  .guided-fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 1rem;
    margin: 1rem 0;
  }
  .guided-fields label {
    display: grid;
    gap: 0.5rem;
    font-size: 0.9rem;
    min-width: 0;
  }
  .guided-fields select,
  .guided-fields input {
    min-width: 0;
    width: 100%;
    border: 1px solid var(--border, #d9e1e9);
    background: white;
    border-radius: 0.35rem;
    padding: 0.6rem;
  }
  .guided-id {
    display: flex;
    align-items: end;
    gap: 0.5rem;
    min-width: 0;
  }
  .guided-id label {
    flex: 1;
  }
  .guided-context p {
    margin: 0.5rem 0;
  }
  .guided-output {
    border-left: 3px solid #aec9de;
    padding: 0.25rem 1rem;
    margin: 1rem 0;
    overflow-wrap: anywhere;
  }
  .guided-output h3 {
    font-size: 1rem;
  }
  @media (max-width: 680px) {
    .guided-heading {
      flex-direction: column;
    }
    .guided-fields {
      grid-template-columns: 1fr;
    }
    .guided-steps {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .guided-id {
      flex-direction: column;
      align-items: stretch;
    }
    .guided-panel {
      padding: 1rem;
    }
  }
</style>
