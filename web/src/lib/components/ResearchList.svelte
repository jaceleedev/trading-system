<script lang="ts">
  import { Search, ChevronRight } from '@lucide/svelte';
  import { Input } from '$lib/components/ui/input';
  import type { InvestmentContext } from '$lib/api/types.gen';
  import {
    filterRecords,
    kindLabels,
    recordTitle,
    actionLabels,
    type RecordKind,
  } from '$lib/research';
  import { formatTime } from '$lib/format';
  let {
    context,
    loading = false,
    error,
    selectedId,
    onselect,
    maxRecords,
    onlimit,
  }: {
    context?: InvestmentContext;
    loading?: boolean;
    error?: string;
    selectedId: string | null;
    onselect: (id: string) => void;
    maxRecords: number;
    onlimit: (value: number) => void;
  } = $props();
  let search = $state('');
  let kind = $state<RecordKind | 'all'>('all');
  let filtered = $derived(filterRecords(context?.records ?? [], search, kind));
</script>

<section class="panel research-panel" aria-labelledby="research-heading" aria-busy={loading}>
  <div class="panel-heading">
    <h2 id="research-heading">판단과 근거</h2>
    <span class="muted record-count">{context?.exported_record_count ?? 0}개 기록</span>
  </div>
  <div class="research-filters">
    <div class="search-field">
      <Search size={18} aria-hidden="true" /><Input
        aria-label="기록 검색"
        placeholder="기록 검색"
        bind:value={search}
        class="search-input"
      />
    </div>
    <select aria-label="기록 종류" bind:value={kind}
      ><option value="all">전체 종류</option
      >{#each Object.entries(kindLabels) as [value, label]}<option {value}>{label}</option
        >{/each}</select
    >
  </div>
  {#if loading}<div class="empty-state" role="status">판단과 근거를 읽고 있습니다.</div>
  {:else if error}<div class="empty-state error-state" role="alert">{error}</div>
  {:else if !context?.records.length}<div class="empty-state">
      <strong>저장된 연구 기록이 없습니다.</strong>
      <p>Codex에서 조사와 판단을 기록하면 이곳에서 이어 볼 수 있습니다.</p>
    </div>
  {:else if !filtered.length}<div class="empty-state">
      <strong>검색 결과가 없습니다.</strong>
      <p>검색어나 기록 종류를 바꿔 주세요.</p>
    </div>
  {:else}
    <div class="record-list">
      {#each filtered as item (item.id)}
        <button
          class="record-row"
          class:selected={selectedId === item.id}
          aria-pressed={selectedId === item.id}
          onclick={() => onselect(item.id)}
        >
          <span class="kind-tag">{kindLabels[item.record.kind]}</span>
          <span class="record-row-content"
            ><strong>{recordTitle(item)}</strong><span class="muted">
              {#if item.record.kind === 'decision'}{[
                  ...new Set(
                    item.record.payload.proposed_actions.map(
                      (action) => actionLabels[action.action],
                    ),
                  ),
                ].join(' · ')} ·
              {/if}
              {#if item.record.mode === 'synthetic'}합성 시연 ·
              {:else if item.record.mode === 'retrospective'}사후 분석 ·
              {/if}
              <time datetime={item.record.recorded_at} title={item.record.recorded_at}
                >{formatTime(item.record.recorded_at)}</time
              >
            </span></span
          >
          <ChevronRight size={18} aria-hidden="true" />
        </button>
      {/each}
    </div>
  {/if}
  {#if context && (context.truncated_count || context.future_record_count || context.omitted_references.length)}
    <div class="omission-note">
      <p>
        현재 {context.exported_record_count} / {context.eligible_record_count}개 표시 · 생략 {context.truncated_count}개
        · 미래 시각 제외 {context.future_record_count}개
      </p>
      {#if context.omitted_references.length}<p>
          목록 밖 연결 기록 {context.omitted_references.length}개는 상세에서 읽을 수 있습니다.
        </p>{/if}
      {#if maxRecords < 100 && context.truncated_count}<button
          class="text-button"
          onclick={() => onlimit(100)}>최근 100개까지 읽기</button
        >{/if}
    </div>
  {/if}
  {#if context?.review_queue.length}
    <details class="review-queue subtle-details">
      <summary>재검토할 판단 {context.review_queue.length}개</summary>
      {#each context.review_queue as review}<button
          class="queue-link text-button"
          onclick={() => onselect(review.decision_id)}
          >{context.records.find((item) => item.id === review.decision_id)
            ? recordTitle(context.records.find((item) => item.id === review.decision_id)!)
            : `판단 ${review.decision_id.slice(0, 10)}`}<span class="muted"
            >{formatTime(review.review_after)}</span
          ></button
        >{/each}
    </details>
  {/if}
</section>
