<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    CapitalPlanRecord,
    CapitalPlanRequest,
    CapitalPlanReserve,
    FundingRefresh,
    InvestmentContext,
    ResearchResponse,
  } from '$lib/api/types.gen';
  import { fetchInvestigations } from '$lib/investigations';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import {
    capitalActionLabels,
    capitalFormRequest,
    capitalFundingRequest,
    capitalInputKey,
    newCapitalAlternative,
    newCapitalLeg,
    fetchCapitalPlans,
    fetchCapitalPlan,
    previewCapital,
    saveCapital,
    definiteCapitalError,
    fetchFunding,
    applyFunding,
    allocateCapital,
    releaseCapital,
    poolRevisions,
    capitalCondition,
    type CapitalFundingDraft,
  } from '$lib/capital';

  let {
    ready = false,
    jobsEnabled = false,
    synthetic = false,
    selectedSnapshot = '',
    context,
    knownRecords = [],
    onselect,
  }: {
    ready?: boolean;
    jobsEnabled?: boolean;
    synthetic?: boolean;
    selectedSnapshot?: string;
    context?: InvestmentContext;
    knownRecords?: ResearchResponse[];
    onselect: (id: string) => void;
  } = $props();
  let opened = $state(false);
  let sourceValue = $state('');
  let funding = $state<CapitalFundingDraft[]>([
    { currency: 'KRW', enabled: false, limit_amount: '', reserve_amount: '' },
    { currency: 'USD', enabled: false, limit_amount: '', reserve_amount: '' },
  ]);
  let alternatives = $state([newCapitalAlternative(1)]);
  let selectedPlanId = $state('');
  let preview = $state<{ key: string; record: CapitalPlanRecord } | null>(null);
  let calculating = $state(false);
  let mutationBusy = $state(false);
  let pendingSave = $state<{ request: CapitalPlanRequest; request_key: string } | null>(null);
  let pendingFunding = $state<FundingRefresh | null>(null);
  let pendingAllocation = $state<{ id: string; label: string; body: CapitalPlanReserve } | null>(
    null,
  );
  let pendingRelease = $state<string | null>(null);
  let feedback = $state('');
  let actionError = $state('');
  let calculationVersion = 0;
  const plansQuery = createQuery(() => ({
    queryKey: ['capital-plans'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchCapitalPlans(signal),
  }));
  const investigationsQuery = createQuery(() => ({
    queryKey: ['investigations'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchInvestigations(signal),
  }));
  const planQuery = createQuery(() => {
    const id = selectedPlanId;
    return {
      queryKey: ['capital-plan', id],
      enabled: ready && opened && !!id,
      queryFn: ({ signal }) => fetchCapitalPlan(id, signal),
    };
  });
  const fundingQuery = createQuery(() => {
    const snapshotId = selectedSnapshot;
    const accountSeq =
      context?.account?.id === selectedSnapshot ? context.account.snapshot.account_seq : '';
    return {
      queryKey: ['funding', accountSeq, snapshotId],
      enabled: ready && opened && jobsEnabled && !!accountSeq,
      queryFn: ({ signal }) => fetchFunding(accountSeq, snapshotId, signal),
    };
  });
  let sources = $derived([
    ...knownRecords
      .filter((item) => item.record.kind === 'decision')
      .map((item) => ({
        value: `decision:${item.id}`,
        mode: item.record.mode,
        label: `${item.record.kind === 'decision' ? item.record.payload.objective : ''} · 판단 ${shortId(item.id)}`,
      })),
    ...(jobsEnabled && investigationsQuery.isSuccess && !investigationsQuery.isFetching
      ? investigationsQuery.data.items
          .filter(
            (item) =>
              item.latest_completed_revision !== null &&
              typeof item.latest_result?.output_id === 'string',
          )
          .map((item) => ({
            value: `investigation_output:${item.latest_result!.output_id}`,
            mode: item.context_input.mode,
            label: `${item.context_input.purpose} · 조사 결과 ${item.latest_completed_revision}번 버전`,
          }))
      : []),
  ]);
  let sourceMode = $derived(
    synthetic
      ? ('synthetic' as const)
      : (sources.find((item) => item.value === sourceValue)?.mode ?? 'prospective'),
  );
  let formKey = $derived(
    capitalInputKey({ selectedSnapshot, sourceValue, sourceMode, funding, alternatives }),
  );
  let selectedAccount = $derived(
    ready && context?.account?.id === selectedSnapshot ? context.account : undefined,
  );
  let visiblePreview = $derived(
    ready && selectedAccount && preview?.key === formKey && !calculating && !actionError
      ? preview.record
      : undefined,
  );
  let plans = $derived(
    ready && jobsEnabled && plansQuery.isSuccess && !plansQuery.isFetching
      ? plansQuery.data.items
      : undefined,
  );
  let saved = $derived(
    ready &&
      plans &&
      planQuery.isSuccess &&
      !planQuery.isFetching &&
      planQuery.data.id === selectedPlanId
      ? planQuery.data
      : undefined,
  );
  const eligibilityLabels = {
    eligible: '계산상 배정 가능',
    blocked: '배정 불가',
    unknown: '추가 확인 필요',
  };
  let allocationState = $derived(
    ready &&
      jobsEnabled &&
      fundingQuery.isSuccess &&
      !fundingQuery.isFetching &&
      fundingQuery.data.account_seq === selectedAccount?.snapshot.account_seq
      ? fundingQuery.data
      : undefined,
  );
  let pendingMutation = $derived(
    !!pendingSave || !!pendingFunding || !!pendingAllocation || !!pendingRelease,
  );
  let canAllocateSaved = $derived(
    !!saved &&
      !!allocationState &&
      saved.record.snapshot.account_seq === allocationState.account_seq &&
      saved.record.request.mode !== 'retrospective',
  );

  function request(): CapitalPlanRequest {
    if (!selectedAccount) throw new Error('계산할 계좌 관측을 선택하고 조회 완료를 기다려 주세요.');
    if (!sources.some((item) => item.value === sourceValue))
      throw new Error('계획의 바탕이 되는 저장 판단이나 조사 결과를 다시 선택해 주세요.');
    return capitalFormRequest(selectedSnapshot, sourceValue, sourceMode, funding, alternatives);
  }
  async function calculate() {
    const version = ++calculationVersion;
    preview = null;
    actionError = '';
    feedback = '';
    try {
      const body = request();
      const key = formKey;
      calculating = true;
      const record = await previewCapital(body);
      if (version === calculationVersion && key === formKey) preview = { key, record };
    } catch (error) {
      if (version === calculationVersion)
        actionError = error instanceof Error ? error.message : '대안을 계산하지 못했습니다.';
    } finally {
      if (version === calculationVersion) calculating = false;
    }
  }
  async function save() {
    if (!jobsEnabled || mutationBusy) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingSave) {
        if (!visiblePreview) throw new Error('현재 입력으로 대안을 먼저 계산해 주세요.');
        pendingSave = { request: structuredClone(request()), request_key: crypto.randomUUID() };
      }
      mutationBusy = true;
      const result = await saveCapital({
        ...pendingSave.request,
        request_key: pendingSave.request_key,
      });
      pendingSave = null;
      preview = null;
      selectedPlanId = result.id;
      feedback = '자금 계획을 저장했습니다. 예산 적용과 대안 배정은 별도로 선택합니다.';
      await plansQuery.refetch();
      await planQuery.refetch();
    } catch (error) {
      if (definiteCapitalError(error)) pendingSave = null;
      actionError =
        error instanceof Error ? error.message : '계획 저장 여부를 확인하지 못했습니다.';
    } finally {
      mutationBusy = false;
    }
  }
  async function refresh() {
    actionError = '';
    feedback = '';
    preview = null;
    ++calculationVersion;
    calculating = false;
    if (jobsEnabled) {
      await plansQuery.refetch();
      await investigationsQuery.refetch();
      if (selectedAccount) await fundingQuery.refetch();
    }
    if (selectedPlanId) await planQuery.refetch();
  }
  async function applyBudget() {
    if (!jobsEnabled || mutationBusy) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingFunding) {
        if (!allocationState) throw new Error('현재 계좌의 계획 배정 상태를 먼저 읽어 주세요.');
        if (!selectedAccount) throw new Error('계획 예산을 적용할 계좌 관측을 선택해 주세요.');
        pendingFunding = structuredClone({
          snapshot_id: selectedSnapshot,
          mode: sourceMode,
          funding: capitalFundingRequest(funding),
          expected_pool_revisions: poolRevisions(allocationState),
        });
      }
      mutationBusy = true;
      await applyFunding(pendingFunding);
      pendingFunding = null;
      preview = null;
      feedback = '선택한 관측과 계획 예산을 적용했습니다. 대안 배정은 별도로 선택합니다.';
      await fundingQuery.refetch();
    } catch (error) {
      if (definiteCapitalError(error)) {
        pendingFunding = null;
        await fundingQuery.refetch();
      }
      actionError =
        error instanceof Error ? error.message : '계획 예산 적용 여부를 확인하지 못했습니다.';
    } finally {
      mutationBusy = false;
    }
  }
  async function allocate(key?: string, label?: string) {
    if (!jobsEnabled || mutationBusy) return;
    actionError = '';
    feedback = '';
    try {
      if (!pendingAllocation) {
        if (!saved || !allocationState || !canAllocateSaved || !key)
          throw new Error('계획과 같은 계좌의 현재 배정 상태를 먼저 읽어 주세요.');
        pendingAllocation = {
          id: saved.id,
          label: label ?? key,
          body: {
            alternative_id: key,
            request_key: crypto.randomUUID(),
            expected_pool_revisions: poolRevisions(allocationState),
          },
        };
      }
      mutationBusy = true;
      await allocateCapital(pendingAllocation.id, pendingAllocation.body);
      pendingAllocation = null;
      preview = null;
      feedback = '선택한 대안에 자금을 배정했습니다. 이 배정은 작업실의 계획 기록입니다.';
      await fundingQuery.refetch();
    } catch (error) {
      if (definiteCapitalError(error)) {
        pendingAllocation = null;
        await fundingQuery.refetch();
      }
      actionError =
        error instanceof Error ? error.message : '대안 배정 여부를 확인하지 못했습니다.';
    } finally {
      mutationBusy = false;
    }
  }
  async function release(id?: string) {
    if (!jobsEnabled || mutationBusy) return;
    actionError = '';
    feedback = '';
    try {
      pendingRelease ??= id ?? null;
      if (!pendingRelease) return;
      mutationBusy = true;
      await releaseCapital(pendingRelease);
      pendingRelease = null;
      preview = null;
      feedback = '계획 배정을 해제했습니다.';
      await fundingQuery.refetch();
    } catch (error) {
      if (definiteCapitalError(error)) {
        pendingRelease = null;
        await fundingQuery.refetch();
      }
      actionError =
        error instanceof Error ? error.message : '배정 해제 여부를 확인하지 못했습니다.';
    } finally {
      mutationBusy = false;
    }
  }
</script>

{#snippet calculation(record: CapitalPlanRecord, title: string)}
  <section class="capital-result" aria-label={title}>
    <h3>{title}</h3>
    <p class="muted">
      계좌 {record.snapshot.account_seq} · {formatTime(record.snapshot.collection_completed_at)} · 계산
      {formatTime(record.recorded_at)}
    </p>
    <p class="muted">이 기록의 입력으로 계산한 가정입니다. 실제 주문이나 체결을 뜻하지 않습니다.</p>
    <p class="muted" title={record.source_context.id}>
      바탕 자료: {record.source_context.kind === 'decision' ? '저장 판단' : '조사 결과'}
      {shortId(record.source_context.id)} · {formatTime(record.source_context.recorded_at)}
    </p>
    <div class="table-container">
      <table>
        <caption class="sr-only">통화별 계획 여력</caption>
        <thead
          ><tr
            ><th>통화</th><th>관측 매수 가능</th><th>계획 한도</th><th>남겨둘 금액</th><th
              >기존 배정</th
            ><th>배정 가능</th></tr
          ></thead
        >
        <tbody
          >{#each record.calculation.cash_capacity as capacity}<tr>
              <th scope="row">{capacity.currency}</th><td class="numeric"
                >{formatDecimal(capacity.observed_buying_power)}</td
              >
              <td class="numeric">{formatDecimal(capacity.operator_limit)}</td><td class="numeric"
                >{formatDecimal(capacity.operator_reserve)}</td
              >
              <td class="numeric">{formatDecimal(capacity.existing_reserved_amount)}</td><td
                class="numeric">{formatDecimal(capacity.available_amount)}</td
              >
            </tr>{/each}</tbody
        >
      </table>
    </div>
    {#if !record.calculation.local_reservations_known}<p class="market-notice">
        기존 계획 배정 상태를 확인하지 못했습니다. 미확인을 0원으로 계산하지 않습니다.
      </p>{/if}
    {#each record.calculation.alternatives as alternative}
      <section class="detail-section" aria-label={`계산 대안 ${alternative.label}`}>
        <div class="investigation-heading">
          <h3>{alternative.label}</h3>
          <span class="investigation-state">{eligibilityLabels[alternative.eligibility]}</span>
        </div>
        <p>{alternative.rationale}</p>
        <p>
          필요 자금: {alternative.cash_requirements
            .map((item) => `${item.currency} ${formatDecimal(item.amount)}`)
            .join(' · ') || '계산된 필요 자금 없음'}
        </p>
        {#if alternative.unknown_cash_currencies.length}<p class="warning-state">
            필요 자금 미확인: {alternative.unknown_cash_currencies.join(' · ')}
          </p>{/if}
        <p>
          예상 매도 대금: {alternative.estimated_sale_proceeds
            .map((item) => `${item.currency} ${formatDecimal(item.amount)}`)
            .join(' · ') || '계산된 매도 대금 없음'}
        </p>
        {#if alternative.unknown_sale_proceeds_currencies.length}<p class="warning-state">
            매도 대금 미확인: {alternative.unknown_sale_proceeds_currencies.join(' · ')}
          </p>{/if}
        <p class="muted">
          같은 대안의 항목은 합산합니다. 예상 매도 대금은 즉시 재사용 가능한 자금에 더하지 않습니다.
        </p>
        {#if alternative.blockers.length}<ul>
            {#each alternative.blockers as blocker}<li class="warning-state">
                {capitalCondition(blocker)}
              </li>{/each}
          </ul>{/if}
        <div class="table-container">
          <table>
            <caption class="sr-only">{alternative.label} 매매 가정</caption>
            <thead
              ><tr
                ><th>종목·행동</th><th>수량</th><th>가정 가격</th><th>예상 수수료</th><th
                  >예상 세금</th
                ><th>필요 자금</th></tr
              ></thead
            >
            <tbody
              >{#each alternative.legs as leg}<tr
                  ><th scope="row"
                    >{leg.symbol} / {leg.market}<small
                      >{capitalActionLabels[leg.action]} · {leg.currency}</small
                    ></th
                  >
                  <td class="numeric">{formatDecimal(leg.quantity)}</td><td class="numeric"
                    >{formatDecimal(leg.price)}</td
                  ><td class="numeric">{formatDecimal(leg.estimated_fee)}</td><td class="numeric"
                    >{formatDecimal(leg.estimated_tax)}</td
                  ><td class="numeric">{formatDecimal(leg.required_cash)}</td>
                </tr>{/each}</tbody
            >
          </table>
        </div>
        <div class="table-container">
          <table>
            <caption class="sr-only">{alternative.label} 예상 보유</caption>
            <thead
              ><tr
                ><th>종목</th><th>관측 수량</th><th>기존 매도 배정</th><th>변경 후 수량</th><th
                  >기존 평균 매수가</th
                ><th>변경 후 평균 매수가</th></tr
              ></thead
            >
            <tbody
              >{#each alternative.holdings as holding}<tr
                  ><th scope="row"
                    >{holding.symbol} / {holding.market}<small>{holding.currency}</small></th
                  >
                  <td class="numeric">{formatDecimal(holding.observed_quantity)}</td><td
                    class="numeric">{formatDecimal(holding.existing_reserved_quantity)}</td
                  ><td class="numeric">{formatDecimal(holding.projected_quantity)}</td><td
                    class="numeric">{formatDecimal(holding.average_purchase_price_before)}</td
                  ><td class="numeric"
                    >{formatDecimal(
                      holding.average_purchase_price_after,
                    )}{holding.average_price_rounded ? ' (반올림)' : ''}</td
                  >
                </tr>{/each}</tbody
            >
          </table>
        </div>
        <p class="muted">평균 매수가는 가정한 수수료·세금을 제외한 값입니다.</p>
        {#each alternative.holdings.filter((item) => item.average_price_reason !== null) as holding}<p
            class="muted"
          >
            {holding.symbol} · {capitalCondition(holding.average_price_reason!)}
          </p>{/each}
      </section>
    {/each}
  </section>
{/snippet}

<section class="panel capital-panel" aria-label="자금 계획">
  <details bind:open={opened}>
    <summary>자금 계획</summary>
    <p class="muted">
      기존 보유와 신규 자금으로 여러 대안을 비교합니다. 수량과 비용은 직접 가정합니다.
    </p>
    <div class="capital-actions">
      <Button
        variant="outline"
        onclick={refresh}
        disabled={!ready || mutationBusy || plansQuery.isFetching}
        ><RotateCw size={14} aria-hidden="true" />자금 계획 다시 읽기</Button
      >
    </div>
    {#if !ready}<p class="empty-state">작업실 연결을 확인하고 있습니다.</p>{:else}
      {#if !selectedAccount}<p class="market-notice">
          상단에서 계산할 계좌 관측을 선택해 주세요.
        </p>{:else}<p class="investigation-account">
          선택 계좌 {selectedAccount.snapshot.account_seq} · {formatTime(
            selectedAccount.snapshot.collection_completed_at,
          )}<br /><span class="muted"
            >현금 잔고 미확인 · 매수 가능 금액과 계획 예산은 구분합니다.</span
          >
        </p>{/if}
      <form
        onsubmit={(event) => {
          event.preventDefault();
          void calculate();
        }}
      >
        <fieldset
          disabled={!selectedAccount || mutationBusy || pendingMutation}
          class="capital-fields"
        >
          <div class="capital-form">
            <label class="capital-full"
              >바탕이 되는 판단·조사<select aria-label="계획 근거" bind:value={sourceValue}
                ><option value="">저장 판단 또는 완료한 조사 결과 선택</option
                >{#each sources as source}<option value={source.value}>{source.label}</option
                  >{/each}</select
              ></label
            >
          </div>
          {#if investigationsQuery.isError}<p class="warning-state">
              조사 결과 목록을 읽지 못했습니다. 저장 판단은 계속 선택할 수 있습니다.
            </p>{/if}
          <div class="capital-funding-grid">
            {#each funding as item}<fieldset class="capital-budget">
                <legend
                  ><input
                    type="checkbox"
                    aria-label={`${item.currency} 계획 예산 사용`}
                    bind:checked={item.enabled}
                  />
                  {item.currency} 계획 예산</legend
                >
                <div class="capital-form">
                  <label
                    >계획 한도<input
                      aria-label={`${item.currency} 계획 한도`}
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={item.limit_amount}
                      disabled={!item.enabled}
                    /></label
                  ><label
                    >남겨둘 금액<input
                      aria-label={`${item.currency} 남겨둘 금액`}
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={item.reserve_amount}
                      disabled={!item.enabled}
                    /></label
                  >
                </div>
              </fieldset>{/each}
          </div>
          {#each alternatives as alternative, index (alternative.key)}<fieldset
              class="capital-alternative"
            >
              <legend>비교 대안 {index + 1}</legend>
              <div class="capital-form">
                <label
                  >대안 이름<input
                    aria-label={`대안 ${index + 1} 이름`}
                    maxlength="200"
                    bind:value={alternative.label}
                  /></label
                ><label
                  >대안의 이유<textarea
                    aria-label={`대안 ${index + 1} 이유`}
                    maxlength="2000"
                    bind:value={alternative.rationale}></textarea></label
                >
              </div>
              {#each alternative.legs as leg, legIndex (leg.rowId)}<div
                  class="capital-leg"
                  role="group"
                  aria-label={`대안 ${index + 1} 항목 ${legIndex + 1}`}
                >
                  <label
                    >행동<select aria-label="행동" bind:value={leg.action}
                      >{#each Object.entries(capitalActionLabels) as [action, label]}<option
                          value={action}>{label}</option
                        >{/each}</select
                    ></label
                  >
                  <label
                    >종목<input aria-label="종목" maxlength="32" bind:value={leg.symbol} /></label
                  >
                  <label
                    >시장<select aria-label="시장" bind:value={leg.market}
                      ><option value="KR">한국</option><option value="US">미국</option></select
                    ></label
                  >
                  <label
                    >통화<select aria-label="통화" bind:value={leg.currency}
                      ><option value="KRW">KRW</option><option value="USD">USD</option></select
                    ></label
                  >
                  <label
                    >수량<input
                      aria-label="수량"
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={leg.quantity}
                    /></label
                  >
                  <label
                    >가정 가격 · 없으면 미확인<input
                      aria-label="가정 가격"
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={leg.price}
                    /></label
                  >
                  <label
                    >수수료율 · bp<input
                      aria-label="수수료율"
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={leg.fee_bps}
                    /></label
                  >
                  <label
                    >고정 수수료 · {leg.currency}<input
                      aria-label="고정 수수료"
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={leg.fixed_fee}
                    /></label
                  >
                  <label
                    >세율 · bp<input
                      aria-label="세율"
                      inputmode="decimal"
                      maxlength="64"
                      bind:value={leg.tax_bps}
                    /></label
                  >
                  <label class="capital-leg-rationale"
                    >항목의 이유<input
                      aria-label="항목의 이유"
                      maxlength="2000"
                      bind:value={leg.rationale}
                    /></label
                  >
                  {#if alternative.legs.length > 1}<button
                      type="button"
                      class="text-button"
                      onclick={() => {
                        alternative.legs = alternative.legs.filter(
                          (item) => item.rowId !== leg.rowId,
                        );
                      }}>항목 삭제</button
                    >{/if}
                </div>{/each}
              <div class="capital-actions">
                <button
                  type="button"
                  class="text-button"
                  disabled={alternative.legs.length >= 50}
                  onclick={() => {
                    alternative.legs = [...alternative.legs, newCapitalLeg()];
                  }}>같은 대안에 항목 추가</button
                >{#if alternatives.length > 1}<button
                    type="button"
                    class="text-button"
                    onclick={() => {
                      alternatives = alternatives.filter((item) => item.key !== alternative.key);
                    }}>대안 삭제</button
                  >{/if}
              </div>
            </fieldset>{/each}
          <p class="muted">
            100 bp = 1% · 비용이 없다는 가정은 0을 입력합니다. 교체는 같은 대안에 매도와 매수를 함께
            추가합니다.
          </p>
          <div class="capital-actions">
            <button
              type="button"
              class="text-button"
              disabled={alternatives.length >= 10}
              onclick={() => {
                alternatives = [...alternatives, newCapitalAlternative(alternatives.length + 1)];
              }}>다른 대안 추가</button
            ><Button type="submit" disabled={!selectedAccount || calculating}>대안 계산</Button>
          </div>
        </fieldset>
      </form>
      {#if calculating}<p role="status" class="empty-state">
          선택한 계좌와 가정으로 계산하고 있습니다.
        </p>{/if}
      {#if visiblePreview}{@render calculation(visiblePreview, '대안 계산 결과')}{/if}
      {#if pendingSave}<p class="market-notice">
          계획 저장 여부 확인 대기 · 계좌 관측 {shortId(pendingSave.request.snapshot_id)}. 같은
          요청으로 확인합니다.
        </p>{/if}
      <div class="capital-actions">
        <Button
          variant="outline"
          onclick={save}
          disabled={!jobsEnabled ||
            mutationBusy ||
            (!!pendingMutation && !pendingSave) ||
            (!visiblePreview && !pendingSave)}
          >{pendingSave ? '같은 계획 저장 다시 확인' : '계획 저장'}</Button
        >{#if !jobsEnabled}<span class="muted"
            >계획 저장과 자금 배정은 이 작업실에서 꺼져 있습니다.</span
          >{/if}
      </div>
      {#if actionError}<p role="alert" class="error-state investigation-feedback">
          {actionError}
        </p>{/if}{#if feedback}<p role="status" class="investigation-feedback">{feedback}</p>{/if}
      <section class="capital-result" aria-label="저장한 자금 계획">
        <h3>저장한 계획</h3>
        {#if !jobsEnabled}<p class="empty-state muted">
            계획 저장소가 꺼져 있습니다. 대안 계산은 계속 사용할 수 있습니다.
          </p>{:else if plansQuery.isFetching}<p role="status" class="empty-state">
            저장한 계획을 읽고 있습니다.
          </p>{:else if plansQuery.isError}<p role="alert" class="error-state">
            {plansQuery.error.message}
          </p>{:else if plans?.length}<ul class="investigation-list">
            {#each plans as plan}<li>
                <button
                  type="button"
                  class="investigation-select"
                  aria-pressed={selectedPlanId === plan.id}
                  onclick={() => {
                    selectedPlanId = plan.id;
                  }}
                  ><span
                    ><strong
                      >{formatTime(plan.recorded_at)} · 대안 {plan.alternative_count}개</strong
                    ><small>계좌 관측 {shortId(plan.snapshot_id)} · {shortId(plan.id)}</small></span
                  ><span class="investigation-state"
                    >{plan.mode === 'synthetic'
                      ? '합성'
                      : plan.mode === 'retrospective'
                        ? '과거 분석'
                        : '사전 계획'}</span
                  ></button
                >
              </li>{/each}
          </ul>{:else}<p class="empty-state muted">저장한 자금 계획이 없습니다.</p>{/if}
      </section>
      {#if selectedPlanId}{#if planQuery.isFetching}<p role="status" class="empty-state">
            계획 상세를 읽고 있습니다.
          </p>{:else if planQuery.isError}<p role="alert" class="error-state">
            {planQuery.error.message}
          </p>{:else if saved}{@render calculation(saved.record, '저장 계획 상세')}
          <p class="identifier muted">계획 {saved.id}</p>
          <div class="capital-actions">
            {#each saved.record.calculation.alternatives as alternative}<Button
                variant="outline"
                disabled={!canAllocateSaved ||
                  !allocationState?.pools.length ||
                  mutationBusy ||
                  pendingMutation ||
                  alternative.eligibility !== 'eligible'}
                onclick={() => allocate(alternative.key, alternative.label)}
                >{alternative.label}에 자금 배정</Button
              >{/each}
          </div>
          {#if !canAllocateSaved}<p class="muted">
              배정하려면 계획과 같은 계좌를 선택하고 현재 계획 예산을 확인해 주세요. 과거 분석은
              배정하지 않습니다.
            </p>{/if}
          {#if saved.record.request.source.kind === 'decision'}<button
              class="text-button"
              onclick={() => onselect(saved!.record.request.source.id)}>바탕이 된 판단 보기</button
            >{/if}{/if}{/if}
      <section class="capital-result" aria-label="계획 예산과 배정">
        <h3>계획 예산과 배정</h3>
        <p class="muted">
          같은 계좌의 기존 배정을 유지합니다. 새 관측을 선택하는 것만으로 예산이 바뀌지 않습니다.
        </p>
        {#if pendingFunding}<p class="market-notice">
            예산 적용 여부 확인 대기 · 관측 {shortId(pendingFunding.snapshot_id)}
          </p>{/if}
        <div class="capital-actions">
          <Button
            variant="outline"
            onclick={applyBudget}
            disabled={!jobsEnabled ||
              mutationBusy ||
              (!!pendingMutation && !pendingFunding) ||
              (!pendingFunding &&
                (!allocationState || !selectedAccount || sourceMode === 'retrospective'))}
            >{pendingFunding ? '같은 예산 적용 다시 확인' : '선택 관측으로 계획 예산 적용'}</Button
          >
        </div>
        {#if pendingAllocation}<p class="market-notice">
            대안 배정 여부 확인 대기 · {pendingAllocation.label} · 계획 {shortId(
              pendingAllocation.id,
            )}
          </p>
          <Button
            variant="outline"
            onclick={() => allocate()}
            disabled={!jobsEnabled || mutationBusy}>같은 대안 배정 다시 확인</Button
          >{/if}
        {#if pendingRelease}<p class="market-notice">
            배정 해제 여부 확인 대기 · {shortId(pendingRelease)}
          </p>
          <Button
            variant="outline"
            onclick={() => release()}
            disabled={!jobsEnabled || mutationBusy}>같은 배정 해제 다시 확인</Button
          >{/if}
        {#if !jobsEnabled}<p class="empty-state muted">
            계획 예산과 배정 기록은 이 작업실에서 꺼져 있습니다.
          </p>{:else if !selectedAccount}<p class="empty-state muted">
            계좌 관측 선택 후 현재 배정 상태를 읽습니다.
          </p>{:else if fundingQuery.isFetching}<p class="empty-state" role="status">
            현재 계획 예산과 배정을 읽고 있습니다.
          </p>{:else if fundingQuery.isError}<p class="error-state" role="alert">
            {fundingQuery.error.message}
          </p>{:else if allocationState}
          <p class="muted">
            계좌 {allocationState.account_seq} · 작업실의 계획 기록이며 증권사의 주문·자금 예약이 아닙니다.
          </p>
          {#if allocationState.pools.length}<div class="table-container">
              <table>
                <caption class="sr-only">현재 계획 예산과 보유 배정</caption><thead
                  ><tr
                    ><th>통화·종목</th><th>적용 관측</th><th>계획 여력</th><th>배정량</th><th
                      >남은 여력</th
                    ></tr
                  ></thead
                ><tbody
                  >{#each allocationState.pools as pool}<tr
                      ><th scope="row"
                        >{pool.kind === 'cash' ? '자금' : `${pool.market} / ${pool.symbol}`} · {pool.currency}<small
                          >{pool.mode === 'synthetic' ? '합성' : '계획'}</small
                        ></th
                      ><td>{formatTime(pool.observed_at)}</td><td class="numeric"
                        >{formatDecimal(pool.capacity)}</td
                      ><td class="numeric">{formatDecimal(pool.reserved)}</td><td class="numeric"
                        >{formatDecimal(pool.available)}{pool.overallocated
                          ? ' · 초과 배정'
                          : ''}</td
                      ></tr
                    >{/each}</tbody
                >
              </table>
            </div>{:else}<p class="empty-state muted">
              이 계좌에 적용한 계획 예산이 없습니다.
            </p>{/if}
          <ul class="investigation-list">
            {#each allocationState.reservations as reservation}<li class="capital-reservation">
                <div>
                  <strong
                    >{reservation.status === 'active'
                      ? '배정 중'
                      : reservation.status === 'released'
                        ? '해제됨'
                        : '교체됨'} · {reservation.mode === 'synthetic' ? '합성' : '계획'}</strong
                  >
                  <p class="muted">
                    계획 {shortId(reservation.plan_id)} · {formatTime(reservation.created_at)}
                  </p>
                  <p>
                    {reservation.requirements.cash
                      .map((item) => `${item.currency} ${formatDecimal(item.amount)}`)
                      .join(' · ') || '현금 배정 없음'}{reservation.requirements.holdings.length
                      ? ` · 보유 ${reservation.requirements.holdings.map((item) => `${item.symbol} ${formatDecimal(item.quantity)}`).join(' · ')}`
                      : ''}
                  </p>
                </div>
                {#if reservation.status === 'active'}<Button
                    variant="outline"
                    onclick={() => release(reservation.id)}
                    disabled={mutationBusy || pendingMutation}>배정 해제</Button
                  >{/if}
              </li>{/each}
          </ul>
          {#if !allocationState.reservations.length}<p class="muted">
              현재 조회 범위에 배정 기록이 없습니다.
            </p>{/if}
          {#if allocationState.omitted_reservation_count}<p class="muted">
              이전 배정 기록 {allocationState.omitted_reservation_count}개 생략
            </p>{/if}
        {/if}
      </section>
    {/if}
  </details>
</section>
