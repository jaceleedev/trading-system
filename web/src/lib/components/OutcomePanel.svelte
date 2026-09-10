<script lang="ts">
  import { createQuery } from '@tanstack/svelte-query';
  import { RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import { fetchPaperBooks } from '$lib/paper';
  import { fetchWorkflows, workflowStatusLabels } from '$lib/workflows';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  import { capitalActionLabels } from '$lib/capital';
  import { reconciliationLabels } from '$lib/broker';
  import { orderDeliveryLabels } from '$lib/orders';
  import {
    fetchOutcomes,
    fetchOutcome,
    createOutcomeReport,
    outcomeTime,
    outcomeSelection,
    outcomeRatio,
    outcomeNote,
    definiteOutcomeError,
  } from '$lib/outcomes';

  let {
    ready = false,
    jobsEnabled = false,
    synthetic = false,
  }: { ready?: boolean; jobsEnabled?: boolean; synthetic?: boolean } = $props();
  let opened = $state(false);
  let bookIds = $state<string[]>([]);
  let workflowIds = $state<string[]>([]);
  let start = $state('');
  let end = $state('');
  let reportId = $state('');
  let busy = $state(false);
  let actionError = $state('');
  let feedback = $state('');
  type CreateBody = Parameters<typeof createOutcomeReport>[0];
  let pending = $state<{ body: CreateBody; draft: string; previousReport: string } | null>(null);
  let mode = $derived(synthetic ? ('synthetic' as const) : ('prospective' as const));
  let draftKey = $derived(JSON.stringify({ bookIds, workflowIds, start, end, mode }));
  const localZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const booksQuery = createQuery(() => ({
    queryKey: ['paper-books'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchPaperBooks(signal),
  }));
  const flowsQuery = createQuery(() => ({
    queryKey: ['workflows'],
    enabled: ready && opened && jobsEnabled,
    queryFn: ({ signal }) => fetchWorkflows(signal),
  }));
  const reportsQuery = createQuery(() => ({
    queryKey: ['outcomes'],
    enabled: ready && opened,
    queryFn: ({ signal }) => fetchOutcomes(signal),
  }));
  const reportQuery = createQuery(() => {
    const id = reportId;
    return {
      queryKey: ['outcome', id],
      enabled: ready && opened && !!id,
      queryFn: ({ signal }) => fetchOutcome(id, signal),
    };
  });
  let books = $derived(
    ready && jobsEnabled && booksQuery.isSuccess && !booksQuery.isFetching
      ? booksQuery.data.items.filter((item) => item.mode === mode)
      : undefined,
  );
  let flows = $derived(
    ready && jobsEnabled && flowsQuery.isSuccess && !flowsQuery.isFetching
      ? flowsQuery.data.items.filter((item) => item.mode === mode)
      : undefined,
  );
  let reports = $derived(
    ready && reportsQuery.isSuccess && !reportsQuery.isFetching ? reportsQuery.data : undefined,
  );
  let selectedReport = $derived(
    reports && reportQuery.isSuccess && !reportQuery.isFetching && reportQuery.data.id === reportId
      ? reportQuery.data
      : undefined,
  );
  let record = $derived(selectedReport?.record);
  let sourcesValid = $derived(
    (bookIds.length > 0 || workflowIds.length > 0) &&
      bookIds.every((id) => books?.some((book) => book.id === id)) &&
      workflowIds.every((id) => flows?.some((flow) => flow.id === id)),
  );

  async function refresh() {
    feedback = '';
    await Promise.allSettled([
      reportsQuery.refetch(),
      ...(jobsEnabled ? [booksQuery.refetch(), flowsQuery.refetch()] : []),
      ...(reportId ? [reportQuery.refetch()] : []),
    ]);
  }
  async function execute() {
    if (!pending || busy || !ready || !jobsEnabled) return;
    const original = pending;
    busy = true;
    actionError = '';
    feedback = '';
    try {
      const saved = await createOutcomeReport(original.body);
      pending = null;
      feedback = `기간별 결과 보고서를 저장했습니다. ${shortId(saved.id)}`;
      if (draftKey === original.draft && reportId === original.previousReport) reportId = saved.id;
      await reportsQuery.refetch();
      if (reportId === saved.id) await reportQuery.refetch();
    } catch (error) {
      actionError = error instanceof Error ? error.message : '처리 결과를 확인하지 못했습니다.';
      if (definiteOutcomeError(error)) pending = null;
    } finally {
      busy = false;
    }
  }
  function calculate(event: SubmitEvent) {
    event.preventDefault();
    if (busy || pending || !ready || !jobsEnabled) return;
    actionError = '';
    try {
      if (!sourcesValid)
        throw new Error('비교할 모의 원장 또는 운용 흐름을 명시적으로 선택해 주세요.');
      const startAt = outcomeTime(start, '시작 시각');
      const endAt = outcomeTime(end, '종료 시각');
      if (!startAt) throw new Error('시작 시각을 입력해 주세요.');
      if (endAt && new Date(startAt).getTime() >= new Date(endAt).getTime())
        throw new Error('종료 시각은 시작 시각 이후여야 합니다.');
      pending = {
        body: {
          book_ids: outcomeSelection(bookIds, '모의 원장'),
          workflow_ids: outcomeSelection(workflowIds, '운용 흐름'),
          start_at: startAt,
          end_at: endAt,
          mode,
          request_key: crypto.randomUUID(),
        },
        draft: draftKey,
        previousReport: reportId,
      };
      void execute();
    } catch (error) {
      actionError = error instanceof Error ? error.message : '입력을 확인해 주세요.';
    }
  }
</script>

<section class="panel capital-panel outcome-panel" aria-label="기간별 결과 비교">
  <details bind:open={opened}>
    <summary>기간별 결과 비교</summary>
    <p class="muted">
      모의 원장의 평가 변화와 운용 흐름의 브로커 관측을 기간별로 읽습니다. 원장·통화별 결과를
      합산하지 않습니다.
    </p>
    {#if !ready}<p class="empty-state">작업실 연결을 확인하고 있습니다.</p>
    {:else}
      <div class="capital-actions">
        <Button variant="outline" size="sm" onclick={refresh} disabled={busy}
          ><RotateCw size={14} /> 결과 자료 재조회</Button
        ><span class="status-chip">실제 손익 미계산</span>
      </div>
      {#if !jobsEnabled}<p class="empty-state">
          이 작업실에서는 새 결과 계산이 꺼져 있습니다. 저장된 보고서는 계속 읽을 수 있습니다.
        </p>
      {:else}
        <details class="paper-create">
          <summary>자료와 기간 선택하기</summary>
          <form class="capital-form" onsubmit={calculate}>
            <fieldset class="outcome-selection">
              <legend>모의 원장 · 최대 4개</legend>
              {#if booksQuery.isFetching}<p class="muted">
                  모의 원장을 읽고 있습니다.
                </p>{:else if booksQuery.isError}<p class="error-message" role="alert">
                  {booksQuery.error.message}
                </p>{:else if !books?.length}<p class="muted">선택할 모의 원장이 없습니다.</p>{/if}
              {#each books ?? [] as book}<label
                  ><input
                    type="checkbox"
                    bind:group={bookIds}
                    value={book.id}
                    disabled={!bookIds.includes(book.id) && bookIds.length >= 4}
                  /><span
                    >{book.label}<small
                      >계좌 {book.account_seq} · {shortId(book.id)} · 생성 {formatTime(
                        book.created_at,
                      )}</small
                    ></span
                  ></label
                >{/each}
              {#if booksQuery.data?.omitted_count}<p class="muted">
                  목록 범위 밖 원장 {booksQuery.data.omitted_count}개
                </p>{/if}
            </fieldset>
            <fieldset class="outcome-selection">
              <legend>운용 흐름 · 최대 4개</legend>
              {#if flowsQuery.isFetching}<p class="muted">
                  운용 흐름을 읽고 있습니다.
                </p>{:else if flowsQuery.isError}<p class="error-message" role="alert">
                  {flowsQuery.error.message}
                </p>{:else if !flows?.length}<p class="muted">선택할 운용 흐름이 없습니다.</p>{/if}
              {#each flows ?? [] as flow}<label
                  ><input
                    type="checkbox"
                    bind:group={workflowIds}
                    value={flow.id}
                    disabled={!workflowIds.includes(flow.id) && workflowIds.length >= 4}
                  /><span
                    >계좌 {flow.account_seq} · {shortId(flow.id)}<small
                      >{workflowStatusLabels[flow.status]} · 생성 {formatTime(
                        flow.created_at,
                      )}</small
                    ></span
                  ></label
                >{/each}
              {#if flowsQuery.data?.omitted_count}<p class="muted">
                  목록 범위 밖 운용 흐름 {flowsQuery.data.omitted_count}개
                </p>{/if}
            </fieldset>
            <label
              >시작 시각<input type="datetime-local" step="1" bind:value={start} required /></label
            >
            <label>종료 시각 · 선택<input type="datetime-local" step="1" bind:value={end} /></label>
            <p class="muted capital-full">
              입력 시간대: {localZone}. 시작 시각의 상태와 종료까지의 기록을 비교합니다. 종료 공란은
              최초 처리 시 서버 시각으로 고정됩니다. 전략의 보유 기간을 정하는 설정은 아닙니다.
            </p>
            <div class="capital-actions capital-full">
              <Button type="submit" disabled={!sourcesValid || !start || busy || !!pending}
                >기간 결과 계산·저장</Button
              ><span class="muted">{synthetic ? '합성 자료 비교' : '전향적 기록 비교'}</span>
            </div>
          </form>
        </details>
      {/if}
      {#if pending}<div class="workflow-notice" role="status">
          <p>
            선택 자료와 기간을 고정한 요청의 처리 결과를 확인 중입니다. 다른 자료로 새로 계산하기
            전에 원래 요청을 확인해 주세요.
          </p>
          <Button variant="outline" onclick={execute} disabled={busy || !jobsEnabled}
            >같은 계산 요청 결과 확인</Button
          >
        </div>{/if}
      {#if actionError}<p class="error-message" role="alert">{actionError}</p>{/if}
      {#if feedback}<p class="feedback" role="status">{feedback}</p>{/if}
      <div class="capital-result">
        <h3>저장된 결과 보고서</h3>
        <label
          >결과 보고서 선택<select bind:value={reportId} disabled={!reports}
            ><option value="">보고서 선택</option>{#each reports?.items ?? [] as report}<option
                value={report.id}
                >{formatTime(report.start_at)} → {formatTime(report.end_at)} · 모의 {report.book_count}
                / 운용 {report.workflow_count} · {shortId(report.id)}</option
              >{/each}</select
          ></label
        >
        {#if reportsQuery.isFetching}<p class="muted">
            보고서 목록을 읽고 있습니다.
          </p>{:else if reportsQuery.isError}<p class="error-message" role="alert">
            {reportsQuery.error.message}
          </p>{:else if !reports?.items.length}<p class="empty-state">
            저장된 결과 보고서가 없습니다.
          </p>{/if}
        {#if reports?.omitted_count || reports?.invalid_count}<p class="muted">
            목록 범위 밖 {reports?.omitted_count ?? 0}개 · 검증할 수 없는 기록 {reports?.invalid_count ??
              0}개
          </p>{/if}
        {#if reportId && reportQuery.isFetching}<p class="muted">
            선택한 결과 보고서를 읽고 있습니다.
          </p>{:else if reportId && reportQuery.isError}<p class="error-message" role="alert">
            {reportQuery.error.message}
          </p>{/if}
        {#if record}<section class="outcome-report" aria-label="선택 결과 보고서">
            <div class="detail-heading">
              <h3>기록 기간의 결과</h3>
              <span class="status-chip">{record.mode === 'synthetic' ? '합성' : '전향적'}</span>
            </div>
            <dl class="workflow-references">
              <div>
                <dt>기간 · 시작 제외, 종료 포함</dt>
                <dd>{formatTime(record.start_at)} → {formatTime(record.end_at)}</dd>
              </div>
              <div>
                <dt>보고서 기록</dt>
                <dd>{formatTime(record.recorded_at)}</dd>
              </div>
              <div>
                <dt>보고서 ID</dt>
                <dd title={selectedReport!.id}>{shortId(selectedReport!.id)}</dd>
              </div>
              <div>
                <dt>고정 입력</dt>
                <dd title={record.input_id}>{shortId(record.input_id)}</dd>
              </div>
            </dl>
            <p class="workflow-notice">
              모의 평가 변화, 과거 취득원가 기준 손익, 브로커 누적 변화는 서로 다른 값입니다. 이
              보고서는 실제 손익이나 AI의 인과적 성과를 확정하지 않습니다.
            </p>
            <h3 class="outcome-source">모의 원장별 결과</h3>
            {#if !record.paper.length}<p class="muted">
                이 보고서에 선택한 모의 원장이 없습니다.
              </p>{/if}
            {#each record.paper as paper}<section
                class="outcome-source"
                aria-label={`모의 결과 ${paper.book_id}`}
              >
                <h4>계좌 {paper.account_seq} · 모의 원장 {shortId(paper.book_id)}</h4>
                <p class="muted">
                  시스템 기록 기준 {formatTime(paper.window.start_at)} → {formatTime(
                    paper.window.end_at,
                  )} · 기록 순번 {paper.window.start_sequence} → {paper.window.end_sequence}
                </p>
                <p class="muted">
                  모의 체결 {paper.counts.fills} · 대안 접수 {paper.counts.submissions} · 취소 {paper
                    .counts.cancellations} · 미체결 기록 {paper.counts.unfilled} · 평가 관측 {paper
                    .counts.observations}
                </p>
                <div class="table-container">
                  <table>
                    <caption>원장별·통화별 모의 평가 변화</caption><thead
                      ><tr
                        ><th>통화</th><th>시작 평가액</th><th>종료 평가액</th><th>평가 변화</th><th
                          >단순 수익 비율</th
                        ><th>수수료</th><th>세금</th><th>슬리피지 비용</th></tr
                      ></thead
                    ><tbody
                      >{#each paper.currencies as amount}<tr
                          ><th>{amount.currency}</th><td class="numeric"
                            >{formatDecimal(amount.start_equity)}</td
                          ><td class="numeric">{formatDecimal(amount.end_equity)}</td><td
                            class="numeric">{formatDecimal(amount.equity_delta)}</td
                          ><td class="numeric"
                            >{outcomeRatio(
                              amount.simple_return,
                            )}{#if amount.return_unknown_reason}<small
                                >{amount.return_unknown_reason === 'equity_unknown'
                                  ? '평가액 미확인'
                                  : '시작 평가액이 양수가 아님'}</small
                              >{/if}</td
                          ><td class="numeric">{formatDecimal(amount.fees)}</td><td class="numeric"
                            >{formatDecimal(amount.taxes)}</td
                          ><td class="numeric">{formatDecimal(amount.slippage_cost)}</td></tr
                        >{/each}</tbody
                    >
                  </table>
                </div>
                <p class="muted">
                  수익 비율 0.1 = 10%. 비용은 모의 계산 가정이며 슬리피지는 기준 가격과의
                  차이입니다. 통화 환산이나 여러 원장 합계는 제공하지 않습니다.
                </p>
                <details class="paper-create">
                  <summary>평가 구성과 미확인 항목</summary>
                  {#each paper.currencies as amount}<div class="outcome-method">
                      <h5>{amount.currency} 모의 평가 구성</h5>
                      <dl class="workflow-references">
                        <div>
                          <dt>모의 현금 · 시작 → 종료</dt>
                          <dd>
                            {formatDecimal(amount.start_cash)} → {formatDecimal(amount.end_cash)}
                          </dd>
                        </div>
                        <div>
                          <dt>보유 평가액 · 시작 → 종료</dt>
                          <dd>
                            {formatDecimal(amount.start_position_value)} → {formatDecimal(
                              amount.end_position_value,
                            )}
                          </dd>
                        </div>
                        <div>
                          <dt>모의 현금 변화</dt>
                          <dd>{formatDecimal(amount.cash_delta)}</dd>
                        </div>
                        <div>
                          <dt>모의 체결 현금 변화</dt>
                          <dd>{formatDecimal(amount.fill_cash_delta)}</dd>
                        </div>
                        <div>
                          <dt>현금 계산 반올림 잔차</dt>
                          <dd>{formatDecimal(amount.cash_rounding_residual)}</dd>
                        </div>
                        <div>
                          <dt>비용 계산 반올림 잔차</dt>
                          <dd>{formatDecimal(amount.cost_rounding_residual)}</dd>
                        </div>
                        <div>
                          <dt>원가 손익 계산 반올림 잔차</dt>
                          <dd>{formatDecimal(amount.realized_rounding_residual)}</dd>
                        </div>
                      </dl>
                      <h5>과거 취득원가 기준 · AI 판단 이후 성과와 구분</h5>
                      <dl class="workflow-references">
                        <div>
                          <dt>실현 손익</dt>
                          <dd>{formatDecimal(amount.historical_cost_realized.amount)}</dd>
                        </div>
                        <div>
                          <dt>원가가 알려진 부분의 실현 손익</dt>
                          <dd>{formatDecimal(amount.historical_cost_realized.known_amount)}</dd>
                        </div>
                        <div>
                          <dt>원가 미확인 매도</dt>
                          <dd>{amount.historical_cost_realized.unknown_sales}건</dd>
                        </div>
                        <div>
                          <dt>평가 손익 · 시작 → 종료</dt>
                          <dd>
                            {formatDecimal(amount.start_unrealized_pnl)} → {formatDecimal(
                              amount.end_unrealized_pnl,
                            )}
                          </dd>
                        </div>
                      </dl>
                      <p class="muted">
                        가격 미확인 · 시작: {amount.start_missing_price_symbols.join(', ') ||
                          '없음'} / 종료: {amount.end_missing_price_symbols.join(', ') || '없음'}
                      </p>
                      <p class="muted">
                        원가 미확인 · 시작: {amount.start_unknown_cost_symbols.join(', ') || '없음'} /
                        종료: {amount.end_unknown_cost_symbols.join(', ') || '없음'}
                      </p>
                    </div>{/each}
                  <p class="muted">
                    원장 계산 반올림 {paper.coverage.source_arithmetic_rounded ? '있음' : '없음'} · 보고서
                    계산 반올림 {paper.coverage.report_arithmetic_rounded ? '있음' : '없음'} · 생성 후
                    외부 모의 입출금 미지원
                  </p>
                </details>
                <details class="paper-create">
                  <summary>종목·행동별 모의 체결과 평가 가격</summary>
                  {#if paper.actions.length}<div class="table-container">
                      <table>
                        <caption>종목·통화·행동별 모의 체결</caption><thead
                          ><tr
                            ><th>종목 / 행동</th><th>통화</th><th>건수 / 수량</th><th>체결 금액</th
                            ><th>현금 변화</th><th>수수료 / 세금</th><th>슬리피지</th><th
                              >알려진 원가의 실현 손익</th
                            ></tr
                          ></thead
                        ><tbody
                          >{#each paper.actions as action}<tr
                              ><th
                                >{action.symbol}<small
                                  >{action.market} · {capitalActionLabels[action.action]}</small
                                ></th
                              ><td>{action.currency}</td><td class="numeric"
                                >{action.fill_count}건 / {formatDecimal(action.quantity)}</td
                              ><td class="numeric">{formatDecimal(action.notional)}</td><td
                                class="numeric">{formatDecimal(action.cash_delta)}</td
                              ><td class="numeric"
                                >{formatDecimal(action.fees)} / {formatDecimal(action.taxes)}</td
                              ><td class="numeric">{formatDecimal(action.slippage_cost)}</td><td
                                class="numeric"
                                >{formatDecimal(action.known_realized_pnl)}<small
                                  >원가 미확인 매도 {action.unknown_realized_sales}건</small
                                ></td
                              ></tr
                            >{/each}</tbody
                        >
                      </table>
                    </div>{:else}<p class="muted">기간 안에 기록된 모의 체결이 없습니다.</p>{/if}
                  {#each [{ label: '시작 평가 가격', items: paper.marks.start }, { label: '종료 평가 가격', items: paper.marks.end }] as marks}<div
                      class="table-container"
                    >
                      <table>
                        <caption>{marks.label}</caption><thead
                          ><tr
                            ><th>종목</th><th>통화 / 가격</th><th>가격 기준 봉 종료</th><th
                              >원본 관측 시각</th
                            ><th>캡처</th></tr
                          ></thead
                        ><tbody
                          >{#each marks.items as mark}<tr
                              ><th>{mark.symbol} · {mark.market}</th><td class="numeric"
                                >{mark.currency} {formatDecimal(mark.price)}</td
                              ><td>{formatTime(mark.period_end)}</td><td
                                >{formatTime(mark.observed_at)}</td
                              ><td title={mark.capture_id}>{shortId(mark.capture_id)}</td></tr
                            >{:else}<tr><td colspan="5">저장된 평가 가격 없음</td></tr
                            >{/each}</tbody
                        >
                      </table>
                    </div>{/each}
                </details>
              </section>{/each}
            <h3 class="outcome-source">브로커 관측 · 실제 손익과 구분</h3>
            {#if !record.broker.length}<p class="muted">
                이 보고서에 선택한 운용 흐름이 없습니다.
              </p>{/if}
            {#each record.broker as broker}<section
                class="outcome-source"
                aria-label={`브로커 결과 ${broker.workflow_id}`}
              >
                <h4>계좌 {broker.account_seq} · 운용 흐름 {shortId(broker.workflow_id)}</h4>
                <dl class="workflow-references">
                  <div>
                    <dt>운용 상태</dt>
                    <dd>
                      {workflowStatusLabels[
                        broker.workflow_status as keyof typeof workflowStatusLabels
                      ] ?? broker.workflow_status}
                    </dd>
                  </div>
                  <div>
                    <dt>주문 의도</dt>
                    <dd title={broker.intent_id ?? undefined}>
                      {broker.intent_id ? shortId(broker.intent_id) : '미연결'}
                    </dd>
                  </div>
                  <div>
                    <dt>자금 배정</dt>
                    <dd>
                      {broker.reservation_held === null
                        ? '미확인'
                        : broker.reservation_held
                          ? '유지 중'
                          : '해제됨'}
                    </dd>
                  </div>
                  <div>
                    <dt>전달 처리 상태</dt>
                    <dd>
                      {broker.operation_states
                        .map(
                          (state) =>
                            orderDeliveryLabels[state as keyof typeof orderDeliveryLabels] ?? state,
                        )
                        .join(', ') || '없음'}
                    </dd>
                  </div>
                  <div>
                    <dt>실제 손익 / 외부 현금 흐름 / 환율 손익</dt>
                    <dd>미확인 / 미확인 / 미확인</dd>
                  </div>
                </dl>
                <p class="muted">
                  개별 체결 ID와 거래 출처를 확인할 수 없습니다. 누적 변화는 새 실제 체결이나 수동
                  매매로 단정하지 않습니다.
                </p>
                {#each broker.warnings as warning}<p class="workflow-unknown">
                    {outcomeNote(warning)}
                  </p>{/each}
                {#if !broker.comparisons.length}<p class="empty-state">
                    선택 기간에 연결된 대조 결과가 없습니다.
                  </p>{/if}
                {#each broker.comparisons as comparison}<div class="outcome-method">
                    <h5 title={comparison.reconciliation_id}>
                      대조 {shortId(comparison.reconciliation_id)}
                    </h5>
                    <p class="muted">
                      이전 계좌 {formatTime(comparison.before_snapshot_at)} → 이후 계좌 {formatTime(
                        comparison.after_snapshot_at,
                      )} · 대조 기준 {formatTime(comparison.as_of)}
                    </p>
                    <p
                      class={comparison.period_matches_requested_window
                        ? 'muted'
                        : 'workflow-unknown'}
                    >
                      {comparison.period_matches_requested_window
                        ? '대조 관측 기간이 요청 기간과 일치합니다.'
                        : '대조 관측 기간이 요청 기간과 다릅니다. 전체 대조 값을 요청 기간의 체결로 나누어 추정하지 않습니다.'}
                    </p>
                    <div class="table-container">
                      <table>
                        <caption>브로커 누적 관측 변화</caption><thead
                          ><tr
                            ><th>주문 / 종목 / 통화</th><th>관측 분류</th><th>누적 수량 변화</th><th
                              >누적 금액 변화</th
                            ><th>수수료 변화</th><th>세금 변화</th></tr
                          ></thead
                        ><tbody
                          >{#each comparison.orders as order}<tr
                              ><th title={order.order_id}
                                >{shortId(order.order_id)}<small
                                  >{(order.after ?? order.before)?.order.symbol ?? '종목 미확인'} · {(
                                    order.after ?? order.before
                                  )?.order.currency ?? '통화 미확인'}</small
                                ></th
                              ><td
                                >{order.classification
                                  .map((value) => reconciliationLabels[value] ?? value)
                                  .join(', ')}<small>출처 미연결</small></td
                              ><td class="numeric">{formatDecimal(order.deltas.filled_quantity)}</td
                              ><td class="numeric">{formatDecimal(order.deltas.filled_amount)}</td
                              ><td class="numeric">{formatDecimal(order.deltas.commission)}</td><td
                                class="numeric">{formatDecimal(order.deltas.tax)}</td
                              ></tr
                            >{:else}<tr><td colspan="6">비교한 주문 관측 없음</td></tr
                            >{/each}</tbody
                        >
                      </table>
                    </div>
                    <details class="paper-create">
                      <summary>보유·매수 가능 금액과 관측 시각</summary>
                      <div class="table-container">
                        <table>
                          <caption>보유 변화 · 주문과의 연결 미확인</caption><thead
                            ><tr
                              ><th>종목 / 시장</th><th>이전 통화 / 이후 통화</th><th>이전 수량</th
                              ><th>이후 수량</th><th>수량 변화</th></tr
                            ></thead
                          ><tbody
                            >{#each comparison.holdings as holding}<tr
                                ><th>{holding.symbol} · {holding.market}</th><td
                                  >{holding.before_currency ?? '미확인'} / {holding.after_currency ??
                                    '미확인'}</td
                                ><td class="numeric">{formatDecimal(holding.before_quantity)}</td
                                ><td class="numeric">{formatDecimal(holding.after_quantity)}</td><td
                                  class="numeric">{formatDecimal(holding.quantity_delta)}</td
                                ></tr
                              >{:else}<tr><td colspan="5">비교한 보유 관측 없음</td></tr
                              >{/each}</tbody
                          >
                        </table>
                      </div>
                      <div class="table-container">
                        <table>
                          <caption>매수 가능 금액 변화 · 현금 잔고와 구분</caption><thead
                            ><tr><th>통화</th><th>이전</th><th>이후</th><th>변화</th></tr></thead
                          ><tbody
                            >{#each comparison.buying_power as power}<tr
                                ><th>{power.currency}</th><td class="numeric"
                                  >{formatDecimal(power.before_amount)}</td
                                ><td class="numeric">{formatDecimal(power.after_amount)}</td><td
                                  class="numeric">{formatDecimal(power.delta)}</td
                                ></tr
                              >{:else}<tr><td colspan="4">비교한 매수 가능 금액 없음</td></tr
                              >{/each}</tbody
                          >
                        </table>
                      </div>
                      {#each comparison.orders as order}<div class="outcome-method">
                          <h5>주문 {shortId(order.order_id)}</h5>
                          {#each [{ label: '이전', version: order.before }, { label: '이후', version: order.after }] as entry}{#if entry.version}<p
                                class="muted"
                              >
                                {entry.label} 관측 {formatTime(entry.version.observed_at)} · 기록 {formatTime(
                                  entry.version.recorded_at,
                                )} · 누적 수량 {formatDecimal(
                                  entry.version.order.execution.filledQuantity,
                                )} · 누적 금액 {formatDecimal(
                                  entry.version.order.execution.filledAmount,
                                )}
                              </p>{:else}<p class="muted">{entry.label} 관측 미확인</p>{/if}{/each}
                        </div>{/each}
                    </details>
                  </div>{/each}
              </section>{/each}
            <h3 class="outcome-source">판단 방식과 출처 연결</h3>
            <p class="muted">
              계획에 연결된 판단 기록을 표시합니다. 요청한 모델 이름은 실제 실행 모델의 정체를
              확인한 결과가 아닙니다.
            </p>
            {#if !record.methods.length}<p class="empty-state">
                연결된 판단 방식 기록이 없습니다.
              </p>{/if}
            {#each record.methods as method}<article class="outcome-method">
                <h4>{method.purpose}</h4>
                <p class="muted">
                  {method.source_kind === 'decision' ? '저장 판단' : 'AI 조사 결과'} · 출력 스키마 {method.output_schema_version ??
                    '미확인'}
                </p>
                <dl class="workflow-references">
                  <div>
                    <dt>자금 계획</dt>
                    <dd title={method.plan_id}>{shortId(method.plan_id)}</dd>
                  </div>
                  <div>
                    <dt>{method.source_kind === 'decision' ? '판단 기록' : 'AI 출력'}</dt>
                    <dd title={method.source_id}>{shortId(method.source_id)}</dd>
                  </div>
                  <div>
                    <dt>조사 입력</dt>
                    <dd title={method.input_id ?? undefined}>
                      {method.input_id ? shortId(method.input_id) : '미확인'}
                    </dd>
                  </div>
                  <div>
                    <dt>실행 기록 연결</dt>
                    <dd>
                      {method.run_selection === 'unique'
                        ? '단일 실행 기록'
                        : method.run_selection === 'ambiguous'
                          ? '여러 실행 후보 · 확정 불가'
                          : '실행 기록 미확인'}
                    </dd>
                  </div>
                  <div>
                    <dt>요청 모델 / 추론 설정</dt>
                    <dd>
                      {method.requested_model ?? '미지정 또는 미확인'} / {method.requested_reasoning_effort ??
                        '미지정 또는 미확인'}
                    </dd>
                  </div>
                  <div>
                    <dt>실행 CLI 버전</dt>
                    <dd>{method.cli_version ?? '미확인'}</dd>
                  </div>
                  <div>
                    <dt>실제 모델 정체</dt>
                    <dd>미확인</dd>
                  </div>
                </dl>
                <details class="paper-create">
                  <summary>실행·스키마·적용 자료 참조</summary>
                  <dl class="workflow-references">
                    <div>
                      <dt>실행 기록</dt>
                      <dd>
                        {#each method.run_ids as id}<p title={id}>
                            {shortId(id)}
                          </p>{:else}미확인{/each}
                      </dd>
                    </div>
                    <div>
                      <dt>조사 지침 해시</dt>
                      <dd title={method.instructions_sha256 ?? undefined}>
                        {method.instructions_sha256
                          ? shortId(method.instructions_sha256)
                          : '미확인'}
                      </dd>
                    </div>
                    <div>
                      <dt>출력 스키마 해시</dt>
                      <dd title={method.output_schema_sha256 ?? undefined}>
                        {method.output_schema_sha256
                          ? shortId(method.output_schema_sha256)
                          : '미확인'}
                      </dd>
                    </div>
                    <div>
                      <dt>모의 원장</dt>
                      <dd>{method.book_ids.map(shortId).join(', ') || '없음'}</dd>
                    </div>
                    <div>
                      <dt>운용 흐름</dt>
                      <dd>{method.workflow_ids.map(shortId).join(', ') || '없음'}</dd>
                    </div>
                  </dl>
                </details>
              </article>{/each}
            <section class="outcome-source" aria-label="비교 조건과 한계">
              <h3>비교 조건과 한계</h3>
              <dl class="workflow-references">
                <div>
                  <dt>동일 초기 원장 설정 · 계좌·관측 포함</dt>
                  <dd>
                    {record.comparison.same_initial_paper_seed === null
                      ? '미확인'
                      : record.comparison.same_initial_paper_seed
                        ? '같음'
                        : '다름'}
                  </dd>
                </div>
                <div>
                  <dt>동일 모의 체결 가정</dt>
                  <dd>
                    {record.comparison.same_paper_profiles === null
                      ? '미확인'
                      : record.comparison.same_paper_profiles
                        ? '같음'
                        : '다름'}
                  </dd>
                </div>
                <div>
                  <dt>여러 판단 방식이 섞인 원장</dt>
                  <dd>
                    {record.comparison.mixed_methods_book_ids.map(shortId).join(', ') || '없음'}
                  </dd>
                </div>
                <div>
                  <dt>기존 보유에서 시작한 원장</dt>
                  <dd>
                    {record.comparison.initial_holdings_book_ids.map(shortId).join(', ') || '없음'}
                  </dd>
                </div>
              </dl>
              <p class="workflow-notice">
                자동 우승 모델이나 합산 손익을 정하지 않습니다. 같은 기간에도 초기 자산·체결
                가정·시장 자료와 여러 판단의 영향이 다를 수 있습니다.
              </p>
              {#each record.comparison.limitations as limitation}<p class="workflow-unknown">
                  {outcomeNote(limitation)}
                </p>{/each}
            </section>
          </section>{/if}
      </div>
    {/if}
  </details>
</section>
