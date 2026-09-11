<script lang="ts">
  import { createQuery, useQueryClient } from '@tanstack/svelte-query';
  import { ChartLine, RotateCw } from '@lucide/svelte';
  import { Button } from '$lib/components/ui/button';
  import AccountPanel from '$lib/components/AccountPanel.svelte';
  import ResearchList from '$lib/components/ResearchList.svelte';
  import RecordDetail from '$lib/components/RecordDetail.svelte';
  import JobsPanel from '$lib/components/JobsPanel.svelte';
  import { fetchContext, fetchHealth, fetchRecord, fetchSnapshots } from '$lib/queries';
  import { formatTime } from '$lib/format';

  let selectedSnapshot = $state('');
  let selectedRecordId = $state<string | null>(null);
  let maxRecords = $state(50);
  const queryClient = useQueryClient();
  const health = createQuery(() => ({
    queryKey: ['health'],
    queryFn: ({ signal }) => fetchHealth(signal),
  }));
  const snapshots = createQuery(() => ({
    queryKey: ['snapshots'],
    queryFn: ({ signal }) => fetchSnapshots(signal),
  }));
  const contextQuery = createQuery(() => {
    const snapshotId = selectedSnapshot;
    const limit = maxRecords;
    return {
      queryKey: ['context', snapshotId, limit],
      queryFn: ({ signal }) => fetchContext(snapshotId, limit, signal),
    };
  });
  const recordQuery = createQuery(() => {
    const recordId = selectedRecordId;
    return {
      queryKey: ['record', recordId],
      enabled: !!recordId,
      queryFn: ({ signal }) => fetchRecord(recordId!, signal),
    };
  });
  let context = $derived(
    health.isSuccess &&
      contextQuery.isSuccess &&
      !contextQuery.isFetching &&
      contextQuery.data.snapshot_freshness.snapshot_id === (selectedSnapshot || null)
      ? contextQuery.data
      : undefined,
  );
  let selectedRecord = $derived(
    health.isSuccess &&
      recordQuery.isSuccess &&
      !recordQuery.isFetching &&
      recordQuery.data.id === selectedRecordId
      ? recordQuery.data
      : undefined,
  );
  let activeSnapshot = $derived(
    snapshots.isSuccess && !snapshots.isFetching
      ? snapshots.data.items.find((item) => item.id === selectedSnapshot)
      : undefined,
  );
  let isReading = $derived(
    health.isFetching || snapshots.isFetching || contextQuery.isFetching || recordQuery.isFetching,
  );

  function selectSnapshot(id: string) {
    selectedRecordId = null;
    selectedSnapshot = id;
    queryClient.removeQueries({ queryKey: ['record'] });
  }
  function selectRecord(id: string) {
    selectedRecordId = id;
  }
  async function reload() {
    selectedRecordId = null;
    await queryClient.cancelQueries();
    await queryClient.resetQueries();
  }
</script>

<svelte:head
  ><title>투자 작업실 · Trading Research</title><meta
    name="description"
    content="저장된 계좌 관측과 투자 조사, 판단의 근거를 이어 읽는 개인 작업실"
  /></svelte:head
>

<a class="skip-link" href="#workbench">작업실로 이동</a>
<header class="site-header">
  <div class="header-inner">
    <a href="/" class="brand"
      ><ChartLine size={30} strokeWidth={1.7} aria-hidden="true" /><span>Trading Research</span></a
    ><span class="header-description">개인 투자 작업실</span>
  </div>
</header>
<main id="workbench">
  <div class="page-heading">
    <div>
      <h1>투자 작업실</h1>
      <p>계좌와 판단의 연결을 확인합니다.</p>
    </div>
    <Button variant="outline" class="reload-button" onclick={reload} disabled={isReading}
      ><RotateCw size={15} aria-hidden="true" class={isReading ? 'reading-icon' : ''} />저장 자료
      다시 읽기</Button
    >
  </div>
  {#if health.isError}<div role="alert" class="top-error">
      작업실 연결을 확인할 수 없습니다. {health.error.message}
    </div>{/if}
  <div class="snapshot-bar">
    <label for="snapshot-select">계좌 관측</label>
    <select
      id="snapshot-select"
      value={selectedSnapshot}
      onchange={(event) => selectSnapshot(event.currentTarget.value)}
      disabled={snapshots.isFetching || snapshots.isError}
    >
      <option value="">계좌 관측 선택</option>
      {#if snapshots.isSuccess && !snapshots.isFetching}{#each snapshots.data.items as snapshot}<option
            value={snapshot.id}
            >계좌 {snapshot.account_seq} · {snapshot.account_type} · {formatTime(
              snapshot.collection_completed_at,
            )}</option
          >{/each}{/if}
    </select>
    {#if activeSnapshot}<time
        class="snapshot-time muted"
        datetime={activeSnapshot.collection_completed_at}
        title={activeSnapshot.collection_completed_at}
        >{formatTime(activeSnapshot.collection_completed_at)}</time
      >{:else if snapshots.isSuccess && !snapshots.data.items.length}<span class="muted"
        >저장된 계좌 관측이 없습니다.</span
      >{/if}
    {#if health.isSuccess && health.data.synthetic}<span class="synthetic-label">합성 예시</span
      >{/if}
    {#if snapshots.isError}<span class="error-state" role="alert">{snapshots.error.message}</span
      >{/if}
  </div>
  <div class="workbench-columns">
    <AccountPanel
      context={snapshots.isSuccess && !snapshots.isFetching ? context : undefined}
      loading={contextQuery.isFetching || snapshots.isFetching || health.isFetching}
      error={contextQuery.isError
        ? contextQuery.error.message
        : snapshots.isError
          ? snapshots.error.message
          : health.isError
            ? health.error.message
            : undefined}
    />
    <ResearchList
      {context}
      loading={contextQuery.isFetching}
      error={contextQuery.isError ? contextQuery.error.message : undefined}
      selectedId={selectedRecordId}
      onselect={selectRecord}
      {maxRecords}
      onlimit={(value) => {
        selectedRecordId = null;
        maxRecords = value;
      }}
    />
  </div>
  <RecordDetail
    item={selectedRecord}
    knownRecords={context?.records ?? []}
    knownSnapshotIds={snapshots.isSuccess ? snapshots.data.items.map((item) => item.id) : []}
    loading={!!selectedRecordId && recordQuery.isFetching}
    error={selectedRecordId && recordQuery.isError ? recordQuery.error.message : undefined}
    onselect={selectRecord}
    onSnapshot={selectSnapshot}
  />
  <JobsPanel {selectedSnapshot} ready={health.isSuccess && !health.isFetching} />
  <footer class="page-footer">
    <p>저장된 관측과 연구 기록을 읽는 작업실입니다.</p>
    {#if context}<p class="context-time" title={context.generated_at}>
        문맥 조회 {formatTime(context.generated_at)}
      </p>{/if}
  </footer>
</main>
