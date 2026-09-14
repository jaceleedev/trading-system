import { describe, expect, it } from 'vitest';
import {
  changeGuidedSelection,
  emptyGuidedSelection,
  guidedKey,
  guidedLocation,
  readGuidedLocation,
} from './guided';

describe('explicit guided navigation', () => {
  it('round trips a frozen historical selection without inventing an account', () => {
    const selected = {
      ...emptyGuidedSelection(),
      investigation_id: 'old-investigation',
      revision: 2,
      output_id: 'old-output',
      plan_id: 'plan',
      alternative_id: 'hold',
      book_id: 'book',
      report_id: 'report',
    };
    const url = guidedLocation(new URL('http://localhost/?unrelated=keep'), selected, 'outcomes');
    expect(readGuidedLocation(url)).toEqual({ selection: selected, stage: 'outcomes' });
    expect(url.searchParams.has('snapshot_id')).toBe(false);
    expect(url.searchParams.get('unrelated')).toBe('keep');
  });
  it('changing a revision clears every downstream link while preserving explicit account', () => {
    const selected = {
      ...emptyGuidedSelection(),
      snapshot_id: 'latest-snapshot',
      investigation_id: 'investigation',
      revision: 2,
      output_id: 'old-output',
      plan_id: 'plan',
      alternative_id: 'hold',
      book_id: 'book',
      report_id: 'report',
    };
    expect(changeGuidedSelection(selected, 'revision', 1)).toEqual({
      ...emptyGuidedSelection(),
      snapshot_id: 'latest-snapshot',
      investigation_id: 'investigation',
      revision: 1,
    });
    expect(selected.report_id).toBe('report');
  });
  it('changing a book preserves plan selection and removes its prior report', () => {
    const selected = {
      ...emptyGuidedSelection(),
      plan_id: 'plan',
      alternative_id: 'hold',
      book_id: 'old',
      report_id: 'old-report',
    };
    expect(changeGuidedSelection(selected, 'book_id', 'new')).toEqual({
      ...selected,
      book_id: 'new',
      report_id: null,
    });
  });
  it.each(['0', '-1', 'nope', '1.5', '9007199254740993'])(
    'keeps malformed revision %s invalid instead of resolving a new revision',
    (value) => {
      expect(
        readGuidedLocation(new URL(`http://localhost/?revision=${value}`)).selection.revision,
      ).toBe(-1);
    },
  );
  it('fences a late response by account and every selected reference and stage', () => {
    const selected = { ...emptyGuidedSelection(), snapshot_id: 'one', revision: 1 };
    expect(guidedKey(selected, 'capital')).not.toBe(
      guidedKey({ ...selected, snapshot_id: 'two' }, 'capital'),
    );
    expect(guidedKey(selected, 'capital')).not.toBe(
      guidedKey({ ...selected, revision: 2 }, 'capital'),
    );
    expect(guidedKey(selected, 'capital')).not.toBe(guidedKey(selected, 'paper'));
  });
});
