<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { tick } from 'svelte';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import type {
    InvestmentContext,
    WorkflowCreate,
    WorkflowMutation,
    WorkflowObserve,
    WorkflowReconcile,
  } from '$lib/api/types.gen';
  import { fetchInvestigations } from '$lib/investigations';
  import { fetchSnapshots } from '$lib/queries';
  import { fetchBrokerScans } from '$lib/broker';
  import { fetchCapitalPlan, capitalActionLabels } from '$lib/capital';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import {
    fetchWorkflowProposal,
    fetchWorkflows,
    fetchWorkflow,
    createFlow,
    controlFlow,
    observeFlow,
    reconcileFlow,
    workflowRevision,
    workflowText,
    workflowObject,
    workflowMissingLabel,
    definiteWorkflowError,
    workflowStatusLabels,
    workflowStepLabels,
    workflowStepStateLabels,
    workflowReferenceLabels,
  } from '$lib/workflows';

  let {
    ready = false,
    jobsEnabled = false,
    synthetic = false,
    selectedSnapshot = '',
    context,
    onSnapshot,
  }: {
    ready?: boolean;
    jobsEnabled?: boolean;
    synthetic?: boolean;
    selectedSnapshot?: string;
    context?: InvestmentContext;
    onSnapshot: (id: string) => void;
  } = $props();
  let opened = $state(false);
  let investigationId = $state('');
  let alternativeId = $state('');
  let workflowId = $state('');
  let observationScanId = $state('');
  let afterSnapshotId = $state('');
  let beforeScanId = $state('');
  let afterScanId = $state('');
  type Control = 'advance' | 'pause' | 'resume' | 'recover';
  type Pending =
    | { kind: 'create'; body: WorkflowCreate; snapshotId: string; previousWorkflow: string }
    | { kind: 'control'; id: string; action: Control; body: WorkflowMutation }
    | { kind: 'observe'; id: string; body: WorkflowObserve }
    | { kind: 'reconcile'; id: string; body: WorkflowReconcile };
  let pending = $state<Pending | null>(null);
  let busy = $state(false);
  let feedback = $state('');
  let actionError = $state('');
  const investigationsQuery = createQuery(() => ({
    queryKey: ['investigations'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchInvestigations(signal),
  }));
  let investigations = $derived(
    ready && jobsEnabled && investigationsQuery.isSuccess && !investigationsQuery.isFetching
      ? investigationsQuery.data.items.filter(
          (item) =>
            item.latest_completed_revision !== null &&
            item.latest_completed_revision === item.current_revision,
        )
      : undefined,
  );
  let investigation = $derived(investigations?.find((item) => item.id === investigationId));
  const proposalQuery = createQuery(() => {
    const id = investigationId;
    const revision = investigation?.latest_completed_revision;
    return {
      queryKey: ['workflow-proposal', id, revision],
      enabled:
        ready && opened && jobsEnabled && !!id && revision !== undefined && revision !== null,
      queryFn: ({ signal }) => fetchWorkflowProposal(id, signal),
    };
  });
  const workflowsQuery = createQuery(() => ({
    queryKey: ['workflows'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchWorkflows(signal),
  }));
  const workflowQuery = createQuery(() => {
    const id = workflowId;
    return {
      queryKey: ['workflow', id],
      enabled: ready && opened && jobsEnabled && !!id,
      queryFn: ({ signal }) => fetchWorkflow(id, signal),
    };
  });
  const snapshotsQuery = createQuery(() => ({
    queryKey: ['snapshots'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchSnapshots(signal),
  }));
  let proposal = $derived(
    investigation &&
      proposalQuery.isSuccess &&
      !proposalQuery.isFetching &&
      proposalQuery.data.investigation_id === investigationId &&
      proposalQuery.data.investigation_revision === investigation.latest_completed_revision
      ? proposalQuery.data
      : undefined,
  );
  let account = $derived(
    ready && context?.account?.id === selectedSnapshot ? context.account : undefined,
  );
  let compatible = $derived(
    !!account &&
      !!proposal?.snapshot_id &&
      proposal.snapshot_id === selectedSnapshot &&
      proposal.account_seq === account.snapshot.account_seq &&
      proposal.mode === (synthetic ? 'synthetic' : 'prospective'),
  );
  let alternative = $derived(
    proposal?.capital_proposal?.alternatives.find((item) => item.key === alternativeId),
  );
  let actionable = $derived(!!alternative?.legs.some((item) => item.action !== 'hold'));
  let complete = $derived(
    !!alternative && !!proposal?.completeness.complete_alternative_keys.includes(alternative.key),
  );
  let workflows = $derived(
    ready && jobsEnabled && workflowsQuery.isSuccess && !workflowsQuery.isFetching
      ? workflowsQuery.data
      : undefined,
  );
  let workflow = $derived(
    workflows &&
      workflowQuery.isSuccess &&
      !workflowQuery.isFetching &&
      workflowQuery.data.id === workflowId
      ? workflowQuery.data
      : undefined,
  );
  const scansQuery = createQuery(() => {
    const seq = workflow?.account_seq ?? '';
    return {
      queryKey: ['broker-scans', seq],
      enabled: ready && opened && jobsEnabled && !!seq,
      queryFn: ({ signal }) => fetchBrokerScans(seq, signal),
    };
  });
  let scans = $derived(
    workflow && scansQuery.isSuccess && !scansQuery.isFetching
      ? scansQuery.data.items.filter(
          (item) => item.account_seq === workflow.account_seq && item.mode === workflow.mode,
        )
      : undefined,
  );
  let snapshots = $derived(
    workflow && snapshotsQuery.isSuccess && !snapshotsQuery.isFetching
      ? snapshotsQuery.data.items.filter((item) => item.account_seq === workflow.account_seq)
      : undefined,
  );
  let resultPlanId = $derived(
    workflow?.steps.map((item) => workflowText(item.result, 'plan_id')).find(Boolean) ?? '',
  );
  const planQuery = createQuery(() => {
    const id = resultPlanId;
    return {
      queryKey: ['capital-plan', id],
      enabled: ready && opened && jobsEnabled && !!id,
      queryFn: ({ signal }) => fetchCapitalPlan(id, signal),
    };
  });
  let plan = $derived(
    workflow &&
      resultPlanId &&
      planQuery.isSuccess &&
      !planQuery.isFetching &&
      planQuery.data.id === resultPlanId
      ? planQuery.data
      : undefined,
  );
  let intentId = $derived(
    workflow?.steps.map((item) => workflowText(item.result, 'intent_id')).find(Boolean),
  );
  const preparationSteps = ['funding_refresh', 'capital_plan', 'reservation', 'order_intent'];
  let canAdvance = $derived(
    workflow?.status === 'active' &&
      !workflow.steps.some((item) => ['running', 'needs_check'].includes(item.state)) &&
      (workflow.steps.some((item) => item.state === 'prepared') ||
        preparationSteps.some(
          (kind) =>
            !workflow.steps.some((item) => item.kind === kind && item.state === 'succeeded'),
        )),
  );
  let linkedScanId = $derived(
    workflow?.steps
      .toReversed()
      .filter((item) => item.kind === 'order_observation' && item.state === 'succeeded')
      .map((item) => workflowText(item.result, 'scan_id'))
      .find(Boolean),
  );
  let mutationBusy = $derived(busy || !!pending);
  function selectWorkflow() {
    observationScanId = '';
    afterSnapshotId = '';
    beforeScanId = '';
    afterScanId = '';
  }
  function refs(value: Record<string, unknown> | null | undefined) {
    return Object.entries(workflowReferenceLabels).flatMap(([key, label]) => {
      const text = workflowText(value, key);
      return text ? [{ key, label, text }] : [];
    });
  }
  function pools(value: Record<string, unknown> | null | undefined) {
    const rows = value?.pools;
    return Array.isArray(rows)
      ? rows.filter(
          (row): row is Record<string, unknown> =>
            !!row && typeof row === 'object' && !Array.isArray(row),
        )
      : [];
  }
  async function refresh() {
    await Promise.all([
      investigationsQuery.refetch(),
      workflowsQuery.refetch(),
      snapshotsQuery.refetch(),
    ]);
    await tick();
    await Promise.all([
      ...(investigation ? [proposalQuery.refetch()] : []),
      ...(workflowId ? [workflowQuery.refetch()] : []),
    ]);
    if (workflow) await scansQuery.refetch();
    if (resultPlanId) await planQuery.refetch();
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
          ? await createFlow(operation.body)
          : operation.kind === 'control'
            ? await controlFlow(operation.id, operation.action, operation.body)
            : operation.kind === 'observe'
              ? await observeFlow(operation.id, operation.body)
              : await reconcileFlow(operation.id, operation.body);
      pending = null;
      if (
        operation.kind === 'create' &&
        selectedSnapshot === operation.snapshotId &&
        investigationId === operation.body.investigation_id &&
        workflowId === operation.previousWorkflow
      ) {
        workflowId = result.id;
        selectWorkflow();
      }
      feedback = `운용 흐름 ${shortId(result.id)}의 처리 결과를 저장했습니다. 최신 단계를 조회합니다.`;
      await workflowsQuery.refetch();
      await tick();
      if (workflowId) await workflowQuery.refetch();
    } catch (error) {
      if (definiteWorkflowError(error)) pending = null;
      actionError = error instanceof Error ? error.message : '처리 결과를 확인하지 못했습니다.';
    } finally {
      busy = false;
    }
  }
  function prepare(make: () => Pending) {
    if (!ready || !jobsEnabled || mutationBusy) return;
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
    if (!workflow) throw new Error('운용 흐름의 최신 조회를 기다려 주세요.');
    return {
      request_key: crypto.randomUUID(),
      expected_revision: workflowRevision(workflow.revision),
    };
  }
  function create(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (
        !proposal ||
        !alternative ||
        !complete ||
        !actionable ||
        !compatible ||
        proposal.capital_context?.status !== 'available'
      )
        throw new Error('조사에 고정된 계좌, 완결된 대안과 저장된 사용자 예산을 확인해 주세요.');
      return {
        kind: 'create',
        snapshotId: selectedSnapshot,
        previousWorkflow: workflowId,
        body: {
          investigation_id: proposal.investigation_id,
          investigation_revision: workflowRevision(proposal.investigation_revision),
          alternative_id: alternative.key,
          request_key: crypto.randomUUID(),
        },
      };
    });
  }
  function control(action: Control) {
    prepare(() => {
      if (!workflow) throw new Error('운용 흐름을 선택해 주세요.');
      return { kind: 'control', id: workflow.id, action, body: mutation() };
    });
  }
  function observe(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (!workflow || !intentId || !scans?.some((item) => item.id === observationScanId))
        throw new Error('같은 계좌의 브로커 관측을 선택해 주세요.');
      return {
        kind: 'observe',
        id: workflow.id,
        body: { ...mutation(), scan_id: observationScanId },
      };
    });
  }
  function reconcile(event: SubmitEvent) {
    event.preventDefault();
    prepare(() => {
      if (
        !workflow ||
        !intentId ||
        !snapshots?.some((item) => item.id === afterSnapshotId) ||
        !scans?.some((item) => item.id === afterScanId) ||
        afterScanId !== linkedScanId ||
        (beforeScanId && !scans?.some((item) => item.id === beforeScanId))
      )
        throw new Error('이후 계좌 관측과 대조할 브로커 관측을 명시적으로 선택해 주세요.');
      return {
        kind: 'reconcile',
        id: workflow.id,
        body: {
          ...mutation(),
          after_snapshot_id: afterSnapshotId,
          before_scan_id: beforeScanId || null,
          after_scan_id: afterScanId,
        },
      };
    });
  }
</script>

<section class="panel capital-panel workflow-panel" aria-label="AI 운용 흐름">
  <details bind:open={opened}>
    <summary>AI 운용 흐름</summary>
    <p class="muted">
      AI의 대안을 저장 예산·자금 계산·배정·주문 의도로 연결합니다. 실제 주문 전송은 비활성입니다.
    </p>
    {#if !ready}<p class="empty-state">작업실 연결을 확인하고 있습니다.</p>
    {:else if !jobsEnabled}<p class="empty-state">
        이 작업실에서는 AI 운용 흐름 저장이 꺼져 있습니다. 기존 계좌와 연구 자료는 계속 조회할 수
        있습니다.
      </p>
    {:else}
      <div class="capital-actions">
        <Button variant="outline" size="sm" onclick={refresh} disabled={busy}
          ><RotateCw size={14} /> 운용 자료 재조회</Button
        ><span class="status-chip">실제 전송 비활성</span>
      </div>
      <details class="paper-create">
        <summary>완료된 AI 대안 연결하기</summary>
        <form class="capital-form" onsubmit={create}>
          <label class="capital-full"
            >운용에 연결할 완료 조사<select
              bind:value={investigationId}
              onchange={() => (alternativeId = '')}
              disabled={!investigations || mutationBusy}
              ><option value="">완료 조사 선택</option>{#each investigations ?? [] as item}<option
                  value={item.id}
                  >{item.context_input.purpose} · 결과 {item.latest_completed_revision}번 버전</option
                >{/each}</select
            ></label
          >
          {#if investigationsQuery.isError}<p class="error-message capital-full" role="alert">
              {investigationsQuery.error.message}
            </p>{/if}
          {#if investigationId && proposalQuery.isFetching}<p class="muted capital-full">
              선택한 조사의 고정 입력과 대안을 읽고 있습니다.
            </p>{:else if investigationId && proposalQuery.isError}<p
              class="error-message capital-full"
              role="alert"
            >
              {proposalQuery.error.message}
            </p>{/if}
          {#if proposal}
            <div class="capital-full workflow-source">
              <strong>조사에 고정된 기준</strong>
              <p>
                계좌 {proposal.account_seq ?? '미확인'} · {proposal.mode === 'synthetic'
                  ? '합성 자료'
                  : proposal.mode === 'retrospective'
                    ? '회고 자료'
                    : '전향적 자료'} · 결과 {proposal.investigation_revision}번 버전
              </p>
              <p class="muted">
                계좌 관측 {proposal.snapshot_id ? shortId(proposal.snapshot_id) : '미지정'} · 입력 {shortId(
                  proposal.input_id,
                )} · 결과 {shortId(proposal.output_id)} · 실행 {shortId(proposal.run_id)}
              </p>
              {#if proposal.snapshot_id && proposal.snapshot_id !== selectedSnapshot}<div
                  class="capital-actions"
                >
                  <Button
                    type="button"
                    variant="outline"
                    disabled={mutationBusy}
                    onclick={() => onSnapshot(proposal!.snapshot_id!)}>조사 계좌 관측 선택</Button
                  >
                </div>{/if}
              {#if !compatible}<p class="muted">
                  이 조사에 고정된 계좌 관측을 선택해야 합니다. 계좌가 없는 조사나 회고 결과는 주문
                  준비로 진행할 수 없습니다.
                </p>{/if}
            </div>
            <div class="capital-full">
              <h4>조사에 저장된 사용자 예산</h4>
              {#if proposal.capital_context?.status === 'available'}<div class="table-container">
                  <table>
                    <thead><tr><th>통화</th><th>계획 예산</th><th>유보 금액</th></tr></thead><tbody
                      >{#each proposal.capital_context.funding ?? [] as item}<tr
                          ><td>{item.currency}</td><td>{formatDecimal(item.limit_amount)}</td><td
                            >{formatDecimal(item.reserve_amount)}</td
                          ></tr
                        >{/each}</tbody
                    >
                  </table>
                </div>
                <p class="muted">
                  AI가 예산을 새로 허가하지 않습니다. 진행 시 서버의 최신 예산·배정을 확인합니다.
                  매수 가능 금액은 현금 잔고가 아닙니다.
                </p>
              {:else}<p class="muted">
                  {proposal.capital_context?.status === 'inconsistent'
                    ? '저장된 예산 기준이 서로 다릅니다.'
                    : '이 조사에 적용할 사용자 예산이 없습니다.'} 자금 계획에서 예산을 명시적으로 적용하고
                  새 조사 결과로 확인해 주세요.
                </p>{/if}
            </div>
            {#if proposal.capital_proposal}<label class="capital-full"
                >AI 제안 대안<select bind:value={alternativeId} disabled={mutationBusy}
                  ><option value="">대안 선택</option
                  >{#each proposal.capital_proposal.alternatives as item}<option value={item.key}
                      >{item.label} · {proposal.completeness.complete_alternative_keys.includes(
                        item.key,
                      )
                        ? '필수값 확인'
                        : '미확인 값 있음'}</option
                    >{/each}</select
                ></label
              >
            {:else}<p class="muted capital-full">
                이 결과에는 수량·가격·비용을 연결하는 V2 자금 제안이 없습니다. 기존 조사 내용은 AI
                조사에서 읽을 수 있습니다.
              </p>{/if}
            {#if alternative}
              <div class="capital-full">
                <h4>{alternative.label}</h4>
                <p class="muted">{alternative.rationale}</p>
                {#each proposal.completeness.incomplete_alternatives.filter((item) => item.key === alternativeId) as item}<p
                    class="workflow-unknown"
                  >
                    미확인: {item.missing_fields.map(workflowMissingLabel).join(', ')}
                  </p>{/each}
                <div class="table-container">
                  <table>
                    <thead
                      ><tr
                        ><th>종목</th><th>행동</th><th>수량</th><th>가정 가격</th><th
                          >수수료율 bp</th
                        ><th>고정 수수료</th><th>세율 bp</th></tr
                      ></thead
                    ><tbody
                      >{#each alternative.legs as item}<tr
                          ><td>{item.symbol} · {item.market}<small>{item.currency}</small></td><td
                            >{capitalActionLabels[item.action]}</td
                          ><td>{formatDecimal(item.quantity)}</td><td
                            >{formatDecimal(item.price)}</td
                          ><td>{formatDecimal(item.fee_bps)}</td><td
                            >{formatDecimal(item.fixed_fee)}</td
                          ><td>{formatDecimal(item.tax_bps)}</td></tr
                        >{/each}</tbody
                    >
                  </table>
                </div>
                {#each alternative.legs as item, index}<details class="workflow-rationale">
                    <summary>{index + 1}번 항목 · {item.symbol} 제안 근거</summary>
                    <p>{item.rationale}</p>
                    <p>수량: {item.sizing_rationale}</p>
                    <p>가격: {item.price_rationale}</p>
                    <p>비용: {item.cost_rationale}</p>
                    <p class="muted">
                      근거 {item.evidence_ids.length
                        ? item.evidence_ids.map(shortId).join(', ')
                        : '연결 없음'} · 시장 캡처 {item.capture_ids.length
                        ? item.capture_ids.map(shortId).join(', ')
                        : '연결 없음'}
                    </p>
                  </details>{/each}
                <p class="muted">
                  대안 안의 항목은 함께 계산합니다. 유지 전용 대안은 주문 준비로 진행하지 않습니다.
                  필수값이 있어도 현재 배정 가능 여부와 지원 주문 조건은 후속 단계에서 확인합니다.
                </p>
              </div>
            {/if}
            <div class="capital-full">
              <Button
                type="submit"
                disabled={!compatible ||
                  !complete ||
                  !actionable ||
                  proposal.capital_context?.status !== 'available' ||
                  mutationBusy}>대안으로 운용 흐름 만들기</Button
              >
            </div>
          {/if}
        </form>
      </details>
      <div class="capital-form">
        <label class="capital-full"
          >저장된 운용 흐름<select
            bind:value={workflowId}
            onchange={selectWorkflow}
            disabled={!workflows}
            ><option value="">운용 흐름 선택</option>{#each workflows?.items ?? [] as item}<option
                value={item.id}
                >계좌 {item.account_seq} · {shortId(item.id)} · {workflowStatusLabels[
                  item.status
                ]}</option
              >{/each}</select
          ></label
        >
      </div>
      {#if workflowsQuery.isError}<p class="error-message" role="alert">
          {workflowsQuery.error.message}
        </p>{:else if workflowsQuery.isFetching}<p class="empty-state">
          운용 흐름을 조회하고 있습니다.
        </p>{:else if workflows && !workflows.items.length}<p class="empty-state">
          저장된 운용 흐름이 없습니다.
        </p>{/if}
      {#if workflows?.omitted_count}<p class="muted">
          최근 {workflows.items.length}개 표시 · {workflows.omitted_count}개 생략
        </p>{/if}
      {#if workflowId && workflowQuery.isError}<p class="error-message" role="alert">
          {workflowQuery.error.message}
        </p>{:else if workflowId && workflowQuery.isFetching}<p class="empty-state">
          선택한 흐름의 최신 상태를 확인하고 있습니다.
        </p>{/if}
      {#if workflow}
        <section class="capital-result" aria-label="선택 운용 흐름">
          <div class="paper-intent-header">
            <h3>
              계좌 {workflow.account_seq} · {workflow.mode === 'synthetic'
                ? '합성 운용 흐름'
                : '전향적 운용 흐름'}
            </h3>
            <span class="status-chip"
              >{workflowStatusLabels[workflow.status]} · 버전 {workflow.revision}</span
            >
          </div>
          <p class="identifier muted">{workflow.id}</p>
          <p class="muted">
            기록 {formatTime(workflow.created_at)} · 최근 변경 {formatTime(workflow.updated_at)}
          </p>
          <dl class="workflow-references">
            {#each refs(workflow.seed) as item}<div>
                <dt>{item.label}</dt>
                <dd>{shortId(item.text)}</dd>
              </div>{/each}
          </dl>
          <div class="capital-actions">
            {#if canAdvance}<Button onclick={() => control('advance')} disabled={mutationBusy}
                >다음 단계 진행</Button
              >{/if}{#if workflow.status === 'active'}<Button
                variant="outline"
                onclick={() => control('pause')}
                disabled={mutationBusy}>운용 흐름 일시정지</Button
              >{:else if workflow.status === 'paused'}<Button
                variant="outline"
                onclick={() => control('resume')}
                disabled={mutationBusy}>운용 흐름 재개</Button
              >{/if}{#if workflow.status === 'attention' || workflow.steps.some( (item) => ['running', 'needs_check'].includes(item.state) )}<Button
                variant="outline"
                onclick={() => control('recover')}
                disabled={mutationBusy}>저장 결과 확인·복구</Button
              >{/if}
          </div>
          <p class="muted">
            한 번에 다음 단계 하나를 진행합니다. 일시정지는 기존 배정이나 주문을 해제하지 않습니다.
          </p>
          {#if intentId}<p class="workflow-notice">
              주문 의도 {shortId(intentId)}까지 연결됐습니다. 실제 전송은 비활성입니다.
              {#if workflow.status === 'completed'}대조 기록이 저장되었습니다. 주문 종료나 배정
                해제를 뜻하지 않으며 주문 관리에서 별도로 확인합니다.
              {:else}후속 관측과 대조 자료를 기다립니다.{/if}
            </p>{/if}
          {#each workflow.steps as step, index}
            <article class="paper-intent">
              <div class="paper-intent-header">
                <strong>{index + 1}. {workflowStepLabels[step.kind] ?? step.kind}</strong><span
                  class="status-chip">{workflowStepStateLabels[step.state]}</span
                >
              </div>
              <p class="muted">
                시도 {step.attempt_count}회 · 기록 {formatTime(step.created_at)} · 결과 기록 {formatTime(
                  step.completed_at,
                )}
              </p>
              {#if step.error_code}<p class="workflow-unknown">
                  처리 확인 필요 · {step.error_code}
                </p>{/if}
              <div class="workflow-step-grid">
                <div>
                  <h5>입력 참조</h5>
                  <dl class="workflow-references">
                    {#each refs(workflowObject(step.input, 'request')) as item}<div>
                        <dt>{item.label}</dt>
                        <dd>{shortId(item.text)}</dd>
                      </div>{/each}
                  </dl>
                </div>
                <div>
                  <h5>결과 참조</h5>
                  <dl class="workflow-references">
                    {#each refs(step.result) as item}<div>
                        <dt>{item.label}</dt>
                        <dd>{shortId(item.text)}</dd>
                      </div>{/each}
                  </dl>
                  {#if !step.result}<p class="muted">결과 미확인</p>{/if}
                </div>
              </div>
              {#if workflowObject(step.result, 'funding')}<div class="table-container">
                  <table>
                    <thead
                      ><tr
                        ><th>자원</th><th>통화</th><th>한도</th><th>배정</th><th>남은 여력</th></tr
                      ></thead
                    ><tbody
                      >{#each pools(workflowObject(step.result, 'funding')) as pool}<tr
                          ><td
                            >{workflowText(pool, 'kind') === 'holding'
                              ? `보유 수량 · ${workflowText(pool, 'symbol') ?? '종목 미확인'}`
                              : '계획 금액'}</td
                          ><td>{workflowText(pool, 'currency') ?? '미확인'}</td><td
                            >{formatDecimal(workflowText(pool, 'capacity'))}</td
                          ><td>{formatDecimal(workflowText(pool, 'reserved'))}</td><td
                            >{formatDecimal(workflowText(pool, 'available'))}</td
                          ></tr
                        >{/each}</tbody
                    >
                  </table>
                </div>{/if}
              <details class="workflow-rationale">
                <summary>단계 기록 정보</summary>
                <p class="muted">
                  기록 순번 {step.sequence} · {step.id} · 입력 {step.request_sha256}
                </p>
                <p class="muted">
                  최근 변경 {formatTime(step.updated_at)} · 처리 임대 종료 {formatTime(
                    step.lease_expires_at,
                  )}
                </p>
              </details>
            </article>
          {/each}
          {#if resultPlanId && planQuery.isError}<p class="error-message" role="alert">
              {planQuery.error.message}
            </p>{/if}
          {#if plan}<details class="paper-create">
              <summary>계산된 자금 요구</summary
              >{#each plan.record.calculation.alternatives as item}<h4 class="paper-subheading">
                  {item.label} · {item.eligibility === 'eligible'
                    ? '계산상 배정 가능'
                    : item.eligibility === 'blocked'
                      ? '배정 불가'
                      : '미확인'}
                </h4>
                {#each item.cash_requirements as money}<p>
                    필요 금액 {formatDecimal(money.amount)}
                    {money.currency}
                  </p>{/each}{#each item.holding_requirements as holding}<p>
                    필요 보유 {holding.symbol} · {holding.market} · {formatDecimal(
                      holding.quantity,
                    )}
                  </p>{/each}{#each item.blockers as blocker}<p class="muted">
                    {blocker}
                  </p>{/each}{/each}
              <p class="muted">매각 예상 대금은 먼저 쓸 수 있는 현금으로 합산하지 않습니다.</p>
            </details>{/if}
          {#if intentId && workflow.status === 'active'}
            <details class="paper-create">
              <summary>후속 관측과 대조</summary>
              <p class="muted">
                같은 계좌의 저장 자료를 선택합니다. 대조 이후 스캔을 먼저 이 흐름에 연결해 주세요.
                누적 변화는 신규 개별 체결이나 이번 판단의 실제 손익으로 해석하지 않습니다.
              </p>
              <form class="capital-form" onsubmit={observe}>
                <label class="capital-full"
                  >흐름에 연결할 브로커 관측<select
                    bind:value={observationScanId}
                    disabled={!scans || mutationBusy}
                    ><option value="">관측 선택</option>{#each scans ?? [] as item}<option
                        value={item.id}>{formatTime(item.recorded_at)} · {shortId(item.id)}</option
                      >{/each}</select
                  ></label
                >
                <div>
                  <Button
                    type="submit"
                    variant="outline"
                    disabled={!scans?.some((item) => item.id === observationScanId) || mutationBusy}
                    >흐름에 관측 연결</Button
                  >
                </div>
              </form>
              <form class="capital-form" onsubmit={reconcile}>
                <label class="capital-full"
                  >대조할 이후 계좌 관측<select
                    bind:value={afterSnapshotId}
                    disabled={!snapshots || mutationBusy}
                    ><option value="">이후 관측 선택</option>{#each snapshots ?? [] as item}<option
                        value={item.id}
                        >{formatTime(item.collection_completed_at)} · {shortId(item.id)}</option
                      >{/each}</select
                  ></label
                ><label
                  >대조 이전 브로커 관측<select
                    bind:value={beforeScanId}
                    disabled={!scans || mutationBusy}
                    ><option value="">기준 스캔 없음</option>{#each scans ?? [] as item}<option
                        value={item.id}>{formatTime(item.recorded_at)} · {shortId(item.id)}</option
                      >{/each}</select
                  ></label
                ><label
                  >대조 이후 브로커 관측<select
                    bind:value={afterScanId}
                    disabled={!scans || mutationBusy}
                    ><option value="">이후 스캔 선택</option>{#each scans ?? [] as item}<option
                        value={item.id}>{formatTime(item.recorded_at)} · {shortId(item.id)}</option
                      >{/each}</select
                  ></label
                >
                <div class="capital-full">
                  <Button
                    type="submit"
                    disabled={!snapshots?.some((item) => item.id === afterSnapshotId) ||
                      !scans?.some((item) => item.id === afterScanId) ||
                      afterScanId !== linkedScanId ||
                      mutationBusy}>선택 자료로 대조</Button
                  >
                </div>
              </form>
              {#if scansQuery.isError}<p class="error-message" role="alert">
                  {scansQuery.error.message}
                </p>{/if}{#if snapshotsQuery.isError}<p class="error-message" role="alert">
                  {snapshotsQuery.error.message}
                </p>{/if}
            </details>
          {/if}
        </section>
      {/if}
      {#if pending}<div class="paper-pending">
          <p class="muted">
            {busy
              ? '처리 결과를 확인하고 있습니다.'
              : '아직 결과가 확인되지 않았습니다. 원래 입력과 요청 식별자로 다시 확인합니다.'} 대상 {shortId(
              pending.kind === 'create' ? pending.body.investigation_id : pending.id,
            )}
          </p>
          {#if !busy}<Button variant="outline" onclick={runPending}>같은 운용 요청 확인</Button
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
