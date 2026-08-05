import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  daysSince, colorForDays, DEFAULT_THRESHOLDS,
  loadThresholds, saveThresholds, applyFilters, heatWeight,
} from './customerMapLogic';

const NOW = new Date('2026-07-14T00:00:00Z');

describe('daysSince', () => {
  it('returns whole days between dates', () => {
    expect(daysSince('2026-07-04T00:00:00Z', NOW)).toBe(10);
  });
  it('returns null for missing date', () => {
    expect(daysSince(null, NOW)).toBeNull();
  });
});

describe('colorForDays', () => {
  it('is green at/under t1', () => {
    expect(colorForDays(0, 14, 30)).toBe(colorForDays(14, 14, 30));
    expect(colorForDays(5, 14, 30)).toMatch(/hsl\(120/);
  });
  it('is red at/over t2', () => {
    expect(colorForDays(30, 14, 30)).toMatch(/hsl\(0[,.]/);
    expect(colorForDays(90, 14, 30)).toMatch(/hsl\(0[,.]/);
  });
  it('interpolates between t1 and t2 (hue between 0 and 120)', () => {
    const c = colorForDays(22, 14, 30); // midpoint-ish
    const hue = Number(c.match(/hsl\(([\d.]+)/)[1]);
    expect(hue).toBeGreaterThan(0);
    expect(hue).toBeLessThan(120);
  });
  it('returns grey for null days', () => {
    expect(colorForDays(null, 14, 30)).toBe('#9ca3af');
  });
});

describe('thresholds persistence', () => {
  // setupTests.js stubs localStorage with no-op vi.fn()s that store nothing;
  // install a functional Map-backed stub for these cases.
  beforeEach(() => {
    const store = new Map();
    vi.stubGlobal('localStorage', {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
      clear: () => store.clear(),
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it('defaults when nothing stored', () => {
    expect(loadThresholds()).toEqual(DEFAULT_THRESHOLDS);
  });
  it('round-trips', () => {
    saveThresholds({ t1: 7, t2: 21 });
    expect(loadThresholds()).toEqual({ t1: 7, t2: 21 });
  });
});

describe('applyFilters', () => {
  const pins = [
    { userId: 1, lastOrderDate: '2026-07-13T00:00:00Z', bottleBalance: 0, outstandingDebt: 0, userType: 'individual' },
    { userId: 2, lastOrderDate: '2026-05-01T00:00:00Z', bottleBalance: 3, outstandingDebt: 0, userType: 'individual' },
    { userId: 3, lastOrderDate: '2026-06-01T00:00:00Z', bottleBalance: 0, outstandingDebt: 5000, userType: 'entity' },
  ];
  it('idleMinDays keeps only stale pins', () => {
    const out = applyFilters(pins, { idleMinDays: 30 }, NOW);
    expect(out.map((p) => p.userId).sort()).toEqual([2, 3]);
  });
  it('bottleOnly keeps balance>0', () => {
    expect(applyFilters(pins, { bottleOnly: true }, NOW).map((p) => p.userId)).toEqual([2]);
  });
  it('debtOnly keeps debt>0', () => {
    expect(applyFilters(pins, { debtOnly: true }, NOW).map((p) => p.userId)).toEqual([3]);
  });
  it('type filter narrows to a user_type', () => {
    expect(applyFilters(pins, { type: 'entity' }, NOW).map((p) => p.userId)).toEqual([3]);
  });
  it('idleMinDays excludes never-ordered pins (lastOrderDate: null)', () => {
    const localPins = [
      ...pins,
      { userId: 4, lastOrderDate: null, bottleBalance: 0, outstandingDebt: 0, userType: 'individual' },
    ];
    const out = applyFilters(localPins, { idleMinDays: 30 }, NOW);
    expect(out.map((p) => p.userId).sort()).toEqual([2, 3]);
    expect(out.map((p) => p.userId)).not.toContain(4);
  });
  it('combines two filters with AND', () => {
    expect(applyFilters(pins, { bottleOnly: true, type: 'individual' }, NOW).map((p) => p.userId)).toEqual([2]);
  });
});

describe('heatWeight', () => {
  // The layer answers "where are my customers?", so a pin's weight is about the
  // PLACE it belongs to, never about how many empties that place happens to hold.
  // `bottleBalance` is the place POOL repeated on every coworker's pin, so it can
  // only be used to de-duplicate — and using it as the magnitude deleted every
  // zero-balance customer from the map.
  it('keeps a zero-balance customer on the map', () => {
    // The regression this test exists for: weighting by `bottleBalance` returned
    // EXACTLY 0 here, and leaflet.heat neither accumulates 0 nor survives merging
    // two of them in one grid cell (0/0 -> NaN coordinates -> the cell is never
    // drawn, taking any positive pin that joins it down too).
    const w = heatWeight({ bottleBalance: 0, placeMemberCount: 1 });
    expect(w).toBeGreaterThan(0);
    expect(Number.isFinite(w)).toBe(true);
  });

  it('does not let the bottle balance scale the layer at all', () => {
    // An empty-handed customer and a 40-bottle grocery store are one place each.
    // `HeatLayer.js` passes no `max`, so simpleheat clamps every cell at 1.0 —
    // magnitudes above the zoom scale are indistinguishable anyway.
    const empty = heatWeight({ bottleBalance: 0, placeMemberCount: 1 });
    const stocked = heatWeight({ bottleBalance: 40, placeMemberCount: 1 });
    expect(empty).toBe(stocked);
    // Over-returned (negative) balances are not special either — still one place.
    expect(heatWeight({ bottleBalance: -4, placeMemberCount: 1 })).toBe(empty);
  });

  it('splits one shared place across its pins so a cluster cannot dominate by count', () => {
    // addressCount is deliberately a DIFFERENT number: dividing by the per-user
    // address counter instead of the place member count yields 1/5, not 1/3.
    expect(heatWeight({ bottleBalance: 7, placeMemberCount: 3, addressCount: 5 })).toBeCloseTo(1 / 3);
  });

  it('makes the three pins of one shared place sum to a single place', () => {
    const office = { bottleBalance: 7, placeMemberCount: 3, isSharedPlace: true };
    const total = [office, office, office].reduce((acc, p) => acc + heatWeight(p), 0);
    expect(total).toBeCloseTo(1);
    // ...i.e. exactly as hot as ONE solo neighbour, not three.
    expect(total).toBeCloseTo(heatWeight({ bottleBalance: 0, placeMemberCount: 1 }));
  });

  it('still lets a genuinely dense block read as dense', () => {
    // De-duplication must not flatten real density: five UNRELATED solo customers
    // in one block are five places and sum to five, while five coworkers at one
    // address sum to one.
    const neighbours = Array.from({ length: 5 }, (_, i) => ({ userId: i, placeMemberCount: 1 }));
    const coworkers = Array.from({ length: 5 }, (_, i) => ({ userId: i, placeMemberCount: 5 }));
    const sum = (pins) => pins.reduce((acc, p) => acc + heatWeight(p), 0);
    expect(sum(neighbours)).toBeCloseTo(5);
    expect(sum(coworkers)).toBeCloseTo(1);
  });

  it('never divides by zero or emits NaN/Infinity for a missing or degenerate member count', () => {
    for (const count of [0, undefined, null, -3, NaN, 'three']) {
      const w = heatWeight({ bottleBalance: 7, placeMemberCount: count });
      expect(Number.isFinite(w)).toBe(true);
      expect(w).toBe(1);
    }
  });

  it('is positive for every shape of pin the backend or a filter can hand it', () => {
    for (const pin of [{}, undefined, null, { placeMemberCount: 2 }, { bottleBalance: 0 }]) {
      const w = heatWeight(pin);
      expect(Number.isFinite(w)).toBe(true);
      expect(w).toBeGreaterThan(0);
    }
  });

  it('accepts the numeric strings a JSON payload can carry', () => {
    expect(heatWeight({ bottleBalance: '7', placeMemberCount: '3' })).toBeCloseTo(1 / 3);
  });
});
