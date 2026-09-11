import { describe, expect, it } from 'vitest';
import { brokerScanParameters, type BrokerScanDraft } from './broker';
const draft: BrokerScanDraft = {
  fromDate: '2026-09-01',
  toDate: '2026-09-10',
  symbol: '',
  maxPages: '10',
  pageSize: '100',
  detailOrderIds: '',
};
describe('broker scan inputs', () => {
  it('preserves account identity as a string and explicit KST calendar date boundaries', () => {
    expect(brokerScanParameters('9007199254740993', draft)).toEqual({
      account_seq: '9007199254740993',
      mode: 'prospective',
      from_date: '2026-09-01',
      to_date: '2026-09-10',
      symbol: null,
      max_pages: 10,
      page_size: 100,
      detail_order_ids: [],
    });
  });
  it('rejects invalid calendar dates and reversed or unbounded request ranges', () => {
    for (const input of [
      { ...draft, fromDate: '2026-02-30' },
      { ...draft, toDate: '2026-08-30' },
      { ...draft, maxPages: '11' },
      { ...draft, pageSize: '0' },
    ])
      expect(() => brokerScanParameters('101', input)).toThrow();
    expect(
      brokerScanParameters('101', {
        ...draft,
        fromDate: '',
        toDate: '',
        detailOrderIds: 'ORDER,A\nORDER-B\nORDER,A',
      }),
    ).toMatchObject({ from_date: null, to_date: null, detail_order_ids: ['ORDER,A', 'ORDER-B'] });
  });
});
