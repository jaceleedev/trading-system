import { expect, it } from 'vitest';
import { executionText, investigationState, parseInvestigationSymbols } from './investigations';

it('keeps selected ticker spelling without inferring holdings or forcing a market', () => {
  expect(parseInvestigationSymbols(' ALPHA, BETA\nALPHA, BRK.B, C-D ')).toEqual([
    'ALPHA',
    'BETA',
    'BRK.B',
    'C-D',
  ]);
  expect(parseInvestigationSymbols('')).toEqual([]);
  expect(parseInvestigationSymbols('alpha')).toEqual(['alpha']);
});

it('does not call an active investigation running until its matching job is observed', () => {
  const item = {
    status: 'active' as const,
    active_job_id: 'first',
    latest_completed_revision: 1,
    current_revision: 2,
  };
  expect(investigationState(item)).toBe('작업 상태 확인 필요');
  expect(investigationState(item, { id: 'second', status: 'running' })).toBe('작업 상태 확인 필요');
  expect(investigationState(item, { id: 'first', status: 'queued' })).toBe('대기');
  expect(investigationState(item, { id: 'first', status: 'running' })).toBe('조사 중');
  expect(investigationState({ ...item, active_job_id: null })).toBe('현재 결과 미확인');
  expect(investigationState({ ...item, active_job_id: null, latest_completed_revision: 2 })).toBe(
    '결과 저장 완료',
  );
});

it('keeps unavailable execution metadata unknown and never expands arbitrary objects', () => {
  expect(executionText({ reported_model: null }, 'reported_model')).toBe('미확인');
  expect(executionText({ requested_model: 'declared-name' }, 'requested_model')).toBe(
    'declared-name',
  );
  expect(executionText({ unexpected: { private_path: 'not-for-display' } }, 'unexpected')).toBe(
    '미확인',
  );
});

it('rejects invalid or unbounded symbol input before creating an investigation', () => {
  expect(() => parseInvestigationSymbols('ALPHA,<script>')).toThrow();
  expect(() => parseInvestigationSymbols('A'.repeat(33))).toThrow();
  expect(() =>
    parseInvestigationSymbols(Array.from({ length: 101 }, (_, index) => `A${index}`).join(',')),
  ).toThrow();
});
