import { describe, it, expect } from 'vitest';
import { workflowRevision, workflowText } from './workflows';
describe('workflow presentation boundaries', () => {
  it('keeps exact identifiers and decimal strings while refusing untyped structures', () => {
    const value = {
      amount: '9007199254740993.000000000000001',
      output_id: 'a'.repeat(64),
      status: { value: 'completed' },
      missing: null,
    };
    expect(workflowText(value, 'amount')).toBe('9007199254740993.000000000000001');
    expect(workflowText(value, 'output_id')).toBe('a'.repeat(64));
    expect(workflowText(value, 'status')).toBeNull();
    expect(workflowText(value, 'missing')).toBeNull();
  });
  it('cannot round a server revision into another mutation version', () => {
    expect(workflowRevision(1)).toBe(1);
    for (const value of [0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1])
      expect(() => workflowRevision(value)).toThrow();
  });
});
