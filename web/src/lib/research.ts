import type { ResearchResponse } from './api/types.gen';

export type RecordKind = ResearchResponse['record']['kind'];
export const kindLabels: Record<RecordKind, string> = {
  evidence: '근거',
  hypothesis: '가설',
  decision: '판단',
  review: '재검토',
};
export const modeLabels = {
  prospective: '앞으로의 판단',
  retrospective: '사후 분석',
  synthetic: '합성 시연',
};
export const actionLabels = {
  buy: '매수 검토',
  add: '추가 매수 검토',
  trim: '축소 검토',
  sell: '매도 검토',
  hold: '보유',
  avoid: '회피',
  research: '추가 조사',
  watch: '관찰',
  wait: '대기',
};
export const judgmentLabels = {
  maintain: '유지',
  revise: '수정',
  retire: '폐기',
  unresolved: '미해결',
};

export function recordTitle({ record }: ResearchResponse): string {
  switch (record.kind) {
    case 'evidence':
      return record.payload.claim;
    case 'hypothesis':
      return record.payload.subject;
    case 'decision':
      return record.payload.objective;
    case 'review':
      return record.payload.what_changed;
  }
}

export function filterRecords(
  records: ResearchResponse[],
  search: string,
  kind: RecordKind | 'all',
): ResearchResponse[] {
  const needle = search.trim().toLocaleLowerCase('ko-KR');
  return records
    .filter(
      (item) =>
        (kind === 'all' || item.record.kind === kind) &&
        (!needle || JSON.stringify(item).toLocaleLowerCase('ko-KR').includes(needle)),
    )
    .toSorted(
      (a, b) =>
        b.record.recorded_at.localeCompare(a.record.recorded_at) || b.id.localeCompare(a.id),
    );
}

export function recordLinks({ record }: ResearchResponse): { id: string; label: string }[] {
  switch (record.kind) {
    case 'evidence':
      return [];
    case 'hypothesis':
      return [
        ...record.payload.supporting_evidence_ids.map((id) => ({ id, label: '찬성 근거' })),
        ...record.payload.opposing_evidence_ids.map((id) => ({ id, label: '반대 근거' })),
        ...(record.payload.supersedes_id
          ? [{ id: record.payload.supersedes_id, label: '이전 가설' }]
          : []),
      ];
    case 'decision':
      return [
        ...record.payload.hypothesis_ids.map((id) => ({ id, label: '연결 가설' })),
        ...record.payload.evidence_ids.map((id) => ({ id, label: '연결 근거' })),
        ...(record.payload.prior_decision_id
          ? [{ id: record.payload.prior_decision_id, label: '이전 판단' }]
          : []),
      ];
    case 'review':
      return [
        { id: record.payload.decision_id, label: '검토한 판단' },
        ...record.payload.new_evidence_ids.map((id) => ({ id, label: '새 근거' })),
        ...(record.payload.replacement_decision_id
          ? [{ id: record.payload.replacement_decision_id, label: '수정 판단' }]
          : []),
      ];
  }
}
