import { describe, it, expect } from 'vitest';
import {
  reorder, moveBy, togglePin, clampPins, buildSavePayload, isDirty, driverColor,
} from './dispatchLogic';

describe('reorder', () => {
  it('moves an item forward', () => {
    expect(reorder([1, 2, 3, 4], 0, 2)).toEqual([2, 3, 1, 4]);
  });

  it('moves an item backward', () => {
    expect(reorder([1, 2, 3, 4], 3, 1)).toEqual([1, 4, 2, 3]);
  });

  it('is a no-op for the same index', () => {
    expect(reorder([1, 2, 3], 1, 1)).toEqual([1, 2, 3]);
  });

  it('does not mutate the input', () => {
    const input = [1, 2, 3];
    reorder(input, 0, 2);
    expect(input).toEqual([1, 2, 3]);
  });

  it('ignores an out-of-range index', () => {
    expect(reorder([1, 2, 3], 5, 0)).toEqual([1, 2, 3]);
  });
});

describe('moveBy', () => {
  it('moves up', () => {
    expect(moveBy([1, 2, 3], 2, -1)).toEqual([1, 3, 2]);
  });

  it('clamps at the top', () => {
    expect(moveBy([1, 2, 3], 0, -1)).toEqual([1, 2, 3]);
  });

  it('clamps at the bottom', () => {
    expect(moveBy([1, 2, 3], 2, 1)).toEqual([1, 2, 3]);
  });
});

describe('togglePin', () => {
  it('pins a stop at its current position', () => {
    expect(togglePin({}, [7, 9, 11], 9)).toEqual({ 9: 1 });
  });

  it('unpins an already-pinned stop', () => {
    expect(togglePin({ 9: 1 }, [7, 9, 11], 9)).toEqual({});
  });

  it('records the new position after a reorder', () => {
    expect(togglePin({}, [9, 7, 11], 9)).toEqual({ 9: 0 });
  });
});

describe('clampPins', () => {
  it('drops pins whose stop left the route and compacts the rest', () => {
    expect(clampPins({ 7: 0, 9: 5, 11: 2 }, [7, 11])).toEqual({ 7: 0, 11: 1 });
  });

  it('maps each pin to its actual index in the new order, not its rank among survivors', () => {
    // 11 was pinned at position 0, 7 at position 2. New order is [7, 11],
    // so 7 should be pinned to index 0, 11 to index 1 — not ranked by
    // their original positions.
    expect(clampPins({ 11: 0, 7: 2 }, [7, 11])).toEqual({ 7: 0, 11: 1 });
  });

  it('returns an empty object for empty input', () => {
    expect(clampPins(null, [1, 2])).toEqual({});
  });
});

describe('buildSavePayload', () => {
  it('sends the draft order with the server set as the staleness guard', () => {
    expect(buildSavePayload({ ids: [9, 7], pinned: { 9: 0 } }, [7, 9])).toEqual({
      ordered_delivery_ids: [9, 7],
      pinned: { 9: 0 },
      expected_delivery_ids: [7, 9],
    });
  });

  it('re-clamps pins against the draft order before sending', () => {
    expect(buildSavePayload({ ids: [7], pinned: { 7: 0, 9: 1 } }, [7])).toEqual({
      ordered_delivery_ids: [7],
      pinned: { 7: 0 },
      expected_delivery_ids: [7],
    });
  });
});

describe('isDirty', () => {
  it('is false when nothing changed', () => {
    expect(isDirty([7, 9], { 7: 0 }, [7, 9], { 7: 0 })).toBe(false);
  });

  it('is true when the order changed', () => {
    expect(isDirty([9, 7], {}, [7, 9], {})).toBe(true);
  });

  it('is true when only a pin changed', () => {
    expect(isDirty([7, 9], { 7: 0 }, [7, 9], {})).toBe(true);
  });
});

describe('driverColor', () => {
  it('is stable for the same driver', () => {
    expect(driverColor(12)).toBe(driverColor(12));
  });

  it('separates two nearby driver ids', () => {
    expect(driverColor(12)).not.toBe(driverColor(13));
  });
});
