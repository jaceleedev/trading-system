<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { tick } from 'svelte';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    InvestmentContext,
    OrderIntentCreate,
    OrderModify,
    OrderCancel,
    OrderMutation,
    OrderObserve,
    OrderSimulate,
  } from '$lib/api/types.gen';
  import {
    fetchCapitalPlans,
    fetchCapitalPlan,
    fetchFunding,
    capitalActionLabels,
  } from '$lib/capital';
  import { fetchBrokerScans } from '$lib/broker';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import {
    fetchOrderIntents,
    fetchOrderIntent,
    createOrder,
    modifyOrder,
    cancelOrder,
    abortOrder,
    recoverOrder,
    observeOrder,
    simulateOrder,
    orderReservations,
    orderChangeFields,
    orderRevision,
    definiteOrderError,
    orderDeliveryLabels,
    orderObservationLabels,
    orderOperationLabels,
    syntheticOrderCases,
  } from '$lib/orders';

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
  let selectedPlanId = $state('');
  let alternativeId = $state('');
  let reservationId = $state('');
  let selectedIntentId = $state('');
  let selectedLeg = $state('');
  let scanId = $state('');
  let price = $state('');
  let quantity = $state('');
  let scenario = $state<OrderSimulate['scenario']>('accept');
  type Pending =
    | { kind: 'create'; body: OrderIntentCreate; snapshotId: string; previousIntent: string }
    | { kind: 'modify'; id: string; body: OrderModify }
    | { kind: 'cancel'; id: string; body: OrderCancel }
    | { kind: 'abort'; id: string; body: OrderMutation }
    | { kind: 'recover'; id: string; body: OrderMutation }
    | { kind: 'observe'; id: string; body: OrderObserve }
    | { kind: 'simulate'; id: string; operationId: string; body: OrderSimulate };
  let pending = $state<Pending | null>(null);
  let busy = $state(false);
  let feedback = $state('');
  let actionError = $state('');
  let account = $derived(
    ready && context?.account?.id === selectedSnapshot ? context.account : undefined,
  );
  const intentsQuery = createQuery(() => ({
    queryKey: ['order-intents'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchOrderIntents(signal),
  }));
  const intentQuery = createQuery(() => {
    const id = selectedIntentId;
    return {
      queryKey: ['order-intent', id],
      enabled: ready && opened && jobsEnabled && !!id,
      queryFn: ({ signal }) => fetchOrderIntent(id, signal),
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
  const fundingQuery = createQuery(() => {
    const seq = account?.snapshot.account_seq ?? '';
    const id = selectedSnapshot;
    return {
      queryKey: ['funding', seq, id],
      enabled: ready && opened && jobsEnabled && !!seq,
      queryFn: ({ signal }) => fetchFunding(seq, id, signal),
    };
  });
  let intents = $derived(
    ready && jobsEnabled && intentsQuery.isSuccess && !intentsQuery.isFetching
      ? intentsQuery.data
      : undefined,
  );
  let intent = $derived(
    intents &&
      intentQuery.isSuccess &&
      !intentQuery.isFetching &&
      intentQuery.data.id === selectedIntentId
      ? intentQuery.data
      : undefined,
  );
  const scansQuery = createQuery(() => {
    const seq = intent?.account_seq ?? '';
    return {
      queryKey: ['broker-scans', seq],
      enabled: ready && opened && jobsEnabled && !!seq,
      queryFn: ({ signal }) => fetchBrokerScans(seq, signal),
    };
  });
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
  let compatible = $derived(
    !!account &&
      !!plan &&
      plan.record.snapshot.account_seq === account.snapshot.account_seq &&
      plan.record.request.mode === (synthetic ? 'synthetic' : 'prospective'),
  );
  let alternative = $derived(
    plan?.record.request.alternatives.find((item) => item.key === alternativeId),
  );
  let funding = $derived(
    account &&
      fundingQuery.isSuccess &&
      !fundingQuery.isFetching &&
      fundingQuery.data.account_seq === account.snapshot.account_seq
      ? fundingQuery.data
      : undefined,
  );
  let reservations = $derived(
    funding && plan && compatible ? orderReservations(funding, plan, alternativeId) : [],
  );
  let reservation = $derived(reservations.find((item) => item.id === reservationId));
  let leg = $derived(intent?.legs.find((item) => String(item.index) === selectedLeg));
  let scans = $derived(
    intent && scansQuery.isSuccess && !scansQuery.isFetching
      ? scansQuery.data.items.filter(
          (item) => item.account_seq === intent.account_seq && item.mode === intent.mode,
        )
      : undefined,
  );
  let operationBusy = $derived(busy || !!pending);
  function selectIntent() {
    selectedLeg = '';
    scanId = '';
    price = '';
    quantity = '';
  }
  async function refresh() {
    await Promise.all([
      intentsQuery.refetch(),
      plansQuery.refetch(),
      ...(account ? [fundingQuery.refetch()] : []),
    ]);
    await Promise.all([
      ...(selectedIntentId ? [intentQuery.refetch()] : []),
      ...(selectedPlanId ? [planQuery.refetch()] : []),
    ]);
    if (intent) await scansQuery.refetch();
  }
  async function runPending() {
    if (!pending || busy || !ready || !jobsEnabled) return;
    const operation = pending;
    if (operation.kind === 'simulate' && !synthetic) return;
    busy = true;
    feedback = '';
    actionError = '';
    try {
      const result =
        operation.kind === 'create'
          ? await createOrder(operation.body)
          : operation.kind === 'modify'
            ? await modifyOrder(operation.id, operation.body)
            : operation.kind === 'cancel'
              ? await cancelOrder(operation.id, operation.body)
              : operation.kind === 'abort'
                ? await abortOrder(operation.id, operation.body)
                : operation.kind === 'recover'
                  ? await recoverOrder(operation.id, operation.body)
                  : operation.kind === 'observe'
                    ? await observeOrder(operation.id, operation.body)
                    : await simulateOrder(operation.id, operation.operationId, operation.body);
      pending = null;
      if (
        operation.kind === 'create' &&
        selectedSnapshot === operation.snapshotId &&
        selectedIntentId === operation.previousIntent
      ) {
        selectedIntentId = result.id;
        selectIntent();
      }
      feedback = `주문 의도 ${shortId(result.id)}의 처리 결과를 저장했습니다. 최신 상태를 조회합니다.`;
      await intentsQuery.refetch();
      await tick();
      if (selectedIntentId) await intentQuery.refetch();
      if (account) await fundingQuery.refetch();
    } catch (error) {
      if (definiteOrderError(error)) pending = null;
      actionError = error instanceof Error ? error.message : '처리 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  function prepare(make: () => Pending) {
    if (!ready || !jobsEnabled || operationBusy) return;
    actionError = '';
    feedback = '';
    try {
      pending = make();
      void runPending();
    } catch (error) {
      actionError = error instanceof Error ? error.message : '입력을 확인해 주세요.';
    }
  }
  function mutation() {
    if (!intent) throw new Error('주문 의도의 최신 조회를 기다려 주세요.');
    return { request_key: crypto.randomUUID(), expected_revision: orderRevision(intent.revision) };
  }
  function create(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (
        !account ||
        !plan ||
        !alternative ||
        !compatible ||
        !reservations.some((item) => item.id === reservationId)
      )
        throw new Error('선택 계좌의 계획·대안과 활성 배정을 선택해 주세요.');
      return {
        kind: 'create',
        snapshotId: selectedSnapshot,
        previousIntent: selectedIntentId,
        body: {
          plan_id: plan.id,
          alternative_id: alternative.key,
          reservation_id: reservationId,
          request_key: crypto.randomUUID(),
        },
      };
    });
  }
  function modify(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (!intent || !leg?.broker_order_ids.length)
        throw new Error('브로커 주문 ID가 확인된 항목을 선택해 주세요.');
      const fields = orderChangeFields(price, leg.leg.market === 'US' ? '' : quantity);
      if (leg.leg.market === 'KR' && fields.quantity === null)
        throw new Error('국내 주문의 정정 수량을 입력해 주세요.');
      return {
        kind: 'modify',
        id: intent.id,
        body: { ...mutation(), leg_index: leg.index, ...fields },
      };
    });
  }
  function cancel() {
    prepare(() => {
      if (!intent || !leg?.broker_order_ids.length)
        throw new Error('브로커 주문 ID가 확인된 항목을 선택해 주세요.');
      return { kind: 'cancel', id: intent.id, body: { ...mutation(), leg_index: leg.index } };
    });
  }
  function local(kind: 'abort' | 'recover') {
    prepare(() => {
      if (!intent) throw new Error('주문 의도를 선택해 주세요.');
      return { kind, id: intent.id, body: mutation() };
    });
  }
  function observe(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (!intent || !scans?.some((item) => item.id === scanId))
        throw new Error('같은 계좌의 저장된 브로커 관측을 선택해 주세요.');
      return { kind: 'observe', id: intent.id, body: { ...mutation(), scan_id: scanId } };
    });
  }
  function simulate(operationId: string) {
    prepare(() => {
      if (
        !synthetic ||
        !intent ||
        intent.mode !== 'synthetic' ||
        !intent.operations.some((item) => item.id === operationId && item.state === 'prepared')
      )
        throw new Error('합성 작업실의 미처리 의도만 검사할 수 있습니다.');
      return { kind: 'simulate', id: intent.id, operationId, body: { ...mutation(), scenario } };
    });
  }
</script>

<section class="panel capital-panel order-panel" aria-label="주문 의도 관리">
  <details bind:open={opened}>
    <summary>주문 의도 관리</summary>
    <p class="muted">저장 계획과 배정을 주문 의도로 연결합니다. 실제 주문 전송은 비활성입니다.</p>
    {#if !ready}<p class="empty-state">작업실 연결을 확인하고 있습니다.</p>
    {:else if !jobsEnabled}<p class="empty-state">
        이 작업실에서는 주문 의도 저장이 꺼져 있습니다. 기존 계좌와 연구 기록은 계속 조회할 수
        있습니다.
      </p>
    {:else}
      <div class="capital-actions">
        <Button variant="outline" size="sm" onclick={refresh} disabled={busy}
          ><RotateCw size={14} /> 주문 자료 재조회</Button
        ><span class="status-chip">실제 전송 비활성</span>
      </div>
      <details class="paper-create">
        <summary>계획에서 주문 의도 만들기</summary>
        <p class="muted">
          {#if account}선택 계좌 {account.snapshot.account_seq} · {formatTime(
              account.snapshot.collection_completed_at,
            )}{:else}상단에서 계좌 관측을 명시적으로 선택해 주세요.{/if}
        </p>
        <form class="capital-form" onsubmit={create}>
          <label
            >주문에 연결할 저장 계획<select
              bind:value={selectedPlanId}
              onchange={() => {
                alternativeId = '';
                reservationId = '';
              }}
              disabled={!plans || !account || operationBusy}
              ><option value="">계획 선택</option>{#each plans?.items ?? [] as item}<option
                  value={item.id}
                  >{shortId(item.id)} · {formatTime(item.recorded_at)} · {item.mode}</option
                >{/each}</select
            ></label
          >
          <label
            >주문 대안<select
              bind:value={alternativeId}
              onchange={() => (reservationId = '')}
              disabled={!plan || !compatible || operationBusy}
              ><option value="">대안 선택</option
              >{#each plan?.record.request.alternatives ?? [] as item}<option value={item.key}
                  >{item.label}</option
                >{/each}</select
            ></label
          >
          <label class="capital-full"
            >연결할 활성 배정<select
              bind:value={reservationId}
              disabled={!alternative || !funding || !compatible || operationBusy}
              ><option value="">배정 선택</option>{#each reservations as item}<option
                  value={item.id}>{shortId(item.id)} · {formatTime(item.created_at)}</option
                >{/each}</select
            ></label
          >
          {#if reservation}
            <div class="capital-full muted">
              <strong>선택한 배정</strong>
              {#each reservation.requirements.cash as item}<p>
                  계획 금액 {formatDecimal(item.amount)}
                  {item.currency}
                </p>{/each}
              {#each reservation.requirements.holdings as item}<p>
                  보유 수량 {item.symbol} · {item.market} · {formatDecimal(item.quantity)}
                </p>{/each}
            </div>
          {/if}
          {#if plansQuery.isError}<p class="error-message capital-full" role="alert">
              {plansQuery.error.message}
            </p>{/if}
          {#if selectedPlanId && planQuery.isError}<p
              class="error-message capital-full"
              role="alert"
            >
              {planQuery.error.message}
            </p>{/if}
          {#if account && fundingQuery.isError}<p class="error-message capital-full" role="alert">
              {fundingQuery.error.message}
            </p>{/if}
          {#if plan && !compatible}<p class="muted capital-full">
              현재 선택 계좌와 작업실 모드에 맞는 계획을 선택해 주세요. 회고 계획은 주문 의도로
              연결할 수 없습니다.
            </p>{/if}
          {#if alternative && compatible}
            <div class="capital-full table-container">
              <table>
                <thead
                  ><tr><th>종목</th><th>행동</th><th>수량</th><th>지정 가격</th><th>통화</th></tr
                  ></thead
                ><tbody
                  >{#each alternative.legs as item}<tr
                      ><td>{item.symbol} · {item.market}</td><td
                        >{capitalActionLabels[item.action]}</td
                      ><td>{formatDecimal(item.quantity)}</td><td>{formatDecimal(item.price)}</td
                      ><td>{item.currency}</td></tr
                    >{/each}</tbody
                >
              </table>
            </div>
            {#if funding && !reservations.length}<p class="muted capital-full">
                이 대안의 활성 배정이 없습니다. 자금 계획에서 먼저 배정해 주세요.
              </p>{/if}
          {/if}
          <p class="muted capital-full">
            지정가 · 당일 유효(LIMIT / DAY). 지원 수량과 가격은 저장 시 확인합니다. 이미 다른 주문에
            연결된 배정은 사용할 수 없으며, 연결 중에는 일반 배정 해제가 제한됩니다.
          </p>
          <div>
            <Button
              type="submit"
              disabled={!account ||
                !alternative ||
                !compatible ||
                !reservations.some((item) => item.id === reservationId) ||
                operationBusy}>주문 의도 저장</Button
            >
          </div>
        </form>
      </details>
      <div class="capital-form">
        <label class="capital-full"
          >저장된 주문 의도<select
            bind:value={selectedIntentId}
            onchange={selectIntent}
            disabled={!intents}
            ><option value="">주문 의도 선택</option>{#each intents?.items ?? [] as item}<option
                value={item.id}
                >계좌 {item.account_seq} · {shortId(item.id)} · {item.status === 'aborted'
                  ? '중단'
                  : '관리 중'}</option
              >{/each}</select
          ></label
        >
      </div>
      {#if intentsQuery.isError}<p class="error-message" role="alert">
          {intentsQuery.error.message}
        </p>{:else if intentsQuery.isFetching}<p class="empty-state">
          주문 의도를 조회하고 있습니다.
        </p>{:else if intents && !intents.items.length}<p class="empty-state">
          저장된 주문 의도가 없습니다.
        </p>{/if}
      {#if intents?.omitted_count}<p class="muted">
          최근 {intents.items.length}개 표시 · 전체 {intents.total_count}개 중 {intents.omitted_count}개
          생략
        </p>{/if}
      {#if selectedIntentId && intentQuery.isError}<p class="error-message" role="alert">
          {intentQuery.error.message}
        </p>{:else if selectedIntentId && intentQuery.isFetching}<p class="empty-state">
          선택한 주문의 최신 상태를 확인하고 있습니다.
        </p>{/if}
      {#if intent}
        <section class="capital-result" aria-label="선택 주문 의도">
          <div class="paper-intent-header">
            <h3>
              계좌 {intent.account_seq} · {intent.mode === 'synthetic'
                ? '합성 주문 의도'
                : '전향적 주문 의도'}
            </h3>
            <span class="status-chip"
              >{intent.status === 'aborted' ? '의도 중단' : '관리 중'} · 버전 {intent.revision}</span
            >
          </div>
          <p class="identifier muted">{intent.id}</p>
          <p class="muted">
            계획 {shortId(intent.plan_id)} · 대안 {intent.alternative_id} · 배정 {shortId(
              intent.reservation_id,
            )}
          </p>
          <p class="muted">
            기록 {formatTime(intent.created_at)} · 최근 변경 {formatTime(intent.updated_at)} · {intent.reservation_held
              ? '연결 배정 유지'
              : '연결 배정 해제'}
          </p>
          <h4 class="paper-subheading">주문 항목과 브로커 관측</h4>
          <p class="muted">
            전달 응답만으로 체결을 확정하지 않습니다. 아래 상태와 누적 수량은 연결한 브로커 관측의
            기록입니다.
          </p>
          {#each intent.legs as item}
            <article class="paper-intent">
              <div class="paper-intent-header">
                <strong
                  >{item.index + 1}. {item.leg.symbol} · {item.leg.market} · {capitalActionLabels[
                    item.leg.action
                  ]}</strong
                ><span class="status-chip">{orderObservationLabels[item.observation_state]}</span>
              </div>
              <p>
                계획 수량 {formatDecimal(item.leg.quantity)} · 계획 가격 {formatDecimal(
                  item.leg.price,
                )}
                {item.leg.currency}
              </p>
              <p class="muted">
                브로커 주문 ID: {item.broker_order_ids.length
                  ? item.broker_order_ids.join(', ')
                  : '미확인'}
              </p>
              {#if item.observation}
                <p class="muted">
                  관측 주문 수량 {formatDecimal(item.observation.order.quantity)} · 관측 지정 가격 {formatDecimal(
                    item.observation.order.price,
                  )}
                  {item.observation.order.currency} · {item.observation.order.orderType} / {item
                    .observation.order.timeInForce}
                </p>
                <p class="muted">주문 생성 {formatTime(item.observation.order.orderedAt)}</p>
                <p>
                  관측 상태 {item.observation.order.status} · 누적 체결 수량 {formatDecimal(
                    item.observation.order.execution.filledQuantity,
                  )} · 누적 체결 금액 {formatDecimal(item.observation.order.execution.filledAmount)}
                  {item.observation.order.currency}
                </p>
                <p class="muted">
                  관측 {formatTime(item.observation.observed_at)} · 출처 스캔 {shortId(
                    item.observation.scan_id,
                  )} · 마지막 체결 시각 {formatTime(item.observation.order.execution.filledAt)}
                </p>
                <p class="muted">
                  누적 수수료 {formatDecimal(item.observation.order.execution.commission)} · 누적 세금
                  {formatDecimal(item.observation.order.execution.tax)}
                  {item.observation.order.currency}
                </p>
              {:else}<p class="muted">연결된 주문 상태 관측이 없습니다.</p>{/if}
            </article>
          {/each}
          <h4 class="paper-subheading">전달 처리 이력</h4>
          {#if synthetic && intent.mode === 'synthetic'}<div class="order-synthetic">
              <strong>합성 주문 응답 검사</strong>
              <p class="muted">
                외부 전송 없이 고정 응답을 적용합니다. 승인 응답은 실제 체결 증거가 아닙니다.
              </p>
              <label
                >합성 응답 유형<select bind:value={scenario} disabled={operationBusy}
                  >{#each Object.entries(syntheticOrderCases) as [key, label]}<option value={key}
                      >{label}</option
                    >{/each}</select
                ></label
              >
            </div>{/if}
          {#each intent.operations as operation}
            <article class="paper-intent">
              <div class="paper-intent-header">
                <strong
                  >{operation.leg_index + 1}번 항목 · {orderOperationLabels[operation.kind]}</strong
                ><span class="status-chip"
                  >{operation.outcome?.error_code === 'synthetic_before_send_failure'
                    ? '전송 전 실패'
                    : orderDeliveryLabels[operation.state]}</span
                >
              </div>
              <p class="muted">
                기록 {formatTime(operation.created_at)} · 변경 {formatTime(operation.updated_at)}
              </p>
              {#if operation.outcome}<p>
                  응답 주문 ID {operation.outcome.order_id ?? '미확인'} · 응답 코드 {operation
                    .outcome.http_status ?? '미확인'}
                </p>
                {#if operation.outcome.error_code}<p class="muted">
                    응답 구분 {operation.outcome.error_code}
                  </p>{/if}{/if}
              {#if operation.state === 'ambiguous'}<p class="muted">
                  처리 결과가 미확정입니다. 재전송하지 않고 주문 ID와 후속 관측으로 확인해야 합니다.
                </p>{/if}
              <details>
                <summary>저장된 요청 내용</summary>
                <dl class="order-request">
                  {#each Object.entries(operation.prepared.body) as [key, value]}<div>
                      <dt>{key}</dt>
                      <dd>{String(value)}</dd>
                    </div>{/each}
                </dl>
                <p class="muted">
                  요청 {shortId(operation.prepared.request_sha256)} · 실행 준비 미확인
                </p>
              </details>
              {#if synthetic && intent.mode === 'synthetic' && operation.state === 'prepared'}<div
                  class="capital-actions"
                >
                  <Button
                    variant="outline"
                    disabled={operationBusy}
                    onclick={() => simulate(operation.id)}>이 의도에 합성 응답 적용</Button
                  >
                </div>{/if}
            </article>
          {/each}
          {#if intent.status === 'active'}
            <details class="paper-create">
              <summary>정정·취소 의도</summary>
              <p class="muted">
                브로커 주문 ID가 확인된 항목에 로컬 의도를 접수합니다. 미국 주문은 가격만, 국내
                주문은 가격과 수량을 입력합니다. 기존 배정을 늘리는 정정은 제한됩니다.
              </p>
              <form class="capital-form" onsubmit={modify}>
                <label class="capital-full"
                  >정정·취소 대상<select
                    bind:value={selectedLeg}
                    onchange={() => {
                      price = '';
                      quantity = '';
                    }}
                    disabled={operationBusy}
                    ><option value="">항목 선택</option
                    >{#each intent.legs.filter((item) => item.broker_order_ids.length) as item}<option
                        value={String(item.index)}
                        >{item.index + 1}. {item.leg.symbol} · {item.leg.market}</option
                      >{/each}</select
                  ></label
                ><label
                  >정정 가격<input
                    bind:value={price}
                    inputmode="decimal"
                    disabled={!leg || operationBusy}
                  /></label
                >{#if leg?.leg.market === 'KR'}<label
                    >정정 수량<input
                      bind:value={quantity}
                      inputmode="decimal"
                      disabled={operationBusy}
                    /></label
                  >{:else if leg}<p class="muted">
                    미국 주문의 수량 변경은 지원하지 않습니다.
                  </p>{/if}
                <div class="capital-actions capital-full">
                  <Button
                    type="submit"
                    variant="outline"
                    disabled={!leg?.broker_order_ids.length || operationBusy}>정정 의도 저장</Button
                  ><Button
                    type="button"
                    variant="outline"
                    onclick={cancel}
                    disabled={!leg?.broker_order_ids.length || operationBusy}>취소 의도 저장</Button
                  >
                </div>
              </form>
            </details>
          {/if}
          <form class="capital-form" onsubmit={observe}>
            <label class="capital-full"
              >연결할 브로커 관측<select bind:value={scanId} disabled={!scans || operationBusy}
                ><option value="">저장 스캔 선택</option>{#each scans ?? [] as item}<option
                    value={item.id}
                    >{formatTime(item.recorded_at)} · {shortId(item.id)} · {item.orders_count}개
                    주문</option
                  >{/each}</select
              ></label
            >
            <div>
              <Button
                type="submit"
                variant="outline"
                disabled={!scans?.some((item) => item.id === scanId) || operationBusy}
                >선택 관측 연결</Button
              >
            </div>
          </form>
          {#if scansQuery.isError}<p class="error-message" role="alert">
              {scansQuery.error.message}
            </p>{/if}
          <div class="capital-actions">
            {#if intent.operations.some((item) => item.state === 'dispatching')}<Button
                variant="outline"
                onclick={() => local('recover')}
                disabled={operationBusy}>중단된 처리를 미확정으로 기록</Button
              >{/if}{#if intent.status === 'active' && intent.operations.every( (item) => ['prepared', 'aborted', 'rejected'].includes(item.state) )}<Button
                variant="outline"
                onclick={() => local('abort')}
                disabled={operationBusy}>전달 전 의도 중단</Button
              >{/if}
          </div>
          <details class="paper-create">
            <summary>기록 이력 ({intent.event_total_count})</summary
            >{#if intent.event_omitted_count}<p class="muted">
                오래된 기록 {intent.event_omitted_count}개 생략
              </p>{/if}
            <ol class="order-events">
              {#each intent.events as event}<li>
                  {event.kind} · {formatTime(event.recorded_at)}<small
                    >{event.operation_id ? shortId(event.operation_id) : '주문 의도 전체'}</small
                  >
                </li>{/each}
            </ol>
          </details>
        </section>
      {/if}
      {#if pending}<div class="paper-pending">
          <p class="muted">
            {busy
              ? '처리 결과를 확인하고 있습니다.'
              : '이 요청의 결과가 아직 확인되지 않았습니다. 같은 요청 식별자로 다시 확인합니다.'} 대상
            {pending.kind === 'create' ? shortId(pending.body.plan_id) : shortId(pending.id)}
          </p>
          {#if !busy}<Button variant="outline" onclick={runPending}>같은 주문 요청 확인</Button
            >{/if}
        </div>{/if}
      {#if actionError}<p class="error-message" role="alert">{actionError}</p>{/if}{#if feedback}<p
          class="muted"
          role="status"
        >
          {feedback}
        </p>{/if}
    {/if}
  </details>
</section>
