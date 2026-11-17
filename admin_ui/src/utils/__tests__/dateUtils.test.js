import { describe, it, expect } from 'vitest';
import { formatCalendarDate, formatDateTimeSeconds } from '../dateUtils';

describe('formatDateTimeSeconds', () => {
  it('formats a UTC ISO string to DD-MM-YYYY HH:mm:ss at UTC+5', () => {
    expect(formatDateTimeSeconds('2026-07-02T08:13:01.483654+00:00')).toBe('02-07-2026 13:13:01');
  });

  it('rolls the date forward when +5 crosses midnight', () => {
    expect(formatDateTimeSeconds('2026-07-02T20:30:00+00:00')).toBe('03-07-2026 01:30:00');
  });

  it('returns a dash for empty input', () => {
    expect(formatDateTimeSeconds(null)).toBe('-');
    expect(formatDateTimeSeconds(undefined)).toBe('-');
    expect(formatDateTimeSeconds('')).toBe('-');
  });
});

describe('formatCalendarDate', () => {
  it('prints a bare calendar day as that day, never shifted between zones', () => {
    expect(formatCalendarDate('2027-02-01')).toBe('Feb 01, 2027');
    expect(formatCalendarDate('2027-12-31')).toBe('Dec 31, 2027');
  });

  it('returns a dash for empty input', () => {
    expect(formatCalendarDate(null)).toBe('-');
    expect(formatCalendarDate(undefined)).toBe('-');
    expect(formatCalendarDate('')).toBe('-');
  });
});
