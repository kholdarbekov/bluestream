/**
 * Delivery-schedule helpers shared by Create Order and the Reschedule modal.
 *
 * Moved out of pages/Orders.js when the Reschedule modal became their second caller. The backend
 * owns what a schedule MEANS (business_app/utils/delivery_window.py); these only fill the two
 * window fields it stores and render the `kind` it publishes.
 */

// A Map, not a plain object, because the lookup key below comes from form
// state: `WINDOW_PRESETS[preset]` is a prototype-pollution sink (a `preset` of
// `__proto__` or `constructor` returns something that is not a preset), which
// is what security/detect-object-injection flags. Map.get() only ever returns
// own entries, so this is a real fix rather than a silenced rule.
export const WINDOW_PRESETS = new Map([
  ['anytime', { start: null, end: null }],
  ['morning', { start: '09:00', end: '12:00' }],
  ['afternoon', { start: '12:00', end: '18:00' }],
  ['evening', { start: '18:00', end: '21:00' }],
]);

// The admin UI never decides what a window MEANS — it only fills the two fields
// the backend stores. `kind` and the human label come back from the API.
export function buildSchedulePayload({ preset, date, start = null, end = null }) {
  if (!date) return {};
  const window = preset === 'custom' ? { start, end } : WINDOW_PRESETS.get(preset) || WINDOW_PRESETS.get('anytime');
  return {
    delivery_date: date,
    delivery_window_start: window.start,
    delivery_window_end: window.end,
  };
}

// The reverse of buildSchedulePayload, for a form that opens on a stored schedule: the preset
// whose edges are the stored ones, else Custom carrying them. This matches edges against the
// form's own presets; it does not classify the window — that is the backend's `kind`, which
// formatDeliveryWindowLabel reads.
export const presetFromWindow = (deliveryWindow) => {
  const start = deliveryWindow?.start ?? null;
  const end = deliveryWindow?.end ?? null;
  for (const [preset, edges] of WINDOW_PRESETS) {
    if (edges.start === start && edges.end === end) return { preset, start, end };
  }
  return { preset: 'custom', start, end };
};

// business_app/utils/delivery_window.py publishes `kind` as the machine-readable
// shape and `label` as an English-only log/fallback string. Rendering `label`
// to an operator is how English leaks into a Russian/Uzbek UI, so this builds
// the display string from `kind` + `start`/`end` and lets i18n own the wording.
// Branch on `kind`, never render `label`, never re-derive the shape from
// `start`/`end` (the backend is the one place that names the four shapes).
export const formatDeliveryWindowLabel = (deliveryWindow, t) => {
  if (!deliveryWindow) return null;
  const { kind, start, end } = deliveryWindow;
  switch (kind) {
    case 'between':
      return t('ui.orders.window_between', '{{start}}–{{end}}', { start, end });
    case 'until':
      return t('ui.orders.window_until', 'Before {{end}}', { end });
    case 'after':
      return t('ui.orders.window_after', 'After {{start}}', { start });
    case 'anytime':
    default:
      return t('ui.orders.window_anytime', 'Anytime');
  }
};
