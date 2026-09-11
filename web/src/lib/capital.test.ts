import { expect, it } from 'vitest';
import {
  capitalDecimal,
  capitalFormRequest,
  capitalInputKey,
  newCapitalAlternative,
  poolRevisions,
} from './capital';
import type { FundingState } from './api/types.gen';

it('preserves fractional quantities and prices without converting them into JS numbers', () => {
  expect(capitalDecimal(' 0.000000000000001 ', '수량')).toBe('0.000000000000001');
  expect(capitalDecimal('9007199254740993.0123456789', '가격')).toBe('9007199254740993.0123456789');
  expect(capitalDecimal('0', '수수료')).toBe('0');
  for (const value of ['', '-1', 'NaN', 'Infinity', '1e3', '1,000'])
    expect(() => capitalDecimal(value, '금액')).toThrow();
});

it('sends independent alternatives with exact amounts and preserves an unspecified price as null', () => {
  const alternative = newCapitalAlternative(1);
  alternative.rationale = '합성 대안 비교';
  Object.assign(alternative.legs[0], {
    action: 'add',
    symbol: 'ALPHA',
    quantity: '0.125',
    price: '',
    fee_bps: '0',
    fixed_fee: '0',
    tax_bps: '0',
    rationale: '합성 추가 매수 검토',
  });
  const request = capitalFormRequest(
    'a'.repeat(64),
    `decision:${'b'.repeat(64)}`,
    'synthetic',
    [
      { currency: 'KRW', enabled: true, limit_amount: '1000', reserve_amount: '200' },
      { currency: 'USD', enabled: false, limit_amount: '', reserve_amount: '' },
    ],
    [alternative],
  );
  expect(request.funding).toEqual([
    { currency: 'KRW', limit_amount: '1000', reserve_amount: '200' },
  ]);
  expect(request.alternatives[0].legs[0]).toMatchObject({
    quantity: '0.125',
    price: null,
    fee_bps: '0',
  });
  expect(request.alternatives[0].legs[0]).not.toHaveProperty('rowId');
  alternative.legs[0].quantity = '';
  expect(() =>
    capitalFormRequest(
      request.snapshot_id,
      `decision:${request.source.id}`,
      'synthetic',
      [{ currency: 'KRW', enabled: true, limit_amount: '1000', reserve_amount: '200' }],
      [alternative],
    ),
  ).toThrow();
});

it('invalidates a calculation after the account or any amount assumption changes', () => {
  const initial = { snapshot_id: 'first', limits: { USD: '1000' }, quantity: '0.125' };
  expect(capitalInputKey({ ...initial })).toBe(capitalInputKey(initial));
  expect(capitalInputKey({ ...initial, snapshot_id: 'second' })).not.toBe(capitalInputKey(initial));
  expect(capitalInputKey({ ...initial, quantity: '0.126' })).not.toBe(capitalInputKey(initial));
});

it('preserves server revision hints for pools not created yet and refuses rounded revisions', () => {
  const id = 'a'.repeat(64);
  const state: FundingState = {
    account_seq: '101',
    provider: 'toss',
    pools: [],
    reservations: [],
    omitted_reservation_count: 0,
    expected_pool_revisions: { [id]: 0 },
    execution_ready: false,
  };
  expect(poolRevisions(state)).toEqual({ [id]: 0 });
  expect(() =>
    poolRevisions({ ...state, expected_pool_revisions: { [id]: 9007199254740992 } }),
  ).toThrow();
});
