/**
 * Pure logic for the dispatch stop editor.
 *
 * Everything here is a function of its arguments: no React, no network, no
 * Leaflet. The panel and map components stay thin because the decisions —
 * what a drag does, which pins survive, whether a draft differs from the
 * server — are testable on their own. Mirrors `utils/customerMapLogic.js`.
 */

/** Move `ids[from]` to index `to`, returning a new array. */
export const reorder = (ids, from, to) => {
  const next = [...ids];
  if (from < 0 || from >= next.length || to < 0 || to >= next.length || from === to) return next;
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  return next;
};

/** Nudge one stop by `delta` positions, clamped to the ends. */
export const moveBy = (ids, index, delta) => {
  const target = index + delta;
  if (target < 0 || target >= ids.length) return [...ids];
  return reorder(ids, index, target);
};

/**
 * Pin a stop to wherever it currently sits, or unpin it.
 *
 * A pin means "keep this stop at this slot when you re-solve", so the position
 * is captured from the CURRENT draft order rather than asked for separately —
 * the admin drags a stop where they want it and then locks it.
 */
export const togglePin = (pinned, ids, deliveryId) => {
  const key = String(deliveryId);
  const current = { ...(pinned || {}) };
  if (key in current) {
    // Rebuild without the key rather than `delete current[key]`. A computed
    // member write on an object whose keys originate in caller data is what
    // security/detect-object-injection guards against; going through entries
    // gets the same result without one, and matches `clampPins` below.
    return Object.fromEntries(Object.entries(current).filter(([k]) => k !== key));
  }
  const position = ids.indexOf(deliveryId);
  if (position === -1) return current;
  return { ...current, [key]: position };
};

/**
 * Drop pins whose stop is no longer in `ids`, then set each survivor to its
 * **actual index in `ids`** (not its rank among survivors).
 *
 * Mirrors `RouteOptimizationService.clamp_pins` on the server. Each delivery's
 * pinned slot must match the position it actually occupies in the new route.
 * Without this a pin recorded at slot 2 on a 3-stop route could point past the
 * end of a 2-stop one. The solver would clamp it to "last", silently promoting
 * a mid-route pin to the final stop — exactly the wrong pin choice.
 */
export const clampPins = (pinned, ids) => {
  if (!pinned) return {};
  const indexMap = new Map(ids.map((id, i) => [String(id), i]));
  return Object.entries(pinned)
    .filter(([key]) => indexMap.has(key))
    .reduce((acc, [key]) => ({ ...acc, [key]: indexMap.get(key) }), {});
};

/**
 * `expected_delivery_ids` is the SERVER's set, not the draft's: it is the
 * staleness guard, and sending the draft's own ids would make it vacuous.
 */
export const buildSavePayload = (draft, serverIds) => ({
  ordered_delivery_ids: [...draft.ids],
  pinned: clampPins(draft.pinned, draft.ids),
  expected_delivery_ids: [...serverIds],
});

const samePins = (a, b) => {
  const left = Object.entries(a || {});
  const right = new Map(Object.entries(b || {}));
  if (left.length !== right.size) return false;
  // `has` before `get` so a pin explicitly set to undefined on one side can't
  // read as equal to an absent key on the other.
  return left.every(([k, v]) => right.has(k) && right.get(k) === v);
};

/** True when the draft differs from the server state in order or pins. */
export const isDirty = (draftIds, draftPinned, serverIds, serverPinned) => {
  if (draftIds.length !== serverIds.length) return true;
  // Index read is provably in bounds — the lengths were just compared, and `i`
  // comes from the iteration itself, never from caller data.
  // eslint-disable-next-line security/detect-object-injection
  if (draftIds.some((id, i) => id !== serverIds[i])) return true;
  return !samePins(draftPinned, serverPinned);
};

// Categorical hues for route lines. Replace with the project's validated
// categorical palette (see the `dataviz` skill's references/palette.md) rather
// than extending this list ad hoc — these are placeholders chosen for
// separability in both themes.
const DRIVER_HUES = [210, 28, 145, 280, 55, 190, 330, 95];

/** Stable per-driver colour: the same driver is the same hue every render. */
export const driverColor = (driverId) => {
  const index = Math.abs(Number(driverId) || 0) % DRIVER_HUES.length;
  // Module-private constant array, index reduced mod its own length — in bounds
  // by construction, and the array is never keyed by caller-supplied strings.
  // eslint-disable-next-line security/detect-object-injection
  return `hsl(${DRIVER_HUES[index]}, 68%, 45%)`;
};
