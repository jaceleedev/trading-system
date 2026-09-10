<script lang="ts">
  import { FileText } from '@lucide/svelte';
  import type { ResearchResponse } from '$lib/api/types.gen';
  import {
    actionLabels,
    judgmentLabels,
    kindLabels,
    modeLabels,
    recordLinks,
    recordTitle,
  } from '$lib/research';
  import { formatDecimal, formatTime, safeSourceUrl, shortId } from '$lib/format';

  let {
    item,
    knownRecords,
    knownSnapshotIds,
    loading,
    error,
    onselect,
    onSnapshot,
  }: {
    item?: ResearchResponse;
    knownRecords: ResearchResponse[];
    knownSnapshotIds: string[];
    loading: boolean;
    error?: string;
    onselect: (id: string) => void;
    onSnapshot: (id: string) => void;
  } = $props();
  let record = $derived(item?.record);
  let links = $derived(
    item
      ? [
          ...recordLinks(item),
          ...knownRecords
            .filter(
              (candidate) =>
                candidate.record.kind === 'review' &&
                candidate.record.payload.decision_id === item.id,
            )
            .map((candidate) => ({ id: candidate.id, label: '이 판단의 재검토' })),
        ]
      : [],
  );
  const verificationLabels = {
    user_supplied: '사용자 제공',
    provider_capture: '저장된 계좌 관측 연결',
    unverified: '독립 검증 미완료',
  };
</script>

{#snippet textList(title: string, values: string[])}
  <section class="detail-section">
    <h3>{title}</h3>
    {#if values.length}<ul class="prose-list">
        {#each values as value}<li>{value}</li>{/each}
      </ul>{:else}<p class="muted">기록된 항목이 없습니다.</p>{/if}
  </section>
{/snippet}

<section class="panel detail-panel" aria-label="연구 기록 상세" aria-busy={loading}>
  {#if loading}<div class="empty-state" role="status">연결된 연구 기록을 읽고 있습니다.</div>
  {:else if error}<div class="empty-state error-state" role="alert">{error}</div>
  {:else if !record || !item}<div class="empty-state detail-empty">
      <FileText size={26} aria-hidden="true" /><strong>판단과 근거를 이어서 읽어보세요.</strong>
      <p>위 목록에서 기록을 선택하면 판단 이유와 연결된 자료를 확인할 수 있습니다.</p>
    </div>
  {:else}
    <div class="detail-body">
      <header class="detail-heading">
        <h2>{recordTitle(item)}</h2>
        <p class="muted">
          {kindLabels[record.kind]} · {modeLabels[record.mode]}{record.kind === 'decision'
            ? ' · 제안'
            : ''}
        </p>
      </header>
      {#if record.kind === 'decision'}
        <section class="detail-section">
          <h3>판단 이유</h3>
          <p>{record.payload.rationale}</p>
        </section>
        {@render textList('비교한 대안', record.payload.alternatives)}
        <section class="detail-section">
          <h3>제안 행동</h3>
          {#each record.payload.proposed_actions as action}
            <div class="action-line">
              <strong
                >{actionLabels[action.action]}{action.symbol
                  ? ` · ${action.symbol} / ${action.market}`
                  : ''}</strong
              >
              <p>{action.rationale}</p>
              {#if action.quantity != null}<p class="muted numeric">
                  제안 수량 {formatDecimal(action.quantity)}
                </p>{/if}
              {#if action.target_weight != null}<p class="muted numeric">
                  목표 비중 {formatDecimal(action.target_weight)} (비율)
                </p>{/if}
            </div>
          {/each}
          <p class="muted">수량과 금액 검증 전의 제안입니다. 주문이나 체결 기록이 아닙니다.</p>
        </section>
        {@render textList('미해결 질문', record.payload.unresolved_questions)}
      {:else if record.kind === 'hypothesis'}
        <section class="detail-section">
          <h3>투자 가설</h3>
          <p>{record.payload.thesis}</p>
        </section>
        {@render textList('불확실성', record.payload.uncertainties)}
        {@render textList('가설을 철회할 조건', record.payload.invalidation_conditions)}
        {@render textList('다시 검토할 상황', record.payload.review_triggers)}
      {:else if record.kind === 'evidence'}
        <section class="detail-section">
          <h3>관측한 근거</h3>
          <p>{record.payload.claim}</p>
        </section>
        {#if record.payload.excerpt}<section class="detail-section">
            <h3>출처 발췌</h3>
            <blockquote>{record.payload.excerpt}</blockquote>
          </section>{/if}
        <section class="detail-section">
          <h3>출처와 확인 범위</h3>
          <dl class="metadata-list">
            <dt>출처</dt>
            <dd>
              {#if safeSourceUrl(record.payload.source_locator)}<a
                  href={safeSourceUrl(record.payload.source_locator)}
                  target="_blank"
                  rel="noopener noreferrer">{record.payload.source_locator}</a
                >{:else}{record.payload.source_locator}{/if}
            </dd>
            <dt>발표 시각</dt>
            <dd title={record.payload.source_published_at ?? ''}>
              {formatTime(record.payload.source_published_at)}
            </dd>
            <dt>조회 시각</dt>
            <dd title={record.payload.retrieved_at}>{formatTime(record.payload.retrieved_at)}</dd>
            <dt>확인 범위</dt>
            <dd>{verificationLabels[record.payload.verification]}</dd>
          </dl>
          <p class="muted">출처와 기록의 연결은 자료 내용의 진실성을 보장하지 않습니다.</p>
        </section>
      {:else if record.kind === 'review'}
        <section class="detail-section">
          <h3>재검토 결과 · {judgmentLabels[record.payload.judgment]}</h3>
          <p>{record.payload.what_changed}</p>
        </section>
        {@render textList('새로 관측한 내용', record.payload.observations)}
      {/if}
      <details class="subtle-details record-metadata">
        <summary>작성 정보와 원본 기록</summary>
        <dl class="metadata-list">
          <dt>기록 시각</dt>
          <dd title={record.recorded_at}>{formatTime(record.recorded_at)}</dd>
          <dt>작성 인터페이스</dt>
          <dd>{record.author.interface}</dd>
          <dt>모델 선언</dt>
          <dd>
            {record.author.model ?? '미확인'} · {record.author.reasoning_effort ??
              '추론 설정 미확인'}
          </dd>
          <dt>모델 식별 근거</dt>
          <dd>
            {record.author.identity_source === 'declared'
              ? '작성자 선언 · 실제 실행 증명 아님'
              : '미확인'}
          </dd>
          <dt>기록 ID</dt>
          <dd class="identifier">{item.id}</dd>
        </dl>
        <pre>{JSON.stringify(item, null, 2)}</pre>
      </details>
    </div>
    <aside class="detail-sidebar" aria-label="연결된 자료">
      <section>
        <h3>연결된 기록</h3>
        {#if links.length}<ul class="linked-records">
            {#each links as link, index (`${link.id}-${index}`)}<li>
                <button class="linked-record" onclick={() => onselect(link.id)}
                  ><FileText size={17} aria-hidden="true" /><span
                    >{knownRecords.find((record) => record.id === link.id)
                      ? recordTitle(knownRecords.find((record) => record.id === link.id)!)
                      : `${link.label} · ${shortId(link.id)}`}<small>{link.label}</small></span
                  ></button
                >
              </li>{/each}
          </ul>{:else}<p class="muted">연결된 연구 기록이 없습니다.</p>{/if}
      </section>
      {#if record.kind === 'decision'}
        <section>
          <h3>다음 재검토</h3>
          <p title={record.payload.review_after}>{formatTime(record.payload.review_after)}</p>
          <p class="muted">새로운 자료를 확인한 뒤 재검토</p>
        </section>
        {#if record.payload.account_snapshot_id}<section>
            <h3>판단이 참조한 계좌</h3>
            <p class="identifier">{shortId(record.payload.account_snapshot_id)}</p>
            <button
              class="text-button"
              onclick={() => onSnapshot(record.payload.account_snapshot_id!)}
              >이 계좌 관측 보기</button
            >
          </section>{/if}
      {:else if record.kind === 'evidence' && record.payload.artifact}
        <section>
          <h3>연결된 계좌 원자료</h3>
          <p class="identifier">{shortId(record.payload.artifact.id)}</p>
          {#if knownSnapshotIds.includes(record.payload.artifact.id)}<button
              class="text-button"
              onclick={() => onSnapshot(record.payload.artifact!.id)}>이 계좌 관측 보기</button
            >{:else}<p class="muted">개별 계좌 관측 원문에 연결된 근거입니다.</p>{/if}
        </section>
      {/if}
    </aside>
  {/if}
</section>
