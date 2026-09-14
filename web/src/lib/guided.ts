import { resolveGuidedFlow } from './api/sdk.gen';
import type { GuidedFlowResponse, GuidedSelection } from './api/types.gen';

export type GuidedStage = 'investigations' | 'capital' | 'paper' | 'outcomes';
export type GuidedResolution = GuidedFlowResponse;
export type GuidedRequest = {
  stage: GuidedStage;
  resolution: GuidedResolution;
};
export const guidedStages: GuidedStage[] = ['investigations', 'capital', 'paper', 'outcomes'];
export const guidedFields = [
  'snapshot_id',
  'investigation_id',
  'revision',
  'output_id',
  'plan_id',
  'alternative_id',
  'book_id',
  'report_id',
] as const;
export const emptyGuidedSelection = (): GuidedSelection => ({
  snapshot_id: null,
  investigation_id: null,
  revision: null,
  output_id: null,
  plan_id: null,
  alternative_id: null,
  book_id: null,
  report_id: null,
});
export function readGuidedLocation(url: URL) {
  const selection = emptyGuidedSelection();
  for (const field of guidedFields) {
    const value = url.searchParams.get(field);
    if (field === 'revision') {
      // Invalid revisions remain invalid input instead of silently resolving the latest revision.
      selection.revision =
        value === null
          ? null
          : /^\d+$/.test(value) && Number.isSafeInteger(Number(value)) && Number(value) > 0
            ? Number(value)
            : -1;
    } else selection[field] = value || null;
  }
  const requestedStage = url.searchParams.get('stage');
  const stage: GuidedStage = guidedStages.includes(requestedStage as GuidedStage)
    ? (requestedStage as GuidedStage)
    : 'investigations';
  return { selection, stage };
}
export function guidedLocation(url: URL, selection: GuidedSelection, stage: GuidedStage) {
  const next = new URL(url);
  for (const field of guidedFields) {
    const value = selection[field];
    if (value !== null && value !== undefined && value !== '')
      next.searchParams.set(field, String(value));
    else next.searchParams.delete(field);
  }
  if (guidedFields.some((field) => field !== 'snapshot_id' && selection[field] !== null))
    next.searchParams.set('stage', stage);
  else next.searchParams.delete('stage');
  return next;
}
export function guidedKey(selection: GuidedSelection, stage: GuidedStage) {
  return JSON.stringify([stage, ...guidedFields.map((field) => selection[field])]);
}
/** Changing one link invalidates its descendants; merely moving stages preserves every link. */
export function changeGuidedSelection(
  selection: GuidedSelection,
  field: Exclude<(typeof guidedFields)[number], 'snapshot_id'>,
  value: string | number | null,
) {
  const next = { ...selection };
  const index = guidedFields.indexOf(field);
  for (const downstream of guidedFields.slice(index)) {
    if (downstream === 'revision') next.revision = null;
    else next[downstream] = null;
  }
  if (field === 'revision') next.revision = typeof value === 'number' ? value : null;
  else next[field] = typeof value === 'string' && value ? value : null;
  return next;
}
export async function fetchGuidedFlow(selection: GuidedSelection, signal?: AbortSignal) {
  const result = await resolveGuidedFlow({
    baseUrl: window.location.origin,
    cache: 'no-store',
    credentials: 'same-origin',
    signal,
    query: Object.fromEntries(Object.entries(selection).filter(([, value]) => value !== null)),
  });
  if (result.error || !result.data)
    throw new Error('선택한 자료의 연결을 확인하지 못했습니다. 연결을 다시 확인한 뒤 이어가세요.');
  return result.data;
}
