import { describe, expect, it } from 'vitest';
import {
  paperCashRequest,
  paperProfileRequest,
  paperRevision,
  paperEventData,
  PaperRequestError,
  definitePaperError,
} from './paper';

describe('paper execution form', () => {
  it('rejects imprecise revisions and retains uncertain request errors for retry', () => {
    expect(paperRevision(1)).toBe(1);
    expect(() => paperRevision(9007199254740992)).toThrow();
    expect(definitePaperError(new PaperRequestError('paper_unavailable'))).toBe(false);
    expect(definitePaperError(new PaperRequestError('paper_conflict'))).toBe(true);
  });
  it('shows only explicit event scalar fields and preserves unknown amounts', () => {
    expect(
      paperEventData({ payload: { data: { price: '9007199254740993.001', fee: null } } }, 'price'),
    ).toBe('9007199254740993.001');
    expect(paperEventData({ payload: { data: { fee: null } } }, 'fee')).toBeNull();
    expect(
      paperEventData({ payload: { data: { fee: { secret: 'not displayable' } } } }, 'fee'),
    ).toBeNull();
  });
  it('keeps explicit cash exact and never fills unselected currencies', () => {
    expect(
      paperCashRequest([
        { currency: 'USD', enabled: true, amount: '9007199254740993.00100' },
        { currency: 'KRW', enabled: false, amount: '' },
      ]),
    ).toEqual([{ currency: 'USD', amount: '9007199254740993.00100' }]);
    expect(() => paperCashRequest([{ currency: 'USD', enabled: true, amount: '' }])).toThrow();
    expect(() => paperCashRequest([])).toThrow();
  });
  it('validates profile boundaries without rounding decimal strings', () => {
    expect(
      paperProfileRequest({
        slippage_bps: '9999.999999999999999',
        participation_bps: '10000',
        quantity_step: '0.0000000000000001',
      }),
    ).toMatchObject({ slippage_bps: '9999.999999999999999', quantity_step: '0.0000000000000001' });
    for (const profile of [
      { slippage_bps: '10000', participation_bps: '1', quantity_step: '1' },
      { slippage_bps: '0', participation_bps: '10000.000000000000001', quantity_step: '1' },
      { slippage_bps: '0', participation_bps: '0', quantity_step: '1' },
      { slippage_bps: '0', participation_bps: '1', quantity_step: '0' },
    ])
      expect(() => paperProfileRequest(profile)).toThrow();
  });
});
