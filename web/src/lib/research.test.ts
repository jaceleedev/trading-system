import { describe, expect, it } from 'vitest';
import type { ResearchResponse } from './api/types.gen';
import { filterRecords, recordLinks } from './research';

const evidence: ResearchResponse = {
  id: 'a'.repeat(64),
  record: {
    kind: 'evidence',
    mode: 'synthetic',
    schema_version: 1,
    recorded_at: '2026-09-10T09:00:00+00:00',
    author: { interface: 'human', model: null, reasoning_effort: null, identity_source: 'unknown' },
    payload: {
      source_kind: 'web',
      source_locator: 'https://example.test/report',
      retrieved_at: '2026-09-10T08:59:00+00:00',
      source_published_at: null,
      claim: 'Alpha 시장 관측',
      verification: 'unverified',
    },
  },
};
const hypothesis: ResearchResponse = {
  id: 'b'.repeat(64),
  record: {
    kind: 'hypothesis',
    mode: 'synthetic',
    schema_version: 1,
    recorded_at: '2026-09-10T09:01:00+00:00',
    author: evidence.record.author,
    payload: {
      subject: '투자 근거 재검토',
      thesis: 'ALPHA의 가설',
      supporting_evidence_ids: [evidence.id],
      opposing_evidence_ids: ['c'.repeat(64)],
      uncertainties: [],
      invalidation_conditions: [],
      review_triggers: [],
    },
  },
};

describe('research discovery does not silently drop modes or opposing evidence', () => {
  it('searches title and body case-insensitively while combining the kind filter', () => {
    expect(filterRecords([evidence, hypothesis], '  alpha ', 'all').map((i) => i.id)).toEqual([
      hypothesis.id,
      evidence.id,
    ]);
    expect(filterRecords([evidence, hypothesis], 'alpha', 'evidence')).toEqual([evidence]);
    expect(filterRecords([evidence], 'missing', 'all')).toEqual([]);
  });
  it('retains source links on both sides even if records are omitted from the context', () => {
    expect(recordLinks(hypothesis)).toEqual([
      { id: evidence.id, label: '찬성 근거' },
      { id: 'c'.repeat(64), label: '반대 근거' },
    ]);
  });
});
