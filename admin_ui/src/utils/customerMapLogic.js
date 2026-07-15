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
