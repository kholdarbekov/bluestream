import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import {
  daysSince, colorForDays, DEFAULT_THRESHOLDS,
  loadThresholds, saveThresholds, applyFilters,
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
