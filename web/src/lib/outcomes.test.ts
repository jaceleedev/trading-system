import { describe, expect, it } from 'vitest';
import { outcomeRatio, outcomeSelection, outcomeTime } from './outcomes';

describe('outcome input and display boundaries', () => {
  it('preserves unknown and exact negative ratios without percent rounding', () => {
    expect(outcomeRatio(null)).toBe('미확인');
    expect(outcomeRatio('-0.000000000000000123456789')).toBe('-0.000000000000000123456789');
    expect(outcomeRatio('0')).toBe('0');
  });
  it('deduplicates each explicit source selection and caps it independently', () => {
    expect(outcomeSelection(['a', 'a', 'b'], '모의 원장')).toEqual(['a', 'b']);
    expect(() => outcomeSelection(['a', 'b', 'c', 'd', 'e'], '모의 원장')).toThrow('최대 4개');
    expect(outcomeSelection([], '운용 흐름')).toEqual([]);
  });
  it('keeps an unspecified endpoint distinct from an explicit UTC timestamp', () => {
    expect(outcomeTime('', '종료 시각')).toBeNull();
    expect(outcomeTime('2026-09-11T09:30:00+09:00', '시작 시각')).toBe('2026-09-11T00:30:00.000Z');
    expect(() => outcomeTime('invalid', '종료 시각')).toThrow();
  });
});
