<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { tick } from 'svelte';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    InvestmentContext,
    PaperBookCreate,
    PaperSubmit,
    PaperAdvance,
    PaperCancel,
  } from '$lib/api/types.gen';
  import { fetchCapitalPlans, fetchCapitalPlan, capitalActionLabels } from '$lib/capital';
  import { fetchMarketCatalog } from '$lib/market';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import {
    fetchPaperBooks,
    fetchPaperBook,
    createPaper,
    submitPaper,
    advancePaper,
    cancelPaper,
    paperCashRequest,
    paperProfileRequest,
    paperRevision,
    definitePaperError,
    paperStatus,
    paperEventLabels,
    paperEventData,
    paperUnfilledReason,
    type PaperCashDraft,
  } from '$lib/paper';

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
  let label = $state('');
  let cash = $state<PaperCashDraft[]>([
    { currency: 'KRW', enabled: false, amount: '' },
    { currency: 'USD', enabled: false, amount: '' },
  ]);
  let selectedBookId = $state('');
  let selectedPlanId = $state('');
  let alternativeId = $state('');
  let profile = $state({ slippage_bps: '', participation_bps: '', quantity_step: '' });
  let captureIds = $state<string[]>([]);
  type Pending =
    | { kind: 'create'; body: PaperBookCreate; previousBook: string }
    | { kind: 'submit'; id: string; body: PaperSubmit }
    | { kind: 'advance'; id: string; body: PaperAdvance }
    | { kind: 'cancel'; id: string; intentId: string; body: PaperCancel };
  let pending = $state<Pending | null>(null);
  let busy = $state(false);
  let feedback = $state('');
  let actionError = $state('');
  const booksQuery = createQuery(() => ({
    queryKey: ['paper-books'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchPaperBooks(signal),
  }));
  const bookQuery = createQuery(() => {
    const id = selectedBookId;
    return {
      queryKey: ['paper-book', id],
      enabled: ready && opened && jobsEnabled && !!id,
      queryFn: ({ signal }) => fetchPaperBook(id, signal),
    };
  });
  const plansQuery = createQuery(() => ({
    queryKey: ['capital-plans'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchCapitalPlans(signal),
  }));
  const planQuery = createQuery(() => {
    const id = selectedPlanId;
    return {
      queryKey: ['capital-plan', id],
      enabled: ready && opened && jobsEnabled && !!id,
      queryFn: ({ signal }) => fetchCapitalPlan(id, signal),
    };
  });
  const catalogQuery = createQuery(() => ({
    queryKey: ['market-catalog'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchMarketCatalog(signal),
  }));
  let account = $derived(
    ready && context?.account?.id === selectedSnapshot ? context.account : undefined,
  );
  let books = $derived(
    ready && jobsEnabled && booksQuery.isSuccess && !booksQuery.isFetching
      ? booksQuery.data
      : undefined,
  );
  let book = $derived(
    books && bookQuery.isSuccess && !bookQuery.isFetching && bookQuery.data.id === selectedBookId
      ? bookQuery.data
      : undefined,
  );
  let plans = $derived(
    ready && jobsEnabled && plansQuery.isSuccess && !plansQuery.isFetching
      ? plansQuery.data
      : undefined,
  );
  let plan = $derived(
    plans && planQuery.isSuccess && !planQuery.isFetching && planQuery.data.id === selectedPlanId
      ? planQuery.data
      : undefined,
  );
  let alternative = $derived(
    plan?.record.request.alternatives.find((item) => item.key === alternativeId),
  );
  let compatiblePlan = $derived(
    !!book &&
      !!plan &&
      plan.record.snapshot.account_seq === book.account_seq &&
      plan.record.request.mode === book.mode,
  );
  let captures = $derived(
    ready && catalogQuery.isSuccess && !catalogQuery.isFetching
      ? catalogQuery.data.items.filter(
          (item) =>
            item.status === 'supported' && item.interval === '1m' && item.adjusted === false,
        )
      : undefined,
  );
  const operationLabels = {
    create: '원장 만들기',
    submit: '대안 모의 주문',
    advance: '관측으로 진행',
    cancel: '모의 주문 취소',
  };

  async function refresh() {
    actionError = '';
    await Promise.all([booksQuery.refetch(), plansQuery.refetch(), catalogQuery.refetch()]);
    await Promise.all([
      ...(selectedBookId ? [bookQuery.refetch()] : []),
      ...(selectedPlanId ? [planQuery.refetch()] : []),
    ]);
  }
  async function runPending() {
    if (!pending || busy || !ready || !jobsEnabled) return;
    const operation = pending;
    busy = true;
    feedback = '';
    actionError = '';
    try {
      const result =
        operation.kind === 'create'
          ? await createPaper(operation.body)
          : operation.kind === 'submit'
            ? await submitPaper(operation.id, operation.body)
            : operation.kind === 'advance'
              ? await advancePaper(operation.id, operation.body)
              : await cancelPaper(operation.id, operation.intentId, operation.body);
      pending = null;
      if (
        operation.kind === 'create' &&
        selectedSnapshot === operation.body.snapshot_id &&
        selectedBookId === operation.previousBook
      )
        selectedBookId = result.book.id;
      feedback = `${result.book.label} · ${operationLabels[operation.kind]} 결과를 저장했습니다. 최신 원장을 조회합니다.`;
      await booksQuery.refetch();
      await tick();
      if (selectedBookId) await bookQuery.refetch();
    } catch (error) {
      if (definitePaperError(error)) pending = null;
      actionError =
        error instanceof Error ? error.message : '모의 원장 처리 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  function prepare(operation: () => Pending) {
    if (!ready || !jobsEnabled || pending || busy) return;
    actionError = '';
    feedback = '';
    try {
      pending = operation();
      void runPending();
    } catch (error) {
      actionError = error instanceof Error ? error.message : '입력 내용을 확인해 주세요.';
    }
  }
  function create(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (!account) throw new Error('시작할 계좌 관측을 명시적으로 선택하고 조회를 기다려 주세요.');
      if (!label.trim()) throw new Error('모의 원장 이름을 입력해 주세요.');
      return {
        kind: 'create',
        previousBook: selectedBookId,
        body: {
          label: label.trim(),
          snapshot_id: selectedSnapshot,
          mode: synthetic ? 'synthetic' : 'prospective',
          initial_cash: paperCashRequest(cash),
          request_key: crypto.randomUUID(),
        },
      };
    });
  }
  function submit(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (!book || !plan || !alternative || !compatiblePlan)
        throw new Error('최신 모의 원장과 저장 계획의 대안을 선택해 주세요.');
      return {
        kind: 'submit',
        id: book.id,
        body: {
          plan_id: plan.id,
          alternative_id: alternative.key,
          profile: paperProfileRequest(profile),
          request_key: crypto.randomUUID(),
          expected_revision: paperRevision(book.revision),
        },
      };
    });
  }
  function advance(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (
        !book ||
        !captures ||
        !captureIds.length ||
        captureIds.length > 20 ||
        captureIds.some((id) => !captures.some((item) => item.capture_id === id))
      )
        throw new Error('진행할 비수정 분봉 캡처를 1~20개 선택해 주세요.');
      return {
        kind: 'advance',
        id: book.id,
        body: {
          capture_ids: [...captureIds],
          request_key: crypto.randomUUID(),
          expected_revision: paperRevision(book.revision),
        },
      };
    });
  }
  function cancel(intentId: string) {
    prepare(() => {
      if (!book || !book.intents.some((item) => item.id === intentId))
        throw new Error('취소할 모의 주문을 최신 원장에서 확인해 주세요.');
      return {
        kind: 'cancel',
        id: book.id,
        intentId,
        body: { request_key: crypto.randomUUID(), expected_revision: paperRevision(book.revision) },
      };
    });
  }
</script>

<section class="panel capital-panel paper-panel" aria-label="모의 매매">
  <details bind:open={opened}>
    <summary>모의 매매</summary>
    <p class="muted">
      선택한 대안을 이후 시장 관측으로 검토합니다. 모의 현금·보유·체결은 실제 계좌와 별도입니다.
    </p>
    {#if synthetic}<p class="warning-state">
        합성 작업실 · 아래 금액과 체결은 합성 모의 기록입니다.
      </p>{/if}
    {#if !ready}<p class="small-empty muted">작업실 상태를 확인하고 있습니다.</p>
    {:else if !jobsEnabled}<p class="small-empty muted">
        이 작업실에서는 모의 원장 저장이 꺼져 있습니다. 기존 계좌와 조사 자료는 계속 조회할 수
        있습니다.
      </p>
    {:else}
      <div class="capital-actions">
        <Button variant="outline" onclick={refresh} disabled={busy || booksQuery.isFetching}
          ><RotateCw size={15} aria-hidden="true" />모의 자료 다시 읽기</Button
        >
      </div>
      {#if actionError}<p role="alert" class="error-state">{actionError}</p>{/if}
      {#if feedback}<p role="status" class="feedback-state">{feedback}</p>{/if}
      {#if pending}<div class="warning-state paper-pending">
          <p>
            처리 확인 중인 요청: {operationLabels[pending.kind]} · {pending.kind === 'create'
              ? `계좌 관측 ${shortId(pending.body.snapshot_id)}`
              : `원장 ${shortId(pending.id)}`}. 입력이나 선택을 바꿔도 이 요청의 내용은 유지합니다.
          </p>
          <p>아래 원장은 마지막 조회 상태입니다. 이 요청의 반영 여부는 아직 미확인입니다.</p>
          <Button variant="outline" onclick={runPending} disabled={busy}
            >같은 모의 요청 결과 확인</Button
          >
        </div>{/if}
      <details class="paper-create">
        <summary>새 모의 원장 만들기</summary>
        <p class="muted">
          선택한 시작 관측: {account
            ? `${account.snapshot.account_seq} · ${shortId(account.id)} · ${formatTime(account.snapshot.collection_completed_at)}`
            : '계좌 관측을 선택해 주세요.'}
        </p>
        <form onsubmit={create} class="capital-form">
          <label class="capital-full"
            >모의 원장 이름<input
              bind:value={label}
              maxlength="100"
              placeholder="예: 9월 대안 검토"
            /></label
          >
          <p class="muted capital-full">
            초기 모의 현금을 직접 입력합니다. 매수 가능 금액은 현금 잔고가 아니므로 복제하지
            않습니다. 통화별로 따로 평가합니다.
          </p>
          {#each cash as item}<fieldset class="capital-budget">
              <legend
                ><label class="paper-check"
                  ><input type="checkbox" bind:checked={item.enabled} />{item.currency} 모의 현금 사용</label
                ></legend
              ><label
                >{item.currency} 초기 모의 현금<input
                  bind:value={item.amount}
                  disabled={!item.enabled}
                  inputmode="decimal"
                  placeholder="직접 입력"
                /></label
              >
            </fieldset>{/each}
          <div class="capital-full">
            <Button type="submit" disabled={!account || !!pending || busy}>모의 원장 만들기</Button>
          </div>
        </form>
      </details>
      <div class="capital-form">
        <label class="capital-full"
          >모의 원장 선택<select bind:value={selectedBookId}
            ><option value="">원장을 명시적으로 선택하세요</option
            >{#each books?.items ?? [] as item}<option value={item.id}
                >{item.label} · {item.account_seq} · {item.mode === 'synthetic'
                  ? '합성 모의'
                  : '전향적 모의'}</option
              >{/each}</select
          ></label
        >
      </div>
      {#if booksQuery.isError}<p role="alert" class="error-state">
          {booksQuery.error.message}
        </p>{:else if booksQuery.isFetching}<p class="small-empty muted">
          모의 원장을 읽고 있습니다.
        </p>{:else if books && !books.items.length}<p class="small-empty muted">
          저장된 모의 원장이 없습니다.
        </p>{/if}
      {#if books?.omitted_count}<p class="muted">
          원장 {books.omitted_count}개는 이번 목록에 포함되지 않았습니다.
        </p>{/if}
      {#if selectedBookId && bookQuery.isError}<p role="alert" class="error-state">
          {bookQuery.error.message}
        </p>{:else if selectedBookId && bookQuery.isFetching}<p class="small-empty muted">
          선택한 모의 원장을 읽고 있습니다.
        </p>{/if}
      {#if book}
        <section class="capital-result" aria-label="선택한 모의 원장">
          <h3>{book.label}</h3>
          <p class="muted">
            {book.mode === 'synthetic' ? '합성 모의' : '전향적 모의'} · 계좌 {book.account_seq} · {book.revision}번
            상태 · 갱신 {formatTime(book.updated_at)}
          </p>
          <p class="muted identifier">시작 계좌 관측 {book.snapshot_id}</p>
          <p class="muted">
            이 원장은 상단의 계좌 관측 선택을 바꿔도 유지됩니다. 실제 계좌나 계획 예산의 배정을
            변경하지 않습니다.
          </p>
          <p class="muted">
            초기 모의 현금: {book.seed.initial_cash
              .map((item) => `${item.currency} ${formatDecimal(item.amount)}`)
              .join(' · ')}
          </p>
          <h3 class="paper-subheading">통화별 모의 평가</h3>
          <p class="muted">
            시작 관측의 기존 보유 원가를 포함한 모의 원장 기준 손익입니다. 이번 판단 이후의 수익으로
            귀속하지 않습니다. 통화는 환산 없이 구분합니다.
          </p>
          <div class="table-container">
            <table>
              <caption class="sr-only">통화별 모의 원장 평가</caption><thead
                ><tr
                  ><th>통화</th><th>모의 현금</th><th>보유 평가</th><th>합계 평가</th><th
                    >원가 기준 평가 손익</th
                  ><th>원가 기준 실현 손익</th><th>모의 누적 비용</th></tr
                ></thead
              ><tbody
                >{#each book.state.valuation as value}<tr
                    ><th scope="row">{value.currency}</th><td class="numeric"
                      >{formatDecimal(value.cash)}</td
                    ><td class="numeric">{formatDecimal(value.position_value)}</td><td
                      class="numeric">{formatDecimal(value.equity)}</td
                    ><td class="numeric">{formatDecimal(value.unrealized_pnl)}</td><td
                      class="numeric">{formatDecimal(value.realized_pnl)}</td
                    ><td class="numeric">{formatDecimal(value.modeled_cost)}</td></tr
                  >{/each}</tbody
              >
            </table>
          </div>
          {#each book.state.valuation as value}
            {#if value.missing_price_symbols.length}<p class="warning-state">
                {value.currency} 평가 가격 미확인: {value.missing_price_symbols.join(', ')}
              </p>{/if}
            {#if value.unknown_cost_symbols.length}<p class="warning-state">
                {value.currency} 보유 원가 미확인: {value.unknown_cost_symbols.join(', ')}
              </p>{/if}
            {#if value.unknown_realized_sales}<p class="warning-state">
                {value.currency} 원가 미확인 매도 {value.unknown_realized_sales}건 · 알려진 부분의
                실현 손익 {formatDecimal(value.known_realized_pnl)}
              </p>{/if}
          {/each}
          {#if book.state.arithmetic_rounded}<p class="muted">
              일부 원장 계산은 소수 연산 정밀도 범위에서 반올림됐습니다.
            </p>{/if}
          <div class="table-container">
            <table>
              <caption class="sr-only">모의 보유와 평가 관측</caption><thead
                ><tr
                  ><th>종목</th><th>모의 보유</th><th>기록 원가</th><th>평가 가격</th><th
                    >봉 종료</th
                  ><th>관측 확인</th><th>평가 캡처</th></tr
                ></thead
              ><tbody
                >{#each book.state.positions as position}{@const mark = book.state.marks.find(
                    (item) =>
                      item.market === position.market &&
                      item.symbol === position.symbol &&
                      item.currency === position.currency,
                  )}<tr
                    ><th scope="row"
                      >{position.symbol} / {position.market}<small>{position.currency}</small></th
                    ><td class="numeric">{formatDecimal(position.quantity)}</td><td class="numeric"
                      >{formatDecimal(position.cost_basis)}</td
                    ><td class="numeric">{formatDecimal(mark?.price)}</td><td
                      >{formatTime(mark?.period_end)}</td
                    ><td>{formatTime(mark?.observed_at)}</td><td title={mark?.capture_id}
                      >{mark ? shortId(mark.capture_id) : '미확인'}</td
                    ></tr
                  >{/each}</tbody
              >
            </table>
          </div>
          <section aria-label="모의 주문 상태">
            <h3 class="paper-subheading">모의 주문 상태</h3>
            {#if !book.intents.length}<p class="muted">접수한 모의 주문이 없습니다.</p>{/if}
            {#each book.intents as intent}<article
                class="paper-intent"
                aria-label={`모의 주문 ${shortId(intent.id)}`}
              >
                <div class="paper-intent-header">
                  <strong>{intent.alternative_id} · {paperStatus[intent.state.status]}</strong
                  >{#if ['pending', 'partially_filled'].includes(intent.state.status)}<Button
                      variant="outline"
                      disabled={busy || !!pending}
                      onclick={() => cancel(intent.id)}>남은 모의 주문 취소</Button
                    >{/if}
                </div>
                <p class="muted">
                  대안 선택 시각 {formatTime(intent.created_at)} · 계획 {shortId(intent.plan_id)}
                </p>
                <p class="muted">
                  고정 가정: 슬리피지 {formatDecimal(intent.state.profile.slippage_bps)} bp · 거래량 참여
                  {formatDecimal(intent.state.profile.participation_bps)} bp · 수량 단위 {formatDecimal(
                    intent.state.profile.quantity_step,
                  )}
                </p>
                <div class="table-container">
                  <table>
                    <caption class="sr-only">{shortId(intent.id)} 모의 주문 수량</caption><thead
                      ><tr
                        ><th>종목·행동</th><th>상태</th><th>계획 수량</th><th>모의 체결</th><th
                          >남은 수량</th
                        ><th>남은 현금 배정</th></tr
                      ></thead
                    ><tbody
                      >{#each intent.state.legs as leg}<tr
                          ><th scope="row"
                            >{leg.request.symbol} / {leg.request.market}<small
                              >{capitalActionLabels[leg.request.action]} · {leg.request
                                .currency}</small
                            ></th
                          ><td>{paperStatus[leg.status]}</td><td class="numeric"
                            >{formatDecimal(leg.request.quantity)}</td
                          ><td class="numeric">{formatDecimal(leg.filled_quantity)}</td><td
                            class="numeric">{formatDecimal(leg.remaining_quantity)}</td
                          ><td class="numeric">{formatDecimal(leg.cash_budget_remaining)}</td></tr
                        >{/each}</tbody
                    >
                  </table>
                </div>
              </article>{/each}
            {#if book.omitted_intent_count}<p class="muted">
                이전 모의 주문 {book.omitted_intent_count}개는 이번 조회에 포함되지 않았습니다.
              </p>{/if}
          </section>
          <section aria-label="모의 체결과 기록">
            <h3 class="paper-subheading">모의 체결과 기록</h3>
            <p class="muted">
              체결 기준은 관측 봉의 종료 시각이며, 시스템 기록 시각과 다릅니다. 관측 봉의 최종 확정
              여부는 미확인입니다.
            </p>
            {#if !book.events.length}<p class="muted">아직 모의 원장 사건이 없습니다.</p>{/if}
            <div class="table-container">
              <table>
                <caption class="sr-only">모의 체결 및 비용 기록</caption><thead
                  ><tr
                    ><th>기록</th><th>종목·주문</th><th>모의 수량 / 가격</th><th>수수료 / 세금</th
                    ><th>현금 변화</th><th>체결 기준 시각</th><th>시스템 기록 시각</th><th>캡처</th
                    ></tr
                  ></thead
                ><tbody
                  >{#each book.events as event}<tr
                      ><th scope="row"
                        >{paperEventLabels[event.kind] ??
                          '원장 기록'}{#if event.kind === 'unfilled'}<small
                            >{paperUnfilledReason[paperEventData(event, 'reason') ?? ''] ??
                              '추가 관측 대기'}</small
                          >{/if}</th
                      ><td
                        >{paperEventData(event, 'symbol') ?? '—'}<small
                          >{event.intent_id ? shortId(event.intent_id) : '—'}</small
                        ></td
                      ><td class="numeric"
                        >{#if event.kind === 'simulated_fill'}{formatDecimal(
                            paperEventData(event, 'quantity'),
                          )} / {formatDecimal(paperEventData(event, 'price'))}{:else}—{/if}</td
                      ><td class="numeric"
                        >{#if event.kind === 'simulated_fill'}{formatDecimal(
                            paperEventData(event, 'fee'),
                          )} / {formatDecimal(paperEventData(event, 'tax'))}{:else}—{/if}</td
                      ><td class="numeric"
                        >{event.kind === 'simulated_fill'
                          ? formatDecimal(paperEventData(event, 'cash_delta'))
                          : '—'}</td
                      ><td
                        >{event.kind === 'simulated_fill'
                          ? formatTime(paperEventData(event, 'modeled_at'))
                          : '—'}</td
                      ><td>{formatTime(event.recorded_at)}</td><td
                        title={event.capture_id ?? paperEventData(event, 'capture_id') ?? undefined}
                        >{shortId(
                          event.capture_id ?? paperEventData(event, 'capture_id') ?? '—',
                        )}</td
                      ></tr
                    >{/each}</tbody
                >
              </table>
            </div>
            {#if book.omitted_event_count}<p class="muted">
                이전 기록 {book.omitted_event_count}개는 이번 조회에 포함되지 않았습니다.
              </p>{/if}
          </section>
        </section>
        <section class="capital-result" aria-label="대안 모의 주문">
          <h3>대안과 체결 가정</h3>
          <form onsubmit={submit} class="capital-form">
            <label class="capital-full"
              >모의 주문할 저장 계획<select
                bind:value={selectedPlanId}
                onchange={() => {
                  alternativeId = '';
                }}
                ><option value="">저장 계획을 선택하세요</option
                >{#each plans?.items ?? [] as item}<option value={item.id}
                    >{formatTime(item.recorded_at)} · {shortId(item.id)} · 대안 {item.alternative_count}개</option
                  >{/each}</select
              ></label
            >
            {#if plansQuery.isError}<p role="alert" class="error-state capital-full">
                {plansQuery.error.message}
              </p>{/if}
            {#if selectedPlanId && planQuery.isError}<p
                role="alert"
                class="error-state capital-full"
              >
                {planQuery.error.message}
              </p>{/if}
            {#if plan}
              {#if !compatiblePlan}<p class="warning-state capital-full">
                  이 계획의 계좌 또는 기록 구분이 선택한 모의 원장과 다릅니다. 같은 계좌의 계획을
                  선택해 주세요.
                </p>{/if}
              <label class="capital-full"
                >모의 주문 대안<select bind:value={alternativeId}
                  ><option value="">하나의 대안을 선택하세요</option
                  >{#each plan.record.request.alternatives as item}<option value={item.key}
                      >{item.label}</option
                    >{/each}</select
                ></label
              >
              {#if alternative}<div class="capital-full">
                  <p>{alternative.rationale}</p>
                  <div class="table-container">
                    <table>
                      <caption class="sr-only">선택한 모의 대안의 고정 계획</caption><thead
                        ><tr
                          ><th>종목·행동</th><th>수량</th><th>계획 가격</th><th
                            >수수료 bp / 고정 비용</th
                          ><th>세금 bp</th></tr
                        ></thead
                      ><tbody
                        >{#each alternative.legs as leg}<tr
                            ><th scope="row"
                              >{leg.symbol} / {leg.market}<small
                                >{capitalActionLabels[leg.action]} · {leg.currency}</small
                              ></th
                            ><td class="numeric">{formatDecimal(leg.quantity)}</td><td
                              class="numeric">{formatDecimal(leg.price)}</td
                            ><td class="numeric"
                              >{formatDecimal(leg.fee_bps)} / {formatDecimal(leg.fixed_fee)}</td
                            ><td class="numeric">{formatDecimal(leg.tax_bps)}</td></tr
                          >{/each}</tbody
                      >
                    </table>
                  </div>
                </div>{/if}
            {/if}
            <p class="muted capital-full">
              선택 후 최초 관측된 비수정 1분봉의 종가에 불리한 슬리피지를 적용합니다. 이 가정은 판단
              주기나 보유 기간을 정하지 않습니다. 100 bp = 1%입니다.
            </p>
            <label
              >불리한 슬리피지 (bp)<input
                bind:value={profile.slippage_bps}
                inputmode="decimal"
                placeholder="0 이상, 10,000 미만"
              /></label
            >
            <label
              >봉 거래량 참여율 (bp)<input
                bind:value={profile.participation_bps}
                inputmode="decimal"
                placeholder="0 초과, 10,000 이하"
              /></label
            >
            <label
              >모의 체결 수량 단위<input
                bind:value={profile.quantity_step}
                inputmode="decimal"
                placeholder="0보다 큰 수량"
              /></label
            >
            <p class="muted capital-full">
              계획 가격·슬리피지·비용으로 모의 현금을 미리 배정합니다. 이후 가격과 거래량에 따라
              일부만 체결되거나 대기할 수 있습니다. 고정 비용은 각 매매 항목의 첫 모의 체결에 한 번
              적용합니다.
            </p>
            <div class="capital-full">
              <Button type="submit" disabled={!alternative || !compatiblePlan || !!pending || busy}
                >가정 고정하고 모의 주문</Button
              >
            </div>
          </form>
        </section>
        <section class="capital-result" aria-label="후속 관측으로 모의 진행">
          <h3>후속 관측으로 진행</h3>
          <p class="muted">
            이미 저장된 비수정 분봉 중 1~20개를 선택합니다. 대안 선택 이후에 관측된 자료인지 서버가
            확인합니다.
          </p>
          <form onsubmit={advance} class="capital-form">
            <label class="capital-full"
              >모의 진행에 사용할 분봉 캡처<select multiple size="5" bind:value={captureIds}
                >{#each captures ?? [] as item}<option value={item.capture_id}
                    >{item.symbol} · {item.currencies.join('/')} · 확인 {formatTime(
                      item.retrieved_at,
                    )} · {shortId(item.capture_id)}</option
                  >{/each}</select
              ></label
            >
            {#if catalogQuery.isError}<p role="alert" class="error-state capital-full">
                {catalogQuery.error.message}
              </p>{:else if captures && !captures.length}<p class="muted capital-full">
                사용할 비수정 분봉 캡처가 없습니다.
              </p>{/if}
            {#if catalogQuery.data?.truncated_count}<p class="muted capital-full">
                시장 캡처 {catalogQuery.data.truncated_count}개는 이번 목록에서 생략됐습니다.
              </p>{/if}
            <div class="capital-full">
              <Button type="submit" disabled={!captures || !captureIds.length || !!pending || busy}
                >선택 관측으로 모의 진행</Button
              >
            </div>
          </form>
        </section>
      {/if}
    {/if}
  </details>
</section>
