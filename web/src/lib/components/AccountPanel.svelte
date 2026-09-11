<script lang="ts">
  import type { InvestmentContext } from '$lib/api/types.gen';
  import { formatDecimal, formatTime, shortId } from '$lib/format';
  let {
    context,
    loading = false,
    error,
  }: { context?: InvestmentContext; loading?: boolean; error?: string } = $props();
  let snapshot = $derived(context?.account?.snapshot);
  let freshness = $derived(context?.snapshot_freshness);
</script>

<section class="panel account-panel" aria-labelledby="account-heading" aria-busy={loading}>
  <div class="panel-heading">
    <h2 id="account-heading">계좌</h2>
    <span class="muted">관측 시점 기준</span>
  </div>
  {#if loading}
    <div class="empty-state" role="status">저장된 계좌 관측을 읽고 있습니다.</div>
  {:else if error}
    <div class="empty-state error-state" role="alert">{error}</div>
  {:else if freshness?.status === 'future'}
    <div class="empty-state warning-state">
      선택한 관측이 현재보다 미래여서 계좌 내용을 표시하지 않습니다.
    </div>
  {:else if !snapshot}
    <div class="empty-state">
      <strong>확인할 계좌 관측을 선택해 주세요.</strong>
      <p>저장된 시점의 보유 종목과 통화별 매수 가능 금액을 확인합니다.</p>
    </div>
  {:else}
    <div class="buying-power">
      {#each ['KRW', 'USD'] as currency}
        <div class="money-box">
          <span>매수 가능 금액 · {currency}</span>
          <strong class="numeric" data-testid={`buying-power-${currency}`}
            >{formatDecimal(snapshot.cash_buying_power[currency as 'KRW' | 'USD'])}</strong
          >
        </div>
      {/each}
    </div>
    <p class="muted account-note">현금 잔고 미확인 · 통화별 금액</p>
    {#if freshness?.status === 'stale'}
      <p class="inline-notice warning-state">
        오래된 관측입니다. 현재 계좌 상태로 간주하지 않습니다.
      </p>
    {/if}
    <h3>보유 종목</h3>
    {#if snapshot.holdings.items.length}
      <div class="table-container">
        <table class="holdings-table">
          <caption class="sr-only">선택한 관측 시점의 보유 종목</caption>
          <thead
            ><tr><th scope="col">종목</th><th scope="col">수량</th><th scope="col">평가액</th></tr
            ></thead
          >
          <tbody>
            {#each snapshot.holdings.items as holding}
              <tr>
                <th scope="row"
                  ><span>{holding.symbol} / {holding.marketCountry}</span><small
                    >{holding.name}</small
                  ></th
                >
                <td class="numeric">{formatDecimal(holding.quantity)}</td>
                <td class="numeric"
                  >{holding.currency} {formatDecimal(holding.marketValue.amount)}</td
                >
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {:else}<p class="muted small-empty">이 조회 범위에 보유 주식이 없습니다.</p>{/if}
    <p class="muted account-note">조회 범위 내 진행 중 주문 {snapshot.open_orders.length}건</p>
    <details class="subtle-details">
      <summary>관측 시각과 조회 범위</summary>
      <dl class="metadata-list">
        <dt>조회 완료</dt>
        <dd title={snapshot.collection_completed_at}>
          {formatTime(snapshot.collection_completed_at)}
        </dd>
        <dt>조회 시작</dt>
        <dd title={snapshot.collection_started_at}>{formatTime(snapshot.collection_started_at)}</dd>
        <dt>관측 상태</dt>
        <dd>
          {freshness?.status === 'fresh' ? '최근 저장 관측' : '오래된 저장 관측'} · 실시간 연결 아님
        </dd>
        <dt>원화 현금</dt>
        <dd>{formatDecimal(snapshot.cash_balances.KRW)}</dd>
        <dt>달러 현금</dt>
        <dd>{formatDecimal(snapshot.cash_balances.USD)}</dd>
        <dt>계좌 범위</dt>
        <dd>국내·미국 주식 · 채권·옵션 제외</dd>
        <dt>주문 범위</dt>
        <dd>API 지원 주문만 조회 · 일부 앱 주문과 조건주문 제외</dd>
        <dt>수집 특성</dt>
        <dd>개별 시각의 순차 관측 · 전체 미체결 약정 미확인</dd>
        <dt>기록 ID</dt>
        <dd class="identifier" title={context?.account?.id}>
          {shortId(context?.account?.id ?? '')}
        </dd>
      </dl>
      {#if snapshot.warnings.length}<p class="warning-state">
          검토 항목: {snapshot.warnings.join(' · ')}
        </p>{/if}
      <details class="raw-details">
        <summary>계좌 조회 데이터 보기</summary>
        <pre>{JSON.stringify(snapshot, null, 2)}</pre>
      </details>
    </details>
    {#if snapshot.open_orders.length}
      <details class="subtle-details">
        <summary>진행 중 주문 보기</summary>
        <div class="table-container">
          <table>
            <thead><tr><th>종목</th><th>상태</th><th>수량</th><th>주문 가격</th></tr></thead><tbody>
              {#each snapshot.open_orders as order}<tr
                  ><td>{order.symbol} · {order.side}</td><td>{order.status}</td><td class="numeric"
                    >{formatDecimal(order.quantity)}</td
                  ><td class="numeric">{order.currency} {formatDecimal(order.price)}</td></tr
                >{/each}
            </tbody>
          </table>
        </div>
      </details>
    {/if}
  {/if}
</section>
