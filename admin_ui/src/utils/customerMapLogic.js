export const DEFAULT_THRESHOLDS = { t1: 14, t2: 30 };
const STORAGE_KEY = 'customerMap.thresholds';
const NEVER_COLOR = '#9ca3af';

export function daysSince(lastOrderDate, now = new Date()) {
  if (!lastOrderDate) return null;
  const then = new Date(lastOrderDate).getTime();
  if (Number.isNaN(then)) return null;
  return Math.floor((now.getTime() - then) / 86400000);
}

export function colorForDays(days, t1, t2) {
  if (days === null || days === undefined) return NEVER_COLOR;
  let hue;
  if (days <= t1) hue = 120;
  else if (days >= t2) hue = 0;
  else hue = 120 * (1 - (days - t1) / (t2 - t1));
  return `hsl(${hue}, 70%, 45%)`;
}

export function loadThresholds() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_THRESHOLDS };
    const parsed = JSON.parse(raw);
    if (typeof parsed.t1 === 'number' && typeof parsed.t2 === 'number' && parsed.t1 < parsed.t2) {
      return { t1: parsed.t1, t2: parsed.t2 };
    }
  } catch { /* fall through to default */ }
  return { ...DEFAULT_THRESHOLDS };
}

export function saveThresholds({ t1, t2 }) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify({ t1, t2 }));
}

/**
 * Heat intensity for one map pin: one PLACE contributes one unit of heat, split
 * evenly across the pins that place puts on the map.
 *
 * WHY NOT `bottleBalance` (the D5 reading shipped 2026-07-28). Weighting by the
 * balance sent every zero-balance customer in at exactly 0, and in leaflet.heat
 * 0.2.0 a 0 does not read as "a bit fainter" — it pins the point to the opacity
 * floor and can delete its neighbours:
 *   - it never accumulates. A lone 0-pin still draws, but at
 *     `globalAlpha = Math.max(0 / _max, minOpacity)` — the same floor smudge
 *     whether one customer sits there or twenty, because merging 0s adds 0. The
 *     layer went flat over the idle / indebted / never-returned segments an
 *     admin most wants to see; and
 *   - two 0-weight pins landing in one 20px grid cell merge as
 *     `(x*0 + x*0) / (0 + 0)` -> NaN (`_redraw` in
 *     node_modules/leaflet.heat/dist/leaflet-heat.js), the cell is pushed as
 *     `[NaN, NaN, ...]`, and `drawImage` returns without drawing for a non-finite
 *     argument. The cell stays NaN afterwards, so it also erases any
 *     positive-weight place that later merges into it.
 * A strictly positive weight makes both impossible.
 *
 * WHAT THE DIVISOR IS FOR — de-duplication, and only that. `bottleBalance` is the
 * PLACE pool resolved through the address group (`customer_map_service.py`:
 * `coalesce(place_balance, solo_balance, 0)`), so three coworkers at one office
 * each report the same `7` and each drop a coincident pin. One place is one
 * delivery stop and one pool, so its pins share a single unit: `1 / members`
 * sums back to 1 however many members it has. Genuinely distinct neighbours
 * still sum to N — a dense block stays hot, which is the whole point of a
 * density layer.
 *
 * WHY MAGNITUDE IS NOT ENCODED AT ALL. `HeatLayer.js` sets no `max`, so simpleheat
 * clamps every cell at 1.0 while `_redraw` scales intensities by
 * `1 / 2^(maxZoom - zoom)`: at zoom 17 a 2-bottle pin and a 40-bottle pin both
 * saturate to the same red, and at zoom 11 everything below 1/64 sits on the
 * `minOpacity` floor. This layer can render "how many things are here", not
 * "how big is this one". Admins who want bottle hotspots have the "Has bottles"
 * filter, which narrows the layer AND the "Showing N/M" counter honestly.
 *
 * Degenerate member counts are clamped rather than propagated: a 0/missing/NaN
 * count would divide by zero and hand `Infinity` to leaflet.heat.
 */
export function heatWeight(pin) {
  const members = Number(pin?.placeMemberCount);
  const divisor = Number.isFinite(members) && members >= 1 ? members : 1;
  return 1 / divisor;
}

export function applyFilters(pins, filters = {}, now = new Date()) {
  const { idleMinDays = 0, bottleOnly = false, debtOnly = false, type = 'all' } = filters;
  return pins.filter((p) => {
    if (bottleOnly && !(Number(p.bottleBalance) > 0)) return false;
    if (debtOnly && !(Number(p.outstandingDebt) > 0)) return false;
    if (type && type !== 'all' && p.userType !== type) return false;
    if (idleMinDays > 0) {
      const d = daysSince(p.lastOrderDate, now);
      if (d === null || d < idleMinDays) return false;
    }
    return true;
  });
}
