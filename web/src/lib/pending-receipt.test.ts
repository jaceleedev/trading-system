import { describe, expect, it } from 'vitest';
import { pendingReceiptKey, readPendingReceipt, writePendingReceipt } from './pending-receipt';
function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
}
describe('pending execution receipts', () => {
  it('preserves original keys, exact decimals and snapshot across reload and workspace changes', () => {
    const target = storage();
    const pending = {
      body: {
        request_key: 'original-key',
        snapshot_id: 'a'.repeat(64),
        amount: '0.000000000000000001',
      },
    };
    writePendingReceipt('one', 'paper', pending, target);
    expect(readPendingReceipt('one', 'paper', target)).toEqual(pending);
    expect(readPendingReceipt('two', 'paper', target)).toBeNull();
    expect(readPendingReceipt('one', 'capital', target)).toBeNull();
    writePendingReceipt('one', 'paper', null, target);
    expect(readPendingReceipt('one', 'paper', target)).toBeNull();
  });
  it('fails closed without deleting corrupt or foreign receipts', () => {
    const target = storage();
    const key = pendingReceiptKey('one', 'outcomes');
    for (const raw of [
      'null',
      '{',
      JSON.stringify({ version: 1, workspace: 'two', scope: 'outcomes', pending: {} }),
      JSON.stringify({ version: 1, workspace: 'one', scope: 'outcomes', pending: [] }),
    ]) {
      target.setItem(key, raw);
      expect(() => readPendingReceipt('one', 'outcomes', target)).toThrow('중복 실행');
      expect(target.getItem(key)).toBe(raw);
    }
  });
  it('blocks send preparation when storage is denied or silently fails', () => {
    const target = storage();
    const throwing = {
      ...target,
      setItem() {
        throw new Error('private-error');
      },
    };
    expect(() => writePendingReceipt('one', 'capital', { key: 'same' }, throwing)).toThrow(
      '중복 실행',
    );
    expect(() =>
      writePendingReceipt('one', 'capital', { key: 'same' }, { ...target, setItem() {} }),
    ).toThrow('중복 실행');
    expect(() =>
      readPendingReceipt('one', 'paper', {
        ...target,
        getItem() {
          throw new Error('private-error');
        },
      }),
    ).not.toThrow('private-error');
  });
  it('retains a receipt if its removal cannot be confirmed', () => {
    const target = storage();
    writePendingReceipt('one', 'paper', { key: 'same' }, target);
    expect(() => writePendingReceipt('one', 'paper', null, { ...target, removeItem() {} })).toThrow(
      '중복 실행',
    );
    expect(readPendingReceipt('one', 'paper', target)).toEqual({ key: 'same' });
  });
});
