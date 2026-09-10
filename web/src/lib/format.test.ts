import { describe, expect, it } from 'vitest';
import { formatDecimal, formatTime, safeSourceUrl } from './format';

describe('financial display preserves the provider string', () => {
  it('retains integers beyond binary floating point precision and every fractional digit', () => {
    expect(formatDecimal('9007199254740993123456789.12345678901234567890')).toBe(
      '9,007,199,254,740,993,123,456,789.12345678901234567890',
    );
  });
  it('keeps fractional holdings, trailing zeros, zero, and losses distinct', () => {
    expect(formatDecimal('0.00012500')).toBe('0.00012500');
    expect(formatDecimal('120.50')).toBe('120.50');
    expect(formatDecimal('0')).toBe('0');
    expect(formatDecimal('-12345.670')).toBe('-12,345.670');
  });
  it('does not turn missing values into zero or reinterpret exponent strings', () => {
    expect(formatDecimal(null)).toBe('미확인');
    expect(formatDecimal(undefined)).toBe('미확인');
    expect(formatDecimal('1E-8')).toBe('1E-8');
  });
});

it('renders a known UTC instant in Korea without using the machine timezone', () => {
  expect(formatTime('2026-09-10T09:03:00Z')).toContain('18:03');
  expect(formatTime('2026-09-10T09:03:00Z')).toContain('KST');
  expect(formatTime(null)).toBe('시각 미확인');
});

it('allows only credential-free HTTPS external source links', () => {
  expect(safeSourceUrl('https://example.com/report')).toBe('https://example.com/report');
  expect(safeSourceUrl('javascript:alert(1)')).toBeUndefined();
  expect(safeSourceUrl('http://example.com')).toBeUndefined();
  expect(safeSourceUrl('https://user:secret@example.com')).toBeUndefined();
});
