import { describe, expect, it } from 'vitest';
import { buildSchedulePayload } from './Orders';

describe('buildSchedulePayload', () => {
  it('maps the Anytime preset to an open window', () => {
    expect(buildSchedulePayload({ preset: 'anytime', date: '2026-08-20' }))
      .toEqual({ delivery_date: '2026-08-20', delivery_window_start: null, delivery_window_end: null });
  });

  it('maps the Afternoon preset to 12:00-18:00', () => {
    expect(buildSchedulePayload({ preset: 'afternoon', date: '2026-08-20' }))
      .toEqual({ delivery_date: '2026-08-20', delivery_window_start: '12:00', delivery_window_end: '18:00' });
  });

  it('maps a custom deadline to an open start', () => {
    expect(buildSchedulePayload({ preset: 'custom', date: '2026-08-20', start: null, end: '10:00' }))
      .toEqual({ delivery_date: '2026-08-20', delivery_window_start: null, delivery_window_end: '10:00' });
  });

  it('maps a custom earliest time to an open end', () => {
    expect(buildSchedulePayload({ preset: 'custom', date: '2026-08-20', start: '19:00', end: null }))
      .toEqual({ delivery_date: '2026-08-20', delivery_window_start: '19:00', delivery_window_end: null });
  });

  it('omits the schedule entirely when no date is chosen', () => {
    expect(buildSchedulePayload({ preset: 'anytime', date: null })).toEqual({});
  });
});
