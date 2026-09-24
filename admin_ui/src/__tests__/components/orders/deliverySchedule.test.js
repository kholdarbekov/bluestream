import { describe, expect, it } from 'vitest';

import {
  WINDOW_PRESETS,
  buildSchedulePayload,
  formatDeliveryWindowLabel,
  presetFromWindow,
} from '../../../components/orders/deliverySchedule';

// i18next's `{{token}}` interpolation over the positional English fallback — the shape every
// call in deliverySchedule.js uses.
const t = (key, fallback, options = {}) =>
  fallback.replace(/\{\{(\w+)\}\}/g, (_, token) => String(options[token]));

describe('presetFromWindow', () => {
  it('opens an order with no window on Anytime', () => {
    expect(presetFromWindow(null)).toEqual({ preset: 'anytime', start: null, end: null });
    expect(presetFromWindow({ start: null, end: null, kind: 'anytime', label: 'anytime' }))
      .toEqual({ preset: 'anytime', start: null, end: null });
  });

  it('recognises every preset the form offers from its stored edges', () => {
    for (const [preset, edges] of WINDOW_PRESETS) {
      expect(presetFromWindow({ ...edges, kind: 'between', label: 'x' }))
        .toEqual({ preset, start: edges.start, end: edges.end });
    }
  });

  it('opens any other stored window as Custom, with its own edges', () => {
    expect(presetFromWindow({ start: '10:30', end: null, kind: 'after', label: 'after 10:30' }))
      .toEqual({ preset: 'custom', start: '10:30', end: null });
    expect(presetFromWindow({ start: '09:00', end: '13:00', kind: 'between', label: '09:00-13:00' }))
      .toEqual({ preset: 'custom', start: '09:00', end: '13:00' });
  });

  it('round-trips through buildSchedulePayload, so an untouched form re-sends the stored window', () => {
    // The Reschedule modal's "nothing changed" gate (Task 16) compares exactly this.
    const stored = [
      { start: null, end: null },
      { start: '12:00', end: '18:00' },
      { start: null, end: '10:00' },
      { start: '19:00', end: null },
      { start: '08:15', end: '09:45' },
    ];
    for (const edges of stored) {
      const { preset, start, end } = presetFromWindow(edges);
      expect(buildSchedulePayload({ preset, date: '2026-09-25', start, end })).toEqual({
        delivery_date: '2026-09-25',
        delivery_window_start: edges.start,
        delivery_window_end: edges.end,
      });
    }
  });
});

describe('formatDeliveryWindowLabel', () => {
  it('builds the label from the published kind, never from the English `label`', () => {
    const shape = (kind, start, end) => ({ kind, start, end, label: 'ENGLISH-ONLY' });
    expect(formatDeliveryWindowLabel(shape('between', '12:00', '18:00'), t)).toBe('12:00–18:00');
    expect(formatDeliveryWindowLabel(shape('until', null, '10:00'), t)).toBe('Before 10:00');
    expect(formatDeliveryWindowLabel(shape('after', '19:00', null), t)).toBe('After 19:00');
    expect(formatDeliveryWindowLabel(shape('anytime', null, null), t)).toBe('Anytime');
    expect(formatDeliveryWindowLabel(null, t)).toBeNull();
  });
});
