import { describe, it, expect } from 'vitest';
import { orderChangeFields } from './orders';
describe('local order change inputs', () => {
  it('preserves exact explicit prices and leaves unspecified quantity unchanged', () => {
    expect(orderChangeFields('100.000000000000001', '')).toEqual({
      price: '100.000000000000001',
      quantity: null,
    });
    expect(orderChangeFields('100', '9007199254740993')).toEqual({
      price: '100',
      quantity: '9007199254740993',
    });
  });
  it('does not infer positive order values from empty, zero, signed or exponential inputs', () => {
    for (const [price, quantity] of [
      ['', '1'],
      ['0', '1'],
      ['1', '0'],
      ['1', '1e3'],
      ['-1', '2'],
    ])
      expect(() => orderChangeFields(price, quantity)).toThrow();
  });
});

import { OrderRequestError, definiteOrderError, orderRevision } from './orders';
it('native validation failures allow correction while unknown receipts remain frozen', () => {
  expect(definiteOrderError(new OrderRequestError('unsupported_us_price_precision', 409))).toBe(
    true,
  );
  expect(
    definiteOrderError(new OrderRequestError('fractional_quantity_requires_us_market_sell', 409)),
  ).toBe(true);
  expect(definiteOrderError(new OrderRequestError('order_store_unavailable', 503))).toBe(false);
  expect(definiteOrderError(new Error('Network unavailable'))).toBe(false);
  expect(() => orderRevision(Number.MAX_SAFE_INTEGER + 1)).toThrow();
});
